# -*- coding: utf-8 -*-
"""诊断一条存活变异：应用它，跑整个测试模块，报出真正变红的测试。

只读诊断（会临时改写目标文件，finally 逐字节还原）。任何时候只处理一条。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "work"))
import r22_mutation_check as M  # noqa: E402

TARGET_MODULE = "test_portfolio_read_model"


def main() -> int:
    mid = sys.argv[1]
    mutation = next(m for m in M.MUTATIONS if m["id"] == mid)
    path = ROOT / mutation.get("file_override", mutation["file"])
    original = path.read_bytes()
    before = hashlib.sha256(original).hexdigest()
    text = original.decode("utf-8").replace("\r\n", "\n")
    mutated = M._apply(text, mutation)
    assert mutated != text, "mutation is inert at the byte level"

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        path.write_bytes(M._adapt_eol(mutated, original))
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", TARGET_MODULE],
            cwd=str(ROOT / "backend"), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900, env=env,
        )
    finally:
        path.write_bytes(original)
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        assert after == before, "restore failed"

    blob = (result.stdout or "") + (result.stderr or "")
    reds = [line for line in blob.splitlines()
            if line.startswith(("FAIL:", "ERROR:"))]
    extra = [line for line in blob.splitlines()
             if line.startswith(("SyntaxError", "IndentationError",
                                 "ImportError", "ModuleNotFoundError"))]
    print(f"mutation {mid}: rc={result.returncode}")
    print(f"  designated test: {mutation['test'].split('.')[-1]}")
    print(f"  red tests ({len(reds)}):")
    for line in reds:
        print("   ", line)
    if extra:
        print("  import/syntax errors:", extra[:3])
    if not reds:
        print("  => NO test observes this mutation in the module.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
