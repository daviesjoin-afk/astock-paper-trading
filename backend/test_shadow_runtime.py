from __future__ import annotations

import ast
import datetime as dt
import hashlib
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import db_migrate
import execution_planner as EP
import market_data_contract as MDC
import paper_quote_policy as PQP
import paper_schema_migrations as PSM
import paper_trading as PT
import shadow_run_repository as SRR
import shadow_runtime as SH
import shadow_run_service as SHS
import simulation_runtime_context as SRC
import tradability_archive as TA


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tradability(code, day, decision_at):
    return TA.TradabilityDecision(
        code=code, session_date=day, decision_time=decision_at,
        can_buy=True, can_sell=True,
        buy_block_reason=TA.TradabilityReason.OK,
        sell_block_reason=TA.TradabilityReason.OK,
        source="owner-fixture", effective_at=decision_at,
        observed_at=decision_at, fingerprint=_sha("tradability"),
        evidence_present=True,
    )


class ShadowRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.day = "2026-09-08"
        self.decision_at = "2026-09-08T10:05:00+08:00"
        self.code = "600000"
        self.quote = {
            "code": self.code, "name": "fixture", "price": 10.0,
            "amount": 50_000_000.0, "quote_at": "2026-09-08T10:00:00+08:00",
            "execution_asof": self.decision_at, "quote_source": "live",
            "quote_validation": "cross_source_checked",
            "quote_cross_check": {"source": ["owner-a"]},
        }
        self.reading = EP.market_reading_for_execution(
            self.quote, asof_day=self.day, execution_asof=self.decision_at,
        )
        self.tradability = _tradability(self.code, self.day, self.decision_at)
        self.factor = {"close": [10.0], "volume": [1000.0]}
        self.active = SH.StrategyStamp("active_fixture", 1, _sha("active-v1"))
        quote_snapshot = MDC.symbol_quote_snapshot(self.quote, asof_day=self.day)
        self.identity = SH.ComparableEnvironmentIdentity.build(
            session_date=self.day, decision_at=self.decision_at,
            market_policy_name=MDC.EXECUTION_QUOTE_POLICY.name,
            market_snapshot_fingerprint=MDC.snapshot_fingerprint(self.reading.snapshot),
            symbol_quote_fingerprints={self.code: MDC.snapshot_fingerprint(quote_snapshot)},
            symbol_factor_fingerprints={self.code: SH.fingerprint(self.factor)},
            tradability_evidence_fingerprints={f"{self.code}@{self.day}": _sha("tradability")},
            execution_ruleset_identity=EP.SIMULATION_EXECUTION_RULESET,
        )
        self.active_context = SRC.build_comparable_runtime_context(
            strategy_id=self.active.strategy_id, strategy_version=self.active.version,
            strategy_checksum=self.active.checksum, session_date=self.day,
            decision_at=self.decision_at,
            market_policy_name=MDC.EXECUTION_QUOTE_POLICY.name,
            market_snapshot_fingerprint=self.identity.market_snapshot_fingerprint,
            symbol_quote_fingerprints=dict(self.identity.symbol_quote_fingerprints),
            tradability_evidence_fingerprints=dict(self.identity.tradability_evidence_fingerprints),
            execution_ruleset_version=EP.SIMULATION_EXECUTION_RULESET,
            risk_policy_identity={"active_capture": "fixture"},
            execution_state_fingerprint=_sha("active-execution-state"),
        )
        self.environment = SH.FrozenShadowEnvironment(
            identity=self.identity, active_runtime_context=self.active_context,
            market_reading=self.reading,
            quotes={self.code: self.quote}, tradability={self.code: self.tradability},
            factor_snapshots={self.code: self.factor},
        )
        self.challenger = SH.StrategyStamp("challenger_fixture", 2, _sha("challenger-v2"))
        self.spec = SH.ShadowRunSpec(
            challenger=self.challenger, active_comparator=self.active,
            environment_fingerprint=self.identity.environment_fingerprint,
            session_date=self.day, decision_at=self.decision_at,
            reference_capital=100_000.0,
        )
        self.strategy_version = types.SimpleNamespace(
            strategy_id=self.challenger.strategy_id, version=self.challenger.version,
            checksum=self.challenger.checksum,
            definition={"dsl_ast": {"op": "gt", "left": {"op": "field", "name": "close"},
                                    "right": {"op": "const", "value": 0}}},
        )
        self.candidate = SH.ShadowCandidate(
            symbol=self.code, side="buy", desired_quantity=100,
            entry_state=EP.EntryGateState(
                capacity_available=True, position_limit=5, pool_limit=5,
                shared_cash=100_000.0,
                require_market_gate=False,
            ),
            risk_policy_identity={"captured_policy_fingerprint": _sha("risk-policy")},
            reference_price=10.0,
        )

    def _run(self, *, spec=None, environment=None, previous=None, lifecycle="shadow"):
        return SH.evaluate_shadow(
            spec=spec or self.spec, environment=environment or self.environment,
            strategy_version=self.strategy_version, lifecycle_state=lifecycle,
            candidates=(self.candidate,), previous_run=previous,
        )

    def test_c1_same_frozen_inputs_produce_same_shadow_evidence(self):
        with mock.patch.object(PT, "_execution_quote_status", return_value={
                "fresh": True, "status": "cross_source_checked"}):
            first, second = self._run(), self._run()
        self.assertEqual(first.run_fingerprint, second.run_fingerprint)
        self.assertEqual(first.projection(), second.projection())
        self.assertEqual(first.decisions[0]["execution"]["fill_quantity"], 100)

    def test_environment_identity_ignores_mapping_insertion_order(self):
        self.assertEqual(SH.fingerprint({"first": 1, "second": 2}),
                         SH.fingerprint({"second": 2, "first": 1}))
        self.assertEqual(SH.fingerprint({"members": {"a", "b", "c"}}),
                         SH.fingerprint({"members": set(("c", "a", "b"))}))

    def test_frozen_environment_detaches_nested_mutable_inputs(self):
        original = self.environment.projection()
        self.quote["quote_cross_check"]["source"][0] = "changed"
        self.factor["close"][0] = 999.0
        self.assertEqual(self.environment.projection(), original)
        with self.assertRaises(TypeError):
            self.environment.quotes[self.code]["quote_cross_check"]["source"] = ("changed",)

    def test_frozen_environment_requires_owner_typed_tradability_decision(self):
        counterfeit = types.SimpleNamespace(
            code=self.code, session_date=self.day, decision_time=self.decision_at,
            can_buy=True, can_sell=True, fingerprint=_sha("tradability"),
            evidence_present=True, source="fake", effective_at=self.decision_at,
            observed_at=self.decision_at,
        )
        with self.assertRaises(SH.ShadowRuntimeError) as caught:
            replace(self.environment, tradability={self.code: counterfeit})
        self.assertEqual(caught.exception.availability, "NOT_COMPARABLE")

    def test_c6_each_shared_environment_identity_dimension_fails_not_comparable(self):
        base = {
            "session_date": self.identity.session_date,
            "decision_at": self.identity.decision_at,
            "market_policy_name": self.identity.market_policy_name,
            "market_snapshot_fingerprint": self.identity.market_snapshot_fingerprint,
            "symbol_quote_fingerprints": dict(self.identity.symbol_quote_fingerprints),
            "symbol_factor_fingerprints": dict(self.identity.symbol_factor_fingerprints),
            "tradability_evidence_fingerprints": dict(
                self.identity.tradability_evidence_fingerprints),
            "execution_ruleset_identity": self.identity.execution_ruleset_identity,
        }
        changed = [
            {"market_snapshot_fingerprint": _sha("other-market")},
            {"decision_at": "2026-09-08T10:06:00+08:00"},
            {"symbol_quote_fingerprints": {self.code: _sha("other-quote")}},
            {"tradability_evidence_fingerprints": {
                f"{self.code}@{self.day}": _sha("other-tradability")}},
        ]
        for delta in changed:
            with self.subTest(delta=tuple(delta)):
                altered = SH.ComparableEnvironmentIdentity.build(**(base | delta))
                with self.assertRaises(SH.ShadowRuntimeError) as caught:
                    replace(self.environment, identity=altered)
                self.assertEqual(caught.exception.availability, "NOT_COMPARABLE")

    def test_c6_each_captured_shared_dimension_fails_not_comparable(self):
        """The Active capture must match the environment on every shared dimension."""
        base = {
            "strategy_id": self.active.strategy_id, "strategy_version": self.active.version,
            "strategy_checksum": self.active.checksum, "session_date": self.day,
            "decision_at": self.decision_at,
            "market_policy_name": MDC.EXECUTION_QUOTE_POLICY.name,
            "market_snapshot_fingerprint": self.identity.market_snapshot_fingerprint,
            "symbol_quote_fingerprints": dict(self.identity.symbol_quote_fingerprints),
            "tradability_evidence_fingerprints": dict(
                self.identity.tradability_evidence_fingerprints),
            "execution_ruleset_version": EP.SIMULATION_EXECUTION_RULESET,
            "risk_policy_identity": {"active_capture": "fixture"},
            "execution_state_fingerprint": _sha("active-execution-state"),
        }
        changed = [
            {"session_date": "2026-09-09"},
            {"decision_at": "2026-09-08T10:06:00+08:00"},
            {"market_policy_name": "other-quote-policy"},
            {"market_snapshot_fingerprint": _sha("other-market")},
            {"symbol_quote_fingerprints": {self.code: _sha("other-quote")}},
            {"tradability_evidence_fingerprints": {
                f"{self.code}@{self.day}": _sha("other-tradability")}},
            {"execution_ruleset_version": "simulation-execution-ruleset-v0"},
        ]
        for delta in changed:
            with self.subTest(dimension=next(iter(delta))):
                captured = SRC.build_comparable_runtime_context(**(base | delta))
                with self.assertRaises(SH.ShadowRuntimeError) as caught:
                    replace(self.environment, active_runtime_context=captured)
                self.assertEqual(caught.exception.availability, "NOT_COMPARABLE")
                self.assertEqual(caught.exception.reason_code,
                                 "shadow_environment_not_comparable")
        canonical = SRC.build_comparable_runtime_context(**base)
        self.assertEqual(
            replace(self.environment, active_runtime_context=canonical).identity,
            self.identity,
        )

    def test_c2_entry_freshness_uses_supplied_decision_instant(self):
        real_freshness = PQP.quote_is_fresh
        wall_clock = {"date": self.day, "at": f"{self.day}T10:05:00+08:00"}

        def clocked_freshness(quote, asof_day, *, date_fn, reference_at=None,
                              today_fn=dt.date.today, now_fn=dt.datetime.now):
            return real_freshness(
                quote, asof_day, date_fn=date_fn,
                today_fn=lambda: dt.date.fromisoformat(wall_clock["date"]),
                now_fn=lambda _tz=None: dt.datetime.fromisoformat(wall_clock["at"]),
                reference_at=reference_at,
            )

        results = []
        with mock.patch.object(PQP, "quote_is_fresh", side_effect=clocked_freshness):
            for machine_day, machine_time in (
                    (self.day, "10:05"), (self.day, "10:30"), ("2026-09-09", "10:00")):
                wall_clock["date"] = machine_day
                wall_clock["at"] = f"{machine_day}T{machine_time}:00+08:00"
                result = self._run()
                results.append(result.run_fingerprint)
                self.assertTrue(result.decisions[0]["entry"]["gates"]["execution_quote"]["fresh"])
        self.assertEqual(len(set(results)), 1)

    def test_c3_provider_and_archive_lookups_are_not_used(self):
        tree = ast.parse(Path(SH.__file__).read_text(encoding="utf-8"))
        forbidden = {"marketdata_providers", "marketdata_cache"}
        imports = {alias.name.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module.split(".")[0] for node in ast.walk(tree)
                       if isinstance(node, ast.ImportFrom) and node.module)
        self.assertFalse(forbidden & imports)
        with mock.patch("tradability_archive.TradabilityArchiveRepository.evidence_at",
                        side_effect=AssertionError("archive read")) as evidence_at, \
                mock.patch("tradability_archive.TradabilityArchiveRepository.evidence_many",
                           side_effect=AssertionError("archive read")) as evidence_many:
            result = self._run()
        evidence_at.assert_not_called()
        evidence_many.assert_not_called()
        self.assertEqual(result.environment["identity"]["environment_fingerprint"],
                         self.identity.environment_fingerprint)

    def test_c4_shadow_repository_does_not_change_formal_ledgers(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys=ON")
        for table, ddl in {
            "paper_accounts": "CREATE TABLE paper_accounts(id TEXT PRIMARY KEY,cash REAL)",
            "paper_positions": "CREATE TABLE paper_positions(id INTEGER PRIMARY KEY,qty INTEGER)",
            "paper_position_lots": "CREATE TABLE paper_position_lots(id INTEGER PRIMARY KEY,qty INTEGER)",
            "paper_orders": "CREATE TABLE paper_orders(id INTEGER PRIMARY KEY,status TEXT)",
            "paper_fills": "CREATE TABLE paper_fills(id INTEGER PRIMARY KEY,qty INTEGER)",
            "paper_capital_reservations": "CREATE TABLE paper_capital_reservations(id INTEGER PRIMARY KEY,amount REAL)",
            "paper_position_risk_state": (
                "CREATE TABLE paper_position_risk_state(cycle_id INTEGER,account_id TEXT,"
                "code TEXT,peak_price REAL,take_stage INTEGER)"
            ),
            "paper_risk_decisions": (
                "CREATE TABLE paper_risk_decisions(id INTEGER PRIMARY KEY,account_id TEXT,decision TEXT)"
            ),
        }.items():
            conn.execute(ddl)
        conn.execute("INSERT INTO paper_accounts VALUES('formal',1234.5)")
        conn.execute("INSERT INTO paper_positions VALUES(1,300)")
        conn.execute("INSERT INTO paper_position_lots VALUES(1,200)")
        conn.execute("INSERT INTO paper_orders VALUES(1,'pending')")
        conn.execute("INSERT INTO paper_fills VALUES(1,100)")
        conn.execute("INSERT INTO paper_capital_reservations VALUES(1,50)")
        conn.execute("INSERT INTO paper_position_risk_state VALUES(1,'formal','600001',10.0,1)")
        conn.execute("INSERT INTO paper_risk_decisions VALUES(1,'formal','hold')")
        migration = next(item for item in db_migrate.MIGRATIONS["paper_trading"] if item[0] == 24)
        db_migrate._run_operation(conn, migration[2])
        tables = ("paper_accounts", "paper_positions", "paper_position_lots", "paper_orders",
                  "paper_fills", "paper_capital_reservations",
                  "paper_position_risk_state", "paper_risk_decisions")
        before = {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                  for name in tables}
        evidence = self._run()
        SRR.append_run(conn, evidence)
        SRR.append_run(conn, evidence)
        after = {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                 for name in tables}
        self.assertEqual(before, after)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shadow_runs").fetchone()[0], 1)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            conn.execute("UPDATE shadow_runs SET session_date='2026-09-09' WHERE run_id=?",
                         (evidence.run_id,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            conn.execute("DELETE FROM shadow_runs WHERE run_id=?", (evidence.run_id,))
        conn.close()

    def test_production_service_resolves_exact_stamp_and_appends_shadow_only(self):
        conn = sqlite3.connect(":memory:")
        migration = next(item for item in db_migrate.MIGRATIONS["paper_trading"] if item[0] == 24)
        db_migrate._run_operation(conn, migration[2])
        with mock.patch.object(SH, "resolve_exact_shadow_strategy",
                               return_value=(self.strategy_version, "shadow")) as resolve, \
                mock.patch.object(PT, "_execution_quote_status", return_value={
                    "fresh": True, "status": "cross_source_checked"}):
            evidence = SHS.run_shadow(conn, spec=self.spec, environment=self.environment,
                                      candidates=(self.candidate,))
        resolve.assert_called_once_with(conn, self.challenger)
        self.assertEqual(SRR.get_run(conn, evidence.run_id).run_fingerprint,
                         evidence.run_fingerprint)
        conn.close()

    def test_c5_only_shadow_lifecycle_is_runnable(self):
        with self.assertRaisesRegex(ValueError, "challenger_lifecycle_not_shadow"):
            self._run(lifecycle="paper")

    def test_c6_environment_mismatch_is_rejected(self):
        with self.assertRaisesRegex(SH.ShadowRuntimeError,
                                    "shadow_environment_not_comparable") as caught:
            self._run(spec=replace(self.spec,
                                   environment_fingerprint=_sha("other-environment")))
        self.assertEqual(caught.exception.availability, "NOT_COMPARABLE")

    def test_c7_continuation_requires_explicit_previous_run(self):
        continuation = replace(self.spec, previous_shadow_run_id=_sha("missing-run"))
        with self.assertRaisesRegex(ValueError, "explicit_previous_shadow_run_required"):
            self._run(spec=continuation)

    def test_c8_replaying_named_run_is_independent_of_later_run(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys=ON")
        migration = next(item for item in db_migrate.MIGRATIONS["paper_trading"] if item[0] == 24)
        db_migrate._run_operation(conn, migration[2])
        with mock.patch.object(PT, "_execution_quote_status", return_value={
                "fresh": True, "status": "cross_source_checked"}):
            original = self._run()
        SRR.append_run(conn, original)
        later_spec = replace(self.spec, previous_shadow_run_id=original.run_id,
                             session_date="2026-09-09", decision_at="2026-09-09T10:05:00+08:00")
        later_quote = dict(self.quote, quote_at=later_spec.decision_at,
                           execution_asof=later_spec.decision_at)
        later_reading = EP.market_reading_for_execution(
            later_quote, asof_day=later_spec.session_date,
            execution_asof=later_spec.decision_at,
        )
        later_quote_snapshot = MDC.symbol_quote_snapshot(later_quote, asof_day=later_spec.session_date)
        later_trad = _tradability(self.code, later_spec.session_date, later_spec.decision_at)
        later_identity = SH.ComparableEnvironmentIdentity.build(
            session_date=later_spec.session_date, decision_at=later_spec.decision_at,
            market_policy_name=MDC.EXECUTION_QUOTE_POLICY.name,
            market_snapshot_fingerprint=MDC.snapshot_fingerprint(later_reading.snapshot),
            symbol_quote_fingerprints={self.code: MDC.snapshot_fingerprint(later_quote_snapshot)},
            symbol_factor_fingerprints={self.code: SH.fingerprint(self.factor)},
            tradability_evidence_fingerprints={f"{self.code}@{later_spec.session_date}": _sha("tradability")},
            execution_ruleset_identity=EP.SIMULATION_EXECUTION_RULESET,
        )
        later_active_context = SRC.build_comparable_runtime_context(
            strategy_id=self.active.strategy_id, strategy_version=self.active.version,
            strategy_checksum=self.active.checksum, session_date=later_spec.session_date,
            decision_at=later_spec.decision_at,
            market_policy_name=MDC.EXECUTION_QUOTE_POLICY.name,
            market_snapshot_fingerprint=later_identity.market_snapshot_fingerprint,
            symbol_quote_fingerprints=dict(later_identity.symbol_quote_fingerprints),
            tradability_evidence_fingerprints=dict(later_identity.tradability_evidence_fingerprints),
            execution_ruleset_version=EP.SIMULATION_EXECUTION_RULESET,
            risk_policy_identity={"active_capture": "fixture"},
            execution_state_fingerprint=_sha("active-execution-state"),
        )
        later_environment = SH.FrozenShadowEnvironment(
            identity=later_identity, active_runtime_context=later_active_context,
            market_reading=later_reading,
            quotes={self.code: later_quote}, tradability={self.code: later_trad},
            factor_snapshots={self.code: self.factor},
        )
        later_spec = replace(later_spec, environment_fingerprint=later_identity.environment_fingerprint)
        with mock.patch.object(PT, "_execution_quote_status", return_value={
                "fresh": True, "status": "cross_source_checked"}):
            later = SH.evaluate_shadow(spec=later_spec, environment=later_environment,
                                       strategy_version=self.strategy_version, lifecycle_state="shadow",
                                       candidates=(self.candidate,),
                                       previous_run=SRR.get_run(conn, original.run_id))
            SRR.append_run(conn, later)
        self.assertEqual(later.before_state["session_date"], later_spec.session_date)
        self.assertEqual(later.before_state["sellable_quantity"], {self.code: 100})
        self.assertEqual(later.before_state["session_consumed_quantity"], {})
        self.assertEqual(SRR.get_run(conn, original.run_id).run_fingerprint,
                         original.run_fingerprint)
        with mock.patch.object(PT, "_execution_quote_status", return_value={
                "fresh": True, "status": "cross_source_checked"}):
            self.assertEqual(self._run().run_fingerprint, original.run_fingerprint)
        conn.close()

    def test_shadow_entry_cash_is_bounded_by_reference_capital(self):
        spec = replace(self.spec, reference_capital=500.0)
        candidate = replace(
            self.candidate,
            entry_state=replace(self.candidate.entry_state, shared_cash=10_000_000.0),
        )
        with mock.patch.object(PT, "_execution_quote_status", return_value={
                "fresh": True, "status": "cross_source_checked"}):
            result = SH.evaluate_shadow(
                spec=spec, environment=self.environment,
                strategy_version=self.strategy_version, lifecycle_state="shadow",
                candidates=(candidate,),
            )
        entry = result.decisions[0]["entry"]
        self.assertFalse(entry["allowed"])
        self.assertEqual(entry["gates"]["cash"]["shared_cash"], 500.0)
        self.assertIsNone(result.decisions[0]["execution"])

    def test_shadow_entry_capacity_ignores_formal_portfolio_occupancy(self):
        candidate = replace(
            self.candidate,
            entry_state=replace(
                self.candidate.entry_state,
                position_limit=1, pool_limit=1,
                open_codes=frozenset({"formal-1", "formal-2"}),
                committed_open_codes=frozenset({"formal-1", "formal-2"}),
                pool_open_positions=frozenset({("other-formal", "formal-1")}),
            ),
        )
        with mock.patch.object(PT, "_execution_quote_status", return_value={
                "fresh": True, "status": "cross_source_checked"}):
            result = SH.evaluate_shadow(
                spec=self.spec, environment=self.environment,
                strategy_version=self.strategy_version, lifecycle_state="shadow",
                candidates=(candidate,),
            )
        entry = result.decisions[0]["entry"]
        self.assertTrue(entry["allowed"])
        self.assertEqual(entry["gates"]["position_count_gate"]["current"], 0)
        self.assertEqual(entry["gates"]["position_count_gate"]["pool_current"], 0)

    def test_shadow_entry_capacity_still_binds_the_challenger_position_limit(self):
        """隔离正式持仓不等于取消挑战者自己的席位上限。"""
        previous = SH.ShadowRunEvidence(
            run_id=_sha("previous-run"), run_fingerprint=_sha("previous-run"),
            spec={**self.spec.projection(),
                  "decision_at": "2026-09-08T09:55:00+08:00",
                  "previous_shadow_run_id": None},
            environment={}, strategy_definition_fingerprint=_sha("definition"),
            before_state=SH.ShadowRuntimeState.initial(
                100_000.0, self.day).projection(),
            decisions=(),
            after_state=SH.ShadowRuntimeState(
                session_date=self.day, reference_cash=99_000.0,
                positions={"999999": 100},
            ).projection(),
            previous_run_fingerprint=None,
        )
        spec = replace(self.spec, previous_shadow_run_id=previous.run_id)

        def entry_at_limit(limit):
            candidate = replace(
                self.candidate,
                entry_state=replace(self.candidate.entry_state,
                                    position_limit=limit, pool_limit=limit),
            )
            result = SH.evaluate_shadow(
                spec=spec, environment=self.environment,
                strategy_version=self.strategy_version, lifecycle_state="shadow",
                candidates=(candidate,), previous_run=previous,
            )
            return result.decisions[0]

        blocked = entry_at_limit(1)
        self.assertFalse(blocked["entry"]["allowed"])
        self.assertIsNone(blocked["execution"])
        self.assertEqual(
            blocked["entry"]["gates"]["position_count_gate"]["current"], 1)
        self.assertTrue(any("席位" in reason for reason in blocked["entry"]["reasons"]))
        self.assertTrue(entry_at_limit(2)["entry"]["allowed"])

    def test_forged_runtime_context_fingerprint_is_rejected(self):
        """重建校验不是恒真：手改 context_fingerprint 的 context 必须被拒。"""
        forged = replace(self.active_context, context_fingerprint=_sha("forged"))
        with self.assertRaises(SH.ShadowRuntimeError) as caught:
            replace(self.environment, active_runtime_context=forged)
        self.assertEqual(caught.exception.availability, "NOT_COMPARABLE")

    def test_shadow_run_evidence_keeps_candidate_inputs(self):
        """run evidence 必须能读回本次运行的输入，且与决策实际用的隔离状态分开。"""
        captured = replace(
            self.candidate,
            entry_state=replace(self.candidate.entry_state, shared_cash=1.0),
        )
        with mock.patch.object(PT, "_execution_quote_status", return_value={
                "fresh": True, "status": "cross_source_checked"}):
            result = SH.evaluate_shadow(
                spec=self.spec, environment=self.environment,
                strategy_version=self.strategy_version, lifecycle_state="shadow",
                candidates=(captured,),
            )
        candidate = result.decisions[0]["candidate"]
        self.assertEqual(candidate["desired_quantity"], 100)
        self.assertEqual(candidate["reference_price"], 10.0)
        self.assertEqual(candidate["order_type"], "market")
        self.assertEqual(candidate["risk_policy_identity"],
                         {"captured_policy_fingerprint": _sha("risk-policy")})
        # 记录的是调用方捕获的原始输入……
        self.assertEqual(candidate["entry_gate_state"]["shared_cash"], 1.0)
        self.assertEqual(candidate["entry_gate_state"]["position_limit"], 5)
        self.assertEqual(candidate["entry_gate_state"]["open_codes"], [])
        # ……而决策仍然只按 Shadow reference capital 评估。
        entry = result.decisions[0]["entry"]
        self.assertTrue(entry["allowed"])
        self.assertEqual(entry["gates"]["cash"]["shared_cash"], 100_000.0)

    def test_duplicate_candidate_legs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "shadow_candidates_must_be_unique"):
            SH.evaluate_shadow(
                spec=self.spec, environment=self.environment,
                strategy_version=self.strategy_version, lifecycle_state="shadow",
                candidates=(self.candidate, self.candidate),
            )

    def test_c3_decision_path_opens_no_database(self):
        """决策路径完全不碰 DB：任何 sqlite3.connect 都视为失败。"""
        with mock.patch("sqlite3.connect",
                        side_effect=AssertionError("shadow opened a database")), \
                mock.patch.object(PT, "_execution_quote_status", return_value={
                    "fresh": True, "status": "cross_source_checked"}):
            result = self._run()
        self.assertEqual(result.decisions[0]["execution"]["fill_quantity"], 100)

    def test_v24_shadow_schema_has_one_ddl_owner_and_is_idempotent(self):
        migration = next(item for item in db_migrate.MIGRATIONS["paper_trading"]
                         if item[0] == 24)
        self.assertIs(migration[2], PSM.ensure_shadow_runs_table)
        conn = sqlite3.connect(":memory:")
        try:
            self.assertEqual(PSM.ensure_shadow_runs_table(conn),
                             {"shadow_runs": "created"})
            self.assertEqual(PSM.ensure_shadow_runs_table(conn),
                             {"shadow_runs": "ok"})
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
            self.assertIn("shadow_runs", names)
            self.assertIn("shadow_runs_no_update", names)
            self.assertIn("shadow_runs_no_delete", names)
        finally:
            conn.close()

    def test_existing_ledger_init_db_creates_shadow_runs(self):
        """既有账本走 init_db 快路径时也必须拿到 ShadowRun 表。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "paper.sqlite3")
            calls = []
            real = PSM.ensure_shadow_runs_table
            with mock.patch.object(PT, "DB_PATH", path), \
                    mock.patch.object(PT, "_benchmark_close", return_value=None), \
                    mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True), \
                    mock.patch.object(PSM, "ensure_shadow_runs_table",
                                      side_effect=lambda conn: (calls.append(conn),
                                                                real(conn))[1]):
                PT.init_db()
                conn = sqlite3.connect(path)
                conn.execute("DROP TRIGGER shadow_runs_no_update")
                conn.execute("DROP TRIGGER shadow_runs_no_delete")
                conn.execute("DROP TABLE shadow_runs")
                conn.commit()
                conn.close()
                calls.clear()
                PT.init_db()
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(len(calls), 1)
                names = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
                self.assertIn("shadow_runs", names)
                self.assertIn("shadow_runs_no_update", names)
                self.assertIn("shadow_runs_no_delete", names)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
