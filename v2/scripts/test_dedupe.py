#!/usr/bin/env python3
"""Offline tests for dedupe_hypotheses.

The correlation maths is tested directly on hand-built series so a regression
in ranking or tie handling shows up without touching a database or a probe.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dedupe_hypotheses import rank, spearman  # noqa: E402


class TestRank(unittest.TestCase):
    def test_distinct_values_get_sequential_ranks(self):
        self.assertEqual(rank([10.0, 20.0, 30.0]), [1.0, 2.0, 3.0])

    def test_ties_share_the_average_rank(self):
        # Positions 2,3,4 in sorted order -> mean 3.0.
        self.assertEqual(rank([5.0, 1.0, 5.0, 5.0]), [3.0, 1.0, 3.0, 3.0])

    def test_all_equal_ranks_are_all_the_middle(self):
        self.assertEqual(rank([7.0, 7.0, 7.0]), [2.0, 2.0, 2.0])

    def test_negative_values_rank_correctly(self):
        # -10 is smallest so it ranks first; -5 second; 0 largest.
        self.assertEqual(rank([-5.0, -10.0, 0.0]), [2.0, 1.0, 3.0])


class TestSpearman(unittest.TestCase):
    def test_identical_ordering_is_one(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)

    def test_reversed_ordering_is_minus_one(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)

    def test_scaling_does_not_change_the_correlation(self):
        # This is the case that catches `mom20 / 2` masquerading as a new idea.
        base = [1, 3, 2, 5, 4]
        self.assertAlmostEqual(spearman(base, [x / 2 for x in base]), 1.0)

    def test_negation_is_a_perfect_mirror(self):
        base = [1, 3, 2, 5, 4]
        self.assertAlmostEqual(spearman(base, [-x for x in base]), -1.0)

    def test_independent_ordering_is_not_extreme(self):
        # Neither ordering predicts the other; the value must stay well away
        # from +-1 or the duplicate test would swallow real distinctness.
        rho = spearman([1, 2, 3, 4, 5, 6], [3, 1, 4, 6, 2, 5])
        self.assertIsNotNone(rho)
        self.assertLess(abs(rho), 0.85)

    def test_constant_series_returns_none_not_zero(self):
        # A constant factor carries no ordering. Reporting 0.0 would look like
        # "measured, unrelated" when the truth is "not comparable".
        self.assertIsNone(spearman([1, 1, 1, 1], [1, 2, 3, 4]))

    def test_too_few_points_returns_none(self):
        self.assertIsNone(spearman([1, 2], [2, 1]))

    def test_length_mismatch_returns_none(self):
        self.assertIsNone(spearman([1, 2, 3], [1, 2]))

    def test_ties_do_not_invent_a_correlation(self):
        # Both series are almost all constant; the surviving variation is
        # opposite, so the answer must be negative, not strongly positive.
        rho = spearman([5, 5, 5, 1], [5, 5, 5, 9])
        self.assertIsNotNone(rho)
        self.assertLess(rho, 0)


class TestThresholdDecision(unittest.TestCase):
    """The verdict boundaries, exercised without a probe."""

    def verdict(self, rho, threshold=0.999):
        if rho is None:
            return "not_comparable"
        if abs(rho) >= threshold:
            return "mirror" if rho < 0 else "identical"
        return "distinct"

    def test_exact_duplicates_are_caught(self):
        self.assertEqual(self.verdict(1.0), "identical")

    def test_exact_mirrors_are_caught_but_labelled(self):
        self.assertEqual(self.verdict(-1.0), "mirror")

    def test_near_duplicates_inside_the_threshold_are_caught(self):
        self.assertEqual(self.verdict(0.9995), "identical")

    def test_high_but_not_duplicate_correlation_survives(self):
        self.assertEqual(self.verdict(0.98), "distinct")


if __name__ == "__main__":
    unittest.main(verbosity=2)
