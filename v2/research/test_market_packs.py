"""Validate the reviewed manifests against exact repository snapshot bytes.
This check supplies SHA256 verification without adding a Rust crypto dependency.
"""
import hashlib
import json
from pathlib import Path
import unittest

DATA = Path(__file__).resolve().parents[1] / "data"

class ReviewedPackTests(unittest.TestCase):
    def test_reviewed_manifests_match_snapshot_contents(self):
        for number in (1, 2):
            with self.subTest(pack=number):
                manifest = json.loads((DATA / f"market-pack-{number}.json").read_text(encoding="utf8"))
                self.assertEqual(f"cn-offline-fixture-pack-{number}", manifest["pack_id"])
                self.assertEqual("offline_fixture", manifest["trust_level"])
                files = {"calendar": "calendar-2026-v1.json", "sessions": "session-policy-v1.json",
                         "instruments": f"instruments-offline-v{number}.json"}
                self.assertEqual(set(files), set(manifest["content_summary"]))
                for key, filename in files.items():
                    self.assertEqual(hashlib.sha256((DATA / filename).read_bytes()).hexdigest(),
                                     manifest["content_summary"][key], filename)
                snapshot = json.loads((DATA / files["instruments"]).read_text(encoding="utf8"))
                self.assertEqual(manifest["coverage_start"], snapshot["coverage_start"])
                self.assertEqual(manifest["coverage_end"], snapshot["coverage_end"])

