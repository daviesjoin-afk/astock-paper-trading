# -*- coding: utf-8 -*-
"""Cycle/as-of bounded portfolio read model.

Authority and dependency direction::

    verified execution / durable lot facts
            |
            v
    cycle/as-of bounded portfolio facts
            |
            v
    API / dashboard / risk / research consumers

This module is read-only.  It never imports :mod:`paper_trading`, never inserts
or updates execution facts, and never resolves the active cycle or the machine
clock.  The only market-value input is an explicit ``valuations`` mapping; a
missing price stays ``None`` (unknown) instead of falling back to a current
quote.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

try:  # ``backend`` on sys.path (production and direct unittest runs)
    import execution_verification as EV
    import paper_portfolio as PP
except ImportError:  # pragma: no cover - package-style import
    from . import execution_verification as EV
    from . import paper_portfolio as PP

__all__ = [
    "PORTFOLIO_READ_MODEL_VERSION",
    "STATUS_VERIFIED",
    "STATUS_UNKNOWN",
    "PortfolioReadContext",
    "bounded_lots",
    "verified_cash_flows",
    "positions_for_context",
    "realized_pnl",
    "cash",
    "portfolio_for_context",
    "exposure",
    "initial_capital",
    "portfolio_for_cycle",
]

PORTFOLIO_READ_MODEL_VERSION = "portfolio-read-model-v1"
STATUS_VERIFIED = "verified"
STATUS_UNKNOWN = "unknown"

_POSITION_LOT_COLUMNS = {
    "cycle_id", "account_id", "code", "qty", "remaining_qty", "cost",
    "acquired_at", "available_date", "asset_type", "source_order_id",
}
_FILL_COLUMNS = {"id", "order_id", "account_id", "side", "code", "qty", "fill_date"}
_ORDER_COLUMNS = {
    "id", "account_id", "side", "code", "status", "cycle_id",
    "execution_status", "execution_verified", "realized_pnl", "executed_at",
    "amount", "fees",
}


def _num(value: Any, default: float | None = 0.0) -> float | None:
    """Convert a stored numeric value, preserving unknown as ``None``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _asof(value: Any) -> dt.date:
    """Parse an explicit as-of date; never fall back to today."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ValueError(f"PortfolioReadContext asof_day is invalid: {value!r}") from exc
    raise ValueError(f"PortfolioReadContext asof_day is required: {value!r}")


def _day_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else None


def _row_dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


def _columns(conn, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _has_columns(conn, table: str, required: set[str]) -> bool:
    columns = _columns(conn, table)
    return bool(columns) and required.issubset(columns)


@dataclass(frozen=True)
class PortfolioReadContext:
    """Immutable identity for one explicit portfolio read."""

    cycle_id: int
    asof_day: dt.date

    def __post_init__(self):
        if self.cycle_id is None:
            raise ValueError("PortfolioReadContext requires explicit cycle_id")
        if self.asof_day is None:
            raise ValueError("PortfolioReadContext requires explicit asof_day")
        try:
            cycle_id = int(self.cycle_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"PortfolioReadContext cycle_id is invalid: {self.cycle_id!r}"
            ) from exc
        object.__setattr__(self, "cycle_id", cycle_id)
        object.__setattr__(self, "asof_day", _asof(self.asof_day))


def _sell_fills(conn, context: PortfolioReadContext, account_id: str | None = None):
    """Return (verified rows, all rows, proof_available)."""
    if not (
        _has_columns(conn, "paper_fills", _FILL_COLUMNS)
        and _has_columns(conn, "paper_orders", _ORDER_COLUMNS)
    ):
        return [], [], False
    params: list[Any] = [context.cycle_id, context.asof_day.isoformat()]
    account_sql = ""
    if account_id:
        account_sql = " AND o.account_id=?"
        params.append(str(account_id))
    rows = _row_dicts(conn.execute(
        "SELECT f.id AS fill_id, f.order_id, f.account_id AS fill_account_id,"
        "       f.side AS fill_side, f.code AS fill_code, f.qty AS fill_qty,"
        "       f.price AS fill_price, f.amount AS fill_amount, f.fees AS fill_fees,"
        "       f.fill_date,"
        "       o.account_id AS order_account_id, o.side AS order_side,"
        "       o.code AS order_code, o.status AS order_status, o.cycle_id,"
        "       o.execution_status, o.execution_verified, o.realized_pnl,"
        "       o.amount AS order_amount, o.fees AS order_fees, o.executed_at"
        "  FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id"
        " WHERE o.cycle_id=? AND o.side='sell' AND o.status='filled'"
        "   AND f.side='sell' AND f.fill_date IS NOT NULL"
        "   AND length(f.fill_date)>=10 AND substr(f.fill_date,1,10)<=?"
        + account_sql +
        " ORDER BY f.fill_date,f.id",
        tuple(params),
    ))
    verified = []
    for row in rows:
        identity_ok = (
            str(row.get("fill_account_id") or "") == str(row.get("order_account_id") or "")
            and str(row.get("fill_code") or "") == str(row.get("order_code") or "")
            and str(row.get("fill_side") or "") == str(row.get("order_side") or "")
        )
        if identity_ok and EV.is_verified_row(row):
            verified.append(row)
    return verified, rows, True


def _all_filled_sell_orders(conn, context: PortfolioReadContext, account_id: str | None = None):
    if not _has_columns(conn, "paper_orders", _ORDER_COLUMNS):
        return [], False
    params: list[Any] = [context.cycle_id, context.asof_day.isoformat()]
    account_sql = ""
    if account_id:
        account_sql = " AND account_id=?"
        params.append(str(account_id))
    rows = _row_dicts(conn.execute(
        "SELECT id,account_id,code,status,cycle_id,execution_status,"
        "       execution_verified,realized_pnl,executed_at"
        "  FROM paper_orders"
        " WHERE cycle_id=? AND side='sell' AND status='filled'"
        "   AND executed_at IS NOT NULL AND length(executed_at)>=10"
        "   AND substr(executed_at,1,10)<=?"
        + account_sql +
        " ORDER BY id",
        tuple(params),
    ))
    return rows, True


def _buy_fills(conn, context: PortfolioReadContext, account_id: str | None = None):
    if not (
        _has_columns(conn, "paper_fills", _FILL_COLUMNS)
        and _has_columns(conn, "paper_orders", _ORDER_COLUMNS)
    ):
        return [], [], False
    params: list[Any] = [context.cycle_id, context.asof_day.isoformat()]
    account_sql = ""
    if account_id:
        account_sql = " AND o.account_id=?"
        params.append(str(account_id))
    rows = _row_dicts(conn.execute(
        "SELECT f.id AS fill_id, f.order_id, f.account_id AS fill_account_id,"
        "       f.side AS fill_side, f.code AS fill_code, f.qty AS fill_qty,"
        "       f.price AS fill_price, f.amount AS fill_amount, f.fees AS fill_fees,"
        "       f.fill_date,"
        "       o.account_id AS order_account_id, o.side AS order_side,"
        "       o.code AS order_code, o.status AS order_status, o.cycle_id,"
        "       o.execution_status, o.execution_verified, o.realized_pnl,"
        "       o.amount AS order_amount, o.fees AS order_fees, o.executed_at"
        "  FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id"
        " WHERE o.cycle_id=? AND o.side='buy' AND o.status='filled'"
        "   AND f.side='buy' AND f.fill_date IS NOT NULL"
        "   AND length(f.fill_date)>=10 AND substr(f.fill_date,1,10)<=?"
        + account_sql +
        " ORDER BY f.fill_date,f.id",
        tuple(params),
    ))
    verified = []
    for row in rows:
        identity_ok = (
            str(row.get("fill_account_id") or "") == str(row.get("order_account_id") or "")
            and str(row.get("fill_code") or "") == str(row.get("order_code") or "")
            and str(row.get("fill_side") or "") == str(row.get("order_side") or "")
        )
        if identity_ok and EV.is_verified_row(row):
            verified.append(row)
    return verified, rows, True


def _has_any_fill_rows(conn, context: PortfolioReadContext,
                       account_id: str | None = None) -> bool | None:
    """Return whether any fill exists up to as-of; ``None`` means unreadable."""
    if not (
        _has_columns(conn, "paper_fills", {"order_id", "fill_date"})
        and _has_columns(conn, "paper_orders", {"id", "cycle_id"})
    ):
        return None
    params: list[Any] = [context.cycle_id, context.asof_day.isoformat()]
    account_sql = ""
    if account_id and "account_id" in _columns(conn, "paper_orders"):
        account_sql = " AND o.account_id=?"
        params.append(str(account_id))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id"
            " WHERE o.cycle_id=? AND f.fill_date IS NOT NULL"
            "   AND length(f.fill_date)>=10 AND substr(f.fill_date,1,10)<=?"
            + account_sql,
            tuple(params),
        ).fetchone()
    except sqlite3.Error:
        return None
    return bool(row and row[0])


def _unproven_sell_exists(conn, context: PortfolioReadContext,
                          account_id: str | None = None) -> bool:
    _verified, rows, proof_available = _sell_fills(conn, context, account_id)
    if not proof_available:
        # No verification columns at all: any historical sell row is unproven.
        if _has_columns(conn, "paper_orders", {"cycle_id", "side", "status", "executed_at"}):
            params = [context.cycle_id, context.asof_day.isoformat()]
            sql = ("SELECT COUNT(*) FROM paper_orders WHERE cycle_id=? AND side='sell'"
                   " AND status='filled' AND executed_at IS NOT NULL"
                   " AND length(executed_at)>=10 AND substr(executed_at,1,10)<=?")
            if account_id:
                sql += " AND account_id=?"
                params.append(str(account_id))
            return bool(conn.execute(sql, tuple(params)).fetchone()[0])
        return False
    unproven_fill = any(
        str(row.get("fill_account_id") or "") != str(row.get("order_account_id") or "")
        or str(row.get("fill_code") or "") != str(row.get("order_code") or "")
        or str(row.get("fill_side") or "") != str(row.get("order_side") or "")
        or not EV.is_verified_row(row)
        for row in rows
    )
    if unproven_fill:
        return True
    verified_fill_order_ids = {
        int(row["order_id"]) for row in _verified
        if row.get("order_id") is not None
    }
    orders, _proof = _all_filled_sell_orders(conn, context, account_id)
    for order in orders:
        if not EV.is_verified_row(order):
            return True
        if int(order["id"]) not in verified_fill_order_ids:
            return True
    return False


def _consume_fifo(lots: list[dict], sells: list[dict]) -> tuple[list[dict], bool]:
    """Apply verified sell quantities to durable lots without touching storage."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for lot in lots:
        item = dict(lot)
        original_qty = int(_num(item.get("qty"), 0) or 0)
        item["remaining_qty"] = original_qty
        key = (str(item.get("account_id") or ""), str(item.get("code") or ""))
        grouped.setdefault(key, []).append(item)
    for key in grouped:
        grouped[key].sort(key=lambda row: (str(row.get("acquired_at") or ""), int(row.get("id") or 0)))
    for sell in sells:
        key = (str(sell.get("fill_account_id") or ""), str(sell.get("fill_code") or ""))
        remaining = int(_num(sell.get("fill_qty"), 0) or 0)
        if remaining <= 0:
            continue
        for lot in grouped.get(key, []):
            take = min(remaining, max(0, int(lot.get("remaining_qty") or 0)))
            if take:
                lot["remaining_qty"] = int(lot["remaining_qty"]) - take
                remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            return [row for bucket in grouped.values() for row in bucket], False
    return [row for bucket in grouped.values() for row in bucket], True


def bounded_lots(conn, context: PortfolioReadContext, *, account_id: str | None = None) -> list[dict]:
    """Reconstruct open lots at ``context.asof_day`` from durable facts.

    ``remaining_qty`` is deliberately ignored as historical authority.  It is
    only replaced on local copies after verified sell fills up to the requested
    day have been consumed in FIFO order.
    """
    lots, _status = bounded_lots_with_status(conn, context, account_id=account_id)
    return [dict(row) for row in lots if int(row.get("remaining_qty") or 0) > 0]


def bounded_lots_with_status(conn, context: PortfolioReadContext, *,
                             account_id: str | None = None) -> tuple[list[dict], str]:
    """Return ``(lots, quantity_status)`` for an explicit context."""
    if not _has_columns(conn, "paper_position_lots", _POSITION_LOT_COLUMNS):
        return [], STATUS_VERIFIED
    params: list[Any] = [context.cycle_id, context.asof_day.isoformat()]
    account_sql = ""
    if account_id:
        account_sql = " AND account_id=?"
        params.append(str(account_id))
    rows = _row_dicts(conn.execute(
        "SELECT * FROM paper_position_lots"
        " WHERE cycle_id=? AND qty>0 AND acquired_at IS NOT NULL"
        "   AND length(acquired_at)>=10 AND substr(acquired_at,1,10)<=?"
        + account_sql +
        " ORDER BY account_id,code,acquired_at,id",
        tuple(params),
    ))
    sells, _rows, proof_available = _sell_fills(conn, context, account_id)
    unproven = _unproven_sell_exists(conn, context, account_id)
    if not proof_available and unproven:
        status = STATUS_UNKNOWN
    else:
        status = STATUS_UNKNOWN if unproven else STATUS_VERIFIED
    rebuilt, fully_consumed = _consume_fifo(rows, sells)
    if not fully_consumed:
        status = STATUS_UNKNOWN
    return rebuilt, status


def verified_cash_flows(conn, context: PortfolioReadContext, *,
                        account_id: str | None = None) -> dict:
    """Bounded cash-flow projection for display cost.

    A key is present only when every relevant fill up to ``asof_day`` is fully
    proven.  Otherwise the caller gets no flow for that key and the display
    layer falls back to durable lot settlement cost.
    """
    buys, _buy_rows, proof = _buy_fills(conn, context, account_id)
    sells, _sell_rows, sell_proof = _sell_fills(conn, context, account_id)
    if not proof or not sell_proof:
        return {}
    flows: dict[tuple[str, str], dict] = {}
    incomplete: set[tuple[str, str]] = set()
    for side, rows in (("buy", buys), ("sell", sells)):
        for row in rows:
            key = (str(row.get("fill_account_id") or ""), str(row.get("fill_code") or ""))
            if not EV.is_verified_row(row):
                incomplete.add(key)
                continue
            flow = flows.setdefault(key, {"buy_cash": 0.0, "sell_cash": 0.0})
            amount = _num(row.get("fill_amount"))
            fees = _num(row.get("fill_fees"), 0.0)
            if amount is None or fees is None:
                incomplete.add(key)
                continue
            if side == "buy":
                flow["buy_cash"] += amount + fees
            else:
                flow["sell_cash"] += amount - fees
    # If there is any unproven sell for a key, do not publish a partial flow.
    _verified, all_sells, sell_proof = _sell_fills(conn, context, account_id)
    if sell_proof:
        for row in all_sells:
            key = (str(row.get("fill_account_id") or ""), str(row.get("fill_code") or ""))
            if not EV.is_verified_row(row):
                incomplete.add(key)
    return {key: value for key, value in flows.items() if key not in incomplete}


def positions_for_context(conn, context: PortfolioReadContext, *,
                           account_id: str | None = None) -> list[dict]:
    """Aggregate bounded durable lots into the compatibility position shape."""
    lots, _quantity_status = bounded_lots_with_status(
        conn, context, account_id=account_id
    )
    open_lots = [row for row in lots if int(row.get("remaining_qty") or 0) > 0]
    flows = verified_cash_flows(conn, context, account_id=account_id)
    return PP.aggregate_positions(
        open_lots, (), flows, context.asof_day.isoformat(), num=_num
    )


def realized_pnl(conn, context: PortfolioReadContext, *,
                 account_id: str | None = None) -> tuple[float | None, str]:
    """Sum verified committed SELL realized PnL up to ``context.asof_day``."""
    if _unproven_sell_exists(conn, context, account_id):
        return None, STATUS_UNKNOWN
    verified, _rows, proof_available = _sell_fills(conn, context, account_id)
    if not proof_available:
        return 0.0, STATUS_VERIFIED
    total = 0.0
    order_ids: set[int] = set()
    for row in verified:
        order_ids.add(int(row["order_id"]))
    if not order_ids:
        return 0.0, STATUS_VERIFIED
    placeholders = ",".join("?" for _ in order_ids)
    orders = _row_dicts(conn.execute(
        f"SELECT id,realized_pnl FROM paper_orders WHERE id IN ({placeholders})",
        tuple(sorted(order_ids)),
    ))
    for order in orders:
        value = _num(order.get("realized_pnl"), None)
        if value is None:
            return None, STATUS_UNKNOWN
        total += value
    return total, STATUS_VERIFIED


def _cycle_initial(conn, context: PortfolioReadContext, account_id: str | None = None):
    """Resolve cycle/account initial capital without reading current cash."""
    if account_id:
        if not _has_columns(conn, "paper_accounts", {"id", "initial_cash"}):
            return None
        row = conn.execute(
            "SELECT initial_cash FROM paper_accounts WHERE id=?", (str(account_id),)
        ).fetchone()
        return _num(row[0], None) if row is not None else None
    if _has_columns(conn, "paper_cycles", {"id", "capital"}):
        cycle = conn.execute(
            "SELECT capital FROM paper_cycles WHERE id=?", (context.cycle_id,)
        ).fetchone()
        if cycle is not None:
            declared = _num(cycle[0], 0.0)
            if declared is not None and declared > 0:
                return declared
    if not _has_columns(conn, "paper_accounts", {"cycle_id", "initial_cash"}):
        return None
    row = conn.execute(
        "SELECT COALESCE(SUM(initial_cash),0) FROM paper_accounts WHERE cycle_id=?",
        (context.cycle_id,),
    ).fetchone()
    return _num(row[0], None) if row is not None else None


def _cash_flow_total(conn, context: PortfolioReadContext, account_id: str | None = None):
    buys, buy_rows, buy_proof = _buy_fills(conn, context, account_id)
    sells, _sell_rows, sell_proof = _sell_fills(conn, context, account_id)
    if not buy_proof or not sell_proof:
        any_fill = _has_any_fill_rows(conn, context, account_id)
        if any_fill is True:
            return None, STATUS_UNKNOWN
        if any_fill is None:
            return None, STATUS_UNKNOWN
        return 0.0, STATUS_VERIFIED
    if _unproven_sell_exists(conn, context, account_id):
        return None, STATUS_UNKNOWN
    if any(not EV.is_verified_row(row) for row in buy_rows):
        return None, STATUS_UNKNOWN
    total = 0.0
    for row in buys:
        amount = _num(row.get("fill_amount"))
        fees = _num(row.get("fill_fees"), 0.0)
        if amount is None or fees is None:
            return None, STATUS_UNKNOWN
        total -= amount + fees
    for row in sells:
        amount = _num(row.get("fill_amount"))
        fees = _num(row.get("fill_fees"), 0.0)
        if amount is None or fees is None:
            return None, STATUS_UNKNOWN
        total += amount - fees
    return total, STATUS_VERIFIED


def cash(conn, context: PortfolioReadContext, *,
         account_id: str | None = None) -> tuple[float | None, str]:
    """Reconstruct bounded cash from the cycle declaration and verified fills."""
    initial = _cycle_initial(conn, context, account_id)
    if initial is None:
        return None, STATUS_UNKNOWN
    net, status = _cash_flow_total(conn, context, account_id)
    if net is None or status != STATUS_VERIFIED:
        return None, STATUS_UNKNOWN
    return initial + net, STATUS_VERIFIED



def initial_capital(conn, context: PortfolioReadContext):
    """Return the durable cycle/account initial capital without current cash."""
    return _cycle_initial(conn, context)


def exposure(positions, quotes, *, num):
    """Pure market-value aggregation for an already-bounded position list."""
    value = 0.0
    industries = {}
    codes = {}
    for pos in positions:
        price = num((quotes.get(pos["code"]) or {}).get("price"), num(pos["cost"]))
        item_value = num(pos["qty"]) * price
        value += item_value
        codes[pos["code"]] = codes.get(pos["code"], 0.0) + item_value
        industry = pos.get("industry") or "未知"
        industries[industry] = industries.get(industry, 0.0) + item_value
    return value, industries, codes

def _valuation_price(valuations: Mapping | None, code: str) -> float | None:
    if not valuations:
        return None
    value = valuations.get(code)
    if isinstance(value, Mapping):
        value = value.get("price")
    return _num(value, None)


def portfolio_for_context(
    conn,
    context: PortfolioReadContext,
    *,
    account_id: str | None = None,
    valuations: Mapping | None = None,
) -> dict:
    """Return one cycle/as-of bounded portfolio read.

    ``market_value`` / ``unrealized_pnl`` / ``nav`` are ``None`` when explicit
    valuation evidence is missing.  They are never filled from a current quote.
    """
    positions = positions_for_context(conn, context, account_id=account_id)
    realized, realized_status = realized_pnl(conn, context, account_id=account_id)
    cash_value, cash_status = cash(conn, context, account_id=account_id)
    missing_codes = []
    market_value = 0.0
    unrealized = 0.0
    for position in positions:
        code = str(position.get("code") or "")
        price = _valuation_price(valuations, code)
        if price is None:
            missing_codes.append(code)
            continue
        qty = _num(position.get("qty"), 0.0) or 0.0
        cost = _num(position.get("cost"), 0.0) or 0.0
        market_value += qty * price
        unrealized += qty * (price - cost)
    if missing_codes:
        market_value = None
        unrealized = None
        market_status = STATUS_UNKNOWN
    else:
        market_status = STATUS_VERIFIED
    nav = (
        cash_value + market_value
        if cash_value is not None and market_value is not None else None
    )
    if nav is None:
        nav_status = STATUS_UNKNOWN
    elif cash_status != STATUS_VERIFIED or market_status != STATUS_VERIFIED:
        nav_status = STATUS_UNKNOWN
    else:
        nav_status = STATUS_VERIFIED
    return {
        "version": PORTFOLIO_READ_MODEL_VERSION,
        "context": {
            "cycle_id": context.cycle_id,
            "asof_day": context.asof_day.isoformat(),
            "account_id": str(account_id) if account_id else None,
        },
        "positions": positions,
        "realized_pnl": realized,
        "realized_pnl_status": realized_status,
        "cash": cash_value,
        "cash_status": cash_status,
        "market_value": market_value,
        "market_value_status": market_status,
        "unrealized_pnl": unrealized,
        "unrealized_pnl_status": market_status,
        "nav": nav,
        "nav_status": nav_status,
        "valuation_missing_codes": sorted(set(missing_codes)),
        "authority": {
            "quantity": "paper_position_lots.qty minus verified SELL fills",
            "acquisition_cost": "paper_position_lots.cost",
            "realized_pnl": "verified committed SELL execution facts",
            "cash": "paper_cycles.capital + verified fill cashflows",
            "market_value": "explicit bounded valuation evidence",
            "projection": "paper_positions is compatibility-only",
        },
    }


def portfolio_for_cycle(
    conn,
    cycle_id: int,
    asof_day,
    *,
    account_id: str | None = None,
    valuations: Mapping | None = None,
) -> dict:
    """Convenience wrapper around :class:`PortfolioReadContext`."""
    return portfolio_for_context(
        conn,
        PortfolioReadContext(cycle_id=cycle_id, asof_day=asof_day),
        account_id=account_id,
        valuations=valuations,
    )