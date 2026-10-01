# -*- coding: utf-8 -*-
"""R32 Final: the entry/sizing ownership boundary is frozen, not unified.

The R32 Final decision (Q1(a)) keeps three different owners, and this file locks
them down so a later "one Entry Authority" refactor cannot smuggle a sizing
change in:

    Entry / Admission Authority   -> whether an order may be admitted (+ evidence)
    Sizing / Market Exposure      -> the Active per-strategy yellow-light risk_scale
    Research / Shadow Observation -> the red-light sector-heat shadow exception

None of these are moved into ``execution_planner``, and the manual path does not
inherit the Active per-strategy scaling.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import execution_planner as EP  # noqa: E402
import paper_trading as PT  # noqa: E402
from test_r32_final_promotion import _PromotionFixture  # noqa: E402

#: Characterized today: the per-strategy yellow-light scaling the Active buy path
#: consumes as a sizing input (`paper_trading.py:9772`).
ACTIVE_YELLOW_SCALE = {"tq_breakout": 0.5, "trend_pullback": 0.75, "sector_rotation": 0.65}
HOT_SECTOR = {"sector_heat": {"rank": 3, "pct": 2.5}}


class ActiveMarketSizingOwnershipTests(unittest.TestCase):
    """R-F9 / R-F10: the Active sizing and shadow-exception semantics are frozen."""

    def test_r_f9_active_yellow_light_scaling_is_unchanged(self):
        for account_id, expected in ACTIVE_YELLOW_SCALE.items():
            with self.subTest(account_id=account_id):
                policy = PT._strategy_market_policy(
                    {"id": account_id}, {"sector_heat": {}}, {"pct": 0.0},
                    {"light": "yellow"})
                self.assertTrue(policy["allowed"])
                self.assertEqual(expected, policy["risk_scale"])
                self.assertEqual("谨慎", policy["state"])
        green = PT._strategy_market_policy({"id": "tq_breakout"}, {}, {}, {"light": "green"})
        self.assertEqual(1.0, green["risk_scale"])
        unknown = PT._strategy_market_policy({"id": "tq_breakout"}, {}, {}, {"light": "unknown"})
        self.assertFalse(unknown["allowed"])
        self.assertEqual("市场数据未知", unknown["reason"])

    def test_r_f10_red_light_sector_heat_shadow_exception_is_unchanged(self):
        policy = PT._strategy_market_policy(
            {"id": "sector_rotation"}, HOT_SECTOR, {"pct": 1.2}, {"light": "red"})
        # The exception is research/shadow observation only: admission stays false.
        self.assertFalse(policy["allowed"])
        self.assertTrue(policy["shadow_exception"])
        self.assertIn("仅记录为影子例外", policy["shadow_reason"])
        cold = PT._strategy_market_policy(
            {"id": "sector_rotation"}, {"sector_heat": {"rank": 40, "pct": -0.4}},
            {"pct": 1.2}, {"light": "red"})
        self.assertFalse(cold["allowed"])
        self.assertFalse(cold.get("shadow_exception"))

    def test_r_f9b_sizing_and_observation_stay_out_of_the_entry_authority(self):
        state_fields = set(EP.EntryGateState.__dataclass_fields__)
        for forbidden in ("risk_scale", "market_light_scale", "market_risk_scale",
                          "exposure_scale", "shadow_exception"):
            self.assertNotIn(forbidden, state_fields)
        planner_source = pathlib.Path(EP.__file__).read_text(encoding="utf-8")
        for token in ("market_light_scale", "shadow_exception", "shadow_reason",
                      "risk_scale"):
            self.assertNotIn(token, planner_source)
        # The Active owner keeps its own rule.
        trading_source = pathlib.Path(PT.__file__).read_text(encoding="utf-8")
        self.assertIn("def _strategy_market_policy(", trading_source)
        self.assertIn('sizing["market_risk_scale"] = _num(market_policy.get("risk_scale"), 1.0)',
                      trading_source)

    def test_r_f11_manual_path_does_not_inherit_the_active_scaling(self):
        # The canonical gate the manual path uses has no scaling vocabulary at all,
        # so a unified call site cannot hand manual order the Active scale.
        for light, expected in (("green", False), ("yellow", False),
                                ("red", True), ("unknown", True)):
            with self.subTest(light=light):
                gate = EP.market_gate({"light": light}, "tq_breakout")
                self.assertEqual(expected, bool(gate["blocked"]))
                self.assertNotIn("risk_scale", gate)
                self.assertNotIn("shadow_exception", gate)


class ComparisonSizingIsolationTests(_PromotionFixture, unittest.TestCase):
    """R-F12: a comparison never reads the Active sizing modifier as entry evidence."""

    def test_r_f12_the_report_carries_no_active_sizing_modifier(self):
        report = self._build_report()
        projection = report.projection()
        blob = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        for token in ("risk_scale", "market_risk_scale", "market_light_scale",
                      "shadow_exception", "exposure_scale"):
            self.assertNotIn(token, blob)
        # The admission evidence the comparison *does* carry is the owner's own
        # decision and gate names, never a sizing modifier.
        admission = projection["active_evidence"]["orders"][0]["admission_evidence"]
        self.assertEqual("risk_payload.decision_snapshot.final", admission["source"])
        self.assertNotIn("risk_scale", json.dumps(admission, ensure_ascii=False))
        self.assertEqual("AVAILABLE", report.availability)


if __name__ == "__main__":
    unittest.main()
