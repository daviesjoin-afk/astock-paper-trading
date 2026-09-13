# -*- coding: utf-8 -*-
"""Contract tests for the fixed-cycle capital attribution boundary."""
from __future__ import annotations

import ast
import inspect
import pathlib
import sqlite3
import unittest
from unittest import mock

import paper_cycle_capital as PCC
import paper_trading as PT


MODULE_PATH = pathlib.Path(__file__).with_name("paper_cycle_capital.py")


def ownership_filter(conn, cycle_id, column="id"):
    return "1=1", ()


class CycleCapitalDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE paper_accounts(id TEXT, cycle_id INTEGER, initial_cash REAL, cash REAL)"
        )
        self.cycle = {"id": 1, "capital": 300000.0}
        self.addCleanup(self.conn.close)

    def add_account(self, account_id, initial_cash, cash=None, cycle_id=1):
        self.conn.execute(
            "INSERT INTO paper_accounts VALUES(?,?,?,?)",
            (account_id, cycle_id, initial_cash, initial_cash if cash is None else cash),
        )

    def available(self, account_id="target", builtin=("a", "b", "c", "d"), filter_fn=ownership_filter):
        return PCC.available_cycle_ledger_capital(
            self.conn, self.cycle, account_id,
            cycle_ledger_filter_fn=filter_fn,
            builtin_account_ids=builtin,
            num_fn=lambda value, default=0.0: float(value if value is not None else default),
        )

    def late_join(self, account_id="target", builtin=("a", "b", "c", "d"), filter_fn=ownership_filter):
        return PCC.late_join_reference_capital(
            self.conn, self.cycle, account_id,
            cycle_ledger_filter_fn=filter_fn,
            builtin_account_ids=builtin,
            num_fn=lambda value, default=0.0: float(value if value is not None else default),
        )

    def test_available_without_other_rows_uses_builtin_fallback(self):
        self.assertEqual(75000.0, self.available())

    def test_empty_builtin_scope_keeps_zero_divisor_guard(self):
        self.assertEqual(300000.0, self.available(builtin=()))

    def test_zero_initial_cash_row_still_counts_as_ownership(self):
        self.add_account("zero", 0.0)
        self.assertEqual(300000.0, self.available())

    def test_multiple_funded_rows_subtract_initial_cash(self):
        self.add_account("a", 70000.0)
        self.add_account("b", 80000.0)
        self.assertEqual(150000.0, self.available())

    def test_overallocation_is_clamped_to_zero(self):
        self.add_account("a", 200000.0)
        self.add_account("b", 150000.0)
        self.assertEqual(0.0, self.available())

    def test_available_excludes_target_account(self):
        self.add_account("target", 300000.0)
        self.assertEqual(75000.0, self.available())

    def test_non_owned_same_cycle_row_is_excluded(self):
        self.add_account("not-owned", 300000.0)
        self.assertEqual(75000.0, self.available(filter_fn=lambda conn, cycle_id, column: ("id=?", ("owned",))))

    def test_available_reads_initial_cash_not_cash(self):
        self.add_account("a", 50000.0, cash=250000.0)
        self.assertEqual(250000.0, self.available())

    def test_late_join_only_counts_positive_initial_cash(self):
        self.add_account("zero", 0.0, cash=100000.0)
        self.add_account("negative", -10.0, cash=100000.0)
        self.add_account("funded", 70000.0)
        self.assertEqual(70000.0, self.late_join())

    def test_late_join_funded_average(self):
        self.add_account("a", 70000.0)
        self.add_account("b", 80000.0)
        self.assertEqual(75000.0, self.late_join())

    def test_late_join_funded_average_rounds_to_two_places(self):
        self.add_account("a", 10000.0)
        self.add_account("b", 10001.0)
        self.assertEqual(10000.5, self.late_join())

    def test_late_join_without_funded_row_uses_rounded_fallback(self):
        self.cycle["capital"] = 100001.0
        self.assertEqual(25000.25, self.late_join())

    def test_late_join_excludes_target_account(self):
        self.add_account("target", 100000.0)
        self.add_account("a", 70000.0)
        self.assertEqual(70000.0, self.late_join())

    def test_late_join_ownership_filter_applies(self):
        self.add_account("a", 70000.0)
        self.add_account("b", 80000.0)
        self.assertEqual(70000.0, self.late_join(filter_fn=lambda conn, cycle_id, column: ("id=?", ("a",))))

    def test_late_join_reads_initial_cash_not_cash(self):
        self.add_account("a", 70000.0, cash=200000.0)
        self.assertEqual(70000.0, self.late_join())

    def test_filter_callback_runs_before_capital_sql(self):
        statements = []
        self.conn.set_trace_callback(statements.append)

        def failing_filter(conn, cycle_id, column):
            raise RuntimeError("filter failed")

        with self.assertRaisesRegex(RuntimeError, "filter failed"):
            self.available(filter_fn=failing_filter)
        self.assertEqual([], statements)

    def test_module_is_read_only_and_stdlib_only(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(["__future__", "typing"], [getattr(node, "module", None) for node in imports])
        self.assertNotRegex(MODULE_PATH.read_text(encoding="utf-8"), r"\b(INSERT|UPDATE|DELETE|COMMIT|ROLLBACK)\b")

    def test_module_has_no_forbidden_reverse_dependencies(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in ("paper_trading", "paper_cycle_ownership", "paper_allocation", "paper_shared_cash", "paper_capital_reservations"):
            self.assertNotIn(forbidden, source)


class CycleCapitalFacadeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE paper_accounts(id TEXT, cycle_id INTEGER, initial_cash REAL, cash REAL)")
        self.cycle = {"id": 1, "capital": 300000.0}
        self.addCleanup(self.conn.close)

    def test_available_facade_signature_is_unchanged(self):
        self.assertEqual(["conn", "cycle", "account_id"], list(inspect.signature(PT._available_cycle_ledger_capital).parameters))

    def test_late_join_facade_signature_is_unchanged(self):
        self.assertEqual(["conn", "cycle", "account_id"], list(inspect.signature(PT._late_join_reference_capital).parameters))

    def test_facade_resolves_filter_at_call_time(self):
        with mock.patch.object(PT, "_active_cycle_filter", side_effect=RuntimeError("patched filter")):
            with self.assertRaisesRegex(RuntimeError, "patched filter"):
                PT._available_cycle_ledger_capital(self.conn, self.cycle, "target")

    def test_facade_resolves_builtin_scope_at_call_time(self):
        with mock.patch.object(PT, "_active_cycle_filter", return_value=("1=1", ())):
            with mock.patch.object(PT, "ACTIVE_ACCOUNT_IDS", ()):
                self.assertEqual(300000.0, PT._available_cycle_ledger_capital(self.conn, self.cycle, "target"))

    def test_facade_resolves_num_at_call_time(self):
        calls = []

        def patched_num(value, default=0.0):
            calls.append((value, default))
            return float(value if value is not None else default)

        with mock.patch.object(PT, "_active_cycle_filter", return_value=("1=1", ())):
            with mock.patch.object(PT, "_num", side_effect=patched_num):
                self.assertEqual(60000.0, PT._available_cycle_ledger_capital(self.conn, self.cycle, "target"))
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
