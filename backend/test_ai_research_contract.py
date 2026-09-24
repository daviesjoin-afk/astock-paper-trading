# -*- coding: utf-8 -*-
"""R27-A —— AI Information & Research Contract 的语义与依赖方向回归。

存在理由是这个不变量：

    **AI 只能读取可信事实并产出 research 结论；它永远不是 Market Data /
    Signal / Execution / Risk / Promotion 的 authority。**

分四组：

    AI-*         contract 语义：两个维度分离、冲突、深冻结、PIT
    AI-TYPED-*   类型化 evidence：identity 由 R24 投影派生，调用方不提供
    AIG-*        architecture guard：依赖方向、"AI 不能 commit signal"、无 IO/时钟
    非空性        护栏必须真的能失败，否则它只是装饰

时间一律**显式**传入（固定的业务日字符串），绝不 ``time.sleep()``、绝不读墙上时钟 ——
本轮的 PIT 语义恰恰是最容易假绿的地方。

刻意**不**起数据库：AIG-05 要证明的是"writer 在触碰任何连接之前就拒绝一个非裁决的
decision"，而 ``commit_signal`` 的校验顺序正好让这一点可以用 ``conn=None`` 验证。
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ai_research_contract as ARC
import market_data_contract as MDC
import signal_service as SIG

BACKEND = os.path.dirname(os.path.abspath(__file__))
CONTRACT_MODULE = "ai_research_contract.py"

#: 固定的业务日 —— 所有 PIT 断言都相对它，与本机时钟无关。
DAY = "2026-08-27"
NEXT_DAY = "2026-08-28"
PREV_DAY = "2026-08-26"

#: 默认 reading 的 owner 口径与观测时点 —— evidence identity 由它们**派生**，
#: 调用方不再提供 source_id（见 AI-TYPED-02）。
DEFAULT_POLICY = "live_market"


def _derived_identity(*, policy: str = DEFAULT_POLICY, as_of: str = DAY) -> str:
    """R24 投影派生出的 evidence identity：``policy@观测时点``。"""
    return f"{policy}@{as_of}T10:30:00+08:00"

#: 只有这一个项目模块允许被 AI 契约 import：R24 的纯行情契约（**读**事实）。
ALLOWED_PROJECT_IMPORTS = {"market_data_contract"}
ALLOWED_STDLIB_IMPORTS = {"__future__", "dataclasses", "types", "typing"}

#: 现有 authority owner。它们**不得**反向 import AI 研究层 —— AI 是消费者。
#: 按 authority 领域分组，新增 authority 时同步登记。
AUTHORITY_MODULES = (
    # R24 market data
    "market_data_contract.py",
    "market_data_service.py",
    "data_fetcher.py",
    # R25 signal
    "signal_service.py",
    "strategy_selection_resolver.py",
    "strategy_selection_provenance.py",
    # R26 execution
    "execution_planner.py",
    "execution_evidence.py",
    "execution_dispatch.py",
    "execution_lifecycle.py",
    "execution_verification.py",
    "execution_outcome.py",
    "manual_orders.py",
    # risk
    "paper_risk_service.py",
    "paper_risk_decision.py",
    "paper_risk_evidence.py",
    "paper_risk_exit_eligibility.py",
    "paper_risk_scan_state.py",
    "risk_center.py",
    # ledger / persistence
    "paper_trading.py",
    "paper_repository.py",
    "paper_storage.py",
    "paper_schema_migrations.py",
    "paper_capital_reservations.py",
    # promotion / evolution
    "promotion_science.py",
    "self_evolution.py",
    "evolution_apply.py",
    "strategy_champion.py",
)

#: 允许 import AI 研究层的生产模块。**现在为空**：R27-A 只建立契约，没有任何生产
#: 消费者。将来接入时必须显式加入，使"谁依赖了 AI"是一次有意识的决定。
ALLOWED_AI_CONSUMERS: set[str] = set()

#: 时钟 / 随机数 / IO —— 研究契约一旦读它们，就能拿 current state 回填历史。
FORBIDDEN_CLOCK_CALLS = (
    "now", "utcnow", "today", "time", "monotonic", "perf_counter",
    "randint", "random", "uuid4", "getenv", "environ",
)
FORBIDDEN_IO_IMPORTS = (
    "sqlite3", "json", "requests", "urllib", "httpx", "socket", "time",
    "random", "uuid", "os", "pathlib", "subprocess", "logging",
    "paper_trading", "paper_storage", "paper_repository", "data_fetcher",
)


# ---------------------------------------------------------------------------
# helpers —— 与既有 guard 同一套（AST / import-level，不做脆弱子串搜索）
# ---------------------------------------------------------------------------


def _source(name: str) -> str:
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


def _tree(name: str) -> ast.Module:
    return ast.parse(_source(name))


def _imported_roots(tree: ast.Module) -> set[str]:
    """所有 import 的**根模块名**（``a.b`` → ``a``；``from a import b`` → ``a``）。"""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_names(tree: ast.Module) -> list[str]:
    """所有 import 的完整模块名（用于判断是否 import 了某个具体模块）。"""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _called_names(tree: ast.Module) -> set[str]:
    """树里出现的所有被调用名（``f(...)`` 的 ``f`` / ``obj.m(...)`` 的 ``m``）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _code_string_constants(tree: ast.Module) -> list[str]:
    """docstring 之外的字符串常量（即真正的 SQL / 命令文本）。"""
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


def _reading(
    verification: str = MDC.VERIFICATION_VERIFIED,
    method: str = MDC.VERIFICATION_METHOD_CROSS_SOURCE,
    *, as_of: str = DAY, policy: str = "live_market", observed_at: str | None = None,
):
    """一个 R24 typed projection（``MarketDataReading``）。

    刻意手工构造：这正是本契约**无法**排除的伪造路径（``MarketDataReading`` 是公开
    dataclass）。测试 helper 里保留它，是为了让"两步伪造"这条限制可见，而不是假装
    它不存在 —— 见 AI-TYPED-01。
    """
    return MDC.MarketDataReading(
        availability=MDC.AVAILABILITY_AVAILABLE,
        freshness=MDC.FRESHNESS_FRESH,
        status=MDC.STATUS_FRESH,
        policy_name=policy,
        snapshot=MDC.MarketDataSnapshot(
            kind="symbol_quote", as_of=as_of,
            observed_at=observed_at or f"{as_of}T10:30:00+08:00",
            verification=verification, verification_method=method,
        ),
    )


def _ref(*, verification=MDC.VERIFICATION_VERIFIED,
         method=MDC.VERIFICATION_METHOD_CROSS_SOURCE, as_of=DAY,
         policy="live_market", observed_at=None):
    """一条由唯一 factory 签发的证据引用（identity 由 R24 投影派生）。"""
    return ARC.evidence_ref_from_market_reading(
        _reading(verification, method, as_of=as_of, policy=policy, observed_at=observed_at),
    )


def _evidence(ref, relation=ARC.RELATION_SUPPORTS):
    return ARC.HypothesisEvidence(ref=ref, relation=relation)


def _hypothesis(*, as_of=DAY, evidence=(), confidence=0.5):
    return ARC.ResearchHypothesis(
        hypothesis_id="H-1", as_of=as_of, subject="600000",
        thesis="momentum persists into the next session",
        evidence=tuple(evidence), confidence=confidence,
    )


# ---------------------------------------------------------------------------
# AI-01 ~ AI-05 —— 事实层面
# ---------------------------------------------------------------------------


class AiResearchFactTests(unittest.TestCase):
    """事实观测与证据引用：只能回答事实层面的问题。"""

    def test_AI01_verified_market_evidence_yields_a_verified_information_event(self):
        """AI-01：已核验的行情事实 → 合法的 InformationEvent，状态被逐字保留。"""
        ref = _ref()
        event = ARC.InformationEvent(
            as_of=DAY, source="market_data_service.read_snapshot", evidence_ref=ref,
            payload={"price": 10.5},
        )
        self.assertEqual(ARC.EVENT_MARKET_OBSERVED, event.kind)
        self.assertEqual(MDC.VERIFICATION_VERIFIED, event.verification)
        self.assertEqual(MDC.VERIFICATION_METHOD_CROSS_SOURCE, event.verification_method)
        self.assertEqual(_derived_identity(), event.evidence_id)
        self.assertTrue(ref.cross_source_verified)

        # kind 与 source_type 一一对应，且派生只读 —— 错标无法表达。
        self.assertEqual(ARC.EVENT_MARKET_OBSERVED, event.kind)
        for source_type, expected in (
            (ARC.EVIDENCE_SOURCE_MARKET_DATA, ARC.EVENT_MARKET_OBSERVED),
            (ARC.EVIDENCE_SOURCE_SIGNAL, ARC.EVENT_SIGNAL_OBSERVED),
            (ARC.EVIDENCE_SOURCE_EXECUTION, ARC.EVENT_EXECUTION_OBSERVED),
            (ARC.EVIDENCE_SOURCE_STRATEGY_RESEARCH, ARC.EVENT_STRATEGY_RESEARCH_OBSERVED),
            (ARC.EVIDENCE_SOURCE_PORTFOLIO_RESEARCH, ARC.EVENT_PORTFOLIO_RESEARCH_OBSERVED),
            (ARC.EVIDENCE_SOURCE_NEWS, ARC.EVENT_NEWS_OBSERVED),
        ):
            with self.subTest(source_type=source_type):
                self.assertEqual(expected, ARC._KIND_BY_SOURCE_TYPE[source_type])
        self.assertEqual(len(ARC._KIND_BY_SOURCE_TYPE), len(ARC.EVIDENCE_SOURCE_TYPES))

        projected = event.projection()
        self.assertEqual(MDC.VERIFICATION_VERIFIED, projected["verification"])
        self.assertEqual(MDC.VERIFICATION_METHOD_CROSS_SOURCE, projected["verification_method"])

    def test_AI02_unverified_evidence_keeps_its_original_state(self):
        """AI-02：未核验的事实保持原状，绝不升级成 verified。"""
        single = _ref(verification=MDC.VERIFICATION_SINGLE_SOURCE)
        self.assertEqual(MDC.VERIFICATION_SINGLE_SOURCE, single.verification)
        self.assertFalse(
            single.cross_source_verified,
            "单源证据不得自称通过了逐票双源交叉核验",
        )

        event = ARC.InformationEvent(as_of=DAY, source="s", evidence_ref=single)
        self.assertEqual(MDC.VERIFICATION_SINGLE_SOURCE, event.verification)

        untouched = MDC.MarketDataReading(
            availability=MDC.AVAILABILITY_AVAILABLE, freshness=MDC.FRESHNESS_STALE,
            status=MDC.STATUS_STALE, policy_name="live_market",
            snapshot=MDC.MarketDataSnapshot(
                kind="symbol_quote", as_of=DAY, observed_at=f"{DAY}T10:30:00+08:00",
            ),
        )
        stale_ref = ARC.evidence_ref_from_market_reading(untouched)
        self.assertEqual(MDC.VERIFICATION_NOT_ATTEMPTED, stale_ref.verification)

        # 非法核验组合（verified 配 none）在构造期就被拒绝，而不是被降级。
        with self.assertRaises(ValueError) as caught:
            ARC._issue_evidence_ref(
                source_type=ARC.EVIDENCE_SOURCE_MARKET_DATA, source_id="x", as_of=DAY,
                verification=MDC.VERIFICATION_VERIFIED,
                verification_method=MDC.VERIFICATION_METHOD_NONE, detail={},
            )
        self.assertIn("illegal verification pair", str(caught.exception))

    def test_AI03_hypothesis_referencing_future_evidence_is_rejected(self):
        """AI-03：引用未来证据的假设在构造期被拒绝，而不是静默过滤。"""
        future = _ref(as_of=NEXT_DAY)
        with self.assertRaises(ValueError) as caught:
            _hypothesis(as_of=DAY, evidence=(_evidence(future),))
        message = str(caught.exception)
        self.assertIn("future evidence", message)
        self.assertIn(NEXT_DAY, message, "错误信息必须点名那条未来事实")

        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, _hypothesis(evidence=(_evidence(_ref()),)).status)

        # 历史假设引用"今天"的事实同样被拒 —— PIT 的 look-ahead 漏洞。
        with self.assertRaises(ValueError):
            _hypothesis(as_of=PREV_DAY, evidence=(_evidence(_ref(as_of=DAY)),))

    def test_AI04_hypothesis_without_evidence_is_insufficient_not_approved(self):
        """AI-04：没有证据的假设是 insufficient_evidence，绝不默认批准。"""
        empty = _hypothesis(evidence=())
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, empty.status)
        self.assertEqual(ARC.RESEARCH_REASON_NO_EVIDENCE, empty.reason)
        self.assertFalse(empty.is_supported)

        confident = _hypothesis(evidence=(), confidence=1.0)
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, confident.status)

        with self.assertRaises(ARC.UnsupportedResearch):
            empty.require_supported()

    def test_AI05_historical_hypothesis_cannot_read_current_state(self):
        """AI-05：历史假设不读取 current state —— as-of 必须显式且证明得出来。"""
        for bad in (None, "", "not-a-day", "2026-13-45"):
            with self.subTest(as_of=bad):
                with self.assertRaises(ValueError):
                    _hypothesis(as_of=bad)

        # R24 投影若没有可证明的业务日（unavailable reading），拒绝映射。
        with self.assertRaises(ValueError):
            ARC.evidence_ref_from_market_reading(
                MDC.unavailable_reading(MDC.LIVE_MARKET_POLICY),
            )

        # 契约本身没有读时钟的入口 —— 这是"无法回填 current"的结构性保证。
        tree = _tree(CONTRACT_MODULE)
        called = _called_names(tree)
        for forbidden in FORBIDDEN_CLOCK_CALLS:
            with self.subTest(call=forbidden):
                self.assertNotIn(
                    forbidden, called,
                    f"{CONTRACT_MODULE} 调用了 {forbidden} —— 研究契约不得读时钟/随机/环境",
                )
        self.assertNotIn("time", _imported_roots(tree))
        self.assertNotIn("datetime", _imported_roots(tree))

        historical = _hypothesis(
            evidence=(_evidence(_ref(as_of=PREV_DAY)), _evidence(_ref(as_of=DAY))),
        )
        self.assertEqual((), historical.look_ahead_refs)
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, historical.status)


# ---------------------------------------------------------------------------
# AI-09 ~ AI-15 —— 两个维度的分离与冲突
# ---------------------------------------------------------------------------


class AiResearchRelationTests(unittest.TestCase):
    """P1-1：fact verification 与 hypothesis relation 是两个正交维度。"""

    def test_AI09_verified_fact_with_context_relation_is_not_supported(self):
        """AI-09：verified 事实 + relation=context → **不**支持 hypothesis。

        verified 只说明"这个事实可信"，不说明"它支持当前 thesis"。一条可信报价
        对"下一日 momentum 会继续"只是背景信息。
        """
        hypothesis = _hypothesis(evidence=(_evidence(_ref(), ARC.RELATION_CONTEXT),))
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
        self.assertEqual(ARC.RESEARCH_REASON_NO_SUPPORTING_EVIDENCE, hypothesis.reason)
        self.assertFalse(hypothesis.is_supported)

        # reason 必须与事实层的结论一致：事实**已经** verified，所以原因不能是
        # evidence_not_verified —— 那会把刚拆开的两个维度又混回去。
        self.assertNotEqual(ARC.RESEARCH_REASON_EVIDENCE_NOT_VERIFIED, hypothesis.reason)
        self.assertNotEqual(ARC.RESEARCH_REASON_EVIDENCE_UNAVAILABLE, hypothesis.reason)

    def test_AI10_verified_fact_with_supports_relation_is_supported(self):
        """AI-10：verified 事实 + relation=supports → supported。"""
        hypothesis = _hypothesis(evidence=(_evidence(_ref(), ARC.RELATION_SUPPORTS),))
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, hypothesis.status)
        self.assertIsNone(hypothesis.reason)
        self.assertTrue(hypothesis.is_supported)

    def test_AI11_verified_fact_with_contradicts_relation_is_unsupported(self):
        """AI-11：verified 事实 + relation=contradicts → unsupported。"""
        hypothesis = _hypothesis(evidence=(_evidence(_ref(), ARC.RELATION_CONTRADICTS),))
        self.assertEqual(ARC.HYPOTHESIS_UNSUPPORTED, hypothesis.status)
        self.assertEqual(ARC.RESEARCH_REASON_EVIDENCE_CONTRADICTED, hypothesis.reason)

        # 可信支持与可信反对并存 → 反对优先（保守），不"票数过半"。
        mixed = _hypothesis(evidence=(
            _evidence(_ref(), ARC.RELATION_SUPPORTS),
            _evidence(_ref(policy="close_snapshot", observed_at=f"{DAY}T15:00:00+08:00"),
                      ARC.RELATION_CONTRADICTS),
        ))
        self.assertEqual(ARC.HYPOTHESIS_UNSUPPORTED, mixed.status)

    def test_AI12_unverified_fact_with_supports_is_insufficient(self):
        """AI-12：未核验事实 + supports → insufficient_evidence（不是 supported）。"""
        hypothesis = _hypothesis(
            evidence=(_evidence(_ref(verification=MDC.VERIFICATION_SINGLE_SOURCE), ), ),
        )
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
        self.assertEqual(ARC.RESEARCH_REASON_EVIDENCE_NOT_VERIFIED, hypothesis.reason)

    def test_AI13_unavailable_source_does_not_contradict_the_thesis(self):
        """AI-13：source unavailable + supports → insufficient_evidence，**不是** unsupported。

        provider disagreement / unavailable 只说明"这条 evidence 本身不能成为可靠依据"，
        不说明"这个 thesis 被事实反驳"。两者必须完全独立。
        """
        for verification in (MDC.VERIFICATION_UNAVAILABLE, MDC.VERIFICATION_DISAGREEMENT):
            with self.subTest(verification=verification):
                ref = _ref(verification=verification)
                hypothesis = _hypothesis(evidence=(_evidence(ref, ARC.RELATION_SUPPORTS),))
                self.assertEqual(
                    ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status,
                    "来源不可用/冲突不得自动升级为 thesis 被反驳",
                )
                self.assertEqual(
                    ARC.RESEARCH_REASON_EVIDENCE_UNAVAILABLE, hypothesis.reason,
                )

        # 与"可信事实反对"是两个结论：原因必须不同。
        contradicted = _hypothesis(evidence=(_evidence(_ref(), ARC.RELATION_CONTRADICTS),))
        self.assertNotEqual(ARC.RESEARCH_REASON_EVIDENCE_UNAVAILABLE, contradicted.reason)

    def test_AI14_conflicting_duplicate_evidence_fails_closed_order_independently(self):
        """AI-14：同 identity 但事实状态不同的证据 → fail closed，且顺序无关。"""
        verified = _ref()                                     # verified / cross_source
        disagreement = _ref(verification=MDC.VERIFICATION_DISAGREEMENT)
        self.assertEqual(verified.identity(), disagreement.identity())

        for label, order in (("A,B", (verified, disagreement)),
                             ("B,A", (disagreement, verified))):
            with self.subTest(order=label):
                with self.assertRaises(ARC.EvidenceConflict):
                    _hypothesis(evidence=tuple(_evidence(ref) for ref in order))

        # 完全相同（含 detail）→ 安全去重，不误报。
        identical = _hypothesis(evidence=(_evidence(verified), _evidence(verified)))
        self.assertEqual(1, len(identical.evidence))
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, identical.status)
        self.assertEqual(1, identical.projection()["evidence_count"])

    def test_AI15_conflicting_relation_on_the_same_evidence_fails_closed(self):
        """AI-15：同一条 evidence 同时 supports + contradicts → fail closed，顺序无关。"""
        ref = _ref()
        for label, order in (("supports,contradicts",
                              ((ref, ARC.RELATION_SUPPORTS), (ref, ARC.RELATION_CONTRADICTS))),
                             ("contradicts,supports",
                              ((ref, ARC.RELATION_CONTRADICTS), (ref, ARC.RELATION_SUPPORTS)))):
            with self.subTest(order=label):
                with self.assertRaises(ARC.EvidenceRelationConflict):
                    _hypothesis(evidence=tuple(_evidence(r, rel) for r, rel in order))

        # 同一 relation 重复不是冲突。
        same = _hypothesis(evidence=(_evidence(ref, ARC.RELATION_CONTEXT),
                                     _evidence(ref, ARC.RELATION_CONTEXT)))
        self.assertEqual(1, len(same.evidence))

    def test_AI16_relation_vocabulary_is_a_minimal_closed_set(self):
        """AI-16：relation 是最小闭集，不含评分档位。"""
        self.assertEqual(
            (ARC.RELATION_SUPPORTS, ARC.RELATION_CONTRADICTS, ARC.RELATION_CONTEXT),
            ARC.RELATIONS,
        )
        for forbidden in ("strong_support", "weak_support", "neutral_positive",
                          "negative", "uncertain", "support"):
            with self.subTest(relation=forbidden):
                with self.assertRaises(ValueError):
                    _evidence(_ref(), forbidden)


# ---------------------------------------------------------------------------
# AI-TYPED-* —— 类型化 evidence：identity 由 owner 投影派生
# ---------------------------------------------------------------------------


class AiResearchTypedEvidenceTests(unittest.TestCase):
    """P1-2（修正版）：调用方不能提供 identity，也不能提供核验结论。

    前一轮把这一层描述成 "owner-issued provenance" 是**过度声称**：
    ``MarketDataReading`` / ``MarketDataSnapshot`` 都是公开 dataclass，一个手工拼出来的
    reading 与本层不可区分。本轮改为诚实命名，并把边界写成可执行的断言。
    """

    def test_AI_TYPED_01_raw_caller_cannot_construct_evidence(self):
        """AI-TYPED-01：没有公开 raw 构造器，身份与核验结论都不来自调用方。"""
        for attempt in (
            {"source_type": ARC.EVIDENCE_SOURCE_MARKET_DATA, "source_id": "fake",
             "as_of": DAY, "verification": MDC.VERIFICATION_VERIFIED,
             "verification_method": MDC.VERIFICATION_METHOD_CROSS_SOURCE},
            {"source_type": ARC.EVIDENCE_SOURCE_NEWS, "source_id": "fake",
             "as_of": DAY, "verification": MDC.VERIFICATION_VERIFIED,
             "verification_method": MDC.VERIFICATION_METHOD_CROSS_SOURCE},
            {"source_type": ARC.EVIDENCE_SOURCE_EXECUTION, "source_id": "fake", "as_of": DAY},
        ):
            with self.subTest(source_type=attempt["source_type"]):
                with self.assertRaises(TypeError) as caught:
                    ARC.ResearchEvidenceRef(**attempt)
                self.assertIn("no public constructor", str(caught.exception))

        self.assertFalse(hasattr(ARC, "_OWNER_ISSUED"),
                         "不得存在可 import 的构造哨兵（那是伪安全）")

    def test_AI_TYPED_02_identity_is_derived_from_the_owner_projection(self):
        """AI-TYPED-02：identity 由 R24 投影派生，调用方无法命名或改名。

        这是本轮复审的核心修正：``source_id`` 曾经是 caller 的自由字符串，于是同一份
        真实 reading 可以被重命名成 FACT_A / FACT_B / FACT_C，绕过
        ``(source_type, source_id, as_of)`` 的 duplicate / conflict identity。
        """
        signature = inspect.signature(ARC.evidence_ref_from_market_reading)
        self.assertEqual(
            ["reading"], list(signature.parameters),
            "factory 不得接受调用方提供的 source_id —— 一个由调用方命名的 identity "
            "不是 identity，而是一个能绕过去重与冲突检测的自由字符串",
        )

        first = ARC.evidence_ref_from_market_reading(_reading())
        second = ARC.evidence_ref_from_market_reading(_reading())
        self.assertEqual(_derived_identity(), first.source_id)
        self.assertEqual(first.identity(), second.identity(),
                         "同一份事实必须派生出同一个 identity")

        # 不同快照（观测时点不同）→ 不同 identity：这是真实的另一份事实。
        other = ARC.evidence_ref_from_market_reading(
            _reading(observed_at=f"{DAY}T14:00:00+08:00"),
        )
        self.assertNotEqual(first.identity(), other.identity())

        # 不同口径（policy）→ 不同 identity。
        other_policy = ARC.evidence_ref_from_market_reading(_reading(policy="close_snapshot"))
        self.assertNotEqual(first.identity(), other_policy.identity())

        # 同一份事实无法被造成两条：duplicate 检查因此不可被绕过。
        hypothesis = _hypothesis(
            evidence=(_evidence(first), _evidence(ARC.evidence_ref_from_market_reading(_reading()))),
        )
        self.assertEqual(1, len(hypothesis.evidence))

    def test_AI_TYPED_03_verification_and_asof_are_copied_verbatim(self):
        """AI-TYPED-03：核验维度与业务日逐字来自投影，不被改写也不被升级。"""
        reading = _reading(as_of=DAY)
        ref = ARC.evidence_ref_from_market_reading(reading)
        projected = reading.projection()

        self.assertEqual(ARC.EVIDENCE_SOURCE_MARKET_DATA, ref.source_type)
        self.assertEqual(projected["as_of"], ref.as_of)
        self.assertEqual(DAY, ref.as_of)
        self.assertEqual(projected["verification"], ref.verification)
        self.assertEqual(projected["verification_method"], ref.verification_method)

        single = ARC.evidence_ref_from_market_reading(
            _reading(MDC.VERIFICATION_SINGLE_SOURCE),
        )
        self.assertEqual(MDC.VERIFICATION_SINGLE_SOURCE, single.verification)
        self.assertNotEqual(MDC.VERIFICATION_VERIFIED, single.verification)
        self.assertFalse(single.cross_source_verified)
        self.assertEqual(
            ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, _hypothesis(evidence=(_evidence(single),)).status,
        )

    def test_AI_TYPED_04_unprovable_business_day_fails_closed(self):
        """AI-TYPED-04：无法证明业务日的 R24 投影必须 fail closed。"""
        # 完全没有 snapshot（unavailable）→ 没有可证明的业务日。
        with self.assertRaises(ValueError):
            ARC.evidence_ref_from_market_reading(
                MDC.unavailable_reading(MDC.LIVE_MARKET_POLICY),
            )

        # duck-typed 对象（带 projection() 但不是 R24 类型）同样拒绝。
        class FakeReading:
            def projection(self):
                return {"as_of": DAY, "policy": "live_market",
                        "verification": MDC.VERIFICATION_VERIFIED,
                        "verification_method": MDC.VERIFICATION_METHOD_CROSS_SOURCE}

        with self.assertRaises(TypeError) as caught:
            ARC.evidence_ref_from_market_reading(FakeReading())
        self.assertIn("R24 projection", str(caught.exception))

        # 缺 policy 的投影无法派生稳定 identity → 拒绝。
        with self.assertRaises(ValueError):
            ARC._market_evidence_identity({"as_of": DAY, "observed_at": f"{DAY}T10:00:00+08:00"})

    def test_AI_TYPED_05_future_projection_cannot_enter_a_historical_hypothesis(self):
        """AI-TYPED-05：未来的 R24 投影不得进入历史 hypothesis。"""
        future = ARC.evidence_ref_from_market_reading(_reading(as_of=NEXT_DAY))
        with self.assertRaises(ValueError) as caught:
            _hypothesis(as_of=DAY, evidence=(_evidence(future),))
        self.assertIn("future evidence", str(caught.exception))

    def test_AI_TYPED_06_two_step_forgery_is_documented_not_claimed_closed(self):
        """AI-TYPED-06：诚实记录本层**不能**排除的路径（两步伪造）。

        ``MarketDataReading`` 是公开 dataclass，因此下列路径在本层是**可以通过**的：

            手工造 MarketDataSnapshot(verified, cross_source)
                → 手工造 MarketDataReading
                → evidence_ref_from_market_reading(...)
                → 得到一条 verification=verified 的 ref

        本层把它作为**已知限制**断言下来，而不是假装已经封堵。真正的修复需要 R24 自己
        签发 evidence token（属于 R24 的职责，不在 R27-A 范围内）。
        """
        forged = ARC.evidence_ref_from_market_reading(
            _reading(MDC.VERIFICATION_VERIFIED, policy="forged_policy"),
        )
        self.assertEqual(MDC.VERIFICATION_VERIFIED, forged.verification)
        self.assertEqual(ARC.EVIDENCE_SOURCE_MARKET_DATA, forged.source_type)

        # 但伪造者仍然**无法**选择 identity：它只能是投影派生出来的那个值。
        self.assertEqual(_derived_identity(policy="forged_policy"), forged.source_id)


# ---------------------------------------------------------------------------
# AI-17 / AI-18（续）—— 深冻结与投影
# ---------------------------------------------------------------------------


class AiResearchImmutabilityTests(unittest.TestCase):
    """P2-2：payload / detail 必须**递归**不可变。"""

    def test_AI17_nested_payload_is_deeply_frozen(self):
        """AI-17：嵌套 dict / list / set 全部冻结，原始输入无法改写已记录内容。"""
        original = {
            "nested": {"items": [1, 2], "tags": {"a"}, "inner": {"deep": [3]}},
            "top": 1,
        }
        event = ARC.InformationEvent(
            as_of=DAY, source="s", evidence_ref=_ref(), payload=original,
        )
        ref = ARC.evidence_ref_from_market_reading(_reading())

        # 调用方保留的原始对象继续被改写 —— 契约内部值必须不变。
        original["nested"]["items"].append(9)
        original["nested"]["new_key"] = "leak"
        original["nested"]["inner"]["deep"].append(9)

        self.assertEqual((1, 2), event.payload["nested"]["items"])
        self.assertEqual(("inner", "items", "tags"), tuple(sorted(event.payload["nested"])))
        self.assertEqual((3,), event.payload["nested"]["inner"]["deep"])
        self.assertNotIn("new_key", event.payload["nested"])

        # 容器类型被规范化成不可变形态。
        self.assertIsInstance(event.payload["nested"]["items"], tuple)
        self.assertIsInstance(event.payload["nested"]["tags"], frozenset)
        self.assertIsInstance(ref.detail, type(event.payload))
        self.assertIsInstance(ref.detail["policy"], str)

        # 通过契约对象写入被拒绝。
        with self.assertRaises(TypeError):
            event.payload["nested"]["x"] = 1
        with self.assertRaises(TypeError):
            event.payload["nested"]["items"] += (4,)
        with self.assertRaises(TypeError):
            ref.detail["policy"] = "changed"
        self.assertEqual(_derived_identity(), ref.source_id)

    def test_AI18_non_json_payload_values_are_rejected(self):
        """AI-18：只接受 JSON-like 值；任意可变对象 fail closed。"""
        for bad in (object(), bytearray(b"x"), {"k": object()}, [object()]):
            with self.subTest(value=type(bad).__name__):
                with self.assertRaises(TypeError):
                    ARC.InformationEvent(
                        as_of=DAY, source="s", evidence_ref=_ref(), payload={"v": bad},
                    )

        # 标量与 None 是允许的。
        event = ARC.InformationEvent(
            as_of=DAY, source="s", evidence_ref=_ref(),
            payload={"price": 10.5, "flag": True, "note": None, "count": 3, "b": b"x"},
        )
        self.assertEqual(10.5, event.payload["price"])
        self.assertIsNone(event.payload["note"])

    def test_AI19_hypothesis_projection_declares_itself_non_authoritative(self):
        """AI-19：投影自带"这只是研究"的结论，供下游离线判断。"""
        hypothesis = _hypothesis(
            evidence=(_evidence(_ref(), ARC.RELATION_SUPPORTS),), confidence=0.62,
        )
        projected = hypothesis.projection()

        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, projected["status"])
        self.assertIsNone(projected["reason"])
        self.assertEqual("research", projected["authority"])
        self.assertIs(False, projected["is_authoritative"])
        self.assertIs(False, hypothesis.is_authoritative)
        self.assertEqual(1, projected["evidence_count"])
        self.assertEqual(ARC.RELATION_SUPPORTS, projected["evidence"][0]["relation"])
        self.assertEqual(MDC.VERIFICATION_VERIFIED, projected["evidence"][0]["verification"])
        self.assertEqual(DAY, projected["evidence"][0]["as_of"])

        weak = _hypothesis(evidence=()).projection()
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, weak["status"])
        self.assertEqual(ARC.RESEARCH_REASON_NO_EVIDENCE, weak["reason"])

    def test_AI20_research_vocabulary_never_overlaps_the_signal_lifecycle(self):
        """AI-20：研究词汇与 signal 生命周期不相交 —— 结论无法直接落成 signal。"""
        signal_lifecycle = {"pending", "approved", "blocked", "waitlist", "recovery"}
        overlap = set(ARC.HYPOTHESIS_STATUSES) & signal_lifecycle
        self.assertEqual(
            set(), overlap,
            f"研究状态与 paper_signals 生命周期重叠：{sorted(overlap)} —— "
            "重叠会让 supported 被当作 approved 使用",
        )
        self.assertEqual(set(), set(ARC.HYPOTHESIS_STATUSES) & {"approved", "blocked"})


# ---------------------------------------------------------------------------
# AIG-* —— architecture guard
# ---------------------------------------------------------------------------


class AiResearchArchitectureGuardTests(unittest.TestCase):
    """AIG-01 ~ AIG-06：AI 是消费者，不得进入既有 authority；反之亦然。"""

    def test_AIG01_contract_depends_only_on_stdlib_and_the_r24_read_contract(self):
        """AIG-01：AI 契约只依赖 stdlib 与 R24 的**读**契约。"""
        roots = _imported_roots(_tree(CONTRACT_MODULE))
        project_modules = {path[:-3] for path in os.listdir(BACKEND) if path.endswith(".py")}

        leaked = sorted((roots & project_modules) - ALLOWED_PROJECT_IMPORTS)
        self.assertEqual(
            [], leaked,
            f"{CONTRACT_MODULE} import 了未登记的项目模块 {leaked}；"
            "AI 层只允许 import market_data_contract（R24 纯契约）",
        )
        self.assertEqual(
            [], sorted(roots - ALLOWED_STDLIB_IMPORTS - ALLOWED_PROJECT_IMPORTS),
            "出现了未登记的 import，请审慎评估后再放行",
        )

    def test_AIG02_no_authority_module_imports_the_ai_research_layer(self):
        """AIG-02：现有 authority 不得 import AI 研究层（依赖方向单向）。"""
        offenders = []
        for name in AUTHORITY_MODULES:
            for imported in _imported_names(_tree(name)):
                if imported.split(".")[0] == "ai_research_contract":
                    offenders.append(f"{name}: import {imported}")
        self.assertEqual(
            [], offenders,
            "authority 反向 import 了 AI 研究层 —— AI 必须是纯消费者",
        )

    def test_AIG03_no_production_module_consumes_the_ai_layer_without_registration(self):
        """AIG-03：生产链路依赖 AI 层必须显式登记（当前为 0）。"""
        offenders = []
        for name in sorted(os.listdir(BACKEND)):
            if not name.endswith(".py") or name == CONTRACT_MODULE:
                continue
            if name.startswith("test_") or name in ALLOWED_AI_CONSUMERS:
                continue
            for imported in _imported_names(_tree(name)):
                if imported.split(".")[0] == "ai_research_contract":
                    offenders.append(f"{name}: import {imported}")
        self.assertEqual(
            [], offenders,
            "有生产模块在未登记的情况下 import 了 AI 研究层。接入必须先把该模块加入 "
            "ALLOWED_AI_CONSUMERS，使'谁依赖了 AI'成为一次有意识的决定",
        )

    def test_AIG04_contract_has_no_io_dependency(self):
        """AIG-04：研究契约不碰 DB / 网络 / 文件 / 环境。"""
        roots = _imported_roots(_tree(CONTRACT_MODULE))
        for forbidden in FORBIDDEN_IO_IMPORTS:
            with self.subTest(module=forbidden):
                self.assertNotIn(
                    forbidden, roots,
                    f"{CONTRACT_MODULE} import 了 {forbidden} —— 研究契约必须是纯契约",
                )

        for text in _code_string_constants(_tree(CONTRACT_MODULE)):
            upper = text.upper()
            for keyword in ("INSERT INTO", "UPDATE ", "DELETE FROM", "SELECT "):
                with self.subTest(keyword=keyword, text=text[:40]):
                    self.assertNotIn(
                        keyword, upper,
                        f"{CONTRACT_MODULE} 出现了 SQL 文本 —— 本轮不建立持久化",
                    )

    def test_AIG05_ai_output_cannot_be_committed_as_a_signal(self):
        """AIG-05：AI 产物无法被当作裁决提交成正式 signal。

        行为证明，而不是约定：``commit_signal`` 在触碰连接之前就要求一个真正的
        ``SignalDecision``，因此把研究假设（或其投影、或其状态字符串）传进去会在
        **任何落库发生前**失败（``conn=None`` 足以验证 —— 校验顺序决定了它先于
        ``conn.execute``）。刻意不修改 ``signal_service`` 来迁就 AI。
        """
        hypothesis = _hypothesis(evidence=(_evidence(_ref()),))
        context = SIG.SignalWriteContext(
            account_id="acct-1", cycle_id=1, strategy_id="strat-1",
            strategy_version=1, strategy_checksum="0" * 64, asof_day=DAY,
        )
        for forged in (hypothesis, hypothesis.projection(), "supported", None):
            with self.subTest(forged=type(forged).__name__):
                with self.assertRaises(ValueError) as caught:
                    SIG.commit_signal(None, context=context, decision=forged, row={})
                self.assertIn("SignalDecision", str(caught.exception))

    def test_AIG06_ai_layer_owns_no_ledger_write_path(self):
        """AIG-06：契约层不持有任何写入 / 提交入口。"""
        called = _called_names(_tree(CONTRACT_MODULE))
        for forbidden in (
            "commit_signal", "commit", "execute", "executemany", "executescript",
            "connect", "begin", "rollback", "write", "insert", "update", "delete",
        ):
            with self.subTest(call=forbidden):
                self.assertNotIn(
                    forbidden, called,
                    f"{CONTRACT_MODULE} 出现了 {forbidden}() —— 研究层不得持有写路径",
                )


class GuardIsNotVacuouslyPassing(unittest.TestCase):
    """护栏必须真的能失败；否则它只是装饰。"""

    def test_import_scanner_fires_on_a_reverse_import(self):
        self.assertIn("ai_research_contract", _imported_roots(ast.parse("import ai_research_contract\n")))

    def test_import_scanner_reads_from_imports_too(self):
        tree = ast.parse("from ai_research_contract import ResearchHypothesis\n")
        self.assertIn("ai_research_contract", _imported_roots(tree))

    def test_call_scanner_fires_on_a_write_call(self):
        tree = ast.parse("def f(conn):\n    conn.execute('INSERT INTO paper_signals')\n")
        self.assertIn("execute", _called_names(tree))

    def test_sql_scanner_fires_on_an_insert_statement(self):
        tree = ast.parse("SQL = 'INSERT INTO paper_signals(a) VALUES(?)'\n")
        self.assertTrue(
            any("INSERT INTO" in text.upper() for text in _code_string_constants(tree)),
        )

    def test_sql_scanner_ignores_docstrings(self):
        """文档里解释"禁止 INSERT INTO"不该让守卫变红。"""
        tree = ast.parse('"""Never INSERT INTO paper_signals here."""\nX = 1\n')
        self.assertEqual([], _code_string_constants(tree))


if __name__ == "__main__":
    unittest.main()
