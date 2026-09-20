# -*- coding: utf-8 -*-
"""R20 SELL 成交提交收敛回归（SF-1 ~ SF-14）。

这些用例驱动真实生产路径：
- ``PT.monitor_risk`` -> ``_monitor_risk_impl`` -> ``EP.commit_fill``
- ``PT._intraday_sell`` -> ``EP.commit_fill``
- manual / deferred SELL 的真实 primitive ``EP.commit_fill``

它们不复制成交账本写入逻辑；断言的是“成交只能由一个 commit owner 落库”。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import types
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import execution_planner as EP  # noqa: E402
import paper_position_risk_state as PPRS  # noqa: E402
import paper_trading as PT  # noqa: E402
import test_position_risk_state as PRS  # noqa: E402
from test_position_risk_state import ACCOUNT, CODE, NAME  # noqa: E402


def _raise_stamp_failure(conn, order_id):
    raise RuntimeError("R20 injected stamp failure")


class _R20RiskBase(PRS._ProductionRiskScanCase):
    """真实 risk-scan fixture，额外提供账户现金读取。"""

    def account_cash(self):
        return float(self.conn.execute(
            "SELECT cash FROM paper_accounts WHERE id=?", (self.ACCOUNT,)
        ).fetchone()[0])


class RiskSellCommitConvergence(_R20RiskBase):
    """SF-1 / SF-4 / SF-5 / SF-11 / SF-12 / SF-13：风控 SELL。"""

    def _drive_full_risk_exit(self):
        self._inner._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)
        return PT.monitor_risk(self.day)

    def test_sf1_risk_sell_commits_through_execution_planner(self):
        self.add_lot(100, 10.0)
        self._inner._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)
        calls = []
        original = EP.commit_fill

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        cash_before = self.account_cash()
        with mock.patch.object(EP, "commit_fill", side_effect=spy), \
             mock.patch.object(PT, "process_pending_manual_orders", return_value=[]):
            result = PT.monitor_risk(self.day)

        filled = [item for item in result.get("orders", [])
                  if item.get("status") == "filled"]
        self.assertEqual(len(filled), 1, result.get("orders"))
        self.assertEqual(len(calls), 1, "risk SELL 没有经过 EP.commit_fill")
        self.assertEqual(len(self.sell_fills()), 1)
        self.assertLess(self.remaining_lots(), 100)
        self.assertGreater(self.account_cash(), cash_before)
        row = self.sell_orders()[0]
        self.assertEqual(row["status"], "filled")
        self.assertEqual(int(row["execution_verified"] or 0), 1)

    def test_sf1b_paused_out_of_cycle_account_still_commits(self):
        """被禁用策略会留下 paused + cycle_id=NULL；它仍必须能风控清仓。"""
        self.add_lot(100, 10.0)
        self.conn.execute(
            "UPDATE paper_accounts SET status='paused', cycle_id=NULL WHERE id=?",
            (self.ACCOUNT,),
        )
        self.conn.commit()
        self._inner._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        result = PT.monitor_risk(self.day)

        filled = [item for item in result.get("orders", [])
                  if item.get("status") == "filled"]
        self.assertEqual(len(filled), 1, result.get("orders"))
        self.assertEqual(self.remaining_lots(), 0)
        self.assertIsNone(self.state_row())

    def test_sf4_risk_partial_sell_advances_take_stage(self):
        self.add_lot(200, 10.0)
        self._inner._set_fresh_exit_quote(
            self.code, price=10.9, pct=9.0, high=11.0, low=10.8,
        )

        result = PT.monitor_risk(self.day)

        filled = [item for item in result.get("orders", [])
                  if item.get("status") == "filled"]
        self.assertEqual(len(filled), 1, result.get("orders"))
        self.assertEqual(int(filled[0]["qty"]), 100)
        self.assertEqual(self.remaining_lots(), 100)
        row = self.state_row()
        self.assertIsNotNone(row, "partial risk SELL 不应关闭 episode")
        self.assertEqual(int(row["take_stage"]), 1, "risk partial SELL 未推进 take_stage")

    def test_sf5_risk_full_sell_closes_episode(self):
        self.add_lot(100, 10.0)
        self.assertIsNotNone(self.state_row())

        result = self._drive_full_risk_exit()

        filled = [item for item in result.get("orders", [])
                  if item.get("status") == "filled"]
        self.assertEqual(len(filled), 1, result.get("orders"))
        self.assertEqual(self.remaining_lots(), 0)
        self.assertIsNone(self.state_row(), "full risk SELL 后 episode 残留")

    def test_sf11_risk_commit_failure_rolls_back_atomically(self):
        self.add_lot(100, 10.0)
        self._inner._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)
        cash_before = self.account_cash()
        fills_before = self.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        lots_before = self.remaining_lots()
        state_before = self.state_row() is not None
        stamp_stub = types.SimpleNamespace(stamp_order=_raise_stamp_failure)

        with mock.patch.object(EP, "_ev", return_value=stamp_stub), \
             mock.patch.object(PT, "process_pending_manual_orders", return_value=[]):
            result = PT.monitor_risk(self.day)

        self.assertEqual(result["orders"][0]["status"], "execution_retry")
        self.assertAlmostEqual(self.account_cash(), cash_before, places=4)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
            fills_before,
        )
        self.assertEqual(self.remaining_lots(), lots_before)
        self.assertEqual(self.state_row() is not None, state_before)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE side='sell' AND status='filled'"
            ).fetchone()[0],
            0,
        )

    def test_sf12_risk_log_and_audit_exactly_once(self):
        self.add_lot(100, 10.0)
        self._drive_full_risk_exit()

        risk_count = self.conn.execute(
            "SELECT COUNT(*) FROM paper_risk_decisions"
            " WHERE account_id=? AND code=? AND side='sell' AND decision='filled'",
            (self.ACCOUNT, self.code),
        ).fetchone()[0]
        audit_count = self.conn.execute(
            "SELECT COUNT(*) FROM paper_audit"
            " WHERE account_id=? AND event='sell_filled'",
            (self.ACCOUNT,),
        ).fetchone()[0]
        self.assertEqual(risk_count, 1)
        self.assertEqual(audit_count, 1)

    def test_sf13_risk_fill_evidence_is_preserved(self):
        self.add_lot(100, 10.0)
        self._drive_full_risk_exit()

        fill = self.conn.execute(
            "SELECT * FROM paper_fills WHERE account_id=? AND side='sell'",
            (self.ACCOUNT,),
        ).fetchone()
        self.assertIsNotNone(fill)
        self.assertEqual(int(fill["qty"]), 100)
        self.assertEqual(fill["fill_date"], self.day.isoformat())
        self.assertEqual(fill["quote_at"], self._inner.quotes_map[self.code]["quote_at"])
        self.assertEqual(fill["assumption"], "实时价 - 0.10% 滑点，含佣金及印花税")
        self.assertGreater(float(fill["price"]), 0)
        self.assertGreater(float(fill["amount"]), 0)
        self.assertGreater(float(fill["fees"]), 0)


class _R20IntradayBase(_R20RiskBase):
    """真实 intraday SELL fixture（普通 T 卖与 opening-event 共用入口）。"""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "UPDATE paper_accounts SET mode='intraday_t' WHERE id=?", (self.ACCOUNT,)
        )
        self.conn.commit()

    def set_t_sell_quote(self, *, price=11.0, high=11.5, prev_close=11.0):
        self._inner.quotes_map[self.code] = {
            "code": self.code, "name": f"测试股_{self.code}", "price": price,
            "high": high, "low": round(price - 0.2, 4), "pct": 0.0,
            "prev_close": prev_close, "amount": 100000.0, "volume": 10000.0,
            "turnover": 1.0, "quote_source": "live",
            "quote_at": f"{self.day.isoformat()} 10:30:00",
            "quote_validation": "cross_source_checked",
        }

    def drive_intraday(self, *, opening_event=False):
        account = dict(self.conn.execute(
            "SELECT * FROM paper_accounts WHERE id=?", (self.ACCOUNT,)
        ).fetchone())
        cycle = PT._active_cycle(self.conn)
        positions = PT._position_rows(self.conn, self.ACCOUNT, self.day)
        self.assertEqual(len(positions), 1, "夹具应恰好产出一条持仓")
        patches = [mock.patch.object(PT, "_completed_kline", return_value=None)]
        if opening_event:
            patches.append(mock.patch.object(
                PT, "_opening_event_assessment",
                return_value={"passed": True, "reason": "R20 opening fixture"},
            ))
        for patcher in patches:
            patcher.start()
        try:
            action, reason = PT._intraday_sell(
                self.conn, account, dict(positions[0]),
                dict(self._inner.quotes_map[self.code]), self.day,
                PT._risk_profile(account, conn=self.conn), cycle,
                opening_event=opening_event,
            )
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        self.conn.commit()
        return action, reason


class IntradaySellCommitConvergence(_R20IntradayBase):
    """SF-2 / SF-3 / SF-6 / SF-7 / SF-11 / SF-12 / SF-13：日内 SELL。"""

    def test_sf2_intraday_sell_commits_through_execution_planner(self):
        self.add_lot(100, 10.0)
        self.set_t_sell_quote()
        calls = []
        original = EP.commit_fill

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        cash_before = self.account_cash()
        with mock.patch.object(EP, "commit_fill", side_effect=spy):
            action, reason = self.drive_intraday()

        self.assertIsNotNone(action, reason)
        self.assertEqual(len(calls), 1, "intraday SELL 没有经过 EP.commit_fill")
        self.assertEqual(len(self.sell_fills()), 1)
        self.assertEqual(self.remaining_lots(), 0)
        self.assertGreater(self.account_cash(), cash_before)
        row = self.sell_orders()[0]
        self.assertEqual(row["status"], "filled")
        self.assertEqual(int(row["execution_verified"] or 0), 1)

    def test_sf3_opening_event_sell_commits_through_execution_planner(self):
        self.add_lot(500, 10.0)
        self.set_t_sell_quote()
        calls = []
        original = EP.commit_fill

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        with mock.patch.object(EP, "commit_fill", side_effect=spy):
            action, reason = self.drive_intraday(opening_event=True)

        self.assertIsNotNone(action, reason)
        self.assertTrue(action["opening_event"])
        self.assertEqual(len(calls), 1, "opening-event SELL 走了第二套提交")
        order = self.sell_orders()[0]
        self.assertEqual(order["status"], "filled")

    def test_sf6_intraday_partial_sell_preserves_take_stage(self):
        self.add_lot(500, 10.0)
        PPRS.update_take_stage(
            self.conn, cycle_id=self.cycle, account_id=self.ACCOUNT,
            code=self.code, take_stage=1,
        )
        self.conn.commit()
        self.set_t_sell_quote()

        action, reason = self.drive_intraday()

        self.assertIsNotNone(action, reason)
        self.assertEqual(int(action["qty"]), 100)
        self.assertEqual(self.remaining_lots(), 400)
        row = self.state_row()
        self.assertIsNotNone(row)
        self.assertEqual(int(row["take_stage"]), 1, "intraday partial 重置了 take_stage")

    def test_sf7_intraday_full_sell_closes_episode(self):
        self.add_lot(100, 10.0)
        self.set_t_sell_quote()

        action, reason = self.drive_intraday()

        self.assertIsNotNone(action, reason)
        self.assertEqual(self.remaining_lots(), 0)
        self.assertIsNone(self.state_row())

    def test_sf11_intraday_commit_failure_rolls_back_atomically(self):
        self.add_lot(100, 10.0)
        self.set_t_sell_quote()
        cash_before = self.account_cash()
        fills_before = self.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        lots_before = self.remaining_lots()
        state_before = self.state_row() is not None
        stamp_stub = types.SimpleNamespace(stamp_order=_raise_stamp_failure)

        with mock.patch.object(EP, "_ev", return_value=stamp_stub):
            action, reason = self.drive_intraday()

        self.assertIsNone(action)
        self.assertIn("高抛执行失败", reason)
        self.assertAlmostEqual(self.account_cash(), cash_before, places=4)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
            fills_before,
        )
        self.assertEqual(self.remaining_lots(), lots_before)
        self.assertEqual(self.state_row() is not None, state_before)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE side='sell' AND status='filled'"
            ).fetchone()[0],
            0,
        )

    def test_sf12_intraday_log_and_audit_exactly_once(self):
        self.add_lot(100, 10.0)
        self.set_t_sell_quote()
        self.drive_intraday()

        risk_count = self.conn.execute(
            "SELECT COUNT(*) FROM paper_risk_decisions"
            " WHERE account_id=? AND code=? AND side='sell'"
            " AND decision='intraday_t_sell'",
            (self.ACCOUNT, self.code),
        ).fetchone()[0]
        audit_count = self.conn.execute(
            "SELECT COUNT(*) FROM paper_audit"
            " WHERE account_id=? AND event='intraday_t_sell'",
            (self.ACCOUNT,),
        ).fetchone()[0]
        self.assertEqual(risk_count, 1)
        self.assertEqual(audit_count, 1)

    def test_sf13_intraday_fill_evidence_is_preserved(self):
        self.add_lot(100, 10.0)
        self.set_t_sell_quote()
        self.drive_intraday()

        fill = self.conn.execute(
            "SELECT * FROM paper_fills WHERE account_id=? AND side='sell'",
            (self.ACCOUNT,),
        ).fetchone()
        self.assertIsNotNone(fill)
        self.assertEqual(int(fill["qty"]), 100)
        self.assertEqual(fill["fill_date"], self.day.isoformat())
        self.assertEqual(fill["quote_at"], self._inner.quotes_map[self.code]["quote_at"])
        self.assertEqual(fill["assumption"], "开盘/5分钟实时快照高抛，含滑点、佣金、印花税")
        self.assertGreater(float(fill["price"]), 0)
        self.assertGreater(float(fill["amount"]), 0)
        self.assertGreater(float(fill["fees"]), 0)


class DirectSellCommitConvergence(PRS._LedgerCase):
    """SF-8 / SF-9 / SF-10 / SF-14：commit primitive 的 SELL/BUY 契约。"""

    def remaining_lots(self, *, cycle_id=None):
        return PPRS.remaining_qty(
            self.conn, cycle_id=cycle_id or self.cycle1,
            account_id=ACCOUNT, code=CODE,
        )

    def sell_order(self, qty, *, cycle_id=None):
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        cur = self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,status,"
            "reason,risk_payload,created_at,origin,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "sell", CODE, NAME, qty, 13.0, "pending_execution", "R20 test",
             "{}", "2026-09-05 10:00:00", "manual", *stamp, cycle_id or self.cycle1),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def legacy_sell_order(self, qty):
        """模拟 v18 之前的 legacy NULL-cycle 订单（临时撤下当前库的写入 trigger）。"""
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        trigger = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger'"
            " AND name='trg_paper_orders_cycle_provenance_insert'"
        ).fetchone()[0]
        self.conn.execute("DROP TRIGGER trg_paper_orders_cycle_provenance_insert")
        try:
            cur = self.conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,status,"
                "reason,risk_payload,created_at,origin,strategy_id,strategy_version,"
                "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (ACCOUNT, "sell", CODE, NAME, qty, 13.0, "pending_execution", "R20 legacy",
                 "{}", "2026-09-05 10:00:00", "manual", *stamp),
            )
            self.conn.commit()
            return int(cur.lastrowid)
        finally:
            self.conn.execute(trigger)
            self.conn.commit()

    def buy_order(self, qty):
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        cur = self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,status,"
            "reason,risk_payload,created_at,origin,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", CODE, NAME, qty, 10.0, "pending_execution", "R20 test",
             "{}", "2026-09-05 10:00:00", "strategy", *stamp, self.cycle1),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def commit_sell(self, order_id, qty, *, price=13.0, day="2026-09-05"):
        return EP.commit_fill(
            self.conn, account=self.account_row(),
            plan={"side": "sell", "code": CODE, "qty": qty, "fill_price": price,
                  "amount": qty * price, "fees": 5.0, "quote_at": None, "risk": {}},
            order_id=order_id, asof_day=dt.date.fromisoformat(day),
            reserved=True, side="sell", action="filled",
            audit_action="sell_filled", reason="R20 test sell",
        )

    def test_sf8_realized_pnl_formula_unchanged(self):
        self.record_buy(200, 10.0)
        order = self.sell_order(100)

        pnl = self.commit_sell(order, 100, price=13.0)

        self.assertAlmostEqual(pnl, 1300.0 - 1000.0 - 5.0, places=6)
        row = self.conn.execute(
            "SELECT realized_pnl FROM paper_orders WHERE id=?", (order,)
        ).fetchone()
        self.assertAlmostEqual(float(row["realized_pnl"]), 295.0, places=6)

    def test_sf9_sell_consumes_the_order_cycle_only(self):
        self.record_buy(100, 10.0)
        other = self.add_cycle(status="archived")
        self.add_lot(other, 100, cost=20.0)
        order = self.sell_order(100, cycle_id=self.cycle1)

        self.commit_sell(order, 100)

        self.assertEqual(self.remaining_lots(), 0)
        other_qty = self.conn.execute(
            "SELECT remaining_qty FROM paper_position_lots"
            " WHERE cycle_id=? AND account_id=? AND code=?",
            (other, ACCOUNT, CODE),
        ).fetchone()[0]
        self.assertEqual(int(other_qty), 100, "SELL 消费了订单周期之外的 lot")

    def test_sf9b_unknown_order_cycle_fails_closed(self):
        self.record_buy(100, 10.0)
        order = self.legacy_sell_order(100)
        lots_before = self.remaining_lots()
        fills_before = self.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]

        with self.assertRaises(PT.OrderCycleProvenanceUnknown):
            self.commit_sell(order, 100)

        self.assertEqual(self.remaining_lots(), lots_before)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
            fills_before,
        )

    def test_sf10_order_identity_mismatch_fails_closed(self):
        self.record_buy(100, 10.0)
        order = self.sell_order(100)
        cash_before = self.account_row()["cash"]
        lots_before = self.remaining_lots()
        fills_before = self.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]

        with self.assertRaises(RuntimeError) as raised:
            EP.commit_fill(
                self.conn, account=self.account_row(),
                plan={"side": "sell", "code": "999999", "qty": 100,
                      "fill_price": 13.0, "amount": 1300.0, "fees": 5.0,
                      "quote_at": None},
                order_id=order, asof_day=dt.date(2026, 9, 5),
                reserved=True, side="sell",
            )
        self.assertIn("order identity mismatch", str(raised.exception))

        self.assertAlmostEqual(float(self.account_row()["cash"]), float(cash_before), places=6)
        self.assertEqual(self.remaining_lots(), lots_before)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
            fills_before,
        )

    def test_sf14_buy_commit_contract_is_unchanged(self):
        order = self.buy_order(100)

        EP.commit_fill(
            self.conn, account=self.account_row(),
            plan={"side": "buy", "code": CODE, "qty": 100, "fill_price": 10.0,
                  "amount": 1000.0, "fees": 1.0, "quote_at": None},
            order_id=order, asof_day=dt.date(2026, 9, 5),
            reserved=False, side="buy", action="strategy_buy",
            reason="R20 test buy",
        )

        row = self.conn.execute(
            "SELECT status FROM paper_orders WHERE id=?", (order,)
        ).fetchone()
        self.assertEqual(row["status"], "filled")
        lot_count = self.conn.execute(
            "SELECT COUNT(*) FROM paper_position_lots"
            " WHERE cycle_id=? AND account_id=? AND code=?",
            (self.cycle1, ACCOUNT, CODE),
        ).fetchone()[0]
        self.assertEqual(lot_count, 1)


if __name__ == "__main__":
    unittest.main()
