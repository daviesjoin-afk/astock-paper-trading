# -*- coding: utf-8 -*-
"""Contract and regression tests for pending position slot occupancy read model."""
from __future__ import annotations

import ast
import inspect
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import paper_slot_occupancy as PSO
import paper_trading as PT

MODULE_PATH = pathlib.Path(__file__).with_name("paper_slot_occupancy.py")
TRADING_PATH = pathlib.Path(__file__).with_name("paper_trading.py")


class OrderDatabaseMixin:
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE paper_orders(
                id INTEGER PRIMARY KEY, account_id TEXT, code TEXT,
                origin TEXT, side TEXT, status TEXT
            )"""
        )
        self.addCleanup(self.conn.close)

    def add_order(
        self,
        account_id: str = "acc1",
        code: str = "600000",
        origin: str = "strategy",
        side: str = "buy",
        status: str = "pending_limit",
        order_id: int | None = None,
    ):
        if order_id is not None:
            self.conn.execute(
                "INSERT INTO paper_orders(id, account_id, code, origin, side, status) VALUES(?,?,?,?,?,?)",
                (order_id, account_id, code, origin, side, status),
            )
        else:
            self.conn.execute(
                "INSERT INTO paper_orders(account_id, code, origin, side, status) VALUES(?,?,?,?,?)",
                (account_id, code, origin, side, status),
            )
        self.conn.commit()

    def pending_slots(
        self,
        positions=(),
        exclude_order_key=None,
        occupying_statuses=None,
        lot_size=100,
        num_fn=float,
        rows_fn=None,
    ):
        if occupying_statuses is None:
            occupying_statuses = (
                "pending_limit",
                PT.MANUAL_EXECUTION_RETRY_STATUS,
                PT.STRATEGY_EXECUTION_RETRY_STATUS,
            )
        if rows_fn is None:
            def _rows(conn, sql, params=()):
                cur = conn.cursor()
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
            rows_fn = _rows
        return PSO.pending_position_slots(
            self.conn,
            positions,
            exclude_order_key=exclude_order_key,
            occupying_statuses=occupying_statuses,
            lot_size=lot_size,
            num_fn=num_fn,
            rows_fn=rows_fn,
        )


class SlotOccupancyFilteringTests(OrderDatabaseMixin, unittest.TestCase):
    def test_order_filtering_counts_executable_buys(self):
        self.add_order(account_id="acc1", code="600001", origin="manual", side="buy", status="pending_limit")
        self.add_order(account_id="acc1", code="600002", origin="strategy", side="buy", status="pending_limit")
        self.add_order(account_id="acc2", code="600003", origin="manual", side="buy", status=PT.MANUAL_EXECUTION_RETRY_STATUS)
        self.add_order(account_id="acc2", code="600004", origin="strategy", side="buy", status=PT.STRATEGY_EXECUTION_RETRY_STATUS)

        expected = {
            ("acc1", "600001"),
            ("acc1", "600002"),
            ("acc2", "600003"),
            ("acc2", "600004"),
        }
        self.assertEqual(expected, self.pending_slots(positions=[]))

    def test_order_filtering_ignores_sell_orders(self):
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="sell", status="pending_limit")
        self.add_order(account_id="acc1", code="600002", origin="manual", side="sell", status=PT.MANUAL_EXECUTION_RETRY_STATUS)
        self.assertEqual(set(), self.pending_slots(positions=[]))

    def test_order_filtering_ignores_other_origins(self):
        self.add_order(account_id="acc1", code="600001", origin="system", side="buy", status="pending_limit")
        self.add_order(account_id="acc1", code="600002", origin="algo", side="buy", status="pending_limit")
        self.add_order(account_id="acc1", code="600003", origin="external", side="buy", status="pending_limit")
        self.assertEqual(set(), self.pending_slots(positions=[]))

    def test_order_filtering_ignores_terminal_and_non_occupying_statuses(self):
        for status in ("filled", "cancelled", "rejected", "released", "expired"):
            self.add_order(account_id="acc1", code=f"600{status[:3]}", origin="strategy", side="buy", status=status)
        self.assertEqual(set(), self.pending_slots(positions=[]))

    def test_waitlist_and_deferred_do_not_occupy_slots(self):
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="buy", status="deferred_capacity")
        self.add_order(account_id="acc1", code="600002", origin="strategy", side="buy", status=PT.ENTRY_FROZEN_WAITLIST_STATUS)
        self.assertEqual(set(), self.pending_slots(positions=[]))

    def test_pending_like_statuses_outside_occupying_tuple_ignored(self):
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="buy", status="submitting")
        self.add_order(account_id="acc1", code="600002", origin="strategy", side="buy", status="pending")
        self.assertEqual(set(), self.pending_slots(positions=[]))

    def test_empty_occupying_statuses_returns_empty_set(self):
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="buy", status="pending_limit")
        self.assertEqual(set(), self.pending_slots(positions=[], occupying_statuses=()))

    def test_empty_statuses_still_validate_invalid_exclude_before_return(self):
        rows_fn = mock.MagicMock()

        with self.assertRaises(ValueError):
            PSO.pending_position_slots(
                self.conn,
                [],
                exclude_order_key="not-an-int",
                occupying_statuses=(),
                lot_size=100,
                num_fn=float,
                rows_fn=rows_fn,
            )

        rows_fn.assert_not_called()


class SlotOccupancyIdentityAndSuppressionTests(OrderDatabaseMixin, unittest.TestCase):
    def test_distinct_identity_same_account_same_code_is_one_slot(self):
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="buy", status="pending_limit")
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="buy", status=PT.STRATEGY_EXECUTION_RETRY_STATUS)
        self.assertEqual({("acc1", "600001")}, self.pending_slots(positions=[]))

    def test_distinct_identity_different_accounts_same_code_are_two_slots(self):
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="buy", status="pending_limit")
        self.add_order(account_id="acc2", code="600001", origin="strategy", side="buy", status="pending_limit")
        self.assertEqual({("acc1", "600001"), ("acc2", "600001")}, self.pending_slots(positions=[]))

    def test_existing_full_position_suppresses_pending_order(self):
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="buy", status="pending_limit")
        self.add_order(account_id="acc2", code="600001", origin="strategy", side="buy", status="pending_limit")
        positions = [{"account_id": "acc1", "code": "600001", "qty": 100}]
        self.assertEqual({("acc2", "600001")}, self.pending_slots(positions=positions))

    def test_sub_lot_boundary_qty_99_does_not_suppress(self):
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="buy", status="pending_limit")
        positions = [{"account_id": "acc1", "code": "600001", "qty": 99}]
        self.assertEqual({("acc1", "600001")}, self.pending_slots(positions=positions, lot_size=100))

    def test_sub_lot_boundary_qty_100_suppresses(self):
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="buy", status="pending_limit")
        positions = [{"account_id": "acc1", "code": "600001", "qty": 100}]
        self.assertEqual(set(), self.pending_slots(positions=positions, lot_size=100))

    def test_sub_lot_boundary_fractional_qty_100_9_suppresses(self):
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="buy", status="pending_limit")
        positions = [{"account_id": "acc1", "code": "600001", "qty": 100.9}]
        self.assertEqual(set(), self.pending_slots(positions=positions, lot_size=100))

    def test_exclude_order_key_filters_exact_id_and_coerces_string(self):
        self.add_order(account_id="acc1", code="600001", origin="strategy", side="buy", status="pending_limit", order_id=10)
        self.add_order(account_id="acc1", code="600002", origin="strategy", side="buy", status="pending_limit", order_id=11)
        self.assertEqual({("acc1", "600002")}, self.pending_slots(positions=[], exclude_order_key="10"))

    def test_exclude_order_key_invalid_string_raises_value_error_without_query(self):
        mock_rows = mock.MagicMock()
        with self.assertRaises(ValueError):
            PSO.pending_position_slots(
                self.conn,
                [],
                exclude_order_key="not-an-int",
                occupying_statuses=("pending_limit",),
                lot_size=100,
                num_fn=float,
                rows_fn=mock_rows,
            )
        self.assertEqual(0, mock_rows.call_count)


class SlotOccupancyFacadeContractTests(unittest.TestCase):
    def test_facade_signature_exact_match(self):
        sig = inspect.signature(PT._pending_position_slots)
        self.assertEqual(
            ["conn", "positions", "exclude_order_key", "cycle_id"],
            list(sig.parameters.keys()))
        self.assertIs(None, sig.parameters["positions"].default)
        self.assertIs(None, sig.parameters["exclude_order_key"].default)
        # cycle_id 是 keyword-only：显式周期只能被指名传入，位置参数调用
        # 必须保持历史语义（面板 / 手动委托读"现在"的席位）。
        self.assertIs(
            inspect.Parameter.KEYWORD_ONLY,
            sig.parameters["cycle_id"].kind,
        )
        self.assertIs(None, sig.parameters["cycle_id"].default)

    def test_explicit_cycle_scopes_the_pending_order_query(self):
        """显式 cycle_id 必须进入 SQL；None 时保持历史（不过滤周期）语义。"""
        with mock.patch.object(PT.PSO, "pending_position_slots",
                               return_value=set()) as mock_pso:
            PT._pending_position_slots(mock.MagicMock(), positions=[], cycle_id=8)
            self.assertEqual(8, mock_pso.call_args.kwargs["cycle_id"])
            PT._pending_position_slots(mock.MagicMock(), positions=[])
            self.assertIsNone(mock_pso.call_args.kwargs["cycle_id"])

    def test_explicit_empty_positions_skips_position_rows_sentinel(self):
        with mock.patch.object(PT, "_position_rows") as mock_pos_rows, \
             mock.patch.object(PT.PSO, "pending_position_slots", return_value=set()) as mock_pso:
            PT._pending_position_slots(mock.MagicMock(), positions=[])
            self.assertEqual(0, mock_pos_rows.call_count)
            self.assertEqual([], mock_pso.call_args.args[1])

    def test_none_positions_invokes_position_rows_sentinel(self):
        sentinel_rows = [{"account_id": "sentinel", "code": "000001", "qty": 100}]
        with mock.patch.object(PT, "_position_rows", return_value=sentinel_rows) as mock_pos_rows, \
             mock.patch.object(PT.PSO, "pending_position_slots", return_value=set()) as mock_pso:
            PT._pending_position_slots(mock.MagicMock(), positions=None)
            self.assertEqual(1, mock_pos_rows.call_count)
            self.assertIs(sentinel_rows, mock_pso.call_args.args[1])

    def test_call_time_dependency_injection(self):
        custom_statuses = ("custom_status",)
        custom_lot_size = 200
        custom_num = mock.MagicMock(side_effect=float)
        custom_rows = mock.MagicMock(return_value=[])
        with mock.patch.object(PT, "ENTRY_SLOT_OCCUPYING_ORDER_STATUSES", custom_statuses), \
             mock.patch.object(PT, "LOT_SIZE", custom_lot_size), \
             mock.patch.object(PT, "_num", custom_num), \
             mock.patch.object(PT, "_rows", custom_rows), \
             mock.patch.object(PT.PSO, "pending_position_slots", return_value=set()) as mock_pso:
            PT._pending_position_slots(mock.MagicMock(), positions=[])
            kwargs = mock_pso.call_args.kwargs
            self.assertIs(custom_statuses, kwargs["occupying_statuses"])
            self.assertEqual(custom_lot_size, kwargs["lot_size"])
            self.assertIs(custom_num, kwargs["num_fn"])
            self.assertIs(custom_rows, kwargs["rows_fn"])

    def test_production_consumers_parity(self):
        # Verify that dashboard_queries and manual_orders import _pending_position_slots
        # from paper_trading rather than writing custom queries
        for module_name in ("dashboard_queries.py", "manual_orders.py"):
            source = pathlib.Path(__file__).with_name(module_name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            found = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "paper_trading":
                    if any(alias.name == "_pending_position_slots" for alias in node.names):
                        found = True
                        break
            self.assertTrue(found, f"{module_name} must import _pending_position_slots from paper_trading")


class SlotOccupancyArchitectureGuardTests(unittest.TestCase):
    def test_module_is_stdlib_only_and_direction_clean(self):
        self.assertTrue(MODULE_PATH.exists())
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module.split(".")[0])

        allowed_modules = {"__future__", "typing", "collections"}
        self.assertTrue(
            imported_modules.issubset(allowed_modules),
            f"Non-stdlib or unapproved imports found: {imported_modules - allowed_modules}",
        )

        forbidden_symbols = (
            "paper_trading",
            "paper_capital_reservations",
            "paper_shared_cash",
            "paper_slot_service",
            "paper_allocation",
            "portfolio_coordinator",
            "strategy_registry",
            "strategy_runtime",
            "paper_cycles",
            "paper_accounts",
            "strategy_definitions",
            "INSERT",
            "UPDATE",
            "DELETE",
            "REPLACE",
            "DROP",
            "ALTER",
            "CREATE",
            "COMMIT",
            "ROLLBACK",
            "BEGIN",
            "SAVEPOINT",
        )
        for forbidden in forbidden_symbols:
            self.assertNotIn(forbidden, source)

        self.assertEqual(1, source.count("SELECT account_id,code FROM paper_orders WHERE"))

    def test_facade_delegation_has_no_inline_sql(self):
        source = TRADING_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        func_nodes = [
            n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_pending_position_slots"
        ]
        self.assertEqual(1, len(func_nodes))
        func_source = ast.get_source_segment(source, func_nodes[0]) or ""
        self.assertNotIn("SELECT", func_source)
        self.assertNotIn("paper_orders", func_source)
        self.assertNotIn("origin IN", func_source)
        self.assertNotIn("status IN", func_source)
        self.assertIn("PSO.pending_position_slots", func_source)

    def test_architecture_notes_distinguishes_slot_service_and_occupancy(self):
        architecture_path = pathlib.Path(__file__).parents[1].joinpath("ARCHITECTURE.md")
        if not architecture_path.exists():
            self.skipTest("docs-free smoke image")
        architecture = architecture_path.read_text(encoding="utf-8")
        self.assertIn("paper_slot_occupancy.py", architecture)
        self.assertIn("paper_slot_service.py", architecture)
        self.assertIn("deferred/waitlist markers do not occupy executable position slots", architecture)


if __name__ == "__main__":
    unittest.main()
