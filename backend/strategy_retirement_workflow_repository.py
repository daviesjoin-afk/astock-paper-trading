# -*- coding: utf-8 -*-
"""Append-only persistence for exact R33-C proposals and human approvals."""
from __future__ import annotations

import json
import sqlite3

import strategy_retirement_workflow as WF


class RetirementWorkflowRepositoryError(ValueError):
    pass


def _proposal_from(row) -> WF.LifecycleTransitionProposal:
    try:
        material = json.loads(row[14])
        proposal = WF.LifecycleTransitionProposal(
            proposal_id=str(row[0]), proposal_fingerprint=str(row[1]),
            decision_id=material["decision_id"],
            decision_fingerprint=material["decision_fingerprint"],
            snapshot_id=material["snapshot_id"],
            snapshot_fingerprint=material["snapshot_fingerprint"],
            strategy_id=material["strategy_id"],
            strategy_version=int(material["strategy_version"]),
            strategy_checksum=material["strategy_checksum"],
            current_state=material["current_state"],
            target_state=material["target_state"], reason=material["reason"],
            approval_status=material["approval_status"],
            created_at=material["created_at"],
            schema_version=material.get("schema_version", WF.PROPOSAL_SCHEMA_VERSION),
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise RetirementWorkflowRepositoryError("retirement_proposal_evidence_invalid") from exc
    columns = ("proposal_id", "proposal_fingerprint", "decision_id", "decision_fingerprint",
               "snapshot_id", "snapshot_fingerprint", "strategy_id", "strategy_version",
               "strategy_checksum", "current_state", "target_state", "reason",
               "approval_status", "created_at")
    projection = proposal.projection()
    if (not WF.verify_proposal_fingerprint(proposal)
            or any(row[index] != projection[name] for index, name in enumerate(columns))):
        raise RetirementWorkflowRepositoryError("retirement_proposal_fingerprint_mismatch")
    return proposal


def _approval_from(row) -> WF.RetirementApproval:
    try:
        material = json.loads(row[8])
        approval = WF.RetirementApproval(
            approval_id=str(row[0]), approval_fingerprint=str(row[1]),
            proposal_id=material["proposal_id"],
            proposal_fingerprint=material["proposal_fingerprint"],
            operator_identity=material["operator_identity"],
            approval_action=material["approval_action"],
            approved_at=material["approved_at"], reason=material["reason"],
            schema_version=material.get("schema_version", WF.APPROVAL_SCHEMA_VERSION),
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise RetirementWorkflowRepositoryError("retirement_approval_evidence_invalid") from exc
    columns = ("approval_id", "approval_fingerprint", "proposal_id", "proposal_fingerprint",
               "operator_identity", "approval_action", "approved_at", "reason")
    projection = approval.projection()
    if (not WF.verify_approval_fingerprint(approval)
            or any(row[index] != projection[name] for index, name in enumerate(columns))):
        raise RetirementWorkflowRepositoryError("retirement_approval_fingerprint_mismatch")
    return approval


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def append_proposal(conn: sqlite3.Connection,
                    proposal: WF.LifecycleTransitionProposal
                    ) -> WF.LifecycleTransitionProposal:
    if not WF.verify_proposal_fingerprint(proposal):
        raise RetirementWorkflowRepositoryError("retirement_proposal_fingerprint_mismatch")
    conn.execute(
        "INSERT OR IGNORE INTO strategy_retirement_proposals"
        "(proposal_id,proposal_fingerprint,decision_id,decision_fingerprint,snapshot_id,"
        "snapshot_fingerprint,strategy_id,strategy_version,strategy_checksum,current_state,"
        "target_state,reason,approval_status,created_at,evidence_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (proposal.proposal_id, proposal.proposal_fingerprint, proposal.decision_id,
         proposal.decision_fingerprint, proposal.snapshot_id, proposal.snapshot_fingerprint,
         proposal.strategy_id, int(proposal.strategy_version), proposal.strategy_checksum,
         proposal.current_state, proposal.target_state, proposal.reason,
         proposal.approval_status, proposal.created_at, _json(proposal.projection())),
    )
    stored = get_proposal(conn, proposal.proposal_id)
    if stored is None or stored.proposal_fingerprint != proposal.proposal_fingerprint:
        raise RetirementWorkflowRepositoryError("retirement_proposal_idempotency_conflict")
    return stored


def get_proposal(conn: sqlite3.Connection,
                 proposal_id: str) -> WF.LifecycleTransitionProposal | None:
    if not isinstance(proposal_id, str) or len(proposal_id) != 64:
        raise RetirementWorkflowRepositoryError("explicit_retirement_proposal_id_required")
    row = conn.execute(
        "SELECT proposal_id,proposal_fingerprint,decision_id,decision_fingerprint,snapshot_id,"
        "snapshot_fingerprint,strategy_id,strategy_version,strategy_checksum,current_state,"
        "target_state,reason,approval_status,created_at,evidence_json"
        " FROM strategy_retirement_proposals WHERE proposal_id=?", (proposal_id,),
    ).fetchone()
    return None if row is None else _proposal_from(row)


def append_approval(conn: sqlite3.Connection,
                    approval: WF.RetirementApproval) -> WF.RetirementApproval:
    if not WF.verify_approval_fingerprint(approval):
        raise RetirementWorkflowRepositoryError("retirement_approval_fingerprint_mismatch")
    conn.execute(
        "INSERT OR IGNORE INTO strategy_retirement_approvals"
        "(approval_id,approval_fingerprint,proposal_id,proposal_fingerprint,operator_identity,"
        "approval_action,approved_at,reason,evidence_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (approval.approval_id, approval.approval_fingerprint, approval.proposal_id,
         approval.proposal_fingerprint, approval.operator_identity, approval.approval_action,
         approval.approved_at, approval.reason, _json(approval.projection())),
    )
    stored = get_approval_for_proposal(conn, approval.proposal_id)
    if stored is None or stored.approval_fingerprint != approval.approval_fingerprint:
        raise RetirementWorkflowRepositoryError("retirement_approval_idempotency_conflict")
    return stored


def get_approval_for_proposal(conn: sqlite3.Connection,
                              proposal_id: str) -> WF.RetirementApproval | None:
    """Read the single approval bound to one exact proposal; never reads a latest approval."""
    row = conn.execute(
        "SELECT approval_id,approval_fingerprint,proposal_id,proposal_fingerprint,"
        "operator_identity,approval_action,approved_at,reason,evidence_json"
        " FROM strategy_retirement_approvals WHERE proposal_id=?", (str(proposal_id),),
    ).fetchone()
    return None if row is None else _approval_from(row)


__all__ = ["RetirementWorkflowRepositoryError", "append_proposal", "get_proposal",
           "append_approval", "get_approval_for_proposal"]
