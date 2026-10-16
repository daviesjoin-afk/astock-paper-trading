"""Indexed disclosure storage must preserve raw evidence and visibility rules."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
from urllib.error import URLError

import disclosure_cache as C
import disclosure_timeline as D


class IndexedDisclosureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "disclosure.json"
        self.payload = {"version": 1, "rows": {code: {
            "fetched_epoch": time.time(), "fetched_at": "2026-10-13T09:00:00+08:00", "ok": True,
            "items": [{"title": "2026年第一季度报告", "display_time": "2026-04-25 18:00:00",
                       "nested": {"preserve": [None, False, 3.5, "公告 😀"]}}],
        } for code in ("000001", "000002")}}
        self.path.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf8")
        patch = mock.patch.object(D, "CACHE_PATH", str(self.path))
        patch.start()
        self.addCleanup(patch.stop)
        D._read_cache_file.cache_clear()
        self.addCleanup(D._read_cache_file.cache_clear)

    def test_import_is_lossless_and_requested_lookup_uses_primary_key(self):
        before = hashlib.sha256(self.path.read_bytes()).digest()
        self.assertEqual(C.migrate(self.path), {"rows": 2, "verified": True})
        self.assertEqual(C.load(self.path, ["000002"]), {"rows": {"000002": self.payload["rows"]["000002"]}})
        with closing(sqlite3.connect(str(self.path) + ".sqlite3")) as conn:
            plan = str(conn.execute("EXPLAIN QUERY PLAN SELECT payload FROM cache_rows WHERE code IN (?)",
                                    ("000002",)).fetchall())
        self.assertIn("SEARCH", plan)
        self.assertNotIn("SCAN cache_rows", plan)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)
        with mock.patch.object(D, "_load_cache", side_effect=AssertionError("whole JSON read")), \
                mock.patch.object(D, "_request_json", side_effect=AssertionError("network")):
            row = D.fetch_disclosure_timeline(["000002"], asof="2026-10-13")[0]
            self.assertEqual(row["status"], "cached")

    def test_visibility_and_failed_refresh_results_match_legacy_for_all_cutoffs(self):
        failure = mock.Mock(side_effect=URLError("offline"))
        cutoffs = ("2026-01-01", "2026-04-25T17:59:59+08:00", "2026-04-25T18:00:00+08:00", None)
        now = D._parse_dt("2026-10-16T14:54:00+08:00")
        with mock.patch.object(D, "_now", return_value=now):
            expected = [D.fetch_disclosure_timeline(["000001", "000002", "000003"], asof=c,
                                                   request_json=failure) for c in cutoffs]
            C.migrate(self.path)
            actual = [D.fetch_disclosure_timeline(["000001", "000002", "000003"], asof=c,
                                                 request_json=failure) for c in cutoffs]
        self.assertEqual(actual, expected)
        self.assertEqual(C.load(self.path, ["000001"])["rows"]["000001"], self.payload["rows"]["000001"])

    def test_only_changed_code_is_published_and_fetched_times_survive(self):
        C.migrate(self.path)
        before = self.path.read_bytes()
        response = {"data": {"list": [{"title": "2026年半年度报告",
                                      "display_time": "2026-08-25 18:00:00"}]}}
        with mock.patch.object(D, "_save_cache", side_effect=AssertionError("whole JSON write")):
            live = D.fetch_disclosure_timeline(["000001"], cache_ttl=0, request_json=lambda *a, **k: response)[0]
        result = C.load(self.path, ["000001", "000002"])["rows"]
        self.assertEqual(result["000002"], self.payload["rows"]["000002"])
        self.assertEqual(live["report_period"], "2026-06-30")
        self.assertEqual(self.path.read_bytes(), before)
        with mock.patch.object(D, "_request_json", side_effect=URLError("offline")), \
                mock.patch.object(C, "save", side_effect=AssertionError("failed refresh wrote")):
            stale = D.fetch_disclosure_timeline(["000001"], cache_ttl=0)[0]
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["fetched_at"], live["fetched_at"])

    def test_distinct_updates_do_not_lose_rows_and_late_result_cannot_overwrite_newer(self):
        C.migrate(self.path)
        epoch = time.time() + 1
        newer = {**self.payload["rows"]["000001"], "fetched_epoch": epoch, "items": []}
        other = {**newer, "fetched_epoch": epoch + 1}
        self.assertTrue(C.save(self.path, {"000001": newer}))
        self.assertTrue(C.save(self.path, {"000002": other}))
        self.assertTrue(C.save(self.path, {"000001": self.payload["rows"]["000001"]}))
        self.assertEqual(C.load(self.path, ["000001", "000002"])["rows"],
                         {"000001": newer, "000002": other})

    def test_bad_second_entry_rolls_back_first_entry_and_does_not_pollute_reads(self):
        C.migrate(self.path)
        newer = {**self.payload["rows"]["000001"], "fetched_epoch": time.time() + 1, "items": []}
        with self.assertRaises(ValueError):
            C.save(self.path, {"000001": newer, "000002": {"items": [float("nan")]}})
        self.assertEqual(C.load(self.path, ["000001", "000002"])["rows"], self.payload["rows"])

    def test_old_writer_atomic_replacement_invalidates_sidecar_even_same_mtime_size(self):
        C.migrate(self.path)
        old = self.path.stat()
        replacement = self.path.with_suffix(".new")
        replacement.write_bytes(self.path.read_bytes())
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        os.replace(replacement, self.path)
        self.assertIsNone(C.load(self.path, ["000001"]))
        with mock.patch.object(D, "_load_cache", wraps=D._load_cache) as load:
            self.assertEqual(D.fetch_disclosure_timeline(["000001"])[0]["status"], "cached")
            load.assert_called_once()

    def test_failed_import_never_publishes_and_existing_store_is_not_replaced(self):
        with mock.patch.object(C, "_signature", side_effect=["before", "after"]):
            with self.assertRaisesRegex(ValueError, "changed"):
                C.migrate(self.path)
        self.assertFalse(Path(str(self.path) + ".sqlite3").exists())
        self.assertFalse(list(self.path.parent.glob("*.tmp")))
        C.migrate(self.path)
        with self.assertRaises(FileExistsError):
            C.migrate(self.path)
        self.assertEqual(C.load(self.path, ["000001"])["rows"]["000001"], self.payload["rows"]["000001"])

    def test_missing_or_corrupt_index_keeps_legacy_available(self):
        self.assertIsNone(C.load(self.path, ["000001"]))
        Path(str(self.path) + ".sqlite3").write_bytes(b"invalid")
        self.assertIsNone(C.load(self.path, ["000001"]))
        self.assertEqual(D.fetch_disclosure_timeline(["000001"])[0]["status"], "cached")
