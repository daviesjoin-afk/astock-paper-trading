# -*- coding: utf-8 -*-
"""A股智能选股系统 - FastAPI 后端"""
import os, sys, time, datetime, threading, json, uuid
from contextlib import asynccontextmanager, contextmanager
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import pandas as pd

try:
    import fcntl as _main_fcntl
except ImportError:  # pragma: no cover - Windows development only.
    _main_fcntl = None

import data_fetcher as dfc
import universe as U
import factors as F
import strategies as S
import backtest as B
import optimizer as O
import decision_engine as DE
import linkage as L
import paper_trading as P
import selection_tracking as ST
import metrics as MET
from api_paper import risk_refresh_status, router as paper_router
from api_adaptive import router as adaptive_router
from api_settings import router as settings_router
from resource_guard import heavy_job_lease

FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

_OVERVIEW_CACHE = {"data": None, "ts": 0.0}
_SELECT_RESULT_CACHE = {}
_SELECT_RESULT_LOCK = threading.RLock()
_SELECT_RESULT_TTL_SECONDS = 60
_SUPER_FLOW_PATH = os.path.join(dfc.CACHE_DIR, "super_flow_cache.json")
_SUPER_FLOW_CACHE = {"data": None, "ts": 0.0, "refreshing": False}
_SUPER_FLOW_LOCK = threading.Lock()
_SUPER_FLOW_TTL_SECONDS = 300
_SUPER_FLOW_FILE_LOCK_PATH = _SUPER_FLOW_PATH + ".lock"
_DATA_UPDATE_LOCK = threading.RLock()
_DATA_UPDATE_STATE = {
    "status": "idle",
    "job_id": None,
    "started_at": None,
    "finished_at": None,
    "target_date": None,
    "result": None,
    "error": None,
    "cancel_requested": False,
    "trigger": None,
}
_SELECTION_FACTORS_PATH = os.path.join(dfc.CACHE_DIR, "selection_factors.csv")
_SELECTION_META_PATH = os.path.join(dfc.CACHE_DIR, "selection_cache.json")
_SELECTION_RESULT_DIR = os.path.join(dfc.CACHE_DIR, "selection_results")
_SELECTION_CACHE = {
    "signature": None,
    "price_factors": None,
    "first_board_codes": None,
}
_SELECTION_REQUIRED_COLUMNS = {
    "three_up",
    "boll_mid_breakout",
    "above_ma5_5d",
    "above_ma10_5d",
    "above_boll_mid",
    "above_ma60",
    "above_all_ma",
    "weekly_oversold",
    "monthly_oversold",
}
_SELECTION_LOCK = threading.Lock()
_HEALTH_CACHE_TTL_SECONDS = 8.0
_HEALTH_CACHE_LOCK = threading.Lock()
_HEALTH_CACHE = {"key": None, "at": 0.0, "data": None}
_LIVE_CACHE = {
    "finance": {"data": {}, "report_dates": []},
    "sentiment": {},
    "gate": None,
    "sector_flow": [],
    "news_hits": [],
    "indices": [],
}


def _health_file_signature(path):
    try:
        stat = os.stat(path)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _health_cache_signature():
    """Key the read-only health cache to the files that affect its view."""
    return (
        _health_file_signature(getattr(U, "UNIVERSE_PATH", "")),
        _health_file_signature(dfc.KLINE_MANIFEST_PATH),
        _health_file_signature(dfc.KLINE_DIR),
        _health_file_signature(dfc.MARKET_SNAPSHOT_FULL_CACHE_PATH),
    )


def _invalidate_health_cache():
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE.update({"key": None, "at": 0.0, "data": None})


def _cached_health_response():
    key = _health_cache_signature()
    now = time.monotonic()
    with _HEALTH_CACHE_LOCK:
        payload = _HEALTH_CACHE.get("data")
        if (
            payload is not None
            and _HEALTH_CACHE.get("key") == key
            and now - float(_HEALTH_CACHE.get("at") or 0.0) < _HEALTH_CACHE_TTL_SECONDS
        ):
            # FastAPI serializes the returned object; copy the outer payload so
            # a caller cannot mutate the shared cache between requests.
            return dict(payload)
    return None


def _store_health_response(payload):
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE.update({"key": _health_cache_signature(), "at": time.monotonic(), "data": dict(payload)})


def _selection_signature():
    try:
        return (
            os.path.getmtime(dfc.KLINE_MANIFEST_PATH),
            os.path.getsize(dfc.KLINE_MANIFEST_PATH),
            dfc.SHARED_KLINE_SOURCE_VERSION,
        )
    except OSError:
        return (0, 0)


def _selection_result_path(strategy: str, topn: int) -> str:
    """Stable per-strategy result path so the UI can reopen the last run."""
    safe_strategy = "".join(ch for ch in str(strategy) if ch.isalnum() or ch in "_-") or "unknown"
    return os.path.join(_SELECTION_RESULT_DIR, f"{safe_strategy}_{int(topn)}.json")


def _save_selection_result(strategy: str, topn: int, result: dict) -> None:
    """Persist only completed selection results; failed/partial runs are never shown as latest."""
    if not isinstance(result, dict) or result.get("need_init") or result.get("error"):
        return
    try:
        os.makedirs(_SELECTION_RESULT_DIR, exist_ok=True)
        payload = {
            "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "strategy": strategy,
            "topn": int(topn),
            "data": result,
        }
        path = _selection_result_path(strategy, topn)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        # Persistence must never make a successful selection request fail.
        return


def _load_selection_result(strategy: str, topn: int):
    path = _selection_result_path(strategy, topn)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get("picks"):
            return None
        data = dict(data)
        data["latest_saved_at"] = payload.get("saved_at")
        data["latest_strategy"] = payload.get("strategy", strategy)
        data["latest_topn"] = payload.get("topn", topn)
        return data
    except (OSError, ValueError, TypeError):
        return None


def _selection_result_staleness(data: dict):
    """Check whether a persisted result is still the current trading-day view.

    ``/api/select/latest`` is intentionally read-only, but showing a week-old
    result as if it were today's selection is worse than showing a short
    refresh state.  A result is current only when its run day and live quote
    day match the current trading day (or the latest Friday on a weekend),
    and its factor reference is not older than the shared coverage cutoff.
    """
    today = datetime.date.today()
    run_day = U.latest_complete_trade_date(today)
    saved_day = str(data.get("latest_saved_at") or "")[:10]
    if saved_day != run_day.isoformat():
        return True, f"结果保存于 {saved_day or '未知日期'}，当前交易日为 {run_day.isoformat()}"
    quality = data.get("data_quality") or {}
    expected = str((U.coverage_report() or {}).get("expected_reference_date") or "")[:10]
    reference = str(quality.get("reference_date") or "")[:10]
    if expected and (not reference or reference < expected):
        return True, f"因子仅更新到 {reference or '未知'}，当前至少需要 {expected}"
    live_day = str(quality.get("live_quote_at") or "")[:10]
    if live_day and live_day != run_day.isoformat():
        return True, f"实时行情停留在 {live_day}，尚未覆盖 {run_day.isoformat()}"
    return False, None


def _complete_daily_cutoff():
    """Return the latest complete daily bar (never today's partial bar)."""
    # Keep the selector's upper bound identical to the shared refresh/coverage
    # calendar.  A weekday is not necessarily a trading day (holiday), and a
    # provider can legitimately return a bar newer than the last completed
    # session while the current session is still in progress.
    return U.latest_complete_trade_date(datetime.date.today())


def _refresh_super_flow(topn):
    try:
        rows = dfc.fetch_fund_flow_rank(topn=topn)
        if not rows:
            return
        payload = {"updated_at": time.time(), "data": rows}
        with _super_flow_file_lock():
            tmp_path = f"{_SUPER_FLOW_PATH}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp_path, _SUPER_FLOW_PATH)
        with _SUPER_FLOW_LOCK:
            _SUPER_FLOW_CACHE.update(data=rows, ts=payload["updated_at"])
        with _SELECT_RESULT_LOCK:
            _SELECT_RESULT_CACHE.clear()
    finally:
        with _SUPER_FLOW_LOCK:
            _SUPER_FLOW_CACHE["refreshing"] = False


@contextmanager
def _super_flow_file_lock():
    """Protect super-order flow cache writes across API worker processes."""
    handle = None
    try:
        handle = open(_SUPER_FLOW_FILE_LOCK_PATH, "a+", encoding="utf-8")
        if _main_fcntl is not None:
            _main_fcntl.flock(handle.fileno(), _main_fcntl.LOCK_EX)
        yield
    finally:
        if handle is not None:
            try:
                if _main_fcntl is not None:
                    _main_fcntl.flock(handle.fileno(), _main_fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()


def _load_super_flow(topn):
    """Return cached fund flow and refresh stale data without blocking requests."""
    should_block = False
    should_refresh = False
    with _SUPER_FLOW_LOCK:
        if _SUPER_FLOW_CACHE["data"] is None:
            try:
                with open(_SUPER_FLOW_PATH, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                _SUPER_FLOW_CACHE.update(
                    data=payload.get("data") or [],
                    ts=float(payload.get("updated_at") or 0),
                )
            except (OSError, ValueError, TypeError):
                pass
        age = time.time() - _SUPER_FLOW_CACHE["ts"]
        if not _SUPER_FLOW_CACHE["refreshing"]:
            if _SUPER_FLOW_CACHE["data"] is None:
                _SUPER_FLOW_CACHE["refreshing"] = True
                should_block = True
            elif age >= _SUPER_FLOW_TTL_SECONDS:
                _SUPER_FLOW_CACHE["refreshing"] = True
                should_refresh = True
        cached = _SUPER_FLOW_CACHE["data"]
    if should_block:
        _refresh_super_flow(topn)
        with _SUPER_FLOW_LOCK:
            return _SUPER_FLOW_CACHE["data"] or []
    if should_refresh:
        threading.Thread(
            target=_refresh_super_flow,
            args=(topn,),
            name="super-flow-refresh",
            daemon=True,
        ).start()
    return cached or []


def _load_selection_base():
    """Load compact persisted factors; rebuild only after the K-line manifest changes."""
    signature = _selection_signature()
    if (
        _SELECTION_CACHE["price_factors"] is not None
        and _SELECTION_CACHE["signature"] == signature
    ):
        return (
            _SELECTION_CACHE["price_factors"],
            _SELECTION_CACHE["first_board_codes"],
        )
    with _SELECTION_LOCK:
        if (
            _SELECTION_CACHE["price_factors"] is not None
            and _SELECTION_CACHE["signature"] == signature
        ):
            return (
                _SELECTION_CACHE["price_factors"],
                _SELECTION_CACHE["first_board_codes"],
            )
        try:
            with open(_SELECTION_META_PATH, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
            if str(meta.get("factor_date") or "")[:10] != _complete_daily_cutoff().isoformat():
                raise ValueError("选股因子缓存不是最近完整交易日")
            if float(meta.get("eligible_factor_coverage_pct") or 0.0) < 90.0:
                raise ValueError("选股因子缓存覆盖率不足90%")
            if (
                tuple(meta.get("signature", ())) == signature
                and os.path.exists(_SELECTION_FACTORS_PATH)
            ):
                price_factors = pd.read_csv(
                    _SELECTION_FACTORS_PATH,
                    dtype={"code": str},
                    index_col="code",
                )
                if not _SELECTION_REQUIRED_COLUMNS.issubset(price_factors.columns):
                    raise ValueError("选股因子缓存版本过旧")
                first_board_codes = set(meta.get("first_board_codes", []))
                _SELECTION_CACHE.update(
                    {
                        "signature": signature,
                        "price_factors": price_factors,
                        "first_board_codes": first_board_codes,
                    }
                )
                return price_factors, first_board_codes
        except (OSError, ValueError, TypeError):
            pass

        universe = U.load_universe()
        cutoff = _complete_daily_cutoff()
        klines = {}
        for row in universe:
            frame = dfc.load_shared_kline(row["code"])
            if frame is not None:
                frame = frame.loc[frame.index.date <= cutoff]
            if frame is not None and len(frame) > 65:
                klines[row["code"]] = frame
        price_factors = F.compute_price_factors(klines)
        # Never persist a mixed-date/partial factor cache.  A failed rebuild
        # remains an explicit blocked state; it cannot replace the last
        # validated snapshot with a few hundred rows.
        if "last_date" in price_factors:
            price_factors = price_factors.loc[
                price_factors["last_date"].astype(str).str[:10].eq(cutoff.isoformat())
            ].copy()
        eligible_codes = {
            str(row.get("code") or "")
            for row in universe
            if row.get("code") and _buy_scope(
                row.get("code"), row.get("name"), row.get("risk_flag"),
                row.get("instrument_type") or row.get("security_type"),
            )["allowed"]
        }
        eligible_factor_rows = len(set(price_factors.index.astype(str)) & eligible_codes)
        required_factor_rows = max(4000, int(len(eligible_codes) * 0.90 + 0.9999))
        if eligible_factor_rows < required_factor_rows:
            _SELECTION_CACHE.update({"signature": signature, "price_factors": price_factors,
                                     "first_board_codes": set()})
            return price_factors, set()
        first_board_codes = set(F.find_first_board_candidates(klines))
        factors_tmp = _SELECTION_FACTORS_PATH + ".tmp"
        price_factors.to_csv(factors_tmp, encoding="utf-8")
        os.replace(factors_tmp, _SELECTION_FACTORS_PATH)
        meta_tmp = _SELECTION_META_PATH + ".tmp"
        with open(meta_tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "signature": list(signature),
                    "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "factor_rows": len(price_factors),
                    "eligible_universe_rows": len(eligible_codes),
                    "eligible_factor_rows": eligible_factor_rows,
                    "eligible_factor_coverage_pct": round(eligible_factor_rows / max(len(eligible_codes), 1) * 100, 2),
                    "factor_date": cutoff.isoformat(),
                    "first_board_codes": sorted(first_board_codes),
                },
                handle,
                ensure_ascii=False,
            )
        os.replace(meta_tmp, _SELECTION_META_PATH)
        _SELECTION_CACHE.update(
            {
                "signature": signature,
                "price_factors": price_factors,
                "first_board_codes": first_board_codes,
            }
        )
        return price_factors, first_board_codes


def _warm_finance():
    try:
        _LIVE_CACHE["finance"] = dfc.fetch_finance_latest()
    except Exception:
        pass


def _update_live(key, fn):
    try:
        _LIVE_CACHE[key] = fn()
    except Exception:
        pass


def _warm_market_context():
    universe = U.load_universe()
    codes = {row["code"] for row in universe}
    names = {row["code"]: row["name"] for row in universe}
    jobs = [
        ("sentiment", lambda: F.compute_sentiment_factors(codes)),
        ("gate", F.overseas_risk_gate),
        ("sector_flow", lambda: dfc.fetch_sector_flow("industry")),
        ("news_hits", lambda: F.news_keyword_scan(names)),
    ]
    for key, fn in jobs:
        threading.Thread(
            target=_update_live,
            args=(key, fn),
            daemon=True,
        ).start()


def _warmup_caches():
    """Precompute full-market historical factors without blocking API startup."""
    try:
        _load_selection_base()
    except Exception:
        pass


@asynccontextmanager
async def _lifespan(_app):
    # 启动阶段只加载应用代码：不扫描全市场 CSV、不拉财报/海外/全市场快照。
    # 这些工作改为用户触发功能时按需执行，避免双击启动占满 CPU、内存和网络。
    # 初始化本地模拟盘账本（幂等且不请求行情）。读模型接口刻意不在每次
    # 请求中调用 init_db()，因此必须在服务启动阶段为全新克隆创建基础表；
    # 否则空数据卷会出现首页/风险页因 paper_jobs/paper_cycles 缺表而 500。
    P.init_db()
    # 盘后自动补齐历史K线并重建选股因子；线程内部带交易日、幂等和
    # 失败重试门禁，不占用请求线程，也不会触碰下单/风控路径。
    # 架构重构（2026-08-19）：容器内 daemon 兜底线程默认关闭，消除"宿主
    # cron + 容器内线程"双调度器竞态；所有定时任务由宿主 cron 统一触发
    # （docker exec 独立进程执行）。如临时需要兜底可设
    # ASTOCK_ENABLE_FALLBACK_THREADS=1 恢复。
    global _AUTO_INCREMENTAL_STARTED
    if not globals().get("_AUTO_INCREMENTAL_STARTED"):
        _AUTO_INCREMENTAL_STARTED = True
        if str(os.getenv("ASTOCK_ENABLE_FALLBACK_THREADS") or "0").strip().lower() in {"1", "true", "yes", "on"}:
            threading.Thread(target=_auto_incremental_loop, name="auto-data-incremental", daemon=True).start()
            threading.Thread(target=_paper_intraday_loop, name="paper-intraday-3m", daemon=True).start()
    yield


app = FastAPI(
    title="A股智能选股系统",
    version="2.0.0",
    lifespan=_lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=256, compresslevel=9)
app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND, "assets")), name="assets")
app.include_router(paper_router)
app.include_router(adaptive_router)
app.include_router(settings_router)


def _stock_code(code):
    code = str(code or "").strip()
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=422, detail="股票代码必须是 6 位数字")
    return code


def _buy_scope(code, name=None, risk_flag=None, instrument_type=None):
    """Single read-side eligibility rule for all buy suggestions.

    Execution has an equivalent hard gate in ``paper_trading``.  The public
    scanner/detail APIs must not describe a restricted board, ST stock or a
    non-equity instrument as a buyable idea simply because it has six digits.
    """
    code = str(code or "").strip()
    label = str(name or "").strip()
    upper = label.upper()
    kind = str(instrument_type or "").strip().lower()
    risk = str(risk_flag or "").strip().lower() in {"1", "true", "yes", "y", "risk", "st"}
    if len(code) != 6 or not code.isdigit():
        return {"allowed": False, "reason": "证券代码格式无效"}
    if risk or "ST" in upper or "退" in label:
        return {"allowed": False, "reason": "ST/退市风险证券不提供买入建议"}
    if kind in {"etf", "fund", "index", "bond", "convertible_bond", "unknown"}:
        return {"allowed": False, "reason": "当前系统仅研究可交易的沪深主板与创业板普通股"}
    if any(token in upper for token in ("ETF", "LOF", "基金", "指数")):
        return {"allowed": False, "reason": "基金或指数不属于当前股票买入范围"}
    if code.startswith(("688", "689")):
        return {"allowed": False, "reason": "科创板仅作产业共振参考，不生成买入建议"}
    if code.startswith(("4", "8", "92")):
        return {"allowed": False, "reason": "北交所股票不在当前交易权限范围"}
    if not code.startswith(("000", "001", "002", "003", "600", "601", "603", "605", "300", "301", "302")):
        return {"allowed": False, "reason": "不属于当前允许的沪深主板或创业板普通股范围"}
    return {"allowed": True, "reason": "沪深主板/创业板普通股"}


def _completed_daily_kline(frame):
    """Never pass an unfinished intraday daily bar into a buy decision."""
    if frame is None or frame.empty:
        return frame
    return frame.loc[frame.index.date <= _complete_daily_cutoff()]

@app.get("/")
def index():
    # 前端为单页脚本；禁止浏览器复用旧 HTML，避免已部署的界面文案/交互
    # 被上一版内存或磁盘缓存继续执行。
    return FileResponse(
        os.path.join(FRONTEND, "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/app.js", include_in_schema=False)
def frontend_app_script():
    """Serve the canonical frontend script, not the stale assets mirror.

    The repository historically kept both ``frontend/app.js`` and
    ``frontend/assets/app.js``.  The HTML was editing the former while
    ``StaticFiles`` published the latter, so deployments could report success
    while browsers continued executing obsolete dashboard code.
    """
    return FileResponse(
        os.path.join(FRONTEND, "app.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )

def _compute_overview():
    # 初始化时已落盘的全市场快照可立即使用；禁止首页加载时再次拉 5,000+ 行情。
    # 指数和海外数据可在后续用户操作中刷新，缺失时明确标为未知而不是阻塞页面。
    idx = _LIVE_CACHE["indices"]
    snap = U.load_universe()
    up = sum(1 for s in snap if isinstance(s.get("pct"), (int, float)) and s["pct"] > 0)
    down = sum(1 for s in snap if isinstance(s.get("pct"), (int, float)) and s["pct"] < 0)
    limit_up = sum(
        1 for s in snap
        if isinstance(s.get("pct"), (int, float))
        and s["pct"] >= F.limit_up_threshold(s.get("code", "")) * 100
    )
    limit_down = sum(
        1 for s in snap
        if isinstance(s.get("pct"), (int, float))
        and s["pct"] <= -F.limit_up_threshold(s.get("code", "")) * 100
    )
    amount = sum(s["amount"] for s in snap if isinstance(s.get("amount"), (int, float)))
    gate = _LIVE_CACHE["gate"] or {
        "light": "unknown",
        "detail": [],
        "advice": "海外数据未按启动阶段预加载，执行层将按保守规则处理。",
    }
    return {"indices": idx, "breadth": {"up": up, "down": down, "flat": len(snap) - up - down,
            "limit_up": limit_up, "limit_down": limit_down, "total": len(snap),
            "amount_yi": round(amount / 1e8, 0)}, "gate": gate}

@app.get("/api/overview")
def overview():
    # 使用本地快照，首页始终秒开；不在启动或页面轮询时触发全市场网络扫描。
    if _OVERVIEW_CACHE["data"] and (time.time() - _OVERVIEW_CACHE["ts"]) < 55:
        return _OVERVIEW_CACHE["data"]
    try:
        _OVERVIEW_CACHE["data"] = _compute_overview()
        _OVERVIEW_CACHE["ts"] = time.time()
    except Exception:
        if _OVERVIEW_CACHE["data"] is None:
            raise
    return _OVERVIEW_CACHE["data"]

@app.get("/api/sectors")
def sectors(type: str = "industry"):
    return {"sectors": dfc.fetch_sector_flow(type)}


@app.get("/api/sector_events")
def sector_events(limit: int = Query(10, ge=1, le=20)):
    """仅扫描强弱板块代表股的事件，避免独立舆情页全市场名称匹配。"""
    flows = dfc.fetch_sector_flow("industry")
    ranked = sorted(flows, key=lambda row: abs(float(row.get("main_net") or 0)), reverse=True)[:limit]
    universe = {str(row.get("code")): row for row in (U.load_universe() or [])}
    names = {}
    for sector in ranked:
        code = str(sector.get("top_stock_code") or "")
        row = universe.get(code)
        if row and row.get("name"):
            names[code] = row["name"]
    hits = F.news_keyword_scan(names) if names else []
    grouped = []
    for sector in ranked:
        code = str(sector.get("top_stock_code") or "")
        events = [hit for hit in hits if hit.get("code") == code][:3]
        grouped.append({
            "sector": sector.get("name"), "pct": sector.get("pct"), "main_pct": sector.get("main_pct"),
            "top_stock": sector.get("top_stock"), "top_stock_code": code,
            "events": events,
        })
    return {"events": grouped, "note": "仅针对资金异动板块的代表股按名称匹配公开快讯，可能遗漏或误匹配。"}

@app.get("/api/strategies")
def strategies():
    return {"strategies": [{"id": k, **v} for k, v in S.STRATEGIES.items()]}

@app.get("/api/init/status")
def init_status():
    st = U.get_init_state()
    st["data_ready"] = U.data_ready()
    return st

@app.get("/metrics")
def metrics_endpoint():
    """Prometheus 文本格式指标（零依赖，供监控采集）。"""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(MET.metrics_payload(), media_type="text/plain; version=0.0.4")


@app.get("/api/health")
def health():
    """不访问外部网络的数据健康检查，供界面快速判断缓存是否陈旧。"""
    cached = _cached_health_response()
    if cached is not None:
        return cached
    # This is a read-only dashboard path; formal selection/risk callers keep
    # the default uncached ``coverage_report()`` behavior.
    coverage = U.coverage_report(cache_ttl=U.COVERAGE_DISPLAY_CACHE_TTL_SECONDS)
    benchmark = dfc.load_shared_kline("BENCH_000300")
    latest = (
        str(benchmark.index[-1].date())
        if benchmark is not None and not benchmark.empty
        else None
    )
    stale_days = (
        (datetime.date.today() - datetime.date.fromisoformat(latest)).days
        if latest
        else None
    )
    warnings = []
    advisories = []
    live_snapshot = {
        "rows": 0, "saved_at": None, "quote_at_min": None,
        "quote_at_max": None, "age_seconds": None, "status": "unknown",
    }
    try:
        with open(dfc.MARKET_SNAPSHOT_FULL_CACHE_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        rows = rows if isinstance(rows, list) else []
        stamps = sorted(str(row.get("quote_at")) for row in rows if row.get("quote_at"))
        saved_at = payload.get("saved_at") if isinstance(payload, dict) else None
        live_snapshot.update({
            "rows": len(rows), "saved_at": saved_at,
            "quote_at_min": stamps[0] if stamps else None,
            "quote_at_max": stamps[-1] if stamps else None,
        })
        if saved_at:
            parsed_saved = datetime.datetime.fromisoformat(str(saved_at).replace("Z", "+00:00"))
            if parsed_saved.tzinfo is None:
                parsed_saved = parsed_saved.replace(tzinfo=datetime.timezone.utc)
            live_snapshot["age_seconds"] = round(
                max(0.0, (datetime.datetime.now(datetime.timezone.utc) - parsed_saved.astimezone(datetime.timezone.utc)).total_seconds()), 1
            )
        china_tz = datetime.timezone(datetime.timedelta(hours=8))
        china_now = datetime.datetime.now(china_tz)
        today = china_now.date()
        expected_quote_day = (
            today.isoformat() if U.is_trade_day(today)
            else U.previous_trade_day(today).isoformat()
        )
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        quote_day_rows = []
        fresh_rows = []
        for row in rows:
            stamp = str(row.get("quote_at") or "")
            if stamp[:10] != expected_quote_day:
                continue
            quote_day_rows.append(row)
            try:
                parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=datetime.timezone.utc)
                age = (now_utc - parsed.astimezone(datetime.timezone.utc)).total_seconds()
                if 0 <= age <= 1800:
                    fresh_rows.append(row)
            except (ValueError, TypeError):
                continue
        in_live_session = U.is_trade_day(today) and (
            datetime.time(9, 15) <= china_now.time() <= datetime.time(11, 35)
            or datetime.time(12, 55) <= china_now.time() <= datetime.time(15, 10)
        )
        live_snapshot["valid_today_rows"] = len(fresh_rows)
        live_snapshot["quote_day_rows"] = len(quote_day_rows)
        live_snapshot["expected_quote_day"] = expected_quote_day
        complete_snapshot = len(rows) >= 4000 and len(quote_day_rows) >= 4000
        live_snapshot["status"] = (
            "fresh" if in_live_session and len(rows) >= 4000 and len(fresh_rows) >= 4000
            else "closed_snapshot" if not in_live_session and complete_snapshot
            else "stale"
        )
        if len(rows) < 4000:
            warnings.append(f"全市场实时快照仅 {len(rows)} 行，未达到完整性门槛 4000")
        if in_live_session and len(fresh_rows) < 4000:
            warnings.append(f"全市场实时快照新鲜有效仅 {len(fresh_rows)} 行，已超过30分钟或不是当日行情")
        if not in_live_session and len(quote_day_rows) < 4000:
            warnings.append(f"最近完整交易日快照仅 {len(quote_day_rows)} 行，未达到完整性门槛 4000")
    except (OSError, ValueError, TypeError) as exc:
        live_snapshot["status"] = "missing"
        warnings.append(f"全市场实时快照读取失败：{type(exc).__name__}")
    source_health = dfc.load_source_health()
    if source_health and not source_health.get("healthy", False):
        warnings.append(source_health.get("action") or "最近一轮行情源探活未通过，等待自动重连")
    if coverage["coverage_pct"] < 90:
        warnings.append(f"K线覆盖率仅 {coverage['coverage_pct']}%")
    if coverage["fresh_selection_pct"] < 90:
        warnings.append("行情缓存可能已过期，请执行增量更新")
    if coverage["fallback_unadjusted"]:
        advisories.append(
            f"{coverage['fallback_unadjusted']} 只使用新浪不复权兜底，"
            "后续初始化会自动尝试升级为腾讯前复权"
        )
    # Health probes run every few seconds.  Do not call the full paper
    # dashboard here: that response includes orders, archives, curves and
    # (historically) quote enrichment, making the health check itself capable
    # of starving the UI.  Job history remains available from the paper API.
    payload = {
        "status": "degraded" if warnings else "ok",
        "version": app.version,
        "universe_size": coverage["universe_size"],
        "history_required": coverage["history_required"],
        "pending_listing_count": coverage["pending_listing"],
        "kline_files": coverage["covered"],
        "coverage_pct": coverage["coverage_pct"],
        "missing_count": coverage["missing"],
        "selection_usable": coverage["usable_selection"],
        "paper_risk_refresh": risk_refresh_status(),
        "data_source_health": source_health,
        "warnings": warnings,
        "advisories": advisories,
        "live_snapshot": live_snapshot,
        "selection_usable_pct": coverage["usable_selection_pct"],
        "backtest_usable": coverage["usable_backtest"],
        "backtest_usable_pct": coverage["usable_backtest_pct"],
        "fresh_kline_files": coverage["fresh"],
        "fresh_coverage_pct": coverage["fresh_pct"],
        "fresh_selection": coverage["fresh_selection"],
        "fresh_selection_pct": coverage["fresh_selection_pct"],
        "expected_reference_date": coverage["expected_reference_date"],
        "stale_count": coverage["stale"],
        "fallback_unadjusted_count": coverage["fallback_unadjusted"],
        "orphan_cache_files": coverage["orphan_cache_files"],
        "latest_trade_date": latest,
        "stale_calendar_days": stale_days,
        "init": U.get_init_state(),
    }
    _store_health_response(payload)
    return payload


def _data_update_snapshot():
    with _DATA_UPDATE_LOCK:
        return json.loads(json.dumps(_DATA_UPDATE_STATE, ensure_ascii=False, default=str))


def _mark_data_update_deferred(job_id, admission):
    """Persist a retryable admission failure without pretending the job ran."""
    with _DATA_UPDATE_LOCK:
        if _DATA_UPDATE_STATE.get("job_id") != job_id:
            return
        _DATA_UPDATE_STATE.update({
            "status": "deferred",
            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "result": {"admission": admission},
            "error": admission.get("reason") or "shared data lease busy",
        })


def _run_manual_incremental_update(job_id, target_date):
    """Run one data update under the shared worker lease.

    ``history_recovery_runner`` and the API both use ``heavy_job_lease``.  The
    lock is kernel-owned (flock), so a process/container restart releases it;
    a stale lock cannot permanently block the next update.
    """
    try:
        with heavy_job_lease("data-incremental") as admission:
            if not admission.get("allowed"):
                _mark_data_update_deferred(job_id, admission)
                return
            _run_manual_incremental_update_locked(job_id, target_date)
    except Exception as exc:
        # Admission failures must not strand the in-memory state at queued.
        # The next manual/automatic attempt can then reclaim the job safely.
        with _DATA_UPDATE_LOCK:
            if _DATA_UPDATE_STATE.get("job_id") == job_id:
                _DATA_UPDATE_STATE.update({
                    "status": "failed",
                    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "result": None,
                    "error": f"{type(exc).__name__}: {exc}",
                })


def _run_manual_incremental_update_locked(job_id, target_date):
    """Run the bounded history/factor refresh outside the request thread.

    This is deliberately the same refresh path as the close task.  It never
    opens an order and does not alter risk permissions; a partial result stays
    visible so the next click can resume the persisted retry queue.
    """
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _DATA_UPDATE_LOCK:
        _DATA_UPDATE_STATE.update({"status": "running", "job_id": job_id,
                                   "started_at": started, "finished_at": None,
                                   "target_date": target_date, "result": None,
                                   "error": None, "cancel_requested": False})
    try:
        try:
            dfc.check_data_source_health(force=True)
        except Exception:
            # Source probing is diagnostic; refresh_history owns its own
            # retries and must still be attempted when one probe fails.
            pass
        history = U.refresh_history(asof_day=target_date, workers=3, max_seconds=420)
        with _DATA_UPDATE_LOCK:
            cancelled = bool(_DATA_UPDATE_STATE.get("cancel_requested"))
        if cancelled:
            with _DATA_UPDATE_LOCK:
                _DATA_UPDATE_STATE.update({"status": "cancelled", "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "result": {"history": history, "factor": None}, "error": None})
            return
        # Persist the K-line manifest *before* calculating the factor
        # signature.  Flushing it afterwards changes only its mtime and makes
        # a freshly built factor cache look stale to the entry circuit-breaker,
        # which incorrectly freezes every strategy until the next rebuild.
        try:
            dfc.flush_kline_manifest()
        except Exception:
            pass
        _invalidate_health_cache()
        factor = None
        rebuild = getattr(P, "_rebuild_selection_factor_cache", None)
        if callable(rebuild):
            factor = rebuild(target_date)
        result = {"history": history, "factor": factor}
        final_status = "completed" if history.get("status") in {"ok", "up_to_date"} and (not factor or factor.get("status") == "ok") else "partial"
        with _DATA_UPDATE_LOCK:
            _DATA_UPDATE_STATE.update({"status": final_status, "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "result": result, "error": None})
    except Exception as exc:
        with _DATA_UPDATE_LOCK:
            _DATA_UPDATE_STATE.update({"status": "failed", "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "result": None, "error": f"{type(exc).__name__}: {exc}"})


def _queue_data_update(trigger="manual"):
    """Idempotently queue one incremental job for the latest complete day."""
    with _DATA_UPDATE_LOCK:
        # cancelling 也拒绝：与 _queue_factor_update 口径一致，否则取消中
        # 的旧线程与新任务线程会交叉覆写 _DATA_UPDATE_STATE。
        if _DATA_UPDATE_STATE.get("status") in {"queued", "running", "cancelling"}:
            return False, _data_update_snapshot()
        target = U.latest_complete_trade_date(datetime.date.today()).isoformat()
        job_id = f"data-incremental-{target}-{int(time.time())}"
        _DATA_UPDATE_STATE.update({"status": "queued", "job_id": job_id, "target_date": target,
                                   "result": None, "error": None, "cancel_requested": False, "trigger": trigger})
    threading.Thread(target=_run_manual_incremental_update, args=(job_id, target), name="data-incremental", daemon=True).start()
    return True, _data_update_snapshot()

def _run_factor_only_update(job_id, target_date):
    """Run factor-only refresh under the same cross-process worker lease."""
    try:
        with heavy_job_lease("factor-incremental") as admission:
            if not admission.get("allowed"):
                _mark_data_update_deferred(job_id, admission)
                return
            _run_factor_only_update_locked(job_id, target_date)
    except Exception as exc:
        with _DATA_UPDATE_LOCK:
            if _DATA_UPDATE_STATE.get("job_id") == job_id:
                _DATA_UPDATE_STATE.update({
                    "status": "failed",
                    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "result": None,
                    "error": f"{type(exc).__name__}: {exc}",
                })


def _run_factor_only_update_locked(job_id, target_date):
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _DATA_UPDATE_LOCK:
        _DATA_UPDATE_STATE.update({"status": "running", "job_id": job_id,
                                   "started_at": started, "finished_at": None,
                                   "target_date": target_date, "result": None,
                                   "error": None, "cancel_requested": False,
                                   "trigger": "factor-only"})
    try:
        rebuild = getattr(P, "_rebuild_selection_factor_cache", None)
        factor = rebuild(target_date) if callable(rebuild) else {"status": "failed", "reason": "因子重建接口不可用"}
        _invalidate_health_cache()
        final = "completed" if factor and factor.get("status") == "ok" else "partial"
        with _DATA_UPDATE_LOCK:
            _DATA_UPDATE_STATE.update({"status": final, "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                       "result": {"history": None, "factor": factor}, "error": None})
    except Exception as exc:
        with _DATA_UPDATE_LOCK:
            _DATA_UPDATE_STATE.update({"status": "failed", "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                       "result": None, "error": f"{type(exc).__name__}: {exc}"})

def _queue_factor_update(trigger="factor-only"):
    with _DATA_UPDATE_LOCK:
        if _DATA_UPDATE_STATE.get("status") in {"queued", "running", "cancelling"}:
            return False, _data_update_snapshot()
        target = U.latest_complete_trade_date(datetime.date.today()).isoformat()
        job_id = f"factor-incremental-{target}-{int(time.time())}"
        _DATA_UPDATE_STATE.update({"status": "queued", "job_id": job_id, "target_date": target, "trigger": trigger})
    threading.Thread(target=_run_factor_only_update, args=(job_id, target), name="factor-incremental", daemon=True).start()
    return True, _data_update_snapshot()


def _auto_incremental_loop():
    """Low-frequency post-close updater; never runs during the trading session."""
    last_slot = None
    while True:
        try:
            now = datetime.datetime.now()
            if U.is_trade_day(now.date()) and now.time() >= datetime.time(15, 20):
                slot = "15:20" if now.time() < datetime.time(15, 50) else ("15:50" if now.time() < datetime.time(16, 30) else "16:30")
                key = f"{now.date().isoformat()}-{slot}"
                if key != last_slot:
                    accepted, _ = _queue_data_update(trigger="auto:" + slot)
                    if accepted:
                        last_slot = key
            time.sleep(60)
        except Exception:
            time.sleep(60)

def _paper_intraday_loop():
    """Linux/container-side fallback for the 3-minute paper monitor.

    The historical implementation registers Windows ``schtasks``; the
    production server is CentOS and has no such scheduler, so without this
    loop the UI can claim 3-minute monitoring while no jobs run.
    """
    last_key = None
    while True:
        try:
            now = datetime.datetime.now()
            in_window = (
                (datetime.time(9, 30) <= now.time() <= datetime.time(11, 25))
                or (datetime.time(13, 0) <= now.time() <= datetime.time(14, 55))
            )
            if U.is_trade_day(now.date()) and in_window and now.minute % 3 == 0 and now.second < 20:
                key = f"{now.date().isoformat()}-{now.strftime('%H:%M')}"
                if key != last_key:
                    last_key = key
                    # Never wait synchronously for a full-market scan.  A
                    # slow provider or one stuck position must not suppress
                    # the next 3-minute dispatch.  run_slot's database lease
                    # remains the single concurrency/idempotence gate.
                    def _dispatch(slot_key=key):
                        try:
                            P.run_slot("intraday")
                        except Exception:
                            # run_slot persists its own failure/retry state;
                            # the scheduler must stay alive for later slots.
                            pass
                    threading.Thread(
                        target=_dispatch,
                        name=f"paper-intraday-{key}",
                        daemon=True,
                    ).start()
            time.sleep(10)
        except Exception:
            time.sleep(10)


@app.get("/api/data-validity")
def data_validity():
    """Fast, read-only data validity dashboard payload."""
    h = health()
    meta = {}
    try:
        with open(_SELECTION_META_PATH, encoding="utf-8") as handle:
            meta = json.load(handle) or {}
    except (OSError, ValueError, TypeError):
        meta = {}
    update = _data_update_snapshot()
    coverage = {
        "universe": h.get("universe_size", 0),
        "kline_files": h.get("kline_files", 0),
        "fresh_kline_files": h.get("fresh_kline_files", 0),
        "coverage_pct": h.get("coverage_pct", 0),
        "fresh_pct": h.get("fresh_coverage_pct", 0),
        "selection_usable": h.get("selection_usable", 0),
        "selection_usable_pct": h.get("selection_usable_pct", 0),
        "fallback_unadjusted": h.get("fallback_unadjusted_count", 0),
        "orphan_cache_files": h.get("orphan_cache_files", 0),
    }
    return {
        "status": h.get("status", "unknown"),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "coverage": coverage,
        "reference": {"latest_trade_date": h.get("latest_trade_date"), "expected_reference_date": h.get("expected_reference_date"), "stale_calendar_days": h.get("stale_calendar_days")},
        "live_snapshot": h.get("live_snapshot", {}),
        "source_health": h.get("data_source_health", {}),
        "factor_cache": {"factor_date": meta.get("factor_date"), "built_at": meta.get("built_at"), "factor_rows": meta.get("factor_rows", 0), "eligible_factor_coverage_pct": meta.get("eligible_factor_coverage_pct", 0)},
        "warnings": h.get("warnings", []),
        "incremental_update": update,
    }


@app.post("/api/data-validity/incremental")
def start_manual_incremental_update():
    """Queue one bounded manual incremental update; repeated clicks are idempotent."""
    accepted, state = _queue_data_update(trigger="manual")
    return {"accepted": accepted, "reason": None if accepted else "running", "incremental_update": state}

@app.post("/api/data-validity/factor/incremental")
def start_factor_incremental_update():
    """仅重建选股因子缓存，不重新下载历史K线。"""
    accepted, state = _queue_factor_update()
    return {"accepted": accepted, "reason": None if accepted else "running", "incremental_update": state}


@app.post("/api/data-validity/incremental/cancel")
def cancel_manual_incremental_update():
    """Request cancellation; the active network batch finishes safely, then factor rebuild is skipped."""
    with _DATA_UPDATE_LOCK:
        if _DATA_UPDATE_STATE.get("status") not in {"queued", "running"}:
            return {"accepted": False, "reason": "not_running", "incremental_update": _data_update_snapshot()}
        _DATA_UPDATE_STATE["cancel_requested"] = True
        _DATA_UPDATE_STATE["status"] = "cancelling"
    return {"accepted": True, "incremental_update": _data_update_snapshot()}

@app.post("/api/init")
def init_data(
    years: int = Query(3, ge=1, le=10),
    size: int = Query(0, ge=0, le=10000),
):
    import datetime
    beg = (datetime.date.today() - datetime.timedelta(days=365 * years)).strftime("%Y%m%d")
    started = U.init_history(beg=beg, size=size)
    _invalidate_health_cache()
    try:
        U.invalidate_coverage_cache()
    except AttributeError:
        pass
    return {"started": started, "state": U.get_init_state()}

def _select_uncached(
    strategy: str = "three_day",
    topn: int = Query(10, ge=1, le=100),
):
    if strategy not in S.STRATEGIES:
        return JSONResponse({"error": "未知策略"}, status_code=400)
    # 选股和回测的完整度要求不同。回测需要近乎全市场连续历史；选股只要
    # 使用“最近完整交易日”已有的因子行即可。不要因少量停牌/更新失败股票
    # 把整个选股页面锁死，更不能让过期行进入本次筛选。
    coverage = U.coverage_report()
    # Public selection and the paper scanner must share the same complete-
    # universe bar.  Running at 80% while the health page reports 90% ready
    # creates two different answers for the same market snapshot.
    if coverage["fresh_selection"] < 1000 or coverage["fresh_selection_pct"] < 90.0:
        return {
            "need_init": True,
            "message": "服务器当前可用的最近交易日选股数据不足，请等待增量更新完成",
            "coverage": coverage,
        }
    uni = U.load_universe() or U.build_universe()
    price_f, first_board_codes = _load_selection_base()
    if price_f.empty:
        return {"need_init": True, "message": "历史K线尚未就绪，请稍候"}
    reference_date = str(coverage["expected_reference_date"])
    complete_cutoff = U.latest_complete_trade_date(datetime.date.today()).isoformat()
    factor_dates = price_f["last_date"].astype(str).str[:10]
    # Public selection uses the same exact-date rule as paper trading: the
    # retained historical factor rows must all be from the latest complete
    # daily bar, never a mixture of a few fresh rows and old cache rows.
    price_f = price_f.loc[factor_dates.eq(complete_cutoff)].copy()
    first_board_codes = set(first_board_codes) & set(price_f.index)
    if price_f.empty:
        return {
            "need_init": True,
            "message": "最近完整交易日的选股因子尚未生成，请等待增量更新完成",
            "coverage": coverage,
        }
    # 初始化时保存的全市场快照提供横截面数据；慢速实时/财务/海外接口只在后台刷新，
    # 不再阻塞每次选股。最终入选股票仍逐只经过执行风控。
    # Public selection and paper execution share the same tradable universe.
    # Keep STAR as context only (the strategy layer can turn its sector
    # strength into a bounded bonus for an allowed peer), never as a pick.
    universe_by_code = {str(row.get("code")): row for row in uni if row.get("code")}
    eligible_codes = {
        code for code, row in universe_by_code.items()
        if _buy_scope(
            code,
            row.get("name"),
            row.get("risk_flag"),
            row.get("instrument_type") or row.get("security_type"),
        )["allowed"]
    }
    # Keep STAR rows in the factor table as context only.  The strategy layer
    # still hard-filters them from candidates, but needs same-snapshot STAR
    # rows to compute the bounded industry impulse for permitted main/ChiNext
    # peers.  Filtering them before ``run_strategy`` silently made that bonus
    # always zero in the public selector.
    star_context_codes = {
        code for code in universe_by_code
        if str(code).startswith(("688", "689"))
    }
    context_codes = eligible_codes | star_context_codes
    price_f = price_f.loc[price_f.index.astype(str).isin(context_codes)].copy()
    first_board_codes = set(first_board_codes) & eligible_codes
    covered_codes = set(price_f.index.astype(str))
    eligible_factor_rows = len(covered_codes & eligible_codes)
    required_factor_rows = max(4000, int(len(eligible_codes) * 0.90 + 0.9999))
    if eligible_factor_rows < required_factor_rows:
        return {
            "need_init": True,
            "message": f"最近完整交易日历史因子覆盖不足 {eligible_factor_rows}/{len(eligible_codes)}，需至少 {required_factor_rows}，已停止使用旧因子",
            "factor_date": complete_cutoff,
            "factor_rows": len(price_f),
            "eligible_factor_rows": eligible_factor_rows,
            "required_factor_rows": required_factor_rows,
        }
    if price_f.empty:
        return {
            "need_init": True,
            "message": "最近完整交易日没有可用于主板/创业板的合规选股因子",
        }
    # 历史因子只取到最近完整交易日；但页面上的价格、涨跌幅、量比和资金流
    # 必须来自盘中实时快照，不能把初始化时写入 universe.json 的旧值标为“今日”。
    # 保留基础库的风险/板块字段，再由实时字段覆盖可变行情字段。
    today = datetime.date.today()
    expected_live_date = (
        today.isoformat() if U.is_trade_day(today)
        else U.previous_trade_day(today).isoformat()
    )
    def _quote_age_ok(value, max_minutes=20):
        text = str(value or "").strip()
        if not text:
            return False
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
            age = (datetime.datetime.now(datetime.timezone.utc) - parsed.astimezone(datetime.timezone.utc)).total_seconds()
            return -120 <= age <= max_minutes * 60
        except (TypeError, ValueError, OverflowError):
            return False

    required_live = max(4000, int(len(eligible_codes) * 0.90 + 0.9999))

    now_clock = datetime.datetime.now().time()
    is_open_session = datetime.time(9, 15) <= now_clock <= datetime.time(15, 10)
    # During the session require a genuinely recent quote.  After the close,
    # the last complete closing snapshot remains valid for research/selection
    # and must not trigger an endless refresh loop merely because it is older
    # than 20 minutes.
    quote_age_limit = 20 if expected_live_date == datetime.date.today().isoformat() and is_open_session else 24 * 60

    def _valid_live_rows(rows):
        return [
            row for row in (rows or [])
            if str(row.get("quote_at") or "")[:10] == expected_live_date
            and _quote_age_ok(row.get("quote_at"), quote_age_limit)
            and row.get("code")
            and isinstance(row.get("price"), (int, float)) and row.get("price") > 0
            and isinstance(row.get("pct"), (int, float))
        ]

    def _safe_live_snapshot(*, force=False):
        """Fetch live quotes without allowing a source exception to leak.

        Selection must fail closed on insufficient coverage, not fail open (or
        turn a transient provider exception into a 500 response).
        """
        try:
            rows = dfc.fetch_market_snapshot_full(max_age=240, force=force)
        except Exception:
            return []
        return rows if isinstance(rows, list) else []

    live_rows = _safe_live_snapshot()
    # A disk fallback can be complete in row count but several days old.  Do
    # not let that cache masquerade as today's real-time quote; force a source
    # health probe and one full refresh before failing closed.
    live_rows = _valid_live_rows(live_rows)
    if len(set(str(row.get("code")) for row in live_rows) & eligible_codes) < required_live:
        try:
            dfc.check_data_source_health(force=True)
        except Exception:
            pass
        live_rows = _safe_live_snapshot(force=True)
        live_rows = _valid_live_rows(live_rows)
    live_map = {
        str(row.get("code")): row
        for row in live_rows
        if row.get("code")
        and isinstance(row.get("price"), (int, float))
        and row.get("price") > 0
        and isinstance(row.get("pct"), (int, float))
    }
    eligible_live_codes = {
        code for code, row in universe_by_code.items()
        if _buy_scope(code, row.get("name"), row.get("risk_flag"),
                      row.get("instrument_type") or row.get("security_type"))["allowed"]
    }
    live_eligible = eligible_live_codes & set(live_map)
    live_coverage = len(live_eligible) / max(len(eligible_live_codes), 1)
    required_live = max(required_live, max(4000, int(len(eligible_live_codes) * 0.90 + 0.9999)))
    if len(live_eligible) < required_live or live_coverage < 0.90:
        return {
            "need_init": True,
            "message": f"实时行情覆盖不足 {len(live_eligible)}/{len(eligible_live_codes)}（{live_coverage * 100:.1f}%），已停止使用旧快照；请稍后重试",
        }
    snap_f = [
        {**row, **live_map[row["code"]]}
        for row in uni
        if row["code"] in covered_codes and row["code"] in live_map
    ]
    if len(snap_f) < required_live:
        return {
            "need_init": True,
            "message": f"实时行情覆盖不足 {len(snap_f)}/{required_live}，已停止使用旧快照进行今日选股",
        }
    finance = _LIVE_CACHE["finance"]
    if not finance.get("data"):
        try:
            finance = dfc.fetch_finance_latest()
        except Exception:
            finance = {"data": {}, "report_dates": []}
        _LIVE_CACHE["finance"] = finance
    fund_f = F.compute_fundamental_factors(snap_f, finance, asof=complete_cutoff)
    # Technical factors remain anchored to downloaded complete daily K-lines;
    # mutable same-day fields must come from this validated live snapshot.
    price_f = price_f.copy()
    live_field_map = {
        "price": "price", "pct": "pct", "amount": "amount",
        "turnover": "turnover", "main_pct": "main_pct",
        "super_net_raw": "super_net", "main_net": "main_net",
    }
    for code, row in live_map.items():
        if code not in price_f.index:
            continue
        for target, source in live_field_map.items():
            value = row.get(source)
            if value is not None and str(value).strip() not in {"", "--", "-"}:
                price_f.loc[code, target] = value
    sentiment = _LIVE_CACHE["sentiment"]
    # 实时资金流直接从同一份行情取（含 f62/f184），省去一次全市场排行抓取
    realtime_flow = {
        s["code"]: (
            s.get("main_pct")
            if isinstance(s.get("main_pct"), (int, float))
            else None
        )
        for s in snap_f
    }
    try:
        super_rows = _load_super_flow(topn=max(len(snap_f), 100))
    except Exception:
        super_rows = []
    # The flow rank endpoint is cached independently from the quote snapshot.
    # Never inject a row with an absent/old timestamp into today's cross-section;
    # such rows silently turn stale capital-flow data into a live factor.
    realtime_super_flow = {
        str(row.get("code")): row.get("super_net")
        for row in super_rows
        if row.get("code")
        and str(row.get("quote_at") or "")[:10] == expected_live_date
        and _quote_age_ok(row.get("quote_at"), quote_age_limit)
        and isinstance(row.get("super_net"), (int, float))
    }
    news_hits = _LIVE_CACHE["news_hits"]
    gate = _LIVE_CACHE["gate"] or {
        "light": "unknown",
        "detail": [],
        "advice": "海外历史源暂不可用，执行层按未知风险保守处理",
    }
    table = S.build_factor_table(
        price_f,
        fund_f,
        sentiment,
        realtime_flow,
        realtime_super_flow=realtime_super_flow,
    )
    result = S.run_strategy(
        strategy,
        table,
        topn=topn,
        news_hits=news_hits,
        gate=gate,
        first_board_codes=first_board_codes,
    )
    for pick in result.get("picks", []) or []:
        live_row = live_map.get(str(pick.get("code")))
        if live_row:
            pick["quote_at"] = live_row.get("quote_at")
            pick["quote_source"] = live_row.get("source") or "live_snapshot"
            pick["quote_data_scope"] = "当日实时行情；技术指标使用前一完整交易日下载K线"
            pick["historical_factor_date"] = complete_cutoff
    result["universe_size"] = len(price_f)
    result["total_universe"] = len(uni)
    result["data_quality"] = {
        "reference_date": reference_date,
        "complete_cutoff": complete_cutoff,
        "eligible_factor_rows": len(price_f),
        "excluded_stale_rows": max(0, len(uni) - len(price_f)),
        "coverage_pct": coverage["fresh_selection_pct"],
        "live_quote_source": "东方财富全市场实时行情",
        "live_quote_at": max((str(row.get("quote_at") or "") for row in snap_f), default=None),
        "live_quote_coverage": len(snap_f),
        "live_eligible_coverage_pct": round(live_coverage * 100, 2),
        "kline_source": "与模拟盘共享：data_cache/klines（前复权日线）",
        "kline_source_version": dfc.SHARED_KLINE_SOURCE_VERSION,
        "kline_cache_path": dfc.KLINE_DIR,
        "note": "历史因子仅使用最近完整交易日；价格、涨跌幅、量比和资金流均由本次实时行情覆盖。未更新或停牌导致的旧因子不参与选股。选股与模拟盘读取同一份前复权日线缓存，不再维护独立K线库。",
    }
    result["finance_report_dates"] = finance.get("report_dates")
    result["latest_finance_report_date"] = (
        (finance.get("report_dates") or [None])[0]
    )
    result["annual_report_date"] = finance.get("annual_report_date")
    # 买入执行风控：每只策略选出的股必须经过六维核查 + T1-T5 时机判定
    snap_map = {s["code"]: s for s in snap_f}
    kline_map = {
        pick["code"]: _completed_daily_kline(dfc.load_shared_kline(pick["code"]))
        for pick in result.get("picks", [])
    }
    sector_flow = _LIVE_CACHE["sector_flow"]
    for p in result.get("picks", []):
        p["buy_decision"] = DE.buy_decision(
            p["code"], name=p.get("name"),
            kline=kline_map.get(p["code"]),
            snap=snap_map.get(p["code"]),
            sector_flow=sector_flow,
            overseas_gate=gate,
            news_hits=news_hits,
        )
    result["executable_count"] = sum(1 for p in result["picks"] if p["buy_decision"]["executable"])
    result["watchlist_count"] = sum(1 for p in result["picks"] if p["buy_decision"]["watchlist"])
    result["board_filter"] = "净利润优先按最新一季报/半年报/三季报累计进度判断，缺失时回退最近年报；公开研究策略仅允许沪深主板和创业板，统一排除ST/退市风险、科创板及北交所；科创板只作同产业实时共振加分"
    result["disclaimer"] = "选股结果经舆情否决+买入执行风控模型过滤：负面舆情一票否决，仅 T1/T2 为建议买入，T3 进入观察清单，T4/T5 不执行。仅供研究参考，不构成投资建议。"
    return result


@app.get("/api/select")
def select(
    strategy: str = "three_day",
    topn: int = Query(10, ge=1, le=100),
):
    """Coalesce identical selection requests on small servers."""
    key = (strategy, topn, _selection_signature())
    now = time.time()
    cached = _SELECT_RESULT_CACHE.get(key)
    if cached and now - cached["ts"] < _SELECT_RESULT_TTL_SECONDS:
        return cached["data"]
    with _SELECT_RESULT_LOCK:
        now = time.time()
        cached = _SELECT_RESULT_CACHE.get(key)
        if cached and now - cached["ts"] < _SELECT_RESULT_TTL_SECONDS:
            return cached["data"]
        result = _select_uncached(strategy=strategy, topn=topn)
        _SELECT_RESULT_CACHE.clear()
        _SELECT_RESULT_CACHE[key] = {"ts": time.time(), "data": result}
        _save_selection_result(strategy, topn, result)
        return result


@app.get("/api/select/latest")
def select_latest(
    strategy: str = "three_day",
    topn: int = Query(10, ge=1, le=100),
):
    """Read-only endpoint used when reopening the selection page.

    It deliberately never invokes the expensive full-market scan.  A missing
    file simply means the user has not completed a run for this strategy/topn.
    """
    if strategy not in S.STRATEGIES:
        return JSONResponse({"error": "未知策略"}, status_code=400)
    result = _load_selection_result(strategy, topn)
    if result is None:
        return {"found": False, "strategy": strategy, "topn": topn}
    result["found"] = True
    result["stale"], result["stale_reason"] = _selection_result_staleness(result)
    return result


@app.get("/api/selection-evaluation")
def selection_evaluation(
    strategy: str = "",
    limit: int = Query(30, ge=1, le=180),
):
    """Read-only forward validation for the automatic daily screener snapshots."""
    if strategy and strategy not in S.STRATEGIES:
        return JSONResponse({"error": f"????: {strategy}"}, status_code=400)
    return ST.dashboard(strategy=strategy, limit=limit)


@app.post("/api/selection-evaluation/refresh")
def refresh_selection_evaluation():
    """Refresh only tracked close prices; it never generates orders or paper trades."""
    return ST.update_observations()


@app.get("/api/backtest")
def backtest(
    strategy: str = "three_day",
    topn: int = Query(10, ge=1, le=100),
    rebalance: int = Query(10, ge=1, le=120),
    gate: bool = True,
):
    if strategy not in S.STRATEGIES:
        return JSONResponse({"error": f"未知策略: {strategy}"}, status_code=400)
    if not U.data_ready():
        return {"need_init": True, "message": "请先初始化历史数据"}
    return B.run_backtest(strategy, topn=topn, rebalance=rebalance, use_gate=gate)

@app.get("/api/optimize")
def optimize(strategy: str = "three_day"):
    if strategy not in S.STRATEGIES:
        return JSONResponse({"error": f"未知策略: {strategy}"}, status_code=400)
    if not U.data_ready():
        return {"need_init": True, "message": "请先初始化历史数据"}
    return O.optimize(strategy)

@app.get("/api/news")
def news():
    uni = U.load_universe()
    names = {u["code"]: u["name"] for u in uni}
    hits = F.news_keyword_scan(names) if names else []
    return {"news": dfc.fetch_fast_news(40), "stock_hits": hits}

@app.get("/api/hot")
def hot():
    ranks = dfc.fetch_hot_rank(50)
    snap = {s["code"]: s for s in dfc.fetch_market_snapshot_full(max_age=240)}
    out = []
    for r in ranks:
        s = snap.get(r["code"], {})
        out.append({**r, "name": s.get("name"), "price": s.get("price"),
                    "pct": s.get("pct"), "industry": s.get("industry")})
    return {"hot": out}

@app.get("/api/gate")
def gate():
    return F.overseas_risk_gate()

# ---------- 模拟盘 API 由 api_paper.router 管理 ----------

# ---------- 持仓跟踪与卖出纪律 ----------
import tracker as T

@app.post("/api/track/add")
def track_add(code: str, name: str = None, cost: float = None,
              strategy: str = None, note: str = None):
    code = _stock_code(code)
    return T.add_position(code, name=name, cost=cost, strategy=strategy, note=note)

@app.post("/api/track/remove")
def track_remove(code: str):
    code = _stock_code(code)
    return T.remove_position(code)

@app.get("/api/track")
def track_list():
    return T.list_positions()

@app.get("/api/track/check")
def track_check():
    d = T.check_positions()
    # 卖出决策风控：给每只持仓叠加 8档阶梯止盈 + 4类强制止损 + 次日竞价 Q1-Q5
    try:
        gate = F.overseas_risk_gate()
    except Exception:
        gate = None
    for p in d.get("positions", []):
        p["sell_decision"] = DE.sell_decision(
            {"code": p["code"], "name": p["name"], "cost": p["cost"],
             "peak_price": p.get("peak_price"), "hold_days": p.get("hold_days")},
            kline=dfc.load_shared_kline(p["code"]),
            overseas_gate=gate,
        )
    return d

@app.post("/api/track/rules")
def track_rules(trail_stop_pct: float = None, hard_stop_pct: float = None,
                max_hold_days: int = None, time_stop_min_ret: float = None):
    new = {k: v for k, v in {
        "trail_stop_pct": trail_stop_pct, "hard_stop_pct": hard_stop_pct,
        "max_hold_days": max_hold_days, "time_stop_min_ret": time_stop_min_ret,
    }.items() if v is not None}
    return {"rules": T.update_rules(new)}

@app.get("/api/sector_linkage")
def sector_linkage(window: int = 60, min_corr: float = 0.7):
    return L.sector_linkage(window=window, min_corr=min_corr)

@app.get("/api/scanner")
def scanner(pe_max: float = None, pb_max: float = None, roe_min: float = None,
            mom20_min: float = None, pct_min: float = None, topn: int = 30):
    """全市场扫描器：按因子条件过滤股票池"""
    if not U.data_ready():
        return {"need_init": True, "message": "请先初始化历史数据"}
    uni = U.load_universe() or U.build_universe()
    codes = [u["code"] for u in uni]
    klines = {}
    for c in codes:
        df = dfc.load_shared_kline(c)
        df = _completed_daily_kline(df)
        if df is not None and len(df) > 65:
            klines[c] = df
    if not klines:
        return {"need_init": True, "message": "历史K线尚未就绪"}
    price_f = F.compute_price_factors(klines)
    requested_codes = list(klines.keys())
    try:
        snapshot_result = dfc.fetch_realtime_for_codes(requested_codes, return_meta=True)
    except TypeError:
        # Keep compatibility with a test double/older worker, but still apply
        # the same fail-closed coverage gate to the returned list.
        snapshot_result = {"rows": dfc.fetch_realtime_for_codes(requested_codes)}
    except Exception as exc:
        snapshot_result = {"rows": [], "error": f"{type(exc).__name__}: {exc}"}
    if isinstance(snapshot_result, dict):
        snap = snapshot_result.get("rows") or []
        returned_codes = {str(row.get("code")) for row in snap if isinstance(row, dict) and row.get("code")}
        expected_count = len(requested_codes)
        coverage_pct = round(len(returned_codes) / max(expected_count, 1) * 100, 2)
        required_count = max(4000, int(expected_count * 0.90 + 0.9999))
        batch_coverage = snapshot_result.get("batches") or []
        if len(returned_codes) < required_count or coverage_pct < 90.0:
            return {
                "need_init": True,
                "status": "degraded",
                "message": f"实时行情覆盖不足 {len(returned_codes)}/{expected_count}（{coverage_pct:.1f}%），已停止正式扫描",
                "quote_coverage": {
                    "expected": expected_count, "returned": len(returned_codes),
                    "coverage_pct": coverage_pct, "required": required_count,
                    "missing_codes": snapshot_result.get("missing_codes") or [],
                    "batches": batch_coverage,
                },
            }
    else:
        snap = snapshot_result or []
        if len({str(row.get("code")) for row in snap if isinstance(row, dict) and row.get("code")}) < max(4000, int(len(requested_codes) * 0.90 + 0.9999)):
            return {"need_init": True, "status": "degraded", "message": "实时行情覆盖不足，已停止正式扫描"}
    snap_f = [s for s in snap if s["code"] in klines]
    try:
        finance = dfc.fetch_finance_latest()
    except Exception:
        finance = {"data": {}, "report_dates": []}
    # Scanner factors are point-in-time: a report is usable only when it was
    # visible by the completed daily cutoff, never merely because its period
    # end date is old enough.
    fund_f = F.compute_fundamental_factors(
        snap_f, finance, asof=U.latest_complete_trade_date(datetime.date.today()).isoformat()
    )
    sentiment = F.compute_sentiment_factors(set(klines.keys()))
    realtime_flow = {s["code"]: s.get("main_pct") for s in snap_f}
    table = S.build_factor_table(price_f, fund_f, sentiment, realtime_flow)
    df = table.reset_index().rename(columns={"index": "code"})
    # 全市场扫描与执行层共用同一证券范围，避免页面推荐无权限标的。
    permitted = df.apply(
        lambda row: _buy_scope(
            row.get("code"), row.get("name"), row.get("risk_flag"),
            row.get("instrument_type") or row.get("security_type"),
        )["allowed"],
        axis=1,
    )
    df = df.loc[permitted].copy()
    # 按条件过滤
    if pe_max is not None:
        df = df[df["pe"].notna() & (df["pe"] <= pe_max)]
    if pb_max is not None:
        df = df[df["pb"].notna() & (df["pb"] <= pb_max)]
    if roe_min is not None:
        df = df[df["roe"].notna() & (df["roe"] >= roe_min)]
    if mom20_min is not None:
        df = df[df["mom20_raw"].notna() & (df["mom20_raw"] >= mom20_min / 100)]
    # 按实时涨跌过滤
    if pct_min is not None:
        df_snap = pd.DataFrame(snap_f)[["code", "pct"]] if snap_f else pd.DataFrame()
        if not df_snap.empty:
            df = df.merge(df_snap[df_snap["pct"] >= pct_min], on="code", how="inner")
    # 按 score 排序（复用多因子权重，但不限策略）
    w = S.WEIGHTS["three_day"]  # 通用筛选沿用当前三日策略的基础排序
    score = pd.Series(0.0, index=df.index)
    for fac, wt in w.items():
        if fac in df.columns:
            score = score + df[fac].fillna(0) * wt
    df["score"] = score
    df = df.sort_values("score", ascending=False).head(min(topn, len(df)))
    picks = []
    for _, r in df.iterrows():
        picks.append({
            "code": r["code"], "name": r["name"], "industry": r.get("industry"),
            "price": round(float(r["price"]), 2) if not pd.isna(r.get("price")) else None,
            "pe": round(float(r["pe"]), 2) if not pd.isna(r.get("pe")) else None,
            "pb": round(float(r["pb"]), 2) if not pd.isna(r.get("pb")) else None,
            "roe": round(float(r["roe"]), 1) if not pd.isna(r.get("roe")) else None,
            "mom5": round(float(r["mom5_raw"]) * 100, 2) if not pd.isna(r.get("mom5_raw")) else None,
            "mom20": round(float(r["mom20_raw"]) * 100, 2) if not pd.isna(r.get("mom20_raw")) else None,
            "pct": round(float(r.get("pct")), 2) if not pd.isna(r.get("pct")) else None,
            "score": round(float(r["score"]), 3),
        })
    return {"count": len(picks), "picks": picks, "filters": {k: v for k, v in {
        "pe_max": pe_max, "pb_max": pb_max, "roe_min": roe_min,
        "mom20_min": mom20_min, "pct_min": pct_min,
    }.items() if v is not None}}

@app.get("/api/export/select")
def export_select(strategy: str = "three_day", topn: int = 20):
    """导出选股结果为 CSV"""
    import io
    result = select(strategy=strategy, topn=topn)
    if "need_init" in result:
        return result
    picks = result.get("picks", [])
    if not picks:
        return {"error": "无选股结果"}
    csv_lines = ["代码,名称,行业,现价,综合分,PE,PB,ROE,5日动量,20日动量,60日动量,今日涨跌,人气榜,买入决策"]
    for p in picks:
        bd = p.get("buy_decision", {})
        csv_lines.append(f'{p["code"]},{p["name"]},{p.get("industry","")},{p.get("price","")},{p["score"]},{p.get("pe","")},{p.get("pb","")},{p.get("roe","")},{p.get("mom5","")},{p.get("mom20","")},{p.get("mom60","")},{p.get("pct","")},{p.get("hot_rank","")},{bd.get("tier","")}')
    csv = "\n".join(csv_lines)
    return StreamingResponse(io.BytesIO(csv.encode("utf-8-sig")), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=select_{strategy}_{datetime.date.today()}.csv"})

@app.get("/api/stock_search")
def stock_search(q: str = ""):
    """搜索股票：支持代码或名称模糊匹配"""
    if not q or len(q) < 1:
        return {"results": []}
    uni = U.load_universe() or U.build_universe()
    ql = q.lower().strip()
    results = []
    for u in uni:
        code = str(u["code"])
        name = u.get("name", "")
        if ql in code or ql in name.lower():
            results.append({"code": code, "name": name, "industry": u.get("industry")})
        if len(results) >= 20:
            break
    return {"results": results}

@app.get("/api/stock_detail")
def stock_detail(code: str, refresh: bool = False):
    """个股分析：K线+财务+买卖决策+新闻"""
    code = _stock_code(code)
    if not U.data_ready():
        return {"need_init": True, "message": "请先初始化历史数据"}
    kline = _completed_daily_kline(dfc.load_shared_kline(code))
    if kline is None or kline.empty:
        return {"error": "无此股票的K线数据"}
    # 默认只用初始化阶段已落盘的全市场快照与进程内缓存。个股页不能因为一个
    # 外部来源变慢而卡住页面；用户明确点击“更新”时才触发外部刷新。
    universe = U.load_universe() or []
    local_snapshot = next((row for row in universe if str(row.get("code")) == code), {})
    if refresh:
        try:
            snap = dfc.fetch_realtime_for_codes([code])
        except Exception:
            snap = []
        s = snap[0] if snap else local_snapshot
        try:
            finance = dfc.fetch_finance_latest()
        except Exception:
            finance = {"data": {}, "report_dates": []}
    else:
        s = local_snapshot
        finance_cache = getattr(dfc, "_mem_cache", {}).get("finance")
        finance = finance_cache[1] if finance_cache else {"data": {}, "report_dates": []}
    scope = _buy_scope(
        code,
        s.get("name") or local_snapshot.get("name"),
        s.get("risk_flag") or local_snapshot.get("risk_flag"),
        s.get("instrument_type") or s.get("security_type")
        or local_snapshot.get("instrument_type") or local_snapshot.get("security_type"),
    )
    fin = finance.get("data", {}).get(code, {})
    # K线最近 120 日
    k = kline.tail(120)
    dates = [str(d.date()) for d in k.index]
    kdata = [float(k["close"].iloc[i]) for i in range(len(k))]
    vol = [int(k["volume"].iloc[i]) for i in range(len(k))]
    # MA
    ma5 = [None]*4 + [round(sum(kdata[max(0,i-4):i+1])/min(5,i+1),2) for i in range(4,len(kdata))]
    ma20 = [None]*19 + [round(sum(kdata[max(0,i-19):i+1])/min(20,i+1),2) for i in range(19,len(kdata))]
    # Bollinger Bands (20,2)
    bb_upper, bb_lower = [], []
    for i in range(len(kdata)):
        if i < 19: bb_upper.append(None); bb_lower.append(None)
        else:
            w = kdata[max(0,i-19):i+1]
            m = sum(w)/20; sd = (sum((x-m)**2 for x in w)/20)**0.5
            bb_upper.append(round(m+2*sd, 2)); bb_lower.append(round(m-2*sd, 2))
    # RSI(14)
    gains, losses = [], []
    for i in range(1, len(kdata)):
        diff = kdata[i] - kdata[i-1]
        gains.append(max(diff, 0)); losses.append(max(-diff, 0))
    rsi = [None]*14
    if len(gains) >= 14:
        avg_gain = sum(gains[:14])/14; avg_loss = sum(losses[:14])/14
        for i in range(14, len(gains)):
            avg_gain = (avg_gain*13 + gains[i])/14
            avg_loss = (avg_loss*13 + losses[i])/14
            rs = avg_gain/max(avg_loss, 0.001)
            rsi.append(round(100 - 100/(1+rs), 1))
    # MACD(12,26,9)
    ema12 = [kdata[0]]; ema26 = [kdata[0]]
    for i in range(1, len(kdata)):
        ema12.append(kdata[i]*2/13 + ema12[-1]*11/13)
        ema26.append(kdata[i]*2/27 + ema26[-1]*25/27)
    dif = [ema12[i]-ema26[i] for i in range(len(kdata))]
    dea = [dif[0]]; macd = [0]*1
    for i in range(1, len(dif)):
        dea.append(dif[i]*2/10 + dea[-1]*8/10)
        macd.append((dif[i]-dea[i])*2)
    # K线高开低收 + 涨幅列表
    opens = [float(k["open"].iloc[i]) for i in range(len(k))]
    highs = [float(k["high"].iloc[i]) for i in range(len(k))]
    lows = [float(k["low"].iloc[i]) for i in range(len(k))]
    pcts = [round((kdata[i]-kdata[i-1])/kdata[i-1]*100, 2) if i>0 and kdata[i-1]!=0 else 0 for i in range(len(kdata))]
    # 买卖决策
    names = {code: s.get("name", code)}
    if refresh:
        news_hits = F.news_keyword_scan(names)
        sector_flow = dfc.fetch_sector_flow("industry")
        try:
            gate = F.overseas_risk_gate()
        except Exception:
            gate = {"status": "unknown", "reason": "海外风险数据暂不可用"}
    else:
        # 已缓存新闻/板块资料可直接复用；不存在时以空资料保守展示，不发起网络请求。
        news_cache = getattr(dfc, "_mem_cache", {}).get("news")
        sector_cache = getattr(dfc, "_mem_cache", {}).get("sector_industry")
        news_hits = F.news_keyword_scan(names) if news_cache else []
        sector_flow = sector_cache[1] if sector_cache else []
        gate = _LIVE_CACHE.get("gate") or {"status": "unknown", "reason": "海外风险缓存尚未刷新"}
    buy_d = (
        DE.buy_decision(code, name=s.get("name"), kline=kline, snap=s,
                        sector_flow=sector_flow, overseas_gate=gate, news_hits=news_hits)
        if scope["allowed"] else {
            "tier": "禁止买入", "executable": False, "watchlist": False,
            "reason": scope["reason"], "security_scope": scope,
        }
    )
    sell_d = DE.sell_decision({"code": code, "name": s.get("name"), "cost": s.get("price"),
                               "peak_price": s.get("price"), "hold_days": 0},
                              kline=kline, overseas_gate=gate, news_hits=news_hits)
    # 相关新闻（匹配该股的）
    stock_news = [h for h in news_hits if h["code"] == code]
    return {
        "code": code,
        "name": s.get("name") or code,
        "industry": s.get("industry"),
        "price": s.get("price"),
        "pct": s.get("pct"),
        "pe": s.get("pe"), "pb": s.get("pb"),
        "roe": fin.get("roe"), "profit_yoy": fin.get("profit_yoy"),
        "rev_yoy": fin.get("rev_yoy"), "report_date": fin.get("report_date"),
        "mktcap": s.get("mktcap"), "float_cap": s.get("float_cap"),
        "turnover": s.get("turnover"),
        "main_pct": s.get("main_pct"),
        "security_scope": scope,
        "kline": {"dates": dates, "close": kdata, "open": opens, "high": highs, "low": lows,
                  "volume": vol, "pcts": pcts,
                  "ma5": ma5, "ma20": ma20, "bb_upper": bb_upper, "bb_lower": bb_lower,
                  "rsi": rsi, "macd_dif": dif, "macd_dea": dea, "macd_bar": macd},
        "buy_decision": buy_d,
        "sell_decision": sell_d,
        "news": [{"tone": n["tone"], "summary": n["summary"], "time": n["time"],
                  "source": n["source"], "keywords": n.get("keywords", [])} for n in stock_news],
    }

@app.get("/api/industry_peers")
def industry_peers(code: str, topn: int = 10):
    code = _stock_code(code)
    """同行业对比：列出同行业其他股票的基本面+行情"""
    if not U.data_ready():
        return {"need_init": True}
    uni = U.load_universe() or U.build_universe()
    target = next((u for u in uni if u["code"] == code), None)
    if not target:
        return {"error": "未找到该股票"}
    ind = target.get("industry")
    if not ind:
        return {"peers": []}
    peers = [u for u in uni if u.get("industry") == ind and u["code"] != code]
    codes = [p["code"] for p in peers[:topn]]
    if not codes:
        return {"industry": ind, "peers": []}
    snap = dfc.fetch_realtime_for_codes(codes)
    out = []
    for s in snap[:topn]:
        out.append({
            "code": str(s["code"]), "name": s.get("name"),
            "price": s.get("price"), "pct": s.get("pct"),
            "pe": s.get("pe"), "pb": s.get("pb"),
            "mktcap": s.get("mktcap"),
        })
    out.sort(key=lambda x: -(x.get("mktcap") or 0))
    return {"industry": ind, "peers": out, "count": len(out)}

@app.get("/api/track/feedback")
def track_feedback(strategy: str = None):
    import tracker as TK
    return TK.strategy_feedback(strategy) or {"count": 0}

@app.get("/api/buy_decision")
def buy_decision_api(code: str):
    code = _stock_code(code)
    universe = U.load_universe() or []
    row = next((item for item in universe if str(item.get("code")) == code), {})
    scope = _buy_scope(
        code, row.get("name"), row.get("risk_flag"),
        row.get("instrument_type") or row.get("security_type"),
    )
    if not scope["allowed"]:
        return {
            "tier": "禁止买入", "executable": False, "watchlist": False,
            "reason": scope["reason"], "security_scope": scope,
        }
    return DE.buy_decision(code)

@app.get("/api/sell_decision")
def sell_decision_api(code: str, cost: float = None, peak: float = None, hold_days: int = None):
    code = _stock_code(code)
    return DE.sell_decision(
        {"code": code, "name": None, "cost": cost, "peak_price": peak, "hold_days": hold_days},
        kline=dfc.load_shared_kline(code),
    )

if __name__ == "__main__":
    import sys
    import urllib.request
    import uvicorn

    if "--open-browser" in sys.argv:
        import webbrowser

        def _open_when_ready():
            url = "http://127.0.0.1:8600"
            for _ in range(120):
                try:
                    with urllib.request.urlopen(f"{url}/api/health", timeout=1) as response:
                        if response.status == 200:
                            webbrowser.open(url, new=2)
                            return
                except Exception:
                    time.sleep(0.5)

        threading.Thread(target=_open_when_ready, daemon=True).start()

    uvicorn.run(app, host="127.0.0.1", port=8600)
