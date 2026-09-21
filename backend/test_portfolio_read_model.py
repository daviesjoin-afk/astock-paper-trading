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
             source_order_id=None):
        acquired_at = acquired_at or f"{DAY.isoformat()} 10:00:00"
        remaining_qty = qty if remaining_qty is None else remaining_qty
        return int(self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
            "remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,"
            "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, ACCOUNT, CODE, "测试股", "测试", qty, remaining_qty, cost,
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
    def test_port11c_account_initial_capital_is_cycle_scoped(self):
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=? WHERE id=?", (self.cycle101, ACCOUNT)
        )
        self.conn.commit()
        context = P.PortfolioReadContext(self.cycle100, DAY)
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