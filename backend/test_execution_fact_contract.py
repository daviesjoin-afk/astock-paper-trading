# -*- coding: utf-8 -*-
"""R27-B2C-1 —— execution owner fact contract 的回归。

存在理由是这条不变量：

    **一条 execution 事实的身份、业务日与核验结论由 owner 发布，不由消费者拼。**

分五组：

    EXFACT-01 ~ 03   filled fact 的投影完整；核验是 owner-native 词表，不是 market 词表
    EXFACT-04        没有 owner 记录的业务日时如实报 unknown（**不**用墙钟日期冒充）
    EXFACT-05 ~ 07   多成交身份、投影不可变且不含 research 语义、调用方无法自述
    EXFACT-08        非法词表 / 非法组合 / 版本不符 / 字段类型错一律 fail closed
    EXFACT-09 ~ 11   identity 真的从读路径可达；信号唯一来源仍是 execution_verification；
                     本契约按"新增独立投影"落地，没有改动 ExecutionEvidence 的字段集

全部离线：只用 ``evidence_from_order`` 的纯函数路径，不连数据库。
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import execution_evidence as EE  # noqa: E402
import execution_verification as EV  # noqa: E402
import paper_trading_rules as PTR  # noqa: E402

DAY = "2026-08-27"
OBSERVED_AT = f"{DAY}T10:30:00+08:00"
CREATED_AT = f"{DAY} 10:29:00"
KEY_A = "a" * 64
KEY_B = "b" * 64

#: 用**生产费率模型**算手续费：随手写一个数会命中
#: ``INCONSISTENCY_FEES_NOT_RECONCILED``，于是来源变成 ``evidence_inconsistent``，
#: 那条用例就不再是在测"干净的账本证据"。
AMOUNT = 100 * 10.5
FEES = PTR.commission(AMOUNT)


def _fill(**overrides):
    row = {
        "account_id": "acct", "side": "buy", "code": "600001", "qty": 100,
        "price": 10.5, "amount": AMOUNT, "fees": FEES,
        "fill_date": DAY, "quote_at": OBSERVED_AT,
    }
    row.update(overrides)
    return row


def _order(**overrides):
    row = {
        "id": 7, "account_id": "acct", "side": "buy", "code": "600001", "qty": 100,
        "planned_price": 10.4, "status": "filled", "reason": "", "created_at": CREATED_AT,
        "order_type": "market",
    }
    row.update(overrides)
    return row


def _evidence(order=None, fills=(), **kwargs):
    return EE.evidence_from_order(order or _order(), fills, **kwargs)


def _projection(order=None, fills=(), **kwargs):
    return EV.fact_projection(_evidence(order, fills, **kwargs))


class OwnerNativeVerificationTests(unittest.TestCase):
    def test_EXFACT_01_a_filled_fact_publishes_identity_day_and_verification(self):
        """EXFACT-01：一条被账本证明成交的事实，四件事都由 owner 给出。"""
        evidence = _evidence(fills=(_fill(event_key=KEY_A),))
        projection = EV.fact_projection(evidence)

        self.assertEqual(EV.EXECUTION_FACT_CONTRACT_VERSION, projection.version)
        self.assertEqual(KEY_A, projection.identity)
        self.assertEqual(EV.IDENTITY_KIND_FILL_EVENT_KEY, projection.identity_kind)
        self.assertTrue(projection.business_day.is_known)
        self.assertEqual(DAY, projection.business_day.require())
        self.assertTrue(projection.observed_at.is_known)
        self.assertEqual(OBSERVED_AT, projection.observed_at.require())

        verification = projection.verification
        self.assertEqual(EV.EXECUTION_VERIFICATION_SCOPE, verification["verification_scope"])
        self.assertEqual(EV.EXECUTION_VERIFICATION_VERSION, verification["verification_version"])
        self.assertEqual(EV.EXECUTION_STATUS_VERIFIED, verification["verification_status"])
        self.assertEqual(EV.EVIDENCE_SOURCE_LEDGER, verification["verification_source"])
        self.assertTrue(verification["is_verified"])
        # 业务日与观测时点是**两个**维度：一个交易日里可以有多个观测时点。
        self.assertNotEqual(
            projection.business_day.require(), projection.observed_at.require(),
        )

    def test_EXFACT_02_verification_contract_rejects_unknown_words_and_impossible_pairs(self):
        """EXFACT-02：词表与组合都 fail closed，不做静默降级。"""
        # 合法组合全部可用（穷尽表本身要真的是穷尽的）。
        for status in EV.EXECUTION_STATUSES:
            with self.subTest(status=status):
                declared = EV.verification_contract(status, EV.EVIDENCE_SOURCE_LEDGER)
                self.assertEqual(status, declared["verification_status"])
                self.assertEqual(EV.is_verified_status(status), declared["is_verified"])

        for status, source, reason in (
            ("confirmed", EV.EVIDENCE_SOURCE_LEDGER, "unknown_verification_status"),
            (EV.EXECUTION_STATUS_VERIFIED, "trust_me", "unknown_evidence_source"),
            (EV.EXECUTION_STATUS_VERIFIED, EV.EVIDENCE_SOURCE_ABSENT,
             "illegal_verification_pair"),
            (EV.EXECUTION_STATUS_PARTIAL, EV.EVIDENCE_SOURCE_LEGACY,
             "illegal_verification_pair"),
        ):
            with self.subTest(status=status, source=source):
                with self.assertRaises(EV.ExecutionFactContractError) as caught:
                    EV.verification_contract(status, source)
                self.assertEqual(reason, caught.exception.reason)

    def test_EXFACT_03_the_contract_is_owner_native_not_market_vocabulary(self):
        """EXFACT-03：这是 execution 自己的核验词表，不是 market 的复制品。

        把 market 的 ``(verification, verification_method)`` 语义抄进来，会让
        "行情做了双源核验"与"账本证明了成交"变成同一句话 —— 正是 R27-B2C 禁止的方向。
        """
        projection = _projection(fills=(_fill(event_key=KEY_A),))
        declared = projection.verification
        # 形态上就不是 market 那一对。
        self.assertNotIn("verification_method", declared)
        self.assertNotIn("cross_source_verified", declared)
        # 取值上也只能是 execution 的词。
        self.assertIn(declared["verification_status"], EV.EXECUTION_STATUSES)
        self.assertIn(declared["verification_source"], EV.EVIDENCE_SOURCES)
        # market 的词进不来。
        for alien in ("cross_source", "coverage_integrity", "single_source"):
            with self.subTest(alien=alien):
                with self.assertRaises(EV.ExecutionFactContractError):
                    EV.verification_contract(alien, EV.EVIDENCE_SOURCE_LEDGER)
                with self.assertRaises(EV.ExecutionFactContractError):
                    EV.verification_contract(EV.EXECUTION_STATUS_VERIFIED, alien)


class PitHonestyTests(unittest.TestCase):
    def test_EXFACT_04_no_owner_recorded_business_day_is_reported_unknown(self):
        """EXFACT-04：没有 owner 记录的业务日就报 unknown，绝不用墙钟日期冒充。

        ``paper_orders`` **没有**交易日列（只有 ``created_at`` 墙钟），因此被拒 / 被撤
        的委托今天没有可证明的业务日。这是 R27-B2C-1 明确记录的下一步前置条件 ——
        契约要**报告**它，而不是把 ``created_at[:10]`` 当成交易日。
        """
        for status, reason in (("risk_rejected", "risk"), ("cancelled", "user cancel")):
            with self.subTest(status=status):
                projection = _projection(_order(status=status, reason=reason), fills=())
                self.assertTrue(projection.business_day.is_unknown)
                self.assertTrue(projection.observed_at.is_unknown)
                self.assertEqual(EV.IDENTITY_KIND_ORDER_ONLY, projection.identity_kind)
                self.assertEqual("order:7", projection.identity)
                # 缺口必须可见：detail 说明"owner 没有记录"，而不是空着。
                self.assertIn("no ", str(projection.business_day.detail or ""))
                # 报 unknown 也**不**等于"确认没发生"。
                self.assertFalse(projection.business_day.is_not_applicable)

        # 跨业务日的成交同样不挑代表值。
        cross = _projection(fills=(
            _fill(event_key=KEY_A, fill_date="2026-08-26"),
            _fill(event_key=KEY_B, fill_date=DAY),
        ))
        self.assertTrue(cross.business_day.is_unknown)
        self.assertIn("2026-08-26", str(cross.business_day.detail))
        self.assertIn(DAY, str(cross.business_day.detail))


class ProjectionShapeTests(unittest.TestCase):
    def test_EXFACT_05_multiple_fills_get_a_set_identity(self):
        """EXFACT-05：一笔委托的多次成交 → 身份是**成交集合**，并如实标注。"""
        projection = _projection(fills=(
            _fill(event_key=KEY_B), _fill(event_key=KEY_A),
        ))
        self.assertEqual(EV.IDENTITY_KIND_FILL_EVENT_KEY_SET, projection.identity_kind)
        self.assertIn(KEY_A, projection.identity)
        self.assertIn(KEY_B, projection.identity)
        # 顺序无关：集合身份不能依赖读入顺序。
        reversed_projection = _projection(fills=(
            _fill(event_key=KEY_A), _fill(event_key=KEY_B),
        ))
        self.assertEqual(projection.identity, reversed_projection.identity)

    def test_EXFACT_06_projection_is_immutable_and_carries_no_research_semantics(self):
        """EXFACT-06：投影不可变，且不含 research 裁决字段。"""
        projection = _projection(fills=(_fill(event_key=KEY_A),))
        self.assertTrue(dataclasses.is_dataclass(projection))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            projection.identity = "forged"

        keys = set(projection.as_dict())
        for research_only in ("status", "reason", "confidence", "authority",
                              "is_authoritative", "thesis"):
            with self.subTest(field=research_only):
                self.assertNotIn(research_only, keys)

    def test_EXFACT_07_callers_cannot_supply_identity_day_or_verification(self):
        """EXFACT-07：调用方不能自述身份 / 业务日 / 核验结论 —— 签名里没有这些参数。"""
        parameters = set(inspect.signature(EV.fact_projection).parameters) - {"evidence"}
        self.assertEqual({"fill_rows_present"}, parameters)
        for forbidden in ("identity", "business_day", "observed_at", "verification",
                          "status", "source", "version"):
            with self.subTest(parameter=forbidden):
                self.assertNotIn(forbidden, parameters)


class FailClosedTests(unittest.TestCase):
    def test_EXFACT_08_illegal_projections_are_rejected(self):
        """EXFACT-08：版本 / 范围 / 字段类型不符一律 fail closed。"""
        base = _projection(fills=(_fill(event_key=KEY_A),))
        good = {
            "version": base.version, "identity": base.identity,
            "identity_kind": base.identity_kind, "order_id": base.order_id,
            "lifecycle_state": base.lifecycle_state, "fill_verdict": base.fill_verdict,
            "business_day": base.business_day, "observed_at": base.observed_at,
            "verification": base.verification, "inconsistencies": base.inconsistencies,
        }

        cases = (
            ("version_mismatch", {"version": "execution-fact-v0"}),
            ("unknown_identity_kind", {"identity_kind": "vibes"}),
            ("alien_identity", {"identity": "  "}),
            ("field_not_an_evidence_field", {"business_day": DAY}),
            ("field_not_an_evidence_field", {"observed_at": EE.EvidenceField.known("code", "x")}),
            ("alien_identity", {"verification": {"verification_scope": "market_data"}}),
            ("unknown_verification_status", {"verification": {
                "verification_scope": EV.EXECUTION_VERIFICATION_SCOPE,
                "verification_status": "supported", "verification_source": EV.EVIDENCE_SOURCE_LEDGER,
            }}),
        )
        for reason, override in cases:
            with self.subTest(reason=reason, override=str(override)[:40]):
                with self.assertRaises(EV.ExecutionFactContractError) as caught:
                    EV._issue_fact_projection(**{**good, **override})
                self.assertEqual(reason, caught.exception.reason)

    def test_EXFACT_12_the_projection_has_no_public_raw_constructor(self):
        """EXFACT-12：调用方不能自造一个与 owner 签发无法区分的投影。

        少了这一条，"唯一发布入口"只是声明：任何调用方都能拼一个自述身份 / 业务日的对象，
        而它的形状与 owner 签发的完全一样。这与 ``ResearchEvidenceRef`` 同一处理方式。
        """
        base = _projection(fills=(_fill(event_key=KEY_A),))
        with self.assertRaises(TypeError):
            EV.ExecutionFactProjection(
                version=base.version, identity="forged", identity_kind=base.identity_kind,
                order_id=7, lifecycle_state=base.lifecycle_state,
                fill_verdict=base.fill_verdict, business_day=base.business_day,
                observed_at=base.observed_at, verification=base.verification,
                inconsistencies=(),
            )
        # 非空性：正常路径仍然可用。
        self.assertIsInstance(base, EV.ExecutionFactProjection)

    def test_EXFACT_13_the_nested_verification_declaration_is_immutable(self):
        """EXFACT-13：投影 frozen，但嵌套的核验声明也必须不可改。

        否则拿到一条合法投影的人可以改掉 ``verification["verification_status"]``，
        然后 ``as_dict()`` 会用同一个"owner 已发布"的对象发布一个被改过的裁决。
        """
        projection = _projection(fills=(_fill(event_key=KEY_A),))
        declared = projection.verification
        for key in ("verification_status", "is_verified", "verification_scope"):
            with self.subTest(key=key):
                with self.assertRaises(TypeError):
                    declared[key] = "tampered"
        # as_dict 仍然给出可序列化的普通映射，且内容未被改动。
        self.assertEqual(
            EV.EXECUTION_STATUS_VERIFIED, projection.as_dict()["verification"]["verification_status"],
        )

    def test_EXFACT_14_partial_fill_evidence_does_not_become_a_whole_fact_claim(self):
        """EXFACT-14：混合新旧流水时，只有部分行带证据 → 不得声称整笔都有。

        两个身份列都可空（旧行没有 ``event_key``，``quote_at`` 也可能为空）。若聚合只把
        "存在的值"交出去，一次混合成交就会被说成"有一个观测时点 / 有一个完整身份"，
        而其中一些被包含进来的流水根本没有该证据。
        """
        projection = _projection(fills=(
            _fill(event_key=KEY_A, quote_at=OBSERVED_AT),
            _fill(event_key="", quote_at=""),          # 旧行：两个身份列都空
        ))
        # 业务日两行都有（生产里 fill_date 是 NOT NULL）→ 仍然 known。
        self.assertTrue(projection.business_day.is_known)
        # 观测时点与身份都**不完整** → 不许报 known / 不许冒充完整身份。
        self.assertTrue(projection.observed_at.is_unknown)
        self.assertIn("1 of 2", str(projection.observed_at.detail))
        self.assertEqual(EV.IDENTITY_KIND_INCOMPLETE_EVENT_KEYS, projection.identity_kind)
        self.assertEqual("order:7", projection.identity)
        self.assertNotEqual(KEY_A, projection.identity)

        # 非空性：业务日的完整性判据同样真的会生效（合成一行缺 fill_date 的流水）。
        no_session = _projection(fills=(
            _fill(event_key=KEY_A),
            _fill(event_key=KEY_B, fill_date=""),
        ))
        self.assertTrue(no_session.business_day.is_unknown)
        self.assertIn("1 of 2", str(no_session.business_day.detail))


class SingleSourceTests(unittest.TestCase):
    def test_EXFACT_09_identity_is_reachable_from_the_read_path(self):
        """EXFACT-09：identity 不是纸面概念 —— ``event_key`` 真的被读路径取出来。

        没有这一步，``identity_kind`` 会永远是 ``order_id_only``，契约等于空转。
        """
        with open(os.path.join(BACKEND, "execution_evidence.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("fill_event_keys", source)
        self.assertIn("fill_observed_ats", source)
        # 读路径的流水 SELECT 必须包含 owner 的逐次事实身份。
        select = source.split("SELECT order_id,account_id,side,code,qty,price,amount,fees,")[1]
        self.assertIn("event_key", select.split("FROM paper_fills")[0])
        # 字段名必须已登记，否则 EvidenceField 构造期就会抛。
        for name in EE.FACT_FIELDS:
            with self.subTest(field=name):
                self.assertIn(name, EE.KNOWN_EVIDENCE_FIELD_NAMES)

    def test_EXFACT_10_the_verdict_still_has_exactly_one_implementation(self):
        """EXFACT-10：本契约**委托**既有判定，没有第二份核验实现。"""
        tree = ast.parse(inspect.getsource(EV.fact_projection))
        called = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                called.add(node.func.id)
        self.assertIn("verification_from_evidence", called,
                      "fact_projection 必须委托 verification_from_evidence")
        # 判定顺序表仍然只有一份，且只有 verified 一个映射到 verified。
        mapped = [v for v, s in EV.VERDICT_TO_STATUS.items() if s == EV.EXECUTION_STATUS_VERIFIED]
        self.assertEqual([EE.FILL_VERDICT_VERIFIED], mapped)

    def test_EXFACT_11_execution_evidence_field_set_was_not_expanded(self):
        """EXFACT-11：本契约按"新增独立投影"落地，没有改动 ExecutionEvidence 的字段集。

        改动它会波及 ``__post_init__`` → ``as_dict``/``fingerprint`` → ``fill_verdict`` /
        ``inconsistencies``，而它们的结论经 ``execution_status`` 传播到十几条读路径。
        """
        names = {field.name for field in dataclasses.fields(EE.ExecutionEvidence)}
        self.assertEqual(set(EE.ALL_EVIDENCE_FIELDS), {name for name in names
                                                      if name in EE.ALL_EVIDENCE_FIELDS})
        for fact_field in EE.FACT_FIELDS:
            with self.subTest(field=fact_field):
                self.assertNotIn(fact_field, names)
        self.assertEqual(("business_day", "observed_at"), EE.FACT_FIELDS)


if __name__ == "__main__":
    unittest.main()
