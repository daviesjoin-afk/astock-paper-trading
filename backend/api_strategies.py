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
- 状态迁移经 Strategy Promotion 证据 policy 与 Strategy Lifecycle CAS owner。
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
import strategy_candidate as SC
import strategy_candidate_repository as SCRepo
import strategy_candidate_service as SCV
import strategy_generator as SG
import strategy_health as SH
import strategy_health_repository as SHR
import strategy_health_service as SHV
import strategy_retirement_policy as RP
import strategy_retirement_repository as RR
import strategy_retirement_service as RTV
import strategy_retirement_workflow as RWF
import strategy_retirement_workflow_repository as RWFR
import strategy_retirement_workflow_service as RWS
import strategy_service as SVC

router = APIRouter(prefix="/api/strategies", tags=["strategies"])
retirement_workflow_router = APIRouter(tags=["strategy-retirement-workflow"])

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


def _raise_health_http(exc: ValueError) -> None:
    """Health-evidence rejections are input/identity conflicts, never 5xx."""
    status = 404 if str(exc) == "health_snapshot_not_found" else 400
    raise HTTPException(status_code=status, detail=str(exc)) from exc


def _raise_retirement_http(exc: ValueError) -> None:
    """Retirement-policy rejections are input/identity conflicts, never 5xx."""
    status = 404 if "not_found" in str(exc) else 400
    raise HTTPException(status_code=status, detail=str(exc)) from exc


def _raise_workflow_http(exc: ValueError) -> None:
    reason = str(exc)
    if "not_found" in reason:
        status = 404
    elif any(token in reason for token in (
            "mismatch", "changed", "stale", "conflict", "already", "not_pending",
            "required", "no_executable", "invalid_lifecycle_transition")):
        status = 409
    else:
        status = 400
    raise HTTPException(status_code=status, detail=reason) from exc


def _raise_candidate_http(exc: ValueError) -> None:
    """Candidate contract rejections are input/identity conflicts, never 5xx.

    ``not_found`` 是 404；身份不符（checksum / fingerprint / parent 不存在）是 409；
    其余（形状、未声明参数、越界、缺 provenance）是 400。生成域**不会**返回 5xx：
    它要么给出一个可验证的候选，要么明确拒绝。
    """
    reason = str(exc)
    if "not_found" in reason:
        status = 404
    elif any(token in reason for token in (
            "mismatch", "unavailable", "conflict", "idempotency")):
        status = 409
    else:
        status = 400
    raise HTTPException(status_code=status, detail=reason) from exc


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
        Query(description="draft/candidate/research/validated/shadow/paper/production_sim/degraded/paused/retiring/archived/rejected/validation_failed/quarantined"),
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
        "formal_cycle_eligible": sum(1 for item in items if item["formal_cycle_allowed"]),
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


@router.get("/{strategy_id}/lifecycle")
def get_strategy_lifecycle(strategy_id: str):
    try:
        return SVC.lifecycle_read_model(strategy_id)
    except SVC.StrategyError as exc:
        _raise_http(exc)


@router.get("/{strategy_id}/challenger")
def get_strategy_challenger(
    strategy_id: str,
    comparison_report_id: Annotated[str | None, Query(max_length=64)] = None,
    version: Annotated[int | None, Query(ge=1)] = None,
):
    """Active vs Challenger workspace facts for exactly the named evidence."""
    try:
        return SVC.challenger_read_model(
            strategy_id, comparison_report_id=comparison_report_id, version=version)
    except SVC.StrategyError as exc:
        _raise_http(exc)


@router.post("/{strategy_id}/promotion/proposals", status_code=201)
def create_strategy_promotion_proposal(
    strategy_id: str, payload: Models.PromotionProposalRequest | None = None,
):
    try:
        return SVC.create_promotion_proposal(
            strategy_id, _coerce(Models.PromotionProposalRequest, payload),
        )
    except SVC.StrategyError as exc:
        _raise_http(exc)


@router.get("/{strategy_id}/promotion/proposals")
def list_strategy_promotion_proposals(
    strategy_id: str,
    version: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    try:
        return {"strategy_id": strategy_id,
                "items": SVC.list_promotion_proposals(strategy_id, version=version, limit=limit)}
    except SVC.StrategyError as exc:
        _raise_http(exc)


@router.post("/{strategy_id}/health/snapshots", status_code=201)
def capture_strategy_health_snapshot(
    strategy_id: str, payload: Models.StrategyHealthCaptureRequest | None = None,
):
    """Capture one exact strategy health snapshot (evidence only)."""
    request = _coerce(Models.StrategyHealthCaptureRequest, payload)
    try:
        return SHV.capture_strategy_health(
            strategy_id, strategy_version=request.strategy_version,
            strategy_checksum=request.strategy_checksum,
            observation_start=request.observation_start,
            observation_end=request.observation_end,
            comparison_report_id=request.comparison_report_id)
    except SVC.StrategyError as exc:
        _raise_http(exc)
    except (SH.HealthEvidenceError, SHR.StrategyHealthRepositoryError) as exc:
        _raise_health_http(exc)


@router.get("/{strategy_id}/health/snapshots/{snapshot_id}")
def get_strategy_health_snapshot(strategy_id: str, snapshot_id: str):
    """Read exactly one health snapshot by id — there is no latest endpoint."""
    try:
        return SHV.get_health_snapshot(strategy_id, snapshot_id)
    except SVC.StrategyError as exc:
        _raise_http(exc)
    except (SH.HealthEvidenceError, SHR.StrategyHealthRepositoryError) as exc:
        _raise_health_http(exc)


@router.post("/{strategy_id}/retirement/evaluate", status_code=201)
def evaluate_strategy_retirement(
    strategy_id: str, payload: Models.StrategyRetirementEvaluateRequest | None = None,
):
    """Evaluate one explicitly named health snapshot into a policy recommendation."""
    request = _coerce(Models.StrategyRetirementEvaluateRequest, payload)
    try:
        return RTV.evaluate_retirement(strategy_id, snapshot_id=request.snapshot_id)
    except SVC.StrategyError as exc:
        _raise_http(exc)
    except (RP.RetirementPolicyError, RR.StrategyRetirementRepositoryError,
            SHR.StrategyHealthRepositoryError, SH.HealthEvidenceError) as exc:
        _raise_retirement_http(exc)


@router.get("/{strategy_id}/retirement/decisions/{decision_id}")
def get_strategy_retirement_decision(strategy_id: str, decision_id: str):
    """Read exactly one retirement decision by id — there is no status endpoint."""
    try:
        return RTV.get_retirement_decision(strategy_id, decision_id)
    except SVC.StrategyError as exc:
        _raise_http(exc)
    except (RP.RetirementPolicyError, RR.StrategyRetirementRepositoryError) as exc:
        _raise_retirement_http(exc)


@router.post("/{strategy_id}/retirement/proposals", status_code=201)
def create_strategy_retirement_proposal(
    strategy_id: str, payload: Models.RetirementProposalRequest | None = None,
):
    request = _coerce(Models.RetirementProposalRequest, payload)
    try:
        return RWS.create_transition_proposal(strategy_id, decision_id=request.decision_id)
    except (RWF.RetirementWorkflowError,
            RWFR.RetirementWorkflowRepositoryError) as exc:
        _raise_workflow_http(exc)


@retirement_workflow_router.post("/api/retirement/proposals/{proposal_id}/approve")
def approve_retirement_proposal(
    proposal_id: str, payload: Models.RetirementApprovalRequest | None = None,
):
    request = _coerce(Models.RetirementApprovalRequest, payload)
    try:
        return RWS.approve_transition_proposal(
            proposal_id, operator_identity=request.operator_identity,
            approval_action=request.approval_action, reason=request.reason)
    except (RWF.RetirementWorkflowError,
            RWFR.RetirementWorkflowRepositoryError) as exc:
        _raise_workflow_http(exc)


@retirement_workflow_router.post("/api/retirement/proposals/{proposal_id}/execute")
def execute_retirement_proposal(proposal_id: str):
    try:
        return RWS.execute_transition_proposal(proposal_id)
    except (RWF.RetirementWorkflowError,
            RWFR.RetirementWorkflowRepositoryError) as exc:
        _raise_workflow_http(exc)


@retirement_workflow_router.get("/api/retirement/proposals/{proposal_id}")
def get_retirement_proposal(proposal_id: str):
    try:
        return RWS.get_transition_proposal(proposal_id)
    except (RWF.RetirementWorkflowError,
            RWFR.RetirementWorkflowRepositoryError) as exc:
        _raise_workflow_http(exc)


# ---------------------------------------------------------------------------
# R35-A：策略候选（StrategyCandidate）生成与只读投影
# ---------------------------------------------------------------------------

@router.post("/{strategy_id}/candidates", status_code=201)
def generate_strategy_candidates(
    strategy_id: str, payload: Models.StrategyCandidateGenerateRequest | None = None,
):
    """Generate constrained candidates from one exact pinned parent version.

    R35-A 只产出 ``StrategyCandidate``：不下单、不改 lifecycle、不产生晋级结论，
    也不评估候选表现。父策略只作为 pinned baseline 存在。
    """
    request = _coerce(Models.StrategyCandidateGenerateRequest, payload)
    try:
        return SCV.capture_strategy_candidates(
            strategy_id,
            strategy_version=request.strategy_version,
            strategy_checksum=request.strategy_checksum,
            asof=request.asof,
            parameter_adjustments=request.parameter_adjustments,
            universe_spec=request.universe_spec,
            intended_market_regime=request.intended_market_regime,
            evidence_count=request.evidence_count,
            hypothesis_id=request.hypothesis_id,
            research_provenance=request.research_provenance,
            random_seed=request.random_seed,
            model_identity=request.model_identity,
            constraints=request.constraints)
    except (SCV.StrategyCandidateUnavailable, SC.CandidateValidationError,
            SG.StrategyGeneratorError, SCRepo.StrategyCandidateRepositoryError) as exc:
        _raise_candidate_http(exc)


@router.get("/{strategy_id}/candidates")
def list_strategy_candidates(
    strategy_id: str,
    strategy_version: Annotated[int, Query(ge=1)] = ...,
    strategy_checksum: Annotated[str, Query(min_length=64, max_length=64)] = ...,
):
    """List candidates pinned to one **exact** parent version (never the head)."""
    try:
        return SCV.read_parent_candidates(
            strategy_id, strategy_version=strategy_version,
            strategy_checksum=strategy_checksum)
    except (SCV.StrategyCandidateUnavailable, SC.CandidateValidationError,
            SCRepo.StrategyCandidateRepositoryError) as exc:
        _raise_candidate_http(exc)


@router.get("/{strategy_id}/candidates/{candidate_id}")
def get_strategy_candidate(strategy_id: str, candidate_id: str):
    """Read exactly one candidate by id — there is no latest endpoint.

    返回的是 candidate 台账里的只读事实（含 parent pin 与提案历史）。候选是否优秀、
    能否晋级、能否进 Shadow 一律**不**在这里发布，前端也不得自行判断。
    """
    try:
        result = SCV.read_strategy_candidate(candidate_id)
    except (SCV.StrategyCandidateUnavailable, SC.CandidateValidationError,
            SCRepo.StrategyCandidateRepositoryError) as exc:
        _raise_candidate_http(exc)
    if str(result["candidate"].get("parent_strategy_id") or "") != str(strategy_id):
        # 页面身份与候选身份必须一致，否则就是把 A 的候选显示成 B 的。
        _raise_candidate_http(SC.CandidateValidationError("candidate_strategy_mismatch"))
    return result


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
