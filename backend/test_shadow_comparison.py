from __future__ import annotations

import ast
import hashlib
import json
import os
import re
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
import paper_schema_migrations as PSM
import paper_trading as PT
import shadow_comparison as SC
import shadow_comparison_repository as SCR
import shadow_comparison_service as SCS
import shadow_run_repository as SRR
import shadow_runtime as SH
import simulation_runtime_context as SRC
import tradability_archive as TA

#: Business conclusions a comparison report must never carry. Namespaced raw
#: owner output such as ``selection_scores.rank_score`` is deliberately not on
#: this list: it is the caller's own persisted score, not a comparison verdict.
FORBIDDEN_REPORT_KEYS = frozenset({
    "winner", "loser", "losers", "promote", "promotion", "promoted",
    "overall_score", "score", "ranking", "rank", "better", "best",
    "better_strategy", "recommendation", "verdict", "conclusion",
})

_ORDER_DDL = """
    CREATE TABLE paper_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
        signal_id INTEGER, cycle_id INTEGER, strategy_id TEXT,
        strategy_version INTEGER, strategy_checksum TEXT, code TEXT NOT NULL,
        side TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, order_type TEXT,
        qty INTEGER NOT NULL, planned_price REAL, filled_qty INTEGER NOT NULL DEFAULT 0,
        filled_price REAL, amount REAL, fees REAL, realized_pnl REAL,
        created_at TEXT NOT NULL, execution_evidence TEXT NOT NULL DEFAULT '{}'
    )
"""

_SIGNAL_DDL = """
    CREATE TABLE paper_signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL, reason TEXT,
        signal_date TEXT NOT NULL, intended_date TEXT NOT NULL, rank_score REAL,
        t_tier TEXT, t_score REAL, close_price REAL, payload TEXT NOT NULL
    )
"""


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


def _keys(value, into=None):
    """Collect every mapping key in a report projection, at any depth."""
    into = set() if into is None else into
    if isinstance(value, dict):
        for key, item in value.items():
            into.add(str(key))
            _keys(item, into)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _keys(item, into)
    return into


class ShadowComparisonTests(unittest.TestCase):
    def setUp(self):
        self.day = "2026-09-08"
        self.decision_at = "2026-09-08T10:05:00+08:00"
        self.code = "600000"
        self.other_code = "600001"
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
        self.challenger = SH.StrategyStamp("challenger_fixture", 2, _sha("challenger-v2"))
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
        # The Active capture: the very context the Shadow run declares as its
        # comparison counterpart, so the shared environment is EQUAL by design.
        self.active_context = SRC.build_comparable_runtime_context(
            strategy_id=self.active.strategy_id, strategy_version=self.active.version,
            strategy_checksum=self.active.checksum, session_date=self.day,
            decision_at=self.decision_at,
            market_policy_name=MDC.EXECUTION_QUOTE_POLICY.name,
            market_snapshot_fingerprint=self.identity.market_snapshot_fingerprint,
            symbol_quote_fingerprints=dict(self.identity.symbol_quote_fingerprints),
            tradability_evidence_fingerprints=dict(
                self.identity.tradability_evidence_fingerprints),
            execution_ruleset_version=EP.SIMULATION_EXECUTION_RULESET,
            risk_policy_identity={"active_capture": "fixture"},
            execution_state_fingerprint=_sha("active-execution-state"),
        )
        self.environment = SH.FrozenShadowEnvironment(
            identity=self.identity, active_runtime_context=self.active_context,
            market_reading=self.reading, quotes={self.code: self.quote},
            tradability={self.code: self.tradability},
            factor_snapshots={self.code: self.factor},
        )
        self.shadow_spec = SH.ShadowRunSpec(
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
        self.execution_policy = EP.execution_policy_snapshot(self.challenger.strategy_id)
        self.candidate = SH.ShadowCandidate(
            symbol=self.code, side="buy", desired_quantity=100,
            entry_state=EP.EntryGateState(
                capacity_available=True, position_limit=5, pool_limit=5,
                shared_cash=100_000.0, require_market_gate=False,
            ),
            risk_policy_identity={"captured_policy_fingerprint": _sha("risk-policy")},
            reference_price=10.0,
        )
        self.shadow_run = self._shadow_run((self.candidate,))

    def _shadow_run(self, candidates):
        with mock.patch.object(PT, "_execution_quote_status", return_value={
                "fresh": True, "status": "cross_source_checked"}):
            return SH.evaluate_shadow(
                spec=self.shadow_spec, environment=self.environment,
                strategy_version=self.strategy_version, lifecycle_state="shadow",
                execution_policy=self.execution_policy, candidates=tuple(candidates),
            )

    def _active_runtime_context(self, **overrides):
        projection = self.active_context.projection()
        projection.update(overrides)
        return projection

    def _execution_evidence(self, *, runtime_context=None, **extra):
        if runtime_context is None:
            context = self._active_runtime_context()
            availability, reason = "AVAILABLE", None
        elif runtime_context is False:
            context, availability, reason = None, "UNAVAILABLE", "missing_entry_state"
        else:
            context = runtime_context
            availability, reason = "AVAILABLE", None
        evidence = {
            "runtime_context": context,
            "runtime_context_availability": availability,
            "runtime_context_unavailability_reason": reason,
            "fill_quantity": 100, "remaining_quantity": 0, "status": "filled",
            "reasons": [], "ruleset_version": EP.SIMULATION_EXECUTION_RULESET,
        }
        evidence.update(extra)
        return json.dumps(evidence, ensure_ascii=False)

    def _signal_row(self, conn, *, decision="approved", reason="owner-fixture"):
        payload = {
            "signal_decision": {"outcome": decision, "status": "pending", "reason": reason},
            "signal_evidence": {"verification": "cross_source",
                                "verification_method": "cross_source",
                                "asof_day": self.day, "policy": None,
                                "detail": {"quote_validation": "cross_source_checked"}},
        }
        cursor = conn.execute(
            "INSERT INTO paper_signals(status,reason,signal_date,intended_date,rank_score,"
            "t_tier,t_score,close_price,payload) VALUES(?,?,?,?,?,?,?,?,?)",
            ("pending", reason, self.day, self.day, 88.5, "T1", 0.9, 10.0,
             json.dumps(payload, ensure_ascii=False)),
        )
        return int(cursor.lastrowid)

    def _ledger(self, *, orders=(), signals=True, with_reports=True,
                execution_evidence=None):
        """Minimal faithful ledger: the exact columns the service reads."""
        conn = sqlite3.connect(":memory:")
        conn.execute(_ORDER_DDL)
        conn.execute(_SIGNAL_DDL)
        if with_reports:
            migration = next(item for item in db_migrate.MIGRATIONS["paper_trading"]
                             if item[0] == 25)
            db_migrate._run_operation(conn, migration[2])
        order_ids = []
        for overrides in (orders or ({},)):
            row = {
                "account_id": self.active.strategy_id,
                "strategy_id": self.active.strategy_id,
                "strategy_version": self.active.version,
                "strategy_checksum": self.active.checksum,
                "code": self.code,
                "side": "buy",
                "status": "filled",
                "reason": None,
                "qty": 100,
                "planned_price": 10.0,
                "filled_qty": 100,
                "filled_price": 10.0,
                "amount": 1000.0,
                "fees": 1.0,
                "realized_pnl": None,
                "created_at": "2026-09-08 10:05:00",
                "order_type": "market",
                "cycle_id": 7,
            }
            row.update({key: value for key, value in overrides.items()
                        if key not in {"execution_evidence", "signal_decision",
                                       "link_signal"}})
            evidence = overrides.get("execution_evidence")
            if evidence is None:
                evidence = (execution_evidence if execution_evidence is not None
                            else self._execution_evidence())
            signal_id = None
            if signals and overrides.get("link_signal", True):
                signal_id = self._signal_row(
                    conn, decision=overrides.get("signal_decision", "approved"))
            cursor = conn.execute(
                "INSERT INTO paper_orders(account_id,signal_id,cycle_id,strategy_id,"
                "strategy_version,strategy_checksum,code,side,status,reason,order_type,"
                "qty,planned_price,filled_qty,filled_price,amount,fees,realized_pnl,"
                "created_at,execution_evidence)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["account_id"], signal_id, row["cycle_id"], row["strategy_id"],
                 row["strategy_version"], row["strategy_checksum"], row["code"],
                 row["side"], row["status"], row["reason"], row["order_type"],
                 row["qty"], row["planned_price"], row["filled_qty"],
                 row["filled_price"], row["amount"], row["fees"],
                 row["realized_pnl"], row["created_at"], evidence),
            )
            order_ids.append(int(cursor.lastrowid))
        conn.commit()
        return conn, order_ids

    def _comparison_spec(self, *, order_ids, expected, **overrides):
        values = {
            "challenger": self.challenger,
            "active_comparator": self.active,
            "shadow_run_id": self.shadow_run.run_id,
            "active_order_ids": tuple(order_ids),
            "environment_fingerprint": self.identity.environment_fingerprint,
            "session_date": self.day,
            "decision_at": self.decision_at,
            "expected_observations": tuple(expected),
        }
        values.update(overrides)
        return SC.ComparisonSpec(**values)

    def _report(self, *, orders=(), execution_evidence=None, expected=None,
                shadow_run=None, signals=True, spec_overrides=None):
        conn, order_ids = self._ledger(orders=orders, signals=signals,
                                      execution_evidence=execution_evidence)
        run = shadow_run if shadow_run is not None else self.shadow_run
        keys = tuple(expected) if expected is not None else (
            SC.ObservationKey(self.code, "buy"),)
        overrides = {"shadow_run_id": run.run_id,
                     "environment_fingerprint": run.spec["environment_fingerprint"]}
        overrides.update(spec_overrides or {})
        spec = self._comparison_spec(order_ids=order_ids, expected=keys, **overrides)
        evidence = SCS.load_active_comparison_evidence(conn, spec)
        report = SC.build_shadow_comparison(
            spec=spec, active_evidence=evidence, shadow_run=run)
        return conn, spec, evidence, report

    # ── fixture for a second, two-symbol environment ─────────────────────────

    def _environment_for(self, *, decision_at, symbols):
        """One frozen Active-captured environment over explicit symbols/prices."""
        quotes, factors, tradability = {}, {}, {}
        for code, price in sorted(symbols.items()):
            quotes[code] = dict(self.quote, code=code, price=price,
                                quote_at=decision_at, execution_asof=decision_at)
            factors[code] = {"close": [price], "volume": [1000.0]}
            tradability[code] = _tradability(code, self.day, decision_at)
        reading = EP.market_reading_for_execution(
            quotes[sorted(symbols)[0]], asof_day=self.day, execution_asof=decision_at)
        quote_fingerprints = {
            code: MDC.snapshot_fingerprint(
                MDC.symbol_quote_snapshot(quote, asof_day=self.day))
            for code, quote in quotes.items()}
        factor_fingerprints = {code: SH.fingerprint(item)
                               for code, item in factors.items()}
        tradability_fingerprints = {f"{code}@{self.day}": _sha("tradability")
                                    for code in quotes}
        identity = SH.ComparableEnvironmentIdentity.build(
            session_date=self.day, decision_at=decision_at,
            market_policy_name=MDC.EXECUTION_QUOTE_POLICY.name,
            market_snapshot_fingerprint=MDC.snapshot_fingerprint(reading.snapshot),
            symbol_quote_fingerprints=quote_fingerprints,
            symbol_factor_fingerprints=factor_fingerprints,
            tradability_evidence_fingerprints=tradability_fingerprints,
            execution_ruleset_identity=EP.SIMULATION_EXECUTION_RULESET,
        )
        context = SRC.build_comparable_runtime_context(
            strategy_id=self.active.strategy_id, strategy_version=self.active.version,
            strategy_checksum=self.active.checksum, session_date=self.day,
            decision_at=decision_at,
            market_policy_name=MDC.EXECUTION_QUOTE_POLICY.name,
            market_snapshot_fingerprint=identity.market_snapshot_fingerprint,
            symbol_quote_fingerprints=quote_fingerprints,
            tradability_evidence_fingerprints=tradability_fingerprints,
            execution_ruleset_version=EP.SIMULATION_EXECUTION_RULESET,
            risk_policy_identity={"active_capture": "fixture"},
            execution_state_fingerprint=_sha("active-execution-state"),
        )
        environment = SH.FrozenShadowEnvironment(
            identity=identity, active_runtime_context=context, market_reading=reading,
            quotes=quotes, tradability=tradability, factor_snapshots=factors,
        )
        return environment, identity, context

    def _two_symbol_run(self):
        """A real run over two symbols, used for continuation and coverage."""
        environment, identity, context = self._environment_for(
            decision_at=self.decision_at,
            symbols={self.code: 10.0, self.other_code: 11.0})
        spec = replace(self.shadow_spec,
                       environment_fingerprint=identity.environment_fingerprint)
        candidates = (self.candidate, replace(self.candidate, symbol=self.other_code))
        with mock.patch.object(PT, "_execution_quote_status", return_value={
                "fresh": True, "status": "cross_source_checked"}):
            run = SH.evaluate_shadow(
                spec=spec, environment=environment,
                strategy_version=self.strategy_version, lifecycle_state="shadow",
                execution_policy=self.execution_policy, candidates=candidates,
            )
        return run, identity, context

    # ── D1–D12 ───────────────────────────────────────────────────────────────

    def test_d1_same_exact_inputs_produce_the_same_report(self):
        conn, spec, evidence, first = self._report()
        second_conn, _, _, second = self._report()
        second_conn.close()
        self.assertEqual("AVAILABLE", first.availability)
        self.assertEqual(first.report_id, first.report_fingerprint)
        self.assertEqual(first.report_fingerprint, second.report_fingerprint)
        self.assertEqual(first.projection(), second.projection())
        # A different building order of the same declared scope is the same input.
        reversed_spec = self._comparison_spec(
            order_ids=spec.active_order_ids, expected=spec.expected_observations)
        self.assertEqual(spec.scope_identity(), reversed_spec.scope_identity())
        conn.close()

    def test_d1b_report_fingerprint_binds_the_exact_evidence_identities(self):
        conn, spec, evidence, report = self._report()
        self.assertEqual(evidence.source_fingerprint,
                         report.active_evidence_identity["source_fingerprint"])
        self.assertEqual(spec.shadow_run_id, report.shadow_run_id)
        self.assertEqual(self.shadow_run.run_fingerprint,
                         report.shadow_run_fingerprint)
        # Different Active evidence content -> different report identity.
        changed_evidence = SC.ActiveComparisonEvidence.build(
            (replace(evidence.orders[0], amount=999.0),))
        same_spec = self._comparison_spec(
            order_ids=spec.active_order_ids, expected=spec.expected_observations)
        changed_report = SC.build_shadow_comparison(
            spec=same_spec, active_evidence=changed_evidence,
            shadow_run=self.shadow_run)
        self.assertNotEqual(evidence.source_fingerprint,
                            changed_evidence.source_fingerprint)
        self.assertNotEqual(report.report_fingerprint,
                            changed_report.report_fingerprint)
        self.assertEqual(changed_evidence.source_fingerprint,
                         changed_report.active_evidence_identity["source_fingerprint"])
        # Different ShadowRun identity -> different report identity.
        other_run = replace(self.shadow_run, run_id=_sha("other-run"),
                            run_fingerprint=_sha("other-run"))
        run_spec = self._comparison_spec(
            order_ids=spec.active_order_ids, expected=spec.expected_observations,
            shadow_run_id=other_run.run_id)
        run_report = SC.build_shadow_comparison(
            spec=run_spec, active_evidence=evidence, shadow_run=other_run)
        self.assertNotEqual(report.report_fingerprint, run_report.report_fingerprint)
        self.assertEqual(other_run.run_id, run_report.shadow_run_id)
        self.assertEqual(other_run.run_fingerprint, run_report.shadow_run_fingerprint)
        conn.close()

    def test_d7b_a_missing_named_active_evidence_row_fails_closed(self):
        conn, order_ids = self._ledger()
        expected = (SC.ObservationKey(self.code, "buy"),)
        spec = self._comparison_spec(order_ids=(order_ids[0] + 1,), expected=expected)
        with self.assertRaisesRegex(SC.ShadowComparisonError,
                                    "active_order_evidence_unavailable"):
            SCS.load_active_comparison_evidence(conn, spec)
        # No "latest order" substitute may be found instead.
        migration = next(item for item in db_migrate.MIGRATIONS["paper_trading"]
                         if item[0] == 24)
        db_migrate._run_operation(conn, migration[2])
        SRR.append_run(conn, self.shadow_run)
        with self.assertRaisesRegex(SC.ShadowComparisonError,
                                    "active_order_evidence_unavailable"):
            SCS.build_and_append_comparison(conn, spec=spec)
        conn.close()

    def test_d2_each_shared_environment_dimension_mismatch_blocks_comparison(self):
        changes = {
            "market_snapshot_fingerprint": _sha("other-market"),
            "symbol_quote_fingerprints": {self.code: _sha("other-quote")},
            "tradability_evidence_fingerprints": {
                f"{self.code}@{self.day}": _sha("other-tradability")},
            "execution_ruleset_version": "simulation-execution-ruleset-v0",
            "decision_at": "2026-09-08T10:06:00+08:00",
        }
        for dimension, value in changes.items():
            with self.subTest(dimension=dimension):
                conn, spec, evidence, report = self._report(
                    execution_evidence=self._execution_evidence(
                        runtime_context=self._active_runtime_context(
                            **{dimension: value})))
                self.assertEqual("UNAVAILABLE", report.availability)
                self.assertNotEqual(
                    "EQUAL", report.environment_identity["shared_environment_equality"])
                self.assertTrue(report.blocking_reasons)
                if dimension != "decision_at":
                    self.assertIn("environment_mismatch", report.blocking_reasons)
                # No delta is computed, and nothing counts as available.
                for section in ("signal_delta", "decision_delta", "execution",
                                "risk_rejection", "turnover", "performance"):
                    self.assertEqual("UNAVAILABLE",
                                     getattr(report, section)["availability"], section)
                for observation in report.observations:
                    self.assertEqual("NOT_COMPARABLE", observation["alignment"])
                    for item in observation["dimensions"].values():
                        self.assertIsNone(item["delta"])
                self.assertEqual(0.0, report.coverage["coverage_ratio"])
                self.assertEqual(1, report.coverage["unavailable_observations"])
                conn.close()

    def test_d3_missing_active_runtime_context_is_not_rebuilt(self):
        for label, evidence in (
            ("absent_evidence", self._execution_evidence(runtime_context=False)),
            ("never_executed", "{}"),
        ):
            with self.subTest(case=label):
                conn, spec, active, report = self._report(execution_evidence=evidence)
                self.assertEqual("UNAVAILABLE", report.availability)
                self.assertTrue(any(item.startswith("active_runtime_context_unavailable:")
                                    for item in report.blocking_reasons),
                                report.blocking_reasons)
                self.assertIsNone(
                    report.environment_identity["shared_environment_fingerprints"]["active"])
                self.assertEqual("UNAVAILABLE",
                                 report.environment_identity["shared_environment_equality"])
                self.assertEqual(0.0, report.coverage["coverage_ratio"])
                self.assertIsNotNone(active.orders[0].runtime_context_unavailability_reason)
                conn.close()

    def test_d4_absence_is_missing_not_false_reject_or_zero(self):
        expected = (SC.ObservationKey(self.code, "buy"),
                    SC.ObservationKey(self.other_code, "buy"),
                    SC.ObservationKey(self.code, "sell"))
        conn, spec, evidence, report = self._report(
            orders=({"code": self.code}, {"code": self.other_code}),
            expected=expected)
        by_key = {(item["observation"]["symbol"], item["observation"]["side"]): item
                  for item in report.observations}
        aligned = by_key[(self.code, "buy")]
        self.assertEqual("ALIGNED", aligned["alignment"])
        active_only = by_key[(self.other_code, "buy")]
        self.assertEqual("ACTIVE_EVIDENCE_ONLY", active_only["alignment"])
        self.assertTrue(active_only["active_evidence_present"])
        self.assertFalse(active_only["challenger_evidence_present"])
        unobserved = by_key[(self.code, "sell")]
        self.assertEqual("NO_EVIDENCE", unobserved["alignment"])
        for item in (active_only, unobserved):
            for name, dimension in item["dimensions"].items():
                if dimension["missing"]["challenger"]:
                    self.assertIn(name, ("signal", "decision", "execution",
                                         "risk_rejection"))
                    self.assertIsNone(dimension["challenger"], name)
                    self.assertIsNone(dimension["delta"], name)
                    self.assertTrue(dimension["blocking_reasons"], name)
        # Absence is never counted as a blocked or zero decision.
        self.assertEqual(0, report.decision_delta["challenger_entry_blocked"])
        self.assertEqual(1, report.decision_delta["challenger_entry_allowed"])
        self.assertEqual({"expected_observations": 3, "available_observations": 1,
                          "partial_observations": 1, "missing_observations": 1,
                          "unavailable_observations": 0},
                         {key: report.coverage[key] for key in (
                             "expected_observations", "available_observations",
                             "partial_observations", "missing_observations",
                             "unavailable_observations")})
        self.assertEqual(round(1 / 3, 6), report.coverage["coverage_ratio"])
        self.assertEqual("PARTIAL", report.availability)
        conn.close()

    def test_d5_coverage_ratio_keeps_expected_as_the_denominator(self):
        run, identity, context = self._two_symbol_run()
        expected = (SC.ObservationKey(self.code, "buy"),
                    SC.ObservationKey(self.other_code, "buy"),
                    SC.ObservationKey(self.code, "sell"),
                    SC.ObservationKey(self.other_code, "sell"))
        conn, spec, evidence, report = self._report(
            orders=({"code": self.code}, {"code": self.other_code}),
            expected=expected, shadow_run=run,
            execution_evidence=self._execution_evidence(
                runtime_context=context.projection()))
        self.assertEqual(4, report.coverage["expected_observations"])
        self.assertEqual(2, report.coverage["available_observations"])
        self.assertEqual(2, report.coverage["missing_observations"])
        self.assertEqual(0.5, report.coverage["coverage_ratio"])
        self.assertEqual("PARTIAL", report.availability)
        conn.close()

    def test_d6_replay_from_exact_evidence_ignores_current_state(self):
        conn, spec, evidence, report = self._report()
        SCR.append_report(conn, report)
        migration = next(item for item in db_migrate.MIGRATIONS["paper_trading"]
                         if item[0] == 24)
        db_migrate._run_operation(conn, migration[2])
        SRR.append_run(conn, self.shadow_run)
        original_cache = EP._POLICY_CACHE
        try:
            EP._POLICY_CACHE = {**(EP._POLICY_CACHE or {}),
                                self.challenger.strategy_id: replace(
                                    EP.policy_for("tq_breakout"),
                                    manual_entry_review=True,
                                    red_light_reason="changed after the report")}
            with mock.patch.object(EP, "policy_for",
                                   side_effect=AssertionError("current policy read")), \
                    mock.patch.object(PT, "_execution_quote_status",
                                      side_effect=AssertionError("current quote read")), \
                    mock.patch.object(PT, "DB_PATH", "definitely-not-the-ledger"):
                reloaded = SRR.get_run(conn, spec.shadow_run_id)
                active_again = SCS.load_active_comparison_evidence(conn, spec)
                replayed = SC.build_shadow_comparison(
                    spec=spec, active_evidence=active_again, shadow_run=reloaded)
        finally:
            EP._POLICY_CACHE = original_cache
        self.assertEqual(report.report_fingerprint, replayed.report_fingerprint)
        self.assertEqual(report.projection(), replayed.projection())
        stored = SCR.get_report(conn, report.report_id)
        self.assertEqual(report.projection(), stored.projection())
        conn.close()

    def test_d7_pure_comparison_has_no_database_provider_clock_or_latest_lookup(self):
        tree = ast.parse(Path(SC.__file__).read_text(encoding="utf-8"))
        forbidden = {"sqlite3", "marketdata_providers", "marketdata_cache",
                     "tradability_archive", "paper_trading", "shadow_comparison_repository"}
        imports = {alias.name.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module.split(".")[0] for node in ast.walk(tree)
                       if isinstance(node, ast.ImportFrom) and node.module)
        self.assertFalse(forbidden & imports)
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse({"now", "utcnow", "today", "time", "fromtimestamp"} & attributes)
        names = [node.name for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertEqual([], [name for name in names
                              if re.search(r"latest|current|head|recent|find_", name)])
        conn, spec, evidence, _ = self._report()
        with mock.patch("sqlite3.connect",
                        side_effect=AssertionError("pure comparison opened a database")):
            report = SC.build_shadow_comparison(
                spec=spec, active_evidence=evidence, shadow_run=self.shadow_run)
        self.assertEqual("AVAILABLE", report.availability)
        conn.close()

    def test_d8_declared_risk_identity_is_never_promoted_to_verified(self):
        conn, spec, evidence, report = self._report()
        self.assertEqual("DECLARED",
                         report.provenance["challenger.risk_policy_identity"])
        self.assertEqual("OWNER_ISSUED", report.provenance["challenger.entry_policy"])
        self.assertEqual("OWNER_ISSUED", report.provenance["challenger.entry"])
        self.assertEqual("UNAVAILABLE", report.provenance["active.entry_admission"])
        self.assertEqual("DECLARED",
                         report.risk_rejection["provenance"]["challenger_risk_policy_identity"])
        risk = report.observations[0]["dimensions"]["risk_rejection"]
        self.assertEqual("DECLARED", risk["challenger"]["risk_policy_identity_provenance"])
        self.assertEqual("OWNER_ISSUED", risk["challenger"]["entry_provenance"])
        self.assertEqual({"captured_policy_fingerprint": _sha("risk-policy")},
                         risk["challenger"]["risk_policy_identity"])
        rendered = json.dumps(report.projection(), ensure_ascii=False)
        self.assertNotIn("OWNER_VERIFIED", rendered)
        self.assertNotIn("owner_verified", rendered)
        conn.close()

    def test_d9_missing_exact_valuation_stays_unavailable(self):
        # A later same-session continuation whose held positions include a symbol
        # this environment has no frozen price for: no current quote may be used.
        first, _, _ = self._two_symbol_run()
        later = "2026-09-08T10:06:00+08:00"
        environment, identity, context = self._environment_for(
            decision_at=later, symbols={self.code: 10.0})
        second_spec = replace(
            self.shadow_spec, decision_at=later,
            environment_fingerprint=identity.environment_fingerprint,
            previous_shadow_run_id=first.run_id)
        with mock.patch.object(PT, "_execution_quote_status", return_value={
                "fresh": True, "status": "cross_source_checked"}):
            second = SH.evaluate_shadow(
                spec=second_spec, environment=environment,
                strategy_version=self.strategy_version, lifecycle_state="shadow",
                execution_policy=self.execution_policy,
                candidates=(self.candidate,), previous_run=first,
            )
        self.assertEqual(100, second.after_state["positions"].get(self.other_code))
        conn, spec, evidence, report = self._report(
            shadow_run=second, spec_overrides={"decision_at": later},
            execution_evidence=self._execution_evidence(
                runtime_context=context.projection()))
        performance = report.performance
        self.assertEqual("UNAVAILABLE", performance["availability"])
        self.assertIn("challenger_position_valuation_absent",
                      performance["blocking_reasons"])
        self.assertEqual([self.other_code], performance["challenger_valuation_missing_symbols"])
        self.assertIsNone(performance["challenger"])
        self.assertEqual("UNAVAILABLE", performance["active_availability"])
        # Best-effort dimensions never block the report's own availability.
        self.assertEqual("AVAILABLE", report.availability)
        conn.close()

    def test_d9b_performance_uses_only_the_frozen_environment_quotes(self):
        conn, spec, evidence, report = self._report()
        challenger = report.performance["challenger"]
        after = self.shadow_run.after_state
        expected_nav = round(float(after["reference_cash"])
                             + sum(quantity * 10.0
                                   for quantity in after["positions"].values()), 4)
        self.assertEqual("frozen_environment_quotes", challenger["valuation_source"])
        self.assertEqual({self.code: 10.0}, challenger["valuation_prices"])
        self.assertEqual(100_000.0, challenger["reference_capital"])
        self.assertEqual(expected_nav, challenger["reference_nav"])
        self.assertEqual(round(expected_nav - 100_000.0, 4), challenger["pnl"])
        self.assertEqual(round(challenger["pnl"] / 100_000.0, 6),
                         challenger["return_ratio"])
        self.assertEqual("PARTIAL", report.performance["availability"])
        self.assertIsNone(challenger["drawdown"])
        conn.close()

    def test_d10_report_append_is_idempotent_and_append_only(self):
        conn, spec, evidence, report = self._report()
        SCR.append_report(conn, report)
        SCR.append_report(conn, report)
        self.assertEqual(1, conn.execute(
            "SELECT COUNT(*) FROM shadow_comparison_reports").fetchone()[0])
        stored = SCR.get_report(conn, report.report_id)
        self.assertEqual(report.projection(), stored.projection())
        with self.assertRaisesRegex(SCR.ShadowComparisonRepositoryError,
                                    "comparison_report_fingerprint_mismatch"):
            SCR.append_report(conn, replace(report, availability="UNAVAILABLE"))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            conn.execute("UPDATE shadow_comparison_reports SET availability='PARTIAL'"
                         " WHERE report_id=?", (report.report_id,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            conn.execute("DELETE FROM shadow_comparison_reports WHERE report_id=?",
                         (report.report_id,))
        self.assertIsNone(SCR.get_report(conn, _sha("missing-report")))
        conn.close()

    def test_d11_comparison_writes_no_formal_ledger_and_not_the_shadow_run(self):
        conn, spec, evidence, report = self._report()
        for migration_id in (24, 25):
            migration = next(item for item in db_migrate.MIGRATIONS["paper_trading"]
                             if item[0] == migration_id)
            db_migrate._run_operation(conn, migration[2])
        SRR.append_run(conn, self.shadow_run)
        tables = ("paper_orders", "paper_signals", "shadow_runs")
        before = {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                  for name in tables}
        SCS.build_and_append_comparison(conn, spec=spec)
        after = {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                 for name in tables}
        self.assertEqual(before, after)
        self.assertEqual(1, conn.execute(
            "SELECT COUNT(*) FROM shadow_comparison_reports").fetchone()[0])
        self.assertEqual(SRR.get_run(conn, spec.shadow_run_id).run_fingerprint,
                         self.shadow_run.run_fingerprint)
        conn.close()

    def test_d12_report_carries_no_winner_promotion_or_strategy_score(self):
        conn, spec, evidence, report = self._report()
        projected = report.projection()
        self.assertEqual(set(), _keys(projected) & FORBIDDEN_REPORT_KEYS)
        rendered = json.dumps(projected, ensure_ascii=False).lower()
        for token in ("winner", "loser", "promote", "promotion", "better_strategy",
                      "overall_score", "ranking", "verdict"):
            self.assertNotIn(token, rendered)
        self.assertEqual(SC.COMPARISON_SCHEMA_VERSION, report.schema_version)
        conn.close()

    # ── persistence contract ─────────────────────────────────────────────────

    def test_v25_comparison_schema_has_one_ddl_owner_and_is_idempotent(self):
        migration = next(item for item in db_migrate.MIGRATIONS["paper_trading"]
                         if item[0] == 25)
        self.assertIs(migration[2], PSM.ensure_shadow_comparison_reports)
        conn = sqlite3.connect(":memory:")
        try:
            self.assertEqual(PSM.ensure_shadow_comparison_reports(conn),
                             {"shadow_comparison_reports": "created"})
            self.assertEqual(PSM.ensure_shadow_comparison_reports(conn),
                             {"shadow_comparison_reports": "ok"})
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
            self.assertIn("shadow_comparison_reports", names)
            self.assertIn("shadow_comparison_reports_no_update", names)
            self.assertIn("shadow_comparison_reports_no_delete", names)
        finally:
            conn.close()

    def test_existing_ledger_init_db_creates_shadow_comparison_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "paper.sqlite3")
            calls = []
            real = PSM.ensure_shadow_comparison_reports
            with mock.patch.object(PT, "DB_PATH", path), \
                    mock.patch.object(PT, "_benchmark_close", return_value=None), \
                    mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True), \
                    mock.patch.object(PSM, "ensure_shadow_comparison_reports",
                                      side_effect=lambda conn: (calls.append(conn),
                                                                real(conn))[1]):
                PT.init_db()
                conn = sqlite3.connect(path)
                conn.execute("DROP TRIGGER shadow_comparison_reports_no_update")
                conn.execute("DROP TRIGGER shadow_comparison_reports_no_delete")
                conn.execute("DROP TABLE shadow_comparison_reports")
                conn.commit()
                conn.close()
                calls.clear()
                PT.init_db()
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(len(calls), 1)
                names = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
                self.assertIn("shadow_comparison_reports", names)
                self.assertIn("shadow_comparison_reports_no_update", names)
                self.assertIn("shadow_comparison_reports_no_delete", names)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

