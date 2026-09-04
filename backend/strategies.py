# -*- coding: utf-8 -*-
"""用户定义的三套规则型选股策略。

三套策略先做硬条件筛选，再按超大单净流入排序。所有日线条件只使用
本地已完成的交易日 K 线；实时资金只参与排序，不冒充历史信号。
"""
import numpy as np
import pandas as pd

import factors as F


STRATEGIES = {
    "three_day": {
        "name": "三日策略",
        "desc": "三连阳且收盘价突破 BOLL 中轨，归母净利润按最新财报报告期进度达到 5000 万元门槛；仅主板/创业板，按超大单资金并结合科创同业映射加分排序",
        "horizon_days": 3,
    },
    "five_day": {
        "name": "五日策略",
        "desc": "连续5日收盘价在 MA10 上方，现价在 BOLL 中轨和 MA60 上方，月线超跌，归母净利润按报告期进度达到 2000 万元；仅主板/创业板，按超大单资金并结合科创同业映射加分排序",
        "horizon_days": 5,
    },
    "ten_day": {
        "name": "十日策略",
        "desc": "连续5日收盘价在 MA10 上方，现价在所有主要均线上方，月线超跌；仅主板/创业板，按超大单资金并结合科创同业映射加分排序",
        "horizon_days": 10,
    },
    "reported_profit_breakout": {
        "name": "三日策略",
        "desc": "三连阳突破 BOLL 中轨并以最新已披露累计净利润按年化进度达到 5000 万元，或连续 5 日收盘高于 MA5 且已披露年净利润达到 2 亿元；现价站上 MA5/MA10/MA20/MA60，排除科创板、北交所及 ST/退市股，按实时超大单资金排序",
        "horizon_days": 5,
        "metadata": {
            "kind": "paper",
            "hard_conditions": {
                "entry_paths": [
                    "three_up + boll_mid_breakout + latest_reported_cumulative_profit_annualized >= 50000000",
                    "above_ma5_5d + latest_reported_annual_net_profit >= 200000000",
                ],
                "major_ma_guard": "price_above_ma5_ma10_ma20_ma60",
                "scope_guard": "main_or_chinext_only; exclude_star_bse_st_delist",
                "disclosure_required": True,
            },
            "sort": "pullback_liquidity_flow_v1",
        },
    },
    "main_force_top10": {
        "name": "超强主力股",
        "desc": "全市场按板块扩散、主力/超大单强度、成交活跃与趋势承接选出每日10只观察股；盘中再确认资金持续性后最多持有3只",
        "horizon_days": 5,
        "metadata": {"kind": "paper", "daily_candidate_limit": 10,
                     "position_limit": 3, "sort": "main_force_composite_desc"},
    },
}

# 保留给历史分析和通用排序使用；实时选股的最终次序始终以超大单净流入为主。
WEIGHTS = {
    "three_day": {"mom_short": 0.65, "flow": 0.35},
    "five_day": {"flow": 0.70, "quality": 0.30},
    "ten_day": {"flow": 0.60, "mom": 0.40},
}

# 模拟盘继续使用原有三套独立模型；这些内部策略不会出现在“策略选股”菜单。
PAPER_WEIGHTS = {
    "one_to_two": {"mom_short": 0.45, "flow": 0.25, "volsurge": 0.20, "sentiment": 0.10},
    "bottom_reversal": {"value": 0.28, "quality": 0.18, "volsurge": 0.22, "flow": 0.15, "mom_short": 0.10, "rsi": 0.07},
    # 结构进化后的趋势延续模型：不以“超跌/抄底”为买入理由，要求
    # 均线结构、动量和资金同步。初始只作为影子候选，需通过进化门禁后启用。
    "trend_continuation": {"mom_short": 0.28, "mom": 0.22, "flow": 0.20, "volsurge": 0.15, "quality": 0.15},
    "sentiment_pioneer": {"sentiment": 0.40, "flow": 0.25, "mom_short": 0.20, "volsurge": 0.15},
}

# ===== 2026-08-28 选股负 alpha 修复 =====
# 实证（08-20~08-27 全部成交信号 K 线回放，reports/selection_alpha_report.md）：
# 四策略买入次日均值 -0.7%~-1.7%，同期随机基准 +0.05%；排序分与次日收益
# 相关系数 -0.29，mom5 -0.20，主力净流入 +0.04（无预测力）。根因是全部
# 策略追高动量、无均值回归约束。以下常数与辅助函数为该项修复的一部分。
MOM5_OVERHEAT_PCT = 0.05      # 5 日动量超过此值视为过热
MOM20_OVERHEAT_PCT = 0.15     # 20 日动量超过此值视为过热
OVERHEAT_RANK_PENALTY = 0.35  # 过热候选的排序分扣减
SECTOR_CLIMAX_MOM5 = 0.08     # 板块平均 5 日动量超过此值视为情绪高潮，禁入


def _strategy_numeric(frame, name):
    if name in frame.columns:
        return pd.to_numeric(frame[name], errors="coerce")
    return pd.Series(np.nan, index=frame.index)


def _overheat_mask(frame):
    mom5 = _strategy_numeric(frame, "mom5_raw") if "mom5_raw" in frame.columns else _strategy_numeric(frame, "mom5")
    mom20 = _strategy_numeric(frame, "mom20_raw") if "mom20_raw" in frame.columns else _strategy_numeric(frame, "mom20")
    return ((mom5 > MOM5_OVERHEAT_PCT) | (mom20 > MOM20_OVERHEAT_PCT)).fillna(False)


def _apply_overheat_penalty(score, frame, penalty=OVERHEAT_RANK_PENALTY):
    """过热候选（短期涨幅透支）在排序分上直接扣减，抑制追高入场。"""
    mask = _overheat_mask(frame).reindex(score.index).fillna(False)
    return score.mask(mask, score - penalty)


def _gate_market_light(gate):
    if not isinstance(gate, dict):
        return ""
    return str(gate.get("light") or "").strip().lower()

# 模拟盘可进化的选股条件。基础条件保留在代码中，进化层只写入
# 这些白名单字段，既允许增删条件，又不会把任意代码注入选股流程。
PAPER_CONDITION_DEFAULTS = {
    "one_to_two": {
        "enabled": {"first_board_bonus": True, "pct_band": True, "chase_guard": True, "weak_guard": True, "board_acceleration": True},
        "first_board_bonus": 0.22, "pct_low": 1.0, "pct_high": 5.0,
        "chase_low": 5.0, "chase_high": 8.0, "chase_guard_pct": 9.0,
        "weak_guard_pct": -1.5, "chase_penalty": 0.45, "weak_penalty": 0.60,
        "board_acceleration_bonus": 0.16, "board_acceleration_pct": 5.0,
    },
    "bottom_reversal": {
        "enabled": {"low_volume_guard": True, "flow_confirm": True, "momentum_guard": True},
        "vol_surge_min": 1.0, "flow_min": 0.5, "mom20_min": -0.05,
        "low_volume_penalty": 0.30, "flow_bonus": 0.20, "momentum_penalty": 0.20,
    },
    "trend_continuation": {
        "enabled": {"trend_structure_guard": True, "momentum_guard": True, "flow_confirm": True, "breakout_bonus": True},
        "ma20_ma60_min": 0.0, "close_ma20_min": 0.0, "mom20_min": 0.0,
        "flow_min": 0.0, "breakout_bonus": 0.24, "broken_structure_penalty": 0.55,
    },
    "sentiment_pioneer": {
        # 板块热度是优先级，不再是一票否决；个股极强时走独立强势路径。
        "enabled": {"sentiment_guard": True, "individual_strong": True},
        "sentiment_min": -0.5, "sentiment_penalty": 0.20,
        "individual_pct_min": 3.5, "individual_pct_max": 8.5,
        "individual_flow_min": 0.65, "individual_vol_surge_min": 1.20,
        "individual_mom5_min": 2.0, "individual_bonus": 0.28,
    },
}


def paper_condition_defaults(strategy_id):
    """返回可序列化的条件基线，供自进化和审计页复用。"""
    source = PAPER_CONDITION_DEFAULTS.get(strategy_id, {})
    return {key: (dict(value) if isinstance(value, dict) else value) for key, value in source.items()}


def _paper_conditions(strategy_id, overrides=None):
    conditions = paper_condition_defaults(strategy_id)
    if not isinstance(overrides, dict):
        return conditions
    enabled = conditions.get("enabled", {})
    enabled.update({str(key): bool(value) for key, value in (overrides.get("enabled") or {}).items() if key in enabled})
    conditions["enabled"] = enabled
    for key in conditions:
        if key == "enabled" or key not in overrides:
            continue
        try:
            value = float(overrides[key])
            if np.isfinite(value):
                conditions[key] = value
        except (TypeError, ValueError):
            continue
    return conditions


def _bounded_weight_simplex(values, minimum=0.03, maximum=0.65):
    """Project positive factor weights onto a bounded, sum-to-one simplex."""
    raw = {key: max(1e-9, _number(value) or 0.0) for key, value in values.items()}
    result = {}
    free = set(raw)
    remaining = 1.0
    while free:
        total = sum(raw[key] for key in free) or float(len(free))
        trial = {key: remaining * raw[key] / total for key in free}
        lows = [key for key, value in trial.items() if value < minimum - 1e-12]
        highs = [key for key, value in trial.items() if value > maximum + 1e-12]
        if not lows and not highs:
            result.update(trial)
            break
        if highs:
            for key in highs:
                result[key] = maximum
                remaining -= maximum
                free.remove(key)
            continue
        for key in lows:
            result[key] = minimum
            remaining -= minimum
            free.remove(key)
    return {key: result[key] for key in values}

TECHNICAL_COLUMNS = (
    "three_up",
    "boll_mid_breakout",
    "above_ma5_5d",
    "above_ma10_5d",
    "above_boll_mid",
    "above_ma60",
    "above_all_ma",
    "weekly_oversold",
    "monthly_oversold",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
)


def zneg(series):
    return F.zscore(pd.Series(series, dtype="float64") * -1)


def _core_stock_preference(table):
    """Bounded preference for liquid/core names, never a hard eligibility gate.

    ``mktcap``/``float_cap`` and turnover are deterministic proxies for a
    traditional core-stock profile.  They only break close score ties and do
    not encode subjective popularity or a permanent whitelist.
    """
    cap = pd.to_numeric(
        table.get("float_cap", table.get("mktcap", pd.Series(np.nan, index=table.index))),
        errors="coerce",
    )
    if cap.notna().sum() == 0 and "mktcap" in table:
        cap = pd.to_numeric(table["mktcap"], errors="coerce")
    amount = pd.to_numeric(table.get("amount", pd.Series(np.nan, index=table.index)), errors="coerce")
    cap_rank = np.log1p(cap.clip(lower=0)).rank(pct=True).fillna(0.5)
    amount_rank = np.log1p(amount.clip(lower=0)).rank(pct=True).fillna(0.5)
    return (cap_rank * 0.65 + amount_rank * 0.35).clip(0.0, 1.0)


def _hot_leader_profile(table):
    """识别热门股的启动段，而不是等到连续上涨完成后才追踪。

    这是排序层证据，不是硬选股条件。它只奖励“当日有强度、资金和
    流动性同步、但尚未明显过热”的股票；接近涨停、短期乖离过大或
    动量已经极端的股票会被标记为 ``late_overheat`` 并降权。所有
    计算都来自当前 table，实时字段缺失时保持中性，不回填旧行情。
    """
    idx = table.index

    def numeric(name):
        return pd.to_numeric(
            table[name] if name in table else pd.Series(np.nan, index=idx),
            errors="coerce",
        )

    def rank01(series):
        known = series.notna()
        result = series.rank(pct=True).where(known)
        # 单行/极小横截面不能制造虚假的极端排名，缺失仍保持中性。
        return result.fillna(0.5).clip(0.0, 1.0)

    pct = numeric("pct")
    super_net = numeric("super_net_raw")
    if super_net.isna().all() and "super_net" in table:
        super_net = numeric("super_net")
    main_pct = numeric("main_pct")
    amount = numeric("amount")
    turnover = numeric("turnover")
    mom5 = numeric("mom5_raw")
    mom20 = numeric("mom20_raw")
    vol_surge = numeric("vol_surge_raw")
    sector = numeric("sector_heat_score")
    sector_onset = numeric("sector_early_rotation_score")
    sector_onset_flag = table.get("sector_early_rotation", pd.Series(False, index=idx)).fillna(False).astype(bool)
    price = numeric("price")
    ma20 = numeric("ma20")

    # 强势启动区间偏向 +1.2%~+6.5%；低于该区间不追踪，超过 8% 只
    # 保留少量观察分，避免把已涨多天的末端股误当作早期机会。
    pct_score = pd.Series(0.0, index=idx)
    pct_score = pct_score.mask(pct.between(1.2, 6.5, inclusive="both"), 1.0)
    # A broad sector that has just turned strong may surface before a stock
    # reaches +1.2%.  Allow that earlier window only when the full-market
    # sector breadth model confirms it; this is still a ranking cue, never a
    # buy permission and never an excuse to chase an overextended name.
    pct_score = pct_score.mask(pct.between(0.5, 1.2, inclusive="left"), 0.45)
    pct_score = pct_score.mask(
        pct.between(0.65, 1.2, inclusive="left") & sector_onset_flag,
        0.68,
    )
    pct_score = pct_score.mask(pct.gt(6.5) & pct.le(8.5), 0.62)
    pct_score = pct_score.mask(pct.gt(8.5) & pct.lt(10.0), 0.18)

    flow_score = (rank01(super_net) * 0.55 + rank01(main_pct) * 0.25
                  + rank01(amount) * 0.20)
    liquidity_score = (rank01(amount) * 0.65 + rank01(turnover) * 0.35)
    momentum_score = (rank01(mom5.clip(-20, 20)) * 0.60
                      + rank01(mom20.clip(-40, 60)) * 0.40)
    sector_score = rank01(sector)

    distance = (price / ma20 - 1.0).where(price.gt(0) & ma20.gt(0))
    # 启动段通常尚未远离 MA20；过热判断同时参考短中期涨幅和均线乖离。
    overheat = pd.Series(0.0, index=idx)
    overheat += mom5.ge(18).astype(float) * 0.30
    overheat += mom20.ge(35).astype(float) * 0.25
    overheat += distance.ge(0.15).astype(float) * 0.25
    overheat += pct.ge(9.0).astype(float) * 0.20
    overheat = overheat.clip(0.0, 1.0)

    onset_score = (sector_onset.clip(0.0, 1.0) * sector_onset_flag.astype(float))
    score = (pct_score * 0.28 + flow_score * 0.34 + liquidity_score * 0.14
             + momentum_score * 0.12 + sector_score * 0.08 + onset_score * 0.04)
    score = score.clip(0.0, 1.0)
    # 数据不完整时不能凭排名制造热门判断；至少需要价格和当日涨跌。
    valid = pct.notna() & price.notna()
    score = score.where(valid, 0.0)
    score = (score - overheat * 0.35).clip(0.0, 1.0)
    stage = pd.Series("normal", index=idx, dtype="object")
    early = valid & score.ge(0.58) & pct.ge(1.2) & pct.le(7.5) & overheat.lt(0.55)
    early |= (
        valid & sector_onset_flag & onset_score.ge(0.45)
        & score.ge(0.61) & pct.ge(0.65) & pct.lt(1.2) & overheat.lt(0.45)
    )
    confirmed = valid & score.ge(0.62) & overheat.lt(0.72) & ~early
    late = valid & overheat.ge(0.55)
    stage.loc[early] = "early_acceleration"
    stage.loc[confirmed] = "confirmed_strength"
    stage.loc[late] = "late_overheat"
    return {
        "score": score,
        "overheat": overheat,
        "stage": stage,
        "pct_score": pct_score,
        "flow_score": flow_score,
        "liquidity_score": liquidity_score,
        "momentum_score": momentum_score,
        "sector_score": sector_score,
        "sector_onset_score": onset_score,
    }


def _bottom_reversal_profile(table):
    """Score a base-to-rebound structure for ``bottom_reversal``.

    A bottom rebound is not the same as a low valuation or a stock that is
    already in a long uptrend.  The profile prefers a prior drawdown, a
    recent positive turn, price near MA20/MA60, improving flow and a modest
    volume expansion.  It is a bounded ranking score only; the existing
    quote, Q-level, T+1, capacity and stop gates remain authoritative.
    """
    idx = table.index

    def numeric(name):
        return pd.to_numeric(
            table[name] if name in table else pd.Series(np.nan, index=idx),
            errors="coerce",
        )

    def rank01(series):
        return series.rank(pct=True).where(series.notna()).fillna(0.5).clip(0.0, 1.0)

    price = numeric("price")
    ma20 = numeric("ma20")
    ma60 = numeric("ma60")
    mom5 = numeric("mom5_raw")
    mom20 = numeric("mom20_raw")
    rsi = numeric("rsi14_raw")
    flow = numeric("flow")
    volume = numeric("vol_surge_raw")
    pct = numeric("pct")
    above_boll = _bool_column(table, "above_boll_mid") | _bool_column(table, "boll_mid_breakout")

    dist20 = (price / ma20 - 1.0).where(price.gt(0) & ma20.gt(0))
    dist60 = (price / ma60 - 1.0).where(price.gt(0) & ma60.gt(0))
    # Ideal base location: still close to long averages, not a stock already
    # 20% above them.  A mild discount below MA60 is acceptable after a base.
    location = pd.Series(0.0, index=idx)
    location = location.mask(dist20.between(-0.08, 0.08, inclusive="both"), 0.85)
    location = location.mask(dist20.between(0.08, 0.14, inclusive="right"), 0.62)
    location = location.mask(dist60.between(-0.15, 0.06, inclusive="both") & location.eq(0), 0.58)
    location = location.mask(dist20.lt(-0.08) & dist20.ge(-0.15), 0.42)

    # Prior weakness plus a positive short turn is the central reversal shape.
    turn = pd.Series(0.0, index=idx)
    turn += ((mom20 <= 0) & mom5.gt(0)).astype(float) * 0.60
    turn += mom5.between(1.0, 10.0, inclusive="both").astype(float) * 0.25
    turn += above_boll.astype(float) * 0.15
    turn = turn.clip(0.0, 1.0)
    rsi_fit = (1.0 - (rsi - 55.0).abs() / 45.0).clip(0.0, 1.0)
    confirmation = (rank01(flow) * 0.50 + rank01(volume) * 0.25
                    + pct.clip(-5, 8).rank(pct=True).fillna(0.5) * 0.25)
    overheat = ((mom5.ge(15).astype(float) * 0.30)
                + (mom20.ge(35).astype(float) * 0.25)
                + (dist20.ge(0.15).astype(float) * 0.30)
                + (pct.ge(8.5).astype(float) * 0.15)).clip(0.0, 1.0)
    score = (location * 0.30 + turn * 0.28 + rsi_fit * 0.12
             + confirmation * 0.30 - overheat * 0.35).clip(0.0, 1.0)
    valid = (price.notna() & ma20.notna() & ma60.notna() & mom5.notna() & mom20.notna()
             & rsi.notna())
    score = score.where(valid, 0.0)
    structure = pd.Series("unknown", index=idx, dtype="object")
    structure.loc[valid & score.ge(0.62) & overheat.lt(0.55)] = "base_rebound"
    structure.loc[valid & score.ge(0.50) & ~structure.eq("base_rebound") & overheat.lt(0.75)] = "reversal_watch"
    structure.loc[valid & overheat.ge(0.55)] = "late_rebound_overheat"
    return {
        "score": score,
        "location": location,
        "turn": turn,
        "confirmation": confirmation,
        "overheat": overheat,
        "structure": structure,
    }


def build_factor_table(
    price_f: pd.DataFrame,
    fund_f: pd.DataFrame,
    sentiment: dict = None,
    realtime_flow: dict = None,
    realtime_super_flow: dict = None,
):
    """合并价格、财务和实时资金字段，并保留规则型策略需要的原始布尔条件。"""
    idx = price_f.index.intersection(fund_f.index)
    table = pd.DataFrame(index=idx)
    price = price_f.loc[idx]
    fund = fund_f.loc[idx]

    for column in (
        "name",
        "industry",
        "pe",
        "pb",
        "roe",
        "rev_yoy",
        "profit_yoy",
        "net_profit",
        "annual_net_profit",
        "annual_report_date",
        "annual_report_published_at",
        "report_published_at",
        "report_age_days",
        "profit_source",
        "mktcap",
        "float_cap",
        "report_date",
    ):
        table[column] = fund[column] if column in fund else np.nan
    table["price"] = price["price"]
    table["pct"] = fund["pct_today"] if "pct_today" in fund else np.nan
    table["amount"] = price["amount"] if "amount" in price else np.nan
    table["turnover"] = price["turnover"] if "turnover" in price else np.nan
    table["mom5_raw"] = price["mom5"]
    table["mom20_raw"] = price["mom20"]
    table["mom60_raw"] = price["mom60"]
    table["vol_surge_raw"] = price["vol_surge"]
    table["rsi14_raw"] = price["rsi14"]
    for column in TECHNICAL_COLUMNS:
        table[column] = price[column] if column in price else False

    # 关键列整列缺失时必须告警：下游 numeric_column().fillna(0) 会把数据
    # 事故变成静默策略退化（pct→0 使涨跌幅带/追高/弱势门禁全部恒假）。
    # 打印结构化 ALARM 供 scheduler.log 捕获，并挂到 table.attrs 供调用方
    # 透传到结果审计字段。
    _missing_columns = [
        name for name in ("pct", "amount", "turnover")
        if name not in table.columns or table[name].notna().sum() == 0
    ]
    if _missing_columns:
        import json as _json
        import sys as _sys
        print(_json.dumps({
            "alarm": "factor_columns_missing",
            "columns": _missing_columns,
            "note": "行情快照关键字段整列缺失，相关条件门禁将按 0 处理（候选可能异常为空）",
        }, ensure_ascii=False), flush=True)
        _sys.stdout.flush()
    table.attrs["factor_warnings"] = _missing_columns

    table["value"] = (zneg(fund["pe"]) + zneg(fund["pb"])) / 2
    table["quality"] = (
        F.zscore(fund["roe"])
        + F.zscore(fund["profit_yoy"]) * 0.5
        + F.zscore(fund["rev_yoy"]) * 0.5
    ) / 2
    table["mom"] = F.zscore(price["mom20"]) * 0.6 + F.zscore(price["mom60"]) * 0.4
    table["mom_short"] = (
        F.zscore(price["mom5"]) * 0.5
        + F.zscore(price["mom20"]) * 0.3
        + F.zscore(price["mom60"]) * 0.2
    )
    table["volsurge"] = F.zscore(price["vol_surge"])
    table["rsi"] = zneg(price["rsi14"])

    proxy_flow = F.zscore(price["flow_proxy"])
    if realtime_flow:
        flow_series = pd.Series({code: realtime_flow.get(code, np.nan) for code in idx})
        live_flow = F.zscore(flow_series, fill_missing=False)
        table["flow"] = live_flow.fillna(proxy_flow * 0.5)
        coverage = int(live_flow.notna().sum())
        table["flow_source"] = f"实时主力净流入占比({coverage}/{len(idx)})，缺失项用量价代理"
    else:
        table["flow"] = proxy_flow
        table["flow_source"] = "量价资金代理"

    fallback_super = (
        pd.to_numeric(fund["super_net"], errors="coerce").reindex(idx)
        if "super_net" in fund
        else pd.Series(np.nan, index=idx, dtype="float64")
    )
    if realtime_super_flow:
        live_super = pd.to_numeric(
            pd.Series({code: realtime_super_flow.get(code, np.nan) for code in idx}),
            errors="coerce",
        ).reindex(idx)
        table["super_net_raw"] = live_super.combine_first(fallback_super)
        table["super_net_live"] = live_super.notna()
        table["super_net_source"] = np.where(
            live_super.notna(), "实时超大单净流入", "历史资金字段（仅上下文）"
        )
    else:
        table["super_net_raw"] = fallback_super
        table["super_net_live"] = False
        table["super_net_source"] = "历史资金字段（仅上下文）"

    if sentiment:
        table["hot_rank"] = pd.Series(
            {code: (sentiment.get(code) or {}).get("hot_rank") for code in idx}
        )
        table["sentiment"] = F.zscore(
            pd.Series(
                {code: (sentiment.get(code) or {}).get("sentiment", np.nan) for code in idx}
            )
        ).fillna(0)
    else:
        table["hot_rank"] = np.nan
        table["sentiment"] = F.zscore(price["vol_surge"] * 0.5 + price["mom20"])
    return table


def _bool_column(table, name):
    if name not in table:
        return pd.Series(False, index=table.index)
    values = table[name]
    if values.dtype == bool:
        return values.fillna(False)
    return values.astype(str).str.lower().isin({"true", "1", "1.0"})


def _report_period_fraction(report_dates):
    """累计财报对应的年度进度：一季/半年/三季/全年。"""
    month_days = report_dates.fillna("").astype(str).str.slice(5, 10)
    return month_days.map(
        {"03-31": 0.25, "06-30": 0.50, "09-30": 0.75, "12-31": 1.00}
    )


def _financial_profit_gate(table, annual_threshold):
    """优先用最新季报累计净利润按报告期进度判断，缺失时回退最近年报。

    财务输入层会把缺失披露时间标成 ``shadow``，并把超出 PIT 截止日
    的值标成 ``future``。这些值可以留在研究证据里，但不能满足可执行
    选股的利润门槛；只有明确已披露的 ``reported`` 值可进入门控。
    """
    latest_profit = pd.to_numeric(table["net_profit"], errors="coerce")
    period_fraction = _report_period_fraction(table["report_date"])
    source = table.get("profit_source", pd.Series("unknown", index=table.index))
    source = source.fillna("unknown").astype(str).str.strip().str.lower()
    reported = source.eq("reported")
    latest_available = latest_profit.notna() & period_fraction.notna() & reported
    latest_pass = latest_profit.ge(annual_threshold * period_fraction)
    annual_profit = pd.to_numeric(table["annual_net_profit"], errors="coerce")
    # ``profit_source`` is the source selected by the factor layer (latest
    # report or annual fallback), so applying the same reported gate here also
    # prevents unknown annual values from leaking through a shadow fallback.
    annual_pass = annual_profit.ge(annual_threshold) & reported
    return latest_pass.where(latest_available, annual_pass)


def _permitted_a_share_mask(table):
    names = table["name"].fillna("").astype(str) if "name" in table else pd.Series("", index=table.index)
    codes = table.index.to_series().astype(str).str.zfill(6)
    # Identity screen must be fail-closed: a missing name means the ST /
    # delisting status is unknown, and such rows silently passed as clean
    # candidates before.  Require an explicit name to enter the pool.
    named = names.str.strip() != ""
    risk_free = named & ~names.str.upper().str.contains("ST", regex=False) & ~names.str.contains("退")
    main = codes.str.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))
    chinext = codes.str.startswith(("300", "301", "302"))
    return risk_free & (main | chinext)


def _table_column(table, name, default=np.nan):
    """Return a table column aligned to the table index, including absent fields."""
    if name not in table:
        return pd.Series(default, index=table.index)
    return table[name].reindex(table.index)


def _known_disclosure_value(series):
    """Whether a disclosure timestamp/period is explicitly present."""
    values = series.fillna("").astype(str).str.strip().str.lower()
    return ~values.isin({"", "nan", "nat", "none", "null", "-", "--"})


def _major_ma_mask(table):
    """Require the current price to be above MA5/MA10/MA20/MA60.

    ``above_all_ma`` is the canonical factor.  The numeric fallback keeps the
    rule testable with a hand-built table while still rejecting incomplete MA
    data rather than treating it as a pass.
    """
    if "above_all_ma" in table:
        return _bool_column(table, "above_all_ma")
    price = pd.to_numeric(_table_column(table, "price"), errors="coerce")
    moving_averages = [
        pd.to_numeric(_table_column(table, column), errors="coerce")
        for column in ("ma5", "ma10", "ma20", "ma60")
    ]
    known = price.notna()
    for moving_average in moving_averages:
        known &= moving_average.notna()
    if not moving_averages:
        return pd.Series(False, index=table.index)
    return known & pd.concat(moving_averages, axis=1).le(price, axis=0).all(axis=1)


def _reported_profit_breakout_masks(table):
    """Build the independent hard gates for ``reported_profit_breakout``.

    The two profit/technical paths are alternatives, while the major-MA and
    permitted-A-share gates are shared.  Publication metadata is checked
    directly in addition to ``profit_source`` so a value with an unknown
    disclosure time can only appear in the shadow lane.
    """
    permitted = _permitted_a_share_mask(table)
    major_ma = _major_ma_mask(table)
    three_technical = (
        _bool_column(table, "three_up")
        & _bool_column(table, "boll_mid_breakout")
    )
    five_technical = _bool_column(table, "above_ma5_5d")
    # The strategy is explicitly sorted by *current* super-large-order flow.
    # A missing live field may remain useful research context, but cannot
    # quietly become an executable candidate through a historical fallback.
    live_super = _bool_column(table, "super_net_live")

    source = _table_column(table, "profit_source", "unknown")
    reported = source.fillna("unknown").astype(str).str.strip().str.lower().eq("reported")

    report_dates = _table_column(table, "report_date")
    if "report_date" not in table and "report_period" in table:
        report_dates = _table_column(table, "report_period")
    period_fraction = _report_period_fraction(report_dates)
    latest_profit = pd.to_numeric(_table_column(table, "net_profit"), errors="coerce")
    latest_published = _known_disclosure_value(_table_column(table, "report_published_at"))
    latest_gate = (
        reported
        & latest_published
        & latest_profit.notna()
        & period_fraction.notna()
        & latest_profit.ge(50_000_000 * period_fraction)
    )

    annual_profit = pd.to_numeric(_table_column(table, "annual_net_profit"), errors="coerce")
    annual_dates = _table_column(table, "annual_report_date")
    if "annual_report_date" not in table:
        annual_dates = report_dates.where(report_dates.astype(str).str.slice(5, 10).eq("12-31"))
    annual_published = _table_column(table, "annual_report_published_at")
    annual_published = annual_published.where(_known_disclosure_value(annual_published))
    annual_published = annual_published.combine_first(_table_column(table, "report_published_at"))
    annual_date_known = _known_disclosure_value(annual_dates)
    annual_period = annual_dates.fillna("").astype(str).str.slice(5, 10)
    annual_period_known = annual_date_known & (
        annual_period.eq("12-31") | annual_dates.fillna("").astype(str).str.fullmatch(r"\d{4}")
    )
    annual_gate = (
        reported
        & _known_disclosure_value(annual_published)
        & annual_profit.notna()
        & annual_period_known
        & annual_profit.ge(200_000_000)
    )

    three_path = three_technical & latest_gate
    five_path = five_technical & annual_gate
    eligible = permitted & major_ma & live_super & (three_path | five_path)
    technical = permitted & major_ma & (three_technical | five_technical)
    return {
        "eligible": eligible,
        "technical": technical,
        "three_path": three_path,
        "five_path": five_path,
        "permitted": permitted,
        "major_ma": major_ma,
        "latest_gate": latest_gate,
        "annual_gate": annual_gate,
        "live_super": live_super,
    }


def _star_sector_bonus(table):
    """Bounded same-industry context; STAR rows can never become candidates."""
    codes = table.index.to_series().astype(str).str.zfill(6)
    star = table.loc[codes.str.startswith(("688", "689"))].copy()
    if star.empty or "industry" not in star or "pct" not in star:
        return pd.Series(0.0, index=table.index)
    star["pct_num"] = pd.to_numeric(star["pct"], errors="coerce")
    grouped = {}
    for industry, rows in star.dropna(subset=["industry", "pct_num"]).groupby("industry"):
        values = rows["pct_num"].clip(-20, 20)
        if len(values) < 5:
            continue
        median_pct = float(values.median())
        positive_ratio = float((values > 0).mean())
        if median_pct < 0.8 or positive_ratio < 0.60:
            continue
        grouped[str(industry)] = min(
            0.12,
            max(0.0, median_pct - 0.5) * 0.018 + max(0.0, positive_ratio - 0.5) * 0.08,
        )
    return table["industry"].astype(str).map(grouped).fillna(0.0)


def _eligible(strategy_id, table):
    permitted = _permitted_a_share_mask(table)

    if strategy_id == "reported_profit_breakout":
        return _reported_profit_breakout_masks(table)["eligible"]
    if strategy_id == "three_day":
        return (
            _bool_column(table, "three_up")
            & _bool_column(table, "boll_mid_breakout")
            & _financial_profit_gate(table, 50_000_000)
            & permitted
        )
    if strategy_id == "five_day":
        return (
            _bool_column(table, "above_ma10_5d")
            & _bool_column(table, "above_boll_mid")
            & _bool_column(table, "above_ma60")
            & _bool_column(table, "monthly_oversold")
            & _financial_profit_gate(table, 20_000_000)
            & permitted
        )
    return (
        _bool_column(table, "above_ma10_5d")
        & _bool_column(table, "above_all_ma")
        & _bool_column(table, "monthly_oversold")
        & permitted
    )


def _rule_reasons(strategy_id, row):
    latest_profit = _number(row.get("net_profit"))
    latest_date = str(row.get("report_date") or "")
    period_names = {
        "03-31": "一季报",
        "06-30": "半年报",
        "09-30": "三季报",
        "12-31": "年报",
    }
    period_name = period_names.get(latest_date[5:10], "最新财报")
    if latest_profit is not None:
        profit_text = f"{latest_date} {period_name}归母净利润 {latest_profit / 1e8:.2f} 亿元，已按报告期进度达标"
    else:
        annual_profit = _number(row.get("annual_net_profit"))
        profit_text = (
            f"季报缺失，使用最近年报归母净利润 {annual_profit / 1e8:.2f} 亿元兜底"
            if annual_profit is not None
            else "最新财报净利润缺失"
        )
    if strategy_id == "reported_profit_breakout":
        return [
            "三连阳突破 BOLL 中轨，或连续5个交易日收盘价高于 MA5",
            "现价高于 MA5、MA10、MA20、MA60 全部主要均线",
            "最新已披露财务数据满足对应累计/年净利润门槛",
            "已排除科创板、北交所与 ST/退市风险股",
            profit_text,
        ]
    if strategy_id == "three_day":
        return ["最近3个交易日连续收阳", "收盘价向上突破 BOLL 中轨", profit_text]
    if strategy_id == "five_day":
        return [
            "连续5个交易日收盘价位于 MA10 上方",
            "现价位于 BOLL 中轨和 MA60 上方",
            "月线超跌",
            profit_text,
            "已排除科创板与 ST/退市风险股",
        ]
    return [
        "连续5个交易日收盘价位于 MA10 上方",
        "现价位于 MA5、MA10、MA20、MA60 全部主要均线上方",
        "月线超跌后的日线修复",
        "已排除科创板与 ST/退市风险股",
    ]


def _reported_profit_breakout_reasons(row, entry_path):
    """Explain the exact hard path used by a reported-profit candidate."""
    reasons = []
    if entry_path == "three_day_profit":
        reasons.extend(["最近3个交易日连续收阳", "收盘价向上突破 BOLL 中轨"])
        latest_profit = _number(row.get("net_profit"))
        report_date = str(row.get("report_date") or "")
        fraction = _report_period_fraction(pd.Series([report_date])).iloc[0]
        annualized = latest_profit / fraction if latest_profit is not None and fraction else None
        if annualized is not None:
            reasons.append(
                f"{report_date} 已披露累计归母净利润 {latest_profit / 1e8:.2f} 亿元，"
                f"按年化进度 {annualized / 1e8:.2f} 亿元 ≥ 0.50 亿元"
            )
    else:
        reasons.append("连续5个交易日收盘价高于 MA5")
        annual_profit = _number(row.get("annual_net_profit"))
        annual_date = str(row.get("annual_report_date") or "")
        if annual_profit is not None:
            reasons.append(
                f"{annual_date} 已披露年归母净利润 {annual_profit / 1e8:.2f} 亿元 ≥ 2.00 亿元"
            )
    reasons.extend([
        "现价高于 MA5、MA10、MA20、MA60 全部主要均线",
        "已排除科创板、北交所与 ST/退市风险股",
    ])
    return reasons


def _run_reported_profit_breakout(table, topn=10, news_hits=None, gate=None):
    """Run the independent disclosed-profit paper strategy.

    Hard eligibility is evaluated before ranking.  The two technical/profit
    paths are alternatives; the MA and security-scope guards are shared.  The
    Ranking blends pullback quality (rev5, distance from the 5-day
    high), liquidity, and a bounded main-force flow rank; momentum
    overheat is penalized (2026-08-28 alpha fix).
    metric so realtime super-large-order flow cannot be masked by another
    score component.
    """
    masks = _reported_profit_breakout_masks(table)
    eligible = masks["eligible"]
    technical = masks["technical"]
    source = _table_column(table, "profit_source", "unknown")
    reported = source.fillna("unknown").astype(str).str.strip().str.lower().eq("reported")
    latest_published = _known_disclosure_value(_table_column(table, "report_published_at"))
    if "annual_report_published_at" not in table:
        annual_published = latest_published
    else:
        annual_published_values = _table_column(table, "annual_report_published_at")
        annual_published_values = annual_published_values.where(
            _known_disclosure_value(annual_published_values)
        ).combine_first(_table_column(table, "report_published_at"))
        annual_published = _known_disclosure_value(annual_published_values)
    disclosure_unknown = (
        (_bool_column(table, "three_up") & _bool_column(table, "boll_mid_breakout")
         & (~reported | ~latest_published))
        | (_bool_column(table, "above_ma5_5d") & (~reported | ~annual_published))
    )
    shadow_eligible = technical & ~eligible & disclosure_unknown

    def _super_net_series(frame):
        values = _table_column(frame, "super_net_raw")
        if "super_net_raw" not in frame and "super_net" in frame:
            values = _table_column(frame, "super_net")
        return pd.to_numeric(values, errors="coerce")

    candidates = table.loc[eligible].copy()
    candidates["super_net_sort"] = _super_net_series(candidates)
    # 修复5（2026-08-28）：super_net 与次日收益实证零相关（corr +0.04），
    # 从主排序键降级为成分之一。主排序改为：
    #   45% 回踩质量（rev5=距 5 日高点回撤，0~-4% 最优）
    # + 30% 流动性（amount 缺失时退回 vol_surge）
    # + 25% 主力净流入排名（绝对 65% + 流通盘强度 35%，保留中盘可发现性）。
    # 过热候选（修复1）扣减排序分，市场黄/红灯（修复3）加倍。
    float_cap = pd.to_numeric(_table_column(candidates, "float_cap"), errors="coerce")
    candidates["super_net_intensity"] = candidates["super_net_sort"].where(float_cap > 0) / float_cap.where(float_cap > 0)
    raw_rank = candidates["super_net_sort"].rank(method="average", pct=True, na_option="bottom")
    intensity_rank = candidates["super_net_intensity"].rank(method="average", pct=True, na_option="bottom")
    flow_rank = (raw_rank * 0.65 + intensity_rank * 0.35).where(candidates["super_net_sort"].notna(), 0.0)
    candidates["super_net_rank"] = flow_rank
    pullback = _strategy_numeric(candidates, "rev5")
    pullback_quality = (1.0 - (pullback.abs() / 0.08).clip(upper=1.0)).where(pullback.notna(), 0.0)
    liq = _strategy_numeric(candidates, "amount")
    if liq.isna().all():
        liq = _strategy_numeric(candidates, "vol_surge")
    liq_rank = liq.rank(method="average", pct=True, na_option="bottom")
    blended = 0.45 * pullback_quality + 0.30 * liq_rank + 0.25 * flow_rank
    risk_off = _gate_market_light(gate) in {"yellow", "red"}
    candidates["super_net_rank"] = _apply_overheat_penalty(
        blended, candidates, OVERHEAT_RANK_PENALTY * (2.0 if risk_off else 1.0),
    )
    candidates["mom5_sort"] = pd.to_numeric(_table_column(candidates, "mom5_raw"), errors="coerce")
    candidates = candidates.sort_values(
        ["super_net_rank", "super_net_sort", "mom5_sort"],
        ascending=[False, False, False],
        na_position="last",
    )
    candidates["score"] = candidates["super_net_rank"]

    news_map = {}
    vetoed_codes = set()
    for hit in news_hits or []:
        if not isinstance(hit, dict) or not hit.get("code"):
            continue
        code = str(hit["code"])
        news_map.setdefault(code, []).append(hit)
        if hit.get("tone", 0) < 0:
            vetoed_codes.add(code)
    news_vetoed = []
    for code in sorted(vetoed_codes):
        if code in candidates.index:
            news_vetoed.append({
                "code": code,
                "name": candidates.loc[code].get("name"),
                "reason": "命中负面公开事件，已从候选中剔除",
            })
    candidates = candidates.drop(index=[code for code in vetoed_codes if code in candidates.index])

    def _entry_path(row):
        code = row.name
        return "three_day_profit" if bool(masks["three_path"].get(code, False)) else "five_day_profit"

    picks = []
    for code, row in candidates.head(topn).iterrows():
        entry_path = _entry_path(row)
        hits = news_map.get(str(code), [])
        pick = {
            "code": code,
            "name": row.get("name"),
            "industry": row.get("industry"),
            "price": _number(row.get("price"), 2),
            "pct": _number(row.get("pct"), 2),
            "score": _number(row.get("score"), 6),
            "super_net": _number(row.get("super_net_sort"), 2),
            "super_net_raw": _number(row.get("super_net_sort"), 2),
            "super_net_source": row.get("super_net_source"),
            "quote_at": row.get("quote_at"),
            "pe": _number(row.get("pe"), 2),
            "pb": _number(row.get("pb"), 2),
            "roe": _number(row.get("roe"), 2),
            "profit_yoy": _number(row.get("profit_yoy"), 2),
            "net_profit": _number(row.get("net_profit"), 2),
            "annual_net_profit": _number(row.get("annual_net_profit"), 2),
            "annual_report_date": row.get("annual_report_date"),
            "annual_report_published_at": row.get("annual_report_published_at"),
            "report_date": row.get("report_date"),
            "report_published_at": row.get("report_published_at"),
            "profit_source": str(row.get("profit_source") or "unknown"),
            "mom5": _percent(row.get("mom5_raw")),
            "mom20": _percent(row.get("mom20_raw")),
            "mom60": _percent(row.get("mom60_raw")),
            "entry_path": entry_path,
            "reasons": _reported_profit_breakout_reasons(row, entry_path),
            "metadata": {
                "strategy_id": "reported_profit_breakout",
                "entry_path": entry_path,
                "hard_gate": True,
                "disclosure_required": True,
                "sort_metric": "pullback_liquidity_flow_v1",
                "sort_direction": "desc",
                "super_net_source": row.get("super_net_source"),
                "quote_at": row.get("quote_at"),
                "execution_allowed": True,
            },
            "news_check": {
                "status": "positive" if any(hit.get("tone", 0) > 0 for hit in hits) else (
                    "neutral_mention" if hits else "clean"
                ),
                "hits": len(hits),
            },
        }
        picks.append(pick)

    shadow_frame = table.loc[shadow_eligible].copy()
    shadow_frame["super_net_sort"] = _super_net_series(shadow_frame)
    shadow_frame["mom5_sort"] = pd.to_numeric(_table_column(shadow_frame, "mom5_raw"), errors="coerce")
    shadow_frame = shadow_frame.sort_values(
        ["super_net_sort", "mom5_sort"],
        ascending=[False, False],
        na_position="last",
    )
    shadow_picks = []
    for code, row in shadow_frame.head(topn).iterrows():
        shadow_picks.append({
            "code": code,
            "name": row.get("name"),
            "industry": row.get("industry"),
            "score": _number(row.get("super_net_sort"), 2),
            "super_net": _number(row.get("super_net_sort"), 2),
            "profit_source": str(row.get("profit_source") or "unknown"),
            "report_date": row.get("report_date"),
            "report_published_at": row.get("report_published_at"),
            "candidate_status": "shadow_disclosure",
            "execution_allowed": False,
            "metadata": {
                "strategy_id": "reported_profit_breakout",
                "hard_gate": False,
                "disclosure_required": True,
                "sort_metric": "pullback_liquidity_flow_v1",
            },
            "reason": "技术/路径条件具备，但已披露时间缺失或未证实；不进入正式候选",
        })

    strategy_metadata = dict(STRATEGIES["reported_profit_breakout"].get("metadata") or {})
    strategy_metadata.update({
        "strategy_id": "reported_profit_breakout",
        "candidate_count_before_news": int(eligible.sum()),
        "sort_metric": "super_net_raw",
        "sort_direction": "desc",
        "shadow_candidate_count": int(shadow_eligible.sum()),
    })
    return {
        "strategy": "reported_profit_breakout",
        "strategy_name": STRATEGIES["reported_profit_breakout"]["name"],
        "strategy_desc": STRATEGIES["reported_profit_breakout"]["desc"],
        "candidate_count": int(eligible.sum()),
        "count": len(picks),
        "picks": picks,
        "shadow_candidate_count": int(shadow_eligible.sum()),
        "shadow_picks": shadow_picks,
        "gate": gate,
        "first_board_candidates": None,
        "news_vetoed": news_vetoed,
        "news_scan": {
            "enabled": news_hits is not None,
            "total_hits": len(news_hits or []),
            "vetoed": len(news_vetoed),
        },
        "flow_source": (
            f"实时超大单资金 super_net_raw 降序（有效 {int(candidates['super_net_sort'].notna().sum())}/{len(candidates)}）"
            if len(candidates)
            else "实时超大单资金 super_net_raw 降序"
        ),
        "metadata": strategy_metadata,
    }


def _run_main_force_top10(table, topn=10, news_hits=None, gate=None):
    """Select a ten-name daily main-force watchlist without mega-cap bias."""
    permitted = _permitted_a_share_mask(table)
    col = lambda name: pd.to_numeric(_table_column(table, name), errors="coerce")
    amount, turnover, pct = col("amount"), col("turnover"), col("pct")
    main_pct, super_net, main_net = col("main_pct"), col("super_net_raw"), col("main_net")
    float_cap, mom20 = col("float_cap"), col("mom20_raw")
    heat = col("sector_heat_score").fillna(0.0)
    eligible = (
        permitted & (amount >= 2e8) & turnover.between(0.5, 18.0, inclusive="both")
        & pct.between(-2.0, 8.8, inclusive="both") & (main_pct >= 2.0)
        & (super_net > 0) & (main_net > 0) & (mom20 >= -0.08)
    )
    candidates = table.loc[eligible].copy()
    metadata = dict(STRATEGIES["main_force_top10"]["metadata"])
    if candidates.empty:
        return {"strategy": "main_force_top10", "strategy_name": "超强主力股",
                "strategy_desc": STRATEGIES["main_force_top10"]["desc"],
                "candidate_count": 0, "count": 0, "picks": [], "gate": gate,
                "news_vetoed": [], "flow_source": "实时主力资金复合评分",
                "metadata": metadata}

    rank = lambda values: pd.to_numeric(values, errors="coerce").rank(
        method="average", pct=True, na_option="bottom")
    idx = candidates.index
    flow_amount = (main_net / amount.where(amount > 0)).reindex(idx)
    flow_cap = (super_net / float_cap.where(float_cap > 0)).reindex(idx)
    candidates["main_force_score"] = (
        rank(main_pct.reindex(idx)) * 0.22 + rank(super_net.reindex(idx)) * 0.18
        + rank(flow_amount) * 0.22 + rank(flow_cap) * 0.15
        + rank(turnover.reindex(idx)) * 0.08 + rank(heat.reindex(idx)) * 0.15
    )
    candidates["main_force_intensity"] = flow_amount
    candidates = candidates.sort_values(
        ["main_force_score", "main_force_intensity", "super_net_raw"],
        ascending=[False, False, False], na_position="last")
    vetoed = {str(hit.get("code")) for hit in (news_hits or [])
              if isinstance(hit, dict) and hit.get("code") and (_number(hit.get("tone")) or 0) < 0}
    news_vetoed = [{"code": code, "name": candidates.loc[code].get("name"),
                    "reason": "命中负面公开事件，已从候选中剔除"}
                   for code in sorted(vetoed) if code in candidates.index]
    candidates = candidates.drop(index=[code for code in vetoed if code in candidates.index])
    picks = []
    for code, row in candidates.head(min(10, max(1, int(topn)))).iterrows():
        picks.append({
            "code": str(code), "name": row.get("name"), "industry": row.get("industry"),
            "price": _number(row.get("price"), 2), "pct": _number(row.get("pct"), 2),
            "score": _number(row.get("main_force_score"), 6),
            "main_pct": _number(row.get("main_pct"), 2),
            "main_net": _number(row.get("main_net"), 2),
            "super_net": _number(row.get("super_net_raw"), 2),
            "super_net_raw": _number(row.get("super_net_raw"), 2),
            "amount": _number(row.get("amount"), 2), "turnover": _number(row.get("turnover"), 2),
            "mom20": _percent(row.get("mom20_raw")),
            "sector_heat_score": _number(row.get("sector_heat_score"), 4),
            "candidate_status": "main_force_daily_top10", "entry_path": "main_force_confirmation",
            "reasons": [f"主力净流入占比 {_number(row.get('main_pct'), 2):+.2f}%",
                        "主力净额/成交额、超大单/流通市值与板块扩散复合排名",
                        "仅进入每日10只观察池，仍需盘中持续性和微观结构确认"],
            "metadata": {"strategy_id": "main_force_top10", "daily_candidate_limit": 10,
                         "position_limit": 3, "execution_allowed": True,
                         "requires_intraday_confirmation": True},
        })
    metadata.update({"candidate_count_before_news": int(eligible.sum()), "returned": len(picks)})
    return {"strategy": "main_force_top10", "strategy_name": "超强主力股",
            "strategy_desc": STRATEGIES["main_force_top10"]["desc"],
            "candidate_count": int(eligible.sum()), "count": len(picks), "picks": picks,
            "gate": gate, "first_board_candidates": None, "news_vetoed": news_vetoed,
            "news_scan": {"enabled": news_hits is not None, "total_hits": len(news_hits or []),
                          "vetoed": len(news_vetoed)},
            "flow_source": "主力占比、超大单、成交额/流通市值强度和板块扩散复合评分",
            "metadata": metadata}


def run_strategy(
    strategy_id,
    table: pd.DataFrame,
    topn=10,
    news_hits=None,
    gate=None,
    auto_news=True,
    klines=None,
    first_board_codes=None,
    weight_overrides=None,
    condition_overrides=None,
):
    """执行硬规则筛选，并按超大单净流入从高到低返回结果。"""
    if strategy_id in PAPER_WEIGHTS:
        return _run_paper_strategy(
            strategy_id,
            table,
            topn,
            gate,
            first_board_codes=first_board_codes,
            weight_overrides=weight_overrides,
            condition_overrides=condition_overrides,
        )
    if strategy_id == "reported_profit_breakout":
        return _run_reported_profit_breakout(
            table,
            topn=topn,
            news_hits=news_hits,
            gate=gate,
        )
    if strategy_id == "main_force_top10":
        return _run_main_force_top10(table, topn=min(10, topn), news_hits=news_hits, gate=gate)
    if strategy_id not in STRATEGIES:
        raise ValueError(f"未知策略: {strategy_id}")
    eligible = _eligible(strategy_id, table)
    # Keep a separate research-only lane for technically valid rows whose
    # financial disclosure timestamp is not proven.  These rows must never be
    # merged into ``picks`` or any execution request; they exist so the UI and
    # out-of-sample evaluator can distinguish "no technical setup" from
    # "setup exists but PIT evidence is incomplete".
    permitted = _permitted_a_share_mask(table)
    if strategy_id == "three_day":
        technical_eligible = (
            _bool_column(table, "three_up")
            & _bool_column(table, "boll_mid_breakout")
            & permitted
        )
    elif strategy_id == "five_day":
        technical_eligible = (
            _bool_column(table, "above_ma10_5d")
            & _bool_column(table, "above_boll_mid")
            & _bool_column(table, "above_ma60")
            & _bool_column(table, "monthly_oversold")
            & permitted
        )
    else:
        technical_eligible = (
            _bool_column(table, "above_ma10_5d")
            & _bool_column(table, "above_all_ma")
            & _bool_column(table, "monthly_oversold")
            & permitted
        )
    source = table.get("profit_source", pd.Series("unknown", index=table.index))
    shadow_source = source.fillna("unknown").astype(str).str.strip().str.lower().isin(
        {"unknown", "shadow", "future"}
    )
    shadow_eligible = technical_eligible & ~eligible & shadow_source
    candidates = table.loc[eligible].copy()
    candidates["super_net_sort"] = pd.to_numeric(
        candidates["super_net_raw"], errors="coerce"
    )
    candidates["score"] = candidates["super_net_sort"].rank(
        method="min", pct=True, na_option="bottom"
    )
    candidates["star_sector_bonus"] = _star_sector_bonus(table).reindex(candidates.index).fillna(0.0)
    candidates["score"] = candidates["score"] + candidates["star_sector_bonus"]
    candidates["core_stock_bonus"] = (_core_stock_preference(table).reindex(candidates.index).fillna(0.5) * 0.025)
    candidates["score"] = candidates["score"] + candidates["core_stock_bonus"]
    candidates = candidates.sort_values(
        ["score", "super_net_sort", "mom5_raw"],
        ascending=[False, False, False],
        na_position="last",
    )

    news_map = {}
    vetoed_codes = set()
    for hit in news_hits or []:
        news_map.setdefault(hit["code"], []).append(hit)
        if hit.get("tone", 0) < 0:
            vetoed_codes.add(hit["code"])
    news_vetoed = []
    for code in list(vetoed_codes):
        if code in candidates.index:
            row = candidates.loc[code]
            news_vetoed.append(
                {
                    "code": code,
                    "name": row["name"],
                    "reason": "命中负面公开事件，已从候选中剔除",
                }
            )
    candidates = candidates.drop(
        index=[code for code in vetoed_codes if code in candidates.index]
    )

    picks = []
    for code, row in candidates.head(topn).iterrows():
        hits = news_map.get(code, [])
        picks.append(
            {
                "code": code,
                "name": row["name"],
                "industry": row.get("industry"),
                "price": _number(row.get("price"), 2),
                "pct": _number(row.get("pct"), 2),
                "score": _number(row.get("score"), 3),
                "super_net": _number(row.get("super_net_raw"), 2),
                "pe": _number(row.get("pe"), 2),
                "pb": _number(row.get("pb"), 2),
                "roe": _number(row.get("roe"), 2),
                "profit_yoy": _number(row.get("profit_yoy"), 2),
                "net_profit": _number(row.get("net_profit"), 2),
                "annual_net_profit": _number(row.get("annual_net_profit"), 2),
                "annual_report_date": row.get("annual_report_date"),
                "report_date": row.get("report_date"),
                "mom5": _percent(row.get("mom5_raw")),
                "mom20": _percent(row.get("mom20_raw")),
                "mom60": _percent(row.get("mom60_raw")),
                "hot_rank": None,
                "star_sector_bonus": _number(row.get("star_sector_bonus"), 3),
                "core_stock_bonus": _number(row.get("core_stock_bonus"), 3),
                "reasons": _rule_reasons(strategy_id, row) + ([
                    f"科创板同业表现映射加分 {_number(row.get('star_sector_bonus'), 3):.3f}"
                ] if (_number(row.get("star_sector_bonus")) or 0) > 0 else []),
                "news_check": {
                    "status": "positive" if any(hit.get("tone", 0) > 0 for hit in hits) else (
                        "neutral_mention" if hits else "clean"
                    ),
                    "hits": len(hits),
                },
            }
        )
    shadow_frame = table.loc[shadow_eligible].copy()
    shadow_frame["super_net_sort"] = pd.to_numeric(
        shadow_frame["super_net_raw"], errors="coerce"
    )
    shadow_frame["star_sector_bonus"] = _star_sector_bonus(table).reindex(
        shadow_frame.index
    ).fillna(0.0)
    shadow_frame["shadow_score"] = shadow_frame["super_net_sort"].rank(
        method="min", pct=True, na_option="bottom"
    ) + shadow_frame["star_sector_bonus"]
    shadow_frame = shadow_frame.sort_values(
        ["shadow_score", "super_net_sort", "mom5_raw"],
        ascending=[False, False, False],
        na_position="last",
    )
    shadow_picks = []
    for code, row in shadow_frame.head(topn).iterrows():
        shadow_picks.append({
            "code": code,
            "name": row.get("name"),
            "industry": row.get("industry"),
            "price": _number(row.get("price"), 2),
            "pct": _number(row.get("pct"), 2),
            "score": _number(row.get("shadow_score"), 3),
            "super_net": _number(row.get("super_net_raw"), 2),
            "profit_source": str(row.get("profit_source") or "unknown"),
            "report_date": row.get("report_date"),
            "candidate_status": "shadow_disclosure",
            "execution_allowed": False,
            "reason": "技术条件满足，但财报披露时点未证实；仅影子验证，不进入正式买入",
        })
    return {
        "strategy": strategy_id,
        "strategy_name": STRATEGIES[strategy_id]["name"],
        "strategy_desc": STRATEGIES[strategy_id]["desc"],
        "candidate_count": int(eligible.sum()),
        "count": len(picks),
        "picks": picks,
        "shadow_candidate_count": int(shadow_eligible.sum()),
        "shadow_picks": shadow_picks,
        "gate": gate,
        "first_board_candidates": None,
        "news_vetoed": news_vetoed,
        "news_scan": {
            "enabled": news_hits is not None,
            "total_hits": len(news_hits or []),
            "vetoed": len(news_vetoed),
        },
        "flow_source": (
            f"超大单净流入排序（有效 {int(candidates['super_net_sort'].notna().sum())}/{len(candidates)}）"
            if len(candidates)
            else "超大单净流入排序"
        ),
    }


def _run_paper_strategy(strategy_id, table, topn, gate, first_board_codes=None, weight_overrides=None, condition_overrides=None):
    """原模拟盘候选排序，仅供模拟账户内部使用。"""
    weights = dict(PAPER_WEIGHTS[strategy_id])
    conditions = _paper_conditions(strategy_id, condition_overrides)
    enabled = conditions.get("enabled", {})
    if isinstance(weight_overrides, dict) and set(weight_overrides) == set(weights):
        weights = _bounded_weight_simplex(weight_overrides)
    score = pd.Series(0.0, index=table.index)
    for factor, weight in weights.items():
        # 行情快照来自 JSON/CSV 混合缓存时，数值列偶尔会以字符串形式
        # 进入 DataFrame。统一转数值，避免 5 分钟监控因字符串与数字比较
        # 异常退出并把本轮状态误显示成“异常”。
        values = pd.to_numeric(table[factor], errors="coerce").fillna(0.0)
        score += values * weight
    # Keep the unmodified factor score for research.  The execution model still
    # uses ``score`` below exactly as before; these extra columns are evidence
    # for the shadow-validation ledger and do not change ranking or orders.
    base_score = score.copy()
    def numeric_column(name):
        source = table[name] if name in table.columns else pd.Series(0.0, index=table.index)
        return pd.to_numeric(source, errors="coerce").fillna(0.0)

    # 科创板本身永远不进入可交易候选；这里只把同产业科创板的实时
    # 共振作为有上限的上下文加分，参与主板/创业板候选排序。
    star_sector_bonus = numeric_column("star_sector_bonus")
    score += star_sector_bonus
    # 传统核心股偏好只作很小的软排序项：不设“名气白名单”，也不
    # 放宽任何硬条件。流通市值和成交额缺失时取中性值，避免数据缺失
    # 把候选静默打入末尾。
    core_stock_bonus = _core_stock_preference(table) * 0.025
    score += core_stock_bonus

    # 热门股启动段是独立的软排序层。它不改变任何证券权限、行情、
    # 财务或风险硬门禁，只让第一/第二个强势日的“资金+量能+板块”
    # 共振先进入观察池；连续加速或远离均线的股票会被反向降权。
    hot_profile = _hot_leader_profile(table)
    hot_cap = {
        "one_to_two": 0.24,
        "sentiment_pioneer": 0.24,
        "trend_continuation": 0.16,
        "bottom_reversal": 0.08,
    }.get(strategy_id, 0.10)
    hot_bonus = hot_profile["score"] * hot_cap
    hot_bonus = hot_bonus.mask(hot_profile["stage"].eq("late_overheat"), -0.06)
    # 修复3（2026-08-28）：市场黄/红灯不开新的动量追逐单——gate.light 此前
    # 已由 paper 侧传入但从未被策略层使用。risk_off 时关闭热门股启动加分。
    market_light = _gate_market_light(gate)
    risk_off = market_light in {"yellow", "red"}
    if risk_off:
        hot_bonus = hot_bonus * 0.0
    score += hot_bonus
    bottom_profile = _bottom_reversal_profile(table)
    bottom_bonus = bottom_profile["score"] * (0.30 if strategy_id == "bottom_reversal" else 0.0)
    score += bottom_bonus

    # 修复1（2026-08-28）：动量过热惩罚——mom5>5% 或 mom20>15% 的候选
    # 排序分直接扣减；黄/红灯下加倍（修复3）。
    score = _apply_overheat_penalty(score, table, OVERHEAT_RANK_PENALTY * (2.0 if risk_off else 1.0))
    if strategy_id == "sentiment_pioneer":
        # 修复2（2026-08-28）：板块情绪高潮禁入 + 早期轮动优先。板块热度
        # 用行业内平均 5 日动量度量——高潮板块（>8%）整体排除，不再在
        # 情绪顶点接力；sector_early_rotation_score（早期启动分）升为排序加分。
        mom5_raw = numeric_column("mom5_raw")
        if "industry" in table.columns:
            sector_mom5 = mom5_raw.groupby(table["industry"].astype(str)).transform("mean")
            climax = (sector_mom5 > SECTOR_CLIMAX_MOM5).reindex(table.index).fillna(False)
        else:
            climax = pd.Series(False, index=table.index)
        onset = numeric_column("sector_early_rotation_score")
        onset_bonus = onset.rank(method="average", pct=True, na_option="bottom") * 0.30
        score = score + onset_bonus
        score = score.mask(climax, -990.0)

    pct = numeric_column("pct")
    vol_surge = numeric_column("vol_surge_raw")
    flow = numeric_column("flow")
    mom20 = numeric_column("mom20_raw")
    sentiment = numeric_column("sentiment")

    if strategy_id == "one_to_two":
        # 日内短线不能被“昨日首板”锁死：首板只代表已验证的强势基因，
        # 而非当天唯一可交易来源。候选池同时保留量价、资金和动量都靠前的
        # 强势股；最终仍由盘中实时成交区间、量比和资金方向决定是否下单。
        is_first_board = (
            score.index.to_series().isin(first_board_codes)
            if first_board_codes is not None
            else pd.Series(False, index=score.index)
        )
        if enabled.get("first_board_bonus", True):
            score += is_first_board.astype(float) * conditions["first_board_bonus"]
        if enabled.get("pct_band", True):
            score += pct.between(conditions["pct_low"], conditions["pct_high"], inclusive="left").astype(float) * 0.28
            score += pct.between(conditions["chase_low"], conditions["chase_high"], inclusive="left").astype(float) * 0.10
        if enabled.get("chase_guard", True):
            score -= pct.ge(conditions["chase_guard_pct"]).astype(float) * conditions["chase_penalty"]
        if enabled.get("weak_guard", True):
            score -= pct.lt(conditions["weak_guard_pct"]).astype(float) * conditions["weak_penalty"]
    elif strategy_id == "bottom_reversal":
        if enabled.get("low_volume_guard", True):
            score -= vol_surge.lt(conditions["vol_surge_min"]).astype(float) * conditions["low_volume_penalty"]
        if enabled.get("flow_confirm", True):
            score += flow.gt(conditions["flow_min"]).astype(float) * conditions["flow_bonus"]
        if enabled.get("momentum_guard", True):
            score -= mom20.lt(conditions["mom20_min"]).astype(float) * conditions["momentum_penalty"]
    elif strategy_id == "trend_continuation":
        # 结构进化模型：对明显破位/抄底型标的降权，对均线和动量共振标的加分。
        mom = numeric_column("mom20_raw")
        flow = numeric_column("flow")
        ma20 = numeric_column("ma20")
        ma60 = numeric_column("ma60")
        price = numeric_column("price")
        # P3 精读修复：ma20_ma60_min 进化参数此前是死配置——实际用硬编码
        # ma20 > ma60，进化调整该值不生效。现在按白名单参数控制间隔余量。
        _ma20_ma60_min = _number_or(conditions.get("ma20_ma60_min"), 0.0)
        structure = ((ma20 / ma60 - 1.0) * 100 >= _ma20_ma60_min) & (price >= ma20 * (1 + conditions["close_ma20_min"] / 100.0))
        if enabled.get("trend_structure_guard", True):
            score += structure.astype(float) * 0.22
            score -= (~structure).astype(float) * conditions["broken_structure_penalty"]
        if enabled.get("momentum_guard", True):
            score += mom.ge(conditions["mom20_min"]).astype(float) * 0.14
        if enabled.get("flow_confirm", True):
            score += flow.ge(conditions["flow_min"]).astype(float) * 0.12
        if enabled.get("breakout_bonus", True):
            score += (structure & mom.ge(conditions["mom20_min"])).astype(float) * conditions["breakout_bonus"]
    else:
        if enabled.get("sentiment_guard", True):
            score -= sentiment.lt(conditions["sentiment_min"]).astype(float) * conditions["sentiment_penalty"]
        if enabled.get("individual_strong", True):
            individual = (
                pct.between(conditions["individual_pct_min"], conditions["individual_pct_max"], inclusive="both")
                & flow.ge(conditions["individual_flow_min"])
                & vol_surge.ge(conditions["individual_vol_surge_min"])
                & numeric_column("mom5_raw").ge(conditions["individual_mom5_min"])
            )
            score += individual.astype(float) * conditions["individual_bonus"]

    ranked = table.copy()
    ranked["score_base"] = base_score
    ranked["score_star_sector_bonus"] = star_sector_bonus
    ranked["score_core_stock_bonus"] = core_stock_bonus
    ranked["score_hot_leader_bonus"] = hot_bonus
    ranked["score_bottom_reversal_bonus"] = bottom_bonus
    ranked["bottom_reversal_score"] = bottom_profile["score"]
    ranked["bottom_reversal_structure"] = bottom_profile["structure"]
    ranked["bottom_reversal_overheat"] = bottom_profile["overheat"]
    ranked["hot_leader_score"] = hot_profile["score"]
    ranked["hot_leader_overheat"] = hot_profile["overheat"]
    ranked["hot_leader_stage"] = hot_profile["stage"]
    ranked["hot_leader_sector_onset_score"] = hot_profile["sector_onset_score"]
    ranked["score_strategy_adjustment"] = score - base_score - star_sector_bonus - core_stock_bonus - hot_bonus - bottom_bonus
    ranked["score"] = score
    ranked = ranked[ranked["score"] > -990].sort_values("score", ascending=False)
    picks = []
    for code, row in ranked.head(topn).iterrows():
        factor_snapshot = {
            key: _number(row.get(key), 6)
            for key in (
                "mom_short", "mom", "flow", "volsurge", "sentiment", "value",
                "quality", "rsi", "mom5_raw", "mom20_raw", "mom60_raw",
                "vol_surge_raw", "rsi14_raw", "pct", "price", "amount",
                "turnover", "main_pct", "super_net", "pe", "pb", "roe",
                "profit_yoy", "net_profit", "annual_net_profit",
                "sector_heat_score", "sector_early_rotation_score",
            )
            if key in ranked.columns
        }
        picks.append(
            {
                "code": code,
                "name": row.get("name"),
                "industry": row.get("industry"),
                "price": _number(row.get("price"), 2),
                "pct": _number(row.get("pct"), 2),
                "score": _number(row.get("score"), 3),
                "pe": _number(row.get("pe"), 2),
                "pb": _number(row.get("pb"), 2),
                "roe": _number(row.get("roe"), 2),
                "profit_yoy": _number(row.get("profit_yoy"), 2),
                "report_date": row.get("report_date"),
                "mom5": _percent(row.get("mom5_raw")),
                "mom20": _percent(row.get("mom20_raw")),
                "mom60": _percent(row.get("mom60_raw")),
                "hot_rank": _number(row.get("hot_rank")),
                "score_components": {
                    "version": "paper-score-evidence-v1",
                    "weights": {key: round(value, 6) for key, value in weights.items()},
                    "base_score": _number(row.get("score_base"), 6),
                    "star_sector_bonus": _number(row.get("score_star_sector_bonus"), 6),
                    "core_stock_bonus": _number(row.get("score_core_stock_bonus"), 6),
                    "hot_leader_bonus": _number(row.get("score_hot_leader_bonus"), 6),
                    "hot_leader_score": _number(row.get("hot_leader_score"), 6),
                    "hot_leader_overheat": _number(row.get("hot_leader_overheat"), 6),
                    "hot_leader_stage": row.get("hot_leader_stage") or "normal",
                    "sector_early_rotation": bool(row.get("sector_early_rotation", False)),
                    "sector_early_rotation_score": _number(row.get("hot_leader_sector_onset_score"), 6),
                    "bottom_reversal_bonus": _number(row.get("score_bottom_reversal_bonus"), 6),
                    "bottom_reversal_score": _number(row.get("bottom_reversal_score"), 6),
                    "bottom_reversal_structure": row.get("bottom_reversal_structure") or "unknown",
                    "bottom_reversal_overheat": _number(row.get("bottom_reversal_overheat"), 6),
                    "strategy_adjustment": _number(row.get("score_strategy_adjustment"), 6),
                    "context_score": _number(
                        _number_or(row.get("score_star_sector_bonus"))
                        + _number_or(row.get("score_core_stock_bonus"))
                        + _number_or(row.get("score_hot_leader_bonus"))
                        + _number_or(row.get("score_bottom_reversal_bonus"))
                        + _number_or(row.get("score_strategy_adjustment")),
                        6,
                    ),
                    "final_score": _number(row.get("score"), 6),
                },
                "factor_snapshot": factor_snapshot,
                "hot_leader": {
                    "stage": row.get("hot_leader_stage") or "normal",
                    "score": _number(row.get("hot_leader_score"), 6),
                    "overheat": _number(row.get("hot_leader_overheat"), 6),
                    "sector_early_rotation": bool(row.get("sector_early_rotation", False)),
                    "sector_early_rotation_score": _number(row.get("hot_leader_sector_onset_score"), 6),
                    "observation_only": True,
                },
                "bottom_reversal": {
                    "structure": row.get("bottom_reversal_structure") or "unknown",
                    "score": _number(row.get("bottom_reversal_score"), 6),
                    "overheat": _number(row.get("bottom_reversal_overheat"), 6),
                    "observation_only": strategy_id != "bottom_reversal",
                },
                "entry_path": "individual_strong" if (
                    strategy_id == "sentiment_pioneer" and enabled.get("individual_strong", True)
                    and _number_or(row.get("pct"), -999.0) >= conditions.get("individual_pct_min", 3.5)
                    and _number_or(row.get("flow"), -999.0) >= conditions.get("individual_flow_min", 0.65)
                    and _number_or(row.get("vol_surge_raw"), -999.0) >= conditions.get("individual_vol_surge_min", 1.2)
                    and _number_or(row.get("mom5_raw"), -999.0) >= conditions.get("individual_mom5_min", 2.0)
                ) else "sector_heat",
                "reasons": [f"模拟盘内部候选分 {float(row['score']):.3f}"],
                "news_check": {"status": "clean", "hits": 0},
            }
        )
    # Keep a small second lane for fresh leaders that are not yet strong
    # enough to win the legacy score top-N.  The paper executor still applies
    # the full quote/Q/T+1/capital/risk gates; this lane only prevents a first
    # or second acceleration day from disappearing before confirmation.
    selected_codes = {str(item.get("code")) for item in picks}
    hot_watch = []
    watch_frame = ranked[
        ranked["hot_leader_stage"].eq("early_acceleration")
        & ~ranked.index.to_series().astype(str).isin(selected_codes)
    ].sort_values(["hot_leader_score", "score"], ascending=False).head(max(6, min(12, topn // 3)))
    for code, row in watch_frame.iterrows():
        hot_watch.append({
            "code": code,
            "name": row.get("name"),
            "industry": row.get("industry"),
            "price": _number(row.get("price"), 2),
            "pct": _number(row.get("pct"), 2),
            "score": _number(row.get("score"), 3),
            "mom5": _percent(row.get("mom5_raw")),
            "mom20": _percent(row.get("mom20_raw")),
            "mom60": _percent(row.get("mom60_raw")),
            "hot_rank": _number(row.get("hot_rank")),
            "hot_leader": {
                "stage": "early_acceleration",
                "score": _number(row.get("hot_leader_score"), 6),
                "overheat": _number(row.get("hot_leader_overheat"), 6),
                "observation_only": False,
            },
            "candidate_status": "hot_leader_watch",
            "entry_path": "hot_leader_onset",
            "reasons": ["热门启动段：当日涨幅、实时资金、成交活跃度与板块强度同步"] ,
            "news_check": {"status": "clean", "hits": 0},
        })
    names = {
        "one_to_two": "强势日内候选（首板优先）",
        "bottom_reversal": "底部启动",
        "trend_continuation": "趋势延续",
        "sentiment_pioneer": "情绪先锋",
    }
    return {
        "strategy": strategy_id,
        "strategy_name": names.get(strategy_id, strategy_id),
        "count": len(picks),
        "picks": picks,
        "hot_leader_watch": hot_watch,
        "hot_leader_watch_count": len(hot_watch),
        "gate": gate,
        "first_board_candidates": len(first_board_codes) if first_board_codes is not None else None,
        "news_vetoed": [],
        "news_scan": {"enabled": False, "total_hits": 0, "vetoed": 0},
        "flow_source": table["flow_source"].iloc[0] if len(table) else None,
        "weights_used": {key: round(value, 6) for key, value in weights.items()},
        "conditions_used": conditions,
        "hot_leader_context": {
            "version": "hot-leader-onset-v1",
            "caps": {key: value for key, value in {
                "one_to_two": 0.24, "sentiment_pioneer": 0.24,
                "trend_continuation": 0.16, "bottom_reversal": 0.08,
            }.items()},
            "stage_counts": {
                str(key): int(value)
                for key, value in hot_profile["stage"].value_counts().to_dict().items()
            },
            "early_acceleration_count": int(hot_profile["stage"].eq("early_acceleration").sum()),
            "late_overheat_count": int(hot_profile["stage"].eq("late_overheat").sum()),
            "hard_gates_unchanged": True,
        },
        "bottom_reversal_context": {
            "version": "bottom-reversal-structure-v1",
            "enabled": strategy_id == "bottom_reversal",
            "structure_counts": {
                str(key): int(value)
                for key, value in bottom_profile["structure"].value_counts().to_dict().items()
            },
            "hard_gates_unchanged": True,
        },
    }


def _number(value, digits=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return round(number, digits) if digits is not None else number


def _number_or(value, default=0.0):
    """Return a comparable numeric value without turning missing data into an exception."""
    number = _number(value)
    return default if number is None else number


def _percent(value):
    number = _number(value)
    return round(number * 100, 2) if number is not None else None
