# -*- coding: utf-8 -*-
"""PR-37：内置策略声明式画像（EntryPolicy / ReviewPolicy / CooldownPolicy）。

paper_trading.py 不再拥有任何按 ``account_id`` 硬编码的策略表：内置五套
的执行/复盘/冷却参数全部集中在这里作为**声明式配置**，经访问器读取；
未声明的账户一律得到 fail-closed 的通用默认值。

新增用户策略**不需要也不允许**修改本模块或 paper_trading.py——用户策略
的 EntryPolicy/ReviewPolicy 全部由生产编译管线（strategy_runtime：
DSL → SRF 指纹 → SRP 风险画像 → EPF 执行画像）在运行时派生，见
``user_strategy_participation.user_spec_for``。
"""
from __future__ import annotations

# 内置策略标识（声明键的唯一来源，paper_trading.py 反向引用本模块）。
NEW_STRATEGY_ID = "reported_profit_breakout"
MAIN_FORCE_STRATEGY_ID = "main_force_top10"

# ---------------------------------------------------------------------------
# EntryPolicy：共享开盘事件引擎（09:30/09:31/13:00 冲高回落）。
# 各策略只通过画像决定阈值和仓位比例，引擎本身不认识任何具体策略。
# ---------------------------------------------------------------------------
OPENING_EVENT_POLICIES: dict[str, dict] = {
    "tq_breakout": {
        "name": "短线日内做T",
        "enabled": True,
        "min_peak_pct": 3.0,
        "min_retrace_pct": 2.2,
        "min_current_pct": -1.0,
        "min_peak_edge_pct": -8.0,
        "trim_ratio": 0.30,
        "allow_loss_trim": True,
        "rebuy_rebound_pct": 1.2,
        "rebuy_max_sold_ratio": 0.995,
        "rebuy_min_observations": 2,
        "rebuy_min_main_pct": -0.25,
        "rebuy_min_current_pct": -1.5,
    },
    "trend_pullback": {
        "name": "趋势波段优选",
        "enabled": True,
        "min_peak_pct": 4.0,
        "min_retrace_pct": 2.6,
        "min_current_pct": -0.8,
        "min_peak_edge_pct": 1.5,
        "trim_ratio": 0.20,
        "allow_loss_trim": False,
        "rebuy_rebound_pct": 1.8,
        "rebuy_max_sold_ratio": 0.985,
        "rebuy_min_observations": 2,
        "rebuy_min_main_pct": 0.20,
        "rebuy_min_current_pct": -0.8,
    },
    "sector_rotation": {
        "name": "板块轮动先锋",
        "enabled": True,
        "min_peak_pct": 4.5,
        "min_retrace_pct": 2.8,
        "min_current_pct": -1.0,
        "min_peak_edge_pct": 1.0,
        "trim_ratio": 0.25,
        "allow_loss_trim": False,
        "rebuy_rebound_pct": 1.5,
        "rebuy_max_sold_ratio": 0.985,
        "rebuy_min_observations": 2,
        "rebuy_min_main_pct": 0.50,
        "rebuy_min_current_pct": -0.5,
    },
    NEW_STRATEGY_ID: {
        "name": "财报突破质量",
        "enabled": True,
        "min_peak_pct": 3.5,
        "min_retrace_pct": 2.4,
        "min_current_pct": -0.8,
        "min_peak_edge_pct": 1.5,
        "trim_ratio": 0.25,
        "allow_loss_trim": False,
        "rebuy_rebound_pct": 1.6,
        "rebuy_max_sold_ratio": 0.985,
        "rebuy_min_observations": 2,
        "rebuy_min_main_pct": 0.20,
        "rebuy_min_current_pct": -0.8,
    },
    MAIN_FORCE_STRATEGY_ID: {"name": "超强主力股", "enabled": True,
        "min_peak_pct": 4.0, "min_retrace_pct": 2.5, "min_current_pct": -1.0,
        "min_peak_edge_pct": 1.5, "trim_ratio": 0.25, "allow_loss_trim": False,
        "rebuy_rebound_pct": 1.8, "rebuy_max_sold_ratio": 0.985,
        "rebuy_min_observations": 2, "rebuy_min_main_pct": 0.50,
        "rebuy_min_current_pct": -0.5},
}


def opening_event_policy(account_id) -> dict:
    """EntryPolicy 访问器。未声明账户 fail-closed：开盘事件引擎不介入。"""
    return OPENING_EVENT_POLICIES.get(str(account_id or "")) or {}


# ---------------------------------------------------------------------------
# ReviewPolicy：盘中下行守卫阶梯（warning → partial → full 两段确认）。
# ---------------------------------------------------------------------------
INTRADAY_DOWNSIDE_POLICIES: dict[str, dict] = {
    "tq_breakout": {
        "warning_pct": -2.0, "partial_pct": -3.0, "full_pct": -5.0,
        "relative_pct": -2.5, "peak_retrace_pct": 3.5,
        "partial_ratio": 0.35,
        # 首次下跌预警不是硬止损：只处理当日可卖仓的四分之一，
        # 每标的一天最多一次；后续恶化仍走 partial/full 防线。
        "warning_trim_ratio": 0.25,
        # Protect a profitable position from giving back its edge even when
        # low-frequency flow looks like a possible washout.
        "giveback_partial_pct": 5.5, "giveback_full_pct": 9.0,
        "giveback_min_peak_return_pct": 4.0,
    },
    "trend_pullback": {
        "warning_pct": -2.2, "partial_pct": -3.0, "full_pct": -5.0,
        "relative_pct": -2.7, "peak_retrace_pct": 4.0,
        "partial_ratio": 0.35,
        "giveback_partial_pct": 5.0, "giveback_full_pct": 8.0,
        "giveback_min_peak_return_pct": 4.0,
    },
    "sector_rotation": {
        # Recent ledger results show that this fast-rotation sleeve was
        # allowing weak hot-theme names to become large losses.  A genuine
        # sector leader should either recover promptly or be replaced; retain
        # two distinct scans, but tighten the loss/retrace ladder.
        "warning_pct": -2.2, "partial_pct": -3.0, "full_pct": -4.5,
        "relative_pct": -2.5, "peak_retrace_pct": 3.5,
        "partial_ratio": 0.40,
        "giveback_partial_pct": 5.0, "giveback_full_pct": 8.0,
        "giveback_min_peak_return_pct": 4.0,
    },
    NEW_STRATEGY_ID: {
        # A quality/breakout holding tolerates less structural damage than a
        # broad trend position, while requiring a distinct confirmation scan
        # before partial/full exits.  Warning trim remains independent from
        # the legacy TQ thresholds.
        "warning_pct": -1.8, "partial_pct": -3.2, "full_pct": -5.8,
        "relative_pct": -2.4, "peak_retrace_pct": 3.2,
        "partial_ratio": 0.40, "warning_trim_ratio": 0.20,
        "giveback_partial_pct": 4.5, "giveback_full_pct": 8.0,
        "giveback_min_peak_return_pct": 3.5,
    },
    MAIN_FORCE_STRATEGY_ID: {
        "warning_pct": -2.0, "partial_pct": -3.0, "full_pct": -5.0,
        "relative_pct": -2.5, "peak_retrace_pct": 4.0,
        "partial_ratio": 0.50, "warning_trim_ratio": 0.0,
        "giveback_partial_pct": 5.0, "giveback_full_pct": 8.0,
        "giveback_min_peak_return_pct": 4.0,
    },
}

# ReviewPolicy 通用默认：与历史行为一致——未声明账户没有下行守卫阶梯
# （dict 为空时 _downside_guard 返回 level=none），其退出保护由编译
# Risk Profile 的 hard_stop / trail_stop / hold_max 独立兜底。


def intraday_downside_policy(account_id) -> dict:
    """ReviewPolicy 访问器。未声明账户返回空 dict（不启用下行守卫阶梯）。"""
    return INTRADAY_DOWNSIDE_POLICIES.get(str(account_id or "")) or {}


# ---------------------------------------------------------------------------
# CooldownPolicy：结构性拒绝后的复审冷却（分钟）。
# ---------------------------------------------------------------------------
BOOTSTRAP_STRUCTURAL_RECHECK_COOLDOWN_MINUTES: dict[str, int] = {
    "tq_breakout": 6,
    "trend_pullback": 12,
    "sector_rotation": 9,
    NEW_STRATEGY_ID: 15,
    MAIN_FORCE_STRATEGY_ID: 9,
}


def bootstrap_recheck_cooldown_minutes(account_id) -> int:
    """CooldownPolicy 访问器。未声明账户无结构性冷却（0 分钟）。"""
    return int(BOOTSTRAP_STRUCTURAL_RECHECK_COOLDOWN_MINUTES.get(str(account_id or ""), 0))
