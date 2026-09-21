# -*- coding: utf-8 -*-
"""R22 mutation matrix M-PORT1 ~ M-PORT34.

Each mutation must turn its corresponding permanent contract RED.  The script
restores every mutated file byte-identically and verifies sha256.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
READ_MODEL = "backend/paper_portfolio_read_model.py"
TEST = "test_portfolio_read_model.PortfolioReadModelContractTests"

MUTATIONS = [
    {
        "id": "M-PORT1", "file": READ_MODEL,
        "old": '" WHERE cycle_id=? AND qty>0"',
        "new": '" WHERE qty>0"',
        "test": f"{TEST}.test_port2_later_cycle_cannot_mutate_historical_quantity",
        "desc": "remove cycle filter from lot read",
    },
    {
        "id": "M-PORT2", "file": READ_MODEL,
        "old": "        if economic > context.asof_day.isoformat():\n            continue\n",
        "new": "        if False:\n            continue\n",
        "test": f"{TEST}.test_port4_future_fill_is_excluded_by_asof",
        "desc": "remove as-of filter from lot read",
    },
    {
        "id": "M-PORT3", "file": READ_MODEL,
        "old": 'original_qty = int(_num(item.get("qty"), 0) or 0)',
        "new": 'original_qty = int(_num(item.get("remaining_qty"), 0) or 0)',
        "test": f"{TEST}.test_port6b_remaining_qty_is_not_historical_authority",
        "desc": "use current remaining_qty as historical quantity",
    },
    {
        "id": "M-PORT4", "file": READ_MODEL,
        "old": '''    positions = PP.aggregate_positions(\n        open_lots, risk_state_rows or (), flows, context.asof_day.isoformat(), num=_num\n    )\n    return positions, quantity_status\n''',
        "new": '''    _pending = [{\n        "account_id": "pending", "code": "PENDING", "name": None,\n        "industry": None, "remaining_qty": 100, "qty": 100, "cost": 1.0,\n        "acquired_at": context.asof_day.isoformat() + " 00:00:00",\n        "available_date": context.asof_day.isoformat(), "asset_type": "stock_t1",\n    }]\n    positions = PP.aggregate_positions(\n        open_lots + _pending, (), flows, context.asof_day.isoformat(), num=_num\n    )\n    return positions, quantity_status\n''',
        "test": f"{TEST}.test_port5_pending_and_unverified_orders_are_excluded",
        "desc": "include a pending order as a position",
    },
    {
        "id": "M-PORT5", "file": READ_MODEL,
        "old": '''    if _unproven_sell_exists(conn, context, account_id):\n        return None, STATUS_UNKNOWN\n    verified, _rows, proof_available = _sell_fills(conn, context, account_id)\n''',
        "new": '''    verified, _rows, proof_available = _sell_fills(conn, context, account_id)\n''',
        "test": f"{TEST}.test_port10_realized_pnl_uses_verified_execution_only",
        "desc": "include unverified SELL in realized pnl",
    },
    {
        "id": "M-PORT6", "file": READ_MODEL,
        "old": '''    positions, quantity_status = positions_for_context_with_status(\n        conn, context, account_id=account_id,\n    )\n    realized, realized_status = realized_pnl(conn, context, account_id=account_id)\n''',
        "new": '''    context = PortfolioReadContext(cycle_id=1, asof_day=context.asof_day)\n    positions, quantity_status = positions_for_context_with_status(\n        conn, context, account_id=account_id,\n    )\n    realized, realized_status = realized_pnl(conn, context, account_id=account_id)\n''',
        "test": f"{TEST}.test_port7_historical_read_never_resolves_active_cycle",
        "desc": "replace requested cycle with another cycle",
    },
    {
        "id": "M-PORT7", "file": READ_MODEL,
        "old": '    if isinstance(value, dt.date):\n        return value',
        "new": '    if isinstance(value, dt.date):\n        return dt.date.today()',
        "test": f"{TEST}.test_port4_future_fill_is_excluded_by_asof",
        "desc": "replace explicit as-of with wall clock",
    },
    {
        "id": "M-PORT8", "file": READ_MODEL,
        "old": '    price = _num(value, None)\n',
        "new": '    price = _num(value, None) or 10.0\n',
        "test": f"{TEST}.test_port9_unknown_valuation_stays_unknown",
        "desc": "fallback missing price to a latest price",
        "last": True,
    },
    {
        "id": "M-PORT9", "file": READ_MODEL,
        "old": '    return initial + net, STATUS_VERIFIED',
        "new": '    return initial + net + 1000.0, STATUS_VERIFIED',
        "test": f"{TEST}.test_port11_cash_and_nav_are_cycle_asof_bounded",
        "desc": "mix unproven current cash into historical NAV",
    },
    {
        "id": "M-PORT10", "file": READ_MODEL,
        "old": '"   AND length(f.fill_date)>=10 AND substr(f.fill_date,1,10)<=?"',
        "new": '"   AND length(f.fill_date)>=10 AND 1=1"',
        "test": f"{TEST}.test_port4_future_fill_is_excluded_by_asof",
        "desc": "include future SELL in historical read",
    },
    {
        "id": "M-PORT11", "file": READ_MODEL,
        "old": '''    return positions, quantity_status\n''',
        "new": '''    for _position in positions:\n        _position["cost"] = 999.0\n    return positions, quantity_status\n''',
        "test": f"{TEST}.test_port1_explicit_cycle_owns_quantity",
        "desc": "use current projection cost for historical cycle",
    },
    {
        "id": "M-PORT12", "file": READ_MODEL,
        "old": '''    if missing_codes or quantity_status != STATUS_VERIFIED:\n        market_value = None\n        unrealized = None\n        market_status = STATUS_UNKNOWN\n    else:\n        market_status = STATUS_VERIFIED\n''',
        "new": '''    if False:\n        market_value = None\n        unrealized = None\n        market_status = STATUS_UNKNOWN\n    else:\n        market_status = STATUS_VERIFIED\n''',
        "test": f"{TEST}.test_port9_unknown_valuation_stays_unknown",
        "desc": "remove unknown valuation preservation",
    },
    {
        "id": "M-PORT13", "file": READ_MODEL,
        "old": '            cash = PPort.cash(conn, context)[0]; cash = PPort.compatibility_cash(conn, context) if cash is None else cash',
        "new": '            cash = PPort.cash(conn, context)[0]; cash = PPort.initial_capital(conn, context) if cash is None else cash',
        "test": f"{TEST}.test_port11k_missing_order_fill_uses_compatibility_cash",
        "desc": "substitute untouched capital when legacy cash is unproven",
        "file_override": "backend/paper_trading.py",
    },
    {
        "id": "M-PORT14", "file": READ_MODEL,
        "old": '''    for rows in (all_buys, all_sells):\n        for row in rows:\n            key = (str(row.get("fill_account_id") or ""), str(row.get("fill_code") or ""))\n            if not _identity_ok(row) or not EV.is_verified_row(row):\n                incomplete.add(key)\n    # A filled order with no verified fill row is not evidence of zero cash;\n    # it blocks the per-symbol projection for that key.\n    for orders, verified_rows in (\n        (_all_filled_buy_orders(conn, context, account_id)[0], all_buys),\n        (_all_filled_sell_orders(conn, context, account_id)[0], all_sells),\n    ):\n''',
        "new": '''    for rows in (all_sells,):\n        for row in rows:\n            key = (str(row.get("fill_account_id") or ""), str(row.get("fill_code") or ""))\n            if not _identity_ok(row) or not EV.is_verified_row(row):\n                incomplete.add(key)\n    for orders, verified_rows in (\n        (_all_filled_sell_orders(conn, context, account_id)[0], all_sells),\n    ):\n''',
        "test": f"{TEST}.test_port3b_unverified_buy_blocks_display_cash_flow",
        "desc": "ignore unverified BUY rows in display cash flow",
    },
    {
        "id": "M-PORT15", "file": READ_MODEL,
        "old": "        if row is None or int(row[1] or -1) != context.cycle_id:\n            return None\n",
        "new": "        if row is None:\n            return None\n",
        "test": f"{TEST}.test_port11c_account_initial_capital_is_cycle_scoped",
        "desc": "scope account initial capital to the wrong cycle",
    },
    {
        "id": "M-PORT16", "file": READ_MODEL,
        "old": "    if buy_orders_proof:\n",
        "new": "    if False:\n",
        "test": f"{TEST}.test_port11a_filled_buy_without_fill_evidence_keeps_cash_unknown",
        "desc": "ignore filled BUY orders without fill evidence",
    },
    {
        "id": "M-PORT17", "file": "backend/paper_trading.py",
        "old": "            context = PPort.PortfolioReadContext(cycle_id, asof_day); positions, quantity_status = PPort.positions_for_context_with_status(conn, context)\n",
        "new": "            positions = PPRM.positions_for_cycle(conn, cycle_id, asof_day=asof_day); quantity_status = PPort.STATUS_VERIFIED\n",
        "test": f"{TEST}.test_port4b_explicit_exposure_uses_bounded_positions",
        "desc": "explicit exposure falls back to current remaining quantities",
    },
    {
        "id": "M-PORT18", "file": READ_MODEL,
        "old": "    if missing_codes or quantity_status != STATUS_VERIFIED:\n",
        "new": "    if missing_codes:\n",
        "test": f"{TEST}.test_port4c_unknown_quantity_keeps_valuation_unknown",
        "desc": "drop unknown quantity status from valuation",
    },
    {
        "id": "M-PORT19", "file": READ_MODEL,
        "old": '''    fill_day = _day_text(row.get("fill_date"))\n    if not fill_day:\n        return None\n    row["economic_date"] = fill_day\n''',
        "new": '''    fill_day = _day_text(lot.get("acquired_at"))\n    if not fill_day:\n        return None\n    row["economic_date"] = fill_day\n''',
        "test": f"{TEST}.test_port4d_lot_economic_date_comes_from_fill_date",
        "desc": "use wall-clock acquired_at instead of fill economic date",
    },
    {
        "id": "M-PORT20", "file": "backend/paper_trading.py",
        "old": "            if quantity_status != PPort.STATUS_VERIFIED: raise PPort.PortfolioReadUnavailable(\"explicit-cycle exposure quantity proof is unknown\")\n",
        "new": "            if False: raise PPort.PortfolioReadUnavailable(\"explicit-cycle exposure quantity proof is unknown\")\n",
        "test": f"{TEST}.test_port4f_explicit_exposure_fails_closed_on_unknown_quantity",
        "desc": "value unproven exposure instead of failing closed",
    },
    {
        "id": "M-PORT21", "file": READ_MODEL,
        "old": "    for orders, verified_rows in (\n",
        "new": "    for orders, verified_rows in ():\n",
        "test": f"{TEST}.test_port3c_buy_order_without_fill_blocks_display_cash_flow",
        "desc": "ignore filled orders with no fill evidence in display flow",
    },
    {
        "id": "M-PORT22", "file": READ_MODEL,
        "old": "    if _cycle_is_archived(conn, context):\n        return [], STATUS_UNKNOWN\n",
        "new": "    if False:\n        return [], STATUS_UNKNOWN\n",
        "test": f"{TEST}.test_port11f_archived_cycle_remains_unknown",
        "desc": "publish an archived cycle as a verified empty portfolio",
    },
    {
        "id": "M-PORT23", "file": READ_MODEL,
        "old": "    if price is None or not math.isfinite(price) or price <= 0:\n        return None\n",
        "new": "    if price is None:\n        return None\n",
        "test": f"{TEST}.test_port9b_non_finite_or_non_positive_valuation_stays_unknown",
        "desc": "accept non-finite valuation evidence as verified",
    },
    {
        "id": "M-PORT24", "file": READ_MODEL,
        "old": "        uncovered_cost, _uncovered_count = _uncovered_lot_facts(conn, all_lots)\n        return total - uncovered_cost\n",
        "new": "        return total\n",
        "test": f"{TEST}.test_port11d_mixed_fill_less_lot_is_reconciled",
        "desc": "ignore uncovered durable lot cost in compatibility cash",
    },
    {
        "id": "M-PORT25", "file": READ_MODEL,
        "old": '    day = context.asof_day.isoformat()\n    return _row_dicts(conn.execute(\n        "SELECT * FROM paper_position_risk_state"',
        "new": '    day = "9999-12-31"\n    return _row_dicts(conn.execute(\n        "SELECT * FROM paper_position_risk_state"',
        "test": f"{TEST}.test_port4i_risk_positions_exclude_future_runtime_state",
        "desc": "inject future runtime risk state into historical read",
    },
    {
        "id": "M-PORT26", "file": READ_MODEL,
        "old": '''    lots, _quantity_status = bounded_lots_with_status(
        conn, context, account_id=account_id,
    )
    _uncovered_cost, uncovered_count = _uncovered_lot_facts(conn, lots)
    if uncovered_count:
        return None, STATUS_UNKNOWN
    return total, STATUS_VERIFIED
''',
        "new": '''    return total, STATUS_VERIFIED
''',
        "test": f"{TEST}.test_port11g_source_less_lot_keeps_cash_unknown",
        "desc": "treat source-less durable lots as verified zero cash flow",
    },
    {
        "id": "M-PORT27", "file": READ_MODEL,
        "old": '''        if evidence is None:
            uncertain_lot_ids.add(lot_id)
            economic = original[:10] or None
''',
        "new": '''        if evidence is None:
            economic = original[:10] or None
''',
        "test": f"{TEST}.test_port11g_source_less_lot_keeps_cash_unknown",
        "desc": "promote wall-clock acquired_at for source-less lots",
    },
    {
        "id": "M-PORT28", "file": READ_MODEL,
        "old": '''    lots, _quantity_status = bounded_lots_with_status(
        conn, context, account_id=account_id,
    )
''',
        "new": '''    lots = bounded_lots(conn, context, account_id=account_id)
''',
        "test": f"{TEST}.test_port11h_consumed_source_less_lot_cash_completeness",
        "desc": "ignore consumed legacy lots in cash completeness",
    },
    {
        "id": "M-PORT29", "file": READ_MODEL,
        "old": '''    unresolved_uncertain = any(
        int(row.get("id") or 0) in uncertain_lot_ids
        and int(row.get("remaining_qty") or 0) > 0
        for row in rebuilt
    )
''',
        "new": '''    unresolved_uncertain = bool(uncertain_lot_ids)
''',
        "test": f"{TEST}.test_port4j_closed_source_less_lot_does_not_poison_risk_reads",
        "desc": "poison risk scans with fully consumed source-less lots",
    },
    {
        "id": "M-PORT30", "file": READ_MODEL,
        "old": '''    if row is None or int(row[0] or 0) <= 0:
        return None
''',
        "new": '''    if row is None:
        return None
''',
        "test": f"{TEST}.test_port11i_missing_cycle_does_not_invent_zero_capital",
        "desc": "invent zero capital for a nonexistent cycle",
    },
    {
        "id": "M-PORT31", "file": READ_MODEL,
        "old": '''        if cycle is not None:
            if not _cycle_created_by(conn, context):
                return None
''',
        "new": '''        if cycle is not None:
''',
        "test": f"{TEST}.test_port11j_read_before_cycle_creation_is_unknown",
        "desc": "publish pre-cycle capital as verified",
    },
    {
        "id": "M-PORT32", "file": READ_MODEL,
        "old": '''    if int(row.get("cycle_id") or -1) != int(lot.get("cycle_id") or -1):
        return None
''',
        "new": '''    if False:
        return None
''',
        "test": f"{TEST}.test_port4k_source_order_identity_must_match_lot",
        "desc": "accept a source BUY order from another cycle",
    },
    {
        "id": "M-PORT33", "file": READ_MODEL,
        "old": '''    if not _has_columns(conn, "paper_position_lots", _POSITION_LOT_COLUMNS):
        return [], STATUS_UNKNOWN
''',
        "new": '''    if not _has_columns(conn, "paper_position_lots", _POSITION_LOT_COLUMNS):
        return [], STATUS_VERIFIED
''',
        "test": f"{TEST}.test_port4l_missing_lot_schema_keeps_quantity_unknown",
        "desc": "invent an empty verified portfolio without lot schema",
    },
    {
        "id": "M-PORT34", "file": READ_MODEL,
        "old": '''    if not proof_available:
        return None, STATUS_UNKNOWN
''',
        "new": '''    if not proof_available:
        return 0.0, STATUS_VERIFIED
''',
        "test": f"{TEST}.test_port10b_realized_pnl_without_execution_schema_stays_unknown",
        "desc": "invent verified zero realized PnL without execution evidence",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r22_mutation_pycache_")
_SEQ = [0]


def run_test(target: str) -> subprocess.CompletedProcess:
    _SEQ[0] += 1
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = os.path.join(PYCACHE_ROOT, f"run{_SEQ[0]:03d}")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=BACKEND, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600, env=env,
    )


def main() -> int:
    print(f"repo root: {ROOT}")
    results = []
    for mutation in MUTATIONS:
        path = os.path.join(ROOT, mutation.get("file_override", mutation["file"]))
        with open(path, "rb") as handle:
            original = handle.read()
        before = sha256(original)
        text = original.decode("utf-8").replace("\r\n", "\n")
        old = mutation["old"]
        new = mutation["new"]
        if mutation.get("last"):
            index = text.rfind(old)
            assert index >= 0, f'{mutation["id"]}: anchor not found'
            mutated = text[:index] + new + text[index + len(old):]
        else:
            assert text.count(old) >= 1, f'{mutation["id"]}: anchor not found'
            mutated = text.replace(old, new, 1)
        try:
            with open(path, "wb") as handle:
                handle.write(_adapt_eol(mutated, original))
            result = run_test(mutation["test"])
            red = result.returncode != 0
            results.append(red)
            print(f'{mutation["id"]} {mutation["desc"]}: '
                  f'{"RED" if red else "SURVIVED"}')
            if not red:
                print(result.stdout[-2000:])
                print(result.stderr[-2000:])
        finally:
            with open(path, "wb") as handle:
                handle.write(original)
            with open(path, "rb") as handle:
                after = sha256(handle.read())
            if after != before:
                raise RuntimeError(f'{mutation["id"]}: restore sha256 mismatch')
    red_count = sum(results)
    print(f"R22 mutations: {red_count}/{len(results)} RED; survived={len(results)-red_count}")
    print("restore sha256: PASS")
    return 0 if red_count == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())