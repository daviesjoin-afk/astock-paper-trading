#!/usr/bin/env python3
"""Whole-market daily ingest from the Tongdaxin after-close package.

One request (~2.7 MB) yields every SH/SZ/BJ security's OHLCV for a trading
day. That is the whole reason this exists: the old reviewed-price-set model
needed a human to approve two symbols at a time, which cannot scale.

Writes straight into the `bar` table of the V2 database. No quality layer, no
cross-source reconciliation, no review gate -- see
docs/V3-OPTIMIZATION-PLAN.md section 6.5 for why, and for the one exception
(prev_close is stored so ex-dividend gaps stay visible).

Usage:
    python ingest_market.py --db <path> --day 2026-11-10
    python ingest_market.py --db <path> --day 2026-11-10 --dry-run
    python ingest_market.py --db <path> --days 2026-11-03:2026-11-10
"""

from __future__ import annotations

import argparse
import io
import math
import re
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

PACKAGE_URL = "https://www.tdx.com.cn/products/data/data/g4day/{ymd}.zip"
USER_AGENT = "Mozilla/5.0 (compatible; astock-v2)"

# Per-market floor on rows carrying a price. Measured 2026-09-20 across 11
# real packages; the minimum observed was SH 16202 / SZ 3641 / BJ 87, so these
# thresholds sit clearly below that. A package under the floor is truncated.
MIN_PRICED = {"sh": 10_000, "sz": 3_000, "bj": 50}

# Packages before this date carry no Beijing exchange files at all.
BJ_FIRST_DAY = "20220506"

CODE_RE = re.compile(r"[0-9]{6}")

# The package carries every security TDX knows about: 22k bonds, 1.7k funds,
# 1.1k TDX-internal board indices (880*/881*), exchange indices. Only ordinary
# A-shares belong in the trading universe. A full day is ~5.6k stocks versus
# 52k total rows, so filtering here removes 89% of the noise before it ever
# reaches the database.
STOCK_PREFIXES: dict[str, tuple[str, ...]] = {
    "sh": ("600", "601", "603", "605", "688", "689"),
    "sz": ("000", "001", "002", "003", "300", "301"),
    "bj": ("43", "83", "87", "88", "92"),
}


def is_ordinary_a_share(market: str, code: str) -> bool:
    prefixes = STOCK_PREFIXES.get(market)
    if prefixes is None:
        return False
    return code.startswith(prefixes)


class IngestError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Row:
    symbol: str
    day: str
    name: str
    open_cents: int
    high_cents: int
    low_cents: int
    close_cents: int
    prev_close_cents: int
    volume_shares: int
    amount_cents: int

    def change_bps(self) -> int:
        """Change from previous close in basis points; 0 when prev close absent."""
        if self.prev_close_cents <= 0:
            return 0
        return (self.close_cents - self.prev_close_cents) * 10_000 // self.prev_close_cents


def _cents(value: float) -> int:
    """Round a decimal price to integer cents.

    The package stores prices as little-endian f64. Values are exact to 2 dp in
    practice, but round() keeps a 0.005 representation error from shaving a cent.
    """
    return int(round(value * 100))


def parse_package(content: bytes, ymd: str) -> list[Row]:
    """Parse a daytime package into rows.

    Layout: per market, a `.cod` code table (150 bytes/record) and an `.md1`
    quote block (512 bytes/block); the code record carries the block index.
    Reference: jing2uo/tdx2db (MIT), cross-checked against Tencent closes.
    """
    day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise IngestError("package_not_zip", str(exc)) from exc

    names = set(archive.namelist())
    rows: list[Row] = []
    for market in ("sh", "sz", "bj"):
        cod_name = f"{market}{ymd[2:]}.cod"
        md1_name = f"{market}{ymd[2:]}.md1"
        if market == "bj" and ymd < BJ_FIRST_DAY:
            if cod_name not in names and md1_name not in names:
                continue
        if cod_name not in names or md1_name not in names:
            raise IngestError(
                "package_missing_file", f"{cod_name}/{md1_name} not in archive"
            )
        cod = archive.read(cod_name)
        md1 = archive.read(md1_name)
        if len(cod) % 150 or len(md1) % 512:
            raise IngestError("package_bad_block_size", market)
        if len(cod) // 150 != len(md1) // 512:
            raise IngestError(
                "package_length_mismatch",
                f"{market}: {len(cod) // 150} codes vs {len(md1) // 512} blocks",
            )

        before = len(rows)
        priced = 0
        seen_codes: set[str] = set()
        seen_seqs: set[int] = set()
        for offset in range(0, len(cod), 150):
            record = cod[offset:offset + 150]
            code = record[0:6].rstrip(b"\x00 ").decode("ascii", "replace")
            if not CODE_RE.fullmatch(code):
                raise IngestError("package_bad_code", f"{market} {code!r}")
            seq = struct.unpack("<H", record[32:34])[0]
            if code in seen_codes or seq in seen_seqs:
                raise IngestError("package_duplicate", f"{market} {code} seq={seq}")
            seen_codes.add(code)
            seen_seqs.add(seq)

            block = md1[seq * 512:(seq + 1) * 512]
            if len(block) != 512:
                raise IngestError("package_block_out_of_range", f"{market}{code}")
            prev_close = struct.unpack("<d", block[4:12])[0]
            open_, high, low, close = struct.unpack("<4d", block[12:44])
            amount = struct.unpack("<d", block[72:80])[0]
            if not all(
                math.isfinite(v) for v in (prev_close, open_, high, low, close, amount)
            ):
                raise IngestError("package_non_finite", f"{market}{code}")
            if close <= 0:
                # Indices and TDX-internal board codes carry an all-zero block.
                continue
            priced += 1
            if not is_ordinary_a_share(market, code):
                continue
            volume = struct.unpack("<Q", block[56:64])[0]
            raw_name = record[40:72].split(b"\x00")[0]
            try:
                name = raw_name.decode("gbk").strip()
            except UnicodeDecodeError as exc:
                raise IngestError("package_bad_name_encoding", f"{market}{code}") from exc
            if not name:
                raise IngestError("package_missing_name", f"{market}{code}")

            rows.append(
                Row(
                    symbol=f"{code}.{market.upper()}",
                    day=day,
                    name=name,
                    open_cents=_cents(open_),
                    high_cents=_cents(high),
                    low_cents=_cents(low),
                    close_cents=_cents(close),
                    prev_close_cents=_cents(prev_close),
                    volume_shares=volume,
                    amount_cents=int(round(amount * 100)),
                )
            )
        # The floor is checked on *priced* rows (bonds, funds, indices included)
        # because that is what the measured thresholds describe. Filtering to
        # stocks happens after, so a truncated package still trips the floor.
        if priced < MIN_PRICED[market]:
            raise IngestError(
                "package_below_floor",
                f"{market}: {priced} priced rows < {MIN_PRICED[market]}",
            )
        if len(rows) - before == 0:
            raise IngestError("package_no_stocks", market)
    return rows


def fetch_package(day: str, *, timeout: float = 90.0) -> bytes:
    ymd = day.replace("-", "")
    url = PACKAGE_URL.format(ymd=ymd)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise IngestError(
                "package_not_published",
                f"{day}: not a trading day, or the package is not out yet",
            ) from exc
        raise IngestError("package_http_error", f"{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise IngestError("package_network_error", str(exc)) from exc
    if not content.startswith(b"PK"):
        raise IngestError("package_not_zip", "response is not a zip archive")
    return content


INSERT_SQL = """
INSERT INTO bar(symbol,day,name,open_cents,high_cents,low_cents,close_cents,
                prev_close_cents,volume_shares,amount_cents)
VALUES(?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(symbol,day) DO UPDATE SET
  name=excluded.name, open_cents=excluded.open_cents,
  high_cents=excluded.high_cents, low_cents=excluded.low_cents,
  close_cents=excluded.close_cents, prev_close_cents=excluded.prev_close_cents,
  volume_shares=excluded.volume_shares, amount_cents=excluded.amount_cents
"""


def write_rows(db_path: Path, rows: list[Row]) -> int:
    connection = sqlite3.connect(db_path, timeout=30)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        with connection:
            connection.executemany(
                INSERT_SQL,
                [
                    (
                        r.symbol, r.day, r.name, r.open_cents, r.high_cents,
                        r.low_cents, r.close_cents, r.prev_close_cents,
                        r.volume_shares, r.amount_cents,
                    )
                    for r in rows
                ],
            )
    finally:
        connection.close()
    return len(rows)


def _expand_days(spec: str) -> list[str]:
    if ":" not in spec:
        return [spec]
    start, _, end = spec.partition(":")
    if not start or not end:
        raise IngestError("bad_day_range", spec)
    from datetime import date, timedelta

    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if last < first:
        raise IngestError("bad_day_range", spec)
    out = []
    cursor = first
    while cursor <= last:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to the V2 sqlite database")
    parser.add_argument("--day", help="one trading day, YYYY-MM-DD")
    parser.add_argument("--days", help="inclusive range, YYYY-MM-DD:YYYY-MM-DD")
    parser.add_argument(
        "--dry-run", action="store_true", help="parse but do not write"
    )
    parser.add_argument(
        "--skip-missing",
        action="store_true",
        help="skip days with no package instead of failing (for range catch-up)",
    )
    args = parser.parse_args(argv)

    if not args.day and not args.days:
        parser.error("one of --day or --days is required")

    db_path = Path(args.db)
    if not args.dry_run and not db_path.exists():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 2

    days = [args.day] if args.day else _expand_days(args.days)
    written = 0
    failed: list[tuple[str, str]] = []

    for day in days:
        started = time.monotonic()
        try:
            content = fetch_package(day)
            rows = parse_package(content, day.replace("-", ""))
        except IngestError as exc:
            if args.skip_missing and exc.code == "package_not_published":
                print(f"{day}  skipped ({exc.code})")
                continue
            print(f"{day}  FAILED  {exc}", file=sys.stderr)
            failed.append((day, exc.code))
            continue

        elapsed = time.monotonic() - started
        if args.dry_run:
            print(f"{day}  parsed {len(rows)} rows in {elapsed:.2f}s (dry run)")
        else:
            count = write_rows(db_path, rows)
            written += count
            print(f"{day}  wrote {count} rows in {elapsed:.2f}s")

    if failed:
        print(f"\n{len(failed)} day(s) failed", file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"\ntotal written: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
