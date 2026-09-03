# -*- coding: utf-8 -*-
"""公开行情 provider 的纯文本解析器。

网络请求、重试和源切换仍由 `data_fetcher` 编排；本模块只把腾讯/新浪
响应转换成统一的原始报价行，便于 provider 单元测试和后续适配器替换。
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd


def fetch_eastmoney_clist(
    *,
    get_json,
    hosts,
    host_health,
    fields,
    fid="f20",
    pages=None,
    pz=200,
    return_meta=False,
    ut,
    fs,
    cooldown_base=30,
    max_cooldown=300,
    now=time.time,
):
    """Fetch paginated Eastmoney ``clist`` rows through an injected transport.

    The caller deliberately owns HTTP sessions and the mutable per-host health
    dictionary.  Keeping those concerns injected makes this provider adapter
    independently testable while preserving the existing fail-closed metadata
    contract used by full-market snapshots.
    """
    host_list = list(hosts)

    def _host_ok(host):
        info = host_health.get(host)
        return not (info and now() < info.get("cooldown_until", 0))

    def _host_ok_set(host):
        host_health[host] = {"failures": 0, "cooldown_until": 0}

    def _host_fail(host):
        info = host_health.get(host, {"failures": 0, "cooldown_until": 0})
        failures = info["failures"] + 1
        host_health[host] = {
            "failures": failures,
            "cooldown_until": now() + min(
                cooldown_base * (2 ** (failures - 1)), max_cooldown
            ),
        }

    def _one_page(page):
        params = {
            "pn": page, "pz": pz, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "ut": ut, "fid": fid, "fs": fs, "fields": fields,
        }
        ordered = [host for host in host_list if _host_ok(host)]
        ordered += [host for host in host_list if not _host_ok(host)]
        for host in ordered:
            try:
                response = get_json(
                    f"https://{host}/api/qt/clist/get", params,
                    timeout=10, retries=1,
                )
                data = (response or {}).get("data") or {}
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

    actual_pz = max(len(first), 1)
    effective_pz = min(pz, actual_pz)
    total_pages = (total + effective_pz - 1) // effective_pz if total else 1
    if pages:
        total_pages = min(pages, total_pages)
    expected_pages = total_pages
    page_rows = {1: list(first)}
    failed_pages = []
    if total_pages > 1:
        with ThreadPoolExecutor(max_workers=min(16, total_pages - 1)) as executor:
            futures = {
                executor.submit(_one_page, page): page
                for page in range(2, total_pages + 1)
            }
            for future in as_completed(futures):
                page = futures[future]
                try:
                    diff, _ = future.result()
                except Exception:
                    diff = []
                if diff:
                    page_rows[page] = list(diff)
                else:
                    failed_pages.append(page)
    rows = []
    for page in sorted(page_rows):
        rows.extend(page_rows[page])
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


def parse_tencent_realtime_text(text, *, attempt=1, allowed_codes=None):
    """解析腾讯 ``qt.gtimg.cn`` 文本响应。"""
    allowed = {str(code) for code in allowed_codes} if allowed_codes is not None else None
    rows_by_code = {}
    for line in str(text or "").strip().split(";"):
        if "=" not in line:
            continue
        raw_code = line.split("=", 1)[0].replace("v_", "").strip()
        code = raw_code[-6:]
        parts = line.split("=", 1)[1].strip().strip('"').split("~")
        if len(parts) < 33 or not code.isdigit() or len(code) != 6:
            continue
        if allowed is not None and code not in allowed:
            continue
        try:
            price = float(parts[3] or 0)
            prev_close = float(parts[4] or 0)
            pct = float(parts[32] or 0)
        except (TypeError, ValueError):
            continue
        # The public schema has changed field positions before; locate the
        # 14-digit quote timestamp instead of trusting a fixed index.
        quote_at = next(
            (part for part in parts if re.fullmatch(r"\d{14}", str(part or "").strip())),
            None,
        )
        if price > 0:
            rows_by_code[code] = {
                "code": code, "name": parts[1], "price": price, "prev_close": prev_close,
                "pct": pct, "quote_at": quote_at,
                "source": "tencent_public_quote", "attempt": attempt,
            }
    return list(rows_by_code.values())


def parse_sina_realtime_text(text, *, allowed_codes=None):
    """解析新浪 ``hq.sinajs.cn`` 文本响应。"""
    allowed = {str(code) for code in allowed_codes} if allowed_codes is not None else None
    rows_by_code = {}
    for line in str(text or "").strip().split(";"):
        if "=" not in line:
            continue
        symbol = line.split("=", 1)[0].strip().split("_")[-1]
        code = symbol[-6:]
        if allowed is not None and code not in allowed:
            continue
        values = line.split("=", 1)[1].strip().strip('"').rstrip(";").strip('"').split(",")
        if len(values) < 10:
            continue
        try:
            price = float(values[3] or 0)
            prev_close = float(values[2] or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or prev_close <= 0:
            continue
        quote_date = ""
        quote_clock = ""
        # Different boards append different field counts; locate date/time by
        # format rather than relying on a fixed offset from the end.
        for idx, value in enumerate(values[:-1]):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value or "").strip()):
                if idx + 1 < len(values) and re.fullmatch(
                    r"\d{2}:\d{2}:\d{2}", str(values[idx + 1] or "").strip()
                ):
                    quote_date = str(value).strip()
                    quote_clock = str(values[idx + 1]).strip()
                    break
        rows_by_code[code] = {
            "code": code,
            "name": values[0],
            "price": price,
            "prev_close": prev_close,
            "pct": round((price - prev_close) / prev_close * 100, 4),
            "quote_at": f"{quote_date}T{quote_clock}+08:00" if quote_date and quote_clock else None,
            "source": "sina_public_quote",
        }
    return list(rows_by_code.values())


def parse_tencent_kline_rows(raw):
    """解析腾讯日线数组并估算成交额/振幅。"""
    rows = []
    previous_close = None
    for item in raw or []:
        if len(item) < 6:
            continue
        date, open_, close, high, low, volume = item[:6]
        open_, close, high, low, volume = map(float, (open_, close, high, low, volume))
        typical = (open_ + close + high + low) / 4
        amplitude_base = previous_close or close
        rows.append({
            "date": date,
            "open": open_,
            "close": close,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": volume * 100 * typical,
            "amplitude": (high - low) / amplitude_base * 100 if amplitude_base else None,
        })
        previous_close = close
    return rows


def parse_sina_kline_rows(payload, beg, end):
    """解析新浪不复权日线并按请求区间过滤。"""
    start = pd.Timestamp(str(beg))
    finish = pd.Timestamp(str(end))
    rows = []
    for item in payload or []:
        date = pd.Timestamp(item.get("day"))
        if date < start or date > finish:
            continue
        open_ = float(item["open"])
        close = float(item["close"])
        high = float(item["high"])
        low = float(item["low"])
        volume = float(item["volume"])
        typical = (open_ + close + high + low) / 4
        rows.append({
            "date": str(date.date()),
            "open": open_,
            "close": close,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": volume * typical,
            "amplitude": (high - low) / close * 100 if close else None,
        })
    return rows


def parse_eastmoney_kline_rows(klines):
    """解析东财逗号分隔日线数组。"""
    rows = []
    for kline in klines or []:
        parts = kline.split(",")
        rows.append({
            "date": parts[0], "open": float(parts[1]), "close": float(parts[2]),
            "high": float(parts[3]), "low": float(parts[4]),
            "volume": float(parts[5]), "amount": float(parts[6]),
            "amplitude": float(parts[7]) if len(parts) > 7 else None,
        })
    return rows
