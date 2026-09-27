# -*- coding: utf-8 -*-
"""Regressions for owner list APIs that must scan past future revisions."""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import os
import datetime as dt
import json
BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_engine as AE
import ai_analysis
import api_adaptive
import deepseek_research as DR
import adaptive_risk as AR
import adaptive_selection as ASEL
import learning_evaluation as LE
import paper_trading as PT


class _PagedConnection:
    def __init__(self, keys):
        self.keys = keys
        self.offsets = []

    def execute(self, _sql, params):
        size, offset = params
        self.offsets.append(offset)
        return SimpleNamespace(fetchall=lambda: [(key,) for key in self.keys[offset:offset + size]])


def _projection(identity):
    return SimpleNamespace(availability_day="2026-09-20", revision_identity=str(identity))


class OwnerAsOfPaginationTests(unittest.TestCase):
    def _assert_scans_past_newer_future_rows(self, function, reader_name, keys):
        conn = _PagedConnection(keys)

        def read(_conn, key, *, as_of):
            self.assertEqual("2026-09-20", as_of)
            return None if key != keys[-1] else _projection(key)

        with mock.patch.object(sys.modules[function.__module__], reader_name, side_effect=read):
            facts = function(conn, as_of="2026-09-20", limit=1)
        self.assertEqual([_projection(keys[-1]).revision_identity], [fact.revision_identity for fact in facts])
        self.assertGreater(len(conn.offsets), 1)

    def test_risk_candidate_facts_scans_older_visible_fact(self):
        self._assert_scans_past_newer_future_rows(AR.risk_candidate_facts, "risk_candidate_fact", [5, 4, 3, 2, 1])

    def test_selection_candidate_facts_scans_older_visible_fact(self):
        self._assert_scans_past_newer_future_rows(ASEL.selection_candidate_facts, "selection_candidate_fact", [5, 4, 3, 2, 1])

    def test_experiment_evaluation_facts_scans_older_visible_fact(self):
        self._assert_scans_past_newer_future_rows(LE.experiment_evaluation_facts, "experiment_evaluation_fact", ["f5", "f4", "f3", "f2", "f1"])

    def test_adaptive_run_facts_scans_older_visible_fact(self):
        self._assert_scans_past_newer_future_rows(AE.adaptive_run_facts, "adaptive_run_fact", [5, 4, 3, 2, 1])

    def test_paper_job_run_facts_scans_older_visible_fact(self):
        self._assert_scans_past_newer_future_rows(PT.paper_job_run_facts, "paper_job_run_fact", ["run-5", "run-4", "run-3", "run-2", "run-1"])


class _InsertCapture:
    def __init__(self):
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        if "INSERT INTO adaptive_runs" in sql:
            self.rows.append(params)


class MiddayResearchResultTests(unittest.TestCase):
    def test_missing_scheduled_context_never_records_candidate_challenge_completed(self):
        conn = _InsertCapture()
        with mock.patch.object(AE, "_connect", return_value=conn), \
             mock.patch.object(AE, "overview", return_value={"ok": True}), \
             mock.patch.object(AE, "_config", return_value={}), \
             mock.patch.object(AE.deepseek_research, "run_task") as run_task:
            AE.run_midday_advisor()
        run_task.assert_not_called()
        self.assertEqual(1, len(conn.rows))
        detail = json.loads(conn.rows[0][4])
        self.assertEqual("advisor_failed", conn.rows[0][1])
        self.assertNotEqual("completed", detail.get("candidate_challenge"))

    def test_failed_candidate_challenge_is_recorded_as_failed_with_error_code(self):
        conn = _InsertCapture()
        market_now = dt.datetime(2026, 9, 27, 12, 0, tzinfo=AE.TZ)
        with mock.patch.object(AE, "_connect", return_value=conn), \
             mock.patch.object(AE, "overview", return_value={"ok": True}), \
             mock.patch.object(AE, "_config", return_value={"llm_realtime_tuning_enabled": False}), \
             mock.patch.object(AE.deepseek_advisor, "enabled", return_value=True), \
             mock.patch.object(AE.deepseek_advisor, "configured", return_value=True), \
             mock.patch.object(AE.deepseek_advisor, "run_review", return_value={"status": "completed"}), \
             mock.patch.object(AE.deepseek_research, "run_task", return_value={
                 "status": "failed", "error_code": "research_asof_context_required",
             }) as run_task:
            AE.run_midday_advisor(market_now=market_now)
        self.assertEqual("2026-09-27", run_task.call_args.kwargs["context"].asof_day)
        self.assertEqual(1, len(conn.rows))
        detail = json.loads(conn.rows[0][4])
        self.assertEqual("advisor_failed", conn.rows[0][1])
        self.assertEqual({"status": "failed", "error_code": "research_asof_context_required"},
                         detail["candidate_challenge"])
        self.assertNotEqual("completed", detail["reason"])


class AnalysisTargetIdentityTests(unittest.TestCase):
    def test_business_key_separates_account_and_cycle_and_is_order_stable(self):
        key_a, _ = ai_analysis._analysis_business_key(
            "2026-09-27", "manual", "all", (("account-A", 1),),
        )
        key_b, _ = ai_analysis._analysis_business_key(
            "2026-09-27", "manual", "all", (("account-B", 2),),
        )
        key_next_cycle, _ = ai_analysis._analysis_business_key(
            "2026-09-27", "manual", "all", (("account-A", 2),),
        )
        same_a, _ = ai_analysis._analysis_business_key(
            "2026-09-27", "manual", "all", (("account-A", 1),),
        )
        ordered, _ = ai_analysis._analysis_business_key(
            "2026-09-27", "manual", "all", (("b", 2), ("a", 1)),
        )
        reversed_order, _ = ai_analysis._analysis_business_key(
            "2026-09-27", "manual", "all", (("a", 1), ("b", 2)),
        )
        self.assertNotEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_next_cycle)
        self.assertEqual(key_a, same_a)
        self.assertEqual(ordered, reversed_order)

    def test_no_target_context_has_stable_explicit_identity(self):
        key, identity = ai_analysis._analysis_business_key("2026-09-27", "manual", "market", ())
        self.assertEqual("none", identity)
        self.assertTrue(key.endswith(":targets:none"))


class ManualResearchResultTests(unittest.TestCase):
    def test_manual_research_returns_canonical_task_result(self):
        conn = _InsertCapture()
        canonical = {"purpose": "candidate_challenge", "status": "failed", "error_code": "no_evidence"}
        with mock.patch.object(AE, "_connect", return_value=conn), \
             mock.patch.object(AE, "_config", return_value={}), \
             mock.patch.object(AE, "overview", return_value={"engine": {"stage": "ready"}}), \
             mock.patch.object(AE.deepseek_research, "run_task", return_value=canonical) as run_task:
            result = AE.run_advisor_review(
                purpose="candidate_challenge", asof_day="2026-09-27",
                market_now="2026-09-27T12:00:00+08:00",
            )
        self.assertEqual(canonical, result["research_task_result"])
        self.assertEqual("2026-09-27", run_task.call_args.kwargs["context"].asof_day)

    def test_single_pnl_requires_explicit_account_and_cycle(self):
        conn = _InsertCapture()
        with mock.patch.object(AE, "_connect", return_value=conn), \
             mock.patch.object(AE, "_config", return_value={}), \
             mock.patch.object(AE.deepseek_research, "run_task") as run_task:
            with self.assertRaisesRegex(ValueError, "attribution_context_required"):
                AE.run_advisor_review(
                    purpose="pnl_attribution", asof_day="2026-09-27",
                    market_now="2026-09-27T12:00:00+08:00",
                )
        run_task.assert_not_called()

    def test_suite_returns_explicit_pnl_unavailable_result(self):
        conn = _InsertCapture()
        suite_results = [
            {"purpose": "pnl_attribution", "status": "failed", "error_code": "attribution_context_required"},
        ]
        with mock.patch.object(AE, "_connect", return_value=conn), \
             mock.patch.object(AE, "_config", return_value={}), \
             mock.patch.object(AE, "overview", return_value={"engine": {"stage": "ready"}}), \
             mock.patch.object(AE.deepseek_advisor, "enabled", return_value=True), \
             mock.patch.object(AE.deepseek_advisor, "run_review", return_value={"status": "completed"}), \
             mock.patch.object(AE.deepseek_research, "run_suite", return_value=suite_results):
            result = AE.run_advisor_suite(
                asof_day="2026-09-27", market_now="2026-09-27T12:00:00+08:00",
            )
        self.assertEqual(suite_results, result["research_suite_results"])

    def test_canonical_suite_marks_missing_pnl_targets_unavailable(self):
        context = DR.ResearchAsOfContext(
            asof_day="2026-09-27",
            market_now=dt.datetime(2026, 9, 27, 12, 0, tzinfo=AE.TZ),
            targets=(),
        )
        evidence = {purpose: [] for purpose in DR.TASKS}
        evidence["pnl_attribution"] = [ValueError("attribution_context_required")]
        with mock.patch.object(DR, "_collect_suite_snapshot", return_value=("snapshot-1", context.asof_day, evidence)), \
             mock.patch.object(DR, "_run_evidence_task", return_value={"status": "completed"}):
            results = DR.run_suite(mock.Mock(), "paper.sqlite3", context=context)
        pnl = next(item for item in results if item["purpose"] == "pnl_attribution")
        self.assertEqual("failed", pnl["status"])
        self.assertEqual("attribution_context_required", pnl["error_code"])

    def test_advisor_api_forwards_explicit_account_and_cycle(self):
        response = {"research_task_result": {"purpose": "pnl_attribution", "status": "completed"}}
        with mock.patch.object(api_adaptive.adaptive, "run_advisor_review", return_value=response) as run:
            actual = api_adaptive.run_advisor(
                purpose="pnl_attribution", as_of="2026-09-27",
                market_now="2026-09-27T12:00:00+08:00", account_id="account-A",
                cycle_id=12, confirmed=True,
            )
        self.assertEqual(response, actual)
        self.assertEqual("account-A", run.call_args.kwargs["account_id"])
        self.assertEqual(12, run.call_args.kwargs["cycle_id"])


if __name__ == "__main__":
    unittest.main()
