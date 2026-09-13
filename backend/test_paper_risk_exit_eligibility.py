# -*- coding: utf-8 -*-
"""Comprehensive contract tests for paper risk exit eligibility read model.

Freezes all required contracts A through N:
  A. execution participant without holdings -> still belongs to risk exit scope
  B. paused account with positive remaining lots -> must belong
  C. archived/retired account with positive remaining lots -> must belong
  D. account outside current cycle with positive remaining lots -> must belong
  E. account outside current cycle with zero remaining -> does not belong
  F. negative remaining qty -> does not confer eligibility
  G. multiple lots for same account -> deduplicated
  H. mixed accounts -> union correctness
  I. facade signature preserved (conn, status='running')
  J. status parameter passed through with exact old semantics
  K. facade uses call-time dependency resolution (monkeypatches respected)
  L. module is strictly read-only (zero write SQL via authorizer and AST)
  M. module does not reverse-import paper_trading (stdlib-only)
  N. module does not reimplement cycle ownership or execution participation
"""
from __future__ import annotations

import ast
import inspect
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import paper_risk_exit_eligibility as PRE
import paper_trading as PT

MODULE_PATH = Path(__file__).resolve().parent / "paper_risk_exit_eligibility.py"
OWNERSHIP_PATH = Path(__file__).resolve().parent / "paper_cycle_ownership.py"


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE paper_position_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            code TEXT,
            remaining_qty REAL DEFAULT 0,
            lot_type TEXT DEFAULT 'normal'
        )"""
    )
    conn.execute(
        """CREATE TABLE paper_accounts (
            id TEXT PRIMARY KEY,
            cycle_id INTEGER,
            status TEXT DEFAULT 'running',
            cash REAL DEFAULT 1000000.0,
            initial_cash REAL DEFAULT 1000000.0
        )"""
    )
    conn.execute(
        """CREATE TABLE strategy_definitions (
            id TEXT PRIMARY KEY,
            lifecycle_status TEXT DEFAULT 'running',
            supports_new_cycle INTEGER DEFAULT 1
        )"""
    )
    conn.execute(
        """CREATE TABLE paper_cycles (
            id INTEGER PRIMARY KEY,
            enabled_strategies TEXT,
            status TEXT DEFAULT 'active'
        )"""
    )
    return conn


class RiskExitEligibilityContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def _add_lot(self, account_id: str, remaining_qty: float, code: str = "600000"):
        self.conn.execute(
            "INSERT INTO paper_position_lots (account_id, code, remaining_qty) VALUES (?, ?, ?)",
            (account_id, code, remaining_qty),
        )

    # Contract A: execution participant without holdings -> still belongs to risk exit scope
    def test_A_execution_participant_without_holdings_is_eligible(self):
        res = PRE.risk_exit_account_ids(self.conn, base_account_ids=["user_exec_1"])
        self.assertIn("user_exec_1", res)
        self.assertEqual(res, {"user_exec_1"})

    # Contract B: paused account with positive remaining lots -> must belong
    def test_B_paused_account_with_positive_remaining_lots_is_eligible(self):
        self._add_lot("user_paused_1", 100.0)
        # Caller supplies only currently active accounts (paused is excluded from base_account_ids)
        res = PRE.risk_exit_account_ids(self.conn, base_account_ids=["user_active_1"])
        self.assertIn("user_active_1", res)
        self.assertIn("user_paused_1", res, "Paused account with positive remaining lots must be eligible")

    # Contract C: archived/retired account with positive remaining lots -> must belong
    def test_C_archived_retired_account_with_positive_remaining_lots_is_eligible(self):
        self._add_lot("user_archived_1", 50.0)
        res = PRE.risk_exit_account_ids(self.conn, base_account_ids=[])
        self.assertIn("user_archived_1", res, "Archived account with remaining lots must be scanned for risk exit")

    # Contract D: account outside current cycle with positive remaining lots -> must belong
    def test_D_account_outside_current_cycle_with_positive_remaining_lots_is_eligible(self):
        self._add_lot("user_old_cycle", 200.0)
        res = PRE.risk_exit_account_ids(self.conn, base_account_ids=["user_new_cycle"])
        self.assertIn("user_new_cycle", res)
        self.assertIn("user_old_cycle", res)

    # Contract E: account outside current cycle with zero remaining -> does not belong
    def test_E_account_outside_current_cycle_with_zero_remaining_is_not_eligible(self):
        self._add_lot("user_flat", 0.0)
        res = PRE.risk_exit_account_ids(self.conn, base_account_ids=["user_active_1"])
        self.assertIn("user_active_1", res)
        self.assertNotIn("user_flat", res, "Account outside execution scope with zero remaining lots must not be eligible")

    # Contract F: negative remaining qty -> does not confer eligibility
    def test_F_negative_remaining_qty_does_not_confer_eligibility(self):
        self._add_lot("user_negative", -10.0)
        res = PRE.risk_exit_account_ids(self.conn, base_account_ids=["user_active_1"])
        self.assertIn("user_active_1", res)
        self.assertNotIn("user_negative", res, "Negative remaining qty must not confer risk exit eligibility")

    # Contract G: multiple lots for same account -> deduplicated
    def test_G_multiple_lots_for_same_account_are_deduplicated(self):
        self._add_lot("user_multi", 100.0, "600000")
        self._add_lot("user_multi", 200.0, "600001")
        self._add_lot("user_multi", 50.0, "600002")
        res = PRE.risk_exit_account_ids(self.conn, base_account_ids=[])
        self.assertEqual(res, {"user_multi"})
        self.assertIsInstance(res, set)

    # Contract H: mixed accounts -> union correctness
    def test_H_mixed_accounts_union_correctness(self):
        self._add_lot("acc_overlap", 100.0)  # in base AND has lots
        self._add_lot("acc_lot_only", 50.0)   # not in base, has lots
        self._add_lot("acc_zero_lot", 0.0)    # not in base, zero lots
        self._add_lot("acc_neg_lot", -5.0)    # not in base, neg lots

        base_ids = ["acc_exec_only", "acc_overlap"]
        res = PRE.risk_exit_account_ids(self.conn, base_account_ids=base_ids)
        expected = {"acc_exec_only", "acc_overlap", "acc_lot_only"}
        self.assertEqual(res, expected)

    # Row reader compatibility (bare sqlite connection without row_factory)
    def test_bare_sqlite_connection_compatibility(self):
        bare_conn = sqlite3.connect(":memory:")
        self.addCleanup(bare_conn.close)
        bare_conn.execute("CREATE TABLE paper_position_lots (account_id TEXT, remaining_qty REAL)")
        bare_conn.execute("INSERT INTO paper_position_lots VALUES ('bare_acc', 100.0)")

        res = PRE.risk_exit_account_ids(bare_conn, base_account_ids=["base_1"])
        self.assertEqual(res, {"base_1", "bare_acc"})

    # Custom rows_fn dependency injection (dict rows and tuple rows)
    def test_custom_rows_fn_injection(self):
        mock_rows_fn = mock.MagicMock(return_value=[
            {"account_id": "dict_acc_1"},
            {"account_id": "dict_acc_2"},
            {"account_id": None},
        ])
        res = PRE.risk_exit_account_ids(
            self.conn,
            base_account_ids=["base_acc"],
            rows_fn=mock_rows_fn,
        )
        self.assertEqual(res, {"base_acc", "dict_acc_1", "dict_acc_2"})
        mock_rows_fn.assert_called_once()

    # Exact legacy account ID semantics (Review 1/2 P3 regression)
    def test_exact_legacy_account_id_semantics(self):
        # 1. base ID "  base_id  " remains exactly "  base_id  " (no strip)
        # 2. base values retain Python identity semantics (not forced through str())
        sentinel_base = object()
        base_ids = ["  base_id  ", "normal_base", sentinel_base]

        # 3. holding ID "  held_id  " remains exactly "  held_id  " (no strip)
        self._add_lot("  held_id  ", 10.0)
        # 4. holding whitespace-only "   " remains present because truthy
        self._add_lot("   ", 20.0)
        # 5. holding empty string is excluded
        self._add_lot("", 30.0)
        # 6. holding None is excluded
        self.conn.execute("INSERT INTO paper_position_lots (account_id, remaining_qty) VALUES (NULL, 40.0)")
        # 7. ordinary canonical IDs remain unchanged
        self._add_lot("normal_held", 50.0)

        res = PRE.risk_exit_account_ids(self.conn, base_account_ids=base_ids)

        self.assertIn("  base_id  ", res)
        self.assertNotIn("base_id", res)
        self.assertIn("normal_base", res)
        self.assertIn(sentinel_base, res)

        self.assertIn("  held_id  ", res)
        self.assertNotIn("held_id", res)
        self.assertIn("   ", res)
        self.assertNotIn("", res)
        self.assertNotIn(None, res)
        self.assertIn("normal_held", res)

        self.assertEqual(res, {"  base_id  ", "normal_base", sentinel_base, "  held_id  ", "   ", "normal_held"})

    def test_raw_account_id_extraction_without_normalization(self):
        self.assertEqual(PRE._raw_account_id({"account_id": "  foo  "}), "  foo  ")
        self.assertEqual(PRE._raw_account_id(("  bar  ",)), "  bar  ")
        self.assertEqual(PRE._raw_account_id({"account_id": "   "}), "   ")
        self.assertEqual(PRE._raw_account_id({"account_id": ""}), "")
        self.assertIsNone(PRE._raw_account_id({"account_id": None}))


class RiskExitEligibilityFacadeContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def _add_account(self, account_id: str, cycle_id: int = 1, status: str = "running"):
        self.conn.execute(
            "INSERT INTO paper_accounts (id, cycle_id, status) VALUES (?, ?, ?)",
            (account_id, cycle_id, status),
        )

    def _add_lot(self, account_id: str, remaining_qty: float):
        self.conn.execute(
            "INSERT INTO paper_position_lots (account_id, remaining_qty) VALUES (?, ?)",
            (account_id, remaining_qty),
        )

    # Contract I: facade signature preserved (conn, status='running')
    def test_I_facade_signature_preserved(self):
        sig = inspect.signature(PT._risk_exit_account_ids)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["conn", "status"])
        self.assertEqual(sig.parameters["status"].default, "running")

    # Contract J: status parameter passed through with exact old semantics
    def test_J_status_parameter_passthrough(self):
        with mock.patch.object(PT, "_active_account_ids", return_value=["mock_id"]) as mock_active:
            res = PT._risk_exit_account_ids(self.conn, status="paused")
            mock_active.assert_called_once_with(self.conn, status="paused")
            self.assertIn("mock_id", res)

        with mock.patch.object(PT, "_active_account_ids", return_value=["mock_none"]) as mock_active:
            res = PT._risk_exit_account_ids(self.conn, status=None)
            mock_active.assert_called_once_with(self.conn, status=None)
            self.assertIn("mock_none", res)

    # Contract K: facade uses call-time dependency resolution (monkeypatches respected)
    def test_K_facade_uses_call_time_dependency_resolution(self):
        self._add_lot("holding_account", 10.0)

        # Call-time monkeypatch of _active_account_ids
        with mock.patch.object(PT, "_active_account_ids", return_value=["dynamic_exec"]):
            res = PT._risk_exit_account_ids(self.conn)
            self.assertEqual(res, {"dynamic_exec", "holding_account"})

        # Subsequent call with different mock returns updated set
        with mock.patch.object(PT, "_active_account_ids", return_value=["another_exec"]):
            res = PT._risk_exit_account_ids(self.conn)
            self.assertEqual(res, {"another_exec", "holding_account"})


class RiskExitEligibilityArchitectureGuardTests(unittest.TestCase):
    # Contract L: module is strictly read-only (zero write SQL via authorizer and AST)
    def test_L_module_is_read_only(self):
        conn = _make_conn()
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO paper_position_lots (account_id, remaining_qty) VALUES ('acc1', 100)")

        write_ops_attempted = []

        def authorizer(action_code, *args):
            # Block and record any write operations
            if action_code in (
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_ATTACH,
                sqlite3.SQLITE_DETACH,
                sqlite3.SQLITE_ALTER_TABLE,
                sqlite3.SQLITE_DROP_TABLE,
                sqlite3.SQLITE_CREATE_TABLE,
            ):
                write_ops_attempted.append(action_code)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        res = PRE.risk_exit_account_ids(conn, base_account_ids=["base1"])
        self.assertEqual(res, {"base1", "acc1"})
        self.assertEqual(write_ops_attempted, [], "Module must not issue any write SQL operations")

        # AST inspection for write SQL keywords
        code = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value.upper()
                for kw in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE", "ALTER TABLE", "PRAGMA"]:
                    self.assertNotIn(kw, s, f"Forbidden write SQL keyword {kw!r} found in module string constants")

    # Contract M: module does not reverse-import paper_trading (stdlib-only)
    def test_M_module_stdlib_only_and_no_reverse_import(self):
        code = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(code)
        allowed_modules = {"__future__", "typing"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    self.assertIn(root, allowed_modules, f"Disallowed import {alias.name!r} in {MODULE_PATH.name}")
                    self.assertNotEqual(root, "paper_trading", "Reverse import of paper_trading is strictly forbidden")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split(".")[0]
                    self.assertIn(root, allowed_modules, f"Disallowed from-import {node.module!r} in {MODULE_PATH.name}")
                    self.assertNotEqual(root, "paper_trading", "Reverse import of paper_trading is strictly forbidden")

    # Contract N: module does not reimplement cycle ownership or execution participation
    def test_N_module_does_not_reimplement_cycle_or_execution(self):
        code = MODULE_PATH.read_text(encoding="utf-8")
        forbidden_identifiers = [
            "paper_cycles",
            "paper_accounts",
            "strategy_definitions",
            "enabled_strategies",
            "lifecycle_status",
            "supports_new_cycle",
            "cycle_ledger_filter",
            "current_cycle_participant_ids",
            "execution_participant_ids",
            "paper_shared_cash",
            "paper_capital_reservations",
            "paper_slot_occupancy",
        ]
        for ident in forbidden_identifiers:
            self.assertNotIn(ident, code, f"Forbidden concept/identifier {ident!r} found in {MODULE_PATH.name}")


if __name__ == "__main__":
    unittest.main()
