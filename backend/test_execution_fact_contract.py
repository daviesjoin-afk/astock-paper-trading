# -*- coding: utf-8 -*-
"""R27-B2C-1 —— execution owner fact contract 的回归。

存在理由是这条不变量：

    **一条 execution 事实的身份、业务日与核验结论由 owner 发布，不由消费者拼。**

分六组：

    EXFACT-01 ~ 03   filled fact 的投影完整；核验是 owner-native 词表，不是 market 词表
    EXFACT-04        没有 owner 记录的业务日时如实报 unknown（**不**用墙钟日期冒充）
    EXFACT-05 ~ 07   多成交身份、投影不可变且不含 research 语义、调用方无法自述
    EXFACT-08        非法词表 / 非法组合 / 版本不符 / 字段类型错一律 fail closed
    EXFACT-09 ~ 11   identity 真的从读路径可达； verdict 唯一来源仍是
                     ``verification_from_evidence``；本契约按"新增独立投影"落地，
                     没有改动 ``ExecutionEvidence`` 的字段集
    EXFACT-12 ~ 14   无公开 raw 构造器、嵌套核验声明不可变、部分流水证据不冒充整笔事实
    EXFACT-15 ~ 18   入口只接受 typed evidence、核验声明精确相等、缺 order id fail closed、
                     PIT 值格式可证明
    EXFACT-19        私有签发口只能由 owner 工厂调用（可执行边界，不是命名约定）

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

#: 私有签发口所在的 owner 模块，以及唯一允许调用它的函数。
PROJECTION_MODULE = "execution_verification.py"
PRIVATE_ISSUER = "_issue_fact_projection"
ALLOWED_ISSUER_CALLERS = frozenset({"fact_projection"})


def _production_modules() -> list:
    return sorted(
        name for name in os.listdir(BACKEND)
        if name.endswith(".py") and not name.startswith("test_")
    )


def _tree(name: str):
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _call_name(node) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _imported_issuer_aliases(tree) -> set:
    """``from execution_verification import _issue_fact_projection as X`` 的本地名。

    别名必须解析：否则 ``import ... as issue`` 之后 ``issue(...)`` 会让"唯一调用点"
    这条边界完全看不到。
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == PROJECTION_MODULE[:-3]:
                for alias in node.names:
                    if alias.name == PRIVATE_ISSUER:
                        names.add(alias.asname or alias.name)
    return names


def _issuer_calls(tree) -> list:
    """每次对私有签发口的调用 → ``(所在函数名, 行号)``。

    刻意只回答"哪个函数里出现了这次调用"这一层（caller/function-scope），
    不做控制流或数据流分析。
    """
    aliases = _imported_issuer_aliases(tree)
    found = []

    def is_issuer(node) -> bool:
        if not isinstance(node, ast.Call):
            return False
        name = _call_name(node)
        return name == PRIVATE_ISSUER or name in aliases

    def walk(node, func_name):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if is_issuer(child):
                found.append((func_name, child.lineno))
            walk(child, func_name)

    walk(tree, None)
    return found


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
            ("alien_verification_scope", {"verification": {"verification_scope": "market_data"}}),
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


class InputBoundaryTests(unittest.TestCase):
    def test_EXFACT_15_duck_typed_evidence_cannot_be_published_as_an_owner_projection(self):
        """EXFACT-15：入口只接受真正的 ``ExecutionEvidence``。

        少了这一步，一个普通伪对象只要实现 ``fill_verdict_value()`` / ``inconsistencies()``
        并塞一段看起来合法的 ``provenance``，就能让 ``fact_projection`` 发布一条
        ``verified + ledger`` 的"owner projection" —— owner contract 这个边界就形同虚设。
        """

        class FakeEvidence:
            order_id = 7
            lifecycle_state = "filled"
            provenance = {
                "fill_rows": 1,
                "fill_session_rows": 1,
                "fill_sessions": [DAY],
                "fill_observed_at_rows": 1,
                "fill_observed_ats": [OBSERVED_AT],
                "fill_event_key_rows": 1,
                "fill_event_keys": [KEY_A],
            }

            def fill_verdict_value(self):
                return EE.FILL_VERDICT_VERIFIED

            def inconsistencies(self):
                return ()

        for payload in (FakeEvidence(), {}, "evidence", None, 42):
            with self.subTest(payload=type(payload).__name__):
                with self.assertRaises(EV.ExecutionFactContractError) as caught:
                    EV.fact_projection(payload)
                self.assertEqual("not_execution_evidence", caught.exception.reason)

        # 子类同样不算：入口要求的就是**这一个**类型，不是"长得像 ExecutionEvidence"。
        class Subclassed(EE.ExecutionEvidence):
            pass

        real = _evidence(fills=(_fill(event_key=KEY_A),))
        subclassed = object.__new__(Subclassed)
        for member in dataclasses.fields(EE.ExecutionEvidence):
            object.__setattr__(subclassed, member.name, getattr(real, member.name, None))
        self.assertIsInstance(subclassed, EE.ExecutionEvidence)
        with self.assertRaises(EV.ExecutionFactContractError) as caught:
            EV.fact_projection(subclassed)
        self.assertEqual("not_execution_evidence", caught.exception.reason)

    def test_EXFACT_16_non_canonical_verification_statement_is_rejected(self):
        """EXFACT-16：核验声明必须与 owner 的 canonical 声明**精确相等**。

        只校验 scope/status/source 是不够的：同一个对象可以同时说 ``status='verified'``
        与 ``is_verified=False``，或带一个伪造的 ``verification_version``，而下游只看到
        "这是一条已发布的裁决"。
        """
        base = _projection(fills=(_fill(event_key=KEY_A),))
        good = {
            "version": base.version, "identity": base.identity,
            "identity_kind": base.identity_kind, "order_id": base.order_id,
            "lifecycle_state": base.lifecycle_state, "fill_verdict": base.fill_verdict,
            "business_day": base.business_day, "observed_at": base.observed_at,
            "verification": dict(base.verification), "inconsistencies": base.inconsistencies,
        }

        tampered = {
            "wrong version": {"verification_version": "fake-version"},
            "wrong is_verified": {"is_verified": False},
            "extra field": {"verification_method": "cross_source"},
        }
        for label, patch in tampered.items():
            with self.subTest(case=label):
                statement = {**good["verification"], **patch}
                with self.assertRaises(EV.ExecutionFactContractError) as caught:
                    EV._issue_fact_projection(**{**good, "verification": statement})
                self.assertEqual("non_canonical_verification", caught.exception.reason)

        # 非空性：canonical 声明本身必须被接受（否则上面三条只是在证明"全都拒绝"）。
        self.assertIsInstance(EV._issue_fact_projection(**good), EV.ExecutionFactProjection)

    def test_EXFACT_17_absent_order_id_never_becomes_a_placeholder_identity(self):
        """EXFACT-17：没有完整成交身份、也没有可用 order id → fail closed。

        ``ExecutionEvidence.order_id`` 默认允许 ``None``，而 ``"order:%s" % None`` 是一个
        **非空字符串**，会被 ``__post_init__`` 当成合法 identity —— 于是多条没有 order id
        的不同事实会共享同一个身份。这是本轮 identity contract 最核心的一条。
        """
        for missing in (None, "", "   ", True):
            with self.subTest(order_id=repr(missing)):
                with self.assertRaises(EV.ExecutionFactContractError) as caught:
                    _projection(_order(id=missing), fills=())
                self.assertEqual("identity_unavailable", caught.exception.reason)

        # 绝不产出占位身份：不同委托的真实 id 必须给出不同身份。
        self.assertNotEqual(
            _projection(_order(id=7), fills=()).identity,
            _projection(_order(id=8), fills=()).identity,
        )

        # 非空性：真实 order id 仍然可用，而且身份里带着它。
        self.assertEqual("order:7", _projection(_order(id=7), fills=()).identity)
        self.assertEqual("order:ABC", _projection(_order(id="ABC"), fills=()).identity)

    def test_EXFACT_18_day_and_instant_values_are_format_validated(self):
        """EXFACT-18：PIT 字段必须是可证明的格式，脏值不得发布为 ``known``。

        数据库列是 TEXT，所以"非空"不等于"可证明"：``banana`` 不能变成 typed PIT 事实，
        不带时区的时间戳也不是一个可证明的瞬时。
        """
        bad_day = _projection(fills=(_fill(event_key=KEY_A, fill_date="banana"),))
        self.assertTrue(bad_day.business_day.is_unknown)
        self.assertIn("not a provable value", str(bad_day.business_day.detail))

        for bad_instant in ("banana", f"{DAY} 10:30:00", "2026-08-27T10:30:00"):
            with self.subTest(observed_at=bad_instant):
                projection = _projection(fills=(
                    _fill(event_key=KEY_A, quote_at=bad_instant),
                ))
                self.assertTrue(projection.observed_at.is_unknown)
                self.assertIn("not a provable value", str(projection.observed_at.detail))

        # 非空性：合法值仍然是 known（否则这条断言只是在证明"全都 unknown"）。
        good = _projection(fills=(_fill(event_key=KEY_A),))
        self.assertEqual(DAY, good.business_day.require())
        self.assertEqual(OBSERVED_AT, good.observed_at.require())
        # 闰日与非闰日都要按真实日历判定，而不是只看形状。
        self.assertTrue(_projection(
            fills=(_fill(event_key=KEY_A, fill_date="2026-02-28"),),
        ).business_day.is_known)
        self.assertTrue(_projection(
            fills=(_fill(event_key=KEY_A, fill_date="2026-02-30"),),
        ).business_day.is_unknown)


class PublicationBoundaryTests(unittest.TestCase):
    def test_EXFACT_19_private_issuer_is_reachable_only_from_the_owner_factory(self):
        """EXFACT-19：私有签发口**结构上**只能由 owner 工厂调用。

        ``_issue_fact_projection`` 名字是 private，但 Python 层任何模块都能直接
        ``EV._issue_fact_projection(...)``；只要传入的 verification shape 合法，就能绕过
        ``fact_projection`` 的类型校验、identity 派生与 PIT 派生，直接造一条投影。
        少了这条 guard，"single publication entry point" 与 "contract-issued = CLOSED"
        就只是命名约定。

        与 R27-A 的 ``_issue_evidence_ref`` 同一模式：不需要隐藏函数，只需要把
        **谁可以调用它** 变成可执行的边界。
        """
        offenders = []
        for name in _production_modules():
            if name == PROJECTION_MODULE:
                continue
            if _issuer_calls(_tree(name)):
                offenders.append(name)
        self.assertEqual(
            [], offenders,
            f"契约模块之外出现了私有签发调用：{offenders}。"
            f"{PRIVATE_ISSUER} 只能由 {sorted(ALLOWED_ISSUER_CALLERS)} 调用。",
        )

        module_calls = _issuer_calls(_tree(PROJECTION_MODULE))
        self.assertTrue(module_calls, "非空性：owner 模块必须真的在调用私有签发口")
        callers = {func for func, _ in module_calls}
        self.assertEqual(
            ALLOWED_ISSUER_CALLERS, callers,
            f"私有签发口的调用点发生变化：{sorted(callers)}",
        )

    def test_EXFACT_19b_issuer_scanner_is_not_vacuous(self):
        """EXFACT-19b：扫描器必须能区分"owner 工厂调用"与"别的 production 函数调用"。"""
        legal = ast.parse(
            "def fact_projection(evidence):\n"
            "    return _issue_fact_projection(version='v')\n"
        )
        self.assertEqual([("fact_projection", 2)], _issuer_calls(legal))

        illegal = ast.parse(
            "def another_production_function(fields):\n"
            "    return _issue_fact_projection(**fields)\n"
        )
        self.assertEqual([("another_production_function", 2)], _issuer_calls(illegal))
        self.assertNotEqual(
            ALLOWED_ISSUER_CALLERS,
            {func for func, _ in _issuer_calls(illegal)},
            "第二个 production 调用点没有被判为越界",
        )

        # 别名同样要被识破（否则 `... import X as issue` 就绕过了这条边界）。
        aliased = ast.parse(
            "from execution_verification import _issue_fact_projection as issue\n"
            "def somewhere_else(fields):\n"
            "    return issue(**fields)\n"
        )
        self.assertEqual([("somewhere_else", 3)], _issuer_calls(aliased))

        # 属性形式也一律计入：同名方法不可能是本模块的签发口，但**宁可从严**——
        # 这条边界的目标是"找不到第二个调用点"，而不是"精确分类每一个同名调用"。
        unrelated = ast.parse("def f():\n    return obj._issue_fact_projection()\n")
        self.assertEqual(1, len(_issuer_calls(unrelated)))


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
