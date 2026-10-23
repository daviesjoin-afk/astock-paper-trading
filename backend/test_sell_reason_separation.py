# -*- coding: utf-8 -*-
"""卖出委托的三个独立事实：策略原因 / 执行失败原因 / 最终成交结果。

生产缺陷（2026-10-23 诊断）
--------------------------
``execution_planner.commit_fill`` 在执行被阻塞时把阻塞码写进 ``paper_orders.reason``
（覆盖策略/风控写下的"为什么卖"）。四笔真实成交的卖单因此都写着
``TRADABILITY_UNKNOWN``：执行阻塞时覆盖了原原因，恢复成交后调用方
（``process_pending_execution_orders``）又把这条被覆盖的 reason 原样传回来，
策略原因永久丢失，复盘无法回答"当时为什么卖"。

钉死的契约：

    SR-01  执行阻塞只写 ``execution_reasons``/``execution_evidence``，
           ``paper_orders.reason`` 保持策略/风控写下的事实。
    SR-02  阻塞若干轮后恢复成交，``reason`` 仍是策略原因（不被"最终执行结果"改写）。
    SR-03  多次阻塞-恢复循环不会改写 ``reason``，``execution_reasons`` 每轮刷新。
    SR-04  部分成交 → 再次成交：``reason`` 全程不变，成交结果由
           ``status``/``filled_qty``/``paper_fills`` 表达。
    SR-05  原风险决策关联（``paper_risk_decisions.order_id``）在阻塞与成交后仍完整。

真实生产 schema 的临时账本；不联网、不动真实数据库。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import execution_planner as EP  # noqa: E402
import paper_trading as PT  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600519"
NAME = "测试股"
DAY = "2026-09-05"
STRATEGY_REASON = "下跌防线：当日跌幅 -4.50%，相对沪深300 弱 3.28%（硬止损未触发，首段减仓）"


def _execution_facts(*, qty, price, available_liquidity=10_000_000, tradability=True,
                     observed="2026-09-05T10:00:00+08:00", sellable=None):
    quote = {
        "price": float(price), "amount": 10_000_000.0,
        "quote_at": observed, "execution_asof": observed,
        "quote_source": "live", "quote_validation": "cross_source_checked",
    }
    reading = EP.market_reading_for_execution(
        quote, asof_day=DAY, execution_asof=observed,
    )
    evidence = (
        SimpleNamespace(
            evidence_present=True, can_buy=True, can_sell=True,
            buy_block_reason=None, sell_block_reason=None,
            to_dict=lambda: {"evidence_present": True, "can_sell": True},
        )
        if tradability else None
    )
    context = EP.ExecutionContext(
        session_date=DAY, execution_asof=observed, quote=quote,
        market_reading=reading, tradability=evidence,
        session_phase="continuous_morning",
        available_liquidity=int(available_liquidity),
        sellable_quantity=qty if sellable is None else sellable,
        participation_rate=1.0,
    )
    return quote, context


class SellReasonSeparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self._patches = (
            mock.patch.object(PT, "DB_PATH", self.path),
            mock.patch.object(PT, "_benchmark_close", return_value=None),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
            mock.patch.object(PT, "_completed_kline", return_value=None),
        )
        for patcher in self._patches:
            patcher.start()
        PT.init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.cycle = int(self.conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT,)).fetchone()[0])
        self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
            "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
            "is_t_base,source_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.cycle, ACCOUNT, CODE, NAME, "测试", 1000, 1000, 12.0,
             "2026-09-01 10:00:00", "2026-09-02", "stock_t1", 1, 1, 4242),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for patcher in reversed(self._patches):
            patcher.stop()
        self.tmp.cleanup()

    # ---------- 夹具 ----------

    def _sell_order(self, qty=300):
        stamp = (PT._strategy_stamp(self.conn, ACCOUNT) or (None, None, None))
        cursor = self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,status,"
            "reason,risk_payload,created_at,origin,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "sell", CODE, NAME, qty, 13.0, "pending_execution",
             STRATEGY_REASON, "{}", f"{DAY} 10:00:00", "strategy", *stamp, self.cycle),
        )
        self.conn.commit()
        order_id = int(cursor.lastrowid)
        # 风控写入的卖出决策与该委托的关联（恢复成交后不得丢失）。
        PT._risk_log(
            self.conn, ACCOUNT, CODE, "sell", "downside_guard", STRATEGY_REASON,
            {"downside_guard": {"level": "partial"}}, order_id=order_id,
            authority="RISK", decision_kind="downside_guard",
        )
        self.conn.commit()
        return order_id

    def _attempt(self, order_id, qty, *, blocked=False, available_liquidity=10_000_000,
                 observed="2026-09-05T10:00:00+08:00", already_filled=0, status=None):
        quote, context = _execution_facts(
            qty=qty, price=13.0, available_liquidity=available_liquidity,
            tradability=not blocked, observed=observed,
        )
        if already_filled or status is not None:
            context = replace(
                context, already_filled_quantity=already_filled,
                current_order_status=status or context.current_order_status,
            )
        return EP.commit_fill(
            self.conn, account={"id": ACCOUNT},
            plan={"side": "sell", "code": CODE, "qty": qty, "fill_price": 13.0,
                  "amount": qty * 13.0, "fees": 5.0,
                  "quote_at": quote["quote_at"], "execution_quote": quote,
                  "risk": {}},
            order_id=order_id, asof_day=dt.date.fromisoformat(DAY),
            reserved=True, action="filled", reason=STRATEGY_REASON,
            execution_context=context,
        )

    def _order(self, order_id):
        return dict(self.conn.execute(
            "SELECT status,reason,filled_qty,remaining_qty,execution_reasons "
            "FROM paper_orders WHERE id=?", (order_id,)).fetchone())

    # ---------- SR-01：阻塞不改写策略原因 ----------

    def test_sr01_blocked_execution_keeps_the_strategy_reason(self):
        order_id = self._sell_order(qty=300)
        self._attempt(order_id, 300, blocked=True)
        self.conn.commit()

        order = self._order(order_id)
        self.assertEqual("pending_execution", order["status"])
        self.assertEqual(STRATEGY_REASON, order["reason"])
        self.assertIn("TRADABILITY_UNKNOWN", order["execution_reasons"] or "")

    # ---------- SR-02 / SR-03：阻塞后成交 / 多次恢复 ----------

    def test_sr02_fill_after_a_block_keeps_the_strategy_reason(self):
        order_id = self._sell_order(qty=300)
        self._attempt(order_id, 300, blocked=True)
        self.conn.commit()
        self._attempt(order_id, 300)
        self.conn.commit()

        order = self._order(order_id)
        self.assertEqual("filled", order["status"])
        self.assertEqual(300, int(order["filled_qty"]))
        self.assertEqual(STRATEGY_REASON, order["reason"])

    def test_sr03_repeated_blocks_and_recoveries_never_rewrite_the_reason(self):
        order_id = self._sell_order(qty=300)
        for _ in range(3):
            self._attempt(order_id, 300, blocked=True)
            self.conn.commit()
            self.assertEqual(STRATEGY_REASON, self._order(order_id)["reason"])
        self._attempt(order_id, 300)
        self.conn.commit()
        order = self._order(order_id)
        self.assertEqual("filled", order["status"])
        self.assertEqual(STRATEGY_REASON, order["reason"])

    # ---------- SR-04：部分成交 ----------

    def test_sr04_partial_then_rest_keeps_the_strategy_reason(self):
        order_id = self._sell_order(qty=300)
        # 参与额度只够 100 股 → 部分成交，阻塞码与流动性原因不得污染 reason。
        self._attempt(order_id, 300, available_liquidity=100)
        self.conn.commit()
        partial = self._order(order_id)
        self.assertEqual("partially_filled", partial["status"])
        self.assertEqual(100, int(partial["filled_qty"]))
        self.assertEqual(STRATEGY_REASON, partial["reason"])

        # 下一个行情事件（时间戳不同 → 新成交事件）吃完剩余 200 股。
        self._attempt(order_id, 200, observed="2026-09-05T10:05:00+08:00",
                      already_filled=100, status="partially_filled")
        self.conn.commit()
        final = self._order(order_id)
        self.assertEqual("filled", final["status"])
        self.assertEqual(300, int(final["filled_qty"]))
        self.assertEqual(STRATEGY_REASON, final["reason"])
        fills = self.conn.execute(
            "SELECT qty FROM paper_fills WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()
        self.assertEqual([100, 200], [int(row["qty"]) for row in fills],
                         "成交结果必须由 fills 表达，而不是改写委托原因")

    # ---------- SR-05：风险决策关联完整 ----------

    def test_sr05_risk_decision_link_survives_block_and_fill(self):
        order_id = self._sell_order(qty=300)
        self._attempt(order_id, 300, blocked=True)
        self.conn.commit()
        self._attempt(order_id, 300)
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT decision,reason FROM paper_risk_decisions WHERE order_id=?",
            (order_id,),
        ).fetchall()
        self.assertTrue(rows, "风险决策与卖单的关联在阻塞/成交后丢失")
        self.assertIn("downside_guard", {str(row["decision"]) for row in rows})
        self.assertTrue(any(STRATEGY_REASON == str(row["reason"]) for row in rows))


if __name__ == "__main__":
    unittest.main()
