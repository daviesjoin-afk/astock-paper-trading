"""The covering index must preserve malformed evidence and avoid audit payload reads."""
import datetime as dt
import hashlib
import sqlite3
import unittest

import db_migrate as DM
import paper_portfolio_read_model as P
import paper_schema_migrations as S


class PortfolioOrderReadIndexTests(unittest.TestCase):
    def test_covering_queries_keep_identical_complete_results(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, account_id TEXT, code TEXT,
                status TEXT, cycle_id INTEGER, execution_status TEXT, execution_verified INTEGER,
                realized_pnl REAL, executed_at TEXT, side TEXT, amount REAL, fees REAL, risk_payload TEXT);
            CREATE TABLE paper_fills(id INTEGER PRIMARY KEY, order_id INTEGER, fill_date TEXT);
        """)
        for i in range(1, 81):
            conn.execute("INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                i, "a" if i % 3 else "b", "600001", ("filled", "partially_filled", "cancelled",
                "pending_execution")[i % 4], 1 if i % 7 else 2,
                "verified" if i % 5 else "unknown", int(i % 5 != 0), None if i % 5 == 0 else 2.,
                "2026-09-01 10:00:00", "buy" if i % 2 else "sell", 1000., 5., "x" * 32768))
        conn.commit()
        context = P.PortfolioReadContext(1, dt.date(2026, 9, 3))
        queries = []
        conn.set_trace_callback(lambda sql: queries.append(sql))
        def material():
            return {(side, account): func(conn, context, account)
                    for side, func in (("buy", P._all_filled_buy_orders), ("sell", P._all_filled_sell_orders))
                    for account in (None, "a", "b")}
        before = material()
        sqls = list(dict.fromkeys(q for q in queries if q.startswith("SELECT id,account_id,code,status,cycle_id")))
        def payload_hash():
            return hashlib.sha256(repr([tuple(r) for r in conn.execute(
                "SELECT * FROM paper_orders ORDER BY id")]).encode()).hexdigest()
        payload_digest = payload_hash()
        self.assertEqual(S.ensure_portfolio_order_read_index(conn), {"idx_paper_orders_portfolio_facts": "ok"})
        S.ensure_portfolio_order_read_index(conn)
        self.assertEqual(material(), before)
        self.assertEqual(payload_hash(), payload_digest)
        self.assertEqual(len(sqls), 6)
        for sql in sqls:
            plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
            self.assertIn("COVERING INDEX idx_paper_orders_portfolio_facts", plan)

    def test_legacy_schema_is_skipped_and_versioned_migration_owns_index(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, status TEXT)")
            self.assertEqual(S.ensure_portfolio_order_read_index(conn),
                             {"idx_paper_orders_portfolio_facts": "schema_unavailable"})
        versions = [v for v in DM.MIGRATIONS["paper_trading"] if v[0] == 38]
        self.assertEqual(len(versions), 1)
        self.assertIs(versions[0][2], S.ensure_portfolio_order_read_index)


if __name__ == "__main__":
    unittest.main()
