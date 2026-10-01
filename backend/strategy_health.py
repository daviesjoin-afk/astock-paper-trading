# -*- coding: utf-8 -*-
"""Exact strategy health evidence（R33-A）.

这个模块是**事实 authority**，不是 policy：它回答

    策略 X 的 exact version V，在显式 observation window 下，我们实际拥有哪些健康事实？

它**不**回答「该不该退休 / 降级 / 暂停 / 归档」，也**不**写 lifecycle。

纯 builder 契约（:func:`build_strategy_health`）：

- 无 DB、无 provider、无网络、无机器时钟、无 current/latest 查询；
- 只消费调用方**已捕获**的 immutable inputs；
- 同一组输入必须得到同一个 ``snapshot_fingerprint``。

维度语义是**证据状态**，不是业务结论：``AVAILABLE`` / ``PARTIAL`` / ``UNAVAILABLE`` /
``NOT_APPLICABLE``。缺证据既不是「健康」也不是「不健康」，绝不被折算成 0/false。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

#: 快照契约版本。维度语义或指纹材料变化时必须递增。
HEALTH_CONTRACT_VERSION = "strategy-health-contract-v1"
HEALTH_SCHEMA_VERSION = "strategy-health-snapshot-v1"

DIMENSION_RUNTIME_INTEGRITY = "runtime_integrity"
DIMENSION_LIFECYCLE_INTEGRITY = "lifecycle_integrity"
DIMENSION_EXECUTION_EVIDENCE = "execution_evidence"
DIMENSION_RISK_EVIDENCE = "risk_evidence"
DIMENSION_ACTIVITY_COVERAGE = "activity_coverage"
DIMENSION_PERFORMANCE = "performance"
DIMENSION_COMPARABLE_EVIDENCE = "comparable_evidence"

#: 每个快照都必须携带**全部**维度：coverage 的分母因此是固定的，
#: 不允许「少采一个维度」把分母悄悄改小。
DIMENSIONS = (
    DIMENSION_RUNTIME_INTEGRITY,
    DIMENSION_LIFECYCLE_INTEGRITY,
    DIMENSION_EXECUTION_EVIDENCE,
    DIMENSION_RISK_EVIDENCE,
    DIMENSION_ACTIVITY_COVERAGE,
    DIMENSION_PERFORMANCE,
    DIMENSION_COMPARABLE_EVIDENCE,
)

STATUS_AVAILABLE = "AVAILABLE"
STATUS_PARTIAL = "PARTIAL"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
DIMENSION_STATUSES = (STATUS_AVAILABLE, STATUS_PARTIAL, STATUS_UNAVAILABLE,
                      STATUS_NOT_APPLICABLE)

PROVENANCE_OWNER_ISSUED = "OWNER_ISSUED"
PROVENANCE_CAPTURED_INPUT = "CAPTURED_INPUT"
PROVENANCE_DERIVED = "DERIVED"
PROVENANCE_UNAVAILABLE = "UNAVAILABLE"
PROVENANCES = (PROVENANCE_OWNER_ISSUED, PROVENANCE_CAPTURED_INPUT,
               PROVENANCE_DERIVED, PROVENANCE_UNAVAILABLE)

#: 维度不可用时的稳定 reason（不解析自然语言，机器可读）。
REASON_STRATEGY_PERFORMANCE_OWNER_UNAVAILABLE = "strategy_performance_owner_unavailable"
REASON_SIGNAL_ATTRIBUTION_UNAVAILABLE = "signal_strategy_attribution_unavailable"
REASON_EXACT_VERSION_NOT_PERSISTED = "exact_strategy_version_not_persisted"
REASON_LIFECYCLE_STATE_UNKNOWN = "exact_version_lifecycle_state_unknown"
REASON_COMPARISON_REPORT_NOT_SPECIFIED = "exact_comparison_report_not_specified"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HealthEvidenceError(ValueError):
    """Stable rejection from the health-evidence contract or its ledger."""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise HealthEvidenceError("health_fact_not_serializable")


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _iso_day(value, label: str) -> str:
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise HealthEvidenceError(f"{label}_must_be_iso_date") from exc


@dataclass(frozen=True, slots=True)
class HealthObservationWindow:
    """The explicit window a snapshot covers. Never implicit, never "now"."""

    observation_start: str
    observation_end: str

    def __post_init__(self):
        start = _iso_day(self.observation_start, "observation_start")
        end = _iso_day(self.observation_end, "observation_end")
        if start >= end:
            raise HealthEvidenceError("observation_window_must_be_forward")
        object.__setattr__(self, "observation_start", start)
        object.__setattr__(self, "observation_end", end)

    @property
    def identity(self) -> str:
        return _sha({"observation_start": self.observation_start,
                     "observation_end": self.observation_end})

    def projection(self) -> dict:
        return {"observation_start": self.observation_start,
                "observation_end": self.observation_end,
                "window_identity": self.identity}


@dataclass(frozen=True, slots=True)
class HealthDimension:
    """One dimension's evidence state. A status, never a healthy/unhealthy verdict."""

    name: str
    status: str
    facts: Mapping
    provenance: str
    source_identity: str | None = None
    source_fingerprint: str | None = None
    blocking_reasons: tuple[str, ...] = ()

    def __post_init__(self):
        if self.name not in DIMENSIONS:
            raise HealthEvidenceError(f"unknown_health_dimension:{self.name}")
        if self.status not in DIMENSION_STATUSES:
            raise HealthEvidenceError(f"unknown_health_dimension_status:{self.status}")
        if self.provenance not in PROVENANCES:
            raise HealthEvidenceError(f"unknown_health_provenance:{self.provenance}")
        facts = self.facts if isinstance(self.facts, Mapping) else {}
        if not isinstance(self.facts, Mapping):
            raise HealthEvidenceError("health_dimension_facts_must_be_mapping")
        reasons = tuple(str(item) for item in (self.blocking_reasons or ()) if str(item or ""))
        if self.status == STATUS_UNAVAILABLE and self.provenance != PROVENANCE_UNAVAILABLE:
            # No owner fact ⇒ no provenance may be claimed.
            raise HealthEvidenceError("unavailable_dimension_requires_unavailable_provenance")
        if self.status in (STATUS_AVAILABLE, STATUS_PARTIAL) and (
                self.provenance == PROVENANCE_UNAVAILABLE):
            raise HealthEvidenceError("available_dimension_requires_a_provenance")
        if self.status in (STATUS_UNAVAILABLE, STATUS_NOT_APPLICABLE) and not reasons:
            # Both "we could not evidence this" and "this does not apply" must say
            # why: silence is never evidence.
            raise HealthEvidenceError("unavailable_dimension_requires_a_reason")
        if self.status == STATUS_NOT_APPLICABLE and facts:
            raise HealthEvidenceError("not_applicable_dimension_carries_no_facts")
        if self.source_fingerprint is not None and not _SHA256.fullmatch(
                str(self.source_fingerprint)):
            raise HealthEvidenceError("health_source_fingerprint_invalid")
        object.__setattr__(self, "facts", _freeze(facts))
        object.__setattr__(self, "blocking_reasons", reasons)
        object.__setattr__(self, "source_identity",
                           None if self.source_identity is None else str(self.source_identity))
        object.__setattr__(self, "source_fingerprint",
                           None if self.source_fingerprint is None else str(self.source_fingerprint))

    def projection(self) -> dict:
        return {"name": self.name, "status": self.status, "facts": _thaw(self.facts),
                "provenance": self.provenance, "source_identity": self.source_identity,
                "source_fingerprint": self.source_fingerprint,
                "blocking_reasons": list(self.blocking_reasons)}


@dataclass(frozen=True, slots=True)
class HealthCoverage:
    """Evidence coverage. The denominator is always the full dimension set."""

    expected_dimensions: int
    available_dimensions: int
    partial_dimensions: int
    unavailable_dimensions: int
    not_applicable_dimensions: int
    coverage_ratio: float

    def __post_init__(self):
        counts = {
            "expected": int(self.expected_dimensions),
            "available": int(self.available_dimensions),
            "partial": int(self.partial_dimensions),
            "unavailable": int(self.unavailable_dimensions),
            "not_applicable": int(self.not_applicable_dimensions),
        }
        if counts["expected"] != len(DIMENSIONS):
            raise HealthEvidenceError("health_coverage_denominator_must_be_all_dimensions")
        if (counts["available"] + counts["partial"] + counts["unavailable"]
                + counts["not_applicable"]) != counts["expected"]:
            raise HealthEvidenceError("health_coverage_buckets_must_partition_dimensions")
        # Only AVAILABLE counts as covered evidence: PARTIAL is not coverage, and
        # a NOT_APPLICABLE dimension is not evidence either.
        expected_ratio = round(counts["available"] / counts["expected"], 6)
        if round(float(self.coverage_ratio), 6) != expected_ratio:
            raise HealthEvidenceError("health_coverage_ratio_must_equal_available_over_expected")
        object.__setattr__(self, "coverage_ratio", expected_ratio)

    def projection(self) -> dict:
        return {"expected_dimensions": self.expected_dimensions,
                "available_dimensions": self.available_dimensions,
                "partial_dimensions": self.partial_dimensions,
                "unavailable_dimensions": self.unavailable_dimensions,
                "not_applicable_dimensions": self.not_applicable_dimensions,
                "coverage_ratio": self.coverage_ratio}


@dataclass(frozen=True, slots=True)
class StrategyHealthSnapshot:
    """Immutable, deterministic health evidence for one exact strategy version."""

    snapshot_id: str
    snapshot_fingerprint: str
    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    observation_start: str
    observation_end: str
    window_identity: str
    lifecycle_state: str | None
    dimensions: tuple[HealthDimension, ...]
    coverage: HealthCoverage
    source_identities: Mapping
    blocking_reasons: tuple[str, ...] = ()
    health_contract_version: str = HEALTH_CONTRACT_VERSION
    schema_version: str = HEALTH_SCHEMA_VERSION

    def dimension(self, name: str) -> HealthDimension | None:
        for item in self.dimensions:
            if item.name == name:
                return item
        return None

    def projection(self) -> dict:
        """Exactly the material the fingerprint covers (no persistence metadata)."""
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "health_contract_version": self.health_contract_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_checksum": self.strategy_checksum,
            "observation_start": self.observation_start,
            "observation_end": self.observation_end,
            "window_identity": self.window_identity,
            "lifecycle_state": self.lifecycle_state,
            "dimensions": [item.projection() for item in self.dimensions],
            "coverage": self.coverage.projection(),
            "source_identities": _thaw(self.source_identities),
            "blocking_reasons": list(self.blocking_reasons),
        }

    def fingerprint_material(self) -> dict:
        material = self.projection()
        material.pop("snapshot_id", None)
        material.pop("snapshot_fingerprint", None)
        return material


def build_strategy_health(*, strategy_id: str, strategy_version: int,
                          strategy_checksum: str,
                          observation_window: HealthObservationWindow,
                          lifecycle_state: str | None,
                          dimensions: Iterable[HealthDimension],
                          source_identities: Mapping | None = None) -> StrategyHealthSnapshot:
    """Assemble one immutable health snapshot from already-captured owner facts.

    Pure: the only inputs are the ones the caller captured. Every dimension must be
    present exactly once, so coverage always has the same denominator, and the
    fingerprint is a deterministic function of the material above — persistence
    metadata such as ``created_at`` is deliberately not part of it.
    """
    identity = str(strategy_id or "")
    if not identity.strip():
        raise HealthEvidenceError("strategy_id_required")
    try:
        version = int(strategy_version)
    except (TypeError, ValueError) as exc:
        raise HealthEvidenceError("strategy_version_must_be_an_integer") from exc
    if version < 1:
        raise HealthEvidenceError("strategy_version_must_be_positive")
    checksum = str(strategy_checksum or "")
    if not _SHA256.fullmatch(checksum):
        raise HealthEvidenceError("strategy_checksum_must_be_sha256")
    if not isinstance(observation_window, HealthObservationWindow):
        raise HealthEvidenceError("explicit_observation_window_required")
    if lifecycle_state is not None and not str(lifecycle_state).strip():
        raise HealthEvidenceError("lifecycle_state_must_be_null_or_text")

    items = tuple(dimensions or ())
    if any(not isinstance(item, HealthDimension) for item in items):
        raise HealthEvidenceError("health_dimensions_must_be_dimension_objects")
    names = [item.name for item in items]
    if sorted(names) != sorted(DIMENSIONS) or len(names) != len(DIMENSIONS):
        # Missing or duplicate dimensions are contract violations, not "less coverage".
        raise HealthEvidenceError("health_dimensions_must_cover_every_dimension_exactly_once")
    ordered = tuple(sorted(items, key=lambda item: item.name))

    counts = {status: 0 for status in DIMENSION_STATUSES}
    for item in ordered:
        counts[item.status] += 1
    coverage = HealthCoverage(
        expected_dimensions=len(DIMENSIONS),
        available_dimensions=counts[STATUS_AVAILABLE],
        partial_dimensions=counts[STATUS_PARTIAL],
        unavailable_dimensions=counts[STATUS_UNAVAILABLE],
        not_applicable_dimensions=counts[STATUS_NOT_APPLICABLE],
        coverage_ratio=round(counts[STATUS_AVAILABLE] / len(DIMENSIONS), 6),
    )
    reasons = tuple(sorted({reason for item in ordered for reason in item.blocking_reasons}))
    material = {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "health_contract_version": HEALTH_CONTRACT_VERSION,
        "strategy_id": identity,
        "strategy_version": version,
        "strategy_checksum": checksum,
        "observation_start": observation_window.observation_start,
        "observation_end": observation_window.observation_end,
        "window_identity": observation_window.identity,
        "lifecycle_state": None if lifecycle_state is None else str(lifecycle_state),
        "dimensions": [item.projection() for item in ordered],
        "coverage": coverage.projection(),
        "source_identities": _thaw(_freeze(source_identities or {})),
        "blocking_reasons": list(reasons),
    }
    fingerprint = _sha(material)
    return StrategyHealthSnapshot(
        snapshot_id=fingerprint, snapshot_fingerprint=fingerprint,
        strategy_id=identity, strategy_version=version, strategy_checksum=checksum,
        observation_start=observation_window.observation_start,
        observation_end=observation_window.observation_end,
        window_identity=observation_window.identity,
        lifecycle_state=None if lifecycle_state is None else str(lifecycle_state),
        dimensions=ordered, coverage=coverage,
        source_identities=_freeze(source_identities or {}),
        blocking_reasons=reasons,
    )


def fingerprint(value) -> str:
    """The canonical fingerprint of any JSON-serializable value (single hasher)."""
    return _sha(value)


def verify_snapshot_fingerprint(snapshot: StrategyHealthSnapshot) -> bool:
    """Re-derive a snapshot's own fingerprint. Stored evidence must self-verify."""
    if not isinstance(snapshot, StrategyHealthSnapshot):
        raise HealthEvidenceError("canonical_health_snapshot_required")
    if snapshot.snapshot_id != snapshot.snapshot_fingerprint:
        return False
    return _sha(snapshot.fingerprint_material()) == snapshot.snapshot_fingerprint


def unavailable_dimension(name: str, reason: str, *,
                          facts: Mapping | None = None) -> HealthDimension:
    """A dimension nobody could evidence. It carries its reason, never a verdict."""
    return HealthDimension(name=name, status=STATUS_UNAVAILABLE, facts=facts or {},
                           provenance=PROVENANCE_UNAVAILABLE,
                           blocking_reasons=(reason,))


def not_applicable_dimension(name: str, reason: str) -> HealthDimension:
    return HealthDimension(name=name, status=STATUS_NOT_APPLICABLE, facts={},
                           provenance=PROVENANCE_UNAVAILABLE,
                           blocking_reasons=(reason,))


__all__ = [
    "HEALTH_CONTRACT_VERSION", "HEALTH_SCHEMA_VERSION", "DIMENSIONS",
    "DIMENSION_STATUSES", "PROVENANCES", "HealthEvidenceError",
    "HealthObservationWindow", "HealthDimension", "HealthCoverage",
    "StrategyHealthSnapshot", "build_strategy_health",
    "verify_snapshot_fingerprint", "unavailable_dimension",
    "not_applicable_dimension",
]
