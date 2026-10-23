# -*- coding: utf-8 -*-
"""候选截面装配：实时覆盖、证券权限、板块热度与因子表构建。

承接 ``_candidate_rows`` 中"取数之后 → 截面装配 → 证据附加"这一段：时效行
过滤、覆盖率门禁、当日实时快照合并、财务因子计算、资金明细补全、因子表构建、
证券权限筛选、板块热度与科创板同业映射。

边界：

- 因子文件读取与新鲜度判定仍由交易主模块负责，本模块只接收已判定的因子帧；
- 覆盖率阈值由调用方传入，交易主模块保持唯一配置源（部署规模阈值可被运维
  与回放用例改写，不能在这里另存一份）；
- 只返回普通数据结构，不产生交易、不写库、不开事务；
- 外部数据仍走既有取数模块，不新增数据源，也不导入 ``paper_trading``。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

import data_fetcher as dfc
import factors as F
import paper_account_specs as ACS
import paper_candidate_live_fields as PCLF
import paper_candidate_projection as PCP
import paper_timing as PTM
import strategies as S
import universe as U
from paper_numeric import num as _num
from paper_trading_rules import security_scope as _security_scope
from paper_trading_rules import trading_weekday_lag as _trading_weekday_lag
from strategy_policies import NEW_STRATEGY_ID


def _date(value=None):
    """Normalize a date-like value; local copy, same contract as the trading module."""
    if value is None:
        return dt.date.today()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _sector_key(value):
    text = str(value or "").strip()
    for suffix in ("行业", "Ⅱ", "Ⅲ", "II", "III", "概念"):
        text = text.replace(suffix, "")
    return text

def _sector_heat_map(universe, sector_rows=None):
    """Build a live breadth-aware sector map.

    The old map was based only on the leading pages of the gainers list.  That
    identifies a sector after several names have already accelerated, but it
    misses the earlier, more useful rotation phase.  We retain that source as
    corroboration, while calculating breadth and median strength from the
    full live universe.  This is a ranking context only: it cannot bypass any
    security, quote, Q-level, capital or risk gate.
    """
    rows = list(sector_rows or [])
    source = "盘中热点板块"

    # First build same-snapshot industry statistics from the full universe.
    # A valid onset needs breadth and positive median strength, not one
    # limit-up stock.  Missing main-flow data is neutral rather than positive.
    grouped = {}
    for item in universe or []:
        key = _sector_key(item.get("industry"))
        pct = item.get("pct")
        if not key or not isinstance(pct, (int, float)) or abs(float(pct)) > 20:
            continue
        bucket = grouped.setdefault(key, {"pct": [], "main_pct": [], "codes": set()})
        bucket["pct"].append(float(pct))
        main_pct = item.get("main_pct")
        if isinstance(main_pct, (int, float)) and abs(float(main_pct)) <= 100:
            bucket["main_pct"].append(float(main_pct))
        code = str(item.get("code") or "")
        if code:
            bucket["codes"].add(code)

    def _median(values):
        values = sorted(values)
        if not values:
            return 0.0
        middle = len(values) // 2
        return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2.0

    live_metrics = {}
    for key, bucket in grouped.items():
        values = bucket["pct"]
        count = len(bucket["codes"]) or len(values)
        if count < 5:
            continue
        median_pct = _median(values)
        positive_ratio = sum(value > 0 for value in values) / len(values)
        median_main_pct = _median(bucket["main_pct"]) if bucket["main_pct"] else None
        # Early rotation: broad participation, modest positive median and no
        # single-name inference.  The flow clause is only required when flow
        # coverage exists for the group; unknown flow never grants a bonus.
        flow_confirmed = median_main_pct is not None and median_main_pct >= 0.0
        onset = (
            median_pct >= 0.35
            and positive_ratio >= 0.58
            and flow_confirmed
        )
        onset_score = 0.0
        if onset:
            onset_score = min(
                1.0,
                0.34
                + min(0.26, median_pct * 0.18)
                + min(0.24, max(0.0, positive_ratio - 0.50) * 1.2)
                + (0.10 if flow_confirmed else 0.0),
            )
        live_metrics[key] = {
            "member_count": count,
            "median_pct": round(median_pct, 3),
            "positive_ratio": round(positive_ratio, 3),
            "median_main_pct": round(median_main_pct, 3) if median_main_pct is not None else None,
            "early_rotation": bool(onset),
            "early_rotation_score": round(onset_score, 3),
        }

    if not rows:
        rows = [
            {
                "name": key,
                "pct": metrics["median_pct"],
                "leader_count": metrics["member_count"],
                "positive_ratio": metrics["positive_ratio"],
                "source": "全市场实时行业聚合",
            }
            for key, metrics in live_metrics.items()
        ]
        rows.sort(key=lambda row: (_num(row.get("pct"), -999), _num(row.get("positive_ratio"), 0)), reverse=True)
        source = "全市场实时行业聚合"

    # Keep the external top-sector rows, then add broad early-rotation groups
    # that are not yet in the top-gainer pages.
    by_key = {_sector_key(row.get("name")): dict(row) for row in rows if _sector_key(row.get("name"))}
    for key, metrics in live_metrics.items():
        if metrics["early_rotation"] and key not in by_key:
            by_key[key] = {
                "name": key, "pct": metrics["median_pct"],
                "leader_count": metrics["member_count"],
                "positive_ratio": metrics["positive_ratio"],
                "source": "全市场实时行业聚合",
            }
    ordered = sorted(
        by_key.values(),
        key=lambda row: (
            bool(live_metrics.get(_sector_key(row.get("name")), {}).get("early_rotation")),
            _num(row.get("pct"), -999), _num(row.get("positive_ratio"), 0),
        ),
        reverse=True,
    )
    output = {}
    for rank, row in enumerate(ordered[:35], 1):
        key = _sector_key(row.get("name"))
        if not key:
            continue
        raw_pct = _num(row.get("pct"), 0)
        if abs(raw_pct) > 30:
            continue
        pct = max(-20.0, min(20.0, raw_pct))
        metrics = live_metrics.get(key, {})
        onset_score = _num(metrics.get("early_rotation_score"), 0.0)
        strength = max(0.0, 2.5 - (rank - 1) * 0.10) + max(0.0, pct) * 0.18 + onset_score * 0.35
        output[key] = {
            "rank": rank,
            "name": row.get("name") or key,
            "pct": round(pct, 2),
            "score": round(strength, 3),
            "top_stock": row.get("top_stock"),
            "top_stock_code": row.get("top_stock_code"),
            "source": row.get("source") or source,
            **metrics,
        }
    return output

def _sector_entry_threshold_context(sector_heat, market, candidate_heat=None):
    """Derive a same-day sector-entry threshold from market and heat dispersion.

    A static 0.68 threshold makes a broad momentum day and a weak rotation day
    indistinguishable.  The context is persisted on each candidate so an order
    audit can show the exact daily threshold and why it moved.
    """
    heats = list((sector_heat or {}).values())
    top = heats[:10]
    positive_top = sum(1 for item in top if _num(item.get("pct"), 0) > 0)
    light = str((market or {}).get("light") or "unknown")
    breadth = _num((market or {}).get("breadth_up_pct"), None)
    threshold = {"green": 0.59, "yellow": 0.63, "red": 0.68}.get(light, 0.67)
    reasons = [f"市场{ {'green':'绿灯','yellow':'黄灯','red':'红灯'}.get(light, '数据未知') }"]
    if breadth is not None:
        if breadth >= 65:
            threshold -= 0.03
            reasons.append(f"上涨家数占比 {breadth:.1f}% 较高")
        elif breadth >= 55:
            threshold -= 0.01
            reasons.append(f"上涨家数占比 {breadth:.1f}% 偏强")
        elif breadth < 45:
            threshold += 0.03
            reasons.append(f"上涨家数占比 {breadth:.1f}% 偏弱")
        elif breadth < 50:
            threshold += 0.015
            reasons.append(f"上涨家数占比 {breadth:.1f}% 偏谨慎")
    else:
        threshold += 0.015
        reasons.append("市场宽度缺失，额外保守")
    if top:
        if positive_top >= 7:
            threshold -= 0.02
            reasons.append(f"前{len(top)}热点中 {positive_top} 个上涨，热点扩散充分")
        elif positive_top <= 2:
            threshold += 0.025
            reasons.append(f"前{len(top)}热点中仅 {positive_top} 个上涨，热点集中")
    else:
        threshold += 0.02
        reasons.append("热点板块样本不足")
    candidate_heat = candidate_heat or {}
    rank = int(_num(candidate_heat.get("rank"), 999))
    pct = _num(candidate_heat.get("pct"), 0)
    if rank <= 3 and pct >= 1.5:
        threshold -= 0.015
        reasons.append(f"所属板块第{rank}名且涨幅 {pct:+.2f}%")
    elif rank > 8 or pct < 0.5:
        threshold += 0.02
        reasons.append(f"所属板块第{rank}名、涨幅 {pct:+.2f}%")
    threshold = round(max(0.54, min(0.72, threshold)), 3)
    return {
        "threshold": threshold,
        "market_light": light,
        "breadth_up_pct": breadth,
        "positive_top_count": positive_top,
        "top_sample_count": len(top),
        "sector_rank": rank,
        "sector_pct": round(pct, 2),
        "reason": "；".join(reasons),
    }

def _trend_entry_threshold_context(market, trend_structure, distance_ma20,
                                   main_pct, pct):
    """按当日环境动态计算趋势回踩确认门槛。

    趋势回踩不是固定分数闸门：强趋势、宽度健康且回踩位置合理时，
    可以降低确认线；弱市、波动放大或均线结构不完整时提高确认线。
    结构性否决仍由 ``_strategy_entry_assessment`` 的 blockers 负责，
    本函数只负责可解释的分数门槛，并把所有输入写入审计快照。
    """
    market = market or {}
    light = str(market.get("light") or "unknown").lower()
    # v3: 只放松趋势回踩的确认分软门槛；红灯/未知市场、双源行情、
    # 权限、流动性和均线破坏等硬门禁保持不变。
    threshold = {"green": 0.53, "yellow": 0.57, "red": 0.68}.get(light, 0.63)
    reasons = [f"市场{ {'green':'绿灯','yellow':'黄灯','red':'红灯'}.get(light, '数据未知') }"]

    breadth = _num(market.get("breadth_up_pct"), None)
    if breadth is None:
        threshold += 0.015
        reasons.append("市场宽度缺失，额外保守")
    elif breadth >= 65:
        threshold -= 0.03
        reasons.append(f"上涨家数占比 {breadth:.1f}% 较强")
    elif breadth >= 55:
        threshold -= 0.015
        reasons.append(f"上涨家数占比 {breadth:.1f}% 偏强")
    elif breadth < 45:
        threshold += 0.035
        reasons.append(f"上涨家数占比 {breadth:.1f}% 偏弱")
    else:
        threshold += 0.015
        reasons.append(f"上涨家数占比 {breadth:.1f}% 偏谨慎")

    # 不同调用方字段名不完全一致，优先使用明确的百分比字段。
    volatility = None
    for key in ("index_volatility_pct", "market_volatility_pct", "volatility_pct"):
        if market.get(key) is not None:
            volatility = _num(market.get(key), None)
            break
    if volatility is not None:
        if volatility >= 2.5:
            threshold += 0.025
            reasons.append(f"指数波动 {volatility:.2f}% 偏高")
        elif volatility <= 1.0:
            threshold -= 0.01
            reasons.append(f"指数波动 {volatility:.2f}% 温和")

    if trend_structure >= 0.90 and distance_ma20 is not None and -0.03 <= distance_ma20 <= 0.05:
        threshold -= 0.025
        reasons.append("MA20/MA60多头且回踩位置合理")
    elif trend_structure >= 0.70:
        threshold -= 0.01
        reasons.append("均线结构基本修复")
    elif trend_structure < 0.50:
        threshold += 0.045
        reasons.append("均线结构偏弱")

    if distance_ma20 is None:
        threshold += 0.025
        reasons.append("MA20偏离度缺失")
    elif distance_ma20 < -0.06 or distance_ma20 > 0.09:
        threshold += 0.03
        reasons.append(f"偏离MA20 {distance_ma20 * 100:+.2f}%，位置不理想")
    elif -0.03 <= distance_ma20 <= 0.05:
        threshold -= 0.015
        reasons.append(f"偏离MA20 {distance_ma20 * 100:+.2f}%，处于回踩区")

    if main_pct <= -8:
        threshold += 0.025
        reasons.append(f"主力净流出 {main_pct:+.2f}%")
    if pct <= -2.5:
        threshold += 0.015
        reasons.append(f"实时涨跌幅 {pct:+.2f}% 偏弱")

    threshold = round(max(0.50, min(0.74, threshold)), 3)
    return {
        "version": "trend-pullback-threshold-v3",
        "threshold": threshold,
        "market_light": light,
        "breadth_up_pct": breadth,
        "volatility_pct": volatility,
        "trend_structure": round(_num(trend_structure), 3),
        "distance_ma20_pct": round(distance_ma20 * 100, 3) if distance_ma20 is not None else None,
        "main_pct": round(_num(main_pct), 3),
        "intraday_pct": round(_num(pct), 3),
        "relaxation": {
            "scope": "trend_pullback_soft_score_only",
            "baseline_delta": -0.02 if light == "green" else -0.03 if light == "yellow" else 0.0,
            "floor": 0.50,
            "hard_gates_unchanged": True,
        },
        "execution_zone": {"ma20_distance_min_pct": -10.0, "ma20_distance_max_pct": 14.0},
        "reason": "；".join(reasons),
    }

def _sector_overheat_guard(kline, quote=None, candidate_heat=None):
    """识别板块轮动中的个股追高/末端加速风险。

    板块强势只说明资金在流入，不代表当前价位仍有安全边际。这里使用
    同一只股票的已完成日线与实时价，动态组合 5/20 日涨幅、均线乖离、
    布林上轨位置和连续上涨天数。缺少足够历史时返回 ``unknown``，由上层
    保守降权而不是编造结论。
    """
    quote = quote or {}
    heat = candidate_heat or {}
    result = {
        "level": "unknown", "score": None, "ret5_pct": None, "ret20_pct": None,
        "ma20_distance_pct": None, "boll_position": None, "up_streak": None,
        "sector_pct": _num(heat.get("pct"), None), "sector_rank": int(_num(heat.get("rank"), 999)),
        "pullback_confirmed": False, "reason": "历史日线不足，暂不判定过热",
    }
    if kline is None or len(kline) < 25:
        return result
    try:
        closes = pd.to_numeric(kline["close"], errors="coerce").dropna()
    except (KeyError, TypeError, ValueError):
        return result
    if len(closes) < 25:
        return result
    close = _num(quote.get("price"), _num(closes.iloc[-1], 0))
    if close <= 0:
        return result
    ma20 = _num(closes.tail(20).mean(), None)
    std20 = _num(closes.tail(20).std(ddof=0), 0.0)
    if not ma20 or ma20 <= 0:
        return result
    ret5 = (float(closes.iloc[-1]) / float(closes.iloc[-6]) - 1.0) * 100
    ret20 = (float(closes.iloc[-1]) / float(closes.iloc[-21]) - 1.0) * 100
    distance = (close / ma20 - 1.0) * 100
    upper = ma20 + 2.0 * std20
    boll_position = (close - (ma20 - 2.0 * std20)) / max(upper - (ma20 - 2.0 * std20), 1e-9)
    streak = 0
    for idx in range(len(closes) - 1, 0, -1):
        if float(closes.iloc[idx]) > float(closes.iloc[idx - 1]):
            streak += 1
        else:
            break
    intraday_high = _num(quote.get("high"), 0.0)
    open_price = _num(quote.get("open_price"), 0.0)
    intraday_pullback_pct = (
        (close / intraday_high - 1.0) * 100
        if intraday_high > 0 and close > 0 else None
    )
    # "距离 MA20 不超过 6%" 是位置描述，不是日内回踩。此前它让不少
    # 正在加速的热点被误标为已经回踩。只有先从当日高点回撤至少 0.8%，
    # 再守住开盘价附近，才算可执行的回踩承接；缺少高/开盘价则不伪造确认。
    pullback = bool(
        intraday_pullback_pct is not None
        and intraday_pullback_pct <= -0.8
        and (open_price <= 0 or close >= open_price * 0.995)
    )
    score = 0.0
    score += 0.30 if ret5 >= 12 else 0.18 if ret5 >= 8 else 0.0
    score += 0.25 if ret20 >= 28 else 0.15 if ret20 >= 20 else 0.0
    score += 0.25 if distance >= 18 else 0.15 if distance >= 12 else 0.0
    score += 0.20 if boll_position >= 1.02 else 0.12 if boll_position >= 0.92 else 0.0
    score += 0.10 if streak >= 6 else 0.05 if streak >= 4 else 0.0
    if _num(heat.get("pct"), 0) >= 3.5 and int(_num(heat.get("rank"), 999)) <= 5:
        score += 0.08
    score = min(1.0, score)
    level = "extreme" if score >= 0.72 else "hot" if score >= 0.50 else "caution" if score >= 0.28 else "normal"
    reason = {
        "extreme": "个股处于连续加速、均线乖离或布林上轨末端，禁止板块轮动追入",
        "hot": "个股短线过热，等待回踩均线/布林中轨并重新放量确认",
        "caution": "个股已有明显短线加速，降低仓位并要求回踩确认",
        "normal": "个股位置未显示明显过热",
    }[level]
    result.update({
        "level": level, "score": round(score, 3), "ret5_pct": round(ret5, 2),
        "ret20_pct": round(ret20, 2), "ma20_distance_pct": round(distance, 2),
        "boll_position": round(boll_position, 3), "up_streak": streak,
        "intraday_high": round(intraday_high, 3) if intraday_high > 0 else None,
        "intraday_pullback_pct": round(intraday_pullback_pct, 2) if intraday_pullback_pct is not None else None,
        "pullback_confirmed": pullback, "reason": reason,
    })
    return result

def _star_sector_impulse(universe, asof_date=None):
    """Map broad STAR-board strength into a small same-industry bonus.

    STAR stocks remain untradable.  Their aggregate move is context only: at
    least five same-day valid peers, a positive majority and a positive median
    are required.  The bounded bonus cannot override any entry or risk gate.
    """
    grouped = {}
    latest = None
    dated_rows = []
    for item in universe or []:
        stamp = str(item.get("quote_at") or item.get("time") or "")
        source_day = stamp[:10]
        if len(source_day) == 10 and source_day[4:5] == "-" and source_day[7:8] == "-":
            dated_rows.append(source_day)
    target_day = _date(asof_date or dt.date.today()).isoformat()
    eligible_days = [value for value in dated_rows if value <= target_day]
    day = max(eligible_days, default=target_day)
    for row in universe or []:
        scope = _security_scope(row.get("code"), row.get("name"), row.get("risk_flag"))
        if scope["board"] != "科创板":
            continue
        quote_at = str(row.get("quote_at") or row.get("time") or "")
        if not quote_at or quote_at[:10] != day:
            continue
        pct = _num(row.get("pct"), None)
        sector = _sector_key(row.get("industry"))
        if not sector or pct is None or abs(pct) > 20:
            continue
        grouped.setdefault(sector, []).append(float(pct))
        latest = max(latest or quote_at, quote_at) if quote_at else latest
    output = {}
    for sector, values in grouped.items():
        if len(values) < 5:
            continue
        series = pd.Series(values, dtype="float64")
        median_pct = float(series.median())
        positive_ratio = float((series > 0).mean())
        if median_pct < 0.8 or positive_ratio < 0.60:
            continue
        bonus = min(0.12, max(0.0, median_pct - 0.5) * 0.018 + max(0.0, positive_ratio - 0.5) * 0.08)
        output[sector] = {
            "sample_count": len(values),
            "median_pct": round(median_pct, 2),
            "positive_ratio_pct": round(positive_ratio * 100, 1),
            "bonus": round(bonus, 3),
            "source": "科创板同业实时表现（仅作映射加分）",
            "asof": latest,
        }
    return output


def assemble_candidate_inputs(account_id, account, spec, asof_date, price_f,
                              base_universe, factor_freshness, *, sector_rows=None,
                              live_universe=None, live_asof_date=None, shared_inputs=None,
                              min_factor_rows=4_000, min_factor_coverage=0.85):
    """装配一次候选扫描的完整截面；门禁不通过时返回 ``(blocked, payload)``。

    调用方已读取因子文件并完成新鲜度判定，这里只处理"已通过新鲜度"之后的
    时效行过滤、覆盖率门禁、实时截面合并、因子表构建与证据附加。
    """
    eligible_factor_universe = {
        code for code, row in base_universe.items()
        if code and _security_scope(code, row.get("name"), row.get("risk_flag"))["allowed"]
    }
    if "last_date" not in price_f:
        return [], {"blocked": True, "reason": "选股因子缺少数据日期"}
    factor_dates = price_f["last_date"].astype(str).str[:10]
    # 盘中/周一不应把自然日直接当因子滞后日；以最近完整收盘日
    # 计算，避免周末后把周四因子误判为超过策略时效。
    try:
        lag_reference_day = U.latest_complete_trade_date(_date(asof_date))
    except Exception:
        lag_reference_day = _date(asof_date)
    lags = factor_dates.map(lambda value: _trading_weekday_lag(value, lag_reference_day))
    usable = lags.notna() & (lags <= spec["max_factor_lag"])
    total_factor_rows = len(price_f)
    dropped_stale = int((~usable).sum())
    price_f = price_f[usable].copy()
    if price_f.empty:
        latest = factor_dates.max() if len(factor_dates) else None
        return [], {
            "blocked": True,
            "reason": f"{spec['entry_model_name']}没有满足 {spec['max_factor_lag']} 个工作日时效要求的因子；最新 {latest or '未知'}",
        }
    usable_coverage = len(price_f) / max(total_factor_rows, 1)
    eligible_usable_codes = set(price_f.index.astype(str)) & eligible_factor_universe
    eligible_factor_coverage = len(eligible_usable_codes) / max(len(eligible_factor_universe), 1)
    if (
        len(price_f) < min_factor_rows
        or usable_coverage < min_factor_coverage
        or eligible_factor_coverage < min_factor_coverage
    ):
        return [], {
            "blocked": True,
            "reason": (
                f"{spec['entry_model_name']}全市场因子覆盖不足：有效 {len(price_f)} / {total_factor_rows} "
                f"（{usable_coverage * 100:.1f}%），可交易范围 {len(eligible_usable_codes)} / "
                f"{len(eligible_factor_universe)}（{eligible_factor_coverage * 100:.1f}%），需至少 "
                f"{min_factor_coverage * 100:.0f}%；本轮不使用残缺样本选股"
            ),
            "usable_factor_rows": len(price_f), "total_factor_rows": total_factor_rows,
            "factor_coverage_pct": round(usable_coverage * 100, 2),
            "eligible_factor_rows": len(eligible_usable_codes),
            "eligible_universe_rows": len(eligible_factor_universe),
            "eligible_factor_coverage_pct": round(eligible_factor_coverage * 100, 2),
            "dropped_stale_rows": dropped_stale,
        }
    factor_date = str(price_f["last_date"].dropna().max())
    factor_oldest_date = str(price_f["last_date"].dropna().min())
    style = (account.get("style") if isinstance(account, dict) else None) or spec["default_style"]
    profile = ACS.style_profile(style, default_style=spec["default_style"])
    expected_live_day = _date(live_asof_date if live_asof_date is not None else asof_date).isoformat()
    live_map = {}
    live_scan = {
        "status": "historical_only" if live_universe is None else "blocked",
        "expected_quote_day": expected_live_day,
        "eligible_rows": len(eligible_factor_universe),
        "covered_rows": 0,
        "coverage_pct": 0.0,
        "required_rows": None,
        "scope": "eligible_a_share_universe",
    }
    for row in (live_universe or []):
        code = str(row.get("code") or "")
        if not code or str(row.get("quote_at") or "")[:10] != expected_live_day:
            continue
        try:
            if pd.isna(row.get("price")) or pd.isna(row.get("pct")) or float(row.get("price")) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        live_map[code] = row
    if live_universe is not None:
        # A live cross-sectional scan must never rank a code using persisted
        # price/pct/flow fields.  Metadata may come from the universe file,
        # but missing live quotes are excluded from this run entirely.
        covered_live_codes = set(live_map) & eligible_factor_universe
        required_live_rows = max(min_factor_rows,
                                 int(len(eligible_factor_universe) * min_factor_coverage + 0.999999))
        live_scan.update({
            "required_rows": required_live_rows,
            "covered_rows": len(covered_live_codes),
            "coverage_pct": round(len(covered_live_codes) / max(len(eligible_factor_universe), 1) * 100, 2),
            "status": "fresh_day_coverage" if len(covered_live_codes) >= required_live_rows else "blocked",
        })
        if len(covered_live_codes) < required_live_rows:
            return [], {
                "blocked": True,
                "reason": f"当日实时行情覆盖不足：{len(covered_live_codes)} / {len(eligible_factor_universe)}，需至少 {required_live_rows} 只",
                "live_quote_day": expected_live_day,
                "live_quote_rows": len(covered_live_codes),
                "live_eligible_rows": len(eligible_factor_universe),
                "live_coverage_pct": round(len(covered_live_codes) / max(len(eligible_factor_universe), 1) * 100, 2),
                "full_market_scan": live_scan,
            }
        live_codes = set(live_map)
        price_f = price_f.loc[price_f.index.astype(str).isin(live_codes)].copy()
        if price_f.empty:
            return [], {"blocked": True, "reason": "当日实时行情未覆盖任何有效因子股票"}
    universe = []
    for code in price_f.index:
        base = dict(base_universe.get(str(code)) or {"code": str(code)})
        if str(code) in live_map:
            base.update(live_map[str(code)])
        universe.append(base)
    if live_map:
        # 技术因子仍来自已收盘日线；实时价格/涨跌幅/资金字段用于当日排序和容量。
        # amount/turnover must still enter before build_factor_table; assign
        # by column so this pre-build step does not repeat scalar writes.
        PCLF.overlay_factor_inputs(price_f, live_map)
    # Use the same financial feed as public selection.  The report-period
    # values remain compatible with the running strategies; publication-time
    # proof is attached below to the concrete candidate records instead of
    # pretending a report period is a disclosure timestamp.
    try:
        with PTM.phase("candidate.finance_source"):
            finance = dfc.fetch_finance_latest()
    except Exception:
        finance = {"data": {}}
    # Keep the paper selector point-in-time consistent with the public
    # selector: a report/period that was not visible at the completed
    # signal date must not satisfy a profit gate.  The factor layer retains
    # unknown disclosures as shadow evidence, while strategies require the
    # explicit ``reported`` source for executable profit thresholds.
    # The new three-day strategy needs the raw reported profit values in order
    # to build its shadow lane.  An unknown publication timestamp must never be
    # treated as reported, but hiding the value before the strategy runs makes
    # it impossible to ask the disclosure source for confirmation and leaves
    # the account permanently at zero candidates.  Keep those values as
    # ``shadow`` for this one model; the strategy still requires an explicit
    # publication record before a formal pick is returned.
    finance_asof = None if account_id == NEW_STRATEGY_ID else asof_date
    # ``universe`` 由当前 universe.json 与当日 live_map 组成，即**当前实时截面**：
    # 它的决策时点是现在，不是已收盘日线 cutoff。用 cutoff 判它会清空 PE/PB/
    # 换手/资金/行业（``build_factor_table`` 的 pct 正取自这里），直接打掉盘中扫描。
    if shared_inputs is None:
        with PTM.phase("candidate.fundamentals"):
            fund = F.compute_fundamental_factors(
                universe, finance, asof=finance_asof, snapshot_asof=F.live_snapshot_asof())
    else:
        with PTM.phase("candidate.fundamentals"):
            fund = shared_inputs.fundamentals(universe, finance, asof=finance_asof)
    live_flow = {
        str(code): row.get("main_pct")
        for code, row in live_map.items()
        if isinstance(row.get("main_pct"), (int, float))
    }
    live_super_flow = {
        str(code): row.get("super_net")
        for code, row in live_map.items()
        if isinstance(row.get("super_net"), (int, float))
    }
    # 补齐缺失的超大单/主力资金数据。底层是 60 秒全市场共享快照，
    # 必须覆盖所有有效候选而非旧版的前 100 只，否则后排股票会长期
    # 以“无超大单资金”反复被审计拒绝。
    _flow_supplemented_codes = set()
    _missing_super_codes = {
        str(code) for code in eligible_factor_universe
        if code in live_map and (
            not isinstance(live_map[code].get("super_net"), (int, float))
            or not isinstance(live_map[code].get("main_pct"), (int, float))
        )
    }
    if _missing_super_codes:
        try:
            with PTM.phase("candidate.flow_source"):
                _supplemented = dfc.enrich_live_flow_details(live_universe, _missing_super_codes)
            for _code, _detail in _supplemented.items():
                if _code in live_map:
                    for _flow_field in ("super_net", "main_net", "big_net", "mid_net", "small_net", "main_pct"):
                        if not isinstance(live_map[_code].get(_flow_field), (int, float)) and isinstance(_detail.get(_flow_field), (int, float)):
                            live_map[_code][_flow_field] = _detail[_flow_field]
                    live_map[_code]["flow_source"] = _detail.get("flow_source")
                    live_map[_code]["flow_quote_at"] = _detail.get("quote_at") or _detail.get("flow_fetched_at")
                    _flow_supplemented_codes.add(_code)
                if isinstance(_detail.get("super_net"), (int, float)):
                    live_super_flow[_code] = _detail["super_net"]
                if isinstance(_detail.get("main_pct"), (int, float)):
                    live_flow[_code] = _detail["main_pct"]
        except Exception:
            pass
    with PTM.phase("candidate.factor_table"):
        table = S.build_factor_table(
            price_f, fund, sentiment=None,
            realtime_flow=live_flow,
            realtime_super_flow=live_super_flow,
        )
    if live_universe is not None:
        # 日线技术指标/财务仍来自已下载的完整历史；当天可变字段必须由
        # 同一轮实时快照覆盖，不能继续沿用上一交易日的收盘价或涨跌幅。
        table = PCP.overlay_live_fields(table, live_map, factor_date)
    star_sector_impulse = _star_sector_impulse(universe, asof_date)
    # Pull a column once rather than dispatching thousands of scalar pandas
    # indexers. Preserve the existing path for non-unique axes.
    unique_axes = table.index.is_unique and table.columns.is_unique
    names = dict(table["name"].items()) if unique_axes and "name" in table else None
    eligible_codes = [
        code for code in table.index
        if _security_scope(
            code,
            (names[code] if names is not None else table.loc[code, "name"]) if "name" in table else (base_universe.get(str(code)) or {}).get("name"),
            (base_universe.get(str(code)) or {}).get("risk_flag"),
        )["allowed"]
    ]
    table = table.loc[eligible_codes].copy()
    industries = dict(table["industry"].items()) if unique_axes and not table.empty else None
    sector_keys = {
        code: _sector_key(industries[code] if industries is not None else table.loc[code, "industry"])
        for code in table.index
    }
    # The paper execution path currently has no authoritative disclosure
    # timestamp for financial reports.  Keep profit values visible in the
    # candidate snapshot, but never pretend they are point-in-time proven.
    # This makes the limitation explicit in audit/research without silently
    # changing the three strategy rule set to an empty universe.
    if "profit_source" in table:
        table["profit_source"] = table["profit_source"].fillna("unknown")
    table["star_sector_bonus"] = pd.Series({
        code: _num((star_sector_impulse.get(sector_keys[code]) or {}).get("bonus"), 0.0)
        for code in table.index
    }).reindex(table.index).fillna(0.0)
    # 外部快照中的停牌/缺失值可能以 ``--`` 等字符串进入表格。策略比较前
    # 统一清洗所有数值列，防止一次脏值中断整轮 5 分钟全市场扫描。
    numeric_columns = (
        "price", "pct", "amount", "turnover", "main_pct", "super_net",
        "flow", "mom5_raw", "mom20_raw", "mom60_raw", "vol_surge_raw",
        "rsi14_raw", "pe", "pb", "roe", "profit_yoy",
    )
    for column in numeric_columns:
        if column in table:
            table[column] = pd.to_numeric(table[column], errors="coerce")
    # 板块热度不仅服务于板块轮动策略，也作为三个模拟策略的同日
    # “热门启动段”上下文。它只进入排序/审计，不改变证券权限和下单硬门禁。
    sector_heat = _sector_heat_map(universe, sector_rows)
    if sector_heat:
        table["sector_heat_score"] = pd.Series(
            {
                code: _num(
                    (sector_heat.get(sector_keys[code]) or {}).get("score"),
                    0.0,
                )
                for code in table.index
            }
        ).reindex(table.index).fillna(0.0)
        table["sector_early_rotation"] = pd.Series(
            {
                code: bool(
                    (sector_heat.get(sector_keys[code]) or {}).get("early_rotation")
                )
                for code in table.index
            }
        ).reindex(table.index).fillna(False)
        table["sector_early_rotation_score"] = pd.Series(
            {
                code: _num(
                    (sector_heat.get(sector_keys[code]) or {}).get("early_rotation_score"),
                    0.0,
                )
                for code in table.index
            }
        ).reindex(table.index).fillna(0.0)
    else:
        table["sector_heat_score"] = 0.0
        table["sector_early_rotation"] = False
        table["sector_early_rotation_score"] = 0.0
    if style == "sector":
        heat_values = pd.Series(
            {
                code: _num((sector_heat.get(sector_keys[code]) or {}).get("score"), -1.5)
                for code in table.index
            }
        )
        table["sentiment"] = F.zscore(heat_values).reindex(table.index).fillna(-1.5)
    return None, {
        "account_id": account_id,
        "style": style,
        "profile": profile,
        "price_f": price_f,
        "universe": universe,
        "live_map": live_map,
        "live_scan": live_scan,
        "table": table,
        "sector_heat": sector_heat,
        "star_sector_impulse": star_sector_impulse,
        "factor_date": factor_date,
        "factor_oldest_date": factor_oldest_date,
        "factor_freshness": factor_freshness,
        "eligible_factor_universe": eligible_factor_universe,
        "eligible_usable_codes": eligible_usable_codes,
        "eligible_factor_coverage": eligible_factor_coverage,
        "usable_coverage": usable_coverage,
        "total_factor_rows": total_factor_rows,
        "dropped_stale": dropped_stale,
        "flow_supplemented_codes": _flow_supplemented_codes,
    }
