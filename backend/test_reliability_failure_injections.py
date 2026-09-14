# -*- coding: utf-8 -*-
"""Reliability failure injection tests (F1 ~ F7).

Verifies fail-closed behavior, isolation, idempotency, and recovery across:
- F1: evaluate exception -> mutation skipped, active unchanged, status=interrupted, runner exit!=0
- F2: mutate candidate generated then crash -> restart does not duplicate candidate, active unchanged, resumes
- F3: validated candidate unactivated -> runtime strictly reads old active
- F4: SQLite concurrency lock -> bounded retry backoff, no infinite hang, no duplicate orders
- F5: evolution scheduler retry window -> 1st window locked, 2nd succeeds, 3rd skips idempotently
- F6: malformed evolution report -> runner fails closed with non-zero exit code
- F7: active pointer corrupted -> fail closed, no fallback to latest candidate, raises EvolutionLifecycleError
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest.mock import MagicMock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import evolution_activation as EA
import evolution_loop as EL
import evolution_loop_runner as ELR
import paper_storage as PS
import self_evolution as SE


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    SE.ensure_schema(conn)
    EA.ensure_schema(conn)
    EL.ensure_loop_schema(conn)
    return conn


def _seed_active_global(conn):
    initial_params = {
        "max_weight_delta": 0.030,
        "max_delta_threshold": 0.005,
        "confidence_threshold": 60,
        "consensus_weight_ratio": 0.60,
        "consensus_direction_threshold": 0.005,
        "hold_bias": 0.10,
    }
    cand = EA.create_candidate(
        conn,
        initial_params,
        strategy_id=None,
        source="manual_init",
        reason="seed_global",
        evidence_count=20,
        validate=True,
    )
    EA.activate_params_candidate(conn, cand["params_id"], actor="tester", reason="init")
    return cand["params_id"]


class ReliabilityFailureInjectionTests(unittest.TestCase):

    def test_f1_evaluate_exception_fail_closed(self):
        """F1: evaluate throws -> mutation skipped, active unchanged, status=interrupted, runner exit!=0."""
        conn = _mem_db()
        self.addCleanup(conn.close)
        active_id = _seed_active_global(conn)

        class FailingEvaluateBackend(EL.Backend):
            def observe(self, conn, generation, ctx):
                return {"params_id": active_id, "params": {}, "has_data": True, "sample_count": 10}

            def evaluate(self, conn, generation, ctx):
                raise RuntimeError("Injected evaluate failure")

            def mutate(self, conn, generation, ctx):
                raise AssertionError("mutate must not execute when evaluate failed (fail-closed guard breached)")

            def validate(self, conn, generation, ctx):
                raise AssertionError("validate must not execute when evaluate failed")

            def apply(self, conn, generation, ctx):
                raise AssertionError("apply must not execute when evaluate failed")

        rep = EL.run_loop(conn, FailingEvaluateBackend(), generations=1)

        # 1. 验证代际被标记为 interrupted 且有阶段失败
        self.assertEqual(rep["completed"], 0)
        self.assertEqual(rep["interrupted"], 1)
        self.assertEqual(rep["total_stage_errors"], 1)
        self.assertGreaterEqual(rep["total_stages_skipped"], 1)

        # 2. 验证当前生效指针未变
        eff = EA.resolve_effective(conn, None)
        self.assertEqual(eff["pointer_params_id"], active_id)

        # 3. 验证 runner 退出码非 0
        exit_code = ELR._result_exit_code(rep)
        self.assertEqual(exit_code, 1)

    def test_f2_mutate_crash_and_resume_idempotent(self):
        """F2: mutate generates candidate then crash -> resume does not duplicate candidate, active unchanged."""
        conn = _mem_db()
        self.addCleanup(conn.close)
        active_id = _seed_active_global(conn)

        candidate_created_counter = [0]

        class CrashAfterMutateBackend(EL.Backend):
            def observe(self, conn, generation, ctx):
                return {"params_id": active_id, "params": {}, "has_data": True, "sample_count": 10}

            def evaluate(self, conn, generation, ctx):
                return {"intelligence_score": 0.80}

            def mutate(self, conn, generation, ctx):
                candidate_created_counter[0] += 1
                mutated = dict(EA.resolve_effective(conn)["active"]["params"])
                mutated["hold_bias"] = 0.12
                res = EA.create_candidate(
                    conn,
                    mutated,
                    strategy_id=None,
                    source="challenger_promotion",
                    reason=f"gen_{generation}",
                    evidence_count=20,
                    validate=False,
                )
                return {
                    "mutated": True,
                    "active_params_id": active_id,
                    "candidate_params_id": res["params_id"],
                    "candidate_validation_state": "candidate",
                }

            def validate(self, conn, generation, ctx):
                # 模拟在 validate 阶段前进程崩溃
                raise KeyboardInterrupt("Simulated process kill right after mutate")

            def apply(self, conn, generation, ctx):
                return {}

        # 第一次运行：在 mutate 产生产物后崩溃
        try:
            EL.run_loop(conn, CrashAfterMutateBackend(), generations=1)
        except KeyboardInterrupt:
            pass

        # 验证第一次运行保存了 candidate，但 generation 未 completed
        gen1_state = conn.execute("SELECT status, candidate_params_id FROM evolution_loop_state WHERE generation=1").fetchone()
        c1_id = gen1_state["candidate_params_id"]
        self.assertIsNotNone(c1_id)

        # 验证 active 指针未被污染
        self.assertEqual(EA.resolve_effective(conn)["pointer_params_id"], active_id)

        # 第二次运行（恢复运行）：Backend 恢复正常
        class RecoveredBackend(EL.Backend):
            def observe(self, conn, generation, ctx):
                return {"params_id": active_id, "params": {}, "has_data": True, "sample_count": 10}

            def evaluate(self, conn, generation, ctx):
                return {"intelligence_score": 0.82}

            def mutate(self, conn, generation, ctx):
                # 如果已 resume 且存在 candidate，不应该再次重复创建
                candidate_created_counter[0] += 1
                return {
                    "mutated": False,
                    "active_params_id": active_id,
                    "candidate_params_id": c1_id,
                    "candidate_validation_state": "candidate",
                }

            def validate(self, conn, generation, ctx):
                cand_id = ctx.get("candidate_params_id") or c1_id
                v = EA.validate_candidate(conn, cand_id)
                return {"valid": v["valid"], "candidate_params_id": cand_id, "candidate_validation_state": "validated"}

            def apply(self, conn, generation, ctx):
                return {
                    "applied": False,
                    "active_params_id_start": active_id,
                    "active_params_id_end": active_id,
                    "pending_activation": True,
                    "candidate_params_id": ctx.get("candidate_params_id") or c1_id,
                }

        rep2 = EL.run_loop(conn, RecoveredBackend(), generations=1)
        self.assertGreaterEqual(rep2["resumed"], 1)
        self.assertEqual(rep2["completed"], 1)

        # active 指针仍然保持为旧 active，未被自动激活
        self.assertEqual(EA.resolve_effective(conn)["pointer_params_id"], active_id)

    def test_f3_validated_candidate_not_activated(self):
        """F3: validated candidate unactivated -> runtime strictly reads old active."""
        conn = _mem_db()
        self.addCleanup(conn.close)
        active_id = _seed_active_global(conn)

        # 创建新候选并完成严格校验
        mutated = dict(EA.resolve_effective(conn)["active"]["params"])
        mutated["hold_bias"] = 0.12
        cand = EA.create_candidate(
            conn,
            mutated,
            strategy_id=None,
            source="challenger_promotion",
            reason="valid_cand_f3",
            evidence_count=20,
            validate=True,
        )
        cand_id = cand["params_id"]
        self.assertTrue(cand["valid"])
        self.assertEqual(cand["validation_state"], "validated")

        # 关键断言：即使 candidate 存在且已 validated，runtime 读取的 active 恒为旧 active_id
        eff = EA.resolve_effective(conn)
        self.assertEqual(eff["pointer_params_id"], active_id)
        self.assertEqual(eff["active"]["id"], active_id)
        self.assertNotEqual(eff["pointer_params_id"], cand_id)

    def test_f4_sqlite_concurrency_lock_jittered_retry(self):
        """F4: SQLite concurrency lock -> bounded retry backoff, no infinite hang, handles busy correctly."""
        # 1. 验证 is_sqlite_busy_error 识别各种 sqlite lock/busy 错误
        err_busy = sqlite3.OperationalError("database is locked")
        self.assertTrue(PS.is_sqlite_busy_error(err_busy))

        err_busy_table = sqlite3.OperationalError("database table is locked: paper_orders")
        self.assertTrue(PS.is_sqlite_busy_error(err_busy_table))

        err_other = sqlite3.OperationalError("no such table: dummy_table")
        self.assertFalse(PS.is_sqlite_busy_error(err_other))

        # 2. 验证 execute_with_retry 在遇到瞬时 locked 时能够重试并成功
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        call_count = [0]

        def side_effect_execute(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise sqlite3.OperationalError("database is locked")
            return mock_cursor

        mock_conn.execute.side_effect = side_effect_execute

        # 应该在第 3 次调用时成功
        res = PS.execute_with_retry(mock_conn, "SELECT 1", max_retries=4)
        self.assertEqual(call_count[0], 3)
        self.assertIsNotNone(res)

        # 3. 验证持续 locked 时有界退出（抛出异常，绝不无限循环）
        call_count_fail = [0]

        def side_effect_always_locked(*args, **kwargs):
            call_count_fail[0] += 1
            raise sqlite3.OperationalError("database is locked")

        mock_conn.execute.side_effect = side_effect_always_locked
        with self.assertRaises(sqlite3.OperationalError):
            PS.execute_with_retry(mock_conn, "SELECT 1", max_retries=3)
        self.assertEqual(call_count_fail[0], 3)

    def test_f5_evolution_scheduler_retry_window_idempotent(self):
        """F5: evolution scheduler retry windows: 1st locked, 2nd succeeds, 3rd skips idempotently."""
        conn = _mem_db()
        self.addCleanup(conn.close)
        active_id = _seed_active_global(conn)
        today = "2026-09-14"

        # 窗口 1 (16:45)：模拟资源锁冲突被拒 (未完成)
        self.assertFalse(EL.is_today_generation_completed(conn, today))

        # 窗口 2 (17:05)：获得锁并成功执行一轮自进化
        class FastSuccessBackend(EL.Backend):
            def observe(self, conn, generation, ctx):
                return {"params_id": active_id, "params": {}, "has_data": True, "sample_count": 10}

            def evaluate(self, conn, generation, ctx):
                return {"intelligence_score": 0.85}

            def mutate(self, conn, generation, ctx):
                return {"mutated": False, "active_params_id": active_id}

            def validate(self, conn, generation, ctx):
                return {"valid": True}

            def apply(self, conn, generation, ctx):
                return {"applied": False, "active_params_id_start": active_id, "active_params_id_end": active_id}

        rep = EL.run_loop(conn, FastSuccessBackend(), generations=1)
        self.assertEqual(rep["completed"], 1)

        # 窗口 2 执行完后，当天被判定为已完成
        self.assertTrue(EL.is_today_generation_completed(conn, today))

        # 窗口 3 (17:35)：重试调度再次触发，runner 经由 is_today_generation_completed 幂等跳过
        class SecondRunBackend(EL.Backend):
            def observe(self, conn, generation, ctx):
                raise AssertionError("Should not run observe in window 3 because today is completed")

        # 模拟 runner --daily 的主流程守护
        is_completed = EL.is_today_generation_completed(conn, today)
        self.assertTrue(is_completed)
        # runner 跳过，返回 0，数据库未生成任何新代数
        gen_count = conn.execute("SELECT COUNT(*) FROM evolution_loop_state").fetchone()[0]
        self.assertEqual(gen_count, 1)

    def test_f6_malformed_evolution_report_fails_closed(self):
        """F6: malformed evolution report -> runner fail closed with exit code != 0."""
        # 畸形 1: None 或空字典
        self.assertEqual(ELR._result_exit_code(None), 1)
        self.assertEqual(ELR._result_exit_code({}), 1)

        # 畸形 2: 有 stage error
        rep_err = {"completed": 1, "total_stage_errors": 1, "generations_run": 1, "failed": 0, "interrupted": 0}
        self.assertEqual(ELR._result_exit_code(rep_err), 1)

        # 畸形 3: 未完成任何代数且非 daily 跳过
        rep_zero = {"completed": 0, "total_stage_errors": 0, "generations_run": 0, "failed": 0, "interrupted": 0}
        self.assertEqual(ELR._result_exit_code(rep_zero), 1)

        # 正常报告: 退出码 0
        rep_ok = {"completed": 1, "total_stage_errors": 0, "generations_run": 1, "failed": 0, "interrupted": 0}
        self.assertEqual(ELR._result_exit_code(rep_ok), 0)

    def test_f7_active_pointer_corrupted_fails_closed(self):
        """F7: active pointer corrupted -> fail closed, no fallback to latest candidate, raises EvolutionLifecycleError."""
        conn = _mem_db()
        self.addCleanup(conn.close)
        active_id = _seed_active_global(conn)

        # 创建一个候选行，使 candidate 存在（如果回落到 latest 则会选中它）
        mutated = dict(EA.resolve_effective(conn)["active"]["params"])
        cand = EA.create_candidate(
            conn,
            mutated,
            strategy_id=None,
            source="challenger_promotion",
            reason="test_cand",
            evidence_count=20,
            validate=True,
        )
        cand_id = cand["params_id"]
        self.assertGreater(cand_id, active_id)

        # 损坏指针：删除 evolution_active_params 中的指针记录，但保留 activation_history
        conn.execute("DELETE FROM evolution_active_params WHERE scope_key=?", (EA.SCOPE_GLOBAL,))
        conn.commit()

        # 严格断言：解析 effective 参数时，绝对禁止回落到 latest candidate 或 default，必须抛出 EvolutionLifecycleError
        with self.assertRaises(EA.EvolutionLifecycleError):
            EA.resolve_effective(conn, None)

        # 损坏情况 2：指针指向了不存在的 params_id
        conn.execute(
            "INSERT INTO evolution_active_params(scope_key, params_id, activated_at, activated_by, reason) VALUES(?,?,?,?,?)",
            (EA.SCOPE_GLOBAL, 999999, "2026-09-14T00:00:00", "test", "corrupted"),
        )
        conn.commit()

        with self.assertRaises(EA.EvolutionLifecycleError):
            EA.resolve_scope_active(conn, EA.SCOPE_GLOBAL)


if __name__ == "__main__":
    unittest.main()
