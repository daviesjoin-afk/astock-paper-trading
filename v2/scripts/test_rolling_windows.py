#!/usr/bin/env python3
"""Offline tests for fold generation.

The single most important property: no test window may overlap its training
window, and no fold may overlap an adjacent fold's training data in a way that
makes an earlier test day part of a later training set without declaring it.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rolling_windows import build_folds, summarize  # noqa: E402


def days(count: int) -> list[str]:
    """Synthetic ordered days; the calendar shape is all that matters here."""
    return [f"2025-{1 + index // 28:02d}-{1 + index % 28:02d}" for index in range(count)]


class TestNoLeak(unittest.TestCase):
    def test_every_fold_has_a_strictly_later_test_window(self):
        folds = build_folds(days(600), test_days=60, step_days=60, min_train_days=200)
        self.assertGreater(len(folds), 0)
        for fold in folds:
            self.assertLess(fold["train"][1], fold["test"][0],
                            f"fold {fold['fold']} leaks")

    def test_adjacent_folds_are_disjoint_in_their_test_windows(self):
        folds = build_folds(days(600), test_days=60, step_days=60, min_train_days=200)
        for earlier, later in zip(folds, folds[1:]):
            self.assertLessEqual(earlier["test"][1], later["test"][0])

    def test_exact_dates_come_from_the_supplied_calendar(self):
        calendar = days(300)
        folds = build_folds(calendar, test_days=50, step_days=50, min_train_days=100)
        for fold in folds:
            for day in fold["train"] + fold["test"]:
                self.assertIn(day, calendar)


class TestShapes(unittest.TestCase):
    def test_expanding_training_window_never_shrinks(self):
        folds = build_folds(days(800), test_days=60, step_days=60, min_train_days=200)
        sizes = [fold["train_days"] for fold in folds]
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(sizes[0], 200)
        # Each step adds exactly one test window's worth of history.
        self.assertEqual(sizes[1] - sizes[0], 60)

    def test_rolling_training_window_keeps_a_fixed_width(self):
        folds = build_folds(days(800), test_days=60, step_days=60,
                            min_train_days=200, expanding=False)
        self.assertTrue(all(fold["train_days"] == 200 for fold in folds))
        for fold in folds:
            self.assertLess(fold["train"][1], fold["test"][0])

    def test_every_test_window_is_the_requested_length(self):
        folds = build_folds(days(600), test_days=42, step_days=42, min_train_days=200)
        self.assertTrue(all(fold["test_days"] == 42 for fold in folds))

    def test_overlapping_step_reuses_test_days_but_still_does_not_leak(self):
        # A step shorter than the test window means later training sets include
        # days an earlier fold already tested. That is real information reuse,
        # and it must still never put a test day inside its own train window.
        folds = build_folds(days(600), test_days=100, step_days=20, min_train_days=200)
        self.assertGreater(len(folds), 1)
        for fold in folds:
            self.assertLess(fold["train"][1], fold["test"][0])


class TestEdges(unittest.TestCase):
    def test_history_too_short_yields_no_folds(self):
        folds = build_folds(days(50), test_days=60, step_days=60, min_train_days=200)
        self.assertEqual(folds, [])
        self.assertEqual(summarize(folds)["folds"], 0)

    def test_exactly_one_fold_fits(self):
        # 200 train + 60 test = 260 days needed.
        folds = build_folds(days(260), test_days=60, step_days=60, min_train_days=200)
        self.assertEqual(len(folds), 1)
        self.assertEqual(folds[0]["train"][0], days(260)[0])
        self.assertEqual(folds[0]["test"][1], days(260)[-1])

    def test_one_day_short_produces_nothing(self):
        folds = build_folds(days(259), test_days=60, step_days=60, min_train_days=200)
        self.assertEqual(folds, [])

    def test_zero_length_windows_are_rejected(self):
        for bad in [(0, 60, 200), (60, 0, 200), (60, 60, 0), (-1, 60, 200)]:
            with self.assertRaises(ValueError):
                build_folds(days(600), test_days=bad[0], step_days=bad[1],
                            min_train_days=bad[2])


class TestSummary(unittest.TestCase):
    def test_summary_reports_the_span_and_the_sizes(self):
        folds = build_folds(days(600), test_days=60, step_days=60, min_train_days=200)
        summary = summarize(folds)
        self.assertEqual(summary["folds"], len(folds))
        self.assertEqual(summary["first_train_start"], days(600)[0])
        self.assertEqual(summary["last_test_end"], folds[-1]["test"][1])
        self.assertEqual(len(summary["train_days"]), len(folds))

    def test_summary_says_why_a_single_fold_is_not_enough(self):
        folds = build_folds(days(300), test_days=60, step_days=60, min_train_days=200)
        self.assertIn("one sample", summarize(folds)["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
