# -*- coding: utf-8 -*-
import unittest
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import decision_context
import decision_engine as DE


class DecisionContextTests(unittest.TestCase):
    def setUp(self):
        self.kline = pd.DataFrame(
            {
                "open": [10.0] * 25,
                "close": [10.0 + index * 0.02 for index in range(25)],
                "volume": [1000] * 25,
            }
        )
        self.snap = {
            "name": "测试标的",
            "price": 10.48,
            "pct": 1.2,
            "main_pct": 2.0,
            "turnover": 3.0,
            "pe": 12.0,
            "pb": 1.5,
            "vol_ratio": 1.2,
            "industry": "测试行业",
            "quote_at": "2026-09-03 10:00:00",
        }

    def test_explicit_inputs_are_injected_without_provider_reads(self):
        sector_flow = ({"name": "测试行业", "pct": 1.0, "main_pct": 2.0},)
        snapshot = decision_context.load_evidence(
            "000001",
            kline=self.kline,
            snap=self.snap,
            sector_flow=sector_flow,
            overseas_gate={"light": "green"},
            news_hits=[],
        )

        self.assertEqual(snapshot.source, "injected")
        self.assertEqual(snapshot.code, "000001")
        self.assertEqual(snapshot.name, "测试标的")
        self.assertIs(snapshot.kline, self.kline)
        self.assertEqual(snapshot.sector_flow, sector_flow)
        self.assertEqual(snapshot.overseas_gate, {"light": "green"})

    def test_buy_decision_accepts_a_complete_evidence_snapshot(self):
        result = DE.buy_decision(
            "000001",
            kline=self.kline,
            snap=self.snap,
            sector_flow=[],
            overseas_gate={"light": "green"},
            news_hits=[],
        )

        self.assertEqual(result["code"], "000001")
        self.assertEqual(result["name"], "测试标的")
        self.assertIn(result["tier"], {"T1", "T2", "T3", "T4", "T5"})
        self.assertEqual(len(result["six_dim"]), 6)

    def test_sell_decision_reuses_injected_sector_evidence(self):
        result = DE.sell_decision(
            {"code": "000001", "name": "测试标的", "cost": 10.0, "peak_price": 11.0, "hold_days": 3},
            kline=self.kline,
            snap=self.snap,
            overseas_gate={"light": "green"},
            news_hits=[],
        )

        self.assertEqual(result["code"], "000001")
        self.assertIn("auction_matrix", result)
        self.assertIn(result["auction_matrix"]["tier"], {"Q1", "Q2", "Q3", "Q4", "Q5"})


if __name__ == "__main__":
    unittest.main()
