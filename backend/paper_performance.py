# -*- coding: utf-8 -*-
"""模拟盘今日盈亏与报价可用性的无副作用计算。"""
from __future__ import annotations

import datetime as dt


TODAY_PNL_QUOTE_SOURCES = frozenset({"live", "dashboard_cache", "live_snapshot"})
MARKET_TZ = dt.timezone(dt.timedelta(hours=8))


def _market_now():
    """Return the current time in the A-share market timezone."""
    return dt.datetime.now(MARKET_TZ)


def _market_today():
    return _market_now().date()


def quote_is_usable(quote, asof_day, *, date_fn):
    """Require a same-day, bounded-age mark before publishing daily P&L."""
    if not isinstance(quote, dict) or quote.get("quote_source") not in TODAY_PNL_QUOTE_SOURCES:
        return False
    quote_at = str(quote.get("quote_at") or "")
    if quote_at[:10] != date_fn(asof_day).isoformat():
        return False
    try:
        parsed = dt.datetime.fromisoformat(quote_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
        if date_fn(asof_day) != _market_today():
            return True
        age_seconds = (_market_now() - parsed.astimezone(MARKET_TZ)).total_seconds()
        return -120 <= age_seconds <= 20 * 60
    except (TypeError, ValueError, OverflowError):
        return False


def position_performance(position, price, quote, asof_day=None, *, date_fn, market_session, num):
    """按当日买入成本与隔夜昨收分别计算持仓今日盈亏。"""
    day = date_fn(asof_day).isoformat()
    if day == _market_today().isoformat() and not market_session()["today_pnl_available"]:
        return None, None, None
    if not quote_is_usable(quote, asof_day, date_fn=date_fn):
        return None, None, None
    qty = int(num(position.get("qty")))
    if qty <= 0 or price <= 0:
        return None, None, None
    bought_today = min(int(num(position.get("today_acquired_qty"))), qty)
    today_cost = num(position.get("today_acquired_cost"))
    carried_qty = qty - bought_today
    quote_pct = num(quote.get("pct"), None)
    if carried_qty and (quote_pct is None or quote_pct <= -99.9):
        return None, None, None
    previous_close = num(quote.get("previous_close"), 0.0) if carried_qty else 0.0
    if carried_qty and previous_close <= 0:
        previous_close = price / (1 + quote_pct / 100)
    baseline = today_cost + carried_qty * previous_close
    pnl = (price * bought_today - today_cost) + (price - previous_close) * carried_qty
    return round(pnl, 2), (round(pnl / baseline * 100, 2) if baseline else None), round(baseline, 2)


def sell_performance(sells, quotes, asof_day=None, *, date_fn, num):
    """计算当日卖出隔夜仓的贡献，不重复计入生命周期收益。"""
    pnl = 0.0
    baseline = 0.0
    covered = 0
    missing = []
    for order in sells:
        quote = quotes.get(str(order.get("code") or "")) or {}
        if not quote_is_usable(quote, asof_day, date_fn=date_fn):
            missing.append(str(order.get("code") or ""))
            continue
        price = num(quote.get("price"), 0.0)
        pct = num(quote.get("pct"), None)
        qty = int(num(order.get("qty")))
        fill_price = num(order.get("filled_price"), 0.0)
        if price <= 0 or pct is None or pct <= -99.9 or qty <= 0 or fill_price <= 0:
            missing.append(str(order.get("code") or ""))
            continue
        previous_close = num(quote.get("previous_close"), 0.0)
        if previous_close <= 0:
            previous_close = price / (1 + pct / 100)
        basis = previous_close * qty
        proceeds = fill_price * qty - num(order.get("fees"))
        pnl += proceeds - basis
        baseline += basis
        covered += 1
    return {
        "pnl": round(pnl, 2),
        "baseline": round(baseline, 2),
        "covered": covered,
        "total": len(sells),
        "missing_codes": sorted({code for code in missing if code}),
    }
