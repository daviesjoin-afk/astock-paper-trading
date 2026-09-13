# -*- coding: utf-8 -*-
"""Pure point-in-time decision audit helpers for paper trading.

This module serializes evidence only. It deliberately performs no database
writes and no network I/O. Runtime evidence loaders/state are injected by the
facade so the audit boundary remains deterministic and independently testable.
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
    if value in (None, ""):
        return None
    try:
        return _date(value).isoformat()
    except (TypeError, ValueError):
        text = str(value)
        return text[:10] or None


def _snapshot_kline(kline, asof_date):
    if kline is None or not hasattr(kline, "iterrows"):
        return {
            "status": "unknown", "source": None, "count": 0,
            "first_date": None, "last_date": None, "omitted_rows": 0,
            "future_rows": 0, "rows": [],
        }
    cutoff = _snapshot_date(asof_date)
    rows = []
    future_rows = 0
    for index, row in kline.iterrows():
        try:
            bar_date = _date(index).isoformat()
        except (TypeError, ValueError):
            bar_date = str(index)[:10] or None
        if cutoff and bar_date and bar_date > cutoff:
            future_rows += 1
            continue
        item = {"date": bar_date}
        for column in ("open", "high", "low", "close", "volume", "amount"):
            try:
                item[column] = snapshot_safe(row[column]) if column in row else None
            except (KeyError, TypeError, ValueError):
                item[column] = None
        rows.append(item)
    dates = [row["date"] for row in rows if row.get("date")]
    max_stored_rows = 120
    omitted_rows = max(0, len(rows) - max_stored_rows)
    if omitted_rows:
        rows = rows[-max_stored_rows:]
    source = snapshot_safe(getattr(kline, "attrs", {}).get("source") or "unknown")
    return {
        "status": ("ok" if rows else "unknown") + ("_truncated" if omitted_rows else ""),
        "source": source,
        "count": len(rows) + omitted_rows,
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "omitted_rows": omitted_rows,
        "future_rows": future_rows,
        "rows": rows,
    }


def _snapshot_factor_evidence(payload, final_score=None):
    payload = payload if isinstance(payload, dict) else {}
    pick = payload.get("pick") if isinstance(payload.get("pick"), dict) else {}
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    entry = decision.get("entry_model") if isinstance(decision.get("entry_model"), dict) else {}

    raw = dict(pick.get("factor_snapshot") or {})
    for key in (
        "score", "price", "pct", "mom5", "mom20", "main_net", "main_pct",
        "super_net", "vol_ratio", "turnover", "roe", "gross_margin",
        "net_profit", "annual_net_profit", "profit_source", "report_date",
        "annual_report_date", "report_published_at", "annual_report_published_at",
        "industry",
    ):
        if key in pick and key not in raw:
            raw[key] = pick.get(key)

    components = dict(pick.get("score_components") or {})
    weights = components.get("weights") if isinstance(components.get("weights"), dict) else {}
    entry_contributions = {}
    for key, weight in weights.items():
        raw_value = raw.get(key)
        try:
            entry_contributions[str(key)] = (
                round(float(raw_value) * float(weight), 8)
                if raw_value is not None else None
            )
        except (TypeError, ValueError):
            entry_contributions[str(key)] = None

    checks = []
    for check in entry.get("checks") or []:
        if not isinstance(check, dict):
            continue
        item = {key: snapshot_safe(value) for key, value in check.items()}
        weight = item.get("weight")
        score = item.get("score")
        try:
            item["contribution"] = round(float(weight) * float(score), 8)
        except (TypeError, ValueError):
            item["contribution"] = None
        checks.append(item)

    return {
        "raw": snapshot_safe(raw),
        "score_components": snapshot_safe(components),
        "entry_checks": checks,
        "entry_weighted_contributions": snapshot_safe(entry_contributions),
        "selection_score": snapshot_safe(pick.get("score")),
        "final_score": snapshot_safe(
            final_score if final_score is not None else entry.get("score")
        ),
    }


def build_decision_snapshot(
    payload=None, *, account_id=None, code=None, side=None, decision=None,
    reason=None, asof_date=None, quote=None, kline=None, news=None,
    final_score=None, decision_at=None, kline_loader=None,
    news_scan_meta=None, risk_version=DEFAULT_RISK_VERSION, now_fn=None,
):
    """Build one point-in-time evidence envelope without changing trade rules."""
    payload = payload if isinstance(payload, dict) else {}
    quote_data = quote if isinstance(quote, dict) else {}
    pick = payload.get("pick") if isinstance(payload.get("pick"), dict) else {}
    model = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    entry = model.get("entry_model") if isinstance(model.get("entry_model"), dict) else {}
    scan_meta = news_scan_meta if isinstance(news_scan_meta, dict) else _DEFAULT_NEWS_SCAN_META

    resolved_code = str(code or pick.get("code") or quote_data.get("code") or "")
    resolved_asof = _snapshot_date(
        asof_date
        or payload.get("execution_day")
        or payload.get("signal_date")
        or payload.get("fill_date")
    )
    if kline is None and resolved_code and resolved_asof and callable(kline_loader):
        try:
            kline = kline_loader(resolved_code, resolved_asof, inclusive=True)
        except Exception:
            kline = None

    quote_at = (
        quote_data.get("quote_at")
        or quote_data.get("time")
        or quote_data.get("timestamp")
        or payload.get("quote_at")
    )
    quote_source = (
        quote_data.get("source")
        or quote_data.get("quote_source")
        or payload.get("quote_source")
    )
    quote_validation = (
        quote_data.get("quote_validation")
        or payload.get("quote_validation")
        or quote_data.get("status")
    )

    financial_source = (
        pick.get("profit_source")
        or pick.get("financial_source")
        or payload.get("financial_source")
    )
    report_period = (
        pick.get("report_period")
        or pick.get("report_date")
        or payload.get("report_period")
    )
    disclosure_at = (
        pick.get("report_published_at")
        or pick.get("disclosure_at")
        or payload.get("disclosure_at")
    )
    annual_report_period = (
        pick.get("annual_report_period")
        or pick.get("annual_report_date")
        or payload.get("annual_report_period")
    )
    annual_disclosure_at = (
        pick.get("annual_report_published_at")
        or payload.get("annual_report_published_at")
    )

    news_events = []
    announcement_times = [
        value for value in (disclosure_at, annual_disclosure_at) if value
    ]
    for item in news or payload.get("news") or []:
        if not isinstance(item, dict):
            continue
        event_time = (
            item.get("published_at")
            or item.get("actual_pub_time")
            or item.get("publication_time")
            or item.get("time")
            or item.get("date")
        )
        if event_time:
            announcement_times.append(event_time)
        news_events.append({
            "title": snapshot_safe(item.get("title")),
            "source": snapshot_safe(item.get("source")),
            "published_at": snapshot_safe(event_time),
            "verified": snapshot_safe(item.get("verified")),
            "risk": snapshot_safe(
                item.get("risk") if "risk" in item else item.get("negative")
            ),
        })

    threshold_context = entry.get("threshold_context")
    if not isinstance(threshold_context, dict):
        threshold_context = {}
    threshold_version = (
        threshold_context.get("version")
        or entry.get("model_version")
        or model.get("version")
        or payload.get("model_version")
    )
    threshold_value = (
        entry.get("threshold")
        if entry.get("threshold") is not None
        else model.get("threshold")
    )
    factor_evidence = _snapshot_factor_evidence(payload, final_score=final_score)
    if final_score is None:
        final_score = (
            entry.get("score")
            if entry.get("score") is not None
            else model.get("score")
        )
    final_reason = (
        reason
        or payload.get("reason")
        or model.get("reason")
        or "no-explicit-reason"
    )

    kline_snapshot = _snapshot_kline(kline, resolved_asof)
    history_last = kline_snapshot.get("last_date")
    if resolved_asof and history_last and history_last > resolved_asof:
        raise ValueError("decision snapshot includes future K-line evidence")

    data_quality = {
        "quote": "ok" if quote_at and quote_source else "unknown",
        "quote_at": snapshot_safe(quote_at),
        "quote_source": snapshot_safe(quote_source),
        "quote_validation": snapshot_safe(quote_validation),
        "history": kline_snapshot.get("status"),
        "history_last_date": history_last,
        "financial": "ok" if financial_source or disclosure_at else "unknown",
        "financial_source": snapshot_safe(financial_source),
        "report_period": snapshot_safe(report_period),
        "disclosure_at": snapshot_safe(disclosure_at),
        "annual_report_period": snapshot_safe(annual_report_period),
        "annual_disclosure_at": snapshot_safe(annual_disclosure_at),
        "news": "ok" if news_events else "unknown",
        "news_scan_observed_at": snapshot_safe(scan_meta.get("observed_at")),
    }
    quality_values = [
        data_quality["quote"], data_quality["history"],
        data_quality["financial"], data_quality["news"],
    ]
    data_quality["status"] = (
        "ok" if all(value == "ok" for value in quality_values)
        else "degraded" if any(value == "ok" for value in quality_values)
        else "unknown"
    )

    clock = now_fn if callable(now_fn) else _now
    return snapshot_safe({
        "version": DECISION_SNAPSHOT_VERSION,
        "recorded_at": decision_at or clock(),
        "strategy_id": account_id or payload.get("strategy_id"),
        "code": resolved_code or None,
        "side": side or payload.get("side"),
        "decision": decision or payload.get("decision_name") or "unknown",
        "reason": final_reason,
        "asof_date": resolved_asof,
        "quote": {
            "price": quote_data.get("price"),
            "pct": quote_data.get("pct"),
            "quote_at": quote_at,
            "source": quote_source,
            "validation": quote_validation,
        },
        "kline": kline_snapshot,
        "financial": {
            "source": financial_source,
            "report_period": report_period,
            "disclosure_at": disclosure_at,
            "annual_report_period": annual_report_period,
            "annual_disclosure_at": annual_disclosure_at,
        },
        "news": {
            "events": news_events,
            "published_at": announcement_times,
            "scan_observed_at": scan_meta.get("observed_at"),
        },
        "factors": factor_evidence,
        "threshold": {
            "version": threshold_version,
            "value": threshold_value,
            "context": threshold_context,
        },
        "risk_version": risk_version,
        "data_quality": data_quality,
        "final": {
            "score": final_score,
            "reason": final_reason,
            "decision": decision or payload.get("decision_name") or "unknown",
        },
    })


def with_decision_snapshot(payload=None, **kwargs):
    """Copy ``payload`` and attach a decision snapshot without mutating input."""
    enriched = dict(payload or {}) if isinstance(payload, dict) else {}
    if kwargs.get("account_id") and "strategy_id" not in enriched:
        enriched["strategy_id"] = kwargs.get("account_id")
    enriched["decision_snapshot"] = build_decision_snapshot(enriched, **kwargs)
    return enriched
