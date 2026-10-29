# -*- coding: utf-8 -*-
import os
import shutil
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
    """注册表契约测试。

    2026-10-29：本文件原先直接读仓库里的 ``data_cache/paper_trading.sqlite3``，
    于是本地账本里多出来的策略定义（实测 ``golden_replay_alpha`` 一行）会让
    ``active_ids()`` 与 ``strategy_center()`` 多一项，两条断言在干净克隆上绿、
    在开发机上红——非 hermetic。现在整类把注册表与账本都指向临时库并初始化，
    断言的语义（干净克隆只有三个策略袖套）与原来完全一致。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-registry-test-")
        cls._db = os.path.join(cls._tmp, "paper.sqlite3")
        cls._patches = [
            mock.patch.object(registry, "DEFAULT_DB_PATH", cls._db),
            mock.patch.object(paper, "DB_PATH", cls._db),
            mock.patch.object(paper, "_benchmark_close", return_value=None),
            mock.patch.object(paper, "_RUNNER_BOOT_RECOVERED", False),
        ]
        for patcher in cls._patches:
            patcher.start()
        paper.init_db()

    @classmethod
    def tearDownClass(cls):
        for patcher in reversed(cls._patches):
            patcher.stop()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_active_ids_match_public_new_cycle_scope(self):
        self.assertEqual(registry.active_ids(), (
            "tq_breakout", "trend_pullback", "reported_profit_breakout",
        ))

    def test_all_registered_strategies_are_active_for_new_cycles(self):
        self.assertTrue(all(spec.status == "paper" for spec in registry.STRATEGY_REGISTRY))
        self.assertTrue(all(spec.supports_new_cycle for spec in registry.STRATEGY_REGISTRY))

    def test_labels_returns_a_copy_without_mutating_registry(self):
        labels = registry.labels()
        labels["tq_breakout"] = "changed"
        # 注册表可能从本地账本读名字，这里只锁"返回值是副本、不污染真相"。
        self.assertNotEqual(registry.labels()["tq_breakout"], "changed")
        self.assertIn("tq_breakout", registry.labels())
        self.assertIsNone(registry.get("unknown"))

    def test_adaptive_risk_labels_are_derived_from_registry(self):
        labels = registry.labels()
        self.assertEqual(adaptive_risk.ACCOUNT_NAMES, {
            account_id: labels.get(account_id, account_id) for account_id in adaptive_risk.BASE_RISK
        })
        self.assertIn("main_force_top10", adaptive_risk.BASE_RISK)
        self.assertIn("main_force_top10", adaptive_risk.DOWNSIDE_BASE)
        current, _, _ = adaptive_risk._current_risk({"id": "main_force_top10", "params": "{}"})
        self.assertEqual(-5.0, current["downside_full_pct"])

    def test_strategy_center_exposes_three_strategy_active_scope(self):
        rows = {row["id"]: row for row in paper.strategy_center()["strategies"]}
        self.assertEqual(set(rows), set(registry.active_ids()))
        self.assertTrue(all(row["strategy_status"] == "paper" for row in rows.values()))
        self.assertTrue(all(row["supports_new_cycle"] for row in rows.values()))

    def test_clean_clone_bootstraps_only_three_strategy_sleeves(self):
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
                    self.assertEqual(len(rows), 3)
                    self.assertEqual({row[2] for row in rows}, {"paused"})
                    self.assertAlmostEqual(sum(row[1] for row in rows), 100000.0)
                    self.assertEqual(conn.execute("SELECT capital FROM paper_cycles").fetchone()[0], 100000.0)
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
