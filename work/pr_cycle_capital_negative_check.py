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


def run_contract_tests() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "unittest", "backend.test_paper_cycle_capital", "-q"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "backend"},
        capture_output=True,
        text=True,
    )


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
    results: list[tuple[str, str]] = []
    pristine_checks: list[bool] = []
    restore_bytes_checks: list[bool] = []
    restore_sha_checks: list[bool] = []
    try:
        baseline = run_contract_tests()
        if baseline.returncode != 0:
            print("baseline: FAILED")
            print(baseline.stdout, baseline.stderr)
            return 1
        print("baseline: PASS")

        for name, before, after in mutations:
            before_bytes = TARGET.read_bytes()
            before_sha = sha256(before_bytes)
            bytes_match = before_bytes == original
            sha256_match = before_sha == original_sha
            pristine_checks.append(bytes_match and sha256_match)
            print(f"{name} pre: bytes_match={bytes_match} sha256_match={sha256_match}")
            if not bytes_match or not sha256_match:
                raise RuntimeError(f"{name} refuses to mutate a non-pristine production source")

            mutated = replace_once(original, before, after)
            try:
                TARGET.write_bytes(mutated)
                run = run_contract_tests()
                caught = run.returncode != 0
                results.append((name, "CAUGHT" if caught else "UNDETECTED"))
                print(f"{name}: {'CAUGHT' if caught else 'UNDETECTED'}")
                if not caught:
                    print(run.stdout, run.stderr)
            finally:
                TARGET.write_bytes(original)

            restored = TARGET.read_bytes()
            restored_sha = sha256(restored)
            restored_bytes_match = restored == original
            restored_sha_match = restored_sha == original_sha
            restore_bytes_checks.append(restored_bytes_match)
            restore_sha_checks.append(restored_sha_match)
            print(
                f"{name} restore: bytes_match={restored_bytes_match} "
                f"sha256_match={restored_sha_match}"
            )
            if not restored_bytes_match or not restored_sha_match:
                raise RuntimeError(f"{name} restore verification failed; refusing next mutation")

        final = TARGET.read_bytes()
        final_bytes_match = final == original
        final_sha_match = sha256(final) == original_sha
        print(f"final restore: bytes_match={final_bytes_match} sha256_match={final_sha_match}")
        complete = (
            len(results) == 12
            and all(result == "CAUGHT" for _, result in results)
            and len(pristine_checks) == 12
            and all(pristine_checks)
            and len(restore_bytes_checks) == 12
            and all(restore_bytes_checks)
            and len(restore_sha_checks) == 12
            and all(restore_sha_checks)
            and final_bytes_match
            and final_sha_match
        )
        print(f"original_sha256={original_sha}")
        return 0 if complete else 1
    finally:
        TARGET.write_bytes(original)


if __name__ == "__main__":
    raise SystemExit(main())
