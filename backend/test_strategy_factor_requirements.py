"""Strategy-specific score preparation preserves decisions and common guards."""
import copy
from dataclasses import replace
import math
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

import factor_calibration as FC
import strategies as S
import strategy_plugins as SP
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
        for model in ("three_day", "five_day", "ten_day", "main_force_top10", "reported_profit_breakout"):
            self.assertEqual(S.scoring_factor_inputs(model), set())
        self.assertIsNone(S.scoring_factor_inputs("custom_extension"))
        with self.assertRaisesRegex(ValueError, "unknown"):
            S.build_factor_table(*self.inputs()[:2], scoring_factors={"typo"})

    def test_replacement_plugin_runner_keeps_full_input_and_restores_builtin_plan(self):
        price, fund, sentiment = self.inputs()
        for selector in ("first_board_relay", "main_force_top10"):
            original = SP.plugin_for_selector(selector)
            original_plan = S.scoring_factor_inputs(selector)
            self.assertIsNotNone(original)
            self.assertIsNone(original.candidate_runner)
            self.assertIsNotNone(original_plan)
            for declaration, consumed in (("quality", ("quality",)),
                                          ("mom_short", ("quality", "value"))):
                with self.subTest(selector=selector, declaration=declaration):
                    def runner(table, _consumed=consumed, _selector=selector, **_kwargs):
                        scores = sum(table[name] for name in _consumed).sort_values(ascending=False)
                        picks = [{"code": str(code), "score": float(score)}
                                 for code, score in scores.items()]
                        return {"strategy": _selector, "count": len(picks), "picks": picks}

                    replacement = SP.StrategyPlugin(
                        original.strategy_id, selector, (declaration,), candidate_runner=runner,
                    )
                    SP.register_plugin(replacement, replace=True)
                    try:
                        full = S.build_factor_table(price, fund, sentiment=sentiment)
                        expected = S.run_strategy(selector, full)
                        self.assertGreater(expected["count"], 0)
                        # Regression control: the previous static plan genuinely
                        # drops a column consumed by this registered runner.
                        legacy_subset = S.build_factor_table(
                            price, fund, sentiment=sentiment, scoring_factors=original_plan,
                        )
                        with self.assertRaises(KeyError):
                            S.run_strategy(selector, legacy_subset)
                        # Neither a plausible factor name nor the old selector
                        # weights prove all dependencies of the active runner.
                        self.assertIsNone(S.scoring_factor_inputs(selector))
                        actual_table = S.build_factor_table(
                            price, fund, sentiment=sentiment,
                            scoring_factors=S.scoring_factor_inputs(selector),
                        )
                        pd.testing.assert_frame_equal(actual_table, full)
                        self.assertEqual(comparable(S.run_strategy(selector, actual_table)),
                                         comparable(expected))
                    finally:
                        SP.register_plugin(original, replace=True)
                self.assertIs(SP.plugin_for_selector(selector), original)
                self.assertEqual(S.scoring_factor_inputs(selector), original_plan)

    def test_plugin_subclass_dispatch_keeps_full_input_without_declared_runner(self):
        class QualityPlugin(SP.StrategyPlugin):
            def _run_candidate(self, table, **_kwargs):
                picks = [{"code": str(code), "score": float(score)}
                         for code, score in table["quality"].sort_values(ascending=False).items()]
                return {"strategy": self.selector_id, "count": len(picks), "picks": picks}

        price, fund, sentiment = self.inputs()
        original = SP.plugin_for_selector("first_board_relay")
        original_plan = S.scoring_factor_inputs("first_board_relay")
        replacement = QualityPlugin(original.strategy_id, original.selector_id, ("quality",))
        self.assertIsNone(replacement.candidate_runner)
        SP.register_plugin(replacement, replace=True)
        try:
            full = S.build_factor_table(price, fund, sentiment=sentiment)
            expected = S.run_strategy("first_board_relay", full)
            self.assertGreater(expected["count"], 0)
            legacy_subset = S.build_factor_table(
                price, fund, sentiment=sentiment, scoring_factors=original_plan,
            )
            with self.assertRaises(KeyError):
                S.run_strategy("first_board_relay", legacy_subset)
            self.assertIsNone(S.scoring_factor_inputs("first_board_relay"))
            actual_table = S.build_factor_table(
                price, fund, sentiment=sentiment,
                scoring_factors=S.scoring_factor_inputs("first_board_relay"),
            )
            pd.testing.assert_frame_equal(actual_table, full)
            self.assertEqual(comparable(S.run_strategy("first_board_relay", actual_table)),
                             comparable(expected))
        finally:
            SP.register_plugin(original, replace=True)
        self.assertIs(SP.plugin_for_selector("first_board_relay"), original)
        self.assertEqual(S.scoring_factor_inputs("first_board_relay"), original_plan)

    def test_native_plugin_added_replay_factor_retains_real_candidate_evidence(self):
        price, fund, sentiment = self.inputs()
        price["amount"] = 4e8
        price["main_pct"] = 3.
        price["main_net"] = 1e8
        price["above_all_ma"] = True
        with tempfile.TemporaryDirectory(prefix="astock-factor-replay-") as temporary, mock.patch.dict(
            os.environ, {"ASTOCK_CACHE_DIR": temporary, "ASTOCK_GIT_COMMIT": "abcdef123456"},
        ):
            for selector, evidence in (("first_board_relay", "quality"),
                                       ("first_board_relay", "quality_evidence_quality"),
                                       ("main_force_top10", "sentiment_source")):
                with self.subTest(selector=selector, evidence=evidence):
                    original = SP.plugin_for_selector(selector)
                    original_plan = S.scoring_factor_inputs(selector)
                    self.assertTrue(SP.is_original_builtin_plugin(original))
                    replacement = replace(original, factor_inputs=original.factor_inputs + (evidence,))
                    self.assertIsNone(replacement.candidate_runner)
                    self.assertIs(type(replacement), SP.StrategyPlugin)
                    SP.register_plugin(replacement, replace=True)
                    try:
                        self.assertFalse(SP.is_original_builtin_plugin(original))
                        self.assertFalse(SP.is_original_builtin_plugin(replacement))
                        full = S.build_factor_table(price, fund, sentiment=sentiment)
                        kwargs = dict(topn=3, gate={"light": "green"},
                                      replay_data_date="2026-10-20", replay_required=True)
                        expected = S.run_strategy(selector, full, **kwargs)
                        self.assertGreater(expected["count"], 0)
                        legacy_subset = S.build_factor_table(
                            price, fund, sentiment=sentiment, scoring_factors=original_plan,
                        )
                        lost = S.run_strategy(selector, legacy_subset, **kwargs)
                        self.assertGreater(lost["count"], 0)
                        lost_value = "unavailable" if evidence == "sentiment_source" else None
                        self.assertTrue(all(pick["candidate_trace"]["factor_snapshot"][evidence] == lost_value
                                            for pick in lost["picks"]))
                        self.assertIsNone(S.scoring_factor_inputs(selector))
                        actual_table = S.build_factor_table(
                            price, fund, sentiment=sentiment,
                            scoring_factors=S.scoring_factor_inputs(selector),
                        )
                        pd.testing.assert_frame_equal(actual_table, full)
                        actual = S.run_strategy(selector, actual_table, **kwargs)
                        self.assertEqual(comparable(actual), comparable(expected))
                        for pick in actual["picks"]:
                            value = pick["candidate_trace"]["factor_snapshot"][evidence]
                            self.assertIsNotNone(value)
                            self.assertNotEqual(value, lost_value)
                            self.assertEqual(value, full.loc[pick["code"], evidence])
                    finally:
                        SP.register_plugin(original, replace=True)
                self.assertTrue(SP.is_original_builtin_plugin(original))
                self.assertIs(SP.plugin_for_selector(selector), original)
                self.assertEqual(S.scoring_factor_inputs(selector), original_plan)

    def test_equal_copy_of_builtin_binding_conservatively_keeps_full_input(self):
        original = SP.plugin_for_selector("first_board_relay")
        original_plan = S.scoring_factor_inputs("first_board_relay")
        replacement = replace(original)
        self.assertEqual(replacement, original)
        SP.register_plugin(replacement, replace=True)
        try:
            self.assertFalse(SP.is_original_builtin_plugin(replacement))
            self.assertIsNone(S.scoring_factor_inputs("first_board_relay"))
        finally:
            SP.register_plugin(original, replace=True)
        self.assertTrue(SP.is_original_builtin_plugin(original))
        self.assertEqual(S.scoring_factor_inputs("first_board_relay"), original_plan)

    def test_unregistered_known_legacy_selector_retains_its_static_plan(self):
        original = SP.plugin_for_selector("first_board_relay")
        SP.unregister_plugin(original.strategy_id)
        try:
            self.assertIsNone(SP.plugin_for_selector("first_board_relay"))
            self.assertFalse(SP.is_original_builtin_plugin(original))
            self.assertEqual(S.scoring_factor_inputs("first_board_relay"), set(S.PAPER_WEIGHTS["first_board_relay"]))
            self.assertIsNone(S.scoring_factor_inputs("unregistered_extension"))
        finally:
            SP.register_plugin(original, replace=True)
        self.assertTrue(SP.is_original_builtin_plugin(original))

    def test_unrequested_score_columns_are_not_computed_and_required_columns_match(self):
        price, fund, sentiment = self.inputs()
        with mock.patch.object(S.F, "zscore", wraps=S.F.zscore) as complete:
            full = S.build_factor_table(price, fund, sentiment=sentiment)
        full_calls = complete.call_count
        optional = {"value", "quality", "mom", "mom_short", "volsurge", "rsi"}
        for model in (*S.PAPER_WEIGHTS, "three_day", "five_day", "ten_day",
                      "reported_profit_breakout", "main_force_top10"):
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
        nondefault_weights = {
            "first_board_relay": {"mom_short": .40, "flow": .25, "volsurge": .25, "sentiment": .10},
            "pullback_reversal": {"value": .26, "quality": .20, "volsurge": .24,
                                "flow": .12, "mom_short": .10, "rsi": .08},
            "trend_continuation": {"mom_short": .24, "mom": .20, "flow": .24,
                                   "volsurge": .17, "quality": .15},
            "sector_sentiment": {"sentiment": .38, "flow": .27, "mom_short": .15, "volsurge": .20},
        }
        for model in S.PAPER_WEIGHTS:
            for overrides in (None, dict(S.PAPER_WEIGHTS[model]), nondefault_weights[model]):
                required = S.scoring_factor_inputs(model)
                subset = S.build_factor_table(price, fund, sentiment=sentiment, scoring_factors=required)
                for table in (full, subset):
                    table["sector_early_rotation_score"] = 0.
                    table["sector_heat_score"] = 0.1
                kwargs = dict(topn=3, gate={"light": "green"}, weight_overrides=overrides)
                expected = S.run_strategy(model, full, **kwargs)
                actual = S.run_strategy(model, subset, **kwargs)
                self.assertGreater(expected["count"], 0, "positive control must have candidates")
                self.assertFalse(actual["factor_calibration"]["weight_override_rejected"])
                if overrides is nondefault_weights[model]:
                    self.assertNotEqual(actual["weights_used"], S.PAPER_WEIGHTS[model])
                    self.assertEqual(actual["weights_used"], overrides)
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
        table = S.build_factor_table(price, fund, scoring_factors=S.scoring_factor_inputs("first_board_relay"))
        self.assertNotIn("quality", table)
        self.assertNotIn("value", table)
        pd.testing.assert_frame_equal(fund, original)
        # Sentiment is a required gate for this model, even in selective mode.
        sector = S.build_factor_table(price, fund, scoring_factors=S.scoring_factor_inputs("sector_sentiment"))
        result = S.run_strategy("sector_sentiment", sector, gate={"light": "green"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["factor_calibration"]["eligible_evidence_rows"], 0)

    def test_raw_rule_strategies_keep_candidates_without_unrelated_calibrated_scores(self):
        price, fund, _ = self.inputs()
        price["amount"] = 4e8
        price["main_pct"] = 3.
        price["main_net"] = 1e8
        for column in ("three_up", "boll_mid_breakout", "above_ma5_5d", "above_all_ma",
                       "above_ma10_5d", "above_boll_mid", "above_ma60", "monthly_oversold"):
            price[column] = True
        fund["profit_source"] = "reported"
        fund["net_profit"] = 1e8
        fund["report_date"] = "2026-06-30"
        fund["report_published_at"] = "2026-08-01"
        live_super = {code: 1e7 for code in price.index}
        full = S.build_factor_table(price, fund, realtime_super_flow=live_super)
        for model in ("three_day", "five_day", "ten_day", "reported_profit_breakout", "main_force_top10"):
            subset = S.build_factor_table(price, fund, realtime_super_flow=live_super,
                                          scoring_factors=S.scoring_factor_inputs(model))
            expected = S.run_strategy(model, full, topn=3, gate={"light": "green"})
            actual = S.run_strategy(model, subset, topn=3, gate={"light": "green"})
            with self.subTest(model=model, case="formal"):
                self.assertGreater(expected["count"], 0)
                self.assertEqual(comparable(actual), comparable(expected))
            for case in ("shadow", "news_veto"):
                expected_table, actual_table = full.copy(), subset.copy()
                kwargs = dict(topn=3, gate={"light": "green"})
                if case == "shadow":
                    for table in (expected_table, actual_table):
                        table["profit_source"] = "unknown"
                else:
                    kwargs["news_hits"] = [{"code": code, "tone": -1} for code in price.index]
                expected = S.run_strategy(model, expected_table, **kwargs)
                actual = S.run_strategy(model, actual_table, **kwargs)
                with self.subTest(model=model, case=case):
                    self.assertEqual(comparable(actual), comparable(expected))
                    if case == "shadow" and model in {"three_day", "five_day", "reported_profit_breakout"}:
                        self.assertEqual(expected["count"], 0)
                        self.assertGreater(expected["shadow_candidate_count"], 0)
                        self.assertTrue(all(not pick["execution_allowed"] for pick in expected["shadow_picks"]))
                    elif case == "news_veto":
                        self.assertEqual(expected["count"], 0)
                        self.assertGreater(len(expected["news_vetoed"]), 0)

            # Skipping calibrated scores must preserve every raw technical veto.
            raw_guards = {
                "three_day": ("three_up", "boll_mid_breakout"),
                "five_day": ("above_ma10_5d", "above_boll_mid", "above_ma60", "monthly_oversold"),
                "ten_day": ("above_ma10_5d", "above_all_ma", "monthly_oversold"),
            }
            for column in raw_guards.get(model, ()):
                for missing in (False, True):
                    with self.subTest(model=model, blocked_column=column, missing=missing):
                        expected_table, actual_table = full.copy(), subset.copy()
                        for table in (expected_table, actual_table):
                            if missing:
                                table.drop(columns=[column], inplace=True)
                            else:
                                table[column] = False
                        expected = S.run_strategy(model, expected_table, topn=3)
                        actual = S.run_strategy(model, actual_table, topn=3)
                        self.assertEqual(expected["count"], 0)
                        self.assertEqual(expected["shadow_candidate_count"], 0)
                        self.assertEqual(comparable(actual), comparable(expected))
