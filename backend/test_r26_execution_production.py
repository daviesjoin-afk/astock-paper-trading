# -*- coding: utf-8 -*-
"""R26 offline production-path replay for partial fills and T+1 accounting."""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

import execution_planner as EP
import paper_portfolio_read_model as PPRM
import paper_trading as PT
from test_production_path_golden_replay import OfflinePaperEnv


D1 = dt.date(2026, 9, 9)
D2 = dt.date(2026, 9, 10)
ACCOUNT_ID = "tq_breakout"
CODE = "600901"


class R26ProductionExecutionTests(OfflinePaperEnv, unittest.TestCase):
    def setUp(self):
        super().setUp()
        PT.init_db()

    @staticmethod
    def _quote(day: dt.date, *, amount: float, minute: str = "10:00") -> dict:
        return {
            "code": CODE,
            "name": "回放甲",
            "price": 10.0,
            "prev_close": 10.0,
            "pct": 0.0,
            "amount": amount,
            "quote_at": f"{day.isoformat()} {minute}:00",
            "quote_source": "live",
            "quote_validation": "cross_source_checked",
            "risk_flag": 0,
            "listing_status": "listed",
            "tradable": True,
        }

    def _insert_order(self, conn, *, cycle_id: int, side: str, qty: int) -> int:
        stamp = PT._strategy_stamp(conn, ACCOUNT_ID, cycle_id=cycle_id)
        cursor = conn.execute(
            """INSERT INTO paper_orders(
                   account_id,side,code,name,qty,planned_price,status,reason,risk_payload,
                   created_at,order_type,origin,strategy_id,strategy_version,
                   strategy_checksum,cycle_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ACCOUNT_ID, side, CODE, "回放甲", qty, 10.0, "pending_execution",
             "R26 production replay", "{}", f"{D1.isoformat()} 09:30:00",
             "market", "strategy", *stamp, cycle_id),
        )
        return int(cursor.lastrowid)

    def _execute(self, conn, *, account, order_id: int, side: str, qty: int,
                 day: dt.date, minute: str, amount: float) -> dict:
        asof = f"{day.isoformat()} {minute}:00"
        quote = self._quote(day, amount=amount, minute=minute)
        return EP.execute_order(
            conn,
            account=account,
            plan={"side": side, "code": CODE, "name": "回放甲", "industry": "回放行业",
                  "qty": qty, "execution_quote": quote},
            order_id=order_id,
            asof_day=day,
            execution_quote=quote,
            execution_as_of=asof,
            action=f"r26_{side}",
            reason="R26 离线生产路径回放",
        )

    def test_partial_fill_retries_as_new_event_and_respects_t_plus_one(self):
        real_date = PT._date
        with mock.patch.object(PT, "_now", lambda: f"{D1.isoformat()} 09:00:00"), \
             mock.patch.object(PT, "_date", lambda value=None: D1 if value is None else real_date(value)):
            with self._conn() as conn:
                cycle = PT._create_cycle(
                    conn, 100_000.0, status="running", enabled_strategies=[ACCOUNT_ID],
                )
                cycle_id = int(cycle["id"])
                account = dict(conn.execute(
                    "SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT_ID,),
                ).fetchone())
                buy_id = self._insert_order(conn, cycle_id=cycle_id, side="buy", qty=300)

                first = self._execute(
                    conn, account=account, order_id=buy_id, side="buy", qty=300,
                    day=D1, minute="10:00", amount=100_000.0,
                )
                self.assertEqual(("partially_filled", 100, 200),
                                 (first["status"], first["filled_qty"], first["remaining_qty"]))

                sell_id = self._insert_order(conn, cycle_id=cycle_id, side="sell", qty=100)
                same_day = self._execute(
                    conn, account=account, order_id=sell_id, side="sell", qty=100,
                    day=D1, minute="10:05", amount=1_000_000.0,
                )
                self.assertEqual("pending_execution", same_day["status"])
                self.assertIn("t_plus_one_locked", same_day["execution"]["reason_codes"])

                next_day_sell = self._execute(
                    conn, account=account, order_id=sell_id, side="sell", qty=100,
                    day=D2, minute="10:00", amount=1_000_000.0,
                )
                self.assertEqual(("filled", 100),
                                 (next_day_sell["status"], next_day_sell["event_filled_qty"]))

                second = self._execute(
                    conn, account=account, order_id=buy_id, side="buy", qty=300,
                    day=D2, minute="10:05", amount=2_000_000.0,
                )
                self.assertEqual(("filled", 300, 0),
                                 (second["status"], second["filled_qty"], second["remaining_qty"]))

                fill_rows = conn.execute(
                    "SELECT id,qty,execution_event_key FROM paper_fills WHERE order_id=? ORDER BY id",
                    (buy_id,),
                ).fetchall()
                self.assertEqual([100, 200], [int(row["qty"]) for row in fill_rows])
                self.assertEqual(2, len({row["execution_event_key"] for row in fill_rows}))
                lot_links = conn.execute(
                    "SELECT source_fill_id,qty FROM paper_position_lots"
                    " WHERE source_order_id=? ORDER BY id", (buy_id,),
                ).fetchall()
                self.assertEqual([(int(row["id"]), int(row["qty"])) for row in fill_rows],
                                 [(int(row["source_fill_id"]), int(row["qty"])) for row in lot_links])

                first_day_context = PPRM.PortfolioReadContext(cycle_id, D1)
                first_day_positions, first_day_status = PPRM.positions_for_context_with_status(
                    conn, first_day_context,
                )
                self.assertEqual("verified", first_day_status)
                self.assertEqual(100, sum(int(row["qty"]) for row in first_day_positions))
                first_day_cash, first_day_cash_status = PPRM.cash(conn, first_day_context)
                self.assertEqual("verified", first_day_cash_status)
                self.assertIsNotNone(first_day_cash)

                context = PPRM.PortfolioReadContext(cycle_id, D2)
                positions, quantity_status = PPRM.positions_for_context_with_status(conn, context)
                self.assertEqual("verified", quantity_status)
                self.assertEqual(200, sum(int(row["qty"]) for row in positions))
                cash, cash_status = PPRM.cash(conn, context)
                self.assertEqual("verified", cash_status)
                self.assertIsNotNone(cash)


if __name__ == "__main__":
    unittest.main()
