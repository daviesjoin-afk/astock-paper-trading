# -*- coding: utf-8 -*-
"""R32 Final semantic mutations (M-F1 … M-F15).

Each mutation breaks exactly one contract the Final PR introduces and is expected
to turn a named regression RED:

    M-F1  PARTIAL comparison accepted
    M-F2  incomplete coverage ignored
    M-F3  exact challenger identity ignored
    M-F4  strategy checksum ignored
    M-F5  a "newest report" fallback introduced
    M-F6  AI may apply a lifecycle transition
    M-F7  stale proposal accepted
    M-F8  the exact report id stops being required
    M-F9  environment equality ignored
    M-F10 owner provenance completeness ignored
    M-F11 lifecycle promotion reaches into the parameter-head authority
    M-F12 parameter-head activation writes the lifecycle
    M-F13 the frontend derives readiness from coverage instead of the owner
    M-F14 the workspace Active leg falls back to the endpoint's registry identity
    M-F15 the report's Challenger stops being bound to the endpoint strategy

Usage:  python work/r32_final_mutation_check.py
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
NODE = os.environ.get("MIMO_NODE") or "node"

# (relative path, old, new, runner) — runner: ("py", selector) | ("node", file)
MUTATIONS = [
    # M-F1：PARTIAL / UNAVAILABLE 被当成可用证据。
    (
        "backend/strategy_promotion.py",
        '        if availability != SC.ComparisonAvailability.AVAILABLE.value:\n',
        '        if availability == "R32_MUTATION_UNREACHABLE":\n',
        ("py", "test_r32_final_promotion.PromotionComparisonEvidenceTests.test_r_f2_partial_report_blocks"),
    ),
    # M-F2：coverage 完整性不再校验。
    (
        "backend/strategy_promotion.py",
        '                or coverage.get("available_observations") != expected):\n',
        '                or False):\n',
        ("py", "test_r32_final_promotion.PromotionComparisonEvidenceTests.test_r_f3c_coverage_and_provenance_must_be_complete"),
    ),
    # M-F3：challenger 的 exact identity 不再与请求的身份比对。
    (
        "backend/strategy_promotion.py",
        '        if (str(stamp.get("strategy_id") or "") != identity["strategy_id"]\n'
        '                or int(stamp.get("version") or 0) != identity["strategy_version"]\n'
        '                or str(stamp.get("checksum") or "") != identity["strategy_checksum"]):\n',
        '        if (str(stamp.get("strategy_id") or "") != str(stamp.get("strategy_id") or "")\n'
        '                or int(stamp.get("version") or 0) != int(stamp.get("version") or 0)\n'
        '                or str(stamp.get("checksum") or "") != str(stamp.get("checksum") or "")):\n',
        ("py", "test_r32_final_promotion.PromotionComparisonEvidenceTests.test_r_f4_report_stamp_must_equal_the_requested_exact_version"),
    ),
    # M-F4：registry 层的 strategy checksum 不再校验。
    (
        "backend/strategy_promotion.py",
        '    elif version[0] != identity["strategy_checksum"]:\n',
        '    elif False:\n',
        ("py", "test_r32_final_promotion.PromotionComparisonEvidenceTests.test_r_f4b_requested_identity_must_match_the_registry_version"),
    ),
    # M-F5：引入 latest fallback（历史证据不得用「最新的一份」替代）。
    (
        "backend/strategy_promotion.py",
        '    if report is None:\n        return None, "shadow_comparison_report_not_found"\n',
        '    if report is None:\n'
        '        _row = conn.execute(\n'
        '            "SELECT report_id FROM shadow_comparison_reports ORDER BY id DESC LIMIT 1"\n'
        '        ).fetchone()\n'
        '        if _row is not None:\n'
        '            report = SCR.get_report(conn, str(_row[0]))\n'
        '    if report is None:\n        return None, "shadow_comparison_report_not_found"\n',
        ("py", "test_r32_final_promotion.PromotionComparisonEvidenceTests.test_r_f5b_a_blocked_report_never_falls_back_to_an_available_one"),
    ),
    # M-F6：AI 可以 apply lifecycle transition。
    (
        "backend/strategy_promotion.py",
        '    if actor_type == "ai":\n        raise PromotionError("ai_cannot_apply_transition")\n',
        '    if actor_type == "ai" and False:\n        raise PromotionError("ai_cannot_apply_transition")\n',
        ("py", "test_r32_final_promotion.PromotionComparisonEvidenceTests.test_r_f6b_ai_and_stale_proposals_fail_closed"),
    ),
    # M-F7：stale proposal 被接受。
    (
        "backend/strategy_promotion.py",
        '    if (not decision.eligible or decision.decision_fingerprint != original.get("decision_fingerprint")):\n',
        '    if False:\n',
        ("py", "test_r32_final_promotion.PromotionComparisonEvidenceTests.test_r_f6b_ai_and_stale_proposals_fail_closed"),
    ),
    # M-F8：exact report id 不再是必填。
    (
        "backend/strategy_promotion.py",
        '        if not bundle.shadow_comparison_report_id:\n',
        '        if False:\n',
        ("py", "test_r32_final_promotion.PromotionComparisonEvidenceTests.test_r_f1b_report_is_required_and_only_the_declared_id_is_read"),
    ),
    # M-F9：共享环境不一致不再阻断。
    (
        "backend/strategy_promotion.py",
        '        if (str(environment.get("shared_environment_equality") or "") != "EQUAL"\n',
        '        if (False\n',
        ("py", "test_r32_final_promotion.PromotionComparisonEvidenceTests.test_r_f4c_environment_mismatch_blocks"),
    ),
    # M-F10：required provenance 完整性不再校验。
    (
        "backend/strategy_promotion.py",
        '            if str(provenance.get(path) or "") not in admissible:\n',
        '            if False:\n',
        ("py", "test_r32_final_promotion.PromotionComparisonEvidenceTests.test_r_f3c_coverage_and_provenance_must_be_complete"),
    ),
    # M-F11：Lifecycle Promotion 伸手到参数头 authority。
    (
        "backend/strategy_promotion.py",
        '    result = SL.transition(paper_conn, strategy_id=proposal["strategy_id"],\n',
        '    import strategy_champion as _SCM  # noqa: F401\n'
        '    result = SL.transition(paper_conn, strategy_id=proposal["strategy_id"],\n',
        ("py", "test_r32_final_promotion.PromotionAuthoritySeparationTests.test_lifecycle_promotion_never_touches_the_parameter_head"),
    ),
    # M-F12：参数头 activation 写 lifecycle。
    (
        "backend/strategy_champion.py",
        "def promote_challenger(paper_conn, evo_conn, strategy_id: str, *, now: dt.datetime | None = None) -> dict[str, Any]:\n",
        "def promote_challenger(paper_conn, evo_conn, strategy_id: str, *, now: dt.datetime | None = None) -> dict[str, Any]:\n"
        "    import strategy_lifecycle as _SL  # noqa: F401\n",
        ("py", "test_r32_final_promotion.PromotionAuthoritySeparationTests.test_parameter_head_activation_never_writes_the_lifecycle"),
    ),
    # M-F13：前端用 coverage 自己推导 readiness。
    (
        "frontend/src/features/strategies.js",
        "+'<p>后端判断：'+(promotion.eligible?'eligible':'blocked')",
        "+'<p>后端判断：'+(((comparison.coverage||{}).coverage_ratio>=1)?'eligible':'blocked')",
        ("node", "tests/challenger-workspace.test.mjs"),
    ),
    # M-F14：Workspace 的 Active 腿被拉回 endpoint 自己的 registry identity。
    (
        "backend/strategy_service.py",
        '            active = _leg_from_report_stamp(\n'
        '                conn, report.active_strategy_stamp,\n'
        '                source="shadow_comparison.active_strategy_stamp")\n',
        '            active = _leg_from_report_stamp(\n'
        '                conn, report.active_strategy_stamp,\n'
        '                source="shadow_comparison.active_strategy_stamp")\n'
        '            active["strategy_id"] = str(strategy_id)\n',
        ("py", "test_r32_final_promotion.ChallengerWorkspaceReadModelTests.test_w1a_both_legs_come_from_the_report_stamps"),
    ),
    # M-F15：删掉 report Challenger 与 endpoint strategy_id 的绑定。
    (
        "backend/strategy_service.py",
        '            if challenger["strategy_id"] != str(strategy_id):\n'
        '                raise InvalidStrategyDefinition("shadow_comparison_identity_mismatch")\n',
        '            if False:\n'
        '                raise InvalidStrategyDefinition("shadow_comparison_identity_mismatch")\n',
        ("py", "test_r32_final_promotion.ChallengerWorkspaceReadModelTests.test_w1c_a_report_of_another_strategy_fails_closed"),
    ),
]

BASELINE_PY = ("test_r32_final_promotion", "test_r32_final_ownership_boundary",
               "test_shadow_comparison")
BASELINE_NODE = "tests/challenger-workspace.test.mjs"


def run_python(selector: str) -> subprocess.CompletedProcess:
    return subprocess.run((sys.executable, "-m", "unittest", selector, "-q"),
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)


def run_node(selector: str) -> subprocess.CompletedProcess:
    return subprocess.run((NODE, "--test", selector), cwd=FRONTEND, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=900)


def run(runner) -> subprocess.CompletedProcess:
    kind, selector = runner
    return run_node(selector) if kind == "node" else run_python(selector)


def restore(path: Path, data: bytes, attempts: int = 10) -> None:
    """Put the original bytes back, retrying transient Windows write failures."""
    for attempt in range(1, attempts + 1):
        try:
            path.write_bytes(data)
            if path.read_bytes() == data:
                return
        except OSError:
            pass
        time.sleep(0.5 * attempt)
    raise SystemExit(f"RESTORE FAILED for {path}: worktree is not clean")


def restore_all(original: dict[Path, bytes]) -> list[str]:
    failures = []
    for path, data in original.items():
        try:
            restore(path, data)
        except SystemExit as exc:
            failures.append(str(exc))
    return failures


def main() -> int:
    files = sorted({ROOT / mutation[0] for mutation in MUTATIONS})
    original = {path: path.read_bytes() for path in files}
    baseline_py = subprocess.run((sys.executable, "-m", "unittest", *BASELINE_PY, "-q"),
                                 cwd=BACKEND, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=900)
    baseline_node = run_node(BASELINE_NODE)
    if baseline_py.returncode or baseline_node.returncode:
        print("baseline = RED")
        print((baseline_py.stdout + baseline_py.stderr)[-3000:])
        print((baseline_node.stdout + baseline_node.stderr)[-3000:])
        return 1
    print("baseline = GREEN")
    detected = fake = timed_out = survived = 0
    try:
        for index, (relative, old, new, runner) in enumerate(MUTATIONS, 1):
            path = ROOT / relative
            source = original[path].decode("utf-8")
            if source.count(old) != 1:
                fake += 1
                print(f"M-F{index} FAKE (anchor count={source.count(old)})")
                continue
            path.write_text(source.replace(old, new, 1), encoding="utf-8", newline="")
            try:
                result = run(runner)
            except subprocess.TimeoutExpired:
                timed_out += 1
                print(f"M-F{index} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"M-F{index} DETECTED")
                else:
                    survived += 1
                    print(f"M-F{index} SURVIVED")
                    print((result.stdout + result.stderr)[-1200:])
            finally:
                restore(path, original[path])
    finally:
        failures = restore_all(original)
    if failures:
        for item in failures:
            print(item)
        return 1
    print(f"mutation = {detected}/{len(MUTATIONS)} DETECTED")
    print(f"survived = {survived}")
    print(f"fake = {fake}")
    print(f"timeout = {timed_out}")
    hashes = {str(path): (path.read_bytes() == data) for path, data in original.items()}
    print("restore SHA256 = " + ("PASS" if all(hashes.values()) else "FAIL"))
    for path, same in hashes.items():
        if not same:
            print(f"  MISMATCH {path}")
    after = subprocess.run((sys.executable, "-m", "unittest", *BASELINE_PY, "-q"),
                           cwd=BACKEND, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
    print("baseline after restore = " + ("GREEN" if after.returncode == 0 else "RED"))
    return 0 if (survived == 0 and fake == 0 and timed_out == 0 and not failures
                 and after.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
