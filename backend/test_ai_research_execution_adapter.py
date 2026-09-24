# -*- coding: utf-8 -*-
"""R27-B2C-3 —— execution fact → typed research evidence adapter 的回归。

存在理由是这条不变量：

    **execution 事实可以进入研究链路，但它的身份、业务日与核验结论全部由 owner 发布；
    而"整张订单是否完整成交"与"这个结论可不可信"是两个不同的问题。**

分七组：

    EXEC-REF-01 ~ 06  签发形状：真投影才可签发、dict/伪对象/子类拒绝、kind 派生、
                      identity 完全由 owner 派生、业务日 known 才签发、unknown 时 fail
                      closed（**不** fallback 到 observed_at 日期）
    EXEC-REF-07 ~ 11  status × source → owner-neutral outcome：全表逐个断言，
                      含 partial / not_executed / unknown / inconsistent-legacy-absent
    EXEC-REF-12       映射与 owner 公开契约**双向穷尽一致**，owner 词表漂移即 fail closed
    EXEC-REF-13 ~ 14  market 兼容面（method=None / cross_source=False）、owner attributes 深冻结
    EXEC-REF-15 ~ 16  同 identity + 内容变了 → EvidenceConflict；同 identity + 同内容 → 安全去重
    EXEC-REF-17       签名不给调用方任何自述入口
    EXEC-REF-18 ~ 20  owner-origin provenance 仍 OPEN、依赖方向单向、映射只有 adapter 一份

全部离线：只用 ``evidence_from_order`` 的纯函数路径与 owner 自己的签发口，不连数据库。
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import sys
import unittest
from types import MappingProxyType
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_research_contract as ARC  # noqa: E402
import ai_research_execution_adapter as ADA  # noqa: E402
import execution_evidence as EE  # noqa: E402
import execution_verification as EV  # noqa: E402
import paper_trading_rules as PTR  # noqa: E402

ADAPTER_MODULE = "ai_research_execution_adapter.py"
CONTRACT_MODULE = "ai_research_contract.py"

#: 固定的业务日 / 观测时刻 —— 与本机时钟无关，测试因此完全确定。
DAY = "2026-08-27"
OBSERVED_AT = f"{DAY}T10:30:00+08:00"
CREATED_AT = f"{DAY} 10:29:00"
KEY_A = "a" * 64
AMOUNT = 100 * 10.5
#: 用**生产费率模型**算手续费：随手写一个数会命中
#: ``INCONSISTENCY_FEES_NOT_RECONCILED``，来源就不是干净的账本证据了。
FEES = PTR.commission(AMOUNT)
PARTIAL_QTY = 30
PARTIAL_AMOUNT = PARTIAL_QTY * 10.5
PARTIAL_FEES = PTR.commission(PARTIAL_AMOUNT)


# ---------------------------------------------------------------------------
# fixtures —— 全部走 ``evidence_from_order`` 纯函数路径，不连数据库
# ---------------------------------------------------------------------------


def _fill(**overrides):
    row = {
        "account_id": "acct", "side": "buy", "code": "600001", "qty": 100,
        "price": 10.5, "amount": AMOUNT, "fees": FEES,
        "fill_date": DAY, "quote_at": OBSERVED_AT, "event_key": KEY_A,
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


def _projection(order=None, fills=(), **kwargs):
    """按 ``verification_for_order`` 选取流水的方式构造投影（带身份列核对）。"""
    rows = tuple(fills)
    identity = {"fill_identity_rows": rows} if rows else {}
    return EV.fact_projection(
        EE.evidence_from_order(order or _order(), rows, **identity), **kwargs
    )


def _verified_projection():
    """``verified`` + 账本证据：整单被证明完整成交。"""
    projection = _projection(_order(), (_fill(),))
    assert projection.verification["verification_status"] == EV.EXECUTION_STATUS_VERIFIED
    assert projection.verification["verification_source"] == EV.EVIDENCE_SOURCE_LEDGER
    return projection


def _partial_projection():
    """``partial`` + 账本证据：账本证明**发生过真实的部分成交**，整单没成交完。"""
    projection = _projection(
        _order(status="partially_filled", qty=100),
        (_fill(qty=PARTIAL_QTY, amount=PARTIAL_AMOUNT, fees=PARTIAL_FEES),),
    )
    assert projection.verification["verification_status"] == EV.EXECUTION_STATUS_PARTIAL
    assert projection.verification["verification_source"] == EV.EVIDENCE_SOURCE_LEDGER
    return projection


def _unknown_ledger_projection():
    """``unknown`` + 账本证据：owner 有记录，但得出不了明确的 execution fact。

    账本说"只部分成交"，可流水合计等于目标数量 —— 数量对不上结论，owner 如实报
    ``unknown``，而不是挑一个说法。业务日是 owner 记录的，因此这条事实**可以**进入研究。
    """
    projection = _projection(_order(status="partially_filled", qty=100), (_fill(),))
    assert projection.verification["verification_status"] == EV.EXECUTION_STATUS_UNKNOWN
    assert projection.verification["verification_source"] == EV.EVIDENCE_SOURCE_LEDGER
    return projection


def _inconsistent_projection():
    """``verified`` + ``evidence_inconsistent``：结论存在，但证据来源本身不可用。"""
    projection = _projection(_order(status="SUBMITTED"), (_fill(),))
    assert projection.verification["verification_status"] == EV.EXECUTION_STATUS_VERIFIED
    assert projection.verification["verification_source"] == EV.EVIDENCE_SOURCE_INCONSISTENT
    return projection


def _incomplete_identity_projection():
    """部分成交流水缺 ``event_key``：身份退成 ``order:<id>`` 并如实标注原因。"""
    projection = _projection(
        _order(status="partially_filled", qty=100),
        (_fill(qty=PARTIAL_QTY, amount=PARTIAL_AMOUNT, fees=PARTIAL_FEES, event_key=None),),
    )
    assert projection.identity_kind == EV.IDENTITY_KIND_INCOMPLETE_EVENT_KEYS
    assert projection.business_day.is_known
    return projection


def _not_executed_ledger_declared():
    """``not_executed`` + 账本证据的 owner 核验声明。"""
    return dict(EV.verification_contract(
        EV.EXECUTION_STATUS_NOT_EXECUTED, EV.EVIDENCE_SOURCE_LEDGER,
    ))


def _rejected_projection():
    """被拒的委托：owner 今天**没有**记录业务日（``paper_orders`` 没有交易日列）。"""
    return _projection(_order(status="rejected", reason="risk rejected"), ())


def _owner_records_the_business_day(projection, *, business_day=DAY):
    """让 owner 补记业务日 —— 走 owner **自己的**签发口，不是在测试里拼字段。

    ``not_executed`` 类事实今天在 ``paper_orders`` 上没有可证明的业务日（owner data
    gap = OPEN）。本 helper 模拟"该缺口被 owner 关闭"之后的形态：除 ``business_day``
    之外的每一个维度仍然逐字来自真实投影，核验声明仍是 owner 的 canonical 声明。
    """
    return EV._issue_fact_projection(
        version=projection.version,
        identity=projection.identity,
        identity_kind=projection.identity_kind,
        order_id=projection.order_id,
        lifecycle_state=projection.lifecycle_state,
        fill_verdict=projection.fill_verdict,
        business_day=EE.EvidenceField.known("business_day", business_day),
        observed_at=projection.observed_at,
        verification=projection.verification,
        inconsistencies=projection.inconsistencies,
    )


def _hypothesis(ref, relation=ARC.RELATION_SUPPORTS):
    return ARC.ResearchHypothesis(
        hypothesis_id="H-EXEC-1", as_of=DAY, subject="600001",
        thesis="the paper ledger reflects real fills",
        evidence=(ARC.HypothesisEvidence(ref=ref, relation=relation),),
    )


def _module_source(name):
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


def _production_modules():
    return sorted(
        name for name in os.listdir(BACKEND)
        if name.endswith(".py") and not name.startswith("test_")
    )


# ---------------------------------------------------------------------------
# EXEC-REF-01 ~ 06 —— 签发形状
# ---------------------------------------------------------------------------


class IssuanceShapeTests(unittest.TestCase):
    def test_EXEC_REF_01_a_real_projection_issues_a_typed_research_ref(self):
        """EXEC-REF-01：真正的 ExecutionFactProjection 可以签发 ResearchEvidenceRef。"""
        projection = _verified_projection()
        ref = ADA.evidence_ref_from_execution_projection(projection)

        self.assertIsInstance(ref, ARC.ResearchEvidenceRef)
        self.assertEqual(ARC.EVIDENCE_SOURCE_EXECUTION, ref.source_type)
        self.assertEqual(DAY, ref.as_of)
        self.assertIsInstance(ref.owner_verification, ARC.OwnerVerification)
        # detail 是最小化的：不复制整份 execution payload。
        self.assertEqual(
            {
                "identity_kind", "order_id", "lifecycle_state", "fill_verdict",
                "observed_at", "content_fingerprint", "fact_contract_version",
            },
            set(ref.detail),
        )
        self.assertEqual(
            EV.EXECUTION_FACT_CONTRACT_VERSION, ref.detail["fact_contract_version"],
        )

    def test_EXEC_REF_02_dict_fake_and_subclass_projections_are_all_rejected(self):
        """EXEC-REF-02：dict / Mapping / 伪对象 / 子类一律拒绝 —— fail closed。

        少了这条，调用方就能自己拼 ``identity`` / ``business_day`` / ``verification``
        然后冒充 execution owner 投影，owner contract 这个边界会形同虚设。
        """

        class DuckTyped:
            identity = KEY_A
            identity_kind = EV.IDENTITY_KIND_FILL_EVENT_KEY
            business_day = EE.EvidenceField.known("business_day", DAY)
            observed_at = EE.EvidenceField.known("observed_at", OBSERVED_AT)
            version = EV.EXECUTION_FACT_CONTRACT_VERSION
            order_id = 7
            lifecycle_state = "FILLED"
            fill_verdict = EE.FILL_VERDICT_VERIFIED
            inconsistencies = ()
            verification = dict(_not_executed_ledger_declared())

            def projection(self):
                return {}

        real = _verified_projection()
        for payload in (
            dict(real.as_dict()), {}, KEY_A, None, 42, DuckTyped(), [real],
        ):
            with self.subTest(payload=type(payload).__name__):
                with self.assertRaises(TypeError) as caught:
                    ADA.evidence_ref_from_execution_projection(payload)
                self.assertIn("ExecutionFactProjection", str(caught.exception))

        # 子类同样不算：入口要求的就是**这一个**类型，不是"长得像 owner 投影"。
        class Subclassed(EV.ExecutionFactProjection):
            pass

        subclassed = object.__new__(Subclassed)
        for member in dataclasses.fields(EV.ExecutionFactProjection):
            object.__setattr__(subclassed, member.name, getattr(real, member.name, None))
        self.assertIsInstance(subclassed, EV.ExecutionFactProjection)
        self.assertIsNot(Subclassed, EV.ExecutionFactProjection)
        with self.assertRaises(TypeError):
            ADA.evidence_ref_from_execution_projection(subclassed)

        # 非空性：真投影仍然可签发，否则上面只是在证明"全都拒绝"。
        self.assertIsInstance(
            ADA.evidence_ref_from_execution_projection(real), ARC.ResearchEvidenceRef,
        )

    def test_EXEC_REF_03_source_type_and_event_kind_are_derived_not_declared(self):
        """EXEC-REF-03：``source_type=execution``，``InformationEvent.kind`` 由它派生。"""
        ref = ADA.evidence_ref_from_execution_projection(_verified_projection())
        self.assertEqual(ARC.EVIDENCE_SOURCE_EXECUTION, ref.source_type)
        self.assertEqual(ARC.EVENT_EXECUTION_OBSERVED, ARC._KIND_BY_SOURCE_TYPE[ref.source_type])

        event = ARC.InformationEvent(as_of=DAY, source="execution_owner", evidence_ref=ref)
        self.assertEqual(ARC.EVENT_EXECUTION_OBSERVED, event.kind)
        self.assertEqual(ARC.EVENT_EXECUTION_OBSERVED, event.projection()["kind"])
        # 调用方无法把一条 execution 事实标成 market 事实：kind 没有可传通道。
        parameters = set(inspect.signature(ARC.InformationEvent).parameters)
        self.assertNotIn("kind", parameters)

    def test_EXEC_REF_04_source_id_is_derived_from_owner_identity_only(self):
        """EXEC-REF-04：``source_id`` 完全由 ``identity_kind`` + ``identity`` 派生。

        adapter 不重算 event key、不接受 ``order_id``：identity authority 是 execution
        owner。fact contract 版本刻意**不**进入 identity —— 版本升级不应该把同一条事实
        变成另一条事实。
        """
        for projection, expected_kind in (
            (_verified_projection(), EV.IDENTITY_KIND_FILL_EVENT_KEY),
            (_partial_projection(), EV.IDENTITY_KIND_FILL_EVENT_KEY),
            (_incomplete_identity_projection(), EV.IDENTITY_KIND_INCOMPLETE_EVENT_KEYS),
            # ``order_id_only`` 恰恰是今天没有 owner 业务日的那一族，因此按 owner 记录
            # 业务日之后的形态断言 —— 身份仍然只由 owner 派生。
            (_owner_records_the_business_day(_rejected_projection()),
             EV.IDENTITY_KIND_ORDER_ONLY),
        ):
            with self.subTest(expected_kind=expected_kind):
                ref = ADA.evidence_ref_from_execution_projection(projection)
                self.assertEqual(expected_kind, projection.identity_kind)
                self.assertEqual(
                    f"{projection.identity_kind}|{projection.identity}", ref.source_id,
                )
                self.assertIn(projection.identity, ref.source_id)

        # 只标识委托的事实：身份里带的是 owner 的 ``order:<id>``，不是裸 order id。
        rejected = _rejected_projection()
        self.assertEqual(EV.IDENTITY_KIND_ORDER_ONLY, rejected.identity_kind)
        self.assertEqual("order:7", rejected.identity)
        self.assertNotEqual(str(rejected.order_id), rejected.identity, "不得退成裸 order id")
        # 版本刻意不参与 identity：改版本不会把同一条事实变成另一条。
        self.assertNotIn(EV.EXECUTION_FACT_CONTRACT_VERSION, rejected.identity)
        self.assertNotIn(
            EV.EXECUTION_FACT_CONTRACT_VERSION,
            ADA.evidence_ref_from_execution_projection(
                _owner_records_the_business_day(rejected),
            ).source_id,
        )

    def test_EXEC_REF_05_known_business_day_becomes_the_ref_as_of(self):
        """EXEC-REF-05：owner 记录的业务日 → ``as_of``。"""
        projection = _verified_projection()
        self.assertTrue(projection.business_day.is_known)
        self.assertEqual(DAY, projection.business_day.require())

        ref = ADA.evidence_ref_from_execution_projection(projection)
        self.assertEqual(DAY, ref.as_of)
        # 业务日与观测时点是两个维度：一个交易日里可以有多个观测时点。
        self.assertNotEqual(DAY, projection.observed_at.require())
        self.assertEqual(projection.observed_at.require(), ref.detail["observed_at"])

    def test_EXEC_REF_06_unknown_business_day_fails_closed_without_any_fallback(self):
        """EXEC-REF-06：业务日 unknown → **拒绝签发**，绝不 fallback。

        被拒 / 被撤等部分 execution facts 今天在 ``paper_orders`` 上没有 owner 记录的交易日
        （只有 ``created_at`` 墙钟）。禁止用它、``observed_at`` 的日期、``order_time`` 或
        墙钟日期顶上：那会把"PIT 不可证明"伪造成一条可用事实。

        本条同时证明这**不是**删能力：owner 一旦记录业务日，同一条事实立刻可签发
        （见 EXEC-REF-09）。
        """
        projection = _rejected_projection()
        self.assertTrue(projection.business_day.is_unknown)
        self.assertEqual(CREATED_AT[:10], DAY, "fixture 的 created_at 日期确实存在且可用")

        with self.assertRaises(ValueError) as caught:
            ADA.evidence_ref_from_execution_projection(projection)
        message = str(caught.exception)
        self.assertIn("business_day", message)
        self.assertIn("owner", message)

        # 更强的一条：观测时点**已知**、业务日未知时仍然拒绝 —— 证明没有去用 observed_at。
        no_session = _projection(_order(status="partially_filled", qty=100),
                                 (_fill(fill_date=None),))
        self.assertTrue(no_session.business_day.is_unknown)
        self.assertTrue(
            no_session.observed_at.is_known,
            "fixture 必须让 observed_at 已知，否则这条断言无法区分 fallback",
        )
        with self.assertRaises(ValueError):
            ADA.evidence_ref_from_execution_projection(no_session)


# ---------------------------------------------------------------------------
# EXEC-REF-07 ~ 11 —— status × source → owner-neutral outcome
# ---------------------------------------------------------------------------


class OwnerOutcomeMappingTests(unittest.TestCase):
    def test_EXEC_REF_07_verified_ledger_is_a_verified_owner_outcome(self):
        """EXEC-REF-07：``verified + ledger`` → ``verified``，整单完整成交。"""
        ref = ADA.evidence_ref_from_execution_projection(_verified_projection())
        self.assertEqual(ARC.OWNER_OUTCOME_VERIFIED, ref.owner_verification.outcome)
        self.assertIs(True, ref.is_verified)
        # owner 的状态词逐字保留，**没有**被翻译成 market 词。
        self.assertEqual(EV.EXECUTION_STATUS_VERIFIED, ref.verification)
        self.assertIs(
            True, ref.verification_attributes["execution_fully_verified"],
        )

    def test_EXEC_REF_08_partial_ledger_is_a_trustworthy_partial_fill_fact(self):
        """EXEC-REF-08（**永久回归**）：``partial + ledger`` 是**可信的事实**。

        execution 的整单布尔位对 ``partial`` 是 ``False``（整单确实没成交完）。若 adapter
        用 ``verification["is_verified"]`` 当 research 判据，这条真实发生的部分成交会被
        降级成"不可信事实"，研究层再也引用不到它。两个问题必须分开：

            完整成交？              NO      → ``execution_fully_verified = False``
            "部分成交"这个事实可信？ YES     → ``ref.is_verified = True``
        """
        projection = _partial_projection()
        self.assertEqual(EV.EXECUTION_STATUS_PARTIAL, projection.verification["verification_status"])
        self.assertIs(False, projection.verification["is_verified"], "整单确实没有完整成交")

        ref = ADA.evidence_ref_from_execution_projection(projection)
        self.assertEqual(EV.EXECUTION_STATUS_PARTIAL, ref.verification, "owner 原词逐字保留")
        self.assertIs(True, ref.is_verified, "'部分成交'这个事实是可信的")
        self.assertIsNone(ref.verification_method)
        self.assertIs(False, ref.cross_source_verified)
        self.assertIs(False, ref.verification_attributes["execution_fully_verified"])

        # 关系由 research 显式声明 —— 可信事实可以作用于 hypothesis。
        hypothesis = _hypothesis(ref, relation=ARC.RELATION_SUPPORTS)
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, hypothesis.status)
        self.assertIsNone(hypothesis.reason)

    def test_EXEC_REF_09_not_executed_ledger_is_a_trustworthy_non_execution_fact(self):
        """EXEC-REF-09（**永久回归**）：``not_executed + ledger`` 可以表达"确认没有执行"。

        ``not_executed`` 是"确认的零"，不是"不知道"。研究层必须能把它当作可信事实，
        而不是因为 ``execution_fully_verified == False`` 就误读成"不可信"。

        同一条事实的**业务日**仍必须由 owner 记录：今天被拒 / 被撤的委托在
        ``paper_orders`` 上没有交易日列（owner data gap = OPEN），因此那一形态在
        EXEC-REF-06 被拒绝。此处先断言映射语义，再让 owner 补记业务日后走完整链路 ——
        缺口一旦关闭，能力立刻可用。
        """
        declared = _not_executed_ledger_declared()
        verification = ADA._execution_owner_verification(declared)
        self.assertEqual(ARC.OWNER_OUTCOME_VERIFIED, verification.outcome)
        self.assertEqual(EV.EXECUTION_STATUS_NOT_EXECUTED, verification.status)
        self.assertIs(True, verification.is_verified)
        self.assertIs(False, verification.attributes["execution_fully_verified"])

        # owner 补记业务日后的完整链路：这是"确认没有执行"，不是"没有可信事实"。
        booked = _owner_records_the_business_day(_rejected_projection())
        self.assertEqual(EV.EXECUTION_STATUS_NOT_EXECUTED, booked.verification["verification_status"])
        self.assertIs(False, booked.verification["is_verified"])
        ref = ADA.evidence_ref_from_execution_projection(booked)
        self.assertEqual(ARC.OWNER_OUTCOME_VERIFIED, ref.owner_verification.outcome)
        self.assertEqual(EV.EXECUTION_STATUS_NOT_EXECUTED, ref.verification)
        self.assertIs(True, ref.is_verified)
        self.assertIs(False, ref.verification_attributes["execution_fully_verified"])

        for relation in (ARC.RELATION_SUPPORTS, ARC.RELATION_CONTRADICTS, ARC.RELATION_CONTEXT):
            with self.subTest(relation=relation):
                hypothesis = _hypothesis(ref, relation=relation)
                if relation == ARC.RELATION_SUPPORTS:
                    self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, hypothesis.status)
                elif relation == ARC.RELATION_CONTRADICTS:
                    self.assertEqual(ARC.HYPOTHESIS_UNSUPPORTED, hypothesis.status)
                else:
                    self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
                    self.assertEqual(ARC.RESEARCH_REASON_NO_SUPPORTING_EVIDENCE, hypothesis.reason)

    def test_EXEC_REF_10_unknown_ledger_cannot_be_upgraded_by_a_supporting_relation(self):
        """EXEC-REF-10 / 26：``unknown + ledger`` → ``unverified``，supports 也无法升级。

        owner 做了核验判定，结论是"不足以判断"（证据在位但没有明确 execution fact）。
        这不是 ``source_unusable``（障碍不在核验过程），而是 ``unverified``：假设层的
        原因必须是 ``evidence_not_verified``，绝不能被 relation 抬成 ``supported``。
        """
        projection = _unknown_ledger_projection()
        ref = ADA.evidence_ref_from_execution_projection(projection)
        self.assertEqual(ARC.OWNER_OUTCOME_UNVERIFIED, ref.owner_verification.outcome)
        self.assertIs(False, ref.is_verified)
        self.assertIs(False, ref.owner_verification.source_unusable)
        self.assertIs(False, ref.verification_attributes["execution_fully_verified"])

        hypothesis = _hypothesis(ref, relation=ARC.RELATION_SUPPORTS)
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
        self.assertEqual(ARC.RESEARCH_REASON_EVIDENCE_NOT_VERIFIED, hypothesis.reason)
        with self.assertRaises(ARC.UnsupportedResearch):
            hypothesis.require_supported()

    def test_EXEC_REF_11_unusable_evidence_sources_stay_separate_from_unverified(self):
        """EXEC-REF-11 / 27：来源不可用 → ``source_unusable`` → reason ``evidence_unavailable``。

        与普通 ``unverified`` 分开是必须的：``evidence_inconsistent`` /
        ``legacy_row_without_fill_evidence`` / ``no_evidence_available`` 说明障碍出在
        **证据来源**，而 ``unknown + ledger`` 是"这条事实不够好"。把两者压成一个原因会让
        用户看不出"要先去修数据"还是"要再等等"。

        行为面用可复现的 ``evidence_inconsistent`` 事实，映射面用 owner 的合法组合表逐个断言。
        """
        ref = ADA.evidence_ref_from_execution_projection(_inconsistent_projection())
        self.assertEqual(ARC.OWNER_OUTCOME_SOURCE_UNUSABLE, ref.owner_verification.outcome)
        self.assertIs(False, ref.is_verified)
        self.assertIs(True, ref.owner_verification.source_unusable)
        # 状态词仍逐字保留，research core 不解释它。
        self.assertEqual(EV.EXECUTION_STATUS_VERIFIED, ref.verification)

        hypothesis = _hypothesis(ref, relation=ARC.RELATION_SUPPORTS)
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
        self.assertEqual(ARC.RESEARCH_REASON_EVIDENCE_UNAVAILABLE, hypothesis.reason)
        self.assertNotEqual(ARC.RESEARCH_REASON_EVIDENCE_NOT_VERIFIED, hypothesis.reason)

        # 三种"来源本身不可用"的来源：owner 认为合法的每一个组合都必须归 source_unusable。
        unusable_sources = (
            EV.EVIDENCE_SOURCE_INCONSISTENT, EV.EVIDENCE_SOURCE_LEGACY, EV.EVIDENCE_SOURCE_ABSENT,
        )
        checked = 0
        for status in EV.EXECUTION_STATUSES:
            for source in unusable_sources:
                try:
                    canonical = EV.verification_contract(status, source)
                except EV.ExecutionFactContractError:
                    continue
                checked += 1
                with self.subTest(status=status, source=source):
                    verification = ADA._execution_owner_verification(dict(canonical))
                    self.assertEqual(ARC.OWNER_OUTCOME_SOURCE_UNUSABLE, verification.outcome)
                    self.assertEqual(status, verification.status)
                    self.assertEqual(source, verification.attributes["verification_source"])
        self.assertGreater(checked, 0, "没有任何不可用来源的合法组合被检查（断言在空转）")


# ---------------------------------------------------------------------------
# EXEC-REF-12 —— 映射与 owner 公开契约双向穷尽
# ---------------------------------------------------------------------------


class MappingExhaustivenessTests(unittest.TestCase):
    def test_EXEC_REF_12_mapping_is_exhaustive_against_the_owner_contract(self):
        """EXEC-REF-12：映射与 owner 的合法组合集合**精确相等**，且漂移即 fail closed。

        这是 B2C-2 ``_MARKET_OUTCOME_BY_VERIFICATION`` 原则在 execution 上的可执行形式：
        **owner 词表的含义由 owner 决定，research 不猜**。曾经的实现是 catch-all：

            outcome = VERIFIED if projection.verification["is_verified"] else UNVERIFIED

        今天四态恰好被部分正确分类，所以行为上不一定看得出来；但 owner 未来新增一个合法
        组合时会自动落进默认分支 —— research 层静默替 owner 决定了一个它没有发布过的语义。

        合法组合集合**由 owner 公开的** ``verification_contract`` 推导，不读它的私有
        ``_LEGAL_SOURCES_BY_STATUS``。
        """
        mapping = ADA._EXECUTION_OUTCOME_BY_VERIFICATION
        legal = ADA._owner_legal_pairs()
        onlookers = len(EV.EXECUTION_STATUSES) * len(EV.EVIDENCE_SOURCES)

        # 1. 正向：映射恰好覆盖 owner 认为合法的组合（不多、不少）。
        self.assertEqual(mapping, {key: mapping[key] for key in mapping})
        self.assertEqual(set(legal), set(mapping))
        self.assertEqual([], ADA._execution_outcome_mapping_problems(mapping, legal))
        # 非空性：owner 的合法组合确实是**子集**（否则上面两条会很空洞）。
        self.assertLess(len(legal), onlookers, "所有组合都合法 —— 穷尽性断言失去意义")

        # 2. 每个合法组合都真的映射到一个合法的 owner-neutral outcome。
        for key in legal:
            with self.subTest(key=key):
                self.assertIn(mapping[key], ARC.OWNER_OUTCOMES)
        # 非空性：三态都必须真的被用到，否则"穷尽"可能只是把一切归到一态。
        self.assertEqual(set(ARC.OWNER_OUTCOMES), set(mapping.values()))

        # 3. 两个方向都必须 RED（纯函数直接可测，不依赖改 owner 源码）。
        dropped = next(key for key in legal if key == (
            EV.EXECUTION_STATUS_UNKNOWN, EV.EVIDENCE_SOURCE_LEDGER,
        ))
        missing = ADA._execution_outcome_mapping_problems(
            {k: v for k, v in mapping.items() if k != dropped}, legal,
        )
        self.assertTrue(missing, "缺一个合法组合未被发现")
        self.assertTrue(any("没有登记" in item for item in missing))

        extra = ADA._execution_outcome_mapping_problems(
            {**mapping, ("brand_new_status", EV.EVIDENCE_SOURCE_LEDGER): ARC.OWNER_OUTCOME_VERIFIED},
            legal,
        )
        self.assertTrue(extra, "映射里多一个 owner 已不接受的组合未被发现")
        self.assertTrue(any("已不接受" in item for item in extra))

        # 4. 行为：**模拟 owner 新增一个合法组合**，归口必须 fail closed。
        #    只 patch owner 自己的词表与合法组合表（那正是 owner 侧一次契约变更的形态），
        #    不动 adapter 源码 —— 从而证明这条保证真的挂在"与 owner 的双向一致"上。
        with mock.patch.object(
            EV, "EXECUTION_STATUSES", (*EV.EXECUTION_STATUSES, "brand_new_status"),
        ), mock.patch.object(
            EV, "_LEGAL_SOURCES_BY_STATUS",
            {**EV._LEGAL_SOURCES_BY_STATUS, "brand_new_status": (EV.EVIDENCE_SOURCE_LEDGER,)},
        ):
            with self.assertRaises(ValueError) as caught:
                ADA._execution_owner_verification(_not_executed_ledger_declared())
            message = str(caught.exception)
            self.assertIn("drifted", message)
            self.assertIn("brand_new_status", message, "错误信息必须点名那个未归口的组合")

        # 5. 反方向行为：owner **收回**一个组合，adapter 也必须 fail closed。
        with mock.patch.object(
            EV, "_LEGAL_SOURCES_BY_STATUS",
            {
                **EV._LEGAL_SOURCES_BY_STATUS,
                EV.EXECUTION_STATUS_PARTIAL: (EV.EVIDENCE_SOURCE_LEDGER,),
            },
        ):
            with self.assertRaises(ValueError) as caught:
                ADA._execution_owner_verification({
                    "verification_status": EV.EXECUTION_STATUS_PARTIAL,
                    "verification_source": EV.EVIDENCE_SOURCE_INCONSISTENT,
                })
            self.assertIn("drifted", str(caught.exception))

        # 6. 非空性对照：词表一致时同一调用必须成功 —— 否则上面可能只是因为该函数恒抛。
        self.assertEqual(
            ARC.OWNER_OUTCOME_VERIFIED,
            ADA._execution_owner_verification(_not_executed_ledger_declared()).outcome,
        )


# ---------------------------------------------------------------------------
# EXEC-REF-13 ~ 14 —— market 兼容面与不可变性
# ---------------------------------------------------------------------------


class CompatibilityAndImmutabilityTests(unittest.TestCase):
    def test_EXEC_REF_13_non_market_facts_carry_no_market_verification_dimension(self):
        """EXEC-REF-13：非 market 事实的 ``verification_method`` 是 ``None``、``cross_source_verified`` 是 ``False``。

        这两个是 **market-only** 读法：``False`` 不代表"execution 事实核验失败"，只代表
        "这不是一条 market cross-source claim"。用一个 market 词值（例如
        ``MDC.VERIFICATION_METHOD_NONE``）去表示"非 market owner"会把别的 owner 重新塞回
        market 的坐标系。
        """
        for ref in (
            ADA.evidence_ref_from_execution_projection(_verified_projection()),
            ADA.evidence_ref_from_execution_projection(_partial_projection()),
            ADA.evidence_ref_from_execution_projection(_unknown_ledger_projection()),
        ):
            with self.subTest(status=ref.verification):
                self.assertIsNone(ref.verification_method)
                self.assertIs(False, ref.cross_source_verified)
                projected = ref.projection()
                self.assertIsNone(projected["verification_method"])
                self.assertIs(False, projected["cross_source_verified"])
                self.assertEqual(ref.verification, projected["verification"])
                # 状态词是 execution 自己的闭集词，不是 market 词。
                self.assertIn(ref.verification, EV.EXECUTION_STATUSES)

            event = ARC.InformationEvent(as_of=DAY, source="execution_owner", evidence_ref=ref)
            self.assertIsNone(event.verification_method)
            self.assertIs(event.is_verified, ref.is_verified)

    def test_EXEC_REF_14_owner_attributes_are_deeply_immutable(self):
        """EXEC-REF-14：owner attributes 深冻结 —— 拿到 ref 的人不能改写 owner 的发布结果。"""
        ref = ADA.evidence_ref_from_execution_projection(_partial_projection())
        attributes = ref.verification_attributes
        self.assertIsInstance(attributes, MappingProxyType)
        self.assertEqual(
            {
                "verification_scope", "verification_version",
                "verification_source", "execution_fully_verified",
            },
            set(attributes),
        )
        with self.assertRaises(TypeError):
            attributes["execution_fully_verified"] = True  # type: ignore[index]

        # 改写**投影自己**的那份 mapping 不影响已签发的 ref（不是同一个对象）。
        projection = _partial_projection()
        ref = ADA.evidence_ref_from_execution_projection(projection)
        with self.assertRaises(TypeError):
            projection.verification["verification_status"] = EV.EXECUTION_STATUS_VERIFIED
        self.assertEqual(EV.EXECUTION_STATUS_PARTIAL, ref.verification)

        # 事实 metadata 不被塞进 verification attributes（那会让 verification 对象重新膨胀
        # 成整份 execution projection）。
        for fact_level in ("identity_kind", "order_id", "business_day", "observed_at",
                           "lifecycle_state", "fill_verdict"):
            with self.subTest(field=fact_level):
                self.assertNotIn(fact_level, attributes)


# ---------------------------------------------------------------------------
# EXEC-REF-15 ~ 16 —— 冲突指纹与安全去重
# ---------------------------------------------------------------------------


class ContentFingerprintTests(unittest.TestCase):
    def test_EXEC_REF_15_same_identity_with_changed_factual_content_is_a_conflict(self):
        """EXEC-REF-15：同一 identity、execution 事实内容被改变 → ``EvidenceConflict``。

        两次运行之间流水被改写（同一笔成交从 ``PARTIAL_FILLED`` 变成 ``FILLED``）不能被
        静默当成"同一条证据"：那会让研究结论静默换掉依据。
        """
        verified = ADA.evidence_ref_from_execution_projection(_verified_projection())
        unknown_ref = ADA.evidence_ref_from_execution_projection(_unknown_ledger_projection())
        # 前提：两条 ref 真的是**同一条** identity（kind + identity + 业务日都相同）。
        self.assertEqual(verified.identity(), unknown_ref.identity())
        self.assertNotEqual(verified.fact_state(), unknown_ref.fact_state())
        self.assertNotEqual(
            verified.detail["content_fingerprint"], unknown_ref.detail["content_fingerprint"],
        )

        for order in (
            (verified, unknown_ref),
            (unknown_ref, verified),
        ):
            with self.subTest(first=order[0].verification):
                with self.assertRaises(ARC.EvidenceConflict):
                    _hypothesis(order[0])
                    ARC.ResearchHypothesis(
                        hypothesis_id="H-EXEC-1", as_of=DAY, subject="600001", thesis="t",
                        evidence=(
                            ARC.HypothesisEvidence(ref=order[0], relation=ARC.RELATION_SUPPORTS),
                            ARC.HypothesisEvidence(ref=order[1], relation=ARC.RELATION_SUPPORTS),
                        ),
                    )

    def test_EXEC_REF_16_same_identity_with_same_content_is_safely_deduped(self):
        """EXEC-REF-16：同一 identity + 同内容 → 安全去重（且指纹是确定性的）。"""
        first = ADA.evidence_ref_from_execution_projection(_verified_projection())
        second = ADA.evidence_ref_from_execution_projection(_verified_projection())

        self.assertEqual(first.identity(), second.identity())
        self.assertEqual(first.fact_state(), second.fact_state())
        self.assertEqual(
            first.detail["content_fingerprint"], second.detail["content_fingerprint"],
        )
        self.assertEqual(64, len(first.detail["content_fingerprint"]))

        hypothesis = ARC.ResearchHypothesis(
            hypothesis_id="H-EXEC-1", as_of=DAY, subject="600001", thesis="t",
            evidence=(
                ARC.HypothesisEvidence(ref=first, relation=ARC.RELATION_SUPPORTS),
                ARC.HypothesisEvidence(ref=second, relation=ARC.RELATION_SUPPORTS),
            ),
        )
        self.assertEqual(1, len(hypothesis.evidence), "同一条事实不得被算成两条")
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, hypothesis.status)

        # 指纹必须是纯函数式的：不受进程内 hash 随机化、时间或调用顺序影响。
        self.assertEqual(
            ADA._content_fingerprint(_verified_projection()),
            ADA._content_fingerprint(_verified_projection()),
        )
        self.assertNotEqual(
            ADA._content_fingerprint(_verified_projection()),
            ADA._content_fingerprint(_partial_projection()),
        )


# ---------------------------------------------------------------------------
# EXEC-REF-17 —— 签名不给调用方自述入口
# ---------------------------------------------------------------------------


class IssuanceBoundaryTests(unittest.TestCase):
    def test_EXEC_REF_17_callers_cannot_supply_identity_day_or_outcome(self):
        """EXEC-REF-17：签名里**只有** projection —— 身份 / 业务日 / 核验结论都不可自述。"""
        parameters = set(
            inspect.signature(ADA.evidence_ref_from_execution_projection).parameters
        )
        self.assertEqual({"projection"}, parameters)
        for forbidden in (
            "source_id", "source_type", "as_of", "verification", "outcome", "status",
            "business_day", "verification_source", "order_id", "identity", "detail",
        ):
            with self.subTest(parameter=forbidden):
                self.assertNotIn(forbidden, parameters)

        # 仍然没有公开 raw 构造器 —— adapter 也不提供绕道。
        with self.assertRaises(TypeError):
            ARC.ResearchEvidenceRef(
                source_type=ARC.EVIDENCE_SOURCE_EXECUTION, source_id="forged", as_of=DAY,
            )
        self.assertFalse(hasattr(ADA, "_OWNER_ISSUED"))
        self.assertFalse(hasattr(ARC, "_OWNER_ISSUED"))

    def test_EXEC_REF_18_owner_origin_provenance_is_still_open_and_required(self):
        """EXEC-REF-18：owner-origin provenance 仍然 **OPEN / REQUIRED**。

        adapter 保证的是"调用方不能自述身份 / 业务日 / 核验结论"，**不是**"输入对象确实
        由 owner 产生"。``ExecutionEvidence`` 仍是公开可构造的，因此两步伪造路径照旧 ——
        本测试把它作为**已知限制**断言下来，而不是假装已封堵。
        """
        doc = ADA.__doc__ or ""
        self.assertIn("OPEN", doc)
        self.assertIn("REQUIRED", doc)
        self.assertIn("owner-origin provenance", doc)
        self.assertIn("两步伪造", doc)
        self.assertNotIn("已关闭 two-step forgery", doc)

        # 两步伪造今天确实可以通过：伪造只是从一步变成两步。
        forged_evidence = EE.evidence_from_order(_order(), (_fill(),))
        forged_projection = EV.fact_projection(forged_evidence)
        forged_ref = ADA.evidence_ref_from_execution_projection(forged_projection)
        self.assertIs(True, forged_ref.is_verified)
        self.assertEqual(ARC.OWNER_OUTCOME_VERIFIED, forged_ref.owner_verification.outcome)


# ---------------------------------------------------------------------------
# EXEC-REF-19 ~ 20 —— 依赖方向与"映射只有一份"
# ---------------------------------------------------------------------------


class AdapterBoundaryTests(unittest.TestCase):
    """§32 maintainability gate 的可执行形式。"""

    def test_EXEC_REF_19_dependency_direction_is_one_way(self):
        """EXEC-REF-19：``execution → adapter → contract``，反向为 0 且 adapter 是唯一接缝。"""
        contract_imports = self._imported_roots(CONTRACT_MODULE)
        self.assertNotIn(ADAPTER_MODULE[:-3], contract_imports)
        for name in ("execution_verification", "execution_evidence"):
            with self.subTest(imported=name):
                self.assertNotIn(name, contract_imports, "research contract 不得 import execution")

        execution_imports = self._imported_roots("execution_verification.py")
        for name in ("ai_research_contract", ADAPTER_MODULE[:-3]):
            with self.subTest(imported=name):
                self.assertNotIn(name, execution_imports, "execution owner 不得 import research")

        adapter_imports = self._imported_roots(ADAPTER_MODULE)
        self.assertIn("ai_research_contract", adapter_imports)
        self.assertIn("execution_verification", adapter_imports)

        # 同时认识两套词表的 production 模块**只有** adapter 一个。
        both = [
            name for name in _production_modules()
            if {"ai_research_contract", "execution_verification"} <= self._imported_roots(name)
        ]
        self.assertEqual([ADAPTER_MODULE], both, f"不止一个模块同时认识两套词表：{both}")
        # 非空性：扫描器确实能看到那个唯一的模块。
        self.assertIn("execution_verification", adapter_imports)

    def test_EXEC_REF_20_execution_status_vocabulary_lives_only_in_the_adapter(self):
        """EXEC-REF-20：execution 状态 / 来源词只出现在 adapter 里，且映射只有一份。

        ``ai_research_contract`` 里当然有 ``EVIDENCE_SOURCE_*`` —— 那是 **research 自己**的
        evidence source 词表（``market_data`` / ``execution`` …），与 execution owner 的
        ``EVIDENCE_SOURCE_LEDGER`` 是同名不同物。真正要守的是：contract 里不得出现
        **execution owner 的状态词与来源词**，而 hypothesis 的判定层不得出现**任何** owner
        的核验词表。
        """
        contract = _module_source(CONTRACT_MODULE)
        self.assertNotIn("EXECUTION_STATUS_", contract)
        self.assertNotIn("_EXECUTION_OUTCOME_BY_VERIFICATION", contract)
        for literal in (
            EV.EVIDENCE_SOURCE_LEDGER, EV.EVIDENCE_SOURCE_LEGACY,
            EV.EVIDENCE_SOURCE_ABSENT, EV.EVIDENCE_SOURCE_INCONSISTENT,
        ):
            with self.subTest(execution_source=literal):
                self.assertNotIn(literal, contract, "research contract 出现了 execution 的来源词")

        # hypothesis 的判定层不得依赖**任何** owner 的核验词表 —— market 的也不行。
        for chunk in (
            self._function_source(contract, "_derive_status"),
            self._class_source(contract, "HypothesisEvidence"),
            self._class_source(contract, "ResearchHypothesis"),
        ):
            for forbidden in ("MDC.", "EXECUTION_STATUS_", "EVIDENCE_SOURCE_", "VERIFICATION_"):
                with self.subTest(forbidden=forbidden, chunk=chunk[:40]):
                    self.assertNotIn(forbidden, chunk)

        # 映射表与合法组合推导只有 adapter 一份实现。
        holders = [
            name for name in _production_modules()
            if "_EXECUTION_OUTCOME_BY_VERIFICATION" in _module_source(name)
        ]
        self.assertEqual([ADAPTER_MODULE], holders, f"outcome 映射出现多份实现：{holders}")
        owners = [
            name for name in _production_modules()
            if "verification_contract" in _module_source(name)
            and name != "execution_verification.py"
        ]
        self.assertEqual([ADAPTER_MODULE], owners, f"owner 合法组合被多处重算：{owners}")

        # 没有 registry framework / BaseAdapter class / manager / service / repository。
        adapter_source = _module_source(ADAPTER_MODULE)
        for forbidden in (
            "class BaseAdapter", "register_adapter", "ADAPTER_REGISTRY",
            "class Manager", "class Service", "class Repository", "class Facade",
        ):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, adapter_source)
        self.assertEqual(("evidence_ref_from_execution_projection",), ADA.__all__)

    def _node_source(self, source, selector):
        """模块级某个 def / class 的源码片段 —— 用于"这一层不得出现某词表"的断言。

        只看**会执行**的代码，不看 docstring：边界模块必须在 docstring 里写清它为什么不
        解释 owner 词表，字符级子串搜索会把说明文字本身当成越界证据。
        """
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == selector:
                    body = [child for child in node.body]
                    if (
                        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)
                    ):
                        body = body[1:]
                    return ast.unparse(ast.Module(body=body, type_ignores=[]))
        raise AssertionError(f"找不到 {selector!r}")

    def _function_source(self, source, name):
        return self._node_source(source, name)

    def _class_source(self, source, name):
        inner = self._node_source(source, name)
        # 类里的方法各自可能带 docstring；把它们也剥掉。
        stripped = []
        for node in ast.parse(inner).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stripped.append(ast.unparse(node))
                continue
            body = list(node.body)
            if (
                body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            stripped.append(ast.unparse(ast.Module(body=body, type_ignores=[])))
        return "\n".join(stripped)

    def _imported_roots(self, name):
        tree = ast.parse(_module_source(name))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                roots.add(node.module.split(".")[0])
        return roots


if __name__ == "__main__":
    unittest.main()
