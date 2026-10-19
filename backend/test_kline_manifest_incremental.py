"""Offline regressions for incremental CSV/manifest reconciliation."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pandas as pd

import data_fetcher as dfc


class KlineManifestIncrementalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="astock-manifest-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.klines = self.root / "klines"
        self.klines.mkdir()
        self.manifest_path = self.root / "manifest.json"
        patches = mock.patch.multiple(
            dfc, KLINE_DIR=str(self.klines), KLINE_MANIFEST_PATH=str(self.manifest_path),
            _manifest=None, _manifest_mtime=None, _manifest_dirty=0, _manifest_pending={},
        )
        patches.start()
        self.addCleanup(patches.stop)

    def frame(self, periods=2):
        return pd.DataFrame(
            {"open": 10., "close": 10.1, "high": 10.2, "low": 9.8,
             "volume": 1000., "amount": 10100.},
            index=pd.date_range("2026-09-01", periods=periods),
        )

    def write_csv(self, code="000001", periods=2):
        path = self.klines / f"{code}.csv"
        self.frame(periods).to_csv(path)
        return path

    def test_unchanged_files_skip_reads_and_preserve_persisted_manifest(self):
        self.write_csv()
        self.write_csv("000002")
        original = dfc.rebuild_kline_manifest()
        original_bytes = self.manifest_path.read_bytes()
        # A fresh process must also reuse the persisted file signatures.
        dfc._manifest = None
        dfc._manifest_mtime = None
        with mock.patch.object(dfc, "load_cached_kline", wraps=dfc.load_cached_kline) as load:
            result = dfc.rebuild_kline_manifest()
        self.assertEqual(load.call_count, 0)
        self.assertEqual(result, original)
        self.assertEqual(self.manifest_path.read_bytes(), original_bytes)

    def test_saved_files_are_verified_once_and_pending_entries_flush(self):
        frame = self.frame()
        frame.attrs.update(source="test-feed", adjustment="qfq")
        dfc.save_kline("000001", frame)
        with mock.patch.object(dfc, "load_cached_kline", wraps=dfc.load_cached_kline) as load:
            dfc.rebuild_kline_manifest()
            dfc.rebuild_kline_manifest()
        self.assertEqual(load.call_count, 1)
        entry = json.loads(self.manifest_path.read_text(encoding="utf-8"))["stocks"]["000001"]
        self.assertEqual(entry["source"], "test-feed")
        self.assertEqual(entry["adjustment"], "qfq")
        self.assertEqual(entry["last_date"], "2026-09-02")
        self.assertEqual(dfc._manifest_pending, {})

    def test_saved_duplicate_unsorted_dates_are_reconciled_from_actual_csv(self):
        frame = self.frame(3).iloc[[2, 0, 0]]
        dfc.save_kline("000001", frame)
        entry = dfc.rebuild_kline_manifest()["000001"]
        self.assertEqual(entry["rows"], 2)
        self.assertEqual(entry["first_date"], "2026-09-01")
        self.assertEqual(entry["last_date"], "2026-09-03")

    def test_legacy_metadata_is_reconciled_once_and_source_is_preserved(self):
        self.write_csv()
        self.manifest_path.write_text(json.dumps({"stocks": {"000001": {
            "rows": 1, "last_date": "2026-08-31", "source": "legacy-feed", "adjustment": "qfq",
        }}}), encoding="utf-8")
        with mock.patch.object(dfc, "load_cached_kline", wraps=dfc.load_cached_kline) as load:
            first = dict(dfc.rebuild_kline_manifest()["000001"])
            dfc.rebuild_kline_manifest()
        self.assertEqual(load.call_count, 1)
        self.assertEqual(first["rows"], 2)
        self.assertEqual(first["last_date"], "2026-09-02")
        self.assertEqual(first["source"], "legacy-feed")
        self.assertEqual(first["adjustment"], "qfq")

    def test_only_new_and_changed_files_are_read(self):
        self.write_csv()
        self.write_csv("000002")
        dfc.rebuild_kline_manifest()
        self.write_csv("000001", periods=3)
        self.write_csv("000003")
        with mock.patch.object(dfc, "load_cached_kline", wraps=dfc.load_cached_kline) as load:
            result = dfc.rebuild_kline_manifest()
        self.assertEqual({call.args[0] for call in load.call_args_list}, {"000001", "000003"})
        self.assertEqual(result["000001"]["rows"], 3)
        self.assertEqual(result["000001"]["last_date"], "2026-09-03")
        self.assertEqual(len(result), 3)

    def test_size_change_is_detected_even_when_mtime_is_preserved(self):
        path = self.write_csv()
        dfc.rebuild_kline_manifest()
        before = path.stat()
        self.write_csv(periods=3)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with mock.patch.object(dfc, "load_cached_kline", wraps=dfc.load_cached_kline) as load:
            result = dfc.rebuild_kline_manifest()
        self.assertEqual(load.call_count, 1)
        self.assertEqual(result["000001"]["rows"], 3)

    def test_mtime_change_is_detected_when_size_is_unchanged(self):
        path = self.write_csv()
        dfc.rebuild_kline_manifest()
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        with mock.patch.object(dfc, "load_cached_kline", wraps=dfc.load_cached_kline) as load:
            dfc.rebuild_kline_manifest()
        self.assertEqual(load.call_count, 1)

    def test_invalid_csv_is_not_certified_and_can_be_retried(self):
        path = self.write_csv()
        prior = dict(dfc.rebuild_kline_manifest()["000001"])
        path.write_text("broken\n1\n", encoding="utf-8")
        result = dfc.rebuild_kline_manifest()
        self.assertEqual(result["000001"], prior)
        self.write_csv(periods=3)
        self.assertEqual(dfc.rebuild_kline_manifest()["000001"]["rows"], 3)

    def test_file_replaced_during_read_is_not_certified(self):
        self.write_csv()
        original_load = dfc.load_cached_kline

        def replacing_load(code):
            frame = original_load(code)
            self.write_csv(code, periods=3)
            return frame

        with mock.patch.object(dfc, "load_cached_kline", side_effect=replacing_load):
            self.assertNotIn("000001", dfc.rebuild_kline_manifest())
        result = dfc.rebuild_kline_manifest()
        self.assertEqual(result["000001"]["rows"], 3)

    def test_metadata_published_during_scan_is_not_overwritten(self):
        self.write_csv()
        original_load = dfc.load_cached_kline
        published = {"rows": 2, "source": "concurrent-feed", "adjustment": "qfq"}

        def publishing_load(code):
            frame = original_load(code)
            dfc._manifest[code] = dict(published)
            dfc._manifest_pending[code] = dict(published)
            return frame

        with mock.patch.object(dfc, "load_cached_kline", side_effect=publishing_load):
            result = dfc.rebuild_kline_manifest()
        self.assertEqual(result["000001"], published)

    def test_incomplete_metadata_is_not_trusted_even_with_matching_signature(self):
        path = self.write_csv()
        stat = path.stat()
        self.manifest_path.write_text(json.dumps({"stocks": {"000001": {
            "file_mtime_ns": stat.st_mtime_ns, "file_size": stat.st_size,
        }}}), encoding="utf-8")
        result = dfc.rebuild_kline_manifest()
        self.assertEqual(result["000001"]["rows"], 2)
        self.assertEqual(result["000001"]["last_date"], "2026-09-02")


if __name__ == "__main__":
    unittest.main()
