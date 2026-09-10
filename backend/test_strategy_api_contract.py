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

        # 工作台「标记 Validated」只传 to_status + reason。
        status, validated = self._call(API.transition_strategy, "fe_life", {
            "to_status": "validated", "reason": "Web workbench 验证通过",
        })
        self.assertEqual(status, 200, validated)
        # 其他动作带 expected_status 做乐观并发（validated -> archived 是合法边）。
        status, archived = self._call(API.transition_strategy, "fe_life", {
            "to_status": "archived", "expected_status": "validated",
            "reason": "Web workbench 操作", "actor": "strategy-workbench",
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


class RequestSchemaTests(_ApiFixture):
    """缺必填字段 400、形状错误 422、伪造风控字段被丢弃。"""

    def test_missing_required_field_is_400(self):
        status, body = self._call(API.create_strategy, {})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["detail"], "id is required")

        status, body = self._call(API.transition_strategy, "whatever", {})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["detail"], "to_status is required")

        status, body = self._call(API.clone_strategy, "whatever", {})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["detail"], "new_strategy_id is required")

    def test_wrong_shape_is_422(self):
        status, _body = self._call(API.create_strategy, {"id": "shape", "name": "x", "metadata": "not-an-object"})
        self.assertEqual(status, 422)
        status, _body = self._call(API.transition_strategy, "shape", {"to_status": {"nested": 1}})
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
        self.assertIn("to_status", components["StrategyTransitionRequest"].get("required", []))

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
            "/api/strategies/{strategy_id}/clone": {"post"},
            "/api/strategies/{strategy_id}/versions": {"get"},
            "/api/strategies/{strategy_id}/events": {"get"},
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
        self.assertEqual("to_status is required", json.loads(missing.body)["detail"])

        wrong_shape = response_for(
            "/api/strategies/x/transition", Models.StrategyTransitionRequest,
            {"to_status": {"nested": 1}},
        )
        self.assertEqual(422, wrong_shape.status_code)

        # 其它路由保持 FastAPI 原生行为（422 + 结构化 detail）。
        other = response_for("/api/paper/anything", Models.StrategyTransitionRequest, {})
        self.assertEqual(422, other.status_code)
        self.assertIsInstance(json.loads(other.body)["detail"], list)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
