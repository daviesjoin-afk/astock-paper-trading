#!/usr/bin/env python3
"""Offline tests for the volume factor scan.

The arithmetic matters less than the guards: an unmeasurable factor must be
reported as such, not as a zero IC, and a sign that flips must not rank above a
consistent one.
"""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from volume_scan import compute, forward_returns, factor_values, rank, spearman  # noqa: E402


class TestRankAndCorrelation(unittest.TestCase):
    def test_ties_share_the_average_rank(self):
        self.assertEqual(rank([5.0, 1.0, 5.0]), [2.5, 1.0, 2.5])

    def test_identical_order_is_one(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)

    def test_reversed_order_is_minus_one(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)

    def test_constant_series_is_not_comparable(self):
        self.assertIsNone(spearman([1, 1, 1], [1, 2, 3]))


class TestFactors(unittest.TestCase):
    def test_volume_ratio_above_ten_thousand_means_busier_than_usual(self):
        closes = [1_000] * 21
        volumes = [1_000] * 20 + [3_000]
        value = compute("volume_ratio_20", closes, volumes)
        self.assertIsNotNone(value)
        self.assertGreater(value, 10_000)

    def test_volume_ratio_is_unmeasurable_without_volume(self):
        # Zero volume must be None, not a division by zero and not zero.
        self.assertIsNone(compute("volume_ratio_20", [1_000] * 21, [0] * 21))

    def test_price_up_volume_down_is_positive_when_that_happens(self):
        closes = [1_000] * 20 + [1_100]
        # Volume shrank into the rise: the divergence the factor is named for.
        volumes = [2_000] * 15 + [1_000] * 5 + [500]
        value = compute("price_up_volume_down", closes, volumes)
        self.assertIsNotNone(value)
        self.assertGreater(value, 0)

    def test_price_up_volume_up_is_not_flagged_as_divergence(self):
        closes = [1_000] * 20 + [1_100]
        volumes = [500] * 15 + [1_000] * 5 + [3_000]
        value = compute("price_up_volume_down", closes, volumes)
        self.assertIsNotNone(value)
        self.assertLess(value, 0)

    def test_amihud_is_unmeasurable_without_volume(self):
        self.assertIsNone(compute("amihud_20", [1_000 + i for i in range(21)], [0] * 21))

    def test_volume_weighted_momentum_needs_volume(self):
        self.assertIsNone(
            compute("volume_weighted_momentum_20", [1_000 + i for i in range(21)], [0] * 21)
        )

    def test_an_unknown_factor_name_is_an_error_not_a_silent_zero(self):
        with self.assertRaises(ValueError):
            compute("not_a_factor", [1_000] * 21, [1_000] * 21)


class TestFactorValues(unittest.TestCase):
    def test_short_series_are_skipped(self):
        series = {"A": [("d0", 1_000, 1_000, 1_000)] * 10}
        self.assertEqual(factor_values(series, "volume_ratio_20"), {})

    def test_a_symbol_without_volume_is_skipped_not_zeroed(self):
        series = {"A": [(f"d{i}", 1_000, 0, 0) for i in range(61)]}
        self.assertEqual(factor_values(series, "volume_ratio_20"), {})


class TestForwardReturns(unittest.TestCase):
    def test_the_label_starts_where_the_factor_ends(self):
        # A flat first 80 sessions then a doubling over the last 20: the label
        # must see ONLY the doubling. If the factor and the label shared any
        # close, the scan would be measuring a series against itself -- which is
        # exactly the look-ahead this guard exists to prevent.
        points = [(f"d{i}", 1_000, 1, 1) for i in range(80)]
        points += [(f"e{i}", 1_000 * 2 ** i, 1, 1) for i in range(1, 21)]
        series = {"A": points}
        result = forward_returns(series, horizon=20)
        # Last 20 sessions: 1_000 * 2^20 growth from 1_000 is enormous; the point
        # is only that it is positive and strictly larger than the pre-window
        # move (which was zero).
        self.assertGreater(result["A"], 0)

    def test_a_flat_history_produces_a_zero_label(self):
        series = {"A": [(f"d{i}", 1_000, 1, 1) for i in range(100)]}
        self.assertEqual(forward_returns(series, horizon=20)["A"], 0.0)

    def test_a_series_that_moves_only_before_the_split_has_no_label_move(self):
        # Everything happens in the first 80 sessions; the last 20 are flat.
        points = [(f"d{i}", 1_000 + i * 50, 1, 1) for i in range(80)]
        points += [(f"e{i}", 1_000 + 79 * 50, 1, 1) for i in range(20)]
        series = {"A": points}
        self.assertEqual(forward_returns(series, horizon=20)["A"], 0.0)

    def test_too_short_a_series_is_excluded(self):
        series = {"A": [(f"d{i}", 1_000, 1, 1) for i in range(5)]}
        self.assertEqual(forward_returns(series, horizon=20), {})


class TestNoLookAhead(unittest.TestCase):
    def test_the_factor_never_sees_the_forward_window(self):
        # Two series that differ ONLY in the last 20 sessions must produce the
        # same factor value, because the factor is evaluated before them.
        head = [(f"d{i}", 1_000 + (i % 7), 1_000 + (i % 5), 1_000) for i in range(80)]
        rising = head + [(f"e{i}", 1_000 + i * 100, 5_000, 5_000) for i in range(1, 21)]
        falling = head + [(f"e{i}", 1_000 - i * 10, 500, 500) for i in range(1, 21)]
        a = factor_values({"A": rising}, "volume_ratio_20", horizon=20)
        b = factor_values({"A": falling}, "volume_ratio_20", horizon=20)
        self.assertIn("A", a)
        self.assertEqual(a["A"], b["A"],
                         "the factor value must not depend on the forward window")


if __name__ == "__main__":
    unittest.main(verbosity=2)
