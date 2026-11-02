"""旧订单信号集合：低绑定上限、过滤、单语句失败及调用方回滚。"""
import sqlite3
import unittest
from contextlib import closing

import signal_service as SIG


class SupersedeSetTests(unittest.TestCase):
    def test_large_set_filters_and_statement_atomicity(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript("""
                CREATE TABLE paper_signals(id INTEGER PRIMARY KEY,status TEXT,reason TEXT);
                CREATE TABLE paper_orders(id INTEGER PRIMARY KEY,signal_id INTEGER,
                    account_id TEXT,code TEXT,side TEXT,status TEXT);
            """)
            conn.executemany("INSERT INTO paper_signals VALUES(?, 'pending', NULL)",
                             [(i,) for i in range(1, 46)])
            conn.executemany("INSERT INTO paper_orders VALUES(?,?,'a','AAA','buy','wait')",
                             [(i, (i + 1) // 2) for i in range(1, 81)])
            conn.executemany("INSERT INTO paper_orders VALUES(?,?,?,?,?,?)", [
                (81, 41, 'b', 'AAA', 'buy', 'wait'),
                (82, 42, 'a', 'BBB', 'buy', 'wait'),
                (83, 43, 'a', 'AAA', 'sell', 'wait'),
                (84, 44, 'a', 'AAA', 'buy', 'filled'),
                (85, 45, 'a', 'AAA', 'buy', 'wait'),
            ])
            conn.execute("UPDATE paper_signals SET status='filled' WHERE id=40")
            conn.commit()
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32)
            before = list(conn.execute("SELECT * FROM paper_signals"))
            conn.execute("""CREATE TRIGGER fail BEFORE UPDATE ON paper_signals
                         WHEN NEW.id=39 BEGIN SELECT RAISE(ABORT,'reject'); END""")

            def run():
                return SIG.supersede_signals(
                    conn, account_id='a', code='AAA', order_statuses=('wait',),
                    keep_order_id=85, statuses=('pending',), suffix='replaced')

            with self.assertRaises(sqlite3.IntegrityError):
                run()
            self.assertEqual(before, list(conn.execute("SELECT * FROM paper_signals")))
            conn.rollback()
            conn.execute("DROP TRIGGER fail")
            self.assertEqual(39, run())
            self.assertEqual([(i, 'pending', None) for i in range(41, 46)],
                             list(conn.execute("SELECT * FROM paper_signals WHERE id>40")))
            self.assertEqual(('filled', None), conn.execute(
                "SELECT status,reason FROM paper_signals WHERE id=40").fetchone())
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            self.assertEqual(before, list(conn.execute("SELECT * FROM paper_signals")))
