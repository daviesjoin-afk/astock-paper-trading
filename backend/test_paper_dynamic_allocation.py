import copy
import json
import random
import sqlite3
import unittest
from unittest.mock import patch

import paper_dynamic_allocation as D


class DynamicPolicyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY,status TEXT);
            CREATE TABLE paper_accounts(id TEXT PRIMARY KEY,cycle_id INTEGER,status TEXT,
                params TEXT,updated_at TEXT,version TEXT,style TEXT);
            CREATE TABLE paper_parameter_versions(id INTEGER PRIMARY KEY,cycle_id INTEGER,
                account_id TEXT,version TEXT,style TEXT,params TEXT,reason TEXT,
                effective_date TEXT,created_at TEXT);
            CREATE TABLE paper_audit(id INTEGER PRIMARY KEY,account_id TEXT,event TEXT,
                detail TEXT,created_at TEXT);
            INSERT INTO paper_cycles VALUES(13,'running');
        """)
        policy = {"id": D.POLICY_ID, "status": "active", "approved_by": "user_delegated_design",
                  "approved_at": "2026-10-12T14:00:00+08:00", "effective_date": "2026-10-13",
                  **D.POLICY_RULES}
        self.params = {"other_risk_cap": 0.5, "adaptive_allocation_policy": policy,
                       "adaptive_allocation": {"status": "active", "weight_pct": 20.0}}
        for k in range(5):
            self.conn.execute("INSERT INTO paper_accounts VALUES(?,13,'running',?,NULL,'v1','trend')",
                              (str(k), json.dumps(self.params)))
        self.conn.commit()

    def run_policy(self, **changes):
        def metric(conn, context, *, account_id):
            self.assertEqual(context.asof_day.isoformat(), "2026-10-12")
            return int(account_id) * 1000.0, "verified"
        with patch.object(D.P, "_cycle_initial", return_value=60000.0), \
                patch.object(D.P, "realized_pnl", side_effect=changes.get("metric", metric)), \
                patch.object(D.P, "_sell_fills", return_value=changes.get(
                    "fills", ([{"order_id": 1}, {"order_id": 2}, {"order_id": 3}], [], True))):
            return D.rebalance(self.conn, cycle_id=13, decision_at="2026-10-13T09:31:00+08:00")

    def test_daily_rebalance_records_explicit_history_and_preserves_other_params(self):
        result = self.run_policy()
        self.assertEqual(result["status"], "evaluated")
        self.assertLess(result["weights_pct"]["0"], result["weights_pct"]["4"])
        for row in self.conn.execute("SELECT params FROM paper_accounts"):
            params = json.loads(row[0])
            self.assertEqual(params["other_risk_cap"], 0.5)
            self.assertEqual(params["adaptive_allocation_policy"], self.params["adaptive_allocation_policy"])
            self.assertEqual(params["adaptive_allocation"]["source"], "authorized_policy_execution")
            self.assertNotIn("decision_id", params["adaptive_allocation"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_parameter_versions").fetchone()[0], 5)
        self.assertEqual(self.run_policy()["status"], "already_evaluated")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_parameter_versions").fetchone()[0], 5)

    def test_missing_evidence_holds_existing_weights_without_writes(self):
        before = self.conn.total_changes
        self.assertEqual(self.run_policy(fills=([{"order_id": 1}], [], True))["status"], "waiting_evidence")
        self.assertEqual(self.conn.total_changes, before)

    def test_no_policy_or_conflicting_authorization_is_not_enabled(self):
        for value in ({}, {**self.params, "adaptive_allocation_policy": {}}):
            self.conn.execute("UPDATE paper_accounts SET params=? WHERE id='0'", (json.dumps(value),))
            before = self.conn.total_changes
            self.assertEqual(self.run_policy()["status"], "not_authorized")
            self.assertEqual(self.conn.total_changes, before)

    def test_policy_is_not_applied_before_effective_date(self):
        before = self.conn.total_changes
        self.assertEqual(D.rebalance(self.conn, cycle_id=13,
                         decision_at="2026-10-12T15:00:00+08:00")["status"], "not_effective")
        self.assertEqual(self.conn.total_changes, before)

    def test_audit_failure_rolls_back_complete_five_account_update(self):
        self.conn.execute("BEGIN IMMEDIATE")
        with patch.object(D.R, "audit", side_effect=sqlite3.OperationalError("audit failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_policy()
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_parameter_versions").fetchone()[0], 0)
        self.assertTrue(all(json.loads(r[0]) == self.params for r in self.conn.execute("SELECT params FROM paper_accounts")))

    def test_sequential_targets_always_preserve_total_bounds_and_step(self):
        rng = random.Random(12)
        current = dict.fromkeys(map(str, range(5)), 20.0)
        for _ in range(500):
            previous = copy.deepcopy(current)
            current = D.target_weights(previous, {k: rng.uniform(-100, 100) for k in previous})
            self.assertAlmostEqual(sum(current.values()), 100.0)
            for k in current:
                self.assertLessEqual(abs(current[k] - previous[k]), 2.000001)
                self.assertTrue(5 <= current[k] <= 60)

    def test_roster_baseline_is_not_dragged_back_to_equal_weight(self):
        """20/50/30 在收益相同的一天必须原地不动（旧实现会拉回等权）。"""
        baseline = {"tq_breakout": 20.0, "trend_pullback": 50.0,
                    "reported_profit_breakout": 30.0}
        equal_returns = dict.fromkeys(baseline, 3.0)
        self.assertEqual(D.target_weights(baseline, equal_returns), baseline)
        # 步长仍然生效：单日最多移动 2 个百分点，且合计恒为 100。
        better = D.target_weights(baseline, {"tq_breakout": 30.0, "trend_pullback": -10.0,
                                             "reported_profit_breakout": -10.0})
        self.assertAlmostEqual(sum(better.values()), 100.0)
        self.assertLessEqual(better["tq_breakout"] - 20.0, 2.000001)
        self.assertGreaterEqual(better["trend_pullback"], 48.0)
        self.assertGreaterEqual(better["reported_profit_breakout"], 28.0)

    def test_policy_v2_does_not_match_v1_authorization(self):
        legacy = {**self.params, "adaptive_allocation_policy": {
            "id": "authorized-daily-performance-v1", "status": "active",
            "approved_by": "user_delegated_design",
            "approved_at": "2026-10-12T14:00:00+08:00", "effective_date": "2026-10-13",
            "min_sell_samples": 3, "min_weight_pct": 15.0,
            "max_weight_pct": 30.0, "max_daily_step_pct": 2.0}}
        for k in range(5):
            self.conn.execute("UPDATE paper_accounts SET params=? WHERE id=?",
                              (json.dumps(legacy), str(k)))
        self.conn.commit()
        before = self.conn.total_changes
        self.assertEqual(self.run_policy()["status"], "not_authorized")
        self.assertEqual(self.conn.total_changes, before)

    def test_out_of_bounds_declaration_stops_the_policy_instead_of_raising(self):
        for k in range(5):
            params = {**self.params, "adaptive_allocation": {"status": "active", "weight_pct": 70.0}}
            self.conn.execute("UPDATE paper_accounts SET params=? WHERE id=?", (json.dumps(params), str(k)))
        self.conn.commit()
        before = self.conn.total_changes
        result = self.run_policy()
        self.assertEqual(result["status"], "infeasible_current_weights")
        self.assertEqual(self.conn.total_changes, before)

    def test_invalid_or_infeasible_current_weights_are_rejected(self):
        for bad in ([20, 20, 20, 20, 21], [70, 10, 10, 5, 5], [float('nan')] * 5):
            with self.assertRaises(ValueError):
                D.target_weights(dict(enumerate(bad)), dict.fromkeys(range(5), 1))
