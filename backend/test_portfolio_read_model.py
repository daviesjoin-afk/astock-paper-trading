# -*- coding: utf-8 -*-
"""R22 permanent contracts for the cycle/as-of bounded portfolio read model."""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_portfolio_read_model as P  # noqa: E402
import paper_position_read_model as PPRM  # noqa: E402
import paper_trading as PT  # noqa: E402

ACCOUNT = next(iter(PT.ACCOUNT_SPECS))
CODE = "600519"
DAY = dt.date(2026, 9, 20)
NEXT = DAY + dt.timedelta(days=1)


class PortfolioReadModelContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self._patchers = (
            mock.patch.object(PT, "DB_PATH", self.path),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
        )
        for patcher in self._patchers:
            patcher.start()
        PT.init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.cycle100 = self._cycle("r22-c100", "paused")
        self.cycle101 = self._cycle("r22-c101", "running")
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=? WHERE id=?", (self.cycle100, ACCOUNT)
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for patcher in reversed(self._patchers):
            patcher.stop()
        self.tmp.cleanup()

    def _cycle(self, key, status):
        created = f"{DAY.isoformat()} 09:00:00"
        return int(self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
            "updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
            (key, status, 100000.0, "shared_pool", created, created,
             created if status == "running" else None),
        ).lastrowid)

    def _order_and_fill(self, *, cycle_id, side, qty, price, fill_date,
                        verified=True, realized_pnl=None, fees=5.0):
        amount = qty * price
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        order_id = int(self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
            "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_status,execution_verified,realized_pnl) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, side, CODE, "测试股", qty, price, price, amount, fees, "filled",
             "r22-test", "{}", f"{fill_date} 09:30:00", f"{fill_date} 09:30:01",
             "market", "seed", *stamp, cycle_id,
             "verified" if verified else "unknown", 1 if verified else 0, realized_pnl),
        ).lastrowid)
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, ACCOUNT, side, CODE, qty, price, amount, fees, fill_date,
             f"{fill_date} 09:30:00", "r22-test"),
        )
        return order_id

    def _lot(self, cycle_id, qty, cost, *, acquired_at=None, remaining_qty=None,
             source_order_id=None, account_id=ACCOUNT):
        acquired_at = acquired_at or f"{DAY.isoformat()} 10:00:00"
        remaining_qty = qty if remaining_qty is None else remaining_qty
        return int(self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
            "remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,"
            "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, account_id, CODE, "测试股", "测试", qty, remaining_qty, cost,
             acquired_at, NEXT.isoformat(), "stock_t1", source_order_id, 1, 1),
        ).lastrowid)

    def _portfolio(self, cycle_id, asof_day=DAY, **kwargs):
        return P.portfolio_for_cycle(self.conn, cycle_id, asof_day, **kwargs)

    def test_port1_context_requires_explicit_cycle_and_asof(self):
        with self.assertRaises(ValueError):
            P.PortfolioReadContext(cycle_id=None, asof_day=DAY)
        with self.assertRaises(ValueError):
            P.PortfolioReadContext(cycle_id=self.cycle100, asof_day=None)
        context = P.PortfolioReadContext(cycle_id=str(self.cycle100), asof_day=DAY.isoformat())
        self.assertEqual(context.cycle_id, self.cycle100)
        self.assertEqual(context.asof_day, DAY)

    def test_port1_explicit_cycle_owns_quantity(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        rows = P.positions_for_context(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertEqual([row["qty"] for row in rows], [100])
        self.assertEqual(rows[0]["cost"], 10.0)

    def test_port2_later_cycle_cannot_mutate_historical_quantity(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        before = P.positions_for_context(self.conn, P.PortfolioReadContext(self.cycle100, DAY))
        self._lot(self.cycle101, 999, 20.0, acquired_at=f"{DAY.isoformat()} 11:00:00")
        self.conn.commit()
        after = P.positions_for_context(self.conn, P.PortfolioReadContext(self.cycle100, DAY))
        self.assertEqual([row["qty"] for row in before], [row["qty"] for row in after])

    def test_port3_later_cycle_cannot_mutate_historical_cost(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        before = P.positions_for_context(self.conn, P.PortfolioReadContext(self.cycle100, DAY))[0]
        self._order_and_fill(cycle_id=self.cycle101, side="buy", qty=100,
                             price=99.0, fill_date=DAY.isoformat())
        self.conn.commit()
        after = P.positions_for_context(self.conn, P.PortfolioReadContext(self.cycle100, DAY))[0]
        self.assertEqual(before["cost"], after["cost"])
        self.assertEqual(before["display_cost"], after["display_cost"])

    def test_port4_future_fill_is_excluded_by_asof(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        before = P.positions_for_context(self.conn, P.PortfolioReadContext(self.cycle100, DAY))[0]
        self._lot(self.cycle100, 50, 11.0, acquired_at=f"{NEXT.isoformat()} 10:00:00")
        self._order_and_fill(cycle_id=self.cycle100, side="sell", qty=25,
                             price=12.0, fill_date=NEXT.isoformat())
        self.conn.commit()
        after = P.positions_for_context(self.conn, P.PortfolioReadContext(self.cycle100, DAY))[0]
        self.assertEqual(before["qty"], 100)
        self.assertEqual(after["qty"], before["qty"])

    def test_port4d_lot_economic_date_comes_from_fill_date(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        # Production _record_lot uses wall-clock _now(); the fill_date is the
        # economic date that must bound a historical read.
        self._lot(self.cycle100, 100, 10.0,
                  acquired_at=f"{NEXT.isoformat()} 10:00:00", source_order_id=buy)
        self.conn.commit()
        rows = P.positions_for_context(self.conn, P.PortfolioReadContext(self.cycle100, DAY))
        self.assertEqual([row["qty"] for row in rows], [100])

    def test_port4e_future_fill_date_is_excluded_even_when_acquired_at_is_early(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=NEXT.isoformat())
        self._lot(self.cycle100, 100, 10.0,
                  acquired_at=f"{DAY.isoformat()} 10:00:00", source_order_id=buy)
        self.conn.commit()
        rows = P.positions_for_context(self.conn, P.PortfolioReadContext(self.cycle100, DAY))
        self.assertEqual(rows, [])

    def test_port4f_explicit_exposure_fails_closed_on_unknown_quantity(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self._order_and_fill(cycle_id=self.cycle100, side="sell", qty=50,
                             price=12.0, fill_date=DAY.isoformat(), verified=False)
        self.conn.commit()
        with self.assertRaises(P.PortfolioReadUnavailable):
            PT._shared_account_exposure(
                self.conn, {CODE: {"price": 10.0}}, DAY, cycle_id=self.cycle100,
            )

    def test_port4g_risk_positions_follow_economic_asof(self):
        historical = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                          price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0,
                  acquired_at=f"{NEXT.isoformat()} 10:00:00", source_order_id=historical)
        future = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=50,
                                      price=11.0, fill_date=NEXT.isoformat())
        self._lot(self.cycle100, 50, 11.0,
                  acquired_at=f"{DAY.isoformat()} 10:00:00", source_order_id=future)
        self.conn.commit()
        positions = P.risk_positions_for_context(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertEqual([row["qty"] for row in positions], [100])

    def test_port4h_risk_positions_fail_closed_on_unknown_quantity(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self._order_and_fill(cycle_id=self.cycle100, side="sell", qty=50,
                             price=12.0, fill_date=DAY.isoformat(), verified=False)
        self.conn.commit()
        with self.assertRaises(P.PortfolioReadUnavailable):
            P.risk_positions_for_context(
                self.conn, P.PortfolioReadContext(self.cycle100, DAY)
            )

    def test_port4i_risk_positions_exclude_future_runtime_state(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.execute(
            "INSERT INTO paper_position_risk_state(cycle_id,account_id,code,peak_price,"
            "take_stage,opened_order_id,initialized_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.cycle100, ACCOUNT, CODE, 12.5, 2, buy,
             f"{DAY.isoformat()} 10:00:00", f"{DAY.isoformat()} 10:00:00"),
        )
        self.conn.commit()
        positions = P.risk_positions_for_context(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertAlmostEqual(positions[0]["peak_price"], 12.5)
        self.assertEqual(positions[0]["take_stage"], 2)
        self.conn.execute(
            "UPDATE paper_position_risk_state SET peak_price=?,take_stage=?,updated_at=?"
            " WHERE cycle_id=? AND account_id=? AND code=?",
            (99.0, 3, f"{NEXT.isoformat()} 10:00:00", self.cycle100, ACCOUNT, CODE),
        )
        self.conn.commit()
        positions = P.risk_positions_for_context(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertAlmostEqual(positions[0]["peak_price"], 10.0)
        self.assertIsNone(positions[0]["take_stage"])
        self.assertIsNone(positions[0]["episode_opened_order_id"])
    def test_port4j_closed_source_less_lot_does_not_poison_risk_reads(self):
        self._lot(self.cycle100, 100, 10.0)
        self._order_and_fill(
            cycle_id=self.cycle100, side="sell", qty=100, price=11.0,
            fill_date=DAY.isoformat(), fees=0.0,
        )
        self.conn.commit()
        positions = P.risk_positions_for_context(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertEqual(positions, [])
    def test_port4k_source_order_identity_must_match_lot(self):
        other_cycle_order = self._order_and_fill(
            cycle_id=self.cycle101, side="buy", qty=100, price=10.0,
            fill_date=DAY.isoformat(),
        )
        self._lot(self.cycle100, 100, 10.0, source_order_id=other_cycle_order)
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        lots, status = P.bounded_lots_with_status(self.conn, context)
        self.assertEqual([row["code"] for row in lots], [CODE])
        self.assertEqual(status, "unknown")
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))
        with self.assertRaises(P.PortfolioReadUnavailable):
            PT._shared_account_exposure(
                self.conn, {CODE: {"price": 10.0}}, DAY, cycle_id=self.cycle100,
            )
    def test_port4b_explicit_exposure_uses_bounded_positions(self):
        future = self._order_and_fill(
            cycle_id=self.cycle100, side="buy", qty=100, price=10.0,
            fill_date=NEXT.isoformat(),
        )
        self._lot(self.cycle100, 100, 10.0, acquired_at=f"{NEXT.isoformat()} 10:00:00",
                  source_order_id=future)
        self.conn.commit()
        positions, _value, _nav, _industries, _codes = PT._shared_account_exposure(
            self.conn, {CODE: {"price": 10.0}}, DAY, cycle_id=self.cycle100,
        )
        self.assertEqual(positions, [])

    def test_port4c_unknown_quantity_keeps_valuation_unknown(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self._order_and_fill(cycle_id=self.cycle100, side="sell", qty=50,
                             price=12.0, fill_date=DAY.isoformat(), verified=False)
        self.conn.commit()
        result = self._portfolio(self.cycle100, valuations={CODE: 10.0})
        self.assertEqual(result["quantity_status"], "unknown")
        self.assertIsNone(result["market_value"])
        self.assertIsNone(result["unrealized_pnl"])
        self.assertIsNone(result["nav"])

    def test_port5_pending_and_unverified_orders_are_excluded(self):
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,status,risk_payload,created_at,"
            "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_status,execution_verified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", CODE, 100, "pending_limit", "{}", f"{DAY.isoformat()} 09:00:00",
             "limit", "strategy", *stamp, self.cycle100, "unknown", 0),
        )
        self.conn.commit()
        rows = P.positions_for_context(self.conn, P.PortfolioReadContext(self.cycle100, DAY))
        self.assertEqual(rows, [])
        realized, status = P.realized_pnl(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertEqual(realized, 0.0)
        self.assertEqual(status, "verified")

    def test_port5c_account_specific_partial_order_schema_fails_closed(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE paper_orders("
                "id INTEGER PRIMARY KEY, cycle_id INTEGER, side TEXT,"
                "status TEXT, executed_at TEXT)"
            )
            value, status = P.realized_pnl(
                conn, P.PortfolioReadContext(self.cycle100, DAY),
                account_id=ACCOUNT,
            )
        finally:
            conn.close()
        self.assertIsNone(value)
        self.assertEqual(status, "unknown")

    def test_port5d_account_specific_partial_activity_schema_fails_closed(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE paper_position_lots(cycle_id INTEGER, acquired_at TEXT)"
            )
            conn.execute(
                "INSERT INTO paper_position_lots VALUES(?,?)",
                (self.cycle100, f"{DAY.isoformat()} 09:00:00"),
            )
            self.assertFalse(P._cycle_has_bounded_activity(
                conn, P.PortfolioReadContext(self.cycle100, DAY), account_id=ACCOUNT,
            ))
        finally:
            conn.close()

    def test_port5h_account_specific_fill_check_requires_order_identity(self):
        # paper_orders 缺 account_id 时，账户级读无法证明“该账户无成交”；
        # _has_any_fill_rows 必须返回 None，cash 不得把初始余额发布成 verified。
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE paper_accounts("
                "id TEXT PRIMARY KEY, initial_cash REAL, cycle_id INTEGER)"
            )
            conn.execute(
                "INSERT INTO paper_accounts VALUES(?,?,?)",
                (ACCOUNT, 100000.0, self.cycle100),
            )
            conn.execute(
                "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, created_at TEXT)"
            )
            conn.execute(
                "INSERT INTO paper_cycles VALUES(?,?)",
                (self.cycle100, f"{DAY.isoformat()} 09:00:00"),
            )
            # R23：账户级 initial capital 现在必须有真实的 bounded attachment
            # 证据，不能再靠 cycle creation 推断。这里给出匹配且 <= asof 的
            # attachment 行，让非空前提仍然成立。
            conn.execute(
                "CREATE TABLE paper_parameter_versions("
                "cycle_id INTEGER, account_id TEXT, effective_date TEXT)"
            )
            conn.execute(
                "INSERT INTO paper_parameter_versions VALUES(?,?,?)",
                (self.cycle100, ACCOUNT, DAY.isoformat()),
            )
            conn.execute("CREATE TABLE paper_fills(order_id INTEGER, fill_date TEXT)")
            conn.execute(
                "CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, cycle_id INTEGER)"
            )
            context = P.PortfolioReadContext(self.cycle100, DAY)
            # 非空门禁：账户确实挂在本周期且 attachment 可证明，
            # 否则本测试区分不了两条路径。
            self.assertIsNotNone(P._cycle_initial(conn, context, account_id=ACCOUNT))
            self.assertIsNone(P._has_any_fill_rows(conn, context, account_id=ACCOUNT))
            self.assertEqual(
                P.cash(conn, context, account_id=ACCOUNT), (None, "unknown")
            )
        finally:
            conn.close()

    def test_port6_projection_corruption_cannot_override_authority(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.execute(
            "INSERT OR REPLACE INTO paper_positions(account_id,code,name,industry,qty,cost,"
            "entry_date,available_date,asset_type,peak_price,take_stage) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, CODE, "测试股", "测试", 999, 999.0, DAY.isoformat(),
             NEXT.isoformat(), "stock_t1", 999.0, 0),
        )
        self.conn.commit()
        rows = P.positions_for_context(self.conn, P.PortfolioReadContext(self.cycle100, DAY))
        self.assertEqual(rows[0]["qty"], 100)

    def test_port6b_remaining_qty_is_not_historical_authority(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, remaining_qty=999, source_order_id=buy)
        self.conn.commit()
        rows = P.positions_for_context(self.conn, P.PortfolioReadContext(self.cycle100, DAY))
        self.assertEqual(rows[0]["qty"], 100)

    def test_port3c_buy_order_without_fill_blocks_display_cash_flow(self):
        first = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                     price=20.0, fill_date=DAY.isoformat(), fees=0.0)
        self._lot(self.cycle100, 100, 20.0, source_order_id=first)
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        ghost = int(self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,status,risk_payload,created_at,"
            "executed_at,order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", CODE, 100, "filled", "{}", f"{DAY.isoformat()} 09:30:00",
             f"{DAY.isoformat()} 09:30:01", "market", "seed", *stamp, self.cycle100),
        ).lastrowid)
        self._lot(self.cycle100, 100, 10.0, source_order_id=ghost)
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertEqual(P.verified_cash_flows(self.conn, context), {})
        rows = P.positions_for_context(self.conn, context)
        self.assertEqual(rows[0]["display_cost"], 15.0)

    def test_port13b_one_order_with_a_mismatched_fill_blocks_its_key(self):
        # 同一订单上一条合法 fill + 一条错配 fill：order 不得因为"存在合法行"
        # 而被认为已覆盖，否则真实 account/code 会留下半份现金流投影。
        order = self._order_and_fill(
            cycle_id=self.cycle100, side="buy", qty=100, price=10.0,
            fill_date=DAY.isoformat(), fees=0.0,
        )
        self.conn.execute(
            "UPDATE paper_orders SET qty=100 WHERE id=?", (order,)
        )
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (order, ACCOUNT, "buy", "000001", 50, 10.0, 500.0, 0.0,
             DAY.isoformat(), f"{DAY.isoformat()} 09:31:00", "r22-test"),
        )
        self._lot(self.cycle100, 100, 10.0, source_order_id=order)
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertEqual(P.verified_cash_flows(self.conn, context), {})
        self.assertEqual(
            P.positions_for_context(self.conn, context)[0]["display_cost"], 10.0
        )

    def test_port4r_partial_sell_keeps_mixed_uncertain_key_unknown(self):
        # 同一 account/code 下：source-less lot（acquired_at 更早）+ 已证 lot。
        # 部分卖出会先吃掉 source-less 行，但 FIFO 无法证明究竟卖的是哪一条；
        # 只要该 key 仍有持仓，quantity 必须保持 unknown。
        self._lot(self.cycle100, 100, 10.0,
                  acquired_at=f"{DAY.isoformat()} 09:00:00")
        buy = self._order_and_fill(
            cycle_id=self.cycle100, side="buy", qty=100, price=10.0,
            fill_date=DAY.isoformat(), fees=0.0,
        )
        self._lot(self.cycle100, 100, 10.0,
                  acquired_at=f"{DAY.isoformat()} 11:00:00", source_order_id=buy)
        self._order_and_fill(
            cycle_id=self.cycle100, side="sell", qty=100, price=11.0,
            fill_date=DAY.isoformat(), fees=0.0,
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        _lots, status = P.bounded_lots_with_status(self.conn, context)
        self.assertEqual(status, "unknown")

    def test_port13a_identity_mismatch_blocks_real_order_cash_flow(self):
        first = self._order_and_fill(
            cycle_id=self.cycle100, side="buy", qty=100, price=10.0,
            fill_date=DAY.isoformat(), fees=0.0,
        )
        second = self._order_and_fill(
            cycle_id=self.cycle100, side="buy", qty=100, price=10.0,
            fill_date=DAY.isoformat(), fees=0.0,
        )
        self._lot(self.cycle100, 100, 10.0, source_order_id=first)
        self._lot(self.cycle100, 100, 10.0, source_order_id=second)
        self.conn.execute(
            "UPDATE paper_fills SET code='000001' WHERE order_id=?", (second,)
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertEqual(P.verified_cash_flows(self.conn, context), {})
        self.assertEqual(
            P.positions_for_context(self.conn, context)[0]["display_cost"], 10.0
        )

    def test_port3b_unverified_buy_blocks_display_cash_flow(self):
        first = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                     price=10.0, fill_date=DAY.isoformat(), fees=0.0)
        self._lot(self.cycle100, 100, 10.0, source_order_id=first)
        second = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                      price=20.0, fill_date=DAY.isoformat(), fees=0.0)
        self.conn.execute("UPDATE paper_orders SET execution_verified=0 WHERE id=?", (second,))
        self._lot(self.cycle100, 100, 20.0, source_order_id=second)
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertEqual(P.verified_cash_flows(self.conn, context), {})
        rows = P.positions_for_context(self.conn, context)
        self.assertEqual(rows[0]["display_cost"], 15.0)

    def test_port11a_filled_buy_without_fill_evidence_keeps_cash_unknown(self):
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,status,risk_payload,created_at,"
            "executed_at,order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", CODE, 100, "filled", "{}", f"{DAY.isoformat()} 09:30:00",
             f"{DAY.isoformat()} 09:30:01", "market", "seed", *stamp, self.cycle100),
        )
        self.conn.commit()
        self.assertEqual(
            P.cash(self.conn, P.PortfolioReadContext(self.cycle100, DAY)),
            (None, "unknown"),
        )

    def test_port11e_future_dated_fill_order_does_not_change_earlier_cash(self):
        order = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                     price=10.0, fill_date=NEXT.isoformat())
        self.conn.execute(
            "UPDATE paper_orders SET executed_at=? WHERE id=?",
            (f"{DAY.isoformat()} 09:30:01", order),
        )
        self.conn.commit()
        self.assertEqual(
            P.cash(self.conn, P.PortfolioReadContext(self.cycle100, DAY)),
            (100000.0, "verified"),
        )

    def test_port11b_legacy_cash_fallback_accounts_for_recorded_fills(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat(), fees=5.0,
                                   verified=False)
        self._lot(self.cycle100, 100, 10.05, source_order_id=buy)
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))
        self.assertEqual(P.compatibility_cash(self.conn, context), 97990.0)
        with self.assertRaises(P.PortfolioReadUnavailable):
            PT._shared_account_exposure(
                self.conn, {CODE: {"price": 10.0}}, DAY, cycle_id=self.cycle100,
            )

    def test_port11d_mixed_fill_less_lot_is_reconciled(self):
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        legacy = int(self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,status,risk_payload,created_at,"
            "executed_at,order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_status,execution_verified) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", CODE, 100, "filled", "{}", f"{DAY.isoformat()} 09:30:00",
             f"{DAY.isoformat()} 09:30:01", "market", "seed", *stamp, self.cycle100,
             "unknown", 0),
        ).lastrowid)
        self._lot(self.cycle100, 100, 10.0, source_order_id=legacy)
        verified = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=200,
                                        price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 200, 10.0, source_order_id=verified)
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))
        self.assertEqual(P.compatibility_cash(self.conn, context), 96995.0)
        with self.assertRaises(P.PortfolioReadUnavailable):
            PT._shared_account_exposure(
                self.conn, {CODE: {"price": 10.0}}, DAY, cycle_id=self.cycle100,
            )

    def test_port11g_source_less_lot_keeps_cash_unknown(self):
        self._lot(self.cycle100, 100, 10.0)
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))
        lots, status = P.bounded_lots_with_status(self.conn, context)
        self.assertEqual([row["code"] for row in lots], [CODE])
        self.assertEqual(status, "unknown")
        with self.assertRaises(P.PortfolioReadUnavailable):
            PT._shared_account_exposure(
                self.conn, {CODE: {"price": 10.0}}, DAY, cycle_id=self.cycle100,
            )
    def test_port11h_consumed_source_less_lot_cash_completeness(self):
        self._lot(self.cycle100, 100, 10.0)
        self._order_and_fill(
            cycle_id=self.cycle100, side="sell", qty=100, price=11.0,
            fill_date=DAY.isoformat(), fees=0.0,
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))
        self.assertEqual(P.compatibility_cash(self.conn, context), 100100.0)

    def test_port11i_missing_cycle_does_not_invent_zero_capital(self):
        context = P.PortfolioReadContext(999999, DAY)
        self.assertIsNone(P.initial_capital(self.conn, context))
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))
        result = self._portfolio(999999, valuations={CODE: 10.0})
        self.assertIsNone(result["cash"])
        self.assertIsNone(result["nav"])
        self.assertEqual(result["cash_status"], "unknown")
    def test_port11j_read_before_cycle_creation_is_unknown(self):
        before = DAY - dt.timedelta(days=1)
        context = P.PortfolioReadContext(self.cycle100, before)
        self.assertIsNone(P.initial_capital(self.conn, context))
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))
        result = self._portfolio(self.cycle100, before, valuations={CODE: 10.0})
        self.assertIsNone(result["nav"])
        self.assertEqual(result["cash_status"], "unknown")
    def test_port11m_pre_cycle_activity_is_account_scoped(self):
        before = DAY - dt.timedelta(days=1)
        self._lot(
            self.cycle100, 100, 10.0,
            acquired_at=f"{before.isoformat()} 10:00:00",
            account_id="r22-other",
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, before)
        self.assertEqual(
            P.cash(self.conn, context, account_id=ACCOUNT),
            (None, "unknown"),
        )

    def test_port11k_missing_order_fill_uses_compatibility_cash(self):
        verified = self._order_and_fill(
            cycle_id=self.cycle100, side="buy", qty=100, price=10.0,
            fill_date=DAY.isoformat(), fees=5.0,
        )
        self._lot(self.cycle100, 100, 10.05, source_order_id=verified)
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,status,risk_payload,created_at,"
            "executed_at,order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_status,execution_verified) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", "000001", 100, "filled", "{}", f"{DAY.isoformat()} 09:30:00",
             f"{DAY.isoformat()} 09:30:01", "market", "seed", *stamp, self.cycle100,
             "unknown", 0),
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))
        self.assertEqual(P.compatibility_cash(self.conn, context), 98995.0)
        _positions, _value, nav, _industries, _codes = PT._shared_account_exposure(
            self.conn, {CODE: {"price": 10.0}}, DAY, cycle_id=self.cycle100,
        )
        self.assertEqual(nav, 99995.0)
    def test_port4m_future_source_less_lot_keeps_quantity_unknown(self):
        self._lot(
            self.cycle100, 100, 10.0,
            acquired_at=f"{NEXT.isoformat()} 10:00:00",
        )
        self.conn.commit()
        lots, status = P.bounded_lots_with_status(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertEqual(lots, [])
        self.assertEqual(status, "unknown")

    def test_port9c_non_finite_ledger_values_stay_unknown(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat(), fees=0.0)
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.execute(
            "UPDATE paper_fills SET amount=? WHERE order_id=?", (float("inf"), buy)
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))
        self.assertIsNone(P.compatibility_cash(self.conn, context))
        self.assertEqual(P.verified_cash_flows(self.conn, context), {})

    def test_port9d_non_finite_lot_quantity_stays_unknown(self):
        self._lot(self.cycle100, 100, 10.0)
        self.conn.execute(
            "UPDATE paper_position_lots SET qty=? WHERE cycle_id=?",
            (float("inf"), self.cycle100),
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        lots, status = P.bounded_lots_with_status(self.conn, context)
        self.assertEqual(lots, [])
        self.assertEqual(status, "unknown")

    def test_port4q_partial_lot_schema_without_id_fails_closed(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE paper_position_lots("
                "cycle_id INTEGER, account_id TEXT, code TEXT, qty INTEGER,"
                "remaining_qty INTEGER, cost REAL, acquired_at TEXT,"
                "available_date TEXT, asset_type TEXT, source_order_id INTEGER)"
            )
            lots, status = P.bounded_lots_with_status(
                conn, P.PortfolioReadContext(self.cycle100, DAY)
            )
        finally:
            conn.close()
        self.assertEqual(lots, [])
        self.assertEqual(status, "unknown")

    def test_port9e_non_finite_fill_fees_stay_unknown(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.execute(
            "UPDATE paper_fills SET fees=? WHERE order_id=?", (float("inf"), buy)
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        # 存在的非有限 fees 必须保持 unknown；只有缺失的 fee 才等价于 0。
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))
        self.assertEqual(P.verified_cash_flows(self.conn, context), {})

    def test_port9f_non_finite_sell_quantity_fails_closed(self):
        self._lot(self.cycle100, 100, 10.0)
        sell = self._order_and_fill(
            cycle_id=self.cycle100, side="sell", qty=100, price=11.0,
            fill_date=DAY.isoformat(), fees=0.0,
        )
        self.conn.execute(
            "UPDATE paper_fills SET qty=? WHERE order_id=?", (float("inf"), sell)
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        lots, status = P.bounded_lots_with_status(self.conn, context)
        self.assertEqual(status, "unknown")
        self.assertEqual([row["remaining_qty"] for row in lots], [100])

    def test_port9g_non_finite_lot_cost_with_source_stays_unknown(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.execute(
            "UPDATE paper_position_lots SET cost=? WHERE cycle_id=?",
            (float("inf"), self.cycle100),
        )
        self.conn.commit()
        result = self._portfolio(self.cycle100, valuations={CODE: 10.0})
        self.assertEqual(result["quantity_status"], "unknown")
        self.assertIsNone(result["unrealized_pnl"])
        self.assertIsNone(result["nav"])

    def test_port10d_non_finite_realized_pnl_stays_unknown(self):
        sell = self._order_and_fill(
            cycle_id=self.cycle100, side="sell", qty=100, price=11.0,
            fill_date=DAY.isoformat(), fees=0.0,
        )
        self.conn.execute(
            "UPDATE paper_orders SET realized_pnl=? WHERE id=?",
            (float("-inf"), sell),
        )
        self.conn.commit()
        value, status = P.realized_pnl(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertIsNone(value)
        self.assertEqual(status, "unknown")

    def test_port4p_partial_risk_state_schema_fails_closed(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE paper_position_risk_state("
                "cycle_id INTEGER, initialized_at TEXT, updated_at TEXT)"
            )
            conn.execute(
                "INSERT INTO paper_position_risk_state VALUES(?,?,?)",
                (self.cycle100, f"{DAY.isoformat()} 09:00:00",
                 f"{DAY.isoformat()} 09:00:00"),
            )
            context = P.PortfolioReadContext(self.cycle100, DAY)
            self.assertEqual(P._risk_state_rows_for_context(conn, context), [])
            # 缺 identity 列的 runtime state 视为不可用（不是 KeyError）。
            with self.assertRaises(P.PortfolioReadUnavailable):
                P.risk_positions_for_context(conn, context)
        finally:
            conn.close()

    def test_port5f_mismatched_fill_does_not_date_its_order(self):
        # 一条 **fill-less** 已成交 SELL（executed_at 落在 asof 内）+ 一条错配的
        # 未来 fill 挂在同一 order 上。错配 fill 不得为该 order 提供经济日。
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        sell = int(self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
            "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_status,execution_verified,realized_pnl) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "sell", CODE, "测试股", 100, 11.0, 11.0, 1100.0, 0.0, "filled",
             "r22-test", "{}", f"{DAY.isoformat()} 09:30:00",
             f"{DAY.isoformat()} 09:30:01", "market", "seed", *stamp, self.cycle100,
             "verified", 1, 99.0),
        ).lastrowid)
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sell, "r22-other", "buy", "000001", 100, 10.0, 1000.0, 0.0,
             NEXT.isoformat(), f"{NEXT.isoformat()} 09:30:00", "r22-test"),
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        # 错配 fill 不得把该 SELL 推到 asof 之后：否则 order 与 fill 同时从
        # bounded 卖出检查里消失，卖前组合被发布成 verified。
        self.assertEqual(P.realized_pnl(self.conn, context), (None, "unknown"))
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))

    def test_port4o_one_source_fill_cannot_fund_two_lots(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        _lots, status = P.bounded_lots_with_status(self.conn, context)
        self.assertEqual(status, "unknown")
        result = self._portfolio(self.cycle100, valuations={CODE: 10.0})
        self.assertEqual(result["quantity_status"], "unknown")
        self.assertIsNone(result["nav"])
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))

    def test_port5e_partial_fill_schema_fails_closed(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE paper_fills("
                "id INTEGER PRIMARY KEY, order_id INTEGER, account_id TEXT,"
                "side TEXT, code TEXT, qty INTEGER, fill_date TEXT)"
            )
            conn.execute(
                "CREATE TABLE paper_orders("
                "id INTEGER PRIMARY KEY, account_id TEXT, side TEXT, code TEXT,"
                "status TEXT, cycle_id INTEGER, execution_status TEXT,"
                "execution_verified INTEGER, realized_pnl REAL, executed_at TEXT,"
                "amount REAL, fees REAL)"
            )
            context = P.PortfolioReadContext(self.cycle100, DAY)
            self.assertEqual(P.realized_pnl(conn, context), (None, "unknown"))
            self.assertEqual(P.cash(conn, context), (None, "unknown"))
            self.assertEqual(P.positions_for_context(conn, context), [])
        finally:
            conn.close()

    def test_port11p_attachment_provenance_missing_stays_unknown(self):
        # 规格 A：cycle 早于 asof 创建、paper_accounts 当前指向该 cycle、
        # paper_parameter_versions 表存在但**没有**匹配的 attachment 行，
        # 且无 account-specific bounded activity
        # => 账户级 initial capital / cash 都必须 UNKNOWN。
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='paper_parameter_versions'"
        ).fetchone(), "attachment-provenance table must exist")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM paper_parameter_versions WHERE cycle_id=? AND account_id=?",
            (self.cycle100, ACCOUNT),
        ).fetchone()[0], 0)
        context = P.PortfolioReadContext(self.cycle100, DAY)
        # 非空门禁 1：cycle 本身在 asof 前已存在 —— 否则本测试无法区分
        # “cycle 不存在”与“account attachment 不可证明”。
        self.assertTrue(P._cycle_created_by(self.conn, context))
        # 非空门禁 2：账户确实挂在这个 cycle 上 —— 否则走的是 cycle_id 不匹配分支。
        self.assertEqual(self.conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT,)
        ).fetchone()[0], self.cycle100)
        self.assertFalse(P._account_attached_by(self.conn, context, ACCOUNT))
        self.assertIsNone(P._cycle_initial(self.conn, context, account_id=ACCOUNT))
        self.assertEqual(P.cash(self.conn, context, account_id=ACCOUNT), (None, "unknown"))

    def test_port11q_attachment_provenance_before_or_on_asof_may_verify(self):
        # 规格 B：匹配的 attachment 证据 effective_date <= asof => 可 verified。
        self.conn.execute(
            "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,"
            "params,reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.cycle100, ACCOUNT, "v1.0", "trend", "{}", "r23-test",
             DAY.isoformat(), f"{DAY.isoformat()} 08:00:00"),
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertTrue(P._account_attached_by(self.conn, context, ACCOUNT))
        self.assertIsNotNone(P._cycle_initial(self.conn, context, account_id=ACCOUNT))

    def test_port11r_attachment_provenance_after_asof_stays_unknown(self):
        # 规格 C：匹配的 attachment 证据 effective_date > asof => UNKNOWN。
        self.conn.execute(
            "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,"
            "params,reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.cycle100, ACCOUNT, "v2.0", "trend", "{}", "r23-test",
             NEXT.isoformat(), f"{NEXT.isoformat()} 09:00:00"),
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertTrue(P._cycle_created_by(self.conn, context))
        self.assertFalse(P._account_attached_by(self.conn, context, ACCOUNT))
        self.assertIsNone(P._cycle_initial(self.conn, context, account_id=ACCOUNT))
        self.assertEqual(P.cash(self.conn, context, account_id=ACCOUNT), (None, "unknown"))

    def test_port11s_partial_attachment_schema_stays_unknown_without_exception(self):
        # 规格 D：attachment-provenance schema 缺失/不完整，
        # 且没有其他 bounded account-specific 证据 => UNKNOWN，不抛异常。
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, capital REAL,"
                " created_at TEXT);"
                "CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, initial_cash REAL,"
                " cycle_id INTEGER);"
                # 只有部分列：缺 effective_date，必须读作“不可用”而非抛 SQL 错误。
                "CREATE TABLE paper_parameter_versions(cycle_id INTEGER,"
                " account_id TEXT);"
            )
            conn.execute("INSERT INTO paper_cycles VALUES(?,?,?)",
                         (7, 100000.0, f"{DAY.isoformat()} 09:00:00"))
            conn.execute("INSERT INTO paper_accounts VALUES(?,?,?)",
                         (ACCOUNT, 100000.0, 7))
            conn.execute("INSERT INTO paper_parameter_versions VALUES(?,?)", (7, ACCOUNT))
            conn.commit()
            context = P.PortfolioReadContext(7, DAY)
            self.assertFalse(P._account_attached_by(conn, context, ACCOUNT))
            self.assertIsNone(P._cycle_initial(conn, context, account_id=ACCOUNT))
            self.assertEqual(P.cash(conn, context, account_id=ACCOUNT), (None, "unknown"))
        finally:
            conn.close()

    def test_port11t_cycle_creation_is_not_account_attachment(self):
        # 核心判据：cycle existed by D != account belonged to cycle by D。
        # 同一条 cycle 级证据在**cycle 级**读取时足以证明资本，
        # 在**账户级**读取时不足以证明 attachment。
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertTrue(P._cycle_created_by(self.conn, context))
        self.assertIsNotNone(P._cycle_initial(self.conn, context))  # cycle 级：仍可证明
        self.assertIsNone(P._cycle_initial(self.conn, context, account_id=ACCOUNT))
        # 不得因为这个修改而让 cycle-level initial capital 无条件 unknown。

    def test_port11n_missing_cycle_creation_evidence_stays_unknown(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, capital REAL)")
            conn.execute("INSERT INTO paper_cycles VALUES(?,?)", (self.cycle100, 100000.0))
            context = P.PortfolioReadContext(self.cycle100, DAY)
            self.assertIsNone(P.initial_capital(conn, context))
            self.assertEqual(P.cash(conn, context), (None, "unknown"))
        finally:
            conn.close()

    def test_port4n_multi_fill_source_lot_keeps_quantity_unknown(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (buy, ACCOUNT, "buy", CODE, 100, 10.0, 1000.0, 5.0,
             NEXT.isoformat(), f"{NEXT.isoformat()} 09:30:00", "r22-test"),
        )
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        _lots, status = P.bounded_lots_with_status(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertEqual(status, "unknown")

    def test_port4l_missing_lot_schema_keeps_quantity_unknown(self):
        conn = sqlite3.connect(":memory:")
        try:
            lots, status = P.bounded_lots_with_status(
                conn, P.PortfolioReadContext(self.cycle100, DAY)
            )
        finally:
            conn.close()
        self.assertEqual(lots, [])
        self.assertEqual(status, "unknown")

    def test_port5g_side_contradicting_fill_blocks_coverage(self):
        # 已成交 BUY order 上挂一条同日 SELL fill：contradictory execution
        # evidence 必须让该 order 的现金流不再是 verified。
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat(), fees=0.0)
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (buy, ACCOUNT, "sell", CODE, 100, 10.0, 1000.0, 0.0,
             DAY.isoformat(), f"{DAY.isoformat()} 09:31:00", "r22-test"),
        )
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        self.assertEqual(P.verified_cash_flows(self.conn, context), {})
        self.assertEqual(P.cash(self.conn, context), (None, "unknown"))

    def test_port11o_account_attached_after_asof_stays_unknown(self):
        # cycle 在 asof 之前就存在（created_at <= asof），但该账户是**之后**
        # 才接入这个周期的：账户级 initial capital 仍必须保持 unknown。
        self.conn.execute(
            "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,"
            "params,reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.cycle100, ACCOUNT, "v2.0", "trend", "{}", "r22-test",
             NEXT.isoformat(), f"{NEXT.isoformat()} 09:00:00"),
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        # 非空门禁：cycle 本身在 asof 之前已存在，否则本测试无法区分两条路径。
        self.assertTrue(P._cycle_created_by(self.conn, context))
        # cycle 级资本仍可证明（周期确实已存在）；账户级资本不可证明。
        self.assertIsNone(P._cycle_initial(self.conn, context, account_id=ACCOUNT))
        self.assertEqual(
            P.cash(self.conn, context, account_id=ACCOUNT), (None, "unknown")
        )

    def test_port10b_realized_pnl_without_execution_schema_stays_unknown(self):
        conn = sqlite3.connect(":memory:")
        try:
            value, status = P.realized_pnl(
                conn, P.PortfolioReadContext(self.cycle100, DAY)
            )
        finally:
            conn.close()
        self.assertIsNone(value)
        self.assertEqual(status, "unknown")
    def test_port11c_account_initial_capital_is_cycle_scoped(self):
        # 必须给出**可证明的 attachment 证据**，否则账户级读取会先被
        # attachment gate 拦下（R23 起），本测试就观测不到 cycle_id 作用域检查。
        self.conn.execute(
            "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,"
            "params,reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.cycle100, ACCOUNT, "v1.0", "trend", "{}", "r23-test",
             DAY.isoformat(), f"{DAY.isoformat()} 08:00:00"),
        )
        self.conn.commit()
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=? WHERE id=?", (self.cycle101, ACCOUNT)
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
        # 非空门禁：attachment 对本周期可证明，因此下面读到的 None
        # 只可能来自 cycle_id 作用域检查，而非 attachment gate。
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM paper_parameter_versions"
                " WHERE cycle_id=? AND account_id=?", (self.cycle100, ACCOUNT)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            P.cash(self.conn, context, account_id=ACCOUNT),
            (None, "unknown"),
        )

    def test_port9b_non_finite_or_non_positive_valuation_stays_unknown(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        for bad_price in (float("nan"), float("inf"), float("-inf"), 0.0, -1.0):
            with self.subTest(price=bad_price):
                result = self._portfolio(self.cycle100, valuations={CODE: bad_price})
                self.assertIsNone(result["market_value"])
                self.assertIsNone(result["unrealized_pnl"])
                self.assertIsNone(result["nav"])
                self.assertEqual(result["market_value_status"], "unknown")

    def test_port10c_partial_future_sell_fill_keeps_realized_pnl_unknown(self):
        order = self._order_and_fill(
            cycle_id=self.cycle100, side="sell", qty=50, price=11.0,
            fill_date=DAY.isoformat(), realized_pnl=123.0, fees=0.0,
        )
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,"
            "fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                order, ACCOUNT, "sell", CODE, 50, 12.0, 600.0, 0.0,
                NEXT.isoformat(), f"{NEXT.isoformat()} 09:30:00", "r22-test",
            ),
        )
        self.conn.commit()
        value, status = P.realized_pnl(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertIsNone(value)
        self.assertEqual(status, "unknown")

    def test_port11f_archived_cycle_remains_unknown(self):
        self.conn.execute(
            "INSERT INTO paper_archives(cycle_id,cycle_key,reason,snapshot,created_at) "
            "VALUES(?,?,?,?,?)",
            (self.cycle100, "r22-c100", "test_archive", "{}", f"{DAY.isoformat()} 15:00:00"),
        )
        self.conn.commit()
        result = self._portfolio(self.cycle100, valuations={CODE: 10.0})
        self.assertTrue(result["archived"])
        self.assertEqual(result["quantity_status"], "unknown")
        self.assertIsNone(result["market_value"])
        self.assertIsNone(result["cash"])
        self.assertIsNone(result["realized_pnl"])
        self.assertIsNone(result["nav"])

    def test_port7_historical_read_never_resolves_active_cycle(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        source = Path(P.__file__).read_text(encoding="utf-8")
        self.assertNotIn("active_cycle_id", source)
        result = self._portfolio(self.cycle100)
        self.assertEqual(result["positions"][0]["qty"], 100)
        self.assertEqual(result["context"]["cycle_id"], self.cycle100)

    def test_port8_historical_read_does_not_use_wall_clock(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        first = self._portfolio(self.cycle100)
        real_date = P.dt.date

        class FakeDateMeta(type):
            def __instancecheck__(cls, instance):
                return isinstance(instance, real_date)

        class FakeDate(real_date, metaclass=FakeDateMeta):
            @classmethod
            def today(cls):
                raise AssertionError("historical read called date.today()")

        fake_dt = type("FakeDatetimeModule", (), {
            "date": FakeDate, "datetime": P.dt.datetime,
        })
        with mock.patch.object(P, "dt", fake_dt):
            second = self._portfolio(self.cycle100)
        self.assertEqual(first["positions"], second["positions"])
        self.assertEqual(first["cash"], second["cash"])

    def test_port9_unknown_valuation_stays_unknown(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        result = self._portfolio(self.cycle100, valuations={"000001": 10.0})
        self.assertIsNone(result["market_value"])
        self.assertIsNone(result["unrealized_pnl"])
        self.assertIsNone(result["nav"])
        self.assertEqual(result["valuation_missing_codes"], [CODE])
        self.assertEqual(result["market_value_status"], "unknown")

    def test_port10_realized_pnl_uses_verified_execution_only(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        verified_sell = self._order_and_fill(
            cycle_id=self.cycle100, side="sell", qty=50, price=20.0,
            fill_date=DAY.isoformat(), realized_pnl=495.0,
        )
        self.conn.commit()
        value, status = P.realized_pnl(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertEqual((value, status), (495.0, "verified"))
        self.conn.execute("UPDATE paper_orders SET execution_verified=0 WHERE id=?", (verified_sell,))
        self.conn.commit()
        value, status = P.realized_pnl(
            self.conn, P.PortfolioReadContext(self.cycle100, DAY)
        )
        self.assertIsNone(value)
        self.assertEqual(status, "unknown")

    def test_port11_cash_and_nav_are_cycle_asof_bounded(self):
        buy = self._order_and_fill(cycle_id=self.cycle100, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat(), fees=5.0)
        self._lot(self.cycle100, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        day_result = self._portfolio(self.cycle100, DAY, valuations={CODE: 10.0})
        self.assertEqual(day_result["cash"], 98995.0)
        self.assertEqual(day_result["nav"], 99995.0)
        self._order_and_fill(
            cycle_id=self.cycle100, side="sell", qty=50, price=20.0,
            fill_date=NEXT.isoformat(), realized_pnl=495.0, fees=5.0,
        )
        historical = self._portfolio(self.cycle100, DAY, valuations={CODE: 10.0})
        self.assertEqual(historical["cash"], 98995.0)
        self.assertEqual(historical["nav"], 99995.0)
        future = self._portfolio(self.cycle100, NEXT, valuations={CODE: 20.0})
        self.assertEqual(future["cash"], 99990.0)
        self.assertEqual(future["nav"], 100990.0)

    def test_port12_current_read_behavior_remains_compatible(self):
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=? WHERE id=?", (self.cycle101, ACCOUNT)
        )
        self._lot(self.cycle101, 100, 10.0, remaining_qty=40)
        self.conn.commit()
        rows = PPRM.current_positions(self.conn)
        self.assertEqual([row["qty"] for row in rows], [40])

    def test_port13_read_model_is_read_only_and_has_no_execution_path(self):
        source = Path(P.__file__).read_text(encoding="utf-8").upper()
        for token in ("INSERT INTO", "UPDATE ", "DELETE FROM", "COMMIT_FILL"):
            self.assertNotIn(token, source)
        self.assertNotIn("IMPORT PAPER_TRADING", source)
        self.assertFalse(any(
            token in source for token in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE")
        ))

    def test_port14_r21_risk_service_receives_explicit_cycle_for_exposure(self):
        source = Path(PT.__file__).with_name("paper_risk_service.py").read_text(encoding="utf-8")
        self.assertIn(
            "ports.shared_exposure(\n            conn, day, quote_map, cycle_id=cycle_id,",
            source,
        )


if __name__ == "__main__":
    unittest.main()
