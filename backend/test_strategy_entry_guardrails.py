"""Regression coverage for strategy-specific entry and downside guardrails."""
from __future__ import annotations

import unittest

import paper_trading as P


def _decision():
    return {"hard_vetoes": [], "avg_score": 0.80}


class StrategyEntryGuardrailTests(unittest.TestCase):
    def test_sector_rotation_rejects_hot_name_without_breadth_or_flow(self):
        result = P._strategy_entry_assessment(
            {"id": "sector_rotation"},
            {
                "code": "000001", "score": 0.8,
                "sector_heat": {
                    "rank": 2, "pct": 2.0, "member_count": 3,
                    "positive_ratio": 0.55, "median_main_pct": -0.2,
                    "early_rotation": False,
                },
            },
            {"pct": 1.2, "vol_ratio": 1.4, "main_pct": 1.0, "price": 10.0},
            None, _decision(), market={"light": "green", "breadth_up_pct": 60},
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("广度不足" in item for item in result["blockers"]))
        self.assertTrue(any("资金未确认" in item for item in result["blockers"]))

    def test_sector_rotation_accepts_broad_early_rotation_with_stock_flow(self):
        result = P._strategy_entry_assessment(
            {"id": "sector_rotation"},
            {
                "code": "000001", "score": 0.8,
                "sector_heat": {
                    "rank": 2, "pct": 2.0, "member_count": 12,
                    "positive_ratio": 0.75, "median_main_pct": 0.8,
                    "early_rotation": True,
                },
            },
            {"pct": 1.5, "vol_ratio": 1.5, "main_pct": 1.2, "price": 10.0},
            None, _decision(), market={"light": "green", "breadth_up_pct": 60},
        )
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["threshold"], 0.65)

    def test_tq_breakout_rejects_negative_live_flow(self):
        result = P._strategy_entry_assessment(
            {"id": "tq_breakout"}, {"code": "000001", "score": 0.8},
            {"pct": 2.0, "vol_ratio": 1.5, "main_pct": -0.1, "price": 10.0},
            None, _decision(), market={"light": "green"},
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("未转正" in item for item in result["blockers"]))

    def test_trend_pullback_rejects_negative_live_flow(self):
        kline = P.pd.DataFrame({"close": [10.0] * 60})
        result = P._strategy_entry_assessment(
            {"id": "trend_pullback"}, {"code": "000001", "score": 0.8, "mom20": 12.0},
            {"pct": 0.0, "vol_ratio": 1.2, "main_pct": -2.1, "price": 10.0, "open_price": 10.0},
            kline, _decision(), market={"light": "green", "breadth_up_pct": 60},
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("偏弱，不做趋势回踩" in item for item in result["blockers"]))

    def test_sector_downside_ladder_is_tighter_than_before(self):
        policy = P.INTRADAY_DOWNSIDE_POLICIES["sector_rotation"]
        self.assertEqual(policy["warning_pct"], -2.2)
        self.assertEqual(policy["partial_pct"], -3.0)
        self.assertEqual(policy["full_pct"], -4.5)
        self.assertEqual(policy["partial_ratio"], 0.40)


if __name__ == "__main__":
    unittest.main()
