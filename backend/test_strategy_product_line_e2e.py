# -*- coding: utf-8 -*-
"""R31：Custom Strategy API 的生命周期与晋级路径验收。

总任务书的闭环是"用户在浏览器里就能完成"，因此本测试**全程走 HTTP API
路由函数**（与前端实际调用一致），而不是直接调用注册表内部函数：

    空库 → 创建 DSL 策略 → validate → preview → Draft 可见
         → exact candidate proposal/apply → research
         → 缺少 R29 时 validated proposal 被阻断
         → retiring → archived（历史保留，不能进入正式新周期）

对照面：
- ``test_api_strategies.py`` 锁 API 自身语义；
- ``test_cycle_ledger_ownership.py`` 锁账本/执行分离；
- 本文件把两者串成一条"用户点得出来"的完整路径，防止单点都绿、整体却走不通。
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import unittest

from fastapi import HTTPException

import api_strategies as API
import paper_trading as PT
import strategy_registry as SR
import strategy_runtime as SRT
import test_production_path_golden_replay as G

STRATEGY_ID = "e2e_momentum_alpha"
BUILTIN_ID = "tq_breakout"
CAPITAL = 300000.0


class StrategyProductLineE2ETests(G.OfflinePaperEnv, unittest.TestCase):
    """一条完整的「浏览器可完成」策略产品线路径。"""

    def setUp(self):
        self._db_index = getattr(self.__class__, "_db_seq", 0)
        self.__class__._db_seq = self._db_index + 1
        PT.DB_PATH = os.path.join(self._tmp, f"product_line_{self._db_index}.sqlite3")
        SRT.clear_cache()
        PT.init_db()

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(PT.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------- 与前端一致：调用路由函数并归一化 (status, body) ----------

    @staticmethod
    def _call(func, *args, default_status: int = 200, **kwargs):
        try:
            return default_status, func(*args, **kwargs)
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}

    def _draft_payload(self, strategy_id: str) -> dict:
        return {
            "id": strategy_id,
            "name": "E2E 动量策略",
            "description": "PR-49 产品线端到端验收策略",
            "metadata": {"style": "trend", "hold": 8, "candidate_topn": 10},
            "dsl_ast": G.RULE,
        }

    def test_browser_path_from_blank_to_archive(self):
        # 0) 空库：只有内置策略，没有任何自定义策略。
        _status, listing = self._call(API.list_strategies, include_archived=True)
        self.assertEqual(0, listing["summary"]["user"])

        # 1) 创建 Draft（浏览器「新建策略 → 保存草稿」）。
        status, created = self._call(
            API.create_strategy, self._draft_payload(STRATEGY_ID), default_status=201,
        )
        self.assertEqual(201, status, created)
        self.assertEqual("draft", created["status"])
        self.assertEqual("user", created["origin"])
        self.assertTrue(created["has_dsl"])

        # 2) validate（编辑器里的"验证 DSL"按钮）。
        status, validation = self._call(
            API.validate_strategy, {"dsl_ast": G.RULE, "metadata": {"style": "trend", "hold": 8}},
        )
        self.assertEqual(200, status, validation)
        self.assertTrue(validation["valid"], validation)

        # 3) preview（"风险与资金预览"按钮）——新用户策略停在 pilot（25%）。
        status, preview = self._call(
            API.preview_strategy,
            {"dsl_ast": G.RULE, "metadata": {"style": "trend", "hold": 8}},
        )
        self.assertEqual(200, status, preview)
        self.assertTrue(preview["valid"], preview)
        self.assertEqual("pilot", preview["allocation"]["lifecycle_stage"])
        self.assertEqual(0.25, preview["allocation"]["capital_scale"])

        # 4) Draft 在列表中可见（刷新后仍在）。
        _status, listing = self._call(API.list_strategies, origin="user")
        self.assertEqual(1, listing["summary"]["user"])
        self.assertEqual(STRATEGY_ID, listing["items"][0]["id"])

        # 5) draft → candidate：必须提交 exact version proposal。
        _status, lifecycle = self._call(API.get_strategy_lifecycle, STRATEGY_ID)
        proposal_base = {"strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"], "expected_state": "draft",
            "proposer_type": "human", "proposer_id": "product-line-e2e",
            "rationale": "verify R31 promotion flow"}
        status, candidate_proposal = self._call(API.create_strategy_promotion_proposal,
            STRATEGY_ID, {**proposal_base, "target_state": "candidate", "evidence_bundle": {}},
            default_status=201)
        self.assertEqual(201, status, candidate_proposal)
        self.assertTrue(candidate_proposal["decision"]["eligible"])
        status, candidate = self._call(API.transition_strategy, STRATEGY_ID,
            {"strategy_version": lifecycle["version"],
             "strategy_checksum": lifecycle["checksum"], "expected_state": "draft",
             "target_state": "candidate", "actor_type": "human",
             "actor_id": "product-line-e2e",
             "proposal_fingerprint": candidate_proposal["proposal_fingerprint"]})
        self.assertEqual(200, status, candidate)
        self.assertEqual("candidate", candidate["status"])
        self.assertFalse(candidate["formal_cycle_allowed"])

        # 6) candidate → research proposal can apply without performance evidence.
        status, research_proposal = self._call(API.create_strategy_promotion_proposal,
            STRATEGY_ID, {**proposal_base, "expected_state": "candidate",
                "target_state": "research", "evidence_bundle": {}}, default_status=201)
        self.assertEqual(201, status, research_proposal)
        self.assertTrue(research_proposal["decision"]["eligible"])
        status, research = self._call(API.transition_strategy, STRATEGY_ID,
            {"strategy_version": lifecycle["version"],
             "strategy_checksum": lifecycle["checksum"], "expected_state": "candidate",
             "target_state": "research", "actor_type": "human",
             "actor_id": "product-line-e2e",
             "proposal_fingerprint": research_proposal["proposal_fingerprint"]})
        self.assertEqual(200, status, research)
        self.assertEqual("research", research["status"])

        # 7) 未提供 exact R29 run 时 proposal 保持 blocked / fail closed。
        status, validation_proposal = self._call(API.create_strategy_promotion_proposal,
            STRATEGY_ID, {**proposal_base, "expected_state": "research",
                "target_state": "validated", "evidence_bundle": {}}, default_status=201)
        self.assertEqual(201, status, validation_proposal)
        self.assertFalse(validation_proposal["decision"]["eligible"])
        self.assertIn("exact_r29_run_key_required",
                      validation_proposal["decision"]["blocking_reasons"])

        # 8) 克隆仍会创建独立 Draft v1。
        clone_id = STRATEGY_ID + "_clone"
        status, clone = self._call(
            API.clone_strategy, STRATEGY_ID,
            {"new_strategy_id": clone_id, "actor": "pr49-e2e"}, default_status=201,
        )
        self.assertEqual(201, status, clone)
        self.assertEqual("draft", clone["status"])
        self.assertEqual("user", clone["origin"])
        self.assertEqual(1, clone["current_version"])

        # 9) safety intent 退役 → 归档，仍要求精确版本与明确原因。
        status, _body = self._call(
            API.transition_strategy, STRATEGY_ID,
            {"strategy_version": lifecycle["version"],
             "strategy_checksum": lifecycle["checksum"], "expected_state": "research",
             "target_state": "retiring", "actor_type": "human",
             "actor_id": "product-line-e2e", "reason_code": "e2e-retire",
             "reason": "E2E retire"},
        )
        self.assertEqual(200, status)
        status, archived = self._call(
            API.transition_strategy, STRATEGY_ID,
            {"strategy_version": lifecycle["version"],
             "strategy_checksum": lifecycle["checksum"], "expected_state": "retiring",
             "target_state": "archived", "actor_type": "human",
             "actor_id": "product-line-e2e", "reason_code": "e2e-archive",
             "reason": "E2E archive"},
        )
        self.assertEqual(200, status, archived)
        self.assertEqual("archived", archived["status"])
        self.assertFalse(archived["supports_new_cycle"])

        # 10) 归档后：默认列表不可见（副本草稿仍在），include_archived 能查到
        # （历史不删除）。
        _status, visible = self._call(API.list_strategies, origin="user")
        visible_ids = [item["id"] for item in visible["items"]]
        self.assertNotIn(STRATEGY_ID, visible_ids)
        self.assertIn(clone_id, visible_ids)
        _status, all_items = self._call(API.list_strategies, origin="user", include_archived=True)
        self.assertIn(STRATEGY_ID, [item["id"] for item in all_items["items"]])
        # 版本与事件时间线仍可追溯。
        status, versions = self._call(API.list_strategy_versions, STRATEGY_ID)
        self.assertEqual(200, status, versions)
        self.assertTrue(versions["items"])
        status, events = self._call(API.list_strategy_events, STRATEGY_ID)
        self.assertEqual(200, status, events)
        self.assertGreaterEqual(len(events["items"]), 4)

        # 11) 归档策略不再进入正式新周期，内置 formal-cycle scope 不受影响。
        with self._conn() as conn:
            eligible = set(SR.active_ids(conn=conn))
        self.assertNotIn(STRATEGY_ID, eligible)
        self.assertIn(BUILTIN_ID, eligible)


if __name__ == "__main__":
    unittest.main()
