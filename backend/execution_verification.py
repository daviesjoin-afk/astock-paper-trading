# -*- coding: utf-8 -*-
"""消费层**执行验证闸门**（live execution verification gate）。

本模块把 PR150 的 execution reality contract（:mod:`execution_evidence` /
:mod:`execution_lifecycle` / :mod:`execution_outcome`）接入 paper trading 主链。
它**只做消费层 wiring**：不重新定义成交判定、不复制生命周期状态机、不重算收益口径。
所有"这是不是一次成交"的结论一律委托 :func:`execution_evidence.fill_verdict`。

──────────────────────── 为什么需要这一层 ────────────────────────

``paper_orders.status = 'filled'`` 是**账本自称**的成交，不是**证据证明**的成交。
历史上两者被当成同一件事，于是：

* 一条 ``status='filled'`` 但没有任何 ``paper_fills`` 流水的行，
  会被计入已实现盈亏、成交统计与执行绩效；
* 升级前的旧订单没有成交流水，却被默认当作真实成交继续参与统计。

这就是"执行幻觉"：选股口径的收益被当成真实成交收益来读。本闸门把两者拆开：

=================================  ==========================================
关注点                              判定依据
=================================  ==========================================
``selection_executable``            PR149 选股契约（本层**不回写**）
``execution_status`` / ``verified`` 本层：**真实成交流水证据**
=================================  ==========================================

``selection_executable=True`` 而 ``execution_verified=False`` 是**合法且常见**的：
策略选中了、也判得可执行，但真实账本里没有成交证据。此时：

* **禁止**计入已实现执行收益 / 真实成交统计 / 执行绩效；
* **保留** selection success 与 market counterfactual（它们本来就不是成交）。

──────────────────────── 四态（机器只读 code） ────────────────────────

``verified``
    成交流水证据证明**完整成交**（数量=目标量，且价格/时段可信）。
    这是 ``execution_verified = True`` 的**唯一**来源。
``partial``
    有正成交但**不完整**（数量少于目标量，或数量对得上而价格/时段证据不足）。
    部分成交**绝不**提升为完整成交。
``not_executed``
    有**肯定性**证据表明没有成交（被拒 / 撤单 / 过期 / 从未提交）。
    这是"确认的零"，不是"不知道"。
``unknown``
    证据不足或自相矛盾（缺流水、仍在途、状态无法识别、旧行无证据）。
    **fail closed**：不得当作成交，也不得当作没成交。

──────────────────────── 旧数据（Phase 4 历史兼容） ────────────────────────

升级前的 ``paper_orders`` 行没有成交流水。它们一律是 ``unknown``，
``execution_verified`` 为假，``execution_evidence_source`` 记明原因。
**禁止**把 ``status='filled'`` 自动升级成 ``verified`` —— 那正是本层要消灭的幻觉。
"""

from __future__ import annotations

from typing import Any, Mapping

try:  # ``backend`` on sys.path（生产与 ``cd backend`` 测试）
    import execution_evidence as EE
except ImportError:  # pragma: no cover - package-style import
    from . import execution_evidence as EE


EXECUTION_VERIFICATION_VERSION = "execution-verification-v1"

#: 闸门四态。机器只读这些字面量。
EXECUTION_STATUS_VERIFIED = "verified"
EXECUTION_STATUS_PARTIAL = "partial"
EXECUTION_STATUS_UNKNOWN = "unknown"
EXECUTION_STATUS_NOT_EXECUTED = "not_executed"
EXECUTION_STATUSES = (
    EXECUTION_STATUS_VERIFIED,
    EXECUTION_STATUS_PARTIAL,
    EXECUTION_STATUS_UNKNOWN,
    EXECUTION_STATUS_NOT_EXECUTED,
)

#: 证据来源（机器可读）：审计要能区分"证据证明过"与"没有证据"。
EVIDENCE_SOURCE_LEDGER = "paper_orders+paper_fills"
EVIDENCE_SOURCE_LEGACY = "legacy_row_without_fill_evidence"
EVIDENCE_SOURCE_ABSENT = "no_evidence_available"
EVIDENCE_SOURCE_INCONSISTENT = "evidence_inconsistent"
EVIDENCE_SOURCES = (
    EVIDENCE_SOURCE_LEDGER,
    EVIDENCE_SOURCE_LEGACY,
    EVIDENCE_SOURCE_ABSENT,
    EVIDENCE_SOURCE_INCONSISTENT,
)

#: ``execution_evidence.fill_verdict`` → 本层四态的**唯一**映射。
#:
#: 这是一张**穷尽**表：每个 verdict 都有归宿，且映射到 ``verified`` 的只有
#: ``fill_verified`` 一个。``fill_pending``（在途）映射到 ``unknown`` 而不是
#: ``partial``：在途既不是"成交了一部分"，也不是"确认没成交"，如实报未知。
VERDICT_TO_STATUS = {
    EE.FILL_VERDICT_VERIFIED: EXECUTION_STATUS_VERIFIED,
    EE.FILL_VERDICT_PARTIAL: EXECUTION_STATUS_PARTIAL,
    EE.FILL_VERDICT_PENDING: EXECUTION_STATUS_UNKNOWN,
    EE.FILL_VERDICT_NONE_CONFIRMED: EXECUTION_STATUS_NOT_EXECUTED,
    EE.FILL_VERDICT_NOT_ATTEMPTED: EXECUTION_STATUS_NOT_EXECUTED,
    EE.FILL_VERDICT_UNKNOWN: EXECUTION_STATUS_UNKNOWN,
}

#: 旧账本行的默认落点：**未知**，绝不因为 ``status='filled'`` 就升级。
LEGACY_EXECUTION_STATUS = EXECUTION_STATUS_UNKNOWN

#: 闸门的**唯一** SQL 谓词。读路径必须引用它，不得各写一份。
#:
#: 同时要求 ``execution_verified = 1`` **与** ``execution_status = 'verified'``：
#: 两列不一致的行（例如被手工改过、或写入时状态没同步）**fail closed**。
#: 旧行的两列都是 NULL → ``COALESCE`` 取 0 → 被排除，这正是 Phase 4 的要求。
VERIFIED_PREDICATE = (
    "(COALESCE(execution_verified, 0) = 1 AND execution_status = 'verified')"
)
#: 未验证谓词（统计"被闸门拦下多少"用，不是用来放行的）。
UNVERIFIED_PREDICATE = "NOT " + VERIFIED_PREDICATE


def status_from_verdict(verdict: Any) -> str:
    """把 PR150 的 ``fill_verdict`` 映射成本层四态。

    未知 verdict（契约之外的字符串）→ ``unknown``：不认识的结论不能当成成交。
    """
    return VERDICT_TO_STATUS.get(str(verdict or ""), EXECUTION_STATUS_UNKNOWN)


def is_verified_status(status: Any) -> bool:
    """只有 ``verified`` 才是 ``execution_verified = True``。"""
    return str(status or "") == EXECUTION_STATUS_VERIFIED


def verification_from_evidence(evidence: Any) -> dict:
    """从一条 :class:`execution_evidence.ExecutionEvidence` 得出验证结论。

    返回 ``{"execution_status", "execution_verified", "execution_evidence_source"}``。
    没有证据（``None``）→ ``unknown``，来源 ``no_evidence_available``。
    """
    if evidence is None:
        return {
            "execution_status": EXECUTION_STATUS_UNKNOWN,
            "execution_verified": False,
            "execution_evidence_source": EVIDENCE_SOURCE_ABSENT,
        }
    verdict = evidence.fill_verdict_value()
    status = status_from_verdict(verdict)
    source = EVIDENCE_SOURCE_LEDGER
    # 证据自相矛盾（例如写着 filled 却没有流水）时把来源标出来，方便审计定位，
    # 但状态仍然是 fail closed 的那一个 —— 来源不改变结论。
    try:
        if evidence.inconsistencies():
            source = EVIDENCE_SOURCE_INCONSISTENT
    except Exception:  # pragma: no cover - 契约自身不会抛；防御未来改动
        source = EVIDENCE_SOURCE_INCONSISTENT
    return {
        "execution_status": status,
        "execution_verified": is_verified_status(status),
        "execution_evidence_source": source,
    }


def legacy_verification(*, has_fill_rows: bool = False) -> dict:
    """升级前旧行的验证结论。

    ``has_fill_rows=False``（没有成交流水）→ ``unknown`` + ``legacy`` 来源。
    **绝不允许**因为 ``status='filled'`` 就返回 ``verified``。

    ``has_fill_rows=True`` 的旧行其实有流水，调用方应走
    :func:`verification_from_evidence`；这里只兜住"连流水都没有"的路径。
    """
    if has_fill_rows:
        return {
            "execution_status": EXECUTION_STATUS_UNKNOWN,
            "execution_verified": False,
            "execution_evidence_source": EVIDENCE_SOURCE_ABSENT,
        }
    return {
        "execution_status": LEGACY_EXECUTION_STATUS,
        "execution_verified": False,
        "execution_evidence_source": EVIDENCE_SOURCE_LEGACY,
    }


def verification_for_order(order: Any, fill_rows: Any = None) -> dict:
    """给一行 ``paper_orders``（+ 它的 ``paper_fills``）算出验证结论。

    这是写入路径的**唯一**入口：:func:`execution_planner.commit_fill` 与迁移脚本
    都调用它，因此"什么算成交"只有一处实现。

    ``fill_rows`` 必须是**带身份列**的流水行（``account_id`` / ``side`` / ``code``
    与数量价格并列）。身份核对在这里显式开启：调用方已经按 ``order_id`` 关联过账本，
    比对身份是它唯一能挡住"别人的流水被当成这笔委托的成交"的手段。
    """
    if order is None:
        return verification_from_evidence(None)
    rows = list(fill_rows or ())
    if not rows:
        # 没有成交流水：即使 status 写着 filled 也是未知（fail closed）。
        return legacy_verification(has_fill_rows=False)
    return verification_from_evidence(
        EE.evidence_from_order(
            order, rows, fill_identity_rows=rows, fill_identity_known=True
        )
    )


def stamp_order(conn, order_id: Any) -> dict:
    """把验证结论写回 ``paper_orders`` 的三列。返回写入的结论。

    写路径专用。读路径**不得**调用它（读面板写库会与 3 分钟 worker 抢锁）。
    """
    row = conn.execute(
        "SELECT * FROM paper_orders WHERE id=?", (order_id,)
    ).fetchone()
    if row is None:
        return verification_from_evidence(None)
    order = dict(row)
    fills = conn.execute(
        "SELECT * FROM paper_fills WHERE order_id=?", (order_id,)
    ).fetchall()
    verdict = verification_for_order(order, [dict(item) for item in fills])
    conn.execute(
        "UPDATE paper_orders SET execution_status=?, execution_verified=?,"
        " execution_evidence_source=? WHERE id=?",
        (
            verdict["execution_status"],
            int(bool(verdict["execution_verified"])),
            verdict["execution_evidence_source"],
            order_id,
        ),
    )
    return verdict


def backfill_legacy_orders(conn) -> dict:
    """给尚未盖章的旧行补上**未知**结论（幂等）。

    Phase 4：旧行没有证据 → ``unknown``，且**不**自动升级为 ``verified``。
    这里只写"未知"，不碰任何 ``execution_verified=1`` 的行。
    """
    rows = conn.execute(
        "SELECT id FROM paper_orders WHERE execution_status IS NULL"
    ).fetchall()
    stamped = 0
    for row in rows:
        verdict = verification_for_order(
            dict(conn.execute(
                "SELECT * FROM paper_orders WHERE id=?", (row["id"],)
            ).fetchone()),
            [dict(item) for item in conn.execute(
                "SELECT * FROM paper_fills WHERE order_id=?", (row["id"],)
            ).fetchall()],
        )
        conn.execute(
            "UPDATE paper_orders SET execution_status=?, execution_verified=?,"
            " execution_evidence_source=? WHERE id=?",
            (
                verdict["execution_status"],
                int(bool(verdict["execution_verified"])),
                verdict["execution_evidence_source"],
                row["id"],
            ),
        )
        stamped += 1
    return {"stamped": stamped}


def gate_report(rows: Any) -> dict:
    """按四态统计一批订单行；用于审计"闸门拦下了多少"。

    每条输入都落在某个状态里，不静默丢行。
    """
    counts = {status: 0 for status in EXECUTION_STATUSES}
    blocked = 0
    total = 0
    for row in rows or ():
        total += 1
        status = str(
            (row.get("execution_status") if isinstance(row, Mapping)
             else getattr(row, "execution_status", None)) or ""
        )
        if status not in counts:
            status = EXECUTION_STATUS_UNKNOWN
        counts[status] += 1
        if status != EXECUTION_STATUS_VERIFIED:
            blocked += 1
    return {
        "version": EXECUTION_VERIFICATION_VERSION,
        "total": total,
        "counts": counts,
        "blocked_from_execution_stats": blocked,
        "verified": counts[EXECUTION_STATUS_VERIFIED],
    }


# ───────────────────────────── self-check ─────────────────────────────


def _self_check() -> None:
    assert status_from_verdict(EE.FILL_VERDICT_VERIFIED) == EXECUTION_STATUS_VERIFIED
    assert status_from_verdict(EE.FILL_VERDICT_PARTIAL) == EXECUTION_STATUS_PARTIAL
    assert status_from_verdict(EE.FILL_VERDICT_PENDING) == EXECUTION_STATUS_UNKNOWN
    assert status_from_verdict(EE.FILL_VERDICT_NONE_CONFIRMED) == EXECUTION_STATUS_NOT_EXECUTED
    assert status_from_verdict(EE.FILL_VERDICT_NOT_ATTEMPTED) == EXECUTION_STATUS_NOT_EXECUTED
    assert status_from_verdict(EE.FILL_VERDICT_UNKNOWN) == EXECUTION_STATUS_UNKNOWN
    # 契约之外的 verdict 不得被当成成交。
    assert status_from_verdict("something_new") == EXECUTION_STATUS_UNKNOWN
    assert status_from_verdict(None) == EXECUTION_STATUS_UNKNOWN

    # 每个 verdict 都有归宿（穷尽），且只有 verified 一个映射到 verified。
    for verdict in EE.FILL_VERDICTS:
        assert verdict in VERDICT_TO_STATUS, verdict
    mapped = [v for v, s in VERDICT_TO_STATUS.items() if s == EXECUTION_STATUS_VERIFIED]
    assert mapped == [EE.FILL_VERDICT_VERIFIED], mapped

    # 没有证据 → unknown，绝不是 verified。
    none_verdict = verification_from_evidence(None)
    assert none_verdict["execution_status"] == EXECUTION_STATUS_UNKNOWN, none_verdict
    assert none_verdict["execution_verified"] is False, none_verdict

    # 旧行（无流水）→ unknown + legacy 来源，**不**升级。
    legacy = legacy_verification(has_fill_rows=False)
    assert legacy["execution_status"] == EXECUTION_STATUS_UNKNOWN, legacy
    assert legacy["execution_verified"] is False, legacy
    assert legacy["execution_evidence_source"] == EVIDENCE_SOURCE_LEGACY, legacy

    # 谓词必须是 fail closed 的：NULL 行被排除。
    assert "COALESCE(execution_verified, 0) = 1" in VERIFIED_PREDICATE
    assert "execution_status = 'verified'" in VERIFIED_PREDICATE

    report = gate_report([
        {"execution_status": EXECUTION_STATUS_VERIFIED},
        {"execution_status": EXECUTION_STATUS_UNKNOWN},
        {"execution_status": None},
        {"execution_status": "nonsense"},
    ])
    assert report["total"] == 4, report
    assert report["verified"] == 1, report
    assert report["blocked_from_execution_stats"] == 3, report
    print("execution_verification self-check: ok")


if __name__ == "__main__":
    _self_check()
