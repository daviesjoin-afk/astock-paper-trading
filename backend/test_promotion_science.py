# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import promotion_science as PS


BASE = dt.datetime(2026, 8, 1, 15, 0, 0)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE shadow_nav(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenger_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            snapshot_checksum TEXT NOT NULL,
            nav_date TEXT,
            nav REAL NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(challenger_id, role, snapshot_checksum)
        )"""
    )
    return conn


def _seed(conn, challenger_navs, champion_navs=None, *, challenger_id=1):
    if champion_navs is None:
        champion_navs = [100000.0 * (1.001 ** i) for i in range(len(challenger_navs))]
    for i, (champion, challenger) in enumerate(
        zip(champion_navs, challenger_navs, strict=True)
    ):
        moment = BASE + dt.timedelta(days=i)
        checksum = f"snap-{i:03d}"
        for role, nav in (("champion", champion), ("challenger", challenger)):
            conn.execute(
                "INSERT INTO shadow_nav(challenger_id,role,snapshot_checksum,nav_date,nav,created_at) VALUES(?,?,?,?,?,?)",
                (challenger_id, role, checksum, moment.date().isoformat(), nav,
                 moment.isoformat(timespec="seconds")),
            )
    conn.commit()


def _window(n):
    return BASE.isoformat(timespec="seconds"), (BASE + dt.timedelta(days=n + 1)).isoformat(timespec="seconds")


class PromotionScienceTests(unittest.TestCase):
    def test_consistent_paired_oos_advantage_passes(self):
        conn = _db()
        self.addCleanup(conn.close)
        challenger = [100000.0 * (1.0016 ** i) for i in range(14)]
        _seed(conn, challenger)
        since, until = _window(14)
        result = PS.evaluate_promotion_evidence(conn, 1, since, until)
        self.assertTrue(result["evaluable"])
        self.assertTrue(result["promotable"])
        self.assertGreater(result["full"]["lower_95"], 0)
        self.assertGreaterEqual(result["distinct_days"], PS.MIN_DISTINCT_DAYS)
        self.assertGreaterEqual(result["holdout"]["positive_ratio"], PS.MIN_HOLDOUT_POSITIVE_RATIO)
        self.assertTrue(result["oos"])

    def test_small_sample_stays_shadow_not_promotable(self):
        conn = _db()
        self.addCleanup(conn.close)
        challenger = [100000.0 * (1.002 ** i) for i in range(5)]
        _seed(conn, challenger)
        since, until = _window(5)
        result = PS.evaluate_promotion_evidence(conn, 1, since, until)
        self.assertFalse(result["evaluable"])
        self.assertFalse(result["promotable"])
        self.assertIn("证据不足", result["reason"])

    def test_holdout_reversal_blocks_promotion(self):
        conn = _db()
        self.addCleanup(conn.close)
        champion = [100000.0]
        challenger = [100000.0]
        for i in range(1, 15):
            champion.append(champion[-1] * 1.001)
            challenger_rate = 1.003 if i <= 9 else 0.998
            challenger.append(challenger[-1] * challenger_rate)
        _seed(conn, challenger, champion)
        since, until = _window(15)
        result = PS.evaluate_promotion_evidence(conn, 1, since, until)
        self.assertTrue(result["evaluable"])
        self.assertFalse(result["promotable"])
        self.assertIn("holdout_mean_positive", result["failed"])

    def test_noisy_unproven_mean_fails_confidence_bound(self):
        conn = _db()
        self.addCleanup(conn.close)
        champion = [100000.0]
        challenger = [100000.0]
        excess = [0.006, -0.005, 0.005, -0.004, 0.006, -0.005,
                  0.005, -0.004, 0.006, -0.005, 0.004, -0.003, 0.004]
        for delta in excess:
            champion.append(champion[-1] * 1.001)
            challenger.append(challenger[-1] * (1.001 + delta))
        _seed(conn, challenger, champion)
        since, until = _window(len(challenger))
        result = PS.evaluate_promotion_evidence(conn, 1, since, until)
        self.assertTrue(result["evaluable"])
        self.assertFalse(result["promotable"])
        self.assertIn("full_lower_95_positive", result["failed"])

    def test_verify_detects_evidence_mutation(self):
        conn = _db()
        self.addCleanup(conn.close)
        challenger = [100000.0 * (1.0016 ** i) for i in range(14)]
        _seed(conn, challenger)
        since, until = _window(14)
        stored = PS.evaluate_promotion_evidence(conn, 1, since, until)
        self.assertTrue(PS.verify_promotion_evidence(conn, 1, stored)["valid"])
        conn.execute(
            "UPDATE shadow_nav SET nav=nav*1.01 WHERE challenger_id=1 AND role='challenger' AND snapshot_checksum='snap-005'"
        )
        conn.commit()
        verified = PS.verify_promotion_evidence(conn, 1, stored)
        self.assertFalse(verified["valid"])
        self.assertIn("发生变化", verified["reason"])

    def test_legacy_or_fabricated_decision_cannot_verify(self):
        conn = _db()
        self.addCleanup(conn.close)
        self.assertFalse(PS.verify_promotion_evidence(conn, 1, {"promotable": True})["valid"])


if __name__ == "__main__":
    unittest.main()
