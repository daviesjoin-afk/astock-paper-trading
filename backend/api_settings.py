"""Unified operator settings API.

The endpoint intentionally separates public runtime controls from AI secrets:
GET responses contain only masked key status and POST bodies are never echoed.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Query, Request

import adaptive_engine as adaptive
import paper_sizing as sizing
import paper_trading as P
import runtime_settings as RSET


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
    P.init_db()
    with P._db() as conn:  # the paper module owns the storage boundary
        settings = RSET.read(conn)
        cycle = P._active_cycle(conn)
        audit = RSET.audit(conn, 40)
        capital = float(cycle.get("capital") or settings["simulation"]["default_starting_capital"])
        enabled = cycle.get("enabled_strategies")
        if isinstance(enabled, str):
            enabled = RSET._decode(enabled, None)
        enabled = list(enabled) if isinstance(enabled, (list, tuple)) else list(RSET.STRATEGIES)
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
    # adaptive_engine masks keys before returning them; no raw credential is
    # ever copied into the unified snapshot.
    try:
        ai_schema = adaptive.ai_settings_schema()
        ai_keys = adaptive.get_dual_ai_api_keys_fn()
    except Exception as exc:
        ai_schema = {"settings": {}, "parameters": {}, "providers": {}}
        ai_keys = {}
        ai_error = f"{type(exc).__name__}: {exc}"
    else:
        ai_error = None
    return {
        "schema_version": "settings-v1",
        "settings": settings,
        "defaults": {
            "simulation": {key: RSET.defaults()[key] for key in RSET.SETTING_GROUPS["simulation"]},
            "risk": {key: RSET.defaults()[key] for key in RSET.SETTING_GROUPS["risk"]},
            "strategy": {"strategy_overrides": RSET.defaults()["strategy_overrides"]},
            "evolution": {"evolution_interval_hours": RSET.defaults()["evolution_interval_hours"]},
        },
        "metadata": RSET.metadata(),
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


@router.post("/")
async def update_settings(request: Request, confirmed: bool = Query(False)):
    _require_confirmation(confirmed, "保存运行设置")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="设置内容必须是JSON对象") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="设置内容必须是JSON对象")
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
        P.init_db()
        with P._db(immediate=True) as conn:
            if runtime_updates:
                RSET.update(conn, runtime_updates, actor="human-ui")
                P._audit(conn, None, "settings_changed", "统一设置中心更新运行参数（详见设置审计）")
        if ai_updates:
            adaptive.update_ai_settings(ai_updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存设置失败：{type(exc).__name__}") from exc
    return _snapshot()


@router.post("/ai-key")
async def update_ai_key(request: Request, confirmed: bool = Query(False)):
    _require_confirmation(confirmed, "保存AI接口配置")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="AI接口配置必须是JSON对象") from exc
    if not isinstance(body, dict) or body.get("provider") not in {"mimo", "deepseek"}:
        raise HTTPException(status_code=422, detail="provider必须是 mimo 或 deepseek")
    api_key = body.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 200):
        raise HTTPException(status_code=422, detail="API Key格式无效")
    try:
        adaptive.update_dual_ai_api_key_fn(
            body["provider"], api_key=api_key,
            base_url=body.get("base_url"), model=body.get("model"), enabled=body.get("enabled"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _snapshot()


@router.get("/audit")
def settings_audit(limit: int = Query(50, ge=1, le=200)):
    P.init_db()
    with P._db() as conn:
        return {"items": RSET.audit(conn, limit)}
