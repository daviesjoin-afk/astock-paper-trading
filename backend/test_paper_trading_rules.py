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


if __name__ == "__main__":
    unittest.main()
