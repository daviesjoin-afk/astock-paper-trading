# -*- coding: utf-8 -*-
"""盘后模拟交易归因与自进化反馈。

这里的 AI 只负责把已经由程序计算出的证据整理成可读解释，不能把
“可能原因”包装成事实，也不能参与下单或绕过风控。确定性字段（个股
涨跌、大盘/板块贡献、公告事件、行情质量）始终先落库，AI 不可用时仍
会留下完整的规则归因。
"""
from __future__ import annotations

import datetime as dt
import csv
import hashlib
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # Windows minimal Python images may not ship tzdata.
    TZ = dt.timezone(dt.timedelta(hours=8))


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE, "data_cache")
DB_PATH = os.path.join(CACHE_DIR, "adaptive_learning.sqlite3")
BENCHMARK_CACHE_KEY = "BENCH_000300"
ENGINE_VERSION = "trade-attribution-v2-pit"
ATTRIBUTION_READ_COLUMNS = """id,order_id,fill_id,account_id,code,name,side,qty,
    fill_date,fill_at,fill_price,amount,fees,order_status,realized_pnl,asof_date,
    close_price,stock_move_pct,benchmark_move_pct,sector_move_pct,stock_alpha_pct,
    news_impact_score,news_impact_label,news_events,reason_codes,quote_quality,
    ai_status,ai_provider,ai_summary,ai_reason,ai_confidence,horizon_results,
    created_at,updated_at"""


def _now() -> str:
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def _num(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _loads(value, default=None):
    try:
        parsed = json.loads(value) if value else default
        return parsed if parsed is not None else default
    except (TypeError, ValueError):
        return default


def ensure_schema(conn):
    """Create only additive tables; existing paper/adaptive data is untouched."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS adaptive_trade_attributions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL UNIQUE,
            fill_id INTEGER,
            account_id TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 0,
            fill_date TEXT NOT NULL,
            fill_at TEXT,
            fill_price REAL,
            amount REAL,
            fees REAL,
            order_status TEXT NOT NULL,
            realized_pnl REAL,
            asof_date TEXT NOT NULL,
            close_price REAL,
            stock_move_pct REAL,
            benchmark_move_pct REAL,
            sector_move_pct REAL,
            stock_alpha_pct REAL,
            news_impact_score REAL,
            news_impact_label TEXT,
            news_events TEXT NOT NULL DEFAULT '[]',
            reason_codes TEXT NOT NULL DEFAULT '[]',
            quote_quality TEXT NOT NULL DEFAULT 'missing',
            ai_status TEXT NOT NULL DEFAULT 'pending',
            ai_provider TEXT,
            ai_summary TEXT,
            ai_reason TEXT,
            ai_confidence REAL,
            context TEXT NOT NULL DEFAULT '{}',
            horizon_results TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trade_attr_date
            ON adaptive_trade_attributions(fill_date DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_trade_attr_account
            ON adaptive_trade_attributions(account_id, fill_date DESC);
        CREATE TABLE IF NOT EXISTS adaptive_trade_attribution_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            order_count INTEGER NOT NULL DEFAULT 0,
            filled_count INTEGER NOT NULL DEFAULT 0,
            analyzed_count INTEGER NOT NULL DEFAULT 0,
            deterministic_count INTEGER NOT NULL DEFAULT 0,
            updated_horizons INTEGER NOT NULL DEFAULT 0,
            evidence_hash TEXT,
            detail TEXT NOT NULL DEFAULT '{}',
            error_code TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trade_attr_runs_recent
            ON adaptive_trade_attribution_runs(id DESC);
        """
    )


def _parse_date(value, fallback=None):
    text = str(value or "")[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return fallback


def _parse_datetime(value):
    """Parse an event/fill timestamp into one timezone-aware Shanghai time."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def _market_snapshot():
    """Read the full-market snapshot already fetched by the 5-minute loop."""
    paths = (
        os.path.join(CACHE_DIR, "market_snapshot_full.json"),
        os.path.join(CACHE_DIR, "market_snapshot.json"),
    )
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if isinstance(rows, list) and rows:
                return rows, payload.get("saved_at"), os.path.basename(path)
        except (OSError, ValueError, TypeError):
            continue
    return [], None, None


def _quote_maps(kline_cache=None):
    rows, saved_at, source_file = _market_snapshot()
    if not rows:
        try:
            import data_fetcher as dfc
            rows = dfc.fetch_market_snapshot_full(max_age=900) or []
            source_file = "live_market_snapshot"
        except Exception:
            rows = []
    quotes = {}
    sector_values = defaultdict(list)
    for row in rows:
        code = str(row.get("code") or "").strip()
        if len(code) != 6:
            continue
        quotes[code] = dict(row)
        industry = str(row.get("industry") or "").strip()
        pct = _num(row.get("pct"))
        if industry and pct is not None and abs(pct) <= 30:
            sector_values[industry].append(pct)
    sectors = {
        key: round(sum(values) / len(values), 4)
        for key, values in sector_values.items() if values
    }
    benchmark = None
    try:
        import data_fetcher as dfc
        for row in dfc.fetch_indices() or []:
            if str(row.get("code") or "").lower() == "sh000300":
                benchmark = dict(row)
                break
    except Exception:
        benchmark = None
    if benchmark is None:
        # The historical benchmark cache is a safe fallback for a source hiccup;
        # it is explicitly marked as fallback below rather than called realtime.
        try:
            import data_fetcher as dfc
            closes = _load_kline_once(BENCHMARK_CACHE_KEY, kline_cache)
            if closes:
                points = sorted(closes.items())
                close = points[-1][1]
                prior = points[-2][1] if len(points) > 1 else None
                benchmark = {"price": close, "pct": (close / prior - 1) * 100 if close and prior else None,
                             "quote_at": points[-1][0].isoformat()}
        except Exception:
            benchmark = None
    return quotes, sectors, benchmark, {
        "saved_at": saved_at,
        "snapshot_date": _parse_date(saved_at).isoformat() if _parse_date(saved_at) else None,
        "source_file": source_file,
        "rows": len(rows),
    }


KLINE_CLOSE_CACHE_MAX = max(16, int(os.getenv("ATTRIBUTION_KLINE_CACHE_MAX", "128")))


def _read_cached_closes(code):
    """Read only date/close from the shared CSV without constructing pandas."""
    path = os.path.join(CACHE_DIR, "klines", f"{code}.csv")
    if not os.path.exists(path):
        return None
    closes = {}
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            date_key = "date" if "date" in (reader.fieldnames or []) else (reader.fieldnames or [None])[0]
            if not date_key or "close" not in (reader.fieldnames or []):
                return None
            for row in reader:
                day = _parse_date(row.get(date_key))
                price = _num(row.get("close"))
                if day is not None and price is not None and price > 0:
                    closes[day] = price
    except (OSError, ValueError, TypeError, csv.Error):
        return None
    return closes or None


def _load_kline_once(code, kline_cache=None):
    """Load compact closes into a bounded per-run LRU cache.

    Attribution only consumes trading date and close price. Retaining every
    full pandas DataFrame for every historical order made memory grow with
    ledger age and eventually OOM-killed the API container.
    """
    if kline_cache is None:
        kline_cache = {}
    key = str(code)
    if key in kline_cache:
        value = kline_cache.pop(key)
        kline_cache[key] = value
        return value
    value = _read_cached_closes(key)
    if value is None:
        # Compatibility seam for tests and non-CSV deployments. Production
        # uses the shared CSV path above and therefore avoids pandas churn.
        try:
            import data_fetcher as dfc
            frame = dfc.load_cached_kline(key)
            closes = {}
            if frame is not None and not frame.empty and "close" in frame:
                for stamp, raw in frame["close"].items():
                    day = _parse_date(stamp)
                    price = _num(raw)
                    if day is not None and price is not None and price > 0:
                        closes[day] = price
            value = closes or None
        except Exception:
            value = None
    while len(kline_cache) >= KLINE_CLOSE_CACHE_MAX:
        victim = next((item for item in kline_cache if item != BENCHMARK_CACHE_KEY), None)
        if victim is None:
            victim = next(iter(kline_cache))
        kline_cache.pop(victim, None)
    kline_cache[key] = value
    return value


def _cached_close_on(code, target_date, kline_cache=None):
    """Return an exact completed daily close, never a later/current bar."""
    closes = _load_kline_once(code, kline_cache)
    return (closes or {}).get(target_date)


def _point_in_time_quote(code, target_date, quotes, source_meta, kline_cache=None):
    """Resolve a quote at ``target_date`` without using today's snapshot.

    A current intraday snapshot is valid only when its own timestamp belongs
    to the requested date.  For older dates, the exact cached daily bar is the
    only acceptable fallback; a previous or later close would introduce look-
    ahead or wrong-day attribution.
    """
    row = quotes.get(str(code)) or {}
    row_date = _parse_date(row.get("quote_at") or row.get("date") or source_meta.get("saved_at"))
    if row_date == target_date:
        price = _num(row.get("price"))
        if price and price > 0:
            return price, "live_snapshot"
    price = _cached_close_on(code, target_date, kline_cache)
    return (price, "historical_kline") if price else (None, "missing")


def _point_in_time_benchmark(target_date, benchmark, kline_cache=None):
    """Resolve the benchmark close/return for the requested date."""
    row_date = _parse_date((benchmark or {}).get("quote_at") or (benchmark or {}).get("date"))
    current_price = _num((benchmark or {}).get("price"))
    if row_date == target_date and current_price:
        return dict(benchmark), "live_snapshot"
    price = _cached_close_on(BENCHMARK_CACHE_KEY, target_date, kline_cache)
    if price:
        previous = None
        try:
            closes = _load_kline_once(BENCHMARK_CACHE_KEY, kline_cache)
            if closes:
                dates = sorted(closes.items())
                prior = [(day, value) for day, value in dates if day and day < target_date and value and value > 0]
                previous = prior[-1][1] if prior else None
        except Exception:
            previous = None
        pct = (price / previous - 1) * 100 if previous else None
        return {"price": price, "pct": pct, "quote_at": target_date.isoformat()}, "historical_kline"
    return {}, "missing"


def _event_rows(conn, code, fill_date, industry=None, fill_at=None):
    """Collect direct company events and linked major market/industry events."""
    start = f"{fill_date}T00:00:00"
    end = f"{fill_date}T23:59:59"
    events = []
    try:
        direct = conn.execute(
            """SELECT code,name,title,source_name,source_type,evidence_grade,published_at,
                      first_seen_at,event_type,expected_direction,severity,source_url
                 FROM news_events WHERE code=? AND first_seen_at>=? AND first_seen_at<=?
                 ORDER BY first_seen_at DESC LIMIT 12""",
            (str(code), start, end),
        ).fetchall()
        events.extend(dict(row) for row in direct)
    except sqlite3.Error:
        pass
    # Major events are contextual unless linked to the security.  Their
    # verification status is retained so the AI cannot treat a single source
    # as a confirmed cause.
    try:
        linked = conn.execute(
            """SELECT e.title,e.summary,e.source_name,e.source_type,e.evidence_grade,
                      e.published_at,e.first_seen_at,e.event_type,e.significance_score,
                      e.verification_status,e.source_url
                 FROM market_major_events e
                 JOIN market_event_candidate_links l ON l.event_id=e.id
                WHERE l.code=? AND e.first_seen_at>=? AND e.first_seen_at<=?
                ORDER BY e.first_seen_at DESC LIMIT 8""",
            (str(code), start, end),
        ).fetchall()
        for row in linked:
            item = dict(row)
            item["expected_direction"] = 0
            item["severity"] = _num(item.get("significance_score"), 0.0)
            item["event_type"] = item.get("event_type") or "major_market_event"
            item["title"] = item.get("title") or item.get("summary") or "重大市场事件"
            events.append(item)
    except sqlite3.Error:
        pass
    # A same-day event is evidence only if its publication/first-seen time is
    # no later than the actual fill.  Comparing date-only strings allowed an
    # afternoon headline to explain a morning trade.  Unknown/malformed fill
    # timestamps fail closed for same-day news.
    fill_dt = _parse_datetime(fill_at)
    if fill_dt is None:
        events = []
    else:
        visible = []
        for item in events:
            timestamps = []
            for key in ("published_at", "first_seen_at"):
                raw = item.get(key)
                if raw:
                    parsed = _parse_datetime(raw)
                    if parsed is not None:
                        timestamps.append(parsed)
                    else:
                        # A supplied but malformed timestamp is not evidence
                        # that can safely be used for point-in-time attribution.
                        timestamps = []
                        break
            if timestamps and all(value <= fill_dt for value in timestamps):
                visible.append(item)
        events = visible

    # De-duplicate the same headline arriving through direct and major tables.
    unique = {}
    for item in events:
        key = (str(item.get("title") or ""), str(item.get("first_seen_at") or ""))
        unique[key] = item
    return list(unique.values())[:16]


def _news_impact(events):
    score = 0.0
    weighted = 0.0
    rendered = []
    grade_weight = {"A": 1.0, "B": 0.8, "C": 0.5, "D": 0.25}
    for item in events:
        direction = _num(item.get("expected_direction"), 0.0) or 0.0
        severity = max(0.0, min(1.0, _num(item.get("severity"), 0.0) or 0.0))
        weight = grade_weight.get(str(item.get("evidence_grade") or "D").upper(), 0.25)
        score += direction * severity * weight
        weighted += severity * weight
        rendered.append({
            "title": str(item.get("title") or item.get("summary") or "")[:220],
            "event_type": str(item.get("event_type") or "unknown")[:60],
            "source": str(item.get("source_name") or "未知来源")[:80],
            "evidence_grade": str(item.get("evidence_grade") or "D"),
            "expected_direction": int(direction),
            "severity": round(severity, 3),
            "verification_status": item.get("verification_status") or "linked",
            "first_seen_at": item.get("first_seen_at"),
            "source_url": item.get("source_url"),
        })
    if score <= -0.35:
        label = "公告/舆情偏空"
    elif score >= 0.35:
        label = "公告/舆情偏多"
    elif rendered:
        label = "有相关事件但方向不确定"
    else:
        label = "未发现当日相关公告/舆情"
    return round(score, 4), label, rendered, round(weighted, 4)


def _reason_codes(stock_move, benchmark_move, sector_move, news_score, quote_quality, side):
    reasons = []
    if quote_quality not in {"live_snapshot", "live_quote", "historical_kline"}:
        reasons.append("行情质量降级")
    if benchmark_move is not None and benchmark_move <= -0.35:
        reasons.append("大盘拖累")
    elif benchmark_move is not None and benchmark_move >= 0.35:
        reasons.append("大盘助推")
    if sector_move is not None and sector_move <= -0.50:
        reasons.append("板块拖累")
    elif sector_move is not None and sector_move >= 0.50:
        reasons.append("板块助推")
    if news_score <= -0.35:
        reasons.append("公告/舆情偏空")
    elif news_score >= 0.35:
        reasons.append("公告/舆情偏多")
    if stock_move is not None and benchmark_move is not None:
        alpha = stock_move - benchmark_move
        if alpha >= 0.60:
            reasons.append("个股跑赢大盘")
        elif alpha <= -0.60:
            reasons.append("个股弱于大盘")
    if side == "sell":
        reasons.append("卖出后续表现单独跟踪")
    return list(dict.fromkeys(reasons)) or ["暂无足够证据"]


def _fallback_summary(item):
    reasons = "、".join(_loads(item.get("reason_codes"), []) or ["暂无足够证据"])
    move = _num(item.get("stock_move_pct"))
    move_text = "待收盘价" if move is None else f"收盘相对成交价 {move:+.2f}%"
    return f"{move_text}；规则归因：{reasons}。该结论是证据归纳，不代表单一因素已被证明为因果。"


def _horizon_results(code, fill_date, fill_price, current_date, current_close, current_benchmark, kline_cache=None):
    """Use cached daily bars when available to mature T+1/T+3/T+5 outcomes."""
    result = {}
    if not fill_price or fill_price <= 0:
        return result
    closes = dict(_load_kline_once(code, kline_cache) or {})
    dates = []
    if current_close and current_date:
        closes[current_date] = current_close
    if closes:
        dates = sorted(day for day in closes
                       if day > fill_date and (current_date is None or day <= current_date))
    benchmark_dates = dict(_load_kline_once(BENCHMARK_CACHE_KEY, kline_cache) or {})
    live_benchmark = current_benchmark if isinstance(current_benchmark, dict) else {}
    live_benchmark_date = _parse_date(
        live_benchmark.get("quote_at") or live_benchmark.get("date")
    )
    live_benchmark_price = _num(live_benchmark.get("price"))
    if live_benchmark_date and live_benchmark_price and live_benchmark_price > 0:
        benchmark_dates[live_benchmark_date] = live_benchmark_price
    for horizon in (1, 3, 5):
        if len(dates) < horizon:
            continue
        target = dates[horizon - 1]
        target_close = closes[target]
        move = (target_close / fill_price - 1) * 100
        bench_move = None
        # The benchmark is the cumulative index move from the last valid
        # close on/before the fill date to the target close.  Using today's
        # single-session pct for T+3/T+5 understated or inverted multi-day
        # excess returns.
        start_candidates = [day for day in sorted(benchmark_dates) if day <= fill_date]
        start_day = start_candidates[-1] if start_candidates else None
        target_benchmark = benchmark_dates.get(target)
        if start_day and benchmark_dates.get(start_day) and target_benchmark:
            bench_move = (target_benchmark / benchmark_dates[start_day] - 1) * 100
        result[f"{horizon}d"] = {"target_date": target.isoformat(), "stock_return_pct": round(move, 4),
                                 "benchmark_return_pct": round(bench_move, 4) if bench_move is not None else None,
                                 "excess_return_pct": round(move - bench_move, 4) if bench_move is not None else None,
                                 "mature": True}
    return result


def _all_horizons_mature(value):
    payload = _loads(value, {}) or {}
    return all(bool((payload.get(f"{horizon}d") or {}).get("mature")) for horizon in (1, 3, 5))


def _order_date(order):
    for key in ("fill_date", "executed_at", "created_at"):
        value = str(order.get(key) or "")
        day = _parse_date(value)
        if day:
            return day
    return None


def _paper_orders(paper_db_path, target_date):
    if not os.path.exists(paper_db_path):
        return []
    conn = sqlite3.connect(paper_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT o.id,o.account_id,o.side,o.code,o.name,o.qty,
                      o.planned_price,o.amount,o.fees,o.status,o.reason,
                      o.realized_pnl,o.created_at,o.executed_at,
                      length(COALESCE(o.risk_payload,'')) AS risk_payload_bytes,
                      f.id AS fill_id,f.price AS fill_price,f.amount AS fill_amount,
                      f.fees AS fill_fees,f.fill_date,f.quote_at AS fill_quote_at,f.assumption AS fill_assumption
                 FROM paper_orders o LEFT JOIN paper_fills f ON f.order_id=o.id
                WHERE substr(COALESCE(f.fill_date,o.executed_at,o.created_at),1,10)<=?
                ORDER BY o.id""",
            (target_date.isoformat(),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _write_deterministic(conn, order, target_date, quotes, sectors, benchmark, source_meta, kline_cache=None):
    order_id = int(order["id"])
    code = str(order.get("code") or "")
    fill_date = _parse_date(order.get("fill_date")) or _order_date(order) or target_date
    fill_price = _num(order.get("fill_price"), _num(order.get("planned_price")))
    if order.get("fill_id") and _num(order.get("fill_price")) is not None:
        fill_price = _num(order.get("fill_price"))
    quote = quotes.get(code) or {}
    close_price, quote_quality = _point_in_time_quote(code, target_date, quotes, source_meta, kline_cache)
    stock_move = ((close_price / fill_price - 1) * 100) if close_price and fill_price and fill_price > 0 else None
    pit_benchmark, benchmark_quality = _point_in_time_benchmark(target_date, benchmark, kline_cache)
    benchmark_move = _num(pit_benchmark.get("pct"))
    industry = str(order.get("industry") or (quote.get("industry") if quote else "") or "").strip()
    # The sector map is built from the current full-market snapshot.  It is
    # only valid for the same business date; historical rows must not inherit
    # today's sector move.
    sector_move = (_num(sectors.get(industry)) if industry and
                   source_meta.get("snapshot_date") == target_date.isoformat() else None)
    fill_at = order.get("fill_quote_at") or order.get("executed_at") or order.get("created_at")
    events = _event_rows(conn, code, fill_date, industry=industry, fill_at=fill_at)
    news_score, news_label, news_items, news_weight = _news_impact(events)
    alpha = stock_move - benchmark_move if stock_move is not None and benchmark_move is not None else None
    reasons = _reason_codes(stock_move, benchmark_move, sector_move, news_score, quote_quality, str(order.get("side") or ""))
    context = {
        "order_reason": str(order.get("reason") or "")[:500],
        # The immutable full decision snapshot remains in paper_orders and is
        # addressable by order_id. Copying it into every attribution row made
        # both databases and the learning process grow without bound.
        "paper_order_id": order_id,
        "risk_payload_bytes": int(order.get("risk_payload_bytes") or 0),
        "fill_assumption": order.get("fill_assumption"),
        "quote_source": source_meta.get("source_file"),
        "quote_date": target_date.isoformat() if close_price is not None else None,
        "benchmark_quality": benchmark_quality,
        "quote_rows": source_meta.get("rows"),
        "news_weight": news_weight,
        "industry": industry,
    }
    prior = conn.execute("SELECT ai_status,ai_provider,ai_summary,ai_reason,ai_confidence,horizon_results FROM adaptive_trade_attributions WHERE order_id=?", (order_id,)).fetchone()
    horizon = _loads(prior["horizon_results"], {}) if prior else {}
    horizon.update(_horizon_results(code, fill_date, fill_price, target_date, close_price, pit_benchmark, kline_cache))
    now = _now()
    values = (
        order_id, order.get("fill_id"), order.get("account_id"), code, order.get("name"), order.get("side") or "unknown",
        int(order.get("qty") or 0), fill_date.isoformat(), order.get("fill_quote_at") or order.get("executed_at"),
        fill_price, _num(order.get("fill_amount"), _num(order.get("amount"))), _num(order.get("fill_fees"), _num(order.get("fees"))),
        order.get("status") or "unknown", _num(order.get("realized_pnl")), target_date.isoformat(), close_price,
        stock_move, benchmark_move, sector_move, alpha, news_score, news_label, _json(news_items), _json(reasons),
        quote_quality, (prior["ai_status"] if prior else ("skipped_not_filled" if not order.get("fill_id") else "pending")),
        prior["ai_provider"] if prior else None, prior["ai_summary"] if prior else None,
        prior["ai_reason"] if prior else None, prior["ai_confidence"] if prior else None, _json(context), _json(horizon), now, now,
    )
    conn.execute(
        """INSERT INTO adaptive_trade_attributions(
           order_id,fill_id,account_id,code,name,side,qty,fill_date,fill_at,fill_price,amount,fees,
           order_status,realized_pnl,asof_date,close_price,stock_move_pct,benchmark_move_pct,sector_move_pct,
           stock_alpha_pct,news_impact_score,news_impact_label,news_events,reason_codes,quote_quality,
           ai_status,ai_provider,ai_summary,ai_reason,ai_confidence,context,horizon_results,created_at,updated_at)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(order_id) DO UPDATE SET
             fill_id=excluded.fill_id,account_id=excluded.account_id,code=excluded.code,name=excluded.name,
             side=excluded.side,qty=excluded.qty,fill_date=excluded.fill_date,fill_at=excluded.fill_at,
             fill_price=excluded.fill_price,amount=excluded.amount,fees=excluded.fees,order_status=excluded.order_status,
             realized_pnl=excluded.realized_pnl,asof_date=excluded.asof_date,close_price=excluded.close_price,
             stock_move_pct=excluded.stock_move_pct,benchmark_move_pct=excluded.benchmark_move_pct,
             sector_move_pct=excluded.sector_move_pct,stock_alpha_pct=excluded.stock_alpha_pct,
             news_impact_score=excluded.news_impact_score,news_impact_label=excluded.news_impact_label,
             news_events=excluded.news_events,reason_codes=excluded.reason_codes,quote_quality=excluded.quote_quality,
             context=excluded.context,horizon_results=excluded.horizon_results,updated_at=excluded.updated_at""",
        values,
    )
    return {"order_id": order_id, "account_id": order.get("account_id"), "filled": bool(order.get("fill_id")),
            "code": code, "reason_codes": reasons, "stock_move_pct": stock_move, "news_impact_label": news_label,
            "quote_quality": quote_quality, "benchmark_quality": benchmark_quality}


def _ai_batches(items, config, trigger, conn_factory):
    """Best-effort AI enrichment. A missing key/provider never deletes evidence."""
    if not items:
        return {"status": "no_pending", "analyzed": 0, "deterministic": 0, "error": None}
    try:
        import deepseek_advisor
        if not bool((config or {}).get("trade_attribution_ai_enabled", True)):
            raise RuntimeError("ai_disabled")
        if not deepseek_advisor.configured():
            raise RuntimeError("api_key_missing")
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        with conn_factory() as conn:
            for item in items:
                conn.execute("UPDATE adaptive_trade_attributions SET ai_status='deterministic_fallback',ai_provider=NULL,ai_reason=?,ai_summary=?,updated_at=? WHERE order_id=?",
                             (reason, _fallback_summary(item), _now(), item["order_id"]))
        return {"status": "deterministic_fallback", "analyzed": 0, "deterministic": len(items), "error": reason}
    analyzed = 0
    errors = []
    for start in range(0, len(items), 20):
        batch = items[start:start + 20]
        evidence = [{
            "order_id": item["order_id"], "strategy": item.get("account_id"), "code": item.get("code"),
            "name": item.get("name"), "side": item.get("side"), "qty": item.get("qty"),
            "fill_date": item.get("fill_date"), "fill_price": item.get("fill_price"),
            "close_price": item.get("close_price"), "stock_move_pct": item.get("stock_move_pct"),
            "benchmark_move_pct": item.get("benchmark_move_pct"), "sector_move_pct": item.get("sector_move_pct"),
            "stock_alpha_pct": item.get("stock_alpha_pct"), "news_impact_label": item.get("news_impact_label"),
            "news_events": _loads(item.get("news_events"), []), "reason_codes": _loads(item.get("reason_codes"), []),
            "order_reason": item.get("order_reason"),
        } for item in batch]
        system = (
            "你是A股模拟盘盘后归因助手。只能根据输入中的确定性证据解释可能原因，"
            "不能把相关性写成已证实因果，不能补造公告、资金或行情；大盘拖累、板块拖累、"
            "公告影响、个股超额都必须引用对应字段。只输出严格JSON："
            '{"analyses":[{"order_id":1,"summary":"中文摘要","primary_causes":["大盘拖累"],"confidence":0}]}。'
        )
        user = "请逐笔返回分析，不要遗漏order_id。置信度0-100，证据不足就写‘证据不足’。\n" + _json(evidence)
        try:
            import deepseek_advisor
            response, _, _ = deepseek_advisor.call_json(system, user, max_tokens=2600)
            rows = response.get("analyses") if isinstance(response, dict) else []
            mapping = {int(row.get("order_id")): row for row in rows if isinstance(row, dict) and str(row.get("order_id") or "").isdigit()}
            with conn_factory() as conn:
                for item in batch:
                    row = mapping.get(int(item["order_id"]))
                    if not row:
                        conn.execute("UPDATE adaptive_trade_attributions SET ai_status='deterministic_fallback',ai_reason='AI未返回该笔记录',ai_summary=?,updated_at=? WHERE order_id=?",
                                     (_fallback_summary(item), _now(), item["order_id"]))
                        continue
                    causes = row.get("primary_causes") if isinstance(row.get("primary_causes"), list) else []
                    summary = str(row.get("summary") or "").strip()[:800]
                    confidence = _num(row.get("confidence"), 0.0)
                    conn.execute("UPDATE adaptive_trade_attributions SET ai_status='completed',ai_provider='DeepSeek',ai_summary=?,ai_reason=?,ai_confidence=?,updated_at=? WHERE order_id=?",
                                 (summary or _fallback_summary(item), _json([str(c)[:80] for c in causes[:6]]), confidence,
                                  _now(), item["order_id"]))
                    analyzed += 1
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{exc}")
            with conn_factory() as conn:
                for item in batch:
                    conn.execute("UPDATE adaptive_trade_attributions SET ai_status='failed_fallback',ai_reason=?,ai_summary=?,updated_at=? WHERE order_id=?",
                                 (type(exc).__name__, _fallback_summary(item), _now(), item["order_id"]))
    return {"status": "completed" if analyzed else "deterministic_fallback", "analyzed": analyzed,
            "deterministic": len(items) - analyzed, "error": ";".join(errors)[:500] if errors else None}


def _pending_items(conn, target_date):
    rows = conn.execute(
        """SELECT order_id,account_id,code,name,side,qty,fill_date,fill_price,
                  close_price,stock_move_pct,benchmark_move_pct,sector_move_pct,
                  stock_alpha_pct,news_impact_label,news_events,reason_codes,
                  CASE WHEN json_valid(context) THEN json_extract(context,'$.order_reason') END AS order_reason
             FROM adaptive_trade_attributions
           WHERE fill_date<=? AND order_status='filled'
             AND (ai_status IN ('pending','failed_fallback','deterministic_fallback') OR ai_summary IS NULL)
           ORDER BY id DESC LIMIT 120""",
        (target_date.isoformat(),),
    ).fetchall()
    return [dict(row) for row in rows]


def run_close_attribution(connect_factory, paper_db_path, trade_date=None, config=None, trigger="scheduled-close"):
    """Run deterministic attribution, update mature horizons, then enrich with AI."""
    target_date = _parse_date(trade_date, dt.datetime.now(TZ).date())
    started = _now()
    # Fetch network-backed evidence before opening the adaptive SQLite write
    # transaction.  Missing current rows remain explicitly missing instead of
    # triggering a per-order network call while the transaction is held.
    orders = _paper_orders(paper_db_path, target_date)
    # Share one immutable per-run K-line load cache across current and
    # historical attribution updates.  It never relaxes PIT bounds: every
    # consumer still filters by its requested date before using a bar.
    kline_cache = {}
    quotes, sectors, benchmark, source_meta = _quote_maps(kline_cache)
    if target_date == dt.datetime.now(TZ).date():
        missing_codes = sorted({str(order.get("code") or "") for order in orders
                                if str(order.get("code") or "") not in quotes})
        if missing_codes:
            try:
                import data_fetcher as dfc
                for row in dfc.fetch_realtime_for_codes(missing_codes) or []:
                    code = str(row.get("code") or "")
                    if _parse_date(row.get("quote_at") or row.get("date")) == target_date:
                        quotes[code] = dict(row)
            except Exception:
                pass
    with connect_factory() as conn:
        ensure_schema(conn)
        today_items = []
        filled = 0
        deterministic_count = 0
        for order in orders:
            if _order_date(order) != target_date:
                continue
            item = _write_deterministic(conn, order, target_date, quotes, sectors, benchmark, source_meta, kline_cache)
            today_items.append(item)
            deterministic_count += 1
            filled += int(item["filled"])
        # Re-run all historical rows through the deterministic horizon updater;
        # it is cheap because it uses local CSV caches and keeps T+1/T+3/T+5
        # outcomes available to the next evolution cycle.
        horizon_updates = 0
        for order in orders:
            if _order_date(order) is None or _order_date(order) > target_date:
                continue
            existing = conn.execute(
                "SELECT horizon_results FROM adaptive_trade_attributions WHERE order_id=?",
                (int(order["id"]),),
            ).fetchone()
            if existing and _order_date(order) != target_date:
                if _all_horizons_mature(existing["horizon_results"]):
                    continue
                _write_deterministic(conn, order, target_date, quotes, sectors, benchmark, source_meta, kline_cache)
                horizon_updates += 1
        pending = _pending_items(conn, target_date)
        detail = {"today_orders": len(today_items), "today_filled": filled,
                  "source": source_meta, "benchmark": benchmark,
                  "horizon_updates": horizon_updates, "pending_ai": len(pending)}
        evidence_hash = hashlib.sha256(_json(detail).encode("utf-8")).hexdigest()
    ai = _ai_batches(pending, config or {}, trigger, connect_factory)
    status = "completed" if ai.get("status") in {"completed", "deterministic_fallback", "no_pending"} else "partial"
    detail.update({"ai": ai, "engine_version": ENGINE_VERSION})
    with connect_factory() as conn:
        ensure_schema(conn)
        conn.execute(
            """INSERT INTO adaptive_trade_attribution_runs(
               trade_date,trigger,status,provider,model,order_count,filled_count,analyzed_count,
               deterministic_count,updated_horizons,evidence_hash,detail,error_code,started_at,finished_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (target_date.isoformat(), str(trigger or "scheduled-close")[:80], status,
             "DeepSeek" if ai.get("analyzed") else None, None, len(today_items), filled,
             int(ai.get("analyzed") or 0), int(ai.get("deterministic") or 0), horizon_updates,
             evidence_hash, _json(detail), ai.get("error"), started, _now()),
        )
        summary = summary_from_conn(conn)
    return {"status": status, "trade_date": target_date.isoformat(), "detail": detail, "summary": summary}


def summary_from_conn(conn, limit=120):
    ensure_schema(conn)
    rows = [dict(row) for row in conn.execute(
        f"SELECT {ATTRIBUTION_READ_COLUMNS} FROM adaptive_trade_attributions ORDER BY fill_date DESC,id DESC LIMIT ?", (int(limit),)
    )]
    by_account = defaultdict(list)
    reason_counts = Counter()
    for row in rows:
        by_account[str(row.get("account_id") or "unknown")].append(row)
        for reason in (_loads(row.get("reason_codes"), []) or []):
            reason_counts[str(reason)] += 1
    account_summary = {}
    for account_id, items in by_account.items():
        filled = [row for row in items if row.get("order_status") == "filled"]
        moves = [_num(row.get("stock_move_pct")) for row in filled]
        alphas = [_num(row.get("stock_alpha_pct")) for row in filled]
        news = [_num(row.get("news_impact_score"), 0.0) for row in filled]
        account_summary[account_id] = {
            "records": len(items), "filled": len(filled),
            "mean_stock_move_pct": round(sum(x for x in moves if x is not None) / max(len([x for x in moves if x is not None]), 1), 4) if any(x is not None for x in moves) else None,
            "mean_alpha_pct": round(sum(x for x in alphas if x is not None) / max(len([x for x in alphas if x is not None]), 1), 4) if any(x is not None for x in alphas) else None,
            "mean_market_move_pct": round(sum(_num(row.get("benchmark_move_pct"), 0.0) for row in filled) / max(len(filled), 1), 4) if filled else None,
            "negative_news_records": sum(1 for score in news if score < -0.35),
            "ai_completed": sum(1 for row in filled if row.get("ai_status") == "completed"),
        }
    latest_run = conn.execute("SELECT * FROM adaptive_trade_attribution_runs ORDER BY id DESC LIMIT 1").fetchone()
    latest = dict(latest_run) if latest_run else None
    if latest:
        latest["detail"] = _loads(latest.get("detail"), {})
    return {"engine_version": ENGINE_VERSION, "records": len(rows), "by_account": dict(account_summary),
            "reason_counts": dict(reason_counts), "latest_run": latest,
            "recent": rows[:30]}


def overview(conn, limit=120):
    return summary_from_conn(conn, limit=limit)


def records(conn, limit=160, account_id=None, trade_date=None):
    ensure_schema(conn)
    clauses, params = [], []
    if account_id:
        clauses.append("account_id=?"); params.append(str(account_id))
    if trade_date:
        day = _parse_date(trade_date)
        if day:
            clauses.append("fill_date=?"); params.append(day.isoformat())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = [dict(row) for row in conn.execute(
        f"SELECT {ATTRIBUTION_READ_COLUMNS} FROM adaptive_trade_attributions{where} ORDER BY fill_date DESC,id DESC LIMIT ?",
        (*params, max(20, min(int(limit), 500))),
    )]
    for row in rows:
        for field in ("news_events", "reason_codes", "horizon_results", "ai_reason"):
            if field in row and field != "ai_reason":
                row[field] = _loads(row[field], [] if field in {"news_events", "reason_codes"} else {})
    return {"records": rows, "count": len(rows), "asof": _now()}
