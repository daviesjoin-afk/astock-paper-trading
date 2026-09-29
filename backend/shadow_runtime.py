"""Isolated, deterministic Shadow execution over explicitly frozen facts.

This module owns the Shadow run contract and reference state transition. It has
no database, provider, cache, or wall-clock dependency. Formal Entry and
Execution authorities remain the only owners of those decisions.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

import execution_planner as EP
import market_data_contract as MDC
import simulation_runtime_context as SRC
import signal_service as SIG
import strategy_dsl as DSL
import strategy_lifecycle as SL
import tradability_archive as TA

ENVIRONMENT_SCHEMA_VERSION = "comparable-environment-v1"
SHADOW_RUN_SCHEMA_VERSION = "shadow-run-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ShadowRuntimeError(ValueError):
    """Stable fail-closed Shadow rejection with explicit availability."""

    def __init__(self, availability: str, reason_code: str):
        self.availability = availability
        self.reason_code = reason_code
        super().__init__(reason_code)


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("shadow evidence contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("shadow evidence mapping keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        items = [_plain(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(
            item, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ))
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if hasattr(value, "projection"):
        return _plain(value.projection())
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    raise ValueError(f"unsupported shadow evidence value: {type(value).__name__}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _execution_projection(value: EP.ExecutionDecision | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "executable_now": value.executable_now,
        "fill_quantity": value.fill_quantity,
        "remaining_quantity": value.remaining_quantity,
        "status": value.status,
        "reasons": list(value.reasons),
        "pricing_basis": value.pricing_basis,
        "reference_price": value.reference_price,
        "fill_price": value.fill_price,
        "slippage_amount": value.slippage_amount,
        "fees": value.fees,
        "execution_asof": value.execution_asof,
        "market_evidence": _plain(value.market_evidence),
        "tradability_evidence": _plain(value.tradability_evidence),
        "liquidity_evidence": _plain(value.liquidity_evidence),
        "ruleset_version": value.ruleset_version,
    }


def _instant(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("decision_at must be an exact instant")
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("decision_at must be an exact instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("decision_at must include a timezone")
    return parsed.astimezone(dt.timezone.utc).isoformat()


def _fingerprint_map(value: Mapping[str, str], *, label: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} fingerprints are required")
    output = []
    for key, digest in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} fingerprint identity is invalid")
        name, value_digest = key.strip(), str(digest or "")
        if not name or not _SHA256.fullmatch(value_digest):
            raise ValueError(f"{label} fingerprint is invalid")
        output.append((name, value_digest))
    return tuple(sorted(output))


@dataclass(frozen=True, slots=True)
class ComparableEnvironmentIdentity:
    schema_version: str
    session_date: str
    decision_at: str
    market_policy_name: str
    market_snapshot_fingerprint: str
    symbol_quote_fingerprints: tuple[tuple[str, str], ...]
    symbol_factor_fingerprints: tuple[tuple[str, str], ...]
    tradability_evidence_fingerprints: tuple[tuple[str, str], ...]
    execution_ruleset_identity: str
    environment_fingerprint: str

    @classmethod
    def build(cls, *, session_date: str, decision_at: str,
              market_policy_name: str, market_snapshot_fingerprint: str,
              symbol_quote_fingerprints: Mapping[str, str],
              symbol_factor_fingerprints: Mapping[str, str],
              tradability_evidence_fingerprints: Mapping[str, str],
              execution_ruleset_identity: str):
        day = dt.date.fromisoformat(str(session_date)).isoformat()
        instant = _instant(decision_at)
        policy, ruleset = str(market_policy_name or "").strip(), str(
            execution_ruleset_identity or "").strip()
        if (policy != MDC.EXECUTION_QUOTE_POLICY.name
                or ruleset != EP.SIMULATION_EXECUTION_RULESET):
            raise ValueError("environment market/execution policy must use canonical simulation rules")
        if not policy or not ruleset:
            raise ValueError("market policy and execution ruleset are required")
        if not _SHA256.fullmatch(str(market_snapshot_fingerprint or "")):
            raise ValueError("market snapshot fingerprint is invalid")
        quotes = _fingerprint_map(symbol_quote_fingerprints, label="symbol quote")
        factors = _fingerprint_map(symbol_factor_fingerprints, label="symbol factor")
        tradability = _fingerprint_map(
            tradability_evidence_fingerprints, label="tradability evidence")
        material = {
            "schema_version": ENVIRONMENT_SCHEMA_VERSION,
            "session_date": day,
            "decision_at": instant,
            "market_policy_name": policy,
            "market_snapshot_fingerprint": market_snapshot_fingerprint,
            "symbol_quote_fingerprints": dict(quotes),
            "symbol_factor_fingerprints": dict(factors),
            "tradability_evidence_fingerprints": dict(tradability),
            "execution_ruleset_identity": ruleset,
        }
        return cls(
            schema_version=ENVIRONMENT_SCHEMA_VERSION, session_date=day,
            decision_at=instant, market_policy_name=policy,
            market_snapshot_fingerprint=market_snapshot_fingerprint,
            symbol_quote_fingerprints=quotes, symbol_factor_fingerprints=factors,
            tradability_evidence_fingerprints=tradability,
            execution_ruleset_identity=ruleset,
            environment_fingerprint=fingerprint(material),
        )

    def projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_date": self.session_date,
            "decision_at": self.decision_at,
            "market_policy_name": self.market_policy_name,
            "market_snapshot_fingerprint": self.market_snapshot_fingerprint,
            "symbol_quote_fingerprints": dict(self.symbol_quote_fingerprints),
            "symbol_factor_fingerprints": dict(self.symbol_factor_fingerprints),
            "tradability_evidence_fingerprints": dict(self.tradability_evidence_fingerprints),
            "execution_ruleset_identity": self.execution_ruleset_identity,
            "environment_fingerprint": self.environment_fingerprint,
        }


def _rebuild_runtime_context(context: SRC.ComparableRuntimeContext) -> SRC.ComparableRuntimeContext:
    projected = context.projection()
    return SRC.build_comparable_runtime_context(
        strategy_id=context.strategy_id,
        strategy_version=context.strategy_version,
        strategy_checksum=context.strategy_checksum,
        session_date=context.session_date, decision_at=context.decision_at,
        market_policy_name=context.market_policy_name,
        market_snapshot_fingerprint=context.market_snapshot_fingerprint,
        symbol_quote_fingerprints=dict(context.symbol_quote_fingerprints),
        tradability_evidence_fingerprints=dict(context.tradability_evidence_fingerprints),
        execution_ruleset_version=context.execution_ruleset_version,
        risk_policy_identity=projected["risk_policy_identity"],
        execution_state_fingerprint=context.execution_state_fingerprint,
        entry_gate_state_fingerprint=context.entry_gate_state_fingerprint,
        entry_policy_fingerprint=context.entry_policy_fingerprint,
    )


@dataclass(frozen=True, slots=True)
class FrozenShadowEnvironment:
    """One Active-captured market view plus the owner-issued facts it contains."""

    identity: ComparableEnvironmentIdentity
    active_runtime_context: SRC.ComparableRuntimeContext
    market_reading: Any
    quotes: Mapping[str, Mapping[str, Any]]
    tradability: Mapping[str, Any]
    factor_snapshots: Mapping[str, Mapping[str, Any]]

    def __post_init__(self):
        if not isinstance(self.identity, ComparableEnvironmentIdentity):
            raise TypeError("comparable environment identity is required")
        if not isinstance(self.active_runtime_context, SRC.ComparableRuntimeContext):
            raise ValueError("Active comparable runtime context is required as capture provenance")
        identity = self.identity
        rebuilt_identity = ComparableEnvironmentIdentity.build(
            session_date=identity.session_date, decision_at=identity.decision_at,
            market_policy_name=identity.market_policy_name,
            market_snapshot_fingerprint=identity.market_snapshot_fingerprint,
            symbol_quote_fingerprints=dict(identity.symbol_quote_fingerprints),
            symbol_factor_fingerprints=dict(identity.symbol_factor_fingerprints),
            tradability_evidence_fingerprints=dict(identity.tradability_evidence_fingerprints),
            execution_ruleset_identity=identity.execution_ruleset_identity,
        )
        if rebuilt_identity != identity:
            raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
        active = self.active_runtime_context
        if (active.context_schema_version != SRC.CONTEXT_SCHEMA_VERSION
                or active.session_date != self.identity.session_date
                or active.decision_at != self.identity.decision_at
                or active.market_policy_name != self.identity.market_policy_name
                or active.market_snapshot_fingerprint != self.identity.market_snapshot_fingerprint
                or dict(active.symbol_quote_fingerprints) != dict(self.identity.symbol_quote_fingerprints)
                or dict(active.tradability_evidence_fingerprints)
                != dict(self.identity.tradability_evidence_fingerprints)):
            raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
        if _rebuild_runtime_context(active).context_fingerprint != active.context_fingerprint:
            raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
        if not isinstance(self.market_reading, MDC.MarketDataReading):
            raise ValueError("frozen market reading is required")
        if self.market_reading.policy_name != self.identity.market_policy_name:
            raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
        snapshot = self.market_reading.snapshot
        if snapshot is None or MDC.snapshot_fingerprint(snapshot) != self.identity.market_snapshot_fingerprint:
            raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
        day = self.identity.session_date
        if str(snapshot.as_of or "") != day:
            raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
        quote_map = {str(k): _freeze(dict(v)) for k, v in self.quotes.items()}
        tradability_map = dict(self.tradability)
        factors = {str(k): _freeze(dict(v)) for k, v in self.factor_snapshots.items()}
        if not quote_map or set(quote_map) != set(tradability_map) or set(quote_map) != set(factors):
            raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
        expected_quotes = dict(self.identity.symbol_quote_fingerprints)
        expected_factors = dict(self.identity.symbol_factor_fingerprints)
        expected_tradability = dict(self.identity.tradability_evidence_fingerprints)
        if set(expected_quotes) != set(quote_map):
            raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
        if set(expected_tradability) != {f"{code}@{day}" for code in tradability_map}:
            raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
        if set(expected_factors) != set(factors):
            raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
        for code, factor_snapshot in factors.items():
            if fingerprint(factor_snapshot) != expected_factors[code]:
                raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
        for code, quote in quote_map.items():
            if str(quote.get("code") or "").strip() != code:
                raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
            if _instant(str(quote.get("execution_asof") or "")) != self.identity.decision_at:
                raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
            quote_snapshot = MDC.symbol_quote_snapshot(quote, asof_day=day)
            if quote_snapshot is None or MDC.snapshot_fingerprint(quote_snapshot) != expected_quotes[code]:
                raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
            evidence = tradability_map[code]
            if (not isinstance(evidence, TA.TradabilityDecision)
                    or not evidence.evidence_present
                    or evidence.code != code
                    or evidence.session_date != day
                    or not evidence.decision_time
                    or _instant(evidence.decision_time) != self.identity.decision_at
                    or not isinstance(evidence.can_buy, bool)
                    or not isinstance(evidence.can_sell, bool)
                    or not _SHA256.fullmatch(str(evidence.fingerprint or ""))
                    or evidence.fingerprint != expected_tradability[f"{code}@{day}"]
                    or not evidence.source or not evidence.effective_at or not evidence.observed_at):
                raise ShadowRuntimeError("NOT_COMPARABLE", "frozen_tradability_evidence_mismatch")
        object.__setattr__(self, "quotes", MappingProxyType(quote_map))
        object.__setattr__(self, "tradability", MappingProxyType(tradability_map))
        object.__setattr__(self, "factor_snapshots", MappingProxyType(factors))

    def projection(self) -> dict[str, Any]:
        return {
            "identity": self.identity.projection(),
            "active_runtime_context": self.active_runtime_context.projection(),
            "market_reading": self.market_reading.projection(),
            "quotes": {key: _plain(value) for key, value in self.quotes.items()},
            "tradability": {key: _plain(value) for key, value in self.tradability.items()},
            "factor_snapshots": {key: _plain(value) for key, value in self.factor_snapshots.items()},
        }


@dataclass(frozen=True, slots=True)
class StrategyStamp:
    strategy_id: str
    version: int
    checksum: str

    def __post_init__(self):
        if (not isinstance(self.strategy_id, str) or not self.strategy_id.strip()
                or self.strategy_id != self.strategy_id.strip()
                or isinstance(self.version, bool) or not isinstance(self.version, int)
                or self.version < 1):
            raise ValueError("exact strategy stamp is required")
        if not _SHA256.fullmatch(str(self.checksum or "")):
            raise ValueError("strategy checksum is invalid")

    def projection(self):
        return {"strategy_id": self.strategy_id, "version": self.version,
                "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class ShadowRunSpec:
    challenger: StrategyStamp
    active_comparator: StrategyStamp
    environment_fingerprint: str
    session_date: str
    decision_at: str
    reference_capital: float
    previous_shadow_run_id: str | None = None
    run_schema_version: str = SHADOW_RUN_SCHEMA_VERSION

    def __post_init__(self):
        if not isinstance(self.challenger, StrategyStamp) or not isinstance(self.active_comparator, StrategyStamp):
            raise TypeError("exact challenger and active strategy stamps are required")
        if not _SHA256.fullmatch(str(self.environment_fingerprint or "")):
            raise ValueError("environment fingerprint is invalid")
        object.__setattr__(self, "session_date", dt.date.fromisoformat(self.session_date).isoformat())
        object.__setattr__(self, "decision_at", _instant(self.decision_at))
        capital = float(self.reference_capital)
        if isinstance(self.reference_capital, bool) or not math.isfinite(capital) or capital <= 0:
            raise ValueError("reference capital must be positive and finite")
        object.__setattr__(self, "reference_capital", capital)
        if self.run_schema_version != SHADOW_RUN_SCHEMA_VERSION:
            raise ValueError("shadow run schema version is unsupported")
        if self.previous_shadow_run_id is not None and not _SHA256.fullmatch(self.previous_shadow_run_id):
            raise ValueError("previous ShadowRun identity is invalid")

    def projection(self):
        return {"run_schema_version": self.run_schema_version,
                "challenger": self.challenger.projection(),
                "active_comparator": self.active_comparator.projection(),
                "environment_fingerprint": self.environment_fingerprint,
                "session_date": self.session_date, "decision_at": self.decision_at,
                "reference_capital": self.reference_capital,
                "previous_shadow_run_id": self.previous_shadow_run_id}


@dataclass(frozen=True, slots=True)
class ShadowRuntimeState:
    session_date: str
    reference_cash: float
    positions: Mapping[str, int] = field(default_factory=dict)
    sellable_quantity: Mapping[str, int] = field(default_factory=dict)
    session_consumed_quantity: Mapping[str, int] = field(default_factory=dict)
    gross_turnover: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, "session_date", dt.date.fromisoformat(self.session_date).isoformat())
        if isinstance(self.reference_cash, bool) or not isinstance(self.reference_cash, (int, float)):
            raise ValueError("Shadow reference cash must be numeric")
        if isinstance(self.gross_turnover, bool) or not isinstance(self.gross_turnover, (int, float)):
            raise ValueError("Shadow gross turnover must be numeric")
        cash, turnover = float(self.reference_cash), float(self.gross_turnover)
        if not math.isfinite(cash) or cash < 0 or not math.isfinite(turnover) or turnover < 0:
            raise ValueError("Shadow reference state is invalid")
        for label, values in (("positions", self.positions), ("sellable", self.sellable_quantity),
                              ("consumed", self.session_consumed_quantity)):
            for key, value in values.items():
                if (not isinstance(key, str) or not key.strip() or key != key.strip()
                        or isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    raise ValueError(f"Shadow {label} state is invalid")
        object.__setattr__(self, "reference_cash", cash)
        object.__setattr__(self, "gross_turnover", turnover)
        object.__setattr__(self, "positions", MappingProxyType({str(k): int(v) for k, v in self.positions.items()}))
        object.__setattr__(self, "sellable_quantity", MappingProxyType({str(k): int(v) for k, v in self.sellable_quantity.items()}))
        object.__setattr__(self, "session_consumed_quantity", MappingProxyType({str(k): int(v) for k, v in self.session_consumed_quantity.items()}))

    @classmethod
    def initial(cls, capital: float, session_date: str):
        return cls(reference_cash=float(capital), session_date=session_date)

    def projection(self):
        return {"session_date": self.session_date,
                "reference_cash": self.reference_cash, "positions": dict(self.positions),
                "sellable_quantity": dict(self.sellable_quantity),
                "session_consumed_quantity": dict(self.session_consumed_quantity),
                "gross_turnover": self.gross_turnover}


@dataclass(frozen=True, slots=True)
class ShadowCandidate:
    symbol: str
    side: str
    desired_quantity: int
    entry_state: EP.EntryGateState
    risk_policy_identity: Mapping[str, Any]
    reference_price: float | None = None
    order_type: str = "market"

    def __post_init__(self):
        if not str(self.symbol or "").strip() or self.side not in {"buy", "sell"}:
            raise ValueError("Shadow candidate identity is invalid")
        if (isinstance(self.desired_quantity, bool)
                or not isinstance(self.desired_quantity, int) or self.desired_quantity < 1):
            raise ValueError("Shadow candidate quantity must be positive")
        if not isinstance(self.entry_state, EP.EntryGateState):
            raise TypeError("captured EntryGateState is required")
        if not isinstance(self.risk_policy_identity, Mapping) or not self.risk_policy_identity:
            raise ValueError("captured risk policy identity is required")
        object.__setattr__(self, "risk_policy_identity", _freeze(dict(self.risk_policy_identity)))


@dataclass(frozen=True, slots=True)
class ShadowRunEvidence:
    run_id: str
    run_fingerprint: str
    spec: Mapping[str, Any]
    environment: Mapping[str, Any]
    strategy_definition_fingerprint: str
    before_state: Mapping[str, Any]
    decisions: tuple[Mapping[str, Any], ...]
    after_state: Mapping[str, Any]
    previous_run_fingerprint: str | None

    def projection(self):
        return {"schema_version": SHADOW_RUN_SCHEMA_VERSION, "run_id": self.run_id,
                "run_fingerprint": self.run_fingerprint, "spec": _plain(self.spec),
                "environment": _plain(self.environment),
                "strategy_definition_fingerprint": self.strategy_definition_fingerprint,
                "before_state": _plain(self.before_state),
                "decisions": _plain(self.decisions), "after_state": _plain(self.after_state),
                "previous_run_fingerprint": self.previous_run_fingerprint}


def evaluate_shadow(*, spec: ShadowRunSpec, environment: FrozenShadowEnvironment,
                    strategy_version: Any, lifecycle_state: str,
                    candidates: tuple[ShadowCandidate, ...],
                    previous_run: ShadowRunEvidence | None = None) -> ShadowRunEvidence:
    """Pure DSL Challenger evaluation over owner-resolved, explicit inputs."""
    if spec.environment_fingerprint != environment.identity.environment_fingerprint:
        raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
    if (spec.session_date, spec.decision_at) != (
            environment.identity.session_date, environment.identity.decision_at):
        raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")
    active_context = environment.active_runtime_context
    if (active_context.strategy_id, active_context.strategy_version,
            active_context.strategy_checksum) != (
            spec.active_comparator.strategy_id, spec.active_comparator.version,
            spec.active_comparator.checksum):
        raise ShadowRuntimeError("NOT_COMPARABLE", "active_comparator_exact_strategy_identity_mismatch")
    if lifecycle_state != "shadow":
        raise ValueError("challenger_lifecycle_not_shadow")
    if not candidates:
        raise ValueError("shadow_candidates_required")
    stamp = (str(getattr(strategy_version, "strategy_id", "")),
             int(getattr(strategy_version, "version", 0)),
             str(getattr(strategy_version, "checksum", "")))
    if stamp != (spec.challenger.strategy_id, spec.challenger.version, spec.challenger.checksum):
        raise ValueError("challenger_exact_strategy_identity_mismatch")
    definition = dict(getattr(strategy_version, "definition", {}) or {})
    ast = definition.get("dsl_ast")
    if ast is None:
        raise ValueError("challenger_dsl_unavailable")
    ast = DSL.normalize(ast)

    if spec.previous_shadow_run_id is None:
        if previous_run is not None:
            raise ValueError("unexpected_previous_shadow_run")
        state = ShadowRuntimeState.initial(spec.reference_capital, spec.session_date)
    else:
        if previous_run is None or previous_run.run_id != spec.previous_shadow_run_id:
            raise ValueError("explicit_previous_shadow_run_required")
        previous = previous_run.projection()
        previous_spec = previous["spec"]
        if (previous_spec["challenger"] != spec.challenger.projection()
                or previous_spec["reference_capital"] != spec.reference_capital):
            raise ValueError("previous_shadow_run_chain_mismatch")
        state = _state_for_run(spec, previous_run)

    quote_fingerprints = dict(environment.identity.symbol_quote_fingerprints)
    decisions: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda row: (row.symbol, row.side)):
        code = candidate.symbol
        if code not in environment.factor_snapshots:
            raise ValueError("shadow_candidate_environment_unavailable")
        quote = dict(environment.quotes[code])
        tradability = environment.tradability[code]
        signal_passed = DSL.evaluate(ast, environment.factor_snapshots[code])
        signal_evidence = SIG.signal_evidence(
            quote, asof_day=spec.session_date,
            policy=environment.identity.market_policy_name)
        signal = SIG.decide_signal(passed=signal_passed,
                                   reason="dsl_rule_passed" if signal_passed else "dsl_rule_not_passed",
                                   evidence=signal_evidence)
        entry_result = None
        execution_result = None
        if signal.outcome == "approved":
            shadow_open_codes = frozenset(
                symbol for symbol, quantity in state.positions.items() if quantity > 0
            )
            # Entry authority remains shared, while cash and portfolio occupancy
            # come only from this Shadow chain. Never reuse formal account cash,
            # reservations, positions, or allocator capacity from a caller snapshot.
            shadow_entry_state = replace(
                candidate.entry_state,
                open_codes=shadow_open_codes,
                committed_open_codes=shadow_open_codes,
                pool_open_positions=frozenset(
                    (spec.challenger.strategy_id, symbol) for symbol in shadow_open_codes
                ),
                pool_limit=max(1, int(candidate.entry_state.position_limit)),
                capacity_available=True,
                seat_reserve={
                    "reserved": False, "owner": None, "interest": 0,
                    "deadline": None, "planner": EP.EXECUTION_PLANNER_VERSION,
                },
                pending_cash=0.0,
                shared_cash=state.reference_cash,
                allocation_source=None,
                allocation_version=None,
            )
            entry_state_fingerprint = EP.entry_gate_state_fingerprint(shadow_entry_state)
            execution_state = EP.ExecutionStateSnapshot(
                buying_power=state.reference_cash if candidate.side == "buy" else None,
                sellable_quantity=state.sellable_quantity.get(code, 0),
                already_filled_quantity=0,
                same_day_consumed_quantity=state.session_consumed_quantity.get(code, 0),
            )
            entry_policy_fp = EP.execution_policy_fingerprint(spec.challenger.strategy_id)
            strategy_runtime_context = SRC.build_comparable_runtime_context(
                strategy_id=spec.challenger.strategy_id,
                strategy_version=spec.challenger.version,
                strategy_checksum=spec.challenger.checksum,
                session_date=spec.session_date, decision_at=spec.decision_at,
                market_policy_name=environment.identity.market_policy_name,
                market_snapshot_fingerprint=environment.identity.market_snapshot_fingerprint,
                symbol_quote_fingerprints=quote_fingerprints,
                tradability_evidence_fingerprints=dict(
                    environment.identity.tradability_evidence_fingerprints),
                execution_ruleset_version=environment.identity.execution_ruleset_identity,
                risk_policy_identity=candidate.risk_policy_identity,
                execution_state_fingerprint=EP.execution_state_fingerprint(execution_state),
                entry_gate_state_fingerprint=entry_state_fingerprint,
                entry_policy_fingerprint=entry_policy_fp,
            )
            estimated_price, desired_amount, desired_fees = EP.estimate_execution_terms(
                candidate.reference_price or float(quote.get("price") or 0),
                candidate.desired_quantity, candidate.side,
            )
            entry_result = EP.evaluate_entry_state(
                state=shadow_entry_state, account_id=spec.challenger.strategy_id,
                code=code, side=candidate.side, quote=quote, asof_day=spec.session_date,
                amount=desired_amount, fees=desired_fees,
                runtime_context=strategy_runtime_context,
            )
            if entry_result["allowed"]:
                order_mapping = {
                    "code": code, "strategy_id": spec.challenger.strategy_id,
                    "strategy_version": spec.challenger.version,
                    "strategy_checksum": spec.challenger.checksum,
                }
                context = EP.execution_context_from_state(
                    order=order_mapping, quote=quote, asof_day=spec.session_date,
                    market_reading=environment.market_reading, tradability=tradability,
                    state=execution_state, runtime_context=strategy_runtime_context,
                )
                intent = EP.PersistedOrderIntent(
                    order_id=1, account_id=spec.challenger.strategy_id,
                    cycle_id=None, strategy_id=spec.challenger.strategy_id,
                    strategy_version=spec.challenger.version,
                    strategy_checksum=spec.challenger.checksum, signal_id=None,
                    symbol=code, side=candidate.side,
                    desired_quantity=int(candidate.desired_quantity), intent_at=spec.decision_at,
                    reference_price=candidate.reference_price,
                    order_type=candidate.order_type,
                )
                execution_result = EP.evaluate_simulated_execution(intent, context)
                if execution_result.fill_quantity:
                    filled = int(execution_result.fill_quantity)
                    amount = round(filled * float(execution_result.fill_price), 2)
                    positions = dict(state.positions)
                    sellable = dict(state.sellable_quantity)
                    cash = state.reference_cash
                    if candidate.side == "buy":
                        positions[code] = positions.get(code, 0) + filled
                        cash = round(cash - amount - execution_result.fees, 2)
                    else:
                        positions[code] = max(0, positions.get(code, 0) - filled)
                        sellable[code] = max(0, sellable.get(code, 0) - filled)
                        cash = round(cash + amount - execution_result.fees, 2)
                    consumed = dict(state.session_consumed_quantity)
                    consumed[code] = consumed.get(code, 0) + filled
                    state = ShadowRuntimeState(
                        session_date=spec.session_date,
                        reference_cash=cash, positions=positions,
                        sellable_quantity=sellable,
                        session_consumed_quantity=consumed,
                        gross_turnover=state.gross_turnover + amount,
                    )
        decisions.append({
            "symbol": code, "side": candidate.side,
            "signal": signal.projection(),
            "entry": entry_result,
            "execution": _execution_projection(execution_result),
        })

    material = {
        "schema_version": SHADOW_RUN_SCHEMA_VERSION,
        "spec": spec.projection(),
        "environment": environment.projection(),
        "strategy_definition_fingerprint": fingerprint(definition),
        "before_state": state_before_projection(spec, previous_run),
        "decisions": decisions,
        "after_state": state.projection(),
        "previous_run_fingerprint": previous_run.run_fingerprint if previous_run else None,
    }
    run_fingerprint = fingerprint(material)
    return ShadowRunEvidence(
        run_id=run_fingerprint, run_fingerprint=run_fingerprint,
        spec=material["spec"], environment=material["environment"],
        strategy_definition_fingerprint=material["strategy_definition_fingerprint"],
        before_state=material["before_state"], decisions=tuple(decisions),
        after_state=material["after_state"],
        previous_run_fingerprint=material["previous_run_fingerprint"],
    )


def state_before_projection(spec: ShadowRunSpec,
                            previous_run: ShadowRunEvidence | None) -> dict[str, Any]:
    if previous_run is None:
        return ShadowRuntimeState.initial(spec.reference_capital, spec.session_date).projection()
    return _state_for_run(spec, previous_run).projection()


def _state_for_run(spec: ShadowRunSpec, previous_run: ShadowRunEvidence) -> ShadowRuntimeState:
    previous_spec = previous_run.spec
    if previous_spec["challenger"] != spec.challenger.projection():
        raise ValueError("previous_shadow_run_chain_mismatch")
    if float(previous_spec["reference_capital"]) != spec.reference_capital:
        raise ValueError("previous_shadow_run_chain_mismatch")
    previous_instant = dt.datetime.fromisoformat(previous_spec["decision_at"])
    current_instant = dt.datetime.fromisoformat(spec.decision_at)
    if current_instant <= previous_instant:
        raise ValueError("shadow_continuation_decision_at_not_increasing")
    state = ShadowRuntimeState(**_plain(previous_run.after_state))
    if state.session_date != previous_spec["session_date"]:
        raise ValueError("previous_shadow_run_state_session_mismatch")
    if spec.session_date < state.session_date:
        raise ValueError("shadow_continuation_session_not_increasing")
    if spec.session_date == state.session_date:
        return state
    # Across sessions, yesterday's holdings become sellable and daily execution
    # consumption resets. Same-session continuations retain both exact values.
    return replace(
        state, session_date=spec.session_date,
        sellable_quantity=state.positions, session_consumed_quantity={},
    )


def resolve_exact_shadow_strategy(conn, stamp: StrategyStamp):
    """Read one explicit immutable version and its R31 state; never read head."""
    import strategy_registry as SR

    version = SR.get_version(stamp.strategy_id, stamp.version,
                             checksum=stamp.checksum, conn=conn)
    if version is None:
        raise ValueError("challenger_exact_strategy_version_unavailable")
    state = SL.get_state(conn, stamp.strategy_id, stamp.version, checksum=stamp.checksum)
    if state is None or state.get("state") != "shadow":
        raise ValueError("challenger_lifecycle_not_shadow")
    return version, state["state"]

