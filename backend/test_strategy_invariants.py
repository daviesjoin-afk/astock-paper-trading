# -*- coding: utf-8 -*-
"""动态策略不变量套件（PR：dynamic strategy invariant suite）。

对**任意 N 个策略**（N=1..12，权重/席位随机化但确定性生成）断言系统级
不变式，防止"再加一个策略"破坏全局约束：

1. 总敞口不越界：Σ 席位 ≤ 硬上限，pool_limit ≤ hard_pool_cap；
2. 单票 aggregate 不越界：任一策略的视图都不超过组合聚合口径，且
   头寸+在途合计受 cap 约束；
3. 删除策略历史仍可回放：从参与集合中移除策略后，分配/指标重放仍成立；
4. 策略不能修改 T+1：T+1 不在可调设置白名单里（任何路径都改不了）；
5. stale signal 不成交：超龄/跨日信号一律不可用；
6. 预算不足一手不成交：allowance < 一手金额时 sizing 返回 0 股；
7. 风险退出永远高于新开仓：P0 sell 的优先级排序先于任何买入意图。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asymmetric_risk as AR
import entry_lifecycle as ELC
import paper_allocation as PA
import portfolio_coordinator as PCO
import runtime_settings as RSET

STRATEGY_POOL = [
    ("alpha", 0.32, 3), ("beta", 0.34, 3), ("gamma", 0.32, 3),
    ("delta", 0.30, 2), ("epsilon", 0.34, 3), ("zeta", 0.28, 2),
    ("eta", 0.30, 2), ("theta", 0.32, 3), ("iota", 0.30, 2),
    ("kappa", 0.34, 3), ("lambda", 0.28, 2), ("mu", 0.30, 2),
]


def _runtimes(count, salt=0):
    runtimes = []
    for index in range(count):
        name, weight, slots = STRATEGY_POOL[(index + salt) % len(STRATEGY_POOL)]
        runtimes.append(PA.StrategyRuntime(
            strategy_id=f"{name}{index}", base_priority=weight,
            max_positions=slots,
        ))
    return runtimes


class PoolExposureInvariantTests(unittest.TestCase):
    """不变式 1：任意 N 策略下总敞口不越界。"""

    def test_total_slots_never_exceed_the_hard_cap(self):
        for count in range(1, 13):
            for salt in (0, 3, 7):
                allocation = PA.position_limits(
                    _runtimes(count, salt), hard_pool_cap=15,
                    strategy_max_positions=6, strategy_min_positions=1,
                    protected_slot_floor=1,
                )
                # 断言必须在 salt 循环内：每个组合都要验证，不能被覆盖。
                self.assertLessEqual(
                    sum(allocation["limits"].values()), 15,
                    f"N={count} salt={salt}",
                )
                self.assertLessEqual(allocation["total_cap"], 15,
                                     f"N={count} salt={salt}")

    def test_pool_limit_stays_within_the_hard_cap_for_any_exposure(self):
        for count in range(1, 13):
            runtimes = [
                PA.StrategyRuntime(strategy_id=f"s{i}", base_priority=0.95,
                                   max_positions=6)
                for i in range(count)
            ]
            allocation = PA.position_limits(
                runtimes, hard_pool_cap=15, strategy_max_positions=6,
                strategy_min_positions=1, protected_slot_floor=1,
            )
            self.assertLessEqual(allocation["total_cap"], 15)
            self.assertTrue(all(v >= 0 for v in allocation["limits"].values()))

    def test_removed_strategy_does_not_break_the_allocation(self):
        """不变式 3：删除策略后剩余集合仍可分配（历史仍可回放）。"""
        runtimes = _runtimes(10)
        for removed in (runtimes[0], runtimes[5], runtimes[-1]):
            remaining = [r for r in runtimes if r.strategy_id != removed.strategy_id]
            allocation = PA.position_limits(
                remaining, hard_pool_cap=15, strategy_max_positions=6,
                strategy_min_positions=1, protected_slot_floor=1,
            )
            self.assertLessEqual(sum(allocation["limits"].values()), 15)
            self.assertNotIn(removed.strategy_id, allocation["limits"])

    def test_ledger_metrics_replay_for_an_unknown_account(self):
        """删除策略后指标重放不报错（空序列即可）。"""
        import strategy_champion as SCM

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, account_id TEXT,
                side TEXT, code TEXT, amount REAL, status TEXT, realized_pnl REAL,
                executed_at TEXT, created_at TEXT);
            CREATE TABLE paper_positions(account_id TEXT, code TEXT, qty INTEGER,
                cost REAL, entry_date TEXT);"""
        )
        metrics = SCM.collect_ledger_metrics(
            conn, "deleted_strategy",
            (dt.datetime.now() - dt.timedelta(days=5)).isoformat(timespec="seconds"),
            dt.datetime.now().isoformat(timespec="seconds"))
        self.assertEqual(0.0, metrics["return_pct"])


class SymbolAggregateInvariantTests(unittest.TestCase):
    """不变式 2：单票 aggregate 不越界。"""

    def test_aggregate_is_never_below_any_single_strategy_view(self):
        for count in range(2, 13):
            positions = [
                {"code": "600000", "qty": 1000, "cost": 10.0,
                 "industry": "银行", "account_id": f"s{i}"}
                for i in range(count)
            ]
            aggregate = PCO.aggregate_exposure(positions, {"600000": {"price": 10.0}})
            own_view = PCO.aggregate_exposure([positions[0]], {"600000": {"price": 10.0}})
            self.assertGreaterEqual(
                aggregate["by_symbol"]["600000"], own_view["by_symbol"]["600000"])

    def test_expansion_cannot_exceed_the_cap_no_matter_the_strategy_count(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        AR.ensure_proposals_table(conn)
        overrides = {
            f"s{i}": {"style": "sector", "max_positions": 3,
                      "max_weight_pct": 32.0, "max_exposure_pct": 92.0}
            for i in range(10)
        }
        proposed = {
            f"s{i}": dict(overrides[f"s{i}"], max_exposure_pct=95.0)
            for i in range(10)
        }
        gate = AR.validate_risk_updates(
            overrides, proposed, evidence_count=99, conn=conn)
        self.assertFalse(gate["allowed"])
        self.assertTrue(all("尚未登记" in v or "观察期" in v
                            for v in gate["violations"]))

    def test_symbol_headroom_clamps_any_number_of_pending_buys(self):
        positions = [
            {"code": "600000", "qty": 500, "cost": 10.0,
             "industry": "银行", "account_id": f"s{i}"}
            for i in range(6)
        ]
        aggregate = PCO.aggregate_exposure(
            positions, {"600000": {"price": 10.0}},
            pending_by_symbol={"600000": 5000.0})
        check = PCO.symbol_headroom("600000", aggregate, cap_amount=20000.0)
        # 30000 持仓 + 5000 在途 > 20000 上限 → 不允许再买。
        self.assertFalse(check["allowed"])


class T1ImmutableTests(unittest.TestCase):
    """不变式 4：策略不能修改 T+1。"""

    def test_t_plus_1_is_not_a_tunable_setting(self):
        with self.assertRaises(ValueError):
            RSET.validate({"t_plus_1": 0})
        with self.assertRaises(ValueError):
            RSET.validate({"settlement": "t0"})

    def test_t_plus_1_is_not_in_any_strategy_override(self):
        for strategy_id, override in RSET.STRATEGY_DEFAULTS.items():
            self.assertNotIn("t_plus_1", override)
            self.assertNotIn("settlement", override)
            for key in override:
                self.assertFalse(
                    "t+" in key.lower() or "t1" in key.lower(),
                    f"{strategy_id}.{key} 疑似 T+1 相关键",
                )


class StaleSignalInvariantTests(unittest.TestCase):
    """不变式 5：stale signal 不成交。"""

    def test_aged_signal_is_never_usable(self):
        now = dt.datetime.now()
        stale = {"created_at": (now - dt.timedelta(hours=6)).isoformat(timespec="seconds"),
                 "intended_date": now.date().isoformat()}
        self.assertFalse(ELC.signal_freshness(stale, now=now)["usable"])

    def test_cross_day_signal_is_never_usable(self):
        now = dt.datetime.now()
        stale = {"created_at": now.isoformat(timespec="seconds"),
                 "intended_date": (now - dt.timedelta(days=3)).date().isoformat()}
        self.assertFalse(ELC.signal_freshness(stale, now=now,
                                              asof_day=now.date().isoformat())["usable"])

    def test_buy_path_guards_against_stale_signals(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading.py")
        with open(path, "r", encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("ELC.signal_freshness", body)
        self.assertIn("signal_expired", body)


class MinLotInvariantTests(unittest.TestCase):
    """不变式 6：预算不足一手不成交。"""

    def test_tiny_budget_sizes_to_zero_shares(self):
        import paper_sizing as PS

        qty, sizing = PS.price_aware_qty(
            nav=100000.0, cash=100.0, position_value=0.0,
            industry_value=0.0, code_value=0.0,
            fill_price=50.0, hard_stop=0.05,
            profile={"max_weight": 0.32, "max_exposure": 0.92,
                     "single_risk": 0.012, "max_industry": 0.42,
                     "cooldown_days": 2, "min_cost_edge": 0.006},
            exposure_cap=0.82, max_exposure_cap=0.82, exposure_scale=1.0,
            strategy_position_value=0.0, strategy_cap_amount=32000.0,
            pool_cap_amount=82000.0,
            pending_strategy_amount=0.0, pending_pool_amount=0.0,
            num=lambda value, default=None: value,  # 与生产包装一致：透传数值
            single_position_max_amount=0.0,
        )
        # 100 元现金买不起一手 50 元股票 → qty 必须为 0（不允许碎股买单）。
        self.assertEqual(0, int(qty))


class IntentPriorityInvariantTests(unittest.TestCase):
    """不变式 7：风险退出永远高于新开仓。"""

    def test_p0_exit_sorts_before_any_buy_for_any_mix(self):
        intents = []
        for index in range(12):
            intents.append({"order": index, "priority": "P5", "side": "buy"})
            intents.append({"order": index + 100, "priority": "P4", "side": "buy"})
        intents.append({"order": 999, "priority": "P0", "side": "sell",
                        "purpose": "hard_stop 崩盘"})
        ordered = PCO.sort_intents_by_priority(intents)
        self.assertEqual("P0", ordered[0]["priority"])
        self.assertEqual("sell", ordered[0]["side"])

    def test_scale_in_yields_to_in_flight_risk_exit(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading.py")
        with open(path, "r", encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("PCO.pending_risk_exit_codes", body)
        self.assertIn("P0 风控退出在途", body)


if __name__ == "__main__":
    unittest.main()
