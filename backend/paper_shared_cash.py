# -*- coding: utf-8 -*-
"""Shared cash arithmetic and the narrow cash-ledger mutation boundary.

This module deliberately accepts already-resolved account rows.  The caller
owns membership, ordering of the input rows, cycle selection, and all other
portfolio or execution policy.  Runtime numeric and clock functions are
injected by the compatibility facade so monkeypatches remain effective.
"""
from __future__ import annotations

import math


def _default_num(value, default=0.0):
    if isinstance(value, (int, float)) and not math.isnan(float(value)):
        return float(value)
    return default


def _default_now():
    return None


def shared_cash(rows, *, num_fn=_default_num):
    """Return the exact cash total for the already-resolved rows."""
    return sum(num_fn(row.get("cash")) for row in rows)


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
    now_fn=_default_now,
    num_fn=_default_num,
):
    """Credit proceeds to the explicitly supplied account only."""
    amount = num_fn(amount)
    if amount < 0:
        raise ValueError(
            f"_credit_shared_cash 收到负数金额 {amount}（account={account_id}），疑似上游计算错误"
        )
    conn.execute(
        "UPDATE paper_accounts SET cash=cash+?,updated_at=? WHERE id=?",
        (amount, now_fn(), account_id),
    )
