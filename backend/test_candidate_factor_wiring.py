"""Real paper candidate preprocessing forwards the active model's score plan."""
import datetime as dt
import json
import unittest
from contextlib import ExitStack
from unittest import mock

import pandas as pd

import paper_trading as PT
import test_factor_calibration_v2 as Fixture


class _SelectionReached(Exception):
    """Stop before selectors persist replay snapshots or produce orders."""


class CandidateFactorWiringTests(unittest.TestCase):
    def capture(self, account):
        day = dt.date(2026, 10, 20)
        price, fund = Fixture.FactorTableTests._frames()
        price["last_date"] = day.isoformat()
        universe = [
            {"code": str(code), "name": "普通样本", "risk_flag": 0}
            for code in price.index
        ]
        real_build = PT.S.build_factor_table
        captured = {}

        def build(*args, **kwargs):
            captured["requested"] = kwargs.get("scoring_factors")
            table = real_build(*args, **kwargs)
            captured["built_table"] = table.copy(deep=True)
            return table

        def select(model, table, **kwargs):
            captured["model"] = model
            captured["selection_table"] = table.copy(deep=True)
            captured["weights"] = kwargs.get("weight_overrides")
            raise _SelectionReached

        with ExitStack() as stack:
            patches = (
                (PT, "_selection_inputs", (price, set())),
                (PT, "_selection_factor_freshness", {"passed": True}),
                (PT.U, "load_universe", universe),
                (PT.U, "latest_complete_trade_date", day),
                (PT.dfc, "fetch_finance_latest", {"data": {}}),
                (PT.F, "compute_fundamental_factors", fund),
                (PT, "_star_sector_impulse", {}),
                (PT, "_sector_heat_map", {}),
            )
            for module, name, result in patches:
                stack.enter_context(mock.patch.object(module, name, return_value=result))
            stack.enter_context(mock.patch.object(PT, "CANDIDATE_FACTOR_MIN_ROWS", 1))
            builder = stack.enter_context(
                mock.patch.object(PT.S, "build_factor_table", side_effect=build)
            )
            selector = stack.enter_context(
                mock.patch.object(PT.S, "run_strategy", side_effect=select)
            )
            with self.assertRaises(_SelectionReached):
                PT._candidate_rows(account, day, {"light": "green"})
            builder.assert_called_once()
            selector.assert_called_once()

        self.assertEqual(len(captured["built_table"]), len(price))
        self.assertEqual(len(captured["selection_table"]), len(price))
        self.assertTrue(captured["selection_table"]["price"].gt(0).all())
        return captured

    def assert_plan(self, captured, model, required):
        self.assertEqual(captured["model"], model)
        self.assertEqual(captured["requested"], required)
        optional_scores = {"value", "quality", "mom", "mom_short", "volsurge", "rsi"}
        for table_name in ("built_table", "selection_table"):
            table = captured[table_name]
            self.assertEqual(set(table) & optional_scores, required & optional_scores)
            for name in required:
                self.assertIn(name, table)
                # Paper preparation passes sentiment=None; absent evidence is
                # expected to remain unknown before the sector overlay.
                if name != "sentiment":
                    self.assertTrue(table[name].notna().any(), name)
                self.assertIn(name + "_evidence_quality", table)
        for name in required - {"sentiment"}:
            pd.testing.assert_series_equal(
                captured["built_table"][name], captured["selection_table"][name]
            )

    def test_all_builtin_accounts_forward_their_own_score_requirements(self):
        cases = (
            ("tq_breakout", "one_to_two", {"flow", "mom_short", "sentiment", "volsurge"}),
            ("trend_pullback", "bottom_reversal",
             {"flow", "mom_short", "quality", "rsi", "value", "volsurge"}),
            ("sector_rotation", "sentiment_pioneer",
             {"flow", "mom_short", "sentiment", "volsurge"}),
            ("reported_profit_breakout", "reported_profit_breakout", set()),
            ("main_force_top10", "main_force_top10", set()),
        )
        for account_id, model, required in cases:
            with self.subTest(account_id=account_id):
                captured = self.capture({"id": account_id, "params": "{}"})
                self.assert_plan(captured, model, required)
                self.assertEqual(captured["weights"], {})

    def test_active_model_switch_uses_its_plan_and_real_nondefault_weights(self):
        weights = {"mom_short": 0.24, "mom": 0.20, "flow": 0.24,
                   "volsurge": 0.17, "quality": 0.15}
        self.assertEqual(set(weights), set(PT.S.PAPER_WEIGHTS["trend_continuation"]))
        self.assertNotEqual(weights, PT.S.PAPER_WEIGHTS["trend_continuation"])
        params = {
            "adaptive_selection": {"model_family": "trend_continuation", "weights": weights},
            "adaptive_selection_meta": {"status": "active", "effective_date": "2020-01-01"},
        }
        captured = self.capture({"id": "trend_pullback", "params": json.dumps(params)})
        self.assert_plan(captured, "trend_continuation",
                         {"flow", "mom", "mom_short", "quality", "volsurge"})
        self.assertEqual(captured["weights"], weights)


if __name__ == "__main__":
    unittest.main()
