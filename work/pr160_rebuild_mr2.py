# -*- coding: utf-8 -*-
"""重建 M-R2 条目（before/after 片段用**真实源码文本**生成，并补上结尾逗号）。幂等。

M-R2 的语义是"先写 archive、后检查 run_id 冲突"。第三轮在两者之间插入了 autocommit
检查，因此原来的两行紧邻片段不再存在。

用法::

    PY=<仓库 venv 的 python>
    $PY work/pr160_rebuild_mr2.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "work" / "tradability_ingestion_mutation_check.py"
SOURCE = ROOT / "backend" / "tradability_ingestion.py"

START = "            self._assert_replay_identity(run_id, run_fingerprint)\n"
END = "                    skipped_records += 1  # 幂等重放：唯一键命中，逻辑状态不变。\n"
DESCRIPTION = "divergent replay 写 archive 但不更新 audit（先写事实再检查 run_id 冲突）"


def _block() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    start = text.index(START)
    end = text.index(END, start) + len(END)
    return text[start:end]


def _mutant(block: str) -> str:
    """把写循环挪到校验之前（divergent replay 会先写 archive）。"""
    lines = block.splitlines(keepends=True)
    assert_head = lines[0]
    loop_start = next(
        i for i, line in enumerate(lines) if "for evidence in normalized_evidence:" in line
    )
    return "".join(lines[loop_start:]) + assert_head


def _as_literal_lines(value: str, indent: str = "        ") -> list:
    """逐行生成矩阵文件里的单引号字面量（``splitlines`` 不带换行，再显式补 ``\\n``）。"""
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
        '        "M-R2",\n'
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
    mutant = _mutant(block)
    if mutant == block:
        print("ERROR: mutant equals original")
        return 1

    text = MATRIX.read_text(encoding="utf-8")
    marker = '        "M-R2",\n        INGESTION,\n'
    start = text.index(marker)
    end_marker = f'        "{DESCRIPTION}",\n    ),\n'
    end = text.index(end_marker, start) + len(end_marker)

    text = text[:start] + _entry(block, mutant) + text[end:]
    MATRIX.write_text(text, encoding="utf-8")
    print("M-R2 rebuilt from real source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
