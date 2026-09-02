"""Regression tests for capacity accounting of paper entry waitlists."""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import paper_trading as P  # noqa: E402


class CapacityWaitlistSlotTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE paper_orders(id INTEGER PRIMARY KEY,account_id TEXT,code TEXT,origin TEXT,side TEXT,status TEXT)"
        )

    def tearDown(self):
        self.conn.close()

    def _order(self, account, code, status):
        self.conn.execute(
            "INSERT INTO paper_orders(account_id,code,origin,side,status) VALUES(?,?,?,?,?)",
            (account, code, "strategy", "buy", status),
        )

    def test_waitlist_markers_do_not_consume_strategy_or_pool_slots(self):
        self._order("sector_rotation", "000001", "deferred_capacity")
        self._order("sector_rotation", "000002", P.ENTRY_FROZEN_WAITLIST_STATUS)
        self._order("sector_rotation", "000003", "superseded")
        self.assertEqual(P._pending_position_slots(self.conn, positions=[]), set())

    def test_executable_pending_orders_still_hold_one_distinct_slot(self):
        self._order("sector_rotation", "000001", "pending_limit")
        self._order("sector_rotation", "000001", P.STRATEGY_EXECUTION_RETRY_STATUS)
        self._order("trend_pullback", "000002", P.MANUAL_EXECUTION_RETRY_STATUS)
        self.assertEqual(
            P._pending_position_slots(self.conn, positions=[]),
            {("sector_rotation", "000001"), ("trend_pullback", "000002")},
        )

    def test_existing_position_is_not_counted_again_as_pending_slot(self):
        self._order("sector_rotation", "000001", "pending_limit")
        positions = [{"account_id": "sector_rotation", "code": "000001", "qty": P.LOT_SIZE}]
        self.assertEqual(P._pending_position_slots(self.conn, positions=positions), set())


if __name__ == "__main__":
    unittest.main()
