#!/usr/bin/env python3
"""Offline tests for the V1 K-line CSV importer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from import_v1_klines import (
    manifest_adjustments,
    read_symbol,
    symbol_of,
    to_cents,
)


class CentParsingTests(unittest.TestCase):
    def test_three_decimals_from_qfq(self):
        # Forward-adjusted prices carry more than two decimals.
        self.assertEqual(to_cents("1550.654"), 155065)

    def test_two_decimals(self):
        self.assertEqual(to_cents("10.40"), 1040)

    def test_integer(self):
        self.assertEqual(to_cents("8"), 800)

    def test_no_float_drift(self):
        self.assertEqual(to_cents("0.29"), 29)
        self.assertEqual(to_cents("1.15"), 115)

    def test_large_amount(self):
        self.assertEqual(to_cents("11765110098.000002"), 1176511009800)

    def test_garbage_rejected(self):
        with self.assertRaises(ValueError):
            to_cents("abc")


class SymbolMappingTests(unittest.TestCase):
    def test_shanghai_main_board(self):
        for code in ("600000", "601398", "603000", "605001"):
            self.assertEqual(symbol_of(code), f"{code}.SH", code)

    def test_star_market(self):
        for code in ("688001", "689009"):
            self.assertEqual(symbol_of(code), f"{code}.SH", code)

    def test_shenzhen(self):
        for code in ("000001", "002415", "300750", "301001"):
            self.assertEqual(symbol_of(code), f"{code}.SZ", code)

    def test_beijing(self):
        for code in ("430047", "830799", "920000"):
            self.assertEqual(symbol_of(code), f"{code}.BJ", code)


def write_csv(path: Path, rows: list[tuple[str, float, float, float, float]]) -> None:
    lines = ["date,open,close,high,low,volume,amount,amplitude"]
    for day, open_, close, high, low in rows:
        lines.append(f"{day},{open_},{close},{high},{low},1000,10000.0,1.0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ReadSymbolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def test_reads_rows_oldest_first(self):
        path = self.root / "600000.csv"
        write_csv(
            path,
            [
                ("2026-11-03", 10.00, 10.10, 10.20, 9.90),
                ("2026-11-04", 10.10, 10.05, 10.15, 10.00),
                ("2026-11-05", 10.05, 10.30, 10.35, 10.02),
            ],
        )
        rows, adjust = read_symbol(path, "600000")
        self.assertEqual(len(rows), 3)
        self.assertEqual([r.day for r in rows], ["2026-11-03", "2026-11-04", "2026-11-05"])
        self.assertEqual(rows[0].symbol, "600000.SH")
        self.assertEqual(rows[2].close_cents, 1030)
        self.assertEqual(adjust, "none")

    def test_prev_close_comes_from_prior_row(self):
        path = self.root / "600000.csv"
        write_csv(
            path,
            [
                ("2026-11-03", 10.00, 10.10, 10.20, 9.90),
                ("2026-11-04", 10.10, 10.05, 10.15, 10.00),
            ],
        )
        rows, _ = read_symbol(path, "600000")
        # First row has no predecessor, so it falls back to its own open.
        self.assertEqual(rows[0].prev_close_cents, 1000)
        # Second row uses the first row's close.
        self.assertEqual(rows[1].prev_close_cents, 1010)

    def test_three_decimal_closes_marked_qfq(self):
        path = self.root / "600519.csv"
        write_csv(
            path,
            [
                ("2023-01-03", 1551.844, 1550.654, 1559.074, 1526.654),
                ("2023-01-04", 1550.0, 1560.0, 1565.0, 1548.0),
            ],
        )
        _, adjust = read_symbol(path, "600519")
        self.assertEqual(adjust, "qfq")

    def test_zero_close_rows_skipped(self):
        path = self.root / "600000.csv"
        write_csv(
            path,
            [
                ("2026-11-03", 10.00, 10.10, 10.20, 9.90),
                ("2026-11-04", 0.0, 0.0, 0.0, 0.0),
                ("2026-11-05", 10.05, 10.30, 10.35, 10.02),
            ],
        )
        rows, _ = read_symbol(path, "600000")
        self.assertEqual([r.day for r in rows], ["2026-11-03", "2026-11-05"])

    def test_malformed_rows_skipped(self):
        path = self.root / "600000.csv"
        path.write_text(
            "date,open,close,high,low,volume,amount,amplitude\n"
            "bad-date,1,2,3,4,5,6,7\n"
            "2026-11-03,notanumber,10.10,10.20,9.90,1000,10000,1.0\n"
            "2026-11-04,10.10,10.05,10.15,10.00,1000,10000,1.0\n",
            encoding="utf-8",
        )
        rows, _ = read_symbol(path, "600000")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].day, "2026-11-04")

    def test_source_tag_is_v1_qfq(self):
        # Must never be confused with `tdx` rows, which are unadjusted.
        path = self.root / "600000.csv"
        write_csv(path, [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        rows, _ = read_symbol(path, "600000")
        self.assertEqual(rows[0].source, "v1-qfq")


class ManifestTests(unittest.TestCase):
    def test_reads_per_symbol_adjustment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "kline_manifest.json").write_text(
                json.dumps(
                    {
                        "stocks": {
                            "600000": {"adjustment": "qfq"},
                            "600036": {"adjustment": "none"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = manifest_adjustments(root)
            self.assertEqual(result["600000"], "qfq")
            self.assertEqual(result["600036"], "none")

    def test_missing_manifest_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(manifest_adjustments(Path(tmp)), {})


if __name__ == "__main__":
    unittest.main()
