"""Focused regressions for production runtime safeguards."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import metrics

try:
    import adaptive_engine as adaptive
except Exception:  # pragma: no cover - source-only environments may omit dependencies
    adaptive = None


class MetricsRegressionTests(unittest.TestCase):
    def test_safe_float_keeps_real_rss_and_rejects_nan(self):
        self.assertEqual(metrics._safe_float("170.4"), 170.4)
        self.assertEqual(metrics._safe_float("nan"), 0.0)


@unittest.skipIf(adaptive is None, "adaptive engine dependencies unavailable")
class AdaptiveSchemaBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="astock-adaptive-schema-")
        self.old_path = adaptive.DB_PATH
        self.old_cache_dir = adaptive.CACHE_DIR
        self.old_ready = adaptive._SCHEMA_READY
        adaptive.DB_PATH = os.path.join(self.directory, "adaptive_learning.sqlite3")
        adaptive.CACHE_DIR = self.directory
        adaptive._SCHEMA_READY = False

    def tearDown(self):
        adaptive.DB_PATH = self.old_path
        adaptive.CACHE_DIR = self.old_cache_dir
        adaptive._SCHEMA_READY = self.old_ready
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_ready_read_connection_does_not_repeat_schema_writes(self):
        adaptive.initialize_schema()
        self.assertTrue(adaptive._SCHEMA_READY)
        with mock.patch.object(adaptive, "_init_schema", side_effect=AssertionError("unexpected schema write")):
            with adaptive._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adaptive_rewards'"
                ).fetchone()
        self.assertIsNotNone(row)
