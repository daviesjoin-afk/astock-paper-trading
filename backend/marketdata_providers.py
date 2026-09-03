# -*- coding: utf-8 -*-
"""公开行情 provider 的纯文本解析器。

网络请求、重试和源切换仍由 `data_fetcher` 编排；本模块只把腾讯/新浪
响应转换成统一的原始报价行，便于 provider 单元测试和后续适配器替换。
"""
from __future__ import annotations

import re


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
