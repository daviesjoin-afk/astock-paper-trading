# -*- coding: utf-8 -*-
"""模拟盘成交行情的新鲜度、活跃度与核验门禁。"""
from __future__ import annotations

import datetime as dt
import math

BUSINESS_TIMEZONE = dt.timezone(dt.timedelta(hours=8))
#: 双源核验的时间口径：两源是两次独立的 HTTP 采样，不是同一时刻的同一份报价。
CROSS_TIGHT_SKEW_SECONDS = 20.0
CROSS_RELAX_LIMIT_SECONDS = 90.0
CROSS_PRICE_GAP_TOLERANCE = 0.005
CROSS_PCT_GAP_TOLERANCE = 0.20
CROSS_RELAXED_PRICE_GAP_LIMIT = 0.020
#: 平盘带：两源涨跌幅都落在这个带内时，方向一致性没有意义，不判方向冲突。
CROSS_FLAT_PCT_BAND = 0.5


def parse_quote_instant(value):
    """解析行情时间戳：ISO 串或 14 位数字，无法解析返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        pass
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) >= 14:
        try:
            return dt.datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
        except ValueError:
            return None
    return None


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _business_aware(instant):
    return instant if instant.tzinfo is not None else instant.replace(tzinfo=BUSINESS_TIMEZONE)


def _cross_date_ok(cross_time, cross_at, expected_day):
    if cross_time is not None:
        return cross_time.date() == expected_day
    text = str(cross_at or "")
    prefix = text[:10].replace("-", "") if "-" in text[:10] else text[:8]
    return bool(text) and prefix == expected_day.strftime("%Y%m%d")


def cross_source_verdict(
    primary_price, primary_pct, primary_at, cross_price, cross_pct, cross_at, *,
    expected_day, primary_live=True, now=None, live_error=None, cross_error=None,
):
    """按两个采样时刻的时滞判定双源核验结果（纯函数，不联网、不读账本）。

    旧判定假设两个源给的是"同一时刻"的报价，把两次相距数十秒的 HTTP 采样
    当成同一份快照来比：急跌 60 秒里 1.6% 的价差是采样时滞，不是数据错误，
    于是系统性写出"双源核验未通过"。这里把时滞本身变成判定输入：
    |Δt| ≤ 20s 用原阈值；20s < |Δt| ≤ 90s 按每 20 秒 0.5pp 线性放宽价差
    （20s→0.5%，60s→1.5%，80s 起触到 2.0% 上限并保持），同时额外要求两源
    方向一致；|Δt| > 90s、时间戳缺失、跨日、备源缺失维持原行为。
    ``live_error``/``cross_error`` 是两源的传输异常原文，只用于保留原始证据。
    """
    price = _finite_number(primary_price)
    pct = _finite_number(primary_pct)
    cross_value = _finite_number(cross_price)
    cross_pct_value = _finite_number(cross_pct)
    primary_ok = (bool(primary_live) and price is not None and price > 0
                  and pct is not None and abs(pct) <= 30)
    available = (cross_value is not None and cross_value > 0
                 and cross_pct_value is not None and abs(cross_pct_value) <= 30
                 and bool(str(cross_at or "").strip()))
    price_gap = (abs(price - cross_value) / cross_value
                 if price is not None and cross_value is not None and cross_value > 0
                 else None)
    pct_gap = (abs(pct - cross_pct_value)
               if pct is not None and cross_pct_value is not None else None)
    primary_time = parse_quote_instant(primary_at)
    cross_time = parse_quote_instant(cross_at)
    time_skew = None
    if primary_time is not None and cross_time is not None:
        time_skew = abs((_business_aware(cross_time) - _business_aware(primary_time)).total_seconds())
    date_ok = _cross_date_ok(cross_time, cross_at, expected_day)
    time_ok = False
    if date_ok and cross_time is not None:
        if expected_day != dt.date.today():
            # 历史回放只校验源日期一致，不校验已归档报价的墙钟新鲜度。
            time_ok = True
        else:
            current = now if now is not None else dt.datetime.now(cross_time.tzinfo)
            seconds_behind = (_business_aware(current) - _business_aware(cross_time)).total_seconds()
            time_ok = -120 <= seconds_behind <= 20 * 60
    scale = 2.0 if pct is not None and abs(pct) >= 3.0 else 1.0
    relaxed = time_skew is not None and CROSS_TIGHT_SKEW_SECONDS < time_skew <= CROSS_RELAX_LIMIT_SECONDS
    if relaxed:
        price_tolerance = min(
            CROSS_RELAXED_PRICE_GAP_LIMIT,
            CROSS_PRICE_GAP_TOLERANCE * time_skew / CROSS_TIGHT_SKEW_SECONDS,
        ) * scale
    else:
        price_tolerance = CROSS_PRICE_GAP_TOLERANCE * scale
    pct_tolerance = CROSS_PCT_GAP_TOLERANCE * scale
    direction_ok = (pct is not None and cross_pct_value is not None and (
        (pct > 0 and cross_pct_value > 0) or (pct < 0 and cross_pct_value < 0)
        or (abs(pct) <= CROSS_FLAT_PCT_BAND and abs(cross_pct_value) <= CROSS_FLAT_PCT_BAND)))
    degraded = False
    if live_error and not primary_live:
        validation, reason, relaxed = "source_error", f"主行情源异常：{live_error}", False
    elif not primary_ok:
        validation, reason, relaxed = "incomplete", "主行情价格或涨跌幅无效", False
    elif cross_error:
        validation, reason, relaxed = "range_timestamp_checked", f"独立行情源异常：{cross_error}", False
    elif not available:
        validation, reason, relaxed = "range_timestamp_checked", "独立行情源未返回有效价格或时间", False
    elif not date_ok:
        validation = "range_timestamp_checked"
        reason, relaxed = f"独立行情源时间不是交易日（{cross_at or '未知'}）", False
    elif not time_ok:
        validation, reason, relaxed = "range_timestamp_checked", "独立行情源更新时间过期，已等待其重连/切换", False
    elif relaxed and not direction_ok:
        validation = "cross_source_failed"
        reason = (f"双源涨跌方向不一致（{pct:+.2f}% vs {cross_pct_value:+.2f}%），"
                  f"采样时滞 {time_skew:.0f}s 不予放宽")
    elif price_gap is None or pct_gap is None:
        validation, reason = "cross_source_failed", "双源差异无法计算"
    elif price_gap > price_tolerance + 1e-12 or pct_gap > pct_tolerance + 1e-12:
        validation = "cross_source_failed"
        if relaxed:
            reason = (f"双源差异超阈值（已按 {time_skew:.0f}s 采样时滞放宽至 "
                      f"{price_tolerance * 100:.2f}%/{pct_tolerance:.2f}pp）："
                      f"价格差 {price_gap * 100:.3f}%、涨跌幅差 {pct_gap:.3f}%")
        else:
            reason = f"双源差异超阈值：价格差 {price_gap * 100:.3f}%、涨跌幅差 {pct_gap:.3f}%"
    else:
        validation = "cross_source_checked"
        degraded = relaxed
        reason = (f"按 {time_skew:.0f}s 采样时滞放宽双源核验通过："
                  f"价格差 {price_gap * 100:.3f}%、涨跌幅差 {pct_gap:.3f}%" if relaxed else None)
    return {
        "validation": validation,
        "reason": reason,
        "price_gap_pct": round(price_gap * 100, 3) if price_gap is not None else None,
        "pct_gap": round(pct_gap, 3) if pct_gap is not None else None,
        "time_skew_seconds": round(time_skew, 1) if time_skew is not None else None,
        "degraded": degraded,
        "relaxed": relaxed,
        "available": available,
        "date_ok": date_ok,
        "time_ok": time_ok,
        "passed": validation == "cross_source_checked",
        "price_tolerance_pct": round(price_tolerance * 100, 3),
        "pct_tolerance": round(pct_tolerance, 3),
    }


def quote_is_fresh(
    quote, asof_date, *, date_fn, today_fn=dt.date.today, now_fn=dt.datetime.now,
    reference_at=None,
):
    """只有带源时间戳的当日公开行情才可触发成交。"""
    if not quote or quote.get("quote_source") != "live":
        return False
    try:
        text = str(quote.get("quote_at") or "")
        quote_time = (dt.datetime.strptime(text, "%Y%m%d%H%M%S")
                      if text.isdigit() and len(text) == 14 else dt.datetime.fromisoformat(text))
    except (TypeError, ValueError):
        return False
    day = date_fn(asof_date)
    if quote_time.date() != day:
        return False
    if reference_at is not None:
        try:
            reference_time = dt.datetime.fromisoformat(
                str(reference_at).strip().replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return False
        business_timezone = dt.timezone(dt.timedelta(hours=8))
        if quote_time.tzinfo is None:
            quote_time = quote_time.replace(tzinfo=business_timezone)
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=business_timezone)
        age_seconds = (reference_time - quote_time).total_seconds()
        return -120 <= age_seconds <= 20 * 60
    # 回放历史日期时只校验源日期；当日运行还要防止接口返回长时间未更新的收盘价。
    if day != today_fn():
        return True
    now = now_fn(quote_time.tzinfo) if quote_time.tzinfo else now_fn()
    age_seconds = (now - quote_time).total_seconds()
    return -120 <= age_seconds <= 20 * 60


def is_trading_active(quote, *, num):
    """判断股票是否在活跃交易（非停牌）。"""
    pct = num(quote.get("pct"), None)
    amount = num(quote.get("amount"), 0)
    turnover = num(quote.get("turnover"), 0)
    volume = num(quote.get("volume"), 0)
    return not (pct == 0 and amount == 0 and turnover == 0 and volume == 0)


def execution_quote_status(
    quote, asof_date, purpose="entry", *, quote_fresh, reference_at=None,
):
    """自动成交行情门禁，不联网、不读账本。"""
    if not quote:
        return {"fresh": False, "status": "missing", "reason": "缺少行情"}
    cross = dict(quote.get("quote_cross_check") or {})
    validation = str(quote.get("quote_validation") or "")
    diagnostics = {
        "quote_source": quote.get("quote_source"),
        "quote_validation": validation or None,
        "quote_at": quote.get("quote_at"),
        "quote_cross_check": cross,
    }
    if validation == "incomplete":
        return {"fresh": False, "status": "invalid", "reason": "主行情价格或涨跌幅无效", **diagnostics}
    if quote.get("quote_source") != "live":
        return {
            "fresh": False,
            "status": "local_cache",
            "reason": "行情来自本地缓存，禁止虚构自动成交",
            "quote_at": quote.get("quote_at"),
            **diagnostics,
        }
    fresh = (
        quote_fresh(quote, asof_date)
        if reference_at is None else
        quote_fresh(quote, asof_date, reference_at=reference_at)
    )
    if not fresh:
        return {
            "fresh": False,
            "status": "stale",
            "reason": "实时行情源时间戳已过期，等待下一次有效报价",
            "quote_at": quote.get("quote_at"),
            **diagnostics,
        }
    if validation == "cross_source_checked":
        return {
            "fresh": True,
            "status": "cross_source_checked",
            "reason": "带当日源时间戳且双源核验通过的实时行情",
            "quote_at": quote.get("quote_at"),
            **diagnostics,
        }
    if validation == "cross_source_failed":
        detail = cross.get("failure_reason") or "独立行情源返回结果与主行情不一致"
        if purpose == "exit":
            # 两个源分歧只说明"哪一份报价更准"没有定论，不说明"该不该卖"没有定论。
            # 风控退出按主行情定价并保留分歧证据；把止损停在源分歧上等于让行情
            # 抖动代替风控做决定。
            return {
                "fresh": True, "status": "degraded_cross_source",
                "reason": detail + "；风控退出按主行情定价并记录源分歧", "degraded": True,
                **diagnostics,
            }
        return {"fresh": False, "status": "cross_source_failed", "reason": detail, **diagnostics}
    if validation == "cross_source_unavailable":
        detail = cross.get("failure_reason") or "本次未获得独立行情源的有效返回"
        if purpose == "exit":
            return {
                "fresh": True, "status": "degraded_cross_source",
                "reason": detail + "；仅允许风控退出", "degraded": True, **diagnostics,
            }
        return {
            "fresh": False, "status": "cross_source_unavailable",
            "reason": detail + "；自动买入等待双源恢复", **diagnostics,
        }
    if validation == "range_timestamp_checked":
        price, pct = quote.get("price"), quote.get("pct")
        if (isinstance(price, bool) or isinstance(pct, bool)
                or not isinstance(price, (int, float)) or not isinstance(pct, (int, float))
                or not math.isfinite(price) or not math.isfinite(pct) or price <= 0 or abs(pct) > 30):
            return {"fresh": False, "status": "invalid", "reason": "单源报价价格或涨跌幅无效", **diagnostics}
        return {
            "fresh": True, "status": "range_timestamp_checked",
            "reason": "单个行情源的价格、涨跌幅和源时间有效，保留单源标记",
            "degraded": True, **diagnostics,
        }
    return {"fresh": False, "status": "unverified", "reason": "未获得通过的独立行情源校验结果", **diagnostics}
