# -*- coding: utf-8 -*-
"""evolution_loop 闭环引擎的离线验证（无 AI / 无网络）。

覆盖：完整多代运行、模拟中断后续跑、单阶段异常隔离且循环继续、
参数逐代漂移、样本不足自愈(skip)、时间预算优雅收尾、真实 self_evolution 集成演化。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

import evolution_loop as EL
from evolution_loop import Backend, ProductionBackend


# ──────────────────────────────────────────────────────────────────────────
# FakeBackend：模拟一个会"越跑越聪明"、参数逐代漂移的后端
# ──────────────────────────────────────────────────────────────────────────
class FakeBackend(Backend):
    def __init__(self, fail_stage=None, fail_once=True, stale=False):
        self.params = {"learning_rate": 0.10, "threshold": 0.50}
        self.gen = 0
        self.fail_stage = fail_stage
        self.fail_once = fail_once
        self._failed = set()
        self.stale = stale  # 模拟缺数据：observe 返回 has_data=False

    def observe(self, conn, generation, ctx):
        return {"params_id": self.gen, "params": dict(self.params),
                "has_data": (not self.stale), "sample_count": 0 if self.stale else 10}

    def evaluate(self, conn, generation, ctx):
        if self.fail_stage == "evaluate" and "evaluate" not in self._failed:
            self._failed.add("evaluate")
            raise RuntimeError("模拟评估阶段异常（瞬时故障）")
        # 智能分随代数单调递增，模拟"越来越聪明"。
        return {"intelligence_score": min(1.0, 0.30 + 0.10 * generation)}

    def mutate(self, conn, generation, ctx):
        # 参数逐代漂移（动态调参的具象化）。
        self.params = {
            "learning_rate": round(self.params["learning_rate"] + 0.01, 4),
            "threshold": round(self.params["threshold"] - 0.02, 4),
        }
        self.gen = generation
        return {"mutated": True, "params_id": generation,
                "adjustments": ["lr+0.01", "thr-0.02"], "changed_keys": ["learning_rate", "threshold"]}

    def validate(self, conn, generation, ctx):
        if self.fail_stage == "validate" and "validate" not in self._failed:
            self._failed.add("validate")
            raise RuntimeError("模拟校验阶段异常")
        return {"valid": True, "params_id": generation,
                "out_of_bounds_corrected": False, "params": dict(self.params)}

    def apply(self, conn, generation, ctx):
        return {"applied_params_id": generation, "active_params_id": generation}


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


# ──────────────────────────────────────────────────────────────────────────
# 测试
# ──────────────────────────────────────────────────────────────────────────
class EvolutionLoopTests(unittest.TestCase):
    def test_full_multi_generation_run(self):
        """连续跑 5 代，全部完成且无异常、无丢失。"""
        conn = _mem_conn()
        EL.ensure_loop_schema(conn)
        rep = EL.run_loop(conn, FakeBackend(), generations=5)
        self.assertEqual(rep["generations_run"], 5)
        self.assertEqual(rep["completed"], 5)
        self.assertEqual(rep["interrupted"], 0)
        self.assertEqual(rep["failed"], 0)
        # 每一代都记录了 intelligence 且逐代递增。
        scores = [d["intelligence_score"] for d in rep["details"]]
        self.assertEqual(scores, sorted(scores))
        self.assertGreater(scores[-1], scores[0])

    def test_resume_after_interruption(self):
        """手动插入一个被中断的代(只跑完 observe)，续跑能补齐剩余阶段。"""
        conn = _mem_conn()
        EL.ensure_loop_schema(conn)
        # 模拟上一次进程在 evaluate 之前崩溃：写了 running + done_stages=["observe"]
        conn.execute(
            """INSERT INTO evolution_loop_state(generation, status, current_stage, done_stages,
               attempts, started_at, updated_at, params_id_start)
               VALUES(1,'running','observe','["observe"]',1,'2026-08-19T10:00:00','2026-08-19T10:00:00',0)"""
        )
        conn.commit()
        # 续跑 1 代（应拾起 gen1 从 evaluate 继续），再开 gen2。
        rep = EL.run_loop(conn, FakeBackend(), generations=1)
        self.assertGreaterEqual(rep["resumed"], 1)
        # gen1 现在应完成 5 个阶段。
        row = conn.execute(
            "SELECT status, done_stages FROM evolution_loop_state WHERE generation=1"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(EL._loads(row["done_stages"], []), list(EL.STAGES))

    def test_stage_exception_isolated_loop_continues(self):
        """某阶段抛异常，整轮不崩，循环继续跑完后续代。"""
        conn = _mem_conn()
        EL.ensure_loop_schema(conn)
        rep = EL.run_loop(conn, FakeBackend(fail_stage="evaluate"), generations=3)
        # 三代数全部被驱动（循环未死）。
        self.assertEqual(rep["generations_run"], 3)
        self.assertGreaterEqual(rep["total_stage_errors"], 1)
        # 即便 evaluate 失败，每代仍走完 observe/mutate/validate/apply（best-effort）。
        for gen in (1, 2, 3):
            ds = conn.execute(
                "SELECT done_stages FROM evolution_loop_state WHERE generation=?", (gen,)
            ).fetchone()[0]
            ds = EL._loads(ds, [])
            self.assertIn("observe", ds)
            self.assertIn("mutate", ds)

    def test_stale_data_self_heal_skip(self):
        """样本不足时 evaluate 阶段自愈为 skip，而非阻塞或抛错。"""
        conn = _mem_conn()
        EL.ensure_loop_schema(conn)
        rep = EL.run_loop(conn, FakeBackend(stale=True), generations=2)
        self.assertEqual(rep["generations_run"], 2)
        self.assertGreaterEqual(rep["total_stages_skipped"], 1)
        # 跳过 evaluate 后整轮仍能收尾（不卡死）。
        self.assertGreaterEqual(rep["completed"] + rep["interrupted"], 1)

    def test_time_budget_graceful_stop(self):
        """时间预算耗尽时优雅收尾，不无限运行。"""
        conn = _mem_conn()
        EL.ensure_loop_schema(conn)
        rep = EL.run_loop(conn, FakeBackend(), generations=100, time_budget_seconds=0.0001)
        self.assertLess(rep["generations_run"], 100)
        self.assertGreaterEqual(rep["generations_run"], 1)

    def test_param_drift_across_generations(self):
        """参数随代演化（动态调参机制生效）。"""
        conn = _mem_conn()
        EL.ensure_loop_schema(conn)
        b = FakeBackend()
        EL.run_loop(conn, b, generations=4)
        # 4 代后 learning_rate 应累计 +0.04，threshold 累计 -0.08。
        self.assertAlmostEqual(b.params["learning_rate"], 0.10 + 0.04, places=4)
        self.assertAlmostEqual(b.params["threshold"], 0.50 - 0.08, places=4)

    def test_loop_status_query(self):
        conn = _mem_conn()
        EL.ensure_loop_schema(conn)
        EL.run_loop(conn, FakeBackend(), generations=2)
        st = EL.loop_status(conn)
        self.assertEqual(len(st["recent_states"]), 2)
        self.assertEqual(len(st["recent_generations"]), 2)
        self.assertEqual(st["max_gen_attempts"], EL.MAX_GEN_ATTEMPTS)


class ProductionIntegrationTests(unittest.TestCase):
    """用真实 self_evolution 验证 ProductionBackend 真的会调参演化。"""

    def _seed(self, conn):
        import self_evolution as SE
        SE.ensure_schema(conn)
        SE.init_params(conn, source="init")
        # 6 条全 consensus 追踪 → 共识率 1.0 → should_evolve 触发"收紧共识幅度比"。
        for i in range(6):
            conn.execute(
                """INSERT INTO evolution_tracking(run_id, trigger, mode, status, applied,
                   applied_count, evolution_params_id, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (1000 + i, "scheduled-close", "normal", "consensus", 1, 2,
                 SE.get_current_params(conn)["id"], "2026-08-19T1%d:00:00" % i),
            )
        conn.commit()

    def test_production_backend_evolves_params(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            self._seed(conn)
            import self_evolution as SE
            before = SE.get_current_params(conn)["id"]
            rep = EL.run_loop(conn, ProductionBackend(), generations=1)
            self.assertEqual(rep["generations_run"], 1)
            after = SE.get_current_params(conn)["id"]
            # 真的发生了调参（新参数版本）。
            self.assertNotEqual(before, after)
            # 逐代快照记录了参数版本变化。
            grow = conn.execute(
                "SELECT params_id_start, params_id_end FROM evolution_generation WHERE generation=1"
            ).fetchone()
            self.assertEqual(grow[0], before)
            self.assertEqual(grow[1], after)
        finally:
            conn.close()
            os.remove(path)

    def test_production_no_crash_with_empty_db(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            rep = EL.run_loop(conn, ProductionBackend(), generations=3)
            self.assertEqual(rep["generations_run"], 3)
            # 空库无样本 → evaluate 跳过，但整轮闭环照常完成。
            self.assertGreaterEqual(rep["completed"] + rep["interrupted"], 1)
        finally:
            conn.close()
            os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
