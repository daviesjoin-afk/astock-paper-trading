# -*- coding: utf-8 -*-
"""R27-B2C —— research evidence 的 **contract-issued boundary**。

────────────────── 两层不变量，必须分清 ──────────────────

**第 1 层（本文件真正强制，今天就能可靠做到）：contract-issued evidence boundary**

    ResearchEvidenceRef 无 public raw constructor；
    _issue_evidence_ref 在 contract 之外零调用；
    public evidence factory 是**精确 allowlist**，且必须真的来自 ai_research_contract；
    owner registry 与 factory registry 双向强制一致，不许静默漂移；
    InformationEvent 在运行期只接受真正的 ResearchEvidenceRef。

**第 2 层（R27 最终目标，**尚未**完成 —— OPEN / REQUIRED，不是 WONTFIX）：owner-origin provenance**

    一条 evidence 不只是"经过了 contract factory"，还必须能证明 **factory 的输入本身
    来自该 canonical owner**，而不是调用方手工造了一份长得一样的 typed object。

    今天 market_data 路径做不到这一点：``MarketDataSnapshot`` / ``MarketDataReading``
    都是公开 dataclass，所以"手工造 reading → evidence_ref_from_market_reading(...)"
    仍能得到一个 ref。这条限制由 ``test_ai_research_contract`` 的
    ``AI_TYPED_06_two_step_forgery_is_documented_not_claimed_closed`` 明确记录。

    因此本文件**不**声称"已经证明所有 evidence 都是 owner-originated"。第 1 层只是
    **必要条件**。owner-origin provenance 必须由 owner/provenance 架构关闭
    （execution → news → adaptive/experiment → runtime/incident，见
    ``docs/R27_B2C_EVIDENCE_OWNER_MATRIX.md``），在 R27 宣布完成之前不得降级。

────────────────── 为什么这里没有静态数据流分析 ──────────────────

本文件曾经尝试自己实现 import 别名解析 + 作用域分析 + reaching-definition +
支配关系 + branch/try 特例，以证明"每个 InformationEvent 的 evidence_ref 都来自 owner"。
那套实现**不能可靠证明它声称的不变量**，而且会持续膨胀成一个劣质静态分析器。

现在改成三条**确定、结构化**的边界，并且明确写出各自能证明什么：

* 谁可以构造 ``InformationEvent`` —— 模块集合显式登记（新增模块必须改这里，是架构变化）；
* 谁可以签发 ref —— 所有 ``evidence_ref_from_*`` 调用必须解析到
  ``ai_research_contract``；本地同名函数 / 其它对象的同名方法 / 其它模块的同名工厂全部拒绝；
* ref 在运行期是否真的是 ref —— 由 ``InformationEvent.__post_init__`` 的类型检查保证。

刻意**不再**静态追踪局部变量（``ref = ...; InformationEvent(evidence_ref=ref)``）的来源：
那条保证由上面第 3 条（运行期类型）与第 1 条（构造模块集合）承担，而不是靠一个近似分析。
"""
from __future__ import annotations

import ast
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

#: 允许的 owner → factory 映射。**精确等值**，不是 contains / prefix / non-empty。
#: 新增一个 owner 必须同时出现：owner 自己发布核验闭集 + 这里的一行 + 契约的导出。
EXPECTED_OWNER_FACTORIES = {
    "market_data": "evidence_ref_from_market_reading",
}

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
# "这个 callable 是否解析到 ai_research_contract 导出的符号？"
# ─────────────────────────────────────────────────────────────────────────────


def _import_origins(tree: ast.Module) -> dict[str, tuple[str, str | None]]:
    """本地名 → ``(模块, 导出名)``。只记录契约模块的 import。

    ``import ai_research_contract as ARC``      → ``{"ARC": ("ai_research_contract", None)}``
    ``from ai_research_contract import X as Y`` → ``{"Y": ("ai_research_contract", "X")}``
    """
    origins: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == CONTRACT_MODULE_NAME:
                    origins[alias.asname or alias.name.split(".")[0]] = (
                        CONTRACT_MODULE_NAME, None,
                    )
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == CONTRACT_MODULE_NAME:
                for alias in node.names:
                    origins[alias.asname or alias.name] = (CONTRACT_MODULE_NAME, alias.name)
    return origins


def _call_origin(node: ast.Call, origins) -> tuple[str | None, str]:
    """``(module_origin, symbol)``。

    * ``ARC.f(...)`` 且 ``ARC`` 来自契约 → ``("ai_research_contract", "f")``
    * ``f(...)`` 且 ``f`` 是 ``from ai_research_contract import f`` → ``("ai_research_contract", "f")``
    * ``f(...)`` 且 ``f`` 是契约导出名的别名 → 归一到原名
    * ``obj.f(...)`` / ``other.f(...)`` → ``(None, "f")``（**来源不明，一律不算契约**）
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
                # ``f.attr`` 其中 f 是从契约导入的名字 —— 不是模块属性访问，来源不明
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


def _approved_factory(node: ast.Call, origins) -> bool:
    """这次调用是否**真的**是契约导出的、已登记的 owner 工厂。

    三个条件缺一不可：来源是契约模块、符号在登记表里、且契约确实导出它。
    因此 ``def evidence_ref_from_market_reading(row): ...``（本地同名）与
    ``fake.evidence_ref_from_market_reading(row)``（其它对象同名）都不算。
    """
    module, symbol = _call_origin(node, origins)
    if module != CONTRACT_MODULE_NAME:
        return False
    if symbol not in EXPECTED_OWNER_FACTORIES.values():
        return False
    return symbol in _contract_exported_factories()


def _contract_exported_factories() -> frozenset[str]:
    return frozenset(
        name for name in getattr(ARC, "__all__", ())
        if name.startswith(OWNER_FACTORY_PREFIX)
    )


def _registry_problems(owners, factories) -> list[str]:
    """owner registry 与 factory registry 的双向一致性。

    单独抽成纯函数，是为了让"两个方向都要红"能被**直接测到**（见非空性用例），
    而不是只能靠改契约源码来验。
    """
    problems = []
    missing_factories = sorted(set(owners) - set(factories))
    if missing_factories:
        problems.append(f"owner 已登记但没有 factory：{missing_factories}")
    unregistered_factories = sorted(set(factories) - set(owners))
    if unregistered_factories:
        problems.append(
            f"factory 已存在但对应 owner 未登记：{unregistered_factories}"
            "（新增 owner 必须先让该 owner 发布自己的核验闭集）"
        )
    return problems


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
        """EVIDENCE-01：owner registry 与契约导出的 factory registry 双向等值。

        两个方向都必须红：契约新增 factory 而没人登记 owner，或者登记了 owner 却没有
        factory。任何一处漂移都意味着"谁能签发证据"已经不受这条边界管辖。
        """
        self.assertEqual(
            set(EXPECTED_OWNER_FACTORIES), set(ARC.SUPPORTED_OWNER_ADAPTERS),
            "SUPPORTED_OWNER_ADAPTERS 与已批准的 owner→factory 映射不一致",
        )
        exported = _contract_exported_factories()
        self.assertEqual(
            set(EXPECTED_OWNER_FACTORIES.values()), set(exported),
            f"契约导出的 owner factory 集合发生变化：{sorted(exported)}",
        )
        # 非空性：只有真的存在 factory 时，上面两条等值断言才有内容可查。
        self.assertTrue(exported, "契约没有导出任何 owner factory")

    def test_EVIDENCE_02_research_evidence_ref_has_no_public_constructor(self):
        """EVIDENCE-02：调用方不能仅凭传字符串声明一条事实。"""
        with self.assertRaises(TypeError):
            ARC.ResearchEvidenceRef(
                source_type="market_data", source_id="forged", as_of="2026-08-27",
            )
        self.assertTrue(callable(ARC.evidence_ref_from_market_reading))

    def test_EVIDENCE_03_private_issuer_is_called_only_from_the_contract(self):
        """EVIDENCE-03：私有签发口在契约模块外零调用（别名也会被识破）。"""
        offenders = [
            name for name in _production_modules()
            if name != CONTRACT_MODULE_FILE
            and _calls(_tree(name), symbol="_issue_evidence_ref")
        ]
        self.assertEqual([], offenders, f"契约之外出现了私有签发调用：{offenders}")
        # 非空性：契约自己必须真的在调用它。
        self.assertTrue(_calls(_tree(CONTRACT_MODULE_FILE), symbol="_issue_evidence_ref"))

    def test_EVIDENCE_04_every_evidence_factory_call_comes_from_the_contract(self):
        """EVIDENCE-04：生产里每个 owner 签发命名空间调用都必须来自契约。

        这是"不得新增未登记 adapter"的可执行形式：本地同名函数、其它对象的同名方法、
        其它模块的同名工厂，全部因为**来源不是契约**而被拒绝。
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
            "evidence_ref_from_* 只能解析到 ai_research_contract 已导出的工厂。",
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


class FactoryOriginNonVacuityTests(unittest.TestCase):
    """factory origin 解析必须真的能区分"来自契约"与"只是名字像"。

    这六种写法覆盖 review 提出的绕过方式：本地同名函数、其它对象同名方法、
    其它模块同名工厂；以及三种合法写法：模块别名、直接 import、直接 import 的别名。
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

    def test_CASE_4_local_function_with_the_same_name_is_rejected(self):
        self.assertFalse(self._approved(
            "def evidence_ref_from_market_reading(row):\n"
            "    return row\n"
            "evidence_ref_from_market_reading(row)\n"
        ))

    def test_CASE_5_same_named_method_on_another_object_is_rejected(self):
        self.assertFalse(self._approved("fake.evidence_ref_from_market_reading(row)\n"))

    def test_CASE_6_same_named_factory_in_another_module_is_rejected(self):
        self.assertFalse(self._approved(
            "import fake_contract\n"
            "fake_contract.evidence_ref_from_market_reading(row)\n"
        ))

    def test_CASE_7_extra_contract_factory_without_registered_owner_fails(self):
        """契约新增 factory，但 owner registry 没跟上 → 必须报问题。"""
        problems = _registry_problems(["market_data"], ["market_data", "paper"])
        self.assertTrue(problems, "多余 factory 未被发现")
        self.assertTrue(any("未登记" in item for item in problems))

    def test_CASE_8_registered_owner_without_factory_fails(self):
        """owner registry 新增 execution，但没有对应 factory → 必须报问题。"""
        problems = _registry_problems(["market_data", "execution"], ["market_data"])
        self.assertTrue(problems, "缺失 factory 未被发现")
        self.assertTrue(any("没有 factory" in item for item in problems))
        # 双向都干净时不得假报。
        self.assertEqual([], _registry_problems(["market_data"], ["market_data"]))

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
