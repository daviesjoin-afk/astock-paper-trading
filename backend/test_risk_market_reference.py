# -*- coding: utf-8 -*-
"""盘中风控的指数参照新鲜度（2026-10-23 诊断修复）。

生产缺陷（`_cached_close_market`）
---------------------------------
旧实现取 ``paper_jobs`` 里 ``market_date<=day AND status='completed'`` 的最近一条
收盘市场。2026-10-23 的收盘任务链是 10/16 completed、10/17~10/22 全部 failed，
于是盘中卖出风控拿到的是 **10-16** 的市场状态（指数 +0.11%），把当日 -4.50% 的
华阳新材算成"相对沪深300 弱 4.61 个百分点"（风控记录 132026）。错误的相对强弱
会进入下跌防线、洗盘/出货分类和持仓质量复核。

本文件钉死的契约：

    MR-01  只有**同一交易日**的收盘记录可复用；一周前的 completed 记录不得代理
           当日指数（``_same_day_close_market`` 返回 None）。
    MR-02  当日记录里指数时间戳不属于当日 → 记录本身不可信，同样按"没有"处理。
    MR-03  没有当日记录时改用当日实时指数；拿不到/不属于当日 → 相对强弱为
           未知，且能说明原因（``unavailable`` / ``stale``）。
    MR-04  过期收盘数据仍作为 ``market_history`` 保留，标注 ``history_only``。
    MR-05  下跌防线的相对强弱在指数不可用时必须是 None，并且不给相对口径的
           预警贡献；指数新鲜时照常计算。
    MR-06  修正指数参照**不**放宽硬止损：硬止损链不消费市场状态。

纯 fixture：内存 sqlite、provider 全部打桩、不联网、不读写真实账本。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import paper_trading as PT  # noqa: E402

DAY = dt.date(2026, 10, 23)
OLD_DAY = dt.date(2026, 10, 16)
ACCOUNT = "main_force_top10"
CODE = "600281"
MARKET_NOW = {"light": "yellow", "reason": "fixture", "live_index_pct": -1.22,
              "live_index_price": 3820.0, "live_index_time": "20261023144242"}


def _close_job(conn, market_date, market):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS paper_jobs("
        "slot TEXT, market_date TEXT, status TEXT, detail TEXT)"
    )
    conn.execute(
        "INSERT INTO paper_jobs(slot,market_date,status,detail) VALUES('close',?,?,?)",
        (str(market_date), "completed", json.dumps({"market": market})),
    )


class CloseMarketFreshnessTests(unittest.TestCase):
    """MR-01 ~ MR-04 —— 当日参照只认当日记录，过期记录降级为历史背景。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def test_mr01_week_old_close_market_is_not_todays_reference(self):
        _close_job(self.conn, OLD_DAY, {
            "live_index_pct": 0.11, "live_index_time": "20261016151100",
            "light": "yellow",
        })
        self.assertIsNone(PT._same_day_close_market(self.conn, DAY))

    def test_mr02_same_day_job_with_foreign_index_timestamp_is_rejected(self):
        _close_job(self.conn, DAY, {
            "live_index_pct": 0.11, "live_index_time": "20261016151100",
            "light": "yellow",
        })
        self.assertIsNone(PT._same_day_close_market(self.conn, DAY))

    def test_mr02b_same_day_job_with_same_day_index_is_used(self):
        market = {"live_index_pct": 0.62, "live_index_time": "20261023150112",
                  "light": "yellow"}
        _close_job(self.conn, DAY, market)
        self.assertEqual(market, PT._same_day_close_market(self.conn, DAY))

    def test_mr03_missing_index_leaves_relative_strength_unknown(self):
        _close_job(self.conn, OLD_DAY, {
            "live_index_pct": 0.11, "live_index_time": "20261016151100",
        })
        # provider 只返回**别的交易日**的指数：不能当成当日基准。
        with mock.patch.object(PT.dfc, "fetch_indices", return_value=[{
            "code": "sh000300", "pct": 0.11, "price": 3800.0,
            "time": "20261016151100",
        }]):
            market = PT._cached_close_market(self.conn, DAY)
        self.assertIsNone(market["live_index_pct"])
        self.assertIsNone(market["live_index_time"])
        self.assertEqual("stale", market["index_reference"]["status"])
        self.assertEqual("unknown", market["light"])
        self.assertIn("相对强弱", market["reason"])

    def test_mr04_old_close_market_survives_only_as_history(self):
        _close_job(self.conn, OLD_DAY, {
            "live_index_pct": 0.11, "live_index_time": "20261016151100",
            "light": "yellow",
        })
        with mock.patch.object(PT.dfc, "fetch_indices",
                               side_effect=RuntimeError("source down")):
            market = PT._cached_close_market(self.conn, DAY)
        history = market["market_history"]
        self.assertEqual("2026-10-16", history["market_date"])
        self.assertEqual(0.11, history["live_index_pct"])
        self.assertEqual("history_only", history["status"])
        self.assertEqual("unavailable", market["index_reference"]["status"])


class SameSessionIndexTests(unittest.TestCase):
    """MR-03 —— 只有时间戳属于当日的指数才算当日参照。"""

    def _rows(self, time_stamp):
        return [{"code": "sh000300", "pct": -1.22, "price": 3820.0,
                 "time": time_stamp}]

    def test_same_session_index_is_accepted(self):
        with mock.patch.object(PT.dfc, "fetch_indices",
                               return_value=self._rows("20261023144242")):
            row, reference = PT._same_session_index(DAY, allow_network=True)
        self.assertIsNotNone(row)
        self.assertEqual("same_session", reference["status"])

    def test_other_session_index_is_reported_stale(self):
        with mock.patch.object(PT.dfc, "fetch_indices",
                               return_value=self._rows("20261022151100")):
            row, reference = PT._same_session_index(DAY, allow_network=True)
        self.assertIsNone(row)
        self.assertEqual("stale", reference["status"])
        self.assertIn("20261022151100", reference["reason"])

    def test_provider_failure_is_reported_unavailable(self):
        with mock.patch.object(PT.dfc, "fetch_indices",
                               side_effect=RuntimeError("source down")):
            row, reference = PT._same_session_index(DAY, allow_network=True)
        self.assertIsNone(row)
        self.assertEqual("unavailable", reference["status"])

    def test_no_network_means_no_reference(self):
        with mock.patch.object(PT.dfc, "fetch_indices") as fetch:
            row, reference = PT._same_session_index(DAY, allow_network=False)
        fetch.assert_not_called()
        self.assertIsNone(row)
        self.assertEqual("unavailable", reference["status"])


class GuardRelativeStrengthTests(unittest.TestCase):
    """MR-05 —— 指数不可用时相对强弱必须是未知，而不是拿旧值代算。"""

    POSITION = {"account_id": ACCOUNT, "code": CODE, "qty": 100,
                "today_acquired_qty": 0, "entry_date": "2026-10-20",
                "cost": 5.30, "peak_price": 5.30, "take_stage": None}
    #: 当日 -4.50%：只有相对强弱这一条能把它推成 partial，因此可以单独观察。
    QUOTE = {"price": 5.06, "high": 5.32, "low": 5.02, "pct": -4.50,
             "main_pct": -1.2, "vol_ratio": 1.05, "quote_at": "2026-10-23 14:40:08"}

    def _guard(self, market):
        return PT._intraday_downside_guard(
            self.POSITION, self.QUOTE, market=market, asof_day=DAY,
        )

    def test_stale_index_does_not_fabricate_relative_strength(self):
        stale_market = {"live_index_pct": None, "live_index_time": None,
                        "index_reference": {"status": "stale",
                                            "reason": "指数不属于当日"}}
        guard = self._guard(stale_market)
        self.assertIsNone(guard["relative_to_market_pct"])
        self.assertEqual("stale", guard["market_reference"]["status"])
        self.assertFalse(guard["market_reference"]["relative_available"])
        self.assertFalse(
            [item for item in guard["reason"].split("；") if "相对沪深300" in item],
            "指数不可用时不得出现相对强弱结论",
        )

    def test_fresh_index_still_computes_relative_strength(self):
        fresh_market = {**MARKET_NOW,
                        "index_reference": {"status": "same_session"}}
        guard = self._guard(fresh_market)
        self.assertAlmostEqual(-3.28, guard["relative_to_market_pct"], places=2)
        self.assertEqual("same_session", guard["market_reference"]["status"])
        self.assertTrue(guard["market_reference"]["relative_available"])


class HardStopIndependenceTests(unittest.TestCase):
    """MR-06 —— 修正指数参照不等于取消硬止损。"""

    SPEC = {"hard_stop": -0.04, "trail_after": 0.04, "trail_stop": 0.05,
            "hold_max": 15, "take_profit": [], "strategy_version": "test-risk-v1"}
    POSITION = {"account_id": ACCOUNT, "code": CODE, "qty": 100,
                "today_acquired_qty": 0, "entry_date": "2026-10-20",
                "cost": 5.30, "peak_price": 5.30, "take_stage": None}

    def test_hard_stop_ignores_the_market_context_entirely(self):
        quote = {"price": 5.06, "high": 5.32, "low": 5.02, "pct": -4.50,
                 "quote_at": "2026-10-23 14:40:08"}
        with mock.patch.object(PT, "_completed_kline", return_value=None):
            ratio, _reason, _stage, detail = PT._sell_plan(
                self.POSITION, quote, DAY, [], spec_override=dict(self.SPEC),
            )
        self.assertGreater(ratio, 0.0)
        self.assertEqual("hard_stop", detail["exit_class"])
        # 硬止损链的输入里没有市场状态：换任何指数参照都得到同一结论。
        with mock.patch.object(PT, "_completed_kline", return_value=None):
            again = PT._sell_plan(
                self.POSITION, quote, DAY, [], spec_override=dict(self.SPEC),
            )
        self.assertEqual(ratio, again[0])
        self.assertEqual(detail["exit_class"], again[3]["exit_class"])


if __name__ == "__main__":
    unittest.main()
