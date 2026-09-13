# -*- coding: utf-8 -*-
"""纸盘账户**声明层**（Extract Paper Account Specs Boundary）。

**归属**：纸盘账户的声明式配置——内置账户 spec、风格声明、风险画像声明、
保守回退 spec，以及只读的纯查询访问器。

**它回答什么**：在纸盘账户配置层，这个账户**被声明**成什么样？
（名称、来源策略、模式、周期天数、席位/权重/敞口上限、风险画像键、默认风格、
入场模型名、因子滞后上限、持有窗口、止损/移动保护、跳空区间、止盈阶梯……）

**它不回答什么**（这些各有唯一 owner，绝不复制进本模块）：

- 这个账户**能否执行 / 是否参与当期周期** → ``paper_trading`` 的周期参与者解析
  （周期快照 ∩ 账本挂接，PR-38/48）；
- 它在注册表里**是否 active / 是否支持新周期** → ``strategy_registry.active_ids``；
- 它的**生命周期状态 / 版本 / checksum / 运行时就绪** → ``strategy_registry`` /
  ``strategy_runtime``；
- 它的**执行许可**（T+1、涨跌停、行情新鲜度、证券范围）→ 系统风控层；
- **用户策略**的 spec 派生 → ``user_strategy_participation.user_spec_for``
  （来自 RuntimeContext 编译画像，不是本模块的声明表）。

换句话说：``account declarative specs != strategy registry/runtime truth
!= current cycle ownership != execution eligibility``。

**依赖方向**（单向，反向禁止）::

    paper_trading  →  paper_account_specs  →  strategy_policies
                                          →  stdlib

本模块无数据库连接、无网络 I/O、无订单/撮合、无调度器启动、无生命周期或周期写入；
import 本模块不改变任何运行时状态。

**可变性契约**：模块级表是声明真相，调用方不得原地修改；所有查询访问器返回
**独立副本**，调用方对返回值的任何改动都不会污染后续查询。

行为与抽取前逐字等价：只搬常量与纯函数，不改一行语义。
"""
from __future__ import annotations

import copy

import strategy_policies as SPOL

__all__ = [
    "SPECS_MODULE_VERSION",
    "NEW_STRATEGY_ID",
    "MAIN_FORCE_STRATEGY_ID",
    "NEW_STRATEGY_VERSION",
    "MAIN_FORCE_STRATEGY_VERSION",
    "STYLE_PROFILES",
    "RISK_PROFILES",
    "ACCOUNT_SPECS",
    "UNKNOWN_USER_SPEC",
    "builtin_spec",
    "fallback_spec",
    "builtin_account_ids",
    "has_style",
    "style_profile",
    "style_name",
    "risk_profile",
    "default_risk_profile_key",
]

SPECS_MODULE_VERSION = "paper-account-specs-v1"

# 内置策略标识的唯一声明源在 strategy_policies（PR-37），本模块只反向引用。
NEW_STRATEGY_ID = SPOL.NEW_STRATEGY_ID
MAIN_FORCE_STRATEGY_ID = SPOL.MAIN_FORCE_STRATEGY_ID
NEW_STRATEGY_VERSION = "reported-profit-breakout-v1"
MAIN_FORCE_STRATEGY_VERSION = "main-force-top10-v1"

# ---------------------------------------------------------------------------
# 风格声明：候选风格 → 展示名 + 源策略。
# ---------------------------------------------------------------------------
STYLE_PROFILES = {
    "strong": {"name": "强势接力", "source_strategy": "one_to_two"},
    "pullback": {"name": "趋势回踩", "source_strategy": "bottom_reversal"},
    "sector": {"name": "板块轮动", "source_strategy": "sentiment_pioneer"},
    "quality": {"name": "三日策略", "source_strategy": NEW_STRATEGY_ID},
    "main_force": {"name": "超强主力股", "source_strategy": MAIN_FORCE_STRATEGY_ID},
}

# ---------------------------------------------------------------------------
# 风险画像声明：账户 spec 的 ``risk_profile`` 取值域（敞口/权重/行业/
# 单笔风险/日损/回撤/冷却/成本边际）。
# ---------------------------------------------------------------------------
RISK_PROFILES = {
    # 2026-08-24 集中化改造：总硬上限 15（动态分配），单笔风险预算
    # 提升至 ~1.2% NAV，使 risk 约束给出的单仓 ≈ ¥16-22K，与 weight 上限
    # 大致同量级。资金利用率目标从 ~35% 提升到 ~75%。
    # 单账户最坏并发止损 = 3 × 1.2% = 3.6% NAV，daily_loss/drawdown 同步
    # 放宽以避免集中仓位在普通波动日频繁触发冷却。
    "breakout": {
        "name": "接力快进快出", "max_weight": 0.32, "max_exposure": 0.95,
        "max_industry": 0.42, "single_risk": 0.012,
        "daily_loss": 0.035, "drawdown": 0.11,
        "cooldown_days": 2, "min_cost_edge": 0.006,
    },
    "trend": {
        "name": "趋势集中持有", "max_weight": 0.34, "max_exposure": 0.95,
        "max_industry": 0.45, "single_risk": 0.012,
        "daily_loss": 0.040, "drawdown": 0.13,
        "cooldown_days": 3, "min_cost_edge": 0.004,
    },
    "sector": {
        "name": "热点轮动集中", "max_weight": 0.32, "max_exposure": 0.92,
        # 热点轮动允许集中，但不能把“板块强势”误当成可无限叠加同业的理由。
        # 42% 仍保留核心主题表达，同时给后续轮动留出缓冲。
        "max_industry": 0.42, "single_risk": 0.012,
        "daily_loss": 0.040, "drawdown": 0.12,
        "cooldown_days": 2, "min_cost_edge": 0.005,
    },
    "core_quality": {
        "name": "三日策略独立风控", "max_weight": 0.32, "max_exposure": 0.90,
        "max_industry": 0.38, "single_risk": 0.012,
        "daily_loss": 0.035, "drawdown": 0.11,
        "cooldown_days": 3, "min_cost_edge": 0.005,
    },
    "main_force": {
        "name": "主力持续性独立风控", "max_weight": 0.34, "max_exposure": 0.95,
        "max_industry": 0.45, "single_risk": 0.012,
        "daily_loss": 0.040, "drawdown": 0.12,
        "cooldown_days": 2, "min_cost_edge": 0.005,
    },
}

# ---------------------------------------------------------------------------
# 内置账户声明 spec：五套内置策略的账户层声明。
# **键序是契约**（分配层 ``account_order`` 与仪表盘行序都按声明顺序），
# 不得重排、不得插入新键改变既有顺序。
# ---------------------------------------------------------------------------
ACCOUNT_SPECS = {
    "tq_breakout": {
        "name": "短线日内做T",
        "mode": "intraday_t",
        "source_strategy": "one_to_two",
        "risk_profile": "breakout",
        "entry_model_name": "强势日内候选实时确认",
        # 盘中使用上一交易日完整收盘因子；0 会把正常隔夜数据误判为过期。
        "max_factor_lag": 1,
        "allowed_q": ("Q1", "Q2"),
        "default_style": "strong",
        "cycle_days": 5,
        "hold_min": 1,
        "hold_max": 8,
        # 高换手也不能靠几十只一手仓分散风险；只保留最强的少数标的。
        # 2026-08-24 集中化：3 席 × ~30% 权重，替代 5 席 × 15%。
        "max_positions": 3,
        "max_weight": 0.32,
        "max_exposure": 0.95,
        "hard_stop": -0.05,
        "trail_after": 0.04,
        "trail_stop": 0.05,
        "take_profit": [(0.08, 0.50)],
        "min_t_score": 0.76,
        "gap_q1": (-0.015, 0.035),
        # 盘中追高上限：现价较今日开盘的溢价上限。做T策略允许动量，
        # 但 5% 以上的日内拉升不再追。
        "max_open_runup_pct": 0.05,
        # 5% 以上不是当然不可交易：仅在盘中确认强势时允许小仓试错，
        # 但仍不模拟涨停板排队成交。
        "gap_q2": (-0.03, 0.07),
    },
    "trend_pullback": {
        "name": "趋势波段优选",
        "mode": "swing",
        "source_strategy": "bottom_reversal",
        "risk_profile": "trend",
        "entry_model_name": "趋势回踩结构确认",
        "max_factor_lag": 2,
        "allowed_q": ("Q1", "Q2", "Q3"),
        "default_style": "pullback",
        "cycle_days": 10,
        "hold_min": 3,
        "hold_max": 10,
        # 波段策略以结构质量为主；集中化后 3 席保证单票有意义的仓位。
        "max_positions": 3,
        "max_weight": 0.34,
        "max_exposure": 0.95,
        "hard_stop": -0.04,
        "trail_after": 0.05,
        "trail_stop": 0.06,
        "take_profit": [(0.07, 1 / 3), (0.12, 1 / 3)],
        "min_t_score": 0.72,
        "gap_q1": (-0.015, 0.025),
        # 回踩策略只在开盘价附近/回踩位入场，严禁追日内拉升。
        "max_open_runup_pct": 0.015,
        "gap_q2": (-0.025, 0.04),
    },
    "sector_rotation": {
        "name": "板块轮动先锋",
        "mode": "swing",
        "source_strategy": "sentiment_pioneer",
        "risk_profile": "sector",
        "entry_model_name": "热点板块相对强度",
        "max_factor_lag": 1,
        "allowed_q": ("Q1", "Q2"),
        "default_style": "sector",
        "cycle_days": 5,
        "hold_min": 2,
        "hold_max": 7,
        # 板块轮动保留跨板块比较空间，但不再铺成大量试探仓。
        "max_positions": 3,
        "max_weight": 0.32,
        "max_exposure": 0.92,
        "hard_stop": -0.045,
        "trail_after": 0.045,
        "trail_stop": 0.055,
        "take_profit": [(0.06, 1 / 3), (0.10, 1 / 3)],
        "min_t_score": 0.74,
        "gap_q1": (-0.015, 0.03),
        # Hot-lane candidates are discovered from live sector/concept flow;
        # allow a little more room to enter before the move is considered
        # exhausted, while the separate position scale keeps risk bounded.
        "max_open_runup_pct": 0.04,
        "gap_q2": (-0.025, 0.06),
    },
    NEW_STRATEGY_ID: {
        "name": "三日策略",
        "mode": "swing",
        "source_strategy": NEW_STRATEGY_ID,
        "risk_profile": "core_quality",
        "strategy_version": NEW_STRATEGY_VERSION,
        "entry_model_name": "已披露财报质量与突破确认",
        "max_factor_lag": 2,
        "allowed_q": ("Q1", "Q2"),
        "default_style": "quality",
        "cycle_days": 12,
        "hold_min": 2,
        "hold_max": 12,
        "max_positions": 3,
        "max_weight": 0.32,
        "max_exposure": 0.90,
        "hard_stop": -0.055,
        "trail_after": 0.045,
        "trail_stop": 0.060,
        "take_profit": [(0.085, 0.40), (0.15, 0.35)],
        "min_t_score": 0.74,
        "gap_q1": (-0.02, 0.03),
        "max_open_runup_pct": 0.02,
        "gap_q2": (-0.03, 0.055),
        "entry_pct_high": 6.5,
    },
    MAIN_FORCE_STRATEGY_ID: {
        "name": "超强主力股", "mode": "swing",
        "source_strategy": MAIN_FORCE_STRATEGY_ID, "risk_profile": "main_force",
        "strategy_version": MAIN_FORCE_STRATEGY_VERSION,
        "entry_model_name": "主力持续性与微观成交确认",
        "max_factor_lag": 1, "allowed_q": ("Q1", "Q2"),
        "default_style": "main_force", "cycle_days": 8,
        "hold_min": 1, "hold_max": 8, "max_positions": 3,
        "max_weight": 0.34, "max_exposure": 0.95,
        "hard_stop": -0.05, "trail_after": 0.05, "trail_stop": 0.06,
        "take_profit": [(0.10, 1 / 3), (0.16, 1 / 3)],
        "min_t_score": 0.76, "gap_q1": (-0.015, 0.04),
        "max_open_runup_pct": 0.035, "gap_q2": (-0.025, 0.07),
        "entry_pct_high": 8.8, "daily_candidate_limit": 10,
        "ignition_zone": (3.5, 7.5), "first_tranche_cap_pct": 0.12,
    },
}

# ---------------------------------------------------------------------------
# 保守回退声明：注册表里查不到运行时上下文的账户（未知 id / 极早期数据库 /
# 损坏定义）得到这一份 fail-closed 声明，而不是 KeyError 或静默映射到内置身份。
# ---------------------------------------------------------------------------
UNKNOWN_USER_SPEC = {
    "name": "未知策略账户", "source_strategy": "strategy_dsl", "selection_mode": "dsl",
    "mode": "swing", "cycle_days": 8, "max_positions": 1, "max_weight": 0.10,
    "max_exposure": 0.35, "risk_profile": "trend", "strategy_version": "v0",
    "default_style": "pullback", "entry_model_name": "未知策略账户",
    "max_factor_lag": 1, "entry_pct_high": 6.5, "gap_q2": (-0.025, 0.07),
    "hold_min": 1, "hold_max": 8, "hard_stop": -0.05, "trail_after": 0.05,
    "trail_stop": 0.06, "take_profit": [(0.10, 1 / 3), (0.16, 1 / 3)],
    "candidate_topn": 10, "lifecycle_stage": "quarantined",
}

# ---------------------------------------------------------------------------
# 只读访问器：唯一允许的读取口。全部返回独立副本，绝不把模块级可变对象
# 交给调用方；``_spec_for`` 的"内置分支"与"回退分支"都走这里。
# ---------------------------------------------------------------------------
def builtin_spec(account_id):
    """内置账户的声明 spec 副本；未声明返回 ``None``（不抛异常、不做映射）。"""
    spec = ACCOUNT_SPECS.get(account_id)
    return copy.deepcopy(spec) if spec is not None else None


def fallback_spec():
    """未知/不可解析账户的保守回退 spec 副本（fail-closed，绝不映射到内置身份）。"""
    return copy.deepcopy(UNKNOWN_USER_SPEC)


def builtin_account_ids():
    """内置账户 id，按**声明顺序**返回（顺序是分配层契约，不得重排）。"""
    return tuple(ACCOUNT_SPECS)


def has_style(style):
    """该风格键是否有声明（``set_account_style`` 的入参守卫）。"""
    return style in STYLE_PROFILES


def style_profile(style, *, default_style):
    """风格声明；与抽取前逐字一致——``default_style`` 未声明时同样抛 ``KeyError``。"""
    return copy.deepcopy(STYLE_PROFILES.get(style, STYLE_PROFILES[default_style]))


def style_name(style, fallback=None):
    """风格展示名；未声明时返回 ``fallback``（缺省回退到风格键本身）。"""
    name = (STYLE_PROFILES.get(style) or {}).get("name")
    if name is not None:
        return name
    return style if fallback is None else fallback


def risk_profile(key, *, default_key):
    """风险画像声明副本；与抽取前逐字一致——``default_key`` 未声明时抛 ``KeyError``。"""
    return copy.deepcopy(RISK_PROFILES.get(key, RISK_PROFILES[default_key]))


def default_risk_profile_key(account_id, fallback="trend"):
    """账户声明的风险画像键；账户未声明时返回 ``fallback``。"""
    spec = ACCOUNT_SPECS.get(account_id) or {}
    return spec.get("risk_profile", fallback)
