# -*- coding: utf-8 -*-
"""Paper-strategy research evidence ledger.

This module is intentionally read-only from the execution engine's point of
view.  It stores the exact candidate inputs and later observed close prices for
the five paper strategies, but it never changes a score, an order, a risk
parameter, or an account balance.  A candidate is therefore evidence only
until a separately reviewed experiment is promoted.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from collections import defaultdict
from strategy_registry import labels as strategy_labels


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE, "data_cache", "paper_research.sqlite3")
VERSION = "paper-research-shadow-v1"
HORIZONS = (1, 3, 5, 10, 20)
CHINA_TZ = dt.timezone(dt.timedelta(hours=8))

STRATEGY_NAMES = strategy_labels()


def _now() -> str:
    return dt.datetime.now(CHINA_TZ).isoformat(timespec="seconds")


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_schema():
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_research_runs (
                id INTEGER PRIMARY KEY,
                signal_date TEXT NOT NULL,
                account_id TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                model_family TEXT NOT NULL,
                research_version TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                factor_asof_date TEXT,
                factor_oldest_date TEXT,
                universe_size INTEGER,
                eligible_count INTEGER,
                candidate_count INTEGER,
                data_quality_json TEXT NOT NULL,
                market_json TEXT NOT NULL,
                meta_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'shadow',
                UNIQUE(signal_date, account_id, research_version)
            );
            CREATE TABLE IF NOT EXISTS paper_research_candidates (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES paper_research_runs(id) ON DELETE CASCADE,
                rank_no INTEGER NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                industry TEXT,
                entry_price REAL,
                base_score REAL,
                context_score REAL,
                final_score REAL,
                score_components_json TEXT NOT NULL,
                factor_snapshot_json TEXT NOT NULL,
                candidate_json TEXT NOT NULL,
                UNIQUE(run_id, code)
            );
            CREATE TABLE IF NOT EXISTS paper_research_observations (
                id INTEGER PRIMARY KEY,
                candidate_id INTEGER NOT NULL REFERENCES paper_research_candidates(id) ON DELETE CASCADE,
                observed_date TEXT NOT NULL,
                price REAL NOT NULL,
                holding_days INTEGER NOT NULL,
                return_pct REAL,
                UNIQUE(candidate_id, observed_date)
            );
            CREATE INDEX IF NOT EXISTS idx_paper_research_runs_strategy_date
                ON paper_research_runs(account_id, signal_date DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_research_observations_candidate_date
                ON paper_research_observations(candidate_id, observed_date DESC);
            """
        )


def _date_text(value) -> str:
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value or "")[:10]


def record_shadow_run(account_id, candidates, meta=None, market=None, signal_date=None):
    """Store the first complete close snapshot for one simulated strategy.

    Same-day retries deliberately keep the first accepted snapshot.  This makes
    research evidence immutable instead of allowing a retry to silently rewrite
    the past with later information.
    """
    ensure_schema()
    meta = dict(meta or {})
    market = dict(market or {})
    day = _date_text(signal_date) or dt.datetime.now(CHINA_TZ).date().isoformat()
    account_id = str(account_id or "")
    if account_id not in STRATEGY_NAMES:
        raise ValueError("research ledger only accepts registered paper strategies")
    rows = [dict(item) for item in (candidates or []) if str(item.get("code") or "")]
    if not rows:
        return {"status": "skipped", "reason": "no eligible paper candidates", "date": day}

    factor_date = _date_text(meta.get("factor_date"))
    factor_oldest = _date_text(meta.get("factor_oldest_date"))
    universe_size = int(meta.get("usable_factor_rows") or 0)
    quality = {
        "factor_asof_date": factor_date or None,
        "factor_oldest_date": factor_oldest or None,
        "universe_size": universe_size,
        "dropped_stale_rows": int(meta.get("dropped_stale_rows") or 0),
        "max_factor_lag": meta.get("max_factor_lag"),
        "security_scope": meta.get("security_scope"),
        "quote_source": meta.get("flow_source"),
        # Financial disclosure timestamps are not yet supplied by the source.
        # Mark that limitation explicitly; never imply point-in-time proof.
        "financial_point_in_time": "unverified_disclosure_timestamp",
    }
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_research_runs(
                signal_date, account_id, strategy_name, model_family, research_version,
                generated_at, factor_asof_date, factor_oldest_date, universe_size,
                eligible_count, candidate_count, data_quality_json, market_json,
                meta_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'shadow')
            """,
            (
                day, account_id, STRATEGY_NAMES[account_id],
                str(meta.get("model_family") or account_id), VERSION, _now(),
                factor_date or None, factor_oldest or None, universe_size,
                int(meta.get("raw_candidate_count") or len(rows)), len(rows),
                json.dumps(_json_safe(quality), ensure_ascii=False, separators=(",", ":")),
                json.dumps(_json_safe(market), ensure_ascii=False, separators=(",", ":")),
                json.dumps(_json_safe(meta), ensure_ascii=False, separators=(",", ":")),
            ),
        )
        run = conn.execute(
            """SELECT id FROM paper_research_runs
               WHERE signal_date=? AND account_id=? AND research_version=?""",
            (day, account_id, VERSION),
        ).fetchone()
        if not run:
            raise RuntimeError("cannot load research run")
        if not cur.rowcount:
            return {"status": "existing", "date": day, "account_id": account_id, "saved": 0}
        for rank, candidate in enumerate(rows, 1):
            components = candidate.get("score_components") or {}
            factors = candidate.get("factor_snapshot") or {}
            conn.execute(
                """
                INSERT INTO paper_research_candidates(
                    run_id, rank_no, code, name, industry, entry_price, base_score,
                    context_score, final_score, score_components_json,
                    factor_snapshot_json, candidate_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["id"], rank, str(candidate.get("code")), candidate.get("name"),
                    candidate.get("industry"), _number(candidate.get("price")),
                    _number(components.get("base_score")),
                    _number(components.get("context_score")), _number(candidate.get("score")),
                    json.dumps(_json_safe(components), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(_json_safe(factors), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(_json_safe(candidate), ensure_ascii=False, separators=(",", ":")),
                ),
            )
    return {"status": "saved", "date": day, "account_id": account_id, "saved": len(rows)}


def update_observations(observed_date, market_rows):
    """Append a close-price observation without relying on a future bar."""
    ensure_schema()
    day = _date_text(observed_date)
    prices = {}
    for row in market_rows or []:
        code = str(row.get("code") or "")
        quote_day = _date_text(row.get("quote_at"))
        price = _number(row.get("price"))
        if code and quote_day == day and price is not None and price > 0:
            prices[code] = price
    if not prices:
        return {"status": "skipped", "reason": "no same-day market prices", "date": day}
    inserted = 0
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.code, c.entry_price
            FROM paper_research_candidates c
            JOIN paper_research_runs r ON r.id=c.run_id
            WHERE r.signal_date < ? AND r.signal_date >= date(?, '-50 day')
            """,
            (day, day),
        ).fetchall()
        for row in rows:
            price = prices.get(row["code"])
            entry = _number(row["entry_price"])
            if price is None or entry is None or entry <= 0:
                continue
            previous = conn.execute(
                "SELECT COUNT(*) AS count FROM paper_research_observations WHERE candidate_id=?",
                (row["id"],),
            ).fetchone()["count"]
            if previous >= max(HORIZONS):
                continue
            result = conn.execute(
                """
                INSERT OR IGNORE INTO paper_research_observations(
                    candidate_id, observed_date, price, holding_days, return_pct
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (row["id"], day, price, previous + 1, (price / entry - 1.0) * 100.0),
            )
            inserted += int(result.rowcount or 0)
    return {"status": "ok", "date": day, "observed": inserted}


def dashboard(limit=40):
    """Small read model for later UI integration; it never exposes a buy signal."""
    ensure_schema()
    with _connect() as conn:
        runs = [dict(row) for row in conn.execute(
            """
            SELECT signal_date, account_id, strategy_name, model_family, generated_at,
                   factor_asof_date, factor_oldest_date, universe_size,
                   eligible_count, candidate_count, data_quality_json, status
            FROM paper_research_runs
            ORDER BY signal_date DESC, account_id
            LIMIT ?
            """,
            (max(1, min(int(limit), 180)),),
        ).fetchall()]
        metrics = defaultdict(dict)
        for horizon in HORIZONS:
            rows = conn.execute(
                """
                SELECT r.account_id, o.return_pct
                FROM paper_research_observations o
                JOIN paper_research_candidates c ON c.id=o.candidate_id
                JOIN paper_research_runs r ON r.id=c.run_id
                WHERE o.holding_days >= ? AND o.return_pct IS NOT NULL
                """,
                (horizon,),
            ).fetchall()
            grouped = defaultdict(list)
            for row in rows:
                grouped[row["account_id"]].append(float(row["return_pct"]))
            for account_id, values in grouped.items():
                metrics[account_id][str(horizon)] = {
                    "samples": len(values),
                    "avg_return_pct": round(sum(values) / len(values), 3),
                    "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 1),
                    "state": "accumulating" if len(values) < 20 else "shadow_review",
                }
    for row in runs:
        row["data_quality"] = json.loads(row.pop("data_quality_json") or "{}")
    # Keep newly registered strategies visible before their first eligible
    # snapshot.  The UI can then distinguish “no run yet” from “not supported”
    # instead of silently dropping the account from the evidence page.
    for account_id in STRATEGY_NAMES:
        metrics.setdefault(account_id, {})
    return {
        "version": VERSION,
        "mode": "shadow_only",
        "message": "研究台账记录五套模拟盘策略的候选与后续表现，不参与下单或自动调参。",
        "strategies": [
            {"id": account_id, "name": name}
            for account_id, name in STRATEGY_NAMES.items()
        ],
        "runs": runs,
        "metrics": {account: dict(values) for account, values in metrics.items()},
    }
