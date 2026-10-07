"""CEX-01–28: candidate replay, exact PIT authority and crash-safe queue binding."""
from __future__ import annotations

import ast
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
import experiment_execution_model as EM
import experiment_pit_validation as PV
import experiment_search_contract as ESC
import experiment_search_repository as ESR
import experiment_search_service as ESS
import experiment_validation_repository as EVR
import experiment_validation_runner as RUNNER
import historical_market_archive as HMA
import historical_session_calendar as HSC
import historical_universe_archive as HUA
import strategy_candidate as SC
import tradability_archive as TA
import walk_forward_validation as WFV
import test_experiment_execution_model as XF
import test_experiment_pit_validation as PF
import test_r36a_experiment_search_controller as SF


def rule(field="close", threshold=0):
    return {"op": "gt", "left": {"op": "field", "name": field},
            "right": {"op": "const", "value": threshold}}


def candidate(**changes):
    values = dict(parent_identity={"strategy_id": "parent", "strategy_version": 1,
                                  "strategy_checksum": "a" * 64},
                  entry_spec=rule(), universe_spec={"scope_kind": "a_share_all"},
                  intended_market_regime="neutral", asof="2026-03-31", constraints={})
    values.update(changes)
    return SC.build_strategy_candidate(**values)


def plan(**changes):
    legacy = XF._spec()
    values = dict(code_revision=legacy.code_revision, dataset_fingerprint=legacy.dataset_fingerprint,
                  market_archive_fingerprint=legacy.market_data_fingerprint,
                  universe_archive_fingerprint=legacy.universe_fingerprint,
                  session_calendar_fingerprint="9" * 64, start_date=XF.SESSIONS[0], end_date=XF.SESSIONS[-1],
                  asof_policy={"policy_id": "pit-close-v1", "cutoff": "2026-01-06T15:00:00+08:00"},
                  execution_assumptions=legacy.execution_assumptions, cost_model=legacy.cost_model,
                  validation_portfolio={"initial_cash": 100000, "max_positions": 3, "position_sizing": "equal_weight"},
                  walk_forward_config=WFV.WalkForwardConfig(min_train_sessions=1, validation_sessions=1, test_sessions=1),
                  random_seed=17)
    values.update(changes)
    return ESC.ExperimentSearchPlan(**values)


def spec(c=None, p=None):
    c = c or candidate()
    return CE.build_candidate_experiment_spec(c, CE.compile_candidate_replay(c), p or plan(), "e" * 64)


def simulate(c, *, closes=(10, 10, 10, 10), names=("000001.SH",), portfolio=None, members=None):
    days = tuple(f"2026-01-0{i + 1}" for i in range(len(closes)))
    p = plan(start_date=days[0], end_date=days[-1],
             cost_model={**XF._spec().cost_model, "slippage_parameters": {"rate": 0}},
             validation_portfolio=portfolio or {"initial_cash": 100000, "max_positions": 3, "position_sizing": "equal_weight"})
    bars = [dict(XF._bar(day), code=name, close=close, open=10, volume=1000000)
            for day, close in zip(days, closes, strict=True) for name in names]
    facts = {(name, day): replace(XF._evidence(), code=name, session_date=day,
             observed_at=day + "T09:00:00+08:00", effective_at=day + "T09:00:00+08:00")
             for day in days for name in names}
    return EM.simulate(spec(c, p), candidate_replay=CE.compile_candidate_replay(c), sessions=days,
                       members_by_session=members if members is not None else {day: [{"code": n} for n in names] for day in days},
                       bars=bars, tradability_repository=None, tradability_evidence=facts, include_trace=True)


class CandidateContractTests(unittest.TestCase):
    def test_cex01_legacy_identity_frozen(self):
        frozen = json.loads((BACKEND / "fixtures/r36b1_legacy_experiment.json").read_text(encoding="utf-8"))
        self.assertEqual(frozen["projection"], XF._spec().projection())
        self.assertEqual(frozen["fingerprint"], XF._spec().fingerprint)

    def test_cex02_candidate_identity(self):
        self.assertNotEqual(spec().fingerprint, spec(candidate(entry_spec=rule(threshold=5))).fingerprint)
        self.assertNotEqual(spec().fingerprint, spec(candidate(parent_identity={"strategy_id": "parent", "strategy_version": 2,
                                                                               "strategy_checksum": "b" * 64})).fingerprint)

    def test_cex03_all_candidate_facts_bind_identity(self):
        for changes in ({"factor_spec": rule("roe")}, {"exit_spec": rule(threshold=9)},
                        {"constraints": {"max_positions": 1}},
                        {"universe_spec": {"scope_kind": "explicit_symbols", "symbols": ["000001"]}}):
            with self.subTest(changes=changes):
                self.assertNotEqual(spec().fingerprint, spec(candidate(**changes)).fingerprint)

    def test_cex04_compiler_rejects_tampering_and_ast_substitution(self):
        c = candidate()
        for key, value in (("candidate_id", "b" * 64), ("entry_spec", rule(threshold=99)),
                           ("factor_spec", rule()), ("exit_spec", rule())):
            with self.subTest(key=key), self.assertRaises(ValueError):
                CE.compile_candidate_replay(replace(c, **{key: value}))
        with self.assertRaises(TypeError):
            CE.CandidateReplayDefinition(c, ast=rule())
        with self.assertRaisesRegex(EM.ExperimentExecutionUnavailable, "candidate_replay_definition_required"):
            EM.simulate(spec(c), ast=rule(threshold=999), sessions=XF.SESSIONS,
                        members_by_session={}, bars=[], tradability_repository=None)
        with self.assertRaises(ValueError):
            CE.CandidateReplayDefinition(c, replay_contract_version="future")

    def test_cex05_factor_gates_entry(self):
        self.assertEqual(0, simulate(candidate(factor_spec=rule(threshold=20)))["trade_count"])
        self.assertEqual(1, simulate(candidate(factor_spec=rule(threshold=5)))["trade_count"])

    def test_cex06_factor_does_not_force_exit(self):
        result = simulate(candidate(factor_spec=rule(threshold=5)), closes=(10, 1, 1, 1))
        self.assertEqual(1, result["trade_count"])

    def test_cex07_explicit_exit_owns_exit(self):
        # Entry stops being true, but explicit exit remains false: keep the position.
        self.assertEqual(1, simulate(candidate(entry_spec=rule(threshold=5), exit_spec=rule(threshold=20)),
                                     closes=(10, 1, 1, 1))["trade_count"])
        # Entry remains true, and explicit exit turns true: sell at the next open.
        self.assertEqual(2, simulate(candidate(exit_spec=rule(threshold=15)),
                                     closes=(10, 20, 20))["trade_count"])

    def test_explicit_exit_is_not_replaced_by_universe_exclusion(self):
        members = {"2026-01-01": [{"code": "000001.SH"}], "2026-01-02": [{"code": "000001.SH"}], "2026-01-03": []}
        # Missing membership cannot manufacture an exit order; missing held-position marks block replay.
        with self.assertRaisesRegex(EM.ExperimentExecutionUnavailable, "market_bar_missing_for_open_position"):
            simulate(candidate(exit_spec=rule(threshold=99)), closes=(10, 10, 10), members=members)

    def test_cex08_absent_exit_inverse_entry(self):
        self.assertEqual(2, simulate(candidate(entry_spec=rule(threshold=5)), closes=(10, 1, 1))["trade_count"])

    def test_cex09_dependency_union(self):
        replay = CE.compile_candidate_replay(candidate(factor_spec=rule("roe"), exit_spec=rule("profit_yoy")))
        deps = PV.replay_dsl_dependencies(replay)
        self.assertEqual(["profit_yoy", "roe"], deps["financial_fields"])
        self.assertEqual(["close"], deps["price_fields"])
        evidence = PV.build_pit_validation_evidence(spec(replay.candidate), candidate_replay=replay,
                                                  dataset_manifest={"dataset_fingerprint": "c" * 64})
        self.assertEqual("blocked", evidence.dimensions["fundamental_pit"]["status"])
        self.assertIn("financial_feature_evidence_missing", evidence.reason_codes)

    def test_cex10_explicit_symbols_and_pinned_universe(self):
        c = candidate(universe_spec={"scope_kind": "explicit_symbols", "symbols": ["000001"],
                                     "asof_universe_identity": "d" * 64})
        replay = CE.compile_candidate_replay(c)
        members = {"2026-01-01": [{"code": "000001.SH"}, {"code": "600000.SH"}]}
        self.assertEqual({"2026-01-01": [{"code": "000001.SH"}]}, CE.filter_candidate_members(replay, members, "d" * 64))
        with self.assertRaisesRegex(ValueError, "candidate_universe_identity_mismatch"):
            CE.filter_candidate_members(replay, members, "f" * 64)
        self.assertEqual(1, simulate(c, names=("000001.SH", "600000.SH"))["trade_count"])

    def test_cex11_boards_fail_closed(self):
        c = candidate(universe_spec={"scope_kind": "a_share_boards", "boards": ["main_board"]})
        with self.assertRaisesRegex(ValueError, "candidate_universe_board_scope_not_supported"):
            CE.filter_candidate_members(CE.compile_candidate_replay(c), {}, "d" * 64)

    def test_cex12_asof_leakage(self):
        for p in (plan(end_date="2026-04-01"), plan(asof_policy={"policy_id": "pit-close-v1", "cutoff": "2026-04-01T00:00:00+08:00"})):
            with self.assertRaisesRegex(ValueError, "candidate_asof_leakage"):
                spec(candidate(), p)

    def test_cex13_constraints(self):
        names = ("000001.SH", "000002.SH", "000003.SH")
        self.assertEqual(1, simulate(candidate(constraints={"max_positions": 1}), names=names)["trade_count"])
        for constraints, limit in (({"max_weight_pct": .1}, .3), ({"max_exposure_pct": .15}, .15)):
            result = simulate(candidate(constraints=constraints), names=names)
            self.assertLessEqual(result["turnover"], limit + 1e-10)
            self.assertGreater(result["turnover"], 0)

    def test_cex14_plan_order_independent(self):
        p = plan()
        self.assertEqual(p.fingerprint, replace(p, validation_portfolio=dict(reversed(list(p.validation_portfolio.items())))).fingerprint)
        self.assertEqual(p, ESC.experiment_plan_from_projection(p.projection()))
        with self.assertRaises(TypeError):
            p.validation_portfolio["initial_cash"] = 1

    def test_cex15_plan_facts_change_search_identity(self):
        p = plan()
        base = ESC.ExperimentSearchSpec("a" * 64, "b" * 64, ("c" * 64,), ESC.SearchBudget(1),
                                       ESC.SEARCH_CONTRACT_VERSION_V2, experiment_plan=p)
        variants = [replace(p, **{key: value}) for key, value in (
            ("code_revision", "e" * 40), ("dataset_fingerprint", "e" * 64),
            ("market_archive_fingerprint", "e" * 64), ("universe_archive_fingerprint", "e" * 64),
            ("session_calendar_fingerprint", "e" * 64), ("financial_archive_fingerprint", "e" * 64),
            ("start_date", "2026-01-04"), ("end_date", "2026-01-07"), ("random_seed", 18),
            ("asof_policy", {"policy_id": "pit-close-v1", "cutoff": "2026-01-07T15:00:00+08:00"}),
            ("validation_portfolio", {"initial_cash": 200000, "max_positions": 3, "position_sizing": "equal_weight"}),
            ("walk_forward_config", WFV.WalkForwardConfig(min_train_sessions=2, validation_sessions=1, test_sessions=1)),
            ("cost_model", {**p.cost_model, "commission_rate": .001}),
            ("execution_assumptions", {**p.execution_assumptions, "t_plus_one_semantics": "changed"}))]
        for variant in variants:
            self.assertNotEqual(base.search_input_fingerprint, replace(base, experiment_plan=variant).search_input_fingerprint)

    def test_cex19_legacy_migration_verbatim_and_idempotent(self):
        frozen = json.loads((BACKEND / "fixtures/r36b1_legacy_validation.json").read_text(encoding="utf-8"))
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.execute(frozen["ddl"])
        old = frozen["row"]
        conn.execute(f"INSERT INTO experiment_validation_runs({','.join(old)}) VALUES({','.join('?' for _ in old)})", tuple(old.values()))
        conn.commit()
        repository = EVR.ExperimentValidationRepository(conn)
        EVR.ensure_schema(conn)
        raw = dict(conn.execute("SELECT * FROM experiment_validation_runs").fetchone())
        self.assertEqual(old, {k: raw[k] for k in old})
        decoded = repository.get_run(run_key=old["run_key"])
        self.assertEqual(EVR.RECORD_V1, decoded["record_contract_version"])
        self.assertEqual("strategy_version", decoded["subject_kind"])
        self.assertEqual(old["payload_fingerprint"], repository.append_run(**frozen["append_args"])["payload_fingerprint"])
        self.assertEqual(old["run_key"], repository.build_run_key(old["experiment_fingerprint"],
                         {"dataset_fingerprint": old["dataset_fingerprint"]}, old["runner_version"]))
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM experiment_validation_runs").fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM experiment_validation_runs")

    def test_cex20_formal_replay_metrics_frozen(self):
        frozen = json.loads((BACKEND / "fixtures/r36b1_legacy_experiment.json").read_text(encoding="utf-8"))
        self.assertEqual(frozen["metrics"], XF._simulate(signal=True, evidence={(XF.CODE, XF.SESSIONS[1]): XF._evidence()}))

    def test_cex28_architecture(self):
        pure = ast.parse((BACKEND / "candidate_experiment.py").read_text(encoding="utf-8"))
        imports = {n.names[0].name for n in ast.walk(pure) if isinstance(n, ast.Import)}
        self.assertFalse(imports & {"sqlite3", "os", "pathlib", "datetime", "requests", "strategy_registry"})
        callsites = []
        for path in BACKEND.glob("*.py"):
            if path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "record_verified_completion_event":
                    callsites.append(path.name)
            if path.name in {"candidate_experiment.py", "candidate_experiment_service.py", "experiment_validation_runner.py"}:
                self.assertFalse(any(isinstance(n, (ast.Import, ast.ImportFrom)) and
                    "robustness" in ast.unparse(n) for n in ast.walk(tree)))
        self.assertEqual(["candidate_experiment_service.py"], callsites)


class CandidateExecutionTests(SF._Base):
    def setUp(self):
        super().setUp()
        self.queue = sqlite3.connect(self.path, isolation_level=None)
        self.queue.row_factory = sqlite3.Row
        self.addCleanup(self.queue.close)
        self.owners = sqlite3.connect(":memory:")
        self.owners.row_factory = sqlite3.Row
        self.addCleanup(self.owners.close)
        self.market = HMA.HistoricalMarketArchiveRepository(self.owners)
        days = PF._sessions()
        rows = [dict(XF._bar(day), code="600000.SH", volume=100000) for day in days]
        manifest = self.market.import_raw_market_archive(rows, source="cex-owner", source_revision="1", adjustment="raw",
            benchmark_calendars={"600000.SH": {"coverage_start": days[0], "coverage_end": days[-1], "sessions": days,
                                               "source": "exchange-export", "source_revision": "1"}})
        self.calendar = HSC.issue_from_market_archive(self.market, archive_fingerprint=manifest.archive_fingerprint,
            benchmark_symbol="600000.SH", start=days[0], end=days[-1])
        self.universe = HUA.HistoricalUniverseArchiveRepository(self.owners)
        um = self.universe.import_historical_security_master([{"code": "600000.SH", "listed_from": "2020-01-01", "delisted_at": None,
            "security_type": "equity", "exchange": "SH", "observed_at": "2020-01-01T09:00:00+08:00"}],
            coverage_start="2020-01-01", coverage_end=days[-1], source="cex-owner", source_revision="1")
        self.tradability = TA.TradabilityArchiveRepository(self.owners)
        self.tradability.ensure_schema()
        for day in days:
            self.tradability.save(replace(XF._evidence(), code="600000.SH", session_date=day,
                observed_at=day + "T09:00:00+08:00", effective_at=day + "T09:00:00+08:00"))
        self.validation = EVR.ExperimentValidationRepository(self.owners)
        self.owners.commit()
        self.plan = plan(start_date=days[0], end_date=days[-1], market_archive_fingerprint=manifest.archive_fingerprint,
                         universe_archive_fingerprint=um.universe_archive_fingerprint,
                         session_calendar_fingerprint=self.calendar.calendar_fingerprint)
        self.batch_record = self.batch(values=[18, 19])
        self.created = self.create(self.batch_record, experiment_plan=self.plan)
        self.queue.execute("BEGIN IMMEDIATE")
        claimed = ESS.claim_next_job(self.queue, self.created["search_run_id"], actor="cex")
        self.queue.commit()
        self.job_id = claimed["job_id"]

    def prepare(self):
        return CES.prepare_candidate_experiment(self.queue, job_id=self.job_id, session_calendar=self.calendar,
            universe_archive_repository=self.universe, tradability_repository=self.tradability)

    def execute(self, **changes):
        args = dict(job_id=self.job_id, validation_repository=self.validation, session_calendar=self.calendar,
            market_archive_repository=self.market, universe_archive_repository=self.universe,
            tradability_repository=self.tradability, dataset_manifest={"dataset_fingerprint": self.plan.dataset_fingerprint},
            samples=PF._samples(), created_at="2026-10-07T00:00:00Z")
        args.update(changes)
        return CES.run_candidate_pit_validation(self.queue, **args)

    def finish(self, key):
        return CES.complete_candidate_pit_job(self.queue, job_id=self.job_id, run_key=key, validation_repository=self.validation)

    def test_cex16_corrupt_search_plan_rejected(self):
        self.queue.execute("DROP TRIGGER experiment_search_runs_no_update")
        for column, value in (("search_contract_version", "future"), ("candidate_count", 99), ("budget_json", '{}'),
                              ("search_spec_json", '{}'), ("search_input_fingerprint", "e" * 64)):
            old = self.queue.execute(f"SELECT {column} FROM experiment_search_runs WHERE search_run_id=?", (self.created["search_run_id"],)).fetchone()[0]
            self.queue.execute(f"UPDATE experiment_search_runs SET {column}=?", (value,))
            with self.assertRaisesRegex(ValueError, "corrupt_search_run"):
                ESR.get_search_run(self.queue, self.created["search_run_id"])
            self.queue.execute(f"UPDATE experiment_search_runs SET {column}=?", (old,))

    def test_corrupt_plan_with_rehashed_storage_payload_rejected(self):
        self.queue.execute("DROP TRIGGER experiment_search_runs_no_update")
        row = dict(self.queue.execute("SELECT * FROM experiment_search_runs").fetchone())
        projection = json.loads(row["search_spec_json"])
        projection["experiment_plan"]["random_seed"] += 1
        row["search_spec_json"] = ESR._canonical(projection)
        material = {k: row[k] for k in ("search_run_id", "search_input_fingerprint", "search_contract_version",
                    "generation_batch_id", "generation_input_fingerprint", "candidate_count", "budget_json",
                    "search_spec_json", "created_at")}
        self.queue.execute("UPDATE experiment_search_runs SET search_spec_json=?,payload_fingerprint=?",
                           (row["search_spec_json"], ESR._fingerprint(material)))
        with self.assertRaisesRegex(ValueError, "corrupt_search_run"):
            ESR.get_search_run(self.queue, self.created["search_run_id"])

    def test_cex17_v1_readable_but_not_executable(self):
        legacy = self.create(self.batch_record)
        self.assertEqual(ESC.SEARCH_CONTRACT_VERSION, ESR.get_search_run(self.queue, legacy["search_run_id"])["search_contract_version"])
        job = ESC.SearchJobSpec(legacy["search_run_id"], self.prepare()["candidate"].candidate_id)
        with self.assertRaisesRegex(ValueError, "search_run_experiment_plan_unavailable"):
            CES._job_inputs(self.queue, job.job_id)

    def test_cex18_subject_columns_and_formal_views(self):
        output = self.execute()
        row = self.validation.get_run(run_key=output["run_key"])
        self.assertEqual("strategy_candidate", row["subject_kind"])
        self.assertIsNone(row["strategy_id"])
        self.assertEqual(self.prepare()["candidate"].parent_strategy_id, row["parent_strategy_id"])
        self.assertEqual([], self.validation.recent_runs(strategy_id=row["parent_strategy_id"]))
        self.assertEqual(EVR.RECORD_V2, row["record_contract_version"])
        self.assertIn("experiment_subject", row["validation_evidence"]["dimensions"])
        self.assertNotIn("strategy_version", row["validation_evidence"]["dimensions"])

    def test_subject_sql_constraint_rejects_null_fingerprint_and_formal_columns(self):
        output = self.execute()
        raw = dict(self.owners.execute("SELECT * FROM experiment_validation_runs WHERE run_key=?", (output["run_key"],)).fetchone())
        raw.pop("id")
        raw["run_key"] = "e" * 64
        for changes in ({"candidate_fingerprint": None}, {"strategy_id": "parent", "strategy_version": 1, "strategy_checksum": "a" * 64}):
            mutated = {**raw, **changes}
            with self.assertRaises(sqlite3.IntegrityError):
                self.owners.execute(f"INSERT INTO experiment_validation_runs({','.join(mutated)}) VALUES({','.join('?' for _ in mutated)})", tuple(mutated.values()))
            self.owners.rollback()

    def test_cex21_runner_subject_and_plan_mismatch(self):
        prepared = self.prepare()
        args = dict(runner_code_revision=self.plan.code_revision, strategy_candidate=candidate(), dataset_manifest=None,
                    samples=(), walk_forward_config=self.plan.walk_forward_config, session_calendar=None,
                    market_archive_repository=None, market_archive_fingerprint=self.plan.market_archive_fingerprint,
                    universe_archive_repository=None, universe_archive_fingerprint=self.plan.universe_archive_fingerprint,
                    tradability_repository=None)
        output = RUNNER.run_validation(prepared["spec"], **args)
        self.assertEqual("candidate_identity_mismatch", output["result"]["failure_reason"])
        self.assertIsNone(output["run_key"])
        args["strategy_candidate"] = prepared["candidate"]
        args["walk_forward_config"] = replace(self.plan.walk_forward_config, min_train_sessions=99)
        output = RUNNER.run_validation(prepared["spec"], **args)
        self.assertEqual("candidate_experiment_plan_mismatch", output["result"]["failure_reason"])

    def test_cex22_exact_verified_evidence_completion(self):
        output = self.execute()
        self.assertEqual("ready", output["validation_evidence"]["status"])
        self.assertEqual("completed", output["result"]["status"])
        event = ESR.list_job_events(self.queue, self.job_id)[-1]
        self.assertEqual(("completed", "experiment_validation_run", output["run_key"]),
                         (event["event_kind"], event["evidence_owner"], event["evidence_id"]))
        row = self.validation.get_run(run_key=event["evidence_id"])
        self.assertEqual(self.prepare()["spec"].fingerprint, row["experiment_fingerprint"])

    def test_candidate_ast_reaches_real_r29_execution(self):
        c = candidate()
        replay = CE.compile_candidate_replay(c)
        capture = self.prepare()["tradability_replay_capture"]
        actual_spec = CE.build_candidate_experiment_spec(c, replay, self.plan, capture["fingerprint"])
        output = RUNNER.run_validation(actual_spec, runner_code_revision=self.plan.code_revision,
            strategy_candidate=c, dataset_manifest={"dataset_fingerprint": self.plan.dataset_fingerprint},
            samples=PF._samples(), walk_forward_config=self.plan.walk_forward_config, session_calendar=self.calendar,
            market_archive_repository=self.market, market_archive_fingerprint=self.plan.market_archive_fingerprint,
            universe_archive_repository=self.universe, universe_archive_fingerprint=self.plan.universe_archive_fingerprint,
            tradability_repository=self.tradability, validation_repository=self.validation,
            created_at="2026-10-07T00:00:00Z")
        self.assertEqual("completed", output["result"]["status"])
        self.assertEqual(1, output["result"]["metrics"]["trade_count"])
        self.assertEqual(c.candidate_id, output["run"]["candidate_id"])

    def test_cex23_fake_completion_rejected(self):
        for call in (ESS.record_job_event, ESR.record_job_event):
            with self.assertRaisesRegex(ValueError, "completion_evidence_binding_unavailable|illegal_job_state_transition"):
                call(self.queue, **({"search_run_id": self.created["search_run_id"]} if call is ESR.record_job_event else {}), job_id=self.job_id, event_kind="completed", evidence_owner="experiment_validation_run", evidence_id="a" * 64)
        with self.assertRaisesRegex(ValueError, "validation_evidence_not_found"):
            self.finish("a" * 64)
        with self.assertRaisesRegex(ValueError, "canonical_validation_repository_required"):
            CES.complete_candidate_pit_job(self.queue, job_id=self.job_id, run_key="a" * 64, validation_repository=object())
        self.assertEqual("claimed", ESR.list_job_events(self.queue, self.job_id)[-1]["event_kind"])

    def test_cex24_blocked_is_operationally_completed(self):
        output = self.execute(samples=())
        self.assertEqual("blocked", output["validation_evidence"]["status"])
        self.assertEqual("unavailable", output["result"]["status"])
        self.assertTrue(all(v is None for v in output["result"]["metrics"].values()))
        self.assertEqual("completed", ESR.list_job_events(self.queue, self.job_id)[-1]["event_kind"])

    def test_cex25_crash_recovery_no_reexecution(self):
        with mock.patch.object(CES, "complete_candidate_pit_job", side_effect=RuntimeError("crash after R29 commit")):
            with self.assertRaisesRegex(RuntimeError, "crash after R29"):
                self.execute()
        row = self.validation.recent_runs()[0]
        self.assertEqual("claimed", ESR.list_job_events(self.queue, self.job_id)[-1]["event_kind"])
        with mock.patch.object(RUNNER, "run_validation", side_effect=AssertionError("must not replay")):
            self.finish(row["run_key"])
        self.assertEqual("completed", ESR.list_job_events(self.queue, self.job_id)[-1]["event_kind"])

    def test_cex26_same_key_idempotent_different_key_conflicts(self):
        output = self.execute()
        before = len(ESR.list_job_events(self.queue, self.job_id))
        self.finish(output["run_key"])
        self.finish(output["run_key"])
        self.assertEqual(before, len(ESR.list_job_events(self.queue, self.job_id)))
        prepared = self.prepare()
        other = RUNNER.run_validation(prepared["spec"], runner_code_revision=self.plan.code_revision,
            strategy_candidate=prepared["candidate"], dataset_manifest={"dataset_fingerprint": self.plan.dataset_fingerprint},
            samples=(), walk_forward_config=self.plan.walk_forward_config, session_calendar=self.calendar,
            market_archive_repository=self.market, market_archive_fingerprint=self.plan.market_archive_fingerprint,
            universe_archive_repository=self.universe, universe_archive_fingerprint=self.plan.universe_archive_fingerprint,
            tradability_repository=self.tradability, validation_repository=self.validation, created_at="2026-10-07T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "completion_evidence_conflict"):
            self.finish(other["run_key"])

    def test_cex27_no_queue_metrics_and_no_write_lock_during_runner(self):
        for table in ("experiment_search_runs", "experiment_search_jobs", "experiment_search_job_events"):
            columns = {r[1] for r in self.queue.execute(f"PRAGMA table_info({table})")}
            self.assertFalse(columns & {"metrics", "total_return", "score", "rank", "passed", "promotion"})
        original = RUNNER.run_validation
        def checked(*args, **kwargs):
            self.assertFalse(self.queue.in_transaction)
            second = sqlite3.connect(self.path, timeout=.1)
            try:
                second.execute("BEGIN IMMEDIATE")
                second.rollback()
            finally:
                second.close()
            return original(*args, **kwargs)
        with mock.patch.object(RUNNER, "run_validation", side_effect=checked):
            self.execute()

    def test_corrupt_r29_payload_rejected_before_completion(self):
        with mock.patch.object(CES, "complete_candidate_pit_job", return_value={}):
            output = self.execute()
        self.owners.execute("DROP TRIGGER experiment_validation_runs_no_update")
        self.owners.execute("UPDATE experiment_validation_runs SET result_json='{}'")
        self.owners.commit()
        with self.assertRaisesRegex(ValueError, "corrupt_validation_run"):
            self.finish(output["run_key"])
        self.assertEqual("claimed", ESR.list_job_events(self.queue, self.job_id)[-1]["event_kind"])

    def test_canonical_failed_result_is_still_operational_completion(self):
        # A canonical computation failure has real PIT evidence and no success metrics.
        with mock.patch.object(EM, "simulate", side_effect=ArithmeticError("canonical evaluation failed")):
            output = self.execute()
        self.assertEqual("failed", output["result"]["status"])
        self.assertEqual("completed", ESR.list_job_events(self.queue, self.job_id)[-1]["event_kind"])

    def test_infrastructure_failure_inside_executor_is_not_canonical_completion(self):
        with mock.patch.object(EM, "simulate", side_effect=OSError("executor storage failure")):
            with self.assertRaises(OSError):
                self.execute()
        self.assertEqual("failed", ESR.list_job_events(self.queue, self.job_id)[-1]["event_kind"])
        self.assertEqual([], self.validation.recent_runs())

    def test_infrastructure_exception_marks_failed(self):
        with mock.patch.object(RUNNER, "run_validation", side_effect=sqlite3.OperationalError("disk failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.execute()
        self.assertEqual("failed", ESR.list_job_events(self.queue, self.job_id)[-1]["event_kind"])

    def test_other_candidate_run_cannot_complete(self):
        output = self.execute()
        self.queue.execute("BEGIN IMMEDIATE")
        second = ESS.claim_next_job(self.queue, self.created["search_run_id"])
        self.queue.commit()
        with self.assertRaisesRegex(ValueError, "validation_evidence_identity_mismatch"):
            CES.complete_candidate_pit_job(self.queue, job_id=second["job_id"], run_key=output["run_key"], validation_repository=self.validation)


if __name__ == "__main__":
    unittest.main()
