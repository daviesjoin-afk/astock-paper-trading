#!/usr/bin/env python3
"""Offline tests for whole-market ingest.

All package bytes are synthesised here so the suite never touches the network.
"""

from __future__ import annotations

import io
import struct
import unittest
import zipfile

from ingest_market import (
    IngestError,
    _expand_days,
    is_ordinary_a_share,
    parse_package,
)


def build_package(
    ymd: str,
    markets: dict[str, list[tuple[str, str, float, float]]],
) -> bytes:
    """Build a minimal but structurally valid daytime package.

    `markets` maps market -> list of (code, name, close, prev_close). A close
    of 0 produces an all-zero block, the way TDX encodes indices.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for market, entries in markets.items():
            cod = bytearray()
            md1 = bytearray()
            for seq, (code, name, close, prev_close) in enumerate(entries):
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
                block[12:20] = struct.pack("<d", close)  # open
                block[20:28] = struct.pack("<d", close)  # high
                block[28:36] = struct.pack("<d", close)  # low
                block[36:44] = struct.pack("<d", close)  # close
                block[56:64] = struct.pack("<Q", 1000)  # volume
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
