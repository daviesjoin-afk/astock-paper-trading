# -*- coding: utf-8 -*-
import unittest

import paper_sizing as sizing


def number(value, default=0.0):
    if value is None:
        return default
    return float(value)


PROFILE = {
    "single_risk": 0.01,
    "max_weight": 0.10,
    "max_exposure": 0.65,
    "max_industry": 0.30,
}


class PaperSizingTests(unittest.TestCase):
    def test_dynamic_minimum_uses_cycle_capital_and_slot_limit(self):
        self.assertEqual(
            sizing.dynamic_minimum_order_amount(100000, 15),
            3200.0,
        )
        self.assertEqual(
            sizing.dynamic_minimum_order_amount(300000, 15),
            9800.0,
        )

    def test_dynamic_minimum_has_safe_nonzero_granularity(self):
        self.assertEqual(sizing.dynamic_minimum_order_amount(1000, 15), 100.0)
        self.assertEqual(sizing.dynamic_minimum_order_amount(100000, 0), 0.0)

    def test_invalid_price_returns_explanation_without_sizing(self):
        qty, detail = sizing.price_aware_qty(
            100000, 10000, 0, 0, 0, 0, -0.05, PROFILE, num=number,
        )
        self.assertEqual(qty, 0)
        self.assertEqual(detail["reason"], "无有效价格")

    def test_cash_can_be_the_binding_constraint(self):
        qty, detail = sizing.price_aware_qty(
            100000, 10000, 0, 0, 0, 10, -0.05, PROFILE, num=number,
        )
        self.assertEqual(qty, 1000)
        self.assertIn("cash", detail["binding_constraints"])

    def test_pending_pool_and_strategy_reserves_reduce_remaining_exposure(self):
        _, detail = sizing.price_aware_qty(
            100000, 100000, 10000, 0, 0, 10, -0.05, PROFILE,
            strategy_position_value=20000, strategy_cap_amount=30000,
            pool_cap_amount=50000, pending_strategy_amount=5000,
            pending_pool_amount=7000, num=number,
        )
        self.assertEqual(detail["pool_remaining_amount"], 33000.0)
        self.assertEqual(detail["strategy_remaining_amount"], 5000.0)
        self.assertEqual(detail["pending_pool_amount"], 7000.0)
        self.assertEqual(detail["pending_strategy_amount"], 5000.0)

    def test_single_position_absolute_cap_overrides_strategy_weight(self):
        qty, detail = sizing.price_aware_qty(
            100000, 100000, 0, 0, 0, 10, -0.05,
            dict(PROFILE, single_risk=0.50, max_weight=0.30),
            single_position_max_amount=15000, num=number,
        )
        self.assertEqual(qty, 1500)
        self.assertEqual(detail["single_position_cap_source"], "configured_absolute_cap")
        self.assertEqual(detail["single_position_max_amount"], 15000.0)


class RiskAmountSizingTests(unittest.TestCase):
    """PR-09：风险金额 sizing + 流动性约束 + explanation/audit 字段。"""

    def _qty(self, **kwargs):
        base = dict(
            nav=100000.0, cash=50000.0, position_value=0.0,
            industry_value=0.0, code_value=0.0, fill_price=20.0,
            hard_stop=-0.05, profile=PROFILE, exposure_cap=None,
            max_exposure_cap=None, exposure_scale=1.0,
            strategy_position_value=0.0, strategy_cap_amount=None,
            pool_cap_amount=None, pending_strategy_amount=0.0,
            pending_pool_amount=0.0, num=number,
        )
        base.update(kwargs)
        return sizing.price_aware_qty(**base)

    def test_qty_risk_is_qty_times_per_share_stop_distance(self):
        qty, detail = self._qty()
        self.assertEqual(qty, 500)  # nav*1% / (20*0.05) = 1000 -> 其他约束更低时另行断言
        self.assertAlmostEqual(detail["qty_risk_amount"], qty * 20.0 * 0.05, places=2)
        self.assertEqual(detail["entry_stop_distance_pct"], 5.0)

    def test_risk_constraint_can_bind(self):
        # 放宽权重/敞口，让风险预算成为唯一约束：nav*1% / 每股止损 1 元 = 1000 股。
        profile = dict(PROFILE, max_weight=0.30, max_exposure=0.90)
        qty, detail = self._qty(cash=1_000_000.0, profile=profile)
        self.assertEqual(qty, 1000)
        self.assertIn("risk", detail["binding_constraints"])
        self.assertEqual(detail["qty_risk_amount"], 1000.0)

    def test_liquidity_can_be_the_binding_constraint(self):
        profile = dict(PROFILE, max_weight=0.30, max_exposure=0.90)
        qty, detail = self._qty(
            cash=1_000_000.0, profile=profile, liquidity_cap_amount=15000.0)
        self.assertEqual(detail["binding_constraints"], ["liquidity"])
        self.assertEqual(qty, 700)  # 15000 / 20 = 750 股 → 整手 700 股
        self.assertEqual(detail["liquidity_cap_amount"], 15000.0)

    def test_missing_liquidity_keeps_legacy_behaviour(self):
        qty, detail = self._qty(cash=1_000_000.0)
        self.assertIsNone(detail["liquidity_cap_amount"])
        self.assertNotIn("liquidity", detail["constraint_shares"])

    def test_floor_to_lot_size_of_100(self):
        # 风险预算 1000 股，流动性 1099 股 → 取 1000 股仍是 100 的倍数；
        # 再用非整百风险验证向下取整。
        profile = dict(PROFILE, max_weight=0.30, max_exposure=0.90)
        qty, detail = self._qty(
            cash=1_000_000.0, profile=profile, liquidity_cap_amount=21990.0,
            nav=97000.0)
        self.assertEqual(qty % 100, 0)
        self.assertEqual(detail["rounded_to_lot_size"], 100)
        self.assertLessEqual(detail["unrounded_shares"], qty + 100)

    def test_explanation_and_audit_fields_always_present(self):
        qty, detail = self._qty(cash=1_000_000.0)
        self.assertEqual(detail["engine"], sizing.SIZING_ENGINE_VERSION)
        self.assertTrue(detail["sizing_explanation"])
        self.assertTrue(all(isinstance(step, str) for step in detail["sizing_explanation"]))
        for key in ("qty_risk_amount", "entry_stop_distance_pct",
                    "unrounded_shares", "rounded_to_lot_size"):
            self.assertIn(key, detail)

    def test_invalid_price_returns_engine_tagged_audit(self):
        qty, detail = self._qty(fill_price=0)
        self.assertEqual(qty, 0)
        self.assertEqual(detail["engine"], sizing.SIZING_ENGINE_VERSION)
        self.assertEqual(detail.get("reason"), "无有效价格")




if __name__ == "__main__":
    unittest.main()
