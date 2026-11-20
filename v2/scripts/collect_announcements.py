#!/usr/bin/env python3
"""Collect CNINFO announcements for a symbol universe into a local table.

Announcements are the first data dimension in this project that carries a real
publication date per row. That matters: every other candidate dimension was
either already exhausted (close, volume, turnover, OHLC shape) or arrives as a
*snapshot* with no history (`company` reports `historical_as_of_supported:
false`, so its industry label is today's, not the label in force when a fold
traded -- using it would be look-ahead).

An announcement says "this was published on 2024-02-24". That is a fact about
the past that does not change, so a factor built on it cannot borrow from the
future.

What this collects and what it refuses to do:

* Stores only what CNINFO returned: date, category code, title, source id.
* Never infers a category from a title, never fills a missing date.
* Idempotent: re-running over the same window does not duplicate rows.

Usage:
    python v2/scripts/collect_announcements.py \
        --symbols symbols.txt --out announcements.sqlite3 \
        --start 2023-01-01 --end 2026-11-11
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_sources"))

from public_data import PublicDataClient, PublicDataError  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcement(
    symbol TEXT NOT NULL,
    announcement_date TEXT NOT NULL,
    announcement_id TEXT NOT NULL,
    category_raw TEXT,
    title TEXT,
    source TEXT,
    source_url TEXT,
    fetched_at TEXT,
    PRIMARY KEY(symbol, announcement_id)
);
CREATE INDEX IF NOT EXISTS announcement_day ON announcement(announcement_date);
CREATE INDEX IF NOT EXISTS announcement_symbol_day ON announcement(symbol, announcement_date);
CREATE TABLE IF NOT EXISTS collect_log(
    symbol TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    rows INTEGER,
    detail TEXT,
    attempted_at TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def already_done(conn: sqlite3.Connection, symbol: str) -> bool:
    row = conn.execute(
        "SELECT status FROM collect_log WHERE symbol = ?", (symbol,)
    ).fetchone()
    return bool(row) and row[0] == "ok"


def store(conn: sqlite3.Connection, symbol: str, rows: list[dict]) -> int:
    written = 0
    for row in rows:
        day = row.get("announcement_date") or row.get("source_date")
        ann_id = row.get("announcement_id") or row.get("source_id")
        # A row without a date or an id cannot be placed in time or deduplicated.
        # Dropping it is the honest option: inventing either would put a fact
        # into the table that the source never asserted.
        if not day or not ann_id:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO announcement"
            "(symbol, announcement_date, announcement_id, category_raw, title,"
            " source, source_url, fetched_at) VALUES(?,?,?,?,?,?,?,?)",
            (symbol, day, str(ann_id), row.get("category_raw"), row.get("title"),
             row.get("source"), row.get("source_url"), row.get("fetched_at")),
        )
        written += 1
    return written


def log(conn: sqlite3.Connection, symbol: str, status: str, rows: int,
        detail: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO collect_log(symbol, status, rows, detail, attempted_at)"
        " VALUES(?,?,?,?,datetime('now'))",
        (symbol, status, rows, detail),
    )
    conn.commit()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, type=Path,
                        help="one canonical symbol per line")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--limit", type=int, default=0, help="0 means no limit")
    parser.add_argument("--sleep", type=float, default=0.25,
                        help="seconds between requests, to stay polite")
    parser.add_argument("--max-pages", type=int, default=40)
    parser.add_argument("--page-size", type=int, default=30,
                        help="CNINFO rejects larger pages with 'empty or repeated page'")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=1.5,
                        help="seconds; grows linearly with each retry")
    args = parser.parse_args(argv)

    symbols = [line.strip() for line in args.symbols.read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.startswith("#")]
    if args.limit:
        symbols = symbols[:args.limit]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    client = PublicDataClient()
    with sqlite3.connect(args.out) as conn:
        ensure_schema(conn)
        todo = [s for s in symbols if not already_done(conn, s)]
        print(f"symbols={len(symbols)} pending={len(todo)}", flush=True)
        for index, symbol in enumerate(todo, 1):
            # CNINFO's paging intermittently reports a short count when a burst
            # of requests lands while its index is being rebuilt. The same
            # symbol succeeds on a retry, so retrying is the correct response --
            # but the shortfall is still recorded if retries do not clear it.
            result = None
            last_error = None
            for attempt in range(args.retries):
                try:
                    result = client.announcements(symbol, args.start, args.end,
                                                  page_size=args.page_size,
                                                  max_pages=args.max_pages)
                    break
                except (PublicDataError, OSError) as error:
                    last_error = error
                    if attempt + 1 < args.retries:
                        time.sleep(args.backoff * (attempt + 1))
            if result is None:
                log(conn, symbol, "error", 0, str(last_error)[:200])
                print(f"  [{index}/{len(todo)}] {symbol} ERROR {str(last_error)[:60]}", flush=True)
                continue
            if result.get("status") != "ok":
                log(conn, symbol, "error", 0, str(result.get("code")))
                print(f"  [{index}/{len(todo)}] {symbol} {result.get('code')}", flush=True)
                continue
            rows = (result.get("data") or {}).get("rows") or []
            written = store(conn, symbol, rows)
            conn.commit()
            log(conn, symbol, "ok", written)
            if index % 25 == 0 or index == len(todo):
                print(f"  [{index}/{len(todo)}] {symbol} rows={written}", flush=True)
            time.sleep(args.sleep)

        total = conn.execute("SELECT COUNT(*) FROM announcement").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM collect_log WHERE status='ok'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM collect_log WHERE status='error'").fetchone()[0]
    print(json.dumps({"rows": total, "symbols_ok": done, "symbols_failed": failed},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
