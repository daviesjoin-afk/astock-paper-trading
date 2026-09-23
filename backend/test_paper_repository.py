# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import unittest
import json

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
                risk_payload TEXT
            )"""
        )
        self.conn.execute(
            "INSERT INTO paper_orders(id,account_id,side,code,status,created_at,risk_payload) VALUES(1,'acct','buy','000001','filled','2026-09-03 10:00:00','large')"
        )
        rows = repository.recent_live_orders(self.conn, {"acct": "策略 A"}, 20)
        self.assertEqual(rows[0]["account_name"], "策略 A")
        self.assertIsNone(rows[0]["archived_cycle"])
        self.assertNotIn("risk_payload", rows[0])

    def test_recent_live_orders_projects_partial_execution_and_fill_evidence(self):
        self.conn.executescript(
            """
            CREATE TABLE paper_orders(
                id INTEGER PRIMARY KEY, account_id TEXT, signal_id INTEGER, side TEXT, code TEXT,
                name TEXT, qty INTEGER, planned_price REAL, filled_price REAL, amount REAL,
                fees REAL, status TEXT, reason TEXT, realized_pnl REAL, created_at TEXT,
                executed_at TEXT, order_type TEXT, origin TEXT, expires_at TEXT, cancelled_at TEXT,
                risk_payload TEXT,cycle_id INTEGER,execution_status TEXT,execution_verified INTEGER,
                execution_evidence_source TEXT
            );
            CREATE TABLE paper_fills(
                id INTEGER PRIMARY KEY,order_id INTEGER,account_id TEXT,side TEXT,code TEXT,
                qty INTEGER,price REAL,amount REAL,fees REAL,fill_date TEXT,quote_at TEXT,
                execution_event_key TEXT,execution_asof TEXT,pricing_basis TEXT,
                slippage_amount REAL,execution_evidence TEXT
            );
            """
        )
        quote = {"quote_at": "2026-09-08T10:00:00", "quote_validation": "cross_source_checked"}
        decision = {"as_of": "2026-09-08T10:00:00", "market_state": "cross_source_checked",
                    "market_evidence": quote, "reason_codes": []}
        self.conn.execute(
            """INSERT INTO paper_orders(id,account_id,side,code,name,qty,planned_price,status,
               reason,created_at,origin,risk_payload,cycle_id,execution_status,execution_verified)
               VALUES(1,'acct','buy','600000','浦发',300,10,'partially_filled','剩余待执行',
               '2026-09-08T10:00:00','manual',?,3,'partial',0)""",
            (json.dumps({"execution": decision}),),
        )
        self.conn.execute(
            """INSERT INTO paper_fills VALUES(1,1,'acct','buy','600000',100,10.01,1001,0.1,
               '2026-09-08','2026-09-08T10:00:00','event-1','2026-09-08T10:00:00',
               'reference_plus_slippage',1,'{}')"""
        )

        order = repository.recent_live_orders(self.conn, {"acct": "策略 A"}, 20)[0]

        self.assertEqual((300, 100, 200), (
            order["desired_qty"], order["filled_qty"], order["remaining_qty"],
        ))
        self.assertEqual("cross_source_checked", order["market_data_validation"])
        self.assertEqual(["cancel"], order["allowed_actions"])
        self.assertEqual("event-1", order["fill_events"][0]["execution_event_key"])
        self.assertNotIn("risk_payload", order)


if __name__ == "__main__":
    unittest.main()
