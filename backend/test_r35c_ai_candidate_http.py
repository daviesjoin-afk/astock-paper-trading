# -*- coding: utf-8 -*-
"""R35-C HTTP 契约：``POST /api/strategies/{id}/candidate-generations/ai``。

route 只做 typed 请求、provider 配置解析、调用 orchestration service 与错误映射。
本模块证明：

* 请求体**没有** generator / constraints / provenance / 凭据字段（AI 路径不开放）
* provider 配置来自既有槽位 authority，客户端无法提交任何 secret
* 拒绝映射稳定：不存在 404 / 不自洽 409 / 越界 400 / 上游失败 502
* provider 失败零写入（不留下半批次）
* 成功响应只发布台账事实，不含任何 evaluation / score / promotion 字段
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ai_research_contract as ARC
import ai_research_repository as ARR
import api_strategies as API
import market_data_contract as MDC
import paper_trading as P
import strategy_ai_provider as SAIPR
import strategy_api_models as Models
import strategy_candidate_service as SCV
import strategy_registry as SR
import strategy_runtime as SRT
import strategy_service as SVC
from fastapi import HTTPException

DAY = "2026-10-05"
UNIVERSE = {"scope_kind": "a_share_all"}

_RULE = {
    "op": "strategy",
    "rule": {"op": "and", "args": [
        {"op": "gt", "left": {"op": "field", "name": "close"},
         "right": {"op": "indicator", "name": "ma", "window": {
             "op": "parameter", "parameter_id": "ma_period", "type": "integer",
             "value": 20, "min": 5, "max": 60, "max_step": 2, "locked": False,
             "risk_direction": "lower_is_riskier", "min_evidence": 0}}},
        {"op": "lt", "left": {"op": "indicator", "name": "rsi", "window": 14},
         "right": {"op": "const", "value": 70}}]},
    "parameters": [],
}


class _Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-r35c-http-")
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
        # 配置一个**就绪**的槽位：provider 配置复用既有 ai_review_service authority，
        # 且 readiness（凭据 / enabled / 地址 / 模型）必须在网络之前成立。
        import ai_review_service as AIReview
        with SCV.paper_connection() as conn:
            AIReview.update_slot(conn, "ai1", api_key="sk-test-slot",
                                 enabled=True, base_url="https://ai1.example.com/v1",
                                 model="test-model")
        self.calls = []
        self._original = SAIPR.transport.call_json
        self.addCleanup(lambda: setattr(SAIPR.transport, "call_json", self._original))
        SAIPR.transport.call_json = self._fake

    def _fake(self, provider_config, system_prompt, user_prompt, max_tokens=1800):
        self.calls.append({"provider_config": provider_config, "user_prompt": user_prompt})
        return ({"parameter_variants": {"ma_period": [18, 20, 22]}}, 30, 15, 4)

    @staticmethod
    def _call(func, *args, default_status=200, **kwargs):
        try:
            return default_status, func(*args, **kwargs)
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}

    def _parent(self, strategy_id):
        status, created = self._call(API.create_strategy, {
            "id": strategy_id, "name": strategy_id, "dsl_ast": _RULE,
            "metadata": {"constraints": {"max_positions": 5}},
        }, default_status=201)
        self.assertEqual(201, status, created)
        return SVC.lifecycle_read_model(strategy_id)

    def _research_run(self):
        ref = ARC.evidence_ref_from_market_reading(
            MDC.classify(
                MDC.symbol_quote_snapshot(
                    {"code": "600000", "price": 10.5, "quote_at": f"{DAY}T10:30:00+08:00",
                     "quote_source": "eastmoney",
                     "quote_validation": "cross_source_checked"}, asof_day=DAY),
                MDC.policy_named("live_market"),
                now=f"{DAY}T10:30:00+08:00", asof_day=DAY))
        hypothesis = ARC.ResearchHypothesis(
            hypothesis_id="http_hyp", as_of=DAY, subject="http_parent",
            thesis="momentum", evidence=(
                ARC.HypothesisEvidence(ref=ref, relation=ARC.RELATION_SUPPORTS),),
            confidence=0.5)
        with SCV.paper_connection() as conn:
            # 与 ``ai_research_service`` 的写路径一致：append 前自己保证 schema。
            ARR.ensure_schema(conn)
            return ARR.append_run(
                conn, hypothesis=hypothesis, purpose="candidate_generation",
                trigger="manual", narrative="n", provider_slot="ai1",
                provider_model="research-model",
                created_at=f"{DAY}T07:00:00+00:00")

    def _counts(self):
        with SCV.paper_connection() as conn:
            return {name: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for name, table in (
                        ("c", "strategy_candidates"),
                        ("p", "strategy_candidate_proposals"),
                        ("b", "strategy_candidate_generation_batches"))}


class AIEndpointTests(_Fixture):
    def test_ai_generation_over_http(self):
        lifecycle = self._parent("ai_http")
        run_id = self._research_run()
        status, result = self._call(API.generate_ai_strategy_candidates, "ai_http", {
            "strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"],
            "research_run_id": str(run_id),
            "asof": DAY,
            "universe_spec": UNIVERSE,
            "intended_market_regime": "momentum",
        }, default_status=201)
        self.assertEqual(201, status, result)
        self.assertEqual(3, result["candidate_count"])
        self.assertEqual(1, len(self.calls))

        # 响应只发布台账事实：没有任何 evaluation / score / promotion 词汇。
        blob = json.dumps(result).lower()
        for forbidden in ("sharpe", "score", "rank", "winner", "sharpe_ratio",
                          "promotion", "expected_return", "deploy"):
            self.assertNotIn(forbidden, blob)
        # candidate 投影是纯内容：没有 generator / provenance。
        for key in ("generator_type", "hypothesis_id", "research_provenance",
                    "random_seed", "model_identity"):
            self.assertNotIn(key, result["candidates"][0])

        # batch 可完整审计：能追回 exact research run 与 canonical search space。
        status, batch = self._call(API.get_candidate_generation_batch, "ai_http",
                                   result["generation_batch_id"])
        self.assertEqual(200, status, batch)
        row = batch["generation_batch"]
        self.assertTrue(SCV.verify_batch_search_space_material(row))
        self.assertEqual(run_id,
                         row["search_space_material"]["extra"]["research_run_id"])

    def test_request_schema_forbids_ai_power_fields(self):
        """请求契约里**不存在** generator / constraints / provenance / 凭据字段。"""
        fields = set(Models.StrategyAICandidateGenerateRequest.model_fields)
        for forbidden in ("generator_type", "generator_version", "constraints",
                          "research_provenance", "model_identity", "hypothesis_id",
                          "evidence_count", "api_key", "base_url", "authorization"):
            self.assertNotIn(forbidden, fields)
        self.assertTrue({"strategy_version", "strategy_checksum", "research_run_id",
                         "asof", "max_candidates"} <= fields)

    def test_client_cannot_smuggle_secrets_or_generator(self):
        """多余字段被接受也不会生效（``extra=ignore``），且绝不进入 provider 配置。"""
        lifecycle = self._parent("ai_smuggle")
        run_id = self._research_run()
        status, result = self._call(API.generate_ai_strategy_candidates, "ai_smuggle", {
            "strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"],
            "research_run_id": str(run_id),
            "asof": DAY,
            "universe_spec": UNIVERSE,
            "intended_market_regime": "momentum",
            # 客户端试图越权的一切：
            "api_key": "sk-client-supplied",
            "base_url": "https://evil.example.com/v1",
            "generator_type": "bayesian_search",
            "constraints": {"max_positions": 999},
        }, default_status=201)
        self.assertEqual(201, status, result)
        # 客户端提交的 secret 绝不能出现在 provider 配置或 prompt 里。
        config = self.calls[0]["provider_config"]
        self.assertNotIn("sk-client-supplied", json.dumps(config))
        self.assertNotIn("evil.example.com", json.dumps(config))
        self.assertNotIn("bayesian_search", json.dumps(result))
        # 越权的 constraints 被契约丢弃：候选仍继承 exact pinned parent 的约束。
        for candidate in result["candidates"]:
            self.assertEqual({"max_positions": 5}, candidate["constraints"])

    def test_unknown_run_is_404_and_never_calls_the_provider(self):
        lifecycle = self._parent("ai_404")
        before = self._counts()
        status, body = self._call(API.generate_ai_strategy_candidates, "ai_404", {
            "strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"],
            "research_run_id": "999999",
            "asof": DAY,
            "universe_spec": UNIVERSE,
            "intended_market_regime": "momentum",
        })
        self.assertEqual(404, status, body)
        self.assertEqual([], self.calls)
        self.assertEqual(before, self._counts())

    def test_asof_mismatch_is_409_before_the_provider(self):
        lifecycle = self._parent("ai_asof")
        run_id = self._research_run()
        before = self._counts()
        status, body = self._call(API.generate_ai_strategy_candidates, "ai_asof", {
            "strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"],
            "research_run_id": str(run_id),
            "asof": "2026-10-06",
            "universe_spec": UNIVERSE,
            "intended_market_regime": "momentum",
        })
        self.assertEqual(409, status, body)
        self.assertEqual([], self.calls)
        self.assertEqual(before, self._counts())

    def test_provider_protocol_failure_is_502_with_zero_writes(self):
        lifecycle = self._parent("ai_502")
        run_id = self._research_run()
        before = self._counts()
        SAIPR.transport.call_json = lambda *a, **k: ({"score": 1}, 1, 1, 1)
        status, body = self._call(API.generate_ai_strategy_candidates, "ai_502", {
            "strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"],
            "research_run_id": str(run_id),
            "asof": DAY,
            "universe_spec": UNIVERSE,
            "intended_market_regime": "momentum",
        })
        self.assertEqual(502, status, body)
        self.assertEqual(before, self._counts())

    def test_cap_above_the_ai_ceiling_is_rejected_without_writes(self):
        lifecycle = self._parent("ai_cap")
        run_id = self._research_run()
        before = self._counts()
        status, body = self._call(API.generate_ai_strategy_candidates, "ai_cap", {
            "strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"],
            "research_run_id": str(run_id),
            "asof": DAY,
            "universe_spec": UNIVERSE,
            "intended_market_regime": "momentum",
            "max_candidates": 64,
        })
        self.assertEqual(400, status, body)
        self.assertEqual([], self.calls)
        self.assertEqual(before, self._counts())

    def test_unknown_provider_slot_is_a_client_error(self):
        """未知槽位是客户端错误，不是 5xx，也不能静默回落到别的槽位。"""
        lifecycle = self._parent("ai_slot")
        run_id = self._research_run()
        before = self._counts()
        status, body = self._call(API.generate_ai_strategy_candidates, "ai_slot", {
            "strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"],
            "research_run_id": str(run_id),
            "provider_slot": "evil-slot",
        })
        self.assertEqual(400, status, body)
        self.assertEqual([], self.calls)
        self.assertEqual(before, self._counts())

    def test_disabled_provider_slot_is_rejected_before_the_provider(self):
        """被禁用的槽位必须拦在**网络之前**。

        ``ai_provider_transport.call_json`` 只检查 api_key / base_url / model，**不看**
        ``enabled``；因此"操作员禁用了该槽位"必须由本层拦下，否则禁用只挡住 UI，挡不住
        真实付费调用。这与 R27-B2B 修过的缺陷是同一个形状。
        """
        import ai_review_service as AIReview
        lifecycle = self._parent("ai_disabled")
        run_id = self._research_run()
        with SCV.paper_connection() as conn:
            AIReview.update_slot(conn, "ai1", api_key="sk-present", enabled=False,
                                 base_url="https://ai1.example.com/v1", model="m")
        before = self._counts()
        self.calls.clear()
        status, body = self._call(API.generate_ai_strategy_candidates, "ai_disabled", {
            "strategy_version": lifecycle["version"],
            "strategy_checksum": lifecycle["checksum"],
            "research_run_id": str(run_id),
            "provider_slot": "ai1",
        })
        self.assertEqual(409, status, body)
        self.assertEqual([], self.calls, "被禁用的槽位不得发起任何 provider 调用")
        self.assertEqual(before, self._counts())
        """route 里不得出现 research gate / parent 查找 / prompt / AST / DB 写入。"""
        import ast
        import inspect
        source = inspect.getsource(API.generate_ai_strategy_candidates)
        tree = ast.parse(source)
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                calls.add(node.attr)
            elif isinstance(node, ast.Name):
                calls.add(node.id)
        for forbidden in ("require_supported", "get_run", "recent_runs",
                          "pin_parent_strategy", "build_search_space",
                          "generate_candidates", "build_strategy_candidate",
                          "execute", "commit"):
            self.assertNotIn(forbidden, calls)
        self.assertIn("generate_candidates_from_research", calls)


if __name__ == "__main__":
    unittest.main()
