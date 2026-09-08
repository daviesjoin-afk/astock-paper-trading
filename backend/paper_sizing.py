# -*- coding: utf-8 -*-
"""模拟盘下单股数与约束解释的纯计算。"""
from __future__ import annotations

import math

# 风险金额 sizing 引擎版本（PR-09）：输出必须携带 explanation/audit 字段。
SIZING_ENGINE_VERSION = "risk-sizing-v2"


def dynamic_minimum_order_amount(
    cycle_capital,
    position_limit,
    *,
    exposure_cap=0.82,
    slot_utilization=0.60,
    round_to=100.0,
):
    """Return a meaningful new-position amount for the current cycle.

    The threshold is derived from the cycle's declared capital and the
    effective maximum number of stock slots, rather than from a fixed amount::

        floor_to_100(cycle_capital * exposure_cap / position_limit * 60%)

    The 60% factor leaves a 40% reserve for risk-controlled adds, fees, price
    movement and shared-pool reconciliation while keeping a normal order
    meaningful.  ``round_to`` is also the minimum non-zero threshold, so a
    malformed tiny cycle cannot disable the dust-order guard.
    """
    try:
        capital = max(0.0, float(cycle_capital or 0.0))
        slots = max(0, int(position_limit or 0))
        exposure = max(0.0, min(1.0, float(exposure_cap)))
        utilization = max(0.0, min(1.0, float(slot_utilization)))
        granularity = max(1.0, float(round_to))
    except (TypeError, ValueError):
        return 0.0
    if capital <= 0.0 or slots <= 0 or exposure <= 0.0 or utilization <= 0.0:
        return 0.0
    raw = capital * exposure / slots * utilization
    rounded = math.floor(raw / granularity) * granularity
    return round(max(granularity, rounded), 2)


def price_aware_qty(
    nav, cash, position_value, industry_value, code_value,
    fill_price, hard_stop, profile, exposure_cap=None, max_exposure_cap=None,
    exposure_scale=1.0, strategy_position_value=None,
    strategy_cap_amount=None, pool_cap_amount=None,
    pending_strategy_amount=0.0, pending_pool_amount=0.0,
    *, num, lot_size=100, single_position_max_amount=None,
    liquidity_cap_amount=None,
):
    """按股数计算下单规模；金额约束只做 sizing，不执行任何写操作。

    约束取最小值：风险金额（entry-stop 距离）、策略权重/单票上限、
    策略预算与共享池上限、行业集中度、**流动性**、现金，最后按
    ``lot_size``（100 股）向下取整。输出必带 explanation/audit 字段：
    ``qty_risk_amount``（最终股数在止损位的损失金额）、
    ``entry_stop_distance_pct``、``sizing_explanation``（可读的逐步解释）。
    """
    if fill_price <= 0:
        return 0, {"engine": SIZING_ENGINE_VERSION, "target_amount": 0.0, "reason": "无有效价格"}
    loss_per_share = fill_price * max(abs(hard_stop), 0.01)
    risk_budget = nav * profile["single_risk"]
    by_risk = risk_budget / loss_per_share
    configured_single_cap = num(single_position_max_amount, 0.0)
    weight_cap_amount = max(0.0, nav * profile["max_weight"])
    position_cap_amount = min(weight_cap_amount, configured_single_cap) if configured_single_cap > 0 else weight_cap_amount
    by_weight = max(0.0, position_cap_amount - code_value) / fill_price
    profile_exposure = num(max_exposure_cap, profile["max_exposure"])
    effective_exposure = min(profile_exposure, num(exposure_cap, profile_exposure))
    scale = max(0.0, min(num(exposure_scale, 1.0), 1.0))
    pool_limit = nav * effective_exposure if pool_cap_amount is None else num(pool_cap_amount)
    pool_remaining = max(
        0.0,
        pool_limit - num(position_value) - max(0.0, num(pending_pool_amount)),
    )
    if strategy_cap_amount is None:
        strategy_remaining = pool_remaining
    else:
        strategy_remaining = max(
            0.0,
            num(strategy_cap_amount) - num(strategy_position_value)
            - max(0.0, num(pending_strategy_amount)),
        )
    remaining_exposure = min(pool_remaining, strategy_remaining)
    by_exposure = remaining_exposure * scale / fill_price
    by_industry = max(0.0, nav * profile["max_industry"] - industry_value) / fill_price
    by_cash = max(0.0, cash) / fill_price
    limits = {
        "risk": by_risk, "weight": by_weight, "exposure": by_exposure,
        "industry": by_industry, "cash": by_cash,
    }
    liquidity_cap = num(liquidity_cap_amount, 0.0) if liquidity_cap_amount is not None else 0.0
    if liquidity_cap > 0.0:
        limits["liquidity"] = liquidity_cap / fill_price
    shares = min(limits.values())
    qty = int(shares / lot_size) * lot_size
    binding = [name for name, value in limits.items() if value <= shares + 1e-6]
    qty_risk_amount = qty * loss_per_share
    stop_distance_pct = max(abs(hard_stop), 0.01) * 100.0
    explanation = [
        f"风险金额：组合净值 {nav:,.2f} × 单笔风险 {profile['single_risk'] * 100:.2f}%"
        f" = {risk_budget:,.2f}；entry-stop 距离 {stop_distance_pct:.2f}%"
        f"（每股 {loss_per_share:.4f}）→ 最多 {by_risk:,.0f} 股",
        f"策略预算/共享池剩余 {by_exposure:,.0f} 股；单票权重上限余量 {by_weight:,.0f} 股",
        f"行业集中度余量 {by_industry:,.0f} 股；现金余量 {by_cash:,.0f} 股",
    ]
    if liquidity_cap > 0.0:
        explanation.append(
            f"流动性上限 {liquidity_cap:,.2f}（成交额 × 参与率）→ 最多 {limits['liquidity']:,.0f} 股"
        )
    explanation.append(
        f"取最小值 {shares:,.0f} 股，按 {lot_size} 股整手向下取整 → {qty} 股"
        f"；若触发止损，损失金额 {qty_risk_amount:,.2f}"
    )
    return max(qty, 0), {
        "engine": SIZING_ENGINE_VERSION,
        "risk_budget": round(risk_budget, 2), "loss_per_share": round(loss_per_share, 4),
        "target_amount": round(qty * fill_price, 2), "price": round(fill_price, 4),
        "effective_max_exposure_pct": round(effective_exposure * 100, 1),
        "pool_remaining_amount": round(pool_remaining, 2),
        "strategy_remaining_amount": round(strategy_remaining, 2),
        "pending_pool_amount": round(max(0.0, num(pending_pool_amount)), 2),
        "pending_strategy_amount": round(max(0.0, num(pending_strategy_amount)), 2),
        "new_exposure_scale_pct": round(scale * 100, 1),
        "single_position_max_amount": round(configured_single_cap, 2) if configured_single_cap > 0 else 0.0,
        "single_position_cap_source": "configured_absolute_cap" if configured_single_cap > 0 else "strategy_weight_auto",
        "constraint_shares": {name: round(value, 2) for name, value in limits.items()},
        "binding_constraints": binding,
        # PR-09 审计字段：QtyRisk 与止损距离、流动性上限、可读解释。
        "qty_risk_amount": round(qty_risk_amount, 2),
        "entry_stop_distance_pct": round(stop_distance_pct, 2),
        "liquidity_cap_amount": round(liquidity_cap, 2) if liquidity_cap > 0.0 else None,
        "liquidity_cap_shares": round(limits["liquidity"], 2) if liquidity_cap > 0.0 else None,
        "unrounded_shares": round(shares, 2),
        "rounded_to_lot_size": int(lot_size),
        "sizing_explanation": explanation,
    }
