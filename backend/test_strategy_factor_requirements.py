"""Strategy-specific score preparation preserves decisions and common guards."""
import copy
import math
import unittest
from unittest import mock

import pandas as pd

import factor_calibration as FC
import strategies as S
import test_factor_calibration_v2 as Fixture


def comparable(value):
    """Treat independently allocated NaNs as the same missing observation."""
    if isinstance(value, dict):
        return {key: comparable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [comparable(item) for item in value]
    return None if isinstance(value, float) and math.isnan(value) else value


class StrategyFactorRequirementsTests(unittest.TestCase):
    def inputs(self):
        price, fund = Fixture.FactorTableTests._frames()
        sentiment = {code: {"hot_rank": index+1, "sentiment": index/6}
                     for index, code in enumerate(price.index)}
        return price, fund, sentiment

    def test_plans_follow_existing_model_weights_and_unknown_extensions_keep_full_input(self):
        for model, weights in S.PAPER_WEIGHTS.items():
            self.assertEqual(S.scoring_factor_inputs(model), set(weights))
        self.assertEqual(S.scoring_factor_inputs("main_force_top10"), set())
        self.assertEqual(S.scoring_factor_inputs("reported_profit_breakout"), set())
        self.assertIsNone(S.scoring_factor_inputs("custom_extension"))
        with self.assertRaisesRegex(ValueError, "unknown"):
            S.build_factor_table(*self.inputs()[:2], scoring_factors={"typo"})

    def test_unrequested_score_columns_are_not_computed_and_required_columns_match(self):
        price, fund, sentiment = self.inputs()
        with mock.patch.object(S.F, "zscore", wraps=S.F.zscore) as complete:
            full = S.build_factor_table(price, fund, sentiment=sentiment)
        full_calls = complete.call_count
        optional = {"value", "quality", "mom", "mom_short", "volsurge", "rsi"}
        for model in (*S.PAPER_WEIGHTS, "reported_profit_breakout", "main_force_top10"):
            with self.subTest(model=model), mock.patch.object(S.F, "zscore", wraps=S.F.zscore) as normalize:
                required = S.scoring_factor_inputs(model)
                actual = S.build_factor_table(price, fund, sentiment=sentiment, scoring_factors=required)
                self.assertEqual(set(actual) & optional, required & optional)
                for name in required:
                    pd.testing.assert_series_equal(actual[name], full[name])
                    pd.testing.assert_series_equal(actual[name+"_evidence_quality"], full[name+"_evidence_quality"])
                # All price, security, disclosure and raw technical facts survive.
                raw_columns = [name for name in full if name not in FC.FACTOR_GROUPS
                               and not name.endswith("_evidence_quality") and name != "sentiment_source"]
                pd.testing.assert_frame_equal(actual[raw_columns], full[raw_columns])
                self.assertLess(normalize.call_count, full_calls)

    def test_same_weighted_strategy_decisions_quality_and_overrides_with_subset_preparation(self):
        price, fund, sentiment = self.inputs()
        full = S.build_factor_table(price, fund, sentiment=sentiment)
        for model in S.PAPER_WEIGHTS:
            for overrides in (None, dict(S.PAPER_WEIGHTS[model])):
                required = S.scoring_factor_inputs(model)
                subset = S.build_factor_table(price, fund, sentiment=sentiment, scoring_factors=required)
                for table in (full, subset):
                    table["sector_early_rotation_score"] = 0.
                    table["sector_heat_score"] = 0.1
                kwargs = dict(topn=3, gate={"light": "green"}, weight_overrides=overrides)
                expected = S.run_strategy(model, full, **kwargs)
                actual = S.run_strategy(model, subset, **kwargs)
                self.assertGreater(expected["count"], 0, "positive control must have candidates")
                # Only the unused score/audit entries intentionally disappear.
                for result in (expected, actual):
                    for pick in result["picks"]:
                        snapshot = pick.get("factor_snapshot", {})
                        for name in set(FC.FACTOR_GROUPS) - required:
                            snapshot.pop(name, None)
                            snapshot.pop(name+"_evidence_quality", None)
                self.assertEqual(comparable(actual), comparable(expected))

    def test_unused_financial_score_inputs_can_be_absent_but_missing_required_factors_stay_unknown(self):
        price, fund, _ = self.inputs()
        fund = fund.drop(columns=["pe", "pb", "roe", "rev_yoy", "profit_yoy"])
        original = copy.deepcopy(fund)
        table = S.build_factor_table(price, fund, scoring_factors=S.scoring_factor_inputs("one_to_two"))
        self.assertNotIn("quality", table)
        self.assertNotIn("value", table)
        pd.testing.assert_frame_equal(fund, original)
        # Sentiment is a required gate for this model, even in selective mode.
        sector = S.build_factor_table(price, fund, scoring_factors=S.scoring_factor_inputs("sentiment_pioneer"))
        result = S.run_strategy("sentiment_pioneer", sector, gate={"light": "green"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["factor_calibration"]["eligible_evidence_rows"], 0)

    def test_raw_rule_strategies_keep_candidates_without_unrelated_calibrated_scores(self):
        price, fund, _ = self.inputs()
        price["amount"] = 4e8
        price["main_pct"] = 3.
        price["main_net"] = 1e8
        for column in ("three_up", "boll_mid_breakout", "above_ma5_5d", "above_all_ma"):
            price[column] = True
        fund["profit_source"] = "reported"
        fund["net_profit"] = 1e8
        fund["report_date"] = "2026-06-30"
        fund["report_published_at"] = "2026-08-01"
        live_super = {code: 1e7 for code in price.index}
        full = S.build_factor_table(price, fund, realtime_super_flow=live_super)
        for model in ("reported_profit_breakout", "main_force_top10"):
            subset = S.build_factor_table(price, fund, realtime_super_flow=live_super,
                                          scoring_factors=S.scoring_factor_inputs(model))
            expected = S.run_strategy(model, full, topn=3, gate={"light": "green"})
            actual = S.run_strategy(model, subset, topn=3, gate={"light": "green"})
            self.assertGreater(expected["count"], 0)
            self.assertEqual(comparable(actual), comparable(expected))
