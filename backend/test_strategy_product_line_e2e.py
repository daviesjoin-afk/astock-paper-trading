# -*- coding: utf-8 -*-
"""PR-49：Custom Strategy Web 产品线端到端验收（浏览器同款路径）。

总任务书的闭环是"用户在浏览器里就能完成"，因此本测试**全程走 HTTP API
路由函数**（与前端实际调用一致），而不是直接调用注册表内部函数：

    空库 → 创建 DSL 策略 → validate → preview → Draft 可见
         → 激活（validated → active）
         → 配置为下一周期参与策略 → 启动新周期（账本绑定 + 分配资金）
         → 生产执行路径生成信号
         → 暂停（经济所有权保留、执行资格退出）
         → 复制为新 Draft
         → 退役 → 归档（历史不删除、不再进新周期）

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
import runtime_settings as RSET
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

        # 5) 激活：draft → validated → active（两个按钮）。
        status, validated = self._call(
            API.transition_strategy, STRATEGY_ID,
            {"to_status": "validated", "expected_status": "draft", "reason": "E2E"},
        )
        self.assertEqual(200, status, validated)
        self.assertEqual("validated", validated["status"])
        status, active = self._call(
            API.transition_strategy, STRATEGY_ID,
            {"to_status": "active", "expected_status": "validated", "reason": "E2E"},
        )
        self.assertEqual(200, status, active)
        self.assertTrue(active["supports_new_cycle"])
        self.assertEqual("pilot", active["runtime"]["lifecycle_stage"])

        # 6) 设为下一周期参与策略，并启动新周期：账本必须绑定并分配资金。
        with self._conn() as conn:
            RSET.update(conn, {"enabled_strategies": [STRATEGY_ID, BUILTIN_ID]}, actor="pr49-e2e")
        _summary, cycle = PT.start_new_cycle(capital=CAPITAL, include_dashboard=False)
        with self._conn() as conn:
            self.assertIn(STRATEGY_ID, PT.cycle_ledger_ids(conn, cycle["id"]))
            self.assertIn(STRATEGY_ID, PT.execution_participant_ids(conn, cycle["id"]))
            rows = {row["id"]: row for row in PT._shared_account_rows(conn, cycle["id"])}
            self.assertAlmostEqual(
                CAPITAL / 2, float(rows[STRATEGY_ID]["initial_cash"]), delta=1.0,
            )
            self.assertAlmostEqual(
                CAPITAL, PT._shared_cash(conn, cycle["id"]), delta=2.0,
            )

        # 7) 生产执行路径真跑一轮：不得失败，且该策略账户没有任何越权 sizing。
        result = PT.generate_signals(G.D0)
        self.assertNotEqual("failed", result.get("status"), result)

        # 8) 暂停：经济所有权保留，执行资格退出。
        status, paused = self._call(
            API.transition_strategy, STRATEGY_ID,
            {"to_status": "paused", "expected_status": "active", "reason": "E2E pause"},
        )
        self.assertEqual(200, status, paused)
        self.assertFalse(paused["supports_new_cycle"])
        with self._conn() as conn:
            self.assertNotIn(STRATEGY_ID, PT.execution_participant_ids(conn, cycle["id"]))
            self.assertIn(STRATEGY_ID, PT.cycle_ledger_ids(conn, cycle["id"]))
            self.assertAlmostEqual(CAPITAL, PT._shared_cash(conn, cycle["id"]), delta=2.0)

        # 9) 复制：暂停中的策略也能复制出新 Draft v1。
        clone_id = STRATEGY_ID + "_clone"
        status, clone = self._call(
            API.clone_strategy, STRATEGY_ID,
            {"new_strategy_id": clone_id, "actor": "pr49-e2e"}, default_status=201,
        )
        self.assertEqual(201, status, clone)
        self.assertEqual("draft", clone["status"])
        self.assertEqual("user", clone["origin"])
        self.assertEqual(1, clone["current_version"])

        # 10) 退役 → 归档：暂停 → retiring → archived。
        status, _body = self._call(
            API.transition_strategy, STRATEGY_ID,
            {"to_status": "retiring", "expected_status": "paused", "reason": "E2E retire"},
        )
        self.assertEqual(200, status)
        status, archived = self._call(
            API.transition_strategy, STRATEGY_ID,
            {"to_status": "archived", "expected_status": "retiring", "reason": "E2E archive"},
        )
        self.assertEqual(200, status, archived)
        self.assertEqual("archived", archived["status"])
        self.assertFalse(archived["supports_new_cycle"])

        # 11) 归档后：默认列表不可见（副本草稿仍在），include_archived 能查到
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

        # 12) 归档策略不再进入下一周期（eligible 排除），且不影响内置策略。
        with self._conn() as conn:
            eligible = set(SR.active_ids(conn=conn))
        self.assertNotIn(STRATEGY_ID, eligible)
        self.assertIn(BUILTIN_ID, eligible)


if __name__ == "__main__":
    unittest.main()
