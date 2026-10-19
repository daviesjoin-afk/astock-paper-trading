"""Business owners work without the trading composition module."""
import ast
import datetime as dt
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

import paper_lot_ledger as L
import paper_order_execution_authority as O


BACKEND = Path(__file__).resolve().parent
DAY = dt.date(2026, 9, 9)


class BusinessOwnerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, status TEXT);
            INSERT INTO paper_cycles VALUES(8,'running');
            CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, cycle_id INTEGER, status TEXT);
            INSERT INTO paper_accounts VALUES('account',8,'running');
            CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, cycle_id INTEGER);
            INSERT INTO paper_orders VALUES(1,8),(2,NULL);
            CREATE TABLE paper_position_lots(
                id INTEGER PRIMARY KEY, cycle_id INTEGER, account_id TEXT,
                code TEXT, name TEXT, industry TEXT, qty INTEGER, remaining_qty INTEGER,
                cost REAL, acquired_at TEXT, available_date TEXT, asset_type TEXT,
                source_order_id INTEGER, cost_fee_included INTEGER, is_t_base INTEGER,
                source_fill_id INTEGER);
            CREATE TABLE paper_position_risk_state(
                cycle_id INTEGER, account_id TEXT, code TEXT, peak_price REAL,
                take_stage INTEGER, opened_order_id INTEGER,
                initialized_at TEXT, updated_at TEXT,
                PRIMARY KEY(cycle_id,account_id,code));
        """)

    def buy(self, **kwargs):
        return L.record_lot(
            self.conn, {"id": "account"}, {"code": "600001", "name": "stock"},
            300, 10., DAY, order_id=1, fees=3., acquired_at="2026-09-09 10:00:00",
            next_trade_day_fn=lambda day: dt.date(2026, 9, 10), **kwargs,
        )

    def test_owners_import_without_trading_or_adaptive_engines(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; import paper_order_execution_authority, paper_lot_ledger; "
             "assert 'paper_trading' not in sys.modules; assert 'adaptive_engine' not in sys.modules"],
            cwd=BACKEND, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_order_authorization_is_read_only_and_preserves_four_states(self):
        self.conn.execute("PRAGMA query_only=ON")
        self.assertEqual(O._order_cycle_provenance_for_order(self.conn, 1).status, O.ORDER_CYCLE_PROVEN)
        self.assertEqual(O._order_cycle_provenance_for_order(self.conn, 2).status, O.ORDER_CYCLE_LEGACY_UNKNOWN)
        self.assertEqual(O._order_cycle_provenance_for_order(self.conn, 99).status, O.ORDER_CYCLE_ORDER_MISSING)
        self.assertEqual(O._assert_order_execution_cycle(self.conn, 1, account_id="account"), 8)
        old = sqlite3.connect(":memory:")
        self.addCleanup(old.close)
        old.executescript("CREATE TABLE paper_orders(id INTEGER); INSERT INTO paper_orders VALUES(1);")
        self.assertEqual(O._order_cycle_provenance_for_order(old, 1).status, O.ORDER_CYCLE_PRE_V18_SCHEMA)

    def test_rebound_economic_account_cannot_authorize_old_order(self):
        self.conn.execute("UPDATE paper_accounts SET cycle_id=9")
        with self.assertRaises(O.OrderExecutionCycleChanged):
            O._assert_order_execution_cycle(self.conn, 1, account_id="account")

    def test_missing_cycle_does_not_create_one(self):
        self.conn.execute("DELETE FROM paper_cycles")
        with self.assertRaises(O.OrderExecutionCycleChanged):
            O._assert_order_execution_cycle(self.conn, 1, account_id="account")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_cycles").fetchone()[0], 0)

    def test_lot_inherits_order_cycle_and_t1_blocks_same_day_consumption(self):
        self.buy(cycle_id=999)
        row = self.conn.execute("SELECT * FROM paper_position_lots").fetchone()
        self.assertEqual(row["cycle_id"], 8)
        self.assertAlmostEqual(row["cost"], 10.01)
        self.assertEqual(L.consume_available_lots(self.conn, "account", "600001", 100, DAY, cycle_id=8), (0, 0.))
        taken, cost = L.consume_available_lots(self.conn, "account", "600001", 100, "2026-09-10", cycle_id=8)
        self.assertEqual(taken, 100)
        self.assertAlmostEqual(cost, 1001.)
        self.assertEqual(self.conn.execute("SELECT remaining_qty FROM paper_position_lots").fetchone()[0], 200)

    def test_lot_owner_never_falls_back_to_active_cycle(self):
        with self.assertRaises(O.OrderCycleProvenanceUnknown):
            L.record_lot(self.conn, {"id": "account"}, {"code": "600001"}, 100, 10., DAY)
        with self.assertRaises(O.OrderCycleProvenanceUnknown):
            L.record_lot(self.conn, {"id": "account"}, {"code": "600001"}, 100, 10., DAY, order_id=2, cycle_id=8)
        with self.assertRaises(O.OrderCycleProvenanceUnknown):
            L.consume_available_lots(self.conn, "account", "600001", 100, DAY)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_position_lots").fetchone()[0], 0)

    def test_lot_and_episode_writes_roll_back_in_caller_transaction(self):
        self.conn.execute("SAVEPOINT caller")
        self.buy()
        self.conn.execute("ROLLBACK TO caller")
        self.conn.execute("RELEASE caller")
        for table in ("paper_position_lots", "paper_position_risk_state"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_production_bindings_use_owners_even_if_engine_facades_fail(self):
        import paper_trading as P
        import execution_planner as E
        import manual_orders as M
        import dashboard_queries as D
        self.assertIs(P.OrderCycleProvenanceUnknown, O.OrderCycleProvenanceUnknown)
        with mock.patch.object(P, "_record_lot", side_effect=AssertionError("engine callback")), \
                mock.patch.object(P, "_assert_order_execution_cycle", side_effect=AssertionError("engine callback")):
            deps = E._pt()
            self.assertIs(deps._record_lot, L.record_lot)
            self.assertIs(deps._consume_available_lots, L.consume_available_lots)
            self.assertIs(deps._sync_positions, L.sync_positions)
            self.assertEqual(deps._assert_order_execution_cycle(self.conn, 1, account_id="account"), 8)
            self.assertIs(M._dependencies()._assert_order_execution_cycle, O._assert_order_execution_cycle)
            self.assertIs(M._dependencies()._position_rows, L.position_rows)
            self.assertIs(D._dependencies()._position_rows, L.position_rows)
        with mock.patch.object(L, "record_lot") as owner_override:
            self.assertIs(E._pt()._record_lot, owner_override)

    def test_source_guards_follow_actual_owners_and_prohibit_transactions(self):
        for module in (O, L):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertNotIn("paper_trading", {a.name for a in node.names})
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "paper_trading")
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, {"commit", "rollback", "connect"})
                    if node.func.attr == "execute":
                        self.assertIsInstance(node.args[0], ast.Constant)
                        sql = node.args[0].value.strip().upper()
                        self.assertFalse(sql.startswith(("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "CREATE", "ALTER")))
                        if module is O:
                            self.assertTrue(sql.startswith(("SELECT", "PRAGMA")))
                        else:
                            self.assertFalse("SELECT" in sql and "FROM PAPER_POSITIONS" in sql)
        facade = ast.parse((BACKEND / "paper_trading.py").read_text(encoding="utf-8"))
        for node in facade.body:
            if isinstance(node, ast.FunctionDef) and node.name in {
                "_record_lot", "_consume_available_lots", "_sync_positions", "_assert_order_execution_cycle",
            }:
                self.assertFalse(any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                                     and call.func.attr == "execute" for call in ast.walk(node)), node.name)


if __name__ == "__main__":
    unittest.main()
