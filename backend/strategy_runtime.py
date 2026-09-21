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
from strategy_parameter_schema import StrategyParameterSchema
from strategy_risk_fingerprint import StrategyRiskFingerprint, compile_strategy_risk_fingerprint
from strategy_risk_profiles import StrategyRiskProfile, compile_strategy_risk_profile


@dataclass(frozen=True)
class EvolutionControlProfile:
    enabled: bool
    lifecycle_stage: str
    interval_hours: int


def lifecycle_stage_for(spec) -> str:
    """把注册表里的策略生命周期翻译成分配层的资金阶段（PR-26）。

    PR-08 定义了 shadow/pilot/standard/mature/quarantined 五档资金系数，
    但此前 ``get_context`` 只会算 ``active ? standard : shadow``——新策略
    一上线就拿到满额预算，生命周期实际只是个原语。现在的取值顺序：

    1. 定义元数据显式指定（``metadata.lifecycle_stage`` 或
       ``metadata.allocation.lifecycle_stage``）——人工晋升/隔离的入口；
    2. 否则按注册表生命周期状态推导：draft→shadow、validated→pilot、
       paused/retiring/archived→quarantined、active→内置 standard /
       用户自建 pilot（新策略默认试点，验证后再人工晋升）；
    3. 未知状态 fail-closed → quarantined（不部署新资金）。
    """
    metadata = getattr(spec, "metadata", None) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    allocation_meta = metadata.get("allocation")
    explicit = metadata.get("lifecycle_stage")
    if explicit is None and isinstance(allocation_meta, dict):
        explicit = allocation_meta.get("lifecycle_stage")
    stage = str(explicit or "").strip().lower()
    if stage in PA.LIFECYCLE_STAGES:
        return stage
    status = str(getattr(spec, "status", "") or "").strip().lower()
    if status == "draft":
        return "shadow"
    if status == "validated":
        return "pilot"
    if status in {"paused", "retiring", "archived"}:
        return "quarantined"
    if status == "active":
        # 内置五套是长期验证过的老账户；用户自建/AI 生成的策略一律从试点起步。
        return "standard" if str(getattr(spec, "origin", "") or "") == "builtin" else "pilot"
    return "quarantined"


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


# 键包含 strategy status 与生命周期阶段：策略暂停/下线后必须让旧缓存失效，
# 否则 pilot/standard 的旧额度会继续被复用（PR-26 评审 P1）。
_CACHE: dict[tuple[str, int, str, str, str, str], StrategyRuntimeContext] = {}


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


def _build_context(
    conn: sqlite3.Connection,
    spec,
    version,
    *,
    settings_rev: str | None = None,
) -> StrategyRuntimeContext:
    """Build one context from an explicitly supplied immutable version.

    Version-derived facts always come from ``version``.  Lifecycle permission
    is intentionally read from the current ``spec`` status, matching the
    existing live/runtime contract.
    """
    revision = settings_rev if settings_rev is not None else settings_revision(conn)
    status = str(getattr(spec, "status", "") or "").strip().lower()
    # PR-26 评审 P1：生命周期阶段参与缓存键，状态迁移（active→paused 等）立即生效。
    stage = lifecycle_stage_for(spec)
    key = (spec.id, version.version, version.checksum, revision, status, stage)
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
    # PR-26：生命周期阶段来自注册表的真实状态，不再是 active/shadow 二选一。
    allocation = PA.StrategyRuntime(
        strategy_id=spec.id,
        base_priority=max(float(soft.get("max_exposure", 0.65)), 0.01),
        max_positions=int(soft.get("max_positions", 3)),
        own_exposure_cap_pct=float(soft.get("max_exposure", 0.65)),
        lifecycle_stage=stage,
    )
    context = StrategyRuntimeContext(
        strategy_id=spec.id, version=version.version, checksum=version.checksum,
        definition=definition, compiled_dsl=compiled, risk_fingerprint=fingerprint,
        risk_profile=risk, execution_profile=execution,
        lifecycle_stage=allocation.lifecycle_stage,
        capital_scale=PA.stage_capital_scale(allocation)[0],
        allocation_runtime=allocation,
        evolution_control=EvolutionControlProfile(
            enabled=status == "active", lifecycle_stage=allocation.lifecycle_stage,
            interval_hours=24,
        ),
        parameter_schema=StrategyParameterSchema.from_dsl(compiled),
        settings_revision=revision,
    )
    _CACHE[key] = context
    return context


def get_context(conn: sqlite3.Connection, strategy_id: str, *, settings_rev: str | None = None) -> StrategyRuntimeContext:
    spec = SR.get(strategy_id, conn=conn)
    if spec is None:
        raise ValueError("unknown strategy id")
    version = SR.get_version(strategy_id, conn=conn)
    if version is None:
        raise ValueError("strategy has no immutable version")
    return _build_context(conn, spec, version, settings_rev=settings_rev)


def get_context_for_cycle(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    cycle_id: int,
    settings_rev: str | None = None,
) -> StrategyRuntimeContext:
    """Build a context from the exact immutable version pinned to one cycle.

    Explicit-cycle callers must not fall back to the current head, legacy
    bindings, or ``paper_accounts.cycle_id``.  A missing pin is an explicit
    ValueError; lifecycle permission still comes from the current spec.
    """
    if cycle_id is None:
        raise ValueError("get_context_for_cycle requires explicit cycle_id")
    spec = SR.get(strategy_id, conn=conn)
    if spec is None:
        raise ValueError("unknown strategy id")
    try:
        cycle = int(cycle_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"get_context_for_cycle cycle_id is invalid: {cycle_id!r}") from exc
    version = SR.cycle_version_for_account(conn, strategy_id, cycle_id=cycle)
    if version is None:
        raise ValueError(
            f"strategy version not pinned for cycle {cycle}: {strategy_id}"
        )
    return _build_context(conn, spec, version, settings_rev=settings_rev)


def _number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return number


def allocation_runtimes(
    account_ids,
    weights=None,
    diversification=None,
    *,
    conn: sqlite3.Connection | None = None,
    profiles=None,
    cycle_id=None,
    slot_caps=None,
    priority_floors=None,
    own_exposure_caps=None,
    strategy_max_positions: int = 6,
):
    """Build allocation runtimes with explicit cycle provenance when requested.

    ``cycle_id=None`` keeps the live/current behavior.  With an explicit cycle,
    version-derived fields come from the already bounded ``profiles`` (which
    include the cycle-pinned compiled profile), while lifecycle permission is
    read from the current strategy status only.  The current strategy head is
    never consulted for historical version-derived caps.
    """
    diversification = diversification or {}
    slot_caps = slot_caps or {}
    priority_floors = priority_floors or {}
    own_exposure_caps = own_exposure_caps or {}
    pinned = cycle_id is not None
    runtimes = []
    for account_id in account_ids:
        weight = _number((weights or {}).get(account_id), 1.0)
        context_runtime = None
        if conn is not None and not pinned:
            try:
                context_runtime = get_context(conn, account_id).allocation_runtime
            except (ValueError, sqlite3.Error):
                context_runtime = None
        if pinned:
            profile = (profiles or {}).get(account_id) or {}
            compiled_audit = profile.get("compiled_risk_profile") or {}
            max_positions = _number(
                compiled_audit.get("max_positions"),
                _number(profile.get("max_positions"), strategy_max_positions),
            )
            own_exposure_cap = _number(profile.get("max_exposure"))
            try:
                spec = SR.get(account_id, conn=conn) if conn is not None else None
            except sqlite3.Error:
                spec = None
            lifecycle_stage = lifecycle_stage_for(spec) if spec is not None else "quarantined"
            capital_scale = PA.stage_capital_scale(PA.StrategyRuntime(
                strategy_id=account_id, lifecycle_stage=lifecycle_stage))[0]
        else:
            max_positions = context_runtime.max_positions if context_runtime else strategy_max_positions
            own_exposure_cap = context_runtime.own_exposure_cap_pct if context_runtime else None
            lifecycle_stage = context_runtime.lifecycle_stage if context_runtime else "standard"
            capital_scale = context_runtime.capital_scale if context_runtime else None
        runtimes.append(PA.StrategyRuntime(
            strategy_id=account_id,
            base_priority=max(weight, 0.01),
            max_positions=slot_caps.get(account_id, max_positions),
            priority_floor_pct=priority_floors.get(account_id),
            own_exposure_cap_pct=own_exposure_caps.get(account_id, own_exposure_cap),
            diversification=_number(diversification.get(account_id), 1.0),
            lifecycle_stage=lifecycle_stage,
            capital_scale=capital_scale,
        ))
    return runtimes


def active_contexts(conn: sqlite3.Connection, *, settings_rev: str | None = None) -> tuple[StrategyRuntimeContext, ...]:
    revision = settings_rev if settings_rev is not None else settings_revision(conn)
    return tuple(get_context(conn, strategy_id, settings_rev=revision) for strategy_id in SR.active_ids(conn=conn))


def apply_parameter_adjustments(
    conn: sqlite3.Connection,
    strategy_id: str,
    adjustments: dict[str, Any],
    *,
    evidence_count: int | None,
    actor: str = "self_evolution",
    change_note: str = "self evolution strategy parameter adjustment",
    challenger_win: bool = False,
) -> dict[str, Any]:
    """Version one strategy after a schema-validated parameter-only change.

    ``StrategyParameterSchema`` owns all validation.  This function intentionally
    has no replacement-AST argument, so the only mutable bytes are declared
    parameter ``value`` fields inside the already pinned strategy definition.

    PR-33：任何带风险方向的参数变化都必须先过非对称风险门——收紧快速放行，
    放大需要证据 + 观察期 + 单轮上限 + Challenger 胜出。AI/自进化路径默认
    ``challenger_win=False``，因此**只能收紧**。
    """
    import asymmetric_risk as AR

    context = get_context(conn, strategy_id)
    if not context.evolution_control.enabled:
        raise ValueError("strategy evolution is disabled for this lifecycle state")
    if context.compiled_dsl is None:
        raise ValueError("strategy has no executable DSL parameter schema")
    by_id = {item.parameter_id: item for item in context.parameter_schema.parameters}
    gate = AR.evaluate_risk_adjustments(
        conn, strategy_id,
        [
            {
                "key": parameter_id,
                "old": by_id[parameter_id].value if parameter_id in by_id else None,
                "new": candidate,
                "direction": by_id[parameter_id].risk_direction if parameter_id in by_id else None,
            }
            for parameter_id, candidate in dict(adjustments or {}).items()
            if by_id.get(parameter_id) is not None
        ],
        evidence_count=evidence_count,
        challenger_win=challenger_win,
        actor=actor,
    )
    if not gate["allowed"]:
        raise ValueError("；".join(gate["violations"]))
    applied = context.parameter_schema.apply(
        context.compiled_dsl, adjustments, evidence_count=evidence_count,
    )
    if not applied.changed:
        return {
            "adjusted": False, "strategy_id": strategy_id,
            "version": context.version, "changed": {},
        }
    version = SR.save_definition(
        conn, strategy_id, {"dsl_ast": applied.dsl_ast},
        expected_version=context.version, actor=actor, change_note=change_note,
        risk_evidence=evidence_count, challenger_win=challenger_win,
    )
    clear_cache()
    # 放大真正落到生产参数后，把观察提案标记为 promoted（生命周期闭环）。
    for expansion in gate["expansions"]:
        AR.promote_proposal(conn, strategy_id, expansion["key"], expansion["new"], actor=actor)
    return {
        "adjusted": True, "strategy_id": strategy_id, "version": version.version,
        "checksum": version.checksum, "changed": applied.changed,
        "structure_checksum": applied.structure_checksum,
        "risk_gate": gate,
    }
