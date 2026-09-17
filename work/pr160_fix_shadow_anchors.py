# -*- coding: utf-8 -*-
"""修正 M-SH1 / M-SH2 的 anchor：源码里的归档状态分支是 ``elif`` 链，不是 ``if``。

anchor 是精确源码文本，写成 ``if`` 就匹配不到（矩阵会在那两条上 abort）。幂等。

用法::

    PY=<仓库 venv 的 python>   # 本地绝对路径不进仓库（敏感扫描）
    $PY work/pr160_fix_shadow_anchors.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "work" / "tradability_ingestion_mutation_check.py"

# 目标文本在矩阵文件里是**单引号字符串**，其中的双引号不需要转义。
_UNKNOWN = 'if archive["archive_state"] == ShadowStatus.ARCHIVE_UNKNOWN.value:'
_UNPROVABLE = 'if archive["archive_state"] == ShadowStatus.ARCHIVE_UNPROVABLE.value:'

PAIRS = (
    (_UNKNOWN, _UNKNOWN.replace("if ", "elif ", 1)),
    (_UNPROVABLE, _UNPROVABLE.replace("if ", "elif ", 1)),
)


def main() -> int:
    text = MATRIX.read_text(encoding="utf-8")
    changes = 0
    for old, new in PAIRS:
        count = text.count(old)
        if count == 0:
            continue
        text = text.replace(old, new)
        changes += count
    MATRIX.write_text(text, encoding="utf-8")
    print(f"anchor occurrences fixed: {changes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
