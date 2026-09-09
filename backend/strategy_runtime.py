"""One immutable, cacheable runtime contract for a strategy version.

All consumers receive the same pinned definition, DSL, risk and execution
profiles.  This module deliberately has no order-placement or network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any

import execution_profiles as EP
import paper_allocation as PA
import strategy_dsl_schema as DSL
import strategy_registry as SR
from strategy_risk_fingerprint import StrategyRiskFingerprint, compile_strategy_risk_fingerprint
from strategy_risk_profiles import StrategyRiskProfile, compile_strategy_risk_profile


@dataclass(frozen=True)
class EvolutionControlProfile:
    enabled: bool
    lifecycle_stage: str
    interval_hours: int


@dataclass(frozen=True)
class StrategyParameterSchema:
    version: str
    editable: tuple[str, ...]
    immutable: tuple[str, ...]


@dataclass(frozen=True)
class StrategyRuntimeContext:
    strategy_id: str
    version: int
    checksum: str
    definition: dict[str, Any]
    compiled_dsl: dict[str, Any] | None
    risk_fingerprint: StrategyRiskFingerprint
    risk_profile: StrategyRiskProfile
    execution_profile: dict[str, Any]
    lifecycle_stage: str
    capital_scale: float
    allocation_runtime: PA.StrategyRuntime
    evolution_control: EvolutionControlProfile
    parameter_schema: StrategyParameterSchema
    settings_revision: str


_CACHE: dict[tuple[str, int, str, str], StrategyRuntimeContext] = {}


def settings_revision(conn: sqlite3.Connection) -> str:
    """A cheap revision token that invalidates contexts after settings writes."""
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(id),0),COALESCE(MAX(created_at),'') FROM paper_runtime_settings_audit"
        ).fetchone()
        return f"{row[0]}:{row[1]}"
    except sqlite3.Error:
        return "0:"


def clear_cache() -> None:
    _CACHE.clear()


def get_context(conn: sqlite3.Connection, strategy_id: str, *, settings_rev: str | None = None) -> StrategyRuntimeContext:
    spec = SR.get(strategy_id, conn=conn)
    if spec is None:
        raise ValueError("unknown strategy id")
    version = SR.get_version(strategy_id, conn=conn)
    if version is None:
        raise ValueError("strategy has no immutable version")
    revision = settings_rev if settings_rev is not None else settings_revision(conn)
    key = (spec.id, version.version, version.checksum, revision)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    definition = dict(version.definition)
    ast = definition.get("dsl_ast")
    compiled = DSL.normalize(ast) if ast is not None else None
    fingerprint = compile_strategy_risk_fingerprint(compiled, definition.get("metadata"))
    risk = compile_strategy_risk_profile(fingerprint)
    execution = EP.execution_profile_for(fingerprint)
    soft = risk.soft_limits
    allocation = PA.StrategyRuntime(
        strategy_id=spec.id,
        base_priority=max(float(soft.get("max_exposure", 0.65)), 0.01),
        max_positions=int(soft.get("max_positions", 3)),
        own_exposure_cap_pct=float(soft.get("max_exposure", 0.65)),
        lifecycle_stage="standard" if spec.status == "active" else "shadow",
    )
    context = StrategyRuntimeContext(
        strategy_id=spec.id, version=version.version, checksum=version.checksum,
        definition=definition, compiled_dsl=compiled, risk_fingerprint=fingerprint,
        risk_profile=risk, execution_profile=execution,
        lifecycle_stage=allocation.lifecycle_stage,
        capital_scale=PA.stage_capital_scale(allocation)[0],
        allocation_runtime=allocation,
        evolution_control=EvolutionControlProfile(
            enabled=spec.status == "active", lifecycle_stage=allocation.lifecycle_stage,
            interval_hours=24,
        ),
        parameter_schema=StrategyParameterSchema(
            version="strategy-parameters-v1",
            editable=("metadata", "dsl_ast"),
            immutable=("strategy_id", "version", "checksum"),
        ),
        settings_revision=revision,
    )
    _CACHE[key] = context
    return context


def active_contexts(conn: sqlite3.Connection, *, settings_rev: str | None = None) -> tuple[StrategyRuntimeContext, ...]:
    revision = settings_rev if settings_rev is not None else settings_revision(conn)
    return tuple(get_context(conn, strategy_id, settings_rev=revision) for strategy_id in SR.active_ids(conn=conn))
