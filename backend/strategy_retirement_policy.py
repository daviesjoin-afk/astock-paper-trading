# -*- coding: utf-8 -*-
"""Strategy retirement policy（R33-B）：解释健康事实，不执行 lifecycle。

这个模块是 **policy authority**，不是 mutation authority：

- 输入只有 :class:`strategy_health.StrategyHealthSnapshot`（R33-A 的事实层）；
- 输出只有 :class:`StrategyRetirementDecision`（动作候选或证据不足）；
- **不**读原始表、不重算收益/回撤/风险、不调用 ``strategy_lifecycle.transition``。

纯函数契约：无 DB、无 provider、无网络、无机器时钟、无 current/latest 查询。
同一个 snapshot 必须永远得到同一个 decision。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import strategy_health as SH

#: 策略版本。规则变化必须递增：旧 decision 因此不会漂移。
RETIREMENT_POLICY_VERSION = "r33b.v1"
RETIREMENT_SCHEMA_VERSION = "strategy-retirement-decision-v1"

DECISION_NO_ACTION = "NO_ACTION"
DECISION_DEGRADE_CANDIDATE = "DEGRADE_CANDIDATE"
DECISION_RETIRE_CANDIDATE = "RETIRE_CANDIDATE"
DECISION_ARCHIVE_READY = "ARCHIVE_READY"
DECISION_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

#: 固定词表 = 动作候选 + 证据不足。禁止 BAD_STRATEGY / FAILED_STRATEGY / UNHEALTHY
#: 这类业务评价词：policy 不评价策略好坏，只给出「下一步可以做什么」的候选。
DECISIONS = (
    DECISION_NO_ACTION,
    DECISION_DEGRADE_CANDIDATE,
    DECISION_RETIRE_CANDIDATE,
    DECISION_ARCHIVE_READY,
    DECISION_INSUFFICIENT_EVIDENCE,
)

#: 动作候选 → 目标 lifecycle state 的**固定映射**。合法性由 lifecycle owner 判定
#: （调用方传入它自己的 `TRANSITION_TABLE`），本模块不 import strategy_lifecycle。
CANDIDATE_TARGET_STATES = {
    DECISION_DEGRADE_CANDIDATE: "degraded",
    DECISION_RETIRE_CANDIDATE: "retiring",
    DECISION_ARCHIVE_READY: "archived",
}

#: 证据完整闸门认作「够用」的状态：AVAILABLE 是证据，NOT_APPLICABLE 是 owner 明确
#: 说过「这条事实不适用」。PARTIAL / UNAVAILABLE 一律不够。
SUFFICIENT_STATUSES = frozenset({SH.STATUS_AVAILABLE, SH.STATUS_NOT_APPLICABLE})

REASON_SNAPSHOT_REQUIRED = "canonical_health_snapshot_required"
REASON_SNAPSHOT_IDENTITY_MISMATCH = "health_snapshot_identity_mismatch"
REASON_CONDITIONS_UNSUPPORTED = "retirement_conditions_unsupported_in_policy_version"


class RetirementPolicyError(ValueError):
    """Stable rejection from the retirement policy contract."""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class StrategyRetirementDecision:
    """Immutable policy recommendation. It recommends; it never mutates."""

    decision_id: str
    decision_fingerprint: str
    snapshot_id: str
    snapshot_fingerprint: str
    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    decision: str
    policy_version: str
    evidence_summary: Mapping
    blocking_reasons: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    satisfied_evidence: tuple[str, ...] = ()
    schema_version: str = RETIREMENT_SCHEMA_VERSION

    def projection(self) -> dict:
        """Exactly the material the fingerprint covers (no persistence metadata)."""
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "decision_fingerprint": self.decision_fingerprint,
            "policy_version": self.policy_version,
            "snapshot_id": self.snapshot_id,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_checksum": self.strategy_checksum,
            "decision": self.decision,
            "evidence_summary": _thaw(self.evidence_summary),
            "blocking_reasons": list(self.blocking_reasons),
            "required_evidence": list(self.required_evidence),
            "satisfied_evidence": list(self.satisfied_evidence),
        }

    def fingerprint_material(self) -> dict:
        material = self.projection()
        material.pop("decision_id", None)
        material.pop("decision_fingerprint", None)
        return material


def evaluate_retirement_conditions(snapshot: SH.StrategyHealthSnapshot) -> tuple[str, ...]:
    """Reserved interface for threshold-based retirement conditions.

    v1 deliberately emits **nothing**: the repository has no owner for a
    version-scoped performance metric, and none of the candidate thresholds
    (drawdown, loss duration, execution failure rate, risk violation frequency)
    exists as a formal owner rule. Inventing one here would turn "unknown" into a
    verdict, which R33-A/R33-B both forbid.

    A future policy version implements this function and thereby starts emitting
    action candidates; the decision vocabulary already exists for that.
    """
    if not isinstance(snapshot, SH.StrategyHealthSnapshot):
        raise RetirementPolicyError(REASON_SNAPSHOT_REQUIRED)
    return ()


def _evidence_summary(snapshot: SH.StrategyHealthSnapshot) -> dict:
    """Replay exactly what the policy looked at. It adds no new fact."""
    return {
        "lifecycle_state_at_capture": snapshot.lifecycle_state,
        "coverage": snapshot.coverage.projection(),
        "dimensions": {
            item.name: {"status": item.status, "provenance": item.provenance,
                        "source_fingerprint": item.source_fingerprint,
                        "blocking_reasons": list(item.blocking_reasons)}
            for item in snapshot.dimensions
        },
        "snapshot_blocking_reasons": list(snapshot.blocking_reasons),
    }


def _decision_for_conditions(conditions) -> str:
    """Extension point for a future policy version.

    v1 defines no condition vocabulary, so a non-empty conditions tuple means the
    caller is running a policy contract this version does not implement. Failing
    closed is the only safe answer: never map an unknown condition onto an action.
    """
    if conditions:
        raise RetirementPolicyError(REASON_CONDITIONS_UNSUPPORTED)
    return DECISION_NO_ACTION


def evaluate_retirement_policy(snapshot: SH.StrategyHealthSnapshot, *,
                               policy_version: str = RETIREMENT_POLICY_VERSION
                               ) -> StrategyRetirementDecision:
    """Turn one immutable health snapshot into one deterministic recommendation.

    Rule 1 gates everything: if any dimension is PARTIAL or UNAVAILABLE, the
    answer is INSUFFICIENT_EVIDENCE — never an action. "We do not know" is not
    "it failed", and UNKNOWN/PARTIAL must never trigger a retirement.
    """
    if not isinstance(snapshot, SH.StrategyHealthSnapshot):
        raise RetirementPolicyError(REASON_SNAPSHOT_REQUIRED)
    if not SH.verify_snapshot_fingerprint(snapshot):
        # 被篡改或损坏的快照不得作为决策输入。
        raise RetirementPolicyError(REASON_SNAPSHOT_IDENTITY_MISMATCH)

    required: list[str] = []
    satisfied: list[str] = []
    blocking: list[str] = []
    for item in snapshot.dimensions:
        required.append(item.name)
        if item.status in SUFFICIENT_STATUSES:
            satisfied.append(item.name)
            continue
        suffix = "partial" if item.status == SH.STATUS_PARTIAL else "unavailable"
        blocking.append(f"health_dimension_{suffix}:{item.name}")

    if blocking:
        decision = DECISION_INSUFFICIENT_EVIDENCE
    else:
        decision = _decision_for_conditions(evaluate_retirement_conditions(snapshot))

    summary = _evidence_summary(snapshot)
    material = {
        "schema_version": RETIREMENT_SCHEMA_VERSION,
        "policy_version": policy_version,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_fingerprint": snapshot.snapshot_fingerprint,
        "strategy_id": snapshot.strategy_id,
        "strategy_version": int(snapshot.strategy_version),
        "strategy_checksum": snapshot.strategy_checksum,
        "decision": decision,
        "evidence_summary": summary,
        "blocking_reasons": sorted(blocking),
        "required_evidence": sorted(required),
        "satisfied_evidence": sorted(satisfied),
    }
    fingerprint = _sha(material)
    return StrategyRetirementDecision(
        decision_id=fingerprint, decision_fingerprint=fingerprint,
        snapshot_id=snapshot.snapshot_id, snapshot_fingerprint=snapshot.snapshot_fingerprint,
        strategy_id=snapshot.strategy_id, strategy_version=int(snapshot.strategy_version),
        strategy_checksum=snapshot.strategy_checksum, decision=decision,
        policy_version=policy_version, evidence_summary=MappingProxyType(summary),
        blocking_reasons=tuple(sorted(blocking)), required_evidence=tuple(sorted(required)),
        satisfied_evidence=tuple(sorted(satisfied)),
    )


def verify_decision_fingerprint(decision: StrategyRetirementDecision) -> bool:
    """Re-derive a decision's own fingerprint. Stored evidence must self-verify."""
    if not isinstance(decision, StrategyRetirementDecision):
        raise RetirementPolicyError("canonical_retirement_decision_required")
    if decision.decision_id != decision.decision_fingerprint:
        return False
    return _sha(decision.fingerprint_material()) == decision.decision_fingerprint


def build_transition_proposal(decision: StrategyRetirementDecision, *,
                              lifecycle_state: str | None,
                              legal_targets) -> dict | None:
    """Describe the lifecycle edge this decision *would* ask for. Never executes it.

    ``legal_targets`` is the lifecycle owner's own ``TRANSITION_TABLE`` entry for
    the captured state, so legality is decided by that authority — this module
    stays free of any lifecycle dependency and never calls ``transition``.
    """
    if not isinstance(decision, StrategyRetirementDecision):
        raise RetirementPolicyError("canonical_retirement_decision_required")
    target = CANDIDATE_TARGET_STATES.get(decision.decision)
    if target is None:
        # NO_ACTION / INSUFFICIENT_EVIDENCE 不产生任何提案。
        return None
    if target not in set(legal_targets or ()):
        return None
    material = {"policy_version": decision.policy_version,
                "decision_id": decision.decision_id,
                "snapshot_fingerprint": decision.snapshot_fingerprint,
                "strategy_id": decision.strategy_id,
                "strategy_version": int(decision.strategy_version),
                "strategy_checksum": decision.strategy_checksum,
                "lifecycle_state_at_capture": lifecycle_state,
                "target_state": target}
    return {**material, "proposal_id": _sha(material), "executed": False,
            "reason": f"retirement_policy:{decision.decision}"}


__all__ = [
    "RETIREMENT_POLICY_VERSION", "RETIREMENT_SCHEMA_VERSION", "DECISIONS",
    "CANDIDATE_TARGET_STATES", "SUFFICIENT_STATUSES", "RetirementPolicyError",
    "StrategyRetirementDecision", "evaluate_retirement_policy",
    "evaluate_retirement_conditions", "verify_decision_fingerprint",
    "build_transition_proposal",
]
