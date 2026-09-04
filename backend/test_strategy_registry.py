# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_registry as registry
import adaptive_risk as adaptive_risk
import paper_trading as paper


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

    def test_adaptive_risk_labels_are_derived_from_registry(self):
        labels = registry.labels()
        self.assertEqual(adaptive_risk.ACCOUNT_NAMES, {
            account_id: labels[account_id] for account_id in adaptive_risk.BASE_RISK
        })
        self.assertIn("main_force_top10", adaptive_risk.BASE_RISK)
        self.assertIn("main_force_top10", adaptive_risk.DOWNSIDE_BASE)
        current, _, _ = adaptive_risk._current_risk({"id": "main_force_top10", "params": "{}"})
        self.assertEqual(-5.0, current["downside_full_pct"])

    def test_strategy_center_exposes_registry_status_without_changing_account_scope(self):
        rows = {row["id"]: row for row in paper.strategy_center()["strategies"]}
        self.assertEqual(rows["tq_breakout"]["strategy_status"], "active")
        self.assertTrue(rows["main_force_top10"]["supports_new_cycle"])
        self.assertEqual(rows["trend_pullback"]["strategy_status"], "legacy")


if __name__ == "__main__":
    unittest.main()
