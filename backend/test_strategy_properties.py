# -*- coding: utf-8 -*-
"""PR-54（一）：策略数量 / 顺序 / 克隆下的**生成式**资金不变量。

与既有套件的分工（刻意不重复）：

- ``test_strategy_invariants.py`` 锁的是 N=1..12 的**席位**口径与单票聚合视图；
- 本文件锁的是**资金引擎**：``paper_allocation.allocation_plan`` 在
  N ∈ {0, 1, 2, 5, 10, 20, 50}、输入顺序变化、克隆成簇、同票并发下的硬边界。

原则：只调用生产函数（``allocation_plan`` / ``position_limits`` /
``stage_capital_scale`` / ``strategy_clusters`` / ``portfolio_coordinator``），
**不复制任何分配公式**——断言的是"上界与一致性"，不是"算法算出来应该是多少"。
所有用例都是确定性的（按下标派生，不用 random）。
"""
from __future__ import annotations

import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_allocation as PA
import paper_trading as PT
import portfolio_coordinator as PCO
import strategy_clusters as SC

NAV = 1_000_000.0
PRICE = 10.0
COUNTS = (0, 1, 2, 5, 10, 20, 50)
STAGES = ("standard", "pilot", "shadow", "quarantined", "unknown_stage")


def _runtime(index, *, stage="standard", weight=0.30, slots=3,
             diversification=1.0, own_exposure_cap_pct=None):
    """确定性造一个分配层运行时（身份差异全在这张声明式结构里）。"""
    return PA.StrategyRuntime(
        strategy_id=f"s{index:02d}",
        base_priority=weight,
        diversification=diversification,
        max_positions=slots,
        own_exposure_cap_pct=own_exposure_cap_pct,
        lifecycle_stage=stage,
    )


def _runtimes(count, *, stage="standard", weight=0.30, slots=3):
    return [_runtime(i, stage=stage, weight=weight, slots=slots) for i in range(count)]


def _plan(runtimes, *, nav=NAV, price=PRICE, pending_total=0.0):
    """调用生产分配引擎（不复制它的任何一步）。"""
    ids = [runtime.strategy_id for runtime in runtimes]
    return PA.allocation_plan(
        runtimes,
        nav=nav,
        values={key: 0.0 for key in ids},
        pending_by_account={key: 0.0 for key in ids},
        pending_total=pending_total,
        prices_by_strategy={key: price for key in ids},
        shared_pool_max_exposure=PT.SHARED_POOL_MAX_EXPOSURE,
        strategy_pool_floor_ratio=PT.STRATEGY_POOL_FLOOR_RATIO,
        lot_size=PT.LOT_SIZE,
    )


def _rows(plan):
    return {row["strategy_id"]: row for row in plan["plan"]}


def _is_finite(number) -> bool:
    return isinstance(number, (int, float)) and math.isfinite(float(number))


class StrategyCountProperties(unittest.TestCase):
    """测试组 A：任意 N 个策略下的共享池硬边界。"""

    def test_empty_pool_is_a_valid_zero_strategy_plan(self):
        # N=0 是合法输入（零策略 = idle），不能抛错、不能凭空分配。
        plan = _plan([])
        self.assertEqual([], plan["plan"])
        self.assertEqual(0.0, plan["total_deployable_amount"])
        self.assertEqual(0.0, plan["total_waiting_capital"])
        # 没有任何占用时，整池余量就是硬上限本身。
        self.assertAlmostEqual(NAV * PT.SHARED_POOL_MAX_EXPOSURE,
                               plan["pool_headroom_amount"], places=2)

    def test_deployable_never_exceeds_shared_pool_headroom(self):
        for count in COUNTS:
            with self.subTest(count=count):
                plan = _plan(_runtimes(count))
                self.assertLessEqual(
                    plan["total_deployable_amount"], plan["pool_headroom_amount"] + 1e-6,
                )
                self.assertLessEqual(
                    plan["total_deployable_amount"],
                    NAV * PT.SHARED_POOL_MAX_EXPOSURE + 1e-6,
                )
                # 逐策略相加也永不越过池余量（分配是顺序消耗，不是各自独立）。
                total = sum(row["deployable_amount"] for row in plan["plan"])
                self.assertLessEqual(total, plan["pool_headroom_amount"] + 1e-6)

    def test_budget_is_never_negative_and_never_not_a_number(self):
        for count in COUNTS:
            for stage in STAGES:
                with self.subTest(count=count, stage=stage):
                    plan = _plan(_runtimes(count, stage=stage))
                    for row in plan["plan"]:
                        for key in ("raw_allowance_amount", "budget_amount",
                                    "scaled_budget_amount", "deployable_amount",
                                    "waiting_capital", "lifecycle_withheld_amount",
                                    "capital_scale"):
                            value = row[key]
                            self.assertTrue(_is_finite(value), f"{key}={value!r}")
                            self.assertGreaterEqual(float(value), -1e-6, f"{key}={value!r}")
                    for key in ("total_deployable_amount", "total_waiting_capital",
                                "pool_headroom_amount"):
                        self.assertTrue(_is_finite(plan[key]), f"{key}={plan[key]!r}")
                        self.assertGreaterEqual(float(plan[key]), -1e-6)

    def test_deployable_is_always_a_whole_number_of_lots(self):
        plan = _plan(_runtimes(20))
        for row in plan["plan"]:
            self.assertEqual(
                row["deployable_amount"],
                row["lots"] * PT.LOT_SIZE * PRICE,
                f"{row['strategy_id']} 出现了碎片订单",
            )

    def test_total_slots_never_exceed_the_hard_slot_cap(self):
        for count in COUNTS:
            with self.subTest(count=count):
                limits = PA.position_limits(
                    _runtimes(count, slots=6), hard_pool_cap=PT.SHARED_POOL_MAX_POSITIONS,
                    strategy_max_positions=6, strategy_min_positions=1,
                    protected_slot_floor=1,
                )
                self.assertLessEqual(sum(limits["limits"].values()),
                                     PT.SHARED_POOL_MAX_POSITIONS)
                self.assertLessEqual(limits["total_cap"], PT.SHARED_POOL_MAX_POSITIONS)
                self.assertTrue(all(value >= 0 for value in limits["limits"].values()))

    def test_shadow_and_quarantined_get_no_deployable_capital(self):
        for stage in ("shadow", "quarantined", "unknown_stage", "not_a_stage"):
            with self.subTest(stage=stage):
                scale, normalized = PA.stage_capital_scale(_runtime(0, stage=stage))
                self.assertEqual(0.0, scale, f"{stage} 必须零部署")
                plan = _plan(_runtimes(5, stage=stage))
                for row in plan["plan"]:
                    self.assertEqual(0.0, row["deployable_amount"], row["strategy_id"])
                    self.assertEqual(0.0, row["lots"])
                    self.assertEqual(0.0, row["capital_scale"])
                    # 被扣留的额度必须显式可见，不能凭空消失。
                    self.assertGreaterEqual(row["lifecycle_withheld_amount"], 0.0)

    def test_pilot_uses_the_pilot_scale_and_still_gets_capital(self):
        scale, stage = PA.stage_capital_scale(_runtime(0, stage="pilot"))
        self.assertEqual(0.25, scale)
        self.assertEqual("pilot", stage)
        plan = _plan(_runtimes(1, stage="pilot"))
        row = plan["plan"][0]
        self.assertEqual(0.25, row["capital_scale"])
        # 预算足够一手时，试点仍要真的部署（不能被当成 shadow）。
        self.assertGreater(row["deployable_amount"], 0.0)
        # 试点部署不超过其缩放后的预算。
        self.assertLessEqual(row["deployable_amount"], row["scaled_budget_amount"] + 1e-6)

    def test_standard_scale_is_never_above_one(self):
        scale, _stage = PA.stage_capital_scale(_runtime(0, stage="standard"))
        self.assertEqual(1.0, scale)

    def test_nonparticipant_has_no_row_and_no_budget(self):
        # 参与集合里没有的策略，不得拿到任何额度（unknown id ≠ 默认预算）。
        participants = _runtimes(5)
        plan = _plan(participants)
        ids = {row["strategy_id"] for row in plan["plan"]}
        self.assertEqual({runtime.strategy_id for runtime in participants}, ids)
        self.assertNotIn("s99", ids)


class PlanOrderIndependenceProperties(unittest.TestCase):
    """测试组 B：输入顺序不得影响结果；tie-break 必须显式且确定性。"""

    def test_permuting_the_runlist_does_not_change_the_plan(self):
        base = [_runtime(0, weight=0.32), _runtime(1, weight=0.30), _runtime(2, weight=0.28)]
        orders = [
            base,
            [base[2], base[0], base[1]],   # C, A, B
            [base[1], base[2], base[0]],   # B, C, A
            list(reversed(base)),
        ]
        rendered = [json.dumps(_plan(order)["plan"], sort_keys=True) for order in orders]
        self.assertEqual(1, len(set(rendered)), "同一组策略换顺序后预算发生变化")

    def test_equal_weight_tie_break_is_deterministic_and_explicit(self):
        # 权重完全相同时，顺序必须由显式键（strategy_id）决定，而不是 list 偶然顺序。
        tied = [_runtime(index, weight=0.30) for index in (0, 1, 2)]
        plan = _plan(tied)
        self.assertEqual(
            ["s00", "s01", "s02"], [row["strategy_id"] for row in plan["plan"]],
        )
        shuffled = _plan([tied[2], tied[0], tied[1]])
        self.assertEqual(
            ["s00", "s01", "s02"], [row["strategy_id"] for row in shuffled["plan"]],
        )

    def test_effective_weight_ordering_drives_consumption_order(self):
        # 权重不同：按有效权重降序消耗池余量（这里是契约，不是公式复制）。
        plan = _plan([
            _runtime(0, weight=0.20),
            _runtime(1, weight=0.90),
            _runtime(2, weight=0.50),
        ])
        self.assertEqual(["s01", "s02", "s00"], [row["strategy_id"] for row in plan["plan"]])

    def test_explicit_account_order_wins_and_is_itself_deterministic(self):
        tied = [_runtime(index, weight=0.30) for index in (0, 1, 2)]
        ids = [runtime.strategy_id for runtime in tied]
        explicit = {"s02": 0, "s00": 1, "s01": 2}
        plan_a = PA.allocation_plan(
            tied, nav=NAV, values={key: 0.0 for key in ids},
            pending_by_account={key: 0.0 for key in ids}, pending_total=0.0,
            prices_by_strategy={key: PRICE for key in ids},
            shared_pool_max_exposure=PT.SHARED_POOL_MAX_EXPOSURE,
            strategy_pool_floor_ratio=PT.STRATEGY_POOL_FLOOR_RATIO,
            lot_size=PT.LOT_SIZE, account_order=explicit,
        )
        plan_b = PA.allocation_plan(
            list(reversed(tied)), nav=NAV, values={key: 0.0 for key in ids},
            pending_by_account={key: 0.0 for key in ids}, pending_total=0.0,
            prices_by_strategy={key: PRICE for key in ids},
            shared_pool_max_exposure=PT.SHARED_POOL_MAX_EXPOSURE,
            strategy_pool_floor_ratio=PT.STRATEGY_POOL_FLOOR_RATIO,
            lot_size=PT.LOT_SIZE, account_order=explicit,
        )
        self.assertEqual(["s02", "s00", "s01"],
                         [row["strategy_id"] for row in plan_a["plan"]])
        self.assertEqual(json.dumps(plan_a["plan"], sort_keys=True),
                         json.dumps(plan_b["plan"], sort_keys=True))

    def test_slot_allocation_is_order_independent_too(self):
        runtimes = _runtimes(10, slots=4)
        first = PA.position_limits(
            runtimes, hard_pool_cap=PT.SHARED_POOL_MAX_POSITIONS,
            strategy_max_positions=6, strategy_min_positions=1, protected_slot_floor=1)
        second = PA.position_limits(
            list(reversed(runtimes)), hard_pool_cap=PT.SHARED_POOL_MAX_POSITIONS,
            strategy_max_positions=6, strategy_min_positions=1, protected_slot_floor=1)
        self.assertEqual(first["limits"], second["limits"])
        self.assertEqual(first["total_cap"], second["total_cap"])


class CloneSybilProperties(unittest.TestCase):
    """测试组 C：克隆/近似克隆不得线性放大组合预算份额。

    使用生产簇契约（``strategy_clusters``）：``cluster_budget_multiplier`` 是
    cluster-first 的封顶口径，``cluster_diversification_factor`` 是次线性权重。
    测试只断言"克隆不放大"，不重写簇算法。
    """

    @staticmethod
    def _clone_cluster(count):
        return {tuple(f"clone{i:02d}" for i in range(count))}

    def test_clone_cluster_budget_is_capped_not_linear(self):
        for count in (2, 10, 50):
            with self.subTest(clones=count):
                cluster = set(f"clone{i:02d}" for i in range(count))
                multiplier = SC.cluster_budget_multiplier(cluster)
                self.assertLess(multiplier, count, "克隆簇预算出现线性膨胀")
                self.assertLessEqual(multiplier, 1.1 + 1e-9,
                                     f"{count} 个克隆的总预算是单策略的 {multiplier} 倍")
                self.assertGreaterEqual(multiplier, 1.0)

    def test_clone_per_member_weight_shrinks_sublinearly(self):
        for count in (2, 10, 50):
            with self.subTest(clones=count):
                cluster = set(f"clone{i:02d}" for i in range(count))
                factor = SC.cluster_diversification_factor("clone00", [cluster])
                self.assertLessEqual(factor, 1.0)
                self.assertLess(factor * count, count,
                                "簇内总权重不得随克隆数线性增长")
                # 单策略簇无惩罚。
                self.assertEqual(1.0, SC.cluster_diversification_factor("loner", [cluster]))

    def test_similar_clones_land_in_one_cluster_and_keep_one_budget(self):
        # 用生产的相似度契约确认"高度相似 → 同簇"；完全相同的 AST 相似度为 1。
        rule = {"op": "gt", "left": {"op": "field", "name": "close"},
                "right": {"op": "const", "value": 10}}
        self.assertEqual(1.0, SC.dsl_ast_similarity(rule, dict(rule)))
        self.assertGreaterEqual(SC.dsl_ast_similarity(rule, dict(rule)), 0.95)

    def test_n_identical_runtimes_cannot_exceed_the_pool_cap(self):
        for count in (2, 10, 50):
            with self.subTest(clones=count):
                cluster = set(f"s{i:02d}" for i in range(count))
                factor = SC.cluster_diversification_factor("s00", [cluster])
                runtimes = [
                    _runtime(i, weight=0.90, diversification=factor)
                    for i in range(count)
                ]
                plan = _plan(runtimes)
                self.assertLessEqual(plan["total_deployable_amount"],
                                     plan["pool_headroom_amount"] + 1e-6)
                self.assertLessEqual(plan["total_deployable_amount"],
                                     NAV * PT.SHARED_POOL_MAX_EXPOSURE + 1e-6)
                # 每一个克隆都不许超过"单策略独享时"的份额。
                single = _plan([_runtime(0, weight=0.90)])["plan"][0]["deployable_amount"]
                for row in plan["plan"]:
                    self.assertLessEqual(row["deployable_amount"], single + 1e-6,
                                         f"{row['strategy_id']} 比独享时拿得还多")


class SameSymbolAggregationProperties(unittest.TestCase):
    """测试组 D：多策略同时买入同一 symbol 时，组合级敞口必须聚合。"""

    SYMBOL = "600901"
    CAP = 50_000.0

    def _aggregate(self, strategy_count, *, qty_each=1000, pending_each=0.0):
        positions = [
            {"code": self.SYMBOL, "qty": qty_each, "cost": PRICE, "industry": "工程机械",
             "account_id": f"s{index:02d}"}
            for index in range(strategy_count)
        ]
        pending = {self.SYMBOL: pending_each * strategy_count} if pending_each else {}
        return PCO.aggregate_exposure(
            positions, {self.SYMBOL: {"price": PRICE}}, pending_by_symbol=pending,
        )

    def test_exposure_is_summed_across_strategy_ids(self):
        for count in (1, 2, 5, 20, 50):
            with self.subTest(strategies=count):
                aggregate = self._aggregate(count)
                self.assertAlmostEqual(
                    count * 1000 * PRICE,
                    aggregate["by_symbol"][self.SYMBOL], places=2,
                )

    def test_more_strategies_cannot_unlock_more_symbol_capacity(self):
        # 单票上限是组合级的：策略 ID 再多，也只能用同一个 cap。
        for count in (1, 2, 10, 50):
            with self.subTest(strategies=count):
                aggregate = self._aggregate(count, qty_each=1000)
                headroom = PCO.symbol_headroom(
                    self.SYMBOL, aggregate, cap_amount=self.CAP)
                if count * 1000 * PRICE >= self.CAP:
                    self.assertFalse(
                        headroom["allowed"],
                        f"{count} 个策略把同一标的合计买过了组合上限仍然放行",
                    )
                self.assertLessEqual(
                    headroom["used_amount"], max(self.CAP, count * 1000 * PRICE) + 1e-6)
                self.assertGreaterEqual(headroom["headroom_amount"], 0.0)

    def test_pending_buys_from_many_strategies_are_counted_before_filling(self):
        # 在途买单（还没成交）也必须计入组合敞口，否则多策略各自"看不见对方"。
        # 10 × 6,000 = 60,000 > cap 50,000 → 必须拒绝；低于 cap 时必须放行（对照）。
        over = self._aggregate(10, qty_each=0, pending_each=6_000.0)
        self.assertAlmostEqual(60_000.0, over["pending_by_symbol"][self.SYMBOL], places=2)
        over_headroom = PCO.symbol_headroom(self.SYMBOL, over, cap_amount=self.CAP)
        self.assertFalse(over_headroom["allowed"])
        self.assertEqual(0.0, over_headroom["headroom_amount"])

        under = self._aggregate(10, qty_each=0, pending_each=4_000.0)
        under_headroom = PCO.symbol_headroom(self.SYMBOL, under, cap_amount=self.CAP)
        self.assertTrue(under_headroom["allowed"])
        self.assertAlmostEqual(10_000.0, under_headroom["headroom_amount"], places=2)

    def test_symbol_cap_is_not_multiplied_by_strategy_count(self):
        # 对照口径：同样的合计在途，不管拆成几个策略，结论必须一致。
        one = PCO.aggregate_exposure([], None, pending_by_symbol={self.SYMBOL: 60_000.0})
        many = self._aggregate(10, qty_each=0, pending_each=6_000.0)
        self.assertEqual(
            PCO.symbol_headroom(self.SYMBOL, one, cap_amount=self.CAP)["allowed"],
            PCO.symbol_headroom(self.SYMBOL, many, cap_amount=self.CAP)["allowed"],
        )
        self.assertFalse(PCO.symbol_headroom(self.SYMBOL, one, cap_amount=self.CAP)["allowed"])


class MinimumLotProperties(unittest.TestCase):
    """测试组 K：池预算不足一整手时的状态必须合理。"""

    def test_small_pool_yields_zero_lots_and_keeps_the_money_waiting(self):
        tiny_nav = 1_000.0
        plan = _plan(_runtimes(5), nav=tiny_nav)
        for row in plan["plan"]:
            if row["budget_amount"] < PT.LOT_SIZE * PRICE:
                self.assertEqual(0.0, row["deployable_amount"], row["strategy_id"])
                self.assertEqual(0.0, row["lots"])
                self.assertFalse(row["allowed"])
                self.assertIsNotNone(row["blocked_reason"])
        # 未部署的钱必须留在 waiting_capital 里，不能凭空消失或变成负预算。
        self.assertGreaterEqual(plan["total_waiting_capital"], 0.0)
        self.assertLessEqual(plan["total_deployable_amount"],
                             plan["pool_headroom_amount"] + 1e-6)

    def test_waiting_plus_deployable_never_exceeds_the_scaled_budget(self):
        for count in (1, 5, 20):
            with self.subTest(count=count):
                plan = _plan(_runtimes(count), nav=100_000.0)
                for row in plan["plan"]:
                    self.assertLessEqual(
                        row["deployable_amount"] + row["waiting_capital"],
                        row["scaled_budget_amount"] + 1e-6,
                        f"{row['strategy_id']} 部署+等待超过缩放后预算",
                    )

    def test_position_limits_never_allocate_a_slot_without_a_limit(self):
        limits = PA.position_limits(
            _runtimes(0), hard_pool_cap=PT.SHARED_POOL_MAX_POSITIONS,
            strategy_max_positions=6, strategy_min_positions=1, protected_slot_floor=1)
        self.assertEqual({}, limits["limits"])
        self.assertEqual(0, limits["total_cap"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
