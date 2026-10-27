# -*- coding: utf-8 -*-
"""负面公告/事件的严重度分级与新增仓位额度决策。

为什么独立成模块
----------------
这段策略原本内联在 ``paper_trading.py``（1.6 万行的单体）里。它只依赖"负面
行的关键词/正文/核验位"这几个纯数据，没有任何行情、账本或时钟依赖，因此
抽出来既让 ``paper_trading`` 变薄，也让分级规则可以脱离交易链路单独验证。

分级规则
--------
原实现把"一条已核验负面"一律判为禁止新增仓位。但负面关键词表里
"股东减持 / 股权质押 / 业绩下滑"与"立案调查 / 退市风险 / 爆雷"混在同一档，
结果一次常规减持公告就把该股彻底冻住，而真正的高风险事件本来就是该拦的。

    severe  （立案/调查/退市/爆雷/冻结/商誉减值）→ 禁止新增仓位（不放宽）
    serious （违规/处罚/诉讼）                  → 允许，额度压到 30%
    minor   （减持/质押/亏损/下滑）              → 允许，额度压到 60%

边界（刻意保持不变）
--------------------
* 只决定**新增仓位**的额度与许可，不参与卖出/止损/减仓判定。
* 不放宽任何行情、成交、T+1、席位或资金门禁；这些仍在原处执行。
* 未完成核验的负面快讯仍是 50% 缩减；数据源过期仍再乘 0.75。
* 一批里既有轻条目又有重条目时取**最重**档：分级只能更严，不能被稀释。
"""
from __future__ import annotations

#: 直接威胁持续经营或触发监管处置的事项：维持"禁止新增仓位"。
NEGATIVE_SEVERE = ("立案", "调查", "退市", "爆雷", "冻结", "商誉减值")
#: 有实质法律/合规含义但通常可继续经营：允许建仓但大幅缩量。
NEGATIVE_SERIOUS = ("违规", "处罚", "诉讼")
#: 常规公司行为或经营波动：小幅缩量。
NEGATIVE_MINOR = ("减持", "质押", "亏损", "下滑")

TIERS = (("severe", NEGATIVE_SEVERE),
         ("serious", NEGATIVE_SERIOUS),
         ("minor", NEGATIVE_MINOR))

#: 每个档位对应的 (模式名, 额度比例, 是否允许新增, 说明模板)。
TIER_POLICY = {
    "severe": ("公告否决", 0.0, False, "禁止该股新增仓位"),
    "serious": ("公告降级", 0.30, True, "新增仓位缩减70%"),
    "minor": ("公告限额", 0.60, True, "新增仓位缩减40%"),
}


def classify(rows):
    """把一组负面行归到最高严重度档，返回 ``(tier, 命中关键词)``。

    关键词既看结构化的 ``keywords``，也看 ``summary`` 正文——公告标题/正文
    才是权威来源，只认结构化字段会漏掉未被分词命中的情形。
    """
    words = set()
    for row in rows or ():
        for word in row.get("keywords") or ():
            words.add(str(word))
        summary = str(row.get("summary") or "")
        if not summary:
            continue
        for _tier, table in TIERS:
            for word in table:
                if word in summary:
                    words.add(word)
    for tier, table in TIERS:
        hit = sorted(words & set(table))
        if hit:
            return tier, hit
    return "none", []


def decide(verified_negative, *, unverified_negative_count=0, stale_source=False,
           market_light="green"):
    """返回 ``(mode, scale, allowed, reason)``。

    ``verified_negative`` 是**已完成公告核验**的负面行；``unverified_negative_count``
    是未核验的负面快讯条数。两者分开是因为它们的证据强度不同：前者按严重度
    分级，后者只能统一缩量。
    """
    verified_negative = list(verified_negative or ())
    if verified_negative:
        tier, hit_words = classify(verified_negative)
        label = "/".join(hit_words[:3]) or "未分级"
        mode, scale, allowed, note = TIER_POLICY.get(tier, TIER_POLICY["minor"])
        reason = f"已核验负面公告/事件 {len(verified_negative)} 条（{label}），{note}"
    elif unverified_negative_count:
        mode, scale, allowed = "舆情收紧", 0.50, True
        reason = (f"负面快讯 {unverified_negative_count} 条尚未完成公告核验，"
                  f"新增仓位缩减50%")
    else:
        mode, scale, allowed = "正常", 1.0, True
        reason = "未命中负面公告或快讯"

    # 数据源过期只影响"尚未核验"的情形；已核验的重事件本来就已否决/降级，
    # 再乘一次系数会让原因文本与实际额度对不上。
    if stale_source and not verified_negative:
        scale *= 0.75
        mode = "舆情源降级"
        reason = "新闻/公告扫描已过期，新增仓位按75%额度执行"

    if str(market_light or "unknown") in {"red", "unknown"}:
        scale = 0.0
        allowed = False
        mode = "市场暂停"
        reason = "市场红灯或数据未知，暂停该股新增仓位"

    return mode, scale, allowed, reason


__all__ = [
    "NEGATIVE_SEVERE", "NEGATIVE_SERIOUS", "NEGATIVE_MINOR", "TIERS",
    "TIER_POLICY", "classify", "decide",
]
