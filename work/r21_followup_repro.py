# -*- coding: utf-8 -*-
"""R21 follow-up before-fix reproduction (C5/C6).

Run from the repository root:

    python work/r21_followup_repro.py

The script deliberately calls the production helpers in a version-tolerant
way: before the fix they do not accept ``cycle_id``; after the fix they do.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_risk_service as PRSVC  # noqa: E402
import strategy_registry as SR  # noqa: E402
import paper_trading as PT  # noqa: E402
import test_entry_capital_asof as EC  # noqa: E402


def _new_case():
    case = EC.CyclePinnedStrategyVersionIsUsed(
        "test_ec15_cycle_pinned_version_beats_a_later_current_head")
    case.setUp()
    return case


def _call_spec_for(conn, account_id, *, cycle_id):
    try:
        return PRSVC._spec_for(account_id, conn, cycle_id=cycle_id)
    except TypeError:
        # R21 base signature is ``_spec_for(account_id, conn=None)``.
        return PRSVC._spec_for(account_id, conn)


def _call_strategy_stamp(conn, account_id, *, cycle_id):
    try:
        return PRSVC._strategy_stamp(conn, account_id, cycle_id=cycle_id)
    except TypeError:
        # R21 base signature is ``_strategy_stamp(conn, account_id, signal_id=None)``.
        return PRSVC._strategy_stamp(conn, account_id)


def c5_user_sell_base_policy_leak():
    case = _new_case()
    try:
        cycle = case.cycle_id()
        pinned_version = case._seed_pinned_cycle(cycle_id=cycle)
        account_id = case.USER
        with PT._db() as conn:
            # At this point the current head is still v1; this is the exact
            # cycle-pinned policy the historical scan is supposed to use.
            pinned_spec = _call_spec_for(conn, account_id, cycle_id=cycle)
        case._advance_head(expected_version=pinned_version)
        with PT._db() as conn:
            current_spec = _call_spec_for(conn, account_id, cycle_id=cycle)
            current_context = PRSVC.SRT.get_context(conn, account_id)
        key_difference = (
            pinned_spec.get("hard_stop") != current_spec.get("hard_stop")
            or pinned_spec.get("hold_max") != current_spec.get("hold_max")
            or pinned_spec.get("trail_stop") != current_spec.get("trail_stop")
        )
        reproduced = (
            key_difference
            and current_spec.get("strategy_version") == f"v{current_context.version}"
            and current_spec != pinned_spec
        )
        print(
            "R21-C5 user SELL base policy leaks current strategy head: "
            f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}"
        )
        print(
            f"    pinned={pinned_spec.get('strategy_version')}/"
            f"hard_stop={pinned_spec.get('hard_stop')}/hold_max={pinned_spec.get('hold_max')} "
            f"current={current_spec.get('strategy_version')}/"
            f"hard_stop={current_spec.get('hard_stop')}/hold_max={current_spec.get('hold_max')}"
        )
        return reproduced
    finally:
        try:
            case.tearDown()
        finally:
            case.doCleanups()


def c6_strategy_stamp_leak():
    case = _new_case()
    try:
        cycle = case.cycle_id()
        pinned_version = case._seed_pinned_cycle(cycle_id=cycle)
        account_id = case.USER
        with PT._db() as conn:
            pinned_stamp = _call_strategy_stamp(conn, account_id, cycle_id=cycle)
        case._advance_head(expected_version=pinned_version)
        with PT._db() as conn:
            current_version = SR.get_version(account_id, conn=conn)
            current_stamp = _call_strategy_stamp(conn, account_id, cycle_id=cycle)
        # Missing-pin case: explicit cycle exists, binding is absent, current
        # user head exists.  The base helper may adopt the current head.
        with PT._db(immediate=True) as conn:
            conn.execute(
                "DELETE FROM paper_cycle_strategy_versions WHERE cycle_id=? AND account_id=?",
                (int(cycle), account_id),
            )
            try:
                conn.execute(
                    "DELETE FROM paper_strategy_legacy_bindings WHERE account_id=?",
                    (account_id,),
                )
            except Exception:
                pass
        with PT._db() as conn:
            missing_stamp = _call_strategy_stamp(conn, account_id, cycle_id=cycle)
        current_leak = (
            current_stamp[1] == current_version.version
            and current_stamp[1] != pinned_stamp[1]
        )
        missing_leak = (
            missing_stamp[1] == current_version.version
            and missing_stamp[1] != pinned_stamp[1]
        )
        reproduced = current_leak or missing_leak
        print(
            "R21-C6 risk facts stamp current strategy version instead of cycle pin: "
            f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}"
        )
        print(
            f"    pinned_stamp={pinned_stamp} current_stamp={current_stamp} "
            f"missing_stamp={missing_stamp} current_head=v{current_version.version}"
        )
        return reproduced
    finally:
        try:
            case.tearDown()
        finally:
            case.doCleanups()


def main():
    results = [c5_user_sell_base_policy_leak(), c6_strategy_stamp_leak()]
    print(f"R21 follow-up before-fix reproduced: {sum(1 for item in results if item)}/2")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
