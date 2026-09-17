# -*- coding: utf-8 -*-
"""重指 M-R2 / M-R8 的 anchor（第三轮改动影响了它们引用的源码片段）。幂等。

* **M-R2**：``_assert_replay_identity`` 与写循环之间插入了 autocommit 检查，
  原片段（两行紧邻）不再存在。新形态把"先写 archive 再校验"注入成：写循环在前、
  校验在后。
* **M-R8**：``conflicts`` payload 从内联列表换成了规范化的 ``json.dumps`` 排序。

用法::

    PY=<仓库 venv 的 python>
    $PY work/pr160_fix_anchors3.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "work" / "tradability_ingestion_mutation_check.py"

# 矩阵文件里，变异体片段本身是**单引号字符串**，其中的 \\n 是字面反斜杠+n。
M_R2_OLD = (
    "        '            self._assert_replay_identity(run_id, run_fingerprint)\\n"
    "            for evidence in normalized_evidence:\\n"
    "                if self._repo.save(evidence):\\n"
    "                    persisted.append(evidence)\\n"
    "                else:\\n"
    "                    skipped_records += 1  # 幂等重放：唯一键命中，逻辑状态不变。\\n',\n"
)
M_R2_NEW = (
    "        '            if self._enforce_explicit_transactions:\\n"
    "                raise IngestionError(\"autocommit\")\\n"
    "            for evidence in normalized_evidence:\\n"
    "                if self._repo.save(evidence):\\n"
    "                    persisted.append(evidence)\\n"
    "                else:\\n"
    "                    skipped_records += 1  # 幂等重放：唯一键命中，逻辑状态不变。\\n',\n"
)

M_R8_OLD = (
    '        \'            "conflicts": [\\n'
    '                {"field": c.field, "providers": list(c.providers), "values": list(c.values)}\\n'
    '                for c in (conflicts or ())\\n'
    '            ],\\n\',\n'
)
M_R8_NEW = (
    '        \'            "conflicts": sorted(\\n\',\n'
)


def main() -> int:
    text = MATRIX.read_text(encoding="utf-8")
    changed = []
    for label, old, new in (
        ("M-R2", M_R2_OLD, M_R2_NEW),
        ("M-R8", M_R8_OLD, M_R8_NEW),
    ):
        if old not in text:
            print(f"ERROR: cannot find {label} anchor")
            return 1
        text = text.replace(old, new, 1)
        changed.append(f"{label} repointed")
    MATRIX.write_text(text, encoding="utf-8")
    print("matrix updated")
    for item in changed:
        print("  -", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
