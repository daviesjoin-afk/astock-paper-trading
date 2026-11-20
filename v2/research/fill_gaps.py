#!/usr/bin/env python3
"""Fill interior gaps in a research copy with explicit halt rows.

A gap is a day the market traded, inside a symbol's own span, with no row at
all. The price-set builder drops such a symbol — correct, but it means a long
replay cannot settle a day where a held name has a hole, and a whole-window
backtest dies partway through.

The only honest source for a missing row is the official daytime package for
that day. This fetches exactly the days that have gaps, keeps rows the parser
marks `tradable=0` (a suspension, which is what a missing row usually is), and
inserts them. It never invents a price and never forward-fills.

If a gap cannot be explained by a suspension — the package has no row, or the
row is a real trade the copy somehow lacks — the day is reported as unresolved
rather than patched.

    python research/fill_gaps.py --db work/data/research.sqlite3 \
        --manifest work/data/gaps.json [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_sources"))

from ingest_market import fetch_package, parse_package  # noqa: E402


def find_gaps(conn: sqlite3.Connection, start: str, end: str) -> dict[str, list[str]]:
    """Map market day -> symbols expected to have a row but missing one.

    A symbol is expected on every market day inside its own [first, last] span.
    Days outside the span are listings and delistings, not gaps.
    """
    days = [row[0] for row in conn.execute(
        "SELECT DISTINCT day FROM bar WHERE adjust='none' AND day BETWEEN ? AND ? ORDER BY day",
        (start, end),
    )]
    if not days:
        return {}
    index = {day: position for position, day in enumerate(days)}
    spans: dict[str, list[int]] = {}
    for symbol, day in conn.execute(
        "SELECT symbol, day FROM bar WHERE adjust='none' AND day BETWEEN ? AND ?", (start, end)
    ):
        position = index.get(day)
        if position is not None:
            spans.setdefault(symbol, []).append(position)

    gaps: dict[str, list[str]] = {}
    for symbol, positions in spans.items():
        low, high = min(positions), max(positions)
        present = set(positions)
        for position in range(low, high + 1):
            if position in present:
                continue
            gaps.setdefault(days[position], []).append(symbol)
    return gaps


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--start", default="2023-01-03")
    parser.add_argument("--end", default="2026-11-11")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.db.exists():
        raise SystemExit(f"database not found: {args.db}")

    conn = sqlite3.connect(args.db)
    gaps = find_gaps(conn, args.start, args.end)
    total = sum(len(symbols) for symbols in gaps.values())
    print(f"market days with gaps: {len(gaps)}, missing rows: {total}", file=sys.stderr)
    if args.dry_run:
        preview = {day: symbols[:5] for day, symbols in list(gaps.items())[:5]}
        print(json.dumps({"days": len(gaps), "rows": total, "preview": preview},
                         ensure_ascii=False, indent=2))
        return 0

    inserted = 0
    unresolved: list[dict] = []
    per_day: list[dict] = []
    started = time.time()
    for position, (day, symbols) in enumerate(sorted(gaps.items()), 1):
        wanted = set(symbols)
        try:
            content = fetch_package(day)
        except Exception as error:  # network or package problem
            unresolved.append({"day": day, "reason": f"package_unavailable: {error}",
                               "symbols": sorted(wanted)})
            continue
        rows = parse_package(content, day.replace("-", ""))
        by_symbol = {row.symbol: row for row in rows}
        added = 0
        for symbol in wanted:
            row = by_symbol.get(symbol)
            if row is None:
                unresolved.append({"day": day, "symbol": symbol,
                                   "reason": "no_row_in_package"})
                continue
            if row.tradable:
                # The package shows a real trade here; the copy should have it.
                # Silently inserting would hide an ingest bug.
                unresolved.append({"day": day, "symbol": symbol,
                                   "reason": "package_shows_trade_but_copy_is_missing_it"})
                continue
            conn.execute(
                "INSERT OR IGNORE INTO bar(symbol,day,name,open_cents,high_cents,low_cents,"
                "close_cents,prev_close_cents,volume_shares,amount_cents,adjust,source,tradable) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'none','tdx-fill',0)",
                (row.symbol, row.day, row.name, row.open_cents, row.high_cents,
                 row.low_cents, row.close_cents, row.prev_close_cents,
                 row.volume_shares, row.amount_cents),
            )
            added += 1
        conn.commit()
        inserted += added
        per_day.append({"day": day, "wanted": len(wanted), "inserted": added})
        if position % 20 == 0 or position == len(gaps):
            elapsed = time.time() - started
            print(f"  {position}/{len(gaps)} days, +{inserted} rows, {elapsed:.0f}s",
                  file=sys.stderr)

    manifest = {
        "database": str(args.db),
        "window": [args.start, args.end],
        "days_with_gaps": len(gaps),
        "rows_missing": total,
        "rows_inserted": inserted,
        "unresolved": unresolved,
        "per_day": per_day,
        "note": ("Only suspension rows from the official package are inserted. "
                 "A gap the package cannot explain stays unresolved."),
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"inserted {inserted}/{total}; unresolved {len(unresolved)} -> {args.manifest}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
