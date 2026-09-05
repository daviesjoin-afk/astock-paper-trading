# -*- coding: utf-8 -*-
"""替代数据源层（P0）：同花顺热点题材归因 + 限售解禁日历。

来源：github.com/xuyongfu/a-stock-data-20260526 (V3.1) 提取改造。
设计原则：
- 全部 fail-open：任何异常返回空结果，绝不阻断交易主链路；
- 进程内 TTL 缓存 + 磁盘日级缓存，扫描级调用不重复打源；
- 仅 HTTP requests，无新依赖。
"""
import datetime as dt
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

import requests

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data_cache", "alt_data")
os.makedirs(CACHE_DIR, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "Chrome/117.0.0.0 Safari/537.36")
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
MINUTE_FLOW_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
MINUTE_FLOW_URLS = (
    MINUTE_FLOW_URL,
    "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
    "https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get",
)
QUOTE_DEPTH_URL = "https://push2.eastmoney.com/api/qt/stock/get"
TICK_DETAIL_URL = "https://push2.eastmoney.com/api/qt/stock/details/get"
MINUTE_TREND_URL = "https://push2.eastmoney.com/api/qt/stock/trends2/get"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="

_mem_cache = {}
_cache_lock = threading.Lock()
NEGATIVE_CACHE_SECONDS = 10 * 60
MAX_MEM_CACHE_ENTRIES = 128


def _eastmoney_secid(code):
    code = str(code or "").strip().zfill(6)
    return f"1.{code}" if code.startswith(("5", "6", "9")) else f"0.{code}"


def _minute_flow_payload(params):
    """Fetch JSON with requests, falling back to the system TLS client.

    Eastmoney currently closes urllib3 connections from some cloud IPs while
    accepting libcurl from the same host.  The fallback is argv-only (no
    shell), time-bounded, and used for at most the small holding/core set.
    """
    errors = []
    for endpoint in MINUTE_FLOW_URLS:
        try:
            response = requests.get(
                endpoint, params=params,
                headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
                timeout=5,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("data"):
                payload["_transport_host"] = endpoint.split("/")[2]
                return payload
        except Exception as exc:
            errors.append(type(exc).__name__)
    for endpoint in MINUTE_FLOW_URLS:
        url = f"{endpoint}?{urlencode(params)}"
        try:
            completed = subprocess.run(
                ["curl", "--http2", "--fail", "--silent", "--show-error",
                 "--max-time", "6", "--header", "Referer: https://data.eastmoney.com/",
                 "--header", f"User-Agent: {UA}", url],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=8, check=True,
            )
            payload = json.loads(completed.stdout)
            if isinstance(payload, dict) and payload.get("data"):
                payload["_transport_host"] = endpoint.split("/")[2]
                return payload
        except Exception as exc:
            errors.append(type(exc).__name__)
    raise RuntimeError(f"minute flow unavailable: {'/'.join(errors[-6:])}")


def eastmoney_minute_fund_flow(code, asof_day=None, ttl_seconds=150):
    """Current-session cumulative minute fund flow, never historical replay.

    Eastmoney does not expose a point-in-time historical snapshot through this
    endpoint.  A non-today ``asof_day`` therefore returns an explicit
    unavailable result instead of leaking today's data into a replay.
    """
    code = str(code or "").strip().zfill(6)
    asof_iso = _asof_iso(asof_day)
    if asof_iso != dt.date.today().isoformat():
        return {"status": "historical_unavailable", "code": code, "points": []}
    key = f"minute_flow:{asof_iso}:{code}"
    hit = _cached(key, ttl_seconds)
    if hit is not None:
        return hit
    result = {"status": "source_unavailable", "code": code, "points": []}
    try:
        payload = _minute_flow_payload({
            "secid": _eastmoney_secid(code), "klt": "1", "lmt": "0",
            "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "ut": "fa5fd1943c7b386f172d6893dbbd1d0c",
        })
        klines = ((payload.get("data") or {}).get("klines") or [])
        points = []
        for raw in klines:
            values = raw.split(",") if isinstance(raw, str) else list(raw or [])
            if len(values) < 6:
                continue
            try:
                points.append({
                    "time": str(values[0]), "main_net": float(values[1]),
                    "small_net": float(values[2]), "mid_net": float(values[3]),
                    "large_net": float(values[4]), "super_net": float(values[5]),
                })
            except (TypeError, ValueError):
                continue
        if points:
            fetched_at = dt.datetime.now().isoformat(timespec="seconds")
            result = {
                "status": "ok", "code": code, "points": points,
                "source": f"eastmoney_minute_fund_flow:{payload.get('_transport_host', 'unknown')}",
                "source_at": fetched_at, "data_at": points[-1]["time"],
            }
    except Exception as exc:
        result["error"] = type(exc).__name__
    # Do not turn a transient provider failure into a 150-second false empty.
    if result.get("status") == "ok":
        _store(key, result)
    return result


def fund_flow_trajectory(code, asof_day=None, ttl_seconds=150):
    """Derive bounded shadow features from cumulative minute fund-flow rows."""
    raw = eastmoney_minute_fund_flow(code, asof_day=asof_day, ttl_seconds=ttl_seconds)
    points = raw.get("points") or []
    base = {
        "status": raw.get("status", "source_unavailable"), "code": str(code).zfill(6),
        "source": raw.get("source"), "source_at": raw.get("source_at"),
        "data_at": raw.get("data_at"), "points": len(points), "score_applied": False,
    }
    if len(points) < 2:
        return base

    def delta(field, periods):
        earlier = points[max(0, len(points) - 1 - periods)][field]
        return points[-1][field] - earlier

    increments = [
        points[idx]["main_net"] - points[idx - 1]["main_net"]
        for idx in range(max(1, len(points) - 10), len(points))
    ]
    active = [item for item in increments if abs(item) >= 1.0]
    persistence = (sum(1 for item in active if item > 0) / len(active)) if active else 0.5
    recent = delta("main_net", 3)
    prior_end = max(1, len(points) - 4)
    prior_start = max(0, prior_end - 3)
    prior = points[prior_end]["main_net"] - points[prior_start]["main_net"]
    reversal = "none"
    if prior < 0 < recent:
        reversal = "outflow_to_inflow"
    elif prior > 0 > recent:
        reversal = "inflow_to_outflow"
    direction = "inflow" if recent > 0 else "outflow" if recent < 0 else "flat"
    base.update({
        "status": "ok", "latest_main_net": round(points[-1]["main_net"], 2),
        "latest_super_net": round(points[-1]["super_net"], 2),
        "main_delta_3m": round(recent, 2), "main_delta_5m": round(delta("main_net", 5), 2),
        "main_delta_15m": round(delta("main_net", 15), 2),
        "super_delta_5m": round(delta("super_net", 5), 2),
        "positive_persistence_10m": round(persistence, 4),
        "direction": direction, "reversal": reversal,
    })
    return base


def fund_flow_trajectories(codes, asof_day=None, max_workers=6):
    """Fetch a small holding/core-candidate set concurrently, outside DB locks."""
    unique = list(dict.fromkeys(str(code or "").zfill(6) for code in (codes or []) if code))
    output = {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(unique) or 1))) as pool:
        futures = {pool.submit(fund_flow_trajectory, code, asof_day): code for code in unique}
        for future in as_completed(futures):
            code = futures[future]
            try:
                output[code] = future.result()
            except Exception as exc:
                output[code] = {"status": "source_unavailable", "code": code,
                                "error": type(exc).__name__, "score_applied": False}
    return output


def _micro_get(url, params, timeout=4):
    """Fetch micro data with host rotation and curl TLS fallback."""
    urls = list(dict.fromkeys((
        url,
        url.replace("push2.eastmoney.com", "push2delay.eastmoney.com"),
        url.replace("push2.eastmoney.com", "push2his.eastmoney.com"),
    )))
    errors = []
    for endpoint in urls:
        try:
            response = requests.get(
                endpoint, params=params,
                headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"},
                timeout=timeout,
            )
            response.raise_for_status()
            data = (response.json() or {}).get("data") or {}
            if data:
                return data
        except Exception as exc:
            errors.append(type(exc).__name__)
    for endpoint in urls:
        try:
            completed = subprocess.run(
                ["curl", "--http2", "--fail", "--silent", "--show-error",
                 "--max-time", str(timeout + 1),
                 "--header", "Referer: https://quote.eastmoney.com/",
                 "--header", f"User-Agent: {UA}",
                 f"{endpoint}?{urlencode(params)}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout + 3, check=True,
            )
            data = (json.loads(completed.stdout) or {}).get("data") or {}
            if data:
                return data
        except Exception as exc:
            errors.append(type(exc).__name__)
    raise RuntimeError(f"micro data unavailable: {'/'.join(errors[-6:])}")


def _tencent_depth(code):
    prefix = "sh" if str(code).startswith(("5", "6", "9")) else "sz"
    response = requests.get(
        TENCENT_QUOTE_URL + prefix + str(code),
        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"}, timeout=5,
    )
    response.raise_for_status()
    response.encoding = "gbk"
    text = response.text or ""
    if "=" not in text:
        return {}
    parts = text.split("=", 1)[1].strip().strip(";\r\n").strip('"').split("~")
    if len(parts) < 29:
        return {}
    bid_volume = sum(float(parts[idx] or 0) for idx in (10, 12, 14, 16, 18))
    ask_volume = sum(float(parts[idx] or 0) for idx in (20, 22, 24, 26, 28))
    return {"bid_volume": bid_volume, "ask_volume": ask_volume,
            "quote_at": parts[30] if len(parts) > 30 else None}


def minute_trend_series(code, asof_day=None, ttl_seconds=90):
    """Parse the current-session 1-minute trend series (price/vwap/volume).

    分时接口每根分钟线含：时间, 价格, 均价(VWAP), 成交量(手), 成交额。
    主力点火判定需要分钟量能结构与低点抬高层次；缺失/失败一律返回空列表
    （fail-open），调用方按数据缺失处理。
    """
    code = str(code or "").strip().zfill(6)
    asof_iso = _asof_iso(asof_day)
    if asof_iso != dt.date.today().isoformat():
        return []
    key = f"minute_trend:{asof_iso}:{code}"
    hit = _cached(key, ttl_seconds)
    if hit is not None:
        return hit
    try:
        trend_data = _micro_get(MINUTE_TREND_URL, {
            "secid": _eastmoney_secid(code), "ndays": "1", "iscr": "0",
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "ut": "fa5fd1943c7b386f172d6893dbbd1d0c",
        })
        rows = []
        for raw in (trend_data.get("trends") or []):
            values = raw.split(",") if isinstance(raw, str) else list(raw or [])
            if len(values) < 5:
                continue
            try:
                rows.append({
                    "time": str(values[0]),
                    "price": float(values[1] or 0),
                    "vwap": float(values[2] or 0),
                    "volume": float(values[3] or 0),
                    "amount": float(values[4] or 0),
                })
            except (TypeError, ValueError):
                continue
        rows = [row for row in rows if row["price"] > 0 and row["volume"] >= 0]
        if rows:
            _store(key, rows)
        return rows
    except Exception:
        return []


def market_microstructure(code, asof_day=None, ttl_seconds=90):
    """Return bounded current-session order-book/tick/VWAP evidence.

    Public quote endpoints are not exchange L2. The result is therefore
    explicitly shadow-grade and may only contribute a small soft score. It is
    never replayed historically and provider failure is fail-open.
    """
    code = str(code or "").strip().zfill(6)
    asof_iso = _asof_iso(asof_day)
    if asof_iso != dt.date.today().isoformat():
        return {"status": "historical_unavailable", "code": code,
                "score_applied": False, "grade": "public_quote_shadow"}
    key = f"microstructure:{asof_iso}:{code}"
    hit = _cached(key, ttl_seconds)
    if hit is not None:
        return hit
    result = {"status": "source_unavailable", "code": code,
              "score_applied": False, "grade": "public_quote_shadow"}
    errors = []
    try:
        depth = _tencent_depth(code)
        bid_volume = float(depth.get("bid_volume") or 0)
        ask_volume = float(depth.get("ask_volume") or 0)
        depth_total = bid_volume + ask_volume
        imbalance = (bid_volume - ask_volume) / depth_total if depth_total > 0 else None
    except Exception as exc:
        errors.append(f"depth:{type(exc).__name__}")
        bid_volume = ask_volume = 0.0
        imbalance = None
        depth = {}
    try:
        ticks = _micro_get(TICK_DETAIL_URL, {
            "secid": _eastmoney_secid(code), "pos": "-100", "iscca": "1",
            "fields1": "f1,f2,f3,f4,f5", "fields2": "f51,f52,f53,f54,f55",
            "ut": "fa5fd1943c7b386f172d6893dbbd1d0c",
        }).get("details") or []
        active_buy = active_sell = neutral = 0.0
        for raw in ticks:
            values = raw.split(",") if isinstance(raw, str) else list(raw or [])
            if len(values) < 5:
                continue
            volume = float(values[2] or 0)
            side = str(values[4])
            if side == "2":
                active_buy += volume
            elif side == "1":
                active_sell += volume
            else:
                neutral += volume
        active_total = active_buy + active_sell
        active_ratio = (active_buy - active_sell) / active_total if active_total > 0 else None
    except Exception as exc:
        errors.append(f"ticks:{type(exc).__name__}")
        ticks = []
        active_buy = active_sell = neutral = 0.0
        active_ratio = None
    try:
        trend_data = _micro_get(MINUTE_TREND_URL, {
            "secid": _eastmoney_secid(code), "ndays": "1", "iscr": "0",
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "ut": "fa5fd1943c7b386f172d6893dbbd1d0c",
        })
        trends = trend_data.get("trends") or []
        last = trends[-1].split(",") if trends and isinstance(trends[-1], str) else []
        last_price = float(last[1]) if len(last) > 1 else None
        minute_vwap = float(last[2]) if len(last) > 2 else None
        vwap_deviation = (
            last_price / minute_vwap - 1.0
            if last_price and minute_vwap and minute_vwap > 0 else None
        )
        data_at = last[0] if last else None
    except Exception as exc:
        errors.append(f"vwap:{type(exc).__name__}")
        trends = []
        last_price = minute_vwap = vwap_deviation = data_at = None
    available = sum(value is not None for value in (imbalance, active_ratio, vwap_deviation))
    if available:
        result.update({
            "status": "ok" if available == 3 else "partial", "source": "eastmoney_public_microstructure",
            "source_at": dt.datetime.now().isoformat(timespec="seconds"), "data_at": data_at,
            "depth_imbalance": round(imbalance, 4) if imbalance is not None else None,
            "bid5_volume": round(bid_volume, 2), "ask5_volume": round(ask_volume, 2),
            "active_buy_volume": round(active_buy, 2), "active_sell_volume": round(active_sell, 2),
            "active_buy_sell_imbalance": round(active_ratio, 4) if active_ratio is not None else None,
            "tick_samples": len(ticks), "minute_samples": len(trends),
            "last_price": last_price, "minute_vwap": minute_vwap,
            "vwap_deviation_pct": round(vwap_deviation * 100, 4) if vwap_deviation is not None else None,
            "errors": errors, "score_applied": False,
        })
        _store(key, result)
    else:
        result["errors"] = errors
    return result


def market_microstructures(codes, asof_day=None, max_workers=5):
    """Fetch only the small live-review set concurrently."""
    unique = list(dict.fromkeys(str(code or "").zfill(6) for code in (codes or []) if code))
    output = {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(unique) or 1))) as pool:
        futures = {pool.submit(market_microstructure, code, asof_day): code for code in unique}
        for future in as_completed(futures):
            code = futures[future]
            try:
                output[code] = future.result()
            except Exception as exc:
                output[code] = {"status": "source_unavailable", "code": code,
                                "error": type(exc).__name__, "score_applied": False,
                                "grade": "public_quote_shadow"}
    return output


def _asof_iso(value=None):
    if value is None:
        return dt.date.today().isoformat()
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return dt.date.fromisoformat(str(value)[:10]).isoformat()


def _age_days(data_date, asof_day=None):
    """Return point-in-time age; future or malformed rows are unusable."""
    try:
        age = (
            dt.date.fromisoformat(_asof_iso(asof_day))
            - dt.date.fromisoformat(str(data_date)[:10])
        ).days
    except (TypeError, ValueError):
        return None
    return age if age >= 0 else None


def _cached(key, ttl_seconds):
    with _cache_lock:
        hit = _mem_cache.get(key)
        if hit and (dt.datetime.now() - hit[0]).total_seconds() < ttl_seconds:
            return hit[1]
    return None


def _store(key, value):
    with _cache_lock:
        if key not in _mem_cache and len(_mem_cache) >= MAX_MEM_CACHE_ENTRIES:
            oldest_key = min(_mem_cache, key=lambda item: _mem_cache[item][0])
            _mem_cache.pop(oldest_key, None)
        _mem_cache[key] = (dt.datetime.now(), value)


def eastmoney_datacenter(report_name, columns="ALL", filter_str="",
                         page_size=50, sort_columns="", sort_types="-1"):
    """东财数据中心统一查询（龙虎榜/解禁/两融/大宗/股东户数共用）。"""
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = requests.get(DATACENTER_URL, params=params,
                     headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    d = r.json()
    result = d.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"eastmoney response missing result: {d.get('message') or d.get('code')}")
    return result.get("data") or []


def ths_hot_reason(date=None, ttl_seconds=600):
    """同花顺当日强势股 + 人工题材归因标签。

    返回 list[dict]: code/name/reason(题材)/zhangfu/huanshou/ddejingliang...
    失败返回 []（fail-open）。盘中 TTL 10 分钟。
    """
    if date is None:
        date = dt.date.today().strftime("%Y-%m-%d")
    key = f"ths_hot:{date}"
    hit = _cached(key, ttl_seconds)
    if hit is not None:
        return hit
    try:
        url = (f"http://zx.10jqka.com.cn/event/api/getharden/"
               f"date/{date}/orderby/date/orderway/desc/charset/GBK/")
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        data = r.json()
        if data.get("errocode", 0) != 0:
            rows = []
        else:
            rows = data.get("data") or []
        fetched_at = dt.datetime.now().isoformat(timespec="seconds")
        rows = [
            {
                "code": str(item.get("code") or ""),
                "name": item.get("name"),
                "reason": item.get("reason"),
                "pct": item.get("zhangfu"),
                "turnover": item.get("huanshou"),
                "amount": item.get("chengjiaoe"),
                "dde_net": item.get("ddejingliang"),
                "source": "ths_hot_reason",
                "source_at": fetched_at,
            }
            for item in rows if item.get("code")
        ]
    except Exception:
        rows = []
    _store(key, rows)
    return rows


def _lockup_disk_path(asof_day):
    return os.path.join(CACHE_DIR, f"lockup_{asof_day}.json")


def lockup_upcoming(codes, asof_day=None, forward_days=90, min_ratio=0.0):
    """批量查询未来 forward_days 天内的限售解禁。

    返回 {code: [{"date","type","shares","ratio"}, ...]}；磁盘日级缓存，
    当日已查过的代码不再请求。失败返回 {}（fail-open）。
    """
    asof_day = _asof_iso(asof_day)
    path = _lockup_disk_path(asof_day)
    store = {}
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                store = json.load(handle) or {}
    except (OSError, ValueError):
        store = {}
    wanted = [str(c) for c in (codes or []) if str(c)]
    missing = [c for c in wanted if c not in store]
    end_str = (dt.date.fromisoformat(asof_day) + dt.timedelta(days=forward_days)).isoformat()
    fetched = 0
    for code in missing[:40]:
        error_key = f"lockup_error:{asof_day}:{code}"
        if _cached(error_key, NEGATIVE_CACHE_SECONDS) is not None:
            continue
        try:
            rows = eastmoney_datacenter(
                "RPT_LIFT_STAGE",
                filter_str=(f'(SECURITY_CODE="{code}")'
                            f"(FREE_DATE>='{asof_day}')(FREE_DATE<='{end_str}')"),
                page_size=20, sort_columns="FREE_DATE", sort_types="1",
            )
            entries = []
            for row in rows:
                try:
                    ratio = float(row.get("FREE_RATIO") or 0)
                except (TypeError, ValueError):
                    ratio = 0.0
                if ratio < min_ratio:
                    continue
                entries.append({
                    "date": str(row.get("FREE_DATE", ""))[:10],
                    "type": row.get("LIMITED_STOCK_TYPE", ""),
                    "shares": row.get("FREE_SHARES_NUM", 0),
                    "ratio": round(ratio, 4),
                })
            store[code] = entries
            fetched += 1
        except Exception:
            # A transport/parser failure is not evidence that the stock has
            # no unlock event.  Keep it retryable instead of persisting a
            # false empty result for the whole trading day.
            _store(error_key, True)
    if fetched:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(store, handle, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass
    return {c: store.get(c, []) for c in wanted}


def lockup_penalty(code, asof_day=None):
    """持仓解禁风险扣分（0..15）与说明。

    未来30天内解禁且自由流通占比≥3% → -12；60天内≥5% → -8；取最重者。
    无数据/无解禁返回 (0.0, None)。
    """
    try:
        upcoming = lockup_upcoming([code], asof_day=asof_day).get(code) or []
    except Exception:
        return 0.0, None
    reference_day = dt.date.fromisoformat(_asof_iso(asof_day))
    worst_penalty, worst_desc = 0.0, None
    for item in upcoming:
        try:
            days = (dt.date.fromisoformat(item["date"]) - reference_day).days
        except (KeyError, ValueError):
            continue
        ratio = float(item.get("ratio") or 0) * 100
        if days <= 30 and ratio >= 3.0:
            penalty, desc = 12.0, f"{item['date']}(T-{days})解禁占流通{ratio:.1f}%"
        elif days <= 60 and ratio >= 5.0:
            penalty, desc = 8.0, f"{item['date']}(T-{days})解禁占流通{ratio:.1f}%"
        else:
            continue
        if penalty > worst_penalty:
            worst_penalty, worst_desc = penalty, desc
    return worst_penalty, worst_desc


# ---------------------------------------------------------------------------
# P1: 北向资金 + 龙虎榜
# ---------------------------------------------------------------------------

HSGT_HEADERS = {
    "User-Agent": UA,
    "Host": "data.hexin.cn",
    "Referer": "https://data.hexin.cn/",
}


def northbound_realtime(ttl_seconds=300):
    """沪深股通当日累计净流入（亿元）。

    返回 {"net_yi", "hgt_yi", "sgt_yi", "time", "points"}；取最后一个
    非空分钟点。上游断供/异常返回 None（fail-open）。注意：东财系北向
    净买字段 2024-08 后断供，此同花顺源是当前少数可用通道。
    """
    hit = _cached("northbound", ttl_seconds)
    if hit is not None:
        return hit
    result = None
    try:
        r = requests.get("https://data.hexin.cn/market/hsgtApi/method/dayChart/",
                         headers=HSGT_HEADERS, timeout=10)
        d = r.json()
        times = d.get("time") or []
        hgt = d.get("hgt") or []
        sgt = d.get("sgt") or []
        # 取最后一个非空分钟点（盘中为最新累计，盘后为全天）
        idx = None
        for i in range(min(len(times), max(len(hgt), len(sgt))) - 1, -1, -1):
            hv = hgt[i] if i < len(hgt) else None
            sv = sgt[i] if i < len(sgt) else None
            if isinstance(hv, (int, float)) or isinstance(sv, (int, float)):
                idx = i
                break
        if idx is not None:
            hv = hgt[idx] if idx < len(hgt) else None
            sv = sgt[idx] if idx < len(sgt) else None
            hv = hv if isinstance(hv, (int, float)) else 0.0
            sv = sv if isinstance(sv, (int, float)) else 0.0
            result = {
                "net_yi": round(hv + sv, 2),
                "hgt_yi": round(hv, 2), "sgt_yi": round(sv, 2),
                "time": str(times[idx]) if idx < len(times) else None,
                "points": len(times),
            }
    except Exception:
        result = None
    _store("northbound", result)
    return result


def lhb_for_stock(code, look_back_days=30, asof_day=None):
    """个股近 look_back_days 天龙虎榜记录。

    返回 [{"date","reason","net_buy_wan","turnover"}]（按日期倒序）；
    磁盘日级缓存，失败返回 []（fail-open）。
    """
    asof_day = _asof_iso(asof_day)
    path = os.path.join(CACHE_DIR, f"lhb_{code}_{asof_day}.json")
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return json.load(handle) or []
    except (OSError, ValueError):
        pass
    start = (dt.date.fromisoformat(asof_day) - dt.timedelta(days=look_back_days)).isoformat()
    records = []
    fetch_ok = False
    try:
        rows = eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=(f'(SECURITY_CODE="{code}")'
                        f"(TRADE_DATE>='{start}')(TRADE_DATE<='{asof_day}')"),
            page_size=50, sort_columns="TRADE_DATE", sort_types="-1",
        )
        fetch_ok = True
        for row in rows:
            try:
                net = round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1)
            except (TypeError, ValueError):
                net = 0.0
            records.append({
                "date": str(row.get("TRADE_DATE", ""))[:10],
                "reason": row.get("EXPLANATION", ""),
                "net_buy_wan": net,
                "turnover": round(float(row.get("TURNOVERRATE") or 0), 2),
            })
    except Exception:
        records = []
    try:
        if not fetch_ok:
            return records
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass
    return records


def lhb_position_signal(code, asof_day=None, within_days=7):
    """持仓龙虎榜信号（±6 分）与说明。

    最近 within_days 天内上榜：龙虎榜净买为正 → +6（游资/机构抢筹）；
    净卖为负 → -6。未上榜返回 (0.0, None)。
    """
    try:
        records = lhb_for_stock(code, look_back_days=15, asof_day=asof_day) or []
    except Exception:
        return 0.0, None
    if not records:
        return 0.0, None
    latest = records[0]
    age_days = _age_days(latest.get("date"), asof_day)
    if age_days is None:
        return 0.0, None
    if age_days > within_days:
        return 0.0, None
    net = float(latest.get("net_buy_wan") or 0)
    if net > 0:
        return 6.0, f"{latest['date']}上榜净买{net:.0f}万（{str(latest.get('reason'))[:20]}）"
    if net < 0:
        return -6.0, f"{latest['date']}上榜净卖{abs(net):.0f}万（{str(latest.get('reason'))[:20]}）"
    return 0.0, None


# ---------------------------------------------------------------------------
# P2: 融资融券 / 大宗交易 / 股东户数 / 一致预期EPS
# ---------------------------------------------------------------------------

def _daily_cached_json(kind, code, asof_day, fetch_fn):
    """Point-in-time disk cache; failures use a short in-memory retry delay."""
    asof_day = _asof_iso(asof_day)
    path = os.path.join(CACHE_DIR, f"{kind}_{code}_{asof_day}.json")
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return json.load(handle) or []
    except (OSError, ValueError):
        pass
    negative_key = f"negative:{kind}:{code}:{asof_day}"
    if _cached(negative_key, NEGATIVE_CACHE_SECONDS) is not None:
        return []
    try:
        rows = fetch_fn() or []
    except Exception:
        _store(negative_key, True)
        return []
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass
    return rows


def margin_trading(code, asof_day=None, page_size=12):
    """融资融券日级明细：[{date, rzye_wan, rqye_wan}]（倒序）。"""
    asof_iso = _asof_iso(asof_day)
    def _fetch():
        rows = eastmoney_datacenter(
            "RPTA_WEB_RZRQ_GGMX",
            filter_str=f'(SCODE="{code}")(DATE<=\'{asof_iso}\')',
            page_size=page_size, sort_columns="DATE", sort_types="-1",
        )
        out = []
        for row in rows:
            try:
                rzye = float(row.get("RZYE") or 0)
                rqye = float(row.get("RQYE") or 0)
            except (TypeError, ValueError):
                continue
            out.append({
                "date": str(row.get("DATE", ""))[:10],
                "rzye_wan": round(rzye / 1e4, 1),
                "rqye_wan": round(rqye / 1e4, 1),
            })
        return out
    return _daily_cached_json("margin", code, asof_iso, _fetch)


def margin_signal(code, asof_day=None):
    """融资余额 5 日变化信号（±4 分）。

    融资余额是杠杆资金温度计：5日增幅>2% +4（杠杆加仓），
    降幅<-2% -4（去杠杆）。数据不足返回 (0.0, None)。
    """
    rows = margin_trading(code, asof_day=asof_day)
    if len(rows) < 6:
        return 0.0, None
    latest, base = rows[0], rows[5]
    try:
        rzye_now = float(latest.get("rzye_wan") or 0)
        rzye_base = float(base.get("rzye_wan") or 0)
    except (TypeError, ValueError):
        return 0.0, None
    if rzye_base <= 0:
        return 0.0, None
    change = (rzye_now / rzye_base - 1) * 100
    if change >= 2.0:
        return 4.0, f"融资余额5日+{change:.1f}%（{rzye_now/1e4:.2f}亿）"
    if change <= -2.0:
        return -4.0, f"融资余额5日{change:.1f}%（{rzye_now/1e4:.2f}亿）"
    return 0.0, None


def block_trade_signal(code, asof_day=None, within_days=30):
    """大宗交易信号（-5..+3 分）。

    近30天大宗：折价≥3%成交 -5（折价出货）；溢价或平价成交 +3
    （接盘方愿意按市价拿货）。无大宗返回 (0.0, None)。
    """
    asof_iso = _asof_iso(asof_day)
    def _fetch():
        rows = eastmoney_datacenter(
            "RPT_DATA_BLOCKTRADE",
            filter_str=f'(SECURITY_CODE="{code}")(TRADE_DATE<=\'{asof_iso}\')',
            page_size=10, sort_columns="TRADE_DATE", sort_types="-1",
        )
        out = []
        for row in rows:
            try:
                close = float(row.get("CLOSE_PRICE") or 0)
                deal = float(row.get("DEAL_PRICE") or 0)
            except (TypeError, ValueError):
                continue
            premium = ((deal / close) - 1) * 100 if close > 0 else 0.0
            out.append({
                "date": str(row.get("TRADE_DATE", ""))[:10],
                "premium_pct": round(premium, 2),
                "amount_wan": round(float(row.get("DEAL_AMT") or 0) / 1e4, 1),
            })
        return out
    rows = _daily_cached_json("block", code, asof_iso, _fetch)
    if not rows:
        return 0.0, None
    latest = rows[0]
    age = _age_days(latest.get("date"), asof_iso)
    if age is None:
        return 0.0, None
    if age > within_days:
        return 0.0, None
    premium = float(latest.get("premium_pct") or 0)
    if premium <= -3.0:
        return -5.0, f"{latest['date']}大宗折价{premium:.1f}%成交{latest.get('amount_wan')}万"
    if premium >= 0.0:
        return 3.0, f"{latest['date']}大宗溢价/平价{premium:.1f}%成交{latest.get('amount_wan')}万"
    return 0.0, None


def holder_signal(code, asof_day=None):
    """股东户数信号（±5 分）。

    最新一期环比：户数降≥2% +5（筹码集中/吸筹），升≥5% -5（筹码分散）。
    季度级数据，日级缓存。
    """
    asof_iso = _asof_iso(asof_day)
    def _fetch():
        rows = eastmoney_datacenter(
            "RPT_HOLDERNUMLATEST",
            filter_str=f'(SECURITY_CODE="{code}")(END_DATE<=\'{asof_iso}\')',
            page_size=4, sort_columns="END_DATE", sort_types="-1",
        )
        out = []
        for row in rows:
            try:
                ratio = float(row.get("HOLDER_NUM_RATIO") or 0)
            except (TypeError, ValueError):
                continue
            published_at = str(
                row.get("HOLD_NOTICE_DATE") or row.get("NOTICE_DATE")
                or row.get("UPDATE_DATE")
                or row.get("LATEST_NOTICE_DATE") or ""
            )[:10]
            # Report-period data is not available until it is actually
            # disclosed.  Unknown disclosure dates remain context-only and
            # must never contribute a historical score.
            if not published_at or published_at > asof_iso:
                continue
            out.append({
                "date": str(row.get("END_DATE", ""))[:10],
                "published_at": published_at,
                "holder_num": row.get("HOLDER_NUM", 0),
                "change_ratio_pct": round(ratio, 2),
            })
        return out
    rows = _daily_cached_json("holder", code, asof_iso, _fetch)
    if not rows:
        return 0.0, None
    latest = rows[0]
    # 时效护栏：部分标的披露严重滞后（如 601919 最新一期是 2017 年），
    # 超过 18 个月的"环比"不再反映当前筹码结构，不给分。
    age_days = _age_days(latest.get("published_at"), asof_iso)
    if age_days is None:
        return 0.0, None
    if age_days > 550:
        return 0.0, None
    change = float(latest.get("change_ratio_pct") or 0)
    if change <= -2.0:
        return 5.0, f"股东户数{latest.get('date')}环比{change:.1f}%（筹码集中）"
    if change >= 5.0:
        return -5.0, f"股东户数{latest.get('date')}环比+{change:.1f}%（筹码分散）"
    return 0.0, None


def ths_eps_forecast(code, cache_days=7, asof_day=None):
    """同花顺机构一致预期 EPS（HTML 表格解析，脆弱，仅上下文用途）。

    周级磁盘缓存（预期变化极慢）。返回 {"table": [...], "fetched_at"} 或
    None（解析失败 fail-open）。"均值" 即机构一致预期。
    """
    # The page exposes only the current consensus, not a historical snapshot.
    # It is therefore invalid evidence for a past replay date.
    asof_iso = _asof_iso(asof_day)
    if asof_iso != dt.date.today().isoformat():
        return None
    week = dt.date.today().isocalendar()
    cache_key = f"{code}_{week[0]}W{week[1]}"
    path = os.path.join(CACHE_DIR, f"eps_{cache_key}.json")
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return json.load(handle) or None
    except (OSError, ValueError):
        pass
    result = None
    try:
        from io import StringIO
        url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
        r = requests.get(url, headers={
            "User-Agent": UA, "Referer": "https://basic.10jqka.com.cn/",
        }, timeout=15)
        r.encoding = "gbk"
        if pd is None:
            return None
        dfs = pd.read_html(StringIO(r.text))
        target = None
        for frame in dfs:
            cols = [str(c) for c in frame.columns]
            if any("每股收益" in c or "均值" in c for c in cols):
                target = frame
                break
        if target is None and dfs:
            target = dfs[0]
        if target is not None:
            result = {
                "table": target.to_dict(orient="records")[:6],
                "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
    except Exception:
        result = None
    if result:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass
    return result
