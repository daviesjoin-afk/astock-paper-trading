# -*- coding: utf-8 -*-
"""R33-C regressions: retirement proposals require human approval and exact CAS."""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock

from fastapi import HTTPException

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import api_strategies as API  # noqa: E402
import strategy_health as SH  # noqa: E402
import strategy_health_repository as SHR  # noqa: E402
import strategy_lifecycle as SL  # noqa: E402
import strategy_registry as SR  # noqa: E402
import strategy_retirement_policy as RP  # noqa: E402
import strategy_retirement_repository as RR  # noqa: E402
import strategy_retirement_workflow as WF  # noqa: E402
import strategy_retirement_workflow_repository as WFR  # noqa: E402
import strategy_retirement_workflow_service as RWS  # noqa: E402
import test_r33a_strategy_health as A  # noqa: E402


class _WorkflowCase(A._HealthCase):
    def _candidate_decision(self):
        dimensions = [SH.HealthDimension(
            name=name, status=SH.STATUS_AVAILABLE, facts={"fixture": True},
            provenance=SH.PROVENANCE_OWNER_ISSUED, source_identity=name,
            source_fingerprint=("a" if index % 2 else "b") * 64,
        ) for index, name in enumerate(SH.DIMENSIONS)]
        snapshot = SH.build_strategy_health(
            strategy_id=self.spec.id, strategy_version=self.version.version,
            strategy_checksum=self.version.checksum,
            observation_window=SH.HealthObservationWindow(**A.WINDOW),
            lifecycle_state="draft", dimensions=dimensions)
        SHR.append_snapshot(self.conn, snapshot)
        with mock.patch.object(RP, "_decision_for_conditions",
                               return_value=RP.DECISION_ARCHIVE_READY):
            decision = RP.evaluate_retirement_policy(snapshot)
        RR.append_decision(self.conn, decision)
        self.conn.commit()
        self.snapshot, self.decision = snapshot, decision
        return decision

    def _proposal(self):
        decision = self._candidate_decision()
        return RWS.create_transition_proposal(self.spec.id, decision_id=decision.decision_id)

    def _state(self):
        return SL.get_state(self.conn, self.spec.id, self.version.version,
                            checksum=self.version.checksum)["state"]

    def _lifecycle_projection(self):
        state = self.conn.execute(
            "SELECT strategy_id,strategy_version,state,last_event_id,updated_at"
            " FROM strategy_lifecycle_state ORDER BY strategy_id,strategy_version").fetchall()
        events = self.conn.execute(
            "SELECT event_fingerprint FROM strategy_lifecycle_events ORDER BY id").fetchall()
        return ([tuple(row) for row in state], [row[0] for row in events])


class RetirementWorkflowContractTests(_WorkflowCase):
    def test_c1_proposal_binds_exact_decision_snapshot_version_and_state(self):
        result = self._proposal()
        self.assertEqual(self.decision.decision_id, result["decision_id"])
        self.assertEqual(self.decision.decision_fingerprint, result["decision_fingerprint"])
        self.assertEqual(self.snapshot.snapshot_id, result["snapshot_id"])
        self.assertEqual(self.version.version, result["strategy_version"])
        self.assertEqual(self.version.checksum, result["strategy_checksum"])
        self.assertEqual("draft", result["current_state"])
        self.assertEqual("archived", result["target_state"])
        self.assertEqual("PENDING_APPROVAL", result["approval_status"])
        self.assertEqual(result["proposal_id"], result["proposal_fingerprint"])

    def test_c2_mismatched_snapshot_fails_closed(self):
        decision = self._candidate_decision()
        other = SH.build_strategy_health(
            strategy_id=self.spec.id, strategy_version=self.version.version,
            strategy_checksum=self.version.checksum,
            observation_window=SH.HealthObservationWindow(
                observation_start="2026-09-02", observation_end="2026-10-01"),
            lifecycle_state="draft", dimensions=self.snapshot.dimensions)
        SHR.append_snapshot(self.conn, other)
        self.conn.commit()
        with mock.patch.object(SHR, "get_snapshot", return_value=other):
            with self.assertRaises(WF.RetirementWorkflowError) as raised:
                RWS.create_transition_proposal(self.spec.id, decision_id=decision.decision_id)
        self.assertEqual("retirement_decision_snapshot_identity_mismatch", str(raised.exception))

    def test_c3_wrong_strategy_version_or_checksum_fails(self):
        self._candidate_decision()
        real = SR.get_version(self.spec.id, self.version.version, conn=self.conn)
        wrong = SR.get_version(self.spec.id, self.version.version, conn=self.conn)
        wrong = dataclasses.replace(real, checksum="0" * 64)
        with mock.patch.object(SR, "get_version", return_value=wrong):
            with self.assertRaises(WF.RetirementWorkflowError) as raised:
                RWS.create_transition_proposal(
                    self.spec.id, decision_id=self.decision.decision_id)
        self.assertEqual("strategy_version_checksum_mismatch", str(raised.exception))

    def test_c4_pending_proposal_cannot_execute(self):
        proposal = self._proposal()
        before = self._lifecycle_projection()
        with self.assertRaises(WF.RetirementWorkflowError) as raised:
            RWS.execute_transition_proposal(proposal["proposal_id"])
        self.assertEqual("retirement_approval_required", str(raised.exception))
        self.assertEqual(before, self._lifecycle_projection())

    def test_c5_rejection_never_transitions(self):
        proposal = self._proposal()
        before = self._lifecycle_projection()
        rejected = RWS.approve_transition_proposal(
            proposal["proposal_id"], operator_identity="operator-1",
            approval_action="REJECT", reason="review rejected")
        self.assertEqual("REJECTED", rejected["status"])
        with self.assertRaises(WF.RetirementWorkflowError):
            RWS.execute_transition_proposal(proposal["proposal_id"])
        self.assertEqual(before, self._lifecycle_projection())

    def test_c6_approved_proposal_executes_once_through_lifecycle_owner(self):
        proposal = self._proposal()
        approved = RWS.approve_transition_proposal(
            proposal["proposal_id"], operator_identity="operator-1",
            reason="approved after review")
        self.assertEqual("APPROVED", approved["status"])
        self.assertEqual("draft", self._state(), "approval alone must not transition lifecycle")
        result = RWS.execute_transition_proposal(proposal["proposal_id"])
        self.assertEqual("EXECUTED", result["status"])
        self.assertEqual("archived", result["transitioned_state"])
        self.assertEqual("archived", self._state())
        event = self.conn.execute(
            "SELECT actor_type,actor_id,reason_text,evidence_json FROM strategy_lifecycle_events"
            " WHERE strategy_id=? AND strategy_version=? AND to_state='archived'",
            (self.spec.id, self.version.version)).fetchone()
        self.assertEqual(("human", "operator-1", "approved after review"), tuple(event[:3]))
        evidence = json.loads(event[3])
        self.assertEqual(proposal["proposal_fingerprint"], evidence["proposal_fingerprint"])
        self.assertEqual(approved["approval"]["approval_fingerprint"],
                         evidence["approval_fingerprint"])
        self.assertEqual("EXECUTED", RWS.execute_transition_proposal(
            proposal["proposal_id"])["status"])
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_lifecycle_events WHERE strategy_id=?"
            " AND strategy_version=? AND to_state='archived'",
            (self.spec.id, self.version.version)).fetchone()[0])

    def test_c7_invalid_transition_is_rejected_by_transition_table(self):
        decision = self._candidate_decision()
        with mock.patch.object(RP, "build_transition_proposal", return_value={
                "target_state": "retiring", "reason": "test_invalid_edge"}):
            with self.assertRaises(WF.RetirementWorkflowError) as raised:
                RWS.create_transition_proposal(self.spec.id, decision_id=decision.decision_id)
        self.assertEqual("invalid_lifecycle_transition", str(raised.exception))

    def test_c8_changed_lifecycle_state_makes_approved_proposal_fail(self):
        proposal = self._proposal()
        RWS.approve_transition_proposal(proposal["proposal_id"],
                                        operator_identity="operator-1")
        SL.transition(self.conn, strategy_id=self.spec.id,
            strategy_version=self.version.version, strategy_checksum=self.version.checksum,
            expected_state="draft", target_state="quarantined", actor_type="human",
            actor_id="operator-2", reason_code="test", reason_text="state changed",
            transition_kind="safety")
        self.conn.commit()
        with self.assertRaises(WF.RetirementWorkflowError) as raised:
            RWS.execute_transition_proposal(proposal["proposal_id"])
        self.assertEqual("retirement_proposal_lifecycle_state_changed", str(raised.exception))
        self.assertEqual("FAILED", RWS.get_transition_proposal(
            proposal["proposal_id"])["status"])

    def test_c9_same_proposal_append_is_idempotent(self):
        first = self._proposal()
        second = RWS.create_transition_proposal(
            self.spec.id, decision_id=self.decision.decision_id)
        self.assertEqual(first["proposal_id"], second["proposal_id"])
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_retirement_proposals WHERE proposal_id=?",
            (first["proposal_id"],)).fetchone()[0])

    def test_c10_proposal_mutation_is_detected(self):
        proposal = self._proposal()
        canonical = WFR.get_proposal(self.conn, proposal["proposal_id"])
        self.assertFalse(WF.verify_proposal_fingerprint(
            dataclasses.replace(canonical, reason="mutated")))
        tampered = dict(proposal)
        tampered["reason"] = "mutated"
        self.conn.execute("DROP TRIGGER strategy_retirement_proposals_no_update")
        self.conn.execute("UPDATE strategy_retirement_proposals SET evidence_json=?"
                          " WHERE proposal_id=?", (json.dumps(tampered), proposal["proposal_id"]))
        self.conn.commit()
        with self.assertRaises(WFR.RetirementWorkflowRepositoryError):
            WFR.get_proposal(self.conn, proposal["proposal_id"])

    def test_c11_approval_mutation_is_detected(self):
        proposal = self._proposal()
        approval = RWS.approve_transition_proposal(
            proposal["proposal_id"], operator_identity="operator-1")
        canonical = WFR.get_approval_for_proposal(self.conn, proposal["proposal_id"])
        self.assertFalse(WF.verify_approval_fingerprint(
            dataclasses.replace(canonical, reason="mutated")))
        tampered = dict(approval["approval"])
        tampered["reason"] = "mutated"
        self.conn.execute("DROP TRIGGER strategy_retirement_approvals_no_update")
        self.conn.execute("UPDATE strategy_retirement_approvals SET evidence_json=?"
                          " WHERE proposal_id=?",
                          (json.dumps(tampered), proposal["proposal_id"]))
        self.conn.commit()
        with self.assertRaises(WFR.RetirementWorkflowRepositoryError):
            WFR.get_approval_for_proposal(self.conn, proposal["proposal_id"])
        self.assertTrue(approval["approval"]["approval_fingerprint"])

    def test_c12_ai_or_provider_unavailable_cannot_change_workflow(self):
        proposal = self._proposal()
        before = self._lifecycle_projection()
        with self.assertRaises(WF.RetirementWorkflowError):
            RWS.approve_transition_proposal(
                proposal["proposal_id"], operator_identity="ai:retirement-agent")
        imports = set()
        tree = ast.parse(pathlib.Path(RWS.__file__).read_text(encoding="utf-8"))
        for item in ast.walk(tree):
            if isinstance(item, ast.Import):
                imports.update(alias.name for alias in item.names)
            elif isinstance(item, ast.ImportFrom) and item.module:
                imports.add(item.module)
        self.assertFalse(any(name.lower().startswith(("openai", "provider", "scheduler"))
                             for name in imports))
        self.assertEqual(before, self._lifecycle_projection())

    def test_c13_execution_does_not_change_trade_ledger(self):
        proposal = self._proposal()
        RWS.approve_transition_proposal(proposal["proposal_id"],
                                        operator_identity="operator-1")
        tables = ("paper_accounts", "paper_positions", "paper_orders", "paper_fills")
        before = {table: self.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                  for table in tables}
        RWS.execute_transition_proposal(proposal["proposal_id"])
        after = {table: self.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                 for table in tables}
        self.assertEqual(before, after)

    def test_c14_c15_no_scheduler_or_automatic_transition_path(self):
        import strategy_health as health
        for module in (RP, health):
            tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
            modules = {node.module for node in ast.walk(tree)
                       if isinstance(node, ast.ImportFrom) and node.module}
            modules.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                           for alias in node.names)
            calls = {node.func.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
            self.assertNotIn("strategy_lifecycle", modules)
            self.assertNotIn("transition", calls)
        health_service_tree = ast.parse(pathlib.Path(A.SHV.__file__).read_text(encoding="utf-8"))
        self.assertFalse(any(isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "transition"
                            for node in ast.walk(health_service_tree)))
        source = pathlib.Path(RWS.__file__).read_text(encoding="utf-8")
        self.assertNotIn("scheduler", source.lower())
        self.assertNotIn("background", source.lower())
        self.assertIn("SL.transition(", source)
        class TransitionCallerVisitor(ast.NodeVisitor):
            def __init__(self):
                self.stack = []
                self.callers = set()

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                if (isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "SL"
                        and node.func.attr == "transition"):
                    self.callers.add(tuple(self.stack))
                self.generic_visit(node)

        visitor = TransitionCallerVisitor()
        visitor.visit(ast.parse(source))
        self.assertEqual({("execute_transition_proposal", "_work")}, visitor.callers)

    def test_repository_never_falls_back_to_latest_proposal(self):
        proposal = self._proposal()
        self.assertIsNone(WFR.get_proposal(self.conn, "f" * 64))
        self.assertIsNotNone(WFR.get_proposal(self.conn, proposal["proposal_id"]))

    def test_workflow_tables_are_append_only_and_have_no_current_or_latest_columns(self):
        import paper_schema_migrations as PSM
        proposal = self._proposal()
        RWS.approve_transition_proposal(proposal["proposal_id"],
                                        operator_identity="operator-1")
        self.assertFalse(any(name in {"current_retirement_status", "latest_proposal"}
                             for name in (*PSM.STRATEGY_RETIREMENT_PROPOSAL_COLUMNS,
                                          *PSM.STRATEGY_RETIREMENT_APPROVAL_COLUMNS)))
        for table in ("strategy_retirement_proposals", "strategy_retirement_approvals"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(f"DELETE FROM {table}")


class RetirementWorkflowApiTests(_WorkflowCase):
    def test_api_surface_has_only_exact_proposal_routes_and_no_status_endpoint(self):
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(API.router)
        app.include_router(API.retirement_workflow_router)
        paths = set(app.openapi()["paths"])
        self.assertIn("/api/strategies/{strategy_id}/retirement/proposals", paths)
        self.assertIn("/api/retirement/proposals/{proposal_id}/approve", paths)
        self.assertIn("/api/retirement/proposals/{proposal_id}/execute", paths)
        self.assertIn("/api/retirement/proposals/{proposal_id}", paths)
        self.assertNotIn("/api/strategies/{strategy_id}/retirement/status", paths)

    def test_api_rejects_missing_operator_and_creates_nothing(self):
        proposal = self._proposal()
        before = self.conn.execute(
            "SELECT COUNT(*) FROM strategy_retirement_approvals").fetchone()[0]
        with self.assertRaises(HTTPException):
            API.approve_retirement_proposal(proposal["proposal_id"], {"reason": "no identity"})
        self.assertEqual(before, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_retirement_approvals").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
