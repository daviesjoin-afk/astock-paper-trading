#!/usr/bin/env python3
"""Offline tests for the announcement scan and its collector helpers.

The scan's whole value rests on one property: an announcement dated after the
decision day must not be counted. Everything else is arithmetic.
"""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from announcement_scan import (  # noqa: E402
    FACTORS, MIN_POINTS, factor_value, spearman, _day_gap, _shift,
)


class TestWindowCounting(unittest.TestCase):
    def test_announcements_after_the_decision_day_do_not_count(self):
        # Three announcements: 40 days ago, 3 days ago, and one tomorrow.
        days = ["2026-10-01", "2026-11-07", "2026-11-11"]
        decision = "2026-11-10"
        value = factor_value("announcements_5d", days, decision)
        self.assertEqual(value, 1.0, "only the 11-07 announcement is inside both "
                                     "the 5-day window and the past")

    def test_a_future_announcement_never_changes_the_5d_count(self):
        base = ["2026-10-01", "2026-11-07"]
        decision = "2026-11-10"
        before = factor_value("announcements_5d", base, decision)
        after = factor_value("announcements_5d", base + ["2026-12-01"], decision)
        self.assertEqual(before, after,
                         "an announcement dated after the decision must be invisible")

    def test_the_60d_window_excludes_older_announcements(self):
        days = ["2026-01-01", "2026-10-15"]
        value = factor_value("announcements_60d", days, "2026-11-10")
        self.assertEqual(value, 1.0)

    def test_days_since_last_uses_the_most_recent_past_announcement(self):
        days = ["2026-10-01", "2026-11-05", "2026-12-31"]
        self.assertEqual(factor_value("days_since_last", days, "2026-11-10"), 5.0)

    def test_a_symbol_with_no_announcement_is_unscored_not_zero(self):
        # "Never published" is not the same claim as "published nothing in the
        # window". Scoring it as zero would invent a fact.
        self.assertIsNone(factor_value("announcements_20d", [], "2026-11-10"))

    def test_an_unknown_factor_is_an_error(self):
        with self.assertRaises(ValueError):
            factor_value("not_a_factor", ["2026-11-01"], "2026-11-10")


class TestDateArithmetic(unittest.TestCase):
    def test_shift_goes_backwards(self):
        self.assertEqual(_shift("2026-11-10", -5), "2026-11-05")

    def test_gap_counts_calendar_days(self):
        self.assertEqual(_day_gap("2026-11-01", "2026-11-10"), 9)


class TestCorrelation(unittest.TestCase):
    def test_reversed_is_minus_one(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)

    def test_constant_is_not_comparable(self):
        self.assertIsNone(spearman([1, 1, 1], [1, 2, 3]))


class TestLibrary(unittest.TestCase):
    def test_four_announcement_factors(self):
        self.assertEqual(len(FACTORS), 4)

    def test_the_window_guard_is_declared(self):
        self.assertGreaterEqual(MIN_POINTS, 60,
                                "a short series must not be scored")


if __name__ == "__main__":
    unittest.main(verbosity=2)
