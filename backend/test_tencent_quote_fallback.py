# -*- coding: utf-8 -*-
"""腾讯全市场价格回退的门禁与来源语义回归。

本轮修复的现场：东方财富 `clist` 返回 0 行时，选股/扫描整轮 fail-closed。
腾讯批量接口实测可用（5,574 只约 3.81 秒），因此接入为**价格快照回退**。

三条不可放弃的边界：

1. 覆盖率/新鲜度不达标就不发布快照 —— 残片绝不能变成正式横截面；
2. 腾讯快照是**价格-only** 事实：不带资金流/估值/行业字段，也不得自称
   ``cross_source`` 核验；
3. 腾讯快照作主报价时，独立核验源不能仍是腾讯 —— 否则"自己核验自己"。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

import data_fetcher as dfc
import market_data_contract as MDC
import market_data_service as MDSvc
import paper_trading as PT

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 14, 10, 30, 0, tzinfo=TZ)


def _codes(count):
    return [f"{600000 + index:06d}" for index in range(count)]


class _TencentRow:
    @staticmethod
    def build(code, *, price=10.0, pct=1.0, quote_at=None):
        return {
            "code": code,
            "name": f"股票{code}",
            "price": price,
            "prev_close": 9.9,
            "pct": pct,
            "quote_at": quote_at or "20261014103000",
            "source": "tencent_public_quote",
        }


class TencentSnapshotGateTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._cache = os.path.join(self._dir, "market_snapshot_full.json")
        patcher = mock.patch.object(dfc, "MARKET_SNAPSHOT_FULL_CACHE_PATH", self._cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._patch_min_rows()
        fallback = mock.patch.object(dfc, "_fetch_sina_realtime_for_codes", return_value=[])
        fallback.start()
        self.addCleanup(fallback.stop)

    def _cleanup(self):
        for name in os.listdir(self._dir):
            path = os.path.join(self._dir, name)
            try:
                os.unlink(path)
            except OSError:
                pass
        os.rmdir(self._dir)

    def _patch_min_rows(self, rows=4000):
        patcher = mock.patch.object(dfc, "_full_market_min_rows", lambda: rows)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_full_coverage_publishes_a_price_only_snapshot(self):
        codes = _codes(dfc.FULL_MARKET_MIN_ROWS + 100)
        rows = [_TencentRow.build(code) for code in codes]
        with mock.patch.object(dfc, "fetch_tencent_realtime_for_codes", return_value=rows):
            snapshot = dfc.fetch_tencent_market_snapshot(codes, max_age=240, now=NOW)
        self.assertEqual(len(snapshot), len(codes))
        published = dfc.load_market_snapshot_full_payload()
        self.assertEqual(published.get("source"), "tencent_public_quote_full_snapshot")
        first = published["rows"][0]
        self.assertEqual(first["quote_at"], "2026-10-14T10:30:00+08:00")
        self.assertIsNone(first["super_net"])
        self.assertIsNone(first["industry"])

    def test_sina_can_supply_the_full_price_slice_when_tencent_is_down(self):
        codes = _codes(4100)
        rows = [{**_TencentRow.build(code), "source": "sina_public_quote",
                 "quote_at": NOW.isoformat()} for code in codes]
        with mock.patch.object(dfc, "fetch_tencent_realtime_for_codes", side_effect=RuntimeError("offline")), \
                mock.patch.object(dfc, "_fetch_sina_realtime_for_codes", return_value=rows) as fallback:
            snapshot = dfc.fetch_tencent_market_snapshot(codes, max_age=240, now=NOW)
        fallback.assert_called_once_with(codes)
        self.assertEqual(len(snapshot), len(codes))
        self.assertEqual(snapshot[0]["source"], "sina_public_quote")
        self.assertEqual(dfc.full_market_snapshot_source()["source"], "public_quote_full_snapshot")
        reading = MDSvc.read_snapshot(now=NOW)
        self.assertEqual(reading.reason, MDC.REASON_PRICE_ONLY_COVERAGE)
        self.assertEqual(reading.freshness, MDC.FRESHNESS_FRESH)
        self.assertFalse(MDC.is_cross_source_verified(reading.snapshot))

    def test_incomplete_coverage_publishes_nothing(self):
        codes = _codes(5000)
        rows = [_TencentRow.build(code) for code in codes[:3000]]
        with mock.patch.object(dfc, "fetch_tencent_realtime_for_codes", return_value=rows):
            self.assertEqual(
                dfc.fetch_tencent_market_snapshot(codes, max_age=240, now=NOW), []
            )
        self.assertFalse(os.path.exists(self._cache))

    def test_stale_source_time_publishes_nothing(self):
        codes = _codes(4100)
        rows = [_TencentRow.build(code, quote_at="20261013100000") for code in codes]
        with mock.patch.object(dfc, "fetch_tencent_realtime_for_codes", return_value=rows):
            self.assertEqual(
                dfc.fetch_tencent_market_snapshot(codes, max_age=240, now=NOW), []
            )
        self.assertFalse(os.path.exists(self._cache))

    def test_malformed_source_time_publishes_nothing(self):
        codes = _codes(4100)
        rows = [_TencentRow.build(code, quote_at="not-a-timestamp") for code in codes]
        with mock.patch.object(dfc, "fetch_tencent_realtime_for_codes", return_value=rows):
            self.assertEqual(
                dfc.fetch_tencent_market_snapshot(codes, max_age=240, now=NOW), []
            )
        self.assertFalse(os.path.exists(self._cache))

    def test_code_list_below_the_formal_floor_is_rejected(self):
        with mock.patch.object(dfc, "fetch_tencent_realtime_for_codes") as fetch:
            self.assertEqual(
                dfc.fetch_tencent_market_snapshot(_codes(10), max_age=240, now=NOW), []
            )
        fetch.assert_not_called()

    def test_fresh_fraction_without_absolute_floor_preserves_old_snapshot(self):
        codes = _codes(4400)
        rows = [_TencentRow.build(c, quote_at="20261014103000" if i < 3960 else "20261013093000")
                for i,c in enumerate(codes)]
        with open(self._cache, "w", encoding="utf-8") as handle:
            handle.write("previous-version")
        with mock.patch.object(dfc, "fetch_tencent_realtime_for_codes", return_value=rows):
            self.assertEqual([], dfc.fetch_tencent_market_snapshot(codes, max_age=240, now=NOW, force=True))
        with open(self._cache, encoding="utf-8") as handle:
            self.assertEqual("previous-version", handle.read())

    def test_future_drift_over_120_seconds_is_not_published(self):
        rows = [_TencentRow.build(c, quote_at="20261014103201") for c in _codes(4100)]
        with mock.patch.object(dfc, "fetch_tencent_realtime_for_codes", return_value=rows):
            self.assertEqual([], dfc.fetch_tencent_market_snapshot(_codes(4100), max_age=240, now=NOW))
        self.assertFalse(os.path.exists(self._cache))

    def test_nonfinite_percentage_is_not_published(self):
        rows = [_TencentRow.build(c, pct=float("nan")) for c in _codes(4100)]
        with mock.patch.object(dfc, "fetch_tencent_realtime_for_codes", return_value=rows):
            self.assertEqual([], dfc.fetch_tencent_market_snapshot(_codes(4100), max_age=240, now=NOW))
        self.assertFalse(os.path.exists(self._cache))

    def test_nested_publisher_does_not_reacquire_its_file_lock(self):
        with dfc._full_snapshot_singleflight_lock():
            with mock.patch.object(dfc.MDC, "full_snapshot_singleflight_lock",
                                   side_effect=AssertionError("nested OS lock")):
                dfc._publish_full_snapshot({"rows": []})



class TencentSnapshotContractTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._cache = os.path.join(self._dir, "market_snapshot_full.json")
        patcher = mock.patch.object(dfc, "MARKET_SNAPSHOT_FULL_CACHE_PATH", self._cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(dfc, "_full_market_min_rows", lambda: 4000)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(MDSvc, "now_utc", lambda: NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cleanup(self):
        for name in os.listdir(self._dir):
            os.unlink(os.path.join(self._dir, name))
        os.rmdir(self._dir)

    def _publish(self, rows, *, source):
        payload = {
            "saved_at": NOW.isoformat(),
            "source": source,
            "rows": rows,
            "complete": True,
            "expected_rows": len(rows),
        }
        with open(self._cache, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def _rows(self):
        return [{
            "code": f"{600000 + index:06d}", "price": 10.0, "pct": 1.0,
            "quote_at": NOW.isoformat(), "observed_at": NOW.isoformat(),
        } for index in range(4000)]

    def test_tencent_slice_is_degraded_never_fresh_or_cross_source(self):
        self._publish(self._rows(), source="tencent_public_quote_full_snapshot")
        reading = MDSvc.read_snapshot(now=NOW)
        self.assertTrue(reading.available)
        self.assertEqual(MDC.STATUS_DEGRADED, reading.status)
        self.assertEqual(MDC.REASON_PRICE_ONLY_COVERAGE, reading.reason)
        self.assertEqual(
            MDC.VERIFICATION_METHOD_NONE,
            reading.snapshot.verification_method,
        )
        self.assertFalse(MDC.is_cross_source_verified(reading.snapshot))

    def test_eastmoney_snapshot_still_classifies_as_fresh(self):
        self._publish(self._rows(), source="eastmoney_clist_full_snapshot")
        reading = MDSvc.read_snapshot(now=NOW)
        self.assertEqual(MDC.STATUS_FRESH, reading.status)

    def test_price_only_artifact_still_expires(self):
        self._publish(self._rows(), source=MDSvc.TENCENT_SNAPSHOT_SOURCE)
        reading = MDSvc.read_snapshot(now=NOW+dt.timedelta(seconds=MDC.LIVE_MARKET_POLICY.max_age_seconds+1))
        self.assertEqual(MDC.STATUS_STALE, reading.status)
        self.assertEqual(MDC.REASON_STALE, reading.reason)

    def test_price_only_refresh_failure_is_not_fresh(self):
        self._publish(self._rows(), source=MDSvc.TENCENT_SNAPSHOT_SOURCE)
        snapshot = MDSvc.read_snapshot(now=NOW).snapshot
        reading = MDC.classify(snapshot, MDC.LIVE_MARKET_POLICY, now=NOW, refresh_failed=True)
        self.assertEqual(MDC.REASON_REFRESH_FAILED, reading.reason)
        self.assertEqual(MDC.STATUS_STALE, reading.status)

    def test_missing_timestamps_count_against_coverage(self):
        rows = self._rows()
        for row in rows[:500]:
            row.pop("quote_at"); row.pop("observed_at")
        self._publish(rows, source=MDSvc.TENCENT_SNAPSHOT_SOURCE)
        self.assertEqual(MDC.STATUS_STALE, MDSvc.read_snapshot(now=NOW).status)

    def test_cached_future_time_cannot_bypass_the_120_second_guard(self):
        rows = self._rows()
        for row in rows:
            row["quote_at"] = row["observed_at"] = (NOW+dt.timedelta(seconds=121)).isoformat()
        self._publish(rows, source=MDSvc.TENCENT_SNAPSHOT_SOURCE)
        self.assertEqual(MDC.STATUS_STALE, MDSvc.read_snapshot(now=NOW).status)

    def test_invalid_tencent_coverage_is_not_marked_price_only_usable(self):
        self._publish(self._rows()[:100], source=MDSvc.TENCENT_SNAPSHOT_SOURCE)
        reading = MDSvc.read_snapshot(now=NOW)
        self.assertNotEqual(MDC.FRESHNESS_FRESH, reading.freshness)
        self.assertFalse(MDC.is_cross_source_verified(reading.snapshot))



class SameSourceCrossCheckTests(unittest.TestCase):
    """主报价与核验源同源时不得记为双源通过。"""

    def setUp(self):
        patchers = (
            mock.patch.object(PT, "_latest_price_map", lambda _codes: {}),
            mock.patch.object(PT, "_universe_snapshot_time", lambda: None),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_tencent_primary_never_uses_tencent_as_cross_source(self):
        # The cross-source time gate accepts a live source instant; a fixed
        # stamp would fail on wall-clock freshness, not on source identity.
        quote_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(
            timespec="seconds"
        )
        tencent_row = {
            "code": "600001", "price": 10.0, "pct": 1.0, "quote_at": quote_at,
            "source": "tencent_public_quote",
        }
        sina_row = {
            "code": "600001", "price": 10.0, "pct": 1.0, "quote_at": quote_at,
            "source": "sina_public_quote",
        }
        with mock.patch.object(PT.dfc, "fetch_realtime_for_codes",
                               return_value=[tencent_row]), \
                mock.patch.object(PT.dfc, "fetch_independent_realtime_for_codes",
                                  return_value=[tencent_row]), \
                mock.patch.object(PT.dfc, "_fetch_sina_realtime_for_codes",
                                  return_value=[sina_row]) as sina:
            row = PT._quotes(["600001"], asof_date=dt.date.today())["600001"]
        sina.assert_called_once_with(["600001"])
        self.assertEqual("sina_public_quote", row["quote_cross_check"]["source"])
        self.assertEqual("cross_source_checked", row["quote_validation"])

    def test_sina_failure_cannot_leave_a_tencent_self_check(self):
        stamp = dt.datetime.now(TZ).isoformat(timespec="seconds")
        row = {"code":"600001", "price":10., "pct":1., "quote_at":stamp, "source":"tencent_public_quote"}
        for failure in ([], RuntimeError("offline"), [row]):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(PT.dfc,"fetch_realtime_for_codes",return_value=[row]), \
                    mock.patch.object(PT.dfc,"fetch_independent_realtime_for_codes",return_value=[row]), \
                    mock.patch.object(PT.dfc,"enrich_live_flow_details",return_value={}), \
                    mock.patch.object(PT.PLTrad,"prefetch",return_value={}), \
                    mock.patch.object(PT.dfc,"_fetch_sina_realtime_for_codes",
                                      **({"side_effect":failure} if isinstance(failure,Exception) else {"return_value":failure})):
                actual=PT._quotes(["600001"],asof_date=dt.date.today())["600001"]
            self.assertNotEqual("cross_source_checked",actual["quote_validation"])
            self.assertFalse(actual["quote_cross_check"]["passed"])

    def test_price_only_owner_can_supply_primary_but_requires_sina(self):
        stamp=dt.datetime.now(TZ).isoformat(timespec="seconds")
        row={"code":"600001","price":10.,"pct":1.,"quote_at":stamp,"source":"tencent_public_quote"}
        missing_fields = ("industry", "pe", "vol_ratio", "amount", "volume", "turnover")
        row.update(dict.fromkeys(missing_fields))
        old = {field: "old industry" if field == "industry" else 123.0
               for field in missing_fields}
        snapshot=MDSvc._full_market_snapshot(rows=[row],observed_at=stamp,saved_at=stamp,
                                            complete=True,expected_rows=1,kind=MDSvc.KIND_FULL_MARKET_SNAPSHOT,
                                            source=MDSvc.TENCENT_SNAPSHOT_SOURCE)
        reading=MDC.classify(snapshot,MDC.LIVE_MARKET_POLICY,now=dt.datetime.now(TZ))
        with mock.patch.object(PT.dfc,"fetch_realtime_for_codes",return_value=[]), \
                mock.patch.object(PT.MDSvc,"read_snapshot",return_value=reading), \
                mock.patch.object(PT,"_latest_price_map",return_value={"600001":old}), \
                mock.patch.object(PT.dfc,"enrich_live_flow_details",return_value={}), \
                mock.patch.object(PT.dfc,"fetch_independent_realtime_for_codes",return_value=[row]), \
                mock.patch.object(PT.dfc,"_fetch_sina_realtime_for_codes",return_value=[{**row,"source":"sina_public_quote"}]), \
                mock.patch.object(PT.dfc,"enrich_live_flow_details",return_value={}), \
                mock.patch.object(PT.PLTrad,"prefetch",return_value={}):
            actual=PT._quotes(["600001"],asof_date=dt.date.today())["600001"]
        self.assertEqual("cross_source_checked",actual["quote_validation"])
        self.assertEqual("price_only_full_market_snapshot",actual["primary_quote_origin"])
        for field in missing_fields:
            self.assertIsNone(actual.get(field), field)



class RefreshFallbackTests(unittest.TestCase):
    """东财失败时腾讯回退；腾讯也失败时保留旧事实而不是伪造新横截面。"""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._cache = os.path.join(self._dir, "market_snapshot_full.json")
        patcher = mock.patch.object(dfc, "MARKET_SNAPSHOT_FULL_CACHE_PATH", self._cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(dfc, "_full_market_min_rows", lambda: 4000)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(MDSvc, "now_utc", lambda: NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cleanup(self):
        for name in os.listdir(self._dir):
            os.unlink(os.path.join(self._dir, name))
        os.rmdir(self._dir)

    def test_refresh_falls_back_to_tencent_when_primary_is_empty(self):
        codes = _codes(4100)
        rows = [_TencentRow.build(code) for code in codes]
        with mock.patch.object(dfc, "fetch_market_snapshot_full", return_value=[]), \
                mock.patch.object(dfc, "fetch_tencent_realtime_for_codes", return_value=rows), \
                mock.patch("universe.load_universe", return_value=[{"code": code} for code in codes]):
            reading = MDSvc.refresh_snapshot(now=NOW)
        self.assertTrue(reading.available)
        self.assertEqual(MDC.STATUS_DEGRADED, reading.status)
        self.assertEqual(4100, reading.snapshot.row_count)
        self.assertFalse(MDC.is_cross_source_verified(reading.snapshot))

    def test_refresh_keeps_last_known_when_both_sources_fail(self):
        payload = {
            "saved_at": NOW.isoformat(),
            "source": "eastmoney_clist_full_snapshot",
            "rows": [{
                "code": f"{600000 + index:06d}", "price": 10.0, "pct": 1.0,
                "quote_at": NOW.isoformat(), "observed_at": NOW.isoformat(),
            } for index in range(4000)],
            "complete": True,
            "expected_rows": 4000,
        }
        with open(self._cache, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with mock.patch.object(dfc, "fetch_market_snapshot_full", return_value=[]), \
                mock.patch.object(dfc, "fetch_tencent_market_snapshot", return_value=[]):
            reading = MDSvc.refresh_snapshot(now=NOW)
        self.assertEqual(MDC.STATUS_STALE, reading.status)
        self.assertEqual(MDC.REASON_REFRESH_FAILED, reading.reason)
        self.assertEqual(4000, reading.snapshot.row_count)

    def test_refresh_reports_unavailable_when_there_is_no_last_known_fact(self):
        with mock.patch.object(dfc, "fetch_market_snapshot_full", return_value=[]), \
                mock.patch.object(dfc, "fetch_tencent_market_snapshot", return_value=[]):
            reading = MDSvc.refresh_snapshot(now=NOW)
        self.assertEqual(MDC.AVAILABILITY_UNAVAILABLE, reading.availability)
        self.assertIsNone(reading.snapshot)

    def test_other_writer_cannot_relabel_tencent_rows_as_eastmoney(self):
        rows=[{**_TencentRow.build(c),"quote_at":NOW.isoformat()} for c in _codes(4100)]
        payload={"rows":[{**r,"source":"eastmoney"} for r in rows],"complete":True,
                 "expected_rows":4100,"saved_at":NOW.isoformat()}
        with mock.patch.object(dfc,"fetch_market_snapshot_full",return_value=[]), \
                mock.patch.object(dfc,"fetch_tencent_market_snapshot",return_value=rows), \
                mock.patch.object(dfc,"load_market_snapshot_full_payload",return_value=payload):
            reading=MDSvc.refresh_snapshot(now=NOW)
        self.assertEqual(MDSvc.TENCENT_SNAPSHOT_SOURCE,reading.snapshot.source)
        self.assertEqual(MDC.STATUS_DEGRADED,reading.status)
        self.assertFalse(MDC.is_cross_source_verified(reading.snapshot))



class PublishedSnapshotProvenanceTests(unittest.TestCase):
    """降级必须可见：审计里要能看出实际服务方与资金流字段是否缺失。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmp.name, "market_snapshot_full.json")
        self._patch = mock.patch.object(dfc, "MARKET_SNAPSHOT_FULL_CACHE_PATH", self._path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, payload):
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_price_only_fallback_is_reported_as_such(self):
        self._write({"source": MDSvc.TENCENT_SNAPSHOT_SOURCE, "saved_at": NOW.isoformat(),
                     "complete": True, "rows": _codes(4100), "expected_rows": 4100})
        provenance = dfc.full_market_snapshot_source()
        self.assertEqual(provenance["source"], MDSvc.TENCENT_SNAPSHOT_SOURCE)
        self.assertTrue(provenance["price_only"])
        self.assertEqual(provenance["rows"], 4100)

    def test_eastmoney_snapshot_is_not_reported_as_price_only(self):
        self._write({"source": "eastmoney-clist", "saved_at": NOW.isoformat(),
                     "complete": True, "rows": _codes(10), "expected_rows": 10})
        provenance = dfc.full_market_snapshot_source()
        self.assertEqual(provenance["source"], "eastmoney-clist")
        self.assertFalse(provenance["price_only"])

    def test_missing_or_unreadable_payload_reports_nothing(self):
        self.assertIsNone(dfc.full_market_snapshot_source())
        with open(self._path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(dfc.full_market_snapshot_source())

    def test_flow_field_coverage_separates_price_only_rows(self):
        price_only = [{**_TencentRow.build(code)} for code in _codes(20)]
        for row in price_only:
            row.pop("main_net", None)
            row.pop("vol_ratio", None)
        self.assertEqual(0.0, MDC.flow_field_coverage(price_only))

        with_flow = [dict(row, main_net=1.0, vol_ratio=1.5) for row in price_only]
        self.assertEqual(1.0, MDC.flow_field_coverage(with_flow))

        mixed = with_flow[:10] + price_only[:10]
        self.assertEqual(0.5, MDC.flow_field_coverage(mixed))
        self.assertEqual(0.0, MDC.flow_field_coverage([]))
        self.assertEqual(0.0, MDC.flow_field_coverage(None))


class HealthProjectionProvenanceTests(unittest.TestCase):
    """健康响应必须能直接读出服务方、是否纯价格、以及资金流字段覆盖率。"""

    @staticmethod
    def _rows(count, *, with_flow):
        rows = []
        for code in _codes(count):
            row = {"code": code, "name": f"股票{code}", "price": 10.0,
                   "prev_close": 9.9, "pct": 1.0, "quote_at": NOW.isoformat()}
            if with_flow:
                row["main_net"] = 1.0
                row["vol_ratio"] = 1.2
            rows.append(row)
        return rows

    def _project(self, source, rows):
        payload = {"source": source, "saved_at": NOW.isoformat(), "complete": True,
                   "expected_rows": len(rows), "rows": rows}
        with mock.patch.object(dfc, "load_market_snapshot_full_payload", return_value=payload), \
                mock.patch.object(dfc, "load_source_health", return_value={"healthy": True}):
            return MDSvc.market_health_projection(now=NOW)

    def test_price_only_fallback_is_projected_with_its_source_and_no_flow(self):
        projection = self._project(MDSvc.TENCENT_SNAPSHOT_SOURCE, self._rows(4100, with_flow=False))
        meta = projection["snapshot_meta"]
        self.assertEqual(MDSvc.TENCENT_SNAPSHOT_SOURCE, meta["source"])
        self.assertTrue(meta["price_only"])
        self.assertEqual(0.0, meta["flow_field_coverage"])
        self.assertEqual(MDC.STATUS_DEGRADED, projection["data_fact"]["status"])
        self.assertEqual(MDC.REASON_PRICE_ONLY_COVERAGE, projection["data_fact"]["reason"])

    def test_cross_section_reports_source_and_full_flow_coverage(self):
        projection = self._project("eastmoney-clist", self._rows(4100, with_flow=True))
        meta = projection["snapshot_meta"]
        self.assertEqual("eastmoney-clist", meta["source"])
        self.assertFalse(meta["price_only"])
        self.assertEqual(1.0, meta["flow_field_coverage"])


if __name__ == "__main__":
    unittest.main()
