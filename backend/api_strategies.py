# -*- coding: utf-8 -*-
"""PR-51：Strategy Admin HTTP 层（``/api/strategies``）。

分层（PR-51 起）：

    FastAPI route  →  typed contract (strategy_api_models)
                   →  StrategyService (strategy_service)
                   →  Registry / Runtime / 预览引擎

本模块只负责四件事，不再承担任何 domain/application 职责：

1. 解析 HTTP 请求（路径参数、查询参数、请求体形状）；
2. 调用 ``strategy_service`` 的用例；
3. 把 domain exception 映射成 HTTP 状态码；
4. 返回响应。

因此这里**不再**出现 ``paper_trading._db()``，也**不再**用
``if "already exists" in message`` 之类的字符串匹配来猜状态码——分类只在
``strategy_service.translate_registry_error`` 一处发生。

约束（与产品原则一致）：
- 用户只能提交声明式 DSL/AST；本模块不做 eval/exec/动态 SQL/shell。
- 编辑不等于 UPDATE 原版本：一律经 ``strategy_service.update_strategy`` 生成
  不可变新版本。
- 状态迁移只走 ``SR.transition`` 的合法边，前端不能任意跳状态。
- Runtime 构建失败时返回 ``runtime_ready=false`` + ``runtime_error``，
  不让整页 500；只有数据结构本身损坏才使用 5xx。
- 风险放大唯一入口（PR-33）不由 HTTP 暴露：``risk_evidence`` /
  ``challenger_win`` 即使出现在 body 里也会被请求契约丢弃，定义变更照常
  fail-closed（Web 只能收紧风险）。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

import strategy_api_models as Models
import strategy_service as SVC

router = APIRouter(prefix="/api/strategies", tags=["strategies"])

# 已有历史引用的策略只能归档，删除请求给出可执行的替代路径。
ARCHIVE_INSTEAD_HINT = "该策略已有历史引用，请使用归档（transition → retiring → archived）而非删除"

# domain exception → HTTP 状态码。顺序敏感：子类必须排在基类之前。
_ERROR_STATUS = (
    (SVC.StrategyNotFound, 404),
    (SVC.StrategyRiskExpansionRejected, 422),
    (SVC.InvalidStrategyDsl, 422),
    (SVC.StrategyConflict, 409),
    (SVC.InvalidStrategyDefinition, 400),
    (SVC.StrategyError, 400),
)


def status_for_error(exc: SVC.StrategyError) -> int:
    for error_type, status in _ERROR_STATUS:
        if isinstance(exc, error_type):
            return status
    return 400


def _raise_http(exc: SVC.StrategyError) -> None:
    raise HTTPException(status_code=status_for_error(exc), detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 请求体：typed contract 的适配（同时支持测试/内部直接调用传 dict）
# ---------------------------------------------------------------------------

def request_validation_status(errors) -> int:
    """缺少必填字段沿用历史 400（旧实现是手写 ``xxx is required``）；
    其余形状/类型错误按标准 422 处理。"""
    return 400 if any((err or {}).get("type") == "missing" for err in (errors or [])) else 422


def request_validation_detail(errors) -> str:
    """把 pydantic 错误归一成单行 detail，缺失字段沿用历史文案。"""
    first = (errors or [{}])[0] or {}
    location = [str(part) for part in (first.get("loc") or ())]
    field = location[-1] if location else "request"
    if first.get("type") == "missing":
        return f"{field} is required"
    return str(first.get("msg") or "invalid request")


def _coerce(model, payload):
    """把 dict / 模型实例统一成请求模型；校验失败直接映射成 HTTP 错误。

    HTTP 请求由 FastAPI 先按 ``model`` 解析（main.py 注册了同一套状态码
    约定的校验错误映射）；测试与内部调用直接传 dict，走同一条校验路径，
    因此两种入口的 4xx 语义一致。
    """
    if isinstance(payload, model):
        return payload
    try:
        return model.model_validate({} if payload is None else payload)
    except ValidationError as exc:
        errors = exc.errors()
        raise HTTPException(
            status_code=request_validation_status(errors),
            detail=request_validation_detail(errors),
        ) from exc


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
    try:
        specs = SVC.list_strategies(
            origin=origin, status=status, include_archived=include_archived,
        )
    except SVC.StrategyError as exc:
        _raise_http(exc)
    items = [SVC.item_payload(spec) for spec in specs]
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
def create_strategy(payload: Models.StrategyCreateRequest | None = None):
    try:
        return SVC.create_strategy(_coerce(Models.StrategyCreateRequest, payload))
    except SVC.StrategyError as exc:
        _raise_http(exc)


# ---------------------------------------------------------------------------
# 4) 编辑（生成不可变新版本）
# ---------------------------------------------------------------------------

def _save(strategy_id: str, payload):
    try:
        return SVC.update_strategy(
            strategy_id, _coerce(Models.StrategyUpdateRequest, payload),
        )
    except SVC.StrategyError as exc:
        _raise_http(exc)


@router.put("/{strategy_id}")
def update_strategy(strategy_id: str, payload: Models.StrategyUpdateRequest | None = None):
    return _save(strategy_id, payload)


@router.patch("/{strategy_id}")
def patch_strategy(strategy_id: str, payload: Models.StrategyUpdateRequest | None = None):
    return _save(strategy_id, payload)


# ---------------------------------------------------------------------------
# 5) DSL 验证
# ---------------------------------------------------------------------------

@router.post("/validate", response_model=Models.StrategyValidateResponse)
def validate_strategy(payload: Models.StrategyValidateRequest | None = None):
    request = _coerce(Models.StrategyValidateRequest, payload)
    return SVC.validate_definition(request.dsl_ast, request.metadata)


# ---------------------------------------------------------------------------
# 6) 预览（与 /api/paper/strategy-preview 同源，只是对外统一结构）
# ---------------------------------------------------------------------------

@router.post("/preview")
def preview_strategy(payload: Models.StrategyPreviewRequest | None = None):
    try:
        return SVC.preview_strategy(_coerce(Models.StrategyPreviewRequest, payload))
    except SVC.StrategyError as exc:
        _raise_http(exc)


# ---------------------------------------------------------------------------
# 7) 生命周期迁移
# ---------------------------------------------------------------------------

# 字面量子路径必须声明在 ``/{strategy_id}`` 之后仍能被注册：新版
# FastAPI/Starlette（0.141+/1.6+）会丢弃被同层路径参数遮蔽的路由，
# 因此这里把 /validate、/preview 放在 /{strategy_id} 之前声明。
@router.get("/{strategy_id}")
def get_strategy(strategy_id: str):
    try:
        return SVC.detail(strategy_id)
    except SVC.StrategyError as exc:
        _raise_http(exc)


@router.post("/{strategy_id}/transition")
def transition_strategy(strategy_id: str, payload: Models.StrategyTransitionRequest | None = None):
    try:
        return SVC.transition(
            strategy_id, _coerce(Models.StrategyTransitionRequest, payload),
        )
    except SVC.StrategyError as exc:
        _raise_http(exc)


# ---------------------------------------------------------------------------
# 8) Clone
# ---------------------------------------------------------------------------

@router.post("/{strategy_id}/clone", status_code=201)
def clone_strategy(strategy_id: str, payload: Models.StrategyCloneRequest | None = None):
    try:
        return SVC.clone_strategy(
            strategy_id, _coerce(Models.StrategyCloneRequest, payload),
        )
    except SVC.StrategyError as exc:
        _raise_http(exc)


# ---------------------------------------------------------------------------
# 9) 版本历史与生命周期时间线
# ---------------------------------------------------------------------------

@router.get("/{strategy_id}/versions")
def list_strategy_versions(strategy_id: str):
    try:
        return {"strategy_id": strategy_id, "items": SVC.list_versions(strategy_id)}
    except SVC.StrategyError as exc:
        _raise_http(exc)


@router.get("/{strategy_id}/events")
def list_strategy_events(strategy_id: str):
    try:
        return {"strategy_id": strategy_id, "items": SVC.list_events(strategy_id)}
    except SVC.StrategyError as exc:
        _raise_http(exc)


# ---------------------------------------------------------------------------
# 10) 删除（仅限**从未离开 draft 生命周期**的用户草稿：PR-56 起，
#     draft→validated→draft 的回退不再具备删除资格，走归档提示）
# ---------------------------------------------------------------------------

@router.delete("/{strategy_id}", response_model=Models.StrategyDeleteResponse)
def delete_strategy(strategy_id: str):
    try:
        return SVC.delete_unused_draft(strategy_id)
    except SVC.StrategyError as exc:
        hint = (
            ARCHIVE_INSTEAD_HINT
            if isinstance(exc, SVC.StrategyHistoricalReferenceError) else None
        )
        message = str(exc)
        raise HTTPException(
            status_code=status_for_error(exc),
            detail={"message": message, "hint": hint} if hint else message,
        ) from exc
