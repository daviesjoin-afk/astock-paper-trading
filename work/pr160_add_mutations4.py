# -*- coding: utf-8 -*-
"""补进第三轮 review 的两条变异（M-R10/R11）。幂等。

* **M-R10**：冲突明细退回"只有 field/providers/values"（session / effective_at /
  observed_at 不进指纹 → 冲突挪 session 被当成幂等重放）
* **M-R11**：不拒绝 autocommit 连接（事实与审计无法原子提交）

用法::

    PY=<仓库 venv 的 python>
    $PY work/pr160_add_mutations4.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "work" / "tradability_ingestion_mutation_check.py"

EXTRA_TEST_MODULES = {
    "M-R10": ("test_tradability_backfill",),
    "M-R11": ("test_tradability_backfill",),
}

NEW_MUTATIONS = (
    (
        "M-R10",
        "backend/tradability_ingestion.py",
        "                    json.dumps(c.identity_payload(), sort_keys=True, ensure_ascii=False, default=str)\n",
        "                    json.dumps({\"field\": c.field, \"providers\": list(c.providers),"
        " \"values\": list(c.values)}, sort_keys=True)  # MUTANT M-R10\n",
        "冲突明细退回部分字段（session/effective_at/observed_at 不进指纹）",
    ),
    (
        "M-R11",
        "backend/tradability_ingestion.py",
        "            if self._enforce_explicit_transactions:\n",
        "            if False:  # MUTANT M-R11: 不拒绝 autocommit 连接\n",
        "不拒绝 autocommit 连接（事实与审计无法原子提交）",
    ),
)


def _format_entries() -> str:
    chunks = []
    for name, path, before, after, description in NEW_MUTATIONS:
        chunks.append(
            "    (\n"
            f'        "{name}",\n'
            f"        INGESTION,\n"
            f"        {before!r},\n"
            f"        {after!r},\n"
            f'        "{description}",\n'
            "    ),\n"
        )
    return "".join(chunks)


def main() -> int:
    text = MATRIX.read_text(encoding="utf-8")
    changed = []

    block = "".join(f'    "{key}": {value!r},\n' for key, value in EXTRA_TEST_MODULES.items())
    header = "    \"M-R9\": ('test_tradability_backfill',),\n"
    if block not in text:
        if header not in text:
            print("ERROR: cannot find TEST_MODULES_BY_ID anchor")
            return 1
        text = text.replace(header, header + block, 1)
        changed.append(f"added {len(EXTRA_TEST_MODULES)} entries to TEST_MODULES_BY_ID")

    tail = (
        '        "允许审计连接与归档连接不同（跨库提交，replay identity 查错库）",\n'
        "    ),\n"
        ")\n"
    )
    new_block = _format_entries()
    if new_block.strip() not in text:
        if tail not in text:
            print("ERROR: cannot find MUTATIONS tail")
            return 1
        text = text.replace(
            tail,
            '        "允许审计连接与归档连接不同（跨库提交，replay identity 查错库）",\n'
            "    ),\n" + new_block + ")\n",
            1,
        )
        changed.append(f"added {len(NEW_MUTATIONS)} mutations")

    MATRIX.write_text(text, encoding="utf-8")
    print(f"matrix updated: {MATRIX}")
    for item in changed:
        print(f"  - {item}")
    if not changed:
        print("  (no changes; already up to date)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
