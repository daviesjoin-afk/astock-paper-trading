# -*- coding: utf-8 -*-
"""R27-B2C —— "只有 owner 能签发证据" 的可执行不变量。

存在理由是 R27-B2C 的 audit 结论（见 ``docs/R27_B2C_EVIDENCE_OWNER_MATRIX.md``）：

    剩余 research runtime 的事实**全部** NOT MIGRATABLE，因为它们缺的不是接线，
    而是**owner 签发的核验维度**。今天只有 ``market_data`` 有可签发的 adapter。

于是本轮**唯一的**风险是"为了迁移速度"绕过这条约束，用 legacy dict 手拼一条 typed
evidence：

    paper_dict_to_information_event(paper_row)          ← 禁止
        evidence_ref=手填 verification / source_id / as_of

那条路会伪造 provenance，而且不会让任何现存测试变红（R27-B1/B2A/B2B 的回归都从 typed
输入开始）。所以这里把"只有 owner 能签发证据"写成会失败的断言：

    EVIDENCE-01  SUPPORTED_OWNER_ADAPTERS 是**等值闭集**：新增 owner 必须是一次有意识的决定
    EVIDENCE-02  ResearchEvidenceRef 仍然**没有公开构造器**（构造即抛 TypeError）
    EVIDENCE-03  私有签发口 _issue_evidence_ref 只在契约模块里被调用
    EVIDENCE-04  生产里每个 InformationEvent 的 evidence_ref 都必须来自 owner 签发
                 （同一函数内直接调用，或绑定到该函数内由 owner 工厂产生的名字）；
                 dict / 裸值 / 手拼对象一律判为伪造
    EVIDENCE-05  生产里**没有**任何地方直接构造 ResearchEvidenceRef
    非空性        上面的扫描器必须真的能失败，否则它们只是装饰

刻意的边界：本文件**不**新增 owner、**不**新增 adapter、**不**改变任何生产行为。
它只是让矩阵文档 §六 的禁令变成 CI 会拦下的东西。
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

CONTRACT_MODULE = "ai_research_contract.py"

#: 唯一合法的 evidence 签发命名空间。owner adapter 只能**新增**在这个前缀下；
#: "研究层自己包装一下"不产生新的 owner，因此也不允许。
OWNER_FACTORY_PREFIX = "evidence_ref_from_"

#: 目前真的有签发能力的 owner。新增一项必须同时是一次有意识的决定：
#: 该 owner 必须先发布自己的核验闭集（matrix 文档 §五 的 1–4 步）。
EXPECTED_OWNER_ADAPTERS = frozenset({"market_data"})

#: 目前生产里构造 ``InformationEvent`` 的模块集合。等值断言：多一个模块就意味着多出
#: 一条"事实 → 研究事件"的路径，而它必须被审阅。
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


def _call_name(node: ast.Call) -> str:
    """调用表达式的**末端**名字（``a.b.Call()`` → ``Call``，``f()`` → ``f``）。"""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _calls_to(tree: ast.Module, name: str) -> int:
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == name
    )


def _is_owner_factory_call(value) -> bool:
    """这个表达式是不是一次 owner 签发命名空间下的调用。"""
    return isinstance(value, ast.Call) and _call_name(value).startswith(OWNER_FACTORY_PREFIX)


def _keyword_value(call: ast.Call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _event_construction_sites(tree: ast.Module):
    """每个 ``InformationEvent(...)`` → ``(所在函数或 None, evidence_ref 实参)``。

    只按关键字取 ``evidence_ref``；位置参数形式返回 ``None`` 并由断言判为违规 ——
    "漏掉位置参数"正好是给伪造留后门，静默忽略等于护栏空转。
    """
    sites = []

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child)
                continue
            if isinstance(child, ast.Call) and _call_name(child) == "InformationEvent":
                sites.append((func, _keyword_value(child, "evidence_ref")))
            walk(child, func)

    walk(tree, None)
    return sites


def _owner_bound_names(func) -> set[str]:
    """``func`` 内绑定到 owner 签发调用结果的局部名字。

    允许"先 ``ref = evidence_ref_from_*`` 再 ``evidence_ref=ref``"这种正常写法，
    同时仍然拒绝任何**不是**由 owner 工厂产生的值 —— 包括从行 / dict / 字符串来的。
    """
    if func is None:
        return set()
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and _is_owner_factory_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _is_owner_issued_ref(func, value) -> bool:
    if _is_owner_factory_call(value):
        return True
    return isinstance(value, ast.Name) and value.id in _owner_bound_names(func)


def _fake_event_construction_modules() -> list[str]:
    """用非 owner 签发方式构造 ``InformationEvent`` 的模块。"""
    offenders = []
    for name in _production_modules():
        for func, value in _event_construction_sites(_tree(name)):
            if value is None or not _is_owner_issued_ref(func, value):
                offenders.append(name)
    return offenders


class OwnerAdapterRegistryTests(unittest.TestCase):
    def test_EVIDENCE_01_owner_adapter_set_is_an_equality_closed_registry(self):
        """EVIDENCE-01：可签发 owner 是显式登记表，不是"谁都能接一条"。

        这一条把 R27-B2C 的 audit 结论固化下来：今天**只有** market_data 能签发。
        新增 owner 必须同时提交它的核验闭集，而不是悄悄往集合里塞一个字符串。
        """
        self.assertEqual(
            EXPECTED_OWNER_ADAPTERS, set(ARC.SUPPORTED_OWNER_ADAPTERS),
            "可签发 owner 集合发生变化。新增 owner 必须先让该 owner 发布自己的核验闭集，"
            "再改这张登记表 —— 这是有意识的决定，不是顺手加一项。",
        )
        # 声明过的 source type 可以多于能签发的 owner（未来来源），但绝不能相反：
        # 能签发却不是合法 source type 意味着身份词表已经分叉。
        self.assertTrue(
            EXPECTED_OWNER_ADAPTERS <= set(ARC.EVIDENCE_SOURCE_TYPES),
            "登记的 owner 不在 EVIDENCE_SOURCE_TYPES 里",
        )

    def test_EVIDENCE_02_research_evidence_ref_still_has_no_public_constructor(self):
        """EVIDENCE-02：调用方不能仅凭传字符串就声明一条事实。"""
        with self.assertRaises(TypeError):
            ARC.ResearchEvidenceRef(
                source_type="market_data", source_id="forged", as_of="2026-08-27",
            )
        # 非空性：正常路径仍然可用（否则"构造器抛错"可能只是模块坏了）。
        self.assertTrue(callable(ARC.evidence_ref_from_market_reading))

    def test_EVIDENCE_03_private_issuer_is_called_only_from_the_contract(self):
        """EVIDENCE-03：私有签发口只在契约模块里被调用，别的模块不得自己发 ref。"""
        offenders = [
            name for name in _production_modules()
            if name != CONTRACT_MODULE and _calls_to(_tree(name), "_issue_evidence_ref")
        ]
        self.assertEqual([], offenders, f"契约之外出现了私有签发调用：{offenders}")
        # 非空性：契约自己**必须**真的在调用它，否则这条断言在证明一个空集。
        self.assertGreater(_calls_to(_tree(CONTRACT_MODULE), "_issue_evidence_ref"), 0)


class TypedEventConstructionTests(unittest.TestCase):
    def test_EVIDENCE_04_every_information_event_is_built_from_an_owner_issued_ref(self):
        """EVIDENCE-04：typed event 只能由 owner 签发的 ref 构造。

        这是"不得写 legacy dict → typed event 包装器"的可执行形式：dict、裸字符串、
        手拼对象都过不了 owner 签发检查，因此 ``paper_dict_to_information_event``
        这类写法一出现就会红。
        """
        offenders = _fake_event_construction_modules()
        self.assertEqual(
            [], offenders,
            f"这些模块用非 owner 签发的方式构造了 InformationEvent：{offenders}。"
            "evidence_ref 必须来自 evidence_ref_from_*（owner 签发），"
            "dict / 裸值 / 手拼对象一律视为伪造 provenance。",
        )

    def test_EVIDENCE_05_no_production_module_constructs_a_ref_directly(self):
        """EVIDENCE-05：生产里没有任何地方直接构造 ``ResearchEvidenceRef``。"""
        offenders = [
            name for name in _production_modules()
            if _calls_to(_tree(name), "ResearchEvidenceRef")
        ]
        self.assertEqual([], offenders, f"直接构造 ResearchEvidenceRef：{offenders}")

    def test_EVIDENCE_05b_event_constructor_set_is_explicit(self):
        """EVIDENCE-05b：构造 typed event 的模块集合是显式登记的等值集合。

        多出一个模块 = 多一条"事实 → 研究事件"的路径。可以加，但必须是一次决定。
        """
        constructors = {
            name for name in _production_modules()
            if _event_construction_sites(_tree(name))
        }
        self.assertEqual(
            EXPECTED_EVENT_CONSTRUCTORS, constructors,
            f"构造 InformationEvent 的模块集合发生变化：{sorted(constructors)}",
        )


class GuardNonVacuityTests(unittest.TestCase):
    """扫描器必须真的能失败 —— 否则 EVIDENCE-01..05 只是装饰。"""

    def _sites(self, text: str):
        return _event_construction_sites(ast.parse(text))

    def test_scanners_detect_the_forbidden_shapes(self):
        # dict 直接当 evidence_ref
        dict_arg = self._sites(
            "row = {}\n"
            "InformationEvent(as_of='2026-08-27', source='legacy', evidence_ref=row)\n"
        )
        self.assertEqual(1, len(dict_arg), "扫描器看不到 InformationEvent 调用")
        func, value = dict_arg[0]
        self.assertFalse(_is_owner_issued_ref(func, value), "dict 冒充 evidence_ref 未被判为违规")

        # 手拼 ref 工厂
        handrolled = self._sites(
            "InformationEvent(as_of='2026-08-27', source='s', "
            "evidence_ref=_hand_made_ref(row))\n"
        )
        self.assertFalse(_is_owner_issued_ref(*handrolled[0]), "手拼 ref 未被判为违规")

        # 位置参数形式必须被看见（返回 None → 判违规），不能静默忽略
        positional = self._sites("InformationEvent('2026-08-27', 's', row)\n")
        self.assertIsNone(positional[0][1], "位置参数形式被静默忽略了")

        # 局部变量只有绑定到 owner 工厂时才算合法
        bound = self._sites(
            "def f(reading):\n"
            "    ref = evidence_ref_from_market_reading(reading)\n"
            "    return InformationEvent(as_of='2026-08-27', source='s', evidence_ref=ref)\n"
        )
        self.assertTrue(_is_owner_issued_ref(*bound[0]), "合法的 owner 绑定被误判为违规")

        # 同一个形状但绑定来源是行 / dict → 必须判违规
        unbound = self._sites(
            "def f(row):\n"
            "    ref = row\n"
            "    return InformationEvent(as_of='2026-08-27', source='s', evidence_ref=ref)\n"
        )
        self.assertFalse(_is_owner_issued_ref(*unbound[0]), "行绑定冒充 owner 签发未被判为违规")

        self.assertEqual(1, _calls_to(ast.parse("x._issue_evidence_ref()\n"), "_issue_evidence_ref"))
        self.assertEqual(1, _calls_to(ast.parse("ResearchEvidenceRef(a=1)\n"), "ResearchEvidenceRef"))


if __name__ == "__main__":
    unittest.main()
