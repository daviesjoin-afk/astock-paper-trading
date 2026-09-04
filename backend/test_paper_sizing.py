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
            4900.0,
        )
        self.assertEqual(
            sizing.dynamic_minimum_order_amount(300000, 15),
            14700.0,
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


if __name__ == "__main__":
    unittest.main()
