import datetime as dt
import os
import tempfile
import unittest

import entry_timing as timing


class FastEntryTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        timing.STATE_PATH = os.path.join(self.tmp.name, "entry.json")
        timing._state = None
        self.t0 = dt.datetime.combine(dt.date.today(), dt.time(9, 40))

    def tearDown(self):
        timing._state = None
        self.tmp.cleanup()

    @staticmethod
    def strong():
        return {
            "cross_source_checked": True,
            "main_pct": 3.2,
            "vol_ratio": 1.6,
            "active_buy_sell_imbalance": 0.05,
            "depth_imbalance": 0.10,
        }

    def test_two_fast_observations_confirm_momentum_candidate(self):
        allowed, info = timing.evaluate("tq_breakout", "000001", 10.0, 4.0, now=self.t0)
        self.assertFalse(allowed)
        self.assertEqual(info["state"], "triggered")
        allowed, info = timing.evaluate(
            "tq_breakout", "000001", 10.01, 4.1,
            now=self.t0 + dt.timedelta(seconds=30), fast=True, evidence=self.strong(),
        )
        self.assertFalse(allowed)
        self.assertEqual(info["state"], "fast_confirming")
        allowed, info = timing.evaluate(
            "tq_breakout", "000001", 10.02, 4.2,
            now=self.t0 + dt.timedelta(seconds=60), fast=True, evidence=self.strong(),
        )
        self.assertTrue(allowed)
        self.assertTrue(info["fast_path"])

    def test_weak_evidence_resets_fast_count(self):
        timing.evaluate("tq_breakout", "000002", 10.0, 4.0, now=self.t0)
        timing.evaluate(
            "tq_breakout", "000002", 10.01, 4.1,
            now=self.t0 + dt.timedelta(seconds=30), fast=True, evidence=self.strong(),
        )
        weak = {**self.strong(), "main_pct": -1.0}
        allowed, info = timing.evaluate(
            "tq_breakout", "000002", 10.01, 4.1,
            now=self.t0 + dt.timedelta(seconds=60), fast=True, evidence=weak,
        )
        self.assertFalse(allowed)
        self.assertIn("0/2", info["reason"])

    def test_fast_path_cannot_bypass_trigger_price_chase_cap(self):
        timing.evaluate("tq_breakout", "000003", 10.0, 4.0, now=self.t0)
        allowed, info = timing.evaluate(
            "tq_breakout", "000003", 10.25, 6.0,
            now=self.t0 + dt.timedelta(seconds=30), fast=True, evidence=self.strong(),
        )
        self.assertFalse(allowed)
        self.assertEqual(info["state"], "expired")

    def test_light_active_selling_can_be_offset_by_depth_and_main_flow(self):
        timing.evaluate("tq_breakout", "000004", 10.0, 2.0, now=self.t0)
        balanced = {
            **self.strong(), "main_pct": 14.63,
            "active_buy_sell_imbalance": -0.2433,
            "depth_imbalance": 0.2822,
        }
        timing.evaluate(
            "tq_breakout", "000004", 10.01, 2.1,
            now=self.t0 + dt.timedelta(seconds=30), fast=True, evidence=balanced,
        )
        allowed, info = timing.evaluate(
            "tq_breakout", "000004", 10.02, 2.2,
            now=self.t0 + dt.timedelta(seconds=60), fast=True, evidence=balanced,
        )
        self.assertTrue(allowed)
        self.assertTrue(info["fast_path"])

    def test_fast_confirmation_is_consumed_by_next_normal_scan(self):
        timing.evaluate("tq_breakout", "000005", 10.0, 4.0, now=self.t0)
        timing.evaluate(
            "tq_breakout", "000005", 10.01, 4.1,
            now=self.t0 + dt.timedelta(seconds=30), fast=True, evidence=self.strong(),
        )
        allowed, _ = timing.evaluate(
            "tq_breakout", "000005", 10.02, 4.2,
            now=self.t0 + dt.timedelta(seconds=60), fast=True, evidence=self.strong(),
        )
        self.assertTrue(allowed)

        allowed, info = timing.evaluate(
            "tq_breakout", "000005", 10.02, 4.2,
            now=self.t0 + dt.timedelta(seconds=90), fast=False,
        )
        self.assertTrue(allowed)
        self.assertEqual(info["state"], "confirmed")
        self.assertTrue(info["fast_path"])
        self.assertIn("三分钟正式任务", info["reason"])


if __name__ == "__main__":
    unittest.main()
