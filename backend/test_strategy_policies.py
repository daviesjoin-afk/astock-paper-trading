# -*- coding: utf-8 -*-
"""PR-37：固定五套策略表 → 声明式画像（strategy_policies）。

验收：
- 内置五套的 EntryPolicy/ReviewPolicy/CooldownPolicy 只在
  strategy_policies 声明，paper_trading.py 不再拥有任何字面量表；
- 未声明账户 fail-closed：开盘事件引擎不介入、无下行守卫阶梯、
  无结构性冷却（退出保护由编译 Risk Profile 兜底）；
- 内置策略标识常量值不变（多个模块从 paper_trading 反向导入）；
- 新增一个完全新的 user strategy 不需要修改 paper_trading.py——
  该性质由 test_production_path_golden_replay 与
  test_strategy_archive_replay 的全链路回放直接证明（用户策略
  交易/退出全程未触碰任何策略表），本测试固化声明侧的前提。
"""
from __future__ import annotations

import os
import unittest

import paper_trading as PT
import strategy_policies as SPOL

OPENING_EVENT_REQUIRED_KEYS = {
    "name", "enabled", "min_peak_pct", "min_retrace_pct", "min_current_pct",
    "min_peak_edge_pct", "trim_ratio", "allow_loss_trim", "rebuy_rebound_pct",
    "rebuy_max_sold_ratio", "rebuy_min_observations", "rebuy_min_main_pct",
    "rebuy_min_current_pct",
}
DOWNSIDE_REQUIRED_KEYS = {
    "warning_pct", "partial_pct", "full_pct", "relative_pct",
    "peak_retrace_pct", "partial_ratio",
}


class DeclarativePolicyTests(unittest.TestCase):
    def test_builtin_opening_event_policies_are_complete(self):
        self.assertEqual(
            set(SPOL.OPENING_EVENT_POLICIES),
            {"tq_breakout", "trend_pullback", "sector_rotation",
             SPOL.NEW_STRATEGY_ID, SPOL.MAIN_FORCE_STRATEGY_ID},
        )
        for account_id, policy in SPOL.OPENING_EVENT_POLICIES.items():
            missing = OPENING_EVENT_REQUIRED_KEYS - set(policy)
            self.assertFalse(missing, f"{account_id} 缺少 EntryPolicy 键: {missing}")
            self.assertTrue(policy["enabled"], account_id)
            self.assertGreater(policy["trim_ratio"], 0)

    def test_builtin_downside_policies_are_complete(self):
        self.assertEqual(
            set(SPOL.INTRADAY_DOWNSIDE_POLICIES),
            {"tq_breakout", "trend_pullback", "sector_rotation",
             SPOL.NEW_STRATEGY_ID, SPOL.MAIN_FORCE_STRATEGY_ID},
        )
        for account_id, policy in SPOL.INTRADAY_DOWNSIDE_POLICIES.items():
            missing = DOWNSIDE_REQUIRED_KEYS - set(policy)
            self.assertFalse(missing, f"{account_id} 缺少 ReviewPolicy 键: {missing}")
            self.assertLess(policy["full_pct"], policy["partial_pct"])
            self.assertLess(policy["partial_pct"], 0)

    def test_builtin_cooldowns_are_positive(self):
        for account_id, minutes in SPOL.BOOTSTRAP_STRUCTURAL_RECHECK_COOLDOWN_MINUTES.items():
            self.assertGreater(int(minutes), 0, account_id)

    def test_unknown_accounts_fail_closed(self):
        # 开盘事件引擎不介入；无下行守卫阶梯；无结构性冷却。
        self.assertEqual(SPOL.opening_event_policy("some_user_strategy"), {})
        self.assertEqual(SPOL.intraday_downside_policy("some_user_strategy"), {})
        self.assertEqual(SPOL.bootstrap_recheck_cooldown_minutes("some_user_strategy"), 0)
        self.assertEqual(SPOL.opening_event_policy(None), {})
        self.assertEqual(SPOL.bootstrap_recheck_cooldown_minutes(None), 0)

    def test_declared_values_are_unchanged(self):
        # 逐字搬迁的抽查锚点：迁移不得悄悄改动任何阈值。
        tq = SPOL.opening_event_policy("tq_breakout")
        self.assertEqual(tq["trim_ratio"], 0.30)
        self.assertEqual(tq["min_peak_pct"], 3.0)
        self.assertTrue(tq["allow_loss_trim"])
        down = SPOL.intraday_downside_policy(SPOL.MAIN_FORCE_STRATEGY_ID)
        self.assertEqual(down["partial_ratio"], 0.50)
        self.assertEqual(down["full_pct"], -5.0)
        self.assertEqual(SPOL.bootstrap_recheck_cooldown_minutes("trend_pullback"), 12)


class PaperTradingOwnershipTests(unittest.TestCase):
    def test_paper_trading_no_longer_owns_policy_tables(self):
        for table in ("OPENING_EVENT_POLICIES", "INTRADAY_DOWNSIDE_POLICIES",
                      "BOOTSTRAP_STRUCTURAL_RECHECK_COOLDOWN_MINUTES"):
            self.assertFalse(hasattr(PT, table), f"paper_trading 仍持有 {table}")

    def test_identity_constants_are_unchanged(self):
        # 其他模块从 paper_trading 反向导入这两个标识，值必须稳定。
        self.assertEqual(PT.NEW_STRATEGY_ID, "reported_profit_breakout")
        self.assertEqual(PT.MAIN_FORCE_STRATEGY_ID, "main_force_top10")
        self.assertEqual(PT.NEW_STRATEGY_ID, SPOL.NEW_STRATEGY_ID)

    def test_call_sites_go_through_accessors(self):
        source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "paper_trading.py"), encoding="utf-8").read()
        self.assertNotIn("OPENING_EVENT_POLICIES", source)
        self.assertNotIn("INTRADAY_DOWNSIDE_POLICIES", source)
        self.assertNotIn("BOOTSTRAP_STRUCTURAL_RECHECK_COOLDOWN_MINUTES", source)
        self.assertIn("SPOL.opening_event_policy(", source)
        self.assertIn("SPOL.intraday_downside_policy(", source)
        self.assertIn("SPOL.bootstrap_recheck_cooldown_minutes(", source)

    def test_user_strategies_stay_out_of_declaration_tables(self):
        # 用户策略永远不出现在声明表里（编译管线派生，而非登记制）。
        self.assertNotIn("golden_replay_alpha", SPOL.OPENING_EVENT_POLICIES)
        self.assertNotIn("arch_replay_beta", SPOL.INTRADAY_DOWNSIDE_POLICIES)


if __name__ == "__main__":
    unittest.main()
