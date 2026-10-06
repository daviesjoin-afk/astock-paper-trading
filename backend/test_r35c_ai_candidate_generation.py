# -*- coding: utf-8 -*-
"""R35-C —— AI Hypothesis → Constrained Candidate Generation 契约测试（C1–C18）。

覆盖的不变量：

* exact research run only，无 latest fallback
* unsupported / insufficient research 在网络调用**之前**被拒
* confidence 不参与资格判定
* research as_of 与 candidate generation asof 必须一致
* AI 不能选 parent / universe / regime / constraints / evaluation
* provider 严格 schema：未知字段与权威字段一律 fail closed
* DSL / parameter 合法性继续由既有 owner 裁决
* AI cap ≤ 32，超限 fail closed（不截断）
* no-op proposal 被拒
* 跨 model 同语义 proposal 仍然 candidate dedup
* research provenance 精确绑定 exact run + `record_hash`
* 网络调用不占 SQLite 写事务
* provider 失败零写入；写失败整批回滚
* generation batch 持久化可自验的 canonical search space
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ai_research_contract as ARC
import ai_research_repository as ARR
import market_data_contract as MDC
import paper_schema_migrations as PSM
import strategy_ai_candidate_service as SAICS
import strategy_ai_proposal as SAIP
import strategy_ai_provider as SAIPR
import strategy_candidate_repository as SCRepo
import strategy_candidate_service as SCV
import strategy_registry as SR

DAY = "2026-10-05"
PARENT = "ai_parent"
UNIVERSE = {"scope_kind": "a_share_all"}
REGIME = "momentum"

#: 父策略 DSL：一个可调参数（ma_period）+ 一个 rsi 常量，足以表达"参数变体"。
_PARENT_RULE = {
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


def _market_snapshot(code="600000"):
    return MDC.symbol_quote_snapshot(
        {"code": code, "price": 10.5, "quote_at": f"{DAY}T10:30:00+08:00",
         "quote_source": "eastmoney", "quote_validation": "cross_source_checked"},
        asof_day=DAY,
    )


def _supported_hypothesis(*, hypothesis_id="hyp_1", confidence=0.4, evidence_count=1):
    """一条真实走 R24 → R27 路径的 supported hypothesis。

    ``evidence_count > 1`` 时用不同的标的代码，以便产出**不同**的 evidence ref ——
    同一条证据重复出现会被 R27 的去重规则合并，那样测的就不是"证据量"了。
    """
    evidence = []
    for index in range(evidence_count):
        ref = ARC.evidence_ref_from_market_reading(
            MDC.classify(_market_snapshot(code=f"60000{index}"),
                         MDC.policy_named("live_market"),
                         now=f"{DAY}T10:30:00+08:00", asof_day=DAY),
        )
        evidence.append(ARC.HypothesisEvidence(ref=ref, relation=ARC.RELATION_SUPPORTS))
    return ARC.ResearchHypothesis(
        hypothesis_id=hypothesis_id, as_of=DAY, subject=PARENT,
        thesis="momentum 延续", evidence=tuple(evidence), confidence=confidence,
    )


def _plain_hypothesis(*, hypothesis_id="hyp_empty"):
    """没有证据的假设 —— ``insufficient_evidence`` / ``unsupported``。"""
    return ARC.ResearchHypothesis(
        hypothesis_id=hypothesis_id, as_of=DAY, subject=PARENT,
        thesis="无证据", evidence=(), confidence=0.99,
    )


class _FakeTransport:
    """Fake provider transport：记录调用、可切换响应/失败模式。

    所有 unit / mutation 都必须走 fake —— 绝不访问真实 provider。
    """

    def __init__(self, response=None, *, payload=None, error=None, calls=None):
        self.payload = payload if payload is not None else (
            response if response is not None else {"parameter_variants": {"ma_period": [18, 20]}})
        self.error = error
        self.calls = calls if calls is not None else []

    def __call__(self, provider_config, system_prompt, user_prompt, max_tokens=1800):
        self.calls.append({
            "provider_config": provider_config,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "max_tokens": max_tokens,
        })
        if self.error is not None:
            raise self.error
        return (self.payload, 120, 60, 11)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "paper.sqlite3")
        conn = self._open()
        PSM.ensure_strategy_candidates(conn)
        ARR.ensure_schema(conn)
        created = SR.create_user_definition(
            conn, PARENT, "AI Parent", dsl_ast=_PARENT_RULE,
            metadata={"constraints": {"max_positions": 5}}, actor="test")
        self.parent_version = created.current_version
        self.parent_checksum = created.current_checksum
        conn.close()
        self.calls = []
        self._transport = _FakeTransport(calls=self.calls)
        self._patch_transport()

    def _open(self, *, immediate=False):
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        return conn

    @contextlib.contextmanager
    def _connection(self, *, immediate=False):
        conn = self._open(immediate=immediate)
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _patch_transport(self):
        self._original = SAIPR.transport.call_json
        SAIPR.transport.call_json = self._transport
        self.addCleanup(self._restore_transport)

    def _restore_transport(self):
        SAIPR.transport.call_json = self._original

    def _reader(self):
        return self._connection()

    @staticmethod
    def _slot(slot="ai1", model="candidate-model", **overrides):
        """一个**就绪**的 provider 配置（readiness 现为 service 强制 gate）。"""
        config = {"slot": slot, "api_key": "sk-test-slot",
                  "base_url": "https://ai1.example.com/v1", "model": model,
                  "enabled": True}
        config.update(overrides)
        return config

    def _writer(self):
        return self._connection(immediate=True)

    def record_run(self, hypothesis):
        with self._connection() as conn:
            return ARR.append_run(
                conn, hypothesis=hypothesis, purpose="candidate_generation",
                trigger="manual", narrative="n", provider_slot="slot_a",
                provider_model="research-model",
                created_at=f"{DAY}T07:00:00+00:00")

    def generate(self, **overrides):
        call = {
            "research_reader": self._reader,
            "writer": self._writer,
            # 默认是一个**就绪**槽位：readiness 现在是 service 的强制 gate，未就绪的
            # 配置会被 provider_slot_not_ready 拒绝（见 ProviderReadinessTests）。
            "provider_config": {"slot": "ai1", "api_key": "sk-test-slot",
                                "base_url": "https://ai1.example.com/v1",
                                "model": "candidate-model", "enabled": True},
            "strategy_id": PARENT,
            "strategy_version": self.parent_version,
            "strategy_checksum": self.parent_checksum,
            "asof": DAY,
            "universe_spec": UNIVERSE,
            "intended_market_regime": REGIME,
        }
        call.update(overrides)
        return SAICS.generate_candidates_from_research(**call)

    # ── 计数 helper：provider / batch / proposal / candidate 四个 delta ──

    def counts(self):
        with self._connection() as conn:
            rows = {
                name: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for name, table in (
                    ("candidates", "strategy_candidates"),
                    ("proposals", "strategy_candidate_proposals"),
                    ("batches", "strategy_candidate_generation_batches"),
                )
            }
            rows["research"] = conn.execute(
                f"SELECT COUNT(*) FROM {ARR.TABLE}").fetchone()[0]
            return rows


class ExactResearchRunTests(_Base):
    """C1 — exact research run only。"""

    def test_c1_exact_run_id_is_read_and_unknown_id_fails_closed(self):
        run_id = self.record_run(_supported_hypothesis())
        result = self.generate(research_run_id=str(run_id))
        self.assertEqual(2, result["candidate_count"])
        self.assertEqual(1, len(self.calls))

        # 不存在的 id：fail closed，且**不**调用 provider。
        before = self.counts()
        self.calls.clear()
        with self.assertRaises(SAICS.AICandidateGenerationError) as ctx:
            self.generate(research_run_id=str(run_id + 1000))
        self.assertEqual(SAICS.REASON_RESEARCH_NOT_FOUND, ctx.exception.reason)
        self.assertEqual([], self.calls)
        self.assertEqual(before, self.counts())

    def test_c1b_non_numeric_or_negative_run_id_fails_closed(self):
        """任何"帮调用方猜一个 run"的输入都拒绝 —— 那正是隐式 latest 的入口。

        构造一个**真实存在**的 run，然后用它的 id 的"近似形态"（前导/尾随空白、前导零）
        请求。这样断言的就是"解析器拒绝了形状"，而不是"恰好查无此行"——后者会让回归
        变成假绿：``" 7"`` 被偷偷解析成 7，只要 7 不存在就"通过"。
        """
        run_id = self.record_run(_supported_hypothesis())
        canonical = str(run_id)
        for bad in (f" {canonical}", f"{canonical} ", f"0{canonical}",
                    f"+{canonical}", f"-{canonical}", f"{canonical}.0",
                    "latest", "*", "", "  ", "7x"):
            with self.subTest(bad=bad):
                self.calls.clear()
                before = self.counts()
                with self.assertRaises(SAICS.AICandidateGenerationError) as ctx:
                    self.generate(research_run_id=bad)
                self.assertEqual(SAICS.REASON_RESEARCH_NOT_FOUND, ctx.exception.reason)
                # 形状非法：既没查账本，也没花钱，更没写任何行。
                self.assertEqual([], self.calls)
                self.assertEqual(before, self.counts())

    def test_c1b2_canonical_run_id_and_ints_are_accepted(self):
        """合法形态：规范十进制字符串与正整数 int（含同一行的两种形态）。"""
        run_id = self.record_run(_supported_hypothesis())
        self.assertEqual(run_id, SAICS._research_run_identity(str(run_id)))
        self.assertEqual(run_id, SAICS._research_run_identity(run_id))
        # bool 是 int 的子类：True 绝不能当成 run 1。
        for bad in (True, False, 0, -1, 1.5, None, []):
            with self.subTest(bad=bad):
                with self.assertRaises(SAICS.AICandidateGenerationError):
                    SAICS._research_run_identity(bad)

    def test_c1b3_strict_identity_is_checked_before_reading_the_ledger(self):
        """形状拒绝必须发生在读账本之前（用 monkeypatch 证明账本从未被读）。"""
        run_id = self.record_run(_supported_hypothesis())
        self.calls.clear()
        original = ARR.get_run
        reads = []

        def _spy(conn, key):
            reads.append(key)
            return original(conn, key)

        ARR.get_run = _spy
        self.addCleanup(setattr, ARR, "get_run", original)
        with self.assertRaises(SAICS.AICandidateGenerationError):
            self.generate(research_run_id=f" {run_id}")
        self.assertEqual([], reads, "形状非法时不得读账本")
        self.assertEqual([], self.calls)

    def test_c1c_no_latest_fallback_in_the_source(self):
        """服务层不得**调用**任何"取最近研究"的读取（文档里提到它是允许的）。"""
        import ast
        tree = ast.parse(open(SAICS.__file__, encoding="utf-8").read())
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                called.add(node.attr)
            elif isinstance(node, ast.Name):
                called.add(node.id)
        for forbidden in ("recent_runs", "latest_run", "last_run",
                          "current_hypothesis", "list_runs"):
            self.assertNotIn(forbidden, called)


class ResearchGateTests(_Base):
    """C2/C3 — 资格 gate 在网络调用之前，且 confidence 不参与。"""

    def test_c2_unsupported_research_is_rejected_before_the_provider(self):
        run_id = self.record_run(_plain_hypothesis())
        before = self.counts()
        self.calls.clear()
        with self.assertRaises(SAICS.AICandidateGenerationError) as ctx:
            self.generate(research_run_id=str(run_id))
        self.assertEqual(SAICS.REASON_RESEARCH_UNSUPPORTED, ctx.exception.reason)
        # 关键：坏输入**没有**触发一次付费调用。
        self.assertEqual([], self.calls)
        self.assertEqual(before, self.counts())

    def test_c2b_not_found_unsupported_and_corrupt_all_fail_closed(self):
        run_id = self.record_run(_supported_hypothesis())
        with self.assertRaises(SAICS.AICandidateGenerationError):
            self.generate(research_run_id="99999")
        with self.assertRaises(SAICS.AICandidateGenerationError):
            self.generate(research_run_id=str(run_id), asof="2026-10-06")
        self.assertEqual([], self.calls)

    def test_c3_confidence_has_no_authority(self):
        """confidence 0.1 与 0.9 的资格逻辑必须完全相同。

        R27 已明确 confidence 是 AI 自评而非证据；R35-C 不得把它偷偷升级成阈值。
        """
        low = self.record_run(_supported_hypothesis(hypothesis_id="h_low", confidence=0.1))
        high = self.record_run(_supported_hypothesis(hypothesis_id="h_high", confidence=0.9))
        result_low = self.generate(research_run_id=str(low))
        result_high = self.generate(research_run_id=str(high))
        self.assertEqual(result_low["candidate_count"], result_high["candidate_count"])
        self.assertEqual(2, len(self.calls))
        # 两条 run 的资格结论一致：都产出候选，都没被 confidence 影响。
        self.assertEqual(2, result_low["candidate_count"])
        self.assertEqual(2, result_high["candidate_count"])

    def test_c3b_no_confidence_threshold_in_the_source(self):
        """资格判定代码里不得出现 confidence 阈值（文档说明不算）。"""
        import ast
        tree = ast.parse(open(SAICS.__file__, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                # confidence > 0.7 / confidence >= x 这类阈值比较。
                for operand in [node.left, *node.comparators]:
                    if isinstance(operand, ast.Attribute) and operand.attr == "confidence":
                        self.fail(f"confidence threshold found at line {node.lineno}")
                    if isinstance(operand, ast.Constant) and isinstance(operand.value, float):
                        self.fail(f"float threshold found at line {node.lineno}")
            if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                    and node.slice.value == "confidence"):
                self.fail(f"confidence read at line {node.lineno}")


class ProviderReadinessTests(_Base):
    """P1：readiness 必须由 **orchestration service 自己**强制，不能只靠 HTTP route。

    这条很重要：``generate_candidates_from_research()`` 的 authority 就是"完整编排边界"，
    它可以被 CLI / R36 / scheduler / 其他内部调用**直接**调用。而
    ``ai_provider_transport.call_json`` **刻意**只检查 api_key / base_url / model、
    **不认识** ``enabled`` —— 所以只把 readiness 放在 route，等于"H有人绕过 route 时禁用
    形同不存在"。gate 必须长在 service 上，规则仍复用
    ``ai_review_service.slot_readiness``（不复制第二套语义）。
    """

    def _config(self, **overrides):
        call = {"slot": "ai1", "api_key": "sk-present",
                "base_url": "https://ai1.example.com/v1", "model": "m", "enabled": True}
        call.update(overrides)
        return call

    def test_disabled_slot_is_blocked_by_the_service_itself(self):
        run_id = self.record_run(_supported_hypothesis())
        before = self.counts()
        self.calls.clear()
        with self.assertRaises(SAICS.AICandidateGenerationError) as ctx:
            self.generate(research_run_id=str(run_id),
                          provider_config=self._config(enabled=False))
        # 稳定 reason 来自 canonical readiness。
        self.assertEqual(SAICS.REASON_PROVIDER_NOT_READY, ctx.exception.reason)
        self.assertEqual("disabled", ctx.exception.detail)
        # provider 零调用、台账零写入。
        self.assertEqual([], self.calls, "被禁用的槽位不得发起任何 provider 调用")
        self.assertEqual(before, self.counts())

    def test_unready_slots_are_blocked_with_canonical_reasons(self):
        run_id = self.record_run(_supported_hypothesis())
        before = self.counts()
        cases = (
            ({"api_key": ""}, "not_configured"),
            ({"base_url": ""}, "unusable_base_url"),
            ({"model": ""}, "model_missing"),
            ({"enabled": False}, "disabled"),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.calls.clear()
                with self.assertRaises(SAICS.AICandidateGenerationError) as ctx:
                    self.generate(research_run_id=str(run_id),
                                  provider_config=self._config(**overrides))
                self.assertEqual(SAICS.REASON_PROVIDER_NOT_READY, ctx.exception.reason)
                self.assertEqual(expected, ctx.exception.detail)
                self.assertEqual([], self.calls)
        self.assertEqual(before, self.counts())

    def test_readiness_reuses_the_canonical_authority_not_a_second_copy(self):
        """不得另写一套 readiness 规则：判定必须与 ai_review_service 完全一致。"""
        import ai_review_service as AIReview
        source = open(SAICS.__file__, encoding="utf-8").read()
        self.assertIn("AIReview.slot_readiness(", source)
        for forbidden in ("def slot_readiness", "def _slot_readiness",
                          "def _provider_ready"):
            self.assertNotIn(forbidden, source)
        # 逐个槽位状态与 canonical 判定一致。
        for config in (self._config(enabled=False), self._config(api_key=""),
                       self._config(model=""), self._config()):
            canonical = AIReview.slot_readiness(config)
            if canonical["ready"]:
                SAICS._require_provider_ready(config)   # 不抛
            else:
                with self.assertRaises(SAICS.AICandidateGenerationError) as ctx:
                    SAICS._require_provider_ready(config)
                self.assertEqual(canonical["reason"], ctx.exception.detail)

    def test_readiness_precedes_the_provider_call_but_follows_the_audit_gates(self):
        """顺序：审计 gate（research / parent）→ readiness → 网络。

        坏审计输入不先花钱，未就绪槽位也绝不被真正调用；两者都满足"拒绝排在付费之前"。
        """
        run_id = self.record_run(_supported_hypothesis())
        self.calls.clear()
        # 坏 as-of + 未就绪槽位：审计 gate 先拒（不暴露槽位状态）。
        with self.assertRaises(SAICS.AICandidateGenerationError) as ctx:
            self.generate(research_run_id=str(run_id), asof="2026-10-06",
                          provider_config=self._config(enabled=False))
        self.assertEqual(SAICS.REASON_ASOF_MISMATCH, ctx.exception.reason)
        self.assertEqual([], self.calls)


class AsofPinningTests(_Base):
    """C4 — research as_of 与 candidate asof 必须钉死。"""

    def test_c4_asof_mismatch_is_rejected_before_the_provider(self):
        run_id = self.record_run(_supported_hypothesis())
        before = self.counts()
        self.calls.clear()
        with self.assertRaises(SAICS.AICandidateGenerationError) as ctx:
            self.generate(research_run_id=str(run_id), asof="2026-10-06")
        self.assertEqual(SAICS.REASON_ASOF_MISMATCH, ctx.exception.reason)
        self.assertEqual([], self.calls)
        self.assertEqual(before, self.counts())

    def test_c4b_batch_keeps_the_research_business_day(self):
        run_id = self.record_run(_supported_hypothesis())
        result = self.generate(research_run_id=str(run_id))
        self.assertEqual(DAY, result["asof"])

    def test_c4c_omitting_asof_adopts_the_research_run_day(self):
        """省略 asof **不是**"用今天"：采纳 exact research run 自己的业务日。

        调用方（尤其是前端）无从知道那条 run 的 as_of，让它自己填一个日期就等于让
        调用方制造业务日事实。省略是唯一诚实的默认，而不是隐式 current date。
        """
        run_id = self.record_run(_supported_hypothesis())
        result = self.generate(research_run_id=str(run_id), asof=None)
        self.assertEqual(DAY, result["asof"])
        self.assertEqual(2, result["candidate_count"])


class NoProviderAuthorityTests(_Base):
    """C5/C6/C7 — AI 不能选 parent / risk / universe / regime / evaluation。"""

    def _reject(self, payload, expected_detail=SAIP.Reason.INVALID_PROVIDER_FIELD):
        """Provider 拒绝必须带**具体** reason，而不是笼统的协议错误。

        ``invalid_proposal_field``（AI 试图声明它无权声明的字段）与
        ``unknown_proposal_field``（协议漂移）是两类不同的事件，审计上必须可区分。
        """
        self._transport.payload = payload
        with self.assertRaises(SAIPR.AIProviderError) as ctx:
            self.generate(research_run_id=str(self.record_run(_supported_hypothesis())))
        self.assertEqual(SAIPR.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)
        self.assertEqual(expected_detail, ctx.exception.detail)
        return ctx.exception

    def test_c5_ai_cannot_choose_the_parent(self):
        for field, value in (("strategy_id", "evil"), ("strategy_version", 99),
                             ("strategy_checksum", "a" * 64)):
            with self.subTest(field=field):
                self._transport.calls.clear()
                self._reject({"parameter_variants": {"ma_period": [18]}, field: value})

    def test_c6_ai_cannot_control_risk_universe_or_regime(self):
        for field, value in (("constraints", {"max_positions": 999}),
                             ("universe_spec", {"scope_kind": "all"}),
                             ("intended_market_regime", "bull"),
                             ("asof", "2030-01-01")):
            with self.subTest(field=field):
                self._transport.calls.clear()
                self._reject({"parameter_variants": {"ma_period": [18]}, field: value})

    def test_c7_authority_and_scoring_fields_are_rejected(self):
        for field, value in (("score", 0.9), ("sharpe", 3.1), ("status", "supported"),
                             ("promotion", "go"), ("winner", True), ("deploy", "now"),
                             ("rank", 1), ("return", 0.5), ("drawdown", 0.1),
                             ("execution", "buy"), ("order", {}), ("risk_override", {})):
            with self.subTest(field=field):
                self._transport.calls.clear()
                self._reject({"parameter_variants": {"ma_period": [18]}, field: value})

    def test_c7b_unknown_fields_are_rejected_not_ignored(self):
        self._reject({"parameter_variants": {"ma_period": [18]}, "totally_new": 1},
                     SAIP.Reason.UNKNOWN_PROVIDER_FIELD)
        source = open(SAICS.__file__, encoding="utf-8").read()
        # 绝不"静默忽略"未知字段。
        self.assertNotIn("pop(field, None) for field", source)

    def test_c7c_authority_fields_are_distinguished_from_unknown_fields(self):
        """越权尝试与协议漂移必须给出**不同** reason。

        两者的字段集合有重叠的危险：一个被误当成"未知字段"的 ``score`` 会让
        "AI 试图声明评分权威"这一事件在审计里消失。这条断言让
        ``FORBIDDEN_PROVIDER_FIELDS`` 成为**承载语义**的边界，而不是装饰。
        """
        authority = {"parameter_variants": {"ma_period": [18]}, "score": 1}
        with self.assertRaises(SAIP.AIProposalError) as ctx:
            SAIP.build_proposal(authority)
        self.assertEqual(SAIP.Reason.INVALID_PROVIDER_FIELD, ctx.exception.reason)
        with self.assertRaises(SAIP.AIProposalError) as ctx:
            SAIP.build_proposal({"parameter_variants": {"ma_period": [18]},
                                 "no_such_field": 1})
        self.assertEqual(SAIP.Reason.UNKNOWN_PROVIDER_FIELD, ctx.exception.reason)
        # 每一个被禁止的字段都必须在**禁止集合**里，而不是只在 unknown 兜底里。
        for field in sorted(SAIP.FORBIDDEN_PROVIDER_FIELDS):
            with self.subTest(field=field):
                self.assertIn(field, SAIP.FORBIDDEN_PROVIDER_FIELDS)
                with self.assertRaises(SAIP.AIProposalError) as ctx:
                    SAIP.build_proposal({"parameter_variants": {"ma_period": [18]},
                                         field: 1})
                self.assertEqual(SAIP.Reason.INVALID_PROVIDER_FIELD, ctx.exception.reason)


class ProposalShapeTests(_Base):
    """C8/C9 — DSL 与 parameter 合法性继续由既有 owner 裁决。"""

    def test_c8_invalid_dsl_is_rejected(self):
        for alternatives in (
            {"op": "python", "code": "import os"},
            {"op": "eval", "expr": "1+1"},
            {"op": "unknown_op"},
            {"op": "gt", "left": {"op": "field", "name": "close"},
             "right": {"op": "const", "value": 1}, "extra": "x"},
        ):
            with self.subTest(alts=alternatives):
                self._transport.payload = {"entry_slot": {
                    "kind": "explicit_variant", "alternatives": [alternatives]}}
                with self.assertRaises(SAICS.AICandidateGenerationError):
                    self.generate(research_run_id=str(self.record_run(_supported_hypothesis())))

    def test_c8b_free_source_never_reaches_a_candidate(self):
        self._transport.payload = {"entry_slot": {
            "kind": "explicit_variant",
            "alternatives": [{"op": "eval", "source_code": "__import__('os')"}]}}
        with self.assertRaises(SAICS.AICandidateGenerationError):
            self.generate(research_run_id=str(self.record_run(_supported_hypothesis())))
        with self._connection() as conn:
            rows = conn.execute("SELECT candidate_json FROM strategy_candidates").fetchall()
        for row in rows:
            self.assertNotIn("source_code", row[0])
            self.assertNotIn("__import__", row[0])

    def test_c9_invalid_parameter_is_rejected_by_the_existing_schema(self):
        for variants in (
            {"undeclared_param": [1, 2]},      # 未声明参数
            {"ma_period": [20, 24]},           # 超过 max_step=2（相对 parent 值 20）
            {"ma_period": [1]},                # 低于 min=5
            {"ma_period": [999]},              # 超过 max=60
            {"ma_period": ["abc"]},            # 非数值
        ):
            with self.subTest(variants=variants):
                self._transport.payload = {"parameter_variants": variants}
                with self.assertRaises(SAICS.AICandidateGenerationError):
                    self.generate(research_run_id=str(self.record_run(_supported_hypothesis())))

    def test_c9b_no_second_parameter_validator_in_the_new_modules(self):
        """parameter 合法性必须仍归既有 owner，不得另写一套。"""
        for module in (SAIP, SAIPR, SAICS):
            source = open(module.__file__, encoding="utf-8").read()
            for forbidden in ("def validate_parameter", "def _validate_parameter",
                              "def validate_ai_ast", "def _check_ast"):
                self.assertNotIn(forbidden, source)


class CandidateCapTests(_Base):
    """C10 — AI cap ≤ 32，超限 fail closed，绝不截断。"""

    #: ma_period 的合法取值：parent 值 20，min=5 / max=60，``max_step=2`` 是**相对
    #: parent 值**的最大偏移，因此合法域是 [18, 22] 内的整数（20 即继承值）。
    #: 用它构造"合法但多值"的搜索空间，才能把"超限拒绝"与"截断"区分开。
    LEGAL_VALUES = [18, 19, 20, 21, 22]

    def test_c10_cap_boundary(self):
        run_id = self.record_run(_supported_hypothesis())
        self._transport.payload = {"parameter_variants": {
            "ma_period": list(self.LEGAL_VALUES)}}
        # 5 个合法值 ≤ 32：整批成功，且一个都不少。
        result = self.generate(research_run_id=str(run_id), max_candidates=32)
        self.assertEqual(5, result["candidate_count"])

    def test_c10b_over_cap_is_rejected_not_truncated(self):
        run_id = self.record_run(_supported_hypothesis())
        self._transport.payload = {"parameter_variants": {
            "ma_period": list(self.LEGAL_VALUES)}}
        with self.assertRaises(SAICS.AICandidateGenerationError) as ctx:
            self.generate(research_run_id=str(run_id), max_candidates=3)
        # 明确是"超限拒绝"，不是被别的规则拦下。
        self.assertEqual(SAIP.Reason.SEARCH_SPACE_REJECTED, ctx.exception.reason)
        # 绝不截断：没有任何 batch / candidate 被写入。
        with self._connection() as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM strategy_candidate_generation_batches").fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM strategy_candidates").fetchone()[0])

    def test_c10c_requested_cap_above_the_ai_cap_is_rejected(self):
        run_id = self.record_run(_supported_hypothesis())
        for requested in (33, 64, 128):
            self.calls.clear()
            with self.assertRaises(SAICS.AICandidateGenerationError) as ctx:
                self.generate(research_run_id=str(run_id), max_candidates=requested)
            self.assertEqual(SAICS.REASON_AI_CANDIDATE_CAP, ctx.exception.reason)
            self.assertEqual([], self.calls)

    def test_c10d_ai_cap_is_below_the_r35b_global_cap(self):
        import strategy_candidate_search_space as SS
        self.assertLessEqual(SAIP.MAX_AI_CANDIDATES_PER_REQUEST,
                             SS.MAX_CANDIDATES_PER_GENERATION_REQUEST)

    def test_c10e_resource_bounds_are_explicit(self):
        self.assertLessEqual(SAIP.MAX_AI_PARAMETERS, 8)
        self.assertLessEqual(SAIP.MAX_AI_VALUES_PER_PARAMETER, 8)
        self.assertLessEqual(SAIP.MAX_AI_ALTERNATIVES_PER_SLOT, 8)
        # 资源上界在禁止字段扫描**之前**生效：畸形大输入不会先被逐个字段检查。
        huge = {"parameter_variants": {f"p{i}": [1, 2, 3, 4, 5, 6, 7, 8, 9]
                                      for i in range(20)}}
        with self.assertRaises(SAIP.AIProposalError) as ctx:
            SAIP.build_proposal(huge)
        self.assertEqual(SAIP.Reason.RESOURCE_LIMIT, ctx.exception.reason)


class NoOpProposalTests(_Base):
    """C11 — 全部 inherit / 无变化 = no-op，必须拒绝。"""

    def test_c11_no_op_proposal_is_rejected(self):
        run_id = self.record_run(_supported_hypothesis())
        for payload in (
            {"factor_slot": {"kind": "inherit_parent"}},
            {"factor_slot": {"kind": "inherit_parent"},
             "entry_slot": {"kind": "inherit_parent"},
             "exit_slot": {"kind": "inherit_parent"}},
            {},
            {"parameter_variants": {}},
            {"factor_slot": {"kind": "explicit_variant", "alternatives": []}},
        ):
            with self.subTest(payload=payload):
                self._transport.payload = payload
                with self.assertRaises(SAIPR.AIProviderError) as ctx:
                    self.generate(research_run_id=str(run_id))
                self.assertEqual(SAIPR.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)
        # 全程零写入。
        with self._connection() as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM strategy_candidate_generation_batches").fetchone()[0])

    def test_c11b_absent_counts_as_a_declared_change(self):
        """``absent`` 是显式声明"本候选没有该角色"，属于真实变化。"""
        proposal = SAIP.build_proposal({"exit_slot": {"kind": "absent"}})
        self.assertEqual({"exit"}, set(proposal.declared_axes))


class CrossModelDedupTests(_Base):
    """C12 — 跨 model 同语义 proposal 仍 candidate dedup。"""

    def test_c12_same_semantics_across_models_dedup_with_separate_provenance(self):
        run_id = self.record_run(_supported_hypothesis())
        # Provider A：model A 提出 ma_period [18, 20]（两种取值顺序都必须等价）。
        self._transport.payload = {"parameter_variants": {"ma_period": [18, 20]}}
        first = self.generate(research_run_id=str(run_id),
                              provider_config=self._slot("a", "model-a"))
        # Provider B：另一个 model 提出**完全相同**的 search space。
        self._transport.payload = {"parameter_variants": {"ma_period": [20, 18]}}
        second = self.generate(research_run_id=str(run_id),
                               provider_config=self._slot("b", "model-b"))

        self.assertEqual(first["candidate_ids"], second["candidate_ids"])
        with self._connection() as conn:
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) FROM strategy_candidates").fetchone()[0])
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) FROM strategy_candidate_generation_batches").fetchone()[0])
            # 每个语义候选被两次提出 ⇒ 每个 2 条 proposal 事件。
            per_candidate = conn.execute(
                "SELECT candidate_id, COUNT(*) FROM strategy_candidate_proposals"
                " GROUP BY candidate_id").fetchall()
        self.assertEqual(2, len(per_candidate))
        for _, count in per_candidate:
            self.assertEqual(2, count)

    def test_c12c_provider_slot_is_recorded_and_bound_into_the_input_fingerprint(self):
        """P2：只记 model 会丢掉"哪个 provider 槽位提出的"。

        ``ai1`` 与 ``ai2`` 可能配**同一个** model。若 ``model_identity`` 只有 model，
        两个槽位提出完全相同 proposal 时台账上看不出差别：candidate 身份相同（正确）、
        事件两条（正确），但"哪个 provider 提出"**无法回答**，且
        ``generation_input_fingerprint`` 也会相同 —— 输入事实的差异被抹平。

        这条同时证明"provider/model provenance 属于事件，candidate specification 属于
        内容"。
        """
        run_id = self.record_run(_supported_hypothesis())
        self._transport.payload = {"parameter_variants": {"ma_period": [18, 20]}}
        first = self.generate(research_run_id=str(run_id),
                              provider_config={"slot": "ai1", "model": "model-x",
                                               "api_key": "k",
                                               "base_url": "https://a.example.com/v1",
                                               "enabled": True})
        second = self.generate(research_run_id=str(run_id),
                               provider_config={"slot": "ai2", "model": "model-x",
                                                "api_key": "k",
                                                "base_url": "https://b.example.com/v1",
                                                "enabled": True})

        # candidate specification 属于内容：同一语义 ⇒ 同一批 candidate id。
        self.assertEqual(first["candidate_ids"], second["candidate_ids"])
        # 事件与批次是两次：batch identity / input fingerprint 都必须不同。
        self.assertNotEqual(first["generation_batch_id"], second["generation_batch_id"])
        self.assertNotEqual(first["generation_input_fingerprint"],
                            second["generation_input_fingerprint"])

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT proposal_json FROM strategy_candidate_proposals"
                " ORDER BY created_at, proposal_id").fetchall()
        identities = [json.loads(row[0])["model_identity"] for row in rows]
        providers = sorted(item.get("provider") for item in identities)
        models = sorted(item.get("model") for item in identities)
        self.assertEqual(["ai1", "ai1", "ai2", "ai2"], providers)
        self.assertEqual(["model-x"] * 4, models)
        # provenance 里仍然只有 canonical 槽位身份 + model，没有厂商耦合、没有编造字段。
        for identity in identities:
            self.assertEqual({"provider", "model"}, set(identity))

    def test_c12d_provider_identity_is_absent_when_the_slot_is_unknown(self):
        """没有可靠槽位就留空，不编造。"""
        self.assertEqual({"model": "m"},
                         SAICS._provider_model_identity({"model": "m"}))
        self.assertEqual({"provider": "ai1"},
                         SAICS._provider_model_identity({"slot": "ai1"}))
        self.assertEqual({}, SAICS._provider_model_identity({}))
        self.assertEqual({}, SAICS._provider_model_identity(None))

    def test_c12b_each_proposal_retains_its_own_model(self):
        run_id = self.record_run(_supported_hypothesis())
        self._transport.payload = {"parameter_variants": {"ma_period": [18, 20]}}
        self.generate(research_run_id=str(run_id),
                      provider_config=self._slot("a", "model-a"))
        self.generate(research_run_id=str(run_id),
                      provider_config=self._slot("b", "model-b"))
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT proposal_json FROM strategy_candidate_proposals").fetchall()
        models = sorted(json.loads(row[0])["model_identity"].get("model") for row in rows)
        self.assertEqual(["model-a", "model-a", "model-b", "model-b"], models)


class ResearchProvenanceTests(_Base):
    """C13 — provenance 精确绑定 exact run + record_hash。"""

    def test_c13_provenance_binds_the_exact_run_and_record_hash(self):
        run_id = self.record_run(_supported_hypothesis())
        self.generate(research_run_id=str(run_id))
        with self._connection() as conn:
            run = conn.execute(
                f"SELECT record_hash FROM {ARR.TABLE} WHERE id=?", (run_id,)).fetchone()
            rows = conn.execute(
                "SELECT proposal_json FROM strategy_candidate_proposals").fetchall()
        provenance = json.loads(rows[0][0])["research_provenance"]
        self.assertEqual("ai_research", provenance["source_kind"])
        self.assertEqual(f"ai_research_run:{run_id}", provenance["source_identity"])
        self.assertEqual(run[0], provenance["source_fingerprint"])
        self.assertEqual("hyp_1", provenance["hypothesis_id"])

    def test_c13b_switching_the_run_changes_the_generation_input_fingerprint(self):
        first = self.generate(research_run_id=str(self.record_run(
            _supported_hypothesis(hypothesis_id="h_a"))))
        second = self.generate(research_run_id=str(self.record_run(
            _supported_hypothesis(hypothesis_id="h_b"))))
        self.assertNotEqual(first["generation_input_fingerprint"],
                            second["generation_input_fingerprint"])
        # 但语义相同的候选仍然是同一行。
        self.assertEqual(first["candidate_ids"], second["candidate_ids"])

    def test_c13c_no_resentinel_identity_is_used(self):
        run_id = self.record_run(_supported_hypothesis())
        self.generate(research_run_id=str(run_id))
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT proposal_json FROM strategy_candidate_proposals").fetchall()
        for row in rows:
            self.assertNotIn("latest-ai-research", row[0])


class TransactionBoundaryTests(_Base):
    """C14/C15/C16 — 网络在事务外、失败零写入、写失败整批回滚。"""

    def test_c14_network_call_holds_no_write_transaction(self):
        """provider 调用期间必须没有活跃写事务。"""
        run_id = self.record_run(_supported_hypothesis())
        observed = {}

        def _spy(provider_config, system_prompt, user_prompt, max_tokens=1800):
            probe = sqlite3.connect(self.path, isolation_level=None)
            try:
                # 另一条连接此刻必须能立刻拿到写锁 —— 说明调用方没有持有它。
                probe.execute("BEGIN IMMEDIATE")
                probe.execute("ROLLBACK")
                observed["free"] = True
            except sqlite3.OperationalError:
                observed["free"] = False
            finally:
                probe.close()
            return ({"parameter_variants": {"ma_period": [18, 20]}}, 10, 5, 1)

        self._transport.error = None
        SAIPR.transport.call_json = _spy
        self.addCleanup(self._restore_transport)
        self.generate(research_run_id=str(run_id))
        self.assertTrue(observed.get("free"), "provider 调用期间不应持有 SQLite 写锁")

    def test_c15_provider_failure_leaves_zero_writes(self):
        run_id = self.record_run(_supported_hypothesis())
        before = self.counts()
        # transport 失败被包装成稳定 reason。
        self._transport.error = SAIPR.transport.ProviderTransportError("timeout")
        with self.assertRaises(SAIPR.AIProviderError) as ctx:
            self.generate(research_run_id=str(run_id))
        self.assertEqual(SAIPR.REASON_TRANSPORT, ctx.exception.reason)
        # 协议错误（非 JSON object / 未知字段）同样零写入。
        self._transport.error = None
        for payload in ("not an object", [], 3, {"score": 1}):
            self._transport.payload = payload
            with self.assertRaises(SAIPR.AIProviderError):
                self.generate(research_run_id=str(run_id))
        self.assertEqual(before, self.counts())

    def test_c15b_provider_failure_does_not_recount_old_candidates(self):
        """已有旧 candidate 时，失败绝不能把它们算成本次结果。"""
        run_id = self.record_run(_supported_hypothesis())
        first = self.generate(research_run_id=str(run_id))
        before = self.counts()
        self.assertEqual(2, first["candidate_count"])
        self._transport.error = SAIPR.transport.ProviderTransportError("timeout")
        with self.assertRaises(SAIPR.AIProviderError):
            self.generate(research_run_id=str(run_id))
        self.assertEqual(before, self.counts())

    def test_c16_write_failure_rolls_back_the_whole_batch(self):
        run_id = self.record_run(_supported_hypothesis())
        before = self.counts()
        original = SCRepo.record_proposal
        calls = {"n": 0}

        def _flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise sqlite3.OperationalError("injected write failure")
            return original(*args, **kwargs)

        SCRepo.record_proposal = _flaky
        self.addCleanup(setattr, SCRepo, "record_proposal", original)
        with self.assertRaises(sqlite3.OperationalError):
            self.generate(research_run_id=str(run_id))
        # 整批回滚：batch / proposal / candidate 都不出现。
        self.assertEqual(before, self.counts())


class BatchAuditabilityTests(_Base):
    """C17 — generation batch 可完整审计 canonical search space。"""

    def test_c17_persisted_search_space_self_verifies(self):
        run_id = self.record_run(_supported_hypothesis())
        result = self.generate(research_run_id=str(run_id))
        with self._connection() as conn:
            batch = SCV.get_generation_batch(conn, result["generation_batch_id"])
        row = batch["generation_batch"]
        self.assertTrue(SCV.verify_batch_search_space_material(row))
        self.assertEqual(result["search_space_fingerprint"],
                         row["search_space_fingerprint"])
        # 完整审计链：哪个 research run / hypothesis / model / search space / parent。
        material = row["search_space_material"]["extra"]
        self.assertEqual(run_id, material["research_run_id"])
        self.assertIn("ai_proposal", material)
        self.assertEqual(PARENT, row["parent_strategy_id"])
        self.assertEqual("bounded_combination", row["generator_type"])

    def test_c17b_tampered_material_does_not_verify(self):
        run_id = self.record_run(_supported_hypothesis())
        result = self.generate(research_run_id=str(run_id))
        with self._connection() as conn:
            row = dict(SCV.get_generation_batch(
                conn, result["generation_batch_id"])["generation_batch"])
        row["search_space_material"] = {
            "search_space": {"tampered": True}, "extra": {}}
        self.assertFalse(SCV.verify_batch_search_space_material(row))

    def test_c17c_legacy_batch_without_material_stays_unknown(self):
        """历史 batch 没有 material：保持 legacy，不回填、不反推。"""
        self.assertFalse(SCV.verify_batch_search_space_material(
            {"search_space_fingerprint": "a" * 64}))


class AuthorityBoundaryTests(_Base):
    """C18 — 不进入 evaluation / promotion / execution；不新增 AI DB authority。"""

    def test_c18_generator_path_has_no_authority_dependency(self):
        for module in (SAIP, SAIPR, SAICS):
            source = open(module.__file__, encoding="utf-8").read()
            for forbidden in ("walk_forward", "learning_evaluation", "promotion",
                              "strategy_lifecycle", "backtest", "order_execution",
                              "portfolio_allocation"):
                self.assertNotIn(f"import {forbidden}", source,
                                 f"{module.__name__} must not import {forbidden}")

    def test_c18b_no_second_ai_provider_transport(self):
        """AI 网络请求必须继续唯一经由 ai_provider_transport。"""
        for module in (SAIP, SAIPR, SAICS):
            source = open(module.__file__, encoding="utf-8").read()
            for forbidden in ("requests.post", "urllib.request", "urlopen",
                              "http.client", "OpenAI(", "Anthropic("):
                self.assertNotIn(forbidden, source,
                                 f"{module.__name__} must not call the network directly")

    def test_c18c_no_ai_specific_database_table(self):
        with self._connection() as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        for forbidden in ("strategy_ai_candidates", "ai_generated_strategies",
                          "llm_candidate_table", "ai_strategy_proposals_db",
                          "strategy_ai_proposal_runs"):
            self.assertNotIn(forbidden, names)

    def test_c18d_no_new_facade_or_helper_module(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for forbidden in ("ai_strategy_manager.py", "ai_strategy_facade.py",
                          "ai_candidate_utils.py", "candidate_generation_helper.py",
                          "research_candidate_common.py", "proposal_utils.py",
                          "ai_candidate_helper.py", "candidate_ai_facade.py"):
            self.assertFalse(os.path.exists(os.path.join(here, forbidden)))

    def test_c18e_research_authority_is_never_upgraded(self):
        """provider 与 proposal 层都不得把 supported 读成 approved。"""
        for module in (SAIP, SAIPR, SAICS):
            source = open(module.__file__, encoding="utf-8").read()
            for forbidden in ("is_authoritative = True", '"authority": "signal"',
                              "activate_strategy", "promote("):
                self.assertNotIn(forbidden, source)

    def test_c18f_provider_never_sees_the_parent_as_a_choice(self):
        """prompt 只给已冻结的 parent，不问"选哪个策略"。"""
        run_id = self.record_run(_supported_hypothesis())
        self.generate(research_run_id=str(run_id))
        prompt = self.calls[0]["user_prompt"]
        self.assertIn(PARENT, prompt)
        for forbidden in ("选择当前最优", "选择策略", "choose the best", "which strategy"):
            self.assertNotIn(forbidden, prompt)

    def test_c18g_prompt_declares_research_text_is_data(self):
        run_id = self.record_run(_supported_hypothesis())
        self.generate(research_run_id=str(run_id))
        system = self.calls[0]["system_prompt"]
        self.assertIn("数据", system)
        self.assertIn("不是指令", system.replace("不是**指令**", "不是指令"))

    def test_c18h_provider_does_not_see_secrets_in_its_prompt(self):
        run_id = self.record_run(_supported_hypothesis())
        self.generate(research_run_id=str(run_id),
                      provider_config=self._slot("a", "m", api_key="sk-secret-value"))
        blob = self.calls[0]["user_prompt"] + self.calls[0]["system_prompt"]
        self.assertNotIn("sk-secret-value", blob)


class ProviderProtocolTests(_Base):
    """provider 边界：非 object、transport 失败、模型 identity 不编造。"""

    def test_malformed_parameter_variants_fail_closed_not_typeerror(self):
        """畸形形状必须是**稳定的拒绝**，不能泄漏 TypeError / AttributeError。

        ``len()`` / ``.items()`` 作用在非 object 上会抛裸 TypeError / AttributeError，
        那不是 ``AIProposalError`` 也不 fail closed —— provider 的畸形 JSON 会以未分类
        异常穿透契约，在 API 层变成 5xx。形状必须**先于**度量被验证。
        """
        for payload in (
            {"parameter_variants": 5},
            {"parameter_variants": "x"},
            {"parameter_variants": [1, 2]},
            {"parameter_variants": {"ma_period": 5}},
            {"factor_slot": "inherit_parent"},
            {"entry_slot": 3},
            {"exit_slot": ["x"]},
            {"parameter_variants": {"ma_period": [18]}, "factor_slot": 7},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(SAIP.AIProposalError) as ctx:
                    SAIP.build_proposal(payload)
                self.assertEqual(SAIP.Reason.INVALID_PROPOSAL_SHAPE, ctx.exception.reason)

    def test_malformed_provider_shape_is_a_provider_error_not_a_crash(self):
        """同一件事走完整路径：provider 畸形 → AIProviderError（可映射 502），不是 500。"""
        run_id = self.record_run(_supported_hypothesis())
        for payload in ({"parameter_variants": 5}, {"factor_slot": "nope"}):
            self._transport.payload = payload
            with self.assertRaises(SAIPR.AIProviderError) as ctx:
                self.generate(research_run_id=str(run_id))
            self.assertEqual(SAIPR.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)

    def test_corrupt_research_record_is_translated_at_the_service_boundary(self):
        """损坏行必须变成稳定 reason，而不是裸 ValueError 穿透成 5xx。"""
        run_id = self.record_run(_supported_hypothesis())
        # 直接篡改持久化内容：record_hash 将不再匹配，get_run 读取时 fail closed。
        with self._connection() as conn:
            conn.execute(f'UPDATE {ARR.TABLE} SET hypothesis = ?  WHERE id = ?',
                         ('{"hypothesis_id":"tampered"}', run_id))
        self.calls.clear()
        with self.assertRaises(SAICS.AICandidateGenerationError) as ctx:
            self.generate(research_run_id=str(run_id))
        self.assertEqual(SAICS.REASON_RESEARCH_UNSUPPORTED, ctx.exception.reason)
        # 而且仍然没有付费调用。
        self.assertEqual([], self.calls)

    def test_non_object_response_is_rejected(self):
        run_id = self.record_run(_supported_hypothesis())
        for payload in ([], "x", 3, None):
            self._transport.payload = payload
            with self.assertRaises(SAIPR.AIProviderError) as ctx:
                self.generate(research_run_id=str(run_id))
            self.assertEqual(SAIPR.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)

    def test_transport_error_is_wrapped_without_echoing_secrets(self):
        run_id = self.record_run(_supported_hypothesis())
        self._transport.error = SAIPR.transport.ProviderTransportError(
            "auth failed for sk-secret-value")
        with self.assertRaises(SAIPR.AIProviderError) as ctx:
            self.generate(research_run_id=str(run_id))
        self.assertEqual(SAIPR.REASON_TRANSPORT, ctx.exception.reason)
        # 原始信息（可能含 secret）不得回显。
        self.assertNotIn("sk-secret-value", str(ctx.exception))

    def test_model_identity_records_no_fabricated_model(self):
        """model 缺失时**不编造**：只记录已知的 provider 槽位，model 键不出现。

        注意这条只测**映射函数**：readiness 要求 model 存在，所以"缺 model 还真的发出
        请求"在完整路径上已被 provider_slot_not_ready 挡在前面（见
        ProviderReadinessTests）。不编造这条规则本身仍要独立成立。
        """
        self.assertEqual({"provider": "a"}, SAICS._provider_model_identity(
            {"slot": "a", "api_key": "k", "base_url": "https://ai1.example.com/v1"}))
        self.assertEqual({"provider": "a", "model": "m"}, SAICS._provider_model_identity(
            {"slot": "a", "model": "m"}))
        self.assertEqual({}, SAICS._provider_model_identity({}))
        self.assertEqual({}, SAICS._provider_model_identity(None))
        # 绝不自造 version。
        self.assertNotIn("version", SAICS._provider_model_identity(
            {"slot": "a", "model": "m"}))

    def test_research_model_and_proposal_model_stay_distinct(self):
        """research run 的 model 与 candidate-proposal 的 model 不是同一个概念。"""
        run_id = self.record_run(_supported_hypothesis())
        self.generate(research_run_id=str(run_id),
                      provider_config=self._slot("a", "proposal-model"))
        with self._connection() as conn:
            run = conn.execute(f"SELECT provider_model FROM {ARR.TABLE}"
                               " WHERE id=?", (run_id,)).fetchone()
            row = conn.execute(
                "SELECT proposal_json FROM strategy_candidate_proposals").fetchone()
        self.assertEqual("research-model", run[0])
        self.assertEqual("proposal-model",
                         json.loads(row[0])["model_identity"]["model"])


class EvidenceCountTests(_Base):
    """evidence_count 由 R27 派生，AI 无权自述。"""

    def test_ai_cannot_declare_evidence_count(self):
        run_id = self.record_run(_supported_hypothesis())
        self._transport.payload = {"parameter_variants": {"ma_period": [18]},
                                   "evidence_count": 100}
        with self.assertRaises(SAIPR.AIProviderError):
            self.generate(research_run_id=str(run_id))

    def test_evidence_count_comes_from_the_canonical_hypothesis(self):
        """evidence_count 由 R27 canonical 投影派生，并进入 generation input 指纹。"""
        run_id = self.record_run(_supported_hypothesis(evidence_count=3))
        self.generate(research_run_id=str(run_id))
        # 它属于**输入**事实（search space），不属于 proposal 事件 payload。
        with self._connection() as conn:
            row = conn.execute(
                "SELECT batch_json FROM strategy_candidate_generation_batches"
            ).fetchone()
        batch = json.loads(row[0])
        self.assertEqual(
            3, batch["search_space_material"]["search_space"]["evidence_count"])
        self.assertEqual(3, batch["material"]["search_space_material"]["search_space"]
                         ["evidence_count"])

    def test_evidence_count_is_bound_into_the_input_fingerprint(self):
        """证据量是输入事实：它变化必须改变 generation input 指纹。"""
        three = self.generate(research_run_id=str(self.record_run(
            _supported_hypothesis(hypothesis_id="h_three", evidence_count=3))))
        five = self.generate(research_run_id=str(self.record_run(
            _supported_hypothesis(hypothesis_id="h_five", evidence_count=5))))
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT batch_json FROM strategy_candidate_generation_batches"
                " ORDER BY created_at, batch_id").fetchall()
        counts = sorted(json.loads(row[0])["search_space_material"]["search_space"]
                        ["evidence_count"] for row in rows)
        self.assertEqual([3, 5], counts)
        self.assertNotEqual(three["generation_input_fingerprint"],
                            five["generation_input_fingerprint"])


if __name__ == "__main__":
    unittest.main()
