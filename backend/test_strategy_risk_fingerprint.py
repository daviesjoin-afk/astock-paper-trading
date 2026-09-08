import copy
import unittest

from strategy_risk_fingerprint import (
    StrategyRiskFingerprint,
    compile_strategy_risk_fingerprint,
)


class StrategyRiskFingerprintTests(unittest.TestCase):
    def test_breakout_ast_and_config_have_a_specific_profile(self):
        ast = {
            "all": [
                {"op": "breakout", "field": "price"},
                {"op": "gt", "field": "realtime_quote"},
                {"op": "stop_loss", "field": "atr"},
            ]
        }
        config = {"horizon_days": 3, "position_limit": 2}

        result = compile_strategy_risk_fingerprint(ast, config)

        self.assertEqual("breakout", result.archetype)
        self.assertEqual("short", result.holding_horizon)
        self.assertEqual("days", result.signal_half_life)
        self.assertEqual("immediate", result.entry_urgency)
        self.assertEqual("high", result.turnover)
        self.assertEqual("high", result.volatility_sensitivity)
        self.assertEqual("realtime", result.data_freshness)
        self.assertEqual("price", result.natural_stop)
        self.assertEqual("high", result.concentration_risk)

    def test_ambiguous_or_empty_input_is_composite_and_conservative(self):
        result = compile_strategy_risk_fingerprint({}, {})

        self.assertEqual(
            StrategyRiskFingerprint(
                archetype="composite", holding_horizon="conservative",
                signal_half_life="conservative", entry_urgency="conservative",
                turnover="conservative", volatility_sensitivity="conservative",
                data_freshness="conservative", natural_stop="conservative",
                concentration_risk="conservative",
            ),
            result,
        )

    def test_compilation_is_deterministic_and_does_not_mutate_inputs(self):
        ast = {"any": [{"field": "daily_close", "op": "trend"}, {"field": "ma20", "op": "gt"}]}
        config = {"holding_days": 10, "max_positions": 8}
        before = copy.deepcopy((ast, config))

        first = compile_strategy_risk_fingerprint(ast, config)
        second = compile_strategy_risk_fingerprint(ast, config)

        self.assertEqual(first, second)
        self.assertEqual(before, (ast, config))
        self.assertEqual("trend", first.archetype)
        self.assertEqual("swing", first.holding_horizon)
        self.assertEqual("daily_close", first.data_freshness)
        self.assertEqual("medium", first.concentration_risk)

    def test_disabled_declarations_are_not_positive_evidence(self):
        # {"realtime": false} / {"stop_loss": false} disable the features, so
        # they must not produce a realtime/high-turnover/price-stop profile.
        result = compile_strategy_risk_fingerprint(
            None, {"realtime": False, "stop_loss": False, "news_scan": None}
        )

        self.assertEqual("composite", result.archetype)
        self.assertNotEqual("realtime", result.data_freshness)
        self.assertNotEqual("immediate", result.entry_urgency)
        self.assertNotEqual("price", result.natural_stop)
        self.assertNotEqual("high", result.turnover)

    def test_horizon_ignores_unrelated_day_keys(self):
        # cooldown_days/lookback_days are not holding periods; the explicit
        # 10-day horizon must win instead of the 30-day cooldown.
        result = compile_strategy_risk_fingerprint(
            None, {"horizon_days": 10, "cooldown_days": 30, "lookback_days": 60}
        )

        self.assertEqual("swing", result.holding_horizon)
        self.assertEqual("weeks", result.signal_half_life)

    def test_holding_range_is_classified_by_its_upper_bound(self):
        # hold_min=1 / hold_max=8 is an 8-day swing plan; a one-day floor is
        # not intraday evidence on a T+1 market.
        result = compile_strategy_risk_fingerprint(None, {"hold_min": 1, "hold_max": 8})

        self.assertEqual("swing", result.holding_horizon)
        self.assertEqual("weeks", result.signal_half_life)

    def test_concentration_uses_position_counts_not_percentages(self):
        # 20 permitted positions is broad diversification even when each
        # position is capped at 10% weight.
        result = compile_strategy_risk_fingerprint(
            None, {"max_positions": 20, "max_weight": 0.1, "max_drawdown": 0.15, "hold_max": 8}
        )

        self.assertEqual("low", result.concentration_risk)
        self.assertEqual("swing", result.holding_horizon)


if __name__ == "__main__":
    unittest.main()
