# -*- coding: utf-8 -*-
"""PR-41：剩余策略键控常量表迁移到 strategy_policies 的等价回归。

背景：PR-37 已把 EntryPolicy / ReviewPolicy / 结构性 CooldownPolicy 迁到
``strategy_policies``（声明式画像模块）。PR-41 继续迁走最后三张按
``account_id`` 键控的表与一组身份特判分支：

1. RotationPolicy：``POSITION_REVIEW_MIN_HOLD_DAYS(_BY_STRATEGY)``
   → ``SPOL.position_review_min_hold_days``；
2. CooldownPolicy：``RISK_REJECT_COOLDOWN_AFTER_TWO_MINUTES``
   → ``SPOL.risk_reject_cooldown_minutes``；
3. RecoveryPolicy：``RECOVERY_WATCH_STATUS`` / ``RECOVERY_POLICIES``
   → ``SPOL.recovery_policy``；
4. EntryEconomicsPolicy：``TQ_MIN_EFFECTIVE_ENTRY_AMOUNT`` /
   ``TQ_MIN_EXPECTED_EDGE_PCT`` → ``SPOL.entry_economics_policy``，
   并删除 ``_buy_order`` 里 ``account["id"] == "tq_breakout"`` 的
   碎单经济性身份特判（改为画像驱动，未声明账户不启用该门槛）。

本测试锁住三件事：
1. 逐策略值等价：访问器返回值与历史常量逐项相等（回归锚点）；
2. fail-closed 语义：未声明账户得到历史默认（min_hold_days=2、
   cooldown=0、recovery 回退 trend_pullback、economics 不启用）；
3. 源码门禁：引擎不再定义这些表，``_buy_order`` 不再按策略名特判
   碎单经济性；引擎级全局上限仍留在 paper_trading。
"""
from __future__ import annotations

import os
import re
import unittest

import paper_trading as PT
import strategy_policies as SPOL

BACKEND = os.path.dirname(os.path.abspath(__file__))

# 内置五套 + 一个未声明账户（fail-closed 语义探针）。
BUILTIN_IDS = ("tq_breakout", "trend_pullback", "sector_rotation",
               SPOL.NEW_STRATEGY_ID, SPOL.MAIN_FORCE_STRATEGY_ID)
UNKNOWN_ID = "unknown_user_strategy_alpha"

# 历史常量（PR-41 迁移前的 paper_trading.py 值，逐字抄录作为回归锚点）。
EXPECTED_MIN_HOLD_DAYS = {
    "tq_breakout": 1, "trend_pullback": 2, "sector_rotation": 0,
    SPOL.NEW_STRATEGY_ID: 1, SPOL.MAIN_FORCE_STRATEGY_ID: 1,
}
EXPECTED_RISK_REJECT_COOLDOWN = {
    "tq_breakout": 30, "trend_pullback": 75, "sector_rotation": 45,
    SPOL.NEW_STRATEGY_ID: 90, SPOL.MAIN_FORCE_STRATEGY_ID: 45,
}
EXPECTED_RECOVERY = {
    "tq_breakout": {"min_scans": 2, "cooldown_minutes": 15, "reclaim_pct": 0.005, "max_days": 1},
    "trend_pullback": {"min_scans": 2, "cooldown_minutes": 60, "reclaim_pct": 0.008, "max_days": 3},
    "sector_rotation": {"min_scans": 3, "cooldown_minutes": 60, "reclaim_pct": 0.010, "max_days": 2},
    SPOL.NEW_STRATEGY_ID: {"min_scans": 2, "cooldown_minutes": 45, "reclaim_pct": 0.008, "max_days": 2},
    SPOL.MAIN_FORCE_STRATEGY_ID: {"min_scans": 2, "cooldown_minutes": 45, "reclaim_pct": 0.010, "max_days": 2},
}
EXPECTED_ENTRY_ECONOMICS = {
    "tq_breakout": {"min_effective_order_amount": 4_000.0, "min_expected_edge_pct": 0.008},
}


def _engine_source() -> str:
    with open(os.path.join(BACKEND, "paper_trading.py"), encoding="utf-8") as handle:
        return handle.read()


class RotationPolicyEquivalenceTests(unittest.TestCase):
    """RotationPolicy：主动换仓观察窗口逐策略等价。"""

    def test_builtin_values_match_history(self):
        for account_id in BUILTIN_IDS:
            self.assertEqual(
                SPOL.position_review_min_hold_days(account_id),
                EXPECTED_MIN_HOLD_DAYS[account_id], account_id,
            )

    def test_unknown_account_falls_back_to_conservative_default(self):
        self.assertEqual(SPOL.position_review_min_hold_days(UNKNOWN_ID), 2)
        self.assertEqual(SPOL.position_review_min_hold_days(None), 2)
        self.assertEqual(SPOL.position_review_min_hold_days(""), 2)

    def test_engine_helper_delegates_to_policy(self):
        for account_id in BUILTIN_IDS + (UNKNOWN_ID, None):
            self.assertEqual(
                PT._replacement_min_hold_days(account_id),
                SPOL.position_review_min_hold_days(account_id), account_id,
            )


class RiskRejectCooldownEquivalenceTests(unittest.TestCase):
    """CooldownPolicy：两次同日风控拒绝后的递进冷却逐策略等价。"""

    def test_builtin_values_match_history(self):
        for account_id in BUILTIN_IDS:
            self.assertEqual(
                SPOL.risk_reject_cooldown_minutes(account_id),
                EXPECTED_RISK_REJECT_COOLDOWN[account_id], account_id,
            )

    def test_unknown_account_has_no_cooldown(self):
        self.assertEqual(SPOL.risk_reject_cooldown_minutes(UNKNOWN_ID), 0)
        self.assertEqual(SPOL.risk_reject_cooldown_minutes(None), 0)

    def test_engine_caps_stay_in_engine(self):
        # 封顶值/采样窗口是执行引擎的全局常量，不属于任何策略画像。
        self.assertEqual(PT.RISK_REJECT_COOLDOWN_MAX_MINUTES, 240)
        self.assertEqual(PT.RISK_REJECT_COOLDOWN_MAX_SAMPLES, 8)


class RecoveryPolicyEquivalenceTests(unittest.TestCase):
    """RecoveryPolicy：保护性退出后的受控恢复观察逐策略等价。"""

    def test_builtin_values_match_history(self):
        for account_id in BUILTIN_IDS:
            self.assertEqual(SPOL.recovery_policy(account_id), EXPECTED_RECOVERY[account_id], account_id)

    def test_unknown_account_falls_back_to_trend_pullback_template(self):
        self.assertEqual(SPOL.recovery_policy(UNKNOWN_ID), EXPECTED_RECOVERY["trend_pullback"])
        self.assertEqual(SPOL.recovery_policy(None), EXPECTED_RECOVERY["trend_pullback"])

    def test_returned_dict_is_a_copy(self):
        policy = SPOL.recovery_policy("tq_breakout")
        policy["min_scans"] = 99
        self.assertEqual(SPOL.recovery_policy("tq_breakout")["min_scans"], 2)
        self.assertEqual(SPOL.RECOVERY_POLICIES["tq_breakout"]["min_scans"], 2)

    def test_watch_status_alias_consistent(self):
        self.assertEqual(SPOL.RECOVERY_WATCH_STATUS, "recovery_watch")
        self.assertEqual(PT.RECOVERY_WATCH_STATUS, SPOL.RECOVERY_WATCH_STATUS)
        # 引擎辅助函数与访问器逐账户一致。
        for account_id in BUILTIN_IDS + (UNKNOWN_ID,):
            self.assertEqual(PT._recovery_policy(account_id), SPOL.recovery_policy(account_id), account_id)


class EntryEconomicsEquivalenceTests(unittest.TestCase):
    """EntryEconomicsPolicy：碎单经济性门槛等价 + fail-closed。"""

    def test_tq_breakout_values_match_history(self):
        econ = SPOL.entry_economics_policy("tq_breakout")
        for key, value in EXPECTED_ENTRY_ECONOMICS["tq_breakout"].items():
            self.assertEqual(econ.get(key), value, key)

    def test_undeclared_accounts_do_not_enable_threshold(self):
        # 历史行为：仅 tq_breakout 启用碎单门槛；其余账户（含用户策略）
        # 返回空 dict，引擎不得对它们做金额/边际拒绝。
        for account_id in ("trend_pullback", "sector_rotation", SPOL.NEW_STRATEGY_ID,
                           SPOL.MAIN_FORCE_STRATEGY_ID, UNKNOWN_ID, None):
            self.assertEqual(SPOL.entry_economics_policy(account_id), {}, account_id)

    def test_returned_dict_is_a_copy(self):
        econ = SPOL.entry_economics_policy("tq_breakout")
        econ["min_effective_order_amount"] = 1.0
        self.assertEqual(SPOL.entry_economics_policy("tq_breakout")["min_effective_order_amount"], 4_000.0)


class PolicyMigrationSourceGuardTests(unittest.TestCase):
    """源码门禁：引擎不再定义策略键控表，不再按策略名特判碎单经济性。"""

    def test_engine_no_longer_defines_migrated_tables(self):
        source = _engine_source()
        for pattern in (
            r"^POSITION_REVIEW_MIN_HOLD_DAYS(_BY_STRATEGY)?\s*=",
            r"^RISK_REJECT_COOLDOWN_AFTER_TWO_MINUTES\s*=",
            r"^RECOVERY_POLICIES\s*=",
            r'^RECOVERY_WATCH_STATUS\s*=\s*"',
            r"^TQ_MIN_(EFFECTIVE_ENTRY_AMOUNT|EXPECTED_EDGE_PCT)\s*=",
        ):
            self.assertIsNone(
                re.search(pattern, source, flags=re.MULTILINE), pattern,
            )

    def test_buy_order_economics_is_policy_driven(self):
        source = _engine_source()
        # 碎单经济性分支改为画像驱动：读 EntryEconomicsPolicy，且分支体
        # 内不再出现 tq_breakout 身份比较。
        self.assertIn("SPOL.entry_economics_policy(account[\"id\"])", source)
        econ_region = source.split("SPOL.entry_economics_policy(account[\"id\"])", 1)[1][:2200]
        self.assertNotIn('== "tq_breakout"', econ_region)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
