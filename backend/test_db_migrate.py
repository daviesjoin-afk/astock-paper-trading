# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db_migrate


class DbMigrateTests(unittest.TestCase):
    def test_legacy_paper_schema_reaches_latest_version_idempotently(self):
        latest_version = max(item[0] for item in db_migrate.MIGRATIONS["paper_trading"])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper.sqlite3")
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, status TEXT);
                CREATE TABLE paper_position_lots(id INTEGER PRIMARY KEY, cost REAL, qty INTEGER);
                CREATE TABLE paper_positions(account_id TEXT, code TEXT, qty INTEGER);
                CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, initial_cash REAL, cash REAL);
                CREATE TABLE paper_jobs(slot TEXT, market_date TEXT, started_at TEXT);
                CREATE TABLE paper_job_runs(run_key TEXT PRIMARY KEY, started_at TEXT);
                CREATE TABLE paper_runtime_locks(lock_key TEXT PRIMARY KEY, acquired_at TEXT, expires_at TEXT);
                CREATE TABLE paper_nav(account_id TEXT, nav_date TEXT);
                """
            )
            conn.commit()
            conn.close()

            with redirect_stdout(StringIO()):
                db_migrate.migrate("paper_trading", path=path)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT version FROM schema_version WHERE db_name='paper_trading'").fetchone()[0], latest_version)
                order_columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_orders)")}
                self.assertTrue({"realized_pnl", "order_type", "origin"}.issubset(order_columns))
                self.assertTrue({"strategy_id", "strategy_version", "strategy_checksum"}.issubset(order_columns))
                self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_ignition_shadow'").fetchone())
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM strategy_definitions WHERE origin='builtin'").fetchone()[0],
                    5,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM paper_strategy_versions WHERE version=1").fetchone()[0],
                    5,
                )
            finally:
                conn.close()

            with redirect_stdout(StringIO()):
                db_migrate.migrate("paper_trading", path=path)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT version FROM schema_version WHERE db_name='paper_trading'").fetchone()[0], latest_version)
            finally:
                conn.close()


    def test_v21_to_latest_refreshes_narrow_strategy_stamp_guard(self):
        """v21 → 最新：R21 的收窄 guard 必须被重新安装（存量库也要拿到新定义）。

        断言的是「迁移链跑到**当前** head」而不是某个写死的版本号 —— 但 v22 的
        收窄行为与 v23 之后的 head 都不是本用例的被测对象，所以只用 ``>= 22``
        锁住「v22 已应用」，避免每次新增迁移都要改这里。
        """
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper.sqlite3")
            bootstrap = sqlite3.connect(path)
            bootstrap.executescript(
                """
                CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, status TEXT);
                CREATE TABLE paper_position_lots(id INTEGER PRIMARY KEY, cost REAL, qty INTEGER);
                CREATE TABLE paper_positions(account_id TEXT, code TEXT, qty INTEGER);
                CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, initial_cash REAL, cash REAL);
                CREATE TABLE paper_jobs(slot TEXT, market_date TEXT, started_at TEXT);
                CREATE TABLE paper_job_runs(run_key TEXT PRIMARY KEY, started_at TEXT);
                CREATE TABLE paper_runtime_locks(lock_key TEXT PRIMARY KEY, acquired_at TEXT, expires_at TEXT);
                CREATE TABLE paper_nav(account_id TEXT, nav_date TEXT);
                CREATE TABLE paper_signals(
                    id INTEGER PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    intended_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE paper_audit(
                    id INTEGER PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL);
                """
            )
            bootstrap.commit()
            bootstrap.close()
            with redirect_stdout(StringIO()):
                db_migrate.migrate("paper_trading", path=path)
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS paper_signals(
                           id INTEGER PRIMARY KEY,
                           account_id TEXT NOT NULL,
                           signal_date TEXT NOT NULL,
                           intended_date TEXT NOT NULL,
                           code TEXT NOT NULL,
                           payload TEXT NOT NULL,
                           status TEXT NOT NULL,
                           created_at TEXT NOT NULL)"""
                )
                # Simulate the pre-v22 broad guard on an existing ledger.
                conn.execute("DROP TRIGGER IF EXISTS trg_paper_signals_strategy_stamp_insert")
                conn.execute(
                    """CREATE TRIGGER trg_paper_signals_strategy_stamp_insert
                       BEFORE INSERT ON paper_signals
                       WHEN NEW.account_id IS NOT NULL
                        AND NOT (
                            NEW.strategy_id IS NULL
                            AND NEW.strategy_version IS NULL
                            AND NEW.strategy_checksum IS NULL
                        )
                        AND (
                            NEW.strategy_id IS NULL OR NEW.strategy_version IS NULL
                            OR NEW.strategy_checksum IS NULL
                            OR NEW.strategy_id <> NEW.account_id
                            OR NOT EXISTS (
                                SELECT 1 FROM paper_strategy_versions v
                                WHERE v.strategy_id=NEW.strategy_id
                                  AND v.version=NEW.strategy_version
                                  AND v.checksum=NEW.strategy_checksum
                            )
                        )
                       BEGIN SELECT RAISE(ABORT, 'invalid strategy version stamp'); END"""
                )
                # Simulate the pre-v22 audit allowance: only the two original
                # causal SELL events were permitted with unknown provenance.
                conn.execute("DROP TRIGGER IF EXISTS trg_paper_audit_strategy_stamp_insert")
                conn.execute(
                    """CREATE TRIGGER trg_paper_audit_strategy_stamp_insert
                       BEFORE INSERT ON paper_audit
                       WHEN NEW.account_id IS NOT NULL
                        AND NOT (
                            NEW.strategy_id IS NULL
                            AND NEW.strategy_version IS NULL
                            AND NEW.strategy_checksum IS NULL
                            AND NEW.event IN ('sell_filled','protective_exit_recovery_watch')
                        )
                        AND (
                            NEW.strategy_id IS NULL OR NEW.strategy_version IS NULL
                            OR NEW.strategy_checksum IS NULL
                            OR NEW.strategy_id <> NEW.account_id
                            OR NOT EXISTS (
                                SELECT 1 FROM paper_strategy_versions v
                                WHERE v.strategy_id=NEW.strategy_id
                                  AND v.version=NEW.strategy_version
                                  AND v.checksum=NEW.strategy_checksum
                            )
                        )
                       BEGIN SELECT RAISE(ABORT, 'invalid strategy version stamp'); END"""
                )
                conn.execute(
                    "UPDATE schema_version SET version=21 WHERE db_name='paper_trading'"
                )
                conn.commit()
            finally:
                conn.close()

            with redirect_stdout(StringIO()):
                db_migrate.migrate("paper_trading", path=path)

            conn = sqlite3.connect(path)
            try:
                applied = conn.execute(
                    "SELECT version FROM schema_version WHERE db_name='paper_trading'"
                ).fetchone()[0]
                self.assertGreaterEqual(applied, 22, "v22 的收窄 guard 未被应用")
                self.assertEqual(
                    applied,
                    max(version for version, _desc, _op
                        in db_migrate.MIGRATIONS["paper_trading"]),
                    "迁移链没有跑到当前 head",
                )

                def try_insert(sql, params=()):
                    try:
                        conn.execute(sql, params)
                        conn.commit()
                        return True
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        return False

                self.assertFalse(try_insert(
                    "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,"
                    "payload,status,created_at) VALUES(?,?,?,?,?,?,?)",
                    ("tq_breakout", "2026-09-10", "2026-09-10", "600001",
                     "{}", "pending", "2026-09-10 09:00:00"),
                ))
                audit_sql = (
                    "INSERT INTO paper_audit(account_id,event,created_at) VALUES(?,?,?)"
                )
                for event in (
                    "quality_rotation",
                    "concentration_rotation",
                    "permission_scope_exit",
                ):
                    self.assertTrue(try_insert(
                        audit_sql, ("tq_breakout", event, "2026-09-10 10:00:00")
                    ))
                self.assertFalse(try_insert(
                    audit_sql,
                    ("tq_breakout", "some_unrelated_event", "2026-09-10 10:00:00"),
                ))
            finally:
                conn.close()

    def test_migration_creates_consistent_pre_upgrade_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper.sqlite3")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute("INSERT INTO paper_orders(status) VALUES('legacy')")
            conn.commit()
            conn.close()

            with redirect_stdout(StringIO()):
                db_migrate.migrate("paper_trading", path=path)

            backups = [name for name in os.listdir(directory) if name.startswith("paper.pre-v0-")]
            self.assertEqual(len(backups), 1)
            backup = sqlite3.connect(os.path.join(directory, backups[0]))
            try:
                self.assertEqual(backup.execute("SELECT status FROM paper_orders").fetchone()[0], "legacy")
                self.assertIsNone(backup.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
                ).fetchone())
            finally:
                backup.close()

    def test_failed_callable_migration_rolls_back_and_keeps_previous_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper.sqlite3")
            sqlite3.connect(path).close()
            with redirect_stdout(StringIO()):
                db_migrate.migrate("paper_trading", path=path)

            original = db_migrate.MIGRATIONS["paper_trading"]
            latest_version = max(item[0] for item in original)

            def fail(conn):
                conn.execute("CREATE TABLE should_rollback (id INTEGER)")
                raise RuntimeError("migration failed")

            db_migrate.MIGRATIONS["paper_trading"] = [
                *original, (latest_version + 1, "故意失败", fail),
            ]
            try:
                with self.assertRaisesRegex(RuntimeError, "migration failed"), redirect_stdout(StringIO()):
                    db_migrate.migrate("paper_trading", path=path)
            finally:
                db_migrate.MIGRATIONS["paper_trading"] = original

            conn = sqlite3.connect(path)
            try:
                self.assertEqual(
                    conn.execute("SELECT version FROM schema_version WHERE db_name='paper_trading'").fetchone()[0],
                    latest_version,
                )
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='should_rollback'").fetchone())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
