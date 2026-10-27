# -*- coding: utf-8 -*-
"""退役策略称谓与「当前账户数」口径的机械守卫。

背景（模块化审计 C3 / B4）：前端曾用一个监听 ``document.body`` 全部新增节点的
``MutationObserver``，把缓存载荷里残留的「五套当前模拟账户」「五策略」在**渲染后**
逐文本节点改写。那是"用观察器修补源模板"，代价是每次 DOM 变更都要走一遍文本树，
而且真正的问题（谁还在生产退役称谓、谁是"当前账户"的权威）被掩盖了。

本文件锁两件事：

1. **没有可渲染字符串再产出退役称谓** —— Python 侧扫 ``str`` 常量（模块/函数/
   类 docstring 除外：那是历史解释，允许存在），前端扫去掉注释后的源码字面量。
2. **口径分母有 owner** —— 前端不得再出现硬编码的账户分母（``/ 5 当前账户``），
   必须使用后端 ``closed_loop.strategy_coverage_total``；该字段由
   ``adaptive_engine._weight_participants()`` 的周期参与快照派生，拿不到时报
   ``None``（unknown），不写 0、不回落到内置策略数量。

两条守卫都带**非空性**断言：把违规文本喂给同一个扫描函数必须变红，否则
"扫描返回空" 只说明扫描器坏了。
"""
from __future__ import annotations

import ast
import os
import re
import unittest

import frontend_sources

BACKEND = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(frontend_sources.FRONTEND, "src")

# 闭集：只有这些形态算退役称谓。刻意不把「五套」单独列进来 —— 「五套历史
# 周期」「五套时代的提案」是历史解释，不是可渲染的账户称谓。
RETIRED_COPY = re.compile(r"(?:二|两|三|四|五)套(?:当前)?(?:模拟账户|策略)|五策略")
# 硬编码的账户分母（真正陈旧的是数字本身，不是"账户"这个词）。
HARDCODED_DENOMINATOR = re.compile(r"/\s*[1-9]\s*当前(?:模拟)?账户")
COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def _python_renderable_strings(source, filename):
    """Yield ``(lineno, value)`` for string constants excluding docstrings."""
    tree = ast.parse(source, filename=filename)
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        first = body[0] if body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            yield node.lineno, node.value


def _python_offenders(sources, pattern=RETIRED_COPY):
    found = []
    for name, text in sources.items():
        for lineno, value in _python_renderable_strings(text, name):
            if pattern.search(value):
                found.append((name, lineno, value.strip()[:80]))
    return found


def _js_offenders(sources, pattern=RETIRED_COPY):
    found = []
    for name, text in sources.items():
        stripped = COMMENT.sub("", text)
        for match in pattern.finditer(stripped):
            line = stripped.count("\n", 0, match.start()) + 1
            found.append((name, line, match.group(0)))
    return found


def _production_python_sources():
    sources = {}
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs
                   if d not in {".venv", "__pycache__", "node_modules"}
                   and not d.startswith(".")]
        for name in files:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                sources[os.path.relpath(path, BACKEND)] = handle.read()
    return sources


def _frontend_sources():
    sources = {}
    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d != "node_modules"]
        for name in files:
            if not name.endswith(".js"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                sources[os.path.relpath(path, SRC).replace(os.sep, "/")] = handle.read()
    return sources


class RetiredStrategyCopyGuard(unittest.TestCase):
    def test_no_production_python_renders_retired_copy(self):
        offenders = _python_offenders(_production_python_sources())
        self.assertEqual([], offenders, f"退役称谓仍可被渲染：{offenders}")

    def test_no_frontend_source_renders_retired_copy(self):
        offenders = _js_offenders(_frontend_sources())
        self.assertEqual([], offenders, f"前端仍产出退役称谓：{offenders}")

    def test_no_frontend_hardcodes_the_account_denominator(self):
        offenders = _js_offenders(_frontend_sources(), HARDCODED_DENOMINATOR)
        self.assertEqual([], offenders, f"硬编码账户分母：{offenders}")

    def test_coverage_denominator_comes_from_the_owner_field(self):
        adaptive = _frontend_sources()["features/adaptive.js"]
        self.assertIn("closedLoop.strategy_coverage_total", adaptive)
        self.assertIn("当前参与账户", adaptive)

    def test_tree_observer_and_normalizer_are_gone(self):
        for name in ("boot.js", "features/strategies.js", "app.js"):
            path = os.path.join(SRC, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn("normalizeActiveStrategyCopy", text, name)
        with open(os.path.join(SRC, "boot.js"), encoding="utf-8") as handle:
            boot = handle.read()
        self.assertNotIn("strategyCopyObserver", boot)

    # ---- 非空性：守卫必须能被同一份扫描函数驱动成 RED ----

    def test_python_scan_detects_a_synthetic_producer(self):
        synthetic = {
            "synthetic.py": (
                'MSG = "已配置五套当前模拟账户"\n'
                'def f():\n'
                '    """历史解释：五策略时代。"""\n'
                '    return MSG\n'
            ),
        }
        offenders = _python_offenders(synthetic)
        self.assertEqual(1, len(offenders), offenders)
        self.assertEqual(1, offenders[0][1])

    def test_js_scan_detects_synthetic_copy_but_ignores_comments(self):
        synthetic = {
            "synthetic.js": (
                '// 历史解释：五策略时代\n'
                'var x="覆盖 3 / 5 当前账户";\n'
                'var y="已启用策略";\n'
            ),
        }
        self.assertEqual(1, len(_js_offenders(synthetic, HARDCODED_DENOMINATOR)))
        self.assertEqual([], _js_offenders(synthetic))


class ClosedLoopCoverageDenominator(unittest.TestCase):
    """``strategy_coverage_total`` 只接受 owner 给的计数，缺失即 unknown。"""

    def _state(self, participant_total):
        import sqlite3

        import adaptive_engine as AE

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE adaptive_market_profiles (profile_date TEXT);
            CREATE TABLE adaptive_evidence_chains (
                account_id TEXT, ledger_type TEXT, integrity_status TEXT,
                risk_decision_id TEXT, integrity_flags TEXT, order_at TEXT
            );
            CREATE TABLE adaptive_canary_state (id INTEGER PRIMARY KEY, updated_at TEXT, evidence TEXT);
            INSERT INTO adaptive_canary_state (id, updated_at, evidence) VALUES (1, '', '{}');
            INSERT INTO adaptive_evidence_chains (account_id, ledger_type, integrity_status, order_at)
                VALUES ('tq_breakout', 'actual', 'valid', ''),
                       ('trend_pullback', 'actual', 'valid', '');
            """
        )
        evidence = {"orders": 2, "linked": 2, "valid": 2, "actual": 2, "counterfactual": 0}
        try:
            return AE._closed_loop_state(conn, evidence, participant_total=participant_total)
        finally:
            conn.close()

    def test_absent_participants_report_unknown_not_zero(self):
        state = self._state(None)
        self.assertEqual(2, state["strategy_coverage"])
        self.assertIsNone(state["strategy_coverage_total"])

    def test_zero_participants_report_unknown_not_zero(self):
        self.assertIsNone(self._state(0)["strategy_coverage_total"])

    def test_owner_count_is_published_verbatim(self):
        self.assertEqual(3, self._state(3)["strategy_coverage_total"])


if __name__ == "__main__":
    unittest.main()
