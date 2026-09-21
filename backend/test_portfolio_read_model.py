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