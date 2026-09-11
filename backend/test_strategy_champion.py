# -*- coding: utf-8 -*-
"""True-shadow Champion/Challenger + scientific promotion regressions."""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import promotion_science as PS
import self_evolution as SE
import strategy_champion as SCM

NOW = dt.datetime.now().replace(microsecond=0)
EVIDENCE = 50
STRATEGY = "trend_pullback"


def _paper_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, cash REAL);
        CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, account_id TEXT, side TEXT,
                                  status TEXT, amount REAL, realized_pnl REAL,
                                  created_at TEXT, executed_at TEXT, code TEXT);
        CREATE TABLE paper_positions(account_id TEXT, code TEXT, qty INTEGER);
        CREATE TABLE paper_capital_reservations(id INTEGER PRIMARY KEY, amount REAL);
        """
    )
    conn.execute("INSERT INTO paper_accounts VALUES(?,?)", (STRATEGY, 100000.0))
    conn.execute("INSERT INTO paper_positions VALUES(?,?,?)", (STRATEGY, "600000", 100))
    conn.execute("INSERT INTO paper_capital_reservations VALUES(1, 5000.0)")
    SCM.ensure_schema(conn)
    return conn


def _evolution_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    SE.ensure_schema(conn)
    SE.init_params(conn)
    return conn


def _output(*, pnl: float, nav: float, code="600000"):
    return {
        "signals": [{"signal_key": "same-signal", "code": code, "side": "buy"}],
        "orders": [{"signal_key": "same-signal", "code": code, "side": "buy", "qty": 100,
                    "planned_price": 10.0, "amount": 10000.0, "status": "filled"}],
        "fills": [
            {"code": code, "side": "buy", "qty": 100, "price": 10.0, "amount": 10000.0},
            {"code": code, "side": "sell", "qty": 100, "price": 10.0, "amount": 10000.0,
             "realized_pnl": pnl},
        ],
        "nav": {"nav_date": NOW.date().isoformat(), "cash": nav, "market_value": 0, "nav": nav},
    }


class TrueShadowLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.paper = _paper_db()
        self.evo = _evolution_db()

    def tearDown(self):
        self.paper.close()
        self.evo.close()

    def _open(self, *, at=None):
        return SCM.open_challenger(
            self.paper, self.evo, STRATEGY, {"max_weight_delta": 0.032},
            evidence_count=EVIDENCE, now=at or NOW - dt.timedelta(days=20),
        )

    def _record_counterfactual(self, *, challenger_pnl=2000.0, at=None):
        return SCM.run_shadow_counterfactual(
            self.paper, STRATEGY, {"asof": "2026-09-09", "600000": {"close": 10.0}},
            _output(pnl=1000.0, nav=101000.0),
            _output(pnl=challenger_pnl, nav=100000.0 + challenger_pnl),
            observed_at=at or NOW - dt.timedelta(days=1),
        )

    def _record_scientific_window(self, *, challenger_rate=0.0016,
                                  challenger_pnl=2000.0, count=14):
        champion_nav = 100000.0
        challenger_nav = 100000.0
        for i in range(count):
            if i:
                champion_nav *= 1.001
                challenger_nav *= 1.0 + challenger_rate
            observed = NOW - dt.timedelta(days=count - i)
            result = SCM.run_shadow_counterfactual(
                self.paper,
                STRATEGY,
                {"asof": observed.date().isoformat(), "seq": i, "600000": {"close": 10.0 + i / 100}},
                _output(pnl=1000.0, nav=champion_nav),
                _output(pnl=challenger_pnl, nav=challenger_nav),
                observed_at=observed,
            )
            self.assertTrue(result["recorded"])

    def test_open_leaves_active_runtime_checksum_and_params_unchanged(self):
        before = SCM.active_runtime_checksum(self.evo, STRATEGY)
        result = self._open()
        after = SCM.active_runtime_checksum(self.evo, STRATEGY)
        self.assertTrue(result["opened"])
        self.assertEqual(before["checksum"], after["checksum"])
        self.assertEqual(before["checksum"], result["active_runtime_checksum_before"])
        self.assertEqual(before["checksum"], result["active_runtime_checksum_after"])
        self.assertEqual(0.03, after["params"]["max_weight_delta"])
        self.assertEqual(0.032, result["params"]["max_weight_delta"])

    def test_candidate_parameter_version_is_physically_immutable(self):
        result = self._open()
        row = self.paper.execute(
            "SELECT params,base_active_checksum FROM strategy_shadow_parameter_versions WHERE id=?",
            (result["shadow_param_version_id"],),
        ).fetchone()
        self.assertEqual(0.032, SCM._json_loads(row["params"])["max_weight_delta"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.paper.execute("UPDATE strategy_shadow_parameter_versions SET params='{}' WHERE id=?", (result["shadow_param_version_id"],))

    def test_shadow_run_writes_only_shadow_ledgers_and_same_snapshot_for_both_sides(self):
        self._open()
        formal_before = {
            table: self.paper.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("paper_accounts", "paper_orders", "paper_positions", "paper_capital_reservations")
        }
        result = self._record_counterfactual()
        formal_after = {
            table: self.paper.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in formal_before
        }
        self.assertTrue(result["recorded"])
        self.assertEqual(formal_before, formal_after)
        self.assertEqual(2, self.paper.execute("SELECT COUNT(*) FROM shadow_signals").fetchone()[0])
        checksums = {row[0] for row in self.paper.execute("SELECT snapshot_checksum FROM shadow_nav")}
        self.assertEqual({result["snapshot_checksum"]}, checksums)
        roles = {row[0] for row in self.paper.execute("SELECT role FROM shadow_nav")}
        self.assertEqual({SCM.ROLE_CHAMPION, SCM.ROLE_CHALLENGER}, roles)

    def test_shadow_context_runs_immutable_candidate_against_the_same_snapshot(self):
        self._open()
        seen = []

        def runner(role, params, snapshot):
            seen.append((role, params["max_weight_delta"], dict(snapshot)))
            snapshot["mutated_by"] = role
            return _output(pnl=2000.0 if role == SCM.ROLE_CHALLENGER else 1000.0,
                           nav=102000.0 if role == SCM.ROLE_CHALLENGER else 101000.0)

        result = SCM.run_shadow_context(
            self.paper, self.evo, STRATEGY, {"asof": "2026-09-09", "close": 10.0}, runner,
            observed_at=NOW - dt.timedelta(days=1),
        )
        self.assertTrue(result["recorded"])
        self.assertEqual([SCM.ROLE_CHAMPION, SCM.ROLE_CHALLENGER], [row[0] for row in seen])
        self.assertEqual(0.03, seen[0][1])
        self.assertEqual(0.032, seen[1][1])
        self.assertEqual({"asof": "2026-09-09", "close": 10.0}, seen[0][2])
        self.assertEqual({"asof": "2026-09-09", "close": 10.0}, seen[1][2])

    def test_insufficient_scientific_evidence_stays_shadow(self):
        before = SCM.active_runtime_checksum(self.evo, STRATEGY)
        self._open()
        self._record_counterfactual()
        result = SCM.evaluate_challenger(self.paper, self.evo, STRATEGY, now=NOW)
        self.assertFalse(result["evaluated"])
        self.assertEqual(SCM.STATUS_SHADOW, result["status"])
        self.assertIn("证据不足", result["reason"])
        self.assertEqual(before["checksum"], SCM.active_runtime_checksum(self.evo, STRATEGY)["checksum"])
        promoted = SCM.promote_challenger(self.paper, self.evo, STRATEGY, now=NOW)
        self.assertFalse(promoted["promoted"])

    def test_evaluation_uses_same_period_counterfactual_and_scientific_gate(self):
        self._open()
        self._record_scientific_window()
        result = SCM.evaluate_challenger(self.paper, self.evo, STRATEGY, now=NOW)
        self.assertTrue(result["evaluated"])
        self.assertEqual(SCM.STATUS_READY, result["status"])
        self.assertTrue(result["scientific_gate"]["promotable"])
        self.assertGreater(result["challenger_metrics"]["return_pct"], result["champion_metrics"]["return_pct"])
        self.assertGreaterEqual(len(result["decision"]["counterfactual_snapshot_checksums"]), PS.MIN_PAIRED_SNAPSHOTS)
        champion_params = SCM._json_loads(self.paper.execute(
            "SELECT champion_params FROM strategy_champion_versions WHERE role='challenger'"
        ).fetchone()[0])
        self.assertEqual(SCM._checksum(champion_params), SCM.active_runtime_checksum(self.evo, STRATEGY)["checksum"])

    def test_mismatched_counterfactual_snapshots_fail_closed(self):
        self._open()
        self._record_scientific_window()
        self.paper.execute(
            "DELETE FROM shadow_nav WHERE role=? AND id=(SELECT MAX(id) FROM shadow_nav WHERE role=?)",
            (SCM.ROLE_CHALLENGER, SCM.ROLE_CHALLENGER),
        )
        self.paper.commit()
        result = SCM.evaluate_challenger(self.paper, self.evo, STRATEGY, now=NOW)
        self.assertFalse(result["evaluated"])
        self.assertIn("同期间同快照", result["reason"])

    def test_failed_shadow_evaluation_never_changes_formal_runtime(self):
        before = SCM.active_runtime_checksum(self.evo, STRATEGY)
        self._open()
        self._record_scientific_window(challenger_rate=0.0005, challenger_pnl=-1000.0)
        result = SCM.evaluate_challenger(self.paper, self.evo, STRATEGY, now=NOW)
        after = SCM.active_runtime_checksum(self.evo, STRATEGY)
        self.assertTrue(result["evaluated"])
        self.assertEqual(SCM.STATUS_ROLLED_BACK, result["status"])
        self.assertEqual(before["checksum"], after["checksum"])

    def test_promotion_is_the_only_active_head_switch(self):
        before = SCM.active_runtime_checksum(self.evo, STRATEGY)
        self._open()
        self._record_scientific_window()
        self.assertEqual(SCM.STATUS_READY, SCM.evaluate_challenger(self.paper, self.evo, STRATEGY, now=NOW)["status"])
        result = SCM.promote_challenger(self.paper, self.evo, STRATEGY, now=NOW)
        after = SCM.active_runtime_checksum(self.evo, STRATEGY)
        self.assertTrue(result["promoted"])
        self.assertNotEqual(before["checksum"], after["checksum"])
        self.assertEqual(0.032, after["params"]["max_weight_delta"])
        self.assertEqual(after["checksum"], result["active_runtime_checksum"])
        self.assertEqual(PS.PROMOTION_SCIENCE_VERSION, result["scientific_gate"]["version"])

    def test_legacy_ready_row_without_science_cannot_promote(self):
        before = SCM.active_runtime_checksum(self.evo, STRATEGY)
        opened = self._open()
        self.paper.execute(
            "UPDATE strategy_champion_versions SET status=?,decision=? WHERE id=?",
            (SCM.STATUS_READY, SCM._json_dumps({"promotable": True}), opened["challenger_id"]),
        )
        self.paper.commit()
        result = SCM.promote_challenger(self.paper, self.evo, STRATEGY, now=NOW)
        self.assertFalse(result["promoted"])
        self.assertIn("科学", result["reason"])
        self.assertEqual(before["checksum"], SCM.active_runtime_checksum(self.evo, STRATEGY)["checksum"])

    def test_scientific_evidence_mutation_blocks_ready_promotion(self):
        before = SCM.active_runtime_checksum(self.evo, STRATEGY)
        self._open()
        self._record_scientific_window()
        self.assertEqual(SCM.STATUS_READY, SCM.evaluate_challenger(self.paper, self.evo, STRATEGY, now=NOW)["status"])
        row = self.paper.execute(
            "SELECT id FROM shadow_nav WHERE role=? ORDER BY id LIMIT 1 OFFSET 5",
            (SCM.ROLE_CHALLENGER,),
        ).fetchone()
        self.paper.execute("UPDATE shadow_nav SET nav=nav*1.01 WHERE id=?", (row["id"],))
        self.paper.commit()
        result = SCM.promote_challenger(self.paper, self.evo, STRATEGY, now=NOW)
        self.assertFalse(result["promoted"])
        self.assertIn("科学晋升证据", result["reason"])
        self.assertEqual(before["checksum"], SCM.active_runtime_checksum(self.evo, STRATEGY)["checksum"])

    def test_side_effect_failure_is_reported_not_raised(self):
        self._open()
        self._record_scientific_window()
        self.assertEqual(SCM.STATUS_READY, SCM.evaluate_challenger(
            self.paper, self.evo, STRATEGY, now=NOW)["status"])

        original = SE.activate_params_candidate

        def boom(*_a, **_k):
            raise SE.ActivationSideEffectFailed("提案闭环失败")

        SE.activate_params_candidate = boom
        try:
            result = SCM.promote_challenger(self.paper, self.evo, STRATEGY, now=NOW)
        finally:
            SE.activate_params_candidate = original
        self.assertFalse(result["promoted"])
        self.assertIn("提案闭环失败", result["reason"])

    def test_manual_rollback_only_discards_shadow_metadata(self):
        before = SCM.active_runtime_checksum(self.evo, STRATEGY)
        self._open()
        result = SCM.rollback_challenger(self.paper, self.evo, STRATEGY)
        self.assertTrue(result["rolled_back"])
        self.assertEqual(before["checksum"], SCM.active_runtime_checksum(self.evo, STRATEGY)["checksum"])


class ValidationAndWiringTests(unittest.TestCase):
    def test_open_still_enforces_profile_locks_and_evidence(self):
        paper, evo = _paper_db(), _evolution_db()
        try:
            locked = SCM.open_challenger(paper, evo, STRATEGY, {"max_delta_threshold": 0.006}, evidence_count=EVIDENCE)
            insufficient = SCM.open_challenger(paper, evo, STRATEGY, {"max_weight_delta": 0.032}, evidence_count=2)
            self.assertFalse(locked["opened"])
            self.assertTrue(any("锁定" in item for item in locked["violations"]))
            self.assertFalse(insufficient["opened"])
            self.assertTrue(any("证据" in item for item in insufficient["violations"]))
        finally:
            paper.close()
            evo.close()

    def test_open_path_cannot_write_formal_parameter_store(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_champion.py")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        open_body = body[body.index("def open_challenger"):body.index("def _mapping_rows")]
        self.assertNotIn("adjust_strategy_params", open_body)
        self.assertIn("strategy_shadow_parameter_versions", open_body)
        self.assertIn("def run_shadow_context", body)
        self.assertIn("run_shadow_counterfactual", body)

    def test_promotion_path_requires_current_scientific_gate(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_champion.py")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        promote_body = body[body.index("def promote_challenger"):body.index("def rollback_challenger")]
        self.assertIn("_verify_ready_science", promote_body)
        self.assertIn("verify_promotion_evidence", body)


if __name__ == "__main__":
    unittest.main()
