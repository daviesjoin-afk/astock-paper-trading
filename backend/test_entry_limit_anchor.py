# -*- coding: utf-8 -*-
"""价格等待锚点与窗口（``paper_trading._entry_price_wait`` / ``_price_wait_outcome``）。

限价画像的参考价每轮取当轮实时价，重算出的限价会跟着价格上移：当轮买不到，
下一轮锚点更高，形成结构性追价。冻结口径仍然是"同一入场机会里第一次**因价格
挂起**的限价"，只降不升。2026-10-23 诊断修复后，这里钉死五件事：

    1. 锚点只由"其他条件都已满足、仅价格未到"的尝试建立——入场确认中、容量
       不足等非价格拒绝不得启动价格等待计时；
    2. 同一入场机会的多轮等待共享最早那张锚，取最早而不是最新（否则每轮重新
       锚定、追价棘轮复活）；
    3. 现价回到（或低于）冻结限价即为"等待兑现"，不能因为等得久就报"未回到
       限价"；
    4. 窗口到期是**本机会**的价格等待失效：留下终态标记，理由必须说清"当日
       不再追价、需新机会重新确认"；
    5. 入场状态机重触发（``retrigger_seq`` 递增）产生新机会，锚点独立，不得
       继续复用上一机会的冻结限价。

纯 fixture：内存 sqlite，不联网、不读写真实账本。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock())
import paper_trading as PT  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600901"
SIGNAL_ID = 9
WINDOW = PT.ENTRY_LIMIT_CHASE_WINDOW_MINUTES


class PriceWaitAnchorTests(unittest.TestCase):
    #: 固定参考时刻（周五下午，交易时段内）。所有单子时间戳与"等待了多久"都从它推导，
    #: 因此用例与真实墙钟无关 —— 以前用 ``now - minutes_ago`` 造时间戳，本地时间在
    #: 04:39 之前运行时会跨日，``substr(created_at,1,10)=当日`` 一条都查不到（CI 在
    #: 2026-10-24 的本地 00:16 就是这样红的）。
    REFERENCE = dt.datetime(2026, 10, 23, 14, 30, 0)

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE paper_orders(
                   id INTEGER PRIMARY KEY, account_id TEXT, code TEXT, side TEXT,
                   signal_id INTEGER, order_type TEXT, planned_price REAL,
                   status TEXT, created_at TEXT, risk_payload TEXT)"""
        )
        self.today = self.REFERENCE.date()
        self.opportunity = PT._entry_opportunity(SIGNAL_ID, None)
        self.other_opportunity = PT._entry_opportunity(
            SIGNAL_ID, {"record": {"retrigger_seq": 1,
                                   "first_trigger_at": "2026-10-23T13:04:40"}},
        )

    def _order(self, *, minutes_ago, price, opportunity=None, wait_status=None,
               signal_id=SIGNAL_ID, order_type="limit", status="execution_retry"):
        stamp = (self.REFERENCE - dt.timedelta(minutes=minutes_ago)).strftime(
            "%Y-%m-%d %H:%M:%S")
        payload = {}
        if wait_status is not None:
            payload["entry_price_wait"] = {
                "opportunity_id": (opportunity or self.opportunity)["id"],
                "anchor_price": price,
                "status": wait_status,
                "started_at": stamp,
                "window_minutes": WINDOW,
            }
        cursor = self.conn.execute(
            """INSERT INTO paper_orders(
                   account_id,code,side,signal_id,order_type,planned_price,status,
                   created_at,risk_payload)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (ACCOUNT, CODE, "buy", signal_id, order_type, price, status, stamp,
             json.dumps(payload)),
        )
        return int(cursor.lastrowid)

    def _resolve(self, opportunity=None):
        opportunity = opportunity or self.opportunity
        return PT._entry_price_wait(
            self.conn, ACCOUNT, CODE, SIGNAL_ID, self.today, opportunity["id"],
            reference_at=self.REFERENCE)

    # ---------- 第 1 条：只有价格等待才起锚 ----------

    def test_non_price_rejection_does_not_start_the_price_clock(self):
        """入场确认中/容量不足的限价单没有价格等待标记 → 不产生锚点。"""
        self._order(minutes_ago=248, price=72.77, wait_status=None)
        self.assertIsNone(self._resolve())

    def test_market_order_carries_no_price_wait(self):
        self._order(minutes_ago=5, price=20.04, order_type="market",
                    wait_status="waiting")
        self.assertIsNone(self._resolve())

    def test_no_price_wait_means_not_frozen(self):
        self.assertIsNone(self._resolve())

    # ---------- 第 2 条：同一机会取最早的锚 ----------

    def test_earliest_anchor_wins_within_one_opportunity(self):
        self._order(minutes_ago=8, price=20.04, wait_status="waiting")
        self._order(minutes_ago=2, price=20.44, wait_status="waiting")
        wait = self._resolve()
        self.assertEqual(20.04, wait["anchor_price"])
        self.assertGreaterEqual(wait["waited_minutes"], 7.5)
        self.assertFalse(wait["expired"])

    # ---------- 第 3 条：回到限价 = 等待兑现 ----------

    def test_returning_to_the_frozen_limit_is_not_an_expiry(self):
        self._order(minutes_ago=279, price=7.71, wait_status="waiting")
        wait = self._resolve()
        outcome = PT._price_wait_outcome(wait, 7.6677)
        self.assertEqual("price_returned", outcome["status"])

    def test_price_still_above_the_frozen_limit_fails_after_the_window(self):
        self._order(minutes_ago=279, price=7.71, wait_status="waiting")
        outcome = PT._price_wait_outcome(self._resolve(), 7.86)
        self.assertEqual("failed", outcome["status"])
        self.assertEqual(7.71, outcome["anchor_price"])

    def test_price_above_the_limit_inside_the_window_keeps_waiting(self):
        self._order(minutes_ago=3, price=7.71, wait_status="waiting")
        outcome = PT._price_wait_outcome(self._resolve(), 7.86)
        self.assertEqual("waiting", outcome["status"])

    # ---------- 第 4 条：到期留下终态标记 ----------

    def test_expired_marker_keeps_the_opportunity_closed(self):
        self._order(minutes_ago=20, price=7.71, wait_status="waiting")
        self._order(minutes_ago=1, price=7.71, wait_status="expired")
        wait = self._resolve()
        self.assertTrue(wait["expired"])
        # 已失效的机会即使价格回到锚点以下，也不再是"仍在等待"。
        self.assertEqual("price_returned",
                         PT._price_wait_outcome(wait, 7.60)["status"])
        self.assertEqual("failed",
                         PT._price_wait_outcome(wait, 7.90)["status"])

    def test_marker_is_only_written_for_price_waits(self):
        marker = PT._next_price_wait_marker(
            opportunity=self.opportunity,
            outcome=PT._price_wait_outcome(None, 20.5),
            limit_price=20.04, deferred=False, now="2026-10-23 10:32:29",
        )
        self.assertIsNone(marker, "非价格拒绝不能建立限价锚点")

    def test_marker_records_the_anchor_and_the_opportunity(self):
        marker = PT._next_price_wait_marker(
            opportunity=self.other_opportunity,
            outcome=PT._price_wait_outcome(None, 20.5),
            limit_price=20.04, deferred=True, now="2026-10-23 13:04:40",
        )
        self.assertEqual("waiting", marker["status"])
        self.assertEqual(20.04, marker["anchor_price"])
        self.assertEqual(f"{SIGNAL_ID}#1", marker["opportunity_id"])
        self.assertEqual(1, marker["retrigger_seq"])

    def test_expiry_marker_is_terminal_and_keeps_the_anchor(self):
        anchor = self._order(minutes_ago=30, price=20.04, wait_status="waiting")
        wait = self._resolve()
        marker = PT._next_price_wait_marker(
            opportunity=self.opportunity,
            outcome={**PT._price_wait_outcome(wait, 21.0),
                     "started_at": wait["started_at"]},
            limit_price=21.04, deferred=False, now="2026-10-23 11:02:00",
        )
        self.assertEqual("expired", marker["status"])
        self.assertEqual(20.04, marker["anchor_price"])
        self.assertEqual(wait["started_at"], marker["started_at"])
        self.assertIsNotNone(anchor)
        self.assertGreaterEqual(marker["waited_minutes"], 29)

    # ---------- 第 5 条：重触发 = 新机会，独立锚点 ----------

    def test_new_opportunity_does_not_reuse_the_previous_anchor(self):
        self._order(minutes_ago=60, price=72.77, wait_status="waiting")
        self.assertEqual(72.77, self._resolve()["anchor_price"])
        self.assertIsNone(
            self._resolve(self.other_opportunity),
            "状态机重触发后的新机会不得沿用上一机会的冻结限价",
        )

    def test_wait_duration_follows_the_injected_reference_instant(self):
        """``reference_at`` 必须真的决定等待时长（它是这段逻辑唯一的墙钟入口）。

        这条同时是非空性证明：若 ``_entry_price_wait`` 忽略注入值改回读墙钟，两条断言
        会同时失败，而不会因为"恰好今天也在交易时段"而假装通过。
        """
        self._order(minutes_ago=30, price=20.04, wait_status="waiting")
        early = PT._entry_price_wait(
            self.conn, ACCOUNT, CODE, SIGNAL_ID, self.today,
            self.opportunity["id"],
            reference_at=self.REFERENCE.isoformat(timespec="seconds"))
        later = PT._entry_price_wait(
            self.conn, ACCOUNT, CODE, SIGNAL_ID, self.today,
            self.opportunity["id"],
            reference_at=(self.REFERENCE + dt.timedelta(minutes=45)).isoformat(
                timespec="seconds"))
        self.assertAlmostEqual(30.0, early["waited_minutes"], places=6)
        self.assertAlmostEqual(75.0, later["waited_minutes"], places=6)

    def test_other_signal_is_isolated(self):
        self._order(minutes_ago=5, price=20.04, wait_status="waiting",
                    signal_id=SIGNAL_ID + 1)
        self.assertIsNone(self._resolve())

    def test_opportunity_id_is_signal_scoped(self):
        base = PT._entry_opportunity(9, None)
        retriggered = PT._entry_opportunity(9, {"record": {"retrigger_seq": 2}})
        other_signal = PT._entry_opportunity(10, {"record": {"retrigger_seq": 2}})
        self.assertEqual("9#0", base["id"])
        self.assertEqual("9#2", retriggered["id"])
        self.assertEqual("10#2", other_signal["id"])


if __name__ == "__main__":
    unittest.main()
