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

Resuming is by covered window, not by "this symbol once succeeded". A symbol is
skipped only when the requested ``--start/--end`` is entirely inside what has
already been collected for it; widening the window (or asking for a window that
sticks out of it) re-collects the whole request and unions the result into the
recorded coverage. Coverage never claims more than was fetched: two windows that
leave a gap between them stay two intervals, and only a single contiguous run is
summarised in ``collect_log.covered_start/covered_end`` (a run with a hole is
left NULL there, because one pair of dates cannot describe it).

Rows written before this rule existed have no recorded window, so their coverage
is unknown and they are collected once more -- that costs one pass and cannot
silently miss a range.

Usage:
    python v2/scripts/collect_announcements.py \
        --symbols symbols.txt --out announcements.sqlite3 \
        --start 2023-01-01 --end 2026-11-11
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, timedelta
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
    attempted_at TEXT,
    covered_start TEXT,
    covered_end TEXT,
    covered_ranges TEXT
);
"""

# Columns added after the first release. An existing database keeps its rows and
# gains the columns here, so resuming never requires deleting what was collected.
ADDED_COLUMNS = (
    ("covered_start", "TEXT"),
    ("covered_end", "TEXT"),
    ("covered_ranges", "TEXT"),
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the schema in place; never drop data."""
    conn.executescript(SCHEMA)
    present = {row[1] for row in conn.execute("PRAGMA table_info(collect_log)")}
    for name, decl in ADDED_COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE collect_log ADD COLUMN {name} {decl}")
    conn.commit()


def day_id(value: str) -> str:
    """Normalise to YYYY-MM-DD or fail loudly: a bad range must not be guessed."""
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as error:
        raise SystemExit(f"invalid date {value!r}: expected YYYY-MM-DD") from error


def _merge_ranges(ranges: list[list[str]]) -> list[list[str]]:
    """Union of [start, end] windows, merging only contiguous or overlapping ones.

    Two windows with a real hole between them stay two ranges: a single
    (start, end) pair would claim the hole was collected when it never was.
    """
    ordered = sorted(([day_id(start), day_id(end)] for start, end in ranges),
                     key=lambda item: item[0])
    merged: list[list[str]] = []
    for start, end in ordered:
        if merged and start <= (date.fromisoformat(merged[-1][1]) + timedelta(days=1)).isoformat():
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


def covered_ranges(conn: sqlite3.Connection, symbol: str) -> list[list[str]]:
    """Windows already collected for this symbol (empty when unknown).

    The coverage columns are the record of what was fetched; ``status`` only says
    how the last attempt went. A row written before coverage existed has no
    window at all, so it reports nothing and is collected again.
    """
    row = conn.execute(
        "SELECT covered_ranges, covered_start, covered_end FROM collect_log"
        " WHERE symbol = ?", (symbol,),
    ).fetchone()
    if not row:
        return []
    ranges_json, start, end = row
    if ranges_json:
        try:
            parsed = json.loads(ranges_json)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and all(
                isinstance(item, list) and len(item) == 2 for item in parsed):
            return _merge_ranges(parsed)
    if start and end:
        return _merge_ranges([[start, end]])
    return []


def already_done(conn: sqlite3.Connection, symbol: str, start: str, end: str) -> bool:
    """True only when the whole requested window is inside collected coverage.

    "This symbol succeeded once" is not enough: widening ``--end`` (or asking for
    a different window) must re-collect, because nothing proves the new part was
    ever fetched.
    """
    start, end = day_id(start), day_id(end)
    for lo, hi in covered_ranges(conn, symbol):
        if lo <= start and end <= hi:
            return True
    return False


def record_coverage(conn: sqlite3.Connection, symbol: str, start: str, end: str) -> None:
    """Union the window just collected into the symbol's recorded coverage."""
    merged = _merge_ranges(covered_ranges(conn, symbol) + [[day_id(start), day_id(end)]])
    # covered_start/covered_end are readable only when one interval describes the
    # whole coverage; with a hole they stay NULL rather than claim the hole.
    span = (merged[0][0], merged[-1][1]) if len(merged) == 1 else (None, None)
    conn.execute(
        "UPDATE collect_log SET covered_start = ?, covered_end = ?, covered_ranges = ?"
        " WHERE symbol = ?",
        (span[0], span[1], json.dumps(merged), symbol),
    )


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
        "INSERT INTO collect_log(symbol, status, rows, detail, attempted_at)"
        " VALUES(?,?,?,?,datetime('now'))"
        " ON CONFLICT(symbol) DO UPDATE SET status=excluded.status, rows=excluded.rows,"
        " detail=excluded.detail, attempted_at=excluded.attempted_at",
        (symbol, status, rows, detail),
    )
    conn.commit()


def collect_symbol(client, conn: sqlite3.Connection, symbol: str, start: str, end: str,
                   *, page_size: int, max_pages: int, retries: int, backoff: float) -> str:
    """Collect one symbol over one window; return "ok", "error" or "skipped"."""
    if already_done(conn, symbol, start, end):
        return "skipped"
    # CNINFO's paging intermittently reports a short count when a burst
    # of requests lands while its index is being rebuilt. The same
    # symbol succeeds on a retry, so retrying is the correct response --
    # but the shortfall is still recorded if retries do not clear it.
    result = None
    last_error = None
    for attempt in range(retries):
        try:
            result = client.announcements(symbol, start, end,
                                          page_size=page_size,
                                          max_pages=max_pages)
            break
        except (PublicDataError, OSError) as error:
            last_error = error
            if attempt + 1 < retries:
                time.sleep(backoff * (attempt + 1))
    if result is None:
        log(conn, symbol, "error", 0, str(last_error)[:200])
        return "error"
    if result.get("status") != "ok":
        log(conn, symbol, "error", 0, str(result.get("code")))
        return "error"
    rows = (result.get("data") or {}).get("rows") or []
    written = store(conn, symbol, rows)
    conn.commit()
    log(conn, symbol, "ok", written)
    # Only a successful fetch may extend coverage: an error leaves the window
    # recorded as uncollected so the next run retries it.
    record_coverage(conn, symbol, start, end)
    conn.commit()
    return "ok"


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

    start, end = day_id(args.start), day_id(args.end)
    if start > end:
        parser.error("--start is after --end")

    symbols = [line.strip() for line in args.symbols.read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.startswith("#")]
    if args.limit:
        symbols = symbols[:args.limit]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    client = PublicDataClient()
    # closing(): the sqlite3 connection context manager commits but never closes,
    # and on Windows an open handle keeps the database file locked after exit.
    with closing(sqlite3.connect(args.out)) as conn:
        ensure_schema(conn)
        todo = [s for s in symbols if not already_done(conn, s, start, end)]
        skipped = [s for s in symbols if s not in todo]
        print(f"symbols={len(symbols)} pending={len(todo)}"
              f" covered={len(skipped)} window={start}..{end}", flush=True)
        for index, symbol in enumerate(todo, 1):
            status = collect_symbol(client, conn, symbol, start, end,
                                    page_size=args.page_size, max_pages=args.max_pages,
                                    retries=args.retries, backoff=args.backoff)
            if status != "ok":
                print(f"  [{index}/{len(todo)}] {symbol} ERROR", flush=True)
                continue
            written = conn.execute(
                "SELECT rows FROM collect_log WHERE symbol = ?", (symbol,)).fetchone()[0]
            if index % 25 == 0 or index == len(todo):
                print(f"  [{index}/{len(todo)}] {symbol} rows={written}", flush=True)
            time.sleep(args.sleep)

        total = conn.execute("SELECT COUNT(*) FROM announcement").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM collect_log WHERE status='ok'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM collect_log WHERE status='error'").fetchone()[0]
    print(json.dumps({"rows": total, "symbols_ok": done, "symbols_failed": failed,
                      "skipped_covered": len(skipped), "window": [start, end]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
