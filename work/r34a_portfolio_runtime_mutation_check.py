#!/usr/bin/env python3
"""Reversible R34-A semantic mutations with byte-for-byte restore checks."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BASELINE = ("test_portfolio_runtime", "test_paper_risk_exit_eligibility")
MUTATIONS = [
    ("backend/portfolio_runtime.py",
     'pins = tuple(sorted((_freeze(pin) for pin in strategy_pins),\n'
     '                        key=lambda p: (p["strategy_id"], p["strategy_version"],\n'
     '                                       p["strategy_checksum"], p["account_id"])))',
     'pins = tuple(_freeze(pin) for pin in strategy_pins)',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_pa2_strategy_input_order_is_canonical"),
    ("backend/portfolio_runtime.py",
     'or not _SHA256.fullmatch(str(pin.get("strategy_checksum") or ""))',
     'or False and not _SHA256.fullmatch(str(pin.get("strategy_checksum") or ""))',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_pa4_bad_exact_pin_checksum_fails"),
    ("backend/portfolio_runtime.py",
     'if [d.name for d in dims] != sorted(DIMENSIONS):', 'if False:',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_dimensions_must_be_complete_and_unique"),
    ("backend/portfolio_runtime.py",
     'if not set(execution).issubset(owners):', 'if False:',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_execution_participants_must_be_economic_owners"),
    ("backend/portfolio_runtime.py",
     'if self.status in (UNAVAILABLE, NOT_APPLICABLE) and not reasons:', 'if False:',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_unavailable_dimension_requires_reason"),
    ("backend/portfolio_runtime.py",
     'if self.status == UNAVAILABLE and self.provenance != "UNAVAILABLE":', 'if False:',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_unavailable_dimension_cannot_claim_owner_provenance"),
    ("backend/portfolio_runtime.py",
     'if self.status == NOT_APPLICABLE and self.facts:', 'if False:',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_not_applicable_dimension_cannot_carry_facts"),
    ("backend/portfolio_runtime.py",
     'and _sha(snapshot.fingerprint_material()) == snapshot.snapshot_fingerprint)',
     'and True)',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_fingerprint_verification_binds_cycle_identity"),
    ("backend/portfolio_runtime_repository.py", 'INSERT OR IGNORE INTO portfolio_runtime_snapshots',
     'INSERT OR REPLACE INTO portfolio_runtime_snapshots',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_pa19_same_id_different_content_conflicts"),
    ("backend/portfolio_runtime.py",
     'return {"schema_version": self.schema_version,',
     'return {"portfolio_score": 0, "schema_version": self.schema_version,',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_pa20_snapshot_contains_no_rank_or_score"),
    ("backend/paper_cycle_ownership.py",
     'if any(not attachment_prover(',
     'if False and any(not attachment_prover(',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_pa21_exact_cycle_owners_require_asof_attachment_proof"),
    ("backend/paper_cycle_ownership.py",
     'if enabled and set(enabled) != bound:', 'if enabled and False and set(enabled) != bound:',
     "test_portfolio_runtime.PortfolioRuntimeContractTests.test_pa22_configured_and_resolved_owner_sets_must_match"),
]


def run(*args):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run((sys.executable, "-m", "unittest", *args), cwd=BACKEND,
                          capture_output=True, text=True, timeout=120, env=env)


def main() -> int:
    paths = sorted({ROOT / row[0] for row in MUTATIONS})
    original = {path: path.read_bytes() for path in paths}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in original.items()}
    baseline = run(*BASELINE, "-q")
    if baseline.returncode:
        print("baseline = RED")
        print((baseline.stdout + baseline.stderr)[-4000:])
        return 1
    detected = fake = timeout = survived = 0
    try:
        for index, (relative, before, after, selector) in enumerate(MUTATIONS, 1):
            path = ROOT / relative
            source = original[path].decode("utf-8")
            count = source.count(before)
            if count != 1:
                fake += 1
                print(f"M-P{index} FAKE anchor_count={count}")
                continue
            path.write_text(source.replace(before, after, 1), encoding="utf-8", newline="")
            try:
                result = run(selector, "-q")
            except subprocess.TimeoutExpired:
                timeout += 1
                print(f"M-P{index} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"M-P{index} DETECTED")
                else:
                    survived += 1
                    print(f"M-P{index} SURVIVED")
                    print((result.stdout + result.stderr)[-1500:])
            finally:
                path.write_bytes(original[path])
    finally:
        for path, data in original.items():
            path.write_bytes(data)
    restored = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path]
                   for path in paths)
    final = run(*BASELINE, "-q")
    print(f"M-P detected = {detected}/{len(MUTATIONS)}")
    print(f"survived = {survived}; fake = {fake}; timeout = {timeout}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if detected == len(MUTATIONS) and not survived and not fake and not timeout \
        and restored and final.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
