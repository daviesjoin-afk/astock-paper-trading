# -*- coding: utf-8 -*-
"""Production-path regression tests for paper trading risk-exit boundary.

Covers the complete end-to-end production chain for risk exit:
  _risk_exit_account_ids -> _position_rows -> _sell_plan / downside guard
  -> order generation -> execution & lot consumption (_consume_available_lots)
  -> order/fill recording -> shared cash credit (_credit_shared_cash)
  -> position sync & NAV recording.

Scenarios A through N:
  A: Normal execution participant + real holding + risk trigger -> executes sell exit.
  B: Paused account + remaining position -> no BUY eligibility, but risk exit SELL allowed;
     flattened account exits eligibility.
  C: Archived / out-of-cycle account + remaining position -> risk exit SELL allowed.
  D: Paused / archived account with zero remaining exposure -> no risk exit orders generated.
  E: Multiple lots -> FIFO deduction, exit quantity cannot exceed available/remaining.
  F: Partial fill / partial trim -> remaining lots accurately reduced; retains exit eligibility.
  G: Full fill -> remaining lot becomes 0; subsequent scan does not duplicate exit orders.
  H: Runner idempotency / repeated runs -> no double-spending of lots, fills, or cash.
  I: Existing pending/unfilled sell -> suppresses duplicate exit orders within cooldown.
  J: Freeze semantics & buy capacity limits do not block risk exit (PAPER_ENTRY_FREEZE).
  K: Cash release consistency -> exact net proceeds (amount - fees) credited to ledger.
  L: Slot occupancy isolation -> risk exit sells do not occupy buy slots.
  M: Golden snapshot state transition -> strict field-level DB integrity across 6 tables.
  N: Failure / exception path -> rollback via savepoint preserves lot state intact.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import paper_trading as PT  # noqa: E402
import universe as U  # noqa: E402


class PaperRiskExitProductionPathTestCase(unittest.TestCase):
    """Base test case managing temporary database and mock dependencies."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "paper_risk_test.sqlite3")
        self.old_db_path = PT.DB_PATH
        PT.DB_PATH = self.db_path
        PT.init_db()

        self.day = dt.date(2026, 9, 10)
        self.code = "600001"
        self.code_b = "600002"

        # Mock universe trade day to decouple from external calendar
        self.old_is_trade_day = U.is_trade_day
        U.is_trade_day = lambda value=None: (U._as_date(value) or dt.date.today()).weekday() < 5

        # Mock quotes, market, news, fund flow
        self.quotes_map: dict[str, dict] = {}
        self.old_quotes = PT._quotes
        PT._quotes = lambda codes, asof_date=None: {
            c: self.quotes_map[c] for c in codes if c in self.quotes_map
        }
        self.old_news_for = PT._news_for
        PT._news_for = lambda *a, **k: []
        self.old_market = PT._cached_close_market
        PT._cached_close_market = lambda conn, day, allow_network=False: {
            "breadth": 0.5, "sentiment": "neutral",
        }
        self.old_ad = PT.AD
        PT.AD = None

        # Activate standard running cycle
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='running' WHERE id=1")

    def tearDown(self):
        PT._quotes = self.old_quotes
        PT._news_for = self.old_news_for
        PT._cached_close_market = self.old_market
        PT.AD = self.old_ad
        U.is_trade_day = self.old_is_trade_day
        PT.DB_PATH = self.old_db_path
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _set_fresh_exit_quote(
        self,
        code: str,
        price: float = 9.0,
        pct: float = -8.0,
        high: float = 9.2,
        low: float = 8.9,
    ):
        """Configure a valid fresh quote that triggers risk exit without ST false-positive."""
        self.quotes_map[code] = {
            "code": code,
            "name": f"测试股_{code}",
            "price": price,
            "high": high,
            "low": low,
            "pct": pct,
            "amount": 100000.0,
            "volume": 10000.0,
            "turnover": 1.0,
            "quote_source": "live",
            "quote_at": f"{self.day.isoformat()} 14:50:00",
            "quote_validation": "cross_source_checked",
        }

    def _insert_lot(
        self,
        account_id: str,
        code: str,
        qty: int,
        cost: float,
        remaining_qty: int | None = None,
        available_date: str = "2026-09-09",
        acquired_at: str = "2026-09-08 10:00:00",
        cycle_id: int = 1,
    ) -> int:
        if remaining_qty is None:
            remaining_qty = qty
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, account_id)
            # 1. Insert a real, terminal, historical BUY seed order
            cur_order = conn.execute(
                """INSERT INTO paper_orders(
                       account_id, side, code, name, qty, planned_price, filled_price,
                       amount, fees, status, reason, risk_payload, created_at, executed_at,
                       order_type, origin, strategy_id, strategy_version, strategy_checksum,
                       cycle_id
                   ) VALUES (?, 'buy', ?, ?, ?, ?, ?, ?, 5.0, 'filled', 'seed_buy',
                             '{}', ?, ?, 'market', 'seed', ?, ?, ?, ?)""",
                (account_id, code, f"测试股_{code}", qty, cost, cost, qty * cost,
                 f"{available_date} 09:30:00", f"{available_date} 09:30:00", *stamp, cycle_id),
            )
            seed_order_id = int(cur_order.lastrowid)

            # 2. Insert seed fill with the exact real seed BUY order id
            conn.execute(
                """INSERT INTO paper_fills(
                       order_id, account_id, side, code, qty, price, amount, fees,
                       fill_date, quote_at, assumption
                   ) VALUES (?, ?, 'buy', ?, ?, ?, ?, 5.0, ?, ?, 'seed')""",
                (seed_order_id, account_id, code, qty, cost, qty * cost,
                 available_date, f"{available_date} 09:30:00"),
            )

            # 3. Insert lot
            cur = conn.execute(
                """INSERT INTO paper_position_lots(
                       cycle_id, account_id, code, name, industry, qty, remaining_qty,
                       cost, acquired_at, available_date, asset_type, cost_fee_included, is_t_base
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stock_t1', 1, 1)""",
                (cycle_id, account_id, code, f"测试股_{code}", "Tech", qty, remaining_qty,
                 cost, acquired_at, available_date),
            )
            PT._sync_positions(conn, asof_day=self.day)
            return cur.lastrowid

    def _clear_scan_run_state(self):
        """清掉本次扫描的 durable 运行身份，模拟"下一次调度周期"。

        R16 之前这一步删的是 ``paper_audit`` 的 ``risk_scan_state`` 标记 —— 那是
        当时唯一的执行权威。R16 起执行权威搬到 ``paper_risk_scan_runs``（身份为
        ``cycle_id + asof_date + scan_minute``），``paper_audit`` 只剩 observability。
        因此"让同一分钟的下一次扫描能真正执行"必须清 durable 运行行；只删 audit
        已经**不再**能打开闸门（这正是本 PR 要证明的降级）。
        """
        with PT._db(immediate=True) as conn:
            conn.execute("DELETE FROM paper_risk_scan_runs")
            conn.execute("DELETE FROM paper_audit WHERE event LIKE 'risk_scan%'")


class TestPaperRiskExitProductionPath(PaperRiskExitProductionPathTestCase):
    """Execution tests for scenarios A through N."""

    # -------------------------------------------------------------------------
    # Scenario A: 正常执行中账户 + 真实持仓 + 触发风控
    # -------------------------------------------------------------------------
    def test_A_normal_running_account_full_risk_exit_pipeline(self):
        account_id = "tq_breakout"
        # 1000 shares @ 10.0, price 9.0 (-10% return <= hard stop -5%), crash pct -8.0% -> full 1.0 exit
        self._insert_lot(account_id, self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        with PT._db() as conn:
            init_cash = conn.execute("SELECT cash FROM paper_accounts WHERE id=?", (account_id,)).fetchone()[0]

        res = PT.monitor_risk(self.day)
        self.assertEqual(res.get("slot"), "risk")
        self.assertEqual(len(res.get("orders", [])), 1)
        self.assertEqual(res["orders"][0]["status"], "filled")
        self.assertEqual(res["orders"][0]["qty"], 1000)

        with PT._db() as conn:
            # Sells: exactly 1 new risk exit sell order
            sell_orders = conn.execute(
                "SELECT id, side, code, qty, status FROM paper_orders WHERE account_id=? AND side='sell'",
                (account_id,),
            ).fetchall()
            self.assertEqual(len(sell_orders), 1)
            self.assertEqual(sell_orders[0]["qty"], 1000)
            self.assertEqual(sell_orders[0]["status"], "filled")

            # Seed buys: historical seed buy order exists and is terminal
            buy_orders = conn.execute(
                "SELECT id, side, code, qty, status FROM paper_orders WHERE account_id=? AND side='buy'",
                (account_id,),
            ).fetchall()
            self.assertEqual(len(buy_orders), 1)
            self.assertEqual(buy_orders[0]["status"], "filled")

            # Fills check
            sell_fills = conn.execute(
                "SELECT order_id, side, code, qty, price, amount, fees FROM paper_fills WHERE account_id=? AND side='sell'",
                (account_id,),
            ).fetchall()
            self.assertEqual(len(sell_fills), 1)
            self.assertEqual(sell_fills[0]["order_id"], sell_orders[0]["id"], "Risk-exit SELL fill must join to SELL order")
            self.assertEqual(sell_fills[0]["qty"], 1000)
            fill_amount = sell_fills[0]["amount"]
            fill_fees = sell_fills[0]["fees"]

            buy_fills = conn.execute(
                "SELECT order_id, side, code, qty, price, amount, fees FROM paper_fills WHERE account_id=? AND side='buy'",
                (account_id,),
            ).fetchall()
            self.assertEqual(len(buy_fills), 1)
            self.assertEqual(buy_fills[0]["order_id"], buy_orders[0]["id"], "Seed BUY fill must join to seed BUY order")

            # Invariant: no fill may join to an opposite-side order
            self.assertNotEqual(sell_fills[0]["order_id"], buy_orders[0]["id"])
            self.assertNotEqual(buy_fills[0]["order_id"], sell_orders[0]["id"])

            lots = conn.execute("SELECT remaining_qty FROM paper_position_lots WHERE account_id=?", (account_id,)).fetchall()
            self.assertEqual(len(lots), 1)
            self.assertEqual(lots[0]["remaining_qty"], 0)

            post_cash = conn.execute("SELECT cash FROM paper_accounts WHERE id=?", (account_id,)).fetchone()[0]
            self.assertAlmostEqual(post_cash, init_cash + fill_amount - fill_fees, places=4)

    # -------------------------------------------------------------------------
    # Scenario B: paused 账户 + 存量持仓 + 触发风控
    # -------------------------------------------------------------------------
    def test_B_paused_account_with_holdings_has_exit_eligibility_but_no_buy_eligibility(self):
        account_id = "tq_breakout"
        self._insert_lot(account_id, self.code, 1000, 10.0)
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_accounts SET status='paused' WHERE id=?", (account_id,))

            # Invariant: paused account has NO buy eligibility
            active_running_ids = [row["id"] for row in PT._active_account_rows(conn, status="running")]
            self.assertNotIn(account_id, active_running_ids)

            # Invariant: paused account WITH remaining lots DOES have risk exit eligibility
            risk_ids = PT._risk_exit_account_ids(conn)
            self.assertIn(account_id, risk_ids)

        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        res = PT.monitor_risk(self.day)
        self.assertEqual(len(res.get("orders", [])), 1)
        self.assertEqual(res["orders"][0]["status"], "filled")

        with PT._db() as conn:
            lots = conn.execute("SELECT remaining_qty FROM paper_position_lots WHERE account_id=?", (account_id,)).fetchall()
            self.assertEqual(lots[0]["remaining_qty"], 0)

            # Invariant: after flattening (remaining_qty == 0), paused account is NO LONGER eligible
            risk_ids_after = PT._risk_exit_account_ids(conn)
            self.assertNotIn(account_id, risk_ids_after)

    # -------------------------------------------------------------------------
    # Scenario C: archived / out-of-cycle 账户 + 存量持仓 + 触发风控
    # -------------------------------------------------------------------------
    def test_C_archived_out_of_cycle_account_executes_risk_exit(self):
        account_id = "main_force_top10"
        self._insert_lot(account_id, self.code, 1000, 10.0, cycle_id=1)
        with PT._db(immediate=True) as conn:
            # Out of cycle: cycle 1 only enables tq_breakout
            conn.execute("UPDATE paper_cycles SET enabled_strategies='[\"tq_breakout\"]' WHERE id=1")
            conn.execute("UPDATE paper_accounts SET status='archived', cycle_id=NULL WHERE id=?", (account_id,))

            # Invariant: out-of-cycle archived account has NO buy eligibility
            active_running_ids = [row["id"] for row in PT._active_account_rows(conn, status="running")]
            self.assertNotIn(account_id, active_running_ids)

            # Invariant: WITH remaining lots, it DOES have risk exit eligibility
            risk_ids = PT._risk_exit_account_ids(conn)
            self.assertIn(account_id, risk_ids)

        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        res = PT.monitor_risk(self.day)
        self.assertEqual(len(res.get("orders", [])), 1)
        self.assertEqual(res["orders"][0]["code"], self.code)
        self.assertEqual(res["orders"][0]["status"], "filled")

        with PT._db() as conn:
            lots = conn.execute("SELECT remaining_qty FROM paper_position_lots WHERE account_id=?", (account_id,)).fetchall()
            self.assertEqual(lots[0]["remaining_qty"], 0)
            self.assertNotIn(account_id, PT._risk_exit_account_ids(conn))

    # -------------------------------------------------------------------------
    # Scenario D: paused / archived 账户 + 0 持仓
    # -------------------------------------------------------------------------
    def test_D_paused_or_archived_account_with_zero_holdings_never_enters_risk_exit(self):
        paused_account = "tq_breakout"
        archived_account = "main_force_top10"
        with PT._db(immediate=True) as conn:
            stamp_p = PT._strategy_stamp(conn, paused_account)
            stamp_a = PT._strategy_stamp(conn, archived_account)

            # Record historical activity so _ensure_cycle treats them as initialized
            cur_p = conn.execute(
                """INSERT INTO paper_orders(
                       account_id, side, code, name, qty, planned_price, filled_price,
                       amount, fees, status, reason, risk_payload, created_at, executed_at,
                       order_type, origin, strategy_id, strategy_version, strategy_checksum, cycle_id
                   ) VALUES (?, 'buy', ?, '测试', 100, 10.0, 10.0, 1000.0, 5.0, 'filled', 'old',
                             '{}', '2026-08-01 09:30:00', '2026-08-01 09:30:00', 'market', 'seed', ?, ?, ?, 1)""",
                (paused_account, self.code, *stamp_p),
            )
            conn.execute(
                """INSERT INTO paper_fills(order_id, account_id, side, code, qty, price, amount, fees, fill_date, quote_at, assumption)
                   VALUES (?, ?, 'buy', ?, 100, 10.0, 1000.0, 5.0, '2026-08-01', '2026-08-01 09:30:00', 'old')""",
                (cur_p.lastrowid, paused_account, self.code),
            )

            cur_a = conn.execute(
                """INSERT INTO paper_orders(
                       account_id, side, code, name, qty, planned_price, filled_price,
                       amount, fees, status, reason, risk_payload, created_at, executed_at,
                       order_type, origin, strategy_id, strategy_version, strategy_checksum, cycle_id
                   ) VALUES (?, 'buy', ?, '测试', 100, 10.0, 10.0, 1000.0, 5.0, 'filled', 'old',
                             '{}', '2026-08-01 09:30:00', '2026-08-01 09:30:00', 'market', 'seed', ?, ?, ?, 1)""",
                (archived_account, self.code, *stamp_a),
            )
            conn.execute(
                """INSERT INTO paper_fills(order_id, account_id, side, code, qty, price, amount, fees, fill_date, quote_at, assumption)
                   VALUES (?, ?, 'buy', ?, 100, 10.0, 1000.0, 5.0, '2026-08-01', '2026-08-01 09:30:00', 'old')""",
                (cur_a.lastrowid, archived_account, self.code),
            )

            # Update statuses: one paused, one archived and out of cycle
            conn.execute("UPDATE paper_accounts SET status='paused' WHERE id=?", (paused_account,))
            conn.execute("UPDATE paper_accounts SET status='archived', cycle_id=NULL WHERE id=?", (archived_account,))
            conn.execute("DELETE FROM paper_position_lots WHERE account_id IN (?, ?)", (paused_account, archived_account))
            PT._sync_positions(conn, asof_day=self.day)

            # Invariant: 0 holdings + paused/archived -> strictly NOT eligible for risk exit
            risk_ids = PT._risk_exit_account_ids(conn)
            self.assertNotIn(paused_account, risk_ids)
            self.assertNotIn(archived_account, risk_ids)

        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)
        res = PT.monitor_risk(self.day)
        emitted_accounts = {o.get("account_id") for o in res.get("orders", [])}
        self.assertNotIn(paused_account, emitted_accounts)
        self.assertNotIn(archived_account, emitted_accounts)

        with PT._db() as conn:
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE account_id IN (?, ?) AND side='sell'",
                (paused_account, archived_account),
            ).fetchall()
            self.assertEqual(len(orders), 0)

    # -------------------------------------------------------------------------
    # Scenario E: 多 lot 持仓场景（FIFO 扣减与不超扣）
    # -------------------------------------------------------------------------
    def test_E_multi_lot_fifo_consumption_without_over_deduction(self):
        account_id = "tq_breakout"
        # Lot 1: 200 shares @ 10.0, acquired 2026-09-07
        # Lot 2: 300 shares @ 12.0, acquired 2026-09-08
        # Total = 500 shares.
        lot1_id = self._insert_lot(account_id, self.code, 200, 10.0, acquired_at="2026-09-07 10:00:00")
        lot2_id = self._insert_lot(account_id, self.code, 300, 12.0, acquired_at="2026-09-08 10:00:00")

        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)
        res = PT.monitor_risk(self.day)
        self.assertEqual(len(res.get("orders", [])), 1)
        self.assertEqual(res["orders"][0]["qty"], 500)

        with PT._db() as conn:
            lot1 = conn.execute("SELECT remaining_qty FROM paper_position_lots WHERE id=?", (lot1_id,)).fetchone()
            lot2 = conn.execute("SELECT remaining_qty FROM paper_position_lots WHERE id=?", (lot2_id,)).fetchone()
            self.assertEqual(lot1["remaining_qty"], 0)
            self.assertEqual(lot2["remaining_qty"], 0)

            # Order realized pnl check: FIFO cost = 200*10 + 300*12 = 5600.0
            order = conn.execute("SELECT realized_pnl, amount, fees FROM paper_orders WHERE account_id=? AND side='sell'", (account_id,)).fetchone()
            expected_pnl = order["amount"] - 5600.0 - order["fees"]
            self.assertAlmostEqual(order["realized_pnl"], expected_pnl, places=4)

    # -------------------------------------------------------------------------
    # Scenario F: 部分成交（partial fill / trim）保留剩余持仓与退出资格
    # -------------------------------------------------------------------------
    def test_F_partial_fill_deducts_accurately_and_preserves_exit_eligibility(self):
        account_id = "tq_breakout"
        # 1000 shares @ 10.0. Price 9.0 (ret -10%), pct -5.0% (not crash tape) -> partial 35% = 300 shares
        lot_id = self._insert_lot(account_id, self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-5.0)

        res = PT.monitor_risk(self.day)
        self.assertEqual(len(res.get("orders", [])), 1)
        self.assertEqual(res["orders"][0]["qty"], 300)

        with PT._db() as conn:
            lot = conn.execute("SELECT remaining_qty FROM paper_position_lots WHERE id=?", (lot_id,)).fetchone()
            self.assertEqual(lot["remaining_qty"], 700)

            pos = conn.execute("SELECT qty FROM paper_positions WHERE account_id=? AND code=?", (account_id, self.code)).fetchone()
            self.assertEqual(pos["qty"], 700)

            # Invariant: Account still retains risk exit eligibility
            self.assertIn(account_id, PT._risk_exit_account_ids(conn))

    # -------------------------------------------------------------------------
    # Scenario G: 完全成交后归零，后续扫描不重复生成卖单
    # -------------------------------------------------------------------------
    def test_G_full_fill_clears_exposure_and_subsequent_review_does_not_duplicate(self):
        account_id = "tq_breakout"
        self._insert_lot(account_id, self.code, 500, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        # Pass 1: exits completely
        res1 = PT.monitor_risk(self.day)
        self.assertEqual(len(res1.get("orders", [])), 1)
        self.assertEqual(res1["orders"][0]["status"], "filled")

        # Clear audit marker for pass 2 to simulate next review pass
        self._clear_scan_run_state()

        res2 = PT.monitor_risk(self.day)
        self.assertEqual(len(res2.get("orders", [])), 0)

        with PT._db() as conn:
            order_count = conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND code=? AND side='sell'",
                (account_id, self.code),
            ).fetchone()[0]
            self.assertEqual(order_count, 1, "Must not generate duplicate sell orders")

    # -------------------------------------------------------------------------
    # Scenario H: runner 幂等性（同一分钟内重复调用不重复扣减）
    # -------------------------------------------------------------------------
    def test_H_runner_idempotency_within_same_minute(self):
        account_id = "tq_breakout"
        self._insert_lot(account_id, self.code, 500, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        res1 = PT.monitor_risk(self.day)
        self.assertEqual(len(res1.get("orders", [])), 1)

        # Call again without clearing scan marker
        res2 = PT.monitor_risk(self.day)
        self.assertEqual(res2.get("status"), "already_scanned")
        self.assertEqual(len(res2.get("orders", [])), 0)

        with PT._db() as conn:
            fills = conn.execute("SELECT COUNT(*) FROM paper_fills WHERE account_id=? AND side='sell'", (account_id,)).fetchone()[0]
            self.assertEqual(fills, 1)

    # -------------------------------------------------------------------------
    # Scenario I: 已存在未完成/重试中卖单（pending sell）抑制重复下单
    # -------------------------------------------------------------------------
    def test_I_unfilled_limit_down_cooldown_suppresses_duplicate_orders(self):
        account_id = "tq_breakout"
        self._insert_lot(account_id, self.code, 500, 10.0)
        # Set limit down price
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-9.6)

        # Pass 1: hits limit down, creates unfilled_limit_down order
        res1 = PT.monitor_risk(self.day)
        self.assertEqual(len(res1.get("orders", [])), 1)
        self.assertEqual(res1["orders"][0]["status"], "unfilled_limit_down")

        # Clear scan marker to simulate subsequent scan pass in cooldown
        self._clear_scan_run_state()

        res2 = PT.monitor_risk(self.day)
        self.assertEqual(len(res2.get("orders", [])), 1)
        self.assertEqual(res2["orders"][0]["status"], "unfilled_limit_down_wait")

        with PT._db() as conn:
            orders = conn.execute("SELECT status FROM paper_orders WHERE account_id=? AND side='sell'", (account_id,)).fetchall()
            self.assertEqual(len(orders), 1, "Only 1 order created in DB during cooldown")
            self.assertEqual(orders[0]["status"], "unfilled_limit_down")

    # -------------------------------------------------------------------------
    # Scenario J: 冻结语义与容量限制互不干扰（PAPER_ENTRY_FREEZE 不阻断风控退出）
    # -------------------------------------------------------------------------
    def test_J_entry_freeze_env_does_not_block_risk_exit(self):
        account_id = "tq_breakout"
        self._insert_lot(account_id, self.code, 500, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        with mock.patch.dict(os.environ, {PT.ENTRY_FREEZE_ENV: "true"}):
            # Assert freeze is active for buy side
            freeze = PT._entry_freeze_status(self.day)
            self.assertTrue(freeze["enabled"])

            res = PT.monitor_risk(self.day)
            self.assertEqual(len(res.get("orders", [])), 1)
            self.assertEqual(res["orders"][0]["status"], "filled")

            with PT._db() as conn:
                lots = conn.execute("SELECT remaining_qty FROM paper_position_lots WHERE account_id=?", (account_id,)).fetchall()
                self.assertEqual(lots[0]["remaining_qty"], 0)

    # -------------------------------------------------------------------------
    # Scenario K: 资金释放一致性（净回款准确归还共享资金/账户资金）
    # -------------------------------------------------------------------------
    def test_K_cash_release_consistency_matches_order_fill_net_proceeds(self):
        account_id = "tq_breakout"
        self._insert_lot(account_id, self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        with PT._db() as conn:
            cash_before = conn.execute("SELECT cash FROM paper_accounts WHERE id=?", (account_id,)).fetchone()[0]

        PT.monitor_risk(self.day)

        with PT._db() as conn:
            fill = conn.execute("SELECT amount, fees FROM paper_fills WHERE account_id=? AND side='sell'", (account_id,)).fetchone()
            self.assertIsNotNone(fill)
            net_proceeds = fill["amount"] - fill["fees"]

            cash_after = conn.execute("SELECT cash FROM paper_accounts WHERE id=?", (account_id,)).fetchone()[0]
            self.assertAlmostEqual(cash_after, cash_before + net_proceeds, places=4)

    # -------------------------------------------------------------------------
    # Scenario L: 槽位占位互不干扰（风控退出 SELL 不占用买入槽位）
    # -------------------------------------------------------------------------
    def test_L_risk_exit_sell_orders_do_not_occupy_buy_slots(self):
        account_id = "tq_breakout"
        self._insert_lot(account_id, self.code, 500, 10.0)

        with PT._db() as conn:
            # Slot occupancy before exit
            slots_before = PT._pending_position_slots(conn)
            stamp = PT._strategy_stamp(conn, account_id)

            # Insert an execution_retry sell order for code_b (not currently held)
            conn.execute(
                """INSERT INTO paper_orders(
                       account_id, origin, side, code, name, qty, planned_price, status, reason, created_at,
                       risk_payload, strategy_id, strategy_version, strategy_checksum, cycle_id
                   ) VALUES (?, 'strategy', 'sell', ?, '测试', 500, 9.0, 'execution_retry', '重试', '2026-09-10 14:50:00',
                             '{}', ?, ?, ?, ?)""",
                (account_id, self.code_b, *stamp, 1),
            )

            # Slot occupancy after sell order: must NOT include (account_id, code_b)
            slots_after = PT._pending_position_slots(conn)
            self.assertEqual(slots_before, slots_after, "SELL orders must never occupy buy position slots")
            self.assertNotIn((account_id, self.code_b), slots_after)

    # -------------------------------------------------------------------------
    # Scenario M: 强一致性断言（golden snapshot state transition across 6 tables）
    # -------------------------------------------------------------------------
    def test_M_strong_consistency_state_transition_golden_snapshot(self):
        account_id = "tq_breakout"
        lot_id = self._insert_lot(account_id, self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        # Snapshot before
        with PT._db() as conn:
            acct_before = dict(conn.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone())
            pos_before = [dict(r) for r in conn.execute("SELECT * FROM paper_positions WHERE account_id=?", (account_id,)).fetchall()]
            lot_before = dict(conn.execute("SELECT * FROM paper_position_lots WHERE id=?", (lot_id,)).fetchone())
            orders_count_before = conn.execute("SELECT COUNT(*) FROM paper_orders WHERE side='sell'").fetchone()[0]
            fills_count_before = conn.execute("SELECT COUNT(*) FROM paper_fills WHERE side='sell'").fetchone()[0]
            reservations_before = conn.execute("SELECT COUNT(*) FROM paper_capital_reservations").fetchone()[0]

        PT.monitor_risk(self.day)

        # Snapshot after
        with PT._db() as conn:
            acct_after = dict(conn.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone())
            pos_after = [dict(r) for r in conn.execute("SELECT * FROM paper_positions WHERE account_id=?", (account_id,)).fetchall()]
            lot_after = dict(conn.execute("SELECT * FROM paper_position_lots WHERE id=?", (lot_id,)).fetchone())
            orders_after = [dict(r) for r in conn.execute("SELECT * FROM paper_orders WHERE side='sell'").fetchall()]
            fills_after = [dict(r) for r in conn.execute("SELECT * FROM paper_fills WHERE side='sell'").fetchall()]
            reservations_after = conn.execute("SELECT COUNT(*) FROM paper_capital_reservations").fetchone()[0]

            # 1. paper_orders: exactly 1 new sell order
            self.assertEqual(len(orders_after), orders_count_before + 1)
            new_order = orders_after[-1]
            self.assertEqual(new_order["side"], "sell")
            self.assertEqual(new_order["status"], "filled")
            self.assertEqual(new_order["qty"], 1000)

            # 2. paper_fills: exactly 1 new fill matching order
            self.assertEqual(len(fills_after), fills_count_before + 1)
            new_fill = fills_after[-1]
            self.assertEqual(new_fill["order_id"], new_order["id"])
            self.assertEqual(new_fill["qty"], 1000)

            # 3. paper_position_lots: remaining_qty reduced to 0
            self.assertEqual(lot_before["remaining_qty"], 1000)
            self.assertEqual(lot_after["remaining_qty"], 0)

            # 4. paper_accounts: cash increment == fill net proceeds
            net_proceeds = new_fill["amount"] - new_fill["fees"]
            self.assertAlmostEqual(acct_after["cash"] - acct_before["cash"], net_proceeds, places=4)

            # 5. paper_positions: position closed
            self.assertEqual(len(pos_before), 1)
            self.assertEqual(len(pos_after), 0)

            # 6. paper_capital_reservations: zero change
            self.assertEqual(reservations_after, reservations_before)

            # 7. Relational integrity: every fill joins to an existing order
            orphan_fills = conn.execute(
                """SELECT f.id FROM paper_fills f
                   LEFT JOIN paper_orders o ON o.id = f.order_id
                   WHERE o.id IS NULL"""
            ).fetchall()
            self.assertEqual(len(orphan_fills), 0, "No orphan fill without corresponding paper_orders entry")

            # 8. All seed BUY fills join to seed BUY orders
            seed_buy_joins = conn.execute(
                """SELECT f.id, f.order_id, o.side as order_side, o.status as order_status
                   FROM paper_fills f
                   JOIN paper_orders o ON o.id = f.order_id
                   WHERE f.side = 'buy'"""
            ).fetchall()
            self.assertGreaterEqual(len(seed_buy_joins), 1)
            for r in seed_buy_joins:
                self.assertEqual(r["order_side"], "buy", "Seed BUY fill must join to BUY order")
                self.assertEqual(r["order_status"], "filled", "Seed BUY order must be terminal filled")

            # 9. All risk-exit SELL fills join to SELL orders
            risk_sell_joins = conn.execute(
                """SELECT f.id, f.order_id, o.side as order_side, o.status as order_status
                   FROM paper_fills f
                   JOIN paper_orders o ON o.id = f.order_id
                   WHERE f.side = 'sell'"""
            ).fetchall()
            self.assertEqual(len(risk_sell_joins), 1)
            self.assertEqual(risk_sell_joins[0]["order_side"], "sell", "Risk-exit SELL fill must join to SELL order")
            self.assertEqual(risk_sell_joins[0]["order_status"], "filled")

            # 10. Invariant: no fill may join to an opposite-side order
            opposite_side_fills = conn.execute(
                """SELECT f.id, f.side as f_side, o.side as o_side
                   FROM paper_fills f
                   JOIN paper_orders o ON o.id = f.order_id
                   WHERE (f.side = 'buy' AND o.side = 'sell') OR (f.side = 'sell' AND o.side = 'buy')"""
            ).fetchall()
            self.assertEqual(len(opposite_side_fills), 0, "No fill may be linked to an opposite-side order")

            # 11. Strict match on account_id, code, and side
            mismatches = conn.execute(
                """SELECT f.id, f.side as f_side, o.side as o_side, f.account_id as f_acct, o.account_id as o_acct, f.code as f_code, o.code as o_code
                   FROM paper_fills f
                   JOIN paper_orders o ON o.id = f.order_id
                   WHERE f.side != o.side OR f.account_id != o.account_id OR f.code != o.code"""
            ).fetchall()
            self.assertEqual(len(mismatches), 0, "Fills must strictly match joined orders on side, account_id, and code")

    # -------------------------------------------------------------------------
    # Scenario N: 容错与异常回滚（中途抛异常不得污染持仓 lot）
    # -------------------------------------------------------------------------
    def test_N_failure_branch_rolls_back_savepoint_without_corrupting_lots(self):
        account_id = "tq_breakout"
        lot_id = self._insert_lot(account_id, self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        with PT._db() as conn:
            init_cash = conn.execute("SELECT cash FROM paper_accounts WHERE id=?", (account_id,)).fetchone()[0]

        # Inject failure during cash credit inside the savepoint
        orig_credit = PT._credit_shared_cash

        def mock_credit_failure(*args, **kwargs):
            raise RuntimeError("Injected simulation ledger failure")

        PT._credit_shared_cash = mock_credit_failure
        try:
            res = PT.monitor_risk(self.day)
            self.assertEqual(len(res.get("orders", [])), 1)
            self.assertEqual(res["orders"][0]["status"], "execution_retry")

            with PT._db() as conn:
                lot = conn.execute("SELECT remaining_qty FROM paper_position_lots WHERE id=?", (lot_id,)).fetchone()
                # Invariant: lot must NOT be dirty or reduced when execution failed
                self.assertEqual(lot["remaining_qty"], 1000, "Unfilled lot must not be mutated on failure")

                # No filled sell order or fill row should exist
                filled_orders = conn.execute("SELECT COUNT(*) FROM paper_orders WHERE side='sell' AND status='filled'").fetchone()[0]
                self.assertEqual(filled_orders, 0)
                fills = conn.execute("SELECT COUNT(*) FROM paper_fills WHERE side='sell'").fetchone()[0]
                self.assertEqual(fills, 0)

                # Cash must remain exactly unchanged
                post_cash = conn.execute("SELECT cash FROM paper_accounts WHERE id=?", (account_id,)).fetchone()[0]
                self.assertAlmostEqual(post_cash, init_cash, places=4, msg="Cash must not be modified on execution rollback")
        finally:
            PT._credit_shared_cash = orig_credit

    # -------------------------------------------------------------------------
    # Scenario O: Top-level production entrypoint run_slot("risk", ...) golden path
    # -------------------------------------------------------------------------
    def test_O_golden_run_slot_risk_production_entrypoint(self):
        account_id = "tq_breakout"
        self._insert_lot(account_id, self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        with mock.patch.object(PT, "request_risk_snapshot_refresh", return_value={"status": "skipped"}):
            res = PT.run_slot("risk", self.day, force=True)
            self.assertEqual(res.get("status"), "completed")
            self.assertEqual(res.get("slot"), "risk")

            with PT._db() as conn:
                orders = conn.execute("SELECT side, code, qty, status FROM paper_orders WHERE account_id=? AND side='sell'", (account_id,)).fetchall()
                self.assertEqual(len(orders), 1)
                self.assertEqual(orders[0]["status"], "filled")
                self.assertEqual(orders[0]["qty"], 1000)

                lots = conn.execute("SELECT remaining_qty FROM paper_position_lots WHERE account_id=?", (account_id,)).fetchall()
                self.assertEqual(lots[0]["remaining_qty"], 0)


class TestRiskScanLifecycleProductionPath(PaperRiskExitProductionPathTestCase):
    """R16 生产回归：风险扫描身份必须是 cycle-owned 的 durable 事实。

    RISK-SCAN-P1  同 cycle + 同 asof + 同分钟：第二次 already_scanned，订单/成交不重复
    RISK-SCAN-P2  跨周期同分钟：cycle 9 不得被 cycle 8 的完成态抑制
    RISK-SCAN-P3  同 cycle + 同分钟 + 不同 asof：两者都能被 claim
    RISK-SCAN-P4  quote I/O 期间周期 rollover：fail closed，且不产生任何新周期副作用
    RISK-SCAN-P5  失败 → 重试：failed(attempt=1) → running(attempt=2) → completed
    RISK-SCAN-P6  跨分钟边界：最终只有**一个**身份行，且 status=failed（无 orphan running）
    """

    def _scan_runs(self):
        with PT._db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT cycle_id,asof_date,scan_minute,status,attempt"
                " FROM paper_risk_scan_runs ORDER BY id").fetchall()]

    def _active_cycle_id(self):
        with PT._db() as conn:
            return int(conn.execute(
                "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
                " ORDER BY id DESC LIMIT 1").fetchone()[0])

    def _add_cycle_and_activate(self, stamp="2026-09-10 14:50:30"):
        with PT._db(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
                "updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
                (f"c-{stamp}", "running", 100000.0, "shared_pool", stamp, stamp, stamp),
            )
            cycle_id = int(cur.lastrowid)
            conn.execute("UPDATE paper_cycles SET status='running' WHERE id=?", (cycle_id,))
            conn.execute(
                "UPDATE paper_accounts SET cycle_id=?, status='running' WHERE id=?",
                (cycle_id, "tq_breakout"),
            )
            return cycle_id

    def _sell_fills(self):
        with PT._db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM paper_fills WHERE side='sell'").fetchone()[0]

    def _sell_orders(self, cycle_id=None):
        with PT._db() as conn:
            sql = "SELECT id,cycle_id FROM paper_orders WHERE side='sell'"
            params = ()
            if cycle_id is not None:
                sql += " AND cycle_id=?"
                params = (cycle_id,)
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ── P1 ─────────────────────────────────────────────────────────────────
    def test_RISK_SCAN_P1_same_identity_dedupes_without_duplicate_side_effects(self):
        self._insert_lot("tq_breakout", self.code, 500, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        first = PT.monitor_risk(self.day)
        second = PT.monitor_risk(self.day)

        self.assertEqual(len(first.get("orders", [])), 1)
        self.assertEqual(second.get("status"), "already_scanned")
        self.assertEqual(second.get("orders"), [])
        self.assertEqual(self._sell_fills(), 1, "同一身份产生了重复成交")

        runs = self._scan_runs()
        self.assertEqual(len(runs), 1, "同一身份产生了多行 scan run")
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(int(runs[0]["attempt"]), 1)

    # ── P2 ─────────────────────────────────────────────────────────────────
    def test_RISK_SCAN_P2_cross_cycle_same_minute_is_not_suppressed(self):
        c8 = self._active_cycle_id()
        self._insert_lot("tq_breakout", self.code, 500, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)
        first = PT.monitor_risk(self.day)
        self.assertEqual(len(first.get("orders", [])), 1)

        # 同一分钟内翻周期，cycle 9 同样持有该票并同样触发风控
        c9 = self._add_cycle_and_activate()
        self._insert_lot("tq_breakout", self.code, 500, 10.0, cycle_id=c9)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        second = PT.monitor_risk(self.day)
        self.assertNotEqual(
            second.get("status"), "already_scanned",
            "cycle 9 被 cycle 8 的同分钟完成态抑制 —— 新周期持仓一次风控都没跑",
        )
        runs = self._scan_runs()
        cycles = sorted(int(r["cycle_id"]) for r in runs)
        self.assertEqual(cycles, [c8, c9], "两个周期未各自留下 scan run")
        self.assertTrue(all(r["status"] == "completed" for r in runs))

    # ── P3 ─────────────────────────────────────────────────────────────────
    def test_RISK_SCAN_P3_different_asof_same_minute_is_claimable(self):
        self._insert_lot("tq_breakout", self.code, 500, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)
        PT.monitor_risk(self.day)

        other_day = dt.date(2026, 9, 11)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)
        PT.monitor_risk(other_day)

        runs = self._scan_runs()
        self.assertEqual(len(runs), 2, "同分钟不同 asof 被错误抑制")
        self.assertEqual(
            sorted(r["asof_date"] for r in runs),
            sorted([self.day.isoformat(), other_day.isoformat()]),
        )

    # ── P4 ─────────────────────────────────────────────────────────────────
    def test_RISK_SCAN_P4_cycle_rollover_during_quote_fetch_fails_closed(self):
        c8 = self._active_cycle_id()
        self._insert_lot("tq_breakout", self.code, 500, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        state = {"c9": None}
        base_quotes = PT._quotes

        def rollover(codes, asof_date=None):
            if state["c9"] is None:
                c9 = self._add_cycle_and_activate()
                self._insert_lot("tq_breakout", self.code, 500, 10.0, cycle_id=c9)
                state["c9"] = c9
            return base_quotes(codes, asof_date=asof_date)

        with mock.patch.object(PT, "_quotes", side_effect=rollover):
            with self.assertRaises(PT.PRSS.RiskScanCycleChanged):
                PT.monitor_risk(self.day)

        c9 = state["c9"]
        self.assertIsNotNone(c9, "夹具未触发周期 rollover")
        self.assertEqual(self._sell_orders(c9), [], "旧快照操作了新周期的订单")
        self.assertEqual(self._sell_fills(), 0, "周期 rollover 后仍然成交")
        with PT._db() as conn:
            c9_lots = conn.execute(
                "SELECT COALESCE(SUM(remaining_qty),0) FROM paper_position_lots WHERE cycle_id=?",
                (c9,)).fetchone()[0]
            c9_reviews = conn.execute(
                "SELECT COUNT(*) FROM paper_position_reviews WHERE cycle_id=?", (c9,)).fetchone()[0]
        self.assertEqual(int(c9_lots), 500, "新周期 lot 被旧周期的扫描消耗")
        self.assertEqual(c9_reviews, 0, "旧周期的扫描写了新周期的 position review")

        runs = self._scan_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "failed", "cycle changed 未 fail closed")
        self.assertEqual(int(runs[0]["cycle_id"]), c8)

    # ── P5 ─────────────────────────────────────────────────────────────────
    def test_RISK_SCAN_P5_failure_is_retryable_with_attempt_increment(self):
        self._insert_lot("tq_breakout", self.code, 500, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        boom = RuntimeError("injected provider failure")
        with mock.patch.object(PT, "_quotes", side_effect=boom):
            with self.assertRaises(RuntimeError):
                PT.monitor_risk(self.day)

        runs = self._scan_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(int(runs[0]["attempt"]), 1)

        # 同一身份直接重试：failed → running(attempt=2) → completed
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)
        result = PT.monitor_risk(self.day)
        self.assertEqual(len(result.get("orders", [])), 1, "重试未能真正执行")
        runs = self._scan_runs()
        self.assertEqual(len(runs), 1, "重试产生了第二个身份")
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(int(runs[0]["attempt"]), 2, "重试未递增 attempt")

    # ── P6 ─────────────────────────────────────────────────────────────────
    def test_RISK_SCAN_P6_minute_boundary_leaves_no_orphan_running(self):
        self._insert_lot("tq_breakout", self.code, 500, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        # 模拟 wrapper 与执行阶段跨分钟：时钟每次调用都前进一分钟。
        ticks = [dt.datetime(2026, 9, 10, 14, 50, 59)]

        def ticking():
            current = ticks[-1]
            ticks.append(current + dt.timedelta(minutes=1))
            return current

        class _Date(dt.date):
            @classmethod
            def today(cls):
                return dt.date(2026, 9, 10)

        class _Datetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                value = ticking()
                return value if tz is None else value.replace(tzinfo=tz)

        class _DT:
            date = _Date
            datetime = _Datetime
            timedelta = dt.timedelta

        boom = RuntimeError("injected failure at minute boundary")
        with mock.patch.object(PT, "dt", _DT), \
                mock.patch.object(PT, "_quotes", side_effect=boom):
            with self.assertRaises(RuntimeError):
                PT.monitor_risk(self.day)

        runs = self._scan_runs()
        self.assertEqual(len(runs), 1, "跨分钟边界产生了第二个 scan 身份")
        self.assertEqual(runs[0]["status"], "failed", "留下了 orphan running")
        self.assertFalse(
            any(r["status"] == "running" for r in runs),
            "存在没有对应 failed 转换的 running 身份",
        )


if __name__ == "__main__":
    unittest.main()
