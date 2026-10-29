"""Real paper candidate preprocessing forwards the active model's score plan."""
import datetime as dt
import json
import unittest
from contextlib import ExitStack
from unittest import mock

import pandas as pd

import paper_trading as PT
import research_orchestration as RO
import test_factor_calibration_v2 as Fixture


class _SelectionReached(Exception):
    """Stop before selectors persist replay snapshots or produce orders."""


class CandidateFactorWiringTests(unittest.TestCase):
    def capture(self, account, *, live_day=None, hot_rows=(), source_error=None,
                run_selector=False, near_tie=False, research_snapshot=None):
        day = dt.date(2026, 10, 20)
        price, fund = Fixture.FactorTableTests._frames()
        if near_tie:
            price.loc[price.index[-2], ["mom5", "vol_surge", "flow_proxy"]] = [0.059, 1.59, 0.69]
        price["last_date"] = day.isoformat()
        universe = [
            {"code": str(code), "name": "普通样本", "risk_flag": 0}
            for code in price.index
        ]
        real_build = PT.S.build_factor_table
        real_select = PT.S.run_strategy
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
            if run_selector:
                captured["result"] = real_select(model, table, **kwargs)
            raise _SelectionReached

        with ExitStack() as stack:
            patches = (
                (PT, "_selection_inputs", (price, set())),
                (PT, "selection_factor_freshness", {"passed": True}),
                (PT.U, "load_universe", universe),
                (PT.U, "latest_complete_trade_date", day),
                (PT.dfc, "fetch_finance_latest", {"data": {}}),
                (PT.F, "compute_fundamental_factors", fund),
                (PT, "_star_sector_impulse", {}),
                (PT, "_sector_heat_map", {}),
                (PT.F, "live_snapshot_asof", "2026-10-21T10:00:00+08:00"),
            )
            for module, name, result in patches:
                stack.enter_context(mock.patch.object(module, name, return_value=result))
            stack.enter_context(mock.patch.object(PT, "CANDIDATE_FACTOR_MIN_ROWS", 1))
            source = stack.enter_context(mock.patch.object(
                PT.F.dfc, "fetch_hot_rank", return_value=list(hot_rows), side_effect=source_error))
            builder = stack.enter_context(
                mock.patch.object(PT.S, "build_factor_table", side_effect=build)
            )
            selector = stack.enter_context(
                mock.patch.object(PT.S, "run_strategy", side_effect=select)
            )
            stack.enter_context(mock.patch.object(RO, "_capture_time",
                                                  return_value="2026-10-20T15:05:00+08:00"))
            with self.assertRaises(_SelectionReached):
                live = None if live_day is None else [
                    {"code": code, "price": float(price.loc[code, "price"]),
                     "pct": float(fund.loc[code, "pct_today"]),
                     "main_pct": float(price.loc[code, "flow_proxy"]), "super_net": 1.0,
                     "quote_at": live_day + "T10:00:00+08:00"}
                    for code in price.index
                ]
                PT._candidate_rows(account, day, {"light": "green"},
                                   live_universe=live, live_asof_date=live_day,
                                   research_snapshot=research_snapshot)
            builder.assert_called_once()
            selector.assert_called_once()
            captured["source_calls"] = source.call_count

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
                # Historical preparation has no popularity observations;
                # sector sentiment is supplied by its separate heat overlay.
                if name != "sentiment":
                    self.assertTrue(table[name].notna().any(), name)
                self.assertIn(name + "_evidence_quality", table)
        for name in required - {"sentiment"}:
            pd.testing.assert_series_equal(
                captured["built_table"][name], captured["selection_table"][name]
            )

    def test_all_builtin_accounts_forward_their_own_score_requirements(self):
        cases = (
            ("tq_breakout", "first_board_relay", {"flow", "mom_short", "sentiment", "volsurge"}),
            ("trend_pullback", "pullback_reversal",
             {"flow", "mom_short", "quality", "rsi", "value", "volsurge"}),
            ("sector_rotation", "sector_sentiment",
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

    def test_live_popularity_reaches_scores_and_frozen_decision_evidence(self):
        captured = self.capture({"id": "tq_breakout", "params": "{}"},
            live_day="2026-10-21", hot_rows=(
                {"code": "000005", "rank": 1, "rank_chg": 200},
                {"code": "000006", "rank": 100, "rank_chg": 0},
            ), run_selector=True)
        self.assertEqual(captured["source_calls"], 1)
        table = captured["selection_table"]
        self.assertTrue(table.loc[["000005", "000006"], "sentiment"].notna().all())
        self.assertTrue(table.loc["000001":"000004", "sentiment"].isna().all())
        self.assertTrue(table.loc["000001":"000004", "sentiment_evidence_quality"].eq(0).all())
        picks = {p["code"]: p for p in captured["result"]["picks"]}
        self.assertEqual(set(picks), set(table.index))
        weights = PT.S.PAPER_WEIGHTS["first_board_relay"]
        for code, pick in picks.items():
            observed = {k: w for k, w in weights.items() if pd.notna(table.loc[code, k])}
            expected = sum(table.loc[code, k] * w for k, w in observed.items()) / sum(observed.values())
            self.assertAlmostEqual(pick["score_components"]["base_score"], expected, places=5)
            self.assertEqual(pick["score_components"]["weights"], weights)
            self.assertEqual(pick["score_components"]["factor_input_version"], "paper-tq-sentiment-v1")
        self.assertEqual(picks["000005"]["score_components"]["observed_weight"], 1.0)
        self.assertEqual(picks["000001"]["score_components"]["observed_weight"], 0.9)
        snapshot = PT._decision_snapshot({"pick": picks["000005"]},
            asof_date="2026-10-21", decision_at="2026-10-21T10:00:00+08:00", kline={})
        self.assertEqual(snapshot["factors"]["raw"]["sentiment"], picks["000005"]["factor_snapshot"]["sentiment"])
        self.assertEqual(snapshot["factors"]["score_components"]["factor_input_version"], "paper-tq-sentiment-v1")

    def test_empty_or_failed_source_preserves_missing_scores_and_candidates(self):
        baseline = self.capture({"id": "tq_breakout"}, live_day="2026-10-21", run_selector=True)
        failed = self.capture({"id": "tq_breakout"}, live_day="2026-10-21",
                              source_error=RuntimeError("source unavailable"), run_selector=True)
        self.assertEqual(baseline["source_calls"], 1)
        self.assertEqual(failed["source_calls"], 1)
        self.assertEqual(json.dumps(baseline["result"]["picks"], sort_keys=True, default=str),
                         json.dumps(failed["result"]["picks"], sort_keys=True, default=str))
        self.assertTrue(failed["selection_table"]["sentiment"].isna().all())
        for pick in failed["result"]["picks"]:
            self.assertIsNone(pick["factor_snapshot"]["sentiment"])
            self.assertEqual(pick["score_components"]["observed_weight"], 0.9)
        legacy_table = baseline["selection_table"].copy(deep=True)
        legacy_table.attrs.pop("factor_input_version")
        legacy = PT.S.run_strategy("first_board_relay", legacy_table, topn=48, gate={"light": "green"})
        self.assertEqual([p["code"] for p in baseline["result"]["picks"]],
                         [p["code"] for p in legacy["picks"]])
        for current, old in zip(baseline["result"]["picks"], legacy["picks"], strict=True):
            self.assertEqual(current["score"], old["score"])
            self.assertEqual(current["factor_snapshot"], old["factor_snapshot"])
            components = dict(current["score_components"])
            components.pop("factor_input_version")
            self.assertEqual(components, old["score_components"])

    def test_historical_snapshots_and_other_accounts_do_not_read_live_popularity(self):
        for account, live_day in (("tq_breakout", None), ("tq_breakout", "2026-10-20"),
                                  ("trend_pullback", "2026-10-21"),
                                  ("sector_rotation", "2026-10-21")):
            with self.subTest(account=account, live_day=live_day):
                result = self.capture({"id": account}, live_day=live_day)
                self.assertEqual(result["source_calls"], 0)
                if account != "tq_breakout":
                    self.assertNotIn("factor_input_version", result["selection_table"].attrs)
        result = self.capture({"id": "tq_breakout", "style": "sector"}, live_day="2026-10-21")
        self.assertEqual(result["source_calls"], 0)

    def test_popularity_changes_near_tied_candidate_order_without_changing_weights(self):
        before = self.capture({"id": "tq_breakout"}, live_day="2026-10-21",
                              run_selector=True, near_tie=True)["result"]["picks"]
        after = self.capture({"id": "tq_breakout"}, live_day="2026-10-21", near_tie=True,
            hot_rows=({"code": "000005", "rank": 1, "rank_chg": 200},
                      {"code": "000002", "rank": 40, "rank_chg": 0},
                      {"code": "000003", "rank": 50, "rank_chg": 0},
                      {"code": "000004", "rank": 60, "rank_chg": 0},
                      {"code": "000006", "rank": 100, "rank_chg": 0}),
            run_selector=True)["result"]["picks"]
        old_order, new_order = ([p["code"] for p in picks] for picks in (before, after))
        self.assertLess(old_order.index("000006"), old_order.index("000005"))
        self.assertLess(new_order.index("000005"), new_order.index("000006"))
        old = {p["code"]: p for p in before}
        new = {p["code"]: p for p in after}
        for code in ("000005", "000006"):
            self.assertNotEqual(new[code]["score_components"]["base_score"], old[code]["score_components"]["base_score"])
            self.assertEqual(new[code]["score_components"]["weights"], old[code]["score_components"]["weights"])
        for key in ("score", "score_components", "factor_snapshot", "reasons"):
            self.assertEqual(new["000001"][key], old["000001"][key])

    def test_research_uses_the_real_selection_table_and_all_quotes(self):
        import native_paired_replay as NPR

        snapshot = {}
        captured = self.capture({"id": "tq_breakout"}, live_day="2026-10-20",
                                research_snapshot=snapshot)
        session = NPR.session_from_payload(snapshot["frozen_sessions"][0])
        self.assertEqual(dt.datetime.fromisoformat(session.decision_at),
                         dt.datetime.fromisoformat("2026-10-20T15:05:00+08:00"))
        pd.testing.assert_frame_equal(session.factor_table, captured["selection_table"],
                                      check_dtype=False)
        self.assertEqual(set(session.quote_map()), set(captured["selection_table"].index))
        self.assertNotIn("reason", snapshot)

    def test_missing_live_quotes_only_skips_research_not_selection(self):
        snapshot = {}
        captured = self.capture({"id": "tq_breakout"}, research_snapshot=snapshot)
        self.assertIn("frozen_selection_quotes_incomplete", snapshot["reason"])
        self.assertNotIn("frozen_sessions", snapshot)
        self.assertEqual(captured["model"], "first_board_relay")

    def test_an_active_overlay_is_not_misrepresented_as_the_default_research_baseline(self):
        snapshot = {}
        overlay = {"model_family": "first_board_relay",
                   "weights": dict(PT.S.PAPER_WEIGHTS["first_board_relay"])}
        with mock.patch.object(PT, "_adaptive_selection", return_value=overlay):
            captured = self.capture({"id": "tq_breakout"}, live_day="2026-10-20",
                                    research_snapshot=snapshot)
        self.assertIn("research_requires_default_selection_parameters", snapshot["reason"])
        self.assertEqual(captured["weights"], overlay["weights"])
        self.assertNotIn("frozen_sessions", snapshot)


if __name__ == "__main__":
    unittest.main()
