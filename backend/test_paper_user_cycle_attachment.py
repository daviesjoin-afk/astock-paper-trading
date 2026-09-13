# -*- coding: utf-8 -*-
"""Contract tests for the user paper-account cycle attachment boundary."""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import paper_trading as PT
import paper_user_cycle_attachment as PUCA
import user_strategy_participation as USP


USER_A = "cycle_user_a"
USER_B = "cycle_user_b"
OTHER_CYCLE = 99
NOWS = ("2026-09-13T10:00:01+08:00", "2026-09-13T10:00:02+08:00", "2026-09-13T10:00:03+08:00")
DAY = dt.date(2026, 9, 13)
CYCLE = {"id": 7, "cycle_key": "cycle-7", "status": "paused", "capital": 1000.0}


def _spec(account_id):
    return {
        "name": f"展示 {account_id}",
        "mode": "dsl",
        "default_style": "momentum",
        "risk_profile": "balanced",
        "strategy_version": "dsl-2026.09",
        "max_positions": 7,
        "max_weight": 0.44,
        "max_exposure": 0.88,
        "lifecycle_stage": "active",
    }


def _make_conn(row_factory=None):
    conn = sqlite3.connect(":memory:")
    if row_factory is not None:
        conn.row_factory = row_factory
    conn.executescript(
        """
        CREATE TABLE paper_accounts(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, source_strategy TEXT NOT NULL,
            status TEXT NOT NULL, initial_cash REAL NOT NULL, cash REAL NOT NULL,
            cycle_days INTEGER NOT NULL, max_positions INTEGER NOT NULL,
            max_weight REAL NOT NULL, max_exposure REAL NOT NULL,
            risk_profile TEXT NOT NULL, version TEXT NOT NULL,
            benchmark_start REAL, daily_start_nav REAL, daily_nav_date TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            cycle_id INTEGER, mode TEXT, style TEXT, params TEXT
        );
        CREATE TABLE paper_nav(
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, nav_date TEXT,
            cash REAL, market_value REAL, nav REAL, benchmark REAL, created_at TEXT
        );
        CREATE TABLE paper_parameter_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, account_id TEXT,
            version TEXT, style TEXT, params TEXT, reason TEXT,
            effective_date TEXT, created_at TEXT
        );
        """
    )
    return conn


def _insert_account(conn, account_id=USER_A, *, cycle_id=None, status="paused",
                    initial_cash=10.0, cash=9.0, params="old-params"):
    conn.execute(
        """INSERT INTO paper_accounts
           (id,name,source_strategy,status,initial_cash,cash,cycle_days,max_positions,
            max_weight,max_exposure,risk_profile,version,benchmark_start,daily_start_nav,
            daily_nav_date,created_at,updated_at,cycle_id,mode,style,params)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (account_id, "旧名称", "旧来源", status, initial_cash, cash, 11, 3, 0.2, 0.5,
         "old-risk", "old-version", 12.5, 12.5, "2026-09-12", "old-created", "old-updated", cycle_id,
         "old-mode", "old-style", params),
    )


def _account(conn, account_id=USER_A):
    return conn.execute(
        """SELECT id,name,source_strategy,status,initial_cash,cash,cycle_days,max_positions,
                  max_weight,max_exposure,risk_profile,version,benchmark_start,created_at,
                  updated_at,cycle_id,mode,style,params
           FROM paper_accounts WHERE id=?""",
        (account_id,),
    ).fetchone()


def _run(conn, *, enabled_ids=(USER_A,), user_account_ids=(USER_A,), cycle=CYCLE,
         spec_for=None, available_capital_fn=None, benchmark_fn=None,
         num_fn=None, date_fn=None, now_fn=None, audit_fn=None,
         builtin_active_ids=()):
    spec_for = spec_for or _spec
    available_capital_fn = available_capital_fn or (lambda _account_id: 123.45)
    benchmark_fn = benchmark_fn or (lambda: 321.09)
    num_fn = num_fn or (lambda value, default=0.0: float(value if value is not None else default))
    date_fn = date_fn or (lambda: DAY)
    now_fn = now_fn or (lambda: NOWS[0])
    audit_fn = audit_fn or (lambda *_args: None)
    return PUCA.reconcile_user_cycle_accounts(
        conn, cycle, enabled_ids, user_account_ids,
        builtin_active_ids=builtin_active_ids, spec_for=spec_for,
        available_capital_fn=available_capital_fn, benchmark_fn=benchmark_fn,
        num_fn=num_fn, date_fn=date_fn, now_fn=now_fn, audit_fn=audit_fn,
    )


class AttachContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)
        _insert_account(self.conn)

    def test_normal_cycle_uses_available_capital_and_golden_account_fields(self):
        available = mock.Mock(return_value=123.45)
        benchmark = mock.Mock(return_value=321.09)
        now_fn = mock.Mock(side_effect=NOWS)
        audit = mock.Mock()
        _run(self.conn, available_capital_fn=available, benchmark_fn=benchmark,
             now_fn=now_fn, audit_fn=audit)
        row = _account(self.conn)
        self.assertEqual(
            (USER_A, "旧名称", "旧来源", "paused", 123.45, 123.45, 11, 7,
             0.44, 0.88, "balanced", "dsl-2026.09", 321.09, "old-created",
             NOWS[0], 7, "dsl", "momentum", "old-params"),
            row,
        )
        available.assert_called_once_with(USER_A)
        benchmark.assert_called_once_with()
        self.assertEqual(3, now_fn.call_count)
        self.assertEqual(1, audit.call_count)

    def test_legacy_cycle_divides_by_enabled_ids_only(self):
        cycle = {**CYCLE, "cycle_key": "legacy-20260913", "capital": 1000.0}
        available = mock.Mock(side_effect=AssertionError("legacy must not use normal capital"))
        _run(self.conn, cycle=cycle, enabled_ids=(USER_A, USER_B),
             available_capital_fn=available)
        self.assertEqual(500.0, _account(self.conn)[4])
        self.assertEqual(500.0, _account(self.conn)[5])
        available.assert_not_called()

    def test_normal_cycle_zero_available_capital_attaches_without_minting(self):
        available = mock.Mock(return_value=0.0)
        _run(self.conn, available_capital_fn=available)
        self.assertEqual(0.0, _account(self.conn)[4])
        self.assertEqual(0.0, _account(self.conn)[5])
        self.assertEqual(0.0, self.conn.execute(
            "SELECT cash FROM paper_nav WHERE account_id=?", (USER_A,)
        ).fetchone()[0])
        available.assert_called_once_with(USER_A)

    def test_attach_writes_only_the_frozen_fields_and_keeps_other_fields(self):
        _run(self.conn)
        row = _account(self.conn)
        self.assertEqual("旧名称", row[1])
        self.assertEqual("旧来源", row[2])
        self.assertEqual(11, row[6])
        self.assertEqual("old-created", row[13])
        self.assertEqual("old-params", row[18])

    def test_attach_resets_paper_nav_to_one_current_initial_row(self):
        self.conn.executemany(
            "INSERT INTO paper_nav(account_id,nav_date,cash,market_value,nav,benchmark,created_at) VALUES(?,?,?,?,?,?,?)",
            [(USER_A, "2026-09-11", 8.0, 2.0, 10.0, 300.0, "old-1"),
             (USER_A, "2026-09-12", 9.0, 3.0, 12.0, 301.0, "old-2")],
        )
        _run(self.conn)
        self.assertEqual(
            [("2026-09-13", 123.45, 0.0, 123.45, 321.09)],
            self.conn.execute(
                "SELECT nav_date,cash,market_value,nav,benchmark FROM paper_nav WHERE account_id=?",
                (USER_A,),
            ).fetchall(),
        )

    def test_parameter_version_evidence_is_golden(self):
        _run(self.conn)
        self.assertEqual(
            [(7, USER_A, "dsl-2026.09", "momentum", "{}", "用户策略接入周期", "2026-09-13")],
            self.conn.execute(
                "SELECT cycle_id,account_id,version,style,params,reason,effective_date "
                "FROM paper_parameter_versions"
            ).fetchall(),
        )

    def test_attach_audit_event_and_detail_are_golden(self):
        audited = []
        _run(self.conn, audit_fn=lambda _conn, account_id, event, detail: audited.append(
            (account_id, event, detail)
        ))
        self.assertEqual(
            [(USER_A, "user_strategy_cycle_attached", "用户策略 展示 cycle_user_a 接入周期（active 档）")],
            audited,
        )


class DetachContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)
        _insert_account(self.conn, cycle_id=CYCLE["id"], status="running", initial_cash=900.0,
                        cash=800.0)
        self.conn.execute(
            "INSERT INTO paper_nav(account_id,nav_date,cash,market_value,nav,benchmark,created_at) VALUES(?,?,?,?,?,?,?)",
            (USER_A, "2026-09-12", 800.0, 100.0, 900.0, 300.0, "nav-old"),
        )
        self.conn.execute(
            "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (7, USER_A, "old-version", "old-style", "{\"x\":1}", "历史证据", "2026-09-12", "pv-old"),
        )

    def test_disabled_bound_current_cycle_detaches_exactly(self):
        spec_for = mock.Mock(side_effect=_spec)
        audit = mock.Mock()
        _run(self.conn, enabled_ids=(), spec_for=spec_for, audit_fn=audit,
             now_fn=lambda: "detach-now")
        self.assertEqual(
            (USER_A, "旧名称", "旧来源", "paused", 0.0, 0.0, 11, 3, 0.2, 0.5,
             "old-risk", "old-version", 12.5, "old-created", "detach-now", None,
             "old-mode", "old-style", "old-params"),
            _account(self.conn),
        )
        spec_for.assert_called_once_with(USER_A)
        audit.assert_not_called()

    def test_detach_preserves_paper_nav_history(self):
        _run(self.conn, enabled_ids=())
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM paper_nav WHERE account_id=?", (USER_A,)
        ).fetchone()[0])

    def test_detach_preserves_parameter_version_evidence(self):
        _run(self.conn, enabled_ids=())
        self.assertEqual(
            [(7, USER_A, "old-version", "历史证据")],
            self.conn.execute(
                "SELECT cycle_id,account_id,version,reason FROM paper_parameter_versions"
            ).fetchall(),
        )

    def test_detach_does_not_write_attachment_audit(self):
        audit = mock.Mock()
        _run(self.conn, enabled_ids=(), audit_fn=audit)
        audit.assert_not_called()

    def test_explicit_idle_enabled_ids_detaches_and_never_falls_back(self):
        _run(self.conn, enabled_ids=())
        self.assertIsNone(_account(self.conn)[15])
        self.assertEqual(0.0, _account(self.conn)[4])


class AttachmentDecisionTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_lifecycle_pause_does_not_detach_when_caller_keeps_id_enabled(self):
        _insert_account(self.conn, cycle_id=None, status="paused")
        _run(self.conn, enabled_ids=(USER_A,))
        self.assertEqual(7, _account(self.conn)[15])

    def test_already_bound_to_current_cycle_is_not_refreshed(self):
        _insert_account(self.conn, cycle_id=7, status="running")
        self.conn.commit()
        before = _account(self.conn)
        spec_for = mock.Mock(side_effect=_spec)
        available = mock.Mock()
        benchmark = mock.Mock()
        audit = mock.Mock()
        _run(self.conn, spec_for=spec_for, available_capital_fn=available,
             benchmark_fn=benchmark, audit_fn=audit)
        self.assertEqual(before, _account(self.conn))
        self.assertEqual([], self.conn.execute("SELECT * FROM paper_nav").fetchall())
        self.assertEqual([], self.conn.execute("SELECT * FROM paper_parameter_versions").fetchall())
        spec_for.assert_called_once_with(USER_A)
        available.assert_not_called()
        benchmark.assert_not_called()
        audit.assert_not_called()

    def test_bound_to_another_cycle_is_not_stolen(self):
        _insert_account(self.conn, cycle_id=OTHER_CYCLE, status="running")
        before = _account(self.conn)
        available = mock.Mock()
        _run(self.conn, available_capital_fn=available)
        self.assertEqual(before, _account(self.conn))
        available.assert_not_called()

    def test_missing_account_is_skipped_without_spec_resolution(self):
        _insert_account(self.conn, account_id=USER_A)
        spec_for = mock.Mock(side_effect=_spec)
        _run(self.conn, user_account_ids=(USER_A, USER_B), spec_for=spec_for)
        spec_for.assert_called_once_with(USER_A)
        self.assertIsNone(_account(self.conn, USER_B))

    def test_spec_resolution_precedes_detach_decision(self):
        _insert_account(self.conn, account_id=USER_B, cycle_id=7, status="running")
        calls = []

        def spec_for(account_id):
            calls.append(("spec", account_id))
            return _spec(account_id)

        def now_fn():
            calls.append(("now", USER_B))
            return "detach-now"

        _run(self.conn, enabled_ids=(), user_account_ids=(USER_B,), spec_for=spec_for,
             now_fn=now_fn)
        self.assertEqual([("spec", USER_B), ("now", USER_B)], calls)

    def test_multiple_accounts_follow_caller_order(self):
        _insert_account(self.conn, account_id=USER_A)
        _insert_account(self.conn, account_id=USER_B)
        seen = []
        _run(self.conn, enabled_ids=(USER_B, USER_A), user_account_ids=(USER_B, USER_A),
             available_capital_fn=lambda account_id: seen.append(account_id) or 10.0)
        self.assertEqual([USER_B, USER_A], seen)

    def test_builtin_ids_are_skipped_by_injected_scope(self):
        _insert_account(self.conn, account_id="builtin", cycle_id=None)
        spec_for = mock.Mock()
        _run(self.conn, user_account_ids=("builtin",), builtin_active_ids=("builtin",),
             spec_for=spec_for)
        spec_for.assert_not_called()
        self.assertIsNone(_account(self.conn, "builtin")[15])

    def test_sqlite_row_and_bare_tuple_rows_are_both_supported(self):
        for row_factory in (None, sqlite3.Row):
            with self.subTest(row_factory=row_factory):
                conn = _make_conn(row_factory)
                try:
                    _insert_account(conn)
                    _run(conn)
                    self.assertEqual(7, _account(conn)[15] if row_factory is None
                                     else _account(conn)["cycle_id"])
                finally:
                    conn.close()


class TransactionAndFacadeTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)
        _insert_account(self.conn)

    def test_audit_failure_propagates_and_caller_rollback_restores_state(self):
        before = _account(self.conn)
        self.conn.commit()
        with self.assertRaisesRegex(RuntimeError, "audit failure"):
            _run(self.conn, audit_fn=mock.Mock(side_effect=RuntimeError("audit failure")))
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(before, _account(self.conn))
        self.assertEqual([], self.conn.execute("SELECT * FROM paper_nav").fetchall())
        self.assertEqual([], self.conn.execute("SELECT * FROM paper_parameter_versions").fetchall())

    def test_module_has_no_transaction_ownership(self):
        source = Path(PUCA.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(source, r"\b(?:BEGIN|SAVEPOINT|COMMIT|ROLLBACK)\b")

    def test_production_ensure_cycle_delegates_between_two_version_binds(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, cycle_key TEXT, status TEXT,
                                      capital REAL, enabled_strategies TEXT);
            CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, cycle_id INTEGER, initial_cash REAL);
            CREATE TABLE strategy_definitions(id TEXT, origin TEXT);
            CREATE TABLE paper_fills(account_id TEXT);
            CREATE TABLE paper_position_lots(account_id TEXT, remaining_qty REAL);
            INSERT INTO paper_cycles VALUES(7,'cycle-7','running',1000,'[\"cycle_user_a\"]');
            INSERT INTO paper_accounts VALUES('cycle_user_a',NULL,0);
            INSERT INTO strategy_definitions VALUES('cycle_user_a','user');
            """
        )
        self.addCleanup(conn.close)
        events = []

        def bind(*args):
            events.append("bind")

        def attach(*args, **kwargs):
            events.append("attach")

        spec_for = mock.Mock(return_value=_spec(USER_A))
        available = mock.Mock(return_value=0.0)
        benchmark = mock.Mock(return_value=300.0)
        num = mock.Mock(side_effect=lambda value, default=0.0: float(value or default))
        date = mock.Mock(return_value=DAY)
        now = mock.Mock(return_value="now")
        audit = mock.Mock()
        with mock.patch.object(PT, "_active_cycle_filter", return_value=("1=1", ())):
            with mock.patch.object(PT, "ACTIVE_ACCOUNT_IDS", ()):
                with mock.patch.object(PT, "ACTIVE_ACCOUNT_SPECS", {}):
                    with mock.patch.object(PT, "ACCOUNT_SPECS", {}):
                        with mock.patch.object(USP, "user_known_ids", return_value=(USER_A,)):
                            with mock.patch.object(PT, "_spec_for", spec_for):
                                with mock.patch.object(PT, "_available_cycle_ledger_capital", available):
                                    with mock.patch.object(PT, "_benchmark_close", benchmark):
                                        with mock.patch.object(PT, "_num", num):
                                            with mock.patch.object(PT, "_date", date):
                                                with mock.patch.object(PT, "_now", now):
                                                    with mock.patch.object(PT, "_audit", audit):
                                                        with mock.patch.object(PT.SR, "bind_cycle_versions", side_effect=bind):
                                                            with mock.patch.object(PT.PUCA, "reconcile_user_cycle_accounts",
                                                                                   side_effect=attach) as delegate:
                                                                with mock.patch.object(PT, "_reconcile_shared_cash",
                                                                                       side_effect=lambda *_args: events.append("cash")):
                                                                    PT._ensure_cycle(conn)
                                                                    callbacks = delegate.call_args.kwargs
                                                                    self.assertEqual((), callbacks["builtin_active_ids"])
                                                                    self.assertIs(callbacks["benchmark_fn"], benchmark)
                                                                    self.assertIs(callbacks["num_fn"], num)
                                                                    self.assertIs(callbacks["date_fn"], date)
                                                                    self.assertIs(callbacks["now_fn"], now)
                                                                    self.assertIs(callbacks["audit_fn"], audit)
                                                                    self.assertEqual(_spec(USER_A), callbacks["spec_for"](USER_A))
                                                                    spec_for.assert_called_once_with(USER_A, conn=conn)
                                                                    self.assertEqual(0.0, callbacks["available_capital_fn"](USER_A))
                                                                    available.assert_called_once_with(conn, delegate.call_args.args[1], USER_A)
        self.assertEqual(["bind", "attach", "bind", "cash"], events)
        self.assertEqual(1, delegate.call_count)
        self.assertEqual((conn, conn.execute("SELECT * FROM paper_cycles").fetchone(), (USER_A,), [USER_A]),
                         delegate.call_args.args)

    def test_facade_passes_call_time_callbacks_to_attachment_module(self):
        source = Path(PT.__file__).read_text(encoding="utf-8")
        function = next(node for node in ast.walk(ast.parse(source))
                        if isinstance(node, ast.FunctionDef) and node.name == "_ensure_cycle")
        body = ast.get_source_segment(source, function) or ""
        self.assertIn("PUCA.reconcile_user_cycle_accounts", body)
        user_block = body[body.index("all_user_ids = set(user_ids)"):body.index(
            "SR.bind_cycle_versions(conn, active[\"id\"], enabled_ids)",
            body.index("all_user_ids = set(user_ids)"),
        )]
        self.assertIn("sorted(all_user_ids)", user_block)
        self.assertNotIn("user_strategy_cycle_attached", user_block)
        self.assertNotIn("INSERT INTO paper_nav", user_block)
        self.assertIn("available_capital_fn=", user_block)
        self.assertIn("spec_for=", user_block)
        self.assertIn("benchmark_fn=", user_block)
        self.assertIn("now_fn=", user_block)

    def test_legacy_user_cycle_attachment_entry_signature_is_explicit(self):
        self.assertEqual(
            ["conn", "cycle", "enabled_ids", "user_account_ids", "builtin_active_ids",
             "spec_for", "available_capital_fn", "benchmark_fn", "num_fn", "date_fn",
             "now_fn", "audit_fn"],
            [*inspect.signature(PUCA.reconcile_user_cycle_accounts).parameters],
        )


class ArchitectureGuardTests(unittest.TestCase):
    MODULE_PATH = Path(PUCA.__file__)
    TRADING_PATH = Path(PT.__file__)

    def test_module_has_no_backend_imports_or_forbidden_authority_names(self):
        source = self.MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertFalse([node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))])
        forbidden = (
            "paper_trading", "paper_cycle_ownership", "paper_shared_cash",
            "paper_user_account_provisioning", "strategy_registry", "strategy_runtime",
            "strategy_definitions", "lifecycle_status", "supports_new_cycle",
            "enabled_strategies", "paper_cycles", "paper_fills", "paper_position_lots",
            "paper_orders", "paper_capital_reservations", "commit", "rollback",
            "BEGIN", "SAVEPOINT",
        )
        for item in forbidden:
            self.assertNotIn(item, source)
        sql = [node.value for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and node.value.lstrip().upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE"))]
        self.assertGreaterEqual(len(sql), 5)
        self.assertTrue(any("SELECT * FROM paper_accounts" in item for item in sql))
        self.assertTrue(any("UPDATE paper_accounts" in item for item in sql))
        self.assertTrue(any("DELETE FROM paper_nav" in item for item in sql))
        self.assertTrue(any("INSERT INTO paper_nav" in item for item in sql))
        self.assertTrue(any("INSERT INTO paper_parameter_versions" in item for item in sql))
        self.assertFalse(any("paper_cycles" in item or "strategy_definitions" in item for item in sql))

    def test_module_writes_only_allowed_tables(self):
        source = self.MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(source, r"\b(?:INSERT|UPDATE|DELETE)\s+(?:INTO\s+)?(?:paper_cycles|paper_orders|paper_fills|paper_position_lots|strategy_definitions)\b")

    def test_single_attachment_audit_implementation_and_no_inline_copy(self):
        production = [
            path for path in self.MODULE_PATH.parent.glob("*.py")
            if not path.name.startswith("test_")
        ]
        counts = {path.name: path.read_text(encoding="utf-8").count("user_strategy_cycle_attached")
                  for path in production}
        self.assertEqual(1, counts["paper_user_cycle_attachment.py"])
        self.assertEqual(0, counts["paper_trading.py"])
        self.assertEqual(1, sum(counts.values()))
        source = self.TRADING_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        ensure = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_ensure_cycle")
        body = ast.get_source_segment(source, ensure) or ""
        self.assertEqual(1, body.count("PUCA.reconcile_user_cycle_accounts"))

    def test_facade_keeps_orchestration_owners_and_order_markers(self):
        source = self.TRADING_PATH.read_text(encoding="utf-8")
        self.assertIn("USP.user_known_ids(conn)", source)
        self.assertGreaterEqual(source.count("SR.bind_cycle_versions(conn, active[\"id\"], enabled_ids)"), 2)
        self.assertIn("_reconcile_shared_cash(conn, active[\"id\"])", source)
        self.assertIn("_available_cycle_ledger_capital", source)
        self.assertIn("_late_join_reference_capital", source)


if __name__ == "__main__":
    unittest.main()
