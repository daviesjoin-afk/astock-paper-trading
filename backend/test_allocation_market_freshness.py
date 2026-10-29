# -*- coding: utf-8 -*-
"""运行中策略页的行情证据必须先判年龄再报"新鲜"。

``risk_center.load_snapshot()`` 只读盘上的 JSON，**不做过期判定**。如果只按
"灯是绿的"就报 fresh + verified，那么风险扫描一停、或者盘上的快照属于上一个
交易日，页面就会把**过期**的行情证据一直显示成健康。

这些用例把"快照年龄 → 状态"的判定钉住，并且用显式时刻而不是墙钟。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import market_data_contract as MDC
import paper_trading as PT

TZ = dt.timezone(dt.timedelta(hours=8))


class AllocationMarketDataFreshnessTests(unittest.TestCase):
    """``strategy_allocation_explain`` 的行情状态。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alloc-fresh-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(PT, "DB_PATH", os.path.join(self.tmp.name, "paper.sqlite3"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _explain(self, snapshot):
        with mock.patch.object(PT.RC, "load_snapshot", return_value=snapshot):
            return PT.strategy_allocation_explain()["market_data"]

    def _snapshot(self, *, asof, light="green"):
        return {"asof": asof, "market": {"light": light, "data_date": "2026-08-28"}}

    def test_a_fresh_snapshot_is_reported_as_fresh_and_verified(self):
        now = dt.datetime.now(TZ)
        data = self._explain(self._snapshot(asof=now.isoformat()))
        self.assertEqual(MDC.STATUS_FRESH, data["status"])
        self.assertEqual("verified", data["verification"])
        self.assertIsNone(data["reason"])

    def test_a_stale_snapshot_is_not_reported_as_fresh(self):
        old = dt.datetime.now(TZ) - dt.timedelta(hours=6)
        data = self._explain(self._snapshot(asof=old.isoformat()))
        self.assertNotEqual(MDC.STATUS_FRESH, data["status"])
        self.assertIsNone(data["verification"],
                          "过期的行情证据不得再声称已核验")
        self.assertEqual("stale", data["reason"])

    def test_no_snapshot_at_all_stays_unavailable(self):
        data = self._explain({})
        self.assertEqual(MDC.STATUS_UNAVAILABLE, data["status"])
        self.assertEqual("missing", data["reason"])

    def test_a_snapshot_without_a_timestamp_is_not_trusted(self):
        data = self._explain({"asof": None, "market": {"light": "green"}})
        self.assertNotEqual(MDC.STATUS_FRESH, data["status"])
        self.assertIsNone(data["verification"])

    def test_the_age_and_window_are_published(self):
        now = dt.datetime.now(TZ)
        data = self._explain(self._snapshot(asof=now.isoformat()))
        self.assertIn("snapshot_age_seconds", data)
        self.assertIn("snapshot_ttl_seconds", data)
        self.assertGreater(data["snapshot_ttl_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
