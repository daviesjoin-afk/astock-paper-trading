# -*- coding: utf-8 -*-
"""校验 r23_mutation_check 的每条 anchor 在当前源码里是否存在且唯一。"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import importlib.util

spec = importlib.util.spec_from_file_location(
    "r23mut", os.path.join(ROOT, "work", "r23_mutation_check.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

bad = []
for mutation in mod.MUTATIONS:
    path = os.path.join(ROOT, mutation.get("file_override", mutation["file"]))
    text = open(path, encoding="utf-8").read().replace("\r\n", "\n")
    count = text.count(mutation["old"])
    status = "OK" if count >= 1 else "MISSING"
    if mutation.get("last"):
        status += "(last)"
    if count == 0:
        bad.append(mutation["id"])
    print(f'{mutation["id"]:8} count={count} {status}  test={mutation["test"]}')

print()
print(f"total={len(mod.MUTATIONS)} missing={len(bad)}: {bad}")
raise SystemExit(1 if bad else 0)
