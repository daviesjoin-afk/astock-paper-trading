# -*- coding: utf-8 -*-
"""数据层：东方财富 + 腾讯免费公开接口，全部实测验证于 2026-07-28
传输层：requests + Session 连接复用，大幅降低进程开销"""
import datetime as dt
import os, json, time, threading, uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pandas as pd
import marketdata_cache as MDC
import marketdata_normalizers as MN
import marketdata_providers as MP
from marketdata_transport import (
    HEADERS,
    _session,
    _session_local,
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
CACHE_DIR = os.path.join(BASE_DIR, "data_cache")
KLINE_DIR = os.path.join(CACHE_DIR, "klines")
KLINE_MANIFEST_PATH = os.path.join(CACHE_DIR, "kline_manifest.json")
# Full-market and risk-proxy snapshots must never overwrite each other.  The
# risk page intentionally samples 20 pages for a fast read; that sample is not
# a valid fallback for the full-market selector or the 5-minute scanner.
MARKET_SNAPSHOT_CACHE_PATH = os.path.join(CACHE_DIR, "market_snapshot.json")
MARKET_SNAPSHOT_FULL_CACHE_PATH = os.path.join(CACHE_DIR, "market_snapshot_full.json")
MARKET_SNAPSHOT_SAMPLE_CACHE_PATH = os.path.join(CACHE_DIR, "market_snapshot_sample_20.json")
MARKET_SNAPSHOT_FULL_LOCK_PATH = MARKET_SNAPSHOT_FULL_CACHE_PATH + ".lock"
DATA_SOURCE_HEALTH_PATH = os.path.join(CACHE_DIR, "data_source_health.json")
os.makedirs(KLINE_DIR, exist_ok=True)

UT_FLOW = "b2884a393a59ad64002292a3e90d46a5"
_cache_lock = threading.Lock()
_mem_cache = {}
_full_snapshot_thread_lock = threading.Lock()
_manifest_lock = threading.Lock()
_manifest = None
_manifest_mtime = None
_manifest_dirty = 0
_manifest_pending = {}


def _cached(key, ttl, fn):
    return MDC.cached(_mem_cache, _cache_lock, key, ttl, fn)


@contextmanager
def _full_snapshot_singleflight_lock():
    """Serialize full-market refreshes across threads and worker processes.

    The in-memory cache is process-local, while API/cron/data-worker processes
    share the same disk cache.  A file lock prevents every process from
    launching its own 25-page refresh after the same TTL boundary.  Windows
    development has no ``fcntl``; the process-local lock still protects its
    threads and the lock file remains harmless.
    """
    with MDC.full_snapshot_singleflight_lock(
        _full_snapshot_thread_lock, MARKET_SNAPSHOT_FULL_LOCK_PATH
    ):
        yield

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
    """Probe primary/independent quote sources and reconnect on failure.

    This is intentionally small (one market page + two index quotes), so it
    can run before every five-minute paper scan.  An empty/invalid response
    never enters the cache; the session is closed and both source families are
    retried once with host rotation before the caller proceeds.
    """
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
            rows = _fetch_clist(fields, fid="f20", pages=1, pz=100)
            valid = [row for row in rows if str(row.get("f12") or "").isdigit()
                     and isinstance(row.get("f2"), (int, float)) and row.get("f2") > 0]
            return {
                "status": "fresh" if len(valid) >= 20 else "degraded",
                "rows": len(rows), "valid_rows": len(valid),
                "coverage_pct": round(len(valid) / max(len(rows), 1) * 100, 1),
                "source": "eastmoney-clist",
            }
        except Exception as exc:
            return {"status": "failed", "rows": 0, "valid_rows": 0,
                    "coverage_pct": 0.0, "source": "eastmoney-clist",
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
                    if price > 0:
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
            rows = fetch_independent_realtime_for_codes(["300214", "688129", "920006"])
            sources = sorted({str(row.get("source") or "") for row in rows if row.get("source")})
            return {
                "status": "fresh" if len(rows) >= 2 else "degraded",
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
            reset_data_source("5次连续数据源探测失败，主动重连并切换数据源", reset_circuit=True)
            reconnected = True
            time.sleep(0.35)
        eastmoney = _probe_eastmoney()
        tencent = _probe_tencent()
        independent_stock = _probe_independent_stock()
        attempts.append({"attempt": attempt + 1, "eastmoney": eastmoney, "tencent": tencent,
                          "independent_stock": independent_stock})
        if eastmoney["status"] == "fresh" and independent_stock["status"] == "fresh":
            break
    eastmoney = attempts[-1]["eastmoney"] if attempts else {"status": "failed"}
    tencent = attempts[-1]["tencent"] if attempts else {"status": "failed"}
    independent_stock = attempts[-1].get("independent_stock", {"status": "failed"}) if attempts else {"status": "failed"}
    healthy = eastmoney.get("status") == "fresh" and independent_stock.get("status") == "fresh"
    payload = {
        "checked_at": checked_at,
        "healthy": healthy,
        "reconnected": reconnected,
        "attempts": len(attempts),
        "eastmoney": eastmoney,
        "tencent": tencent,
        "independent_stock": independent_stock,
        "host_rotation": "东方财富主机轮换 + 腾讯个股源重试 + 新浪个股备用",
        "action": "正常使用" if healthy else ("已重连并降级使用可用源" if reconnected else "等待下一轮自动重连"),
    }
    _save_source_health(payload)
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
        diff = []
        for attempt in range(2):
            for host in ["push2delay.eastmoney.com", "push2.eastmoney.com", "82.push2.eastmoney.com"]:
                try:
                    j = _get_json(f"https://{host}/api/qt/clist/get", params)
                    diff = (j or {}).get("data", {}).get("diff", []) or []
                except Exception:
                    diff = []
                if diff:
                    break
            if diff:
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


def _fetch_concept_members(board_code, max_pages=8):
    """Fetch all currently reported members, with explicit completeness data."""
    fields = "f2,f3,f8,f10,f12,f14,f62,f66,f100,f124,f184"

    def one_page(page):
        params = {
            "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "ut": UT_FLOW, "fid": "f62", "fs": f"b:{board_code}+f:!50",
            "fields": fields,
        }
        for host in CLIST_HOSTS:
            try:
                payload = _get_json(f"https://{host}/api/qt/clist/get", params,
                                    timeout=5, retries=0)
                data = (payload or {}).get("data") or {}
                rows = data.get("diff") or []
                if rows:
                    return rows, int(data.get("total") or len(rows)), True
            except Exception:
                continue
        return [], 0, False

    first, total, ok = one_page(1)
    if not ok:
        return {"members": [], "expected_count": 0, "pages_ok": 0,
                "pages_expected": 0, "complete": False}
    pages_expected = max(1, (total + 99) // 100)
    pages_to_fetch = min(pages_expected, max(1, int(max_pages)))
    results = {1: first}
    if pages_to_fetch > 1:
        with ThreadPoolExecutor(max_workers=min(4, pages_to_fetch - 1)) as pool:
            futures = {pool.submit(one_page, page): page for page in range(2, pages_to_fetch + 1)}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    rows, _, page_ok = future.result()
                except Exception:
                    rows, page_ok = [], False
                if page_ok:
                    results[page] = rows
    members, seen, malformed = [], set(), 0
    for page in sorted(results):
        for item in results[page]:
            code = str(item.get("f12") or "")
            price, pct = _finite_number(item.get("f2")), _finite_number(item.get("f3"))
            if not code or code in seen or price is None or pct is None:
                malformed += 1
                continue
            seen.add(code)
            members.append({
                "code": code, "name": item.get("f14"), "price": price, "pct": pct,
                "turnover": _finite_number(item.get("f8")),
                "vol_ratio": _finite_number(item.get("f10")), "industry": item.get("f100"),
                "main_net": _finite_number(item.get("f62")),
                "super_net": _finite_number(item.get("f66")),
                "main_pct": _finite_number(item.get("f184")),
                "quote_ts": item.get("f124"), "quote_at": _quote_at(item.get("f124")),
            })
    complete = pages_expected <= max_pages and len(results) == pages_expected and len(members) >= max(1, total - malformed)
    return {"members": members, "expected_count": total, "fetched_count": len(members),
            "pages_ok": len(results), "pages_expected": pages_expected,
            "malformed_rows": malformed, "complete": bool(complete)}


def fetch_hot_concept_snapshot(topn=6):
    """Funded concepts and complete constituents; incomplete boards stay out."""
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
            members = result.get("members") or []
            if not result.get("complete") or not members:
                continue
            positive = sum(1 for member in members if member["pct"] > 0)
            output.append({**board, **{k: v for k, v in result.items() if k != "members"},
                           "rank": rank, "members": members, "member_count": len(members),
                           "positive_ratio": round(positive / len(members), 4),
                           "source": "eastmoney_concept_flow+complete_constituents"})
        return output
    return _cached(f"hot_concept_snapshot_{topn}", 75, _do)


def _stock_secid(code):
    return MN.stock_secid(code)


def _fetch_stock_concept_refs(code):
    """Fetch concept-board references for one stock, without inferring tags.

    ``slist`` is the inverse of the usual board->constituent endpoint.  It is
    deliberately used only for a very small set of strong leaders; doing this
    for the whole market would be both slow and prone to provider throttling.
    """
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
            if isinstance(rows, dict):
                rows = rows.values()
            refs = []
            seen = set()
            for row in rows:
                board_code = str(row.get("f12") or "")
                name = str(row.get("f14") or "").strip()
                if (not board_code.startswith("BK") or not name or board_code in seen
                        or any(token in name for token in _NON_THEME_CONCEPT_TOKENS)):
                    continue
                seen.add(board_code)
                refs.append({"code": board_code, "name": name})
            if refs:
                return refs
        except Exception:
            continue
    return []


def fetch_leader_concept_snapshot(leaders, max_leaders=3):
    """Map a few genuine intraday leaders back to complete concept boards.

    This closes the missing ``leader -> concept -> peer`` link.  A leader
    never becomes an automatically tradable candidate: the returned board is
    only discovery evidence for *other* constituents, which still require a
    fresh full-market quote, positive flow, volume and all execution gates.
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
            members = result.get("members") or []
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
_CLIST_HOST_COOLDOWN_BASE = 30
_CLIST_HOST_MAX_COOLDOWN = 300
FULL_MARKET_MIN_ROWS = 4000
FLOW_MIN_COVERAGE = 0.90
_flow_fetch_state = {
    "status": "unknown", "total": 0, "rows": 0, "coverage_pct": 0.0,
    "complete": False, "fetched_at": None, "failed_pages": [],
    "last_attempt_monotonic": 0.0,
}
_hot_sector_fetch_state = {
    "status": "unknown", "shadow_only": True, "rows": 0,
    "pages_expected": 0, "pages_ok": 0, "failed_pages": [],
    "fetched_at": None,
}


def _sanitize_market_row(row):
    return MN.sanitize_market_row(row)

def _fetch_clist(fields, fid="f20", pages=None, pz=200, return_meta=False):
    """分页拉取 clist，并可返回分页完整性元数据。

    ``clist`` 会在单页失败时返回一个看似合法的残片。历史调用方仍默认
    得到 ``list`` 以保持兼容；需要将结果写入正式全市场/资金快照的调用方
    必须传 ``return_meta=True``，并检查 ``complete``，避免残片污染横截面。
    """
    def _host_ok(h):
        info = _clist_host_health.get(h)
        return not (info and time.time() < info.get("cooldown_until", 0))
    def _host_ok_set(h):
        _clist_host_health[h] = {"failures": 0, "cooldown_until": 0}
    def _host_fail(h):
        info = _clist_host_health.get(h, {"failures": 0, "cooldown_until": 0})
        f = info["failures"] + 1
        _clist_host_health[h] = {"failures": f, "cooldown_until": time.time() + min(_CLIST_HOST_COOLDOWN_BASE * (2 ** (f - 1)), _CLIST_HOST_MAX_COOLDOWN)}
    def _one_page(pn):
        params = {"pn": pn, "pz": pz, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                  "ut": UT_FLOW, "fid": fid, "fs": SNAP_FS, "fields": fields}
        ordered = [h for h in CLIST_HOSTS if _host_ok(h)] + [h for h in CLIST_HOSTS if not _host_ok(h)]
        for host in ordered:
            try:
                j = _get_json(f"https://{host}/api/qt/clist/get", params, timeout=10, retries=1)
                data = (j or {}).get("data") or {}
                diff = data.get("diff") or []
                total = data.get("total", 0)
            except Exception:
                diff, total = [], 0
            if diff:
                _host_ok_set(host)
                return diff, total
            _host_fail(host)
        return [], 0
    first, total = _one_page(1)
    try:
        total = int(total or 0)
    except (TypeError, ValueError):
        total = 0
    if not first:
        result = {
            "rows": [], "total": total, "pages_expected": 0,
            "pages_ok": 0, "failed_pages": [1], "complete": False,
        }
        return result if return_meta else []
    rows = list(first)
    # Some hosts (e.g. push2delay) cap results below the requested pz.
    # Use the actual page size to calculate how many pages we really need.
    actual_pz = max(len(first), 1)
    effective_pz = min(pz, actual_pz)
    total_pages = (total + effective_pz - 1) // effective_pz if total else 1
    if pages:
        total_pages = min(pages, total_pages)
    expected_pages = total_pages
    page_rows = {1: list(first)}
    failed_pages = []
    if total_pages > 1:
        with ThreadPoolExecutor(max_workers=min(16, total_pages - 1)) as ex:
            futs = {ex.submit(_one_page, pn): pn for pn in range(2, total_pages + 1)}
            for f in as_completed(futs):
                page = futs[f]
                try:
                    d, _ = f.result()
                except Exception:
                    d = []
                if d:
                    page_rows[page] = list(d)
                else:
                    failed_pages.append(page)
    rows = []
    for page in sorted(page_rows):
        rows.extend(page_rows[page])
    # For an explicit sample, ``complete`` means all requested pages arrived;
    # for a full pull, this is the same as every page implied by ``total``.
    # A zero total with non-empty rows is not trusted as a complete response.
    complete = bool(total and len(page_rows) == expected_pages and not failed_pages)
    meta = {
        "rows": rows,
        "total": total,
        "pages_expected": expected_pages,
        "pages_ok": len(page_rows),
        "failed_pages": sorted(failed_pages),
        "complete": complete,
    }
    return meta if return_meta else rows

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
            if pages is None and not _full_snapshot_payload_is_complete(payload):
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
        rows = []
        for d in raw:
            rows.append(_sanitize_market_row({
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
            }))
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
                temp_path = f"{cache_path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                with open(temp_path, "w", encoding="utf-8") as handle:
                    payload = {
                        "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "rows": rows,
                    }
                    if pages is None:
                        payload.update({
                            "complete": bool(pagination_complete),
                            "expected_rows": int(raw_meta.get("total") or len(rows)),
                            "pages_expected": int(raw_meta.get("pages_expected") or 0),
                            "pages_ok": int(raw_meta.get("pages_ok") or 0),
                        })
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


def _full_snapshot_payload_is_complete(payload):
    """Validate the persisted full-market snapshot marker and row coverage."""
    if not isinstance(payload, dict) or payload.get("complete") is not True:
        return False
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) < FULL_MARKET_MIN_ROWS:
        return False
    try:
        expected = int(payload.get("expected_rows") or 0)
    except (TypeError, ValueError):
        return False
    if expected <= 0:
        return False
    codes = {str(row.get("code") or "") for row in rows if isinstance(row, dict)}
    codes.discard("")
    return len(codes) >= max(FULL_MARKET_MIN_ROWS, int(expected * 0.90))


def load_market_snapshot_full_cached():
    """Return the last persisted full-market snapshot rows.  Read-only, no network."""
    try:
        with open(MARKET_SNAPSHOT_FULL_CACHE_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        return rows if _full_snapshot_payload_is_complete(payload) and rows else []
    except (OSError, ValueError, TypeError):
        return []


def _fresh_full_snapshot_from_disk(max_age):
    """Read a complete full-market cache when both file and content are fresh."""
    try:
        age = max(0.0, time.time() - os.path.getmtime(MARKET_SNAPSHOT_FULL_CACHE_PATH))
        if age > max_age:
            return None
        with open(MARKET_SNAPSHOT_FULL_CACHE_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not _full_snapshot_payload_is_complete(payload):
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
        if isinstance(rows, list) and len(rows) >= FULL_MARKET_MIN_ROWS:
            return rows
        return []


def fetch_hot_sector_snapshot(pages=4):
    """用涨幅榜前若干页聚合盘中热点行业，避免为了板块轮动每5分钟扫描全市场。"""
    def _do():
        fields = "f2,f3,f8,f12,f14,f62,f100,f184"
        global _hot_sector_fetch_state
        requested_pages = max(1, min(int(pages), 6))
        raw_meta = _fetch_clist(fields, fid="f3", pages=requested_pages, return_meta=True)
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


def get_hot_sector_fetch_state():
    """Return sector pagination quality without triggering a network request."""
    return dict(_hot_sector_fetch_state)


def _realtime_row_from_ulist(raw):
    return MN.realtime_row_from_ulist(raw, quote_at_fn=_quote_at)


def fetch_realtime_for_codes(codes, fields="f2,f3,f5,f6,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,f62,f66,f72,f78,f84,f184,f100,f124", return_meta=False):
    """批量取指定代码的实时行情（ulist.np，单次/分批请求，比全市场快照快数十倍）。
    用于选股：按全市场基础库代码分批拉取，避免使用单个超长 URL。"""
    # Callers often combine strategy candidates and the universe, which can
    # contain the same code several times.  Preserve order while removing
    # duplicates so request counts and coverage denominators describe the
    # actual universe rather than the caller's intermediate list.
    normalized = list(dict.fromkeys(
        str(code) for code in (codes or [])
        if str(code).isdigit() and len(str(code)) == 6
    ))
    if not normalized:
        return {"rows": [], "expected": 0, "returned": 0, "coverage_pct": 0.0,
                "complete": False, "batches": [], "missing_codes": []} if return_meta else []
    # ulist 接口在 push2delay 上最稳最快，优先；其余作为兜底
    ULIST_HOSTS = ["push2delay.eastmoney.com", "push2.eastmoney.com", "82.push2.eastmoney.com"]
    out = []
    batch_meta = []
    BATCH = 200  # secids 过长时分批，避免超长 URL
    for i in range(0, len(normalized), BATCH):
        batch = normalized[i:i + BATCH]
        secids = ",".join(_secid(c) for c in batch)
        params = {
            "pn": 1, "pz": len(batch), "np": 1, "fltt": 2, "invt": 2,
            "ut": UT_FLOW, "fields": fields, "secids": secids,
        }
        batch_by_code = {}
        attempts_used = 0
        for attempt in range(3):
            attempts_used = attempt + 1
            for k in range(len(ULIST_HOSTS)):
                host = ULIST_HOSTS[(i // BATCH + k + attempt) % len(ULIST_HOSTS)]
                try:
                    j = _get_json(f"https://{host}/api/qt/ulist.np/get", params, retries=1)
                    diff = (j or {}).get("data", {}).get("diff") or []
                except Exception:
                    diff = []
                if diff:
                    for d in diff:
                        row = _realtime_row_from_ulist(d)
                        code = row["code"] if row else ""
                        if not row or code not in batch:
                            continue
                        batch_by_code[code] = row
                    # Do not stop after a partial page: the next attempt
                    # requests the same batch and fills the missing codes.
                    if len(batch_by_code) >= len(batch):
                        break
                reset_data_source("实时行情源空响应")
                time.sleep(0.25 * (attempt + 1))
            if len(batch_by_code) >= len(batch):
                break
        # One last smaller request reduces the common failure mode where a
        # provider drops a few codes from an otherwise valid 200-row page.
        missing = [code for code in batch if code not in batch_by_code]
        if missing and len(missing) > 1:
            for start in range(0, len(missing), 50):
                small = missing[start:start + 50]
                secids_small = ",".join(_secid(c) for c in small)
                small_params = dict(params, secids=secids_small, pz=len(small))
                for host in ULIST_HOSTS:
                    try:
                        j = _get_json(f"https://{host}/api/qt/ulist.np/get", small_params, retries=1)
                        diff = (j or {}).get("data", {}).get("diff") or []
                    except Exception:
                        diff = []
                    for d in diff:
                        row = _realtime_row_from_ulist(d)
                        code = row["code"] if row else ""
                        if row and code in small:
                            batch_by_code[code] = row
                    if all(code in batch_by_code for code in small):
                        break
        batch_rows = [batch_by_code[code] for code in batch if code in batch_by_code]
        out.extend(batch_rows)
        batch_meta.append({
            "offset": i, "requested": len(batch), "returned": len(batch_rows),
            "coverage_pct": round(len(batch_rows) / max(len(batch), 1) * 100, 2),
            "complete": len(batch_rows) == len(batch), "attempts": attempts_used,
            "missing_codes": [code for code in batch if code not in batch_by_code][:100],
        })
    returned_codes = {str(row.get("code")) for row in out if row.get("code")}
    metadata = {
        "rows": out, "expected": len(normalized), "returned": len(returned_codes),
        "coverage_pct": round(len(returned_codes) / max(len(normalized), 1) * 100, 2),
        "complete": len(returned_codes) == len(normalized), "batches": batch_meta,
        "missing_codes": [code for code in normalized if code not in returned_codes][:200],
    }
    return metadata if return_meta else out


def fetch_tencent_realtime_for_codes(codes):
    """Independent public quote cross-check for a small list of A-share codes."""
    codes = [str(code) for code in (codes or []) if str(code).isdigit() and len(str(code)) == 6]
    if not codes:
        return []
    # 腾讯单次 URL 过长时会出现 200/空正文，不能把整批标记成“独立源未返回”。
    # 分成小批后逐批重试，失败代码再交给新浪备用源。
    # Smaller requests avoid Tencent returning HTTP 200 with a truncated body
    # during peak market traffic; missing codes are retried independently.
    if len(codes) > 30:
        rows = []
        for start in range(0, len(codes), 30):
            rows.extend(fetch_tencent_realtime_for_codes(codes[start:start + 30]))
        return rows
    def _prefix(code):
        # 北交所新代码以 920 开头；必须在通用的 9 开头沪市分支之前识别。
        if code.startswith(("920", "8", "4")):
            return "bj"
        if code.startswith(("6", "9")):
            return "sh"
        return "sz"
    # 公共接口偶发截断或瞬断。只重试尚未得到有效返回的代码，避免把一次
    # 网络抖动直接升级成“行情不可信”。
    pending = list(codes)
    rows_by_code = {}
    for attempt in range(3):
        if not pending:
            break
        try:
            text = http_get(
                "https://qt.gtimg.cn/q=" + ",".join(_prefix(code) + code for code in pending),
                timeout=8, encoding="gbk", retries=1,
            )
        except Exception:
            text = ""
        for row in MP.parse_tencent_realtime_text(
            text, attempt=attempt + 1, allowed_codes=pending
        ):
            rows_by_code[row["code"]] = row
        pending = [code for code in pending if code not in rows_by_code]
        if pending and attempt < 2:
            if not text:
                reset_data_source("腾讯个股独立行情源空响应")
            time.sleep(0.25 * (attempt + 1))
    return [rows_by_code[code] for code in codes if code in rows_by_code]


def _fetch_sina_realtime_for_codes(codes):
    """新浪公开个股行情备用源；只用于腾讯缺失代码的独立交叉核验。"""
    codes = [str(code) for code in (codes or []) if str(code).isdigit() and len(str(code)) == 6]
    if not codes:
        return []

    def _prefix(code):
        if code.startswith(("920", "8", "4")):
            return "bj"
        if code.startswith(("6", "9")):
            return "sh"
        return "sz"

    rows_by_code = {}
    # 新浪也会对过长 URL 返回空正文，控制在 80 个代码以内。
    for start in range(0, len(codes), 80):
        batch = codes[start:start + 80]
        text = ""
        for attempt in range(2):
            try:
                response = _session().get(
                    "https://hq.sinajs.cn/list=" + ",".join(_prefix(code) + code for code in batch),
                    headers={"Referer": "https://finance.sina.com.cn/", **HEADERS},
                    timeout=8,
                )
                response.raise_for_status()
                response.encoding = "gbk"
                text = response.text or ""
            except Exception:
                text = ""
            if text.strip():
                break
            if attempt == 0:
                reset_data_source("新浪独立行情源空响应，自动重试")
                time.sleep(0.25)
        for row in MP.parse_sina_realtime_text(text, allowed_codes=batch):
            rows_by_code[row["code"]] = row
    return [rows_by_code[code] for code in codes if code in rows_by_code]


def fetch_independent_realtime_for_codes(codes):
    """获取独立个股行情：腾讯→新浪备用，逐代码重试并保留来源标记。

    不能只按“是否有行”判断腾讯成功：部分响应可能带空价、空时间或旧
    格式。此类行会进入备用源，而不是把整只股票误报成已核验。
    """
    normalized = [str(code) for code in (codes or []) if str(code).isdigit() and len(str(code)) == 6]
    if not normalized:
        return []
    def _usable(row):
        if not isinstance(row, dict):
            return False
        try:
            price = float(row.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        return price > 0 and bool(str(row.get("quote_at") or "").strip())

    primary = fetch_tencent_realtime_for_codes(normalized)
    by_code = {
        str(row.get("code")): row for row in primary
        if row.get("code") and _usable(row)
    }
    missing = [code for code in normalized if code not in by_code]
    if missing:
        fallback = _fetch_sina_realtime_for_codes(missing)
        by_code.update({str(row.get("code")): row for row in fallback if row.get("code") and _usable(row)})
    # 新浪偶发只返回部分列表；再做一次小批次备用重试，避免同一轮把
    # 瞬时网络抖动写成“独立行情源未返回”。
    missing = [code for code in normalized if code not in by_code]
    if missing:
        reset_data_source("腾讯/新浪独立行情均有缺口，自动切换后重试")
        time.sleep(0.25)
        fallback_retry = _fetch_sina_realtime_for_codes(missing)
        by_code.update({str(row.get("code")): row for row in fallback_retry if row.get("code") and _usable(row)})
    return [by_code[code] for code in normalized if code in by_code]


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
    }
    # A partial flow ranking is actively misleading: callers would sort the
    # returned subset as if it were the whole market.  Retain diagnostics above
    # but fail closed for formal candidate scoring.
    return out if coverage_ok else {}


def get_flow_fetch_state():
    """Return the last full-market super-order fetch quality metadata."""
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


def enrich_live_flow(live_universe, missing_codes):
    """对缺失超大单资金的个股进行补齐。

    Args:
        live_universe: 市场快照行列表
        missing_codes: 缺失 super_net 的股票代码集合

    Returns:
        dict: {code: super_net_value}
    """
    flow_data = enrich_live_flow_details(live_universe, missing_codes)
    return {code: data.get("super_net") for code, data in flow_data.items() if data.get("super_net") is not None}


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
        return {
            "data": merged,
            "report_dates": used_dates,
            "annual_report_date": annual_used,
        }
    return _cached("finance", 3600 * 6, _do)

def _fetch_finance_report(report_date):
    rows = []
    page = 1
    while page <= 60:
        params = {
            "reportName": "RPT_LICO_FN_CPD",
            "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,WEIGHTAVG_ROE,YSTZ,SJLTZ,PARENT_NETPROFIT,BASIC_EPS,BPS,REPORTDATE",
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
            rows.append({
                "code": str(d.get("SECURITY_CODE")),
                "name": d.get("SECURITY_NAME_ABBR"),
                "roe": d.get("WEIGHTAVG_ROE"),
                "rev_yoy": d.get("YSTZ"), "profit_yoy": d.get("SJLTZ"),
                "net_profit": d.get("PARENT_NETPROFIT"),
                "eps": d.get("BASIC_EPS"), "bps": d.get("BPS"),
                "report_date": report_date,
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
    rows = []
    previous_close = None
    for item in raw:
        if len(item) < 6:
            continue
        date, open_, close, high, low, volume = item[:6]
        open_, close, high, low, volume = map(float, (open_, close, high, low, volume))
        typical = (open_ + close + high + low) / 4
        amplitude_base = previous_close or close
        rows.append(
            {
                "date": date,
                "open": open_,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": volume * 100 * typical,
                "amplitude": (high - low) / amplitude_base * 100 if amplitude_base else None,
            }
        )
        previous_close = close
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
    start = pd.Timestamp(str(beg))
    finish = pd.Timestamp(str(end))
    rows = []
    for item in payload:
        date = pd.Timestamp(item.get("day"))
        if date < start or date > finish:
            continue
        open_ = float(item["open"])
        close = float(item["close"])
        high = float(item["high"])
        low = float(item["low"])
        volume = float(item["volume"])
        typical = (open_ + close + high + low) / 4
        rows.append(
            {
                "date": str(date.date()),
                "open": open_,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": volume * typical,
                "amplitude": (high - low) / close * 100 if close else None,
            }
        )
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
    rows = []
    for k in klines:
        p = k.split(",")
        rows.append({
            "date": p[0], "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]),
            "volume": float(p[5]), "amount": float(p[6]),
            "amplitude": float(p[7]) if len(p) > 7 else None,
        })
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
    """Reconcile metadata with existing CSV files after upgrading old caches."""
    global _manifest, _manifest_mtime, _manifest_dirty, _manifest_pending
    existing = get_kline_manifest()
    rebuilt = {}
    for filename in os.listdir(KLINE_DIR):
        if not filename.endswith(".csv"):
            continue
        code = filename[:-4]
        frame = load_cached_kline(code)
        if frame is None:
            continue
        valid = frame.dropna(subset=["close"])
        prior = existing.get(code, {})
        rebuilt[code] = {
            "rows": int(len(valid)),
            "first_date": str(valid.index[0].date()) if len(valid) else None,
            "last_date": str(valid.index[-1].date()) if len(valid) else None,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": prior.get("source", "unknown"),
            "adjustment": prior.get("adjustment", "unknown"),
        }
    with _manifest_lock:
        manifest = _load_kline_manifest_unlocked()
        manifest.update(rebuilt)
        _manifest = manifest
        _manifest_pending.update(rebuilt)
        _manifest_dirty += len(rebuilt) or 1
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
        for key, (secid, name) in OVERSEAS_SECIDS.items():
            try:
                df = fetch_kline(None, beg=beg, secid=secid)
                if df is not None and not df.empty:
                    out[key] = {"name": name, "df": df}
            except Exception:
                continue
            time.sleep(0.15)
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
    return _cached("news", 120, _do)


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

    return _cached("announcements:" + ",".join(normalized), 300, _do)


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
