"""Tests for the combined archive verifier."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_archive  # noqa: E402
import verify_export  # noqa: E402


def make_db(path: Path, accounts: int = 1) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE v2_paper_accounts (account_id TEXT PRIMARY KEY)")
    connection.execute("CREATE TABLE v2_evolution_events (event_id TEXT PRIMARY KEY)")
    connection.executemany(
        "INSERT INTO v2_paper_accounts(account_id) VALUES(?)",
        [(f"acct-{index}",) for index in range(accounts)],
    )
    connection.execute("PRAGMA user_version = 15")
    connection.commit()
    connection.close()


def make_export(path: Path) -> dict:
    document = {
        "schema_version": 1,
        "candidate_id": None,
        "status": None,
        "from_day": None,
        "to_day": None,
        "count": 1,
        "events": [{"event_id": "evo-1", "status": "promoted"}],
    }
    document["digest"] = verify_export.canonical_digest(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    return document


class VerifyArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="archive-"))
        self.addCleanup(lambda: [p.unlink(missing_ok=True) for p in self.dir.iterdir()] and self.dir.rmdir())
        self.db = self.dir / "ledger.sqlite3"
        self.export = self.dir / "events.json"
        make_db(self.db)
        make_export(self.export)

    def test_valid_archive_passes(self) -> None:
        self.assertEqual(verify_archive.main(["--db", str(self.db), "--export", str(self.export)]), 0)

    def test_tampered_export_fails(self) -> None:
        document = json.loads(self.export.read_text(encoding="utf-8"))
        document["count"] = 5
        self.export.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(verify_archive.main(["--db", str(self.db), "--export", str(self.export)]), 1)

    def test_truncated_database_fails(self) -> None:
        self.db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
        self.assertEqual(verify_archive.main(["--db", str(self.db), "--export", str(self.export)]), 1)

    def test_missing_file_is_usage_error(self) -> None:
        self.assertEqual(
            verify_archive.main(["--db", str(self.dir / "nope.sqlite3"), "--export", str(self.export)]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
