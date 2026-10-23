# -*- coding: utf-8 -*-
"""evolution_validation.pre_apply_gate 观察期门禁回归测试。

缺陷：门禁只读 ``adaptive_ab_tests`` 快照，而该快照只在每日收盘学习时写入。
同一交易日内刚 apply 的覆盖在快照表里查不到 → 第二次覆盖直接放行（fail-open），
效果无法归因。门禁必须同时看账户 params 里当前生效覆盖的 ``applied_at``。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import evolution_validation as EV

TZ = ZoneInfo("Asia/Shanghai")


class PreApplyGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adaptive_path = f"{self.tmp.name}/adaptive.sqlite3"
        self.paper_path = f"{self.tmp.name}/paper.sqlite3"
        with self._adaptive_ctx() as conn:
            conn.execute(
                """CREATE TABLE adaptive_risk_deployments(
                       id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT,
                       version TEXT, candidate_id INTEGER, deployed_at TEXT,
                       rolled_back_at TEXT)""")
        with self._paper_ctx() as conn:
            conn.executescript(
                """
                CREATE TABLE paper_accounts(
                    id TEXT PRIMARY KEY, params TEXT, updated_at TEXT);
                CREATE TABLE paper_nav(
                    account_id TEXT, nav_date TEXT, nav REAL);
                """)
            conn.execute(
                "INSERT INTO paper_accounts(id,params,updated_at) VALUES(?,?,?)",
                ("tq_breakout", "{}", datetime.now(TZ).isoformat()))

    # ── 连接与播种工具 ──────────────────────────────────────────────
    @contextmanager
    def _adaptive_ctx(self):
        conn = sqlite3.connect(self.adaptive_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _paper_ctx(self):
        conn = sqlite3.connect(self.paper_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _seed_nav(self, days=12):
        today = datetime.now(TZ).date()
        with self._paper_ctx() as conn:
            for i in range(days):
                day = today - timedelta(days=days - 1 - i)
                conn.execute(
                    "INSERT INTO paper_nav(account_id,nav_date,nav) VALUES(?,?,?)",
                    ("tq_breakout", day.isoformat(), 1.0 + 0.001 * i))

    def _seed_live_overlay(self, days_ago=0, kind="allocation"):
        """只在账户 params 里写入生效覆盖，不写 adaptive_ab_tests 快照。"""
        moment = (datetime.now(TZ) - timedelta(days=days_ago)).isoformat()
        with self._paper_ctx() as conn:
            row = conn.execute(
                "SELECT params FROM paper_accounts WHERE id=?", ("tq_breakout",)
            ).fetchone()
            params = json.loads(row["params"] or "{}")
            if kind == "allocation":
                params["adaptive_allocation"] = {
                    "weight_pct": 40.0, "status": "active",
                    "effective_date": moment[:10], "applied_at": moment,
                    "decision_id": 7}
            else:
                params["adaptive_selection"] = {"weights": {"a": 0.5}}
                params["adaptive_selection_meta"] = {
                    "status": "active", "version": "tuner-7",
                    "applied_at": moment, "effective_date": moment[:10],
                    "tier": "llm_consensus"}
            conn.execute(
                "UPDATE paper_accounts SET params=? WHERE id=?",
                (json.dumps(params), "tq_breakout"))
        return moment

    def _snapshot_count(self):
        with self._adaptive_ctx() as conn:
            EV.ensure_schema(conn)
            return conn.execute("SELECT COUNT(*) FROM adaptive_ab_tests").fetchone()[0]

    # ── 回归用例 ────────────────────────────────────────────────────
    def test_gate_blocks_intraday_overlay_without_ab_snapshot(self):
        """当日刚 apply 的覆盖：快照尚未落库也必须拦住第二次覆盖。"""
        self._seed_nav(days=12)
        self._seed_live_overlay(days_ago=0)
        self.assertEqual(self._snapshot_count(), 0)
        with self.assertRaises(ValueError):
            EV.pre_apply_gate(
                self._adaptive_ctx, self.paper_path, "tq_breakout", "allocation")

    def test_gate_blocks_intraday_tuner_overlay_without_ab_snapshot(self):
        self._seed_nav(days=12)
        self._seed_live_overlay(days_ago=1, kind="tuner")
        self.assertEqual(self._snapshot_count(), 0)
        with self.assertRaises(ValueError):
            EV.pre_apply_gate(
                self._adaptive_ctx, self.paper_path, "tq_breakout", "tuner")

    def test_gate_allows_live_overlay_after_observation(self):
        """满观察期（>=5 个净值日）后仍然放行，不引入假阳性。"""
        self._seed_nav(days=12)
        self._seed_live_overlay(days_ago=9)
        EV.pre_apply_gate(
            self._adaptive_ctx, self.paper_path, "tq_breakout", "allocation")

    def test_gate_allows_without_any_overlay(self):
        self._seed_nav(days=12)
        EV.pre_apply_gate(
            self._adaptive_ctx, self.paper_path, "tq_breakout", "allocation")

    def test_gate_ignores_inactive_overlay(self):
        """已回滚/未生效的覆盖不参与观察期计算。"""
        self._seed_nav(days=12)
        with self._paper_ctx() as conn:
            conn.execute(
                "UPDATE paper_accounts SET params=? WHERE id=?",
                (json.dumps({"adaptive_allocation": {
                    "status": "rolled_back",
                    "applied_at": datetime.now(TZ).isoformat()}}), "tq_breakout"))
        EV.pre_apply_gate(
            self._adaptive_ctx, self.paper_path, "tq_breakout", "allocation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
