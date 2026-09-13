# -*- coding: utf-8 -*-
import ast
import inspect
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import paper_trading as PT
import paper_user_account_provisioning as PUAP
import user_strategy_participation as USP


USER_A = "user_alpha"
USER_B = "user_beta"
USER_C = "user_gamma"
NOW = "2026-09-13T10:00:00+08:00"


def _spec(account_id):
    return {
        "name": f"展示 {account_id}",
        "source_strategy": f"dsl_{account_id}",
        "cycle_days": 9,
        "max_positions": 6,
        "max_weight": 0.31,
        "max_exposure": 0.77,
        "risk_profile": "balanced",
        "strategy_version": "dsl-2026.09-custom",
        "dsl_version": "2026.09",
    }


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE paper_accounts(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, source_strategy TEXT NOT NULL,
            status TEXT NOT NULL, initial_cash REAL NOT NULL, cash REAL NOT NULL,
            cycle_days INTEGER NOT NULL, max_positions INTEGER NOT NULL,
            max_weight REAL NOT NULL, max_exposure REAL NOT NULL,
            risk_profile TEXT NOT NULL, version TEXT NOT NULL,
            benchmark_start REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            cycle_id INTEGER, params TEXT, mode TEXT, style TEXT
        );
        """
    )
    return conn


def _account_row(conn, account_id):
    return conn.execute(
        """SELECT id,name,source_strategy,status,initial_cash,cash,cycle_days,max_positions,
                  max_weight,max_exposure,risk_profile,version,benchmark_start,created_at,
                  updated_at,cycle_id,params,mode,style
           FROM paper_accounts WHERE id=?""",
        (account_id,),
    ).fetchone()


class ProvisioningContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_missing_account_matches_full_golden_row(self):
        PUAP.provision_user_accounts(
            self.conn, (USER_A,), spec_for=_spec, now_fn=lambda: NOW,
            audit_fn=lambda *_args: None,
        )
        self.assertEqual(
            (USER_A, f"展示 {USER_A}", f"dsl_{USER_A}", "paused", 0.0, 0.0, 9, 6,
             0.31, 0.77, "balanced", "dsl-2026.09-custom", None, NOW, NOW,
             None, None, None, None),
            _account_row(self.conn, USER_A),
        )

    def test_existing_account_is_untouched_and_skips_callbacks(self):
        before = (USER_A, "old name", "old source", "running", 123.0, 99.0, 3, 2,
                  0.2, 0.4, "old-risk", "old-version", 12.5, "old-created",
                  "old-updated", 44, "old-params", "old-mode", "old-style")
        self.conn.execute(
            """INSERT INTO paper_accounts
               (id,name,source_strategy,status,initial_cash,cash,cycle_days,max_positions,
                max_weight,max_exposure,risk_profile,version,benchmark_start,created_at,
                updated_at,cycle_id,params,mode,style) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            before,
        )
        spec_for = mock.Mock(side_effect=AssertionError("existing row resolved a spec"))
        audit_fn = mock.Mock(side_effect=AssertionError("existing row audited"))
        PUAP.provision_user_accounts(
            self.conn, (USER_A,), spec_for=spec_for, now_fn=mock.Mock(), audit_fn=audit_fn,
        )
        self.assertEqual(before, _account_row(self.conn, USER_A))
        spec_for.assert_not_called()
        audit_fn.assert_not_called()

    def test_multiple_accounts_only_missing_rows_follow_input_order(self):
        self.conn.execute(
            "INSERT INTO paper_accounts "
            "(id,name,source_strategy,status,initial_cash,cash,cycle_days,max_positions,"
            "max_weight,max_exposure,risk_profile,version,benchmark_start,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (USER_A, "existing", "existing", "paused", 0.0, 0.0, 1, 1, 0.1, 0.2,
             "safe", "existing", None, NOW, NOW),
        )
        resolved = []
        audited = []

        def spec_for(account_id):
            resolved.append(account_id)
            return _spec(account_id)

        def audit_fn(_conn, account_id, event, detail):
            audited.append((account_id, event, detail))

        PUAP.provision_user_accounts(
            self.conn, (USER_A, USER_B, USER_C), spec_for=spec_for,
            now_fn=lambda: NOW, audit_fn=audit_fn,
        )
        self.assertEqual([USER_B, USER_C], resolved)
        self.assertEqual([USER_B, USER_C], [item[0] for item in audited])
        self.assertTrue(all(item[1] == "user_strategy_account_provisioned" for item in audited))
        self.assertEqual(
            f"用户策略 展示 {USER_B} 已开户（paused，等待周期分配资金；DSL v2026.09）",
            audited[0][2],
        )
        self.assertEqual((USER_B, USER_C), tuple(row[0] for row in self.conn.execute(
            "SELECT id FROM paper_accounts WHERE id IN (?,?) ORDER BY rowid", (USER_B, USER_C)
        )))

    def test_empty_input_performs_no_sql_mutation(self):
        before = self.conn.total_changes
        statements = []
        self.conn.set_trace_callback(statements.append)
        PUAP.provision_user_accounts(
            self.conn, (), spec_for=mock.Mock(), now_fn=mock.Mock(), audit_fn=mock.Mock(),
        )
        self.assertEqual(before, self.conn.total_changes)
        self.assertEqual([], statements)

    def test_repeated_call_is_idempotent(self):
        spec_for = mock.Mock(side_effect=_spec)
        now_fn = mock.Mock(return_value=NOW)
        audit_fn = mock.Mock()
        args = (self.conn, (USER_A,),)
        kwargs = {"spec_for": spec_for, "now_fn": now_fn, "audit_fn": audit_fn}
        PUAP.provision_user_accounts(*args, **kwargs)
        self.assertEqual(1, self.conn.total_changes)
        self.assertEqual(1, audit_fn.call_count)
        spec_for.reset_mock()
        now_fn.reset_mock()
        audit_fn.reset_mock()
        PUAP.provision_user_accounts(*args, **kwargs)
        self.assertEqual(1, self.conn.total_changes)
        spec_for.assert_not_called()
        now_fn.assert_not_called()
        audit_fn.assert_not_called()

    def test_spec_failure_propagates_without_insert_or_audit(self):
        audit_fn = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "spec failure"):
            PUAP.provision_user_accounts(
                self.conn, (USER_A,),
                spec_for=mock.Mock(side_effect=RuntimeError("spec failure")),
                now_fn=mock.Mock(return_value=NOW), audit_fn=audit_fn,
            )
        self.assertIsNone(_account_row(self.conn, USER_A))
        audit_fn.assert_not_called()

    def test_insert_failure_propagates_without_audit(self):
        self.conn.execute(
            """CREATE TRIGGER reject_user_account BEFORE INSERT ON paper_accounts
               BEGIN SELECT RAISE(ABORT, 'insert failure'); END;"""
        )
        audit_fn = mock.Mock()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "insert failure"):
            PUAP.provision_user_accounts(
                self.conn, (USER_A,), spec_for=_spec, now_fn=lambda: NOW, audit_fn=audit_fn,
            )
        audit_fn.assert_not_called()
        self.assertIsNone(_account_row(self.conn, USER_A))

    def test_audit_failure_propagates_without_commit_or_rollback(self):
        with self.assertRaisesRegex(RuntimeError, "audit failure"):
            PUAP.provision_user_accounts(
                self.conn, (USER_A,), spec_for=_spec, now_fn=lambda: NOW,
                audit_fn=mock.Mock(side_effect=RuntimeError("audit failure")),
            )
        self.assertTrue(self.conn.in_transaction)
        self.assertIsNotNone(_account_row(self.conn, USER_A))
        self.conn.rollback()
        self.assertIsNone(_account_row(self.conn, USER_A))


class FacadeContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_facade_reads_all_dependencies_at_call_time(self):
        spec = _spec(USER_A)
        with mock.patch.object(USP, "user_participant_ids", return_value=(USER_A,)) as ids:
            with mock.patch.object(PT, "_spec_for", return_value=spec) as spec_for:
                with mock.patch.object(PT, "_now", return_value=NOW) as now_fn:
                    with mock.patch.object(PT, "_audit") as audit_fn:
                        PT._ensure_user_strategy_accounts(self.conn)
        ids.assert_called_once_with(self.conn)
        spec_for.assert_called_once_with(USER_A, conn=self.conn)
        now_fn.assert_called_once_with()
        audit_fn.assert_called_once()
        self.assertEqual(USER_A, _account_row(self.conn, USER_A)[0])

    def test_legacy_facade_signature_is_preserved(self):
        self.assertEqual(["conn"], list(inspect.signature(PT._ensure_user_strategy_accounts).parameters))


class ArchitectureGuardTests(unittest.TestCase):
    MODULE_PATH = Path(__file__).with_name("paper_user_account_provisioning.py")
    TRADING_PATH = Path(__file__).with_name("paper_trading.py")

    def test_module_is_stdlib_free_of_imports_and_forbidden_boundaries(self):
        source = self.MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertFalse([node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))])
        forbidden = (
            "paper_trading", "strategy_runtime", "paper_cycle_ownership", "paper_shared_cash",
            "strategy_definitions", "supports_new_cycle", "lifecycle_status", "paper_cycles",
            "enabled_strategies", "paper_position_lots", "paper_orders", "paper_fills", "paper_nav",
            "_ensure_cycle", "_debit_shared_cash", "_credit_shared_cash", "cycle_id",
            "commit", "rollback",
        )
        for item in forbidden:
            self.assertNotIn(item, source)
        sql_literals = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and any(keyword in node.value.upper() for keyword in ("SELECT", "INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(2, len(sql_literals))
        insert_literals = [item for item in sql_literals if "INSERT" in item.upper()]
        self.assertEqual(1, len(insert_literals))
        self.assertIn("INSERT INTO paper_accounts", insert_literals[0])
        self.assertFalse(any(re.search(r"\b(?:UPDATE|DELETE|REPLACE)\b", item, re.I)
                             for item in sql_literals))
        self.assertFalse(any(isinstance(node, ast.Call) for node in tree.body))

    def test_facade_is_only_a_call_time_delegate_without_inline_sql(self):
        source = self.TRADING_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_ensure_user_strategy_accounts")
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        delegates = [node for node in calls if isinstance(node.func, ast.Attribute)
                     and isinstance(node.func.value, ast.Name)
                     and node.func.value.id == "PUAP" and node.func.attr == "provision_user_accounts"]
        self.assertEqual(1, len(delegates))
        self.assertFalse(any(isinstance(node.func, ast.Attribute) and node.func.attr == "execute"
                             for node in calls))
        self.assertNotIn("INSERT", ast.get_source_segment(source, function) or "")

    def test_provisioning_event_has_one_production_implementation(self):
        production = [
            path for path in self.MODULE_PATH.parent.glob("*.py")
            if not path.name.startswith("test_")
        ]
        counts = {path.name: path.read_text(encoding="utf-8").count("user_strategy_account_provisioned")
                  for path in production}
        self.assertEqual(1, counts["paper_user_account_provisioning.py"])
        self.assertEqual(0, counts["paper_trading.py"])
        self.assertEqual(1, sum(counts.values()))

    def test_compatibility_owners_remain_in_paper_trading(self):
        tree = ast.parse(self.TRADING_PATH.read_text(encoding="utf-8"))
        names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertTrue({"_ensure_accounts", "_spec_for", "_ensure_cycle"} <= names)


if __name__ == "__main__":
    unittest.main()
