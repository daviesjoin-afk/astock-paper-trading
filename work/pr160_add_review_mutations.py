# -*- coding: utf-8 -*-
"""把第二轮 review 修复对应的变异条目补进矩阵，并重指被改动源码影响的 anchor。

新增（对应 3 个 P1 + 3 个 P2）：

* M-R5 无持久审计时仍接受 replay-capable 写入
* M-R6 provider 结果分布不进指纹（error→unknown 被当成幂等重放）
* M-SH9 生产 verdict 的 side 与比对 side 不一致时仍接受
* M-SH10 用"后来是否摄取过"决定归档分类（未来事实改写历史比对）
* M-SH11 卖出方向伪造 entry_session（造出假 T+1 分歧）——落在 CLI 文件

重指：M-SH1 / M-SH2 的 anchor 因 ``_archive_side`` 结构变化而失效。

脚本幂等。

用法::

    PY=<仓库 venv 的 python>
    $PY work/pr160_add_review_mutations.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "work" / "tradability_ingestion_mutation_check.py"
CLI = "work/tradability_shadow_validation.py"

INGESTION = "backend/tradability_ingestion.py"
SHADOW = "backend/tradability_shadow.py"

EXTRA_TEST_MODULES = {
    "M-R5": ("test_tradability_backfill",),
    "M-R6": ("test_tradability_backfill",),
    "M-SH9": ("test_tradability_shadow",),
    "M-SH10": ("test_tradability_shadow",),
    "M-SH11": ("test_tradability_shadow_architecture_guard",),
}

EXTRA_IMPORT_MODULES = {
    "M-SH11": "tradability_shadow",
}

NEW_MUTATIONS = (
    (
        "M-R5",
        INGESTION,
        "        if self._audit_conn is None:\n"
        '            raise IngestionError(\n'
        '                "write=True 需要持久审计存储（audit_conn）才能校验 run_id 的 replay "\n'
        '                "identity；缺少它时既读不到既有指纹也无法记录本次指纹，"\n'
        '                "同 run_id 的 divergent replay 将无法被拒绝"\n'
        "            )\n",
        "        if self._audit_conn is None:  # MUTANT M-R5: 无持久审计也放行\n"
        "            return\n",
        "无持久审计存储时仍接受 replay-capable 写入（divergent replay 不再可挡）",
    ),
    (
        "M-R6",
        INGESTION,
        '            "outcomes": dict(sorted((outcomes or {}).items())),\n',
        "        }\n"
        "        payload.pop(\"outcomes\", None)\n"
        "        payload.update({  # MUTANT M-R6: 结果分布不进指纹\n",
        "provider 结果分布不进指纹（error→unknown 被当成幂等重放）",
    ),
    (
        "M-SH9",
        SHADOW,
        "            or side_mismatch\n",
        "            # MUTANT M-SH9: 不检查生产 verdict 的 side\n",
        "生产 verdict 的 side 与比对 side 不一致时仍接受（标签错误的一致/分歧）",
    ),
    (
        "M-SH10",
        SHADOW,
        "        elif archive[\"archive_state\"] == ShadowStatus.ARCHIVE_UNPROVABLE.value:\n"
        "            status = ShadowStatus.ARCHIVE_UNPROVABLE\n",
        "        elif archive[\"archive_state\"] == ShadowStatus.ARCHIVE_UNPROVABLE.value:\n"
        "            status = ShadowStatus.ARCHIVE_MISSING  # MUTANT M-SH10\n",
        "归档不可证明被降级成 missing（缺证据与不可证明混淆）",
    ),
    (
        "M-SH11",
        CLI,
        "    return ST.exit_tradability(evidence, code=code, exit_session=session)\n",
        "    return ST.exit_tradability(  # MUTANT M-SH11: 伪造同日 entry_session\n"
        "        evidence, code=code, exit_session=session, entry_session=session\n"
        "    )\n",
        "卖出方向伪造同日 entry_session（造出假的 T+1 分歧）",
    ),
)


def _format_entries() -> str:
    consts = {INGESTION: "INGESTION", SHADOW: "SHADOW"}
    chunks = []
    for name, path, before, after, description in NEW_MUTATIONS:
        const = consts.get(path) or "SHADOW_CLI"
        chunks.append(
            "    (\n"
            f'        "{name}",\n'
            f"        {const},\n"
            f"        {before!r},\n"
            f"        {after!r},\n"
            f'        "{description}",\n'
            "    ),\n"
        )
    return "".join(chunks)


# 重指 M-F2 / M-F3：`_run_fingerprint` 的调用点因新增 outcomes 参数而换行，
# 被替换的源码片段本身也变了。
REPOINTS = ()


def main() -> int:
    text = MATRIX.read_text(encoding="utf-8")
    changed = []

    for old, new in REPOINTS:
        if new in text:
            continue
        if old not in text:
            print(f"ERROR: cannot find repoint anchor: {old[:90]!r}")
            return 1
        text = text.replace(old, new, 1)
        changed.append("repointed an anchor")

    if 'SHADOW_CLI = ' not in text:
        marker = 'SHADOW = "backend/tradability_shadow.py"\n'
        if marker not in text:
            print("ERROR: cannot find SHADOW constant")
            return 1
        text = text.replace(marker, marker + f'SHADOW_CLI = "{CLI}"\n', 1)
        changed.append("added SHADOW_CLI constant")

    for mapping, extras, header in (
        ("TEST_MODULES_BY_ID", EXTRA_TEST_MODULES,
         "    \"M-SH8\": ('test_tradability_shadow',),\n"),
        ("IMPORT_MODULE_BY_ID", EXTRA_IMPORT_MODULES,
         "    \"M-SH6\": 'tradability_shadow',\n"),
    ):
        block = "".join(f'    "{key}": {value!r},\n' for key, value in extras.items())
        if block.strip() and block not in text:
            if header not in text:
                print(f"ERROR: cannot find anchor in {mapping}")
                return 1
            text = text.replace(header, header + block, 1)
            changed.append(f"added {len(extras)} entries to {mapping}")

    tail_marker = (
        '        "相同 comparison identity 允许 conflicting overwrite（last-write-wins）",\n'
        "    ),\n"
        ")\n"
    )
    new_block = _format_entries()
    if new_block.strip() not in text:
        if tail_marker not in text:
            print("ERROR: cannot find MUTATIONS tail")
            return 1
        text = text.replace(
            tail_marker,
            '        "相同 comparison identity 允许 conflicting overwrite（last-write-wins）",\n'
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
