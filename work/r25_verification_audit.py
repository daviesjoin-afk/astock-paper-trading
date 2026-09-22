# -*- coding: utf-8 -*-
"""R25 pre-flight —— verification 语义误用审计（只读）。

规格 §3：修改 production 前先分类所有 verification 消费者：
  A. 只展示 "policy 已通过"
  B. 真正要求 cross-source verification
  C. 测试/文档
  D. 语义不确定

本脚本只做证据收集，不修改任何文件。
"""
from __future__ import annotations

import pathlib
import re

PATTERNS = {
    "R24_verification_attr": re.compile(r"\.verification\b"),
    "R24_verification_method": re.compile(r"verification_method"),
    "R24_verified_const": re.compile(r"VERIFICATION_VERIFIED"),
    "R24_cross_source_helper": re.compile(r"is_cross_source_verified"),
    "R24_cross_map": re.compile(r"verification_from_cross_status"),
    "LEGACY_cross_checked_str": re.compile(r"cross_source_checked"),
    "LEGACY_quote_validation": re.compile(r"quote_validation"),
}

BACKEND = pathlib.Path("backend")
FRONTEND = pathlib.Path("frontend/src")


def scan():
    rows = []
    for root in (BACKEND, FRONTEND):
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".py", ".js"}:
                continue
            if path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                hits = [name for name, pat in PATTERNS.items() if pat.search(line)]
                if hits:
                    rows.append((str(path).replace("\\", "/"), lineno, ",".join(hits), line.strip()))
    return rows


if __name__ == "__main__":
    rows = scan()
    print("total lines referencing verification semantics:", len(rows))
    print()
    width = max((len(r[0]) for r in rows), default=0)
    for path, lineno, hits, line in rows:
        print(f"{path:<{width}}  {lineno:>6}  [{hits}]  {line[:110]}")
