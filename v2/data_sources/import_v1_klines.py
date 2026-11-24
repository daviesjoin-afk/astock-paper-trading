#!/usr/bin/env python3
"""Import the V1 archive's per-symbol K-line CSVs into the `bar` table.

The V1 runtime kept one CSV per symbol under `data_cache/klines/`:

    date,open,close,high,low,volume,amount,amplitude

5574 files, 4.9M rows, 2023-01-03 .. 2026-11-06.

Three things this deliberately does NOT hide:

1. **The adjustment kind is never inferred.** `kline_manifest.json` records
   `"adjustment": "qfq"` for 5568 of 5574 symbols (5 unadjusted, 2 unknown).
   Forward adjustment rewrites *history* using *future* dividends, so a
   backtest run on it sees prices nobody could have seen at the time. The
   number of decimal places in `close` is NOT evidence of anything: a fully
   2-decimal series can still be forward-adjusted (the adjustment only shows
   up around ex-dividend dates). A symbol without an explicit declaration in
   the manifest is therefore *refused*, not guessed. Refused symbols are
   reported and the run exits non-zero; nothing is written for them.

2. **Missing volume/amount stays missing.** A record whose `volume` or
   `amount` field is absent, blank or unparsable is stored as `NULL` and
   marked `tradable=0` -- the row keeps a usable close, but we do not claim it
   traded. A zero is a fact ("nothing traded"); a fabricated zero is a lie.
   If the target `bar` table declares those columns `NOT NULL` (the Rust
   schema does), the row is refused and counted instead of being filled with
   zeros.

3. **`prev_close` is derived, not authoritative.** These CSVs carry no
   previous-close column, so it is taken from the prior row's close (or the
   row's own open for the first row of a symbol). Unlike the daytime package,
   this cannot distinguish an ex-dividend gap from a crash -- in qfq data the
   gap has already been smoothed away, which is precisely why the gap check
   needs the unadjusted source.

Rows land with `source='v1-qfq'` so they never get confused with rows from
`ingest_market.py` (`source='tdx'`), which are unadjusted. Because both
sources share the `(symbol, day)` primary key, an archive row must never
overwrite an unadjusted TDX row: such conflicts are skipped and reported.

Usage:
    python import_v1_klines.py --db <path> --dir <.data/v1-klines>
    python import_v1_klines.py --db <path> --dir ... --symbols 600519,000001
    python import_v1_klines.py --dir ... --dry-run

Exit codes:
    0  everything selected was imported
    2  usage error (missing --dir/klines, no CSV matched, --db required)
    3  the import ran but is incomplete: at least one symbol was refused for
       a missing adjustment declaration, or at least one row could not keep
       its unknown volume/amount and was refused instead of zero-filled
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
# Adjustment kinds the manifest may declare. Anything else -- including a
# missing entry, a missing manifest and the literal "unknown" -- is refused.
ALLOWED_ADJUST = ("qfq", "none", "hfq")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3


@dataclass(frozen=True)
class Imported:
    symbol: str
    day: str
    open_cents: int
    high_cents: int
    low_cents: int
    close_cents: int
    prev_close_cents: int
    # None means "the source did not say"; never a fabricated zero.
    volume_shares: int | None
    amount_cents: int | None
    source: str
    tradable: bool


def to_cents(value: str) -> int:
    """Decimal string -> integer cents, without float rounding drift."""
    from decimal import Decimal, InvalidOperation

    try:
        return int((Decimal(value) * 100).to_integral_value())
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"bad price {value!r}")


def _decimal(value) -> "Decimal | None":
    from decimal import Decimal

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except Exception:  # noqa: BLE001 - any unparsable field is "unknown"
        return None


def optional_cents(value) -> int | None:
    """Missing/blank/unparsable -> None. Never a default of 0."""
    number = _decimal(value)
    if number is None or number < 0:
        return None
    from decimal import InvalidOperation

    try:
        return int((number * 100).to_integral_value())
    except (InvalidOperation, TypeError, ValueError):
        return None


def optional_shares(value) -> int | None:
    """Missing/blank/unparsable -> None. Never a default of 0."""
    number = _decimal(value)
    if number is None or number < 0:
        return None
    from decimal import InvalidOperation

    try:
        return int(number.to_integral_value())
    except (InvalidOperation, TypeError, ValueError):
        return None


def symbol_of(code: str) -> str:
    """`600519` -> `600519.SH`. Market is inferred from the code prefix."""
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return f"{code}.SH"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return f"{code}.SZ"
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"{code}.BJ"
    return f"{code}.SH"


def read_symbol(path: Path, code: str) -> list[Imported]:
    """Parse one symbol's CSV into rows, oldest first.

    The adjustment kind is NOT reported here: it can only come from the
    manifest, and a CSV cannot prove it. See `declared_adjustment`.
    """
    symbol = symbol_of(code)
    rows: list[Imported] = []
    prev_close: int | None = None
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
            volume = optional_shares(record.get("volume"))
            amount = optional_cents(record.get("amount"))
            rows.append(
                Imported(
                    symbol=symbol,
                    day=day,
                    open_cents=open_c,
                    high_cents=high_c,
                    low_cents=low_c,
                    close_cents=close_c,
                    prev_close_cents=prev_close if prev_close else open_c,
                    volume_shares=volume,
                    amount_cents=amount,
                    source="v1-qfq",
                    # No measured volume or turnover means we cannot claim the
                    # row was tradable; the prices still describe the session.
                    tradable=volume is not None and amount is not None,
                )
            )
            prev_close = close_c
    return rows


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


def declared_adjustment(manifest: dict[str, str], code: str) -> str | None:
    """The explicitly declared adjustment kind, or None when it is not declared.

    There is no fallback: an absent entry, a missing manifest and the literal
    "unknown" all mean "the archive did not tell us", and guessing would put a
    forward-adjusted series behind an unadjusted query.
    """
    declared = manifest.get(code)
    if isinstance(declared, str) and declared.strip().lower() in ALLOWED_ADJUST:
        return declared.strip().lower()
    return None


# Never overwrite an unadjusted TDX row: both sources share (symbol, day) and
# the settlement feed reads adjust='none'. A conflicting archive row is skipped.
INSERT_SQL = """
INSERT INTO bar(symbol,day,name,open_cents,high_cents,low_cents,close_cents,
                prev_close_cents,volume_shares,amount_cents,adjust,source,tradable)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(symbol,day) DO UPDATE SET
  open_cents=excluded.open_cents, high_cents=excluded.high_cents,
  low_cents=excluded.low_cents, close_cents=excluded.close_cents,
  prev_close_cents=excluded.prev_close_cents,
  volume_shares=excluded.volume_shares, amount_cents=excluded.amount_cents,
  adjust=excluded.adjust, source=excluded.source, tradable=excluded.tradable
WHERE NOT (bar.adjust = 'none' AND bar.source = 'tdx')
"""

PROTECTED_SQL = """
SELECT i.symbol, COUNT(*) FROM _incoming i
JOIN bar b ON b.symbol = i.symbol AND b.day = i.day
WHERE b.adjust = 'none' AND b.source = 'tdx'
GROUP BY i.symbol
"""


def null_tolerant(connection: sqlite3.Connection | None) -> bool:
    """True when the target `bar` table accepts NULL volume/amount.

    The Rust schema declares both columns NOT NULL. Rather than quietly
    relaxing a shared table (or writing a fake zero), the importer detects the
    constraint and refuses rows whose volume/amount the source never gave.
    """
    if connection is None:
        return True
    rows = connection.execute("PRAGMA table_info(bar)").fetchall()
    if not rows:
        return True
    not_null = {row[1]: bool(row[3]) for row in rows}
    return not not_null.get("volume_shares", False) and not not_null.get("amount_cents", False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="path to the V2 sqlite database")
    parser.add_argument("--dir", required=True, help="directory holding klines/")
    parser.add_argument("--symbols", help="comma-separated codes to limit the import")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    klines = root / "klines"
    if not klines.is_dir():
        print(f"error: no klines/ under {root}", file=sys.stderr)
        return EXIT_USAGE
    if not args.dry_run and not args.db:
        print("error: --db is required unless --dry-run", file=sys.stderr)
        return EXIT_USAGE

    only = set(args.symbols.split(",")) if args.symbols else None
    manifest = manifest_adjustments(root)

    files = sorted(klines.glob("*.csv"))
    if only:
        files = [f for f in files if f.stem in only]
    if not files:
        print("error: no CSVs matched", file=sys.stderr)
        return EXIT_USAGE

    write = not args.dry_run
    connection: sqlite3.Connection | None = None
    if args.db:
        if write:
            connection = sqlite3.connect(args.db, timeout=60)
            connection.execute("PRAGMA journal_mode=WAL")
        else:
            connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60)
    nulls_ok = null_tolerant(connection)

    total_rows = 0
    total_files = 0
    unknown_rows: dict[str, int] = {}
    refused: list[tuple[str, str]] = []
    refused_rows: dict[str, int] = {}
    protected: dict[str, int] = {}
    adjust_counts: dict[str, int] = {}
    pending: list[tuple] = []

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        if connection is not None:
            pairs = [(row[0], row[1]) for row in pending]
            connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _incoming("
                "symbol TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY(symbol, day))"
            )
            connection.execute("DELETE FROM _incoming")
            connection.executemany(
                "INSERT OR IGNORE INTO _incoming(symbol, day) VALUES(?,?)", pairs
            )
            for symbol, count in connection.execute(PROTECTED_SQL).fetchall():
                protected[symbol] = protected.get(symbol, 0) + int(count)
            if write:
                with connection:
                    connection.executemany(INSERT_SQL, pending)
        pending = []

    for path in files:
        code = path.stem
        if not code.isdigit() or len(code) != 6:
            refused.append((code, "not a six-digit A-share code"))
            continue
        # No explicit declaration -> refuse the whole symbol. Never infer the
        # adjustment kind from the number of decimal places in `close`.
        kind = declared_adjustment(manifest, code)
        if kind is None:
            refused.append(
                (code, "no explicit adjustment declaration in kline_manifest.json")
            )
            continue
        try:
            rows = read_symbol(path, code)
        except OSError as exc:
            refused.append((code, f"read error: {exc}"))
            continue
        if not rows:
            refused.append((code, "no usable rows"))
            continue

        adjust_counts[kind] = adjust_counts.get(kind, 0) + 1

        name = ""
        kept = 0
        for row in rows:
            unknown_fill = row.volume_shares is None or row.amount_cents is None
            if unknown_fill and not nulls_ok:
                # The target cannot hold NULL: keep the fact "unknown" by
                # refusing the row, not by writing a zero.
                refused_rows[code] = refused_rows.get(code, 0) + 1
                continue
            if unknown_fill:
                unknown_rows[code] = unknown_rows.get(code, 0) + 1
            pending.append(
                (
                    row.symbol, row.day, name, row.open_cents, row.high_cents,
                    row.low_cents, row.close_cents, row.prev_close_cents,
                    row.volume_shares, row.amount_cents, kind, row.source,
                    int(row.tradable),
                )
            )
            kept += 1
        if kept == 0:
            refused.append((code, "every row had unknown volume/amount"))
            continue
        total_rows += kept
        total_files += 1
        if len(pending) >= BATCH_ROWS:
            flush()
    flush()

    if connection is not None:
        connection.close()

    mode = "dry run" if args.dry_run else "imported"
    print(f"{mode}: {total_files} symbols, {total_rows:,} rows")
    print("adjustment:", dict(sorted(adjust_counts.items())))
    protected_rows = sum(protected.values())
    if protected_rows:
        print(f"protected: {protected_rows:,} row(s) already held by unadjusted TDX "
              f"rows were skipped instead of overwritten")
        for symbol, count in sorted(protected.items())[:10]:
            print(f"  {symbol}: {count:,}")
        if len(protected) > 10:
            print(f"  ... and {len(protected) - 10} more symbol(s)")
    if unknown_rows:
        print(f"unknown volume/amount: {sum(unknown_rows.values()):,} row(s) in "
              f"{len(unknown_rows)} symbol(s) were stored as NULL and marked "
              f"tradable=0 (no zero was invented)")
        for symbol, count in sorted(unknown_rows.items())[:10]:
            print(f"  {symbol}: {count:,}")
        if len(unknown_rows) > 10:
            print(f"  ... and {len(unknown_rows) - 10} more symbol(s)")
    if refused_rows:
        print(f"refused: {sum(refused_rows.values()):,} row(s) with unknown volume/amount "
              f"in {len(refused_rows)} symbol(s); the target bar table declares the "
              f"columns NOT NULL, so they were not zero-filled")
        for symbol, count in sorted(refused_rows.items())[:10]:
            print(f"  {symbol}: {count:,}")
        if len(refused_rows) > 10:
            print(f"  ... and {len(refused_rows) - 10} more symbol(s)")
    if refused:
        print(f"refused {len(refused)} file(s):", file=sys.stderr)
        for code, why in refused[:10]:
            print(f"  {code}: {why}", file=sys.stderr)
        if len(refused) > 10:
            print(f"  ... and {len(refused) - 10} more", file=sys.stderr)
    incomplete = bool(refused) or bool(refused_rows)
    if incomplete:
        print("error: import incomplete; nothing was inferred and nothing was "
              "zero-filled. Resolve the symbols above and re-run.", file=sys.stderr)
        return EXIT_INCOMPLETE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
