# -*- coding: utf-8 -*-
"""策略支持范围表与代码闭集必须逐列相等（B3/B4 的机械守卫）。

`docs/STRATEGY_SUPPORT_MATRIX.md` 回答"这个策略现在到底在哪条链路上被支持"。
如果那份表靠手写维护，它会在第一次改注册表/名册时变成一份好看的谎话 —— 而这
正是 B4 要修的问题（菜单隐藏了，直接调用入口还在）。所以这里解析文档里的表格，
和代码侧的闭集逐列比对：任何一侧漂移都会红。

四列的权威来源：

- 正式新入场 → ``strategy_registry.BUILTIN_STRATEGIES`` 中 ``supports_new_cycle`` 的 id，
  并必须与 ``strategy_roster.formal_strategy_ids()`` 相等（名册与注册表不许各说一套）；
- 旧持仓退出 → ``strategy_roster.exit_only_ids()``，并必须等于 ``LEGACY_STRATEGIES`` 的 id；
- 独立研究 → 已登记插件 id ∪ ``strategies.STRATEGIES``（研究入口另有实现）；
- 历史回放 → 与独立研究同一闭集（回放不能凭空多出未注册的 selector）。
"""
from __future__ import annotations

import os
import unittest

import strategies as S
import strategy_plugins as SP
import strategy_registry as SR
import strategy_roster as ROSTER

BACKEND = os.path.dirname(os.path.abspath(__file__))
DOC = os.path.join(os.path.dirname(BACKEND), "docs", "STRATEGY_SUPPORT_MATRIX.md")
SUPPORT = "支持"
HEADER = ("strategy_id", "正式新入场", "独立研究", "历史回放", "旧持仓退出")


def _rows(text):
    """Parse every support table in the document into ``id -> column cells``."""
    rows = {}
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        # 第一列的表头允许写成 strategy_id / selector_id：研究专用 selector 那
        # 一张表用的是后者，但四列语义完全一致。
        if len(cells) != len(HEADER) or cells[1:] != HEADER[1:]:
            continue
        for body in lines[index + 2:]:
            if not body.startswith("|"):
                break
            values = tuple(cell.strip() for cell in body.strip("|").split("|"))
            if len(values) != len(HEADER):
                raise AssertionError(f"支持矩阵列数不对: {body}")
            key = values[0]
            if key in rows:
                raise AssertionError(f"支持矩阵重复登记: {key}")
            rows[key] = dict(zip(HEADER[1:], values[1:], strict=True))
    return rows


def _supported(rows, column):
    return {key for key, cells in rows.items() if cells[column] == SUPPORT}


def _registered():
    return {spec.id for spec in SR.BUILTIN_STRATEGIES} | set(ROSTER.exit_only_ids())


class SupportMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(DOC, encoding="utf-8") as handle:
            cls.rows = _rows(handle.read())

    def test_document_registers_every_reachable_id_exactly_once(self):
        self.assertEqual(_registered() | set(S.STRATEGIES), set(self.rows))

    def test_formal_entry_matches_registry_and_roster(self):
        expected = {spec.id for spec in SR.BUILTIN_STRATEGIES if spec.supports_new_cycle}
        self.assertEqual(expected, _supported(self.rows, "正式新入场"))
        self.assertEqual(set(ROSTER.formal_strategy_ids()), expected)

    def test_legacy_exit_matches_the_roster(self):
        self.assertEqual(set(ROSTER.exit_only_ids()), _supported(self.rows, "旧持仓退出"))
        self.assertEqual({spec.id for spec in SR.LEGACY_STRATEGIES},
                         _supported(self.rows, "旧持仓退出"))

    def test_research_and_replay_cover_the_whole_reachable_set(self):
        expected = _registered() | set(S.STRATEGIES)
        for column in ("独立研究", "历史回放"):
            self.assertEqual(expected, _supported(self.rows, column), column)

    def test_retiring_ids_stay_reachable_on_purpose(self):
        # B4 记录的事实：对外提供面已经不含退役 id，但按 id 仍能取到实现。
        self.assertFalse(set(ROSTER.exit_only_ids()) & set(SP.plugin_ids()))
        self.assertEqual({spec.id for spec in SR.BUILTIN_STRATEGIES}, set(SP.plugin_ids()))
        for strategy_id in sorted(_registered()):
            with self.subTest(strategy_id=strategy_id):
                self.assertEqual(strategy_id, SP.get_plugin(strategy_id).strategy_id)

    def test_retiring_strategies_are_never_formal_entry(self):
        self.assertFalse(set(ROSTER.exit_only_ids()) & _supported(self.rows, "正式新入场"))

    def test_parser_is_not_vacuous(self):
        # 非空性：同一份解析器必须能读出一张合成表，否则"相等"只是两边都为空。
        synthetic = (
            "| strategy_id | 正式新入场 | 独立研究 | 历史回放 | 旧持仓退出 |\n"
            "|---|---|---|---|---|\n"
            "| alpha | 支持 | 支持 | 支持 | — |\n"
        )
        rows = _rows(synthetic)
        self.assertEqual({"alpha"}, set(rows))
        self.assertEqual({"alpha"}, _supported(rows, "正式新入场"))
        with self.assertRaises(AssertionError):
            _rows(synthetic + "| alpha | 支持 | 支持 | 支持 | — |\n")


if __name__ == "__main__":
    unittest.main()
