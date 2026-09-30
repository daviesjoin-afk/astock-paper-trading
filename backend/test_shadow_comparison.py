from __future__ import annotations

import ast
import hashlib
import inspect
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
        created_at TEXT NOT NULL, execution_evidence TEXT NOT NULL DEFAULT '{}',
        risk_payload TEXT NOT NULL DEFAULT '{}'
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

    def _risk_payload(self, *, decision="approved", reason=None, with_snapshot=True):
        """The order's own risk_payload as the production writers shape it."""
        payload = {
            "chase_entry": {"allowed": True, "required": False, "reason": None},
            "three_day_timing_gate": {"allowed": True, "mode": "常规入场", "reason": None},
            "entry_price_gate": {"allowed": True, "reason": None},
        }
        if with_snapshot:
            payload["decision_snapshot"] = {
                "version": "decision-snapshot-v3",
                "final": {"score": 88.5, "reason": reason, "decision": decision},
            }
        return json.dumps(payload, ensure_ascii=False)

    def _ledger(self, *, orders=(), signals=True, with_reports=True,
                execution_evidence=None, risk_payload=None):
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
                        if key not in {"execution_evidence", "risk_payload",
                                       "signal_decision", "link_signal"}})
            evidence = overrides.get("execution_evidence")
            if evidence is None:
                evidence = (execution_evidence if execution_evidence is not None
                            else self._execution_evidence())
            row_payload = overrides.get("risk_payload")
            if row_payload is None:
                row_payload = (risk_payload if risk_payload is not None
                               else self._risk_payload())
            signal_id = None
            if signals and overrides.get("link_signal", True):
                signal_id = self._signal_row(
                    conn, decision=overrides.get("signal_decision", "approved"))
            cursor = conn.execute(
                "INSERT INTO paper_orders(account_id,signal_id,cycle_id,strategy_id,"
                "strategy_version,strategy_checksum,code,side,status,reason,order_type,"
                "qty,planned_price,filled_qty,filled_price,amount,fees,realized_pnl,"
                "created_at,execution_evidence,risk_payload)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["account_id"], signal_id, row["cycle_id"], row["strategy_id"],
                 row["strategy_version"], row["strategy_checksum"], row["code"],
                 row["side"], row["status"], row["reason"], row["order_type"],
                 row["qty"], row["planned_price"], row["filled_qty"],
                 row["filled_price"], row["amount"], row["fees"],
                 row["realized_pnl"], row["created_at"], evidence, row_payload),
            )
            order_ids.append(int(cursor.lastrowid))
        conn.commit()
        return conn, order_ids

    def _comparison_spec(self, *, order_ids, expected, active_evidence_id, **overrides):
        values = {
            "challenger": self.challenger,
            "active_comparator": self.active,
            "shadow_run_id": self.shadow_run.run_id,
            "active_order_ids": tuple(order_ids),
            "active_evidence_id": active_evidence_id,
            "environment_fingerprint": self.identity.environment_fingerprint,
            "session_date": self.day,
            "decision_at": self.decision_at,
            "expected_observations": tuple(expected),
        }
        values.update(overrides)
        return SC.ComparisonSpec(**values)

    def _report(self, *, orders=(), execution_evidence=None, risk_payload=None,
                expected=None, shadow_run=None, signals=True, spec_overrides=None):
        """Capture the exact evidence first, then declare its fingerprint."""
        conn, order_ids = self._ledger(orders=orders, signals=signals,
                                       execution_evidence=execution_evidence,
                                       risk_payload=risk_payload)
        run = shadow_run if shadow_run is not None else self.shadow_run
        keys = tuple(expected) if expected is not None else (
            SC.ObservationKey(self.code, "buy"),)
        evidence = SCS.capture_active_comparison_evidence(
            conn, active_order_ids=tuple(order_ids))
        overrides = {"shadow_run_id": run.run_id,
                     "environment_fingerprint": run.spec["environment_fingerprint"]}
        overrides.update(spec_overrides or {})
        spec = self._comparison_spec(
            order_ids=order_ids, expected=keys,
            active_evidence_id=evidence.source_fingerprint, **overrides)
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

    def _blocked_signal_run(self):
        """A real run whose own signal decision blocked the candidate.

        The Challenger's signal outcome is owner evidence that the later stages
        never applied, which is different from missing downstream evidence.
        """
        version = types.SimpleNamespace(
            strategy_id=self.challenger.strategy_id, version=self.challenger.version,
            checksum=self.challenger.checksum,
            definition={"dsl_ast": {
                "op": "gt", "left": {"op": "field", "name": "close"},
                "right": {"op": "const", "value": 10_000.0}}},
        )
        with mock.patch.object(PT, "_execution_quote_status", return_value={
                "fresh": True, "status": "cross_source_checked"}):
            return SH.evaluate_shadow(
                spec=self.shadow_spec, environment=self.environment,
                strategy_version=version, lifecycle_state="shadow",
                execution_policy=self.execution_policy,
                candidates=(self.candidate,))

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
        # Required dimensions are signal/decision/execution/risk_rejection; the
        # Active ledger has no order-linked risk rejection evidence, so a report
        # cannot be AVAILABLE today. It is PARTIAL, never silently complete.
        self.assertEqual("PARTIAL", first.availability)
        # The Active side has no order-linked risk evidence at all, so the
        # dimension can never be AVAILABLE; it is PARTIAL (one leg present).
        self.assertEqual("PARTIAL", first.risk_rejection["availability"])
        self.assertEqual("UNAVAILABLE",
                         first.observations[0]["dimensions"]["risk_rejection"]["legs"][
                             "active"])
        self.assertEqual(first.report_id, first.report_fingerprint)
        self.assertEqual(first.report_fingerprint, second.report_fingerprint)
        self.assertEqual(first.projection(), second.projection())
        # A different declaration order of the same scope is the same input.
        reversed_spec = self._comparison_spec(
            order_ids=spec.active_order_ids, expected=spec.expected_observations,
            active_evidence_id=spec.active_evidence_id)
        self.assertEqual(spec.scope_identity(), reversed_spec.scope_identity())
        conn.close()

    def test_d1b_report_fingerprint_binds_the_exact_evidence_identities(self):
        conn, spec, evidence, report = self._report()
        self.assertEqual(evidence.source_fingerprint,
                         report.active_evidence["source_fingerprint"])
        self.assertEqual(spec.active_evidence_id,
                         report.active_evidence["source_fingerprint"])
        self.assertEqual(spec.shadow_run_id, report.shadow_run_id)
        self.assertEqual(self.shadow_run.run_fingerprint,
                         report.shadow_run_fingerprint)
        # The persisted envelope can re-verify its own source fingerprint.
        self.assertEqual(evidence.source_fingerprint,
                         SC.SR.fingerprint({
                             "source_schema_version": report.active_evidence[
                                 "source_schema_version"],
                             "source": report.active_evidence["source_identity"]["source"],
                             "orders": report.active_evidence["orders"],
                         }))
        self.assertEqual(spec.active_order_ids,
                         tuple(report.active_evidence["source_identity"]["order_ids"]))
        # Different Active evidence content -> different report identity.
        changed_evidence = SC.ActiveComparisonEvidence.build(
            (replace(evidence.orders[0], amount=999.0),))
        same_spec = self._comparison_spec(
            order_ids=spec.active_order_ids, expected=spec.expected_observations,
            active_evidence_id=spec.active_evidence_id)
        changed_report = SC.build_shadow_comparison(
            spec=same_spec, active_evidence=changed_evidence,
            shadow_run=self.shadow_run)
        self.assertNotEqual(evidence.source_fingerprint,
                            changed_evidence.source_fingerprint)
        self.assertNotEqual(report.report_fingerprint,
                            changed_report.report_fingerprint)
        self.assertEqual(changed_evidence.source_fingerprint,
                         changed_report.active_evidence["source_fingerprint"])
        # A spec that still declares the old evidence id blocks the new content.
        self.assertEqual("UNAVAILABLE", changed_report.availability)
        self.assertIn("active_evidence_fingerprint_mismatch",
                      changed_report.blocking_reasons)
        # Different ShadowRun identity -> different report identity.
        other_run = replace(self.shadow_run, run_id=_sha("other-run"),
                            run_fingerprint=_sha("other-run"))
        run_spec = self._comparison_spec(
            order_ids=spec.active_order_ids, expected=spec.expected_observations,
            shadow_run_id=other_run.run_id, active_evidence_id=spec.active_evidence_id)
        run_report = SC.build_shadow_comparison(
            spec=run_spec, active_evidence=evidence, shadow_run=other_run)
        self.assertNotEqual(report.report_fingerprint, run_report.report_fingerprint)
        self.assertEqual(other_run.run_id, run_report.shadow_run_id)
        self.assertEqual(other_run.run_fingerprint, run_report.shadow_run_fingerprint)
        conn.close()

    def test_d7b_a_missing_named_active_evidence_row_fails_closed(self):
        conn, order_ids = self._ledger()
        expected = (SC.ObservationKey(self.code, "buy"),)
        # The declared fingerprint here is deliberately irrelevant: the capture
        # refuses a missing row before any fingerprint comparison happens.
        declared = SCS.capture_active_comparison_evidence(
            conn, active_order_ids=tuple(order_ids)).source_fingerprint
        spec = self._comparison_spec(order_ids=(order_ids[0] + 1,), expected=expected,
                                     active_evidence_id=declared)
        with self.assertRaisesRegex(SC.ShadowComparisonError,
                                    "active_order_evidence_unavailable"):
            SCS.capture_active_comparison_evidence(
                conn, active_order_ids=(order_ids[0] + 1,))
        with self.assertRaisesRegex(SC.ShadowComparisonError,
                                    "explicit_active_order_ids_required"):
            SCS.capture_active_comparison_evidence(conn, active_order_ids=())
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
        self.assertEqual("ALIGNED", by_key[(self.code, "buy")]["alignment"])
        active_only = by_key[(self.other_code, "buy")]
        self.assertEqual("ACTIVE_EVIDENCE_ONLY", active_only["alignment"])
        self.assertTrue(active_only["active_evidence_present"])
        self.assertFalse(active_only["challenger_evidence_present"])
        unobserved = by_key[(self.code, "sell")]
        self.assertEqual("NO_EVIDENCE", unobserved["alignment"])
        for item in (active_only, unobserved):
            for name, dimension in item["dimensions"].items():
                self.assertNotEqual("AVAILABLE", dimension["availability"], name)
                self.assertIsNone(dimension["delta"], name)
                self.assertTrue(dimension["blocking_reasons"], name)
                if dimension["legs"]["challenger"] in {"MISSING", "UNAVAILABLE"}:
                    self.assertIsNone(dimension["challenger"], name)
        # Absence is never counted as a blocked decision or a zero fill.
        self.assertEqual(0, report.decision_delta["challenger_admission_blocked"])
        self.assertEqual(1, report.execution["comparable_observations"])
        self.assertEqual(100, report.execution["challenger_filled_quantity"])
        self.assertEqual({"active": "PRESENT", "challenger": "MISSING"},
                         by_key[(self.other_code, "buy")]["dimensions"]["execution"]["legs"])
        coverage = report.coverage
        self.assertEqual(3, coverage["expected_observations"])
        self.assertEqual(0, coverage["available_observations"])
        self.assertEqual(2, coverage["partial_observations"])
        self.assertEqual(1, coverage["missing_observations"])
        self.assertEqual(1, coverage["aligned_incomplete_observations"])
        self.assertEqual(0.0, coverage["coverage_ratio"])
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
        coverage = report.coverage
        self.assertEqual(4, coverage["expected_observations"])
        # Two observations aligned, but required dimensions are not owner-complete
        # (no order-linked Active risk evidence), so none counts as available.
        self.assertEqual(2, coverage["aligned_incomplete_observations"])
        self.assertEqual(0, coverage["available_observations"])
        self.assertEqual(2, coverage["missing_observations"])
        self.assertEqual(0.0, coverage["coverage_ratio"])
        self.assertEqual("PARTIAL", report.availability)
        # The ratio is always available/expected, and a ratio that silently drops
        # the unavailable remainder is rejected by construction.
        unit = SC.CoverageEvidence(
            expected_observations=4, available_observations=2, partial_observations=0,
            missing_observations=2, unavailable_observations=0, coverage_ratio=0.5)
        self.assertEqual(0.5, unit.projection()["coverage_ratio"])
        with self.assertRaisesRegex(ValueError, "must equal available/expected"):
            SC.CoverageEvidence(
                expected_observations=4, available_observations=2,
                partial_observations=0, missing_observations=2,
                unavailable_observations=0, coverage_ratio=1.0)
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
                active_again = SCS.capture_active_comparison_evidence(
                    conn, active_order_ids=spec.active_order_ids)
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
        self.assertEqual("PARTIAL", report.availability)
        conn.close()

    def test_d8_declared_risk_identity_is_never_promoted_to_verified(self):
        conn, spec, evidence, report = self._report()
        self.assertEqual("DECLARED",
                         report.provenance["challenger.risk_policy_identity"])
        self.assertEqual("OWNER_ISSUED", report.provenance["challenger.entry_policy"])
        self.assertEqual("OWNER_ISSUED", report.provenance["challenger.entry"])
        self.assertEqual("OWNER_ISSUED", report.provenance["active.admission_decision"])
        self.assertEqual("UNAVAILABLE", report.provenance["active.risk_rejection"])
        self.assertEqual(
            "DECLARED",
            report.risk_rejection["provenance"]["challenger_risk_policy_identity"])
        self.assertEqual(
            "UNAVAILABLE",
            report.risk_rejection["provenance"]["active_rejection_outcome"])
        risk = report.observations[0]["dimensions"]["risk_rejection"]
        self.assertEqual("DECLARED", risk["challenger"]["risk_policy_identity_provenance"])
        self.assertEqual("OWNER_ISSUED", risk["challenger"]["entry_provenance"])
        self.assertEqual("UNAVAILABLE", risk["active"]["risk_rejection_availability"])
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
        # Best-effort dimensions never block the report's own availability, and
        # the report is PARTIAL anyway because a required dimension is incomplete.
        self.assertEqual("PARTIAL", report.availability)
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

    # ── D13–D18: exact evidence identity and evidence completeness ────────────

    def test_d13_mutated_active_row_fails_closed_against_the_pinned_fingerprint(self):
        """order id 不是 evidence identity：同一 order id 的行漂移必须 fail closed。"""
        # A comparison may not run without a pinned Active evidence fingerprint.
        for invalid in (None, "", "not-a-fingerprint"):
            with self.subTest(declared=invalid):
                with self.assertRaisesRegex(
                        ValueError, "exact Active evidence fingerprint is required"):
                    self._comparison_spec(
                        order_ids=(1,),
                        expected=(SC.ObservationKey(self.code, "buy"),),
                        active_evidence_id=invalid)
        conn, spec, evidence, _ = self._report()
        for migration_id in (24, 25):
            migration = next(item for item in db_migrate.MIGRATIONS["paper_trading"]
                             if item[0] == migration_id)
            db_migrate._run_operation(conn, migration[2])
        SRR.append_run(conn, self.shadow_run)
        cursor = conn.execute(
            "UPDATE paper_orders SET filled_qty=?,amount=?,status=?,execution_evidence=?"
            " WHERE id=?",
            (0, 0.0, "cancelled", "{}", spec.active_order_ids[0]))
        self.assertEqual(1, cursor.rowcount)
        conn.commit()
        drifted = SCS.capture_active_comparison_evidence(
            conn, active_order_ids=spec.active_order_ids)
        self.assertEqual(tuple(spec.active_order_ids),
                         tuple(drifted.source_identity["order_ids"]))
        self.assertNotEqual(evidence.source_fingerprint, drifted.source_fingerprint)
        # The same declared fingerprint no longer matches the mutable row.
        with self.assertRaisesRegex(SC.ShadowComparisonError,
                                    "active_evidence_fingerprint_mismatch"):
            SCS.build_and_append_comparison(conn, spec=spec)
        self.assertEqual(0, conn.execute(
            "SELECT COUNT(*) FROM shadow_comparison_reports").fetchone()[0])
        # Re-declaring the drifted fingerprint is a different comparison, and it
        # is an explicit act: it produces a different report identity.
        re_declared = replace(spec, active_evidence_id=drifted.source_fingerprint)
        replayed = SCS.build_and_append_comparison(conn, spec=re_declared)
        self.assertNotEqual(evidence.source_fingerprint, replayed.active_evidence[
            "source_fingerprint"])
        self.assertEqual(1, conn.execute(
            "SELECT COUNT(*) FROM shadow_comparison_reports").fetchone()[0])
        conn.close()

    def test_d14_candidate_without_execution_evidence_is_not_execution_evidence(self):
        blocked_run = self._blocked_signal_run()
        self.assertIsNone(blocked_run.decisions[0]["execution"])
        self.assertIsNone(blocked_run.decisions[0]["entry"])
        conn, spec, evidence, report = self._report(shadow_run=blocked_run)
        execution = report.observations[0]["dimensions"]["execution"]
        # The owner's own signal decision proves the stage never applied.
        self.assertEqual("NOT_APPLICABLE", execution["legs"]["challenger"])
        self.assertEqual("PRESENT", execution["legs"]["active"])
        self.assertNotEqual("AVAILABLE", execution["availability"])
        self.assertIsNone(execution["challenger"]["execution_fill_quantity"])
        self.assertIsNone(execution["delta"])
        self.assertNotEqual("AVAILABLE", report.availability)
        self.assertEqual(0, report.coverage["available_observations"])
        self.assertNotEqual(1.0, report.coverage["coverage_ratio"])
        # An entry-admitted candidate with no execution evidence is MISSING.
        tampered = replace(blocked_run, decisions=({
            **dict(blocked_run.decisions[0]),
            "signal": {"outcome": "approved", "status": "pending", "reason": "x",
                       "evidence": None},
            "entry": {"allowed": True, "reasons": [], "gates": {},
                      "policy": None, "requires_manual_entry_review": False},
        },))
        _, _, _, missing_report = self._report(shadow_run=tampered)
        missing = missing_report.observations[0]["dimensions"]["execution"]
        self.assertEqual("MISSING", missing["legs"]["challenger"])
        self.assertNotEqual("AVAILABLE", missing["availability"])
        self.assertIsNone(missing["challenger"]["execution_fill_quantity"])
        self.assertEqual(0, missing_report.coverage["available_observations"])
        conn.close()

    def test_d15_active_execution_evidence_absent_is_never_complete(self):
        # Without the Execution Authority's own envelope the Active decision has
        # neither a comparable environment nor an execution result.
        conn, spec, evidence, report = self._report(execution_evidence="{}")
        self.assertEqual("UNAVAILABLE", report.availability)
        self.assertTrue(any(item.startswith("active_runtime_context_unavailable:")
                            for item in report.blocking_reasons),
                        report.blocking_reasons)
        self.assertNotEqual(
            "AVAILABLE",
            report.observations[0]["dimensions"]["execution"]["availability"])
        self.assertEqual(0, report.coverage["available_observations"])
        conn.close()

    def test_d15b_owner_issued_zero_is_reported_as_zero_not_fabricated(self):
        """0 只能来自 owner 明确产出的证据；缺失永远是 None。"""
        conn, spec, evidence, report = self._report(
            execution_evidence=self._execution_evidence(fill_quantity=0))
        execution = report.observations[0]["dimensions"]["execution"]
        self.assertEqual("PRESENT", execution["legs"]["active"])
        self.assertTrue(execution["active"]["execution_evidence_availability"]
                        == "OWNER_ISSUED")
        self.assertEqual(0, execution["active"]["execution_fill_quantity"])
        self.assertEqual(0.0, execution["delta"]["active_fill_ratio"])
        self.assertEqual("AVAILABLE", execution["availability"])
        self.assertEqual(0, report.execution["active_filled_quantity"])
        conn.close()

    def test_d16_missing_challenger_entry_is_not_an_admission_decision(self):
        blocked_run = self._blocked_signal_run()
        conn, spec, evidence, report = self._report(shadow_run=blocked_run)
        decision = report.observations[0]["dimensions"]["decision"]
        risk = report.observations[0]["dimensions"]["risk_rejection"]
        self.assertEqual("NOT_APPLICABLE", decision["legs"]["challenger"])
        self.assertNotEqual("AVAILABLE", decision["availability"])
        self.assertIsNone(decision["challenger"]["admission_decision"])
        self.assertEqual("UNAVAILABLE",
                         decision["challenger"]["admission_provenance"])
        self.assertNotEqual("AVAILABLE", risk["availability"])
        # No entry decision means no owner-issued risk evidence either.
        self.assertEqual("UNAVAILABLE", risk["challenger"]["entry_provenance"])
        self.assertIsNone(risk["challenger"]["entry_reasons"])
        self.assertEqual("DECLARED",
                         risk["challenger"]["risk_policy_identity_provenance"])
        conn.close()

    def test_d17_order_status_is_not_risk_authority_evidence(self):
        conn, spec, evidence, report = self._report()
        risk = report.observations[0]["dimensions"]["risk_rejection"]
        self.assertEqual("UNAVAILABLE", risk["legs"]["active"])
        self.assertIsNone(risk["active"]["risk_rejection_evidence"])
        self.assertEqual("UNAVAILABLE", risk["active"]["risk_rejection_availability"])
        self.assertEqual("no_order_linked_risk_authority_evidence",
                         risk["active"]["reason"])
        self.assertFalse(risk["active"]["detail"]["order_lifecycle_status_used"])
        self.assertIsNone(risk["active"]["detail"]["paper_risk_decisions_order_linkage"])
        self.assertEqual("UNAVAILABLE", report.provenance["active.risk_rejection"])
        # The lifecycle status is reported, but in its own section and flagged as
        # not being an admission, an execution result, or risk evidence.
        lifecycle = report.active_order_lifecycle
        self.assertEqual("filled", lifecycle["orders"][0]["order_status"])
        self.assertEqual({"filled": 1}, lifecycle["status_counts"])
        self.assertFalse(lifecycle["used_as_entry_admission"])
        self.assertFalse(lifecycle["used_as_execution_result"])
        self.assertFalse(lifecycle["used_as_risk_evidence"])
        self.assertEqual("OWNER_ISSUED", lifecycle["provenance"])
        conn.close()

    def test_d18_absent_execution_never_becomes_zero_or_false(self):
        blocked_run = self._blocked_signal_run()
        conn, spec, evidence, report = self._report(shadow_run=blocked_run)
        rendered = report.projection()
        execution = rendered["observations"][0]["dimensions"]["execution"]
        self.assertIsNone(execution["challenger"]["execution_fill_quantity"])
        self.assertIsNone(execution["challenger"]["status"])
        self.assertIsNone(execution["challenger"]["reasons"])
        self.assertIsNone(execution["delta"])
        self.assertIsNone(rendered["execution"]["challenger_filled_quantity"])
        self.assertIsNone(rendered["execution"]["fill_quantity_delta"])
        self.assertFalse(execution["challenger"]["candidate_present"] is None)
        # Nothing anywhere claims a zero fill or a rejected flag for the absent leg.
        flattened = json.dumps(execution["challenger"], ensure_ascii=False)
        self.assertNotIn('"filled_quantity": 0', flattened)
        self.assertNotIn('"rejected"', flattened)
        self.assertNotIn('"fill_ratio": 0', flattened)
        conn.close()

    # ── D19–D20: owner-key mapping and the single capture owner ──────────────

    def test_d19_admission_score_comes_from_its_canonical_owner_key(self):
        """owner 原始分数经 capture → observation → 持久化 report 全程保持 88.5。"""
        conn, spec, evidence, report = self._report()
        self.assertEqual(88.5, evidence.orders[0].admission_evidence["admission_score"])
        self.assertEqual(88.5, evidence.orders[0].admission["admission_score"])
        decision = report.observations[0]["dimensions"]["decision"]
        self.assertEqual(88.5, decision["active"]["admission_score"])
        self.assertEqual("risk_payload.decision_snapshot.final",
                         decision["active"]["admission_source"])
        self.assertEqual("approved", decision["active"]["admission_decision"])
        # Round-tripping through the append-only report table keeps the value.
        SCR.append_report(conn, report)
        stored = SCR.get_report(conn, report.report_id)
        self.assertEqual(88.5, stored.observations[0]["dimensions"]["decision"][
            "active"]["admission_score"])
        self.assertEqual(
            88.5,
            stored.active_evidence["orders"][0]["admission_evidence"]["admission_score"])
        # The owner's raw score keeps its namespaced name: no bare "score" key
        # reappears anywhere in the persisted report.
        self.assertEqual(set(), _keys(stored.projection()) & {"score"})
        conn.close()

    def test_d20_active_capture_depends_only_on_explicit_order_ids(self):
        """capture 不依赖 ComparisonSpec；旧的 spec 驱动签名已删除（0 caller）。"""
        parameters = inspect.signature(
            SCS.capture_active_comparison_evidence).parameters
        self.assertEqual({"conn", "active_order_ids"}, set(parameters))
        self.assertEqual(inspect.Parameter.KEYWORD_ONLY,
                         parameters["active_order_ids"].kind)
        module_functions = [name for name, item in vars(SCS).items()
                            if inspect.isfunction(item)
                            and "active_comparison_evidence" in name]
        self.assertEqual(["capture_active_comparison_evidence"], module_functions)
        conn, order_ids = self._ledger()
        with self.assertRaisesRegex(SC.ShadowComparisonError,
                                    "explicit_active_order_ids_required"):
            SCS.capture_active_comparison_evidence(conn, active_order_ids=(1, 1))
        conn.close()

    def test_d20b_documented_caller_flow_works_end_to_end(self):
        """§二.6 的正式调用顺序：capture → declare → build_and_append。"""
        conn, order_ids = self._ledger()
        for migration_id in (24, 25):
            migration = next(item for item in db_migrate.MIGRATIONS["paper_trading"]
                             if item[0] == migration_id)
            db_migrate._run_operation(conn, migration[2])
        SRR.append_run(conn, self.shadow_run)
        evidence = SCS.capture_active_comparison_evidence(
            conn, active_order_ids=tuple(order_ids))
        spec = SC.ComparisonSpec(
            challenger=self.challenger, active_comparator=self.active,
            shadow_run_id=self.shadow_run.run_id,
            active_order_ids=tuple(order_ids),
            active_evidence_id=evidence.source_fingerprint,
            environment_fingerprint=self.identity.environment_fingerprint,
            session_date=self.day, decision_at=self.decision_at,
            expected_observations=(SC.ObservationKey(self.code, "buy"),),
        )
        report = SCS.build_and_append_comparison(conn, spec=spec)
        self.assertEqual(evidence.source_fingerprint,
                         report.active_evidence["source_fingerprint"])
        self.assertEqual(1, conn.execute(
            "SELECT COUNT(*) FROM shadow_comparison_reports").fetchone()[0])
        # Capturing again after a mutable-row change no longer matches the spec.
        conn.execute("UPDATE paper_orders SET amount=? WHERE id=?", (7.0, order_ids[0]))
        conn.commit()
        with self.assertRaisesRegex(SC.ShadowComparisonError,
                                    "active_evidence_fingerprint_mismatch"):
            SCS.build_and_append_comparison(conn, spec=spec)
        self.assertEqual(1, conn.execute(
            "SELECT COUNT(*) FROM shadow_comparison_reports").fetchone()[0])
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

