# -*- coding: utf-8 -*-
"""模拟盘策略身份注册表。

策略状态只描述公开运行口径，不自动启用或删除任何账户。新周期当前只
启用两套策略；历史策略保留给审计、研究和回放使用。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategySpec:
    id: str
    name: str
    status: str
    supports_new_cycle: bool


STRATEGY_REGISTRY = (
    StrategySpec("tq_breakout", "短线日内做T", "active", True),
    StrategySpec("trend_pullback", "趋势波段优选", "legacy", False),
    StrategySpec("sector_rotation", "板块轮动先锋", "legacy", False),
    StrategySpec("reported_profit_breakout", "三日策略", "legacy", False),
    StrategySpec("main_force_top10", "超强主力股", "active", True),
)

_BY_ID = {spec.id: spec for spec in STRATEGY_REGISTRY}


def get(strategy_id):
    """按策略 ID 返回不可变规格；未知 ID 返回 ``None``。"""
    return _BY_ID.get(str(strategy_id or ""))


def labels():
    """返回兼容旧模块的 ID → 展示名称映射副本。"""
    return {spec.id: spec.name for spec in STRATEGY_REGISTRY}


def active_ids():
    """返回当前新周期允许使用的策略 ID。"""
    return tuple(spec.id for spec in STRATEGY_REGISTRY if spec.supports_new_cycle)


def statuses():
    """返回 ID → active/legacy 状态映射副本。"""
    return {spec.id: spec.status for spec in STRATEGY_REGISTRY}
