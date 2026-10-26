# -*- coding: utf-8 -*-
"""竞价因子影子评估的契约测试：不猜、不填零、不自动启用。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import auction_factor_shadow as SHADOW
import auction_tracking as A


class SpearmanTests(unittest.TestCase):
    def test_monotonic_and_reversed_relationships(self):
        self.assertAlmostEqual(SHADOW.spearman([(1, 10), (2, 20), (3, 30)]), 1.0, places=9)
        self.assertAlmostEqual(SHADOW.spearman([(1, 30), (2, 20), (3, 10)]), -1.0, places=9)

    def test_ties_and_degenerate_samples_return_none(self):
        self.assertIsNone(SHADOW.spearman([(1, 1), (2, 1), (3, 1)]))
        self.assertIsNone(SHADOW.spearman([(1, 2), (None, 3), (2, None)]))
        self.assertIsNone(SHADOW.spearman([]))

    def test_missing_values_are_dropped_not_zero_filled(self):
        mixed = SHADOW.spearman([(1, 10), (2, None), (3, 30), (4, 40)])
        clean = SHADOW.spearman([(1, 10), (3, 30), (4, 40)])
        self.assertAlmostEqual(mixed, clean, places=9)


class AdmissionTests(unittest.TestCase):
    def test_insufficient_days_is_reported_with_the_gap(self):
        decision = SHADOW.admit({"usable_day_count": 0, "stats": {}})
        self.assertFalse(decision["admitted"])
        self.assertEqual(decision["status"], "not_admitted")
        self.assertTrue(any("有效交易日 0" in item for item in decision["reasons"]))

    def test_qualifying_feature_still_requires_a_human(self):
        report = {
            "usable_day_count": SHADOW.MIN_DAYS,
            "stats": {"open_to_close": {"auction_pct": {
                "mean_ic": 0.08, "sign_consistency": 0.7, "days": SHADOW.MIN_DAYS}}},
        }
        decision = SHADOW.admit(report)
        self.assertFalse(decision["admitted"])
        self.assertEqual(len(decision["qualifying"]), 1)
        self.assertIn("人工", decision["next_step"])

    def test_each_feature_target_needs_its_own_minimum_history(self):
        report = {
            "usable_day_count": SHADOW.MIN_DAYS,
            "stats": {"open_to_close": {
                "main_pct": {"mean_ic": 0.08, "sign_consistency": 1.0, "days": 1},
                "auction_pct": {"mean_ic": 0.08, "sign_consistency": 0.7,
                                "days": SHADOW.MIN_DAYS},
            }},
        }
        decision = SHADOW.admit(report)
        self.assertEqual([item["feature"] for item in decision["qualifying"]], ["auction_pct"])
        self.assertTrue(any("open_to_close/main_pct 有效交易日 1" in reason
                            for reason in decision["reasons"]))

    def test_weak_signal_is_not_admitted(self):
        report = {"usable_day_count": SHADOW.MIN_DAYS,
                  "stats": {"open_to_close": {"auction_pct": {
                      "mean_ic": 0.005, "sign_consistency": 0.52, "days": SHADOW.MIN_DAYS}}}}
        decision = SHADOW.admit(report)
        self.assertFalse(decision["admitted"])
        self.assertEqual(decision["qualifying"], [])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = mock.patch.dict("os.environ", {"ASTOCK_DATA_DIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def test_without_archive_the_report_says_insufficient(self):
        report = SHADOW.build_report(dt.datetime(2026, 10, 26, 15, 0, tzinfo=A.CN))
        self.assertEqual(report["status"], "insufficient_days")
        self.assertFalse(report["enabled"])
        self.assertFalse(report["admission"]["admitted"])
        self.assertEqual(report["usable_day_count"], 0)

    def test_day_without_kline_is_skipped_instead_of_scored(self):
        folder = self.root / "auction_tracking"
        folder.mkdir(parents=True, exist_ok=True)
        payload = {"trade_date": "2026-10-23", "rows": [
            {"code": f"60000{index}", "pct": 1.0, "amount": 1000.0 * index}
            for index in range(1, 7)]}
        (folder / "2026-10-23.json").write_text(json.dumps(payload), encoding="utf-8")
        with mock.patch.object(SHADOW, "load_kline_days", return_value={}):
            report = SHADOW.build_report(dt.datetime(2026, 10, 26, 15, 0, tzinfo=A.CN))
        self.assertEqual(report["usable_day_count"], 0)
        self.assertIn("2026-10-23", report["skipped_days"])

    def test_feature_stats_are_built_from_real_kline_rows(self):
        folder = self.root / "auction_tracking"
        folder.mkdir(parents=True, exist_ok=True)
        day = dt.date(2026, 10, 23)
        rows = [{"code": f"60000{index}", "pct": float(index), "amount": 1_000_000.0 * index}
                for index in range(1, 7)]
        (folder / "2026-10-23.json").write_text(
            json.dumps({"trade_date": day.isoformat(), "rows": rows}), encoding="utf-8")

        def fake_kline(code):
            index = int(code[-1])
            return {
                day - dt.timedelta(days=1): {"open": 10.0, "close": 10.0, "amount": 1_000_000.0},
                day: {"open": 10.0, "close": 10.0 + index * 0.1, "amount": 1_000_000.0},
                day + dt.timedelta(days=1): {"open": 10.0, "close": 10.0 + index * 0.2,
                                             "amount": 1_000_000.0},
            }

        with mock.patch.object(SHADOW, "load_kline_days", side_effect=fake_kline):
            report = SHADOW.build_report(dt.datetime(2026, 10, 26, 15, 0, tzinfo=A.CN))
        self.assertEqual(report["usable_day_count"], 1)
        stats = report["stats"]["open_to_close"]["auction_pct"]
        self.assertEqual(stats["days"], 1)
        self.assertIsNotNone(stats["mean_ic"])
        ratio = report["stats"]["close_to_next_close"]["amount_ratio"]
        self.assertEqual(ratio["days"], 1)
        self.assertFalse(report["admission"]["admitted"])

    def test_historical_asof_excludes_future_archives_and_returns(self):
        folder = self.root / "auction_tracking"
        folder.mkdir(parents=True, exist_ok=True)
        day = dt.date(2026, 10, 23)
        future_day = dt.date(2026, 10, 26)
        for archive_day in (day, future_day):
            rows = [{"code": f"60000{index}", "pct": float(index),
                     "amount": 1_000_000.0 * index} for index in range(1, 7)]
            (folder / f"{archive_day}.json").write_text(
                json.dumps({"trade_date": archive_day.isoformat(), "rows": rows}),
                encoding="utf-8")

        def fake_kline(code):
            index = int(code[-1])
            return {
                day - dt.timedelta(days=1): {"open": 10.0, "close": 10.0, "amount": 1.0},
                day: {"open": 10.0, "close": 10.0 + index * 0.1, "amount": 1.0},
                future_day: {"open": 10.0, "close": 10.0 + index * 0.2, "amount": 1.0},
            }

        with mock.patch.object(SHADOW, "load_kline_days", side_effect=fake_kline):
            report = SHADOW.build_report(dt.datetime(2026, 10, 24, 12, 0, tzinfo=A.CN))
        self.assertEqual(report["usable_days"], [day.isoformat()])
        self.assertEqual(report["archived_days"], 1)
        self.assertEqual(report["stats"]["open_to_close"]["auction_pct"]["days"], 1)
        self.assertEqual(report["stats"]["close_to_next_close"]["auction_pct"]["days"], 0)

    def test_asof_after_close_can_use_same_day_close(self):
        folder = self.root / "auction_tracking"
        folder.mkdir(parents=True, exist_ok=True)
        day = dt.date(2026, 10, 23)
        rows = [{"code": f"60000{index}", "pct": float(index),
                 "amount": 1_000_000.0 * index} for index in range(1, 7)]
        (folder / f"{day}.json").write_text(
            json.dumps({"trade_date": day.isoformat(), "rows": rows}), encoding="utf-8")

        def fake_kline(code):
            return {day: {"open": 10.0, "close": 10.0 + int(code[-1]) * 0.1,
                          "amount": 1.0}}
        with mock.patch.object(SHADOW, "load_kline_days", side_effect=fake_kline):
            before_close = SHADOW.build_report(dt.datetime(2026, 10, 23, 14, 59, tzinfo=A.CN))
            after_close = SHADOW.build_report(dt.datetime(2026, 10, 23, 15, 0, tzinfo=A.CN))
        self.assertEqual(before_close["archived_days"], 0)
        self.assertEqual(after_close["usable_days"], [day.isoformat()])
        self.assertEqual(after_close["stats"]["open_to_close"]["auction_pct"]["days"], 1)
        self.assertEqual(after_close["stats"]["close_to_next_close"]["auction_pct"]["days"], 0)


if __name__ == "__main__":
    unittest.main()
