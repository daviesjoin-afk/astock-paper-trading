#!/usr/bin/env python3
"""Offline tests for the power check.

The point of this tool is to stop experiments before they are run. Its
arithmetic has to be right, and its two directions have to agree.
"""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from power_check import (  # noqa: E402
    Z95, detectable_effect, required_samples, verdict,
)


class TestDetectableEffect(unittest.TestCase):
    def test_it_is_the_standard_error_times_the_z_value(self):
        # stdev 507 over 22 samples: se = 108.1, so the floor is ~211.9
        floor = detectable_effect(507, 22)
        self.assertAlmostEqual(floor, Z95 * 507 / 22 ** 0.5, places=6)
        self.assertAlmostEqual(round(floor), 212, places=0)

    def test_more_samples_lower_the_floor(self):
        self.assertLess(detectable_effect(507, 100), detectable_effect(507, 22))

    def test_zero_samples_is_an_error_not_infinity(self):
        with self.assertRaises(ValueError):
            detectable_effect(507, 0)


class TestRequiredSamples(unittest.TestCase):
    def test_a_larger_effect_needs_fewer_samples(self):
        self.assertLess(required_samples(507, 200), required_samples(507, 50))

    def test_the_two_directions_agree(self):
        # The two functions are inverses up to the ceiling: at the returned
        # count the effect is always resolvable, and the count is never more
        # than one sample above the exact crossing point.
        stdev, effect = 507, 100
        needed = required_samples(stdev, effect)
        self.assertIsNotNone(needed)
        self.assertLessEqual(
            detectable_effect(stdev, needed), effect,
            "the returned sample count must be enough to resolve the effect",
        )
        self.assertGreater(
            detectable_effect(stdev, needed - 1), 0,
            "one fewer sample still leaves a positive floor",
        )

    def test_the_count_is_within_one_of_the_crossing_point(self):
        stdev, effect = 507, 100
        needed = required_samples(stdev, effect)
        exact = (1.96 * stdev / effect) ** 2
        self.assertGreaterEqual(needed, exact)
        self.assertLess(needed - exact, 1.0, "ceil must not overshoot by a whole sample")

    def test_a_zero_effect_has_no_sample_requirement(self):
        # Returning a large number would read as "just collect more", which is
        # false: no sample size distinguishes zero from zero.
        self.assertIsNone(required_samples(507, 0))
        self.assertIsNone(required_samples(507, -20))

    def test_the_twenty_bps_case_needs_far_more_than_we_have(self):
        needed = required_samples(507, 20)
        self.assertIsNotNone(needed)
        self.assertGreater(needed, 2000, "20 bps over 507 stdev needs thousands of folds")


class TestVerdict(unittest.TestCase):
    def test_an_effect_below_the_floor_is_not_measurable(self):
        floor = detectable_effect(507, 22)
        result = verdict(28, floor)
        self.assertFalse(result["measurable"])
        self.assertIn("not significant", result["reason"])

    def test_an_effect_above_the_floor_is_measurable(self):
        floor = detectable_effect(507, 22)
        self.assertTrue(verdict(300, floor)["measurable"])

    def test_the_ratio_is_reported(self):
        floor = detectable_effect(507, 22)
        result = verdict(106, floor)
        self.assertEqual(result["ratio"], 0.5)

    def test_no_noise_estimate_is_refused(self):
        self.assertFalse(verdict(100, 0)["measurable"])


class TestRealCases(unittest.TestCase):
    """The numbers this project actually produced."""

    def test_every_account_level_experiment_was_below_the_floor(self):
        effects = [28, 38, 1, 3, -2, -2, 30, -73, -18, -24]
        floor = detectable_effect(507, 22)
        for effect in effects:
            self.assertFalse(
                verdict(abs(effect), floor)["measurable"],
                f"{effect} bps should be below the {floor:.0f} bps floor",
            )

    def test_the_two_significant_findings_clear_it(self):
        floor_steady = detectable_effect(747, 53)
        self.assertTrue(verdict(1193, floor_steady)["measurable"])
        floor_trades = detectable_effect(732, 271)
        self.assertTrue(verdict(215, floor_trades)["measurable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
