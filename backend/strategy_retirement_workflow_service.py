# -*- coding: utf-8 -*-
"""R33-C operator-controlled retirement workflow application boundary."""
from __future__ import annotations

import datetime as dt
import json

import paper_trading as PT
import strategy_health as SH
import strategy_health_repository as SHR
import strategy_lifecycle as SL
import strategy_registry as SR
import strategy_retirement_policy as RP
import strategy_retirement_repository as RR
import strategy_retirement_workflow as WF
import strategy_retirement_workflow_repository as WFR


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _with_connection(work, *, immediate: bool = False):
    PT.init_db()
    with PT._db(immediate=immediate) as conn:
        return work(conn)


def _decision_for(conn, decision_id: str, strategy_id: str | None = None):
    try:
        decision = RR.get_decision(conn, decision_id)
    except RR.StrategyRetirementRepositoryError as exc:
        raise WF.RetirementWorkflowError(str(exc)) from exc
    if decision is None:
        raise WF.RetirementWorkflowError("retirement_decision_not_found")
    if strategy_id is not None and decision.strategy_id != str(strategy_id):
        raise WF.RetirementWorkflowError("retirement_decision_strategy_mismatch")
    return decision


def _snapshot_for(conn, decision):
    try:
        snapshot = SHR.get_snapshot(conn, decision.snapshot_id)
    except SHR.StrategyHealthRepositoryError as exc:
        raise WF.RetirementWorkflowError("health_snapshot_corrupt") from exc
    if snapshot is None:
        raise WF.RetirementWorkflowError("health_snapshot_not_found")
    if (not SH.verify_snapshot_fingerprint(snapshot)
            or snapshot.snapshot_fingerprint != decision.snapshot_fingerprint
            or snapshot.strategy_id != decision.strategy_id
            or int(snapshot.strategy_version) != int(decision.strategy_version)
            or snapshot.strategy_checksum != decision.strategy_checksum):
        raise WF.RetirementWorkflowError("retirement_decision_snapshot_identity_mismatch")
    return snapshot


def _exact_lifecycle_state(conn, *, strategy_id: str, strategy_version: int,
                           strategy_checksum: str):
    try:
        version = SR.get_version(strategy_id, int(strategy_version),
                                 checksum=strategy_checksum, conn=conn)
    except (TypeError, ValueError) as exc:
        raise WF.RetirementWorkflowError("strategy_version_checksum_mismatch") from exc
    if version is None:
        raise WF.RetirementWorkflowError("strategy_version_checksum_mismatch")
    if (int(version.version) != int(strategy_version)
            or str(version.checksum) != str(strategy_checksum)):
        raise WF.RetirementWorkflowError("strategy_version_checksum_mismatch")
    state = SL.get_state(conn, strategy_id, int(strategy_version),
                         checksum=strategy_checksum)
    if state is None:
        raise WF.RetirementWorkflowError("strategy_lifecycle_state_not_found")
    return str(state["state"])


def _proposal_or_error(conn, proposal_id: str):
    try:
        proposal = WFR.get_proposal(conn, proposal_id)
    except WFR.RetirementWorkflowRepositoryError as exc:
        raise WF.RetirementWorkflowError(str(exc)) from exc
    if proposal is None:
        raise WF.RetirementWorkflowError("retirement_proposal_not_found")
    return proposal


def _approval_for(conn, proposal):
    try:
        approval = WFR.get_approval_for_proposal(conn, proposal.proposal_id)
    except WFR.RetirementWorkflowRepositoryError as exc:
        raise WF.RetirementWorkflowError(str(exc)) from exc
    if approval is not None and approval.proposal_fingerprint != proposal.proposal_fingerprint:
        raise WF.RetirementWorkflowError("retirement_approval_proposal_identity_mismatch")
    return approval


def _matching_lifecycle_event(conn, proposal, approval):
    rows = conn.execute(
        "SELECT event_fingerprint,from_state,to_state,strategy_checksum,actor_type,actor_id,"
        "reason_code,reason_text,evidence_json FROM strategy_lifecycle_events "
        "WHERE strategy_id=? AND strategy_version=?",
        (proposal.strategy_id, int(proposal.strategy_version)),
    ).fetchall()
    for row in rows:
        try:
            evidence = json.loads(row[8] or "{}")
        except (TypeError, ValueError):
            continue
        if evidence.get("proposal_id") != proposal.proposal_id:
            continue
        if (row[1] != proposal.current_state or row[2] != proposal.target_state
                or row[3] != proposal.strategy_checksum or row[4] != "human"
                or approval is None or row[5] != approval.operator_identity
                or evidence.get("proposal_fingerprint") != proposal.proposal_fingerprint
                or evidence.get("approval_fingerprint") != approval.approval_fingerprint
                or evidence.get("decision_fingerprint") != proposal.decision_fingerprint
                or not row[0]):
            raise WF.RetirementWorkflowError("retirement_lifecycle_event_identity_mismatch")
        return dict(zip(("event_fingerprint", "from_state", "to_state",
                         "strategy_checksum", "actor_type", "actor_id", "reason_code",
                         "reason_text", "evidence_json"), row, strict=True))
    return None


def _status(conn, proposal, approval):
    event = _matching_lifecycle_event(conn, proposal, approval)
    if event is not None:
        return "EXECUTED", event
    if approval is None:
        status = "PENDING_APPROVAL"
    elif approval.approval_action == "REJECT":
        status = "REJECTED"
    else:
        status = "APPROVED"
    if status in {"PENDING_APPROVAL", "APPROVED"}:
        try:
            current = _exact_lifecycle_state(
                conn, strategy_id=proposal.strategy_id,
                strategy_version=proposal.strategy_version,
                strategy_checksum=proposal.strategy_checksum)
        except WF.RetirementWorkflowError:
            return "FAILED", None
        if (current != proposal.current_state
                or proposal.target_state not in SL.TRANSITION_TABLE.get(current, ())):
            return "FAILED", None
    return status, None


def _response(conn, proposal, approval=None):
    if approval is None:
        approval = _approval_for(conn, proposal)
    status, event = _status(conn, proposal, approval)
    result = proposal.projection()
    result["approval_status"] = (
        "APPROVED" if status in {"APPROVED", "EXECUTED"} else
        "REJECTED" if status == "REJECTED" else
        "PENDING_APPROVAL" if status == "PENDING_APPROVAL" else "FAILED")
    result["status"] = status
    result["approval"] = None if approval is None else approval.projection()
    result["lifecycle_event"] = event
    return result


def create_transition_proposal(strategy_id: str, *, decision_id: str) -> dict:
    """Append an exact proposal for a policy action candidate, if still current."""
    def _work(conn):
        decision = _decision_for(conn, decision_id, strategy_id)
        snapshot = _snapshot_for(conn, decision)
        captured_state = snapshot.lifecycle_state
        current_state = _exact_lifecycle_state(
            conn, strategy_id=decision.strategy_id,
            strategy_version=decision.strategy_version,
            strategy_checksum=decision.strategy_checksum)
        if current_state != captured_state:
            raise WF.RetirementWorkflowError("retirement_decision_lifecycle_state_stale")
        candidate = RP.build_transition_proposal(
            decision, lifecycle_state=captured_state,
            legal_targets=SL.TRANSITION_TABLE.get(captured_state or "", ()))
        if candidate is None:
            raise WF.RetirementWorkflowError("retirement_decision_has_no_executable_candidate")
        if candidate["target_state"] not in SL.TRANSITION_TABLE.get(current_state, ()):
            raise WF.RetirementWorkflowError("invalid_lifecycle_transition")
        proposal = WF.make_proposal(
            decision_id=decision.decision_id,
            decision_fingerprint=decision.decision_fingerprint,
            snapshot_id=snapshot.snapshot_id,
            snapshot_fingerprint=snapshot.snapshot_fingerprint,
            strategy_id=decision.strategy_id,
            strategy_version=decision.strategy_version,
            strategy_checksum=decision.strategy_checksum,
            current_state=current_state, target_state=candidate["target_state"],
            reason=candidate["reason"], created_at=_now())
        try:
            stored = WFR.append_proposal(conn, proposal)
        except WFR.RetirementWorkflowRepositoryError as exc:
            raise WF.RetirementWorkflowError(str(exc)) from exc
        return _response(conn, stored)
    return _with_connection(_work, immediate=True)


def approve_transition_proposal(proposal_id: str, *, operator_identity: str,
                                approval_action: str = "APPROVE", reason: str = "") -> dict:
    """Record exactly one human APPROVE or REJECT decision for a proposal."""
    def _work(conn):
        proposal = _proposal_or_error(conn, proposal_id)
        identity = str(operator_identity or "").strip()
        if identity.casefold().startswith(("ai:", "system:", "automation:", "provider:", "bot:")):
            raise WF.RetirementWorkflowError("human_operator_required")
        existing = _approval_for(conn, proposal)
        if existing is not None:
            try:
                candidate = WF.make_approval(
                    proposal_id=proposal.proposal_id,
                    proposal_fingerprint=proposal.proposal_fingerprint,
                    operator_identity=identity,
                    approval_action=approval_action, approved_at=_now(), reason=reason)
            except WF.RetirementWorkflowError as exc:
                raise WF.RetirementWorkflowError(str(exc)) from exc
            if existing.approval_fingerprint != candidate.approval_fingerprint:
                raise WF.RetirementWorkflowError("retirement_proposal_already_reviewed")
            return _response(conn, proposal, existing)
        status, _ = _status(conn, proposal, None)
        if status != "PENDING_APPROVAL":
            raise WF.RetirementWorkflowError("retirement_proposal_not_pending")
        try:
            approval = WF.make_approval(
                proposal_id=proposal.proposal_id,
                proposal_fingerprint=proposal.proposal_fingerprint,
                operator_identity=identity,
                approval_action=approval_action, approved_at=_now(), reason=reason)
            stored = WFR.append_approval(conn, approval)
        except (WF.RetirementWorkflowError,
                WFR.RetirementWorkflowRepositoryError) as exc:
            raise WF.RetirementWorkflowError(str(exc)) from exc
        return _response(conn, proposal, stored)
    return _with_connection(_work, immediate=True)


def execute_transition_proposal(proposal_id: str) -> dict:
    """Revalidate the approved exact proposal and apply via the R31 CAS owner."""
    def _work(conn):
        proposal = _proposal_or_error(conn, proposal_id)
        approval = _approval_for(conn, proposal)
        if approval is None or approval.approval_action != "APPROVE":
            raise WF.RetirementWorkflowError("retirement_approval_required")
        if (approval.operator_identity.startswith("ai:")
                or approval.operator_identity.startswith("system:")):
            raise WF.RetirementWorkflowError("human_operator_required")
        already = _matching_lifecycle_event(conn, proposal, approval)
        if already is not None:
            result = _response(conn, proposal, approval)
            result["transitioned_state"] = str(already["to_state"])
            return result

        decision = _decision_for(conn, proposal.decision_id, proposal.strategy_id)
        if (decision.decision_fingerprint != proposal.decision_fingerprint
                or decision.snapshot_id != proposal.snapshot_id
                or decision.snapshot_fingerprint != proposal.snapshot_fingerprint
                or decision.strategy_version != proposal.strategy_version
                or decision.strategy_checksum != proposal.strategy_checksum):
            raise WF.RetirementWorkflowError("retirement_proposal_decision_identity_mismatch")
        snapshot = _snapshot_for(conn, decision)
        if (snapshot.snapshot_id != proposal.snapshot_id
                or snapshot.snapshot_fingerprint != proposal.snapshot_fingerprint):
            raise WF.RetirementWorkflowError("retirement_proposal_snapshot_identity_mismatch")
        current_state = _exact_lifecycle_state(
            conn, strategy_id=proposal.strategy_id,
            strategy_version=proposal.strategy_version,
            strategy_checksum=proposal.strategy_checksum)
        if current_state != proposal.current_state:
            raise WF.RetirementWorkflowError("retirement_proposal_lifecycle_state_changed")
        if proposal.target_state not in SL.TRANSITION_TABLE.get(current_state, ()):
            raise WF.RetirementWorkflowError("invalid_lifecycle_transition")
        candidate = RP.build_transition_proposal(
            decision, lifecycle_state=current_state,
            legal_targets=SL.TRANSITION_TABLE.get(current_state, ()))
        if candidate is None or candidate.get("target_state") != proposal.target_state:
            raise WF.RetirementWorkflowError("retirement_proposal_policy_candidate_mismatch")

        try:
            state = SL.transition(
                conn, strategy_id=proposal.strategy_id,
                strategy_version=proposal.strategy_version,
                strategy_checksum=proposal.strategy_checksum,
                expected_state=proposal.current_state, target_state=proposal.target_state,
                actor_type="human", actor_id=approval.operator_identity,
                reason_code=candidate["reason"],
                reason_text=approval.reason or proposal.reason,
                transition_kind="safety",
                evidence={
                    "source": "r33c_retirement_workflow",
                    "proposal_id": proposal.proposal_id,
                    "proposal_fingerprint": proposal.proposal_fingerprint,
                    "decision_id": proposal.decision_id,
                    "decision_fingerprint": proposal.decision_fingerprint,
                    "snapshot_id": proposal.snapshot_id,
                    "snapshot_fingerprint": proposal.snapshot_fingerprint,
                    "approval_id": approval.approval_id,
                    "approval_fingerprint": approval.approval_fingerprint,
                    "approval_reason": approval.reason,
                })
        except SL.LifecycleError as exc:
            raise WF.RetirementWorkflowError(str(exc)) from exc
        result = _response(conn, proposal, approval)
        result["transitioned_state"] = state["state"]
        return result
    return _with_connection(_work, immediate=True)


def get_transition_proposal(proposal_id: str) -> dict:
    """Read one named proposal and its derived state, never a current/latest record."""
    def _work(conn):
        proposal = _proposal_or_error(conn, proposal_id)
        decision = _decision_for(conn, proposal.decision_id, proposal.strategy_id)
        if (decision.decision_fingerprint != proposal.decision_fingerprint
                or decision.snapshot_id != proposal.snapshot_id
                or decision.snapshot_fingerprint != proposal.snapshot_fingerprint
                or decision.strategy_version != proposal.strategy_version
                or decision.strategy_checksum != proposal.strategy_checksum):
            raise WF.RetirementWorkflowError("retirement_proposal_decision_identity_mismatch")
        snapshot = _snapshot_for(conn, decision)
        if (snapshot.snapshot_id != proposal.snapshot_id
                or snapshot.snapshot_fingerprint != proposal.snapshot_fingerprint):
            raise WF.RetirementWorkflowError("retirement_proposal_snapshot_identity_mismatch")
        return _response(conn, proposal)
    return _with_connection(_work)


__all__ = ["create_transition_proposal", "approve_transition_proposal",
           "execute_transition_proposal", "get_transition_proposal"]
