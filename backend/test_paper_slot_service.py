# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
import unittest
from unittest import mock

try:
    import paper_slot_service as PSS
except ImportError:
    from . import paper_slot_service as PSS


class _Conn:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def __enter__(self):
        self.events.append(f"db:{self.name}:enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.events.append(f"db:{self.name}:exit")
        return False


class SlotContractTests(unittest.TestCase):
    def test_supported_slots_are_exactly_the_legacy_set(self):
        self.assertEqual(PSS.SUPPORTED_SLOTS, frozenset({
            "auction", "open", "risk", "close", "weekly-review", "intraday",
        }))

    def test_invalid_slot_keeps_legacy_error(self):
        with self.assertRaisesRegex(
            ValueError,
            "slot 必须是 auction、open、risk、close、weekly-review 或 intraday",
        ):
            PSS.validate_slot("unknown")


class PreflightTests(unittest.TestCase):
    def _factory(self, events):
        counter = {"n": 0}
        def factory():
            counter["n"] += 1
            return _Conn(str(counter["n"]), events)
        return factory

    def test_lifecycle_cleanup_precedes_dispatch(self):
        events = []
        day = dt.date(2026, 9, 11)
        with mock.patch.object(PSS.ELC, "expire_stale_signals", side_effect=lambda conn, asof_day: events.append("signals")), \
             mock.patch.object(PSS.ELC, "expire_stale_orders", side_effect=lambda conn: events.append("orders")), \
             mock.patch.object(PSS.EPD, "run_execution_dispatch", side_effect=lambda conn: events.append("dispatch")):
            result = PSS.run_preflight(
                db_factory=self._factory(events),
                audit=lambda *args: None,
                resolve_asof_day=lambda: day,
            )
        self.assertEqual(result["lifecycle"], "ok")
        self.assertEqual(result["dispatch"], "ok")
        self.assertEqual(result["asof_day"], "2026-09-11")
        self.assertLess(events.index("signals"), events.index("orders"))
        self.assertLess(events.index("orders"), events.index("dispatch"))

    def test_lifecycle_failure_is_audited_but_dispatch_still_runs(self):
        events = []
        audits = []
        with mock.patch.object(PSS.ELC, "expire_stale_signals", side_effect=RuntimeError("boom")), \
             mock.patch.object(PSS.EPD, "run_execution_dispatch", side_effect=lambda conn: events.append("dispatch")):
            result = PSS.run_preflight(
                db_factory=self._factory(events),
                audit=lambda conn, actor, event, detail: audits.append((actor, event, detail)),
                resolve_asof_day=lambda: dt.date(2026, 9, 11),
            )
        self.assertEqual(result["lifecycle"], "error")
        self.assertEqual(result["dispatch"], "ok")
        self.assertIn("dispatch", events)
        self.assertEqual(audits[0][1], "entry_lifecycle_error")
        self.assertIn("RuntimeError: boom", audits[0][2])

    def test_date_resolution_failure_is_audited_and_dispatch_still_runs(self):
        events = []
        audits = []
        resolver = mock.Mock(side_effect=ValueError("bad date"))
        with mock.patch.object(PSS.ELC, "expire_stale_signals") as signals, \
             mock.patch.object(PSS.EPD, "run_execution_dispatch", side_effect=lambda conn: events.append("dispatch")):
            result = PSS.run_preflight(
                db_factory=self._factory(events),
                audit=lambda conn, actor, event, detail: audits.append((event, detail)),
                resolve_asof_day=resolver,
            )
        self.assertEqual(result["lifecycle"], "error")
        self.assertIn("dispatch", events)
        signals.assert_not_called()
        self.assertEqual(audits[0][0], "entry_lifecycle_error")
        self.assertIn("ValueError: bad date", audits[0][1])

    def test_dispatch_failure_is_audited_and_not_raised(self):
        events = []
        audits = []
        with mock.patch.object(PSS.ELC, "expire_stale_signals", return_value=None), \
             mock.patch.object(PSS.ELC, "expire_stale_orders", return_value=None), \
             mock.patch.object(PSS.EPD, "run_execution_dispatch", side_effect=ValueError("bad dispatch")):
            result = PSS.run_preflight(
                db_factory=self._factory(events),
                audit=lambda conn, actor, event, detail: audits.append((event, detail)),
                resolve_asof_day=lambda: dt.date(2026, 9, 11),
            )
        self.assertEqual(result["dispatch"], "error")
        self.assertEqual(audits[0][0], "execution_dispatch_error")

    def test_audit_failure_is_swallowed(self):
        events = []
        with mock.patch.object(PSS.ELC, "expire_stale_signals", side_effect=RuntimeError("boom")), \
             mock.patch.object(PSS.EPD, "run_execution_dispatch", return_value=None):
            result = PSS.run_preflight(
                db_factory=self._factory(events),
                audit=mock.Mock(side_effect=RuntimeError("audit unavailable")),
                resolve_asof_day=lambda: dt.date(2026, 9, 11),
            )
        self.assertEqual(result["lifecycle"], "error")
        self.assertEqual(result["dispatch"], "ok")


_PAPER_TRADING_SOURCE = Path(__file__).with_name("paper_trading.py")


def _dotted_name(node):
    """把 ``PSS.run_preflight`` 这类调用目标还原成点号字符串；不是名字则返回 None。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _iter_calls(node):
    """遍历 node 内的调用，但**不进入**嵌套函数与 lambda。

    只看 facade 自己写了什么，而不是它内联的 lambda 里碰巧有什么 —— 否则
    ``resolve_asof_day=lambda: _date(asof_date)`` 里的 ``_date`` 会被误当成
    facade 的直接调用。
    """
    stack = [node]
    while stack:
        for child in ast.iter_child_nodes(stack.pop()):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                yield child
            stack.append(child)


def _run_slot_function():
    tree = ast.parse(_PAPER_TRADING_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_slot":
            return node
    return None


def _is_second_date_resolution(stmt):
    """判定 ``day = _date(asof_date)``：facade 在 preflight 之后的再次解析。"""
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return False
    target = stmt.targets[0]
    if not (isinstance(target, ast.Name) and target.id == "day"):
        return False
    value = stmt.value
    if not (isinstance(value, ast.Call) and _dotted_name(value.func) == "_date"):
        return False
    return (len(value.args) == 1 and isinstance(value.args[0], ast.Name)
            and value.args[0].id == "asof_date")


def _top_level_statement(func, predicate):
    """返回 ``(语句索引, 语句)``；这是 run_slot 顶层的第几条语句。"""
    for index, stmt in enumerate(func.body):
        if predicate(stmt):
            return index, stmt
    return None, None


class FacadeDependencyGuardTests(unittest.TestCase):
    """facade 用到的模块别名必须真的被 import。

    回归背景（PR #113 引入的真实故障）：把 slot 清理委托给 service 时，
    ``import entry_lifecycle as ELC`` / ``import execution_dispatch as EPD``
    被一并换成了 ``import paper_slot_service as PSS``，但 freshness 查询、
    entry slice plan、dispatch 规划/核验、gated order 查询等**非 cleanup**
    用法还在。结果是 facade 一跑到 ``_buy_order`` 就 ``NameError: name 'ELC'
    is not defined`` —— 语法检查、ruff 和 import 级测试都发现不了，只有真跑
    成交路径才会炸。
    """

    def test_facade_uses_no_unbound_module_alias(self):
        tree = ast.parse(_PAPER_TRADING_SOURCE.read_text(encoding="utf-8"))
        bound = set(dir(__builtins__))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bound.update(alias.asname or alias.name.split(".")[0]
                             for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                bound.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node, ast.arg):
                bound.add(node.arg)

        used = {node.value.id for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id[:1].isupper()}
        self.assertEqual(set(), used - bound,
                         "paper_trading.py 使用了未 import 的模块别名（运行期会 NameError）")

    def test_facade_still_binds_the_lifecycle_and_dispatch_modules(self):
        """显式锁住这两个别名，避免有人把 import 当成"cleanup 残留"再删一次。"""
        tree = ast.parse(_PAPER_TRADING_SOURCE.read_text(encoding="utf-8"))
        aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update(alias.asname or alias.name.split(".")[0]
                               for alias in node.names)
        self.assertIn("ELC", aliases)
        self.assertIn("EPD", aliases)


class ArchitectureGuardTests(unittest.TestCase):
    def test_service_never_imports_paper_trading_or_api_layer(self):
        source = Path(PSS.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        self.assertNotIn("paper_trading", imported)
        self.assertNotIn("api_paper", imported)
        self.assertEqual(imported & {"entry_lifecycle", "execution_dispatch"}, {
            "entry_lifecycle", "execution_dispatch",
        })

    def test_facade_delegates_preflight_without_legacy_cleanup_calls(self):
        """facade 的架构契约按 AST 语义校验，**不依赖源码排版**。

        这里刻意不用源码字符串匹配：``PSS.run_preflight(...)`` 写成一行还是
        多行、参数怎么折行，都是格式化工具的自由。生产语义正确时不该因为
        换行方式不同而变红，也不该因为恰好写在一行就变绿。
        """
        # A. 找到 run_slot，并锁住签名
        func = _run_slot_function()
        self.assertIsNotNone(func, "paper_trading.py 中找不到 run_slot")
        self.assertEqual(["slot", "asof_date", "force"], [a.arg for a in func.args.args])
        self.assertEqual(2, len(func.args.defaults))
        self.assertIsNone(func.args.defaults[0].value)
        self.assertIs(False, func.args.defaults[1].value)

        calls: dict = {}
        for stmt in func.body:
            for call in _iter_calls(stmt):
                calls.setdefault(_dotted_name(call.func), []).append(call)

        # B. slot 校验仍委托给 service，且参数就是 slot
        validate = calls.get("PSS.validate_slot") or []
        self.assertEqual(1, len(validate), "run_slot 必须且只能委托一次 PSS.validate_slot")
        self.assertEqual(1, len(validate[0].args))
        self.assertIsInstance(validate[0].args[0], ast.Name)
        self.assertEqual("slot", validate[0].args[0].id)

        # C. preflight 委托，并锁住三个 keyword 依赖（只看函数名会丢掉依赖保护）
        preflight = calls.get("PSS.run_preflight") or []
        self.assertEqual(1, len(preflight), "run_slot 必须且只能委托一次 PSS.run_preflight")
        keywords = {kw.arg: kw.value for kw in preflight[0].keywords}
        self.assertIsInstance(keywords.get("db_factory"), ast.Name)
        self.assertEqual("_db", keywords["db_factory"].id)
        self.assertIsInstance(keywords.get("audit"), ast.Name)
        self.assertEqual("_audit", keywords["audit"].id)
        resolver = keywords.get("resolve_asof_day")
        self.assertIsInstance(resolver, ast.Lambda)
        self.assertEqual([], resolver.args.args)
        self.assertIsInstance(resolver.body, ast.Call)
        self.assertEqual("_date", _dotted_name(resolver.body.func))
        self.assertEqual(1, len(resolver.body.args))
        self.assertIsInstance(resolver.body.args[0], ast.Name)
        self.assertEqual("asof_date", resolver.body.args[0].id)

        # D. 顺序：preflight 必须早于 facade 的第二次 _date(asof_date)
        #    这条兼容路径锁的是"resolver 内首次解析失败后，facade 仍会再解析一次
        #    并把原始异常继续向上抛"，顺序颠倒会改变异常语义。
        preflight_index, preflight_stmt = _top_level_statement(
            func, lambda s: any(_dotted_name(c.func) == "PSS.run_preflight"
                                for c in _iter_calls(s)))
        date_index, date_stmt = _top_level_statement(func, _is_second_date_resolution)
        self.assertIsNotNone(preflight_index, "run_slot 顶层找不到 PSS.run_preflight 语句")
        self.assertIsNotNone(
            date_index,
            "run_slot 必须保留 preflight 之后的第二次 _date(asof_date) 解析")
        self.assertLess(preflight_index, date_index,
                        "PSS.run_preflight 必须在第二次 _date(asof_date) 之前")
        self.assertLess(preflight_stmt.lineno, date_stmt.lineno)

        # E. facade 不得重新持有 legacy cleanup（按调用目标判定，别名/注释/排版变化不误报）
        for forbidden in ("ELC.expire_stale_signals", "ELC.expire_stale_orders",
                          "EPD.run_execution_dispatch"):
            self.assertNotIn(forbidden, calls,
                             f"facade 不得直接调用 {forbidden}，必须委托给 slot service")


if __name__ == "__main__":
    unittest.main()
