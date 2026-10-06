# -*- coding: utf-8 -*-
"""PR-51：Strategy Admin 的 HTTP 契约（类型化请求 / 错误映射 / 分层）。

锁住四类东西：

1. **前端 contract 不破坏**——用前端源码（PR-55 起为 ``frontend/src/**``）里真实发出的 payload
   形状回放：Workbench 与旧策略构建器两条路径的 create / patch、transition、
   clone（新契约 ``new_strategy_id`` 与旧 ``id``）、validate、preview、list、
   delete。
2. **请求 schema**——缺必填字段 400、形状/类型错误 422，且带伪造的风控证据
   字段（``risk_evidence`` / ``challenger_win``）被契约丢弃。
3. **OpenAPI**——每个接口都展示出对应的 request schema（typed contract 的
   可见性）。
4. **分层**——api_strategies 不再持有 DB 访问与 Registry 引用；真实 HTTP 的
   校验错误走与直接函数调用相同的状态码映射。
"""
from __future__ import annotations

import asyncio
import ast
import json
import os
import sqlite3
import shutil
import tempfile
import types
import unittest
import unittest.mock

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

import api_strategies as API
import main
import paper_trading as P
import strategy_api_models as Models
import strategy_lifecycle as SL
import strategy_registry as SR
import strategy_runtime as SRT
import strategy_service as SVC

API_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_strategies.py")

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
BAD_RULE = {"op": "const", "value": 1}


def _code_without_docstrings(path: str) -> str:
    """模块源码去掉 docstring——契约检查针对**代码**，不针对说明文字。"""
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body.pop(0)
    return ast.unparse(tree)


class _ApiFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-strategy-api-contract-")
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

    @staticmethod
    def _call(func, *args, default_status: int = 200, **kwargs):
        try:
            return default_status, func(*args, **kwargs)
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}


class FrontendPayloadContractTests(_ApiFixture):
    """回放前端源码真实发出的请求体。"""

    def test_workbench_create_and_patch_payload(self):
        draft = {
            "id": "fe_wb_alpha", "name": "前端工作台策略",
            "description": "前端回放", "metadata": {"style": "trend", "candidate_topn": 10, "hold": 8},
            "dsl_ast": RULE,
        }
        payload = {"actor": "strategy-workbench"}
        payload.update(draft)
        status, created = self._call(API.create_strategy, payload, default_status=201)
        self.assertEqual(status, 201, created)
        self.assertEqual(created["id"], "fe_wb_alpha")

        status, saved = self._call(API.patch_strategy, "fe_wb_alpha", {
            "changes": {"name": draft["name"], "description": "第二版",
                        "metadata": draft["metadata"], "dsl_ast": draft["dsl_ast"]},
            "expected_version": 1,
            "change_note": "Web editor update",
            "actor": "strategy-workbench",
        })
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["created_version"], 2)

    def test_legacy_builder_create_and_patch_payload(self):
        # 旧策略构建器（sb*）：create 带 description；patch 只给 changes，
        # 且 changes.name 可能因 JS undefined 而缺键。
        status, created = self._call(API.create_strategy, {
            "id": "fe_sb_alpha", "name": "fe_sb_alpha",
            "description": "用户自建策略（策略构建器）",
            "dsl_ast": RULE, "metadata": {"style": "trend", "hold": 8}, "actor": "human-ui",
        }, default_status=201)
        self.assertEqual(status, 201, created)

        status, saved = self._call(API.patch_strategy, "fe_sb_alpha", {
            "changes": {"dsl_ast": RULE, "metadata": {"style": "trend", "hold": 8}},
            "expected_version": 1, "actor": "human-ui", "change_note": "strategy builder edit",
        })
        self.assertEqual(status, 200, saved)

    def test_transition_clone_validate_preview_list_delete_payloads(self):
        status, created = self._call(API.create_strategy, {
            "id": "fe_life", "name": "fe_life", "dsl_ast": RULE,
            "metadata": {"style": "trend", "hold": 8}, "actor": "strategy-workbench",
        }, default_status=201)
        self.assertEqual(status, 201, created)

        # 安全迁移必须绑定当前 immutable version/state，并说明原因。
        lifecycle = SVC.lifecycle_read_model("fe_life")
        status, archived = self._call(API.transition_strategy, "fe_life", {
            "strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"],
            "expected_state": lifecycle["state"], "target_state": "archived",
            "actor_type": "human", "actor_id": "strategy-workbench",
            "reason_code": "test_archive", "reason": "Web workbench 操作",
        })
        self.assertEqual(status, 200, archived)

        # clone 走旧字段 id。
        status, cloned = self._call(API.clone_strategy, "fe_life", {
            "id": "fe_life_copy", "actor": "strategy-workbench",
        }, default_status=201)
        self.assertEqual(status, 201, cloned)
        self.assertEqual(cloned["id"], "fe_life_copy")

        status, ok = self._call(API.validate_strategy, {
            "dsl_ast": RULE, "metadata": {"style": "trend", "hold": 8},
        })
        self.assertEqual(status, 200, ok)
        self.assertTrue(ok["valid"])

        status, preview = self._call(API.preview_strategy, {
            "id": "fe_life", "name": "fe_life", "description": "",
            "metadata": {"style": "trend", "hold": 8}, "dsl_ast": RULE,
        })
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["allocation"]["lifecycle_stage"], "pilot")

        status, listing = self._call(API.list_strategies, include_archived=True)
        self.assertEqual(status, 200)
        self.assertIn("fe_life", {item["id"] for item in listing["items"]})
        self.assertIn("summary", listing)

        status, versions = self._call(API.list_strategy_versions, "fe_life")
        self.assertEqual(status, 200)
        self.assertEqual([row["version"] for row in versions["items"]], [1])
        status, events = self._call(API.list_strategy_events, "fe_life")
        self.assertEqual(status, 200)
        self.assertTrue(events["items"])

        status, deleted = self._call(API.delete_strategy, "fe_life_copy")
        self.assertEqual(status, 200, deleted)
        self.assertTrue(deleted["deleted"])
        self.assertIsNone(deleted["archived_instead_hint"])

    def test_r35a_candidate_generation_and_read_model_over_http(self):
        """R35-A：候选生成走真实 HTTP 契约，读模型只发布 backend 事实。"""
        parameterized = {
            "op": "strategy",
            "rule": {"op": "gt", "left": {"op": "field", "name": "close"},
                     "right": {"op": "indicator", "name": "ma", "window": {
                         "op": "parameter", "parameter_id": "ma_period", "type": "integer",
                         "value": 20, "min": 5, "max": 60, "max_step": 2, "locked": False,
                         "risk_direction": "lower_is_riskier", "min_evidence": 0}}},
            "parameters": [],
        }
        status, created = self._call(API.create_strategy, {
            "id": "fe_cand", "name": "fe_cand", "dsl_ast": parameterized,
        }, default_status=201)
        self.assertEqual(status, 201, created)
        lifecycle = SVC.lifecycle_read_model("fe_cand")

        payload = {
            "strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"],
            "asof": "2026-10-05",
            "parameter_adjustments": {"ma_period": [18, 22]},
            "universe_spec": {"scope_kind": "a_share_all"},
            "intended_market_regime": "momentum",
            "evidence_count": 0,
            "research_provenance": {"source_kind": "human"},
        }
        status, result = self._call(API.generate_strategy_candidates, "fe_cand", payload,
                                    default_status=201)
        self.assertEqual(status, 201, result)
        self.assertEqual(2, result["candidate_count"])
        self.assertEqual(lifecycle["checksum"],
                         result["parent_strategy_pin"]["strategy_checksum"])

        candidate_id = result["candidate_ids"][0]
        status, read = self._call(API.get_strategy_candidate, "fe_cand", candidate_id)
        self.assertEqual(status, 200, read)
        self.assertEqual(candidate_id, read["candidate"]["candidate_id"])
        self.assertEqual("CANDIDATE", read["status"])
        # 生成路径不发布评估 / 晋级结论。
        self.assertIsNone(read["evaluation"])
        self.assertIsNone(read["promotion"])
        for forbidden in ("sharpe", "max_drawdown", "win_rate", "promotion_result"):
            self.assertNotIn(forbidden, json.dumps(read).lower())

        # 列表必须按 exact pin 过滤；用另一个 checksum 查不到任何候选。
        status, listed = self._call(API.list_strategy_candidates, "fe_cand",
                                    strategy_version=lifecycle["version"],
                                    strategy_checksum=lifecycle["checksum"])
        self.assertEqual(status, 200, listed)
        self.assertEqual(2, len(listed["items"]))
        self.assertIn("created_at", listed["items"][0]["persistence"])
        self.assertEqual(candidate_id, listed["items"][0]["candidate"]["candidate_id"])
        # R35-B：列表项额外发布**提案事件**摘要（generator 能力 / batch），
        # 因为候选行本身不再携带能力身份。
        self.assertEqual("parameter_variant",
                         listed["items"][0]["proposal"]["generator_type"])
        self.assertEqual(64, len(listed["items"][0]["proposal"]["generation_batch_id"]))
        # 列表按显式 batch id 发布 batch 摘要（绝不查"最新一批"）。
        self.assertEqual(1, len(listed["generation_batches"]))
        self.assertEqual(listed["items"][0]["proposal"]["generation_batch_id"],
                         listed["generation_batches"][0]["batch_id"])
        self.assertEqual(lifecycle["checksum"],
                         listed["generation_batches"][0]["parent_strategy_checksum"])
        status, other = self._call(API.list_strategy_candidates, "fe_cand",
                                   strategy_version=lifecycle["version"],
                                   strategy_checksum="b" * 64)
        self.assertEqual(200, status)
        self.assertEqual([], other["items"])

        # 页面身份与候选身份不一致 → 409，绝不把 A 的候选显示成 B 的。
        status, mismatch = self._call(API.get_strategy_candidate, "fe_life", candidate_id)
        self.assertEqual(409, status, mismatch)

        # 缺失 provenance / 越权参数一律被拒（形状错误 422，契约拒绝 400/409），
        # 且不产生任何候选行。
        for broken in (
            {**payload, "strategy_checksum": ""},
            {**payload, "parameter_adjustments": {"ma_period": [40]}},
            {**payload, "universe_spec": None},
        ):
            status, rejected = self._call(API.generate_strategy_candidates, "fe_cand", broken)
            self.assertIn(status, (400, 409, 422), rejected)
        status, still = self._call(API.list_strategy_candidates, "fe_cand",
                                   strategy_version=lifecycle["version"],
                                   strategy_checksum=lifecycle["checksum"])
        self.assertEqual(2, len(still["items"]), "被拒绝的请求不得留下候选")

    def test_r35b_search_space_generation_over_http(self):
        """R35-B：显式 search space 走真实 HTTP 契约；batch 只发布台账事实。"""
        factor_a = {"op": "gt", "left": {"op": "field", "name": "pe"},
                    "right": {"op": "const", "value": 30}}
        factor_b = {"op": "lt", "left": {"op": "field", "name": "pb"},
                    "right": {"op": "const", "value": 3}}
        status, created = self._call(API.create_strategy, {
            "id": "fe_expand", "name": "fe_expand", "dsl_ast": RULE,
            "metadata": {"constraints": {"max_positions": 5, "max_exposure_pct": 0.6}},
        }, default_status=201)
        self.assertEqual(status, 201, created)
        lifecycle = SVC.lifecycle_read_model("fe_expand")
        base = {
            "strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"],
            "asof": "2026-10-05",
            "universe_spec": {"scope_kind": "a_share_all"},
            "intended_market_regime": "momentum",
            "evidence_count": 0,
            "research_provenance": {"source_kind": "human"},
        }

        # factor_variant：显式有限备选集 → 每个备选一个候选。
        status, expanded = self._call(API.generate_strategy_candidates, "fe_expand", {
            **base, "generator_type": "factor_variant", "generator_version": "v1",
            "factor_slot": {"kind": "explicit_variant", "alternatives": [factor_a, factor_b]},
        }, default_status=201)
        self.assertEqual(status, 201, expanded)
        self.assertEqual(2, expanded["candidate_count"])
        self.assertEqual("factor_variant", expanded["generator_type"])
        self.assertEqual(64, len(expanded["generation_batch_id"]))
        self.assertEqual(64, len(expanded["search_space_fingerprint"]))

        # batch 只发布台账事实：exact pin + search-space + 每条 proposal 事件。
        status, batch = self._call(API.get_candidate_generation_batch, "fe_expand",
                                   expanded["generation_batch_id"])
        self.assertEqual(status, 200, batch)
        record = batch["generation_batch"]
        self.assertEqual(lifecycle["checksum"], record["parent_strategy_checksum"])
        self.assertEqual(expanded["search_space_fingerprint"],
                         record["search_space_fingerprint"])
        self.assertEqual(2, record["candidate_count"])
        self.assertEqual(2, len(batch["proposals"]))
        self.assertEqual({expanded["generation_batch_id"]},
                         {item["generation_batch_id"] for item in batch["proposals"]})
        # 页面身份与 batch 身份必须一致。
        status, mismatch = self._call(API.get_candidate_generation_batch, "fe_cand",
                                      expanded["generation_batch_id"])
        self.assertEqual(409, status, mismatch)

        # 组合超限必须 fail closed，且不留下任何候选/批次行。
        factors = [{"op": "gt", "left": {"op": "field", "name": "pe"},
                    "right": {"op": "const", "value": value}}
                   for value in range(1, 201)]
        status, rejected = self._call(API.generate_strategy_candidates, "fe_expand", {
            **base, "generator_type": "bounded_combination",
            "generator_version": "v1", "factor_slot": {
                "kind": "explicit_variant", "alternatives": factors},
        })
        self.assertIn(status, (400, 409, 422), rejected)
        # 放宽 constraints 是风险放大动作，不属于生成域权限（父策略声明了边界）。
        status, widened = self._call(API.generate_strategy_candidates, "fe_expand", {
            **base, "constraints": {"max_positions": 99, "max_exposure_pct": 0.6}})
        self.assertIn(status, (400, 409, 422), widened)
        status, still = self._call(API.list_strategy_candidates, "fe_expand",
                                   strategy_version=lifecycle["version"],
                                   strategy_checksum=lifecycle["checksum"])
        self.assertEqual(2, len(still["items"]), "被拒绝的请求不得留下候选")

    def test_workbench_can_resume_paused_strategy_with_human_reason(self):
        status, created = self._call(API.create_strategy, {
            "id": "fe_resume", "name": "fe_resume", "dsl_ast": RULE,
        }, default_status=201)
        self.assertEqual(status, 201, created)
        conn = sqlite3.connect(P.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DROP TRIGGER strategy_lifecycle_events_no_delete")
            conn.execute("DELETE FROM strategy_lifecycle_events WHERE strategy_id='fe_resume'")
            conn.execute("DELETE FROM strategy_lifecycle_state WHERE strategy_id='fe_resume'")
            conn.execute("UPDATE strategy_definitions SET lifecycle_status='active' WHERE id='fe_resume'")
            SL.ensure_schema(conn)
            conn.commit()
        finally:
            conn.close()

        lifecycle = SVC.lifecycle_read_model("fe_resume")
        self.assertEqual("paper", lifecycle["state"])
        self.assertIn("paused", lifecycle["safety_transitions"])
        status, paused = self._call(API.transition_strategy, "fe_resume", {
            "strategy_version": lifecycle["version"], "strategy_checksum": lifecycle["checksum"],
            "expected_state": "paper", "target_state": "paused", "actor_type": "human",
            "actor_id": "strategy-workbench", "reason_code": "operator_pause",
            "reason": "人工临时暂停",
        })
        self.assertEqual(status, 200, paused)

        lifecycle = SVC.lifecycle_read_model("fe_resume")
        self.assertEqual("paused", lifecycle["state"])
        self.assertIn("paper", lifecycle["safety_transitions"])
        status, resumed = self._call(API.transition_strategy, "fe_resume", {
            "strategy_version": lifecycle["version"], "strategy_checksum": lifecycle["checksum"],
            "expected_state": "paused", "target_state": "paper", "actor_type": "human",
            "actor_id": "strategy-workbench", "reason_code": "operator_resume",
            "reason": "人工复核后恢复",
        })
        self.assertEqual(status, 200, resumed)
        self.assertEqual("paper", resumed["transitioned_version"]["state"])


class RequestSchemaTests(_ApiFixture):
    """缺必填字段 400、形状错误 422、伪造风控字段被丢弃。"""

    def test_missing_required_field_is_400(self):
        status, body = self._call(API.create_strategy, {})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["detail"], "id is required")

        status, body = self._call(API.transition_strategy, "whatever", {})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["detail"], "strategy_version is required")

        status, body = self._call(API.clone_strategy, "whatever", {})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["detail"], "new_strategy_id is required")

    def test_wrong_shape_is_422(self):
        status, _body = self._call(API.create_strategy, {"id": "shape", "name": "x", "metadata": "not-an-object"})
        self.assertEqual(status, 422)
        status, _body = self._call(API.transition_strategy, "shape", {
            "strategy_version": "wrong", "strategy_checksum": "0" * 64,
            "expected_state": "draft", "target_state": "candidate",
            "actor_type": "human", "actor_id": "test",
        })
        self.assertEqual(status, 422)

    def test_update_requires_a_non_empty_changes_object(self):
        status, created = self._call(API.create_strategy, {
            "id": "schema_changes", "name": "schema_changes", "dsl_ast": RULE,
        }, default_status=201)
        self.assertEqual(status, 201, created)
        status, body = self._call(API.update_strategy, "schema_changes", {"changes": {}})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["detail"], "changes must be a non-empty object")

    def test_forged_risk_evidence_is_dropped_by_the_contract(self):
        parsed = Models.StrategyUpdateRequest.model_validate({
            "changes": {"description": "x"},
            "risk_evidence": 999, "challenger_win": True,
            "promotion_approval": True, "evolution_evidence": {"samples": 10 ** 6},
        })
        dumped = parsed.model_dump()
        for forged in ("risk_evidence", "challenger_win", "promotion_approval", "evolution_evidence"):
            self.assertNotIn(forged, dumped)

    def test_forged_evidence_never_reaches_save_definition(self):
        status, created = self._call(API.create_strategy, {
            "id": "schema_forge", "name": "schema_forge", "dsl_ast": RULE,
        }, default_status=201)
        self.assertEqual(status, 201, created)
        seen = {}
        real = SR.save_definition

        def spy(conn, strategy_id, changes, **kwargs):
            seen.update(kwargs)
            return real(conn, strategy_id, changes, **kwargs)

        with unittest.mock.patch.object(SVC.SR, "save_definition", side_effect=spy):
            status, body = self._call(API.update_strategy, "schema_forge", {
                "expected_version": 1, "changes": {"description": "只改描述"},
                "risk_evidence": 999, "challenger_win": True, "promotion_approval": True,
            })
        self.assertEqual(status, 200, body)
        self.assertIsNone(seen.get("risk_evidence"))
        self.assertFalse(seen.get("challenger_win"))

    def test_invalid_dsl_is_422_and_is_not_persisted(self):
        from strategy_service import StrategyNotFound
        status, _body = self._call(API.create_strategy, {
            "id": "schema_bad_dsl", "name": "schema_bad_dsl", "dsl_ast": BAD_RULE,
        })
        self.assertEqual(status, 422)
        with self.assertRaises(StrategyNotFound):
            SVC.get_strategy("schema_bad_dsl")


class OpenApiContractTests(_ApiFixture):
    def test_request_schemas_are_published(self):
        schema = main.app.openapi()
        components = schema.get("components", {}).get("schemas", {})

        expected = {
            ("/api/strategies", "post"): "StrategyCreateRequest",
            ("/api/strategies/{strategy_id}", "put"): "StrategyUpdateRequest",
            ("/api/strategies/{strategy_id}", "patch"): "StrategyUpdateRequest",
            ("/api/strategies/validate", "post"): "StrategyValidateRequest",
            ("/api/strategies/preview", "post"): "StrategyPreviewRequest",
            ("/api/strategies/{strategy_id}/transition", "post"): "StrategyTransitionRequest",
            ("/api/strategies/{strategy_id}/clone", "post"): "StrategyCloneRequest",
        }
        for (path, method), model in expected.items():
            body = schema["paths"][path][method].get("requestBody")
            self.assertIsNotNone(body, (path, method))
            self.assertIn(model, json.dumps(body), (path, method))
            self.assertIn(model, components, model)

        # 必填字段必须真的标成 required（否则契约没说清调用方要给什么）。
        create = components["StrategyCreateRequest"]
        self.assertIn("id", create.get("required", []))
        self.assertIn("name", create.get("required", []))
        props = create["properties"]
        for field in ("id", "name", "description", "metadata", "dsl_ast", "actor"):
            self.assertIn(field, props, field)
        for required in ("strategy_version", "strategy_checksum", "expected_state",
                         "target_state", "actor_type", "actor_id"):
            self.assertIn(required, components["StrategyTransitionRequest"].get("required", []))

    def test_response_models_are_published(self):
        schema = main.app.openapi()
        components = schema.get("components", {}).get("schemas", {})
        self.assertIn("StrategyValidateResponse", components)
        self.assertIn("StrategyDeleteResponse", components)

    def test_routes_still_registered(self):
        paths = main.app.openapi().get("paths") or {}
        expected = {
            "/api/strategies": {"get", "post"},
            "/api/strategies/validate": {"post"},
            "/api/strategies/preview": {"post"},
            "/api/strategies/{strategy_id}": {"get", "put", "patch", "delete"},
            "/api/strategies/{strategy_id}/transition": {"post"},
            "/api/strategies/{strategy_id}/lifecycle": {"get"},
            "/api/strategies/{strategy_id}/promotion/proposals": {"get", "post"},
            "/api/strategies/{strategy_id}/clone": {"post"},
            "/api/strategies/{strategy_id}/versions": {"get"},
            "/api/strategies/{strategy_id}/events": {"get"},
            # R35-A：候选生成与只读投影（没有 latest/current 路由）。
            "/api/strategies/{strategy_id}/candidates": {"get", "post"},
            "/api/strategies/{strategy_id}/candidates/{candidate_id}": {"get"},
        }
        for path, methods in expected.items():
            self.assertIn(path, paths, path)
            for method in methods:
                self.assertIn(method, paths[path], (path, method))


class LayeringTests(_ApiFixture):
    """HTTP 层不再持有 DB 访问与 Registry 依赖。"""

    def test_api_module_has_no_domain_dependencies(self):
        for attr in ("P", "SR", "SRT", "DSL"):
            self.assertFalse(hasattr(API, attr), attr)
        source = _code_without_docstrings(API_SOURCE)
        self.assertNotIn("P._db(", source)
        self.assertNotIn("import paper_trading", source)
        self.assertNotIn("import strategy_registry", source)
        # 不允许再用 Registry 错误文案猜状态码。
        for token in ("already exists", "version changed", "historical references",
                      "runtime is not ready", "is reserved"):
            self.assertNotIn(token, source, token)

    def test_service_owns_db_and_domain_calls(self):
        for attr in ("P", "SR", "SRT", "DSL"):
            self.assertTrue(hasattr(SVC, attr), attr)

    def test_registry_valueerror_is_translated_once(self):
        # 路由层只认 domain exception：直接抛 ValueError 不会被吞成 500，
        # 而是由服务层统一翻译（这里验证映射表本身）。
        self.assertIsInstance(
            SVC.translate_registry_error(ValueError("strategy id already exists")),
            SVC.StrategyConflict,
        )
        self.assertEqual(409, API.status_for_error(SVC.StrategyConflict("x")))
        self.assertEqual(404, API.status_for_error(SVC.StrategyNotFound("x")))
        self.assertEqual(422, API.status_for_error(SVC.InvalidStrategyDsl("x")))
        self.assertEqual(422, API.status_for_error(SVC.StrategyRiskExpansionRejected("x")))
        self.assertEqual(409, API.status_for_error(SVC.StrategyHistoricalReferenceError("x")))
        self.assertEqual(400, API.status_for_error(SVC.InvalidStrategyDefinition("x")))

    def test_http_validation_handler_matches_direct_call_semantics(self):
        handler = main.app.exception_handlers.get(RequestValidationError)
        self.assertIsNotNone(handler, "缺少 /api/strategies 的校验错误映射")

        def response_for(path, model, payload):
            try:
                model.model_validate(payload)
            except ValidationError as exc:
                error = RequestValidationError(exc.errors())
            request = types.SimpleNamespace(url=types.SimpleNamespace(path=path))
            return asyncio.run(handler(request, error))

        missing = response_for(
            "/api/strategies/x/transition", Models.StrategyTransitionRequest, {},
        )
        self.assertEqual(400, missing.status_code)
        self.assertEqual("strategy_version is required", json.loads(missing.body)["detail"])

        wrong_shape = response_for(
            "/api/strategies/x/transition", Models.StrategyTransitionRequest,
            {"strategy_version": "wrong", "strategy_checksum": "0" * 64,
             "expected_state": "draft", "target_state": "candidate",
             "actor_type": "human", "actor_id": "test"},
        )
        self.assertEqual(422, wrong_shape.status_code)

        # 其它路由保持 FastAPI 原生行为（422 + 结构化 detail）。
        other = response_for("/api/paper/anything", Models.StrategyTransitionRequest, {})
        self.assertEqual(422, other.status_code)
        self.assertIsInstance(json.loads(other.body)["detail"], list)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
