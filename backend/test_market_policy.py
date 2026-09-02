# -*- coding: utf-8 -*-
"""Regression coverage for the shared execution/dashboard market policy."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from market_policy import MARKET_LIGHT_SCALES, market_light_scale


STRATEGIES = (
    "tq_breakout",
    "trend_pullback",
    "sector_rotation",
    "reported_profit_breakout",
    "main_force_top10",
)


class MarketPolicyTests(unittest.TestCase):
    def test_all_live_strategies_have_explicit_scale_for_every_light(self):
        for light in ("green", "yellow", "red", "unknown"):
            self.assertEqual(set(STRATEGIES), set(MARKET_LIGHT_SCALES[light]))

    def test_main_force_is_scaled_in_yellow_and_closed_in_unknown(self):
        self.assertEqual(0.60, market_light_scale("yellow", "main_force_top10"))
        self.assertEqual(0.0, market_light_scale("unknown", "main_force_top10"))

    def test_unregistered_strategy_fails_closed(self):
        self.assertEqual(0.0, market_light_scale("green", "future_strategy"))


if __name__ == "__main__":
    unittest.main()
