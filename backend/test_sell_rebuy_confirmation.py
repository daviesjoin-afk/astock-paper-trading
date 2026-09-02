"""Regression tests for the independent high-sell rebuy confirmation lane."""
from __future__ import annotations

import sqlite3
import unittest

import paper_trading as P


class SellRebuyConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE paper_intraday_observations(
                id INTEGER PRIMARY KEY, account_id TEXT, code TEXT,
                price REAL, observed_at TEXT
            )"""
        )
        self.account = {"id": "tq_breakout"}
        self.sold = {
            "id": 10,
            "code": "000001",
            "payload": P._json({"sell_price": 10.0, "qty": 200, "opening_event": False}),
        }

    def tearDown(self):
        self.conn.close()

    def _observe(self, *prices):
        for offset, price in enumerate(prices, start=11):
            self.conn.execute(
                "INSERT INTO paper_intraday_observations VALUES(?,?,?,?,?)",
                (offset, "tq_breakout", "000001", price, "2026-09-02 10:00:00"),
            )

    def test_rebuy_needs_discount_low_rebound_and_nonnegative_flow(self):
        self._observe(9.80, 9.88)
        result = P._sell_rebuy_confirmation(
            self.conn, self.sold,
            {"price": 9.90, "pct": -0.5, "main_pct": -0.1},
            "2026-09-02", self.account,
        )
        self.assertTrue(result["passed"], result)
        self.assertEqual("sell_rebuy_confirmation_v2", result["model"])
        self.assertEqual(2, result["rebound_confirmations"])

    def test_rebuy_rejects_when_funds_remain_negative(self):
        self._observe(9.80, 9.88)
        result = P._sell_rebuy_confirmation(
            self.conn, self.sold,
            {"price": 9.90, "pct": -0.5, "main_pct": -0.3},
            "2026-09-02", self.account,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("主力净流入" in item for item in result["blockers"]))

    def test_rebuy_rejects_single_tick_rebound(self):
        self._observe(9.80)
        result = P._sell_rebuy_confirmation(
            self.conn, self.sold,
            {"price": 9.90, "pct": -0.5, "main_pct": 0.2},
            "2026-09-02", self.account,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("观察" in item or "持续" in item for item in result["blockers"]))


if __name__ == "__main__":
    unittest.main()
