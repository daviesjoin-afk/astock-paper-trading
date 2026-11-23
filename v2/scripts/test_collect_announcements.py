#!/usr/bin/env python3
"""Behaviour tests for the announcement collector's resume rule.

The collector's value depends on one property: a symbol is skipped only when the
requested window is already covered. Skipping on "this symbol succeeded once"
silently drops every announcement added by a wider ``--end`` -- the run then
reports success for a range it never fetched. These tests drive the real CLI
entry point against a fake CNINFO client (no network) and assert what was
actually requested, stored and recorded.
"""
from __future__ import annotations

import io
from contextlib import closing, redirect_stdout
import sqlite3
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect_announcements as collector  # noqa: E402

SYMBOL = "600519.SH"

# Announcements the fake source knows about, all inside 2026-01-01..2026-01-31.
CALENDAR = {
    "2026-01-05": "ann-d1",
    "2026-01-12": "ann-d2",
    "2026-01-20": "ann-d3",
    "2026-01-29": "ann-d4",
}


class FakeClient:
    """Stands in for PublicDataClient; records every window it is asked for."""

    calls: list[tuple[str, str, str]] = []
    fail_windows: set[tuple[str, str]] = set()

    def __init__(self, *args, **kwargs) -> None:
        pass

    def announcements(self, symbol: str, start: str, end: str, **kwargs) -> dict:
        FakeClient.calls.append((symbol, start, end))
        if (start, end) in FakeClient.fail_windows:
            raise collector.PublicDataError("provider_unavailable")
        rows = [
            {"announcement_date": day, "announcement_id": ann_id,
             "category_raw": "cat", "title": f"t-{ann_id}",
             "source": "cninfo", "source_url": "https://example.com/x"}
            for day, ann_id in sorted(CALENDAR.items()) if start <= day <= end
        ]
        return {"status": "ok", "provider": "cninfo", "symbol": symbol,
                "data": {"rows": rows}}


class CollectorResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "ann.sqlite3"
        self.symbols = self.dir / "symbols.txt"
        self.symbols.write_text(SYMBOL + "\n", encoding="utf-8")
        self._real_client = collector.PublicDataClient
        collector.PublicDataClient = FakeClient
        self.addCleanup(setattr, collector, "PublicDataClient", self._real_client)
        FakeClient.calls = []
        FakeClient.fail_windows = set()
        # --sleep is politeness towards a real server; the tests run offline.
        patcher = mock.patch.object(collector, "time",
                                    SimpleNamespace(sleep=lambda _seconds: None))
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers ---------------------------------------------------------
    def run_cli(self, start: str, end: str) -> int:
        out = io.StringIO()
        with redirect_stdout(out):
            code = collector.main(["--symbols", str(self.symbols), "--out", str(self.db),
                                   "--start", start, "--end", end, "--retries", "1",
                                   "--sleep", "0"])
        self.last_output = out.getvalue()
        return code

    def stored_ids(self) -> list[str]:
        with closing(sqlite3.connect(self.db)) as conn:
            return [row[0] for row in conn.execute(
                "SELECT announcement_id FROM announcement ORDER BY announcement_id")]

    def coverage(self) -> tuple:
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(
                "SELECT status, covered_start, covered_end, covered_ranges"
                " FROM collect_log WHERE symbol = ?", (SYMBOL,)).fetchone()

    # -- the defect the audit found --------------------------------------
    def test_widening_the_end_recollects_the_symbol(self):
        self.assertEqual(self.run_cli("2026-01-01", "2026-01-12"), 0)
        self.assertEqual(self.stored_ids(), ["ann-d1", "ann-d2"])
        self.assertEqual(FakeClient.calls, [(SYMBOL, "2026-01-01", "2026-01-12")])

        # Same symbol, wider window: the newly reachable days must be fetched,
        # not skipped because the symbol once reported "ok".
        self.assertEqual(self.run_cli("2026-01-01", "2026-01-29"), 0)
        self.assertEqual(FakeClient.calls,
                         [(SYMBOL, "2026-01-01", "2026-01-12"),
                          (SYMBOL, "2026-01-01", "2026-01-29")],
                         "the widened window has to be requested again")
        self.assertEqual(self.stored_ids(), ["ann-d1", "ann-d2", "ann-d3", "ann-d4"])
        self.assertEqual(self.coverage()[1:3], ("2026-01-01", "2026-01-29"),
                         "coverage is the union of what was fetched")

    def test_a_requested_subset_of_covered_days_is_skipped(self):
        self.assertEqual(self.run_cli("2026-01-01", "2026-01-29"), 0)
        self.assertEqual(len(FakeClient.calls), 1)
        rows_before = self.stored_ids()

        self.assertEqual(self.run_cli("2026-01-05", "2026-01-12"), 0)
        self.assertEqual(len(FakeClient.calls), 1,
                         "a window fully inside the covered range needs no request")
        self.assertIn("covered=1", self.last_output)
        self.assertEqual(self.stored_ids(), rows_before)

    def test_a_later_start_that_sticks_out_is_recollected(self):
        self.assertEqual(self.run_cli("2026-01-01", "2026-01-20"), 0)
        FakeClient.calls = []
        self.assertEqual(self.run_cli("2026-01-15", "2026-01-29"), 0)
        self.assertEqual(FakeClient.calls, [(SYMBOL, "2026-01-15", "2026-01-29")])

    def test_a_gap_between_two_windows_is_never_claimed_as_covered(self):
        # Two windows with a hole between them: one (start, end) pair cannot
        # describe that, so the span columns stay NULL and the hole is re-fetched.
        self.assertEqual(self.run_cli("2026-01-01", "2026-01-05"), 0)
        self.assertEqual(self.run_cli("2026-01-29", "2026-01-29"), 0)
        status, start, end, ranges = self.coverage()
        self.assertEqual(status, "ok")
        self.assertIsNone(start, "a hole cannot be summarised by one start date")
        self.assertIsNone(end, "a hole cannot be summarised by one end date")
        self.assertIn("2026-01-05", ranges)
        self.assertIn("2026-01-29", ranges)

        FakeClient.calls = []
        self.assertEqual(self.run_cli("2026-01-05", "2026-01-20"), 0)
        self.assertEqual(FakeClient.calls, [(SYMBOL, "2026-01-05", "2026-01-20")],
                         "a window bridging the hole must be fetched in full")

    def test_a_failed_window_does_not_extend_coverage(self):
        FakeClient.fail_windows = {("2026-01-01", "2026-01-29")}
        self.assertEqual(self.run_cli("2026-01-01", "2026-01-29"), 0)
        self.assertEqual(self.stored_ids(), [])
        self.assertEqual(self.coverage(), ("error", None, None, None))

        FakeClient.fail_windows = set()
        FakeClient.calls = []
        self.assertEqual(self.run_cli("2026-01-01", "2026-01-29"), 0)
        self.assertEqual(FakeClient.calls, [(SYMBOL, "2026-01-01", "2026-01-29")],
                         "the failed window is retried instead of being skipped")
        self.assertEqual(self.stored_ids(), ["ann-d1", "ann-d2", "ann-d3", "ann-d4"])

    def test_rerunning_an_identical_window_is_idempotent(self):
        self.assertEqual(self.run_cli("2026-01-01", "2026-01-12"), 0)
        self.assertEqual(self.run_cli("2026-01-01", "2026-01-12"), 0)
        self.assertEqual(len(FakeClient.calls), 1)
        self.assertEqual(len(self.stored_ids()), len(set(self.stored_ids())))


class SchemaMigrationTests(unittest.TestCase):
    """An existing database must gain the coverage columns without losing rows."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "old.sqlite3"

    def _legacy_database(self) -> None:
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript("""
                CREATE TABLE announcement(
                    symbol TEXT NOT NULL, announcement_date TEXT NOT NULL,
                    announcement_id TEXT NOT NULL, category_raw TEXT, title TEXT,
                    source TEXT, source_url TEXT, fetched_at TEXT,
                    PRIMARY KEY(symbol, announcement_id));
                CREATE TABLE collect_log(
                    symbol TEXT PRIMARY KEY, status TEXT NOT NULL, rows INTEGER,
                    detail TEXT, attempted_at TEXT);
                INSERT INTO announcement VALUES
                    ('600519.SH', '2026-01-05', 'old-1', 'cat', 't', 'cninfo', 'u', 'f');
                INSERT INTO collect_log(symbol, status, rows) VALUES ('600519.SH', 'ok', 1);
            """)

    def test_migration_adds_columns_and_keeps_rows(self):
        self._legacy_database()
        with closing(sqlite3.connect(self.db)) as conn:
            collector.ensure_schema(conn)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(collect_log)")}
            self.assertTrue({"covered_start", "covered_end", "covered_ranges"} <= columns)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM announcement").fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT status FROM collect_log WHERE symbol = '600519.SH'").fetchone()[0], "ok")

    def test_a_legacy_ok_row_is_not_treated_as_covering_anything(self):
        # The old schema recorded no window, so nothing proves which days were
        # fetched. Skipping would keep whatever gap the old run left behind.
        self._legacy_database()
        with closing(sqlite3.connect(self.db)) as conn:
            collector.ensure_schema(conn)
            self.assertEqual(collector.covered_ranges(conn, "600519.SH"), [])
            self.assertFalse(collector.already_done(conn, "600519.SH",
                                                    "2026-01-01", "2026-01-12"))

    def test_migration_is_idempotent(self):
        self._legacy_database()
        with closing(sqlite3.connect(self.db)) as conn:
            collector.ensure_schema(conn)
            collector.ensure_schema(conn)
            columns = [row[1] for row in conn.execute("PRAGMA table_info(collect_log)")]
        self.assertEqual(columns.count("covered_ranges"), 1)


class RangeArithmeticTests(unittest.TestCase):
    def test_contiguous_windows_merge(self):
        self.assertEqual(
            collector._merge_ranges([["2026-01-01", "2026-01-05"],
                                     ["2026-01-06", "2026-01-09"]]),
            [["2026-01-01", "2026-01-09"]])

    def test_overlapping_windows_merge(self):
        self.assertEqual(
            collector._merge_ranges([["2026-01-01", "2026-01-10"],
                                     ["2026-01-05", "2026-01-08"]]),
            [["2026-01-01", "2026-01-10"]])

    def test_windows_with_a_hole_stay_separate(self):
        self.assertEqual(
            collector._merge_ranges([["2026-01-01", "2026-01-05"],
                                     ["2026-01-07", "2026-01-09"]]),
            [["2026-01-01", "2026-01-05"], ["2026-01-07", "2026-01-09"]])

    def test_an_invalid_date_is_rejected_not_guessed(self):
        with self.assertRaises(SystemExit):
            collector.day_id("2026-13-45")


if __name__ == "__main__":
    unittest.main()
