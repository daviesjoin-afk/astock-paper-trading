# -*- coding: utf-8 -*-
"""``api_earnings`` 只读接口的回归（不依赖 HTTP 客户端，与 ``test_api_strategies`` 同手法）。"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

import api_earnings as API
import earnings_disclosure_ledger as LEDGER
import earnings_disclosure_store as STORE
import main

FORECAST_BODY = """
    一、本期业绩预计情况

    （一）业绩预告期间

    2026 年 1 月 1 日至 2026 年 6 月 30 日

    项  目                          本报告期                            上年同期

归属于上市公司 亏损：-22,800 万元～-18,000 万元            亏损：-23,892.72 万元
股东的净利润    比上年同期增长：5%～25%
"""

OBSERVED = dt.datetime(2026, 7, 16, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


class RouteRegistrationTest(unittest.TestCase):
    def test_route_is_registered_on_the_product_app(self):
        # 用 OpenAPI schema 校验接线：这只 fastapi 的 ``app.routes`` 里存的是
        # ``_IncludedRouter`` 包装对象（没有 path 属性），直接内省跨版本不可靠
        # （与 ``test_api_strategies.test_routes_registered_on_app`` 同一手法）。
        paths = (main.app.openapi() or {}).get("paths") or {}
        self.assertIn("/api/earnings-disclosure/events", paths)
        self.assertIn("get", paths["/api/earnings-disclosure/events"])


class EventsEndpointTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = os.path.join(self._directory.name, STORE.DB_FILENAME)
        patcher = mock.patch.object(STORE, "db_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self):
        STORE.ensure(self.path)
        with STORE.connect_writable(self.path) as conn:
            LEDGER.record_announcement(
                conn, code="002230", title="科大讯飞:2026年半年度业绩预告",
                announcement_id="AN-1", source_name="fixture",
                source_type="public_aggregator", body=FORECAST_BODY,
                observed_at=OBSERVED,
            )
            LEDGER.record_announcement(
                conn, code="002415", title="海康威视:2026年半年度业绩预告",
                announcement_id="AN-2", source_name="fixture",
                source_type="public_aggregator",
                body="本公司预计本期业绩与上年同期相比发生较大变动。",
                observed_at=OBSERVED,
            )

    def test_missing_ledger_degrades_instead_of_erroring(self):
        payload = API.events(as_of="2026-07-16", limit=10)
        self.assertFalse(payload["available"])
        self.assertEqual("ledger_unavailable", payload["reason"])
        self.assertEqual([], payload["events"])
        self.assertFalse(os.path.exists(self.path), "GET 不得创建数据库文件")

    def test_events_expose_owner_numbers_and_status(self):
        self._seed()
        payload = API.events(as_of="2026-07-16", limit=10)
        self.assertTrue(payload["available"])
        self.assertEqual(2, payload["count"])
        self.assertEqual({"verified": 1, "unparsable": 1}, payload["by_status"])
        verified = next(item for item in payload["events"]
                        if item["announcement_id"] == "AN-1")
        self.assertTrue(verified["is_owner_verified"])
        self.assertEqual("-22800", verified["net_profit_wan"]["low"])
        self.assertEqual("-18000", verified["net_profit_wan"]["high"])
        self.assertEqual("万元", verified["net_profit_wan"]["unit"])
        self.assertIsNone(verified["latest_claim"])
        unreadable = next(item for item in payload["events"]
                          if item["announcement_id"] == "AN-2")
        self.assertFalse(unreadable["is_owner_verified"])
        self.assertIsNone(unreadable["net_profit_wan"]["low"])

    def test_pit_boundary_hides_events_seen_after_as_of(self):
        self._seed()
        self.assertEqual(0, API.events(as_of="2026-07-15", limit=10)["count"])
        self.assertEqual(2, API.events(as_of="2026-07-16", limit=10)["count"])

    def test_default_as_of_is_today_in_the_owner_timezone(self):
        self._seed()
        with mock.patch.object(API, "_today", return_value="2026-07-16"):
            self.assertEqual(2, API.events(as_of="", limit=10)["count"])
        with mock.patch.object(API, "_today", return_value="2026-07-15"):
            self.assertEqual(0, API.events(as_of="", limit=10)["count"])

    def test_limit_is_applied_after_the_pit_filter(self):
        self._seed()
        self.assertEqual(1, API.events(as_of="2026-07-16", limit=1)["count"])


if __name__ == "__main__":
    unittest.main()
