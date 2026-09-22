# -*- coding: utf-8 -*-
"""审计 r23_mutation_check 的 21 条 mutation anchor **唯一性**。

mutation runner 本身已在 ``_apply()`` 内强制 ``count == 1``（这是执行 mutation 的
唯一入口，安全条件不依赖审核者记得额外跑一个脚本）。本脚本是**审计报告工具**：
把每条 anchor 的命中次数打印出来，任何重复都会显式失败。

一个 mutation = 一个唯一 anchor = 一个明确 regression：没有 `last=True` 之类的
例外，因为当前 21 条全部天然唯一。

用法：
    python work/r23_round2_anchor_check.py
退出码 0 = 全部唯一；1 = 有 anchor 缺失或重复。
"""
from __future__ import annotations

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_matrix():
    spec = importlib.util.spec_from_file_location(
        "r23mut", os.path.join(ROOT, "work", "r23_mutation_check.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MUTATIONS


def main() -> int:
    mutations = _load_matrix()
    missing: list[str] = []
    duplicates: list[str] = []

    for mutation in mutations:
        path = os.path.join(ROOT, mutation.get("file_override", mutation["file"]))
        text = open(path, encoding="utf-8").read().replace("\r\n", "\n")
        count = text.count(mutation["old"])
        if count == 0:
            missing.append(mutation["id"])
            verdict = "MISSING"
        elif count == 1:
            verdict = "OK"
        else:
            duplicates.append(mutation["id"])
            verdict = f"DUPLICATE({count})"
        print(f'{mutation["id"]:8} count={count} {verdict}  test={mutation["test"]}')

    print()
    print(f"total={len(mutations)} missing={len(missing)} duplicate={len(duplicates)}")
    if missing:
        print(f"MISSING: {missing}")
    if duplicates:
        print(f"DUPLICATE (anchor 必须唯一): {duplicates}")
    if missing or duplicates:
        print("VERDICT: FAIL")
        return 1
    print("VERDICT: PASS（每条 anchor 恰好命中一次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
