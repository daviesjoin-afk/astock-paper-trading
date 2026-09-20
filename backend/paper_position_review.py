# -*- coding: utf-8 -*-
"""纯 position-review 决策领域模块（R17）。

定位
----
``paper_trading`` 负责证据收集（读持仓、读行情、读 K 线、解析权重、解析替代数据），
本模块只管两件**确定性**的事：

1. :func:`score_quality` —— 质量评分算术、新仓趋势中性化、grade 边界；
2. :func:`decide_action` —— 集中换仓 / 观察 / 持有 动作决策状态机。

硬边界（由 ``test_paper_trading_architecture_guard.py`` 静态强制）::

    zero DB / network / filesystem / wall clock
    不 import paper_trading / strategy_policies / read model / data provider
    相同输入必须逐字得到相同输出

所有输入（weights、各分项、policy 阈值）都由调用方构造并注入 —— 本模块**不解析
账户配置**。尤其：

* ``model_score`` 由调用方通过 :mod:`paper_position_review_evidence` 从
  episode provenance 解析（永远不是"account+code 的最近一条 signal"）；
* 所有 ``*_score`` 都是调用方已算好的 0..100 分项；
* decide 用的阈值从 ``policy`` 读，不在本模块内写死任何策略参数。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ReviewPolicy",
    "POSITION_REVIEW_VERSION",
    "score_quality",
    "decide_action",
]

POSITION_REVIEW_VERSION = "position-review-v1"


@dataclass(frozen=True)
class ReviewPolicy:
    """由 adapter 从 paper_trading 常量构造的决策参数（纯数据，不解析账户）。

    这些值对应 ``paper_trading.py`` 顶部的同名常量；把它们注入纯模块是为了让
    领域层保持 zero-project-import 的同时，阈值仍只有一个事实来源（调用方）。
    """
    lot_size: int = 100
    max_sells_per_run: int = 3
    exit_score: float = 38.0
    replace_score: float = 52.0
    any_replace_score: float = 42.0
    replacement_edge: float = 18.0
    replacement_execution_buffer: float = 3.0
    full_cap_edge: float = 10.0
    full_cap_max_score: float = 68.0
    slot_upgrade_min_candidate_score: float = 75.0
    slot_upgrade_min_edge: float = 25.0
    rotation_max_per_day: int = 2


def _num(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def score_quality(*, model_score: float, trend_score: float, flow_score: float,
                  momentum_score: float, return_score: float, news_penalty: float,
                  weights: dict, hold_days: int) -> dict[str, Any]:
    """0..100 质量评分 —— 公式与旧 ``_position_quality_score`` **逐字等价**。

    新仓趋势中性化必保留：``hold_days < 1`` 时趋势分用 50.0（原始 daily-K 证据
    在首个交易日尚不可信），**不得**因为抽模块就用 raw trend 造成新仓评分漂移。

    返回 ``{"score", "grade", "trend_for_score", "review_phase"}``。
    """
    hold_days = int(hold_days or 0)
    trend_for_score = 50.0 if hold_days < 1 else _num(trend_score, 50.0)
    review_phase = "建仓复核" if hold_days < 1 else "持仓复核"

    score = (
        _num(model_score) * weights["model"]
        + trend_for_score * weights["trend"]
        + _num(flow_score) * weights["flow"]
        + _num(momentum_score) * weights["momentum"]
        + _num(return_score) * weights["return"]
        - _num(news_penalty)
    )
    score = round(max(0.0, min(100.0, score)), 2)

    if hold_days < 1:
        grade = "建仓复核"
    elif score >= 65:
        grade = "核心"
    elif score >= 50:
        grade = "观察"
    elif score >= 40:
        grade = "减仓"
    else:
        grade = "淘汰"

    return {"score": score, "grade": grade,
            "trend_for_score": round(trend_for_score, 2), "review_phase": review_phase}


def decide_action(review, position, quote_status, sells_used, *, policy: ReviewPolicy):
    """集中换仓 / 观察 / 持有 动作决策 —— 与旧 ``_concentration_action`` 逐项等价。

    决策优先级（**顺序不可变**）：quote 陈旧 → missing score → T+1 锁定 →
    每轮卖出上限 → 紧急择强换仓 → 最短观察期 → 趋势确认 → 绝对淘汰分 →
    换仓配额 → 替补优势 / 满位升级 → watch → hold。

    本函数是纯逻辑：不写库、不读行情、不解析账户。它按需要原地写入
    ``review["replacement_execution_buffer"]`` / ``review["replacement_net_edge"]``
    诊断字段（与旧实现一致），返回 ``(action, reason)``。
    """
    pf = policy
    if not quote_status or not quote_status.get("fresh"):
        return "quote_pending", "行情未通过核验，暂不做集中换仓"
    if not review or review.get("score") is None:
        return "review_pending", "持仓评分尚未完成，暂不做集中换仓"
    if int(position.get("available_qty") or 0) < pf.lot_size:
        return "t1_locked", "T+1 可卖份额不足，等待可卖后再评估"
    if sells_used >= pf.max_sells_per_run:
        return "queued", "本轮集中换仓已达到最多三笔，顺延下一轮"

    score = _num(review.get("score"))
    raw_edge = _num(review.get("replacement_edge"), None)
    edge = (
        raw_edge - pf.replacement_execution_buffer
        if raw_edge is not None else None
    )
    review["replacement_execution_buffer"] = pf.replacement_execution_buffer
    review["replacement_net_edge"] = round(edge, 2) if edge is not None else None
    replacement_score = _num(review.get("replacement_score"), None)
    at_dynamic_limit = bool(review.get("at_dynamic_limit"))
    rotations_today = int(_num(review.get("rotations_today")))

    urgent_slot_upgrade = bool(
        replacement_score is not None
        and replacement_score >= pf.slot_upgrade_min_candidate_score
        and edge is not None and edge >= pf.slot_upgrade_min_edge
        and score <= pf.exit_score
    )
    if urgent_slot_upgrade:
        return "consolidation_exit", (
            f"紧急择强换仓：现仓 {score:.1f} 分，替补 {replacement_score:.1f} 分，"
            f"原始分差 {raw_edge:.1f}、扣除执行缓冲后 {edge:.1f}；"
            "豁免最短观察期但不豁免 T+1/行情/总池门禁"
        )

    min_hold_days = int(_num(review.get("min_hold_days"), 2))
    if review.get("hold_days", 0) < min_hold_days:
        return "new_position", (
            f"持仓观察期 {review.get('hold_days', 0)}/{min_hold_days} 日，"
            "暂不因评分换仓"
        )

    can_replace = (
        edge is not None and edge >= pf.replacement_edge
        and (
            (review.get("small_position") and score < pf.replace_score)
            or score <= pf.any_replace_score
        )
    )
    full_slot_upgrade = (
        at_dynamic_limit
        and edge is not None and edge >= pf.full_cap_edge
        and score < pf.full_cap_max_score
    )
    if position.get("account_id") == "trend_pullback" and score <= pf.exit_score:
        if not bool(review.get("quality_exit_confirmed")):
            return "watch", (
                f"趋势持仓评分 {score:.1f} 低于淘汰线，但尚无连续观察确认；"
                "保留观察，等待下一完整窗口或结构破坏"
            )
    if score <= pf.exit_score:
        return "consolidation_exit", f"持仓质量评分 {score:.1f} 低于淘汰线 {pf.exit_score:.0f}"
    if (can_replace or full_slot_upgrade) and rotations_today >= pf.rotation_max_per_day:
        return "queued", (
            f"本策略今日主动换仓已达 {rotations_today}/{pf.rotation_max_per_day} 上限，"
            "候选保留至下一轮/下一交易日"
        )
    if can_replace or full_slot_upgrade:
        if full_slot_upgrade:
            return "consolidation_exit", (
                f"动态席位已满，择强换股：现仓 {score:.1f} 分，"
                f"替补原始高 {raw_edge:.1f} 分，扣执行缓冲后仍高 {edge:.1f} 分"
            )
        if can_replace:
            return "consolidation_exit", (
                f"低质量小仓换仓：评分 {score:.1f}，后备候选原始高 {raw_edge:.1f} 分，"
                f"扣执行缓冲后高 {edge:.1f} 分"
            )
    if score < pf.replace_score:
        return "watch", f"评分 {score:.1f} 偏弱，尚无足够优势候选替换"
    return "hold", f"评分 {score:.1f}，保留并等待策略加仓确认"