# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_registry as registry


class StrategyRegistryTests(unittest.TestCase):
    def test_active_ids_match_public_new_cycle_scope(self):
        self.assertEqual(registry.active_ids(), ("tq_breakout", "main_force_top10"))

    def test_legacy_strategies_remain_registered_for_replay(self):
        self.assertEqual(registry.get("trend_pullback").status, "legacy")
        self.assertEqual(registry.get("sector_rotation").status, "legacy")
        self.assertEqual(registry.get("reported_profit_breakout").status, "legacy")

    def test_labels_returns_a_copy_without_mutating_registry(self):
        labels = registry.labels()
        labels["tq_breakout"] = "changed"
        self.assertEqual(registry.get("tq_breakout").name, "短线日内做T")
        self.assertIsNone(registry.get("unknown"))


if __name__ == "__main__":
    unittest.main()
