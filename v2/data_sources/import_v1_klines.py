#!/usr/bin/env python3
"""Import the V1 archive's per-symbol K-line CSVs into the `bar` table.

The V1 runtime kept one CSV per symbol under `data_cache/klines/`:

    date,open,close,high,low,volume,amount,amplitude

5574 files, 4.9M rows, 2023-01-03 .. 2026-11-06.

Two things this deliberately does NOT hide:

1. **The data is 前复权 (forward-adjusted).** `kline_manifest.json` records
   `"adjustment": "qfq"` for 5568 of 5574 symbols (5 are unadjusted, 2
   unknown). Forward adjustment rewrites *history* using *future* dividends,
   so a backtest run on it sees prices nobody could have seen at the time.
   For an after-the-fact chart this is fine; for walk-forward evaluation it
   is a look-ahead bug. Imported rows are tagged `adjust='qfq'` so callers
   can refuse them where that matters.

2. **`prev_close` is derived, not authoritative.** These CSVs carry no
   previous-close column, so it is taken from the prior row's close (or the
   row's own open for the first row of a symbol). Unlike the daytime package,
   this cannot distinguish an ex-dividend gap from a crash -- in qfq data the
   gap has already been smoothed away, which is precisely why the gap check
   needs the unadjusted source.

Rows land with `source='v1-qfq'` so they never get confused with rows from
`ingest_market.py` (`source='tdx'`), which are unadjusted.

Usage:
    python import_v1_klines.py --db <path> --dir <.data/v1-klines>
    python import_v1_klines.py --db <path> --dir ... --symbols 600519,000001
    python import_v1_klines.py --dir ... --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

BATCH_ROWS = 20_000


@dataclass(frozen=True)
class Imported:
    symbol: str
    day: str
    open_cents: int
    high_cents: int
    low_cents: int
    close_cents: int
    prev_close_cents: int
    volume_shares: int
    amount_cents: int
    adjust: str
    source: str


def to_cents(value: str) -> int:
    """Decimal string -> integer cents, without float rounding drift."""
    from decimal import Decimal, InvalidOperation

    try:
        return int((Decimal(value) * 100).to_integral_value())
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"bad price {value!r}")


def symbol_of(code: str) -> str:
    """`600519` -> `600519.SH`. Market is inferred from the code prefix."""
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return f"{code}.SH"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return f"{code}.SZ"
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"{code}.BJ"
    return f"{code}.SH"


def read_symbol(path: Path, code: str) -> tuple[list[Imported], str]:
    """Parse one symbol's CSV into rows, oldest first.

    Returns the rows plus the adjustment kind, so the caller can report what
    it imported.
    """
    symbol = symbol_of(code)
    rows: list[Imported] = []
    prev_close: int | None = None
    adjust = "unknown"
    with path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            try:
                open_c = to_cents(record["open"])
                close_c = to_cents(record["close"])
                high_c = to_cents(record["high"])
                low_c = to_cents(record["low"])
            except (KeyError, ValueError):
                continue
            if close_c <= 0 or open_c <= 0:
                continue
            day = (record.get("date") or "").strip()
            if len(day) != 10:
                continue
            # qfq data carries >2 decimal places on most historical rows; a
            # fully 2-decimal series is either unadjusted or barely adjusted.
            if "." in record["close"] and len(record["close"].split(".")[-1]) > 2:
                adjust = "qfq"
            rows.append(
                Imported(
                    symbol=symbol,
                    day=day,
                    open_cents=open_c,
                    high_cents=high_c,
                    low_cents=low_c,
                    close_cents=close_c,
                    prev_close_cents=prev_close if prev_close else open_c,
                    volume_shares=int(float(record.get("volume") or 0)),
                    amount_cents=to_cents(record.get("amount") or "0"),
                    adjust="qfq",
                    source="v1-qfq",
                )
            )
            prev_close = close_c
    if adjust == "unknown" and rows:
        adjust = "none"
    return rows, adjust


def manifest_adjustments(dir_path: Path) -> dict[str, str]:
    """Per-symbol `adjustment` from kline_manifest.json, when present."""
    manifest = dir_path / "kline_manifest.json"
    if not manifest.exists():
        return {}
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    return {
        code: entry.get("adjustment", "unknown")
        for code, entry in payload.get("stocks", {}).items()
    }


INSERT_SQL = """
INSERT INTO bar(symbol,day,name,open_cents,high_cents,low_cents,close_cents,
                prev_close_cents,volume_shares,amount_cents,adjust,source)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(symbol,day) DO UPDATE SET
  open_cents=excluded.open_cents, high_cents=excluded.high_cents,
  low_cents=excluded.low_cents, close_cents=excluded.close_cents,
  prev_close_cents=excluded.prev_close_cents,
  volume_shares=excluded.volume_shares, amount_cents=excluded.amount_cents,
  adjust=excluded.adjust, source=excluded.source
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="path to the V2 sqlite database")
    parser.add_argument("--dir", required=True, help="directory holding klines/")
    parser.add_argument("--symbols", help="comma-separated codes to limit the import")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    klines = root / "klines"
    if not klines.is_dir():
        print(f"error: no klines/ under {root}", file=sys.stderr)
        return 2
    if not args.dry_run and not args.db:
        print("error: --db is required unless --dry-run", file=sys.stderr)
        return 2

    only = set(args.symbols.split(",")) if args.symbols else None
    manifest = manifest_adjustments(root)

    files = sorted(klines.glob("*.csv"))
    if only:
        files = [f for f in files if f.stem in only]
    if not files:
        print("error: no CSVs matched", file=sys.stderr)
        return 2

    connection = None
    if not args.dry_run:
        connection = sqlite3.connect(args.db, timeout=60)
        connection.execute("PRAGMA journal_mode=WAL")

    total_rows = 0
    total_files = 0
    skipped: list[tuple[str, str]] = []
    adjust_counts: dict[str, int] = {}
    pending: list[tuple] = []

    def flush() -> None:
        nonlocal pending
        if connection is not None and pending:
            with connection:
                connection.executemany(INSERT_SQL, pending)
            pending = []

    for path in files:
        code = path.stem
        if not code.isdigit() or len(code) != 6:
            skipped.append((code, "not a six-digit A-share code"))
            continue
        try:
            rows, detected = read_symbol(path, code)
        except OSError as exc:
            skipped.append((code, f"read error: {exc}"))
            continue
        if not rows:
            skipped.append((code, "no usable rows"))
            continue

        # The manifest is authoritative when it covers the symbol; the CSV
        # heuristic is only a fallback.
        declared = manifest.get(code)
        kind = declared if declared in ("qfq", "none", "hfq") else detected
        adjust_counts[kind] = adjust_counts.get(kind, 0) + 1

        name = ""
        for row in rows:
            pending.append(
                (
                    row.symbol, row.day, name, row.open_cents, row.high_cents,
                    row.low_cents, row.close_cents, row.prev_close_cents,
                    row.volume_shares, row.amount_cents, kind, row.source,
                )
            )
        total_rows += len(rows)
        total_files += 1
        if len(pending) >= BATCH_ROWS:
            flush()
    flush()

    if connection is not None:
        connection.close()

    mode = "dry run" if args.dry_run else "imported"
    print(f"{mode}: {total_files} symbols, {total_rows:,} rows")
    print("adjustment:", dict(sorted(adjust_counts.items())))
    if skipped:
        print(f"skipped {len(skipped)} file(s):")
        for code, why in skipped[:10]:
            print(f"  {code}: {why}")
        if len(skipped) > 10:
            print(f"  ... and {len(skipped) - 10} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
