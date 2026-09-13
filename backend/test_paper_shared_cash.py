# -*- coding: utf-8 -*-
"""Shared cash ledger contract and compatibility-facade tests."""
from __future__ import annotations

import ast
import inspect
import pathlib
import sqlite3
import unittest
from unittest import mock

import paper_shared_cash as PSC
import paper_trading as PT


MODULE_PATH = pathlib.Path(__file__).with_name("paper_shared_cash.py")
TRADING_PATH = pathlib.Path(__file__).with_name("paper_trading.py")


def _rows(*items):
    return [
        {
            "id": account_id,
            "cash": cash,
            "initial_cash": initial_cash,
        }
        for account_id, cash, initial_cash in items
    ]


class CashDatabaseMixin:
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE paper_accounts("
            "id TEXT PRIMARY KEY, cash REAL, initial_cash REAL, cycle_id INTEGER, "
            "status TEXT, updated_at TEXT)"
        )
        self.addCleanup(self.conn.close)

    def insert_accounts(self, *items):
        self.conn.executemany(
            "INSERT INTO paper_accounts(id,cash,initial_cash,cycle_id,status,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            [(account_id, cash, initial_cash, 7, "running", "old")
             for account_id, cash, initial_cash in items],
        )

    def snapshot(self):
        return self.conn.execute(
            "SELECT id,cash,initial_cash,cycle_id,status,updated_at "
            "FROM paper_accounts ORDER BY id"
        ).fetchall()

    def debit(self, rows, amount, preferred=None):
        return PSC.debit_shared_cash(
            self.conn,
            rows,
            amount,
            preferred_account_id=preferred,
            now_fn=lambda: "fixed",
            num_fn=PT._num,
        )


class SharedCashAggregateTests(unittest.TestCase):
    def test_shared_cash_sums_already_resolved_rows(self):
        self.assertEqual(18.0, PSC.shared_cash(_rows(("a", 12.34, 2), ("b", 5.66, 3))))

    def test_shared_cash_empty_rows_is_zero(self):
        self.assertEqual(0, PSC.shared_cash([]))

    def test_declared_capital_takes_priority(self):
        rows = _rows(("a", 10.0, 11.0), ("b", 20.0, 22.0))
        self.assertEqual(100.0, PSC.shared_initial_cash(rows, 100.0))

    def test_declared_capital_priority_does_not_need_rows(self):
        self.assertEqual(100.0, PSC.shared_initial_cash([], 100.0))

    def test_initial_cash_fallback_sums_rows_when_declared_is_unavailable(self):
        rows = _rows(("a", 10.0, 11.0), ("b", 20.0, 22.0))
        self.assertEqual(33.0, PSC.shared_initial_cash(rows, None))
        self.assertEqual(33.0, PSC.shared_initial_cash(rows, 0.0))

    def test_missing_initial_cash_uses_cash_fallback(self):
        rows = [{"id": "a", "cash": 12.5}, {"id": "b", "cash": 7.5}]
        self.assertEqual(20.0, PSC.shared_initial_cash(rows, None))

    def test_initial_cash_fallback_clamps_negative_total(self):
        rows = _rows(("a", 0.0, -10.0), ("b", 0.0, 2.0))
        self.assertEqual(0.0, PSC.shared_initial_cash(rows, None))

    def test_none_and_zero_cash_values_use_existing_numeric_fallback(self):
        rows = [{"cash": None}, {"cash": 0.0}, {"cash": 2.5}]
        self.assertEqual(2.5, PSC.shared_cash(rows))


class SharedCashDebitTests(CashDatabaseMixin, unittest.TestCase):
    def test_preferred_account_is_consumed_first(self):
        rows = _rows(("a", 20.0, 0.0), ("b", 50.0, 0.0))
        self.insert_accounts(("a", 20.0, 0.0), ("b", 50.0, 0.0))
        self.assertTrue(self.debit(rows, 30.0, preferred="a"))
        self.assertEqual(
            [("a", 0.0, 0.0, 7, "running", "fixed"),
             ("b", 40.0, 0.0, 7, "running", "fixed")],
            self.snapshot(),
        )

    def test_preferred_shortfall_spills_to_other_accounts(self):
        rows = _rows(("a", 20.0, 0.0), ("b", 50.0, 0.0), ("c", 80.0, 0.0))
        self.insert_accounts(("a", 20.0, 0.0), ("b", 50.0, 0.0), ("c", 80.0, 0.0))
        self.debit(rows, 100.0, preferred="a")
        self.assertEqual(
            [("a", 0.0, 0.0, 7, "running", "fixed"),
             ("b", 50.0, 0.0, 7, "running", "old"),
             ("c", 0.0, 0.0, 7, "running", "fixed")],
            self.snapshot(),
        )

    def test_remaining_accounts_are_sorted_by_cash_descending(self):
        rows = _rows(("a", 10.0, 0.0), ("b", 40.0, 0.0), ("c", 20.0, 0.0))
        self.insert_accounts(("a", 10.0, 0.0), ("b", 40.0, 0.0), ("c", 20.0, 0.0))
        self.debit(rows, 50.0)
        self.assertEqual(
            [("a", 10.0, 0.0, 7, "running", "old"),
             ("b", 0.0, 0.0, 7, "running", "fixed"),
             ("c", 10.0, 0.0, 7, "running", "fixed")],
            self.snapshot(),
        )

    def test_equal_cash_tie_preserves_input_order(self):
        rows = _rows(("b", 10.0, 0.0), ("a", 10.0, 0.0))
        self.insert_accounts(("a", 10.0, 0.0), ("b", 10.0, 0.0))
        self.debit(rows, 15.0)
        self.assertEqual(
            [("a", 5.0, 0.0, 7, "running", "fixed"),
             ("b", 0.0, 0.0, 7, "running", "fixed")],
            self.snapshot(),
        )

    def test_preferred_id_missing_keeps_cash_sorting(self):
        rows = _rows(("a", 10.0, 0.0), ("b", 40.0, 0.0))
        self.insert_accounts(("a", 10.0, 0.0), ("b", 40.0, 0.0))
        self.debit(rows, 10.0, preferred="missing")
        self.assertEqual(10.0, self.snapshot()[0][1])
        self.assertEqual(30.0, self.snapshot()[1][1])

    def test_total_debit_matches_requested_amount(self):
        rows = _rows(("a", 12.34, 0.0), ("b", 8.88, 0.0))
        self.insert_accounts(("a", 12.34, 0.0), ("b", 8.88, 0.0))
        before = sum(row[1] for row in self.snapshot())
        self.debit(rows, 20.0)
        after = sum(row[1] for row in self.snapshot())
        self.assertAlmostEqual(20.0, before - after, places=6)

    def test_insufficient_funds_preflight_does_not_partially_write(self):
        rows = _rows(("a", 10.0, 1.0), ("b", 20.0, 2.0))
        self.insert_accounts(("a", 10.0, 1.0), ("b", 20.0, 2.0))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "共享资金池可用现金不足"):
            self.debit(rows, 30.00001)
        self.assertEqual(before, self.snapshot())

    def test_negative_debit_is_fail_closed_no_op(self):
        rows = _rows(("a", 10.0, 1.0))
        self.insert_accounts(("a", 10.0, 1.0))
        before = self.snapshot()
        self.assertTrue(self.debit(rows, -5.0))
        self.assertEqual(before, self.snapshot())

    def test_zero_debit_is_no_op(self):
        rows = _rows(("a", 10.0, 1.0))
        self.insert_accounts(("a", 10.0, 1.0))
        before = self.snapshot()
        self.assertTrue(self.debit(rows, 0.0))
        self.assertEqual(before, self.snapshot())

    def test_single_account_pool(self):
        rows = _rows(("a", 10.0, 1.0))
        self.insert_accounts(("a", 10.0, 1.0))
        self.debit(rows, 3.25)
        self.assertEqual(6.75, self.snapshot()[0][1])

    def test_successful_debit_only_changes_cash_and_timestamp(self):
        rows = _rows(("a", 10.0, 7.0), ("b", 3.0, 4.0))
        self.insert_accounts(("a", 10.0, 7.0), ("b", 3.0, 4.0))
        self.debit(rows, 2.0, preferred="a")
        self.assertEqual(
            [("a", 8.0, 7.0, 7, "running", "fixed"),
             ("b", 3.0, 4.0, 7, "running", "old")],
            self.snapshot(),
        )


class SharedCashCreditTests(CashDatabaseMixin, unittest.TestCase):
    def test_credit_increases_only_the_explicit_account(self):
        self.insert_accounts(("a", 10.0, 7.0), ("b", 20.0, 8.0))
        PSC.credit_shared_cash(
            self.conn, 5.5, "a", now_fn=lambda: "fixed", num_fn=PT._num,
        )
        self.assertEqual(
            [("a", 15.5, 7.0, 7, "running", "fixed"),
             ("b", 20.0, 8.0, 7, "running", "old")],
            self.snapshot(),
        )

    def test_multiple_credits_do_not_redistribute_between_accounts(self):
        self.insert_accounts(("a", 10.0, 7.0), ("b", 20.0, 8.0))
        PSC.credit_shared_cash(self.conn, 1.0, "b", now_fn=lambda: "one", num_fn=PT._num)
        PSC.credit_shared_cash(self.conn, 2.0, "b", now_fn=lambda: "two", num_fn=PT._num)
        self.assertEqual(10.0, self.snapshot()[0][1])
        self.assertEqual(23.0, self.snapshot()[1][1])

    def test_zero_credit_does_not_change_cash(self):
        self.insert_accounts(("a", 10.0, 7.0))
        PSC.credit_shared_cash(self.conn, 0.0, "a", now_fn=lambda: "zero", num_fn=PT._num)
        self.assertEqual(10.0, self.snapshot()[0][1])
        self.assertEqual("zero", self.snapshot()[0][5])

    def test_negative_credit_is_rejected_without_write(self):
        self.insert_accounts(("a", 10.0, 7.0))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "收到负数金额"):
            PSC.credit_shared_cash(self.conn, -1.0, "a", now_fn=lambda: "bad", num_fn=PT._num)
        self.assertEqual(before, self.snapshot())

    def test_credit_total_increase_matches_input(self):
        self.insert_accounts(("a", 10.0, 7.0), ("b", 20.0, 8.0))
        before = sum(row[1] for row in self.snapshot())
        PSC.credit_shared_cash(self.conn, 1.25, "a", now_fn=lambda: "fixed", num_fn=PT._num)
        after = sum(row[1] for row in self.snapshot())
        self.assertAlmostEqual(1.25, after - before, places=6)

    def test_successful_credit_only_changes_target_cash_and_timestamp(self):
        self.insert_accounts(("a", 10.0, 7.0), ("b", 20.0, 8.0))
        PSC.credit_shared_cash(self.conn, 2.0, "b", now_fn=lambda: "fixed", num_fn=PT._num)
        self.assertEqual(
            [("a", 10.0, 7.0, 7, "running", "old"),
             ("b", 22.0, 8.0, 7, "running", "fixed")],
            self.snapshot(),
        )


class SharedCashFacadeTests(CashDatabaseMixin, unittest.TestCase):
    def test_original_symbols_and_signatures_remain_compatible(self):
        self.assertEqual("(conn, cycle_id=None)", str(inspect.signature(PT._shared_cash)))
        self.assertEqual("(conn, cycle=None)", str(inspect.signature(PT._shared_initial_cash)))
        self.assertEqual(
            "(conn, amount, preferred_account_id=None)",
            str(inspect.signature(PT._debit_shared_cash)),
        )
        self.assertEqual(
            "(conn, amount, account_id)",
            str(inspect.signature(PT._credit_shared_cash)),
        )

    def test_shared_cash_facade_uses_rows_at_call_time(self):
        rows = _rows(("a", 12.0, 0.0))
        with mock.patch.object(PT, "_shared_account_rows", return_value=rows) as get_rows:
            self.assertEqual(12.0, PT._shared_cash(self.conn, 7))
        get_rows.assert_called_once_with(self.conn, 7)

    def test_initial_cash_facade_keeps_declared_capital_short_circuit(self):
        with mock.patch.object(PT, "_active_cycle", return_value={"id": 7, "capital": 100.0}), \
                mock.patch.object(PT, "_shared_account_rows") as get_rows:
            self.assertEqual(100.0, PT._shared_initial_cash(self.conn))
        get_rows.assert_not_called()

    def test_initial_cash_facade_passes_fallback_rows_to_module(self):
        rows = _rows(("a", 12.0, 7.0))
        with mock.patch.object(PT, "_shared_account_rows", return_value=rows):
            self.assertEqual(7.0, PT._shared_initial_cash(self.conn, {"id": 7, "capital": 0.0}))

    def test_debit_facade_delegates_and_uses_current_clock(self):
        rows = _rows(("a", 10.0, 7.0))
        self.insert_accounts(("a", 10.0, 7.0))
        with mock.patch.object(PT, "_shared_account_rows", return_value=rows), \
                mock.patch.object(PT, "_now", return_value="patched-now"):
            self.assertTrue(PT._debit_shared_cash(self.conn, 2.0, preferred_account_id="a"))
        self.assertEqual("patched-now", self.snapshot()[0][5])

    def test_credit_facade_delegates_and_uses_current_clock(self):
        self.insert_accounts(("a", 10.0, 7.0))
        with mock.patch.object(PT, "_now", return_value="patched-now"):
            PT._credit_shared_cash(self.conn, 2.0, "a")
        self.assertEqual(("a", 12.0, 7.0, 7, "running", "patched-now"), self.snapshot()[0])

    def test_facade_passes_current_numeric_normalizer(self):
        rows = _rows(("a", 10.0, 7.0))
        calls = []
        original_num = PT._num

        def patched_num(value, default=0.0):
            calls.append(value)
            return original_num(value, default)

        with mock.patch.object(PT, "_shared_account_rows", return_value=rows), \
                mock.patch.object(PT, "_num", side_effect=patched_num):
            self.assertEqual(10.0, PT._shared_cash(self.conn, 7))
        self.assertTrue(calls)

    def test_facades_have_no_second_cash_algorithm(self):
        tree = ast.parse(TRADING_PATH.read_text(encoding="utf-8"))
        expected = {
            "_shared_cash": "shared_cash",
            "_shared_initial_cash": "shared_initial_cash",
            "_debit_shared_cash": "debit_shared_cash",
            "_credit_shared_cash": "credit_shared_cash",
        }
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in expected
        }
        self.assertEqual(set(expected), set(functions))
        for name, target in expected.items():
            calls = [
                node.func.attr
                for node in ast.walk(functions[name])
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "PSC"
            ]
            self.assertTrue(calls, name)
            self.assertEqual([target], sorted(set(calls)), name)
            self.assertFalse(any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(functions[name])))
            self.assertFalse(any(isinstance(node, ast.Attribute) and node.attr == "execute" for node in ast.walk(functions[name])))


class SharedCashArchitectureGuardTests(unittest.TestCase):
    def test_module_imports_only_stdlib_and_has_no_membership_or_exposure_truth(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertEqual({"__future__", "math"}, roots)
        source = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "enabled_strategies", "paper_cycles", "strategy_definitions",
            "paper_position_lots", "pending_buy_reservations", "shared_account_exposure",
            "risk_exit_account_ids", "ensure_cycle", "ensure_user_strategy_accounts",
        ):
            self.assertNotIn(forbidden, source)

    def test_module_write_sql_is_only_cash_and_timestamp_update(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        sql = [
            node.value.strip().upper()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.strip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "PRAGMA"))
        ]
        self.assertEqual(2, len(sql))
        for statement in sql:
            self.assertTrue(statement.startswith("UPDATE PAPER_ACCOUNTS SET CASH="), statement)
            self.assertIn("UPDATED_AT", statement)
            self.assertIn("WHERE ID=?", statement)
            self.assertNotIn("INITIAL_CASH", statement)

    def test_module_has_no_import_time_business_calls(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        top_level_calls = [node for node in tree.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)]
        self.assertEqual([], top_level_calls)

    def test_architecture_docs_freeze_cash_boundary_and_separation(self):
        source = pathlib.Path(__file__).parents[1].joinpath("ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertIn("cycle ownership != execution eligibility", source)
        self.assertIn("shared cash accounting", source)
        self.assertIn("open-order reservation", source)
        self.assertIn("position exposure", source)
        self.assertIn("risk-exit eligibility", source)
        self.assertIn("paper_trading` → `paper_shared_cash", source)


if __name__ == "__main__":
    unittest.main()
