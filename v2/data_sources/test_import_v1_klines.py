#!/usr/bin/env python3
"""Offline tests for the V1 K-line CSV importer."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from import_v1_klines import (
    declared_adjustment,
    main,
    manifest_adjustments,
    optional_cents,
    optional_shares,
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


class OptionalFieldTests(unittest.TestCase):
    """A missing measurement must never become a zero measurement."""

    def test_missing_volume_is_none_not_zero(self):
        for value in (None, "", "   ", "notanumber"):
            self.assertIsNone(optional_shares(value), repr(value))

    def test_missing_amount_is_none_not_zero(self):
        for value in (None, "", "   ", "notanumber"):
            self.assertIsNone(optional_cents(value), repr(value))

    def test_present_values_are_kept(self):
        self.assertEqual(optional_shares("1000"), 1000)
        self.assertEqual(optional_cents("10000.5"), 1000050)

    def test_a_real_zero_is_still_a_zero(self):
        # "0 shares traded" is a fact the source asserted; keep it.
        self.assertEqual(optional_shares("0"), 0)
        self.assertEqual(optional_cents("0"), 0)


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


HEADER = "date,open,close,high,low,volume,amount,amplitude"


def write_csv(path: Path, rows: list[tuple[str, float, float, float, float]]) -> None:
    lines = [HEADER]
    for day, open_, close, high, low in rows:
        lines.append(f"{day},{open_},{close},{high},{low},1000,10000.0,1.0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


RUST_SCHEMA = """
CREATE TABLE bar(
  symbol TEXT NOT NULL, day TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
  open_cents INTEGER NOT NULL, high_cents INTEGER NOT NULL,
  low_cents INTEGER NOT NULL, close_cents INTEGER NOT NULL,
  prev_close_cents INTEGER NOT NULL, volume_shares INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL, adjust TEXT NOT NULL DEFAULT 'none',
  source TEXT NOT NULL DEFAULT 'tdx', tradable INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(symbol, day));
"""

NULLABLE_SCHEMA = """
CREATE TABLE bar(
  symbol TEXT NOT NULL, day TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
  open_cents INTEGER NOT NULL, high_cents INTEGER NOT NULL,
  low_cents INTEGER NOT NULL, close_cents INTEGER NOT NULL,
  prev_close_cents INTEGER NOT NULL, volume_shares INTEGER,
  amount_cents INTEGER, adjust TEXT NOT NULL DEFAULT 'none',
  source TEXT NOT NULL DEFAULT 'tdx', tradable INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(symbol, day));
"""


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
        rows = read_symbol(path, "600000")
        self.assertEqual(len(rows), 3)
        self.assertEqual([r.day for r in rows], ["2026-11-03", "2026-11-04", "2026-11-05"])
        self.assertEqual(rows[0].symbol, "600000.SH")
        self.assertEqual(rows[2].close_cents, 1030)

    def test_prev_close_comes_from_prior_row(self):
        path = self.root / "600000.csv"
        write_csv(
            path,
            [
                ("2026-11-03", 10.00, 10.10, 10.20, 9.90),
                ("2026-11-04", 10.10, 10.05, 10.15, 10.00),
            ],
        )
        rows = read_symbol(path, "600000")
        # First row has no predecessor, so it falls back to its own open.
        self.assertEqual(rows[0].prev_close_cents, 1000)
        # Second row uses the first row's close.
        self.assertEqual(rows[1].prev_close_cents, 1010)

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
        rows = read_symbol(path, "600000")
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
        rows = read_symbol(path, "600000")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].day, "2026-11-04")

    def test_source_tag_is_v1_qfq(self):
        # Must never be confused with `tdx` rows, which are unadjusted.
        path = self.root / "600000.csv"
        write_csv(path, [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        rows = read_symbol(path, "600000")
        self.assertEqual(rows[0].source, "v1-qfq")

    def test_reader_does_not_pretend_to_know_the_adjustment(self):
        # The CSV alone cannot prove an adjustment kind; only the manifest can.
        path = self.root / "600000.csv"
        write_csv(path, [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        rows = read_symbol(path, "600000")
        self.assertFalse(hasattr(rows[0], "adjust"),
                         "read_symbol must not carry an inferred adjustment kind")

    def test_missing_volume_and_amount_stay_unknown(self):
        path = self.root / "600000.csv"
        path.write_text(
            HEADER + "\n"
            "2026-11-03,10.00,10.10,10.20,9.90,,,\n"
            "2026-11-04,10.10,10.05,10.15,10.00,1000,10000.0,1.0\n",
            encoding="utf-8",
        )
        rows = read_symbol(path, "600000")
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0].volume_shares, "a blank volume is not zero shares")
        self.assertIsNone(rows[0].amount_cents, "a blank amount is not zero turnover")
        self.assertFalse(rows[0].tradable,
                         "a row with no measured volume must not claim to be tradable")
        # The prices are still real, so the row is kept.
        self.assertEqual(rows[0].close_cents, 1010)
        self.assertEqual(rows[1].volume_shares, 1000)
        self.assertTrue(rows[1].tradable)


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

    def test_only_an_explicit_declaration_counts(self):
        manifest = {"600000": "qfq", "600036": "none", "600519": "unknown"}
        self.assertEqual(declared_adjustment(manifest, "600000"), "qfq")
        self.assertEqual(declared_adjustment(manifest, "600036"), "none")
        self.assertIsNone(declared_adjustment(manifest, "600519"),
                          "an explicit 'unknown' is not a declaration")
        self.assertIsNone(declared_adjustment(manifest, "000001"),
                          "an absent entry is not a declaration")
        self.assertIsNone(declared_adjustment({}, "600000"))

    def test_garbage_declaration_is_not_a_declaration(self):
        self.assertIsNone(declared_adjustment({"600000": None}, "600000"))
        self.assertIsNone(declared_adjustment({"600000": ""}, "600000"))
        self.assertIsNone(declared_adjustment({"600000": "probably"}, "600000"))


class ImportRun:
    """Fixture: a klines/ tree plus a target bar table."""

    def __init__(self, case: unittest.TestCase, *, schema: str = RUST_SCHEMA):
        self._dir = tempfile.TemporaryDirectory()
        case.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        (self.root / "klines").mkdir()
        self.db = self.root / "bars.sqlite3"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(schema)
            conn.commit()

    def add_symbol(self, code: str, rows: list[tuple] | None = None,
                   raw: str | None = None) -> None:
        path = self.root / "klines" / f"{code}.csv"
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
            return
        write_csv(path, rows)

    def declare(self, **adjustments: str) -> None:
        (self.root / "kline_manifest.json").write_text(
            json.dumps({"stocks": {code: {"adjustment": kind}
                                   for code, kind in adjustments.items()}}),
            encoding="utf-8",
        )

    def run(self, *extra: str) -> int:
        return main(["--dir", str(self.root), "--db", str(self.db), *extra])

    def rows(self) -> list[tuple]:
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(
                "SELECT symbol, day, close_cents, volume_shares, amount_cents, "
                "adjust, source, tradable FROM bar ORDER BY symbol, day"
            ).fetchall()


class RefuseUndeclaredAdjustmentTests(unittest.TestCase):
    """No declaration -> refuse the symbol. Never infer 'none'."""

    def test_two_decimal_closes_without_declaration_are_refused(self):
        run = ImportRun(self)
        # A fully 2-decimal series: the old heuristic called this "none".
        run.add_symbol("600000", [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        run.declare()  # manifest exists but says nothing about 600000
        code = run.run()
        self.assertEqual(code, 3, "an undeclared adjustment must fail the run")
        self.assertEqual(run.rows(), [],
                         "nothing may be written under a guessed adjustment kind")

    def test_no_manifest_at_all_is_refused(self):
        run = ImportRun(self)
        run.add_symbol("600000", [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        self.assertEqual(run.run(), 3)
        self.assertEqual(run.rows(), [])

    def test_explicit_unknown_is_refused(self):
        run = ImportRun(self)
        run.add_symbol("600519", [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        run.declare(**{"600519": "unknown"})
        self.assertEqual(run.run(), 3)
        self.assertEqual(run.rows(), [])

    def test_one_undeclared_symbol_does_not_block_the_declared_one(self):
        run = ImportRun(self)
        run.add_symbol("600000", [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        run.add_symbol("000001", [("2026-11-03", 9.00, 9.10, 9.20, 8.90)])
        run.declare(**{"600000": "qfq"})
        self.assertEqual(run.run(), 3, "the batch is incomplete, so the exit code says so")
        stored = run.rows()
        self.assertEqual([row[0] for row in stored], ["600000.SH"],
                         "the declared symbol is imported; the undeclared one is not")
        self.assertEqual(stored[0][5], "qfq")

    def test_declared_unadjusted_is_imported_as_none(self):
        run = ImportRun(self)
        run.add_symbol("600036", [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        run.declare(**{"600036": "none"})
        self.assertEqual(run.run(), 0)
        self.assertEqual(run.rows()[0][5], "none")

    def test_dry_run_reports_the_refusal_without_writing(self):
        run = ImportRun(self)
        run.add_symbol("600000", [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        self.assertEqual(run.run("--dry-run"), 3)
        self.assertEqual(run.rows(), [])


class UnknownVolumeTests(unittest.TestCase):
    def test_not_null_target_refuses_the_row_instead_of_writing_zero(self):
        run = ImportRun(self, schema=RUST_SCHEMA)
        run.add_symbol(
            "600000",
            raw=HEADER + "\n"
                       "2026-11-03,10.00,10.10,10.20,9.90,,,\n"
                       "2026-11-04,10.10,10.05,10.15,10.00,1000,10000.0,1.0\n",
        )
        run.declare(**{"600000": "qfq"})
        self.assertEqual(run.run(), 3, "a refused row makes the import incomplete")
        stored = run.rows()
        self.assertEqual([row[1] for row in stored], ["2026-11-04"],
                         "the row with unknown volume is absent, not zero-filled")
        self.assertEqual(stored[0][3], 1000)

    def test_nullable_target_stores_null_and_marks_not_tradable(self):
        run = ImportRun(self, schema=NULLABLE_SCHEMA)
        run.add_symbol(
            "600000",
            raw=HEADER + "\n"
                       "2026-11-03,10.00,10.10,10.20,9.90,,,\n",
        )
        run.declare(**{"600000": "qfq"})
        self.assertEqual(run.run(), 0, "storing NULL loses nothing, so it is complete")
        stored = run.rows()
        self.assertEqual(len(stored), 1)
        self.assertIsNone(stored[0][3], "volume must stay NULL")
        self.assertIsNone(stored[0][4], "amount must stay NULL")
        self.assertEqual(stored[0][7], 0, "tradable must be 0 when volume is unknown")
        self.assertEqual(stored[0][2], 1010, "the price row itself is still useful")

    def test_partial_unknown_is_enough_to_drop_tradable(self):
        run = ImportRun(self, schema=NULLABLE_SCHEMA)
        run.add_symbol(
            "600000",
            raw=HEADER + "\n"
                       "2026-11-03,10.00,10.10,10.20,9.90,1000,,\n",
        )
        run.declare(**{"600000": "qfq"})
        self.assertEqual(run.run(), 0)
        stored = run.rows()
        self.assertEqual(stored[0][3], 1000)
        self.assertIsNone(stored[0][4])
        self.assertEqual(stored[0][7], 0)

    def test_a_symbol_whose_rows_are_all_refused_counts_as_incomplete(self):
        run = ImportRun(self, schema=RUST_SCHEMA)
        run.add_symbol("600000", raw=HEADER + "\n2026-11-03,10,10.1,10.2,9.9,,,\n")
        run.declare(**{"600000": "qfq"})
        self.assertEqual(run.run(), 3)
        self.assertEqual(run.rows(), [])


class TdxRowProtectionTests(unittest.TestCase):
    """The archive must never overwrite an unadjusted TDX row."""

    def seed_tdx(self, run: ImportRun, symbol: str, day: str) -> None:
        with closing(sqlite3.connect(run.db)) as conn:
            conn.execute(
                "INSERT INTO bar(symbol,day,name,open_cents,high_cents,low_cents,"
                "close_cents,prev_close_cents,volume_shares,amount_cents,adjust,"
                "source,tradable) VALUES(?,?,'tdx-close',980,1010,975,1000,990,"
                "5000,500000,'none','tdx',1)",
                (symbol, day),
            )
            conn.commit()

    def test_overlapping_tdx_row_is_not_overwritten(self):
        run = ImportRun(self)
        self.seed_tdx(run, "600000.SH", "2026-11-03")
        run.add_symbol(
            "600000",
            [
                ("2026-11-03", 10.00, 10.10, 10.20, 9.90),
                ("2026-11-04", 10.10, 10.05, 10.15, 10.00),
            ],
        )
        run.declare(**{"600000": "qfq"})
        self.assertEqual(run.run(), 0)
        stored = {row[1]: row for row in run.rows()}
        kept = stored["2026-11-03"]
        self.assertEqual(kept[5], "none", "the TDX row must keep adjust='none'")
        self.assertEqual(kept[6], "tdx", "the TDX row must keep source='tdx'")
        self.assertEqual(kept[2], 1000, "the TDX settlement close must survive")
        # The non-overlapping archive row is still imported.
        self.assertEqual(stored["2026-11-04"][5], "qfq")
        self.assertEqual(stored["2026-11-04"][6], "v1-qfq")

    def test_skipped_conflict_is_reported(self):
        run = ImportRun(self)
        self.seed_tdx(run, "600000.SH", "2026-11-03")
        run.add_symbol("600000", [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        run.declare(**{"600000": "qfq"})
        output = self.capture(run)
        self.assertIn("protected", output)
        self.assertIn("600000.SH", output)
        self.assertIn("1 row", output, f"the report must state how many rows were "
                                       f"skipped, got: {output!r}")

    def test_dry_run_reports_the_conflict_without_writing(self):
        run = ImportRun(self)
        self.seed_tdx(run, "600000.SH", "2026-11-03")
        run.add_symbol("600000", [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        run.declare(**{"600000": "qfq"})
        output = self.capture(run, "--dry-run")
        self.assertIn("protected", output)
        stored = {row[1]: row for row in run.rows()}
        self.assertEqual(stored["2026-11-03"][6], "tdx")

    def test_an_archive_row_may_replace_another_archive_row(self):
        # Re-running the import over its own output is still idempotent.
        run = ImportRun(self)
        run.add_symbol("600000", [("2026-11-03", 10.00, 10.10, 10.20, 9.90)])
        run.declare(**{"600000": "qfq"})
        self.assertEqual(run.run(), 0)
        self.assertEqual(run.run(), 0)
        stored = run.rows()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0][6], "v1-qfq")

    @staticmethod
    def capture(run: ImportRun, *extra: str) -> str:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run.run(*extra)
        return f"exit={code}\n" + buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
