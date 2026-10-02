# -*- coding: utf-8 -*-
"""Immutable R33-C proposal and human approval contracts.

This module has no I/O and no lifecycle dependency. R33-B owns recommendations;
the workflow service owns review and asks the R31 lifecycle owner to execute.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

PROPOSAL_SCHEMA_VERSION = "lifecycle-transition-proposal-v1"
APPROVAL_SCHEMA_VERSION = "retirement-approval-v1"
APPROVAL_ACTIONS = frozenset({"APPROVE", "REJECT"})
PROPOSAL_STATES = frozenset({
    "PENDING_APPROVAL", "APPROVED", "REJECTED", "EXECUTED", "FAILED",
})


class RetirementWorkflowError(ValueError):
    """Stable rejection from the controlled retirement workflow."""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LifecycleTransitionProposal:
    proposal_id: str
    proposal_fingerprint: str
    decision_id: str
    decision_fingerprint: str
    snapshot_id: str
    snapshot_fingerprint: str
    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    current_state: str
    target_state: str
    reason: str
    approval_status: str
    created_at: str
    schema_version: str = PROPOSAL_SCHEMA_VERSION

    def fingerprint_material(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "decision_fingerprint": self.decision_fingerprint,
            "snapshot_id": self.snapshot_id,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "strategy_id": self.strategy_id,
            "strategy_version": int(self.strategy_version),
            "strategy_checksum": self.strategy_checksum,
            "current_state": self.current_state,
            "target_state": self.target_state,
            "reason": self.reason,
            "approval_status": self.approval_status,
        }

    def projection(self) -> dict:
        return {"proposal_id": self.proposal_id,
                "proposal_fingerprint": self.proposal_fingerprint,
                **self.fingerprint_material(), "created_at": self.created_at}


def verify_proposal_fingerprint(proposal: LifecycleTransitionProposal) -> bool:
    if not isinstance(proposal, LifecycleTransitionProposal):
        raise RetirementWorkflowError("canonical_retirement_proposal_required")
    return (proposal.proposal_id == proposal.proposal_fingerprint
            and _sha(proposal.fingerprint_material()) == proposal.proposal_fingerprint)


def make_proposal(*, decision_id: str, decision_fingerprint: str,
                  snapshot_id: str, snapshot_fingerprint: str, strategy_id: str,
                  strategy_version: int, strategy_checksum: str, current_state: str,
                  target_state: str, reason: str, created_at: str
                  ) -> LifecycleTransitionProposal:
    material = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "decision_id": str(decision_id),
        "decision_fingerprint": str(decision_fingerprint),
        "snapshot_id": str(snapshot_id),
        "snapshot_fingerprint": str(snapshot_fingerprint),
        "strategy_id": str(strategy_id),
        "strategy_version": int(strategy_version),
        "strategy_checksum": str(strategy_checksum),
        "current_state": str(current_state),
        "target_state": str(target_state),
        "reason": str(reason),
        "approval_status": "PENDING_APPROVAL",
    }
    fingerprint = _sha(material)
    return LifecycleTransitionProposal(
        proposal_id=fingerprint, proposal_fingerprint=fingerprint,
        decision_id=str(decision_id), decision_fingerprint=str(decision_fingerprint),
        snapshot_id=str(snapshot_id), snapshot_fingerprint=str(snapshot_fingerprint),
        strategy_id=str(strategy_id), strategy_version=int(strategy_version),
        strategy_checksum=str(strategy_checksum), current_state=str(current_state),
        target_state=str(target_state), reason=str(reason),
        approval_status="PENDING_APPROVAL", created_at=str(created_at),
    )


@dataclass(frozen=True, slots=True)
class RetirementApproval:
    approval_id: str
    approval_fingerprint: str
    proposal_id: str
    proposal_fingerprint: str
    operator_identity: str
    approval_action: str
    approved_at: str
    reason: str
    schema_version: str = APPROVAL_SCHEMA_VERSION

    def fingerprint_material(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "operator_identity": self.operator_identity,
            "approval_action": self.approval_action,
            "reason": self.reason,
        }

    def projection(self) -> dict:
        return {"approval_id": self.approval_id,
                "approval_fingerprint": self.approval_fingerprint,
                **self.fingerprint_material(), "approved_at": self.approved_at}


def make_approval(*, proposal_id: str, proposal_fingerprint: str,
                  operator_identity: str, approval_action: str,
                  approved_at: str, reason: str) -> RetirementApproval:
    identity = str(operator_identity or "").strip()
    action = str(approval_action or "").upper()
    if not identity:
        raise RetirementWorkflowError("operator_identity_required")
    if action not in APPROVAL_ACTIONS:
        raise RetirementWorkflowError("retirement_approval_action_invalid")
    material = {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "proposal_id": str(proposal_id),
        "proposal_fingerprint": str(proposal_fingerprint),
        "operator_identity": identity,
        "approval_action": action,
        "reason": str(reason or "").strip(),
    }
    fingerprint = _sha(material)
    return RetirementApproval(
        approval_id=fingerprint, approval_fingerprint=fingerprint,
        proposal_id=str(proposal_id), proposal_fingerprint=str(proposal_fingerprint),
        operator_identity=identity, approval_action=action,
        approved_at=str(approved_at), reason=material["reason"],
    )


def verify_approval_fingerprint(approval: RetirementApproval) -> bool:
    if not isinstance(approval, RetirementApproval):
        raise RetirementWorkflowError("canonical_retirement_approval_required")
    return (approval.approval_id == approval.approval_fingerprint
            and _sha(approval.fingerprint_material()) == approval.approval_fingerprint)


__all__ = [
    "APPROVAL_ACTIONS", "PROPOSAL_STATES", "LifecycleTransitionProposal",
    "RetirementApproval", "RetirementWorkflowError", "make_proposal",
    "make_approval", "verify_proposal_fingerprint", "verify_approval_fingerprint",
]
