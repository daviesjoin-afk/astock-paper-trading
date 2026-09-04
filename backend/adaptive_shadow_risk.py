# -*- coding: utf-8 -*-
"""Pure portfolio-risk calculations used by the adaptive read model.

This module deliberately has no database, filesystem, network, or order
execution dependency.  Callers inject the numeric coercion and clock helpers
used by the application so the calculations remain easy to replay in tests.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict


def _default_num(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def shadow_history_rows(history, *, num=None):
    """Normalize cached history into close/high/low rows without I/O."""
    num = num or _default_num
    if history is None:
        return []
    if hasattr(history, "to_dict"):
        try:
            history = history.to_dict("records")
        except Exception:
            history = []
    if isinstance(history, dict):
        history = history.get("rows") or history.get("data") or []
    if not isinstance(history, (list, tuple)):
        return []
    rows = []
    for item in history:
        if isinstance(item, dict):
            def pick(*keys):
                for key in keys:
                    if key in item:
                        value = num(item.get(key), None)
                        if value is not None:
                            return value
                return None
            close = pick("close", "close_price", "price", "收盘")
            high = pick("high", "high_price", "最高")
            low = pick("low", "low_price", "最低")
            previous = pick("prev_close", "previous_close", "昨收")
            if close is not None and close > 0:
                rows.append({"close": close, "high": high, "low": low, "prev_close": previous})
        elif isinstance(item, (list, tuple)) and len(item) == 1:
            value = num(item[0], None)
            if value is not None and value > 0:
                rows.append({"close": value, "high": None, "low": None, "prev_close": None})
        else:
            value = num(item, None)
            if value is not None and value > 0:
                rows.append({"close": value, "high": None, "low": None, "prev_close": None})
    return rows


def shadow_volatility(history, window, *, num=None):
    """Return annualised close-to-close volatility or explicit unknown."""
    rows = shadow_history_rows(history, num=num)
    if len(rows) < int(window) + 1:
        return {"status": "unknown", "value_pct": None, "samples": max(0, len(rows) - 1),
                "required": int(window) + 1, "reason": "历史K线不足"}
    closes = [row["close"] for row in rows[-(int(window) + 1):]]
    returns = [(closes[idx] / closes[idx - 1]) - 1.0 for idx in range(1, len(closes))
               if closes[idx - 1] > 0 and closes[idx] > 0]
    if len(returns) < int(window):
        return {"status": "unknown", "value_pct": None, "samples": len(returns),
                "required": int(window), "reason": "有效收盘价不足"}
    value = statistics.stdev(returns) * math.sqrt(252.0) * 100.0 if len(returns) > 1 else 0.0
    return {"status": "known", "value_pct": round(value, 4), "samples": len(returns),
            "window": int(window)}


def shadow_atr_pct(history, window=20, *, num=None):
    """Return ATR as a percent of the latest close, or unknown."""
    rows = shadow_history_rows(history, num=num)
    if len(rows) < int(window) + 1:
        return {"status": "unknown", "value_pct": None, "samples": 0,
                "required": int(window) + 1, "reason": "历史K线不足"}
    selected = rows[-(int(window) + 1):]
    true_ranges = []
    for idx in range(1, len(selected)):
        row = selected[idx]
        prev_close = selected[idx - 1]["close"]
        high, low = row.get("high"), row.get("low")
        if high is None or low is None or prev_close <= 0 or high < low:
            continue
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    latest = selected[-1]["close"]
    if len(true_ranges) < int(window) or latest <= 0:
        return {"status": "unknown", "value_pct": None, "samples": len(true_ranges),
                "required": int(window), "reason": "高低价或昨收缺失"}
    return {"status": "known", "value_pct": round(statistics.mean(true_ranges) / latest * 100.0, 4),
            "samples": len(true_ranges), "window": int(window)}


def shadow_hhi(values, *, num=None):
    """Compute a normalized Herfindahl index from positive exposures."""
    num = num or _default_num
    clean = [float(value) for value in values if num(value, None) is not None and float(value) > 0]
    total = sum(clean)
    return round(sum((value / total) ** 2 for value in clean), 6) if total > 0 else None


def portfolio_shadow_risk(positions, histories=None, asof=None, industry_shock_pct=-8.0,
                          limit_down_pct=-10.0, *, num=None, now_fn=None):
    """Build read-only portfolio risk metrics for the adaptive overview."""
    num = num or _default_num
    now_fn = now_fn or (lambda: "")
    histories = histories if isinstance(histories, dict) else {}
    positions = positions if isinstance(positions, (list, tuple)) else []
    normalized = []
    unknown_marks = []
    for raw in positions:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        if not code:
            continue
        qty = num(raw.get("qty"), 0.0) or 0.0
        if qty <= 0:
            continue
        mark = num(raw.get("market_price"), None)
        mark = mark if mark is not None else num(raw.get("current_price"), None)
        mark = mark if mark is not None else num(raw.get("price"), None)
        cost = num(raw.get("cost"), None)
        if mark is not None and mark > 0:
            value, source = qty * mark, "quote"
        elif cost is not None and cost > 0:
            value, source = qty * cost, "cost_fallback"
        else:
            value, source = None, "unknown"
            unknown_marks.append(code)
        normalized.append({
            "account_id": str(raw.get("account_id") or "unknown"),
            "account_name": raw.get("account_name"),
            "code": code,
            "name": str(raw.get("name") or ""),
            "industry": str(raw.get("industry") or "未知"),
            "qty": int(qty) if float(qty).is_integer() else round(qty, 4),
            "mark_price": mark,
            "cost": cost,
            "market_value": round(value, 2) if value is not None else None,
            "valuation_source": source,
        })
    valued = [row for row in normalized if row["market_value"] is not None]
    total_value = sum(row["market_value"] for row in valued)
    by_strategy = defaultdict(float)
    by_code = defaultdict(float)
    by_industry = defaultdict(float)
    code_accounts = defaultdict(set)
    for row in valued:
        by_strategy[row["account_id"]] += row["market_value"]
        by_code[row["code"]] += row["market_value"]
        by_industry[row["industry"]] += row["market_value"]
    for row in normalized:
        code_accounts[row["code"]].add(row["account_id"])

    def shares(values):
        return {key: round(value / total_value * 100.0, 3)
                for key, value in sorted(values.items(), key=lambda item: item[1], reverse=True)} if total_value > 0 else {}

    strategy_shares = shares(by_strategy)
    industry_shares = shares(by_industry)
    cross_strategy = []
    for code, accounts in sorted(code_accounts.items()):
        if len(accounts) > 1:
            cross_strategy.append({
                "code": code,
                "strategies": sorted(accounts),
                "strategy_count": len(accounts),
                "market_value": round(by_code.get(code, 0.0), 2),
                "share_pct": round(by_code.get(code, 0.0) / total_value * 100.0, 3) if total_value > 0 else None,
            })
    volatility = {}
    for code in sorted({row["code"] for row in normalized}):
        history = histories.get(code)
        volatility[code] = {
            "20d": shadow_volatility(history, 20, num=num),
            "60d": shadow_volatility(history, 60, num=num),
            "atr20_pct": shadow_atr_pct(history, 20, num=num),
        }
    if total_value > 0:
        index_stress = {
            "index_minus_2_pct": round(total_value * -0.02, 2),
            "index_minus_5_pct": round(total_value * -0.05, 2),
        }
        industry_stress = [{
            "industry": industry, "shock_pct": float(industry_shock_pct),
            "loss": round(value * industry_shock_pct / 100.0, 2),
            "exposure": round(value, 2), "share_pct": industry_shares.get(industry),
        } for industry, value in sorted(by_industry.items(), key=lambda item: item[1], reverse=True)]
        worst_code, worst_value = max(by_code.items(), key=lambda item: item[1])
        single_stress = {"code": worst_code, "shock_pct": float(limit_down_pct),
                         "loss": round(worst_value * limit_down_pct / 100.0, 2),
                         "exposure": round(worst_value, 2)}
    else:
        index_stress, industry_stress, single_stress = {
            "status": "unknown", "reason": "没有可估值持仓"
        }, [], {"status": "unknown", "reason": "没有可估值持仓"}
    return {
        "mode": "shadow",
        "version": "portfolio-risk-shadow-v2",
        "asof": asof or now_fn(),
        "data_quality": {
            "status": "known" if valued and not unknown_marks else ("partial" if valued else "unknown"),
            "valued_positions": len(valued), "total_positions": len(normalized),
            "unknown_mark_codes": sorted(set(unknown_marks)),
            "history_codes": sorted(str(key) for key in histories if str(key) in {row["code"] for row in normalized}),
            "note": "cost_fallback 仅用于影子估值；没有行情或历史不补造数据。",
        },
        "positions": {"total": len(normalized), "valued": len(valued), "unknown": len(normalized) - len(valued),
                      "total_value": round(total_value, 2) if total_value > 0 else None},
        "exposure": {
            "by_strategy": [{"account_id": key, "market_value": round(value, 2), "share_pct": strategy_shares.get(key)} for key, value in sorted(by_strategy.items(), key=lambda item: item[1], reverse=True)],
            "by_industry": [{"industry": key, "market_value": round(value, 2), "share_pct": industry_shares.get(key)} for key, value in sorted(by_industry.items(), key=lambda item: item[1], reverse=True)],
            "cross_strategy_same_code": cross_strategy,
        },
        "concentration": {
            "strategy_hhi": shadow_hhi(by_strategy.values(), num=num),
            "industry_hhi": shadow_hhi(by_industry.values(), num=num),
            "code_hhi": shadow_hhi(by_code.values(), num=num),
            "top_strategy_share_pct": max(strategy_shares.values()) if strategy_shares else None,
            "top_industry_share_pct": max(industry_shares.values()) if industry_shares else None,
            "top_code_share_pct": round(max(by_code.values()) / total_value * 100.0, 3) if total_value > 0 else None,
        },
        "volatility": volatility,
        "stress": {
            "index": index_stress,
            "industry": {"assumption_pct": float(industry_shock_pct), "scenarios": industry_stress},
            "single_limit_down": single_stress,
        },
        "flags": [
            *(("行情估值不完整",) if unknown_marks else ()),
            *(("行业集中度较高",) if max(industry_shares.values(), default=0.0) >= 35.0 else ()),
            *(("同股跨策略重复暴露",) if cross_strategy else ()),
        ],
    }
