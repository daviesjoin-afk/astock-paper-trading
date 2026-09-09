# -*- coding: utf-8 -*-
"""策略相关.cluster（PR：strategy correlation clusters）。

动机：如果把同一套打法复制成 N 个"策略"，每个策略各自的风险预算会线性
叠加——10 个近似策略拿到 10 倍风险额度。相关.cluster 用证据把行为近似
的策略聚成一簇，并对簇施加**封顶**的预算缩放（PR-27 cluster-first）：

    簇总预算 = 单策略基准 × (1 + novelty_bonus)
    novelty_bonus = min(0.10, 0.02 × (n-1))   # 复制 2/10/50 份 → 1.02/1.10/1.10
    簇内每个成员的分项上限 = 簇总预算 / n（均分，先到先得被取消）

⇒ 复制不改预算：50 个克隆的总预算 ≈ 1.1 倍单策略（v1 曾是 15 倍），
  且无法靠抢先下单挤占簇内额度；真正独立的策略（不同簇）完全不受影响。

五类相似度证据（加权；PR-27 新增第五类 DSL 结构）：
- **信号重合**：近期候选/信号代码集合的 Jaccard；
- **持仓重合**：当前持仓代码集合的 Jaccard；
- **行业暴露**：持仓行业集合的 Jaccard；
- **收益相关**：两条日收益序列的 Pearson 相关（样本 < 5 或缺失时按 0，
  权重让渡给其它证据）；
- **DSL 结构**：策略 DSL AST 归一化后的结构相似度（**忽略具体参数值**，
  参数扫描型克隆同构即相似；缺失时权重让渡）。

归簇条件（满足其一）：
- 加权相似度 ≥ ``CLUSTER_SIMILARITY_THRESHOLD``；
- **DSL 结构完全同构**（≥ ``DSL_CLONE_THRESHOLD``）——结构性克隆是硬证据：
  新复制的策略没有任何持仓/信号/收益历史，四类行为证据全空，
  只有结构证据能第一时间抓住它。
"""
from __future__ import annotations

import difflib
import math
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "STRATEGY_CLUSTER_VERSION",
    "CLUSTER_SIMILARITY_THRESHOLD",
    "DSL_CLONE_THRESHOLD",
    "NOVELTY_BONUS_PER_MEMBER",
    "MAX_NOVELTY_BONUS",
    "SIMILARITY_WEIGHTS",
    "MIN_RETURN_SAMPLES",
    "cluster_budget_multiplier",
    "cluster_diversification_factor",
    "cluster_of",
    "dsl_ast_similarity",
    "jaccard",
    "pearson_correlation",
    "pairwise_similarity",
    "similarity_profile",
    "strategy_clusters",
]

STRATEGY_CLUSTER_VERSION = "strategy-clusters-v2"

CLUSTER_SIMILARITY_THRESHOLD = 0.6

# PR-27：DSL 结构完全同构即视为克隆（硬证据），无论行为历史是否存在。
DSL_CLONE_THRESHOLD = 0.95

# PR-27：簇预算的新意奖励——每多一个成员 +2%，封顶 +10%。
NOVELTY_BONUS_PER_MEMBER = 0.02
MAX_NOVELTY_BONUS = 0.10

SIMILARITY_WEIGHTS = {
    "signal": 0.30,
    "position": 0.30,
    "industry": 0.10,
    "return": 0.15,
    "dsl": 0.15,
}

MIN_RETURN_SAMPLES = 5


def similarity_profile(
    *,
    signal_codes: Iterable[str] = (),
    position_codes: Iterable[str] = (),
    industries: Iterable[str] = (),
    returns: Sequence[float] = (),
    dsl_ast: Mapping[str, Any] | Sequence[Any] | None = None,
) -> dict[str, Any]:
    """构造一个策略的相似度画像（集合 + 收益序列 + DSL AST）。"""
    profile = {
        "signals": {str(code) for code in signal_codes if code},
        "positions": {str(code) for code in position_codes if code},
        "industries": {str(item) for item in industries if item},
        "returns": [float(value) for value in returns],
        "dsl_ast": dsl_ast,
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


def _flatten_ast(node: Any, out: list[str]) -> None:
    """把 DSL AST 摊平成结构 token 序列（键名 + 标识符，忽略常量值）。

    忽略 ``value``/数值类叶子：同结构不同参数（阈值扫描、窗口微调）
    的克隆在结构上完全一致——这正是要抓的手法。
    """
    if isinstance(node, Mapping):
        for key in sorted(node):
            if key in ("value",):
                continue
            out.append(str(key))
            _flatten_ast(node[key], out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _flatten_ast(item, out)
    elif node is not None:
        text = str(node)
        # 纯数字叶子一律视为参数值，不参与结构比较。
        try:
            float(text)
        except ValueError:
            out.append(text)


def dsl_ast_similarity(left: Any, right: Any) -> float:
    """DSL AST 结构相似度 ∈ [0, 1]；任一侧缺失返回 0。

    基于 ``difflib.SequenceMatcher`` 的结构 token 序列比较：
    完全同构（含键序无关、参数无关）= 1.0。
    """
    if not left or not right:
        return 0.0
    left_tokens: list[str] = []
    right_tokens: list[str] = []
    _flatten_ast(left, left_tokens)
    _flatten_ast(right, right_tokens)
    if not left_tokens or not right_tokens:
        return 0.0
    return difflib.SequenceMatcher(None, left_tokens, right_tokens).ratio()


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
    left_returns = list(left.get("returns") or ())
    right_returns = list(right.get("returns") or ())
    dsl_score = dsl_ast_similarity(left.get("dsl_ast"), right.get("dsl_ast"))
    # 证据缺失时把对应权重按比例重新归一化到剩余证据上——
    # 让渡出去的权重必须跟着已知证据走，而不是砸在恰好为空的集合上。
    missing: list[str] = []
    if len(left_returns) < MIN_RETURN_SAMPLES or len(right_returns) < MIN_RETURN_SAMPLES:
        missing.append("return")
        correlation = 0.0
    if not left.get("dsl_ast") or not right.get("dsl_ast"):
        missing.append("dsl")
        dsl_score = 0.0
    if missing:
        active = {key: value for key, value in w.items() if key not in missing}
        total_active = sum(active.values())
        if total_active > 0:
            w = {key: (value / total_active if key in active else 0.0)
                 for key, value in w.items()}
        else:
            w = {key: 0.0 for key in w}
    score = (
        w.get("signal", 0.0) * signal_score
        + w.get("position", 0.0) * position_score
        + w.get("industry", 0.0) * industry_score
        + w.get("return", 0.0) * max(correlation, 0.0)
        + w.get("dsl", 0.0) * dsl_score
    )
    return max(0.0, min(1.0, score))


def strategy_clusters(
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    threshold: float = CLUSTER_SIMILARITY_THRESHOLD,
) -> list[set[str]]:
    """按证据归簇（union-find）；返回簇的列表（每簇是策略 ID 集合）。

    归簇条件（满足其一）：
    - 加权相似度 ≥ ``threshold``；
    - DSL 结构同构 ≥ ``DSL_CLONE_THRESHOLD``（结构性克隆硬证据，
      保证零历史的新复制第一时间被抓住）。
    """
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
            left = profiles[left_name]
            right = profiles[right_name]
            similar = pairwise_similarity(left, right) >= threshold
            structural_clone = dsl_ast_similarity(
                left.get("dsl_ast"), right.get("dsl_ast"),
            ) >= DSL_CLONE_THRESHOLD
            if similar or structural_clone:
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
    floor: float = 0.0,
) -> float:
    """簇内策略的分散化系数 = 1/sqrt(簇规模)。

    单策略簇 = 1.0（无惩罚）。PR-27 取消 0.3 地板：地板是线性膨胀的
    根源（50 克隆 → 50×0.3 = 15 倍），资金端的硬约束改由
    cluster-first 均分预算承担，这里只保留次线性的相对权重信号。
    """
    size = max(1, len(cluster_of(strategy_id, clusters)))
    factor = 1.0 / math.sqrt(size)
    return round(max(float(floor), min(1.0, factor)), 4)


def cluster_budget_multiplier(
    clusters: Sequence[set[str]] | set[str],
    *,
    per_member_bonus: float = NOVELTY_BONUS_PER_MEMBER,
    max_bonus: float = MAX_NOVELTY_BONUS,
) -> float:
    """整簇总预算相对单策略基准的倍数（PR-27 cluster-first 封顶口径）。

    ``倍数 = 1 + min(max_bonus, per_member_bonus × (n-1))``：
    复制 2/10/50 份 → 1.02/1.10/1.10。**复制不改预算**——多出来的
    额度只是给真实多变体的小幅新意奖励，克隆簇总预算恒 ≤ 1.1 倍。
    """
    if isinstance(clusters, set):
        size = max(1, len(clusters))
    else:
        size = max((len(cluster) for cluster in clusters), default=1)
    if size <= 1:
        return 1.0
    bonus = min(float(max_bonus), float(per_member_bonus) * (size - 1))
    return round(1.0 + bonus, 4)
