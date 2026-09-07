# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_trading_rules as rules


class PaperTradingRulesTests(unittest.TestCase):
    def test_security_scope_keeps_risk_boards_closed(self):
        self.assertTrue(rules.security_scope("000001")["allowed"])
        self.assertFalse(rules.security_scope("688001")["allowed"])
        self.assertFalse(rules.security_scope("000001", "ST测试")["allowed"])

    def test_asset_type_distinguishes_etf_t0_from_stock_t1(self):
        self.assertEqual(rules.asset_type("510300", "沪深300ETF"), "etf_t0")
        self.assertEqual(rules.asset_type("000001", "平安银行"), "stock_t1")

    def test_commission_uses_current_no_minimum_policy(self):
        self.assertAlmostEqual(rules.commission(10000), 1.0)
        self.assertEqual(rules.commission(0), 0.0)

    def test_limit_pct_matches_board_and_st_tiers(self):
        # ST/退市风险标的固定 5%，与板块无关（issue #27 场景矩阵）。
        self.assertEqual(rules.limit_pct("000001", "平安银行"), 9.5)
        self.assertEqual(rules.limit_pct("300750", "宁德测试"), 19.5)
        self.assertEqual(rules.limit_pct("688001", "科创测试"), 19.5)
        self.assertEqual(rules.limit_pct("000001", "ST测试"), 5.0)
        self.assertEqual(rules.limit_pct("000001", "退市测试"), 5.0)
        self.assertEqual(rules.limit_pct("000001", risk_flag=True), 5.0)

    def test_sell_stamp_tax_and_slippage_constants(self):
        # 印花税只对卖出方向生效，费率由成交流径组合：commission + stamp。
        self.assertEqual(rules.STAMP_SELL, 0.0005)
        self.assertEqual(rules.SLIPPAGE, 0.001)
        buy_fees = rules.commission(100000)
        sell_fees = rules.commission(100000) + 100000 * rules.STAMP_SELL
        self.assertAlmostEqual(sell_fees - buy_fees, 100000 * rules.STAMP_SELL)
        # 滑点方向：买入抬高成本、卖出压低所得（成交流径的组合约定）。
        self.assertAlmostEqual(100.0 * (1 + rules.SLIPPAGE), 100.1)
        self.assertAlmostEqual(100.0 * (1 - rules.SLIPPAGE), 99.9)

    def test_security_scope_normalizes_short_codes(self):
        self.assertTrue(rules.security_scope("1")["allowed"])  # zfill -> 000001
        self.assertFalse(rules.security_scope("830001")["allowed"])  # 北交所


if __name__ == "__main__":
    unittest.main()
