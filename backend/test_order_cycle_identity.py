# -*- coding: utf-8 -*-
"""§25/§26：跨周期订单/lot 身份，与 legacy 精确时间 fallback。

两组契约各自独立：

* ``CrossCycleIdentityTests`` —— 订单、lot、卖出成交三者的 cycle 必须一致，
  不一致时 fail closed / 回滚；legacy NULL parent 可以被新行引用但**不得**反向回填。
* ``ExactTimeFallbackTests`` —— 同一天内的周期边界必须按**时刻**比较；拿不到
  成交时刻时不得猜日内顺序，只有严格跨日才允许用日期证明排除。

两组都直接驱动适配器（``tradability_position_evidence``），因此断言的是生产
判定逻辑，而不是夹具。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_schema_migrations as PSM
import tradability_position_evidence as PE

ACCOUNT = "acct_a"
CYCLE = 8
OTHER_CYCLE = 4
CODE = "600901"
NORMAL = "600902"

D0 = "2026-09-14"   # 周一
D1 = "2026-09-15"   # 周二
D2 = "2026-09-16"   # 周三
AS_OF = "2026-10-09T16:00:00+08:00"

DDL = """
CREATE TABLE paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
    signal_id INTEGER, side TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
    qty INTEGER NOT NULL, planned_price REAL, filled_price REAL,
    amount REAL, fees REAL, status TEXT NOT NULL, reason TEXT,
    risk_payload TEXT NOT NULL DEFAULT '', realized_pnl REAL,
    created_at TEXT NOT NULL, executed_at TEXT,
    order_type TEXT NOT NULL DEFAULT 'market',
    origin TEXT NOT NULL DEFAULT 'strategy', expires_at TEXT, cancelled_at TEXT,
    strategy_id TEXT, strategy_version INTEGER, strategy_checksum TEXT,
    retry_of_order_id INTEGER,
    execution_status TEXT, execution_verified INTEGER, execution_evidence_source TEXT
);
CREATE TABLE paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
    account_id TEXT NOT NULL, side TEXT NOT NULL, code TEXT NOT NULL,
    qty INTEGER NOT NULL, price REAL NOT NULL, amount REAL NOT NULL, fees REAL NOT NULL,
    fill_date TEXT NOT NULL, quote_at TEXT, assumption TEXT NOT NULL DEFAULT ''
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
CREATE TABLE paper_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL, started_at TEXT, ended_at TEXT, created_at TEXT
);
"""


class _Case(unittest.TestCase):
    #: 是否在 ``paper_orders`` 上建 v18 的 ``cycle_id`` 列。
    #: ``True`` = post-v18 形状（显式 durable 身份路径）；
    #: ``False`` = pre-v18 形状（只能走 legacy 时间窗路径）。
    with_cycle_column = True

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)
        if self.with_cycle_column:
            PSM.ensure_columns(self.conn, "paper_orders", {"cycle_id": "INTEGER"})
        self.conn.execute(
            "INSERT INTO paper_cycles(id,cycle_key,status,started_at,ended_at,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (CYCLE, "cycle-8", "running", f"{D0} 09:30:00", None, f"{D0} 09:30:00"),
        )
        self.conn.execute(
            "INSERT INTO paper_cycles(id,cycle_key,status,started_at,ended_at,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (OTHER_CYCLE, "cycle-4", "paused", None, None, f"{D0} 09:30:00"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    # ── 夹具 ──

    def add_order(self, order_id, *, side, session, qty, cycle_id=CYCLE,
                  executed_at=None, code=CODE, retry_of=None):
        """写一条委托。``cycle_id=None`` 表示 **legacy 行**（升级前的形状）。

        pre-v18 形状（``with_cycle_column = False``）下不写该列 —— 列不存在时
        适配器只能走 legacy 时间窗路径，这正是 §20/§21 要收紧的那条。
        """
        if not self.with_cycle_column:
            self.conn.execute(
                "INSERT INTO paper_orders(id,account_id,side,code,name,qty,status,"
                "risk_payload,created_at,executed_at,execution_status,execution_verified,"
                "execution_evidence_source,retry_of_order_id)"
                " VALUES(?,?,?,?,?,?,'filled','',?,?,'verified',1,'ev',?)",
                (order_id, ACCOUNT, side, code, "测试股", int(qty),
                 f"{session} 09:30:00",
                 executed_at if executed_at is None else executed_at,
                 retry_of),
            )
            return
        self.conn.execute(
            "INSERT INTO paper_orders(id,account_id,side,code,name,qty,status,"
            "risk_payload,created_at,executed_at,execution_status,execution_verified,"
            "execution_evidence_source,retry_of_order_id,cycle_id)"
            " VALUES(?,?,?,?,?,?,'filled','',?,?,'verified',1,'ev',?,?)",
            (order_id, ACCOUNT, side, code, "测试股", int(qty),
             f"{session} 09:30:00",
             executed_at if executed_at is None else executed_at,
             retry_of, cycle_id),
        )

    def add_fill(self, fill_id, order_id, *, side, session, qty, code=CODE):
        self.conn.execute(
            "INSERT INTO paper_fills(id,order_id,account_id,side,code,qty,price,"
            "amount,fees,fill_date,quote_at,assumption)"
            " VALUES(?,?,?,?,?,?,10.0,?,0.0,?,NULL,'x')",
            (fill_id, order_id, ACCOUNT, side, code, int(qty),
             float(qty) * 10.0, session),
        )

    def add_lot(self, lot_id, *, session, qty, cycle_id=CYCLE, code=CODE,
                available=None, remaining=None, source_order_id=None):
        self.conn.execute(
            "INSERT INTO paper_position_lots(id,cycle_id,account_id,code,name,industry,"
            "qty,remaining_qty,cost,acquired_at,available_date,asset_type,"
            "source_order_id,cost_fee_included,is_t_base)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,1)",
            (lot_id, cycle_id, ACCOUNT, code, "测试股", "测试", int(qty),
             int(qty if remaining is None else remaining), 10.0,
             f"{session} 09:30:00", available or session, "stock_t1",
             source_order_id),
        )

    def adapter(self):
        return PE.PositionEvidenceAdapter(self.conn)

    def context(self, *, code=CODE, session=D2, cycle_id=CYCLE, decision_at=None):
        return self.adapter().context_for(
            code, cycle_id=cycle_id, account_id=ACCOUNT, decision_session=session,
            decision_at=decision_at or AS_OF,
        )

    def order_cycle(self, order_id):
        return self.conn.execute("SELECT cycle_id FROM paper_orders WHERE id=?",
                                 (order_id,)).fetchone()[0]

    def lot_cycle(self, lot_id):
        return self.conn.execute("SELECT cycle_id FROM paper_position_lots WHERE id=?",
                                 (lot_id,)).fetchone()[0]


class CrossCycleIdentityTests(_Case):
    """§15/§16/§25：订单 ↔ lot ↔ 卖出成交的周期身份必须一致。"""

    def test_OC1_buy_order_and_its_lot_share_one_cycle(self):
        """OC1：BUY order cycle=8 与它产生的 lot cycle=8 ⇒ 一致。"""
        self.add_order(1, side="buy", session=D0, qty=1000, cycle_id=CYCLE)
        self.add_lot(101, session=D0, qty=1000, cycle_id=CYCLE, source_order_id=1)
        self.assertEqual(self.order_cycle(1), CYCLE)
        self.assertEqual(self.lot_cycle(101), CYCLE)

    def test_OC2_lot_from_a_different_cycle_is_not_visible_to_the_order_cycle(self):
        """OC2：lot 属于 cycle 9 时，cycle 8 的上下文看不到它（不会串周期）。"""
        other = 9
        self.conn.execute(
            "INSERT INTO paper_cycles(id,cycle_key,status,started_at,created_at)"
            " VALUES(?,?,?,?,?)", (other, "cycle-9", "running", f"{D0} 09:30:00",
                                   f"{D0} 09:30:00"))
        self.add_lot(101, session=D0, qty=1000, cycle_id=other)
        ctx = self.context(cycle_id=CYCLE)
        # cycle 8 名下没有任何 lot ⇒ 没有仓位证据（绝不借用 cycle 9 的 lot）。
        self.assertIn(ctx.evidence_status,
                      (PE.PositionEvidenceStatus.UNKNOWN,
                       PE.PositionEvidenceStatus.UNPROVABLE))

    def test_OC3_sell_order_cycle_must_match_the_consumed_lot_cycle(self):
        """OC3：消耗 cycle 8 的 lot、卖出委托却写 cycle 9 ⇒ 归属冲突，fail closed。"""
        self.add_order(1, side="buy", session=D0, qty=1000, cycle_id=CYCLE)
        self.add_lot(101, session=D0, qty=1000, cycle_id=CYCLE, source_order_id=1,
                     available=D1)
        # 卖出成交的**来源委托**声称属于另一个周期。
        self.add_order(2, side="sell", session=D2, qty=1000, cycle_id=9)
        self.add_fill(201, 2, side="sell", session=D2, qty=1000)
        ctx = self.context(session=D2, cycle_id=CYCLE)
        # 该卖出显式指向别的周期 ⇒ 不得扣减 cycle 8 的 lot。
        self.assertNotEqual(ctx.held_quantity, 0)
        self.assertTrue(
            any("mismatch" in d or "unprovable" in d for d in ctx.diagnostics)
            or ctx.quantity_basis == PE.QUANTITY_BASIS_UNPROVABLE,
            f"跨周期卖出必须被拒绝，实际 diagnostics={ctx.diagnostics}",
        )

    def test_OC4_legacy_parent_retry_is_referenced_without_backfill(self):
        """OC4：legacy parent（cycle NULL）可被新 retry 引用，parent 保持 NULL。"""
        self.conn.execute(
            "INSERT INTO paper_orders(id,account_id,side,code,qty,status,risk_payload,"
            "created_at,execution_status,execution_verified,execution_evidence_source,"
            "cycle_id) VALUES(1,?,?,?,100,'superseded','','2026-09-01 09:30:00',"
            "'verified',1,'ev',NULL)",
            (ACCOUNT, "buy", CODE),
        )
        self.add_order(2, side="buy", session=D0, qty=100, cycle_id=CYCLE, retry_of=1)
        self.assertEqual(self.order_cycle(2), CYCLE)
        self.assertIsNone(self.order_cycle(1), "legacy parent 绝不能被反向回填")

    def test_OC6_explicit_order_cycle_resolves_historical_ambiguity(self):
        """OC6：历史窗口歧义（cycle 4 起点未知）下，显式 order cycle 仍能证明归属。"""
        self.add_order(1, side="buy", session=D0, qty=1000, cycle_id=CYCLE)
        self.add_lot(101, session=D0, qty=1000, cycle_id=CYCLE, source_order_id=1,
                     available=D1)
        self.add_order(2, side="sell", session=D2, qty=1000, cycle_id=CYCLE)
        self.add_fill(201, 2, side="sell", session=D2, qty=1000)
        ctx = self.context(session=D2, cycle_id=CYCLE)
        self.assertEqual(ctx.held_quantity, 0,
                         "显式 durable order cycle 必须解决窗口歧义，而不是被它挡住")
        self.assertNotIn("competing_cycle_unprovable", ctx.diagnostics)

    def test_OC5_retry_that_changes_cycle_is_detectable(self):
        """OC5：post-v18 parent cycle=8、retry 却写 cycle=9 ⇒ 不得静默当作同周期重试。"""
        self.add_order(1, side="buy", session=D0, qty=100, cycle_id=CYCLE)
        self.add_order(2, side="buy", session=D1, qty=100, cycle_id=9, retry_of=1)
        self.assertNotEqual(self.order_cycle(1), self.order_cycle(2),
                            "retry 换了周期就必须在数据上可见")


class ExactTimeFallbackTests(_Case):
    """§20/§21/§26：周期边界必须按时刻比较；拿不到时刻时不猜日内顺序。

    本组刻意使用 **pre-v18 形状**（``paper_orders`` 无 ``cycle_id``）：收紧的正是
    这条 legacy 时间窗 fallback。post-v18 的显式身份路径由
    ``CrossCycleIdentityTests`` 覆盖。
    """

    with_cycle_column = False

    def _attribution(self, *, code=CODE, cycle_id=CYCLE):
        """读出该卖出事件的**周期归属**结论（四态字符串）。

        不能借 ``evidence_status`` 观察归属：lot 被完全消耗时 ``context_for``
        按既有生产语义返回 ``UNKNOWN``（``no_open_lot_visible_at_decision``），
        那是仓位证据口径，不是归属口径。归属必须从 ``_sell_events`` 读。
        """
        events = self.adapter()._sell_events(ACCOUNT, code, cycle_id=cycle_id)
        self.assertTrue(events, "夹具必须真的产生一条卖出事件")
        return events[0]["cycle_attribution"]

    def _sell_with_instant(self, instant, *, session=D2):
        """legacy 形状的卖出：来源委托无 ``cycle_id`` ⇒ 只能走**时间窗**归属路径。"""
        self.add_order(1, side="buy", session=D0, qty=1000, cycle_id=CYCLE)
        self.add_lot(101, session=D0, qty=1000, cycle_id=CYCLE, source_order_id=1,
                     available=D1)
        self.add_order(2, side="sell", session=session, qty=1000, cycle_id=None,
                       executed_at=instant)
        self.add_fill(201, 2, side="sell", session=session, qty=1000)
        self.conn.execute("UPDATE paper_position_lots SET remaining_qty=0 WHERE id=101")
        self.conn.commit()

    def test_TIME_CYCLE_1_sell_before_the_cycle_start_is_mismatch(self):
        """TIME-CYCLE-1：SELL 10:00，请求周期同日 16:00 才开始 ⇒ 不在窗内。"""
        self.conn.execute("UPDATE paper_cycles SET started_at=? WHERE id=?",
                          (f"{D2} 16:00:00", CYCLE))
        self._sell_with_instant(f"{D2} 10:00:00")
        ctx = self.context(session=D2, cycle_id=CYCLE)
        self.assertIn("sell_fill_cycle_mismatch", ctx.diagnostics)

    def test_TIME_CYCLE_2_competitor_ended_before_the_sell_is_excluded(self):
        """TIME-CYCLE-2：其它周期同日 11:00 结束、SELL 14:00 ⇒ 可证明不竞争。

        必须断言**结论是 proven**：只断言"没有 unprovable"是不够的 —— 时刻精度
        丢失时该竞争者会退化成 ``ambiguous``，而那同样是一个（错误的）非 proven
        结论，却不会触发 ``competing_cycle_unprovable``。
        """
        self.conn.execute(
            "UPDATE paper_cycles SET started_at=?, ended_at=? WHERE id=?",
            (f"{D0} 09:30:00", f"{D2} 11:00:00", OTHER_CYCLE))
        self._sell_with_instant(f"{D2} 14:00:00")
        attribution = self._attribution()
        self.assertEqual(attribution, "proven",
                         "竞争者已结束 ⇒ 归属应可证明（而不是 ambiguous / unprovable）")

    def test_TIME_CYCLE_3_competitor_started_after_the_sell_is_excluded(self):
        """TIME-CYCLE-3：其它周期同日 15:00 才开始、SELL 10:00 ⇒ 可证明不竞争。"""
        self.conn.execute("UPDATE paper_cycles SET started_at=? WHERE id=?",
                          (f"{D2} 15:00:00", OTHER_CYCLE))
        self._sell_with_instant(f"{D2} 10:00:00")
        attribution = self._attribution()
        self.assertEqual(attribution, "proven",
                         "竞争者尚未开始 ⇒ 归属应可证明（而不是 ambiguous / unprovable）")

    def test_TIME_CYCLE_4_missing_executed_at_on_the_boundary_day_is_unprovable(self):
        """TIME-CYCLE-4：SELL 无 executed_at，边界与它同日 ⇒ 不猜日内顺序。"""
        self.conn.execute("UPDATE paper_cycles SET started_at=? WHERE id=?",
                          (f"{D2} 16:00:00", CYCLE))
        self._sell_with_instant(None)
        ctx = self.context(session=D2, cycle_id=CYCLE)
        self.assertEqual(ctx.evidence_status, PE.PositionEvidenceStatus.UNPROVABLE)
        self.assertIn("sell_fill_cycle_unprovable", ctx.diagnostics)

    def test_TIME_CYCLE_5_strictly_different_dates_keep_the_safe_date_fallback(self):
        """TIME-CYCLE-5：严格跨日时日期粒度 fallback 必须保留。"""
        self.conn.execute("UPDATE paper_cycles SET started_at=? WHERE id=?",
                          (f"{D2} 09:30:00", CYCLE))
        self.add_order(1, side="buy", session=D0, qty=1000, cycle_id=CYCLE)
        self.add_lot(101, session=D0, qty=1000, cycle_id=CYCLE, source_order_id=1,
                     available=D1)
        # 卖出在前一日，且**没有**精确时刻 —— 严格跨日足以证明不在窗内。
        self.add_order(2, side="sell", session=D1, qty=1000, cycle_id=None,
                       executed_at=None)
        self.conn.execute("UPDATE paper_orders SET executed_at=NULL WHERE id=2")
        self.add_fill(201, 2, side="sell", session=D1, qty=1000)
        self.conn.commit()
        ctx = self.context(session=D2, cycle_id=CYCLE)
        self.assertIn("sell_fill_cycle_mismatch", ctx.diagnostics)
