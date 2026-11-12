#!/usr/bin/env python3
"""Offline tests for whole-market ingest.

All package bytes are synthesised here so the suite never touches the network.
"""

from __future__ import annotations

from contextlib import closing
import io
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
import zipfile

from ingest_market import (
    IngestError,
    _expand_days,
    is_ordinary_a_share,
    parse_package,
    sync_schema,
)


def build_package(
    ymd: str,
    markets: dict[str, list[tuple[str, str, float, float]] | list[tuple[str, str, float, float, float]]],
) -> bytes:
    """Build a minimal but structurally valid daytime package.

    `markets` maps market -> list of (code, name, close, prev_close). A close
    of 0 produces an all-zero block, the way TDX encodes indices. A fifth
    tuple value overrides the volume for a positive close.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for market, entries in markets.items():
            cod = bytearray()
            md1 = bytearray()
            for seq, record_values in enumerate(entries):
                code, name, close, prev_close = record_values[:4]
                volume = record_values[4] if len(record_values) == 5 else 1000
                record = bytearray(150)
                record[0:6] = code.encode("ascii")
                record[32:34] = struct.pack("<H", seq)
                encoded = name.encode("gbk")[:31]
                # Butt against the NUL: the parser splits on the first NUL byte,
                # so a name filling the field with no terminator reads as truncated.
                record[40:40 + len(encoded)] = encoded
                cod.extend(record)

                block = bytearray(512)
                block[4:12] = struct.pack("<d", prev_close)
                # A zero volume is the fixture for a carried quote: open, high
                # and low stay zero while the positive close remains present.
                quote = 0.0 if volume == 0 else close
                block[12:20] = struct.pack("<d", quote)  # open
                block[20:28] = struct.pack("<d", quote)  # high
                block[28:36] = struct.pack("<d", quote)  # low
                block[36:44] = struct.pack("<d", close)  # close
                block[56:64] = struct.pack("<Q", volume)
                block[72:80] = struct.pack("<d", 12345.67)  # amount
                md1.extend(block)
            archive.writestr(f"{market}{ymd[2:]}.cod", bytes(cod))
            archive.writestr(f"{market}{ymd[2:]}.md1", bytes(md1))
    return buffer.getvalue()


def big_market(market: str, count: int) -> list[tuple[str, str, float, float]]:
    """Enough priced rows to clear the per-market floor.

    Codes must stay six digits wide, so the index is zero-padded across the
    whole suffix rather than a fixed three digits.
    """
    # Codes are six digits wide, generated as base+i so they never overflow the
    # 6-byte field in the .cod record (assigning a longer string into a fixed
    # slice silently grows the bytearray and corrupts every following record).
    if market == "sh":
        base = 600_000
    elif market == "sz":
        base = 0
    else:
        base = 430_000
    return [(f"{base + i:06d}", f"S{i}", 10.00, 10.10) for i in range(count)]


class StockFilterTests(unittest.TestCase):
    def test_sh_main_board(self):
        for code in ("600000", "601398", "603000", "605001"):
            self.assertTrue(is_ordinary_a_share("sh", code), code)

    def test_star_market(self):
        for code in ("688001", "689009"):
            self.assertTrue(is_ordinary_a_share("sh", code), code)

    def test_sz_boards(self):
        for code in ("000001", "001979", "002415", "003816", "300750", "301001"):
            self.assertTrue(is_ordinary_a_share("sz", code), code)

    def test_beijing(self):
        for code in ("430047", "830799", "871981", "920000"):
            self.assertTrue(is_ordinary_a_share("bj", code), code)

    def test_rejects_non_stocks(self):
        # Bonds, funds, TDX-internal board codes, exchange indices.
        for market, code in (
            ("sh", "019547"),   # treasury bond
            ("sh", "510300"),   # ETF
            ("sh", "880005"),   # TDX board index
            ("sh", "000001"),   # SSE composite index
            ("sz", "399001"),   # SZSE component index
            ("sz", "159915"),   # SZSE ETF
        ):
            self.assertFalse(is_ordinary_a_share(market, code), f"{market}{code}")

    def test_unknown_market_rejected(self):
        self.assertFalse(is_ordinary_a_share("hk", "00700"))


class ParseTests(unittest.TestCase):
    def test_parses_stock_rows_only(self):
        content = build_package(
            "20261110",
            {
                "sh": big_market("sh", 10_100)
                + [
                    ("000001", "INDEX", 3000.0, 2990.0),   # index, not a stock
                    ("510300", "ETF", 4.0, 4.0),          # fund, not a stock
                ],
                "sz": big_market("sz", 3_100),
                "bj": big_market("bj", 60),
            },
        )
        rows = parse_package(content, "20261110")
        markets = {r.symbol.split(".")[1] for r in rows}
        self.assertEqual(markets, {"SH", "SZ", "BJ"})
        symbols = {r.symbol for r in rows}
        self.assertNotIn("000001.SH", symbols)   # index filtered out
        self.assertNotIn("510300.SH", symbols)   # ETF filtered out
        self.assertIn("600000.SH", symbols)
        self.assertIn("000000.SZ", symbols)
        self.assertIn("430000.BJ", symbols)
        self.assertTrue(all(r.day == "2026-11-10" for r in rows))

    def test_prices_become_cents(self):
        content = build_package(
            "20261110",
            {
                "sh": big_market("sh", 10_100),
                "sz": big_market("sz", 3_100),
                "bj": big_market("bj", 60),
            },
        )
        rows = parse_package(content, "20261110")
        row = next(r for r in rows if r.symbol == "600000.SH")
        self.assertEqual(row.close_cents, 1000)
        self.assertEqual(row.prev_close_cents, 1010)
        self.assertEqual(row.amount_cents, 1_234_567)
        self.assertEqual(row.change_bps(), (1000 - 1010) * 10_000 // 1010)

    def test_zero_close_block_skipped(self):
        entries = big_market("sh", 10_100)
        # SH floor is 10_000 rows, so 10_100 generated codes span
        # 600000..610099. Stay above that range.
        entries.append(("688999", "HALTED", 0.0, 0.0))
        content = build_package(
            "20261110",
            {"sh": entries, "sz": big_market("sz", 3_100), "bj": big_market("bj", 60)},
        )
        rows = parse_package(content, "20261110")
        self.assertNotIn("688999.SH", {r.symbol for r in rows})

    def test_zero_close_with_official_reference_is_valuation_only(self):
        entries = big_market("sh", 10_100)
        entries.append(("688999", "HALTED", 0.0, 10.25))
        content = build_package(
            "20261110",
            {"sh": entries, "sz": big_market("sz", 3_100), "bj": big_market("bj", 60)},
        )
        rows = parse_package(content, "20261110")
        halted = next(r for r in rows if r.symbol == "688999.SH")
        self.assertFalse(halted.tradable)
        self.assertEqual(halted.close_cents, 1025)
        self.assertEqual(halted.prev_close_cents, 1025)
        self.assertEqual(halted.volume_shares, 0)

    def test_zero_close_without_reference_is_dropped(self):
        entries = big_market("sh", 10_100)
        entries.append(("688999", "HALTED", 0.0, 0.0))
        content = build_package(
            "20261110",
            {"sh": entries, "sz": big_market("sz", 3_100), "bj": big_market("bj", 60)},
        )
        rows = parse_package(content, "20261110")
        self.assertNotIn("688999.SH", {r.symbol for r in rows})

    def test_positive_close_with_zero_volume_and_zero_ohlc_is_valuation_only(self):
        entries = big_market("sh", 10_100)
        entries.append(("688999", "STALE", 10.25, 10.20, 0))
        content = build_package(
            "20261110",
            {"sh": entries, "sz": big_market("sz", 3_100), "bj": big_market("bj", 60)},
        )
        rows = parse_package(content, "20261110")
        stale = next(r for r in rows if r.symbol == "688999.SH")
        self.assertFalse(stale.tradable)
        self.assertEqual(stale.close_cents, 1020)
        self.assertEqual(stale.volume_shares, 0)

    def test_below_floor_rejected(self):
        content = build_package(
            "20261110",
            {"sh": big_market("sh", 10), "sz": big_market("sz", 10), "bj": big_market("bj", 60)},
        )
        with self.assertRaises(IngestError) as caught:
            parse_package(content, "20261110")
        self.assertEqual(caught.exception.code, "package_below_floor")

    def test_not_a_zip_rejected(self):
        with self.assertRaises(IngestError) as caught:
            parse_package(b"<html>error</html>", "20261110")
        self.assertEqual(caught.exception.code, "package_not_zip")

    def test_missing_file_rejected(self):
        # Only the code table, no quote block.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("sh261110.cod", bytes(150))
        with self.assertRaises(IngestError) as caught:
            parse_package(buffer.getvalue(), "20261110")
        self.assertEqual(caught.exception.code, "package_missing_file")

    def test_before_bj_first_day_tolerates_absent_bj(self):
        content = build_package(
            "20211210",
            {"sh": big_market("sh", 10_100), "sz": big_market("sz", 3_100)},
        )
        rows = parse_package(content, "20211210")
        self.assertEqual({r.symbol.split(".")[1] for r in rows}, {"SH", "SZ"})


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.executescript("""
                CREATE TABLE bar(
                    symbol TEXT, day TEXT, name TEXT,
                    open_cents INTEGER, high_cents INTEGER, low_cents INTEGER,
                    close_cents INTEGER, prev_close_cents INTEGER,
                    volume_shares INTEGER, amount_cents INTEGER,
                    adjust TEXT, source TEXT, PRIMARY KEY(symbol, day));
                INSERT INTO bar VALUES
                    ('000007.SZ','2026-11-10','halt',0,0,0,480,474,0,0,'none','tdx'),
                    ('000008.SZ','2026-11-10','no-reference',0,0,0,480,0,0,0,'none','tdx'),
                    ('600000.SH','2026-11-10','traded',1000,1020,990,1020,1010,100,102000,'none','tdx'),
                    ('600001.SH','2026-11-10','adjusted',0,0,0,480,474,0,0,'qfq','v1-qfq');
            """)

    def rows(self):
        connection = sqlite3.connect(self.db)
        try:
            return dict((symbol, (price, tradable)) for symbol, price, tradable
                        in connection.execute("SELECT symbol,close_cents,tradable FROM bar"))
        finally:
            connection.close()

    def test_legacy_halt_uses_reference_without_allowing_a_trade(self):
        sync_schema(self.db)
        self.assertEqual(self.rows()["000007.SZ"], (474, 0))
        self.assertEqual(self.rows()["600000.SH"], (1020, 1))
        self.assertEqual(self.rows()["600001.SH"], (480, 1))

    def test_legacy_zero_quote_without_reference_stays_unpriced(self):
        sync_schema(self.db)
        self.assertEqual(self.rows()["000008.SZ"], (0, 0))

    def test_reinitialization_preserves_existing_explicit_tradability(self):
        sync_schema(self.db)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE bar SET tradable=0 WHERE symbol='600000.SH'")
        before = self.rows()
        sync_schema(self.db)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.rows()["600000.SH"], (1020, 0))


class DayRangeTests(unittest.TestCase):
    def test_single_day(self):
        self.assertEqual(_expand_days("2026-11-10"), ["2026-11-10"])

    def test_inclusive_range_across_weekend(self):
        days = _expand_days("2026-11-06:2026-11-10")
        self.assertEqual(
            days,
            ["2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"],
        )

    def test_reversed_range_rejected(self):
        with self.assertRaises(IngestError):
            _expand_days("2026-11-10:2026-11-01")

    def test_malformed_range_rejected(self):
        with self.assertRaises(IngestError):
            _expand_days("2026-11-10:")


if __name__ == "__main__":
    unittest.main()
