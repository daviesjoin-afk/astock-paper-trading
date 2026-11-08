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

    def test_manifest_sidecar_is_written(self) -> None:
        sidecar = self.dir / "manifest.json"
        code = verify_archive.main([
            "--db", str(self.db),
            "--export", str(self.export),
            "--manifest-out", str(sidecar),
        ])
        self.assertEqual(code, 0)
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 15)
        self.assertEqual(payload["export_count"], 1)
        self.assertEqual(len(payload["manifest"]), 64)
        self.assertEqual(payload["db"]["path"], self.db.name)

    def test_failed_archive_writes_no_manifest(self) -> None:
        self.db.write_bytes(b"SQLite format 3" + bytes(64))
        sidecar = self.dir / "manifest.json"
        self.assertEqual(
            verify_archive.main([
                "--db", str(self.db),
                "--export", str(self.export),
                "--manifest-out", str(sidecar),
            ]),
            1,
        )
        self.assertFalse(sidecar.exists())

    def test_missing_file_is_usage_error(self) -> None:
        self.assertEqual(
            verify_archive.main(["--db", str(self.dir / "nope.sqlite3"), "--export", str(self.export)]),
            2,
        )

    def clear_dir(self) -> None:
        for path in self.dir.iterdir():
            path.unlink(missing_ok=True)

    def test_directory_mode_verifies_pairs(self) -> None:
        self.clear_dir()
        db = self.dir / "astock-2026-11.sqlite3"
        make_db(db)
        export = self.dir / "astock-2026-11.export.json"
        make_export(export)
        self.assertEqual(verify_archive.main(["--dir", str(self.dir)]), 0)

    def test_directory_mode_flags_missing_export(self) -> None:
        self.clear_dir()
        make_db(self.dir / "orphan.sqlite3")
        self.assertEqual(verify_archive.main(["--dir", str(self.dir)]), 1)

    def test_directory_mode_checks_sidecar_manifest(self) -> None:
        self.clear_dir()
        db = self.dir / "pair.sqlite3"
        make_db(db)
        export = self.dir / "pair.export.json"
        make_export(export)
        sidecar = self.dir / "pair.manifest.json"
        sidecar.write_text(json.dumps({"manifest": "0" * 64}), encoding="utf-8")
        self.assertEqual(verify_archive.main(["--dir", str(self.dir)]), 1)
if __name__ == "__main__":
    unittest.main()

    def test_directory_mode_verifies_pairs(self) -> None:
        db = self.dir / "astock-2026-11.sqlite3"
        make_db(db)
        export = self.dir / "astock-2026-11.export.json"
        make_export(export)
        self.assertEqual(verify_archive.main(["--dir", str(self.dir)]), 0)

    def test_directory_mode_flags_missing_export(self) -> None:
        make_db(self.dir / "orphan.sqlite3")
        self.assertEqual(verify_archive.main(["--dir", str(self.dir)]), 1)

    def test_directory_mode_checks_sidecar_manifest(self) -> None:
        db = self.dir / "pair.sqlite3"
        make_db(db)
        export = self.dir / "pair.export.json"
        make_export(export)
        sidecar = self.dir / "pair.manifest.json"
        sidecar.write_text(json.dumps({"manifest": "0" * 64}), encoding="utf-8")
        self.assertEqual(verify_archive.main(["--dir", str(self.dir)]), 1)
