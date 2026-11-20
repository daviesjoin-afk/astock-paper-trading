#!/usr/bin/env python3
"""Seed `bar` rows into the isolated browser-test database.

The kernel owns the schema, so this runs after it has migrated the file once.
Kept as a real script instead of an inline -c string: nested quoting is what
broke the first attempt, and a file is easier to read and debug.
"""
from __future__ import annotations

import json
import sqlite3
import sys

SYMBOLS = [("600000.SH", 1000, 3), ("000001.SZ", 1200, 4)]
DAYS = 70


def trading_days(count: int) -> list[str]:
    from datetime import date, timedelta

    day = date(2026, 7, 1)
    out: list[str] = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: seed-bars.py <sqlite-path>", file=sys.stderr)
        return 2
    days = trading_days(DAYS)
    rows = []
    for symbol, base, slope in SYMBOLS:
        for index, day in enumerate(days):
            close = base + index * slope
            previous = base + (index - 1) * slope if index else close
            rows.append((symbol, day, close, close, close, close, previous, 1000, close * 1000))
    with sqlite3.connect(sys.argv[1]) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO bar(symbol,day,open_cents,high_cents,low_cents,"
            "close_cents,prev_close_cents,volume_shares,amount_cents,adjust,source,tradable) "
            "VALUES(?,?,?,?,?,?,?,?,?,'none','e2e',1)",
            rows,
        )
        conn.commit()
        stored = conn.execute("SELECT count(*) FROM bar").fetchone()[0]
    print(f"seeded {len(rows)} bars, table now holds {stored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
