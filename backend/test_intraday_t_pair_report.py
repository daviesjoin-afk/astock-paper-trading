# -*- coding: utf-8 -*-
"""配对做 T 评价口径测试：底仓 slice 盈亏、配对价差、未回补机会成本三块分离。"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

import intraday_t_pair_report as REPORT


def sell_order(**changes):
    row = {"id": 11, "account_id": "tq_breakout", "code": "000001", "side": "sell",
           "qty": 1000, "filled_qty": 1000, "filled_price": 10.0, "amount": 10000.0,
           "fees": 5.0, "status": "filled", "execution_status": "verified",
           "execution_verified": 1, "executed_at": "2026-10-23 10:05:00",
           "risk_payload": json.dumps({"cost_amount": 9000.0})}
    return {**row, **changes}


def rebuy_order(**changes):
    row = {"id": 12, "account_id": "tq_breakout", "code": "000001", "side": "buy",
           "qty": 1000, "filled_qty": 1000, "filled_price": 9.7, "amount": 9700.0,
           "fees": 5.0, "status": "filled", "execution_status": "verified",
           "execution_verified": 1, "executed_at": "2026-10-23 10:40:00",
           "risk_payload": json.dumps({"kind": "stock_inventory_t",
                                       "paired_sell_observation": 301})}
    return {**row, **changes}


def sell_observation(**changes):
    row = {"id": 301, "code": "000001", "observed_at": "2026-10-23 10:05:00",
           "price": 10.0, "action": "t_sell",
           "payload": json.dumps({"order_id": 11, "sell_price": 10.0, "qty": 1000})}
    return {**row, **changes}


class PairSummaryTests(unittest.TestCase):
    def test_full_round_trip_separates_base_slice_from_spread(self):
        result = REPORT.summarize([sell_order()], [sell_observation()], [rebuy_order()])
        pair = result["rows"][0]
        self.assertEqual(result["status"], "case_review_only")
        self.assertEqual(pair["status"], "closed")
        self.assertAlmostEqual(pair["base_slice_pnl"], 10000.0 - 5.0 - 9000.0, places=6)
        self.assertAlmostEqual(pair["spread_pnl"], 300.0 - 5.0 - 5.0, places=6)
        self.assertEqual(pair["unrebought_qty"], 0)
        self.assertIsNone(pair["unrebought_opportunity_cost"])
        self.assertAlmostEqual(result["base_slice_pnl"], 995.0, places=6)
        self.assertAlmostEqual(result["paired_spread_pnl"], 290.0, places=6)

    def test_partial_rebuy_prorates_sell_fee_and_prices_the_gap(self):
        result = REPORT.summarize(
            [sell_order()], [sell_observation()],
            [rebuy_order(filled_qty=600, qty=600, amount=5820.0, fees=3.0)],
            closes={"000001": {"price": 10.2, "source": "kline_close"}})
        pair = result["rows"][0]
        self.assertEqual(pair["status"], "partial")
        self.assertAlmostEqual(pair["rebought_qty"], 600, places=6)
        self.assertAlmostEqual(pair["unrebought_qty"], 400, places=6)
        self.assertAlmostEqual(pair["spread_pnl"], 0.3 * 600 - 3.0 - 3.0, places=6)
        self.assertAlmostEqual(pair["unrebought_opportunity_cost"], 0.2 * 400, places=6)
        self.assertEqual(pair["close_price_source"], "kline_close")
        self.assertEqual(result["partial_pairs"], 1)
        self.assertEqual(result["unrebought_qty"], 400)

    def test_zero_share_t_rebuy_observation_is_not_a_closure(self):
        deferred = sell_observation(id=302, action="t_rebuy",
                                    payload=json.dumps({"qty": 0, "deferred": True}))
        result = REPORT.summarize([sell_order()], [sell_observation(), deferred], [])
        pair = result["rows"][0]
        self.assertEqual(pair["status"], "open")
        self.assertIsNone(pair["spread_pnl"])
        self.assertEqual(result["deferred_rebuy_observations"], 1)
        self.assertEqual(result["closed_pairs"], 0)

    def test_unverifiable_legs_degrade_instead_of_zero_filling(self):
        unverified = REPORT.summarize(
            [sell_order(execution_verified=0)], [sell_observation()], [rebuy_order()])
        self.assertEqual(unverified["rows"][0]["status"], "unverified_sell")
        self.assertIsNone(unverified["base_slice_pnl"])
        orphan = REPORT.summarize(
            [sell_order(risk_payload=json.dumps({}))], [sell_observation()], [rebuy_order()])
        self.assertIsNone(orphan["rows"][0]["base_slice_pnl"])
        self.assertIsNotNone(orphan["rows"][0]["spread_pnl"])

    def test_missing_observation_for_sell_is_ignored_not_guessed(self):
        result = REPORT.summarize([sell_order()], [], [rebuy_order()])
        self.assertEqual(result["pairs"], 0)
        self.assertIsNone(result["paired_spread_pnl"])
        self.assertEqual(result["deferred_rebuy_observations"], 0)

    def test_report_declares_its_boundary_and_caveats(self):
        result = REPORT.summarize([sell_order()], [sell_observation()], [rebuy_order()])
        self.assertIn("不等于已回补", result["boundary"])
        self.assertTrue(any("不是做 T 收益" in item for item in result["caveats"]))
        summary = REPORT.evidence_summary(result)
        self.assertNotIn("rows", summary)
        self.assertIn("paired_spread_pnl", summary)


class PairReportReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "paper.sqlite3")
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, cycle_id INTEGER)")
        conn.execute("INSERT INTO paper_accounts VALUES('tq_breakout', 7)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.directory.cleanup()

    def test_missing_tables_report_unavailable_without_creating_them(self):
        before = os.path.getsize(self.path)
        result = REPORT.build_report(self.path, asof="2026-10-23T15:00:00+08:00")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "ledger_or_schema_unavailable")
        self.assertEqual(os.path.getsize(self.path), before)
        conn = sqlite3.connect(self.path)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(tables, {"paper_accounts"})

    def test_invalid_asof_is_rejected_explicitly(self):
        result = REPORT.build_report(self.path, asof="not-a-time")
        self.assertEqual(result["reason"], "invalid_asof")


if __name__ == "__main__":
    unittest.main()
