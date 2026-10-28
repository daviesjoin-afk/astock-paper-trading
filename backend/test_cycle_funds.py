# -*- coding: utf-8 -*-
"""A4：资金读数必须绑定明确周期。

旧实现里 `_shared_risk_state` 永远读"当前周期"的初始资金；在多周期并存
（归档周期仍有未释放预占）时，风险基线会和订单所属周期对不上。这里把
显式周期的资金事实收敛到 `paper_shared_cash.cycle_funds`，
`paper_trading._cycle_funds` 只是它的 facade。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_shared_cash as PSC  # noqa: E402
import paper_trading as PT  # noqa: E402


class CycleFundsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE paper_accounts("
            "id TEXT PRIMARY KEY, cash REAL, initial_cash REAL, cycle_id INTEGER, "
            "status TEXT, updated_at TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, capital REAL, status TEXT)"
        )
        self.conn.executemany(
            "INSERT INTO paper_accounts(id,cash,initial_cash,cycle_id,status,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            [
                ("tq_breakout", 100.0, 1000.0, 7, "running", "old"),
                ("trend_pullback", 200.0, 2000.0, 7, "running", "old"),
                ("sector_rotation", 400.0, 4000.0, 6, "paused", "old"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO paper_cycles(id,capital,status) VALUES(?,?,?)",
            [(7, 3000.0, "running"), (6, 4000.0, "paused")],
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_explicit_cycle_reads_its_own_owners(self):
        funds = PSC.cycle_funds(
            self.conn, 7, builtin_scope=("tq_breakout", "trend_pullback",
                                         "sector_rotation", "reported_profit_breakout",
                                         "main_force_top10"),
            num_fn=PSC._default_num,
        )
        self.assertEqual(300.0, funds["cash"])
        self.assertEqual(3000.0, funds["initial_cash"])
        self.assertEqual(("tq_breakout", "trend_pullback"), funds["owner_ids"])

    def test_another_cycle_has_its_own_funds(self):
        funds = PSC.cycle_funds(
            self.conn, 6, builtin_scope=("tq_breakout", "trend_pullback",
                                         "sector_rotation", "reported_profit_breakout",
                                         "main_force_top10"),
            num_fn=PSC._default_num,
        )
        self.assertEqual(400.0, funds["cash"])
        self.assertEqual(4000.0, funds["initial_cash"])
        self.assertEqual(("sector_rotation",), funds["owner_ids"])

    def test_declared_capital_wins_over_row_sum(self):
        funds = PSC.cycle_funds(
            self.conn, 7, builtin_scope=("tq_breakout", "trend_pullback",
                                         "sector_rotation", "reported_profit_breakout",
                                         "main_force_top10"),
            declared_capital=1234.0, num_fn=PSC._default_num,
        )
        self.assertEqual(1234.0, funds["initial_cash"])

    def test_engine_facade_uses_the_declared_cycle_capital(self):
        funds = PT._cycle_funds(self.conn, 7)
        self.assertEqual(300.0, funds["cash"])
        self.assertEqual(3000.0, funds["initial_cash"])
        self.assertEqual(7, funds["cycle_id"])


class StatusVocabularyOwnerTests(unittest.TestCase):
    """A1 phase 2：状态词表只有一个 owner，引擎不得再抄一份。"""

    def test_engine_constants_come_from_the_owner(self):
        import entry_lifecycle as ELC
        self.assertEqual(ELC.ENTRY_FROZEN_WAITLIST_STATUS, PT.ENTRY_FROZEN_WAITLIST_STATUS)
        self.assertEqual(ELC.ENTRY_RETRY_SIGNAL_STATUSES, PT.ENTRY_RETRY_SIGNAL_STATUSES)
        self.assertEqual(ELC.ENTRY_RETRY_ORDER_STATUSES, PT.ENTRY_RETRY_ORDER_STATUSES)
        self.assertEqual(ELC.MANUAL_EXECUTION_RETRY_STATUS, PT.MANUAL_EXECUTION_RETRY_STATUS)
        self.assertEqual(ELC.STRATEGY_EXECUTION_RETRY_STATUS, PT.STRATEGY_EXECUTION_RETRY_STATUS)
        self.assertEqual(ELC.ENTRY_FREEZE_ENV, PT.ENTRY_FREEZE_ENV)

    def test_lot_size_and_pool_exposure_come_from_their_owners(self):
        import paper_shared_cash as PSC
        import paper_trading_rules as PTRules
        self.assertEqual(PTRules.LOT_SIZE, PT.LOT_SIZE)
        self.assertEqual(PSC.SHARED_POOL_MAX_EXPOSURE, PT.SHARED_POOL_MAX_EXPOSURE)


if __name__ == "__main__":
    unittest.main()
