"""Permanent dependency and authority guards for R27-B3 read surfaces."""
from __future__ import annotations

import inspect
import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BACKEND)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_analysis  # noqa: E402
import api_adaptive  # noqa: E402
import deepseek_advisor  # noqa: E402


def _read(relative_path):
    with open(os.path.join(ROOT, relative_path), encoding="utf-8") as handle:
        return handle.read()


class CanonicalResearchAPIGuards(unittest.TestCase):
    def test_B3_G01_G02_endpoints_delegate_reads_to_repository(self):
        list_source = inspect.getsource(api_adaptive.canonical_research_runs)
        detail_source = inspect.getsource(api_adaptive.canonical_research_run)
        self.assertIn("repository.recent_runs", list_source)
        self.assertIn("repository.get_run", detail_source)
        for source in (list_source, detail_source):
            self.assertNotRegex(source, r"\b(?:SELECT|INSERT|UPDATE|DELETE|REPLACE|UPSERT)\b")

    def test_B3_G03_G04_get_path_is_query_only_and_has_no_execution_calls(self):
        connection_source = inspect.getsource(api_adaptive._canonical_research_connection)
        self.assertIn("repository.ensure_schema", connection_source)
        self.assertIn("PRAGMA query_only=ON", connection_source)
        self.assertNotIn("adaptive._connect", connection_source)
        for function in (api_adaptive.canonical_research_runs, api_adaptive.canonical_research_run):
            source = inspect.getsource(function)
            for forbidden in ("run_research", "run_review", "run_suite", "provider", "urlopen", "requests"):
                self.assertNotIn(forbidden, source)

    def test_B3_G05_G06_legacy_projection_and_reader_are_removed(self):
        self.assertFalse(hasattr(deepseek_advisor, "_canonical_research_display"))
        advisor_source = _read("backend/deepseek_advisor.py")
        self.assertNotRegex(
            advisor_source,
            r"(?is)\b(?:SELECT|INSERT\s+INTO|CREATE\s+TABLE|UPDATE|DELETE\s+FROM)\b[^;\n]{0,180}adaptive_advisor_runs",
        )
        overview = inspect.getsource(deepseek_advisor.overview)
        self.assertNotIn("latest_by_purpose", overview)

    def test_B3_G07_frontend_research_history_uses_only_canonical_api(self):
        frontend = _read("frontend/src/features/adaptive.js")
        self.assertIn("/api/adaptive/research/runs?limit=50", frontend)
        self.assertIn("/api/adaptive/research/runs/", frontend)
        self.assertNotIn("latest_by_purpose", frontend)
        self.assertNotIn("deepseek.latest", frontend)
        self.assertNotIn("legacy_adaptive_advisor_runs", frontend)
        self.assertIn("await refreshAdaptiveResearchHistory()", frontend)

    def test_B3_G08_G09_timeline_never_embeds_or_promotes_conclusions(self):
        source = inspect.getsource(ai_analysis.timeline)
        self.assertIn('item["canonical_run_id"]', source)
        self.assertIn('item["result"] = None', source)
        self.assertNotIn("canonical_research", source)
        self.assertNotIn("repository.get_run", source)
        self.assertIn("legacy_compatibility_history", source)

    def test_B3_G10_G11_G12_ui_renderers_keep_evidence_semantics(self):
        frontend = _read("frontend/src/features/adaptive.js")
        status_renderer = frontend.split("export function adaptiveResearchStatusLabel(")[1].split("\n}")[0]
        evidence_renderer = frontend.split("export function adaptiveResearchEvidenceHtml(")[1].split("\n}")[0]
        confidence_renderer = frontend.split("export function adaptiveResearchConfidencePercent(")[1].split("\n}")[0]
        self.assertNotIn("is_authoritative", status_renderer)
        self.assertIn("ref.cross_source_verified===true", evidence_renderer)
        self.assertNotRegex(evidence_renderer, r"verification\s*===?\s*['\"]verified")
        self.assertIn("number*100", confidence_renderer)

    def test_B3_G13_list_limit_is_bounded_in_fastapi_contract(self):
        parameter = inspect.signature(api_adaptive.canonical_research_runs).parameters["limit"].default
        constraints = {type(item).__name__: item for item in parameter.metadata}
        self.assertEqual(1, constraints["Ge"].ge)
        self.assertEqual(200, constraints["Le"].le)

    def test_B3_G14_G15_corruption_and_fresh_db_are_behaviors_not_source_claims(self):
        test_source = _read("backend/test_r27b3_canonical_research_api.py")
        self.assertIn("test_B3_API_01_fresh_database_returns_empty_and_ensures_schema", test_source)
        self.assertIn("test_B3_API_05_corrupt_rows_fail_closed_with_stable_reason", test_source)
        self.assertIn("test_B3_API_06_repository_read_connection_is_query_only", test_source)


if __name__ == "__main__":
    unittest.main()
