# -*- coding: utf-8 -*-
"""修正 M-F2 / M-F3 的 anchor（`_run_fingerprint` 调用点因新增 outcomes 参数而换行）。

幂等。

用法::

    PY=<仓库 venv 的 python>
    $PY work/pr160_fix_fingerprint_anchors.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "work" / "tradability_ingestion_mutation_check.py"

# M-F2：把"用 persisted 当指纹输入"的替换点从单行调用改成跨行调用形态。
M_F2_OLD = (
    '"            codes, sessions, self._cutoff, normalized_evidence\\n",\n'
    '        "            codes, sessions, self._cutoff, persisted\\n",\n'
)
M_F2_NEW = (
    '"            normalized_evidence,\\n",\n'
    '        "            persisted,\\n",\n'
)

# M-F3：第三个片段（调用点）已不再存在，从 before / after 两个列表里一并删掉。
M_F3_OLD = (
    '            "            codes, sessions, self._cutoff, normalized_evidence\\n",\n'
    "        ],\n"
    "        [\n"
    '            "        run_id: str, codes: Sequence[str], sessions: Sequence[str], cutoff: str,\\n",\n'
    '            "            \\"version\\": FINGERPRINT_VERSION,\\n"\n'
    '            "            \\"run_id\\": run_id,\\n",\n'
    '            "            run_id, codes, sessions, self._cutoff, normalized_evidence\\n",\n'
    "        ],\n"
)
M_F3_NEW = (
    "        ],\n"
    "        [\n"
    '            "        run_id: str, codes: Sequence[str], sessions: Sequence[str], cutoff: str,\\n",\n'
    '            "            \\"version\\": FINGERPRINT_VERSION,\\n"\n'
    '            "            \\"run_id\\": run_id,\\n",\n'
    "        ],\n"
)


def main() -> int:
    text = MATRIX.read_text(encoding="utf-8")
    changed = []
    for label, old, new in (
        ("M-F2", M_F2_OLD, M_F2_NEW),
        ("M-F3", M_F3_OLD, M_F3_NEW),
    ):
        if new in text:
            changed.append(f"{label} already fixed")
            continue
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
