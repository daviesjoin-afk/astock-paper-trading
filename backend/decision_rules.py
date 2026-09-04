# -*- coding: utf-8 -*-
"""与数据源无关的买卖决策规则。

这些函数只处理已经加载好的值，不联网、不读缓存、不写账本。保持
``decision_engine`` 的旧导出名，由兼容层继续转发调用。
"""

import pandas as pd


def _safe(v):
    return v if isinstance(v, (int, float)) and not pd.isna(v) else 0.0


def _normalize_news_hits(news_hits, code):
    """归一化 news_hits，返回 ``{pos: [], neg: []}``。"""
    pos, neg = [], []
    if not news_hits:
        return {"pos": pos, "neg": neg}
    if isinstance(news_hits, dict):
        hit = news_hits.get(code, {})
        return {"pos": hit.get("pos", []), "neg": hit.get("neg", [])}
    for hit in news_hits:
        if hit.get("code") != code:
            continue
        if hit.get("tone", 0) > 0:
            pos.append(hit)
        elif hit.get("tone", 0) < 0:
            neg.append(hit)
    return {"pos": pos, "neg": neg}


def _dim_status(score):
    if score >= 0.6:
        return "green"
    if score >= 0.35:
        return "yellow"
    return "red"


def buy_timing_tier(dims, seal=None):
    """根据六维评分和封单证据返回 T1-T5 时机。"""
    green = sum(1 for dim in dims if dim["status"] == "green")
    red = sum(1 for dim in dims if dim["status"] == "red")
    avg_score = sum(dim["score"] for dim in dims) / len(dims) if dims else 0

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


def ladder_take_profit(ret_pct):
    """根据浮盈返回最高一档止盈规则。"""
    tiers = [
        (5, 10, None),
        (10, 15, 0),
        (15, 20, 3),
        (20, 25, 5),
        (30, 30, 10),
        (50, 40, 20),
        (80, 50, 40),
        (100, 60, 50),
    ]
    active = []
    for threshold, take_pct, protect_profit in tiers:
        if ret_pct >= threshold:
            active.append(
                {
                    "threshold": threshold,
                    "take_pct": take_pct,
                    "protect_profit": protect_profit,
                    "msg": (
                        f"浮盈 ≥{threshold}% 已触发，建议减仓 {take_pct}%，移动止盈保 {protect_profit}%"
                        if protect_profit is not None
                        else f"浮盈 ≥{threshold}% 已触发，建议减仓 {take_pct}%"
                    ),
                }
            )
    return active[-1] if active else None


def forced_stop_losses(price, cost, peak_price, hold_days, mom20, main_pct,
                       news_neg=None, overseas_light=None):
    """根据已加载的持仓与证据值返回止损信号。"""
    signals = []
    ret_pct = (price / cost - 1) * 100 if (price and cost) else None
    drawdown = (1 - price / peak_price) * 100 if (price and peak_price) else None

    if ret_pct is not None and ret_pct <= -8:
        signals.append({"type": "价格止损", "level": "sell",
                        "msg": f"较成本下跌 {abs(ret_pct):.1f}% ≥ 8%"})
    if drawdown is not None and drawdown >= 10:
        signals.append({"type": "回撤止损", "level": "sell",
                        "msg": f"自峰值回撤 {drawdown:.1f}% ≥ 10%"})
    if hold_days is not None and hold_days > 20 and (ret_pct is None or ret_pct < 5):
        signals.append({"type": "时间止损", "level": "warn",
                        "msg": f"持有 {hold_days} 个交易日，收益不足 5%，机会成本过高"})

    deterioration = []
    if mom20 is not None and mom20 < 0:
        deterioration.append(f"20日动量转负({mom20:.1f}%)")
    if isinstance(main_pct, (int, float)) and main_pct < -5:
        deterioration.append(f"主力净流出占比 {main_pct:.1f}%")
    if news_neg:
        deterioration.append(f"负面舆情 {len(news_neg)} 条")
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
