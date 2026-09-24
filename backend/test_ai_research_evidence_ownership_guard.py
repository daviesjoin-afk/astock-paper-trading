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
    EVIDENCE-04  生产里每个 InformationEvent 的 evidence_ref 都必须来自**契约导出的**
                 owner 工厂，且该绑定必须真的能到达这次使用
    EVIDENCE-05  生产里**没有**任何地方直接构造 ResearchEvidenceRef
    非空性        上面的扫描器必须真的能失败，否则它们只是装饰

────────────── 这个守卫刻意做到多强（以及不做什么）──────────────

三条"看起来能过、其实不该过"的写法必须被判违规，因此扫描器不是字符串前缀匹配：

1. **别名不能绕过。** ``from ai_research_contract import InformationEvent as Event``
   之后 ``Event(...)`` 仍然被认出是受保护符号（import 别名解析表）。
2. **靠拼写不算 owner 工厂。** ``evidence_ref_from_paper_dict(row)`` 即使前缀正确，
   只要它不是 ``ai_research_contract`` 真正导出的工厂，就判违规 —— 登记表
   ``SUPPORTED_OWNER_ADAPTERS`` 因此不会被"名字长得像"绕过去。
3. **看的是真的到达那次使用的赋值。** 比使用点更晚的赋值、被不安全赋值覆盖过的名字、
   嵌套函数里的同名绑定，都不算数；在条件块里绑定而使用在其外面也不算数。

它**不**做完整控制流分析（没有 CFG、没有跨模块追踪），因此它的强度是"名称解析 +
同作用域内按行序取最后一个**能支配**使用点的绑定"。这是刻意的取舍：更强需要真正的
数据流分析，而这里的目的是拦住 dict → typed event 这类写法，不是证明整个程序安全。

刻意的边界：本文件**不**新增 owner、**不**新增 adapter、**不**改变任何生产行为。
它只是把矩阵文档 §六 的禁令变成 CI 会拦下的东西。
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
CONTRACT_MODULE_NAME = "ai_research_contract"

#: 受保护的符号：构造研究事件、构造证据引用、以及私有签发口。
PROTECTED_SYMBOLS = frozenset({"InformationEvent", "ResearchEvidenceRef", "_issue_evidence_ref"})

#: owner 工厂的命名空间前缀。前缀**本身不构成授权**（见 :func:`_is_owner_factory_call`）。
OWNER_FACTORY_PREFIX = "evidence_ref_from_"

#: 目前真的有签发能力的 owner。新增一项必须同时是一次有意识的决定：
#: 该 owner 必须先发布自己的核验闭集（matrix 文档 §五 的 1–4 步）。
EXPECTED_OWNER_ADAPTERS = frozenset({"market_data"})

#: 目前生产里构造 ``InformationEvent`` 的模块集合。等值断言：多一个模块就意味着多出
#: 一条"事实 → 研究事件"的路径，而它必须被审阅。
EXPECTED_EVENT_CONSTRUCTORS = frozenset({"deepseek_advisor.py"})

#: 只在条件 / 循环容器处切分"支配块"。``try`` / ``with`` 刻意透明：
#: 把绑定写在 ``try`` 里、在 ``try`` 之后使用是正常写法，而"except 分支里换成不安全的值"
#: 仍然会被"最后一个能支配使用点的绑定优先"抓住。
_BLOCK_CONTAINERS = (ast.If, ast.For, ast.AsyncFor, ast.While)
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


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
# 名称解析：import 别名 → 受保护符号 / owner 工厂
# ─────────────────────────────────────────────────────────────────────────────


def _import_aliases(tree: ast.Module) -> dict[str, tuple[str, str | None]]:
    """本地名 → ``(模块, 属性或 None)``。只关心契约模块的别名。

    ``import ai_research_contract as ARC``      → ``{"ARC": ("ai_research_contract", None)}``
    ``from ai_research_contract import X as Y`` → ``{"Y": ("ai_research_contract", "X")}``
    """
    aliases: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == CONTRACT_MODULE_NAME:
                    aliases[alias.asname or alias.name.split(".")[0]] = (
                        CONTRACT_MODULE_NAME, None,
                    )
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == CONTRACT_MODULE_NAME:
                for alias in node.names:
                    aliases[alias.asname or alias.name] = (CONTRACT_MODULE_NAME, alias.name)
    return aliases


def _resolved_symbol(node, aliases) -> str:
    """调用的**规范**名字：别名会被还原成契约里的原名。

    还原不出来时退回末端名字 —— 因此 ``InformationEvent`` 这种裸名**仍然**受保护，
    别名不能凭"名字对不上"溜过去。
    """
    func = node.func
    if isinstance(func, ast.Name):
        canonical = aliases.get(func.id)
        if canonical is not None and canonical[1]:
            return canonical[1]
        return func.id
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name):
            canonical = aliases.get(base.id)
            if canonical is not None and canonical[1] is None:
                # 契约模块属性访问：``ARC.InformationEvent``
                return func.attr
        return func.attr
    return ""


def _calls_to(tree: ast.Module, symbol: str, aliases=None) -> int:
    aliases = _import_aliases(tree) if aliases is None else aliases
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _resolved_symbol(node, aliases) == symbol
    )


def _contract_public_factories() -> frozenset[str]:
    """契约**自己导出**的 owner 工厂名。

    这是"前缀不构成授权"的依据：只有契约导出的工厂才算签发口，因此一个本地的
    ``evidence_ref_from_paper_dict`` 无法靠名字混进来。
    """
    return frozenset(
        name for name in getattr(ARC, "__all__", ())
        if name.startswith(OWNER_FACTORY_PREFIX)
    )


def _is_owner_factory_call(node, aliases) -> bool:
    """这次调用是不是**契约导出的** owner 工厂 —— 不是"名字以某前缀开头"。"""
    if not isinstance(node, ast.Call):
        return False
    symbol = _resolved_symbol(node, aliases)
    if not symbol.startswith(OWNER_FACTORY_PREFIX):
        return False
    return symbol in _contract_public_factories()


# ─────────────────────────────────────────────────────────────────────────────
# 同作用域内的绑定解析（只做"能支配使用点的最后一个绑定"这一层）
# ─────────────────────────────────────────────────────────────────────────────


def _same_scope_nodes(func):
    """``func`` 自身作用域内的节点 —— 不进入嵌套函数 / 类 / lambda。"""
    out = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _NESTED_SCOPES):
                continue
            out.append(child)
            walk(child)

    walk(func)
    return out


def _scope_chains(func) -> dict[int, tuple[int, ...]]:
    """节点 → 从函数体到它的**条件 / 循环**容器链（``try`` / ``with`` 透明）。"""
    chains: dict[int, tuple[int, ...]] = {}

    def walk(node, chain):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _NESTED_SCOPES):
                continue
            next_chain = chain + (id(child),) if isinstance(child, _BLOCK_CONTAINERS) else chain
            chains[id(child)] = next_chain
            walk(child, next_chain)

    walk(func, ())
    return chains


def _dominates(binding_chain, use_chain) -> bool:
    """绑定所在的条件块必须是使用点所在条件块的前缀（结构支配的近似）。"""
    return use_chain[:len(binding_chain)] == binding_chain


def _owner_bindings(func, aliases) -> dict[str, list[tuple[int, tuple[int, ...], bool]]]:
    """``name → [(行号, 条件块链, 是否 owner 工厂结果)]``（同作用域）。"""
    bindings: dict[str, list[tuple[int, tuple[int, ...], bool]]] = {}
    chains = _scope_chains(func)
    for node in _same_scope_nodes(func):
        if not isinstance(node, ast.Assign):
            continue
        is_owner = _is_owner_factory_call(node.value, aliases)
        for target in node.targets:
            if isinstance(target, ast.Name):
                bindings.setdefault(target.id, []).append(
                    (node.lineno, chains.get(id(node), ()), is_owner),
                )
    return bindings


def _name_is_owner_issued(func, name: str, use_node, aliases) -> bool:
    """``name`` 在这次使用点上是否**真的**解析到 owner 工厂的结果。

    取"能支配使用点、且行号在使用点之前"的**最后一个**绑定：
    * 更晚的赋值不算（绑定还没发生）；
    * 被不安全赋值覆盖过的名字不算（最后一个绑定的来源不是 owner 工厂）；
    * 条件块里绑定、条件块外使用不算（绑定可能没执行）；
    * 嵌套函数里的同名绑定不算（不同作用域）。
    """
    bindings = _owner_bindings(func, aliases)
    chains = _scope_chains(func)
    use_chain = chains.get(id(use_node), ())
    candidates = [
        (lineno, is_owner)
        for lineno, chain, is_owner in bindings.get(name, [])
        if lineno < use_node.lineno and _dominates(chain, use_chain)
    ]
    if not candidates:
        return False
    return max(candidates, key=lambda item: item[0])[1]


# ─────────────────────────────────────────────────────────────────────────────
# InformationEvent 构造点
# ─────────────────────────────────────────────────────────────────────────────


def _keyword_value(call: ast.Call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _event_construction_sites(tree: ast.Module):
    """每个 ``InformationEvent(...)`` → ``(所在函数或 None, 调用节点, evidence_ref 实参)``。

    只按关键字取 ``evidence_ref``；位置参数形式返回 ``None`` 并由断言判为违规 ——
    "漏掉位置参数"正好是给伪造留后门，静默忽略等于护栏空转。
    """
    aliases = _import_aliases(tree)
    sites = []

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child)
                continue
            if isinstance(child, ast.Call) and _resolved_symbol(child, aliases) == "InformationEvent":
                sites.append((func, child, _keyword_value(child, "evidence_ref")))
            walk(child, func)

    walk(tree, None)
    return sites


def _is_owner_issued_ref(func, call_node, value, aliases) -> bool:
    if _is_owner_factory_call(value, aliases):
        return True
    if isinstance(value, ast.Name) and func is not None:
        return _name_is_owner_issued(func, value.id, call_node, aliases)
    return False


def _fake_event_construction_modules() -> list[str]:
    """用非 owner 签发方式构造 ``InformationEvent`` 的模块。"""
    offenders = []
    for name in _production_modules():
        tree = _tree(name)
        aliases = _import_aliases(tree)
        for func, call_node, value in _event_construction_sites(tree):
            if value is None or not _is_owner_issued_ref(func, call_node, value, aliases):
                offenders.append(name)
    return offenders


class OwnerAdapterRegistryTests(unittest.TestCase):
    def test_EVIDENCE_01_owner_adapter_set_is_an_equality_closed_registry(self):
        """EVIDENCE-01：可签发 owner 是显式登记表，不是"谁都能接一条"。"""
        self.assertEqual(
            EXPECTED_OWNER_ADAPTERS, set(ARC.SUPPORTED_OWNER_ADAPTERS),
            "可签发 owner 集合发生变化。新增 owner 必须先让该 owner 发布自己的核验闭集，"
            "再改这张登记表 —— 这是有意识的决定，不是顺手加一项。",
        )
        self.assertTrue(
            EXPECTED_OWNER_ADAPTERS <= set(ARC.EVIDENCE_SOURCE_TYPES),
            "登记的 owner 不在 EVIDENCE_SOURCE_TYPES 里",
        )
        # 登记表必须与"契约真的导出多少工厂"对得上：两者脱节说明有一方在说空话。
        self.assertTrue(_contract_public_factories(), "契约没有导出任何 owner 工厂")
        self.assertIn("evidence_ref_from_market_reading", _contract_public_factories())

    def test_EVIDENCE_02_research_evidence_ref_still_has_no_public_constructor(self):
        """EVIDENCE-02：调用方不能仅凭传字符串就声明一条事实。"""
        with self.assertRaises(TypeError):
            ARC.ResearchEvidenceRef(
                source_type="market_data", source_id="forged", as_of="2026-08-27",
            )
        self.assertTrue(callable(ARC.evidence_ref_from_market_reading))

    def test_EVIDENCE_03_private_issuer_is_called_only_from_the_contract(self):
        """EVIDENCE-03：私有签发口只在契约模块里被调用（别名同样会被识破）。"""
        offenders = [
            name for name in _production_modules()
            if name != CONTRACT_MODULE
            and _calls_to(_tree(name), "_issue_evidence_ref")
        ]
        self.assertEqual([], offenders, f"契约之外出现了私有签发调用：{offenders}")
        self.assertGreater(_calls_to(_tree(CONTRACT_MODULE), "_issue_evidence_ref"), 0)


class TypedEventConstructionTests(unittest.TestCase):
    def test_EVIDENCE_04_every_information_event_is_built_from_an_owner_issued_ref(self):
        """EVIDENCE-04：typed event 只能由**契约导出的** owner 工厂签发的 ref 构造。

        这是"不得写 legacy dict → typed event 包装器"的可执行形式。
        """
        offenders = _fake_event_construction_modules()
        self.assertEqual(
            [], offenders,
            f"这些模块用非 owner 签发的方式构造了 InformationEvent：{offenders}。"
            "evidence_ref 必须来自契约导出的 evidence_ref_from_* 工厂，"
            "dict / 裸值 / 手拼对象 / 本地同名包装一律视为伪造 provenance。",
        )

    def test_EVIDENCE_05_no_production_module_constructs_a_ref_directly(self):
        """EVIDENCE-05：生产里没有任何地方直接构造 ``ResearchEvidenceRef``。"""
        offenders = [
            name for name in _production_modules()
            if _calls_to(_tree(name), "ResearchEvidenceRef")
        ]
        self.assertEqual([], offenders, f"直接构造 ResearchEvidenceRef：{offenders}")

    def test_EVIDENCE_05b_event_constructor_set_is_explicit(self):
        """EVIDENCE-05b：构造 typed event 的模块集合是显式登记的等值集合。"""
        constructors = {
            name for name in _production_modules()
            if _event_construction_sites(_tree(name))
        }
        self.assertEqual(
            EXPECTED_EVENT_CONSTRUCTORS, constructors,
            f"构造 InformationEvent 的模块集合发生变化：{sorted(constructors)}",
        )


class GuardNonVacuityTests(unittest.TestCase):
    """扫描器必须真的能失败 —— 否则 EVIDENCE-01..05 只是装饰。

    这三组正是"看起来能过、其实不该过"的写法：靠拼写的工厂、import 别名、以及
    不等同于使用点的绑定。
    """

    def _sites(self, text: str):
        tree = ast.parse(text)
        return tree, _event_construction_sites(tree)

    def _accepted(self, text: str) -> bool:
        tree, sites = self._sites(text)
        aliases = _import_aliases(tree)
        func, call_node, value = sites[0]
        return _is_owner_issued_ref(func, call_node, value, aliases)

    def test_fake_prefixed_factory_is_rejected(self):
        """靠拼写混进来的"工厂"不算 owner 签发。"""
        self.assertFalse(self._accepted(
            "import ai_research_contract as ARC\n"
            "def f(row):\n"
            "    ref = evidence_ref_from_paper_dict(row)\n"
            "    return ARC.InformationEvent(as_of='2026-08-27', source='s', evidence_ref=ref)\n"
        ), "本地同名包装 evidence_ref_from_paper_dict 被当成 owner 签发")
        self.assertFalse(self._accepted(
            "import ai_research_contract as ARC\n"
            "def f(row):\n"
            "    return ARC.InformationEvent(as_of='2026-08-27', source='s', "
            "evidence_ref=evidence_ref_from_paper_dict(row))\n"
        ), "内联的本地包装未被判为违规")

    def test_import_alias_cannot_hide_protected_symbols(self):
        """``... import InformationEvent as Event`` 之后 ``Event(...)`` 仍然被看见。"""
        tree, sites = self._sites(
            "from ai_research_contract import InformationEvent as Event\n"
            "def f(row):\n"
            "    return Event(as_of='2026-08-27', source='s', evidence_ref=row)\n"
        )
        self.assertEqual(1, len(sites), "别名构造点被完全漏掉了")
        aliases = _import_aliases(tree)
        func, call_node, value = sites[0]
        self.assertFalse(_is_owner_issued_ref(func, call_node, value, aliases))

        # 模块别名下的私有签发口 / ref 构造器同样要被看见。
        aliased = ast.parse(
            "from ai_research_contract import _issue_evidence_ref as issue\n"
            "from ai_research_contract import ResearchEvidenceRef as Ref\n"
            "issue()\nRef(a=1)\n"
        )
        self.assertEqual(1, _calls_to(aliased, "_issue_evidence_ref"))
        self.assertEqual(1, _calls_to(aliased, "ResearchEvidenceRef"))

    def test_only_the_binding_that_reaches_the_use_counts(self):
        """绑定必须真的能到达那次使用。"""
        accepted = (
            "import ai_research_contract as ARC\n"
            "def f(reading):\n"
            "    ref = ARC.evidence_ref_from_market_reading(reading)\n"
            "    return ARC.InformationEvent(as_of='2026-08-27', source='s', "
            "evidence_ref=ref)\n"
        )
        self.assertTrue(self._accepted(accepted), "合法的 owner 绑定被误判为违规")

        # ① 绑定在使用点之后 → 不算
        self.assertFalse(self._accepted(
            "import ai_research_contract as ARC\n"
            "def f(reading):\n"
            "    event = ARC.InformationEvent(as_of='2026-08-27', source='s', "
            "evidence_ref=ref)\n"
            "    ref = ARC.evidence_ref_from_market_reading(reading)\n"
            "    return event\n"
        ), "使用点之后的绑定被当成了有效来源")

        # ② 被不安全赋值覆盖 → 不算
        self.assertFalse(self._accepted(
            "import ai_research_contract as ARC\n"
            "def f(reading, row):\n"
            "    ref = ARC.evidence_ref_from_market_reading(reading)\n"
            "    ref = row\n"
            "    return ARC.InformationEvent(as_of='2026-08-27', source='s', "
            "evidence_ref=ref)\n"
        ), "被不安全赋值覆盖后的名字仍被当成 owner 签发")

        # ③ 嵌套函数里的同名绑定 → 不算
        self.assertFalse(self._accepted(
            "import ai_research_contract as ARC\n"
            "def f(reading):\n"
            "    def inner():\n"
            "        ref = ARC.evidence_ref_from_market_reading(reading)\n"
            "    return ARC.InformationEvent(as_of='2026-08-27', source='s', "
            "evidence_ref=ref)\n"
        ), "嵌套函数内的绑定逃过了作用域检查")

        # ④ 条件块里绑定、条件块外使用 → 不算
        self.assertFalse(self._accepted(
            "import ai_research_contract as ARC\n"
            "def f(reading, flag, row):\n"
            "    if flag:\n"
            "        ref = ARC.evidence_ref_from_market_reading(reading)\n"
            "    else:\n"
            "        ref = row\n"
            "    return ARC.InformationEvent(as_of='2026-08-27', source='s', "
            "evidence_ref=ref)\n"
        ), "分支绑定被当成能支配使用点")

        # ⑤ 同一个条件块内绑定并使用 → 算（否则这条规则会误杀正常写法）
        self.assertTrue(self._accepted(
            "import ai_research_contract as ARC\n"
            "def f(flag, reading):\n"
            "    if flag:\n"
            "        ref = ARC.evidence_ref_from_market_reading(reading)\n"
            "        return ARC.InformationEvent(as_of='2026-08-27', source='s', "
            "evidence_ref=ref)\n"
            "    return None\n"
        ), "同一条件块内的合法绑定被误判为违规")

        # ⑥ try 里的绑定 + try 之后使用 → 算（正常写法，try/with 刻意透明）
        self.assertTrue(self._accepted(
            "import ai_research_contract as ARC\n"
            "def f(reading):\n"
            "    try:\n"
            "        ref = ARC.evidence_ref_from_market_reading(reading)\n"
            "    except Exception:\n"
            "        return ()\n"
            "    return ARC.InformationEvent(as_of='2026-08-27', source='s', "
            "evidence_ref=ref)\n"
        ), "try 内绑定 + try 后使用被误判为违规")

    def test_miscellaneous_scanners(self):
        # 位置参数形式必须被看见（返回 None → 判违规），不能静默忽略
        _, sites = self._sites(
            "import ai_research_contract as ARC\n"
            "ARC.InformationEvent('2026-08-27', 's', row)\n"
        )
        self.assertIsNone(sites[0][2], "位置参数形式被静默忽略了")
        # 真实生产写法（模块别名 + 直接调用）必须被接受
        self.assertTrue(self._accepted(
            "import ai_research_contract as ARC\n"
            "def f(reading):\n"
            "    return ARC.InformationEvent(as_of='2026-08-27', source='s', "
            "evidence_ref=ARC.evidence_ref_from_market_reading(reading))\n"
        ), "模块别名下的直接 owner 调用被误判为违规")


if __name__ == "__main__":
    unittest.main()
