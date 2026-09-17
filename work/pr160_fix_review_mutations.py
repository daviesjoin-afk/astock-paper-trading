# -*- coding: utf-8 -*-
"""修正两个变异条目：

* **M-R6**：原变异体把 ``payload.update({`` 拼进表达式里，注入后语法不成立 →
  ``IMPORT-FAILED``，那是**假杀**（runner 正确地拒绝把它算成 CAUGHT）。改成把
  outcomes 值置空——仍可导入，但指纹不再覆盖结果分布，正是要挡的缺陷。
* **M-SH9**：原来的 anchor 只是从条件表达式里删掉一行，注入后仍然成立且**行为不变**
  （side_mismatch 依然参与判断），属于 inert 变异。改成让 side_mismatch 恒为 False，
  这样"生产 verdict 的 side 与比对 side 不一致时仍接受"才是真的被注入。

幂等。

用法::

    PY=<仓库 venv 的 python>
    $PY work/pr160_fix_review_mutations.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "work" / "tradability_ingestion_mutation_check.py"

FIXES = (
    (
        "M-R6",
        '        \'            "outcomes": dict(sorted((outcomes or {}).items())),\\n\',\n'
        '        \'        }\\n        payload.pop("outcomes", None)\\n'
        '        payload.update({  # MUTANT M-R6: 结果分布不进指纹\\n\',\n',
        '        \'            "outcomes": dict(sorted((outcomes or {}).items())),\\n\',\n'
        '        \'            "outcomes": {},  # MUTANT M-R6: 结果分布不进指纹\\n\',\n',
    ),
    (
        "M-SH9",
        "        '            or side_mismatch\\n',\n"
        "        '            # MUTANT M-SH9: 不检查生产 verdict 的 side\\n',\n",
        "        '            or side_mismatch\\n',\n"
        "        '            or False  # MUTANT M-SH9: 不检查生产 verdict 的 side\\n',\n",
    ),
)


def main() -> int:
    text = MATRIX.read_text(encoding="utf-8")
    changed = []
    for label, old, new in FIXES:
        if new in text:
            changed.append(f"{label} already fixed")
            continue
        if old not in text:
            print(f"ERROR: cannot find {label} anchor")
            return 1
        text = text.replace(old, new, 1)
        changed.append(f"{label} fixed")
    MATRIX.write_text(text, encoding="utf-8")
    print("matrix updated")
    for item in changed:
        print("  -", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
