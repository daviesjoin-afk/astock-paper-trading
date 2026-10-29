# -*- coding: utf-8 -*-
"""主报价与独立核验源必须并行取数。

旧实现先等东财 ulist、再等腾讯/新浪核验源，两个 8 秒级等待直接相加；
生产 `position_quotes` 单轮 3~63 秒，其中很大一部分就是这段串行叠加。
两个源只依赖同一份代码集合，用屏障证明它们确实同时发出请求。
"""
import datetime as dt
import threading
import unittest
from unittest import mock

import paper_trading as PT

TZ = dt.timezone(dt.timedelta(hours=8))


class QuoteSourceParallelismTests(unittest.TestCase):
    def test_primary_and_independent_sources_run_concurrently(self):
        day = dt.date.today()
        stamp = dt.datetime.now(TZ).isoformat(timespec="seconds")
        common = {
            "code": "600001", "price": 10.0, "pct": 1.0, "quote_at": stamp,
            "super_net": 1.0, "main_pct": 0.1,
        }
        barrier = threading.Barrier(2, timeout=5)
        arrived = []

        def sync():
            barrier.wait()
            arrived.append(threading.get_ident())

        def primary(codes):
            sync()
            return [{**common, "source": "eastmoney_ulist"}]

        def independent(codes):
            sync()
            return [{**common, "source": "sina_public_quote"}]

        with mock.patch.object(PT.dfc, "fetch_realtime_for_codes", side_effect=primary), \
                mock.patch.object(PT.dfc, "fetch_independent_realtime_for_codes",
                                  side_effect=independent), \
                mock.patch.object(PT.dfc, "enrich_live_flow_details", return_value={}), \
                mock.patch.object(PT, "_latest_price_map", return_value={}), \
                mock.patch.object(PT, "_universe_snapshot_time", return_value=None), \
                mock.patch.object(PT.PLTrad, "prefetch", return_value={}):
            row = PT._quotes(["600001"], asof_date=day)["600001"]

        self.assertEqual(len(arrived), 2, "两个源没有同时进入请求")
        self.assertEqual(row["quote_validation"], "cross_source_checked")
        self.assertEqual(row["quote_cross_check"]["source"], "sina_public_quote")

    def test_one_source_failure_is_recorded_without_cancelling_the_other(self):
        day = dt.date.today()
        stamp = dt.datetime.now(TZ).isoformat(timespec="seconds")
        common = {"code": "600001", "price": 10.0, "pct": 1.0, "quote_at": stamp}
        reading = mock.Mock(status="unavailable", available=False, freshness=None,
                            reason=None, snapshot=None)
        reading.rows.return_value = []

        with mock.patch.object(PT.dfc, "fetch_realtime_for_codes",
                               side_effect=RuntimeError("primary down")), \
                mock.patch.object(PT.dfc, "fetch_independent_realtime_for_codes",
                                  return_value=[{**common, "source": "sina_public_quote"}]), \
                mock.patch.object(PT.MDSvc, "read_snapshot", return_value=reading), \
                mock.patch.object(PT.dfc, "enrich_live_flow_details", return_value={}), \
                mock.patch.object(PT, "_latest_price_map", return_value={}), \
                mock.patch.object(PT, "_universe_snapshot_time", return_value=None), \
                mock.patch.object(PT.PLTrad, "prefetch", return_value={}):
            row = PT._quotes(["600001"], asof_date=day)["600001"]

        self.assertEqual(row["price"], 10.0)
        self.assertIn("primary down", row["quote_source_errors"]["live"])
        # 独立源被提升为主报价后，不能再用自己核验自己。
        self.assertNotEqual(row["quote_validation"], "cross_source_checked")


if __name__ == "__main__":
    unittest.main()
