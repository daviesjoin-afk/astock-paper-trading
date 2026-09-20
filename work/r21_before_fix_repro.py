# -*- coding: utf-8 -*-
"""R21 before-fix reproduction on the R20 merge base.

Run from the repository root:

    python work/r21_before_fix_repro.py

The probe drives the real ``PT.monitor_risk`` production path with its existing
fixtures and records exactly which provenance the risk scan passes to the three
bounded capital/risk helpers.
"""
from __future__ import annotations

import ast
import datetime as dt
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_trading as PT  # noqa: E402
import strategy_registry as SR  # noqa: E402
import strategy_risk_enforcement as SRE  # noqa: E402
import test_position_risk_state as PRS  # noqa: E402


ACCOUNT = "tq_breakout"


def _set_future_adaptive_risk(day, *, max_exposure=0.30, warning_pct=-4.0):
    effective = (day + dt.timedelta(days=1)).isoformat()
    with PT._db(immediate=True) as conn:
        row = conn.execute(
            "SELECT params FROM paper_accounts WHERE id=?", (ACCOUNT,)
        ).fetchone()
        params = PT._loads(row["params"] if row is not None else None, {}) or {}
        params["adaptive_risk"] = {
            "max_exposure": max_exposure,
            "downside_warning_pct": warning_pct,
        }
        params["adaptive_risk_meta"] = {
            "status": "active",
            "effective_date": effective,
            "version": "r21-before-fix",
            "candidate_id": "r21-repro",
        }
        conn.execute(
            "UPDATE paper_accounts SET params=? WHERE id=?",
            (PT._json(params), ACCOUNT),
        )
    return effective


def _fresh_non_sell_quote(case):
    case._inner._set_fresh_exit_quote(
        case.code, price=10.2, pct=1.0, high=10.3, low=10.1,
    )


def _advance_head_after_pin(case):
    with PT._db(immediate=True) as conn:
        SR.bind_cycle_versions(conn, case.cycle, (ACCOUNT,))
        current = SR.get_version(ACCOUNT, conn=conn)
        SR.save_definition(
            conn, ACCOUNT,
            {"metadata": {
                "style": "trend", "hold": 2, "daily": True, "positions": 2,
            }},
            expected_version=current.version,
            actor="r21-before-fix",
            change_note="r21 before-fix head advance",
            risk_evidence=20,
        )


def _new_risk_case():
    case = PRS.RiskScanFullExitClosesEpisode(
        "test_risk_full_exit_deletes_position_risk_state")
    case.setUp()
    with PT._db(immediate=True) as conn:
        conn.execute(
            "UPDATE paper_accounts SET status='running', cycle_id=? WHERE id=?",
            (case.cycle, ACCOUNT),
        )
    case.add_lot(100, 10.0)
    _fresh_non_sell_quote(case)
    return case


def c1_capacity_budget_leak():
    case = _new_risk_case()
    try:
        _set_future_adaptive_risk(case.day)
        _advance_head_after_pin(case)
        with PT._db() as conn:
            bounded = PT._dynamic_position_limits(
                conn, cycle_id=case.cycle, asof_day=case.day)
            current = PT._dynamic_position_limits(conn)
        calls = []
        original = PT._dynamic_position_limits

        def spy(conn, *args, **kwargs):
            calls.append((tuple(args), dict(kwargs)))
            return original(conn, *args, **kwargs)

        with mock.patch.object(PT, "_dynamic_position_limits", side_effect=spy):
            PT.monitor_risk(case.day)
        unbounded_calls = [
            item for item in calls
            if item[0] == () and "cycle_id" not in item[1] and "asof_day" not in item[1]
        ]
        leaked = (
            bounded["weights"] != current["weights"]
            or bounded["limits"] != current["limits"]
            or bounded["pool_limit"] != current["pool_limit"]
        )
        reproduced = bool(unbounded_calls) and leaked
        print(
            "R21-C1 risk scan capacity budget leaks current facts: "
            f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}"
        )
        print(
            f"    scan_call_without_context={bool(unbounded_calls)} "
            f"bounded_weights={bounded['weights']} "
            f"current_weights={current['weights']}"
        )
        return reproduced
    finally:
        case.doCleanups()


def c2_downside_policy_leak():
    case = _new_risk_case()
    try:
        _set_future_adaptive_risk(case.day)
        captured = {}
        original = PT._intraday_downside_guard

        def spy(position, quote, **kwargs):
            captured.update(kwargs)
            return original(position, quote, **kwargs)

        with mock.patch.object(PT, "_intraday_downside_guard", side_effect=spy):
            PT.monitor_risk(case.day)
        override = captured.get("policy_override") or {}
        with PT._db() as conn:
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT,)).fetchone())
            bounded = PT._risk_profile(
                account, asof_day=case.day, conn=conn, cycle_id=case.cycle)
        leaked_value = override.get("downside_warning_pct")
        bounded_value = bounded.get("downside_warning_pct")
        reproduced = leaked_value == -4.0 and leaked_value != bounded_value
        print(
            "R21-C2 downside policy leaks future/current risk profile: "
            f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}"
        )
        print(
            f"    scan_override_warning={leaked_value} "
            f"bounded_warning={bounded_value}"
        )
        return reproduced
    finally:
        case.doCleanups()


def c3_sell_spec_current_head_leak():
    case = _new_risk_case()
    try:
        with PT._db(immediate=True) as conn:
            SR.bind_cycle_versions(conn, case.cycle, (ACCOUNT,))
            current = SR.get_version(ACCOUNT, conn=conn)
            base_spec = PT.ACCOUNT_SPECS.get(ACCOUNT) or {}
            # Advance the current head after the cycle was pinned.  Only a
            # deliberate tightening is used, so the policy gate accepts it.
            SR.save_definition(
                conn, ACCOUNT,
                {"metadata": {
                    "style": "trend", "hold": 2, "daily": True, "positions": 2,
                }},
                expected_version=current.version,
                actor="r21-before-fix",
                change_note="r21 before-fix head advance",
                risk_evidence=20,
            )
            current_spec = SRE.effective_spec(conn, ACCOUNT, base_spec)
            pinned_after = SRE.effective_spec_for_cycle(
                conn, ACCOUNT, base_spec, cycle_id=case.cycle)
        calls = []
        original_spec = SRE.effective_spec

        def spy(conn, account_id, base, *args, **kwargs):
            result = original_spec(conn, account_id, base, *args, **kwargs)
            calls.append({
                "account_id": account_id,
                "cycle_id": kwargs.get("cycle_id"),
                "result": result,
            })
            return result

        with mock.patch.object(SRE, "effective_spec", side_effect=spy):
            PT.monitor_risk(case.day)
        scan_used_current = any(
            item["account_id"] == ACCOUNT and item["cycle_id"] is None
            for item in calls
        )
        distinct = current_spec != pinned_after
        reproduced = scan_used_current and distinct
        print(
            "R21-C3 sell policy leaks current strategy head: "
            f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}"
        )
        print(
            f"    scan_uses_unbounded_effective_spec={scan_used_current} "
            f"head_vs_pin_distinct={distinct} "
            f"head_hard_stop={current_spec.get('hard_stop')} "
            f"pin_hard_stop={pinned_after.get('hard_stop')}"
        )
        return reproduced
    finally:
        case.doCleanups()


def c4_application_orchestration_baseline():
    path = os.path.join(BACKEND, "paper_trading.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    target = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_monitor_risk_impl"
    )
    loc = target.end_lineno - target.lineno + 1
    calls = sorted({ast.unparse(node.func) for node in ast.walk(target)
                    if isinstance(node, ast.Call)})
    expected = {
        "snapshot", "external evidence", "quality review", "capacity review",
        "sell decision", "sell execution orchestration", "rotation",
        "projection/nav", "pending manual retry",
    }
    responsibilities = {
        "snapshot": any("positions_for_cycle" in call for call in calls),
        "external evidence": any(name.startswith("_quotes") or name.startswith("_news_for")
                                 for name in calls),
        "quality review": any("_position_quality_score" in call for call in calls),
        "capacity review": any("_over_capacity_exit_candidates" in call for call in calls),
        "sell decision": any("_sell_plan" in call for call in calls),
        "sell execution orchestration": any("EP.commit_fill" in call for call in calls),
        "rotation": any("_rotation_buy_candidate" in call for call in calls),
        "projection/nav": any("_record_nav" in call for call in calls),
        "pending manual retry": any("process_pending_manual_orders" in call for call in calls),
    }
    reproduced = loc >= 600 and responsibilities == {key: True for key in expected}
    print(
        "R21-C4 risk application orchestration remains in paper_trading: "
        f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}"
    )
    print(f"    _monitor_risk_impl LOC={loc} responsibilities={responsibilities}")
    return reproduced


def main():
    results = [
        c1_capacity_budget_leak(),
        c2_downside_policy_leak(),
        c3_sell_spec_current_head_leak(),
        c4_application_orchestration_baseline(),
    ]
    print(f"R21 before-fix reproduced: {sum(1 for item in results if item)}/4")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())


