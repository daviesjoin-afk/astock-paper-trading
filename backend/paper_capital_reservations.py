"""Runtime BUY buying-power reservations.

This module owns only the reservation ledger runtime operations.  It does not
own shared cash, positions, slots, orders, cycles, or transaction boundaries.
All runtime dependencies are supplied by the caller so compatibility facades
can observe call-time monkeypatches and application state.
"""
from __future__ import annotations


def _as_dict(cursor, row):
    if row is None:
        return None
    columns = [item[0] for item in cursor.description or ()]
    return dict(zip(columns, row, strict=True))


def _query_one(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    return _as_dict(cursor, cursor.fetchone())


def _as_int(value):
    """把存储值转成 ``int``；不可解析时返回 ``None``。

    刻意**不**在失败时回退到任何默认周期：``None`` 在这里的含义是「无法证明」，
    而无法证明的周期归属必须让调用方 fail closed，不能悄悄变成「与订单一致」。
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _query_all(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    return [_as_dict(cursor, row) for row in cursor.fetchall()]


def pending_buy_reservations(conn, cycle_id=None, exclude_order_key=None, *, num_fn):
    """Aggregate all currently reserved BUY reservations.

    ``cycle_id`` remains a compatibility parameter intentionally ignored:
    reserved orders from an older cycle still consume the real shared pool.
    """
    params = []
    where = "side='buy' AND status='reserved'"
    if exclude_order_key is not None:
        where += " AND order_key<>?"
        params.append(str(exclude_order_key))
    rows = _query_all(
        conn,
        f"""SELECT account_id,COALESCE(SUM(amount+fees),0) AS amount
            FROM paper_capital_reservations WHERE {where}
            GROUP BY account_id""",
        tuple(params),
    )
    by_account = {
        row["account_id"]: max(0.0, num_fn(row.get("amount")))
        for row in rows
    }
    return by_account, sum(by_account.values())


def reserve_shared_capital(
    conn,
    order_key,
    account_id,
    code,
    amount,
    fees=0.0,
    *,
    num_fn,
    now_fn,
    shared_cash_fn,
    active_cycle_fn,
    expected_cycle_id=None,
):
    """Create or resize a BUY reservation without owning the transaction.

    ``expected_cycle_id``（§20–§22）：调用方已知这张订单的周期归属时传入，本函数
    据此校验**已存在**的预占行没有被记到别的周期上。

    为什么必须校验而不是直接 ``UPDATE amount/fees``：预占行是共享资金池的占用凭证，
    它的 ``cycle_id`` 与订单的周期是同一笔经济事实的两半。如果订单属于 cycle 8 而
    预占行记在 cycle 9，那么"调整金额"本身就已经把 cycle 9 的资金额度改成了 cycle 8
    的委托所需的规模 —— 另一种口径的账本错配，且订单行与成交行都看不出异常。

    冲突与缺失一律 fail closed，**绝不**改写 ``cycle_id`` 来"修正"：周期归属是不可变
    事实，修法是让上层把这张订单终态化，而不是重写历史。
    """
    order_key = str(order_key)
    amount = max(0.0, num_fn(amount))
    fees = max(0.0, num_fn(fees))
    existing = _query_one(
        conn,
        "SELECT status,cycle_id FROM paper_capital_reservations WHERE order_key=?",
        (order_key,),
    )
    if existing and existing["status"] == "consumed":
        return False, "该订单资金预占已经消费，禁止重复成交"
    if existing and expected_cycle_id is not None:
        reserved_cycle = _as_int(existing.get("cycle_id"))
        if reserved_cycle != int(expected_cycle_id):
            return False, (
                "reservation_cycle_mismatch: 预占周期 "
                f"{reserved_cycle!r} 与订单周期 {int(expected_cycle_id)} 不一致，"
                "拒绝改写预占（周期归属不可变）"
            )

    _, pending_total = pending_buy_reservations(
        conn, exclude_order_key=order_key, num_fn=num_fn
    )
    available_cash = shared_cash_fn(conn) - pending_total
    if amount + fees > available_cash + 1e-6:
        return False, f"待成交买单已预占 ¥{pending_total:,.2f}，共享可用现金不足"

    if existing:
        conn.execute(
            """UPDATE paper_capital_reservations
               SET status='reserved',released_at=NULL,amount=?,fees=?,created_at=?
               WHERE order_key=?""",
            (amount, fees, now_fn(), order_key),
        )
        return True, None

    cycle = active_cycle_fn(conn)
    conn.execute(
        """INSERT INTO paper_capital_reservations
           (cycle_id,order_key,account_id,code,side,amount,fees,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (cycle["id"], order_key, account_id, code, "buy", amount, fees,
         "reserved", now_fn()),
    )
    return True, None


def finish_capital_reservation(conn, order_key, status, *, now_fn):
    """Finish one reserved row; terminal transitions are idempotent no-ops."""
    if status not in {"consumed", "released"}:
        raise ValueError("非法资金预占状态")
    conn.execute(
        """UPDATE paper_capital_reservations
           SET status=?,released_at=?
           WHERE order_key=? AND status='reserved'""",
        (status, now_fn(), str(order_key)),
    )
