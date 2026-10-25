# -*- coding: utf-8 -*-
"""三策略正式名册：激进 / 稳健 / 保守（唯一声明源，纯计算）。

**它回答什么**：*下一周期应当由哪三个策略、各拿多少研究预算、以什么风险目标
运行；另外两个旧策略为什么退出独立账户；目标参数与当前代码声明差多少。*

**它不回答什么**（各有唯一 owner，本模块绝不复制其结论）：

- 当前周期谁能执行 → ``paper_cycle_ownership``（周期快照 ∩ 账本挂接）；
- 某版本当前生命周期状态 / 能否进正式周期 → ``strategy_lifecycle``；
- 账户声明表 / 风险画像 → ``paper_account_specs``（本模块只读比对，不改写）；
- 分配额度 / 资金权重 → ``paper_parameter_versions``（按周期的追加式声明）；
- 下单、撮合、止损、资金与持仓写入 → 执行层与风控层。

因此本模块是**声明 + 只读比对**：无数据库连接、无网络、无排序输出、无副作用，
import 期零调用。真正把五套切换成三套，需要 ``scripts/three_strategy_migration.py``
在指定账本上执行生命周期与权重声明；本模块只给出目标与差异。

设计边界：预算比例与风险目标是**待验证的试验起点**（见
``plans/three-strategy-redesign.md`` 第 4 节），不是历史寻优结果，也不是收益承诺。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

ROSTER_VERSION = "three-strategy-roster-v1"
BUDGET_POLICY_VERSION = "three-strategy-budget-v1"

AGGRESSIVE = "aggressive"
BALANCED = "balanced"
CONSERVATIVE = "conservative"


@dataclass(frozen=True, slots=True)
class StrategyRole:
    """一个正式策略的职责与目标上限（比例均相对该策略参考本金）。"""

    strategy_id: str
    role: str
    name: str
    objective: str
    budget_share: float
    within_budget_exposure: float
    within_budget_single: float
    max_positions: int
    single_trade_risk: float
    daily_loss_stop: float
    drawdown_stop: float
    intraday_t: bool
    ai_depth: str
    not_trading_when: str

    def to_dict(self):
        return asdict(self)

    def nav_limits(self):
        """把"预算内比例"换算成相对总参考本金的硬上限（便于总池约束）。"""
        return {
            "exposure_cap_nav": round(self.budget_share * self.within_budget_exposure, 6),
            "single_name_cap_nav": round(self.budget_share * self.within_budget_single, 6),
            "single_trade_risk_nav": round(self.budget_share * self.single_trade_risk, 6),
            "daily_loss_stop_nav": round(self.budget_share * self.daily_loss_stop, 6),
            "drawdown_stop_nav": round(self.budget_share * self.drawdown_stop, 6),
        }


ROLES = (
    StrategyRole(
        strategy_id="tq_breakout",
        role=AGGRESSIVE,
        name="激进做T",
        objective="用已有底仓的日内波动降低持仓成本；底仓涨跌另计",
        budget_share=0.20,
        within_budget_exposure=0.70,
        within_budget_single=0.25,
        max_positions=3,
        single_trade_risk=0.008,
        daily_loss_stop=0.020,
        drawdown_stop=0.060,
        intraday_t=True,
        ai_depth="机会排序、反证、高抛/回补区间建议、事后复盘",
        not_trading_when="没有可卖底仓、价差不足以覆盖双边成本、行情过期或资金与价格互相矛盾",
    ),
    StrategyRole(
        strategy_id="trend_pullback",
        role=BALANCED,
        name="稳健波段",
        objective="吃持续数日至数周的趋势；趋势确认后等回踩，不因单日涨幅第一而换股",
        budget_share=0.50,
        within_budget_exposure=0.65,
        within_budget_single=0.24,
        max_positions=3,
        single_trade_risk=0.006,
        daily_loss_stop=0.015,
        drawdown_stop=0.050,
        intraday_t=False,
        ai_depth="候选复核、趋势破坏与事件风险解释",
        not_trading_when="趋势未确认、结构已破坏或正在追高",
    ),
    StrategyRole(
        strategy_id="reported_profit_breakout",
        role=CONSERVATIVE,
        name="保守防守",
        objective="少犯错、低换手、多留现金；质量与估值先承担风险过滤职责",
        budget_share=0.30,
        within_budget_exposure=0.35,
        within_budget_single=0.12,
        max_positions=3,
        single_trade_risk=0.003,
        daily_loss_stop=0.010,
        drawdown_stop=0.030,
        intraday_t=False,
        ai_depth="财报异常与风险事件复核",
        not_trading_when="财务数据未在决策时点公开、缺关键字段、出现风险事件或没有合格便宜的标的",
    ),
)


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """一个不再作为独立资金账户的旧策略。"""

    strategy_id: str
    name: str
    reason: str
    target_state: str
    keeps: str

    def to_dict(self):
        return asdict(self)


EXIT_ONLY = (
    ExitDecision(
        strategy_id="sector_rotation",
        name="板块轮动先锋",
        reason="当前周期 −4.09%、日终最大回撤 6.11%；放量追热点的高分减低分收益为 −3.13 个百分点",
        target_state="retiring",
        keeps="板块强弱与概念扩散只作其他策略的背景确认，不再单独买入同一风险",
    ),
    ExitDecision(
        strategy_id="main_force_top10",
        name="超强主力股",
        reason="当前周期 −9.81%、日终最大回撤 9.81%；资金占比与资金成交强度曾被重复计分",
        target_state="retiring",
        keeps="资金流观察降级为三策略共享的辅助确认（main_force_confirmation），不单独开仓",
    ),
)

_EXIT_BY_ID = {item.strategy_id: item for item in EXIT_ONLY}
_ROLE_BY_ID = {item.strategy_id: item for item in ROLES}


def formal_roles():
    """三个正式策略的职责声明（元组，顺序即展示与分配顺序）。"""
    return ROLES


def formal_strategy_ids():
    return tuple(item.strategy_id for item in ROLES)


def exit_only_ids():
    return tuple(item.strategy_id for item in EXIT_ONLY)


def role_for(strategy_id):
    """该 id 的正式职责；不在名册内（含用户策略）返回 ``None``。"""
    return _ROLE_BY_ID.get(str(strategy_id or ""))


def display_name(strategy_id, fallback=None):
    """展示名：正式策略用名册里的名字，其余用调用方给的兜底值。

    注册表里的名字属于不可变版本快照（改名要建新版本），所以展示层统一从名册
    取名字，避免"仪表盘叫激进做T、自进化页却还叫短线日内做T"。
    """
    role = role_for(strategy_id)
    if role is not None:
        return role.name
    if fallback:
        return str(fallback)
    return str(strategy_id or "")


def exit_decision(strategy_id):
    return _EXIT_BY_ID.get(str(strategy_id or ""))


def is_formal(strategy_id):
    return role_for(strategy_id) is not None


def is_exit_only(strategy_id):
    """旧策略是否已退出独立资金账户（只退出，不搬移持仓与成本）。"""
    return exit_decision(strategy_id) is not None


def entry_block_reason(strategy_id):
    """新开仓的拒绝理由；正式策略返回 ``None``。

    仅用于**新开仓**：退出独立策略仍须保留保护性卖出与旧版风控。真正的执行
    资格仍由 ``paper_cycle_ownership`` 按生命周期判定，本函数只是名册视角的
    说明性口径，不替代它。
    """
    decision = exit_decision(strategy_id)
    if decision is None:
        return None
    return f"{decision.name}已退出独立资金账户，只允许退出与保护性卖出：{decision.keeps}"


def budget_shares():
    """策略 → 研究预算占比（合计 1.0）。"""
    return {item.strategy_id: item.budget_share for item in ROLES}


def pool_budget_example(total_capital):
    """按给定总参考本金展示三策略预算与最高持仓（纯展示，不分配现金）。"""
    total = float(total_capital)
    rows = []
    for item in ROLES:
        budget = total * item.budget_share
        rows.append({
            "strategy_id": item.strategy_id, "name": item.name,
            "budget": round(budget, 2),
            "max_holding": round(budget * item.within_budget_exposure, 2),
            "single_name_cap": round(budget * item.within_budget_single, 2),
            "max_positions": item.max_positions,
        })
    return {
        "total_capital": total,
        "strategies": rows,
        "max_holding_total": round(
            sum(row["max_holding"] for row in rows), 2),
        "note": "预算展示，不是新增现金；不得把已亏损资金重置回总参考本金。",
    }


def allocation_declaration(strategy_id, *, effective_date, reason=BUDGET_POLICY_VERSION):
    """写入 ``paper_parameter_versions.params.adaptive_allocation`` 的追加式声明。

    权重来自 ``budget_shares()``，单位为百分比；本函数不写库，只产出载荷。
    """
    role = role_for(strategy_id)
    if role is None:
        raise ValueError("strategy_not_in_roster")
    day = str(effective_date or "")[:10]
    if len(day) != 10:
        raise ValueError("explicit_effective_date_required")
    return {
        "style": "three-strategy-budget",
        "reason": reason,
        "effective_date": day,
        "params": {
            "adaptive_allocation": {
                "status": "active",
                "effective_date": day,
                "weight_pct": round(role.budget_share * 100, 2),
                "roster_version": ROSTER_VERSION,
                "role": role.role,
                "declared_policy": BUDGET_POLICY_VERSION,
            },
        },
    }


def initial_params(strategy_id, *, effective_date):
    """新周期开局时写进 ``paper_accounts.params`` 的初始预算声明。

    这是"初始 20/50/30"的唯一落地口径：每个新周期开始时按名册重置一次，
    之后的调整交给自进化通道（``paper_dynamic_allocation`` 的每日小步与
    ``evolution_apply`` 的显式提案），两条通道都只在既有边界内改
    ``weight_pct``，本函数不参与后续覆盖。

    非名册账户（用户策略、已退出策略）返回空表：不得凭空给它们造预算。
    """
    if role_for(strategy_id) is None:
        return {}
    declaration = allocation_declaration(strategy_id, effective_date=effective_date)
    return declaration["params"]


def factor_policy():
    """因子取舍：优先试验 / 降低依赖 / 不据此调权（依据见方案第 3 节）。"""
    return {
        "prioritize": (
            "资金持续性确认（flow 1 日 IC +0.043；5 日 +0.148 仅作线索）",
            "趋势成立后的回踩确认（rsi 5 日 IC +0.042、高低组差 +0.83 个百分点）",
            "估值辅助排序（value 5 日 IC +0.064、覆盖 75.42%，缺失不填零）",
        ),
        "reduce": (
            "短期动量（mom_short 1 日 IC −0.122、5 日 −0.172）：不再主导底仓加分",
            "异常放量（volsurge 轮动 5 日 IC −0.252）：改为流动性/异常风险检查",
            "热点追涨：不移植到新的三策略",
        ),
        "not_admitted": (
            "新闻人气与 AI 主观信心：样本缺失或无法回放，不进入收益排序",
            "身份不全或有效日期不足的分组：保留研究，不转成权重",
        ),
        "boundary": "全部因子最多只有 13 个有效日期；结论是方向性线索，不构成验证或收益承诺。",
    }


_TARGET_SPEC_FIELDS = ("max_positions", "max_weight", "max_exposure")

#: 名册目标与代码声明之间**故意保留**的差异，逐条写明为什么不动。
#:
#: 调研结论（2026-10-25）：账户声明里有一批字段根本没有执行消费者，另有一批
#: 字段的口径与方案假设不同（方案按"策略参考本金"，代码按"共享池 NAV"），还有
#: 一批改了会立刻影响现有持仓的退出。把这些差异混进"待办"里，等于假装改一个
#: 数字就能得到方案里的风险画像——所以单独标注，并在报告里解释。
DELIBERATE_REASONS = {
    "max_weight": (
        "ACCOUNT_SPECS.max_weight 没有任何执行路径读取（只写进审计列/页面展示）；"
        "真正的单票上限在 RISK_PROFILES.max_weight，按共享池 NAV 计"),
    "max_exposure": (
        "ACCOUNT_SPECS.max_exposure 同上；且 RISK_PROFILES.max_exposure 会参与席位权重，"
        "改动可能触发现有持仓的容量性减仓，需单独一轮验证"),
    "risk_profile.single_risk": (
        "方案按“策略参考本金”给百分比，代码按共享池 NAV 换算；直接套用会把仓位砍到"
        "不足四分之一。本轮只把保守从 1.2% 降到 0.6% 观察"),
    "risk_profile.daily_loss": (
        "全池熔断：数值相对共享 NAV，任一策略触发即对所有策略生效；单策略日内止损未实现"),
    "risk_profile.drawdown": (
        "全池熔断并写入全池 cooldown_until，改它等于同时冻结三个策略；单策略回撤止损未实现"),
}


def spec_drift(account_specs, risk_profiles):
    """目标与当前代码声明的差异，逐条标注 ``open`` / ``deliberate``。

    只读比对，不修改传入的声明表。``max_weight``/``max_exposure`` 在代码里是
    相对本策略参考本金的比例，与名册的"预算内比例"同口径；资金占比
    （``budget_share``）不在账户声明里，由按周期的分配权重声明承载。
    ``deliberate`` 表示"调研后决定不动"，原因见 :data:`DELIBERATE_REASONS`。
    """
    rows = []
    for role in ROLES:
        spec = dict((account_specs or {}).get(role.strategy_id) or {})
        if not spec:
            rows.append({"strategy_id": role.strategy_id, "field": "spec", "expected": "declared",
                         "actual": "missing", "status": "open", "reason": ""})
            continue
        expected = {
            "max_positions": role.max_positions,
            "max_weight": role.within_budget_single,
            "max_exposure": role.within_budget_exposure,
        }
        for field in _TARGET_SPEC_FIELDS:
            actual = spec.get(field)
            if isinstance(actual, (int, float)) and isinstance(expected[field], (int, float)):
                if abs(float(actual) - float(expected[field])) <= 1e-9:
                    continue
            elif actual == expected[field]:
                continue
            rows.append({"strategy_id": role.strategy_id, "field": field,
                         "expected": expected[field], "actual": actual,
                         "status": "deliberate" if field in DELIBERATE_REASONS else "open",
                         "reason": DELIBERATE_REASONS.get(field, "")})
        profile_key = spec.get("risk_profile")
        profile = dict((risk_profiles or {}).get(profile_key) or {})
        for field, value in (("single_risk", role.single_trade_risk),
                             ("daily_loss", role.daily_loss_stop),
                             ("drawdown", role.drawdown_stop)):
            actual = profile.get(field)
            if isinstance(actual, (int, float)) and abs(float(actual) - value) <= 1e-9:
                continue
            key = f"risk_profile.{field}"
            rows.append({"strategy_id": role.strategy_id, "field": key,
                         "expected": value, "actual": actual,
                         "status": "deliberate" if key in DELIBERATE_REASONS else "open",
                         "reason": DELIBERATE_REASONS.get(key, "")})
    return rows


__all__ = [
    "ROSTER_VERSION", "BUDGET_POLICY_VERSION", "AGGRESSIVE", "BALANCED", "CONSERVATIVE",
    "StrategyRole", "ExitDecision", "ROLES", "EXIT_ONLY", "DELIBERATE_REASONS",
    "formal_roles", "formal_strategy_ids", "exit_only_ids", "role_for", "exit_decision",
    "is_formal", "is_exit_only", "entry_block_reason", "budget_shares", "pool_budget_example",
    "allocation_declaration", "initial_params", "factor_policy", "spec_drift", "display_name",
]
