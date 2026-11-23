#!/usr/bin/env python3
"""Mine the paper ledger for how the AI's own past calls actually turned out.

The observer is stateless per round: it never sees whether the names it blocked
went on to rise, or whether the buys it named were worth it. This module turns
the committed history into that missing memory, so the next round can be told
"in this regime, your blocks lost 137 bps on average" instead of repeating the
same advice blind.

Read-only. It never writes the ledger and never invents a missing price.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

# A call is scored against the session's own closing snapshot, so nothing after
# that session is used. Below this many cases the number is not reported at all:
# a 3-case average is noise dressed as evidence.
MIN_CASES = 20


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _quote_rows(db: sqlite3.Connection, round_id: str) -> dict[str, int]:
    """Tradable last prices for one round; unusable rows are absent, not zero."""
    return {
        row["symbol"]: row["price_cents"]
        for row in db.execute(
            "SELECT symbol, price_cents FROM quote_snapshot "
            "WHERE round_id=? AND price_cents>0", (round_id,))
    }


def _buyable(db: sqlite3.Connection, round_id: str) -> set[str]:
    """Symbols that were not already sealed at their own upside limit.

    A name pinned at the ceiling could never have been bought, so treating a
    block on it as a missed gain would overstate how wrong the model was.
    """
    return {
        row["symbol"]
        for row in db.execute(
            "SELECT symbol, price_cents, limit_up_cents FROM quote_snapshot "
            "WHERE round_id=? AND price_cents>0", (round_id,))
        if not (row["limit_up_cents"] and row["price_cents"] >= row["limit_up_cents"])
    }


def _close_of(db: sqlite3.Connection, day: str) -> tuple[str, dict[str, int]] | None:
    row = db.execute(
        "SELECT round_id FROM round WHERE round_id LIKE ? ORDER BY rowid DESC LIMIT 1",
        (day + "-%",)).fetchone()
    if not row:
        return None
    return row["round_id"], _quote_rows(db, row["round_id"])


def _move_bps(start: int, end: int) -> int | None:
    if start <= 0 or end <= 0:
        return None
    return (end - start) * 10_000 // start


def collect(db: sqlite3.Connection) -> dict[str, Any]:
    """Every AI call with its forward outcome, grouped by market regime."""
    days = [r["d"] for r in db.execute(
        "SELECT DISTINCT substr(round_id,1,10) d FROM round ORDER BY d")]
    blocked_cases: list[dict[str, Any]] = []
    buy_cases: list[dict[str, Any]] = []
    round_cases: list[dict[str, Any]] = []
    for day in days:
        closing = _close_of(db, day)
        if closing is None:
            continue
        close_id, close_prices = closing
        rows = db.execute(
            "SELECT o.round_id, o.application_json, r.regime FROM ai_observation o "
            "JOIN round r USING(round_id) WHERE o.round_id LIKE ? ORDER BY o.rowid",
            (day + "-%",)).fetchall()
        for row in rows:
            if row["round_id"] == close_id:
                continue  # no forward window inside the session
            try:
                app = json.loads(row["application_json"])
            except (TypeError, ValueError):
                continue
            if not app.get("accepted"):
                continue
            prices = _quote_rows(db, row["round_id"])
            buyable = _buyable(db, row["round_id"])
            regime = row["regime"] or "unknown"
            for symbol in app.get("blocked_symbols") or []:
                if symbol not in buyable:
                    continue  # sealed at the ceiling: nothing was given up
                move = _move_bps(prices.get(symbol, 0), close_prices.get(symbol, 0))
                if move is not None:
                    blocked_cases.append({"round_id": row["round_id"], "symbol": symbol,
                                          "regime": regime, "move_bps": move})
            for item in app.get("active_suggestions") or app.get("suggestions") or []:
                if item.get("side") != "buy":
                    continue
                symbol = item.get("symbol")
                move = _move_bps(prices.get(symbol, 0), close_prices.get(symbol, 0))
                if move is not None:
                    buy_cases.append({"round_id": row["round_id"], "symbol": symbol,
                                      "regime": regime, "move_bps": move})
            # The regime call itself: did the market drift up or down? Sealed
            # names are excluded on both sides so this stays a drift measure and
            # not a disguised copy of the blocked-symbol result.
            moves = [_move_bps(price, close_prices.get(symbol, 0))
                     for symbol, price in prices.items()
                     if close_prices.get(symbol) and symbol in buyable]
            moves = [m for m in moves if m is not None]
            if moves:
                round_cases.append({"round_id": row["round_id"], "regime": regime,
                                    "entry_delta": app.get("entry_delta"),
                                    "bias": app.get("bias"),
                                    "mean_move_bps": sum(moves) // len(moves)})
    return {"days": days, "blocked": blocked_cases, "buys": buy_cases, "rounds": round_cases}


def _summary(cases: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    moves = [c["move_bps"] for c in cases]
    if len(moves) < MIN_CASES:
        return None
    return {"cases": len(moves), "mean_bps": sum(moves) // len(moves),
            "up": sum(1 for m in moves if m > 0),
            "down": sum(1 for m in moves if m < 0)}


def scorecard(memory: Mapping[str, Any]) -> dict[str, Any]:
    """What the model should know about its own track record, per regime.

    `blocked.mean_bps` is the cost of being wrong: a positive number means the
    names it refused to buy went on to rise, so the block lost that much.
    """
    by_regime: dict[str, dict[str, Any]] = {}
    for regime in sorted({c["regime"] for c in memory["blocked"]} |
                         {c["regime"] for c in memory["rounds"]}):
        blocked = _summary(c for c in memory["blocked"] if c["regime"] == regime)
        buys = _summary(c for c in memory["buys"] if c["regime"] == regime)
        rounds = _summary(({"move_bps": c["mean_move_bps"]} for c in memory["rounds"]
                           if c["regime"] == regime))
        if blocked or buys or rounds:
            by_regime[regime] = {"blocked_symbols": blocked, "named_buys": buys,
                                 "market_drift": rounds}
    return {"basis": "session_close_of_the_same_day", "min_cases": MIN_CASES,
            "sessions": len(memory["days"]), "by_regime": by_regime}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    with closing(_connect(args.db)) as db:
        card = scorecard(collect(db))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(json.dumps(card, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
