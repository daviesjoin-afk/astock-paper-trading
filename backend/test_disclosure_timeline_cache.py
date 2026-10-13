import json
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError

import disclosure_timeline as D


class DisclosureCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "disclosure.json"
        self.path_patch = patch.object(D, "CACHE_PATH", str(self.path))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        D._read_cache_file.cache_clear()
        self.addCleanup(D._read_cache_file.cache_clear)

    def _seed(self):
        item = {"title": "2026年第一季度报告", "display_time": "2026-04-25 18:00:00"}
        payload = {"version": 1, "rows": {"000001": {
            "fetched_epoch": time.time(), "fetched_at": "2026-10-13T09:00:00+08:00",
            "items": [item], "ok": True,
        }}}
        self.path.write_text(json.dumps(payload), encoding="utf8")
        return payload

    def test_unchanged_file_is_parsed_once_with_independent_row_mappings(self):
        self._seed()
        with patch.object(D.json, "load", wraps=json.load) as load:
            first = D._load_cache()
            first["rows"]["000001"] = {"items": []}
            second = D._load_cache()
        self.assertEqual(load.call_count, 1)
        self.assertTrue(second["rows"]["000001"]["items"])
        self.assertEqual(D._read_cache_file.cache_info().maxsize, 1)

    def test_same_size_atomic_replacement_with_same_mtime_invalidates(self):
        self.path.write_text('{"value": 1}', encoding="utf8")
        self.assertEqual(D._load_cache()["value"], 1)
        before = self.path.stat()
        replacement = self.path.with_suffix(".new")
        replacement.write_text('{"value": 2}', encoding="utf8")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, self.path)
        self.assertEqual(D._load_cache()["value"], 2)

    def test_missing_or_corrupt_file_remains_unknown_then_recovers(self):
        self.assertEqual(D._load_cache(), {})
        self.path.write_text("invalid", encoding="utf8")
        self.assertEqual(D._load_cache(), {})
        self._seed()
        self.assertIn("000001", D._load_cache()["rows"])

    def test_fresh_hits_do_not_fetch_or_rewrite_and_keep_source_time(self):
        payload = self._seed()
        original = self.path.read_bytes()
        with patch.object(D, "_request_json", side_effect=AssertionError("network")), \
             patch.object(D, "_save_cache", wraps=D._save_cache) as save:
            for _ in range(2):
                result = D.fetch_disclosure_timeline(["000001"], asof="2026-10-13")[0]
                self.assertEqual(result["status"], "cached")
                self.assertEqual(result["fetched_at"], payload["rows"]["000001"]["fetched_at"])
            save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_refresh_keeps_stale_evidence_without_rewriting(self):
        payload = self._seed()
        with patch.object(D, "_request_json", side_effect=URLError("offline")), \
             patch.object(D, "_save_cache", wraps=D._save_cache) as save:
            result = D.fetch_disclosure_timeline(["000001"], cache_ttl=0)[0]
            self.assertEqual(result["status"], "stale")
            self.assertEqual(result["fetched_at"], payload["rows"]["000001"]["fetched_at"])
            save.assert_not_called()

    def test_unsaved_refresh_does_not_pollute_the_parsed_file_cache(self):
        payload = self._seed()
        response = {"data": {"list": [{
            "title": "2026年半年度报告", "display_time": "2026-08-25 18:00:00",
        }]}}
        with patch.object(D, "_request_json", return_value=response), \
             patch.object(D, "_save_cache") as save:
            result = D.fetch_disclosure_timeline(["000001"], cache_ttl=0)[0]
            self.assertEqual(result["report_period"], "2026-06-30")
            save.assert_called_once()
        self.assertEqual(D._load_cache()["rows"], payload["rows"])

    def test_successful_refresh_is_persisted_and_seen_by_next_read(self):
        self._seed()
        response = {"data": {"list": [{
            "title": "2026年半年度报告", "display_time": "2026-08-25 18:00:00",
        }]}}
        with patch.object(D, "_request_json", return_value=response):
            D.fetch_disclosure_timeline(["000001"], cache_ttl=0)
        with patch.object(D, "_request_json", side_effect=AssertionError("network")):
            result = D.fetch_disclosure_timeline(["000001"])[0]
            self.assertEqual(result["report_period"], "2026-06-30")
            self.assertEqual(result["status"], "cached")

    def test_save_matches_streaming_json_bytes_and_preserves_nested_evidence(self):
        payload = self._seed()
        payload["rows"]["000001"]["items"][0].update({
            "nested": [None, True, False, 1.25, {"text": '中文\n"公告"\\链接 😀'}],
        })
        original = io.StringIO()
        json.dump(payload, original, ensure_ascii=False, allow_nan=False)
        D._save_cache(payload)
        self.assertEqual(self.path.read_bytes(), original.getvalue().encode("utf8"))
        self.assertEqual(D._load_cache(), payload)

    def test_encoding_failure_never_replaces_valid_cache_or_parsed_evidence(self):
        payload = self._seed()
        D._load_cache()
        original = self.path.read_bytes()
        for bad in (float("nan"), float("inf"), object()):
            with self.subTest(value=type(bad).__name__):
                with self.assertRaises((ValueError, TypeError)):
                    D._save_cache({"bad": bad})
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(D._load_cache(), payload)

    def test_failed_atomic_replace_preserves_file_and_parsed_revision(self):
        payload = self._seed()
        D._load_cache()
        original = self.path.read_bytes()
        with patch.object(D.os, "replace", side_effect=OSError("read-only")):
            D._save_cache({"rows": {}})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(D._load_cache(), payload)
        self.assertFalse(Path(f"{self.path}.{os.getpid()}.tmp").exists())
