from __future__ import annotations

import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import historical_universe_public_sources as HUPS


class PublicHistoricalUniverseTests(unittest.TestCase):
    def test_normalizes_exchange_date_formats_and_rejects_invalid_dates(self):
        self.assertEqual(HUPS._as_day("20240102"), "2024-01-02")
        self.assertEqual(HUPS._as_day("2024/4/8"), "2024-04-08")
        with self.assertRaises(HUPS.PublicUniverseSourceError):
            HUPS._as_day("not-a-date")

    def test_archive_rows_create_a_delisting_revision(self):
        rows = HUPS._archive_rows([{
            "code": "600000.SH", "listed_from": "1999-11-10",
            "delisted_at": "2024-06-01", "security_type": "ordinary_share",
            "exchange": "SSE", "name": "sample",
        }], "revision")
        self.assertEqual([row["observed_at"] for row in rows],
                         ["1999-11-10", "2024-06-01"])
        self.assertIsNone(rows[0]["delisted_at"])
        self.assertEqual(rows[1]["delisted_at"], "2024-06-01")

    def test_refresh_imports_complete_archive_and_returns_asof_members(self):
        fixture = {
            "rows": [
                {"code": "600000.SH", "listed_from": "1999-11-10", "delisted_at": None,
                 "security_type": "ordinary_share", "exchange": "SSE", "name": "active"},
                {"code": "000001.SZ", "listed_from": "1991-04-03", "delisted_at": "2023-01-01",
                 "security_type": "ordinary_share", "exchange": "SZSE", "name": "delisted"},
            ],
            "coverage_start": "1991-04-03", "source_revision": "test-revision",
        }
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "adaptive.sqlite3")
            with patch.object(HUPS, "fetch_public_security_master", return_value=fixture):
                result = HUPS.refresh_archive(db_path, asof="2022-12-31")
            self.assertTrue(result["source"]["historical_membership_complete"])
            self.assertEqual({row["code"] for row in result["members"]},
                             {"600000.SH", "000001.SZ"})
            with patch.object(HUPS, "fetch_public_security_master",
                              side_effect=AssertionError("same-day archive should be reused")):
                reused = HUPS.refresh_archive(db_path, asof="2022-12-31")
            self.assertEqual(result["manifest"]["universe_archive_fingerprint"],
                             reused["manifest"]["universe_archive_fingerprint"])
            with patch.object(HUPS, "fetch_public_security_master", return_value=fixture):
                future = HUPS.refresh_archive(db_path, asof="2024-01-01")
            self.assertEqual({row["code"] for row in future["members"]}, {"600000.SH"})


if __name__ == "__main__":
    unittest.main()
