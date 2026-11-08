"""Tests for the offline export verifier."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_export  # noqa: E402


def bundle() -> dict:
    document = {
        "schema_version": 1,
        "candidate_id": None,
        "status": "promoted",
        "from_day": "2026-11-05",
        "to_day": None,
        "count": 1,
        "events": [{"event_id": "evo-1", "status": "promoted", "decided_at": "2026-11-05T15:00:00+08:00"}],
    }
    document["digest"] = verify_export.canonical_digest(document)
    return document


class VerifyExportTest(unittest.TestCase):
    def write(self, document: dict) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(document, handle)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_valid_bundle_verifies(self) -> None:
        self.assertEqual(verify_export.main([self.write(bundle())]), 0)

    def test_tampered_event_fails(self) -> None:
        document = bundle()
        document["events"][0]["status"] = "reverted"
        self.assertEqual(verify_export.main([self.write(document)]), 1)

    def test_count_mismatch_fails(self) -> None:
        document = bundle()
        document["count"] = 2
        document["digest"] = verify_export.canonical_digest(document)
        self.assertEqual(verify_export.main([self.write(document)]), 1)

    def test_bad_json_exits_two(self) -> None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        handle.write("{not json")
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        self.assertEqual(verify_export.main([handle.name]), 2)


if __name__ == "__main__":
    unittest.main()
