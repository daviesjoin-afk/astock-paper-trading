# -*- coding: utf-8 -*-
"""进化参数生命周期契约测试：candidate / validated / active 三者分离。

修复的根因：旧实现用 ``ORDER BY id DESC LIMIT 1`` 推断"当前生效参数"，于是
**插入一条候选行 == 立即成为 current**。而 ``self_evolution`` 的 current
params 会被 ``dual_ai_tuner`` 当作调参边界直接消费，所以一条从未被校验、
从未被批准的候选会立刻改变调参器行为。

永久不变量（本文件每条断言都服务于其中一条）：

    candidate  != validated
    validated  != active
    latest     != active
    created    != approved
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import evolution_activation as EA
import self_evolution as SE

GLOBAL = "__global__"

# 旧库 schema：没有 strategy_id / 生命周期列，也没有生命周期表。
LEGACY_DDL = """
CREATE TABLE evolution_params(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    params TEXT NOT NULL,
    source TEXT NOT NULL,
    reason TEXT,
    parent_id INTEGER,
    performance_snapshot TEXT,
    strategy_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE evolution_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    params_id INTEGER,
    detail TEXT NOT NULL,
    metrics TEXT,
    created_at TEXT NOT NULL
);
"""

BASE_PARAMS = {
    "max_weight_delta": 0.03,
    "max_delta_threshold": 0.008,
    "confidence_threshold": 70,
    "consensus_weight_ratio": 0.6,
    "consensus_direction_threshold": 0.008,
    "hold_bias": 0.2,
}


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    SE.ensure_schema(conn)
    return conn


def _legacy_db():
    """造一个"旧系统"的库：只有 evolution_params + evolution_log。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_DDL)
    return conn


def _insert_legacy_row(conn, params, *, strategy_id=None, source="init",
                       created_at="2026-08-01T10:00:00+08:00"):
    if strategy_id is None:
        conn.execute(
            "INSERT INTO evolution_params(version, params, source, reason, created_at)"
            " VALUES(?,?,?,?,?)",
            (SE.EVOLUTION_VERSION, json.dumps(params), source, "legacy", created_at),
        )
    else:
        conn.execute(
            "INSERT INTO evolution_params(version, params, source, reason, strategy_id,"
            " created_at) VALUES(?,?,?,?,?,?)",
            (SE.EVOLUTION_VERSION, json.dumps(params), source, "legacy",
             strategy_id, created_at),
        )
    conn.commit()


def _pointer(conn, scope):
    row = conn.execute(
        "SELECT params_id FROM evolution_active_params WHERE scope_key=?", (scope,)
    ).fetchone()
    return None if row is None else int(row["params_id"])


class LegacyBootstrapTests(unittest.TestCase):
    """旧库迁移：保留旧 runtime 事实，且只能跑一次。"""

    def test_legacy_latest_row_becomes_initial_pointer(self):
        conn = _legacy_db()
        _insert_legacy_row(conn, BASE_PARAMS)
        _insert_legacy_row(conn, dict(BASE_PARAMS, max_weight_delta=0.033),
                           strategy_id="tq_breakout", source="evolve")
        EA.ensure_schema(conn)
        # 全局 latest row（id=1）与策略 latest row（id=2）各自成为初始指针。
        self.assertEqual(1, _pointer(conn, GLOBAL))
        self.assertEqual(2, _pointer(conn, "strategy:tq_breakout"))
        self.assertEqual(2, conn.execute(
            "SELECT COUNT(*) FROM evolution_activation_history").fetchone()[0])
        conn.close()

    def test_bootstrap_is_idempotent(self):
        conn = _legacy_db()
        _insert_legacy_row(conn, BASE_PARAMS)
        EA.ensure_schema(conn)
        first = _pointer(conn, GLOBAL)
        EA.ensure_schema(conn)
        EA.ensure_schema(conn)
        self.assertEqual(first, _pointer(conn, GLOBAL))
        self.assertEqual(1, conn.execute(
            "SELECT COUNT(*) FROM evolution_activation_history").fetchone()[0])
        conn.close()

    def test_post_migration_candidate_is_never_auto_bootstrapped(self):
        """迁移之后新建的候选，绝不能被后续 bootstrap 当成 legacy latest row。"""
        conn = _legacy_db()
        _insert_legacy_row(conn, BASE_PARAMS)
        EA.ensure_schema(conn)
        candidate = EA.create_candidate(
            conn, dict(BASE_PARAMS, max_weight_delta=0.033),
            strategy_id="brand_new", source="evolve", reason="post-migration",
            evidence_count=20)
        EA.ensure_schema(conn)
        # 没有指针 = 继承全局；候选仍然是候选。
        self.assertIsNone(_pointer(conn, "strategy:brand_new"))
        self.assertEqual("global", EA.resolve_effective(conn, "brand_new")["source"])
        self.assertNotEqual(candidate["params_id"], _pointer(conn, GLOBAL))
        conn.close()

    def test_dangling_pointer_fails_closed(self):
        """指针指向缺失行 → 抛错，绝不回落 latest row。"""
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        conn.execute("UPDATE evolution_active_params SET params_id=99999 WHERE scope_key=?",
                     (GLOBAL,))
        conn.commit()
        with self.assertRaises(SE.EvolutionLifecycleError):
            SE.get_current_params(conn)

    def test_evolution_status_reports_corruption_instead_of_crashing(self):
        """状态页必须把指针损坏**报出来**，而不是 500。

        损坏时 `current_params` 显式标记为不可用（不是"悄悄用 latest row"），
        `lifecycle_healthy=False` 让运维一眼看见。
        """
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        conn.execute("UPDATE evolution_active_params SET params_id=99999 WHERE scope_key=?",
                     (GLOBAL,))
        conn.commit()
        status = SE.evolution_status(conn)
        self.assertFalse(status["lifecycle_healthy"])
        self.assertIsNone(status["current_params"]["id"])
        self.assertTrue(status["current_params"].get("unavailable"))
        self.assertIn("99999", str(status["lifecycle"]["error"]))


class CandidateIsolationTests(unittest.TestCase):
    """核心：创建候选不改变 runtime。"""

    def test_global_candidate_does_not_change_current_params(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        before = SE.get_current_params(conn)
        created = EA.create_candidate(
            conn, dict(BASE_PARAMS, max_weight_delta=0.033),
            source="evolve", reason="auto")
        after = SE.get_current_params(conn)
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(before["params"], after["params"])
        self.assertNotEqual(before["id"], created["params_id"])
        self.assertEqual(EA.STATE_VALIDATED, created["validation_state"])

    def test_strategy_candidate_does_not_change_effective_params(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        before = SE.get_strategy_params(conn, "trend_pullback")
        created = SE.adjust_strategy_params(
            conn, "trend_pullback", {"max_weight_delta": 0.032}, evidence_count=20)
        self.assertTrue(created["adjusted"])
        self.assertFalse(created["activated"])
        after = SE.get_strategy_params(conn, "trend_pullback")
        self.assertEqual(before["params"], after["params"])
        # 显式激活后才生效。
        SE.activate_params_candidate(conn, created["new_params_id"], actor="test")
        self.assertEqual(
            0.032, SE.get_strategy_params(conn, "trend_pullback")["params"]["max_weight_delta"])

    def test_latest_row_is_not_active(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        active_id = SE.get_current_params(conn)["id"]
        for _ in range(3):
            EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.033),
                                source="evolve", reason="noise")
        view = SE.lifecycle_view(conn)
        self.assertEqual(active_id, view["active"]["id"])
        self.assertNotEqual(view["latest_candidate"]["id"], view["active"]["id"])
        self.assertTrue(view["validated_pending"])

    def test_manual_adjust_only_creates_candidate(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        before = SE.get_current_params(conn)["params"]["confidence_threshold"]
        result = SE.manual_adjust(conn, {"confidence_threshold": 72}, reason="test")
        self.assertTrue(result["adjusted"])
        self.assertFalse(result["activated"])
        self.assertEqual(before, SE.get_current_params(conn)["params"]["confidence_threshold"])
        SE.activate_params_candidate(conn, result["new_params_id"], actor="test")
        self.assertEqual(72, SE.get_current_params(conn)["params"]["confidence_threshold"])


class ActivationGateTests(unittest.TestCase):
    """激活门：未校验 / 被拒绝 / 不存在的候选一律拒绝。"""

    def _validated(self, conn):
        return EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.033),
                                   source="evolve", reason="r")

    def test_unvalidated_candidate_cannot_be_activated(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        raw = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.033),
                                  source="evolve", reason="r", validate=False)
        with self.assertRaises(SE.CandidateNotValidated):
            SE.activate_params_candidate(conn, raw["params_id"], actor="test")

    def test_rejected_candidate_cannot_be_activated(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        bad = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=99),
                                  source="evolve", reason="out of bounds")
        self.assertEqual(EA.STATE_REJECTED, bad["validation_state"])
        with self.assertRaises(SE.CandidateRejected):
            SE.activate_params_candidate(conn, bad["params_id"], actor="test")

    def test_missing_candidate_is_404(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        with self.assertRaises(SE.CandidateNotFound):
            SE.activate_params_candidate(conn, 99999, actor="test")

    def test_activation_records_history_and_is_idempotent(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        created = self._validated(conn)
        result = SE.activate_params_candidate(conn, created["params_id"], actor="operator")
        self.assertTrue(result["activated"])
        self.assertEqual(created["params_id"], SE.get_current_params(conn)["id"])
        history = SE.activation_history(conn)
        self.assertEqual(EA.ACTION_ACTIVATE, history[0]["action"])
        self.assertEqual("operator", history[0]["actor"])
        # 重复激活 = no-op，不会往历史里再塞一条。
        again = SE.activate_params_candidate(conn, created["params_id"], actor="operator")
        self.assertTrue(again["already_active"])
        self.assertEqual(len(history), len(SE.activation_history(conn)))


class StaleBaseTests(unittest.TestCase):
    """stale-base CAS：候选必须记录创建时的 effective base。"""

    def test_candidate_records_effective_base(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        active_id = SE.get_current_params(conn)["id"]
        created = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.033),
                                      strategy_id="trend_pullback", source="evolve",
                                      reason="r", evidence_count=20)
        # 策略尚无专属指针 → base 必须是"继承到的全局 active 行"。
        self.assertEqual(active_id, created["base_params_id"])

    def test_stale_candidate_is_refused_after_base_moved(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        old = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.033),
                                  source="evolve", reason="old", validate=True)
        # 先把全局推到另一个版本，让 old 的基线过期。
        new = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.027),
                                  source="evolve", reason="new", validate=True)
        SE.activate_params_candidate(conn, new["params_id"], actor="test")
        with self.assertRaises(SE.CandidateStale):
            SE.activate_params_candidate(conn, old["params_id"], actor="test")
        # 拒绝后指针没有被动过。
        self.assertEqual(new["params_id"], SE.get_current_params(conn)["id"])

    def test_strategy_candidate_goes_stale_when_inherited_global_moves(self):
        """候选必须记"创建时继承到的全局行"，而不是"策略自己的最新行"。

        若 base 记成策略自己的最新行（或干脆不记），全局前移后这类候选会被
        错误地当成"基线仍然匹配"而放行。
        """
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        pending = SE.adjust_strategy_params(
            conn, "tq_breakout", {"max_weight_delta": 0.033}, evidence_count=20)
        moved = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.027),
                                    source="evolve", reason="global moves")
        SE.activate_params_candidate(conn, moved["params_id"], actor="test")
        with self.assertRaises(SE.CandidateStale):
            SE.activate_params_candidate(conn, pending["new_params_id"], actor="test")

    def test_regenerated_candidate_after_base_move_can_activate(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        first = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.027),
                                    source="evolve", reason="first")
        SE.activate_params_candidate(conn, first["params_id"], actor="test")
        # 基于**新的**生效版本重新生成 → 基线匹配 → 允许激活。
        second = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.024),
                                     source="evolve", reason="second")
        self.assertEqual(first["params_id"], second["base_params_id"])
        SE.activate_params_candidate(conn, second["params_id"], actor="test")
        self.assertEqual(second["params_id"], SE.get_current_params(conn)["id"])


class InheritanceTests(unittest.TestCase):
    """策略无指针 = 继承全局 active，不是错误。"""

    def test_strategy_without_pointer_inherits_global(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        state = SE.get_strategy_params(conn, "tq_breakout")
        self.assertEqual(SE.get_current_params(conn)["id"], state["id"])
        self.assertEqual(GLOBAL, state["inherited_from"])

    def test_global_move_changes_inherited_view(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        before = SE.get_strategy_params(conn, "tq_breakout")["params"]["max_weight_delta"]
        created = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.033),
                                      source="evolve", reason="r")
        SE.activate_params_candidate(conn, created["params_id"], actor="test")
        after = SE.get_strategy_params(conn, "tq_breakout")["params"]["max_weight_delta"]
        self.assertEqual(0.03, before)
        self.assertEqual(0.033, after)

    def test_own_pointer_overrides_global(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        created = SE.adjust_strategy_params(
            conn, "tq_breakout", {"max_weight_delta": 0.033}, evidence_count=20)
        SE.activate_params_candidate(conn, created["new_params_id"], actor="test")
        self.assertEqual("strategy", EA.resolve_effective(conn, "tq_breakout")["source"])
        self.assertEqual(0.033, SE.get_strategy_params(conn, "tq_breakout")["params"]["max_weight_delta"])
        self.assertEqual(0.03, SE.get_current_params(conn)["params"]["max_weight_delta"])


class RollbackTests(unittest.TestCase):
    """回滚基于 activation history，不看行 id 顺序。"""

    def test_rollback_targets_previous_activated_version(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        start = SE.get_current_params(conn)["id"]
        first = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.033),
                                    source="evolve", reason="first")
        SE.activate_params_candidate(conn, first["params_id"], actor="test")
        second = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.036),
                                     source="evolve", reason="second")
        SE.activate_params_candidate(conn, second["params_id"], actor="test")
        # 再插一条 id 最大、但从未生效的候选：回滚绝不能落到它身上。
        EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.039),
                            source="evolve", reason="never activated")
        result = EA.rollback_active(conn, actor="test", reason="revert")
        self.assertTrue(result["rolled_back"])
        self.assertEqual(first["params_id"], result["target_params_id"])
        self.assertEqual(first["params_id"], SE.get_current_params(conn)["id"])
        self.assertNotEqual(start, first["params_id"])

    def test_repeated_rollback_is_idempotent(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        only = EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.033),
                                   source="evolve", reason="only")
        SE.activate_params_candidate(conn, only["params_id"], actor="test")
        EA.rollback_active(conn, actor="test")
        second = EA.rollback_active(conn, actor="test")
        self.assertFalse(second["rolled_back"])
        self.assertTrue(second.get("already_at_target"))

    def test_strategy_rollback_restores_inherited_global(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        manual = SE.manual_adjust(conn, {"confidence_threshold": 80}, reason="global 80")
        SE.activate_params_candidate(conn, manual["new_params_id"], actor="test")
        scoped = SE.adjust_strategy_params(conn, "tq_breakout",
                                           {"max_weight_delta": 0.033}, evidence_count=20)
        SE.activate_params_candidate(conn, scoped["new_params_id"], actor="test")
        result = SE.rollback_strategy_params(conn, "tq_breakout")
        self.assertTrue(result["rolled_back"])
        # 回到"继承全局"：回落的是当时的全局基线 80，不是出厂默认 70。
        self.assertEqual("global_default", result["target"])
        state = SE.get_strategy_params(conn, "tq_breakout")
        self.assertEqual(80, state["params"]["confidence_threshold"])
        self.assertEqual(0.03, state["params"]["max_weight_delta"])


class ConsumerTests(unittest.TestCase):
    """下游消费者只看到 active，看不到候选。"""

    def test_track_run_records_active_params_id(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        active_id = SE.get_current_params(conn)["id"]
        EA.create_candidate(conn, dict(BASE_PARAMS, max_weight_delta=0.033),
                            source="evolve", reason="noise")
        SE.track_run(conn, 4242, "test", "normal", "consensus")
        row = conn.execute(
            "SELECT evolution_params_id FROM evolution_tracking WHERE run_id=?", (4242,)
        ).fetchone()
        self.assertEqual(active_id, row["evolution_params_id"])

    def test_auto_evolve_never_activates(self):
        """自动进化只生成候选；runtime 参数必须保持不变。"""
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        for i in range(6):
            conn.execute(
                """INSERT INTO evolution_tracking(run_id, trigger, mode, status, applied,
                   applied_count, evolution_params_id, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (9000 + i, "scheduled-close", "normal", "consensus", 1, 2,
                 SE.get_current_params(conn)["id"], "2026-08-19T1%d:00:00" % i),
            )
        conn.commit()
        before = SE.get_current_params(conn)["id"]
        result = SE.auto_evolve_if_needed(conn)
        self.assertIsNotNone(result)
        self.assertTrue(result["evolved"])
        self.assertFalse(result["activated"])
        self.assertEqual(before, SE.get_current_params(conn)["id"])
        self.assertTrue(SE.validated_pending(conn))


class SideEffectTests(unittest.TestCase):
    """与"生效"绑定的副作用只在激活时执行。"""

    def test_risk_expansion_proposal_closes_only_at_activation(self):
        conn = _db()
        self.addCleanup(conn.close)
        SE.init_params(conn)
        import asymmetric_risk as AR
        calls: list = []
        original = AR.promote_proposal
        AR.promote_proposal = lambda c, s, k, v, actor="": (calls.append((s, k, v)), True)[1]
        try:
            created = SE.adjust_strategy_params(
                conn, "trend_pullback", {"max_weight_delta": 0.032},
                evidence_count=20, source="challenger_promotion", challenger_win=True)
            self.assertTrue(created["adjusted"])
            # 仅生成候选：提案必须仍未闭环。
            self.assertEqual([], calls)
            # 模拟一条已获授权的放大，验证它在**激活**时才 promote。
            conn.execute(
                "UPDATE evolution_params SET validation_detail=? WHERE id=?",
                (EA._json({"expansions": [{"key": "max_weight_delta", "new": 0.032}]}),
                 created["new_params_id"]),
            )
            conn.commit()
            SE.activate_params_candidate(conn, created["new_params_id"], actor="test")
        finally:
            AR.promote_proposal = original
        self.assertEqual([("trend_pullback", "max_weight_delta", 0.032)], calls)


class ApiContractTests(unittest.TestCase):
    """HTTP 边界：确认门禁 + 异常到状态码的映射。"""

    def test_activate_requires_confirmation(self):
        from fastapi import HTTPException

        import api_adaptive as API
        with self.assertRaises(HTTPException) as ctx:
            API.activate_evolution_candidate(params_id=1, confirmed=False, reason=None)
        self.assertEqual(409, ctx.exception.status_code)

    def test_activate_maps_errors_to_http_status(self):
        from fastapi import HTTPException

        import adaptive_engine as adaptive
        import api_adaptive as API

        original = adaptive.activate_evolution_candidate_fn
        try:
            cases = (
                (SE.CandidateNotFound("缺失"), 404),
                (SE.CandidateNotValidated("未校验"), 409),
                (SE.CandidateStale("基线过期"), 409),
                (SE.CandidateRejected("已拒绝"), 409),
                (SE.ActivationConflict("并发"), 409),
                (SE.EvolutionLifecycleError("指针损坏"), 503),
            )
            for exc, status in cases:
                def boom(*_a, _exc=exc, **_k):
                    raise _exc
                adaptive.activate_evolution_candidate_fn = boom
                with self.assertRaises(HTTPException) as ctx:
                    API.activate_evolution_candidate(params_id=1, confirmed=True, reason=None)
                self.assertEqual(status, ctx.exception.status_code, repr(exc))
        finally:
            adaptive.activate_evolution_candidate_fn = original

    def test_new_activate_endpoint_is_covered_by_operator_boundary(self):
        """operator 边界按 method 判定、与路径无关：新增 POST 端点自动受保护。"""
        import operator_auth as OA
        decision = OA.evaluate_request(
            "POST", {}, "203.0.113.7", scheme="http",
            host_header="paper.example.com", server_port=80,
        )
        self.assertFalse(decision.allowed)
        self.assertIn(decision.status, (401, 403))


if __name__ == "__main__":
    unittest.main()
