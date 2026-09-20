# -*- coding: utf-8 -*-
"""纯 replacement / slot-upgrade 决策领域模块（R18）。

定位
----
``paper_trading`` 负责证据收集（读候选 signal、读持仓、读历史 review、跑
security scope），本模块只管三件**确定性**的事：

1. :func:`score_candidate` —— replacement candidate 的 0..100 复合分；
2. :func:`choose_best_candidate` —— 从已筛好的候选中选出最强的一个；
3. :func:`decide_slot_upgrade` —— 借位 / 择强换仓的比较与状态机。

硬边界（由 ``test_paper_trading_architecture_guard.py`` 静态强制）::

    zero DB / network / filesystem / wall clock
    不 import paper_trading / paper_position_review / strategy_policies / sqlite3
    相同输入必须逐字得到相同输出

为什么必须与 :mod:`paper_position_review` 分开
----------------------------------------------
``paper_position_review`` 拥有**持仓**的复核动作（watch / hold /
consolidation_exit）；本模块拥有**候选**的评分与席位比较。二者口径不同：

* candidate 分 = ``entry 45% + t_score 35% + rank 20%``；
* holding 分 = ``model / trend / flow / momentum / return / news`` 加权。

把它们合并成一个"统一评分"会悄悄改变两边的语义，因此刻意保持两个模块。

所有阈值经 :class:`ReplacementPolicy` 由调用方注入；本模块内**不写死**任何策略参数。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = [
    "ReplacementPolicy",
    "REPLACEMENT_DECISION_VERSION",
    "allocation_version_id",
    "score_candidate",
    "choose_best_candidate",
    "derive_donors",
    "decide_slot_upgrade",
]

REPLACEMENT_DECISION_VERSION = "replacement-decision-v1"


def allocation_version_id(text, default: int = 0) -> int:
    """把 ``slots-vN`` 形式的席位版本 token 解析成 ``N``。

    借位 / 回滚都要用同一个版本行身份去定位**显式周期**的
    ``paper_position_limit_versions`` 行；解析逻辑只此一份，避免各自
    ``try/except`` 出不同的默认值。无法解析（缺字段 / 非法 token）时返回
    ``default``，调用方据此 fail closed。
    """
    try:
        return int(str(text).rsplit("v", 1)[-1])
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ReplacementPolicy:
    """由 adapter 从 ``paper_trading`` 常量构造的纯数据策略包。

    字段名与 ``paper_trading.py`` 顶部的常量一一对应；把值注入纯模块是为了让领域层
    保持 zero-project-import，同时阈值仍只有一个事实来源（调用方）。

    ``exit_score`` 不参与候选评分，但 ``urgent``（紧急择强）判定需要它：只有**最弱
    持仓**已经掉到淘汰线附近时，"候选显著更强"才算紧急。
    """
    strategy_min_positions: int = 2
    strategy_max_positions: int = 6
    shared_pool_max_positions: int = 15
    borrow_min_candidate_score: float = 65.0
    borrow_min_edge: float = 12.0
    upgrade_min_candidate_score: float = 75.0
    upgrade_min_edge: float = 25.0
    execution_buffer: float = 3.0
    full_cap_edge: float = 10.0
    full_cap_max_score: float = 68.0
    replace_edge: float = 18.0
    any_replace_score: float = 42.0
    replace_score: float = 52.0
    exit_score: float = 38.0
    lot_size: int = 100


def _num(value, default=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value:  # NaN
        return default
    return float(value)


def _score100(value, default=50.0):
    """Normalize model/rank scores that may be expressed as 0..1 or 0..100."""
    if value is None:
        return default
    value = _num(value, default / 100.0)
    if 0.0 <= value <= 1.5:
        value *= 100.0
    return max(0.0, min(100.0, value))


def _payload_of(signal):
    if not isinstance(signal, dict):
        return {}
    raw = signal.get("payload")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def score_candidate(signal) -> float:
    """Return the comparable 0..100 score used for a slot replacement.

    The entry assessment, intraday score and selection rank have different
    scales.  Keeping this composition in one place prevents the order gate,
    holding review and UI audit from comparing different numbers.

    公式（**逐字保持**，不得改权重）::

        entry_model.score * 0.45 + t_score * 0.35 + rank_score * 0.20

    含 0..1 / 0..100 归一化、0..100 clamp 与 ``round(..., 2)``。
    """
    signal = signal or {}
    payload = _payload_of(signal)
    entry = (payload.get("decision") or {}).get("entry_model") or {}
    return round(
        _score100(entry.get("score"), 0.0) * 0.45
        + _score100(signal.get("t_score"), 0.0) * 0.35
        + _score100(signal.get("rank_score"), 0.0) * 0.20,
        2,
    )


def choose_best_candidate(candidates, *, held_codes):
    """选出最强的允许候选（纯排序，不读库、不做 security 判断）。

    ``candidates`` 必须已经由 evidence + adapter 筛好（同一账户、同一
    ``intended_date == asof_day``、``signal_date <= asof_day``、可执行 status、
    security scope 允许）。本函数只负责：

    * 排除已持有代码（每项取 ``code`` 字段）；
    * 按分数取最大；分数相同则**先出现者胜**（调用方已按 t_score / rank_score /
      id 排序，因此保持旧实现的稳定性）。

    返回 ``{"signal_id", "status", "code", "name", "score", "intended_date"}``
    或 ``None``。
    """
    held = {str(code) for code in (held_codes or set())}
    best = None
    for row in candidates or []:
        code = str((row or {}).get("code") or "")
        if not code or code in held:
            continue
        score = score_candidate(row)
        item = {
            "signal_id": int(row["id"]),
            "status": row.get("status"),
            "code": code,
            "name": row.get("name") or code,
            "score": round(score, 2),
            "intended_date": row.get("intended_date"),
        }
        if best is None or item["score"] > best["score"]:
            best = item
    return best


def derive_donors(*, limits, counts, account_id, pool_limit, occupied_pool_count,
                  policy: ReplacementPolicy):
    """列出可让出一个未使用席位的 donor（纯算术）。

    规则（**不得放宽**）：

    * 出让策略必须保留 ``max(strategy_min_positions, 当前持仓数)`` 的底座；
    * 共享池在**未用满**时直接出借一个未分配席位（不突破总上限）。

    ``limits`` / ``counts`` 都是 ``{account_id: int}``。
    """
    donors = []
    for donor_id, donor_limit_raw in (limits or {}).items():
        if donor_id == account_id:
            continue
        donor_limit = int(_num(donor_limit_raw))
        donor_count = int(_num((counts or {}).get(donor_id, 0)))
        donor_floor = max(policy.strategy_min_positions, donor_count)
        if donor_limit > donor_floor and donor_count < donor_limit:
            donors.append({
                "account_id": donor_id,
                "limit": donor_limit,
                "count": donor_count,
                "remaining_after": donor_limit - 1,
            })
    occupied = int(_num(occupied_pool_count))
    if occupied < int(_num(pool_limit)):
        donors.append({
            "account_id": "shared_pool",
            "limit": int(_num(pool_limit)),
            "count": occupied,
            "remaining_after": occupied,
            "unused_pool_slots": int(_num(pool_limit)) - occupied,
        })
    return donors


def _borrow_ready(*, target_limit, donors, candidate_score, pool_donor_available,
                  edge, policy: ReplacementPolicy):
    return bool(
        int(_num(target_limit)) < policy.strategy_max_positions
        and donors
        and candidate_score >= policy.borrow_min_candidate_score
        and (pool_donor_available or (edge is not None and edge >= policy.borrow_min_edge))
    )


def decide_slot_upgrade(*, candidate_score, weakest, target_limit, donors,
                        at_dynamic_limit, min_hold_days, policy: ReplacementPolicy):
    """借位 / 择强换仓的纯比较与状态机。

    决策优先级（**顺序不可变**，规格 §32）::

        borrow eligibility
          → weakest position
          → T+1 lock
          → minimum hold
          → urgent upgrade
          → normal / full-slot upgrade
          → edge insufficient

    特别：T+1 锁定**不得**因为候选很强而被普通 upgrade 绕过。

    ``at_dynamic_limit``（该策略持仓已达动态上限）由 adapter 用**显式周期**的持仓
    数算出后传入 —— 纯模块不读持仓、不认周期。

    返回 schema 与旧 ``_slot_upgrade_context`` 的返回**逐字段一致**；
    ``weakest is None`` 时同样只返回 ``candidate_score`` / ``borrow_candidate_score`` /
    ``borrow_ready`` / ``donors`` / ``eligible`` / ``reason``（不造 ``weakest``/``state``）。
    """
    donors = list(donors or [])
    pool_donor_available = any(item.get("account_id") == "shared_pool" for item in donors)
    target_limit = int(_num(target_limit))

    if weakest is None:
        borrow = _borrow_ready(
            target_limit=target_limit, donors=donors, candidate_score=candidate_score,
            pool_donor_available=pool_donor_available, edge=None, policy=policy)
        return {
            "candidate_score": candidate_score,
            "borrow_candidate_score": candidate_score,
            "borrow_ready": borrow,
            "donors": donors,
            "eligible": borrow,
            "reason": (
                f"候选 {candidate_score:.1f} 分可从 {donors[0]['account_id']} 借用一个未使用席位"
                if borrow else "暂无可比较的存量持仓"
            ),
        }

    edge = candidate_score - _num(weakest.get("score"))
    borrow_ready = _borrow_ready(
        target_limit=target_limit, donors=donors, candidate_score=candidate_score,
        pool_donor_available=pool_donor_available, edge=edge, policy=policy)
    urgent = bool(
        candidate_score >= policy.upgrade_min_candidate_score
        and edge >= policy.upgrade_min_edge
        and _num(weakest.get("score")) <= policy.exit_score
    )
    net_edge = edge - policy.execution_buffer
    at_dynamic_limit = bool(at_dynamic_limit)
    full_slot_ready = bool(
        at_dynamic_limit
        and net_edge >= policy.full_cap_edge
        and _num(weakest.get("score")) < policy.full_cap_max_score
    )
    regular_upgrade_ready = bool(
        net_edge >= policy.replace_edge
        and (
            _num(weakest.get("score")) <= policy.any_replace_score
            or (
                weakest.get("review_action") in {"watch", "reduce", "exit"}
                and _num(weakest.get("score")) < policy.replace_score
            )
        )
    )
    upgrade_ready = bool(urgent or full_slot_ready or regular_upgrade_ready)
    min_hold_days = int(_num(min_hold_days))

    if int(_num(weakest.get("available_qty"))) < policy.lot_size:
        state = "t1_locked"
        reason = (
            f"高分替补 {candidate_score:.1f} 分，现有最弱仓 {weakest['name']} "
            f"{_num(weakest.get('score')):.1f} 分，分差 {edge:.1f}；最弱仓受 T+1 锁定，"
            "保留为优先替补，最早可卖后自动复核"
        )
    elif upgrade_ready and int(_num(weakest.get("hold_days"))) < min_hold_days and not urgent:
        state = "observe"
        reason = (
            f"候选 {candidate_score:.1f} 分高于最弱仓 {_num(weakest.get('score')):.1f} 分"
            f"（+{edge:.1f}），但最弱仓观察期仅 {int(_num(weakest.get('hold_days')))}/"
            f"{min_hold_days} 日，继续观察以避免高频换手"
        )
    elif urgent:
        state = "urgent_upgrade"
        reason = (
            f"候选 {candidate_score:.1f} 分显著高于最弱仓 {weakest['name']} "
            f"{_num(weakest.get('score')):.1f} 分（+{edge:.1f}），达到紧急择强换仓条件；"
            "等待下一次风控扫描按 T+1 和行情核验执行"
        )
    elif full_slot_ready or regular_upgrade_ready:
        state = "upgrade_ready"
        reason = (
            f"候选 {candidate_score:.1f} 分高于最弱仓 {weakest['name']} "
            f"{_num(weakest.get('score')):.1f} 分（原始 +{edge:.1f}、成本缓冲后 +{net_edge:.1f}），"
            "进入择强换仓队列"
        )
    else:
        state = "edge_insufficient"
        reason = (
            f"候选 {candidate_score:.1f} 分较最弱仓 {_num(weakest.get('score')):.1f} 分高 "
            f"{edge:.1f}，扣除执行缓冲后 {net_edge:.1f}，未达到满席净优势 "
            f"{policy.full_cap_edge:.1f} 分；继续候选重排，不占换仓队列"
        )
    return {
        "candidate_score": candidate_score, "weakest": weakest,
        "edge": round(edge, 2), "urgent": urgent,
        "borrow_candidate_score": candidate_score,
        "borrow_ready": borrow_ready,
        "donors": donors,
        "state": "slot_borrow_ready" if borrow_ready else state,
        "eligible": bool(borrow_ready or state in {"urgent_upgrade", "upgrade_ready"}),
        "reason": (
            f"候选 {candidate_score:.1f} 分达到借位条件，可从 {donors[0]['account_id']} "
            f"转入一个未使用席位；不突破总上限{policy.shared_pool_max_positions}"
            if borrow_ready else reason
        ),
    }
