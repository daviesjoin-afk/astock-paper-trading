# -*- coding: utf-8 -*-
"""PR-45：Strategy Admin HTTP API（``/api/strategies``）。

Web 工作台只通过这套接口创建/编辑/验证/预览/迁移用户声明式策略：
所有写操作都落到 ``strategy_registry``（Registry 是唯一权威来源），
API 层不复制任何规则、不手写 SQL、不重新计算风险画像。

约束（与产品原则一致）：
- 用户只能提交声明式 DSL/AST；本模块不做 eval/exec/动态 SQL/shell。
- 编辑不等于 UPDATE 原版本：一律经 ``SR.save_definition`` 生成不可变新版本。
- 状态迁移只走 ``SR.transition`` 的合法边，前端不能任意跳状态。
- Runtime 构建失败时返回 ``runtime_ready=false`` + ``runtime_error``，
  不让整页 500；只有数据结构本身损坏才使用 5xx。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

import paper_trading as P
import strategy_dsl_schema as DSL
import strategy_registry as SR
import strategy_runtime as SRT

router = APIRouter(prefix="/api/strategies", tags=["strategies"])

# Registry 以 ValueError 表达业务拒绝；这里按语义映射成 HTTP 状态码。
_NOT_FOUND_TOKENS = (
    "unknown strategy id",
    "unknown source strategy version",
    "strategy has no immutable version",
)
_CONFLICT_TOKENS = (
    "already exists",
    "is reserved",
    "version changed",
    "status changed",
    "invalid lifecycle transition",
    "historical references",
    "only unused user drafts",
    "concurrently",
    "runtime is not ready",
)
_BAD_REQUEST_TOKENS = (
    "must match",
    "is required",
    "non-versioned strategy field",
    "invalid strategy status",
    "must be an object",
)
# 非对称风险门（唯一风险放大门）拒绝：语义是"这次定义变更被风控否决"，
# 历史上人工 API 返回 422，迁移到 Strategy Admin 后保持一致。
_RISK_GATE_TOKENS = ("风险放大", "证据不足", "观察期未满", "Challenger", "单轮放大")


def _raise_registry_error(exc: Exception, *, default: int = 400) -> None:
    message = str(exc) or type(exc).__name__
    lowered = message.lower()
    if any(token in lowered for token in _NOT_FOUND_TOKENS):
        code = 404
    elif any(token in message for token in _RISK_GATE_TOKENS):
        code = 422
    elif any(token in lowered for token in _CONFLICT_TOKENS):
        code = 409
    elif any(token in lowered for token in _BAD_REQUEST_TOKENS):
        code = 400
    else:
        code = default
    raise HTTPException(status_code=code, detail=message) from exc


# ---------------------------------------------------------------------------
# 序列化：注册表对象 → API 契约（不重算任何业务字段）
# ---------------------------------------------------------------------------

def _item(spec: SR.StrategySpec) -> dict:
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


def _runtime_payload(conn, strategy_id: str, spec: SR.StrategySpec) -> dict:
    """编译期只读画像。失败降级为 runtime_ready=false，不抛 5xx。"""
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


def _detail(conn, spec: SR.StrategySpec, *, with_runtime: bool = True) -> dict:
    version = SR.get_version(spec.id, conn=conn)
    payload = {
        **_item(spec),
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
        payload["runtime"] = _runtime_payload(conn, spec.id, spec)
        payload["runtime_ready"] = bool(payload["runtime"].get("runtime_ready"))
        payload["runtime_error"] = payload["runtime"].get("runtime_error")
    return payload


# ---------------------------------------------------------------------------
# 1) 只读列表
# ---------------------------------------------------------------------------

@router.get("")
def list_strategies(
    # Annotated + 默认值写法：保留 OpenAPI 元数据，同时让直接函数调用
    # （测试/内部复用）拿到真实的 None，而不是 Query 描述对象。
    origin: Annotated[str | None, Query(description="builtin / user")] = None,
    status: Annotated[
        str | None,
        Query(description="draft/validated/active/paused/retiring/archived"),
    ] = None,
    include_archived: Annotated[bool, Query()] = False,
):
    P.init_db()
    try:
        with P._db() as conn:
            specs = SR.list_definitions(
                conn=conn,
                origins=(origin,) if origin else None,
                statuses=(status,) if status else None,
                include_archived=include_archived,
            )
    except ValueError as exc:
        _raise_registry_error(exc)
    items = [_item(spec) for spec in specs]
    summary = {
        "total": len(items),
        "builtin": sum(1 for item in items if item["origin"] == "builtin"),
        "user": sum(1 for item in items if item["origin"] == "user"),
        "draft": sum(1 for item in items if item["status"] == "draft"),
        "active": sum(1 for item in items if item["status"] == "active"),
        "paused": sum(1 for item in items if item["status"] == "paused"),
    }
    return {"items": items, "summary": summary}


# ---------------------------------------------------------------------------
# 2) 单策略详情
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 3) 创建用户策略
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
def create_strategy(payload: dict | None = None):
    body = payload if isinstance(payload, Mapping) else {}
    strategy_id = str(body.get("id") or "").strip()
    name = str(body.get("name") or "").strip()
    description = str(body.get("description") or "").strip()
    metadata = body.get("metadata") or {}
    dsl_ast = body.get("dsl_ast")
    actor = str(body.get("actor") or "web-ui")
    if not strategy_id:
        raise HTTPException(status_code=400, detail="strategy id is required")
    if not name:
        raise HTTPException(status_code=400, detail="strategy name is required")
    if dsl_ast is not None:
        try:
            DSL.normalize(dsl_ast)
        except Exception as exc:  # noqa: BLE001 - DSL 校验器自身抛错也要 422
            raise HTTPException(status_code=422, detail=f"invalid DSL: {exc}") from exc
    P.init_db()
    try:
        with P._db() as conn:
            spec = SR.create_user_definition(
                conn, strategy_id, name, description=description,
                metadata=metadata, dsl_ast=dsl_ast, actor=actor,
            )
            return _detail(conn, spec)
    except ValueError as exc:
        _raise_registry_error(exc)


# ---------------------------------------------------------------------------
# 4) 编辑（生成不可变新版本）
# ---------------------------------------------------------------------------

def _save(strategy_id: str, payload: dict | None):
    body = payload if isinstance(payload, Mapping) else {}
    changes = body.get("changes")
    if changes is None:
        # PATCH 便捷形态：顶层直接给可版本化字段。
        changes = {key: body[key] for key in ("name", "description", "metadata", "dsl_ast")
                   if key in body}
    if not isinstance(changes, Mapping) or not changes:
        raise HTTPException(status_code=400, detail="changes must be a non-empty object")
    changes = dict(changes)
    if changes.get("dsl_ast") is not None:
        try:
            DSL.normalize(changes["dsl_ast"])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"invalid DSL: {exc}") from exc
    expected_version = body.get("expected_version")
    change_note = str(body.get("change_note") or "")
    actor = str(body.get("actor") or "web-ui")
    # 非对称风险门（唯一风险放大门）不暴露给 HTTP 调用方：任何请求都按
    # fail-closed 处理（risk_evidence/challenger_win 一律为空），风险放大
    # 只能走正规提案流程（自进化 / Champion 晋升），Web 端只允许收紧。
    risk_evidence = None
    challenger_win = False
    P.init_db()
    try:
        with P._db() as conn:
            version = SR.save_definition(
                conn, strategy_id, changes, expected_version=expected_version,
                actor=actor, change_note=change_note,
                risk_evidence=risk_evidence, challenger_win=challenger_win,
            )
            spec = SR.get(strategy_id, conn=conn)
            detail = _detail(conn, spec) if spec else {}
            return {
                "strategy": detail,
                "version": version.to_dict() if hasattr(version, "to_dict") else dict(version),
                "created_version": getattr(version, "version", None),
                "checksum": getattr(version, "checksum", None),
            }
    except ValueError as exc:
        _raise_registry_error(exc)


@router.post("/validate")
def validate_strategy(payload: dict | None = None):
    body = payload if isinstance(payload, Mapping) else {}
    ast = body.get("dsl_ast")
    metadata = body.get("metadata")
    errors: list[str] = []
    warnings: list[str] = []
    normalized = None
    checksum = None
    if ast is None:
        errors.append("dsl_ast is required")
    else:
        try:
            normalized, _, checksum = DSL.canonicalize(ast)
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


# ---------------------------------------------------------------------------
# 6) 预览（与 /api/paper/strategy-preview 同源，只是对外统一结构）
# ---------------------------------------------------------------------------

@router.post("/preview")
def preview_strategy(payload: dict | None = None):
    body = dict(payload) if isinstance(payload, Mapping) else {}
    try:
        raw = P.strategy_creation_preview(body)
    except Exception as exc:  # noqa: BLE001 - 预览失败不应 500 整页崩溃
        raise HTTPException(status_code=400, detail=f"preview failed: {exc}") from exc
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


# ---------------------------------------------------------------------------
# 7) 生命周期迁移
# ---------------------------------------------------------------------------

@router.put("/{strategy_id}")
def update_strategy(strategy_id: str, payload: dict | None = None):
    return _save(strategy_id, payload)


@router.patch("/{strategy_id}")
def patch_strategy(strategy_id: str, payload: dict | None = None):
    return _save(strategy_id, payload)


# ---------------------------------------------------------------------------
# 5) DSL 验证
# ---------------------------------------------------------------------------

# 字面量子路径必须声明在 ``/{strategy_id}`` 之后仍能被注册：新版
# FastAPI/Starlette（0.141+/1.6+）会丢弃被同层路径参数遮蔽的路由，
# 因此这里把 /validate、/preview 放在 /{strategy_id} 之前声明。
@router.get("/{strategy_id}")
def get_strategy(strategy_id: str):
    P.init_db()
    with P._db() as conn:
        spec = SR.get(strategy_id, conn=conn)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown strategy id")
    with P._db() as conn:
        return _detail(conn, spec)


@router.post("/{strategy_id}/transition")
def transition_strategy(strategy_id: str, payload: dict | None = None):
    body = payload if isinstance(payload, Mapping) else {}
    to_status = str(body.get("to_status") or "").strip()
    expected_status = body.get("expected_status")
    reason = str(body.get("reason") or "")
    actor = str(body.get("actor") or "web-ui")
    if not to_status:
        raise HTTPException(status_code=400, detail="to_status is required")
    P.init_db()
    try:
        with P._db() as conn:
            # validated 与 active 同样是"可进新周期"的入口门槛：进入
            # validated 前先跑一遍生产编译闸门（与被移除的旧 /validate
            # 端点语义一致），不就绪就拒绝。
            if to_status == "validated":
                readiness = SR.runtime_readiness(conn, strategy_id)
                if not readiness.get("runtime_ready"):
                    raise ValueError(
                        "strategy runtime is not ready: " + "; ".join(readiness.get("errors") or [])
                    )
            spec = SR.transition(
                conn, strategy_id, to_status, reason=reason, actor=actor,
                expected_status=expected_status if expected_status is not None else None,
            )
            return _detail(conn, spec)
    except ValueError as exc:
        # 生命周期拒绝（非法边 / 状态已变 / runtime 未就绪）统一 409。
        message = str(exc) or type(exc).__name__
        lowered = message.lower()
        if any(token in lowered for token in _NOT_FOUND_TOKENS):
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=409, detail=message) from exc


# ---------------------------------------------------------------------------
# 8) Clone
# ---------------------------------------------------------------------------

@router.post("/{strategy_id}/clone", status_code=201)
def clone_strategy(strategy_id: str, payload: dict | None = None):
    body = payload if isinstance(payload, Mapping) else {}
    # 旧前端（策略注册表）传的是 ``id``；新契约用 ``new_strategy_id``。
    new_strategy_id = str(body.get("new_strategy_id") or body.get("id") or "").strip()
    name = body.get("name")
    actor = str(body.get("actor") or "web-ui")
    if not new_strategy_id:
        raise HTTPException(status_code=400, detail="new_strategy_id is required")
    P.init_db()
    try:
        with P._db() as conn:
            source_version = body.get("source_version")
            if source_version is None:
                current = SR.get_version(strategy_id, conn=conn)
                source_version = current.version if current else None
            spec = SR.clone_definition(
                conn, strategy_id, source_version, new_strategy_id,
                name=None if name is None else str(name), actor=actor,
            )
            return _detail(conn, spec)
    except ValueError as exc:
        _raise_registry_error(exc)


# ---------------------------------------------------------------------------
# 9) 版本历史与生命周期时间线
# ---------------------------------------------------------------------------

@router.get("/{strategy_id}/versions")
def list_strategy_versions(strategy_id: str):
    P.init_db()
    with P._db() as conn:
        if SR.get(strategy_id, conn=conn) is None:
            raise HTTPException(status_code=404, detail="unknown strategy id")
        versions = SR.list_versions(strategy_id, conn=conn)
    return {
        "strategy_id": strategy_id,
        "items": [
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
            for version in versions
        ],
    }


@router.get("/{strategy_id}/events")
def list_strategy_events(strategy_id: str):
    P.init_db()
    with P._db() as conn:
        if SR.get(strategy_id, conn=conn) is None:
            raise HTTPException(status_code=404, detail="unknown strategy id")
        events = SR.lifecycle_events(conn, strategy_id)
    return {"strategy_id": strategy_id, "items": [dict(row) for row in events]}


# ---------------------------------------------------------------------------
# 10) 删除（仅未使用的用户 draft）
# ---------------------------------------------------------------------------

@router.delete("/{strategy_id}")
def delete_strategy(strategy_id: str):
    P.init_db()
    try:
        with P._db() as conn:
            result = SR.hard_delete_unused_draft(conn, strategy_id)
        return {**result, "archived_instead_hint": None}
    except ValueError as exc:
        message = str(exc) or type(exc).__name__
        lowered = message.lower()
        if any(token in lowered for token in _NOT_FOUND_TOKENS):
            raise HTTPException(status_code=404, detail=message) from exc
        hint = None
        if "historical references" in lowered or "only unused" in lowered:
            hint = "该策略已有历史引用，请使用归档（transition → retiring → archived）而非删除"
        raise HTTPException(
            status_code=409,
            detail={"message": message, "hint": hint} if hint else message,
        ) from exc
