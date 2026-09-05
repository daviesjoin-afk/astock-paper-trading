# -*- coding: utf-8 -*-
"""???????????????????

????????????????????????????????????
??????????????????????????????300?????
"""
import datetime as dt
import json
import os
import sqlite3
from collections import defaultdict

import data_fetcher as dfc

DB_PATH = os.path.join(dfc.CACHE_DIR, "selection_tracking.db")
BENCHMARK_CODE = "BENCH_000300"
BENCHMARK_NAME = "CSI 300"
HORIZONS = (1, 3, 5, 10, 20)
MAX_TRACKING_DAYS = 20
CHINA_TZ = dt.timezone(dt.timedelta(hours=8))


def _today():
    return dt.datetime.now(CHINA_TZ).date()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_schema():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS selection_runs (
                id INTEGER PRIMARY KEY,
                run_date TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                strategy TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                data_asof_date TEXT,
                benchmark_entry_price REAL,
                universe_size INTEGER,
                candidate_count INTEGER,
                selected_count INTEGER,
                executable_count INTEGER,
                source TEXT NOT NULL,
                result_json TEXT NOT NULL,
                UNIQUE(run_date, strategy)
            );
            CREATE TABLE IF NOT EXISTS selection_picks (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES selection_runs(id) ON DELETE CASCADE,
                rank_no INTEGER NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                industry TEXT,
                entry_price REAL,
                score REAL,
                super_net REAL,
                decision_tier TEXT,
                decision_action TEXT,
                snapshot_json TEXT NOT NULL,
                UNIQUE(run_id, code)
            );
            CREATE TABLE IF NOT EXISTS selection_observations (
                id INTEGER PRIMARY KEY,
                pick_id INTEGER NOT NULL REFERENCES selection_picks(id) ON DELETE CASCADE,
                observed_date TEXT NOT NULL,
                price REAL NOT NULL,
                benchmark_price REAL,
                holding_days INTEGER NOT NULL,
                return_pct REAL,
                benchmark_return_pct REAL,
                excess_return_pct REAL,
                UNIQUE(pick_id, observed_date)
            );
            CREATE INDEX IF NOT EXISTS idx_selection_runs_strategy_date
                ON selection_runs(strategy, run_date DESC);
            CREATE INDEX IF NOT EXISTS idx_selection_observations_pick_date
                ON selection_observations(pick_id, observed_date DESC);
            """
        )


def _number(value):
    return float(value) if isinstance(value, (int, float)) else None


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _latest_signal_date(picks):
    dates = []
    for pick in picks:
        frame = dfc.load_shared_kline(pick.get("code"))
        if frame is not None and not frame.empty:
            dates.append(frame.index[-1].date().isoformat())
    return min(dates) if dates else None


def _benchmark_price_on_or_before(day):
    frame = dfc.load_shared_kline(BENCHMARK_CODE)
    if frame is None or frame.empty:
        return None
    rows = frame.loc[frame.index.date <= day]
    if rows.empty:
        return None
    value = rows.iloc[-1].get("close")
    return _number(value)


def refresh_benchmark():
    """Incrementally refresh the sole benchmark bar; never scans the whole universe."""
    cached = dfc.load_shared_kline(BENCHMARK_CODE)
    beg = "20230101"
    if cached is not None and not cached.empty:
        beg = cached.index[-1].date().strftime("%Y%m%d")
    try:
        latest = dfc.fetch_kline(None, beg=beg, secid="1.000300")
    except Exception:
        return False
    if latest is None or latest.empty:
        return False
    if cached is not None and not cached.empty:
        import pandas as pd
        latest = pd.concat([cached[cached.index < latest.index[0]], latest])
    dfc.save_kline(BENCHMARK_CODE, latest)
    dfc.flush_kline_manifest()
    return True


def record_run(result, run_date=None, source="scheduled"):
    """Persist one immutable candidate snapshot per strategy and trading date.

    A same-day retry replaces only that strategy's unfinished snapshot, keeping
    the schedule idempotent while preserving all older research evidence.
    """
    ensure_schema()
    strategy = str(result.get("strategy") or "")
    if not strategy:
        raise ValueError("missing strategy id")
    day = run_date or _today().isoformat()
    picks = list(result.get("picks") or [])
    data_asof = _latest_signal_date(picks)
    bench_entry = _benchmark_price_on_or_before(dt.date.fromisoformat(day))
    generated_at = dt.datetime.now(CHINA_TZ).isoformat(timespec="seconds")
    safe_result = _json_safe(result)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO selection_runs(
                run_date, generated_at, strategy, strategy_name, data_asof_date,
                benchmark_entry_price, universe_size, candidate_count, selected_count,
                executable_count, source, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_date, strategy) DO UPDATE SET
                generated_at=excluded.generated_at,
                strategy_name=excluded.strategy_name,
                data_asof_date=excluded.data_asof_date,
                benchmark_entry_price=excluded.benchmark_entry_price,
                universe_size=excluded.universe_size,
                candidate_count=excluded.candidate_count,
                selected_count=excluded.selected_count,
                executable_count=excluded.executable_count,
                source=excluded.source,
                result_json=excluded.result_json
            """,
            (
                day, generated_at, strategy, str(result.get("strategy_name") or strategy),
                data_asof, bench_entry, result.get("universe_size"),
                result.get("candidate_count"), len(picks), result.get("executable_count"),
                source, json.dumps(safe_result, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        run = conn.execute(
            "SELECT id FROM selection_runs WHERE run_date=? AND strategy=?", (day, strategy)
        ).fetchone()
        if run is not None and str(day) < _today().isoformat():
            # 历史日期的快照视为不可变：对过去 run_date 的 upsert 会删除
            # 既有 picks，FK ON DELETE CASCADE 进而销毁全部 observations，
            # holding_days 等前向验证指标将被永久破坏。同日重试不受影响。
            return {
                "status": "skipped", "reason": "historical run_date is immutable",
                "run_date": day, "strategy": strategy, "run_id": run["id"],
            }
        conn.execute("DELETE FROM selection_picks WHERE run_id=?", (run["id"],))
        for rank, pick in enumerate(picks, 1):
            decision = pick.get("buy_decision") or {}
            conn.execute(
                """
                INSERT INTO selection_picks(
                    run_id, rank_no, code, name, industry, entry_price, score,
                    super_net, decision_tier, decision_action, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["id"], rank, str(pick.get("code") or ""), pick.get("name"),
                    pick.get("industry"), _number(pick.get("price")), _number(pick.get("score")),
                    _number(pick.get("super_net")), decision.get("tier"), decision.get("action"),
                    json.dumps(_json_safe(pick), ensure_ascii=False, separators=(",", ":")),
                ),
            )
    return {"date": day, "strategy": strategy, "saved": len(picks), "data_asof_date": data_asof}


def _latest_snapshot_prices():
    rows = dfc.fetch_market_snapshot()
    by_code = {}
    dates = []
    for row in rows:
        price = _number(row.get("price"))
        quote_at = row.get("quote_at") or ""
        quote_day = str(quote_at)[:10]
        if price is not None and price > 0 and len(quote_day) == 10:
            by_code[str(row.get("code") or "")] = price
            dates.append(quote_day)
    if not dates:
        return None, {}
    # A verified quote date prevents holiday/after-hours stale snapshots being
    # recorded as a fictitious trading day.
    return max(dates), by_code


def update_observations():
    """Append one end-of-day market observation for active candidate snapshots."""
    ensure_schema()
    today = _today().isoformat()
    quote_day, prices = _latest_snapshot_prices()
    if quote_day != today:
        return {"status": "skipped", "reason": "market quote is not a same-day close", "quote_date": quote_day}
    refresh_benchmark()
    benchmark = _benchmark_price_on_or_before(dt.date.fromisoformat(today))
    inserted = 0
    with _connect() as conn:
        active = conn.execute(
            """
            SELECT p.id, p.code, p.entry_price, r.run_date, r.benchmark_entry_price
            FROM selection_picks p JOIN selection_runs r ON r.id=p.run_id
            WHERE r.run_date < ? AND r.run_date >= ?
            """,
            (today, (dt.date.fromisoformat(today) - dt.timedelta(days=45)).isoformat()),
        ).fetchall()
        for row in active:
            price = prices.get(row["code"])
            entry = _number(row["entry_price"])
            if price is None or entry is None or entry <= 0:
                continue
            prior_days = conn.execute(
                "SELECT COUNT(*) AS c FROM selection_observations WHERE pick_id=?",
                (row["id"],),
            ).fetchone()["c"]
            if prior_days >= MAX_TRACKING_DAYS:
                continue
            ret = (price / entry - 1) * 100
            bench_entry = _number(row["benchmark_entry_price"])
            bench_ret = ((benchmark / bench_entry - 1) * 100) if benchmark and bench_entry else None
            excess = ret - bench_ret if bench_ret is not None else None
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO selection_observations(
                    pick_id, observed_date, price, benchmark_price, holding_days,
                    return_pct, benchmark_return_pct, excess_return_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row["id"], today, price, benchmark, prior_days + 1, ret, bench_ret, excess),
            )
            inserted += int(cur.rowcount or 0)
    return {"status": "ok", "date": today, "observed": inserted, "benchmark": benchmark}


def _assessment(samples, horizon):
    if len(samples) < 20:
        return {"state": "accumulating", "label": "Accumulating", "advice": f"Only {horizon}-day evidence is available; parameters remain unchanged."}
    avg_excess = sum(x["excess_return_pct"] for x in samples if x["excess_return_pct"] is not None)
    excess_n = sum(1 for x in samples if x["excess_return_pct"] is not None)
    avg_excess = avg_excess / excess_n if excess_n else None
    win_rate = sum(1 for x in samples if x["return_pct"] > 0) / len(samples) * 100
    if avg_excess is not None and avg_excess <= -2 and win_rate < 45:
        return {"state": "review", "label": "Review needed", "advice": "Sustained underperformance: review out-of-sample and sector splits before any change."}
    if avg_excess is not None and avg_excess > 0 and win_rate >= 50:
        return {"state": "validated", "label": "Keep observing", "advice": "Positive excess return needs evidence across more market regimes."}
    return {"state": "watch", "label": "Keep observing", "advice": "No stable edge yet; preserve rules and continue collecting evidence."}


def _metric_rows(conn, strategy):
    where = "" if not strategy else "WHERE r.strategy=?"
    args = () if not strategy else (strategy,)
    rows = conn.execute(
        f"""
        SELECT p.id AS pick_id, r.strategy, r.strategy_name, r.run_date, p.rank_no,
               p.code, p.name, p.industry, p.entry_price, p.decision_tier,
               o.observed_date, o.price, o.holding_days, o.return_pct,
               o.benchmark_return_pct, o.excess_return_pct
        FROM selection_picks p
        JOIN selection_runs r ON r.id=p.run_id
        LEFT JOIN selection_observations o ON o.pick_id=p.id
        {where}
        ORDER BY r.run_date DESC, p.rank_no, o.holding_days
        """,
        args,
    ).fetchall()
    grouped = defaultdict(list)
    pick_meta = {}
    for row in rows:
        item = dict(row)
        pick_meta[item["pick_id"]] = item
        if item["observed_date"]:
            grouped[item["pick_id"]].append(item)
    return pick_meta, grouped


def _duplicate_picks(conn, max_days=60):
    """按信号日聚合五套策略的重复入选股票。"""
    rows = conn.execute(
        """
        SELECT p.id AS pick_id, r.run_date, r.strategy, r.strategy_name,
               p.rank_no, p.code, p.name, p.industry, p.entry_price,
               p.decision_tier
        FROM selection_picks p
        JOIN selection_runs r ON r.id=p.run_id
        WHERE r.run_date >= date('now', ?)
        ORDER BY r.run_date DESC, p.code, p.rank_no
        """,
        (f"-{max(1, int(max_days))} day",),
    ).fetchall()
    groups = defaultdict(list)
    for row in rows:
        item = dict(row)
        if item.get("code"):
            groups[(item["run_date"], item["code"])].append(item)
    # 读取最新跟踪点，避免重复项只有静态排名没有后续表现。
    latest = {}
    for row in conn.execute(
        """SELECT pick_id, observed_date, price, holding_days, return_pct, excess_return_pct
           FROM selection_observations ORDER BY pick_id, holding_days DESC"""
    ).fetchall():
        latest.setdefault(row["pick_id"], dict(row))
    duplicates = []
    for (run_date, code), items in groups.items():
        by_strategy = {}
        for item in items:
            by_strategy.setdefault(item["strategy"], item)
        if len(by_strategy) < 2:
            continue
        strategy_items = []
        returns = []
        excess = []
        for item in sorted(by_strategy.values(), key=lambda x: x["rank_no"]):
            point = latest.get(item["pick_id"])
            strategy_items.append({
                "strategy": item["strategy"],
                "strategy_name": item["strategy_name"],
                "rank": item["rank_no"],
                "entry_price": item["entry_price"],
                "decision_tier": item["decision_tier"],
            })
            if point and point.get("return_pct") is not None:
                returns.append(point["return_pct"])
            if point and point.get("excess_return_pct") is not None:
                excess.append(point["excess_return_pct"])
        first = items[0]
        duplicates.append({
            "run_date": run_date,
            "code": code,
            "name": first.get("name"),
            "industry": first.get("industry"),
            "strategy_count": len(strategy_items),
            "strategies": strategy_items,
            "latest_return_pct": sum(returns) / len(returns) if returns else None,
            "latest_excess_return_pct": sum(excess) / len(excess) if excess else None,
        })
    duplicates.sort(key=lambda x: (x["run_date"], -x["strategy_count"], x["code"]), reverse=True)
    return duplicates


def dashboard(strategy="", limit=30):
    """Return compact, auditable forward-performance evidence for the UI."""
    ensure_schema()
    with _connect() as conn:
        run_where = "" if not strategy else "WHERE strategy=?"
        run_args = () if not strategy else (strategy,)
        runs = [dict(row) for row in conn.execute(
            f"""SELECT run_date, generated_at, strategy, strategy_name, data_asof_date,
                       selected_count, candidate_count, executable_count, source
                FROM selection_runs {run_where}
                ORDER BY run_date DESC, strategy LIMIT ?""",
            (*run_args, max(1, min(int(limit), 180))),
        ).fetchall()]
        pick_meta, grouped = _metric_rows(conn, strategy)
        duplicate_picks = _duplicate_picks(conn)
    strategy_ids = sorted({item["strategy"] for item in pick_meta.values()} | {r["strategy"] for r in runs})
    strategies = []
    for sid in strategy_ids:
        own_ids = [pid for pid, item in pick_meta.items() if item["strategy"] == sid]
        horizons = []
        for horizon in HORIZONS:
            samples = []
            for pid in own_ids:
                point = next((x for x in grouped.get(pid, []) if x["holding_days"] >= horizon), None)
                if point:
                    samples.append(point)
            avg_return = sum(x["return_pct"] for x in samples) / len(samples) if samples else None
            excess_values = [x["excess_return_pct"] for x in samples if x["excess_return_pct"] is not None]
            avg_excess = sum(excess_values) / len(excess_values) if excess_values else None
            win_rate = sum(1 for x in samples if x["return_pct"] > 0) / len(samples) * 100 if samples else None
            assessment = _assessment(samples, horizon)
            horizons.append({
                "horizon": horizon, "sample_count": len(samples),
                "avg_return_pct": avg_return, "avg_excess_pct": avg_excess,
                "win_rate_pct": win_rate, "assessment": assessment,
            })
        latest_meta = next((item for item in pick_meta.values() if item["strategy"] == sid), {})
        primary = next((x for x in horizons if x["horizon"] == 10), horizons[-1])
        strategies.append({
            "strategy": sid, "strategy_name": latest_meta.get("strategy_name") or sid,
            "pick_count": len(own_ids), "metrics": horizons,
            "assessment": primary["assessment"],
        })
    latest_picks = []
    for pid, meta in pick_meta.items():
        points = grouped.get(pid, [])
        latest = points[-1] if points else None
        latest_picks.append({
            "run_date": meta["run_date"], "strategy": meta["strategy"],
            "strategy_name": meta["strategy_name"], "rank": meta["rank_no"],
            "code": meta["code"], "name": meta["name"], "industry": meta["industry"],
            "entry_price": meta["entry_price"], "decision_tier": meta["decision_tier"],
            "observed_date": latest.get("observed_date") if latest else None,
            "holding_days": latest.get("holding_days") if latest else 0,
            "price": latest.get("price") if latest else None,
            "return_pct": latest.get("return_pct") if latest else None,
            "excess_return_pct": latest.get("excess_return_pct") if latest else None,
        })
    latest_picks.sort(key=lambda x: (x["run_date"], -x["rank"]), reverse=True)
    manifest = dfc.get_kline_manifest()
    dates = [str(item.get("last_date"))[:10] for item in manifest.values() if item.get("last_date")]
    # 15:05 前的当日线可能仍是半根K线；展示给验证页的“完整日”必须回退。
    cutoff = dt.datetime.now(CHINA_TZ).date()
    if dt.datetime.now(CHINA_TZ).time() < dt.time(15, 5):
        cutoff -= dt.timedelta(days=1)
    complete_dates = [value for value in dates if value <= cutoff.isoformat()]
    return {
        "generated_at": dt.datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
        "tracking_days": MAX_TRACKING_DAYS,
        "benchmark": BENCHMARK_NAME,
        "kline_source": "与模拟盘共享：data_cache/klines（前复权日线）",
        "kline_source_version": getattr(dfc, "SHARED_KLINE_SOURCE_VERSION", "unknown"),
        "kline_manifest_updated_at": (max((str(item.get("updated_at")) for item in manifest.values() if item.get("updated_at")), default=None)),
        "kline_latest_complete_date": max(complete_dates) if complete_dates else None,
        "runs": runs,
        "strategies": strategies,
        "latest_picks": latest_picks[:100],
        "duplicate_picks": duplicate_picks[:300],
        "note": "Returns start at the saved signal close and only verified post-close quotes are recorded. Rules are never auto-mutated from a small sample.",
    }
