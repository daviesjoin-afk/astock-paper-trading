"""Tests for the offline price-set builder."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import make_price_set as builder

HEADER = "symbol,day,price,source,observed_at\n"
ROWS = (
    "600000.SH,2026-10-30,12.34,vendor-close,2026-10-30T15:00:00+08:00\n"
    "600000.SH,2026-11-02,12.19,vendor-close,2026-11-02T15:00:00+08:00\n"
)


def write_csv(directory: Path, body: str) -> Path:
    path = directory / "prices.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    return path


class PriceSetBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_main(self, *extra: str) -> int:
        return builder.main(
            [
                "--input", str(self.csv),
                "--out", str(self.dir / "set.json"),
                "--report", str(self.dir / "report.json"),
                "--set-id", "cn-close-2026-11",
                "--pack", "cn-offline-fixture-pack-1",
                "--coverage-start", "2026-10-30",
                "--coverage-end", "2026-11-02",
                "--symbols", "600000.SH",
                *extra,
            ]
        )

    def test_builds_cents_and_digest(self) -> None:
        self.csv = write_csv(self.dir, ROWS)
        self.assertEqual(self.run_main("--days", "2026-10-30,2026-11-02"), 0)
        artifact = json.loads((self.dir / "set.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["schema_version"], 1)
        self.assertEqual(artifact["trust"], "reviewed")
        self.assertEqual([entry["price_cents"] for entry in artifact["entries"]], [1234, 1219])
        self.assertEqual(len(artifact["source_digest"]), 64)
        report = json.loads((self.dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["entries"], 2)

    def test_rejects_three_decimal_prices(self) -> None:
        self.csv = write_csv(self.dir, "600000.SH,2026-10-30,12.345,x,2026-10-30T15:00:00+08:00\n")
        self.assertEqual(self.run_main(), 2)
        self.assertFalse((self.dir / "set.json").exists())

    def test_rejects_duplicate_rows(self) -> None:
        self.csv = write_csv(self.dir, ROWS + ROWS.splitlines()[0] + "\n")
        self.assertEqual(self.run_main(), 2)

    def test_rejects_undeclared_symbol(self) -> None:
        self.csv = write_csv(self.dir, "000001.SZ,2026-10-30,9.50,x,2026-10-30T15:00:00+08:00\n")
        self.assertEqual(self.run_main(), 2)

    def test_reports_gaps_and_fails_without_allow(self) -> None:
        self.csv = write_csv(self.dir, ROWS)
        self.assertEqual(self.run_main("--days", "2026-10-30,2026-11-02,2026-11-03"), 3)
        self.assertFalse((self.dir / "set.json").exists())
        report = json.loads((self.dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["missing"], [{"symbol": "600000.SH", "day": "2026-11-03"}])

    def test_allow_gaps_writes_artifact(self) -> None:
        self.csv = write_csv(self.dir, ROWS)
        self.assertEqual(
            self.run_main("--days", "2026-11-03", "--allow-gaps"), 0
        )
        self.assertTrue((self.dir / "set.json").exists())

    def test_entry_outside_coverage_is_refused(self) -> None:
        self.csv = write_csv(self.dir, ROWS)
        self.assertEqual(
            builder.main(
                [
                    "--input", str(self.csv),
                    "--out", str(self.dir / "set.json"),
                    "--set-id", "s",
                    "--pack", "cn-offline-fixture-pack-1",
                    "--coverage-start", "2026-11-01",
                    "--coverage-end", "2026-11-02",
                    "--symbols", "600000.SH",
                ]
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
