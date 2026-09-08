# -*- coding: utf-8 -*-
"""跨策略组合协调器（PR：cross-strategy exposure and intent coordinator）。

各策略账本保持独立，但 **风险按共享组合汇总**：同一 symbol 的持仓在所有
策略间合并计算，行业与主题（theme）敞口同样合并。本模块提供：

1. **意图优先级**：``P0 风控退出 > P1 止盈/轮出 > P2 手动卖出 > P3 风险减仓
   > P4 新开仓 > P5 加仓``。P0 在途时，同一标的的 P5 加仓必须让位。
2. **聚合敞口**：把持仓 + 在途买单按 symbol / industry / theme 三个维度
   汇总，供 sizing 与闸门使用。
3. **symbol 余量**：``symbol_headroom`` 回答"这个 symbol 还能买多少"——
   已用 = 全策略持仓市值 + **全策略在途买单金额**。没有在途口径时，两个
   策略同时买入同一标的会各自只看到已成交部分，合计击穿单票上限。

失败语义：数据库读取异常一律按"无在途单"返回空集合（读失败不阻塞主扫描），
真正的硬上限仍由 sizing 与风控兜底。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "PORTFOLIO_COORDINATOR_VERSION",
    "INTENT_PRIORITY",
    "INTENT_PRIORITY_INDEX",
    "DEFAULT_THEME_MAP",
    "aggregate_exposure",
    "classify_intent",
    "pending_risk_exit_codes",
    "pending_symbol_amounts",
    "sort_intents_by_priority",
    "symbol_headroom",
    "theme_for",
]

PORTFOLIO_COORDINATOR_VERSION = "portfolio-coordinator-v1"

# 意图优先级：数字越小越优先。P0 风控退出永远先于一切买入意图执行。
INTENT_PRIORITY: tuple[tuple[str, str, str], ...] = (
    ("P0", "risk_exit", "风控退出（硬止损/强平/崩盘清仓）"),
    ("P1", "take_profit_exit", "止盈/轮出退出"),
    ("P2", "manual_exit", "手动卖出"),
    ("P3", "risk_reduce", "风险减仓（部分止盈/降敞口）"),
    ("P4", "new_entry", "新开仓"),
    ("P5", "add_position", "确认加仓"),
)
INTENT_PRIORITY_INDEX = {name: index for index, (name, _, _) in enumerate(INTENT_PRIORITY)}
INTENT_LABELS = {name: label for name, _, label in INTENT_PRIORITY}

# 卖出意图的 purpose 关键字 → 优先级（新买入默认 P4，加仓默认 P5）。
_EXIT_PURPOSE_KEYWORDS = (
    ("P0", ("hard_stop", "崩盘", "强平", "liquidation", "risk_exit", "drawdown")),
    ("P1", ("take_profit", "轮出", "rotation_out", "scale_out")),
    ("P2", ("manual",)),
    ("P3", ("reduce", "降敞口", "partial")),
)

# 行业 → 主题（theme）聚合的保守默认映射；未命中的行业自成主题。
DEFAULT_THEME_MAP: dict[str, str] = {
    "银行": "金融", "证券": "金融", "保险": "金融", "多元金融": "金融",
    "半导体": "科技", "软件": "科技", "计算机": "科技", "通信": "科技",
    "电子": "科技", "传媒": "科技",
    "白酒": "消费", "食品": "消费", "饮料": "消费", "家电": "消费",
    "纺织": "消费", "零售": "消费", "商贸": "消费",
    "医药": "医药医疗", "生物": "医药医疗", "医疗器械": "医药医疗",
    "中药": "医药医疗",
    "电力": "公用事业", "燃气": "公用事业", "水务": "公用事业",
    "钢铁": "周期", "煤炭": "周期", "有色": "周期", "化工": "周期",
    "建材": "周期", "石油": "周期", "采掘": "周期",
    "军工": "国防军工", "航天": "国防军工", "船舶": "国防军工",
    "房地产": "地产建筑", "建筑": "地产建筑", "装修": "地产建筑",
    "汽车": "汽车制造", "汽车零部件": "汽车制造",
    "运输": "交运物流", "物流": "交运物流", "航空": "交运物流", "港口": "交运物流",
}


def classify_intent(side: str, purpose: str = "") -> dict[str, Any]:
    """把一笔意图（买卖方向 + 目的）归类到 P0–P5 优先级。"""
    side_name = str(side or "").strip().lower()
    purpose_text = str(purpose or "").strip().lower()
    if side_name == "sell":
        priority = "P3"
        for name, keywords in _EXIT_PURPOSE_KEYWORDS:
            if any(keyword.lower() in purpose_text for keyword in keywords):
                priority = name
                break
    elif side_name == "buy":
        priority = "P4"
    else:
        priority = "P4"
    if "add" in purpose_text or "加仓" in purpose_text or "scale_in" in purpose_text:
        priority = "P5" if side_name == "buy" else priority
    return {
        "side": side_name or "buy",
        "priority": priority,
        "label": INTENT_LABELS.get(priority, priority),
        "version": PORTFOLIO_COORDINATOR_VERSION,
    }


def sort_intents_by_priority(intents: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 P0→P5 稳定排序意图；缺省优先级的意图按 P4 处理。"""
    def _key(item: Mapping[str, Any]) -> tuple[int, int]:
        priority = str(item.get("priority") or "")
        return (INTENT_PRIORITY_INDEX.get(priority, INTENT_PRIORITY_INDEX["P4"]), int(item.get("order", 0)))

    return [dict(item) for item in sorted(intents, key=_key)]


def theme_for(industry: Any, theme_map: Mapping[str, str] | None = None) -> str:
    """行业 → 主题；未命中映射的行业自成主题（保守，不强行归并）。"""
    name = str(industry or "").strip()
    if not name:
        return "未分类"
    mapping = DEFAULT_THEME_MAP if theme_map is None else theme_map
    if not mapping:
        return name
    return mapping.get(name, name)


def pending_symbol_amounts(conn, *, exclude_signal_id=None) -> dict[str, float]:
    """按 symbol 汇总所有策略的在途买单金额（qty × planned_price）。

    ``exclude_signal_id`` 用于成交路径：同一信号的重试在 sizing 时点尚未
    替换旧单，排除自身后才能避免"自己挂单压低自己"的自缩循环。
    """
    if conn is None:
        return {}
    try:
        if exclude_signal_id is not None:
            rows = conn.execute(
                """SELECT code, COALESCE(SUM(qty * COALESCE(planned_price,0)),0) AS amount
                     FROM paper_orders
                    WHERE side='buy' AND status IN
                          ('pending_limit','deferred_capacity','execution_retry',
                           'manual_execution_retry','entry_frozen_waitlist','awaiting_batch',
                           'pending_verification')
                      AND (signal_id IS NULL OR signal_id<>?)
                    GROUP BY code""",
                (int(exclude_signal_id),),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT code, COALESCE(SUM(qty * COALESCE(planned_price,0)),0) AS amount
                     FROM paper_orders
                    WHERE side='buy' AND status IN
                          ('pending_limit','deferred_capacity','execution_retry',
                           'manual_execution_retry','entry_frozen_waitlist','awaiting_batch',
                           'pending_verification')
                    GROUP BY code"""
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(row["code"]): float(row["amount"] or 0.0) for row in rows}


def pending_risk_exit_codes(conn) -> set[str]:
    """返回存在在途卖出意图（含风控退出）的 symbol 集合。"""
    if conn is None:
        return set()
    try:
        rows = conn.execute(
            """SELECT DISTINCT code FROM paper_orders
                WHERE side='sell'
                  AND status IN ('pending_limit','deferred_capacity','execution_retry',
                                 'manual_execution_retry','unfilled_limit_down')"""
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row["code"]) for row in rows}


def _positions_and_quotes(
    positions: Sequence[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]] | None,
) -> list[tuple[str, float, str]]:
    priced: list[tuple[str, float, str]] = []
    for pos in positions:
        code = str(pos.get("code") or "")
        quote = (quotes or {}).get(code) or {}
        price = float(quote.get("price") or 0.0) or float(pos.get("cost") or 0.0)
        priced.append((code, max(0.0, float(pos.get("qty") or 0)) * max(0.0, price),
                       str(pos.get("industry") or "")))
    return priced


def aggregate_exposure(
    positions: Sequence[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    pending_by_symbol: Mapping[str, float] | None = None,
    theme_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """把持仓按 symbol / industry / theme 汇总（跨策略共享组合口径）。"""
    by_symbol: dict[str, float] = {}
    by_industry: dict[str, float] = {}
    by_theme: dict[str, float] = {}
    total = 0.0
    for code, value, industry in _positions_and_quotes(positions, quotes):
        by_symbol[code] = by_symbol.get(code, 0.0) + value
        by_industry[industry or "未知"] = by_industry.get(industry or "未知", 0.0) + value
        theme = theme_for(industry, theme_map)
        by_theme[theme] = by_theme.get(theme, 0.0) + value
        total += value
    pending = dict(pending_by_symbol or {})
    for code, amount in pending.items():
        # 在途买单不进总市值（未成交），但按 symbol 暴露在待执行敞口里。
        by_symbol[code] = by_symbol.get(code, 0.0)
    return {
        "total_value": round(total, 2),
        "by_symbol": {key: round(value, 2) for key, value in by_symbol.items()},
        "by_industry": {key: round(value, 2) for key, value in by_industry.items()},
        "by_theme": {key: round(value, 2) for key, value in by_theme.items()},
        "pending_by_symbol": {key: round(value, 2) for key, value in pending.items()},
        "version": PORTFOLIO_COORDINATOR_VERSION,
    }


def symbol_headroom(
    code: str,
    aggregate: Mapping[str, Any],
    *,
    cap_amount: float,
) -> dict[str, Any]:
    """该 symbol 的组合级余量：上限 − (全策略持仓 + 全策略在途买单)。

    ``cap_amount <= 0`` 表示未配置组合级单票上限，返回不限（allowed=True）。
    """
    cap = max(0.0, float(cap_amount or 0.0))
    code_name = str(code or "")
    used = float((aggregate.get("by_symbol") or {}).get(code_name, 0.0))
    pending = float((aggregate.get("pending_by_symbol") or {}).get(code_name, 0.0))
    committed = used + pending
    if cap <= 0.0:
        return {
            "allowed": True, "cap_amount": 0.0, "used_amount": round(used, 2),
            "pending_amount": round(pending, 2), "headroom_amount": None,
            "reason": None, "version": PORTFOLIO_COORDINATOR_VERSION,
        }
    headroom = max(0.0, cap - committed)
    allowed = committed < cap - 1e-6
    return {
        "allowed": allowed,
        "cap_amount": round(cap, 2),
        "used_amount": round(used, 2),
        "pending_amount": round(pending, 2),
        "headroom_amount": round(headroom, 2),
        "reason": None if allowed else (
            f"组合级单票上限已占满：{code_name} 持仓 {used:,.0f} + 在途 {pending:,.0f} "
            f"≥ 上限 {cap:,.0f}，新买入不允许绕过聚合上限"
        ),
        "version": PORTFOLIO_COORDINATOR_VERSION,
    }
