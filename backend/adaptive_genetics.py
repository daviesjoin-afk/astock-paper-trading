# -*- coding: utf-8 -*-
"""Adaptive alpha 实验室的纯遗传搜索算子。"""
from __future__ import annotations

import statistics
from collections import defaultdict


def normalize_genome(weights, features):
    values = [float(weights.get(name, 0.0)) for name in features]
    scale = sum(abs(value) for value in values) or 1.0
    return {name: round(value / scale, 8) for name, value in zip(features, values, strict=True)}


def alpha_fitness(genome, rows, features, horizon_weights):
    """计算跨窗口横截面 spread，并惩罚不稳定和过度复杂。"""
    by_window = defaultdict(list)
    for row in rows:
        score = sum(genome[name] * row[name] for name in features)
        by_window[(row["profile_date"], row["horizon"])].append((score, row["excess_return_pct"]))
    spreads = []
    for (_, horizon), values in by_window.items():
        if len(values) < 100:
            continue
        values.sort(key=lambda item: item[0])
        bucket = max(10, len(values) // 10)
        low = statistics.mean(value[1] for value in values[:bucket])
        high = statistics.mean(value[1] for value in values[-bucket:])
        spreads.append((high - low) * horizon_weights.get(horizon, 0.0))
    if not spreads:
        return {"fitness": -999.0, "spread_pct": 0.0, "stability": 0.0, "windows": 0}
    mean_spread = statistics.mean(spreads)
    stability = sum(value > 0 for value in spreads) / len(spreads)
    dispersion = statistics.pstdev(spreads) if len(spreads) > 1 else abs(mean_spread)
    complexity = sum(abs(value) >= 0.08 for value in genome.values())
    fitness = mean_spread + 0.35 * stability - 0.18 * dispersion - 0.015 * complexity
    return {
        "fitness": round(fitness, 8),
        "spread_pct": round(mean_spread, 8),
        "stability": round(stability, 6),
        "windows": len(spreads),
    }


def mutate_genome(parent, rng, features):
    child = dict(parent)
    count = 1 if rng.random() < 0.72 else 2
    for name in rng.sample(list(features), count):
        child[name] += rng.gauss(0, 0.16)
    return normalize_genome(child, features)


def crossover(left, right, rng, features):
    return normalize_genome({
        name: left[name] if rng.random() < 0.5 else right[name]
        for name in features
    }, features)
