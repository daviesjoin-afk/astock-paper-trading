# -*- coding: utf-8 -*-
"""重建 M-R8 的 anchor：conflicts payload 现在是多行 ``sorted(...)`` 块，替换成
``[]`` 必须整体替换，否则注入后语法不成立（``IMPORT-FAILED`` = 假杀）。幂等。

用法::

    PY=<仓库 venv 的 python>
    $PY work/pr160_rebuild_mr8.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "work" / "tradability_ingestion_mutation_check.py"
SOURCE = ROOT / "backend" / "tradability_ingestion.py"

START = '            "conflicts": sorted(\n'
END = "            ),\n"
DESCRIPTION = "conflict 明细不进内容身份（冲突集合变化被当成幂等重放）"
MUTANT = '            "conflicts": [],  # MUTANT M-R8: conflict 明细不进指纹\n'


def _block() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    start = text.index(START)
    end = text.index(END, start) + len(END)
    return text[start:end]


def _as_literal_lines(value: str, indent: str = "        ") -> list:
    parts = []
    for line in value.splitlines():
        escaped = line.replace("\\", "\\\\").replace("'", "\\'")
        parts.append(f"{indent}'{escaped}\\n'")
    return parts


def _entry(before: str, after: str) -> str:
    before_lines = _as_literal_lines(before)
    after_lines = _as_literal_lines(after)
    before_lines[-1] += ","
    after_lines[-1] += ","
    return (
        '        "M-R8",\n'
        "        INGESTION,\n"
        + "\n".join(before_lines)
        + "\n"
        + "\n".join(after_lines)
        + "\n"
        + f'        "{DESCRIPTION}",\n'
        + "    ),\n"
    )


def main() -> int:
    block = _block()
    text = MATRIX.read_text(encoding="utf-8")
    marker = '        "M-R8",\n        INGESTION,\n'
    start = text.index(marker)
    end_marker = f'        "{DESCRIPTION}",\n    ),\n'
    end = text.index(end_marker, start) + len(end_marker)
    text = text[:start] + _entry(block, MUTANT) + text[end:]
    MATRIX.write_text(text, encoding="utf-8")
    print("M-R8 rebuilt from real source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
