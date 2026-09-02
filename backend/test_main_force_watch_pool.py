import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import paper_trading as PT  # noqa: E402


class MainForceWatchPoolTests(unittest.TestCase):
    def test_freeze_keeps_ten_when_later_model_output_shrinks(self):
        initial = [
            {"code": f"0000{i:02d}", "name": f"N{i}", "score": 1 - i / 100,
             "price": 10 + i, "pct": 1.0}
            for i in range(10)
        ]
        live = {
            item["code"]: {"price": 20 + index, "pct": 2.0, "quote_at": "2026-09-02T10:00:00+08:00"}
            for index, item in enumerate(initial)
        }
        now = dt.datetime(2026, 9, 2, 9, 35)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(PT, "MAIN_FORCE_WATCH_POOL_PATH", os.path.join(directory, "pool.json")):
            first, first_meta = PT._main_force_watch_pool(initial, asof_day=now.date(), now=now, live_map=live)
            later, later_meta = PT._main_force_watch_pool(
                [initial[0]], asof_day=now.date(), now=now + dt.timedelta(minutes=3), live_map=live,
            )
        self.assertEqual(len(first), 10)
        self.assertEqual(len(later), 10)
        self.assertEqual(first_meta["pool"], later_meta["pool"])
        self.assertEqual(later_meta["frozen_carry_count"], 9)
        self.assertEqual(later[-1]["price"], 29)


if __name__ == "__main__":
    unittest.main()
