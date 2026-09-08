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


if __name__ == "__main__":
    unittest.main()
