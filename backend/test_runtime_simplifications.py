import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import data_fetcher as dfc
import news_runner
import paper_runner
import universe as U


class RuntimeSimplificationTests(unittest.TestCase):
    def test_runner_nonzero_statuses_are_visible_to_scheduler(self):
        self.assertEqual(paper_runner._result_exit_code({"status": "completed"}), 0)
        self.assertEqual(paper_runner._result_exit_code({"status": "skipped"}), 0)
        self.assertEqual(paper_runner._result_exit_code({"status": "partial"}), 1)
        self.assertEqual(paper_runner._result_exit_code({"status": "blocked"}), 1)
        self.assertEqual(news_runner._result_exit_code("completed"), 0)
        self.assertEqual(news_runner._result_exit_code("failed"), 1)

    def test_realtime_inputs_are_deduplicated_and_parser_is_shared(self):
        calls = []
        row = {
            "f12": "000001", "f14": "平安银行", "f2": 10.0, "f3": 1.2,
            "f5": 1, "f6": 2, "f8": 3, "f9": 4, "f10": 5,
            "f15": 11, "f16": 9, "f17": 9.5, "f18": 9.8,
            "f20": 100, "f21": 80, "f23": 1.2, "f62": 1,
            "f66": 2, "f72": 3, "f78": 4, "f84": 5,
            "f184": 0.1, "f100": "银行", "f124": 1787600000,
        }
        row2 = dict(row, f12="600000", f14="浦发银行")

        def fake_get_json(_url, params, **_kwargs):
            calls.append(params["secids"])
            return {"data": {"diff": [row, row2]}}

        with mock.patch.object(dfc, "_get_json", side_effect=fake_get_json), \
                mock.patch.object(dfc, "reset_data_source"), \
                mock.patch.object(dfc.time, "sleep"):
            result = dfc.fetch_realtime_for_codes(
                ["000001", "000001", "600000"], return_meta=True
            )
        self.assertEqual(result["expected"], 2)
        self.assertEqual(result["returned"], 2)
        self.assertTrue(result["complete"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], "0.000001,1.600000")
        self.assertEqual(dfc._realtime_row_from_ulist(row)["name"], "平安银行")

    def test_full_snapshot_refresh_is_singleflight_and_double_checks_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "snapshot.json"
            lock_path = Path(tmp) / "snapshot.lock"
            rows = [{"code": f"{i:06d}"} for i in range(4000)]
            calls = []

            def fake_fetch(*_args, **_kwargs):
                calls.append(time.monotonic())
                payload = {
                    "complete": True, "expected_rows": 4000, "rows": rows,
                }
                cache_path.write_text(json.dumps(payload), encoding="utf-8")
                return rows

            with mock.patch.object(dfc, "MARKET_SNAPSHOT_FULL_CACHE_PATH", str(cache_path)), \
                    mock.patch.object(dfc, "MARKET_SNAPSHOT_FULL_LOCK_PATH", str(lock_path)), \
                    mock.patch.object(dfc, "fetch_market_snapshot", side_effect=fake_fetch), \
                    mock.patch.object(dfc, "_mem_cache", {}):
                results = []
                threads = [
                    threading.Thread(
                        target=lambda: results.append(dfc.fetch_market_snapshot_full(max_age=60)),
                    )
                    for _ in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
                self.assertEqual(len(calls), 1)
                self.assertEqual([len(result) for result in results], [4000, 4000])

    def test_coverage_display_cache_does_not_change_formal_default(self):
        universe_rows = [{
            "code": "000001", "snapshot_tradable": True,
            "industry": "银行", "float_cap": 1,
        }]
        manifest = {
            "000001": {
                "rows": 120, "last_date": "2026-08-22",
                "source": "tencent", "adjustment": "qfq",
            },
        }
        calls = {"load": 0}

        def fake_load():
            calls["load"] += 1
            return universe_rows

        with mock.patch.object(U, "load_universe", side_effect=fake_load), \
                mock.patch.object(U.dfc, "get_kline_manifest", return_value=manifest), \
                mock.patch.object(U, "_universe_cache_signature", return_value=(1, 1)), \
                mock.patch.object(U, "_coverage_file_signature", return_value=(1, 1)), \
                mock.patch.object(U, "_previous_trade_weekday", return_value=__import__("datetime").date(2026, 8, 22)), \
                mock.patch.object(U, "_history_is_fresh", return_value=True), \
                mock.patch.object(U.os, "listdir", return_value=["000001.csv"]):
            U.invalidate_coverage_cache()
            cached_one = U.coverage_report(cache_ttl=10)
            cached_two = U.coverage_report(cache_ttl=10)
            formal = U.coverage_report(cache_ttl=0)
        self.assertEqual(calls["load"], 2)
        self.assertEqual(cached_one, cached_two)
        self.assertEqual(cached_one, formal)


if __name__ == "__main__":
    unittest.main()
