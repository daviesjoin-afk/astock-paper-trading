# -*- coding: utf-8 -*-
"""策略执行画像（PR-10）的回归测试。"""
import unittest

import execution_profiles as profiles
from strategy_risk_fingerprint import compile_strategy_risk_fingerprint


class ProfileSelectionTests(unittest.TestCase):
    def test_seven_families_are_declared(self):
        self.assertEqual(
            {"breakout", "trend", "mean_reversion", "rotation",
             "event_driven", "flow_momentum", "composite"},
            set(profiles.EXECUTION_PROFILES),
        )

    def test_first_version_only_uses_market_and_limit(self):
        for family, profile in profiles.EXECUTION_PROFILES.items():
            with self.subTest(family=family):
                self.assertIn(profile["order_type"], {"market", "limit"})

    def test_family_urgency_mapping(self):
        expected = {
            "breakout": "fast", "trend": "medium",
            "mean_reversion": "passive", "rotation": "batch",
            "event_driven": "verified", "flow_momentum": "strict_ttl",
            "composite": "conservative",
        }
        for family, urgency in expected.items():
            with self.subTest(family=family):
                self.assertEqual(urgency, profiles.EXECUTION_PROFILES[family]["urgency"])

    def test_account_risk_profiles_resolve_to_families(self):
        resolve = profiles.execution_profile_for
        self.assertEqual("breakout", resolve("breakout")["family"])
        self.assertEqual("breakout", resolve("momentum")["family"])
        self.assertEqual("trend", resolve("trend")["family"])
        self.assertEqual("mean_reversion", resolve("mean_reversion")["family"])
        self.assertEqual("rotation", resolve("sector")["family"])
        self.assertEqual("event_driven", resolve("core_quality")["family"])
        self.assertEqual("event_driven", resolve("event_driven")["family"])
        self.assertEqual("flow_momentum", resolve("main_force")["family"])
        self.assertEqual("flow_momentum", resolve("flow_momentum")["family"])

    def test_fingerprint_object_uses_its_archetype(self):
        fingerprint = compile_strategy_risk_fingerprint(
            None, {"style": "trend following", "hold": "10 days"})
        profile = profiles.execution_profile_for(fingerprint)
        self.assertEqual("trend", profile["family"])

    def test_unknown_profile_falls_back_to_conservative_composite(self):
        profile = profiles.execution_profile_for("totally_unknown")
        self.assertEqual("composite", profile["family"])
        self.assertEqual("conservative", profile["urgency"])
        self.assertEqual("totally_unknown", profile.get("fallback_from"))

    def test_mapping_input_reads_risk_profile_key(self):
        profile = profiles.execution_profile_for({"risk_profile": "sector"})
        self.assertEqual("rotation", profile["family"])


class LimitPriceTests(unittest.TestCase):
    def test_market_profiles_have_no_limit_price(self):
        self.assertIsNone(profiles.limit_price_for(
            profiles.EXECUTION_PROFILES["breakout"], 20.0))

    def test_trend_offset_pays_up_slightly(self):
        self.assertEqual(20.04, profiles.limit_price_for(
            profiles.EXECUTION_PROFILES["trend"], 20.0))

    def test_mean_reversion_is_passive_at_reference(self):
        self.assertEqual(20.0, profiles.limit_price_for(
            profiles.EXECUTION_PROFILES["mean_reversion"], 20.0))


class EntryLimitTests(unittest.TestCase):
    def test_market_profile_always_allows(self):
        result = profiles.enforce_entry_limit(
            profiles.EXECUTION_PROFILES["breakout"], 25.0, reference_price=24.0)
        self.assertTrue(result["allowed"])
        self.assertIsNone(result["limit_price"])
        self.assertEqual("market", result["order_type"])

    def test_limit_profile_allows_when_price_within_limit(self):
        # 限价锚定参考价（信号收盘价 20.0）：20.03 ≤ 20.04 → 允许。
        result = profiles.enforce_entry_limit(
            profiles.EXECUTION_PROFILES["trend"], 20.03, reference_price=20.0)
        self.assertTrue(result["allowed"])
        self.assertEqual(20.04, result["limit_price"])

    def test_limit_profile_defers_when_price_exceeds_limit(self):
        result = profiles.enforce_entry_limit(
            profiles.EXECUTION_PROFILES["trend"], 20.10, reference_price=20.0)
        self.assertFalse(result["allowed"])
        self.assertEqual(20.04, result["limit_price"])
        self.assertTrue(result["reason"])

    def test_passive_limit_defers_on_any_gap_up(self):
        # mean_reversion 让价 0%：现价高于参考价即延期，只在参考价内被动成交。
        within = profiles.enforce_entry_limit(
            profiles.EXECUTION_PROFILES["mean_reversion"], 20.0, reference_price=20.0)
        gapped = profiles.enforce_entry_limit(
            profiles.EXECUTION_PROFILES["mean_reversion"], 20.3, reference_price=20.0)
        self.assertTrue(within["allowed"])
        self.assertFalse(gapped["allowed"])

    def test_profile_flags_cover_ttl_batch_and_verification(self):
        strict = profiles.EXECUTION_PROFILES["flow_momentum"]
        self.assertTrue(strict["strict_ttl"])
        self.assertEqual(5, strict["ttl_minutes"])
        batch = profiles.EXECUTION_PROFILES["rotation"]
        self.assertTrue(batch["batch"])
        event = profiles.EXECUTION_PROFILES["event_driven"]
        self.assertTrue(event["verification_required"])
        self.assertIsNone(event["ttl_minutes"])

    def test_version_stamped_on_every_resolution(self):
        for family in profiles.EXECUTION_PROFILES:
            self.assertEqual(
                profiles.EXECUTION_PROFILE_VERSION,
                profiles.execution_profile_for(family)["version"])


if __name__ == "__main__":
    unittest.main()
