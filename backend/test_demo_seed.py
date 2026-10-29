# -*- coding: utf-8 -*-
"""Deterministic demo mode regression tests.

demo_seed.ensure_demo_data() must produce a fully synthetic universe plus a
narrated paper ledger that the real read models can serve: dashboard, activity
orders, risk audit alerts, position reviews and NAV.  Runs against a temporary
data directory so it never touches a real data_cache/.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

BACKEND = os.path.dirname(os.path.abspath(__file__))
import sys  # noqa: E402
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import data_fetcher as dfc  # noqa: E402
import universe as U  # noqa: E402
import paper_trading as PT  # noqa: E402
import demo_seed  # noqa: E402


class DeterministicDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-demo-test-")
        # Redirect every data path the pipeline touches into the temp dir.
        cls._patches = []
        for target, attr in (
            (dfc, "CACHE_DIR"), (dfc, "KLINE_DIR"), (dfc, "MARKET_SNAPSHOT_FULL_CACHE_PATH"),
            (U, "UNIVERSE_PATH"), (PT, "DB_PATH"),
        ):
            old = getattr(target, attr)
            cls._patches.append((target, attr, old))
            if attr == "UNIVERSE_PATH":
                setattr(target, attr, os.path.join(cls._tmp, "universe.json"))
            elif attr == "MARKET_SNAPSHOT_FULL_CACHE_PATH":
                setattr(target, attr, os.path.join(cls._tmp, "market_snapshot_full.json"))
            elif attr == "DB_PATH":
                setattr(target, attr, os.path.join(cls._tmp, "paper_trading.sqlite3"))
            else:
                setattr(target, attr, cls._tmp)
        # dashboard() refreshes schedule_status() once per cache miss; on a
        # Windows sandbox that shells out to schtasks.exe and hangs.  The demo
        # test only cares about ledger/read-model content, not host schedules.
        cls._patches.append((PT, "schedule_status", PT.schedule_status))
        PT.schedule_status = staticmethod(lambda: {"scheduler": "demo", "enabled": False})

    @classmethod
    def tearDownClass(cls):
        for target, attr, old in cls._patches:
            setattr(target, attr, old)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        # Every test starts from a freshly force-seeded demo ledger so order
        # of execution can never leak state between cases.
        self.assertTrue(demo_seed.ensure_demo_data(force=True))

    def _read_marker_count(self, event):
        with PT._db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM paper_audit WHERE event=?", (event,)
            ).fetchone()[0]

    def test_seed_writes_synthetic_universe(self):
        with open(U.UNIVERSE_PATH, encoding="utf-8") as handle:
            universe = json.load(handle)
        self.assertEqual(universe["scope"], "demo_synthetic")
        self.assertEqual(len(universe["stocks"]), 10)
        self.assertEqual(universe["stocks"][0]["name"], "晨星锂电")
        # A +9.98% limit-up ticker must be present for the demo narrative.
        self.assertTrue(any(s["pct"] >= 9.9 for s in universe["stocks"]))

    def test_ledger_contains_full_event_chain(self):
        with PT._db() as conn:
            fills = conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
            rejected = conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE status='risk_rejected'"
            ).fetchone()[0]
            self.assertGreaterEqual(fills, 5, "demo needs several fills")
            self.assertGreaterEqual(rejected, 2, "T+1 and stale-quote rejections")
            decisions = {
                row["decision"] for row in PT._rows(
                    conn, "SELECT decision FROM paper_risk_decisions"
                )
            }
            for expected in ("entry_approved", "t1_rejected", "stale_quote_rejected", "hard_stop_sell"):
                self.assertIn(expected, decisions, expected)
            positions = conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
            self.assertGreaterEqual(positions, 3, "open holdings for the portfolio view")
            reviews = {
                row["action"] for row in PT._rows(
                    conn, "SELECT action FROM paper_position_reviews"
                )
            }
            self.assertIn("sell", reviews)
            self.assertIn("hold", reviews)
            nav_days = conn.execute(
                "SELECT COUNT(DISTINCT nav_date) FROM paper_nav"
            ).fetchone()[0]
            self.assertGreaterEqual(nav_days, 5, "NAV curve history")

    def test_idempotent_reseed_does_not_duplicate(self):
        self.assertFalse(demo_seed.ensure_demo_data(), "second call must be a no-op")
        with PT._db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        self.assertEqual(self._read_marker_count("demo_seeded"), 1)
        # force-reseed archives the old ledger first (deterministic replay)
        self.assertTrue(demo_seed.ensure_demo_data(force=True))
        with PT._db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
                count, "force reseed must not grow the ledger",
            )

    def test_read_models_serve_demo_ledger(self):
        # dashboard with activity + history must not raise and must expose the
        # narrated events through the real serving read models.
        overview = PT.dashboard(include_activity=True, include_history_symbols=True)
        codes = {str(pos["code"]) for pos in overview["positions"]}
        self.assertTrue(codes & {"600901", "600902", "600904", "600906"}, codes)
        order_codes = {
            str(o.get("code")) for o in overview.get("orders", [])
            if o.get("code")
        }
        self.assertIn("600905", order_codes)  # hard-stop sell
        self.assertIn("600904", order_codes)  # T+1 rejected sell
        audit = PT.risk_audit(limit=100)
        alert_text = json.dumps(audit, ensure_ascii=False)
        for needle in ("晨星锂电", "汇联医疗", "极光智能"):
            self.assertIn(needle, alert_text, needle)
        self.assertIn("T+1", alert_text)
        self.assertIn("硬止损", alert_text)

    def test_missing_holding_quote_does_not_publish_partial_pool_daily_pnl(self):
        # Run the real dashboard and account projections over the demo ledger.
        # Even if the legacy pool calculation returns a number, one holding
        # without usable daily evidence cannot be reported as complete P&L.
        original_shared = PT._shared_metrics

        def shared_with_legacy_value(*args, **kwargs):
            result = original_shared(*args, **kwargs)
            result.update(today_pnl=123.0, today_return_pct=1.23,
                          today_baseline_nav=10000.0, today_pnl_available=True,
                          daily_loss_pct=3.2)
            return result

        session = {"code": "trading", "label": "交易中", "today_pnl_available": True}
        with patch.object(PT, "_market_session", return_value=session), \
             patch.object(PT, "_today_position_performance", return_value=(None, None, None)), \
             patch.object(PT, "_shared_metrics", side_effect=shared_with_legacy_value):
            overview = PT.dashboard()
        self.assertTrue(overview["positions"])
        self.assertTrue(any(a["today_position_missing_codes"] for a in overview["accounts"]))
        self.assertEqual(overview["shared"]["ledger_today_pnl"], 123.0)
        self.assertIsNone(overview["shared"]["today_pnl"])
        self.assertIsNone(overview["shared"]["today_return_pct"])
        self.assertFalse(overview["shared"]["today_pnl_available"])
        self.assertEqual(overview["shared"]["today_pnl_status"], "暂无完整行情")
        self.assertIsNone(overview["shared"]["daily_loss_pct"])
        for account in overview["accounts"]:
            if account["today_position_missing_codes"] or account["today_sell_missing_codes"]:
                self.assertIsNone(account["daily_loss_pct"])

    def test_risk_dashboard_does_not_publish_stale_daily_loss_when_quote_missing(self):
        original_shared = PT._shared_metrics

        def shared_with_legacy_value(*args, **kwargs):
            result = original_shared(*args, **kwargs)
            result.update(today_pnl=-320.0, today_return_pct=-3.2,
                          today_baseline_nav=10000.0, today_pnl_available=True,
                          daily_loss_pct=3.2)
            return result

        session = {"code": "trading", "label": "交易中", "today_pnl_available": True}
        snapshot = {"asof": "2026-10-27T10:00:00+08:00", "market": {"light": "green"},
                    "data_quality": [], "dynamic_risk": {"mode": "normal"}, "news": {}}
        with patch.object(PT, "_market_session", return_value=session), \
             patch.object(PT, "_today_position_performance", return_value=(None, None, None)), \
             patch.object(PT, "_shared_metrics", side_effect=shared_with_legacy_value), \
             patch.object(PT.RC, "load_snapshot", return_value=snapshot), \
             patch.object(PT.RC, "snapshot_age_seconds", return_value=1), \
             patch.object(PT.dfc, "load_source_health", return_value={}):
            base = PT._risk_base_dashboard()
            result = PT.risk_dashboard(allow_network=False)
        self.assertTrue(base["positions"])
        self.assertTrue(any(a["today_position_missing_codes"] for a in base["accounts"]))
        self.assertIsNone(base["shared"]["daily_loss_pct"])
        for account in base["accounts"]:
            if account["today_position_missing_codes"] or account["today_sell_missing_codes"]:
                self.assertIsNone(account["daily_loss_pct"])
        self.assertTrue(result["accounts"])
        self.assertIsNone(result["shared"]["daily_loss_pct"])
        self.assertIsNone(result["shared"]["today_pnl"])
        self.assertIsNone(result["shared"]["today_return_pct"])
        self.assertFalse(result["shared"]["today_pnl_available"])
        self.assertEqual(result["shared"]["today_pnl_status"], "暂无完整行情")
        for account in result["accounts"]:
            self.assertIsNone(account["metrics"]["daily_loss_pct"])

    def test_risk_reader_loads_cached_quote_for_today_fully_sold_symbol(self):
        requested = []

        def cached_quotes(codes):
            requested.extend(codes)
            return {code: {"price": 10.0, "pct": 0.0, "previous_close": 10.0,
                           "quote_at": "2026-10-27T10:00:00+08:00",
                           "quote_source": "dashboard_cache"} for code in codes}

        session = {"code": "trading", "label": "交易中", "today_pnl_available": True}
        with patch.object(PT, "_market_session", return_value=session), \
             patch.object(PT, "_today_position_performance", return_value=(20.0, 2.0, 1000.0)), \
             patch.object(PT.PPerf, "quote_is_usable", return_value=True), \
             patch.object(PT.dfc, "load_market_snapshot_full_cached", return_value=[]), \
             patch.object(PT, "_latest_price_map", side_effect=cached_quotes), \
             patch.object(PT, "_quotes", side_effect=AssertionError("unexpected network")):
            base = PT._risk_base_dashboard()
        self.assertIn("600905", requested)
        self.assertNotIn("600905", {position["code"] for position in base["positions"]})
        account = next(row for row in base["accounts"] if row["id"] == "tq_breakout")
        self.assertEqual(account["today_sell_quote_coverage"], "1/1")
        self.assertEqual(account["today_sell_pnl"], 37106.46)
        self.assertEqual(account["today_position_missing_codes"], [])
        self.assertIsNotNone(base["shared"]["daily_loss_pct"])

    def test_narrowed_strategy_registry_skips_seeding_instead_of_crashing(self):
        """策略账户不足三个时必须**跳过播种**，不能在启动阶段抛 IndexError。

        2026-10-29 实测缺陷：``ensure_demo_data`` 硬取 ``accounts[1]`` /
        ``accounts[2]``，当策略注册表被收窄（只启用 1~2 个策略）时它在应用
        lifespan 里抛 ``IndexError``，uvicorn 启动直接失败——
        ``test_operator_boundary`` 的 4 个用例因此 setup 超时。这里钉住新行为：
        返回 False、留下可查的审计证据、不写坏账本。
        """
        real_create_cycle = PT.create_cycle

        def narrow_create_cycle(*args, **kwargs):
            # 仍然真的建周期（否则共享账本会停在"没有活动周期"，后续用例的
            # 读模型会去补 legacy 周期，键精确到秒，同一秒内两次就撞 UNIQUE），
            # 只把返回的 enabled_strategies 收窄成 1 个。
            cycle = real_create_cycle(*args, **kwargs)
            return {**cycle, "enabled_strategies": ["tq_breakout"]}

        with patch.object(PT, "create_cycle", side_effect=narrow_create_cycle):
            self.assertFalse(demo_seed.ensure_demo_data(force=True))
        with PT._db() as conn:
            row = conn.execute(
                "SELECT detail FROM paper_audit WHERE event='demo_seed_skipped' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row, "跳过播种必须留下审计证据")
        payload = json.loads(row[0] if not isinstance(row, dict) else row["detail"])
        self.assertEqual(["tq_breakout"], payload["enabled_strategies"])
        self.assertIn("三个策略账户", payload["reason"])


if __name__ == "__main__":
    unittest.main()
