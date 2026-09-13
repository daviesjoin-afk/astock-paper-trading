# -*- coding: utf-8 -*-
"""Unit tests verifying today_pnl is None during pre-market, non-trading, and auction sessions in _shared_metrics."""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import paper_trading as P  # noqa: E402


class MarketSessionSharedPnlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db = P.DB_PATH
        P.DB_PATH = os.path.join(self.tmp.name, "test_paper.db")
        self.addCleanup(setattr, P, "DB_PATH", self.old_db)
        P.init_db()
        self.conn = sqlite3.connect(P.DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

        prev_date = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        now_str = dt.datetime.now().isoformat()
        self.conn.execute("UPDATE paper_cycles SET status='running'")
        for aid in P.ACTIVE_ACCOUNT_IDS:
            self.conn.execute(
                "INSERT INTO paper_nav(account_id, nav_date, cash, market_value, nav, benchmark, created_at, quote_status) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (aid, prev_date, 60000.0, 0.0, 60000.0, 1.0, now_str, "ok")
            )
        self.conn.commit()

    def test_shared_metrics_pre_market_shows_none_today_pnl(self):
        cycle = dict(self.conn.execute("SELECT * FROM paper_cycles WHERE status='running' LIMIT 1").fetchone())
        positions = []
        quotes = {}

        # Mock market session to simulate 02:00 pre-market
        fake_session = {
            "code": "premarket",
            "label": "盘前未开盘",
            "today_pnl_available": False,
        }
        with mock.patch.object(P, "_market_session", return_value=fake_session):
            metrics = P._shared_metrics(self.conn, cycle, positions, quotes)
            self.assertIsNone(metrics["today_pnl"])
            self.assertIsNone(metrics["today_return_pct"])
            self.assertFalse(metrics["today_pnl_available"])
            self.assertEqual(metrics["today_pnl_status"], "盘前未开盘")
            self.assertEqual(metrics["market_session"], "premarket")

    def test_shared_metrics_weekend_shows_none_today_pnl(self):
        cycle = dict(self.conn.execute("SELECT * FROM paper_cycles WHERE status='running' LIMIT 1").fetchone())
        positions = []
        quotes = {}

        # Mock market session to simulate weekend
        fake_session = {
            "code": "weekend",
            "label": "非交易日",
            "today_pnl_available": False,
        }
        with mock.patch.object(P, "_market_session", return_value=fake_session):
            metrics = P._shared_metrics(self.conn, cycle, positions, quotes)
            self.assertIsNone(metrics["today_pnl"])
            self.assertIsNone(metrics["today_return_pct"])
            self.assertFalse(metrics["today_pnl_available"])
            self.assertEqual(metrics["today_pnl_status"], "非交易日")
            self.assertEqual(metrics["market_session"], "weekend")

    def test_shared_metrics_intraday_calculates_today_pnl(self):
        cycle = dict(self.conn.execute("SELECT * FROM paper_cycles WHERE status='running' LIMIT 1").fetchone())
        positions = []
        quotes = {}

        # Mock market session to simulate trading hours
        fake_session = {
            "code": "morning_session",
            "label": "连续竞价",
            "today_pnl_available": True,
        }
        with mock.patch.object(P, "_market_session", return_value=fake_session):
            metrics = P._shared_metrics(self.conn, cycle, positions, quotes)
            self.assertIsNotNone(metrics["today_pnl"])
            self.assertTrue(metrics["today_pnl_available"])
            self.assertEqual(metrics["today_pnl_status"], "")
            self.assertEqual(metrics["market_session"], "morning_session")


    def test_build_factor_table_copies_main_force_columns(self):
        import numpy as np
        import pandas as pd
        import strategies as S

        idx = ["000001", "000002"]
        price = pd.DataFrame({
            "price": [10.0, 20.0],
            "mom5": [0.01, 0.02],
            "mom20": [0.03, 0.04],
            "mom60": [0.05, 0.06],
            "vol_surge": [1.1, 1.2],
            "rsi14": [50.0, 60.0],
            "flow_proxy": [0.1, -0.1],
            "price_evidence_quality": [1.0, 1.0],
            "adjustment_warning": [False, False],
            "main_pct": [12.5, -3.2],
            "main_net": [50000000.0, -1000000.0],
            "turnover": [3.5, 1.2],
            "amount": [1e8, 5e7],
        }, index=idx)
        for col in S.TECHNICAL_COLUMNS:
            price[col] = False if not col.startswith("ma") else price["price"] * 0.9
        fund = pd.DataFrame({
            "name": ["A", "B"],
            "industry": ["电子", "汽车"],
            "pe": [15.0, 20.0],
            "pb": [1.5, 2.0],
            "roe": [10.0, 12.0],
            "pct_today": [1.5, -0.5],
            "rev_yoy": [10.0, 5.0],
            "profit_yoy": [8.0, 3.0],
            "super_net": [100.0, 200.0],
            "mktcap": [100.0, 200.0],
            "float_cap": [80.0, 150.0],
        }, index=idx)

        table = S.build_factor_table(price, fund)
        self.assertIn("main_pct", table.columns)
        self.assertIn("main_net", table.columns)
        self.assertEqual(table.loc["000001", "main_pct"], 12.5)
        self.assertEqual(table.loc["000001", "main_net"], 50000000.0)

    def test_paper_selection_falls_back_to_shadow_picks(self):
        import paper_selection as PS

        fake_res = {
            "picks": [],
            "shadow_picks": [
                {
                    "code": "600001",
                    "name": "测试股",
                    "price": 10.0,
                    "score": 88.0,
                    "pct": 2.5,
                    "super_net": 100000,
                    "reasons": ["主力大额流入"],
                    "industry": "金融",
                    "historical_factor_date": "2026-09-11",
                }
            ],
            "data_quality": {"reference_date": "2026-09-11", "complete_cutoff": "2026-09-11"},
        }
        with mock.patch.object(PS, "_run_one", return_value=fake_res):
            res = PS.run_daily(topn=5, run_date="2026-09-11")
            strat = res["strategies"][0]
            self.assertEqual(len(strat["picks"]), 1)
            self.assertEqual(strat["picks"][0]["code"], "600001")


if __name__ == "__main__":
    unittest.main()
