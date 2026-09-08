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
