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


# ---------------------------------------------------------------------------
# RotationPolicy：主动换仓的观察窗口（PR-41 自 paper_trading.py 迁入）。
# sellable 弱持仓只有观察满 min_hold_days 后，才能把整席让给显著更强的候选。
# T+1 仍是第一道闸门，0 天窗口也绝不让当日买入的股份可卖。
# ---------------------------------------------------------------------------
POSITION_REVIEW_MIN_HOLD_DAYS_DEFAULT = 2  # 未声明账户的保守回退值
POSITION_REVIEW_MIN_HOLD_DAYS_BY_STRATEGY: dict[str, int] = {
    "tq_breakout": 1,
    "trend_pullback": 2,
    "sector_rotation": 0,
    NEW_STRATEGY_ID: 1,
    MAIN_FORCE_STRATEGY_ID: 1,
}


def position_review_min_hold_days(account_id) -> int:
    """RotationPolicy 访问器。未声明账户回退到保守默认（2 天）。"""
    return int(POSITION_REVIEW_MIN_HOLD_DAYS_BY_STRATEGY.get(
        str(account_id or ""), POSITION_REVIEW_MIN_HOLD_DAYS_DEFAULT,
    ))


# ---------------------------------------------------------------------------
# CooldownPolicy：两次同日风控拒绝后的递进冷却（分钟）（PR-41 迁入）。
# 首次拒绝仍可下一轮复审；连续两次才进入策略本地冷却，之后按 2 的幂
# 保守延长（封顶值 RISK_REJECT_COOLDOWN_MAX_MINUTES 仍留在执行引擎）。
# ---------------------------------------------------------------------------
RISK_REJECT_COOLDOWN_AFTER_TWO_MINUTES: dict[str, int] = {
    "tq_breakout": 30,
    "trend_pullback": 75,
    "sector_rotation": 45,
    NEW_STRATEGY_ID: 90,
    MAIN_FORCE_STRATEGY_ID: 45,
}


def risk_reject_cooldown_minutes(account_id) -> int:
    """CooldownPolicy 访问器。未声明账户无冷却（0 分钟）。"""
    return int(RISK_REJECT_COOLDOWN_AFTER_TWO_MINUTES.get(str(account_id or ""), 0))


# ---------------------------------------------------------------------------
# RecoveryPolicy：保护性退出后的受控恢复观察（PR-41 迁入）。
# 防护性退出允许一次受控的恢复观察路径，但绝不能变成立即追涨或绕过
# 常规入场闸门。
# ---------------------------------------------------------------------------
RECOVERY_WATCH_STATUS = "recovery_watch"
RECOVERY_POLICIES: dict[str, dict] = {
    "tq_breakout": {"min_scans": 2, "cooldown_minutes": 15, "reclaim_pct": 0.005, "max_days": 1},
    "trend_pullback": {"min_scans": 2, "cooldown_minutes": 60, "reclaim_pct": 0.008, "max_days": 3},
    "sector_rotation": {"min_scans": 3, "cooldown_minutes": 60, "reclaim_pct": 0.010, "max_days": 2},
    NEW_STRATEGY_ID: {"min_scans": 2, "cooldown_minutes": 45, "reclaim_pct": 0.008, "max_days": 2},
    MAIN_FORCE_STRATEGY_ID: {"min_scans": 2, "cooldown_minutes": 45, "reclaim_pct": 0.010, "max_days": 2},
}


def recovery_policy(account_id) -> dict:
    """RecoveryPolicy 访问器。未声明账户回退到 trend_pullback 模板（历史行为）。"""
    table = RECOVERY_POLICIES
    return dict(table.get(str(account_id or ""), table["trend_pullback"]))


# ---------------------------------------------------------------------------
# EntryEconomicsPolicy：碎单经济性门槛（PR-41 迁入）。
# 短线日内做T的低额委托经常被最低佣金、卖出印花税和双向滑点吞掉。
# 这不是提高仓位上限，而是拒绝"理论有收益、成本后没有意义"的碎单。
# 未声明账户返回空 dict：不启用该门槛（与历史"仅 tq_breakout"一致）。
# ---------------------------------------------------------------------------
ENTRY_ECONOMICS_POLICIES: dict[str, dict] = {
    "tq_breakout": {
        "min_effective_order_amount": 4_000.0,
        "min_expected_edge_pct": 0.008,
    },
}


def entry_economics_policy(account_id) -> dict:
    """EntryEconomicsPolicy 访问器。未声明账户返回空 dict（不启用碎单门槛）。"""
    return dict(ENTRY_ECONOMICS_POLICIES.get(str(account_id or "")) or {})
