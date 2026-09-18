# -*- coding: utf-8 -*-
"""Round-6：延迟成交不得跨越周期边界（Blocker 1 / Blocker 2 回归）。

本文件覆盖规格 §14–§19（Regression 1..6）、§12（订单身份）、§20–§21（重试血缘）
与 §13（真实可达路径 E2E）。

全部用例都驱动**真实**生产 schema 与**真实**生产原语（``paper_trading.init_db``
+ ``execution_planner.commit_fill``）。手写最小 DDL 只能证明夹具自洽：本轮两个
缺陷恰恰是「订单行与 lot 行的周期可以不一致」，而那个不变量由生产代码持有。

不变量（本文件存在的全部理由）：

    Order cycle provenance is a creation-time fact.
    An existing order must never acquire a different cycle because the active
    cycle changed before fill time.
    A legacy order may not manufacture a known lot cycle through a later
    active-cycle fallback.
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

import execution_planner as EP
import paper_trading as PT

ACCOUNT = "tq_breakout"
CODE = "600901"
NAME = "测试股"


class _ProvenRiskHarness(unittest.TestCase):
    """复用 ``test_paper_risk_exit_production_path`` 的**已验证**生产夹具。

    真实待成交扫描需要：新鲜且通过双源校验的行情、running 账户、真实 seed 买单
    + fill + lot。这些闸门由那个文件已经证明可用；自己再手写一份只会复现闸门
    而不是被测行为。这里只做最小适配（暴露 ``conn`` / ``quotes_map`` / ``day``）。
    """

    def setUp(self):
        import sqlite3

        import test_paper_risk_exit_production_path as RISK
        self._inner = RISK.TestPaperRiskExitProductionPath(
            "test_A_normal_running_account_full_risk_exit_pipeline"
        )
        self._inner.setUp()
        self.addCleanup(self._inner.doCleanups)
        self.addCleanup(self._inner.tearDown)
        self.account_id = "tq_breakout"
        self.day = self._inner.day
        self.code = self._inner.code
        # 该 harness 把行情字典与 DB 路径放在实例/模块上；这里直接借用，
        # 不复制一份，以免两边的夹具漂移。
        self.quotes_map = self._inner.quotes_map
        self.conn = sqlite3.connect(PT.DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

    # 把内层 harness 的私有助手转发出来，测试体读起来仍是「自己的夹具」。
    def _insert_lot(self, *args, **kwargs):
        return self._inner._insert_lot(*args, **kwargs)

    def _set_fresh_exit_quote(self, *args, **kwargs):
        return self._inner._set_fresh_exit_quote(*args, **kwargs)


class _LedgerCase(unittest.TestCase):
    """真实生产 schema 的临时账本：一条 active cycle + 一个账户。"""

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
        self.cycle = int(self.conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT,)
        ).fetchone()[0])
        self.cash_before = self._cash()

    def tearDown(self):
        self.conn.close()
        for patcher in reversed(self._patches):
            patcher.stop()
        self.tmp.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def _cash(self):
        row = self.conn.execute(
            "SELECT cash FROM paper_accounts WHERE id=?", (ACCOUNT,)
        ).fetchone()
        return None if row is None else float(row["cash"])

    def add_cycle(self, *, status="running", started_at="2026-09-20 09:30:00"):
        """再建一个周期（id 更大）并把它设为 active。

        ``_active_cycle`` 按 ``status IN ('draft','running','paused') ORDER BY id
        DESC LIMIT 1`` 解析，所以新行会立刻成为 active —— 这正是要模拟的
        「成交前 active cycle 变了」。
        """
        cur = self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,"
            "created_at,updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
            (f"c-{self.cycle + 1}", status, 100000.0, "shared_pool",
             started_at, started_at, started_at if status == "running" else None),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def active_cycle(self):
        return int(self.conn.execute(
            "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])

    def add_lot(self, cycle_id, qty, *, available_date="2026-09-01", code=CODE):
        self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,"
            "qty,remaining_qty,cost,acquired_at,available_date,asset_type,"
            "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, ACCOUNT, code, NAME, "测试", qty, qty, 10.0,
             "2026-08-31 10:00:00", available_date, "stock_t1", 1, 1),
        )
        self.conn.commit()

    def lot_remaining(self, cycle_id, code=CODE):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(remaining_qty),0) AS n FROM paper_position_lots"
            " WHERE cycle_id=? AND account_id=? AND code=?",
            (cycle_id, ACCOUNT, code),
        ).fetchone()
        return int(row["n"])

    def add_order(self, *, side, qty, cycle_id, status="pending_limit", code=CODE):
        """写一条订单。``cycle_id=None`` 表示 **legacy**（升级前创建的行）。

        legacy 行**无法**经 v18 的 INSERT guard 写入 —— 那正是 guard 的职责。要构造
        历史形状，必须暂时卸下 guard 再装回去（与 ``test_order_cycle_provenance``
        的既有做法一致）。这不是绕过被测行为：被测的是**成交阶段**读取 legacy 行
        时是否 fail closed，而不是 guard 本身。
        """
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        if cycle_id is None:
            import paper_schema_migrations as PSM
            self.conn.execute("DROP TRIGGER IF EXISTS trg_paper_orders_cycle_provenance_insert")
            try:
                cur = self.conn.execute(
                    "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                    "status,reason,risk_payload,created_at,origin,strategy_id,"
                    "strategy_version,strategy_checksum,cycle_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (ACCOUNT, side, code, NAME, qty, 10.0, status, "seed", "{}",
                     "2026-09-05 10:00:00", "manual", *stamp),
                )
            finally:
                PSM._ensure_order_cycle_provenance_guards(self.conn)
        else:
            cur = self.conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                "status,reason,risk_payload,created_at,origin,strategy_id,"
                "strategy_version,strategy_checksum,cycle_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ACCOUNT, side, code, NAME, qty, 10.0, status, "seed", "{}",
                 "2026-09-05 10:00:00", "manual", *stamp, cycle_id),
            )
        self.conn.commit()
        return int(cur.lastrowid)

    def assert_no_ledger_mutation(self, before, msg):
        """断言「0 business mutations」：lot / fill / order / 现金全部未变。"""
        after = self.counts()
        self.assertEqual(
            (after["lots"], after["fills"], after["orders"], self._cash()),
            (before["lots"], before["fills"], before["orders"], before["cash"]),
            msg,
        )

    def order_cycle(self, order_id):
        row = self.conn.execute(
            "SELECT cycle_id FROM paper_orders WHERE id=?", (order_id,)
        ).fetchone()
        return None if row["cycle_id"] is None else int(row["cycle_id"])

    def counts(self):
        def one(sql, params=()):
            return int(self.conn.execute(sql, params).fetchone()[0])
        return {
            "fills": one("SELECT COUNT(*) FROM paper_fills"),
            "lots": one("SELECT COUNT(*) FROM paper_position_lots"),
            "orders": one("SELECT COUNT(*) FROM paper_orders"),
            "cash": self._cash(),
        }

    def plan(self, *, side, qty, code=CODE, price=10.0):
        return {
            "side": side, "code": code, "qty": qty, "fill_price": price,
            "amount": qty * price, "fees": 5.0, "quote_at": "2026-09-05T10:00:00",
            "risk": {"x": 1},
        }

    def commit(self, order_id, *, side, qty, reserved=True, code=CODE, price=10.0):
        """经**生产** ``commit_fill`` 成交（reserved=True 跳过预占，聚焦周期归属）。"""
        with PT._db(immediate=True) as conn:
            return EP.commit_fill(
                conn,
                account={"id": ACCOUNT},
                plan=self.plan(side=side, qty=qty, code=code, price=price),
                order_id=order_id,
                asof_day=dt.date(2026, 9, 5),
                reserved=reserved,
                action="manual_filled",
                reason="测试成交",
            )


# ══════════════════════════════════════════════════════════════════════════
# §14 Regression 1 / §19 Regression 6：pending SELL 不得跨周期消费
# ══════════════════════════════════════════════════════════════════════════
class PendingSellStaysInItsOwnCycle(_LedgerCase):
    """一个 cycle 8 建的 pending SELL，在 cycle 9 激活后成交，仍只碰 cycle 8。"""

    def test_R1_pending_sell_consumes_only_its_own_cycle(self):
        """Regression 1：只消费 cycle 8 的 lot，cycle 9 的 lot 纹丝不动。"""
        self.add_lot(self.cycle, 100)
        order = self.add_order(side="sell", qty=100, cycle_id=self.cycle)
        later = self.add_cycle()
        self.add_lot(later, 100)  # 必须真的存在 cycle 9 底仓，否则断言是空的
        self.assertNotEqual(later, self.cycle)
        self.assertEqual(self.active_cycle(), later, "夹具必须真的把 active 切走")

        self.commit(order, side="sell", qty=100)

        self.assertEqual(self.lot_remaining(self.cycle), 0, "cycle 8 的 lot 应被消耗")
        self.assertEqual(self.lot_remaining(later), 100, "cycle 9 的 lot 不得被触碰")
        self.assertEqual(self.order_cycle(order), self.cycle, "订单周期不可变")

    def test_R6_post_v18_sell_prefers_durable_cycle_over_active(self):
        """Regression 6：durable provenance 优先于 current active state。"""
        self.add_lot(self.cycle, 100)
        order = self.add_order(side="sell", qty=100, cycle_id=self.cycle)
        self.add_cycle()  # active 变成新周期

        self.commit(order, side="sell", qty=100)

        self.assertEqual(self.lot_remaining(self.cycle), 0)
        row = self.conn.execute(
            "SELECT side, qty, order_id FROM paper_fills"
        ).fetchone()
        self.assertEqual(row["order_id"], order, "fill 必须挂在原订单上")
        self.assertEqual(row["side"], "sell")


# ══════════════════════════════════════════════════════════════════════════
# §15 Regression 2：cycle 8 不足时不得借 cycle 9
# ══════════════════════════════════════════════════════════════════════════
class InsufficientOwnCycleMustNotBorrow(_LedgerCase):
    """cycle 8 只有 50、cycle 9 有 100、要卖 100 ⇒ 必须整体失败。"""

    def test_R2_short_own_cycle_fails_instead_of_borrowing_the_next(self):
        self.add_lot(self.cycle, 50)
        order = self.add_order(side="sell", qty=100, cycle_id=self.cycle)
        later = self.add_cycle()
        self.add_lot(later, 100)
        before = self.counts()

        with self.assertRaises(RuntimeError):
            self.commit(order, side="sell", qty=100)

        # 四个「不得」：两边余额都不变、无 fill、无现金入账、订单未标记成交。
        self.assertEqual(self.lot_remaining(self.cycle), 50, "cycle 8 余额不得变化")
        self.assertEqual(self.lot_remaining(later), 100, "cycle 9 余额不得被借走")
        after = self.counts()
        self.assertEqual(after["fills"], before["fills"], "不得写入 fill")
        self.assertEqual(self._cash(), self.cash_before, "不得入账现金")
        status = self.conn.execute(
            "SELECT status FROM paper_orders WHERE id=?", (order,)
        ).fetchone()["status"]
        self.assertNotEqual(status, "filled", "订单不得被标记成交")


# ══════════════════════════════════════════════════════════════════════════
# §16 Regression 3 / §17 Regression 4：legacy NULL-cycle 必须 fail closed
# ══════════════════════════════════════════════════════════════════════════
class LegacyNullCycleFailsClosed(_LedgerCase):
    """升级前创建、升级后仍 pending 的订单：归属不可证明 ⇒ 不得成交。"""

    def test_R3_legacy_buy_must_not_manufacture_a_current_cycle_lot(self):
        """Regression 3：不得生成 lot.cycle_id = 当前 active cycle。"""
        order = self.add_order(side="buy", qty=100, cycle_id=None)
        self.assertEqual(self.active_cycle(), self.cycle)
        before = self.counts()

        with self.assertRaises(PT.OrderCycleProvenanceUnknown) as ctx:
            self.commit(order, side="buy", qty=100, reserved=False)
        self.assertEqual(ctx.exception.status, PT.ORDER_CYCLE_LEGACY_UNKNOWN)
        self.assertEqual(ctx.exception.marker, "legacy_order_cycle_unproven")

        after = self.counts()
        self.assertEqual(after["lots"], before["lots"], "不得创建任何 lot")
        self.assertEqual(after["fills"], before["fills"], "不得写入 fill")
        self.assertEqual(self._cash(), self.cash_before, "不得扣款")
        self.assertIsNone(self.order_cycle(order), "legacy 订单必须保持 NULL")

    def test_R4_legacy_sell_must_not_consume_the_current_cycle(self):
        """Regression 4：legacy SELL 不得消费当前周期的底仓。"""
        self.add_lot(self.cycle, 100)
        order = self.add_order(side="sell", qty=100, cycle_id=None)
        before = self.counts()

        with self.assertRaises(PT.OrderCycleProvenanceUnknown):
            self.commit(order, side="sell", qty=100)

        self.assertEqual(self.lot_remaining(self.cycle), 100, "不得消费 cycle 8")
        after = self.counts()
        self.assertEqual(after["fills"], before["fills"], "不得写入 fill")
        self.assertEqual(self._cash(), self.cash_before, "不得入账现金")
        self.assertIsNone(self.order_cycle(order), "legacy 订单必须保持 NULL")

    def test_legacy_null_cycle_never_becomes_sellable_through_fill(self):
        """「不知道」不能变成「可卖」：两种 legacy 都必须在写任何东西之前停下。"""
        buy = self.add_order(side="buy", qty=100, cycle_id=None, code="600902")
        sell = self.add_order(side="sell", qty=100, cycle_id=None, code="600903")
        self.add_lot(self.cycle, 100, code="600903")
        before = self.counts()

        for order, side, reserved in ((buy, "buy", False), (sell, "sell", True)):
            with self.assertRaises(PT.OrderCycleProvenanceUnknown):
                self.commit(order, side=side, qty=100, reserved=reserved,
                            code="600902" if side == "buy" else "600903")

        after = self.counts()
        self.assertEqual(
            (after["lots"], after["fills"], self._cash()),
            (before["lots"], before["fills"], self.cash_before),
            "legacy 成交被拒时不得有任何 ledger mutation（0 business mutations）",
        )


# ══════════════════════════════════════════════════════════════════════════
# §12 订单身份：plan/account 必须与落库订单一致
# ══════════════════════════════════════════════════════════════════════════
class OrderIdentityMismatchFailsClosed(_LedgerCase):
    """``order_id`` 来自 A 而 plan/account 来自 B ⇒ fail closed（不写账本）。"""

    def _stub_side_effects(self):
        """隔离现金/lot 副作用，让断言落在身份校验本身。"""
        stub = mock.Mock()
        stub._assert_active_lease = lambda *a, **k: None
        stub._reserve_shared_capital = lambda *a, **k: (True, None)
        for name in ("_debit_shared_cash", "_finish_capital_reservation",
                     "_credit_shared_cash", "_record_lot", "_risk_log",
                     "_audit", "_sync_positions"):
            setattr(stub, name, lambda *a, **k: None)
        stub._consume_available_lots = lambda *a, **k: (0, 0.0)
        stub._num = lambda value, default=0.0: default if value in (None, "") else float(value)
        stub._json = lambda value: value
        stub._date = lambda day: day
        stub._now = lambda: "2026-09-05 15:00:00"
        return stub

    def _commit_with(self, order_id, *, account_id, code, side, qty=100):
        with PT._db(immediate=True) as conn:
            return EP.commit_fill(
                conn, account={"id": account_id},
                plan=self.plan(side=side, qty=qty, code=code), order_id=order_id,
                asof_day=dt.date(2026, 9, 5), reserved=True,
                action="manual_filled", reason="测试成交",
            )

    def test_code_mismatch_is_rejected(self):
        order = self.add_order(side="sell", qty=100, cycle_id=self.cycle)
        self.add_lot(self.cycle, 100)
        before = self.counts()
        with mock.patch.object(EP, "_pt", lambda: self._stub_side_effects()):
            with self.assertRaisesRegex(RuntimeError, "order identity mismatch"):
                self._commit_with(order, account_id=ACCOUNT, code="600999", side="sell")
        self.assert_no_ledger_mutation(before, "身份不符时不得写任何账本事实")

    def test_account_mismatch_is_rejected(self):
        order = self.add_order(side="sell", qty=100, cycle_id=self.cycle)
        self.add_lot(self.cycle, 100)
        before = self.counts()
        with mock.patch.object(EP, "_pt", lambda: self._stub_side_effects()):
            with self.assertRaisesRegex(RuntimeError, "order identity mismatch"):
                self._commit_with(order, account_id="sector_rotation", code=CODE, side="sell")
        self.assert_no_ledger_mutation(before, "身份不符时不得写任何账本事实")

    def test_side_mismatch_is_rejected(self):
        order = self.add_order(side="sell", qty=100, cycle_id=self.cycle)
        self.add_lot(self.cycle, 100)
        before = self.counts()
        with mock.patch.object(EP, "_pt", lambda: self._stub_side_effects()):
            with self.assertRaisesRegex(RuntimeError, "order identity mismatch"):
                self._commit_with(order, account_id=ACCOUNT, code=CODE, side="buy")
        self.assert_no_ledger_mutation(before, "身份不符时不得写任何账本事实")


# ══════════════════════════════════════════════════════════════════════════
# §11 provenance 预检必须先于任何不可逆写
# ══════════════════════════════════════════════════════════════════════════
class ProvenancePrecheckPrecedesMutation(_LedgerCase):
    """不可证明的归属必须在 cash/lot 之前就被拒绝（0 business mutations）。"""

    def test_no_cash_moves_for_a_legacy_buy(self):
        order = self.add_order(side="buy", qty=100, cycle_id=None)
        before = self.counts()
        with self.assertRaises(PT.OrderCycleProvenanceUnknown):
            self.commit(order, side="buy", qty=100, reserved=False)
        self.assert_no_ledger_mutation(before, "买入的 legacy 拒付必须发生在扣款之前")

    def test_no_lot_moves_for_a_legacy_sell(self):
        self.add_lot(self.cycle, 100)
        order = self.add_order(side="sell", qty=100, cycle_id=None)
        before = self.counts()
        with self.assertRaises(PT.OrderCycleProvenanceUnknown):
            self.commit(order, side="sell", qty=100)
        self.assert_no_ledger_mutation(before, "卖出的 legacy 拒付必须发生在消耗之前")

    def test_pre_v18_schema_order_is_also_unprovable(self):
        """schema 无列 ⇒ ``pre_v18_schema``，不是 legacy_unknown（两者必须可区分）。"""
        prov = PT._order_cycle_provenance_for_order(self.conn, 1)
        self.assertTrue(prov.schema_has_cycle_column, "本夹具是 post-v18 schema")
        # 手工构造 pre-v18 形状：另一条连接、无 cycle_id 列。
        tmp = sqlite3.connect(":memory:")
        tmp.execute("CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, account_id TEXT)")
        tmp.execute("INSERT INTO paper_orders(id,account_id) VALUES(1,'x')")
        stripped = PT._order_cycle_provenance_for_order(tmp, 1)
        tmp.close()
        self.assertFalse(stripped.schema_has_cycle_column)
        self.assertEqual(stripped.status, PT.ORDER_CYCLE_PRE_V18_SCHEMA)
        self.assertNotEqual(stripped.status, PT.ORDER_CYCLE_LEGACY_UNKNOWN,
                            "「schema 没有该列」与「该行的值是 NULL」是两种情形")
        self.assertIsNone(stripped.cycle_id)
        self.assertFalse(stripped.is_proven)


# ══════════════════════════════════════════════════════════════════════════
# §20 / §21 重试血缘硬约束
# ══════════════════════════════════════════════════════════════════════════
class RetryLineageHardConstraint(_LedgerCase):
    """``retry_of_order_id`` 只表达**同一次经济尝试**的重试。

    旧测试只证明「不同值能看出来」，那只是可检测性，不是约束力。这里证明的是
    行为：跨周期时**不建立血缘**（终止旧血缘、创建新的独立尝试），同周期才链接，
    legacy 父行可以链接且父行**永远保持 NULL**。
    """

    def _terminal_parent(self, *, cycle_id, signal_id=9001):
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        self.conn.execute(
            "INSERT INTO paper_signals(id,account_id,code,intended_date,signal_date,"
            "status,reason,payload,created_at,strategy_id,strategy_version,"
            "strategy_checksum) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (signal_id, ACCOUNT, CODE, "2026-09-05", "2026-09-05", "deferred_capacity",
             "种子信号", "{}", "2026-09-05 09:30:00", *stamp),
        )
        cur = self.conn.execute(
            "INSERT INTO paper_orders(account_id,signal_id,side,code,name,qty,status,"
            "reason,risk_payload,created_at,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, signal_id, "buy", CODE, NAME, 100, "superseded", "终态", "{}",
             "2026-09-05 09:31:00", *stamp, cycle_id),
        )
        self.conn.commit()
        return signal_id, int(cur.lastrowid)

    def _resolve(self, signal_id, child_cycle):
        return PT._previous_attempt_order_id(self.conn, signal_id, child_cycle)

    def test_cross_cycle_retry_does_not_inherit_lineage(self):
        """跨周期 ⇒ 终止血缘：不返回父 id（新订单是**独立**尝试）。"""
        signal_id, parent = self._terminal_parent(cycle_id=self.cycle)
        later = self.add_cycle()
        self.assertIsNone(
            self._resolve(signal_id, later),
            "跨周期的重试不是同一次经济尝试，绝不能沿用血缘",
        )

    def test_same_cycle_retry_keeps_lineage(self):
        signal_id, parent = self._terminal_parent(cycle_id=self.cycle)
        self.assertEqual(self._resolve(signal_id, self.cycle), parent)

    def test_legacy_parent_stays_null_and_may_be_linked(self):
        """§21：legacy 父行（cycle NULL）允许被链接，但父行本身永远保持 NULL。"""
        import paper_schema_migrations as PSM
        self.conn.execute("DROP TRIGGER IF EXISTS trg_paper_orders_cycle_provenance_insert")
        try:
            signal_id, parent = self._terminal_parent(cycle_id=None)
        finally:
            PSM._ensure_order_cycle_provenance_guards(self.conn)
        self.conn.commit()

        self.assertEqual(self._resolve(signal_id, self.cycle), parent,
                         "legacy 父行可以被新尝试引用（未知 ≠ 不同周期）")
        self.assertIsNone(self.order_cycle(parent),
                          "引用父行**绝不**意味着把父行解释成属于子订单的周期")

    def test_without_child_cycle_the_legacy_behaviour_is_preserved(self):
        """不传子周期时保持旧语义（调用方兼容），但生产调用点都传。"""
        import paper_schema_migrations as PSM
        self.conn.execute("DROP TRIGGER IF EXISTS trg_paper_orders_cycle_provenance_insert")
        try:
            signal_id, parent = self._terminal_parent(cycle_id=None)
        finally:
            PSM._ensure_order_cycle_provenance_guards(self.conn)
        self.conn.commit()
        self.assertEqual(self._resolve(signal_id, None), parent)


# ══════════════════════════════════════════════════════════════════════════
# §13 真实可达路径 E2E：manual pending_limit → scan → revalidate → commit
# ══════════════════════════════════════════════════════════════════════════
class RealPendingSellPathEndToEnd(_ProvenRiskHarness):
    """走**真实**的 ``manual_orders.process_pending_manual_orders`` 扫描。

    规格 §13 明令不能只测 ``_consume_available_lots`` 原语：真正的生产入口是
    「手动待成交扫描 → 复核 → 成交」。这里让 active cycle 在扫描**之前**切走，
    并断言只有订单自己的周期被消耗。

    夹具复用 ``test_paper_risk_exit_production_path`` 里**已被证明可用**的
    harness（真实的 seed 买单 + fill + lot + 通过校验的新鲜行情）。手写一份
    「看起来像」的行情只会测到闸门，测不到被测行为。
    """

    def _current_cycle(self):
        return int(self.conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id=?",
            (self.account_id,),
        ).fetchone()["cycle_id"])

    def _seed_pending_sell(self):
        self._insert_lot(self.account_id, self.code, 100, 10.0,
                         available_date="2026-09-09")
        cycle = self._current_cycle()
        stamp = PT._strategy_stamp(self.conn, self.account_id)
        cur = self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "status,reason,risk_payload,created_at,origin,strategy_id,"
            "strategy_version,strategy_checksum,cycle_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.account_id, "sell", self.code, f"测试股_{self.code}", 100, 12.0,
             "manual_execution_retry", "seed", "{}", f"{self.day} 10:00:00",
             "manual", *stamp, cycle),
        )
        self.conn.commit()
        return int(cur.lastrowid), cycle

    def _add_later_cycle(self):
        """用**真实**的 ``_create_cycle`` 推进周期（含账号重绑定与 NAV 行）。

        手写一条 ``paper_cycles`` 行会把账号留在旧周期上（active cycle ≠ 账号
        cycle），于是扫描会以「策略账户未运行/无持仓」这类闸门拒绝，测到的是夹具
        而不是被测行为。用生产原语推进，才真正模拟「成交前 active cycle 变了」。
        """
        with PT._db(immediate=True) as conn:
            conn.execute("DELETE FROM paper_nav")  # 夹具：让新周期能写 NAV 行
        with PT._db(immediate=True) as conn:
            cycle = PT._create_cycle(conn, 300000.0, status="running",
                                     reason="测试推进周期")
        return int(cycle["id"])

    def _run_scan(self):
        import manual_orders as MO
        with mock.patch.object(PT, "_quotes", return_value=self.quotes_map), \
                mock.patch.object(PT, "init_db", lambda *a, **k: None), \
                mock.patch.object(PT, "_entry_freeze_enabled", lambda: False):
            return MO.process_pending_manual_orders(asof_date=self.day)

    def _set_account_running(self):
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_accounts SET status='running' WHERE id=?",
                         (self.account_id,))

    def test_scan_fills_from_the_orders_own_cycle_after_active_moved(self):
        order, cycle = self._seed_pending_sell()
        later = self._add_later_cycle()
        self.assertNotEqual(later, cycle)
        self._set_account_running()
        self._set_fresh_exit_quote(self.code, price=12.0, pct=1.0, high=12.2, low=11.8)
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,"
                "qty,remaining_qty,cost,acquired_at,available_date,asset_type,"
                "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (later, self.account_id, self.code, f"测试股_{self.code}", "Tech",
                 100, 100, 10.0, "2026-09-01 10:00:00", "2026-09-02", "stock_t1", 1, 1),
            )
        output = self._run_scan()
        self.assertTrue(output, f"扫描必须处理这条待成交委托：{output}")
        status = self.conn.execute(
            "SELECT status FROM paper_orders WHERE id=?", (order,)
        ).fetchone()["status"]
        self.assertEqual("filled", status, f"真实路径必须成交：{output}")
        own = self.conn.execute(
            "SELECT COALESCE(SUM(remaining_qty),0) FROM paper_position_lots"
            " WHERE cycle_id=? AND account_id=? AND code=?",
            (cycle, self.account_id, self.code),
        ).fetchone()[0]
        other = self.conn.execute(
            "SELECT COALESCE(SUM(remaining_qty),0) FROM paper_position_lots"
            " WHERE cycle_id=? AND account_id=? AND code=?",
            (later, self.account_id, self.code),
        ).fetchone()[0]
        self.assertEqual(int(own), 0, "E2E：必须消耗订单自己周期（cycle 8）的底仓")
        self.assertEqual(int(other), 100, "E2E：绝不能借 active 周期（cycle 9）的底仓")

    def test_scan_cannot_fill_a_legacy_null_cycle_sell(self):
        """legacy NULL-cycle 的 pending SELL：扫描必须不成交、不碰任何周期底仓。"""
        self._insert_lot(self.account_id, self.code, 100, 10.0,
                         available_date="2026-09-09")
        stamp = PT._strategy_stamp(self.conn, self.account_id)
        import paper_schema_migrations as PSM
        self.conn.execute("DROP TRIGGER IF EXISTS trg_paper_orders_cycle_provenance_insert")
        try:
            cur = self.conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                "status,reason,risk_payload,created_at,origin,strategy_id,"
                "strategy_version,strategy_checksum,cycle_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (self.account_id, "sell", self.code, f"测试股_{self.code}", 100, 12.0,
                 "manual_execution_retry", "seed", "{}", f"{self.day} 10:00:00",
                 "manual", *stamp),
            )
        finally:
            PSM._ensure_order_cycle_provenance_guards(self.conn)
        self.conn.commit()
        order = int(cur.lastrowid)
        cycle = self._current_cycle()
        self._set_fresh_exit_quote(self.code, price=12.0, pct=1.0, high=12.2, low=11.8)

        with PT._db() as conn:
            fills_before = conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        output = self._run_scan()

        after = self.conn.execute(
            "SELECT COALESCE(SUM(remaining_qty),0) FROM paper_position_lots"
            " WHERE cycle_id=? AND account_id=? AND code=?",
            (cycle, self.account_id, self.code),
        ).fetchone()[0]
        self.assertEqual(int(after), 100, "E2E：legacy 不得消费任何周期底仓")
        with PT._db() as conn:
            fills_after = conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        self.assertEqual(fills_after, fills_before, f"不得写入 fill：{output}")
        status = self.conn.execute(
            "SELECT status, cycle_id FROM paper_orders WHERE id=?", (order,)
        ).fetchone()
        self.assertNotEqual(status["status"], "filled", "legacy 不得被判为已成交")
        self.assertIsNone(status["cycle_id"], "legacy 订单的 cycle 必须保持 NULL")
        self.assertTrue(output, "扫描仍应报告一个明确的非成交结论")


# ══════════════════════════════════════════════════════════════════════════
# §4/§9/§11 原语级契约（不可由 commit_fill 的外层预检代替）
# ══════════════════════════════════════════════════════════════════════════
class PrimitiveGuardsAreReachableDirectly(_LedgerCase):
    """两个原语**自己**必须 fail closed，不能只靠 ``commit_fill`` 的外层预检。

    ``commit_fill`` 现在先校验归属再调用原语，因此从那条路径**看不到**原语内部的
    守卫。测试若只走 ``commit_fill``，就会在守卫被删掉后依然通过 —— 防御纵深层
    等于没有被验证。这里直接驱动原语。

    （另注：``commit_fill`` 包在事务里，**回滚会把已发生的 mutation 抹掉**，所以
    「事后账本没变」并不能证明「mutation 发生在校验之后」。要证明顺序，必须观察
    副作用函数是否被调用。）
    """

    def _legacy_buy_order(self):
        return self.add_order(side="buy", qty=100, cycle_id=None)

    def test_record_lot_refuses_a_legacy_source_order(self):
        """``_record_lot`` 对 legacy 来源订单必须拒绝，且**不写任何 lot**。"""
        order = self._legacy_buy_order()
        signal = {"code": CODE, "name": NAME, "industry": "测试"}
        before = self.conn.execute(
            "SELECT COUNT(*) FROM paper_position_lots").fetchone()[0]
        with PT._db(immediate=True) as conn:
            with self.assertRaises(PT.OrderCycleProvenanceUnknown) as ctx:
                PT._record_lot(conn, {"id": ACCOUNT}, signal, 100, 10.0,
                               dt.date(2026, 9, 5), order)
        self.assertEqual(ctx.exception.status, PT.ORDER_CYCLE_LEGACY_UNKNOWN)
        after = self.conn.execute(
            "SELECT COUNT(*) FROM paper_position_lots").fetchone()[0]
        self.assertEqual(after, before, "legacy 来源订单不得造出任何 lot")

    def test_record_lot_still_works_for_a_proven_source_order(self):
        """正对照：来源订单归属可证明时原语正常工作（证明上面的拒绝有区分度）。"""
        order = self.add_order(side="buy", qty=100, cycle_id=self.cycle)
        signal = {"code": CODE, "name": NAME, "industry": "测试"}
        with PT._db(immediate=True) as conn:
            PT._record_lot(conn, {"id": ACCOUNT}, signal, 100, 10.0,
                           dt.date(2026, 9, 5), order)
        row = self.conn.execute(
            "SELECT cycle_id FROM paper_position_lots WHERE source_order_id=?",
            (order,)).fetchone()
        self.assertEqual(int(row["cycle_id"]), self.cycle)

    def test_consume_lots_refuses_a_missing_cycle(self):
        """``_consume_available_lots`` 无显式周期时必须拒绝，且**不动任何 lot**。"""
        self.add_lot(self.cycle, 100)
        before = self.lot_remaining(self.cycle)
        with PT._db(immediate=True) as conn:
            with self.assertRaises(PT.OrderCycleProvenanceUnknown):
                PT._consume_available_lots(conn, ACCOUNT, CODE, 100, dt.date(2026, 9, 5))
        self.assertEqual(self.lot_remaining(self.cycle), before,
                         "匿名 lot 消耗不得回退到 active cycle")

    def test_consume_lots_still_works_with_an_explicit_cycle(self):
        """正对照：显式周期下原语正常消耗。"""
        self.add_lot(self.cycle, 100)
        with PT._db(immediate=True) as conn:
            consumed, _cost = PT._consume_available_lots(
                conn, ACCOUNT, CODE, 100, dt.date(2026, 9, 5), cycle_id=self.cycle)
        self.assertEqual(consumed, 100)
        self.assertEqual(self.lot_remaining(self.cycle), 0)


class ProvenancePrecheckHappensBeforeAnyMutation(_LedgerCase):
    """§11：归属校验必须发生在**任何**业务 mutation 之前（观察调用顺序）。

    只断言「事后账本没变」是无效的：``commit_fill`` 在事务内，抛异常会整体回滚，
    于是「先扣款再拒绝」与「先拒绝」在账本上完全一样。要证明顺序，必须观察
    ``_reserve_shared_capital`` / ``_debit_shared_cash`` / ``_record_lot`` /
    ``_consume_available_lots`` / ``_credit_shared_cash`` 有没有被**调用**。
    """

    def _spy_stub(self, calls):
        stub = mock.Mock()
        stub._assert_active_lease = lambda *a, **k: None
        stub._reserve_shared_capital = lambda *a, **k: (calls.append("reserve"), (True, None))[1]
        stub._debit_shared_cash = lambda *a, **k: calls.append("debit")
        stub._finish_capital_reservation = lambda *a, **k: calls.append("reservation")
        stub._record_lot = lambda *a, **k: calls.append("lot")
        stub._consume_available_lots = lambda *a, **k: (calls.append("consume"), (0, 0.0))[1]
        stub._credit_shared_cash = lambda *a, **k: calls.append("credit")
        stub._risk_log = lambda *a, **k: None
        stub._audit = lambda *a, **k: None
        stub._sync_positions = lambda *a, **k: None
        stub._json = lambda value: value
        stub._now = lambda: "2026-09-05 15:00:00"
        stub._date = lambda day: day
        stub._num = lambda value, default=0.0: default if value in (None, "") else float(value)
        # 关键：**不**替身被验证的东西。归属读取与异常类型必须指向真实实现，
        # 否则 mock.Mock 的任意属性都是真值，`provenance.is_proven` 恒真，
        # 预检被静默跳过，测试就变成永绿的空壳。
        stub._order_cycle_provenance_for_order = PT._order_cycle_provenance_for_order
        stub.OrderCycleProvenanceUnknown = PT.OrderCycleProvenanceUnknown
        return stub

    def _drive(self, order, side, reserved):
        calls = []
        with mock.patch.object(EP, "_pt", lambda: self._spy_stub(calls)):
            with PT._db(immediate=True) as conn:
                with self.assertRaises((PT.OrderCycleProvenanceUnknown, RuntimeError)):
                    EP.commit_fill(
                        conn, account={"id": ACCOUNT},
                        plan=self.plan(side=side, qty=100), order_id=order,
                        asof_day=dt.date(2026, 9, 5), reserved=reserved,
                        action="manual_filled", reason="测试成交",
                    )
        return calls

    def test_legacy_buy_is_rejected_before_any_side_effect(self):
        order = self.add_order(side="buy", qty=100, cycle_id=None)
        calls = self._drive(order, "buy", reserved=False)
        self.assertEqual(calls, [], "归属不可证明时不得发生任何业务 mutation")

    def test_legacy_sell_is_rejected_before_any_side_effect(self):
        self.add_lot(self.cycle, 100)
        order = self.add_order(side="sell", qty=100, cycle_id=None)
        calls = self._drive(order, "sell", reserved=True)
        self.assertEqual(calls, [], "归属不可证明时不得发生任何业务 mutation")

    def test_identity_mismatch_is_rejected_before_any_side_effect(self):
        """身份不符同样必须在任何 mutation 之前拒绝（§12）。"""
        order = self.add_order(side="sell", qty=100, cycle_id=self.cycle)
        self.add_lot(self.cycle, 100)
        calls = []
        stub = self._spy_stub(calls)
        plan = self.plan(side="sell", qty=100)
        plan["code"] = "600999"  # 与订单不符
        with mock.patch.object(EP, "_pt", lambda: stub):
            with PT._db(immediate=True) as conn:
                with self.assertRaisesRegex(RuntimeError, "order identity mismatch"):
                    EP.commit_fill(
                        conn, account={"id": ACCOUNT}, plan=plan, order_id=order,
                        asof_day=dt.date(2026, 9, 5), reserved=True,
                        action="manual_filled", reason="测试成交",
                    )
        self.assertEqual(calls, [], "身份不符时不得发生任何业务 mutation")
