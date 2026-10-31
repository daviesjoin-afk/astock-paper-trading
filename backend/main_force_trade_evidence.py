"""Read-only winner AND loser case evidence from frozen main-force orders.

This is a trade case review, not factor attribution or proof of expected alpha.
Open holdings are kept separate; partial sells are not independent samples.
"""
from __future__ import annotations

from collections import defaultdict
import datetime as dt
import json
from pathlib import Path
import sqlite3

import execution_verification as EV
from main_force_confirmation import event_time as _time, number as _number, MARKET_TZ

VERSION = "main-force-trade-evidence-v1"
ACCOUNT_ID = "main_force_top10"
BOUNDARY = (
    "仅复核当前周期已核验成交，按从空仓买入到再次清仓合并交易；"
    "只使用买入委托冻结的行情，不读取后来刷新的信号。"
    "同时保留盈利、亏损与未结束交易；盈利案例不是因子有效性或因果证明，"
    "不能据此提高仓位、放宽门槛或挑选历史赢家。"
)


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _entry(row):
    risk = _object(row.get("risk_payload"))
    quote = _object(risk.get("quote"))
    model = _object(risk.get("entry_model"))
    main_net = _number(quote.get("main_net"))
    amount = _number(quote.get("amount"))
    return {
        "order_id": row.get("id"), "executed_at": row.get("executed_at"),
        "strategy_version": row.get("strategy_version"),
        "strategy_checksum": row.get("strategy_checksum"),
        "quote_at": quote.get("quote_at"),
        "pct": _number(quote.get("pct")), "main_pct": _number(quote.get("main_pct")),
        "flow_intensity_pct": (main_net / amount * 100
                               if main_net is not None and amount and amount > 0 else None),
        "vol_ratio": _number(quote.get("vol_ratio")),
        "turnover": _number(quote.get("turnover")),
        "entry_score": _number(model.get("score")),
        "entry_threshold": _number(model.get("threshold")),
        "microstructure_status": _object(model.get("microstructure")).get("status"),
    }


def summarize_orders(rows):
    """Rows must be current-cycle verified orders, sorted by execution time/id."""
    pending = {}
    closed = []
    rejected_codes = set()
    issues = defaultdict(int)
    for row in rows:
        code = str(row.get("code") or "")
        qty = _number(row.get("qty"))
        if not code or qty is None or qty <= 0 or qty != int(qty):
            issues["invalid_order"] += 1
            if code:
                rejected_codes.add(code)
            continue
        if code in rejected_codes:
            continue
        if row.get("side") == "buy":
            episode = pending.setdefault(code, {
                "code": code, "remaining_qty": 0, "buy_orders": 0, "sell_orders": 0,
                "realized_pnl": 0.0, "opened_at": row.get("executed_at"),
                "first_entry": _entry(row),
            })
            episode["remaining_qty"] += int(qty)
            episode["buy_orders"] += 1
        elif row.get("side") == "sell":
            episode = pending.get(code)
            pnl = _number(row.get("realized_pnl"))
            if episode is None or qty > episode["remaining_qty"] or pnl is None:
                issues["unmatched_or_unpriced_sell"] += 1
                rejected_codes.add(code)
                pending.pop(code, None)
                continue
            episode["remaining_qty"] -= int(qty)
            episode["sell_orders"] += 1
            episode["realized_pnl"] += pnl
            if episode["remaining_qty"] == 0:
                episode["closed_at"] = row.get("executed_at")
                episode["realized_pnl"] = round(episode["realized_pnl"], 2)
                closed.append(pending.pop(code))
        else:
            issues["unknown_side"] += 1
            rejected_codes.add(code)
    # A broken sequence cannot be rescued by retaining its earlier winners.
    closed = [row for row in closed if row["code"] not in rejected_codes]
    opened = [row for code, row in pending.items() if code not in rejected_codes]
    winners = sorted((row for row in closed if row["realized_pnl"] > 0),
                     key=lambda row: (-row["realized_pnl"], row["code"]))
    losers = sorted((row for row in closed if row["realized_pnl"] < 0),
                    key=lambda row: (row["realized_pnl"], row["code"]))
    return {
        "version": VERSION, "status": "case_review_only", "boundary": BOUNDARY,
        "closed_trades": len(closed), "winning_trades": len(winners),
        "losing_trades": len(losers), "flat_trades": len(closed) - len(winners) - len(losers),
        "open_trades": len(opened), "invalid_codes": sorted(rejected_codes),
        "issues": dict(issues), "closed": closed, "open": opened,
        "winner_examples": winners[:3], "loser_examples": losers[:3],
        "factor_admission": "not_proven_by_trade_cases",
    }


def build_report(paper_db_path, *, asof=None):
    """No migration, network calls or writes; explicit unavailable on schema gaps."""
    now = _time(asof) if asof is not None else dt.datetime.now(MARKET_TZ)
    unavailable = {"version": VERSION, "status": "unavailable", "boundary": BOUNDARY,
                   "factor_admission": "not_proven_by_trade_cases"}
    if now is None:
        return {**unavailable, "reason": "invalid_asof"}
    cutoff = now.astimezone(MARKET_TZ).replace(tzinfo=None).isoformat(sep=" ")
    conn = None
    try:
        conn = sqlite3.connect(Path(paper_db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        account = conn.execute("SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT_ID,)).fetchone()
        if account is None or account["cycle_id"] is None:
            return {**unavailable, "reason": "current_account_cycle_missing"}
        rows = [dict(row) for row in conn.execute(
            "SELECT id,code,side,qty,realized_pnl,executed_at,risk_payload,"
            "strategy_version,strategy_checksum"
            " FROM paper_orders WHERE account_id=? AND cycle_id=?"
            " AND status='filled' AND " + EV.VERIFIED_PREDICATE +
            " AND datetime(executed_at)<=datetime(?)"
            " ORDER BY datetime(executed_at),id", (ACCOUNT_ID, account["cycle_id"], cutoff))]
        result = summarize_orders(rows)
        result.update({"cycle_id": account["cycle_id"], "asof": now.isoformat(),
                       "verified_order_count": len(rows)})
        return result
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {**unavailable, "reason": "ledger_or_schema_unavailable", "error_type": type(exc).__name__}
    finally:
        if conn is not None:
            conn.close()


def evidence_summary(report):
    """Bounded prompt payload; no raw order logs, news text or account secrets."""
    return {key: value for key, value in report.items() if key not in {"closed", "open"}}
