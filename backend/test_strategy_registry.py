# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_registry as registry
import adaptive_risk as adaptive_risk
import paper_trading as paper


class StrategyRegistryTests(unittest.TestCase):
    def test_active_ids_match_public_new_cycle_scope(self):
        self.assertEqual(registry.active_ids(), (
            "tq_breakout", "trend_pullback", "sector_rotation",
            "reported_profit_breakout", "main_force_top10",
        ))

    def test_all_registered_strategies_are_active_for_new_cycles(self):
        self.assertTrue(all(spec.status == "active" for spec in registry.STRATEGY_REGISTRY))
        self.assertTrue(all(spec.supports_new_cycle for spec in registry.STRATEGY_REGISTRY))

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

    def test_strategy_center_exposes_five_strategy_active_scope(self):
        rows = {row["id"]: row for row in paper.strategy_center()["strategies"]}
        self.assertEqual(set(rows), set(registry.active_ids()))
        self.assertTrue(all(row["strategy_status"] == "active" for row in rows.values()))
        self.assertTrue(all(row["supports_new_cycle"] for row in rows.values()))

    def test_clean_clone_bootstraps_five_equal_strategy_sleeves(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper.sqlite3")
            with mock.patch.object(paper, "DB_PATH", path), \
                    mock.patch.object(paper, "_benchmark_close", return_value=None), \
                    mock.patch.object(paper, "_RUNNER_BOOT_RECOVERED", False):
                paper.init_db()
                conn = sqlite3.connect(path)
                try:
                    rows = conn.execute(
                        "SELECT id,initial_cash,status FROM paper_accounts ORDER BY id"
                    ).fetchall()
                    self.assertEqual(len(rows), 5)
                    self.assertEqual({row[2] for row in rows}, {"paused"})
                    self.assertEqual({row[1] for row in rows}, {20000.0})
                    self.assertEqual(conn.execute("SELECT capital FROM paper_cycles").fetchone()[0], 100000.0)
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
