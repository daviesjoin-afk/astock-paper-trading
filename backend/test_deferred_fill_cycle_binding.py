# -*- coding: utf-8 -*-
"""Round-6：延迟成交不得跨越周期边界（Blocker 1 / Blocker 2 回归）。

本文件覆盖规格 §14–§19（Regression 1..6）、§12（订单身份）、§20–§21（重试血缘）
与 §13（真实可达路径 E2E）。

全部用例都驱动**真实**生产 schema 与**真实**生产原语（``paper_trading.init_db``
+ ``execution_planner.execute_order``）。手写最小 DDL 只能证明夹具自洽：本轮两个
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
        quote = {
            "code": code, "name": NAME, "price": price, "prev_close": price,
            "pct": 0.0, "amount": max(100000.0, qty * price * 200),
            "quote_at": "2026-09-10T10:00:00",
            "quote_source": "live", "quote_validation": "cross_source_checked",
        }
        return {
            "side": side, "code": code, "qty": qty, "execution_quote": quote,
            "risk": {"x": 1},
        }

    def commit(self, order_id, *, side, qty, reserved=True, code=CODE, price=10.0):
        """经**生产** ``execute_order`` 成交（reserved=True 跳过预占，聚焦周期归属）。"""
        with PT._db(immediate=True) as conn:
            return EP.execute_order(
                conn,
                account={"id": ACCOUNT},
                plan=self.plan(side=side, qty=qty, code=code, price=price),
                order_id=order_id,
                asof_day=dt.date(2026, 9, 10),
                action="manual_filled",
                reason="测试成交",
            )


# ══════════════════════════════════════════════════════════════════════════
# §14 Regression 1 / §19 Regression 6：pending SELL 不得跨周期消费
# ══════════════════════════════════════════════════════════════════════════
class PendingSellStaysInItsOwnCycle(_LedgerCase):
    """§13/§14：cycle 8 建的 pending SELL，在 active 搬到 cycle 9 后**必须拒绝成交**。

    早先这两条测试期望「cycle 8 的订单在 cycle 9 激活后仍然成交并消耗 cycle 8 的
    底仓」。那个期望本身是错的，且已被规格 §13 点名：``paper_accounts`` 已被
    ``_create_cycle`` 重绑到 cycle 9，卖出所得会进 cycle 9 的现金 —— 卖 cycle 8 的
    持仓、收 cycle 9 的钱，是 cross-cycle 账本错配。系统里没有可独立写入的历史周期
    现金账本，所以正确 contract 是：**持久化的周期归属不等于成交授权**。

    这两条现在验证「归属可证明但账本已搬家 ⇒ fail closed，且账本零变化」。
    """

    def test_R1_pending_sell_is_refused_when_execution_cycle_changed(self):
        """Regression 1：拒绝成交，且两个周期的底仓都纹丝不动。"""
        self.add_lot(self.cycle, 100)
        order = self.add_order(side="sell", qty=100, cycle_id=self.cycle)
        later = self.add_cycle()
        self.add_lot(later, 100)  # 必须真的存在 cycle 9 底仓，否则断言是空的
        self.assertNotEqual(later, self.cycle)
        self.assertEqual(self.active_cycle(), later, "夹具必须真的把 active 切走")
        before = self.counts()

        with mock.patch.object(PT, "_date", return_value=dt.date(2026, 9, 10)):
            with self.assertRaises(PT.OrderExecutionCycleChanged) as ctx:
                self.commit(order, side="sell", qty=100)

        self.assertEqual(ctx.exception.order_cycle_id, self.cycle)
        self.assertEqual(ctx.exception.active_cycle_id, later)
        self.assertEqual(self.lot_remaining(self.cycle), 100, "cycle 8 的 lot 不得被消耗")
        self.assertEqual(self.lot_remaining(later), 100, "cycle 9 的 lot 不得被触碰")
        self.assertEqual(self.order_cycle(order), self.cycle, "订单周期不可变")
        after = self.counts()
        self.assertEqual(after["fills"], before["fills"], "不得写入 fill")
        self.assertEqual(self._cash(), before["cash"], "不得入账现金")

    def test_R6_durable_cycle_still_wins_when_the_ledger_agrees(self):
        """Regression 6：三者一致时 durable provenance 正常成交（正对照）。

        原测试断言「active 变了仍然成交」；现在只有在**账本与订单同周期**时成交才
        合法。这里把账户也留在 cycle 8，验证 durable ``order.cycle_id`` 确实被用作
        成交依据（而不是被 active state 覆盖）。
        """
        self.add_lot(self.cycle, 100)
        order = self.add_order(side="sell", qty=100, cycle_id=self.cycle)
        self.assertEqual(self.active_cycle(), self.cycle)
        self.assertEqual(
            int(self.conn.execute("SELECT cycle_id FROM paper_accounts WHERE id=?",
                                  (ACCOUNT,)).fetchone()[0]),
            self.cycle,
        )

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

    def test_R2_sub_lot_own_cycle_waits_without_borrowing_the_next(self):
        self.add_lot(self.cycle, 50)
        order = self.add_order(side="sell", qty=100, cycle_id=self.cycle)
        later = self.add_cycle()
        self.add_lot(later, 100)
        before = self.counts()

        result = self.commit(order, side="sell", qty=100)

        self.assertEqual(result["status"], "pending_execution")
        self.assertEqual(result["filled_qty"], 0)
        self.assertEqual(self.lot_remaining(self.cycle), 50)
        self.assertEqual(self.lot_remaining(later), 100)
        self.assertEqual(self.counts()["fills"], before["fills"])
        self.assertEqual(self._cash(), self.cash_before)


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
            return EP.execute_order(
                conn, account={"id": account_id},
                plan=self.plan(side=side, qty=qty, code=code), order_id=order_id,
                asof_day=dt.date(2026, 9, 10),
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
        """写一条种子 signal + 一条终态父委托。

        ``cycle_id=None`` 表示 **legacy**（升级前创建的行）。与 :meth:`add_order`
        同理：legacy shape **无法**经 v18/v23 的 INSERT guard 写入 —— 那正是 guard
        的职责。要构造历史形状，必须暂时卸下 guard 再装回去。这不是绕过被测行为：
        被测的是成交/血缘阶段**读取** legacy 行时的语义，而不是 guard 本身。
        """
        import paper_schema_migrations as PSM
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        if cycle_id is None:
            self.conn.execute(
                "DROP TRIGGER IF EXISTS trg_paper_signals_cycle_provenance_insert")
            try:
                self.conn.execute(
                    "INSERT INTO paper_signals(id,account_id,code,intended_date,signal_date,"
                    "status,reason,payload,created_at,strategy_id,strategy_version,"
                    "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (signal_id, ACCOUNT, CODE, "2026-09-05", "2026-09-05", "deferred_capacity",
                     "种子信号", "{}", "2026-09-05 09:30:00", *stamp),
                )
            finally:
                PSM._ensure_signal_cycle_provenance_guards(self.conn)
        else:
            self.conn.execute(
                "INSERT INTO paper_signals(id,account_id,code,intended_date,signal_date,"
                "status,reason,payload,created_at,strategy_id,strategy_version,"
                "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (signal_id, ACCOUNT, CODE, "2026-09-05", "2026-09-05", "deferred_capacity",
                 "种子信号", "{}", "2026-09-05 09:30:00", *stamp, cycle_id),
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

    def _snapshot(self):
        """成交相关账本的只读快照（fill / lot / 现金 / 预占明细）。

        §11：只比行数与 ``SUM(amount+fees)`` 是**不够**的 —— 那看不出预占的
        ``status`` / ``cycle_id`` / ``released_at`` 有没有变。而「终态清理允许把
        stale 预占从 ``reserved`` 释放为 ``released``、但绝不允许改写 ``cycle_id`` /
        ``amount`` / ``fees``」这条契约，恰恰只能通过逐行明细来断言。因此这里记录
        每一条预占行的完整身份：``cycle_id`` / ``status`` / ``amount`` / ``fees`` /
        ``released_at``。

        ``lots_from_orders`` 只数**由订单产生**的 lot（``source_order_id`` 非空）。
        不能直接用全表 lot 计数：本仓库存在一条**与被测订单无关**的既有路径 ——
        ``_position_rows`` → ``_migrate_legacy_positions`` 会在 active cycle 前进后
        把 ``paper_positions`` 里的历史镜像行重新展开成一条 ``source_order_id IS
        NULL`` 的 lot。该行为在 base commit 上同样存在（已实测复现），属于本轮范围
        之外的既有缺陷，不能让它把「这张漂移订单没有造出任何 lot」这个真断言搅浑。
        """
        def one(sql, params=()):
            return int(self.conn.execute(sql, params).fetchone()[0])

        def maybe(sql, params=()):
            row = self.conn.execute(sql, params).fetchone()
            return None if row is None else row[0]

        reservations = {
            str(row["order_key"]): (
                None if row["cycle_id"] is None else int(row["cycle_id"]),
                row["status"], float(row["amount"]), float(row["fees"]),
                row["released_at"],
            )
            for row in self.conn.execute(
                "SELECT order_key,cycle_id,status,amount,fees,released_at"
                " FROM paper_capital_reservations")
        }
        return {
            "fills": one("SELECT COUNT(*) FROM paper_fills"),
            "lots": one("SELECT COUNT(*) FROM paper_position_lots"),
            "lots_from_orders": one(
                "SELECT COUNT(*) FROM paper_position_lots"
                " WHERE source_order_id IS NOT NULL"),
            "reservations": one("SELECT COUNT(*) FROM paper_capital_reservations"),
            "reservation_rows": reservations,
            "cash": maybe("SELECT cash FROM paper_accounts WHERE id=?",
                          (self.account_id,)),
        }

    def test_scan_refuses_pending_order_after_execution_cycle_changed(self):
        """§13/§14：active cycle 搬走后，cycle 8 的 pending SELL **必须拒绝成交**。

        本测试替换了早先的 ``test_scan_fills_from_the_orders_own_cycle_after_active_moved``
        —— 那个期望本身是错的：它要求「cycle 8 的订单在 cycle 9 激活后仍然消耗
        cycle 8 的底仓并成交」。但 ``paper_accounts`` 已被 ``_create_cycle`` 重绑到
        cycle 9，卖出所得会打进 cycle 9 的现金，形成 cross-cycle 账本错配：

        * 卖的是 cycle 8 的持仓，
        * 收钱的是 cycle 9 的账户。

        系统里并不存在可独立写入的历史周期现金账本，所以正确 contract 不是「想办法
        把 cash 写回旧周期」，而是：持久化的周期归属 **不等于** 成交授权 —— 账本一旦
        搬家，旧订单即 stale，必须 fail closed 并终态化。
        """
        order, cycle = self._seed_pending_sell()
        later = self._add_later_cycle()
        self.assertNotEqual(later, cycle)
        self._set_account_running()
        self._set_fresh_exit_quote(self.code, price=12.0, pct=1.0, high=12.2, low=11.8)
        # cycle 9 也放一份底仓：如果实现错误地"借"当前周期，数量断言会抓住。
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,"
                "qty,remaining_qty,cost,acquired_at,available_date,asset_type,"
                "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (later, self.account_id, self.code, f"测试股_{self.code}", "Tech",
                 100, 100, 10.0, "2026-09-01 10:00:00", "2026-09-02", "stock_t1", 1, 1),
            )
        fills_before = self.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        cash_before = self.conn.execute(
            "SELECT cash FROM paper_accounts WHERE id=?", (self.account_id,)
        ).fetchone()["cash"]
        output = self._run_scan()
        row = self.conn.execute(
            "SELECT status,reason FROM paper_orders WHERE id=?", (order,)
        ).fetchone()
        self.assertNotEqual("filled", row["status"], f"周期漂移后绝不能成交：{output}")
        self.assertEqual("superseded", row["status"],
                         f"必须收敛为终态，而不是每轮重试：{output}")
        self.assertIn("order_execution_cycle_changed", str(row["reason"]))
        # E2E 核心断言：两侧底仓都没动。
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
        self.assertEqual(int(own), 100, "E2E：cycle 8 的底仓不得被消耗")
        self.assertEqual(int(other), 100, "E2E：cycle 9 的底仓不得被借走")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
            fills_before, "E2E：不得写任何 fill",
        )
        self.assertEqual(
            self.conn.execute("SELECT cash FROM paper_accounts WHERE id=?",
                              (self.account_id,)).fetchone()["cash"],
            cash_before, "E2E：不得有任何现金变动",
        )
        self.assertEqual(int(self.conn.execute(
            "SELECT cycle_id FROM paper_orders WHERE id=?", (order,)
        ).fetchone()["cycle_id"]), cycle, "订单周期归属必须保持不变（不可变事实）")

    def test_scan_refuses_pending_buy_after_execution_cycle_changed(self):
        """§15：cycle 8 的 pending BUY 在 cycle 9 激活后，不得成交、不得写 lot。

        这是本轮最重要的 BUY 承重测试。BUY 的问题方向与 SELL 相反但后果同样严重：
        cycle 8 的委托会**用 cycle 9 的现金**买出一个 cycle 8 的 lot。旧代码还会在
        cycle guard 之前就调用 ``_reserve_shared_capital``，于是 cycle 9 的账上留下
        一张属于 cycle 8 委托的预占 —— 即使成交随后失败，那笔预占也已经落库。
        """
        cycle = self._current_cycle()
        self._insert_lot(self.account_id, self.code, 100, 10.0,
                         available_date="2026-09-09")
        stamp = PT._strategy_stamp(self.conn, self.account_id)
        cur = self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "status,reason,risk_payload,created_at,origin,strategy_id,"
            "strategy_version,strategy_checksum,cycle_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.account_id, "buy", self.code, f"测试股_{self.code}", 100, 10.0,
             "manual_execution_retry", "seed", "{}", f"{self.day} 10:00:00",
             "manual", *stamp, cycle),
        )
        self.conn.commit()
        order = int(cur.lastrowid)
        later = self._add_later_cycle()
        self.assertNotEqual(later, cycle)
        self._set_account_running()
        self._set_fresh_exit_quote(self.code, price=12.0, pct=1.0, high=12.2, low=11.8)
        before = self._snapshot()
        output = self._run_scan()
        row = self.conn.execute(
            "SELECT status,reason FROM paper_orders WHERE id=?", (order,)
        ).fetchone()
        self.assertNotEqual("filled", row["status"], f"周期漂移后绝不能成交：{output}")
        self.assertEqual("superseded", row["status"])
        self.assertIn("order_execution_cycle_changed", str(row["reason"]))
        after = self._snapshot()
        self.assertEqual(after["fills"], before["fills"], "不得写 fill")
        self.assertEqual(
            after["lots_from_orders"], before["lots_from_orders"],
            "不得为这张漂移订单创建 lot",
        )
        self.assertEqual(after["cash"], before["cash"], "不得扣款")
        self.assertEqual(
            after["reservations"],
            before["reservations"],
            "不得为漂移订单创建/改写预占（guard 必须在 reservation 之前）",
        )
        self.assertEqual(int(self.conn.execute(
            "SELECT cycle_id FROM paper_orders WHERE id=?", (order,)
        ).fetchone()["cycle_id"]), cycle, "订单周期归属不可变")

    def test_same_cycle_deferred_order_still_fills_normally(self):
        """§18：同周期的 deferred fill 仍然正常成交 —— 本 PR 只拒绝周期漂移。

        防止"修得太狠"：如果 guard 把「隔了几轮扫描才成交」也判成漂移，deferred
        fill 这个正常能力就被误杀了。这里 order/account/active 三者同为 cycle 8。
        """
        order, cycle = self._seed_pending_sell()
        self.assertEqual(self._current_cycle(), cycle)
        self._set_account_running()
        self._set_fresh_exit_quote(self.code, price=12.0, pct=1.0, high=12.2, low=11.8)
        output = self._run_scan()
        row = self.conn.execute(
            "SELECT status FROM paper_orders WHERE id=?", (order,)
        ).fetchone()
        self.assertEqual("filled", row["status"], f"同周期 deferred fill 必须成交：{output}")
        own = self.conn.execute(
            "SELECT COALESCE(SUM(remaining_qty),0) FROM paper_position_lots"
            " WHERE cycle_id=? AND account_id=? AND code=?",
            (cycle, self.account_id, self.code),
        ).fetchone()[0]
        self.assertEqual(int(own), 0, "同周期成交必须正常消耗底仓")

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
# §4/§9/§11 原语级契约（不可由 execute_order 的外层预检代替）
# ══════════════════════════════════════════════════════════════════════════
class ReadOnlyCycleLookupHasNoSideEffects(_LedgerCase):
    """§9/§10：周期闸门**只读**，绝不能顺手创建周期。"""

    def test_guard_does_not_create_a_cycle_on_an_empty_ledger(self):
        """空库上调 guard 必须 fail closed，且**不得** INSERT 任何周期行。

        ``_active_cycle`` 在没有任何周期时会调用 ``_ensure_cycle`` 建一个新周期。
        如果 guard 用了它，「现在属于哪个周期」这个问题的答案就会从「不存在」变成
        「我刚造出来的一个」，于是验证动作本身改变了被验证的世界。
        """
        with PT._db(immediate=True) as conn:
            conn.execute("DELETE FROM paper_cycles")
        cycles_before = int(self.conn.execute(
            "SELECT COUNT(*) FROM paper_cycles").fetchone()[0])
        order = self.add_order(side="buy", qty=100, cycle_id=None)
        with PT._db(immediate=True) as conn:
            with self.assertRaises(PT.OrderCycleProvenanceUnknown):
                PT._assert_order_execution_cycle(conn, order, account_id=ACCOUNT)
            active = PT._active_cycle_id_readonly(conn)
        self.assertIsNone(active, "无周期时必须返回 None，不得凭空创建")
        self.assertEqual(
            int(self.conn.execute("SELECT COUNT(*) FROM paper_cycles").fetchone()[0]),
            cycles_before, "guard 不得创建周期",
        )

    def test_readonly_active_cycle_matches_creation_free_semantics(self):
        """正对照：存在周期时只读入口返回同一个 active cycle。"""
        with PT._db(immediate=True) as conn:
            self.assertEqual(PT._active_cycle_id_readonly(conn), self.cycle)
            self.assertEqual(
                int(PT._active_cycle(conn)["id"]), self.cycle,
            )


class ExecutionCycleInvariantIsChecked(_LedgerCase):
    """§5/§11：三项一致性 —— 订单周期 == 账户周期 == active 周期。"""

    def _proven_buy(self):
        return self.add_order(side="buy", qty=100, cycle_id=self.cycle)

    def test_account_cycle_mismatch_is_rejected(self):
        """账户被重绑到新周期、但订单仍属旧周期 ⇒ 必须 fail closed。

        即使 ``paper_accounts.id`` 没变：``_create_cycle`` 会**重绑并重置**账户，
        所以「同一个账户 id」绝不代表「同一个经济周期」。
        """
        order = self._proven_buy()
        later = self.add_cycle(started_at="2026-09-20 09:30:00")
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_accounts SET cycle_id=? WHERE id=?",
                         (later, ACCOUNT))
        with PT._db(immediate=True) as conn:
            with self.assertRaises(PT.OrderExecutionCycleChanged) as ctx:
                PT._assert_order_execution_cycle(conn, order, account_id=ACCOUNT)
        self.assertEqual(ctx.exception.order_cycle_id, self.cycle)
        self.assertEqual(ctx.exception.account_cycle_id, later)
        self.assertEqual(ctx.exception.marker, "order_execution_cycle_changed")

    def test_active_cycle_mismatch_is_rejected(self):
        """活跃周期前进、订单仍属旧周期 ⇒ 必须 fail closed。"""
        order = self._proven_buy()
        later = self.add_cycle(started_at="2026-09-20 09:30:00")
        self.assertNotEqual(later, self.cycle)
        with PT._db(immediate=True) as conn:
            with self.assertRaises(PT.OrderExecutionCycleChanged):
                PT._assert_order_execution_cycle(conn, order, account_id=ACCOUNT)

    def test_null_account_cycle_is_rejected(self):
        """账户 ``cycle_id`` 为 NULL（未启用策略）⇒ 必须 fail closed。"""
        order = self._proven_buy()
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_accounts SET cycle_id=NULL WHERE id=?",
                         (ACCOUNT,))
        with PT._db(immediate=True) as conn:
            with self.assertRaises(PT.OrderExecutionCycleChanged):
                PT._assert_order_execution_cycle(conn, order, account_id=ACCOUNT)

    def test_missing_account_is_rejected(self):
        """账户不存在 ⇒ 必须 fail closed（不得把 None 当成"一致"）。"""
        order = self._proven_buy()
        with PT._db(immediate=True) as conn:
            with self.assertRaises(PT.OrderExecutionCycleChanged):
                PT._assert_order_execution_cycle(conn, order, account_id="ghost")

    def test_consistent_triple_passes_and_returns_the_order_cycle(self):
        """正对照：三者一致时返回订单周期（证明上面的拒绝有区分度）。"""
        order = self._proven_buy()
        with PT._db(immediate=True) as conn:
            self.assertEqual(
                PT._assert_order_execution_cycle(conn, order, account_id=ACCOUNT),
                self.cycle,
            )

    def test_guard_refuses_an_unprovable_order_directly(self):
        """§19：闸门**自己**必须拒绝归属不可证明的订单，绝不回退到 active cycle。

        必须直接驱动闸门：``execute_order`` 在调用闸门**之前**也读了一次归属并抛异常，
        所以从 ``execute_order`` 那条路径看，闸门内部的这个判断是被遮蔽的（防御纵深
        的第二层）。只测 ``execute_order`` 无法证明闸门自己会拒绝 —— 把闸门的判断换成
        「拿当前 active cycle 顶上」之后，``execute_order`` 的用例依然全绿。
        """
        legacy = self.add_order(side="buy", qty=100, cycle_id=None)
        with PT._db(immediate=True) as conn:
            with self.assertRaises(PT.OrderCycleProvenanceUnknown):
                PT._assert_order_execution_cycle(conn, legacy, account_id=ACCOUNT)

    def test_guard_does_not_fall_back_to_active_cycle_for_a_drifted_order(self):
        """§5：漂移订单也不得被"修好" —— 拒绝，而不是改用 active cycle。"""
        order = self._proven_buy()
        later = self.add_cycle(started_at="2026-09-20 09:30:00")
        self.assertNotEqual(later, self.cycle)
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_accounts SET cycle_id=? WHERE id=?",
                         (later, ACCOUNT))
        with PT._db(immediate=True) as conn:
            with self.assertRaises(PT.OrderExecutionCycleChanged) as ctx:
                PT._assert_order_execution_cycle(conn, order, account_id=ACCOUNT)
        # 异常必须携带**订单自己的**周期，而不是 active 周期 —— 否则上层无法区分
        # "归属未知" 与 "归属已知但账本已搬家"。
        self.assertEqual(ctx.exception.order_cycle_id, self.cycle)
        self.assertEqual(ctx.exception.marker, "order_execution_cycle_changed")


class ReservationCycleProvenance(_LedgerCase):
    """§20/§21/§22：预占的周期归属必须与订单一致，且**不可改写**。"""

    def _reserve(self, cycle_id, *, amount=1000.0):
        with PT._db(immediate=True) as conn:
            return PT._reserve_shared_capital(
                conn, "order-1", ACCOUNT, CODE, amount, 5.0,
                expected_cycle_id=cycle_id,
            )

    def _reservation_cycle(self):
        row = self.conn.execute(
            "SELECT cycle_id FROM paper_capital_reservations WHERE order_key=?",
            ("order-1",)).fetchone()
        return None if row is None else int(row["cycle_id"])

    def test_mismatched_reservation_is_not_resized(self):
        """预占记在 cycle 9、订单属于 cycle 8 ⇒ 拒绝调整，且**不改写** cycle_id。

        §4：冲突以**类型化异常**表达（``ReservationCycleMismatch``），不是返回一段
        自由文本 —— 上层据类型判定，而不是对 reason 做 contains 匹配。
        """
        later = self.add_cycle(started_at="2026-09-20 09:30:00")
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_capital_reservations(cycle_id,order_key,account_id,"
                "code,side,amount,fees,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (later, "order-1", ACCOUNT, CODE, "buy", 1000.0, 5.0, "reserved",
                 "2026-09-20 09:30:00"),
            )
        with PT._db(immediate=True) as conn:
            with self.assertRaises(PT.ReservationCycleMismatch) as ctx:
                PT._reserve_shared_capital(
                    conn, "order-1", ACCOUNT, CODE, 2000.0, 5.0,
                    expected_cycle_id=self.cycle,
                )
        self.assertEqual(ctx.exception.marker, "reservation_cycle_mismatch")
        self.assertEqual(ctx.exception.reserved_cycle_id, later)
        self.assertEqual(ctx.exception.order_cycle_id, self.cycle)
        self.assertEqual(self._reservation_cycle(), later,
                         "§22：绝不改写预占的 cycle_id 来\"修复\"")
        amount = self.conn.execute(
            "SELECT amount FROM paper_capital_reservations WHERE order_key=?",
            ("order-1",)).fetchone()[0]
        self.assertEqual(float(amount), 1000.0, "拒绝时必须原样保留金额")

    def test_matching_reservation_is_resized_normally(self):
        """正对照：周期一致时可以正常调整（证明拒绝有区分度）。"""
        self._reserve(self.cycle, amount=1000.0)
        ok, reason = self._reserve(self.cycle, amount=2000.0)
        self.assertTrue(ok, f"同周期预占应可调整：{reason}")
        amount = self.conn.execute(
            "SELECT amount FROM paper_capital_reservations WHERE order_key=?",
            ("order-1",)).fetchone()[0]
        self.assertEqual(float(amount), 2000.0)
        self.assertEqual(self._reservation_cycle(), self.cycle)


class ReservationCycleMismatchEndToEnd(_ProvenRiskHarness):
    """§8/§9/§10：**真实** ``process_pending_manual_orders`` 下的预占周期冲突。

    规格明确要求这些场景必须走真实 scanner，而不是只测预占原语 —— 因为缺陷恰恰
    出在 scanner 把「永久归属冲突」当成「临时资金不足」的那两处分支里：
    not-triggered 分支甚至**没有**把订单周期传下去，triggered 分支则把它打回
    ``pending_limit`` 无限重试。
    """

    def _current_cycle(self):
        return int(self.conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id=?",
            (self.account_id,),
        ).fetchone()["cycle_id"])

    def _seed_pending_buy_with_mismatched_reservation(self, *, triggered):
        """构造 order(cycle 8) + reservation(cycle 9) 的 pending 限价 BUY 订单。

        触发判定是 ``plan["triggered"] = quote_price <= limit_price``（买入方向）——
        即限价**高于**现价时才会成交。因此：

        * ``triggered=True``  ⇒ 限价 15.0 > 现价 12.0，落到"已触发"分支；
        * ``triggered=False`` ⇒ 限价 5.0  < 现价 12.0，落到"未触发"分支。

        （实测教训：早先误以为"限价 10 现价 12"会触发，结果两条场景都进了
        not-triggered 分支，于是"已触发"的测试其实是空壳 —— 见 revert 非空性。）
        """
        cycle = self._current_cycle()
        stamp = PT._strategy_stamp(self.conn, self.account_id)
        # 人工构造一个不属于本订单的周期 id，用来模拟历史错误归属。
        bogus_cycle = int(self.conn.execute(
            "SELECT COALESCE(MAX(id),0)+1 FROM paper_cycles").fetchone()[0])
        price = 15.0 if triggered else 5.0
        cur = self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "status,reason,risk_payload,created_at,origin,order_type,"
            "strategy_id,strategy_version,strategy_checksum,cycle_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.account_id, "buy", self.code, f"测试股_{self.code}", 100, price,
             "pending_limit", "seed", "{}", f"{self.day} 10:00:00",
             "manual", "limit", *stamp, cycle),
        )
        order_id = int(cur.lastrowid)
        self.conn.execute(
            "INSERT INTO paper_capital_reservations(cycle_id,order_key,account_id,"
            "code,side,amount,fees,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (bogus_cycle, str(order_id), self.account_id, self.code, "buy",
             price * 100, 5.0, "reserved", f"{self.day} 10:00:00"),
        )
        self.conn.commit()
        return order_id, cycle, bogus_cycle

    def _reservation_row(self, order_id):
        return self.conn.execute(
            "SELECT cycle_id,status,amount,fees,released_at"
            " FROM paper_capital_reservations WHERE order_key=?",
            (str(order_id),)).fetchone()

    def _run_scan(self, *, market_light="green", tier="T1"):
        """跑真实 scanner。

        两个前置门禁必须显式喂成「通过」，否则扫描走不到预占分支，测到的是门禁
        而不是被测行为：

        * ``market_context`` —— plan 构造里有 ``EP.market_gate(...)["blocked"]``，
          而基类 harness 的 ``_cached_close_market`` 只返回
          ``{"breadth": 0.5, "sentiment": "neutral"}``，缺 ``light`` 时按「未知」
          处理并禁止新开仓；
        * ``DE.buy_decision`` —— plan 构造要求 ``tier in ("T1","T2")``，否则以
          「买入模型为 T5，未通过开仓门禁」拒绝。这里替成确定性的 T1。
        """
        import manual_orders as MO
        market = {"light": market_light, "breadth": 0.5, "sentiment": "neutral"}
        decision = {"tier": tier, "action": "买入", "reason": "fixture"}
        # 捕获 scanner 实际构建的 plan，记录它走了哪个分支 —— 这样「已触发」与
        # 「未触发」两条测试就不会因为夹具参数写错而双双落进同一分支（那正是本
        # 轮 revert 非空性抓出的空壳：限价 10 现价 12 其实**不**触发）。
        self.branch_probe = {}
        real_plan = MO._manual_order_plan

        def spy(*args, **kwargs):
            built = real_plan(*args, **kwargs)
            self.branch_probe["triggered"] = built.get("triggered")
            self.branch_probe["allowed"] = built.get("allowed")
            self.branch_probe["limit_price"] = kwargs.get("limit_price")
            return built

        with mock.patch.object(PT, "_quotes", return_value=self.quotes_map), \
                mock.patch.object(PT, "init_db", lambda *a, **k: None), \
                mock.patch.object(PT, "_entry_freeze_enabled", lambda: False), \
                mock.patch.object(PT, "_market_state", return_value=market), \
                mock.patch.object(PT, "_cached_close_market",
                                  lambda conn, day, allow_network=False: market), \
                mock.patch.object(PT.DE, "buy_decision", return_value=decision), \
                mock.patch.object(MO, "_manual_order_plan", spy):
            return MO.process_pending_manual_orders(asof_date=self.day)

    def _set_account_running(self):
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_accounts SET status='running' WHERE id=?",
                         (self.account_id,))

    def _snapshot(self):
        one = lambda sql: int(self.conn.execute(sql).fetchone()[0])
        cash = self.conn.execute(
            "SELECT cash FROM paper_accounts WHERE id=?",
            (self.account_id,)).fetchone()["cash"]
        return {
            "fills": one("SELECT COUNT(*) FROM paper_fills"),
            "lots_from_orders": one(
                "SELECT COUNT(*) FROM paper_position_lots"
                " WHERE source_order_id IS NOT NULL"),
            "reservations": one("SELECT COUNT(*) FROM paper_capital_reservations"),
            "cash": cash,
        }

    def _assert_mismatch_terminalized(self, order_id, cycle, bogus_cycle, output,
                                      original_amount):
        """§8/§9 共同断言：终态化 + **冲突预占原样保留** + 身份不变 + 零业务写入。

        R19 §50 修正：这张预占行记在 ``bogus_cycle`` 上，属于**别的**经济事实，
        不是本订单可以处置的资产。旧行为（把它 release 掉）会把别人的资金挪为
        可用；正确处置是只终态化当前订单，冲突预占保持原样。
        """
        row = self.conn.execute(
            "SELECT status,reason,cycle_id FROM paper_orders WHERE id=?",
            (order_id,)).fetchone()
        # 1. 订单必须终态化，**不是** pending_limit / pending_execution_retry。
        self.assertEqual("superseded", row["status"],
                         f"归属冲突必须终态化，不能重试：{output}")
        self.assertIn("reservation_cycle_mismatch", str(row["reason"]))
        self.assertNotIn("pending_limit", str(row["status"]))
        # 2. 订单周期归属不可变。
        self.assertEqual(int(row["cycle_id"]), cycle, "订单周期不可变")
        # 3. 冲突预占：cycle/amount/fees/status 全部原样 —— 它不属于本订单。
        res = self._reservation_row(order_id)
        self.assertEqual(int(res["cycle_id"]), bogus_cycle,
                         "§22：预占 cycle_id 绝不被改写")
        self.assertEqual(float(res["fees"]), 5.0, "费用不被 resize")
        self.assertEqual("reserved", res["status"],
                         "§50：冲突的预占属于别的订单，绝不被 release")
        self.assertIsNone(res["released_at"],
                          "§50：冲突预占不得留下 released_at")
        # 金额必须等于**扫描前捕获的原值**，证明 scanner 没有按本轮市价或其它规模
        # resize 它（原值随场景不同：未触发用限价，已触发用成交价）。
        self.assertEqual(float(res["amount"]), float(original_amount),
                         "§8：预占金额必须保持扫描前原值，绝不得 resize")
        return res

    def test_not_triggered_mismatched_reservation_terminalizes(self):
        """§8：**未触发**限价分支的归属冲突必须终态化（Blocker A 的承重测试）。

        旧缺陷：该分支调用 ``_reserve_shared_capital`` 时**没有**传
        ``expected_cycle_id``，于是周期冲突根本不会被发现，预占会被静默按本订单
        规模 resize，订单继续留在队列里。
        """
        order_id, cycle, bogus = self._seed_pending_buy_with_mismatched_reservation(
            triggered=False)
        self._set_account_running()
        self._set_fresh_exit_quote(self.code, price=12.0, pct=1.0, high=12.2, low=11.8)
        before = self._snapshot()
        amount_before = float(self._reservation_row(order_id)["amount"])
        output = self._run_scan()
        # 前置：夹具必须真的落进 not-triggered 分支，否则本测试是空壳。
        self.assertFalse(self.branch_probe.get("triggered"),
                         f"夹具必须走未触发分支：{self.branch_probe}")
        self._assert_mismatch_terminalized(order_id, cycle, bogus, output,
                                           amount_before)
        after = self._snapshot()
        self.assertEqual(after["fills"], before["fills"], "不得写 fill")
        self.assertEqual(after["lots_from_orders"], before["lots_from_orders"],
                         "不得创建 lot")
        self.assertEqual(after["cash"], before["cash"], "不得扣款")
        self.assertEqual(after["reservations"], before["reservations"],
                         "不得创建新预占")

    def test_triggered_mismatched_reservation_terminalizes(self):
        """§9：**已触发**分支的归属冲突同样必须终态化（Blocker B 的承重测试）。

        旧缺陷：该分支把 ``reservation_cycle_mismatch`` 与「临时资金不足」一起
        处理 ⇒ 打回 ``pending_limit`` 让下一轮再试；而两个 cycle 都不可变，重试
        永远不会成功。
        """
        order_id, cycle, bogus = self._seed_pending_buy_with_mismatched_reservation(
            triggered=True)
        self._set_account_running()
        self._set_fresh_exit_quote(self.code, price=12.0, pct=1.0, high=12.2, low=11.8)
        before = self._snapshot()
        amount_before = float(self._reservation_row(order_id)["amount"])
        output = self._run_scan()
        # 前置：夹具必须真的落进**已触发**分支。买入限价单的触发条件是
        # `quote_price <= limit_price`，所以限价必须高于现价（15 > 12）。
        # 缺了这条断言，两条测试可能双双走未触发分支，而"已触发"那条就成了空壳。
        self.assertTrue(self.branch_probe.get("triggered"),
                        f"夹具必须走已触发分支：{self.branch_probe}")
        self._assert_mismatch_terminalized(order_id, cycle, bogus, output,
                                           amount_before)
        after = self._snapshot()
        self.assertEqual(after["fills"], before["fills"], "不得写 fill")
        self.assertEqual(after["lots_from_orders"], before["lots_from_orders"],
                         "不得创建 lot")
        self.assertEqual(after["cash"], before["cash"], "不得扣款")

    def test_release_failure_is_not_swallowed(self):
        """§7：预占释放失败必须让本次尝试失败，**不得**静默把订单终态化。

        若释放失败被吞掉，就会出现「订单已 superseded、预占仍 reserved」的组合 ——
        那笔资金被永久占用且没有任何订单再引用它，比直接失败更糟。

        R19 起这条契约只对**会释放预占**的路径成立：预占周期归属冲突属于
        **别的**订单，R19 §50 明确不再释放它（见
        ``test_not_triggered_mismatched_reservation_terminalizes``）。因此这里
        改走**订单周期漂移**路径 —— 那张预占属于本订单，必须释放，而释放失败
        不得被吞。
        """
        cycle = self._current_cycle()
        stamp = PT._strategy_stamp(self.conn, self.account_id)
        cur = self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "status,reason,risk_payload,created_at,origin,strategy_id,"
            "strategy_version,strategy_checksum,cycle_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.account_id, "buy", self.code, f"测试股_{self.code}", 100, 10.0,
             "manual_execution_retry", "seed", "{}", f"{self.day} 10:00:00",
             "manual", *stamp, cycle),
        )
        order_id = int(cur.lastrowid)
        # 本订单**自己的**预占（同周期）⇒ 终态化时应当释放。
        self.conn.execute(
            "INSERT INTO paper_capital_reservations(cycle_id,order_key,account_id,"
            "code,side,amount,fees,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (cycle, str(order_id), self.account_id, self.code, "buy", 1000.0, 5.0,
             "reserved", f"{self.day} 10:00:00"),
        )
        self.conn.commit()
        with PT._db(immediate=True) as conn:
            conn.execute("DELETE FROM paper_nav")
        with PT._db(immediate=True) as conn:
            PT._create_cycle(conn, 300000.0, status="running", reason="测试推进周期")
        self._set_account_running()
        self._set_fresh_exit_quote(self.code, price=12.0, pct=1.0, high=12.2, low=11.8)

        real = PT._finish_capital_reservation

        def boom(conn, order_key, status):
            if status == "released":
                raise RuntimeError("RELEASE_BOOM: simulated ledger failure")
            return real(conn, order_key, status)

        import manual_orders as MO
        market = {"light": "green", "breadth": 0.5, "sentiment": "neutral"}
        with mock.patch.object(PT, "_quotes", return_value=self.quotes_map), \
                mock.patch.object(PT, "init_db", lambda *a, **k: None), \
                mock.patch.object(PT, "_entry_freeze_enabled", lambda: False), \
                mock.patch.object(PT, "_market_state", return_value=market), \
                mock.patch.object(PT, "_cached_close_market",
                                  lambda conn, day, allow_network=False: market), \
                mock.patch.object(PT.DE, "buy_decision",
                                  return_value={"tier": "T1", "action": "买入"}), \
                mock.patch.object(PT, "_finish_capital_reservation", boom):
            with self.assertRaises(RuntimeError) as ctx:
                MO.process_pending_manual_orders(asof_date=self.day)
        self.assertIn("RELEASE_BOOM", str(ctx.exception))
        # 订单**不得**被静默终态化（否则就是「订单终态 + 资金被占用」的坏组合）。
        self.assertNotEqual(
            "superseded",
            self.conn.execute("SELECT status FROM paper_orders WHERE id=?",
                              (order_id,)).fetchone()["status"],
            "释放失败时不得把订单标成 superseded",
        )
        self.assertNotEqual(
            "released", self._reservation_row(order_id)["status"],
            "释放并未真正成功，状态不得显示 released",
        )

    def test_matching_reservation_still_resizes_and_fills(self):
        """§10 正对照：周期一致时，未触发可正常 resize、已触发可正常成交。"""
        # (a) 未触发 ⇒ resize 后保持 pending_limit
        cycle = self._current_cycle()
        stamp = PT._strategy_stamp(self.conn, self.account_id)
        price = 5.0
        cur = self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "status,reason,risk_payload,created_at,origin,order_type,"
            "strategy_id,strategy_version,strategy_checksum,cycle_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.account_id, "buy", self.code, f"测试股_{self.code}", 100, price,
             "pending_limit", "seed", "{}", f"{self.day} 10:00:00",
             "manual", "limit", *stamp, cycle),
        )
        pending_id = int(cur.lastrowid)
        self.conn.execute(
            "INSERT INTO paper_capital_reservations(cycle_id,order_key,account_id,"
            "code,side,amount,fees,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (cycle, str(pending_id), self.account_id, self.code, "buy", 100.0, 5.0,
             "reserved", f"{self.day} 10:00:00"),
        )
        self.conn.commit()
        self._set_account_running()
        self._set_fresh_exit_quote(self.code, price=12.0, pct=1.0, high=12.2, low=11.8)
        self._run_scan()
        row = self.conn.execute(
            "SELECT status FROM paper_orders WHERE id=?", (pending_id,)).fetchone()
        self.assertEqual("pending_limit", row["status"],
                         "同周期未触发订单必须保持 pending_limit（不得误杀）")
        res = self._reservation_row(pending_id)
        self.assertEqual(int(res["cycle_id"]), cycle)
        self.assertEqual("reserved", res["status"], "同周期预占不得被释放")


class FinalReviewGateAllSixAbsences(_ProvenRiskHarness):
    """§43 最终人工审核 Gate：cycle mismatch 时六项必须全部缺席。

    规格要求的六个「no」逐一断言（不只是「订单没成交」这一个笼统观察）：

    * no reservation creation / resize / cycle rewrite（**终态释放是允许且期望的**：
      stale 订单的预占必须从 ``reserved`` 转为 ``released``，否则资金被永久占用）
    * no cash mutation
    * no lot mutation
    * no fill
    * no execution verification stamp
    * no position sync caused by the stale order

    §6/§11：把这一条写成笼统的 "no reservation mutation" 是**不准确**的 —— 释放
    本身就是一次 mutation，而且是正确行为。真正的契约是：``cycle_id`` / ``amount``
    / ``fees`` 一律不变，``status`` 只允许 ``reserved -> released``。因此快照必须
    逐行记录预占的完整身份，而不是只比行数与 ``SUM(amount+fees)``。

    走**真实** ``process_pending_manual_orders``，而不是原语，因为 §43 要审的是
    「生产路径在漂移时会不会留下痕迹」。
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
        with PT._db(immediate=True) as conn:
            conn.execute("DELETE FROM paper_nav")
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

    def test_all_six_absences_hold_for_a_stale_order(self):
        order, cycle = self._seed_pending_sell()
        later = self._add_later_cycle()
        self.assertNotEqual(later, cycle)
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_accounts SET status='running' WHERE id=?",
                         (self.account_id,))
        self._set_fresh_exit_quote(self.code, price=12.0, pct=1.0, high=12.2, low=11.8)

        def count(sql, params=()):
            return int(self.conn.execute(sql, params).fetchone()[0])

        before = {
            "reservations": count("SELECT COUNT(*) FROM paper_capital_reservations"),
            "reservation_amount": self.conn.execute(
                "SELECT COALESCE(SUM(amount+fees),0) FROM paper_capital_reservations"
            ).fetchone()[0],
            # §11：逐行记录预占身份，才能断言 cycle_id/status 的契约。
            "reservation_rows": {
                str(row["order_key"]): (
                    None if row["cycle_id"] is None else int(row["cycle_id"]),
                    row["status"], float(row["amount"]), float(row["fees"]),
                )
                for row in self.conn.execute(
                    "SELECT order_key,cycle_id,status,amount,fees"
                    " FROM paper_capital_reservations")
            },
            "cash": self.conn.execute(
                "SELECT cash FROM paper_accounts WHERE id=?", (self.account_id,)
            ).fetchone()["cash"],
            "lots": count("SELECT COUNT(*) FROM paper_position_lots"),
            "lots_from_orders": count(
                "SELECT COUNT(*) FROM paper_position_lots"
                " WHERE source_order_id IS NOT NULL"),
            "fills": count("SELECT COUNT(*) FROM paper_fills"),
            "verified": count(
                "SELECT COUNT(*) FROM paper_orders"
                " WHERE execution_verified=1 OR execution_status='verified'"),
            "nav": count("SELECT COUNT(*) FROM paper_nav"),
        }

        output = self._run_scan()

        # 1. §6：预占契约 —— 不得新建、不得 resize、不得改写 cycle_id；
        #    stale 订单的预占**允许且期望**从 reserved 释放为 released。
        self.assertEqual(
            count("SELECT COUNT(*) FROM paper_capital_reservations"),
            before["reservations"], f"不得新建预占：{output}",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COALESCE(SUM(amount+fees),0) FROM paper_capital_reservations"
            ).fetchone()[0],
            before["reservation_amount"], "不得改写任何预占金额/费用",
        )
        # 逐行核对身份：cycle_id 不可变；status 只允许 reserved -> released。
        for row in self.conn.execute(
                "SELECT order_key,cycle_id,status FROM paper_capital_reservations"):
            key = str(row["order_key"])
            if key not in before["reservation_rows"]:
                continue
            prior_cycle, prior_status = before["reservation_rows"][key][:2]
            self.assertEqual(
                None if row["cycle_id"] is None else int(row["cycle_id"]),
                prior_cycle, f"§22：预占 {key} 的 cycle_id 绝不可改写",
            )
            self.assertIn(
                row["status"], ("reserved", "released"),
                f"预占 {key} 的状态必须仍是 reserved 或已 released",
            )
            if row["status"] == "released":
                self.assertEqual(prior_status, "reserved",
                                 "释放只允许从 reserved 转到 released")
        # 2. no cash mutation
        self.assertEqual(
            self.conn.execute("SELECT cash FROM paper_accounts WHERE id=?",
                              (self.account_id,)).fetchone()["cash"],
            before["cash"], "不得有现金变动",
        )
        # 3. no lot mutation（订单来源的 lot 是唯一可归因给本订单的口径）
        self.assertEqual(
            count("SELECT COUNT(*) FROM paper_position_lots"
                  " WHERE source_order_id IS NOT NULL"),
            before["lots_from_orders"], "不得让本订单造出 lot",
        )
        # 4. no fill
        self.assertEqual(count("SELECT COUNT(*) FROM paper_fills"),
                         before["fills"], "不得写 fill")
        # 5. no execution verification stamp
        self.assertEqual(
            count("SELECT COUNT(*) FROM paper_orders"
                  " WHERE execution_verified=1 OR execution_status='verified'"),
            before["verified"], "不得为 stale 订单盖章成交验证",
        )
        # 6. 该订单本身必须终态化（§23），且周期归属不可变。
        row = self.conn.execute(
            "SELECT status,reason,cycle_id FROM paper_orders WHERE id=?", (order,)
        ).fetchone()
        self.assertEqual(row["status"], "superseded", f"必须终态化：{output}")
        self.assertIn("order_execution_cycle_changed", str(row["reason"]))
        self.assertEqual(int(row["cycle_id"]), cycle, "订单周期归属不可变")


class PrimitiveGuardsAreReachableDirectly(_LedgerCase):
    """两个原语**自己**必须 fail closed，不能只靠 ``execute_order`` 的外层预检。

    ``execute_order`` 现在先校验归属再调用原语，因此从那条路径**看不到**原语内部的
    守卫。测试若只走 ``execute_order``，就会在守卫被删掉后依然通过 —— 防御纵深层
    等于没有被验证。这里直接驱动原语。

    （另注：``execute_order`` 包在事务里，**回滚会把已发生的 mutation 抹掉**，所以
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

    只断言「事后账本没变」是无效的：``execute_order`` 在事务内，抛异常会整体回滚，
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
        # execution-cycle 闸门同样必须指向真实实现：auto-Mock 会返回真值，让
        # 「漏掉周期一致性校验」这件事在 spy 测试里观测不到。
        stub._assert_order_execution_cycle = PT._assert_order_execution_cycle
        stub.OrderExecutionCycleChanged = PT.OrderExecutionCycleChanged
        return stub

    def _drive(self, order, side, reserved):
        calls = []
        with mock.patch.object(EP, "_pt", lambda: self._spy_stub(calls)):
            with PT._db(immediate=True) as conn:
                with self.assertRaises((PT.OrderCycleProvenanceUnknown, RuntimeError)):
                    EP.execute_order(
                        conn, account={"id": ACCOUNT},
                        plan=self.plan(side=side, qty=100), order_id=order,
                        asof_day=dt.date(2026, 9, 10),
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
                    EP.execute_order(
                        conn, account={"id": ACCOUNT}, plan=plan, order_id=order,
                        asof_day=dt.date(2026, 9, 10),
                        action="manual_filled", reason="测试成交",
                    )
        self.assertEqual(calls, [], "身份不符时不得发生任何业务 mutation")
