# -*- coding: utf-8 -*-
"""证明 M-SP20 / M-SP19 的 RED 是**业务断言失败**，不是接线错误（本地证据）。

要求（最终 review blocker 第 2 项）：
  RV07 必须因为 stored version=v2 / expected=v1 而 FAIL；
  不得因为 NameError / UnboundLocalError / SyntaxError / ImportError 而 RED。

做法：套用 mutation 的 new 文本，编译并运行目标测试，打印真实的失败原因。
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, ROOT)

WIRING_RE = re.compile(
    r"(SyntaxError|IndentationError|TabError|ImportError|ModuleNotFoundError"
    r"|NameError|UnboundLocalError|_FailedTest|is not defined"
    r"|local variable .* referenced before assignment)")

import importlib.util

spec = importlib.util.spec_from_file_location(
    "r23mut", os.path.join(ROOT, "work", "r23_mutation_check.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

TARGETS = ["M-SP19", "M-SP20", "M-SP21"]


def run(target_test):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = tempfile.mkdtemp(prefix="r23_biz_pycache_")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-m", "unittest", target_test],
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900, env=env)


def main() -> int:
    bad = []
    for mutation in mod.MUTATIONS:
        if mutation["id"] not in TARGETS:
            continue
        path = os.path.join(ROOT, mutation["file"])
        original = open(path, "rb").read()
        before = hashlib.sha256(original).hexdigest()
        text = original.decode("utf-8").replace("\r\n", "\n")
        if text.count(mutation["old"]) != 1:
            print(f'{mutation["id"]}: anchor count != 1，先修 anchor')
            bad.append(mutation["id"])
            continue
        # 先证明 mutant 本身是合法 Python（语法层面可运行）。
        mutated = text.replace(mutation["old"], mutation["new"], 1)
        try:
            compile(mutated, mutation["file"], "exec")
            syntax_ok = True
        except SyntaxError as exc:
            syntax_ok = False
            print(f'{mutation["id"]}: mutant 语法错误 -> {exc}')

        try:
            with open(path, "wb") as handle:
                handle.write(mutated.encode("utf-8"))
            result = run(mutation["test"])
        finally:
            with open(path, "wb") as handle:
                handle.write(original)
        assert hashlib.sha256(open(path, "rb").read()).hexdigest() == before

        blob = (result.stdout or "") + (result.stderr or "")
        wiring = bool(WIRING_RE.search(blob))
        reasons = [line.strip() for line in blob.splitlines()
                   if "Error" in line or "assert" in line.lower()]
        print(f'{mutation["id"]}: syntax_ok={syntax_ok} '
              f'rc={result.returncode} wiring_error={wiring}')
        for line in reasons[:4]:
            print("    ", line[:200])
        if not syntax_ok or wiring or result.returncode == 0:
            bad.append(mutation["id"])

    print()
    if bad:
        print(f"VERDICT: FAIL — 这些 mutant 的 RED 不是业务断言: {bad}")
        return 1
    print("VERDICT: PASS — 全部 mutant 语法合法，且因业务断言失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
