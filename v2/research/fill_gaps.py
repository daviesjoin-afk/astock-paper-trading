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

**The calendar is external to the copy.** The first version derived the market
calendar from `SELECT DISTINCT day FROM bar`, so a day on which *nothing* was
ingested was invisible: every symbol looked continuous across it and the report
said "no gaps". A whole-market outage is exactly the case this script exists to
repair, so the calendar now comes from a real calendar file. If no calendar
file can be found, or the requested window is not covered by it, the script
fails loudly instead of reporting a clean copy it never checked.

    python research/fill_gaps.py --db work/data/research.sqlite3 \
        --manifest work/data/gaps.json \
        [--calendar data/calendar-2026-v1.json] [--dry-run]
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_sources"))

from ingest_market import fetch_package, parse_package  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
# Where a calendar file is looked for when --calendar is not given. Only files
# that exist are used; finding none is an error, not an empty calendar.
CALENDAR_CANDIDATES = (
    REPO_ROOT / "data" / "calendar-2026-v1.json",
)


class CalendarMissing(Exception):
    """No usable trading calendar, so "no gaps" cannot be asserted."""


def load_calendar(path: Path) -> dict:
    """Read a calendar file: coverage bounds plus holiday ranges.

    The same schema the Rust domain uses (`data/calendar-2026-v1.json`), so
    there is one definition of a trading day in the repository and this script
    is not a second opinion.
    """
    if not path.is_file():
        raise CalendarMissing(f"calendar file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CalendarMissing(f"calendar unreadable: {path}: {error}") from error
    try:
        coverage_start = payload["coverage_start"]
        coverage_end = payload["coverage_end"]
        holidays = payload["holidays"]
    except (KeyError, TypeError) as error:
        raise CalendarMissing(
            f"calendar {path} lacks coverage_start/coverage_end/holidays"
        ) from error
    ranges = []
    for entry in holidays:
        try:
            ranges.append((entry["start"], entry["end"]))
        except (KeyError, TypeError) as error:
            raise CalendarMissing(f"calendar {path} has a malformed holiday entry") from error
    return {
        "path": str(path),
        "version": payload.get("version"),
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "holidays": ranges,
    }


def calendar_days(calendars: list[dict], start: str, end: str) -> list[str]:
    """Trading days in `[start, end]` according to the calendar file(s).

    Several files may be given; their coverage is unioned so a repository that
    ships one file per year still answers a multi-year window. A window that no
    file covers cannot be answered, and is refused rather than silently
    truncated — a partial calendar would under-report gaps exactly the way the
    DB-derived calendar did.
    """
    if not calendars:
        raise CalendarMissing("no calendar loaded")
    intervals = sorted((calendar["coverage_start"], calendar["coverage_end"])
                       for calendar in calendars)
    merged: list[list[str]] = []
    for first, last in intervals:
        if merged and first <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], last)
        else:
            merged.append([first, last])
    covered = any(first <= start and end <= last for first, last in merged)
    if not covered:
        described = ", ".join(f"{calendar['path']} ({calendar['coverage_start']}.."
                              f"{calendar['coverage_end']})" for calendar in calendars)
        raise CalendarMissing(
            f"calendar coverage {described} does not include the requested window "
            f"{start}..{end}"
        )
    blocked = set()
    for calendar in calendars:
        for holiday_start, holiday_end in calendar["holidays"]:
            cursor = date.fromisoformat(holiday_start)
            last = date.fromisoformat(holiday_end)
            while cursor <= last:
                blocked.add(cursor.isoformat())
                cursor += timedelta(days=1)
    days = []
    cursor = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while cursor <= last:
        if cursor.weekday() < 5 and cursor.isoformat() not in blocked:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def resolve_calendar(explicit: list[Path] | None) -> list[dict]:
    if explicit:
        return [load_calendar(path) for path in explicit]
    found = [path for path in CALENDAR_CANDIDATES if path.is_file()]
    if not found:
        raise CalendarMissing(
            "no trading calendar found; pass --calendar or place the repository "
            f"calendar at {CALENDAR_CANDIDATES[0]}. Without one, a missing market "
            "day is indistinguishable from a day with no gaps."
        )
    return [load_calendar(path) for path in found]


def find_gaps(conn: sqlite3.Connection, start: str, end: str,
              calendars: list[dict] | None = None) -> dict[str, list[str]]:
    """Map market day -> symbols expected to have a row but missing one.

    A symbol is expected on every market day inside its own [first, last] span.
    Days outside the span are listings and delistings, not gaps.

    The market days are the calendar's, not the copy's. A day the whole market
    is missing appears in the copy as an absence too — deriving days from the
    bars made it invisible.
    """
    if not calendars:
        raise CalendarMissing(
            "find_gaps needs a trading calendar; the bars in the copy cannot "
            "describe days they do not contain"
        )
    days = calendar_days(calendars, start, end)
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


def calendar_inconsistencies(conn: sqlite3.Connection, start: str, end: str,
                             calendars: list[dict]) -> list[str]:
    """Traded days in the copy that the calendar says are not trading days.

    Not a gap, but it means one of the two sources is wrong, and a repair run
    built on a wrong calendar would insert rows on the wrong days.
    """
    days = set(calendar_days(calendars, start, end))
    traded = {row[0] for row in conn.execute(
        "SELECT DISTINCT day FROM bar WHERE adjust='none' AND day BETWEEN ? AND ?",
        (start, end),
    )}
    return sorted(traded - days)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--calendar", type=Path, action="append",
                        help="trading calendar json; repeatable; defaults to the "
                             "repository copy")
    parser.add_argument("--start", default="2023-01-03")
    parser.add_argument("--end", default="2026-11-11")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.db.exists():
        raise SystemExit(f"database not found: {args.db}")
    try:
        calendars = resolve_calendar(args.calendar)
        conn = sqlite3.connect(args.db)
        gaps = find_gaps(conn, args.start, args.end, calendars)
        inconsistent = calendar_inconsistencies(conn, args.start, args.end, calendars)
    except CalendarMissing as error:
        # Not an empty result: an unanswerable question is reported as such.
        raise SystemExit(f"calendar_missing: {error}")
    calendar_label = ", ".join(f"{calendar['path']} ({calendar['version']})"
                               for calendar in calendars)
    total = sum(len(symbols) for symbols in gaps.values())
    print(f"calendar {calendar_label}", file=sys.stderr)
    print(f"market days with gaps: {len(gaps)}, missing rows: {total}", file=sys.stderr)
    if inconsistent:
        print(f"WARNING: {len(inconsistent)} traded day(s) are not in the calendar: "
              f"{', '.join(inconsistent[:5])}", file=sys.stderr)
    if args.dry_run:
        preview = {day: symbols[:5] for day, symbols in list(gaps.items())[:5]}
        print(json.dumps({"days": len(gaps), "rows": total, "preview": preview,
                          "calendars": [calendar["path"] for calendar in calendars],
                          "calendar_inconsistencies": inconsistent},
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
        "calendar": [calendar["path"] for calendar in calendars],
        "calendar_versions": [calendar["version"] for calendar in calendars],
        "calendar_inconsistencies": inconsistent,
        "days_with_gaps": len(gaps),
        "rows_missing": total,
        "rows_inserted": inserted,
        "unresolved": unresolved,
        "per_day": per_day,
        "note": ("Only suspension rows from the official package are inserted. "
                 "A gap the package cannot explain stays unresolved. Gaps are "
                 "measured against the trading calendar, not against the days "
                 "the copy happens to contain."),
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"inserted {inserted}/{total}; unresolved {len(unresolved)} -> {args.manifest}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
