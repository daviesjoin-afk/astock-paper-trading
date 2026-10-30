# -*- coding: utf-8 -*-
"""数据层：东方财富 + 腾讯免费公开接口，全部实测验证于 2026-07-28
传输层：requests + Session 连接复用，大幅降低进程开销"""
import datetime as dt
import contextvars
import copy
import hashlib
import os, json, time, threading, uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pandas as pd
import marketdata_cache as MDC
import marketdata_feeds as MDF
import marketdata_normalizers as MN
import marketdata_providers as MP
import data_paths
from env_utils import env_float
from marketdata_transport import (
    HEADERS,
    _session,
    _tencent_circuit,
    _tencent_circuit_lock,
    http_get,
    http_post_json,
    reset_data_source,
)

try:  # POSIX containers use flock; local Windows checks keep the thread lock.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised only on Windows.
    _fcntl = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ``ASTOCK_DATA_DIR`` must move *every* runtime artifact, not just the ledgers:
# the K-line cache, snapshots, manifest and health file are the inputs that
# selection / selection_tracking derive their own paths from.  Resolving the
# cache root through ``data_paths`` keeps one directory owner for the whole
# runtime state instead of splitting it between the source tree and an
# isolated directory.
CACHE_DIR = data_paths.data_dir()


# 低频事件源的缓存窗口。7x24 快讯每轮扫描都会整页重拉；公告按持仓代码
# 单独拉取。两者都属于"分钟级变化、分钟级可见"的数据，没必要在 3 分钟一轮
# 的扫描里各拉一次。新闻保留 5 分钟（风控负面事件仍需较快可见），公告保留
# 30 分钟（公告发布本身有交易所披露节奏，且重抓同一批文本没有新信息）。
NEWS_CACHE_TTL_SECONDS = env_float("ASTOCK_NEWS_CACHE_TTL_SECONDS", 300, minimum=1.0)
ANNOUNCEMENT_CACHE_TTL_SECONDS = env_float("ASTOCK_ANNOUNCEMENT_CACHE_TTL_SECONDS", 1800, minimum=1.0)
# 每个 cron 轮次都是新进程：只在内存里做 TTL 等于每次重新联网。新闻与公告
# 结果落到 data_cache，并用 flock single-flight 防止多个容器同时抓同一批。
NEWS_CACHE_PATH = os.path.join(CACHE_DIR, "fast_news.json")
ANNOUNCEMENT_CACHE_DIR = os.path.join(CACHE_DIR, "announcements")
_news_fetch_lock = threading.Lock()
_announcement_fetch_lock = threading.Lock()

KLINE_DIR = os.path.join(CACHE_DIR, "klines")
KLINE_MANIFEST_PATH = os.path.join(CACHE_DIR, "kline_manifest.json")
# Full-market and risk-proxy snapshots must never overwrite each other.  The
# risk page intentionally samples 20 pages for a fast read; that sample is not
# a valid fallback for the full-market selector or the 5-minute scanner.
MARKET_SNAPSHOT_FULL_CACHE_PATH = os.path.join(CACHE_DIR, "market_snapshot_full.json")
MARKET_SNAPSHOT_SAMPLE_CACHE_PATH = os.path.join(CACHE_DIR, "market_snapshot_sample_20.json")
MARKET_SNAPSHOT_FULL_LOCK_PATH = MARKET_SNAPSHOT_FULL_CACHE_PATH + ".lock"
DATA_SOURCE_HEALTH_PATH = os.path.join(CACHE_DIR, "data_source_health.json")
FINANCE_CACHE_PATH = data_paths.data_path("finance_latest.json")
CONCEPT_MEMBERS_CACHE_DIR = data_paths.data_path("concept_members")
CONCEPT_REFS_CACHE_DIR = data_paths.data_path("concept_refs")
# Board membership and stock-to-concept tags are reference data: they change
# when a board is rebalanced or a stock is re-tagged, not between two scans.
# The quote fields that ride along in the same payload are NOT reference data
# and are therefore dropped before caching; callers overlay the round's own
# live snapshot instead.
CONCEPT_REFERENCE_CACHE_TTL_SECONDS = 86400
# Bump when the stored shape changes. Earlier builds cached the whole provider
# payload, quotes included; without a version boundary those files would be
# served for a day as if they were membership-only entries.
CONCEPT_MEMBERS_CACHE_VERSION = 2
os.makedirs(KLINE_DIR, exist_ok=True)

UT_FLOW = "b2884a393a59ad64002292a3e90d46a5"
_cache_lock = threading.Lock()
_mem_cache = {}
_finance_fetch_lock = threading.Lock()
_finance_fetch_state = {"retry_at": 0.0}
_concept_member_locks = {}
_concept_member_locks_guard = threading.Lock()
_full_snapshot_thread_lock = threading.Lock()
_manifest_lock = threading.Lock()
_manifest = None
_manifest_mtime = None
_manifest_dirty = 0
_manifest_pending = {}

_health_probe_scope = contextvars.ContextVar("health_probe_scope", default=None)


@contextmanager
def health_probe_scope():
    """Reuse only a recent successful forced probe inside one caller workflow."""
    token = _health_probe_scope.set({})
    try:
        yield
    finally:
        _health_probe_scope.reset(token)


def _cached(key, ttl, fn):
    return MDC.cached(_mem_cache, _cache_lock, key, ttl, fn)


def _persistent_list(key, path, ttl, fn, fetch_lock):
    """跨进程复用列表型低频数据；空列表也是有效答案。"""
    return MDC.persistent_cached(
        _mem_cache, _cache_lock, fetch_lock, key, path, ttl, fn,
        lambda value: isinstance(value, list), allow_empty=True,
    )


def cached_age_seconds(key):
    """Read provider-cache age without extending its lifetime or fetching data."""
    with _cache_lock:
        entry = _mem_cache.get(key)
    if entry is None:
        return None
    try:
        age = time.time() - float(entry[0])
    except (TypeError, ValueError, IndexError):
        return float("inf")
    return age if 0 <= age < float("inf") else float("inf")


_full_snapshot_lock_context = threading.local()


@contextmanager
def _full_snapshot_singleflight_lock():
    """Serialize full-market refreshes across threads and worker processes.

    The in-memory cache is process-local, while API/cron/data-worker processes
    share the same disk cache.  A file lock prevents every process from
    launching its own 25-page refresh after the same TTL boundary.  Windows
    development has no ``fcntl``; the process-local lock still protects its
    threads and the lock file remains harmless.
    """
    # Nested publishers reuse this thread's existing file-lock ownership.
    if getattr(_full_snapshot_lock_context, "held", False):
        yield
        return
    with MDC.full_snapshot_singleflight_lock(
        _full_snapshot_thread_lock, MARKET_SNAPSHOT_FULL_LOCK_PATH
    ):
        _full_snapshot_lock_context.held = True
        try:
            yield
        finally:
            _full_snapshot_lock_context.held = False


def _get_json(url, params=None, timeout=15, retries=2):
    txt = http_get(url, params=params, timeout=timeout, retries=retries)
    if not txt:
        return None
    return json.loads(txt)


def _save_source_health(payload):
    return MDC.save_source_health(DATA_SOURCE_HEALTH_PATH, payload)


def load_source_health():
    """Return the latest persisted source check without touching the network."""
    return MDC.load_source_health(DATA_SOURCE_HEALTH_PATH)


def check_data_source_health(force=False):
    """Probe availability; either fresh stock-quote family can keep scans open.

    This is intentionally small (one market page + two index quotes), so it
    can run before every five-minute paper scan.  An empty/invalid response
    never enters the cache. If neither stock-quote family works, reconnect and
    retry once. Provider status stays separate from single-stock validation.
    A forced success can be reused for at most 30 seconds within an explicit
    workflow scope; failures are never reused and scope exit clears the window.
    """
    scope = _health_probe_scope.get()
    if force and scope is not None:
        cached = scope.get("successful_probe")
        if cached is not None and time.monotonic() - cached[0] < 30:
            return copy.deepcopy(cached[1])
    previous = load_source_health()
    checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
    if not force and previous.get("checked_at"):
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(previous["checked_at"])).total_seconds()
            if age < 240:
                return previous
        except (TypeError, ValueError):
            pass

    def _probe_eastmoney():
        try:
            fields = "f2,f3,f12,f14,f124"
            result = _fetch_clist(fields, fid="f20", pages=1, pz=100, return_meta=True)
            # 兼容旧调用方的 list 形状：没有 meta 时按"无主机可达性信息"处理，
            # 既不声称不可达，也不声称可达。
            meta = result if isinstance(result, dict) else {"rows": list(result or [])}
            rows = meta.get("rows") or []
            reach = _clist_host_reachability(meta)
            valid = [row for row in rows if len(str(row.get("f12") or "")) == 6
                     and str(row.get("f12") or "").isdigit()
                     and MN.source_quote_is_fresh({"price": row.get("f2"), "pct": row.get("f3"),
                                                  "quote_at": MN.quote_at(row.get("f124"))})]
            # 主机级不可达与"这一页没数据"是两件事，必须分开呈现：
            # 前者是机房出口被封（换域名无效），后者是数据级缺失。
            host_unreachable = bool(reach.get("host_unreachable"))
            if host_unreachable:
                status = "host_unreachable"
                reason = _flow_unreachable_reason(reach)
            elif valid:
                status = "fresh"
                reason = None
            else:
                status = "degraded"
                reason = "东方财富主机可达但本次未返回可用行情行（数据级缺失）"
            return {
                "status": status,
                "rows": len(rows), "valid_rows": len(valid),
                "coverage_pct": round(len(valid) / max(len(rows), 1) * 100, 1),
                "source": "eastmoney-clist",
                "host_unreachable": host_unreachable,
                "hosts_tried": list(reach.get("hosts_tried") or []),
                "host_outcomes": dict(reach.get("host_outcomes") or {}),
                "reason": reason,
            }
        except Exception as exc:
            return {"status": "failed", "rows": 0, "valid_rows": 0,
                    "coverage_pct": 0.0, "source": "eastmoney-clist",
                    "host_unreachable": False, "hosts_tried": [], "host_outcomes": {},
                    "reason": f"探测异常：{type(exc).__name__}: {exc}",
                    "error": f"{type(exc).__name__}: {exc}"}

    def _probe_tencent():
        try:
            text = http_get(
                "https://qt.gtimg.cn/q=sh000300,sh000001,sz399001",
                timeout=6, encoding="gbk", retries=1,
            )
            valid = 0
            latest = None
            for line in str(text or "").split(";"):
                if "=" not in line:
                    continue
                parts = line.split("=", 1)[1].strip().strip('"').split("~")
                if len(parts) >= 33:
                    try:
                        price = float(parts[3] or 0)
                    except (TypeError, ValueError):
                        price = 0
                    if MN.source_quote_is_fresh({"price": price, "pct": parts[32], "quote_at": parts[30]}):
                        valid += 1
                        latest = parts[30] or latest
            return {"status": "fresh" if valid >= 2 else "degraded",
                    "rows": valid, "valid_rows": valid,
                    "coverage_pct": round(valid / 3 * 100, 1),
                    "source": "tencent-index", "source_at": latest}
        except Exception as exc:
            return {"status": "failed", "rows": 0, "valid_rows": 0,
                    "coverage_pct": 0.0, "source": "tencent-index",
                    "error": f"{type(exc).__name__}: {exc}"}

    def _probe_independent_stock():
        # 指数接口正常不代表个股接口正常；策略成交门禁真正依赖的是个股报价。
        # 这里固定抽查沪深/北交各一只，并允许新浪作为腾讯个股接口的独立备用源。
        try:
            codes = {"300214", "688129", "920006"}
            rows = fetch_independent_realtime_for_codes(sorted(codes))
            rows = list({row["code"]: row for row in rows
                         if row.get("code") in codes and MN.source_quote_is_fresh(row)}.values())
            sources = sorted({str(row.get("source") or "") for row in rows if row.get("source")})
            return {
                "status": "fresh" if rows else "degraded",
                "rows": len(rows), "valid_rows": len(rows),
                "coverage_pct": round(len(rows) / 3 * 100, 1),
                "source": "+".join(sources) or "tencent_public_quote+sina_public_quote",
            }
        except Exception as exc:
            return {"status": "failed", "rows": 0, "valid_rows": 0,
                    "coverage_pct": 0.0,
                    "source": "tencent_public_quote+sina_public_quote",
                    "error": f"{type(exc).__name__}: {exc}"}

    attempts = []
    reconnected = False
    for attempt in range(2):
        if attempt:
            reset_data_source("行情源探测失败，重连并切换备用源", reset_circuit=True)
            reconnected = True
            time.sleep(0.35)
        eastmoney = _probe_eastmoney()
        tencent = _probe_tencent()
        independent_stock = _probe_independent_stock()
        attempts.append({"attempt": attempt + 1, "eastmoney": eastmoney, "tencent": tencent,
                          "independent_stock": independent_stock})
        if eastmoney["status"] == "fresh" or independent_stock["status"] == "fresh":
            break
    eastmoney = attempts[-1]["eastmoney"] if attempts else {"status": "failed"}
    tencent = attempts[-1]["tencent"] if attempts else {"status": "failed"}
    independent_stock = attempts[-1].get("independent_stock", {"status": "failed"}) if attempts else {"status": "failed"}
    healthy = eastmoney.get("status") == "fresh" or independent_stock.get("status") == "fresh"
    # 资金源状态独立成段：运营要一眼看出"东财整段不可达 / 腾讯新浪可用"，
    # 而不是从 "rows 0" 里反推。这里取的是**最近一次全市场资金流抓取**的
    # 主机级事实（只读，不额外触网）。
    #
    # 两级证据的优先级：本次探测的可达性（最新、且就是同一批 clist 主机）
    # 高于最近一次资金流抓取的快照。资金流抓取可能还没跑过（``unknown``），
    # 但探测已经证实了整段不可达 —— 那就必须如实报出来。
    flow_state = get_flow_fetch_state()
    flow_status = flow_state.get("flow_source_status") or MP.FLOW_SOURCE_UNKNOWN
    eastmoney_unreachable = bool(eastmoney.get("host_unreachable"))
    hosts_tried = list(eastmoney.get("hosts_tried") or []) or list(
        flow_state.get("hosts_tried") or [])
    host_outcomes = dict(eastmoney.get("host_outcomes") or {}) or dict(
        flow_state.get("host_outcomes") or {})
    if eastmoney_unreachable:
        # 探测已经证实整段不可达：状态就是 host_unreachable，不等资金流抓取。
        flow_status = MP.FLOW_SOURCE_HOST_UNREACHABLE
        summary = (
            "东方财富整段不可达（已试 "
            + "、".join(hosts_tried) +
            "），腾讯/新浪个股源可用；资金流字段全市场缺失"
        )
    elif flow_status == MP.FLOW_SOURCE_HOST_UNREACHABLE:
        summary = (
            "东方财富资金流主机整段不可达（已试 "
            + "、".join(hosts_tried) +
            "），腾讯/新浪个股源可用"
        )
    elif flow_status == MP.FLOW_SOURCE_SYMBOL_MISSING:
        summary = "东方财富资金流主机可达，仅个别标的/页缺资金字段（符号级缺失）"
    else:
        summary = "东方财富资金流状态未知或正常"
    payload = {
        "checked_at": checked_at,
        "healthy": healthy,
        "reconnected": reconnected,
        "attempts": len(attempts),
        "eastmoney": eastmoney,
        "tencent": tencent,
        "independent_stock": independent_stock,
        "host_rotation": "东方财富主机轮换 + 腾讯个股源重试 + 新浪个股备用",
        # 资金源可用性：**独立**于行情源可用性。行情源可用不代表资金源可用
        # （腾讯/新浪报文里根本没有资金流字段），反之亦然。
        "flow_source": {
            "status": flow_status,
            "host_unreachable": bool(flow_state.get("host_unreachable")
                                     or eastmoney_unreachable),
            "hosts_tried": hosts_tried,
            "host_outcomes": host_outcomes,
            "reason": flow_state.get("reason") or eastmoney.get("reason"),
            "summary": summary,
            "rows": int(flow_state.get("rows") or 0),
            "coverage_pct": flow_state.get("coverage_pct"),
            "checked_at": flow_state.get("fetched_at"),
        },
        "action": ("正常使用" if eastmoney.get("status") == independent_stock.get("status") == "fresh"
                   else "单源可用，继续运行" if healthy
                   else "暂无有效行情源，等待下一轮重连"),
    }
    _save_source_health(payload)
    if force and scope is not None and healthy:
        scope["successful_probe"] = (time.monotonic(), copy.deepcopy(payload))
    elif scope is not None:
        scope.pop("successful_probe", None)
    return payload

# ---------- 1. 大盘指数 + 海外市场实时（腾讯接口） ----------
TENCENT_CODES = {
    "sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指",
    "sh000300": "沪深300", "sh000905": "中证500", "sh000688": "科创50",
    "usDJI": "道琼斯", "usIXIC": "纳斯达克", "usINX": "标普500",
    "hkHSI": "恒生指数", "hkHSTECH": "恒生科技",
}

def fetch_indices():
    def _do():
        url = "https://qt.gtimg.cn/q=" + ",".join(TENCENT_CODES.keys())
        text = http_get(url, timeout=10, encoding="gbk")
        out = []
        for line in text.strip().split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            code = line.split("=")[0].replace("v_", "").strip()
            parts = line.split("=")[1].strip('"').split("~")
            if len(parts) < 40:
                continue
            try:
                out.append({
                    "code": code, "name": parts[1],
                    "price": float(parts[3] or 0), "prev_close": float(parts[4] or 0),
                    "change": float(parts[31] or 0), "pct": float(parts[32] or 0),
                    "amount": float(parts[37] or 0),  # 成交额(万)
                    "time": parts[30],
                    "market": ("A股" if code.startswith(("sh", "sz")) else ("美股" if code.startswith("us") else "港股")),
                })
            except (ValueError, IndexError):
                continue
        return out
    return _cached("indices", 30, _do)

# ---------- 2. 板块资金流向（东财，496 个行业+概念板块） ----------
def fetch_sector_flow(sector_type="industry"):
    fs = "m:90+t:2" if sector_type == "industry" else "m:90+t:3"
    def _do():
        params = {
            "pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "ut": UT_FLOW, "fid": "f62", "fs": fs,
            "fields": "f12,f14,f2,f3,f62,f66,f72,f78,f84,f124,f184,f204,f205",
        }
        # 多主机轮换 + 重试：主域名对该接口间歇性返回空（与全市场快照同样的问题）
        # 但这里是板块轮动的一个输入，单次 15 秒 × 2 次重试 × 3 主机 = 90 秒
        # 的尾部等待没有意义；改成 6 秒/0 重试 + 12 秒总预算，超时即按缺失处理。
        diff = []
        deadline = time.monotonic() + 12.0
        for attempt in range(2):
            for host in ["push2delay.eastmoney.com", "push2.eastmoney.com", "82.push2.eastmoney.com"]:
                if time.monotonic() >= deadline:
                    break
                try:
                    j = _get_json(f"https://{host}/api/qt/clist/get", params,
                                  timeout=6, retries=0)
                    diff = (j or {}).get("data", {}).get("diff", []) or []
                except Exception:
                    diff = []
                if diff:
                    break
            if diff or time.monotonic() >= deadline:
                break
            time.sleep(0.3)
        rows = []
        for d in diff:
            rows.append({
                "code": d.get("f12"), "name": d.get("f14"),
                "pct": d.get("f3"), "main_net": d.get("f62"),
                "super_net": d.get("f66"), "big_net": d.get("f72"),
                "mid_net": d.get("f78"), "small_net": d.get("f84"),
                "main_pct": d.get("f184"),
                "quote_ts": d.get("f124"), "quote_at": _quote_at(d.get("f124")),
                "top_stock": d.get("f204"), "top_stock_code": d.get("f205"),
            })
        return rows
    return _cached(f"sector_{sector_type}", 60, _do)


_NON_THEME_CONCEPT_TOKENS = (
    "融资融券", "沪股通", "深股通", "QFII", "标准普尔", "中证", "上证",
    "机构重仓", "基金重仓", "预亏", "预增", "昨日涨停", "百元股",
    "小盘股", "大盘股", "高股息", "转债标的", "MSCI",
)


def _finite_number(value):
    return MN.finite_number(value)


def _concept_member_fetch_lock(board_code):
    """Per-board lock so one board never serializes the whole parallel batch."""
    with _concept_member_locks_guard:
        return _concept_member_locks.setdefault(str(board_code), threading.Lock())


def _membership_only(payload):
    """Drop the quote fields that ride along with the constituent list."""
    return {
        **{key: value for key, value in payload.items() if key != "members"},
        "members": [
            {key: member.get(key) for key in ("code", "name", "industry")}
            for member in payload.get("members") or []
        ],
    }


def _fetch_concept_members(board_code, max_pages=8):
    """Fetch all currently reported members, with explicit completeness data.

    Board membership is reference data: it changes on a rebalance, not between
    two three-minute scans, so the membership list is shared across rounds and
    across worker processes for a day.  The member quotes that arrive in the
    same payload are deliberately stripped before caching, because caching them
    for a day would freeze the positive-ratio and active-peer pre-filter.
    Incomplete or empty answers are never cached: a failed page set must be
    retried, not promoted to discovery evidence.
    """
    def _fetch():
        return MP.fetch_eastmoney_concept_members(
            get_json=_get_json,
            hosts=CLIST_HOSTS,
            board_code=board_code,
            ut=UT_FLOW,
            quote_at=_quote_at,
            finite_number=_finite_number,
            max_pages=max_pages,
        )

    def _membership():
        return _membership_only(_fetch())

    name = str(board_code)
    if max_pages != 8 or not name.isalnum():
        return _fetch()
    os.makedirs(CONCEPT_MEMBERS_CACHE_DIR, exist_ok=True)
    return MDC.persistent_cached(
        _mem_cache, _cache_lock, _concept_member_fetch_lock(name),
        f"concept_members:v{CONCEPT_MEMBERS_CACHE_VERSION}:{name}",
        os.path.join(CONCEPT_MEMBERS_CACHE_DIR,
                     f"v{CONCEPT_MEMBERS_CACHE_VERSION}-{name}.json"),
        CONCEPT_REFERENCE_CACHE_TTL_SECONDS, _membership,
        lambda value: (isinstance(value, dict) and bool(value.get("complete"))
                       and bool(value.get("members"))),
    )


def _overlay_member_quotes(members, quotes):
    """Replace cached member quotes with the round's own live snapshot."""
    if not quotes:
        return members
    overlaid = []
    for member in members:
        live = quotes.get(str(member.get("code") or ""))
        if not isinstance(live, dict):
            overlaid.append(member)
            continue
        overlaid.append({**member,
                         **{key: live[key] for key in ("price", "pct")
                            if live.get(key) is not None}})
    return overlaid


def fetch_hot_concept_snapshot(topn=6, quotes=None):
    """Funded concepts and complete constituents; incomplete boards stay out.

    ``quotes`` is the caller's live full-market snapshot.  Constituents come
    from the daily membership cache and carry no quotes of their own, so the
    positive ratio is computed from that snapshot.
    """
    def _do():
        boards = []
        for row in fetch_sector_flow("concept") or []:
            name = str(row.get("name") or "").strip()
            pct, main_net = _finite_number(row.get("pct")), _finite_number(row.get("main_net"))
            if (not str(row.get("code") or "").startswith("BK") or not name
                    or any(token in name for token in _NON_THEME_CONCEPT_TOKENS)
                    or pct is None or main_net is None or pct <= 0 or main_net <= 0):
                continue
            boards.append({**row, "pct": pct, "main_net": main_net})
        boards.sort(key=lambda row: (row["main_net"], row["pct"]), reverse=True)
        boards = boards[:max(1, min(int(topn), 10))]
        fetched = {}
        with ThreadPoolExecutor(max_workers=min(4, len(boards) or 1)) as pool:
            futures = {pool.submit(_fetch_concept_members, str(board["code"])): board["code"] for board in boards}
            for future in as_completed(futures):
                try:
                    fetched[futures[future]] = future.result()
                except Exception:
                    fetched[futures[future]] = {"members": [], "complete": False}
        output = []
        for rank, board in enumerate(boards, 1):
            result = fetched.get(board["code"]) or {}
            members = _overlay_member_quotes(result.get("members") or [], quotes)
            if not result.get("complete") or not members:
                continue
            positive = sum(1 for member in members
                           if (_finite_number(member.get("pct")) or 0) > 0)
            output.append({**board, **{k: v for k, v in result.items() if k != "members"},
                           "rank": rank, "members": members, "member_count": len(members),
                           "positive_ratio": round(positive / len(members), 4),
                           "source": "eastmoney_concept_flow+complete_constituents"})
        return output
    return _cached(f"hot_concept_snapshot_{topn}", 75, _do)


def _stock_secid(code):
    return MN.stock_secid(code)


def _concept_ref_fetch_lock(code):
    with _concept_member_locks_guard:
        return _concept_member_locks.setdefault("refs:" + str(code), threading.Lock())


def _fetch_stock_concept_refs(code):
    """Fetch concept-board references for one stock, without inferring tags.

    ``slist`` is the inverse of the usual board->constituent endpoint.  It is
    deliberately used only for a very small set of strong leaders; doing this
    for the whole market would be both slow and prone to provider throttling.
    A stock's board tags are reference data, so they are reused for a day
    across rounds and processes; an empty answer is never cached.
    """
    def _fetch():
        secid = _stock_secid(code)
        if not secid:
            return []
        params = {
            "forcect": "1", "spt": "3", "pi": "0", "pz": "200", "po": "1",
            "fid": "f3", "fid0": "f4003", "fltt": "2", "invt": "2",
            "secid": secid, "fields": "f12,f14,f3,f128", "ut": UT_FLOW,
        }
        for host in CLIST_HOSTS:
            try:
                payload = _get_json(f"https://{host}/api/qt/slist/get", params,
                                    timeout=5, retries=0)
                rows = ((payload or {}).get("data") or {}).get("diff") or []
                refs = MP.parse_eastmoney_concept_refs(
                    rows, excluded_tokens=_NON_THEME_CONCEPT_TOKENS,
                )
                if refs:
                    return refs
            except Exception:
                continue
        return []

    name = str(code)
    if not name.isalnum():
        return _fetch()
    os.makedirs(CONCEPT_REFS_CACHE_DIR, exist_ok=True)
    return MDC.persistent_cached(
        _mem_cache, _cache_lock, _concept_ref_fetch_lock(name),
        f"concept_refs:{name}",
        os.path.join(CONCEPT_REFS_CACHE_DIR, f"{name}.json"),
        CONCEPT_REFERENCE_CACHE_TTL_SECONDS, _fetch,
        lambda value: isinstance(value, list) and bool(value),
    )


def fetch_leader_concept_snapshot(leaders, max_leaders=3, quotes=None):
    """Map a few genuine intraday leaders back to complete concept boards.

    This closes the missing ``leader -> concept -> peer`` link.  A leader
    never becomes an automatically tradable candidate: the returned board is
    only discovery evidence for *other* constituents, which still require a
    fresh full-market quote, positive flow, volume and all execution gates.

    ``quotes`` is the caller's live full-market snapshot.  Constituents come
    from the daily membership cache and carry no quotes of their own, so the
    positive ratio and active-peer count are computed from that snapshot.
    """
    leaders = [dict(row) for row in (leaders or []) if str(row.get("code") or "")]
    leaders = leaders[:max(1, min(int(max_leaders), 4))]
    if not leaders:
        return []
    cache_codes = ",".join(sorted(str(row.get("code")) for row in leaders))

    def _do():
        leader_by_code = {str(row.get("code")): row for row in leaders}
        refs_by_board = {}
        with ThreadPoolExecutor(max_workers=min(3, len(leaders))) as pool:
            futures = {pool.submit(_fetch_stock_concept_refs, row["code"]): row for row in leaders}
            for future in as_completed(futures):
                leader = futures[future]
                try:
                    refs = future.result()
                except Exception:
                    refs = []
                for ref in refs:
                    refs_by_board.setdefault(ref["code"], {**ref, "leader_codes": set()})["leader_codes"].add(str(leader["code"]))
        if not refs_by_board:
            return []

        # Board-flow values are enrichments, not a prerequisite.  A rising
        # board can be absent from the top-100 flow page while its leader and
        # multiple constituents provide enough independent live evidence.
        flow_rows = fetch_sector_flow("concept") or []
        flow_by_code = {str(row.get("code")): row for row in flow_rows}
        fetched = {}
        with ThreadPoolExecutor(max_workers=min(3, len(refs_by_board))) as pool:
            futures = {pool.submit(_fetch_concept_members, code): code for code in refs_by_board}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    fetched[code] = future.result()
                except Exception:
                    fetched[code] = {"members": [], "complete": False}

        output = []
        for board_code, ref in refs_by_board.items():
            result = fetched.get(board_code) or {}
            members = _overlay_member_quotes(result.get("members") or [], quotes)
            member_codes = {str(member.get("code") or "") for member in members}
            linked_leaders = [leader_by_code[code] for code in ref["leader_codes"] if code in member_codes]
            if not result.get("complete") or len(members) < 5 or not linked_leaders:
                continue
            positive = sum(1 for member in members if _finite_number(member.get("pct")) is not None and member["pct"] > 0)
            active_peers = sum(
                1 for member in members
                if str(member.get("code") or "") not in {str(leader.get("code")) for leader in linked_leaders}
                and 0.2 < (_finite_number(member.get("pct")) or -999) < 9.0
            )
            # A single limit-up stock is not a tradeable theme.  Require both
            # board breadth and two non-leader peers before exposing the board.
            if positive / len(members) < 0.45 or active_peers < 2:
                continue
            flow = flow_by_code.get(board_code) or {}
            output.append({
                **flow, **{k: v for k, v in result.items() if k != "members"},
                "code": board_code, "name": ref["name"], "members": members,
                "member_count": len(members), "positive_ratio": round(positive / len(members), 4),
                "active_peer_count": active_peers, "complete": True,
                "rank": int(flow.get("rank") or 999),
                "pct": _finite_number(flow.get("pct")) or 0.0,
                "main_net": _finite_number(flow.get("main_net")) or 0.0,
                "leader_context": [{
                    "code": str(leader.get("code")), "name": leader.get("name"),
                    "pct": _finite_number(leader.get("pct")),
                    "main_net": _finite_number(leader.get("main_net")),
                    "vol_ratio": _finite_number(leader.get("vol_ratio")),
                } for leader in linked_leaders],
                "source": "eastmoney_stock_concept_reverse+complete_constituents",
            })
        return output

    return _cached(f"leader_concept_snapshot_{cache_codes}", 45, _do)

# ---------- 3. 全市场快照 + 主力资金排行 ----------
SNAP_FS = (
    "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,"
    "m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:81+s:2048"
)

CLIST_HOSTS = ["push2.eastmoney.com", "82.push2.eastmoney.com", "push2delay.eastmoney.com"]
_clist_host_health = {}  # {host: {"failures": int, "cooldown_until": float}}
#: 最近一次 clist 请求的**主机级**可达性事实。``_fetch_clist`` 每次都更新，
#: 于是任何调用方（全市场快照 / 资金流 / 板块流 / 健康探测）都能读到同一份
#: "这一段东财主机到底通不通"的事实，而不必各自再探一次网。
#:
#: 结构与 ``MP.clist_host_summary`` 的输出一致：
#:   ``hosts_tried`` / ``host_outcomes`` / ``host_unreachable`` /
#:   ``reachable_hosts`` / ``unreachable_hosts``。
_clist_reachability = {}
_CLIST_HOST_COOLDOWN_BASE = 30
_CLIST_HOST_MAX_COOLDOWN = 300
FULL_MARKET_MIN_ROWS = 4000


def _full_market_min_rows():
    """Row threshold for a 'complete' full-market snapshot.

    Production default is FULL_MARKET_MIN_ROWS.  Demo mode (ASTOCK_DEMO=1)
    writes a small synthetic snapshot and overrides this via
    ASTOCK_FULL_MARKET_MIN_ROWS so the read models consume the synthetic data
    instead of falling back to network paths.
    """
    value = os.environ.get("ASTOCK_FULL_MARKET_MIN_ROWS", "").strip()
    if value.isdigit():
        return int(value)
    return FULL_MARKET_MIN_ROWS
FLOW_MIN_COVERAGE = 0.90
_flow_fetch_state = {
    "status": "unknown", "total": 0, "rows": 0, "coverage_pct": 0.0,
    "complete": False, "fetched_at": None, "failed_pages": [],
    "last_attempt_monotonic": 0.0,
    # 资金源"为什么没有数据"。``flow_source_status`` 区分两种语义：
    #   * ``host_unreachable`` —— 承载资金流的主机**整段**不可达（机房级）；
    #   * ``symbol_missing``  —— 主机可达，只是这只票/这批页缺字段。
    # 缺失时二者**必须**分开呈现，否则"今天全市场都没资金"会被读成
    # "这只票没资金数据"。
    "flow_source_status": MP.FLOW_SOURCE_UNKNOWN,
    "host_unreachable": False,
    "hosts_tried": [],
    "host_outcomes": {},
    "reason": None,
}
_hot_sector_fetch_state = {
    "status": "unknown", "shadow_only": True, "rows": 0,
    "pages_expected": 0, "pages_ok": 0, "failed_pages": [],
    "fetched_at": None,
}


def _clist_host_reachability(meta):
    """从 ``_fetch_clist`` 的 meta 里取出主机级可达性事实。

    ``meta`` 可能来自旧调用方（没有该字段），此时一律按"不可判定"处理：
    既不声称不可达，也不声称可达。
    """
    if not isinstance(meta, dict):
        return {}
    reach = meta.get("host_reachability")
    return reach if isinstance(reach, dict) else {}


def _flow_unreachable_reason(reach):
    """把主机级可达性事实渲染成**可辨识**的运维文案。

    文案必须一眼看出"东财整段不可达"而不是"缺几个字段"，并且带上试过
    哪些主机 —— 换域名救不回来这件事要能从日志里直接看出来。
    """
    if not reach or not reach.get("host_unreachable"):
        return None
    hosts = list(reach.get("hosts_tried") or [])
    tried = "、".join(hosts) if hosts else "（无候选主机）"
    return (
        f"东方财富资金流主机整段不可达（已试 {tried}）："
        "TCP 层全部连接失败，非字段缺失；换域名/换主机无效"
    )


def _sanitize_market_row(row):
    return MN.sanitize_market_row(row)

def _fetch_clist(fields, fid="f20", pages=None, pz=200, return_meta=False,
                 timeout=10, retries=1):
    """分页拉取 clist，并可返回分页完整性元数据。

    ``clist`` 会在单页失败时返回一个看似合法的残片。历史调用方仍默认
    得到 ``list`` 以保持兼容；需要将结果写入正式全市场/资金快照的调用方
    必须传 ``return_meta=True``，并检查 ``complete``，避免残片污染横截面。

    每次调用都会把**主机级**可达性事实写入 ``_clist_reachability``（模块级
    只读快照）：所有候选主机都连不上 = 这一段出口不可达；有主机应答了但
    没数据 = 数据级缺失。两者必须能被下游区分开。
    """
    global _clist_reachability
    result = MP.fetch_eastmoney_clist(
        get_json=_get_json,
        hosts=CLIST_HOSTS,
        host_health=_clist_host_health,
        fields=fields,
        fid=fid,
        pages=pages,
        pz=pz,
        return_meta=return_meta,
        ut=UT_FLOW,
        fs=SNAP_FS,
        cooldown_base=_CLIST_HOST_COOLDOWN_BASE,
        max_cooldown=_CLIST_HOST_MAX_COOLDOWN,
        timeout=timeout,
        retries=retries,
    )
    if isinstance(result, dict):
        reach = result.get("host_reachability")
        if isinstance(reach, dict):
            _clist_reachability = dict(reach)
    return result


def clist_host_reachability():
    """最近一次 clist 请求的主机级可达性事实（只读，不触网）。

    供风控审计与看板区分"整段不可达"与"个别标的缺失"。
    """
    return dict(_clist_reachability)

def fetch_market_snapshot(pages=None, allow_disk_fallback=True):
    """全市场快照：价格/涨跌/换手/量比/PE/PB/市值/行业。注意 clist 不支持 f37 等财务扩展字段"""
    cache_path = (
        MARKET_SNAPSHOT_FULL_CACHE_PATH
        if pages is None
        else (MARKET_SNAPSHOT_SAMPLE_CACHE_PATH if int(pages) == 20 else os.path.join(CACHE_DIR, f"market_snapshot_sample_{int(pages)}.json"))
    )
    def _disk_fallback():
        try:
            with open(cache_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                return []
            # A full-market cache is a formal input only when the writer
            # recorded a complete page set.  Legacy files without the marker
            # are treated as unknown and must be refreshed.
            if pages is None and not full_snapshot_payload_is_complete(payload):
                return []
            return rows
        except (OSError, ValueError, TypeError):
            return []

    def _do():
        # clist 的日内 OHLC/昨收字段是 f17/f15/f16/f18。f44/f45/f46/f60
        # 属于个股详情接口的另一套口径，在 clist 中会返回成交额/市值量级，
        # 不能当价格使用。
        fields = "f2,f3,f5,f6,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,f62,f66,f72,f78,f84,f100,f124,f184"
        result = _fetch_clist(fields, fid="f20", pages=pages, return_meta=(pages is None))
        if pages is None:
            raw_meta = result if isinstance(result, dict) else {"rows": [], "complete": False}
            raw = raw_meta.get("rows") or []
            pagination_complete = bool(raw_meta.get("complete"))
        else:
            raw = result if isinstance(result, list) else []
            pagination_complete = True
        # 本批页的主机级可达性：所有候选主机都连不上 = 这一段出口不可达。
        host_unreachable_rows = bool(
            (_clist_host_reachability(raw_meta) or clist_host_reachability())
            .get("host_unreachable"))
        rows = []
        for d in raw:
            row = _sanitize_market_row({
                "code": str(d.get("f12")), "name": d.get("f14"),
                "price": d.get("f2"), "pct": d.get("f3"),
                "open_price": d.get("f17"), "high": d.get("f15"),
                "low": d.get("f16"), "prev_close": d.get("f18"),
                "volume": d.get("f5"), "amount": d.get("f6"),
                "turnover": d.get("f8"), "pe": d.get("f9"), "vol_ratio": d.get("f10"),
                "mktcap": d.get("f20"), "float_cap": d.get("f21"),
                "pb": d.get("f23"), "industry": d.get("f100"),
                "main_net": d.get("f62"), "super_net": d.get("f66"),
                "big_net": d.get("f72"), "mid_net": d.get("f78"),
                "small_net": d.get("f84"),
                "main_pct": d.get("f184"),
                "quote_ts": d.get("f124"), "quote_at": _quote_at(d.get("f124")),
                # PIT：每行都带可信观测时点（优先行情揭示时间，缺失则本地抓取时刻）。
                # 没有它，strict 历史模式只能把这些动态字段判成不可用。
                "observed_at": MN.observed_at(d.get("f124")),
            })
            # 资金源状态随行透传：这行到底有没有拿到资金证据，以及"没有"的
            # 原因是个别标的缺字段还是**整段主机不可达**。下游风控据此把
            # "这只票没资金数据"和"今天全市场都没资金数据"分开表述。
            row["flow_source_status"] = MP.flow_source_status(
                row, host_unreachable=host_unreachable_rows)
            rows.append(row)
        # 全市场任务不得用少量残页覆盖上一份完整快照。部分市场数据会
        # 严重扭曲市场宽度、热点排序和候选覆盖率。
        unique_codes = {
            str(row.get("code") or "") for row in rows
            if isinstance(row, dict) and row.get("code")
        }
        try:
            expected_rows = int(raw_meta.get("total") or 0) if pages is None else 0
        except (TypeError, ValueError):
            expected_rows = 0
        full_coverage = (
            pagination_complete
            and len(rows) >= FULL_MARKET_MIN_ROWS
            and len(unique_codes) >= max(
                FULL_MARKET_MIN_ROWS,
                int(expected_rows * 0.90) if expected_rows else FULL_MARKET_MIN_ROWS,
            )
        )
        complete_enough = pages is not None or full_coverage
        if rows and complete_enough:
            try:
                payload = {"saved_at": dt.datetime.now(dt.timezone.utc).isoformat(), "rows": rows}
                if pages is None:
                    payload.update({"complete": bool(pagination_complete),
                                    "expected_rows": int(raw_meta.get("total") or len(rows)),
                                    "pages_expected": int(raw_meta.get("pages_expected") or 0),
                                    "pages_ok": int(raw_meta.get("pages_ok") or 0)})
                    _publish_full_snapshot(payload)
                else:
                    temp_path = f"{cache_path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                    with open(temp_path, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
                    os.replace(temp_path, cache_path)
            except OSError:
                pass
            return rows
        # Full-market callers must be able to fail closed.  Returning an old
        # complete file after a partial/failed refresh is acceptable for a
        # read-only dashboard, but it is unsafe for a live cross-sectional
        # scan.  The caller opts into that legacy fallback explicitly.
        return _disk_fallback() if allow_disk_fallback else []
    return _cached(f"snapshot_{pages}", 120, _do)


def fetch_tencent_market_snapshot(codes, *, max_age, now, force=False):
    """Serialize fallback requests and publication with the primary writer."""
    normalized = MDF.normalize_codes(codes)
    if len(normalized) < _full_market_min_rows():
        return []
    with _full_snapshot_singleflight_lock():
        if not force:
            cached = _fresh_full_snapshot_from_disk(max_age)
            wanted = set(normalized)
            usable = [row for row in cached or [] if row.get("code") in wanted]
            if len(usable) >= max(_full_market_min_rows(), len(wanted) * 0.90):
                return usable
        return _fetch_tencent_market_snapshot_locked(normalized, max_age=max_age, now=now)


def _snapshot_ohlc(row, price):
    """把上游行情行的日内高低/今开**如实**整理成快照字段。

    上游（腾讯/新浪解析器）已经做过逐字段的自洽校验（``low ≤ price ≤ high``
    且 ``low ≤ open ≤ high``，全部为正有限数）。这里再做一次**跨行**的
    收口校验，防止个别行带着不自洽的值进入全市场快照：

    * 三个字段各自必须是正有限数；
    * ``low ≤ high``；
    * 若 ``price`` 可用，必须 ``low ≤ price ≤ high``（现价落在日内区间内）。

    任何一条不成立就整体返回 ``(None, None, None)`` —— 保持 unknown，
    绝不填 0、绝不猜、绝不让别的字段顶替。**没有值就是 None**，下游按
    unknown 处理（``MN.finite_number`` 会把 None 归一成 None，不会变 0）。
    """
    def _num(key):
        value = MN.finite_number((row or {}).get(key))
        if value is None or value <= 0:
            return None
        return value

    low = _num("low")
    high = _num("high")
    open_price = _num("open_price")
    if low is None or high is None or high < low:
        return None, None, None
    if price is not None and not (low <= price <= high):
        return None, None, None
    if open_price is not None and not (low <= open_price <= high):
        open_price = None
    return open_price, high, low


def _fetch_tencent_market_snapshot_locked(codes, *, max_age, now):
    """Build a price-only full-market slice from the formal current code list."""
    normalized = MDF.normalize_codes(codes)
    if len(normalized) < _full_market_min_rows():
        return []
    try:
        rows = fetch_tencent_realtime_for_codes(normalized) or []
    except Exception:
        rows = []
    allowed = set(normalized)
    by_code = {
        str(row.get("code")): row for row in rows
        if isinstance(row, dict) and str(row.get("code")) in allowed
        and MDF.usable_quote(row)
        and MN.finite_number(row.get("pct")) is not None
    }
    try:
        from market_data_contract import _parse_instant
    except ImportError:
        from .market_data_contract import _parse_instant
    current = _parse_instant(now)
    if current is None:
        return []

    def _tencent_instant(value):
        try:
            return dt.datetime.strptime(value, "%Y%m%d%H%M%S").replace(
                tzinfo=dt.timezone(dt.timedelta(hours=8))
            )
        except (TypeError, ValueError):
            return _parse_instant(value)

    def current_row(row):
        stamp = _tencent_instant(row.get("quote_at"))
        return (MDF.usable_quote(row) and MN.finite_number(row.get("pct")) is not None
                and stamp is not None and -120 <= (current - stamp).total_seconds() <= max_age)

    missing = [code for code in normalized if not current_row(by_code.get(code) or {})]
    if missing:
        missing_codes = set(missing)
        try:
            alternate = fetch_sina_realtime_for_codes(missing)
        except Exception:
            alternate = []
        for row in alternate:
            code = str(row.get("code") or "")
            if code in missing_codes and row.get("source") == "sina_public_quote" and current_row(row):
                by_code[code] = row

    fresh = 0
    for row in by_code.values():
        observed = _tencent_instant(row["quote_at"])
        if observed is not None:
            age = (current - observed).total_seconds()
            if -120 <= age <= max_age:
                fresh += 1
    if fresh / len(normalized) < 0.90:
        return []
    snapshot = []
    # 腾讯/新浪个股报文里没有资金流字段；缺资金证据的原因是东财资金主机
    # 整段不可达（机房级），还是这一行本身就没有资金字段（符号级）。
    host_unreachable_rows = bool(clist_host_reachability().get("host_unreachable"))
    for code in normalized:
        row = by_code.get(code)
        if row is None:
            continue
        observed = _tencent_instant(row["quote_at"])
        if observed is None or not -120 <= (current - observed).total_seconds() <= max_age:
            continue
        price = MN.finite_number(row["price"])
        open_price, high, low = _snapshot_ohlc(row, price)
        volume = MN.finite_number(row.get("volume"))
        amount = MN.finite_number(row.get("amount"))
        turnover = MN.finite_number(row.get("turnover"))
        snapshot.append({
            "code": code, "name": row.get("name"), "price": price,
            "pct": MN.finite_number(row["pct"]), "prev_close": MN.finite_number(row.get("prev_close")),
            "quote_at": observed.isoformat(timespec="seconds"),
            "observed_at": observed.isoformat(timespec="seconds"),
            "source": row.get("source") or "tencent_public_quote",
            # 日内高低/今开：**如实透传**上游解析出的值。腾讯报文里本来就有
            # （idx5=今开、idx33=最高、idx34=最低），此前在这里被写死成 None，
            # 于是下游"收盘靠近日内低位"那 14 分永远拿不到、深跌票卡在门槛下。
            # 没有值就是 None（unknown），绝不填 0、绝不猜、绝不用别的字段顶替。
            "open_price": open_price, "high": high, "low": low,
            # 量/额/换手：腾讯报文 idx6/37/38 已经解析并做过量纲交叉校验，
            # 校验不通过时上游就不写这些键 —— 这里同样只透传真实值。
            "volume": volume, "amount": amount, "turnover": turnover,
            "pe": None, "vol_ratio": None, "mktcap": None,
            "float_cap": None, "pb": None, "industry": None,
            "main_net": None, "super_net": None, "big_net": None,
            "mid_net": None, "small_net": None, "main_pct": None,
            # 腾讯/新浪报文里**没有**资金流字段。这里如实标记"这一行没有
            # 资金证据"，并区分原因：东财资金主机整段不可达（机房级）还是
            # 仅仅这一行缺字段（符号级）。绝不拿委比/成交量顶替资金字段。
            "flow_source_status": MP.flow_source_status(
                {}, host_unreachable=host_unreachable_rows),
        })
    return snapshot if _persist_tencent_snapshot(snapshot, normalized) else []


def _publish_full_snapshot(payload):
    """Atomically publish a full-market payload under the shared singleflight lock.

    The full-market cache is shared by every writer (Eastmoney refresh, Tencent
    fallback, close task, data worker).  Publishing outside the lock lets two
    refreshes interleave so a partial Eastmoney page set can overwrite a
    complete Tencent snapshot, and vice versa.
    """
    with _full_snapshot_singleflight_lock():
        temp_path = f"{MARKET_SNAPSHOT_FULL_CACHE_PATH}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            os.replace(temp_path, MARKET_SNAPSHOT_FULL_CACHE_PATH)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        with _cache_lock:
            _mem_cache.pop("snapshot_None", None)


def _persist_tencent_snapshot(snapshot, normalized):
    """Persist a Tencent price-only snapshot as an explicitly labelled artifact.

    ``source`` is part of the payload so readers can tell a price-only slice
    from an Eastmoney cross-section: it must never be mistaken for evidence
    that flow, valuation or sector fields were verified too.
    """
    if len(snapshot) < _full_market_min_rows():
        return False
    payload = {
        "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": ("tencent_public_quote_full_snapshot"
                   if all(row.get("source") == "tencent_public_quote" for row in snapshot)
                   else "public_quote_full_snapshot"),
        "rows": snapshot, "complete": True, "expected_rows": len(normalized),
        "pages_expected": 0, "pages_ok": 0,
    }
    _publish_full_snapshot(payload)
    return True


def full_snapshot_payload_is_complete(payload):
    """Validate the persisted full-market snapshot marker and row coverage."""
    if not isinstance(payload, dict) or payload.get("complete") is not True:
        return False
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) < _full_market_min_rows():
        return False
    try:
        expected = int(payload.get("expected_rows") or 0)
    except (TypeError, ValueError):
        return False
    if expected <= 0:
        return False
    codes = {str(row.get("code") or "") for row in rows if isinstance(row, dict)}
    codes.discard("")
    return len(codes) >= max(_full_market_min_rows(), int(expected * 0.90))



def full_market_snapshot_source():
    """Report which provider actually served the published full snapshot.

    The intraday health probe issues one small ``clist`` request, so it can
    report a healthy Eastmoney source while the paged full-market fetch is
    being refused and the scan silently falls back to a price-only slice.
    Reading the published payload is the only way to see which one the scan
    really got, and it costs no request.
    """
    try:
        with open(MARKET_SNAPSHOT_FULL_CACHE_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "source": payload.get("source"),
        "saved_at": payload.get("saved_at"),
        "rows": len(payload.get("rows") or []),
        "price_only": payload.get("source") in {"tencent_public_quote_full_snapshot", "public_quote_full_snapshot"},
    }


def load_market_snapshot_full_cached():
    """Return the last persisted full-market snapshot rows.  Read-only, no network."""
    payload = load_market_snapshot_full_payload()
    rows = payload.get("rows")
    return rows if isinstance(rows, list) and rows else []


def load_market_snapshot_full_payload():
    """Return the last persisted full-market payload, validated.  No network.

    Callers that need the source metadata (``saved_at`` / ``expected_rows``)
    must use this instead of ``open()``-ing the cache file directly: the raw
    file read skips ``full_snapshot_payload_is_complete`` and would let a
    truncated or legacy payload be counted as a normal snapshot.
    """
    try:
        with open(MARKET_SNAPSHOT_FULL_CACHE_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    if not full_snapshot_payload_is_complete(payload):
        return {}
    return payload


def _fresh_full_snapshot_from_disk(max_age):
    """Read a complete full-market cache when both file and content are fresh."""
    try:
        age = max(0.0, time.time() - os.path.getmtime(MARKET_SNAPSHOT_FULL_CACHE_PATH))
        if age > max_age:
            return None
        with open(MARKET_SNAPSHOT_FULL_CACHE_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not full_snapshot_payload_is_complete(payload):
            return None
        # Content-level freshness: check newest quote_at timestamp.  Keep the
        # source timezone intact; forcing a UTC label makes Beijing timestamps
        # look eight hours old and causes a needless full refresh every scan.
        newest_quote_at = None
        for row in rows:
            quote_at = row.get("quote_at")
            if quote_at and (newest_quote_at is None or str(quote_at) > str(newest_quote_at)):
                newest_quote_at = quote_at
        if newest_quote_at:
            try:
                quote_time = dt.datetime.fromisoformat(str(newest_quote_at).replace("Z", "+00:00"))
                if quote_time.tzinfo is None:
                    quote_time = quote_time.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
                content_age = abs(
                    (dt.datetime.now(dt.timezone.utc) - quote_time.astimezone(dt.timezone.utc)).total_seconds()
                )
                if content_age > max_age:
                    return None
            except (TypeError, ValueError):
                # Preserve the previous mtime fallback for malformed quote_at.
                pass
        # 缓存准入必须和消费契约使用同一套横截面时效规则：只看"最新一行"
        # 会把 3999 条 600 秒前的旧数据 + 1 条刚更新当成新鲜，随后才被
        # market_data_service 判 stale。东财快照和腾讯价格快照都要检查覆盖率。
        try:
            from market_data_contract import fresh_ratio
        except ImportError:
            from .market_data_contract import fresh_ratio
        ratio = fresh_ratio(rows, dt.datetime.now(dt.timezone.utc), max_age, max_future_seconds=120)
        if ratio is None or ratio < 0.90:
            return None
        return rows
    except (OSError, ValueError, TypeError):
        return None


def fetch_market_snapshot_full(max_age=240, force=False):
    """Return a full-market live snapshot with a bounded cross-process TTL.

    Each paper-runner invocation is a fresh process, so the in-memory cache
    cannot protect the data source.  A short disk TTL lets the 5-minute jobs
    reuse a successful full snapshot while still refreshing several times per
    session.  A failed refresh falls back only to the full-market cache, never
    to the 20-page risk sample.  Expired callers share one refresh through a
    process/file lock and re-check the cache after acquiring it.
    """
    max_age = max(0.0, float(max_age))
    if force:
        # ``fetch_market_snapshot`` is memoized for 120s.  A force refresh must
        # actually probe the source, otherwise a failed source can look live
        # for an entire scan interval.
        with _cache_lock:
            _mem_cache.pop("snapshot_None", None)
    else:
        cached_rows = _fresh_full_snapshot_from_disk(max_age)
        if cached_rows is not None:
            return cached_rows

    with _full_snapshot_singleflight_lock():
        # Another process may have refreshed while this caller waited.  The
        # second check is deliberately skipped for an explicit force request.
        if not force:
            cached_rows = _fresh_full_snapshot_from_disk(max_age)
            if cached_rows is not None:
                return cached_rows
        # A stale/corrupt disk file must also invalidate a previously memoized
        # response; otherwise the process cache can silently bypass max_age.
        with _cache_lock:
            _mem_cache.pop("snapshot_None", None)
        # Never fall back to an arbitrarily old full snapshot here.  The caller
        # has already checked the bounded cache age; a failed refresh must stop
        # the candidate scan and trigger source recovery instead.
        rows = fetch_market_snapshot(pages=None, allow_disk_fallback=False)
        if isinstance(rows, list) and len(rows) >= _full_market_min_rows():
            return rows
        return []


def fetch_hot_sector_snapshot(pages=4):
    """用涨幅榜前若干页聚合盘中热点行业，避免为了板块轮动每5分钟扫描全市场。"""
    def _do():
        fields = "f2,f3,f8,f12,f14,f62,f100,f184"
        global _hot_sector_fetch_state
        requested_pages = max(1, min(int(pages), 6))
        # 热点板块只是候选评分的一个输入（4 页、可降级），不值得为它保留
        # 「3 主机 × 2 次 × 10 秒」的尾部等待；5 秒/0 重试仍保留主机轮换。
        raw_meta = _fetch_clist(fields, fid="f3", pages=requested_pages,
                                return_meta=True, timeout=5, retries=0)
        if not isinstance(raw_meta, dict):
            raw_meta = {"rows": raw_meta or [], "complete": False,
                        "pages_expected": requested_pages, "pages_ok": 0,
                        "failed_pages": list(range(1, requested_pages + 1))}
        raw = raw_meta.get("rows") or []
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
        _hot_sector_fetch_state = {
            "status": "fresh" if raw_meta.get("complete") else "degraded",
            "shadow_only": not bool(raw_meta.get("complete")),
            "rows": len(raw), "pages_expected": int(raw_meta.get("pages_expected") or 0),
            "pages_ok": int(raw_meta.get("pages_ok") or 0),
            "failed_pages": list(raw_meta.get("failed_pages") or []),
            "fetched_at": fetched_at,
        }
        # A missing middle page invalidates the cross-sectional sector rank.
        # Keep the diagnostic state for the shadow dashboard, but do not feed
        # an incomplete list into formal candidate scoring.
        if not raw_meta.get("complete"):
            return []
        grouped = {}
        for item in raw:
            industry = str(item.get("f100") or "").strip()
            pct = item.get("f3")
            name = str(item.get("f14") or "").strip()
            # 新股首日/次新股的涨幅不能代表行业强度；极端值也不能进入行业均值。
            # 东方财富涨幅榜会把 N/C 前缀股票混入榜单，若直接取前五会把行业涨幅放大到几十个百分点。
            name_upper = name.upper()
            if (not industry or not isinstance(pct, (int, float)) or
                    name_upper.startswith(("N", "C", "U", "W", "ST", "*ST")) or
                    abs(float(pct)) > 30):
                continue
            row = {
                "code": str(item.get("f12") or ""),
                "name": name,
                "pct": float(pct),
                "main_net": item.get("f62"),
                "main_pct": item.get("f184"),
            }
            grouped.setdefault(industry, []).append(row)
        sectors = []
        for industry, members in grouped.items():
            members.sort(key=lambda row: row["pct"], reverse=True)
            leaders = members[:5]
            if len(leaders) < 3:
                continue
            avg_pct = sum(row["pct"] for row in leaders) / len(leaders)
            positive = sum(1 for row in members if row["pct"] > 0)
            sectors.append({
                "name": industry,
                "pct": round(avg_pct, 2),
                "leader_count": len(members),
                "positive_ratio": round(positive / len(members), 3),
                "top_stock": leaders[0].get("name"),
                "top_stock_code": leaders[0].get("code"),
                "source": "盘中涨幅榜聚合",
            })
        sectors.sort(
            key=lambda row: (row["pct"], row["positive_ratio"], row["leader_count"]),
            reverse=True,
        )
        return sectors[:30]
    # The paper scanner runs every three minutes.  Keeping this cache for the
    # same full interval made a newly rotating sector invisible until the
    # following scan; 75s still shields the provider while giving each scan a
    # materially fresher sector view.
    return _cached(f"hot_sector_snapshot_{pages}", 75, _do)



def _realtime_row_from_ulist(raw):
    return MN.realtime_row_from_ulist(raw, quote_at_fn=_quote_at)


_REALTIME_FIELDS = "f2,f3,f5,f6,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,f62,f66,f72,f78,f84,f184,f100,f124"


def eastmoney_realtime_feed(fields=_REALTIME_FIELDS):
    return MDF.EastmoneyRealtimeFeed(
        get_json=_get_json,
        secid=_secid,
        row_parser=_realtime_row_from_ulist,
        reset_data_source=reset_data_source,
        ut=UT_FLOW,
        fields=fields,
        sleep=time.sleep,
    )



def fetch_realtime_for_codes(codes, fields=_REALTIME_FIELDS, return_meta=False):
    """Batch realtime quotes through the primary DataFeed adapter.

    The historical list/metadata return shapes stay unchanged so scan and
    execution callers keep their existing fail-closed coverage checks.
    """
    feed = eastmoney_realtime_feed(fields)
    return feed.fetch_realtime_with_meta(codes) if return_meta else feed.fetch_realtime(codes)


def tencent_realtime_feed():
    return MDF.TencentRealtimeFeed(
        http_get=http_get,
        parser=MP.parse_tencent_realtime_text,
        reset_data_source=reset_data_source,
        sleep=time.sleep,
    )



def sina_realtime_feed():
    return MDF.SinaRealtimeFeed(
        session_factory=_session,
        headers=HEADERS,
        parser=MP.parse_sina_realtime_text,
        reset_data_source=reset_data_source,
        sleep=time.sleep,
    )



def _ths_realtime_feed():
    return MDF.ThsRealtimeFeed(
        http_get=http_get,
        parser=MP.parse_ths_realtime_text,
        reset_data_source=reset_data_source,
        sleep=time.sleep,
    )


def _independent_realtime_feed():
    # 腾讯 → 新浪 → 同花顺。前两家都缺价时才启用同花顺（单票一请求，上限 8 只），
    # 让"独立核验源整体不可用"不再等于"只能按单一源价格决策"。
    return MDF.DataFeedChain(
        (tencent_realtime_feed(), sina_realtime_feed(), _ths_realtime_feed()),
        reset_data_source=reset_data_source,
        sleep=time.sleep,
        retry_last_feed=True,
        accept_quote=MN.source_quote_is_fresh,
    )


def fetch_tencent_realtime_for_codes(codes):
    """Independent Tencent quote source through the shared DataFeed contract."""
    return tencent_realtime_feed().fetch_realtime(codes)


def fetch_sina_realtime_for_codes(codes):
    """Independent Sina fallback through the shared DataFeed contract."""
    return sina_realtime_feed().fetch_realtime(codes)



def fetch_independent_realtime_for_codes(codes):
    """Resolve independent quotes through the ordered pluggable DataFeed chain."""
    return _independent_realtime_feed().fetch_realtime(codes)


def fetch_fund_flow_rank(topn=300):
    """个股主力净流入排行（实时）"""
    def _do():
        fields = "f12,f14,f2,f3,f62,f66,f72,f78,f84,f124,f184"
        result = _fetch_clist(
            fields, fid="f62", pages=max(1, (topn + 99) // 100), return_meta=True,
        )
        meta = result if isinstance(result, dict) else {"rows": [], "complete": False}
        if not meta.get("complete"):
            # A ranking built from a missing middle page is not a ranking of
            # the requested slice; do not silently expose it as factor input.
            return []
        raw = meta.get("rows") or []
        rows = []
        for d in raw:
            rows.append({
                "code": str(d.get("f12")), "name": d.get("f14"),
                "price": d.get("f2"), "pct": d.get("f3"),
                "main_net": d.get("f62"), "super_net": d.get("f66"),
                "big_net": d.get("f72"), "mid_net": d.get("f78"),
                "small_net": d.get("f84"), "main_pct": d.get("f184"),
                "quote_ts": d.get("f124"), "quote_at": _quote_at(d.get("f124")),
            })
        return rows
    return _cached(f"flow_rank_{topn}", 60, _do)


def _fetch_all_flow_map():
    """一次拉取全量资金流映射（约 5000+ 只），供 batch 接口 60s 内复用。

    资金流数据每分钟才变化，同一轮扫描里 4 个账户的补齐请求共享一次
    全量抓取即可，避免每账户重复拉 60 页（约 25-30 次请求）。
    """
    fields = "f12,f14,f2,f3,f62,f66,f72,f78,f84,f124,f184"
    global _flow_fetch_state
    result = _fetch_clist(fields, fid="f12", pages=60, return_meta=True)
    meta = result if isinstance(result, dict) else {"rows": [], "total": 0, "complete": False}
    raw = meta.get("rows") or []
    fetched_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    out = {}
    for d in raw:
        code = str(d.get("f12", ""))
        super_net = d.get("f66")
        if super_net is not None and isinstance(super_net, (int, float)):
            out[code] = {
                "super_net": super_net,
                "main_net": d.get("f62") if isinstance(d.get("f62"), (int, float)) else None,
                "big_net": d.get("f72") if isinstance(d.get("f72"), (int, float)) else None,
                "mid_net": d.get("f78") if isinstance(d.get("f78"), (int, float)) else None,
                "small_net": d.get("f84") if isinstance(d.get("f84"), (int, float)) else None,
                "main_pct": d.get("f184") if isinstance(d.get("f184"), (int, float)) else None,
                # 资金流与报价同属本轮东财全市场快照。明确保存来源和时点，
                # 让调用方可区分“资金流不可用”与“行情不可执行”。
                "quote_at": _quote_at(d.get("f124")),
                "flow_fetched_at": fetched_at,
                "flow_source": "eastmoney_full_market_flow",
            }
    total = int(meta.get("total") or 0)
    coverage_pct = round(len(out) / total * 100, 1) if total else 0.0
    complete = bool(meta.get("complete"))
    coverage_ok = bool(complete and total and coverage_pct >= FLOW_MIN_COVERAGE * 100)
    # 主机级可达性：承载资金流的主机整段不可达时，"没有资金数据"是
    # **机房级**事实，不是"这只票缺几个字段"。两者必须分开记。
    # meta 上没有该字段时（旧调用方 / 被 mock 替换），回退到模块级的
    # 最近一次可达性快照。
    reach = _clist_host_reachability(meta) or clist_host_reachability()
    host_unreachable = bool(reach.get("host_unreachable"))
    if host_unreachable:
        flow_status = MP.FLOW_SOURCE_HOST_UNREACHABLE
    elif out:
        flow_status = MP.FLOW_SOURCE_OK
    elif reach:
        # 主机可达但这批页没有资金行：符号级/数据级缺失。
        flow_status = MP.FLOW_SOURCE_SYMBOL_MISSING
    else:
        flow_status = MP.FLOW_SOURCE_UNKNOWN
    _flow_fetch_state = {
        "status": "fresh" if coverage_ok else "degraded",
        "total": total,
        "rows": len(out),
        "coverage_pct": coverage_pct,
        "complete": complete,
        "coverage_ok": coverage_ok,
        "fetched_at": fetched_at,
        "failed_pages": list(meta.get("failed_pages") or []),
        "pages_expected": int(meta.get("pages_expected") or 0),
        "pages_ok": int(meta.get("pages_ok") or 0),
        "source": "eastmoney_full_market_flow",
        "last_attempt_monotonic": time.monotonic(),
        "flow_source_status": flow_status,
        "host_unreachable": host_unreachable,
        "hosts_tried": list(reach.get("hosts_tried") or []),
        "host_outcomes": dict(reach.get("host_outcomes") or {}),
        "reason": _flow_unreachable_reason(reach),
    }
    # A partial flow ranking is actively misleading: callers would sort the
    # returned subset as if it were the whole market.  Retain diagnostics above
    # but fail closed for formal candidate scoring.
    return out if coverage_ok else {}


def get_flow_fetch_state():
    """Return the last full-market super-order fetch quality metadata.

    这里同时也是资金源状态的**只读**快照：``flow_source_status`` 区分
    ``host_unreachable``（承载资金流的主机整段不可达）与 ``symbol_missing``
    （主机可达，只是这只票缺字段）。调用方据此区分"这只票没资金数据"与
    "今天全市场都没资金数据"。不触网、不改变任何状态。
    """
    return dict(_flow_fetch_state)


def fetch_stock_flow_batch(codes):
    """批量补齐个股超大单资金数据。

    使用 clist 全量接口拉取（单次可覆盖全部 5000+ 只 A 股），
    从中筛选目标股票的资金流数据。全量抓取结果按 60s TTL 缓存，
    同一轮扫描内多个调用方共享，避免重复拉取。

    Args:
        codes: 股票代码列表

    Returns:
        dict: {code: {"super_net": float, "main_net": float, ...}}
    """
    if not codes:
        return {}
    target_set = {str(c) for c in codes}
    try:
        # ``_cached`` intentionally does not store empty values.  Keep a short
        # negative TTL for an incomplete page set so four strategies do not
        # immediately repeat the same 60-page failed request in one scan.
        last_attempt = float(_flow_fetch_state.get("last_attempt_monotonic") or 0.0)
        if (_flow_fetch_state.get("status") == "degraded"
                and time.monotonic() - last_attempt < 60.0):
            return {}
        all_flow = _cached("flow_all_map_60", 60, _fetch_all_flow_map)
        return {code: all_flow[code] for code in target_set if code in all_flow}
    except Exception:
        return {}



def enrich_live_flow_details(live_universe, missing_codes):
    """补齐全量候选的资金流，并保留时点/来源元数据。

    ``fetch_stock_flow_batch`` 的底层本来就是一次全市场拉取；旧实现把
    缺失代码截成前 100 个，只会造成后排候选长期没有超大单数据。这里不
    再截断，仍由 60 秒共享缓存保证四个策略不会重复请求。
    """
    if not missing_codes:
        return {}
    return fetch_stock_flow_batch(sorted({str(code) for code in missing_codes}))


# ---------- 4. 财务指标（东财数据中心 业绩报表） ----------
def fetch_finance_latest():
    """取最近一期已披露的业绩报表：ROE/营收同比/净利同比/EPS/BPS"""
    def _do():
        import datetime
        today = datetime.date.today()
        qdates = []
        for y in range(today.year, today.year - 2, -1):
            for md in ["12-31", "09-30", "06-30", "03-31"]:
                d = f"{y}-{md}"
                if d <= str(today):
                    qdates.append(d)
        qdates.sort(reverse=True)
        merged = {}
        used_dates = []
        for qd in qdates[:3]:  # 最近3期，新披露优先
            rows = _fetch_finance_report(qd)
            if rows:
                used_dates.append(qd)
                for r in rows:
                    code = r["code"]
                    if code not in merged:
                        merged[code] = r
            if len(merged) > 4000:
                break
        annual_dates = [f"{today.year - 1}-12-31", f"{today.year - 2}-12-31"]
        annual_used = None
        for annual_date in annual_dates:
            annual_rows = _fetch_finance_report(annual_date)
            if not annual_rows:
                continue
            annual_used = annual_date
            for row in annual_rows:
                code = row["code"]
                merged.setdefault(code, {})
                merged[code]["annual_net_profit"] = row.get("net_profit")
                merged[code]["annual_report_date"] = annual_date
            break
        if not merged:
            # A truthy metadata envelope must not cache an empty provider
            # response as a successful six-hour financial snapshot.
            return None
        return {
            "data": merged,
            "report_dates": used_dates,
            "annual_report_date": annual_used,
        }
    def _attempt():
        if time.monotonic() < _finance_fetch_state["retry_at"]:
            return None
        try:
            value = _do()
        except Exception:
            _finance_fetch_state["retry_at"] = time.monotonic() + 30
            raise
        if not value:
            _finance_fetch_state["retry_at"] = time.monotonic() + 30
        return value
    # Serializing the existing cache check also coalesces concurrent misses.
    # Empty attempts wait briefly, rather than either retrying per strategy
    # or becoming a successful six-hour cache entry.
    return MDC.persistent_cached(
        _mem_cache, _cache_lock, _finance_fetch_lock, "finance", FINANCE_CACHE_PATH,
        3600 * 6, _attempt,
        lambda value: isinstance(value, dict) and isinstance(value.get("data"), dict)
        and any(isinstance(row, dict) and row for row in value["data"].values()),
    ) or {"data": {}, "report_dates": [], "annual_report_date": None}

def _fetch_finance_report(report_date):
    rows = []
    page = 1
    while page <= 60:
        params = {
            "reportName": "RPT_LICO_FN_CPD",
            "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,WEIGHTAVG_ROE,YSTZ,SJLTZ,PARENT_NETPROFIT,BASIC_EPS,BPS,REPORTDATE,NOTICE_DATE",
            "filter": f"(REPORTDATE='{report_date}')",
            "pageNumber": page, "pageSize": 500,
            "sortColumns": "SECURITY_CODE", "sortTypes": 1,
        }
        try:
            j = _get_json("https://datacenter-web.eastmoney.com/api/data/v1/get", params)
        except Exception:
            break
        result = (j or {}).get("result") or {}
        data = result.get("data") or []
        if not data:
            break
        for d in data:
            notice_val = d.get("NOTICE_DATE")
            rows.append({
                "code": str(d.get("SECURITY_CODE")),
                "name": d.get("SECURITY_NAME_ABBR"),
                "roe": d.get("WEIGHTAVG_ROE"),
                "rev_yoy": d.get("YSTZ"), "profit_yoy": d.get("SJLTZ"),
                "net_profit": d.get("PARENT_NETPROFIT"),
                "eps": d.get("BASIC_EPS"), "bps": d.get("BPS"),
                "report_date": report_date,
                "notice_date": notice_val,
                "report_published_at": notice_val,
            })
        if page >= result.get("pages", 1):
            break
        page += 1
        time.sleep(0.1)
    return rows

# ---------- 5. 历史K线（前复权） ----------
def _secid(code):
    return MN.secid(code)


def _quote_at(value):
    return MN.quote_at(value)

KLINE_HOSTS = ["push2his.eastmoney.com", "92.push2his.eastmoney.com", "33.push2his.eastmoney.com"]
# The paper trader and the screener intentionally consume one authoritative
# local K-line cache.  Keep a version marker so a factor cache built from an
# older contract is invalidated instead of silently mixing data versions.
SHARED_KLINE_SOURCE_VERSION = "paper-kline-shared-v1"

def _kline_frame(rows):
    return MN.kline_frame(rows)


def _fetch_kline_tencent(code, beg, end, fqt=1, secid=None):
    """腾讯前复权日线兜底源。

    腾讯接口不返回成交额，使用典型价×成交量（手）×100 估算。这个近似不影响单票的
    量能倍数，但跨股票的绝对成交额筛选只能视作估计值。
    """
    symbol = None
    if code is not None:
        code = str(code)
        if code.startswith(("4", "8", "92")):
            symbol = "bj" + code
        else:
            symbol = ("sh" if code.startswith(("5", "6", "9")) else "sz") + code
    elif secid == "1.000300":
        symbol = "sh000300"
    if not symbol:
        return pd.DataFrame()
    with _tencent_circuit_lock:
        now = time.time()
        # 熔断只暂停并发请求，不永久屏蔽腾讯源；每 20 秒允许一次半开探测。
        if now < _tencent_circuit["open_until"]:
            if now - _tencent_circuit["last_probe_at"] < 20:
                return pd.DataFrame()
            _tencent_circuit["last_probe_at"] = now

    import datetime

    start_date = datetime.datetime.strptime(str(beg), "%Y%m%d").date()
    requested_end = datetime.datetime.strptime(str(end), "%Y%m%d").date()
    end_date = min(requested_end, datetime.date.today())
    adjust = "qfq" if fqt == 1 else ""
    # 单次最多约 640 根，向前分页，确保 3/5/10 年请求不会悄悄缺头部数据。
    raw_by_date = {}
    # 腾讯接口偶发忽略 qfq 参数而只返回 ``day``。此前无论实际字段都
    # 标成 qfq，会把未复权数据混进共享 K 线缓存。这里宁可降级为 raw，
    # 让上层改走东财或锚定兜底，也不伪造复权口径。
    used_qfq_rows = True
    cursor_end = end_date
    for _page in range(12):
        param = f"{symbol},day,{start_date.isoformat()},{cursor_end.isoformat()},640,{adjust}"
        payload = _get_json(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            {"param": param},
            timeout=8,
            retries=0,
        ) or {}
        node = (payload.get("data") or {}).get(symbol) or {}
        qfq_rows = node.get("qfqday") or []
        page_rows = qfq_rows or node.get("day") or []
        if not qfq_rows:
            used_qfq_rows = False
        if not page_rows:
            with _tencent_circuit_lock:
                _tencent_circuit["failures"] += 1
                if _tencent_circuit["failures"] >= 5:
                    _tencent_circuit["backoff_seconds"] = min(_tencent_circuit.get("backoff_seconds", 45) * 2, 600)
                    _tencent_circuit["open_until"] = time.time() + _tencent_circuit["backoff_seconds"]
            break
        with _tencent_circuit_lock:
            _tencent_circuit["failures"] = 0
            _tencent_circuit["open_until"] = 0.0
            _tencent_circuit["backoff_seconds"] = 45
            _tencent_circuit["last_probe_at"] = 0.0
        for item in page_rows:
            raw_by_date[item[0]] = item
        earliest = datetime.date.fromisoformat(page_rows[0][0])
        if len(page_rows) < 640 or earliest <= start_date:
            break
        cursor_end = earliest - datetime.timedelta(days=1)
    raw = [raw_by_date[key] for key in sorted(raw_by_date)]
    rows = MP.parse_tencent_kline_rows(raw)
    frame = _kline_frame(rows)
    frame.attrs.update({
        "source": "tencent" if (fqt != 1 or used_qfq_rows) else "tencent_raw",
        "adjustment": "qfq" if (fqt == 1 and used_qfq_rows) else "none",
    })
    return frame


def _fetch_kline_sina(code, beg, end):
    """A 股历史兜底。新浪源为不复权日线，仅在腾讯限流/历史不足时使用。"""
    code = str(code)
    if code.startswith(("4", "8", "92")):
        symbol = "bj" + code
    elif code.startswith(("5", "6", "9")):
        symbol = "sh" + code
    else:
        symbol = "sz" + code
    payload = _get_json(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData",
        {"symbol": symbol, "scale": 240, "ma": "no", "datalen": 1023},
        timeout=8,
        retries=0,
    ) or []
    rows = MP.parse_sina_kline_rows(payload, beg, end)
    frame = _kline_frame(rows)
    frame.attrs.update({"source": "sina", "adjustment": "none"})
    return frame


def _fetch_kline_eastmoney(code, beg, end, klt, fqt, secid):
    params = {
        "secid": secid or _secid(code), "klt": klt, "fqt": fqt,
        "beg": beg, "end": end,
        "fields1": "f1,f2,f3", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
    }
    # 多镜像轮换 + 空响应重试（push2delay 不提供K线，勿加入）
    klines = []
    for attempt in range(2):
        for host in KLINE_HOSTS:
            try:
                j = _get_json(
                    f"https://{host}/api/qt/stock/kline/get",
                    params,
                    timeout=5,
                    retries=0,
                )
                klines = ((j or {}).get("data") or {}).get("klines") or []
            except Exception:
                klines = []
            if klines:
                break
        if klines:
            break
        time.sleep(0.4)
    rows = MP.parse_eastmoney_kline_rows(klines)
    frame = _kline_frame(rows)
    frame.attrs.update({"source": "eastmoney", "adjustment": "qfq" if fqt == 1 else "none"})
    return frame


def fetch_kline(code, beg="20230101", end="20500101", klt=101, fqt=1, secid=None):
    """历史 K 线：A股/沪深300优先腾讯、再用东财前复权，新浪仅作末级兜底。"""    
    if klt == 101 and (code is not None or secid == "1.000300"):
        frame = pd.DataFrame()
        # 腾讯在盘后/限流时可能返回非空但停留在前一交易日的结果。
        # 这种结果不能阻止东财备用源；调用方传入明确 end 时，必须优先
        # 选择达到该截止日的结果，否则增量刷新会把“旧日期”误报成成功。
        try:
            requested_end = pd.Timestamp(str(end)[:10]).date()
            if requested_end > dt.date.today() + dt.timedelta(days=1):
                requested_end = None
        except (TypeError, ValueError):
            requested_end = None

        def _last_date(value):
            try:
                if value is None or value.empty:
                    return None
                return pd.to_datetime(value.index, errors="coerce").max().date()
            except (AttributeError, TypeError, ValueError):
                return None

        try:
            frame = _fetch_kline_tencent(code, beg, end, fqt=fqt, secid=secid)  
        except (requests.RequestException, ValueError, KeyError, TypeError, json.JSONDecodeError):
            frame = pd.DataFrame()
        # 增量更新常常只返回最近 1-2 根，非空就是有效结果，不能因不足 60 根
        # 而用不复权源覆盖原有前复权历史；但若最新日期早于明确截止日，
        # 必须切换东财，避免“旧日期非空响应”阻塞备用源。
        if frame is None or frame.empty or (
            requested_end is not None
            and _last_date(frame) is not None
            and _last_date(frame) < requested_end
        ):
            try:
                secondary = _fetch_kline_eastmoney(code, beg, end, klt, fqt, secid)
                if secondary is not None and not secondary.empty:
                    # 备用源只有在日期更完整时替换；如果两源都旧，保留
                    # 腾讯结果让上层 required_date 校验统一判定失败。
                    if frame is None or frame.empty or (
                        _last_date(secondary) is not None
                        and (_last_date(frame) is None or _last_date(secondary) > _last_date(frame))
                    ):
                        frame = secondary
                if requested_end is not None and _last_date(frame) is not None and _last_date(frame) < requested_end:
                    # 两个前复权源都返回了旧日期时，不要把旧腾讯结果继续
                    # 当作“非空成功”；交给末级新浪源返回当日原始K线，
                    # 上层会决定是否可做前复权连续化或仅影子使用。
                    frame = pd.DataFrame()
            except (requests.RequestException, ValueError, KeyError, TypeError, json.JSONDecodeError):
                if requested_end is not None and _last_date(frame) is not None and _last_date(frame) < requested_end:
                    frame = pd.DataFrame()
        if code is not None and (frame is None or frame.empty):
            try:
                frame = _fetch_kline_sina(code, beg, end)
            except (requests.RequestException, ValueError, KeyError, TypeError, json.JSONDecodeError):
                frame = pd.DataFrame()
        if frame is not None and not frame.empty:
            return frame
        return pd.DataFrame()
    return _fetch_kline_eastmoney(code, beg, end, klt, fqt, secid)

def kline_cache_path(code):
    return os.path.join(KLINE_DIR, f"{code}.csv")

def _kline_bar_dates(index):
    """把日线索引解析成逐行的自然日；无法解析的行返回 None。

    返回 None 表示整段索引都不可用（例如混合时区字符串索引），此时调用方
    必须 fail-closed，绝不能退化成"返回原始未截断数据"。

    自然日取索引自身的**本地日期**（缓存 CSV 写出的就是本地日期），因此
    不用 UTC 归一化，避免带时区索引被挪到前一天。
    """
    if isinstance(index, pd.DatetimeIndex):
        values = list(index.date)
    else:
        values = list(index)
    dates = []
    for value in values:
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            dates.append(None)
            continue
        if isinstance(value, dt.datetime):
            dates.append(value.date())
            continue
        if isinstance(value, dt.date):
            dates.append(value)
            continue
        text = str(value).strip()
        if not text:
            dates.append(None)
            continue
        try:
            dates.append(dt.date.fromisoformat(text[:10]))
            continue
        except ValueError:
            pass
        try:
            parsed = pd.to_datetime(text, errors="raise")
        except (TypeError, ValueError, OverflowError):
            dates.append(None)
            continue
        try:
            dates.append(parsed.date() if hasattr(parsed, "date") else None)
        except (TypeError, ValueError):
            dates.append(None)
    return dates


def completed_kline(code, cutoff_date, *, inclusive=True):
    """K 线 owner：只返回截止指定完整交易日的日线。

    盘中"当日 K 线"是半根，禁止被当成完整收盘数据使用；这个截断语义原来
    只存在于交易引擎内部，下层模块必须能自己取到同一份事实。

    截断失败的历史语义是 fail-closed：宁可返回空（=没有可用日线证据），
    也绝不把截止日之后的原始行交给决策使用。日期无法解析的行会被丢弃，
    因为"无法确认是否越界"等同于越界风险。
    """
    frame = load_cached_kline(str(code))
    if frame is None or frame.empty:
        return frame
    cutoff = dt.date.fromisoformat(str(cutoff_date)[:10])
    dates = _kline_bar_dates(frame.index)
    mask = [
        value is not None and (value <= cutoff if inclusive else value < cutoff)
        for value in dates
    ]
    truncated = frame.loc[mask].copy()
    truncated.attrs.update(dict(getattr(frame, "attrs", {}) or {}))
    return truncated


def load_cached_kline(code):
    p = kline_cache_path(code)
    if os.path.exists(p):
        try:
            df = pd.read_csv(p, index_col=0, parse_dates=True)
            required = {"open", "close", "high", "low", "volume", "amount"}
            if not required.issubset(df.columns):
                return None
            df.index = pd.to_datetime(df.index, errors="coerce")
            df = df[~df.index.isna()]
            df = df[~df.index.duplicated(keep="last")].sort_index()
            # CSV 不保存 DataFrame.attrs；恢复清单中的来源/复权口径，
            # 否则跨进程增量修复会把可靠前复权缓存降级成 unknown。
            with _manifest_lock:
                meta = dict(_load_kline_manifest_unlocked().get(str(code), {}) or {})
            if isinstance(meta, dict):
                df.attrs.update({
                    "source": meta.get("source", "unknown"),
                    "adjustment": meta.get("adjustment", "unknown"),
                })
            return df
        except Exception:
            return None
    return None


def load_shared_kline(code):
    """Read the same cached, adjusted daily K-line used by paper trading.

    This small named boundary prevents future features from introducing a
    second selection-only cache.  The function deliberately does not fetch
    from the network; refresh_history() remains the single writer and paper
    trading/selection both read the resulting files.
    """
    return load_cached_kline(code)


@contextmanager
def _kline_file_lock(code):
    """Serialize writers for one code across API/data-worker processes."""
    lock_handle = None
    lock_path = f"{kline_cache_path(code)}.lock"
    try:
        lock_handle = open(lock_path, "a+", encoding="utf-8")
        if _fcntl is not None:
            _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_EX)
        yield
    finally:
        if lock_handle is not None:
            try:
                if _fcntl is not None:
                    _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_UN)
            except OSError:
                pass
            lock_handle.close()

def save_kline(code, df):
    global _manifest_dirty, _manifest_pending
    path = kline_cache_path(code)
    with _kline_file_lock(code):
        tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        df.to_csv(tmp, encoding="utf-8")
        os.replace(tmp, path)
    valid = df.dropna(subset=["close"]) if "close" in df else df
    meta = {
        "rows": int(len(valid)),
        "first_date": str(valid.index[0].date()) if len(valid) else None,
        "last_date": str(valid.index[-1].date()) if len(valid) else None,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": df.attrs.get("source", "unknown"),
        "adjustment": df.attrs.get("adjustment", "unknown"),
    }
    with _manifest_lock:
        manifest = _load_kline_manifest_unlocked()
        manifest[str(code)] = meta
        _manifest_pending[str(code)] = meta
        _manifest_dirty += 1
        if _manifest_dirty >= 50:
            _flush_kline_manifest_unlocked()


def load_kline_manifest():
    """公开读取 K 线清单（带内存缓存，mtime 失效）。供 paper_trading 复用，
    避免各调用方各自重复读盘 893KB 的 kline_manifest.json。"""
    with _manifest_lock:
        return _load_kline_manifest_unlocked()


def _load_kline_manifest_unlocked():
    global _manifest, _manifest_mtime
    try:
        stat = os.stat(KLINE_MANIFEST_PATH)
        file_mtime = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        file_mtime = None
    # The API process stays alive while the scheduled refresh writes the
    # shared manifest from another process/container invocation.  Previously
    # the first in-memory read lived forever, so health and selection kept
    # reporting the old 0.5% fresh coverage until a restart.
    if _manifest is None or file_mtime != _manifest_mtime:
        try:
            with open(KLINE_MANIFEST_PATH, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            loaded = payload.get("stocks", {})
            _manifest = loaded if isinstance(loaded, dict) else {}
        except FileNotFoundError:
            _manifest = {}
        except (OSError, ValueError, TypeError):
            # Do not discard in-process pending updates when another process
            # briefly exposes malformed JSON during recovery.
            if _manifest is None:
                _manifest = {}
        _manifest.update(_manifest_pending)
        _manifest_mtime = file_mtime
    return _manifest


def _read_manifest_disk():
    """Read the newest on-disk manifest; ``None`` means malformed JSON."""
    try:
        with open(KLINE_MANIFEST_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        stocks = payload.get("stocks", {}) if isinstance(payload, dict) else {}
        return dict(stocks) if isinstance(stocks, dict) else None
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        return None


@contextmanager
def _manifest_file_lock():
    """Serialize manifest read/merge/write across API and data-worker processes."""
    lock_handle = None
    try:
        lock_handle = open(f"{KLINE_MANIFEST_PATH}.lock", "a+", encoding="utf-8")
        if _fcntl is not None:
            _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_EX)
        yield
    finally:
        if lock_handle is not None:
            try:
                if _fcntl is not None:
                    _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_UN)
            finally:
                lock_handle.close()


def _flush_kline_manifest_unlocked():
    global _manifest, _manifest_dirty, _manifest_mtime, _manifest_pending
    if _manifest is None and not _manifest_pending:
        return
    # Read the latest file under a process-shared lock, merge only this
    # process's pending entries, and publish via an atomic unique temp file.
    # Merging a stale in-memory full copy would resurrect old values from a
    # competing writer, so pending entries are tracked separately.
    with _manifest_file_lock():
        latest = _read_manifest_disk()
        if latest is None:
            # Never overwrite a malformed manifest while another writer may be
            # recovering it; the CSV files remain the source of truth and the
            # next reconciliation pass can rebuild metadata safely.
            return
        latest.update(_manifest_pending)
        tmp = f"{KLINE_MANIFEST_PATH}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "stocks": latest,
                },
                handle,
                ensure_ascii=False,
            )
        os.replace(tmp, KLINE_MANIFEST_PATH)
        _manifest = latest
        _manifest_pending = {}
        _manifest_dirty = 0
        try:
            stat = os.stat(KLINE_MANIFEST_PATH)
            _manifest_mtime = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            _manifest_mtime = None


def flush_kline_manifest():
    """Force persisted metadata after a batch download."""
    with _manifest_lock:
        _flush_kline_manifest_unlocked()


def get_kline_manifest():
    with _manifest_lock:
        return dict(_load_kline_manifest_unlocked())


def rebuild_kline_manifest():
    """Reconcile CSV metadata, reading only new or changed files."""
    global _manifest, _manifest_mtime, _manifest_dirty, _manifest_pending
    existing = get_kline_manifest()
    rebuilt = {}
    for filename in os.listdir(KLINE_DIR):
        if not filename.endswith(".csv"):
            continue
        code = filename[:-4]
        path = kline_cache_path(code)
        try:
            file_stat = os.stat(path)
        except OSError:
            continue
        prior = existing.get(code, {})
        # Legacy entries have no file signature and must be read once.  Do not
        # trust an incomplete entry merely because its file signature matches.
        if (
            isinstance(prior, dict)
            and {"rows", "first_date", "last_date", "source", "adjustment"}.issubset(prior)
            and prior.get("file_mtime_ns") == file_stat.st_mtime_ns
            and prior.get("file_size") == file_stat.st_size
        ):
            continue
        frame = load_cached_kline(code)
        if frame is None:
            continue
        try:
            after_stat = os.stat(path)
        except OSError:
            continue
        # A concurrent atomic replacement must not certify metadata read from
        # a different file version.  Retry it on the next reconciliation.
        if (file_stat.st_mtime_ns, file_stat.st_size) != (after_stat.st_mtime_ns, after_stat.st_size):
            continue
        valid = frame.dropna(subset=["close"])
        rebuilt[code] = {
            "rows": int(len(valid)),
            "first_date": str(valid.index[0].date()) if len(valid) else None,
            "last_date": str(valid.index[-1].date()) if len(valid) else None,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": prior.get("source", "unknown"),
            "adjustment": prior.get("adjustment", "unknown"),
            "file_mtime_ns": after_stat.st_mtime_ns,
            "file_size": after_stat.st_size,
        }
    with _manifest_lock:
        manifest = _load_kline_manifest_unlocked()
        # Prefer metadata published by another writer during the CSV scan.
        rebuilt = {code: meta for code, meta in rebuilt.items() if manifest.get(code) == existing.get(code)}
        manifest.update(rebuilt)
        _manifest = manifest
        _manifest_pending.update(rebuilt)
        _manifest_dirty += len(rebuilt)
        if _manifest_pending:
            _flush_kline_manifest_unlocked()
    return manifest

# ---------- 6. 海外市场历史（用于风险门控与回测择时） ----------
OVERSEAS_SECIDS = {
    "DJIA": ("100.DJIA", "道琼斯"), "NDX": ("100.NDX", "纳斯达克100"),
    "SPX": ("100.SPX", "标普500"), "HSI": ("100.HSI", "恒生指数"),
    "USDIDX": ("100.UDI", "美元指数"),
}

def fetch_overseas_history(beg="20230101"):
    def _do():
        out = {}
        def _one(item):
            key, (secid, name) = item
            try:
                df = fetch_kline(None, beg=beg, secid=secid)
                return key, {"name": name, "df": df} if (df is not None and not df.empty) else None
            except Exception:
                return key, None

        # 5 个境外指数各自独立抓取，没有依赖顺序。串行 + 150 毫秒限速在
        # 单线程下把 5 次 K 线抓取排成一串，整段累积到 6+ 秒。改成线程池
        # 后只付一次 TCP/TLS 握手的最慢者，限速 sleep 也不再需要：不同
        # 主机/不同 secid 互不争用同一个令牌桶。
        items = list(OVERSEAS_SECIDS.items())
        with ThreadPoolExecutor(max_workers=min(5, len(items) or 1)) as pool:
            results = pool.map(_one, items)
        for key, value in results:
            if value is not None:
                out[key] = value
        return out
    return _cached("overseas", 1800, _do)

# ---------- 7. 舆情：7x24快讯 + 人气榜 ----------
def fetch_fast_news(page_size=50):
    def _do():
        params = {
            "client": "web", "biz": "web_724", "fastColumn": "102",
            "sortEnd": "", "pageSize": page_size, "req_trace": int(time.time()),
        }
        j = _get_json("https://np-listapi.eastmoney.com/comm/web/getFastNewsList", params)
        items = (j or {}).get("data", {}).get("fastNewsList", []) or []
        output = []
        for item in items:
            article_code = str(item.get("code") or "").strip()
            stocks = []
            for raw in item.get("stockList") or []:
                code = str(raw or "").split(".")[-1]
                if len(code) == 6 and code.isdigit():
                    stocks.append(code)
            output.append({
                "time": item.get("showTime"),
                "title": item.get("title") or "",
                "summary": item.get("summary") or item.get("title") or "",
                "source": "东方财富7x24",
                "source_url": f"https://finance.eastmoney.com/a/{article_code}.html" if article_code else None,
                "article_id": article_code or None,
                "stock_codes": sorted(set(stocks)),
                "source_type": "news_aggregator",
                "evidence_grade": "C",
            })
        return output
    return _persistent_list("news", NEWS_CACHE_PATH, NEWS_CACHE_TTL_SECONDS, _do, _news_fetch_lock)


def fetch_company_announcements(codes, page_size=100):
    """东方财富聚合的上市公司公告：作为可追溯事件源，不直接产生交易。"""
    normalized = sorted({str(code).zfill(6) for code in (codes or []) if str(code).isdigit()})
    if not normalized:
        return []

    def _do():
        params = {
            "sr": "-1", "page_size": min(max(int(page_size), 20), 100),
            "page_index": 1, "ann_type": "A", "client_source": "web",
            "stock_list": ",".join(normalized),
        }
        try:
            payload = _get_json("https://np-anotice-stock.eastmoney.com/api/security/ann", params, timeout=12, retries=1) or {}
        except Exception:
            return []
        rows = (payload.get("data") or {}).get("list") or []
        output = []
        for row in rows:
            codes_in_row = row.get("codes") or []
            matched = next(
                (str(item.get("stock_code") or item.get("code") or "").zfill(6)
                 for item in codes_in_row
                 if str(item.get("stock_code") or item.get("code") or "").zfill(6) in normalized),
                None,
            )
            if not matched:
                matched = str(row.get("stock_code") or "").zfill(6)
            if matched not in normalized:
                continue
            output.append({
                "code": matched,
                "name": row.get("stock_name"),
                "time": row.get("display_time") or row.get("notice_date"),
                "summary": row.get("title") or "",
                "source": "上市公司公告（东方财富聚合）",
                "category": row.get("column_name") or row.get("notice_type"),
                "article_id": row.get("art_code"),
                "verified": True,
                "source_url": (
                    f"https://data.eastmoney.com/notices/detail/{matched}/{row.get('art_code')}.html"
                    if row.get("art_code") else None
                ),
                "source_type": "announcement_aggregator",
                "evidence_grade": "B",
            })
        return output

    key = "announcements:" + ",".join(normalized)
    os.makedirs(ANNOUNCEMENT_CACHE_DIR, exist_ok=True)
    path = os.path.join(
        ANNOUNCEMENT_CACHE_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json")
    return _persistent_list(key, path, ANNOUNCEMENT_CACHE_TTL_SECONDS, _do, _announcement_fetch_lock)


def fetch_hot_rank(topn=100):
    def _do():
        body = {
            "appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
            "marketType": "", "pageNo": 1, "pageSize": topn,
        }
        j = http_post_json("https://emappdata.eastmoney.com/stockrank/getAllCurrentList", body, timeout=10)
        out = []
        for d in j.get("data") or []:
            sc = d.get("sc", "")
            out.append({"code": sc[2:], "market": sc[:2], "rank": d.get("rk"), "rank_chg": d.get("hisRc")})
        return out
    return _cached(f"hot_{topn}", 300, _do)

# ---------------------------------------------------------------------------
# 兼容别名：模块内部已全部改用公开名；下面这些只为旧调用方/旧测试保留。
_eastmoney_realtime_feed = eastmoney_realtime_feed
_fetch_sina_realtime_for_codes = fetch_sina_realtime_for_codes
_full_snapshot_payload_is_complete = full_snapshot_payload_is_complete
_sina_realtime_feed = sina_realtime_feed
_tencent_realtime_feed = tencent_realtime_feed
