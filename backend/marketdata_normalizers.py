# -*- coding: utf-8 -*-
"""行情 provider 输出的无副作用标准化函数。

这些函数只处理字段映射、数值清洗、证券代码和 K 线 DataFrame 整形；不发
请求、不读写缓存，便于 provider 适配和独立测试。
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd


def finite_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def stock_secid(code):
    """Return EastMoney's market-prefixed identifier for an A-share code."""
    code = str(code or "").strip().zfill(6)
    if not code.isdigit() or len(code) != 6:
        return None
    return f"{'1' if code.startswith(('5', '6', '9')) else '0'}.{code}"


def secid(code):
    code = str(code)
    # 北交所 920 新代码仍属于东财 market=0；必须在通用的 9 开头沪市判断之前处理。
    if code.startswith("920"):
        return f"0.{code}"
    if code.startswith(("6", "9", "5")):
        return f"1.{code}"
    return f"0.{code}"


def quote_at(value):
    """把东财 f124 Unix 时间转换为中国市场时区的可审计时间。"""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    china_tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.fromtimestamp(value, china_tz).isoformat(timespec="seconds")


def sanitize_market_row(row):
    """Drop impossible OHLC values instead of feeding them to risk models."""
    price = finite_number(row.get("price"))
    if price is None or price <= 0:
        return row
    for key in ("open_price", "high", "low", "prev_close"):
        value = finite_number(row.get(key))
        if value is None or value <= 0 or value / price < 0.20 or value / price > 5.0:
            row[key] = None
    high, low = finite_number(row.get("high")), finite_number(row.get("low"))
    if high is not None and low is not None and high < low:
        row["high"], row["low"] = None, None
    return row


def realtime_row_from_ulist(raw, *, quote_at_fn=quote_at):
    """Normalize one Eastmoney ulist row for primary and fallback paths."""
    if not isinstance(raw, dict):
        return None
    code = str(raw.get("f12") or "")
    if not code:
        return None
    return {
        "code": code, "name": raw.get("f14"),
        "price": raw.get("f2"), "pct": raw.get("f3"),
        "open_price": raw.get("f17"), "high": raw.get("f15"),
        "low": raw.get("f16"), "prev_close": raw.get("f18"),
        "volume": raw.get("f5"), "amount": raw.get("f6"),
        "turnover": raw.get("f8"), "pe": raw.get("f9"), "vol_ratio": raw.get("f10"),
        "mktcap": raw.get("f20"), "float_cap": raw.get("f21"),
        "pb": raw.get("f23"), "main_net": raw.get("f62"),
        "super_net": raw.get("f66"), "big_net": raw.get("f72"),
        "mid_net": raw.get("f78"), "small_net": raw.get("f84"),
        "main_pct": raw.get("f184"), "industry": raw.get("f100"),
        "quote_ts": raw.get("f124"), "quote_at": quote_at_fn(raw.get("f124")),
    }


def kline_frame(rows):
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.set_index("date")
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame
