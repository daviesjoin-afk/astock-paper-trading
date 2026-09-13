# -*- coding: utf-8 -*-
"""Read model for paper trading risk exit eligibility.

This module determines the set of account IDs eligible for risk-exit evaluation.
Eligibility is defined as the union of:
  1. Base / execution accounts provided by the caller (active/participating scope).
  2. Any account with open/unclosed position lots (remaining_qty > 0) in paper_position_lots.

This ensures accounts that are paused, retired, or archived but still hold
positions continue to be scanned for risk exits until positions are safely closed.

The module is strictly read-only and stdlib-only.  It does not decide cycle
ownership, does not touch accounts or strategy tables, does not interpret
cycle configuration, and does not perform order dispatch or trading.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable


def _raw_account_id(row: Any) -> Any:
    """Extract raw account_id value from dict, tuple, or row object without normalization."""
    if isinstance(row, dict):
        return row.get("account_id")
    if isinstance(row, (tuple, list)):
        return row[0] if row else None
    if hasattr(row, "__getitem__"):
        try:
            return row["account_id"]
        except (TypeError, KeyError, IndexError):
            return row[0] if len(row) > 0 else None
    return getattr(row, "account_id", None)


def risk_exit_account_ids(
    conn: Any,
    base_account_ids: Iterable[Any] | None = None,
    *,
    rows_fn: Callable[[Any, str, tuple[Any, ...]], list[Any]] | None = None,
) -> set[Any]:
    """Return distinct account IDs eligible for risk-exit evaluation.

    The result is the union of caller-supplied ``base_account_ids`` (copied
    directly into a set with original identity) and any accounts currently
    holding lots with ``remaining_qty > 0`` in ``paper_position_lots`` (where
    any truthy raw account_id is added as str(raw_value) without stripping).
    """
    result = set(base_account_ids or ())
    sql = "SELECT DISTINCT account_id FROM paper_position_lots WHERE remaining_qty>0"

    if rows_fn is not None:
        raw_rows = rows_fn(conn, sql, ())
    elif conn is not None and hasattr(conn, "execute"):
        cursor = conn.execute(sql)
        raw_rows = cursor.fetchall()
    else:
        raw_rows = []

    for row in raw_rows:
        value = _raw_account_id(row)
        if value:
            result.add(str(value))

    return result
