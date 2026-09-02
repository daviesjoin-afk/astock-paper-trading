# -*- coding: utf-8 -*-
"""Regression coverage for fixed-capital shared-pool late strategy joins."""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import paper_trading as P  # noqa: E402


class SharedPoolLateJoinTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE paper_cycles(
                id INTEGER PRIMARY KEY, cycle_key TEXT, status TEXT,
                capital REAL, updated_at TEXT
            );
            CREATE TABLE paper_accounts(
                id TEXT PRIMARY KEY, cycle_id INTEGER, initial_cash REAL,
                cash REAL, params TEXT, updated_at TEXT
            );
            CREATE TABLE paper_fills(
                id INTEGER PRIMARY KEY, account_id TEXT, side TEXT,
                amount REAL, fees REAL
            );
            CREATE TABLE paper_nav(
                account_id TEXT, nav_date TEXT, nav REAL
            );
            CREATE TABLE paper_audit(
                id INTEGER PRIMARY KEY, account_id TEXT, event TEXT,
                detail TEXT, created_at TEXT
            );
            """
        )
        self.conn.execute(
            "INSERT INTO paper_cycles VALUES(1,'cycle-fixed','running',300000,'2026-08-31 00:00:00')"
        )
        rows = [
            ("a", 1, 75000.0, 59223.15881905, "{}", ""),
            ("b", 1, 75000.0, 20399.19862270, "{}", ""),
            ("c", 1, 75000.0, 25411.77654605, "{}", ""),
            ("d", 1, 75000.0, 10569.56508060, "{}", ""),
            (
                P.MAIN_FORCE_STRATEGY_ID, 1, 0.0, 75000.0,
                '{"shared_pool_reference_capital":60000.0}', "",
            ),
        ]
        self.conn.executemany("INSERT INTO paper_accounts VALUES(?,?,?,?,?,?)", rows)

    def tearDown(self):
        self.conn.close()

    def test_late_join_has_no_unallocated_economic_capital(self):
        cycle = dict(self.conn.execute("SELECT * FROM paper_cycles WHERE id=1").fetchone())
        self.assertEqual(
            0.0,
            P._available_cycle_ledger_capital(self.conn, cycle, P.MAIN_FORCE_STRATEGY_ID),
        )
        self.assertEqual(300000.0, P._shared_initial_cash(self.conn, cycle))

    def test_reconcile_removes_minted_cash_against_cycle_capital(self):
        self.conn.execute(
            "INSERT INTO paper_fills(account_id,side,amount,fees) VALUES(?,?,?,?)",
            (P.MAIN_FORCE_STRATEGY_ID, "buy", 184377.86, 18.4409316),
        )
        drift = P._reconcile_shared_cash(self.conn, 1)
        cash = self.conn.execute("SELECT SUM(cash) FROM paper_accounts WHERE cycle_id=1").fetchone()[0]
        self.assertAlmostEqual(-75000.0, drift, places=2)
        self.assertAlmostEqual(115603.6990684, cash, places=6)
        self.assertAlmostEqual(
            0.0,
            self.conn.execute(
                "SELECT cash FROM paper_accounts WHERE id=?", (P.MAIN_FORCE_STRATEGY_ID,)
            ).fetchone()[0],
            places=6,
        )

    def test_pool_history_excludes_late_join_reference_deposit(self):
        self.conn.executemany(
            "INSERT INTO paper_nav(account_id,nav_date,nav) VALUES(?,?,?)",
            [
                ("a", "2026-08-28", 74618.37),
                ("b", "2026-08-28", 67405.48),
                ("c", "2026-08-28", 71580.93),
                ("d", "2026-08-28", 73025.79),
                ("a", "2026-08-31", 74802.16),
                ("b", "2026-08-31", 65388.12),
                ("c", "2026-08-31", 72916.81),
                ("d", "2026-08-31", 73318.67),
                (P.MAIN_FORCE_STRATEGY_ID, "2026-08-31", 64281.94),
            ],
        )
        cycle = dict(self.conn.execute("SELECT * FROM paper_cycles WHERE id=1").fetchone())
        history = P._economic_pool_nav_history(self.conn, cycle)
        self.assertEqual(["2026-08-28", "2026-08-31"], [row["nav_date"] for row in history])
        self.assertAlmostEqual(286630.57, history[0]["nav"], places=2)
        self.assertAlmostEqual(290707.70, history[1]["nav"], places=2)
        self.assertLess(history[1]["nav"] - history[0]["nav"], 10000.0)


if __name__ == "__main__":
    unittest.main()
