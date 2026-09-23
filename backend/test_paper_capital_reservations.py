# -*- coding: utf-8 -*-
"""Contract tests for the runtime paper capital-reservation ledger boundary."""
from __future__ import annotations

import inspect
import ast
import pathlib
import sqlite3
import unittest
from unittest import mock

import paper_capital_reservations as PCR
import paper_trading as PT


MODULE_PATH = pathlib.Path(__file__).with_name("paper_capital_reservations.py")
TRADING_PATH = pathlib.Path(__file__).with_name("paper_trading.py")


class ReservationDatabaseMixin:
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """CREATE TABLE paper_capital_reservations(
                id INTEGER PRIMARY KEY, cycle_id INTEGER, order_key TEXT,
                account_id TEXT, code TEXT, side TEXT, amount REAL, fees REAL,
                status TEXT, created_at TEXT, released_at TEXT)"""
        )
        self.addCleanup(self.conn.close)

    def add_row(self, order_key, account_id="a", cycle_id=1, side="buy",
                amount=10.0, fees=1.0, status="reserved", created_at="old",
                released_at=None, code="600000"):
        self.conn.execute(
            """INSERT INTO paper_capital_reservations
               (cycle_id,order_key,account_id,code,side,amount,fees,status,created_at,released_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (cycle_id, order_key, account_id, code, side, amount, fees, status,
             created_at, released_at),
        )
        self.conn.commit()


class PendingReservationAggregateTests(ReservationDatabaseMixin, unittest.TestCase):
    def pending(self, cycle_id=None, exclude=None):
        return PCR.pending_buy_reservations(self.conn, cycle_id, exclude, num_fn=float)

    def test_empty_is_zero(self):
        self.assertEqual(({}, 0), self.pending())

    def test_one_account_and_amount_plus_fees(self):
        self.add_row("1", amount=12, fees=3)
        self.assertEqual(({"a": 15.0}, 15.0), self.pending())

    def test_multiple_accounts(self):
        self.add_row("1", account_id="a", amount=2, fees=1)
        self.add_row("2", account_id="b", amount=4, fees=5)
        self.assertEqual(({"a": 3.0, "b": 9.0}, 12.0), self.pending())

    def test_only_buy_reserved_are_counted(self):
        self.add_row("buy", side="buy", status="reserved")
        self.add_row("sell", side="sell")
        self.add_row("released", status="released")
        self.add_row("consumed", status="consumed")
        self.assertEqual(({"a": 11.0}, 11.0), self.pending())

    def test_released_and_consumed_are_ignored(self):
        self.add_row("r", status="released", amount=100)
        self.add_row("c", status="consumed", amount=100)
        self.assertEqual(({}, 0), self.pending())

    def test_exclude_order_key_is_string_and_cross_cycle_is_counted(self):
        self.add_row("same", cycle_id=9, amount=7, fees=1)
        self.add_row("other", cycle_id=2, amount=5, fees=2)
        self.assertEqual(({"a": 7.0}, 7.0), self.pending(1, "same"))

    def test_cycle_id_does_not_scope_query(self):
        self.add_row("old", cycle_id=99, amount=20, fees=0)
        self.assertEqual(({"a": 20.0}, 20.0), self.pending(1))

    def test_negative_aggregate_is_clamped(self):
        self.add_row("negative", amount=-10, fees=0)
        self.assertEqual(({"a": 0.0}, 0.0), self.pending())

    def test_sqlite_row_and_tuple_connections_work(self):
        self.add_row("1", amount=2, fees=3)
        self.assertEqual(5.0, PCR.pending_buy_reservations(self.conn, num_fn=float)[1])
        self.conn.row_factory = sqlite3.Row
        self.assertEqual(5.0, PCR.pending_buy_reservations(self.conn, num_fn=float)[1])


class ReserveCapitalTests(ReservationDatabaseMixin, unittest.TestCase):
    def reserve(self, **kwargs):
        defaults = dict(num_fn=float, now_fn=lambda: "now",
                        shared_cash_fn=lambda conn: 100.0,
                        active_cycle_fn=lambda conn: {"id": 7})
        defaults.update(kwargs)
        return PCR.reserve_shared_capital(self.conn, "new", "a", "600000", 10, 2, **defaults)

    def test_new_reservation_golden_row_and_buy_side(self):
        self.assertEqual((True, None), self.reserve())
        self.assertEqual((7, "new", "a", "600000", "buy", 10, 2, "reserved", "now"),
                         self.conn.execute("""SELECT cycle_id,order_key,account_id,code,side,
                         amount,fees,status,created_at FROM paper_capital_reservations""").fetchone())

    def test_new_resolves_active_cycle_lazily(self):
        active = mock.Mock(return_value={"id": 8})
        self.assertEqual((True, None), self.reserve(active_cycle_fn=active))
        active.assert_called_once_with(self.conn)

    def test_new_reservation_calls_cycle_then_clock(self):
        events = []

        def active_cycle(conn):
            events.append("cycle")
            return {"id": 8}

        def now():
            events.append("now")
            return "now"

        self.assertEqual((True, None), self.reserve(active_cycle_fn=active_cycle, now_fn=now))
        self.assertEqual(["cycle", "now"], events)

    def test_new_cycle_failure_never_calls_clock_or_writes(self):
        now = mock.Mock(return_value="now")
        before = self.conn.total_changes
        with self.assertRaisesRegex(RuntimeError, "^cycle unavailable$"):
            self.reserve(
                active_cycle_fn=mock.Mock(side_effect=RuntimeError("cycle unavailable")),
                now_fn=now,
            )
        now.assert_not_called()
        self.assertEqual(before, self.conn.total_changes)
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM paper_capital_reservations").fetchone()[0])

    def test_negative_amount_and_fees_clamp_to_zero(self):
        self.assertEqual((True, None), self.reserve())
        self.conn.execute("DELETE FROM paper_capital_reservations")
        self.assertEqual((True, None), PCR.reserve_shared_capital(
            self.conn, "x", "a", "c", -4, -2, num_fn=float, now_fn=lambda: "n",
            shared_cash_fn=lambda conn: 0, active_cycle_fn=lambda conn: {"id": 1}))
        self.assertEqual((0.0, 0.0), self.conn.execute(
            "SELECT amount,fees FROM paper_capital_reservations WHERE order_key='x'").fetchone())

    def test_consumed_refusal_performs_zero_writes(self):
        self.add_row("new", status="consumed")
        before = self.conn.total_changes
        self.assertEqual((False, "该订单资金预占已经消费，禁止重复成交"), self.reserve())
        self.assertEqual(before, self.conn.total_changes)

    def test_resize_excludes_itself_and_preserves_identity(self):
        self.add_row("new", cycle_id=3, account_id="old", code="old", side="buy",
                     amount=10, fees=2, status="reserved", released_at="released")
        self.add_row("other", amount=2, fees=0, status="reserved")
        self.assertEqual((True, None), self.reserve(shared_cash_fn=lambda conn: 16.0))
        self.assertEqual((3, "old", "old", "buy", 10.0, 2.0, "reserved", "now", None),
                         self.conn.execute("""SELECT cycle_id,account_id,code,side,amount,fees,
                         status,created_at,released_at FROM paper_capital_reservations
                         WHERE order_key='new'""").fetchone())

    def test_existing_row_does_not_resolve_active_cycle(self):
        self.add_row("new", status="released")
        active = mock.Mock(side_effect=AssertionError("existing rows must not resolve cycle"))
        self.assertEqual((True, None), self.reserve(active_cycle_fn=active))
        active.assert_not_called()

    def test_existing_row_calls_clock_only_for_update(self):
        self.add_row("new", status="released")
        events = []
        self.assertEqual(
            (True, None),
            self.reserve(
                active_cycle_fn=lambda conn: events.append("cycle") or {"id": 7},
                now_fn=lambda: events.append("now") or "now",
            ),
        )
        self.assertEqual(["now"], events)

    def test_exact_cash_and_epsilon_succeed_but_insufficient_does_not_mutate(self):
        self.assertEqual((True, None), self.reserve(shared_cash_fn=lambda conn: 12.0))
        self.conn.execute("DELETE FROM paper_capital_reservations")
        self.assertEqual((True, None), self.reserve(shared_cash_fn=lambda conn: 11.999999))
        self.conn.execute("DELETE FROM paper_capital_reservations")
        before = self.conn.total_changes
        self.assertEqual((False, "待成交买单已预占 ¥0.00，共享可用现金不足"),
                         self.reserve(shared_cash_fn=lambda conn: 11.0))
        self.assertEqual(before, self.conn.total_changes)

    def test_shared_cash_is_read_at_call_time(self):
        cash = [0.0]
        self.assertEqual(False, self.reserve(shared_cash_fn=lambda conn: cash[0])[0])
        cash[0] = 12.0
        self.assertEqual(True, self.reserve(shared_cash_fn=lambda conn: cash[0])[0])


class FinishReservationTests(ReservationDatabaseMixin, unittest.TestCase):
    def finish(self, status, order_key="x"):
        return PCR.finish_capital_reservation(self.conn, order_key, status, now_fn=lambda: "finished")

    def test_reserved_to_consumed_and_released(self):
        self.add_row("x")
        self.finish("consumed")
        self.assertEqual(("consumed", "finished"), self.conn.execute(
            "SELECT status,released_at FROM paper_capital_reservations WHERE order_key='x'").fetchone())
        self.add_row("y")
        self.assertIsNone(self.finish("released", "y"))
        self.assertEqual("released", self.conn.execute(
            "SELECT status FROM paper_capital_reservations WHERE order_key='y'").fetchone()[0])

    def test_invalid_status_exact_error(self):
        with self.assertRaisesRegex(ValueError, "^非法资金预占状态$"):
            self.finish("reserved")

    def test_consumed_row_is_noop(self):
        self.add_row("x", status="consumed")
        before = self.conn.total_changes
        self.finish("released")
        self.assertEqual(before, self.conn.total_changes)

    def test_released_row_is_noop(self):
        self.add_row("x", status="released")
        before = self.conn.total_changes
        self.finish("consumed")
        self.assertEqual(before, self.conn.total_changes)

    def test_unknown_order_key_is_noop(self):
        before = self.conn.total_changes
        self.finish("consumed")
        self.assertEqual(before, self.conn.total_changes)


class FacadeContractTests(unittest.TestCase):
    def test_legacy_names_and_signatures(self):
        self.assertTrue(all(hasattr(PT, name) for name in (
            "_pending_buy_reservations", "_reserve_shared_capital", "_finish_capital_reservation")))
        self.assertEqual("(conn, cycle_id=None, exclude_order_key=None)",
                         str(inspect.signature(PT._pending_buy_reservations)))
        self.assertEqual(
            "(conn, order_key, account_id, code, amount, fees=0.0, *, expected_cycle_id=None)",
            str(inspect.signature(PT._reserve_shared_capital)),
            "Round-7 起新增 keyword-only 的 expected_cycle_id：位置调用语义不变，"
            "但预占层必须能收到订单的周期（§20–§22）",
        )
        self.assertEqual("(conn, order_key, status)", str(inspect.signature(PT._finish_capital_reservation)))

    def test_facades_delegate_once(self):
        conn = object()
        with mock.patch.object(PT.PCR, "pending_buy_reservations", return_value=(1, 2)) as fn:
            self.assertEqual((1, 2), PT._pending_buy_reservations(conn, 3, "x"))
            fn.assert_called_once()
        with mock.patch.object(PT.PCR, "reserve_shared_capital", return_value=(True, None)) as fn:
            self.assertEqual((True, None), PT._reserve_shared_capital(conn, 1, "a", "c", 2))
            fn.assert_called_once()
        with mock.patch.object(PT.PCR, "finish_capital_reservation") as fn:
            PT._finish_capital_reservation(conn, 1, "released")
            fn.assert_called_once()

    def test_runtime_dependency_patches_are_visible(self):
        with mock.patch.object(PT.PCR, "pending_buy_reservations", return_value=(None, 0)) as fn, \
             mock.patch.object(PT.PCR, "reserve_shared_capital", return_value=(True, None)) as reserve, \
             mock.patch.object(PT.PCR, "finish_capital_reservation") as finish, \
             mock.patch.object(PT, "_num", side_effect=lambda value, default=0.0: 99.0), \
             mock.patch.object(PT, "_now", return_value="patched"), \
             mock.patch.object(PT, "_shared_cash", return_value=88.0), \
             mock.patch.object(PT, "_active_cycle", return_value={"id": 1}):
            PT._pending_buy_reservations(object())
            PT._reserve_shared_capital(object(), "x", "a", "c", 1)
            PT._finish_capital_reservation(object(), "x", "released")
            kwargs = fn.call_args.kwargs
            self.assertIs(PT._num, kwargs["num_fn"])
            self.assertIs(PT._now, reserve.call_args.kwargs["now_fn"])
            self.assertIs(PT._shared_cash, reserve.call_args.kwargs["shared_cash_fn"])
            self.assertIs(PT._active_cycle, reserve.call_args.kwargs["active_cycle_fn"])
            self.assertIs(PT._now, finish.call_args.kwargs["now_fn"])

    def test_facade_contains_no_reservation_sql_algorithm(self):
        source = TRADING_PATH.read_text(encoding="utf-8")
        body_start = source.index("def _pending_buy_reservations")
        body_end = source.index("def _debit_shared_cash", body_start)
        body = source[body_start:body_end]
        self.assertNotIn("paper_capital_reservations", body)
        self.assertNotIn("INSERT", body)
        self.assertNotIn("UPDATE", body)


class ArchitectureGuardTests(unittest.TestCase):
    def test_module_direction_and_runtime_boundary(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import paper_trading", source)
        for forbidden in ("paper_orders", "paper_positions", "paper_cycles", "COMMIT", "ROLLBACK", "SAVEPOINT"):
            self.assertNotIn(forbidden, source)
        self.assertEqual(1, source.count("INSERT INTO paper_capital_reservations"))
        # Two updates manage reservation lifecycle; two consume amounts as R26
        # partial fills commit. Keep all reservation writes in this owner.
        self.assertEqual(4, source.count('"""UPDATE paper_capital_reservations'))

    def test_recovery_exception_is_documented(self):
        architecture_path = pathlib.Path(__file__).parents[1].joinpath("ARCHITECTURE.md")
        if not architecture_path.exists():
            self.skipTest("docs-free smoke image")
        architecture = architecture_path.read_text(encoding="utf-8")
        self.assertIn("recovery exception", architecture.lower())
        self.assertIn("cross-cycle", architecture.lower())

    def test_recovery_exception_source_is_enforced(self):
        source = TRADING_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        function_sources = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        facade_region = "\n".join(
            function_sources[name]
            for name in (
                "_pending_buy_reservations",
                "_reserve_shared_capital",
                "_finish_capital_reservation",
            )
        )
        self.assertNotIn("paper_capital_reservations", facade_region)
        self.assertNotIn("INSERT", facade_region)
        self.assertNotIn("UPDATE", facade_region)

        direct_mutators = {
            name for name, body in function_sources.items()
            if "paper_capital_reservations" in body
            and ("INSERT INTO" in body or "UPDATE paper_capital_reservations" in body)
        }
        # ``init_db`` is bootstrap/schema reconciliation, not normal runtime
        # CRUD.  Prove that classification before excluding it from the
        # runtime set; this keeps the recovery detector non-vacuous.
        self.assertIn("init_db", direct_mutators)
        self.assertIn("CREATE TABLE IF NOT EXISTS paper_capital_reservations", function_sources["init_db"])
        runtime_mutators = direct_mutators - {"init_db"}
        self.assertIn("_reconcile_signal_order_states", runtime_mutators)
        self.assertEqual({"_reconcile_signal_order_states"}, runtime_mutators)


if __name__ == "__main__":
    unittest.main()
