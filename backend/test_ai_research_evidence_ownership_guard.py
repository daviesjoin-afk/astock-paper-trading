# -*- coding: utf-8 -*-
"""R27-B2C —— research evidence 的 **contract-issued boundary**。

────────────────── 两层不变量，必须分清 ──────────────────

**第 1 层（本文件真正强制，今天就能可靠做到）：contract-issued evidence boundary**

    ResearchEvidenceRef 无 public raw constructor；
    _issue_evidence_ref 的调用者是**精确 allowlist**（module + enclosing function，契约外
      零调用、契约内也只能是已批准的 owner factory）；
    public evidence factory 的 (module, factory) 对是**精确 allowlist**，且必须真的解析到
      那个模块**导出**的那个符号；
    owner registry 与 factory registry 双向强制一致，不许静默漂移；
    InformationEvent 在运行期只接受真正的 ResearchEvidenceRef。

**第 2 层（R27 最终目标，**尚未**完成 —— OPEN / REQUIRED，不是 WONTFIX）：owner-origin provenance**

    一条 evidence 不只是"经过了 contract factory"，还必须能证明 **factory 的输入本身
    来自该 canonical owner**，而不是调用方手工造了一份长得一样的 typed object。

    今天**三条** owner 路径都做不到这一点：``MarketDataSnapshot`` / ``MarketDataReading``、
    ``ExecutionEvidence`` 都是公开可构造的类型，而 ``PortfolioFactProjection`` 虽然有私有
    构造器，调用方仍可自造 SQLite connection / fixture 调 owner 的 public read 拿到投影。
    所以"手工造输入 → factory"仍能得到一个 ref。这条限制由
    ``test_ai_research_contract`` 的
    ``AI_TYPED_06_two_step_forgery_is_documented_not_claimed_closed``、
    ``test_ai_research_execution_adapter`` 的 ``EXEC-REF-18`` 与
    ``test_ai_research_portfolio_adapter`` 的 ``PORT-REF-18`` 明确记录。

    因此本文件**不**声称"已经证明所有 evidence 都是 owner-originated"。第 1 层只是
    **必要条件**。owner-origin provenance 必须由 owner/provenance 架构关闭
    （execution → news → adaptive/experiment → runtime/incident，见
    ``docs/R27_B2C_EVIDENCE_OWNER_MATRIX.md``），在 R27 宣布完成之前不得降级。

────────────────── 为什么这里没有静态数据流分析 ──────────────────

本文件曾经尝试自己实现 import 别名解析 + 作用域分析 + reaching-definition +
支配关系 + branch/try 特例，以证明"每个 InformationEvent 的 evidence_ref 都来自 owner"。
那套实现**不能可靠证明它声称的不变量**，而且会持续膨胀成一个劣质静态分析器。

现在改成四条**确定、结构化**的边界，并且明确写出各自能证明什么：

* 谁可以构造 ``InformationEvent`` —— 模块集合显式登记（新增模块必须改这里，是架构变化）；
* 谁可以签发 ref —— 每个 ``evidence_ref_from_*`` 调用必须解析到**已登记模块导出的**那个
  工厂：本地同名函数 / 其它对象的同名方法 / **另一个已登记模块的同名工厂**全部拒绝；
* 谁可以调用私有签发口 —— ``(模块, enclosing function)`` 精确 allowlist；
* ref 在运行期是否真的是 ref —— 由 ``InformationEvent.__post_init__`` 的类型检查保证。

刻意**不再**静态追踪局部变量（``ref = ...; InformationEvent(evidence_ref=ref)``）的来源：
那条保证由运行期类型检查与构造模块集合登记承担。同样刻意**不**做控制流 / reaching
definition / dominance / branch / try-flow 分析：私有签发口的边界只回答"哪个模块的哪个
函数调用了它"，这一层结构事实已经足够，再往下就是近似分析。
"""
from __future__ import annotations

import ast
import importlib
import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_research_contract as ARC  # noqa: E402

CONTRACT_MODULE_FILE = "ai_research_contract.py"
CONTRACT_MODULE_NAME = "ai_research_contract"

#: 受保护符号：构造研究事件、构造证据引用、以及私有签发口。
PROTECTED_SYMBOLS = frozenset({"InformationEvent", "ResearchEvidenceRef", "_issue_evidence_ref"})

#: owner 签发口的命名空间前缀。**前缀本身不构成授权** —— 见 :func:`_approved_factory`。
OWNER_FACTORY_PREFIX = "evidence_ref_from_"

#: 允许的 owner → ``(承载 factory 的模块, 该模块导出的 factory)``。**精确等值**，不是
#: contains / prefix / non-empty。新增一个 owner 必须同时出现：owner 自己发布核验闭集 +
#: 这里的一行 + ``SUPPORTED_OWNER_ADAPTERS`` 的一行。
#:
#: factory **不必都住在契约文件里**（R27-B2C-3 起 execution 的住在
#: ``ai_research_execution_adapter``）：一个必须同时认识 owner 词表与 research 契约的
#: 接缝住在哪，是这个 registry 要回答的问题，而不是"谁 import 了谁"。
EXPECTED_OWNER_FACTORIES = {
    "market_data": ("ai_research_contract", "evidence_ref_from_market_reading"),
    "execution": (
        "ai_research_execution_adapter", "evidence_ref_from_execution_projection",
    ),
    "portfolio_research": (
        "ai_research_portfolio_adapter", "evidence_ref_from_portfolio_projection",
    ),
}

#: 需要在 import 别名解析里被识别的模块 —— 已登记 factory 的宿主模块。
ADAPTER_MODULE_NAMES = frozenset(
    module for module, _factory in EXPECTED_OWNER_FACTORIES.values()
)

#: 已登记的 ``(模块, factory)`` 对。授权判据就是这个集合，不是"名字像前缀"。
OWNER_FACTORY_ORIGINS = frozenset(EXPECTED_OWNER_FACTORIES.values())

#: 允许调用私有签发口 ``_issue_evidence_ref`` 的 ``(模块文件, 直接包裹调用的函数)``。
#:
#: **精确 allowlist，双向等值**：少一个（登记了却不调用）或多一个（有人偷偷调用）都算
#: 违规。R27-B2C-3 引入第二个 approved factory 时把 B2C-2 的
#: "契约外零调用" 升级成这条 caller-set 等值；R27-B2C-4B 引入 portfolio/accounting 的
#: 第三个 —— 只需 module + enclosing function 这一层结构信息，不需要 CFG。
APPROVED_ISSUER_CALLERS = frozenset({
    (CONTRACT_MODULE_FILE, "evidence_ref_from_market_reading"),
    ("ai_research_execution_adapter.py", "evidence_ref_from_execution_projection"),
    ("ai_research_portfolio_adapter.py", "evidence_ref_from_portfolio_projection"),
})

#: 目前生产里构造 ``InformationEvent`` 的模块集合。等值断言：多一个模块就是一次
#: 架构变化（多出一条"事实 → 研究事件"的路径），必须人工修改这里。
EXPECTED_EVENT_CONSTRUCTORS = frozenset({"deepseek_advisor.py"})


def _source(name: str) -> str:
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


def _tree(name: str) -> ast.Module:
    return ast.parse(_source(name))


def _production_modules() -> list[str]:
    return sorted(
        name for name in os.listdir(BACKEND)
        if name.endswith(".py") and not name.startswith("test_")
    )


# ─────────────────────────────────────────────────────────────────────────────
# 调用的真实来源 —— 只回答一个问题：
# "这个 callable 是否解析到某个已登记模块导出的那个工厂？"
# ─────────────────────────────────────────────────────────────────────────────


def _import_origins(tree: ast.Module) -> dict[str, tuple[str, str | None]]:
    """本地名 → ``(模块, 导出名)``。只记录**已登记模块**的 import。

    ``import ai_research_contract as ARC``      → ``{"ARC": ("ai_research_contract", None)}``
    ``from ai_research_execution_adapter import f as g``
                                                → ``{"g": ("ai_research_execution_adapter", "f")}``
    """
    origins: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in ADAPTER_MODULE_NAMES:
                    origins[alias.asname or root] = (root, None)
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in ADAPTER_MODULE_NAMES:
                for alias in node.names:
                    origins[alias.asname or alias.name] = (module, alias.name)
    return origins


def _call_origin(node: ast.Call, origins) -> tuple[str | None, str]:
    """``(module_origin, symbol)``。

    * ``ARC.f(...)`` 且 ``ARC`` 来自某个已登记模块 → ``("<module>", "f")``
    * ``f(...)`` 且 ``f`` 来自 ``from <module> import f`` → ``("<module>", "f")``
    * ``f(...)`` 且 ``f`` 是某导出名的别名 → 归一到原名
    * ``obj.f(...)`` / ``other.f(...)`` → ``(None, "f")``（**来源不明，一律不算授权**）
    * 本模块 ``def f(...)`` → ``(None, "f")``
    """
    func = node.func
    if isinstance(func, ast.Name):
        origin = origins.get(func.id)
        if origin is not None:
            module, exported = origin
            return module, (exported or func.id)
        return None, func.id
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name):
            origin = origins.get(base.id)
            if origin is not None and origin[1] is None:
                return origin[0], func.attr
            if origin is not None and origin[1] is not None:
                # ``f.attr`` 其中 f 是从某模块导入的名字 —— 不是模块属性访问，来源不明
                return None, func.attr
        return None, func.attr
    return None, ""


def _protected_symbol(node: ast.Call, origins) -> str | None:
    """这次调用是否是受保护符号（别名会被还原）。

    裸名（``InformationEvent``）**始终**算受保护，即使它并非从契约导入 —— 否则
    "本地定义一个同名工厂"就能让构造点从扫描里消失。
    """
    module, symbol = _call_origin(node, origins)
    if module == CONTRACT_MODULE_NAME and symbol in PROTECTED_SYMBOLS:
        return symbol
    if module is None and symbol in PROTECTED_SYMBOLS:
        return symbol
    return None


def _is_owner_factory_namespace(node: ast.Call, origins) -> bool:
    _, symbol = _call_origin(node, origins)
    return symbol.startswith(OWNER_FACTORY_PREFIX)


#: ``(module, factory)`` → 该模块是否真的导出这个可调用符号。import 有代价，缓存一次。
_EXPORT_CACHE: dict[tuple[str, str], bool] = {}


def _module_exports_factory(module: str, factory: str) -> bool:
    """这个模块是否**真的导出**这个 factory 符号（``__all__`` 里且可调用）。

    origin（模块）与符号必须**同时**匹配：只比终端名字会让
    ``ai_research_contract.evidence_ref_from_execution_projection`` 这类
    "在别的模块里借用一个已登记的名字"通过。
    """
    key = (module, factory)
    if key not in _EXPORT_CACHE:
        try:
            imported = importlib.import_module(module)
        except ImportError:  # pragma: no cover - 登记表写着不存在的模块
            _EXPORT_CACHE[key] = False
            return False
        _EXPORT_CACHE[key] = (
            factory in getattr(imported, "__all__", ())
            and callable(getattr(imported, factory, None))
        )
    return _EXPORT_CACHE[key]


def _approved_factory(node: ast.Call, origins) -> bool:
    """这次调用是否**真的**是已登记模块导出的、已登记的 owner factory。

    三个条件缺一不可：

    1. 来源是某个已登记模块（不是本地同名函数、不是其它对象的同名方法）；
    2. ``(模块, 符号)`` 是已登记的授权对 —— 因此
       ``ai_research_contract.evidence_ref_from_execution_projection`` 与
       ``ai_research_execution_adapter.evidence_ref_from_market_reading`` 都不算；
    3. 该模块**确实导出**该符号（``__all__`` + 可调用）。
    """
    module, symbol = _call_origin(node, origins)
    if module is None:
        return False
    if (module, symbol) not in OWNER_FACTORY_ORIGINS:
        return False
    return _module_exports_factory(module, symbol)


def _contract_exported_factories() -> frozenset[str]:
    """契约模块自己导出的 factory 名字集合（execution 的那一份不在其中）。"""
    return frozenset(
        name for name in getattr(ARC, "__all__", ())
        if name.startswith(OWNER_FACTORY_PREFIX)
    )


def _registry_problems(owners, registered) -> list[str]:
    """owner registry 与 factory registry 的双向一致性。

    单独抽成纯函数，是为了让"两个方向都要红"能被**直接测到**（见非空性用例），
    而不是只能靠改契约源码来验。
    """
    problems = []
    missing_factories = sorted(set(owners) - set(registered))
    if missing_factories:
        problems.append(f"owner 已登记但没有 factory：{missing_factories}")
    unregistered_factories = sorted(set(registered) - set(owners))
    if unregistered_factories:
        problems.append(
            f"factory 已存在但对应 owner 未登记：{unregistered_factories}"
            "（新增 owner 必须先让该 owner 发布自己的核验闭集）"
        )
    return problems


def _issuer_calls(tree: ast.Module) -> list[tuple[str | None, int]]:
    """每次对私有签发口的调用 → ``(最近的 enclosing function, 行号)``。

    刻意只回答 **module + enclosing function** 这一层结构问题，不做 reaching
    definition / dominance / CFG / branch 或 try-flow 分析。
    """
    origins = _import_origins(tree)
    found: list[tuple[str | None, int]] = []

    def walk(node: ast.AST, func_name: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and _protected_symbol(child, origins) == "_issue_evidence_ref"
            ):
                found.append((func_name, child.lineno))
            walk(child, func_name)

    walk(tree, None)
    return found


def _issuer_caller_set() -> set[tuple[str, str | None]]:
    """production tree 里实际出现的 ``(模块文件, enclosing function)`` 签发调用集合。"""
    return {
        (name, func)
        for name in _production_modules()
        for func, _line in _issuer_calls(_tree(name))
    }


def _calls(tree: ast.Module, *, namespace: bool = False, symbol: str | None = None):
    origins = _import_origins(tree)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if namespace and _is_owner_factory_namespace(node, origins):
            out.append(node)
        elif symbol is not None and _protected_symbol(node, origins) == symbol:
            out.append(node)
    return out


class EvidenceFactoryBoundaryTests(unittest.TestCase):
    def test_EVIDENCE_01_owner_and_factory_registries_agree_exactly(self):
        """EVIDENCE-01：owner registry 与已登记的 factory 集合双向等值。

        两个方向都必须红：新增 factory 而没人登记 owner，或者登记了 owner 却没有
        factory。任何一处漂移都意味着"谁能签发证据"已经不受这条边界管辖。

        另外每个 ``(模块, factory)`` 必须**真的存在**：registry 不是一句声明。
        """
        self.assertEqual(
            set(EXPECTED_OWNER_FACTORIES), set(ARC.SUPPORTED_OWNER_ADAPTERS),
            "SUPPORTED_OWNER_ADAPTERS 与已批准的 owner→factory 映射不一致",
        )
        self.assertEqual(
            [], _registry_problems(ARC.SUPPORTED_OWNER_ADAPTERS, EXPECTED_OWNER_FACTORIES),
        )

        for owner, (module, factory) in EXPECTED_OWNER_FACTORIES.items():
            with self.subTest(owner=owner, factory=factory):
                self.assertTrue(
                    _module_exports_factory(module, factory),
                    f"{module} 没有导出可调用的 {factory} —— registry 与真实代码漂移了",
                )

        # 非空性：只有真的存在 factory 时，上面两条等值断言才有内容可查。
        self.assertTrue(EXPECTED_OWNER_FACTORIES, "没有任何已登记的 owner factory")
        # 契约自己导出的 factory 只有 market 一份：execution / portfolio 的住在各自 adapter 里。
        self.assertEqual(
            frozenset({"evidence_ref_from_market_reading"}),
            _contract_exported_factories(),
            "契约模块导出的 factory 集合发生变化（execution / portfolio 的那两份应住在 adapter 模块）",
        )
        # 只借用一个已登记的名字不算授权：模块 origin 与符号必须同时匹配。
        self.assertFalse(_module_exports_factory(
            "ai_research_contract", "evidence_ref_from_execution_projection",
        ))
        self.assertFalse(_module_exports_factory(
            "ai_research_execution_adapter", "evidence_ref_from_market_reading",
        ))
        # R27-B2C-4B：contract 模块**不得**为了方便 re-export portfolio 的 factory，
        # portfolio adapter 也**不得**借用 market / execution 的 factory 名。
        self.assertFalse(_module_exports_factory(
            "ai_research_contract", "evidence_ref_from_portfolio_projection",
        ))
        self.assertFalse(_module_exports_factory(
            "ai_research_portfolio_adapter", "evidence_ref_from_market_reading",
        ))
        self.assertFalse(_module_exports_factory(
            "ai_research_portfolio_adapter", "evidence_ref_from_execution_projection",
        ))
        self.assertFalse(_module_exports_factory("ai_research_contract", "not_a_factory"))

    def test_EVIDENCE_02_research_evidence_ref_has_no_public_constructor(self):
        """EVIDENCE-02：调用方不能仅凭传字符串声明一条事实。"""
        with self.assertRaises(TypeError):
            ARC.ResearchEvidenceRef(
                source_type="market_data", source_id="forged", as_of="2026-08-27",
            )
        self.assertTrue(callable(ARC.evidence_ref_from_market_reading))
        self.assertTrue(_module_exports_factory(
            "ai_research_execution_adapter", "evidence_ref_from_execution_projection",
        ))

    def test_EVIDENCE_03_private_issuer_callers_are_an_exact_allowlist(self):
        """EVIDENCE-03：私有签发口的调用者是 ``(模块, enclosing function)`` 精确 allowlist。

        这条同时覆盖两个方向：

        * 契约**外**零调用（B2C-2 的保证，别名也会被识破）；
        * 契约**内**只能是已批准的 owner factory（B2C-3 新增，对比 EXFACT-19）——
          adapter 里出现**第二个** helper 调签发口同样 RED。

        刻意只做 module + enclosing function 这一层结构扫描，不做 CFG。
        """
        actual = _issuer_caller_set()
        self.assertEqual(
            APPROVED_ISSUER_CALLERS, actual,
            f"私有签发口的调用者集合发生变化：{sorted(actual)}",
        )
        # 非空性：allowlist 里每一对都必须真的在调用它，否则等值断言会空转。
        self.assertTrue(APPROVED_ISSUER_CALLERS)
        for module, func in APPROVED_ISSUER_CALLERS:
            with self.subTest(module=module, function=func):
                calls = _issuer_calls(_tree(module))
                self.assertIn(func, [name for name, _line in calls])

    def test_EVIDENCE_04_every_evidence_factory_call_comes_from_a_registered_module(self):
        """EVIDENCE-04：生产里每个 owner 签发命名空间调用都必须来自已登记模块。

        这是"不得新增未登记 adapter"的可执行形式：本地同名函数、其它对象的同名方法、
        其它模块的同名工厂，全部因为**来源不是已登记模块**而被拒绝；而"在另一个已登记
        模块里借用同一个工厂名"因为 **(module, symbol) 对**不匹配而被拒绝。
        """
        offenders = []
        approved = 0
        for name in _production_modules():
            tree = _tree(name)
            origins = _import_origins(tree)
            for node in _calls(tree, namespace=True):
                if _approved_factory(node, origins):
                    approved += 1
                else:
                    offenders.append(name)
        self.assertEqual(
            [], offenders,
            f"这些模块调用了来源不明的 evidence factory：{offenders}。"
            "evidence_ref_from_* 只能解析到已登记模块导出的那个工厂。",
        )
        # 非空性：真实生产里确实存在被批准的签发调用（否则上面的空 offender 无意义）。
        self.assertGreater(approved, 0, "扫描器看不到任何被批准的签发调用（护栏会空转）")

    def test_EVIDENCE_05_no_production_module_constructs_a_ref_directly(self):
        """EVIDENCE-05：生产里没有任何地方直接构造 ``ResearchEvidenceRef``。"""
        offenders = [
            name for name in _production_modules()
            if _calls(_tree(name), symbol="ResearchEvidenceRef")
        ]
        self.assertEqual([], offenders, f"直接构造 ResearchEvidenceRef：{offenders}")

    def test_EVIDENCE_06_event_constructor_module_set_is_explicit(self):
        """EVIDENCE-06：构造 typed event 的模块集合是显式登记的等值集合。

        这是删掉数据流分析之后承担主要保证的那一条：一条新的"事实 → 研究事件"路径
        必然引入一个新的构造模块，因此会被这里拦下并要求人工确认。
        """
        constructors = {
            name for name in _production_modules()
            if _calls(_tree(name), symbol="InformationEvent")
        }
        self.assertEqual(
            EXPECTED_EVENT_CONSTRUCTORS, constructors,
            f"构造 InformationEvent 的模块集合发生变化：{sorted(constructors)}",
        )

    def test_EVIDENCE_07_information_event_rejects_non_ref_evidence(self):
        """EVIDENCE-07：运行期类型边界 —— event 只接受真正的 ResearchEvidenceRef。

        数据流分析删掉之后，"传进来的那个局部变量到底是什么"由这条运行期保证回答，
        而不是靠一个近似静态分析。
        """
        for payload in ({}, "source_id", 42, None):
            with self.subTest(payload=repr(payload)[:20]):
                with self.assertRaises(TypeError):
                    ARC.InformationEvent(as_of="2026-08-27", source="s", evidence_ref=payload)


def _module_exports_factory_and_get(module: str, factory: str):
    """取回一个已登记模块的 factory（不存在即断言失败）。"""
    imported = importlib.import_module(module)
    assert _module_exports_factory(module, factory), f"{module}.{factory} 不是导出的 factory"
    return getattr(imported, factory)


class FactoryOriginNonVacuityTests(unittest.TestCase):
    """factory origin 解析必须真的能区分"来自已登记模块"与"只是名字像"。

    这些写法覆盖 review 提出的绕过方式：本地同名函数、其它对象同名方法、其它模块同名
    工厂、**在另一个已登记模块里借用同一个名字**；以及合法写法：模块别名、直接 import、
    直接 import 的别名。
    """

    def _approved(self, text: str) -> bool:
        tree = ast.parse(text)
        origins = _import_origins(tree)
        calls = _calls(tree, namespace=True)
        self.assertEqual(1, len(calls), f"扫描器看到了 {len(calls)} 个签发调用：{text!r}")
        return _approved_factory(calls[0], origins)

    def test_CASE_1_module_alias_is_approved(self):
        self.assertTrue(self._approved(
            "import ai_research_contract as ARC\n"
            "ARC.evidence_ref_from_market_reading(reading)\n"
        ))

    def test_CASE_2_direct_import_is_approved(self):
        self.assertTrue(self._approved(
            "from ai_research_contract import evidence_ref_from_market_reading\n"
            "evidence_ref_from_market_reading(reading)\n"
        ))

    def test_CASE_3_direct_import_alias_is_approved(self):
        self.assertTrue(self._approved(
            "from ai_research_contract import evidence_ref_from_market_reading as make_ref\n"
            "make_ref(reading)\n"
        ))

    def test_CASE_4_the_execution_adapter_factory_is_approved(self):
        """B2C-3 新增的第二个 owner：模块别名与直接 import 都必须被批准。"""
        self.assertTrue(self._approved(
            "import ai_research_execution_adapter as ADA\n"
            "ADA.evidence_ref_from_execution_projection(projection)\n"
        ))
        self.assertTrue(self._approved(
            "from ai_research_execution_adapter import "
            "evidence_ref_from_execution_projection as make_ref\n"
            "make_ref(projection)\n"
        ))

    def test_CASE_5_local_function_with_the_same_name_is_rejected(self):
        self.assertFalse(self._approved(
            "def evidence_ref_from_market_reading(row):\n"
            "    return row\n"
            "evidence_ref_from_market_reading(row)\n"
        ))
        self.assertFalse(self._approved(
            "def evidence_ref_from_execution_projection(row):\n"
            "    return row\n"
            "evidence_ref_from_execution_projection(row)\n"
        ))

    def test_CASE_6_same_named_method_on_another_object_is_rejected(self):
        self.assertFalse(self._approved("fake.evidence_ref_from_market_reading(row)\n"))
        self.assertFalse(self._approved(
            "fake.evidence_ref_from_execution_projection(row)\n"
        ))

    def test_CASE_7_same_named_factory_in_another_module_is_rejected(self):
        self.assertFalse(self._approved(
            "import fake_contract\n"
            "fake_contract.evidence_ref_from_market_reading(row)\n"
        ))

    def test_CASE_8_a_registered_module_cannot_lend_another_owners_factory_name(self):
        """模块 origin 与导出符号必须**同时**匹配 —— 这是 B2C-3 的关键收紧。

        ``ai_research_contract`` 是已登记模块，``ai_research_execution_adapter`` 也是；
        但"在契约模块下写 execution 的工厂名"（或反之）只是一次同名借用，不是授权。
        """
        self.assertFalse(self._approved(
            "import ai_research_contract as ARC\n"
            "ARC.evidence_ref_from_execution_projection(projection)\n"
        ))
        self.assertFalse(self._approved(
            "import ai_research_execution_adapter as ADA\n"
            "ADA.evidence_ref_from_market_reading(reading)\n"
        ))
        # 非空性对照：同一个模块写自己的工厂名就必须被批准。
        self.assertTrue(self._approved(
            "import ai_research_contract as ARC\n"
            "ARC.evidence_ref_from_market_reading(reading)\n"
        ))

    def test_CASE_9_extra_factory_without_registered_owner_fails(self):
        """新增 factory，但 owner registry 没跟上 → 必须报问题。"""
        problems = _registry_problems(["market_data"], ["market_data", "paper"])
        self.assertTrue(problems, "多余 factory 未被发现")
        self.assertTrue(any("未登记" in item for item in problems))

    def test_CASE_10_registered_owner_without_factory_fails(self):
        """owner registry 新增 news，但没有对应 factory → 必须报问题。"""
        problems = _registry_problems(["market_data", "news"], ["market_data"])
        self.assertTrue(problems, "缺失 factory 未被发现")
        self.assertTrue(any("没有 factory" in item for item in problems))
        # 双向都干净时不得假报。
        self.assertEqual([], _registry_problems(["market_data"], ["market_data"]))
        self.assertEqual(
            [], _registry_problems(
                ["market_data", "execution"], ["execution", "market_data"],
            ),
        )

    def test_CASE_12_the_portfolio_adapter_factory_is_approved(self):
        """B2C-4B 新增的第三个 owner：模块别名与直接 import 都必须被批准。"""
        self.assertTrue(self._approved(
            "import ai_research_portfolio_adapter as PFA\n"
            "PFA.evidence_ref_from_portfolio_projection(projection)\n"
        ))
        self.assertTrue(self._approved(
            "from ai_research_portfolio_adapter import "
            "evidence_ref_from_portfolio_projection as make_ref\n"
            "make_ref(projection)\n"
        ))
        # 非空性对照：execution / market 的 factory 仍然必须被批准（新 owner 不排挤旧 owner）。
        self.assertTrue(self._approved(
            "import ai_research_execution_adapter as ADA\n"
            "ADA.evidence_ref_from_execution_projection(projection)\n"
        ))

    def test_CASE_13_portfolio_factory_cannot_be_forged_or_borrowed(self):
        """B2C-4B 的四条负向路径 —— 每一条都必须被拒绝。

        1. 本地同名函数：``def evidence_ref_from_portfolio_projection`` 自己签发；
        2. 其它对象的同名方法；
        3. portfolio adapter 偷调 market / execution 的 factory（**(module, symbol) 对**
           不匹配，即使两个模块都已登记）；
        4. contract 模块偷导出 portfolio 的 factory（registry 里必须只有 market 那一份，
           且 ``ai_research_contract`` **没有**该符号）。

        刻意只回答 module + symbol 这一层结构事实，不扩展成 CFG / dataflow scanner。
        """
        self.assertFalse(self._approved(
            "def evidence_ref_from_portfolio_projection(row):\n"
            "    return row\n"
            "evidence_ref_from_portfolio_projection(row)\n"
        ))
        self.assertFalse(self._approved(
            "fake.evidence_ref_from_portfolio_projection(row)\n"
        ))
        self.assertFalse(self._approved(
            "import fake_contract\n"
            "fake_contract.evidence_ref_from_portfolio_projection(row)\n"
        ))
        # 3. 已登记模块之间不得互相借用 factory 名。
        self.assertFalse(self._approved(
            "import ai_research_portfolio_adapter as PFA\n"
            "PFA.evidence_ref_from_market_reading(reading)\n"
        ))
        self.assertFalse(self._approved(
            "import ai_research_portfolio_adapter as PFA\n"
            "PFA.evidence_ref_from_execution_projection(projection)\n"
        ))
        self.assertFalse(self._approved(
            "import ai_research_execution_adapter as ADA\n"
            "ADA.evidence_ref_from_portfolio_projection(projection)\n"
        ))
        self.assertFalse(self._approved(
            "import ai_research_contract as ARC\n"
            "ARC.evidence_ref_from_portfolio_projection(projection)\n"
        ))
        # 4. contract 模块**没有**这个符号，且 registry 里 market 仍然只对应它自己那一份。
        self.assertFalse(_module_exports_factory(
            "ai_research_contract", "evidence_ref_from_portfolio_projection",
        ))
        self.assertEqual(
            ("ai_research_contract", "evidence_ref_from_market_reading"),
            EXPECTED_OWNER_FACTORIES["market_data"],
        )
        # 非空性对照：portfolio adapter 写自己的 factory 名必须被批准。
        self.assertTrue(self._approved(
            "import ai_research_portfolio_adapter as PFA\n"
            "PFA.evidence_ref_from_portfolio_projection(projection)\n"
        ))

    def test_CASE_11_issuer_caller_scan_detects_second_helpers_and_aliases(self):
        """签发口扫描必须能看见别名，也能看见同一模块里的第二个 caller。"""
        alias_tree = ast.parse(
            "from ai_research_contract import _issue_evidence_ref as issue\n"
            "def first():\n"
            "    return issue()\n"
        )
        self.assertEqual([("first", 3)], _issuer_calls(alias_tree))

        second_helper = ast.parse(
            "import ai_research_contract as ARC\n"
            "def public_factory(projection):\n"
            "    return ARC._issue_evidence_ref()\n"
            "def helper_that_must_not_issue(projection):\n"
            "    return ARC._issue_evidence_ref()\n"
        )
        self.assertEqual(
            [("public_factory", 3), ("helper_that_must_not_issue", 5)],
            _issuer_calls(second_helper),
            "同一模块里的第二个签发调用必须被看见（B2C-3 的收紧点）",
        )
        # 嵌套函数按**最近**的 enclosing function 记账，不会归到外层。
        nested = ast.parse(
            "def outer():\n"
            "    def inner():\n"
            "        return _issue_evidence_ref()\n"
            "    return inner\n"
        )
        self.assertEqual([("inner", 3)], _issuer_calls(nested))
        # 非空性对照：不调用签发口的模块必须报 0 条。
        self.assertEqual([], _issuer_calls(ast.parse("def f():\n    return g()\n")))
        # 裸名**始终**算受保护符号（否则本地同名函数就能让调用点消失），因此"在别的对象上
        # 调同名方法"也会被报出来 —— 这是 fail closed：多报一个待人工确认，而不是漏掉。
        self.assertEqual([(None, 1)], _issuer_calls(ast.parse("obj._issue_evidence_ref()\n")))

    def test_protected_symbols_survive_import_aliases(self):
        tree = ast.parse(
            "from ai_research_contract import InformationEvent as Event\n"
            "from ai_research_contract import _issue_evidence_ref as issue\n"
            "Event(as_of='2026-08-27', source='s', evidence_ref=row)\n"
            "issue()\n"
        )
        self.assertEqual(1, len(_calls(tree, symbol="InformationEvent")))
        self.assertEqual(1, len(_calls(tree, symbol="_issue_evidence_ref")))


if __name__ == "__main__":
    unittest.main()
