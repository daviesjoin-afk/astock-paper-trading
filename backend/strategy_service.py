# -*- coding: utf-8 -*-
"""PR-51：Strategy 应用服务——HTTP 层与 Registry/Runtime 之间的唯一边界。

职责（也就是 ``api_strategies.py`` 交出来的那些）：

- 打开数据库连接、界定事务边界（``paper_trading._db`` 只在本模块出现）；
- 编排 Registry / Runtime / 预览引擎的调用顺序与前置条件；
- 把 Registry 抛出的 ``ValueError`` **单点**翻译成 domain exception；
- 组装对外视图（item / detail / versions / events / runtime 画像）。

不负责：HTTP 状态码、查询参数校验、请求体形状（``strategy_api_models`` 与
``api_strategies`` 的职责）。

边界原则：业务规则仍然只住在 domain owner 里。本模块**不**复制 id 模式、
DSL 校验、生命周期合法边、晋级证据规则、非对称风险闸门，也**不**重新计算任何风险/资金画像；
它只决定"先调谁、失败算哪一类错误"。

风险放大唯一入口（PR-33）：``update_strategy`` 不把 ``risk_evidence`` /
``challenger_win`` 暴露给 HTTP 调用方，固定按 fail-closed 传入（Web 只能收紧
风险）。任何"调用方自带证据"的字段在 ``strategy_api_models`` 就被丢弃，因此
不存在从 API 放大风险的路径。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import paper_trading as P
import strategy_api_models as Models
import strategy_dsl_schema as DSL
import strategy_registry as SR
import strategy_lifecycle as SL
import strategy_promotion as SPR
import strategy_runtime as SRT


# ---------------------------------------------------------------------------
# domain exception：HTTP 层只认这些类型，不再匹配错误字符串
# ---------------------------------------------------------------------------

class StrategyError(Exception):
    """Strategy 用例失败的基类。"""


class StrategyNotFound(StrategyError):
    """策略 / 版本不存在。"""


class InvalidStrategyDefinition(StrategyError):
    """定义本身不合法（缺字段、id 不合法、改动不可版本化字段……）。"""


class InvalidStrategyDsl(InvalidStrategyDefinition):
    """DSL/AST 无法解析 —— 与"定义不合法"分开，因为它映射到 422。"""


class StrategyConflict(StrategyError):
    """与现有状态冲突（重复 id、预留 id……）。"""


class StrategyVersionConflict(StrategyConflict):
    """乐观并发：期望版本/状态与库中不一致。"""


class InvalidLifecycleTransition(StrategyConflict):
    """生命周期合法边之外的迁移。"""


class StrategyRuntimeNotReady(StrategyConflict):
    """生产编译闸门未通过（进 validated/active 前的前置条件）。"""


class StrategyHistoricalReferenceError(StrategyConflict):
    """已有历史引用：只能归档，不能删除。"""


class StrategyRiskExpansionRejected(StrategyError):
    """非对称风险门拒绝本次风险放大。"""


# Registry 至今只用 ValueError 表达业务拒绝。分类只在**这一处**发生：
# 顺序敏感——先匹配最具体的类别，最后才回落到"定义不合法"（历史上是 400）。
_RISK_GATE_TOKENS = ("风险放大", "证据不足", "观察期未满", "Challenger", "单轮放大")
_NOT_FOUND_TOKENS = (
    "unknown strategy id",
    "unknown source strategy version",
    "strategy has no immutable version",
    "strategy version binding not found",
    "legacy strategy binding not found",
    "legacy strategy version not found",
)
_VERSION_CONFLICT_TOKENS = ("version changed",)
_HISTORICAL_TOKENS = ("historical references", "only unused user drafts")
_RUNTIME_TOKENS = ("runtime is not ready",)
_LIFECYCLE_TOKENS = (
    "status changed",
    "invalid lifecycle transition",
    "strategy_lifecycle_conflict",
    "strategy_version_changed",
    "strategy_checksum_mismatch",
    "promotion_",
    "r29_",
    "r30_",
    "ai_cannot_apply_transition",
    "quarantine_release_evidence_missing",
    "lifecycle_",
    "strategy cannot be archived",
    "invalid strategy status",
    "evolution is disabled",
)
_CONFLICT_TOKENS = ("already exists", "is reserved")


def translate_registry_error(exc: Exception) -> StrategyError:
    """把 Registry/Runtime 的 ValueError 翻译成 domain exception（单点转换）。"""
    message = str(exc) or type(exc).__name__
    lowered = message.lower()
    if any(token in message for token in _RISK_GATE_TOKENS):
        return StrategyRiskExpansionRejected(message)
    if any(token in lowered for token in _NOT_FOUND_TOKENS):
        return StrategyNotFound(message)
    if any(token in lowered for token in _VERSION_CONFLICT_TOKENS):
        return StrategyVersionConflict(message)
    if any(token in lowered for token in _HISTORICAL_TOKENS):
        return StrategyHistoricalReferenceError(message)
    if any(token in lowered for token in _RUNTIME_TOKENS):
        return StrategyRuntimeNotReady(message)
    if any(token in lowered for token in _LIFECYCLE_TOKENS):
        return InvalidLifecycleTransition(message)
    if any(token in lowered for token in _CONFLICT_TOKENS):
        return StrategyConflict(message)
    return InvalidStrategyDefinition(message)


# ---------------------------------------------------------------------------
# 连接 / 事务
# ---------------------------------------------------------------------------

def _with_connection(work, *, immediate: bool = False):
    """在 ``paper_trading`` 的连接与事务里执行 ``work(conn)``。

    唯一的连接出口：HTTP 层不再出现 ``P._db()``。Registry 自己的
    ``ValueError`` 在这里统一翻译，调用方（含测试）只看到 domain exception。
    """
    P.init_db()
    try:
        with P._db(immediate=immediate) as conn:
            return work(conn)
    except StrategyError:
        raise
    except ValueError as exc:
        raise translate_registry_error(exc) from exc


def _assert_parsable_dsl(dsl_ast: Any) -> None:
    """DSL 形状门（与旧 API 层等价）：解析失败是请求问题 → 422。"""
    if dsl_ast is None:
        return
    try:
        DSL.normalize(dsl_ast)
    except Exception as exc:  # noqa: BLE001 - 校验器自身抛错也算 DSL 非法
        raise InvalidStrategyDsl(f"invalid DSL: {exc}") from exc


# ---------------------------------------------------------------------------
# 视图组装（只搬运既有字段，不重算任何业务值）
# ---------------------------------------------------------------------------

def item_payload(spec: SR.StrategySpec) -> dict:
    return {
        "id": spec.id,
        "name": spec.name,
        "origin": spec.origin,
        "status": spec.status,
        "supports_new_cycle": bool(spec.supports_new_cycle),
        "formal_cycle_allowed": bool(spec.supports_new_cycle),
        "current_version": spec.current_version,
        "current_checksum": spec.current_checksum,
        "description": spec.description,
        "metadata": dict(spec.metadata or {}),
        "has_dsl": spec.dsl_ast is not None,
    }


def runtime_payload(conn, strategy_id: str, spec: SR.StrategySpec) -> dict:
    """编译期只读画像。失败降级为 runtime_ready=false，不作为服务错误抛出。"""
    try:
        context = SRT.get_context(conn, strategy_id)
    except Exception as exc:  # noqa: BLE001 - 编译失败是数据问题，不是服务崩溃
        base = {"runtime_ready": False, "runtime_error": str(exc) or type(exc).__name__}
        try:
            readiness = SR.runtime_readiness(conn, strategy_id)
        except Exception:  # pragma: no cover - 兜底：连只读检查都失败
            readiness = None
        if readiness:
            base["checks"] = readiness.get("checks")
            base["errors"] = readiness.get("errors")
        return base
    allocation = context.allocation_runtime
    return {
        "runtime_ready": True,
        "runtime_error": None,
        "risk_fingerprint": context.risk_fingerprint.to_dict(),
        "risk_profile": context.risk_profile.to_dict(),
        "execution_profile": context.execution_profile,
        "lifecycle_stage": context.lifecycle_stage,
        "capital_scale": context.capital_scale,
        "allocation_runtime": {
            "strategy_id": allocation.strategy_id,
            "base_priority": allocation.base_priority,
            "max_positions": allocation.max_positions,
            "own_exposure_cap_pct": allocation.own_exposure_cap_pct,
            "lifecycle_stage": allocation.lifecycle_stage,
        },
    }


def detail_payload(conn, spec: SR.StrategySpec, *, with_runtime: bool = True) -> dict:
    version = SR.get_version(spec.id, conn=conn)
    lifecycle_state = (SL.get_state(conn, spec.id, version.version, checksum=version.checksum)
                       if version else None)
    lifecycle_history = SL.history(conn, spec.id, version.version if version else None)
    payload = {
        **item_payload(spec),
        "definition": dict(version.definition) if version else None,
        "dsl_ast": spec.dsl_ast,
        "version": version.version if version else None,
        "checksum": version.checksum if version else None,
        "created_by": version.created_by if version else None,
        "change_note": version.change_note if version else None,
        "supports_new_cycle": bool(spec.supports_new_cycle),
        "formal_cycle_allowed": bool(spec.supports_new_cycle),
        "lifecycle": {
            "state": lifecycle_state.get("state") if lifecycle_state else None,
            "status": lifecycle_state.get("state") if lifecycle_state else None,
            "supports_new_cycle": bool(spec.supports_new_cycle),
            "formal_cycle_allowed": bool(spec.supports_new_cycle),
        },
        "events": [dict(row) for row in lifecycle_history[-20:]],
    }
    if with_runtime:
        payload["runtime"] = runtime_payload(conn, spec.id, spec)
        payload["runtime_ready"] = bool(payload["runtime"].get("runtime_ready"))
        payload["runtime_error"] = payload["runtime"].get("runtime_error")
    return payload


# ---------------------------------------------------------------------------
# 用例：只读
# ---------------------------------------------------------------------------

def list_strategies(*, origin: str | None = None, status: str | None = None,
                    include_archived: bool = False) -> list[SR.StrategySpec]:
    return _with_connection(
        lambda conn: list(SR.list_definitions(
            conn=conn,
            origins=(origin,) if origin else None,
            statuses=(status,) if status else None,
            include_archived=include_archived,
        ))
    )


def get_strategy(strategy_id: str) -> SR.StrategySpec:
    def _work(conn):
        spec = SR.get(strategy_id, conn=conn)
        if spec is None:
            raise StrategyNotFound("unknown strategy id")
        return spec
    return _with_connection(_work)


def detail(strategy_id: str, *, with_runtime: bool = True) -> dict:
    def _work(conn):
        spec = SR.get(strategy_id, conn=conn)
        if spec is None:
            raise StrategyNotFound("unknown strategy id")
        return detail_payload(conn, spec, with_runtime=with_runtime)
    return _with_connection(_work)


def list_versions(strategy_id: str) -> list[dict]:
    def _work(conn):
        if SR.get(strategy_id, conn=conn) is None:
            raise StrategyNotFound("unknown strategy id")
        return [
            {
                "version": version.version,
                "checksum": version.checksum,
                "created_at": version.created_at,
                "created_by": version.created_by,
                "change_note": version.change_note,
                "definition": dict(version.definition or {}),
                "cloned_from_strategy_id": version.cloned_from_strategy_id,
                "cloned_from_version": version.cloned_from_version,
                "cloned_from_checksum": version.cloned_from_checksum,
            }
            for version in SR.list_versions(strategy_id, conn=conn)
        ]
    return _with_connection(_work)


def list_events(strategy_id: str) -> list[dict]:
    def _work(conn):
        if SR.get(strategy_id, conn=conn) is None:
            raise StrategyNotFound("unknown strategy id")
        return [dict(row) for row in SL.history(conn, strategy_id)]
    return _with_connection(_work)


# ---------------------------------------------------------------------------
# 用例：写入
# ---------------------------------------------------------------------------

def create_strategy(request: Models.StrategyCreateRequest) -> dict:
    strategy_id = str(request.id or "").strip()
    name = str(request.name or "").strip()
    if not strategy_id:
        raise InvalidStrategyDefinition("strategy id is required")
    if not name:
        raise InvalidStrategyDefinition("strategy name is required")
    _assert_parsable_dsl(request.dsl_ast)

    def _work(conn):
        spec = SR.create_user_definition(
            conn, strategy_id, name, description=str(request.description or "").strip(),
            metadata=request.metadata or {}, dsl_ast=request.dsl_ast,
            actor=str(request.actor or "web-ui"),
        )
        return detail_payload(conn, spec)
    return _with_connection(_work)


def update_strategy(strategy_id: str, request: Models.StrategyUpdateRequest) -> dict:
    """生成不可变新版本；风险放大只能收紧（见模块 docstring）。"""
    changes = request.versioned_fields()
    if not changes:
        raise InvalidStrategyDefinition("changes must be a non-empty object")
    _assert_parsable_dsl(changes.get("dsl_ast"))
    # 非对称风险门（唯一风险放大门）不暴露给 HTTP 调用方：任何请求都按
    # fail-closed 处理（risk_evidence/challenger_win 一律为空），风险放大
    # 只能走正规提案流程（自进化 / Champion 晋升），Web 端只允许收紧。
    risk_evidence = None
    challenger_win = False

    def _work(conn):
        version = SR.save_definition(
            conn, strategy_id, changes, expected_version=request.expected_version,
            actor=str(request.actor or "web-ui"), change_note=str(request.change_note or ""),
            risk_evidence=risk_evidence, challenger_win=challenger_win,
        )
        spec = SR.get(strategy_id, conn=conn)
        return {
            "strategy": detail_payload(conn, spec) if spec else {},
            "version": version.to_dict() if hasattr(version, "to_dict") else dict(version),
            "created_version": getattr(version, "version", None),
            "checksum": getattr(version, "checksum", None),
        }
    return _with_connection(_work)


def _open_evidence_connection():
    """Open the separate append-only adaptive evidence store for exact reads."""
    return P._evolution_conn()


def lifecycle_read_model(strategy_id: str) -> dict:
    def _work(conn):
        spec = SR.get(strategy_id, conn=conn)
        if spec is None:
            raise StrategyNotFound("unknown strategy id")
        version = SR.get_version(spec.id, conn=conn)
        if version is None:
            raise StrategyNotFound("strategy has no immutable version")
        state = SL.get_state(conn, spec.id, version.version, checksum=version.checksum)
        if state is None:
            raise StrategyConflict("lifecycle_state_not_found")
        evidence_conn = None
        decisions = {}
        try:
            evidence_conn = _open_evidence_connection()
        except Exception:
            evidence_conn = None
        try:
            legal = sorted(SL.TRANSITION_TABLE[state["state"]])
            eligible, blocked = [], {}
            for target in legal:
                if target in SL.SAFETY_TRANSITION_TARGETS:
                    eligible.append(target)
                    continue
                if state["state"] == "paused" and target in SL.RESUME_TRANSITION_TARGETS:
                    try:
                        resume_target, _ = SL._resume_policy(
                            conn, spec.id, version.version, version.checksum)
                    except SL.LifecycleError:
                        resume_target = None
                    if state["state"] == "paused" and target == resume_target:
                        eligible.append(target)
                    else:
                        blocked[target] = {"eligible": False,
                            "blocking_reasons": ["resume_source_or_policy_unavailable"],
                            "required_evidence": ["human_resume_reason"]}
                    continue
                decision = SPR.evaluate(conn, strategy_id=spec.id,
                    strategy_version=version.version, strategy_checksum=version.checksum,
                    from_state=state["state"], target_state=target,
                    evidence_bundle=None, evidence_conn=evidence_conn)
                decisions[target] = decision.projection()
                if decision.eligible:
                    eligible.append(target)
                else:
                    blocked[target] = decision.projection()
            proposals = SPR.list_proposals(conn, spec.id, version=version.version, limit=50)
            return {"strategy_id": spec.id, "state": state["state"],
                "version": version.version, "checksum": version.checksum,
                "state_history": SL.history(conn, spec.id),
                "legal_transitions": legal, "eligible_transitions": eligible,
                "safety_transitions": sorted((set(legal) & SL.SAFETY_TRANSITION_TARGETS)
                    | (set(eligible) & SL.RESUME_TRANSITION_TARGETS
                       if state["state"] == "paused" else set())),
                "promotion_transitions": sorted(set(legal) - SL.SAFETY_TRANSITION_TARGETS
                    - (SL.RESUME_TRANSITION_TARGETS if state["state"] == "paused" else set())),
                "blocked_transitions": blocked,
                "blocking_reasons": sorted({reason for row in blocked.values()
                    for reason in row.get("blocking_reasons", [])}),
                "required_evidence": {target: item.get("required_evidence", [])
                    for target, item in blocked.items()},
                "evidence": {target: item for target, item in decisions.items()},
                "proposals": proposals,
                "formal_cycle_allowed": SL.allows_formal_cycle(state["state"])}
        finally:
            if evidence_conn is not None:
                evidence_conn.close()
    return _with_connection(_work)


# ---------------------------------------------------------------------------
# Active vs Challenger workspace（read model：只组装 owner 事实）
# ---------------------------------------------------------------------------

def _exact_comparison_report(conn, report_id):
    """The explicitly named report, or ``None`` with a stable owner reason.

    There is deliberately no "newest report" resolution: an unnamed or unknown
    report is reported as absent. A row the owner's reader rejects as corrupt is
    reported as corrupt rather than being silently dropped.
    """
    if report_id is None:
        return None, "shadow_comparison_report_required"
    if not isinstance(report_id, str) or len(report_id) != 64:
        raise InvalidStrategyDefinition("exact_comparison_report_id_required")
    import shadow_comparison_repository as SCR
    try:
        report = SCR.get_report(conn, report_id)
    except SCR.ShadowComparisonRepositoryError:
        return None, "shadow_comparison_corrupt"
    except Exception:
        return None, "shadow_comparison_report_not_found"
    if report is None:
        return None, "shadow_comparison_report_not_found"
    return report, None


def _comparison_section(report, report_id, reason) -> dict:
    """The comparison owner's own facts, verbatim. Nothing is recomputed here."""
    base = {"authority": "shadow_comparison", "report_id": report_id}
    if report is None:
        return {**base, "available": False, "unavailable_reason": reason,
                "availability": "UNAVAILABLE", "coverage": None, "blocking_reasons": [],
                "report_fingerprint": None, "comparison_scope_identity": None,
                "shadow_run_id": None, "shadow_run_fingerprint": None,
                "active_strategy_stamp": None, "challenger_strategy_stamp": None,
                "environment_identity": None, "provenance": None, "observations": [],
                "signal_delta": None, "decision_delta": None, "execution": None,
                "risk_rejection": None, "turnover": None, "performance": None}
    return {
        **base, "available": True, "unavailable_reason": None,
        "availability": str(report.availability),
        "coverage": dict(report.coverage),
        "blocking_reasons": [str(item) for item in report.blocking_reasons],
        "report_fingerprint": str(report.report_fingerprint),
        "comparison_scope_identity": str(
            (report.comparison_spec or {}).get("comparison_scope_identity") or ""),
        "comparison_spec": dict(report.comparison_spec or {}),
        "shadow_run_id": str(report.shadow_run_id),
        "shadow_run_fingerprint": str(report.shadow_run_fingerprint),
        "active_strategy_stamp": dict(report.active_strategy_stamp or {}),
        "challenger_strategy_stamp": dict(report.challenger_strategy_stamp or {}),
        "environment_identity": dict(report.environment_identity or {}),
        "provenance": dict(report.provenance or {}),
        "observations": [dict(item) for item in report.observations],
        "signal_delta": dict(report.signal_delta or {}),
        "decision_delta": dict(report.decision_delta or {}),
        "execution": dict(report.execution or {}),
        "risk_rejection": dict(report.risk_rejection or {}),
        "turnover": dict(report.turnover or {}),
        "performance": dict(report.performance or {}),
    }


def _parameter_head_section(conn, strategy_id: str) -> dict:
    """Current parameter-head activation state — a separate authority.

    This is the Champion activation ledger. Its target fact is the formal
    parameter/version head, never the strategy lifecycle state, so it is reported
    in its own section and is never combined with the lifecycle promotion
    readiness. Read-only.
    """
    import strategy_champion as SCM
    section = {"authority": "strategy_champion",
               "target_fact": "formal parameter/version head",
               "engine": SCM.STRATEGY_CHAMPION_VERSION,
               "mutation_executor": "self_evolution.activate_params_candidate",
               "versions": []}
    try:
        rows = conn.execute(
            "SELECT id,role,status,source,base_checksum,proposed_at,evaluated_at"
            " FROM strategy_champion_versions WHERE strategy_id=?"
            " ORDER BY id DESC LIMIT 5", (str(strategy_id),)).fetchall()
    except Exception:
        return {**section, "available": False,
                "unavailable_reason": "parameter_head_ledger_unavailable"}
    return {**section, "available": True, "unavailable_reason": None,
            "versions": [dict(row) for row in rows]}


def _leg_lifecycle_state(conn, strategy_id: str, version: int, checksum: str):
    """Exact lifecycle lookup for one stamped version. Unknown stays unknown."""
    state = SL.get_state(conn, strategy_id, version, checksum=checksum)
    return (state or {}).get("state")


def _leg_from_report_stamp(conn, stamp, *, source: str) -> dict:
    """One comparison leg, taken verbatim from the report's own strategy stamp."""
    values = dict(stamp or {})
    leg_id = str(values.get("strategy_id") or "")
    leg_version = int(values.get("version") or 0)
    leg_checksum = str(values.get("checksum") or "")
    if not leg_id or leg_version < 1 or not leg_checksum:
        raise InvalidStrategyDefinition("shadow_comparison_stamp_invalid")
    return {
        "available": True,
        "unavailable_reason": None,
        "identity_source": source,
        "comparison_bound": True,
        "strategy_id": leg_id,
        "version": leg_version,
        "checksum": leg_checksum,
        # The Active comparator's state is looked up for *its own* exact version;
        # an unknown strategy/version is reported as unknown, never filled from
        # the Challenger's state.
        "lifecycle_state": _leg_lifecycle_state(conn, leg_id, leg_version, leg_checksum),
    }


def challenger_read_model(strategy_id: str, *, comparison_report_id: str | None = None,
                          version: int | None = None) -> dict:
    """Active vs Challenger workspace facts, assembled from exact owner output.

    Read-only composition, with one hard rule: while an exact
    ``ShadowComparisonReport`` is named, **both comparison legs come from that
    report's own strategy stamps** — never from the registry head. A report whose
    Challenger stamp is not this endpoint's strategy, or a ``version`` that
    contradicts the report, is an input identity conflict and fails closed.
    Without a report there is no Active comparator fact at all.
    """
    def _work(conn):
        spec = SR.get(strategy_id, conn=conn)
        if spec is None:
            raise StrategyNotFound("unknown strategy id")
        # The actual current head is its own owner fact: a caller-requested exact
        # version must never be reported as the registry head.
        registry_head = SR.get_version(strategy_id, conn=conn)
        report, reason = _exact_comparison_report(conn, comparison_report_id)
        if report is not None:
            challenger = _leg_from_report_stamp(
                conn, report.challenger_strategy_stamp,
                source="shadow_comparison.challenger_strategy_stamp")
            active = _leg_from_report_stamp(
                conn, report.active_strategy_stamp,
                source="shadow_comparison.active_strategy_stamp")
            # The named evidence must belong to this endpoint's strategy, and the
            # caller's version must agree with it; otherwise the page would mix
            # one strategy with another strategy's comparison.
            if challenger["strategy_id"] != str(strategy_id):
                raise InvalidStrategyDefinition("shadow_comparison_identity_mismatch")
            if version is not None and int(version) != challenger["version"]:
                raise InvalidStrategyDefinition("shadow_comparison_identity_mismatch")
        else:
            # No report ⇒ no Active comparator fact. The Challenger is shown as a
            # registry candidate so the page can explain what evidence is needed.
            candidate = (SR.get_version(strategy_id, version, conn=conn)
                         if version is not None else registry_head)
            if candidate is None:
                raise StrategyNotFound("strategy version not found")
            challenger = {
                "available": True,
                "unavailable_reason": None,
                "identity_source": "registry_candidate",
                "comparison_bound": False,
                "strategy_id": str(strategy_id),
                "version": int(candidate.version),
                "checksum": candidate.checksum,
                "lifecycle_state": _leg_lifecycle_state(
                    conn, str(strategy_id), int(candidate.version), candidate.checksum),
            }
            active = {"available": False,
                      "unavailable_reason": reason,
                      "identity_source": None, "comparison_bound": False,
                      "strategy_id": None, "version": None, "checksum": None,
                      "lifecycle_state": None}
        state = challenger["lifecycle_state"]
        # The promotion candidate is the *displayed* Challenger exact identity. If
        # that version is no longer the registry head, the policy itself returns
        # `strategy_version_changed` — the workspace still shows the old exact
        # version rather than silently swapping in the current head.
        decision = SPR.evaluate(
            conn, strategy_id=challenger["strategy_id"],
            strategy_version=challenger["version"],
            strategy_checksum=challenger["checksum"],
            from_state=state or "", target_state="paper",
            evidence_bundle=({"shadow_comparison_report_id": comparison_report_id}
                             if comparison_report_id else None))
        return {
            "strategy_id": strategy_id,
            "active": active,
            "challenger": challenger,
            "registry_head": {
                "version": (int(registry_head.version) if registry_head is not None else None),
                "checksum": (registry_head.checksum if registry_head is not None else None),
            },
            "comparison": _comparison_section(report, comparison_report_id, reason),
            "lifecycle_promotion": {
                "authority": "strategy_promotion",
                "target_fact": "strategy lifecycle state",
                "policy_version": SPR.POLICY_VERSION,
                "from_state": state,
                "target_state": "paper",
                "candidate": {"strategy_id": challenger["strategy_id"],
                              "strategy_version": challenger["version"],
                              "strategy_checksum": challenger["checksum"]},
                "eligible": bool(decision.eligible),
                "blocking_reasons": list(decision.blocking_reasons),
                "required_evidence": list(decision.required_evidence),
                "satisfied_evidence": list(decision.satisfied_evidence),
                "evidence_fingerprints": dict(decision.evidence_fingerprints),
                "decision_fingerprint": decision.decision_fingerprint,
                "mutation_executor": "strategy_lifecycle.transition",
            },
            "parameter_head_activation": _parameter_head_section(conn, strategy_id),
            "lifecycle": {
                "state": state,
                "version": challenger["version"],
                "checksum": challenger["checksum"],
                "allowed_next_transitions": sorted(SL.TRANSITION_TABLE.get(state or "", ())),
                "history": [dict(row) for row in SL.history(
                    conn, challenger["strategy_id"], challenger["version"])][-20:],
            },
            "exact_evidence": {"comparison_report_id": comparison_report_id},
        }
    return _with_connection(_work)


def create_promotion_proposal(strategy_id: str, request: Models.PromotionProposalRequest) -> dict:
    def _work(conn):
        spec = SR.get(strategy_id, conn=conn)
        if spec is None:
            raise StrategyNotFound("unknown strategy id")
        version = SR.get_version(strategy_id, request.strategy_version, checksum=request.strategy_checksum, conn=conn)
        if version is None:
            raise StrategyNotFound("strategy version not found")
        try:
            evidence_conn = _open_evidence_connection()
        except Exception:
            evidence_conn = None
        try:
            return SPR.create_proposal(conn, strategy_id=strategy_id,
                strategy_version=request.strategy_version, strategy_checksum=request.strategy_checksum,
                from_state=request.expected_state, target_state=request.target_state,
                evidence_bundle=request.evidence_bundle, proposer_type=request.proposer_type,
                proposer_id=request.proposer_id, rationale=request.rationale,
                evidence_conn=evidence_conn)
        finally:
            if evidence_conn is not None:
                evidence_conn.close()
    return _with_connection(_work, immediate=True)


def list_promotion_proposals(strategy_id: str, *, version: int | None = None, limit: int = 50) -> list[dict]:
    def _work(conn):
        if SR.get(strategy_id, conn=conn) is None:
            raise StrategyNotFound("unknown strategy id")
        return SPR.list_proposals(conn, strategy_id, version=version, limit=limit)
    return _with_connection(_work)


def transition(strategy_id: str, request: Models.StrategyTransitionRequest) -> dict:
    def _work(conn):
        spec = SR.get(strategy_id, conn=conn)
        if spec is None:
            raise StrategyNotFound("unknown strategy id")
        version = SR.get_version(strategy_id, request.strategy_version,
                                 checksum=request.strategy_checksum, conn=conn)
        if version is None:
            raise StrategyNotFound("strategy version not found")
        is_resume = (request.expected_state == "paused"
                     and request.target_state in SL.RESUME_TRANSITION_TARGETS)
        if request.target_state in SL.SAFETY_TRANSITION_TARGETS or is_resume:
            transitioned = SL.transition(conn, strategy_id=strategy_id,
                strategy_version=request.strategy_version, strategy_checksum=request.strategy_checksum,
                expected_state=request.expected_state, target_state=request.target_state,
                actor_type=request.actor_type, actor_id=request.actor_id,
                reason_code=request.reason_code, reason_text=request.reason,
                transition_kind="resume" if is_resume else "safety",
                evidence={"source": "strategy_transition_api"})
            return {**detail_payload(conn, SR.get(strategy_id, conn=conn)),
                    "transitioned_version": {"version": request.strategy_version,
                        "checksum": request.strategy_checksum,
                        "state": transitioned["state"]}}
        if not request.proposal_fingerprint:
            raise InvalidStrategyDefinition("promotion_proposal_fingerprint_required")
        proposal = SPR.get_proposal(conn, request.proposal_fingerprint)
        if proposal is None:
            raise StrategyNotFound("promotion_proposal_not_found")
        if (proposal["strategy_id"] != strategy_id
                or proposal["strategy_version"] != request.strategy_version
                or proposal["strategy_checksum"] != request.strategy_checksum
                or proposal["from_state"] != request.expected_state
                or proposal["target_state"] != request.target_state):
            raise StrategyVersionConflict("promotion_proposal_identity_mismatch")
        evidence_conn = _open_evidence_connection()
        try:
            applied = SPR.apply_proposal(conn, evidence_conn, proposal,
                actor_type=request.actor_type, actor_id=request.actor_id)
        finally:
            evidence_conn.close()
        return {**detail_payload(conn, SR.get(strategy_id, conn=conn)), **applied}
    return _with_connection(_work, immediate=True)


def clone_strategy(strategy_id: str, request: Models.StrategyCloneRequest) -> dict:
    new_strategy_id = request.target_id()
    if not new_strategy_id:
        raise InvalidStrategyDefinition("new_strategy_id is required")
    name = request.name

    def _work(conn):
        source_version = request.source_version
        if source_version is None:
            current = SR.get_version(strategy_id, conn=conn)
            source_version = current.version if current else None
        spec = SR.clone_definition(
            conn, strategy_id, source_version, new_strategy_id,
            name=None if name is None else str(name), actor=str(request.actor or "web-ui"),
        )
        return detail_payload(conn, spec)
    return _with_connection(_work)


def delete_unused_draft(strategy_id: str) -> dict:
    def _work(conn):
        result = SR.hard_delete_unused_draft(conn, strategy_id)
        return {**result, "archived_instead_hint": None}
    return _with_connection(_work)


# ---------------------------------------------------------------------------
# 用例：无库路径
# ---------------------------------------------------------------------------

def validate_definition(dsl_ast: Any, metadata: Any = None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    normalized = None
    checksum = None
    if dsl_ast is None:
        errors.append("dsl_ast is required")
    else:
        try:
            normalized, _, checksum = DSL.canonicalize(dsl_ast)
        except Exception as exc:  # noqa: BLE001 - 任何 DSL 校验失败都算 invalid
            errors.append(str(exc) or type(exc).__name__)
    if metadata is not None and not isinstance(metadata, Mapping):
        errors.append("metadata must be an object")
    if not errors and not normalized:
        warnings.append("empty DSL AST accepted; runtime will fall back to conservative defaults")
    return {
        "valid": not errors,
        "normalized_ast": normalized,
        "checksum": checksum,
        "errors": errors,
        "warnings": warnings,
    }


def preview_strategy(request: Models.StrategyPreviewRequest) -> dict:
    """预览与 ``/api/paper/strategy-preview`` 同源。

    ``paper_trading.strategy_creation_preview`` 是既有的兼容入口（它自己负责
    取共享资金池现值），只能从本模块调用；HTTP 层不再接触私有 ``P._db``。
    """
    draft = request.draft()
    try:
        raw = P.strategy_creation_preview(draft)
    except Exception as exc:  # noqa: BLE001 - 预览失败不应让整页 5xx
        raise InvalidStrategyDefinition(f"preview failed: {exc}") from exc
    raw = dict(raw or {})
    # 只做结构搬运，不重新计算任何画像。
    lifecycle = raw.get("lifecycle") if isinstance(raw.get("lifecycle"), Mapping) else {}
    recommended = raw.get("recommended") if isinstance(raw.get("recommended"), Mapping) else {}
    risk_profile = raw.get("risk_profile") if isinstance(raw.get("risk_profile"), Mapping) else None
    execution = raw.get("execution_profile") or recommended.get("execution_profile") or {}
    dsl_error = raw.get("dsl_error")
    return {
        "valid": bool(raw.get("dsl_valid", True)) and not dsl_error,
        "engine": raw.get("engine"),
        "structure_checksum": raw.get("structure_checksum"),
        "risk_fingerprint": raw.get("risk_fingerprint"),
        "risk_profile": risk_profile or {
            "recommended_profile": recommended.get("risk_profile"),
            "recommended_profile_label": recommended.get("risk_profile_label"),
            "limits": recommended.get("limits"),
        },
        "execution_profile": execution,
        "allocation": {
            "lifecycle_stage": lifecycle.get("initial_stage"),
            "stage_label": lifecycle.get("stage_label"),
            "capital_scale": lifecycle.get("capital_scale"),
            "pool_capital": lifecycle.get("pool_capital"),
            "estimated_capital": lifecycle.get("estimated_capital"),
        },
        "high_risk_overrides": raw.get("high_risk_overrides") or [],
        "fail_closed": raw.get("fail_closed"),
        "errors": [dsl_error] if dsl_error else [],
        "warnings": [],
    }
