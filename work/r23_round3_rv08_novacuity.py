# -*- coding: utf-8 -*-
"""RV08 非空性 —— 把 commit phase 改回 deferred，确认 RV08 变红（本地证据）。

用带上下文的唯一锚点，避免误改其它 ``_db(immediate=True)`` 调用点。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
PAPER = os.path.join(BACKEND, "paper_trading.py")

OLD = """    with _db(immediate=True) as conn:
        for account, candidates, meta in candidate_batches:"""
NEW = """    with _db() as conn:
        for account, candidates, meta in candidate_batches:"""

TEST = ("test_provenance_inflight_change.SignalCommitFencingTests"
        ".test_RV08_rollover_cannot_land_between_validation_and_first_insert")


def run(target):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = tempfile.mkdtemp(prefix="r23_rv08_pycache_")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-m", "unittest", target],
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900, env=env)


def main() -> int:
    with open(PAPER, "rb") as handle:
        original = handle.read()
    before = hashlib.sha256(original).hexdigest()
    text = original.decode("utf-8").replace("\r\n", "\n")
    count = text.count(OLD)
    print(f"anchor count = {count}")
    if count != 1:
        print("VERDICT: anchor 不唯一，无法给出非空性证据")
        return 1

    baseline = run(TEST)
    print(f"baseline (immediate): {'GREEN' if baseline.returncode == 0 else 'RED'}")
    if baseline.returncode != 0:
        print("VERDICT: baseline 不是绿的 —— 非空性检查无意义")
        return 1

    try:
        with open(PAPER, "wb") as handle:
            handle.write(text.replace(OLD, NEW, 1).encode("utf-8"))
        mutant = run(TEST)
    finally:
        with open(PAPER, "wb") as handle:
            handle.write(original)
    after = hashlib.sha256(open(PAPER, "rb").read()).hexdigest()
    assert before == after, "restore mismatch"

    blob = (mutant.stdout or "") + (mutant.stderr or "")
    print(f"mutant (deferred): {'RED' if mutant.returncode != 0 else 'GREEN'}")
    for line in blob.splitlines():
        if "AssertionError" in line or "OperationalError" in line:
            print("   ", line.strip()[:220])
    print("restore sha256: PASS")
    verdict = "CAUGHT" if mutant.returncode != 0 else "SURVIVED"
    print(f"VERDICT: {verdict}")
    return 0 if mutant.returncode != 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
