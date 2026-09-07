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


if __name__ == "__main__":
    unittest.main()
