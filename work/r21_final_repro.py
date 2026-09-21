# -*- coding: utf-8 -*-
"""R21 final before-fix reproduction for C7/C8/C9."""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import test_risk_application_service as RSVC  # noqa: E402
import strategy_registry as SR  # noqa: E402


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



def _stamp(row):
    if row is None:
        return (None, None, None)
    return (
        row["strategy_id"], row["strategy_version"], row["strategy_checksum"],
    )


def _seed_over_capacity_sell(case, *, keep_cycle_pin):
    """Create a real sellable over-capacity position for a logged-in user."""
    pinned = case.seed_user_strategy_v1()
    case.advance_user_head(pinned)
    head = SR.get_version(case.USER, conn=case.conn)
    assert pinned.version != head.version
    assert pinned.checksum != head.checksum
    # A user strategy may have no legacy binding while still retaining its
    # explicit cycle pin.  Removing the legacy row makes the fallback resolver
    # exercise the current-head path that this regression targets.
    case.conn.execute(
        "DELETE FROM paper_strategy_legacy_bindings WHERE account_id=?",
        (case.USER,),
    )
    case.conn.commit()
    if not keep_cycle_pin:
        case.delete_user_cycle_binding()
    # The built-in fixture has five default positions; six real lots force the
    # existing over-capacity branch without stubbing audit writes.
    for code in ("600001", "600002", "600003", "600004", "600005", "600006"):
        case.add_lot_for_account(case.USER, code, 100, 10.0)
        case.set_quote(code, price=10.0, pct=0.5, high=10.2, low=9.8)
    result = case.run_risk()
    return pinned, head, result


def c9_post_fill_rotation_audit_provenance_split_brain():
    """Prove post-fill rotation audits can re-resolve current strategy head."""
    reproduced = []
    details = []

    # Case A: cycle pin v1, current head v2, no legacy binding.
    case = _new_case()
    try:
        pinned, head, result = _seed_over_capacity_sell(case, keep_cycle_pin=True)
        order = case.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum,status"
            " FROM paper_orders WHERE account_id=? AND side='sell'"
            " ORDER BY id DESC LIMIT 1",
            (case.USER,),
        ).fetchone()
        decision = case.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_risk_decisions WHERE account_id=? AND side='sell'"
            " AND decision='filled' ORDER BY id DESC LIMIT 1",
            (case.USER,),
        ).fetchone()
        sell_filled = case.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_audit WHERE account_id=? AND event='sell_filled'"
            " ORDER BY id DESC LIMIT 1",
            (case.USER,),
        ).fetchone()
        rotation = case.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_audit WHERE account_id=? AND event='concentration_rotation'"
            " ORDER BY id DESC LIMIT 1",
            (case.USER,),
        ).fetchone()
        sell_rows = [
            item for item in result.get("orders", [])
            if item.get("status") == "filled" and item.get("concentration_rotation")
        ]
        case_reproduced = bool(
            sell_rows
            and order is not None and order["status"] == "filled"
            and _stamp(order) == (pinned.strategy_id, pinned.version, pinned.checksum)
            and _stamp(decision) == (pinned.strategy_id, pinned.version, pinned.checksum)
            and _stamp(sell_filled) == (pinned.strategy_id, pinned.version, pinned.checksum)
            and _stamp(rotation) == (head.strategy_id, head.version, head.checksum)
        )
        reproduced.append(case_reproduced)
        details.append(
            "pinned_case order=%s decision=%s sell_filled=%s rotation=%s "
            "pinned_v=%s head_v=%s" % (
                _stamp(order), _stamp(decision), _stamp(sell_filled), _stamp(rotation),
                pinned.version, head.version,
            )
        )
    finally:
        case.doCleanups()

    # Case B: cycle pin absent, current head v2 still exists.
    case = _new_case()
    try:
        pinned, head, result = _seed_over_capacity_sell(case, keep_cycle_pin=False)
        order = case.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum,status"
            " FROM paper_orders WHERE account_id=? AND side='sell'"
            " ORDER BY id DESC LIMIT 1",
            (case.USER,),
        ).fetchone()
        decision = case.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_risk_decisions WHERE account_id=? AND side='sell'"
            " AND decision='filled' ORDER BY id DESC LIMIT 1",
            (case.USER,),
        ).fetchone()
        sell_filled = case.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_audit WHERE account_id=? AND event='sell_filled'"
            " ORDER BY id DESC LIMIT 1",
            (case.USER,),
        ).fetchone()
        rotation = case.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_audit WHERE account_id=? AND event='concentration_rotation'"
            " ORDER BY id DESC LIMIT 1",
            (case.USER,),
        ).fetchone()
        sell_rows = [
            item for item in result.get("orders", [])
            if item.get("status") == "filled" and item.get("concentration_rotation")
        ]
        case_reproduced = bool(
            sell_rows
            and order is not None and order["status"] == "filled"
            and _stamp(order) == (None, None, None)
            and _stamp(decision) == (None, None, None)
            and _stamp(sell_filled) == (None, None, None)
            and _stamp(rotation) == (head.strategy_id, head.version, head.checksum)
        )
        reproduced.append(case_reproduced)
        details.append(
            "missing_pin order=%s decision=%s sell_filled=%s rotation=%s head_v=%s" % (
                _stamp(order), _stamp(decision), _stamp(sell_filled), _stamp(rotation),
                head.version,
            )
        )
    finally:
        case.doCleanups()

    for detail in details:
        print(f"    {detail}")
    is_reproduced = any(reproduced)
    print(
        "R21-C9 post-fill rotation audit re-resolves current strategy head: "
        f"{'REPRODUCED' if is_reproduced else 'NOT REPRODUCED'}"
    )
    return is_reproduced



def main():
    results = [
        c7_filled_sell_downstream_provenance_split_brain(),
        c8_all_null_guard_scope(),
        c9_post_fill_rotation_audit_provenance_split_brain(),
    ]
    print(f"R21 final before-fix reproduced: {sum(1 for item in results if item)}/3")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
