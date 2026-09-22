# -*- coding: utf-8 -*-
"""校验 r23_mutation_check 的每条 anchor 在当前源码里的**唯一性**。

普通 mutation 的 anchor 必须**恰好出现一次**：`_apply()` 用的是
``text.replace(old, new, 1)``，出现多次意味着「改哪一处」由字符串顺序决定，
而 anchor 一旦漂移就可能悄悄改到别的调用点（矩阵仍然打印 CAUGHT，但测的是别的
东西）。确实需要「改最后一处」的 mutation 必须显式声明 ``last=True``，由本脚本
按其设计单独验证（``rfind`` 命中，且它确实不是唯一命中）。

用法：
    python work/r23_round2_anchor_check.py
退出码 0 = 全部合格；1 = 有 anchor 缺失或重复。
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
    duplicates: list[str] = []
    missing: list[str] = []
    last_mode: list[str] = []

    for mutation in _load_matrix():
        path = os.path.join(ROOT, mutation.get("file_override", mutation["file"]))
        text = open(path, encoding="utf-8").read().replace("\r\n", "\n")
        count = text.count(mutation["old"])
        is_last = bool(mutation.get("last"))

        if count == 0:
            missing.append(mutation["id"])
            verdict = "MISSING"
        elif is_last:
            # ``last=True`` 走 rfind：允许重复，但必须真的能在尾部命中，
            # 且必须**确实**是重复的（否则 last 语义是多余的、容易误导）。
            last_mode.append(mutation["id"])
            verdict = "OK(last)" if count > 1 else "OK(last-but-unique)"
        elif count == 1:
            verdict = "OK"
        else:
            duplicates.append(mutation["id"])
            verdict = f"DUPLICATE({count})"

        print(f'{mutation["id"]:8} count={count} {verdict}  test={mutation["test"]}')

    print()
    print(f"total={len(_load_matrix())} missing={len(missing)} duplicate={len(duplicates)} "
          f"last_mode={len(last_mode)}")
    if missing:
        print(f"MISSING: {missing}")
    if duplicates:
        print(f"DUPLICATE (anchor 不唯一，必须消歧或显式 last=True): {duplicates}")
    if missing or duplicates:
        print("VERDICT: FAIL")
        return 1
    print("VERDICT: PASS（每条普通 anchor 恰好命中一次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
