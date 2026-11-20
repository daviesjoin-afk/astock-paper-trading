#!/usr/bin/env python3
"""Offline tests for the regime accuracy measurement.

Two things must hold:
* an inverted call is reported as inverted, not smoothed into "mixed";
* a single fold cannot produce an "everywhere" verdict.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from regime_accuracy import (  # noqa: E402
    forward_return_bps, judge, regime_calls, score_calls,
)


class TestForwardReturn(unittest.TestCase):
    def test_a_rising_series_is_positive(self):
        series = [100, 105, 110]
        self.assertEqual(forward_return_bps(series, 0, 2), 1_000)

    def test_a_falling_series_is_negative(self):
        series = [100, 95, 90]
        self.assertEqual(forward_return_bps(series, 0, 2), -1_000)

    def test_not_enough_future_returns_none_not_zero(self):
        # Zero would read as "flat market", which is a measurement.
        self.assertIsNone(forward_return_bps([100, 101], 0, 5))

    def test_zero_start_is_unmeasurable(self):
        self.assertIsNone(forward_return_bps([0, 100], 0, 1))


class TestJudge(unittest.TestCase):
    def test_every_directional_call_wrong_is_inverted(self):
        table = {
            "trending": {"samples": 10, "median_forward_bps": -200, "mean_forward_bps": -180},
            "declining": {"samples": 5, "median_forward_bps": +600, "mean_forward_bps": 600},
            "choppy": {"samples": 20, "median_forward_bps": 300, "mean_forward_bps": 320},
        }
        result = judge(table)
        self.assertEqual(result["direction"], "inverted")
        self.assertEqual(result["verdicts"]["trending"], "inverted")
        self.assertEqual(result["verdicts"]["declining"], "inverted")

    def test_every_directional_call_right_is_supported(self):
        table = {
            "trending": {"samples": 10, "median_forward_bps": 400, "mean_forward_bps": 400},
            "declining": {"samples": 5, "median_forward_bps": -300, "mean_forward_bps": -300},
        }
        self.assertEqual(judge(table)["direction"], "supported")

    def test_one_right_one_wrong_is_mixed(self):
        table = {
            "trending": {"samples": 10, "median_forward_bps": 400, "mean_forward_bps": 400},
            "declining": {"samples": 5, "median_forward_bps": 300, "mean_forward_bps": 300},
        }
        self.assertEqual(judge(table)["direction"], "mixed")

    def test_choppy_is_not_scored_on_direction(self):
        # "Choppy" claims nothing about direction; grading it would be a
        # category error.
        table = {"choppy": {"samples": 20, "median_forward_bps": 900, "mean_forward_bps": 900}}
        result = judge(table)
        self.assertEqual(result["verdicts"]["choppy"], "directionless")
        self.assertEqual(result["direction"], "unmeasured")

    def test_a_regime_with_no_samples_is_not_graded(self):
        table = {"trending": {"samples": 0, "median_forward_bps": 0, "mean_forward_bps": 0}}
        self.assertEqual(judge(table)["verdicts"]["trending"], "no_sample")


class TestRegimeCalls(unittest.TestCase):
    def test_first_call_of_a_day_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps({"filtered": {"trades": [
                {"trade_day": "2025-01-10", "basis": {"regime": "choppy"}},
                {"trade_day": "2025-01-10", "basis": {"regime": "trending"}},
                {"trade_day": "2025-01-11", "basis": {"regime": "declining"}},
            ]}}), encoding="utf-8")
            calls = regime_calls(path, "filtered")
        self.assertEqual(calls["2025-01-10"], "choppy")
        self.assertEqual(calls["2025-01-11"], "declining")
        self.assertEqual(len(calls), 2)

    def test_trades_without_a_basis_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps({"filtered": {"trades": [
                {"trade_day": "2025-01-10"},
                {"trade_day": "2025-01-11", "basis": {}},
            ]}}), encoding="utf-8")
            self.assertEqual(regime_calls(path, "filtered"), {})


class TestScoreCalls(unittest.TestCase):
    def test_groups_by_regime_and_measures_forward_move(self):
        days = ["d0", "d1", "d2", "d3"]
        series = [100, 110, 120, 130]
        calls = {"d0": "trending", "d1": "declining"}
        table = score_calls(calls, days, series, horizon=2)
        # d0 -> d2: 100 -> 120 = +2000 bps
        self.assertEqual(table["trending"]["median_forward_bps"], 2_000)
        # d1 -> d3: 110 -> 130 = +1818 bps
        self.assertEqual(table["declining"]["median_forward_bps"], 1_818)

    def test_a_call_on_the_last_day_has_no_forward_window(self):
        days = ["d0", "d1"]
        series = [100, 110]
        table = score_calls({"d1": "trending"}, days, series, horizon=2)
        self.assertEqual(table, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
