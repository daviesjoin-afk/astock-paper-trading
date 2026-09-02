"""Regression coverage for the bounded aggressive sector-hot lane."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import paper_trading as P  # noqa: E402


class SectorHotChaseTests(unittest.TestCase):
    def setUp(self):
        self.account = {"id": "sector_rotation"}
        self.pick = {
            "code": "000001", "candidate_status": "sector_surge_lane",
            "sector_heat": {"rank": 3, "pct": 3.2},
        }
        self.quote = {"pct": 8.2, "price": 10.82, "open_price": 10.0, "main_pct": 3.5, "vol_ratio": 2.4}
        self.model = {"passed": True, "score": 0.84, "overheat_guard": {"level": "caution"}}
        self.quality = {"tier": "Q1"}
        self.execution_quote = {"status": "cross_source_checked"}

    def test_hot_sector_candidate_can_use_bounded_chase_lane(self):
        price_ok, _ = P._new_entry_price_gate(self.account, self.pick, self.quote)
        gate = P._chase_entry_gate(self.account, self.pick, self.quote, {"light": "green"}, self.model, self.quality, self.execution_quote)
        self.assertTrue(price_ok)
        self.assertTrue(gate["required"])
        self.assertTrue(gate["allowed"])
        self.assertEqual(gate["risk_scale"], 0.50)

    def test_hot_sector_candidate_fails_closed_without_sector_or_flow_confirmation(self):
        weak = dict(self.pick, sector_heat={"rank": 12, "pct": 1.0})
        gate = P._chase_entry_gate(self.account, weak, self.quote, {"light": "green"}, self.model, self.quality, self.execution_quote)
        self.assertTrue(gate["required"])
        self.assertFalse(gate["allowed"])
        self.assertIn("板块前5", gate["reason"])

    def test_ordinary_sector_candidate_keeps_original_price_ceiling(self):
        ordinary = dict(self.pick, candidate_status="ordinary")
        price_ok, reason = P._new_entry_price_gate(self.account, ordinary, self.quote)
        self.assertFalse(price_ok)
        # It may be rejected by either the opening-runup guard or the
        # strategy ceiling; both are pre-existing ordinary-entry safeguards.
        self.assertTrue(reason)

    def test_market_scale_is_not_applied_twice_after_budgeting(self):
        scale = P._entry_execution_scale(
            {"risk_scale": 0.65},
            {"position_scale": 0.75},
            {"risk_scale": 1.0},
            {"risk_scale": 1.0},
            {"market_scale_applied": True},
        )
        self.assertEqual(scale, 0.75)

    def test_market_scale_remains_for_unscaled_budget_fallback(self):
        scale = P._entry_execution_scale(
            {"risk_scale": 0.65},
            {"position_scale": 0.75},
            {"risk_scale": 1.0},
            {"risk_scale": 1.0},
            {"market_scale_applied": False},
        )
        self.assertAlmostEqual(scale, 0.4875)

    def test_tq_mid_acceleration_requires_same_q1_chase_confirmation(self):
        account = {"id": "tq_breakout"}
        pick = {"code": "000001", "score": 0.5}
        quote = {"pct": 5.0, "main_pct": 3.0, "vol_ratio": 2.0}
        model = {"passed": True, "score": 0.82}
        execution = {"status": "cross_source_checked"}
        blocked = P._chase_entry_gate(account, pick, quote, {"light": "green"}, model, {"tier": "Q2"}, execution)
        allowed = P._chase_entry_gate(account, pick, quote, {"light": "green"}, model, {"tier": "Q1"}, execution)
        self.assertFalse(blocked["allowed"])
        self.assertIn("Q1", blocked["reason"])
        self.assertTrue(allowed["allowed"])

    def test_sector_pullback_requires_real_intraday_retrace(self):
        closes = P.pd.DataFrame({"close": [10 + i * 0.03 for i in range(30)]})
        no_retrace = P._sector_overheat_guard(
            closes, {"price": 10.85, "high": 10.86, "open_price": 10.2}, {"rank": 2, "pct": 3.8},
        )
        retrace = P._sector_overheat_guard(
            closes, {"price": 10.65, "high": 10.85, "open_price": 10.5}, {"rank": 2, "pct": 3.8},
        )
        self.assertFalse(no_retrace["pullback_confirmed"])
        self.assertTrue(retrace["pullback_confirmed"])


if __name__ == "__main__":
    unittest.main()
