# -*- coding: utf-8 -*-
"""A股智能选股系统 - FastAPI 后端"""
import os, sys, time, datetime, threading, json
from contextlib import asynccontextmanager
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import pandas as pd

import build_info as BI
import data_fetcher as dfc
import market_data_contract as MDC
import market_data_service as MDSvc
import universe as U
import factors as F
import strategies as S
import backtest as B
import optimizer as O
import decision_engine as DE
import linkage as L
import paper_trading as P
import selection_application as SEL
from selection_application import health_file_signature as _health_file_signature
from selection_application import completed_daily_kline as _completed_daily_kline
import selection_tracking as ST
import metrics as MET
import operator_auth
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from api_paper import (portfolio_allocation_router, portfolio_runtime_router,
                       portfolio_workspace_router, risk_refresh_status,
                       router as paper_router)
from api_adaptive import router as adaptive_router
from api_earnings import router as earnings_router
from api_settings import router as settings_router
import api_strategies as strategies_api
from api_strategies import router as strategies_router
from api_strategies import retirement_workflow_router
from resource_guard import heavy_job_lease

FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

_OVERVIEW_CACHE = {"data": None, "ts": 0.0}
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
_HEALTH_CACHE_TTL_SECONDS = 8.0
_HEALTH_CACHE_LOCK = threading.Lock()
_HEALTH_CACHE = {"key": None, "at": 0.0, "data": None}




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












































@asynccontextmanager
def _sync_operator_bypass_from_settings() -> None:
    """按运行时设置决定是否旁路操作员凭据。

    读不到设置就保持校验开启（fail-safe）：宁可多要一次解锁，也不因为
    一次读取失败把写接口敞开。
    """
    try:
        import runtime_settings as _RSET
        with P._db() as conn:
            security = _RSET.read(conn).get("security") or {}
        operator_auth.set_bypass(not bool(security.get("operator_auth_required", True)))
    except Exception:
        operator_auth.set_bypass(False)


async def _lifespan(_app):
    # 启动阶段只加载应用代码：不扫描全市场 CSV、不拉财报/海外/全市场快照。
    # 这些工作改为用户触发功能时按需执行，避免双击启动占满 CPU、内存和网络。
    # 初始化本地模拟盘账本（幂等且不请求行情）。读模型接口刻意不在每次
    # 请求中调用 init_db()，因此必须在服务启动阶段为全新克隆创建基础表；
    # 否则空数据卷会出现首页/风险页因 paper_jobs/paper_cycles 缺表而 500。
    P.init_db()
    _sync_operator_bypass_from_settings()
    # Deterministic demo mode: ASTOCK_DEMO=1 seeds a fully synthetic universe
    # and a narrated paper ledger (T+1 reject, stale-quote reject, hard-stop
    # sell, limit-up buy, NAV curve) so a fresh clone shows the whole
    # signal->risk->order->fill->NAV->audit chain offline.  Idempotent.
    if str(os.getenv("ASTOCK_DEMO") or "0").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            import demo_seed
        except ImportError:  # pragma: no cover - demo module co-located with backend
            demo_seed = None
        if demo_seed is not None:
            demo_seed.ensure_demo_data(
                force=str(os.getenv("ASTOCK_DEMO_FORCE") or "0").strip().lower() in {"1", "true", "yes", "on"},
            )
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
            # 兜底 worker 归研究入口自己：引擎不 import 研究工人，避免 import 环。
            import research_worker
            research_worker.start_research_worker_fallback()
    yield


app = FastAPI(
    title="A股智能选股系统",
    version="2.0.0",
    lifespan=_lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=256, compresslevel=9)
app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND, "assets")), name="assets")
app.include_router(paper_router)
app.include_router(portfolio_runtime_router)
app.include_router(portfolio_allocation_router)
app.include_router(portfolio_workspace_router)
app.include_router(adaptive_router)
app.include_router(settings_router)
app.include_router(strategies_router)
app.include_router(retirement_workflow_router)
app.include_router(earnings_router)


# ─── PR-2：HTTP 操作员安全边界（Operator Security Boundary）───────────────
# 统一入口在 operator_auth 模块；这里只做"取 Request 信息 → 调 operator_auth →
# 按 Decision 返回 → call_next"这一件事。**不在本文件复制** Bearer parser /
# Origin parser / IP parser / token 校验逻辑。
#
# 为什么用全局中间件而不是逐路由 Depends：
#   逐路由标注要求每个新写接口都被作者记得补上鉴权——这正是 PR-2 要消灭的
#   失效模式（"新增 mutation route 时容易忘记单独补鉴权"）。全局中间件按
#   **HTTP 方法**（而非 URL 前缀）判定，因此不存在"前缀旁路"：任何 POST/
#   PUT/PATCH/DELETE，无论挂在 /api/paper、/api/adaptive、/api/settings、
#   /api/strategies 还是 main.py 顶层（/api/init、/api/track/*、
#   /api/data-validity/* 等）都被同一条规则覆盖。
#
# 只读方法（GET/HEAD/OPTIONS）不经鉴权：控制面读写分离，看板无需密钥。
# 但**浏览器来源防护**（Origin / Sec-Fetch-Site）对写方法始终生效，因此
# 环回客户端也不能被跨站页面冒用（DNS-rebinding / localhost CSRF）。
_OPERATOR_STATUS_PATH = "/api/operator-status"


def _operator_request_context(request):
    """从 Request 取出判定所需的最小信息（不做任何解析逻辑）。

    客户端地址**只**取 ``request.client.host``——不读取 X-Forwarded-For /
    X-Real-IP / Forwarded（§17）。代理信任由 Uvicorn 的
    ``--proxy-headers --forwarded-allow-ips=127.0.0.1`` 负责（§18）。
    """
    client = getattr(request, "client", None)
    client_host = getattr(client, "host", None) if client is not None else None
    url = getattr(request, "url", None)
    scheme = getattr(url, "scheme", None) or "http"
    try:
        server_port = request.url.port
    except Exception:
        server_port = None
    if server_port is None:
        server_port = 443 if str(scheme).lower() == "https" else 80
    host_header = request.headers.get("host") or None
    return {
        "client_host": client_host,
        "scheme": scheme,
        "host_header": host_header,
        "server_port": server_port,
    }


@app.middleware("http")
async def _operator_boundary(request, call_next):
    if operator_auth.bypass_disabled():
        # 操作员在设置中心显式关闭了凭据校验。安全响应头仍然添加，
        # 只有 Bearer token 这一道门被拿掉。
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response
    context = _operator_request_context(request)
    decision = operator_auth.evaluate_request(
        request.method,
        request.headers,
        context["client_host"],
        scheme=context["scheme"],
        host_header=context["host_header"],
        server_port=context["server_port"],
    )
    if not decision.allowed:
        return JSONResponse(
            status_code=decision.status,
            content={"detail": decision.detail, "reason": decision.reason},
            headers=operator_auth.challenge_headers(decision),
        )
    response = await call_next(request)
    # 安全响应头：与 nginx 侧对齐（直连容器时也生效）。
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.get(_OPERATOR_STATUS_PATH)
def operator_status():
    """操作员边界状态（**只读**，永不回显 token）。

    只返回 ``mode`` / ``token_configured`` / ``writes_protected``；
    不返回 token 长度、前缀或任何强度细节（§55）。
    """
    return operator_auth.describe_configuration()


# 启动期把当前模式打进日志（只打模式名，不打 token 长度/值/前缀）。
operator_auth.log_configuration()




# PR-51：/api/strategies 的请求体由类型化契约（strategy_api_models）解析。
# FastAPI 默认把校验失败一律报 422，但 Strategy Admin 的历史语义是"缺必填
# 字段 → 400（``xxx is required``）、形状/类型错误 → 422"。这里让真实 HTTP
# 请求走与直接函数调用（api_strategies._coerce）完全相同的映射，避免两种入口
# 对同一个畸形请求给出不同的状态码；其余路由保持 FastAPI 原生行为不变。
@app.exception_handler(RequestValidationError)
async def _strategy_request_validation(request, exc):
    if request.url.path.startswith(strategies_router.prefix):
        errors = exc.errors()
        return JSONResponse(
            status_code=strategies_api.request_validation_status(errors),
            content={"detail": strategies_api.request_validation_detail(errors)},
        )
    return await request_validation_exception_handler(request, exc)


def _stock_code(code):
    code = str(code or "").strip()
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=422, detail="股票代码必须是 6 位数字")
    return code


from paper_trading_rules import selection_buy_scope as _buy_scope



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
    """Serve the bundled frontend script built from frontend/app.js.

    Sources are bundled into ``frontend/dist/`` by the esbuild pipeline; the
    committed artifact is served here so browsers never run stale checked-in
    mirrors.  ``frontend/dist`` is kept in sync with the sources by CI.
    """
    return FileResponse(
        os.path.join(FRONTEND, "dist", "app.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/app.css", include_in_schema=False)
def frontend_app_css():
    """Serve the bundled stylesheet built from frontend/app.css."""
    return FileResponse(
        os.path.join(FRONTEND, "dist", "app.css"),
        media_type="text/css",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )

def _compute_overview():
    # 初始化时已落盘的全市场快照可立即使用；禁止首页加载时再次拉 5,000+ 行情。
    # 指数和海外数据可在后续用户操作中刷新，缺失时明确标为未知而不是阻塞页面。
    market_context = SEL.live_market_context()
    idx = market_context["indices"]
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
    gate = market_context["gate"] or {
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

# PR-45：``/api/strategies`` 现在是 Strategy Admin API（注册表语义，
# 见 api_strategies.py）。选股扫描页的静态策略列表迁到专属路径，
# 避免同一路径两种语义把 UI 打挂。
@app.get("/api/scanner-strategies")
def strategies():
    return {"strategies": [{"id": k, **v} for k, v in S.STRATEGIES.items()]}


# PR-52：策略定义只有一套 API —— ``/api/strategies``（Strategy Admin，
# 见 api_strategies.py / strategy_service.py）。这里曾经并行存在
# ``/api/strategy-definitions``（定义列表 + 版本列表）与四个生命周期别名
# ``/api/strategies/{id}/validate|activate|pause|archive``，唯一调用方是
# 前端「模拟交易 → 策略中心」里那套旧定义编辑器；该编辑器已删除，这些
# 重复入口一并移除，生命周期写入统一走
# ``POST /api/strategies/{id}/transition``。
#
# 注意区分（两者语义不同，都必须保留）：
#   - ``POST /api/strategies/validate``  → DSL 校验（无副作用）
#   - ``POST /api/strategies/preview``   → 风险/资金预览（无副作用）
#   - ``POST /api/strategies/{id}/transition`` → 唯一生命周期写入入口


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


@app.get("/api/version")
def version():
    """PR-49：返回发布标识，零网络、不缓存。

    运维/发布核验用它把「本地 ↔ 服务器 ↔ GitHub」对齐到同一个 build id，
    不必再 md5 比对文件；设置中心也会展示它，便于一眼确认当前跑的是哪版。
    """
    return BI.build_payload()


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
    # R24：行情事实经唯一 authority 判定，health 只投影、不自己重算 freshness。
    # 数据事实（data_fact）与 provider 健康（provider_health）并列但互不推导：
    # provider 红灯不自动作废最后已验证的 snapshot，snapshot 存在也不代表源健康。
    try:
        market_data = MDSvc.market_health_projection(
            now=datetime.datetime.now(datetime.timezone.utc)
        )
    except Exception as exc:
        market_data = {
            "data_fact": {
                "status": "unavailable", "availability": "unavailable",
                "freshness": "unknown", "verification": "not_attempted",
                "verification_method": "none",
                "as_of": None, "observed_at": None, "age_seconds": None,
                "reason": "provider_unavailable", "policy": "market_health_display",
                "row_count": 0,
            },
            "snapshot_meta": {"saved_at": None, "expected_rows": 0, "rows": 0,
                              "complete": False},
            "provider_health": {"healthy": False, "checked_at": None,
                                "action": f"行情事实判定异常：{type(exc).__name__}"},
        }
    live_snapshot = {
        "rows": 0, "saved_at": None, "quote_at_min": None,
        "quote_at_max": None, "age_seconds": None, "status": "unknown",
    }
    try:
        # R24：经 Market Data Authority 读取 —— 它复用既有的 complete-marker 与
        # 行覆盖校验（``_full_snapshot_payload_is_complete``）。此前这里直接
        # ``open(MARKET_SNAPSHOT_FULL_CACHE_PATH)``，一份残缺/伪造 payload 会被
        # 当成正常快照计数。
        cached_reading, cached_payload = MDSvc.read_snapshot_with_meta(
            now=datetime.datetime.now(datetime.timezone.utc)
        )
        rows = [dict(row) for row in cached_reading.rows()]
        stamps = sorted(str(row.get("quote_at")) for row in rows if row.get("quote_at"))
        # ``saved_at`` 由 authority 保留（不再被丢成 None），因此这里的
        # data-validity 年龄口径与迁移前一致。
        saved_at = cached_payload.get("saved_at") if cached_payload else None
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
        # R24：freshness 的**判决**只有 Market Data Authority 一个来源。这里保留
        # live_snapshot 是因为 data-validity 页消费其行数/覆盖明细，但 status 不再
        # 在这里重新判一遍（否则同一响应里会出现两个权威口径）。
        live_snapshot["status"] = market_data["data_fact"]["status"]
        live_snapshot["closed_snapshot"] = bool(
            not in_live_session and complete_snapshot
        )
        if len(rows) < 4000:
            warnings.append(f"全市场实时快照仅 {len(rows)} 行，未达到完整性门槛 4000")
        if in_live_session and len(fresh_rows) < 4000:
            warnings.append(f"全市场实时快照新鲜有效仅 {len(fresh_rows)} 行，已超过30分钟或不是当日行情")
        if not in_live_session and len(quote_day_rows) < 4000:
            warnings.append(f"最近完整交易日快照仅 {len(quote_day_rows)} 行，未达到完整性门槛 4000")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # 明细行读取失败只影响展示；freshness 结论仍只由 authority 给出
        # （它已独立读过同一份校验过的 payload）。
        warnings.append(f"全市场实时快照明细读取失败：{type(exc).__name__}")
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
        # R24：行情事实的唯一投影（前端只渲染，不重算 freshness/provider 规则）。
        "market_data": market_data,
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
    import auction_tracking
    h = health()
    meta = SEL.selection_metadata()
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
        "auction_tracking": auction_tracking.status(),
        "source_health": h.get("data_source_health", {}),
        "factor_cache": {"factor_date": meta.get("factor_date"), "built_at": meta.get("built_at"), "factor_rows": meta.get("factor_rows", 0), "eligible_factor_coverage_pct": meta.get("eligible_factor_coverage_pct", 0)},
        "warnings": h.get("warnings", []),
        "incremental_update": update,
    }


@app.get("/api/auction/summary")
def auction_summary(trade_date: str | None = None, limit: int = 30):
    """Read-only auction snapshot: per-stock rows plus the observed amount summary.

    Defaults to today, falls back to ``current.json`` before the 09:25 archive
    exists, and never creates or migrates anything.
    """
    import auction_tracking

    try:
        size = max(1, min(int(limit or 30), 100))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="limit 必须是整数") from None
    raw_day = str(trade_date or datetime.datetime.now(datetime.timezone(
        datetime.timedelta(hours=8))).date().isoformat())[:10]
    # 只接受真实的 ISO 日期：日期直接拼进归档路径，不能让 "../" 之类的东西穿透目录。
    try:
        day = datetime.date.fromisoformat(raw_day).isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail="trade_date 必须是 YYYY-MM-DD") from None
    archived = auction_tracking.load_archived_day(day)
    if archived:
        rows = archived.get("rows") or []
        summary = archived.get("summary") or auction_tracking.summarize(rows)
        source, captured_at = "archive", archived.get("captured_at")
    else:
        current = auction_tracking.current_snapshot()
        rows, captured_at = [], None
        if current.get("trade_date") == day:
            final = [item for item in current.get("observations", [])
                     if item.get("phase") == "final_snapshot"]
            latest = final or current.get("observations", [])
            rows = (latest[-1].get("rows") if latest else []) or []
            captured_at = latest[-1].get("captured_at") if latest else None
        summary, source = auction_tracking.summarize(rows), "current"
    # 上游可能把缺失金额给成 "--" 之类的字符串：用跟踪模块的 _number 归一到 None，
    # 否则一次脏数据就让只读接口 500。
    ranked = sorted(
        rows, key=lambda row: -(auction_tracking._number(row.get("amount")) or 0.0))[:size]
    return {
        "trade_date": day,
        "source": source,
        "captured_at": captured_at,
        "summary": summary,
        "rows": ranked,
        "rows_returned": len(ranked),
        "rows_total": len(rows),
        "boundary": "竞价阶段没有连续成交；amount 是上游给出的当日累计撮合额，缺失即 None，未覆盖部分不计入。",
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





def _select_uncached(strategy: str = "three_day", topn: int = 10, *, background_context: bool = False):
    """Compatibility adapter for callers of the former HTTP-owned selector."""
    result = SEL.select_uncached(strategy=strategy, topn=topn,
                                 background_context=background_context)
    if isinstance(result, dict) and result.get("error") == "未知策略":
        return JSONResponse(result, status_code=400)
    return result


@app.get("/api/select")
def select(strategy: str = "three_day", topn: int = Query(10, ge=1, le=100)):
    result = SEL.select(strategy=strategy, topn=topn)
    if isinstance(result, dict) and result.get("error") == "未知策略":
        return JSONResponse(result, status_code=400)
    return result


@app.get("/api/paper-selection")
def paper_selection(
    date: str = "",
    strategy: str = "",
):
    """「策略选股」页只读入口：按策略分组返回最近交易日（或指定日）的结果。"""
    import paper_selection as PS
    return PS.latest(trade_date=date or None, strategy_id=strategy)


@app.post("/api/paper-selection/run")
def run_paper_selection(
    topn: int = Query(5, ge=1, le=20),
    strategy: str = "",
    date: str = "",
    confirmed: bool = Query(False),
):
    """人工重跑当天选股（覆盖写，不生成订单、不改动模拟盘账本）。"""
    if not confirmed:
        return JSONResponse({"error": "请先在页面点击确认后运行"}, status_code=422)
    import paper_selection as PS
    strategies = [item.strip() for item in str(strategy or "").split(",") if item.strip()]
    return PS.run_daily(strategies=strategies or None, topn=topn,
                        run_date=date or None, source="manual")


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
    result = SEL.latest_result(strategy, topn)
    if result is None:
        return {"found": False, "strategy": strategy, "topn": topn}
    result["found"] = True
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
    # R24：只读页面不为了"补价格"而同步刷新全市场快照 —— 经 authority 读
    # 当前已知事实；快照缺失/过期时相关字段留空而不是阻塞这个只读请求。
    snap = MDSvc.read_snapshot(
        MDC.LIVE_MARKET_POLICY, now=datetime.datetime.now(datetime.timezone.utc),
    ).snapshot
    snap_by_code = snap.by_code() if snap is not None else {}
    out = []
    for r in ranks:
        s = snap_by_code.get(r["code"], {})
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

#: ``factors.compute_price_factors`` 的语义版本：算法一变，历史缓存必须失效。
SCANNER_PRICE_FACTOR_VERSION = "scanner-price-factors-v1"
_SCANNER_HISTORY_LOCK = threading.Lock()
_SCANNER_HISTORY_CACHE = {"signature": None, "price": None, "codes": ()}


def _scanner_history_signature():
    """历史因子的输入指纹：完整交易日 + 历史清单版本 + 因子算法版本。"""
    return (
        _health_file_signature(dfc.KLINE_MANIFEST_PATH),
        _health_file_signature(dfc.KLINE_DIR),
        _health_file_signature(getattr(U, "UNIVERSE_PATH", "")),
        getattr(dfc, "SHARED_KLINE_SOURCE_VERSION", "unknown"),
        SCANNER_PRICE_FACTOR_VERSION,
        U.latest_complete_trade_date(datetime.date.today()).isoformat(),
    )


def _scanner_history_factors():
    """复用未变化的历史因子表：只改一个过滤条件不该重做整段历史。

    每次扫描都读几千个 CSV、解析日期、去重排序再算整张价格因子表，其中
    "完整交易日 + 历史清单 + 因子算法"三者不变时结果也完全相同。这里按这三项
    复用已验证的因子表；**实时行情仍独立检查覆盖率与新鲜度**，历史数据有效性
    检查也照旧执行（不是把整个响应缓存起来）。
    """
    signature = _scanner_history_signature()
    with _SCANNER_HISTORY_LOCK:
        cached = dict(_SCANNER_HISTORY_CACHE)
    if cached["signature"] == signature and cached["price"] is not None:
        return cached["price"], cached["codes"]
    uni = U.load_universe() or U.build_universe()
    klines = {}
    for item in uni:
        code = item["code"]
        frame = _completed_daily_kline(dfc.load_shared_kline(code))
        if frame is not None and len(frame) > 65:
            klines[code] = frame
    if not klines:
        return None, ()
    price_f = F.compute_price_factors(klines)
    codes = tuple(klines.keys())
    del klines
    with _SCANNER_HISTORY_LOCK:
        _SCANNER_HISTORY_CACHE.update(signature=signature, price=price_f, codes=codes)
    return price_f, codes


#: 证券范围判断需要读取的列（与 `selection_service._buy_scope` 的入参对应）。
_SCOPE_COLUMNS = ("code", "name", "risk_flag", "instrument_type", "security_type")


def _permitted_scope_mask(table):
    """证券范围掩码：逐行调用**同一个** ``_buy_scope``，但不重建行对象。

    ``DataFrame.apply(axis=1)`` 会为每一行构造一个 Series：5000 行 × 64 列时
    实测 46ms，同样规则的 ``itertuples`` 约 9.5ms（见
    ``scripts/source-audit-20261023/scope-probe.py``），掩码逐行一致。规则本身仍然
    只在 ``selection_service._buy_scope`` 一处，这里不做任何复制。
    """
    present = [column for column in _SCOPE_COLUMNS if column in table.columns]
    if "code" not in present:
        return pd.Series(False, index=table.index, dtype=bool)
    positions = {column: position for position, column in enumerate(present)}

    def field(record, column):
        position = positions.get(column)
        return record[position] if position is not None else None

    allowed = []
    for record in table[present].itertuples(index=False, name=None):
        allowed.append(_buy_scope(
            field(record, "code"), field(record, "name"), field(record, "risk_flag"),
            field(record, "instrument_type") or field(record, "security_type"),
        )["allowed"])
    return pd.Series(allowed, index=table.index, dtype=bool)


@app.get("/api/scanner")
def scanner(pe_max: float = None, pb_max: float = None, roe_min: float = None,
            mom20_min: float = None, pct_min: float = None, topn: int = 30):
    """全市场扫描器：按因子条件过滤股票池"""
    if not U.data_ready():
        return {"need_init": True, "message": "请先初始化历史数据"}
    price_f, requested_codes = _scanner_history_factors()
    if price_f is None:
        return {"need_init": True, "message": "历史K线尚未就绪"}
    history_codes = set(requested_codes)
    try:
        snapshot_result = dfc.fetch_realtime_for_codes(list(requested_codes), return_meta=True)
    except TypeError:
        # Keep compatibility with a test double/older worker, but still apply
        # the same fail-closed coverage gate to the returned list.
        snapshot_result = {"rows": dfc.fetch_realtime_for_codes(list(requested_codes))}
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
    snap_f = [s for s in snap if s["code"] in history_codes]
    try:
        finance = dfc.fetch_finance_latest()
    except Exception:
        finance = {"data": {}, "report_dates": []}
    # Scanner factors are point-in-time: a report is usable only when it was
    # visible by the completed daily cutoff, never merely because its period
    # end date is old enough.
    fund_f = F.compute_fundamental_factors(
        snap_f, finance,
        asof=U.latest_complete_trade_date(datetime.date.today()).isoformat(),
        snapshot_asof=F.live_snapshot_asof(),
    )
    sentiment = F.compute_sentiment_factors(history_codes)
    realtime_flow = {s["code"]: s.get("main_pct") for s in snap_f}
    table = S.build_factor_table(price_f, fund_f, sentiment, realtime_flow)
    df = table.reset_index().rename(columns={"index": "code"})
    # 全市场扫描与执行层共用同一证券范围，避免页面推荐无权限标的。
    df = df.loc[_permitted_scope_mask(df)].copy()
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
        gate = SEL.live_market_context().get("gate") or {
            "status": "unknown", "reason": "海外风险缓存尚未刷新"}
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
