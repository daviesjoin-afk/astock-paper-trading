# -*- coding: utf-8 -*-
"""Pure point-in-time decision audit helpers for paper trading.

This module owns the decision-snapshot serializer: it is the single source of
truth for the audit envelope, and ``paper_trading`` exposes a compatibility
facade that delegates here. It performs no database writes and no network I/O,
and every runtime-only input (K-line loader, news scan metadata, risk version,
clock) is an explicit dependency injected by the caller.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd


DECISION_SNAPSHOT_VERSION = "decision-snapshot-v1"
DEFAULT_RISK_VERSION = "paper-risk-v4"
_DEFAULT_NEWS_SCAN_META = {"observed_at": None, "stale": False, "error": None}


def _date(value=None):
    if value is None:
        return dt.date.today()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def snapshot_safe(value):
    """Return JSON-safe values without turning missing evidence into strings."""
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date, dt.time, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): snapshot_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [snapshot_safe(item) for item in value]
    if isinstance(value, float):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return snapshot_safe(item())
        except (TypeError, ValueError):
            pass
    return value


def _snapshot_date(value):
    """Normalise a replay date, returning None for invalid/future-free input."""
    if value is None or value == "":
        return None
    try:
        return _date(value).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _snapshot_first(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _snapshot_kline(kline, asof_date=None):
    """Serialise every completed daily bar and explicitly count future bars."""
    if kline is None or not hasattr(kline, "iterrows"):
        return {
            "source": "unknown",
            "rows": [],
            "count": 0,
            "first_date": None,
            "last_date": None,
            "future_rows": 0,
            "status": "unknown",
        }
    cutoff = _snapshot_date(asof_date)
    rows, future_rows = [], 0
    raw_columns = getattr(kline, "columns", None)
    columns = list(raw_columns) if raw_columns is not None else []
    for index, row in kline.iterrows():
        try:
            bar_date = _date(index).isoformat()
        except (TypeError, ValueError, OverflowError):
            bar_date = str(index)[:10] or None
        if cutoff and bar_date and bar_date > cutoff:
            future_rows += 1
            continue
        item = {"date": bar_date}
        for column in columns:
            try:
                item[str(column)] = snapshot_safe(row[column])
            except (KeyError, TypeError, IndexError):
                item[str(column)] = None
        rows.append(item)
    dates = [item.get("date") for item in rows if item.get("date")]
    max_stored_bars = 120
    omitted_rows = max(0, len(rows) - max_stored_bars)
    if omitted_rows:
        rows = rows[-max_stored_bars:]
    source = snapshot_safe(getattr(kline, "attrs", {}).get("source")) or "unknown"
    return {
        "source": source,
        "rows": rows,
        "count": len(rows) + omitted_rows,
        "rows_stored": len(rows),
        "omitted_rows": omitted_rows,
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
        "future_rows": future_rows,
        "status": (
            "ok"
            if rows and not future_rows
            else ("future_excluded" if future_rows else "unknown")
        )
        + ("_truncated" if omitted_rows else ""),
    }


def _snapshot_factor_evidence(payload):
    """Extract raw factors and contribution evidence already produced upstream."""
    payload = payload if isinstance(payload, dict) else {}
    pick = payload.get("pick") if isinstance(payload.get("pick"), dict) else {}
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    entry = (
        decision.get("entry_model")
        if isinstance(decision.get("entry_model"), dict)
        else {}
    )
    raw = dict(pick.get("factor_snapshot") or {})
    for key in (
        "mom5",
        "mom20",
        "mom60",
        "pe",
        "pb",
        "roe",
        "profit_yoy",
        "net_profit",
        "annual_net_profit",
        "report_date",
        "annual_report_date",
        "disclosure_at",
        "financial_source",
    ):
        if key in pick and key not in raw:
            raw[key] = pick.get(key)
    components = dict(pick.get("score_components") or {})
    weights = (
        components.get("weights")
        if isinstance(components.get("weights"), dict)
        else {}
    )
    contributions = {}
    for key, weight in weights.items():
        raw_value = raw.get(key)
        try:
            contributions[key] = (
                round(float(raw_value) * float(weight), 8)
                if raw_value is not None
                else None
            )
        except (TypeError, ValueError):
            contributions[key] = None
    checks = entry.get("checks") if isinstance(entry.get("checks"), list) else []
    entry_contributions = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        try:
            contribution = round(
                float(check.get("score")) * float(check.get("weight")), 8
            )
        except (TypeError, ValueError):
            contribution = None
        entry_contributions.append(
            {
                "name": check.get("name"),
                "raw_score": check.get("score"),
                "weight": check.get("weight"),
                "contribution": contribution,
                "detail": check.get("detail"),
            }
        )
    return {
        "raw": snapshot_safe(raw),
        "contributions": snapshot_safe(contributions),
        "score_components": snapshot_safe(components),
        "entry_checks": entry_contributions,
    }


def build_decision_snapshot(
    payload=None,
    *,
    account_id=None,
    code=None,
    side=None,
    decision=None,
    reason=None,
    asof_date=None,
    quote=None,
    kline=None,
    news=None,
    final_score=None,
    decision_at=None,
    kline_loader=None,
    news_scan_meta=None,
    risk_version=DEFAULT_RISK_VERSION,
    now_fn=None,
):
    """Build one point-in-time evidence envelope without changing trade rules."""
    payload = payload if isinstance(payload, dict) else {}
    pick = payload.get("pick") if isinstance(payload.get("pick"), dict) else {}
    model = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    if not model and isinstance(payload.get("model"), dict):
        model = payload.get("model")
    if not model and isinstance(payload.get("signal"), dict):
        model = payload.get("signal")
    entry = (
        model.get("entry_model")
        if isinstance(model.get("entry_model"), dict)
        else {}
    )
    if not entry and isinstance(payload.get("entry_model"), dict):
        entry = payload.get("entry_model")
    quote_data = dict(quote or payload.get("quote") or {})
    resolved_code = str(code or pick.get("code") or payload.get("code") or "") or None
    resolved_asof = _snapshot_date(
        asof_date
        or payload.get("asof")
        or payload.get("asof_date")
        or payload.get("execution_day")
        or payload.get("signal_date")
        or payload.get("fill_date")
    )
    if kline is None and resolved_code and resolved_asof and callable(kline_loader):
        try:
            kline = kline_loader(resolved_code, resolved_asof, inclusive=True)
        except Exception:
            kline = None
    quote_at = _snapshot_first(quote_data, "quote_at", "time", "timestamp")
    quote_source = _snapshot_first(quote_data, "quote_source", "source") or "unknown"
    history = (
        payload.get("history")
        or payload.get("history_meta")
        or payload.get("factor", {})
    )
    history = history if isinstance(history, dict) else {}
    factor_evidence = _snapshot_factor_evidence(payload)
    financial_source = (
        _snapshot_first(
            pick, "financial_source", "finance_source", "fundamental_source"
        )
        or _snapshot_first(history, "financial_source", "finance_source")
        or "unknown"
    )
    report_period = (
        _snapshot_first(pick, "report_date", "report_period", "financial_period")
        or _snapshot_first(factor_evidence.get("raw"), "report_date", "report_period")
    )
    disclosure_at = (
        _snapshot_first(
            pick,
            "disclosure_at",
            "disclosure_time",
            "announce_at",
            "announcement_at",
        )
        or _snapshot_first(
            history, "disclosure_at", "disclosure_time", "announce_at"
        )
    )
    annual_report_date = _snapshot_first(
        pick, "annual_report_date"
    ) or _snapshot_first(factor_evidence.get("raw"), "annual_report_date")
    news_rows = news if news is not None else payload.get("news")
    news_rows = news_rows if isinstance(news_rows, list) else []
    news_events = []
    announcement_times = []
    for item in news_rows:
        if not isinstance(item, dict):
            continue
        event_time = _snapshot_first(
            item, "time", "quote_at", "published_at", "announcement_at"
        )
        event = snapshot_safe(dict(item))
        event["event_at"] = event_time
        news_events.append(event)
        if item.get("verified") or item.get("source_type") == "announcement_aggregator":
            if event_time:
                announcement_times.append(event_time)
    threshold_context = (
        entry.get("threshold_context")
        if isinstance(entry.get("threshold_context"), dict)
        else {}
    )
    factor_meta = payload.get("factor") if isinstance(payload.get("factor"), dict) else {}
    selection_meta = (
        factor_meta.get("selection_evolution")
        if isinstance(factor_meta.get("selection_evolution"), dict)
        else {}
    )
    news_learning = (
        entry.get("news_learning")
        if isinstance(entry.get("news_learning"), dict)
        else {}
    )
    components = factor_evidence.get("score_components") or {}
    threshold_version = (
        threshold_context.get("version")
        or selection_meta.get("version")
        or factor_meta.get("risk_version")
        or components.get("version")
        or risk_version
    )
    threshold_value = _snapshot_first(entry, "threshold") or _snapshot_first(
        threshold_context, "threshold"
    )
    threshold_delta = _snapshot_first(news_learning, "threshold_delta")
    if threshold_delta is None:
        threshold_delta = _snapshot_first(selection_meta, "entry_score_delta")
    if final_score is None:
        final_score = (
            _snapshot_first(entry, "score")
            or _snapshot_first(model, "final_score", "avg_score")
            or _snapshot_first(components, "final_score")
            or _snapshot_first(pick, "score")
        )
    final_reason = reason or payload.get("reason") or _snapshot_first(entry, "reason")
    if not final_reason:
        reasons = (
            entry.get("reasons") if isinstance(entry.get("reasons"), list) else []
        )
        blockers = (
            entry.get("blockers") if isinstance(entry.get("blockers"), list) else []
        )
        final_reason = (
            "；".join(str(item) for item in (reasons or blockers) if item) or None
        )
    kline_evidence = _snapshot_kline(kline, resolved_asof)
    history_last = _snapshot_first(history, "last_date", "factor_date")
    quote_validation = _snapshot_first(quote_data, "quote_validation") or "unknown"
    scan_meta = (
        news_scan_meta
        if isinstance(news_scan_meta, dict)
        else _DEFAULT_NEWS_SCAN_META
    )
    data_quality = {
        "quote": (
            "ok"
            if (
                quote_at
                and quote_source != "unknown"
                and quote_validation
                in {"cross_source_checked", "range_timestamp_checked"}
            )
            else ("degraded" if quote_at else "unknown")
        ),
        "kline": kline_evidence.get("status") or "unknown",
        "financial": "ok" if report_period else "unknown",
        "news": "ok" if news_rows else ("stale" if scan_meta.get("stale") else "unknown"),
        "history_last_date": history_last,
        "news_scan": snapshot_safe(dict(scan_meta)),
    }
    quality_values = [
        value
        for key, value in data_quality.items()
        if key in {"quote", "kline", "financial", "news"} and isinstance(value, str)
    ]
    data_quality["overall"] = (
        "degraded"
        if any(
            value in {"degraded", "stale", "future_excluded"}
            for value in quality_values
        )
        else (
            "ok"
            if all(value in {"ok", None} for value in quality_values[:4])
            else "unknown"
        )
    )
    clock = now_fn if callable(now_fn) else _now
    return snapshot_safe(
        {
            "version": DECISION_SNAPSHOT_VERSION,
            "decision_at": decision_at or clock(),
            "asof": resolved_asof,
            "account_id": account_id,
            "strategy_id": account_id,
            "code": resolved_code,
            "side": side,
            "quote": {
                "quote_at": quote_at,
                "source": quote_source,
                "validation": quote_validation,
                "cross_check": quote_data.get("quote_cross_check"),
                "price": quote_data.get("price"),
                "pct": quote_data.get("pct"),
            },
            "kline": kline_evidence,
            "financial": {
                "report_period": report_period,
                "report_date": report_period,
                "annual_report_date": annual_report_date,
                "disclosure_at": disclosure_at,
                "source": financial_source,
            },
            "news": {
                "events": news_events,
                "announcement_times": announcement_times,
            },
            "factors": factor_evidence,
            "threshold": {
                "version": threshold_version,
                "value": threshold_value,
                "delta": threshold_delta,
                "dynamic": bool(
                    threshold_delta is not None
                    or threshold_context
                    or selection_meta
                ),
            },
            "data_quality": data_quality,
            "final": {
                "score": final_score,
                "reason": final_reason,
                "decision": decision
                or payload.get("decision_name")
                or "unknown",
            },
        }
    )


def with_decision_snapshot(payload=None, **kwargs):
    """Copy payload and attach a parity decision snapshot without mutating input."""
    enriched = dict(payload or {}) if isinstance(payload, dict) else {}
    if kwargs.get("account_id") and "strategy_id" not in enriched:
        enriched["strategy_id"] = kwargs.get("account_id")
    enriched["decision_snapshot"] = build_decision_snapshot(enriched, **kwargs)
    return enriched
