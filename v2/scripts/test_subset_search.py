#!/usr/bin/env python3
"""Offline tests for the subset search and its correction.

The property under test is the one that matters: a subgroup found by searching
must not be reported as a discovery unless it survives the correction for
having searched.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from subset_search import (  # noqa: E402
    build_hypotheses, interval, load_trades, report, z_for_alpha,
)


class TestZTable(unittest.TestCase):
    def test_the_standard_levels_are_the_standard_values(self):
        self.assertAlmostEqual(z_for_alpha(0.05), 1.960, places=3)
        self.assertAlmostEqual(z_for_alpha(0.005), 2.807, places=3)
        self.assertAlmostEqual(z_for_alpha(0.001), 3.291, places=3)

    def test_a_tighter_alpha_needs_a_wider_interval(self):
        self.assertGreater(z_for_alpha(0.005), z_for_alpha(0.05))
        self.assertGreater(z_for_alpha(0.0001), z_for_alpha(0.001))

    def test_the_fallback_is_monotone(self):
        values = [z_for_alpha(a) for a in (0.05, 0.01, 0.005, 0.001, 0.0001)]
        self.assertEqual(values, sorted(values), "tighter alpha must never give a smaller z")


class TestInterval(unittest.TestCase):
    def test_it_brackets_the_mean(self):
        result = interval([100, 200, 300, 400, 500], 1.96)
        self.assertEqual(result["n"], 5)
        self.assertEqual(result["mean"], 300)
        self.assertLess(result["lo"], 300)
        self.assertGreater(result["hi"], 300)

    def test_a_wider_z_gives_a_wider_interval(self):
        values = [100, 200, 300, 400, 500]
        narrow = interval(values, 1.96)
        wide = interval(values, 2.807)
        self.assertLess(wide["lo"], narrow["lo"])
        self.assertGreater(wide["hi"], narrow["hi"])

    def test_one_sample_has_no_interval(self):
        result = interval([100], 1.96)
        self.assertIsNone(result["lo"])


class TestHypotheses(unittest.TestCase):
    def test_the_count_is_what_the_correction_uses(self):
        # Changing the hypothesis list changes the correction, so its size is
        # part of the contract, not an implementation detail.
        self.assertEqual(len(build_hypotheses()), 10)

    def test_every_hypothesis_has_a_name_and_a_predicate(self):
        for name, predicate in build_hypotheses():
            self.assertTrue(name)
            self.assertTrue(callable(predicate))


class TestCorrectionVerdict(unittest.TestCase):
    def make_trades(self, slot: str, mean: int, count: int, spread: int = 500):
        """Trades whose pnl averages `mean` with a fixed spread."""
        trades = []
        for index in range(count):
            offset = spread if index % 2 == 0 else -spread
            trades.append({
                "fold": "0", "slot": slot, "pnl_bps": mean + offset,
                "regime": "trending", "score": 90,
            })
        return trades

    def test_a_large_effect_survives_the_correction(self):
        # A -1200 bps effect with modest spread is far outside any corrected band.
        trades = self.make_trades("steady", -1200, 60, spread=300)
        result = report(trades)
        steady = next(r for r in result["rows"] if r["hypothesis"] == "slot=steady")
        self.assertTrue(steady["corrected_significant"])
        self.assertEqual(result["verdict"], "survives_correction")

    def test_a_small_effect_is_dropped_by_the_correction(self):
        # +60 bps over 50 trades with a 500 bps spread cannot survive 10 tests.
        trades = self.make_trades("aggressive", 60, 50, spread=500)
        result = report(trades)
        aggressive = next(r for r in result["rows"] if r["hypothesis"] == "slot=aggressive")
        self.assertFalse(aggressive["corrected_significant"],
                         "a 60 bps effect must not survive a 10-way correction")

    def test_the_account_projection_applies_the_weight(self):
        trades = self.make_trades("aggressive", 100, 40, spread=200)
        result = report(trades, slot_weight_bps=5_500)
        aggressive = next(r for r in result["rows"] if r["hypothesis"] == "slot=aggressive")
        self.assertEqual(aggressive["account_excess_bps"], 55)

    def test_small_subgroups_are_skipped_not_scored(self):
        trades = self.make_trades("steady", -1200, 60, spread=300)
        trades += [{"fold": "0", "slot": "aggressive", "pnl_bps": 50,
                    "regime": "declining", "score": 90}]
        result = report(trades)
        declining = next(
            r for r in result["rows"] if r["hypothesis"] == "aggressive+regime=declining"
        )
        self.assertIn("skipped", declining)


class TestLoader(unittest.TestCase):
    def test_it_pairs_buys_and_sells_and_keeps_the_basis(self):
        payload = {"filtered": {"trades": [
            {"slot": "aggressive", "symbol": "600000.SH", "side": "buy",
             "price_cents": 1_000, "trade_day": "2025-01-02",
             "basis": {"regime": "trending", "score": 95}},
            {"slot": "aggressive", "symbol": "600000.SH", "side": "sell",
             "price_cents": 1_100, "trade_day": "2025-01-10"},
        ]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fold-3.json").write_text(json.dumps(payload), encoding="utf-8")
            trades = load_trades(root)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["pnl_bps"], 1_000)
        self.assertEqual(trades[0]["regime"], "trending")
        self.assertEqual(trades[0]["fold"], "3")

    def test_an_open_position_is_not_a_trade(self):
        payload = {"filtered": {"trades": [
            {"slot": "aggressive", "symbol": "600000.SH", "side": "buy",
             "price_cents": 1_000, "trade_day": "2025-01-02", "basis": {}},
        ]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fold-0.json").write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(load_trades(root), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
