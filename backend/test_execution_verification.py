# -*- coding: utf-8 -*-
"""执行验证闸门（PR-150 wiring）回归测试。

每条测试对应一个**可证伪**陈述；``pr150_mutation_check.py`` 的 M56–M60 逐条还原
这些缺陷，本文件必须把它们抓住。

三层关系（本文件的中心论点）：

* PR149 的 ``selection_executable`` —— 选股口径，本层**不回写**；
* 本层的 ``execution_verified`` —— **真实成交流水证据**；
* 两者都为真才允许把收益计入真实执行绩效。

``selection_executable=True`` 而 ``execution_verified=False`` 是**合法且常见**的，
但那时**禁止**计入已实现执行收益 / 真实成交统计 / 执行绩效。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import tempfile
import unittest

import execution_evidence as EE
import execution_verification as EV
import paper_schema_migrations as PSM

SESSION = "2024-06-18"
NEXT_SESSION = "2024-06-19"
ACCOUNT = "tq_breakout"
CODE = "600001"


def _db():
    """一个带 paper_orders / paper_fills 的内存账本（含闸门三列）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE paper_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
            signal_id INTEGER, side TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
            qty INTEGER NOT NULL, planned_price REAL, filled_price REAL,
            amount REAL, fees REAL, status TEXT NOT NULL, reason TEXT,
            risk_payload TEXT NOT NULL, realized_pnl REAL, created_at TEXT NOT NULL,
            executed_at TEXT, order_type TEXT NOT NULL DEFAULT 'market',
            origin TEXT NOT NULL DEFAULT 'strategy', expires_at TEXT,
            cancelled_at TEXT, strategy_id TEXT, strategy_version INTEGER,
            strategy_checksum TEXT, retry_of_order_id INTEGER
        );
        CREATE TABLE paper_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
            account_id TEXT NOT NULL, side TEXT NOT NULL, code TEXT NOT NULL,
            qty INTEGER NOT NULL, price REAL NOT NULL, amount REAL NOT NULL,
            fees REAL NOT NULL, fill_date TEXT NOT NULL, quote_at TEXT,
            assumption TEXT NOT NULL
        );
        """
    )
    PSM.ensure_execution_verification_columns(conn)
    return conn


def _insert_order(conn, *, status="filled", side="buy", qty=100, price=10.0,
                  amount=None, fees=None, executed_at=SESSION + " 15:00:00",
                  account_id=ACCOUNT, code=CODE):
    gross = qty * price if amount is None else amount
    charge = 5.0 if fees is None else fees
    cur = conn.execute(
        """INSERT INTO paper_orders(account_id,side,code,qty,planned_price,
               filled_price,amount,fees,status,reason,risk_payload,realized_pnl,
               created_at,executed_at,order_type,origin)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (account_id, side, code, qty, price, price, gross, charge, status, "test",
         "{}", None, SESSION + " 10:00:00", executed_at, "limit", "strategy"),
    )
    return int(cur.lastrowid)


def _insert_fill(conn, order_id, *, qty=100, price=10.0, side="buy",
                 account_id=ACCOUNT, code=CODE, session=SESSION, fees=None):
    gross = qty * price
    conn.execute(
        """INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,
               fees,fill_date,quote_at,assumption)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (order_id, account_id, side, code, qty, price, gross,
         5.0 if fees is None else fees, session, session + "T15:00:00", "local"),
    )


class GateStateTest(unittest.TestCase):
    """四态映射：只有 ``fill_verified`` 才是 ``verified``。"""

    def test_every_fill_verdict_maps_to_exactly_one_gate_state(self):
        expected = {
            EE.FILL_VERDICT_VERIFIED: EV.EXECUTION_STATUS_VERIFIED,
            EE.FILL_VERDICT_PARTIAL: EV.EXECUTION_STATUS_PARTIAL,
            EE.FILL_VERDICT_PENDING: EV.EXECUTION_STATUS_UNKNOWN,
            EE.FILL_VERDICT_NONE_CONFIRMED: EV.EXECUTION_STATUS_NOT_EXECUTED,
            EE.FILL_VERDICT_NOT_ATTEMPTED: EV.EXECUTION_STATUS_NOT_EXECUTED,
            EE.FILL_VERDICT_UNKNOWN: EV.EXECUTION_STATUS_UNKNOWN,
        }
        for verdict, status in expected.items():
            self.assertEqual(status, EV.status_from_verdict(verdict), verdict)
        # 穷尽：契约里每个 verdict 都有归宿。
        self.assertEqual(set(EE.FILL_VERDICTS), set(EV.VERDICT_TO_STATUS))

    def test_only_fill_verified_yields_a_verified_gate_state(self):
        verified = [
            verdict for verdict, status in EV.VERDICT_TO_STATUS.items()
            if status == EV.EXECUTION_STATUS_VERIFIED
        ]
        self.assertEqual([EE.FILL_VERDICT_VERIFIED], verified)

    def test_unknown_verdict_string_fails_closed(self):
        """契约之外的 verdict 不得被当成成交。"""
        self.assertEqual(
            EV.EXECUTION_STATUS_UNKNOWN, EV.status_from_verdict("brand_new_verdict"))
        self.assertEqual(EV.EXECUTION_STATUS_UNKNOWN, EV.status_from_verdict(None))

    def test_no_evidence_at_all_is_unknown_not_verified(self):
        verdict = EV.verification_from_evidence(None)
        self.assertEqual(EV.EXECUTION_STATUS_UNKNOWN, verdict["execution_status"])
        self.assertFalse(verdict["execution_verified"])
        self.assertEqual(EV.EVIDENCE_SOURCE_ABSENT, verdict["execution_evidence_source"])


class LegacyCompatibilityTest(unittest.TestCase):
    """Phase 4：旧订单没有证据 → ``unknown``，**禁止**自动升级为 ``filled``。"""

    def test_legacy_order_without_fill_rows_stays_unknown(self):
        verdict = EV.legacy_verification(has_fill_rows=False)
        self.assertEqual(EV.EXECUTION_STATUS_UNKNOWN, verdict["execution_status"])
        self.assertFalse(verdict["execution_verified"])
        self.assertEqual(EV.EVIDENCE_SOURCE_LEGACY, verdict["execution_evidence_source"])

    def test_stored_filled_without_any_fill_row_does_not_verify(self):
        """**核心回归（M56）**：``status='filled'`` 但没有任何流水 → 不是成交。"""
        conn = _db()
        order_id = _insert_order(conn, status="filled")
        verdict = EV.stamp_order(conn, order_id)
        self.assertEqual(EV.EXECUTION_STATUS_UNKNOWN, verdict["execution_status"])
        self.assertFalse(verdict["execution_verified"])
        row = conn.execute(
            "SELECT execution_status,execution_verified FROM paper_orders WHERE id=?",
            (order_id,),
        ).fetchone()
        self.assertEqual(EV.EXECUTION_STATUS_UNKNOWN, row["execution_status"])
        self.assertEqual(0, row["execution_verified"])

    def test_backfill_marks_old_rows_unknown_and_never_verified(self):
        """旧行一律 ``unknown`` 且 ``execution_verified=0``。

        这条旧行同时自称 ``status='filled'``，所以来源报更具体的
        ``evidence_inconsistent``（"写着成交却没有任何流水"）——那是审计要看的
        完整性问题。无论来源怎么写，结论都必须**不是** verified。
        """
        conn = _db()
        legacy = _insert_order(conn, status="filled")
        conn.execute(
            "UPDATE paper_orders SET execution_status=NULL,execution_verified=NULL"
            " WHERE id=?", (legacy,))
        result = EV.backfill_legacy_orders(conn)
        self.assertEqual(1, result["stamped"])
        row = conn.execute(
            "SELECT execution_status,execution_verified,execution_evidence_source"
            " FROM paper_orders WHERE id=?", (legacy,)).fetchone()
        self.assertEqual(EV.EXECUTION_STATUS_UNKNOWN, row["execution_status"])
        self.assertEqual(0, row["execution_verified"])
        self.assertEqual(EV.EVIDENCE_SOURCE_INCONSISTENT,
                         row["execution_evidence_source"])

    def test_legacy_row_that_never_claimed_a_fill_keeps_the_legacy_source(self):
        """对照：不自称成交的旧行 → 来源是普通的 ``legacy_row_without_fill_evidence``。"""
        conn = _db()
        legacy = _insert_order(conn, status="cancelled")
        conn.execute(
            "UPDATE paper_orders SET reason='撤单',execution_status=NULL,"
            " execution_verified=NULL WHERE id=?", (legacy,))
        EV.backfill_legacy_orders(conn)
        row = conn.execute(
            "SELECT execution_status,execution_verified,execution_evidence_source"
            " FROM paper_orders WHERE id=?", (legacy,)).fetchone()
        self.assertEqual(0, row["execution_verified"])
        self.assertNotEqual(EV.EXECUTION_STATUS_VERIFIED, row["execution_status"])
        self.assertEqual(EV.EVIDENCE_SOURCE_LEGACY, row["execution_evidence_source"])

    def test_backfill_is_idempotent_and_does_not_touch_verified_rows(self):
        conn = _db()
        _insert_order(conn, status="filled")  # a legacy row the backfill must stamp
        verified = _insert_order(conn, status="filled")
        _insert_fill(conn, verified)
        EV.stamp_order(conn, verified)
        first = EV.backfill_legacy_orders(conn)
        second = EV.backfill_legacy_orders(conn)
        self.assertEqual(1, first["stamped"])
        self.assertEqual(0, second["stamped"], "重复回填不得重复写")
        row = conn.execute(
            "SELECT execution_verified FROM paper_orders WHERE id=?",
            (verified,)).fetchone()
        self.assertEqual(1, row["execution_verified"], "已验证的行不得被回填改写")

    def test_gate_predicate_excludes_null_legacy_rows(self):
        """谓词必须 fail closed：NULL 行被 ``COALESCE`` 取 0 而排除。"""
        conn = _db()
        legacy = _insert_order(conn, status="filled")
        conn.execute(
            "UPDATE paper_orders SET execution_status=NULL,execution_verified=NULL"
            " WHERE id=?", (legacy,))
        found = conn.execute(
            f"SELECT COUNT(*) FROM paper_orders WHERE {EV.VERIFIED_PREDICATE}"
        ).fetchone()[0]
        self.assertEqual(0, found, "旧行（NULL）绝不能被算成已验证成交")

    def test_gate_predicate_rejects_inconsistent_columns(self):
        """两列不一致（被手工改过）→ fail closed，不算已验证。"""
        conn = _db()
        order_id = _insert_order(conn, status="filled")
        conn.execute(
            "UPDATE paper_orders SET execution_verified=1,execution_status='unknown'"
            " WHERE id=?", (order_id,))
        found = conn.execute(
            f"SELECT COUNT(*) FROM paper_orders WHERE {EV.VERIFIED_PREDICATE}"
        ).fetchone()[0]
        self.assertEqual(0, found, "execution_status 不是 verified 就不许放行")


class WritePathStampTest(unittest.TestCase):
    """写入路径：``commit_fill`` 落库后必须盖章，且盖章结论来自证据。"""

    def test_full_fill_is_stamped_verified(self):
        conn = _db()
        order_id = _insert_order(conn, status="filled", qty=100, price=10.0)
        _insert_fill(conn, order_id, qty=100, price=10.0)
        verdict = EV.stamp_order(conn, order_id)
        self.assertEqual(EV.EXECUTION_STATUS_VERIFIED, verdict["execution_status"])
        self.assertTrue(verdict["execution_verified"])

    def test_partial_fill_is_stamped_partial_not_verified(self):
        """**M57 对照**：部分成交只能是 ``partial``，绝不上调为 ``verified``。"""
        conn = _db()
        order_id = _insert_order(conn, status="filled", qty=100, price=10.0)
        _insert_fill(conn, order_id, qty=40, price=10.0)
        verdict = EV.stamp_order(conn, order_id)
        self.assertEqual(EV.EXECUTION_STATUS_PARTIAL, verdict["execution_status"])
        self.assertFalse(verdict["execution_verified"])

    def test_rejected_order_is_not_executed_not_unknown(self):
        conn = _db()
        order_id = _insert_order(conn, status="rejected", qty=100)
        conn.execute(
            "UPDATE paper_orders SET reason='资金不足' WHERE id=?", (order_id,))
        verdict = EV.stamp_order(conn, order_id)
        self.assertEqual(EV.EXECUTION_STATUS_NOT_EXECUTED, verdict["execution_status"])
        self.assertFalse(verdict["execution_verified"])

    def test_mismatched_fill_identity_does_not_verify_the_order(self):
        """身份不符的流水（串账户/方向/标的）不得验证这笔委托。"""
        conn = _db()
        order_id = _insert_order(conn, status="filled", side="buy", code=CODE)
        _insert_fill(conn, order_id, side="sell", code="600002", account_id="other")
        verdict = EV.stamp_order(conn, order_id)
        self.assertFalse(verdict["execution_verified"], verdict)
        self.assertNotEqual(EV.EXECUTION_STATUS_VERIFIED, verdict["execution_status"])

    def test_stamp_is_idempotent(self):
        conn = _db()
        order_id = _insert_order(conn, status="filled")
        _insert_fill(conn, order_id)
        first = EV.stamp_order(conn, order_id)
        second = EV.stamp_order(conn, order_id)
        self.assertEqual(first, second)


class MigrationTest(unittest.TestCase):
    """Phase 3：三列只**新增**，不覆盖旧字段。"""

    def test_columns_are_added_without_touching_existing_ones(self):
        conn = _db()
        # 预先写入旧字段，迁移后必须原样保留。
        order_id = _insert_order(conn, status="filled", qty=100, price=10.0)
        conn.execute("UPDATE paper_orders SET realized_pnl=42.5 WHERE id=?", (order_id,))
        PSM.ensure_execution_verification_columns(conn)
        row = conn.execute(
            "SELECT status,realized_pnl,execution_status FROM paper_orders WHERE id=?",
            (order_id,)).fetchone()
        self.assertEqual("filled", row["status"], "旧 status 不得被覆盖")
        self.assertEqual(42.5, row["realized_pnl"], "旧 realized_pnl 不得被覆盖")
        self.assertIsNone(row["execution_status"], "历史行保持 NULL，不自动升级")

    def test_migration_is_idempotent(self):
        conn = _db()  # _db() already applied it once
        again = PSM.ensure_execution_verification_columns(conn)
        third = PSM.ensure_execution_verification_columns(conn)
        self.assertEqual((), again.get("paper_orders"), "重复迁移不得重复加列")
        self.assertEqual((), third.get("paper_orders"), "重复迁移不得重复加列")

    def test_migration_adds_the_columns_to_a_bare_table(self):
        """对照：在一个**没有**闸门列的账本上，迁移必须真的补上三列。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE paper_orders (id INTEGER PRIMARY KEY, status TEXT)")
        added = PSM.ensure_execution_verification_columns(conn)
        self.assertEqual(
            ("execution_status", "execution_verified", "execution_evidence_source"),
            added["paper_orders"],
        )

    def test_archive_table_gets_the_same_columns(self):
        """归档表列集必须与活跃表一致（retention 用 SELECT * 整行拷贝）。"""
        conn = _db()
        conn.execute(
            "CREATE TABLE paper_orders_archive AS SELECT * FROM paper_orders WHERE 0")
        PSM.ensure_execution_verification_columns(conn)
        archive_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(paper_orders_archive)")}
        for column in ("execution_status", "execution_verified",
                       "execution_evidence_source"):
            self.assertIn(column, archive_cols, column)

    def test_migration_is_on_the_canonical_startup_path(self):
        import db_migrate
        handlers = [entry[2] for entry in db_migrate.MIGRATIONS["paper_trading"]]
        self.assertIn(PSM.ensure_execution_verification_columns, handlers)


class GateReportTest(unittest.TestCase):
    def test_every_row_lands_in_exactly_one_state(self):
        report = EV.gate_report([
            {"execution_status": EV.EXECUTION_STATUS_VERIFIED},
            {"execution_status": EV.EXECUTION_STATUS_PARTIAL},
            {"execution_status": EV.EXECUTION_STATUS_UNKNOWN},
            {"execution_status": EV.EXECUTION_STATUS_NOT_EXECUTED},
            {"execution_status": None},
            {"execution_status": "nonsense"},
        ])
        self.assertEqual(6, report["total"])
        self.assertEqual(1, report["verified"])
        self.assertEqual(5, report["blocked_from_execution_stats"])
        self.assertEqual(6, sum(report["counts"].values()), "不得静默丢行")

    def test_empty_input_is_empty_safe(self):
        report = EV.gate_report([])
        self.assertEqual(0, report["total"])
        self.assertEqual(0, report["verified"])


class ExecutionVerificationSqlGateTest(unittest.TestCase):
    """闸门谓词在生产读路径里真的生效（端到端，真实 paper_trading 代码）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "paper_trading.sqlite3")

    def _ledger(self):
        import paper_trading as PT
        conn = sqlite3.connect(self.db_path)
        # Windows refuses to delete a temp dir while a handle is open, so every
        # connection this helper opens is registered for cleanup.
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                side TEXT NOT NULL, code TEXT NOT NULL, qty INTEGER NOT NULL,
                planned_price REAL, filled_price REAL, amount REAL, fees REAL,
                status TEXT NOT NULL, reason TEXT, risk_payload TEXT NOT NULL,
                realized_pnl REAL, created_at TEXT NOT NULL, executed_at TEXT,
                order_type TEXT NOT NULL DEFAULT 'market',
                origin TEXT NOT NULL DEFAULT 'strategy'
            );
            CREATE TABLE paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, side TEXT NOT NULL, code TEXT NOT NULL,
                qty INTEGER NOT NULL, price REAL NOT NULL, amount REAL NOT NULL,
                fees REAL NOT NULL, fill_date TEXT NOT NULL, quote_at TEXT,
                assumption TEXT NOT NULL
            );
            """
        )
        PSM.ensure_execution_verification_columns(conn)
        return PT, conn

    def test_rebuild_realized_pnl_ignores_unverified_filled_orders(self):
        """**核心回归（M56 消费侧）**：没有流水的 filled 行不得进已实现盈亏重算。"""
        PT, conn = self._ledger()
        # 一条有真实流水（会盖章 verified）、一条只有自称 filled。
        good = _insert_order(conn, side="buy", qty=100, price=10.0)
        _insert_fill(conn, good, qty=100, price=10.0)
        EV.stamp_order(conn, good)
        ghost = _insert_order(conn, side="sell", qty=100, price=12.0,
                              amount=1200.0, fees=5.0)
        conn.commit()
        # 自称成交的卖出没有任何流水 → 未验证。
        ghost_row = conn.execute(
            "SELECT execution_status,execution_verified FROM paper_orders WHERE id=?",
            (ghost,)).fetchone()
        self.assertNotEqual(EV.EXECUTION_STATUS_VERIFIED, ghost_row["execution_status"])

        # 重算前把两条的 realized_pnl 都置空，观察闸门是否放行 ghost。
        conn.execute("UPDATE paper_orders SET realized_pnl=NULL")
        conn.commit()
        PT._rebuild_realized_pnl(conn)
        conn.commit()
        good_pnl = conn.execute(
            "SELECT realized_pnl FROM paper_orders WHERE id=?", (good,)).fetchone()[0]
        ghost_pnl = conn.execute(
            "SELECT realized_pnl FROM paper_orders WHERE id=?", (ghost,)).fetchone()[0]
        self.assertIsNone(ghost_pnl,
                          "未验证的自称成交绝不能被写入已实现盈亏")
        self.assertIsNone(good_pnl, "买入腿本身不产生已实现盈亏")

    def test_unverified_sell_is_excluded_from_the_verified_predicate(self):
        PT, conn = self._ledger()
        sell = _insert_order(conn, side="sell", qty=100, price=12.0,
                             amount=1200.0, fees=5.0)
        conn.commit()
        matched = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE id=? AND "
            + PT._execution_verified_predicate(), (sell,)).fetchone()[0]
        self.assertEqual(0, matched)

    def test_verified_sell_is_admitted(self):
        """对照：真有流水的卖出必须被放行（防止过度收紧）。"""
        PT, conn = self._ledger()
        sell = _insert_order(conn, side="sell", qty=100, price=12.0,
                             amount=1200.0, fees=5.0)
        _insert_fill(conn, sell, side="sell", qty=100, price=12.0)
        EV.stamp_order(conn, sell)
        conn.commit()
        matched = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE id=? AND "
            + PT._execution_verified_predicate(), (sell,)).fetchone()[0]
        self.assertEqual(1, matched)


class PositionPathGateTest(unittest.TestCase):
    """**M60 回归**：持仓读模型（``_position_rows``）的现金流必须过闸门。

    ``_position_rows`` 用一条按 (account, code) 汇总 ``paper_orders`` 的查询算
    ``display_cost``（摊薄成本）。若那条查询漏掉闸门，一条**未验证**的自称成交
    就会改变持仓成本 —— 用"账本自称"改写持仓估值，正是本层要消灭的幻觉。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "paper_trading.sqlite3")

    def _ledger(self):
        import paper_trading as PT
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE paper_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, capital REAL NOT NULL, risk_profile TEXT NOT NULL,
                started_at TEXT, ended_at TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE paper_position_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT, industry TEXT,
                qty INTEGER NOT NULL, remaining_qty INTEGER NOT NULL, cost REAL NOT NULL,
                acquired_at TEXT NOT NULL, available_date TEXT NOT NULL,
                asset_type TEXT NOT NULL DEFAULT 'stock_t1', source_order_id INTEGER,
                cost_fee_included INTEGER NOT NULL DEFAULT 0,
                is_t_base INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE paper_positions (
                account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT, industry TEXT,
                qty INTEGER NOT NULL, cost REAL NOT NULL, entry_date TEXT NOT NULL,
                available_date TEXT NOT NULL,
                asset_type TEXT NOT NULL DEFAULT 'stock_t1', peak_price REAL,
                take_stage INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account_id, code)
            );
            CREATE TABLE paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                side TEXT NOT NULL, code TEXT NOT NULL, qty INTEGER NOT NULL,
                planned_price REAL, filled_price REAL, amount REAL, fees REAL,
                status TEXT NOT NULL, reason TEXT, risk_payload TEXT NOT NULL,
                realized_pnl REAL, created_at TEXT NOT NULL, executed_at TEXT,
                order_type TEXT NOT NULL DEFAULT 'market',
                origin TEXT NOT NULL DEFAULT 'strategy'
            );
            CREATE TABLE paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, side TEXT NOT NULL, code TEXT NOT NULL,
                qty INTEGER NOT NULL, price REAL NOT NULL, amount REAL NOT NULL,
                fees REAL NOT NULL, fill_date TEXT NOT NULL, quote_at TEXT,
                assumption TEXT NOT NULL
            );
            INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,updated_at)
                VALUES('c1','running',100000.0,'aggressive','2024-06-18','2024-06-18');
            INSERT INTO paper_position_lots(cycle_id,account_id,code,qty,remaining_qty,
                cost,acquired_at,available_date,asset_type)
                VALUES(1,'tq_breakout','600001',100,100,10.0,'2024-06-18','2024-06-18','stock_t1');
            """
        )
        PSM.ensure_execution_verification_columns(conn)
        return PT, conn

    def test_unverified_fill_does_not_change_the_position_cost(self):
        PT, conn = self._ledger()
        # A verified buy at 10.0 (so display_cost == cost == 10.0).
        good = _insert_order(conn, side="buy", qty=100, price=10.0,
                             amount=1000.0, fees=0.0)
        _insert_fill(conn, good, qty=100, price=10.0, fees=0.0)
        EV.stamp_order(conn, good)
        conn.commit()

        def display_cost():
            rows = PT._position_rows(conn, asof_day=dt.date(2024, 6, 18),
                                     readonly=True)
            return rows[0]["display_cost"] if rows else None

        baseline = display_cost()
        self.assertAlmostEqual(10.0, baseline, places=6)

        # An UNVERIFIED buy claiming filled (no fill rows) at a wildly different
        # price must not move the position cost.
        ghost = _insert_order(conn, side="buy", qty=100, price=99.0,
                              amount=9900.0, fees=0.0)
        conn.commit()
        ghost_row = conn.execute(
            "SELECT execution_status FROM paper_orders WHERE id=?",
            (ghost,)).fetchone()
        self.assertNotEqual(EV.EXECUTION_STATUS_VERIFIED, ghost_row["execution_status"])

        after = display_cost()
        self.assertAlmostEqual(
            baseline, after, places=6,
            msg="未验证的自称成交不得改写持仓成本（display_cost）",
        )

    def test_verified_fill_does_change_the_position_cost(self):
        """对照：**已验证**的成交必须照常影响持仓成本（防止过度收紧）。"""
        PT, conn = self._ledger()
        good = _insert_order(conn, side="buy", qty=100, price=10.0,
                             amount=1000.0, fees=0.0)
        _insert_fill(conn, good, qty=100, price=10.0, fees=0.0)
        EV.stamp_order(conn, good)
        # A second, also-verified buy at a higher price.
        second = _insert_order(conn, side="buy", qty=100, price=20.0,
                               amount=2000.0, fees=0.0)
        _insert_fill(conn, second, qty=100, price=20.0, fees=0.0)
        EV.stamp_order(conn, second)
        conn.commit()
        rows = PT._position_rows(conn, asof_day=dt.date(2024, 6, 18), readonly=True)
        # net invested = 3000 over 100 lot shares -> display_cost 30.0
        self.assertAlmostEqual(30.0, rows[0]["display_cost"], places=6)


class CommitFillStampsTest(unittest.TestCase):
    """``execution_planner.commit_fill`` 必须真的盖章（生产写入路径）。"""

    def test_commit_fill_stamps_the_order_it_just_filled(self):
        import types
        from unittest import mock

        import execution_planner as EP

        conn = _db()
        order_id = _insert_order(conn, status="pending", qty=100, price=10.0,
                                 amount=1000.0, fees=5.0)
        stub = types.SimpleNamespace(
            _assert_active_lease=lambda conn, label: None,
            _reserve_shared_capital=lambda *a, **k: (True, None),
            _debit_shared_cash=lambda *a, **k: None,
            _finish_capital_reservation=lambda *a, **k: None,
            _record_lot=lambda *a, **k: None,
            _consume_available_lots=lambda conn, account_id, code, qty, day: (qty, 0.0),
            _credit_shared_cash=lambda *a, **k: None,
            _json=lambda value: "{}",
            _now=lambda: SESSION + " 15:00:00",
            _date=lambda day: day,
            _num=lambda value, default=0.0: default if value in (None, "") else float(value),
            _risk_log=lambda *a, **k: None,
            _audit=lambda *a, **k: None,
            _sync_positions=lambda *a, **k: None,
        )
        plan = {
            "side": "buy", "code": CODE, "qty": 100, "amount": 1000.0,
            "fees": 5.0, "fill_price": 10.0, "quote_at": SESSION + "T10:00:00",
        }
        with mock.patch.object(EP, "_pt", lambda: stub):
            EP.commit_fill(
                conn, account={"id": ACCOUNT}, plan=plan, order_id=order_id,
                asof_day=dt.date(2024, 6, 18), reserved=False,
                action="strategy_buy", reason="test",
            )
        row = conn.execute(
            "SELECT status,execution_status,execution_verified FROM paper_orders"
            " WHERE id=?", (order_id,)).fetchone()
        self.assertEqual("filled", row["status"])
        self.assertEqual(EV.EXECUTION_STATUS_VERIFIED, row["execution_status"],
                         "commit_fill 必须为它刚写入的成交盖章 verified")
        self.assertEqual(1, row["execution_verified"])


if __name__ == "__main__":
    unittest.main()
