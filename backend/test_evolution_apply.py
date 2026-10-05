# -*- coding: utf-8 -*-
"""自进化落地通道（A 批闭环）离线测试。

覆盖 evolution_apply 的三条门禁与写读链路：
- apply_allocation：影子阶段拒绝 / 权重越界拒绝 / 正常应用写入 params 并被 exact canonical weight owner 消费 / 回滚恢复前值
- apply_tuner_proposals：非 consensus 拒绝 / 幅度越界拒绝 / 正常应用写入 adaptive_selection 覆盖 / applied_ids 回写
- dual_ai_tuner._check_consensus：evolution 参数（步长/幅度比/提案上限）真实约束输出
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import dual_ai_tuner
import evolution_apply
import portfolio_allocation_weights as PAW

TZ = ZoneInfo("Asia/Shanghai")


def _iso_recent(minutes_ago=0):
    return (datetime.now(TZ) - timedelta(minutes=minutes_ago)).isoformat()


class EvolutionApplyTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adaptive_path = f"{self.tmp.name}/adaptive.sqlite3"
        self.paper_path = f"{self.tmp.name}/paper.sqlite3"
        self._init_adaptive()
        self._init_paper()

    # -- schema ------------------------------------------------------------
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
        """与 adaptive_engine._connect 等价的临时库上下文。"""
        conn = self._adaptive()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_adaptive(self):
        with self._adaptive_ctx() as conn:
            conn.executescript(
                """
                CREATE TABLE adaptive_decisions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_date TEXT, profile_id INTEGER, regime TEXT, mode TEXT,
                    stage TEXT, weights TEXT, scores TEXT, evidence TEXT,
                    status TEXT, engine_version TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE adaptive_config(
                    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
                CREATE TABLE dual_ai_tuning_runs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT,
                    merged_proposals TEXT, applied_ids TEXT,
                    created_at TEXT NOT NULL);
                CREATE TABLE evolution_params(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT,
                    params TEXT NOT NULL, source TEXT, reason TEXT,
                    parent_id INTEGER, performance_snapshot TEXT,
                    created_at TEXT NOT NULL);
                CREATE TABLE evolution_tracking(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,
                    trigger TEXT, mode TEXT, status TEXT, market_regime TEXT,
                    applied BOOLEAN DEFAULT 0, applied_count INTEGER DEFAULT 0,
                    evaluated BOOLEAN DEFAULT 0, eval_score REAL, eval_detail TEXT,
                    eval_at TEXT, mimo_latency_ms INTEGER, deepseek_latency_ms INTEGER,
                    total_latency_ms INTEGER, mimo_confidence REAL,
                    deepseek_confidence REAL, consensus_confidence REAL,
                    evolution_params_id INTEGER, created_at TEXT NOT NULL);
                """
            )
            # self_evolution 当前参数版本（A2b 消费源）
            conn.execute(
                "INSERT INTO evolution_params(version,params,source,created_at) VALUES(?,?,?,?)",
                ("evo-test", json.dumps({"max_weight_delta": 0.03,
                                         "max_delta_threshold": 0.005,
                                         "consensus_weight_ratio": 0.60,
                                         "max_proposals_per_run": 3}), "init", _iso_recent()),
            )
            # Bandit 权重边界（apply_allocation 的 _config 消费源）
            for key, value in (("min_strategy_weight_pct", 10.0),
                               ("max_strategy_weight_pct", 60.0)):
                conn.execute(
                    "INSERT INTO adaptive_config(key,value,updated_at) VALUES(?,?,?)",
                    (key, json.dumps(value), _iso_recent()),
                )

    def _init_paper(self):
        with self._paper_ctx() as conn:
            conn.executescript(
                """
                CREATE TABLE paper_accounts(
                    id TEXT PRIMARY KEY, name TEXT, source_strategy TEXT,
                    status TEXT, initial_cash REAL, cash REAL, cycle_days INTEGER,
                    max_positions INTEGER, max_weight REAL, max_exposure REAL,
                    version TEXT, style TEXT, cycle_id INTEGER,
                    params TEXT, updated_at TEXT);
                CREATE TABLE paper_parameter_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                    account_id TEXT NOT NULL, version TEXT NOT NULL, style TEXT NOT NULL,
                    params TEXT NOT NULL, reason TEXT NOT NULL,
                    effective_date TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE paper_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT,
                    event TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);
                """
            )
            for account_id, source in (
                ("tq_breakout", "one_to_two"),
                ("trend_pullback", "bottom_reversal"),
                ("sector_rotation", "sentiment_pioneer"),
                ("reported_profit_breakout", "quality_breakout"),
                ("main_force_top10", "main_force"),
            ):
                conn.execute(
                    "INSERT INTO paper_accounts(id,name,source_strategy,status,initial_cash,"
                    "cash,cycle_days,max_positions,max_weight,max_exposure,version,style,"
                    "cycle_id,params,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (account_id, account_id, source, "active", 100000.0, 100000.0,
                     60, 15, 0.10, 0.80, "v1", "balanced", 1, "{}", _iso_recent()),
                )

    @contextmanager
    def _paper_ctx(self):
        conn = self._paper()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- seeders -----------------------------------------------------------
    def _seed_decision(self, stage="eligible_for_review", status="human_review_required",
                       weights=None, minutes_ago=5):
        weights = weights or {"tq_breakout": 40.0, "trend_pullback": 25.0,
                              "sector_rotation": 15.0, "reported_profit_breakout": 10.0,
                              "main_force_top10": 10.0}
        with self._adaptive_ctx() as conn:
            cursor = conn.execute(
                "INSERT INTO adaptive_decisions(decision_date,profile_id,regime,mode,stage,"
                "weights,scores,evidence,status,engine_version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("2026-09-07", 1, "balanced", "shadow", stage,
                 json.dumps(weights), "{}", "{}", status, "t",
                 _iso_recent(minutes_ago), _iso_recent(minutes_ago)),
            )
            return cursor.lastrowid

    def _seed_tuner_run(self, proposals, status="consensus", minutes_ago=5, applied_ids=None):
        with self._adaptive_ctx() as conn:
            cursor = conn.execute(
                "INSERT INTO dual_ai_tuning_runs(status,merged_proposals,applied_ids,created_at) "
                "VALUES(?,?,?,?)",
                (status, json.dumps(proposals),
                 json.dumps(applied_ids) if applied_ids else None,
                 _iso_recent(minutes_ago)),
            )
            return cursor.lastrowid

    def _paper_params(self, account_id):
        with self._paper_ctx() as conn:
            row = conn.execute(
                "SELECT params FROM paper_accounts WHERE id=?", (account_id,)
            ).fetchone()
            return json.loads(row["params"] or "{}")


class ApplyAllocationTests(EvolutionApplyTestBase):
    def test_rejects_shadow_stage(self):
        decision_id = self._seed_decision(stage="shadow")
        with self.assertRaises(ValueError):
            evolution_apply.apply_allocation(
                self._adaptive_ctx, self.paper_path, decision_id, confirmed=True)

    def test_rejects_stale_decision(self):
        decision_id = self._seed_decision(minutes_ago=45)
        with self.assertRaises(ValueError):
            evolution_apply.apply_allocation(
                self._adaptive_ctx, self.paper_path, decision_id, confirmed=True)

    def test_rejects_out_of_bounds_weights(self):
        decision_id = self._seed_decision(
            weights={"tq_breakout": 90.0, "trend_pullback": 2.5,
                     "sector_rotation": 2.5, "reported_profit_breakout": 2.5,
                     "main_force_top10": 2.5})
        with self.assertRaises(ValueError):
            evolution_apply.apply_allocation(
                self._adaptive_ctx, self.paper_path, decision_id, confirmed=True)

    def test_apply_writes_params_and_marks_decision(self):
        decision_id = self._seed_decision()
        result = evolution_apply.apply_allocation(
            self._adaptive_ctx, self.paper_path, decision_id, confirmed=True)
        self.assertTrue(result["applied"])
        for account_id, weight in result["weights"].items():
            params = self._paper_params(account_id)
            alloc = params["adaptive_allocation"]
            self.assertEqual(alloc["weight_pct"], weight)
            self.assertEqual(alloc["status"], "active")
            self.assertEqual(alloc["decision_id"], decision_id)
            self.assertIsNone(params.get("adaptive_allocation_previous"))
        with self._adaptive_ctx() as conn:
            status = conn.execute(
                "SELECT status FROM adaptive_decisions WHERE id=?", (decision_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "applied")

    def test_canonical_weight_owner_consumes_allocation_overlay(self):
        """已批准权重由追加式参数版本 owner 读取。"""
        decision_id = self._seed_decision()
        evolution_apply.apply_allocation(
            self._adaptive_ctx, self.paper_path, decision_id, confirmed=True)
        conn = self._paper()
        try:
            rows = PAW.read_canonical_allocation_weight_owner_rows(
                conn, eligible_account_ids=tuple(
                    row["id"] for row in conn.execute(
                        "SELECT id FROM paper_accounts ORDER BY id").fetchall()),
                cycle_id=1,
            )
        finally:
            conn.close()
        cycle_id = 1
        asof_day = datetime.now(TZ).date().isoformat()
        resolved = PAW.resolve_canonical_allocation_weights(
            rows,
            eligible_account_ids=tuple(row["account_id"] for row in rows),
            cycle_id=cycle_id,
            asof_day=asof_day,
            decision_at=(datetime.now(TZ) + timedelta(minutes=1)).isoformat(),
            strategy_pins=[{"account_id": row["id"], "strategy_id": row["id"],
                            "strategy_version": 1, "strategy_checksum": "a" * 64}
                           for row in self._paper_account_rows()],
        )
        self.assertAlmostEqual(resolved["weights"]["tq_breakout"], 0.40)
        self.assertAlmostEqual(resolved["weights"]["trend_pullback"], 0.25)

    def _paper_account_rows(self):
        conn = self._paper()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT id FROM paper_accounts ORDER BY id")]
        finally:
            conn.close()

    def _allocation_weight_pair(self, decision_at):
        with self._paper_ctx() as conn:
            owner_rows = PAW.read_canonical_allocation_weight_owner_rows(
                conn, eligible_account_ids=("tq_breakout", "trend_pullback"),
                cycle_id=1)
        ids = ("tq_breakout", "trend_pullback")
        resolved = PAW.resolve_canonical_allocation_weights(
            owner_rows, eligible_account_ids=ids, cycle_id=1,
            asof_day=datetime.now(TZ).date().isoformat(), decision_at=decision_at,
            strategy_pins=[{"account_id": account, "strategy_id": account,
                            "strategy_version": 1,
                            "strategy_checksum": "a" * 64} for account in ids],
        )
        return resolved["weights"]

    def _apply_two_account_weights(self, weights):
        import adaptive_engine as engine

        with self._adaptive_ctx() as conn:
            conn.execute(
                "UPDATE adaptive_config SET value=? WHERE key='max_strategy_weight_pct'",
                (json.dumps(100.0),),
            )
        decision_id = self._seed_decision(weights=weights)
        with mock.patch.object(engine, "ACCOUNT_LABELS", {
                "tq_breakout": "突破策略", "trend_pullback": "回撤策略"}):
            evolution_apply.apply_allocation(
                self._adaptive_ctx, self.paper_path, decision_id, confirmed=True)

    def test_rc15_later_weight_apply_cannot_heal_earlier_decision(self):
        today = datetime.now(TZ).date().isoformat()
        clock = {"now": f"{today}T09:00:00+08:00"}
        with mock.patch.object(evolution_apply, "_now", side_effect=lambda: clock["now"]):
            self._apply_two_account_weights({"tq_breakout": 30.0, "trend_pullback": 70.0})
            clock["now"] = f"{today}T14:00:00+08:00"
            self._apply_two_account_weights({"tq_breakout": 40.0, "trend_pullback": 60.0})

        old = self._allocation_weight_pair(f"{today}T10:00:00+08:00")
        new = self._allocation_weight_pair(f"{today}T15:00:00+08:00")
        self.assertEqual({"tq_breakout": 0.3, "trend_pullback": 0.7}, old)
        self.assertEqual({"tq_breakout": 0.4, "trend_pullback": 0.6}, new)

    def test_rc16_rollback_appends_a_replayable_parameter_fact(self):
        today = datetime.now(TZ).date().isoformat()
        clock = {"now": f"{today}T09:00:00+08:00"}
        ids = ("tq_breakout", "trend_pullback")
        with mock.patch.object(evolution_apply, "_now", side_effect=lambda: clock["now"]):
            self._apply_two_account_weights({"tq_breakout": 30.0, "trend_pullback": 70.0})
            clock["now"] = f"{today}T14:00:00+08:00"
            self._apply_two_account_weights({"tq_breakout": 40.0, "trend_pullback": 60.0})
            clock["now"] = f"{today}T16:00:00+08:00"
            for account in ids:
                evolution_apply.rollback_allocation(
                    self.paper_path, account, reason="RC16 rollback", confirmed=True)

        self.assertEqual(
            {"tq_breakout": 0.3, "trend_pullback": 0.7},
            self._allocation_weight_pair(f"{today}T10:00:00+08:00"),
        )
        self.assertEqual(
            {"tq_breakout": 0.4, "trend_pullback": 0.6},
            self._allocation_weight_pair(f"{today}T15:00:00+08:00"),
        )
        self.assertEqual(
            {"tq_breakout": 0.3, "trend_pullback": 0.7},
            self._allocation_weight_pair(f"{today}T17:00:00+08:00"),
        )
        with self._paper_ctx() as conn:
            for account in ids:
                facts = conn.execute(
                    "SELECT id,reason FROM paper_parameter_versions "
                    "WHERE cycle_id=1 AND account_id=? ORDER BY id", (account,),
                ).fetchall()
                self.assertEqual(3, len(facts))
                self.assertEqual(3, len({int(row["id"]) for row in facts}))
                self.assertIn("adaptive allocation applied", facts[0]["reason"])
                self.assertIn("adaptive allocation applied", facts[1]["reason"])
                self.assertIn("RC16 rollback", facts[2]["reason"])

    def test_rollback_restores_no_previous(self):
        decision_id = self._seed_decision()
        evolution_apply.apply_allocation(
            self._adaptive_ctx, self.paper_path, decision_id, confirmed=True)
        result = evolution_apply.rollback_allocation(
            self.paper_path, "tq_breakout", confirmed=True)
        self.assertTrue(result["rolled_back"])
        self.assertFalse(result["restored_previous"])
        self.assertNotIn("adaptive_allocation", self._paper_params("tq_breakout"))


class ApplyTunerProposalsTests(EvolutionApplyTestBase):
    BASE_WEIGHTS = {"mom_short": 0.45, "flow": 0.25, "volsurge": 0.20, "sentiment": 0.10}

    def _proposal(self, weights=None, entry=0.004):
        weights = weights or {k: v + 0.02 for k, v in self.BASE_WEIGHTS.items()}
        return {"account_id": "tq_breakout", "weights": weights,
                "entry_score_delta": entry, "conditions": {}}

    def test_rejects_non_consensus(self):
        run_id = self._seed_tuner_run([self._proposal()], status="no_consensus")
        with self.assertRaises(ValueError):
            evolution_apply.apply_tuner_proposals(
                self._adaptive_ctx, self.paper_path, run_id, confirmed=True)

    def test_rejects_already_applied(self):
        run_id = self._seed_tuner_run([self._proposal()], applied_ids=["tq_breakout"])
        with self.assertRaises(ValueError):
            evolution_apply.apply_tuner_proposals(
                self._adaptive_ctx, self.paper_path, run_id, confirmed=True)

    def test_rejects_stale_run(self):
        run_id = self._seed_tuner_run([self._proposal()], minutes_ago=45)
        with self.assertRaises(ValueError):
            evolution_apply.apply_tuner_proposals(
                self._adaptive_ctx, self.paper_path, run_id, confirmed=True)

    def test_rejects_weight_step_violation(self):
        weights = {k: v + 0.05 for k, v in self.BASE_WEIGHTS.items()}  # > 0.03
        run_id = self._seed_tuner_run([self._proposal(weights=weights)])
        with self.assertRaises(ValueError):
            evolution_apply.apply_tuner_proposals(
                self._adaptive_ctx, self.paper_path, run_id, confirmed=True)

    def test_rejects_unknown_factor_set(self):
        weights = dict(self.BASE_WEIGHTS)
        weights.pop("sentiment")
        weights["unknown_factor"] = 0.10
        run_id = self._seed_tuner_run([self._proposal(weights=weights)])
        with self.assertRaises(ValueError):
            evolution_apply.apply_tuner_proposals(
                self._adaptive_ctx, self.paper_path, run_id, confirmed=True)

    def test_apply_writes_overlay_and_applied_ids(self):
        run_id = self._seed_tuner_run([self._proposal()])
        result = evolution_apply.apply_tuner_proposals(
            self._adaptive_ctx, self.paper_path, run_id, confirmed=True)
        self.assertEqual(result["accounts"], ["tq_breakout"])
        params = self._paper_params("tq_breakout")
        overlay = params["adaptive_selection"]
        meta = params["adaptive_selection_meta"]
        self.assertEqual(set(overlay["weights"]), set(self.BASE_WEIGHTS))
        for factor, base in self.BASE_WEIGHTS.items():
            self.assertLessEqual(
                abs(overlay["weights"][factor] - base), 0.03 + 1e-6)
        self.assertEqual(meta["tier"], "llm_consensus")
        self.assertEqual(meta["status"], "active")
        self.assertEqual(meta["run_id"], run_id)
        with self._adaptive_ctx() as conn:
            row = conn.execute(
                "SELECT applied_ids FROM dual_ai_tuning_runs WHERE id=?", (run_id,)
            ).fetchone()
        self.assertEqual(json.loads(row["applied_ids"]), ["tq_breakout"])

    def test_apply_refuses_when_selection_overlay_active(self):
        with self._paper_ctx() as conn:
            row = conn.execute(
                "SELECT params FROM paper_accounts WHERE id='tq_breakout'"
            ).fetchone()
            params = json.loads(row["params"] or "{}")
            params["adaptive_selection"] = {"weights": dict(self.BASE_WEIGHTS)}
            params["adaptive_selection_meta"] = {"status": "active",
                                                 "version": "select-evo-1"}
            conn.execute(
                "UPDATE paper_accounts SET params=? WHERE id='tq_breakout'",
                (json.dumps(params),))
        run_id = self._seed_tuner_run([self._proposal()])
        with self.assertRaises(ValueError):
            evolution_apply.apply_tuner_proposals(
                self._adaptive_ctx, self.paper_path, run_id, confirmed=True)

    def test_rollback_restores_overlay_free_state(self):
        run_id = self._seed_tuner_run([self._proposal()])
        evolution_apply.apply_tuner_proposals(
            self._adaptive_ctx, self.paper_path, run_id, confirmed=True)
        result = evolution_apply.rollback_tuner_overlay(
            self._adaptive_ctx, self.paper_path, "tq_breakout", confirmed=True)
        self.assertTrue(result["rolled_back"])
        params = self._paper_params("tq_breakout")
        self.assertNotIn("adaptive_selection", params)
        self.assertNotIn("adaptive_selection_meta", params)


class CheckConsensusEvolutionParamsTests(unittest.TestCase):
    """A2b：_check_consensus 的边界必须读取 self_evolution 参数版本。"""

    BASE = {"weights": {"a": 0.50, "b": 0.50}, "entry_score_delta": 0.0,
            "conditions": {}}

    def _accounts(self):
        return {"tq_breakout": dict(self.BASE)}

    def _proposals(self, target_a):
        def make(confidence=90):
            return {"account_id": "tq_breakout", "decision": "propose",
                    "confidence": confidence,
                    "weights": {"a": target_a, "b": 1.0 - target_a},
                    "entry_score_delta": 0.0, "conditions": {}}
        return [make()], [make()]

    def test_default_step_allows_three_pp(self):
        ok, _, merged = dual_ai_tuner._check_consensus(
            *self._proposals(0.53), accounts_map=self._accounts())
        self.assertTrue(ok)
        self.assertAlmostEqual(merged[0]["weights"]["a"], 0.53, places=6)

    def test_evolution_step_tightens_bound(self):
        # step 边界是钳制不是拒绝：evolution step=0.01 时 +0.03 的提案
        # 必须被收敛到 +0.01 落地。
        ok, _, merged = dual_ai_tuner._check_consensus(
            *self._proposals(0.53), accounts_map=self._accounts(),
            evolution={"max_weight_delta": 0.01})
        self.assertTrue(ok)
        self.assertAlmostEqual(merged[0]["weights"]["a"], 0.51, places=6)

    def test_evolution_magnitude_ratio_rejects_asymmetric(self):
        def asymmetric(confidence=90, value=0.53):
            return {"account_id": "tq_breakout", "decision": "propose",
                    "confidence": confidence,
                    "weights": {"a": value, "b": 1.0 - value},
                    "entry_score_delta": 0.0, "conditions": {}}
        # MiMo +0.03 vs DeepSeek +0.01：幅度比 0.33 < 0.60 默认即拒绝；
        # 提高 ratio 上限不放宽方向判断，这里验证 ratio 放宽为 0.2 后可通过。
        ok, _, _ = dual_ai_tuner._check_consensus(
            [asymmetric(value=0.53)], [asymmetric(value=0.51)],
            accounts_map=self._accounts(), evolution={"consensus_weight_ratio": 0.2})
        self.assertTrue(ok)

    def test_max_proposals_caps_merged(self):
        accounts = {}
        proposals_m, proposals_d = [], []
        for i, account in enumerate(("a1", "a2", "a3", "a4")):
            accounts[account] = dict(self.BASE)
            proposal = {"account_id": account, "decision": "propose",
                        "confidence": 90, "weights": {"a": 0.51, "b": 0.49},
                        "entry_score_delta": 0.0, "conditions": {}}
            proposals_m.append(dict(proposal))
            proposals_d.append(dict(proposal))
        ok, _, merged = dual_ai_tuner._check_consensus(
            proposals_m, proposals_d, accounts_map=accounts,
            evolution={"max_proposals_per_run": 2})
        self.assertTrue(ok)
        self.assertEqual(len(merged), 2)


if __name__ == "__main__":
    unittest.main()
