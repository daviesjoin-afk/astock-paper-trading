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


def insert_order(
    conn,
    *,
    account_id,
    side,
    code,
    status,
    reason,
    risk_payload,
    origin,
    created_at,
    signal_id=None,
    name=None,
    qty=0,
    planned_price=None,
    strategy_id=None,
    strategy_version=None,
    strategy_checksum=None,
    retry_of_order_id=None,
    cycle_id=None,
):
    """创建一条订单行，返回新行 id。

    列集合与原 INSERT 逐字一致；业务侧（策略戳、重试血缘、payload 序列化）
    全部由调用方算好再传进来 —— owner 不算价、不解析周期、不读时钟。
    """
    cursor = conn.execute(
        """INSERT INTO paper_orders(
               account_id,signal_id,side,code,name,qty,planned_price,status,reason,
               risk_payload,origin,created_at,strategy_id,strategy_version,strategy_checksum,
               retry_of_order_id,cycle_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            account_id, signal_id, side, code, name, qty, planned_price, status, reason,
            risk_payload, origin, created_at,
            strategy_id, strategy_version, strategy_checksum,
            retry_of_order_id, cycle_id,
        ),
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


def signal_ids_by_reason_marker(conn, marker: str) -> list[int]:
    """只读辅助：按原因标记反查需要一并回收的信号 id。"""
    return [
        int(row[0]) for row in conn.execute(
            """SELECT signal_id FROM paper_orders
               WHERE status='superseded' AND side='buy' AND signal_id IS NOT NULL
                 AND reason LIKE ?""",
            (f"%{marker}%",),
        )
    ]
