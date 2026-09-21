# -*- coding: utf-8 -*-
"""R21 mutation matrix M-RSK1 ~ M-RSK21.

Each mutation must turn its corresponding contract test RED.  The script
restores every mutated file byte-identically and verifies sha256.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
SERVICE = "backend/paper_risk_service.py"
EVIDENCE = "backend/paper_risk_evidence.py"
PAPER_TRADING = "backend/paper_trading.py"
PLANNER = "backend/execution_planner.py"
REPLACEMENT_EVIDENCE = "backend/paper_replacement_evidence.py"

SERVICE_TEST = "test_risk_application_service.RiskServiceContractTests"
STRATEGY_VERSIONING_TEST = "test_strategy_versioning.StrategyVersioningTests"
GUARD_TEST = "test_paper_trading_architecture_guard.RiskApplicationServiceBoundary"

MUTATIONS = [
    {
        "id": "M-RSK1", "file": SERVICE,
        "old": "            conn, cycle_id=cycle_id, asof_day=day,\n",
        "new": "            conn, asof_day=day,\n",
        "test": f"{SERVICE_TEST}.test_rsvc2_historical_capacity_uses_bounded_budget",
        "desc": "dynamic limits drop cycle_id",
    },
    {
        "id": "M-RSK2", "file": SERVICE,
        "old": "            conn, cycle_id=cycle_id, asof_day=day,\n",
        "new": "            conn, cycle_id=cycle_id,\n",
        "test": f"{SERVICE_TEST}.test_rsvc2_historical_capacity_uses_bounded_budget",
        "desc": "dynamic limits drop asof_day",
    },
    {
        "id": "M-RSK3", "file": SERVICE,
        "old": "                    asof_day=day, conn=conn, cycle_id=cycle_id,\n",
        "new": "                    conn=conn, cycle_id=cycle_id,\n",
        "test": f"{SERVICE_TEST}.test_rsvc3_historical_downside_policy_is_cycle_asof_bound",
        "desc": "downside profile drops asof_day",
    },
    {
        "id": "M-RSK4", "file": SERVICE,
        "old": "                    asof_day=day, conn=conn, cycle_id=cycle_id,\n",
        "new": "                    asof_day=day, conn=conn,\n",
        "test": f"{SERVICE_TEST}.test_rsvc3_historical_downside_policy_is_cycle_asof_bound",
        "desc": "downside profile drops cycle_id",
    },
    {
        "id": "M-RSK5", "file": SERVICE,
        "old": "                spec_override=SRE.effective_spec_for_cycle(\n",
        "new": "                spec_override=SRE.effective_spec(\n",
        "test": f"{SERVICE_TEST}.test_rsvc4_sell_spec_uses_cycle_pin_not_current_head",
        "desc": "SELL spec reverts to current compiled profile",
    },
    {
        "id": "M-RSK6", "file": SERVICE,
        "old": "    cycle_id = context.cycle_id\n",
        "new": "    cycle_id = context.cycle_id\n    cycle_id = _active_cycle()\n",
        "test": f"{GUARD_TEST}.test_guard12c_explicit_context_and_no_active_cycle_resolution",
        "desc": "risk run re-resolves active cycle",
    },
    {
        "id": "M-RSK7", "file": SERVICE,
        "old": "        PRSS.assert_cycle_active(conn, cycle_id=cycle_id)\n",
        "new": "        # mutation: external-I/O cycle fence removed\n",
        "test": f"{SERVICE_TEST}.test_rsvc5_external_io_cycle_rollover_fails_closed",
        "desc": "external-I/O cycle fence removed",
    },
    {
        "id": "M-RSK8", "file": PAPER_TRADING,
        "old": "    return PRSVC.run(context, ports=_risk_service_ports())\n",
        "new": "    return _r21_bypass_run(context, ports=_risk_service_ports())\n",
        "test": f"{GUARD_TEST}.test_guard12b_paper_trading_facade_is_thin",
        "desc": "monitor_risk bypasses paper_risk_service",
    },
    {
        "id": "M-RSK9", "file": SERVICE,
        "old": "from __future__ import annotations\n",
        "new": "from __future__ import annotations\nimport paper_trading  # mutation: reverse import\n",
        "test": f"{GUARD_TEST}.test_guard12a_no_reverse_dependency",
        "desc": "paper_risk_service reverse-imports paper_trading",
    },
    {
        "id": "M-RSK10", "file": SERVICE,
        "old": "                EP.commit_fill(\n",
        "new": "                _r21_bypass_commit_fill(\n",
        "test": f"{SERVICE_TEST}.test_rsvc7_risk_sell_commits_through_execution_planner_once",
        "desc": "risk SELL bypasses execution_planner.commit_fill",
    },
    {
        "id": "M-RSK11", "file": SERVICE,
        "old": "                    sell_next_take_stage=next_stage,\n",
        "new": "                    sell_next_take_stage=0,  # mutation: reset stage\n",
        "test": f"{SERVICE_TEST}.test_rsvc8_partial_risk_sell_advances_take_stage",
        "desc": "partial SELL does not advance take_stage",
    },
    {
        "id": "M-RSK12", "file": PLANNER,
        "old": "        PPRS.finalize_sell(\n",
        "new": "        _r21_skip_finalize_sell(\n",
        "test": f"{SERVICE_TEST}.test_rsvc9_full_risk_sell_closes_episode_from_authoritative_lots",
        "desc": "full SELL does not finalize episode",
    },
    {
        "id": "M-RSK13", "file": REPLACEMENT_EVIDENCE,
        "old": '"   AND intended_date=? AND signal_date<=?"\n',
        "new": '"   AND intended_date=? AND (? IS NOT NULL OR 1=1)"\n',
        "test": f"{SERVICE_TEST}.test_rsvc11_replacement_candidate_is_intended_asof_and_signal_bounded",
        "desc": "replacement query drops signal_date upper bound",
    },
    {
        "id": "M-RSK14", "file": SERVICE,
        "old": "    result = ports.rotation_buy(\n",
        "new": "    result = EP.commit_fill(\n",
        "test": f"{SERVICE_TEST}.test_rsvc12_rotation_buy_delegates_to_existing_buy_adapter",
        "desc": "rotation BUY bypasses existing BUY adapter",
    },
    {
        "id": "M-RSK15", "file": SERVICE,
        "old": "        try:\n            manual_orders = ports.process_pending_manual_orders(day)\n",
        "new": "        try:\n            manual_orders = []  # mutation: skip empty pending batch\n",
        "test": f"{SERVICE_TEST}.test_rsvc14_pending_manual_runs_on_empty_and_nonempty_paths",
        "desc": "empty branch skips pending manual processing",
    },
    {
        "id": "M-RSK16", "file": SERVICE,
        "old": "        positions.sort(key=lambda item: (\n            0 if (item[\"account_id\"], item[\"code\"]) in permission_exit_reasons else 1,\n            0 if (item[\"account_id\"], item[\"code\"]) in capacity_exit_reasons else 1,\n            _num((quality_reviews.get((item[\"account_id\"], item[\"code\"])) or {}).get(\"score\"), 100.0),\n        ))\n",
        "new": "        positions.sort(key=lambda item: 0)  # mutation: insertion order\n",
        "test": f"{SERVICE_TEST}.test_rsvc10_quality_capacity_permission_order_is_stable",
        "desc": "quality exit sorting reverts to insertion order",
    },
    {
        "id": "M-RSK17", "file": SERVICE,
        "old": "        context = SRT.get_context_for_cycle(conn, account_id, cycle_id=cycle_id)\n",
        "new": "        context = SRT.get_context(conn, account_id)\n",
        "test": f"{SERVICE_TEST}.test_rsvc15_user_sell_base_policy_uses_cycle_pinned_version",
        "desc": "user SELL base spec follows current strategy head",
    },
    {
        "id": "M-RSK18", "file": SERVICE,
        "old": "    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=cycle_id)\n",
        "new": "    stamp = SR.stamp_for_account(conn, account_id)\n",
        "test": f"{SERVICE_TEST}.test_rsvc16_risk_facts_use_cycle_pinned_strategy_provenance",
        "desc": "risk facts fall back to legacy/current strategy stamp",
    },
    {
        "id": "M-RSK19", "file": PLANNER,
        "old": "        risk_log_reason or reason, fill_detail,\n        strategy_stamp=order_strategy_stamp,\n    )\n",
        "new": "        risk_log_reason or reason, fill_detail,\n    )\n",
        "test": f"{SERVICE_TEST}.test_rsvc17_filled_sell_inherits_durable_order_provenance",
        "desc": "filled risk log ignores durable order strategy stamp",
    },
    {
        "id": "M-RSK20", "file": "backend/paper_schema_migrations.py",
        "old": 'STRATEGY_STAMP_UNKNOWN_ALLOWANCE = {\n    "paper_orders": (\n',
        "new": 'STRATEGY_STAMP_UNKNOWN_ALLOWANCE = {\n    "paper_signals": "1=1",\n    "paper_orders": (\n',
        "test": f"{STRATEGY_VERSIONING_TEST}.test_db_strat_1_signal_all_null_rejected",
        "desc": "all-NULL unknown exception is widened to paper_signals",
    },
    {
        "id": "M-RSK21", "file": SERVICE,
        "old": '''                _audit(
                    conn, position["account_id"], "concentration_rotation",
                    f"{position['code']} 质量评分 {quality_review.get('score', 0):.1f}，释放额度等待高分候选 {((quality_review.get('replacement') or {}).get('code') or '下一轮选股')}",
                    strategy_stamp=strategy_stamp,
                )
''',
        "new": '''                _audit(
                    conn, position["account_id"], "concentration_rotation",
                    f"{position['code']} 质量评分 {quality_review.get('score', 0):.1f}，释放额度等待高分候选 {((quality_review.get('replacement') or {}).get('code') or '下一轮选股')}",
                )
''',
        "test": f"{SERVICE_TEST}.test_rsvc18_post_fill_rotation_audits_inherit_sell_provenance",
        "desc": "post-fill concentration rotation ignores causal SELL strategy stamp",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r21_mutation_pycache_")
_SEQ = [0]


def run_test(target: str) -> subprocess.CompletedProcess:
    _SEQ[0] += 1
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = os.path.join(PYCACHE_ROOT, f"run{_SEQ[0]:03d}")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=BACKEND, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=1800, env=env,
    )


def main() -> int:
    print(f"repo root: {ROOT}")
    for module in (
        f"{SERVICE_TEST}.test_rsvc1_risk_run_context_requires_explicit_identity",
        f"{GUARD_TEST}.test_guard12a_no_reverse_dependency",
        f"{SERVICE_TEST}.test_rsvc15_user_sell_base_policy_uses_cycle_pinned_version",
        f"{SERVICE_TEST}.test_rsvc16_risk_facts_use_cycle_pinned_strategy_provenance",
        f"{SERVICE_TEST}.test_rsvc17_filled_sell_inherits_durable_order_provenance",
        f"{SERVICE_TEST}.test_rsvc18_post_fill_rotation_audits_inherit_sell_provenance",
        f"{STRATEGY_VERSIONING_TEST}.test_db_strat_1_signal_all_null_rejected",
    ):
        base = run_test(module)
        if base.returncode != 0:
            print(f"BASELINE FAILED: {module}")
            print(base.stdout[-5000:], base.stderr[-5000:])
            return 2
    print("BASELINE: RSVC and Guard 12 green\n")

    results = []
    for mut in MUTATIONS:
        path = os.path.join(ROOT, mut["file"])
        original = open(path, "rb").read()
        before_sha = sha256(original)
        try:
            old = _adapt_eol(mut["old"], original)
            new = _adapt_eol(mut["new"], original)
            count = original.count(old)
            if count != 1:
                raise AssertionError(
                    f"mutation anchor count != 1 for {mut['id']} in {mut['file']}: {count}")
            mutated = original.replace(old, new, 1)
            if mutated == original:
                raise AssertionError(f"mutation did not change bytes: {mut['id']}")
            with open(path, "wb") as handle:
                handle.write(mutated)

            target = mut["test"]
            method = target.rsplit(".", 1)[-1]
            proc = run_test(target)
            combined = proc.stdout + proc.stderr
            caught = proc.returncode != 0 and (
                f"FAIL: {method}" in combined or f"ERROR: {method}" in combined
            )
            print(f"[{mut['id']}] {mut['desc']}")
            print(f"    -> {'RED' if caught else 'SUSPECT'} (rc={proc.returncode})")
            if not caught:
                print(combined[-4000:])
            results.append((mut["id"], caught, mut["desc"]))
        finally:
            with open(path, "wb") as handle:
                handle.write(original)
            restored = open(path, "rb").read()
            if sha256(restored) != before_sha or restored != original:
                print(f"!!!! restore failed for {mut['id']}")
                os._exit(3)

    print("\n===== R21 mutation summary =====")
    for mid, caught, desc in results:
        print(f"  {mid:<8} {'RED' if caught else 'SUSPECT':<8} {desc}")
    bad = [mid for mid, caught, _ in results if not caught]
    if bad:
        print(f"\nRESULT: {len(bad)} mutation(s) NOT confirmed -> DO NOT TRUST GATE")
        return 1
    print(f"\nRESULT: {len(results)}/{len(results)} mutations RED, "
          "all files restored byte-identical")
    return 0


if __name__ == "__main__":
    code = main()
    shutil.rmtree(PYCACHE_ROOT, ignore_errors=True)
    raise SystemExit(code)
