# -*- coding: utf-8 -*-
"""R22 before-fix reproduction on the R21 merge base.

Run from the repository root:

    python work/r22_before_fix_repro.py

The probe uses the real production schema and the production position/read
entry points. It records only facts that are actually observable on this
baseline; theoretical risks are marked NOT REPRODUCED.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_position_read_model as PPRM  # noqa: E402
import paper_trading as PT  # noqa: E402

ACCOUNT = next(iter(PT.ACCOUNT_SPECS))
CODE = "600519"
DAY = dt.date(2026, 9, 20)
NEXT = DAY + dt.timedelta(days=1)


def _bounded_positions(conn, cycle_id, asof_day):
    """Prefer the R22 bounded read model; fall back on the pre-fix facade."""
    try:
        import paper_portfolio_read_model as PPort
    except ImportError:
        return PPRM.positions_for_cycle(conn, cycle_id, asof_day=asof_day)
    return PPort.positions_for_context(
        conn, PPort.PortfolioReadContext(cycle_id=cycle_id, asof_day=asof_day)
    )


def _stamp(conn, account_id=ACCOUNT):
    return PT._strategy_stamp(conn, account_id)


def _cycle(conn, *, key, status, capital=100000.0, created=None):
    created = created or f"{DAY.isoformat()} 09:00:00"
    cur = conn.execute(
        "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
        "updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
        (key, status, capital, "shared_pool", created, created,
         created if status == "running" else None),
    )
    return int(cur.lastrowid)


def _order_and_fill(conn, *, cycle_id, side, qty, price, fill_date, verified=True):
    stamp = _stamp(conn)
    amount = qty * price
    fees = 5.0
    order_id = conn.execute(
        "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
        "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
        "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
        "execution_status,execution_verified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ACCOUNT, side, CODE, "测试股", qty, price, price, amount, fees, "filled",
         "r22-repro", "{}", f"{fill_date} 09:30:00", f"{fill_date} 09:30:01",
         "market", "seed", *stamp, cycle_id,
         "verified" if verified else "unknown", 1 if verified else 0),
    ).lastrowid
    conn.execute(
        "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,fill_date,"
        "quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, ACCOUNT, side, CODE, qty, price, amount, fees, fill_date,
         f"{fill_date} 09:30:00", "r22-repro"),
    )
    return int(order_id)


def _lot(conn, cycle_id, qty, cost, *, acquired_at=None, remaining_qty=None, source_order_id=None):
    acquired_at = acquired_at or f"{DAY.isoformat()} 10:00:00"
    remaining_qty = qty if remaining_qty is None else remaining_qty
    return int(conn.execute(
        "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
        "remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,"
        "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cycle_id, ACCOUNT, CODE, "测试股", "测试", qty, remaining_qty, cost,
         acquired_at, (DAY + dt.timedelta(days=1)).isoformat(), "stock_t1",
         source_order_id, 1, 1),
    ).lastrowid)


def _new_ledger():
    tmp = tempfile.TemporaryDirectory()
    path = os.path.join(tmp.name, "paper.sqlite3")
    patches = (
        mock.patch.object(PT, "DB_PATH", path),
        mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
    )
    for patcher in patches:
        patcher.start()
    PT.init_db()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return tmp, patches, conn


def _close_ledger(tmp, patches, conn):
    conn.close()
    for patcher in reversed(patches):
        patcher.stop()
    tmp.cleanup()


def _seed_base(conn):
    # init_db already created a runnable cycle/account. Create explicit
    # historical cycles so the requested cycle and the active cycle differ.
    c100 = _cycle(conn, key="r22-c100", status="paused")
    c101 = _cycle(conn, key="r22-c101", status="running")
    conn.execute("UPDATE paper_accounts SET cycle_id=? WHERE id=?", (c100, ACCOUNT))
    conn.commit()
    return c100, c101


def c1_later_cycle_contamination():
    """Later-cycle display cash flows must not rewrite cycle 100's cost."""
    tmp, patches, conn = _new_ledger()
    try:
        c100, c101 = _seed_base(conn)
        buy = _order_and_fill(conn, cycle_id=c100, side="buy", qty=100, price=10.0,
                              fill_date=DAY.isoformat())
        _lot(conn, c100, 100, 10.0, source_order_id=buy)
        conn.commit()
        before = _bounded_positions(conn, c100, DAY)
        _order_and_fill(conn, cycle_id=c101, side="buy", qty=1, price=999.0,
                        fill_date=DAY.isoformat())
        conn.commit()
        after = _bounded_positions(conn, c100, DAY)
        before_cost = before[0].get("display_cost") if before else None
        after_cost = after[0].get("display_cost") if after else None
        reproduced = bool(before and after and abs(before_cost - after_cost) > 0.001)
        print("R22-C1 later-cycle position contamination: "
              f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
        print(f"    qty={before[0]['qty'] if before else None} "
              f"display_cost_before={before_cost} display_cost_after={after_cost}")
        return reproduced
    finally:
        _close_ledger(tmp, patches, conn)


def c2_display_cost_cross_cycle_contamination():
    """A different-cycle execution must not change cycle 100 display cost."""
    tmp, patches, conn = _new_ledger()
    try:
        c100, c101 = _seed_base(conn)
        buy = _order_and_fill(conn, cycle_id=c100, side="buy", qty=100, price=10.0,
                              fill_date=DAY.isoformat())
        _lot(conn, c100, 100, 10.0, source_order_id=buy)
        conn.commit()
        before = _bounded_positions(conn, c100, DAY)[0]
        _order_and_fill(conn, cycle_id=c101, side="sell", qty=50, price=20.0,
                        fill_date=DAY.isoformat())
        conn.commit()
        after = _bounded_positions(conn, c100, DAY)[0]
        reproduced = (before.get("display_cost_source") != after.get("display_cost_source")
                      or abs(before.get("display_cost") - after.get("display_cost")) > 0.001)
        print("R22-C2 display-cost cross-cycle contamination: "
              f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
        print(f"    before={before.get('display_cost')}/{before.get('display_cost_source')} "
              f"after={after.get('display_cost')}/{after.get('display_cost_source')}")
        return reproduced
    finally:
        _close_ledger(tmp, patches, conn)


def c3_future_fill_contamination():
    """A future lot/fill must not alter an earlier as-of view."""
    tmp, patches, conn = _new_ledger()
    try:
        c100, _c101 = _seed_base(conn)
        buy = _order_and_fill(conn, cycle_id=c100, side="buy", qty=100, price=10.0,
                              fill_date=DAY.isoformat())
        _lot(conn, c100, 100, 10.0, source_order_id=buy)
        conn.commit()
        before = _bounded_positions(conn, c100, DAY)[0]
        _lot(conn, c100, 50, 11.0, acquired_at=f"{NEXT.isoformat()} 10:00:00")
        conn.commit()
        after_buy = _bounded_positions(conn, c100, DAY)[0]
        # A future sell must also not reduce the historical quantity.
        sell = _order_and_fill(conn, cycle_id=c100, side="sell", qty=25, price=12.0,
                               fill_date=NEXT.isoformat())
        conn.execute("UPDATE paper_position_lots SET remaining_qty=75 WHERE id=?", (buy and 1,))
        conn.commit()
        after_sell = _bounded_positions(conn, c100, DAY)[0]
        reproduced = (
            before["qty"] != after_buy["qty"] or before["qty"] != after_sell["qty"]
        )
        print("R22-C3 future fill contamination: "
              f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
        print(f"    qty_before={before['qty']} qty_after_future_buy={after_buy['qty']} "
              f"qty_after_future_sell={after_sell['qty']} future_sell_order={sell}")
        return reproduced
    finally:
        _close_ledger(tmp, patches, conn)


def c4_pending_unverified_exclusion():
    """Pending/unverified orders must not create portfolio truth."""
    tmp, patches, conn = _new_ledger()
    try:
        c100, _c101 = _seed_base(conn)
        # No lot is written. These rows exercise the read gate only.
        stamp = _stamp(conn)
        conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,status,risk_payload,created_at,"
            "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_status,execution_verified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", CODE, 100, "pending_limit", "{}", f"{DAY.isoformat()} 09:00:00",
             "limit", "strategy", *stamp, c100, "unknown", 0),
        )
        conn.commit()
        rows = _bounded_positions(conn, c100, DAY)
        reproduced = bool(rows)
        print("R22-C4 pending/unverified order contamination: "
              f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
        print(f"    positions={rows}")
        return reproduced
    finally:
        _close_ledger(tmp, patches, conn)


def c5_projection_corruption():
    """The compatibility projection must not override durable lots."""
    tmp, patches, conn = _new_ledger()
    try:
        c100, _c101 = _seed_base(conn)
        buy = _order_and_fill(conn, cycle_id=c100, side="buy", qty=100, price=10.0,
                              fill_date=DAY.isoformat())
        _lot(conn, c100, 100, 10.0, source_order_id=buy)
        conn.execute(
            "INSERT OR REPLACE INTO paper_positions(account_id,code,name,industry,qty,cost,"
            "entry_date,available_date,asset_type,peak_price,take_stage) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, CODE, "测试股", "测试", 999, 999.0, DAY.isoformat(),
             NEXT.isoformat(), "stock_t1", 999.0, 0),
        )
        conn.commit()
        rows = _bounded_positions(conn, c100, DAY)
        reproduced = bool(rows and int(rows[0]["qty"]) == 999)
        print("R22-C5 projection corruption overrides authority: "
              f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
        print(f"    authoritative_qty={rows[0]['qty'] if rows else None}")
        return reproduced
    finally:
        _close_ledger(tmp, patches, conn)


def c6_current_cycle_fallback():
    """An explicit cycle read must not be silently replaced by active cycle."""
    tmp, patches, conn = _new_ledger()
    try:
        c100, c101 = _seed_base(conn)
        buy = _order_and_fill(conn, cycle_id=c100, side="buy", qty=100, price=10.0,
                              fill_date=DAY.isoformat())
        _lot(conn, c100, 100, 10.0, source_order_id=buy)
        conn.commit()
        exact = _bounded_positions(conn, c100, DAY)
        # The legacy exposure helper has no explicit cycle parameter and uses
        # the active cycle (c101), so it loses the requested c100 position.
        try:
            exposure_positions, _value, _nav, _industries, _codes = PT._shared_account_exposure(
                conn, {CODE: {"price": 10.0}}, DAY, cycle_id=c100)
        except TypeError:
            # R22 before-fix signature: no explicit cycle parameter.
            exposure_positions, _value, _nav, _industries, _codes = PT._shared_account_exposure(
                conn, {CODE: {"price": 10.0}}, DAY)
        reproduced = bool(exact) and not any(
            str(row.get("code")) == CODE for row in exposure_positions
        )
        print("R22-C6 current-cycle fallback affects portfolio identity: "
              f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
        print(f"    exact_cycle_rows={len(exact)} exposure_rows={len(exposure_positions)} "
              f"active_cycle={c101}")
        return reproduced
    finally:
        _close_ledger(tmp, patches, conn)


def c7_wall_clock_leakage():
    """The explicit as-of quantity must be invariant under wall clock changes."""
    tmp, patches, conn = _new_ledger()
    try:
        c100, _c101 = _seed_base(conn)
        buy = _order_and_fill(conn, cycle_id=c100, side="buy", qty=100, price=10.0,
                              fill_date=DAY.isoformat())
        _lot(conn, c100, 100, 10.0, source_order_id=buy)
        conn.commit()
        first = _bounded_positions(conn, c100, DAY)
        real_date = PPRM.dt.date

        class FakeDate(real_date):
            @classmethod
            def today(cls):
                return NEXT

        fake_dt = type("FakeDatetimeModule", (), {
            "date": FakeDate, "datetime": PPRM.dt.datetime,
        })
        with mock.patch.object(PPRM, "dt", fake_dt):
            second = _bounded_positions(conn, c100, DAY)
        reproduced = first != second
        print("R22-C7 wall-clock leakage into explicit as-of read: "
              f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
        print(f"    first={[(p['qty'], p['cost']) for p in first]} "
              f"second={[(p['qty'], p['cost']) for p in second]}")
        return reproduced
    finally:
        _close_ledger(tmp, patches, conn)


def c8_unknown_valuation_preservation():
    """No historical valuation evidence must not become a current quote."""
    tmp, patches, conn = _new_ledger()
    try:
        c100, _c101 = _seed_base(conn)
        buy = _order_and_fill(conn, cycle_id=c100, side="buy", qty=100, price=10.0,
                              fill_date=DAY.isoformat())
        _lot(conn, c100, 100, 10.0, source_order_id=buy)
        conn.commit()
        try:
            import paper_portfolio_read_model as PPort
        except ImportError:
            # Before-fix baseline had no bounded portfolio contract.
            rows = _bounded_positions(conn, c100, DAY)
            has_valuation_fact = any("market_value" in row or "nav" in row for row in rows)
            reproduced = bool(rows) and has_valuation_fact
            print("R22-C8 unknown historical valuation contaminated by current quote: "
                  f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
            print("    legacy_positions_expose_no_historical_valuation_contract")
            return reproduced
        result = PPort.portfolio_for_cycle(conn, c100, DAY)
        reproduced = not (
            result["market_value"] is None
            and result["unrealized_pnl"] is None
            and result["nav"] is None
            and result["market_value_status"] == "unknown"
        )
        print("R22-C8 unknown historical valuation contaminated by current quote: "
              f"{'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
        print(f"    market_value={result['market_value']} nav={result['nav']} "
              f"status={result['market_value_status']}")
        return reproduced
    finally:
        _close_ledger(tmp, patches, conn)


def main():
    checks = [
        c1_later_cycle_contamination,
        c2_display_cost_cross_cycle_contamination,
        c3_future_fill_contamination,
        c4_pending_unverified_exclusion,
        c5_projection_corruption,
        c6_current_cycle_fallback,
        c7_wall_clock_leakage,
        c8_unknown_valuation_preservation,
    ]
    results = [bool(fn()) for fn in checks]
    print(f"R22 before-fix reproduced: {sum(results)}/{len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())