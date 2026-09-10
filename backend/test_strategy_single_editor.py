# -*- coding: utf-8 -*-
"""PR-52：策略定义只有一个编辑入口（策略工坊），生命周期只有一条写入 API。

覆盖：

1. **前端**：模拟交易的「运行策略」视图里没有定义编辑器——没有 DSL/版本/克隆/
   生命周期写入，也没有 ``/api/strategy-definitions`` 这类第二套定义 API；
   它只读运行时状态，并通过「在策略工坊打开」跳转到唯一编辑器。
2. **导航**：主导航是「策略工坊」，模拟交易内部是「运行策略」；全站不再出现
   「策略中心」这个歧义名。
3. **后端**：``/api/strategies/{id}/validate|activate|pause|archive`` 与
   ``/api/strategy-definitions*`` 已删除；canonical 的 ``/transition``、
   DSL ``/validate``、``/preview`` 仍在。
4. **行为不变量（API 边界）**：pause 不改变不可变版本、archive 后历史仍可读；
   （深层账本不变量由既有 ``test_cycle_ledger_ownership.py`` / PR-36
   ``test_strategy_archive_replay.py`` 继续覆盖，本文件不复制那套规则。）
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from fastapi import HTTPException

import api_strategies as API
import frontend_sources
import main
import paper_trading as P
import strategy_registry as SR
import strategy_runtime as SRT
import strategy_service as SVC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# PR-55：源已拆到 frontend/src/**；用助手拿"整份源码"做包含断言。
APP_JS = os.path.join(ROOT, "frontend", "src")
INDEX_HTML = os.path.join(ROOT, "frontend", "index.html")
DIST_JS = os.path.join(ROOT, "frontend", "dist", "app.js")
MAIN_PY = os.path.join(ROOT, "backend", "main.py")

# 旧编辑器残留（函数名 / DOM 挂载点 / 第二套定义 API）。
LEGACY_EDITOR_SYMBOLS = (
    "openStrategyBuilder", "closeStrategyBuilder", "loadStrategyRegistry",
    "renderStrategyRegistry", "strategyLifecycleAction", "submitStrategyBuilder",
    "strategyBuilderMount", "strategyRegistryList", "sbRenderNode", "sbDslToGroup",
    "/api/strategy-definitions",
)
# 旧生命周期别名（只有 canonical /transition 允许存在）。
LEGACY_LIFECYCLE_ALIASES = ("validate", "activate", "pause", "archive")


def _read(path: str) -> str:
    """读单个文件；传目录（新布局的 frontend/src）时返回全部模块拼接。"""
    if os.path.isdir(path):
        return frontend_sources.source_text()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


RULE = {
    "op": "and",
    "args": [
        {"op": "gt", "left": {"op": "field", "name": "close"},
         "right": {"op": "indicator", "name": "ma", "window": 20}},
        {"op": "gt", "left": {"op": "field", "name": "volume"},
         "right": {"op": "mul", "left": {"op": "indicator", "name": "volume_mean", "window": 5},
                   "right": {"op": "const", "value": 1.2}}},
    ],
}


class FrontendSingleEditorTests(unittest.TestCase):
    """模拟交易的运行策略视图里没有第二套 Strategy Definition 编辑器。"""

    @classmethod
    def setUpClass(cls):
        cls.source = _read(APP_JS)
        cls.dist = _read(DIST_JS)

    def _paper_view_block(self) -> str:
        start = self.source.index("function loadPaperStrategyCenter(){")
        end = self.source.index("function paperResearchStrategyName(id){")
        return self.source[start:end]

    def test_legacy_editor_is_gone_from_source(self):
        for symbol in LEGACY_EDITOR_SYMBOLS:
            self.assertNotIn(symbol, self.source, symbol)

    def test_legacy_editor_is_gone_from_built_bundle(self):
        for symbol in LEGACY_EDITOR_SYMBOLS:
            self.assertNotIn(symbol, self.dist, symbol)

    def test_paper_view_has_no_definition_write_path(self):
        block = self._paper_view_block()
        # 只有 GET 辅助函数；没有任何写动词。
        for verb in ("apiPostJson(", "apiJson(", "apiPost("):
            self.assertNotIn(verb, block, verb)
        for verb in ("PATCH", "POST", "DELETE", "PUT"):
            self.assertNotIn("'%s'" % verb, block, verb)
        # 也没有任何"按策略 id 调接口"的写法（写操作必然要带 id）。
        self.assertNotIn("encodeURIComponent", block)

    def test_paper_view_is_runtime_only(self):
        block = self._paper_view_block()
        # 只读运行时来源 + 卡片展示的运行字段。
        self.assertIn("/api/paper/allocation-explain", block)
        self.assertIn("/api/strategies?include_archived=true", block)
        for field in ("running", "capital_scale", "position_limit", "position_count",
                      "waiting_reason", "blocked_reason", "current_version"):
            self.assertIn(field, block, field)

    def test_paper_view_links_to_the_workbench(self):
        block = self._paper_view_block()
        self.assertIn("openInStrategyWorkbench", block)
        start = self.source.index("function openInStrategyWorkbench(strategyId){")
        body = self.source[start:start + 600]
        self.assertIn("activatePage('p-strategies')", body)
        self.assertIn("wbOpenDetail(", body)


class NavigationNamingTests(unittest.TestCase):
    """主导航=策略工坊；模拟交易内部=运行策略；全站不再出现「策略中心」。"""

    @classmethod
    def setUpClass(cls):
        cls.html = _read(INDEX_HTML)
        cls.source = _read(APP_JS)

    def test_main_nav_is_strategy_workshop(self):
        start = self.html.index('data-page="p-strategies"')
        end = self.html.index("</button>", start)
        tab = self.html[start:end]
        self.assertIn("策略工坊", tab)
        self.assertNotIn("策略中心", tab)

    def test_paper_module_tab_is_running_strategies(self):
        start = self.html.index('data-paper-view="strategy"')
        end = self.html.index("</button>", start)
        tab = self.html[start:end]
        self.assertIn("运行策略", tab)
        self.assertNotIn("策略中心", tab)

    def test_ambiguous_name_is_gone(self):
        self.assertNotIn("策略中心", self.html)
        self.assertNotIn("策略中心", self.source)

    def test_settings_links_point_to_the_workshop(self):
        self.assertIn("去策略工坊", self.source)
        self.assertNotIn("去策略中心", self.source)


class CanonicalApiSurfaceTests(unittest.TestCase):
    """生命周期写入只有 /transition；定义 API 只有 /api/strategies。"""

    @classmethod
    def setUpClass(cls):
        cls.paths = main.app.openapi().get("paths") or {}
        cls.main_source = _read(MAIN_PY)

    def test_canonical_routes_exist(self):
        self.assertIn("/api/strategies", self.paths)
        self.assertIn("/api/strategies/{strategy_id}/transition", self.paths)
        self.assertIn("post", self.paths["/api/strategies/{strategy_id}/transition"])
        # DSL 校验与预览：与 /{strategy_id}/... 完全不同，必须保留。
        self.assertIn("/api/strategies/validate", self.paths)
        self.assertIn("/api/strategies/preview", self.paths)

    def test_legacy_lifecycle_aliases_are_gone(self):
        for alias in LEGACY_LIFECYCLE_ALIASES:
            path = "/api/strategies/{strategy_id}/%s" % alias
            self.assertNotIn(path, self.paths, path)

    def test_duplicate_definition_api_is_gone(self):
        self.assertNotIn("/api/strategy-definitions", self.paths)
        self.assertNotIn("/api/strategy-definitions/{strategy_id}/versions", self.paths)

    def test_removed_routes_are_not_reintroduced_in_main(self):
        for alias in LEGACY_LIFECYCLE_ALIASES:
            self.assertNotIn('@app.post("/api/strategies/{strategy_id}/%s")' % alias,
                             self.main_source)
        self.assertNotIn('@app.get("/api/strategy-definitions"', self.main_source)

    def test_dsl_validation_endpoint_is_distinct_from_lifecycle(self):
        # /api/strategies/validate 是**无副作用**的 DSL 校验；它不接受策略 id，
        # 也不得变成生命周期写入口。
        self.assertEqual(
            {"post"}, {m.lower() for m in self.paths["/api/strategies/validate"]},
        )
        self.assertNotIn("/api/strategies/validate", [
            p for p in self.paths if "{strategy_id}" in p
        ])


class CanonicalLifecycleBehaviourTests(unittest.TestCase):
    """canonical /transition 的行为不变量（在 API 边界上验证）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-single-editor-")
        cls._old_db = P.DB_PATH
        cls._old_sr_db = SR.DEFAULT_DB_PATH
        P.DB_PATH = os.path.join(cls._tmp, "paper_trading.sqlite3")
        SR.DEFAULT_DB_PATH = P.DB_PATH
        P.init_db()

    @classmethod
    def tearDownClass(cls):
        SRT.clear_cache()
        P.DB_PATH = cls._old_db
        SR.DEFAULT_DB_PATH = cls._old_sr_db
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        SRT.clear_cache()
        # 每个用例用独立策略 id（同一临时库、且生命周期用例会推进到 archived）。
        self.id = ("single_editor_" + self._testMethodName)[:63].lower()
        try:
            API.create_strategy({
                "id": self.id, "name": "唯一编辑入口验证", "dsl_ast": RULE,
                "metadata": {"style": "trend", "hold": 8}, "actor": "test",
            })
        except HTTPException:
            pass

    def _transition(self, to_status, expected_status=None):
        payload = {"to_status": to_status, "reason": "test", "actor": "test"}
        if expected_status is not None:
            payload["expected_status"] = expected_status
        return API.transition_strategy(self.id, payload)

    def test_canonical_transition_drives_the_whole_lifecycle(self):
        self.assertEqual("validated", self._transition("validated", "draft")["status"])
        active = self._transition("active", "validated")
        self.assertEqual("active", active["status"])
        self.assertTrue(active["supports_new_cycle"])

        paused = self._transition("paused", "active")
        self.assertEqual("paused", paused["status"])
        self.assertFalse(paused["supports_new_cycle"])
        # pause 不改变不可变版本：版本号与 checksum 原样保留（账本可继续解析）。
        self.assertEqual(1, paused["version"])
        version = SVC.get_strategy(self.id)
        self.assertEqual(1, version.current_version)
        self.assertTrue(version.current_checksum)

        self.assertEqual("active", self._transition("active", "paused")["status"])
        self.assertEqual("retiring", self._transition("retiring", "active")["status"])
        archived = self._transition("archived", "retiring")
        self.assertEqual("archived", archived["status"])
        self.assertFalse(archived["supports_new_cycle"])

    def test_archive_keeps_history_readable(self):
        self._transition("validated", "draft")
        self._transition("active", "validated")
        self._transition("retiring", "active")
        self._transition("archived", "retiring")

        versions = API.list_strategy_versions(self.id)
        self.assertEqual([1], [row["version"] for row in versions["items"]])
        self.assertTrue(versions["items"][0]["checksum"])
        events = API.list_strategy_events(self.id)
        transitions = [(row["from_status"], row["to_status"]) for row in events["items"]]
        self.assertIn(("active", "retiring"), transitions)
        self.assertIn(("retiring", "archived"), transitions)
        # 归档后的策略仍可读详情（历史血缘不删除）。
        self.assertEqual("archived", API.get_strategy(self.id)["status"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
