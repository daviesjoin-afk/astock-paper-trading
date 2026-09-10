# -*- coding: utf-8 -*-
"""PR-48 回归：cycle 经济所有权与执行资格分离。

钦定场景：
    Cycle [A(user), B(builtin)]，capital = 300000 → A/B 各 150000；
    lifecycle pause A；
    configure_capital = 500000；
    resume A。

断言：
    - sum(initial_cash) == 500000（A pause 时仍属于经济账本）；
    - A pause 期间无新 signal / order（执行资格剔除）；
    - A 的经济所有权不消失（_shared_account_rows / _shared_cash / NAV 含 A）；
    - resume 后不会凭空放大资本（账本合计仍是 500000）。
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import unittest

import paper_trading as PT
import strategy_registry as SR
import strategy_runtime as SRT
import test_production_path_golden_replay as G


class CycleLedgerOwnershipTests(G.OfflinePaperEnv, unittest.TestCase):
    def setUp(self):
        self._db_index = getattr(self.__class__, "_db_seq", 0)
        self.__class__._db_seq = self._db_index + 1
        PT.DB_PATH = os.path.join(self._tmp, f"paper_trading_{self._db_index}.sqlite3")
        SRT.clear_cache()
        PT.init_db()

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(PT.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _activate_user(self, strategy_id):
        with self._conn() as conn:
            SR.ensure_schema(conn)
            SR.create_user_definition(
                conn, strategy_id, "账本所有权测试 " + strategy_id, dsl_ast=G.RULE,
                metadata={"candidate_topn": 10, "style": "trend", "hold": 8},
                actor="pr48-test",
            )
            SR.transition(conn, strategy_id, "validated", expected_status="draft",
                          reason="validate", actor="pr48-test")
            SR.transition(conn, strategy_id, "active", expected_status="validated",
                          reason="activate", actor="pr48-test")

    def _lifecycle_pause(self, strategy_id):
        with self._conn() as conn:
            SR.transition(conn, strategy_id, "paused", expected_status="active",
                          reason="pr48 lifecycle pause", actor="pr48-test")

    def _lifecycle_resume(self, strategy_id):
        with self._conn() as conn:
            SR.transition(conn, strategy_id, "active", expected_status="paused",
                          reason="pr48 lifecycle resume", actor="pr48-test")

    def _start_cycle_with(self, strategy_ids):
        with self._conn() as conn:
            import runtime_settings as RSET
            RSET.update(conn, {"enabled_strategies": list(strategy_ids)}, actor="pr48-test")
        PT.init_db()
        _summary, cycle = PT.start_new_cycle(
            capital=300000.0, include_dashboard=False,
        )
        return cycle

    def _count(self, table, where="1=1", params=()):
        with self._conn() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", params
            ).fetchone()[0]

    def test_pause_keeps_economic_ownership_and_execution_separates(self):
        # 1) Cycle [A(user), tq_breakout]，各得 150000。
        self._activate_user("ledger_user_a")
        cycle = self._start_cycle_with(["ledger_user_a", "tq_breakout"])
        with self._conn() as conn:
            ledger = PT.cycle_ledger_ids(conn, cycle["id"])
            self.assertIn("ledger_user_a", ledger)
            self.assertIn("tq_breakout", ledger)
            rows = {
                row["id"]: row
                for row in PT._shared_account_rows(conn, cycle["id"])
            }
            self.assertAlmostEqual(
                150000.0, float(rows["ledger_user_a"]["initial_cash"]), delta=1.0,
            )
            self.assertAlmostEqual(
                150000.0, float(rows["tq_breakout"]["initial_cash"]), delta=1.0,
            )
            # 两个 resolver 的口径差异：执行 = 经济 − lifecycle paused。
            self.assertEqual(
                set(PT.cycle_ledger_ids(conn, cycle["id"])),
                set(PT.execution_participant_ids(conn, cycle["id"])),
            )
        # 2) lifecycle pause A → 执行资格剔除，经济账本保留。
        self._lifecycle_pause("ledger_user_a")
        with self._conn() as conn:
            self.assertNotIn(
                "ledger_user_a", PT.execution_participant_ids(conn, cycle["id"]),
            )
            self.assertIn(
                "ledger_user_a", PT.cycle_ledger_ids(conn, cycle["id"]),
            )
            ledger_rows = {r["id"] for r in PT._shared_account_rows(conn, cycle["id"])}
            self.assertIn("ledger_user_a", ledger_rows)
            cash = PT._shared_cash(conn, cycle["id"])
            self.assertAlmostEqual(300000.0, cash, delta=2.0)
        # 3) pause 期间扫描一轮：A 无新 signal/order。
        close_result = PT.generate_signals(G.D0)
        self.assertNotEqual(close_result.get("status"), "failed", close_result)
        self.assertEqual(0, self._count(
            "paper_signals", "account_id=?", ("ledger_user_a",),
        ))
        self.assertEqual(0, self._count(
            "paper_orders", "account_id=?", ("ledger_user_a",),
        ))
        # 4) 周期暂停后重配资金（configure 要求非 running）：A/B 都必须拿到
        # 份额——A 已 lifecycle pause，但经济所有权不丢。
        PT.set_accounts_status("paused")
        PT.configure_capital(500000.0)
        with self._conn() as conn:
            total = conn.execute(
                "SELECT COALESCE(SUM(initial_cash),0) FROM paper_accounts WHERE cycle_id=?",
                (cycle["id"],),
            ).fetchone()[0]
            self.assertAlmostEqual(500000.0, float(total), delta=2.0)
            a_cash = conn.execute(
                "SELECT initial_cash FROM paper_accounts WHERE id='ledger_user_a'"
            ).fetchone()[0]
            self.assertAlmostEqual(250000.0, float(a_cash), delta=1.0)
        # 5) 重新恢复运行：账本不凭空放大；A 回到执行层。
        self._lifecycle_resume("ledger_user_a")
        PT.set_accounts_status("running")
        with self._conn() as conn:
            total = conn.execute(
                "SELECT COALESCE(SUM(initial_cash),0) FROM paper_accounts WHERE cycle_id=?",
                (cycle["id"],),
            ).fetchone()[0]
            self.assertAlmostEqual(500000.0, float(total), delta=2.0)
            self.assertIn(
                "ledger_user_a", PT.execution_participant_ids(conn, cycle["id"]),
            )


if __name__ == "__main__":
    unittest.main()
