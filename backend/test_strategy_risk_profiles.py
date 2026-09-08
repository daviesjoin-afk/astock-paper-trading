import copy
import unittest

from strategy_risk_fingerprint import StrategyRiskFingerprint
from strategy_risk_profiles import (
    _SYSTEM_HARD_RULE_KEYS,
    SystemHardRuleOverrideError,
    compile_strategy_risk_profile,
    compile_strategy_risk_profile_from_strategy,
)


class StrategyRiskProfileTests(unittest.TestCase):
    def test_all_seven_archetypes_compile_to_distinct_layered_templates(self):
        expected = {
            "momentum": "Momentum", "trend": "Trend", "mean_reversion": "MeanReversion",
            "rotation": "Rotation", "event_driven": "Event", "flow_momentum": "Flow",
            "composite": "Composite",
        }
        for archetype, template in expected.items():
            with self.subTest(archetype=archetype):
                profile = compile_strategy_risk_profile(self._fingerprint(archetype))
                self.assertEqual(template, profile.template)
                self.assertTrue(profile.hard_rules)
                self.assertTrue(profile.soft_limits)
                self.assertTrue(profile.evolvable_params)
                self.assertFalse(profile.user_locked_params)
                layers = (set(profile.hard_rules), set(profile.soft_limits), set(profile.evolvable_params))
                self.assertFalse(layers[0] & layers[1] | layers[0] & layers[2] | layers[1] & layers[2])

    def test_user_lock_is_preserved_and_removed_from_tunable_layer(self):
        profile = compile_strategy_risk_profile(
            "Momentum", user_locked_params={"entry_score": 0.81, "max_positions": 2}
        )

        self.assertEqual({"entry_score": 0.81, "max_positions": 2}, profile.user_locked_params)
        self.assertNotIn("entry_score", profile.evolvable_params)
        self.assertNotIn("max_positions", profile.soft_limits)

    def test_system_hard_rules_cannot_be_overridden_even_when_nested(self):
        for attempted in (
            {"paper_trading_rules": {"slippage": 0}},
            {"entry_score": {"commission": 0}},
            # Prefix tuples and the ST/delisting gate also belong to
            # paper_trading_rules and define the tradable security scope.
            {"entry_score": {"MAIN_BOARD_PREFIXES": []}},
            {"entry_score": {"CHINEXT_PREFIXES": ("300",)}},
            {"entry_score": {"STAR_PREFIXES": ("688",)}},
            {"entry_score": {"T0_ETF_PREFIXES": ("51",)}},
            {"entry_score": {"is_st_or_delisting": None}},
        ):
            with self.subTest(attempted=attempted):
                with self.assertRaises(SystemHardRuleOverrideError):
                    compile_strategy_risk_profile("Trend", user_locked_params=attempted)

    def test_templates_do_not_publish_system_trading_rule_keys(self):
        for archetype in ("momentum", "trend", "mean_reversion", "rotation", "event_driven", "flow_momentum", "composite"):
            with self.subTest(archetype=archetype):
                profile = compile_strategy_risk_profile(self._fingerprint(archetype))
                emitted = set(profile.hard_rules) | set(profile.soft_limits) | set(profile.evolvable_params)
                self.assertFalse(emitted & _SYSTEM_HARD_RULE_KEYS)

    def test_profile_can_compile_directly_from_pr03_fingerprint_input(self):
        result = compile_strategy_risk_profile_from_strategy(
            {"all": [{"op": "breakout"}, {"field": "realtime_quote"}]},
            {"horizon_days": 3},
        )

        self.assertEqual("Momentum", result.template)
        self.assertEqual("realtime", result.hard_rules["signal_confirmation"])

    def test_compilation_is_deterministic_and_does_not_mutate_locks(self):
        locks = {"trail_stop": 0.04}
        before = copy.deepcopy(locks)

        first = compile_strategy_risk_profile("Momentum", user_locked_params=locks)
        second = compile_strategy_risk_profile("Momentum", user_locked_params=locks)

        self.assertEqual(first, second)
        self.assertEqual(before, locks)

    @staticmethod
    def _fingerprint(archetype):
        return StrategyRiskFingerprint(
            archetype=archetype, holding_horizon="short", signal_half_life="days",
            entry_urgency="next_session", turnover="medium", volatility_sensitivity="medium",
            data_freshness="daily_close", natural_stop="technical", concentration_risk="medium",
        )


if __name__ == "__main__":
    unittest.main()
