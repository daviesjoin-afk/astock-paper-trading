"""入场时机状态机的时间窗回归测试。"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import entry_timing as ET  # noqa: E402


class EntryTimingWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_path = ET.STATE_PATH
        self.original_state = ET._state
        ET.STATE_PATH = os.path.join(self.tmpdir.name, "entry_timing_state.json")
        ET._state = None
        ET.reset()

    def tearDown(self):
        ET.STATE_PATH = self.original_path
        ET._state = self.original_state
        self.tmpdir.cleanup()

    @staticmethod
    def _weak_evidence():
        return {
            "cross_source_checked": True,
            "main_pct": 0.0,
            "vol_ratio": 1.0,
            "active_buy_sell_imbalance": 0.0,
            "depth_imbalance": 0.0,
        }

    def test_fast_scans_do_not_consume_three_minute_expiry_budget(self):
        now = dt.datetime(2026, 9, 1, 10, 0, 0)
        allowed, info = ET.evaluate("tq_breakout", "000001", 10.0, 1.0, now=now)
        self.assertFalse(allowed)
        for step in range(1, 16):
            allowed, info = ET.evaluate(
                "tq_breakout", "000001", 10.0, 1.0,
                now=now + dt.timedelta(seconds=30 * step), fast=True,
                evidence=self._weak_evidence(),
            )
            self.assertFalse(allowed)
            self.assertNotEqual(info["state"], "expired", info)
        self.assertEqual(info["record"]["scans_since_trigger"], 1)

    def test_timeout_retriggers_when_price_is_still_within_chase_cap(self):
        now = dt.datetime(2026, 9, 1, 10, 0, 0)
        ET.evaluate("sector_rotation", "000002", 10.0, 1.0, now=now)
        allowed, info = ET.evaluate(
            "sector_rotation", "000002", 10.1, 1.0,
            now=now + dt.timedelta(minutes=61),
        )
        self.assertFalse(allowed)
        self.assertEqual(info["state"], "triggered")
        self.assertTrue(info["record"]["retriggered_after_timeout"])
        self.assertEqual(info["record"]["trigger_price"], 10.1)
