# -*- coding: utf-8 -*-
"""Cycle/as-of bounded portfolio read model —— portfolio/accounting **factual owner**.

Authority and dependency direction::

    verified execution / durable lot facts
            |
            v
    cycle/as-of bounded portfolio facts          （本模块：唯一 owner）
            |
            +--> API / dashboard / risk consumers
            |
            +--> PortfolioFactProjection   （owner fact contract，R27-B2C-4B）
                        |
                        v
                 ai_research_portfolio_adapter （只翻译，不判定）
                        |
                        v
                 ai_research_contract.ResearchEvidenceRef

This module is read-only.  It never imports :mod:`paper_trading`, never inserts
or updates execution facts, and never resolves the active cycle or the machine
clock.  The only market-value input is an explicit ``valuations`` mapping; a
missing price stays ``None`` (unknown) instead of falling back to a current
quote.

──────────────── authority 边界：本模块**不**拥有 NAV ────────────────

``portfolio_for_context(...)`` 可以接收调用方显式传入的价格映射并据此算出
``market_value`` / ``unrealized_pnl`` / ``nav``。那只是"调用方给了一个合法数字"，
**不能**证明这些价格来自 R24 market owner、通过了哪套核验、对应哪个真实 market
snapshot。因此 typed fact contract（:func:`accounting_fact_projections`）**刻意不发布**
``nav`` / ``latest_nav`` / ``prior_nav`` / ``daily_pnl`` / ``daily_return`` /
``market_value`` / ``unrealized_pnl`` / ``benchmark`` / 估值价格 / ``quote_status``，
其签名也不接受 ``valuations`` / ``MarketDataReading`` / current quote。

```text
portfolio ledger authority   现金 / 已实现盈亏 / 持仓成本 / cycle·account·asof 身份
market valuation authority   估值价格 / market 核验 / market observation 的 as-of
cross-owner composition      NAV / 市值 / 未实现盈亏 / 日 PnL / 日收益   （B2C-4C）
```

把 caller 提供的裸价格升级成"owner 已证明的估值"，就是伪造 provenance —— 这正是
R27 要根除的东西。``paper_nav`` 本轮仍是 legacy/compatibility（它的
``quote_status='verified'`` 不是 R24 的 OwnerVerification，也没有 typed market evidence
identity），因此**不**被读来签发 typed fact；``paper_positions`` 同理仍是
compatibility-only 投影，**不**作为 typed fact 的事实来源。
"""
from __future__ import annotations

import datetime as dt
import math
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
    "PortfolioReadUnavailable",
    "PortfolioReadContext",
    "bounded_lots",
    "verified_cash_flows",
    "positions_for_context",
    "positions_for_context_with_status",
    "risk_positions_for_context",
    "realized_pnl",
    "cash",
    "portfolio_for_context",
    "exposure",
    "initial_capital",
    "compatibility_cash",
    "portfolio_for_cycle",
    # owner fact contract（R27-B2C-4B）
    "PORTFOLIO_FACT_CONTRACT_VERSION",
    "PORTFOLIO_FACT_VERIFICATION_SCOPE",
    "PORTFOLIO_FACT_CASH",
    "PORTFOLIO_FACT_REALIZED_PNL",
    "PORTFOLIO_FACT_POSITION_COST_SUMMARY",
    "PORTFOLIO_FACT_KINDS",
    "PORTFOLIO_FACT_STATUSES",
    "PortfolioFactContractError",
    "PositionCostSummary",
    "PortfolioFactProjection",
    "accounting_fact_projections",
]

PORTFOLIO_READ_MODEL_VERSION = "portfolio-read-model-v1"
STATUS_VERIFIED = "verified"
STATUS_UNKNOWN = "unknown"

_POSITION_LOT_COLUMNS = {
    "id", "cycle_id", "account_id", "code", "qty", "remaining_qty", "cost",
    "acquired_at", "available_date", "asset_type", "source_order_id",
}
_FILL_COLUMNS = {"id", "order_id", "account_id", "side", "code", "qty", "fill_date"}
#: Columns the fill readers actually ``SELECT``.  A partially migrated
#: ``paper_fills`` table missing one of these must fail closed as unknown
#: instead of raising ``sqlite3.OperationalError`` out of a portfolio read.
_FILL_SELECT_COLUMNS = _FILL_COLUMNS | {"price", "amount", "fees"}
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


def _known_int(value: Any) -> int | None:
    """按 SQLite INTEGER 语义把存储值转成 ``int``；不可解析 → ``None``。

    刻意不做 ``int(float(value))`` 之类的宽容转换：``source_fill_id`` 是血缘指针，
    一旦把非整数"解释"成某个 id，就会指向一笔不存在的成交。无法证明即 ``None``
    （调用方据此走 legacy 路径并 fail closed）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    return None


def _ledger_num(value: Any, default: float | None = None) -> float | None:
    """Numeric ledger evidence where non-finite values are unknown, not data.

    SQLite REAL columns can hold ``inf`` / ``-inf`` / ``nan``.  Publishing such
    a value would make cash / realized PnL / NAV / exposure "verified infinite",
    so it is treated exactly like an unreadable value — the same policy
    :func:`_valuation_price` already applies to prices.

    A **missing** value may take ``default`` (an absent fee really is zero), but
    a value that is present and non-finite stays ``None`` so callers fail closed
    instead of silently turning corrupt evidence into a number.
    """
    if value is None:
        return default
    number = _num(value, None)
    if number is None or not math.isfinite(number):
        return None
    return number


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


def _identity_ok(row: Mapping) -> bool:
    """A fill row must match its source order identity before it counts."""
    return (
        str(row.get("fill_account_id") or "") == str(row.get("order_account_id") or "")
        and str(row.get("fill_code") or "") == str(row.get("order_code") or "")
        and str(row.get("fill_side") or "") == str(row.get("order_side") or "")
    )


def _columns(conn, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _has_columns(conn, table: str, required: set[str]) -> bool:
    columns = _columns(conn, table)
    return bool(columns) and required.issubset(columns)


class PortfolioReadUnavailable(RuntimeError):
    """Raised when a bounded portfolio read cannot prove a required fact."""


def _cycle_is_archived(conn, context: PortfolioReadContext) -> bool:
    """Return whether the requested cycle's live ledger has been archived."""
    if _has_columns(conn, "paper_archives", {"cycle_id"}):
        try:
            row = conn.execute(
                "SELECT 1 FROM paper_archives WHERE cycle_id=? LIMIT 1",
                (context.cycle_id,),
            ).fetchone()
            if row is not None:
                return True
        except sqlite3.Error:
            pass
    if _has_columns(conn, "paper_cycles", {"id", "status"}):
        row = conn.execute(
            "SELECT status FROM paper_cycles WHERE id=?", (context.cycle_id,)
        ).fetchone()
        return bool(row is not None and str(row[0] or "") == "archived")
    return False


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
    """Return (proven rows, all rows, proof_available).

    Every fill attached to a bounded SELL order is selected, **not** just the
    rows whose declared side agrees with the order: a contradictory fill is
    execution evidence that must fail closed, so it has to reach the
    completeness checks instead of being filtered out by ``f.side``.

    R26：选择条件从"订单 ``status='filled'``"改为"**该委托有正成交**"
    （完整或部分成交）。部分成交是已经发生的事实，它同样消耗了底仓；只认整单
    全成交会让重建出来的持仓偏多，与 ``paper_position_lots.remaining_qty``
    以及风险读路径给出**不同的持仓结论** —— 那正是不可接受的"同一张单在不同
    路径得到不同结果"。数量口径由 :data:`EV.POSITIVE_EXECUTION_PREDICATE`
    唯一决定，本模块不另写一份。
    """
    if not (
        _has_columns(conn, "paper_fills", _FILL_SELECT_COLUMNS)
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
        " WHERE o.cycle_id=? AND o.side='sell' AND " + EV.FILL_CARRYING_PREDICATE +
        "   AND f.fill_date IS NOT NULL"
        "   AND length(f.fill_date)>=10 AND substr(f.fill_date,1,10)<=?"
        + account_sql +
        " ORDER BY f.fill_date,f.id",
        tuple(params),
    ))
    proven = []
    for row in rows:
        if _identity_ok(row) and EV.is_positive_execution_row(row):
            proven.append(row)
    return proven, rows, True


def _all_filled_sell_orders(conn, context: PortfolioReadContext, account_id: str | None = None):
    if not _has_columns(conn, "paper_orders", _ORDER_COLUMNS):
        return [], False
    params: list[Any] = [context.cycle_id]
    account_sql = ""
    if account_id:
        account_sql = " AND account_id=?"
        params.append(str(account_id))
    rows = _row_dicts(conn.execute(
        "SELECT id,account_id,code,status,cycle_id,execution_status,"
        "       execution_verified,realized_pnl,executed_at"
        "  FROM paper_orders"
        " WHERE cycle_id=? AND side='sell' AND " + EV.FILL_CARRYING_PREDICATE +
        account_sql +
        " ORDER BY id",
        tuple(params),
    ))
    economic_dates, proof = _lot_economic_dates(conn, (row.get("id") for row in rows))
    bounded = []
    for row in rows:
        economic = economic_dates.get(int(row["id"]))
        if economic is None or economic <= context.asof_day.isoformat():
            bounded.append(row)
    return bounded, proof


def _all_filled_buy_orders(conn, context: PortfolioReadContext,
                           account_id: str | None = None):
    if not _has_columns(conn, "paper_orders", _ORDER_COLUMNS):
        return [], False
    params: list[Any] = [context.cycle_id]
    account_sql = ""
    if account_id:
        account_sql = " AND account_id=?"
        params.append(str(account_id))
    rows = _row_dicts(conn.execute(
        "SELECT id,account_id,code,status,cycle_id,execution_status,"
        "       execution_verified,realized_pnl,executed_at"
        "  FROM paper_orders"
        " WHERE cycle_id=? AND side='buy' AND " + EV.FILL_CARRYING_PREDICATE +
        account_sql +
        " ORDER BY id",
        tuple(params),
    ))
    economic_dates, proof = _lot_economic_dates(conn, (row.get("id") for row in rows))
    bounded = []
    for row in rows:
        economic = economic_dates.get(int(row["id"]))
        if economic is None or economic <= context.asof_day.isoformat():
            bounded.append(row)
    return bounded, proof


def _buy_fills(conn, context: PortfolioReadContext, account_id: str | None = None):
    """Return (verified rows, all rows, proof_available).

    As with :func:`_sell_fills`, every fill attached to a bounded BUY order is
    selected so a side-contradicting fill reaches the completeness checks
    instead of being filtered out by ``f.side``.
    """
    if not (
        _has_columns(conn, "paper_fills", _FILL_SELECT_COLUMNS)
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
        " WHERE o.cycle_id=? AND o.side='buy' AND " + EV.FILL_CARRYING_PREDICATE +
        "   AND f.fill_date IS NOT NULL"
        "   AND length(f.fill_date)>=10 AND substr(f.fill_date,1,10)<=?"
        + account_sql +
        " ORDER BY f.fill_date,f.id",
        tuple(params),
    ))
    proven = []
    for row in rows:
        if _identity_ok(row) and EV.is_positive_execution_row(row):
            proven.append(row)
    return proven, rows, True


def _has_any_fill_rows(conn, context: PortfolioReadContext,
                       account_id: str | None = None) -> bool | None:
    """Return whether any fill exists up to as-of; ``None`` means unreadable."""
    if not (
        _has_columns(conn, "paper_fills", {"order_id", "fill_date"})
        and _has_columns(conn, "paper_orders", {"id", "cycle_id"})
    ):
        return None
    order_columns = _columns(conn, "paper_orders")
    if account_id and "account_id" not in order_columns:
        # An account-scoped read cannot prove "this account had no fills"
        # without the order identity column; failing open to zero net flow
        # would publish the account's initial balance as verified.
        return None
    params: list[Any] = [context.cycle_id, context.asof_day.isoformat()]
    account_sql = ""
    if account_id:
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
    _proven, rows, proof_available = _sell_fills(conn, context, account_id)
    if not proof_available:
        # No verification columns at all: any historical sell row is unproven.
        required_columns = {"cycle_id", "side", "status", "executed_at"}
        order_columns = _columns(conn, "paper_orders")
        if account_id and "account_id" not in order_columns:
            return True
        if required_columns.issubset(order_columns):
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
        not _identity_ok(row) or not EV.is_positive_execution_row(row)
        for row in rows
    )
    if unproven_fill:
        return True
    proven_fill_order_ids = {
        int(row["order_id"]) for row in _proven
        if row.get("order_id") is not None
    }
    orders, _proof = _all_filled_sell_orders(conn, context, account_id)
    for order in orders:
        # 订单自身的判定同样用"正成交"口径：部分成交订单的已成交部分有流水为证，
        # 已在上面的 ``proven_fill_order_ids`` 里；这里只在订单没有任何正成交证据时
        # 才当成"未经证明"。
        if not EV.is_positive_execution_row(order):
            return True
        if int(order["id"]) not in proven_fill_order_ids:
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
        # A non-finite fill quantity is not evidence; `int(inf)` would raise
        # ``OverflowError`` out of the whole portfolio / risk read.
        if _ledger_num(sell.get("fill_qty")) is None:
            return [row for bucket in grouped.values() for row in bucket], False
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


def _lot_economic_dates(conn, order_ids) -> tuple[dict[int, str], bool]:
    """Return source-order acquisition dates, preferring the actual fill date.

    Only an identity-consistent fill may date its order.  An unrelated fill
    (another account / code / side) must not push a filled order's economic
    date past the requested as-of: that would hide both the fill and the order
    from the bounded sell checks and publish a pre-sale portfolio as verified.
    """
    ids = sorted({int(value) for value in order_ids if value is not None})
    if not ids:
        return {}, True
    fills_available = _has_columns(conn, "paper_fills", {"order_id", "fill_date"})
    orders_available = _has_columns(conn, "paper_orders", {"id", "executed_at", "status"})
    if not fills_available and not orders_available:
        return {}, False
    identity_available = (
        _has_columns(conn, "paper_fills",
                     {"order_id", "fill_date", "account_id", "side", "code"})
        and _has_columns(conn, "paper_orders", {"id", "account_id", "side", "code"})
    )
    dates: dict[int, str] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        placeholders = ",".join("?" for _ in chunk)
        if fills_available:
            if identity_available:
                rows = _row_dicts(conn.execute(
                    f"SELECT f.order_id, f.fill_date AS fill_date,"
                    f"       f.account_id AS fill_account_id, f.side AS fill_side,"
                    f"       f.code AS fill_code, o.account_id AS order_account_id,"
                    f"       o.side AS order_side, o.code AS order_code"
                    f"  FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id"
                    f" WHERE f.order_id IN ({placeholders})"
                    f"   AND f.fill_date IS NOT NULL AND length(f.fill_date)>=10",
                    tuple(chunk),
                ))
                for row in rows:
                    if not _identity_ok(row):
                        continue
                    order_id = int(row["order_id"])
                    day = str(row["fill_date"])[:10]
                    if order_id not in dates or day < dates[order_id]:
                        dates[order_id] = day
            else:
                rows = conn.execute(
                    f"SELECT order_id,MIN(fill_date) FROM paper_fills"
                    f" WHERE order_id IN ({placeholders}) AND fill_date IS NOT NULL"
                    f"   AND length(fill_date)>=10 GROUP BY order_id",
                    tuple(chunk),
                ).fetchall()
                for order_id, fill_date in rows:
                    dates[int(order_id)] = str(fill_date)[:10]
        missing = [value for value in chunk if value not in dates]
        if missing and orders_available:
            placeholders = ",".join("?" for _ in missing)
            rows = conn.execute(
                f"SELECT id,executed_at FROM paper_orders WHERE id IN ({placeholders})"
                f"   AND status='filled' AND executed_at IS NOT NULL"
                f"   AND length(executed_at)>=10",
                tuple(missing),
            ).fetchall()
            for order_id, executed_at in rows:
                dates[int(order_id)] = str(executed_at)[:10]
    return dates, True


def _reused_source_orders(lots) -> set[int]:
    """Return source order ids claimed by more than one durable lot.

    ``paper_position_lots.source_order_id`` carries no uniqueness constraint, so
    two lot rows can point at the same verified single-fill BUY order.  Each
    per-lot check would then pass independently while cash only ever subtracts
    that one fill: a duplicated 100-share lot becomes 200 verified shares and
    halves the display cost.  Reused sources are therefore not evidence.
    """
    counts: dict[int, int] = {}
    for lot in lots:
        try:
            order_id = int(lot.get("source_order_id"))
        except (TypeError, ValueError):
            continue
        counts[order_id] = counts.get(order_id, 0) + 1
    return {order_id for order_id, count in counts.items() if count > 1}


def _verified_source_buy_fill(conn, lot: Mapping, *,
                              reused_sources: set[int] | None = None) -> dict | None:
    """Return a fully matching verified BUY fill for one durable lot.

    精确血缘（R26）：lot 的 ``source_fill_id`` 直接指向**那一笔** FillEvent，
    因此"这个 lot 由哪笔成交出资"是可证明的。没有该列（历史旧 lot，值为 NULL）
    时退回订单级匹配，并要求该订单恰好只有一笔流水 —— 一笔委托多次部分成交时，
    订单级匹配无法证明某个 lot 对应哪一笔，必须 fail closed 而不是随便挑一笔。
    """
    source_order_id = lot.get("source_order_id")
    if source_order_id is None:
        return None
    if not (
        _has_columns(conn, "paper_fills", _FILL_SELECT_COLUMNS)
        and _has_columns(conn, "paper_orders", _ORDER_COLUMNS)
    ):
        return None
    try:
        order_id = int(source_order_id)
    except (TypeError, ValueError):
        return None
    if reused_sources and order_id in reused_sources:
        return None
    source_fill_id = _known_int(lot.get("source_fill_id"))
    if source_fill_id is not None:
        # 有逐笔血缘：直接定位那一笔流水，不再要求"订单只有一笔"。
        row = conn.execute(
            "SELECT f.id AS fill_id, f.order_id, f.qty AS fill_qty,"
            "       f.account_id AS fill_account_id, f.side AS fill_side,"
            "       f.code AS fill_code, f.fill_date,"
            "       o.account_id AS order_account_id, o.side AS order_side,"
            "       o.code AS order_code, o.status AS order_status, o.cycle_id,"
            "       o.execution_status, o.execution_verified, o.executed_at"
            "  FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id"
            " WHERE f.order_id=? AND f.id=?",
            (order_id, source_fill_id),
        ).fetchall()
    else:
        row = conn.execute(
            "SELECT f.id AS fill_id, f.order_id, f.qty AS fill_qty,"
            "       f.account_id AS fill_account_id, f.side AS fill_side,"
            "       f.code AS fill_code, f.fill_date,"
            "       o.account_id AS order_account_id, o.side AS order_side,"
            "       o.code AS order_code, o.status AS order_status, o.cycle_id,"
            "       o.execution_status, o.execution_verified, o.executed_at"
            "  FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id"
            " WHERE f.order_id=? ORDER BY f.id",
            (order_id,),
        ).fetchall()
        # 没有逐笔血缘的 legacy lot：多笔成交的订单无法证明出资关系。
        if len(row) != 1:
            return None
    if len(row) != 1:
        return None
    row = dict(row[0])
    if int(row.get("cycle_id") or -1) != int(lot.get("cycle_id") or -1):
        return None
    if str(row.get("order_account_id") or "") != str(lot.get("account_id") or ""):
        return None
    if str(row.get("fill_account_id") or "") != str(lot.get("account_id") or ""):
        return None
    if str(row.get("order_code") or "") != str(lot.get("code") or ""):
        return None
    if str(row.get("fill_code") or "") != str(lot.get("code") or ""):
        return None
    if str(row.get("order_side") or "").lower() != "buy":
        return None
    if str(row.get("fill_side") or "").lower() != "buy":
        return None
    # 该 lot 由这一笔流水出资，因此证明的是**这笔 FillEvent** 有效，而不是
    # "整张订单完全成交"。部分成交委托的已成交部分同样是可信事实。
    if not EV.is_positive_execution_row(row):
        return None
    if _num(row.get("fill_qty"), None) != _num(lot.get("qty"), None):
        return None
    fill_day = _day_text(row.get("fill_date"))
    if not fill_day:
        return None
    row["economic_date"] = fill_day
    return row

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
    if _cycle_is_archived(conn, context):
        return [], STATUS_UNKNOWN
    if not _has_columns(conn, "paper_position_lots", _POSITION_LOT_COLUMNS):
        return [], STATUS_UNKNOWN
    params: list[Any] = [context.cycle_id]
    account_sql = ""
    if account_id:
        account_sql = " AND account_id=?"
        params.append(str(account_id))
    rows = _row_dicts(conn.execute(
        "SELECT * FROM paper_position_lots"
        " WHERE cycle_id=? AND qty>0"
        + account_sql +
        " ORDER BY acquired_at,id",
        tuple(params),
    ))
    unknown_date = False
    unresolved_uncertain = False
    uncertain_lot_ids: set[int] = set()
    reused_sources = _reused_source_orders(rows)
    bounded = []
    for row in rows:
        lot = dict(row)
        lot_id = int(lot.get("id") or 0)
        original = str(lot.get("acquired_at") or "")
        # Non-finite stored quantities are not ledger evidence.  They would
        # otherwise raise out of the FIFO integer arithmetic below.
        if _ledger_num(lot.get("qty")) is None or _ledger_num(lot.get("cost")) is None:
            unknown_date = True
            uncertain_lot_ids.add(lot_id)
            continue
        evidence = _verified_source_buy_fill(conn, lot, reused_sources=reused_sources)
        if evidence is None:
            uncertain_lot_ids.add(lot_id)
            economic = original[:10] or None
        else:
            economic = evidence.get("economic_date")
        if not economic:
            unknown_date = True
            if lot_id in uncertain_lot_ids:
                unresolved_uncertain = True
            continue
        if economic > context.asof_day.isoformat():
            if lot_id in uncertain_lot_ids:
                unresolved_uncertain = True
            continue
        # Keep the original intraday time for FIFO ordering, but replace the
        # economic date with the source fill's trading date.
        suffix = original[10:] if len(original) >= 10 else " 00:00:00"
        lot["acquired_at"] = economic + (suffix or " 00:00:00")
        bounded.append(lot)
    sells, _rows, proof_available = _sell_fills(conn, context, account_id)
    unproven = _unproven_sell_exists(conn, context, account_id)
    rebuilt, fully_consumed = _consume_fifo(bounded, sells)
    # Uncertainty is only resolved by a **fully closed** account/code position.
    # A source-less lot and a verified lot can share the key; a partial SELL may
    # consume the source-less row first purely because its untrusted
    # ``acquired_at`` sorts earlier, so FIFO cannot prove which lot was actually
    # sold.  As long as anything remains open under that key, the missing
    # acquisition evidence still makes the remainder — and its cost and entry
    # date — unprovable.
    open_by_key: dict[tuple[str, str], int] = {}
    for row in rebuilt:
        key = (str(row.get("account_id") or ""), str(row.get("code") or ""))
        open_by_key[key] = open_by_key.get(key, 0) + int(row.get("remaining_qty") or 0)
    unresolved_uncertain = unresolved_uncertain or any(
        int(row.get("id") or 0) in uncertain_lot_ids
        and open_by_key.get(
            (str(row.get("account_id") or ""), str(row.get("code") or "")), 0
        ) > 0
        for row in rebuilt
    )
    status = STATUS_UNKNOWN if (
        unknown_date or unresolved_uncertain or unproven or not proof_available
    ) else STATUS_VERIFIED
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
    buys, all_buys, proof = _buy_fills(conn, context, account_id)
    sells, all_sells, sell_proof = _sell_fills(conn, context, account_id)
    if not proof or not sell_proof:
        return {}
    flows: dict[tuple[str, str], dict] = {}
    incomplete: set[tuple[str, str]] = set()
    # Every relevant fill must be identity-consistent and verified before a
    # per-symbol display cost may use a partial cash-flow projection.
    for rows in (all_buys, all_sells):
        for row in rows:
            key = (str(row.get("fill_account_id") or ""), str(row.get("fill_code") or ""))
            if not _identity_ok(row) or not EV.is_verified_row(row):
                incomplete.add(key)
    # A filled order with no verified fill row is not evidence of zero cash;
    # it blocks the per-symbol projection for that key.  An order only counts as
    # covered when **every** fill selected for it is identity-consistent and
    # verified: one valid fill alongside a mismatched one would otherwise leave
    # a partial projection on the order's real account/code.
    for orders, all_rows in (
        (_all_filled_buy_orders(conn, context, account_id)[0], all_buys),
        (_all_filled_sell_orders(conn, context, account_id)[0], all_sells),
    ):
        rows_by_order: dict[int, list[dict]] = {}
        for row in all_rows:
            if row.get("order_id") is not None:
                rows_by_order.setdefault(int(row["order_id"]), []).append(row)
        for order in orders:
            key = (str(order.get("account_id") or ""), str(order.get("code") or ""))
            rows = rows_by_order.get(int(order["id"]), [])
            if not EV.is_verified_row(order) or not rows or any(
                not _identity_ok(row) or not EV.is_verified_row(row) for row in rows
            ):
                incomplete.add(key)
    for side, rows in (("buy", buys), ("sell", sells)):
        for row in rows:
            key = (str(row.get("fill_account_id") or ""), str(row.get("fill_code") or ""))
            flow = flows.setdefault(key, {"buy_cash": 0.0, "sell_cash": 0.0})
            amount = _ledger_num(row.get("fill_amount"))
            fees = _ledger_num(row.get("fill_fees"), 0.0)
            if amount is None or fees is None:
                incomplete.add(key)
                continue
            if side == "buy":
                flow["buy_cash"] += amount + fees
            else:
                flow["sell_cash"] += amount - fees
    return {key: value for key, value in flows.items() if key not in incomplete}


def positions_for_context_with_status(
    conn, context: PortfolioReadContext, *, account_id: str | None = None,
    risk_state_rows: list[dict] | None = None,
) -> tuple[list[dict], str]:
    """Aggregate bounded lots and retain the quantity-proof status."""
    lots, quantity_status = bounded_lots_with_status(
        conn, context, account_id=account_id
    )
    open_lots = [row for row in lots if int(row.get("remaining_qty") or 0) > 0]
    flows = verified_cash_flows(conn, context, account_id=account_id)
    positions = PP.aggregate_positions(
        open_lots, risk_state_rows or (), flows, context.asof_day.isoformat(), num=_num
    )
    return positions, quantity_status


def positions_for_context(conn, context: PortfolioReadContext, *,
                           account_id: str | None = None) -> list[dict]:
    """Aggregate bounded durable lots into the compatibility position shape."""
    return positions_for_context_with_status(
        conn, context, account_id=account_id,
    )[0]


def realized_pnl(conn, context: PortfolioReadContext, *,
                 account_id: str | None = None) -> tuple[float | None, str]:
    """Sum verified committed SELL realized PnL up to ``context.asof_day``."""
    if _cycle_is_archived(conn, context):
        return None, STATUS_UNKNOWN
    if _unproven_sell_exists(conn, context, account_id):
        return None, STATUS_UNKNOWN
    verified, _rows, proof_available = _sell_fills(conn, context, account_id)
    if not proof_available:
        return None, STATUS_UNKNOWN
    total = 0.0
    order_ids: set[int] = set()
    for row in verified:
        order_ids.add(int(row["order_id"]))
    if not order_ids:
        return 0.0, STATUS_VERIFIED
    placeholders = ",".join("?" for _ in order_ids)
    sorted_order_ids = tuple(sorted(order_ids))
    fill_counts = conn.execute(
        f"SELECT order_id, COUNT(*),"
        f"       SUM(CASE WHEN fill_date IS NOT NULL"
        f"                AND length(fill_date)>=10"
        f"                AND substr(fill_date,1,10)<=?"
        f"           THEN 1 ELSE 0 END)"
        f"  FROM paper_fills WHERE order_id IN ({placeholders})"
        f" GROUP BY order_id",
        (context.asof_day.isoformat(), *sorted_order_ids),
    ).fetchall()
    completeness = {
        int(row[0]): (int(row[1]), int(row[2] or 0))
        for row in fill_counts
    }
    if (
        len(completeness) != len(order_ids)
        or any(total != bounded for total, bounded in completeness.values())
    ):
        return None, STATUS_UNKNOWN
    orders = _row_dicts(conn.execute(
        f"SELECT id,realized_pnl FROM paper_orders WHERE id IN ({placeholders})",
        sorted_order_ids,
    ))
    for order in orders:
        value = _ledger_num(order.get("realized_pnl"))
        if value is None:
            return None, STATUS_UNKNOWN
        total += value
    return total, STATUS_VERIFIED


def _cycle_has_bounded_activity(conn, context: PortfolioReadContext,
                                account_id: str | None = None) -> bool:
    """Return whether bounded ledger evidence predates ``context.asof_day``."""
    day = context.asof_day.isoformat()
    account_sql = " AND account_id=?" if account_id else ""
    account_params: tuple = (str(account_id),) if account_id else ()
    lot_columns = {"cycle_id", "acquired_at"}
    if account_id:
        lot_columns.add("account_id")
    if _has_columns(conn, "paper_position_lots", lot_columns):
        row = conn.execute(
            "SELECT 1 FROM paper_position_lots WHERE cycle_id=?"
            " AND substr(acquired_at,1,10)<=?" + account_sql + " LIMIT 1",
            (context.cycle_id, day, *account_params),
        ).fetchone()
        if row is not None:
            return True
    if _has_columns(conn, "paper_fills", {"order_id", "account_id", "fill_date"}) and \
       _has_columns(conn, "paper_orders", {"id", "cycle_id"}):
        fill_account_sql = " AND f.account_id=?" if account_id else ""
        row = conn.execute(
            "SELECT 1 FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id"
            " WHERE o.cycle_id=? AND substr(f.fill_date,1,10)<=?"
            + fill_account_sql + " LIMIT 1",
            (context.cycle_id, day, *account_params),
        ).fetchone()
        if row is not None:
            return True
    order_columns = {"cycle_id", "executed_at"}
    if account_id:
        order_columns.add("account_id")
    if _has_columns(conn, "paper_orders", order_columns):
        row = conn.execute(
            "SELECT 1 FROM paper_orders WHERE cycle_id=?"
            " AND substr(executed_at,1,10)<=?" + account_sql + " LIMIT 1",
            (context.cycle_id, day, *account_params),
        ).fetchone()
        if row is not None:
            return True
    return False


def _cycle_created_by(conn, context: PortfolioReadContext,
                      account_id: str | None = None) -> bool:
    """Return whether an existing cycle row provably predates ``context.asof_day``.

    Missing creation evidence is **not** proof that the cycle already existed: a
    partially migrated ``paper_cycles`` table with ``id`` and ``capital`` but no
    ``created_at`` must not let an arbitrarily early as-of read publish the
    declared capital as verified.  Such a read falls back to bounded activity
    evidence, and stays unknown when that is absent too.
    """
    if not _has_columns(conn, "paper_cycles", {"id", "created_at"}):
        return _cycle_has_bounded_activity(conn, context, account_id=account_id)
    row = conn.execute(
        "SELECT created_at FROM paper_cycles WHERE id=?", (context.cycle_id,)
    ).fetchone()
    if row is None:
        return _cycle_has_bounded_activity(conn, context, account_id=account_id)
    created = _day_text(row[0])
    if created and created <= context.asof_day.isoformat():
        return True
    return _cycle_has_bounded_activity(conn, context, account_id=account_id)

def _account_attached_by(conn, context: PortfolioReadContext, account_id) -> bool:
    """Return whether ``account_id`` provably belonged to the cycle by ``asof_day``.

    A cycle's ``created_at`` says nothing about when a **given account** joined
    it: the supported mid-cycle attachment paths rebind
    ``paper_accounts.cycle_id`` and record a later
    ``paper_parameter_versions.effective_date``.  Without bounded
    attachment evidence the account must not lend its current ``initial_cash``
    to a snapshot that predates its participation.

    ``cycle existed by D`` is **not** ``account belonged to that cycle by D``, so
    this resolver never falls back to cycle creation.  The only alternative
    bounded evidence is account-scoped ledger activity, which proves the account
    was participating on a date that is already <= ``asof_day``.
    """
    if _has_columns(conn, "paper_parameter_versions",
                    {"cycle_id", "account_id", "effective_date"}):
        row = conn.execute(
            "SELECT MIN(effective_date) FROM paper_parameter_versions"
            " WHERE cycle_id=? AND account_id=?",
            (context.cycle_id, str(account_id)),
        ).fetchone()
        if row is not None and row[0] is not None:
            day = _day_text(row[0])
            return bool(day and day <= context.asof_day.isoformat())
    # No attachment provenance row: fall back only to account-scoped bounded
    # activity, never to the cycle's own creation evidence.
    return _cycle_has_bounded_activity(conn, context, account_id=account_id)


def _cycle_initial(conn, context: PortfolioReadContext, account_id: str | None = None):
    """Resolve cycle/account initial capital without reading current cash."""
    if account_id:
        if not _has_columns(conn, "paper_accounts", {"id", "initial_cash", "cycle_id"}):
            return None
        row = conn.execute(
            "SELECT initial_cash,cycle_id FROM paper_accounts WHERE id=?", (str(account_id),)
        ).fetchone()
        if row is None or int(row[1] or -1) != context.cycle_id:
            return None
        if not _account_attached_by(conn, context, account_id):
            return None
        return _ledger_num(row[0])
    if _has_columns(conn, "paper_cycles", {"id", "capital"}):
        cycle = conn.execute(
            "SELECT capital FROM paper_cycles WHERE id=?", (context.cycle_id,)
        ).fetchone()
        if cycle is not None:
            if not _cycle_created_by(conn, context):
                return None
            declared = _ledger_num(cycle[0], 0.0)
            if declared is not None and declared > 0:
                return declared
    if not _has_columns(conn, "paper_accounts", {"cycle_id", "initial_cash"}):
        return None
    row = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(initial_cash),0)"
        " FROM paper_accounts WHERE cycle_id=?",
        (context.cycle_id,),
    ).fetchone()
    if row is None or int(row[0] or 0) <= 0:
        return None
    return _ledger_num(row[1])


def _cash_flow_total(conn, context: PortfolioReadContext, account_id: str | None = None):
    if _cycle_is_archived(conn, context):
        return None, STATUS_UNKNOWN
    buys, buy_rows, buy_proof = _buy_fills(conn, context, account_id)
    sells, _sell_rows, sell_proof = _sell_fills(conn, context, account_id)
    buy_orders, buy_orders_proof = _all_filled_buy_orders(conn, context, account_id)
    if buy_orders_proof:
        verified_buy_order_ids = {
            int(row["order_id"]) for row in buys if row.get("order_id") is not None
        }
        for order in buy_orders:
            if not EV.is_verified_row(order):
                return None, STATUS_UNKNOWN
            if int(order["id"]) not in verified_buy_order_ids:
                return None, STATUS_UNKNOWN
    if not buy_proof or not sell_proof:
        any_fill = _has_any_fill_rows(conn, context, account_id)
        if any_fill is True:
            return None, STATUS_UNKNOWN
        if any_fill is None:
            return None, STATUS_UNKNOWN
        lots, _quantity_status = bounded_lots_with_status(
            conn, context, account_id=account_id,
        )
        _uncovered_cost, uncovered_count = _uncovered_lot_facts(conn, lots)
        if uncovered_count:
            return None, STATUS_UNKNOWN
        return 0.0, STATUS_VERIFIED
    if _unproven_sell_exists(conn, context, account_id):
        return None, STATUS_UNKNOWN
    if any(not _identity_ok(row) or not EV.is_verified_row(row) for row in buy_rows):
        return None, STATUS_UNKNOWN
    total = 0.0
    for row in buys:
        amount = _ledger_num(row.get("fill_amount"))
        fees = _ledger_num(row.get("fill_fees"), 0.0)
        if amount is None or fees is None:
            return None, STATUS_UNKNOWN
        total -= amount + fees
    for row in sells:
        amount = _ledger_num(row.get("fill_amount"))
        fees = _ledger_num(row.get("fill_fees"), 0.0)
        if amount is None or fees is None:
            return None, STATUS_UNKNOWN
        total += amount - fees
    lots, _quantity_status = bounded_lots_with_status(
        conn, context, account_id=account_id,
    )
    _uncovered_cost, uncovered_count = _uncovered_lot_facts(conn, lots)
    if uncovered_count:
        return None, STATUS_UNKNOWN
    return total, STATUS_VERIFIED


def cash(conn, context: PortfolioReadContext, *,
         account_id: str | None = None) -> tuple[float | None, str]:
    """Reconstruct bounded cash from the cycle declaration and verified fills."""
    if _cycle_is_archived(conn, context):
        return None, STATUS_UNKNOWN
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


def _uncovered_lot_facts(conn, lots: list[dict]):
    """Cash cost and count of durable lots without verified matching BUY fills.

    Returns ``(cost, count)``; ``cost`` is ``None`` when an uncovered lot
    carries non-finite ledger evidence, which is unknown rather than a number.
    """
    if not lots:
        return 0.0, 0
    reused_sources = _reused_source_orders(lots)
    uncovered_cost = 0.0
    uncovered_count = 0
    for lot in lots:
        if _verified_source_buy_fill(conn, lot, reused_sources=reused_sources) is not None:
            continue
        qty = _ledger_num(lot.get("qty"), 0.0)
        cost = _ledger_num(lot.get("cost"), 0.0)
        uncovered_count += 1
        if qty is None or cost is None:
            uncovered_cost = None
            continue
        if uncovered_cost is not None:
            uncovered_cost += qty * cost
    return uncovered_cost, uncovered_count


def compatibility_cash(conn, context: PortfolioReadContext, *,
                      account_id: str | None = None):
    """Bounded legacy/risk compatibility cash estimate.

    The strict :func:`cash` stays unknown when fill verification is absent.
    This narrower fallback is for the R21 risk port only: it keeps the value
    bounded to the requested cycle/as-of and accounts for recorded fills or
    held lot cost, without reading current account cash.
    """
    if _cycle_is_archived(conn, context):
        return None
    initial = _cycle_initial(conn, context, account_id)
    if initial is None:
        return None
    all_lots, _quantity_status = bounded_lots_with_status(
        conn, context, account_id=account_id,
    )
    open_lots = [
        row for row in all_lots if int(row.get("remaining_qty") or 0) > 0
    ]
    buys, buy_rows, buy_proof = _buy_fills(conn, context, account_id)
    sells, sell_rows, sell_proof = _sell_fills(conn, context, account_id)
    rows = list(buy_rows or ()) + list(sell_rows or ())
    if rows and (buy_proof or sell_proof):
        total = initial
        for row in rows:
            amount = _ledger_num(row.get("fill_amount"))
            fees = _ledger_num(row.get("fill_fees"), 0.0)
            if amount is None or fees is None:
                return None
            if str(row.get("fill_side") or "") == "buy":
                total -= amount + fees
            else:
                total += amount - fees
        uncovered_cost, _uncovered_count = _uncovered_lot_facts(conn, all_lots)
        if uncovered_cost is None:
            return None
        return total - uncovered_cost
    invested = sum(
        int(row.get("remaining_qty") or 0) * (_ledger_num(row.get("cost"), 0.0) or 0.0)
        for row in open_lots
    )
    return max(0.0, initial - invested)


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
    price = _num(value, None)
    if price is None or not math.isfinite(price) or price <= 0:
        return None
    return price


def _risk_state_rows_for_context(conn, context: PortfolioReadContext) -> list[dict]:
    """Return only runtime risk rows provably no newer than ``asof_day``.

    ``account_id`` / ``code`` are part of the prerequisite check: the caller
    indexes every returned row by both, so a partially migrated table without
    them must be treated as unavailable rather than raising ``KeyError``.
    """
    if not _has_columns(
        conn, "paper_position_risk_state",
        {"cycle_id", "account_id", "code", "initialized_at", "updated_at"},
    ):
        return []
    day = context.asof_day.isoformat()
    return _row_dicts(conn.execute(
        "SELECT * FROM paper_position_risk_state"
        " WHERE cycle_id=? AND substr(initialized_at,1,10)<=?"
        "   AND substr(updated_at,1,10)<=?",
        (context.cycle_id, day, day),
    ))


def risk_positions_for_context(conn, context: PortfolioReadContext, *,
                              account_id: str | None = None) -> list[dict]:
    """Bounded position read with cycle-owned runtime risk state for risk scans."""
    risk_state_rows = _risk_state_rows_for_context(conn, context)
    positions, status = positions_for_context_with_status(
        conn, context, account_id=account_id,
        risk_state_rows=risk_state_rows,
    )
    if status != STATUS_VERIFIED:
        raise PortfolioReadUnavailable("portfolio quantity proof is unknown")
    return positions


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
    positions, quantity_status = positions_for_context_with_status(
        conn, context, account_id=account_id,
    )
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
    if missing_codes or quantity_status != STATUS_VERIFIED:
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
        "quantity_status": quantity_status,
        "archived": _cycle_is_archived(conn, context),
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


# ─────────────────── owner fact contract（R27-B2C-4B）───────────────────
#
# 这一段是 portfolio/accounting owner **正式发布**的 fact contract：它回答"这个
# account 在这个 cycle 的这个业务日上，哪些记账事实能被证明"。三条刻意的边界：
#
# * **不回答 research 语义**（status / reason / 支持什么 thesis）—— 那些属于 research。
# * **不重复 market 词表**：本 owner 的核验范围是"cycle + account + asof 上的记账重建"，
#   不是"行情是否可信"。把 market 的 ``verification / verification_method`` 抄过来，
#   research 层就再也分不清"组合账本被证明"与"估值被证明"。
# * **不接受调用方提供的估值**。``nav`` / ``market_value`` / ``unrealized_pnl`` /
#   ``daily_pnl`` / ``daily_return`` 是**跨 owner 组合**事实（portfolio ledger + R24
#   market valuation），因此**不在**本契约的发布面上，签名里也没有 ``valuations``。
#
# fail closed 的两条硬规则（与 ``ExecutionFactContract`` 同一风格）：
#
# * ``status == unknown`` 时 ``value`` **必须**是 ``None``。绝不允许
#   ``status=unknown, value=123.45`` —— 那会让消费者忽略 status 后偷偷用一个没有被
#   证明的数字。
# * 归属（account 属于该 cycle、且在 asof 前已挂载）不可证明时，三种事实**全部**
#   ``unknown``，而不是"没卖出所以已实现盈亏 = verified 0"。（见 PFACT-06）

PORTFOLIO_FACT_CONTRACT_VERSION = "portfolio-fact-v1"

#: 本 owner 的核验**语义范围**。research 层必须能区分"组合记账是否被重建证明"与
#: "行情估值是否可信" —— 两者是不同维度，因此范围是契约的一部分。
PORTFOLIO_FACT_VERIFICATION_SCOPE = "cycle_account_asof_accounting"

PORTFOLIO_FACT_CASH = "cash"
PORTFOLIO_FACT_REALIZED_PNL = "realized_pnl"
PORTFOLIO_FACT_POSITION_COST_SUMMARY = "position_cost_summary"

#: 本 owner 发布的 fact 种类闭集，**顺序固定**（= 发布顺序）。
#: 调用方不需要猜顺序，deterministic fingerprint 与测试也因此稳定。
#:
#: 刻意**不含** ``nav`` / ``market_value`` / ``unrealized_pnl`` / ``daily_pnl`` /
#: ``daily_return`` / ``benchmark`` / ``quote_status``：它们是跨 owner 组合事实。
PORTFOLIO_FACT_KINDS = (
    PORTFOLIO_FACT_CASH,
    PORTFOLIO_FACT_REALIZED_PNL,
    PORTFOLIO_FACT_POSITION_COST_SUMMARY,
)

#: 本 owner 的核验结论闭集。刻意只有两态：owner 要么**证明了**这条记账事实，
#: 要么**证明不了**。"证据源本身不可用"（``source_unusable``）不是本 owner 发布的
#: 独立状态，因此 research 侧不得替它猜一个。
PORTFOLIO_FACT_STATUSES = (
    STATUS_VERIFIED,
    STATUS_UNKNOWN,
)


class PortfolioFactContractError(ValueError):
    """owner fact contract 的构造被拒绝 —— fail closed。

    ``reason`` 是稳定 machine code，供调用方与测试依赖；文案本身不承载判定：

    * ``version_mismatch`` —— fact contract 版本不符；
    * ``unknown_fact_kind`` —— fact 种类不在本 owner 的闭集里；
    * ``unknown_fact_status`` —— 核验结论不在本 owner 的闭集里；
    * ``alien_cycle_id`` / ``alien_account_id`` / ``alien_asof_day`` —— identity 不可证明；
    * ``unknown_fact_with_value`` —— ``status=unknown`` 却带了一个值（必须为 ``None``）；
    * ``field_not_a_position_cost_summary`` —— 该 kind 的值不是 typed summary；
    * ``field_not_a_finite_number`` / ``non_finite_fact_value`` —— 数值事实不是有限数；
    * ``non_canonical_position_count`` / ``non_canonical_cost_value`` —— summary 形状非法。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = str(reason)
        text = self.reason if not detail else "%s: %s" % (self.reason, detail)
        super().__init__(text)


@dataclass(frozen=True, slots=True)
class PositionCostSummary:
    """组合持仓成本的聚合摘要 —— 纯 value type。

    ``position_count`` 是**开仓**（``remaining_qty > 0``）的唯一 account/code 持仓数，
    ``cost_value`` 是 ``Σ remaining_qty × durable acquisition cost``。两者都从
    :func:`bounded_lots_with_status` 的 as-of bounded durable lots 派生，
    **不读** ``paper_positions``（R22 已明确它是 compatibility-only 投影）。
    """

    position_count: int
    cost_value: float

    def __post_init__(self) -> None:
        count = self.position_count
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PortfolioFactContractError(
                "non_canonical_position_count", repr(count),
            )
        value = self.cost_value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PortfolioFactContractError("non_canonical_cost_value", repr(value))
        number = float(value)
        if not math.isfinite(number):
            raise PortfolioFactContractError("non_canonical_cost_value", repr(value))
        object.__setattr__(self, "position_count", int(count))
        object.__setattr__(self, "cost_value", number)

    def as_dict(self) -> Mapping:
        return {"position_count": self.position_count, "cost_value": self.cost_value}


@dataclass(frozen=True, slots=True, init=False)
class PortfolioFactProjection:
    """portfolio/accounting owner 对**一条记账事实**的正式投影。

    research 层将来引用一条组合记账事实时，读到的就是它：fact contract 版本、事实种类、
    ``cycle`` / ``account`` / ``asof`` 身份、owner 自己的核验结论，以及**已经证明**的值。
    它**不**包含 research 语义，**不**携带 market 词表，也**不**包含任何估值表面。

    **没有公开 raw 构造器。** ``PortfolioFactProjection(...)`` 一律抛 ``TypeError``；
    唯一签发路径是 :func:`accounting_fact_projections`（它再走私有
    :func:`_issue_portfolio_fact_projection`）。否则任何调用方都能自述
    ``status`` / ``value`` / ``cycle_id`` / ``account_id`` / ``asof_day`` 然后声称
    "这是一条 portfolio fact" —— "唯一发布入口"就只是一句声明。

    两条结构性规则：

    * ``status == STATUS_UNKNOWN`` 时 ``value`` **必须**是 ``None``（构造期强制）；
    * 数值事实的值必须是**有限**浮点数，``position_cost_summary`` 的值必须是
      :class:`PositionCostSummary`。``known(0)`` 是**合法且已验证**的零，不会退化成
      ``unknown``；反之 ``unknown`` 也不会被补成 0。
    """

    version: str
    fact_kind: str
    cycle_id: int
    account_id: str
    asof_day: str
    status: str
    value: float | PositionCostSummary | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError(
            "PortfolioFactProjection has no public constructor: 调用方不能自述一条组合"
            "记账事实的种类 / 身份 / 业务日 / 核验结论 / 值。请使用 "
            "accounting_fact_projections(conn, context, account_id=...) —— 全部字段由 "
            "portfolio owner 从它自己 bounded 的重建结果派生"
        )

    def __post_init__(self) -> None:
        if self.version != PORTFOLIO_FACT_CONTRACT_VERSION:
            raise PortfolioFactContractError(
                "version_mismatch",
                f"{self.version!r} != {PORTFOLIO_FACT_CONTRACT_VERSION!r}",
            )
        if self.fact_kind not in PORTFOLIO_FACT_KINDS:
            raise PortfolioFactContractError("unknown_fact_kind", repr(self.fact_kind))
        if self.status not in PORTFOLIO_FACT_STATUSES:
            raise PortfolioFactContractError("unknown_fact_status", repr(self.status))
        if (
            isinstance(self.cycle_id, bool)
            or not isinstance(self.cycle_id, int)
            or self.cycle_id <= 0
        ):
            raise PortfolioFactContractError("alien_cycle_id", repr(self.cycle_id))
        account = str(self.account_id or "").strip()
        if not account:
            raise PortfolioFactContractError("alien_account_id", repr(self.account_id))
        object.__setattr__(self, "account_id", account)
        try:
            parsed_day = dt.date.fromisoformat(str(self.asof_day))
        except (TypeError, ValueError) as exc:
            raise PortfolioFactContractError(
                "alien_asof_day", repr(self.asof_day),
            ) from exc
        if parsed_day.isoformat() != str(self.asof_day):
            raise PortfolioFactContractError("alien_asof_day", repr(self.asof_day))

        if self.status == STATUS_UNKNOWN:
            # 结构性保证：未证明的事实**没有**值，消费者无法忽略 status 偷用数字。
            if self.value is not None:
                raise PortfolioFactContractError(
                    "unknown_fact_with_value",
                    f"{self.fact_kind} is unknown but carries {self.value!r}",
                )
            return
        if self.fact_kind == PORTFOLIO_FACT_POSITION_COST_SUMMARY:
            if type(self.value) is not PositionCostSummary:
                raise PortfolioFactContractError(
                    "field_not_a_position_cost_summary",
                    f"expected PositionCostSummary, got {type(self.value).__name__}",
                )
            return
        raw = self.value
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise PortfolioFactContractError(
                "field_not_a_finite_number",
                f"{self.fact_kind} requires a finite number, got {type(raw).__name__}",
            )
        number = float(raw)
        if not math.isfinite(number):
            raise PortfolioFactContractError(
                "non_finite_fact_value", f"{self.fact_kind}={number!r}",
            )
        object.__setattr__(self, "value", number)

    def as_dict(self) -> Mapping:
        value = self.value
        return {
            "version": self.version,
            "fact_kind": self.fact_kind,
            "cycle_id": self.cycle_id,
            "account_id": self.account_id,
            "asof_day": self.asof_day,
            "status": self.status,
            "value": value.as_dict() if isinstance(value, PositionCostSummary) else value,
        }


def _issue_portfolio_fact_projection(**fields: Any) -> PortfolioFactProjection:
    """签发一条 owner fact 投影。只有本模块的 :func:`_portfolio_fact` 调用它。

    绕过恒抛错的 ``__init__`` 并在设置完全部字段后跑 ``__post_init__``，使校验逻辑仍然
    只有一份、且紧挨字段定义（与 ``execution_verification._issue_fact_projection`` 及
    ``ai_research_contract._issue_evidence_ref`` 同一手法）。
    """
    projection = object.__new__(PortfolioFactProjection)
    for name, value in fields.items():
        object.__setattr__(projection, name, value)
    projection.__post_init__()
    return projection


def _portfolio_fact(
    context: PortfolioReadContext,
    account_id: str,
    fact_kind: str,
    value: float | PositionCostSummary | None,
) -> PortfolioFactProjection:
    """把 owner 的重建结果包成一条 typed fact。

    ``status`` **刻意不是参数**：它只由 ``value is None`` 决定。于是"自述
    ``status=verified`` 却没有值"（以及反过来）在结构上无法表达 —— 这正是
    ``PortfolioFactProjection.__post_init__`` 那条规则在上游的对应物。
    """
    unknown = value is None
    return _issue_portfolio_fact_projection(
        version=PORTFOLIO_FACT_CONTRACT_VERSION,
        fact_kind=fact_kind,
        cycle_id=context.cycle_id,
        account_id=str(account_id),
        asof_day=context.asof_day.isoformat(),
        status=STATUS_UNKNOWN if unknown else STATUS_VERIFIED,
        value=None if unknown else value,
    )


def _numeric_fact_value(value: Any, status: str) -> float | None:
    """``(owner 重建值, owner 核验结论)`` → 可发布的有限数值，否则 ``None``。

    ``verified 0.0`` 与 ``unknown`` 必须分开：前者返回 ``0.0``（一个**已被证明**的零），
    后者返回 ``None``。非有限账本值同样返回 ``None`` —— 绝不发布"已验证的无穷大"。
    """
    if status != STATUS_VERIFIED or value is None:
        return None
    number = _ledger_num(value)
    if number is None or not math.isfinite(number):
        return None
    return number


def _position_cost_summary(
    conn, context: PortfolioReadContext, account_id: str,
) -> PositionCostSummary | None:
    """as-of bounded durable-lot 成本摘要；无法证明时返回 ``None``。

    事实来源是 durable lots（``paper_position_lots``）减去 as-of 之前已验证的 SELL
    成交，而**不是** ``paper_positions``：R22 已明确后者是 compatibility-only 投影，
    它既没有 cycle 归属，也不携带 as-of 证据。
    """
    # 唯一 authority 仍然是 cycle/as-of bounded durable lots —— 兼容投影不参与。
    lots, quantity_status = bounded_lots_with_status(
        conn, context, account_id=account_id,
    )
    if quantity_status != STATUS_VERIFIED:
        # 数量未经证明时**不发布** summary：既不发 "能算多少算多少"，
        # 也不发 verified zero。
        return None
    positions: set[tuple[str, str]] = set()
    cost_value = 0.0
    for row in lots:
        remaining = _ledger_num(row.get("remaining_qty"))
        cost = _ledger_num(row.get("cost"))
        if remaining is None or cost is None:
            return None
        if remaining <= 0:
            continue
        positions.add((
            str(row.get("account_id") or ""), str(row.get("code") or ""),
        ))
        cost_value += remaining * cost
    if not math.isfinite(cost_value):
        return None
    return PositionCostSummary(position_count=len(positions), cost_value=cost_value)


def accounting_fact_projections(
    conn, context: PortfolioReadContext, *, account_id: str,
) -> tuple[PortfolioFactProjection, ...]:
    """本 owner 的**唯一** public typed fact 入口（R27-B2C-4B）。

    返回**固定顺序**的三条事实：``cash`` → ``realized_pnl`` →
    ``position_cost_summary``。顺序固定使调用方不需要猜，也便于 deterministic
    fingerprint 与回归断言。

    调用方必须显式给出 ``account_id``（非空）与 context 的 ``cycle_id`` /
    ``asof_day``。刻意**没有**任何 fallback：不取 active account、不取 active /
    current cycle、不取 ``today()``、不取 ``latest()``、不读 ``paper_accounts.cash``
    当前可变状态、不读 ``paper_nav``，签名里也**没有** ``valuations`` /
    ``MarketDataReading`` / current quote —— 组合账本 owner 不能把调用方给的裸市场价
    升级成自己的 verified 事实。

    归属证明优先复用既有 bounded owner logic：先要求
    :func:`_cycle_initial`（内部即 ``paper_accounts`` 的 cycle 绑定 +
    :func:`_account_attached_by`）能证明该 account 属于该 cycle 且在 ``asof_day`` 前已
    挂载。**证明不了**时三条事实全部 ``unknown`` 且 ``value=None`` —— 绝不退化成
    "这个账户没有卖出，所以已实现盈亏 = verified 0"（PFACT-06）。

    每条事实各自消费既有 owner 读路径：``cash`` 用 :func:`cash`（bounded 重建现金，
    不是当前账户余额），``realized_pnl`` 用 :func:`realized_pnl`（cycle/as-of bounded、
    只认已验证 SELL、incomplete sell fail closed、``realized_pnl`` 非有限即 unknown），
    ``position_cost_summary`` 用 :func:`_position_cost_summary`（durable lots）。
    本函数**不**再写第二套 SQL 判定。
    """
    if not isinstance(context, PortfolioReadContext):
        raise TypeError(
            "accounting_fact_projections requires a PortfolioReadContext, got "
            f"{type(context).__name__} — 业务日与 cycle 身份不得由调用方自述"
        )
    account = str(account_id or "").strip()
    if not account:
        raise ValueError(
            "accounting_fact_projections requires an explicit non-empty account_id "
            "（不接受 active account fallback）"
        )

    if _cycle_initial(conn, context, account_id=account) is None:
        # 归属/挂载不可证明 —— 绝不能发布 verified zero，也不能发布别的数字。
        return tuple(
            _portfolio_fact(context, account, kind, None)
            for kind in PORTFOLIO_FACT_KINDS
        )

    cash_value, cash_status = cash(conn, context, account_id=account)
    realized_value, realized_status = realized_pnl(conn, context, account_id=account)
    summary = _position_cost_summary(conn, context, account)
    return (
        _portfolio_fact(
            context, account, PORTFOLIO_FACT_CASH,
            _numeric_fact_value(cash_value, cash_status),
        ),
        _portfolio_fact(
            context, account, PORTFOLIO_FACT_REALIZED_PNL,
            _numeric_fact_value(realized_value, realized_status),
        ),
        _portfolio_fact(
            context, account, PORTFOLIO_FACT_POSITION_COST_SUMMARY, summary,
        ),
    )
