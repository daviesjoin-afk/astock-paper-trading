# -*- coding: utf-8 -*-
from __future__ import annotations

import pathlib
import unittest
from unittest import mock

import numpy as np
import pandas as pd

try:
    import factor_calibration as FC
    import factors as F
    import strategies as S
except ImportError:  # package-style collection
    from . import factor_calibration as FC
    from . import factors as F
    from . import strategies as S


class CalibrationPrimitiveTests(unittest.TestCase):
    def test_weighted_available_renormalizes_observed_components(self):
        idx = ["a", "b"]
        result = FC.weighted_available(
            {
                "x": pd.Series([2.0, np.nan], index=idx),
                "y": pd.Series([4.0, np.nan], index=idx),
            },
            {"x": 0.25, "y": 0.75},
            index=idx,
        )
        self.assertAlmostEqual(float(result.loc["a"]), 3.5)
        self.assertTrue(pd.isna(result.loc["b"]))

    def test_missing_is_not_numeric_neutral(self):
        result = FC.weighted_available(
            {"x": pd.Series([np.nan]), "y": pd.Series([5.0])},
            {"x": 0.9, "y": 0.1},
        )
        self.assertAlmostEqual(float(result.iloc[0]), 5.0)

    def test_alpha_and_evidence_quality_are_independent(self):
        frame_high = pd.DataFrame({
            "a": [1.0], "b": [3.0],
            "a_evidence_quality": [1.0], "b_evidence_quality": [1.0],
        })
        frame_low = frame_high.copy()
        frame_low["a_evidence_quality"] = 0.2
        frame_low["b_evidence_quality"] = 0.4
        high = FC.score_factors(frame_high, {"a": 0.5, "b": 0.5})
        low = FC.score_factors(frame_low, {"a": 0.5, "b": 0.5})
        self.assertAlmostEqual(float(high.alpha_score.iloc[0]), 2.0)
        self.assertAlmostEqual(float(low.alpha_score.iloc[0]), 2.0)
        self.assertAlmostEqual(float(high.evidence_quality.iloc[0]), 1.0)
        self.assertAlmostEqual(float(low.evidence_quality.iloc[0]), 0.3)

    def test_missing_factor_lowers_quality_but_renormalizes_alpha(self):
        frame = pd.DataFrame({"a": [2.0], "b": [np.nan]})
        result = FC.score_factors(frame, {"a": 0.4, "b": 0.6})
        self.assertAlmostEqual(float(result.alpha_score.iloc[0]), 2.0)
        self.assertAlmostEqual(float(result.evidence_quality.iloc[0]), 0.4)
        self.assertAlmostEqual(float(result.observed_weight.iloc[0]), 0.4)

    def test_required_factor_missing_fails_separate_required_gate(self):
        frame = pd.DataFrame({"a": [3.0], "sentiment": [np.nan]})
        result = FC.score_factors(
            frame, {"a": 0.5, "sentiment": 0.5}, required_factors=("sentiment",)
        )
        self.assertFalse(bool(result.required_ok.iloc[0]))
        self.assertAlmostEqual(float(result.alpha_score.iloc[0]), 3.0)

    def test_rank_ic_uses_pairs_and_is_monotonic(self):
        idx = list("abcdef")
        factor = pd.Series([1, 2, 3, 4, 5, 6], index=idx, dtype=float)
        forward = pd.Series([10, 20, 30, 40, 50, 60], index=idx, dtype=float)
        self.assertAlmostEqual(FC.rank_ic(factor, forward), 1.0)
        self.assertIsNone(FC.rank_ic(factor.iloc[:4], forward.iloc[:4]))

    def test_icir_requires_dispersion_and_minimum_periods(self):
        self.assertIsNone(FC.icir([0.1, 0.2]))
        self.assertIsNone(FC.icir([0.1, 0.1, 0.1]))
        value = FC.icir([0.1, 0.2, 0.3])
        self.assertIsNotNone(value)
        self.assertGreater(value, 0)

    def test_group_neutralization_preserves_unknowns(self):
        values = pd.Series([1.0, 3.0, 5.0, np.nan, 9.0], index=list("abcde"))
        groups = pd.Series(["A", "A", "B", "B", None], index=list("abcde"))
        result = FC.neutralize_by_group(values, groups)
        self.assertAlmostEqual(float(result.loc["a"]), -1.0)
        self.assertAlmostEqual(float(result.loc["b"]), 1.0)
        self.assertTrue(pd.isna(result.loc["c"]))  # group B has one observed value
        self.assertTrue(pd.isna(result.loc["d"]))
        self.assertTrue(pd.isna(result.loc["e"]))

    def test_chronological_oos_split_never_shuffles(self):
        labels = list(range(10))
        train, holdout = FC.chronological_oos_split(labels, holdout_fraction=0.40)
        self.assertEqual(train, list(range(6)))
        self.assertEqual(holdout, list(range(6, 10)))


class SourceQualityTests(unittest.TestCase):
    @staticmethod
    def _kline():
        idx = pd.date_range("2026-01-01", periods=80, freq="D")
        close = pd.Series(np.linspace(10.0, 14.0, len(idx)), index=idx)
        return pd.DataFrame({
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.linspace(1_000_000, 1_300_000, len(idx)),
            "amount": np.linspace(10_000_000, 18_000_000, len(idx)),
        }, index=idx)

    def test_unadjusted_source_changes_quality_not_alpha(self):
        klines = {"000001": self._kline()}
        with mock.patch.object(F.dfc, "get_kline_manifest", return_value={
            "000001": {"source": "sina", "adjustment": "none"}
        }):
            unadjusted = F.compute_price_factors(klines)
        with mock.patch.object(F.dfc, "get_kline_manifest", return_value={
            "000001": {"source": "eastmoney", "adjustment": "qfq"}
        }):
            adjusted = F.compute_price_factors(klines)
        for column in ("mom5", "mom20", "mom60", "rev5"):
            self.assertAlmostEqual(
                float(unadjusted.loc["000001", column]),
                float(adjusted.loc["000001", column]),
                places=12,
            )
        self.assertTrue(bool(unadjusted.loc["000001", "adjustment_warning"]))
        self.assertFalse(bool(adjusted.loc["000001", "adjustment_warning"]))
        self.assertEqual(
            float(unadjusted.loc["000001", "price_evidence_quality"]),
            FC.UNADJUSTED_PRICE_QUALITY,
        )
        self.assertEqual(float(adjusted.loc["000001", "price_evidence_quality"]), 1.0)


class FactorTableTests(unittest.TestCase):
    @staticmethod
    def _frames():
        idx = [f"00000{i}" for i in range(1, 7)]
        price = pd.DataFrame(index=idx)
        price["price"] = [10, 11, 12, 13, 14, 15]
        price["amount"] = [10, 20, 30, 40, 50, 60]
        price["turnover"] = [1, 2, 3, 4, 5, 6]
        price["mom5"] = [-0.03, -0.01, 0.0, 0.01, 0.03, 0.06]
        price["mom20"] = [-0.10, -0.05, 0.0, 0.05, 0.10, 0.15]
        price["mom60"] = [-0.20, -0.10, 0.0, 0.10, 0.20, 0.30]
        price["vol_surge"] = [0.7, 0.9, 1.0, 1.1, 1.3, 1.6]
        price["rsi14"] = [30, 40, 45, 55, 60, 70]
        price["flow_proxy"] = [-0.5, -0.2, 0.0, 0.1, 0.3, 0.7]
        price["price_evidence_quality"] = [1.0, 0.7, 1.0, 1.0, 1.0, 1.0]
        price["adjustment_warning"] = [False, True, False, False, False, False]
        for col in S.TECHNICAL_COLUMNS:
            price[col] = False if not col.startswith("ma") else price["price"] * 0.9

        fund = pd.DataFrame(index=idx)
        fund["name"] = [f"股票{i}" for i in range(1, 7)]
        fund["industry"] = ["A", "A", "B", "B", "C", "C"]
        fund["pe"] = [25, 20, 18, 16, 14, 12]
        fund["pb"] = [4, 3.5, 3, 2.5, 2, 1.5]
        fund["roe"] = [5, 6, 7, 8, 9, 10]
        fund["rev_yoy"] = [-5, 0, 5, 10, 15, 20]
        fund["profit_yoy"] = [-10, -5, 0, 10, 20, 30]
        fund["pct_today"] = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
        fund["super_net"] = [1, 2, 3, 4, 5, 6]
        fund["mktcap"] = [100, 110, 120, 130, 140, 150]
        fund["float_cap"] = [80, 90, 100, 110, 120, 130]
        return price, fund

    def test_short_momentum_is_independent_of_medium_horizons(self):
        price, fund = self._frames()
        first = S.build_factor_table(price, fund)
        changed = price.copy()
        changed["mom20"] = changed["mom20"] * -9
        changed["mom60"] = changed["mom60"] * 13
        second = S.build_factor_table(changed, fund)
        pd.testing.assert_series_equal(first["mom_short"], second["mom_short"])
        self.assertFalse(first["mom"].equals(second["mom"]))

    def test_sentiment_unavailable_is_not_momentum_volume_proxy(self):
        price, fund = self._frames()
        table = S.build_factor_table(price, fund, sentiment=None)
        self.assertTrue(table["sentiment"].isna().all())
        self.assertTrue((table["sentiment_evidence_quality"] == 0.0).all())
        self.assertEqual(set(table["sentiment_source"]), {"unavailable"})

    def test_partial_sentiment_stays_missing_not_zero(self):
        price, fund = self._frames()
        sentiment = {price.index[0]: {"hot_rank": 1, "sentiment": 0.9}}
        table = S.build_factor_table(price, fund, sentiment=sentiment)
        self.assertTrue(pd.notna(table.loc[price.index[0], "sentiment"]))
        self.assertTrue(table.loc[price.index[1]:, "sentiment"].isna().all())
        self.assertEqual(float(table.loc[price.index[0], "sentiment_evidence_quality"]), 1.0)
        self.assertEqual(float(table.loc[price.index[1], "sentiment_evidence_quality"]), 0.0)

    def test_proxy_flow_preserves_alpha_and_records_lower_quality(self):
        price, fund = self._frames()
        table = S.build_factor_table(price, fund, realtime_flow=None)
        expected = F.zscore(price["flow_proxy"], fill_missing=False)
        pd.testing.assert_series_equal(table["flow"], expected, check_names=False)
        self.assertTrue((table["flow_evidence_quality"] == FC.PROXY_QUALITY).all())

    def test_partial_live_flow_marks_quality_per_row(self):
        price, fund = self._frames()
        realtime = {price.index[0]: -2.0, price.index[1]: -1.0,
                    price.index[2]: 0.0, price.index[3]: 1.0,
                    price.index[4]: 2.0}
        table = S.build_factor_table(price, fund, realtime_flow=realtime)
        self.assertEqual(float(table.loc[price.index[0], "flow_evidence_quality"]), 1.0)
        self.assertEqual(float(table.loc[price.index[5], "flow_evidence_quality"]), FC.PROXY_QUALITY)
        proxy = F.zscore(price["flow_proxy"], fill_missing=False)
        self.assertAlmostEqual(float(table.loc[price.index[5], "flow"]), float(proxy.iloc[5]))

    def test_price_quality_propagates_without_mutating_factor_alpha(self):
        price, fund = self._frames()
        table = S.build_factor_table(price, fund)
        code = price.index[1]
        self.assertEqual(float(table.loc[code, "mom_short_evidence_quality"]), 0.7)
        self.assertEqual(float(table.loc[code, "mom_evidence_quality"]), 0.7)
        self.assertEqual(table.attrs["factor_calibration_version"], FC.CALIBRATION_VERSION)

    def test_nested_composite_quality_tracks_component_coverage(self):
        price, fund = self._frames()
        code = price.index[0]
        fund.loc[code, "pb"] = np.nan
        fund.loc[code, "profit_yoy"] = np.nan
        fund.loc[code, "rev_yoy"] = np.nan
        price.loc[code, "mom60"] = np.nan
        table = S.build_factor_table(price, fund)
        self.assertAlmostEqual(float(table.loc[code, "value_evidence_quality"]), 0.5)
        self.assertAlmostEqual(float(table.loc[code, "quality_evidence_quality"]), 0.5)
        self.assertAlmostEqual(float(table.loc[code, "mom_evidence_quality"]), 0.6)
        self.assertTrue(pd.notna(table.loc[code, "value"]))
        self.assertTrue(pd.notna(table.loc[code, "quality"]))
        self.assertTrue(pd.notna(table.loc[code, "mom"]))


class RuntimeCalibrationTests(unittest.TestCase):
    def _table(self, with_sentiment=False):
        price, fund = FactorTableTests._frames()
        sentiment = None
        if with_sentiment:
            sentiment = {
                code: {"hot_rank": i + 1, "sentiment": (6 - i) / 6}
                for i, code in enumerate(price.index)
            }
        table = S.build_factor_table(price, fund, sentiment=sentiment)
        # Provide context columns expected by the paper ranking profiles.
        table["main_pct"] = [1, 2, 3, 4, 5, 6]
        table["main_net"] = [1, 2, 3, 4, 5, 6]
        table["sector_heat_score"] = [0.1] * len(table)
        table["sector_early_rotation_score"] = [0.0] * len(table)
        table["sector_early_rotation"] = False
        table["star_sector_bonus"] = 0.0
        return table

    def test_one_to_two_can_use_explicit_remaining_evidence_without_sentiment(self):
        result = S.run_strategy("one_to_two", self._table(False), topn=3, gate={"light": "green"})
        self.assertGreater(result["factor_calibration"]["eligible_evidence_rows"], 0)
        self.assertEqual(result["factor_calibration"]["version"], FC.CALIBRATION_VERSION)
        self.assertGreater(result["count"], 0)
        self.assertLess(result["picks"][0]["score_components"]["evidence_quality"], 1.0)

    def test_sentiment_pioneer_requires_real_sentiment(self):
        result = S.run_strategy(
            "sentiment_pioneer", self._table(False), topn=3, gate={"light": "green"}
        )
        self.assertEqual(result["factor_calibration"]["required_factors"], ["sentiment"])
        self.assertEqual(result["factor_calibration"]["eligible_evidence_rows"], 0)
        self.assertEqual(result["count"], 0)

    def test_sentiment_pioneer_can_run_when_real_sentiment_exists(self):
        result = S.run_strategy(
            "sentiment_pioneer", self._table(True), topn=3, gate={"light": "green"}
        )
        self.assertGreater(result["factor_calibration"]["eligible_evidence_rows"], 0)
        self.assertGreater(result["count"], 0)

    def test_context_bonus_cannot_resurrect_low_evidence(self):
        table = self._table(False)
        for factor in ("mom_short", "flow", "volsurge", "sentiment"):
            table[f"{factor}_evidence_quality"] = 0.0
        table["star_sector_bonus"] = 100.0
        result = S.run_strategy("one_to_two", table, topn=3, gate={"light": "green"})
        self.assertEqual(result["factor_calibration"]["eligible_evidence_rows"], 0)
        self.assertEqual(result["count"], 0)

    def test_score_audit_contract_is_v2(self):
        result = S.run_strategy("one_to_two", self._table(False), topn=1, gate={"light": "green"})
        components = result["picks"][0]["score_components"]
        self.assertEqual(components["version"], "paper-score-evidence-v2")
        self.assertEqual(components["factor_calibration_version"], FC.CALIBRATION_VERSION)
        self.assertIn("evidence_quality", components)
        self.assertIn("observed_weight", components)


class SourceGuardTests(unittest.TestCase):
    def test_legacy_quality_as_alpha_patterns_are_gone(self):
        root = pathlib.Path(__file__).resolve().parent
        factors_source = (root / "factors.py").read_text(encoding="utf-8")
        strategies_source = (root / "strategies.py").read_text(encoding="utf-8")
        self.assertNotIn('row_data[_fcol] = row_data[_fcol] * 0.7', factors_source)
        self.assertNotIn('proxy_flow * 0.5', strategies_source)
        self.assertNotIn('price["vol_surge"] * 0.5 + price["mom20"]', strategies_source)

    def test_hot_leader_winsorization_uses_fraction_units(self):
        source = pathlib.Path(S.__file__).read_text(encoding="utf-8")
        self.assertIn('mom5.clip(-0.20, 0.20)', source)
        self.assertIn('mom20.clip(-0.40, 0.60)', source)
        self.assertNotIn('mom5.clip(-20, 20)', source)
        self.assertNotIn('mom20.clip(-40, 60)', source)

    def test_existing_strategy_weight_tables_are_not_broadly_retuned(self):
        self.assertEqual(S.PAPER_WEIGHTS["one_to_two"], {
            "mom_short": 0.45, "flow": 0.25, "volsurge": 0.20, "sentiment": 0.10,
        })
        self.assertEqual(S.PAPER_WEIGHTS["trend_continuation"], {
            "mom_short": 0.28, "mom": 0.22, "flow": 0.20,
            "volsurge": 0.15, "quality": 0.15,
        })


if __name__ == "__main__":
    unittest.main()
