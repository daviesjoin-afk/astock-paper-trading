# -*- coding: utf-8 -*-
"""Application boundary for retirement policy evaluation（R33-B）.

三件事，仅此而已：

1. 按调用方给出的 **exact snapshot id** 读取既有健康快照（绝不查找 latest / current）；
2. 调用纯 policy :func:`strategy_retirement_policy.evaluate_retirement_policy`；
3. 追加决策，并**只描述**该决策将来会要求哪条 lifecycle 边（proposal），由 lifecycle owner
   自己的 ``TRANSITION_TABLE`` 判合法性 —— 本模块**不调用** ``transition``。

它不读原始账本（orders / fills / risk / positions / performance），因此不可能在这里重新计算
收益、回撤或风险。
"""
from __future__ import annotations

import paper_trading as PT
import strategy_health_repository as SHRepo
import strategy_lifecycle as SL
import strategy_retirement_policy as RP
import strategy_retirement_repository as RR


def _with_paper_connection(work, *, immediate: bool = False):
    PT.init_db()
    with PT._db(immediate=immediate) as conn:
        return work(conn)


def _snapshot_for(conn, strategy_id: str, snapshot_id: str):
    try:
        snapshot = SHRepo.get_snapshot(conn, snapshot_id)
    except SHRepo.StrategyHealthRepositoryError as exc:
        # 畸形 id 是 caller 的输入错误，存储行自校验失败是损坏：两者都在这里翻译成
        # 受控的 retirement 拒绝，HTTP 层才会给出 4xx 而不是 500。
        reason = ("explicit_health_snapshot_id_required"
                  if str(exc) == "explicit_health_snapshot_id_required"
                  else "health_snapshot_corrupt")
        raise RP.RetirementPolicyError(reason) from exc
    if snapshot is None:
        raise RP.RetirementPolicyError("health_snapshot_not_found")
    if snapshot.strategy_id != str(strategy_id):
        # 页面身份与快照身份必须一致：否则就是把 A 的证据算成 B 的退休建议。
        raise RP.RetirementPolicyError("health_snapshot_strategy_mismatch")
    return snapshot


def evaluate_retirement(strategy_id: str, *, snapshot_id: str) -> dict:
    """Evaluate one exact health snapshot into one policy recommendation."""
    def _work(conn):
        snapshot = _snapshot_for(conn, strategy_id, snapshot_id)
        decision = RP.evaluate_retirement_policy(snapshot)
        appended = RR.append_decision(conn, decision)
        proposal = RP.build_transition_proposal(
            appended, lifecycle_state=snapshot.lifecycle_state,
            legal_targets=SL.TRANSITION_TABLE.get(snapshot.lifecycle_state or "", ()))
        return {"decision": appended.projection(), "transition_proposal": proposal}
    return _with_paper_connection(_work, immediate=True)


def get_retirement_decision(strategy_id: str, decision_id: str) -> dict:
    """Read exactly one decision by id; never "the latest decision"."""
    def _work(conn):
        decision = RR.get_decision(conn, decision_id)
        if decision is None:
            raise RP.RetirementPolicyError("retirement_decision_not_found")
        if decision.strategy_id != str(strategy_id):
            raise RP.RetirementPolicyError("retirement_decision_strategy_mismatch")
        return decision.projection()
    return _with_paper_connection(_work)


__all__ = ["evaluate_retirement", "get_retirement_decision"]
