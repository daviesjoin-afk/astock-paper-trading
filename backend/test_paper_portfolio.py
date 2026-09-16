# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_portfolio as portfolio


class PaperPortfolioTests(unittest.TestCase):
    def test_aggregate_positions_preserves_t1_locked_quantity_and_costs(self):
        lots = [
            {
                "account_id": "acct",
                "code": "000001",
                "name": "测试股",
                "industry": "银行",
                "remaining_qty": 100,
                "cost": 10.0,
                "acquired_at": "2026-09-02 10:00:00",
                "available_date": "2026-09-03",
                "asset_type": "stock_t1",
            },
            {
                "account_id": "acct",
                "code": "000001",
                "name": "测试股",
                "industry": "银行",
                "remaining_qty": 100,
                "cost": 11.0,
                "acquired_at": "2026-09-03 10:00:00",
                "available_date": "2026-09-04",
                "asset_type": "stock_t1",
            },
        ]
        result = portfolio.aggregate_positions(
            lots,
            [],
            {},
            "2026-09-03",
            num=lambda value, default=0.0: float(value) if value is not None else default,
        )

        self.assertEqual(len(result), 1)
        position = result[0]
        self.assertEqual(position["qty"], 200)
        self.assertEqual(position["available_qty"], 100)
        self.assertEqual(position["locked_qty"], 100)
        self.assertEqual(position["today_acquired_qty"], 100)
        self.assertAlmostEqual(position["cost"], 10.5)

    def test_aggregate_positions_applies_legacy_peak_and_display_cash_flow(self):
        lots = [{
            "account_id": "acct", "code": "000001", "name": "测试股",
            "industry": "银行", "remaining_qty": 100, "cost": 10.0,
            "acquired_at": "2026-09-02 10:00:00", "available_date": "2026-09-03",
            "asset_type": "stock_t1",
        }]
        result = portfolio.aggregate_positions(
            lots,
            [{"account_id": "acct", "code": "000001", "peak_price": 12.0, "take_stage": 1}],
            {("acct", "000001"): {"buy_cash": 1000.0, "sell_cash": 200.0}},
            "2026-09-03",
            num=lambda value, default=0.0: float(value) if value is not None else default,
        )

        position = result[0]
        self.assertEqual(position["peak_price"], 12.0)
        self.assertEqual(position["take_stage"], 1)
        self.assertAlmostEqual(position["display_cost"], 8.0)

    def test_missing_cash_flow_is_unknown_not_a_defined_zero_cost(self):
        """现金流投影缺失时摊薄成本必须是未知，不能塌成"有定义的 0"。

        升级前的历史委托验证列为 NULL，因此**没有**现金流行。若把缺失当成 0，
        摊薄成本就是 0，前端优先取它，于是每个升级前的持仓都显示成本 0。
        """
        lots = [{
            "account_id": "acct", "code": "000001", "name": "测试股",
            "industry": "银行", "remaining_qty": 100, "cost": 10.0,
            "acquired_at": "2026-09-02 10:00:00", "available_date": "2026-09-03",
            "asset_type": "stock_t1",
        }]
        result = portfolio.aggregate_positions(
            lots, [], {}, "2026-09-03",
            num=lambda value, default=0.0: float(value) if value is not None else default,
        )
        position = result[0]
        self.assertAlmostEqual(position["display_cost"], 10.0)
        self.assertAlmostEqual(position["settlement_cost"], 10.0)
        self.assertEqual(position["display_cost_source"], "lot_settlement_cost")
        self.assertNotEqual(0.0, position["display_cost"])

    def test_a_present_cash_flow_row_is_still_used_for_display_cost(self):
        """有已验证现金流行时照旧用摊薄口径，不能被回落逻辑顶掉。"""
        lots = [{
            "account_id": "acct", "code": "000001", "name": "测试股",
            "industry": "银行", "remaining_qty": 100, "cost": 10.0,
            "acquired_at": "2026-09-02 10:00:00", "available_date": "2026-09-03",
            "asset_type": "stock_t1",
        }]
        result = portfolio.aggregate_positions(
            lots, [],
            {("acct", "000001"): {"buy_cash": 1000.0, "sell_cash": 0.0}},
            "2026-09-03",
            num=lambda value, default=0.0: float(value) if value is not None else default,
        )
        position = result[0]
        self.assertAlmostEqual(position["display_cost"], 10.0)
        self.assertEqual(position["display_cost_source"], "verified_cash_flow")

    def test_a_zero_cash_flow_row_is_a_real_zero_not_an_absent_row(self):
        """空 dict 是"行存在但值为 0"，与"没有行"不是同一件事。"""
        lots = [{
            "account_id": "acct", "code": "000001", "name": "测试股",
            "industry": "银行", "remaining_qty": 100, "cost": 10.0,
            "acquired_at": "2026-09-02 10:00:00", "available_date": "2026-09-03",
            "asset_type": "stock_t1",
        }]
        result = portfolio.aggregate_positions(
            lots, [], {("acct", "000001"): {"buy_cash": None, "sell_cash": None}},
            "2026-09-03",
            num=lambda value, default=0.0: float(value) if value is not None else default,
        )
        position = result[0]
        self.assertEqual(position["display_cost_source"], "verified_cash_flow")
        self.assertAlmostEqual(position["display_cost"], 0.0)


if __name__ == "__main__":
    unittest.main()
