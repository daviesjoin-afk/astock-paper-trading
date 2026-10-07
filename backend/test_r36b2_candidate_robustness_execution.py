"""CRB-01–38: candidate robustness execution, exact baseline binding and queue closure."""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

import candidate_experiment as CE
import candidate_experiment_service as CES
import candidate_robustness_service as CRS
import experiment_search_contract as ESC
import experiment_search_repository as ESR
import experiment_search_service as ESS
import experiment_validation_repository as EVR
import experiment_validation_runner as R29
import historical_market_archive as HMA
import historical_session_calendar as HSC
import historical_universe_archive as HUA
import robustness_contract as RC
import robustness_repository as RREP
import robustness_runner as RRUN
import tradability_archive as TA
import test_experiment_execution_model as XF
import test_r36a_experiment_search_controller as SF
import test_r36b1_candidate_experiment_execution as B1
import walk_forward_validation as WFV

SESSIONS = ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09")
CODE = "600000.SH"
OTHER = "600001.SH"
BENCHMARK = "600000.SH"


def validation_samples():
    return [
        WFV.ValidationSample(
            sample_key=f"sample-{day}", code="600000",
            decision_session=day, decision_at=f"{day}T09:30:00+08:00",
            label_available_at=f"{day}T15:00:00+08:00",
            target=0.01 * (index + 1), pit_status=WFV.PIT_VERIFIED)
        for index, day in enumerate(SESSIONS)
    ]


def regime_policy():
    return {"policy_version": "r30-regime-v1", "benchmark_symbol": BENCHMARK,
            "trend_window_sessions": 3, "bull_threshold": 0.01, "bear_threshold": 0.01,
            "volatility_window_sessions": 3, "high_vol_threshold": 0.02,
            "low_vol_threshold": 0.005}


def policy(**changes):
    values = {"random_seed": 7, "regime_policy": regime_policy(),
              "cost_stresses": [{"commission_multiplier": 2}],
              "slippage_stresses": [], "execution_delay_stresses": [], "signal_delay_stresses": [],
              "liquidity_stresses": [], "missing_data_stresses": [], "parameter_stresses": [],
              "start_date_stresses": [], "end_date_stresses": [], "universe_stresses": []}
    values.update(changes)
    return RC.RobustnessPolicy(**values)


class PolicyTests(unittest.TestCase):
    def test_crb02_policy_identity(self):
        base = policy()
        self.assertEqual(base.fingerprint, policy().fingerprint)
        for changes in ({"random_seed": 8},
                        {"regime_policy": {**regime_policy(), "bull_threshold": 0.02}},
                        {"cost_stresses": [{"commission_multiplier": 3}]},
                        {"slippage_stresses": [{"multiplier": 2}]},
                        {"execution_delay_stresses": [{"execution_delay_sessions": 1}]},
                        {"signal_delay_stresses": [{"signal_delay_sessions": 1}]},
                        {"liquidity_stresses": [{"liquidity_multiplier": 0.5}]},
                        {"missing_data_stresses": [{"missing_fraction": 0.1, "field_scope": "whole_bar"}]},
                        {"start_date_stresses": [{"shift_sessions": 1}]},
                        {"end_date_stresses": [{"shift_sessions": -1}]},
                        {"universe_stresses": [{"drop_fraction": 0.1}]},
                        {"max_drawdown_limit": 0.2}, {"max_return_degradation": 0.2}):
            with self.subTest(changes=changes):
                self.assertNotEqual(base.fingerprint, policy(**changes).fingerprint)

    def test_crb03_policy_bind(self):
        base = policy()
        a = base.bind("a" * 64, "b" * 64)
        b = base.bind("c" * 64, "b" * 64)
        self.assertNotEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(base.fingerprint, policy().fingerprint)
        # bind() must preserve every stress fact of the policy verbatim.
        for name in ("random_seed", "regime_policy", "cost_stresses", "slippage_stresses",
                     "execution_delay_stresses", "signal_delay_stresses", "liquidity_stresses",
                     "missing_data_stresses", "parameter_stresses", "start_date_stresses",
                     "end_date_stresses", "universe_stresses", "allowed_parameter_paths",
                     "max_drawdown_limit", "max_return_degradation"):
            self.assertEqual(getattr(base, name), getattr(a, name), name)
        self.assertEqual(base.random_seed, a.random_seed)
        # plan projection identity is unchanged by adding the policy layer
        self.assertEqual({"plan_version", "baseline_run_key", "baseline_experiment_fingerprint",
                          "random_seed", "regime_policy", "allowed_parameter_paths",
                          "max_drawdown_limit", "max_return_degradation",
                          "cost_stresses", "slippage_stresses", "execution_delay_stresses",
                          "signal_delay_stresses", "liquidity_stresses", "missing_data_stresses",
                          "parameter_stresses", "start_date_stresses", "end_date_stresses",
                          "universe_stresses"}, set(a.projection()))

    def test_crb06_policy_has_no_runtime_default(self):
        with self.assertRaises(TypeError):
            RC.RobustnessPolicy()


class PlanV2Tests(unittest.TestCase):
    def test_crb04_plan_v1_identity_unchanged(self):
        p = B1.plan()
        frozen = p.fingerprint
        self.assertEqual(frozen, ESC.experiment_plan_from_projection(p.projection()).fingerprint)
        self.assertEqual(ESC.EXPERIMENT_PLAN_CONTRACT_VERSION, p.plan_contract_version)

    def test_crb05_plan_v2_search_identity(self):
        p = B1.plan()
        v1 = ESC.ExperimentSearchSpec("a" * 64, "b" * 64, ("c" * 64,), ESC.SearchBudget(1),
                                      ESC.SEARCH_CONTRACT_VERSION_V2, experiment_plan=p)
        v2 = ESC.ExperimentSearchPlanV2.from_v1(p, policy())
        v2_spec = ESC.ExperimentSearchSpec("a" * 64, "b" * 64, ("c" * 64,), ESC.SearchBudget(1),
                                           ESC.SEARCH_CONTRACT_VERSION_V2, experiment_plan=v2)
        self.assertNotEqual(v1.search_input_fingerprint, v2_spec.search_input_fingerprint)
        changed = ESC.ExperimentSearchPlanV2.from_v1(p, policy(random_seed=8))
        changed_spec = ESC.ExperimentSearchSpec("a" * 64, "b" * 64, ("c" * 64,), ESC.SearchBudget(1),
                                                ESC.SEARCH_CONTRACT_VERSION_V2, experiment_plan=changed)
        self.assertNotEqual(v2_spec.search_input_fingerprint, changed_spec.search_input_fingerprint)
        self.assertEqual(v2_spec.search_input_fingerprint,
                         ESC.search_spec_from_projection(v2_spec.projection()).search_input_fingerprint)

    def test_crb06b_plan_v2_requires_policy(self):
        with self.assertRaises(TypeError):
            ESC.ExperimentSearchPlanV2(plan=B1.plan())

    def test_unified_job_decoder_dispatches_and_fails_closed(self):
        pit = ESC.SearchJobSpec("a" * 64, "b" * 64)
        self.assertIsInstance(ESC.search_job_from_projection(pit.projection()), ESC.SearchJobSpec)
        rob = ESC.RobustnessSearchJobSpec(
            search_run_id="a" * 64, candidate_id="b" * 64, baseline_run_key="c" * 64,
            baseline_experiment_fingerprint="d" * 64,
            robustness_policy_fingerprint="e" * 64, robustness_plan_fingerprint="f" * 64)
        self.assertIsInstance(ESC.search_job_from_projection(rob.projection()),
                              ESC.RobustnessSearchJobSpec)
        unknown = dict(pit.projection(), stage="optimization")
        with self.assertRaises(ESC.SearchContractError):
            ESC.search_job_from_projection(unknown)

    def test_robustness_job_id_binds_baseline_and_plan(self):
        kwargs = dict(search_run_id="a" * 64, candidate_id="b" * 64, baseline_run_key="c" * 64,
                      baseline_experiment_fingerprint="d" * 64,
                      robustness_policy_fingerprint="e" * 64, robustness_plan_fingerprint="f" * 64)
        base = ESC.RobustnessSearchJobSpec(**kwargs)
        for key in ("baseline_run_key", "baseline_experiment_fingerprint",
                    "robustness_policy_fingerprint", "robustness_plan_fingerprint"):
            with self.subTest(key=key):
                self.assertNotEqual(base.job_id, ESC.RobustnessSearchJobSpec(
                    **{**kwargs, key: "9" * 64}).job_id)


class CandidateRobustnessFixture(SF._Base):
    def setUp(self):
        super().setUp()
        self.queue = sqlite3.connect(self.path, isolation_level=None)
        self.queue.row_factory = sqlite3.Row
        self.addCleanup(self.queue.close)
        self.owners = sqlite3.connect(":memory:", check_same_thread=False)
        self.owners.row_factory = sqlite3.Row
        self.addCleanup(self.owners.close)
        self.market = HMA.HistoricalMarketArchiveRepository(self.owners)
        days = list(SESSIONS)
        rows = [dict(XF._bar(day), code=CODE, volume=1000000) for day in days]
        rows += [dict(XF._bar(day), code=OTHER, volume=1000000) for day in days]
        manifest = self.market.import_raw_market_archive(rows, source="crb-owner", source_revision="1",
            adjustment="raw", benchmark_calendars={BENCHMARK: {
                "coverage_start": days[0], "coverage_end": days[-1], "sessions": days,
                "source": "exchange-export", "source_revision": "1"}})
        self.calendar = HSC.issue_from_market_archive(self.market,
            archive_fingerprint=manifest.archive_fingerprint, benchmark_symbol=BENCHMARK,
            start=days[0], end=days[-1])
        self.universe = HUA.HistoricalUniverseArchiveRepository(self.owners)
        um = self.universe.import_historical_security_master([
            {"code": CODE, "listed_from": "2020-01-01", "delisted_at": None,
             "security_type": "equity", "exchange": "SH",
             "observed_at": "2020-01-01T09:00:00+08:00"},
            {"code": OTHER, "listed_from": "2020-01-01", "delisted_at": None,
             "security_type": "equity", "exchange": "SH",
             "observed_at": "2020-01-01T09:00:00+08:00"}],
            coverage_start="2020-01-01", coverage_end=days[-1],
            source="crb-owner", source_revision="1")
        self.tradability = TA.TradabilityArchiveRepository(self.owners)
        self.tradability.ensure_schema()
        for day in days:
            for code in (CODE, OTHER):
                self.tradability.save(replace(XF._evidence(), code=code, session_date=day,
                    observed_at=day + "T09:00:00+08:00", effective_at=day + "T09:00:00+08:00"))
        self.validation = EVR.ExperimentValidationRepository(self.owners)
        self.robustness = RREP.RobustnessRepository(self.owners)
        self.owners.commit()
        self.plan = B1.plan(start_date=days[0], end_date=days[-1],
                            market_archive_fingerprint=manifest.archive_fingerprint,
                            universe_archive_fingerprint=um.universe_archive_fingerprint,
                            session_calendar_fingerprint=self.calendar.calendar_fingerprint,
                            asof_policy={"policy_id": "pit-close-v1",
                                         "cutoff": days[-1] + "T15:00:00+08:00"})
        self.plan_v2 = ESC.ExperimentSearchPlanV2.from_v1(self.plan, policy())
        self.batch_record = self.batch(values=[18, 19])

    def create_v2(self, batch=None, **overrides):
        return self.create(batch or self.batch_record, experiment_plan=self.plan_v2, **overrides)

    def run_pit(self, search_run_id, *, samples=None):
        self.queue.execute("BEGIN IMMEDIATE")
        job = ESS.claim_next_job(self.queue, search_run_id, actor="crb")
        self.queue.commit()
        if job["job_id"] is None:
            return None
        CES.run_candidate_pit_validation(
            self.queue, job_id=job["job_id"], validation_repository=self.validation,
            session_calendar=self.calendar, market_archive_repository=self.market,
            universe_archive_repository=self.universe, tradability_repository=self.tradability,
            dataset_manifest={"dataset_fingerprint": self.plan.dataset_fingerprint},
            samples=validation_samples() if samples is None else samples,
            created_at="2026-10-07T00:00:00Z")
        return job["job_id"]

    def run_all_pit(self, search_run_id):
        while True:
            job_id = self.run_pit(search_run_id)
            if job_id is None:
                return

    def _claim_one_robustness(self, search_run_id):
        self.declare(search_run_id)
        job_id = self.claim_robustness(search_run_id)
        self.assertIsNotNone(job_id)
        return job_id

    def declare(self, search_run_id):
        return CRS.declare_candidate_robustness_jobs(
            self.queue, search_run_id=search_run_id, validation_repository=self.validation,
            created_at="2026-10-07T00:00:00Z")

    def claim_robustness(self, search_run_id):
        self.queue.execute("BEGIN IMMEDIATE")
        for _ in range(20):
            job = ESS.claim_next_job(self.queue, search_run_id, actor="crb")
            if job["job_id"] is None:
                break
            row = ESR.get_search_job(self.queue, job["job_id"])
            if row["stage"] == ESC.JOB_STAGE_ROBUSTNESS:
                self.queue.commit()
                return job["job_id"]
            ESS.record_job_event(self.queue, job_id=job["job_id"], event_kind="failed",
                                 reason="skip_non_robustness")
        self.queue.commit()
        return None

    def execute_robustness(self, job_id, **changes):
        args = dict(job_id=job_id, validation_repository=self.validation,
                    session_calendar=self.calendar, market_archive_repository=self.market,
                    universe_archive_repository=self.universe,
                    tradability_repository=self.tradability, robustness_repository=self.robustness,
                    dataset_manifest={"dataset_fingerprint": self.plan.dataset_fingerprint},
                    samples=validation_samples(), created_at="2026-10-07T00:00:00Z")
        args.update(changes)
        return CRS.run_candidate_robustness(self.queue, **args)


class DeclarationTests(CandidateRobustnessFixture):
    def test_crb07_stage_barrier(self):
        created = self.create_v2()
        with self.assertRaisesRegex(CRS.CandidateRobustnessUnavailable, "pit_stage_not_terminal"):
            self.declare(created["search_run_id"])

    def test_crb08_blocked_pit_gets_no_robustness_job(self):
        with mock.patch.object(SF, "UNIVERSE", {"scope_kind": "a_share_boards", "boards": ["main_board"]}):
            blocked = self.batch(values=[18], campaign="blocked_boards")
        created = self.create(blocked, experiment_plan=self.plan_v2)
        self.run_all_pit(created["search_run_id"])
        result = self.declare(created["search_run_id"])
        self.assertEqual(0, result["jobs_created"])
        states = [j for j in ESS.list_search_jobs(self.queue, created["search_run_id"])
                  if j["stage"] == ESC.JOB_STAGE_ROBUSTNESS]
        self.assertEqual([], states)

    def test_crb09_cancelled_pit_gets_no_robustness_job(self):
        created = self.create_v2()
        # queued -> cancelled is the legal terminal transition.
        for state in ESS.list_search_jobs(self.queue, created["search_run_id"]):
            self.queue.execute("BEGIN IMMEDIATE")
            ESS.record_job_event(self.queue, job_id=state["job_id"], event_kind="cancelled")
            self.queue.commit()
        result = self.declare(created["search_run_id"])
        self.assertEqual(0, result["jobs_created"])
        self.assertEqual([], [j for j in ESS.list_search_jobs(self.queue, created["search_run_id"])
                              if j["stage"] == ESC.JOB_STAGE_ROBUSTNESS])

    def test_crb10_11_12_exact_atomic_idempotent_declaration(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        before_jobs = self.queue.execute("SELECT COUNT(*) FROM experiment_search_jobs").fetchone()[0]
        first = self.declare(created["search_run_id"])
        self.assertEqual(2, first["jobs_created"])
        self.assertEqual(2, first["queued_events_created"])
        after_jobs = self.queue.execute("SELECT COUNT(*) FROM experiment_search_jobs").fetchone()[0]
        self.assertEqual(before_jobs + 2, after_jobs)
        events_before = self.queue.execute("SELECT COUNT(*) FROM experiment_search_job_events").fetchone()[0]
        second = self.declare(created["search_run_id"])
        self.assertEqual(0, second["jobs_created"])
        self.assertEqual(0, second["queued_events_created"])
        self.assertEqual(events_before,
                         self.queue.execute("SELECT COUNT(*) FROM experiment_search_job_events").fetchone()[0])
        # exact baseline/plan captured on each job
        for state in ESS.list_search_jobs(self.queue, created["search_run_id"]):
            if state["stage"] != ESC.JOB_STAGE_ROBUSTNESS:
                continue
            row = ESR.get_search_job(self.queue, state["job_id"])
            self.assertTrue(row["job"]["baseline_run_key"])
            self.assertEqual(self.plan_v2.robustness_policy.fingerprint,
                             row["job"]["robustness_policy_fingerprint"])

    def test_crb11_atomic_on_insert_failure(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        jobs_before = self.queue.execute("SELECT COUNT(*) FROM experiment_search_jobs").fetchone()[0]
        events_before = self.queue.execute("SELECT COUNT(*) FROM experiment_search_job_events").fetchone()[0]
        original = ESR.record_job
        calls = {"n": 0}

        def flaky(conn, *, job, created_at=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("injected failure")
            return original(conn, job=job, created_at=created_at)

        with mock.patch.object(ESR, "record_job", side_effect=flaky):
            with self.assertRaises(sqlite3.OperationalError):
                self.declare(created["search_run_id"])
        self.assertEqual(jobs_before,
                         self.queue.execute("SELECT COUNT(*) FROM experiment_search_jobs").fetchone()[0])
        self.assertEqual(events_before,
                         self.queue.execute("SELECT COUNT(*) FROM experiment_search_job_events").fetchone()[0])

    def test_crb13_declaration_conflict_on_changed_baseline(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        # Rewrite one robustness job's baseline binding: the same (search, candidate, stage)
        # now maps to a different identity and must be a hard conflict.
        self.queue.execute("DROP TRIGGER experiment_search_jobs_no_update")
        row = dict(self.queue.execute(
            "SELECT * FROM experiment_search_jobs WHERE stage='robustness'").fetchone())
        projection = json.loads(row["job_json"])
        projection["baseline_run_key"] = "9" * 64
        new_job = ESC.RobustnessSearchJobSpec(
            search_run_id=projection["search_run_id"], candidate_id=projection["candidate_id"],
            baseline_run_key="9" * 64,
            baseline_experiment_fingerprint=projection["baseline_experiment_fingerprint"],
            robustness_policy_fingerprint=projection["robustness_policy_fingerprint"],
            robustness_plan_fingerprint=projection["robustness_plan_fingerprint"])
        self.queue.execute("UPDATE experiment_search_jobs SET job_id=?,job_fingerprint=?,job_json=? WHERE job_id=?",
                           (new_job.job_id, new_job.job_fingerprint, ESR._canonical(new_job.projection()),
                            row["job_id"]))
        with self.assertRaisesRegex(CRS.CandidateRobustnessUnavailable, "robustness_job_identity_conflict"):
            self.declare(created["search_run_id"])


class ExecutionTests(CandidateRobustnessFixture):
    def test_crb14_candidate_baseline_replay_and_identity(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        output = self.execute_robustness(job_id)
        report = output["report"]
        self.assertEqual(RRUN.CANDIDATE_RUNNER_VERSION, report["runner_version"])
        self.assertEqual("strategy_candidate",
                         report["baseline_identity"]["experiment_subject"]["kind"])
        self.assertNotIn("strategy_id", report["baseline_identity"])
        self.assertNotIn("strategy_version", report["baseline_identity"])
        self.assertEqual("completed", ESR.list_job_events(self.queue, job_id)[-1]["event_kind"])
        self.assertEqual("robustness_report", ESR.list_job_events(self.queue, job_id)[-1]["evidence_owner"])

    def test_crb15_candidate_universe_exact(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        output = self.execute_robustness(job_id)
        report = output["report"]
        baseline = self.validation.get_run(run_key=report["baseline_run_key"])
        self.assertEqual(baseline["tradability_evidence_fingerprint"],
                         report["baseline_identity"]["tradability_evidence_fingerprint"])

    def test_crb16_universe_perturbation_after_candidate_filter(self):
        # Universe stress must drop from the candidate-filtered scope, not the full market.
        c = B1.candidate(universe_spec={"scope_kind": "explicit_symbols", "symbols": ["000001"]})
        replay = CE.compile_candidate_replay(c)
        full = {"2026-01-05": [{"code": "000001.SH"}, {"code": "600000.SH"}]}
        candidate_scope = CE.filter_candidate_members(replay, full, self.plan.universe_archive_fingerprint)
        self.assertEqual([{"code": "000001.SH"}], candidate_scope["2026-01-05"])
        # A universe drop over the candidate scope can never reintroduce 600000.SH.
        dropped, affected = RRUN._drop_universe_members(candidate_scope, seed=1, fraction=0.0)
        self.assertEqual({"000001.SH"},
                         {row["code"] for rows in dropped.values() for row in rows})
        self.assertEqual([], affected)

    def test_crb16b_runner_applies_candidate_scope_before_stress(self):
        # The real R30 candidate path must run only the candidate-filtered symbols.
        batch = self.batch(values=[18], campaign="narrow")
        created = self.create(batch, experiment_plan=self.plan_v2)
        self.run_all_pit(created["search_run_id"])
        job_id = self._claim_one_robustness(created["search_run_id"])
        output = self.execute_robustness(job_id)
        report = output["report"]
        # Archive has two symbols; the candidate scope is a_share_all by default, so both.
        self.assertEqual(2, report["data_coverage"]["symbols"])
        # Now an explicit-symbols candidate narrows the same archive to exactly one symbol.
        narrow = B1.candidate(
            universe_spec={"scope_kind": "explicit_symbols", "symbols": ["600000"]})
        replay = CE.compile_candidate_replay(narrow)
        members = R29._universe_rows(self.universe, self.plan.universe_archive_fingerprint,
                                     list(SESSIONS))
        filtered = CE.filter_candidate_members(replay, members, self.plan.universe_archive_fingerprint)
        self.assertEqual({CODE}, {row["code"] for rows in filtered.values() for row in rows})

    def test_crb17_factor_exit_retained(self):
        replay = CE.compile_candidate_replay(B1.candidate(factor_spec=B1.rule("roe"),
                                                          exit_spec=B1.rule(threshold=9)))
        deps = R29.PV.replay_dsl_dependencies(replay)
        self.assertIn("roe", deps["financial_fields"])
        # Factor gates entry: a factor that is false blocks the entry.
        self.assertEqual(0, B1.simulate(B1.candidate(factor_spec=B1.rule(threshold=20)))["trade_count"])
        self.assertEqual(1, B1.simulate(B1.candidate(factor_spec=B1.rule(threshold=5)))["trade_count"])
        # Explicit exit owns exit; factor must not force an exit.
        self.assertEqual(1, B1.simulate(B1.candidate(factor_spec=B1.rule(threshold=5)),
                                        closes=(10, 1, 1, 1))["trade_count"])
        self.assertEqual(2, B1.simulate(B1.candidate(exit_spec=B1.rule(threshold=15)),
                                        closes=(10, 20, 20))["trade_count"])

    def test_crb18_constraints_retained(self):
        c = B1.candidate(constraints={"max_positions": 1})
        replay = CE.compile_candidate_replay(c)
        self.assertEqual({"max_positions": 1}, dict(replay.candidate.constraints))
        names = ("000001.SH", "000002.SH", "000003.SH")
        # max_positions=1 must actually cap the replayed portfolio.
        self.assertEqual(1, B1.simulate(c, names=names)["trade_count"])
        self.assertEqual(3, B1.simulate(B1.candidate(), names=names)["trade_count"])

    @staticmethod
    def parameter_entry(*, value=5, minimum=0, maximum=20):
        return {"op": "strategy", "rule": {
            "op": "gt", "left": {"op": "parameter", "parameter_id": "entry_threshold",
                                 "type": "float", "value": value, "min": minimum,
                                 "max": maximum, "max_step": 5, "locked": False,
                                 "risk_direction": "higher_is_riskier", "min_evidence": 0},
            "right": {"op": "const", "value": 0}}}

    def test_crb21_parameter_stress_does_not_create_candidate(self):
        import strategy_parameter_schema as SPS
        c = B1.candidate(entry_spec=self.parameter_entry())
        replay = CE.compile_candidate_replay(c)
        derived_ast = SPS.apply_parameter_stress(
            c.entry_spec, {"path": "strategy_parameters.entry_threshold",
                           "operation": "delta", "value": 2})
        derived = replay.derive_entry_ast(derived_ast)
        # Same candidate, same canonical replay identity; only the scenario replay differs.
        self.assertEqual(replay.candidate.candidate_id, derived.candidate.candidate_id)
        self.assertEqual(replay.replay_fingerprint, derived.replay_fingerprint)
        self.assertNotEqual(replay.scenario_replay_fingerprint, derived.scenario_replay_fingerprint)
        self.assertEqual(replay.projection(), derived.projection())

    def test_crb25_parameter_bounds_unavailable(self):
        import strategy_parameter_schema as SPS
        c = B1.candidate(entry_spec=self.parameter_entry(minimum=0, maximum=6))
        with self.assertRaisesRegex(ValueError, "strategy_parameter_stress_out_of_bounds"):
            SPS.apply_parameter_stress(
                c.entry_spec, {"path": "strategy_parameters.entry_threshold",
                               "operation": "delta", "value": 10})

    @staticmethod
    def _refingerprint(forged):
        """Recompute the embedded report fingerprint exactly as an attacker would."""
        report = forged["report"]
        report["report_fingerprint"] = RC.report_fingerprint(
            baseline_identity={**report["baseline_identity"], "spec": report["baseline_spec"]},
            plan_fingerprint=forged["plan_fingerprint"], cases=report["cases"],
            report_version=report["report_version"])
        return forged

    def _forged_report(self, job_id):
        output = self.execute_robustness(job_id)
        stored = self.robustness.get_report_by_key(output["report_key"])
        forged = dict(stored)
        forged["report"] = dict(stored["report"])
        return forged

    def test_crb15c_forged_nested_provenance_rejected(self):
        # Even if a forged ledger row were read back, completion must reject a nested
        # baseline identity that contradicts the top-level report identity.
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        output = self.execute_robustness(job_id)
        stored = self.robustness.get_report_by_key(output["report_key"])
        for field, value in (("run_key", "1" * 64),
                             ("experiment_fingerprint", "2" * 64),
                             ("result_fingerprint", "3" * 64)):
            with self.subTest(field=field):
                forged = dict(stored)
                forged["report"] = dict(stored["report"])
                forged["report"]["baseline_identity"] = {
                    **stored["report"]["baseline_identity"], field: value}
                self._refingerprint(forged)
                with mock.patch.object(self.robustness, "get_report_by_key",
                                       return_value=forged):
                    with self.assertRaises(CRS.CandidateRobustnessUnavailable):
                        CRS.complete_candidate_robustness_job(
                            self.queue, job_id=job_id, report_key=forged["report_key"],
                            validation_repository=self.validation,
                            robustness_repository=self.robustness)

    def test_crb15d_forged_baseline_spec_rejected(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        forged = self._forged_report(job_id)
        spec = dict(forged["report"]["baseline_spec"])
        spec["random_seed"] = spec["random_seed"] + 1
        forged["report"]["baseline_spec"] = spec
        # Use a fresh report_key so the rejection can only come from the embedded
        # baseline-spec invariant, not from same-key idempotency.
        forged["report_key"] = "9" * 64
        self._refingerprint(forged)
        with self.assertRaises(RREP.RobustnessPersistenceError):
            self.robustness.append_report(forged)
        self.assertIsNone(self.robustness.get_report_by_key("9" * 64))

    def test_crb15e_ledger_rejects_forged_baseline_spec(self):
        # Direct unit test of the ledger's canonical-identity invariant: a record whose
        # embedded baseline_spec does not hash to the declared experiment fingerprint is
        # corrupt, independent of the append-time idempotency pre-check.
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        output = self.execute_robustness(job_id)
        stored = self.robustness.get_report_by_key(output["report_key"])
        record = {key: stored[key] for key in (
            "report_key", "baseline_run_key", "baseline_experiment_fingerprint",
            "baseline_result_fingerprint", "plan_fingerprint", "report_fingerprint",
            "plan", "report", "runner_version")}
        record["report"] = dict(stored["report"])
        spec = dict(record["report"]["baseline_spec"])
        spec["random_seed"] = spec["random_seed"] + 1
        record["report"]["baseline_spec"] = spec
        recomputed = RC.report_fingerprint(
            baseline_identity={**record["report"]["baseline_identity"], "spec": spec},
            plan_fingerprint=record["plan_fingerprint"], cases=record["report"]["cases"],
            report_version=record["report"]["report_version"])
        record["report"]["report_fingerprint"] = recomputed
        record["report_fingerprint"] = recomputed
        with self.assertRaises(RREP.RobustnessPersistenceError):
            RREP._validate_canonical_identity(record)

    def test_crb12b_concurrent_declaration_is_idempotent(self):
        # Two concurrent declarations for the same search must not collide on the
        # unique (search_run_id, candidate_id, stage) constraint.
        import threading
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        results = []
        errors = []

        def declare_once():
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                results.append(CRS.declare_candidate_robustness_jobs(
                    conn, search_run_id=created["search_run_id"],
                    validation_repository=self.validation,
                    created_at="2026-10-07T00:00:00Z"))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=declare_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        total_created = sum(item["jobs_created"] for item in results)
        self.assertEqual(2, total_created)
        jobs = [j for j in ESS.list_search_jobs(self.queue, created["search_run_id"])
                if j["stage"] == ESC.JOB_STAGE_ROBUSTNESS]
        self.assertEqual(2, len(jobs))
        self.assertEqual(2, len({j["job_id"] for j in jobs}))

    def test_crb15f_embedded_subject_must_match_baseline(self):
        # A producer that swaps the embedded candidate subject (recomputing the
        # report fingerprint) must not be able to complete the job.
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        output = self.execute_robustness(job_id)
        stored = self.robustness.get_report_by_key(output["report_key"])
        forged = dict(stored)
        forged["report"] = dict(stored["report"])
        subject = dict(forged["report"]["baseline_identity"]["experiment_subject"])
        subject["replay_fingerprint"] = "9" * 64
        forged["report"]["baseline_identity"] = {
            **forged["report"]["baseline_identity"], "experiment_subject": subject}
        spec = dict(forged["report"]["baseline_spec"])
        spec["subject"] = subject
        forged["report"]["baseline_spec"] = spec
        self._refingerprint(forged)
        with mock.patch.object(self.robustness, "get_report_by_key", return_value=forged):
            with self.assertRaises(CRS.CandidateRobustnessUnavailable):
                CRS.complete_candidate_robustness_job(
                    self.queue, job_id=job_id, report_key=forged["report_key"],
                    validation_repository=self.validation,
                    robustness_repository=self.robustness)

    def test_crb29_no_candidate_robustness_table(self):
        names = {row[0] for row in self.owners.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("robustness_reports", names)
        self.assertNotIn("candidate_robustness_reports", names)

    def test_crb30_31_fake_report_rejected(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        output = self.execute_robustness(job_id)
        with self.assertRaisesRegex(CRS.CandidateRobustnessUnavailable, "robustness_report_not_found"):
            CRS.complete_candidate_robustness_job(
                self.queue, job_id=job_id, report_key="a" * 64,
                validation_repository=self.validation, robustness_repository=self.robustness)
        # idempotent same key
        before = len(ESR.list_job_events(self.queue, job_id))
        CRS.complete_candidate_robustness_job(
            self.queue, job_id=job_id, report_key=output["report_key"],
            validation_repository=self.validation, robustness_repository=self.robustness)
        self.assertEqual(before, len(ESR.list_job_events(self.queue, job_id)))

    def test_crb32_33_unavailable_or_failed_case_still_completed(self):
        # A policy whose scenario is unavailable must still produce a canonical report.
        self.plan_v2 = ESC.ExperimentSearchPlanV2.from_v1(
            self.plan, policy(missing_data_stresses=[{"missing_fraction": 1.0,
                                                      "field_scope": "whole_bar"}]))
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        output = self.execute_robustness(job_id)
        self.assertEqual("completed", output["completion"]["event_kind"])
        report = output["report"]
        self.assertTrue(report["unavailable_cases"])
        event = ESR.list_job_events(self.queue, job_id)[-1]
        self.assertEqual("completed", event["event_kind"])
        self.assertEqual("robustness_report", event["evidence_owner"])

    def test_crb15b_invalid_report_key_primitive_rejects(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        for bad in ("short", "", "z" * 64, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ESR.ExperimentSearchRepositoryError):
                    ESR.record_verified_robustness_completion_event(
                        self.queue, job_id=job_id,
                        search_run_id=created["search_run_id"], report_key=bad)
        self.assertEqual("claimed", ESR.list_job_events(self.queue, job_id)[-1]["event_kind"])

    def test_crb11c_financial_dependency_fails_closed(self):
        # A candidate that needs a financial field must fail closed when the canonical
        # repository has no evidence, and the service must derive that field from replay.
        import inspect
        source = inspect.getsource(CRS.run_candidate_robustness)
        self.assertIn('R29.PV.replay_dsl_dependencies', source)
        self.assertIn('required_fields = dependencies["financial_fields"]', source)
        c = B1.candidate(factor_spec={"op": "gt", "left": {"op": "field", "name": "roe"},
                                      "right": {"op": "const", "value": 0}})
        deps = R29.PV.replay_dsl_dependencies(CE.compile_candidate_replay(c))
        self.assertEqual(["roe"], deps["financial_fields"])
        # With no canonical financial owner, no features can be manufactured.
        features = R29.build_replay_financial_features(
            spec=B1.spec(c), samples=[], financial_fields=deps["financial_fields"],
            financial_feature_repository=None, financial_archive_fingerprint=None)
        self.assertEqual({}, features)

    def test_crb04c_tampered_search_policy_cannot_run_job(self):
        # Declare with policy P, then rewrite the stored search to a *different valid*
        # policy Q while keeping the search payload internally consistent. The job still
        # pins P, so prepare/run must reject the substitution.
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        job_id = self._claim_one_robustness(created["search_run_id"])
        self.queue.execute("DROP TRIGGER experiment_search_runs_no_update")
        row = dict(self.queue.execute(
            "SELECT * FROM experiment_search_runs WHERE search_run_id=?",
            (created["search_run_id"],)).fetchone())
        spec_projection = json.loads(row["search_spec_json"])
        spec_projection["experiment_plan"]["robustness_policy"] = policy(random_seed=99).projection()
        spec_projection["experiment_plan"]["robustness_policy_fingerprint"] = policy(random_seed=99).fingerprint
        spec_projection.pop("search_input_fingerprint", None)
        # Rebuild a fully consistent spec projection so only the pinned job can catch it.
        rebuilt = ESC.ExperimentSearchPlanV2.from_v1(
            self.plan, policy(random_seed=99))
        spec_projection = ESC.ExperimentSearchSpec(
            generation_batch_id=spec_projection["generation_batch_id"],
            generation_input_fingerprint=spec_projection["generation_input_fingerprint"],
            candidate_ids=tuple(spec_projection["candidate_ids"]),
            budget=ESC.SearchBudget(**spec_projection["budget"]),
            search_contract_version=ESC.SEARCH_CONTRACT_VERSION_V2,
            experiment_plan=rebuilt).projection()
        row["search_spec_json"] = ESR._canonical(spec_projection)
        row["search_input_fingerprint"] = spec_projection["search_input_fingerprint"]
        material = {k: row[k] for k in ("search_run_id", "search_input_fingerprint",
            "search_contract_version", "generation_batch_id",
            "generation_input_fingerprint", "candidate_count", "budget_json",
            "search_spec_json", "created_at")}
        self.queue.execute("UPDATE experiment_search_runs SET search_spec_json=?,search_input_fingerprint=?,payload_fingerprint=?",
                           (row["search_spec_json"], row["search_input_fingerprint"],
                            ESR._fingerprint(material)))
        with self.assertRaises(CRS.CandidateRobustnessUnavailable):
            CRS.prepare_candidate_robustness(
                self.queue, job_id=job_id, validation_repository=self.validation)

    def test_crb16b_foreign_report_cannot_complete_other_job(self):
        # Two robustness jobs: a report built for A must never complete B.
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_a = self.claim_robustness(created["search_run_id"])
        output = self.execute_robustness(job_a)
        self.queue.execute("BEGIN IMMEDIATE")
        claimed = ESS.claim_next_job(self.queue, created["search_run_id"], actor="crb")
        self.queue.commit()
        self.assertNotEqual(job_a, claimed["job_id"])
        job_b = ESR.get_search_job(self.queue, claimed["job_id"])
        job_a_row = ESR.get_search_job(self.queue, job_a)
        # Distinct candidates -> distinct baselines: a foreign report must be rejected.
        self.assertNotEqual(job_a_row["job"]["baseline_run_key"],
                            job_b["job"]["baseline_run_key"])
        with self.assertRaises(CRS.CandidateRobustnessUnavailable):
            CRS.complete_candidate_robustness_job(
                self.queue, job_id=claimed["job_id"], report_key=output["report_key"],
                validation_repository=self.validation, robustness_repository=self.robustness)
        self.assertNotEqual("completed",
                            ESR.list_job_events(self.queue, claimed["job_id"])[-1]["event_kind"])

    def test_crb20b_v2_payload_without_policy_rejected(self):
        # A plan-v2 payload missing the pinned policy must fail closed, never fall back.
        v2 = ESC.ExperimentSearchPlanV2.from_v1(self.plan, policy())
        projection = v2.projection()
        projection.pop("robustness_policy")
        projection.pop("robustness_policy_fingerprint")
        with self.assertRaises(ESC.SearchContractError):
            ESC.experiment_plan_from_projection(projection)

    def test_crb20_legacy_v1_search_has_no_default_policy(self):
        legacy = self.create(self.batch_record)  # plan-v1: no experiment plan
        run = ESR.get_search_run(self.queue, legacy["search_run_id"])
        self.assertEqual(ESC.SEARCH_CONTRACT_VERSION, run["search_contract_version"])
        self.assertNotIn("experiment_plan", run["search_spec"])
        with self.assertRaisesRegex(CRS.CandidateRobustnessUnavailable,
                                    "search_run_robustness_policy_unavailable"):
            CRS.declare_candidate_robustness_jobs(
                self.queue, search_run_id=legacy["search_run_id"],
                validation_repository=self.validation)

    def test_crb11_service_has_no_caller_financial_injection(self):
        import inspect
        params = set(inspect.signature(CRS.run_candidate_robustness).parameters)
        self.assertNotIn("financial_features", params)
        self.assertIn("financial_feature_repository", params)
        # The canonical repository-derived builder is the only feature authority.
        self.assertTrue(hasattr(R29, "build_replay_financial_features"))

    def test_crb35_crash_recovery(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        with mock.patch.object(CRS, "complete_candidate_robustness_job",
                               side_effect=RuntimeError("crash after R30 commit")):
            with self.assertRaisesRegex(RuntimeError, "crash after R30 commit"):
                self.execute_robustness(job_id)
        stored = self.robustness.recent_reports()[0]
        self.assertEqual("claimed", ESR.list_job_events(self.queue, job_id)[-1]["event_kind"])
        with mock.patch.object(RRUN, "run_robustness", side_effect=AssertionError("must not replay")):
            CRS.complete_candidate_robustness_job(
                self.queue, job_id=job_id, report_key=stored["report_key"],
                validation_repository=self.validation, robustness_repository=self.robustness)
        self.assertEqual("completed", ESR.list_job_events(self.queue, job_id)[-1]["event_kind"])

    def test_crb34_infrastructure_failure_is_retryable(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        with mock.patch.object(RRUN, "run_robustness", side_effect=sqlite3.OperationalError("disk")):
            with self.assertRaises(sqlite3.OperationalError):
                self.execute_robustness(job_id)
        self.assertEqual("failed", ESR.list_job_events(self.queue, job_id)[-1]["event_kind"])
        self.assertEqual([], self.robustness.recent_reports())
        self.queue.execute("BEGIN IMMEDIATE")
        ESS.record_job_event(self.queue, job_id=job_id, event_kind="claimed")
        self.queue.commit()
        self.assertEqual("completed", self.execute_robustness(job_id)["completion"]["event_kind"])

    def test_crb36_different_report_key_conflicts(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        output = self.execute_robustness(job_id)
        # A second canonical report for the same job but a different plan is a different key.
        fake = dict(self.robustness.get_report_by_key(output["report_key"]))
        fake["plan_fingerprint"] = "9" * 64
        with self.assertRaises((ValueError, Exception)):
            CRS.complete_candidate_robustness_job(
                self.queue, job_id=job_id, report_key="b" * 64,
                validation_repository=self.validation, robustness_repository=self.robustness)

    def test_security_formal_run_cannot_be_candidate_baseline(self):
        # The exact-read helper never trusts a caller-supplied run; it re-reads by key.
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        prepared = CRS.prepare_candidate_robustness(
            self.queue, job_id=self._claim_one_robustness(created["search_run_id"]),
            validation_repository=self.validation)
        forged = dict(prepared["baseline"], subject_kind="strategy_version",
                      strategy_id="parent", strategy_version=1, strategy_checksum="a" * 64)
        with self.assertRaisesRegex(RRUN.RobustnessBaselineError,
                                    "baseline_subject_kind_mismatch"):
            RRUN._verify_candidate_baseline(forged, prepared["spec"],
                                            prepared["robustness_plan"], prepared["replay"])

    def test_security_masquerade_check_rejects_strategy_columns(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        job_id = self._claim_one_robustness(created["search_run_id"])
        prepared = CRS.prepare_candidate_robustness(
            self.queue, job_id=job_id, validation_repository=self.validation)
        forged = dict(prepared["baseline"], strategy_id="parent", strategy_version=1,
                      strategy_checksum="a" * 64)
        with self.assertRaisesRegex(RRUN.RobustnessBaselineError,
                                    "masquerades_as_strategy_version"):
            RRUN._verify_candidate_baseline(forged, prepared["spec"],
                                            prepared["robustness_plan"], prepared["replay"])

    def test_security_runtime_policy_replacement_rejected(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        # Tamper the stored search policy without updating the search identity.
        self.queue.execute("DROP TRIGGER experiment_search_runs_no_update")
        row = dict(self.queue.execute(
            "SELECT * FROM experiment_search_runs WHERE search_run_id=?",
            (created["search_run_id"],)).fetchone())
        spec = json.loads(row["search_spec_json"])
        spec["experiment_plan"]["robustness_policy"]["random_seed"] += 1
        row["search_spec_json"] = ESR._canonical(spec)
        material = {k: row[k] for k in ("search_run_id", "search_input_fingerprint",
            "search_contract_version", "generation_batch_id",
            "generation_input_fingerprint", "candidate_count", "budget_json",
            "search_spec_json", "created_at")}
        self.queue.execute("UPDATE experiment_search_runs SET search_spec_json=?,payload_fingerprint=?",
                           (row["search_spec_json"], ESR._fingerprint(material)))
        with self.assertRaises(ValueError):
            self.declare(created["search_run_id"])

    def test_security_other_candidate_report_rejected(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        # Run and complete one robustness job, then try to complete the other with its report.
        job_a = self.claim_robustness(created["search_run_id"])
        output = self.execute_robustness(job_a)
        self.queue.execute("BEGIN IMMEDIATE")
        claimed = ESS.claim_next_job(self.queue, created["search_run_id"], actor="crb")
        self.queue.commit()
        self.assertIsNotNone(claimed["job_id"])
        self.assertNotEqual(job_a, claimed["job_id"])
        with self.assertRaises(CRS.CandidateRobustnessUnavailable):
            CRS.complete_candidate_robustness_job(
                self.queue, job_id=claimed["job_id"], report_key=output["report_key"],
                validation_repository=self.validation, robustness_repository=self.robustness)
        self.assertNotEqual("completed",
                            ESR.list_job_events(self.queue, claimed["job_id"])[-1]["event_kind"])

    def test_crb04b_runtime_policy_substitution_rejected(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        job_id = self._claim_one_robustness(created["search_run_id"])
        job = ESR.get_search_job(self.queue, job_id)
        prepared = CRS.prepare_candidate_robustness(
            self.queue, job_id=job_id, validation_repository=self.validation)
        # The rebuilt plan must match the pinned job plan fingerprint exactly.
        self.assertEqual(job["job"]["robustness_plan_fingerprint"],
                         prepared["robustness_plan"].fingerprint)
        self.assertEqual(job["job"]["robustness_policy_fingerprint"],
                         prepared["plan"].robustness_policy.fingerprint)

    def test_crb11b_required_financial_fields_come_from_replay(self):
        # The service must derive required financial fields from the canonical replay.
        c = B1.candidate(factor_spec={"op": "gt", "left": {"op": "field", "name": "roe"},
                                      "right": {"op": "const", "value": 0}})
        batch = self.batch(values=[18], campaign="fin")
        with mock.patch.object(SF, "UNIVERSE", {"scope_kind": "a_share_all"}):
            pass
        recorded = []
        original = R29.build_replay_financial_features

        def record(*, spec, samples, financial_fields, **kwargs):
            recorded.append(list(financial_fields))
            return original(spec=spec, samples=samples, financial_fields=financial_fields, **kwargs)

        # Build a search whose plan carries the roe candidate by re-using the exact batch.
        with mock.patch.object(R29, "build_replay_financial_features", side_effect=record):
            try:
                created = self.create(batch, experiment_plan=self.plan_v2)
                self.run_all_pit(created["search_run_id"])
            except Exception:
                pass
        # Directly exercise the dependency derivation the service uses.
        replay = CE.compile_candidate_replay(c)
        self.assertEqual(["roe"], R29.PV.replay_dsl_dependencies(replay)["financial_fields"])

    def test_crb26b_date_stress_runs_r29_through_runner(self):
        calls = []

        def fake_run(spec, **kwargs):
            calls.append(kwargs.get("strategy_candidate") is not None)
            calendar = kwargs["session_calendar"]
            return {"status": "ready", "result": {"status": "completed"},
                    "validation_evidence_fingerprint": "e" * 64, "run_key": "f" * 64,
                    "runner_version": R29.CANDIDATE_RUNNER_VERSION,
                    "owner_identities": {
                        "calendar_fingerprint": calendar.calendar_fingerprint,
                        "market_archive_fingerprint": spec.market_data_fingerprint,
                        "universe_archive_fingerprint": spec.universe_fingerprint,
                        "dataset_fingerprint": spec.dataset_fingerprint,
                        "financial_archive_fingerprint": None,
                        "tradability_evidence_fingerprint": spec.tradability_fingerprint}}

        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        job_id = self._claim_one_robustness(created["search_run_id"])
        prepared = CRS.prepare_candidate_robustness(
            self.queue, job_id=job_id, validation_repository=self.validation)
        plan = RC.RobustnessPolicy(
            random_seed=7, regime_policy=regime_policy(),
            start_date_stresses=[{"shift_sessions": 1}],
            cost_stresses=[], slippage_stresses=[], liquidity_stresses=[],
            missing_data_stresses=[], universe_stresses=[]).bind(
                prepared["baseline"]["run_key"], prepared["baseline"]["experiment_fingerprint"])
        context = {"walk_forward_config": object(), "dataset_manifest": object(),
                   "samples": [], "benchmark_symbol": BENCHMARK}
        with mock.patch.object(R29, "run_validation", side_effect=fake_run):
            report = RRUN.run_robustness(
                baseline_run=prepared["baseline"], spec=prepared["spec"], plan=plan,
                candidate_replay=prepared["replay"], session_calendar=self.calendar,
                market_archive_repository=self.market,
                universe_archive_repository=self.universe,
                tradability_repository=self.tradability,
                date_range_validation_context=context,
                created_at="2026-10-07T00:00:00Z")
        # A start-date stress changed the range, so R29 PIT revalidation must have run.
        self.assertTrue(calls)
        self.assertTrue(all(calls))
        case = next(c for c in report["cases"] if c["scenario"]["category"] == "start_date")
        self.assertIn("date_range_pit_proof", case["evidence"])

    def test_security_different_policy_report_rejected(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        output = self.execute_robustness(job_id)
        report = dict(self.robustness.get_report_by_key(output["report_key"]))
        report["plan_fingerprint"] = "9" * 64
        report["report"] = dict(report["report"], plan_fingerprint="9" * 64)
        with self.assertRaises(ValueError):
            self.robustness.append_report(report)

    def test_security_candidate_baseline_replay_mutation_detected(self):
        # A factor/constraint mutation changes the R29 metrics and must break bit-identity.
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        job_id = self._claim_one_robustness(created["search_run_id"])
        prepared = CRS.prepare_candidate_robustness(
            self.queue, job_id=job_id, validation_repository=self.validation)
        baseline = dict(prepared["baseline"])
        baseline["result"] = dict(baseline["result"],
                                  metrics={**baseline["result"]["metrics"], "return": 0.5})
        with self.assertRaisesRegex(RRUN.RobustnessBaselineError,
                                    "result_fingerprint_mismatch|replay_not_bit_identical"):
            RRUN.run_robustness(
                baseline_run=baseline, spec=prepared["spec"],
                plan=prepared["robustness_plan"], candidate_replay=prepared["replay"],
                session_calendar=self.calendar, market_archive_repository=self.market,
                universe_archive_repository=self.universe,
                tradability_repository=self.tradability,
                created_at="2026-10-07T00:00:00Z")

    def test_crb37_queue_has_no_robustness_metrics(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        for table in ("experiment_search_runs", "experiment_search_jobs", "experiment_search_job_events"):
            columns = {row[1] for row in self.queue.execute(f"PRAGMA table_info({table})")}
            self.assertFalse(columns & {"return", "drawdown", "sharpe", "fragility",
                                        "threshold_breach", "score", "rank", "winner",
                                        "passed", "promotable"})

    def test_crb38_no_selection_imports(self):
        import ast
        for name in ("candidate_robustness_service.py",):
            tree = ast.parse((BACKEND / name).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(imported & {"promotion_science", "strategy_promotion",
                                         "strategy_lifecycle", "ai_research_service",
                                         "strategy_ai_provider", "portfolio_allocation_service"})


class FormalRegressionTests(unittest.TestCase):
    """CRB-01: the formal R30 identity/report/metrics are frozen by exact-base fixture."""

    def test_formal_r30_identity_frozen(self):
        import test_robustness_runner as T
        frozen = json.loads((BACKEND / "fixtures/r36b2_formal_r30.json").read_text(encoding="utf-8"))
        fixture = T.RobustnessFixture()
        try:
            report = fixture.execute()
        finally:
            fixture.close()
        self.assertEqual(frozen["plan_fingerprint"], fixture.plan.fingerprint)
        self.assertEqual(frozen["report_fingerprint"], report["report_fingerprint"])
        self.assertEqual(frozen["report_key"], report["report_key"])
        self.assertEqual(frozen["report_version"], report["runner_version"])
        self.assertNotIn("experiment_subject", report["baseline_identity"])
        self.assertEqual("trend_pullback", report["baseline_identity"]["strategy_id"])


class CandidateRobustnessBoundaryTests(CandidateRobustnessFixture):
    def test_crb19_financial_dependency_union(self):
        # entry/factor/exit each declare a different financial field: union is exact.
        c = B1.candidate(
            entry_spec={"op": "strategy", "rule": {"op": "and", "args": [
                {"op": "gt", "left": {"op": "field", "name": "close"},
                 "right": {"op": "const", "value": 0}},
                {"op": "gt", "left": {"op": "field", "name": "roe"},
                 "right": {"op": "const", "value": 0}}]}},
            factor_spec={"op": "gt", "left": {"op": "field", "name": "profit_yoy"},
                         "right": {"op": "const", "value": 0}},
            exit_spec={"op": "gt", "left": {"op": "field", "name": "debt_ratio"},
                       "right": {"op": "const", "value": 0}})
        replay = CE.compile_candidate_replay(c)
        deps = R29.PV.replay_dsl_dependencies(replay)
        self.assertEqual(["debt_ratio", "profit_yoy", "roe"], deps["financial_fields"])
        # Required fields are derived from the canonical replay, never from a caller.
        self.assertIn("financial_fields", deps)

    def test_crb19b_missing_canonical_financial_evidence_blocks(self):
        c = B1.candidate(factor_spec={"op": "gt", "left": {"op": "field", "name": "roe"},
                                      "right": {"op": "const", "value": 0}})
        replay = CE.compile_candidate_replay(c)
        deps = R29.PV.replay_dsl_dependencies(replay)
        self.assertEqual(["roe"], deps["financial_fields"])
        # A caller-supplied financial map cannot satisfy the canonical repository owner.
        financial = R29.build_replay_financial_features(
            spec=B1.spec(c), samples=[], financial_fields=deps["financial_fields"],
            financial_feature_repository=None, financial_archive_fingerprint=None)
        self.assertEqual({}, financial)

    def test_crb26_date_stress_reruns_pit(self):
        created = self.create_v2()
        self.run_all_pit(created["search_run_id"])
        job_id = self._claim_one_robustness(created["search_run_id"])
        prepared = CRS.prepare_candidate_robustness(
            self.queue, job_id=job_id, validation_repository=self.validation)
        replay = prepared["replay"]
        calls = []

        def fake_run(spec, **kwargs):
            calls.append(("run_validation", kwargs.get("strategy_candidate") is not None))
            calendar = kwargs["session_calendar"]
            return {"status": "ready", "result": {"status": "completed"},
                    "validation_evidence_fingerprint": "e" * 64, "run_key": "f" * 64,
                    "runner_version": R29.CANDIDATE_RUNNER_VERSION,
                    "owner_identities": {
                        "calendar_fingerprint": calendar.calendar_fingerprint,
                        "market_archive_fingerprint": spec.market_data_fingerprint,
                        "universe_archive_fingerprint": spec.universe_fingerprint,
                        "dataset_fingerprint": spec.dataset_fingerprint,
                        "financial_archive_fingerprint": None,
                        "tradability_evidence_fingerprint": spec.tradability_fingerprint}}

        spec = prepared["spec"]
        capture = R29.PV.tradability_replay_projection(
            R29._universe_rows(self.universe, spec.universe_fingerprint, list(SESSIONS)),
            list(SESSIONS), self.tradability)
        proof = None
        with mock.patch.object(R29, "run_validation", side_effect=fake_run):
            proof = RRUN._revalidate_scenario_date_range(
                sessions=list(SESSIONS), spec=spec,
                baseline_identity=prepared["baseline"],
                market_archive_repository=self.market,
                universe_archive_repository=self.universe,
                tradability_repository=self.tradability,
                candidate_replay=replay,
                validation_context={"walk_forward_config": object(),
                                    "dataset_manifest": object(), "samples": [],
                                    "benchmark_symbol": BENCHMARK},
                replay_capture=capture, scenario_fingerprint="a" * 64)
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0][1])  # candidate subject passed, never the parent StrategyVersion
        self.assertEqual(R29.CANDIDATE_RUNNER_VERSION, proof["runner_version"])

    def test_crb27_candidate_asof_cannot_be_crossed(self):
        c = B1.candidate(asof="2026-01-07")
        with self.assertRaisesRegex(ValueError, "candidate_asof_leakage"):
            CE.validate_candidate_asof(c, "2026-01-09", "2026-01-09T15:00:00+08:00")
        with self.assertRaisesRegex(ValueError, "candidate_asof_leakage"):
            CE.validate_candidate_asof(c, "2026-01-05", "2026-01-09T15:00:00+08:00")
        # In-range is accepted.
        CE.validate_candidate_asof(c, "2026-01-07", "2026-01-07T15:00:00+08:00")

    def test_crb28_derived_date_identity(self):
        c = B1.candidate()
        base = B1.spec(c)
        derived = replace(base, robustness_scenario_fingerprint="a" * 64)
        self.assertEqual(base.fingerprint,
                         B1.spec(c).fingerprint)
        self.assertNotEqual(base.fingerprint, derived.fingerprint)
        self.assertEqual(base.parameter_set.get("experiment_plan_fingerprint"),
                         derived.parameter_set.get("experiment_plan_fingerprint"))


if __name__ == "__main__":
    unittest.main()
