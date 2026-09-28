#!/usr/bin/env python3
"""Reversible semantic mutations for the R31 lifecycle/promotion contract."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TEST_CLASS = "test_strategy_lifecycle_promotion.StrategyLifecyclePromotionRegressions"


def mutation(path: str, old: str, new: str, number: int):
    # Resolve the generated test name through a short unittest selector below.
    return (ROOT / path, old, new, number)


MUTATIONS = [
    mutation("backend/strategy_lifecycle.py", '"draft": frozenset({"candidate", "archived", "quarantined"})', '"draft": frozenset({"archived", "quarantined"})', 2),
    mutation("backend/strategy_lifecycle.py", '"candidate": frozenset({"research", "rejected", "retiring", "quarantined"})', '"candidate": frozenset({"rejected", "retiring", "quarantined"})', 2),
    mutation("backend/strategy_lifecycle.py", 'if target_state not in TRANSITION_TABLE[expected_state]:', 'if False:', 3),
    mutation("backend/strategy_lifecycle.py", '"archived": frozenset(),', '"archived": frozenset({"draft"}),', 4),
    mutation("backend/strategy_lifecycle.py", 'if actor_type == "ai":\n        raise LifecycleError("ai_cannot_apply_transition")', 'if actor_type == "never":\n        raise LifecycleError("ai_cannot_apply_transition")', 8),
    mutation("backend/strategy_lifecycle.py", 'if int(head[0]) != int(version) or head[1] != checksum:', 'if False:', 6),
    mutation("backend/strategy_lifecycle.py", 'if row[0] != checksum:', 'if False:', 7),
    mutation("backend/strategy_lifecycle.py", "BEFORE UPDATE ON strategy_lifecycle_events\n      BEGIN SELECT RAISE(ABORT,'append-only strategy lifecycle events'); END;", "BEFORE UPDATE ON strategy_lifecycle_events\n      BEGIN SELECT 1; END;", 10),
    mutation("backend/strategy_lifecycle.py", "BEFORE DELETE ON strategy_lifecycle_events\n      BEGIN SELECT RAISE(ABORT,'append-only strategy lifecycle events'); END;", "BEFORE DELETE ON strategy_lifecycle_events\n      BEGIN SELECT 1; END;", 10),
    mutation("backend/strategy_lifecycle.py", 'FORMAL_CYCLE_STATES = frozenset({"paper", "production_sim"})', 'FORMAL_CYCLE_STATES = frozenset({"paper", "production_sim", "shadow"})', 14),
    mutation("backend/strategy_lifecycle.py", '"active": "paper"', '"active": "production_sim"', 20),
    mutation("backend/strategy_lifecycle.py", '"draft": "draft", "validated": "validated", "active": "paper",', '"draft": "draft", "validated": "validated", "active": "paper", "candidate": "candidate",', 24),
    mutation("backend/strategy_lifecycle.py", '"draft",\n                    transition_kind="version_created"', '"paper",\n                    transition_kind="version_created"', 12),
    mutation("backend/strategy_lifecycle.py", 'return any(str(row[1]) != "draft" or row[0] not in (None, "draft")', 'return False and any(str(row[1]) != "draft" or row[0] not in (None, "draft")', 71),
    mutation("backend/strategy_promotion.py", 'if (from_state, target_state) == ("draft", "candidate"):', 'if False:', 29),
    mutation("backend/strategy_promotion.py", 'if readiness.get("runtime_ready"):', 'if True:', 29),
    mutation("backend/strategy_promotion.py", 'if (run.get("validation_status") != "ready"\n            or run.get("strategy_id") != identity["strategy_id"]\n            or run.get("strategy_version") != identity["strategy_version"]\n            or run.get("strategy_checksum") != identity["strategy_checksum"]):', 'if False:', 32),
    mutation("backend/strategy_promotion.py", 'if not isinstance(result, Mapping) or result.get("status") != "completed":', 'if not isinstance(result, Mapping) or result.get("status") == "completed":', 33),
    mutation("backend/strategy_promotion.py", 'or run.get("strategy_version") != identity["strategy_version"]', 'or False', 34),
    mutation("backend/strategy_promotion.py", 'or run.get("strategy_checksum") != identity["strategy_checksum"]):', '):', 35),
    mutation("backend/strategy_promotion.py", 'if (expected != run_key or run.get("run_key") != run_key\n                or run.get("runner_version") != R29.RUNNER_VERSION):', 'if False:', 36),
    mutation("backend/strategy_promotion.py", 'or baseline.get("run_key") != run_key', 'or False', 38),
    mutation("backend/strategy_promotion.py", 'or baseline.get("strategy_version") != identity["strategy_version"]', 'or False', 39),
    mutation("backend/strategy_promotion.py", 'or case["result"].get("status") == "failed" for case in cases)', 'or False for case in cases)', 40),
    mutation("backend/strategy_promotion.py", 'case["result"].get("status") == "unavailable" for case in cases', 'False for case in cases', 41),
    mutation("backend/strategy_promotion.py", 'if not bundle.r30_report_key:', 'if False:', 45),
    mutation("backend/strategy_promotion.py", 'proposal_fingerprint = _sha(fingerprint_material)', 'proposal_fingerprint = _sha({**fingerprint_material, "created_at": created_at})', 48),
    mutation("backend/strategy_promotion.py", 'fingerprint_material = {key: payload[key] for key in (\n        "strategy_id", "strategy_version", "strategy_checksum", "from_state", "target_state",\n        "evidence_bundle", "proposer_type", "proposer_id", "policy_version")}', 'fingerprint_material = {key: payload[key] for key in (\n        "strategy_id", "strategy_checksum", "from_state", "target_state",\n        "evidence_bundle", "proposer_type", "proposer_id", "policy_version")}', 46),
    mutation("backend/strategy_promotion.py", 'fingerprint_material = {key: payload[key] for key in (\n        "strategy_id", "strategy_version", "strategy_checksum", "from_state", "target_state",\n        "evidence_bundle", "proposer_type", "proposer_id", "policy_version")}', 'fingerprint_material = {key: payload[key] for key in (\n        "strategy_id", "strategy_version", "strategy_checksum", "from_state", "target_state",\n        "proposer_type", "proposer_id", "policy_version")}', 47),
    mutation("backend/strategy_promotion.py", 'if prior[0] != payload_fingerprint:', 'if False:', 49),
    mutation("backend/strategy_promotion.py", "CREATE TRIGGER IF NOT EXISTS strategy_promotion_proposals_no_update\n      BEFORE UPDATE ON strategy_promotion_proposals\n      BEGIN SELECT RAISE(ABORT,'append-only strategy promotion proposals'); END;\n    CREATE TRIGGER IF NOT EXISTS strategy_promotion_proposals_no_delete\n      BEFORE DELETE ON strategy_promotion_proposals\n      BEGIN SELECT RAISE(ABORT,'append-only strategy promotion proposals'); END;", "CREATE TRIGGER IF NOT EXISTS strategy_promotion_proposals_no_update\n      BEFORE UPDATE ON strategy_promotion_proposals\n      BEGIN SELECT 1; END;\n    CREATE TRIGGER IF NOT EXISTS strategy_promotion_proposals_no_delete\n      BEFORE DELETE ON strategy_promotion_proposals\n      BEGIN SELECT 1; END;", 50),
    mutation("backend/strategy_promotion.py", 'if actor_type == "ai":\n        raise PromotionError("ai_cannot_apply_transition")', 'if actor_type == "never":\n        raise PromotionError("ai_cannot_apply_transition")', 52),
    mutation("backend/strategy_promotion.py", 'if (not decision.eligible or decision.decision_fingerprint != original.get("decision_fingerprint")):', 'if False:', 53),
    mutation("backend/strategy_lifecycle.py", 'if expected_state == "quarantined" and target_state != "retiring":', 'if False:', 64),
    mutation("backend/strategy_lifecycle.py", 'FORMAL_CYCLE_STATES = frozenset({"paper", "production_sim"})', 'FORMAL_CYCLE_STATES = frozenset({"paper", "production_sim", "quarantined"})', 63),
    mutation("backend/strategy_lifecycle.py", 'return str(state or "") in FORMAL_CYCLE_STATES', 'return True', 17),
    mutation("backend/strategy_lifecycle.py", 'except Exception:\n        if conn.in_transaction:\n            try:\n                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")', 'except Exception:\n        if False:\n            try:\n                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")', 9),
    mutation("frontend/src/features/strategies.js", 'var eligible=(lifecycle.eligible_transitions||[]);', 'var eligible=(lifecycle.legal_transitions||[]);', 86),
    mutation("frontend/src/features/strategies.js", 'data-testid="strategy-lifecycle-event"', 'data-testid="removed-event-history"', 85),
    mutation("frontend/src/features/strategies.js", 'lifecycle.blocking_reasons||[]', '[]', 87),
    mutation("frontend/src/features/strategies.js", "'<small>R29 '+adaptiveEsc(bundle.r29_run_key||'未指定')+' · R30 '+adaptiveEsc(bundle.r30_report_key||'未指定')+'</small>'", "'<small>Evidence pending</small>'", 89),
    mutation("frontend/src/features/strategies.js", "'<article class=\"strategy-proposal\" data-testid=\"strategy-promotion-proposal\"><b>PROPOSAL · '", "'<article class=\"strategy-proposal\" data-testid=\"strategy-promotion-proposal\"><b>APPLIED · '", 90),
    mutation("frontend/src/features/strategies.js", "proposal.proposer_type!=='ai'&&decision.eligible", "true", 92),
]


def run(command: tuple[str, ...], *, timeout: int = 45):
    return subprocess.run(command, cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def test_command(number: int) -> tuple[str, ...]:
    # unittest accepts the concrete generated method name, discovered from the test module.
    probe = run((sys.executable, "-c",
        f"import test_strategy_lifecycle_promotion as t; "
        f"print(next(n for n in dir(t.StrategyLifecyclePromotionRegressions) "
        f"if n.startswith('test_R31_{number:02d}_')))"))
    if probe.returncode:
        raise RuntimeError(probe.stderr or probe.stdout)
    return (sys.executable, "-m", "unittest",
            f"{TEST_CLASS}.{probe.stdout.strip()}", "-q")


def main() -> int:
    files = sorted({entry[0] for entry in MUTATIONS})
    original = {path: path.read_bytes() for path in files}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in original.items()}
    baseline = run((sys.executable, "-m", "unittest", "test_strategy_lifecycle_promotion", "-q"))
    if baseline.returncode:
        print("baseline = RED")
        print((baseline.stdout + baseline.stderr)[-5000:])
        return 1
    print("baseline = GREEN")
    detected = fake = timeout_count = survived = 0
    try:
        for index, (path, old, new, number) in enumerate(MUTATIONS, 1):
            source = original[path].decode("utf-8")
            if source.count(old) != 1:
                fake += 1
                print(f"M-R31-{index:02d} FAKE (anchor count={source.count(old)})")
                continue
            path.write_text(source.replace(old, new, 1), encoding="utf-8", newline="")
            try:
                result = run(test_command(number))
            except subprocess.TimeoutExpired:
                timeout_count += 1
                print(f"M-R31-{index:02d} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"M-R31-{index:02d} DETECTED")
                else:
                    survived += 1
                    print(f"M-R31-{index:02d} SURVIVED")
                    print((result.stdout + result.stderr)[-1000:])
            finally:
                path.write_bytes(original[path])
    finally:
        for path, data in original.items():
            path.write_bytes(data)
    restored = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path]
                   for path in files)
    final = run((sys.executable, "-m", "unittest", "test_strategy_lifecycle_promotion", "-q"))
    print(f"mutation = {detected}/{len(MUTATIONS)} DETECTED")
    print(f"survived = {survived}")
    print(f"fake = {fake}")
    print(f"timeout = {timeout_count}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if detected == len(MUTATIONS) and not survived and not fake \
        and not timeout_count and restored and final.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
