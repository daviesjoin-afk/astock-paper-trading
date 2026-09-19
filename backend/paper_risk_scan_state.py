# -*- coding: utf-8 -*-
"""cycle-owned 的风险扫描运行生命周期状态（R16）。

不变量::

    Every risk scan run belongs to exactly one
    (cycle_id, asof_date, scan_minute) identity.

    Cycle identity is resolved once and never re-guessed after external I/O.

本模块存在的理由
----------------
在 R16 之前，"这次分钟级风险扫描是否已经跑过"是由 ``paper_audit`` 里一条
``event='risk_scan_state'`` 的 JSON 标记决定的，而那条标记的 key **只有机器分钟**：

    SELECT event,detail FROM paper_audit
     WHERE event='risk_scan_state' AND detail LIKE '%"scan_minute": "14:50"%'

这带来三个真实缺陷：

1. **跨周期同分钟互相抑制**：14:50:05 周期 8 完成扫描，14:50:30 翻到周期 9，
   周期 9 会读到周期 8 的同分钟 completed 标记并直接 ``already_scanned`` ——
   周期 9 的持仓因此**一次风控都没跑**。
2. **同分钟不同 asof 碰撞**：人工 replay 在同一分钟依次跑 ``asof=2026-09-10``
   与 ``asof=2026-09-11``，后者被前者的标记抑制 —— 它们是两个不同的业务扫描。
3. **身份被计算两次**：``monitor_risk`` 与 ``_monitor_risk_impl`` 各自
   ``dt.datetime.now()`` 取一次分钟，跨分钟边界时 running 标记与 failed 标记
   落在不同身份上，留下**永远不会有对应 failed 转换的 orphan running**。

更根本的问题是 authority 泄漏：``paper_audit`` 本该只是 human-readable audit
trail，却被当成了"是否执行真实风险订单"的**控制状态**。

本模块把 scan run 生命周期变成 durable、cycle-owned 的状态：

* 身份是 ``(cycle_id, asof_date, scan_minute)``，三者缺一不可；
* 身份一经插入**不可更改**（由 migration 的 trigger 强制）；
* 只有本模块写 ``paper_risk_scan_runs``，``paper_trading`` 不得直接 CRUD；
* 调用方可以在外部 I/O 之后用 :func:`assert_cycle_active` 把已认领的周期
  重新验一遍 —— 周期变了就 fail closed，绝不偷偷改用新周期继续跑。

硬边界（由 ``test_paper_trading_architecture_guard.py`` 静态强制）
------------------------------------------------------------------
* 不 import ``paper_trading``（依赖方向单向：``paper_trading`` → 本模块）；
* **零 wall clock**：不出现 ``date.today`` / ``datetime.now`` / ``time.time``；
  所有时间戳（``started_at`` / ``finished_at``）一律由调用方显式传入，
  这样身份不可能自己偷偷漂移；
* **零事务所有权**：不 ``commit`` / ``rollback`` / ``BEGIN`` / ``SAVEPOINT``；
  事务由调用方（``paper_trading.monitor_risk``）持有；
* 只读写本模块自己的表，不碰 ``paper_positions`` 投影、不建周期、不解析策略。
"""
from __future__ import annotations

import json
import sqlite3

__all__ = [
    "SCAN_RUN_TABLE",
    "SCAN_STATUSES",
    "ACTIVE_CYCLE_STATUSES",
    "RiskScanStateConflict",
    "RiskScanCycleChanged",
    "claim_scan",
    "complete_scan",
    "fail_scan",
    "scan_run",
    "assert_cycle_active",
]

#: durable 运行表名（DDL 由 ``paper_schema_migrations`` 的 v21 持有）。
SCAN_RUN_TABLE = "paper_risk_scan_runs"

#: 合法状态。与 migration 的 ``CHECK`` 约束共用一份事实。
SCAN_STATUSES = ("running", "completed", "failed")

#: "当前 active cycle"的判据。与 ``paper_position_read_model.active_cycle_id``
#: 完全一致（同一条 SQL），但本模块刻意自己持有这一条极窄只读查询 ——
#: 不 import 读模型可以避免 ``risk scan state → read model → ...`` 的无谓依赖链。
ACTIVE_CYCLE_STATUSES = ("draft", "running", "paused")


class RiskScanStateConflict(RuntimeError):
    """CAS 更新没有命中唯一一行 —— 状态转换必须精确落在 exact identity 上。

    典型场景：``complete_scan`` 时该 identity 已不在 ``running``（被并发入口
    改成 completed/failed，或身份根本不存在）。**绝不** silent success。
    """


class RiskScanCycleChanged(RuntimeError):
    """已认领的周期不再是当前 active cycle —— 本次扫描必须 fail closed。"""


def _json(value):
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _identity(cycle_id, asof_date, scan_minute):
    """规整并校验三元身份；任一缺失即 fail fast。

    身份不允许"猜"：``cycle_id`` 为 ``None`` 说明调用方还没解析出周期，
    ``asof_date`` / ``scan_minute`` 为空说明身份不完整 —— 三者都必须显式给出。
    """
    if cycle_id is None:
        raise ValueError("risk scan identity 需要显式 cycle_id")
    try:
        cycle_id = int(cycle_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"risk scan identity 的 cycle_id 非法: {cycle_id!r}") from exc
    asof = str(asof_date or "").strip()
    if not asof:
        raise ValueError("risk scan identity 需要显式 asof_date")
    minute = str(scan_minute or "").strip()
    if not minute:
        raise ValueError("risk scan identity 需要显式 scan_minute")
    return cycle_id, asof, minute


def _row_dict(row):
    return dict(row) if row is not None else None


def scan_run(conn, *, cycle_id, asof_date, scan_minute):
    """读取 exact identity 的运行行（只读）；不存在返回 ``None``。"""
    identity = _identity(cycle_id, asof_date, scan_minute)
    row = conn.execute(
        f"SELECT * FROM {SCAN_RUN_TABLE}"
        " WHERE cycle_id=? AND asof_date=? AND scan_minute=?",
        identity,
    ).fetchone()
    return _row_dict(row)


def claim_scan(conn, *, cycle_id, asof_date, scan_minute, started_at):
    """认领一次风险扫描；返回 ``{"claimed", "state", "attempt"}``。

    语义（不得调整）::

        身份不存在                → INSERT running attempt=1   → claimed=True  state="claimed"
        身份已 completed          → 不改                      → claimed=False state="completed"
        身份仍 running            → 不改                      → claimed=False state="running"
        身份 failed               → running, attempt+=1       → claimed=True  state="retry"
                                    （started_at 更新，finished_at/error 清空）

    ``failed`` 可重试是刻意的：provider / ledger 异常不该让同一分钟内的直接
    重试被当成 ``already_scanned`` 吞掉。
    """
    identity = _identity(cycle_id, asof_date, scan_minute)
    started = str(started_at or "").strip()
    if not started:
        raise ValueError("claim_scan 需要显式 started_at")

    inserted = conn.execute(
        f"INSERT INTO {SCAN_RUN_TABLE}"
        "(cycle_id,asof_date,scan_minute,status,attempt,started_at,finished_at,error,detail)"
        " VALUES(?,?,?,'running',1,?,NULL,NULL,'{}')"
        " ON CONFLICT(cycle_id,asof_date,scan_minute) DO NOTHING",
        (*identity, started),
    )
    if inserted.rowcount == 1:
        return {"claimed": True, "state": "claimed", "attempt": 1}

    row = scan_run(conn, cycle_id=identity[0], asof_date=identity[1], scan_minute=identity[2])
    if row is None:
        # 唯一冲突后却读不到行：只可能是并发删除。当作冲突上报，绝不静默放行。
        raise RiskScanStateConflict("claim_scan 冲突后无法读回 scan run 行")
    status = str(row["status"])
    if status == "failed":
        attempt = int(row["attempt"] or 0) + 1
        updated = conn.execute(
            f"UPDATE {SCAN_RUN_TABLE}"
            " SET status='running', attempt=attempt+1, started_at=?,"
            "     finished_at=NULL, error=NULL"
            " WHERE cycle_id=? AND asof_date=? AND scan_minute=? AND status='failed'",
            (started, *identity),
        )
        if updated.rowcount == 1:
            return {"claimed": True, "state": "retry", "attempt": attempt}
        # 并发入口抢先把 failed 变成了 running：尊重对方，不重复认领。
        row = scan_run(conn, cycle_id=identity[0], asof_date=identity[1], scan_minute=identity[2])
        status = str(row["status"]) if row else "running"
        return {"claimed": False, "state": status, "attempt": int(row["attempt"] or 0) if row else 0}
    return {"claimed": False, "state": status, "attempt": int(row["attempt"] or 0)}


def complete_scan(conn, *, cycle_id, asof_date, scan_minute, finished_at, detail=None):
    """把 exact identity 从 ``running`` 推进到 ``completed``。

    必须命中唯一一行（``rowcount == 1``），否则 :class:`RiskScanStateConflict`。
    ``detail`` 只承载诊断信息，不参与身份判定。
    """
    identity = _identity(cycle_id, asof_date, scan_minute)
    finished = str(finished_at or "").strip()
    if not finished:
        raise ValueError("complete_scan 需要显式 finished_at")
    updated = conn.execute(
        f"UPDATE {SCAN_RUN_TABLE}"
        " SET status='completed', finished_at=?, error=NULL, detail=?"
        " WHERE cycle_id=? AND asof_date=? AND scan_minute=? AND status='running'",
        (finished, _json(detail), *identity),
    )
    if updated.rowcount != 1:
        raise RiskScanStateConflict(
            f"complete_scan 未命中 running 身份 {identity}（rowcount={updated.rowcount}）"
        )
    return {"state": "completed", "attempt": None}


def fail_scan(conn, *, cycle_id, asof_date, scan_minute, finished_at, error):
    """把 exact identity 从 ``running`` 推进到 ``failed``。

    只更新**同一条**身份行 —— 绝不 INSERT 一条新的 failed 记录来假装状态已经
    转换（那正是 orphan running 的成因）。
    """
    identity = _identity(cycle_id, asof_date, scan_minute)
    finished = str(finished_at or "").strip()
    if not finished:
        raise ValueError("fail_scan 需要显式 finished_at")
    updated = conn.execute(
        f"UPDATE {SCAN_RUN_TABLE}"
        " SET status='failed', finished_at=?, error=?"
        " WHERE cycle_id=? AND asof_date=? AND scan_minute=? AND status='running'",
        (finished, str(error or ""), *identity),
    )
    if updated.rowcount != 1:
        raise RiskScanStateConflict(
            f"fail_scan 未命中 running 身份 {identity}（rowcount={updated.rowcount}）"
        )
    return {"state": "failed"}


def assert_cycle_active(conn, *, cycle_id):
    """确认 ``cycle_id`` 仍是当前 active cycle，否则 fail closed。

    这是"外部 I/O 之后重新验证已认领周期"的 fence。语义与
    :func:`paper_position_read_model.active_cycle_id` 逐字一致（同一条 SQL、
    同样读不到即"无法证明"），但**没有**任何 fallback：不建周期、不改用新周期、
    不返回 ``None`` 让调用方自行解释。

    ``paper_cycles`` 表不存在（极简 schema / 测试库）同样视为无法证明。
    """
    if cycle_id is None:
        raise RiskScanCycleChanged("risk scan 没有已认领的 cycle_id，无法证明周期归属")
    try:
        expected = int(cycle_id)
    except (TypeError, ValueError) as exc:
        raise RiskScanCycleChanged(f"risk scan 的 cycle_id 非法: {cycle_id!r}") from exc
    try:
        row = conn.execute(
            "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise RiskScanCycleChanged(f"无法读取当前 active cycle: {exc}") from exc
    if row is None:
        raise RiskScanCycleChanged("当前没有 active cycle，已认领的 risk scan 无法继续")
    try:
        current = int(row["id"] if hasattr(row, "keys") else row[0])
    except (TypeError, ValueError, IndexError) as exc:
        raise RiskScanCycleChanged("当前 active cycle id 不可解析") from exc
    if current != expected:
        raise RiskScanCycleChanged(
            f"risk scan 已认领 cycle {expected}，但当前 active cycle 是 {current}"
        )
    return current
