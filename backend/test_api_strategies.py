# -*- coding: utf-8 -*-
"""PR-45：Strategy Admin API（``/api/strategies``）闭环与错误映射。

说明：直接调用路由函数并断言 ``HTTPException`` 的状态码，而不是起 HTTP
客户端——CI/容器镜像里的 starlette 需要 ``httpx2``，测试不该依赖额外的
网络栈包。路由是否真的挂到产品 app 上由 ``test_routes_registered_on_app``
单独锁住（检查 ``main.app.routes``）。

覆盖：创建 draft → 列表可见 → validate → transition validated →
transition active（supports_new_cycle=true）→ 编辑产生 v2 → clone →
pause → resume；以及错误语义：非法 id 400 / 重复 id 409 / 非法 DSL 422 /
版本冲突 409 / 非法状态迁移 409 / 已引用与内置策略不可删。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import types
import unittest
import unittest.mock

from fastapi import HTTPException

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

    @classmethod
    def tearDownClass(cls):
        SRT.clear_cache()
        P.DB_PATH = cls._old_db
        SR.DEFAULT_DB_PATH = cls._old_sr_db
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        SRT.clear_cache()

    # ---------- 辅助：调用路由函数并归一化成 (status, body) ----------

    @staticmethod
    def _call(func, *args, default_status: int = 200, **kwargs):
        try:
            return default_status, func(*args, **kwargs)
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}

    def _create(self, strategy_id: str, **overrides):
        payload = {
            "id": strategy_id,
            "name": f"{strategy_id} 名称",
            "description": "接口测试策略",
            "metadata": {"style": "trend", "hold": 8, "candidate_topn": 10},
            "dsl_ast": RULE,
        }
        payload.update(overrides)
        return self._call(API.create_strategy, payload, default_status=201)

    def _create_draft(self, strategy_id: str):
        status, body = self._create(strategy_id)
        self.assertEqual(status, 201, body)
        return body

    def _transition(self, strategy_id: str, to_status: str, expected_status=None):
        payload = {"to_status": to_status, "reason": "接口测试"}
        if expected_status is not None:
            payload["expected_status"] = expected_status
        return self._call(API.transition_strategy, strategy_id, payload)

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

    # ---------- 0) 路由注册 ----------

    def test_routes_registered_on_app(self):
        # 用 OpenAPI schema 校验接线：fastapi 0.141/starlette 1.6 的
        # app.routes 里有 _IncludedRouter 包装对象（无 path 属性），
        # 直接内省 app.routes 跨版本不可靠；openapi paths 才是稳定契约。
        openapi = main.app.openapi()
        paths = openapi.get("paths") or {}
        expected = {
            "/api/strategies": {"get", "post"},
            "/api/strategies/validate": {"post"},
            "/api/strategies/preview": {"post"},
            "/api/strategies/{strategy_id}": {"get", "put", "patch", "delete"},
            "/api/strategies/{strategy_id}/transition": {"post"},
            "/api/strategies/{strategy_id}/clone": {"post"},
            "/api/strategies/{strategy_id}/versions": {"get"},
            "/api/strategies/{strategy_id}/events": {"get"},
        }
        for path, methods in expected.items():
            self.assertIn(path, paths, path)
            for method in methods:
                self.assertIn(method, paths[path], (path, method))
        self.assertIn("/api/scanner-strategies", paths)

    def test_scanner_strategies_endpoint_is_separate(self):
        # 选股扫描页的静态策略列表不能和注册表共用 /api/strategies。
        body = main.strategies()
        ids = {item["id"] for item in body["strategies"]}
        self.assertTrue(ids)
        self.assertNotIn("trend_pullback", ids)
        paths = {getattr(route, "path", None) for route in main.app.routes}
        self.assertIn("/api/scanner-strategies", paths)

    # ---------- 1) 列表 ----------

    def test_list_returns_builtins_and_user_with_summary(self):
        self._create_draft("api_list_alpha")
        status, body = self._call(API.list_strategies)
        self.assertEqual(status, 200)
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
        status, body = self._call(API.list_strategies, origin="builtin")
        self.assertEqual(status, 200)
        self.assertTrue(body["items"])
        self.assertTrue(all(item["origin"] == "builtin" for item in body["items"]))

    def test_list_invalid_origin_is_rejected(self):
        status, _body = self._call(API.list_strategies, origin="martian")
        self.assertEqual(status, 400)

    # ---------- 2) 详情 ----------

    def test_detail_includes_definition_version_and_runtime(self):
        created = self._create_draft("api_detail_alpha")
        status, body = self._call(API.get_strategy, created["id"])
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], "api_detail_alpha")
        self.assertEqual(body["origin"], "user")
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["definition"]["dsl_ast"], RULE)
        self.assertTrue(body["runtime_ready"], body.get("runtime"))
        self.assertIn("risk_fingerprint", body["runtime"])
        self.assertIn("risk_profile", body["runtime"])
        self.assertIn("execution_profile", body["runtime"])
        # PR-26：draft 不部署资金（shadow），validated/active 才是 pilot。
        self.assertEqual(body["runtime"]["lifecycle_stage"], "shadow")
        status, events = self._call(API.list_strategy_events, created["id"])
        self.assertEqual(status, 200)
        self.assertTrue(events["items"])

    def test_detail_unknown_strategy_404(self):
        status, _body = self._call(API.get_strategy, "nope_missing")
        self.assertEqual(status, 404)

    def test_detail_runtime_failure_does_not_500(self):
        # RuntimeContext 构建失败（编译期异常）必须降级为 runtime_ready=false，
        # 而不是让整个详情页 500。只替换 API 模块内的运行时引用，
        # 避免影响 paper_trading 的 init_db 路径。
        created = self._create_draft("api_runtime_broken")

        def _boom(*_args, **_kwargs):
            raise ValueError("编译失败")

        fake_runtime = types.SimpleNamespace(get_context=_boom)
        with unittest.mock.patch.object(API, "SRT", fake_runtime):
            status, body = self._call(API.get_strategy, created["id"])
        self.assertEqual(status, 200, body)
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
        status, _body = self._create("BAD ID!")
        self.assertEqual(status, 400)

    def test_create_duplicate_id_409(self):
        self._create_draft("api_dup_alpha")
        status, _body = self._create("api_dup_alpha")
        self.assertEqual(status, 409)

    def test_create_builtin_conflict_409(self):
        status, _body = self._create("trend_pullback")
        self.assertEqual(status, 409)

    def test_create_invalid_dsl_422(self):
        status, _body = self._create("api_bad_dsl", dsl_ast=BAD_RULE)
        self.assertEqual(status, 422)

    # ---------- 4) 编辑 → 不可变新版本 ----------

    def test_save_creates_next_immutable_version(self):
        created = self._create_draft("api_edit_alpha")
        status, body = self._call(
            API.update_strategy, created["id"],
            {"expected_version": 1, "change_note": "调整量能阈值",
             "changes": {"description": "第二版"}},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["created_version"], 2)
        _status, detail = self._call(API.get_strategy, created["id"])
        self.assertEqual(detail["version"], 2)
        self.assertEqual(detail["definition"]["description"], "第二版")
        _status, versions = self._call(API.list_strategy_versions, created["id"])
        self.assertEqual([row["version"] for row in versions["items"]], [1, 2])

    def test_save_expected_version_conflict_409(self):
        created = self._create_draft("api_conflict_alpha")
        status, _body = self._call(
            API.update_strategy, created["id"],
            {"expected_version": 9, "changes": {"description": "过期写"}},
        )
        self.assertEqual(status, 409)

    def test_save_non_versioned_field_400(self):
        created = self._create_draft("api_nonversioned")
        status, _body = self._call(
            API.update_strategy, created["id"], {"changes": {"origin": "builtin"}},
        )
        self.assertEqual(status, 400)

    # ---------- 5) 验证 ----------

    def test_validate_valid_and_invalid(self):
        _status, ok = self._call(API.validate_strategy, {"dsl_ast": RULE})
        self.assertTrue(ok["valid"], ok)
        self.assertTrue(ok["checksum"])
        self.assertTrue(ok["normalized_ast"])
        _status, bad = self._call(API.validate_strategy, {"dsl_ast": BAD_RULE})
        self.assertFalse(bad["valid"])
        self.assertTrue(bad["errors"])
        _status, missing = self._call(API.validate_strategy, {})
        self.assertFalse(missing["valid"])

    # ---------- 6) 预览 ----------

    def test_preview_returns_unified_profiles(self):
        status, body = self._call(
            API.preview_strategy,
            {"dsl_ast": RULE, "metadata": {"style": "trend", "hold": 8}},
        )
        self.assertEqual(status, 200, body)
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
        status, _body = self._transition(sid, "validated", "draft")
        self.assertEqual(status, 200, "draft→validated")
        status, active = self._transition(sid, "active", "validated")
        self.assertEqual(status, 200, active)
        self.assertTrue(active["supports_new_cycle"], "active 后必须可进新周期")
        # 用户策略上线即试点：active 后 runtime 阶段为 pilot（内置才是 standard）。
        self.assertEqual(active["runtime"]["lifecycle_stage"], "pilot")
        status, paused = self._transition(sid, "paused", "active")
        self.assertEqual(status, 200, paused)
        self.assertFalse(paused["supports_new_cycle"])
        status, resumed = self._transition(sid, "active", "paused")
        self.assertEqual(status, 200, resumed)
        self.assertTrue(resumed["supports_new_cycle"])

    def test_invalid_transition_409(self):
        created = self._create_draft("api_jump_alpha")
        status, _body = self._transition(created["id"], "active")
        self.assertEqual(status, 409)

    def test_transition_requires_to_status(self):
        created = self._create_draft("api_no_status")
        status, _body = self._call(API.transition_strategy, created["id"], {})
        self.assertEqual(status, 400)

    # ---------- 8) Clone ----------

    def test_clone_returns_user_draft_v1(self):
        created = self._create_draft("api_clone_src")
        status, body = self._call(
            API.clone_strategy, created["id"],
            {"source_version": 1, "new_strategy_id": "api_clone_dst",
             "name": "我的趋势策略副本"},
            default_status=201,
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["id"], "api_clone_dst")
        self.assertEqual(body["origin"], "user")
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["name"], "我的趋势策略副本")
        self.assertFalse(body["supports_new_cycle"])
        _status, versions = self._call(API.list_strategy_versions, "api_clone_dst")
        self.assertEqual(versions["items"][0]["cloned_from_strategy_id"], "api_clone_src")
        self.assertEqual(versions["items"][0]["cloned_from_version"], 1)

    def test_clone_accepts_legacy_id_field(self):
        # 旧前端（策略注册表）传 {id: 新策略ID}，新契约用 new_strategy_id。
        created = self._create_draft("api_clone_legacy")
        status, body = self._call(
            API.clone_strategy, created["id"],
            {"source_version": 1, "id": "api_clone_legacy_dst"},
            default_status=201,
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["id"], "api_clone_legacy_dst")

    def test_transition_validated_requires_runtime_ready(self):
        # 无 DSL 的用户策略不能进 validated（与旧 /validate 端点语义一致）。
        status, created = self._create("api_no_dsl", dsl_ast=None)
        self.assertEqual(status, 201, created)
        self.assertEqual(created["status"], "draft")
        status, body = self._transition(created["id"], "validated", "draft")
        self.assertEqual(status, 409, body)
        self.assertIn("runtime is not ready", str(body["detail"]))

    def test_risk_expansion_cannot_bypass_gate_via_payload(self):
        # 非对称风险门不接受 HTTP 调用方自带的 risk_evidence/challenger_win：
        # 声明"Challenger 已胜出"也不能放大风险，必须 422 且不落库。
        expanding = {
            "op": "strategy",
            "rule": {
                "op": "gt", "left": {"op": "field", "name": "close"},
                "right": {"op": "indicator", "name": "ma", "window": 20},
            },
            "parameters": [
                {"op": "parameter", "parameter_id": "risk_per_trade", "type": "number",
                 "value": 0.02, "min": 0.002, "max": 0.02, "max_step": 0.002,
                 "locked": False, "risk_direction": "higher_is_riskier",
                 "min_evidence": 10},
            ],
        }
        base_rule = {
            "op": "strategy",
            "rule": {
                "op": "gt", "left": {"op": "field", "name": "close"},
                "right": {"op": "indicator", "name": "ma", "window": 20},
            },
            "parameters": [
                {"op": "parameter", "parameter_id": "risk_per_trade", "type": "number",
                 "value": 0.008, "min": 0.002, "max": 0.02, "max_step": 0.002,
                 "locked": False, "risk_direction": "higher_is_riskier",
                 "min_evidence": 10},
            ],
        }
        status, created = self._create("api_gate_bypass", dsl_ast=base_rule)
        self.assertEqual(status, 201, created)
        status, body = self._call(
            API.update_strategy, created["id"],
            {"expected_version": 1, "changes": {"dsl_ast": expanding},
             "risk_evidence": 999, "challenger_win": True},
        )
        self.assertEqual(status, 422, body)
        self.assertRegex(str(body["detail"]), "风险放大|单轮放大")
        _status, detail = self._call(API.get_strategy, created["id"])
        self.assertEqual(detail["version"], 1)

    def test_clone_duplicate_target_409(self):
        created = self._create_draft("api_clone_dup")
        status, _body = self._call(
            API.clone_strategy, created["id"],
            {"source_version": 1, "new_strategy_id": "api_clone_dup"},
            default_status=201,
        )
        self.assertEqual(status, 409)

    # ---------- 9) 版本与事件 ----------

    def test_versions_and_events_timeline(self):
        created = self._create_draft("api_hist_alpha")
        self._transition(created["id"], "validated", "draft")
        _status, versions = self._call(API.list_strategy_versions, created["id"])
        self.assertEqual(len(versions["items"]), 1)
        self.assertEqual(versions["items"][0]["version"], 1)
        self.assertTrue(versions["items"][0]["checksum"])
        self.assertEqual(versions["items"][0]["change_note"], "initial user definition")
        _status, events = self._call(API.list_strategy_events, created["id"])
        transitions = [(row["from_status"], row["to_status"]) for row in events["items"]]
        self.assertIn((None, "draft"), transitions)
        self.assertIn(("draft", "validated"), transitions)

    # ---------- 10) 删除 ----------

    def test_unused_draft_can_be_deleted(self):
        self._create_draft("api_del_free")
        status, body = self._call(API.delete_strategy, "api_del_free")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["deleted"])
        status, _body = self._call(API.get_strategy, "api_del_free")
        self.assertEqual(status, 404)

    def test_referenced_draft_cannot_be_deleted(self):
        self._create_draft("api_del_used")
        self._reference("api_del_used")
        status, body = self._call(API.delete_strategy, "api_del_used")
        self.assertEqual(status, 409, body)
        self.assertIn("归档", str(body["detail"]))

    def test_builtin_cannot_be_deleted(self):
        status, _body = self._call(API.delete_strategy, "trend_pullback")
        self.assertEqual(status, 409)

    def test_active_strategy_cannot_be_deleted(self):
        created = self._create_draft("api_del_active")
        self._transition(created["id"], "validated", "draft")
        status, body = self._transition(created["id"], "active", "validated")
        self.assertEqual(status, 200, body)
        status, _body = self._call(API.delete_strategy, created["id"])
        self.assertEqual(status, 409)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
