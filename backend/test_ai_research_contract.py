# -*- coding: utf-8 -*-
"""R27-A —— AI Information & Research Contract 的语义与依赖方向回归。

本文件存在的全部理由，是这个不变量：

    **AI 只能读取可信事实并产出 research 结论；它永远不是 Market Data /
    Signal / Execution / Risk / Promotion 的 authority。**

因此分三组：

    AI-*   contract：事实观测、证据引用、假设强度、PIT、fail-closed 消费
    AIG-*  architecture guard：依赖方向（AI → R24 contract）、反向 import、
           时钟/IO/DB 依赖、以及"AI 产物无法成为账本行"
    非空性  护栏必须真的能失败，否则它只是装饰

时间一律**显式**传入（固定的 as_of 字符串），绝不 ``time.sleep()``、绝不读墙上
时钟 —— 本轮的 PIT 语义恰恰是最容易假绿的地方。

刻意**不**起数据库：AI-07 要证明的是"writer 在触碰任何连接之前就拒绝一个非
裁决的 decision"，而 ``commit_signal`` 的校验顺序正好让这一点可以用 ``conn=None``
验证。不需要 sqlite，测试因此是秒级的。
"""
from __future__ import annotations

import ast
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

#: 只有这一个项目模块允许被 AI 契约 import：R24 的纯行情契约（**读**事实）。
ALLOWED_PROJECT_IMPORTS = {"market_data_contract"}
ALLOWED_STDLIB_IMPORTS = {"__future__", "dataclasses", "types", "typing"}

#: 现有 authority owner。它们**不得**反向 import AI 研究层 —— AI 是消费者。
#: 按 authority 领域分组，新增 authority 时同步登记（R26 的执行权威已在此）。
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

#: 允许 import AI 研究层的模块。**现在为空**：R27-A 只建立契约，没有任何
#: 生产消费者。将来接入时必须显式加到这份清单里，使"谁依赖了 AI"是一次
#: 有意识的决定，而不是一次静默扩散。
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
    """所有 import 的完整名字（用于判断是否 import 了某个具体模块）。"""
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


def _verified_ref(source_type: str = ARC.EVIDENCE_SOURCE_MARKET_DATA, *, as_of: str = DAY):
    """一条**通过核验**的证据引用（R24 双源）。"""
    return ARC.ResearchEvidenceRef(
        source_type=source_type,
        source_id=f"{source_type}-fact-1",
        as_of=as_of,
        verification=MDC.VERIFICATION_VERIFIED,
        verification_method=MDC.VERIFICATION_METHOD_CROSS_SOURCE,
    )


def _single_source_ref(*, as_of: str = DAY):
    """一条**只有单源**的证据引用 —— 可用，但不得自称已核验。"""
    return ARC.ResearchEvidenceRef(
        source_type=ARC.EVIDENCE_SOURCE_MARKET_DATA,
        source_id="market_data-fact-single",
        as_of=as_of,
        verification=MDC.VERIFICATION_SINGLE_SOURCE,
        verification_method=MDC.VERIFICATION_METHOD_CROSS_SOURCE,
    )


def _disagreement_ref(*, as_of: str = DAY):
    """一条**被多源否证**的证据引用。"""
    return ARC.ResearchEvidenceRef(
        source_type=ARC.EVIDENCE_SOURCE_MARKET_DATA,
        source_id="market_data-fact-contested",
        as_of=as_of,
        verification=MDC.VERIFICATION_DISAGREEMENT,
        verification_method=MDC.VERIFICATION_METHOD_CROSS_SOURCE,
    )


def _hypothesis(*, as_of: str = DAY, refs=(), confidence: float = 0.5):
    return ARC.ResearchHypothesis(
        hypothesis_id="H-1", as_of=as_of, subject="600000",
        thesis="momentum persists into the next session",
        evidence_refs=tuple(refs), confidence=confidence,
    )


# ---------------------------------------------------------------------------
# AI-01 ~ AI-05 —— contract 语义
# ---------------------------------------------------------------------------


class AiResearchContractTests(unittest.TestCase):
    """契约层的业务语义：事实 → 证据 → 假设，以及 PIT 与 fail-closed。"""

    def test_AI01_verified_market_evidence_yields_a_verified_information_event(self):
        """AI-01：已核验的行情事实 → 合法的 InformationEvent，且状态被逐字保留。"""
        ref = _verified_ref()
        event = ARC.InformationEvent(
            as_of=DAY, source="market_data_service.read_snapshot", evidence_ref=ref,
            payload={"price": 10.5},
        )
        self.assertEqual(ARC.EVENT_MARKET_OBSERVED, event.kind)
        self.assertEqual(MDC.VERIFICATION_VERIFIED, event.verification)
        self.assertEqual(MDC.VERIFICATION_METHOD_CROSS_SOURCE, event.verification_method)
        self.assertEqual("market_data-fact-1", event.evidence_id)
        self.assertTrue(ref.cross_source_verified)

        # kind 与 source_type 一一对应，且是派生只读的 —— 错标无法表达。
        self.assertEqual(
            _KIND_BY_SOURCE[ref.source_type], event.kind,
            "InformationEvent.kind 必须由 source_type 派生",
        )
        for source_type, expected_kind in _KIND_BY_SOURCE.items():
            with self.subTest(source_type=source_type):
                self.assertEqual(
                    expected_kind,
                    ARC.InformationEvent(
                        as_of=DAY, source="s", evidence_ref=_verified_ref(source_type),
                    ).kind,
                )

        # 投影只 render，不重算：verification 与 method 同时下发。
        projected = event.projection()
        self.assertEqual(MDC.VERIFICATION_VERIFIED, projected["verification"])
        self.assertEqual(MDC.VERIFICATION_METHOD_CROSS_SOURCE,
                         projected["verification_method"])

    def test_AI02_stale_or_unverified_evidence_keeps_its_original_state(self):
        """AI-02：未核验的事实**保持原状**，绝不被升级成 verified。

        这是 §五 的核心：把 ``single_source`` / ``not_attempted`` 当成
        ``verified`` 会让 AI 侧凭空提高事实可信度，而事实的核验程度只有一个
        owner（R24）。
        """
        single = _single_source_ref()
        self.assertEqual(MDC.VERIFICATION_SINGLE_SOURCE, single.verification)
        self.assertNotEqual(MDC.VERIFICATION_VERIFIED, single.verification)
        self.assertFalse(
            single.cross_source_verified,
            "单源证据不得自称通过了逐票双源交叉核验",
        )
        self.assertEqual(ARC.STANDING_DEGRADED, single.standing)

        # 未核验事件：verification 从证据继承，不被事件层改写。
        event = ARC.InformationEvent(as_of=DAY, source="s", evidence_ref=single)
        self.assertEqual(MDC.VERIFICATION_SINGLE_SOURCE, event.verification)

        # 单源证据不足以支撑一个假设 —— 是"证据不足"，不是"支持"。
        self.assertEqual(
            ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, _hypothesis(refs=(single,)).status,
        )

        # 从未核验（not_attempted）同样是 degraded，绝不因为"没人反对"就通过。
        untouched = ARC.ResearchEvidenceRef(
            source_type=ARC.EVIDENCE_SOURCE_NEWS, source_id="news-1", as_of=DAY,
        )
        self.assertEqual(MDC.VERIFICATION_NOT_ATTEMPTED, untouched.verification)
        self.assertEqual(ARC.STANDING_DEGRADED, untouched.standing)
        hypothesis = _hypothesis(refs=(untouched,))
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
        self.assertEqual(ARC.RESEARCH_REASON_EVIDENCE_NOT_VERIFIED, hypothesis.reason)

        # 非法核验组合（verified 配 none）在**构造期**就被拒绝，而不是被降级。
        with self.assertRaises(ValueError) as caught:
            ARC.ResearchEvidenceRef(
                source_type=ARC.EVIDENCE_SOURCE_MARKET_DATA, source_id="x", as_of=DAY,
                verification=MDC.VERIFICATION_VERIFIED,
                verification_method=MDC.VERIFICATION_METHOD_NONE,
            )
        self.assertIn("illegal verification pair", str(caught.exception))

    def test_AI03_hypothesis_referencing_future_evidence_is_rejected(self):
        """AI-03：引用未来证据的假设在**构造期**被拒绝，而不是静默过滤。"""
        future = _verified_ref(as_of=NEXT_DAY)
        with self.assertRaises(ValueError) as caught:
            _hypothesis(as_of=DAY, refs=(future,))
        message = str(caught.exception)
        self.assertIn("future evidence", message)
        self.assertIn(NEXT_DAY, message, "错误信息必须点名那条未来事实")

        # 同一天的证据是合法的（as_of <= hypothesis.as_of）。
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, _hypothesis(as_of=DAY, refs=(_verified_ref(),)).status)

        # 历史假设引用"今天"的事实同样被拒 —— 这正是 PIT 的 look-ahead 漏洞。
        stale_hypothesis_as_of = PREV_DAY
        with self.assertRaises(ValueError):
            _hypothesis(as_of=stale_hypothesis_as_of, refs=(_verified_ref(as_of=DAY),))

    def test_AI04_hypothesis_without_evidence_is_insufficient_not_approved(self):
        """AI-04：没有证据的假设是 ``insufficient_evidence``，绝不默认批准。"""
        empty = _hypothesis(refs=())
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, empty.status)
        self.assertEqual(ARC.RESEARCH_REASON_NO_EVIDENCE, empty.reason)
        self.assertFalse(empty.is_supported)

        # 高 confidence 不能替代证据 —— 自信不是事实。
        confident = _hypothesis(refs=(), confidence=1.0)
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, confident.status)

        # 消费端 fail closed：拿不到结论就拿不到默认值。
        with self.assertRaises(ARC.UnsupportedResearch):
            empty.require_supported()

        # 被否证的证据让假设变成 ``unsupported``（证据反对），与"证据不足"可区分。
        contradicted = _hypothesis(refs=(_disagreement_ref(),))
        self.assertEqual(ARC.HYPOTHESIS_UNSUPPORTED, contradicted.status)
        self.assertEqual(ARC.RESEARCH_REASON_EVIDENCE_CONTRADICTED, contradicted.reason)

        # 支持证据与反对证据并存 → 反对优先（保守），绝不"票数过半"。
        mixed = _hypothesis(refs=(_verified_ref(), _disagreement_ref()))
        self.assertEqual(ARC.HYPOTHESIS_UNSUPPORTED, mixed.status)

        # 核验源不可用与"两个源互相否证"是两个结论，原因必须不同。
        unavailable = ARC.ResearchEvidenceRef(
            source_type=ARC.EVIDENCE_SOURCE_EXECUTION, source_id="order-1", as_of=DAY,
            verification=MDC.VERIFICATION_UNAVAILABLE,
            verification_method=MDC.VERIFICATION_METHOD_CROSS_SOURCE,
        )
        unavailable_hypothesis = _hypothesis(refs=(unavailable,))
        self.assertEqual(ARC.HYPOTHESIS_UNSUPPORTED, unavailable_hypothesis.status)
        self.assertEqual(
            ARC.RESEARCH_REASON_EVIDENCE_UNAVAILABLE, unavailable_hypothesis.reason,
        )
        self.assertNotEqual(contradicted.reason, unavailable_hypothesis.reason)

    def test_AI05_historical_hypothesis_cannot_read_current_state(self):
        """AI-05：历史假设不读取 current state —— as-of 必须显式且证明得出来。"""
        # 业务日无法证明 → 构造期拒绝（绝不回落到 today()）。
        for bad in (None, "", "not-a-day", "2026-13-45"):
            with self.subTest(as_of=bad):
                with self.assertRaises(ValueError):
                    _hypothesis(as_of=bad)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    ARC.ResearchEvidenceRef(
                        source_type=ARC.EVIDENCE_SOURCE_MARKET_DATA,
                        source_id="x", as_of=bad,
                    )

        # 契约本身没有任何读时钟的入口 —— 这是"无法回填 current"的结构性保证。
        tree = _tree(CONTRACT_MODULE)
        called = _called_names(tree)
        for forbidden in FORBIDDEN_CLOCK_CALLS:
            with self.subTest(call=forbidden):
                self.assertNotIn(
                    forbidden, called,
                    f"{CONTRACT_MODULE} 调用了 {forbidden} —— "
                    "研究契约不得读时钟/随机/环境，否则历史研究会被 current 回填",
                )
        self.assertNotIn("time", _imported_roots(tree))
        self.assertNotIn("datetime", _imported_roots(tree))

        # 整份证据集合都必须是"过去或当天"，没有例外。
        historical = _hypothesis(
            as_of=DAY, refs=(_verified_ref(as_of=PREV_DAY), _verified_ref(as_of=DAY)),
        )
        self.assertEqual((), historical.look_ahead_refs)
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, historical.status)

    def test_AI06_research_vocabulary_never_overlaps_the_signal_lifecycle(self):
        """AI-06：研究词汇与 signal 生命周期**不相交** —— 结论无法直接落成 signal。"""
        signal_lifecycle = {"pending", "approved", "blocked", "waitlist", "recovery"}
        overlap = set(ARC.HYPOTHESIS_STATUSES) & signal_lifecycle
        self.assertEqual(
            set(), overlap,
            f"研究状态与 paper_signals 生命周期重叠：{sorted(overlap)} —— "
            "重叠会让 supported 被当作 approved 使用",
        )
        # decision outcome 词汇同样不得复用。
        self.assertEqual(set(), set(ARC.HYPOTHESIS_STATUSES) & {"approved", "blocked"})

    def test_AI07_hypothesis_projection_declares_itself_non_authoritative(self):
        """AI-07：假设投影必须自带"这只是研究"的结论，供下游离线判断。"""
        hypothesis = _hypothesis(refs=(_verified_ref(),), confidence=0.62)
        projected = hypothesis.projection()

        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, projected["status"])
        self.assertIsNone(projected["reason"])
        self.assertEqual("research", projected["authority"])
        self.assertIs(False, projected["is_authoritative"])
        self.assertIs(False, hypothesis.is_authoritative)
        self.assertEqual(1, projected["evidence_count"])
        self.assertEqual(DAY, projected["evidence"][0]["as_of"])
        self.assertEqual(MDC.VERIFICATION_VERIFIED, projected["evidence"][0]["verification"])

        # 证据不足时，投影如实说出原因，而不是给一个空 status。
        weak = _hypothesis(refs=()).projection()
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, weak["status"])
        self.assertEqual(ARC.RESEARCH_REASON_NO_EVIDENCE, weak["reason"])

    def test_AI08_evidence_refs_are_frozen_deduped_and_ai_text_is_not_a_source(self):
        """AI-08：引用是冻结的、去重的；AI 自产文本**无法**表达成事实来源。"""
        # 闭集：没有任何 AI 自产类别。
        for forbidden in ("llm_output", "ai_narrative", "hypothesis", "ai_research", ""):
            with self.subTest(source_type=forbidden):
                with self.assertRaises(ValueError):
                    ARC.ResearchEvidenceRef(
                        source_type=forbidden, source_id="x", as_of=DAY,
                    )

        # 重复引用同一条事实不是两份独立证据。
        duplicated = _hypothesis(refs=(_verified_ref(), _verified_ref()))
        self.assertEqual(1, len(duplicated.evidence_refs))
        self.assertEqual(1, duplicated.projection()["evidence_count"])

        # payload / detail 冻结：不可原位改写。
        ref = _verified_ref()
        with self.assertRaises(TypeError):
            ref.detail["mutated"] = 1  # type: ignore[index]
        event = ARC.InformationEvent(
            as_of=DAY, source="s", evidence_ref=ref, payload={"a": 1},
        )
        with self.assertRaises(TypeError):
            event.payload["mutated"] = 1  # type: ignore[index]

        # 事实必须来自 owner 的投影，且 as-of 不可证明时拒绝构造。
        reading = MDC.MarketDataReading(
            availability=MDC.AVAILABILITY_AVAILABLE, freshness=MDC.FRESHNESS_FRESH,
            status=MDC.STATUS_FRESH, policy_name="live_market",
            snapshot=MDC.MarketDataSnapshot(
                kind="symbol_quote", as_of=DAY, observed_at=f"{DAY}T10:30:00+08:00",
                verification=MDC.VERIFICATION_VERIFIED,
                verification_method=MDC.VERIFICATION_METHOD_CROSS_SOURCE,
            ),
        )
        mapped = ARC.evidence_ref_from_reading(
            ARC.EVIDENCE_SOURCE_MARKET_DATA, "LIVE_MARKET_POLICY", reading,
        )
        self.assertEqual(DAY, mapped.as_of)
        self.assertEqual(MDC.VERIFICATION_VERIFIED, mapped.verification)
        self.assertEqual(MDC.VERIFICATION_METHOD_CROSS_SOURCE, mapped.verification_method)

        # 不可用的 reading 没有可证明的 as-of → 拒绝，而不是造一条"空事实"。
        unavailable = MDC.unavailable_reading(MDC.LIVE_MARKET_POLICY)
        with self.assertRaises(ValueError):
            ARC.evidence_ref_from_reading(
                ARC.EVIDENCE_SOURCE_MARKET_DATA, "LIVE_MARKET_POLICY", unavailable,
            )


# ---------------------------------------------------------------------------
# AIG-* —— architecture guard：依赖方向与"AI 不是 authority"
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
        """AIG-02：现有 authority **不得** import AI 研究层（依赖方向单向）。"""
        offenders = []
        for name in AUTHORITY_MODULES:
            for imported in _imported_names(_tree(name)):
                if imported.split(".")[0] == "ai_research_contract":
                    offenders.append(f"{name}: import {imported}")
        self.assertEqual(
            [], offenders,
            "authority 反向 import 了 AI 研究层 —— AI 必须是纯消费者，"
            "不得成为行情/signal/执行/风控/晋升的依据",
        )

    def test_AIG03_no_production_module_consumes_the_ai_layer_without_registration(self):
        """AIG-03：生产链路依赖 AI 层必须**显式登记**（当前为 0）。"""
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
            "有生产模块在未登记的情况下 import 了 AI 研究层。"
            "接入 AI 研究结论必须先把该模块加入 ALLOWED_AI_CONSUMERS，"
            "使'谁依赖了 AI'成为一次有意识的决定，而不是静默扩散",
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

        # 也不得出现任何 SQL 文本（它没有账本，也不该有）。
        for text in _code_string_constants(_tree(CONTRACT_MODULE)):
            upper = text.upper()
            for keyword in ("INSERT INTO", "UPDATE ", "DELETE FROM", "SELECT "):
                with self.subTest(keyword=keyword, text=text[:40]):
                    self.assertNotIn(
                        keyword, upper,
                        f"{CONTRACT_MODULE} 出现了 SQL 文本 —— 本轮不建立持久化",
                    )

    def test_AIG05_ai_output_cannot_be_committed_as_a_signal(self):
        """AIG-05：AI 产物**无法**被当作裁决提交成正式 signal。

        行为证明，而不是约定：``commit_signal`` 在触碰连接之前就要求一个真正的
        :class:`signal_service.SignalDecision`，因此把一份研究假设当 decision 传进去
        会在**任何落库发生前**失败（``conn=None`` 足以验证这一点 —— 校验顺序
        决定了它先于 ``conn.execute``）。
        """
        hypothesis = _hypothesis(refs=(_verified_ref(),))
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
        tree = ast.parse("import ai_research_contract\n")
        self.assertIn("ai_research_contract", _imported_roots(tree))

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


#: ``kind`` ↔ ``source_type`` 的期望映射，与被测模块的闭集对齐。
_KIND_BY_SOURCE = {
    ARC.EVIDENCE_SOURCE_MARKET_DATA: ARC.EVENT_MARKET_OBSERVED,
    ARC.EVIDENCE_SOURCE_SIGNAL: ARC.EVENT_SIGNAL_OBSERVED,
    ARC.EVIDENCE_SOURCE_EXECUTION: ARC.EVENT_EXECUTION_OBSERVED,
    ARC.EVIDENCE_SOURCE_STRATEGY_RESEARCH: ARC.EVENT_STRATEGY_RESEARCH_OBSERVED,
    ARC.EVIDENCE_SOURCE_PORTFOLIO_RESEARCH: ARC.EVENT_PORTFOLIO_RESEARCH_OBSERVED,
    ARC.EVIDENCE_SOURCE_NEWS: ARC.EVENT_NEWS_OBSERVED,
}


if __name__ == "__main__":
    unittest.main()
