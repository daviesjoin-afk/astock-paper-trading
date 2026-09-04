# -*- coding: utf-8 -*-
import unittest

import adaptive_shadow_risk as risk


class AdaptiveShadowRiskTests(unittest.TestCase):
    def test_history_normalization_accepts_rows_and_scalar_closes(self):
        rows = risk.shadow_history_rows([
            {"close": "10", "high": "11", "low": "9"},
            12,
            {"close": 0},
        ])
        self.assertEqual(rows, [
            {"close": 10.0, "high": 11.0, "low": 9.0, "prev_close": None},
            {"close": 12.0, "high": None, "low": None, "prev_close": None},
        ])

    def test_portfolio_shadow_risk_marks_unknown_and_duplicate_exposure(self):
        result = risk.portfolio_shadow_risk([
            {"account_id": "a", "code": "000001", "industry": "银行", "qty": 100, "market_price": 10},
            {"account_id": "b", "code": "000001", "industry": "银行", "qty": 50, "cost": 9},
            {"account_id": "b", "code": "000002", "industry": "科技", "qty": 10},
        ], asof="2026-09-03")
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["positions"]["total_value"], 1450.0)
        self.assertEqual(result["data_quality"]["status"], "partial")
        self.assertEqual(result["data_quality"]["unknown_mark_codes"], ["000002"])
        self.assertEqual(result["exposure"]["cross_strategy_same_code"][0]["code"], "000001")
        self.assertIn("同股跨策略重复暴露", result["flags"])

    def test_volatility_and_atr_report_unknown_for_insufficient_history(self):
        self.assertEqual(risk.shadow_volatility([10, 11], 20)["status"], "unknown")
        self.assertEqual(risk.shadow_atr_pct([10, 11], 20)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
