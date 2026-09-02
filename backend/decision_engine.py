# -*- coding: utf-8 -*-
"""
买入执行 / 卖出决策 风控模型
参考：
- 买入执行：T1-T5 五级时机 · 封单真伪六维鉴别 · 开仓前置六维核查 · 买入观察清单
- 卖出决策：8 档阶梯止盈 · 4 类强制止损 · 次日竞价 Q1-Q5 决策矩阵

所有决策基于可免费获取的公开数据代理实现：日线、实时快照、板块资金流、
7×24 快讯、人气榜、海外指数。不构成投资建议。
"""
import os, sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_fetcher as dfc
import factors as F


def _safe(v):
    return v if isinstance(v, (int, float)) and not pd.isna(v) else 0.0


def _normalize_news_hits(news_hits, code):
    """归一化 news_hits：兼容 list[dict]（来自 news_keyword_scan）和 dict 两种格式。
    返回 {pos:[], neg:[]}"""
    pos, neg = [], []
    if not news_hits:
        return {"pos": pos, "neg": neg}
    # dict 格式: {code: {pos:[], neg:[]}}
    if isinstance(news_hits, dict):
        hit = news_hits.get(code, {})
        return {"pos": hit.get("pos", []), "neg": hit.get("neg", [])}
    # list[dict] 格式: [{code, name, tone, keywords, summary, time, source}, ...]
    for h in news_hits:
        if h.get("code") != code:
            continue
        if h.get("tone", 0) > 0:
            pos.append(h)
        elif h.get("tone", 0) < 0:
            neg.append(h)
    return {"pos": pos, "neg": neg}


# ---------- 工具：六维健康度 ----------
def _dim_status(score):
    if score >= 0.6:
        return "green"
    if score >= 0.35:
        return "yellow"
    return "red"


# ---------- 一、买入执行 ----------

def pre_open_six_dim(code, name=None, kline=None, snap=None, sector_flow=None,
                     overseas_gate=None, news_hits=None):
    """
    开仓前置六维核查，返回每个维度的 status(green/yellow/red)、score(0-1)、reason。
    """
    name = name or code
    if snap is None:
        snap_rows = dfc.fetch_realtime_for_codes([code])
        snap = snap_rows[0] if snap_rows else {}
    if kline is None:
        kline = dfc.load_cached_kline(code)
    if sector_flow is None:
        sector_flow = dfc.fetch_sector_flow("industry")
    if overseas_gate is None:
        try:
            overseas_gate = F.overseas_risk_gate()
        except Exception:
            overseas_gate = {"light": "unknown"}

    pct = _safe(snap.get("pct"))
    limit_pct = F.limit_up_threshold(code) * 100
    main_pct = _safe(snap.get("main_pct"))
    turnover = _safe(snap.get("turnover"))
    pe = snap.get("pe")
    pb = snap.get("pb")
    vol_ratio = _safe(snap.get("vol_ratio"))

    dims = []

    # 1. 大盘维度
    gate_light = overseas_gate.get("light", "unknown") if overseas_gate else "unknown"
    if gate_light == "green":
        dims.append({"name": "大盘环境", "status": "green", "score": 0.85,
                     "reason": "海外风险绿灯，大环境友好"})
    elif gate_light == "yellow":
        dims.append({"name": "大盘环境", "status": "yellow", "score": 0.5,
                     "reason": "海外风险黄灯，保持谨慎"})
    else:
        dims.append({"name": "大盘环境", "status": "red", "score": 0.15,
                     "reason": "海外风险红灯或未知，宜控仓观望"})

    # 2. 板块维度
    ind = snap.get("industry") or "未知"
    sf = next((s for s in (sector_flow or []) if s.get("name") == ind), None)
    if sf:
        s_pct = _safe(sf.get("pct"))
        s_main = _safe(sf.get("main_pct"))
        if s_pct > 0 and s_main > 0:
            dims.append({"name": "所属板块", "status": "green", "score": 0.8,
                         "reason": f"{ind} 上涨 +{s_pct}%，主力净流入占比 {s_main}%"})
        elif s_pct > 0:
            dims.append({"name": "所属板块", "status": "yellow", "score": 0.55,
                         "reason": f"{ind} 上涨但资金一般"})
        else:
            dims.append({"name": "所属板块", "status": "red", "score": 0.25,
                         "reason": f"{ind} 下跌或资金流出"})
    else:
        dims.append({"name": "所属板块", "status": "yellow", "score": 0.45,
                     "reason": f"未找到 {ind} 板块资金流"})

    # 3. 个股质量维度
    quality_score = 0.5
    reasons = []
    if isinstance(pe, (int, float)) and 0 < pe < 50:
        quality_score += 0.15; reasons.append("PE合理")
    if isinstance(pb, (int, float)) and 0 < pb < 6:
        quality_score += 0.1; reasons.append("PB不高")
    if vol_ratio and vol_ratio > 1:
        quality_score += 0.1; reasons.append("放量")
    if turnover and 0.5 < turnover < 20:
        quality_score += 0.1; reasons.append("换手适中")
    if pct >= limit_pct:
        quality_score -= 0.25; reasons.append("已涨停，买入条件苛刻")
    if pct <= -limit_pct:
        quality_score -= 0.3; reasons.append("已跌停，不可买入")
    quality_score = min(max(quality_score, 0.05), 0.95)
    dims.append({"name": "个股质量", "status": _dim_status(quality_score),
                 "score": round(quality_score, 2),
                 "reason": "；".join(reasons) if reasons else "无明显亮点"})

    # 4. 资金维度
    if main_pct > 5:
        dims.append({"name": "主力资金", "status": "green", "score": 0.85,
                     "reason": f"主力净流入占比 {main_pct}%"})
    elif main_pct > 0:
        dims.append({"name": "主力资金", "status": "yellow", "score": 0.55,
                     "reason": f"主力小幅流入 {main_pct}%"})
    else:
        dims.append({"name": "主力资金", "status": "red", "score": 0.2,
                     "reason": f"主力净流出或占比 {main_pct}%"})

    # 5. 情绪维度
    sent_score = 0.5
    sent_reasons = []
    # news_hits 可能为 list[dict]（来自 news_keyword_scan）或 dict；统一规范化为 {code: {neg:[], pos:[]}}
    nh = _normalize_news_hits(news_hits, code)
    if nh.get("neg"):
        sent_score -= 0.3; sent_reasons.append(f"负面舆情 {len(nh['neg'])} 条")
    if nh.get("pos"):
        sent_score += 0.15; sent_reasons.append(f"正面舆情 {len(nh['pos'])} 条")
    # 人气榜：排名靠前但非极高视为情绪积极
    hot = snap.get("hot_rank")
    if isinstance(hot, int):
        if hot <= 20:
            sent_score += 0.1; sent_reasons.append(f"人气第 {hot}")
        elif hot <= 100:
            sent_score += 0.05
    sent_score = min(max(sent_score, 0.1), 0.95)
    dims.append({"name": "市场情绪", "status": _dim_status(sent_score),
                 "score": round(sent_score, 2),
                 "reason": "；".join(sent_reasons) if sent_reasons else "情绪中性"})

    # 6. 技术/趋势维度
    tech_score = 0.45
    tech_reasons = []
    if kline is not None and len(kline) > 21:
        c = kline["close"]
        mom5 = (c.iloc[-1] / c.iloc[-6] - 1) * 100 if len(c) >= 6 else None
        mom20 = (c.iloc[-1] / c.iloc[-21] - 1) * 100
        if mom5 and mom5 > 0:
            tech_score += 0.15; tech_reasons.append(f"5日动量 +{mom5:.1f}%")
        if mom20 > 0:
            tech_score += 0.15; tech_reasons.append(f"20日动量 +{mom20:.1f}%")
        else:
            tech_score -= 0.1; tech_reasons.append(f"20日动量 {mom20:.1f}%")
        # 站在 20 日均线上方
        ma20 = c.tail(20).mean()
        if c.iloc[-1] > ma20:
            tech_score += 0.15; tech_reasons.append("站上20日均线")
        else:
            tech_score -= 0.1; tech_reasons.append("跌破20日均线")
    if pct > 0:
        tech_score += 0.05
    tech_score = min(max(tech_score, 0.1), 0.95)
    dims.append({"name": "技术趋势", "status": _dim_status(tech_score),
                 "score": round(tech_score, 2),
                 "reason": "；".join(tech_reasons) if tech_reasons else "技术信号中性"})

    return dims


def limit_up_seal_check(code, snap=None, kline=None):
    """
    封单真伪六维鉴别。
    免费数据没有 level2 封单，用涨停日量价 + 资金 + 板块共振做代理。
    返回 status(green/yellow/red)、score、details。
    非涨停股返回 None（不适用）。
    """
    if snap is None:
        rows = dfc.fetch_realtime_for_codes([code])
        snap = rows[0] if rows else {}
    pct = _safe(snap.get("pct"))
    if pct < F.limit_up_threshold(code) * 100:
        return None
    if kline is None:
        kline = dfc.load_cached_kline(code)

    details = []
    score = 0.5

    # 1. 换手率适中（<15% 说明筹码锁定较好，>30% 容易炸板）
    turnover = _safe(snap.get("turnover"))
    if 1 < turnover < 15:
        score += 0.1; details.append("换手适中(锁定好)")
    elif turnover > 30:
        score -= 0.15; details.append("换手过高(炸板风险)")

    # 2. 量能：今日量是20日均量1.5倍以上但不过度异常
    if kline is not None and len(kline) >= 21:
        vol_today = snap.get("volume") or 0
        avg_vol = kline["volume"].tail(20).mean() or 1
        ratio = vol_today / avg_vol if avg_vol else 0
        if 1.5 <= ratio <= 8:
            score += 0.1; details.append("量能配合涨停")
        elif ratio > 15:
            score -= 0.1; details.append("量能异常放大")
        else:
            details.append("量能一般")

    # 3. 主力资金净流入占比
    main_pct = _safe(snap.get("main_pct"))
    if main_pct > 10:
        score += 0.15; details.append("主力强封")
    elif main_pct > 0:
        score += 0.05; details.append("主力小幅流入")
    else:
        score -= 0.2; details.append("主力流出(假板概率高)")

    # 4. 流通市值（小票容易被操纵，大票封单更真）
    float_cap = snap.get("float_cap") or 0
    if float_cap > 1e10:
        score += 0.1; details.append("流通市值较大")
    elif float_cap < 2e9:
        score -= 0.1; details.append("小盘股，封单可信度低")

    # 5. 是否一字板（开盘即涨停且未打开）
    # 日期对齐守卫：K 线缓存最后一根可能是昨日/更早（行情补齐滞后），
    # 用旧 open 对比实时 close 会伪造“一字板”加分。
    open_p = None
    if kline is not None and len(kline) >= 1:
        try:
            kline_day = str(kline.index[-1])[:10]
            snap_day = str(snap.get("quote_at") or snap.get("time") or "")[:10]
            if not snap_day or kline_day == snap_day:
                open_p = kline["open"].iloc[-1]
        except (TypeError, ValueError, IndexError):
            open_p = None
    close_p = snap.get("price")
    if open_p and close_p and abs(open_p - close_p) / close_p * 100 < 0.5:
        score += 0.1; details.append("一字板，封单坚决")

    # 6. 板块共振：所在行业今日涨幅
    ind = snap.get("industry")
    if ind:
        sf = next((s for s in dfc.fetch_sector_flow("industry") if s.get("name") == ind), None)
        if sf and _safe(sf.get("pct")) > 0:
            score += 0.05; details.append("板块共振上涨")

    score = min(max(score, 0.05), 0.95)
    status = _dim_status(score)
    return {"status": status, "score": round(score, 2), "details": details,
            "verdict": ("封单可信" if status == "green" else
                        ("谨慎追高" if status == "yellow" else "封单存疑，不建议追"))}


def buy_timing_tier(dims, seal=None):
    """
    T1-T5 五级时机：
    T1 积极买入，T2 可执行买入，T3 观察/轻仓，T4 放弃或仅观察，T5 禁止买入。
    """
    green = sum(1 for d in dims if d["status"] == "green")
    red = sum(1 for d in dims if d["status"] == "red")
    avg_score = sum(d["score"] for d in dims) / len(dims) if dims else 0

    if seal and seal["status"] == "red":
        return {"tier": "T5", "action": "放弃", "reason": "涨停封单存疑"}
    if green >= 4 and red == 0 and avg_score >= 0.65:
        return {"tier": "T1", "action": "积极买入", "reason": "六维 mostly 绿灯，大盘环境友好"}
    if green >= 3 and red <= 1 and avg_score >= 0.5:
        return {"tier": "T2", "action": "可执行买入", "reason": "多数维度健康，可开仓"}
    if red <= 2 and avg_score >= 0.4:
        return {"tier": "T3", "action": "观察/轻仓", "reason": "信号混杂，建议观察清单跟踪"}
    if red >= 3:
        return {"tier": "T4", "action": "放弃", "reason": "多个维度红灯"}
    return {"tier": "T5", "action": "禁止买入", "reason": "综合评分过低或涨停封单不可信"}


def buy_decision(code, name=None, kline=None, snap=None, sector_flow=None,
                 overseas_gate=None, news_hits=None):
    """
    完整买入执行决策：六维核查 + 涨停封单鉴别 + T1-T5 时机 + 观察清单判定。
    """
    dims = pre_open_six_dim(code, name=name, kline=kline, snap=snap,
                            sector_flow=sector_flow, overseas_gate=overseas_gate,
                            news_hits=news_hits)
    # 调用方已批量取得快照时直接复用，避免每只候选再次请求行情接口。
    if snap is None:
        snap_rows = dfc.fetch_realtime_for_codes([code])
        snap = snap_rows[0] if snap_rows else {}
    seal = limit_up_seal_check(code, snap=snap, kline=kline)
    tier = buy_timing_tier(dims, seal=seal)
    hard_vetoes = []
    pct = _safe(snap.get("pct"))
    main_pct = _safe(snap.get("main_pct"))
    news = _normalize_news_hits(news_hits, code)
    if pct <= -5:
        hard_vetoes.append(f"当日大跌 {pct:.2f}%")
    if pct <= -F.limit_up_threshold(code) * 100:
        hard_vetoes.append("处于或接近跌停，无法执行")
    if main_pct <= -10:
        hard_vetoes.append(f"主力净流出占比 {main_pct:.2f}%")
    if news.get("neg"):
        hard_vetoes.append(f"负面舆情 {len(news['neg'])} 条")
    if overseas_gate and overseas_gate.get("light") == "red":
        hard_vetoes.append("海外风险红灯")
    if hard_vetoes:
        tier = {"tier": "T5", "action": "禁止买入", "reason": "；".join(hard_vetoes)}
    avg_score = round(sum(d["score"] for d in dims) / len(dims), 2) if dims else 0

    executable = tier["tier"] in ("T1", "T2")
    watchlist = tier["tier"] == "T3"

    return {
        "code": code, "name": name or snap.get("name") or code,
        "avg_score": avg_score,
        "six_dim": dims,
        "seal_check": seal,
        "tier": tier["tier"],
        "action": tier["action"],
        "executable": executable,
        "watchlist": watchlist,
        "hard_vetoes": hard_vetoes,
        "summary": f"{tier['tier']}｜{tier['action']}｜综合评分 {avg_score}",
    }


# ---------- 二、卖出决策 ----------

def ladder_take_profit(ret_pct):
    """
    8 档阶梯止盈：到达对应浮盈档位建议减仓比例。
    返回 [(threshold, take_pct, keep_stop)]，keep_stop 为移动止盈位（相对成本 %）。
    """
    tiers = [
        (5, 10, None),     # +5%  减仓 10%
        (10, 15, 0),       # +10% 减仓 15%，保本
        (15, 20, 3),       # +15% 减仓 20%，保 3% 利润
        (20, 25, 5),       # +20% 减仓 25%，保 5% 利润
        (30, 30, 10),      # +30% 减仓 30%，保 10% 利润
        (50, 40, 20),      # +50% 减仓 40%，保 20% 利润
        (80, 50, 40),      # +80% 减仓 50%，保 40% 利润
        (100, 60, 50),     # +100% 减仓 60%，保 50% 利润
    ]
    active = []
    for thr, take, stop in tiers:
        if ret_pct >= thr:
            active.append({"threshold": thr, "take_pct": take,
                           "protect_profit": stop,
                           "msg": f"浮盈 ≥{thr}% 已触发，建议减仓 {take}%，移动止盈保 {stop}%" if stop is not None
                                   else f"浮盈 ≥{thr}% 已触发，建议减仓 {take}%"})
    # 返回最高一档
    return active[-1] if active else None


def forced_stop_losses(price, cost, peak_price, hold_days, mom20, main_pct,
                       news_neg=None, overseas_light=None):
    """
    4 类强制止损：价格、回撤、时间、因子/黑天鹅。
    返回所有触发的信号列表。
    """
    signals = []
    ret_pct = (price / cost - 1) * 100 if (price and cost) else None
    dd = (1 - price / peak_price) * 100 if (price and peak_price) else None

    # 1. 价格止损
    if ret_pct is not None and ret_pct <= -8:
        signals.append({"type": "价格止损", "level": "sell",
                        "msg": f"较成本下跌 {abs(ret_pct):.1f}% ≥ 8%"})

    # 2. 回撤止损
    if dd is not None and dd >= 10:
        signals.append({"type": "回撤止损", "level": "sell",
                        "msg": f"自峰值回撤 {dd:.1f}% ≥ 10%"})

    # 3. 时间止损
    if hold_days is not None and hold_days > 20 and (ret_pct is None or ret_pct < 5):
        signals.append({"type": "时间止损", "level": "warn",
                        "msg": f"持有 {hold_days} 个交易日，收益不足 5%，机会成本过高"})

    # 4. 因子/黑天鹅止损
    deterioration = []
    if mom20 is not None and mom20 < 0:
        deterioration.append(f"20日动量转负({mom20:.1f}%)")
    if isinstance(main_pct, (int, float)) and main_pct < -5:
        deterioration.append(f"主力净流出占比 {main_pct:.1f}%")
    if news_neg:
        deterioration.append(f"负面舆情 {len(news_neg)} 条")
    # 单一技术信号不再强制卖出：负面舆情，或动量+资金同时恶化才升级为卖出。
    if news_neg or (
        mom20 is not None
        and mom20 < 0
        and isinstance(main_pct, (int, float))
        and main_pct < -5
    ):
        signals.append({"type": "因子/黑天鹅止损", "level": "sell",
                        "msg": "；".join(deterioration)})
    elif deterioration:
        signals.append({"type": "因子恶化", "level": "warn",
                        "msg": "；".join(deterioration)})
    if overseas_light == "red":
        signals.append({"type": "海外风险", "level": "warn",
                        "msg": "海外风险红灯，建议降低组合仓位而非单票无条件卖出"})

    return signals


def next_day_auction_matrix(code, name=None, kline=None, snap=None,
                            overseas_gate=None, news_hits=None):
    """
    次日竞价 Q1-Q5 决策矩阵。
    综合隔夜海外、板块、个股舆情、技术动量给出开盘动作建议。
    Q1 偏强持有/加仓；Q5 偏强减仓/竞价出。
    """
    if snap is None:
        rows = dfc.fetch_realtime_for_codes([code])
        snap = rows[0] if rows else {}
    if kline is None:
        kline = dfc.load_cached_kline(code)
    if overseas_gate is None:
        try:
            overseas_gate = F.overseas_risk_gate()
        except Exception:
            overseas_gate = {"light": "unknown"}

    score = 0.5
    reasons = []

    # 海外隔夜
    light = overseas_gate.get("light", "unknown") if overseas_gate else "unknown"
    if light == "green":
        score += 0.15; reasons.append("海外绿灯")
    elif light == "yellow":
        score += 0.0; reasons.append("海外黄灯")
    elif light == "red":
        score -= 0.25; reasons.append("海外红灯")

    # 板块资金
    ind = snap.get("industry")
    sf = next((s for s in dfc.fetch_sector_flow("industry") if s.get("name") == ind), None)
    if sf:
        s_pct = _safe(sf.get("pct"))
        s_main = _safe(sf.get("main_pct"))
        if s_pct > 0 and s_main > 0:
            score += 0.1; reasons.append("板块资金正向")
        elif s_pct < 0:
            score -= 0.1; reasons.append("板块回调")

    # 个股舆情
    nh = _normalize_news_hits(news_hits, code)
    if nh.get("neg"):
        score -= 0.2; reasons.append(f"个股负面舆情 {len(nh['neg'])} 条")
    if nh.get("pos"):
        score += 0.1; reasons.append(f"个股正面舆情 {len(nh['pos'])} 条")

    # 技术动量
    mom20 = None
    if kline is not None and len(kline) > 21:
        c = kline["close"]
        mom20 = (c.iloc[-1] / c.iloc[-21] - 1) * 100
        if mom20 > 5:
            score += 0.1; reasons.append("20日动量良好")
        elif mom20 < -5:
            score -= 0.15; reasons.append("20日动量走弱")

    # 今日收盘强弱
    pct = _safe(snap.get("pct"))
    if pct >= 5:
        score += 0.05; reasons.append("今日强势收盘")
    elif pct <= -3:
        score -= 0.1; reasons.append("今日弱势收盘")

    score = min(max(score, 0.05), 0.95)
    if score >= 0.75:
        q = "Q1"; action = "偏强持有/可加仓"; level = "buy"
    elif score >= 0.6:
        q = "Q2"; action = "持有"; level = "hold"
    elif score >= 0.45:
        q = "Q3"; action = "中性观望"; level = "hold"
    elif score >= 0.3:
        q = "Q4"; action = "考虑减仓"; level = "warn"
    else:
        q = "Q5"; action = "竞价/开盘减仓"; level = "sell"

    return {"tier": q, "score": round(score, 2), "action": action,
            "level": level, "reasons": reasons}


def sell_decision(position_state, kline=None, snap=None,
                  overseas_gate=None, news_hits=None):
    """
    完整卖出决策：阶梯止盈 + 4 类强制止损 + 次日竞价 Q1-Q5。
    position_state 来自 tracker 的持仓记录。
    """
    code = position_state["code"]
    name = position_state.get("name", code)
    if snap is None:
        rows = dfc.fetch_realtime_for_codes([code])
        snap = rows[0] if rows else {}
    if kline is None:
        kline = dfc.load_cached_kline(code)

    price = snap.get("price")
    cost = position_state.get("cost")
    peak = position_state.get("peak_price") or price
    hold_days = position_state.get("hold_days")
    mom20 = None
    if kline is not None and len(kline) > 21:
        mom20 = (kline["close"].iloc[-1] / kline["close"].iloc[-21] - 1) * 100
    main_pct = snap.get("main_pct")
    neg = _normalize_news_hits(news_hits, code).get("neg")

    ret_pct = (price / cost - 1) * 100 if (price and cost) else None

    take_profit = ladder_take_profit(ret_pct) if ret_pct is not None else None
    stops = forced_stop_losses(price, cost, peak, hold_days, mom20, main_pct,
                               news_neg=neg, overseas_light=(overseas_gate or {}).get("light"))
    auction = next_day_auction_matrix(code, name=name, kline=kline, snap=snap,
                                      overseas_gate=overseas_gate, news_hits=news_hits)

    sell_signals = [s for s in stops if s["level"] == "sell"]
    action = ("卖出" if sell_signals else
              ("止盈减仓" if take_profit else
               ("关注" if stops else auction["action"])))

    return {
        "code": code, "name": name,
        "ret_pct": round(ret_pct, 2) if ret_pct is not None else None,
        "take_profit": take_profit,
        "forced_stops": stops,
        "auction_matrix": auction,
        "action": action,
        "summary": f"{action}｜次日竞价 {auction['tier']}｜" +
                   (f"止盈 {take_profit['threshold']}%" if take_profit else "未达止盈") +
                   (f" / 止损 {len(sell_signals)} 个" if sell_signals else ""),
    }
