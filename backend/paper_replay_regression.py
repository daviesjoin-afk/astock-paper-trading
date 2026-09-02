#!/usr/bin/env python3
"""Read-only paper-ledger replay invariants.

This is intentionally dependency-free and never imports the trading engine:
it can run against a stopped or live WAL SQLite ledger without changing an
order, position, risk decision, or job state.  It is a regression guard for
the most damaging paper-account failures: phantom fills, FIFO-lot drift and
unexplained capital reservations.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def validate(path: str) -> dict:
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"paper_orders", "paper_fills", "paper_positions", "paper_position_lots", "paper_capital_reservations"}
        missing = sorted(required - tables)
        result = {"ok": not missing, "db": os.path.abspath(path), "missing_tables": missing, "errors": [], "warnings": [], "checks": {}}
        if missing:
            return result

        phantom = _rows(conn, """SELECT id,status,code,side,filled_price,amount,fees,executed_at
            FROM paper_orders
            WHERE status <> 'filled' AND (filled_price IS NOT NULL OR amount IS NOT NULL OR fees IS NOT NULL OR executed_at IS NOT NULL)
            ORDER BY id DESC LIMIT 100""")
        result["checks"]["nonfilled_orders_with_fill_fields"] = len(phantom)
        if phantom:
            result["errors"].append({"kind": "phantom_order_fill", "rows": phantom})

        orphan_fills = _rows(conn, """SELECT f.id,f.order_id,f.account_id,f.code,f.qty
            FROM paper_fills f LEFT JOIN paper_orders o ON o.id=f.order_id
            WHERE o.id IS NULL ORDER BY f.id DESC LIMIT 100""")
        result["checks"]["orphan_fills"] = len(orphan_fills)
        if orphan_fills:
            result["errors"].append({"kind": "orphan_fill", "rows": orphan_fills})

        position_bad = _rows(conn, """SELECT account_id,code,qty,cost FROM paper_positions
            WHERE qty < 0 OR cost < 0 OR (qty % 100) <> 0 ORDER BY account_id,code LIMIT 100""")
        result["checks"]["invalid_positions"] = len(position_bad)
        if position_bad:
            result["errors"].append({"kind": "invalid_position", "rows": position_bad})

        lot_drift = _rows(conn, """SELECT p.account_id,p.code,p.qty AS position_qty,
                   COALESCE(SUM(CASE WHEN l.remaining_qty > 0 THEN l.remaining_qty ELSE 0 END),0) AS lot_qty
            FROM paper_positions p LEFT JOIN paper_position_lots l
              ON l.account_id=p.account_id AND l.code=p.code
            GROUP BY p.account_id,p.code HAVING p.qty <> lot_qty
            ORDER BY ABS(p.qty-lot_qty) DESC LIMIT 100""")
        result["checks"]["position_lot_drift"] = len(lot_drift)
        if lot_drift:
            result["errors"].append({"kind": "position_lot_drift", "rows": lot_drift})

        stale_reservations = _rows(conn, """SELECT r.id,r.order_key,r.account_id,r.code,r.amount,r.status,r.created_at
            FROM paper_capital_reservations r
            LEFT JOIN paper_orders o ON CAST(o.id AS TEXT)=r.order_key
            WHERE r.status='reserved' AND o.id IS NULL
            ORDER BY r.id DESC LIMIT 100""")
        result["checks"]["unattached_reserved_capital"] = len(stale_reservations)
        if stale_reservations:
            result["warnings"].append({"kind": "unattached_reserved_capital", "rows": stale_reservations})

        result["checks"]["orders"] = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
        result["checks"]["fills"] = conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        result["checks"]["positions"] = conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
        result["ok"] = not result["errors"]
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_cache", "paper_trading.sqlite3")
    report = validate(sys.argv[1] if len(sys.argv) > 1 else default)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    raise SystemExit(0 if report["ok"] else 2)
