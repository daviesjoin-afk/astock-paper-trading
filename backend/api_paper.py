# -*- coding: utf-8 -*-
"""Paper-trading HTTP boundary."""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

import paper_trading as P


router = APIRouter(prefix="/api/paper", tags=["paper-trading"])

# ─── 内存缓存 ───
_cache_lock = threading.RLock()
_cache = {}
_cache_ts = {}
_cache_generation = {}
_cache_inflight = {}
MAX_CACHE_ENTRIES = 128


def _current_cache_generation():
    """Return the ledger generation used to invalidate read-through caches.

    A TTL alone made the UI show an old order/risk view after a successful
    scan.  The paper ledger owns the mutation sequence, so cache validity is
    tied to that generation as well as the short latency TTL.  Failure to read
    the generation is deliberately fail-closed for cache reuse.
    """
    try:
        return P.paper_cache_generation()
    except Exception:
        return None

_CACHE_MISS = object()


def _cget(key, ttl=30, *, generation=_CACHE_MISS):
    """Read a cache entry without re-reading the ledger generation.

    ``generation`` is optional for compatibility with existing callers, but
    the request helpers below pass one value through both the read and write
    sides.  This prevents a browser request from doing two identical SQLite
    generation probes.
    """
    if generation is _CACHE_MISS:
        generation = _current_cache_generation()
    with _cache_lock:
        if (
            generation is not None
            and key in _cache
            and _cache_generation.get(key) == generation
            and time.time() - _cache_ts.get(key, 0) < ttl
        ):
            return _cache[key]
    return None


def _cset(key, val, *, generation=_CACHE_MISS):
    if generation is _CACHE_MISS:
        generation = _current_cache_generation()
    with _cache_lock:
        if key not in _cache and len(_cache) >= MAX_CACHE_ENTRIES:
            oldest_key = min(_cache, key=lambda item: _cache_ts.get(item, 0))
            _cache.pop(oldest_key, None)
            _cache_ts.pop(oldest_key, None)
            _cache_generation.pop(oldest_key, None)
        _cache[key] = val
        _cache_ts[key] = time.time()
        _cache_generation[key] = generation


def _cache_load(key, loader, *, ttl=30):
    """Single-flight read-through cache for expensive paper read models.

    Only the first concurrent request runs ``loader``.  Waiters receive the
    same result (or the same exception) and never start a second dashboard or
    risk query.  One ledger-generation lookup is performed per caller that
    reaches this helper; the owner reuses it when publishing the result.
    """
    generation = _current_cache_generation()
    cached = _cget(key, ttl=ttl, generation=generation)
    if cached is not None:
        return cached
    with _cache_lock:
        flight = _cache_inflight.get(key)
        if flight is None:
            flight = {"event": threading.Event(), "result": _CACHE_MISS, "error": None}
            _cache_inflight[key] = flight
            owner = True
        else:
            owner = False
    if not owner:
        # A slow provider/database read should not leave a waiter blocked
        # forever.  The owner publishes the exception before signalling.
        flight["event"].wait(timeout=180)
        if flight.get("error") is not None:
            raise flight["error"]
        if flight.get("result") is not _CACHE_MISS:
            return flight["result"]
        # The owner timed out or disappeared; retry once as a new owner.
        return _cache_load(key, loader, ttl=ttl)
    try:
        result = loader()
        _cset(key, result, generation=generation)
        flight["result"] = result
        return result
    except Exception as exc:
        flight["error"] = exc
        raise
    finally:
        with _cache_lock:
            _cache_inflight.pop(key, None)
            flight["event"].set()

def _cclear(prefix=None):
    with _cache_lock:
        if prefix is None:
            _cache.clear()
            _cache_ts.clear()
            _cache_generation.clear()
        else:
            for k in list(_cache.keys()):
                if k.startswith(prefix):
                    del _cache[k]
                    del _cache_ts[k]
                    _cache_generation.pop(k, None)


def _call_with_retry(fn, *args, retries=3, delay=2, **kwargs):
    for i in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if "database is locked" in str(e) and i < retries - 1:
                time.sleep(delay)
                continue
            raise


def risk_refresh_status() -> dict:
    try:
        status = P.risk_snapshot_refresh_status()
        # Keep the historical nested field for older frontends, but expose a
        # single backend-owned state machine as the source of truth.
        status["background"] = dict(status)
        return status
    except Exception:
        return {"running": False, "last_error": "status_unavailable", "background": {}}


@router.get("/overview")
def overview(
    refresh: bool = Query(False, description="Force one fresh dashboard read for an explicit user refresh"),
    activity: bool = Query(False, description="Include cross-cycle order activity for the activity workspace"),
    history_symbols: bool = Query(False, description="Include the history-symbol quick selector"),
):
    try:
        cache_key = f"overview:{int(activity)}:{int(history_symbols)}"
        loader = lambda: _call_with_retry(
            P.dashboard, include_activity=activity, include_history_symbols=history_symbols,
        )
        # Normal navigation remains a short-TTL, generation-aware read model.
        # The visible manual refresh button can request one fresh ledger read
        # without forcing every browser navigation to rebuild the dashboard.
        # Activity/history payloads are ledger-generation aware already.  A
        # forced rebuild there would re-decode archives even when no order has
        # changed, so manual refresh reuses their current-generation cache.
        # Portfolio marks, by contrast, intentionally honour the explicit
        # fresh-read request.
        if refresh and not (activity or history_symbols):
            result = loader()
            _cset(cache_key, result)
        else:
            result = _cache_load(cache_key, loader, ttl=30)
        # The overview contains a large but already JSON-native audit view.
        # Returning a Response avoids FastAPI recursively re-walking every
        # archived order on every browser refresh.
        return JSONResponse(content=result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Dashboard failed: {type(exc).__name__}: {str(exc)[:200]}") from exc


@router.get("/ignition-shadow")
def ignition_shadow(
    limit: int = Query(200, ge=10, le=1000),
):
    """主力点火影子对比：原规则 vs 点火规则命中后 30/60 分钟表现。"""
    try:
        return P.ignition_shadow_report(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ignition shadow failed: {type(exc).__name__}: {str(exc)[:200]}") from exc


@router.get("/risk-overview")
def risk_overview():
    try:
        # Reading the risk page must not synchronously fetch a full-market
        # snapshot when the last persisted snapshot is a few minutes old.
        # That belongs to the explicit background ``/risk-refresh`` action;
        # returning the marked stale snapshot keeps the dashboard responsive
        # and leaves CPU/RAM for the risk runner itself.  ``allow_network``
        # also forbids a first-load cold fetch inside this request thread —
        # the background refresh is kicked instead and the page fills in on
        # the next poll.
        result = _cache_load(
            "risk_overview",
            lambda: _call_with_retry(P.risk_dashboard, allow_stale=True, allow_network=False),
            ttl=60,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Risk overview failed: {type(exc).__name__}: {str(exc)[:200]}") from exc


@router.get("/risk-audit")
def risk_audit(
    limit: int = Query(160, ge=20, le=300),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=200),
):
    cache_key = f"risk_audit:{limit}:{page}:{page_size}"
    try:
        def load_audit():
            full = _call_with_retry(P.risk_audit, limit)
            if isinstance(full, list) and len(full) > page_size:
                total = len(full)
                start = (page - 1) * page_size
                end = start + page_size
                return {
                    "items": full[start:end],
                    "pagination": {"page": page, "page_size": page_size, "total": total,
                                   "total_pages": (total + page_size - 1) // page_size,
                                   "has_next": end < total, "has_prev": page > 1}
                }
            return full

        return _cache_load(cache_key, load_audit, ttl=15)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Risk audit failed: {type(exc).__name__}") from exc


@router.get("/strategy-center")
def strategy_center():
    try:
        return _cache_load("strategy_center", lambda: _call_with_retry(P.strategy_center), ttl=120)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Strategy center failed: {type(exc).__name__}") from exc


@router.get("/reviews")
def reviews():
    try:
        return _call_with_retry(P.latest_reviews)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Reviews failed: {type(exc).__name__}") from exc


@router.get("/research-validation")
def research_validation(
    limit: int = Query(90, ge=1, le=200),
):
    """策略证据：候选快照与后续兑现跟踪（前端 research 工作区）。

    H12 修复：此前该函数存在但从未注册路由，前端 /api/paper/research-validation
    返回 404 → “策略证据读取失败：Not Found”。
    """
    try:
        return _call_with_retry(P.research_validation_dashboard, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Research validation failed: {type(exc).__name__}: {str(exc)[:200]}") from exc


@router.post("/research-validation/backfill")
def research_validation_backfill():
    """收盘后手动补录当日研究快照（前端“补录当日收盘快照”按钮）。

    H12 修复：后端 manual_research_backfill 存在但从未注册路由，
    前端 /api/paper/research-validation/backfill 返回 404。
    """
    try:
        return _call_with_retry(P.manual_research_backfill)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Research backfill failed: {type(exc).__name__}: {str(exc)[:200]}") from exc


@router.get("/order-preview")
def order_preview_get(
    account_id: str = Query(...), code: str = Query(...), side: str = Query(...),
    qty: int = Query(0, ge=0), order_type: str = Query("market"), limit_price: float = Query(None),
):
    """下单预检（GET 别名，前端 api() 用 fetch(path) 即 GET）。

    H12 修复：前端调用 /api/paper/order-preview（GET），原实现仅注册了
    POST /order/preview，路径与方法双重不匹配 → 前端预检失败。
    """
    try:
        return _call_with_retry(P.preview_manual_order, account_id, code, side, qty, order_type, limit_price)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Preview failed: {type(exc).__name__}") from exc


@router.post("/order/preview")
def order_preview(
    account_id: str = Query(...), code: str = Query(...), side: str = Query(...),
    qty: int = Query(0, ge=0), order_type: str = Query("market"), limit_price: float = Query(None),
):
    try:
        return _call_with_retry(P.preview_manual_order, account_id, code, side, qty, order_type, limit_price)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Preview failed: {type(exc).__name__}") from exc


@router.post("/order/submit")
def order_submit(
    account_id: str = Query(...), code: str = Query(...), side: str = Query(...),
    qty: int = Query(0, ge=0), order_type: str = Query("market"), limit_price: float = Query(None),
    confirmed: bool = Query(False),
):
    if not confirmed:
        raise HTTPException(status_code=400, detail="Confirmation required")
    try:
        result = _call_with_retry(P.submit_manual_order, account_id, code, side, qty, order_type, limit_price)
        _cclear()
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Submit failed: {type(exc).__name__}") from exc


@router.post("/order/cancel")
def order_cancel(order_id: int = Query(..., gt=0)):
    try:
        result = _call_with_retry(P.cancel_manual_order, order_id)
        _cclear()
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cancel failed: {type(exc).__name__}") from exc


@router.post("/run-now")
def run_now(slot: str = Query("risk"), force: bool = False):
    try:
        result = _call_with_retry(P.run_slot, slot, force=force)
        # A successful manual run changes orders, risk decisions, jobs and
        # often the NAV projection.  Do not let the browser keep serving the
        # previous 30/60/120-second read model after the action completed.
        _cclear()
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Run failed: {type(exc).__name__}") from exc


@router.post("/configure")
def configure(capital: float = Query(..., ge=1000, le=10_000_000)):
    try:
        result = _call_with_retry(P.configure_capital, capital)
        _cclear()  # 资金变更必须立刻反映到 overview/风险页
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Configure failed: {type(exc).__name__}") from exc


@router.post("/start")
def start(capital: float | None = Query(None, ge=1000, le=10_000_000)):
    try:
        result = _call_with_retry(P.start_new_cycle, capital)
        _cclear()  # 新周期启动后立即刷新所有缓存视图
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Start failed: {type(exc).__name__}") from exc


@router.post("/pause")
def pause():
    try:
        result = _call_with_retry(P.set_accounts_status, "paused")
        _cclear()  # 修复：状态变更后必须清 overview 缓存，否则前端 loadPaper
        return result  # 仍读到 30s 旧缓存（显示 running）→ “暂停无反应”
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pause failed: {type(exc).__name__}") from exc


@router.post("/resume")
def resume():
    try:
        result = _call_with_retry(P.set_accounts_status, "running")
        _cclear()  # 同上：恢复后立即刷新前端状态
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Resume failed: {type(exc).__name__}") from exc


@router.post("/reset")
def reset(capital: float | None = Query(None, ge=1000, le=10_000_000)):
    try:
        # include_dashboard=False：重置后立即返回，避免抓行情超时；前端 loadPaper 再拉 overview
        result = _call_with_retry(P.reset_cycle, capital, False)
        _cclear()  # 重置归档后立即刷新前端
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Reset failed: {type(exc).__name__}") from exc


@router.post("/style")
def style(account_id: str = Query(...), style: str = Query(...)):
    try:
        result = _call_with_retry(P.set_account_style, account_id, style)
        _cclear()  # 风格切换立即反映到策略中心/overview
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Style failed: {type(exc).__name__}") from exc


@router.get("/stock-history")
def stock_history(code: str = Query(..., min_length=6, max_length=6), account_id: str = ""):
    try:
        return _call_with_retry(P.stock_trade_history, code, account_id or None)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"History failed: {type(exc).__name__}") from exc


@router.post("/risk-refresh")
def risk_refresh():
    try:
        # There is one backend-owned refresh worker.  The API no longer starts
        # a second thread/state machine that could race the scheduled runner.
        _cclear("risk_")
        return P.request_risk_snapshot_refresh(
            trigger="manual",
            on_complete=lambda: _cclear("risk_"),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Refresh failed: {type(exc).__name__}") from exc


@router.get("/cache/stats")
def cache_stats():
    with _cache_lock:
        return {"size": len(_cache), "keys": list(_cache.keys())[:20]}


@router.post("/cache/clear")
def clear_cache(prefix: str = Query(None)):
    _cclear(prefix)
    return {"cleared": True}
