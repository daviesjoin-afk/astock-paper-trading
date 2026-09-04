# -*- coding: utf-8 -*-
"""模拟盘风控中心的只读快照与解释层。

本模块不写订单、不修改账户参数。资金与拥挤度仍是解释性代理；新闻/公告
通过统一动态风控门禁参与新增风险控制，并完整保留来源、时间与降级状态。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics

from market_policy import MARKET_LIGHT_SCALES, market_light_scale, market_light_scales


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_PATH = os.path.join(BASE, "data_cache", "paper_risk_snapshot.json")
HISTORY_PATH = os.path.join(BASE, "data_cache", "paper_risk_factor_history.json")
RULE_VERSION = "paper-risk-v4-dynamic-v3-budgeted"
EFFECTIVE_AT = "2026-07-29"

LEVEL_ORDER = {"normal": 0, "watch": 1, "tightened": 2, "blocked": 3}
LEVEL_LABELS = {
    "normal": "正常",
    "watch": "关注",
    "tightened": "收紧",
    "blocked": "禁止开仓",
}
MARKET_SCALES = MARKET_LIGHT_SCALES
# The execution ledger is a single shared pool.  Strategy profiles still
# control per-name, industry and stop-risk sizing, but they must not turn the
# pool into three separate cash buckets.
SHARED_POOL_MAX_EXPOSURE_PCT = 82.0

DYNAMIC_RISK_VERSION = "dynamic-risk-v2"
NEWS_UNVERIFIED_TTL_SECONDS = 12 * 60 * 60
NEWS_VERIFIED_TTL_SECONDS = 3 * 24 * 60 * 60


def dynamic_risk_state(
    market=None,
    news_events=None,
    news_error=None,
    positions=None,
    news_observed_at=None,
    news_stale=False,
):
    """Build the single dynamic-risk state shared by risk center and learning UI.

    News/announcements are no longer a separate shadow-only panel: verified
    negative events block new entries for the affected symbol, unverified
    negative hits reduce size, and stale/failed news collection degrades new
    entry capacity.  Market and data-source state are evaluated on every
    snapshot refresh so the result changes with live conditions.
    """
    market = market or {}
    now = _now()
    events = list(news_events or [])
    active_events = []
    expired_negative_count = 0
    for row in events:
        if _number(row.get("tone"), 0) >= 0:
            active_events.append(row)
            continue
        observed = _parse_time(row.get("time"))
        # 时间戳解析失败的负面事件按刚发生处理（fail-closed）：宁可多观察，
        # 不可对未知时点的利空放行入场。事件会随上游抓取窗口滚动而自然消失。
        age = (now - observed).total_seconds() if observed else 0
        ttl = NEWS_VERIFIED_TTL_SECONDS if bool(row.get("verified")) else NEWS_UNVERIFIED_TTL_SECONDS
        if observed is not None and age > ttl:
            expired_negative_count += 1
            continue
        active_events.append(row)
    negative = [row for row in active_events if _number(row.get("tone"), 0) < 0]
    verified_negative = [row for row in negative if bool(row.get("verified"))]
    verified_codes = set()
    unverified_codes = set()
    code_rows = {}
    for row in negative:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        item = code_rows.setdefault(code, {"negative": 0, "verified_negative": 0, "events": []})
        item["negative"] += 1
        item["verified_negative"] += int(bool(row.get("verified")))
        (verified_codes if row.get("verified") else unverified_codes).add(code)
        item["events"].append({
            "title": row.get("summary") or row.get("title"),
            "source": row.get("source"),
            "time": row.get("time"),
            "verified": bool(row.get("verified")),
        })
    light = str(market.get("light") or "unknown")
    market_scale = 1.0 if light == "green" else (0.65 if light == "yellow" else 0.0)
    mode = "normal" if light == "green" else ("caution" if light == "yellow" else "halt")
    reasons = []
    if light == "yellow":
        reasons.append("市场黄灯，按策略缩放新开仓额度")
    elif light in {"red", "unknown"}:
        reasons.append("市场红灯或数据未知，暂停新开仓，仅保留退出风控")
    if verified_negative:
        reasons.append(f"{len(verified_codes)} 只标的命中已核验负面公告，按个股禁止新增")
    elif negative:
        reasons.append(f"{len(unverified_codes) or len(negative)} 只标的命中未核验负面，新增仓位降级")
    if news_error:
        reasons.append("新闻/公告源暂时不可用，新增仓位按降级额度执行")
    elif news_stale:
        reasons.append("新闻/公告扫描已过期，新增仓位按降级额度执行")
    if expired_negative_count:
        reasons.append(f"已忽略 {expired_negative_count} 条过期负面事件")
    if not reasons:
        reasons.append("市场、行情和新闻事件未触发动态收紧")
    # 个股公告只封锁命中个股，不再把整个账户统一砍半；未核验事件和
    # 新闻源降级才作用于全局，而且只做软缩放。
    if unverified_codes:
        mode = "caution" if mode != "halt" else mode
        market_scale *= 0.85
    if news_error or news_stale:
        mode = "caution" if mode != "halt" else mode
        market_scale *= 0.75
    held_codes = {str(row.get("code")) for row in (positions or []) if row.get("code")}
    affected_held = sorted(code for code in held_codes if code in code_rows)
    account_states = {}
    for row in positions or []:
        account_id = str(row.get("account_id") or "unknown")
        state = account_states.setdefault(account_id, {"blocked_codes": [], "caution_codes": []})
        code = str(row.get("code") or "")
        event_state = code_rows.get(code) or {}
        if event_state.get("verified_negative"):
            state["blocked_codes"].append(code)
        elif event_state.get("negative"):
            state["caution_codes"].append(code)
    for state in account_states.values():
        state["blocked_codes"] = sorted(set(state["blocked_codes"]))
        state["caution_codes"] = sorted(set(state["caution_codes"]))
    return {
        "version": DYNAMIC_RISK_VERSION,
        "mode": mode,
        "label": {"normal": "正常", "caution": "动态收紧", "halt": "暂停开仓"}.get(mode, "动态收紧"),
        "new_entry_allowed": mode != "halt",
        "risk_scale_pct": round(max(0.0, min(1.0, market_scale)) * 100, 1),
        "reason": "；".join(reasons),
        "updated_at": _iso(),
        "market_light": light,
        "news": {
            "event_count": len(active_events),
            "negative_count": len(negative),
            "verified_negative_count": len(verified_negative),
            "affected_held_count": len(affected_held),
            "scan_status": "failed" if news_error else ("stale" if news_stale else "ok"),
            "error": str(news_error) if news_error else None,
            "expired_negative_count": expired_negative_count,
            "verified_codes": sorted(verified_codes),
            "unverified_codes": sorted(unverified_codes),
            "codes": code_rows,
        },
        "accounts": account_states,
        "policy": "核验负面公告按个股禁止新开仓；未核验负面只降级仓位；数据源失败不伪装正常，自动降低新增风险。",
    }


def _now():
    return dt.datetime.now().astimezone()


def _iso(value=None):
    value = value or _now()
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        value = dt.datetime.combine(value, dt.time.min).astimezone()
    return value.isoformat(timespec="seconds")


def _number(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.isdigit() and len(text) >= 14:
            parsed = dt.datetime.strptime(text[:14], "%Y%m%d%H%M%S")
            return parsed.astimezone()
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.astimezone()
    except (TypeError, ValueError):
        return None


def source_health(name, source, observed_at=None, ttl_seconds=300, received_at=None, error=None, coverage_pct=None, verification=None):
    received = _parse_time(received_at) or _now()
    observed = _parse_time(observed_at)
    age = (received - observed).total_seconds() if observed else None
    if error:
        status = "failed"
    elif observed is None:
        status = "unknown"
    elif age is not None and age > ttl_seconds:
        status = "stale"
    else:
        status = "fresh"
    return {
        "name": name,
        "source": source,
        "status": status,
        "observed_at": _iso(observed) if observed else None,
        "received_at": _iso(received),
        "age_seconds": round(age, 1) if age is not None else None,
        "ttl_seconds": ttl_seconds,
        "coverage_pct": round(coverage_pct, 1) if coverage_pct is not None else None,
        "degraded": status != "fresh",
        "error": str(error) if error else None,
        "verification": verification or {"status": "not_independently_verified", "note": "single public source"},
    }


def snapshot_age_seconds(snapshot, now=None):
    observed = _parse_time((snapshot or {}).get("asof"))
    if not observed:
        return None
    return max(0.0, ((_now() if now is None else now) - observed).total_seconds())


def _load_history():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as handle:
            rows = json.load(handle)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _save_history(rows):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    temp_path = HISTORY_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(rows[-864:], handle, ensure_ascii=False, allow_nan=False)
    os.replace(temp_path, HISTORY_PATH)


def _flow_trend(rows):
    values = [_number(row.get("main_pct")) for row in rows]
    values = [value for value in values if value is not None]
    if len(values) < 3:
        return "样本不足"
    tail = values[-3:]
    if all(value > 0 for value in tail):
        return "连续流入"
    if all(value < 0 for value in tail):
        return "连续流出"
    if tail[-1] > 0 >= tail[-2]:
        return "由弱转强"
    if tail[-1] < 0 <= tail[-2]:
        return "由强转弱"
    return "震荡"


def _snapshot_market(universe, market, snapshot_at):
    current_width = str(snapshot_at or "")[:10] == _now().date().isoformat()
    rows = [
        row for row in (universe or [])
        if current_width and isinstance(row.get("pct"), (int, float))
    ]
    pcts = [float(row["pct"]) for row in rows]
    amounts = [
        float(row["amount"]) for row in rows
        if isinstance(row.get("amount"), (int, float))
    ]
    turnover_rows = [
        float(row["turnover"]) for row in rows
        if isinstance(row.get("turnover"), (int, float))
    ]
    positive_count = sum(value > 0 for value in pcts)
    negative_count = sum(value < 0 for value in pcts)
    directional_count = positive_count + negative_count
    breadth_up_pct = positive_count / directional_count * 100 if directional_count else None
    sample_count = len(rows)
    limit_up_proxy_count = sum(value >= 9.5 for value in pcts) if pcts else None
    return {
        **(market or {}),
        "breadth_up_pct": round(breadth_up_pct, 1) if breadth_up_pct is not None else None,
        "up": positive_count if pcts else None,
        "down": negative_count if pcts else None,
        "median_pct": round(statistics.median(pcts), 2) if pcts else None,
        "market_amount": round(sum(amounts), 2) if amounts else None,
        "limit_up_proxy_count": limit_up_proxy_count,
        "limit_down_proxy_count": sum(value <= -9.5 for value in pcts) if pcts else None,
        "limit_up_proxy_pct": round(limit_up_proxy_count / sample_count * 100, 2) if limit_up_proxy_count is not None and sample_count else None,
        "high_turnover_ratio_pct": (
            round(sum(value >= 10 for value in turnover_rows) / len(turnover_rows) * 100, 1)
            if turnover_rows else None
        ),
        "market_sample_count": sample_count,
        "snapshot_at": snapshot_at,
        "breadth_execution_mode": "reference_only",
    }


def refresh_snapshot(*, market, positions, universe, snapshot_at, news_events, news_error, sector_rows):
    """生成并原子保存当前风险快照；不产生任何交易。"""
    received = _now()
    market_view = _snapshot_market(universe, market, snapshot_at)
    quote_times = [
        _parse_time(position.get("quote_at"))
        for position in positions
        if position.get("quote_source") == "live" and position.get("quote_at")
    ]
    latest_quote = max((value for value in quote_times if value), default=None)
    quote_coverage = (
        sum(position.get("quote_source") == "live" for position in positions) / len(positions) * 100
        if positions else 100.0
    )
    flow_coverage = (
        sum(_number(position.get("main_pct")) is not None for position in positions) / len(positions) * 100
        if positions else 100.0
    )
    small_coverage = (
        sum(_number(position.get("small_net")) is not None for position in positions) / len(positions) * 100
        if positions else 0.0
    )
    news_times = [
        _parse_time(event.get("time")) for event in news_events or []
        if event.get("time")
    ]
    news_observed = max((value for value in news_times if value), default=received if not news_error else None)
    news_stale = bool(news_observed is None or (received - news_observed).total_seconds() > 15 * 60)
    index_observed = _parse_time((market or {}).get("live_index_time"))
    width_observed = _parse_time(snapshot_at)
    live_positions = [position for position in positions if position.get("quote_source") == "live"]
    quote_valid_count = sum(
        isinstance(position.get("price"), (int, float)) and position.get("price") > 0
        and isinstance(position.get("ret_pct"), (int, float)) and abs(position.get("ret_pct")) <= 30
        and _parse_time(position.get("quote_at")) is not None
        for position in live_positions
    )
    cross_checked_count = sum(position.get("quote_validation") == "cross_source_checked" for position in live_positions)
    quote_validation = {
        "status": "cross_source_checked" if live_positions and cross_checked_count == len(live_positions) else ("range_timestamp_checked" if live_positions and quote_valid_count == len(live_positions) else "incomplete"),
        "note": "Eastmoney and Tencent quotes reconciled by code, trade date, price and daily-change tolerance" if live_positions and cross_checked_count == len(live_positions) else "source timestamp, positive price and daily change range checked; independent reconciliation incomplete",
        "checked_rows": len(live_positions), "passed_rows": cross_checked_count if cross_checked_count else quote_valid_count,
    }
    index_valid = isinstance((market or {}).get("live_index_price"), (int, float)) and (market or {}).get("live_index_price") > 0 and isinstance((market or {}).get("live_index_pct"), (int, float)) and abs((market or {}).get("live_index_pct")) <= 20
    index_verification = {
        "status": "range_timestamp_checked" if index_valid else "incomplete",
        "note": "index code, timestamp and value range checked; single public source",
    }
    market_valid_rows = sum(
        _number(row.get("pct")) is not None
        for row in (universe or [])
    )
    market_coverage = (
        market_valid_rows / len(universe) * 100
        if universe else 0.0
    )
    data_quality = [
        source_health(
            "持仓实时行情", "东方财富公开行情", latest_quote, 20 * 60,
            received, coverage_pct=quote_coverage, verification=quote_validation,
        ),
        source_health(
            "沪深300实时行情", "腾讯实时指数", index_observed, 180,
            received, coverage_pct=100 if index_observed else 0, verification=index_verification,
        ),
        source_health(
            "市场宽度快照", "全市场本地快照（仅参考）", width_observed, 600,
            received, coverage_pct=market_coverage if str(snapshot_at or "")[:10] == received.date().isoformat() else 0,
            verification={"status": "reference_only", "note": "local market-wide snapshot; never used as an execution quote"},
        ),
        source_health(
            "主力资金代理", "东方财富按订单大小分类", latest_quote, 300,
            received, coverage_pct=flow_coverage,
            verification={"status": "single_source_range_checked", "note": "主力资金字段为单一公开源，仅校验时间戳与数值范围；价格双源核验不覆盖该字段"},
        ),
        source_health(
            "快讯关键词扫描", "东方财富7×24快讯（影子）", news_observed, 900,
            received, error=news_error, coverage_pct=None,
            verification={"status": "event_mapping_only", "note": "快讯只代表当前抓取窗口与名称映射结果；公司公告另以可追溯事件展示"},
        ),
        source_health(
            "小单/拥挤度代理", "公开行情小单净额（影子）",
            latest_quote if small_coverage else None, 300, received,
            coverage_pct=small_coverage,
            verification={"status": "single_source_range_checked", "note": "小单字段为单源代理，仅校验时间戳与数值范围"} if small_coverage else {"status": "incomplete", "note": "small-order fields missing"},
        ),
        source_health(
            "历史K线", "本地前复权日线", (market or {}).get("data_date"),
            3 * 86400, received, coverage_pct=100 if (market or {}).get("data_date") else 0,
        ),
        source_health(
            "海外风险", "公开海外指数历史", None,
            1800, received, coverage_pct=0,
            verification={"status": "not_timestamped", "note": "provider response has no source timestamp; kept as conservative reference only"},
        ),
    ]
    # 每次由 5 分钟任务或后台刷新写入一个同口径观察点。历史采用文件而非
    # 覆盖单点 JSON，连续少于 3 点时明确显示“样本不足”，不臆造资金趋势。
    history = _load_history()
    observation = {
        "asof": _iso(received),
        "positions": [
            {
                "account_id": row.get("account_id"),
                "code": str(row.get("code") or ""),
                "main_pct": _number(row.get("main_pct")),
                "small_net": _number(row.get("small_net")),
                "turnover": _number(row.get("turnover")),
                "market_value": _number(row.get("market_value"), 0),
                "quote_at": row.get("quote_at"),
                "quote_validation": row.get("quote_validation"),
            }
            for row in positions
            if row.get("code")
        ],
    }
    previous_at = _parse_time((history[-1] or {}).get("asof")) if history else None
    if previous_at and (received - previous_at).total_seconds() < 240:
        # 4 分钟内的多次页面刷新属于同一观察桶，只保留最新值。
        history[-1] = observation
    else:
        history.append(observation)
    _save_history(history)
    position_trends = []
    for row in positions:
        key_rows = [
            item
            for sample in history
            for item in (sample.get("positions") or [])
            if item.get("account_id") == row.get("account_id")
            and str(item.get("code")) == str(row.get("code"))
        ][-12:]
        position_trends.append({
            "account_id": row.get("account_id"),
            "code": str(row.get("code") or ""),
            "sample_count": len(key_rows),
            "trend": _flow_trend(key_rows),
            "latest_main_pct": _number(row.get("main_pct")),
        })
    flow_history = []
    for sample in history[-48:]:
        rows = [
            item for item in (sample.get("positions") or [])
            if _number(item.get("main_pct")) is not None and _number(item.get("market_value"), 0) > 0
        ]
        denominator = sum(_number(item.get("market_value"), 0) for item in rows)
        weighted = (
            sum(_number(item.get("main_pct")) * _number(item.get("market_value"), 0) for item in rows) / denominator
            if denominator else None
        )
        flow_history.append({"asof": sample.get("asof"), "main_pct": round(weighted, 3) if weighted is not None else None})
    verified_events = [event for event in (news_events or []) if event.get("verified")]
    warning_events = [event for event in (news_events or []) if _number(event.get("tone"), 0) < 0]
    snapshot = {
        "asof": _iso(received),
        "rule_version": RULE_VERSION,
        "effective_at": EFFECTIVE_AT,
        "mode": "V4执行安全门禁 + 统一动态风控",
        "market": market_view,
        "positions": positions,
        "sector_rows": list(sector_rows or [])[:20],
        "news": {
            "scan_status": "failed" if news_error else "ok",
            "last_scan_at": _iso(news_observed) if news_observed else None,
            "warning_count": len(warning_events),
            "event_count": len(news_events or []),
            "verified_event_count": len(verified_events),
            "events": list(news_events or [])[:30],
            "error": str(news_error) if news_error else None,
            "stale": news_stale,
            "execution_mode": "dynamic",
            "notice": "新闻与公司公告进入统一动态风控：核验负面公告禁止该股新增仓位，未核验负面只缩减新增额度；不据此自动卖出。",
        },
        "dynamic_risk": dynamic_risk_state(
            market=market_view,
            news_events=news_events,
            news_error=news_error,
            positions=positions,
            news_observed_at=news_observed,
            news_stale=news_stale,
        ),
        "data_quality": data_quality,
        "fund_flow": {
            "execution_mode": "shadow",
            "sample_count": max((item["sample_count"] for item in position_trends), default=0),
            "coverage_pct": round(flow_coverage, 1),
            "trend_status": _flow_trend([
                {"main_pct": item.get("main_pct")} for item in flow_history if item.get("main_pct") is not None
            ]),
            "position_trends": position_trends,
            "history": flow_history,
            "notice": "连续不少于 3 个同口径样本后才显示流入、流出或转折；主力/小单字段为单源代理，不参与自动成交。",
        },
        "crowding": {
            "execution_mode": "shadow",
            "market_width_pct": market_view.get("breadth_up_pct"),
            "median_pct": market_view.get("median_pct"),
            "high_turnover_ratio_pct": market_view.get("high_turnover_ratio_pct"),
            "small_net_coverage_pct": round(small_coverage, 1),
            "limit_up_proxy_pct": market_view.get("limit_up_proxy_pct"),
            "limit_up_proxy_count": market_view.get("limit_up_proxy_count"),
            "market_sample_count": market_view.get("market_sample_count"),
            "snapshot_at": market_view.get("snapshot_at"),
            "status": "available" if small_coverage else "unknown",
            "notice": "小单净额、人气与换手仅是拥挤度代理，不代表真实散户账户交易。",
        },
    }
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    temp_path = SNAPSHOT_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, allow_nan=False)
    os.replace(temp_path, SNAPSHOT_PATH)
    return snapshot


def load_snapshot():
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _worst_level(levels):
    return max(levels, key=lambda level: LEVEL_ORDER.get(level, 1), default="watch")


def _account_card(account, positions, market, shared=None):
    limits = account.get("risk_limits") or {}
    account_positions = [row for row in positions if row.get("account_id") == account.get("id")]
    max_position = max((_number(row.get("account_weight_pct"), 0) for row in account_positions), default=0)
    industries = {}
    nav = max(_number(account.get("nav"), 0), 1)
    for row in account_positions:
        industry = row.get("industry") or "未知"
        industries[industry] = industries.get(industry, 0.0) + _number(row.get("market_value"), 0)
    largest_industry_name = max(industries, key=industries.get) if industries else None
    largest_industry_pct = (
        industries.get(largest_industry_name, 0) / nav * 100 if largest_industry_name else 0
    )
    market_light = str((market or {}).get("light") or "unknown")
    market_scale = market_light_scale(market_light, account["id"])
    drivers = []
    level = "normal"
    if account.get("status") != "running":
        level, market_scale = "blocked", 0.0
        drivers.append("账户当前已暂停")
    elif market_scale <= 0:
        level = "blocked"
        drivers.append((market or {}).get("reason") or "关键市场数据未知或市场红灯")
    elif market_scale < 1:
        level = "tightened"
        drivers.append(f"市场{market_light}，新开仓额度缩放至 {market_scale * 100:.0f}%")
    shared = shared or {}
    # The 82% value is a pool hard stop.  Each strategy card now consumes the
    # execution engine's fair budget snapshot instead of displaying a second
    # copy of that global ceiling.
    budget = account.get("strategy_budget") or {}
    exposure = _number(budget.get("current_pct"), 0.0)
    strategy_budget_pct = _number(budget.get("target_pct"), 0.0)
    strategy_floor_pct = _number(budget.get("floor_pct"), 0.0)
    strategy_cap_pct = (
        _number(budget.get("absolute_cap_amount"), 0.0)
        / max(_number(shared.get("nav"), _number(account.get("nav"), 1)), 1) * 100
        if budget else strategy_budget_pct
    )
    pool_exposure = _number(budget.get("pool_exposure_pct"), _number(shared.get("fund_utilization_pct"), 0.0))
    pool_limit = _number(budget.get("pool_limit_pct"), SHARED_POOL_MAX_EXPOSURE_PCT)
    exposure_limit = strategy_cap_pct or strategy_budget_pct
    if pool_limit and pool_exposure >= pool_limit:
        level = _worst_level([level, "tightened"])
        drivers.append(f"总资金池已达到 {pool_limit:.0f}% 硬上限")
    elif exposure_limit and exposure >= exposure_limit - 0.01:
        level = _worst_level([level, "tightened"])
        drivers.append(f"本策略已达到动态预算上限 {exposure_limit:.1f}%")
    elif strategy_budget_pct and exposure >= strategy_budget_pct:
        drivers.append(f"基础预算 {strategy_budget_pct:.1f}% 已用满，等待其他策略保底后再转入")
    raw_drawdown = _number(shared.get("max_drawdown_pct"), _number(account.get("max_drawdown_pct"), 0))
    drawdown = abs(raw_drawdown)
    drawdown_limit = _number(limits.get("drawdown_pct"), 0)
    if drawdown_limit and drawdown >= drawdown_limit:
        level, market_scale = "blocked", 0.0
        drivers.append("滚动回撤达到冷静期阈值")
    elif drawdown_limit and drawdown >= drawdown_limit * 0.7:
        level = _worst_level([level, "watch"])
        drivers.append("滚动回撤接近策略阈值")
    weighted_flow = 0.0
    flow_weight = 0.0
    for row in account_positions:
        value = _number(row.get("market_value"), 0)
        main_pct = _number(row.get("main_pct"))
        if main_pct is not None and value > 0:
            weighted_flow += main_pct * value
            flow_weight += value
    current_main_pct = weighted_flow / flow_weight if flow_weight else None
    if current_main_pct is not None and current_main_pct < 0:
        drivers.append(f"影子：持仓加权主力占比 {current_main_pct:+.2f}%（单点，不参与成交）")
        if level == "normal":
            level = "watch"
    if not drivers:
        drivers.append("当前未触发账户级熔断；新增资金与舆情信号仍在影子观察")
    scale_pct = round(market_scale * 100, 1)
    permission = "禁止新开仓，仅保留风险卖出" if level == "blocked" else (
        f"按 {scale_pct:.0f}% 风险额度开仓" if scale_pct < 100 else "按当前策略风险预算执行"
    )
    return {
        "id": account["id"],
        "name": account["name"],
        "level": level,
        "status_label": LEVEL_LABELS[level],
        "summary": drivers[0],
        "trade_permission": permission,
        "risk_scale_pct": scale_pct,
        "cooldown_until": account.get("cooldown_until"),
        "drivers": drivers,
        "actions": ["持仓继续执行原价格止损、移动止盈、T+1和涨跌停约束"],
        "data_status": "执行门禁 + 影子解释",
        "metrics": {
            "position_exposure_pct": exposure,
            "max_exposure_pct": round(exposure_limit, 2),
            "exposure_buffer_pct": round(max(exposure_limit - exposure, 0), 2),
            "strategy_budget_pct": round(strategy_budget_pct, 2),
            "strategy_floor_pct": round(strategy_floor_pct, 2),
            "market_scale_pct": _number(budget.get("market_scale_pct"), scale_pct),
            "pool_exposure_pct": round(pool_exposure, 2),
            "pool_limit_pct": round(pool_limit, 2),
            "pool_buffer_pct": round(max(pool_limit - pool_exposure, 0), 2),
            "redistribution_available_pct": round(_number(budget.get("redistribution_amount"), 0.0) / max(_number(shared.get("nav"), 1), 1) * 100, 2),
            "largest_position_pct": round(max_position, 2),
            "max_position_pct": _number(limits.get("max_weight_pct"), 0),
            "position_buffer_pct": round(max(_number(limits.get("max_weight_pct"), 0) - max_position, 0), 2),
            "largest_industry_name": largest_industry_name,
            "largest_industry_pct": round(largest_industry_pct, 2),
            "max_industry_pct": _number(limits.get("max_industry_pct"), 0),
            "industry_buffer_pct": round(max(_number(limits.get("max_industry_pct"), 0) - largest_industry_pct, 0), 2),
            "daily_loss_pct": _number(shared.get("daily_loss_pct"), _number(account.get("daily_loss_pct"))),
            "daily_loss_limit_pct": _number(limits.get("daily_loss_pct"), 0),
            "rolling_drawdown_pct": round(drawdown, 2),
            "drawdown_limit_pct": drawdown_limit,
            "current_main_pct": round(current_main_pct, 2) if current_main_pct is not None else None,
            "flow_trend": "样本不足",
        },
    }


def _position_queue(accounts, positions, news_events):
    names = {account["id"]: account["name"] for account in accounts}
    # P3 审计修复（R5）：与 dynamic_risk_state 同一口径的 TTL 过滤——
    # 旧实现直接用全量事件，过期负面快讯在持仓队列里永久压制。
    now = _now()
    active_events = []
    for event in news_events or []:
        if _number(event.get("tone"), 0) >= 0:
            active_events.append(event)
            continue
        observed = _parse_time(event.get("time"))
        age = (now - observed).total_seconds() if observed else 0
        ttl = NEWS_VERIFIED_TTL_SECONDS if bool(event.get("verified")) else NEWS_UNVERIFIED_TTL_SECONDS
        if observed is not None and age > ttl:
            continue
        active_events.append(event)
    news_codes = {str(event.get("code")) for event in active_events}
    verified_news_codes = {
        str(event.get("code")) for event in active_events
        if _number(event.get("tone"), 0) < 0 and bool(event.get("verified"))
    }
    queue = []
    for row in positions:
        fresh = row.get("quote_source") == "live" and bool(row.get("quote_at"))
        risk_price = _number(row.get("risk_price"))
        price = _number(row.get("price"))
        available = int(_number(row.get("available_qty"), 0))
        reasons = []
        if not fresh:
            level, action, executable = "blocked", "行情过期，暂不执行", False
            reasons.append("持仓报价不是带当日源时间戳的实时行情")
        elif price is not None and risk_price is not None and price <= risk_price:
            if available >= 100:
                level, action, executable = "tightened", "触发清仓", True
                reasons.append("现价已触及策略价格风控线")
            else:
                level, action, executable = "blocked", "等待T+1后退出", False
                reasons.append("已触及价格风控线，但当前份额受T+1锁定")
        elif _number(row.get("ret_pct"), 0) <= -1 or _number(row.get("main_pct"), 0) <= -5:
            level, action, executable = "watch", "暂停加仓", False
            reasons.append("浮亏或单点主力资金偏弱，进入加强观察")
        else:
            level, action, executable = "normal", "继续持有", True
            reasons.append("尚未触发价格退出条件")
        if str(row.get("code")) in verified_news_codes:
            level = _worst_level([level, "tightened"])
            action, executable = "禁止加仓，人工复核", False
            reasons.append("已核验负面公告/事件命中动态风控，暂停加仓并进入人工复核")
        elif str(row.get("code")) in news_codes:
            level = _worst_level([level, "watch"])
            reasons.append("负面快讯尚未完成公告核验，暂停加仓并降低新增风险")
        queue.append({
            "level": level,
            "account_id": row.get("account_id"),
            "account_name": names.get(row.get("account_id"), row.get("account_id")),
            "code": row.get("code"),
            "name": row.get("name"),
            "market_value": row.get("market_value"),
            "account_weight_pct": row.get("account_weight_pct"),
            "unrealized_pnl": row.get("unrealized_pnl"),
            "ret_pct": row.get("ret_pct"),
            "price": price,
            "risk_price": risk_price,
            "main_pct": row.get("main_pct"),
            "news_status": "公告否决" if str(row.get("code")) in verified_news_codes else ("舆情收紧" if str(row.get("code")) in news_codes else "未命中"),
            "t1_status": row.get("t1_status"),
            "available_qty": available,
            "reason": "；".join(reasons),
            "action": action,
            "executable": executable,
            "quote_at": row.get("quote_at"),
            "quote_source": row.get("quote_source"),
        })
    return sorted(queue, key=lambda item: LEVEL_ORDER.get(item["level"], 0), reverse=True)


def _attach_dynamic_pool_budgets(accounts, positions, shared, market):
    """Mirror paper_trading's dynamic strategy budget for the read-only view."""
    nav = max(_number(shared.get("nav"), 0.0), 1.0)
    pool_value = _number(shared.get("market_value"), 0.0)
    if pool_value <= 0:
        pool_value = sum(_number(row.get("market_value"), 0.0) for row in positions)
    ids = [str(account.get("id")) for account in accounts if account.get("id")]
    weights = {
        account_id: max(_number((next((a for a in accounts if a.get("id") == account_id), {})
                                  .get("risk_limits") or {}).get("max_exposure_pct"), 0.0), 1.0)
        for account_id in ids
    }
    total_weight = sum(weights.values()) or 1.0
    base_targets = {key: SHARED_POOL_MAX_EXPOSURE_PCT * value / total_weight for key, value in weights.items()}
    light = str((market or {}).get("light") or "unknown")
    scales = market_light_scales(light)
    targets = {key: base_targets[key] * market_light_scale(light, key) for key in ids}
    floors = {key: targets[key] * 0.60 for key in ids}
    current = {key: 0.0 for key in ids}
    for row in positions:
        key = str(row.get("account_id") or "")
        if key in current:
            current[key] += _number(row.get("market_value"), 0.0)
    global_remaining = max(0.0, nav * SHARED_POOL_MAX_EXPOSURE_PCT / 100.0 - pool_value)
    for account in accounts:
        key = str(account.get("id"))
        target_pct = targets.get(key, 0.0)
        floor_pct = floors.get(key, 0.0)
        current_amount = current.get(key, 0.0)
        own_headroom = max(0.0, nav * target_pct / 100.0 - current_amount)
        other_floor_reserve = sum(
            max(0.0, nav * floors.get(other, 0.0) / 100.0 - current.get(other, 0.0))
            for other in ids if other != key
        )
        after_floor = max(0.0, global_remaining - other_floor_reserve)
        others_met = all(
            current.get(other, 0.0) + 1e-6 >= nav * floors.get(other, 0.0) / 100.0
            for other in ids if other != key
        )
        redistribution = max(0.0, after_floor - own_headroom) if others_met else 0.0
        allowance = min(global_remaining, after_floor, own_headroom + redistribution)
        account["strategy_budget"] = {
            "target_pct": round(target_pct, 2),
            "base_target_pct": round(base_targets.get(key, 0.0), 2),
            "floor_pct": round(floor_pct, 2),
            "current_pct": round(current_amount / nav * 100, 2),
            "absolute_cap_amount": round(current_amount + max(0.0, allowance), 2),
            "allowance_amount": round(max(0.0, allowance), 2),
            "redistribution_amount": round(max(0.0, redistribution), 2),
            "market_scale_pct": round(market_light_scale(light, key) * 100, 1),
            "pool_exposure_pct": round(pool_value / nav * 100, 2),
            "pool_limit_pct": SHARED_POOL_MAX_EXPOSURE_PCT,
        }
    return accounts


def build_dashboard(base_dashboard, snapshot):
    accounts = list(base_dashboard.get("accounts") or [])
    positions = list(base_dashboard.get("positions") or [])
    shared = dict(base_dashboard.get("shared") or {})
    market = dict(snapshot.get("market") or {})
    _attach_dynamic_pool_budgets(accounts, positions, shared, market)
    cards = [
        _account_card(account, positions, market, shared=shared)
        for account in accounts
    ]
    position_queue = _position_queue(accounts, positions, (snapshot.get("news") or {}).get("events"))
    dynamic = snapshot.get("dynamic_risk") or dynamic_risk_state(
        market=market, news_events=(snapshot.get("news") or {}).get("events"),
        news_error=(snapshot.get("news") or {}).get("error"), positions=positions,
        news_observed_at=(snapshot.get("news") or {}).get("last_scan_at"),
        news_stale=bool((snapshot.get("news") or {}).get("stale")),
    )
    worst = _worst_level([card["level"] for card in cards] + [
        # P3 审计修复（R4）：空仓时"持仓实时行情"源天然 unknown，不应
        # 判成关键数据源失败把整个风控面板标 blocked。
        "blocked" if any(
            item.get("status") in {"failed", "unknown"}
            and item.get("name") in {"沪深300实时行情", "持仓实时行情"}
            and not (item.get("name") == "持仓实时行情" and not positions)
            for item in snapshot.get("data_quality") or []
        ) else ("blocked" if dynamic.get("mode") == "halt" else ("watch" if dynamic.get("mode") == "caution" else "normal"))
    ])
    min_scale = min((card["risk_scale_pct"] for card in cards), default=0)
    overall_summary = {
        "normal": "两套当前账户未触发新开仓熔断，新增因子仍处于影子观察。",
        "watch": "存在需要跟踪的单点资金、快讯或持仓弱势信号，不自动减仓。",
        "tightened": "至少一个策略已降低新开仓额度，原持仓继续按各自退出规则监控。",
        "blocked": "至少一个策略或关键数据源禁止新增风险，风险卖出仍继续执行。",
    }[worst]
    alerts = []
    for card in cards:
        if card["level"] != "normal":
            alerts.append({
                "time": snapshot.get("asof"),
                "account_id": card["id"],
                "account_name": card["name"],
                "code": None,
                "level": card["level"],
                "reason_code": "ACCOUNT_RISK_STATE",
                "reason": card["summary"],
                "action": card["trade_permission"],
                "execution_mode": "active",
                "rule_version": RULE_VERSION,
            })
    for decision in (base_dashboard.get("risk_decisions") or [])[:60]:
        alerts.append({
            "time": decision.get("created_at"),
            "account_id": decision.get("account_id"),
            "account_name": decision.get("account_name"),
            "code": decision.get("code"),
            "level": "tightened" if decision.get("decision") in {"filled", "exit_pending_data"} else "watch",
            "reason_code": str(decision.get("decision") or "RISK_DECISION").upper(),
            "reason": decision.get("reason"),
            "action": decision.get("decision"),
            "execution_mode": "active",
            "rule_version": RULE_VERSION,
        })
    return {
        "asof": snapshot.get("asof"),
        "last_success_at": snapshot.get("asof"),
        "next_check_at": "交易时段每5分钟；页面只读每60秒刷新",
        "rule_version": RULE_VERSION,
        "effective_at": EFFECTIVE_AT,
        "mode": snapshot.get("mode"),
        "overall": {
            "level": worst,
            "label": LEVEL_LABELS[worst],
            "summary": overall_summary,
            "trade_permission": (
                "仅在共享资金池整体红灯/数据失真时禁止新增；策略级限制只影响对应策略，风险卖出继续执行"
                if worst == "blocked" else "两套当前策略共用总资金池；按各自单票、行业和止损模型执行"
            ),
            "risk_scale_pct": min_scale,
            "sell_monitoring": "继续执行；T+1、跌停或行情过期时明确记录风险未解除",
        },
        "accounts": cards,
        "market": market,
        "fund_flow": snapshot.get("fund_flow") or {},
        "sentiment": snapshot.get("news") or {},
        "dynamic_risk": dynamic,
        "crowding": snapshot.get("crowding") or {},
        "sector_rows": snapshot.get("sector_rows") or [],
        "position_queue": position_queue,
        "data_quality": snapshot.get("data_quality") or [],
        "alerts": alerts[:100],
        "disclaimer": "风控中心只服务本地模拟盘。新闻与公告由统一动态风控参与新增风险门禁；主力资金、小单和人气仍是公开数据代理，仅作解释性观察。",
    }
