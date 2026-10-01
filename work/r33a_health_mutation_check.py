# -*- coding: utf-8 -*-
"""R33-A semantic mutations (M-H1 … M-H10).

每条变异只破坏一个 contract，并且必须让指定回归变 RED：

    M-H1  exact version identity ignored
    M-H2  checksum ignored
    M-H3  observation window omitted from the fingerprint
    M-H4  unknown execution evidence counted as verified
    M-H5  ENTRY/EXECUTION rows counted as RISK
    M-H6  missing performance turned into a zero
    M-H7  a latest-snapshot fallback introduced
    M-H8  the snapshot fingerprint drops the source fingerprint
    M-H9  health capture mutates the lifecycle
    M-H10 the append idempotency verification removed

Usage:  python work/r33a_health_mutation_check.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

# (relative path, old, new, test selector)
MUTATIONS = [
    # M-H1：不再绑定 exact version（退回 head）。
    (
        "backend/strategy_health_service.py",
        '        version_row = SR.get_version(identity[0], identity[1], checksum=identity[2], conn=conn)\n',
        '        version_row = SR.get_version(identity[0], conn=conn)\n',
        "test_r33a_strategy_health.StrategyHealthContractTests.test_h2_unknown_exact_version_fails_closed",
    ),
    # M-H2：忽略 checksum。
    (
        "backend/strategy_health_service.py",
        '        version_row = SR.get_version(identity[0], identity[1], checksum=identity[2], conn=conn)\n',
        '        version_row = SR.get_version(identity[0], identity[1], conn=conn)\n',
        "test_r33a_strategy_health.StrategyHealthContractTests.test_h3_checksum_mismatch_fails_closed",
    ),
    # M-H3：observation window 不进指纹。
    (
        "backend/strategy_health.py",
        '        "window_identity": observation_window.identity,\n',
        '        "window_identity": None,\n',
        "test_r33a_strategy_health.StrategyHealthContractTests.test_h4b_window_is_part_of_the_fingerprint",
    ),
    # M-H4：未盖章/未核验的订单被当成已验证成交。
    (
        "backend/strategy_health_service.py",
        '        "verified_orders": _verified_order_count(conn, identity, window),\n',
        '        "verified_orders": len(rows),\n',
        "test_r33a_strategy_health.StrategyHealthDimensionTests.test_h7_unknown_execution_stays_unknown",
    ),
    # M-H5：把 ENTRY / EXECUTION 行也算成风险事件。
    (
        "backend/strategy_health_service.py",
        '        "owner_issued_risk_rows": by_authority["RISK"],\n',
        '        "owner_issued_risk_rows": sum(by_authority.values()),\n',
        "test_r33a_strategy_health.StrategyHealthDimensionTests.test_h6_only_risk_authority_rows_count_as_risk",
    ),
    # M-H6：把「没有 owner」的 performance 编成 0。
    (
        "backend/strategy_health_service.py",
        '    return SH.unavailable_dimension(\n'
        '        SH.DIMENSION_PERFORMANCE, SH.REASON_STRATEGY_PERFORMANCE_OWNER_UNAVAILABLE,\n'
        '        facts={"strategy_version_scoped_owner": None})\n',
        '    return SH.HealthDimension(\n'
        '        name=SH.DIMENSION_PERFORMANCE, status=SH.STATUS_AVAILABLE,\n'
        '        facts={"strategy_version_scoped_owner": None, "return_pct": 0.0},\n'
        '        provenance=SH.PROVENANCE_DERIVED)\n',
        "test_r33a_strategy_health.StrategyHealthDimensionTests.test_h8_performance_has_no_owner_and_stays_unavailable",
    ),
    # M-H7：引入 latest snapshot 兜底。
    (
        "backend/strategy_health_repository.py",
        '    if row is None:\n        return None\n',
        '    if row is None:\n'
        '        row = conn.execute(\n'
        '            "SELECT snapshot_id,snapshot_fingerprint,evidence_json"\n'
        '            " FROM strategy_health_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()\n'
        '    if row is None:\n        return None\n',
        "test_r33a_strategy_health.StrategyHealthImmutabilityTests.test_h14_exact_get_only",
    ),
    # M-H8：快照指纹丢掉维度来源指纹。
    (
        "backend/strategy_health.py",
        '                "source_fingerprint": self.source_fingerprint,\n',
        '                "source_fingerprint": None,\n',
        "test_r33a_strategy_health.StrategyHealthContractTests.test_h1c_source_fingerprints_participate_in_the_snapshot_fingerprint",
    ),
    # M-H9：健康采集顺手写 lifecycle。
    (
        "backend/strategy_health_service.py",
        "        appended = SHRepo.append_snapshot(conn, snapshot)\n",
        "        SL.transition(conn, strategy_id=str(strategy_id),\n"
        "                      strategy_version=int(strategy_version),\n"
        "                      strategy_checksum=str(strategy_checksum),\n"
        "                      expected_state=\"draft\", target_state=\"candidate\",\n"
        "                      actor_type=\"human\", actor_id=\"mutation\")\n"
        "        appended = SHRepo.append_snapshot(conn, snapshot)\n",
        "test_r33a_strategy_health.StrategyHealthArchitectureGuardTests.test_capture_never_writes_the_lifecycle",
    ),
    # M-H10：append 幂等/冲突校验被拿掉。
    (
        "backend/strategy_health_repository.py",
        '    if row is None or str(row[0]) != payload:\n'
        '        # Same identity, different content is a conflict, never a silent overwrite.\n'
        '        raise StrategyHealthRepositoryError("health_snapshot_idempotency_conflict")\n',
        '    if row is None:\n'
        '        raise StrategyHealthRepositoryError("health_snapshot_idempotency_conflict")\n',
        "test_r33a_strategy_health.StrategyHealthImmutabilityTests.test_h13_same_identity_different_content_conflicts",
    ),
    # M-H11：成交计数退回按订单创建日绑定（丢窗口内成交、算进窗口外成交）。
    (
        "backend/strategy_health_service.py",
        '        " AND substr(f.fill_date,1,10) >= ? AND substr(f.fill_date,1,10) < ?",\n',
        '        " AND substr(o.created_at,1,10) >= ? AND substr(o.created_at,1,10) < ?",\n',
        "test_r33a_strategy_health.StrategyHealthDimensionTests.test_h7c_fills_are_bounded_by_their_own_fill_date",
    ),
    # M-H12：畸形 comparison report id 不再在 owner reader 之前被挡。
    (
        "backend/strategy_health_service.py",
        '    if not isinstance(comparison_report_id, str) or len(comparison_report_id) != 64:\n'
        '        # 输入身份非法属于 caller 的错，不是「证据不存在」：在 owner reader 之前\n'
        '        # 就 fail closed，这样 HTTP 层只会看到受控的 4xx，而不是 reader 抛出的 5xx。\n'
        '        raise SH.HealthEvidenceError("exact_comparison_report_id_required")\n',
        '    if False:\n'
        '        raise SH.HealthEvidenceError("exact_comparison_report_id_required")\n',
        "test_r33a_strategy_health.StrategyHealthApiSurfaceTests.test_malformed_or_unknown_comparison_report_id_is_a_controlled_4xx",
    ),
]

BASELINE_MODULE = "test_r33a_strategy_health"


def run(selector: str) -> subprocess.CompletedProcess:
    return subprocess.run((sys.executable, "-m", "unittest", selector, "-q"),
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)


def restore(path: Path, data: bytes, attempts: int = 10) -> None:
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
    baseline = run(BASELINE_MODULE)
    if baseline.returncode:
        print("baseline = RED")
        print((baseline.stdout + baseline.stderr)[-3000:])
        return 1
    print("baseline = GREEN")
    detected = fake = timed_out = survived = 0
    try:
        for index, (relative, old, new, selector) in enumerate(MUTATIONS, 1):
            path = ROOT / relative
            source = original[path].decode("utf-8")
            if source.count(old) != 1:
                fake += 1
                print(f"M-H{index} FAKE (anchor count={source.count(old)})")
                continue
            path.write_text(source.replace(old, new, 1), encoding="utf-8", newline="")
            try:
                result = run(selector)
            except subprocess.TimeoutExpired:
                timed_out += 1
                print(f"M-H{index} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"M-H{index} DETECTED")
                else:
                    survived += 1
                    print(f"M-H{index} SURVIVED")
                    print((result.stdout + result.stderr)[-1000:])
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
    same = {str(path): (path.read_bytes() == data) for path, data in original.items()}
    print("restore SHA256 = " + ("PASS" if all(same.values()) else "FAIL"))
    after = run(BASELINE_MODULE)
    print("baseline after restore = " + ("GREEN" if after.returncode == 0 else "RED"))
    return 0 if (survived == 0 and fake == 0 and timed_out == 0 and not failures
                 and after.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
