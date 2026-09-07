# -*- coding: utf-8 -*-
"""自进化验证层（B 批）离线测试。

覆盖 evolution_validation：
- _window_return：窗口收益与样本不足
- record_ab_snapshots：观察期 verdict=observing、满观察期 excess 计算、同日幂等
- pre_apply_gate：观察期内拒绝、满观察期放行、无部署放行
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import evolution_validation as EV

TZ = ZoneInfo("Asia/Shanghai")


class ValidationBase(unittest.TestCase):
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
                ("tq_breakout", "{}", self._iso(0)))

    def _iso(self, days_ago=0):
        return (datetime.now(TZ) - timedelta(days=days_ago)).isoformat()

    def _adaptive(self):
        conn = sqlite3.connect(self.adaptive_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _paper(self):
        conn = sqlite3.connect(self.paper_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _adaptive_ctx(self):
        conn = self._adaptive()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _paper_ctx(self):
        conn = self._paper()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _seed_nav(self, account_id="tq_breakout", base=1.0, daily=0.0, days=12,
                  end_days_ago=0):
        """生成以今天为终点（end_days_ago 可回退）的 NAV 序列。"""
        today = datetime.now(TZ).date()
        with self._paper_ctx() as conn:
            for i in range(days):
                day = today - timedelta(days=end_days_ago + days - 1 - i)
                nav = base * (1 + daily) ** i
                conn.execute(
                    "INSERT INTO paper_nav(account_id,nav_date,nav) VALUES(?,?,?)",
                    (account_id, day.isoformat(), round(nav, 6)))

    def _seed_deployment(self, account_id="tq_breakout", kind="allocation",
                         version="decision-7", deployed_days_ago=8):
        deployed = (datetime.now(TZ) - timedelta(days=deployed_days_ago)).isoformat()
        with self._paper_ctx() as conn:
            if kind == "allocation":
                row = conn.execute(
                    "SELECT params FROM paper_accounts WHERE id=?", (account_id,)
                ).fetchone()
                params = json.loads(row["params"] or "{}")
                params["adaptive_allocation"] = {
                    "weight_pct": 40.0, "status": "active",
                    "effective_date": deployed[:10], "applied_at": deployed,
                    "decision_id": 7}
                conn.execute(
                    "UPDATE paper_accounts SET params=? WHERE id=?",
                    (json.dumps(params), account_id))
        if kind == "risk":
            with self._adaptive_ctx() as conn:
                conn.execute(
                    "INSERT INTO adaptive_risk_deployments(account_id,version,"
                    "candidate_id,deployed_at,rolled_back_at) VALUES(?,?,?,?,NULL)",
                    (account_id, version, 9, deployed))
        return {"deployed": deployed, "version": version}


class WindowReturnTests(ValidationBase):
    def test_window_return_basic(self):
        self._seed_nav(daily=0.01, days=12)
        conn = self._paper()
        try:
            series = EV._nav_series(conn, "tq_breakout")
        finally:
            conn.close()
        value = EV._window_return(series, datetime.now(TZ).date().isoformat(), 5)
        expected = ((1.01 ** 5) - 1) * 100
        self.assertAlmostEqual(value, expected, places=3)

    def test_window_return_insufficient_samples(self):
        self._seed_nav(days=3)
        conn = self._paper()
        try:
            series = EV._nav_series(conn, "tq_breakout")
        finally:
            conn.close()
        self.assertIsNone(
            EV._window_return(series, datetime.now(TZ).date().isoformat(), 5))


class RecordAbSnapshotsTests(ValidationBase):
    def test_observing_when_under_min_days(self):
        self._seed_nav(days=12)
        self._seed_deployment(deployed_days_ago=2)
        result = EV.record_ab_snapshots(self._adaptive_ctx, self.paper_path)
        self.assertEqual(result["recorded"], 1)
        with self._adaptive_ctx() as conn:
            row = dict(conn.execute("SELECT * FROM adaptive_ab_tests").fetchone())
        self.assertEqual(row["verdict"], "observing")
        self.assertIsNone(row["excess_pct"])

    def test_pass_and_fail_verdicts(self):
        # 部署前 5 日持平（基线 0%），部署后 5 日每日 +1% → excess > 0 → pass
        today = datetime.now(TZ).date()
        with self._paper_ctx() as conn:
            for i in range(12):
                day = today - timedelta(days=11 - i)
                nav = 1.0 if i < 7 else 1.0 * (1.01 ** (i - 6))
                conn.execute(
                    "INSERT INTO paper_nav(account_id,nav_date,nav) VALUES(?,?,?)",
                    ("tq_breakout", day.isoformat(), round(nav, 6)))
        self._seed_deployment(deployed_days_ago=5)
        EV.record_ab_snapshots(self._adaptive_ctx, self.paper_path)
        with self._adaptive_ctx() as conn:
            row = dict(conn.execute("SELECT * FROM adaptive_ab_tests").fetchone())
        self.assertEqual(row["verdict"], "pass")
        self.assertGreater(row["excess_pct"], 0)
        # 同日重复调用幂等
        again = EV.record_ab_snapshots(self._adaptive_ctx, self.paper_path)
        self.assertEqual(again["recorded"], 0)

    def test_fail_verdict_when_deployment_underperforms(self):
        today = datetime.now(TZ).date()
        with self._paper_ctx() as conn:
            for i in range(12):
                day = today - timedelta(days=11 - i)
                nav = 1.0 if i < 7 else 1.0 * (0.99 ** (i - 6))
                conn.execute(
                    "INSERT INTO paper_nav(account_id,nav_date,nav) VALUES(?,?,?)",
                    ("tq_breakout", day.isoformat(), round(nav, 6)))
        self._seed_deployment(deployed_days_ago=5)
        EV.record_ab_snapshots(self._adaptive_ctx, self.paper_path)
        with self._adaptive_ctx() as conn:
            row = dict(conn.execute("SELECT * FROM adaptive_ab_tests").fetchone())
        self.assertEqual(row["verdict"], "fail")

    def test_risk_kind_from_deployments_table(self):
        self._seed_nav(days=12)
        self._seed_deployment(kind="risk", version="risk-evo-1", deployed_days_ago=9)
        result = EV.record_ab_snapshots(self._adaptive_ctx, self.paper_path)
        self.assertEqual(result["deployments"], 1)
        with self._adaptive_ctx() as conn:
            row = dict(conn.execute("SELECT kind,version FROM adaptive_ab_tests").fetchone())
        self.assertEqual(row["kind"], "risk")
        self.assertEqual(row["version"], "risk-evo-1")


class PreApplyGateTests(ValidationBase):
    def test_gate_blocks_within_observation(self):
        self._seed_nav(days=12)
        self._seed_deployment(deployed_days_ago=2)
        EV.record_ab_snapshots(self._adaptive_ctx, self.paper_path)
        with self.assertRaises(ValueError):
            EV.pre_apply_gate(self._adaptive_ctx, self.paper_path, "tq_breakout", "allocation")

    def test_gate_allows_after_observation(self):
        self._seed_nav(days=12)
        self._seed_deployment(deployed_days_ago=9)
        EV.record_ab_snapshots(self._adaptive_ctx, self.paper_path)
        EV.pre_apply_gate(self._adaptive_ctx, self.paper_path, "tq_breakout", "allocation")

    def test_gate_allows_without_deployment(self):
        EV.pre_apply_gate(self._adaptive_ctx, self.paper_path, "tq_breakout", "allocation")


if __name__ == "__main__":
    unittest.main()
