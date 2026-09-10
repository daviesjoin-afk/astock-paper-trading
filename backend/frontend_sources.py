# -*- coding: utf-8 -*-
"""PR-55：前端源码读取助手（测试用）。

前端源已从单文件 `frontend/app.js` 拆分到：

- `frontend/src/**/*.js`   ESM 模块（入口 `src/app.js` = build id + 兼容桥 + boot）
- `frontend/styles/**/*.css`  CSS 片段（入口 `styles/index.css` 按原顺序 @import）
- 产物仍是 `frontend/dist/app.js`、`frontend/dist/app.css`（index.html 只加载这两个）

需要"整份前端源码"的测试统一走这里，避免各自硬编码路径在重构后再断一次。
"""
from __future__ import annotations

import os

BACKEND = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(os.path.dirname(BACKEND), "frontend")


def _walk(base: str, suffix: str) -> list[str]:
    found: list[str] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in {"node_modules", "__pycache__"}]
        for name in sorted(files):
            if name.endswith(suffix):
                found.append(os.path.join(root, name))
    return sorted(found)


def source_files() -> list[str]:
    """全部 JS 模块（含入口、bridge、boot），按路径排序。"""
    return _walk(os.path.join(FRONTEND, "src"), ".js")


def style_files() -> list[str]:
    """全部 CSS 片段（含 styles/index.css 入口）。"""
    return _walk(os.path.join(FRONTEND, "styles"), ".css")


def _concat(paths: list[str]) -> str:
    chunks = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            chunks.append(handle.read())
    return "\n".join(chunks)


def source_text() -> str:
    """拼接后的前端 JS 源码（用于"包含/不包含某实现"的断言）。"""
    return _concat(source_files())


def style_text() -> str:
    """拼接后的前端 CSS 源码。"""
    return _concat(style_files())


def index_html() -> str:
    with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as handle:
        return handle.read()


def dist_text(name: str) -> str:
    with open(os.path.join(FRONTEND, "dist", name), encoding="utf-8") as handle:
        return handle.read()
