"""Pure, deterministic Active/Challenger comparison evidence.

This module owns one thing: turning three **explicit** inputs — one exact Active
evidence envelope, one exact ShadowRun, and one explicit spec — into an
immutable comparison report. It opens no database, reads no provider, consults
no wall clock, and never resolves latest/current/head state. The application
service (`shadow_comparison_service`) loads the evidence by explicit ID and
persists the report; the pure builder here only reads evidence, normalizes it,
aligns observations, and computes deltas and coverage.

Business rules stay with their owners: this layer never re-runs signal, entry,
risk, execution, or valuation rules. It reports what the owning authorities
already produced, plus explicitly-labelled derived deltas.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

import shadow_runtime as SR

COMPARISON_SCHEMA_VERSION = "shadow-comparison-v1"
ACTIVE_EVIDENCE_SCHEMA_VERSION = "paper-orders-active-evidence-v1"
ACTIVE_EVIDENCE_SOURCE = "paper_orders"
SHARED_ENVIRONMENT_SCHEMA_VERSION = "comparison-shared-environment-v1"

#: Dimensions that must be complete for a report to be AVAILABLE.
REQUIRED_COMPARISON_DIMENSIONS = ("signal", "decision", "execution", "risk_rejection")
#: Dimensions reported on a best-effort basis; incomplete here never blocks AVAILABLE.
BEST_EFFORT_COMPARISON_DIMENSIONS = ("turnover", "performance")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIDES = frozenset({"buy", "sell"})
_SHARED_ENVIRONMENT_DIMENSIONS = (
    "session_date",
    "decision_at",
    "market_policy_name",
    "market_snapshot_fingerprint",
    "symbol_quote_fingerprints",
    "tradability_evidence_fingerprints",
    "execution_ruleset_identity",
)


class ShadowComparisonError(ValueError):
    """Stable fail-closed rejection of an explicit comparison input."""


class ComparisonAvailability(str, Enum):
    """Whether a comparison could be established, and how completely."""

    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class DimensionAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceProvenance(str, Enum):
    """Where a reported fact came from. Declared never becomes verified here."""

    OWNER_ISSUED = "OWNER_ISSUED"
    CAPTURED_INPUT = "CAPTURED_INPUT"
    DECLARED = "DECLARED"
    DERIVED = "DERIVED"
    UNAVAILABLE = "UNAVAILABLE"


class ObservationAlignment(str, Enum):
    """Which legs hold explicit evaluation evidence for one observation key.

    These are statements about evidence presence, never about a business
    outcome: a leg without evidence is `MISSING`, which is not the same as a
    rejected, blocked, or zero decision.
    """

    ALIGNED = "ALIGNED"
    ACTIVE_EVIDENCE_ONLY = "ACTIVE_EVIDENCE_ONLY"
    CHALLENGER_EVIDENCE_ONLY = "CHALLENGER_EVIDENCE_ONLY"
    NO_EVIDENCE = "NO_EVIDENCE"
    #: Evidence may well exist, but the report is blocked before alignment, so
    #: claiming absence would be wrong.
    NOT_COMPARABLE = "NOT_COMPARABLE"


class LegEvidenceState(str, Enum):
    """What one leg's own owner evidence can prove about one dimension.

    A container existing (a candidate, an order row) is not owner evidence:
    `PRESENT` requires the authority's own decision for this stage.
    """

    PRESENT = "PRESENT"
    #: The owner's own evidence proves the stage never applied.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: The stage applies, but the owner produced no evidence for it.
    MISSING = "MISSING"
    #: Evidence exists but cannot be used for this stage.
    UNAVAILABLE = "UNAVAILABLE"


def _text(value: Any, label: str) -> str:
    text = str(value or "")
    if not text.strip() or text != text.strip():
        raise ValueError(f"{label} is required")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return int(value)


def _optional_number(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _frozen_mapping(value: Any, label: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} must be a non-empty mapping")
    return MappingProxyType(dict(SR._plain(value)))


@dataclass(frozen=True, slots=True)
class ObservationKey:
    """Deterministic comparison identity for one decision observation."""

    symbol: str
    side: str

    def __post_init__(self):
        symbol = str(self.symbol or "").strip()
        side = str(self.side or "").strip().lower()
        if not symbol or symbol != str(self.symbol):
            raise ValueError("comparison observation symbol is required")
        if side not in _SIDES:
            raise ValueError("comparison observation side is invalid")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)

    @property
    def identity(self) -> str:
        return f"{self.symbol}|{self.side}"

    def projection(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "side": self.side}


@dataclass(frozen=True, slots=True)
class ActiveOrderEvidence:
    """One persisted Active decision, projected verbatim and never re-decided.

    Every field is read from an exact persisted column or JSON envelope. The
    runtime context and its availability are taken from the Execution
    Authority's own evidence (`paper_orders.execution_evidence`), which remains
    the only owner of that fact.
    """

    order_id: int
    account_id: str
    symbol: str
    side: str
    order_status: str
    created_at: str
    requested_quantity: int
    filled_quantity: int
    signal_id: int | None = None
    cycle_id: int | None = None
    strategy_id: str | None = None
    strategy_version: int | None = None
    strategy_checksum: str | None = None
    order_reason: str | None = None
    order_type: str | None = None
    planned_price: float | None = None
    filled_price: float | None = None
    amount: float | None = None
    fees: float | None = None
    realized_pnl: float | None = None
    signal_evidence: Mapping[str, Any] | None = None
    admission_evidence: Mapping[str, Any] | None = None
    execution_evidence: Mapping[str, Any] | None = None

    def __post_init__(self):
        if isinstance(self.order_id, bool) or not isinstance(self.order_id, int) or self.order_id < 1:
            raise ValueError("Active order identity is required")
        object.__setattr__(self, "account_id", _text(self.account_id, "Active order account"))
        object.__setattr__(self, "symbol", _text(self.symbol, "Active order symbol"))
        object.__setattr__(self, "order_status", _text(self.order_status, "Active order status"))
        object.__setattr__(self, "created_at", _text(self.created_at, "Active order created_at"))
        side = str(self.side or "").strip().lower()
        if side not in _SIDES:
            raise ValueError("Active order side is invalid")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "requested_quantity",
                           _count(self.requested_quantity, "Active requested quantity"))
        object.__setattr__(self, "filled_quantity",
                           _count(self.filled_quantity, "Active filled quantity"))
        for name in ("signal_id", "cycle_id"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"Active order {name} is invalid")
        stamp = (self.strategy_id, self.strategy_version, self.strategy_checksum)
        if any(item is not None for item in stamp) and any(item is None for item in stamp):
            raise ValueError("Active order strategy stamp must be complete")
        if self.strategy_id is not None:
            object.__setattr__(self, "strategy_id",
                               _text(self.strategy_id, "Active order strategy id"))
            version = self.strategy_version
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ValueError("Active order strategy version is invalid")
            if not _SHA256.fullmatch(str(self.strategy_checksum or "")):
                raise ValueError("Active order strategy checksum is invalid")
        for name in ("planned_price", "filled_price", "amount", "fees", "realized_pnl"):
            object.__setattr__(self, name, _optional_number(getattr(self, name), f"Active {name}"))
        object.__setattr__(self, "order_reason", _optional_text(self.order_reason))
        object.__setattr__(self, "order_type", _optional_text(self.order_type))
        object.__setattr__(self, "signal_evidence",
                           _frozen_mapping(self.signal_evidence, "Active signal evidence"))
        object.__setattr__(self, "admission_evidence",
                           _frozen_mapping(self.admission_evidence, "Active admission evidence"))
        execution = _frozen_mapping(self.execution_evidence, "Active execution evidence")
        object.__setattr__(self, "execution_evidence", execution)
        claimed = (execution or {}).get("runtime_context_availability") if execution else None
        if claimed is not None and str(claimed) not in {"AVAILABLE", "UNAVAILABLE"}:
            raise ValueError("Active execution evidence runtime context availability is invalid")
        if (str(claimed) == "AVAILABLE") != (self.runtime_context is not None):
            raise ValueError("Active execution evidence runtime context is inconsistent")

    @property
    def key(self) -> ObservationKey:
        return ObservationKey(self.symbol, self.side)

    @property
    def runtime_context(self) -> Mapping[str, Any] | None:
        """The Active decision's comparable inputs, as persisted by its owner."""
        evidence = self.execution_evidence or {}
        context = evidence.get("runtime_context")
        return context if isinstance(context, Mapping) else None

    @property
    def runtime_context_availability(self) -> str:
        return "AVAILABLE" if self.runtime_context is not None else "UNAVAILABLE"

    @property
    def runtime_context_unavailability_reason(self) -> str | None:
        if self.runtime_context is not None:
            return None
        evidence = self.execution_evidence or {}
        reason = _optional_text(evidence.get("runtime_context_unavailability_reason"))
        return reason or "missing_runtime_context"

    @property
    def execution(self) -> Mapping[str, Any] | None:
        """The Execution Authority's own evidence projection, verbatim."""
        return self.execution_evidence

    @property
    def admission(self) -> Mapping[str, Any] | None:
        """The Active buy-path admission decision, when its owner persisted one.

        It is read from the order's own row (exact linkage, never a time- or
        latest-based guess) and names its source. It is a different authority
        from ``execution_planner.evaluate_entry_state``, so the comparison
        reports both vocabularies verbatim and never maps one onto the other.
        """
        evidence = self.admission_evidence or {}
        if not str(evidence.get("decision") or "").strip():
            return None
        return evidence

    def projection(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "account_id": self.account_id,
            "signal_id": self.signal_id,
            "cycle_id": self.cycle_id,
            "strategy_stamp": {
                "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
                "strategy_checksum": self.strategy_checksum,
            },
            "symbol": self.symbol,
            "side": self.side,
            "order_status": self.order_status,
            "order_reason": self.order_reason,
            "order_type": self.order_type,
            "requested_quantity": self.requested_quantity,
            "planned_price": self.planned_price,
            "filled_quantity": self.filled_quantity,
            "filled_price": self.filled_price,
            "amount": self.amount,
            "fees": self.fees,
            "realized_pnl": self.realized_pnl,
            "created_at": self.created_at,
            "signal_evidence": SR._plain(self.signal_evidence),
            "admission_evidence": SR._plain(self.admission_evidence),
            "execution_evidence": SR._plain(self.execution_evidence),
        }


def _active_evidence_material(orders: tuple[ActiveOrderEvidence, ...]) -> dict[str, Any]:
    return {
        "source_schema_version": ACTIVE_EVIDENCE_SCHEMA_VERSION,
        "source": ACTIVE_EVIDENCE_SOURCE,
        "orders": [order.projection() for order in orders],
    }


@dataclass(frozen=True, slots=True)
class ActiveComparisonEvidence:
    """Immutable reference to exact persisted Active evidence.

    It wraps rows its owners already wrote; it never recomputes an Active
    decision. `paper_orders` rows are updated in place as execution progresses,
    so `source_fingerprint` identifies the exact projected evidence as loaded,
    and the source identity carries the primary key it came from.
    """

    source_schema_version: str
    source_identity: Mapping[str, Any]
    source_fingerprint: str
    orders: tuple[ActiveOrderEvidence, ...]

    @classmethod
    def build(cls, orders) -> ActiveComparisonEvidence:
        ordered = tuple(sorted(orders, key=lambda item: item.order_id))
        return cls(
            source_schema_version=ACTIVE_EVIDENCE_SCHEMA_VERSION,
            source_identity={"source": ACTIVE_EVIDENCE_SOURCE,
                             "order_ids": [item.order_id for item in ordered]},
            source_fingerprint=SR.fingerprint(_active_evidence_material(ordered)),
            orders=ordered,
        )

    def __post_init__(self):
        if self.source_schema_version != ACTIVE_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("Active evidence schema version is unsupported")
        orders = tuple(self.orders)
        if not orders or any(not isinstance(item, ActiveOrderEvidence) for item in orders):
            raise ValueError("Active evidence requires exact order evidence")
        if tuple(sorted(orders, key=lambda item: item.order_id)) != orders:
            raise ValueError("Active evidence order identity order is not canonical")
        keys = [item.key.identity for item in orders]
        if len(set(keys)) != len(keys):
            # Two rows with the same (symbol, side) have no deterministic order
            # of their own; the caller must disambiguate before comparing.
            raise ShadowComparisonError("active_observation_ambiguous_duplicate")
        identity = dict(self.source_identity or {})
        if identity != {"source": ACTIVE_EVIDENCE_SOURCE,
                        "order_ids": [item.order_id for item in orders]}:
            raise ValueError("Active evidence source identity mismatch")
        if self.source_fingerprint != SR.fingerprint(_active_evidence_material(orders)):
            raise ValueError("Active evidence fingerprint mismatch")
        object.__setattr__(self, "orders", orders)
        object.__setattr__(self, "source_identity",
                           MappingProxyType({"source": ACTIVE_EVIDENCE_SOURCE,
                                             "order_ids": [item.order_id for item in orders]}))

    def projection(self) -> dict[str, Any]:
        return {
            "source_schema_version": self.source_schema_version,
            "source_identity": dict(self.source_identity),
            "source_fingerprint": self.source_fingerprint,
            "orders": [order.projection() for order in self.orders],
        }

    def orders_by_identity(self) -> dict[str, ActiveOrderEvidence]:
        return {order.key.identity: order for order in self.orders}


@dataclass(frozen=True, slots=True)
class ComparisonSpec:
    """Every input the comparison needs, declared explicitly by the caller."""

    challenger: SR.StrategyStamp
    active_comparator: SR.StrategyStamp
    shadow_run_id: str
    active_order_ids: tuple[int, ...]
    #: Exact Active evidence identity. Order ids alone are NOT an evidence
    #: identity: `paper_orders` rows are updated in place, so a comparison must
    #: pin the fingerprint of the projection it consumed and fail closed when the
    #: mutable row has drifted.
    active_evidence_id: str
    environment_fingerprint: str
    session_date: str
    decision_at: str
    expected_observations: tuple[ObservationKey, ...]
    comparison_schema_version: str = COMPARISON_SCHEMA_VERSION

    def __post_init__(self):
        if self.comparison_schema_version != COMPARISON_SCHEMA_VERSION:
            raise ValueError("comparison schema version is unsupported")
        if not isinstance(self.challenger, SR.StrategyStamp) or not isinstance(
                self.active_comparator, SR.StrategyStamp):
            raise TypeError("exact challenger and active strategy stamps are required")
        if not _SHA256.fullmatch(str(self.shadow_run_id or "")):
            raise ValueError("explicit shadow run id is required")
        order_ids = tuple(sorted(self.active_order_ids))
        if not order_ids or any(isinstance(item, bool) or not isinstance(item, int)
                                or item < 1 for item in order_ids):
            raise ValueError("explicit Active order ids are required")
        if len(set(order_ids)) != len(order_ids):
            raise ValueError("explicit Active order ids must be unique")
        if not _SHA256.fullmatch(str(self.environment_fingerprint or "")):
            raise ValueError("environment fingerprint is invalid")
        try:
            day = dt.date.fromisoformat(str(self.session_date)).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("comparison session_date must be an ISO date") from exc
        observations = tuple(sorted(self.expected_observations,
                                    key=lambda item: (item.symbol, item.side)))
        if not observations or any(not isinstance(item, ObservationKey) for item in observations):
            raise ValueError("explicit expected observations are required")
        identities = [item.identity for item in observations]
        if len(set(identities)) != len(identities):
            raise ShadowComparisonError("comparison_scope_ambiguous_duplicate")
        if not _SHA256.fullmatch(str(self.active_evidence_id or "")):
            raise ValueError("exact Active evidence fingerprint is required")
        object.__setattr__(self, "active_order_ids", order_ids)
        object.__setattr__(self, "session_date", day)
        # Both owners persist the canonical UTC instant; normalizing here with the
        # same rule keeps the declared instant comparable by exact string equality.
        object.__setattr__(self, "decision_at", SR._instant(self.decision_at))
        object.__setattr__(self, "expected_observations", observations)

    def declared(self) -> dict[str, Any]:
        """Exactly the fields the caller declared, without derived identity."""
        return {
            "comparison_schema_version": self.comparison_schema_version,
            "challenger": self.challenger.projection(),
            "active_comparator": self.active_comparator.projection(),
            "shadow_run_id": self.shadow_run_id,
            "active_order_ids": list(self.active_order_ids),
            "active_evidence_id": self.active_evidence_id,
            "environment_fingerprint": self.environment_fingerprint,
            "session_date": self.session_date,
            "decision_at": self.decision_at,
            "expected_observations": [item.projection()
                                      for item in self.expected_observations],
        }

    def scope_identity(self) -> str:
        """Deterministic identity of everything this comparison declares."""
        return SR.fingerprint(self.declared())

    def projection(self) -> dict[str, Any]:
        return {**self.declared(), "comparison_scope_identity": self.scope_identity()}


@dataclass(frozen=True, slots=True)
class CoverageEvidence:
    """Expected-vs-observed coverage. The denominator is always `expected`."""

    expected_observations: int
    available_observations: int
    partial_observations: int
    missing_observations: int
    unavailable_observations: int
    coverage_ratio: float
    #: Aligned observations whose required dimensions are not owner-complete.
    #: They are part of `partial`, never of `available`.
    aligned_incomplete_observations: int = 0
    blocking_reasons: tuple[str, ...] = ()

    def __post_init__(self):
        counts = {
            "expected": _count(self.expected_observations, "expected observations"),
            "available": _count(self.available_observations, "available observations"),
            "partial": _count(self.partial_observations, "partial observations"),
            "missing": _count(self.missing_observations, "missing observations"),
            "unavailable": _count(self.unavailable_observations, "unavailable observations"),
        }
        if not counts["expected"]:
            raise ValueError("coverage requires at least one expected observation")
        if (counts["available"] + counts["partial"] + counts["missing"]
                + counts["unavailable"]) != counts["expected"]:
            raise ValueError("coverage buckets must partition the expected observations")
        incomplete = _count(self.aligned_incomplete_observations,
                            "aligned incomplete observations")
        if incomplete > counts["partial"]:
            raise ValueError("aligned incomplete observations must be part of partial")
        ratio = float(self.coverage_ratio)
        if (not math.isfinite(ratio)
                or round(ratio, 6) != round(counts["available"] / counts["expected"], 6)):
            raise ValueError("coverage ratio must equal available/expected")
        object.__setattr__(self, "blocking_reasons",
                           tuple(str(item) for item in self.blocking_reasons))

    def projection(self) -> dict[str, Any]:
        return {
            "expected_observations": self.expected_observations,
            "available_observations": self.available_observations,
            "partial_observations": self.partial_observations,
            "missing_observations": self.missing_observations,
            "unavailable_observations": self.unavailable_observations,
            "aligned_incomplete_observations": self.aligned_incomplete_observations,
            "coverage_ratio": self.coverage_ratio,
            "blocking_reasons": list(self.blocking_reasons),
        }


@dataclass(frozen=True, slots=True)
class ShadowComparisonReport:
    """Immutable comparison evidence. It states facts and deltas, never a winner."""

    report_id: str
    report_fingerprint: str
    comparison_spec: Mapping[str, Any]
    active_evidence: Mapping[str, Any]
    active_order_lifecycle: Mapping[str, Any]
    shadow_run_id: str
    shadow_run_fingerprint: str
    environment_identity: Mapping[str, Any]
    active_strategy_stamp: Mapping[str, Any]
    challenger_strategy_stamp: Mapping[str, Any]
    availability: str
    coverage: Mapping[str, Any]
    observations: tuple[Mapping[str, Any], ...]
    signal_delta: Mapping[str, Any]
    decision_delta: Mapping[str, Any]
    turnover: Mapping[str, Any]
    execution: Mapping[str, Any]
    risk_rejection: Mapping[str, Any]
    performance: Mapping[str, Any]
    provenance: Mapping[str, Any]
    blocking_reasons: tuple[str, ...] = ()
    schema_version: str = COMPARISON_SCHEMA_VERSION

    def projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "report_fingerprint": self.report_fingerprint,
            "comparison_spec": SR._plain(self.comparison_spec),
            "active_evidence": SR._plain(self.active_evidence),
            "active_order_lifecycle": SR._plain(self.active_order_lifecycle),
            "shadow_run_id": self.shadow_run_id,
            "shadow_run_fingerprint": self.shadow_run_fingerprint,
            "environment_identity": SR._plain(self.environment_identity),
            "active_strategy_stamp": SR._plain(self.active_strategy_stamp),
            "challenger_strategy_stamp": SR._plain(self.challenger_strategy_stamp),
            "availability": self.availability,
            "coverage": SR._plain(self.coverage),
            "observations": SR._plain(self.observations),
            "signal_delta": SR._plain(self.signal_delta),
            "decision_delta": SR._plain(self.decision_delta),
            "turnover": SR._plain(self.turnover),
            "execution": SR._plain(self.execution),
            "risk_rejection": SR._plain(self.risk_rejection),
            "performance": SR._plain(self.performance),
            "provenance": SR._plain(self.provenance),
            "blocking_reasons": list(self.blocking_reasons),
        }


# ─── pure builder ────────────────────────────────────────────────────────────

def _reasons(items) -> tuple[str, ...]:
    return tuple(sorted({str(item) for item in items if str(item or "")}))


def _unavailable_section(reasons) -> dict[str, Any]:
    return {"availability": DimensionAvailability.UNAVAILABLE.value,
            "blocking_reasons": list(_reasons(reasons))}


def _as_stamp(value: Any) -> dict[str, Any]:
    stamp = value if isinstance(value, Mapping) else {}
    return {"strategy_id": stamp.get("strategy_id"),
            "version": stamp.get("version"),
            "checksum": stamp.get("checksum")}


def _pairs(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


_SHARED_ENVIRONMENT_RULESET_KEYS = (
    "execution_ruleset_identity", "execution_ruleset_version")


def shared_environment_projection(projection: Any) -> dict[str, Any]:
    """The seven shared environment dimensions, from either leg's projection.

    An Active `ComparableRuntimeContext` projection names the ruleset
    `execution_ruleset_version`; a Shadow `ComparableEnvironmentIdentity`
    projection names it `execution_ruleset_identity`. Both map onto the same
    dimension here, so equality is computed on one canonical shape.
    """
    source = projection if isinstance(projection, Mapping) else {}
    ruleset = next((source[key] for key in _SHARED_ENVIRONMENT_RULESET_KEYS
                    if source.get(key) not in (None, "")), None)
    return {
        "schema_version": SHARED_ENVIRONMENT_SCHEMA_VERSION,
        "session_date": source.get("session_date"),
        "decision_at": source.get("decision_at"),
        "market_policy_name": source.get("market_policy_name"),
        "market_snapshot_fingerprint": source.get("market_snapshot_fingerprint"),
        "symbol_quote_fingerprints": _pairs(source.get("symbol_quote_fingerprints")),
        "tradability_evidence_fingerprints": _pairs(
            source.get("tradability_evidence_fingerprints")),
        "execution_ruleset_identity": ruleset,
    }


def _shadow_spec(shadow_run: SR.ShadowRunEvidence) -> dict[str, Any]:
    return dict(shadow_run.spec or {})


def _shadow_environment(shadow_run: SR.ShadowRunEvidence) -> dict[str, Any]:
    return dict(shadow_run.environment or {})


def _binding_reasons(*, spec: ComparisonSpec, active_evidence: ActiveComparisonEvidence,
                     shadow_run: SR.ShadowRunEvidence) -> list[str]:
    """Everything that must hold before any delta may be computed."""
    reasons: list[str] = []
    if not isinstance(shadow_run, SR.ShadowRunEvidence):
        raise TypeError("exact ShadowRun evidence is required")
    shadow_spec = _shadow_spec(shadow_run)
    environment = _shadow_environment(shadow_run)
    identity = environment.get("identity")
    if shadow_run.run_id != shadow_run.run_fingerprint or shadow_run.run_id != spec.shadow_run_id:
        reasons.append("shadow_run_identity_mismatch")
    if tuple(active_evidence.source_identity.get("order_ids") or ()) != spec.active_order_ids:
        reasons.append("active_evidence_identity_mismatch")
    if active_evidence.source_fingerprint != spec.active_evidence_id:
        reasons.append("active_evidence_fingerprint_mismatch")
    if _as_stamp(shadow_spec.get("challenger")) != spec.challenger.projection():
        reasons.append("challenger_strategy_identity_mismatch")
    if _as_stamp(shadow_spec.get("active_comparator")) != spec.active_comparator.projection():
        reasons.append("active_comparator_strategy_identity_mismatch")
    if (str(shadow_spec.get("session_date") or "") != spec.session_date
            or str(shadow_spec.get("decision_at") or "") != spec.decision_at):
        reasons.append("comparison_instant_mismatch")
    shadow_fingerprint = str(shadow_spec.get("environment_fingerprint") or "")
    if (not isinstance(identity, Mapping)
            or str(identity.get("environment_fingerprint") or "") != shadow_fingerprint):
        reasons.append("shadow_environment_identity_inconsistent")
    if shadow_fingerprint != spec.environment_fingerprint:
        reasons.append("environment_fingerprint_mismatch")
    if str(shadow_spec.get("reference_capital") or "") == "":
        reasons.append("shadow_reference_capital_unavailable")
    return reasons


def _active_leg_reasons(*, spec: ComparisonSpec,
                        active_evidence: ActiveComparisonEvidence) -> list[str]:
    """The Active leg must be usable on its own before it is compared."""
    reasons: list[str] = []
    contexts: list[dict[str, Any]] = []
    for order in active_evidence.orders:
        identity = order.key.identity
        if (order.strategy_id is not None
                and (order.strategy_id != spec.active_comparator.strategy_id
                     or order.strategy_version != spec.active_comparator.version
                     or order.strategy_checksum != spec.active_comparator.checksum)):
            reasons.append(f"active_order_strategy_identity_mismatch:{identity}")
        context = order.runtime_context
        if context is None:
            reasons.append("active_runtime_context_unavailable:"
                           f"{identity}:{order.runtime_context_unavailability_reason}")
            continue
        if (str(context.get("session_date") or "") != spec.session_date
                or str(context.get("decision_at") or "") != spec.decision_at):
            reasons.append(f"active_decision_instant_mismatch:{identity}")
        if (str(context.get("strategy_id") or "") != spec.active_comparator.strategy_id
                or context.get("strategy_version") != spec.active_comparator.version
                or str(context.get("strategy_checksum") or "") != spec.active_comparator.checksum):
            reasons.append(f"active_context_strategy_identity_mismatch:{identity}")
        contexts.append(shared_environment_projection(context))
    if len({SR.canonical_json(item) for item in contexts}) > 1:
        reasons.append("active_environment_inconsistent")
    return reasons


def _shadow_observations(shadow_run: SR.ShadowRunEvidence) -> dict[str, Mapping[str, Any]]:
    observations: dict[str, Mapping[str, Any]] = {}
    for decision in shadow_run.decisions or ():
        if not isinstance(decision, Mapping):
            raise ShadowComparisonError("shadow_observation_evidence_invalid")
        key = ObservationKey(str(decision.get("symbol") or ""),
                             str(decision.get("side") or "")).identity
        if key in observations:
            raise ShadowComparisonError("shadow_observation_ambiguous_duplicate")
        observations[key] = decision
    return observations


def _leg_dimension(*, active: Any, challenger: Any,
                   active_state: str, challenger_state: str,
                   delta: Mapping[str, Any] | None = None,
                   reasons=()) -> dict[str, Any]:
    """One per-observation dimension, with each leg's owner evidence explicit.

    A dimension is only AVAILABLE when both legs' own authority evidence is
    PRESENT. Absence is reported as MISSING (or NOT_APPLICABLE when the owner's
    own evidence proves the stage never applied) and is never turned into a
    false, a rejected, or a zero.
    """
    states = {active_state, challenger_state}
    if states == {LegEvidenceState.PRESENT.value}:
        availability = DimensionAvailability.AVAILABLE.value
    elif states <= {LegEvidenceState.MISSING.value, LegEvidenceState.UNAVAILABLE.value}:
        availability = DimensionAvailability.UNAVAILABLE.value
    else:
        availability = DimensionAvailability.PARTIAL.value
    return {
        "availability": availability,
        "legs": {"active": active_state, "challenger": challenger_state},
        "active": active,
        "challenger": challenger,
        "delta": dict(delta) if delta is not None else None,
        "blocking_reasons": list(_reasons(reasons)),
    }


def _relation(left: Any, right: Any) -> str:
    return "SAME" if left == right else "DIFFERENT"


def _challenger_signal_blocked(challenger: Mapping[str, Any] | None) -> bool:
    """Does the owner's own signal decision prove no later stage applied?"""
    signal = challenger.get("signal") if isinstance(challenger, Mapping) else None
    outcome = signal.get("outcome") if isinstance(signal, Mapping) else None
    return str(outcome or "").strip().lower() == "blocked"


def _signal_dimension(active: ActiveOrderEvidence | None,
                      challenger: Mapping[str, Any] | None) -> dict[str, Any]:
    present, missing = (
        LegEvidenceState.PRESENT.value, LegEvidenceState.MISSING.value)
    active_payload = None
    active_state = missing
    reasons: list[str] = []
    if active is None:
        reasons.append("active_observation_absent")
    else:
        active_decision = (active.signal_evidence or {}).get("signal_decision")
        active_payload = {
            "outcome": (active_decision or {}).get("outcome"),
            "status": (active_decision or {}).get("status"),
            "reason": (active_decision or {}).get("reason"),
            "evidence": (active.signal_evidence or {}).get("signal_evidence"),
            # Selection scores are raw owner output from the persisted signal
            # row; the comparison never synthesizes a score of its own.
            "selection_scores": (active.signal_evidence or {}).get("selection_scores"),
        }
        if active_payload["outcome"] is not None:
            active_state = present
        elif active.signal_evidence is None:
            reasons.append("active_signal_evidence_absent")
        else:
            reasons.append("active_signal_decision_absent")
    challenger_payload = None
    challenger_state = missing
    if challenger is None:
        reasons.append("challenger_observation_absent")
    else:
        challenger_signal = challenger.get("signal")
        challenger_payload = {
            "outcome": (challenger_signal or {}).get("outcome")
            if isinstance(challenger_signal, Mapping) else None,
            "status": (challenger_signal or {}).get("status")
            if isinstance(challenger_signal, Mapping) else None,
            "reason": (challenger_signal or {}).get("reason")
            if isinstance(challenger_signal, Mapping) else None,
            "evidence": (challenger_signal or {}).get("evidence")
            if isinstance(challenger_signal, Mapping) else None,
        }
        if challenger_payload["outcome"] is not None:
            challenger_state = present
        else:
            reasons.append("challenger_signal_evidence_absent")
    if active_state != present or challenger_state != present:
        return _leg_dimension(active=active_payload, challenger=challenger_payload,
                              active_state=active_state,
                              challenger_state=challenger_state, delta=None,
                              reasons=reasons)
    delta = {
        "active_outcome": active_payload["outcome"],
        "challenger_outcome": challenger_payload["outcome"],
        "outcome_relation": _relation(active_payload["outcome"],
                                      challenger_payload["outcome"]),
        "active_reason": active_payload.get("reason"),
        "challenger_reason": challenger_payload.get("reason"),
        "reason_relation": _relation(active_payload.get("reason"),
                                     challenger_payload.get("reason")),
        "active_evidence": active_payload.get("evidence"),
        "challenger_evidence": challenger_payload.get("evidence"),
        "evidence_relation": _relation(active_payload.get("evidence"),
                                       challenger_payload.get("evidence")),
    }
    return _leg_dimension(active=active_payload, challenger=challenger_payload,
                          active_state=active_state, challenger_state=challenger_state,
                          delta=delta)


def _decision_dimension(active: ActiveOrderEvidence | None,
                        challenger: Mapping[str, Any] | None) -> dict[str, Any]:
    present, not_applicable, missing, unavailable = (
        LegEvidenceState.PRESENT.value, LegEvidenceState.NOT_APPLICABLE.value,
        LegEvidenceState.MISSING.value, LegEvidenceState.UNAVAILABLE.value)
    entry = challenger.get("entry") if isinstance(challenger, Mapping) else None
    candidate = challenger.get("candidate") if isinstance(challenger, Mapping) else None
    active_payload = None
    active_state = missing
    if active is not None:
        admission = active.admission
        active_payload = {
            # The order's own persisted admission decision. It comes from the
            # Active buy path, a different authority from the Challenger's entry
            # gate, so it is reported verbatim under its own source name.
            "admission_source": (admission or {}).get("source")
            or "risk_payload.decision_snapshot.final",
            "admission_decision": (admission or {}).get("decision"),
            "admission_reason": (admission or {}).get("reason"),
            "admission_score": (admission or {}).get("score"),
            "admission_gates": (admission or {}).get("gates"),
            "admission_provenance": (EvidenceProvenance.OWNER_ISSUED.value
                                     if admission is not None
                                     else EvidenceProvenance.UNAVAILABLE.value),
            "order_type": active.order_type,
            "requested_quantity": active.requested_quantity,
            "planned_price": active.planned_price,
        }
        active_state = present if admission is not None else missing
    challenger_payload = None
    challenger_state = missing
    if isinstance(challenger, Mapping):
        entry_mapping = entry if isinstance(entry, Mapping) else None
        candidate_mapping = candidate if isinstance(candidate, Mapping) else None
        challenger_payload = {
            "admission_source": "entry_gate",
            "admission_decision": (entry_mapping or {}).get("allowed"),
            "admission_reasons": list((entry_mapping or {}).get("reasons") or ()),
            "admission_gates": (entry_mapping or {}).get("gates"),
            "admission_policy": (entry_mapping or {}).get("policy"),
            "requires_manual_entry_review": (entry_mapping or {}).get(
                "requires_manual_entry_review"),
            "admission_provenance": (EvidenceProvenance.OWNER_ISSUED.value
                                     if entry_mapping is not None
                                     and "allowed" in entry_mapping
                                     else EvidenceProvenance.UNAVAILABLE.value),
            "order_type": (candidate_mapping or {}).get("order_type"),
            "requested_quantity": (candidate_mapping or {}).get("desired_quantity"),
            "reference_price": (candidate_mapping or {}).get("reference_price"),
        }
        has_admission = (entry_mapping is not None and "allowed" in entry_mapping)
        if has_admission:
            challenger_state = present
        elif _challenger_signal_blocked(challenger):
            # The owner's own signal decision proves the candidate never reached
            # an admission decision.
            challenger_state = not_applicable
        else:
            challenger_state = missing
    if active_state != present or challenger_state != present:
        reasons = []
        if active is None:
            reasons.append("active_observation_absent")
        elif active_state == missing:
            reasons.append("active_admission_decision_not_persisted")
        elif active_state == unavailable:
            reasons.append("active_admission_evidence_unusable")
        if challenger is None:
            reasons.append("challenger_observation_absent")
        elif challenger_state == not_applicable:
            reasons.append("challenger_admission_not_applicable")
        elif challenger_state == missing:
            reasons.append("challenger_admission_decision_absent")
        elif challenger_state == unavailable:
            reasons.append("challenger_admission_evidence_unusable")
        return _leg_dimension(active=active_payload, challenger=challenger_payload,
                              active_state=active_state,
                              challenger_state=challenger_state, delta=None,
                              reasons=reasons)
    active_quantity = active_payload.get("requested_quantity")
    challenger_quantity = challenger_payload.get("requested_quantity")
    delta = {
        "active_admission_decision": active_payload.get("admission_decision"),
        "challenger_admission_decision": challenger_payload.get("admission_decision"),
        "order_type_relation": _relation(active_payload.get("order_type"),
                                         challenger_payload.get("order_type")),
        "requested_quantity_delta": (
            active_quantity - challenger_quantity
            if isinstance(active_quantity, int) and isinstance(challenger_quantity, int)
            else None),
        # The two legs' admission decisions come from different authorities with
        # different vocabularies (an Active buy-path outcome versus an entry-gate
        # admission). No owner has defined a mapping, so the comparison states the
        # two facts and that the relation itself is undefined - it does not invent
        # one, and it does not present a lifecycle status as an admission.
        "cross_vocabulary_admission_relation": unavailable,
        "cross_vocabulary_relation_defined": False,
        "cross_vocabulary_reason": "no_owner_defined_admission_vocabulary_mapping",
    }
    return _leg_dimension(active=active_payload, challenger=challenger_payload,
                          active_state=active_state, challenger_state=challenger_state,
                          delta=delta)


def _fill_ratio(requested: Any, filled: Any) -> float | None:
    if not isinstance(requested, int) or isinstance(requested, bool) or requested <= 0:
        return None
    if not isinstance(filled, int) or isinstance(filled, bool) or filled < 0:
        return None
    return round(filled / requested, 6)


def _execution_dimension(active: ActiveOrderEvidence | None,
                         challenger: Mapping[str, Any] | None) -> dict[str, Any]:
    present, not_applicable, missing, unavailable = (
        LegEvidenceState.PRESENT.value, LegEvidenceState.NOT_APPLICABLE.value,
        LegEvidenceState.MISSING.value, LegEvidenceState.UNAVAILABLE.value)
    entry = challenger.get("entry") if isinstance(challenger, Mapping) else None
    candidate = challenger.get("candidate") if isinstance(challenger, Mapping) else None
    execution = challenger.get("execution") if isinstance(challenger, Mapping) else None
    active_payload = None
    active_state = missing
    if active is not None:
        evidence = active.execution
        active_payload = {
            # The Execution Authority's own evidence, verbatim, plus the ledger
            # column as a separate labelled fact.
            "execution_evidence": evidence,
            "execution_evidence_availability": (
                EvidenceProvenance.OWNER_ISSUED.value if evidence is not None
                else EvidenceProvenance.UNAVAILABLE.value),
            "execution_fill_quantity": ((evidence or {}).get("fill_quantity")
                                        if evidence is not None else None),
            "ledger_requested_quantity": active.requested_quantity,
            "ledger_filled_quantity": active.filled_quantity,
            "ledger_amount": active.amount,
            "ledger_fees": active.fees,
            "ledger_realized_pnl": active.realized_pnl,
        }
        # Without the Execution Authority's own evidence the Active leg has no
        # execution evidence; the ledger status is not used to infer one.
        active_state = present if evidence is not None else unavailable
    challenger_payload = None
    challenger_state = missing
    if isinstance(challenger, Mapping):
        execution_mapping = execution if isinstance(execution, Mapping) else None
        entry_mapping = entry if isinstance(entry, Mapping) else None
        candidate_mapping = candidate if isinstance(candidate, Mapping) else None
        challenger_payload = {
            "candidate_present": candidate_mapping is not None,
            "execution_evidence": execution_mapping,
            "execution_evidence_availability": (
                EvidenceProvenance.OWNER_ISSUED.value if execution_mapping is not None
                else EvidenceProvenance.UNAVAILABLE.value),
            # Never fabricated: absent execution evidence has no fill quantity.
            "execution_fill_quantity": ((execution_mapping or {}).get("fill_quantity")
                                        if execution_mapping is not None else None),
            "remaining_quantity": ((execution_mapping or {}).get("remaining_quantity")
                                   if execution_mapping is not None else None),
            "status": ((execution_mapping or {}).get("status")
                       if execution_mapping is not None else None),
            "reasons": (list((execution_mapping or {}).get("reasons") or ())
                        if execution_mapping is not None else None),
            "reference_price": ((execution_mapping or {}).get("reference_price")
                                if execution_mapping is not None else None),
            "fill_price": ((execution_mapping or {}).get("fill_price")
                           if execution_mapping is not None else None),
            "fees": ((execution_mapping or {}).get("fees")
                     if execution_mapping is not None else None),
            "slippage_amount": ((execution_mapping or {}).get("slippage_amount")
                                if execution_mapping is not None else None),
            "ruleset_version": ((execution_mapping or {}).get("ruleset_version")
                                if execution_mapping is not None else None),
            "liquidity_evidence": ((execution_mapping or {}).get("liquidity_evidence")
                                   if execution_mapping is not None else None),
            "tradability_evidence": ((execution_mapping or {}).get("tradability_evidence")
                                     if execution_mapping is not None else None),
            "requested_quantity": (candidate_mapping or {}).get("desired_quantity"),
        }
        if execution_mapping is not None:
            challenger_state = present
        elif entry_mapping is not None and entry_mapping.get("allowed") is False:
            # The owner's own admission decision proves no execution attempt
            # exists, so this stage did not apply rather than being incomplete.
            challenger_state = not_applicable
        elif _challenger_signal_blocked(challenger):
            challenger_state = not_applicable
        else:
            challenger_state = missing
    active_ratio = _fill_ratio(
        (active_payload or {}).get("ledger_requested_quantity"),
        (active_payload or {}).get("execution_fill_quantity"))
    challenger_ratio = _fill_ratio(
        (challenger_payload or {}).get("requested_quantity"),
        (challenger_payload or {}).get("execution_fill_quantity"))
    if active_state != present or challenger_state != present:
        reasons = []
        if active_state == unavailable:
            reasons.append("active_execution_evidence_not_persisted")
        elif active_state == missing:
            reasons.append("active_execution_evidence_absent")
        if challenger_state == missing:
            reasons.append("challenger_execution_evidence_absent")
        elif challenger_state == not_applicable:
            reasons.append("challenger_execution_not_applicable")
        elif challenger_state == unavailable:
            reasons.append("challenger_execution_evidence_absent")
        return _leg_dimension(active=active_payload, challenger=challenger_payload,
                              active_state=active_state,
                              challenger_state=challenger_state, delta=None,
                              reasons=reasons)
    active_filled = active_payload.get("execution_fill_quantity")
    challenger_filled = challenger_payload.get("execution_fill_quantity")
    delta = {
        "active_fill_ratio": active_ratio,
        "challenger_fill_ratio": challenger_ratio,
        "fill_ratio_relation": (
            _relation(active_ratio, challenger_ratio)
            if active_ratio is not None and challenger_ratio is not None else None),
        "fill_quantity_delta": (
            active_filled - challenger_filled
            if isinstance(active_filled, int) and isinstance(challenger_filled, int)
            else None),
    }
    return _leg_dimension(active=active_payload, challenger=challenger_payload,
                          active_state=active_state, challenger_state=challenger_state,
                          delta=delta)


def _risk_dimension(active: ActiveOrderEvidence | None,
                    challenger: Mapping[str, Any] | None) -> dict[str, Any]:
    present, missing, unavailable = (
        LegEvidenceState.PRESENT.value, LegEvidenceState.MISSING.value,
        LegEvidenceState.UNAVAILABLE.value)
    entry = challenger.get("entry") if isinstance(challenger, Mapping) else None
    candidate = challenger.get("candidate") if isinstance(challenger, Mapping) else None
    active_payload = None
    active_state = missing
    if active is not None:
        # Inventory result: no exact, order-linked Risk Authority decision is
        # persisted. `paper_risk_decisions` carries no order reference, so any
        # (account, code, side) match would be a time-ordered guess, and the
        # order's own lifecycle status is not risk evidence. The Active risk
        # rejection therefore stays UNAVAILABLE rather than being fabricated.
        active_payload = {
            "risk_rejection_evidence": None,
            "risk_rejection_availability": EvidenceProvenance.UNAVAILABLE.value,
            "reason": "no_order_linked_risk_authority_evidence",
            "detail": {
                "paper_risk_decisions_order_linkage": None,
                "order_lifecycle_status_used": False,
                "exact_linkage_searched": True,
            },
        }
        active_state = unavailable
    challenger_payload = None
    challenger_state = missing
    if isinstance(challenger, Mapping):
        entry_mapping = entry if isinstance(entry, Mapping) else None
        candidate_mapping = candidate if isinstance(candidate, Mapping) else None
        has_entry = entry_mapping is not None and "allowed" in entry_mapping
        challenger_payload = {
            "entry_allowed": ((entry_mapping or {}).get("allowed") if has_entry else None),
            "entry_reasons": (list((entry_mapping or {}).get("reasons") or ())
                              if has_entry else None),
            # No entry decision means no owner-issued risk evidence either.
            "entry_provenance": (EvidenceProvenance.OWNER_ISSUED.value if has_entry
                                 else EvidenceProvenance.UNAVAILABLE.value),
            # R32-C established that this identity is caller-declared evidence.
            # It is reported as declared, never promoted to owner-verified.
            "risk_policy_identity": ((candidate_mapping or {}).get("risk_policy_identity")
                                     if candidate_mapping is not None else None),
            "risk_policy_identity_provenance": EvidenceProvenance.DECLARED.value,
        }
        challenger_state = present if has_entry else missing
    reasons = []
    if active is None:
        reasons.append("active_observation_absent")
    elif active_state == unavailable:
        reasons.append("active_risk_rejection_evidence_absent")
    if challenger is None:
        reasons.append("challenger_observation_absent")
    elif challenger_state == missing:
        reasons.append("challenger_risk_rejection_evidence_absent")
    delta = None
    if active_state == present and challenger_state == present:
        delta = {
            "challenger_entry_allowed": challenger_payload.get("entry_allowed"),
            "challenger_entry_reasons": list(
                challenger_payload.get("entry_reasons") or ()),
            "risk_policy_identity_provenance": EvidenceProvenance.DECLARED.value,
        }
    return _leg_dimension(active=active_payload, challenger=challenger_payload,
                          active_state=active_state, challenger_state=challenger_state,
                          delta=delta, reasons=reasons)


def _observation_comparison(*, key: ObservationKey, active: ActiveOrderEvidence | None,
                            challenger: Mapping[str, Any] | None) -> dict[str, Any]:
    dimensions = {
        "signal": _signal_dimension(active, challenger),
        "decision": _decision_dimension(active, challenger),
        "execution": _execution_dimension(active, challenger),
        "risk_rejection": _risk_dimension(active, challenger),
    }
    has_active = active is not None
    has_challenger = isinstance(challenger, Mapping)
    if has_active and has_challenger:
        alignment = ObservationAlignment.ALIGNED.value
    elif has_active:
        alignment = ObservationAlignment.ACTIVE_EVIDENCE_ONLY.value
    elif has_challenger:
        alignment = ObservationAlignment.CHALLENGER_EVIDENCE_ONLY.value
    else:
        alignment = ObservationAlignment.NO_EVIDENCE.value
    blocking = [f"{name}:{reason}"
                for name, dimension in dimensions.items()
                for reason in dimension["blocking_reasons"]]
    return {
        "observation": key.projection(),
        "alignment": alignment,
        "active_evidence_present": has_active,
        "challenger_evidence_present": has_challenger,
        "dimensions": dimensions,
        "blocking_reasons": list(_reasons(blocking)),
    }


def _coverage(*, expected: tuple[ObservationKey, ...],
              observations: tuple[Mapping[str, Any], ...],
              block_ratio_reason: bool,
              reasons) -> CoverageEvidence:
    total = len(expected)
    if block_ratio_reason:
        # Nothing is comparable, so nothing counts as available: the ratio is 0,
        # not a filtered-out 1.0.
        return CoverageEvidence(
            expected_observations=total, available_observations=0,
            partial_observations=0, missing_observations=0,
            unavailable_observations=total, coverage_ratio=0.0,
            blocking_reasons=_reasons(reasons))
    available = partial = missing = unavailable = 0
    aligned_incomplete = 0
    for item in observations:
        alignment = item["alignment"]
        if alignment == ObservationAlignment.ALIGNED.value:
            required_ok = all(
                item["dimensions"][name]["availability"]
                == DimensionAvailability.AVAILABLE.value
                for name in REQUIRED_COMPARISON_DIMENSIONS
            )
            if required_ok:
                available += 1
            else:
                # Evidence exists on both legs but a required dimension is not
                # owner-complete, so the observation is not a completed
                # comparison and must not be counted as available.
                partial += 1
                aligned_incomplete += 1
        elif alignment == ObservationAlignment.NO_EVIDENCE.value:
            missing += 1
        else:
            partial += 1
    coverage = CoverageEvidence(
        expected_observations=total,
        available_observations=available,
        partial_observations=partial,
        missing_observations=missing,
        unavailable_observations=unavailable,
        coverage_ratio=round(available / total, 6),
        aligned_incomplete_observations=aligned_incomplete,
        blocking_reasons=_reasons(reasons),
    )
    return coverage


def _aggregate_availability(present: int, total: int) -> str:
    if not total or not present:
        return DimensionAvailability.UNAVAILABLE.value
    if present == total:
        return DimensionAvailability.AVAILABLE.value
    return DimensionAvailability.PARTIAL.value


def _signal_aggregate(observations: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    counters = {"both_approved": 0, "both_blocked": 0,
                "active_approved_challenger_blocked": 0,
                "active_blocked_challenger_approved": 0, "unclassified": 0}
    reasons: list[str] = []
    present = 0
    for item in observations:
        delta = item["dimensions"]["signal"]["delta"]
        if not delta:
            reasons.append("signal_delta_unavailable:"
                           f"{item['observation']['symbol']}|{item['observation']['side']}")
            continue
        present += 1
        pair = (str(delta.get("active_outcome")), str(delta.get("challenger_outcome")))
        mapping = {
            ("approved", "approved"): "both_approved",
            ("blocked", "blocked"): "both_blocked",
            ("approved", "blocked"): "active_approved_challenger_blocked",
            ("blocked", "approved"): "active_blocked_challenger_approved",
        }
        bucket = mapping.get(pair)
        if bucket is None:
            counters["unclassified"] += 1
            reasons.append("signal_outcome_vocabulary_unknown:"
                           f"{item['observation']['symbol']}|{item['observation']['side']}")
        else:
            counters[bucket] += 1
    return {
        "availability": _aggregate_availability(present, len(observations)),
        "outcome_pairs": counters,
        "blocking_reasons": list(_reasons(reasons)),
    }


def _decision_aggregate(observations: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    present = 0
    allowed = blocked = 0
    active_decisions: dict[str, int] = {}
    reasons: list[str] = []
    for item in observations:
        delta = item["dimensions"]["decision"]["delta"]
        if not delta:
            reasons.append("decision_delta_unavailable:"
                           f"{item['observation']['symbol']}|{item['observation']['side']}")
            continue
        present += 1
        active_decision = str(delta.get("active_admission_decision") or "")
        active_decisions[active_decision] = active_decisions.get(active_decision, 0) + 1
        challenger_decision = delta.get("challenger_admission_decision")
        if challenger_decision is True:
            allowed += 1
        elif challenger_decision is False:
            blocked += 1
    return {
        "availability": _aggregate_availability(present, len(observations)),
        "challenger_admission_allowed": allowed,
        "challenger_admission_blocked": blocked,
        "active_admission_decisions": {key: active_decisions[key]
                                       for key in sorted(active_decisions)},
        # Two different persisted vocabularies; mapping them is not this layer's
        # authority, so no cross-leg admission conclusion is emitted.
        "cross_vocabulary_admission_relation": DimensionAvailability.UNAVAILABLE.value,
        "cross_vocabulary_relation_defined": False,
        "cross_vocabulary_reason": "no_owner_defined_admission_vocabulary_mapping",
        "blocking_reasons": list(_reasons(reasons)),
    }


def _execution_aggregate(observations: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    present = 0
    active_filled = challenger_filled = 0
    active_requested = challenger_requested = 0
    reasons: list[str] = []
    for item in observations:
        dimension = item["dimensions"]["execution"]
        if not dimension["delta"]:
            reasons.append("execution_delta_unavailable:"
                           f"{item['observation']['symbol']}|{item['observation']['side']}")
            continue
        present += 1
        active = dimension["active"] or {}
        challenger = dimension["challenger"] or {}
        # Only quantities the owner actually produced are summed; an absent
        # quantity is never coerced to zero.
        if isinstance(active.get("execution_fill_quantity"), int):
            active_filled += int(active["execution_fill_quantity"])
        if isinstance(challenger.get("execution_fill_quantity"), int):
            challenger_filled += int(challenger["execution_fill_quantity"])
        if isinstance(active.get("ledger_requested_quantity"), int):
            active_requested += int(active["ledger_requested_quantity"])
        if isinstance(challenger.get("requested_quantity"), int):
            challenger_requested += int(challenger["requested_quantity"])
    total = len(observations)
    if not present:
        # Nothing was comparable, so no total is asserted: an absent quantity is
        # never reported as a zero.
        return {
            "availability": DimensionAvailability.UNAVAILABLE.value,
            "comparable_observations": 0,
            "active_requested_quantity": None,
            "challenger_requested_quantity": None,
            "active_filled_quantity": None,
            "challenger_filled_quantity": None,
            "fill_quantity_delta": None,
            "blocking_reasons": list(_reasons(reasons)),
        }
    return {
        "availability": _aggregate_availability(present, total),
        "comparable_observations": present,
        "active_requested_quantity": active_requested,
        "challenger_requested_quantity": challenger_requested,
        "active_filled_quantity": active_filled,
        "challenger_filled_quantity": challenger_filled,
        "fill_quantity_delta": active_filled - challenger_filled,
        "blocking_reasons": list(_reasons(reasons)),
    }


def _risk_aggregate(observations: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    active_present = challenger_present = 0
    entry_reasons: set[str] = set()
    declared = 0
    reasons: list[str] = []
    for item in observations:
        dimension = item["dimensions"]["risk_rejection"]
        states = dimension["legs"]
        if states["active"] == LegEvidenceState.PRESENT.value:
            active_present += 1
        else:
            reasons.append("active_risk_rejection_evidence_absent:"
                           f"{item['observation']['symbol']}|{item['observation']['side']}")
        challenger = dimension["challenger"] or {}
        if states["challenger"] == LegEvidenceState.PRESENT.value:
            challenger_present += 1
            entry_reasons.update(str(reason)
                                 for reason in challenger.get("entry_reasons") or ())
        else:
            reasons.append("challenger_risk_rejection_evidence_absent:"
                           f"{item['observation']['symbol']}|{item['observation']['side']}")
        if challenger.get("risk_policy_identity") is not None:
            declared += 1
    total = len(observations)
    if not active_present and not challenger_present:
        availability = DimensionAvailability.UNAVAILABLE.value
    elif active_present == total and challenger_present == total:
        availability = DimensionAvailability.AVAILABLE.value
    else:
        availability = DimensionAvailability.PARTIAL.value
    return {
        "availability": availability,
        "active_risk_rejection_observations": active_present,
        "challenger_risk_rejection_observations": challenger_present,
        "challenger_entry_reasons": sorted(entry_reasons),
        "declared_risk_identity_observations": declared,
        "provenance": {
            # No order-linked Risk Authority decision is persisted, so the
            # Active side carries no owner-issued rejection evidence at all.
            "active_rejection_outcome": EvidenceProvenance.UNAVAILABLE.value,
            "challenger_entry_decision": EvidenceProvenance.OWNER_ISSUED.value,
            "challenger_risk_policy_identity": EvidenceProvenance.DECLARED.value,
        },
        "blocking_reasons": list(_reasons(reasons)),
    }


def _order_lifecycle(active_evidence: ActiveComparisonEvidence) -> dict[str, Any]:
    """Active order lifecycle facts, kept apart from any admission decision.

    `paper_orders.status` / `reason` are ledger lifecycle facts. They are
    reported here verbatim and are explicitly **not** used as an entry admission,
    an execution result, or risk evidence.
    """
    rows = [{
        "order_id": order.order_id,
        "symbol": order.symbol,
        "side": order.side,
        "order_status": order.order_status,
        "order_reason": order.order_reason,
        "order_type": order.order_type,
        "requested_quantity": order.requested_quantity,
        "filled_quantity": order.filled_quantity,
        "amount": order.amount,
        "fees": order.fees,
        "realized_pnl": order.realized_pnl,
        "created_at": order.created_at,
    } for order in active_evidence.orders]
    statuses: dict[str, int] = {}
    for row in rows:
        statuses[row["order_status"]] = statuses.get(row["order_status"], 0) + 1
    return {
        "provenance": EvidenceProvenance.OWNER_ISSUED.value,
        "used_as_entry_admission": False,
        "used_as_execution_result": False,
        "used_as_risk_evidence": False,
        "status_counts": {key: statuses[key] for key in sorted(statuses)},
        "orders": rows,
    }


def _turnover(*, active_evidence: ActiveComparisonEvidence,
              shadow_run: SR.ShadowRunEvidence) -> dict[str, Any]:
    shadow_spec = _shadow_spec(shadow_run)
    before = dict(shadow_run.before_state or {})
    after = dict(shadow_run.after_state or {})
    reasons: list[str] = []
    active_amounts = [order.amount for order in active_evidence.orders
                      if order.amount is not None]
    active_raw = round(sum(active_amounts), 4) if active_amounts else None
    if active_raw is None:
        reasons.append("active_traded_notional_absent")
    elif len(active_amounts) != len(active_evidence.orders):
        reasons.append("active_traded_notional_partial")
    try:
        denominator = float(shadow_spec.get("reference_capital"))
    except (TypeError, ValueError):
        denominator = None
    run_turnover = None
    if "gross_turnover" in before and "gross_turnover" in after:
        run_turnover = round(float(after.get("gross_turnover") or 0.0)
                             - float(before.get("gross_turnover") or 0.0), 4)
    else:
        reasons.append("challenger_gross_turnover_absent")
    normalized = None
    if run_turnover is not None and denominator:
        normalized = round(run_turnover / denominator, 6)
    if active_raw is None or run_turnover is None:
        availability = (DimensionAvailability.PARTIAL.value
                        if active_raw is not None or run_turnover is not None
                        else DimensionAvailability.UNAVAILABLE.value)
    else:
        availability = DimensionAvailability.AVAILABLE.value
    # The Active ledger's own reference denominator is not part of this
    # comparison's exact evidence, so the normalized Active side stays unknown
    # rather than borrowing a current NAV.
    reasons.append("active_turnover_denominator_absent")
    return {
        "availability": availability,
        "active_raw_traded_notional": active_raw,
        "challenger_gross_turnover": run_turnover,
        "challenger_denominator": denominator,
        "challenger_normalized_turnover": normalized,
        "active_denominator": None,
        "active_normalized_turnover": None,
        "normalized_turnover_delta": None,
        "raw_traded_notional_delta": (
            round(active_raw - run_turnover, 4)
            if active_raw is not None and run_turnover is not None else None),
        "blocking_reasons": list(_reasons(reasons)),
    }


def _performance(shadow_run: SR.ShadowRunEvidence) -> dict[str, Any]:
    shadow_spec = _shadow_spec(shadow_run)
    environment = _shadow_environment(shadow_run)
    quotes = environment.get("quotes")
    quotes = quotes if isinstance(quotes, Mapping) else {}
    after = dict(shadow_run.after_state or {})
    positions = after.get("positions")
    positions = positions if isinstance(positions, Mapping) else {}
    reasons = ["active_performance_evidence_absent",
               "intraperiod_valuation_series_absent"]
    prices: dict[str, float] = {}
    missing: list[str] = []
    for code in sorted(positions):
        quantity = positions.get(code)
        if not isinstance(quantity, int) or quantity <= 0:
            continue
        quote = quotes.get(code)
        price = quote.get("price") if isinstance(quote, Mapping) else None
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            missing.append(str(code))
            continue
        prices[str(code)] = float(price)
    challenger = None
    try:
        denominator = float(shadow_spec.get("reference_capital"))
    except (TypeError, ValueError):
        denominator = None
    if missing:
        reasons.append("challenger_position_valuation_absent")
        availability = DimensionAvailability.UNAVAILABLE.value
    elif not denominator:
        reasons.append("challenger_reference_capital_absent")
        availability = DimensionAvailability.UNAVAILABLE.value
    else:
        cash = float(after.get("reference_cash") or 0.0)
        reference_nav = round(cash + sum(positions[code] * price
                                         for code, price in sorted(prices.items())), 4)
        pnl = round(reference_nav - denominator, 4)
        challenger = {
            "reference_capital": denominator,
            "reference_nav": reference_nav,
            "pnl": pnl,
            "return_ratio": round(pnl / denominator, 6),
            "valuation_prices": {code: prices[code] for code in sorted(prices)},
            "valuation_source": "frozen_environment_quotes",
            "drawdown": None,
            "drawdown_unavailable_reason": "intraperiod_valuation_series_absent",
        }
        # The Active side has no exact valuation evidence in this envelope, so
        # the section can never be fully available.
        availability = DimensionAvailability.PARTIAL.value
    return {
        "availability": availability,
        "active": None,
        "active_availability": DimensionAvailability.UNAVAILABLE.value,
        "challenger": challenger,
        "challenger_valuation_missing_symbols": missing,
        "blocking_reasons": list(_reasons(reasons)),
    }


def _provenance_map() -> dict[str, Any]:
    return {
        "active.order_lifecycle_columns": EvidenceProvenance.OWNER_ISSUED.value,
        "active.signal_decision": EvidenceProvenance.OWNER_ISSUED.value,
        "active.signal_evidence": EvidenceProvenance.OWNER_ISSUED.value,
        "active.admission_decision": EvidenceProvenance.OWNER_ISSUED.value,
        "active.execution_evidence": EvidenceProvenance.OWNER_ISSUED.value,
        "active.runtime_context": EvidenceProvenance.OWNER_ISSUED.value,
        "active.selection_scores": EvidenceProvenance.OWNER_ISSUED.value,
        # No order-linked Risk Authority decision exists in the Active ledger.
        "active.risk_rejection": EvidenceProvenance.UNAVAILABLE.value,
        "active.turnover_denominator": EvidenceProvenance.UNAVAILABLE.value,
        "active.performance": EvidenceProvenance.UNAVAILABLE.value,
        "challenger.signal": EvidenceProvenance.OWNER_ISSUED.value,
        "challenger.entry": EvidenceProvenance.OWNER_ISSUED.value,
        "challenger.execution": EvidenceProvenance.OWNER_ISSUED.value,
        "challenger.entry_policy": EvidenceProvenance.OWNER_ISSUED.value,
        "challenger.environment": EvidenceProvenance.CAPTURED_INPUT.value,
        "challenger.entry_gate_state": EvidenceProvenance.CAPTURED_INPUT.value,
        "challenger.risk_policy_identity": EvidenceProvenance.DECLARED.value,
        "comparison.deltas": EvidenceProvenance.DERIVED.value,
        "comparison.coverage": EvidenceProvenance.DERIVED.value,
        "comparison.active_order_lifecycle": EvidenceProvenance.OWNER_ISSUED.value,
    }


def _observation_not_comparable(key: ObservationKey,
                               reasons) -> dict[str, Any]:
    dimensions = {
        name: _leg_dimension(active=None, challenger=None,
                             active_state=LegEvidenceState.UNAVAILABLE.value,
                             challenger_state=LegEvidenceState.UNAVAILABLE.value,
                             delta=None, reasons=reasons)
        for name in REQUIRED_COMPARISON_DIMENSIONS
    }
    return {
        "observation": key.projection(),
        "alignment": ObservationAlignment.NOT_COMPARABLE.value,
        "active_evidence_present": None,
        "challenger_evidence_present": None,
        "dimensions": dimensions,
        "blocking_reasons": list(_reasons(reasons)),
    }


def build_shadow_comparison(*, spec: ComparisonSpec,
                            active_evidence: ActiveComparisonEvidence,
                            shadow_run: SR.ShadowRunEvidence) -> ShadowComparisonReport:
    """Build one immutable Active/Challenger comparison report.

    Everything that can influence the result is one of the three explicit
    inputs. There is no database, provider, clock, or latest/current lookup.
    """
    if not isinstance(spec, ComparisonSpec):
        raise TypeError("explicit comparison spec is required")
    if not isinstance(active_evidence, ActiveComparisonEvidence):
        raise TypeError("exact Active comparison evidence is required")
    if not isinstance(shadow_run, SR.ShadowRunEvidence):
        raise TypeError("exact ShadowRun evidence is required")

    shadow_spec = _shadow_spec(shadow_run)
    environment = _shadow_environment(shadow_run)
    identity = environment.get("identity")
    shadow_shared = shared_environment_projection(identity)
    active_shared = [shared_environment_projection(order.runtime_context)
                     for order in active_evidence.orders if order.runtime_context is not None]
    active_shared = active_shared[0] if len({SR.canonical_json(item)
                                             for item in active_shared}) == 1 else None

    blocking = list(_binding_reasons(spec=spec, active_evidence=active_evidence,
                                     shadow_run=shadow_run))
    blocking.extend(_active_leg_reasons(spec=spec, active_evidence=active_evidence))
    if active_shared is None:
        relations = sorted({SR.canonical_json(shared_environment_projection(order.runtime_context))
                            for order in active_evidence.orders
                            if order.runtime_context is not None})
        equality = "MISMATCH" if len(relations) > 1 else "UNAVAILABLE"
    elif SR.canonical_json(active_shared) == SR.canonical_json(shadow_shared):
        equality = "EQUAL"
    else:
        equality = "MISMATCH"
    if equality != "EQUAL":
        blocking.append("environment_mismatch" if equality == "MISMATCH"
                        else "active_shared_environment_unavailable")
    blocking = list(_reasons(blocking))
    blocked = bool(blocking)

    identity_context = environment.get("active_runtime_context")
    environment_identity = {
        "shared_environment_dimensions": shadow_shared,
        "shared_environment_fingerprints": {
            "shadow": SR.fingerprint(shadow_shared),
            "active": SR.fingerprint(active_shared) if active_shared else None,
        },
        "shared_environment_equality": equality,
        "shadow_environment_fingerprint": str(shadow_spec.get("environment_fingerprint") or ""),
        # The ShadowRun's own Active capture fingerprint is recorded as evidence;
        # it is deliberately not an equality assertion here. A full strategy
        # runtime context contains strategy-specific state that may legitimately
        # differ between the two legs (R32-A/C contract).
        "shadow_capture_context_fingerprint": (
            str(identity_context.get("context_fingerprint") or "") or None
            if isinstance(identity_context, Mapping) else None),
        "comparison_active_context_fingerprints": sorted(
            {str(order.runtime_context.get("context_fingerprint") or "")
             for order in active_evidence.orders if order.runtime_context is not None}),
    }

    if blocked:
        challenger_observations: dict[str, Mapping[str, Any]] = {}
    else:
        challenger_observations = _shadow_observations(shadow_run)
    active_observations = active_evidence.orders_by_identity()
    observations = tuple(
        _observation_not_comparable(key, blocking) if blocked
        else _observation_comparison(
            key=key, active=active_observations.get(key.identity),
            challenger=challenger_observations.get(key.identity))
        for key in spec.expected_observations
    )

    coverage = _coverage(expected=spec.expected_observations, observations=observations,
                         block_ratio_reason=blocked, reasons=blocking)
    if blocked:
        signal_delta = _unavailable_section(blocking)
        decision_delta = _unavailable_section(blocking)
        execution = _unavailable_section(blocking)
        risk_rejection = _unavailable_section(blocking)
        turnover = _unavailable_section(blocking)
        performance = _unavailable_section(blocking)
        availability = ComparisonAvailability.UNAVAILABLE.value
    else:
        signal_delta = _signal_aggregate(observations)
        decision_delta = _decision_aggregate(observations)
        execution = _execution_aggregate(observations)
        risk_rejection = _risk_aggregate(observations)
        turnover = _turnover(active_evidence=active_evidence, shadow_run=shadow_run)
        performance = _performance(shadow_run)
        required_ok = all(
            item["dimensions"][name]["availability"]
            == DimensionAvailability.AVAILABLE.value
            for item in observations for name in REQUIRED_COMPARISON_DIMENSIONS
        )
        complete = (coverage.available_observations == coverage.expected_observations)
        availability = (ComparisonAvailability.AVAILABLE.value
                        if required_ok and complete
                        else ComparisonAvailability.PARTIAL.value)

    material = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "comparison_spec": spec.projection(),
        # The full canonical Active envelope (source table, order ids, source
        # schema version, source fingerprint and the exact consumed projection)
        # is persisted so the report can re-verify its own source fingerprint.
        "active_evidence": active_evidence.projection(),
        # Order lifecycle facts live in their own section and are explicitly not
        # used as an admission, an execution result, or risk evidence.
        "active_order_lifecycle": _order_lifecycle(active_evidence),
        "shadow_run_id": shadow_run.run_id,
        "shadow_run_fingerprint": shadow_run.run_fingerprint,
        "environment_identity": environment_identity,
        "active_strategy_stamp": spec.active_comparator.projection(),
        "challenger_strategy_stamp": spec.challenger.projection(),
        "availability": availability,
        "coverage": coverage.projection(),
        "observations": [SR._plain(item) for item in observations],
        "signal_delta": signal_delta,
        "decision_delta": decision_delta,
        "turnover": turnover,
        "execution": execution,
        "risk_rejection": risk_rejection,
        "performance": performance,
        "provenance": _provenance_map(),
        "blocking_reasons": list(blocking),
    }
    report_fingerprint = SR.fingerprint(material)
    return ShadowComparisonReport(
        report_id=report_fingerprint,
        report_fingerprint=report_fingerprint,
        comparison_spec=material["comparison_spec"],
        active_evidence=material["active_evidence"],
        active_order_lifecycle=material["active_order_lifecycle"],
        shadow_run_id=material["shadow_run_id"],
        shadow_run_fingerprint=material["shadow_run_fingerprint"],
        environment_identity=material["environment_identity"],
        active_strategy_stamp=material["active_strategy_stamp"],
        challenger_strategy_stamp=material["challenger_strategy_stamp"],
        availability=material["availability"],
        coverage=material["coverage"],
        observations=observations,
        signal_delta=signal_delta,
        decision_delta=decision_delta,
        turnover=turnover,
        execution=execution,
        risk_rejection=risk_rejection,
        performance=performance,
        provenance=material["provenance"],
        blocking_reasons=tuple(blocking),
    )

