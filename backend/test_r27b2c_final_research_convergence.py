# -*- coding: utf-8 -*-
"""R27-B2C-FINAL permanent architecture regression guards."""
from __future__ import annotations

import ast
import os
import re
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BACKEND)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def _function(tree: ast.Module, name: str):
    return next((node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name == name), None)


def _calls(node: ast.AST) -> set[str]:
    result = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            result.add(func.id if isinstance(func, ast.Name) else
                       func.attr if isinstance(func, ast.Attribute) else "")
    return result


def _string_constants(node: ast.AST) -> list[str]:
    return [child.value for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)]


def _imported_roots(tree: ast.Module) -> set[str]:
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _js_function(source: str, name: str) -> str:
    match = re.search(rf"export\s+(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    if not match:
        return ""
    end = source.find("\nexport ", match.end())
    return source[match.start():] if end < 0 else source[match.start():end]


def architecture_violations(sources: dict[str, str]) -> list[str]:
    """Return semantic boundary violations for a virtual source tree."""
    dr_name = "backend/deepseek_research.py"
    aa_name = "backend/ai_analysis.py"
    dr = ast.parse(sources[dr_name])
    aa = ast.parse(sources[aa_name])
    issues: list[str] = []

    # Review regressions: scheduled context/result propagation, identity, owner paging,
    # explicit portfolio parameters, and exchange-local canonical dates.
    ae_name = "backend/adaptive_engine.py"
    runner_name = "backend/adaptive_runner.py"
    frontend_name = "frontend/src/features/adaptive.js"
    api_name = "backend/api_adaptive.py"
    ae = ast.parse(sources.get(ae_name, ""))
    runner = ast.parse(sources.get(runner_name, ""))
    frontend = sources.get(frontend_name, "")
    midday = _function(ae, "run_midday_advisor")
    midday_source = ast.unparse(midday) if midday else ""
    if (not midday or "context=research_context" not in midday_source
            or "challenge_status" not in midday_source
            or 'detail["candidate_challenge"] = "completed"' in midday_source):
        issues.append("FINAL-G19: scheduled candidate challenge can lose context or mask failure")
    runner_source = ast.unparse(runner)
    if "market_now=dt.datetime.now(adaptive.TZ)" not in runner_source:
        issues.append("FINAL-G19: scheduler does not supply an explicit Asia/Shanghai instant")
    run_analysis_source = ast.unparse(_function(aa, "run_analysis") or aa)
    identity_helper = _function(aa, "_analysis_business_key")
    identity_source = ast.unparse(identity_helper) if identity_helper else ""
    identity_return = next((node for node in ast.walk(identity_helper or aa)
                            if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)), None)
    if ("_analysis_business_key" not in run_analysis_source
            or "target_identity" not in identity_source
            or identity_return is None or "target_identity" not in ast.unparse(identity_return.value.elts[0])):
        issues.append("FINAL-G20: ai_analysis idempotency key omits portfolio identity")

    list_specs = {
        "backend/adaptive_risk.py": ("risk_candidate_facts", "risk_candidate_fact"),
        "backend/adaptive_selection.py": ("selection_candidate_facts", "selection_candidate_fact"),
        "backend/learning_evaluation.py": ("experiment_evaluation_facts", "experiment_evaluation_fact"),
        "backend/adaptive_engine.py": ("adaptive_run_facts", "adaptive_run_fact"),
        "backend/paper_trading.py": ("paper_job_run_facts", "paper_job_run_fact"),
    }
    for filename, (function_name, fact_name) in list_specs.items():
        tree = ast.parse(sources.get(filename, ""))
        node = _function(tree, function_name)
        has_offset_query = any("OFFSET ?" in value.upper() for value in _string_constants(node or tree))
        if not has_offset_query or fact_name not in _calls(node or tree):
            issues.append(f"FINAL-G21: {function_name} no longer pages through owner facts")

    manual = _function(ae, "run_advisor_review")
    suite = _function(ae, "run_advisor_suite")
    manual_source = ast.unparse(manual) if manual else ""
    suite_source = ast.unparse(suite) if suite else ""
    if "research_task_result" not in manual_source or "account_id" not in manual_source or "cycle_id" not in manual_source:
        issues.append("FINAL-G22: manual P&L result or portfolio context is discarded")
    if "research_suite_results" not in suite_source:
        issues.append("FINAL-G22: suite result statuses are discarded")
    run_action_source = _js_function(frontend, "runAdaptiveResearchTask")
    suite_action_source = _js_function(frontend, "runAdaptiveResearchSuite")
    analyze_action_source = _js_function(frontend, "retryAdaptiveAiWindow")
    if ("&account_id=" not in run_action_source or "&cycle_id=" not in run_action_source
            or "adaptiveResearchContext" not in run_action_source
            or "&account_id=" not in suite_action_source or "&cycle_id=" not in suite_action_source
            or "research_suite_results" not in frontend):
        issues.append("FINAL-G22: manual P&L action omits account/cycle or visible result statuses")
    if ("adaptiveShanghaiDate" not in frontend or "adaptiveLocalDate()" in run_action_source
            or "adaptiveLocalDate()" in suite_action_source
            or "adaptiveResearchAsOf" not in analyze_action_source
            or "researchContext.asOf" not in run_action_source
            or "researchContext.asOf" not in suite_action_source
            or "Asia/Shanghai" not in _js_function(frontend, "adaptiveShanghaiDate")):
        issues.append("FINAL-G23: canonical research as_of can use browser-local date")

    if "account_id: str | None = Query" not in sources.get(api_name, "") or "cycle_id: int | None = Query" not in sources.get(api_name, ""):
        issues.append("FINAL-G22: advisor API no longer accepts explicit portfolio context")

    collector_names = ("_candidate_evidence", "_incident_evidence", "_overfit_evidence",
                       "_event_evidence", "_collect_typed_events")
    collector_nodes = [_function(dr, name) for name in collector_names]
    collector_nodes = [node for node in collector_nodes if node is not None]
    sql = "\n".join(value for node in collector_nodes for value in _string_constants(node))
    forbidden_tables = (
        "adaptive_runs", "paper_jobs", "paper_orders", "adaptive_selection_candidates",
        "adaptive_risk_candidates", "adaptive_rewards", "paper_nav", "paper_positions",
        "news_events", "market_major_events", "news_source_reputation",
        "news_factor_versions", "market_event_candidate_links",
    )
    if re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE|FROM|JOIN)\b", sql, re.I):
        if any(re.search(rf"\b{re.escape(table)}\b", sql, re.I) for table in forbidden_tables):
            issues.append("FINAL-G01: migrated research collector contains legacy evidence SQL")

    event = _function(dr, "_event_evidence")
    if event and ({"fetch_company_announcements", "fetch_fast_news"} & _calls(event)):
        issues.append("FINAL-G02: event evidence has a remote fallback")
    if event and "data_fetcher" in _imported_roots(dr):
        issues.append("FINAL-G02: event evidence imports the fetch module")
    if event and any(isinstance(child, ast.Attribute) and child.attr == "published_at"
                     for child in ast.walk(event)):
        issues.append("FINAL-G04: event PIT is derived from publication time")
    incident = _function(dr, "_incident_evidence")
    if incident and "paper_orders" in " ".join(_string_constants(incident)):
        issues.append("FINAL-G03: incident evidence reads business order lifecycle")
    if incident and any(isinstance(child, ast.Attribute) and child.attr == "status"
                        for child in ast.walk(incident)):
        issues.append("FINAL-G17: incident research interprets runtime lifecycle status")

    for name in ("collect", "run_task", "run_suite", "_collect_suite_snapshot",
                 "_collect_typed_events", "_candidate_evidence", "_incident_evidence",
                 "_overfit_evidence", "_event_evidence"):
        node = _function(dr, name)
        if node is None:
            continue
        calls = _calls(node)
        if calls & {"today", "now"}:
            issues.append("FINAL-G04/G10/G11: research evidence derives time from current clock")
            break
        names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
        if names & {"latest_row", "current_row", "latest_runtime_row", "current_runtime_row"}:
            issues.append("FINAL-G03/G04: runtime evidence uses current/latest row fallback")
            break
    suite = _function(dr, "_collect_suite_snapshot")
    if suite and "ResearchAsOfContext" not in ast.unparse(suite):
        issues.append("FINAL-G10/G11: suite does not require the shared explicit context")
    if not all(name in ast.unparse(_function(dr, "_collect_typed_events") or dr)
               for name in ("adaptive_run_facts", "paper_job_run_facts",
                            "risk_candidate_facts", "selection_candidate_facts",
                            "experiment_evaluation_facts", "news_fact_projections")):
        issues.append("FINAL-G05/G06/G07: a migrated owner reader is no longer wired")
    if not all(name in ast.unparse(_function(dr, "_collect_typed_events") or dr)
               for name in ("ai_research_news_adapter", "ai_research_strategy_adapter",
                            "ai_research_runtime_adapter")):
        issues.append("FINAL-G05/G06/G07: a required owner adapter is no longer used")

    if _function(dr, "_save_run") is not None or "_save_run" in _calls(dr):
        issues.append("FINAL-G10: legacy research writer is present")
    production = "\n".join(
        text for name, text in sources.items()
        if name.startswith("backend/") and name.endswith(".py") and not os.path.basename(name).startswith("test_")
    )
    if re.search(r"\bINSERT\s+INTO\s+adaptive_advisor_runs\b", production, re.I):
        issues.append("FINAL-G10: canonical result is dual-written to the legacy ledger")

    aa_literals = "\n".join(_string_constants(aa))
    if re.search(r"\b(?:FROM|JOIN)\s+paper_(?:positions|orders)\b", aa_literals, re.I):
        issues.append("FINAL-G09: ai_analysis reads raw positions/orders as evidence")
    aa_calls = _calls(aa)
    if "run_research" in aa_calls or "ai_research_provider" in _imported_roots(aa):
        issues.append("FINAL-G10: ai_analysis bypasses canonical research service")
    run_analysis = _function(aa, "run_analysis")
    if run_analysis and "run_research_run" not in _calls(run_analysis):
        issues.append("FINAL-G10: ai_analysis does not use canonical research lifecycle")
    compatibility = _function(dr, "_compatibility_evidence")
    if compatibility and "research_composition_only_not_an_owner" not in _string_constants(compatibility):
        issues.append("FINAL-G11: compatibility projection claims or omits its non-owner authority")

    # Unknown must stay explicit; no empty-object/zero substitute for a missing prior result.
    latest_quality = _function(dr, "_latest_data_quality")
    if latest_quality and any(
        (isinstance(child, ast.Dict) and not child.keys)
        or (isinstance(child, ast.List) and not child.elts)
        or (isinstance(child, ast.Constant) and not isinstance(child.value, bool)
            and child.value == 0)
        for child in ast.walk(latest_quality)
    ):
        issues.append("FINAL-G11: unavailable research context is represented as empty/zero")
    fact_payload = _function(dr, "_fact_payload")
    payload_source = ast.unparse(fact_payload) if fact_payload else ""
    if "is_verified" not in payload_source or "outcome" not in payload_source:
        issues.append("FINAL-G11/G18: owner verification fields can leak into compatibility payload")
    if "verified" in " ".join(_string_constants(incident or dr)):
        issues.append("FINAL-G17: incident research interprets runtime status as verification")
    snapshot = _function(aa, "deterministic_snapshot")
    if snapshot and re.search(r"\brows\s*=\s*0\s+if\s+.*snapshot\s+is\s+None", ast.unparse(snapshot)):
        issues.append("FINAL-G11: missing market owner rows are coerced to zero")
    return issues


def _load_sources() -> dict[str, str]:
    sources = {}
    names = (
        "deepseek_research.py", "ai_analysis.py", "adaptive_engine.py", "adaptive_runner.py",
        "adaptive_risk.py", "adaptive_selection.py", "learning_evaluation.py",
        "paper_trading.py", "api_adaptive.py",
    )
    for name in names:
        with open(os.path.join(BACKEND, name), encoding="utf-8") as stream:
            sources[f"backend/{name}"] = stream.read()
    frontend_path = os.path.join(ROOT, "frontend", "src", "features", "adaptive.js")
    with open(frontend_path, encoding="utf-8") as stream:
        sources["frontend/src/features/adaptive.js"] = stream.read()
    return sources


class FinalResearchConvergenceGuardTests(unittest.TestCase):
    def test_FINAL_G01_through_G11_baseline_is_green(self):
        self.assertEqual([], architecture_violations(_load_sources()))

    def test_news_strategy_runtime_adapters_have_real_production_callers(self):
        source = _load_sources()["backend/deepseek_research.py"]
        self.assertIn("ai_research_news_adapter", source)
        self.assertIn("ai_research_strategy_adapter", source)
        self.assertIn("ai_research_runtime_adapter", source)

    def test_FINAL_G08_approved_issuer_set_remains_exact(self):
        import test_ai_research_evidence_ownership_guard as ownership_guard
        self.assertEqual(ownership_guard.APPROVED_ISSUER_CALLERS,
                         ownership_guard._issuer_caller_set())

    def test_missing_previous_research_is_explicitly_unavailable(self):
        import deepseek_research as DR
        class _Advisor:
            @staticmethod
            def latest_data_quality_research(_conn):
                return None
        original = DR.advisor
        try:
            DR.advisor = _Advisor()
            result = DR._latest_data_quality(object())
        finally:
            DR.advisor = original
        self.assertEqual("unavailable", result["availability"])
        self.assertEqual("canonical_data_quality_research_unavailable", result["reason"])
        self.assertNotIn("evidence", result)

    def test_ai_analysis_refuses_implicit_context_before_opening_database(self):
        import ai_analysis
        from unittest import mock
        factory = mock.Mock()
        with self.assertRaisesRegex(ValueError, "research_asof_context_required"):
            ai_analysis.run_analysis(factory, "paper.sqlite3", (), context=None)
        with self.assertRaisesRegex(ValueError, "research_asof_context_required"):
            ai_analysis.deterministic_snapshot("paper.sqlite3", (), context=None)
        factory.assert_not_called()

    def test_manual_research_entrypoints_require_declared_context(self):
        import adaptive_engine as AE
        from unittest import mock
        with mock.patch.object(AE, "_connect") as connect:
            with self.assertRaisesRegex(ValueError, "research_asof_context_required"):
                AE.run_advisor_review(purpose="incident_triage")
            with self.assertRaisesRegex(ValueError, "research_asof_context_required"):
                AE.run_advisor_suite()
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
