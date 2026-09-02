# -*- coding: utf-8 -*-
"""HTTP boundary for the auditable self-evolution subsystem."""
import json
import os
import tempfile
import threading
import time

from fastapi import APIRouter, HTTPException, Query

import adaptive_engine as adaptive
import adaptive_learning_dispatch as learning_dispatch


router = APIRouter(prefix="/api/adaptive", tags=["adaptive-learning"])

# ─── 轻量内存缓存 ───
# One cache implementation is enough for the read-only status endpoints.
# Keeping timestamp and value maps separate preserves the existing O(1) access
# path without maintaining a dead second namespace.
_cache = {}
_cache_ts = {}

def _cache_get(key, ttl=30):
    import time as _t
    if key in _cache and _t.time() - _cache_ts.get(key, 0) < ttl:
        return _cache[key]
    return None

def _cache_set(key, value):
    import time as _t
    _cache[key] = value
    _cache_ts[key] = _t.time()

def _cache_clear(prefix=None):
    if prefix is None:
        _cache.clear()
        _cache_ts.clear()
    else:
        for k in list(_cache.keys()):
            if k.startswith(prefix):
                del _cache[k]
                del _cache_ts[k]


def _quote_metadata(rows):
    """Return auditable freshness/source/coverage metadata for a quote pull."""
    rows = rows if isinstance(rows, list) else []
    codes = {str(row.get("code") or "") for row in rows
             if isinstance(row, dict) and str(row.get("code") or "").strip()}
    quote_times = [str(row.get("quote_at") or row.get("quote_ts") or "")
                   for row in rows if isinstance(row, dict)
                   and (row.get("quote_at") or row.get("quote_ts"))]
    sources = sorted({str(row.get("source") or row.get("source_name") or "")
                      for row in rows if isinstance(row, dict)
                      and (row.get("source") or row.get("source_name"))})
    # The snapshot endpoint does not expose a trusted universe denominator;
    # report measured coverage explicitly instead of inventing a percentage.
    return {
        "quote_asof": max(quote_times) if quote_times else None,
        "source": sources[0] if len(sources) == 1 else ("mixed" if sources else "unknown"),
        "sources": sources,
        "coverage": {"rows": len(rows), "unique_codes": len(codes), "denominator": None},
    }


def _fetch_rebalance_quotes():
    """Fetch quotes before opening the SQLite connection used by a scan."""
    from data_fetcher import fetch_market_snapshot
    rows = fetch_market_snapshot(pages=20, allow_disk_fallback=False)
    metadata = _quote_metadata(rows)
    if metadata["source"] == "unknown" and rows:
        # The adapter currently normalizes rows without a per-row provider
        # field.  Keep the source explicit at the boundary rather than making
        # downstream audit readers infer it from a missing value.
        metadata["source"] = "fetch_market_snapshot"
    if not rows:
        raise HTTPException(status_code=503, detail={
            "status": "quote_unavailable", "quote_meta": metadata,
        })
    return {str(row.get("code")): row for row in rows if isinstance(row, dict) and row.get("code")}, metadata

_MANUAL_ACTOR = "human-ui"
_OVERVIEW_TTL_SECONDS = 60.0
_OVERVIEW_SNAPSHOT_PATH = os.path.join(
    getattr(adaptive, "CACHE_DIR", os.path.join(os.path.dirname(__file__), "data_cache")),
    "adaptive_overview_ui_snapshot.json",
)


def _is_complete_overview(data):
    """Only serve a persisted view if it can render every adaptive section."""
    return isinstance(data, dict) and all(
        isinstance(data.get(key), dict)
        for key in ("engine", "risk_optimizer", "selection_optimizer", "deepseek_advisor", "neural_control")
    )


def _load_overview_snapshot():
    try:
        with open(_OVERVIEW_SNAPSHOT_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if _is_complete_overview(data) else None
    except (OSError, ValueError, TypeError):
        return None


def _store_overview_snapshot(data):
    if not _is_complete_overview(data):
        return
    directory = os.path.dirname(_OVERVIEW_SNAPSHOT_PATH)
    try:
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="adaptive-overview-", suffix=".json", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, _OVERVIEW_SNAPSHOT_PATH)
    except OSError:
        try:
            if "temporary" in locals() and os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass


_persisted_overview = _load_overview_snapshot()
_overview_cache = {"data": _persisted_overview, "ts": 0.0, "running": False, "error": None}
_overview_lock = threading.Lock()


def _schedule_overview_refresh() -> None:
    """Build the costly read model off the request path."""
    with _overview_lock:
        if _overview_cache["running"]:
            return
        _overview_cache.update(running=True, error=None)

    def worker() -> None:
        try:
            # The engine keeps its own coherent short cache.  Forcing a full
            # SQLite/attribution rebuild here defeated the non-blocking API
            # cache and made ordinary tab switches contend with research work.
            data = adaptive.overview(force=False)
            _store_overview_snapshot(data)
            with _overview_lock:
                _overview_cache.update(data=data, ts=time.time(), running=False)
        except Exception as exc:
            with _overview_lock:
                _overview_cache.update(running=False, error=f"{type(exc).__name__}: {exc}")

    threading.Thread(target=worker, name="adaptive-overview-refresh", daemon=True).start()


def _require_confirmation(confirmed: bool, action: str):
    """Keep browser-originated state changes explicitly human-confirmed.

    There is intentionally no shared secret in this single-operator paper
    system.  The API boundary still must not let an accidental click or a
    stale UI request mutate the learning ledger without the same confirmation
    affordance used for apply and rollback.
    """
    if not confirmed:
        raise HTTPException(status_code=409, detail=f"请先在页面确认{action}")


@router.get("/overview")
def overview():
    now = time.time()
    with _overview_lock:
        data = _overview_cache["data"]
        fresh = data is not None and now - _overview_cache["ts"] < _OVERVIEW_TTL_SECONDS
        running = bool(_overview_cache["running"])
        error = _overview_cache["error"]
    if fresh:
        return data
    _schedule_overview_refresh()
    if data is not None:
        # A stale coherent snapshot is much more useful than a blocked page.
        return {**data, "snapshot_stale": True, "refreshing": True}
    return {
        "refreshing": True,
        "snapshot_stale": True,
        "message": "正在后台汇总自进化证据，页面不会阻塞。",
        "refresh_error": error,
    }


@router.get("/ai/settings")
def ai_settings():
    """Return safe AI controls, explanations, and provider capabilities."""
    return adaptive.ai_settings_schema()


@router.post("/ai/settings")
def update_ai_settings(
    llm_advisor_enabled: bool | None = Query(None),
    llm_provider: str | None = Query(None),
    llm_realtime_tuning_enabled: bool | None = Query(None),
    llm_realtime_auto_apply: bool | None = Query(None),
    llm_realtime_require_cross_source: bool | None = Query(None),
    llm_realtime_min_interval_minutes: int | None = Query(None, ge=5, le=240),
    llm_realtime_min_valid_rows: int | None = Query(None, ge=100, le=10000),
    llm_realtime_mode: str | None = Query(None),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "保存AI调参设置")
    updates = {k: v for k, v in {
        "llm_advisor_enabled": llm_advisor_enabled,
        "llm_provider": llm_provider,
        "llm_realtime_tuning_enabled": llm_realtime_tuning_enabled,
        "llm_realtime_auto_apply": llm_realtime_auto_apply,
        "llm_realtime_require_cross_source": llm_realtime_require_cross_source,
        "llm_realtime_min_interval_minutes": llm_realtime_min_interval_minutes,
        "llm_realtime_min_valid_rows": llm_realtime_min_valid_rows,
        "llm_realtime_mode": llm_realtime_mode,
    }.items() if v is not None}
    try:
        return {"settings": adaptive.update_ai_settings(updates)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/ai/overview")
def ai_overview():
    """Fast AI tab payload; full evidence aggregation always runs in background."""
    with _overview_lock:
        data = _overview_cache.get("data")
        refreshing = bool(_overview_cache.get("running"))
    if data is None:
        _schedule_overview_refresh()
        data, refreshing = {}, True
    advisor = data.get("deepseek_advisor") or {}
    tuning = advisor.get("realtime_tuning") or {}
    candidates = (data.get("selection_optimizer") or {}).get("candidates") or []
    ai_candidates = [item for item in candidates if item.get("tier") == "ai_realtime" or "AI" in str(item.get("reason") or "").upper()]
    return {"settings": adaptive.ai_settings(), "parameters": adaptive.AI_SETTINGS_META,
            "providers": __import__("deepseek_advisor").provider_catalog(), "advisor": advisor,
            "realtime_tuning": tuning, "latest_runs": [tuning.get("latest")] if tuning.get("latest") else [],
            "candidates": ai_candidates[:30], "snapshot_stale": not bool(data), "refreshing": refreshing}

@router.get("/trade-attributions")
def trade_attributions(
    limit: int = Query(160, ge=20, le=500),
    account_id: str | None = Query(None, max_length=40),
    trade_date: str | None = Query(None, min_length=10, max_length=10),
):
    """逐笔收盘归因：个股走势、大盘/板块贡献、公告事件和 AI 摘要。"""
    return adaptive.trade_attributions(limit=limit, account_id=account_id, trade_date=trade_date)


@router.post("/run")
def run(
    trigger: str = Query("manual", max_length=40),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "运行模拟盘学习")
    accepted, state = learning_dispatch.enqueue(trigger)
    return {
        "status": "accepted" if accepted else "busy",
        "message": "模拟盘学习已转入后台运行" if accepted else "已有模拟盘学习任务运行中",
        "run": state,
    }


@router.get("/run/status")
def run_status():
    # Status polling must stay O(1); refreshing the overview here made every
    # five-second browser poll compete with the learning worker for memory.
    return learning_dispatch.read_status()


@router.post("/advisor/run")
def run_advisor(
    trigger: str = Query("manual-ui", max_length=40),
    purpose: str = Query("data_quality", max_length=40),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "运行数据质量审阅")
    try:
        return adaptive.run_advisor_review(trigger=trigger, purpose=purpose)
    except RuntimeError as exc:
        error = str(exc)
        messages = {
            "advisor_disabled": "DeepSeek 数据质量审阅尚未启用",
            "api_key_missing": "DeepSeek API 密钥尚未配置",
        }
        raise HTTPException(status_code=409, detail=messages.get(error, "DeepSeek 审阅暂不可用")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="不支持的 DeepSeek 研究任务") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DeepSeek 审阅失败：{type(exc).__name__}") from exc


@router.post("/advisor/suite")
def run_advisor_suite(
    trigger: str = Query("manual-suite", max_length=40),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "运行研究套件")
    try:
        return adaptive.run_advisor_suite(trigger=trigger)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail="DeepSeek 研究套件尚未启用或密钥不可用") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DeepSeek 研究套件运行失败：{type(exc).__name__}") from exc


@router.post("/ai/tune")
def run_ai_tuning(
    trigger: str = Query("manual-ai-tuning", max_length=80),
    mode: str = Query("intraday", max_length=30),
    confirmed: bool = Query(False),
):
    """Run the bounded DeepSeek tuner for the three paper accounts only."""
    if mode not in {"intraday", "close", "shadow"}:
        raise HTTPException(status_code=422, detail="调参模式只支持 intraday、close 或 shadow")
    _require_confirmation(confirmed, "运行 AI 有界调参")
    try:
        return adaptive.run_ai_tuning(trigger=trigger, mode=mode)
    except RuntimeError as exc:
        error = str(exc)
        messages = {
            "advisor_disabled": "DeepSeek 调参尚未启用",
            "api_key_missing": "DeepSeek API 密钥尚未配置",
        }
        raise HTTPException(status_code=409, detail=messages.get(error, "DeepSeek 调参暂不可用")) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DeepSeek 调参失败：{type(exc).__name__}") from exc


@router.post("/news/run")
def run_news_learning(
    trigger: str = Query("manual-ui", max_length=40),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "运行新闻学习")
    try:
        return adaptive.run_news_learning(trigger=trigger)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"新闻学习运行失败：{type(exc).__name__}: {exc}") from exc


@router.post("/feedback")
def feedback(
    decision_id: int = Query(..., gt=0),
    account_id: str = Query(...),
    verdict: str = Query(...),
    note: str = Query("", max_length=500),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "写入人工反馈")
    try:
        return adaptive.record_feedback(decision_id, account_id, verdict, note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/risk/apply")
def apply_risk_candidate(
    candidate_id: int = Query(..., gt=0),
    approved_by: str = Query("human", max_length=80),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "批准风控版本")
    try:
        # Keep the legacy query parameter for client compatibility, but never
        # trust it as the audit actor.
        return adaptive.apply_risk_candidate(candidate_id, _MANUAL_ACTOR)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/risk/rollback")
def rollback_risk(
    account_id: str = Query(...),
    reason: str = Query("人工回滚", max_length=300),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "回滚风控版本")
    try:
        return adaptive.rollback_risk(account_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/selection/rollback")
def rollback_selection(
    account_id: str = Query(...),
    reason: str = Query("人工回滚", max_length=300),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "回滚选股版本")
    try:
        return adaptive.rollback_selection(account_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/selection/apply")
def apply_selection(
    candidate_id: int = Query(..., gt=0),
    approved_by: str = Query("human", min_length=1, max_length=80),
    confirmed: bool = Query(False),
):
    """人工确认结构化选股进化候选；参数级候选仍可由周期自动应用。"""
    _require_confirmation(confirmed, "批准选股版本")
    try:
        # Keep the legacy query parameter for client compatibility, but never
        # trust it as the audit actor.
        return adaptive.apply_selection(candidate_id, _MANUAL_ACTOR)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/neural/approve")
def approve_neural_network(
    confirmed: bool = Query(False),
):
    """人工确认神经网络进入有界影子排序；不允许绕过任何交易硬门禁。"""
    _require_confirmation(confirmed, "批准神经网络影子评分")
    try:
        return adaptive.approve_neural_network(confirmed=True, approved_by=_MANUAL_ACTOR)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/rebalance/rollback")
def rollback_rebalance(
    account_id: str = Query(...),
    reason: str = Query("人工回滚调仓版本", max_length=300),
    confirmed: bool = Query(False),
):
    """Semantic alias used by the paper-rebalance workspace."""
    _require_confirmation(confirmed, "回滚调仓版本")
    try:
        return adaptive.rollback_selection(account_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

# ─── 调仓扫描路由 ───

@router.get("/rebalance/status")
def rebalance_status():
    cached = _cache_get("rebalance_status", ttl=30)
    if cached is not None:
        return cached
    try:
        import rebalance_scanner
        with adaptive._connect() as conn:
            rebalance_scanner.ensure_schema(conn)
            result = rebalance_scanner.get_rebalance_status(conn)
            _cache_set("rebalance_status", result)
            return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"调仓状态失败：{type(exc).__name__}") from exc


@router.post("/rebalance/scan")
def run_rebalance_scan(confirmed: bool = Query(False)):
    _require_confirmation(confirmed, "运行调仓扫描")
    try:
        import rebalance_scanner
        # The network call must complete before opening the SQLite connection;
        # a slow quote source must never hold a write/read transaction open.
        quotes, quote_meta = _fetch_rebalance_quotes()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail={
            "status": "quote_unavailable", "error": type(exc).__name__,
        }) from exc
    try:
        with adaptive._connect() as conn:
            rebalance_scanner.ensure_schema(conn)
            accounts = []
            for acc in conn.execute("SELECT * FROM paper_accounts WHERE status='running'").fetchall():
                acc_dict = dict(acc)
                acc_dict["positions"] = [dict(p) for p in conn.execute("SELECT * FROM paper_positions WHERE account_id=? AND qty>0", (acc["id"],)).fetchall()]
                accounts.append(acc_dict)
            result = rebalance_scanner.daily_close_scan(conn, accounts, quotes)
            if isinstance(result, dict):
                result["quote_meta"] = quote_meta
            return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"调仓扫描失败：{type(exc).__name__}: {str(exc)[:200]}") from exc


@router.post("/rebalance/verify")
def verify_rebalance_plans(confirmed: bool = Query(False)):
    _require_confirmation(confirmed, "验证调仓计划")
    try:
        import rebalance_scanner
        with adaptive._connect() as conn:
            rebalance_scanner.ensure_schema(conn)
            plans = rebalance_scanner.get_pending_plans(conn)
        if not plans:
            return {"message": "没有待验证的调仓计划", "plans": []}
        # Fetch outside the connection scope; verification only writes after a
        # complete, auditable quote snapshot is available.
        quotes, quote_meta = _fetch_rebalance_quotes()
        with adaptive._connect() as conn:
            rebalance_scanner.ensure_schema(conn)
            results = rebalance_scanner.verify_all_plans(conn, plans, quotes)
            return {"plans": results, "quote_meta": quote_meta}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"验证失败：{type(exc).__name__}") from exc


@router.get("/rebalance/plans")
def get_rebalance_plans(status: str = Query("all")):
    try:
        import rebalance_scanner
        with adaptive._connect() as conn:
            rebalance_scanner.ensure_schema(conn)
            if status == "all":
                rows = conn.execute("SELECT * FROM rebalance_plans ORDER BY id DESC LIMIT 50").fetchall()
            else:
                rows = conn.execute("SELECT * FROM rebalance_plans WHERE status=? ORDER BY id DESC LIMIT 50", (status,)).fetchall()
            return {"plans": [dict(r) for r in rows]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取计划失败：{type(exc).__name__}") from exc

# ─── 双AI调参路由 ───

@router.get("/dual-ai/status")
def dual_ai_status():
    cached = _cache_get("dual_ai_status", ttl=30)
    if cached is not None:
        return cached
    try:
        result = adaptive.dual_ai_status_fn()
        _cache_set("dual_ai_status", result)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"双AI状态失败：{type(exc).__name__}") from exc


@router.get("/dual-ai/keys")
def dual_ai_api_keys():
    try:
        return {"keys": adaptive.get_dual_ai_api_keys_fn()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取API Key失败：{type(exc).__name__}") from exc


@router.post("/dual-ai/keys")
def update_dual_ai_api_key(
    provider: str = Query(..., max_length=20),
    api_key: str = Query(None, max_length=200),
    base_url: str = Query(None, max_length=500),
    model: str = Query(None, max_length=100),
    enabled: bool = Query(None),
    confirmed: bool = Query(False),
):
    if provider not in ("mimo", "deepseek"):
        raise HTTPException(status_code=422, detail="provider必须是 mimo 或 deepseek")
    _require_confirmation(confirmed, f"保存 {provider} API配置")
    try:
        return {"keys": adaptive.update_dual_ai_api_key_fn(provider, api_key=api_key, base_url=base_url, model=model, enabled=enabled)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/dual-ai/tune")
def run_dual_ai_tuning(
    trigger: str = Query("manual-dual-ai", max_length=80),
    mode: str = Query("intraday", max_length=30),
    confirmed: bool = Query(False),
):
    if mode not in {"intraday", "close", "shadow"}:
        raise HTTPException(status_code=422, detail="调参模式只支持 intraday、close 或 shadow")
    _require_confirmation(confirmed, "运行双AI共识调参")
    try:
        return adaptive.run_dual_ai_tuning_fn(trigger=trigger, mode=mode)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"双AI调参失败：{type(exc).__name__}: {str(exc)[:200]}") from exc


@router.get("/dual-ai/runs")
def dual_ai_runs(limit: int = Query(20, ge=1, le=100)):
    try:
        import dual_ai_tuner
        with adaptive._connect() as conn:
            dual_ai_tuner.ensure_schema(conn)
            return {"runs": dual_ai_tuner.recent_runs(conn, limit)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取调参记录失败：{type(exc).__name__}") from exc


# ─── 自进化路由 ───

@router.get("/evolution/status")
def evolution_status():
    cached = _cache_get("evolution_status", ttl=30)
    if cached is not None:
        return cached
    try:
        result = adaptive.evolution_status_fn()
        _cache_set("evolution_status", result)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"自进化状态失败：{type(exc).__name__}") from exc


@router.get("/evolution/params")
def evolution_params():
    try:
        return adaptive.get_evolution_params_fn()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取进化参数失败：{type(exc).__name__}") from exc


@router.post("/evolution/evolve")
def trigger_evolution(confirmed: bool = Query(False)):
    _require_confirmation(confirmed, "执行自进化")
    try:
        return adaptive.trigger_evolution_fn()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"执行进化失败：{type(exc).__name__}") from exc


@router.get("/evolution/metrics")
def evolution_metrics(window: int = Query(20, ge=5, le=100)):
    try:
        return adaptive.evolution_metrics_fn(window)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取性能指标失败：{type(exc).__name__}") from exc


@router.get("/evolution/log")
def evolution_log(limit: int = Query(50, ge=1, le=200)):
    try:
        return {"log": adaptive.evolution_log_fn(limit)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取进化日志失败：{type(exc).__name__}") from exc


# ─── modlens 路由 ───

@router.get("/modlens/status")
def modlens_status():
    try:
        return adaptive.modlens_status_fn()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"modlens状态失败：{type(exc).__name__}") from exc


@router.post("/modlens/read-image")
def modlens_read_image(
    path: str = Query(..., max_length=1000),
    prompt: str = Query(None, max_length=500),
):
    try:
        return adaptive.modlens_read_image_fn(path, prompt)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"图片读取失败：{type(exc).__name__}") from exc
