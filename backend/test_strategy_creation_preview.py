# -*- coding: utf-8 -*-
"""策略创建预览（PR-18）回归测试。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_trading as paper
import strategy_creation_preview as SCP

RISK_PROFILES = {
    "breakout": {"name": "接力快进快出", "max_weight": 0.32,
                 "max_exposure": 0.95, "max_positions": 3},
    "trend": {"name": "趋势波段", "max_weight": 0.34,
              "max_exposure": 0.95, "max_positions": 3},
    "sector": {"name": "板块轮动", "max_weight": 0.32,
               "max_exposure": 0.92, "max_positions": 3},
    "core_quality": {"name": "三日策略", "max_weight": 0.32,
                     "max_exposure": 0.90, "max_positions": 3},
    "main_force": {"name": "超强主力", "max_weight": 0.34,
                   "max_exposure": 0.95, "max_positions": 3},
    "composite": {"name": "保守组合", "max_weight": 0.30,
                  "max_exposure": 0.85, "max_positions": 3},
}


class FingerprintTests(unittest.TestCase):
    def test_breakout_draft_maps_to_breakout_stack(self):
        preview = SCP.strategy_creation_preview({
            "style": "突破 breakout", "realtime": "实时分钟盘中行情",
            "stop_loss": "止损", "max_positions": 3,
        }, RISK_PROFILES)
        self.assertEqual("breakout", preview["risk_fingerprint"]["archetype"])
        self.assertEqual("breakout", preview["recommended"]["risk_profile"])
        self.assertEqual("market",
                         preview["recommended"]["execution_profile"]["order_type"])

    def test_trend_draft_maps_to_trend_and_limit(self):
        preview = SCP.strategy_creation_preview({
            "style": "trend pullback 趋势 回踩 均线", "daily": "日线收盘",
            "hold": "持有 20 天",
        }, RISK_PROFILES)
        self.assertEqual("trend", preview["risk_fingerprint"]["archetype"])
        self.assertEqual("trend", preview["recommended"]["risk_profile"])
        self.assertEqual("limit",
                         preview["recommended"]["execution_profile"]["order_type"])

    def test_empty_draft_fails_closed_to_conservative(self):
        preview = SCP.strategy_creation_preview({}, RISK_PROFILES)
        self.assertEqual("composite", preview["risk_fingerprint"]["archetype"])
        self.assertTrue(preview["fail_closed"])
        self.assertEqual("composite", preview["recommended"]["risk_profile"])

    def test_evolution_profile_is_included(self):
        preview = SCP.strategy_creation_preview({
            "style": "突破 breakout", "realtime": "盘中",
        }, RISK_PROFILES)
        self.assertIn("tunable", preview["evolution"])
        self.assertIn("locked", preview["evolution"])
        self.assertGreater(preview["evolution"]["min_samples"], 0)


class HighRiskOverrideTests(unittest.TestCase):
    def test_more_positions_is_flagged_high_risk(self):
        preview = SCP.strategy_creation_preview({
            "style": "板块轮动", "max_positions": 6,
        }, RISK_PROFILES)
        flagged = {item["key"] for item in preview["high_risk_overrides"]}
        self.assertIn("max_positions", flagged)
        entry = next(item for item in preview["high_risk_overrides"]
                     if item["key"] == "max_positions")
        self.assertEqual(6, entry["user_value"])
        self.assertEqual(3, entry["recommended_value"])

    def test_within_recommendation_is_not_flagged(self):
        preview = SCP.strategy_creation_preview({
            "style": "板块轮动", "max_positions": 3, "max_exposure_pct": 80.0,
        }, RISK_PROFILES)
        self.assertEqual([], preview["high_risk_overrides"])

    def test_higher_exposure_than_recommended_is_flagged(self):
        preview = SCP.strategy_creation_preview({
            "style": "板块轮动", "max_exposure_pct": 96.0,
        }, RISK_PROFILES)
        flagged = {item["key"] for item in preview["high_risk_overrides"]}
        self.assertIn("max_exposure_pct", flagged)


class ApiWiringTests(unittest.TestCase):
    def test_preview_function_exists_on_paper_module(self):
        preview = paper.strategy_creation_preview({
            "style": "突破 breakout", "max_positions": 2,
        })
        self.assertIn("risk_fingerprint", preview)
        self.assertIn("recommended", preview)

    def test_endpoint_registered(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_paper.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("/strategy-preview", source)


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    """P1/P2 回归：hold 基线存在、风格别名归一化。"""

    def test_full_draft_with_holding_fields_does_not_crash(self):
        # 模拟 ACCOUNT_SPECS 形态的完整草稿：hold_max 覆盖比较必须有基线。
        preview = SCP.strategy_creation_preview({
            "style": "板块轮动", "hold_max": 7, "hold_min": 3,
            "max_positions": 3,
        }, RISK_PROFILES)
        self.assertIn("hold_max", preview["recommended"]["limits"])
        self.assertIn("hold_min", preview["recommended"]["limits"])

    def test_shorter_hold_than_baseline_is_flagged(self):
        preview = SCP.strategy_creation_preview({
            "style": "板块轮动", "hold_min": 1,
        }, RISK_PROFILES)
        flagged = {item["key"] for item in preview["high_risk_overrides"]}
        self.assertIn("hold_min", flagged)

    def test_style_aliases_map_to_archetypes(self):
        cases = {
            "strong": "breakout",
            "pullback": "trend",
            "sector": "rotation",
            "quality": "event_driven",
            "main_force": "flow_momentum",
        }
        for alias, expected in cases.items():
            preview = SCP.strategy_creation_preview({"style": alias}, RISK_PROFILES)
            self.assertEqual(expected, preview["risk_fingerprint"]["archetype"],
                             alias)

    def test_chinese_natural_labels_are_normalized(self):
        preview = SCP.strategy_creation_preview({"style": "板块轮动"}, RISK_PROFILES)
        self.assertEqual("rotation", preview["risk_fingerprint"]["archetype"])
