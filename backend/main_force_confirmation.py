"""Bounded, shared main-force confirmation; never an independent buy signal.

Historical winners suggest investigating positive flow at an unextended price,
not rewarding the largest flow number. The sample is too small to establish
alpha. This confirmation cannot rescue a failed entry or override risk gates.
"""
from __future__ import annotations

import datetime as dt
from paper_numeric import coerced_number as _coerced_number

VERSION = "main-force-confirmation-v1"
MARKET_TZ = dt.timezone(dt.timedelta(hours=8))
MAX_QUOTE_AGE_SECONDS = 180
BONUS_BY_STRATEGY = {"tq_breakout": 0.03, "trend_pullback": 0.02}


# 数值读取合同的唯一 owner：宽松解析、严格有效（布尔/NaN/无穷一律未知）。
_number = _coerced_number


def _time(value):
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return result.replace(tzinfo=MARKET_TZ) if result.tzinfo is None else result


def assess(quote, *, asof=None):
    """Use only this quote's observed fields; missing/stale data earns no bonus.

    ``asof`` is injectable for replay. The deliberately broad bounds are a
    research starting policy, not fitted winning-stock thresholds. No stock
    names, retrospective returns, network reads or position actions are used.
    """
    quote = quote if isinstance(quote, dict) else {}
    now = _time(asof) if asof is not None else dt.datetime.now(MARKET_TZ)
    observed = _time(quote.get("quote_at"))
    fields = ("price", "pct", "main_pct", "main_net", "amount", "super_net", "vol_ratio")
    inputs = {key: _number(quote.get(key)) for key in fields}
    if inputs["main_pct"] is None:
        inputs["main_pct"] = _number(quote.get("main_net_pct"))
    result = {
        "version": VERSION, "status": "unavailable", "confirmed": False,
        "inputs": inputs, "quote_at": quote.get("quote_at"),
        "asof": now.isoformat() if now else None,
        "evidence_status": "exploratory_not_validated_alpha",
        "policy": "confirmation_only_no_gate_or_position_override",
        "reasons": [],
    }
    if now is None or observed is None:
        result["reasons"] = ["主力辅助确认缺少有效报价时间"]
        return result
    age = (now - observed).total_seconds()
    result["quote_age_seconds"] = round(age, 3)
    if not 0 <= age <= MAX_QUOTE_AGE_SECONDS:
        result["reasons"] = ["主力辅助确认报价过期或来自未来"]
        return result
    missing = [key for key, value in inputs.items() if value is None]
    if missing or inputs["price"] <= 0 or inputs["amount"] <= 0:
        result["reasons"] = ["主力辅助确认字段缺失或无效"]
        result["missing_fields"] = missing
        return result
    flow_pct = inputs["main_net"] / inputs["amount"] * 100
    result["computed_main_pct"] = round(flow_pct, 4)
    # These are two representations of ONE flow observation, not two votes.
    if (abs(inputs["main_pct"]) > 100 or abs(flow_pct) > 100
            or abs(flow_pct - inputs["main_pct"]) > 2.0):
        result["status"] = "conflicting"
        result["reasons"] = ["主力占比与净额/成交额口径冲突，不加分"]
        return result
    checks = {
        "positive_flow": inputs["main_pct"] >= 3.0 and flow_pct >= 2.0,
        "positive_super_flow": inputs["super_net"] > 0,
        "unextended_price": 0.3 <= inputs["pct"] <= 3.5,
        "bounded_volume": 1.0 <= inputs["vol_ratio"] <= 5.0,
    }
    result["checks"] = checks
    result["confirmed"] = all(checks.values())
    result["status"] = "confirmed" if result["confirmed"] else "not_confirmed"
    result["reasons"] = (["资金为正、价格未明显追高且量能未过度放大；仅作辅助确认"]
                         if result["confirmed"] else ["主力辅助条件未同时成立，不加分"])
    return result


def entry_effect(strategy_id, evidence, *, base_score, threshold, blockers):
    """A small preference only AFTER the unchanged base entry has passed.

    Quality-defense receives evidence but no trading bonus. Missing evidence
    leaves existing entry/exit behavior intact. Even a caller-supplied positive
    evidence object cannot rescue an invalid baseline score or a hard veto.
    """
    score = _number(base_score)
    minimum = _number(threshold)
    base_passed = (score is not None and minimum is not None
                   and 0 <= score <= 1 and 0 <= minimum <= 1
                   and score >= minimum and not blockers)
    bonus = (BONUS_BY_STRATEGY.get(str(strategy_id), 0.0)
             if base_passed and evidence.get("confirmed") is True else 0.0)
    return {
        "base_score": base_score, "base_passed": base_passed,
        "bonus": bonus, "score": min(1.0, score + bonus) if score is not None else base_score,
        "passed": bool(base_passed),
    }
