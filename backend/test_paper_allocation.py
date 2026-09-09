# -*- coding: utf-8 -*-
"""paper_allocation（N 策略分配引擎 v2，PR-07）的回归与 property 测试。"""
import unittest

import paper_allocation as allocation


def _runtime(strategy_id, **kwargs):
    return allocation.StrategyRuntime(strategy_id=strategy_id, **kwargs)


class StrategyRuntimeTests(unittest.TestCase):
    def test_effective_weight_is_product_of_clamped_factors(self):
        runtime = _runtime(
            "a", base_priority=0.8, regime_fit=0.5, confidence=2.0,
            health=float("nan"), data_quality=-1.0, diversification=1.0,
        )
        # 越界因子分别夹到 1.0 / 1.0 / 0.0，NaN 回到中性 1.0。
        self.assertEqual(0.8 * 0.5 * 1.0 * 1.0 * 0.0 * 1.0, runtime.effective_weight())

    def test_default_runtime_is_neutral(self):
        self.assertEqual(1.0, _runtime("a").effective_weight())

    def test_duplicate_ids_keep_the_first_runtime(self):
        weights = allocation.effective_weights(
            [_runtime("a", base_priority=0.9), _runtime("a", base_priority=0.1),
             _runtime("b", base_priority=0.5)]
        )
        self.assertEqual({"a": 0.9, "b": 0.5}, weights)


class DiversificationTests(unittest.TestCase):
    def test_no_penalty_when_within_fair_share(self):
        self.assertEqual(1.0, allocation.diversification_factor(
            exposure_share=0.2, fair_share=0.25))

    def test_penalty_grows_with_over_concentration(self):
        mild = allocation.diversification_factor(exposure_share=0.5, fair_share=0.25)
        heavy = allocation.diversification_factor(exposure_share=0.9, fair_share=0.25)
        self.assertEqual(0.5, mild)
        # 0.9 的超额被 0.5 的下限截住，但不会高于 mild。
        self.assertLessEqual(heavy, mild)
        self.assertGreaterEqual(heavy, 0.5)

    def test_zero_fair_share_is_neutral(self):
        self.assertEqual(1.0, allocation.diversification_factor(
            exposure_share=0.9, fair_share=0.0))


class PositionLimitsTests(unittest.TestCase):
    def test_five_strategy_baseline_matches_legacy_shape(self):
        ids = ["tq_breakout", "trend_pullback", "sector_rotation",
               "reported_profit_breakout", "main_force_top10"]
        runtimes = [
            _runtime(key, base_priority=0.8,
                     max_positions=3 if key == "main_force_top10" else None)
            for key in ids
        ]
        result = allocation.position_limits(
            runtimes,
            hard_pool_cap=15,
            strategy_max_positions=6,
            strategy_min_positions=2,
            protected_slot_floor=2,
            account_order={key: idx for idx, key in enumerate(ids)},
        )
        self.assertLessEqual(sum(result["limits"].values()), 15)
        self.assertEqual(3, result["limits"]["main_force_top10"])
        self.assertEqual(
            {"engine", "risk_scale", "protected_slot_floor", "total_cap",
             "limits", "effective_weights"},
            set(result),
        )

    def test_main_force_style_runtime_is_capped_by_declaration(self):
        result = allocation.position_limits(
            [_runtime("a", base_priority=1.0, max_positions=3),
             _runtime("b", base_priority=0.4, max_positions=3)],
            hard_pool_cap=15,
            strategy_max_positions=6,
            strategy_min_positions=1,
            protected_slot_floor=2,
            account_order={"a": 0, "b": 1},
        )
        self.assertLessEqual(sum(result["limits"].values()), 12)
        self.assertLessEqual(result["limits"]["a"], 3)
        self.assertGreaterEqual(result["limits"]["b"], 1)

    def test_hard_pool_cap_wins_over_per_strategy_caps(self):
        runtimes = [_runtime(f"s{i}", base_priority=1.0, max_positions=6)
                    for i in range(5)]
        result = allocation.position_limits(
            runtimes,
            hard_pool_cap=4,
            strategy_max_positions=6,
            strategy_min_positions=2,
            protected_slot_floor=2,
            account_order={},
        )
        self.assertLessEqual(sum(result["limits"].values()), 4)

    def test_zero_strategies_return_empty_allocation(self):
        result = allocation.position_limits(
            [], hard_pool_cap=15, strategy_max_positions=6,
            strategy_min_positions=2, protected_slot_floor=2, account_order={},
        )
        self.assertEqual({}, result["limits"])
        self.assertEqual(0, result["total_cap"])


class StrategyPoolBudgetTests(unittest.TestCase):
    def test_priority_floor_is_declared_not_identity_based(self):
        result = allocation.strategy_pool_budget(
            [_runtime("main_force_top10", base_priority=0.5, priority_floor_pct=0.20),
             _runtime("tq_breakout", base_priority=0.5)],
            account_id="main_force_top10",
            values={"tq_breakout": 1000.0, "main_force_top10": 0.0},
            pending_by_account={"tq_breakout": 100.0, "main_force_top10": 0.0},
            pending_total=100.0,
            nav=10000.0,
            market_scales={"tq_breakout": 0.65, "main_force_top10": 0.8},
            shared_pool_max_exposure=0.82,
            strategy_pool_floor_ratio=0.55,
        )
        self.assertEqual(20.0, result["priority_floor_pct"])
        self.assertTrue(result["market_scale_applied"])
        self.assertGreaterEqual(result["floor_amount"], 2000.0)

    def test_own_exposure_cap_limits_allowance(self):
        result = allocation.strategy_pool_budget(
            [_runtime("sector_rotation", base_priority=1.0, own_exposure_cap_pct=1.0),
             _runtime("other", base_priority=1.0)],
            account_id="sector_rotation",
            values={"sector_rotation": 9500.0, "other": 0.0},
            pending_by_account={"sector_rotation": 100.0, "other": 0.0},
            pending_total=100.0,
            nav=10000.0,
            market_scales=None,
            shared_pool_max_exposure=0.82,
            strategy_pool_floor_ratio=0.55,
        )
        self.assertLessEqual(
            result["current_total_amount"] + result["allowance_amount"], 10000.0)

    def test_allowance_never_exceeds_pool_headroom(self):
        result = allocation.strategy_pool_budget(
            [_runtime("a"), _runtime("b")],
            account_id="a",
            values={"a": 7000.0, "b": 1000.0},
            pending_by_account={"a": 0.0, "b": 200.0},
            pending_total=200.0,
            nav=10000.0,
            market_scales=None,
            shared_pool_max_exposure=0.82,
            strategy_pool_floor_ratio=0.60,
        )
        self.assertLessEqual(result["allowance_amount"], result["global_remaining_amount"])


class PoolCapPropertyTests(unittest.TestCase):
    """不变式：任意 N 个策略下 Σ allocated ≤ shared pool cap 恒成立。"""

    def _runtimes(self, count):
        return [
            _runtime(
                f"s{index}",
                base_priority=0.2 + (index % 7) * 0.1,
                regime_fit=0.5 + (index % 3) * 0.2,
                confidence=0.4 + (index % 5) * 0.1,
                health=0.3 + (index % 4) * 0.2,
                data_quality=0.6,
                diversification=0.7 + (index % 2) * 0.3,
                max_positions=3 + (index % 3),
            )
            for index in range(count)
        ]

    def test_seat_allocation_never_exceeds_pool_cap(self):
        for count in (0, 1, 2, 5, 10, 20, 50):
            with self.subTest(count=count):
                runtimes = self._runtimes(count)
                result = allocation.position_limits(
                    runtimes,
                    hard_pool_cap=15,
                    strategy_max_positions=6,
                    strategy_min_positions=2,
                    protected_slot_floor=2,
                    account_order={r.strategy_id: i for i, r in enumerate(runtimes)},
                )
                self.assertLessEqual(sum(result["limits"].values()), 15)
                for key, limit in result["limits"].items():
                    cap = next(r.max_positions for r in runtimes if r.strategy_id == key)
                    self.assertLessEqual(limit, cap)

    def test_budget_allowance_never_exceeds_pool_cap(self):
        for count in (0, 1, 2, 5, 10, 20, 50):
            with self.subTest(count=count):
                runtimes = self._runtimes(count)
                nav = 100000.0
                cap_amount = nav * 0.82
                # 构造合法初始态：committed ≤ pool cap。
                raw_values = {r.strategy_id: (i * 977.0) % 4000.0 for i, r in enumerate(runtimes)}
                pending = {r.strategy_id: (i * 131.0) % 500.0 for i, r in enumerate(runtimes)}
                scale = min(1.0, cap_amount * 0.8 / max(sum(raw_values.values()) + sum(pending.values()), 1.0))
                values = {key: round(value * scale, 2) for key, value in raw_values.items()}
                pending_total = sum(pending.values())
                for runtime in runtimes:
                    result = allocation.strategy_pool_budget(
                        runtimes,
                        account_id=runtime.strategy_id,
                        values=values,
                        pending_by_account=pending,
                        pending_total=pending_total,
                        nav=nav,
                        market_scales=None,
                        shared_pool_max_exposure=0.82,
                        strategy_pool_floor_ratio=0.60,
                    )
                    self.assertLessEqual(result["allowance_amount"],
                                         result["global_remaining_amount"] + 0.01)
                    self.assertLessEqual(result["current_total_amount"]
                                         + result["allowance_amount"],
                                         cap_amount + 0.01)

    def test_at_cap_no_strategy_gets_allowance(self):
        # 池子打满时，任何 N 个策略的可追加额度都必须归零。
        for count in (1, 5, 20, 50):
            with self.subTest(count=count):
                runtimes = self._runtimes(count)
                nav = 100000.0
                cap_amount = nav * 0.82
                per = cap_amount / count
                values = {r.strategy_id: per for r in runtimes}
                pending = {r.strategy_id: 0.0 for r in runtimes}
                for runtime in runtimes:
                    result = allocation.strategy_pool_budget(
                        runtimes,
                        account_id=runtime.strategy_id,
                        values=values,
                        pending_by_account=pending,
                        pending_total=0.0,
                        nav=nav,
                        market_scales=None,
                        shared_pool_max_exposure=0.82,
                        strategy_pool_floor_ratio=0.60,
                    )
                    self.assertEqual(0.0, result["allowance_amount"])

    def test_budget_with_empty_runtimes_is_safe(self):
        # 未注册（不在运行时表）的策略 fail-closed：拿不到任何预算。
        result = allocation.strategy_pool_budget(
            [],
            account_id="ghost",
            values={"ghost": 100.0},
            pending_by_account={"ghost": 0.0},
            pending_total=0.0,
            nav=10000.0,
            market_scales=None,
            shared_pool_max_exposure=0.82,
            strategy_pool_floor_ratio=0.60,
        )
        self.assertEqual(0.0, result["allowance_amount"])
        self.assertTrue(result["unknown_strategy"])


if __name__ == "__main__":
    unittest.main()


class LifecycleStageTests(unittest.TestCase):
    def test_default_stage_is_standard_with_full_scale(self):
        scale, stage = allocation.stage_capital_scale(_runtime("a"))
        self.assertEqual(("standard", 1.0), (stage, scale))

    def test_stage_scale_table_covers_cold_start_to_retirement(self):
        expected = {"shadow": 0.0, "pilot": 0.25, "standard": 1.0,
                    "mature": 1.0, "quarantined": 0.0}
        for stage, scale in expected.items():
            with self.subTest(stage=stage):
                got_scale, got_stage = allocation.stage_capital_scale(
                    _runtime("a", lifecycle_stage=stage))
                self.assertEqual((stage, scale), (got_stage, got_scale))

    def test_explicit_capital_scale_overrides_stage(self):
        scale, stage = allocation.stage_capital_scale(
            _runtime("a", lifecycle_stage="standard", capital_scale=0.4))
        self.assertEqual(("standard", 0.4), (stage, scale))

    def test_unknown_stage_fails_closed_as_quarantined(self):
        scale, stage = allocation.stage_capital_scale(_runtime("a", lifecycle_stage="zzz"))
        self.assertEqual(("quarantined", 0.0), (stage, scale))


class CapitalEligibilityTests(unittest.TestCase):
    def test_minimum_deployable_budget_is_one_lot(self):
        self.assertEqual(2150.0, allocation.minimum_deployable_budget(21.5))
        self.assertEqual(2193.0, allocation.minimum_deployable_budget(21.5, price_buffer=1.02))

    def test_budget_below_one_lot_never_produces_a_fragment(self):
        result = allocation.deployable_budget(budget_amount=2000.0, price=21.5)
        self.assertFalse(result["allowed"])
        self.assertEqual(0, result["lots"])
        self.assertEqual(0.0, result["deployable_amount"])
        self.assertEqual(2000.0, result["waiting_capital"])

    def test_exact_multiple_deploys_without_waiting(self):
        result = allocation.deployable_budget(budget_amount=4300.0, price=21.5)
        self.assertTrue(result["allowed"])
        self.assertEqual(2, result["lots"])
        self.assertEqual(4300.0, result["deployable_amount"])
        self.assertEqual(0.0, result["waiting_capital"])

    def test_remainder_after_whole_lots_goes_to_waiting(self):
        result = allocation.deployable_budget(budget_amount=5000.0, price=21.5)
        self.assertEqual(2, result["lots"])
        self.assertEqual(4300.0, result["deployable_amount"])
        self.assertEqual(700.0, result["waiting_capital"])

    def test_invalid_price_freezes_the_budget(self):
        result = allocation.deployable_budget(budget_amount=5000.0, price=0)
        self.assertFalse(result["allowed"])
        self.assertEqual(0.0, result["deployable_amount"])
        self.assertEqual(5000.0, result["waiting_capital"])

    def test_shadow_and_quarantined_deploy_nothing(self):
        for stage in ("shadow", "quarantined"):
            with self.subTest(stage=stage):
                result = allocation.deployable_budget(
                    budget_amount=10000.0, price=21.5,
                    capital_scale=allocation.DEFAULT_STAGE_CAPITAL_SCALE[stage],
                    lifecycle_stage=stage)
                self.assertEqual(0, result["lots"])
                self.assertEqual(0.0, result["deployable_amount"])
                # 生命周期系数为 0：不部署一分钱，但整笔预算仍挂在等待池账上
                # （被阶段系数扣留的资金不能从账目中消失）。
                self.assertEqual(10000.0, result["waiting_capital"])
                self.assertEqual(10000.0, result["lifecycle_withheld_amount"])
                self.assertEqual(0.0, result["scaled_budget"])

    def test_pilot_waiting_capital_keeps_the_withheld_majority(self):
        """pilot 系数 0.25：等待池要含全部未部署预算，而非只剩缩放后的零头。"""
        result = allocation.deployable_budget(
            budget_amount=10000.0, price=21.5, capital_scale=0.25,
            lifecycle_stage="pilot")
        # 10000 × 0.25 = 2500 → 21.5 元/股 × 100 股/手 = 2150 元/手 → 1 手。
        self.assertEqual(1, result["lots"])
        self.assertEqual(2150.0, result["deployable_amount"])
        self.assertEqual(7850.0, result["waiting_capital"])
        self.assertEqual(7500.0, result["lifecycle_withheld_amount"])
        self.assertEqual(2500.0, result["scaled_budget"])


class AllocationPlanPropertyTests(unittest.TestCase):
    """不变式：任意 N 个策略下 Σ deployable ≤ pool headroom，且永无碎片订单。"""

    def _runtimes(self, count):
        stages = ("standard", "pilot", "mature", "shadow", "quarantined")
        return [
            _runtime(
                f"s{index}",
                base_priority=0.2 + (index % 7) * 0.1,
                regime_fit=0.5 + (index % 3) * 0.2,
                confidence=0.4 + (index % 5) * 0.1,
                health=0.3 + (index % 4) * 0.2,
                data_quality=0.6,
                diversification=0.7 + (index % 2) * 0.3,
                max_positions=3 + (index % 3),
                lifecycle_stage=stages[index % len(stages)],
            )
            for index in range(count)
        ]

    def test_plan_invariants_hold_for_any_strategy_count(self):
        for count in (0, 1, 2, 5, 10, 20, 50):
            with self.subTest(count=count):
                runtimes = self._runtimes(count)
                nav = 200000.0
                values = {r.strategy_id: (i * 2711.0) % 6000.0 for i, r in enumerate(runtimes)}
                pending = {r.strategy_id: (i * 353.0) % 800.0 for i, r in enumerate(runtimes)}
                prices = {r.strategy_id: 8.0 + (i % 11) * 7.3 for i, r in enumerate(runtimes)}
                plan = allocation.allocation_plan(
                    runtimes,
                    nav=nav,
                    values=values,
                    pending_by_account=pending,
                    pending_total=sum(pending.values()),
                    prices_by_strategy=prices,
                    shared_pool_max_exposure=0.82,
                    strategy_pool_floor_ratio=0.60,
                )
                # ① Σ deployable ≤ 池余量；
                self.assertLessEqual(plan["total_deployable_amount"],
                                     plan["pool_headroom_amount"] + 0.02)
                # ② 永无碎片订单：要么 lots ≥ 1，要么 deployable == 0；
                for row in plan["plan"]:
                    if row["lots"] >= 1:
                        self.assertGreater(row["deployable_amount"], 0.0)
                    else:
                        self.assertEqual(0.0, row["deployable_amount"])
                        self.assertTrue(row["blocked_reason"])
                    # 部署 + 等待 = 原始预算（含被生命周期系数扣留的部分）。
                    self.assertAlmostEqual(row["deployable_amount"] + row["waiting_capital"],
                                           row["budget_amount"], delta=0.02)
                # ③ waiting 与 deployable 的账目自洽；
                budget_total = sum(row["budget_amount"] for row in plan["plan"])
                self.assertAlmostEqual(
                    plan["total_deployable_amount"] + plan["total_waiting_capital"],
                    budget_total, delta=count * 0.02 + 0.02)
                # ④ shadow/quarantined 永远 0 部署。
                for row in plan["plan"]:
                    if row["lifecycle_stage"] in ("shadow", "quarantined"):
                        self.assertEqual(0.0, row["deployable_amount"])

    def test_plan_is_deterministic(self):
        runtimes = self._runtimes(7)
        kwargs = dict(
            nav=150000.0,
            values={r.strategy_id: 500.0 * (i + 1) for i, r in enumerate(runtimes)},
            pending_by_account={r.strategy_id: 0.0 for r in runtimes},
            pending_total=0.0,
            prices_by_strategy={r.strategy_id: 12.0 + i for i, r in enumerate(runtimes)},
            shared_pool_max_exposure=0.82,
            strategy_pool_floor_ratio=0.60,
        )
        first = allocation.allocation_plan(runtimes, **kwargs)
        second = allocation.allocation_plan(runtimes, **kwargs)
        self.assertEqual(first, second)

    def test_empty_plan_is_safe(self):
        plan = allocation.allocation_plan(
            [], nav=10000.0, values={}, pending_by_account={}, pending_total=0.0,
            prices_by_strategy={}, shared_pool_max_exposure=0.82,
            strategy_pool_floor_ratio=0.60,
        )
        self.assertEqual([], plan["plan"])
        self.assertEqual(0.0, plan["total_deployable_amount"])
        self.assertEqual(0.0, plan["total_waiting_capital"])

    # -- PR-26：生命周期感知的资金部署 -------------------------------
    def _plan(self, runtimes, prices=None, nav=1_000_000.0):
        return allocation.allocation_plan(
            runtimes,
            nav=nav,
            values={runtime.strategy_id: 0.0 for runtime in runtimes},
            pending_by_account={runtime.strategy_id: 0.0 for runtime in runtimes},
            pending_total=0.0,
            prices_by_strategy=prices or {runtime.strategy_id: 10.0 for runtime in runtimes},
            shared_pool_max_exposure=0.82,
            strategy_pool_floor_ratio=0.60,
        )

    def test_pilot_with_top_weight_never_gets_full_budget(self):
        """PR-26 验收：权重最高的试点策略也只能部署 25% 预算。"""
        runtimes = [
            _runtime("new_user_strategy", base_priority=1.0, lifecycle_stage="pilot"),
            _runtime("veteran", base_priority=0.2),
        ]
        rows = {row["strategy_id"]: row for row in self._plan(runtimes)["plan"]}
        pilot = rows["new_user_strategy"]
        veteran = rows["veteran"]
        # 权重最高 → 原始额度最大，但部署额被阶段系数砍到四分之一。
        self.assertGreater(pilot["raw_allowance_amount"], veteran["raw_allowance_amount"])
        self.assertEqual(0.25, pilot["capital_scale"])
        self.assertLessEqual(
            pilot["deployable_amount"], pilot["raw_allowance_amount"] * 0.25 + 1e-6
        )
        self.assertLess(pilot["deployable_amount"], pilot["raw_allowance_amount"])
        self.assertGreater(pilot["deployable_amount"], 0.0)
        self.assertGreater(pilot["waiting_capital"], 0.0)

    def test_shadow_and_quarantined_deploy_nothing(self):
        runtimes = [
            _runtime("shadow_one", lifecycle_stage="shadow"),
            _runtime("quarantined_one", lifecycle_stage="quarantined"),
            _runtime("standard_one"),
        ]
        rows = {row["strategy_id"]: row for row in self._plan(runtimes)["plan"]}
        for strategy_id in ("shadow_one", "quarantined_one"):
            row = rows[strategy_id]
            self.assertFalse(row["allowed"])
            self.assertEqual(0, row["lots"])
            self.assertEqual(0.0, row["deployable_amount"])
            self.assertIn("生命周期阶段", row["blocked_reason"] or "")
            # 整笔预算留在等待池，不因阶段系数为 0 而蒸发。
            self.assertEqual(row["budget_amount"], row["waiting_capital"])
            self.assertEqual(row["budget_amount"], row["lifecycle_withheld_amount"])
        self.assertTrue(rows["standard_one"]["allowed"])
        self.assertGreater(rows["standard_one"]["deployable_amount"], 0.0)

    def test_budget_below_one_lot_goes_to_waiting_capital(self):
        runtimes = [_runtime("tiny", base_priority=0.01)]
        # 价格 10 元 → 一手 1000 元；预算被阶段系数压到不足一手。
        row = self._plan(runtimes, prices={"tiny": 900.0}, nav=1_200.0)["plan"][0]
        self.assertFalse(row["allowed"])
        self.assertEqual(0, row["lots"])
        self.assertEqual(0.0, row["deployable_amount"])
        self.assertGreater(row["waiting_capital"], 0.0)
        self.assertIn("等待池", row["blocked_reason"] or "")

    def test_explicit_capital_scale_beats_stage_default(self):
        runtimes = [_runtime("promoted", lifecycle_stage="pilot", capital_scale=0.5)]
        row = self._plan(runtimes)["plan"][0]
        self.assertEqual(0.5, row["capital_scale"])
        self.assertGreater(row["deployable_amount"], 0.0)
