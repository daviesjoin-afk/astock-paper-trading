# -*- coding: utf-8 -*-
"""策略相关.cluster（PR：strategy correlation clusters）。

动机：如果把同一套打法复制成 N 个"策略"，每个策略各自的风险预算会线性
叠加——10 个近似策略拿到 10 倍风险额度。相关.cluster 用四类证据把行为
近似的策略聚成一簇，并对簇内策略施加**次线性**的预算缩放：

    簇内每个策略的有效预算 = 原预算 × 1/sqrt(簇规模)
    ⇒ 整簇合计 ≈ sqrt(n) × 单策略预算（10 个克隆 ≈ 3.16 倍，而不是 10 倍）

四类相似度证据（加权）：
- **信号重合**：近期候选/信号代码集合的 Jaccard；
- **持仓重合**：当前持仓代码集合的 Jaccard；
- **行业暴露**：持仓行业集合的 Jaccard；
- **收益相关**：两条日收益序列的 Pearson 相关（样本 < 5 或缺失时按 0，
  权重让渡给其它证据）。

相似度 ≥ ``CLUSTER_SIMILARITY_THRESHOLD`` 的策略经 union-find 归入同一簇。
单一策略（簇规模 1）不受任何惩罚。
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "STRATEGY_CLUSTER_VERSION",
    "CLUSTER_SIMILARITY_THRESHOLD",
    "SIMILARITY_WEIGHTS",
    "MIN_RETURN_SAMPLES",
    "cluster_budget_multiplier",
    "cluster_diversification_factor",
    "cluster_of",
    "jaccard",
    "pearson_correlation",
    "pairwise_similarity",
    "similarity_profile",
    "strategy_clusters",
]

STRATEGY_CLUSTER_VERSION = "strategy-clusters-v1"

CLUSTER_SIMILARITY_THRESHOLD = 0.6

SIMILARITY_WEIGHTS = {
    "signal": 0.35,
    "position": 0.35,
    "industry": 0.10,
    "return": 0.20,
}

MIN_RETURN_SAMPLES = 5


def similarity_profile(
    *,
    signal_codes: Iterable[str] = (),
    position_codes: Iterable[str] = (),
    industries: Iterable[str] = (),
    returns: Sequence[float] = (),
) -> dict[str, Any]:
    """构造一个策略的相似度画像（集合 + 收益序列）。"""
    profile = {
        "signals": {str(code) for code in signal_codes if code},
        "positions": {str(code) for code in position_codes if code},
        "industries": {str(item) for item in industries if item},
        "returns": [float(value) for value in returns],
    }
    profile["version"] = STRATEGY_CLUSTER_VERSION
    return profile


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def pearson_correlation(left: Sequence[float], right: Sequence[float]) -> float:
    """Pearson 相关；样本不足、零方差或长度不一致时返回 0。"""
    if len(left) != len(right) or len(left) < MIN_RETURN_SAMPLES:
        return 0.0
    mean_left = sum(left) / len(left)
    mean_right = sum(right) / len(right)
    dev_left = [value - mean_left for value in left]
    dev_right = [value - mean_right for value in right]
    cov = sum(a * b for a, b in zip(dev_left, dev_right, strict=False))
    var_left = sum(a * a for a in dev_left)
    var_right = sum(b * b for b in dev_right)
    if var_left <= 0 or var_right <= 0:
        return 0.0
    return max(-1.0, min(1.0, cov / math.sqrt(var_left * var_right)))


def pairwise_similarity(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    weights: Mapping[str, float] | None = None,
) -> float:
    """两个策略画像的加权相似度，结果夹到 [0, 1]。"""
    w = dict(SIMILARITY_WEIGHTS if weights is None else weights)
    signal_score = jaccard(set(left.get("signals") or ()), set(right.get("signals") or ()))
    position_score = jaccard(set(left.get("positions") or ()), set(right.get("positions") or ()))
    industry_score = jaccard(set(left.get("industries") or ()), set(right.get("industries") or ()))
    correlation = pearson_correlation(
        list(left.get("returns") or ()), list(right.get("returns") or ())
    )
    # 收益样本不足时把该权重按比例让渡给信号/持仓重合（更可靠的证据）。
    if len(list(left.get("returns") or ())) < MIN_RETURN_SAMPLES or len(
        list(right.get("returns") or ())
    ) < MIN_RETURN_SAMPLES:
        transfer = w.get("return", 0.0)
        w["signal"] = w.get("signal", 0.0) + transfer / 2
        w["position"] = w.get("position", 0.0) + transfer / 2
        w["return"] = 0.0
        correlation = 0.0
    score = (
        w.get("signal", 0.0) * signal_score
        + w.get("position", 0.0) * position_score
        + w.get("industry", 0.0) * industry_score
        + w.get("return", 0.0) * max(correlation, 0.0)
    )
    return max(0.0, min(1.0, score))


def strategy_clusters(
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    threshold: float = CLUSTER_SIMILARITY_THRESHOLD,
) -> list[set[str]]:
    """按相似度阈值用 union-find 归簇；返回簇的列表（每簇是策略 ID 集合）。"""
    names = sorted(profiles)
    parent = {name: name for name in names}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for index, left_name in enumerate(names):
        for right_name in names[index + 1:]:
            if pairwise_similarity(profiles[left_name], profiles[right_name]) >= threshold:
                union(left_name, right_name)
    clusters: dict[str, set[str]] = {}
    for name in names:
        clusters.setdefault(find(name), set()).add(name)
    return sorted(clusters.values(), key=lambda cluster: sorted(cluster)[0])


def cluster_of(strategy_id: str, clusters: Sequence[set[str]]) -> set[str]:
    """返回策略所在簇；不在任何簇里时返回只含自身的单元素簇。"""
    for cluster in clusters:
        if strategy_id in cluster:
            return set(cluster)
    return {strategy_id}


def cluster_diversification_factor(
    strategy_id: str,
    clusters: Sequence[set[str]],
    *,
    floor: float = 0.3,
) -> float:
    """簇内策略的分散化系数 = 1/sqrt(簇规模)，下限 ``floor``。

    单策略簇 = 1.0（无惩罚）；10 个近似策略 = 1/sqrt(10) ≈ 0.316。
    """
    size = max(1, len(cluster_of(strategy_id, clusters)))
    factor = 1.0 / math.sqrt(size)
    return round(max(float(floor), min(1.0, factor)), 4)


def cluster_budget_multiplier(
    clusters: Sequence[set[str]] | set[str],
    *,
    floor: float = 0.3,
) -> float:
    """整簇合计预算相对单策略预算的倍数 = sqrt(簇规模)（下限保护后）。

    这是"复制 N 个近似策略拿不到 N 倍额度"的直接不变式：
    N 个同簇策略 × 各自 1/sqrt(N) 的系数 = sqrt(N) 倍合计（N=10 → 3.16）。
    """
    if isinstance(clusters, set):
        size = max(1, len(clusters))
    else:
        size = max((len(cluster) for cluster in clusters), default=1)
    return round(math.sqrt(max(1, size)) if size > 1 else 1.0, 4) if size > 1 else 1.0
