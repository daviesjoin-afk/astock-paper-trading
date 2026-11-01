"""订单行持久化 owner（PR-10 工作包 A 起建）。

本模块只做一件事：**把调用方已判定的订单行事实写进 `paper_orders`**。
创建、状态迁移、成交结果字段、核验戳与归档删除最终都从这里出，
`paper_orders` 因此只有一个持久化入口。

边界（与 `docs/LEDGER_OWNERSHIP.md` 第16节一致）：

* 不决定"这笔委托能不能下、该不该终止" —— 那是各阶段的业务判定者；
* 不决定顺序、不提交、不回滚、不吞异常 —— 事务归属调用方同一连接与同一事务；
* 不读时钟 —— 时间戳一律由调用方传入；
* 不算成交价、费用、可卖量、BUY 数量 —— 成交判定唯一属于
  `execution_planner.commit_fill`，本模块只落库它给出的结果。

工作包 A 覆盖的是**非成交生命周期**：过期、等待、替代、终止与重试接管。
创建与成交结果写入在工作包 B 里接入同一入口。
"""
from __future__ import annotations

from typing import Sequence

import entry_lifecycle as ELC

__all__ = [
    "ORDER_LEDGER_VERSION",
    "accumulate_realized_pnl",
    "record_execution_block",
    "record_fill_result",
    "record_realized_pnl",
    "stamp_execution",
    "stamp_execution_rows",
    "update_order_outcome",
    "update_risk_payload",
    "TERMINAL_ORDER_STATUSES",
    "expire_order",
    "insert_order",
    "retire_duplicate_waitlist_orders",
    "retire_gated_order",
    "cancel_order",
    "supersede_duplicate_execution_retry",
    "supersede_previous_execution_retry",
    "supersede_for_expired_signal",
    "supersede_for_terminal_signal",
    "supersede_order",
    "terminate_order",
    "nonfilled_order_count",
]

ORDER_LEDGER_VERSION = "paper-order-ledger-v1"

#: 允许写入的终态集合。``terminate_order`` 接受状态参数，但只接受这里的取值，
#: 避免退化成"任意 set_status"。
TERMINAL_ORDER_STATUSES = (
    "expired",
    "cancelled",
    "superseded",
    "risk_rejected",
    "rejected",
)


def expire_order(conn, order_id, *, reason, cancelled_at):
    """订单自身过期：写 expired，原因追加、取消时间取调用方给的值。"""
    conn.execute(
        """UPDATE paper_orders
                  SET status=?,reason=COALESCE(reason,'') || '；' || ?,cancelled_at=?
                WHERE id=?""",
        ("expired", reason, cancelled_at, order_id),
    )


#: 订单行的规范列集合（与 schema 一致）。``insert_order`` 只接受这里的列名，
#: 调用方不能凭空插入未知列；未提供的列走 schema 默认值。
ORDER_INSERT_COLUMNS = (
    "account_id", "signal_id", "side", "code", "name", "qty", "planned_price",
    "order_type", "filled_price", "amount", "fees", "status", "reason",
    "risk_payload", "origin", "created_at", "executed_at", "expires_at",
    "strategy_id", "strategy_version", "strategy_checksum",
    "retry_of_order_id", "cycle_id",
    "allocation_intent_kind", "portfolio_snapshot_id", "allocation_plan_id",
    "allocation_plan_fingerprint", "allocation_policy_version",
)


def record_execution_block(
    conn, order_id, *, status, execution_asof, reasons, evidence,
    ruleset_version, expected_version,
):
    """执行阻塞结果写库（乐观锁版本不符则不写），返回受影响行数。

    判定（阻塞码、目标状态、规则版本）由成交权威给出；owner 只落库。
    """
    return conn.execute(
        """UPDATE paper_orders
              SET status=?,execution_asof=?,execution_reasons=?,
                  execution_evidence=?,ruleset_version=?,execution_version=execution_version+1,
                  remaining_qty=MAX(0,qty-COALESCE(filled_qty,0))
            WHERE id=? AND execution_version=?""",
        (status, execution_asof, reasons, evidence,
         ruleset_version, order_id, expected_version),
    ).rowcount


def record_fill_result(
    conn, order_id, *, status, filled_qty, remaining_qty, filled_price, amount, fees,
    reason, risk_payload, execution_asof, execution_reasons, execution_evidence,
    pricing_basis, slippage, ruleset_version, executed_at, expected_version,
    expected_filled_qty, min_remaining,
):
    """成交结果写库：状态、成交数量/均价/金额/费用、证据与版本推进。

    五条 ``WHERE`` 条件逐字保留（版本、已成交数量、剩余量下限、活动状态集合），
    并发或终态竞争时返回 0，由调用方抛错。价格与费用由 ``commit_fill`` 算好，
    本入口不重算任何成交事实。
    """
    return conn.execute(
        """UPDATE paper_orders
              SET status=?,filled_qty=?,remaining_qty=?,filled_price=?,amount=?,fees=?,
                  reason=?,risk_payload=?,execution_asof=?,execution_reasons=?,
                  execution_evidence=?,pricing_basis=?,slippage=?,ruleset_version=?,
                  executed_at=?,execution_version=execution_version+1
            WHERE id=? AND execution_version=?
              AND COALESCE(filled_qty,0)=? AND COALESCE(remaining_qty,qty-COALESCE(filled_qty,0))>=?
              AND status IN ('pending_execution','partially_filled','ready_to_fill',
                             'pending_limit','pending_verification','execution_retry',
                             'manual_execution_retry')""",
        (status, filled_qty, remaining_qty, filled_price, amount, fees,
         reason, risk_payload, execution_asof, execution_reasons,
         execution_evidence, pricing_basis, slippage, ruleset_version,
         executed_at, order_id, expected_version, expected_filled_qty, min_remaining),
    ).rowcount


def accumulate_realized_pnl(conn, order_id, *, delta):
    """在一笔已有 realized_pnl 上累加本次卖出实现盈亏。"""
    conn.execute(
        "UPDATE paper_orders SET realized_pnl=COALESCE(realized_pnl,0)+? WHERE id=?",
        (delta, order_id),
    )


def stamp_execution(conn, order_id, *, status, verified, source):
    """写入执行核验结论的三列；结论由调用方（核验 owner）算好。"""
    conn.execute(
        "UPDATE paper_orders SET execution_status=?, execution_verified=?,"
        " execution_evidence_source=? WHERE id=?",
        (status, int(bool(verified)), source, order_id),
    )


def stamp_execution_rows(conn, rows):
    """批量写核验结论：``rows`` 为 ``(status, verified, source, order_id)`` 序列。"""
    data = [(status, int(bool(verified)), source, order_id)
            for status, verified, source, order_id in rows]
    if not data:
        return 0
    conn.executemany(
        "UPDATE paper_orders SET execution_status=?, execution_verified=?,"
        " execution_evidence_source=? WHERE id=?",
        data,
    )
    return len(data)


def record_realized_pnl(conn, order_id, *, realized):
    """回填一笔订单的已实现盈亏（口径由调用方决定）。"""
    conn.execute(
        "UPDATE paper_orders SET realized_pnl=? WHERE id=?",
        (realized, order_id),
    )


#: ``update_order_outcome`` 允许写入的订单状态（调用方判定结果的白名单）。
ORDER_OUTCOME_STATUSES = (
    "pending_limit", "pending_execution", "risk_rejected", "expired", "cancelled",
    "superseded", "execution_retry", "manual_execution_retry", "entry_frozen_waitlist",
    "ready_to_fill", "filled", "partially_filled",
)

_UNSET = object()


def update_order_outcome(
    conn, order_id, *, status=_UNSET, reason=_UNSET, risk_payload=_UNSET,
    clear_fill=False, match_status=None,
):
    """订单状态推进：按调用方给出的字段写 row，返回受影响行数。

    只允许写 ``status`` / ``reason`` / ``risk_payload``，外加"清空成交字段"这一
    个固定组合（``clear_fill``）。``status`` 必须在 :data:`ORDER_OUTCOME_STATUSES`
    白名单内，避免退化成任意列/任意值的 set_status。
    """
    sets: list[str] = []
    params: list = []
    if status is not _UNSET:
        if status not in ORDER_OUTCOME_STATUSES:
            raise ValueError(f"非法订单状态：{status!r}")
        sets.append("status=?")
        params.append(status)
    if reason is not _UNSET:
        sets.append("reason=?")
        params.append(reason)
    if risk_payload is not _UNSET:
        sets.append("risk_payload=?")
        params.append(risk_payload)
    if clear_fill:
        sets.append("filled_price=NULL")
        sets.append("amount=NULL")
        sets.append("fees=NULL")
        sets.append("executed_at=NULL")
    if not sets:
        raise ValueError("update_order_outcome 需要至少一个字段")
    sql = "UPDATE paper_orders SET " + ", ".join(sets) + " WHERE id=?"
    params.append(order_id)
    if match_status is not None:
        states = tuple(match_status)
        if not states:
            raise ValueError("match_status 需要非空集合")
        sql += " AND status IN (" + ",".join("?" for _ in states) + ")"
        params.extend(states)
    return conn.execute(sql, tuple(params)).rowcount


def update_risk_payload(conn, order_id, *, payload, expected_version):
    """按乐观锁版本改写订单的 risk_payload；版本不符则不写。"""
    return conn.execute(
        "UPDATE paper_orders SET risk_payload=? WHERE id=? AND execution_version=?",
        (payload, order_id, expected_version),
    ).rowcount


def insert_order(conn, *, fields):
    """创建一条订单行，返回新行 id。

    ``fields`` 的键必须落在 :data:`ORDER_INSERT_COLUMNS` 里；列序按该常量，
    而不是调用方给的顺序 —— 写入形状由 owner 决定。业务侧（策略戳、重试血缘、
    payload 序列化、计划快照）全部由调用方算好再传进来：owner 不算价、不解析
    周期、不读时钟。
    """
    unknown = set(fields) - set(ORDER_INSERT_COLUMNS)
    if unknown:
        raise ValueError(f"未知订单列：{sorted(unknown)}")
    columns = [name for name in ORDER_INSERT_COLUMNS if name in fields]
    if not columns:
        raise ValueError("insert_order 需要至少一个字段")
    marks = ",".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO paper_orders({','.join(columns)}) VALUES({marks})",
        tuple(fields[name] for name in columns),
    )
    return int(cursor.lastrowid)


def supersede_order(conn, order_id, *, reason, cancelled_at):
    """取消/替代：写 superseded，原因追加、取消时间取调用方给的值。"""
    conn.execute(
        """UPDATE paper_orders
              SET status='superseded',
                  reason=COALESCE(reason,'') || '；' || ?,cancelled_at=?
            WHERE id=?""",
        (reason, cancelled_at, order_id),
    )


def terminate_order(conn, order_id, *, status, reason, cancelled_at):
    """订单终态写入：``status`` 只能取 :data:`TERMINAL_ORDER_STATUSES`。

    这是 ``execution_dispatch._terminate`` 的写入出口 —— 判定（终止成什么状态、
    要不要连带处理信号、要不要释放预占）仍在派发层。
    """
    if status not in TERMINAL_ORDER_STATUSES:
        raise ValueError(f"非法订单终态：{status!r}")
    conn.execute(
        """UPDATE paper_orders
              SET status=?,reason=COALESCE(reason,'') || '；' || ?,cancelled_at=?
            WHERE id=?""",
        (status, reason, cancelled_at, order_id),
    )


def supersede_for_expired_signal(conn, order_id, *, cancelled_at):
    """失效信号名下的活动买单回收；保持原状态集合与固定原因。"""
    conn.execute(
        """UPDATE paper_orders
                  SET status='superseded',reason=COALESCE(reason,'') || '；信号已失效，委托回收',
                      cancelled_at=?
                WHERE id=? AND status IN (?,?,?,?,?)""",
        (cancelled_at, order_id, *ELC.RECOVERABLE_ORDER_STATUSES),
    )


def supersede_duplicate_execution_retry(conn, order_id):
    """同一信号只保留最新执行重试：退役旧重试行。

    调用方已按 ``signal_id,id DESC`` 判定"哪条算最新"，并负责随后的预占释放；
    本入口只做原状态迁移，``WHERE`` 条件保证同一行不会被重复改写。
    """
    conn.execute(
        """UPDATE paper_orders
              SET status='superseded',
                  reason=COALESCE(reason,'') || '；同一信号仅保留最新执行重试'
            WHERE id=? AND status='execution_retry'""",
        (order_id,),
    )


def supersede_previous_execution_retry(conn, order_id):
    """新一轮执行重试接管：退役上一条重试行（原因追加）。

    与对账里的"保留最新一条"不同：这里是发起新重试前把**全部**旧行退掉，
    判定与顺序由调用方负责，owner 只落库。
    """
    conn.execute(
        """UPDATE paper_orders
              SET status='superseded',
                  reason=COALESCE(reason,'') || '；新一轮执行重试已接管'
            WHERE id=? AND status='execution_retry'""",
        (order_id,),
    )


def cancel_order(conn, order_id, *, reason, cancelled_at, statuses: Sequence[str]):
    """用户撤单：只对传入的可撤销状态生效，返回受影响行数。

    调用方用返回值判断"是否真的撤销了一条"（已成交/已终态时返回 0）；状态集合
    由调用方给出，owner 不自己拼词表。
    """
    states = tuple(statuses)
    if not states:
        raise ValueError("cancel_order 需要非空可撤销状态集合")
    placeholders = ",".join("?" for _ in states)
    return conn.execute(
        f"""UPDATE paper_orders
               SET status='cancelled',reason=?,cancelled_at=?
             WHERE id=? AND status IN ({placeholders})""",
        (reason, cancelled_at, order_id, *states),
    ).rowcount


def supersede_for_terminal_signal(conn, order_id, *, reason, statuses: Sequence[str]):
    """回收关联终态信号的遗留活动委托；状态集合由调用方传入（原词表）。

    ``statuses`` 是业务词表而不是任意过滤条件：调用方传的是
    ``entry_lifecycle.ENTRY_RETRY_ORDER_STATUSES``，本模块不自己拼状态。
    """
    states = tuple(statuses)
    if not states:
        raise ValueError("supersede_for_terminal_signal 需要非空状态集合")
    placeholders = ",".join("?" for _ in states)
    conn.execute(
        f"""UPDATE paper_orders
              SET status='superseded',
                  reason=COALESCE(reason,'') || '；' || ?
            WHERE id=? AND status IN ({placeholders})""",
        (reason, order_id, *states),
    )


def retire_gated_order(conn, order_id, *, reason, statuses: Sequence[str]):
    """回收一条挂起委托（batch / verification 等待）；状态集合由调用方传入。"""
    states = tuple(statuses)
    if not states:
        raise ValueError("retire_gated_order 需要非空状态集合")
    placeholders = ",".join("?" for _ in states)
    conn.execute(
        f"""UPDATE paper_orders
                  SET status='superseded',reason=COALESCE(reason,'') || '；' || ?
                WHERE id=? AND status IN ({placeholders})""",
        (reason, order_id, *states),
    )


def retire_duplicate_waitlist_orders(conn, *, waitlist_status):
    """退役同一账户/标的下被较新记录替代的等待池订单（批量）。

    判定（哪条算较新、用哪两个活动状态）由调用方给出；``EXISTS`` 自连接谓词与
    原因字面量逐字保留。
    """
    conn.execute(
        """UPDATE paper_orders AS older
           SET status='superseded',
               reason=COALESCE(older.reason,'') || '；同标的较新等待池记录已保留'
           WHERE older.side='buy' AND older.status='deferred_capacity'
             AND EXISTS(
                 SELECT 1 FROM paper_orders AS newer
                 WHERE newer.account_id=older.account_id
                   AND newer.code=older.code AND newer.side='buy'
                   AND newer.status IN ('deferred_capacity',?)
                   AND newer.id>older.id
             )""",
        (waitlist_status,),
    )


def nonfilled_order_count(conn, account_id):
    """账户指标的非 filled 委托数；只读、保留原状态过滤与异常边界。"""
    return conn.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND status!='filled'",
        (account_id,),
    ).fetchone()[0]
