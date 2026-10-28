# -*- coding: utf-8 -*-
"""公开行情 provider 的纯文本解析器。

网络请求、重试和源切换仍由 `data_fetcher` 编排；本模块只把腾讯/新浪
响应转换成统一的原始报价行，便于 provider 单元测试和后续适配器替换。
"""
from __future__ import annotations

import json
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
    timeout=10,
    retries=1,
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
                    timeout=timeout, retries=retries,
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


def fetch_eastmoney_concept_members(
    *,
    get_json,
    hosts,
    board_code,
    ut,
    quote_at,
    finite_number,
    max_pages=8,
):
    """Fetch and normalize one Eastmoney concept board's reported members.

    This remains an adapter: callers inject transport and domain normalizers,
    while the adapter guarantees that a capped or failed page set is marked
    incomplete instead of being silently promoted to strategy evidence.
    """
    fields = "f2,f3,f8,f10,f12,f14,f62,f66,f100,f124,f184"
    host_list = list(hosts)

    def _one_page(page):
        params = {
            "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "ut": ut, "fid": "f62", "fs": f"b:{board_code}+f:!50",
            "fields": fields,
        }
        for host in host_list:
            try:
                payload = get_json(
                    f"https://{host}/api/qt/clist/get", params,
                    timeout=5, retries=0,
                )
                data = (payload or {}).get("data") or {}
                rows = data.get("diff") or []
                if rows:
                    return rows, int(data.get("total") or len(rows)), True
            except Exception:
                continue
        return [], 0, False

    first, total, ok = _one_page(1)
    if not ok:
        return {
            "members": [], "expected_count": 0, "pages_ok": 0,
            "pages_expected": 0, "complete": False,
        }
    pages_expected = max(1, (total + 99) // 100)
    pages_to_fetch = min(pages_expected, max(1, int(max_pages)))
    results = {1: first}
    if pages_to_fetch > 1:
        with ThreadPoolExecutor(max_workers=min(4, pages_to_fetch - 1)) as pool:
            futures = {
                pool.submit(_one_page, page): page
                for page in range(2, pages_to_fetch + 1)
            }
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
            price = finite_number(item.get("f2"))
            pct = finite_number(item.get("f3"))
            if not code or code in seen or price is None or pct is None:
                malformed += 1
                continue
            seen.add(code)
            members.append({
                "code": code, "name": item.get("f14"), "price": price, "pct": pct,
                "turnover": finite_number(item.get("f8")),
                "vol_ratio": finite_number(item.get("f10")), "industry": item.get("f100"),
                "main_net": finite_number(item.get("f62")),
                "super_net": finite_number(item.get("f66")),
                "main_pct": finite_number(item.get("f184")),
                "quote_ts": item.get("f124"), "quote_at": quote_at(item.get("f124")),
            })
    complete = (
        pages_expected <= max_pages
        and len(results) == pages_expected
        and len(members) >= max(1, total - malformed)
    )
    return {
        "members": members, "expected_count": total, "fetched_count": len(members),
        "pages_ok": len(results), "pages_expected": pages_expected,
        "malformed_rows": malformed, "complete": bool(complete),
    }


def parse_eastmoney_concept_refs(rows, *, excluded_tokens=()):
    """Normalize a stock-to-concept response without inferring theme tags."""
    if isinstance(rows, dict):
        rows = rows.values()
    refs, seen = [], set()
    for row in rows or []:
        board_code = str(row.get("f12") or "")
        name = str(row.get("f14") or "").strip()
        if (
            not board_code.startswith("BK")
            or not name
            or board_code in seen
            or any(token in name for token in excluded_tokens)
        ):
            continue
        seen.add(board_code)
        refs.append({"code": board_code, "name": name})
    return refs


def _tencent_volume_amount_turnover(parts, price):
    """从腾讯 ``qt.gtimg.cn`` 报文取成交量/成交额/换手率，并做自洽校验。

    为什么需要：价格-only 回退（东财 clist 返回 0 行时启用）此前只解析价格与
    涨跌幅，于是选股因子表的 ``amount`` / ``turnover`` **整列缺失**，每轮都打
    ``factor_columns_missing`` 告警，相关因子只能整列按 unknown 处理。但腾讯
    报文本来就带这些字段（实测 sh600000：``[6]`` 成交量(手)、``[37]`` 成交额
    (万元)、``[38]`` 换手率(%)），只是没被解析。

    公开报文格式改过版，所以不盲信下标：先按位置取值，再用
    ``volume × 100 × price ≈ amount`` 做量纲/错位交叉校验；不通过就返回
    ``None``（保持 unknown）。这个仓库的口径是"未知不等于零"，也不等于一个
    猜出来的数。单位对齐东财 clist：``amount`` 用元（万元 × 10000），
    ``turnover`` 用百分数（与 f8 同口径）。
    """
    def _num_at(index):
        if index >= len(parts):
            return None
        try:
            value = float(str(parts[index] or "").strip())
        except (TypeError, ValueError):
            return None
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value

    volume_lots = _num_at(6)      # 手
    amount_wan = _num_at(37)      # 万元
    turnover = _num_at(38)        # %
    if volume_lots is not None and volume_lots < 0:
        volume_lots = None
    if price is None or price <= 0:
        volume_lots = None
    amount = None
    if amount_wan is not None and amount_wan >= 0:
        candidate = amount_wan * 10000.0
        if volume_lots:
            implied = volume_lots * 100.0 * price
            # 日内价格会漂移，允许 60% 偏差；超过即认为字段错位或量纲不同。
            if implied > 0 and abs(implied - candidate) / max(candidate, 1.0) <= 0.60:
                amount = candidate
        elif candidate > 0:
            amount = candidate
    if turnover is not None and not (0.0 <= turnover <= 100.0):
        turnover = None
    return volume_lots, amount, turnover


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
            volume_lots, amount, turnover = _tencent_volume_amount_turnover(parts, price)
            rows_by_code[code] = {
                "code": code, "name": parts[1], "price": price, "prev_close": prev_close,
                "pct": pct, "quote_at": quote_at,
                # 取不到的字段不出现在行里，下游继续按 unknown 处理。
                **({"volume": volume_lots} if volume_lots is not None else {}),
                **({"amount": amount} if amount is not None else {}),
                **({"turnover": turnover} if turnover is not None else {}),
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
            **_sina_volume_amount(values),
            "source": "sina_public_quote",
        }
    return list(rows_by_code.values())


def _sina_volume_amount(values):
    """新浪行情里的成交量(股)与成交额(元)；取不到或自相矛盾时不写字段。

    与腾讯同一动机：价格-only 回退不该让因子表的 ``amount`` 整列变 unknown。
    新浪 ``hq.sinajs.cn`` 的成交量在 ``[8]``、成交额在 ``[9]``，仍做一次
    ``volume × price ≈ amount`` 的交叉校验，错位就保持 unknown。
    """
    def _num_at(index):
        if index >= len(values):
            return None
        try:
            value = float(str(values[index] or "").strip())
        except (TypeError, ValueError):
            return None
        if value != value or value in (float("inf"), float("-inf")) or value < 0:
            return None
        return value

    volume_shares = _num_at(8)
    amount = _num_at(9)
    if amount is not None and amount == 0:
        amount = None
    price = _num_at(3)
    if amount is not None and volume_shares and price and price > 0:
        implied = volume_shares * price
        if implied > 0 and abs(implied - amount) / max(amount, 1.0) > 0.60:
            amount = None
    out = {}
    if volume_shares is not None:
        out["volume"] = volume_shares / 100.0  # 对齐腾讯"手"的口径
    if amount is not None:
        out["amount"] = amount
    return out


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


def parse_ths_realtime_text(text, *, allowed_codes=None):
    """解析同花顺 ``d.10jqka.com.cn/v6/time/hs_<code>/last.js`` 分时快照。

    这是独立于东财/腾讯/新浪的第四方报价源，只在两家独立源都缺价时使用。
    报文形如::

        quotebridge_v6_time_hs_600000_last({"hs_600000": {
            "name": "浦发银行", "pre": "9.82", "date": "20261028",
            "data": "0930,9.80,4071900,9.800,415500;0931,..."}})

    ``data`` 是当日分钟序列 ``时间,价格,成交额,均价,成交量``。我们只取最后
    一根的**价格**并用 ``pre``（昨收）计算涨跌幅；成交额/均价的单位没有公开
    契约，宁可不填，也不把猜测值写进报价。时间戳由 ``date`` + 最后一根
    ``HHMM`` 组合为 +08:00，供调用方做同日/时效校验。
    """
    allowed = {str(code) for code in allowed_codes} if allowed_codes is not None else None
    raw = str(text or "")
    start, end = raw.find("("), raw.rfind(")")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(raw[start + 1:end])
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    rows = []
    for key, item in payload.items():
        if not isinstance(item, dict):
            continue
        code = str(key).split("_")[-1][-6:]
        if not code.isdigit() or len(code) != 6:
            continue
        if allowed is not None and code not in allowed:
            continue
        series = [part for part in str(item.get("data") or "").split(";") if part]
        if not series:
            continue
        parts = str(series[-1]).split(",")
        if len(parts) < 2:
            continue
        try:
            price = float(parts[1])
            prev_close = float(item.get("pre"))
        except (TypeError, ValueError):
            continue
        if price <= 0 or prev_close <= 0:
            continue
        day = str(item.get("date") or "")
        clock = str(parts[0]).strip()
        quote_at = None
        if re.fullmatch(r"\d{8}", day) and re.fullmatch(r"\d{4}", clock):
            quote_at = (
                f"{day[:4]}-{day[4:6]}-{day[6:8]}T"
                f"{clock[:2]}:{clock[2:]}:00+08:00"
            )
        if not quote_at:
            continue
        rows.append({
            "code": code, "name": item.get("name"), "price": price,
            "prev_close": prev_close,
            "pct": round((price - prev_close) / prev_close * 100, 4),
            "quote_at": quote_at, "source": "ths_public_quote",
        })
    return rows
