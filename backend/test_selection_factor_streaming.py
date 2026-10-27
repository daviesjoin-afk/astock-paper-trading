"""Value-equivalent streaming and publication boundaries for shared factors."""
import datetime as dt
import gc
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import weakref

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factors as F
import selection_application as M
import paper_trading as P
import selection_factor_store as STORE


def history(code):
    n = 220
    close = 10 + np.sin(np.arange(n) / (3 + int(code[-1]))) + np.arange(n) / 30
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                         "volume": np.ones(n), "amount": np.arange(n) + 100.},
                        index=pd.date_range(end="2024-06-14", periods=n, freq="B"))


class StreamingFactorTests(unittest.TestCase):
    def test_iterable_and_mapping_values_and_pit_diagnostics_match(self):
        frames = {f"60000{i}": history(str(i)) for i in range(1, 8)}
        frames["600002"].index = pd.Index(["dirty"] * len(frames["600002"]))
        manifest = {code: {"source": "test", "adjustment": "none"} for code in frames}
        manifest["600003"]["adjustment"] = "qfq"
        with mock.patch.object(F.dfc, "get_kline_manifest", return_value=manifest):
            for asof in (None, "2024-06-14", "2024-06-14T10:00:00+08:00"):
                expected = F.compute_price_factors(frames, asof=asof)
                actual = F.compute_price_factors(iter(frames.items()), asof=asof)
                pd.testing.assert_frame_equal(expected, actual)
                self.assertEqual(expected.attrs, actual.attrs)

    def test_streaming_does_not_retain_full_market_histories(self):
        alive = []
        def stream():
            for i in range(18):
                gc.collect()
                self.assertLessEqual(sum(ref() is not None for ref in alive), 1)
                frame = history(str(i % 7 + 1))
                alive.append(weakref.ref(frame))
                yield f"600{i:03}", frame
                frame = None
        with mock.patch.object(F.dfc, "get_kline_manifest", return_value={}):
            result = F.compute_price_factors(stream())
        self.assertEqual(18, len(result))
        gc.collect()
        self.assertFalse(any(ref() is not None for ref in alive))

    def test_builder_releases_histories_and_reads_each_security_once(self):
        codes = ["600001", "600002", "600003"]
        reads, refs = [], []
        def load(code):
            gc.collect()
            self.assertLessEqual(sum(ref() is not None for ref in refs), 1)
            frame = history(code)
            refs.append(weakref.ref(frame))
            reads.append(code)
            return frame
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(P.U, "load_universe", return_value=[{"code": c} for c in codes]), \
                mock.patch.object(P.dfc, "load_shared_kline", side_effect=load), \
                mock.patch.object(P, "_selection_factor_manifest_signature", return_value={"size": 1}), \
                mock.patch.object(P, "SELECTION_FACTORS_PATH", os.path.join(tmp, "f.csv")), \
                mock.patch.object(P, "SELECTION_META_PATH", os.path.join(tmp, "m.json")):
            result = P._rebuild_selection_factor_cache()
        self.assertEqual("ok", result["status"])
        self.assertEqual(codes, reads)

    def test_manifest_changed_during_build_keeps_previous_pair(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(P.U, "load_universe", return_value=[]), \
                mock.patch.object(P, "_selection_factor_manifest_signature", side_effect=[{"size": 1}, {"size": 2}]), \
                mock.patch.object(P, "SELECTION_META_PATH", os.path.join(tmp, "m.json")), \
                mock.patch.object(STORE, "publish", side_effect=AssertionError("must not publish")):
            result = P._rebuild_selection_factor_cache()
        self.assertEqual("blocked", result["status"])

    def test_duplicate_builder_is_deferred_before_loading_data(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(P, "SELECTION_META_PATH", os.path.join(tmp, "m.json")), \
                mock.patch.object(P.U, "load_universe", side_effect=AssertionError("duplicate I/O")):
            with STORE.file_lock(P.SELECTION_META_PATH + ".build.lock"):
                result = P._rebuild_selection_factor_cache()
        self.assertEqual("deferred", result["status"])


class FactorPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "f.csv")
        self.meta = os.path.join(self.tmp.name, "m.json")
        self.frame = pd.DataFrame({"price": [10.]}, index=pd.Index(["000001"], name="code"))
        STORE.publish(self.frame, {"first_board_codes": ["000001"]}, self.csv, self.meta)

    def test_hash_binds_csv_to_sidecar_and_retains_leading_zero(self):
        frame, meta = STORE.read(self.csv, self.meta)
        pd.testing.assert_frame_equal(self.frame, frame)
        self.assertEqual(64, len(meta["factors_sha256"]))
        Path(self.csv).write_text("code,price\n000001,999.0\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            STORE.read(self.csv, self.meta)

    def test_non_object_metadata_is_an_explicit_invalid_cache(self):
        Path(self.meta).write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            STORE.read(self.csv, self.meta)

    def test_preparation_failure_keeps_old_pair_and_cleans_unique_files(self):
        before = Path(self.csv).read_bytes(), Path(self.meta).read_bytes()
        with mock.patch.object(self.frame, "to_csv", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                STORE.publish(self.frame, {}, self.csv, self.meta)
        self.assertEqual(before, (Path(self.csv).read_bytes(), Path(self.meta).read_bytes()))
        self.assertEqual([], list(Path(self.tmp.name).glob("*.tmp")))

    def test_read_waits_for_pair_publication_but_not_the_long_build(self):
        started, finished = threading.Event(), threading.Event()
        def reader():
            started.set()
            STORE.read(self.csv, self.meta)
            finished.set()
        with STORE.file_lock(self.meta + ".build.lock"):
            STORE.read(self.csv, self.meta)
            with STORE.file_lock(self.meta + ".publish.lock"):
                thread = threading.Thread(target=reader)
                thread.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.wait(0.05))
            thread.join(3)
            self.assertTrue(finished.is_set())

    @unittest.skipIf(STORE.fcntl is None, "Linux cross-process deployment boundary")
    def test_second_process_cannot_acquire_the_active_build_lock(self):
        path = self.meta + ".build.lock"
        script = ("import sys; sys.path.insert(0, sys.argv[1]); "
                  "import selection_factor_store as s; "
                  "lease=s.file_lock(sys.argv[2],blocking=False); "
                  "acquired=lease.__enter__(); lease.__exit__(None,None,None); "
                  "sys.exit(1 if acquired else 0)")
        with STORE.file_lock(path):
            result = subprocess.run([sys.executable, "-c", script, str(Path(__file__).parent), path],
                                    capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_interrupted_two_file_replace_is_rejected(self):
        original = STORE.os.replace
        def replace(src, dst):
            if dst == self.meta:
                raise OSError("interrupted")
            original(src, dst)
        newer = self.frame * 2
        with mock.patch.object(STORE.os, "replace", side_effect=replace):
            with self.assertRaises(OSError):
                STORE.publish(newer, {}, self.csv, self.meta)
        with self.assertRaises(ValueError):
            STORE.read(self.csv, self.meta)

    def test_request_never_rebuilds_missing_or_invalid_factors(self):
        with mock.patch.object(M, "_complete_daily_cutoff", return_value=dt.date(2024, 6, 14)), \
                mock.patch.object(M, "_SELECTION_FACTORS_PATH", self.csv), \
                mock.patch.object(M, "_SELECTION_META_PATH", self.meta), \
                mock.patch.dict(M._SELECTION_CACHE, {"signature": None, "price_factors": None}), \
                mock.patch.object(P, "_rebuild_selection_factor_cache", side_effect=AssertionError("request build")), \
                mock.patch.object(M.dfc, "load_shared_kline", side_effect=AssertionError("market I/O")):
            self.assertTrue(M._load_selection_base()[0].empty)

    def test_factor_only_update_invalidates_request_cache(self):
        with mock.patch.object(M, "_SELECTION_FACTORS_PATH", self.csv), \
                mock.patch.object(M, "_SELECTION_META_PATH", self.meta):
            previous = M._selection_signature()
            STORE.publish(self.frame * 2, {}, self.csv, self.meta)
            self.assertNotEqual(previous, M._selection_signature())

    def test_request_reads_valid_pair_without_touching_any_history(self):
        frame = self.frame.copy()
        for column in M._SELECTION_REQUIRED_COLUMNS:
            frame[column] = False
        STORE.publish(frame, {"first_board_codes": ["000001"]}, self.csv, self.meta)
        with mock.patch.object(M, "_complete_daily_cutoff", return_value=dt.date(2024, 6, 14)), \
                mock.patch.object(M, "_SELECTION_FACTORS_PATH", self.csv), \
                mock.patch.object(M, "_SELECTION_META_PATH", self.meta), \
                mock.patch.dict(M._SELECTION_CACHE, {"signature": None, "price_factors": None}), \
                mock.patch.object(P, "_selection_factor_freshness", return_value={"passed": True}), \
                mock.patch.object(M.U, "load_universe", return_value=[]), \
                mock.patch.object(M.dfc, "load_shared_kline", side_effect=AssertionError("market I/O")):
            actual, first_boards = M._load_selection_base()
            pd.testing.assert_frame_equal(frame, actual)
            self.assertEqual({"000001"}, first_boards)


if __name__ == "__main__":
    unittest.main()
