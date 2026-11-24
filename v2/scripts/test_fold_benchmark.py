#!/usr/bin/env python3
"""Offline tests for the fold benchmark.

The behaviour that matters: a fold that lost money in a worse market must be
labelled a win, and a fold that made money in a better market must be labelled
a loss. Getting this backwards is how a defensive configuration gets thrown
away for "losing money".
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fold_benchmark import classify, market_median_bps, summarise  # noqa: E402


def row(fold: int, market: int, strat: int, label: str | None = None) -> dict:
    return {"fold": fold, "test": ["2024-01-01", "2024-06-30"], "status": "ok",
            "market_bps": market, "strat_bps": strat,
            "excess_bps": strat - market,
            "label": label or classify(market, strat - market)}


class TestClassify(unittest.TestCase):
    def test_losing_less_than_a_falling_market_is_a_win(self):
        self.assertEqual(classify(-2000, +1800), "defensive_win")

    def test_losing_more_than_a_falling_market_is_a_loss(self):
        self.assertEqual(classify(-2000, -500), "defensive_loss")

    def test_beating_a_rising_market_is_a_win(self):
        self.assertEqual(classify(1000, +500), "offensive_win")

    def test_trailing_a_rising_market_is_a_loss(self):
        self.assertEqual(classify(1600, -1100), "offensive_loss")

    def test_unmeasured_when_either_side_is_missing(self):
        self.assertEqual(classify(None, 100), "unmeasured")
        self.assertEqual(classify(100, None), "unmeasured")


class TestShape(unittest.TestCase):
    def test_earning_in_down_markets_and_losing_in_up_ones_is_defensive(self):
        summary = summarise([row(0, -2400, -876), row(1, 1600, 980),
                             row(2, 1600, 536), row(3, 500, 1335),
                             row(4, -2000, -186)])
        self.assertEqual(summary["shape"], "defensive")
        self.assertGreater(summary["mean_excess_when_down_bps"], 0)
        self.assertLess(summary["mean_excess_when_up_bps"], 0)

    def test_the_opposite_pattern_is_offensive(self):
        summary = summarise([row(0, -1000, -1500), row(1, 1000, 1500)])
        self.assertEqual(summary["shape"], "offensive")

    def test_no_consistent_sign_is_inconsistent(self):
        # Down-market excess positive, up-market excess ALSO positive -> the
        # strategy beats the market both ways, which is neither shape.
        summary = summarise([row(0, -1000, 500), row(1, 1000, 2000)])
        self.assertEqual(summary["shape"], "inconsistent")

    def test_losing_in_both_states_is_also_inconsistent(self):
        summary = summarise([row(0, -1000, -2000), row(1, 1000, 500)])
        self.assertEqual(summary["shape"], "inconsistent")

    def test_all_folds_on_one_side_is_one_sided(self):
        summary = summarise([row(0, -1000, 500), row(1, -2000, 100)])
        self.assertEqual(summary["shape"], "one_sided")

    def test_no_measured_fold_says_so(self):
        summary = summarise([{"fold": 0, "market_bps": None, "strat_bps": None,
                              "excess_bps": None, "label": "unmeasured"}])
        self.assertIn("note", summary)


class TestMarketMedian(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "m.sqlite3"
        conn = sqlite3.connect(self.db)
        conn.executescript(
            "CREATE TABLE bar(symbol TEXT, day TEXT, close_cents INTEGER, adjust TEXT, "
            "tradable INTEGER);"
        )
        # Three symbols: +100%, +0%, -50% over the window.
        rows = []
        for index in range(12):
            day = f"2024-01-{index + 1:02d}"
            rows.append(("A", day, 1000 + index * 100, "none", 1))
            rows.append(("B", day, 1000, "none", 1))
            rows.append(("C", day, 1000 - index * 40, "none", 1))
        conn.executemany("INSERT INTO bar VALUES(?,?,?,?,?)", rows)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_median_is_the_benchmark(self):
        result = market_median_bps(self.db, "2024-01-01", "2024-01-12")
        self.assertEqual(result["symbols"], 3)
        # A: +1100 bps, B: 0, C: -440 bps -> median 0
        self.assertEqual(result["median_bps"], 0)

    def test_short_series_are_excluded(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO bar VALUES('D','2024-01-01',500,'none',1)")
        conn.execute("INSERT INTO bar VALUES('D','2024-01-12',900,'none',1)")
        conn.commit()
        conn.close()
        # D has only 2 observations, below the min_days floor.
        result = market_median_bps(self.db, "2024-01-01", "2024-01-12", min_days=5)
        self.assertEqual(result["symbols"], 3)

    def test_a_halt_with_only_reference_closes_is_excluded(self):
        # A name suspended through the window carries rows, but at tradable=0.
        # A return measured off a reference close was never available to trade.
        conn = sqlite3.connect(self.db)
        for index in range(12):
            conn.execute("INSERT INTO bar VALUES('H',?,800,'none',0)",
                         (f"2024-01-{index + 1:02d}",))
        conn.commit()
        conn.close()
        result = market_median_bps(self.db, "2024-01-01", "2024-01-12")
        self.assertEqual(result["symbols"], 3)
        self.assertEqual(result["excluded"].get("untradable_at_first_day"), 1)

    def test_a_symbol_missing_the_first_day_is_excluded(self):
        # Its first row is 2024-01-02, so its "window return" starts a day late.
        conn = sqlite3.connect(self.db)
        for index in range(1, 12):
            conn.execute("INSERT INTO bar VALUES('L',?,1000,'none',1)",
                         (f"2024-01-{index + 1:02d}",))
        conn.commit()
        conn.close()
        result = market_median_bps(self.db, "2024-01-01", "2024-01-12")
        self.assertEqual(result["symbols"], 3)
        self.assertEqual(result["excluded"].get("missing_first_day"), 1)

    def test_empty_window_returns_none_not_zero(self):
        result = market_median_bps(self.db, "2030-01-01", "2030-12-31")
        self.assertEqual(result["symbols"], 0)
        self.assertIsNone(result["median_bps"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
