#!/usr/bin/env python3
"""Offline tests for the six-task daily runner.

No model is called. What is tested is the part that decides *whether* a task
runs, because that is where a fabricated input would enter: a task given an
empty `flows` dict will happily produce a confident, meaningless reading.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_sources"))

import llm_daily  # noqa: E402
import llm_tasks  # noqa: E402

DAY = "2026-11-14"
TODAY_REPORT = {
    "day": DAY,
    "trades": [{"symbol": "600000.SH", "name": "浦发银行", "side": "buy",
                "quantity": 1000, "price_cents": 1020, "reason": "score",
                "basis": {"score": 55}}],
    "blocked": [{"symbol": "000001.SZ", "name": "平安银行", "side": "sell",
                 "quantity": 500, "reason": "halted", "blocked_day": DAY}],
    "activity": {"return_bps": -120, "fills": 1},
}
NOW = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)  # 14:00 Shanghai

# The adapter's real envelope: rows, data, top-level and nested provenance.
FLOW_RESULT = {
    "status": "ok",
    "capability": "stock_fund_flow_120d",
    "rows": [
        {"date": DAY, "main_net": 1000.0, "small_net": 100.0,
         "mid_net": 200.0, "large_net": 300.0, "super_net": 400.0},
        # A future row must never be used as evidence for a past decision.
        {"date": "2026-11-15", "main_net": 9999.0, "small_net": 1.0,
         "mid_net": 2.0, "large_net": 3.0, "super_net": 4.0},
    ],
    "data": {},
    "source": "eastmoney",
    "temporal": "recent_window",
    "provenance": {"source": "eastmoney", "temporal": "recent_window",
                   "source_urls": ["https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"],
                   "historical_first_seen_verified": False,
                   "fetched_at": "2026-11-14T14:00:00+08:00"},
    "fetched_at": "2026-11-14T14:00:00+08:00",
    "availability": "retrieved_version",
    "historical_first_seen_verified": False,
}
NEWS_RESULT = {
    "status": "ok",
    "capability": "eastmoney_stock_news",
    "rows": [
        {"title": "回购公告", "content": "公司拟回购股份", "time": "2026-11-14 10:00:00",
         "source": "东方财富", "url": "https://example.invalid/news/1"},
        # A publication after the cutoff must be excluded, not summarized.
        {"title": "明天的公告", "content": "尚未发布", "time": "2026-11-14 23:00:00",
         "source": "东方财富"},
    ],
    "data": {},
    "source": "eastmoney",
    "temporal": "current_snapshot",
    "provenance": {"source": "eastmoney", "temporal": "current_snapshot",
                   "historical_first_seen_verified": False,
                   "fetched_at": "2026-11-14T14:00:00+08:00"},
    "fetched_at": "2026-11-14T14:00:00+08:00",
    "availability": "retrieved_version",
}


class FakeClient:
    """Minimal injectable shared client; records every call and failure."""

    def __init__(self, *, fail=(), temporal=None):
        self.calls = []
        self.fail = set(fail)
        self.temporal = temporal

    def datasets(self):
        entries = [
            {"name": "eastmoney_fund_flow_minute", "parameters": ["code"],
             "temporal": "current_snapshot"},
            {"name": "stock_fund_flow_120d", "parameters": ["code"],
             "temporal": "recent_window"},
            {"name": "eastmoney_stock_news", "parameters": ["code", "page_size"],
             "temporal": "current_snapshot"},
            {"name": "announcements", "parameters": ["symbol", "start_day",
                                                      "end_day", "page_size"],
             "temporal": "dated_history"},
        ]
        if self.temporal:
            for entry in entries:
                entry["temporal"] = self.temporal
        return entries

    def dataset(self, name, as_of=None, **params):
        self.calls.append((name, dict(params, **({"as_of": as_of} if as_of else {}))))
        if name in self.fail:
            raise OSError(f"network unreachable: {name}")
        if name in ("eastmoney_fund_flow_minute", "stock_fund_flow_120d"):
            return dict(FLOW_RESULT, capability=name, provenance=dict(
                FLOW_RESULT["provenance"], temporal=self.temporal or "recent_window"))
        return dict(NEWS_RESULT, capability=name, provenance=dict(
            NEWS_RESULT["provenance"], temporal=self.temporal or "current_snapshot"))


REPORT = {
    "day": "2026-11-10",
    "trades": [
        {"symbol": "600000.SH", "side": "buy", "quantity": 1000,
         "price_cents": 1020, "reason": "score", "basis": {"score": 55}},
    ],
    "blocked": [
        {"symbol": "000001.SZ", "side": "sell", "quantity": 500,
         "reason": "halted", "blocked_day": "2026-11-10"},
    ],
    "activity": {"return_bps": -120, "fills": 1},
}


class TestSignalPayloads(unittest.TestCase):
    def test_refusals_are_reviewed_like_fills(self):
        # A blocked order is a decision. Reviewing only the fills would skip
        # exactly the cases where the system declined to act.
        payloads = list(llm_daily._signal_payloads(REPORT))
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["symbol"], "600000.SH")
        self.assertEqual(payloads[1]["symbol"], "000001.SZ")
        self.assertTrue(payloads[1]["basis"]["blocked"])

    def test_capital_limits_the_number_reviewed(self):
        self.assertEqual(len(llm_daily.signal_payloads(REPORT, 1)), 1)
        self.assertEqual(len(llm_daily.signal_payloads(REPORT, 99)), 2)

    def test_a_payload_carries_every_required_input(self):
        required = llm_tasks.TASKS["signal_review"].required_inputs
        for payload in llm_daily._signal_payloads(REPORT):
            for key in required:
                self.assertIn(key, payload, f"missing {key}")


class TestOptionalPayloads(unittest.TestCase):
    def test_pnl_is_built_from_real_activity(self):
        payload = llm_daily.pnl_payload(REPORT)
        self.assertEqual(payload["period"], "2026-11-10")
        self.assertEqual(payload["total_bps"], -120)

    def test_pnl_is_absent_when_there_was_no_activity(self):
        # Not a zero: zero would be a claim that the day broke even.
        self.assertIsNone(llm_daily.pnl_payload({"day": "x"}))

    def test_evolution_needs_parameters(self):
        self.assertIsNone(llm_daily.evolution_payload(REPORT, {}))

    def test_evolution_is_built_when_parameters_exist(self):
        payload = llm_daily.evolution_payload(REPORT, {"entry_score": 40})
        self.assertEqual(payload["parameters"]["entry_score"], 40)
        self.assertEqual(payload["performance"]["fills"], 1)
        self.assertEqual(payload["performance"]["blocked"], 1)


class TestSkipReasons(unittest.TestCase):
    """The four tasks that may not run must explain themselves."""

    def run_without_model(self):
        with mock.patch.dict("os.environ", {"ASTOCK_LLM_BASE": "http://example.com", "ASTOCK_LLM_KEY": "k"}), \
             mock.patch.object(llm_daily, "get", return_value=REPORT), \
             mock.patch.object(llm_daily.llm_runner, "run",
                               return_value={"status": "ok"}), \
             mock.patch.object(sys, "argv", [
                 "llm_daily", "--day", "2026-11-10", "--out", "unused",
                 "--max-calls", "10"]):
            import io
            from contextlib import redirect_stdout
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                llm_daily.main([])
            return json.loads(buffer.getvalue())

    def test_all_six_tasks_are_accounted_for(self):
        # signal_review may appear once per decision (fill or refusal), so the
        # check is on the set of task names, not on the count of records.
        with mock.patch.dict("os.environ", {"ASTOCK_LLM_BASE": "http://example.com", "ASTOCK_LLM_KEY": "k"}), \
             mock.patch.object(llm_daily, "get", return_value=REPORT), \
             mock.patch.object(llm_daily.llm_runner, "run", return_value={"status": "ok"}):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                llm_daily.main(["--day", "2026-11-10", "--out", str(out), "--max-calls", "10"])
                written = json.loads((out / "2026-11-10.llm.json").read_text(encoding="utf-8"))
        tasks = {item["task"] for item in written["records"]}
        self.assertEqual(tasks, set(llm_tasks.TASKS.keys()),
                         "every task name must appear at least once")

    def test_flow_reading_is_skipped_not_invented(self):
        with mock.patch.dict("os.environ", {"ASTOCK_LLM_BASE": "http://example.com", "ASTOCK_LLM_KEY": "k"}), \
             mock.patch.object(llm_daily, "get", return_value=REPORT), \
             mock.patch.object(llm_daily.llm_runner, "run", return_value={"status": "ok"}):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                llm_daily.main(["--day", "2026-11-10", "--out", str(out), "--max-calls", "10"])
                written = json.loads((out / "2026-11-10.llm.json").read_text(encoding="utf-8"))
        flow = [item for item in written["records"] if item["task"] == "flow_reading"][0]
        self.assertEqual(flow["status"], "skipped")
        self.assertEqual(flow["reason"], "data_unavailable")
        self.assertTrue(flow["missing"], "the skipped task must name what is missing")

    def test_news_is_skipped_without_materials(self):
        with mock.patch.dict("os.environ", {"ASTOCK_LLM_BASE": "http://example.com", "ASTOCK_LLM_KEY": "k"}), \
             mock.patch.object(llm_daily, "get", return_value=REPORT), \
             mock.patch.object(llm_daily.llm_runner, "run", return_value={"status": "ok"}):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                llm_daily.main(["--day", "2026-11-10", "--out", str(out), "--max-calls", "10"])
                written = json.loads((out / "2026-11-10.llm.json").read_text(encoding="utf-8"))
        news = [item for item in written["records"] if item["task"] == "news_impact"][0]
        self.assertEqual(news["status"], "skipped")
        self.assertEqual(news["reason"], "materials_not_supplied")

    def test_budget_stops_further_calls(self):
        with mock.patch.dict("os.environ", {"ASTOCK_LLM_BASE": "http://example.com", "ASTOCK_LLM_KEY": "k"}), \
             mock.patch.object(llm_daily, "get", return_value=REPORT), \
             mock.patch.object(llm_daily.llm_runner, "run", return_value={"status": "ok"}):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                llm_daily.main(["--day", "2026-11-10", "--out", str(out), "--max-calls", "1"])
                written = json.loads((out / "2026-11-10.llm.json").read_text(encoding="utf-8"))
        self.assertEqual(written["calls"], 1)
        self.assertTrue(any(item.get("status") == "skipped_budget"
                            for item in written["records"]))

    def test_no_configuration_is_a_clean_skip(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            import io
            from contextlib import redirect_stdout
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = llm_daily.main(["--day", "2026-11-10", "--out", "unused"])
        self.assertEqual(code, 0)
        self.assertIn("no_llm_configuration", buffer.getvalue())


class TestPublicDataPlanning(unittest.TestCase):
    def test_auto_enables_current_data_for_todays_report(self):
        plan = llm_daily.plan_public_data(
            TODAY_REPORT, requested="auto", now=NOW,
            catalog=llm_daily.catalog_index(FakeClient().datasets()))
        self.assertEqual(plan["strategy"], "current")
        self.assertEqual(plan["symbols"], ["600000.SH", "000001.SZ"])
        self.assertEqual(len(plan["queries"]), 4,
                         "one flow and one news dataset per selected symbol")
        self.assertTrue(all(q["params"].get("code") for q in plan["queries"]))
        self.assertTrue(all("as_of" not in q["params"] for q in plan["queries"]),
                        "as_of is a client-level keyword, not a capability param")
        self.assertIsNone(plan["client_as_of"],
                          "live reports must not request a frozen historical snapshot")

    def test_auto_disables_current_data_for_a_past_report(self):
        old = dict(TODAY_REPORT, day="2026-11-10")
        plan = llm_daily.plan_public_data(old, requested="auto", now=NOW)
        self.assertEqual(plan["strategy"], "off")
        self.assertEqual(plan["queries"], [])

    def test_current_strategy_refuses_a_past_report(self):
        plan = llm_daily.plan_public_data(dict(TODAY_REPORT, day="2026-11-10"),
                                          requested="current", now=NOW)
        self.assertEqual(plan["strategy"], "refused_current_for_historical")
        self.assertEqual(plan["queries"], [])

    def test_historical_uses_dated_datasets_and_strict_as_of(self):
        old = dict(TODAY_REPORT, day="2026-11-10")
        client = FakeClient()
        plan = llm_daily.plan_public_data(
            old, requested="historical", now=NOW,
            catalog=llm_daily.catalog_index(client.datasets()))
        self.assertEqual(plan["strategy"], "historical")
        names = {q["dataset"] for q in plan["queries"]}
        self.assertNotIn("eastmoney_fund_flow_minute", names)
        self.assertNotIn("eastmoney_stock_news", names)
        self.assertTrue(names <= {"stock_fund_flow_120d", "announcements",
                                  "cninfo_announcements", "fund_flow_120d",
                                  "capital.fund_flow_120d",
                                  "fundamentals.cninfo_announcements"})
        self.assertEqual(plan["client_as_of"], plan["cutoff"],
                         "strict frozen-only as_of is a client-level keyword")
        self.assertTrue(all("as_of" not in q["params"] for q in plan["queries"]))

    def test_symbol_and_call_budgets_are_bounded(self):
        report = dict(TODAY_REPORT, symbols=[f"60000{i}.SH" for i in range(20)])
        plan = llm_daily.plan_public_data(
            report, requested="current", now=NOW, max_symbols=2, max_calls=2,
            catalog=llm_daily.catalog_index(FakeClient().datasets()))
        self.assertEqual(len(plan["symbols"]), 2)
        self.assertEqual(len(plan["queries"]), 2)
        self.assertTrue(all(q["params"].get("code") for q in plan["queries"]))


class TestPublicDataBuilders(unittest.TestCase):
    def test_flow_payload_keeps_raw_units_provenance_and_derived_semantics(self):
        built = llm_daily.build_flow_payload(
            "600000.SH", "stock_fund_flow_120d", FLOW_RESULT, day=DAY,
            strategy="current", cutoff=llm_daily.parse_cutoff("2026-11-14T14:00:00+08:00"))
        self.assertNotIn("skip", built)
        flows = built["payload"]["flows"]
        self.assertEqual(flows["row_count"], 1, "the future row is excluded")
        self.assertEqual(flows["super_large_net_inflow_cents"], 40000)
        self.assertEqual(flows["retail_net_inflow_cents"], 10000)
        row = flows["rows"][0]
        self.assertEqual(row["raw"]["super_large_net_inflow_cents"]["unit"], "yuan")
        self.assertEqual(row["semantics"]["is_raw_level2_tape"], False)
        self.assertFalse(row["semantics"]["substituted_from_volume_or_price"])
        self.assertEqual(row["provenance"]["source"], "eastmoney")
        self.assertEqual(row["provenance"]["temporal"], "recent_window")
        self.assertEqual(flows["cutoff"], "2026-11-14T14:00:00+08:00")
        self.assertEqual(flows["excluded"]["after_report_day"], 1)

    def test_a_missing_flow_bucket_is_a_skip_not_a_zero(self):
        incomplete = dict(FLOW_RESULT, rows=[{"date": DAY, "main_net": 1.0,
                                              "small_net": 1.0}])
        built = llm_daily.build_flow_payload(
            "600000.SH", "stock_fund_flow_120d", incomplete, day=DAY,
            strategy="current", cutoff=NOW)
        self.assertIn("skip", built)
        self.assertEqual(built["skip"]["excluded"]["missing_buckets"], 1)

    def test_a_current_snapshot_cannot_answer_a_historical_request(self):
        built = llm_daily.build_flow_payload(
            "600000.SH", "stock_fund_flow_120d", FLOW_RESULT, day=DAY,
            strategy="historical", cutoff=NOW)
        # A dated series is allowed, but a current-snapshot envelope is not.
        snapshot = dict(FLOW_RESULT, temporal="current_snapshot")
        refused = llm_daily.build_flow_payload(
            "600000.SH", "stock_fund_flow_120d", snapshot, day=DAY,
            strategy="historical", cutoff=NOW)
        self.assertNotIn("skip", built)
        self.assertEqual(refused["skip"]["code"], "snapshot_not_historical")

    def test_news_payload_labels_raw_vs_derived_and_filters_by_cutoff(self):
        built = llm_daily.build_news_payload(
            "600000.SH", "eastmoney_stock_news", NEWS_RESULT, day=DAY,
            strategy="current",
            cutoff=llm_daily.parse_cutoff("2026-11-14T14:00:00+08:00"))
        materials = built["payload"]["materials"]
        self.assertEqual(len(materials), 1, "a 23:00 article is after the 14:00 cutoff")
        item = materials[0]
        self.assertEqual(item["semantics"]["kind"], "derived_excerpt")
        self.assertEqual(item["source"], "东方财富")
        self.assertEqual(item["provenance"]["source"], "eastmoney")
        self.assertEqual(item["publication_precision"], "timestamp")

    def test_a_date_precision_announcement_never_invents_a_publish_time(self):
        result = {
            "status": "ok", "capability": "announcements",
            "rows": [{"title": "董事会决议公告", "announcement_date": DAY,
                      "published_at": None, "publication_precision": "date",
                      "source": "cninfo", "source_url": "https://example.invalid/a"}],
            "provenance": {"source": "cninfo", "temporal": "dated_history"},
            "temporal": "dated_history", "fetched_at": "2026-11-14T14:00:00+08:00",
        }
        built = llm_daily.build_news_payload(
            "000001.SZ", "announcements", result, day=DAY, strategy="historical",
            cutoff=NOW)
        item = built["payload"]["materials"][0]
        self.assertEqual(item["semantics"]["kind"], "raw_source_text")
        self.assertIsNone(item["published_at"])
        self.assertEqual(item["publication_precision"], "date")
        self.assertEqual(item["provenance"]["historical_first_seen_verified"], None)

    def test_an_error_envelope_is_a_skip(self):
        built = llm_daily.build_news_payload(
            "600000.SH", "eastmoney_stock_news",
            {"status": "error", "code": "source_empty_unverified"},
            day=DAY, strategy="current", cutoff=NOW)
        self.assertEqual(built["skip"]["code"], "dataset_error")


class TestPublicDataWiring(unittest.TestCase):
    """Behavioural tests of the real main() path, with an injected client."""

    def run_daily(self, report=TODAY_REPORT, client=None, now=NOW, args=()):
        with mock.patch.dict("os.environ", {"ASTOCK_LLM_BASE": "http://example.com",
                                            "ASTOCK_LLM_KEY": "k",
                                            "ASTOCK_PUBLIC_DATA": "auto"}), \
             mock.patch.object(llm_daily, "get", return_value=report), \
             mock.patch.object(llm_daily.llm_runner, "run",
                               return_value={"status": "ok"}):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                llm_daily.main(["--day", report.get("day", DAY),
                                "--out", str(out), *args], client=client, now=now)
                written = json.loads((out / f"{report.get('day', DAY)}.llm.json")
                                     .read_text(encoding="utf-8"))
        return written

    def test_todays_report_runs_flow_and_news_with_configured_model(self):
        client = FakeClient()
        written = self.run_daily(client=client)
        records = {item["task"]: item for item in written["records"]}
        self.assertEqual(records["flow_reading"]["status"], "ok")
        self.assertEqual(records["news_impact"]["status"], "ok")
        self.assertEqual(written["public_data"]["strategy"], "current")
        self.assertEqual(len(written["public_data"]["calls"]), 4)
        self.assertEqual(written["calls"], 6,
                         "two signal reviews + pnl + evolution + flow + news + hypothesis")
        self.assertTrue(any(name in ("stock_fund_flow_120d",
                                     "eastmoney_fund_flow_minute")
                            for name, _ in client.calls))

    def test_network_error_skips_only_the_affected_task(self):
        client = FakeClient(fail=("eastmoney_fund_flow_minute",))
        written = self.run_daily(client=client)
        records = {item["task"]: item for item in written["records"]}
        self.assertEqual(records["flow_reading"]["status"], "skipped")
        self.assertEqual(records["flow_reading"]["reason"], "data_unavailable")
        self.assertEqual(records["news_impact"]["status"], "ok")
        self.assertEqual(written["public_data"]["strategy"], "current")

    def test_a_past_report_does_not_query_todays_snapshot_by_default(self):
        client = FakeClient()
        old = dict(TODAY_REPORT, day="2026-11-10")
        written = self.run_daily(report=old, client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(written["public_data"]["strategy"], "off")

    def test_historical_strategy_queries_dated_data_for_a_past_report(self):
        client = FakeClient()
        old = dict(TODAY_REPORT, day="2026-11-10")
        written = self.run_daily(report=old, client=client,
                                 args=("--public-data", "historical"))
        self.assertEqual(written["public_data"]["strategy"], "historical")
        self.assertTrue(client.calls)
        self.assertTrue(all("as_of" in params for _, params in client.calls))
        dated = [params for name, params in client.calls
                 if name == "announcements"]
        self.assertTrue(dated, "the dated CNINFO route should be used")
        self.assertTrue(all(q.get("start_day") and q.get("end_day") for q in dated),
                        "dated announcements must be bounded by trading dates")

    def test_off_strategy_preserves_original_skip_fields(self):
        client = FakeClient()
        written = self.run_daily(client=client, args=("--public-data", "off"))
        self.assertEqual(client.calls, [])
        records = {item["task"]: item for item in written["records"]}
        self.assertEqual(records["flow_reading"]["reason"], "data_unavailable")
        self.assertEqual(records["flow_reading"]["missing"],
                         list(llm_tasks.TASKS["flow_reading"].required_fields))
        self.assertEqual(records["news_impact"]["reason"], "materials_not_supplied")



if __name__ == "__main__":
    unittest.main(verbosity=2)
