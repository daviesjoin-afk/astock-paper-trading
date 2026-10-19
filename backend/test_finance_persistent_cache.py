"""Financial snapshots must share evidence across processes without TTL renewal."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import data_fetcher as D
import marketdata_cache as C


VALUE = {"data": {"600001": {"net_profit": 3., "notice_date": "2026-08-01",
                             "report_published_at": "2026-08-01"}}}


class PersistentFinanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "finance.json"
        self.memory = {}
        self.memory_lock, self.fetch_lock = threading.Lock(), threading.Lock()

    def fetch(self, fn, memory=None):
        return C.persistent_cached(self.memory if memory is None else memory,
                                   self.memory_lock, self.fetch_lock, "finance", self.path,
                                   21600, fn, lambda v: isinstance(v, dict) and bool(v.get("data")))

    def test_new_process_memory_keeps_original_age_and_identical_disclosure_fields(self):
        with mock.patch.object(C.time, "time", return_value=100000.):
            self.assertEqual(self.fetch(lambda: VALUE), VALUE)
        next_memory = {}
        with mock.patch.object(C.time, "time", return_value=120000.):
            value = self.fetch(mock.Mock(side_effect=AssertionError("network")), next_memory)
        self.assertEqual(value, VALUE)
        self.assertEqual(next_memory["finance"][0], 100000.)
        self.assertEqual(json.loads(self.path.read_text())["fetched_epoch"], 100000.)

    def test_exact_expiry_empty_failure_and_future_stamp_do_not_renew_old_evidence(self):
        with mock.patch.object(C.time, "time", return_value=100000.):
            self.fetch(lambda: VALUE)
        original = self.path.read_bytes()
        with mock.patch.object(C.time, "time", return_value=121600.):
            self.assertEqual(self.fetch(lambda: {"data": {}}), {"data": {}})
            with self.assertRaisesRegex(ValueError, "provider"):
                self.fetch(mock.Mock(side_effect=ValueError("provider")))
        self.assertEqual(self.path.read_bytes(), original)
        with mock.patch.object(C.time, "time", return_value=99999.):
            self.assertEqual(self.fetch(lambda: None), None)
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_metadata_and_non_finite_or_future_times_require_provider(self):
        for bad in (None, {}, "bad", float("nan"), float("inf"), 100001.):
            with self.subTest(stamp=bad):
                self.memory.clear()
                self.path.write_text(json.dumps({"version": 1, "fetched_epoch": bad, "value": VALUE}))
                load = mock.Mock(return_value=VALUE)
                with mock.patch.object(C.time, "time", return_value=100000.):
                    self.assertEqual(self.fetch(load), VALUE)
                load.assert_called_once()

    def test_failed_atomic_publish_preserves_file_and_cleans_temporary(self):
        with mock.patch.object(C.time, "time", return_value=100000.):
            self.fetch(lambda: VALUE)
        original = self.path.read_bytes()
        newer = {"data": {"600002": {"notice_date": "2026-08-02"}}}
        with mock.patch.object(C.time, "time", return_value=121600.), \
                mock.patch.object(C.os, "replace", side_effect=OSError("read only")):
            self.assertEqual(self.fetch(lambda: newer), newer)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_provider_uses_same_snapshot_and_age_after_memory_reset(self):
        with mock.patch.object(D, "FINANCE_CACHE_PATH", str(self.path)), \
                mock.patch.dict(D._mem_cache, {}, clear=True), \
                mock.patch.dict(D._finance_fetch_state, {"retry_at": 0.}, clear=True), \
                mock.patch.object(C.time, "time", return_value=100000.):
            row = {**VALUE["data"]["600001"], "code": "600001"}
            with mock.patch.object(D, "_fetch_finance_report", return_value=[row]):
                first = D.fetch_finance_latest()
            D._mem_cache.clear()
            with mock.patch.object(C.time, "time", return_value=100100.), \
                    mock.patch.object(D, "_fetch_finance_report", side_effect=AssertionError("network")):
                self.assertEqual(first, D.fetch_finance_latest())
                self.assertEqual(D.cached_age_seconds("finance"), 100.)

    @unittest.skipIf(C._fcntl is None, "cross-process flock is provided on POSIX")
    def test_separate_processes_coalesce_one_successful_provider_attempt(self):
        code = '''import sys,time,threading
import marketdata_cache as C
def provider():
    with open(sys.argv[2],'ab') as f: f.write(b'x')
    time.sleep(0.2)
    return {'data':{'600001':{'notice_date':'2026-08-01'}}}
v=C.persistent_cached({},threading.Lock(),threading.Lock(),'finance',sys.argv[1],21600,provider,lambda v:bool(v.get('data')))
assert v['data']['600001']['notice_date']=='2026-08-01'
'''
        marker = self.path.with_suffix(".attempts")
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.path), str(marker)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env={**os.environ, "PYTHONPATH": str(Path(__file__).parent)}) for _ in range(3)]
        try:
            for process in processes:
                out, err = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, (out, err))
            self.assertEqual(marker.read_bytes(), b"x")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
