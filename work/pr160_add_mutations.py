# -*- coding: utf-8 -*-
"""把 PR #160 新增的变异条目 + 修好的 anchor 补丁进既有矩阵文件。

既有矩阵文件（``work/tradability_ingestion_mutation_check.py``）在两个地方需要更新：

1. **anchor 失效**：M-F1 / M-S1 的 anchor 指的是被本 PR 改动的源码片段（persistence
   循环搬到了 replay 校验之后；``resolve_sessions`` 增加了范围解析分支）。anchor 是
   精确源码文本，改了被守卫的代码就必须在同一个 commit 里重新指向它，否则矩阵会在
   那一条上直接 abort。
2. **新增条目**：Phase A 的三个 P2 + Phase B 的 Shadow 契约各需要自己的变异。

脚本是幂等的：重复运行不会重复插入。

用法::

    PY=<仓库 venv 的 python>   # 本地绝对路径不进仓库（敏感扫描）
    $PY work/pr160_add_mutations.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "work" / "tradability_ingestion_mutation_check.py"

# ── 1. 重指向失效的 anchor ────────────────────────────────────────────────

M_F1_OLD_ANCHOR = (
    '        "                normalized_records += 1\\n"\n'
    '        "                normalized_evidence.append(evidence)\\n"\n'
    '        "                if write:\\n",\n'
)
M_F1_NEW_ANCHOR = (
    '        "                normalized_records += 1\\n"\n'
    '        "                normalized_evidence.append(evidence)\\n",\n'
)
M_F1_OLD_AFTER = (
    '        "                normalized_records += 1\\n"\n'
    '        "                if write:\\n"\n'
    '        "                    normalized_evidence.append(evidence)\\n"\n'
    '        "                if write:\\n",\n'
)
# 新形态：把"收集 normalized evidence"改成"只在 write 时收集"，同样让 dry-run 的
# fingerprint 漂移（缺 evidence_fingerprints）。变异体必须仍然可导入。
M_F1_NEW_AFTER = (
    '        "                normalized_records += 1\\n"\n'
    '        "                if write:\\n"\n'
    '        "                    normalized_evidence.append(evidence)\\n",\n'
)

M_S1_OLD_ANCHOR = (
    '        "    if calendar is None:\\n"\n'
    '        "        return sessions_between(first, last)\\n"\n'
    '        "    return sessions_between(first, last, calendar=calendar)\\n",\n'
)
M_S1_NEW_ANCHOR = (
    '        "    if calendar is None:\\n"\n'
    '        "        sessions = sessions_between(first, last)\\n"\n'
    '        "    else:\\n"\n'
    '        "        sessions = sessions_between(first, last, calendar=calendar)\\n",\n'
)
M_S1_OLD_AFTER = (
    '        "    if calendar is None:\\n"\n'
    '        "        return sessions_between(first, last)\\n"\n'
    '        "    _f = _dt.date.fromisoformat(str(first)[:10])  # MUTANT M-S1\\n"\n'
    '        "    _l = _dt.date.fromisoformat(str(last)[:10])\\n"\n'
    '        "    return [(_f + _dt.timedelta(days=i)).isoformat() for i in range((_l - _f).days + 1)]\\n",\n'
)
M_S1_NEW_AFTER = (
    '        "    _f = _dt.date.fromisoformat(str(first)[:10])  # MUTANT M-S1\\n"\n'
    '        "    _l = _dt.date.fromisoformat(str(last)[:10])\\n"\n'
    '        "    sessions = [(_f + _dt.timedelta(days=i)).isoformat() "\n'
    '        "for i in range((_l - _f).days + 1)]\\n",\n'
)

# ── 2. 新增条目 ──────────────────────────────────────────────────────────

INGESTION = "backend/tradability_ingestion.py"
BACKFILL = "backend/tradability_backfill.py"
ARCHIVE = "backend/tradability_archive.py"
SHADOW = "backend/tradability_shadow.py"

BACKFILL_TEST = "test_tradability_backfill"
SHADOW_TEST = "test_tradability_shadow"
SHADOW_GUARD = "test_tradability_shadow_architecture_guard"

# 追加到 TEST_MODULES_BY_ID / IMPORT_MODULE_BY_ID 的条目。
EXTRA_TEST_MODULES = {
    "M-R1": (BACKFILL_TEST,),
    "M-R2": (BACKFILL_TEST,),
    "M-R3": (BACKFILL_TEST,),
    "M-R4": (BACKFILL_TEST,),
    "M-CODE1": (BACKFILL_TEST,),
    "M-CODE2": (BACKFILL_TEST,),
    "M-SESSION1": (BACKFILL_TEST,),
    "M-SESSION2": (BACKFILL_TEST,),
    "M-SH1": (SHADOW_TEST,),
    "M-SH2": (SHADOW_TEST,),
    "M-SH3": (SHADOW_TEST,),
    "M-SH4": (SHADOW_TEST,),
    "M-SH5": (SHADOW_TEST,),
    "M-SH6": (SHADOW_GUARD,),
    "M-SH7": (SHADOW_TEST,),
    "M-SH8": (SHADOW_TEST,),
}

EXTRA_IMPORT_MODULES = {
    "M-SH6": "tradability_shadow",
}

NEW_MUTATIONS = (
    (
        "M-R1",
        INGESTION,
        "            self._assert_replay_identity(run_id, run_fingerprint)\n",
        "            pass  # MUTANT M-R1: allow same run_id with divergent fingerprint\n",
        "允许 same run_id + divergent fingerprint（archive 保存新 revision，audit 仍描述旧 run）",
    ),
    (
        "M-R2",
        INGESTION,
        "            self._assert_replay_identity(run_id, run_fingerprint)\n"
        "            for evidence in normalized_evidence:\n"
        "                if self._repo.save(evidence):\n"
        "                    persisted.append(evidence)\n"
        "                else:\n"
        "                    skipped_records += 1  # 幂等重放：唯一键命中，逻辑状态不变。\n",
        "            for evidence in normalized_evidence:\n"
        "                if self._repo.save(evidence):\n"
        "                    persisted.append(evidence)\n"
        "                else:\n"
        "                    skipped_records += 1\n"
        "            if self._repo.count() < 0:  # MUTANT M-R2: divergent replay 写 archive\n"
        "                self._assert_replay_identity(run_id, run_fingerprint)\n",
        "divergent replay 写 archive 但不更新 audit（先写事实再检查 run_id 冲突）",
    ),
    (
        "M-R3",
        INGESTION,
        '            raise IngestionError(\n'
        '                f"run_id {run_id!r} 的 divergent replay 被拒绝："\n'
        '                f"stored={stored} incoming={run_fingerprint}"\n'
        '            )\n',
        '            self._persist_run(  # MUTANT M-R3: 先落 audit 再拒绝\n'
        '                run_id=run_id, started_at=_now_utc(), cutoff=self._cutoff,\n'
        '                sessions=sessions, status=STATUS_COMPLETED,\n'
        '                requested_codes=1, raw_records=0, normalized_records=0,\n'
        '                persisted_records=0, skipped_records=0, unknown_records=0,\n'
        '                conflict_records=0, unprovable_records=0, error_records=0,\n'
        '                conflicts=[], unprovable=[], run_fingerprint=run_fingerprint,\n'
        '            )\n',
        "divergent replay 拒绝前先写一行 audit（audit 被污染）",
    ),
    (
        "M-R4",
        INGESTION,
        "        if stored is None:\n",
        "        if stored is None or stored:\n",  # 永不进入 fail-closed 分支
        "既有 run 没有 run_fingerprint 时放行（无法证明是同一次重放却接受）",
    ),
    (
        "M-CODE1",
        BACKFILL,
        "        selected = normalize_codes(codes)\n",
        "        selected = sorted(load_listing_records().keys())  # MUTANT M-CODE1\n",
        "explicit empty codes fallback whole universe（--write 下放大成全市场写入）",
    ),
    (
        "M-CODE2",
        BACKFILL,
        "    if invalid:\n"
        "        raise CodeScopeError(\n"
        '            "显式 --codes 含非法代码: " + ", ".join(sorted(set(invalid)))\n'
        "        )\n",
        "    if False:  # MUTANT M-CODE2: 静默丢弃非法代码\n"
        "        pass\n",
        "all-invalid codes 被静默丢弃（操作员以为整批都处理了）",
    ),
    (
        "M-SESSION1",
        BACKFILL,
        "    if not sessions:\n"
        "        raise SessionScopeError(\n"
        '            f"日期范围 {first} → {last} 解析出 0 个交易日，无 session 可回填"\n'
        "        )\n",
        "    if False:  # MUTANT M-SESSION1: 接受零交易日范围\n"
        "        pass\n",
        "empty session range accepted（报 completed 但什么都没做）",
    ),
    (
        "M-SESSION2",
        INGESTION,
        "        if not sessions:\n"
        '            raise IngestionError("ingestion 拒绝空 session scope（0 个交易日）")\n',
        "        if False:  # MUTANT M-SESSION2: 零 session 也照写 audit\n"
        "            pass\n",
        "zero-session write creates an audit row（把自己记成一次完成的摄取）",
    ),
    (
        "M-SH1",
        SHADOW,
        '        if archive["archive_state"] == ShadowStatus.ARCHIVE_UNKNOWN.value:\n'
        "            status = ShadowStatus.ARCHIVE_UNKNOWN\n",
        '        if archive["archive_state"] == ShadowStatus.ARCHIVE_UNKNOWN.value:\n'
        "            status = ShadowStatus.PRODUCTION_BLOCK_ARCHIVE_ALLOW  # MUTANT M-SH1\n",
        "archive unknown 被算成 disagreement（证据缺口进分歧分母）",
    ),
    (
        "M-SH2",
        SHADOW,
        '        if archive["archive_state"] == ShadowStatus.ARCHIVE_UNPROVABLE.value:\n'
        "            status = ShadowStatus.ARCHIVE_UNPROVABLE\n",
        '        if archive["archive_state"] == ShadowStatus.ARCHIVE_UNPROVABLE.value:\n'
        "            status = ShadowStatus.AGREE_ALLOW  # MUTANT M-SH2\n",
        "archive unprovable 被算成 comparable（历史不可证明却进一致率分母）",
    ),
    (
        "M-SH3",
        SHADOW,
        "            agreement_rate=_ratio(agree, comparable),\n",
        "            agreement_rate=_ratio(agree, requested),  # MUTANT M-SH3\n",
        "agreement_rate 用 requested 当分母（把不可比的也当成一致）",
    ),
    (
        "M-SH4",
        ARCHIVE,
        "    verdict = PIT.is_visible_at(evidence.observed_at, decision_time)\n"
        '    if verdict.get("mode") != "strict" or not verdict.get("visible"):\n'
        "        return False\n",
        "    pass  # MUTANT M-SH4: 未来证据可参与历史 comparison\n",
        "future observed evidence 被允许参与历史 comparison（PIT 失效）",
    ),
    (
        "M-SH5",
        ARCHIVE,
        "            if evidence.price_limit_direction == PRICE_LIMIT_DOWN:\n"
        "                return TradabilityReason.OK\n"
        "            return TradabilityReason.BUY_LIMIT_LOCKED\n",
        "            if evidence.price_limit_direction == PRICE_LIMIT_UP:  # MUTANT M-SH5\n"
        "                return TradabilityReason.OK\n"
        "            return TradabilityReason.BUY_LIMIT_LOCKED\n",
        "BUY/SELL 涨跌停方向反转（涨停放行买入）",
    ),
    (
        "M-SH6",
        SHADOW,
        "class ShadowError(ValueError):\n",
        "def allow_order(*args, **kwargs):  # MUTANT M-SH6\n"
        '    """Shadow 长出 authority 入口。"""\n'
        "    raise NotImplementedError\n\n\n"
        "class ShadowError(ValueError):\n",
        "shadow result 暴露 authority 入口（可覆盖 production decision）",
    ),
    (
        "M-SH7",
        SHADOW,
        "        agree = counts[ShadowStatus.AGREE_ALLOW.value] + counts[ShadowStatus.AGREE_BLOCK.value]\n",
        "        agree = sum(counts.values())  # MUTANT M-SH7: 把一切算成一致\n",
        "shadow 汇总把全部结论算成一致（on/off 改变可观测结论）",
    ),
    (
        "M-SH8",
        SHADOW,
        '        if stored == row["content_fingerprint"]:\n'
        '            return "identical"\n'
        "        raise ShadowConflictError(\n"
        '            "同一比对身份出现冲突内容："\n'
        "            f\"{comparison.identity} stored={stored} incoming={row['content_fingerprint']}\"\n"
        "        )\n",
        "        return \"identical\"  # MUTANT M-SH8: last-write-wins\n",
        "相同 comparison identity 允许 conflicting overwrite（last-write-wins）",
    ),
)


def _format_entries() -> str:
    chunks = []
    for name, path, before, after, description in NEW_MUTATIONS:
        chunks.append(
            "    (\n"
            f'        "{name}",\n'
            f"        {Path(path).stem.upper() if path == INGESTION else _const_for(path)},\n"
            f"        {before!r},\n"
            f"        {after!r},\n"
            f'        "{description}",\n'
            "    ),\n"
        )
    return "".join(chunks)


_CONST_NAMES = {INGESTION: "INGESTION", BACKFILL: "BACKFILL", ARCHIVE: "ARCHIVE", SHADOW: "SHADOW"}


def _const_for(path: str) -> str:
    return _CONST_NAMES[path]


def main() -> int:
    text = MATRIX.read_text(encoding="utf-8")
    changed = []

    # 1. 重指向 anchor
    for label, old, new in (
        ("M-F1 anchor", M_F1_OLD_ANCHOR, M_F1_NEW_ANCHOR),
        ("M-F1 mutation", M_F1_OLD_AFTER, M_F1_NEW_AFTER),
        ("M-S1 anchor", M_S1_OLD_ANCHOR, M_S1_NEW_ANCHOR),
        ("M-S1 mutation", M_S1_OLD_AFTER, M_S1_NEW_AFTER),
    ):
        if old in text:
            text = text.replace(old, new, 1)
            changed.append(f"repointed {label}")
        elif new in text:
            changed.append(f"{label} already repointed")
        else:
            print(f"ERROR: cannot find {label}")
            return 1

    # 2. 新增目标文件常量
    if "SHADOW = " not in text:
        marker = 'INGESTION_TEST = "backend/test_tradability_ingestion.py"\n'
        if marker not in text:
            print("ERROR: cannot find module-constant block")
            return 1
        text = text.replace(
            marker,
            marker + 'SHADOW = "backend/tradability_shadow.py"\n'
            'ARCHIVE = "backend/tradability_archive.py"\n',
            1,
        )
        changed.append("added SHADOW/ARCHIVE module constants")

    # 3. 追加 TEST_MODULES_BY_ID / IMPORT_MODULE_BY_ID 条目
    for mapping, extras, header in (
        ("TEST_MODULES_BY_ID", EXTRA_TEST_MODULES, '    "M-F3": ("test_tradability_backfill",),\n'),
        ("IMPORT_MODULE_BY_ID", EXTRA_IMPORT_MODULES, '    "M-DR1": "tradability_backfill",\n'),
    ):
        block = "".join(f'    "{key}": {value!r},\n' for key, value in extras.items())
        if block.strip() and block not in text:
            if header not in text:
                print(f"ERROR: cannot find anchor in {mapping}")
                return 1
            text = text.replace(header, header + block, 1)
            changed.append(f"added {len(extras)} entries to {mapping}")

    # 4. 追加 MUTATIONS 条目（插在 MUTATIONS 元组结尾 ``)`` 之前）
    tail_marker = (
        '        "coverage_ratio 被改回 evidence_present / requested_pairs（一点证据 = 100%覆盖）",\n'
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
            '        "coverage_ratio 被改回 evidence_present / requested_pairs（一点证据 = 100%覆盖）",\n'
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
