# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_repository as repository


class PaperRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        self.conn.execute("CREATE TABLE paper_audit (account_id TEXT, event TEXT, detail TEXT, created_at TEXT)")

    def tearDown(self):
        self.conn.close()

    def test_rows_returns_plain_dicts(self):
        self.conn.execute("INSERT INTO sample(id,value) VALUES(?,?)", (1, "ok"))
        self.assertEqual(repository.rows(self.conn, "SELECT * FROM sample WHERE id=?", (1,)), [{"id": 1, "value": "ok"}])

    def test_audit_writes_explicit_timestamp(self):
        repository.audit(self.conn, "acct", "test_event", "detail", "2026-09-03T10:00:00+08:00")
        row = self.conn.execute("SELECT * FROM paper_audit").fetchone()
        self.assertEqual(dict(row), {
            "account_id": "acct", "event": "test_event", "detail": "detail",
            "created_at": "2026-09-03T10:00:00+08:00",
        })

    def test_account_metric_inputs_batches_legacy_safe_projection(self):
        """卖出投影必须走唯一的验证谓词：自称成交但无证据的行不得进入绩效口径。

        仓库对**可选列**（realized_pnl / executed_at）保留兼容探测，但闸门列不是
        可选项：探测不到就把谓词关掉是 fail open，会让同一份投影静默降级。
        """
        self.conn.executescript(
            """
            CREATE TABLE paper_orders(
                id INTEGER PRIMARY KEY, account_id TEXT, code TEXT, qty INTEGER,
                filled_price REAL, amount REAL, fees REAL, status TEXT, side TEXT,
                created_at TEXT, execution_status TEXT, execution_verified INTEGER
            );
            CREATE TABLE paper_fills(account_id TEXT, side TEXT);
            CREATE TABLE paper_nav(account_id TEXT, nav_date TEXT, nav REAL, benchmark REAL, created_at TEXT);
            INSERT INTO paper_orders VALUES(1,'acct','000001',100,10,1000,1,'filled','sell','2026-09-03 10:00:00','verified',1);
            INSERT INTO paper_orders VALUES(2,'acct','000002',100,10,1000,1,'filled','sell','2026-09-03 10:00:00',NULL,NULL);
            INSERT INTO paper_orders VALUES(3,'acct','000003',100,10,1000,1,'filled','sell','2026-09-03 10:00:00','unknown',0);
            INSERT INTO paper_orders VALUES(4,'acct','000004',100,10,1000,1,'filled','sell','2026-09-03 10:00:00','verified',0);
            INSERT INTO paper_fills VALUES('acct','buy');
            INSERT INTO paper_nav VALUES('acct','2026-09-02',100000,1,'2026-09-02 15:00:00');
            """
        )
        result = repository.account_metric_inputs(self.conn, ["acct"], "2026-09-03")
        self.assertEqual(result["buy_count"], {"acct": 1})
        # 只有 #1（两列一致且为 verified）进入卖出投影；#2 旧行 NULL、#3 未验证、
        # #4 两列自相矛盾 —— 全部 fail closed。
        self.assertEqual([row["id"] for row in result["sells"]["acct"]], [1])
        self.assertEqual(result["sells"]["acct"][0]["realized_pnl"], None)
        self.assertEqual(result["previous_nav"]["acct"]["nav_date"], "2026-09-02")

    def test_account_metric_inputs_empty_accounts_avoids_schema_reads(self):
        self.assertEqual(repository.account_metric_inputs(self.conn, [], "2026-09-03"), {
            "latest_nav": {}, "navs": {}, "previous_nav": {}, "sells": {},
            "buy_count": {}, "rejected": {},
        })

    def test_recent_live_orders_keeps_lightweight_activity_projection(self):
        self.conn.execute(
            """CREATE TABLE paper_orders(
                id INTEGER PRIMARY KEY, account_id TEXT, signal_id INTEGER, side TEXT, code TEXT,
                name TEXT, qty INTEGER, planned_price REAL, filled_price REAL, amount REAL,
                fees REAL, status TEXT, reason TEXT, realized_pnl REAL, created_at TEXT,
                executed_at TEXT, order_type TEXT, origin TEXT, expires_at TEXT, cancelled_at TEXT,
                filled_qty INTEGER, remaining_qty INTEGER, execution_asof TEXT,
                execution_reasons TEXT, pricing_basis TEXT, slippage REAL,
                ruleset_version TEXT, execution_evidence TEXT, risk_payload TEXT
            )"""
        )
        self.conn.execute(
            "INSERT INTO paper_orders(id,account_id,side,code,status,created_at,risk_payload) VALUES(1,'acct','buy','000001','filled','2026-09-03 10:00:00','large')"
        )
        rows = repository.recent_live_orders(self.conn, {"acct": "策略 A"}, 20)
        self.assertEqual(rows[0]["account_name"], "策略 A")
        self.assertIsNone(rows[0]["archived_cycle"])
        self.assertNotIn("risk_payload", rows[0])
        self.assertNotIn("execution_evidence", rows[0])
        self.assertEqual(rows[0]["execution_reasons"], [])

    def test_execution_display_facts_projects_only_market_trust_and_reasons(self):
        facts = repository.execution_display_facts(
            '{"decision":{"market_evidence":{"freshness":"fresh",'
            '"verification":"verified","availability":"available",'
            '"as_of":"2026-09-10"}}}',
            '["T1_NOT_SELLABLE","MARKET_STALE"]',
        )
        self.assertEqual(facts["execution_market_freshness"], "fresh")
        self.assertEqual(facts["execution_market_verification"], "verified")
        self.assertEqual(facts["execution_market_asof"], "2026-09-10")
        self.assertEqual(facts["execution_reasons"], ["T1_NOT_SELLABLE", "MARKET_STALE"])


class AuditCleanupEquivalenceTests(unittest.TestCase):
    """第六批：清理目标、原始行、顺序和补偿异常的冻结回归。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self.snapshot = {
            "account": {"params": ' {"old": true} ', "version": "old", "updated_at": "before"},
            "versions": [{"id": 1, "version": "old"}], "versions_complete": True,
        }
        self.audit_rows = [
            ("acct", "applied", "candidate=12; 失败操作"),
            ("acct", "applied", "CANDIDATE=12; SQLite 原有大小写语义"),
            ("acct", "applied", "candidate=123; 相邻候选"),
            ("other", "applied", "candidate=12; 另一账户"),
            ("acct", "rollback", "candidate=12; 另一事件"),
            ("acct", "applied", " candidate=12; 前导空格"),
            ("acct", "applied", ' {"z": 1, "a": null}\n原始 detail '),
            ("acct", "applied", None),
            ("acct", "applied", sqlite3.Binary(b"candidate=12; raw blob")),
            ("acct", "applied", "candidate=12"),
        ]
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE paper_accounts(id TEXT PRIMARY KEY,params TEXT,version TEXT,updated_at TEXT);
                INSERT INTO paper_accounts VALUES('acct','failed params','failed','after');
                CREATE TABLE paper_parameter_versions(id INTEGER PRIMARY KEY,account_id TEXT,version TEXT);
                INSERT INTO paper_parameter_versions VALUES(1,'acct','old'),(2,'acct','failed');
                CREATE TABLE paper_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,account_id TEXT,event TEXT,detail TEXT,
                    created_at TEXT,strategy_id TEXT,strategy_version INTEGER,strategy_checksum TEXT);
            """)
            for index, (account, event, detail) in enumerate(self.audit_rows):
                conn.execute(
                    "INSERT INTO paper_audit(account_id,event,detail,created_at,"
                    "strategy_id,strategy_version,strategy_checksum) VALUES(?,?,?,?,?,?,?)",
                    (account, event, detail, f"time-{index}", f"source-{index}", index, f"checksum-{index}"))

    @contextmanager
    def connection(self, path=None):
        conn = sqlite3.connect(path or self.path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def state(self):
        with self.connection() as conn:
            return {table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                    for table in ("paper_accounts", "paper_parameter_versions", "paper_audit")}

    def restore(self, candidate_id=12, expected_version="failed"):
        import adaptive_engine as engine
        with mock.patch.object(engine, "PAPER_DB_PATH", self.path):
            engine._restore_paper_account(
                self.snapshot, "acct", candidate_id, "applied", expected_version=expected_version)

    def legacy_remaining_audit(self):
        # BLOB 的 LIKE 行为由 SQLite 编译选项决定，必须以原 SQL 为对照。
        with self.connection() as source, self.connection(":memory:") as old:
            source.backup(old)
            old.execute("DELETE FROM paper_audit WHERE account_id=? AND event=? AND detail LIKE ?",
                        ("acct", "applied", "candidate=12;%"))
            return old.execute("SELECT * FROM paper_audit ORDER BY id").fetchall()

    def test_cleanup_matches_legacy_delete_for_exact_prefixes_and_raw_rows(self):
        for account, candidate in (("acct", 12), ("acct", "0012"), ("acct", 123),
                                   ("acct", -12), ("other", 12), (None, 12)):
            with self.subTest(account=account, candidate=candidate):
                with self.connection() as conn, self.connection(":memory:") as old:
                    conn.backup(old)
                    old.execute(
                        "DELETE FROM paper_audit WHERE account_id=? AND event=? AND detail LIKE ?",
                        (account, "applied", f"candidate={int(candidate)};%"))
                    expected = old.execute("SELECT * FROM paper_audit ORDER BY id").fetchall()
                    statements = []
                    conn.execute("BEGIN")
                    conn.set_trace_callback(statements.append)
                    repository.remove_failed_evolution_audit(conn, account, "applied", candidate)
                    self.assertEqual(1, len(statements), "仍应只执行一条 DELETE")
                    self.assertEqual(expected, conn.execute("SELECT * FROM paper_audit ORDER BY id").fetchall())
                    conn.rollback()

    def test_cleanup_does_not_commit_other_writes(self):
        before = self.state()
        with self.connection() as conn:
            conn.execute("UPDATE paper_accounts SET params='pending' WHERE id='acct'")
            repository.remove_failed_evolution_audit(conn, "acct", "applied", 12)
            self.assertTrue(conn.in_transaction)
            conn.rollback()
        self.assertEqual(before, self.state())

    def test_owner_propagates_sqlite_errors_to_caller(self):
        before = self.state()
        with self.connection() as conn:
            conn.execute("PRAGMA query_only=ON")
            with self.assertRaises(sqlite3.OperationalError):
                repository.remove_failed_evolution_audit(conn, "acct", "applied", 12)
        self.assertEqual(before, self.state())

    def test_compensation_keeps_surviving_rows_and_write_order(self):
        import adaptive_engine as engine
        expected_audit = self.legacy_remaining_audit()
        statements = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(engine.sqlite3, "connect", side_effect=traced_connect):
            self.restore()
        after = self.state()
        self.assertEqual([("acct", ' {"old": true} ', "old", "before")], after["paper_accounts"])
        self.assertEqual([(1, "acct", "old")], after["paper_parameter_versions"])
        self.assertEqual(expected_audit, after["paper_audit"])
        ordered = [next(i for i, sql in enumerate(statements) if sql.startswith(prefix))
                   for prefix in ("UPDATE paper_accounts", "DELETE FROM paper_parameter_versions",
                                  "DELETE FROM paper_audit", "COMMIT")]
        self.assertEqual(ordered, sorted(ordered))

    def test_compensation_swallows_only_sqlite_cleanup_error_and_still_commits(self):
        with self.connection() as conn:
            conn.execute("""CREATE TRIGGER refuse_audit_delete BEFORE DELETE ON paper_audit
                            BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
        before = self.state()
        self.restore()
        after = self.state()
        self.assertEqual(before["paper_audit"], after["paper_audit"])
        self.assertEqual("old", after["paper_accounts"][0][2])
        self.assertEqual([(1, "acct", "old")], after["paper_parameter_versions"])

    def test_non_sqlite_error_propagates_and_rolls_back_compensation(self):
        before = self.state()
        with self.assertRaises(ValueError):
            self.restore(candidate_id="invalid")
        self.assertEqual(before, self.state())

    def test_newer_account_version_skips_cleanup(self):
        before = self.state()
        with mock.patch.object(repository, "remove_failed_evolution_audit") as cleanup, \
                mock.patch("builtins.print") as alarm:
            self.restore(expected_version="another-version")
        cleanup.assert_not_called()
        alarm.assert_called_once()
        self.assertIn("restore_version_mismatch", alarm.call_args.args[0])
        self.assertEqual(before, self.state())

    def test_append_keeps_raw_detail_explicit_source_and_order(self):
        expected_audit = self.legacy_remaining_audit()
        raw = '  {"z": null, "a": "中文"}\n '
        with self.connection() as conn:
            for stamp in (("frozen", 7, "checksum"), (None, None, None)):
                repository.audit(conn, "acct", "kept", raw, "write-time", strategy_stamp=stamp)
            repository.remove_failed_evolution_audit(conn, "acct", "applied", 12)
        rows = self.state()["paper_audit"]
        self.assertEqual(expected_audit, rows[:-2])
        self.assertEqual(("acct", "kept", raw, "write-time", "frozen", 7, "checksum"), rows[-2][1:])
        self.assertEqual(("acct", "kept", raw, "write-time", None, None, None), rows[-1][1:])
        self.assertLess(rows[-2][0], rows[-1][0])


if __name__ == "__main__":
    unittest.main()
