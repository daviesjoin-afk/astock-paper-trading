# -*- coding: utf-8 -*-
"""R34-A contract regressions for exact portfolio runtime facts."""
from __future__ import annotations

import json
import ast
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_trading as PT
import paper_cycle_ownership as PCY
import paper_portfolio_read_model as PPRM
import api_paper as API
import market_data_contract as MDC
import portfolio_runtime as PR
import portfolio_runtime_repository as PRRepo
import portfolio_runtime_service as PRS

CHECKSUM = "a" * 64


def _dimensions():
    values = []
    for name in PR.DIMENSIONS:
        if name == "capital":
            values.append(PR.PortfolioDimension(
                name, PR.AVAILABLE, {"capital": 100}, "OWNER_ISSUED", "ledger"))
        else:
            values.append(PR.PortfolioDimension(
                name, PR.UNAVAILABLE, {}, "UNAVAILABLE",
                blocking_reasons=(f"{name}_owner_unavailable",)))
    return tuple(values)


def _build(**overrides):
    values = {"cycle_id": 7, "asof_day": "2026-09-30",
              "decision_at": "2026-10-01T09:30:00+08:00",
              "cycle_identity": {"cycle_id": 7, "cycle_key": "c-7"},
              "strategy_pins": [{"account_id": "s1", "strategy_id": "s1",
                                 "strategy_version": 2,
                                 "strategy_checksum": CHECKSUM}],
              "economic_owner_ids": ["s1"], "execution_participant_ids": ["s1"],
              "risk_exit_participant_ids": ["s1"],
              "source_identities": {"cycle": "paper_cycles:7"},
              "market_evidence_identity": None, "dimensions": _dimensions()}
    values.update(overrides)
    return PR.build_portfolio_runtime_snapshot(**values)


class PortfolioRuntimeContractTests(unittest.TestCase):
    def _fresh_market_reading(self, rows, *, asof="2026-10-01"):
        snapshot = MDC.MarketDataSnapshot(
            kind="full_market_snapshot", rows=tuple(rows), as_of=asof,
            observed_at=f"{asof}T09:30:00+08:00", source="market-owner",
            complete=True, expected_rows=len(rows),
            verification=MDC.VERIFICATION_VERIFIED,
            verification_method=MDC.VERIFICATION_METHOD_CROSS_SOURCE,
        )
        return MDC.MarketDataReading(
            availability=MDC.AVAILABILITY_AVAILABLE,
            freshness=MDC.FRESHNESS_FRESH, status=MDC.STATUS_FRESH,
            policy_name=MDC.LIVE_MARKET_POLICY.name, snapshot=snapshot,
        )

    def test_rc1_market_valuation_uses_exact_quote_owner_not_position_cost(self):
        reading = self._fresh_market_reading([
            {"code": "AAA", "price": 12.5, "quote_at": "2026-10-01T09:30:00+08:00",
             "source": "source-a"},
            {"code": "BBB", "price": 7.0, "quote_at": "2026-10-01T09:30:00+08:00",
             "source": "source-b"},
        ])
        dimension = PRS._market_valuation_dimension(
            owners=("s1", "s2"), asof_day="2026-10-01", reading=reading,
            expected_identity=MDC.snapshot_fingerprint(reading.snapshot),
            positions=({"account_id": "s1", "code": "AAA", "qty": 10,
                        "cost": 99_999.0},
                       {"account_id": "s2", "code": "BBB", "qty": 3,
                        "cost": 99_999.0}),
        )
        self.assertEqual(dimension.status, PR.AVAILABLE)
        self.assertEqual(dimension.facts["market_value_by_account"],
                         {"s1": 125.0, "s2": 21.0})
        self.assertNotIn("cost", json.dumps(dimension.projection()["facts"]))
        self.assertIn(MDC.snapshot_fingerprint(reading.snapshot),
                      dimension.source_identity)

    def test_rc2_missing_exact_quote_is_partial_and_not_cost_filled(self):
        reading = self._fresh_market_reading([
            {"code": "AAA", "price": 12.5, "quote_at": "2026-10-01T09:30:00+08:00",
             "source": "source-a"},
        ])
        dimension = PRS._market_valuation_dimension(
            owners=("s1",), asof_day="2026-10-01", reading=reading,
            positions=({"account_id": "s1", "code": "MISSING", "qty": 2,
                        "cost": 99.0},),
        )
        self.assertEqual(dimension.status, PR.PARTIAL)
        self.assertIsNone(dimension.facts["market_value_by_account"]["s1"])
        self.assertEqual(dimension.projection()["facts"][
            "missing_quote_symbols_by_account"], {"s1": ["MISSING"]})

    def test_rc3_market_quote_identity_mismatch_fails(self):
        reading = self._fresh_market_reading([
            {"code": "AAA", "price": 12.5, "quote_at": "2026-10-01T09:30:00+08:00",
             "source": "source-a"},
        ])
        with self.assertRaisesRegex(PR.PortfolioRuntimeError,
                                    "market_evidence_identity_mismatch"):
            PRS._market_valuation_dimension(
                owners=("s1",), asof_day="2026-10-01", reading=reading,
                expected_identity="0" * 64,
                positions=({"account_id": "s1", "code": "AAA", "qty": 2},),
            )

    def test_rc8_reservation_requires_exact_pending_order_identity(self):
        reservations = {"status": "AVAILABLE", "reservations": [{
            "reservation_id": 4, "order_id": "17", "cycle_id": 8,
            "account_id": "s1", "symbol": "AAA", "side": "buy",
        }], "unknown_reservations": []}
        order = {"status": "AVAILABLE", "source_identity": "paper_orders:pending",
                 "order_identities": [{"order_id": 17, "cycle_id": 8,
                                        "account_id": "s1", "symbol": "AAA",
                                        "side": "buy"}]}
        valid = PRS._reservation_order_identity(reservations, order)
        self.assertEqual("AVAILABLE", valid["order_identity_validation"]["status"])
        mismatch = PRS._reservation_order_identity(
            reservations, {**order, "order_identities": [{
                **order["order_identities"][0], "account_id": "other"}]})
        self.assertEqual("UNAVAILABLE", mismatch["status"])
        self.assertEqual("reservation_order_identity_mismatch",
                         mismatch["unknown_reservations"][0]["reason"])

    def test_rc9_pending_symbol_projection_uses_verified_reservation_amount_and_fees(self):
        reservations = {"status": "AVAILABLE", "reservations": [
            {"symbol": "AAA", "amount": 100.0, "fees": 1.0},
            {"symbol": "AAA", "amount": 200.0, "fees": 2.0},
            {"symbol": "BBB", "amount": 50.0, "fees": 1.0},
        ]}
        self.assertEqual(
            {"AAA": 303.0, "BBB": 51.0},
            PRS._pending_reservations_by_symbol(reservations))
        unavailable = {**reservations, "status": "UNAVAILABLE"}
        self.assertIsNone(PRS._pending_reservations_by_symbol(unavailable))

    def test_pa1_same_input_has_same_fingerprint(self):
        self.assertEqual(_build().snapshot_fingerprint, _build().snapshot_fingerprint)

    def test_pa2_strategy_input_order_is_canonical(self):
        pin2 = {"account_id": "s2", "strategy_id": "s2", "strategy_version": 1,
                "strategy_checksum": "b" * 64}
        first = _build(strategy_pins=[pin2, _build().strategy_pins[0]],
                       economic_owner_ids=["s1", "s2"],
                       execution_participant_ids=["s1", "s2"],
                       risk_exit_participant_ids=["s1", "s2"])
        second = _build(strategy_pins=[_build().strategy_pins[0], pin2],
                        economic_owner_ids=["s1", "s2"],
                        execution_participant_ids=["s1", "s2"],
                        risk_exit_participant_ids=["s1", "s2"])
        self.assertEqual(first.snapshot_id, second.snapshot_id)

    def test_pa3_missing_explicit_cycle_fails(self):
        with self.assertRaisesRegex(PR.PortfolioRuntimeError, "cycle_id_required"):
            _build(cycle_id=None)
        with self.assertRaisesRegex(PR.PortfolioRuntimeError, "cycle_id_required"):
            _build(cycle_id=0)

    def test_pa4_bad_exact_pin_checksum_fails(self):
        with self.assertRaisesRegex(PR.PortfolioRuntimeError, "pin_identity_invalid"):
            _build(strategy_pins=[{"account_id": "s1", "strategy_id": "s1",
                                   "strategy_version": 2, "strategy_checksum": "bad"}])

    def test_exact_strategy_pins_must_cover_each_economic_owner(self):
        with self.assertRaisesRegex(PR.PortfolioRuntimeError, "must_cover_economic_owners"):
            _build(strategy_pins=[], economic_owner_ids=["s1"])

    def test_pa5_no_current_or_latest_lookup_exists_in_service(self):
        source = pathlib.Path(PRS.__file__).read_text(encoding="utf-8").lower()
        self.assertNotIn("latest", source)
        self.assertNotIn("current_cycle", source)

    def test_pa6_idle_cycle_has_empty_participants(self):
        item = _build(strategy_pins=[], economic_owner_ids=[],
                      execution_participant_ids=[], risk_exit_participant_ids=[])
        self.assertEqual(item.economic_owner_ids, ())
        self.assertEqual(item.execution_participant_ids, ())

    def test_pa7_lifecycle_pause_does_not_remove_economic_owner(self):
        item = _build(strategy_pins=[{"account_id": "s1", "strategy_id": "s1",
                                     "strategy_version": 2, "strategy_checksum": CHECKSUM,
                                     "lifecycle_state": "paused"}],
                      execution_participant_ids=[], risk_exit_participant_ids=["s1"])
        self.assertEqual(item.economic_owner_ids, ("s1",))
        self.assertEqual(item.execution_participant_ids, ())
        self.assertEqual(item.risk_exit_participant_ids, ("s1",))

    def test_pa8_risk_exit_scope_is_independent_and_can_retain_open_lot_owner(self):
        item = _build(execution_participant_ids=[], risk_exit_participant_ids=["legacy"])
        self.assertEqual(item.risk_exit_participant_ids, ("legacy",))

    def test_pa9_pending_capacity_failure_is_unavailable_not_zero(self):
        dimension = PR.PortfolioDimension("capacity", PR.UNAVAILABLE, {}, "UNAVAILABLE",
                                          blocking_reasons=("pending_read_failed",))
        self.assertEqual(dimension.status, PR.UNAVAILABLE)
        self.assertNotIn("pending_amount", dimension.facts)

    def test_rc10_runtime_pending_query_failure_is_not_composed_as_empty(self):
        source = pathlib.Path(PRS.__file__).read_text(encoding="utf-8")
        self.assertIn("except POI.PendingIntentEvidenceUnavailable:", source)
        self.assertIn("pending_intents = None", source)
        self.assertNotIn("pending_intents = []", source)

    def test_pa10_cost_basis_does_not_become_market_value(self):
        item = _build()
        facts = item.projection()["dimensions"]
        self.assertNotIn("market_value", json.dumps(facts))

    def test_pa11_correlation_without_owner_is_unavailable_not_zero(self):
        item = next(d for d in _build().dimensions if d.name == "correlation")
        self.assertEqual(item.status, PR.UNAVAILABLE)
        self.assertNotIn("correlation", item.facts)

    def test_pa12_no_style_owner_does_not_guess_classification(self):
        item = next(d for d in _build().dimensions if d.name == "concentration")
        self.assertEqual(item.status, PR.UNAVAILABLE)
        self.assertEqual(item.facts, {})

    def test_pa13_signal_conflict_requires_exact_evidence(self):
        item = next(d for d in _build().dimensions if d.name == "signal_conflicts")
        self.assertEqual(item.status, PR.UNAVAILABLE)

    def test_pa14_zero_activity_is_not_a_zero_risk_or_correlation_fact(self):
        item = _build()
        for name in ("risk_consumption", "correlation"):
            fact = next(d for d in item.dimensions if d.name == name)
            self.assertEqual(fact.status, PR.UNAVAILABLE)

    def test_pa15_persisted_snapshot_does_not_heal(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE portfolio_runtime_snapshots(snapshot_id TEXT PRIMARY KEY,"
                     "snapshot_fingerprint TEXT NOT NULL,evidence_json TEXT NOT NULL,"
                     "created_at TEXT NOT NULL)")
        snapshot = _build()
        PRRepo.append_snapshot(conn, snapshot, created_at="fixed")
        changed = _build(dimensions=tuple(
            PR.PortfolioDimension(d.name, PR.PARTIAL, {"later": True}, "CAPTURED_INPUT",
                                  blocking_reasons=("later_evidence",))
            if d.name == "correlation" else d for d in _dimensions()))
        PRRepo.append_snapshot(conn, changed, created_at="later")
        self.assertEqual(PRRepo.get_snapshot(conn, snapshot.snapshot_id).projection(),
                         snapshot.projection())
        self.assertNotEqual(snapshot.snapshot_id, changed.snapshot_id)
        conn.close()

    def test_pa16_capture_is_read_only_for_formal_ledger(self):
        self._with_idle_capture()

    def test_pa17_capture_does_not_write_lifecycle(self):
        self._with_idle_capture()

    def test_rc7_idle_cycle_without_economic_owners_keeps_nav_unavailable(self):
        temp = tempfile.TemporaryDirectory(prefix="r34c-market-capture-")
        old_db = PT.DB_PATH
        PT.DB_PATH = os.path.join(temp.name, "paper.sqlite3")
        try:
            PT.init_db()
            PT.start_new_cycle(capital=10_000, include_dashboard=False)
            conn = sqlite3.connect(PT.DB_PATH)
            conn.row_factory = sqlite3.Row
            cycle_id = int(conn.execute(
                "SELECT MAX(id) FROM paper_cycles").fetchone()[0])
            conn.execute(
                "UPDATE paper_cycles SET enabled_strategies='[]' WHERE id=?",
                (cycle_id,),
            )
            conn.commit()
            conn.close()
            reading = self._fresh_market_reading([], asof="2026-10-03")
            with PT._db(immediate=True) as conn:
                result = PRS.capture_portfolio_runtime_snapshot(
                    conn, cycle_id=cycle_id, asof_day="2026-10-03",
                    decision_at="2026-10-03T09:31:00+08:00",
                    builtin_scope=PT.ACTIVE_ACCOUNT_IDS,
                    market_evidence_identity=MDC.snapshot_fingerprint(reading.snapshot),
                    market_reading=reading,
                )
                conn.commit()
            dimensions = {item["name"]: item for item in result["dimensions"]}
            self.assertEqual(dimensions["strategy_exposure"]["status"], PR.AVAILABLE)
            self.assertEqual(dimensions["strategy_exposure"]["facts"][
                "market_evidence_identity"], MDC.snapshot_fingerprint(reading.snapshot))
            self.assertTrue(dimensions["capital"]["facts"][
                "nav_uses_portfolio_read_model_composer"])
            # An ownerless cycle has no bounded accounting rows from which the
            # portfolio read model can prove cash/NAV.  Empty owners are not a
            # successful zero exposure/capacity observation.
            self.assertIsNone(dimensions["capital"]["facts"]["nav"])
            self.assertEqual(dimensions["capital"]["status"], PR.PARTIAL)
            self.assertEqual(dimensions["capacity"]["status"], PR.UNAVAILABLE)
        finally:
            PT.DB_PATH = old_db
            temp.cleanup()

    def _with_idle_capture(self):
        temp = tempfile.TemporaryDirectory(prefix="r34a-")
        old_db = PT.DB_PATH
        PT.DB_PATH = os.path.join(temp.name, "paper.sqlite3")
        try:
            PT.init_db()
            PT.start_new_cycle(capital=10_000, include_dashboard=False)
            conn = sqlite3.connect(PT.DB_PATH)
            conn.row_factory = sqlite3.Row
            cycle_id = int(conn.execute("SELECT MAX(id) FROM paper_cycles").fetchone()[0])
            # Use an exact idle cycle to avoid manufacturing strategy ownership.
            conn.execute("UPDATE paper_cycles SET enabled_strategies='[]' WHERE id=?", (cycle_id,))
            conn.commit()
            before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("paper_accounts", "paper_cycles", "paper_orders",
                                    "paper_fills", "paper_position_lots",
                                    "strategy_lifecycle_events")}
            conn.close()
            with PT._db(immediate=True) as conn:
                result = PRS.capture_portfolio_runtime_snapshot(
                    conn, cycle_id=cycle_id, asof_day="2026-09-30",
                    decision_at="2026-10-01T09:30:00+08:00",
                    builtin_scope=PT.ACTIVE_ACCOUNT_IDS,
                    market_evidence_identity="f" * 64)
                retry = PRS.capture_portfolio_runtime_snapshot(
                    conn, cycle_id=cycle_id, asof_day="2026-09-30",
                    decision_at="2026-10-01T09:30:00+08:00",
                    builtin_scope=PT.ACTIVE_ACCOUNT_IDS,
                    market_evidence_identity="f" * 64)
                conn.commit()
            self.assertEqual(retry, result)
            with PT._db_readonly() as conn:
                self.assertEqual(
                    PRS.get_portfolio_runtime_snapshot(conn, result["snapshot_id"]),
                    result)
            dimensions = {item["name"]: item for item in result["dimensions"]}
            self.assertEqual(dimensions["correlation"]["status"], PR.UNAVAILABLE)
            self.assertIsNone(dimensions["strategy_exposure"]["facts"]["market_value_by_account"])
            self.assertIsNone(result["market_evidence_identity"])
            self.assertEqual(dimensions["signal_conflicts"]["status"], PR.AVAILABLE)
            self.assertEqual(dimensions["signal_conflicts"]["facts"][
                "pending_resource_intents"], [])
            self.assertEqual(dimensions["capacity"]["status"], PR.UNAVAILABLE)
            self.assertEqual(dimensions["capital"]["status"], PR.PARTIAL)
            self.assertIsNone(dimensions["capital"]["facts"]["nav"])
            self.assertTrue(dimensions["capital"]["facts"][
                "nav_uses_portfolio_read_model_composer"])
            self.assertTrue(all(value["nav"] is None for value in dimensions["capital"][
                "facts"]["portfolio_valuation_by_account"].values()))
            check = sqlite3.connect(PT.DB_PATH)
            after = {table: check.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in before}
            self.assertEqual(before, after)
            self.assertEqual(check.execute(
                "SELECT COUNT(*) FROM portfolio_runtime_snapshots").fetchone()[0], 1)
            self.assertEqual(result["cycle_id"], cycle_id)
            check.close()
        finally:
            PT.DB_PATH = old_db
            temp.cleanup()

    def test_pa18_repository_exposes_only_exact_getter(self):
        self.assertEqual(set(PRRepo.__all__),
                         {"PortfolioRuntimeRepositoryError", "append_snapshot", "get_snapshot"})

    def test_pa19_same_id_different_content_conflicts(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE portfolio_runtime_snapshots(snapshot_id TEXT PRIMARY KEY,"
                     "snapshot_fingerprint TEXT NOT NULL,evidence_json TEXT NOT NULL,"
                     "created_at TEXT NOT NULL)")
        original = _build()
        PRRepo.append_snapshot(conn, original)
        conn.execute("UPDATE portfolio_runtime_snapshots SET evidence_json='{}' WHERE snapshot_id=?",
                     (original.snapshot_id,))
        with self.assertRaisesRegex(PRRepo.PortfolioRuntimeRepositoryError,
                                    "idempotency_conflict"):
            PRRepo.append_snapshot(conn, original)
        conn.close()

    def test_pa20_snapshot_contains_no_rank_or_score(self):
        projection = json.dumps(_build().projection()).lower()
        for forbidden in ("portfolio_score", "health_score", "winner", "rank"):
            self.assertNotIn(forbidden, projection)

    def _owner_conn(self, enabled, bound, attachment_dates):
        import paper_account_specs as PAS

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY,cycle_key TEXT,"
                     "status TEXT,enabled_strategies TEXT,capital REAL)")
        conn.execute("CREATE TABLE paper_accounts(id TEXT PRIMARY KEY,cycle_id INTEGER)")
        conn.execute("CREATE TABLE paper_parameter_versions(cycle_id INTEGER,account_id TEXT,"
                     "effective_date TEXT)")
        conn.execute("INSERT INTO paper_cycles VALUES(7,'c-7','running',?,1000)",
                     (json.dumps(enabled),))
        conn.executemany("INSERT INTO paper_accounts(id,cycle_id) VALUES(?,7)",
                         [(account,) for account in bound])
        conn.executemany("INSERT INTO paper_parameter_versions VALUES(7,?,?)",
                         [(account, day) for account, day in attachment_dates.items()])
        return conn, PAS.builtin_account_ids()

    def test_pa21_exact_cycle_owners_require_asof_attachment_proof(self):
        import paper_account_specs as PAS

        first, second = PAS.builtin_account_ids()[:2]
        conn, builtin_scope = self._owner_conn(
            [first, second], [first, second], {first: "2026-10-01", second: "2026-10-10"})
        try:
            with self.assertRaisesRegex(ValueError, "cycle_economic_owners_unavailable"):
                PCY.exact_cycle_owner_snapshot(
                    conn, 7, asof_day="2026-10-01",
                    attachment_prover=PPRM.account_attached_by_asof,
                    builtin_scope=builtin_scope)
            owner_snapshot = PCY.exact_cycle_owner_snapshot(
                conn, 7, asof_day="2026-10-10",
                attachment_prover=PPRM.account_attached_by_asof,
                builtin_scope=builtin_scope)
            self.assertEqual(owner_snapshot["economic_owner_ids"], tuple(sorted((first, second))))
        finally:
            conn.close()

    def test_pa22_configured_and_resolved_owner_sets_must_match(self):
        import paper_account_specs as PAS

        first, second = PAS.builtin_account_ids()[:2]
        conn, builtin_scope = self._owner_conn(
            [first, second], [first],
            {first: "2026-10-01", second: "2026-10-01"})
        try:
            with self.assertRaisesRegex(ValueError, "cycle_economic_owners_unavailable"):
                PCY.exact_cycle_owner_snapshot(
                    conn, 7, asof_day="2026-10-01",
                    attachment_prover=PPRM.account_attached_by_asof,
                    builtin_scope=builtin_scope)
            conn.execute("UPDATE paper_cycles SET enabled_strategies=? WHERE id=7",
                         (json.dumps([first, "unknown"]),))
            with self.assertRaisesRegex(ValueError, "cycle_economic_owners_unavailable"):
                PCY.exact_cycle_owner_snapshot(
                    conn, 7, asof_day="2026-10-01",
                    attachment_prover=PPRM.account_attached_by_asof,
                    builtin_scope=builtin_scope)
        finally:
            conn.close()

    def test_pa23_service_does_not_own_snapshot_schema(self):
        tree = ast.parse(pathlib.Path(PRS.__file__).read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        self.assertNotIn("paper_schema_migrations", imported)
        source = pathlib.Path(PRS.__file__).read_text(encoding="utf-8")
        self.assertNotIn("ensure_portfolio_runtime_snapshots", source)

    def test_dimensions_must_be_complete_and_unique(self):
        with self.assertRaisesRegex(PR.PortfolioRuntimeError, "dimensions_must_be_complete"):
            _build(dimensions=_dimensions()[:-1])

    def test_execution_participants_must_be_economic_owners(self):
        with self.assertRaisesRegex(PR.PortfolioRuntimeError, "scope_inconsistent"):
            _build(execution_participant_ids=["alien"])

    def test_unavailable_dimension_cannot_claim_owner_provenance(self):
        with self.assertRaisesRegex(PR.PortfolioRuntimeError, "provenance_mismatch"):
            PR.PortfolioDimension("capacity", PR.UNAVAILABLE, {}, "OWNER_ISSUED",
                                  blocking_reasons=("failed",))

    def test_not_applicable_dimension_cannot_carry_facts(self):
        with self.assertRaisesRegex(PR.PortfolioRuntimeError, "must_be_empty"):
            PR.PortfolioDimension("capacity", PR.NOT_APPLICABLE, {"pending": 0},
                                  "OWNER_ISSUED", blocking_reasons=("not_applicable",))

    def test_unavailable_dimension_requires_reason(self):
        with self.assertRaisesRegex(PR.PortfolioRuntimeError, "requires_reason"):
            PR.PortfolioDimension("capacity", PR.UNAVAILABLE, {}, "UNAVAILABLE")

    def test_fingerprint_verification_binds_cycle_identity(self):
        self.assertFalse(PR.verify_snapshot_fingerprint(replace(_build(), cycle_id=8)))

    def test_exact_getter_reads_named_snapshot_not_newer_snapshot(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE portfolio_runtime_snapshots(snapshot_id TEXT PRIMARY KEY,"
                     "snapshot_fingerprint TEXT NOT NULL,evidence_json TEXT NOT NULL,"
                     "created_at TEXT NOT NULL)")
        first = _build()
        second = _build(asof_day="2026-10-01")
        PRRepo.append_snapshot(conn, first, created_at="1")
        PRRepo.append_snapshot(conn, second, created_at="2")
        self.assertEqual(PRRepo.get_snapshot(conn, first.snapshot_id).snapshot_id,
                         first.snapshot_id)
        conn.close()

    def test_corrupt_stored_evidence_is_rejected(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE portfolio_runtime_snapshots(snapshot_id TEXT PRIMARY KEY,"
                     "snapshot_fingerprint TEXT NOT NULL,evidence_json TEXT NOT NULL,"
                     "created_at TEXT NOT NULL)")
        snapshot = _build()
        PRRepo.append_snapshot(conn, snapshot)
        conn.execute("UPDATE portfolio_runtime_snapshots SET evidence_json='{}' WHERE snapshot_id=?",
                     (snapshot.snapshot_id,))
        with self.assertRaisesRegex(PRRepo.PortfolioRuntimeRepositoryError,
                                    "evidence_invalid"):
            PRRepo.get_snapshot(conn, snapshot.snapshot_id)
        conn.close()

    def test_pure_builder_has_no_database_provider_or_clock_imports(self):
        tree = ast.parse(pathlib.Path(PR.__file__).read_text(encoding="utf-8"))
        imports = {alias.name.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom))
                   for alias in node.names}
        self.assertFalse(imports & {"sqlite3", "fastapi", "requests", "urllib", "time",
                                    "socket", "paper_trading", "market_data_service"})

    def test_api_exposes_only_explicit_snapshot_capture_and_get(self):
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(API.portfolio_runtime_router)
        paths = set(app.openapi()["paths"])
        self.assertEqual(paths, {"/api/portfolio/runtime/snapshots",
                                 "/api/portfolio/runtime/snapshots/{snapshot_id}"})

    def test_created_at_is_not_fingerprint_material(self):
        snapshot = _build()
        self.assertTrue(PR.verify_snapshot_fingerprint(snapshot))


if __name__ == "__main__":
    unittest.main()
