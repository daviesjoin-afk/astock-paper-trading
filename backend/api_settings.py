"""Unified operator settings API.

The endpoint intentionally separates public runtime controls from AI secrets:
GET responses contain only masked key status and POST bodies are never echoed.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

import notify_wxpusher as NOTIFY
import operator_auth
import paper_sizing as sizing
import paper_trading as P
import paper_storage as PST
import runtime_settings as RSET


def _get_adaptive():
    override = globals().get("adaptive")
    if override is not None:
        return override
    import adaptive_engine
    return adaptive_engine


def __getattr__(name):
    if name == "adaptive":
        return _get_adaptive()
    raise AttributeError(name)


router = APIRouter(prefix="/api/settings", tags=["runtime-settings"])


def _require_confirmation(confirmed: bool, action: str) -> None:
    if not confirmed:
        raise HTTPException(status_code=409, detail=f"请先在页面确认{action}")


def _planned_end(started_at: str | None, duration_days: int | None) -> str | None:
    if not duration_days or not started_at:
        return None
    try:
        value = dt.datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        # Cycle durations are expressed in trading days.  We do not embed an
        # exchange-holiday calendar in the settings API, so weekends are
        # skipped here and the scheduler remains responsible for holiday
        # handling at execution time.
        current = value.date()
        remaining = int(duration_days)
        while remaining > 0:
            current += dt.timedelta(days=1)
            if current.weekday() < 5:
                remaining -= 1
        return current.isoformat()
    except (TypeError, ValueError):
        return None


def _snapshot() -> dict:
    adaptive = _get_adaptive()
    P.init_db()
    with PST.paper_db() as conn:  # the storage boundary owns the connection
        settings = RSET.read(conn)
        # appToken 只回显掩码：明文不得出现在任何 GET 响应里。
        notification = dict(settings.get("notification") or {})
        raw_token = str(notification.get("wxpusher_app_token") or "")
        notification["wxpusher_app_token"] = NOTIFY.mask_token(raw_token)
        notification["wxpusher_token_configured"] = bool(raw_token)
        settings["notification"] = notification
        cycle = P.load_active_cycle(conn)
        audit = RSET.audit(conn, 40)
        capital = float(cycle.get("capital") or settings["simulation"]["default_starting_capital"])
        enabled = cycle.get("enabled_strategies")
        if isinstance(enabled, str):
            enabled = RSET.decode(enabled, None)
        enabled = list(enabled) if isinstance(enabled, (list, tuple)) else RSET.enabled_strategies(conn)
        cycle_duration = cycle.get("duration_days")
        if cycle_duration is None:
            cycle_duration = settings["simulation"]["cycle_duration_days"]
        slots = int(settings["risk"]["shared_pool_position_limit"])
        exposure = float(settings["risk"]["shared_pool_exposure_cap"])
        minimum = sizing.dynamic_minimum_order_amount(
            capital, slots, exposure_cap=exposure,
            slot_utilization=float(settings["risk"]["minimum_entry_slot_utilization"]),
        )
        current = {
            "id": cycle.get("id"), "cycle_key": cycle.get("cycle_key"),
            "status": cycle.get("status"), "capital": capital,
            "duration_days": int(cycle_duration or 0),
            "duration_label": RSET.cycle_duration_label(cycle_duration),
            "started_at": cycle.get("started_at"),
            "planned_end": _planned_end(cycle.get("started_at"), cycle_duration),
            "enabled_strategies": enabled,
            "historical_unchanged": True,
        }
        defaults = RSET.defaults(conn)
        metadata = RSET.metadata(conn)
    # adaptive_engine masks keys before returning them; no raw credential is
    # ever copied into the unified snapshot.
    try:
        ai_schema = adaptive.ai_settings_schema()
        ai_keys = adaptive.get_dual_ai_api_keys_fn()
        ai_review = adaptive.ai_review_settings_fn()
    except Exception as exc:
        ai_schema = {"settings": {}, "parameters": {}, "providers": {}}
        ai_keys = {}
        ai_review = {"slots": {}, "review_mode": None, "single_reviewer_slot": None}
        ai_error = f"{type(exc).__name__}: {exc}"
    else:
        ai_error = None
    return {
        "schema_version": "settings-v1",
        "settings": settings,
        "defaults": {
            "simulation": {key: defaults[key] for key in RSET.SETTING_GROUPS["simulation"]},
            "risk": {key: defaults[key] for key in RSET.SETTING_GROUPS["risk"]},
            "strategy": {"strategy_overrides": defaults["strategy_overrides"]},
            "evolution": {"evolution_interval_hours": defaults["evolution_interval_hours"]},
            "execution": {key: defaults[key] for key in RSET.SETTING_GROUPS["execution"]},
            "notification": {key: defaults[key] for key in RSET.SETTING_GROUPS["notification"]},
            "security": {key: defaults[key] for key in RSET.SETTING_GROUPS["security"]},
        },
        "metadata": metadata,
        "effective": {
            "current_cycle": current,
            "next_cycle": {
                "capital": settings["simulation"]["default_starting_capital"],
                "duration_days": settings["simulation"]["cycle_duration_days"],
                "duration_label": RSET.cycle_duration_label(settings["simulation"]["cycle_duration_days"]),
                "enabled_strategies": settings["simulation"]["enabled_strategies"],
            },
            "dynamic_minimum_order_amount": minimum,
            "single_position_max_amount": settings["risk"]["single_position_max_amount"] or None,
            "risk_change_effect": "下一次扫描读取最新风控设置；历史成交与归档不改写",
        },
        "ai": {
            "settings": ai_schema.get("settings", {}),
            "parameters": ai_schema.get("parameters", {}),
            "providers": ai_schema.get("providers", {}),
            "keys": ai_keys,
            # 通用槽位权威视图（review_mode + ai1/ai2 掩码状态），
            # 绝不含明文 Key；前端与其它调用方都应优先读这里。
            "review": ai_review,
            "error": ai_error,
        },
        "audit": audit,
    }


@router.get("/")
def settings_snapshot():
    try:
        return _snapshot()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取设置失败：{type(exc).__name__}") from exc


def _apply_runtime_settings(runtime_updates: dict, ai_updates: dict,
                            clear_token: bool = False) -> dict:
    """一次完整的同步业务单元：连接与事务都在**线程内**创建和关闭。

    ``P.init_db()`` / ``PST.paper_db(immediate=True)`` 是同步 SQLite 调用，且可能要等
    写锁（普通数据库锁等待可达 60 秒）。在 ``async def`` 里直接执行它们会阻塞
    事件循环本身 —— 同一进程调度其它请求也正是这条循环，于是一个保存设置的
    请求会把并发心跳一起拖住。所以整块搬进线程池，而不是只搬某一行。
    """
    adaptive = _get_adaptive()
    P.init_db()
    with PST.paper_db(immediate=True) as conn:
        if clear_token:
            runtime_updates["wxpusher_app_token"] = ""
        if runtime_updates:
            RSET.update(conn, runtime_updates, actor="human-ui")
            P.audit(conn, None, "settings_changed", "统一设置中心更新运行参数（详见设置审计）")
            # 保存后立即生效，不必等重启；关闭时该状态会写进设置审计。
            if "operator_auth_required" in runtime_updates:
                operator_auth.set_bypass(not bool(runtime_updates["operator_auth_required"]))
    if ai_updates:
        adaptive.update_ai_settings(ai_updates)
    return _snapshot()


@router.post("/")
async def update_settings(request: Request, confirmed: bool = Query(False)):
    _require_confirmation(confirmed, "保存运行设置")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="设置内容必须是JSON对象") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="设置内容必须是JSON对象")
    body = dict(body)
    # appToken 语义与 AI Key 一致：留空表示保持原值，清除必须显式声明，
    # 否则一次只改发送时间的前端提交会把凭据抹掉。
    clear_token = False
    notification_body = body.get("notification")
    if isinstance(notification_body, dict):
        notification_body = dict(notification_body)
        clear_token = bool(notification_body.pop("clear_app_token", False))
        if not str(notification_body.get("wxpusher_app_token") or "").strip():
            notification_body.pop("wxpusher_app_token", None)
        body["notification"] = notification_body
    allowed_groups = set(RSET.SETTING_GROUPS) | {"ai"}
    unknown_groups = set(body) - allowed_groups
    if unknown_groups:
        raise HTTPException(status_code=422, detail="未知设置分组: " + ",".join(sorted(unknown_groups)))
    runtime_updates = {}
    for group in RSET.SETTING_GROUPS:
        value = body.get(group)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise HTTPException(status_code=422, detail=f"{group}设置必须是对象")
        runtime_updates.update(value)
    ai_updates = body.get("ai") or {}
    if ai_updates and not isinstance(ai_updates, dict):
        raise HTTPException(status_code=422, detail="AI设置必须是对象")
    try:
        return await run_in_threadpool(
            _apply_runtime_settings, runtime_updates, ai_updates, clear_token,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存设置失败：{type(exc).__name__}") from exc


@router.post("/ai-key")
async def update_ai_key(request: Request, confirmed: bool = Query(False)):
    """历史入口（兼容保留）：``provider`` 作为 ``ai1``/``ai2`` 的别名。

    新代码请用 ``PUT /api/settings/ai-review/slots/{slot}``；本路由的返回结构与
    ``/ai-review`` 完全一致，不再返回厂商专属视图。
    """
    adaptive = _get_adaptive()
    _require_confirmation(confirmed, "保存AI接口配置")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="AI接口配置必须是JSON对象") from exc
    if not isinstance(body, dict) or body.get("provider") is None:
        raise HTTPException(status_code=422, detail="provider必须是 ai1 或 ai2（兼容 mimo/deepseek 别名）")
    api_key = body.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 200):
        raise HTTPException(status_code=422, detail="API Key格式无效")

    def _write() -> dict:
        adaptive.update_ai_slot_fn(
            body["provider"],
            api_key=api_key, base_url=body.get("base_url"), model=body.get("model"),
            enabled=body.get("enabled"),
        )
        return _snapshot()

    try:
        return await run_in_threadpool(_write)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ─── 通用 AI 槽位设置（ai1 / ai2）───
# 这是设置页唯一的 AI 权威接口：GET 永不返回明文 Key，PUT 用空 api_key 保持旧 Key，
# 需要清空必须显式 clear_api_key=true。

_AI_REVIEW_KEYS = {"review_mode", "single_reviewer_slot"}
_AI_SLOT_KEYS = {
    "api_key", "base_url", "model", "enabled",
    "display_name", "timeout_seconds", "clear_api_key", "provider_id",
}


async def _read_json_object(request: Request, message: str) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=message) from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail=message)
    return body


@router.get("/ai-review")
def ai_review_snapshot():
    adaptive = _get_adaptive()
    try:
        return adaptive.ai_review_settings_fn()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取AI审核设置失败：{type(exc).__name__}") from exc


@router.put("/ai-review")
async def update_ai_review(request: Request, confirmed: bool = Query(False)):
    _require_confirmation(confirmed, "切换AI审核模式")
    body = await _read_json_object(request, "AI审核设置必须是JSON对象")
    unknown = set(body) - _AI_REVIEW_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail="未知设置项: " + ",".join(sorted(unknown)))
    if not body:
        raise HTTPException(status_code=422, detail="至少需要提供 review_mode 或 single_reviewer_slot")

    def _write() -> dict:
        return _get_adaptive().update_ai_review_settings_fn(
            review_mode=body.get("review_mode"),
            single_reviewer_slot=body.get("single_reviewer_slot"),
        )

    try:
        return await run_in_threadpool(_write)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存AI审核设置失败：{type(exc).__name__}") from exc


@router.put("/ai-review/slots/{slot}")
async def update_ai_review_slot(slot: str, request: Request, confirmed: bool = Query(False)):
    _require_confirmation(confirmed, "保存AI槽位配置")
    body = await _read_json_object(request, "AI槽位配置必须是JSON对象")
    unknown = set(body) - _AI_SLOT_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail="未知设置项: " + ",".join(sorted(unknown)))
    api_key = body.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 200):
        raise HTTPException(status_code=422, detail="API Key格式无效")
    if "enabled" in body and not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=422, detail="enabled必须是布尔值")
    if "clear_api_key" in body and not isinstance(body["clear_api_key"], bool):
        raise HTTPException(status_code=422, detail="clear_api_key必须是布尔值")
    if "provider_id" in body and not isinstance(body["provider_id"], str):
        raise HTTPException(status_code=422, detail="provider_id必须是字符串")

    def _write() -> dict:
        return _get_adaptive().update_ai_slot_fn(
            slot,
            api_key=api_key,
            base_url=body.get("base_url"),
            model=body.get("model"),
            enabled=body.get("enabled"),
            display_name=body.get("display_name"),
            timeout_seconds=body.get("timeout_seconds"),
            clear_api_key=bool(body.get("clear_api_key")),
            **({"provider_id": body["provider_id"]} if "provider_id" in body else {}),
        )

    try:
        return await run_in_threadpool(_write)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存AI槽位失败：{type(exc).__name__}") from exc


@router.post("/ai-review/providers")
async def create_ai_provider(request: Request, confirmed: bool = Query(False)):
    return await _save_ai_provider(request, confirmed)


@router.put("/ai-review/providers/{provider_id}")
async def update_ai_provider(provider_id: str, request: Request, confirmed: bool = Query(False)):
    return await _save_ai_provider(request, confirmed, provider_id)


async def _save_ai_provider(request, confirmed, provider_id=None):
    _require_confirmation(confirmed, "保存AI提供商")
    body = await _read_json_object(request, "提供商配置必须是JSON对象")
    try:
        return _get_adaptive().save_ai_provider_fn(provider_id, **body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存提供商失败：{type(exc).__name__}") from exc


@router.post("/ai-review/providers/{provider_id}/models/refresh")
def refresh_ai_provider_models(provider_id: str, confirmed: bool = Query(False)):
    _require_confirmation(confirmed, "读取提供商模型")
    try:
        return _get_adaptive().refresh_ai_provider_models_fn(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取模型失败：{type(exc).__name__}") from exc


@router.post("/ai-review/slots/{slot}/test")
def test_ai_review_slot(slot: str, confirmed: bool = Query(False)):
    """对指定槽位做一次真实连通性探测；返回结构绝不含明文 Key。"""
    adaptive = _get_adaptive()
    _require_confirmation(confirmed, "测试AI槽位连通性")
    try:
        return adaptive.test_ai_slot_fn(slot)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"测试AI槽位失败：{type(exc).__name__}") from exc


@router.get("/ai-review/slots/{slot}/models")
def list_ai_review_slot_models(slot: str):
    """列出该槽位上游的可用模型。只读：不发 chat 请求、不消耗 token。

    用已保存的凭据去问上游，界面上的未保存改动不参与，避免把 Key 送到
    一个还没保存的地址。
    """
    adaptive = _get_adaptive()
    try:
        return adaptive.list_ai_slot_models_fn(slot)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取模型列表失败：{type(exc).__name__}") from exc


@router.post("/notification/test")
def test_notification(kind: str = Query("daily"), confirmed: bool = Query(False)):
    """发送一条真实测试推送；因为会真的发到微信，必须显式确认。"""
    _require_confirmation(confirmed, "发送测试推送")
    P.init_db()
    with PST.paper_db() as conn:
        settings = RSET.read(conn)["notification"]
        try:
            result = NOTIFY.dispatch(conn, kind, settings)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not result["sent"]:
        raise HTTPException(status_code=422, detail=result["reason"])
    return result


@router.get("/audit")
def settings_audit(limit: int = Query(50, ge=1, le=200)):
    P.init_db()
    with PST.paper_db() as conn:
        return {"items": RSET.audit(conn, limit)}


# --------------------------------------------------------------------------- #
# AI 能力凭据签发（方案 §5.2）：没有签发就没有自动采用
# --------------------------------------------------------------------------- #

_CAPABILITY_FIELDS = (
    "capability_id", "mode", "strategy_ids", "allowed_target_kinds", "allowed_fields",
    "bounds_version", "evidence_policy_id", "max_change_per_apply", "max_change_per_day",
    "loss_stop_policy_id", "max_trials_per_day", "daily_cost_cap",
    "max_tool_steps_per_task", "expires_at", "revocation_epoch", "issued_by",
)


def _read_capabilities():
    import ai_capability as CAP
    import ai_capability_store as STORE

    def _work():
        with PST.paper_db() as conn:
            return STORE.list_capabilities(conn), CAP.describe_capability(None)

    return _work()


@router.get("/ai-review/capabilities")
def ai_capabilities_snapshot():
    """列出已签发的授权凭据（不含任何密钥）与"当前是否在自动采用"。"""
    try:
        rows, current = _read_capabilities()
    except Exception as exc:  # noqa: BLE001 - 读不到就明说，不假装没有凭据
        raise HTTPException(status_code=500,
                            detail=f"读取授权凭据失败：{type(exc).__name__}") from exc
    return {"capabilities": rows, "current": current}


@router.post("/ai-review/capabilities")
async def issue_ai_capability(request: Request, confirmed: bool = Query(False)):
    """签发一份有界授权。这是**运维行为**：需确认，且不提供任何自动签发路径。"""
    _require_confirmation(confirmed, "签发AI授权凭据")
    body = await _read_json_object(request, "授权凭据必须是JSON对象")
    unknown = set(body) - set(_CAPABILITY_FIELDS)
    if unknown:
        raise HTTPException(status_code=422,
                            detail="未知凭据字段: " + ",".join(sorted(unknown)))
    missing = [name for name in _CAPABILITY_FIELDS if name not in body]
    if missing:
        raise HTTPException(status_code=422,
                            detail="凭据缺少必填字段: " + ",".join(missing))
    import ai_capability as CAP
    import ai_capability_store as STORE

    try:
        credential = CAP.CapabilityCredential(
            capability_id=str(body["capability_id"]),
            mode=str(body["mode"]),
            strategy_ids=tuple(body["strategy_ids"]),
            allowed_target_kinds=tuple(body["allowed_target_kinds"]),
            allowed_fields=tuple(body["allowed_fields"]),
            bounds_version=str(body["bounds_version"]),
            evidence_policy_id=str(body["evidence_policy_id"]),
            max_change_per_apply=body["max_change_per_apply"],
            max_change_per_day=body["max_change_per_day"],
            loss_stop_policy_id=str(body["loss_stop_policy_id"]),
            max_trials_per_day=body["max_trials_per_day"],
            daily_cost_cap=body["daily_cost_cap"],
            max_tool_steps_per_task=body["max_tool_steps_per_task"],
            expires_at=str(body["expires_at"]),
            revocation_epoch=body["revocation_epoch"],
            issued_by=str(body["issued_by"]))
    except (CAP.AdoptionRefused, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"凭据不合法：{exc}") from exc

    def _work():
        with PST.paper_db(immediate=True) as conn:
            issued = STORE.issue(conn, credential, operator=credential.issued_by)
            return CAP.describe_capability(issued)

    try:
        return await run_in_threadpool(_work)
    except STORE.CapabilityStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/ai-review/capabilities/{capability_id}/revoke")
async def revoke_ai_capability(capability_id: str, request: Request,
                               confirmed: bool = Query(False)):
    """撤销：递增撤销代号，持旧代号的请求立即失效。"""
    _require_confirmation(confirmed, "撤销AI授权凭据")
    body = await _read_json_object(request, "撤销请求必须是JSON对象")
    operator = str(body.get("operator") or "").strip()
    if not operator:
        raise HTTPException(status_code=422, detail="撤销必须填写操作人")
    reason = str(body.get("reason") or "")[:200]
    import ai_capability as CAP
    import ai_capability_store as STORE

    def _work():
        with PST.paper_db(immediate=True) as conn:
            revoked = STORE.revoke(conn, capability_id, operator=operator, reason=reason)
            return CAP.describe_capability(revoked)

    try:
        return await run_in_threadpool(_work)
    except STORE.CapabilityStoreError as exc:
        status = 404 if str(exc) == STORE.R_CAPABILITY_UNKNOWN else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
