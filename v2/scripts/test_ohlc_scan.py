#!/usr/bin/env python3
"""Offline tests for the OHLC shape scan.

Two things matter: the shape arithmetic, and the no-look-ahead cut that the
volume scan already had to learn the hard way.
"""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ohlc_scan import (  # noqa: E402
    FACTORS, compute, factor_values, forward_returns, spearman,
)

NEWLINE = chr(10)


def point(day: str, op: int, hi: int, lo: int, cl: int, prev: int):
    return (day, op, hi, lo, cl, prev)


class TestShapes(unittest.TestCase):
    def test_an_upper_shadow_is_measured_against_the_range(self):
        # open 100, close 100, high 200, low 100: the whole range is upper shadow.
        value = compute("upper_shadow_ratio", [point("d", 100, 200, 100, 100, 100)])
        self.assertEqual(value, 10_000)

    def test_a_lower_shadow_is_the_mirror(self):
        value = compute("lower_shadow_ratio", [point("d", 200, 200, 100, 200, 200)])
        self.assertEqual(value, 10_000)

    def test_close_location_says_where_the_auction_finished(self):
        at_high = compute("close_location", [point("d", 100, 200, 100, 200, 100)])
        at_low = compute("close_location", [point("d", 200, 200, 100, 100, 200)])
        self.assertEqual(at_high, 10_000)
        self.assertEqual(at_low, 0)

    def test_a_full_body_has_no_shadows(self):
        value = compute("body_ratio", [point("d", 100, 200, 100, 200, 100)])
        self.assertEqual(value, 10_000)

    def test_a_gap_is_measured_against_the_previous_close(self):
        value = compute("gap_bps", [point("d", 110, 120, 100, 110, 100)])
        self.assertEqual(value, 1_000)

    def test_a_zero_range_is_not_measured(self):
        # A halted or single-tick session has no shape to describe.
        self.assertIsNone(compute("upper_shadow_ratio", [point("d", 100, 100, 100, 100, 100)]))
        self.assertIsNone(compute("close_location", [point("d", 100, 100, 100, 100, 100)]))

    def test_a_non_positive_previous_close_is_not_measured(self):
        self.assertIsNone(compute("gap_bps", [point("d", 110, 120, 100, 110, 0)]))

    def test_an_unknown_factor_is_an_error(self):
        with self.assertRaises(ValueError):
            compute("not_a_factor", [point("d", 100, 110, 90, 105, 100)])


class TestNoLookAhead(unittest.TestCase):
    def test_the_factor_cannot_see_the_forward_window(self):
        # Two series identical up to the decision bar and different after it must
        # produce identical factor values. This is the guard the volume scan
        # earned the hard way.
        head = [point(f"d{i}", 1_000 + (i % 5), 1_020 + (i % 7), 980 - (i % 3), 1_000, 1_000)
                for i in range(80)]
        rising = head + [point(f"e{i}", 1_000, 1_500, 1_000, 1_400, 1_000) for i in range(20)]
        falling = head + [point(f"e{i}", 1_000, 1_050, 600, 700, 1_000) for i in range(20)]
        for name in ("upper_shadow_ratio", "close_location", "body_ratio", "gap_bps"):
            a = factor_values({"A": rising}, name)
            b = factor_values({"A": falling}, name)
            self.assertIn("A", a, name)
            self.assertEqual(a["A"], b["A"], f"{name} must not depend on the forward window")

    def test_the_label_starts_after_the_decision_bar(self):
        # Flat first 80 sessions, then a doubling: the label sees only the rise.
        head = [point(f"d{i}", 1_000, 1_000, 1_000, 1_000, 1_000) for i in range(80)]
        tail = [point(f"e{i}", 1_000, 1_000, 1_000, 1_000 * 2 ** i, 1_000) for i in range(1, 21)]
        result = forward_returns({"A": head + tail})
        self.assertGreater(result["A"], 0)

    def test_a_series_that_only_moves_before_the_split_has_no_label_move(self):
        # The last pre-split close must equal the flat tail, otherwise the split
        # itself is a move and the label is correctly non-zero.
        head = [point(f"d{i}", 1_000, 1_000, 1_000, 1_000 + i * 5, 1_000) for i in range(79)]
        head.append(point("d79", 1_395, 1_395, 1_395, 1_395, 1_390))
        tail = [point(f"e{i}", 1_395, 1_395, 1_395, 1_395, 1_395) for i in range(20)]
        self.assertEqual(forward_returns({"A": head + tail})["A"], 0.0)


class TestCorrelation(unittest.TestCase):
    def test_reversed_order_is_minus_one(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)

    def test_identical_order_is_one(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)

    def test_constant_series_is_not_comparable(self):
        self.assertIsNone(spearman([1, 1, 1], [1, 2, 3]))


class TestLibrary(unittest.TestCase):
    def test_the_six_shape_factors_are_present(self):
        self.assertEqual(len(FACTORS), 6)
        self.assertIn("close_location", FACTORS)
        self.assertIn("upper_shadow_ratio", FACTORS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
