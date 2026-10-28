# -*- coding: utf-8 -*-
"""低频状态源的缓存窗口（停牌/公告/快讯）。

参照 HTTP 缓存的 ``max-age`` / ``stale-while-revalidate`` 分层思路：报价是
秒级数据，停牌名单与公告是分钟到小时级数据。盘中每 3 分钟重拉完整停牌报表
既没有新信息，又把成交性校验拖到数秒。这里把窗口钉成可验证的行为。
"""
import json
import tempfile
import unittest
from unittest import mock

import data_fetcher as dfc
import marketdata_cache as MCache
import paper_live_tradability as T


class SuspensionCacheTests(unittest.TestCase):
    def setUp(self):
        with T._LOCK:
            T._CACHE.clear()

    def tearDown(self):
        with T._LOCK:
            T._CACHE.clear()

    def test_status_window_is_minutes_not_the_quote_window(self):
        self.assertGreaterEqual(T.STATUS_CACHE_TTL_SECONDS, 600)
        # 成交观测本身仍是 120 秒硬门禁；缓存只复用报表内容。
        self.assertEqual(T.STATUS_MAX_AGE_SECONDS, 120)

    def test_repeated_scans_reuse_the_suspension_payload(self):
        clock = [1000.0]
        calls = []

        def fake_get(_url, params=None, **_kwargs):
            calls.append(params)
            return json.dumps({"success": True, "result": {
                "pages": 1, "count": 0, "data": []}})

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(T, "STATUS_CACHE_DIR", tmp), \
                mock.patch.object(T, "http_get", side_effect=fake_get), \
                mock.patch.object(MCache.time, "time", side_effect=lambda: clock[0]):
            T._get(("suspend", "2026-10-28", 1), "u", {})
            clock[0] += 61.0
            T._get(("suspend", "2026-10-28", 1), "u", {})
            self.assertEqual(len(calls), 1, "60 秒后的下一轮扫描不应重拉停牌报表")
            clock[0] += T.STATUS_CACHE_TTL_SECONDS + 1
            T._get(("suspend", "2026-10-28", 1), "u", {})
            self.assertEqual(len(calls), 2)


class NewsCacheTests(unittest.TestCase):
    def test_news_and_announcement_keep_separate_windows(self):
        self.assertGreaterEqual(dfc.NEWS_CACHE_TTL_SECONDS, 300)
        self.assertGreaterEqual(dfc.ANNOUNCEMENT_CACHE_TTL_SECONDS, 1800)
        self.assertGreater(dfc.ANNOUNCEMENT_CACHE_TTL_SECONDS,
                           dfc.NEWS_CACHE_TTL_SECONDS)

    def test_fast_news_uses_the_persistent_news_window(self):
        with mock.patch.object(dfc, "_persistent_list", return_value=[]) as cached:
            self.assertEqual(dfc.fetch_fast_news(50), [])
        args = cached.call_args.args
        self.assertEqual(args[0], "news")
        self.assertEqual(args[1], dfc.NEWS_CACHE_PATH)
        self.assertEqual(args[2], dfc.NEWS_CACHE_TTL_SECONDS)

    def test_announcements_use_the_persistent_announcement_window(self):
        with mock.patch.object(dfc, "_persistent_list", return_value=[]) as cached:
            self.assertEqual(dfc.fetch_company_announcements(["600000", "000001"]), [])
        args = cached.call_args.args
        self.assertEqual(args[0], "announcements:000001,600000")
        self.assertTrue(args[1].startswith(dfc.ANNOUNCEMENT_CACHE_DIR))
        self.assertEqual(args[2], dfc.ANNOUNCEMENT_CACHE_TTL_SECONDS)


if __name__ == "__main__":
    unittest.main()
