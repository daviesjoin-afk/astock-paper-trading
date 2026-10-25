# -*- coding: utf-8 -*-
"""权重决策的参与者范围与有界投影（唯一入口，纯计算 + 只读查询）。

**为什么需要**：提案/审批通道原来用 ``strategy_registry.labels()``——它连
``retiring``/``archived`` 的策略一起返回。五套并列时，15% 的单策略下限把任何
一个策略的上限压到 40%，稳健的 50% 在提案空间里**不可表示**；退役两套之后，
它还会把已经退出独立资金账户的旧策略重新拉进权重，然后由分配权重解析器静默
丢弃（``portfolio_allocation_weights`` 只收执行参与者）。

本模块给出一份答案：**执行参与者**（``paper_cycles.enabled_strategies`` ∩
账本挂接 − 非正式生命周期）。它不 import ``paper_trading``，因此不会形成环。

**边界**：本模块不写库、不下单、不改权重、不做收益判断；``project_weights``
是纯函数，只保证"恰好 100%、每个值在给定上下限内、0.1 个百分点分辨率"。
"""
from __future__ import annotations

import math

import paper_cycle_ownership as PCY
import strategy_registry as SR

__all__ = ["participant_ids", "project_weights", "SCOPE_VERSION"]

SCOPE_VERSION = "strategy-weight-scope-v1"


def participant_ids(conn, *, cycle_id=None):
    """当前周期的执行参与者（有序元组）。读不到时返回**空元组**。

    空元组是真实的空（idle 周期、账本不可读、注册表缺失），调用方必须显式
    处理；**不得**退回到"全部注册策略"，那正是这次要修的旧口径。
    """
    try:
        if conn is None:
            return ()
        builtin = tuple(SR.active_ids(conn=conn))
    except Exception:
        return ()
    try:
        return tuple(PCY.execution_participant_ids(conn, cycle_id, builtin_scope=builtin))
    except Exception:
        return ()


def project_weights(raw, ids, *, min_pct, max_pct):
    """把任意权重投影到"恰好 100%、每项 ``[min_pct, max_pct]``"。

    ``raw`` 只用于排序偏好（缺失按 0 处理，全 0 时退化为等权）。返回 0.1 个
    百分点分辨率的字典，合计恰好 100.0。下限/上限对给定参与者数量不可行时抛
    ``ValueError``（例如 5 个策略各 15% 下限要求 100% 可行，但 1 个策略 60% 上限
    不可行）。
    """
    accounts = [str(item) for item in ids]
    if not accounts:
        raise ValueError("no_participant")
    if len(set(accounts)) != len(accounts):
        raise ValueError("duplicate_participant")
    try:
        low = int(round(float(min_pct) * 10))
        high = int(round(float(max_pct) * 10))
    except (TypeError, ValueError) as exc:
        raise ValueError("weight bounds must be numeric") from exc
    total = 1000
    if low < 0 or high > total or low > high:
        raise ValueError("weight bounds invalid")
    if low * len(accounts) > total:
        raise ValueError("min_weight infeasible for participant count")
    if high * len(accounts) < total:
        raise ValueError("max_weight infeasible for participant count")
    preference = {}
    for account in accounts:
        value = raw.get(account) if isinstance(raw, dict) else None
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        preference[account] = value if math.isfinite(value) and value > 0 else 0.0
    if sum(preference.values()) <= 0:
        preference = {account: 1.0 for account in accounts}
    raw_total = sum(preference.values())
    targets = {account: preference[account] / raw_total * total for account in accounts}

    # 水位法：找一个统一平移量，使 Σ clamp(target+shift, low, high) 恰好等于 100%；
    # 直接"先限幅再归一化"会把策略压到下限以下（这正是旧实现的问题）。
    left, right = float(-total), float(total)
    for _ in range(80):
        shift = (left + right) / 2
        placed = sum(min(max(targets[account] + shift, low), high) for account in accounts)
        if placed < total:
            left = shift
        else:
            right = shift
    units = {account: int(math.floor(min(max(targets[account] + right, low), high)))
             for account in accounts}
    remainder = total - sum(units.values())
    if remainder < 0:
        raise ValueError("weight bounds infeasible")
    order = sorted(accounts, key=lambda key: (-(targets[key] + right - units[key]), key))
    for account in order:
        if remainder <= 0:
            break
        if units[account] + 1 <= high:
            units[account] += 1
            remainder -= 1
    if remainder != 0 or sum(units.values()) != total:
        raise ValueError("unable to project exact weights")
    if any(units[account] < low or units[account] > high for account in accounts):
        raise ValueError("projected weights outside bounds")
    return {account: units[account] / 10 for account in accounts}
