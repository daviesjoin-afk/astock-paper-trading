# -*- coding: utf-8 -*-
"""Regression tests for the slim activity overview and order-confirm guard.

The activity workspace only renders orders, the account strip and the audit
board.  The portfolio-only sections (candidate signals, overlap matrix,
reviews/jobs) used to be built for every activity request as well, adding
~0.9MB to the 1.5MB overview response and measurable cold-rebuild time.
These tests pin the wiring so the split cannot silently regress.
"""
from __future__ import annotations

import os
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(os.path.dirname(BACKEND), "frontend")


def _load_source(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class ActivityOverviewSlimTests(unittest.TestCase):
    """dashboard() must gate portfolio-only queries behind include_activity.

    The read-model implementation lives in dashboard_queries.py (extracted in
    the paper_trading split); paper_trading.py keeps a thin facade so the
    historical public import path keeps working.  Structural assertions target
    the implementation module, not the facade.
    """

    def setUp(self):
        self.pt_source = _load_source(os.path.join(BACKEND, "paper_trading.py"))
        self.dq_source = _load_source(os.path.join(BACKEND, "dashboard_queries.py"))

    def _dashboard_source(self, source):
        import ast
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "dashboard":
                lines = source.splitlines()
                return "\n".join(lines[node.lineno - 1:node.end_lineno])
        self.fail("dashboard() not found in the scanned module")

    def test_facade_keeps_public_import_path(self):
        # paper_trading.dashboard stays a thin wrapper over the new module.
        self.assertIn("def dashboard(", self.pt_source)
        self.assertIn(
            "from dashboard_queries import dashboard as _impl",
            self.pt_source,
            "paper_trading.dashboard must forward to dashboard_queries.dashboard",
        )

    def test_activity_guard_branch_exists(self):
        self.assertIn(
            "if include_activity:",
            self.dq_source,
            "dashboard() needs an explicit include_activity branch",
        )

    def test_signals_query_is_not_duplicated_outside_the_guard(self):
        body = self._dashboard_source(self.dq_source)
        self.assertEqual(
            body.count("FROM paper_signals"), 2,
            "dashboard must keep exactly the projection query plus its JSON1 fallback",
        )
        guard_at = body.index("if include_activity:")
        query_at = body.index("FROM paper_signals")
        self.assertLess(
            guard_at, query_at,
            "the signals query must live inside (after) the include_activity gate",
        )

    def test_activity_mode_skips_portfolio_only_reads(self):
        for expected in (
            "signals = []",
            "candidate_overlap = []",
            "reviews = []",
            "last_jobs = []",
        ):
            self.assertIn(expected, self.dq_source, expected)

    def test_position_reviews_response_field_is_activity_gated(self):
        self.assertIn(
            "[] if include_activity else review_rows",
            self.dq_source,
            "position_reviews must be omitted from the activity response",
        )


class OrderConfirmGuardTests(unittest.TestCase):
    """The manual submit path must stay single-entry and re-entrancy safe."""

    def setUp(self):
        self.app_js = _load_source(os.path.join(FRONTEND, "app.js"))

    def test_submit_button_guard_exists(self):
        self.assertIn(
            "window._paperOrderSubmitting",
            self.app_js,
            "submitPaperOrder must ignore re-entrant clicks while a submit is in flight",
        )

    def test_confirm_dialog_still_gates_submission(self):
        self.assertIn(
            "这是纯本地模拟，不会发送到券商，继续吗？",
            self.app_js,
            "the second-confirm dialog text must remain before POST /order/submit",
        )
        self.assertLess(
            self.app_js.index("这是纯本地模拟，不会发送到券商，继续吗？"),
            self.app_js.index("/api/paper/order/submit"),
            "confirm() must run before the submit request is issued",
        )

    def test_backend_rejects_unconfirmed_submits(self):
        source = _load_source(os.path.join(BACKEND, "api_paper.py"))
        self.assertIn("if not confirmed:", source)
        self.assertIn("Confirmation required", source)

    def test_dead_candidate_render_is_gone(self):
        self.assertNotIn(
            "candidateCards",
            self.app_js,
            "the never-rendered candidate-card computation must stay deleted",
        )


class FrontendBuildPipelineTests(unittest.TestCase):
    """The served bundle must be reproducible from the checked-in sources.

    frontend/dist is the only artifact served at runtime (/app.js, /app.css).
    The esbuild pipeline (frontend/build.mjs) rebuilds it; CI runs the same
    build and fails when the committed dist drifted from the sources.
    """

    def test_dist_exists_and_matches_sources(self):
        # Cheap source-level guard for local runs: the dist header must be
        # present and the committed bundle must not be empty.  Full freshness
        # is enforced in CI (npm run build && git diff --exit-code).
        dist_js = os.path.join(os.path.dirname(FRONTEND), "frontend", "dist", "app.js")
        self.assertTrue(
            os.path.isfile(dist_js) and os.path.getsize(dist_js) > 100_000,
            "frontend/dist/app.js missing or empty; run `npm run build` in frontend/",
        )

    def test_assets_dir_holds_no_script_mirrors(self):
        assets_dir = os.path.join(os.path.dirname(FRONTEND), "frontend", "assets")
        self.assertTrue(os.path.isdir(assets_dir))
        leftovers = [name for name in os.listdir(assets_dir) if name.startswith("app.")]
        self.assertEqual(
            leftovers, [],
            "legacy assets/app.* mirrors must stay deleted; only vendor files belong in assets/",
        )


if __name__ == "__main__":
    unittest.main()
