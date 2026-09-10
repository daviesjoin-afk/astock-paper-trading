# -*- coding: utf-8 -*-
"""PR-51：StrategyService 的用例级测试——**完全不经过 FastAPI**。

这一层锁三件事：

1. 用例能独立于 HTTP 跑通（create → list → get → edit → validate → preview →
   transition → clone → versions/events → delete）；
2. 失败一律是 domain exception（``strategy_service.Strategy*``），不再是裸
   ``ValueError``、也不依赖 HTTP 状态码；
3. 服务自己持有数据库连接/事务（``paper_trading._db`` 只在它内部出现），
   并且不认识 HTTP（源码里不出现 fastapi / HTTPException）。

风险放大唯一入口（PR-33）在本层的证据：``update_strategy`` 固定以
``risk_evidence=None, challenger_win=False`` 调用 Registry，放大必被拒绝。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
import unittest.mock

import paper_trading as P
import strategy_api_models as Models
import strategy_registry as SR
import strategy_runtime as SRT
import strategy_service as SVC

SERVICE_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_service.py")

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


def _risky_rule(risk_per_trade: float) -> dict:
    """带风险方向参数的可执行 DSL（用来触发非对称风险门）。"""
    return {
        "op": "strategy",
        "rule": {
            "op": "gt", "left": {"op": "field", "name": "close"},
            "right": {"op": "indicator", "name": "ma", "window": 20},
        },
        "parameters": [
            {"op": "parameter", "parameter_id": "risk_per_trade", "type": "number",
             "value": risk_per_trade, "min": 0.002, "max": 0.02, "max_step": 0.002,
             "locked": False, "risk_direction": "higher_is_riskier", "min_evidence": 10},
        ],
    }


class StrategyServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-strategy-service-")
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

    # ---------- 契约构造 ----------

    @staticmethod
    def _create_request(strategy_id: str, *, dsl_ast=RULE, **overrides):
        payload = {
            "id": strategy_id, "name": f"{strategy_id} 名称",
            "description": "服务层测试", "metadata": {"style": "trend", "hold": 8},
            "dsl_ast": dsl_ast,
        }
        payload.update(overrides)
        return Models.StrategyCreateRequest.model_validate(payload)

    def _draft(self, strategy_id: str, **overrides) -> dict:
        return SVC.create_strategy(self._create_request(strategy_id, **overrides))

    # ---------- 1) 用例闭环 ----------

    def test_full_use_case_chain_without_fastapi(self):
        created = self._draft("svc_alpha")
        self.assertEqual(created["id"], "svc_alpha")
        self.assertEqual(created["status"], "draft")
        self.assertEqual(created["version"], 1)
        # PR-26：draft 不部署资金（shadow）。
        self.assertEqual(created["runtime"]["lifecycle_stage"], "shadow")

        listed = SVC.list_strategies(origin="user")
        self.assertIn("svc_alpha", {spec.id for spec in listed})
        self.assertEqual(SVC.get_strategy("svc_alpha").status, "draft")

        updated = SVC.update_strategy("svc_alpha", Models.StrategyUpdateRequest.model_validate(
            {"expected_version": 1, "change_note": "第二版", "changes": {"description": "第二版"}},
        ))
        self.assertEqual(updated["created_version"], 2)
        self.assertEqual(updated["strategy"]["definition"]["description"], "第二版")
        self.assertEqual([row["version"] for row in SVC.list_versions("svc_alpha")], [1, 2])

        self.assertEqual(SVC.transition("svc_alpha", Models.StrategyTransitionRequest.model_validate(
            {"to_status": "validated", "expected_status": "draft"})).get("status"), "validated")
        active = SVC.transition("svc_alpha", Models.StrategyTransitionRequest.model_validate(
            {"to_status": "active", "expected_status": "validated"}))
        self.assertEqual(active["status"], "active")
        self.assertTrue(active["supports_new_cycle"])
        # 用户策略 active 仍停在 pilot（PR-26 验收条件）。
        self.assertEqual(active["runtime"]["lifecycle_stage"], "pilot")

        cloned = SVC.clone_strategy("svc_alpha", Models.StrategyCloneRequest.model_validate(
            {"source_version": 1, "new_strategy_id": "svc_clone"}))
        self.assertEqual((cloned["id"], cloned["status"], cloned["version"]), ("svc_clone", "draft", 1))

        events = SVC.list_events("svc_alpha")
        transitions = [(row["from_status"], row["to_status"]) for row in events]
        self.assertIn((None, "draft"), transitions)
        self.assertIn(("draft", "validated"), transitions)

        self.assertTrue(SVC.delete_unused_draft("svc_clone")["deleted"])
        with self.assertRaises(SVC.StrategyNotFound):
            SVC.get_strategy("svc_clone")

    def test_validate_and_preview_are_pure_reads(self):
        ok = SVC.validate_definition(RULE, {"style": "trend", "hold": 8})
        self.assertTrue(ok["valid"], ok)
        self.assertTrue(ok["checksum"])
        self.assertTrue(ok["normalized_ast"])

        bad = SVC.validate_definition(BAD_RULE, None)
        self.assertFalse(bad["valid"])
        self.assertTrue(bad["errors"])

        missing = SVC.validate_definition(None, None)
        self.assertFalse(missing["valid"])
        self.assertIn("dsl_ast is required", missing["errors"])

        non_object_metadata = SVC.validate_definition(RULE, ["not", "an", "object"])
        self.assertFalse(non_object_metadata["valid"])
        self.assertIn("metadata must be an object", non_object_metadata["errors"])

        preview = SVC.preview_strategy(Models.StrategyPreviewRequest.model_validate(
            {"dsl_ast": RULE, "metadata": {"style": "trend", "hold": 8}},
        ))
        self.assertTrue(preview["valid"], preview)
        self.assertIn("archetype", preview["risk_fingerprint"])
        # 用户新策略一律 pilot 起步（0.25 资金系数）。
        self.assertEqual(preview["allocation"]["lifecycle_stage"], "pilot")
        self.assertEqual(preview["allocation"]["capital_scale"], 0.25)

    # ---------- 2) 失败一律是 domain exception ----------

    def test_duplicate_id_raises_conflict(self):
        self._draft("svc_dup")
        with self.assertRaises(SVC.StrategyConflict):
            self._draft("svc_dup")

    def test_builtin_id_raises_conflict(self):
        with self.assertRaises(SVC.StrategyConflict):
            self._draft("trend_pullback")

    def test_unknown_id_raises_not_found(self):
        for call in (
            lambda: SVC.get_strategy("svc_missing"),
            lambda: SVC.detail("svc_missing"),
            lambda: SVC.list_versions("svc_missing"),
            lambda: SVC.list_events("svc_missing"),
            lambda: SVC.delete_unused_draft("svc_missing"),
            lambda: SVC.update_strategy("svc_missing", Models.StrategyUpdateRequest.model_validate(
                {"changes": {"description": "x"}})),
        ):
            with self.assertRaises(SVC.StrategyNotFound):
                call()

    def test_invalid_id_and_missing_fields_raise_invalid_definition(self):
        with self.assertRaises(SVC.InvalidStrategyDefinition):
            SVC.create_strategy(Models.StrategyCreateRequest.model_validate(
                {"id": "BAD ID!", "name": "x"}))
        with self.assertRaises(SVC.InvalidStrategyDefinition):
            SVC.update_strategy("svc_alpha2", Models.StrategyUpdateRequest.model_validate({}))
        with self.assertRaises(SVC.InvalidStrategyDefinition):
            SVC.transition("svc_alpha2", Models.StrategyTransitionRequest.model_validate(
                {"to_status": ""}))

    def test_invalid_dsl_raises_dedicated_type(self):
        with self.assertRaises(SVC.InvalidStrategyDsl):
            self._draft("svc_bad_dsl", dsl_ast=BAD_RULE)
        # 不落库：定义与版本都不存在。
        with self.assertRaises(SVC.StrategyNotFound):
            SVC.get_strategy("svc_bad_dsl")

    def test_version_and_lifecycle_conflicts_raise(self):
        self._draft("svc_conflict")
        with self.assertRaises(SVC.StrategyVersionConflict):
            SVC.update_strategy("svc_conflict", Models.StrategyUpdateRequest.model_validate(
                {"expected_version": 9, "changes": {"description": "过期写"}}))
        # draft 不能直接跳 active。
        with self.assertRaises(SVC.InvalidLifecycleTransition):
            SVC.transition("svc_conflict", Models.StrategyTransitionRequest.model_validate(
                {"to_status": "active"}))

    def test_runtime_not_ready_raises_before_validated(self):
        self._draft("svc_no_dsl", dsl_ast=None)
        with self.assertRaises(SVC.StrategyRuntimeNotReady) as ctx:
            SVC.transition("svc_no_dsl", Models.StrategyTransitionRequest.model_validate(
                {"to_status": "validated", "expected_status": "draft"}))
        self.assertIn("runtime is not ready", str(ctx.exception))

    def test_historical_reference_raises_dedicated_type(self):
        self._draft("svc_referenced")
        conn = sqlite3.connect(P.DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            version = SR.get_version("svc_referenced", conn=conn)
            conn.execute(
                "INSERT INTO paper_audit(account_id,event,detail,created_at,strategy_id,"
                " strategy_version,strategy_checksum) VALUES(?,'unit-test','{}',?,?,?,?)",
                ("svc_referenced", "2026-09-10T00:00:00+00:00", "svc_referenced",
                 version.version, version.checksum),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(SVC.StrategyHistoricalReferenceError):
            SVC.delete_unused_draft("svc_referenced")

    def test_risk_expansion_is_rejected_by_the_gate(self):
        self._draft("svc_gate", dsl_ast=_risky_rule(0.008))
        seen = {}
        real = SR.save_definition

        def spy(conn, strategy_id, changes, **kwargs):
            seen.update(kwargs)
            return real(conn, strategy_id, changes, **kwargs)

        with unittest.mock.patch.object(SVC.SR, "save_definition", side_effect=spy):
            with self.assertRaises(SVC.StrategyRiskExpansionRejected):
                SVC.update_strategy("svc_gate", Models.StrategyUpdateRequest.model_validate(
                    {"expected_version": 1, "changes": {"dsl_ast": _risky_rule(0.02)}}))
        # 服务固定按 fail-closed 申报证据：调用方无从注入。
        self.assertIsNone(seen.get("risk_evidence"))
        self.assertFalse(seen.get("challenger_win"))
        # 未落库：版本仍是 1。
        self.assertEqual(SVC.get_strategy("svc_gate").current_version, 1)

    # ---------- 3) 结构性约束 ----------

    def test_service_is_http_agnostic(self):
        with open(SERVICE_SOURCE, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("fastapi", "HTTPException", "@router", "APIRouter"):
            self.assertNotIn(forbidden, source, f"StrategyService 不得依赖 {forbidden}")

    def test_service_owns_database_access(self):
        with open(SERVICE_SOURCE, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("P._db(", source)

    def test_translate_maps_registry_value_errors(self):
        cases = {
            "unknown strategy id": SVC.StrategyNotFound,
            "strategy id already exists": SVC.StrategyConflict,
            "built-in strategy id is reserved": SVC.StrategyConflict,
            "strategy version changed: expected v2, found v1": SVC.StrategyVersionConflict,
            "invalid lifecycle transition: draft -> active": SVC.InvalidLifecycleTransition,
            "strategy status changed concurrently": SVC.InvalidLifecycleTransition,
            "strategy has historical references and must be archived": SVC.StrategyHistoricalReferenceError,
            "only unused user drafts can be hard-deleted": SVC.StrategyHistoricalReferenceError,
            "strategy runtime is not ready: no dsl": SVC.StrategyRuntimeNotReady,
            "strategy id must match ^[a-z][a-z0-9_]{2,63}$": SVC.InvalidStrategyDefinition,
            "某种从未见过的失败": SVC.InvalidStrategyDefinition,
        }
        for message, expected in cases.items():
            self.assertIsInstance(
                SVC.translate_registry_error(ValueError(message)), expected, message,
            )
        self.assertIsInstance(
            SVC.translate_registry_error(ValueError("风险放大：单轮放大超过上限")),
            SVC.StrategyRiskExpansionRejected,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
