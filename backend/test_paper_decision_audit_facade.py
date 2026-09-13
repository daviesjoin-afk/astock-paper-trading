# -*- coding: utf-8 -*-
"""Facade delegation and architecture guards for the issue #124 cutover.

After the cutover ``paper_trading`` no longer owns a decision-audit serializer:
it exposes a compatibility facade that injects the live runtime dependencies
and delegates to :mod:`paper_decision_audit`.  These tests assert

* the delegation really happens and receives the *current* (patchable) runtime
  objects rather than import-time frozen copies;
* the facade still produces the frozen golden envelope end to end;
* the module dependency direction and the "single source of truth" invariant
  cannot silently regress into a second duplicated implementation.
"""
import ast
import copy
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_decision_audit as audit
import paper_trading as paper

from test_paper_decision_audit import (  # noqa: E402 - path injected above
    GOLDEN_ENVELOPE,
    GOLDEN_SCAN_META,
    _kline,
    _payload,
    _quote,
)

BACKEND = Path(__file__).resolve().parent
PAPER_TRADING_PATH = BACKEND / "paper_trading.py"
AUDIT_PATH = BACKEND / "paper_decision_audit.py"

# String constants that only the real decision-audit serializer can emit.  They
# are the contract keys of the envelope, so a renamed copy of the algorithm
# still has to contain them.
SERIALIZER_SENTINELS = {
    "decision-snapshot-v1",
    "rows_stored",
    "omitted_rows",
    "future_excluded",
}

FORBIDDEN_AUDIT_IMPORT_ROOTS = {
    "sqlite3",
    "requests",
    "urllib",
    "http",
    "socket",
    "subprocess",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
    "telnetlib",
}

FORBIDDEN_AUDIT_CALL_ATTRS = {
    "execute",
    "executemany",
    "commit",
    "rollback",
    "urlopen",
    "connect",
    "sendall",
    "post",
    "put",
}

RUNTIME_INJECTION_KEYWORDS = {
    "kline_loader",
    "news_scan_meta",
    "risk_version",
    "now_fn",
}


def _scan_meta(stale=False):
    return {
        "observed_at": "2026-09-11T10:00:00",
        "stale": stale,
        "error": None,
    }


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _string_constants(tree):
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _import_roots(tree):
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _module_attribute_calls(tree, attribute):
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "PDA"
        and node.func.attr == attribute
    ]


class CompatibilityFacadeSurfaceTests(unittest.TestCase):
    def test_legacy_private_names_still_exist(self):
        for name in (
            "DECISION_SNAPSHOT_VERSION",
            "_snapshot_safe",
            "_snapshot_date",
            "_snapshot_first",
            "_snapshot_kline",
            "_snapshot_factor_evidence",
            "_decision_snapshot",
            "_with_decision_snapshot",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(paper, name), f"missing facade symbol {name}")

    def test_pure_helpers_are_aliases_of_the_audit_module(self):
        self.assertIs(paper._snapshot_safe, audit.snapshot_safe)
        self.assertIs(paper._snapshot_date, audit._snapshot_date)
        self.assertIs(paper._snapshot_first, audit._snapshot_first)
        self.assertIs(paper._snapshot_kline, audit._snapshot_kline)
        self.assertIs(paper._snapshot_factor_evidence, audit._snapshot_factor_evidence)
        self.assertEqual(
            paper.DECISION_SNAPSHOT_VERSION, audit.DECISION_SNAPSHOT_VERSION
        )


class DecisionSnapshotFacadeDelegationTests(unittest.TestCase):
    def test_delegates_to_audit_module_with_live_runtime_dependencies(self):
        recorded = {}

        def fake(payload=None, **kwargs):
            recorded["payload"] = payload
            recorded.update(kwargs)
            return {"delegated": True}

        with mock.patch.object(
            audit, "build_decision_snapshot", side_effect=fake
        ) as patched:
            result = paper._decision_snapshot(
                {"pick": {"code": "000001"}}, account_id="tq_breakout"
            )

        self.assertEqual(patched.call_count, 1)
        self.assertEqual(result, {"delegated": True})
        self.assertEqual(recorded["payload"], {"pick": {"code": "000001"}})
        self.assertEqual(recorded["account_id"], "tq_breakout")
        self.assertIs(recorded["kline_loader"], paper._completed_kline)
        self.assertIs(recorded["news_scan_meta"], paper._NEWS_SCAN_META)
        self.assertEqual(recorded["risk_version"], paper.RISK_VERSION)
        self.assertIs(recorded["now_fn"], paper._now)

    def test_reads_patched_runtime_dependencies_instead_of_frozen_copies(self):
        recorded = {}

        def fake(payload=None, **kwargs):
            recorded["payload"] = payload
            recorded.update(kwargs)
            return {}

        patched_scan_meta = _scan_meta(stale=True)
        patched_now = "2031-02-03 04:05:06"

        def patched_loader(*_args, **_kwargs):
            raise AssertionError("loader must not be called by this test")

        with (
            mock.patch.object(paper, "_NEWS_SCAN_META", patched_scan_meta),
            mock.patch.object(paper, "_completed_kline", patched_loader),
            mock.patch.object(paper, "RISK_VERSION", "paper-risk-fixture"),
            mock.patch.object(paper, "_now", return_value=patched_now),
            mock.patch.object(audit, "build_decision_snapshot", side_effect=fake),
        ):
            paper._decision_snapshot({})

        self.assertIs(recorded["news_scan_meta"], patched_scan_meta)
        self.assertIs(recorded["kline_loader"], patched_loader)
        self.assertEqual(recorded["risk_version"], "paper-risk-fixture")
        self.assertEqual(recorded["now_fn"](), patched_now)

    def test_patched_kline_loader_is_actually_used(self):
        frame = _kline(source="facade-loader")
        calls = []

        def loader(code, asof_date, inclusive=True):
            calls.append((code, asof_date, inclusive))
            return frame

        with (
            mock.patch.object(paper, "_completed_kline", loader),
            mock.patch.object(paper, "_NEWS_SCAN_META", _scan_meta()),
        ):
            snapshot = paper._decision_snapshot(
                {"pick": {"code": "000001"}, "signal_date": "2026-09-11"},
                decision_at="2026-09-11 10:01:03",
            )

        self.assertEqual(calls, [("000001", "2026-09-11", True)])
        self.assertEqual(snapshot["kline"]["source"], "facade-loader")
        self.assertEqual(snapshot["kline"]["count"], 2)

    def test_patched_clock_and_scan_meta_flow_through_the_facade(self):
        with (
            mock.patch.object(paper, "_now", return_value="2031-02-03 04:05:06"),
            mock.patch.object(paper, "_NEWS_SCAN_META", _scan_meta(stale=True)),
        ):
            snapshot = paper._decision_snapshot({})

        self.assertEqual(snapshot["decision_at"], "2031-02-03 04:05:06")
        self.assertEqual(snapshot["data_quality"]["news"], "stale")
        self.assertTrue(snapshot["data_quality"]["news_scan"]["stale"])

    def test_facade_reproduces_the_frozen_golden_envelope(self):
        payload = _payload()
        with (
            mock.patch.object(paper, "_NEWS_SCAN_META", GOLDEN_SCAN_META),
            mock.patch.object(paper, "RISK_VERSION", "paper-risk-v4"),
        ):
            snapshot = paper._decision_snapshot(
                payload,
                account_id="tq_breakout",
                code="000001",
                side="buy",
                decision="approved_signal",
                reason="fixture passed",
                asof_date="2026-09-11",
                quote=_quote(),
                kline=_kline(),
                news=payload["news"],
                final_score=0.83,
                decision_at="2026-09-11 10:01:03",
            )
        self.assertEqual(snapshot, GOLDEN_ENVELOPE)

    def test_facade_does_not_swallow_unexpected_keyword_arguments(self):
        with self.assertRaises(TypeError):
            paper._decision_snapshot({}, unexpected_keyword=True)


class WithDecisionSnapshotFacadeDelegationTests(unittest.TestCase):
    def test_delegates_to_audit_module_with_live_runtime_dependencies(self):
        recorded = {}

        def fake(payload=None, **kwargs):
            recorded["payload"] = payload
            recorded.update(kwargs)
            return {"delegated": True}

        with mock.patch.object(
            audit, "with_decision_snapshot", side_effect=fake
        ) as patched:
            result = paper._with_decision_snapshot(
                {"side": "buy"}, account_id="tq_breakout", asof_date="2026-09-11"
            )

        self.assertEqual(patched.call_count, 1)
        self.assertEqual(result, {"delegated": True})
        self.assertEqual(recorded["payload"], {"side": "buy"})
        self.assertEqual(recorded["account_id"], "tq_breakout")
        self.assertEqual(recorded["asof_date"], "2026-09-11")
        self.assertIs(recorded["kline_loader"], paper._completed_kline)
        self.assertIs(recorded["news_scan_meta"], paper._NEWS_SCAN_META)
        self.assertEqual(recorded["risk_version"], paper.RISK_VERSION)
        self.assertIs(recorded["now_fn"], paper._now)

    def test_reads_patched_runtime_dependencies_instead_of_frozen_copies(self):
        recorded = {}

        def fake(payload=None, **kwargs):
            recorded["payload"] = payload
            recorded.update(kwargs)
            return {}

        patched_scan_meta = _scan_meta(stale=True)
        patched_loader = lambda *args, **kwargs: _kline()  # noqa: E731

        with (
            mock.patch.object(paper, "_NEWS_SCAN_META", patched_scan_meta),
            mock.patch.object(paper, "_completed_kline", patched_loader),
            mock.patch.object(paper, "RISK_VERSION", "paper-risk-fixture"),
            mock.patch.object(paper, "_now", return_value="2031-02-03 04:05:06"),
            mock.patch.object(audit, "with_decision_snapshot", side_effect=fake),
        ):
            paper._with_decision_snapshot({})

        self.assertIs(recorded["news_scan_meta"], patched_scan_meta)
        self.assertIs(recorded["kline_loader"], patched_loader)
        self.assertEqual(recorded["risk_version"], "paper-risk-fixture")
        self.assertEqual(recorded["now_fn"](), "2031-02-03 04:05:06")

    def test_preserves_caller_payload_and_enriches_strategy_id(self):
        payload = {
            "side": "buy",
            "decision_name": "fixture",
            "nested": {"keep": True},
        }
        original = copy.deepcopy(payload)
        with mock.patch.object(paper, "_NEWS_SCAN_META", _scan_meta()):
            enriched = paper._with_decision_snapshot(
                payload,
                account_id="tq_breakout",
                asof_date="2026-09-11",
                kline=_kline(),
                decision_at="2026-09-11 10:01:03",
            )
        self.assertEqual(payload, original)
        self.assertIsNot(enriched, payload)
        self.assertEqual(enriched["strategy_id"], "tq_breakout")
        self.assertEqual(enriched["nested"], {"keep": True})
        self.assertEqual(enriched["decision_snapshot"]["strategy_id"], "tq_breakout")
        self.assertNotIn("decision_snapshot", payload)

    def test_existing_strategy_id_is_preserved(self):
        with mock.patch.object(paper, "_NEWS_SCAN_META", _scan_meta()):
            enriched = paper._with_decision_snapshot(
                {"strategy_id": "explicit-strategy"},
                account_id="tq_breakout",
                decision_at="2026-09-11 10:01:03",
            )
        self.assertEqual(enriched["strategy_id"], "explicit-strategy")

    def test_patched_scan_meta_is_visible_in_the_attached_snapshot(self):
        with mock.patch.object(paper, "_NEWS_SCAN_META", _scan_meta(stale=True)):
            enriched = paper._with_decision_snapshot(
                {}, decision_at="2026-09-11 10:01:03"
            )
        self.assertEqual(enriched["decision_snapshot"]["data_quality"]["news"], "stale")
        self.assertEqual(
            enriched["decision_snapshot"]["data_quality"]["overall"], "degraded"
        )


class DecisionAuditArchitectureGuardTests(unittest.TestCase):
    """Source guards so a duplicated serializer cannot silently come back."""

    @classmethod
    def setUpClass(cls):
        cls.audit_tree = _parse(AUDIT_PATH)
        cls.paper_tree = _parse(PAPER_TRADING_PATH)

    def test_audit_module_never_imports_paper_trading(self):
        offenders = {
            root
            for root in _import_roots(self.audit_tree)
            if root == "paper_trading"
        }
        self.assertEqual(offenders, set())

    def test_audit_module_depends_only_on_stdlib_and_pandas(self):
        top_level = set()
        for node in self.audit_tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top_level.add(node.module.split(".")[0])
        self.assertLessEqual(top_level, {"__future__", "datetime", "pandas"})

    def test_audit_module_performs_no_database_or_network_io(self):
        roots = _import_roots(self.audit_tree)
        self.assertEqual(roots & FORBIDDEN_AUDIT_IMPORT_ROOTS, set())
        called_attrs = {
            node.func.attr
            for node in ast.walk(self.audit_tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertEqual(called_attrs & FORBIDDEN_AUDIT_CALL_ATTRS, set())

    def test_sentinel_guard_is_not_vacuous(self):
        # The sentinels must genuinely mark the serializer, otherwise the
        # paper_trading guard below would pass for the wrong reason.
        self.assertLessEqual(
            SERIALIZER_SENTINELS, _string_constants(self.audit_tree)
        )

    def test_paper_trading_holds_no_duplicated_serializer_implementation(self):
        constants = _string_constants(self.paper_tree)
        self.assertEqual(constants & SERIALIZER_SENTINELS, set())
        names = {
            node.id for node in ast.walk(self.paper_tree) if isinstance(node, ast.Name)
        }
        self.assertNotIn("max_stored_bars", names)
        # A renamed copy would still have to emit the contract keys, so scan
        # every function body for the serializer-only markers as well.
        for node in ast.walk(self.paper_tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body_source = ast.unparse(node)
                for marker in ("rows_stored", "omitted_rows", "future_excluded"):
                    self.assertNotIn(
                        marker,
                        body_source,
                        f"{node.name} re-implements the decision serializer",
                    )

    def test_paper_trading_delegates_the_serializer_exactly_once(self):
        build_calls = _module_attribute_calls(self.paper_tree, "build_decision_snapshot")
        self.assertEqual(len(build_calls), 1)
        build_keywords = {keyword.arg for keyword in build_calls[0].keywords}
        self.assertLessEqual(RUNTIME_INJECTION_KEYWORDS, build_keywords)

        with_calls = _module_attribute_calls(self.paper_tree, "with_decision_snapshot")
        self.assertEqual(len(with_calls), 1)
        with_keywords = {keyword.arg for keyword in with_calls[0].keywords}
        self.assertLessEqual(RUNTIME_INJECTION_KEYWORDS, with_keywords)

    def test_serializer_contract_keys_live_only_in_the_audit_module(self):
        """Single source of truth is a repo-wide claim, not just a
        ``paper_trading`` one.

        A copy of the serializer in any *other* backend module would satisfy
        every facade guard above while silently forking the algorithm, so scan
        all production modules for the envelope's contract keys.
        """
        offenders = {}
        for path in sorted(BACKEND.glob("*.py")):
            if path == AUDIT_PATH or path.name.startswith("test_"):
                continue
            found = _string_constants(_parse(path)) & SERIALIZER_SENTINELS
            if found:
                offenders[path.name] = sorted(found)
        self.assertEqual(offenders, {})

    def test_paper_trading_imports_the_audit_module_under_one_alias(self):
        imported = set()
        for node in ast.walk(self.paper_tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "paper_decision_audit":
                        imported.add(alias.asname or alias.name)
        self.assertEqual(imported, {"PDA"})


if __name__ == "__main__":
    unittest.main()
