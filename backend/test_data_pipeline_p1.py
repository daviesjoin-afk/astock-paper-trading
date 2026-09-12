"""Offline pipeline regression tests (issue #29: collected by unittest).

These were previously pytest-style top-level functions, which
``unittest discover`` silently skipped - including inside the CI
``--network none`` offline gate.  They are unittest cases now so the
offline gate actually exercises them.
"""

import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

import data_fetcher as dfc
import paper_trading as paper
import selection_runner


class DataPipelineP1Tests(unittest.TestCase):
    def _patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fetch_clist_reports_missing_page(self):
        self._patch(dfc, "CLIST_HOSTS", ["test-host"])

        def fake_get_json(_url, params, **_kwargs):
            page = int(params["pn"])
            if page == 1:
                return {"data": {"total": 6, "diff": [{"f12": "000001"}, {"f12": "000002"}]}}
            if page == 2:
                return {"data": {"total": 6, "diff": [{"f12": "000003"}, {"f12": "000004"}]}}
            return {"data": {"total": 6, "diff": []}}

        self._patch(dfc, "_get_json", fake_get_json)
        result = dfc._fetch_clist("f12", pages=None, pz=2, return_meta=True)
        self.assertEqual(result["pages_expected"], 3)
        self.assertEqual(result["pages_ok"], 2)
        self.assertEqual(result["failed_pages"], [3])
        self.assertIs(result["complete"], False)

    def test_full_market_snapshot_fails_closed_on_partial_pages(self):
        dfc._mem_cache.clear()
        self._patch(
            dfc,
            "_fetch_clist",
            lambda *args, **kwargs: {
                "rows": [{"f12": str(i), "f2": 1.0, "f3": 0.0} for i in range(4500)],
                "total": 5000,
                "pages_expected": 25,
                "pages_ok": 24,
                "failed_pages": [25],
                "complete": False,
            },
        )
        self.assertEqual(dfc.fetch_market_snapshot(pages=None, allow_disk_fallback=False), [])
        self.assertIs(dfc._full_snapshot_payload_is_complete({"rows": ["legacy"] * 5000}), False)

    def test_full_market_snapshot_contract_accepts_complete_coverage(self):
        rows = [{"code": f"{i:06d}"} for i in range(4)]
        payload = {
            "complete": True,
            "expected_rows": 4,
            "pages_expected": 1,
            "pages_ok": 1,
            "rows": rows,
        }
        with mock.patch.dict(os.environ, {"ASTOCK_FULL_MARKET_MIN_ROWS": "3"}, clear=False):
            self.assertTrue(dfc._full_snapshot_payload_is_complete(payload))

    def test_full_market_snapshot_contract_rejects_row_or_unique_code_shortfall(self):
        with mock.patch.dict(os.environ, {"ASTOCK_FULL_MARKET_MIN_ROWS": "3"}, clear=False):
            too_few_rows = {
                "complete": True,
                "expected_rows": 4,
                "pages_expected": 1,
                "pages_ok": 1,
                "rows": [{"code": "000001"}, {"code": "000002"}],
            }
            duplicate_heavy = {
                "complete": True,
                "expected_rows": 4,
                "pages_expected": 1,
                "pages_ok": 1,
                "rows": [
                    {"code": "000001"},
                    {"code": "000001"},
                    {"code": "000002"},
                    {"code": "000002"},
                ],
            }
            self.assertFalse(dfc._full_snapshot_payload_is_complete(too_few_rows))
            self.assertFalse(dfc._full_snapshot_payload_is_complete(duplicate_heavy))

    def test_full_market_disk_cache_accepts_fresh_content_timestamp(self):
        tmp = tempfile.TemporaryDirectory(prefix="astock-market-cache-")
        self.addCleanup(tmp.cleanup)
        cache_path = os.path.join(tmp.name, "market_snapshot_full.json")
        now = dt.datetime.now(dt.timezone.utc)
        rows = [
            {"code": "000001", "quote_at": now.isoformat()},
            {"code": "000002", "quote_at": now.isoformat()},
        ]
        payload = {
            "complete": True,
            "expected_rows": 2,
            "pages_expected": 1,
            "pages_ok": 1,
            "rows": rows,
        }
        with open(cache_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        self._patch(dfc, "MARKET_SNAPSHOT_FULL_CACHE_PATH", cache_path)
        with mock.patch.dict(os.environ, {"ASTOCK_FULL_MARKET_MIN_ROWS": "2"}, clear=False):
            self.assertEqual(dfc._fresh_full_snapshot_from_disk(60), rows)

    def test_full_market_disk_cache_rejects_stale_content_timestamp(self):
        tmp = tempfile.TemporaryDirectory(prefix="astock-market-cache-")
        self.addCleanup(tmp.cleanup)
        cache_path = os.path.join(tmp.name, "market_snapshot_full.json")
        stale_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        rows = [
            {"code": "000001", "quote_at": stale_at.isoformat()},
            {"code": "000002", "quote_at": stale_at.isoformat()},
        ]
        payload = {
            "complete": True,
            "expected_rows": 2,
            "pages_expected": 1,
            "pages_ok": 1,
            "rows": rows,
        }
        with open(cache_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        self._patch(dfc, "MARKET_SNAPSHOT_FULL_CACHE_PATH", cache_path)
        with mock.patch.dict(os.environ, {"ASTOCK_FULL_MARKET_MIN_ROWS": "2"}, clear=False):
            self.assertIsNone(dfc._fresh_full_snapshot_from_disk(60))

    def _quote_cross_check(self, *, cross_price, cross_pct):
        code = "000001"
        quote_at = "2026-09-03T10:00:00+08:00"
        self._patch(
            paper.dfc,
            "fetch_realtime_for_codes",
            lambda _codes: [{
                "code": code,
                "price": 10.0,
                "pct": 1.0,
                "quote_at": quote_at,
                "super_net": 1.0,
                "main_pct": 0.1,
            }],
        )
        self._patch(
            paper.dfc,
            "fetch_independent_realtime_for_codes",
            lambda _codes: [{
                "code": code,
                "price": cross_price,
                "pct": cross_pct,
                "quote_at": quote_at,
                "source": "test-independent-source",
            }],
        )
        self._patch(paper, "_latest_price_map", lambda _codes: {})
        self._patch(paper, "_universe_snapshot_time", lambda: None)
        return paper._quotes([code], asof_date=dt.date(2026, 9, 3))[code]

    def test_quote_cross_check_accepts_values_within_tolerance(self):
        row = self._quote_cross_check(cross_price=10.04, cross_pct=1.15)
        self.assertEqual(row["quote_validation"], "cross_source_checked")
        self.assertTrue(row["quote_cross_check"]["passed"])

    def test_quote_cross_check_rejects_price_gap_over_tolerance(self):
        row = self._quote_cross_check(cross_price=10.10, cross_pct=1.0)
        self.assertEqual(row["quote_validation"], "cross_source_failed")
        self.assertFalse(row["quote_cross_check"]["passed"])
        self.assertIn("双源差异超阈值", row["quote_cross_check"]["failure_reason"])

    def test_quote_cross_check_rejects_pct_gap_over_tolerance(self):
        row = self._quote_cross_check(cross_price=10.0, cross_pct=1.25)
        self.assertEqual(row["quote_validation"], "cross_source_failed")
        self.assertFalse(row["quote_cross_check"]["passed"])
        self.assertIn("双源差异超阈值", row["quote_cross_check"]["failure_reason"])

    def test_flow_map_fails_closed_and_exposes_coverage(self):
        self._patch(
            dfc,
            "_fetch_clist",
            lambda *args, **kwargs: {
                "rows": [{"f12": str(i), "f66": 1.0} for i in range(5000)],
                "total": 5500,
                "pages_expected": 28,
                "pages_ok": 27,
                "failed_pages": [28],
                "complete": False,
            },
        )
        self.assertEqual(dfc._fetch_all_flow_map(), {})
        state = dfc.get_flow_fetch_state()
        self.assertIs(state["complete"], False)
        self.assertIs(state["coverage_ok"], False)
        self.assertLess(state["coverage_pct"], 100.0)

    def test_kline_save_uses_atomic_manifest_and_persists_entry(self):
        tmp = tempfile.TemporaryDirectory(prefix="astock-kline-")
        self.addCleanup(tmp.cleanup)
        kline_dir = f"{tmp.name}/klines"
        os.makedirs(kline_dir)
        manifest_path = f"{tmp.name}/kline_manifest.json"
        self._patch(dfc, "KLINE_DIR", kline_dir)
        self._patch(dfc, "KLINE_MANIFEST_PATH", manifest_path)
        self._patch(dfc, "_manifest", None)
        self._patch(dfc, "_manifest_mtime", None)
        self._patch(dfc, "_manifest_dirty", 0)
        self._patch(dfc, "_manifest_pending", {})
        frame = pd.DataFrame(
            [{"open": 1, "close": 1.1, "high": 1.2, "low": 0.9, "volume": 10, "amount": 11}],
            index=pd.to_datetime(["2026-08-24"]),
        )
        frame.attrs.update({"source": "test", "adjustment": "qfq"})
        dfc.save_kline("000001", frame)
        dfc.flush_kline_manifest()
        with open(manifest_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["stocks"]["000001"]["last_date"], "2026-08-24")
        self.assertEqual(list(__import__("pathlib").Path(kline_dir).glob("*.tmp")), [])
        self.assertTrue(os.path.exists(f"{kline_dir}/000001.csv"))

    def test_selection_runner_marks_partial_when_one_strategy_fails(self):
        self._patch(selection_runner.ST, "ensure_schema", lambda: None)
        self._patch(selection_runner.ST, "update_observations", lambda: {"updated": 0})
        self._patch(selection_runner.M, "_complete_daily_cutoff", lambda: dt.date(2026, 8, 20))
        self._patch(selection_runner.M.U, "refresh_history", lambda **_kwargs: {"status": "up_to_date"})
        self._patch(selection_runner.P, "_rebuild_selection_factor_cache", lambda _target: {"status": "ok"})
        self._patch(selection_runner.S, "STRATEGIES", ["one", "two"])
        self._patch(
            selection_runner.M,
            "_select_uncached",
            lambda strategy, topn: {"strategy": strategy} if strategy == "one" else {"error": "provider"},
        )
        self._patch(selection_runner.ST, "record_run", lambda result, **_kwargs: result)
        result = selection_runner._run_daily(
            now=dt.datetime(2026, 8, 24, 16, 0, 0),
            admission={"allowed": True},
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["strategy"] for item in result["failures"]], ["two"])


if __name__ == "__main__":
    unittest.main()
