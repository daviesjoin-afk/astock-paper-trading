# -*- coding: utf-8 -*-
"""Shared cash arithmetic and the narrow cash-ledger mutation boundary.

Existing arithmetic accepts already-resolved account rows.  The explicit-cycle
read entry delegates economic ownership to its existing owner.  The caller
selects the cycle and owns the connection and transaction; no reservation,
portfolio or execution policy is inferred here.
"""
from __future__ import annotations

import math

import paper_cycle_ownership as PCY


def _default_num(value, default=0.0):
    if isinstance(value, (int, float)) and not math.isnan(float(value)):
        return float(value)
    return default


def _default_now():
    return None


def shared_cash(rows, *, num_fn=_default_num):
    """Return the exact cash total for the already-resolved rows."""
    return sum(num_fn(row.get("cash")) for row in rows)


def cycle_account_rows(conn, cycle_id, *, builtin_scope):
    """Read economic account rows for an explicitly selected cycle.

    Selection and any legacy cycle initialization remain the caller's job.
    Lifecycle status is not an economic ownership filter.  The supplied
    connection is used as-is and this function never commits it.
    """
    if cycle_id is None:
        raise ValueError("explicit_cycle_id_required")
    return PCY.cycle_ledger_rows(conn, cycle_id, builtin_scope=builtin_scope)


def cycle_shared_cash(conn, cycle_id, *, builtin_scope, num_fn=_default_num):
    """Sum the economic owners' cash in the caller's transaction snapshot.

    This is gross cash, not available buying power: open reservations, including
    those from other cycles, must still be accounted for by their owner.
    """
    return shared_cash(cycle_account_rows(conn, cycle_id, builtin_scope=builtin_scope),
                       num_fn=num_fn)


def shared_initial_cash(rows, declared_capital=None, *, num_fn=_default_num):
    """Return declared capital or the existing per-row initial-cash fallback."""
    declared = num_fn(declared_capital)
    if declared > 0:
        return declared
    return max(
        sum(
            num_fn(row["initial_cash"] if "initial_cash" in row else row.get("cash"))
            for row in rows
        ),
        0.0,
    )


def debit_shared_cash(
    conn,
    rows,
    amount,
    preferred_account_id=None,
    *,
    now_fn=_default_now,
    num_fn=_default_num,
):
    """Debit the supplied cash rows while retaining per-account audit ownership."""
    amount = max(0.0, num_fn(amount))
    ordered_rows = list(rows)
    ordered_rows.sort(
        key=lambda row: (
            0 if row["id"] == preferred_account_id else 1,
            -num_fn(row.get("cash")),
        )
    )
    if amount > sum(num_fn(row.get("cash")) for row in ordered_rows) + 1e-6:
        raise ValueError("共享资金池可用现金不足")
    remaining = amount
    for row in ordered_rows:
        debit = min(max(0.0, num_fn(row.get("cash"))), remaining)
        if debit:
            conn.execute(
                "UPDATE paper_accounts SET cash=cash-?,updated_at=? WHERE id=?",
                (debit, now_fn(), row["id"]),
            )
            remaining -= debit
        if remaining <= 1e-6:
            break
    return True


def credit_shared_cash(
    conn,
    amount,
    account_id,
    *,
    cycle_id=None,
    now_fn=_default_now,
    num_fn=_default_num,
):
    """Credit proceeds to the account that owns the position.

    Sell proceeds can only go back to the account holding the position, so this
    deliberately does **not** require the account to be an economic owner of the
    requested cycle: archived sleeves and accounts that no longer participate in
    the current cycle still have to execute risk exits, and blocking their
    proceeds strands real cash (it turned genuine risk exits into
    ``execution_retry`` in practice).

    The consistency rule that does hold is "the account's durable cycle equals
    the order's cycle" whenever both are known. An unknown cycle (``NULL`` in
    historical data) is not a conflict: the position owner remains the authority
    for the proceeds, and withholding the credit is the bigger ledger error.
    """
    row = conn.execute(
        "SELECT cycle_id FROM paper_accounts WHERE id=?", (account_id,),
    ).fetchone()
    if row is None:
        raise ValueError("credit_account_unknown")
    account_cycle = row[0]
    if cycle_id is not None and account_cycle is not None:
        if int(account_cycle) != int(cycle_id):
            raise ValueError("credit_account_cycle_mismatch")
    amount = num_fn(amount)
    if amount < 0:
        raise ValueError(
            f"_credit_shared_cash 收到负数金额 {amount}（account={account_id}），疑似上游计算错误"
        )
    conn.execute(
        "UPDATE paper_accounts SET cash=cash+?,updated_at=? WHERE id=?",
        (amount, now_fn(), account_id),
    )
