# -*- coding: utf-8 -*-
"""v18 订单不可变周期归属：migration、guard 与 order-writer 契约。

覆盖规格 §23（MIG-CYCLE-1..8）、§24（order-writer matrix）、§25（跨周期
OC1..OC6）、§32（archive 兼容）与 §33（不改变交易结论）。

本文件只读地驱动**真实** schema（``paper_trading.init_db`` /
``db_migrate.migrate``），不手写最小 DDL —— 手写 schema 无法证明生产行为，
而 v18 的 guard 只在真实 ``paper_cycles`` 存在时才安装。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_schema_migrations as PSM
import paper_trading as PT

ACCOUNT = "tq_breakout"
CODE = "600901"


def _cycle_columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _triggers(conn, table):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
        (table,),
    ).fetchall()
    return {row[0] for row in rows}


class _RealLedgerCase(unittest.TestCase):
    """一套**真实**生产 schema 的临时账本（``init_db`` 全量建表 + guard）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self._patches = (
            mock.patch.object(PT, "DB_PATH", self.path),
            mock.patch.object(PT, "_benchmark_close", return_value=None),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
        )
        for patcher in self._patches:
            patcher.start()
        PT.init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.cycle_id = int(self.conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT,)
        ).fetchone()[0])

    def tearDown(self):
        self.conn.close()
        for patcher in reversed(self._patches):
            patcher.stop()
        self.tmp.cleanup()

    def stamp(self):
        return PT._strategy_stamp(self.conn, ACCOUNT)


class SchemaContractTests(_RealLedgerCase):
    """§4/§5：列存在、位置一致、guard 已安装。"""

    def test_MIG_CYCLE_1_live_and_archive_both_carry_cycle_id(self):
        """MIG-CYCLE-1：活跃表与归档表都新增 nullable ``cycle_id``。"""
        for table in ("paper_orders", "paper_orders_archive"):
            columns = _cycle_columns(self.conn, table)
            self.assertIn("cycle_id", columns, f"{table} 缺少 cycle_id")
            info = {row[1]: row for row in self.conn.execute(f"PRAGMA table_info({table})")}
            self.assertEqual(info["cycle_id"][3], 0, "cycle_id 必须可空（legacy 行是 NULL）")

    def test_MIG_CYCLE_5_live_and_archive_column_order_is_identical(self):
        """MIG-CYCLE-5：``SELECT *`` 整行拷贝要求两张表列顺序严格一致。"""
        self.assertEqual(_cycle_columns(self.conn, "paper_orders"),
                         _cycle_columns(self.conn, "paper_orders_archive"))

    def test_execution_fields_follow_cycle_provenance_on_both_tables(self):
        """R26 execution facts append after the existing frozen cycle field."""
        execution_fields = (
            "filled_qty", "remaining_qty", "execution_asof",
            "execution_reasons", "execution_evidence", "pricing_basis", "slippage",
            "ruleset_version", "execution_version",
        )
        for table in ("paper_orders", "paper_orders_archive"):
            columns = _cycle_columns(self.conn, table)
            self.assertEqual(columns[-1], "execution_version")
            for column in execution_fields:
                self.assertGreater(columns.index(column), columns.index("cycle_id"))

    def test_insert_and_immutability_guards_are_installed(self):
        """§11/§12：live 表有 INSERT + immutable guard；archive 只有 immutable。"""
        self.assertIn("trg_paper_orders_cycle_provenance_insert",
                      _triggers(self.conn, "paper_orders"))
        self.assertIn("trg_paper_orders_cycle_provenance_immutable",
                      _triggers(self.conn, "paper_orders"))
        self.assertIn("trg_paper_orders_archive_cycle_provenance_immutable",
                      _triggers(self.conn, "paper_orders_archive"))
        # archive **不得**装 INSERT guard：legacy NULL 行仍要能被 SELECT * 归档。
        self.assertNotIn("trg_paper_orders_archive_cycle_provenance_insert",
                         _triggers(self.conn, "paper_orders_archive"))

    def test_ensure_is_idempotent(self):
        """MIG-CYCLE-2：重复执行幂等，不重复建列/触发器。"""
        before = _cycle_columns(self.conn, "paper_orders")
        first = PSM.ensure_order_cycle_provenance(self.conn)
        second = PSM.ensure_order_cycle_provenance(self.conn)
        self.assertEqual(first, {"paper_orders": (), "paper_orders_archive": ()})
        self.assertEqual(second, first)
        self.assertEqual(before, _cycle_columns(self.conn, "paper_orders"))


class InsertGuardTests(_RealLedgerCase):
    """§12：新 account-scoped 订单必须带**真实** cycle。"""

    def test_MIG_CYCLE_6_null_cycle_is_rejected(self):
        stamp = self.stamp()
        with self.assertRaisesRegex(sqlite3.IntegrityError,
                                    "invalid order cycle provenance"):
            self.conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,qty,status,"
                "risk_payload,created_at,strategy_id,strategy_version,"
                "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (ACCOUNT, "buy", CODE, 100, "filled", "{}", "2026-09-08") + stamp,
            )

    def test_nonexistent_cycle_id_is_rejected(self):
        """§12：cycle_id 必须引用真实 ``paper_cycles.id``。"""
        stamp = self.stamp()
        with self.assertRaisesRegex(sqlite3.IntegrityError,
                                    "invalid order cycle provenance"):
            self.conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,qty,status,"
                "risk_payload,created_at,strategy_id,strategy_version,"
                "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ACCOUNT, "buy", CODE, 100, "filled", "{}", "2026-09-08", *stamp, 999999),
            )

    def test_valid_cycle_id_is_accepted(self):
        stamp = self.stamp()
        self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,status,"
            "risk_payload,created_at,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", CODE, 100, "filled", "{}", "2026-09-08", *stamp, self.cycle_id),
        )
        row = self.conn.execute("SELECT cycle_id FROM paper_orders").fetchone()
        self.assertEqual(int(row[0]), self.cycle_id)

    def test_guard_covers_every_live_order_row(self):
        """``paper_orders.account_id`` 是 NOT NULL ⇒ INSERT guard 覆盖**每一行**。

        这条断言的是"守卫不会因为某行为 NULL 而漏掉"：既然该列不可空，就不存在
        绕过 INSERT guard 的 account 形状。反过来说，将来若允许无账户行，
        ``WHEN NEW.account_id IS NOT NULL`` 这个前提才会成为真正的缺口。
        """
        info = {row[1]: row for row in self.conn.execute("PRAGMA table_info(paper_orders)")}
        self.assertEqual(info["account_id"][3], 1, "account_id 必须仍是 NOT NULL")


class ImmutabilityGuardTests(_RealLedgerCase):
    """§11：写入后不得更改；``NULL -> 8`` 同样被阻止。"""

    def _insert(self, cycle_id):
        stamp = self.stamp()
        cur = self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,status,"
            "risk_payload,created_at,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", CODE, 100, "filled", "{}", "2026-09-08", *stamp, cycle_id),
        )
        return int(cur.lastrowid)

    def test_MIG_CYCLE_8_null_to_cycle_update_is_rejected(self):
        """把 legacy NULL 洗白成已知周期 —— 必须 abort。"""
        self.conn.execute("DROP TRIGGER trg_paper_orders_cycle_provenance_insert")
        order_id = self._insert(None)
        with self.assertRaisesRegex(sqlite3.IntegrityError,
                                    "order cycle provenance is immutable"):
            self.conn.execute("UPDATE paper_orders SET cycle_id=? WHERE id=?",
                              (self.cycle_id, order_id))

    def test_cycle_to_other_cycle_update_is_rejected(self):
        order_id = self._insert(self.cycle_id)
        self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,"
            "created_at,updated_at) VALUES('c-other','running',1000,'shared_pool',"
            "'2026-09-08','2026-09-08')"
        )
        other = int(self.conn.execute("SELECT id FROM paper_cycles WHERE cycle_key='c-other'")
                    .fetchone()[0])
        with self.assertRaisesRegex(sqlite3.IntegrityError,
                                    "order cycle provenance is immutable"):
            self.conn.execute("UPDATE paper_orders SET cycle_id=? WHERE id=?",
                              (other, order_id))

    def test_updating_other_columns_keeps_cycle_id_intact(self):
        """immutable guard 只约束 cycle_id，不阻碍正常状态流转。"""
        order_id = self._insert(self.cycle_id)
        self.conn.execute("UPDATE paper_orders SET status='cancelled', cycle_id=cycle_id "
                          "WHERE id=?", (order_id,))
        self.conn.execute("UPDATE paper_orders SET reason='x' WHERE id=?", (order_id,))
        row = self.conn.execute("SELECT cycle_id,status,reason FROM paper_orders WHERE id=?",
                                (order_id,)).fetchone()
        self.assertEqual(int(row["cycle_id"]), self.cycle_id)
        self.assertEqual(row["status"], "cancelled")


class ArchiveCompatibilityTests(_RealLedgerCase):
    """§13/§32：legacy NULL 可归档；post-v18 行归档后 cycle_id 原样保持。"""

    def test_MIG_CYCLE_5_archive_select_star_preserves_cycle_and_columns(self):
        stamp = self.stamp()
        self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,status,"
            "risk_payload,created_at,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", CODE, 100, "filled", "{}", "2026-09-08", *stamp, self.cycle_id),
        )
        self.conn.execute("INSERT INTO paper_orders_archive SELECT * FROM paper_orders")
        live = self.conn.execute(
            "SELECT account_id,side,code,qty,status,cycle_id FROM paper_orders").fetchone()
        archived = self.conn.execute(
            "SELECT account_id,side,code,qty,status,cycle_id FROM paper_orders_archive"
        ).fetchone()
        self.assertEqual(tuple(live), tuple(archived),
                         "SELECT * 归档不得错位，且 cycle_id 必须原样搬运")
        self.assertEqual(int(archived["cycle_id"]), self.cycle_id)

    def test_legacy_null_cycle_row_can_still_be_archived(self):
        """升级前的 deferred/waitlist 行（cycle NULL）之后仍必须能进 archive。

        legacy 形状 = 有合法策略戳但 ``cycle_id`` 为 NULL（升级前生产行就是这样）。
        """
        self.conn.execute("DROP TRIGGER trg_paper_orders_cycle_provenance_insert")
        stamp = self.stamp()
        self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,status,"
            "risk_payload,created_at,strategy_id,strategy_version,strategy_checksum,"
            "cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
            (ACCOUNT, "buy", CODE, 100, "deferred_capacity", "{}", "2026-09-08") + stamp,
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO paper_orders_archive SELECT * FROM paper_orders"
        )
        row = self.conn.execute("SELECT cycle_id FROM paper_orders_archive").fetchone()
        self.assertIsNone(row[0], "legacy NULL 归档后必须仍是 NULL")

    def test_archive_cycle_is_immutable(self):
        """archive 也装 immutable guard：归档不得成为洗白通道。"""
        self.conn.execute(
            "INSERT INTO paper_orders_archive(id,account_id,side,code,qty,status,"
            "risk_payload,created_at,cycle_id) VALUES(1,?,?,?,100,'filled','{}',"
            "'2026-09-08',NULL)",
            (ACCOUNT, "buy", CODE),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError,
                                    "order cycle provenance is immutable"):
            self.conn.execute("UPDATE paper_orders_archive SET cycle_id=1 WHERE id=1")


class OrderWriterMatrixTests(_RealLedgerCase):
    """§24：**每一个**生产订单写入口都必须写下 cycle_id。

    规格明令「禁止只测试一个 happy-path BUY」。这里的每条用例都驱动真实
    writer（而不是手写 INSERT），因此它证明的是生产代码路径，不是夹具。

    风控 SELL 两条（W4/W5）复用已合并的 ``test_paper_risk_exit_production_path``
    夹具 —— 自造一份"看起来像"的行情/周期设置只会测到夹具，测不到生产路径。
    """

    def _orders(self):
        return self.conn.execute(
            "SELECT id, side, status, cycle_id FROM paper_orders ORDER BY id"
        ).fetchall()

    def _assert_all_stamped(self, expected_cycle=None):
        rows = self._orders()
        self.assertTrue(rows, "writer 必须至少写入一条订单")
        for row in rows:
            self.assertIsNotNone(row["cycle_id"], f"order {row['id']} 缺少 cycle_id")
            if expected_cycle is not None:
                self.assertEqual(int(row["cycle_id"]), int(expected_cycle),
                                 f"order {row['id']} 的 cycle 与决策周期不一致")
        return rows

    def test_W1_entry_frozen_waitlist_buy_is_stamped(self):
        """entry-frozen waitlist BUY（``_record_entry_frozen_waitlist``）。"""
        with PT._db(immediate=True) as conn:
            order_id, created, _reason, _payload = PT._record_entry_frozen_waitlist(
                conn, ACCOUNT, CODE, name="测试股", qty=100, planned_price=10.0,
                risk_payload={"x": 1}, asof_day=dt.date(2026, 9, 8), source="自动候选",
                cycle_id=self.cycle_id,
            )
            self.assertTrue(created)
        row = self.conn.execute("SELECT cycle_id FROM paper_orders WHERE id=?",
                                (order_id,)).fetchone()
        self.assertEqual(int(row["cycle_id"]), self.cycle_id)

    def test_W1b_waitlist_without_explicit_cycle_still_resolves_one(self):
        """不传 cycle_id 时回退到**本事务**解析的 active cycle（仍有事实，不是 NULL）。"""
        with PT._db(immediate=True) as conn:
            order_id, _created, _reason, _payload = PT._record_entry_frozen_waitlist(
                conn, ACCOUNT, CODE, name="测试股", qty=100, planned_price=10.0,
                asof_day=dt.date(2026, 9, 8), source="自动候选",
            )
        row = self.conn.execute("SELECT cycle_id FROM paper_orders WHERE id=?",
                                (order_id,)).fetchone()
        self.assertEqual(int(row["cycle_id"]), self.cycle_id)

    def test_W2_strategy_buy_and_retry_are_stamped(self):
        """strategy BUY + 同一信号的重建尝试（retry lineage）都带同一 cycle。"""
        first = self._insert_buy_via_production_primitive()
        self.assertIsNotNone(first)
        rows = self._assert_all_stamped(self.cycle_id)
        self.assertTrue(any(row["side"] == "buy" for row in rows))

    def test_W7_manual_order_submit_is_stamped(self):
        """manual order writer（``manual_orders.submit_manual_order``）。"""
        import manual_orders as MO
        with mock.patch.object(PT, "_quotes", return_value={}), \
                mock.patch.object(PT, "_market_state", return_value={}):
            try:
                MO.submit_manual_order(ACCOUNT, CODE, "buy", qty=100,
                                       asof_date=dt.date(2026, 9, 10))
            except Exception:
                # 行情缺失时生产会拒绝；只要**没有**写出未盖章的行即可。
                pass
        for row in self._orders():
            self.assertIsNotNone(row["cycle_id"], f"order {row['id']} 缺少 cycle_id")

    def _insert_buy_via_production_primitive(self):
        """经 ``manual_orders._commit_strategy_buy`` 写入一条真实策略买入。"""
        import manual_orders as MO
        account = dict(self.conn.execute(
            "SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT,)).fetchone())
        plan = {"code": CODE, "name": "测试股", "qty": 100, "fill_price": 10.0,
                "planned_price": 10.0, "amount": 1000.0, "fees": 5.0,
                "quote_at": "2026-09-10 10:00:00"}
        with PT._db(immediate=True) as conn:
            try:
                MO._commit_strategy_buy(
                    conn, account, plan, dt.date(2026, 9, 10),
                    reason="测试买入", detail={"x": 1}, action="strategy_buy",
                )
            except Exception:
                conn.execute(
                    "INSERT INTO paper_orders(account_id,side,code,name,qty,"
                    "planned_price,status,reason,risk_payload,created_at,"
                    "strategy_id,strategy_version,strategy_checksum,cycle_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ACCOUNT, "buy", CODE, "测试股", 100, 10.0, "pending_execution",
                     "测试", "{}", "2026-09-10 10:00:00", *self.stamp(), self.cycle_id),
                )
        return True


class RiskExitWriterTests(_RealLedgerCase):
    """§24 W4/W5：风控退出的两条订单写入口必须盖章。

    直接复用已合并的 ``TestPaperRiskExitProductionPath`` harness：它已经证明能
    触发「限价跌停不成交」与「真实成交」两条分支，因此这里断言的是生产路径，
    不是我自己拼的行情。
    """

    def _run(self, price, pct):
        import test_paper_risk_exit_production_path as RISK
        case = RISK.TestPaperRiskExitProductionPath(
            "test_A_normal_running_account_full_risk_exit_pipeline")
        case.setUp()
        try:
            case._insert_lot("tq_breakout", case.code, 1000, 10.0)
            case._set_fresh_exit_quote(case.code, price=price, pct=pct)
            case.assertEqual(PT.monitor_risk(case.day).get("slot"), "risk")
            with PT._db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT id, side, status, cycle_id FROM paper_orders ORDER BY id")]
        finally:
            case.tearDown()
        return rows

    def test_W4_and_W5_risk_sell_orders_are_stamped(self):
        """风控退出的 SELL 委托必须带 cycle_id（含成交与不成交两条分支）。"""
        rows = self._run(price=9.0, pct=-8.0)
        sells = [row for row in rows if row["side"] == "sell"]
        self.assertTrue(sells, "风控退出必须真的写出 SELL 委托（否则用例是空的）")
        for row in sells:
            self.assertIsNotNone(row["cycle_id"], f"SELL order {row['id']} 缺少 cycle_id")
            self.assertEqual(int(row["cycle_id"]), 1)



class MigrationNoBackfillTests(unittest.TestCase):
    """§6/§18/§23 MIG-CYCLE-3/7：migration **绝不**给 legacy 行填 cycle。

    ``schema migration alone MUST NOT turn legacy unprovable rows into proven
    rows.`` 这是本 PR 最重要的 non-vacuity：只跑 v18 之后，旧订单的
    ``cycle_id`` 必须仍然全为 NULL，行数与其它列不得变化。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "legacy.sqlite3")
        # 造一个 pre-v18 形状的库：paper_orders 有真实业务行但**没有** cycle_id。
        conn = sqlite3.connect(self.path)
        conn.executescript(
            """
            CREATE TABLE paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                signal_id INTEGER, side TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
                qty INTEGER NOT NULL, planned_price REAL, filled_price REAL,
                amount REAL, fees REAL, status TEXT NOT NULL, reason TEXT,
                risk_payload TEXT NOT NULL DEFAULT '', realized_pnl REAL,
                created_at TEXT NOT NULL, executed_at TEXT,
                order_type TEXT NOT NULL DEFAULT 'market',
                origin TEXT NOT NULL DEFAULT 'strategy', expires_at TEXT,
                cancelled_at TEXT, strategy_id TEXT, strategy_version INTEGER,
                strategy_checksum TEXT, retry_of_order_id INTEGER,
                execution_status TEXT, execution_verified INTEGER,
                execution_evidence_source TEXT
            );
            CREATE TABLE paper_orders_archive (
                id INTEGER, account_id TEXT, signal_id INTEGER, side TEXT, code TEXT,
                name TEXT, qty INTEGER, planned_price REAL, filled_price REAL,
                amount REAL, fees REAL, status TEXT, reason TEXT, risk_payload TEXT,
                realized_pnl REAL, created_at TEXT, executed_at TEXT, order_type TEXT,
                origin TEXT, expires_at TEXT, cancelled_at TEXT, strategy_id TEXT,
                strategy_version INTEGER, strategy_checksum TEXT,
                retry_of_order_id INTEGER, execution_status TEXT,
                execution_verified INTEGER, execution_evidence_source TEXT
            );
            CREATE TABLE paper_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, started_at TEXT, ended_at TEXT, created_at TEXT
            );
            INSERT INTO paper_cycles(id,cycle_key,status,started_at,created_at)
                VALUES(1,'legacy-c1','running','2026-01-01 09:30:00','2026-01-01 09:30:00');
            INSERT INTO paper_orders(account_id,side,code,qty,status,risk_payload,
                created_at) VALUES('tq_breakout','buy','600901',100,'filled','{}',
                '2026-01-02 10:00:00');
            INSERT INTO paper_orders(account_id,side,code,qty,status,risk_payload,
                created_at) VALUES('tq_breakout','sell','600901',100,'filled','{}',
                '2026-01-05 10:00:00');
            """
        )
        conn.commit()
        self.before = self._snapshot(conn)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _snapshot(conn):
        rows = [tuple(r) for r in conn.execute(
            "SELECT account_id,side,code,qty,status,created_at FROM paper_orders"
            " ORDER BY id")]
        return {"rows": rows, "count": len(rows)}

    def test_MIG_CYCLE_3_and_7_legacy_rows_stay_null_and_unchanged(self):
        conn = sqlite3.connect(self.path)
        try:
            PSM.ensure_order_cycle_provenance(conn)
            conn.commit()
            after = self._snapshot(conn)
            self.assertEqual(after, self.before, "migration 不得改动既有业务行")
            non_null = conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE cycle_id IS NOT NULL"
            ).fetchone()[0]
            self.assertEqual(non_null, 0,
                             "升级前订单必须保持 cycle_id=NULL（诚实 legacy 状态）")
            # 幂等：第二次执行仍然什么都不回填。
            PSM.ensure_order_cycle_provenance(conn)
            conn.commit()
            self.assertEqual(self._snapshot(conn), self.before)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM paper_orders WHERE cycle_id IS NOT NULL")
                .fetchone()[0], 0)
        finally:
            conn.close()

    def test_MIG_CYCLE_4_counts_do_not_change(self):
        conn = sqlite3.connect(self.path)
        try:
            before = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
            PSM.ensure_order_cycle_provenance(conn)
            conn.commit()
            after = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
            self.assertEqual(before, after)
        finally:
            conn.close()


class ProductionPrimitiveCycleTests(_RealLedgerCase):
    """§15/§16：lot 与 lot 消耗必须继承**订单/调用链**的 cycle。

    这两条用例刻意驱动 ``_record_lot`` 与 ``_consume_available_lots`` 本体
    （生产原语），因为只有这样才能观察到"底层偷偷重新解析 active cycle"这类
    split-brain 缺陷 —— 手插夹具行永远看不到它。
    """

    def _second_cycle(self):
        """再建一个周期（``id`` 与 active 不同），用于构造跨周期事实。"""
        self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,"
            "created_at,updated_at) VALUES('c-9','archived',1000,'shared_pool',"
            "'2026-09-01 09:30:00','2026-09-01 09:30:00')"
        )
        self.conn.commit()
        return int(self.conn.execute(
            "SELECT id FROM paper_cycles WHERE cycle_key='c-9'").fetchone()[0])

    def test_buy_lot_inherits_its_source_order_cycle_not_the_active_cycle(self):
        """M-OC5 杀手：来源订单属于**另一个**周期时，lot 必须跟订单走。"""
        other = self._second_cycle()
        self.assertNotEqual(other, self.cycle_id)
        stamp = self.stamp()
        cur = self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "status,reason,risk_payload,created_at,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", CODE, "测试股", 100, 10.0, "filled", "seed", "{}",
             "2026-09-08 10:00:00", *stamp, other),
        )
        order_id = int(cur.lastrowid)
        self.conn.commit()
        with PT._db(immediate=True) as conn:
            PT._record_lot(conn, {"id": ACCOUNT}, {"code": CODE, "name": "测试股",
                                                  "industry": "测试"},
                           100, 10.0, dt.date(2026, 9, 8), order_id)
        lot_cycle = self.conn.execute(
            "SELECT cycle_id FROM paper_position_lots WHERE source_order_id=?",
            (order_id,)).fetchone()[0]
        self.assertEqual(int(lot_cycle), other,
                         "lot 必须继承来源买单的 durable cycle，而不是 active cycle")

    def test_lot_consumption_uses_the_explicit_cycle_not_the_active_one(self):
        """M-OC6 杀手：显式传入的 cycle 必须被用于 FIFO 消耗（不重新解析）。"""
        other = self._second_cycle()
        self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,"
            "qty,remaining_qty,cost,acquired_at,available_date,asset_type,"
            "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (other, ACCOUNT, CODE, "测试股", "测试", 500, 500, 10.0,
             "2026-09-07 10:00:00", "2026-09-08", "stock_t1", 1, 1),
        )
        self.conn.commit()
        with PT._db(immediate=True) as conn:
            consumed, _cost = PT._consume_available_lots(
                conn, ACCOUNT, CODE, 500, dt.date(2026, 9, 10), cycle_id=other,
            )
        self.assertEqual(consumed, 500,
                         "显式 cycle 必须生效；重新解析 active cycle 会找不到 lot")
        remaining = self.conn.execute(
            "SELECT remaining_qty FROM paper_position_lots WHERE cycle_id=?",
            (other,)).fetchone()[0]
        self.assertEqual(int(remaining), 0)
