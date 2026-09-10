# -*- coding: utf-8 -*-
"""PR-47 regression: Settings must be registry-driven, not fixed-five.

静态契约测试：
- index.html 不再出现"五套策略/五套模型/五套账户"旧产品表述；
- app.js 的策略集合渲染必须走 ``settingsStrategyItems()``（Registry 驱动，
  SETTINGS_STRATEGY_NAMES 仅作为 Registry 不可用时的 builtin 展示兜底）；
- 旧模式（直接 ``Object.keys(SETTINGS_STRATEGY_NAMES)`` 生成勾选框/参数卡）
  不允许回潮；
- 后端保存语义：显式空 enabled_strategies 合法（零策略 idle 周期）。
"""
from __future__ import annotations
import frontend_sources

import os
import sqlite3
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(os.path.dirname(BACKEND), "frontend")

import runtime_settings as settings  # noqa: E402
import strategy_registry as registry  # noqa: E402


def _load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class SettingsRegistryDrivenTests(unittest.TestCase):
    def setUp(self):
        self.index_source = _load(os.path.join(FRONTEND, "index.html"))
        self.app_source = frontend_sources.source_text()

    def test_index_html_has_no_fixed_five_copy(self):
        for phrase in ("五套策略", "五套模型", "五套账户"):
            self.assertNotIn(phrase, self.index_source, f"index.html 仍含旧文案: {phrase}")

    def test_strategy_collection_is_registry_driven(self):
        # 勾选框与参数卡必须经 settingsStrategyItems() 渲染。
        self.assertIn("function settingsStrategyItems()", self.app_source)
        self.assertIn("settingsStrategyItems()", self.app_source)
        # 旧的直接枚举渲染模式不允许回潮。
        self.assertNotIn(
            "Object.keys(SETTINGS_STRATEGY_NAMES).map(function(id){return '<label>"
            "<input type=\"checkbox\" class=\"setting-strategy-enabled\"",
            self.app_source,
        )

    def test_settings_strategy_names_is_fallback_only(self):
        # SETTINGS_STRATEGY_NAMES 只允许出现在 settingsStrategyItems() 兜底
        # 函数体内（Registry 不可用时的 builtin 展示兜底）。
        marker = "function settingsStrategyItems()"
        start = self.app_source.index(marker)
        # 兜底函数体：从 marker 到下一个顶层 function 声明。
        end = self.app_source.find("\nfunction ", start + len(marker))
        body = self.app_source[start:end if end > 0 else len(self.app_source)]
        uses_outside = [
            line.strip() for line in self.app_source.splitlines()
            if "SETTINGS_STRATEGY_NAMES" in line
            and "var SETTINGS_STRATEGY_NAMES" not in line
            and line.strip() not in body
        ]
        self.assertFalse(
            uses_outside,
            f"SETTINGS_STRATEGY_NAMES 出现在兜底之外的渲染路径: {uses_outside[:2]}",
        )

    def test_checkbox_only_for_active_new_cycle_strategies(self):
        # 勾选资格门禁：active && supports_new_cycle；paused 提供恢复入口
        # （PR-52 起该入口指向「策略工坊」，它是唯一的生命周期编辑器）。
        self.assertIn("item.status==='active'&&item.supports_new_cycle", self.app_source)
        self.assertIn("去策略工坊恢复", self.app_source)

    def test_explicit_empty_enabled_strategies_is_valid_idle_cycle(self):
        """后端语义：空列表合法，读取返回空（不回落全集）。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, enabled_strategies TEXT,"
            " duration_days INTEGER)"
        )
        settings.ensure_schema(conn)
        try:
            settings.update(conn, {"enabled_strategies": []}, actor="test")
            self.assertEqual([], settings.enabled_strategies(conn))
            conn.execute("DELETE FROM paper_runtime_settings WHERE key='enabled_strategies'")
            self.assertEqual(
                list(registry.active_ids(conn=conn)),
                settings.enabled_strategies(conn),
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
