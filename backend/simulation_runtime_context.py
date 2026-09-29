"""Immutable identity for one comparable strategy simulation decision.

This module is a pure contract. It does not discover facts or read runtime
state; callers must pass exact identities issued by their owning domains.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CONTEXT_SCHEMA_VERSION = "comparable-runtime-context-v3"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

ACTIVE_CONTEXT_UNAVAILABLE_REASONS = frozenset({
    "missing_strategy_identity",
    "missing_market_snapshot",
    "missing_quote_identity",
    "missing_tradability_evidence",
    "missing_execution_state",
    "missing_entry_state",
    "missing_policy_identity",
    "strategy_cycle_identity_mismatch",
    "invalid_runtime_context_inputs",
})


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("runtime context contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("runtime context mapping keys must be strings")
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    raise ValueError(f"unsupported runtime context value: {type(value).__name__}")


def _json(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _identity_map(value: Mapping[str, str], *, label: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} identity is required")
    pairs = []
    for key, fingerprint in value.items():
        name = str(key).strip()
        digest = str(fingerprint or "")
        if not name or not _SHA256.fullmatch(digest):
            raise ValueError(f"{label} identity is invalid")
        pairs.append((name, digest))
    return tuple(sorted(pairs))


def _instant(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("decision_at must be a timezone-aware instant")
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("decision_at must be a timezone-aware instant") from exc
    if parsed.tzinfo is None:
        # Existing A-share execution timestamps without an offset are interpreted
        # in the exchange's fixed UTC+08:00 business timezone.
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    if parsed.utcoffset() is None:
        raise ValueError("decision_at must be an exact instant")
    return parsed.astimezone(dt.timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ComparableRuntimeContext:
    context_schema_version: str
    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    session_date: str
    decision_at: str
    market_policy_name: str
    market_snapshot_fingerprint: str
    symbol_quote_fingerprints: tuple[tuple[str, str], ...]
    tradability_evidence_fingerprints: tuple[tuple[str, str], ...]
    execution_ruleset_version: str
    risk_policy_identity: str
    execution_state_fingerprint: str | None
    entry_gate_state_fingerprint: str | None
    entry_policy_fingerprint: str | None
    context_fingerprint: str

    def projection(self) -> dict[str, Any]:
        return {
            "context_schema_version": self.context_schema_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_checksum": self.strategy_checksum,
            "session_date": self.session_date,
            "decision_at": self.decision_at,
            "market_policy_name": self.market_policy_name,
            "market_snapshot_fingerprint": self.market_snapshot_fingerprint,
            "symbol_quote_fingerprints": dict(self.symbol_quote_fingerprints),
            "tradability_evidence_fingerprints": dict(self.tradability_evidence_fingerprints),
            "execution_ruleset_version": self.execution_ruleset_version,
            "risk_policy_identity": json.loads(self.risk_policy_identity),
            "execution_state_fingerprint": self.execution_state_fingerprint,
            "entry_gate_state_fingerprint": self.entry_gate_state_fingerprint,
            "entry_policy_fingerprint": self.entry_policy_fingerprint,
            "context_fingerprint": self.context_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ActiveRuntimeContextResult:
    """Explicit outcome of capturing one Active decision's comparable inputs."""

    availability: str
    context: ComparableRuntimeContext | None = None
    reason_code: str | None = None

    def __post_init__(self):
        if self.availability not in {"AVAILABLE", "UNAVAILABLE"}:
            raise ValueError("runtime context availability is invalid")
        if self.availability == "AVAILABLE":
            if not isinstance(self.context, ComparableRuntimeContext) or self.reason_code is not None:
                raise ValueError("available runtime context result is inconsistent")
        elif self.context is not None or self.reason_code not in ACTIVE_CONTEXT_UNAVAILABLE_REASONS:
            raise ValueError("unavailable runtime context result needs a stable reason")

    @classmethod
    def available(cls, context: ComparableRuntimeContext):
        return cls("AVAILABLE", context=context)

    @classmethod
    def unavailable(cls, reason_code: str):
        return cls("UNAVAILABLE", reason_code=str(reason_code or ""))

    def projection(self) -> dict[str, Any]:
        return {
            "availability": self.availability,
            "reason_code": self.reason_code,
            "runtime_context": self.context.projection() if self.context else None,
            "runtime_context_fingerprint": (
                self.context.context_fingerprint if self.context else None
            ),
        }


def build_comparable_runtime_context(
    *, strategy_id: str, strategy_version: int, strategy_checksum: str,
    session_date: str, decision_at: str, market_policy_name: str,
    market_snapshot_fingerprint: str,
    symbol_quote_fingerprints: Mapping[str, str],
    tradability_evidence_fingerprints: Mapping[str, str],
    execution_ruleset_version: str, risk_policy_identity: Mapping[str, Any],
    execution_state_fingerprint: str | None = None,
    entry_gate_state_fingerprint: str | None = None,
    entry_policy_fingerprint: str | None = None,
) -> ComparableRuntimeContext:
    """Build the same context identity from the same explicit semantic inputs."""
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id or isinstance(strategy_version, bool) or not isinstance(strategy_version, int) or strategy_version < 1:
        raise ValueError("exact strategy identity is required")
    if not _SHA256.fullmatch(str(strategy_checksum or "")):
        raise ValueError("exact strategy checksum is invalid")
    try:
        day = dt.date.fromisoformat(str(session_date)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("session_date must be an ISO date") from exc
    decision = _instant(decision_at)
    if not str(market_policy_name or "").strip():
        raise ValueError("market policy identity is required")
    if not _SHA256.fullmatch(str(market_snapshot_fingerprint or "")):
        raise ValueError("market snapshot identity is invalid")
    quotes = _identity_map(symbol_quote_fingerprints, label="symbol quote")
    tradability = _identity_map(tradability_evidence_fingerprints, label="tradability")
    if not str(execution_ruleset_version or "").strip():
        raise ValueError("execution ruleset identity is required")
    if not isinstance(risk_policy_identity, Mapping) or not risk_policy_identity:
        raise ValueError("risk policy identity is required")
    if execution_state_fingerprint is None and entry_gate_state_fingerprint is None:
        raise ValueError("decision state identity is required")
    if (entry_gate_state_fingerprint is None) != (entry_policy_fingerprint is None):
        raise ValueError("entry policy identity must accompany entry state identity")
    for label, fingerprint in (
        ("execution state", execution_state_fingerprint),
        ("entry gate state", entry_gate_state_fingerprint),
        ("entry policy", entry_policy_fingerprint),
    ):
        if fingerprint is not None and not _SHA256.fullmatch(str(fingerprint)):
            raise ValueError(f"{label} identity is invalid")
    risk_json = _json(risk_policy_identity)
    identity = {
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "strategy_checksum": strategy_checksum,
        "session_date": day,
        "decision_at": decision,
        "market_policy_name": str(market_policy_name),
        "market_snapshot_fingerprint": market_snapshot_fingerprint,
        "symbol_quote_fingerprints": dict(quotes),
        "tradability_evidence_fingerprints": dict(tradability),
        "execution_ruleset_version": str(execution_ruleset_version),
        "risk_policy_identity": json.loads(risk_json),
        "execution_state_fingerprint": execution_state_fingerprint,
        "entry_gate_state_fingerprint": entry_gate_state_fingerprint,
        "entry_policy_fingerprint": entry_policy_fingerprint,
    }
    return ComparableRuntimeContext(
        context_schema_version=CONTEXT_SCHEMA_VERSION,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        strategy_checksum=strategy_checksum,
        session_date=day,
        decision_at=decision,
        market_policy_name=str(market_policy_name),
        market_snapshot_fingerprint=str(market_snapshot_fingerprint),
        symbol_quote_fingerprints=quotes,
        tradability_evidence_fingerprints=tradability,
        execution_ruleset_version=str(execution_ruleset_version),
        risk_policy_identity=risk_json,
        execution_state_fingerprint=(str(execution_state_fingerprint)
                                     if execution_state_fingerprint is not None else None),
        entry_gate_state_fingerprint=(str(entry_gate_state_fingerprint)
                                      if entry_gate_state_fingerprint is not None else None),
        entry_policy_fingerprint=(str(entry_policy_fingerprint)
                                  if entry_policy_fingerprint is not None else None),
        context_fingerprint=_sha256(identity),
    )
