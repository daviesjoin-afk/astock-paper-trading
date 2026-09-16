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

    def test_backfill_verifies_a_legacy_row_that_has_fill_evidence(self):
        """对照：有**完整成交流水证据**的旧行必须被回填成 ``verified``。

        升级前由生产写路径落库、只是没有盖章的那些行就是真实成交。回填若一律
        只写 ``unknown``，上线后这些真实成交会永久停在"没有证据"，被闸门从
        已实现盈亏 / NAV / 执行绩效里整批剔除。
        """
        conn = _db()
        order_id = _insert_order(conn, status="filled")
        _insert_fill(conn, order_id)
        conn.execute(
            "UPDATE paper_orders SET execution_status=NULL,execution_verified=NULL"
            " WHERE id=?", (order_id,))
        result = EV.backfill_legacy_orders(conn)
        self.assertEqual(1, result["stamped"])
        self.assertEqual(1, result["verified"], "有证据的旧行必须被回填成已验证")
        row = conn.execute(
            "SELECT execution_status,execution_verified FROM paper_orders WHERE id=?",
            (order_id,)).fetchone()
        self.assertEqual(EV.EXECUTION_STATUS_VERIFIED, row["execution_status"])
        self.assertEqual(1, row["execution_verified"])

    def test_backfill_never_upgrades_a_row_without_evidence(self):
        """对照的另一半：没有证据的旧行**绝不**升级 —— 那正是本层要消灭的幻觉。"""
        conn = _db()
        order_id = _insert_order(conn, status="filled")
        conn.execute(
            "UPDATE paper_orders SET execution_status=NULL,execution_verified=NULL"
            " WHERE id=?", (order_id,))
        result = EV.backfill_legacy_orders(conn)
        self.assertEqual(0, result["verified"])
        row = conn.execute(
            "SELECT execution_verified FROM paper_orders WHERE id=?",
            (order_id,)).fetchone()
        self.assertEqual(0, row["execution_verified"])

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


class RowPredicateAgreementTest(unittest.TestCase):
    """Python 侧谓词必须与 SQL 谓词**逐值对齐**并同样 fail closed。

    两边不一致时，同一行会在"SQL 读路径"与"行列已在手的读路径"之间得到相反
    结论 —— 执行绩效就会出现两个数字。这条测试把两份谓词钉在一起。

    **oracle 是真实 SQLite，不是手写预期**。早期版本用 ``int(flag)`` 判断，
    于是 ``execution_verified = 1.1`` 被截断成 ``1``：Python 判成交、SQL 拒绝。
    所以这里的做法是：把矩阵**真实写进** SQLite → 用 ``VERIFIED_PREDICATE`` 查一遍
    → 把同一批行读回来喂给 ``is_verified_row()`` → 比较两边选中的 **id 集合**。
    """

    #: 两列一致性的基础用例集（旧行、缺列、状态不一致）。
    CASES = (
        ("verified", 1, True),
        ("verified", 0, False),      # 两列不一致 → fail closed
        ("unknown", 1, False),
        ("partial", 1, False),
        ("not_executed", 1, False),
        (None, None, False),         # 旧行
        (None, 1, False),
        ("verified", None, False),
        ("", 1, False),
    )

    @staticmethod
    def _probe_db(declared):
        """一个**未声明类型** / 指定 affinity 的探针表。

        ``declared`` 为空时保留 SQLite 原生动态类型（BLOB affinity），这样
        ``b"1"`` 之类的值真的以 BLOB 存储类落库，才能观察到真实行为。
        生产列声明的是 ``INTEGER``，所以同时用 INTEGER affinity 覆盖一遍：
        affinity 会改变**存进去**的值（``"1"`` 存成整数 1），两侧都必须跟着变。
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE predicate_probe (id INTEGER PRIMARY KEY, label TEXT,"
            f" execution_verified {declared}, execution_status TEXT)"
        )
        return conn

    def _agreement(self, cases, declared=""):
        """把 cases 写进真实 SQLite，比较 SQL 与 Python 选中的 id 集合。"""
        conn = self._probe_db(declared)
        for index, (label, flag, status) in enumerate(cases, start=1):
            conn.execute(
                "INSERT INTO predicate_probe"
                "(id,label,execution_verified,execution_status) VALUES(?,?,?,?)",
                (index, label, flag, status),
            )
        conn.commit()
        sql_ids = [
            int(row["id"]) for row in conn.execute(
                f"SELECT id FROM predicate_probe WHERE {EV.VERIFIED_PREDICATE}"
                " ORDER BY id")
        ]
        rows = [
            dict(row) for row in
            conn.execute("SELECT * FROM predicate_probe ORDER BY id")
        ]
        py_ids = [int(row["id"]) for row in rows if EV.is_verified_row(row)]
        labels = {index: label for index, (label, _f, _s) in enumerate(cases, 1)}
        return sql_ids, py_ids, labels, conn

    def test_row_predicate_agrees_with_the_sql_predicate(self):
        conn = _db()
        expected = set()
        for index, (status, flag, verified) in enumerate(self.CASES, start=1):
            order_id = _insert_order(conn, status="filled")
            conn.execute(
                "UPDATE paper_orders SET execution_status=?,execution_verified=?"
                " WHERE id=?", (status, flag, order_id))
            if verified:
                expected.add(order_id)
        rows = [dict(row) for row in conn.execute(
            "SELECT id,execution_status,execution_verified FROM paper_orders")]
        sql_verified = {
            int(row["id"]) for row in conn.execute(
                f"SELECT id FROM paper_orders WHERE {EV.VERIFIED_PREDICATE}")
        }
        py_verified = {int(row["id"]) for row in rows if EV.is_verified_row(row)}
        self.assertEqual(sql_verified, py_verified,
                         "SQL 谓词与 Python 谓词对同一批行给出了不同结论")
        self.assertEqual(expected, sql_verified,
                         "只有两列一致且为 verified 的行才算已验证")
        self.assertTrue(expected, "用例集本身不能空转")

    #: 完整类型矩阵：SQLite 允许没有 CHECK 约束的列存任意存储类。
    #:   flag: 1 / 1.0 / 1.1 / 1.9 / 0 / 2 / "1" / "1.0" / b"1" / True / False / None
    #:   status: verified / unknown / partial / not_executed / None / b"verified" / 1 / "VERIFIED"
    TYPE_MATRIX = (
        ("flag_int_1", 1, "verified"),
        ("flag_float_1_0", 1.0, "verified"),
        ("flag_float_1_1", 1.1, "verified"),
        ("flag_float_1_9", 1.9, "verified"),
        ("flag_int_0", 0, "verified"),
        ("flag_int_2", 2, "verified"),
        ("flag_str_1", "1", "verified"),
        ("flag_str_1_0", "1.0", "verified"),
        ("flag_blob_1", b"1", "verified"),
        ("flag_bool_true", True, "verified"),
        ("flag_bool_false", False, "verified"),
        ("flag_null", None, "verified"),
        ("status_unknown", 1, "unknown"),
        ("status_partial", 1, "partial"),
        ("status_not_executed", 1, "not_executed"),
        ("status_null", 1, None),
        ("status_blob", 1, b"verified"),
        ("status_int", 1, 1),
        ("status_upper", 1, "VERIFIED"),
        ("both_null", None, None),
    )

    def test_full_type_matrix_agrees_on_an_untyped_column(self):
        """**核心回归**：未声明类型的列上，SQL 与 Python 必须选中同一批行。

        未声明类型 → BLOB affinity，值以原始存储类落库。旧实现 ``int(flag)`` 会
        在这里多选 ``1.1`` / ``"1"`` / ``b"1"`` / ``1.9``。
        """
        sql_ids, py_ids, labels, _conn = self._agreement(self.TYPE_MATRIX, "")
        self.assertEqual(
            sql_ids, py_ids,
            "SQL 与 Python 选中的行不一致："
            f"SQL-only={[labels[i] for i in sorted(set(sql_ids) - set(py_ids))]} "
            f"Python-only={[labels[i] for i in sorted(set(py_ids) - set(sql_ids))]}")
        # 非空转保护：矩阵里必须有真值行，否则"两边都选空"会假性通过。
        self.assertTrue(sql_ids, "矩阵必须至少选中一行，否则一致性是假的")

    def test_full_type_matrix_agrees_on_an_integer_affinity_column(self):
        """生产列声明的是 INTEGER：affinity 会改变存入值，两侧必须同步改变。

        在 INTEGER affinity 下 ``"1"`` / ``"1.0"`` / ``True`` 会被**存成整数 1**，
        因此 SQL 会选中它们；Python 读到的也是整数 1，同样选中。这条测试锁住
        "Python 不必自己解释字符串"，因为 SQLite 在写入时已经完成了转换。
        """
        sql_ids, py_ids, labels, _conn = self._agreement(self.TYPE_MATRIX, "INTEGER")
        self.assertEqual(
            sql_ids, py_ids,
            "SQL 与 Python 选中的行不一致："
            f"SQL-only={[labels[i] for i in sorted(set(sql_ids) - set(py_ids))]} "
            f"Python-only={[labels[i] for i in sorted(set(py_ids) - set(sql_ids))]}")
        self.assertTrue(sql_ids, "矩阵必须至少选中一行")

    def test_fractional_verified_flag_does_not_pass_python_gate(self):
        """**独立回归**：``execution_verified = 1.1`` 不得通过 Python 闸门。

        旧实现 ``int(1.1) == 1`` 会放行；SQLite 的 ``COALESCE(v,0) = 1`` 拒绝它。
        """
        self.assertFalse(EV.is_verified_row({
            "execution_verified": 1.1,
            "execution_status": "verified",
        }))
        # 同样必须拒绝的其它"像 1"的值
        for flag in (1.9, 2, 0.5, "1", "1.0", b"1", "yes"):
            self.assertFalse(
                EV.is_verified_row({
                    "execution_verified": flag,
                    "execution_status": "verified",
                }), flag)
        # 对照：真正等价于 SQL 的值必须通过
        for flag in (1, 1.0, True):
            self.assertTrue(
                EV.is_verified_row({
                    "execution_verified": flag,
                    "execution_status": "verified",
                }), flag)

    def test_blob_and_text_flags_never_verify(self):
        """BLOB / TEXT 存储类与数值 1 比较恒为假 —— SQL 与 Python 都必须拒绝。"""
        sql_ids, py_ids, labels, _conn = self._agreement((
            ("blob_1", b"1", "verified"),
            ("text_1", "1", "verified"),
            ("int_1", 1, "verified"),
        ), "")
        self.assertEqual(sql_ids, py_ids)
        self.assertEqual([3], sql_ids, [(labels[i]) for i in sql_ids])

    def test_row_predicate_rejects_rows_without_the_columns(self):
        """归档快照 / 旧形状的行没有这两列 → 一律不算成交（fail closed）。"""
        self.assertFalse(EV.is_verified_row({"status": "filled", "side": "sell"}))
        self.assertFalse(EV.is_verified_row({"execution_status": "verified"}))
        self.assertFalse(EV.is_verified_row(None))
        self.assertFalse(EV.is_verified_row({"execution_status": "verified",
                                             "execution_verified": "yes"}))
        self.assertTrue(EV.is_verified_row({"execution_status": "verified",
                                            "execution_verified": True}))

    def test_row_predicate_reads_every_supported_row_shape(self):
        """dict / Mapping / sqlite3.Row / 普通对象 都必须能读，且结论一致。"""
        conn = self._probe_db("INTEGER")
        conn.execute(
            "INSERT INTO predicate_probe(label,execution_verified,execution_status)"
            " VALUES('ok',1,'verified')")
        conn.execute(
            "INSERT INTO predicate_probe(label,execution_verified,execution_status)"
            " VALUES('bad',0,'verified')")
        conn.commit()

        class VerifiedObjectRow:
            execution_verified = 1
            execution_status = "verified"

        class UnverifiedObjectRow:
            execution_verified = 0
            execution_status = "verified"

        sql_row = conn.execute(
            "SELECT * FROM predicate_probe WHERE id=1").fetchone()
        self.assertTrue(EV.is_verified_row(sql_row), "sqlite3.Row 形状")
        self.assertTrue(EV.is_verified_row(
            {"execution_verified": 1, "execution_status": "verified"}), "dict 形状")
        self.assertTrue(EV.is_verified_row(VerifiedObjectRow()), "属性形状")
        self.assertFalse(EV.is_verified_row(UnverifiedObjectRow()),
                         "属性形状也必须 fail closed")
        # sqlite3.Row 与同一行的 dict 视图必须给出相同结论
        bad_row = conn.execute(
            "SELECT * FROM predicate_probe WHERE id=2").fetchone()
        self.assertEqual(
            EV.is_verified_row(bad_row),
            EV.is_verified_row(dict(bad_row)),
            "同一行的 Row 与 dict 形状结论不一致",
        )


if __name__ == "__main__":
    unittest.main()
