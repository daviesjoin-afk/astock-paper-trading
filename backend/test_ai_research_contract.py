# -*- coding: utf-8 -*-
"""R27-A —— AI Information & Research Contract 的语义与依赖方向回归。

存在理由是这个不变量：

    **AI 只能读取可信事实并产出 research 结论；它永远不是 Market Data /
    Signal / Execution / Risk / Promotion 的 authority。**

分四组：

    AI-*         contract 语义：两个维度分离、冲突、深冻结、PIT
    AI-18*       JSON-like payload 边界：bytes / 非 str key 在**契约期**就 fail closed
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
from collections.abc import Mapping

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

#: 默认 reading 的 owner 口径 —— evidence identity 由它加 kind / subject / 观测时点
#: **派生**，调用方不提供 source_id（见 AI-TYPED-02）。
DEFAULT_POLICY = "live_market"
DEFAULT_CODE = "600000"
SNAPSHOT_KIND = "symbol_quote"

#: R24 既有的逐票核验术语（``quote_validation``）—— 唯一的映射入口在 R24 那侧。
VALIDATION_CROSS_SOURCE = "cross_source_checked"
VALIDATION_SINGLE_SOURCE = "range_timestamp_checked"
VALIDATION_DISAGREEMENT = "cross_source_failed"
VALIDATION_UNAVAILABLE = "cross_source_unavailable"


def _derived_identity(*, policy: str = DEFAULT_POLICY, code: str = DEFAULT_CODE,
                      as_of: str = DAY, observed_at: str | None = None) -> str:
    """reading 派生出的 evidence identity：``policy|kind|subject@观测时点``。

    单票事实的 subject 就是 ``code`` —— 这正是修复 identity 碰撞的那一维：
    同一时刻的两只不同股票必须得到不同 identity。
    """
    stamp = observed_at or f"{as_of}T10:30:00+08:00"
    return f"{policy}|{SNAPSHOT_KIND}|{code}@{stamp}"

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

#: 允许 import AI 研究层的生产模块。**必须显式登记** —— "谁依赖了 AI"是一次有意识
#: 的决定，而不是静默扩散。
#:
#: R27-A 时为空（只有契约，没有消费者）。R27-B1 新增**第一个**消费者：
#: ``ai_research_provider`` —— 把 typed ``InformationEvent`` 投给 LLM，再把输出映射回
#: ``ResearchHypothesis``。R27-B2A 新增**第二个**：``ai_research_repository`` ——
#: typed research 的 append-only 持久化 owner。
#:
#: R27-B2B 新增**第三个**：``deepseek_advisor`` —— 它原来就是 ``data_quality`` 这个
#: purpose 的 writer，本轮把那条 runtime 迁到 typed 路径，因此它必须自己签发 typed
#: ``InformationEvent``（从 R24 reading 派生）。这正是"迁移"与"静默扩散"的区别：
#: 它从 legacy 消费者变成了 typed 消费者，并且必须在这里留下一条可审计的记录。
#:
#: 四个都**不是** authority：provider 是 typed research producer，repository 是 typed
#: research persistence consumer，``deepseek_advisor`` 是 runtime caller。authority 仍然
#: 不得反向 import 其中任何一个。
#:
#: 注意 ``ai_research_service`` **不**在这个集合里：orchestration boundary 只依赖
#: provider 与 repository，刻意不 import 契约 —— 多一个消费者就多一份"两套规则必然
#: 漂移"的风险。
ALLOWED_AI_CONSUMERS: set[str] = {
    "ai_research_provider.py",
    "ai_research_repository.py",
    "deepseek_advisor.py",
}

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


def _function_source(source: str, name: str) -> str:
    """某个顶层函数（含嵌套定义）的源码文本 —— 用于"这段逻辑里没有 X"的静态断言。

    按 AST 定位而不是子串搜索：docstring 里解释"不要比较 market 常量"不该让断言变红。
    """
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} 不是 {CONTRACT_MODULE} 的顶层函数")


def _function_symbols(source: str, name: str) -> set[str]:
    """顶层函数体里出现的**被引用符号名**（``X.Y`` 的 ``Y``、裸名 ``Z``）。

    刻意按 AST 取符号而不是搜文本：注释与 docstring 里解释"不得比较
    ``MDC.VERIFICATION_*``"是**正确**的文档行为，不该让"这段逻辑没有依赖 market
    词表"的断言变红。
    """
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != name:
            continue
        found: set[str] = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute):
                found.add(inner.attr)
            elif isinstance(inner, ast.Name):
                found.add(inner.id)
        return found
    raise AssertionError(f"{name} 不是 {CONTRACT_MODULE} 的顶层函数")


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


def _quote(code: str = DEFAULT_CODE, price: float = 10.5,
           validation: str = VALIDATION_CROSS_SOURCE, *, observed_at: str | None = None):
    """一条已预取的逐票报价 envelope（R24 ``symbol_quote_snapshot`` 的输入形态）。

    ``validation`` 用的是**既有业务术语**（``quote_validation``），由 R24 的唯一映射
    ``verification_from_cross_status`` 翻成契约维度 —— 测试不发明第二套核验词汇。
    """
    return {
        "code": code, "price": price,
        "quote_at": observed_at or f"{DAY}T10:30:00+08:00",
        "quote_source": "eastmoney", "quote_validation": validation,
    }


def _reading(
    *, as_of: str = DAY, policy: str = DEFAULT_POLICY,
    validation: str = VALIDATION_CROSS_SOURCE, code: str = DEFAULT_CODE,
    observed_at: str | None = None, price: float = 10.5, now: str | None = None,
):
    """一个 R24 typed projection（``MarketDataReading``），走真实的 owner 路径。

    ``symbol_quote_snapshot`` + ``classify`` 都是 R24 的公开入口，因此这里产出的
    reading 与生产路径同形（含 ``kind`` / ``rows`` / ``policy``）。

    刻意仍由测试手工调用：这正是本契约**无法**排除的两步伪造路径
    （``MarketDataReading`` / ``MarketDataSnapshot`` 都是公开 dataclass）。测试里保留
    它，是为了让这条限制可见 —— 见 AI-TYPED-06。
    """
    stamp = observed_at or f"{as_of}T10:30:00+08:00"
    snapshot = MDC.symbol_quote_snapshot(
        _quote(code, price, validation, observed_at=stamp), asof_day=as_of,
    )
    return MDC.classify(
        snapshot, MDC.policy_named(policy), now=now or stamp,
        asof_day=as_of if policy == DEFAULT_POLICY else None,
    )


def _ref(*, as_of: str = DAY, code: str = DEFAULT_CODE,
         validation: str = VALIDATION_CROSS_SOURCE, observed_at: str | None = None,
         price: float = 10.5):
    """一条由唯一 factory 签发的证据引用（identity 由 reading 派生）。"""
    return ARC.evidence_ref_from_market_reading(
        _reading(as_of=as_of, code=code, validation=validation,
                 observed_at=observed_at, price=price),
    )


def _evidence(ref, relation=ARC.RELATION_SUPPORTS):
    return ARC.HypothesisEvidence(ref=ref, relation=relation)


def _hypothesis(*, as_of=DAY, evidence=(), confidence=0.5):
    return ARC.ResearchHypothesis(
        hypothesis_id="H-1", as_of=as_of, subject="600000",
        thesis="momentum persists into the next session",
        evidence=tuple(evidence), confidence=confidence,
    )


#: **仅测试使用**的 owner-native 核验状态词。刻意不等于
#: ``market_data_contract.VERIFICATION_VERIFIED`` —— 用它才能证明 research 的
#: "是否通过核验"判据真的与 market 字符串解耦，而不是碰巧两边都叫 ``verified``。
OWNER_NATIVE_STATUS = "owner_verified"


def _owner_native_ref(
    *, source_type=ARC.EVIDENCE_SOURCE_EXECUTION, source_id="fill:abc",
    as_of=DAY, outcome=ARC.OWNER_OUTCOME_VERIFIED, status=OWNER_NATIVE_STATUS,
    attributes=None, fingerprint="owner-native-fp",
):
    """一个**非 market** 的 typed ref，用契约私有签发口构造（仅测试）。

    刻意**不**新增 production public factory —— B2C-2 的 factory registry 仍然只有
    ``market_data``，execution adapter 属于 B2C-3。这里只是把"契约核心能否携带
    owner-native 核验"变成一条可执行的断言。
    """
    return ARC._issue_evidence_ref(
        source_type=source_type,
        source_id=source_id,
        as_of=as_of,
        owner_verification=ARC.OwnerVerification(
            outcome=outcome, status=status,
            attributes=(
                {"verification_scope": "execution_order_fill_evidence"}
                if attributes is None else attributes
            ),
        ),
        detail={"content_fingerprint": fingerprint},
    )


def _market_ref_with_method(*, method, code=DEFAULT_CODE,
                            observed_at=None, price=10.5):
    """一条 method 明确指定的 market ref（identity / 内容指纹与 method 无关）。

    刻意手工构造 snapshot 再让 R24 自己 ``classify``：``symbol_quote_snapshot``
    只会产出 ``cross_source``，而 ``coverage_integrity`` 是 R24 的另一个合法
    method（它**不是**逐票双源）。
    """
    stamp = observed_at or f"{DAY}T10:30:00+08:00"
    snapshot = MDC.MarketDataSnapshot(
        kind=SNAPSHOT_KIND,
        rows=({"code": code, "price": price, "quote_at": stamp},),
        as_of=DAY, observed_at=stamp, source="eastmoney", complete=True,
        expected_rows=1,
        verification=MDC.VERIFICATION_VERIFIED, verification_method=method,
    )
    return ARC.evidence_ref_from_market_reading(
        MDC.classify(snapshot, MDC.policy_named(DEFAULT_POLICY), now=stamp, asof_day=DAY),
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
        single = _ref(validation=VALIDATION_SINGLE_SOURCE)
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
        # 这条判据现在住在 **market owner 的 factory** 里（B2C-2 起它不再被命名为
        # "owner verification" —— 它只认识 R24 的词表），research core 看不到它。
        with self.assertRaises(ValueError) as caught:
            ARC._market_owner_verification(
                {"verification": MDC.VERIFICATION_VERIFIED,
                 "verification_method": MDC.VERIFICATION_METHOD_NONE},
                None,
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
            _evidence(_ref(code="000001", observed_at=f"{DAY}T15:00:00+08:00"),
                      ARC.RELATION_CONTRADICTS),
        ))
        self.assertEqual(ARC.HYPOTHESIS_UNSUPPORTED, mixed.status)

    def test_AI12_unverified_fact_with_supports_is_insufficient(self):
        """AI-12：未核验事实 + supports → insufficient_evidence（不是 supported）。"""
        hypothesis = _hypothesis(
            evidence=(_evidence(_ref(validation=VALIDATION_SINGLE_SOURCE), ), ),
        )
        self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
        self.assertEqual(ARC.RESEARCH_REASON_EVIDENCE_NOT_VERIFIED, hypothesis.reason)

    def test_AI13_unavailable_source_does_not_contradict_the_thesis(self):
        """AI-13：source unavailable + supports → insufficient_evidence，**不是** unsupported。

        provider disagreement / unavailable 只说明"这条 evidence 本身不能成为可靠依据"，
        不说明"这个 thesis 被事实反驳"。两者必须完全独立。
        """
        for validation in (VALIDATION_UNAVAILABLE, VALIDATION_DISAGREEMENT):
            with self.subTest(validation=validation):
                ref = _ref(validation=validation)
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
        disagreement = _ref(validation=VALIDATION_DISAGREEMENT)
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
            _reading(validation=VALIDATION_SINGLE_SOURCE),
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

        # 缺 kind 的快照无法区分 symbol_quote 与横截面 → identity 会碰撞，拒绝。
        bare = MDC.MarketDataReading(
            availability=MDC.AVAILABILITY_AVAILABLE, freshness=MDC.FRESHNESS_FRESH,
            status=MDC.STATUS_FRESH, policy_name=DEFAULT_POLICY,
            snapshot=MDC.MarketDataSnapshot(
                kind="", as_of=DAY, observed_at=f"{DAY}T10:00:00+08:00",
                verification=MDC.VERIFICATION_VERIFIED,
                verification_method=MDC.VERIFICATION_METHOD_CROSS_SOURCE,
            ),
        )
        with self.assertRaises(ValueError) as caught:
            ARC.evidence_ref_from_market_reading(bare)
        self.assertIn("kind", str(caught.exception))

        # 缺 observed_at / as_of 的快照无法证明业务日 → 拒绝。
        undated = MDC.MarketDataReading(
            availability=MDC.AVAILABILITY_AVAILABLE, freshness=MDC.FRESHNESS_UNKNOWN,
            status=MDC.STATUS_STALE, policy_name=DEFAULT_POLICY,
            snapshot=MDC.MarketDataSnapshot(
                kind=SNAPSHOT_KIND, observed_at=None, as_of=None,
                verification=MDC.VERIFICATION_VERIFIED,
                verification_method=MDC.VERIFICATION_METHOD_CROSS_SOURCE,
            ),
        )
        with self.assertRaises(ValueError) as caught:
            ARC.evidence_ref_from_market_reading(undated)
        self.assertIn("observed_at", str(caught.exception))

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
        # 完整的两步伪造：手工拼一个声称 verified 的 R24 类型对象。
        forged_snapshot = MDC.MarketDataSnapshot(
            kind=SNAPSHOT_KIND, rows=({"code": "999999", "price": 1.0,
                                       "quote_at": f"{DAY}T10:30:00+08:00"},),
            as_of=DAY, observed_at=f"{DAY}T10:30:00+08:00", source="handmade",
            complete=True, expected_rows=1,
            verification=MDC.VERIFICATION_VERIFIED,
            verification_method=MDC.VERIFICATION_METHOD_CROSS_SOURCE,
        )
        forged = ARC.evidence_ref_from_market_reading(
            MDC.MarketDataReading(
                availability=MDC.AVAILABILITY_AVAILABLE, freshness=MDC.FRESHNESS_FRESH,
                status=MDC.STATUS_FRESH, policy_name=DEFAULT_POLICY,
                snapshot=forged_snapshot,
            ),
        )
        # 本层**照原样复制**了这个自述的核验结论 —— 它无从证明 reading 的来源。
        self.assertEqual(MDC.VERIFICATION_VERIFIED, forged.verification)
        self.assertEqual(MDC.VERIFICATION_METHOD_CROSS_SOURCE, forged.verification_method)
        self.assertEqual(ARC.EVIDENCE_SOURCE_MARKET_DATA, forged.source_type)
        self.assertTrue(forged.cross_source_verified)

        # 但伪造者仍然**无法**选择 identity：它只能是被派生出来的那个值。
        self.assertEqual(
            _derived_identity(code="999999"), forged.source_id,
            "伪造者不能凭自己的意愿命名 identity",
        )

    def test_AI_TYPED_07_distinct_symbols_never_share_an_identity(self):
        """AI-TYPED-07：不同股票、同 policy / 同时点，必须有**不同** identity。

        这是 identity 碰撞的永久回归。早期 identity 只有 ``policy @ observed_at``，
        于是同一时刻的两只不同股票得到完全相同的 identity；由于 ``fact_state`` 当时也
        不含内容指纹，它们连冲突状态都相同 —— 会被**静默去重成一条事实**，既不报错也
        不报 conflict。这是本 contract 自己的 correctness 缺陷。
        """
        first = _ref(code="600000")
        second = _ref(code="000001")

        self.assertNotEqual(first.identity(), second.identity())
        self.assertNotEqual(first.fact_state(), second.fact_state())

        # 两条独立事实必须都保留，而不是被折叠成 1 条。
        hypothesis = _hypothesis(
            evidence=(_evidence(first), _evidence(second)),
        )
        self.assertEqual(2, len(hypothesis.evidence))
        self.assertEqual(2, hypothesis.projection()["evidence_count"])

        # identity 必须能读出 subject，否则"不同股票"这件事不可审计。
        self.assertIn("600000", first.source_id)
        self.assertIn("000001", second.source_id)
        self.assertIn(SNAPSHOT_KIND, first.source_id)

    def test_AI_TYPED_08_same_identity_with_changed_content_is_a_conflict(self):
        """AI-TYPED-08：同 identity 但事实内容变了 → EvidenceConflict，不静默去重。

        payload 完全排除在冲突判断之外，会让"报价被悄悄改写"看起来像"同一条事实"。
        同时确认：freshness 差异**不是**内容变化，不该误报 conflict。
        """
        original = _ref(code=DEFAULT_CODE, price=10.50)
        changed = _ref(code=DEFAULT_CODE, price=11.90)
        self.assertEqual(original.identity(), changed.identity())
        self.assertNotEqual(original.fact_state(), changed.fact_state())

        for label, order in (("A,B", (original, changed)), ("B,A", (changed, original))):
            with self.subTest(order=label):
                with self.assertRaises(ARC.EvidenceConflict):
                    _hypothesis(evidence=tuple(_evidence(ref) for ref in order))

        # 内容完全一致 → 安全去重。
        identical = _hypothesis(
            evidence=(_evidence(original), _evidence(_ref(code=DEFAULT_CODE, price=10.50))),
        )
        self.assertEqual(1, len(identical.evidence))

        # 同一份快照在不同 now 下 fresh vs stale 是**时效**，不是事实变化。
        fresh = _reading(now=f"{DAY}T10:30:00+08:00")
        stale = _reading(now=f"{DAY}T23:59:00+08:00")
        fresh_ref = ARC.evidence_ref_from_market_reading(fresh)
        stale_ref = ARC.evidence_ref_from_market_reading(stale)
        self.assertEqual(fresh_ref.identity(), stale_ref.identity())
        self.assertEqual(
            fresh_ref.fact_state(), stale_ref.fact_state(),
            "freshness 属于时效维度，不得进入事实冲突判定",
        )
        self.assertEqual(
            1,
            len(_hypothesis(evidence=(_evidence(fresh_ref), _evidence(stale_ref))).evidence),
        )

    def test_AI_TYPED_09_kind_is_part_of_the_identity(self):
        """AI-TYPED-09：snapshot kind 参与 identity —— 横截面与单票不得相撞。"""
        single = _ref(code=DEFAULT_CODE)
        self.assertIn(SNAPSHOT_KIND, single.source_id)

        # policy 也参与：同一份快照在不同口径下是不同的研究事实。
        self.assertNotEqual(
            _derived_identity(code=DEFAULT_CODE),
            _derived_identity(code=DEFAULT_CODE).replace(DEFAULT_POLICY, "close_snapshot"),
        )


# ---------------------------------------------------------------------------
# RVERIFY-* —— B2C-2：research core 消费 **owner-native** 核验
# ---------------------------------------------------------------------------


class OwnerNativeVerificationTests(unittest.TestCase):
    """R27-B2C-2：`ResearchEvidenceRef` 的 canonical 核验是 owner-neutral 的。

    本轮要消除的结构性耦合：ref 曾经直接携带 ``verification`` /
    ``verification_method``（一对 **market 形状**的字段），而
    ``_owner_verification_pair()`` 实际只会问 ``MarketDataSnapshot`` 是否合法。于是
    execution / news / adaptive 想进入 research 时只剩两个错误选择：假装自己是
    ``market_data``，或把自己的核验结论**翻译**成 market 词 —— 后者就是由非 owner
    发明核验结论。

    现在 owner 自己发布 :class:`ai_research_contract.OwnerVerification`，research core
    只消费 ``outcome``（三态）而不解释任何 owner 的状态字符串。
    """

    def test_RVERIFY_01_market_ref_compatibility_surface_is_unchanged(self):
        """RVERIFY-01：market 事实的既有读取行为与 B2C-2 之前**逐字相同**。

        B2C-2 不做 B3 的 API/UI cleanup，因此 ``verification`` /
        ``verification_method`` / ``cross_source_verified`` 三个 market 读法必须保持
        原语义；canonical storage 换成 owner-neutral 值对象**不得**改变它们的值。
        """
        cross = _ref(validation=VALIDATION_CROSS_SOURCE)
        self.assertEqual(MDC.VERIFICATION_VERIFIED, cross.verification)
        self.assertEqual(MDC.VERIFICATION_METHOD_CROSS_SOURCE, cross.verification_method)
        self.assertIs(True, cross.cross_source_verified)
        self.assertIs(True, cross.is_verified)

        single = _ref(validation=VALIDATION_SINGLE_SOURCE)
        self.assertEqual(MDC.VERIFICATION_SINGLE_SOURCE, single.verification)
        self.assertIs(False, single.cross_source_verified)
        self.assertIs(False, single.is_verified)

        disagreement = _ref(validation=VALIDATION_DISAGREEMENT)
        self.assertEqual(MDC.VERIFICATION_DISAGREEMENT, disagreement.verification)

        # 投影里既有的 market key/value 一个不少、一个不改（additive only）。
        projected = cross.projection()
        for key, value in (
            ("source_type", ARC.EVIDENCE_SOURCE_MARKET_DATA),
            ("source_id", _derived_identity()),
            ("as_of", DAY),
            ("verification", MDC.VERIFICATION_VERIFIED),
            ("verification_method", MDC.VERIFICATION_METHOD_CROSS_SOURCE),
            ("cross_source_verified", True),
        ):
            with self.subTest(key=key):
                self.assertEqual(value, projected[key])
        # additive 的 owner-neutral 维度同时存在。
        self.assertIs(True, projected["is_verified"])
        self.assertEqual(
            MDC.VERIFICATION_METHOD_CROSS_SOURCE,
            projected["verification_attributes"]["verification_method"],
        )

        # InformationEvent 的 market 兼容读法同样保持。
        event = ARC.InformationEvent(as_of=DAY, source="s", evidence_ref=cross)
        self.assertEqual(MDC.VERIFICATION_VERIFIED, event.verification)
        self.assertEqual(MDC.VERIFICATION_METHOD_CROSS_SOURCE, event.verification_method)
        self.assertEqual(MDC.VERIFICATION_METHOD_CROSS_SOURCE,
                         event.projection()["verification_method"])

    def test_RVERIFY_02_verified_judgement_does_not_depend_on_market_vocabulary(self):
        """RVERIFY-02：research 的"是否通过核验"判据与 market 状态词**解耦**。

        构造一条 ``source_type=execution``、``status="owner_verified"`` 的事实 —— 这个
        状态词**刻意不等于** ``MDC.VERIFICATION_VERIFIED``。若 hypothesis 判定仍然比较
        market 字符串，它就永远不可能被判定为"通过核验"，本用例必红。

        同时确认 research core 不再 import/比较任何 market 核验常量：静态断言
        ``_derive_status`` 与 ``HypothesisEvidence.is_verified`` 的源码里不出现
        ``VERIFICATION_`` 常量。
        """
        self.assertNotEqual(MDC.VERIFICATION_VERIFIED, OWNER_NATIVE_STATUS)

        ref = _owner_native_ref()
        self.assertEqual(ARC.EVIDENCE_SOURCE_EXECUTION, ref.source_type)
        self.assertEqual(OWNER_NATIVE_STATUS, ref.verification)
        self.assertIs(True, ref.is_verified)
        self.assertIs(True, ARC.HypothesisEvidence(
            ref=ref, relation=ARC.RELATION_SUPPORTS,
        ).is_verified)

        hypothesis = _hypothesis(evidence=(_evidence(ref, ARC.RELATION_SUPPORTS),))
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, hypothesis.status)
        self.assertIsNone(hypothesis.reason)

        # 非 market 事实不提供 market 的两个兼容字段，且**不**用 market 词填充。
        self.assertIsNone(ref.verification_method)
        self.assertIs(False, ref.cross_source_verified)

        # 静态：通用 hypothesis 判定里不得再引用 market 核验常量。
        symbols = _function_symbols(_source(CONTRACT_MODULE), "_derive_status")
        for forbidden in ("VERIFICATION_UNAVAILABLE", "VERIFICATION_DISAGREEMENT",
                          "VERIFICATION_VERIFIED"):
            with self.subTest(constant=forbidden):
                self.assertNotIn(
                    forbidden, symbols,
                    "_derive_status 仍引用 market 核验常量 —— research core 不得依赖 market 词表",
                )

    def test_RVERIFY_03_owner_is_verified_false_cannot_support_regardless_of_spelling(self):
        """RVERIFY-03：owner 说"没通过核验"时，任何状态词拼写都不能让它支持假设。"""
        for status in (OWNER_NATIVE_STATUS, "verified", "partially_verified", "not_executed"):
            with self.subTest(status=status):
                ref = _owner_native_ref(
                    outcome=ARC.OWNER_OUTCOME_UNVERIFIED, status=status,
                )
                self.assertIs(False, ref.is_verified)
                hypothesis = _hypothesis(evidence=(_evidence(ref, ARC.RELATION_SUPPORTS),))
                self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
                self.assertEqual(
                    ARC.RESEARCH_REASON_EVIDENCE_NOT_VERIFIED, hypothesis.reason,
                    "未通过核验的 supports 只能报 evidence_not_verified",
                )

        # 非空性对照：同一状态词、outcome=verified → 必须支持。否则上面的断言可能只是
        # 因为"非 market 事实永远不通过"而变绿。
        supported = _hypothesis(evidence=(
            _evidence(_owner_native_ref(outcome=ARC.OWNER_OUTCOME_VERIFIED), ARC.RELATION_SUPPORTS),
        ))
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, supported.status)

    def test_RVERIFY_04_owner_verification_is_deeply_immutable(self):
        """RVERIFY-04：owner 核验结论（含嵌套 attributes）必须**递归**不可改。"""
        ref = _owner_native_ref(attributes={
            "verification_scope": "execution_order_fill_evidence",
            "verification_source": "ledger",
            "nested": {"sources": ["ledger", "fills"], "counts": {"n": 2}},
        })
        verification = ref.owner_verification

        with self.assertRaises((TypeError, AttributeError)):
            verification.outcome = ARC.OWNER_OUTCOME_UNVERIFIED
        with self.assertRaises((TypeError, AttributeError)):
            verification.status = "spoofed"
        with self.assertRaises(TypeError):
            verification.attributes["verification_source"] = "spoofed"
        with self.assertRaises(TypeError):
            verification.attributes["nested"]["sources"] += ("extra",)
        with self.assertRaises(TypeError):
            verification.attributes["nested"]["counts"]["n"] = 99

        # 通过 ref 拿到的视图同样不可写。
        with self.assertRaises(TypeError):
            ref.verification_attributes["verification_scope"] = "spoofed"

        # 值本身没被上面的尝试改掉。
        self.assertEqual("ledger", verification.attributes["verification_source"])
        self.assertEqual(("ledger", "fills"), verification.attributes["nested"]["sources"])
        self.assertEqual(2, verification.attributes["nested"]["counts"]["n"])

        # 调用方保留的原始 dict 继续被改写也不影响已发布的值。
        original = {"verification_source": "ledger", "extra": {"items": [1]}}
        held = _owner_native_ref(attributes=original)
        original["extra"]["items"].append(2)
        original["new_key"] = "leak"
        self.assertEqual((1,), held.verification_attributes["extra"]["items"])
        self.assertNotIn("new_key", held.verification_attributes)

    def test_RVERIFY_05_changed_owner_verification_state_is_a_conflict(self):
        """RVERIFY-05：同 identity + owner 核验状态不同 → EvidenceConflict（顺序无关）。

        这是 ``fact_state`` owner-neutral 化的行为证明：状态比较不再只看 market 那一对
        字段，而是 owner 发布的整个核验结论。
        """
        verified = _owner_native_ref(outcome=ARC.OWNER_OUTCOME_VERIFIED)
        unverified = _owner_native_ref(outcome=ARC.OWNER_OUTCOME_UNVERIFIED)
        unusable = _owner_native_ref(outcome=ARC.OWNER_OUTCOME_SOURCE_UNUSABLE)

        self.assertEqual(verified.identity(), unverified.identity())
        self.assertNotEqual(verified.fact_state(), unverified.fact_state())

        for label, order in (
            ("verified,unverified", (verified, unverified)),
            ("unverified,verified", (unverified, verified)),
            ("verified,source_unusable", (verified, unusable)),
        ):
            with self.subTest(order=label):
                with self.assertRaises(ARC.EvidenceConflict):
                    _hypothesis(evidence=tuple(_evidence(ref) for ref in order))

        # 完全相同 → 安全去重。
        identical = _hypothesis(evidence=(_evidence(verified), _evidence(_owner_native_ref())))
        self.assertEqual(1, len(identical.evidence))

    def test_RVERIFY_06_market_verification_method_remains_part_of_conflict_state(self):
        """RVERIFY-06：market 的 ``verification_method`` 仍参与冲突判定。

        R24 的永久不变量：``verified + cross_source`` 与 ``verified + coverage_integrity``
        是**不同**的核验结论（后者不是逐票双源）。owner-neutral 化以后，差异必须由
        ``OwnerVerification.attributes`` 承载并进入 canonical form —— 只比较 ``status``
        会让这两条事实被误判成同一条（并静默去重）。
        """
        cross = _market_ref_with_method(method=MDC.VERIFICATION_METHOD_CROSS_SOURCE)
        coverage = _market_ref_with_method(method=MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY)

        # 前置：这两条事实的 status 相同、method 不同 —— 正是最容易被压平的情形。
        self.assertEqual(MDC.VERIFICATION_VERIFIED, cross.verification)
        self.assertEqual(MDC.VERIFICATION_VERIFIED, coverage.verification)
        self.assertNotEqual(cross.verification_method, coverage.verification_method)
        self.assertEqual(cross.identity(), coverage.identity())

        self.assertNotEqual(
            cross.fact_state(), coverage.fact_state(),
            "status 相同但 method 不同必须算事实状态不同（attributes 参与 canonical form）",
        )
        for label, order in (("cross,coverage", (cross, coverage)),
                             ("coverage,cross", (coverage, cross))):
            with self.subTest(order=label):
                with self.assertRaises(ARC.EvidenceConflict):
                    _hypothesis(evidence=tuple(_evidence(ref) for ref in order))

        # attributes 键顺序不影响 canonical form（deterministic）。
        reordered = ARC.OwnerVerification(
            outcome=ARC.OWNER_OUTCOME_VERIFIED, status=MDC.VERIFICATION_VERIFIED,
            attributes={
                "cross_source_verified": True,
                "verification_method": MDC.VERIFICATION_METHOD_CROSS_SOURCE,
            },
        )
        self.assertEqual(
            cross.owner_verification.canonical(), reordered.canonical(),
            "canonical form 必须与 attributes 的插入顺序无关",
        )

    def test_RVERIFY_07_market_cross_source_semantics_stay_delegated_to_r24(self):
        """RVERIFY-07：market 的 ``cross_source_verified`` 仍然**委托** R24，不由本层推断。

        ``verified + coverage_integrity`` 不是逐票双源，因此 ``cross_source_verified``
        必须是 ``False`` 而 ``is_verified`` 仍是 ``True`` —— 说明"是否通过核验"与
        "是否真的双源"是两个问题，后者仍然只有 R24 能回答。
        """
        coverage = _market_ref_with_method(method=MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY)
        self.assertEqual(MDC.VERIFICATION_VERIFIED, coverage.verification)
        self.assertIs(True, coverage.is_verified)
        self.assertIs(False, coverage.cross_source_verified)
        self.assertEqual(
            MDC.is_cross_source_verified(MDC.MarketDataSnapshot(
                kind="symbol_quote", verification=coverage.verification,
                verification_method=coverage.verification_method,
            )),
            coverage.cross_source_verified,
        )

        # 静态：本层不自行比较 market 的 verified 字面量（判据必须来自 R24）。
        compares = [
            node for node in ast.walk(_tree(CONTRACT_MODULE))
            if isinstance(node, ast.Compare)
            and any(isinstance(c, ast.Constant) and c.value == "verified"
                    for c in [node.left, *node.comparators])
        ]
        self.assertEqual(
            [], compares,
            "契约自行比较了 market 的 verified 字面量 —— 双源判据必须委托 R24",
        )

        # 行为：coverage_integrity 的 verified 事实仍然支持假设（核验通过），
        # 但双源结论如实为 False。
        hypothesis = _hypothesis(evidence=(_evidence(coverage, ARC.RELATION_SUPPORTS),))
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, hypothesis.status)
        self.assertIs(False, hypothesis.evidence[0].ref.cross_source_verified)

    def test_RVERIFY_08_non_market_verification_does_not_require_a_market_method(self):
        """RVERIFY-08：非 market 事实**不**被要求提供 ``verification_method``。

        本轮禁止的两个错误做法：把 execution 的 ``verification_source`` 塞进
        ``verification_method``，或用 ``MDC.VERIFICATION_METHOD_NONE`` 代表"非 market
        owner"。前者是把 market 语义强加给别的 owner，后者是让 market 词表继续充当
        通用坐标。
        """
        ref = _owner_native_ref(
            attributes={
                "verification_scope": "execution_order_fill_evidence",
                "verification_version": "execution-fact-v1",
                "verification_source": "ledger",
            },
        )
        self.assertIsNone(
            ref.verification_method,
            "非 market 事实的 verification_method 必须是明确的「不适用」，而不是 market 词",
        )
        self.assertNotEqual(MDC.VERIFICATION_METHOD_NONE, ref.verification_method)
        self.assertIs(False, ref.cross_source_verified)
        self.assertNotIn("verification_method", ref.verification_attributes)

        # owner-specific 维度完整保留（research core 只保存，不解释）。
        self.assertEqual("ledger", ref.verification_attributes["verification_source"])
        self.assertEqual(
            "execution-fact-v1", ref.verification_attributes["verification_version"],
        )
        self.assertIsNone(ref.projection()["verification_method"])
        self.assertIs(False, ref.projection()["cross_source_verified"])

        # InformationEvent 同样不要求 market method。
        event = ARC.InformationEvent(as_of=DAY, source="execution_owner", evidence_ref=ref)
        self.assertIsNone(event.verification_method)
        self.assertIs(True, event.is_verified)
        self.assertIsNone(event.projection()["verification_method"])
        self.assertEqual(ARC.EVENT_EXECUTION_OBSERVED, event.kind)

    def test_RVERIFY_09_research_evidence_ref_still_has_no_public_constructor(self):
        """RVERIFY-09：改签名不得放宽签发边界 —— 仍然没有公开 raw 构造器。"""
        for attempt in (
            {"source_type": ARC.EVIDENCE_SOURCE_EXECUTION, "source_id": "x", "as_of": DAY},
            {"source_type": ARC.EVIDENCE_SOURCE_MARKET_DATA, "source_id": "x", "as_of": DAY,
             "owner_verification": ARC.OwnerVerification(
                 outcome=ARC.OWNER_OUTCOME_VERIFIED, status=OWNER_NATIVE_STATUS)},
        ):
            with self.subTest(source_type=attempt["source_type"]):
                with self.assertRaises(TypeError) as caught:
                    ARC.ResearchEvidenceRef(**attempt)
                self.assertIn("no public constructor", str(caught.exception))

        # 私有签发口**要求** owner 发布的核验结论：省略它无法签发一条"没有核验"的证据。
        with self.assertRaises(TypeError):
            ARC._issue_evidence_ref(
                source_type=ARC.EVIDENCE_SOURCE_EXECUTION, source_id="x", as_of=DAY,
                detail={},
            )
        # duck-typed 对象不能冒充 OwnerVerification。
        with self.assertRaises(TypeError):
            ARC._issue_evidence_ref(
                source_type=ARC.EVIDENCE_SOURCE_EXECUTION, source_id="x", as_of=DAY,
                owner_verification="verified", detail={},
            )

    def test_RVERIFY_10_private_issuer_and_factory_registry_boundary_unchanged(self):
        """RVERIFY-10：私有签发口边界与 factory registry 保持不变。

        B2C-2 只让**核心数据结构**从 market-shaped 变成 owner-neutral，**没有**提前
        登记 execution adapter（那是 B2C-3）：公开 evidence factory 仍然只有
        ``market_data`` 一个，``SUPPORTED_OWNER_ADAPTERS`` 不得扩张。
        """
        exported = frozenset(
            name for name in ARC.__all__ if name.startswith("evidence_ref_from_")
        )
        self.assertEqual(
            frozenset({"evidence_ref_from_market_reading"}), exported,
            "B2C-2 不得新增公开 owner factory（execution adapter 属于 B2C-3）",
        )
        self.assertEqual(frozenset({ARC.EVIDENCE_SOURCE_MARKET_DATA}),
                         ARC.SUPPORTED_OWNER_ADAPTERS)
        self.assertFalse(hasattr(ARC, "_OWNER_ISSUED"),
                         "不得存在可 import 的构造哨兵（那是伪安全）")

        # 契约模块里签发口只被 owner factory 调用（**唯一**一处）。
        tree = _tree(CONTRACT_MODULE)
        issuer_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "_issue_evidence_ref"
        ]
        self.assertEqual(1, len(issuer_calls), "契约里出现了第二个私有签发调用点")


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

        # 标量与 None 是允许的（bytes 刻意不在其中 —— 见 AI-18b）。
        event = ARC.InformationEvent(
            as_of=DAY, source="s", evidence_ref=_ref(),
            payload={"price": 10.5, "flag": True, "note": None, "count": 3},
        )
        self.assertEqual(10.5, event.payload["price"])
        self.assertIsNone(event.payload["note"])

    def test_AI18b_bytes_fails_closed_at_the_contract_boundary(self):
        """AI-18b：``bytes`` 在契约边界就拒绝 —— 绝不放行到下游的裸 json.dumps。

        R27 的 payload / detail 是要投给 JSON provider 的 JSON-like 内容，而
        ``json.dumps`` **从不**接受 bytes。若契约放行 bytes，一个**合法构造**的 typed
        event 会一路走到下游序列化才抛
        ``TypeError: Object of type bytes is not JSON serializable`` ——
        那是一个没有契约的失败：不在本层、没有 machine reason、也无法保证在任何网络
        调用之前发生。本层因此是唯一正确的拒绝点。

        拒绝文案逐字列出合法标量，使"bytes 会被拒绝"这件事可被调用方稳定依赖。
        """
        cases = (
            (b"x", "bytes"),
            (bytearray(b"x"), "bytearray"),
            ({"k": b"x"}, "bytes"),
            ([b"x"], "bytes"),
            ({"s": {b"x"}}, "bytes"),
            ({"deep": {"inner": [b"x"]}}, "bytes"),
        )
        for bad, offender in cases:
            with self.subTest(value=type(bad).__name__):
                with self.assertRaises(TypeError) as caught:
                    ARC.InformationEvent(
                        as_of=DAY, source="s", evidence_ref=_ref(), payload={"v": bad},
                    )
                message = str(caught.exception)
                self.assertIn(f"got {offender}", message)
                self.assertIn("JSON-like", message)

    def test_AI18c_non_string_mapping_keys_are_rejected(self):
        """AI-18c：mapping key 也必须是 ``str`` —— 只查 value 会留下同族的后门。

        ``{b"k": 1}`` 的 value 完全合法，却会让 ``json.dumps`` 抛
        ``TypeError: keys must be str, int, float, bool or None, not bytes``。
        契约声称"payload 是 JSON-like 内容"，这个声称就必须在**构造期**完整成立，
        而不是只对 value 成立。

        ``detail`` 走同一个 freezer，因此自动受同一约束。
        """
        for bad_key in (b"k", 1, None, ("a",), True):
            with self.subTest(key=type(bad_key).__name__):
                with self.assertRaises(TypeError) as caught:
                    ARC.InformationEvent(
                        as_of=DAY, source="s", evidence_ref=_ref(), payload={bad_key: 1},
                    )
                self.assertIn("mapping keys must be str", str(caught.exception))

    def test_AI18d_accepted_content_is_always_json_like(self):
        """AI-18d：契约接受的任何 payload，递归下去只有 JSON-like 标量与 str key。

        这是 AI-18b / AI-18c 的**非空性**对照：把 freezer 收紧成一律拒绝也能让上面两条
        变绿，所以必须同时证明"该接受的确实接受"。

        断言的是**契约自己的**保证（递归的标量 / key 形状），而不是"能直接
        ``json.dumps``"：契约把 set 规范化为 frozenset、把序列规范化为 tuple，
        再由 provider 的 ``_jsonable`` 做一层形状还原 —— 端到端的可序列化由
        RPROV-24 在 provider 那一侧锁定。契约层不 import provider（依赖方向单向）。
        """
        payload = {
            "price": 10.5, "flag": True, "note": None, "count": 3,
            "tags": {"a", "b"}, "items": (1, 2), "nested": {"deep": [{"x": 1}]},
        }
        event = ARC.InformationEvent(
            as_of=DAY, source="s", evidence_ref=_ref(), payload=payload,
        )

        leaves = []

        def walk(value):
            if isinstance(value, Mapping):
                for key, item in value.items():
                    self.assertIsInstance(key, str)
                    walk(item)
                return
            if isinstance(value, (tuple, frozenset, list, set)):
                for item in value:
                    walk(item)
                return
            leaves.append(value)

        walk(event.payload)
        self.assertTrue(leaves, "非空性：必须真的走到了叶子")
        for leaf in leaves:
            with self.subTest(leaf=repr(leaf)[:30]):
                self.assertTrue(
                    leaf is None or isinstance(leaf, (str, bool, int, float)),
                    f"契约放行了非 JSON-like 标量 {type(leaf).__name__}",
                )
                self.assertNotIsInstance(leaf, (bytes, bytearray))

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
        """AIG-02：现有 authority 不得 import AI 研究层（依赖方向单向）。

        R27-B1 起 AI 层有多个模块（契约 / transport / typed adapter / persistence
        owner）；authority 反向 import **其中任何一个**都算违规 —— 只守住契约会留下
        "authority 直接 import adapter 发请求"或"直接 import repository 写研究台账"
        这两个后门。
        """
        offenders = []
        for name in AUTHORITY_MODULES:
            for imported in _imported_names(_tree(name)):
                if imported.split(".")[0] in (
                    "ai_research_contract", "ai_research_provider", "ai_provider_transport",
                    "ai_research_repository", "ai_research_service",
                ):
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
