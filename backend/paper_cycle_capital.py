"""Read-only fixed-cycle capital attribution calculations.

This module answers only two questions about a fixed cycle's economic ledger:
how much capital is still unallocated, and which funded sleeve capital should
be used as a late-join display reference.  Ownership policy and all runtime
dependencies are supplied by the caller; this module does not own a
transaction, schema, shared cash, or allocation engine.
"""
from __future__ import annotations

from typing import Any, Callable, Collection


def _row_as_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    columns = [item[0] for item in cursor.description or ()]
    return dict(zip(columns, row, strict=True))


def available_cycle_ledger_capital(
    conn: Any,
    cycle: Any,
    account_id: Any,
    *,
    cycle_ledger_filter_fn: Callable[[Any, Any, str], tuple[str, Collection[Any]]],
    builtin_account_ids: Collection[Any],
    num_fn: Callable[..., float],
) -> float:
    """Return fixed-cycle capital not yet attributed to another owner."""
    active_clause, active_ids = cycle_ledger_filter_fn(conn, cycle["id"], "id")
    cursor = conn.execute(
        f"SELECT COALESCE(SUM(initial_cash),0) s,COUNT(*) n "
        f"FROM paper_accounts "
        f"WHERE cycle_id=? AND id<>? AND {active_clause}",
        (cycle["id"], account_id, *active_ids),
    )
    allocated = _row_as_dict(cursor, cursor.fetchone())
    if int(allocated["n"] or 0) <= 0:
        return num_fn(cycle["capital"], 0.0) / max(len(builtin_account_ids), 1)
    existing_initial = num_fn(allocated["s"])
    return max(0.0, num_fn(cycle["capital"], 0.0) - existing_initial)


def late_join_reference_capital(
    conn: Any,
    cycle: Any,
    account_id: Any,
    *,
    cycle_ledger_filter_fn: Callable[[Any, Any, str], tuple[str, Collection[Any]]],
    builtin_account_ids: Collection[Any],
    num_fn: Callable[..., float],
) -> float:
    """Return display-only average funded capital for a late-join sleeve."""
    active_clause, active_ids = cycle_ledger_filter_fn(conn, cycle["id"], "id")
    cursor = conn.execute(
        f"""SELECT COALESCE(SUM(initial_cash),0) s, COUNT(*) n
             FROM paper_accounts
             WHERE cycle_id=?
               AND id<>?
               AND initial_cash>0
               AND {active_clause}""",
        (cycle["id"], account_id, *active_ids),
    )
    funded = _row_as_dict(cursor, cursor.fetchone())
    if int(funded["n"] or 0) > 0 and num_fn(funded["s"]) > 0:
        return round(num_fn(funded["s"]) / int(funded["n"]), 2)
    return round(
        num_fn(cycle["capital"], 0.0) / max(len(builtin_account_ids), 1),
        2,
    )
