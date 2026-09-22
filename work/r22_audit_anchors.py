# -*- coding: utf-8 -*-
"""R22 变异锚点静态审计（只读，秒级）。

对 MUTATIONS 里每一条，检查 old 锚点在目标文件里出现次数，
并确认 old != new（注入必须改变字节）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r22_mutation_check as M  # noqa: E402

ONLY = None
if "--only" in sys.argv:
    ONLY = {x for x in sys.argv[sys.argv.index("--only") + 1].split(",") if x}


def main() -> int:
    bad = 0
    checked = 0
    for m in M.MUTATIONS:
        if ONLY is not None and m["id"] not in ONLY:
            continue
        checked += 1
        path = os.path.join(M.ROOT, m.get("file_override", m["file"]))
        text = open(path, "rb").read().decode("utf-8").replace("\r\n", "\n")
        count = text.count(m["old"])
        flags = []
        if count == 0:
            flags.append("NOT-FOUND")
        elif m.get("last"):
            if count < 1:
                flags.append("NOT-FOUND")
        elif count > 1:
            flags.append(f"AMBIGUOUS({count})")
        if m["old"] == m["new"]:
            flags.append("INERT")
        status = "OK" if not flags else "BAD"
        if flags:
            bad += 1
        print(f'{m["id"]}: {status} count={count} {" ".join(flags)}')
    print(f"audit: {checked - bad}/{checked} OK; bad={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
