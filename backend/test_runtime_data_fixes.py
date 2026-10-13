import copy
import datetime as dt
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import pandas as pd
import universe as U
import risk_center as RC
import paper_trading as PT
import quote_probe as QP


def bars(start, periods):
    return pd.DataFrame({"close": range(1, periods + 1)}, index=pd.date_range(start, periods=periods, freq="B"))


class BenchmarkHistoryTests(unittest.TestCase):
    def refresh(self, cached, fetched, target):
        with patch.object(U.dfc, "load_cached_kline", return_value=cached), \
                patch.object(U.dfc, "fetch_kline", return_value=fetched) as fetch, \
                patch.object(U.dfc, "save_kline") as save:
            result = U.refresh_benchmark_history(target)
            return result, fetch, save

    def test_incremental_response_preserves_ma20_history(self):
        cached = bars("2026-09-01", 25)
        fetched = bars("2026-10-06", 1)
        result, _, save = self.refresh(cached, fetched, "2026-10-06")
        self.assertEqual(result, {"status": "updated", "rows": 26})
        self.assertEqual(len(save.call_args.args[1]), 26)

    def test_same_date_single_row_cache_is_repaired_not_skipped(self):
        fetched = bars("2026-09-01", 25)
        target = fetched.index[-1].date()
        result, fetch, save = self.refresh(fetched.tail(1), fetched, target)
        self.assertEqual(result["rows"], 25)
        self.assertEqual(fetch.call_args.kwargs["beg"], (target - dt.timedelta(days=120)).strftime("%Y%m%d"))
        self.assertEqual(len(save.call_args.args[1]), 25)

    def test_short_or_stale_response_does_not_overwrite(self):
        for fetched in (bars("2026-10-06", 1), bars("2026-09-01", 1), None):
            result, _, save = self.refresh(bars("2026-10-05", 1), fetched, "2026-10-06")
            self.assertEqual(result["status"], "failed")
            save.assert_not_called()

    def test_overlap_replaces_same_date_and_preserves_source(self):
        cached = bars("2026-09-01", 25)
        fetched = bars("2026-10-05", 2)
        fetched.attrs["source"] = "tencent"
        _, _, save = self.refresh(cached, fetched, "2026-10-06")
        merged = save.call_args.args[1]
        self.assertEqual(len(merged), 26)
        self.assertEqual(merged.loc["2026-10-05", "close"], 1)
        self.assertEqual(merged.attrs["source"], "tencent")


class CoverageTests(unittest.TestCase):
    def test_coverage_reuses_one_reference_date_and_recalculates_next_day(self):
        class Clock(dt.date):
            current = dt.date(2026, 10, 12)

            @classmethod
            def today(cls):
                return cls.current

        manifest = {"000001": {"rows": 150, "last_date": "2026-10-09"},
                    "000002": {"rows": 150, "last_date": "2026-10-12"},
                    "000003": {"rows": 150, "last_date": "invalid"}}
        rows = [{"code": code, "snapshot_tradable": True} for code in manifest]
        with patch.object(U, "load_universe", return_value=rows), \
                patch.object(U.dfc, "get_kline_manifest", return_value=manifest), \
                patch.object(U.os, "listdir", return_value=[code + ".csv" for code in manifest]), \
                patch.object(U.dt, "date", Clock), \
                patch.object(U, "_previous_trade_weekday", side_effect=[
                    dt.date(2026, 10, 9), dt.date(2026, 10, 12)]) as previous:
            first = U.coverage_report()
            self.assertEqual(previous.call_count, 1)
            Clock.current = dt.date(2026, 10, 13)
            second = U.coverage_report()
            self.assertEqual(previous.call_count, 2)
        self.assertEqual(first["fresh"], 2)
        self.assertEqual(second["fresh"], 1)
        self.assertEqual(first["expected_reference_date"], "2026-10-09")
        self.assertEqual(second["expected_reference_date"], "2026-10-12")
        self.assertEqual(first["missing_codes"], second["missing_codes"])

    def test_suspended_and_orphan_files_do_not_inflate_required_coverage(self):
        rows = [{"code": code, "snapshot_tradable": True} for code in ("000001", "000002")]
        manifest = {"000001": {"rows": 150, "last_date": "2026-10-09"},
                    "000002": {"rows": 150, "last_date": "2026-09-01"}}
        with patch.object(U, "load_universe", return_value=rows), \
                patch.object(U.dfc, "get_kline_manifest", return_value=manifest), \
                patch.object(U.os, "listdir", return_value=["000001.csv", "000002.csv", "000003.csv"]), \
                patch.object(U, "_previous_trade_weekday", return_value=dt.date(2026, 10, 9)), \
                patch.object(U, "_history_is_fresh", side_effect=lambda last, *args, **kwargs: last == "2026-10-09"):
            result = U.coverage_report()
        self.assertEqual(result["history_required"], 1)
        self.assertEqual(result["covered"], 1)
        self.assertEqual(result["long_suspended"], 1)
        self.assertEqual(result["orphan_cache_files"], 1)
        for key in ("coverage_pct", "usable_selection_pct", "usable_backtest_pct"):
            self.assertEqual(result[key], 100)
        self.assertEqual(result["usable_selection"], 1)


class SnapshotTests(unittest.TestCase):
    def test_unknown_snapshot_time_is_not_presented_as_fresh(self):
        self.assertTrue(RC.project_snapshot_freshness({"asof": "invalid"})["snapshot_stale"])
        self.assertFalse(RC.project_snapshot_freshness({"initializing": True})["snapshot_stale"])

    def test_read_time_freshness_does_not_mutate_cached_snapshot(self):
        now = dt.datetime(2026, 10, 12, 11, 20, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        observed = now - dt.timedelta(seconds=345)
        row = RC.source_health("index", "tencent", observed.isoformat(), 300, observed.isoformat())
        payload = {"asof": observed.isoformat(), "snapshot_ttl_seconds": 300, "snapshot_stale": False,
                   "data_quality": [row, {"status": "failed"}, {"status": "refreshing"}]}
        original = copy.deepcopy(payload)
        result = RC.project_snapshot_freshness(payload, now=now)
        self.assertTrue(result["snapshot_stale"])
        self.assertEqual(result["snapshot_age_seconds"], 345)
        self.assertEqual(result["data_quality"][0]["status"], "stale")
        self.assertEqual(result["data_quality"][0]["age_seconds"], 345)
        self.assertEqual(result["data_quality"][1]["status"], "failed")
        self.assertEqual(payload, original)

    def test_scheduled_and_existing_refresh_wait_before_runner_exit(self):
        for status in ("scheduled", "already_running"):
            with patch.object(PT, "request_risk_snapshot_refresh", return_value={"status": status}), \
                    patch.object(PT, "wait_risk_snapshot_refresh", return_value={"status": "timeout_after_240s"}) as wait:
                detail = PT._refresh_slot_risk_snapshot("risk")
            wait.assert_called_once_with(timeout=240.0)
            self.assertEqual(PT._slot_completion_status(detail), "failed")
        self.assertEqual(PT._slot_completion_status({"risk_snapshot_refresh": "completed"}), "completed")

    def test_already_finished_worker_failure_is_not_idle_success(self):
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        with patch.dict(PT._RISK_SNAPSHOT_REFRESH, thread=thread, running=False, last_error="RuntimeError: failed"):
            self.assertEqual(PT.wait_risk_snapshot_refresh()["status"], "failed")
        with patch.dict(PT._RISK_SNAPSHOT_REFRESH, thread=thread, running=False, last_error="cache_invalidation: warning"):
            self.assertEqual(PT.wait_risk_snapshot_refresh()["status"], "completed")


class QuoteProbeTests(unittest.TestCase):
    def test_missing_codes_and_raw_times_are_visible_without_execution_approval(self):
        with patch.object(QP, "make_feed") as factory:
            factory.return_value.fetch_realtime.return_value = [{"code": "600887", "price": 20, "quote_at": "20261012113000"}]
            result = QP.probe(["600887", "002980", "600887"], "tencent", include_quotes=True)
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["missing_codes"], ["002980"])
        self.assertEqual(result["quote_times"], ["20261012113000"])
        self.assertNotIn("cross_source_verified", result["quotes"][0])

    def test_invalid_or_unbounded_codes_rejected(self):
        for values in ([], ["600887;evil"], [f"{i:06d}" for i in range(201)]):
            with self.assertRaises(ValueError):
                QP.parse_codes(values)


if __name__ == "__main__":
    unittest.main()
