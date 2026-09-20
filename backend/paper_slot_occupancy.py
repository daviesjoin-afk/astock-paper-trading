# -*- coding: utf-8 -*-
"""Read model for pending position slot occupancy in paper trading.

This module provides a pure read projection over caller-provided positions and
executable pending BUY orders to determine distinct (account_id, code) seats
held in the simulated portfolio.

It deliberately excludes capital reservations, dynamic position limits,
order dispatch, runner lifecycle, and execution gates.
"""
from __future__ import annotations

from typing import Any, Callable, Collection, Iterable


def pending_position_slots(
    conn: Any,
    positions: Iterable[dict[str, Any]],
    exclude_order_key: Any = None,
    *,
    cycle_id: Any = None,
    occupying_statuses: Collection[str],
    lot_size: int,
    num_fn: Callable[[Any], float | int],
    rows_fn: Callable[[Any, str, tuple[Any, ...]], list[dict[str, Any]]],
) -> set[tuple[str, str]]:
    """Return distinct (account_id, code) slots held by executable pending buy orders.

    Only orders matching origin IN ('manual', 'strategy'), side='buy', and status in
    occupying_statuses occupy new position seats.  Any pending order for an
    account/code pair that already holds a full-lot position (>= lot_size) is suppressed
    because it adds to an existing position rather than claiming a new position slot.

    ``cycle_id`` 为 ``None`` 时保持历史语义（不过滤周期，供只读面板与兼容调用）；
    传显式周期时只统计**该周期**的委托。一旦席位比较固定到某个周期，它读到的
    pending 席位也必须是同一周期的 —— 否则更新的 active cycle 的在途买单会占掉
    被请求周期的席位（legacy ``cycle_id IS NULL`` 行在显式周期下自然不可见）。
    """
    existing = {
        (str(item.get("account_id")), str(item.get("code")))
        for item in positions
        if int(num_fn(item.get("qty"))) >= lot_size
    }
    status_list = list(occupying_statuses)

    excluded_id: int | None = None
    if exclude_order_key is not None:
        excluded_id = int(exclude_order_key)

    if not status_list:
        return set()

    placeholders = ",".join("?" for _ in status_list)
    where = f"origin IN ('manual','strategy') AND side='buy' AND status IN ({placeholders})"
    params: list[Any] = list(status_list)
    if cycle_id is not None:
        where += " AND cycle_id=?"
        params.append(int(cycle_id))
    if excluded_id is not None:
        where += " AND id<>?"
        params.append(excluded_id)

    rows = rows_fn(conn, f"SELECT account_id,code FROM paper_orders WHERE {where}", tuple(params))
    return {
        (str(row.get("account_id")), str(row.get("code")))
        for row in rows
        if (str(row.get("account_id")), str(row.get("code"))) not in existing
    }
