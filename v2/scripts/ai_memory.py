#!/usr/bin/env python3
"""Mine the paper ledger for how the AI's own past calls actually turned out.

The observer is stateless per round: it never sees whether the names it blocked
went on to rise, or whether the buys it named were worth it. This module turns
the committed history into that missing memory, so the next round can be told
"in this regime, your blocks lost 137 bps on average" instead of repeating the
same advice blind.

Three rules keep the number honest:

* **Only a closed session is scored.** A process that restarted at 11:00 leaves
  a final round that is a midday price, not a close; scoring against it would
  record half a session as if it were the day.
* **A name and its benchmark share one window.** The stock's move is measured
  from the round that made the call to the close, so the market median it is
  compared against is measured from that same round to that same close. Mixing
  a mid-session entry with an open-to-close median manufactures alpha out of
  nothing, in either direction.
* **A counterfactual must have been executable.** A name pinned at its limit-up
  could not have been bought, so a buy "missed" on it is not a missed gain and a
  buy "named" on it is not a fill. Such cases are carried but labelled, and they
  never enter an average.

Read-only. It never writes the ledger and never invents a missing price.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, time as time_cls, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

# A call is scored against the session's own closing snapshot, so nothing after
# that session is used. Below this many cases the number is not reported at all:
# a 3-case average is noise dressed as evidence.
MIN_CASES = 20
# The scheduler's last in-session round is 14:57 for a 15:00 close. A session
# whose final observation is earlier than this never reached its close (a
# mid-session restart, a feed outage), so its last snapshot is a midday price
# and must not be treated as the session's close.
SESSION_CLOSE = time_cls(14, 50)
SHANGHAI = timezone(timedelta(hours=8))


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _parse_stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 16 or text[10] not in "T ":
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("/", "-"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=SHANGHAI)


def _round_marks(db: sqlite3.Connection, round_id: str) -> dict[str, dict[str, Any]]:
    """Tradable marks for one round, each with its own executability verdict.

    A mark is executable as a *buy* counterfactual only when the name was not
    already sealed at its published ceiling: a buyer could not have got in.
    """
    marks: dict[str, dict[str, Any]] = {}
    for row in db.execute(
            "SELECT symbol, price_cents, limit_up_cents FROM quote_snapshot "
            "WHERE round_id=? AND price_cents>0", (round_id,)):
        price = row["price_cents"]
        sealed = bool(row["limit_up_cents"] and price >= row["limit_up_cents"])
        marks[row["symbol"]] = {
            "price_cents": price,
            "executable": not sealed,
            "non_executable_reason": "sealed_at_limit_up" if sealed else None,
        }
    return marks


def _prices(marks: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    return {symbol: mark["price_cents"] for symbol, mark in marks.items()}


def _close_of(db: sqlite3.Connection, day: str
              ) -> tuple[str, dict[str, dict[str, Any]]] | str | None:
    """``(close_round_id, marks)``, a skip reason, or None when the day is empty.

    The closing observation only counts when that session actually ran to its
    close. ``finished_at`` carries the source quote time, so a session that
    stopped at 10:30 is recognisable without a wall clock.
    """
    row = db.execute(
        "SELECT round_id, finished_at FROM round "
        "WHERE round_id LIKE ? AND round_id NOT LIKE '%-close' "
        "ORDER BY rowid DESC LIMIT 1", (day + "-%",)).fetchone()
    if not row:
        return None
    stamp = _parse_stamp(row["finished_at"])
    if stamp is None:
        return "close_time_unparseable"
    if stamp.timetz().replace(tzinfo=None) < SESSION_CLOSE:
        return "session_not_closed"
    marks = _round_marks(db, row["round_id"])
    if not marks:
        return "close_has_no_tradable_marks"
    return row["round_id"], marks


def _move_bps(start: int, end: int) -> int | None:
    if start <= 0 or end <= 0:
        return None
    return (end - start) * 10_000 // start


def _window_baseline(marks: Mapping[str, Mapping[str, Any]],
                     close_prices: Mapping[str, int]) -> int | None:
    """Median move of this round's executable names to the close.

    The baseline is measured over exactly the window the individual case is
    measured over, so ``move - baseline`` is an excess over the same stretch of
    time and not an artifact of two different ones.
    """
    drift = [_move_bps(mark["price_cents"], close_prices[symbol])
             for symbol, mark in marks.items()
             if mark["executable"] and close_prices.get(symbol)]
    drift = sorted(move for move in drift if move is not None)
    if not drift:
        return None
    return drift[len(drift) // 2]  # median, robust to a few limit-ups


def _case(round_id: str, close_id: str, symbol: str, regime: str,
          marks: Mapping[str, Mapping[str, Any]],
          close_prices: Mapping[str, int],
          baseline: int | None) -> dict[str, Any]:
    mark = marks.get(symbol)
    move = (_move_bps(mark["price_cents"], close_prices.get(symbol, 0))
            if mark and close_prices.get(symbol) else None)
    executable = bool(mark and mark["executable"] and move is not None)
    reason = (mark["non_executable_reason"] if mark
              else "no_tradable_quote")
    if mark and mark["executable"] and move is None:
        reason = "no_closing_mark"
    return {
        "round_id": round_id,
        "close_round_id": close_id,
        "symbol": symbol,
        "regime": regime,
        "entry_price_cents": mark["price_cents"] if mark else None,
        "move_bps": move,
        "baseline_bps": baseline,
        "excess_bps": (move - baseline
                       if move is not None and baseline is not None else None),
        # A counterfactual that could not have been traded is reported but never
        # averaged: it is a fact about the ledger, not about the model's skill.
        "executable": executable,
        "non_executable_reason": None if executable else reason,
        "window": {"from_round": round_id, "to_round": close_id},
    }


def collect(db: sqlite3.Connection) -> dict[str, Any]:
    """Every AI call with its forward outcome, grouped by market regime."""
    days = [r["d"] for r in db.execute(
        "SELECT DISTINCT substr(round_id,1,10) d FROM round ORDER BY d")]
    blocked_cases: list[dict[str, Any]] = []
    buy_cases: list[dict[str, Any]] = []
    round_cases: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for day in days:
        closing = _close_of(db, day)
        if closing is None:
            continue
        if isinstance(closing, str):
            # Not a closed session: the last snapshot is a midday price, so
            # nothing in this day can be scored as a session outcome.
            skipped.append({"day": day, "reason": closing})
            continue
        close_id, close_marks = closing
        close_prices = _prices(close_marks)
        rows = db.execute(
            "SELECT o.round_id, o.application_json, r.regime FROM ai_observation o "
            "JOIN round r USING(round_id) "
            "WHERE o.round_id LIKE ? AND o.round_id NOT LIKE '%-close' "
            "ORDER BY o.rowid",
            (day + "-%",)).fetchall()
        for row in rows:
            if row["round_id"] == close_id:
                continue  # no forward window inside the session
            try:
                app = json.loads(row["application_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(app, Mapping) or not app.get("accepted"):
                continue
            marks = _round_marks(db, row["round_id"])
            if not marks:
                continue
            # One baseline per round: the same start point, the same close, the
            # same executable universe as every case scored below.
            baseline = _window_baseline(marks, close_prices)
            regime = row["regime"] or "unknown"
            for symbol in app.get("blocked_symbols") or []:
                if not isinstance(symbol, str):
                    continue
                blocked_cases.append(_case(row["round_id"], close_id, symbol,
                                           regime, marks, close_prices, baseline))
            for item in app.get("active_suggestions") or app.get("suggestions") or []:
                if not isinstance(item, Mapping) or item.get("side") != "buy":
                    continue
                symbol = item.get("symbol")
                if not isinstance(symbol, str):
                    continue
                buy_cases.append(_case(row["round_id"], close_id, symbol,
                                       regime, marks, close_prices, baseline))
            # The regime call itself: did the market drift up or down over this
            # same window? Sealed names are excluded so this stays a drift
            # measure and not a disguised copy of the blocked-symbol result.
            moves = [_move_bps(mark["price_cents"], close_prices[symbol])
                     for symbol, mark in marks.items()
                     if mark["executable"] and close_prices.get(symbol)]
            moves = [move for move in moves if move is not None]
            if moves:
                round_cases.append({"round_id": row["round_id"], "regime": regime,
                                    "entry_delta": app.get("entry_delta"),
                                    "bias": app.get("bias"),
                                    "baseline_bps": baseline,
                                    "mean_move_bps": sum(moves) // len(moves)})
    return {"days": days, "blocked": blocked_cases, "buys": buy_cases,
            "rounds": round_cases, "skipped_sessions": skipped,
            "baseline": "same_window_median_move_to_session_close"}


def _summary(cases: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    rows = list(cases)
    executable = [row for row in rows if row.get("executable")]
    moves = [row["move_bps"] for row in executable
             if isinstance(row.get("move_bps"), int)]
    if len(moves) < MIN_CASES:
        return None
    excess = [row["excess_bps"] for row in executable
              if isinstance(row.get("excess_bps"), int)]
    out = {
        "cases": len(moves),
        "executable_cases": len(moves),
        "non_executable": len(rows) - len(executable),
        "mean_bps": sum(moves) // len(moves),
        "up": sum(1 for move in moves if move > 0),
        "down": sum(1 for move in moves if move < 0),
        # The verdict rests on excess, not on the raw move: a rising tide lifts
        # every name, and raw return would call a bad block a missed gain.
        "excess_bps": (sum(excess) // len(excess)) if len(excess) >= MIN_CASES else None,
        "excess_cases": len(excess),
        "beat_market": sum(1 for value in excess if value > 0),
        "lagged_market": sum(1 for value in excess if value < 0),
    }
    return out


def scorecard(memory: Mapping[str, Any]) -> dict[str, Any]:
    """What the model should know about its own track record, per regime.

    `blocked.excess_bps` is the honest verdict: the blocked names' move over the
    round-to-close window minus the market median over the same window. Positive
    means they outperformed from the moment of the call, so refusing them cost
    something real; negative means the block was right even on a day when
    everything rose. `mean_bps` is the raw move and is reported only as context
    -- on a strong day it flatters every block. Cases that could not have been
    traded are counted in `non_executable` and excluded from every average.
    """
    by_regime: dict[str, dict[str, Any]] = {}
    for regime in sorted({c["regime"] for c in memory["blocked"]} |
                         {c["regime"] for c in memory["buys"]} |
                         {c["regime"] for c in memory["rounds"]}):
        blocked = _summary(c for c in memory["blocked"] if c["regime"] == regime)
        buys = _summary(c for c in memory["buys"] if c["regime"] == regime)
        rounds = _summary(({"move_bps": c["mean_move_bps"], "executable": True}
                           for c in memory["rounds"] if c["regime"] == regime))
        if blocked or buys or rounds:
            by_regime[regime] = {"blocked_symbols": blocked, "named_buys": buys,
                                 "market_drift": rounds}
    return {"basis": "same_window_median_move_from_call_round_to_session_close",
            "verdict_on": "excess_bps", "min_cases": MIN_CASES,
            "closed_sessions_only": True,
            "sessions": len(memory["days"]) - len(memory.get("skipped_sessions") or []),
            "skipped_sessions": len(memory.get("skipped_sessions") or []),
            "by_regime": by_regime}


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
