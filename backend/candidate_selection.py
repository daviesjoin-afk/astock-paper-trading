"""R36-C pure candidate selection contract: gates, Pareto fronts and report identity.

This module owns the **selection policy** and the **search-local selection decision**.
It is a pure domain authority:

* NO DB, NO network, NO filesystem, NO clock, NO strategy registry;
* NO current/latest lookup, NO lifecycle, NO promotion, NO AI.

It consumes canonical ``CandidateSelectionEvidence`` (already extracted from exact R29 /
R30 owners by :mod:`candidate_selection_service`) and produces a canonical
``CandidateSelectionReport``. It never manufactures or reinterprets R29/R30 facts.

An ``advance`` disposition means only that R36-D may consume the candidate as a
next-generation parent input. It never means promoted / validated / shadow / paper /
production / champion / winner / best strategy.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

SELECTION_POLICY_VERSION = "candidate-selection-policy-v1"
SELECTION_REPORT_VERSION = "candidate-selection-report-v1"

MAXIMIZE = "maximize"
MINIMIZE = "minimize"

#: Closed objective vocabulary with **fixed** directions. Callers cannot invert them.
OBJECTIVE_DIRECTIONS = MappingProxyType({
    "baseline_return": MAXIMIZE,
    "baseline_drawdown_abs": MINIMIZE,
    "baseline_turnover": MINIMIZE,
    "robustness_worst_return": MAXIMIZE,
    "robustness_worst_drawdown_abs": MINIMIZE,
    "robustness_max_return_degradation": MINIMIZE,
    "robustness_fragility_count": MINIMIZE,
})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_METRIC_KEYS = ("return", "drawdown", "volatility", "turnover", "trade_count", "cost",
                "exposure", "capacity_proxy", "data_coverage")


class SelectionContractError(ValueError):
    """Stable rejection from the selection contract (always fail closed)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _finite(value: Any, *, name: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SelectionContractError(f"{name}_must_be_a_finite_number")
    if not math.isfinite(float(value)):
        raise SelectionContractError(f"{name}_must_be_a_finite_number")
    return value


def _optional_number(value: Any, *, name: str, minimum: float | None = None,
                     maximum: float | None = None) -> float | int | None:
    if value is None:
        return None
    number = _finite(value, name=name)
    if minimum is not None and number < minimum:
        raise SelectionContractError(f"{name}_below_allowed_range")
    if maximum is not None and number > maximum:
        raise SelectionContractError(f"{name}_above_allowed_range")
    return number


def _optional_int(value: Any, *, name: str, minimum: int = 0) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SelectionContractError(f"{name}_must_be_an_integer")
    return value


def _required_int(value: Any, *, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SelectionContractError(f"{name}_must_be_an_integer")
    return value


@dataclass(frozen=True, slots=True)
class CandidateSelectionPolicy:
    """Explicit, immutable, canonically fingerprinted selection policy.

    ``None`` on a gate means **that gate is disabled** — an explicit contract semantic,
    never a hidden default. Every business field is supplied by the caller; nothing is
    defaulted inside a service.
    """

    objectives: tuple[str, ...]
    policy_version: str = SELECTION_POLICY_VERSION
    min_baseline_return: float | int | None = None
    max_baseline_drawdown_abs: float | int | None = None
    min_trade_count: int | None = None
    min_data_coverage: float | int | None = None
    require_no_robustness_unavailable: bool = False
    require_no_robustness_failed: bool = False
    require_no_threshold_breaches: bool = False
    max_observed_fragilities: int | None = None
    advance_through_front: int = 1
    retain_through_front: int = 1

    def __post_init__(self):
        if self.policy_version != SELECTION_POLICY_VERSION:
            raise SelectionContractError("unsupported_selection_policy_version")
        if isinstance(self.objectives, (str, bytes)) or not hasattr(self.objectives, "__iter__"):
            raise SelectionContractError("objectives_must_be_a_sequence")
        raw = [str(item) for item in self.objectives]
        if not raw:
            raise SelectionContractError("selection_objectives_required")
        unknown = sorted(set(raw) - set(OBJECTIVE_DIRECTIONS))
        if unknown:
            raise SelectionContractError("unsupported_selection_objective")
        if len(set(raw)) != len(raw):
            raise SelectionContractError("duplicate_selection_objective")
        # Objective order carries no semantics: canonicalise to sorted order.
        object.__setattr__(self, "objectives", tuple(sorted(raw)))
        for name in ("require_no_robustness_unavailable", "require_no_robustness_failed",
                     "require_no_threshold_breaches"):
            if not isinstance(getattr(self, name), bool):
                raise SelectionContractError(f"{name}_must_be_a_boolean")
        object.__setattr__(self, "min_baseline_return",
                           _optional_number(self.min_baseline_return,
                                            name="min_baseline_return"))
        object.__setattr__(self, "max_baseline_drawdown_abs",
                           _optional_number(self.max_baseline_drawdown_abs,
                                            name="max_baseline_drawdown_abs", minimum=0))
        object.__setattr__(self, "min_trade_count",
                           _optional_int(self.min_trade_count, name="min_trade_count"))
        object.__setattr__(self, "min_data_coverage",
                           _optional_number(self.min_data_coverage,
                                            name="min_data_coverage", minimum=0, maximum=1))
        object.__setattr__(self, "max_observed_fragilities",
                           _optional_int(self.max_observed_fragilities,
                                         name="max_observed_fragilities"))
        advance = _required_int(self.advance_through_front, name="advance_through_front",
                                minimum=1)
        retain = _required_int(self.retain_through_front, name="retain_through_front",
                               minimum=1)
        if retain < advance:
            raise SelectionContractError("retain_through_front_below_advance_through_front")
        object.__setattr__(self, "advance_through_front", advance)
        object.__setattr__(self, "retain_through_front", retain)

    def projection(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "objectives": list(self.objectives),
            "min_baseline_return": self.min_baseline_return,
            "max_baseline_drawdown_abs": self.max_baseline_drawdown_abs,
            "min_trade_count": self.min_trade_count,
            "min_data_coverage": self.min_data_coverage,
            "require_no_robustness_unavailable": self.require_no_robustness_unavailable,
            "require_no_robustness_failed": self.require_no_robustness_failed,
            "require_no_threshold_breaches": self.require_no_threshold_breaches,
            "max_observed_fragilities": self.max_observed_fragilities,
            "advance_through_front": self.advance_through_front,
            "retain_through_front": self.retain_through_front,
        }

    @property
    def fingerprint(self) -> str:
        return _sha(self.projection())

    @classmethod
    def from_projection(cls, value: Mapping[str, Any]) -> "CandidateSelectionPolicy":
        if not isinstance(value, Mapping):
            raise SelectionContractError("selection_policy_projection_invalid")
        try:
            args = dict(value)
            args.pop("policy_version", None)
            policy = cls(policy_version=value.get("policy_version"), **args)
        except (TypeError, ValueError, KeyError) as exc:
            raise SelectionContractError("selection_policy_projection_invalid") from exc
        if policy.projection() != dict(value):
            raise SelectionContractError("selection_policy_projection_invalid")
        return policy


@dataclass(frozen=True, slots=True)
class CandidateSelectionEvidence:
    """One candidate's exact R29 + R30 selection inputs.

    ``robustness_*`` references are ``None`` for a candidate whose canonical R29 evidence
    is not READY (blocked / result not completed). Such a candidate is still bound to its
    exact R29 evidence and may be reported as ineligible.
    """

    candidate_id: str
    pit_run_key: str
    pit_result_fingerprint: str
    pit_validation_status: str
    pit_result_status: str
    robustness_report_key: str | None = None
    robustness_report_fingerprint: str | None = None
    baseline_return: float | int | None = None
    baseline_drawdown_abs: float | int | None = None
    baseline_turnover: float | int | None = None
    baseline_trade_count: int | None = None
    baseline_data_coverage: float | int | None = None
    robustness_worst_return: float | int | None = None
    robustness_worst_drawdown_abs: float | int | None = None
    robustness_max_return_degradation: float | int | None = None
    robustness_unavailable_count: int | None = None
    robustness_failed_count: int | None = None
    robustness_threshold_breach_count: int | None = None
    robustness_fragility_count: int | None = None

    def __post_init__(self):
        for name in ("candidate_id", "pit_run_key", "pit_result_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise SelectionContractError(f"{name}_must_be_a_sha256_digest")
        if self.pit_validation_status not in ("ready", "blocked"):
            raise SelectionContractError("pit_validation_status_invalid")
        if self.pit_result_status not in ("completed", "failed", "unavailable"):
            raise SelectionContractError("pit_result_status_invalid")
        if (self.robustness_report_key is None) != (self.robustness_report_fingerprint is None):
            raise SelectionContractError("robustness_reference_incomplete")
        for name in ("robustness_report_key", "robustness_report_fingerprint"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not _SHA256.fullmatch(value)):
                raise SelectionContractError(f"{name}_must_be_a_sha256_digest")
        for name in ("baseline_return", "baseline_drawdown_abs", "baseline_turnover",
                     "baseline_data_coverage", "robustness_worst_return",
                     "robustness_worst_drawdown_abs", "robustness_max_return_degradation"):
            object.__setattr__(self, name, _optional_number(getattr(self, name), name=name))
        object.__setattr__(self, "baseline_trade_count",
                           _optional_int(self.baseline_trade_count, name="baseline_trade_count"))
        for name in ("robustness_unavailable_count", "robustness_failed_count",
                     "robustness_threshold_breach_count", "robustness_fragility_count"):
            object.__setattr__(self, name, _optional_int(getattr(self, name), name=name))

    @property
    def has_ready_baseline(self) -> bool:
        return self.pit_validation_status == "ready" and self.pit_result_status == "completed"

    def evidence_binding_projection(self) -> dict[str, Any]:
        """Canonical material for the evidence-set fingerprint (no derived metrics)."""
        return {
            "candidate_id": self.candidate_id,
            "pit_run_key": self.pit_run_key,
            "pit_result_fingerprint": self.pit_result_fingerprint,
            "pit_validation_status": self.pit_validation_status,
            "pit_result_status": self.pit_result_status,
            "robustness_report_key": self.robustness_report_key,
            "robustness_report_fingerprint": self.robustness_report_fingerprint,
        }

    def feature_projection(self) -> dict[str, Any]:
        return {
            "baseline_return": self.baseline_return,
            "baseline_drawdown_abs": self.baseline_drawdown_abs,
            "baseline_turnover": self.baseline_turnover,
            "baseline_trade_count": self.baseline_trade_count,
            "baseline_data_coverage": self.baseline_data_coverage,
            "robustness_worst_return": self.robustness_worst_return,
            "robustness_worst_drawdown_abs": self.robustness_worst_drawdown_abs,
            "robustness_max_return_degradation": self.robustness_max_return_degradation,
            "robustness_unavailable_count": self.robustness_unavailable_count,
            "robustness_failed_count": self.robustness_failed_count,
            "robustness_threshold_breach_count": self.robustness_threshold_breach_count,
            "robustness_fragility_count": self.robustness_fragility_count,
        }

    def objective_value(self, objective: str) -> float | int | None:
        if objective not in OBJECTIVE_DIRECTIONS:
            raise SelectionContractError("unsupported_selection_objective")
        return getattr(self, objective)


def _gate_eligibility(policy: CandidateSelectionPolicy,
                      evidence: CandidateSelectionEvidence) -> list[str]:
    """Return stable blocking reasons; an empty list means eligible."""
    if not evidence.has_ready_baseline:
        reasons = []
        if evidence.pit_validation_status != "ready":
            reasons.append("pit_validation_not_ready")
        if evidence.pit_result_status != "completed":
            reasons.append("pit_result_not_completed")
        return reasons
    reasons = []
    checks = (
        ("min_baseline_return", "baseline_return", "baseline_return_below_floor",
         lambda value, limit: value < limit),
        ("max_baseline_drawdown_abs", "baseline_drawdown_abs",
         "baseline_drawdown_above_limit", lambda value, limit: value > limit),
        ("min_trade_count", "baseline_trade_count", "baseline_trade_count_below_floor",
         lambda value, limit: value < limit),
        ("min_data_coverage", "baseline_data_coverage", "baseline_data_coverage_below_floor",
         lambda value, limit: value < limit),
    )
    for limit_name, feature_name, reason, compare in checks:
        limit = getattr(policy, limit_name)
        if limit is None:
            continue
        value = getattr(evidence, feature_name)
        if value is None:
            reasons.append("selection_metric_unavailable")
            continue
        if compare(value, limit):
            reasons.append(reason)
    # A missing count is never a neutral zero: an enabled robustness gate on an
    # unavailable count fails closed with a stable reason.
    for enabled, feature_name, reason in (
            (policy.require_no_robustness_unavailable, "robustness_unavailable_count",
             "robustness_unavailable_cases_present"),
            (policy.require_no_robustness_failed, "robustness_failed_count",
             "robustness_failed_cases_present"),
            (policy.require_no_threshold_breaches, "robustness_threshold_breach_count",
             "robustness_threshold_breaches_present")):
        if not enabled:
            continue
        count = getattr(evidence, feature_name)
        if count is None:
            reasons.append("selection_metric_unavailable")
        elif count > 0:
            reasons.append(reason)
    if policy.max_observed_fragilities is not None:
        count = evidence.robustness_fragility_count
        if count is None:
            reasons.append("selection_metric_unavailable")
        elif count > policy.max_observed_fragilities:
            reasons.append("robustness_fragility_count_above_limit")
    # Objectives must be resolvable on the canonical evidence; a missing objective
    # value is an explicit unavailable state, never a neutral zero.
    for objective in policy.objectives:
        if evidence.objective_value(objective) is None:
            reasons.append("selection_objective_unavailable")
    return sorted(set(reasons))


def _dominates(policy: CandidateSelectionPolicy,
               first: CandidateSelectionEvidence,
               second: CandidateSelectionEvidence) -> bool:
    """Pareto dominance with fixed directions: no worse everywhere, better somewhere."""
    strictly_better = False
    for objective in policy.objectives:
        direction = OBJECTIVE_DIRECTIONS[objective]
        left = first.objective_value(objective)
        right = second.objective_value(objective)
        if left is None or right is None:
            # Unresolvable objectives never enter the Pareto set (gated out upstream).
            return False
        if direction == MAXIMIZE:
            if left < right:
                return False
            if left > right:
                strictly_better = True
        else:
            if left > right:
                return False
            if left < right:
                strictly_better = True
    return strictly_better


def _pareto_fronts(policy: CandidateSelectionPolicy,
                   eligible: list[CandidateSelectionEvidence]) -> dict[str, int]:
    """Deterministic non-dominated sorting. Fronts are atomic; order is irrelevant."""
    remaining = sorted(eligible, key=lambda item: item.candidate_id)
    fronts: dict[str, int] = {}
    level = 1
    while remaining:
        front = [candidate for candidate in remaining
                 if not any(_dominates(policy, other, candidate)
                            for other in remaining if other is not candidate)]
        if not front:  # pragma: no cover - defensive: dominance is a strict partial order
            raise SelectionContractError("selection_pareto_front_unresolved")
        for candidate in front:
            fronts[candidate.candidate_id] = level
        remaining = [candidate for candidate in remaining if candidate not in front]
        level += 1
    return fronts


def _disposition(policy: CandidateSelectionPolicy, front: int) -> tuple[str, bool]:
    if front <= policy.advance_through_front:
        return "advance", True
    if front <= policy.retain_through_front:
        return "retain", False
    return "eliminate", False


def evaluate_selection(*, search_run_id: str, search_input_fingerprint: str,
                       policy: CandidateSelectionPolicy,
                       evidence: list[CandidateSelectionEvidence]) -> dict[str, Any]:
    """Pure selection: gates -> Pareto fronts -> dispositions -> canonical report."""
    if not isinstance(policy, CandidateSelectionPolicy):
        raise SelectionContractError("canonical_selection_policy_required")
    if not isinstance(search_run_id, str) or not _SHA256.fullmatch(search_run_id):
        raise SelectionContractError("search_run_id_must_be_a_sha256_digest")
    if not isinstance(search_input_fingerprint, str) or not _SHA256.fullmatch(
            search_input_fingerprint):
        raise SelectionContractError("search_input_fingerprint_must_be_a_sha256_digest")
    rows = list(evidence)
    if any(not isinstance(item, CandidateSelectionEvidence) for item in rows):
        raise SelectionContractError("canonical_selection_evidence_required")
    ids = [item.candidate_id for item in rows]
    if len(set(ids)) != len(ids):
        raise SelectionContractError("duplicate_selection_evidence")
    rows.sort(key=lambda item: item.candidate_id)

    eligibility: dict[str, list[str]] = {}
    for item in rows:
        eligibility[item.candidate_id] = _gate_eligibility(policy, item)
    eligible = [item for item in rows if not eligibility[item.candidate_id]]
    fronts = _pareto_fronts(policy, eligible)

    candidates = []
    for item in rows:
        blocking = eligibility[item.candidate_id]
        if blocking:
            front = None
            disposition = "eliminate"
            next_generation_eligible = False
        else:
            front = fronts[item.candidate_id]
            disposition, next_generation_eligible = _disposition(policy, front)
        candidates.append({
            "candidate_id": item.candidate_id,
            "pit_run_key": item.pit_run_key,
            "pit_result_fingerprint": item.pit_result_fingerprint,
            "robustness_report_key": item.robustness_report_key,
            "robustness_report_fingerprint": item.robustness_report_fingerprint,
            "eligibility": not blocking,
            "blocking_reasons": blocking,
            "selection_features": item.feature_projection(),
            "pareto_front": front,
            "disposition": disposition,
            "next_generation_eligible": next_generation_eligible,
        })
    evidence_set_fingerprint = _sha([item.evidence_binding_projection() for item in rows])
    report = {
        "report_version": SELECTION_REPORT_VERSION,
        "search_run_id": search_run_id,
        "search_input_fingerprint": search_input_fingerprint,
        "selection_policy": policy.projection(),
        "selection_policy_fingerprint": policy.fingerprint,
        "evidence_set_fingerprint": evidence_set_fingerprint,
        "candidate_count": len(rows),
        "eligible_count": sum(1 for item in candidates if item["eligibility"]),
        "advance_count": sum(1 for item in candidates if item["disposition"] == "advance"),
        "retain_count": sum(1 for item in candidates if item["disposition"] == "retain"),
        "eliminate_count": sum(1 for item in candidates if item["disposition"] == "eliminate"),
        "candidates": candidates,
    }
    report["selection_report_key"] = selection_report_key(
        report_version=report["report_version"], search_run_id=search_run_id,
        search_input_fingerprint=search_input_fingerprint,
        selection_policy_fingerprint=policy.fingerprint,
        evidence_set_fingerprint=evidence_set_fingerprint)
    report["report_fingerprint"] = _sha({key: value for key, value in report.items()
                                         if key != "report_fingerprint"})
    return report


def selection_report_key(*, report_version: str, search_run_id: str,
                         search_input_fingerprint: str, selection_policy_fingerprint: str,
                         evidence_set_fingerprint: str) -> str:
    return _sha({"report_version": report_version, "search_run_id": search_run_id,
                 "search_input_fingerprint": search_input_fingerprint,
                 "selection_policy_fingerprint": selection_policy_fingerprint,
                 "evidence_set_fingerprint": evidence_set_fingerprint})


def selection_evidence_from_projection(value: Mapping[str, Any]) -> CandidateSelectionEvidence:
    if not isinstance(value, Mapping):
        raise SelectionContractError("selection_evidence_projection_invalid")
    try:
        return CandidateSelectionEvidence(**dict(value))
    except (TypeError, ValueError, KeyError) as exc:
        raise SelectionContractError("selection_evidence_projection_invalid") from exc


__all__ = [
    "MAXIMIZE",
    "MINIMIZE",
    "OBJECTIVE_DIRECTIONS",
    "SELECTION_POLICY_VERSION",
    "SELECTION_REPORT_VERSION",
    "CandidateSelectionEvidence",
    "CandidateSelectionPolicy",
    "SelectionContractError",
    "evaluate_selection",
    "selection_evidence_from_projection",
    "selection_report_key",
]
