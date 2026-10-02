# -*- coding: utf-8 -*-
"""Pure, deterministic contract for exact multi-strategy portfolio facts.

This module assembles caller-captured owner evidence. It does not read a
database, clock, provider, current strategy head, or portfolio policy.
"""
from __future__ import annotations

import hashlib
import json
import re
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

CONTRACT_VERSION = "portfolio-runtime-facts-v1"
SCHEMA_VERSION = "portfolio-runtime-snapshot-v1"
AVAILABLE = "AVAILABLE"
PARTIAL = "PARTIAL"
UNAVAILABLE = "UNAVAILABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"
STATUSES = (AVAILABLE, PARTIAL, UNAVAILABLE, NOT_APPLICABLE)
PROVENANCES = ("OWNER_ISSUED", "CAPTURED_INPUT", "DERIVED", "UNAVAILABLE")
DIMENSIONS = (
    "capital", "strategy_exposure", "concentration", "turnover",
    "risk_consumption", "signal_conflicts", "capacity", "correlation",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PortfolioRuntimeError(ValueError):
    """Invalid identity or evidence in a portfolio runtime snapshot."""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(v) for v in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise PortfolioRuntimeError("portfolio_fact_not_json_serializable")


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(k): _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class PortfolioDimension:
    name: str
    status: str
    facts: Mapping
    provenance: str
    source_identity: str | None = None
    source_fingerprint: str | None = None
    blocking_reasons: tuple[str, ...] = ()

    def __post_init__(self):
        if self.name not in DIMENSIONS:
            raise PortfolioRuntimeError("unknown_portfolio_dimension")
        if self.status not in STATUSES:
            raise PortfolioRuntimeError("unknown_portfolio_dimension_status")
        if self.provenance not in PROVENANCES:
            raise PortfolioRuntimeError("unknown_portfolio_dimension_provenance")
        if not isinstance(self.facts, Mapping):
            raise PortfolioRuntimeError("portfolio_dimension_facts_must_be_mapping")
        reasons = tuple(sorted({str(v) for v in self.blocking_reasons if str(v)}))
        if self.status in (UNAVAILABLE, NOT_APPLICABLE) and not reasons:
            raise PortfolioRuntimeError("nonavailable_dimension_requires_reason")
        if self.status == NOT_APPLICABLE and self.facts:
            raise PortfolioRuntimeError("not_applicable_dimension_must_be_empty")
        if self.status == UNAVAILABLE and self.provenance != "UNAVAILABLE":
            raise PortfolioRuntimeError("unavailable_dimension_provenance_mismatch")
        if self.status in (AVAILABLE, PARTIAL) and self.provenance == "UNAVAILABLE":
            raise PortfolioRuntimeError("available_dimension_provenance_required")
        if self.source_fingerprint is not None and not _SHA256.fullmatch(
                str(self.source_fingerprint)):
            raise PortfolioRuntimeError("portfolio_source_fingerprint_invalid")
        object.__setattr__(self, "facts", _freeze(self.facts))
        object.__setattr__(self, "blocking_reasons", reasons)

    def projection(self) -> dict:
        return {"name": self.name, "status": self.status,
                "facts": _thaw(self.facts), "provenance": self.provenance,
                "source_identity": self.source_identity,
                "source_fingerprint": self.source_fingerprint,
                "blocking_reasons": list(self.blocking_reasons)}


@dataclass(frozen=True, slots=True)
class PortfolioRuntimeSnapshot:
    snapshot_id: str
    snapshot_fingerprint: str
    cycle_id: int
    asof_day: str
    decision_at: str
    cycle_identity: Mapping
    strategy_pins: tuple[Mapping, ...]
    economic_owner_ids: tuple[str, ...]
    execution_participant_ids: tuple[str, ...]
    risk_exit_participant_ids: tuple[str, ...]
    source_identities: Mapping
    market_evidence_identity: str | None
    dimensions: tuple[PortfolioDimension, ...]
    contract_version: str = CONTRACT_VERSION
    schema_version: str = SCHEMA_VERSION

    def projection(self) -> dict:
        return {"schema_version": self.schema_version,
                "snapshot_id": self.snapshot_id,
                "snapshot_fingerprint": self.snapshot_fingerprint,
                "contract_version": self.contract_version,
                "cycle_id": self.cycle_id, "asof_day": self.asof_day,
                "decision_at": self.decision_at,
                "cycle_identity": _thaw(self.cycle_identity),
                "strategy_pins": [_thaw(v) for v in self.strategy_pins],
                "economic_owner_ids": list(self.economic_owner_ids),
                "execution_participant_ids": list(self.execution_participant_ids),
                "risk_exit_participant_ids": list(self.risk_exit_participant_ids),
                "source_identities": _thaw(self.source_identities),
                "market_evidence_identity": self.market_evidence_identity,
                "dimensions": [v.projection() for v in self.dimensions]}

    def fingerprint_material(self) -> dict:
        value = self.projection()
        value.pop("snapshot_id")
        value.pop("snapshot_fingerprint")
        return value


def build_portfolio_runtime_snapshot(*, cycle_id, asof_day: str, decision_at: str,
                                     cycle_identity: Mapping,
                                     strategy_pins, economic_owner_ids,
                                     execution_participant_ids,
                                     risk_exit_participant_ids,
                                     source_identities: Mapping,
                                     market_evidence_identity: str | None,
                                     dimensions) -> PortfolioRuntimeSnapshot:
    """Build one exact snapshot from fully explicit identities and owner facts."""
    try:
        cycle = int(cycle_id)
    except (TypeError, ValueError) as exc:
        raise PortfolioRuntimeError("explicit_cycle_id_required") from exc
    if cycle <= 0:
        raise PortfolioRuntimeError("explicit_cycle_id_required")
    try:
        day = dt.date.fromisoformat(str(asof_day)).isoformat()
    except (TypeError, ValueError) as exc:
        raise PortfolioRuntimeError("explicit_asof_day_required") from exc
    try:
        decision = dt.datetime.fromisoformat(str(decision_at)).isoformat()
    except (TypeError, ValueError) as exc:
        raise PortfolioRuntimeError("explicit_decision_at_required") from exc
    parsed_decision = dt.datetime.fromisoformat(decision)
    if parsed_decision.tzinfo is None:
        raise PortfolioRuntimeError("explicit_decision_at_must_include_timezone")
    if day > parsed_decision.date().isoformat():
        raise PortfolioRuntimeError("asof_day_after_decision_at")
    if not isinstance(cycle_identity, Mapping) or not cycle_identity:
        raise PortfolioRuntimeError("exact_cycle_identity_required")
    pins = tuple(sorted((_freeze(pin) for pin in strategy_pins),
                        key=lambda p: (p["strategy_id"], p["strategy_version"],
                                       p["strategy_checksum"], p["account_id"])))
    account_ids = [str(pin.get("account_id") or "") for pin in pins]
    if any(not account for account in account_ids) or len(set(account_ids)) != len(account_ids):
        raise PortfolioRuntimeError("exact_strategy_pins_invalid")
    for pin in pins:
        if (not pin.get("strategy_id") or int(pin.get("strategy_version", 0)) < 1
                or not _SHA256.fullmatch(str(pin.get("strategy_checksum") or ""))):
            raise PortfolioRuntimeError("exact_strategy_pin_identity_invalid")
    owners = tuple(sorted({str(v) for v in economic_owner_ids}))
    if set(account_ids) != set(owners):
        raise PortfolioRuntimeError("exact_strategy_pins_must_cover_economic_owners")
    execution = tuple(sorted({str(v) for v in execution_participant_ids}))
    risk_exit = tuple(sorted({str(v) for v in risk_exit_participant_ids}))
    if not set(execution).issubset(owners):
        raise PortfolioRuntimeError("portfolio_participant_scope_inconsistent")
    dims = tuple(sorted(dimensions, key=lambda d: d.name))
    if any(not isinstance(d, PortfolioDimension) for d in dims):
        raise PortfolioRuntimeError("portfolio_dimensions_must_be_typed")
    if [d.name for d in dims] != sorted(DIMENSIONS):
        raise PortfolioRuntimeError("portfolio_dimensions_must_be_complete_and_unique")
    material = {"schema_version": SCHEMA_VERSION, "contract_version": CONTRACT_VERSION,
                "cycle_id": cycle, "asof_day": day, "decision_at": decision,
                "cycle_identity": _thaw(_freeze(cycle_identity)),
                "strategy_pins": [_thaw(pin) for pin in pins],
                "economic_owner_ids": list(owners),
                "execution_participant_ids": list(execution),
                "risk_exit_participant_ids": list(risk_exit),
                "source_identities": _thaw(_freeze(source_identities)),
                "market_evidence_identity": market_evidence_identity,
                "dimensions": [d.projection() for d in dims]}
    fingerprint = _sha(material)
    return PortfolioRuntimeSnapshot(
        snapshot_id=fingerprint, snapshot_fingerprint=fingerprint, cycle_id=cycle,
        asof_day=day, decision_at=decision, cycle_identity=_freeze(cycle_identity),
        strategy_pins=pins, economic_owner_ids=owners,
        execution_participant_ids=execution, risk_exit_participant_ids=risk_exit,
        source_identities=_freeze(source_identities),
        market_evidence_identity=market_evidence_identity, dimensions=dims)


def verify_snapshot_fingerprint(snapshot: PortfolioRuntimeSnapshot) -> bool:
    return (isinstance(snapshot, PortfolioRuntimeSnapshot)
            and snapshot.snapshot_id == snapshot.snapshot_fingerprint
            and _sha(snapshot.fingerprint_material()) == snapshot.snapshot_fingerprint)


__all__ = ["CONTRACT_VERSION", "SCHEMA_VERSION", "DIMENSIONS", "AVAILABLE", "PARTIAL",
           "UNAVAILABLE", "NOT_APPLICABLE", "PortfolioRuntimeError", "PortfolioDimension",
           "PortfolioRuntimeSnapshot", "build_portfolio_runtime_snapshot",
           "verify_snapshot_fingerprint"]
