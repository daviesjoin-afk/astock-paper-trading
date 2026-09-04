# -*- coding: utf-8 -*-
"""Regression tests for the low-risk research/attribution simplifications."""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

try:
    import data_fetcher  # noqa: E402
except ModuleNotFoundError:
    # The attribution test only needs the loader seam; use a tiny module stub
    # when optional HTTP dependencies are absent from a source-only image.
    data_fetcher = types.ModuleType("data_fetcher")
    data_fetcher.load_cached_kline = lambda _code: None
    sys.modules["data_fetcher"] = data_fetcher
import trade_attribution as TA  # noqa: E402

try:
    import adaptive_engine as AE  # noqa: E402
except Exception:
    AE = None

try:
    import deepseek_research as DS  # noqa: E402
except Exception:
    # ZoneInfo/tzdata is supplied by the deployment image; skip suite tests
    # when running in a bare source-only Python installation.
    DS = None

try:
    import api_adaptive as API  # noqa: E402
except Exception:
    # Keep the source-level regression suite runnable in minimal CI images;
    # the API-specific assertion is exercised in the FastAPI deployment image.
    API = None


class _Series:
    def __init__(self, values):
        self._values = values

    def items(self):
        return self._values.items()


class _Frame:
    empty = False

    def __init__(self, values):
        self._series = _Series(values)

    def __contains__(self, key):
        return key == "close"

    def __getitem__(self, key):
        if key != "close":
            raise KeyError(key)
        return self._series


class AttributionCacheTests(unittest.TestCase):
    def test_daily_kline_is_loaded_once_and_future_bar_is_not_used(self):
        frame = _Frame({"2026-08-24": 10.0, "2026-08-25": 11.0, "2026-08-26": 99.0})
        cache = {}
        with mock.patch.object(TA, "_read_cached_closes", return_value=None), \
             mock.patch.object(data_fetcher, "load_cached_kline", return_value=frame) as load:
            self.assertEqual(
                TA._cached_close_on("000001", dt.date(2026, 8, 25), cache), 11.0
            )
            self.assertEqual(
                TA._cached_close_on("000001", dt.date(2026, 8, 25), cache), 11.0
            )
        load.assert_called_once_with("000001")
        self.assertIsNone(TA._cached_close_on("000001", dt.date(2026, 8, 27), cache))

    def test_kline_cache_is_compact_and_bounded(self):
        frame = _Frame({"2026-08-24": 10.0, "2026-08-25": 11.0})
        cache = {}
        with mock.patch.object(TA, "KLINE_CLOSE_CACHE_MAX", 2), \
             mock.patch.object(TA, "_read_cached_closes", return_value=None), \
             mock.patch.object(data_fetcher, "load_cached_kline", return_value=frame):
            TA._load_kline_once(TA.BENCHMARK_CACHE_KEY, cache)
            TA._load_kline_once("000001", cache)
            TA._load_kline_once("000002", cache)
        self.assertLessEqual(len(cache), 2)
        self.assertIn(TA.BENCHMARK_CACHE_KEY, cache)
        self.assertIsInstance(cache["000002"], dict)
        self.assertEqual(cache["000002"][dt.date(2026, 8, 25)], 11.0)

    def test_paper_order_query_does_not_materialize_full_risk_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "paper.sqlite3")
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE paper_orders(
                    id INTEGER PRIMARY KEY,account_id TEXT,side TEXT,code TEXT,name TEXT,qty INTEGER,
                    planned_price REAL,amount REAL,fees REAL,status TEXT,reason TEXT,risk_payload TEXT,
                    realized_pnl REAL,created_at TEXT,executed_at TEXT);
                CREATE TABLE paper_fills(
                    id INTEGER PRIMARY KEY,order_id INTEGER,price REAL,amount REAL,fees REAL,
                    fill_date TEXT,quote_at TEXT,assumption TEXT);
            """)
            payload = "x" * 500000
            conn.execute(
                "INSERT INTO paper_orders VALUES(1,'a','buy','000001','x',100,10,1000,1,'filled','r',?,0,?,?)",
                (payload, "2026-08-25T10:00:00+08:00", "2026-08-25T10:00:01+08:00"),
            )
            conn.commit(); conn.close()
            rows = TA._paper_orders(path, dt.date(2026, 8, 25))
        self.assertEqual(len(rows), 1)
        self.assertNotIn("risk_payload", rows[0])
        self.assertEqual(rows[0]["risk_payload_bytes"], len(payload))

    def test_completed_horizons_do_not_need_historical_rewrite(self):
        self.assertTrue(TA._all_horizons_mature(
            '{"1d":{"mature":true},"3d":{"mature":true},"5d":{"mature":true}}'
        ))
        self.assertFalse(TA._all_horizons_mature(
            '{"1d":{"mature":true},"3d":{"mature":true}}'
        ))


@unittest.skipIf(AE is None, "adaptive engine dependencies are not installed")
class AlphaLabBoundTests(unittest.TestCase):
    def test_window_sampling_is_bounded_and_deterministic(self):
        rows = [
            {"profile_date": "2026-08-31", "horizon": 1, "code": f"{i:06d}"}
            for i in range(250)
        ]
        first = AE._alpha_bounded_sample(rows, max_rows_per_window=100)
        second = AE._alpha_bounded_sample(list(reversed(rows)), max_rows_per_window=100)
        self.assertEqual(len(first), 100)
        self.assertEqual([row["code"] for row in first], [row["code"] for row in second])


class ResearchSuiteSnapshotTests(unittest.TestCase):
    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    @unittest.skipIf(DS is None, "DeepSeek research dependencies are not installed")
    def test_suite_collects_with_one_adaptive_connection_and_marks_each_projection(self):
        conn = self._Conn()
        factory = mock.Mock(return_value=conn)
        with mock.patch.object(DS.advisor, "ensure_schema"), \
             mock.patch.object(DS.advisor, "_now", return_value="2026-08-25T15:35:00+08:00"), \
             mock.patch.object(DS, "collect", side_effect=lambda purpose, _conn, _path: {"purpose": purpose}):
            snapshot_id, asof, evidence = DS._collect_suite_snapshot(factory, "paper.sqlite3")
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(snapshot_id)
        self.assertEqual(asof, "2026-08-25T15:35:00+08:00")
        self.assertEqual(set(evidence), set(DS.TASKS))
        for purpose, item in evidence.items():
            self.assertEqual(item["purpose"], purpose)
            self.assertEqual(item["_suite_snapshot"]["id"], snapshot_id)


class AdaptiveApiQuoteTests(unittest.TestCase):
    @unittest.skipIf(API is None, "FastAPI is not installed in this test image")
    def test_quote_metadata_is_explicit_when_source_has_no_timestamp(self):
        rows = [{"code": "000001", "price": 10.0, "source": "eastmoney"},
                {"code": "000001", "price": 10.1, "source": "eastmoney"}]
        metadata = API._quote_metadata(rows)
        self.assertEqual(metadata["source"], "eastmoney")
        self.assertEqual(metadata["coverage"]["unique_codes"], 1)
        self.assertIsNone(metadata["quote_asof"])


if __name__ == "__main__":
    unittest.main()
