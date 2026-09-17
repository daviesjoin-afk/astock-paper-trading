# -*- coding: utf-8 -*-
"""补进第二轮 review 的三条变异（M-R7/R8/R9）。幂等。

* **M-R7**：unprovable 明细不进指纹（observed_kind 翻转被当成幂等重放）
* **M-R8**：conflict 明细不进指纹
* **M-R9**：审计连接允许与归档连接不同（跨库提交，replay identity 查错库）

用法::

    PY=<仓库 venv 的 python>
    $PY work/pr160_add_mutations3.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "work" / "tradability_ingestion_mutation_check.py"

INGESTION = "backend/tradability_ingestion.py"

EXTRA_TEST_MODULES = {
    "M-R7": ("test_tradability_backfill",),
    "M-R8": ("test_tradability_backfill",),
    "M-R9": ("test_tradability_backfill",),
}

NEW_MUTATIONS = (
    (
        "M-R7",
        INGESTION,
        '            "unprovable": sorted(str(pair) for pair in (unprovable or ())),\n',
        '            "unprovable": [],  # MUTANT M-R7: unprovable 明细不进指纹\n',
        "unprovable 明细不进内容身份（observed_kind 翻转被当成幂等重放）",
    ),
    (
        "M-R8",
        INGESTION,
        '            "conflicts": [\n'
        '                {"field": c.field, "providers": list(c.providers), "values": list(c.values)}\n'
        "                for c in (conflicts or ())\n"
        "            ],\n",
        '            "conflicts": [],  # MUTANT M-R8: conflict 明细不进指纹\n',
        "conflict 明细不进内容身份（冲突集合变化被当成幂等重放）",
    ),
    (
        "M-R9",
        INGESTION,
        "        if audit_conn is not None and audit_conn is not repository.connection:\n"
        "            raise IngestionError(\n"
        '                "audit_conn 必须与 repository 使用同一个连接：replay identity 的查询与"\n'
        '                "事实写入必须在同一个库、同一个事务内，否则事实与审计无法原子提交"\n'
        "            )\n",
        "        if False:  # MUTANT M-R9: 允许跨库审计连接\n"
        "            pass\n",
        "允许审计连接与归档连接不同（跨库提交，replay identity 查错库）",
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
    header = "    \"M-SH11\": ('test_tradability_shadow_architecture_guard',),\n"
    if block not in text:
        if header not in text:
            print("ERROR: cannot find TEST_MODULES_BY_ID anchor")
            return 1
        text = text.replace(header, header + block, 1)
        changed.append(f"added {len(EXTRA_TEST_MODULES)} entries to TEST_MODULES_BY_ID")

    tail = (
        '        "卖出方向伪造同日 entry_session（造出假的 T+1 分歧）",\n'
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
            '        "卖出方向伪造同日 entry_session（造出假的 T+1 分歧）",\n'
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
