# -*- coding: utf-8 -*-
"""PR-53：仓库卫生契约。

四类断言，都是"以后别再犯"的护栏：

1. ``backend/`` 生产源码里不允许出现**没有说明**的 0 字节 ``.py``
   （唯一例外是显式 allowlist——当前为空；将来真需要包标记文件时在这里登记）。
2. 被本轮删除的废弃产物（0 字节 ``modlens.py``、手工 demo replay、
   ``paper_trading`` 兼容包装器）**不得回流**，也不得再被任何仓库内文件引用。
3. **本地 import 必须可解析**：生产模块里出现的每一个"看起来是本仓库模块"的
   import，都要能对应到真实的 ``backend/<模块>.py``（或包）。
   放在 ``try/except ImportError`` 里的可选依赖不算 broken。
4. **文档内部链接无断链**：``docs/``、根目录 markdown 里的相对链接必须指向真实文件。

这些断言只读仓库，不依赖网络；删除/重命名文件时它们会先炸，而不是等 CI 冒烟。
"""
from __future__ import annotations

import ast
import os
import re
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BACKEND)
sys.path.insert(0, BACKEND)

# 运行时镜像（Dockerfile）只 COPY backend/、frontend/、deploy/ 与依赖清单，
# 不带 docs/ 与根目录 markdown。CI 的 tests(3.11/3.12) 作业在完整检出上跑，
# 这些断言在那边照常生效；docker-smoke 的镜像里则跳过，而不是假装通过。
FULL_TREE = os.path.isdir(os.path.join(ROOT, "docs")) and os.path.isfile(
    os.path.join(ROOT, "ARCHITECTURE.md"))
requires_full_tree = unittest.skipUnless(
    FULL_TREE, "当前布局是运行时镜像（只含 backend/frontend/deploy），仓库级文档断言不适用")
requires_requirements = unittest.skipUnless(
    os.path.isfile(os.path.join(ROOT, "requirements.txt")), "缺少 requirements 清单")

# 允许保留的 0 字节源码（显式登记才放过；当前没有）。
ZERO_BYTE_ALLOWLIST: frozenset[str] = frozenset()

# 一律不得回流的废弃产物（本 PR 删除；回来说明有人又把它加回来了）。
REMOVED_ARTIFACTS = (
    "backend/modlens.py",                      # 0 字节占位，真实实现在 modlens_bridge.py
    "backend/demo_strategy_replay.py",         # 手工 replay，被生产路径 golden replay 取代
    "backend/test_demo_strategy_replay.py",
    "backend/paper_trading_compat.py",         # 死包装器，docstring 与真实架构不符
    "backend/paper_trading_wrapper.py",
    "backend/paper_trading_compat",            # 多种写法一起挡
    "backend/paper_trading_wrapper",
    "demo_strategy_replay",
    "paper_trading_compat",
    "paper_trading_wrapper",
)

# 依赖搜索范围（与 PR 描述里列出的位置一致）。
SCAN_DIRS = ("backend", "frontend", ".github", "deploy", "docs", "scripts")
SCAN_ROOT_FILES = (
    "README.md", "README_EN.md", "ARCHITECTURE.md", "SECURITY.md", "CONTRIBUTING.md",
    "CHANGELOG.md", "Dockerfile", "docker-compose.yml", "docker-compose.server.yml",
    "pyproject.toml", "requirements.txt", "requirements.lock",
    "start.sh", "start.bat", "start.ps1",
)
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".workbuddy", "dist"}
SCAN_SUFFIXES = (".py", ".js", ".html", ".css", ".md", ".sh", ".bat", ".ps1",
                 ".yml", ".yaml", ".toml", ".txt", ".cfg", ".ini")


def _walk_scan_scope():
    for name in SCAN_DIRS:
        base = os.path.join(ROOT, name)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                if filename.endswith(SCAN_SUFFIXES):
                    yield os.path.join(dirpath, filename)
    for name in SCAN_ROOT_FILES:
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            yield path


def _production_sources():
    for dirpath, dirnames, filenames in os.walk(BACKEND):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in sorted(filenames):
            if filename.endswith(".py") and not filename.startswith("test_"):
                yield os.path.join(dirpath, filename)


class ZeroByteSourceTests(unittest.TestCase):
    """生产源码里不允许无说明的空文件（空 .py 只会静默遮蔽真实模块）。"""

    def test_no_unexplained_empty_python_files(self):
        offenders = []
        for path in _production_sources():
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if os.path.getsize(path) == 0 and rel not in ZERO_BYTE_ALLOWLIST:
                offenders.append(rel)
        self.assertEqual(
            [], offenders,
            "出现 0 字节生产源码；删除，或在 ZERO_BYTE_ALLOWLIST 里显式登记原因",
        )


class RemovedArtifactTests(unittest.TestCase):
    """被删除的废弃产物不得回流，也不得再被引用。"""

    def test_removed_files_stay_removed(self):
        for rel in ("backend/modlens.py", "backend/demo_strategy_replay.py",
                    "backend/test_demo_strategy_replay.py",
                    "backend/paper_trading_compat.py",
                    "backend/paper_trading_wrapper.py"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, rel)),
                             "%s 已被 PR-53 删除，不要再加回来" % rel)

    def test_no_references_to_removed_artifacts(self):
        needles = tuple(REMOVED_ARTIFACTS)
        hits = []
        for path in _walk_scan_scope():
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if rel == "backend/test_repository_hygiene.py":
                continue  # 本文件自身持有名单
            try:
                with open(path, encoding="utf-8", errors="ignore") as handle:
                    text = handle.read()
            except OSError:  # pragma: no cover - 读取失败不该让卫生测试崩
                continue
            for needle in needles:
                if needle in text:
                    hits.append("%s -> %s" % (rel, needle))
        self.assertEqual([], hits, "废弃产物仍被引用")

    def test_real_modlens_implementation_is_present(self):
        # 删的是 0 字节占位，真实实现必须还在（否则就是真删了功能）。
        self.assertTrue(os.path.isfile(os.path.join(BACKEND, "modlens_bridge.py")))
        self.assertGreater(os.path.getsize(os.path.join(BACKEND, "modlens_bridge.py")), 0)


@requires_requirements
class LocalImportTests(unittest.TestCase):
    """生产模块的 import 必须是：本地模块、标准库，或**已声明的依赖**。

    为什么不用 ``importlib.util.find_spec`` 判断：多个测试会
    ``sys.modules.setdefault("requests", MagicMock())``，被 stub 过的名字
    ``__spec__`` 为空，``find_spec`` 会抛 ``ValueError``；用"环境是否装了这个包"
    当判据还会让结果随本机/镜像不同而漂移。改成读 ``requirements.txt`` /
    ``requirements.lock`` 的静态声明，结论只取决于仓库内容。
    """

    # 模块名与发行版名不一致的少数情况（module -> distribution）。
    MODULE_ALIASES = {
        "chinese_calendar": "chinese-calendar",
        "PIL": "pillow",
        "yaml": "pyyaml",
        "dateutil": "python-dateutil",
        "dotenv": "python-dotenv",
    }

    @classmethod
    def _declared_distributions(cls):
        names = set()
        for rel in ("requirements.txt", "requirements.lock"):
            path = os.path.join(ROOT, rel)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.split("#", 1)[0].strip()
                    if not line or line.startswith("-"):
                        continue
                    match = re.match(r"^([A-Za-z0-9_.\-]+)", line)
                    if match:
                        names.add(match.group(1).lower().replace("_", "-"))
        return names

    @staticmethod
    def _imports_with_optional_flag(path):
        """返回 [(顶层模块名, 是否处于 try/except 内)]。"""
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        guarded = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            texts = [ast.dump(h.type) if h.type is not None else "" for h in node.handlers]
            if any("ImportError" in t or "Exception" in t for t in texts):
                for child in node.body:
                    for sub in ast.walk(child):
                        guarded.add(id(sub))
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.append((alias.name.split(".")[0], id(node) in guarded))
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                found.append((node.module.split(".")[0], id(node) in guarded))
        return found

    def _local_modules(self):
        modules = {name[:-3] for name in os.listdir(BACKEND) if name.endswith(".py")}
        modules |= {
            name for name in os.listdir(BACKEND)
            if os.path.isdir(os.path.join(BACKEND, name))
            and os.path.isfile(os.path.join(BACKEND, name, "__init__.py"))
        }
        return modules

    def test_imports_are_local_stdlib_or_declared(self):
        local = self._local_modules()
        declared = self._declared_distributions()
        undeclared = []
        for path in _production_sources():
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            for name, optional in self._imports_with_optional_flag(path):
                if optional or name in local or name in sys.stdlib_module_names:
                    continue
                key = self.MODULE_ALIASES.get(name, name).lower().replace("_", "-")
                if key in declared:
                    continue
                undeclared.append("%s -> import %s（未在 requirements 中声明）" % (rel, name))
        self.assertEqual([], sorted(set(undeclared)), "存在未声明的第三方 import")

    def test_every_production_module_parses(self):
        broken = []
        for path in _production_sources():
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            try:
                with open(path, encoding="utf-8") as handle:
                    ast.parse(handle.read(), filename=path)
            except SyntaxError as exc:
                broken.append("%s: %s" % (rel, exc))
        self.assertEqual([], broken, "生产源码存在语法错误")


@requires_full_tree
class DocumentationLinkTests(unittest.TestCase):
    """markdown 相对链接必须指向真实文件（发布说明等正式文档一并纳入）。"""

    LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")

    @classmethod
    def _markdown_files(cls):
        for base in ("docs", "."):
            root = os.path.join(ROOT, base)
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                if dirpath != ROOT and base == ".":
                    dirnames[:] = []  # 根目录只看顶层 markdown
                for filename in filenames:
                    if filename.endswith(".md"):
                        yield os.path.join(dirpath, filename)

    def test_relative_links_resolve(self):
        broken = []
        for path in self._markdown_files():
            rel_doc = os.path.relpath(path, ROOT).replace(os.sep, "/")
            with open(path, encoding="utf-8") as handle:
                in_fence = False
                for lineno, line in enumerate(handle, start=1):
                    if line.lstrip().startswith("```"):
                        in_fence = not in_fence
                        continue
                    if in_fence:
                        continue
                    for target in self.LINK.findall(line):
                        if target.startswith(("http://", "https://", "mailto:", "#")):
                            continue
                        clean = target.split("#", 1)[0]
                        if not clean:
                            continue
                        if not os.path.exists(os.path.normpath(os.path.join(os.path.dirname(path), clean))):
                            broken.append("%s:%d -> %s" % (rel_doc, lineno, target))
        self.assertEqual([], broken, "文档相对链接指向不存在的文件")

    def test_release_notes_and_current_docs_are_kept(self):
        # 明确不许删的正式文档必须仍在原位。
        for rel in ("docs/RELEASE-v1.1.0.md", "docs/RELEASE-v1.2.0.md",
                    "docs/RUNBOOK.md", "docs/TEST_MATRIX.md", "docs/SETTINGS_PRD.md",
                    "docs/DEMO.md", "ARCHITECTURE.md", "docs/PRD-architecture-hardening.md"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)

    def test_layout_and_archive_docs_exist(self):
        layout = os.path.join(ROOT, "docs/REPOSITORY_LAYOUT.md")
        self.assertTrue(os.path.isfile(layout))
        with open(layout, encoding="utf-8") as handle:
            text = handle.read()
        for section in ("api_*.py", "*_runner.py", "backend/", "frontend/dist",
                        "docs/archive"):
            self.assertIn(section, text, section)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "docs/archive/README.md")))

    def test_superseded_docs_are_archived_not_deleted(self):
        for rel in ("docs/archive/HANDOFF-architecture-hardening-2026-09-05.md",
                    "docs/archive/REPOSITORY-REVIEW-2026-09-05.md",
                    "docs/archive/architecture-hardening-plan.md"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
