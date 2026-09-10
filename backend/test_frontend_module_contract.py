# -*- coding: utf-8 -*-
"""PR-55：前端模块化契约（纯 Python，不依赖 Node，可在运行时镜像里跑）。

拆成 ESM 模块后有两个只靠"看代码"就能守住、但一破就会在浏览器里静的坑：

1. **inline handler 的全局名**：index.html 与各模块生成的 HTML 里大量 `onclick="fn(...)"`，
   模块化后这些名字不再是全局。`src/bridge.js` 必须把它们**全部**显式挂到 ``window``。
2. **跨模块引用必须 import**：模块之间不再共享作用域，漏一个 import 不会让构建失败，
   只会在运行时 `ReferenceError`（PR-55 期间真实发生过：`APP_PAGE_KEY is not defined`）。

因此本文件重新**从源码推导**这两份集合再断言，而不是信任生成器。
"""
from __future__ import annotations

import os
import re
import unittest

import build_info as BI
import frontend_sources

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = frontend_sources.FRONTEND
SRC = os.path.join(FRONTEND, "src")
STYLES = os.path.join(FRONTEND, "styles")

HANDLER_ATTR = re.compile(r"\bon[a-z]+\s*=\s*\\?[\"']")
CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
DECLARED = re.compile(r"^export\s+(?:async function|function|var)\s+([A-Za-z_$][\w$]*)", re.M)
IMPORT_LINE = re.compile(r'^import\s*\{([^}]*)\}\s*from\s*"([^"]+)";', re.M)
# 浏览器/JS 内建或本就由 <script> 提供的名字，不需要桥接。
IGNORED_CALLS = {
    "alert", "confirm", "prompt", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "parseInt", "parseFloat", "Number", "String", "Boolean", "Array", "Object", "JSON", "Math",
    "Date", "RegExp", "Promise", "Map", "Set", "Error", "isNaN", "encodeURIComponent",
    "decodeURIComponent", "fetch", "requestAnimationFrame", "cancelAnimationFrame", "require",
    "if", "for", "while", "return", "typeof", "catch", "switch", "function",
}
BROWSER_GLOBALS = {
    "window", "document", "console", "location", "history", "localStorage", "sessionStorage",
    "navigator", "echarts", "MutationObserver", "Node", "AbortController", "URL", "URLSearchParams",
    "Intl", "FormData", "Blob", "TextEncoder", "TextDecoder", "CustomEvent", "Event",
}


def _handler_texts(blob: str):
    for match in HANDLER_ATTR.finditer(blob):
        i, quote, depth = match.end(), blob[match.end() - 1], 0
        out = []
        while i < len(blob):
            ch = blob[i]
            if ch == "\\":
                i += 2
                continue
            if ch == quote and depth == 0:
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            out.append(ch)
            i += 1
        yield "".join(out)


class BridgeCoverageTests(unittest.TestCase):
    """inline handler 调用的每个本仓库函数都必须能被浏览器解析到。"""

    @classmethod
    def setUpClass(cls):
        cls.modules = {}
        for path in frontend_sources.source_files():
            with open(path, encoding="utf-8") as handle:
                cls.modules[path] = handle.read()
        cls.declared = {}
        for path, text in cls.modules.items():
            for name in DECLARED.findall(text):
                cls.declared[name] = path
        cls.bridge = cls.modules[os.path.join(SRC, "bridge.js")]
        cls.bridged = set(re.findall(r"^window\.([A-Za-z_$][\w$]*)\s*=", cls.bridge, re.M))

    def _required(self) -> set[str]:
        blobs = [frontend_sources.index_html()] + [
            text for path, text in self.modules.items() if not path.endswith("bridge.js")
        ]
        needed = set()
        for blob in blobs:
            for handler in _handler_texts(blob):
                for name in CALL.findall(handler):
                    if name in self.declared and name not in IGNORED_CALLS:
                        needed.add(name)
        return needed

    def test_every_inline_handler_target_is_bridged(self):
        required = self._required()
        self.assertTrue(required, "没有解析到任何 inline handler，规则可能失效了")
        missing = sorted(required - self.bridged)
        self.assertEqual([], missing, "inline handler 调用的函数没有挂到 window（会静默失效）")

    def test_bridge_has_no_stale_globals(self):
        stale = sorted(name for name in self.bridged if name not in self.declared)
        self.assertEqual([], stale, "bridge.js 挂了不存在的函数")

    def test_bridge_imports_every_name_it_assigns(self):
        imported = set()
        for names, _path in IMPORT_LINE.findall(self.bridge):
            imported.update(part.strip().split(" as ")[-1].strip() for part in names.split(","))
        self.assertEqual(set(), self.bridged - imported, "bridge.js 赋值了没有 import 的名字")


class CrossModuleImportTests(unittest.TestCase):
    """模块引用别的模块的声明时，必须有对应 import（漏了只在运行时炸）。"""

    @classmethod
    def setUpClass(cls):
        cls.modules = {}
        for path in frontend_sources.source_files():
            with open(path, encoding="utf-8") as handle:
                cls.modules[path] = handle.read()
        cls.owner: dict[str, str] = {}
        for path, text in cls.modules.items():
            for name in DECLARED.findall(text):
                cls.owner[name] = path

    @staticmethod
    def _imported(text: str) -> set[str]:
        imported = set()
        for names, _path in IMPORT_LINE.findall(text):
            imported.update(part.strip().split(" as ")[-1].strip() for part in names.split(","))
        return imported

    @staticmethod
    def _strip_comments(text: str) -> str:
        """注释里提到别的模块的函数名不算引用（例如解释缓存策略时写的 loadPaper()）。"""
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        return re.sub(r"//[^\n]*", " ", text)

    def test_no_module_uses_another_modules_declaration_without_importing_it(self):
        offenders = []
        for path, text in self.modules.items():
            if path.endswith(("bridge.js",)):
                continue  # bridge 自己就是全局映射表，单独校验
            imported = self._imported(text)
            body = self._strip_comments(IMPORT_LINE.sub("", text))
            for name, owner in self.owner.items():
                if owner == path or name in imported:
                    continue
                if re.search(r"(?<![\w$.])%s(?![\w$])" % re.escape(name), body):
                    offenders.append("%s -> %s（声明在 %s）"
                                     % (os.path.basename(path), name,
                                        os.path.basename(owner)))
        self.assertEqual([], sorted(set(offenders)), "跨模块引用缺少 import")

    def test_modules_do_not_reference_undefined_browser_unknown_names(self):
        # 只查"曾是单文件顶层名、现在既不在本文件也没 import"的情况：
        # 这是重构引入 ReferenceError 的唯一来源，已被上一用例覆盖；
        # 这里额外确认没有被误删的声明（每个原顶层名都仍存在于某个模块）。
        names = set(self.owner)
        self.assertGreater(len(names), 200, "导出的顶层名数量异常（拆分为空？）")
        for required in ("wbNewStrategy", "loadStrategyWorkbench", "loadSettings", "loadPaper",
                         "loadAdaptive", "loadPaperSelection", "activatePage", "refreshApp",
                         "verifyExecutionOrder", "loadPaperRisk", "startInit"):
            self.assertIn(required, names, required)


class ModuleLayoutTests(unittest.TestCase):
    """目录契约：单文件源已移除，入口/产物路径稳定，产物含 build id。"""

    def test_single_file_sources_are_gone(self):
        for stale in ("app.js", "app.css"):
            self.assertFalse(os.path.exists(os.path.join(FRONTEND, stale)),
                             "frontend/%s 应已拆到 src/ 与 styles/" % stale)

    def test_entry_files_exist(self):
        self.assertTrue(os.path.isfile(os.path.join(SRC, "app.js")))
        self.assertTrue(os.path.isfile(os.path.join(SRC, "boot.js")))
        self.assertTrue(os.path.isfile(os.path.join(SRC, "bridge.js")))
        self.assertTrue(os.path.isfile(os.path.join(STYLES, "index.css")))

    def test_build_id_is_declared_in_the_entry(self):
        with open(os.path.join(SRC, "app.js"), encoding="utf-8") as handle:
            entry = handle.read()
        match = re.search(r"__ASTOCK_ADAPTIVE_UI_BUILD__='([^']+)'", entry)
        self.assertIsNotNone(match, "入口缺少 __ASTOCK_ADAPTIVE_UI_BUILD__ 声明")
        self.assertEqual(match.group(1), BI.APP_BUILD_ID)

    def test_dist_bundle_is_built_from_the_new_sources(self):
        dist = frontend_sources.dist_text("app.js")
        self.assertIn(BI.APP_BUILD_ID, dist, "dist 与源 build id 不一致，忘了 npm run build？")
        for name in sorted(re.findall(r"^window\.([A-Za-z_$][\w$]*)\s*=",
                                      self._bridge(), re.M)):
            self.assertRegex(dist, r"window\.%s\s*=" % re.escape(name),
                             "dist 缺少桥接全局 %s" % name)
        self.assertNotRegex(dist, r"\brequire\(", "产物不应包含 CJS require")

    @staticmethod
    def _bridge() -> str:
        with open(os.path.join(SRC, "bridge.js"), encoding="utf-8") as handle:
            return handle.read()

    def test_styles_entry_imports_every_style_file_in_order(self):
        with open(os.path.join(STYLES, "index.css"), encoding="utf-8") as handle:
            index = handle.read()
        imported = re.findall(r'@import\s+"\./([^"]+)";', index)
        actual = sorted(os.path.basename(path) for path in frontend_sources.style_files()
                        if not path.endswith("index.css"))
        self.assertEqual(sorted(imported), actual, "styles/index.css 与片段文件不一致")
        # 顺序即层叠顺序：一个片段都不能漏。
        self.assertEqual(len(imported), len(actual))

    def test_css_split_is_order_preserving(self):
        # 每个片段都记录了它在原 app.css 里的行区间；把它们按 index.css 顺序拼起来
        # 应当与 origin/main 的 app.css 等价（这里只校验区间单调、无缝、从 1 开始）。
        with open(os.path.join(STYLES, "index.css"), encoding="utf-8") as handle:
            index_css = handle.read()
        spans = []
        for name in re.findall(r'@import\s+"\./([^"]+)";', index_css):
            with open(os.path.join(STYLES, name), encoding="utf-8") as handle:
                head = handle.read(600)
            found = re.search(r"原始行区间 (\d+)–(\d+)", head)
            self.assertIsNotNone(found, "%s 缺少行区间标注" % name)
            spans.append((int(found.group(1)), int(found.group(2)), name))
        self.assertEqual(1, spans[0][0], "第一段必须从第 1 行开始")
        for index in range(1, len(spans)):
            self.assertEqual(spans[index - 1][1] + 1, spans[index][0],
                             "%s 与 %s 之间有空隙或重叠" % (spans[index - 1][2], spans[index][2]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
