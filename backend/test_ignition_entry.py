"""主力点火入场通道 + 观察池冻结 + 同日峰值口径的回归测试（2026-08-31）。"""
from __future__ import annotations

import datetime as dt
import sqlite3
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import paper_trading as P  # noqa: E402
import entry_timing as ET  # noqa: E402
import ignition_entry as IGN  # noqa: E402


def _minute_series(volumes, prices):
    return [
        {"time": f"09{30 + i // 60:02d}{(30 + i) % 60:02d}", "price": p, "volume": v}
        for i, (v, p) in enumerate(zip(volumes, prices, strict=False))
    ]


class IgnitionEntryTests(unittest.TestCase):
    def test_out_of_zone_rejected_without_any_data(self):
        result = IGN.evaluate_ignition("000001", pct=2.0)
        self.assertFalse(result["passed"])

    def test_missing_data_is_fail_closed(self):
        # 点火区间内但没有任何分钟/资金/盘口证据 → 不通过
        result = IGN.evaluate_ignition(
            "000001", pct=5.0,
            micro=None, flow=None, minute_series=None,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("缺失" in reason or "不足" in reason for reason in result["reasons"]))

    def _full(self, tail_volumes, tail_prices, prepend_prices):
        """真实盘中分钟线远多于 12 根；用 prepend 垫底，尾部为待测结构。"""
        n = len(prepend_prices)
        volumes = [500] * n + list(tail_volumes)
        prices = list(prepend_prices) + list(tail_prices)
        return _minute_series(volumes, prices)

    def test_full_evidence_passes(self):
        # 前5根量平稳（800±），最近2根放量 2000 = 2.5×中位；
        # prepend 段构造两个逐级抬高的局部低点（10.01 → 10.02 → 10.03）
        prepend = [10.0, 10.04, 10.01, 10.06, 10.02, 10.08, 10.03, 10.1]
        volumes = [800, 820, 780, 810, 800, 830, 2000, 1900]
        prices = [10.12, 10.13, 10.14, 10.15, 10.16, 10.17, 10.18, 10.19]
        series = self._full(volumes, prices, prepend)
        micro = {"vwap_deviation_pct": 0.35, "active_buy_sell_imbalance": 0.10, "depth_imbalance": 0.05}
        flow = {"main_delta_5m": 80000.0, "positive_persistence_10m": 0.75}
        result = IGN.evaluate_ignition("000001", pct=5.0, limit_pct=10.0,
                                       micro=micro, flow=flow, minute_series=series)
        self.assertTrue(result["passed"], result["reasons"])

    def test_volume_surge_missing_fails(self):
        prepend = [10.0, 10.04, 10.01, 10.06, 10.02, 10.08, 10.03, 10.1]
        volumes = [800, 820, 780, 810, 800, 830, 900, 950]  # 无点火放量
        prices = [10.12, 10.13, 10.14, 10.15, 10.16, 10.17, 10.18, 10.19]
        series = self._full(volumes, prices, prepend)
        micro = {"vwap_deviation_pct": 0.35, "active_buy_sell_imbalance": 0.10, "depth_imbalance": 0.05}
        flow = {"main_delta_5m": 80000.0, "positive_persistence_10m": 0.75}
        result = IGN.evaluate_ignition("000001", pct=5.0, limit_pct=10.0,
                                       micro=micro, flow=flow, minute_series=series)
        self.assertFalse(result["passed"])
        self.assertTrue(any("点火量" in r for r in result["reasons"]))

    def test_lows_not_rising_fails(self):
        # 最后两个局部低点 10.2 → 10.0 逐级降低，且点火量/盘口/资金均达标
        prepend = [10.6, 10.55, 10.5, 10.45]  # 单调下行，不产生额外低点
        volumes = [800, 820, 780, 810, 800, 830, 2000, 1900]
        prices = [10.4, 10.2, 10.5, 10.3, 10.0, 10.3, 10.5, 10.6]  # 尾段单调上行，不产生新低点
        series = self._full(volumes, prices, prepend)
        micro = {"vwap_deviation_pct": 0.35, "active_buy_sell_imbalance": 0.10, "depth_imbalance": 0.05}
        flow = {"main_delta_5m": 80000.0, "positive_persistence_10m": 0.75}
        result = IGN.evaluate_ignition("000001", pct=5.0, limit_pct=10.0,
                                       micro=micro, flow=flow, minute_series=series)
        self.assertFalse(result["passed"])
        self.assertTrue(any("低点" in r for r in result["reasons"]), result["reasons"])

    def test_near_limit_rejected(self):
        # 区间内（≤7.5）但已接近涨停价（limit-1.0=7.0）→ 拒绝
        result = IGN.evaluate_ignition("000001", pct=7.4, limit_pct=8.0)
        self.assertFalse(result["passed"])
        self.assertTrue(any("涨停" in r for r in result["reasons"]))


class EntryTimingMainForceTests(unittest.TestCase):
    def setUp(self):
        ET.reset()

    def tearDown(self):
        ET.reset()

    def test_main_force_profile_exists_with_ignition_zone(self):
        profile = ET.PROFILES["main_force_top10"]
        self.assertEqual(profile["ignition_zone"], (3.5, 7.5))
        self.assertEqual(profile["ignition_confirm_scans"], 2)

    def test_normal_lane_requires_two_confirms(self):
        now = dt.datetime(2026, 8, 31, 10, 0, 0)
        allowed, info = ET.evaluate("main_force_top10", "000001", 10.0, 2.0, now=now)
        self.assertFalse(allowed)
        self.assertEqual(info["state"], "triggered")
        allowed, info = ET.evaluate("main_force_top10", "000001", 10.0, 2.0,
                                    now=now + dt.timedelta(seconds=70))
        self.assertTrue(allowed, info)
        self.assertEqual(info["state"], "confirmed")

    def test_ignition_mode_requires_ignition_ok_in_fast_path(self):
        now = dt.datetime(2026, 8, 31, 10, 0, 0)
        base_ev = {
            "cross_source_checked": True, "main_pct": 5.0, "vol_ratio": 2.0,
            "active_buy_sell_imbalance": 0.1, "depth_imbalance": 0.1,
        }
        # 首次触发（点火区间）
        allowed, info = ET.evaluate("main_force_top10", "000002", 10.5, 5.0, now=now)
        self.assertFalse(allowed)
        self.assertEqual(info["record"]["mode"], "ignition")
        # 快速确认：缺 ignition_ok → 不放行
        ev_no_ignition = dict(base_ev, ignition_ok=False, ignition_detail="资金持续率不足")
        allowed, _ = ET.evaluate("main_force_top10", "000002", 10.5, 5.0,
                                 now=now + dt.timedelta(seconds=5), fast=True,
                                 evidence=ev_no_ignition)
        self.assertFalse(allowed)
        allowed, _ = ET.evaluate("main_force_top10", "000002", 10.5, 5.0,
                                 now=now + dt.timedelta(seconds=35), fast=True,
                                 evidence=ev_no_ignition)
        self.assertFalse(allowed)
        # 两次相隔≥30秒且 ignition_ok=True → 放行
        allowed, _ = ET.evaluate("main_force_top10", "000002", 10.5, 5.0,
                                 now=now + dt.timedelta(seconds=70), fast=True,
                                 evidence=dict(base_ev, ignition_ok=True))
        self.assertFalse(allowed)  # 第一次严格确认
        allowed, info = ET.evaluate("main_force_top10", "000002", 10.5, 5.0,
                                    now=now + dt.timedelta(seconds=105), fast=True,
                                    evidence=dict(base_ev, ignition_ok=True))
        self.assertTrue(allowed, info)
        self.assertEqual(info["record"]["confirmation_path"], "fast_watch")


class MainForceWatchPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmpdir.name, "pool.json")
        self._orig = P.MAIN_FORCE_WATCH_POOL_PATH
        P.MAIN_FORCE_WATCH_POOL_PATH = self.path

    def tearDown(self):
        P.MAIN_FORCE_WATCH_POOL_PATH = self._orig
        self.tmpdir.cleanup()

    def _picks(self, codes):
        return [{"code": c, "score": 1.0 - i * 0.01} for i, c in enumerate(codes)]

    def test_first_scan_freezes_pool_of_ten(self):
        picks = self._picks([f"{100000 + i}" for i in range(13)])
        frozen, meta = P._main_force_watch_pool(picks)
        self.assertEqual(len(frozen), 10)
        self.assertEqual(meta["pool"], [f"{100000 + i}" for i in range(10)])

    def test_max_two_replacements_per_day(self):
        day1 = self._picks([f"{100000 + i}" for i in range(13)])
        frozen, _ = P._main_force_watch_pool(day1)
        # 第二轮：榜单尾部 3 只全部掉出 buffer，3 只新面孔进入
        day2_codes = [f"{100100 + i}" for i in range(3)] + [
            c for c in [f"{100000 + i}" for i in range(13)] if c not in {
                f"{100000 + 10}", f"{100000 + 11}", f"{100000 + 12}"
            }
        ]
        day2 = self._picks(day2_codes)
        merged, meta = P._main_force_watch_pool(day2)
        self.assertLessEqual(len(meta["replaced_today"]), 2)
        # 池仍为 10 只：最多 2 只被换入
        new_in = {row["in"] for row in meta["replaced_today"]}
        self.assertEqual(len(new_in), len(meta["replaced_today"]))

    def test_fail_open_on_corrupt_state(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{corrupt")
        picks = self._picks([f"{100000 + i}" for i in range(12)])
        frozen, meta = P._main_force_watch_pool(picks)
        self.assertEqual(len(frozen), 10)  # 状态损坏视为首次冻结


class SameDayPeakTests(unittest.TestCase):
    def test_bought_today_detection(self):
        day = dt.date(2026, 8, 31)
        pos_new = {"qty": 100, "today_acquired_qty": 100, "entry_date": "2026-08-31"}
        pos_old = {"qty": 100, "today_acquired_qty": 100, "entry_date": "2026-08-28"}
        pos_add = {"qty": 200, "today_acquired_qty": 100, "entry_date": "2026-08-28"}
        self.assertTrue(P._bought_today(pos_new, day))
        self.assertFalse(P._bought_today(pos_old, day))
        self.assertFalse(P._bought_today(pos_add, day))

    def test_same_day_position_ignores_pre_buy_high(self):
        # 买入前日内最高 3.62，买入价 3.40：同日新仓不得把 3.62 计入峰值
        day = dt.date(2026, 8, 31)
        pos = {"qty": 100, "today_acquired_qty": 100, "entry_date": "2026-08-31",
               "peak_price": 3.40}
        quote = {"high": 3.62}
        self.assertEqual(P._position_peak(pos, quote, 3.40, day), 3.40)

    def test_next_day_restores_full_intraday_high(self):
        day = dt.date(2026, 8, 28)
        pos = {"qty": 100, "today_acquired_qty": 0, "entry_date": "2026-08-28",
               "peak_price": 3.40}
        quote = {"high": 3.62}
        self.assertEqual(P._position_peak(pos, quote, 3.40, day), 3.62)

    def test_partial_add_keeps_full_high_scope(self):
        day = dt.date(2026, 8, 31)
        pos = {"qty": 200, "today_acquired_qty": 100, "entry_date": "2026-08-28",
               "peak_price": 3.40}
        quote = {"high": 3.62}
        self.assertEqual(P._position_peak(pos, quote, 3.40, day), 3.62)


class IgnitionShadowMigrationTests(unittest.TestCase):
    """既有账本走 init_db 快路径，新增表必须有独立幂等迁移。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_migration_creates_table_on_existing_ledger(self):
        self.conn.execute(
            "CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, status TEXT)"
        )
        P._ensure_ignition_shadow_table(self.conn)
        names = [row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        self.assertIn("paper_ignition_shadow", names)

    def test_migration_is_idempotent(self):
        P._ensure_ignition_shadow_table(self.conn)
        P._ensure_ignition_shadow_table(self.conn)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='paper_ignition_shadow'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_insert_and_backfill_shape(self):
        P._ensure_ignition_shadow_table(self.conn)
        self.conn.execute(
            """INSERT INTO paper_ignition_shadow(day,bucket,code,recorded_at,
                   price,pct,old_rule_passed,ignition_passed)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("2026-08-31", "intraday:202608311030", "000965",
             "2026-08-31T10:30:00", 3.40, 5.2, 0, 1),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT old_rule_passed, ignition_passed, price, resolved FROM paper_ignition_shadow"
        ).fetchone()
        self.assertEqual(row, (0, 1, 3.40, 0))


class MainForceGateRoutingTests(unittest.TestCase):
    def test_ignition_zone_routes_instead_of_rejecting(self):
        account = {"id": P.MAIN_FORCE_STRATEGY_ID}
        pick = {"code": "000965", "pct": 5.5}
        quote = {"pct": 5.5, "price": 10.55, "open_price": 10.0}
        allowed, reason = P._new_entry_price_gate(account, pick, quote)
        self.assertTrue(allowed, reason)
        self.assertTrue(pick.get("ignition_lane"))

    def test_above_ignition_high_still_rejected(self):
        account = {"id": P.MAIN_FORCE_STRATEGY_ID}
        pick = {"code": "000965", "pct": 8.2}
        quote = {"pct": 8.2, "price": 10.82, "open_price": 10.0}
        allowed, reason = P._new_entry_price_gate(account, pick, quote)
        # >+7.5% 仍被拒：开盘追高上限或点火区间上限任一口径
        self.assertFalse(allowed)
        self.assertTrue("追高上限" in reason or "7.5" in reason, reason)

    def test_open_runup_no_longer_blocks_ignition_zone(self):
        # 旧规则：现价较开盘 > 3.5% 直接拒绝；点火区间内改为路由
        account = {"id": P.MAIN_FORCE_STRATEGY_ID}
        pick = {"code": "000965", "pct": 6.0}
        quote = {"pct": 6.0, "price": 10.80, "open_price": 10.0}  # runup 8%
        allowed, reason = P._new_entry_price_gate(account, pick, quote)
        self.assertTrue(allowed, reason)

    def test_normal_zone_below_35_unchanged(self):
        account = {"id": P.MAIN_FORCE_STRATEGY_ID}
        pick = {"code": "000965", "pct": 2.0}
        quote = {"pct": 2.0, "price": 10.20, "open_price": 10.0}
        allowed, reason = P._new_entry_price_gate(account, pick, quote)
        self.assertTrue(allowed, reason)
        self.assertNotIn("ignition_lane", pick)


if __name__ == "__main__":
    unittest.main()
