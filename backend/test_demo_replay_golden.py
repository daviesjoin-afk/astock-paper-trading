# -*- coding: utf-8 -*-
"""Demo golden replay: the seeded ledger must be byte-identical across replays.

Issue #28 turns the deterministic demo from a showcase into a regression
contract.  Two independent force-reseeds of ``demo_seed.ensure_demo_data()``
must produce exactly the same structural ledger summary:

  orders / fills / risk decisions / positions / reviews / NAV / audit events

The summary deliberately excludes wall-clock fields (created_at, nav_date,
entry_date, autoincrement ids) and keeps everything structural (prices, qty,
status, decision kinds, NAV values).  The canonical JSON string of the summary
is compared byte-for-byte against the committed golden constant, so any drift
in the demo narrative fails CI with a readable diff instead of silently
changing what a fresh clone shows.

Runs fully offline against a temporary data directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import data_fetcher as dfc  # noqa: E402
import universe as U  # noqa: E402
import paper_trading as PT  # noqa: E402
import demo_seed  # noqa: E402

# Golden ledger summary (canonical JSON).  Regenerate only when the demo
# narrative intentionally changes: run this module with --print to emit the
# new constant, review the diff, and update it together with docs/DEMO.md.
GOLDEN = (
    "{\"audit_events\":[[\"capital_configured\",1],[\"cycle_archived\",1],[\"cycle_created\",1],[\"demo_seeded\","
    "1],[\"main_force_activation_repaired\",1]],\"cycles\":[[\"archived\",100000.0],[\"running\",1000000.0]],\"de"
    "cisions\":[[0,\"600901\",\"buy\",\"entry_approved\",\"晨星锂电 通过行情/资金/T+1/风控门禁，限价 18.5 元成交\"],[0,\"600904\",\"b"
    "uy\",\"entry_approved\",\"逐日新材 今日买入成交\"],[0,\"600904\",\"sell\",\"t1_rejected\",\"逐日新材 T+1 门禁拒绝：当日买入不可当日卖出\""
    "],[1,\"600902\",\"buy\",\"entry_approved\",\"蓝湾数据 通过行情/资金/T+1/风控门禁，限价 45.2 元成交\"],[1,\"600903\",\"buy\",\"st"
    "ale_quote_rejected\",\"海岳风能 行情快照已过期，拒绝基于陈旧价格的委托\"],[1,\"600905\",\"buy\",\"entry_approved\",\"汇联医疗 60.00 元建仓"
    "\"],[1,\"600905\",\"sell\",\"hard_stop_sell\",\"汇联医疗 触及硬止损，56.40 元清仓离场（已实现亏损 -2880 元）\"],[2,\"600906\",\"buy"
    "\",\"entry_approved\",\"极光智能 +9.98% 涨停候选通过门禁，27.50 元成交\"]],\"fills\":[[0,\"600901\",\"buy\",2000,18.5,\"snap"
    "shot_price_rule\"],[1,\"600902\",\"buy\",300,45.2,\"snapshot_price_rule\"],[2,\"600904\",\"buy\",400,22.1,\""
    "snapshot_price_rule\"],[5,\"600905\",\"buy\",800,60.0,\"snapshot_price_rule\"],[6,\"600905\",\"sell\",800,56"
    ".4,\"snapshot_price_rule\"],[7,\"600906\",\"buy\",1200,27.5,\"snapshot_price_rule\"]],\"nav\":[[0,0,200200.0"
    ",198000.0],[0,1,200000.0,198237.6],[0,2,199600.0,198475.2],[0,3,200600.0,198712.8],[0,4,200000.0,null],[1,0,"
    "200400.0,198000.0],[1,1,200200.0,198237.6],[1,2,200800.0,198475.2],[1,3,201200.0,198712.8],[1,4,200000.0,nul"
    "l],[2,0,200000.0,198000.0],[2,1,200200.0,198237.6],[2,2,200400.0,198475.2],[2,3,200600.0,198712.8],[2,4,2000"
    "00.0,null],[3,0,200200.0,198000.0],[3,1,200200.0,198237.6],[3,2,200200.0,198475.2],[3,3,200200.0,198712.8],["
    "3,4,200000.0,null],[4,0,200200.0,198000.0],[4,1,200400.0,198237.6],[4,2,200000.0,198475.2],[4,3,199800.0,198"
    "712.8],[4,4,200000.0,null]],\"orders\":[[0,\"600901\",\"buy\",\"filled\",2000,18.5,18.5],[0,\"600904\",\"buy"
    "\",\"filled\",400,22.1,22.1],[0,\"600904\",\"sell\",\"risk_rejected\",400,22.4,null],[1,\"600902\",\"buy\","
    "\"filled\",300,45.2,45.2],[1,\"600903\",\"buy\",\"risk_rejected\",5000,9.62,null],[1,\"600905\",\"buy\",\"fi"
    "lled\",800,60.0,60.0],[1,\"600905\",\"sell\",\"filled\",800,56.4,56.4],[2,\"600906\",\"buy\",\"filled\",1200"
    ",27.5,27.5]],\"positions\":[[0,\"600901\",\"晨星锂电\",\"合成\",2000,18.5],[0,\"600904\",\"逐日新材\",\"新材料\",400,22.1"
    "],[1,\"600902\",\"蓝湾数据\",\"合成\",300,45.2],[2,\"600906\",\"极光智能\",\"算力服务\",1200,27.5]],\"reviews\":[[0,\"6009"
    "01\",7.5,\"A\",\"hold\",37000.0,3.7],[1,\"600905\",2.0,\"D\",\"sell\",45120.0,4.5]]}"
)


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _account_index_map(conn):
    rows = PT._rows(conn, "SELECT id FROM paper_accounts ORDER BY id")
    return {row["id"]: idx for idx, row in enumerate(rows)}


def _order_index_map(conn):
    rows = PT._rows(conn, "SELECT id FROM paper_orders ORDER BY id")
    return {row["id"]: idx for idx, row in enumerate(rows)}


def _summary() -> dict:
    """Structural, wall-clock-free summary of the demo ledger."""
    with PT._db() as conn:
        acc = _account_index_map(conn)
        order_idx = _order_index_map(conn)
        orders = sorted(
            [acc[o["account_id"]], str(o["code"]), o["side"], o["status"],
             o["qty"], o["planned_price"], o["filled_price"]]
            for o in PT._rows(conn, "SELECT * FROM paper_orders")
        )
        fills = sorted(
            [order_idx[f["order_id"]], str(f["code"]), f["side"],
             f["qty"], f["price"], f["assumption"]]
            for f in PT._rows(conn, "SELECT * FROM paper_fills")
        )
        decisions = sorted(
            [acc[d["account_id"]], str(d["code"]), d["side"],
             d["decision"], d["reason"]]
            for d in PT._rows(conn, "SELECT * FROM paper_risk_decisions")
        )
        positions = sorted(
            [acc[p["account_id"]], str(p["code"]), p["name"], p["industry"],
             p["qty"], p["cost"]]
            for p in PT._rows(conn, "SELECT * FROM paper_positions")
        )
        reviews = sorted(
            [acc[r["account_id"]], str(r["code"]), r["score"], r["grade"],
             r["action"], r["market_value"], r["position_pct"]]
            for r in PT._rows(conn, "SELECT * FROM paper_position_reviews")
        )
        nav_rows = PT._rows(
            conn, "SELECT * FROM paper_nav ORDER BY account_id, nav_date"
        )
        nav_days = {}
        nav = []
        for row in nav_rows:
            day = nav_days.setdefault(row["nav_date"], len(nav_days))
            nav.append([acc[row["account_id"]], day, row["nav"], row["benchmark"]])
        audit_events = sorted(
            [row["event"], row["n"]]
            for row in PT._rows(
                conn,
                "SELECT event, COUNT(*) AS n FROM paper_audit GROUP BY event",
            )
        )
        cycle = sorted(
            [c["status"], c.get("initial_capital", c.get("capital", 0.0))]
            for c in PT._rows(conn, "SELECT * FROM paper_cycles")
        )
    return {
        "orders": orders, "fills": fills, "decisions": decisions,
        "positions": positions, "reviews": reviews, "nav": nav,
        "audit_events": audit_events, "cycles": cycle,
    }


class DemoReplayGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-demo-golden-")
        cls._patches = []
        for target, attr in (
            (dfc, "CACHE_DIR"), (dfc, "MARKET_SNAPSHOT_FULL_CACHE_PATH"),
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
        # Avoid host scheduler probing (schtasks) on Windows sandboxes.
        cls._patches.append((PT, "schedule_status", PT.schedule_status))
        PT.schedule_status = staticmethod(lambda: {"scheduler": "demo", "enabled": False})

    @classmethod
    def tearDownClass(cls):
        for target, attr, old in cls._patches:
            setattr(target, attr, old)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _fresh_seed(self):
        """Reset to an empty schema, then seed — mirrors a fresh clone.

        force-reseed on an existing DB archives the old cycle and appends new
        audit rows, which accumulates history by design.  The golden contract
        is about what a *fresh clone* shows, so each replay starts from a
        deleted DB file + init_db().
        """
        if os.path.exists(PT.DB_PATH):
            os.remove(PT.DB_PATH)
        PT.init_db()
        self.assertTrue(demo_seed.ensure_demo_data(force=True))

    def test_replays_are_byte_identical(self):
        self._fresh_seed()
        first = _canonical(_summary())
        self._fresh_seed()
        second = _canonical(_summary())
        self.assertEqual(
            first, second,
            "two fresh replays produced different ledgers (issue #28)",
        )

    def test_matches_committed_golden(self):
        self._fresh_seed()
        actual = _canonical(_summary())
        if actual != GOLDEN and "--print" in sys.argv:
            print(actual)
        self.assertEqual(
            actual, GOLDEN,
            "demo ledger drifted from the golden replay; if the narrative "
            "change is intended, regenerate GOLDEN via "
            "`python backend/test_demo_replay_golden.py --print` and review "
            "the diff (issue #28)",
        )
        self.assertEqual(
            hashlib.sha256(actual.encode("utf-8")).hexdigest(),
            hashlib.sha256(GOLDEN.encode("utf-8")).hexdigest(),
        )


if __name__ == "__main__":
    if "--print" in sys.argv:
        tmp = tempfile.mkdtemp(prefix="astock-golden-print-")
        dfc.CACHE_DIR = tmp
        dfc.MARKET_SNAPSHOT_FULL_CACHE_PATH = os.path.join(tmp, "market_snapshot_full.json")
        U.UNIVERSE_PATH = os.path.join(tmp, "universe.json")
        PT.DB_PATH = os.path.join(tmp, "paper_trading.sqlite3")
        PT.schedule_status = staticmethod(lambda: {"scheduler": "demo", "enabled": False})
        demo_seed.ensure_demo_data(force=True)
        print(_canonical(_summary()))
    else:
        unittest.main()
