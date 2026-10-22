# -*- coding: utf-8 -*-
"""调仓扫描的"今日已卖出"窗口必须是**半开区间** ``[今日, 次日)``。

缺陷背景：查询只写了下界 ``created_at >= today``。只要时钟超前（换机、容器时区、
手工回放写错时间），落在"明天"的卖出委托也会被算成"今天已经卖过"，于是调仓扫描
对这只票直接返回 ``risk_handled=True`` 并整轮跳过——真实需要换仓的持仓被静默
抑制，而且没有任何报错。

本文件只钉这一件事：同一张 ``filled + verified`` 的卖单，``created_at`` 在今天
算"已卖出"，在明天**不算**。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_trading as PT  # noqa: E402
import rebalance_scanner as RBS  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600519"
STAMP_VERSION = 1
STAMP_CHECKSUM = "a" * 64


class RiskHandledWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="rb-window-")
        cls._old_path = PT.DB_PATH
        PT.DB_PATH = os.path.join(cls._tmp, "paper.sqlite3")
        PT.init_db()
        cls.conn = sqlite3.connect(PT.DB_PATH, timeout=30)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        PT.DB_PATH = cls._old_path
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        self.conn.execute("DELETE FROM paper_orders")
        self.conn.execute("DELETE FROM paper_cycles")
        stamp_now = self._stamp(dt.datetime.now())
        self.cycle_id = int(self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,"
            "started_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("test-cycle", "running", 1_000_000.0, "balanced",
             stamp_now, stamp_now, stamp_now),
        ).lastrowid)
        # paper_orders 的 INSERT 触发器要求"非 NULL 冻结戳"必须指向一条真实存在的
        # 不可变策略版本，所以先落一行版本，再用它对委托盖章。
        self.conn.execute(
            """INSERT OR REPLACE INTO paper_strategy_versions(
                   strategy_id,version,checksum,definition_json,name,implementation_key,
                   description,metadata,dsl_ast,created_at,created_by,change_note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ACCOUNT, 1, STAMP_CHECKSUM, "{}", "测试策略", ACCOUNT, "",
             "{}", "{}", stamp_now, "test", "test"),
        )
        self.conn.commit()

    @staticmethod
    def _stamp(moment):
        return moment.strftime("%Y-%m-%d %H:%M:%S")

    def _insert_verified_sell(self, created_at):
        self.conn.execute(
            """INSERT INTO paper_orders(
                   account_id,side,code,status,created_at,cycle_id,
                   strategy_id,strategy_version,strategy_checksum,risk_payload,
                   execution_verified,execution_status,filled_qty,qty)
               VALUES(?,?,?,?,?,?,?,?,?,'{}',1,'verified',100,100)""",
            (ACCOUNT, "sell", CODE, "filled", created_at, self.cycle_id,
             ACCOUNT, STAMP_VERSION, STAMP_CHECKSUM),
        )
        self.conn.commit()

    def _handled(self, today):
        return RBS._is_risk_handled(self.conn, ACCOUNT, CODE, today, self.cycle_id)

    def test_sell_recorded_today_counts(self):
        today = dt.date(2026, 10, 22)
        self._insert_verified_sell("2026-10-22 10:30:00")
        verdict = self._handled(today)
        self.assertTrue(verdict["handled"], verdict)
        self.assertIn("今日已卖出", verdict["reason"])

    def test_sell_recorded_tomorrow_does_not_count(self):
        """时钟超前写下的"未来行"不能被当成今天已经卖过。"""
        today = dt.date(2026, 10, 22)
        self._insert_verified_sell("2026-10-23 09:31:00")
        verdict = self._handled(today)
        self.assertFalse(
            verdict["handled"],
            "未来时间的卖单被算成今日已卖出，会静默跳过真实换仓: %r" % (verdict,),
        )

    def test_sell_recorded_yesterday_does_not_count(self):
        today = dt.date(2026, 10, 22)
        self._insert_verified_sell("2026-10-21 14:50:00")
        self.assertFalse(self._handled(today)["handled"])

    def test_unverified_sell_does_not_count(self):
        today = dt.date(2026, 10, 22)
        self.conn.execute(
            """INSERT INTO paper_orders(
                   account_id,side,code,status,created_at,cycle_id,
                   strategy_id,strategy_version,strategy_checksum,risk_payload,
                   execution_verified,execution_status,filled_qty,qty)
               VALUES(?,?,?,?,?,?,?,?,?,'{}',0,'unknown',100,100)""",
            (ACCOUNT, "sell", CODE, "filled", "2026-10-22 10:30:00", self.cycle_id,
             ACCOUNT, STAMP_VERSION, STAMP_CHECKSUM),
        )
        self.conn.commit()
        self.assertFalse(self._handled(today)["handled"])


if __name__ == "__main__":
    unittest.main()
