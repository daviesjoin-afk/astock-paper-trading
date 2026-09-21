# -*- coding: utf-8 -*-
"""R21 final before-fix reproduction for C7/C8."""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import test_risk_application_service as RSVC  # noqa: E402


def _new_case():
    case = RSVC.RiskServiceContractTests(
        "test_rsvc13_risk_log_and_audit_counts_stay_exactly_once")
    case.setUp()
    return case


def c7_filled_sell_downstream_provenance_split_brain():
    case = _new_case()
    try:
        pinned = case.seed_user_strategy_v1()
        case.advance_user_head(pinned)
        # Missing-pin protective exit: the claimed cycle is still the account's
        # execution cycle, but its strategy binding is absent.  The order is
        # explicitly unknown, while the downstream legacy log/audit lookup can
        # still adopt current head v2.
        case.delete_user_cycle_binding()
        case.add_lot_for_account(case.USER, case.code, 100, 10.0)
        case.set_quote(case.code, price=9.0, pct=-8.0, high=9.2, low=8.9)
        case.run_risk()
        order = case.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum,status"
            " FROM paper_orders WHERE account_id=? AND code=? AND side='sell'"
            " ORDER BY id DESC LIMIT 1",
            (case.USER, case.code),
        ).fetchone()
        decision = case.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_risk_decisions WHERE account_id=? AND code=? AND side='sell'"
            " ORDER BY id DESC LIMIT 1",
            (case.USER, case.code),
        ).fetchone()
        audit = case.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_audit WHERE account_id=? AND event='sell_filled'"
            " ORDER BY id DESC LIMIT 1",
            (case.USER,),
        ).fetchone()
        order_stamp = (
            order["strategy_id"], order["strategy_version"], order["strategy_checksum"],
        )
        decision_stamp = (
            decision["strategy_id"], decision["strategy_version"], decision["strategy_checksum"],
        ) if decision is not None else (None, None, None)
        audit_stamp = (
            audit["strategy_id"], audit["strategy_version"], audit["strategy_checksum"],
        ) if audit is not None else (None, None, None)
        order_filled = bool(order is not None and order["status"] == "filled")
        reproduced = (
            order_filled
            and order_stamp == (None, None, None)
            and (
                decision_stamp[1] == pinned.version + 1
                or audit_stamp[1] == pinned.version + 1
            )
        )
        print(
            "R21-C7 filled SELL downstream provenance re-resolves current head: "
            f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}"
        )
        print(
            f"    order={order_stamp} decision={decision_stamp} audit={audit_stamp} "
            f"pinned_v={pinned.version}"
        )
        return reproduced
    finally:
        case.doCleanups()


def c8_all_null_guard_scope():
    case = _new_case()
    try:
        signal_ok = False
        buy_ok = False
        try:
            case.conn.execute(
                "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,"
                "payload,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (case.ACCOUNT, case.day.isoformat(), case.day.isoformat(), "600001",
                 "{}", "pending", f"{case.day.isoformat()} 09:00:00"),
            )
            case.conn.commit()
            signal_ok = True
        except sqlite3.IntegrityError:
            case.conn.rollback()
        try:
            case.conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,qty,status,risk_payload,"
                "created_at,order_type,origin,cycle_id) VALUES(?,'buy',?,?,?,?,?,?,?,?)",
                (case.ACCOUNT, "600001", 100, "pending_execution", "{}",
                 f"{case.day.isoformat()} 09:00:00", "market", "strategy", case.cycle),
            )
            case.conn.commit()
            buy_ok = True
        except sqlite3.IntegrityError:
            case.conn.rollback()
        reproduced = signal_ok or buy_ok
        print(
            "R21-C8 all-NULL strategy provenance is accepted outside protective SELL: "
            f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}"
        )
        print(f"    paper_signals_insert_ok={signal_ok} paper_orders_buy_insert_ok={buy_ok}")
        return reproduced
    finally:
        case.doCleanups()


def main():
    results = [c7_filled_sell_downstream_provenance_split_brain(), c8_all_null_guard_scope()]
    print(f"R21 final before-fix reproduced: {sum(1 for item in results if item)}/2")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
