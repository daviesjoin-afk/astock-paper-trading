# -*- coding: utf-8 -*-
"""Regression coverage for the transaction/risk re-audit packet.

These tests use only an in-memory lease table and deterministic quote helpers;
they do not touch the paper-trading database or any market-data provider.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import paper_trading as P  # noqa: E402


class LeaseAndFreshnessTests(unittest.TestCase):
    @staticmethod
    def _lease_conn():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE paper_runtime_locks(
                lock_key TEXT PRIMARY KEY, owner_key TEXT NOT NULL,
                slot TEXT NOT NULL, acquired_at TEXT NOT NULL,
                heartbeat_at TEXT, expires_at TEXT NOT NULL,
                fencing_token INTEGER NOT NULL DEFAULT 0
            )"""
        )
        return conn

    def test_lease_generation_increments_and_stale_owner_is_fenced(self):
        conn = self._lease_conn()
        self.addCleanup(conn.close)
        first, owner1, _ = P._claim_runtime_lease(
            conn, "test-lock", "worker-a", "risk", ttl_seconds=60
        )
        self.assertTrue(first)
        token1 = conn.execute(
            "SELECT fencing_token FROM paper_runtime_locks WHERE lock_key='test-lock'"
        ).fetchone()[0]
        self.assertEqual(token1, 1)
        self.assertFalse(P._claim_runtime_lease(
            conn, "test-lock", "worker-b", "risk", ttl_seconds=60
        )[0])

        conn.execute(
            "UPDATE paper_runtime_locks SET expires_at='2000-01-01 00:00:00' "
            "WHERE lock_key='test-lock'"
        )
        second, owner2, _ = P._claim_runtime_lease(
            conn, "test-lock", "worker-b", "risk", ttl_seconds=60
        )
        self.assertTrue(second)
        token2 = conn.execute(
            "SELECT owner_key,fencing_token FROM paper_runtime_locks WHERE lock_key='test-lock'"
        ).fetchone()
        self.assertNotEqual(owner1, owner2)
        self.assertEqual(token2[1], token1 + 1)

        P._set_lease_context("test-lock", owner2, token2[1])
        P._assert_active_lease(conn, "test")
        conn.execute(
            "UPDATE paper_runtime_locks SET owner_key='new-owner',fencing_token=? "
            "WHERE lock_key='test-lock'",
            (token2[1] + 1,),
        )
        with self.assertRaisesRegex(RuntimeError, "paper lease lost"):
            P._assert_active_lease(conn, "stale worker")
        P._clear_lease_context()

    def test_intraday_key_is_three_minute_bucket(self):
        self.assertEqual(
            P._intraday_business_key(dt.datetime(2026, 8, 25, 13, 2, 59)),
            "intraday:202608251300",
        )
        self.assertEqual(
            P._intraday_business_key(dt.datetime(2026, 8, 25, 13, 3, 0)),
            "intraday:202608251303",
        )

    def test_today_pnl_quote_requires_fresh_live_mark(self):
        now = dt.datetime.now()
        fresh = {
            "price": 10.0,
            "quote_source": "live",
            "quote_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        stale = dict(fresh, quote_at=(now - dt.timedelta(minutes=21)).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertTrue(P._today_quote_is_usable(fresh, now.date()))
        self.assertFalse(P._today_quote_is_usable(stale, now.date()))


class ApiCacheGenerationTests(unittest.TestCase):
    def test_cache_is_invalidated_by_ledger_generation(self):
        # Import lazily so source-only checks can still collect this module in
        # a minimal environment that does not install FastAPI.
        import api_paper as API

        API._cclear()
        generations = iter(("g1", "g1", "g2"))
        with mock.patch.object(API.P, "paper_cache_generation", side_effect=lambda: next(generations)):
            API._cset("risk", {"version": 1})
            self.assertEqual(API._cget("risk", ttl=60), {"version": 1})
            self.assertIsNone(API._cget("risk", ttl=60))
        API._cclear()


if __name__ == "__main__":
    unittest.main()
