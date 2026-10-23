# -*- coding: utf-8 -*-
"""evolution_loop 崩溃窗口 / 续跑幂等回归测试。

场景：一代的全部阶段产物都已提交（``done_stages`` 完整），但写终态的
``UPDATE evolution_loop_state SET status='completed'`` 之前进程崩溃 / 数据库报错。
下次 ``run_loop`` 必须只补写终态：

- 绝不从头重跑 observe/mutate/apply（有副作用且会重复计数）；
- 绝不把已完成的代判成 ``interrupted`` 或永久 ``failed``；
- 历史遗留的重复 ``done_stages`` 必须先归一化。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import evolution_loop as EL
from evolution_loop import Backend


class CountingBackend(Backend):
    """记录每个阶段被真正调用了几次。"""

    def __init__(self):
        self.calls = []

    def observe(self, conn, generation, ctx):
        self.calls.append("observe")
        return {"params_id": 1, "params": {}, "has_data": True, "sample_count": 10}

    def evaluate(self, conn, generation, ctx):
        self.calls.append("evaluate")
        # 逐代递增，便于断言"改进量"是按上一代算的。
        return {"intelligence_score": 0.4 + 0.1 * generation}

    def mutate(self, conn, generation, ctx):
        self.calls.append("mutate")
        return {"mutated": False, "params_id": 1, "active_params_id": 1,
                "candidate_params_id": None}

    def validate(self, conn, generation, ctx):
        self.calls.append("validate")
        return {"valid": True, "params_id": 1}

    def apply(self, conn, generation, ctx):
        self.calls.append("apply")
        return {"applied_params_id": None, "active_params_id_end": 1,
                "pending_activation": False}


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


class CrashWindowRecoveryTests(unittest.TestCase):
    def _run_once(self, conn, backend):
        rep = EL.run_loop(conn, backend, generations=1)
        self.assertEqual(rep["completed"], 1)
        row = conn.execute(
            "SELECT status, done_stages, finished_at FROM evolution_loop_state "
            "WHERE generation=1").fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(EL._loads(row["done_stages"], []), list(EL.STAGES))
        self.assertIsNotNone(row["finished_at"])
        return row

    def _simulate_crash_before_terminal_update(self, conn):
        """模拟"阶段全落盘、终态未提交"的崩溃窗口。"""
        conn.execute(
            "UPDATE evolution_loop_state SET status='running', finished_at=NULL, "
            "current_stage='apply' WHERE generation=1")
        conn.commit()

    def test_all_stages_done_but_status_running_finalizes_completed(self):
        conn = _mem_conn()
        self.addCleanup(conn.close)
        EL.ensure_loop_schema(conn)
        backend = CountingBackend()
        self._run_once(conn, backend)
        self._simulate_crash_before_terminal_update(conn)

        backend.calls.clear()
        rep = EL.run_loop(conn, backend, generations=1)

        # 修复前：从 observe 起重跑全部阶段 → done_stages 变成 10 条 → interrupted。
        self.assertEqual(backend.calls, [],
                         f"已完成的代不得重跑任何阶段，实际重跑了 {backend.calls}")
        self.assertEqual(rep["resumed"], 1)
        self.assertEqual(rep["completed"], 1)
        self.assertEqual(rep["interrupted"], 0)
        row = conn.execute(
            "SELECT status, done_stages, finished_at FROM evolution_loop_state "
            "WHERE generation=1").fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(EL._loads(row["done_stages"], []), list(EL.STAGES))
        self.assertIsNotNone(row["finished_at"])
        self.assertTrue(EL.is_today_generation_completed(conn))

    def test_completed_stages_do_not_consume_attempt_budget(self):
        """重试预算耗尽也必须补写 completed，而不是判永久 failed。"""
        conn = _mem_conn()
        self.addCleanup(conn.close)
        EL.ensure_loop_schema(conn)
        self._run_once(conn, CountingBackend())
        self._simulate_crash_before_terminal_update(conn)
        conn.execute(
            "UPDATE evolution_loop_state SET attempts=? WHERE generation=1",
            (EL.MAX_GEN_ATTEMPTS,))
        conn.commit()

        backend = CountingBackend()
        rep = EL.run_loop(conn, backend, generations=1)

        row = conn.execute(
            "SELECT status, done_stages FROM evolution_loop_state WHERE generation=1"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(EL._loads(row["done_stages"], []), list(EL.STAGES))
        self.assertEqual(backend.calls, [])
        self.assertEqual(rep["failed"], 0)
        self.assertEqual(rep["completed"], 1)

    def test_duplicated_done_stages_are_normalized(self):
        """历史遗留的重复 done_stages（旧版本重复追加）不得造成虚假未完成。"""
        conn = _mem_conn()
        self.addCleanup(conn.close)
        EL.ensure_loop_schema(conn)
        self._run_once(conn, CountingBackend())
        duplicated = list(EL.STAGES) + list(EL.STAGES)
        conn.execute(
            """UPDATE evolution_loop_state
               SET status='interrupted', finished_at=NULL, done_stages=?
               WHERE generation=1""",
            (EL._json(duplicated),))
        conn.commit()

        backend = CountingBackend()
        rep = EL.run_loop(conn, backend, generations=1)

        row = conn.execute(
            "SELECT status, done_stages FROM evolution_loop_state WHERE generation=1"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(EL._loads(row["done_stages"], []), list(EL.STAGES))
        self.assertEqual(backend.calls, [])
        self.assertEqual(rep["completed"], 1)

    def test_partial_generation_still_resumes_remaining_stages(self):
        """正常续跑语义不变：只补跑未完成的阶段。"""
        conn = _mem_conn()
        self.addCleanup(conn.close)
        EL.ensure_loop_schema(conn)
        conn.execute(
            """INSERT INTO evolution_loop_state(generation, status, current_stage,
               done_stages, attempts, started_at, updated_at, params_id_start)
               VALUES(1,'running','evaluate','["observe"]',1,'2026-08-19T10:00:00',
                      '2026-08-19T10:00:00',1)""")
        conn.commit()

        backend = CountingBackend()
        rep = EL.run_loop(conn, backend, generations=1)

        self.assertEqual(backend.calls, ["evaluate", "mutate", "validate", "apply"])
        self.assertEqual(rep["completed"], 1)
        row = conn.execute(
            "SELECT status, done_stages FROM evolution_loop_state WHERE generation=1"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(EL._loads(row["done_stages"], []), list(EL.STAGES))

    def test_finalize_keeps_generation_snapshot_and_improvement(self):
        """纯终态补写不得把已记录的 intelligence / improvement 覆盖成空值。

        修复前：续跑时 ``prev`` 查询取到的是本代自己刚写的快照，improvement
        恒为 0；纯补写路径还会把 intelligence_score 写成 NULL。
        """
        conn = _mem_conn()
        self.addCleanup(conn.close)
        EL.ensure_loop_schema(conn)
        rep = EL.run_loop(conn, CountingBackend(), generations=2)
        self.assertEqual(rep["completed"], 2)
        before = conn.execute(
            "SELECT intelligence_score, improvement FROM evolution_generation "
            "WHERE generation=2").fetchone()
        self.assertAlmostEqual(before["intelligence_score"], 0.6)
        self.assertAlmostEqual(before["improvement"], 0.1)

        conn.execute(
            "UPDATE evolution_loop_state SET status='running', finished_at=NULL "
            "WHERE generation=2")
        conn.commit()
        backend = CountingBackend()
        EL.run_loop(conn, backend, generations=1)
        self.assertEqual(backend.calls, [])

        after = conn.execute(
            "SELECT intelligence_score, improvement FROM evolution_generation "
            "WHERE generation=2").fetchone()
        self.assertAlmostEqual(after["intelligence_score"], 0.6)
        self.assertAlmostEqual(after["improvement"], 0.1)


class MutateCrashWindowIdempotencyTests(unittest.TestCase):
    """mutate 的崩溃窗口：候选已 commit、state 未记录时不得重复产候选。

    真实故障链：``self_evolution.evolve()`` 内部会先 commit 候选行，引擎随后才把
    ``candidate_params_id`` 写回 ``evolution_loop_state``。若进程在这两步之间死掉，
    续跑会重新进入 mutate。修复后以 ``loop_generation`` 为幂等键复用既有候选，
    并由部分唯一索引在数据库层兜底。
    """

    def _db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        EL.ensure_loop_schema(conn)
        import self_evolution as SE
        import evolution_activation as EA
        SE.ensure_schema(conn)
        EA.ensure_schema(conn)
        return conn

    def test_crash_between_candidate_commit_and_checkpoint_reuses_the_candidate(self):
        conn = self._db()
        self.addCleanup(conn.close)
        import self_evolution as SE
        import evolution_activation as EA

        params = dict(SE.get_current_params(conn)["params"])
        calls = {"n": 0}

        def flaky(conn_, generation=None):
            calls["n"] += 1
            created = EA.create_candidate(
                conn_, params, source="evolve", reason="test crash window",
                loop_generation=generation)
            if calls["n"] == 1:
                raise RuntimeError("crash after candidate commit")
            return {"evolved": True, "new_params_id": created["params_id"],
                    "validation_state": created.get("validation_state")}

        with mock.patch.object(SE, "auto_evolve_if_needed", side_effect=flaky):
            first = EL.run_loop(conn, EL.ProductionBackend(), generations=1)
            self.assertEqual(first["completed"], 0)
            self.assertEqual(first["interrupted"], 1)
            rows = conn.execute(
                "SELECT id FROM evolution_params WHERE loop_generation IS NOT NULL"
            ).fetchall()
            self.assertEqual(len(rows), 1, "崩溃前候选应已落库")
            candidate_id = rows[0]["id"]

            second = EL.run_loop(conn, EL.ProductionBackend(), generations=1)

        self.assertEqual(calls["n"], 1, "续跑必须复用已落库候选，不得再次调用 evolve")
        rows = conn.execute(
            "SELECT id FROM evolution_params WHERE loop_generation=1").fetchall()
        self.assertEqual([row["id"] for row in rows], [candidate_id],
                         "同一代不得出现第二条候选")
        self.assertEqual(second["completed"], 1)
        self.assertEqual(second["interrupted"], 0)
        state = conn.execute(
            "SELECT status, candidate_params_id FROM evolution_loop_state WHERE generation=1"
        ).fetchone()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["candidate_params_id"], candidate_id)

    def test_database_enforces_one_candidate_per_generation(self):
        conn = self._db()
        self.addCleanup(conn.close)
        import self_evolution as SE
        import evolution_activation as EA

        params = dict(SE.get_current_params(conn)["params"])
        first = EA.create_candidate(
            conn, params, source="evolve", reason="a", loop_generation=7)
        with self.assertRaises(sqlite3.IntegrityError):
            EA.create_candidate(
                conn, params, source="evolve", reason="b", loop_generation=7)
        # 非 loop 路径（NULL）不受部分唯一索引影响：人工/AI/策略调参可多次候选。
        EA.create_candidate(conn, params, source="manual", reason="c")
        EA.create_candidate(conn, params, source="manual", reason="d")
        rows = conn.execute(
            "SELECT id FROM evolution_params WHERE loop_generation=7").fetchall()
        self.assertEqual([row["id"] for row in rows], [first["params_id"]])

    def test_evolve_records_the_loop_generation(self):
        conn = self._db()
        self.addCleanup(conn.close)
        import self_evolution as SE

        with mock.patch.object(SE, "get_performance_metrics", return_value={
                "has_data": True, "sample_count": 10,
                "learning_consensus_rate": 0.1, "avg_eval_score": 0.0}):
            result = SE.evolve(conn, reason="test", generation=11)

        self.assertTrue(result.get("evolved"), result)
        row = conn.execute(
            "SELECT loop_generation FROM evolution_params WHERE id=?",
            (result["new_params_id"],)).fetchone()
        self.assertEqual(row["loop_generation"], 11)


if __name__ == "__main__":
    unittest.main(verbosity=2)
