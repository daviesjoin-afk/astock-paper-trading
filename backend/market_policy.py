# -*- coding: utf-8 -*-
"""Shared market-light policy for paper-trading sleeves.

This module intentionally has no dependency on the execution engine or the
risk dashboard.  Both consumers must use this single policy so a new strategy
cannot be tradable in one path and shown as blocked in the other.
"""
from __future__ import annotations


MARKET_LIGHT_SCALES = {
    "green": {
        "tq_breakout": 1.00,
        "trend_pullback": 1.00,
        "sector_rotation": 1.00,
        "reported_profit_breakout": 1.00,
        "main_force_top10": 1.00,
    },
    "yellow": {
        "tq_breakout": 0.50,
        "trend_pullback": 0.75,
        "sector_rotation": 0.65,
        "reported_profit_breakout": 0.70,
        # 主力策略允许参与确认后的热点，但不应在市场收紧时满额追涨。
        "main_force_top10": 0.60,
    },
    "red": {
        "tq_breakout": 0.00,
        "trend_pullback": 0.00,
        "sector_rotation": 0.00,
        "reported_profit_breakout": 0.00,
        "main_force_top10": 0.00,
    },
    "unknown": {
        "tq_breakout": 0.00,
        "trend_pullback": 0.00,
        "sector_rotation": 0.00,
        "reported_profit_breakout": 0.00,
        "main_force_top10": 0.00,
    },
}


def market_light_scales(light):
    """Return the configured scales for a market light, fail-closed."""
    return MARKET_LIGHT_SCALES.get(str(light or "unknown").lower(), MARKET_LIGHT_SCALES["unknown"])


def market_light_scale(light, strategy_id, default=0.0):
    """Return one strategy's scale without silently treating unknown IDs as safe."""
    return float(market_light_scales(light).get(str(strategy_id or ""), default))
