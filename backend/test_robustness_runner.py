"""R30 contract, owner binding, regime, and scenario execution regressions."""
from __future__ import annotations

import sqlite3
import sys
import math
import random
from dataclasses import replace
from pathlib import Path
import unittest
from unittest import mock

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import experiment_contract as EC
import experiment_execution_model as EM
import experiment_validation_repository as EVR
import historical_market_archive as HMA
import historical_session_calendar as HSC
import historical_universe_archive as HUA
import robustness_contract as RC
import robustness_regimes as RR
import robustness_runner as RUN
import strategy_registry as SR
import tradability_archive as TA
import test_experiment_execution_model as EXEC


SESSIONS = ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09")
CODE = "600000.SH"
BENCHMARK = "000001.SH"
RUN_KEY = "d" * 64


def _policy():
    return {"policy_version": "r30-regime-v1", "benchmark_symbol": BENCHMARK,
            "trend_window_sessions": 3, "bull_threshold": 0.01, "bear_threshold": 0.01,
            "volatility_window_sessions": 3, "high_vol_threshold": 0.02,
            "low_vol_threshold": 0.005}


def _plan(**changes):
    values = {"baseline_run_key": RUN_KEY,
              "baseline_experiment_fingerprint": "f" * 64,
              "random_seed": 7, "regime_policy": _policy(),
              "cost_stresses": [{"commission_multiplier": 2}],
              "slippage_stresses": [{"multiplier": 2}],
              "execution_delay_stresses": [], "signal_delay_stresses": [],
              "liquidity_stresses": [{"liquidity_multiplier": 0.5}],
              "missing_data_stresses": [{"missing_fraction": 0.2, "field_scope": "whole_bar"}],
              "parameter_stresses": [], "start_date_stresses": [],
              "end_date_stresses": [], "universe_stresses": [{"drop_fraction": 0.0}]}
    values.update(changes)
    return RC.RobustnessPlan(**values)


def _bar(code, session, close):
    return {"code": code, "session": session, "open": close, "high": close + 1,
            "low": close - 1, "close": close, "volume": 100000, "amount": 1000000}


class RobustnessFixture:
    def __init__(self):
        self.market_conn = sqlite3.connect(":memory:")
        self.universe_conn = sqlite3.connect(":memory:")
        self.tradability_conn = sqlite3.connect(":memory:")
        for connection in (self.market_conn, self.universe_conn, self.tradability_conn):
            connection.row_factory = sqlite3.Row
        self.market = HMA.HistoricalMarketArchiveRepository(self.market_conn)
        self.universe = HUA.HistoricalUniverseArchiveRepository(self.universe_conn)
        self.tradability = TA.TradabilityArchiveRepository(self.tradability_conn)
        self.tradability.ensure_schema()
        self.baseline_sessions = SESSIONS[1:-1]
        bars = []
        benchmark_closes = (100.0, 102.0, 99.0, 96.0, 98.0)
        for day, benchmark_close in zip(SESSIONS, benchmark_closes, strict=True):
            bars.append(_bar(BENCHMARK, day, benchmark_close))
            bars.append(_bar(CODE, day, 10.0))
            observed = "2026-01-04T09:00:00+08:00" if day == SESSIONS[0] else f"{day}T09:00:00+08:00"
            self.tradability.save(TA.TradabilityEvidence(
                code=CODE, session_date=day, is_listed=True, listing_date="2020-01-01",
                delisting_date=None, is_st=False, is_suspended=False,
                suspension_reason=None, has_market_quote=True, has_trade_volume=True,
                is_price_limit_locked=False, price_limit_direction=None,
                source="fixture-owner", observed_at=observed,
                effective_at=f"{day}T09:30:00+08:00"))
        self.market_manifest = self.market.import_raw_market_archive(
            bars, source="r30-fixture", source_revision="1", adjustment="raw",
            benchmark_calendars={BENCHMARK: {
                "coverage_start": SESSIONS[0], "coverage_end": SESSIONS[-1],
                "sessions": list(SESSIONS), "source": "r30-calendar-owner",
                "source_revision": "1"}})
        self.universe_manifest = self.universe.import_historical_security_master([{
            "code": CODE, "listed_from": "2020-01-01", "delisted_at": None,
            "security_type": "equity", "exchange": "SH",
            "observed_at": "2020-01-01T09:00:00+08:00",
        }], coverage_start="2020-01-01", coverage_end=SESSIONS[-1],
            source="r30-fixture", source_revision="1")
        base = EXEC._spec()
        self.calendar = HSC.issue_from_market_archive(
            self.market, archive_fingerprint=self.market_manifest.archive_fingerprint,
            benchmark_symbol=BENCHMARK, start=self.baseline_sessions[0], end=self.baseline_sessions[-1])
        self.spec = replace(base,
            market_data_fingerprint=self.market_manifest.archive_fingerprint,
            universe_fingerprint=self.universe_manifest.universe_archive_fingerprint,
            start_date=self.baseline_sessions[0], end_date=self.baseline_sessions[-1],
            parameter_set={**base.parameter_set,
                           "validation_calendar_fingerprint": self.calendar.calendar_fingerprint})
        self.strategy = SR.StrategyVersion(
            strategy_id=self.spec.strategy.strategy_id, version=self.spec.strategy.version,
            checksum=self.spec.strategy.checksum,
            definition={"dsl_ast": {"op": "strategy", "rule": {
                "op": "gt", "left": {"op": "field", "name": "close"},
                "right": {"op": "const", "value": 1000}}}},
            created_at="2026-01-01T00:00:00+00:00", created_by="test")
        members = {session: [{"code": CODE}] for session in self.baseline_sessions}
        security_bars = [row for row in bars if row["code"] == CODE
                         and row["session"] in self.baseline_sessions]
        open_facts = self.tradability.evidence_many({
            (CODE, session): f"{session}T09:30:00+08:00" for session in self.baseline_sessions})
        metrics = EM.simulate(self.spec, ast=self.strategy.definition["dsl_ast"],
            sessions=self.baseline_sessions, members_by_session=members, bars=security_bars,
            tradability_repository=self.tradability, tradability_evidence=open_facts)
        result = EC.ExperimentResult(
            experiment_fingerprint=self.spec.fingerprint, status="completed",
            total_return=metrics["total_return"], max_drawdown=metrics["max_drawdown"],
            volatility=metrics["volatility"], turnover=metrics["turnover"],
            trade_count=metrics["trade_count"], total_cost=metrics["total_cost"],
            exposure=metrics["exposure"], capacity_proxy=metrics["capacity_proxy"],
            data_coverage=metrics["data_coverage"], regime_breakdown=metrics["regime_breakdown"])
        validation_evidence = {"experiment_fingerprint": self.spec.fingerprint,
                               "status": "ready", "reason_codes": []}
        self.run = {"id": 1, "run_key": "" ,
            "experiment_fingerprint": self.spec.fingerprint,
            "strategy_id": self.spec.strategy.strategy_id,
            "strategy_version": self.spec.strategy.version,
            "strategy_checksum": self.spec.strategy.checksum,
            "calendar_fingerprint": self.calendar.calendar_fingerprint,
            "universe_archive_fingerprint": self.spec.universe_fingerprint,
            "financial_archive_fingerprint": None,
            "tradability_evidence_fingerprint": "c" * 64,
            "market_archive_fingerprint": self.spec.market_data_fingerprint,
            "dataset_fingerprint": self.spec.dataset_fingerprint,
            "validation_status": "ready", "validation_evidence": validation_evidence,
            "result": result.projection(),
            "runner_version": "r29-pit-validation-runner-v1",
            "runner_code_revision": self.spec.code_revision}
        owner_projection = {
            "calendar_fingerprint": self.run["calendar_fingerprint"],
            "universe_archive_fingerprint": self.run["universe_archive_fingerprint"],
            "tradability_evidence_fingerprint": self.run["tradability_evidence_fingerprint"],
            "market_archive_fingerprint": self.run["market_archive_fingerprint"],
            "financial_archive_fingerprint": self.run["financial_archive_fingerprint"],
            "dataset_fingerprint": self.run["dataset_fingerprint"],
            "strategy_version": self.spec.strategy.projection(),
            "validation_evidence_fingerprint": RUN._sha(validation_evidence),
        }
        self.run["run_key"] = EVR.ExperimentValidationRepository.build_run_key(
            self.spec.fingerprint, owner_projection, RUN.R29_RUNNER_VERSION)
        self.plan = _plan(baseline_run_key=self.run["run_key"],
                          baseline_experiment_fingerprint=self.spec.fingerprint)

    def close(self):
        self.market_conn.close(); self.universe_conn.close(); self.tradability_conn.close()

    def execute(self, plan=None, *, strategy=None, created_at="2026-01-10T00:00:00Z"):
        return RUN.run_robustness(baseline_run=self.run, spec=self.spec,
            plan=plan or self.plan, strategy_version=strategy or self.strategy,
            session_calendar=self.calendar, market_archive_repository=self.market,
            universe_archive_repository=self.universe,
            tradability_repository=self.tradability, created_at=created_at)


class RobustnessContractTests(unittest.TestCase):
    def test_R30_01_same_plan_has_same_fingerprint(self):
        self.assertEqual(_plan().fingerprint, _plan().fingerprint)

    def test_R30_02_mapping_key_order_does_not_change_plan_fingerprint(self):
        first = _plan(regime_policy=_policy())
        second = _plan(regime_policy=dict(reversed(list(_policy().items()))))
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_R30_03_seed_changes_plan_fingerprint(self):
        self.assertNotEqual(_plan().fingerprint, _plan(random_seed=8).fingerprint)

    def test_R30_04_stress_value_changes_plan_fingerprint(self):
        self.assertNotEqual(_plan(cost_stresses=[{"commission_multiplier": 2}]).fingerprint,
                            _plan(cost_stresses=[{"commission_multiplier": 3}]).fingerprint)

    def test_R30_05_created_at_is_outside_plan_identity(self):
        self.assertEqual(_plan(created_at="2026-01-01").fingerprint,
                         _plan(created_at="2026-02-01").fingerprint)

    def test_R30_06_nonfinite_numbers_are_rejected(self):
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _plan(cost_stresses=[{"commission_multiplier": value}])

    def test_R30_07_boolean_numeric_stress_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "finite number"):
            _plan(cost_stresses=[{"commission_multiplier": True}])

    def test_R30_08_unknown_scenario_category_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "scenario_category_invalid"):
            RC.RobustnessScenario(scenario_id="x", category="optimization",
                scenario_version=RC.SCENARIO_VERSION, parameters={}, seed=1,
                baseline_run_key="a" * 64,
                baseline_experiment_fingerprint="a" * 64)

    def test_R30_09_scenario_fingerprint_binds_baseline_experiment(self):
        one = RC.RobustnessScenario(scenario_id="cost", category="cost",
            scenario_version=RC.SCENARIO_VERSION, parameters={"commission_multiplier": 2}, seed=1,
            baseline_run_key=RUN_KEY,
            baseline_experiment_fingerprint="a" * 64)
        two = RC.RobustnessScenario(scenario_id="cost", category="cost",
            scenario_version=RC.SCENARIO_VERSION, parameters={"commission_multiplier": 2}, seed=1,
            baseline_run_key=RUN_KEY,
            baseline_experiment_fingerprint="b" * 64)
        self.assertNotEqual(one.fingerprint, two.fingerprint)
        self.assertEqual("a" * 64, one.projection()["baseline_experiment_fingerprint"])
        other_owner_run = RC.RobustnessScenario(scenario_id="cost", category="cost",
            scenario_version=RC.SCENARIO_VERSION, parameters={"commission_multiplier": 2}, seed=1,
            baseline_run_key="e" * 64,
            baseline_experiment_fingerprint="a" * 64)
        self.assertNotEqual(one.fingerprint, other_owner_run.fingerprint)

    def test_R30_10_report_fingerprint_is_deterministic(self):
        args = {"baseline_identity": {"run_key": "a" * 64},
                "plan_fingerprint": "b" * 64,
                "cases": [{"scenario_fingerprint": "c" * 64, "status": "completed"}]}
        self.assertEqual(RC.report_fingerprint(**args), RC.report_fingerprint(**args))
        changed = {**args, "baseline_identity": {"run_key": "a" * 64,
            "result_fingerprint": "d" * 64}}
        self.assertNotEqual(RC.report_fingerprint(**args), RC.report_fingerprint(**changed))


class RobustnessRunnerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = RobustnessFixture()
        self.addCleanup(self.fixture.close)

    def test_R30_11_noncanonical_run_cannot_be_baseline(self):
        self.fixture.run["runner_version"] = "legacy-backtest-v1"
        with self.assertRaisesRegex(RUN.RobustnessBaselineError, "runner_version_unknown"):
            self.fixture.execute()

    def test_R30_12_blocked_run_is_rejected(self):
        self.fixture.run["validation_status"] = "blocked"
        with self.assertRaisesRegex(RUN.RobustnessBaselineError, "not_ready"):
            self.fixture.execute()

    def test_R30_13_failed_result_is_rejected(self):
        self.fixture.run["result"] = dict(self.fixture.run["result"], status="failed")
        with self.assertRaisesRegex(RUN.RobustnessBaselineError, "not_completed"):
            self.fixture.execute()

    def test_R30_14_baseline_result_fingerprint_mismatch_is_rejected(self):
        self.fixture.run["result"]["result_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(RUN.RobustnessBaselineError, "fingerprint_mismatch"):
            self.fixture.execute()

    def test_R30_15_experiment_spec_fingerprint_mismatch_is_rejected(self):
        self.fixture.plan = _plan(baseline_run_key=self.fixture.run["run_key"],
                                   baseline_experiment_fingerprint="e" * 64)
        with self.assertRaisesRegex(RUN.RobustnessBaselineError, "experiment_mismatch"):
            self.fixture.execute()

    def test_R30_16_owner_identity_mismatch_is_rejected(self):
        self.fixture.run["market_archive_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(RUN.RobustnessBaselineError, "owner_identity_mismatch"):
            self.fixture.execute()

    def test_R30_17_legacy_result_shape_cannot_enter(self):
        self.fixture.run["result"] = {"status": "completed", "metrics": {"return": 1}}
        with self.assertRaisesRegex(RUN.RobustnessBaselineError, "result_experiment_mismatch"):
            self.fixture.execute()

    def test_R30_18_baseline_is_explicit_and_has_no_latest_fallback(self):
        plan = _plan(baseline_run_key="e" * 64,
                     baseline_experiment_fingerprint=self.fixture.spec.fingerprint)
        with self.assertRaisesRegex(RUN.RobustnessBaselineError, "run_identity_mismatch"):
            self.fixture.execute(plan)

    def test_baseline_run_key_must_match_the_R29_owner_projection(self):
        self.fixture.run["run_key"] = "e" * 64
        self.fixture.plan = _plan(baseline_run_key="e" * 64,
            baseline_experiment_fingerprint=self.fixture.spec.fingerprint)
        with self.assertRaisesRegex(RUN.RobustnessBaselineError, "run_key_mismatch"):
            self.fixture.execute()

    def test_R30_19_regime_classifier_is_trailing_only(self):
        bars = [_bar(BENCHMARK, session, close)
                for session, close in zip(SESSIONS, (100, 101, 103, 90, 80), strict=True)]
        before = RR.classify_sessions(bars, SESSIONS, _policy())[SESSIONS[2]]
        self.assertEqual("bull", before["trend_regime"])
        bars[-1]["close"] = 10000
        after = RR.classify_sessions(bars, SESSIONS, _policy())[SESSIONS[2]]
        self.assertEqual(before, after)

    def test_R30_20_future_return_cannot_relabel_past_session(self):
        bars = [_bar(BENCHMARK, session, close)
                for session, close in zip(SESSIONS, (100, 101, 103, 90, 80), strict=True)]
        first = RR.classify_sessions(bars, SESSIONS, _policy())
        bars[-1]["close"] *= 100
        self.assertEqual(first[SESSIONS[0]], RR.classify_sessions(bars, SESSIONS, _policy())[SESSIONS[0]])

    def test_R30_21_insufficient_history_is_unknown(self):
        labels = RR.classify_sessions([_bar(BENCHMARK, SESSIONS[0], 100)],
                                      SESSIONS[:1], _policy())
        self.assertEqual({"trend_regime": "unknown", "volatility_regime": "unknown"},
                         labels[SESSIONS[0]])

    def test_R30_22_bull_policy_is_explicit(self):
        bars = [_bar(BENCHMARK, day, price) for day, price in
                zip(SESSIONS[:3], (100, 101, 104), strict=True)]
        self.assertEqual("bull", RR.classify_sessions(bars, SESSIONS[:3], _policy())[SESSIONS[2]]["trend_regime"])

    def test_R30_23_bear_policy_is_explicit(self):
        bars = [_bar(BENCHMARK, day, price) for day, price in
                zip(SESSIONS[:3], (100, 99, 96), strict=True)]
        self.assertEqual("bear", RR.classify_sessions(bars, SESSIONS[:3], _policy())[SESSIONS[2]]["trend_regime"])

    def test_R30_24_sideways_policy_is_explicit(self):
        bars = [_bar(BENCHMARK, day, price) for day, price in
                zip(SESSIONS[:3], (100, 100.2, 100.1), strict=True)]
        self.assertEqual("sideways", RR.classify_sessions(bars, SESSIONS[:3], _policy())[SESSIONS[2]]["trend_regime"])

    def test_R30_25_high_volatility_policy_is_explicit(self):
        bars = [_bar(BENCHMARK, day, price) for day, price in
                zip(SESSIONS[:3], (100, 110, 90), strict=True)]
        self.assertEqual("high", RR.classify_sessions(bars, SESSIONS[:3], _policy())[SESSIONS[2]]["volatility_regime"])

    def test_R30_26_low_volatility_policy_is_explicit(self):
        bars = [_bar(BENCHMARK, day, price) for day, price in
                zip(SESSIONS[:3], (100, 100.1, 100.2), strict=True)]
        self.assertEqual("low", RR.classify_sessions(bars, SESSIONS[:3], _policy())[SESSIONS[2]]["volatility_regime"])

    def test_R30_27_regime_labels_are_not_added_to_strategy_input(self):
        report = self.fixture.execute()
        for case in report["cases"]:
            ast = case["evidence"].get("execution_changes", {}).get("derived_ast", {})
            self.assertNotIn("regime", str(ast).lower())

    def test_R30_28_cost_stress_comes_from_spec_cost_model(self):
        scenario = {"category": "cost", "parameters": {"commission_multiplier": 3}}
        stress, evidence = RUN._execution_stress("cost", scenario["parameters"], self.fixture.spec)
        expected = self.fixture.spec.cost_model["commission_rate"] * 3
        self.assertEqual(expected, stress["cost_model"]["commission_rate"])
        self.assertEqual(expected, evidence["commission_rate"]["stressed"])

    def test_R30_29_slippage_stress_comes_from_spec(self):
        stress, evidence = RUN._execution_stress("slippage", {"multiplier": 2}, self.fixture.spec)
        self.assertEqual(2, stress["slippage_multiplier"])
        self.assertEqual(self.fixture.spec.cost_model["slippage_parameters"]["rate"] * 2,
                         evidence["slippage_rate"]["stressed"])

    def test_R30_30_stress_evidence_contains_baseline_multiplier_and_value(self):
        _, evidence = RUN._execution_stress("cost", {"commission_multiplier": 2}, self.fixture.spec)
        self.assertEqual({"baseline", "multiplier", "stressed"},
                         set(evidence["commission_rate"]))

    def test_R30_31_signal_delay_uses_owner_session_indices(self):
        code = EXEC.CODE
        bars = [EXEC._bar(day) for day in SESSIONS]
        members = {day: [{"code": code}] for day in SESSIONS}
        facts = {(code, day): TA.TradabilityEvidence(
            code=code, session_date=day, is_listed=True, listing_date="2020-01-01",
            delisting_date=None, is_st=False, is_suspended=False, suspension_reason=None,
            has_market_quote=True, has_trade_volume=True, is_price_limit_locked=False,
            price_limit_direction=None, source="fixture-owner", observed_at=f"{day}T09:00:00+08:00",
            effective_at=f"{day}T09:30:00+08:00") for day in SESSIONS}
        ast = {"op": "strategy", "rule": {"op": "gt", "left": {"op": "field", "name": "close"},
               "right": {"op": "const", "value": 0}}}
        delayed = EM.simulate_with_trace(spec=EXEC._spec(), ast=ast, sessions=SESSIONS,
            members_by_session=members, bars=bars, tradability_repository=None,
            tradability_evidence=facts, stress={"signal_delay_sessions": 1})
        self.assertEqual([0, 0, 1, 0, 0], [row["trade_count_delta"] for row in delayed["trace"]])

    def test_R30_32_execution_delay_uses_session_counts(self):
        stress, _ = RUN._execution_stress("execution_delay", {
            "execution_delay_sessions": 2}, self.fixture.spec)
        self.assertEqual(2, stress["execution_delay_sessions"])

    def test_R30_33_execution_model_requeries_delayed_tradability(self):
        sessions = SESSIONS[:3]
        code = EXEC.CODE
        evidence = {(code, sessions[0]): EXEC._evidence(observed_at=f"{sessions[0]}T09:00:00+08:00"),
                    (code, sessions[1]): EXEC._evidence(observed_at=f"{sessions[1]}T09:00:00+08:00")}
        with self.assertRaisesRegex(EM.ExperimentExecutionUnavailable,
                                    "execution_tradability_unavailable"):
            EM.simulate(EXEC._spec(), ast={"op": "strategy", "rule": {"op": "gt",
                "left": {"op": "field", "name": "close"}, "right": {"op": "const", "value": 0}}},
                sessions=sessions, members_by_session={day: [{"code": code}] for day in sessions},
                bars=[EXEC._bar(day) for day in sessions], tradability_repository=None,
                tradability_evidence=evidence, stress={"execution_delay_sessions": 1})

    def test_R30_34_missing_delayed_market_bar_is_unavailable(self):
        sessions = SESSIONS[:3]
        code = EXEC.CODE
        evidence = {(code, day): EXEC._evidence(observed_at=f"{day}T09:00:00+08:00")
                    for day in sessions}
        with self.assertRaisesRegex(EM.ExperimentExecutionUnavailable,
                                    "execution_market_bar_unavailable"):
            EM.simulate(EXEC._spec(), ast={"op": "strategy", "rule": {"op": "gt",
                "left": {"op": "field", "name": "close"}, "right": {"op": "const", "value": 0}}},
                sessions=sessions, members_by_session={day: [{"code": code}] for day in sessions},
                bars=[EXEC._bar(sessions[0]), EXEC._bar(sessions[1])],
                tradability_repository=None, tradability_evidence=evidence,
                stress={"execution_delay_sessions": 1})

    def test_R30_35_trace_keeps_R29_aggregate_metrics_bit_identical(self):
        bars = [EXEC._bar(day) for day in EXEC.SESSIONS]
        kwargs = {"spec": EXEC._spec(), "ast": {"op": "strategy", "rule": {"op": "const", "value": False}},
            "sessions": EXEC.SESSIONS,
            "members_by_session": {day: [{"code": CODE}] for day in EXEC.SESSIONS},
            "bars": bars, "tradability_repository": None}
        aggregate = EM.simulate(**kwargs)
        traced = EM.simulate_with_trace(**kwargs)
        self.assertEqual(aggregate, {key: value for key, value in traced.items() if key != "trace"})
        self.assertEqual(len(EXEC.SESSIONS), len(traced["trace"]))

    def test_R30_36_liquidity_stress_changes_capacity_not_price_truth(self):
        bars = [EXEC._bar(day) for day in EXEC.SESSIONS]
        before = [dict(row) for row in bars]
        code = EXEC.CODE
        ast = {"op": "strategy", "rule": {"op": "gt",
               "left": {"op": "field", "name": "close"}, "right": {"op": "const", "value": 0}}}
        facts = {(code, day): TA.TradabilityEvidence(
            code=code, session_date=day, is_listed=True, listing_date="2020-01-01",
            delisting_date=None, is_st=False, is_suspended=False, suspension_reason=None,
            has_market_quote=True, has_trade_volume=True, is_price_limit_locked=False,
            price_limit_direction=None, source="fixture-owner",
            observed_at=f"{day}T09:00:00+08:00", effective_at=f"{day}T09:30:00+08:00")
            for day in EXEC.SESSIONS}
        normal = EM.simulate(EXEC._spec(), ast=ast, sessions=EXEC.SESSIONS,
            members_by_session={day: [{"code": code}] for day in EXEC.SESSIONS}, bars=bars,
            tradability_repository=None, tradability_evidence=facts)
        reduced = EM.simulate(EXEC._spec(), ast=ast, sessions=EXEC.SESSIONS,
            members_by_session={day: [{"code": code}] for day in EXEC.SESSIONS}, bars=bars,
            tradability_repository=None, tradability_evidence=facts,
            stress={"liquidity_multiplier": 0.5})
        self.assertEqual(before, bars)
        self.assertEqual(1, normal["trade_count"])
        self.assertEqual(1, reduced["trade_count"])
        self.assertNotEqual(normal["total_return"], reduced["total_return"])

    def test_R30_37_unknown_volume_does_not_become_infinite_capacity(self):
        bars = [EXEC._bar(day) for day in EXEC.SESSIONS]
        bars[1]["volume"] = 0
        code = EXEC.CODE
        metrics = EM.simulate(EXEC._spec(), ast={"op": "strategy", "rule": {"op": "gt",
            "left": {"op": "field", "name": "close"}, "right": {"op": "const", "value": 0}}},
            sessions=EXEC.SESSIONS, members_by_session={day: [{"code": code}] for day in EXEC.SESSIONS},
            bars=bars, tradability_repository=None,
            tradability_evidence={(code, EXEC.SESSIONS[1]): EXEC._evidence()})
        self.assertEqual(0, metrics["trade_count"])
        self.assertTrue(math.isfinite(metrics["capacity_proxy"]))

    def test_R30_38_missingness_is_deterministic(self):
        bar = _bar(CODE, SESSIONS[0], 10)
        with mock.patch.object(random, "random", side_effect=[0.1, 0.9]):
            first = RUN._bar_is_masked(bar, seed=8, fraction=0.5, field_scope="whole_bar")
            second = RUN._bar_is_masked(bar, seed=8, fraction=0.5, field_scope="whole_bar")
        self.assertEqual(first, second)

    def test_R30_39_different_seed_changes_mask_selection(self):
        bars = [_bar(CODE, day, 10) for day in SESSIONS]
        left = {row["session"] for row in bars if RUN._bar_is_masked(
            row, seed=1, fraction=0.5, field_scope="whole_bar")}
        right = {row["session"] for row in bars if RUN._bar_is_masked(
            row, seed=2, fraction=0.5, field_scope="whole_bar")}
        self.assertNotEqual(left, right)

    def test_R30_40_mask_selector_has_no_performance_input(self):
        import inspect
        self.assertEqual(("bar", "seed", "fraction", "field_scope"), tuple(inspect.signature(
            RUN._bar_is_masked).parameters))

    def test_R30_41_missing_required_execution_bar_is_unavailable(self):
        code = EXEC.CODE
        evidence = {(code, EXEC.SESSIONS[1]): EXEC._evidence()}
        with self.assertRaisesRegex(EM.ExperimentExecutionUnavailable,
                                    "execution_market_bar_unavailable"):
            EM.simulate(EXEC._spec(), ast={"op": "strategy", "rule": {"op": "gt",
                "left": {"op": "field", "name": "close"}, "right": {"op": "const", "value": 0}}},
                sessions=EXEC.SESSIONS, members_by_session={day: [{"code": code}] for day in EXEC.SESSIONS},
                bars=[EXEC._bar(EXEC.SESSIONS[0])], tradability_repository=None,
                tradability_evidence=evidence)

    def test_masked_required_strategy_bar_is_unavailable_not_zero_performance(self):
        plan = _plan(baseline_run_key=self.fixture.run["run_key"],
            baseline_experiment_fingerprint=self.fixture.spec.fingerprint,
            missing_data_stresses=[{"missing_fraction": 1.0, "field_scope": "whole_bar"}])
        report = self.fixture.execute(plan=plan)
        case = next(row for row in report["cases"]
                    if row["scenario"]["category"] == "data_missingness")
        self.assertEqual("unavailable", case["result"]["status"])
        self.assertIsNone(case["result"]["metrics"])
        self.assertGreater(case["evidence"]["masked_observations"], 0)

    def test_R30_42_stress_does_not_mutate_market_archive(self):
        before = self.fixture.market.read_bars(self.fixture.spec.market_data_fingerprint,
            start=self.fixture.spec.start_date, end=self.fixture.spec.end_date)
        self.fixture.execute()
        after = self.fixture.market.read_bars(self.fixture.spec.market_data_fingerprint,
            start=self.fixture.spec.start_date, end=self.fixture.spec.end_date)
        self.assertEqual(before, after)

    def test_R30_43_synthetic_rows_are_not_persisted_as_owner_facts(self):
        before = self.fixture.market_conn.execute("SELECT COUNT(*) FROM historical_market_bars").fetchone()[0]
        self.fixture.execute()
        after = self.fixture.market_conn.execute("SELECT COUNT(*) FROM historical_market_bars").fetchone()[0]
        self.assertEqual(before, after)

    def test_R30_44_only_allowlisted_parameter_path_changes(self):
        with self.assertRaisesRegex(ValueError, "parameter_path_not_allowlisted"):
            _plan(parameter_stresses=[{"path": "validation_portfolio.initial_cash",
                                      "operation": "delta", "value": 10}])

    def test_R30_45_unlisted_numeric_parameter_is_unchanged(self):
        ast = {"op": "strategy", "rule": {"op": "const", "value": 1}}
        with self.assertRaisesRegex(ValueError, "parameter_path_unavailable"):
            RUN._apply_parameter_stress(ast, {"path": "strategy_parameters.rsi_threshold",
                                              "operation": "delta", "value": 1})

    def test_R30_46_parameter_perturbation_is_one_scenario_not_search(self):
        plan = _plan(parameter_stresses=[{"path": "strategy_parameters.entry_threshold",
            "operation": "delta", "value": 1}],
            allowed_parameter_paths=["strategy_parameters.entry_threshold"])
        self.assertEqual(1, len([s for s in plan.scenarios() if s["category"] == "parameter"]))

    def test_R30_47_start_shift_uses_owner_sessions(self):
        shifted = RUN._scenario_sessions({"category": "start_date",
            "parameters": {"shift_sessions": 1}}, self.fixture.spec, SESSIONS)
        self.assertEqual(SESSIONS[2], shifted[0])

    def test_R30_48_end_shift_uses_owner_sessions(self):
        shifted = RUN._scenario_sessions({"category": "end_date",
            "parameters": {"shift_sessions": -1}}, self.fixture.spec, SESSIONS)
        self.assertEqual(SESSIONS[-3], shifted[-1])

    def test_R30_49_out_of_coverage_date_shift_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "outside_archive_coverage"):
            RUN._scenario_sessions({"category": "start_date",
                "parameters": {"shift_sessions": -2}}, self.fixture.spec,
                self.fixture.calendar.sessions)

    def test_date_expansion_requires_owner_issued_calendar_and_R29_revalidation(self):
        expanded_calendar = HSC.issue_from_market_archive(
            self.fixture.market, archive_fingerprint=self.fixture.spec.market_data_fingerprint,
            benchmark_symbol=BENCHMARK, start=SESSIONS[0], end=SESSIONS[-1])
        plan = _plan(baseline_run_key=self.fixture.run["run_key"],
                     baseline_experiment_fingerprint=self.fixture.spec.fingerprint,
                     start_date_stresses=[{"shift_sessions": -1}])
        report = RUN.run_robustness(
            baseline_run=self.fixture.run, spec=self.fixture.spec, plan=plan,
            strategy_version=self.fixture.strategy,
            session_calendar=self.fixture.calendar,
            extended_session_calendar=expanded_calendar,
            market_archive_repository=self.fixture.market,
            universe_archive_repository=self.fixture.universe,
            tradability_repository=self.fixture.tradability,
            created_at="2026-01-10T00:00:00Z")
        case = next(row for row in report["cases"] if row["scenario"]["category"] == "start_date")
        self.assertEqual("unavailable", case["result"]["status"])
        self.assertEqual("date_range_pit_context_missing", case["result"]["reason_code"])
        self.assertIsNone(case["result"]["metrics"])

    def test_R30_50_universe_drop_is_deterministic(self):
        members = {day: [{"code": f"{index:06d}.SH"} for index in range(100)] for day in SESSIONS}
        first, dropped_first = RUN._drop_universe_members(members, seed=3, fraction=0.4)
        second, dropped_second = RUN._drop_universe_members(members, seed=3, fraction=0.4)
        self.assertEqual(dropped_first, dropped_second)
        self.assertEqual(first, second)
        self.assertTrue(dropped_first)

    def test_R30_51_universe_drop_has_no_performance_input(self):
        import inspect
        self.assertEqual(("members_by_session", "seed", "fraction"), tuple(inspect.signature(
            RUN._drop_universe_members).parameters))

    def test_R30_52_universe_does_not_add_current_names(self):
        members = __import__("experiment_validation_runner")._universe_rows(
            self.fixture.universe, self.fixture.spec.universe_fingerprint, SESSIONS)
        self.assertEqual({CODE}, {row["code"] for values in members.values() for row in values})

    def test_R30_53_future_listed_member_cannot_be_added(self):
        self.assertNotIn("000002.SH", {row["code"] for values in
            __import__("experiment_validation_runner")._universe_rows(
                self.fixture.universe, self.fixture.spec.universe_fingerprint, SESSIONS).values()
            for row in values})

    def test_R30_54_unknown_metrics_remain_none(self):
        result = RC.case_result(scenario_fingerprint="a" * 64, status="unavailable",
            metrics=None, baseline_delta=None, reason_code="owner_unavailable")
        self.assertIsNone(result["metrics"])

    def test_R30_55_unavailable_scenario_cannot_claim_metrics(self):
        with self.assertRaisesRegex(ValueError, "cannot_claim_metrics"):
            RC.case_result(scenario_fingerprint="a" * 64, status="unavailable",
                metrics={"return": 0}, baseline_delta=None, reason_code="missing")

    def test_R30_56_completed_case_requires_complete_metric_set(self):
        with self.assertRaisesRegex(ValueError, "metrics_required"):
            RC.case_result(scenario_fingerprint="a" * 64, status="completed",
                metrics={"return": 1}, baseline_delta={})

    def test_R30_57_baseline_delta_uses_exact_baseline(self):
        report = self.fixture.execute()
        baseline = self.fixture.run["result"]["metrics"]["return"]
        for case in report["cases"]:
            result = case["result"]
            if result["status"] == "completed":
                self.assertEqual(result["metrics"]["return"] - baseline,
                                 result["baseline_delta"]["return"])

    def test_R30_58_report_has_no_single_score(self):
        import json
        self.assertNotIn('"score"', json.dumps(self.fixture.execute()).lower())

    def test_R30_59_report_has_no_grade(self):
        import json
        self.assertNotIn('"grade"', json.dumps(self.fixture.execute()).lower())

    def test_R30_60_report_has_no_promotion_status(self):
        import json
        report = self.fixture.execute()
        self.assertNotIn("promot", json.dumps(report).lower())

    def test_R30_61_same_exact_inputs_produce_same_report_identity(self):
        first = self.fixture.execute(created_at="2026-01-10T00:00:00Z")
        second = self.fixture.execute(created_at="2026-02-10T00:00:00Z")
        self.assertNotEqual(first["created_at"], second["created_at"])
        self.assertEqual(first["report_fingerprint"], second["report_fingerprint"])

    def test_R30_62_case_order_is_deterministic(self):
        report = self.fixture.execute()
        expected = [scenario["category"] for scenario in self.fixture.plan.scenarios()]
        self.assertEqual(expected, [case["scenario"]["category"] for case in report["cases"]])
        self.assertEqual([case["scenario"]["scenario_fingerprint"] for case in report["cases"]],
                         [case["scenario"]["scenario_fingerprint"] for case in self.fixture.execute()["cases"]])

    def test_R30_63_regime_metrics_use_trace_rows(self):
        trace = [{"session": SESSIONS[0], "daily_return": 0.1, "exposure": 0.4,
                  "trade_count_delta": 1, "cost_delta": 2, "turnover_delta": 0.3}]
        summary = RR.summarize_trace(trace, {SESSIONS[0]: {"trend_regime": "bull",
                                                           "volatility_regime": "high"}})
        self.assertEqual(1, summary["trend"]["bull"]["trade_count"])
        self.assertAlmostEqual(0.1, summary["trend"]["bull"]["return"])


if __name__ == "__main__":
    unittest.main()
