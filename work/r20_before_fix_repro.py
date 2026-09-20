# -*- coding: utf-8 -*-
"""R20 before-fix reproduction.

Run from the repository root:

    python work/r20_before_fix_repro.py

The script uses the existing production fixtures rather than hand-written
ledger rows.  It proves the four pre-R20 SELL commit gaps.
"""
from __future__ import annotations

import ast
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import execution_planner as EP  # noqa: E402
import paper_trading as PT  # noqa: E402
import test_position_risk_state as PRS  # noqa: E402


def _commit_sentinel(calls):
    original = EP.commit_fill

    def sentinel(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    return sentinel


def _runtime_fill_writers(path):
    raw = open(path, encoding="utf-8").read()
    tree = ast.parse(raw)
    writers = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Constant) or not isinstance(child.value, str):
                continue
            normalized = " ".join(child.value.upper().split())
            if "INSERT INTO PAPER_FILLS" in normalized:
                writers.append(node.name)
                break
    return sorted(set(writers))


def c1_risk_sell_bypass():
    case = PRS.RiskScanFullExitClosesEpisode(
        "test_risk_full_exit_deletes_position_risk_state")
    case.setUp()
    try:
        case.add_lot(100, 10.0)
        case._inner._set_fresh_exit_quote(case.code, price=9.0, pct=-8.0)
        calls = []
        with mock.patch.object(EP, "commit_fill", side_effect=_commit_sentinel(calls)):
            result = PT.monitor_risk(case.day)
        fills = case.sell_fills()
        filled = [item for item in result.get("orders", [])
                  if item.get("status") == "filled"]
        reproduced = bool(filled) and len(fills) == 1 and not calls
        print(f"R20-C1 risk sell bypasses centralized fill commit: "
              f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
        print(f"    fills={len(fills)} commit_fill_calls={len(calls)}")
        return reproduced
    finally:
        case.doCleanups()


def c2_intraday_sell_bypass():
    case = PRS.IntradaySellClosesEpisode(
        "test_intraday_full_sell_deletes_position_risk_state")
    case.setUp()
    try:
        case.add_lot(100, 10.0)
        case._set_t_sell_quote()
        calls = []
        with mock.patch.object(EP, "commit_fill", side_effect=_commit_sentinel(calls)):
            action, reason = case._drive()
        fills = case.sell_fills()
        reproduced = action is not None and len(fills) == 1 and not calls
        print(f"R20-C2 intraday sell bypasses centralized fill commit: "
              f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
        print(f"    fills={len(fills)} commit_fill_calls={len(calls)} reason={reason}")
        return reproduced
    finally:
        case.doCleanups()


def c3_multiple_fill_writers():
    pt_writers = _runtime_fill_writers(os.path.join(BACKEND, "paper_trading.py"))
    ep_writers = _runtime_fill_writers(os.path.join(BACKEND, "execution_planner.py"))
    reproduced = bool(pt_writers) and "commit_fill" in ep_writers
    print(f"R20-C3 multiple runtime paper_fills writers: "
          f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
    print(f"    paper_trading={pt_writers} execution_planner={ep_writers}")
    return reproduced


def c4_intraday_sell_not_atomic():
    case = PRS.IntradaySellClosesEpisode(
        "test_intraday_full_sell_deletes_position_risk_state")
    case.setUp()
    try:
        case.add_lot(100, 10.0)
        case._set_t_sell_quote()
        cash_before = PT._shared_cash(case.conn)
        fills_before = case.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        lots_before = case.remaining_lots()
        account = dict(case.conn.execute(
            "SELECT * FROM paper_accounts WHERE id=?", (case.ACCOUNT,)).fetchone())
        cycle = PT._active_cycle(case.conn)
        positions = PT._position_rows(case.conn, case.ACCOUNT, case.day)
        quote = dict(case._inner.quotes_map[case.code])
        with mock.patch.object(PT, "_completed_kline", return_value=None), \
             mock.patch.object(PT.EV, "stamp_order", side_effect=RuntimeError("R20_C4")):
            try:
                PT._intraday_sell(
                    case.conn, account, dict(positions[0]), quote, case.day,
                    PT._risk_profile(account, conn=case.conn), cycle,
                )
            except RuntimeError:
                pass
        case.conn.commit()
        cash_after = PT._shared_cash(case.conn)
        fills_after = case.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        lots_after = case.remaining_lots()
        filled_orders = case.conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE side='sell' AND status='filled'"
        ).fetchone()[0]
        reproduced = (
            fills_after > fills_before
            and lots_after < lots_before
            and abs(cash_after - cash_before) > 0.001
            and filled_orders > 0
        )
        print(f"R20-C4 intraday sell failure leaves partial ledger: "
              f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
        print(f"    fills {fills_before}->{fills_after} lots {lots_before}->{lots_after} "
              f"cash_delta={cash_after - cash_before:.2f} filled_orders={filled_orders}")
        return reproduced
    finally:
        case.doCleanups()


def main():
    results = [c1_risk_sell_bypass(), c2_intraday_sell_bypass(),
               c3_multiple_fill_writers(), c4_intraday_sell_not_atomic()]
    print(f"R20 before-fix reproduced: {sum(1 for item in results if item)}/4")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
