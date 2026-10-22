# -*- coding: utf-8 -*-
"""限价锚点冻结（``paper_trading._frozen_entry_limit``）。

限价画像的参考价每轮取当轮实时价，重算出的限价会跟着价格上移：当轮买不到，
下一轮锚点更高，形成结构性追价。冻结口径是"该信号当日**首次**限价延期单的
``planned_price``"，只降不升。这里钉死三件事：

    1. 取最早那张限价单，而不是最新一张（否则每轮都会重新锚定）；
    2. 直接返回冻结限价与已等待分钟，供调用方判断追价窗口是否已过；
    3. 当天没有限价单时返回 ``(None, None)``，调用方按未冻结处理。

纯 fixture：内存 sqlite，不联网、不读写真实账本。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import paper_trading as PT  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600901"
SIGNAL_ID = 9


class FrozenEntryLimitTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE paper_orders(
                   id INTEGER PRIMARY KEY, account_id TEXT, code TEXT, side TEXT,
                   signal_id INTEGER, order_type TEXT, planned_price REAL,
                   status TEXT, created_at TEXT)"""
        )
        self.today = dt.date.today()

    def _order(self, *, minutes_ago, price, order_type="limit", status="execution_retry",
               signal_id=SIGNAL_ID):
        stamp = (dt.datetime.now() - dt.timedelta(minutes=minutes_ago)).strftime(
            "%Y-%m-%d %H:%M:%S")
        cursor = self.conn.execute(
            """INSERT INTO paper_orders(
                   account_id,code,side,signal_id,order_type,planned_price,status,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (ACCOUNT, CODE, "buy", signal_id, order_type, price, status, stamp))
        return int(cursor.lastrowid)

    def _resolve(self):
        return PT._frozen_entry_limit(self.conn, ACCOUNT, CODE, SIGNAL_ID, self.today)

    def test_returns_the_earliest_limit_and_its_age(self):
        """取最早那张限价单：晚些的延期单不得把锚点重新抬高。"""
        self._order(minutes_ago=8, price=20.04)   # 首次延期：冻结在 20.04
        self._order(minutes_ago=2, price=20.44)   # 后续延期：若取最新就会追到 20.44
        limit_price, waited = self._resolve()
        self.assertEqual(20.04, limit_price)
        self.assertIsNotNone(waited)
        self.assertGreaterEqual(waited, 7.5)

    def test_market_order_is_not_a_frozen_limit(self):
        self._order(minutes_ago=5, price=20.04, order_type="market")
        self.assertEqual((None, None), self._resolve())

    def test_no_limit_order_means_not_frozen(self):
        self.assertEqual((None, None), self._resolve())

    def test_other_signal_is_isolated(self):
        self._order(minutes_ago=5, price=20.04, signal_id=SIGNAL_ID + 1)
        self.assertEqual((None, None), self._resolve())


if __name__ == "__main__":
    unittest.main()
