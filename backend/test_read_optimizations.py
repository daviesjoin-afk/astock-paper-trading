# -*- coding: utf-8 -*-
"""Offline regression tests for paper read-model simplifications."""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import paper_trading as P  # noqa: E402


class AccountMetricBatchTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE paper_orders(
                id INTEGER PRIMARY KEY, account_id TEXT, code TEXT, qty INTEGER,
                filled_price REAL, amount REAL, fees REAL, status TEXT,
                realized_pnl REAL, created_at TEXT, executed_at TEXT, side TEXT
            );
            CREATE TABLE paper_fills(
                id INTEGER PRIMARY KEY, order_id INTEGER, account_id TEXT,
                side TEXT, code TEXT, qty INTEGER, price REAL, amount REAL,
                fees REAL, fill_date TEXT
            );
            CREATE TABLE paper_nav(
                account_id TEXT, nav_date TEXT, nav REAL, benchmark REAL,
                quote_status TEXT, created_at TEXT
            );
            """
        )
        self.account_id = next(iter(P.ACCOUNT_SPECS))
        self.account = {
            "id": self.account_id, "name": "test", "status": "running",
            "initial_cash": 100000.0, "cash": 100000.0, "cycle_days": 5,
            "mode": "swing", "style": "pullback", "risk_profile": "trend",
            "version": "test", "params": "{}", "benchmark_start": 100.0,
        }
        self.conn.executemany(
            "INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, self.account_id, "000001", 100, 10.0, 1000.0, 5.0,
                 "filled", 42.5, "2026-08-20 10:00:00", "2026-08-20 10:00:01", "sell"),
                (2, self.account_id, "000002", 100, 11.0, 1100.0, 5.0,
                 "risk_rejected", None, "2026-08-20 10:01:00", None, "buy"),
            ],
        )
        self.conn.execute(
            "INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?,?,?)",
            (3, 1, self.account_id, "buy", "000001", 100, 9.5, 950.0, 5.0, "2026-08-19"),
        )
        self.conn.executemany(
            "INSERT INTO paper_nav VALUES(?,?,?,?,?,?)",
            [
                (self.account_id, "2026-08-19", 100000.0, 100.0, "verified", "2026-08-19"),
                (self.account_id, "2026-08-20", 100042.5, 101.0, "verified", "2026-08-20"),
            ],
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_batched_inputs_match_uncached_account_metrics(self):
        cache = P._account_metric_inputs(self.conn, [self.account_id], "2026-08-21")
        with mock.patch.object(P, "_position_rows", return_value=[]), \
                mock.patch.object(P, "_market_session", return_value={
                    "today_pnl_available": False, "label": "盘前", "code": "preopen",
                }):
            uncached = P._account_metrics(
                self.conn, self.account, quotes={}, positions=[], metric_cache=None,
            )
            batched = P._account_metrics(
                self.conn, self.account, quotes={}, positions=[], metric_cache=cache,
            )
        for key in ("trade_count", "risk_blocks", "realized_pnl", "total_pnl",
                    "nav", "max_drawdown_pct", "today_pnl"):
            self.assertEqual(batched[key], uncached[key], key)


class ApiCacheSingleFlightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import api_paper as api
        except ModuleNotFoundError as exc:
            if exc.name != "fastapi":
                raise unittest.SkipTest(f"API dependencies unavailable: {exc.name}")
            # The bundled offline test runtime intentionally omits FastAPI.
            # A tiny decorator-compatible stub still exercises the cache
            # implementation without starting an HTTP server.
            class Router:
                def __init__(self, **_kwargs):
                    pass

                def get(self, *_args, **_kwargs):
                    return lambda fn: fn
                post = get

            fastapi = types.ModuleType("fastapi")
            fastapi.APIRouter = Router
            fastapi.HTTPException = type("HTTPException", (Exception,), {})
            fastapi.Query = lambda default=None, **_kwargs: default
            responses = types.ModuleType("fastapi.responses")
            responses.JSONResponse = lambda content=None, **_kwargs: content
            sys.modules["fastapi"] = fastapi
            sys.modules["fastapi.responses"] = responses
            import api_paper as api
        cls.api = api

    def test_same_key_runs_loader_once(self):
        api = self.api
        api._cclear()
        calls = 0
        calls_lock = threading.Lock()

        def loader():
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return {"ok": True}

        with mock.patch.object(api.P, "paper_cache_generation", return_value="g1"):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(
                    lambda _item: api._cache_load("single-flight", loader, ttl=60),
                    range(8),
                ))
        self.assertEqual(calls, 1)
        self.assertEqual(results, [{"ok": True}] * 8)
        api._cclear()

    def test_manual_risk_refresh_uses_backend_worker(self):
        api = self.api
        with mock.patch.object(api, "_cclear") as clear, \
                mock.patch.object(api.P, "request_risk_snapshot_refresh",
                                  return_value={"status": "scheduled"}) as request:
            result = api.risk_refresh()
            request.call_args.kwargs["on_complete"]()
        self.assertEqual(result["status"], "scheduled")
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["trigger"], "manual")
        self.assertGreaterEqual(clear.call_count, 2)


class CompactRiskReadModelTests(unittest.TestCase):
    def test_risk_dashboard_does_not_build_full_overview(self):
        base = {"accounts": [], "positions": [], "shared": {}, "risk_decisions": []}
        snapshot = {"asof": "2026-08-25T10:00:00", "market": {}, "news": {},
                    "data_quality": [], "dynamic_risk": {}, "sector_rows": []}
        with mock.patch.object(P, "_risk_base_dashboard", return_value=base), \
                mock.patch.object(P, "dashboard", side_effect=AssertionError("full dashboard")), \
                mock.patch.object(P.RC, "load_snapshot", return_value=snapshot), \
                mock.patch.object(P.RC, "snapshot_age_seconds", return_value=1), \
                mock.patch.object(P.RC, "build_dashboard", return_value={}), \
                mock.patch.object(P.dfc, "load_source_health", return_value={}):
            result = P.risk_dashboard()
        self.assertEqual(result["capital_model"], "shared_pool")


if __name__ == "__main__":
    unittest.main()
