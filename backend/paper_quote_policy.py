# -*- coding: utf-8 -*-
"""模拟盘成交行情的新鲜度、活跃度与核验门禁。"""
from __future__ import annotations

import datetime as dt


def quote_is_fresh(quote, asof_date, *, date_fn, today_fn=dt.date.today, now_fn=dt.datetime.now):
    """只有带源时间戳的当日公开行情才可触发成交。"""
    if not quote or quote.get("quote_source") != "live":
        return False
    try:
        quote_time = dt.datetime.fromisoformat(str(quote.get("quote_at") or ""))
    except (TypeError, ValueError):
        return False
    day = date_fn(asof_date)
    if quote_time.date() != day:
        return False
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


def execution_quote_status(quote, asof_date, purpose="entry", *, quote_fresh):
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
    if not quote_fresh(quote, asof_date):
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
    if validation == "range_timestamp_checked" and purpose == "exit":
        return {
            "fresh": True, "status": "degraded_cross_source",
            "reason": "主行情新鲜有效，但未完成独立行情源校验；仅允许风控退出",
            "degraded": True, **diagnostics,
        }
    return {"fresh": False, "status": "unverified", "reason": "未获得通过的独立行情源校验结果", **diagnostics}
