# -*- coding: utf-8 -*-
"""R27-B1 —— provider-neutral transport 与 typed research provider adapter 回归。

分三组：

    PROVIDER-01 ~ 10   :mod:`ai_provider_transport` —— 厂商中立、fail closed、
                       secret 不泄漏、旧调用入口保持兼容
    RPROV-01 ~ 16      :mod:`ai_research_provider` —— typed 输入、strict 输出协议、
                       authority 边界、PIT、证据冲突
    RG-01 ~ 06         架构 guard —— 依赖方向、网络 owner、无写路径

**全部离线**：所有 provider 调用都被 ``urllib.request.urlopen`` 的桩替换，
CI 绝不产生 API 费用，也不访问任何真实 endpoint。

时间一律显式传入（固定业务日字符串），绝不读墙上时钟。
"""
from __future__ import annotations

import ast
import json
import os
import sys
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_provider_transport as T  # noqa: E402
import ai_research_contract as ARC  # noqa: E402
import ai_research_provider as P  # noqa: E402
import market_data_contract as MDC  # noqa: E402

DAY = "2026-08-27"
NEXT_DAY = "2026-08-28"
CODE = "600000"
DEFAULT_POLICY = "live_market"

CROSS_SOURCE = "cross_source_checked"
SINGLE_SOURCE = "range_timestamp_checked"

CONFIG = {
    "slot": "ai1",
    "api_key": "fake-slot-key",
    "base_url": "https://provider.example.com/v1",
    "model": "model-one",
    "timeout_seconds": 30,
}


# ─────────────────────────────────────────────────────────────────────────────
# helpers —— 走真实 owner 路径产出 typed evidence
# ─────────────────────────────────────────────────────────────────────────────


def _reading(*, as_of=DAY, code=CODE, validation=CROSS_SOURCE, observed_at=None, price=10.5):
    stamp = observed_at or f"{as_of}T10:30:00+08:00"
    snapshot = MDC.symbol_quote_snapshot(
        {"code": code, "price": price, "quote_at": stamp,
         "quote_source": "eastmoney", "quote_validation": validation},
        asof_day=as_of,
    )
    return MDC.classify(snapshot, MDC.policy_named(DEFAULT_POLICY), now=stamp, asof_day=as_of)


def _event(*, as_of=DAY, code=CODE, validation=CROSS_SOURCE, observed_at=None,
           price=10.5, payload=None):
    return ARC.InformationEvent(
        as_of=as_of, source="market_data_service",
        evidence_ref=ARC.evidence_ref_from_market_reading(
            _reading(as_of=as_of, code=code, validation=validation,
                     observed_at=observed_at, price=price),
        ),
        payload=dict(payload if payload is not None else {"price": price, "kind": "symbol_quote"}),
    )


def _reply(content, *, prompt_tokens=11, completion_tokens=7):
    """一个 OpenAI-compatible 外层响应。"""
    body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return {
        "choices": [{"message": {"role": "assistant", "content": body}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class _FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TransportTestBase(unittest.TestCase):
    """替换 urllib 传输；``network_calls`` 计数供"未发出请求"断言使用。"""

    def setUp(self):
        self.network_calls = 0

    def patch_urlopen(self, payload):
        def fake_urlopen(request, timeout=None):
            self.network_calls += 1
            self.last_request = request
            if isinstance(payload, Exception):
                raise payload
            return _FakeResponse(payload)

        return mock.patch.object(T.urllib.request, "urlopen", side_effect=fake_urlopen)


class ResearchTestBase(TransportTestBase):
    def run_research(self, content, *, events=None, as_of=DAY, config=None, **kwargs):
        with self.patch_urlopen(_reply(content)):
            return P.run_research(
                provider_config=dict(config or CONFIG),
                hypothesis_id="H-1", as_of=as_of, subject=CODE,
                question="momentum 是否延续？",
                events=tuple(events if events is not None else (_event(),)),
                **kwargs,
            )


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER-01 ~ 10 —— transport
# ─────────────────────────────────────────────────────────────────────────────


class ProviderTransportTests(TransportTestBase):
    def test_PROVIDER_01_request_body_is_the_minimal_openai_compatible_schema(self):
        """PROVIDER-01：请求体只有最小公共字段，协议稳定。"""
        body = T.build_request_body(CONFIG, "sys", "user")
        self.assertEqual(
            {"model", "messages", "response_format", "max_tokens", "stream"}, set(body),
        )
        self.assertEqual("model-one", body["model"])
        self.assertEqual(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}],
            body["messages"],
        )
        self.assertEqual({"type": "json_object"}, body["response_format"])
        self.assertIs(False, body["stream"])
        self.assertIn("max_tokens", body)

    def test_PROVIDER_02_slot_identity_never_changes_the_request_schema(self):
        """PROVIDER-02：ai1 / ai2 使用**完全相同**的协议，槽位身份不是协议维度。"""
        body_a = T.build_request_body(dict(CONFIG, slot="ai1"), "s", "u")
        body_b = T.build_request_body(dict(CONFIG, slot="ai2"), "s", "u")
        self.assertEqual(body_a, body_b, "槽位身份改变了请求体")
        # 也证明 display_name 这类展示字段不参与 transport 分支
        with_name = T.build_request_body(dict(CONFIG, display_name="主审核"), "s", "u")
        self.assertEqual(body_a, with_name, "display_name 参与了 transport 分支")
        self.assertNotIn("thinking", json.dumps(body_a))

    def test_PROVIDER_03_missing_api_key_fails_closed_before_any_network_call(self):
        """PROVIDER-03：缺 api_key → fail closed，且**未发出**网络请求。"""
        with self.patch_urlopen(_reply({"ok": True})):
            with self.assertRaises(T.ProviderTransportError) as ctx:
                T.call_json(dict(CONFIG, api_key=""), "s", "u")
        self.assertEqual(T.REASON_API_KEY_MISSING, ctx.exception.reason)
        self.assertEqual(0, self.network_calls, "缺 Key 时不得发出网络请求")

    def test_PROVIDER_04_missing_model_fails_closed(self):
        """PROVIDER-04：缺 model → fail closed。"""
        with self.assertRaises(T.ProviderTransportError) as ctx:
            T.build_request_body(dict(CONFIG, model="  "), "s", "u")
        self.assertEqual(T.REASON_MODEL_MISSING, ctx.exception.reason)

    def test_PROVIDER_05_missing_or_invalid_base_url_fails_closed(self):
        """PROVIDER-05：缺失 / 非法 base_url → fail closed（网络前）。"""
        for bad in ("", "   ", "abc", "123", "://wrong", "ftp://example.com/v1", None):
            with self.subTest(base_url=bad):
                with self.patch_urlopen(_reply({"ok": True})):
                    with self.assertRaises(T.ProviderTransportError) as ctx:
                        T.call_json(dict(CONFIG, base_url=bad), "s", "u")
                self.assertEqual(T.REASON_BASE_URL_INVALID, ctx.exception.reason)
                self.assertEqual(0, self.network_calls)

    def test_PROVIDER_06_a_json_object_response_is_parsed_with_usage(self):
        """PROVIDER-06：合法 JSON object 响应被正确解析，usage 一并返回。"""
        with self.patch_urlopen(_reply({"decision": "hold"}, prompt_tokens=13, completion_tokens=5)):
            parsed, in_tok, out_tok, latency = T.call_json(CONFIG, "s", "u")
        self.assertEqual({"decision": "hold"}, parsed)
        self.assertEqual(13, in_tok)
        self.assertEqual(5, out_tok)
        self.assertIsInstance(latency, int)
        self.assertEqual(1, self.network_calls)

    def test_PROVIDER_07_non_object_content_fails_closed(self):
        """PROVIDER-07：content 是 list / scalar / null / 非 JSON → fail closed。

        ``[]`` / ``"abc"`` / ``123`` / ``null`` 都**不是**成功的响应。
        """
        for content in ([], "abc", 123, None, "not json at all", '"str"', "[]", "123"):
            with self.subTest(content=content):
                with self.patch_urlopen(_reply(content)):
                    with self.assertRaises(T.ProviderTransportError) as ctx:
                        T.call_json(CONFIG, "s", "u")
                self.assertIn(
                    ctx.exception.reason,
                    (T.REASON_CONTENT_NOT_STRING, T.REASON_CONTENT_NOT_OBJECT),
                )

    def test_PROVIDER_07b_malformed_envelope_fails_closed(self):
        """PROVIDER-07b：外层响应结构不完整（choices / message 缺失）→ fail closed。"""
        for payload in ({}, {"choices": []}, {"choices": "x"}, {"choices": [{}]},
                        {"choices": [{"message": None}]}, {"choices": [{"message": {}}]}):
            with self.subTest(payload=payload):
                with self.patch_urlopen(payload):
                    with self.assertRaises(T.ProviderTransportError) as ctx:
                        T.call_json(CONFIG, "s", "u")
                self.assertIn(
                    ctx.exception.reason,
                    (T.REASON_CHOICES_MISSING, T.REASON_CONTENT_NOT_STRING),
                )

    def test_PROVIDER_08_http_and_network_errors_never_leak_the_api_key(self):
        """PROVIDER-08：HTTP / 网络错误不得泄漏 API Key、请求头或 prompt。"""
        secret = "sk-super-secret-value-1234567890"
        import urllib.error

        cases = (
            urllib.error.HTTPError("https://provider.example.com/chat/completions", 401,
                                   "Unauthorized", {}, None),
            urllib.error.URLError("connection refused"),
        )
        for exc in cases:
            with self.subTest(exc=type(exc).__name__):
                with self.patch_urlopen(exc):
                    with self.assertRaises(T.ProviderTransportError) as ctx:
                        T.call_json(dict(CONFIG, api_key=secret), "SYS-PROMPT", "USER-PROMPT")
                blob = "%s|%s|%s" % (ctx.exception, ctx.exception.reason, repr(ctx.exception))
                for leaked in (secret, "Bearer", "SYS-PROMPT", "USER-PROMPT", "Authorization"):
                    self.assertNotIn(leaked, blob, f"异常泄漏了 {leaked}")

    def test_PROVIDER_08b_http_error_keeps_only_a_stable_reason_and_status(self):
        """PROVIDER-08b：HTTP 错误只保留稳定 reason + status code。"""
        import urllib.error

        with self.patch_urlopen(urllib.error.HTTPError(
                "https://provider.example.com/v1/chat/completions", 503, "busy", {}, None)):
            with self.assertRaises(T.ProviderTransportError) as ctx:
                T.call_json(CONFIG, "s", "u")
        self.assertEqual(T.REASON_HTTP_ERROR, ctx.exception.reason)
        self.assertEqual(503, ctx.exception.status)

    def test_PROVIDER_09_transport_has_no_database_or_environment_access(self):
        """PROVIDER-09：transport 不自读配置来源（无 sqlite3 / getenv / environ）。"""
        tree = ast.parse(_source("ai_provider_transport.py"))
        roots = _imported_roots(tree)
        for forbidden in ("sqlite3", "os", "pathlib", "subprocess", "logging"):
            with self.subTest(module=forbidden):
                self.assertNotIn(forbidden, roots, f"transport import 了 {forbidden}")
        called = _called_names(tree)
        for forbidden in ("getenv", "environ", "connect", "execute", "commit"):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, called, f"transport 调用了 {forbidden}()")

    def test_PROVIDER_10_ai_review_service_legacy_entrypoints_stay_compatible(self):
        """PROVIDER-10：``ai_review_service`` 的旧入口行为不变（薄包装）。"""
        import ai_review_service as S

        body = S.build_request_body(dict(CONFIG, slot="ai1"), "sys", "user")
        self.assertEqual(T.build_request_body(dict(CONFIG, slot="ai1"), "sys", "user"), body)
        self.assertEqual(
            {"model", "messages", "response_format", "max_tokens", "stream"}, set(body),
        )
        with self.assertRaises(RuntimeError) as ctx:
            S.build_request_body({"slot": "ai1", "model": ""}, "s", "u")
        self.assertIn("ai1_model_missing", str(ctx.exception))

        # base_url 归一化的历史契约必须逐字保持，含 ValueError 类型
        self.assertEqual("https://a.example.com/v1", S.normalize_base_url("https://a.example.com/v1/"))
        self.assertEqual("https://a.example.com/chat/completions",
                         S.chat_completions_url("https://a.example.com"))
        self.assertEqual("https://a.example.com/v1/chat/completions",
                         S.chat_completions_url("https://a.example.com/v1/"))
        with self.assertRaises(ValueError):
            S.chat_completions_url("   ")

        with self.patch_urlopen(_reply({"decision": "hold"})):
            self.assertEqual(
                ({"decision": "hold"}, 11, 7, mock.ANY),
                S._call_slot(dict(CONFIG), "s", "u"),
            )


# ─────────────────────────────────────────────────────────────────────────────
# RPROV-01 ~ 16 —— typed research provider adapter
# ─────────────────────────────────────────────────────────────────────────────


class ResearchProviderTests(ResearchTestBase):
    def _relations(self, content, *, events=None, as_of=DAY, **kwargs):
        result = self.run_research(content, events=events, as_of=as_of, **kwargs)
        return result.hypothesis

    def test_RPROV_01_verified_supports_derives_supported_from_r27a(self):
        """RPROV-01：verified + supports → supported，且 status 来自 R27-A。"""
        event = _event()
        hypothesis = self._relations({
            "thesis": "momentum 延续", "confidence": 0.73,
            "evidence_relations": [{"evidence_id": event.evidence_id, "relation": "supports"}],
            "narrative": "报价经双源核验且方向一致", "counter_arguments": [],
        }, events=(event,))

        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, hypothesis.status)
        self.assertIsNone(hypothesis.reason)
        # 证明这个 status 是 R27-A 派生的，而不是 adapter 写进去的：
        # 同一个 evidence + relation 交给契约本身，必须得到同一结论。
        mirrored = ARC.ResearchHypothesis(
            hypothesis_id="H-1", as_of=DAY, subject=CODE, thesis="x",
            evidence=(ARC.HypothesisEvidence(ref=event.evidence_ref, relation="supports"),),
            confidence=0.0,
        )
        self.assertEqual(mirrored.status, hypothesis.status)
        self.assertFalse(hypothesis.is_authoritative)

    def test_RPROV_02_verified_context_is_no_supporting_evidence(self):
        """RPROV-02：verified + context → insufficient_evidence / no_supporting_evidence。"""
        event = _event()
        hypothesis = self._relations({
            "thesis": "t", "confidence": 0.5,
            "evidence_relations": [{"evidence_id": event.evidence_id, "relation": "context"}],
        }, events=(event,))

        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
        self.assertEqual(ARC.RESEARCH_REASON_NO_SUPPORTING_EVIDENCE, hypothesis.reason)

    def test_RPROV_03_single_source_never_upgrades_to_verified(self):
        """RPROV-03：single_source + provider supports **仍然** single_source。"""
        event = _event(validation=SINGLE_SOURCE)
        self.assertEqual(MDC.VERIFICATION_SINGLE_SOURCE, event.verification)
        hypothesis = self._relations({
            "thesis": "t", "confidence": 0.9,
            "evidence_relations": [{"evidence_id": event.evidence_id, "relation": "supports"}],
        }, events=(event,))

        self.assertEqual(MDC.VERIFICATION_SINGLE_SOURCE, hypothesis.evidence[0].ref.verification)
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
        self.assertEqual(ARC.RESEARCH_REASON_EVIDENCE_NOT_VERIFIED, hypothesis.reason)
        self.assertFalse(hypothesis.is_supported, "provider 不得把单源升级成 supported")

    def test_RPROV_03b_ref_is_the_identical_input_object(self):
        """RPROV-03b：``HypothesisEvidence.ref`` 直接复用输入 ref（不复制、不重建）。"""
        event = _event()
        hypothesis = self._relations({
            "thesis": "t", "confidence": 0.5,
            "evidence_relations": [{"evidence_id": event.evidence_id, "relation": "supports"}],
        }, events=(event,))

        self.assertIs(event.evidence_ref, hypothesis.evidence[0].ref)

    def test_RPROV_03c_coverage_integrity_verification_survives_unchanged(self):
        """RPROV-03c：coverage_integrity 的 verified 逐字保留，不被改写成 cross_source。"""
        snapshot = MDC.MarketDataSnapshot(
            kind="symbol_quote",
            rows=({"code": CODE, "price": 10.5, "quote_at": f"{DAY}T10:30:00+08:00"},),
            as_of=DAY, observed_at=f"{DAY}T10:30:00+08:00", source="eastmoney", complete=True,
            expected_rows=1, verification=MDC.VERIFICATION_VERIFIED,
            verification_method=MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY,
        )
        reading = MDC.classify(snapshot, MDC.policy_named(DEFAULT_POLICY),
                               now=f"{DAY}T10:30:00+08:00", asof_day=DAY)
        event = ARC.InformationEvent(
            as_of=DAY, source="market_data_service",
            evidence_ref=ARC.evidence_ref_from_market_reading(reading),
        )
        hypothesis = self._relations({
            "thesis": "t", "confidence": 0.5,
            "evidence_relations": [{"evidence_id": event.evidence_id, "relation": "supports"}],
        }, events=(event,))

        ref = hypothesis.evidence[0].ref
        self.assertEqual(MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY, ref.verification_method)
        self.assertEqual(MDC.VERIFICATION_VERIFIED, ref.verification)
        # 不是逐票双源 —— 必须由 R24 判据回答，而不是比较 verification 字符串
        self.assertFalse(ref.cross_source_verified)

    def test_RPROV_04_unknown_evidence_id_fails_closed(self):
        """RPROV-04：provider 引用不存在的 evidence_id → fail closed。"""
        event = _event()
        with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
            self.run_research({
                "thesis": "t", "confidence": 0.5,
                "evidence_relations": [
                    {"evidence_id": "HALLUCINATED_FACT_123", "relation": "supports"},
                ],
            }, events=(event,))
        self.assertEqual(P.REASON_UNKNOWN_EVIDENCE_ID, ctx.exception.reason)

    def test_RPROV_05_invalid_relation_fails_closed(self):
        """RPROV-05：非法 relation → fail closed。"""
        event = _event()
        for relation in ("strong_support", "neutral", "SUPPORTS", "", 1, None):
            with self.subTest(relation=relation):
                with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
                    self.run_research({
                        "thesis": "t", "confidence": 0.5,
                        "evidence_relations": [
                            {"evidence_id": event.evidence_id, "relation": relation},
                        ],
                    }, events=(event,))
                self.assertEqual(P.REASON_INVALID_RELATION, ctx.exception.reason)

    def test_RPROV_06_provider_declaring_status_fails_closed(self):
        """RPROV-06：provider 输出 ``status="supported"`` → fail closed，不忽略。"""
        event = _event()
        with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
            self.run_research({
                "thesis": "t", "confidence": 0.5, "status": "supported",
                "evidence_relations": [{"evidence_id": event.evidence_id, "relation": "supports"}],
            }, events=(event,))
        self.assertEqual(P.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)

    def test_RPROV_07_provider_declaring_verification_fails_closed(self):
        """RPROV-07：provider 输出 ``verification="verified"`` → fail closed。"""
        for field in ("verification", "verification_method", "source_type", "source_id", "as_of"):
            with self.subTest(field=field):
                with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
                    self.run_research({"thesis": "t", "confidence": 0.5, field: "verified"})
                self.assertEqual(P.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)

    def test_RPROV_08_provider_declaring_authority_fails_closed(self):
        """RPROV-08：provider 输出 ``authority="signal"`` → fail closed。"""
        for value in ("signal", "approved", True):
            with self.subTest(value=value):
                with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
                    self.run_research({"thesis": "t", "confidence": 0.5, "authority": value})
                self.assertEqual(P.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)
        with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
            self.run_research({"thesis": "t", "confidence": 0.5, "is_authoritative": True})
        self.assertEqual(P.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)

    def test_RPROV_08b_unknown_provider_fields_fail_closed(self):
        """RPROV-08b：schema 之外的新字段也一律拒绝（严格协议，不是白名单漂移）。"""
        with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
            self.run_research({"thesis": "t", "confidence": 0.5, "trading_signal": "buy"})
        self.assertEqual(P.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)

    def test_RPROV_08c_authority_rejection_does_not_rest_on_a_single_guard(self):
        """RPROV-08c：authority 字段被**两道独立防线**拒绝，不是靠单一守卫。

        显式 ``AUTHORITY_FIELDS`` 检查给出可审计的原因；严格 allowlist 则保证即使
        有人往 ``_ALLOWED_PROVIDER_FIELDS`` 里加字段，authority 字段依然进不来。
        行为证明：绕过显式检查（直接把 id 混进允许集合）也必须继续被拒绝 ——
        这把"两道防线都活着"变成一条永久回归，而不是一次性的 mutation 结论。
        """
        import ai_research_provider as _P

        schema = _P._ALLOWED_PROVIDER_FIELDS
        # 反向证明：字段确实被显式枚举为 authority 字段
        for field in ("status", "reason", "authority", "is_authoritative",
                      "verification", "verification_method", "source_type",
                      "source_id", "as_of"):
            with self.subTest(field=field):
                self.assertIn(field, _P.AUTHORITY_FIELDS)
                self.assertNotIn(field, schema, "authority 字段不得出现在允许列表里")

        # 只关掉显式守卫：allowlist 必须**独立**继续拒绝
        with mock.patch.object(_P, "AUTHORITY_FIELDS", frozenset()):
            with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
                self.run_research({"thesis": "t", "confidence": 0.5, "status": "supported"})
        self.assertEqual(P.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)

    def test_RPROV_09_confidence_must_be_a_fraction_in_unit_interval(self):
        """RPROV-09：``confidence=73`` → fail closed；``0.73`` → PASS。"""
        for bad in (73, -0.2, 1.5, True, "0.8", None, [0.5]):
            with self.subTest(confidence=bad):
                with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
                    self.run_research({"thesis": "t", "confidence": bad})
                self.assertEqual(P.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)

        result = self.run_research({"thesis": "t", "confidence": 0.73})
        self.assertEqual(0.73, result.hypothesis.confidence)

    def test_RPROV_10_future_evidence_fails_before_any_network_call(self):
        """RPROV-10：event.as_of > 请求 as_of → 在 transport 被调用前失败。

        ``network_calls == 0`` 是关键断言：绝不为一次注定 look-ahead 的研究先付费。
        """
        future = _event(as_of=NEXT_DAY)
        with self.patch_urlopen(_reply({"thesis": "t", "confidence": 0.5})):
            with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
                P.run_research(
                    provider_config=dict(CONFIG), hypothesis_id="H-1", as_of=DAY,
                    subject=CODE, question="q", events=(future,),
                )
        self.assertEqual(P.REASON_LOOK_AHEAD_EVIDENCE, ctx.exception.reason)
        self.assertEqual(0, self.network_calls, "look-ahead 必须在网络请求之前被拒绝")

    def test_RPROV_11_identical_duplicate_events_are_safely_deduplicated(self):
        """RPROV-11：同一 evidence_id 且内容完全相同 → 安全去重。"""
        event = _event()
        same = _event()
        self.assertEqual(event.evidence_id, same.evidence_id)

        with self.patch_urlopen(_reply({"thesis": "t", "confidence": 0.5})):
            result = P.run_research(
                provider_config=dict(CONFIG), hypothesis_id="H-1", as_of=DAY, subject=CODE,
                question="q", events=(event, same),
            )
        self.assertEqual(1, self.network_calls)
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, result.hypothesis.status)

    def test_RPROV_11b_the_provider_sees_each_evidence_id_once(self):
        """RPROV-11b：去重后 provider 只看到一份该 evidence。"""
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse(_reply({"thesis": "t", "confidence": 0.5}))

        with mock.patch.object(T.urllib.request, "urlopen", side_effect=fake_urlopen):
            P.run_research(
                provider_config=dict(CONFIG), hypothesis_id="H-1", as_of=DAY, subject=CODE,
                question="q", events=(_event(), _event()),
            )
        user_prompt = captured["body"]["messages"][1]["content"]
        evidence_blob = user_prompt.split("evidence=", 1)[1]
        self.assertEqual(1, evidence_blob.count(_event().evidence_id))

    def test_RPROV_12_same_id_with_different_content_fails_closed(self):
        """RPROV-12：同 evidence_id 但内容不同 → fail closed（不 first-wins）。"""
        first = _event()
        changed = _event(payload={"price": 99.9, "kind": "symbol_quote"})
        self.assertEqual(first.evidence_id, changed.evidence_id)
        with self.assertRaises(ARC.EvidenceConflict):
            self.run_research({"thesis": "t", "confidence": 0.5}, events=(first, changed))

        # 顺序无关：反序必须同样失败
        with self.assertRaises(ARC.EvidenceConflict):
            self.run_research({"thesis": "t", "confidence": 0.5}, events=(changed, first))

    def test_RPROV_13_conflicting_relations_surface_as_relation_conflict(self):
        """RPROV-13：同一 evidence 返回 supports + contradicts → EvidenceRelationConflict。

        adapter 不得 first-wins：这个矛盾必须由 R27-A 裁决。
        """
        event = _event()
        with self.assertRaises(ARC.EvidenceRelationConflict):
            self.run_research({
                "thesis": "t", "confidence": 0.5,
                "evidence_relations": [
                    {"evidence_id": event.evidence_id, "relation": "supports"},
                    {"evidence_id": event.evidence_id, "relation": "contradicts"},
                ],
            }, events=(event,))

    def test_RPROV_14_no_evidence_is_never_supported(self):
        """RPROV-14：provider 不引用任何 evidence → insufficient_evidence / no_evidence。"""
        for content in (
            {"thesis": "t", "confidence": 0.99},
            {"thesis": "t", "confidence": 0.99, "evidence_relations": []},
        ):
            with self.subTest(content=content):
                hypothesis = self._relations(content)
                self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
                self.assertEqual(ARC.RESEARCH_REASON_NO_EVIDENCE, hypothesis.reason)
                self.assertFalse(hypothesis.is_supported)

    def test_RPROV_15_hypothesis_identity_comes_only_from_the_caller(self):
        """RPROV-15：hypothesis_id / as_of / subject 全部以 caller 为准。"""
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse(_reply({
                "thesis": "t", "confidence": 0.5,
                "hypothesis_id": "H-PROVIDER", "subject": "999999", "as_of": NEXT_DAY,
            }))

        # provider 试图改写身份字段：这些字段不在允许列表里 → fail closed，
        # 而不是被静默忽略（静默忽略会让"谁定义了身份"变得不可审计）。
        with mock.patch.object(T.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
                P.run_research(
                    provider_config=dict(CONFIG), hypothesis_id="H-CALLER", as_of=DAY,
                    subject="600000", question="q", events=(_event(),),
                )
        self.assertEqual(P.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)

        # 正常路径下身份完全来自 caller
        with self.patch_urlopen(_reply({"thesis": "t", "confidence": 0.5})):
            result = P.run_research(
                provider_config=dict(CONFIG), hypothesis_id="H-CALLER", as_of=DAY,
                subject="600000", question="q", events=(_event(),),
            )
        self.assertEqual("H-CALLER", result.hypothesis.hypothesis_id)
        self.assertEqual(DAY, result.hypothesis.as_of)
        self.assertEqual("600000", result.hypothesis.subject)

    def test_RPROV_16_cross_source_status_is_delegated_not_inferred(self):
        """RPROV-16：adapter 不比较 ``verification == "verified"`` 推断双源。

        行为证明：一条 method=coverage_integrity 的 verified 事实，
        provider 声明 supports，最终 status 是 supported（因为 R27-A 的判定只看
        ``verification``），但 ``cross_source_verified`` 必须如实为 ``False`` ——
        说明"是否真的双源"这条语义被委托给了 R24 判据，没有被 adapter 压平。
        """
        # 静态：源码里不存在"把 verification 与 verified 字面量比较"的比较表达式。
        # 按 AST 判定而非子串搜索 —— docstring 里解释"不要比较 verified"不该变红。
        compares = [
            node for node in ast.walk(_tree(RESEARCH_MODULE))
            if isinstance(node, ast.Compare)
            and any(isinstance(c, ast.Constant) and c.value == "verified"
                    for c in [node.left, *node.comparators])
        ]
        self.assertEqual([], compares, "adapter 自行比较了 verified 字面量")

        # 行为：coverage_integrity 的 verified 事实，其 cross_source_verified 如实为 False
        snapshot = MDC.MarketDataSnapshot(
            kind="symbol_quote",
            rows=({"code": CODE, "price": 10.5, "quote_at": f"{DAY}T10:30:00+08:00"},),
            as_of=DAY, observed_at=f"{DAY}T10:30:00+08:00", source="eastmoney", complete=True,
            expected_rows=1, verification=MDC.VERIFICATION_VERIFIED,
            verification_method=MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY,
        )
        reading = MDC.classify(snapshot, MDC.policy_named(DEFAULT_POLICY),
                               now=f"{DAY}T10:30:00+08:00", asof_day=DAY)
        event = ARC.InformationEvent(
            as_of=DAY, source="market_data_service",
            evidence_ref=ARC.evidence_ref_from_market_reading(reading),
        )
        hypothesis = self._relations({
            "thesis": "t", "confidence": 0.5,
            "evidence_relations": [{"evidence_id": event.evidence_id, "relation": "supports"}],
        }, events=(event,))

        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, hypothesis.status)
        self.assertTrue(hypothesis.evidence[0].is_verified)
        self.assertFalse(
            hypothesis.evidence[0].ref.cross_source_verified,
            "coverage_integrity 不是逐票双源 —— 判据必须来自 R24",
        )

    def test_RPROV_16b_contradicting_evidence_yields_unsupported(self):
        """RPROV-16b：verified + contradicts → unsupported（同样由 R27-A 派生）。"""
        event = _event()
        hypothesis = self._relations({
            "thesis": "t", "confidence": 0.2,
            "evidence_relations": [{"evidence_id": event.evidence_id, "relation": "contradicts"}],
        }, events=(event,))
        self.assertEqual(ARC.HYPOTHESIS_UNSUPPORTED, hypothesis.status)
        self.assertEqual(ARC.RESEARCH_REASON_EVIDENCE_CONTRADICTED, hypothesis.reason)

    def test_RPROV_17_result_carries_narrative_and_usage_without_being_authority(self):
        """RPROV-17：result 携带叙述与用量，但 authority 只在 hypothesis 上。"""
        event = _event()
        result = self.run_research({
            "thesis": "momentum 延续", "confidence": 0.61,
            "evidence_relations": [{"evidence_id": event.evidence_id, "relation": "supports"}],
            "narrative": "报价双源一致", "counter_arguments": ["成交量未确认"],
        }, events=(event,))

        self.assertEqual("报价双源一致", result.narrative)
        self.assertEqual(("成交量未确认",), result.counter_arguments)
        self.assertEqual("model-one", result.model)
        self.assertEqual(11, result.input_tokens)
        self.assertEqual(7, result.output_tokens)
        self.assertFalse(result.hypothesis.is_authoritative)
        self.assertEqual("research", result.hypothesis.authority)

    def test_RPROV_18_output_length_bounds_are_enforced(self):
        """RPROV-18：畸形超长返回被硬上界拒绝。"""
        cases = (
            ({"thesis": "x" * (P.MAX_THESIS_CHARS + 1), "confidence": 0.5}, "thesis"),
            ({"thesis": "t", "confidence": 0.5, "narrative": "x" * (P.MAX_NARRATIVE_CHARS + 1)},
             "narrative"),
            ({"thesis": "t", "confidence": 0.5,
              "counter_arguments": ["x"] * (P.MAX_COUNTER_ARGUMENTS + 1)}, "counter_arguments"),
            ({"thesis": "t", "confidence": 0.5,
              "counter_arguments": ["x" * (P.MAX_COUNTER_ARGUMENT_CHARS + 1)]}, "counter_argument"),
        )
        for content, label in cases:
            with self.subTest(label=label):
                with self.assertRaises(P.ResearchProviderProtocolError) as ctx:
                    self.run_research(content)
                self.assertEqual(P.REASON_INVALID_PROVIDER_RESPONSE, ctx.exception.reason)

    def test_RPROV_19_input_must_be_typed_events_not_dicts_or_strings(self):
        """RPROV-19：dict / raw string 不得冒充 evidence。"""
        for fake in ({"source_id": "xxx", "verification": "verified"}, "source_id=xxx", None, 42):
            with self.subTest(fake=type(fake).__name__):
                with self.assertRaises(TypeError):
                    P.run_research(
                        provider_config=dict(CONFIG), hypothesis_id="H-1", as_of=DAY,
                        subject=CODE, question="q", events=(fake,),
                    )

    def test_RPROV_20_unprovable_as_of_is_rejected_before_the_network(self):
        """RPROV-20：无法证明的业务日拒绝，且不发出网络请求。"""
        for bad in ("", "   ", "not-a-day"):
            with self.subTest(as_of=bad):
                with self.patch_urlopen(_reply({"thesis": "t", "confidence": 0.5})):
                    with self.assertRaises(ValueError):
                        P.run_research(
                            provider_config=dict(CONFIG), hypothesis_id="H-1", as_of=bad,
                            subject=CODE, question="q", events=(_event(),),
                        )
                self.assertEqual(0, self.network_calls)

    def test_RPROV_21_evidence_payload_is_shown_to_the_model(self):
        """RPROV-21：LLM 看到事实内容（payload），而不只是引用。"""
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse(_reply({"thesis": "t", "confidence": 0.5}))

        event = _event(payload={"price": 12.34, "note": "observed"})
        with mock.patch.object(T.urllib.request, "urlopen", side_effect=fake_urlopen):
            P.run_research(
                provider_config=dict(CONFIG), hypothesis_id="H-1", as_of=DAY, subject=CODE,
                question="q", events=(event,),
            )
        user_prompt = captured["body"]["messages"][1]["content"]
        self.assertIn("12.34", user_prompt)
        self.assertIn(event.evidence_id, user_prompt)
        self.assertIn("cross_source_verified", user_prompt)
        # 事实维度一并交给模型阅读，但这些字段**不接受**它回写
        self.assertIn("verification_method", user_prompt)

    def test_RPROV_22_system_prompt_declares_the_data_not_instructions_boundary(self):
        """RPROV-22：system prompt 明确 evidence 是数据而非指令。"""
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse(_reply({"thesis": "t", "confidence": 0.5}))

        with mock.patch.object(T.urllib.request, "urlopen", side_effect=fake_urlopen):
            P.run_research(
                provider_config=dict(CONFIG), hypothesis_id="H-1", as_of=DAY, subject=CODE,
                question="q", events=(_event(),),
            )
        system = captured["body"]["messages"][0]["content"]
        for token in ("数据", "不是指令", "evidence_id", "market fact",
                      "不允许输出交易指令", "status", "verification"):
            with self.subTest(token=token):
                self.assertIn(token, system)
        # 但真正的边界是 parser，不是 prompt 长度
        self.assertLess(len(system), 4000)


# ─────────────────────────────────────────────────────────────────────────────
# RG-01 ~ 06 —— architecture guard
# ─────────────────────────────────────────────────────────────────────────────


def _source(name):
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


def _tree(name):
    return ast.parse(_source(name))


def _imported_roots(tree):
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_names(tree):
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _called_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _code_string_constants(tree):
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


RESEARCH_MODULE = "ai_research_provider.py"
TRANSPORT_MODULE = "ai_provider_transport.py"

ALLOWED_RESEARCH_IMPORTS = {
    "__future__", "collections", "dataclasses", "typing", "json",
    "ai_research_contract", "ai_provider_transport",
}

FORBIDDEN_RESEARCH_IMPORTS = {
    "market_data_service", "data_fetcher", "signal_service", "strategy_registry",
    "execution_planner", "execution_evidence", "execution_dispatch",
    "execution_lifecycle", "execution_verification", "execution_outcome",
    "manual_orders", "paper_trading", "paper_repository", "paper_storage",
    "paper_risk_service", "paper_risk_decision", "risk_center",
    "promotion_science", "self_evolution", "evolution_apply", "strategy_champion",
    "ai_review_service", "dual_ai_tuner", "adaptive_engine", "deepseek_advisor",
    "sqlite3", "requests", "urllib", "httpx", "socket", "os", "pathlib", "subprocess",
}

AUTHORITY_MODULES = (
    "market_data_contract.py", "market_data_service.py", "data_fetcher.py",
    "signal_service.py", "strategy_selection_resolver.py", "strategy_selection_provenance.py",
    "execution_planner.py", "execution_evidence.py", "execution_dispatch.py",
    "execution_lifecycle.py", "execution_verification.py", "execution_outcome.py",
    "manual_orders.py", "paper_risk_service.py", "paper_risk_decision.py",
    "paper_risk_evidence.py", "paper_risk_exit_eligibility.py", "paper_risk_scan_state.py",
    "risk_center.py", "paper_trading.py", "paper_repository.py", "paper_storage.py",
    "paper_schema_migrations.py", "paper_capital_reservations.py",
    "promotion_science.py", "self_evolution.py", "evolution_apply.py", "strategy_champion.py",
)


class AiResearchProviderArchitectureGuardTests(unittest.TestCase):
    def test_RG_01_research_provider_imports_only_the_sanctioned_layers(self):
        """RG-01：adapter 只 import stdlib + 两个 sanctioned 模块。"""
        roots = _imported_roots(_tree(RESEARCH_MODULE))
        project_modules = {path[:-3] for path in os.listdir(BACKEND) if path.endswith(".py")}

        leaked = sorted((roots & project_modules) - {"ai_research_contract", "ai_provider_transport"})
        self.assertEqual(
            [], leaked,
            f"{RESEARCH_MODULE} import 了未登记的项目模块 {leaked}",
        )
        self.assertEqual(
            [], sorted(roots & FORBIDDEN_RESEARCH_IMPORTS),
            f"{RESEARCH_MODULE} import 了被禁止的模块",
        )
        self.assertEqual(
            [], sorted(roots - ALLOWED_RESEARCH_IMPORTS),
            "出现了未登记的 import，请审慎评估后再放行",
        )

    def test_RG_02_research_provider_owns_no_io_no_db_no_network(self):
        """RG-02：adapter 无 sqlite3 / DB writer / 文件系统 / getenv / 直接网络。"""
        tree = _tree(RESEARCH_MODULE)
        roots = _imported_roots(tree)
        for forbidden in ("sqlite3", "os", "pathlib", "subprocess", "logging",
                          "urllib", "requests", "httpx", "socket"):
            with self.subTest(module=forbidden):
                self.assertNotIn(forbidden, roots, f"{RESEARCH_MODULE} import 了 {forbidden}")

        called = _called_names(tree)
        for forbidden in ("urlopen", "Request", "urlretrieve", "socket",
                          "getenv", "environ", "connect", "execute", "executemany",
                          "executescript", "commit", "rollback", "open", "write"):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, called, f"{RESEARCH_MODULE} 调用了 {forbidden}()")

    def test_RG_03_real_network_calls_live_only_in_the_transport(self):
        """RG-03：真实网络调用只出现在 ai_provider_transport。"""
        transport_tree = _tree(TRANSPORT_MODULE)
        self.assertIn("urllib", _imported_roots(transport_tree))
        self.assertIn("urlopen", _called_names(transport_tree))

        research_source = _source(RESEARCH_MODULE)
        for token in ("urllib", "urlopen", "requests.", "httpx", "socket."):
            with self.subTest(token=token):
                self.assertNotIn(token, research_source,
                                 f"{RESEARCH_MODULE} 直接持有网络调用")

    def test_RG_04_no_authority_module_reverse_imports_ai(self):
        """RG-04：authority → AI 的依赖必须为 0。"""
        offenders = []
        for name in AUTHORITY_MODULES:
            for imported in _imported_names(_tree(name)):
                if imported.split(".")[0] in ("ai_research_contract", "ai_research_provider",
                                              "ai_provider_transport"):
                    offenders.append(f"{name}: import {imported}")
        self.assertEqual(
            [], offenders,
            "authority 反向 import 了 AI 层 —— AI 必须是纯消费者",
        )

    def test_RG_05_the_adapter_is_the_only_new_registered_consumer(self):
        """RG-05：唯一新增的 research-contract 生产消费者是 ai_research_provider.py。"""
        offenders = []
        for name in sorted(os.listdir(BACKEND)):
            if not name.endswith(".py") or name == "ai_research_contract.py":
                continue
            if name.startswith("test_"):
                continue
            for imported in _imported_names(_tree(name)):
                if imported.split(".")[0] == "ai_research_contract":
                    offenders.append(name)
        self.assertEqual(
            sorted(set(offenders)), [RESEARCH_MODULE],
            f"research contract 的生产消费者集合发生变化：{sorted(set(offenders))}",
        )

    def test_RG_06_research_provider_owns_no_ledger_or_apply_path(self):
        """RG-06：adapter 源码不含写入 / 应用词汇。"""
        source = _source(RESEARCH_MODULE)
        for token in ("INSERT", "UPDATE", "DELETE",
                      "paper_signals", "paper_orders", "paper_fills",
                      "commit_signal", "apply_tuner_proposals"):
            with self.subTest(token=token):
                self.assertNotIn(token, source, f"{RESEARCH_MODULE} 出现了 {token}")

        for text in _code_string_constants(_tree(RESEARCH_MODULE)):
            upper = text.upper()
            for keyword in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
                with self.subTest(keyword=keyword):
                    self.assertNotIn(keyword, upper)

    def test_RG_07_guards_are_not_vacuously_passing(self):
        """RG-07：护栏本身必须真的能失败。"""
        self.assertIn("ai_research_contract",
                      _imported_roots(ast.parse("import ai_research_contract\n")))
        self.assertIn("urlopen",
                      _called_names(ast.parse("import urllib.request\nurllib.request.urlopen(r)\n")))
        self.assertIn("connect",
                      _called_names(ast.parse("def f(c):\n    c.connect()\n")))
        self.assertTrue(any("INSERT INTO" in text.upper()
                            for text in _code_string_constants(
                                ast.parse("SQL = 'INSERT INTO paper_signals(a) VALUES(?)'\n"))))
        self.assertEqual([], _code_string_constants(
            ast.parse('"""Never INSERT INTO paper_signals here."""\nX = 1\n')))

    def test_RG_08_no_second_provider_config_or_network_owner_is_introduced(self):
        """RG-08：R27-B1 provider 链路内没有第二个网络 owner，也没有第三套配置。"""
        import ai_review_service as S

        # adapter 复用既有槽位配置形状，不新增 API Key 来源
        self.assertEqual(("ai1", "ai2"), S.AI_SLOTS)
        research_source = _source(RESEARCH_MODULE)
        for token in ("RESEARCH_API_KEY", "OPENAI_API_KEY", "api_key = os"):
            with self.subTest(token=token):
                self.assertNotIn(token, research_source)

        # 本轮 provider 链路里，真实 urlopen 只允许出现在 transport。
        # 链路 = R27-A 契约 + transport + adapter + 已完成迁移的 ai_review_service。
        # 历史遗留模块（deepseek_advisor / deepseek_research / ai_analysis /
        # disclosure_timeline / main）本轮**刻意不动**，留给 R27-B2；它们不在本
        # 断言的范围内，否则这条 guard 会变成"顺手重写历史"的借口。
        chain = (TRANSPORT_MODULE, RESEARCH_MODULE, "ai_research_contract.py",
                 "ai_review_service.py")
        owners = [name for name in chain if "urlopen" in _called_names(_tree(name))]
        self.assertEqual([TRANSPORT_MODULE], owners,
                         f"provider 链路里网络 owner 不再唯一：{owners}")

        # 反向：transport 必须真的持有那一次调用（guard 不得空转）
        self.assertIn("urlopen", _called_names(_tree(TRANSPORT_MODULE)))


if __name__ == "__main__":
    unittest.main()
