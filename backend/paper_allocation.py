# -*- coding: utf-8 -*-
"""模拟盘共享资金与席位分配的纯计算。"""
from __future__ import annotations


def position_limits(
    account_ids,
    weights,
    baseline_exposure,
    *,
    hard_pool_cap,
    strategy_max_positions,
    strategy_min_positions,
    protected_slot_floor,
    account_order,
    main_force_id,
    main_force_cap=3,
):
    """按风险权重在总席位硬上限内分配各策略席位。"""
    ids = list(account_ids)
    count = len(ids)
    current = sum(weights.values()) / max(count, 1)
    hard_cap = min(hard_pool_cap, strategy_max_positions * count)
    risk_scale = max(0.60, min(1.0, current / max(baseline_exposure, 0.01)))
    base_floor_total = min(hard_cap, strategy_min_positions * count)
    total_cap = max(base_floor_total, min(hard_cap, int(round(hard_cap * risk_scale))))
    protected = (
        min(protected_slot_floor, strategy_max_positions)
        if total_cap >= protected_slot_floor * count
        else strategy_min_positions
    )
    minimum = min(protected, total_cap // max(count, 1))
    weight_total = sum(weights.values()) or 1.0
    raw = {key: total_cap * weights[key] / weight_total for key in ids}
    caps = {
        key: (main_force_cap if key == main_force_id else strategy_max_positions)
        for key in ids
    }
    limits = {
        key: max(minimum, min(caps[key], int(raw[key])))
        for key in ids
    }
    while sum(limits.values()) < total_cap:
        candidates = [key for key in ids if limits[key] < caps[key]]
        if not candidates:
            break
        key = max(
            candidates,
            key=lambda item: (
                raw[item] - limits[item], weights[item], -account_order.get(item, 99)
            ),
        )
        limits[key] += 1
    while sum(limits.values()) > total_cap:
        candidates = [key for key in ids if limits[key] > minimum]
        if not candidates:
            break
        key = max(
            candidates,
            key=lambda item: (
                limits[item] - raw[item], -weights[item], account_order.get(item, 99)
            ),
        )
        limits[key] -= 1
    return {
        "risk_scale": risk_scale,
        "protected_slot_floor": protected,
        "total_cap": total_cap,
        "limits": limits,
    }


def strategy_pool_budget(
    *,
    account_id,
    values,
    weights,
    pending_by_account,
    pending_total,
    nav,
    market_scales=None,
    shared_pool_max_exposure,
    strategy_pool_floor_ratio,
    main_force_id,
    main_force_priority_floor_pct,
    sector_rotation_id="sector_rotation",
):
    """计算共享资金池中单一策略的目标、地板和可追加额度。"""
    nav = max(float(nav or 0.0), 0.0)
    pool_cap_amount = nav * shared_pool_max_exposure
    pool_value = sum(values.values())
    global_remaining = max(0.0, pool_cap_amount - pool_value - pending_total)
    weight_total = sum(weights.values()) or 1.0
    base_target_pct = {
        key: shared_pool_max_exposure * weight / weight_total
        for key, weight in weights.items()
    }
    scales = market_scales
    target_pct = {
        key: value * (scales.get(key, 0.0) if scales is not None else 1.0)
        for key, value in base_target_pct.items()
    }
    floor_pct = {key: value * strategy_pool_floor_ratio for key, value in target_pct.items()}
    priority_floor_amount = (
        nav * main_force_priority_floor_pct
        if account_id == main_force_id else 0.0
    )
    if priority_floor_amount > 0.0:
        target_pct[account_id] = max(
            target_pct.get(account_id, 0.0), main_force_priority_floor_pct
        )
        floor_pct[account_id] = max(
            floor_pct.get(account_id, 0.0), main_force_priority_floor_pct
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
    if account_id == sector_rotation_id and nav > 0:
        allowance = min(allowance, max(0.0, nav - current_total_amount))
    absolute_cap = current_total_amount + max(0.0, allowance)
    round2 = lambda value: round(value, 2)
    return {
        "account_id": account_id,
        "target_pct": round(target_pct.get(account_id, 0.0) * 100, 2),
        "base_target_pct": round(base_target_pct.get(account_id, 0.0) * 100, 2),
        "market_scale_pct": round((scales.get(account_id, 0.0) if scales is not None else 1.0) * 100, 1),
        "market_scale_applied": bool(scales is not None),
        "floor_pct": round(floor_pct.get(account_id, 0.0) * 100, 2),
        "priority_floor_pct": round(main_force_priority_floor_pct * 100, 2) if priority_floor_amount > 0.0 else None,
        "priority_floor_amount": round2(priority_floor_amount),
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
        "redistribution_allowed": bool(redistribution > 0.0),
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
