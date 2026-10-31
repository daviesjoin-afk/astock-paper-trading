"""Business owners work without the trading composition module."""
import ast
import dataclasses
import datetime as dt
import importlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

import paper_capital_reservations as PCR
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
        self.assertEqual(O._require_order_execution_cycle(self.conn, 1, account_id="account"), 8)
        old = sqlite3.connect(":memory:")
        self.addCleanup(old.close)
        old.executescript("CREATE TABLE paper_orders(id INTEGER); INSERT INTO paper_orders VALUES(1);")
        self.assertEqual(O._order_cycle_provenance_for_order(old, 1).status, O.ORDER_CYCLE_PRE_V18_SCHEMA)

    def test_rebound_economic_account_cannot_authorize_old_order(self):
        self.conn.execute("UPDATE paper_accounts SET cycle_id=9")
        with self.assertRaises(O.OrderExecutionCycleChanged):
            O._require_order_execution_cycle(self.conn, 1, account_id="account")

    def test_missing_cycle_does_not_create_one(self):
        self.conn.execute("DELETE FROM paper_cycles")
        with self.assertRaises(O.OrderExecutionCycleChanged):
            O._require_order_execution_cycle(self.conn, 1, account_id="account")
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
                mock.patch.object(P, "_require_order_execution_cycle", side_effect=AssertionError("engine callback")):
            deps = E._pt()
            self.assertIs(deps.record_lot, L.record_lot)
            self.assertIs(deps.consume_available_lots, L.consume_available_lots)
            self.assertIs(deps.sync_positions, L.sync_positions)
            self.assertEqual(deps.require_order_execution_cycle(self.conn, 1, account_id="account"), 8)
            self.assertIs(M._dependencies().require_order_execution_cycle, O._require_order_execution_cycle)
            self.assertIs(M._dependencies().position_rows, L.position_rows)
            self.assertIs(D._dependencies().position_rows, L.position_rows)
        with mock.patch.object(L, "record_lot") as owner_override:
            self.assertIs(E._pt().record_lot, owner_override)

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
                "_record_lot", "_consume_available_lots", "_sync_positions", "_require_order_execution_cycle",
            }:
                self.assertFalse(any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                                     and call.func.attr == "execute" for call in ast.walk(node)), node.name)


class ManualOrderCapabilityBridgeTests(unittest.TestCase):
    """手单的能力桥只保留交易引擎自己的业务能力。

    模块对象（决策引擎、组合分配/运行时、预占、运行时设置）都有**独立 owner**。
    把它们经 ``paper_trading`` 的全局桥转发一遍，依赖数量一个不少，"谁负责" 却
    更难看出来 —— 而且这条转发会让"手单不加载交易引擎"变成一句空话。这里用
    机械口径把它固定住：桥里不允许出现任何模块对象。
    """

    DIRECT_OWNERS = {
        "DE": "decision_engine",
        "PAPolicy": "portfolio_allocation_policy",
        "PAW": "portfolio_allocation_weights",
        "PRS": "portfolio_runtime_service",
        "PRuntime": "portfolio_runtime",
        "RSET": "runtime_settings",
    }

    @staticmethod
    def _relayed_modules(field_names, resolve):
        """Return field names whose value is a module other than the engine."""
        relayed = []
        for name in field_names:
            value = resolve(name)
            module_name = getattr(value, "__name__", None)
            if isinstance(value, type(sys)) and module_name != "paper_trading":
                relayed.append(f"{name}->{module_name}")
        return relayed

    def test_scan_detects_a_relayed_module(self):
        # 非空性：同一份扫描必须能把"模块对象走桥"这种情况抓出来。
        synthetic = {"DE": sys, "LOT_SIZE": 100}
        self.assertEqual(["DE->sys"], self._relayed_modules(synthetic, synthetic.get))

    def test_bridge_carries_no_module_objects(self):
        import manual_orders as M
        import paper_trading as P

        names = [field.name for field in dataclasses.fields(M.ManualOrderDependencies)]
        self.assertGreater(len(names), 20, "能力桥为空，扫描没有意义")
        self.assertEqual([], self._relayed_modules(names, lambda name: getattr(P, name, None)))

    def test_owners_are_imported_directly(self):
        import manual_orders as M

        for attr, module_name in self.DIRECT_OWNERS.items():
            with self.subTest(attr=attr):
                self.assertIs(getattr(M, attr), importlib.import_module(module_name))
        self.assertIs(M.ReservationCycleMismatch, PCR.ReservationCycleMismatch)

    def test_manual_orders_import_does_not_load_the_trading_engine(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; import manual_orders; "
             "assert 'paper_trading' not in sys.modules, sorted(sys.modules)[:5]"],
            cwd=BACKEND, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
