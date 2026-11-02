"""账户参数恢复的省略/显式 NULL 行为及单语句约束。"""
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import paper_account_config as PAC


class AccountParamsTests(unittest.TestCase):
    def test_restore_rejects_incomplete_snapshot_before_any_cleanup(self):
        import adaptive_engine as engine
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript("""
                    CREATE TABLE paper_accounts(id TEXT PRIMARY KEY,params TEXT,
                        version TEXT NOT NULL,updated_at TEXT NOT NULL);
                    INSERT INTO paper_accounts VALUES('a','failed','v2','after');
                """)
            snapshot = {"account": {"params": 'old', "version": 'v1', "updated_at": None}}
            with mock.patch.object(engine, "PAPER_DB_PATH", str(path)):
                with self.assertRaises(sqlite3.IntegrityError):
                    engine._restore_paper_account(snapshot, 'a', 1, 'applied', expected_version='v2')
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(('failed', 'v2', 'after'), conn.execute(
                    "SELECT params,version,updated_at FROM paper_accounts").fetchone())

    def test_omitted_fields_are_preserved_and_explicit_null_fails_atomically(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript("""
                CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, params TEXT,
                    version TEXT NOT NULL, updated_at TEXT NOT NULL);
                INSERT INTO paper_accounts VALUES('a','old','v1','old-time');
            """)
            PAC.write_params(conn, 'a', params='new')
            self.assertEqual(('new', 'v1', 'old-time'), conn.execute(
                "SELECT params,version,updated_at FROM paper_accounts").fetchone())
            conn.rollback()
            for fields in ({'version': None}, {'now': None}, {'version': 'v2', 'now': None}):
                with self.subTest(fields=fields):
                    with self.assertRaises(sqlite3.IntegrityError):
                        PAC.write_params(conn, 'a', params='bad', **fields)
                    self.assertEqual(('old', 'v1', 'old-time'), conn.execute(
                        "SELECT params,version,updated_at FROM paper_accounts").fetchone())
                    conn.rollback()
            PAC.write_params(conn, 'a', params='ok', version='v2', now='new-time')
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            self.assertEqual(('old', 'v1', 'old-time'), conn.execute(
                "SELECT params,version,updated_at FROM paper_accounts").fetchone())
