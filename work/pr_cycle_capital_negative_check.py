"""Local workspace mutation tooling/evidence for PR #136 (N1-N12)."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "backend" / "paper_cycle_capital.py"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replace_once(source: bytes, before: str, after: str) -> bytes:
    old = before.encode()
    new = after.encode()
    if source.count(old) != 1:
        raise AssertionError(f"mutation anchor count != 1: {before!r}")
    return source.replace(old, new, 1)


def main() -> int:
    original = TARGET.read_bytes()
    original_sha = sha256(original)
    mutations = [
        ("N1", "WHERE cycle_id=? AND id<>? AND {active_clause}", "WHERE cycle_id=? AND 1=1 AND {active_clause}"),
        ("N2", "WHERE cycle_id=? AND id<>? AND {active_clause}", "WHERE cycle_id=? AND id<>? AND 1=1"),
        ("N3", "WHERE cycle_id=? AND id<>? AND {active_clause}", "WHERE cycle_id=? AND id<>? AND initial_cash>0 AND {active_clause}"),
        ("N4", "return num_fn(cycle[\"capital\"], 0.0) / max(len(builtin_account_ids), 1)", "return num_fn(cycle[\"capital\"], 0.0) / max(len(builtin_account_ids), 2)"),
        ("N5", "return num_fn(cycle[\"capital\"], 0.0) / max(len(builtin_account_ids), 1)", "return num_fn(cycle[\"capital\"], 0.0) / len(builtin_account_ids)"),
        ("N6", "return max(0.0, num_fn(cycle[\"capital\"], 0.0) - existing_initial)", "return num_fn(cycle[\"capital\"], 0.0) - existing_initial"),
        ("N7", "COALESCE(SUM(initial_cash),0) s,COUNT(*) n ", "COALESCE(SUM(cash),0) s,COUNT(*) n "),
        ("N8", "AND id<>?\n               AND initial_cash>0", "AND 1=1\n               AND initial_cash>0"),
        ("N9", "AND initial_cash>0\n               AND {active_clause}", "AND initial_cash>0\n               AND 1=1"),
        ("N10", "AND initial_cash>0\n               AND {active_clause}", "AND 1=1\n               AND {active_clause}"),
        ("N11", "return round(num_fn(funded[\"s\"]) / int(funded[\"n\"]), 2)", "return round(num_fn(cycle[\"capital\"], 0.0) / max(len(builtin_account_ids), 1), 2)"),
        ("N12", "return round(num_fn(funded[\"s\"]) / int(funded[\"n\"]), 2)", "return round(num_fn(funded[\"s\"]) / int(funded[\"n\"]), 0)"),
    ]
    results = []

    baseline = subprocess.run(
        [sys.executable, "-m", "unittest", "backend.test_paper_cycle_capital", "-q"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "backend"},
        capture_output=True,
        text=True,
    )
    if baseline.returncode != 0:
        print("baseline: FAILED")
        print(baseline.stdout, baseline.stderr)
        return 1
    print("baseline: PASS")

    try:
        for name, before, after in mutations:
            mutated = replace_once(original, before, after)
            TARGET.write_bytes(mutated)
            run = subprocess.run(
                [sys.executable, "-m", "unittest", "backend.test_paper_cycle_capital", "-q"],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": "backend"},
                capture_output=True,
                text=True,
            )
            caught = run.returncode != 0
            results.append((name, "CAUGHT" if caught else "UNDETECTED"))
            if not caught:
                print(run.stdout, run.stderr)
    finally:
        TARGET.write_bytes(original)

    restored = TARGET.read_bytes()
    print(f"original_sha256={original_sha}")
    for name, result in results:
        print(f"{name}: {result}")
    print(f"restored_byte_for_byte={restored == original}")
    print(f"restored_sha256={sha256(restored)}")
    return 0 if len(results) == 12 and all(result == "CAUGHT" for _, result in results) and restored == original else 1


if __name__ == "__main__":
    raise SystemExit(main())
