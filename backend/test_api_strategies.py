# -*- coding: utf-8 -*-
"""PR-45：Strategy Admin API（``/api/strategies``）闭环与错误映射。

端到端跑通产品验收链条：创建 draft → 列表可见 → validate → transition
validated → transition active（supports_new_cycle=true）→ 编辑产生 v2 →
clone → pause → resume；并锁定错误语义：非法 id 400 / 重复 id 409 /
非法 DSL 422 / 版本冲突 409 / 非法状态迁移 409 / 已引用与内置策略不可删。
"""
from __future__ import annotations

import os

import shutil
import tempfile
import types
import unittest.mock

import unittest

from fastapi.testclient import TestClient

import api_strategies as API
import main
import paper_trading as P
import strategy_registry as SR
import strategy_runtime as SRT

# 声明式 DSL：close > ma20 且 volume > volume_mean5 × 1.2（纯离线可判）。
RULE = {
    "op": "and",
    "args": [
        {"op": "gt", "left": {"op": "field", "name": "close"},
         "right": {"op": "indicator", "name": "ma", "window": 20}},
        {"op": "gt", "left": {"op": "field", "name": "volume"},
         "right": {"op": "mul", "left": {"op": "indicator", "name": "volume_mean", "window": 5},
                   "right": {"op": "const", "value": 1.2}}},
    ],
}
# 根是常量而非布尔表达式 → 非法 DSL（normalize 会拒绝）。
BAD_RULE = {"op": "const", "value": 1}


class ApiStrategiesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-api-strategies-")
        cls._old_db = P.DB_PATH
        cls._old_sr_db = SR.DEFAULT_DB_PATH
        P.DB_PATH = os.path.join(cls._tmp, "paper_trading.sqlite3")
        SR.DEFAULT_DB_PATH = P.DB_PATH
        P.init_db()
        SRT.clear_cache()
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls):
        SRT.clear_cache()
        P.DB_PATH = cls._old_db
        SR.DEFAULT_DB_PATH = cls._old_sr_db
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        SRT.clear_cache()

    # ---------- 辅助 ----------

    def _create(self, strategy_id: str, **overrides):
        payload = {
            "id": strategy_id,
            "name": f"{strategy_id} 名称",
            "description": "接口测试策略",
            "metadata": {"style": "trend", "hold": 8, "candidate_topn": 10},
            "dsl_ast": RULE,
        }
        payload.update(overrides)
        return self.client.post("/api/strategies", json=payload)

    def _create_draft(self, strategy_id: str):
        response = self._create(strategy_id)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _reference(self, strategy_id: str) -> None:
        """制造一条历史引用，使该 draft 不再可硬删除。"""
        with P._db() as conn:
            version = SR.get_version(strategy_id, conn=conn)
            # paper_audit 有版本戳触发器：引用行必须带合法 version/checksum。
            conn.execute(
                "INSERT INTO paper_audit(account_id,event,detail,created_at,strategy_id,"
                " strategy_version,strategy_checksum) VALUES(?,'unit-test','{}',?,?,?,?)",
                (strategy_id, "2026-09-10T00:00:00+00:00", strategy_id,
                 version.version, version.checksum),
            )

    # ---------- 1) 列表 ----------

    def test_list_returns_builtins_and_user_with_summary(self):
        self._create_draft("api_list_alpha")
        response = self.client.get("/api/strategies")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        ids = {item["id"] for item in body["items"]}
        self.assertIn("trend_pullback", ids)
        self.assertIn("api_list_alpha", ids)
        summary = body["summary"]
        self.assertEqual(summary["total"], len(body["items"]))
        self.assertEqual(summary["user"], sum(1 for i in body["items"] if i["origin"] == "user"))
        self.assertEqual(summary["builtin"], sum(1 for i in body["items"] if i["origin"] == "builtin"))
        self.assertEqual(summary["draft"], sum(1 for i in body["items"] if i["status"] == "draft"))
        self.assertGreaterEqual(summary["builtin"], 5)
        self.assertTrue(summary["user"])

    def test_list_origin_filter(self):
        response = self.client.get("/api/strategies", params={"origin": "builtin"})
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertTrue(items)
        self.assertTrue(all(item["origin"] == "builtin" for item in items))

    def test_list_invalid_origin_is_rejected(self):
        response = self.client.get("/api/strategies", params={"origin": "martian"})
        self.assertEqual(response.status_code, 400)

    # ---------- 2) 详情 ----------

    def test_detail_includes_definition_version_and_runtime(self):
        created = self._create_draft("api_detail_alpha")
        response = self.client.get(f"/api/strategies/{created['id']}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], "api_detail_alpha")
        self.assertEqual(body["origin"], "user")
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["version"], 1)
        self.assertTrue(body["definition"]["dsl_ast"])
        self.assertEqual(body["definition"]["dsl_ast"], RULE)
        self.assertTrue(body["runtime_ready"], body.get("runtime"))
        self.assertIn("risk_fingerprint", body["runtime"])
        self.assertIn("risk_profile", body["runtime"])
        self.assertIn("execution_profile", body["runtime"])
        # PR-26：draft 不部署资金（shadow），validated/active 才是 pilot。
        self.assertEqual(body["runtime"]["lifecycle_stage"], "shadow")
        self.assertIn("items", self.client.get("/api/strategies/api_detail_alpha/events").json())

    def test_detail_unknown_strategy_404(self):
        self.assertEqual(self.client.get("/api/strategies/nope_missing").status_code, 404)

    def test_detail_runtime_failure_does_not_500(self):
        # RuntimeContext 构建失败（编译期异常）必须降级为 runtime_ready=false，
        # 而不是让整个详情页 500。只替换 API 模块内的运行时引用，
        # 避免影响 paper_trading 的 init_db 路径。
        created = self._create_draft("api_runtime_broken")

        def _boom(*_args, **_kwargs):
            raise ValueError("编译失败")

        fake_runtime = types.SimpleNamespace(get_context=_boom)
        with unittest.mock.patch.object(API, "SRT", fake_runtime):
            response = self.client.get(f"/api/strategies/{created['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["runtime_ready"])
        self.assertIn("编译失败", str(body["runtime"]["runtime_error"]))

    # ---------- 3) 创建 ----------

    def test_create_returns_201_with_full_object(self):
        body = self._create_draft("api_create_alpha")
        self.assertEqual(body["origin"], "user")
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["current_version"], 1)
        self.assertTrue(body["current_checksum"])
        self.assertTrue(body["has_dsl"])

    def test_create_invalid_id_400(self):
        response = self._create("BAD ID!")
        self.assertEqual(response.status_code, 400, response.text)

    def test_create_duplicate_id_409(self):
        self._create_draft("api_dup_alpha")
        response = self._create("api_dup_alpha")
        self.assertEqual(response.status_code, 409, response.text)

    def test_create_builtin_conflict_409(self):
        response = self._create("trend_pullback")
        self.assertEqual(response.status_code, 409, response.text)

    def test_create_invalid_dsl_422(self):
        response = self._create("api_bad_dsl", dsl_ast=BAD_RULE)
        self.assertEqual(response.status_code, 422, response.text)

    # ---------- 4) 编辑 → 不可变新版本 ----------

    def test_save_creates_next_immutable_version(self):
        created = self._create_draft("api_edit_alpha")
        response = self.client.put(
            f"/api/strategies/{created['id']}",
            json={"expected_version": 1, "change_note": "调整量能阈值",
                  "changes": {"description": "第二版"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["created_version"], 2)
        detail = self.client.get(f"/api/strategies/{created['id']}").json()
        self.assertEqual(detail["version"], 2)
        self.assertEqual(detail["definition"]["description"], "第二版")
        versions = self.client.get(f"/api/strategies/{created['id']}/versions").json()["items"]
        self.assertEqual([row["version"] for row in versions], [1, 2])

    def test_save_expected_version_conflict_409(self):
        created = self._create_draft("api_conflict_alpha")
        response = self.client.put(
            f"/api/strategies/{created['id']}",
            json={"expected_version": 9, "changes": {"description": "过期写"}},
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_save_non_versioned_field_400(self):
        created = self._create_draft("api_nonversioned")
        response = self.client.put(
            f"/api/strategies/{created['id']}",
            json={"changes": {"origin": "builtin"}},
        )
        self.assertEqual(response.status_code, 400, response.text)

    # ---------- 5) 验证 ----------

    def test_validate_valid_and_invalid(self):
        ok = self.client.post("/api/strategies/validate", json={"dsl_ast": RULE}).json()
        self.assertTrue(ok["valid"], ok)
        self.assertTrue(ok["checksum"])
        self.assertTrue(ok["normalized_ast"])
        bad = self.client.post("/api/strategies/validate", json={"dsl_ast": BAD_RULE})
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(bad.json()["valid"])
        self.assertTrue(bad.json()["errors"])
        missing = self.client.post("/api/strategies/validate", json={}).json()
        self.assertFalse(missing["valid"])

    # ---------- 6) 预览 ----------

    def test_preview_returns_unified_profiles(self):
        response = self.client.post(
            "/api/strategies/preview",
            json={"dsl_ast": RULE, "metadata": {"style": "trend", "hold": 8}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["valid"], body)
        self.assertIn("archetype", body["risk_fingerprint"])
        self.assertIn("risk_profile", body)
        self.assertIn("execution_profile", body)
        self.assertEqual(body["allocation"]["lifecycle_stage"], "pilot")
        self.assertEqual(body["allocation"]["capital_scale"], 0.25)

    # ---------- 7) 生命周期 ----------

    def test_full_lifecycle_to_active_pause_resume(self):
        created = self._create_draft("api_life_alpha")
        sid = created["id"]

        def move(to_status, expected_status):
            return self.client.post(
                f"/api/strategies/{sid}/transition",
                json={"to_status": to_status, "expected_status": expected_status,
                      "reason": "接口测试"},
            )

        self.assertEqual(move("validated", "draft").status_code, 200, "draft→validated")
        active = move("active", "validated")
        self.assertEqual(active.status_code, 200, active.text)
        self.assertTrue(active.json()["supports_new_cycle"], "active 后必须可进新周期")
        # 用户策略上线即试点：active 后 runtime 阶段为 pilot（内置才是 standard）。
        self.assertEqual(active.json()["runtime"]["lifecycle_stage"], "pilot")
        paused = move("paused", "active")
        self.assertEqual(paused.status_code, 200)
        self.assertFalse(paused.json()["supports_new_cycle"])
        resumed = move("active", "paused")
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertTrue(resumed.json()["supports_new_cycle"])

    def test_invalid_transition_409(self):
        created = self._create_draft("api_jump_alpha")
        response = self.client.post(
            f"/api/strategies/{created['id']}/transition", json={"to_status": "active"},
        )
        self.assertEqual(response.status_code, 409, response.text)

    # ---------- 8) Clone ----------

    def test_clone_returns_user_draft_v1(self):
        created = self._create_draft("api_clone_src")
        response = self.client.post(
            f"/api/strategies/{created['id']}/clone",
            json={"source_version": 1, "new_strategy_id": "api_clone_dst",
                  "name": "我的趋势策略副本"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["id"], "api_clone_dst")
        self.assertEqual(body["origin"], "user")
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["name"], "我的趋势策略副本")
        self.assertFalse(body["supports_new_cycle"])
        versions = self.client.get("/api/strategies/api_clone_dst/versions").json()["items"]
        self.assertEqual(versions[0]["cloned_from_strategy_id"], "api_clone_src")
        self.assertEqual(versions[0]["cloned_from_version"], 1)

    def test_clone_duplicate_target_409(self):
        created = self._create_draft("api_clone_dup")
        response = self.client.post(
            f"/api/strategies/{created['id']}/clone",
            json={"source_version": 1, "new_strategy_id": "api_clone_dup"},
        )
        self.assertEqual(response.status_code, 409, response.text)

    # ---------- 9) 版本与事件 ----------

    def test_versions_and_events_timeline(self):
        created = self._create_draft("api_hist_alpha")
        self.client.put(
            f"/api/strategies/{created['id']}/transition", json={},
        )  # 空 payload → 400，不影响历史
        self.client.post(
            f"/api/strategies/{created['id']}/transition",
            json={"to_status": "validated", "expected_status": "draft"},
        )
        versions = self.client.get(f"/api/strategies/{created['id']}/versions").json()
        self.assertEqual(len(versions["items"]), 1)
        self.assertEqual(versions["items"][0]["version"], 1)
        self.assertTrue(versions["items"][0]["checksum"])
        self.assertEqual(versions["items"][0]["change_note"], "initial user definition")
        events = self.client.get(f"/api/strategies/{created['id']}/events").json()["items"]
        transitions = [(row["from_status"], row["to_status"]) for row in events]
        self.assertIn((None, "draft"), transitions)
        self.assertIn(("draft", "validated"), transitions)

    # ---------- 10) 删除 ----------

    def test_unused_draft_can_be_deleted(self):
        self._create_draft("api_del_free")
        response = self.client.delete("/api/strategies/api_del_free")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["deleted"])
        self.assertEqual(self.client.get("/api/strategies/api_del_free").status_code, 404)

    def test_referenced_draft_cannot_be_deleted(self):
        self._create_draft("api_del_used")
        self._reference("api_del_used")
        response = self.client.delete("/api/strategies/api_del_used")
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertIn("归档", str(detail))

    def test_builtin_cannot_be_deleted(self):
        response = self.client.delete("/api/strategies/trend_pullback")
        self.assertEqual(response.status_code, 409, response.text)

    def test_active_strategy_cannot_be_deleted(self):
        created = self._create_draft("api_del_active")
        self.client.post(
            f"/api/strategies/{created['id']}/transition",
            json={"to_status": "validated", "expected_status": "draft"},
        )
        activated = self.client.post(
            f"/api/strategies/{created['id']}/transition",
            json={"to_status": "active", "expected_status": "validated"},
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        self.assertEqual(
            self.client.delete(f"/api/strategies/{created['id']}").status_code, 409,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
