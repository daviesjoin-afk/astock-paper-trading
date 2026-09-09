# -*- coding: utf-8 -*-
"""模拟盘共享资金与席位分配引擎（PR-07，N 策略版）。

设计
----
- 输入是任意 N 个 :class:`StrategyRuntime`：每个策略用一份声明式运行时
  描述自己在分配层的差异（席位上限、优先级地板、自身敞口上限、六个
  影响有效权重的因子）。引擎内部**不出现任何策略身份比较**——新增、
  下线或调整策略都只是增删运行时条目，不改分配代码。
- ``effective_weight = base_priority × regime_fit × confidence ×
  health × data_quality × diversification``，六个因子各自夹在 [0, 1]。
  调用方暂时没有实时信号时全部传中性值 1.0，结果与旧权重口径等价；
  :func:`diversification_factor` 提供按当前敞口集中度计算分散化系数的
  统一口径，供有敞口数据的调用方使用。
- 不变式（property tests 覆盖）：任何 N ≥ 0 的输入下，
  ``Σ allocated ≤ shared pool cap`` 恒成立——席位分配满足
  ``Σ limits ≤ min(hard_pool_cap, Σ max_positions)``，资金分配的
  ``allowance`` 永远不超过 ``pool_cap − 已占用 − 在途预占``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "ALLOCATION_ENGINE_VERSION",
    "DEFAULT_STAGE_CAPITAL_SCALE",
    "FACTOR_FIELDS",
    "LIFECYCLE_STAGES",
    "StrategyRuntime",
    "allocation_plan",
    "deployable_budget",
    "diversification_factor",
    "effective_weight",
    "effective_weights",
    "minimum_deployable_budget",
    "pool_headroom",
    "position_limits",
    "stage_capital_scale",
    "strategy_pool_budget",
]

ALLOCATION_ENGINE_VERSION = "allocation-engine-v2"

# 生命周期（PR-08）：冷启动 → 试点 → 标准 → 成熟；隔离策略不参与实盘分配。
# 每个阶段一个资金系数（相对其可分配预算的真实部署比例）。
LIFECYCLE_STAGES = ("shadow", "pilot", "standard", "mature", "quarantined")
DEFAULT_STAGE_CAPITAL_SCALE = {
    "shadow": 0.0,        # 影子运行：只记录意图，不动真金白银
    "pilot": 0.25,        # 试点：小额真实资金验证
    "standard": 1.0,      # 标准：全额部署
    "mature": 1.0,        # 成熟：全额部署（规模上限由账户配置决定）
    "quarantined": 0.0,   # 隔离：停止新开仓，等待人工处理
}

# 有效权重的六个因子；全部夹在 [0, 1]，缺省中性 1.0。
FACTOR_FIELDS = (
    "base_priority",
    "regime_fit",
    "confidence",
    "health",
    "data_quality",
    "diversification",
)


def _unit(value: Any, default: float = 1.0) -> float:
    """把因子夹到 [0, 1]；非法输入回到中性值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(0.0, min(1.0, number))


@dataclass(frozen=True)
class StrategyRuntime:
    """一个策略在分配层的声明式运行时。

    身份差异全部落在这张数据结构里（谁有几席、谁有地板、谁的占用受
    自身净值约束），分配函数只读字段、不比较策略 ID。
    """

    strategy_id: str
    base_priority: float = 1.0
    regime_fit: float = 1.0
    confidence: float = 1.0
    health: float = 1.0
    data_quality: float = 1.0
    diversification: float = 1.0
    # 分配层席位上限；None 时由引擎回退到全局 strategy_max_positions。
    max_positions: int | None = None
    min_positions: int | None = None
    # 该策略的资金地板（占净值比例）；None 表示不设地板。
    priority_floor_pct: float | None = None
    # 该策略总占用不得超过净值的该比例（如“不得超过自身净值”）；None 表示不限。
    own_exposure_cap_pct: float | None = None
    # 生命周期阶段（PR-08）：shadow/pilot/standard/mature/quarantined。
    lifecycle_stage: str = "standard"
    # 显式资金系数；None 时使用生命周期阶段的默认系数。
    capital_scale: float | None = None

    def effective_weight(self) -> float:
        product = 1.0
        for name in FACTOR_FIELDS:
            product *= _unit(getattr(self, name))
        return product


def effective_weight(runtime: StrategyRuntime) -> float:
    """单一策略的有效权重：六个因子的乘积（各因子已夹到 [0, 1]）。"""
    return runtime.effective_weight()


def effective_weights(runtimes: Sequence[StrategyRuntime]) -> dict[str, float]:
    """全部策略的有效权重（保留输入顺序；重复 ID 以第一个为准）。"""
    weights: dict[str, float] = {}
    for runtime in runtimes or ():
        key = str(runtime.strategy_id or "")
        if key and key not in weights:
            weights[key] = runtime.effective_weight()
    return weights


def diversification_factor(
    *,
    exposure_share: float,
    fair_share: float,
    floor: float = 0.5,
) -> float:
    """分散化系数：当前敞口占比超过公允份额时按比例降权。

    - ``exposure_share``：该策略当前占用 / 共享池已占用；
    - ``fair_share``：该策略有效权重 / 全部有效权重；
    - 占比不超过公允份额时返回 1.0（不加权）；超得越多越接近 ``floor``。
    """
    fair = max(float(fair_share or 0.0), 0.0)
    exposure = max(float(exposure_share or 0.0), 0.0)
    if fair <= 0.0 or exposure <= fair:
        return 1.0
    bounded_floor = max(0.0, min(1.0, float(floor)))
    return max(bounded_floor, min(1.0, fair / exposure))


def pool_headroom(
    *,
    pool_cap_amount: float,
    pool_value: float,
    pending_total: float,
) -> float:
    """共享池剩余可分配额度（永不为负）。"""
    cap = max(float(pool_cap_amount or 0.0), 0.0)
    committed = max(float(pool_value or 0.0), 0.0) + max(float(pending_total or 0.0), 0.0)
    return max(0.0, cap - committed)


def _runtime_map(runtimes: Sequence[StrategyRuntime]) -> dict[str, StrategyRuntime]:
    runtimes = list(runtimes or ())
    result: dict[str, StrategyRuntime] = {}
    for runtime in runtimes:
        key = str(runtime.strategy_id or "")
        if key:
            result.setdefault(key, runtime)
    return result


def position_limits(
    runtimes: Sequence[StrategyRuntime],
    *,
    hard_pool_cap,
    strategy_max_positions,
    strategy_min_positions,
    protected_slot_floor,
    account_order=None,
    baseline_exposure=None,
) -> dict[str, Any]:
    """按有效权重在总席位硬上限内分配各策略席位（任意 N 个策略）。

    不变式：``Σ limits ≤ total_cap ≤ min(hard_pool_cap, Σ max_positions)``，
    且 ``Σ limits ≥ 0``；函数返回前再做一次最终钳制，保证无论输入如何
    都不会越过共享池硬上限。
    """
    runtime_map = _runtime_map(runtimes)
    ids = list(runtime_map)
    count = len(ids)
    if count == 0:
        return {
            "engine": ALLOCATION_ENGINE_VERSION,
            "risk_scale": 1.0,
            "protected_slot_floor": 0,
            "total_cap": 0,
            "limits": {},
            "effective_weights": {},
        }
    order = {key: int(value) for key, value in (account_order or {}).items()}
    weights = {key: runtime_map[key].effective_weight() for key in ids}
    caps = {
        key: max(
            1,
            int(
                runtime_map[key].max_positions
                if runtime_map[key].max_positions is not None
                else strategy_max_positions
            ),
        )
        for key in ids
    }
    mins = {
        key: max(
            0,
            int(
                runtime_map[key].min_positions
                if runtime_map[key].min_positions is not None
                else strategy_min_positions
            ),
        )
        for key in ids
    }
    hard_cap = min(int(hard_pool_cap), sum(caps.values()))
    if baseline_exposure is None:
        baseline = sum(weights.values()) / count
    else:
        baseline = float(baseline_exposure)
    current = sum(weights.values()) / count
    risk_scale = max(0.60, min(1.0, current / max(baseline, 0.01)))
    base_floor_total = min(hard_cap, strategy_min_positions * count)
    total_cap = max(base_floor_total, min(hard_cap, int(round(hard_cap * risk_scale))))
    total_cap = min(total_cap, hard_cap)
    protected = (
        min(int(protected_slot_floor), strategy_max_positions)
        if total_cap >= int(protected_slot_floor) * count
        else strategy_min_positions
    )
    # 单策略下限受“它自己的上限”与“总席位能摊到的份额”双重约束。
    minimum = {
        key: min(mins[key], caps[key], total_cap // count if count else 0)
        for key in ids
    }
    # 全局下限之和不得超过总席位，否则先压缩各策略下限。
    while sum(minimum.values()) > total_cap and any(minimum[key] > 0 for key in ids):
        heaviest = max(ids, key=lambda item: (minimum[item], -order.get(item, 99)))
        minimum[heaviest] -= 1
    weight_total = sum(weights.values()) or 1.0
    raw = {key: total_cap * weights[key] / weight_total for key in ids}
    limits = {
        key: max(minimum[key], min(caps[key], int(raw[key])))
        for key in ids
    }
    while sum(limits.values()) < total_cap:
        candidates = [key for key in ids if limits[key] < caps[key]]
        if not candidates:
            break
        key = max(
            candidates,
            key=lambda item: (
                raw[item] - limits[item], weights[item], -order.get(item, 99)
            ),
        )
        limits[key] += 1
    while sum(limits.values()) > total_cap:
        candidates = [key for key in ids if limits[key] > minimum[key]]
        if not candidates:
            break
        key = max(
            candidates,
            key=lambda item: (
                limits[item] - raw[item], -weights[item], order.get(item, 99)
            ),
        )
        limits[key] -= 1
    # 最终钳制：无论上游输入如何，都不允许越过共享池硬上限。
    while sum(limits.values()) > hard_cap:
        removable = [key for key in ids if limits[key] > minimum[key]]
        if not removable:
            removable = [key for key in ids if limits[key] > 0]
            if not removable:
                break
        key = max(removable, key=lambda item: (limits[item], order.get(item, 99)))
        limits[key] -= 1
    return {
        "engine": ALLOCATION_ENGINE_VERSION,
        "risk_scale": risk_scale,
        "protected_slot_floor": protected,
        "total_cap": total_cap,
        "limits": limits,
        "effective_weights": {key: round(value, 6) for key, value in weights.items()},
    }


def strategy_pool_budget(
    runtimes: Sequence[StrategyRuntime],
    *,
    account_id,
    values,
    weights=None,
    pending_by_account,
    pending_total,
    nav,
    market_scales=None,
    shared_pool_max_exposure,
    strategy_pool_floor_ratio,
) -> dict[str, Any]:
    """计算共享资金池中单一策略的目标、地板和可追加额度（任意 N 个策略）。

    不变式：``current_total + allowance ≤ min(pool_cap_amount, 自身敞口上限)``，
    且 ``allowance ≤ pool_headroom``——无论 N 是多少，聚合占用都不会越过
    共享池硬上限。
    """
    runtime_map = _runtime_map(runtimes)
    runtime = runtime_map.get(str(account_id or ""))
    eff_weights = effective_weights(runtimes)
    if weights is not None:
        # 兼容显式传入权重的调用方：只覆盖运行时里出现的策略。
        for key, value in (weights or {}).items():
            if key in eff_weights:
                eff_weights[key] = _unit(value, 0.0)
    nav = max(float(nav or 0.0), 0.0)
    pool_cap_amount = nav * shared_pool_max_exposure
    pool_value = sum(values.values())
    global_remaining = pool_headroom(
        pool_cap_amount=pool_cap_amount,
        pool_value=pool_value,
        pending_total=pending_total,
    )
    weight_total = sum(eff_weights.values()) or 1.0
    base_target_pct = {
        key: shared_pool_max_exposure * weight / weight_total
        for key, weight in eff_weights.items()
    }
    scales = market_scales
    target_pct = {
        key: value * (scales.get(key, 0.0) if scales is not None else 1.0)
        for key, value in base_target_pct.items()
    }
    floor_pct = {key: value * strategy_pool_floor_ratio for key, value in target_pct.items()}
    priority_floor_pct = (
        runtime.priority_floor_pct if runtime is not None else None
    )
    priority_floor_amount = (
        nav * priority_floor_pct if priority_floor_pct is not None else 0.0
    )
    if priority_floor_pct is not None and priority_floor_pct > 0.0 and nav > 0:
        target_pct[account_id] = max(
            target_pct.get(account_id, 0.0), priority_floor_pct
        )
        floor_pct[account_id] = max(
            floor_pct.get(account_id, 0.0), priority_floor_pct
        )
    current_amount = values.get(account_id, 0.0)
    pending_strategy_amount = pending_by_account.get(account_id, 0.0)
    current_total_amount = current_amount + pending_strategy_amount
    target_amount = nav * target_pct.get(account_id, 0.0)
    floor_amount = nav * floor_pct.get(account_id, 0.0)
    own_headroom = max(0.0, target_amount - current_total_amount)
    other_floor_reserve = sum(
        max(
            0.0,
            nav * floor_pct.get(key, 0.0)
            - values.get(key, 0.0)
            - pending_by_account.get(key, 0.0),
        )
        for key in values
        if key != account_id
    )
    after_floor = max(0.0, global_remaining - other_floor_reserve)
    other_floors_met = all(
        values.get(key, 0.0) + pending_by_account.get(key, 0.0) + 1e-6
        >= nav * floor_pct.get(key, 0.0)
        for key in values
        if key != account_id
    )
    redistribution = max(0.0, after_floor - own_headroom) if other_floors_met else 0.0
    allowance = min(global_remaining, after_floor, own_headroom + redistribution)
    # fail-closed：不在运行时表里的策略不参与资金分配（新增策略必须先注册）。
    unknown_strategy = runtime is None
    if unknown_strategy:
        allowance = 0.0
    # 声明式自身敞口上限（替代按策略 ID 的特判）：该策略总占用不得超过净值 × cap。
    own_exposure_cap = (
        runtime.own_exposure_cap_pct if runtime is not None else None
    )
    own_exposure_cap_amount = (
        max(0.0, nav * own_exposure_cap - current_total_amount)
        if own_exposure_cap is not None and nav > 0
        else None
    )
    if own_exposure_cap_amount is not None:
        allowance = min(allowance, own_exposure_cap_amount)
    # 最终钳制：任何情况下都不越过共享池硬上限。
    allowance = min(max(0.0, allowance), global_remaining)
    absolute_cap = current_total_amount + allowance
    round2 = lambda value: round(value, 2)
    return {
        "engine": ALLOCATION_ENGINE_VERSION,
        "account_id": account_id,
        "effective_weights": {key: round(value, 6) for key, value in eff_weights.items()},
        "target_pct": round(target_pct.get(account_id, 0.0) * 100, 2),
        "base_target_pct": round(base_target_pct.get(account_id, 0.0) * 100, 2),
        "market_scale_pct": round((scales.get(account_id, 0.0) if scales is not None else 1.0) * 100, 1),
        "market_scale_applied": bool(scales is not None),
        "floor_pct": round(floor_pct.get(account_id, 0.0) * 100, 2),
        "priority_floor_pct": round(priority_floor_pct * 100, 2) if priority_floor_pct is not None and priority_floor_pct > 0.0 else None,
        "priority_floor_amount": round2(priority_floor_amount),
        "own_exposure_cap_pct": round(own_exposure_cap * 100, 2) if own_exposure_cap is not None else None,
        "current_pct": round(current_amount / nav * 100, 2) if nav else 0.0,
        "target_amount": round2(target_amount),
        "floor_amount": round2(floor_amount),
        "current_amount": round2(current_amount),
        "pending_reserve_amount": round2(pending_strategy_amount),
        "current_total_amount": round2(current_total_amount),
        "allowance_amount": round2(max(0.0, allowance)),
        "absolute_cap_amount": round2(max(0.0, absolute_cap)),
        "global_remaining_amount": round2(global_remaining),
        "other_floor_reserve": round2(other_floor_reserve),
        "redistribution_amount": round2(redistribution),
        "redistribution_allowed": bool(redistribution > 0.0 and not unknown_strategy),
        "unknown_strategy": bool(unknown_strategy),
        "pool_value": round2(pool_value),
        "pool_cap_amount": round2(pool_cap_amount),
        "pool_exposure_pct": round(pool_value / nav * 100, 2) if nav else 0.0,
        "pending_pool_reserve_amount": round2(pending_total),
        "pool_committed_amount": round2(pool_value + pending_total),
        "pool_committed_pct": round((pool_value + pending_total) / nav * 100, 2) if nav else 0.0,
        "pool_available_amount": round2(global_remaining),
        "pool_limit_pct": round(shared_pool_max_exposure * 100, 2),
        "other_floors_met": bool(other_floors_met),
    }


def stage_capital_scale(runtime: StrategyRuntime) -> tuple[float, str]:
    """返回该运行时的（资金系数, 归一化阶段）。

    - 显式 ``capital_scale`` 优先（夹到 [0, 1]）；
    - 否则使用生命周期阶段的默认系数；
    - 未知阶段 fail-closed 按 quarantined 处理（系数 0）。
    """
    if runtime.capital_scale is not None:
        return _unit(runtime.capital_scale, 0.0), str(runtime.lifecycle_stage)
    stage = str(runtime.lifecycle_stage or "")
    if stage in DEFAULT_STAGE_CAPITAL_SCALE:
        return DEFAULT_STAGE_CAPITAL_SCALE[stage], stage
    return DEFAULT_STAGE_CAPITAL_SCALE["quarantined"], "quarantined"


def minimum_deployable_budget(
    price,
    *,
    lot_size: int = 100,
    price_buffer: float = 1.0,
) -> float:
    """买入一手所需的最小预算（含价格缓冲，默认不留缓冲）。"""
    try:
        usable_price = max(float(price or 0.0), 0.0) * max(float(price_buffer), 1.0)
    except (TypeError, ValueError):
        return 0.0
    lot_size = max(int(lot_size), 1)
    return usable_price * lot_size


def deployable_budget(
    *,
    budget_amount,
    price,
    lot_size: int = 100,
    capital_scale: float = 1.0,
    price_buffer: float = 1.0,
    lifecycle_stage: str | None = None,
) -> dict[str, Any]:
    """把预算折算成整手可部署资金。

    不变式（PR-08）：预算不足一手时**绝不生成碎片订单**——
    ``lots == 0`` 且全部预算进入 ``waiting_capital``，不允许出现
    ``0 < deployable < 一手成本`` 的碎片。
    """
    round2 = lambda value: round(value, 2)
    scaled = max(float(budget_amount or 0.0), 0.0) * _unit(capital_scale, 0.0)
    try:
        usable_price = max(float(price or 0.0), 0.0) * max(float(price_buffer), 1.0)
    except (TypeError, ValueError):
        usable_price = 0.0
    lot_size = max(int(lot_size), 1)
    one_lot_cost = usable_price * lot_size
    if one_lot_cost <= 0.0 or scaled < one_lot_cost:
        stage = str(lifecycle_stage or "").strip()
        if _unit(capital_scale, 0.0) <= 0.0 and stage:
            # PR-26：shadow/quarantined 阶段系数 0，预算整笔进等待池——
            # 原因必须写明是生命周期，而不是含糊的“预算不足一手”。
            reason = f"生命周期阶段 {stage}：不部署新资金，预算进入等待池"
        else:
            reason = (
                "价格无效，预算冻结等待"
                if one_lot_cost <= 0.0
                else "预算不足一手，资金进入等待池"
            )
        return {
            "allowed": False,
            "lots": 0,
            "deployable_amount": 0.0,
            "waiting_capital": round2(scaled),
            "scaled_budget": round2(scaled),
            "one_lot_cost": round2(one_lot_cost),
            "reason": reason,
        }
    lots = int(scaled // one_lot_cost)
    deployable = lots * usable_price * lot_size
    return {
        "allowed": True,
        "lots": lots,
        "deployable_amount": round2(deployable),
        "waiting_capital": round2(max(0.0, scaled - deployable)),
        "scaled_budget": round2(scaled),
        "one_lot_cost": round2(one_lot_cost),
        "reason": None,
    }


def allocation_plan(
    runtimes: Sequence[StrategyRuntime],
    *,
    nav,
    values,
    pending_by_account,
    pending_total,
    prices_by_strategy,
    shared_pool_max_exposure,
    strategy_pool_floor_ratio,
    lot_size: int = 100,
    account_order=None,
    market_scales=None,
) -> dict[str, Any]:
    """整池资金分配计划（PR-08）：预算 → 生命周期缩放 → 整手部署。

    不变式：按有效权重从高到低依次消耗共享池余量，
    ``Σ deployable ≤ pool_headroom`` 恒成立；任何一步不足一手都把剩余
    预算放进 ``waiting_capital``，绝不产生碎片订单。
    """
    runtime_map = _runtime_map(runtimes)
    nav_value = max(float(nav or 0.0), 0.0)
    headroom = pool_headroom(
        pool_cap_amount=nav_value * shared_pool_max_exposure,
        pool_value=sum(values.values()),
        pending_total=pending_total,
    )
    order = {key: int(value) for key, value in (account_order or {}).items()}
    ordered = sorted(
        runtime_map.values(),
        key=lambda item: (
            -item.effective_weight(),
            order.get(item.strategy_id, 99),
            item.strategy_id,
        ),
    )
    rows: list[dict[str, Any]] = []
    remaining_headroom = headroom
    total_deployable = 0.0
    total_waiting = 0.0
    for runtime in ordered:
        budget = strategy_pool_budget(
            runtimes,
            account_id=runtime.strategy_id,
            values=values,
            pending_by_account=pending_by_account,
            pending_total=pending_total,
            nav=nav,
            market_scales=market_scales,
            shared_pool_max_exposure=shared_pool_max_exposure,
            strategy_pool_floor_ratio=strategy_pool_floor_ratio,
        )
        raw_allowance = max(0.0, float(budget.get("allowance_amount") or 0.0))
        budget_amount = min(raw_allowance, remaining_headroom)
        scale, stage = stage_capital_scale(runtime)
        deployment = deployable_budget(
            budget_amount=budget_amount,
            price=(prices_by_strategy or {}).get(runtime.strategy_id),
            lot_size=lot_size,
            capital_scale=scale,
            lifecycle_stage=stage,
        )
        deployable = float(deployment["deployable_amount"])
        remaining_headroom = max(0.0, remaining_headroom - deployable)
        total_deployable += deployable
        total_waiting += float(deployment["waiting_capital"])
        rows.append({
            "strategy_id": runtime.strategy_id,
            "lifecycle_stage": stage,
            "capital_scale": round(scale, 4),
            "raw_allowance_amount": round(raw_allowance, 2),
            "budget_amount": round(budget_amount, 2),
            "scaled_budget_amount": deployment["scaled_budget"],
            "lots": deployment["lots"],
            "deployable_amount": deployment["deployable_amount"],
            "waiting_capital": deployment["waiting_capital"],
            "blocked_reason": deployment["reason"],
            "allowed": bool(deployment["allowed"]),
        })
    return {
        "engine": ALLOCATION_ENGINE_VERSION,
        "plan": rows,
        "total_deployable_amount": round(total_deployable, 2),
        "total_waiting_capital": round(total_waiting, 2),
        "pool_headroom_amount": round(headroom, 2),
        "lot_size": max(int(lot_size), 1),
    }
