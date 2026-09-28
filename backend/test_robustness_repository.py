"""R30 append-only report ledger regressions (R30-64 through R30-67)."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import robustness_repository as RREP
import test_robustness_runner as FIXTURE


class RobustnessRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FIXTURE.RobustnessFixture()
        self.addCleanup(self.fixture.close)
        self.report = self.fixture.execute()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.repo = RREP.RobustnessRepository(self.conn)

    def test_R30_64_report_ledger_is_append_only(self):
        stored = self.repo.append_report(self.report)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE robustness_reports SET report_json='{}' WHERE id=?",
                              (stored["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM robustness_reports WHERE id=?", (stored["id"],))

    def test_R30_65_same_report_is_idempotent(self):
        first = self.repo.append_report(self.report)
        replay = dict(self.report, created_at="2026-02-01T00:00:00Z")
        second = self.repo.append_report(replay)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(1, self.conn.execute("SELECT count(*) FROM robustness_reports").fetchone()[0])

    def test_R30_66_same_key_with_different_content_is_rejected(self):
        self.repo.append_report(self.report)
        conflicting = dict(self.report)
        conflicting["cases"] = []
        with self.assertRaisesRegex(RREP.RobustnessPersistenceError, "report_key_payload_conflict"):
            self.repo.append_report(conflicting)

    def test_R30_67_corrupt_report_json_fails_closed(self):
        stored = self.repo.append_report(self.report)
        self.conn.execute("DROP TRIGGER robustness_reports_no_update")
        self.conn.execute("UPDATE robustness_reports SET report_json='{' WHERE id=?", (stored["id"],))
        with self.assertRaisesRegex(RREP.RobustnessPersistenceError, "corrupt_robustness_report"):
            self.repo.get_report(stored["id"])

    def test_report_list_is_bounded_to_two_hundred(self):
        with self.assertRaisesRegex(RREP.RobustnessPersistenceError, "invalid_robustness_report_limit"):
            self.repo.recent_reports(limit=201)


if __name__ == "__main__":
    unittest.main()
