# -*- coding: utf-8 -*-
"""PR-51：Strategy 应用服务——HTTP 层与 Registry/Runtime 之间的唯一边界。

职责（也就是 ``api_strategies.py`` 交出来的那些）：

- 打开数据库连接、界定事务边界（``paper_trading._db`` 只在本模块出现）；
- 编排 Registry / Runtime / 预览引擎的调用顺序与前置条件；
- 把 Registry 抛出的 ``ValueError`` **单点**翻译成 domain exception；
- 组装对外视图（item / detail / versions / events / runtime 画像）。

不负责：HTTP 状态码、查询参数校验、请求体形状（``strategy_api_models`` 与
``api_strategies`` 的职责）。

边界原则：业务规则仍然只住在既有 domain 模块里。本模块**不**复制 id 模式、
DSL 校验、生命周期合法边、非对称风险闸门，也**不**重新计算任何风险/资金画像；
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
    payload = {
        **item_payload(spec),
        "definition": dict(version.definition) if version else None,
        "dsl_ast": spec.dsl_ast,
        "version": version.version if version else None,
        "checksum": version.checksum if version else None,
        "created_by": version.created_by if version else None,
        "change_note": version.change_note if version else None,
        "supports_new_cycle": bool(spec.supports_new_cycle),
        "lifecycle": {
            "status": spec.status,
            "supports_new_cycle": bool(spec.supports_new_cycle),
        },
        "events": [dict(row) for row in SR.lifecycle_events(conn, spec.id)[-20:]],
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
        return [dict(row) for row in SR.lifecycle_events(conn, strategy_id)]
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


def transition(strategy_id: str, request: Models.StrategyTransitionRequest) -> dict:
    to_status = str(request.to_status or "").strip()
    if not to_status:
        raise InvalidStrategyDefinition("to_status is required")

    def _work(conn):
        # validated 与 active 同样是"可进新周期"的入口门槛：进入 validated
        # 前先跑一遍生产编译闸门，不就绪就拒绝。
        if to_status == "validated":
            readiness = SR.runtime_readiness(conn, strategy_id)
            if not readiness.get("runtime_ready"):
                raise StrategyRuntimeNotReady(
                    "strategy runtime is not ready: " + "; ".join(readiness.get("errors") or [])
                )
        spec = SR.transition(
            conn, strategy_id, to_status, reason=str(request.reason or ""),
            actor=str(request.actor or "web-ui"),
            expected_status=request.expected_status,
        )
        return detail_payload(conn, spec)
    return _with_connection(_work)


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
