# -*- coding: utf-8 -*-
"""A13：/metrics 的任务新鲜度、市场快照状态与运行时版本采集。"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import metrics as MET  # noqa: E402


def _stamp(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


class RuntimeVersionMetricTests(unittest.TestCase):
    def test_version_number_encodes_major_minor_patch(self):
        value = MET._collect_runtime_version_metrics()["sqlite_version_number"]
        parts = [int(piece) for piece in sqlite3.sqlite_version.split(".")[:3]]
        self.assertEqual(parts[0] * 10000 + parts[1] * 100 + parts[2], value)


class PaperMetricTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(MET, "CACHE_DIR", self._tmp.name)
        self._patch.start()
        self._db = os.path.join(self._tmp.name, "paper_trading.sqlite3")

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _create(self):
        con = sqlite3.connect(self._db)
        con.execute(
            "CREATE TABLE paper_job_runs (run_key TEXT, slot TEXT, market_date TEXT, "
            "status TEXT, detail TEXT, started_at TEXT, finished_at TEXT, owner_key TEXT, "
            "heartbeat_at TEXT, expires_at TEXT, fencing_token INTEGER)"
        )
        con.execute(
            "CREATE TABLE paper_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "account_id TEXT, event TEXT, detail TEXT, created_at TEXT, strategy_id TEXT, "
            "strategy_version TEXT, strategy_checksum TEXT)"
        )
        con.commit()
        con.close()

    def test_missing_database_reports_nothing(self):
        self.assertEqual({}, MET._collect_paper_metrics())

    def test_latest_run_and_snapshot_state_are_reported(self):
        self._create()
        started = dt.datetime.now() - dt.timedelta(seconds=30)
        finished = started + dt.timedelta(seconds=95)
        con = sqlite3.connect(self._db)
        con.execute(
            "INSERT INTO paper_job_runs (run_key, slot, status, started_at, finished_at) "
            "VALUES (?,?,?,?,?)",
            ("intraday:1", "intraday", "completed", _stamp(started), _stamp(finished)),
        )
        con.execute(
            "INSERT INTO paper_audit (event, detail) VALUES (?,?)",
            ("live_scan_gate", json.dumps({
                "ready": True,
                "snapshot": {"source": "tencent_public_quote_full_snapshot",
                             "price_only": True, "rows": 5555, "flow_field_coverage": 0.0},
            })),
        )
        con.commit()
        con.close()

        metrics = MET._collect_paper_metrics()
        self.assertEqual(0, metrics["paper_slot_last_failed"])
        self.assertAlmostEqual(95.0, metrics["paper_slot_last_duration_seconds"], delta=1.0)
        self.assertGreaterEqual(metrics["paper_slot_last_age_seconds"], 25.0)
        self.assertLess(metrics["paper_slot_last_age_seconds"], 120.0)
        self.assertEqual(1, metrics["market_snapshot_price_only"])
        self.assertEqual(0.0, metrics["market_snapshot_flow_field_coverage"])
        self.assertEqual(5555.0, metrics["market_snapshot_rows"])

    def test_failed_run_is_flagged(self):
        self._create()
        con = sqlite3.connect(self._db)
        con.execute(
            "INSERT INTO paper_job_runs (run_key, slot, status, started_at) VALUES (?,?,?,?)",
            ("intraday:2", "intraday", "failed", _stamp(dt.datetime.now())),
        )
        con.commit()
        con.close()
        metrics = MET._collect_paper_metrics()
        self.assertEqual(1, metrics["paper_slot_last_failed"])
        self.assertNotIn("paper_slot_last_duration_seconds", metrics)

    def test_healthy_snapshot_is_not_flagged_as_price_only(self):
        self._create()
        con = sqlite3.connect(self._db)
        con.execute(
            "INSERT INTO paper_audit (event, detail) VALUES (?,?)",
            ("live_scan_gate", json.dumps({
                "ready": True,
                "snapshot": {"source": "eastmoney_clist_full_snapshot",
                             "price_only": False, "rows": 5588, "flow_field_coverage": 1.0},
            })),
        )
        con.commit()
        con.close()
        metrics = MET._collect_paper_metrics()
        self.assertEqual(0, metrics["market_snapshot_price_only"])
        self.assertEqual(1.0, metrics["market_snapshot_flow_field_coverage"])


if __name__ == "__main__":
    unittest.main()
