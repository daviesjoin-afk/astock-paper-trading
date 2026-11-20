#!/usr/bin/env python3
"""Run every LLM task the day's data actually supports.

Six tasks exist. All six are accounted for. The four that once had no data now
have an honest, bounded path to it:

* `flow_reading` uses a vendor size-bucketed flow dataset. If the dataset is
  absent for the symbol, or its rows cannot be dated on/before the report day,
  the task reports `data_unavailable` with the exact reason.
* `news_impact` uses dated news/announcement rows, with source provenance and
  raw-vs-derived labels. No materials means `materials_not_supplied`.
* `factor_hypothesis` needs only a theme, so it runs.
* `evolution_proposal` needs parameters and performance, both from the ledger.

Nothing is invented: an empty `flows` dict never reaches a model, volume is
never substituted for order flow, and a live snapshot is never used as history.

Strategy (explicit, no guessing):

    --public-data auto    default: today's report uses current data; a past
                          report gets no public data (live snapshots would be
                          look-ahead).
    --public-data current only for today's report; refuses a past report.
    --public-data historical
                          only datasets whose rows carry their own trading-date
                          stamp; current snapshots are excluded by name and by
                          envelope metadata.
    --public-data off     no public queries (the old behaviour, for tests).

The pure builder below owns selection, cutoff and provenance. Only `main`
performs I/O through the injected shared `PublicDataClient`; a network error in
one query skips that task alone and warns, never aborts the run.

Adapter contract (main thread owns ``PublicDataClient.dataset``):

    client.datasets()                -> [{"name", "parameters", "source",
                                          "temporal", ...}], no network
    client.dataset(name, **params)   -> {"status": "ok"|"empty"|"error"|...,
                                          "rows": [...], "data": ...,
                                          "source", "temporal", "provenance",
                                          "fetched_at", "availability", ...}

``as_of`` is passed to the adapter only for the historical strategy: the
main-thread adapter treats it as a strict request for a *previously frozen*
snapshot, and effective dates on returned rows never waive that rule. For a
live report the consumer applies its own timestamp cutoff instead. Only the
parameters a capability declares are forwarded (``code``, ``page_size``,
``function``, ``start_day``/``end_day``); unknown keys are never sent, because
the adapter hashes all params into its cache identity.

Cost control is explicit: each task has a cap, the run has a total cap, and the
report says what was spent. Public dataset calls have their own budget.

Usage:
    python scripts/llm_daily.py --api http://127.0.0.1:18765 --day 2026-11-14 \
        --out ../work/reports --max-calls 10
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_sources"))

import llm_runner  # noqa: E402
import llm_tasks  # noqa: E402

# China has no DST; a fixed +08:00 avoids a tzdata dependency on Windows.
SHANGHAI = timezone(timedelta(hours=8))

PUBLIC_DATA_STRATEGIES = ("auto", "current", "historical", "off")
DEFAULT_MAX_SYMBOLS = 2
DEFAULT_PUBLIC_CALLS = 4       # one flow + one news query per selected symbol
PUBLIC_ROW_LIMIT = 12
PUBLIC_WINDOW_DAYS = 7         # bounded look-back for dated materials
TEXT_LIMIT = 500               # consumer-side cap, always flagged as derived

# One dataset name per role per strategy. Aliases exist only for catalog
# resolution; resolution is a single lookup, not a sequence of endpoint calls.
DATASETS = {
    "current": {
        "flow_reading": ("eastmoney_fund_flow_minute", "fund_flow_minute",
                         "stock_fund_flow_120d"),
        "news_impact": ("eastmoney_stock_news", "stock_news",
                        "cninfo_announcements", "announcements"),
    },
    "historical": {
        # Current intraday snapshots are deliberately absent from this table.
        # Both entries are dated series in the real adapter catalog.
        "flow_reading": ("stock_fund_flow_120d", "capital.fund_flow_120d",
                         "fund_flow_120d"),
        "news_impact": ("cninfo_announcements",
                        "fundamentals.cninfo_announcements", "announcements"),
    },
}
ROLES = ("flow_reading", "news_impact")
DEFAULT_PARAMS = {
    "flow_reading": ("code", "function"),
    "news_impact": ("symbol", "code", "page_size", "function",
                    "start_day", "end_day"),
}
# Capability metadata from the adapter catalog that proves the temporal claim.
TEMPORAL_KEYS = ("temporal", "history", "history_kind", "temporal_kind")
PROVENANCE_KEYS = (
    "source", "provider", "dataset", "capability", "function",
    "fetched_at", "observed_at", "first_seen_at", "known_at", "available_at",
    "availability", "source_urls", "source_url", "sha256", "snapshot_sha256",
    "cache_hit", "provider_version", "publication_precision", "timezone",
    "adjust", "unit", "price_unit", "historical_as_of_supported",
    "historical_first_seen_verified", "temporal", "kind", "status", "warnings",
)
# (output field, source keys in priority order). Keys ending in _cents are
# already in cents; the upstream *_net keys are yuan and are converted.
FLOW_BUCKETS = (
    ("super_large_net_inflow_cents",
     ("super_large_net_inflow_cents", "super_net", "super_large_net", "super")),
    ("large_net_inflow_cents",
     ("large_net_inflow_cents", "large_net", "large_net_inflow", "large")),
    ("medium_net_inflow_cents",
     ("medium_net_inflow_cents", "mid_net", "medium_net", "middle_net", "mid")),
    ("retail_net_inflow_cents",
     ("retail_net_inflow_cents", "small_net", "retail_net", "small")),
)
DATE_KEYS = ("date", "day", "announcement_date", "source_date", "trade_day",
             "trading_day")
TIME_KEYS = ("published_at", "time", "showTime", "show_time", "source_datetime",
             "datetime", "observed_at", "quote_time", "publish_time")
ID_KEYS = ("source_id", "announcement_id", "announcementId", "id", "uid")
URL_KEYS = ("source_url", "url", "attachment_url", "detail_url")
TITLE_KEYS = ("title", "announcementTitle", "headline", "name")
BODY_KEYS = ("content", "summary", "description", "brief", "text")

_SYMBOL = re.compile(r"[0-9]{6}\.(SH|SZ|BJ)$")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class SourceResultError(Exception):
    """The dataset answered with an error/invalid envelope, not with rows."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Pure public-data planning and payload builders (no I/O, clock or model here)
# ---------------------------------------------------------------------------

def shanghai_now(now: datetime) -> datetime:
    """Normalize an injected wall-clock reading to +08:00."""
    if now.tzinfo is None:
        return now.replace(tzinfo=SHANGHAI)
    return now.astimezone(SHANGHAI)


def parse_cutoff(value: str) -> datetime:
    """An explicit as-of must be parseable; naive input is read as Beijing time."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"invalid as-of timestamp: {value!r}") from exc
    return shanghai_now(parsed)


def cutoff_for(day: str, strategy: str, *, now: datetime,
               as_of: str | None = None) -> datetime:
    """Never later than the report day, and never later than *now*.

    A date-precision fact only proves it existed on its date, so its stamp is
    the end of that date; a timestamped fact is compared against the full
    instant. For today's report under the historical strategy, the wall clock
    still wins: a report cannot know this evening's news at lunchtime.
    """
    day_end = datetime.combine(date.fromisoformat(day), time(23, 59, 59),
                               tzinfo=SHANGHAI)
    current = shanghai_now(now)
    chosen = parse_cutoff(as_of) if as_of else current
    upper = min(chosen, current)
    if strategy == "historical":
        upper = min(upper, day_end)
    return upper


def resolve_strategy(day: str, requested: str, today: str) -> str:
    """Resolve the default without ever guessing a historical source.

    ``auto`` enables current data for *today's* report only. A past report gets
    no public data unless the caller explicitly asks for historical data by
    trading date; silently feeding it a live snapshot would be look-ahead.
    """
    if requested not in PUBLIC_DATA_STRATEGIES:
        raise ValueError(f"unknown public-data strategy: {requested}")
    if requested == "off":
        return "off"
    if requested == "current":
        return "current" if day == today else "refused_current_for_historical"
    if requested == "historical":
        return "historical"
    return "current" if day == today else "off"


def selected_symbols(report: dict, cap: int) -> list[str]:
    """Only the report's own decisions/holdings, in relevance order, bounded.

    Fills first, then refusals, then held names. Whole-market enumeration and
    unrelated watchlists are never queried. Unknown or non-canonical symbols
    are dropped rather than passed to a source that might widen the query.
    """
    ordered: list[str] = []
    for key in ("trades", "blocked", "blocked_detail", "positions", "holdings",
                "symbols"):
        items = report.get(key) or []
        if isinstance(items, dict):
            items = list(items.keys())
        if not isinstance(items, (list, tuple)):
            continue
        for item in items:
            symbol = (item.get("symbol") or item.get("code")
                      if isinstance(item, dict) else item)
            if isinstance(symbol, str) and _SYMBOL.fullmatch(symbol.strip().upper()):
                symbol = symbol.strip().upper()
                if symbol not in ordered:
                    ordered.append(symbol)
            if len(ordered) >= max(0, cap):
                return ordered[:max(0, cap)]
    return ordered[:max(0, cap)]


def temporal_of(entry: dict) -> str:
    for key in TEMPORAL_KEYS:
        value = _text(entry.get(key)).lower()
        if value:
            return value
    return ""


def catalog_index(entries) -> dict[str, dict]:
    """Normalize ``datasets()`` output to lower-case name -> metadata."""
    index: dict[str, dict] = {}
    if isinstance(entries, dict):
        pairs = list(entries.items())
    elif isinstance(entries, (list, tuple, set, frozenset)):
        pairs = [(entry, entry if isinstance(entry, dict) else {})
                 for entry in entries]
    else:
        pairs = []
    for name, meta in pairs:
        if isinstance(name, dict):
            name = name.get("name") or name.get("dataset") or name.get("id")
        if isinstance(name, str) and name:
            index[name.lower()] = meta if isinstance(meta, dict) else {}
        elif isinstance(meta, dict):
            for key in ("name", "id"):
                alias = meta.get(key)
                if isinstance(alias, str) and alias:
                    index[alias.lower()] = meta
    return index


def resolve_name(catalog: dict[str, dict], aliases: tuple[str, ...]) -> tuple[str, dict]:
    """One catalog lookup. No endpoint is tried speculatively.

    The adapter exposes both capability ids (``capital.fund_flow_120d``) and
    upstream function aliases (``stock_fund_flow_120d``). A function alias is
    used only when it is actually registered; the adapter then receives
    ``function=...`` so it resolves to that exact upstream implementation.
    """
    for alias in aliases:
        entry = catalog.get(alias.lower())
        if entry is not None:
            return alias, entry
    return "", {}                         # no registered capability -> no query


def declared_params(entry: dict) -> tuple[str, ...] | None:
    """The adapter catalog calls its declared arguments ``parameters``."""
    for key in ("parameters", "params", "arguments", "kwargs"):
        value = entry.get(key)
        if isinstance(value, dict):
            return tuple(str(k) for k in value.keys())
        if isinstance(value, (list, tuple)):
            return tuple(str(k) for k in value)
    return None


def params_for(role: str, symbol: str, day: str, cutoff: datetime,
               strategy: str, name: str, entry: dict | None = None) -> dict:
    """Only parameters the capability declares; never widen the query.

    ``as_of`` is forwarded solely for the historical strategy, where the adapter
    requires a previously frozen snapshot. ``limit``/``page_size`` bound the
    response; ``function`` pins a function alias to its exact implementation.
    """
    code = symbol.split(".")[0]
    start = (date.fromisoformat(day) - timedelta(days=PUBLIC_WINDOW_DAYS - 1)).isoformat()
    values = {
        "symbol": symbol,
        "code": code,
        "page_size": PUBLIC_ROW_LIMIT,
        "start_day": start, "end_day": day,
        "day": day, "date": day,
        "as_of": cutoff.isoformat(),
        "function": name,
    }
    declared = declared_params(entry or {})
    allowed = DEFAULT_PARAMS[role] if declared is None else tuple(
        key for key in declared if key in values)
    if not allowed:
        allowed = ("code",)
    if name == "announcements":
        # The project's legacy route has a fixed signature; `code`/`function`
        # are upstream-adapter keys and would be a TypeError here.
        allowed = ("symbol", "start_day", "end_day", "page_size")
    return {key: values[key] for key in dict.fromkeys(allowed)}


def plan_public_data(report: dict, *, requested: str, now: datetime,
                     as_of: str | None = None,
                     max_symbols: int = DEFAULT_MAX_SYMBOLS,
                     max_calls: int = DEFAULT_PUBLIC_CALLS,
                     catalog: dict[str, dict] | None = None) -> dict:
    """Pure plan: strategy, cutoff, bounded symbols and the exact dataset calls."""
    now = shanghai_now(now)
    today = now.date().isoformat()
    day = str(report.get("day") or "")
    if not _ISO_DATE.fullmatch(day):
        raise ValueError("report day must be YYYY-MM-DD")
    strategy = resolve_strategy(day, requested, today)
    cutoff = cutoff_for(day, strategy, now=now, as_of=as_of)
    index = catalog or {}
    historical = strategy == "historical"
    result = {
        "strategy": strategy,
        "day": day,
        "today": today,
        "cutoff": cutoff.isoformat(),
        "symbols": selected_symbols(report, max_symbols),
        "queries": [],
        # The adapter's `as_of` is a client-level keyword: a strict, frozen-only
        # historical read. It is not a capability parameter (those are hashed
        # into the cache identity), so it is carried separately on the plan.
        "client_as_of": cutoff.isoformat() if historical else None,
        "note": "",
    }
    if strategy == "off":
        result["note"] = ("historical report: public data is off by default; "
                          "pass --public-data historical to use dated series")
        return result
    if strategy == "refused_current_for_historical":
        result["note"] = ("current snapshots cannot be used for a past report; "
                          "use --public-data historical (dated rows only)")
        return result
    if strategy == "current" and day != today:
        result["note"] = "current strategy is only valid for today's report"
        return result
    if not result["symbols"]:
        result["note"] = "no canonical symbols in the report; no public query"
        return result
    for symbol in result["symbols"]:
        for role in ROLES:
            if len(result["queries"]) >= max(0, max_calls):
                result["note"] = f"dataset call budget {max_calls} reached"
                return result
            name, entry = resolve_name(index, DATASETS[strategy][role])
            if not name:
                continue                    # capability not registered: no call
            params = params_for(role, symbol, day, cutoff, strategy, name, entry)
            if role == "news_impact" and strategy == "historical":
                # Prefer the project's legacy CNINFO route: it returns dated
                # rows with publication_precision=date and a bounded date
                # window. The upstream alias is used only if absent.
                if "announcements" in index:
                    name = "announcements"
                    params = {
                        "symbol": symbol,
                        "start_day": (date.fromisoformat(day)
                                      - timedelta(days=PUBLIC_WINDOW_DAYS - 1)).isoformat(),
                        "end_day": day,
                        "page_size": PUBLIC_ROW_LIMIT,
                    }
            result["queries"].append({
                "task": role,
                "label": symbol,
                "symbol": symbol,
                "dataset": name,
                "temporal": temporal_of(entry),
                "params": params,
            })
    result["note"] = ("only the report's own symbols are queried; one flow and "
                      "one news dataset per run, both bounded by the cutoff")
    return result


def rows_and_provenance(result) -> tuple[list[dict], dict]:
    """Accept the documented envelopes; refuse an error envelope."""
    if result is None:
        return [], {}
    if isinstance(result, (list, tuple)):
        return [row for row in result if isinstance(row, dict)], {}
    if not isinstance(result, dict):
        raise SourceResultError("dataset_invalid_result", type(result).__name__)
    status = str(result.get("status") or "ok").lower()
    if status not in ("ok", "empty"):
        raise SourceResultError("dataset_" + status,
                                str(result.get("code") or result.get("detail")
                                    or status))
    data = result.get("data")
    legacy = data.get("data") if isinstance(data, dict) else None
    rows = result.get("rows")
    if rows is None and isinstance(data, dict):
        rows = data.get("rows")
    if rows is None and isinstance(legacy, dict):
        rows = legacy.get("rows")
    if rows is None and isinstance(data, (list, tuple)):
        rows = data
    if rows is None:
        rows = []
    if not isinstance(rows, (list, tuple)):
        raise SourceResultError("dataset_invalid_result", "rows is not a list")
    envelope: dict = {}
    nested = result.get("provenance")
    for source in (result, data if isinstance(data, dict) else {},
                   legacy if isinstance(legacy, dict) else {},
                   nested if isinstance(nested, dict) else {}):
        for key in PROVENANCE_KEYS:
            value = source.get(key)
            if value not in (None, "", [], {}) and key not in envelope:
                envelope[key] = value
    # Keep the full provenance object: the adapter carries source_urls,
    # first_seen and temporal evidence inside it.
    if isinstance(nested, dict) and nested:
        envelope["provenance"] = nested
    return [row for row in rows if isinstance(row, dict)], envelope


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else (
        "" if value is None else str(value).strip())


def _first(row: dict, keys, predicate=None) -> str:
    for key in keys:
        value = _text(row.get(key))
        if value and (predicate is None or predicate(value)):
            return value
    return ""


def _parse_stamp(value) -> tuple[datetime | None, str]:
    """Return (instant, precision) with precision in {date, timestamp}."""
    text = _text(value)
    if not text:
        return None, "unknown"
    if _ISO_DATE.fullmatch(text):
        try:
            day = date.fromisoformat(text)
        except ValueError:
            return None, "unknown"
        return datetime.combine(day, time(23, 59, 59), tzinfo=SHANGHAI), "date"
    parsed = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y%m%d%H%M%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None, "unknown"
    return shanghai_now(parsed), "timestamp"


def row_stamp(row: dict) -> tuple[datetime | None, str, date | None]:
    """A date-precision source is never promoted to an exact publish time."""
    declared = _text(row.get("publication_precision")).lower()
    if declared == "date" or (row.get("published_at") is None and
                              row.get("announcement_date")):
        value = _first(row, DATE_KEYS)
        instant, precision = _parse_stamp(value)
        if instant is not None:
            return instant, "date", instant.date()
    for key in TIME_KEYS + DATE_KEYS:
        instant, precision = _parse_stamp(row.get(key))
        if instant is not None:
            return instant, precision, instant.astimezone(SHANGHAI).date()
    return None, "unknown", None


def eligible(row: dict, *, day: str, strategy: str,
             cutoff: datetime) -> tuple[bool, str]:
    """No look-ahead: no row dated after the report day, no future instant."""
    instant, precision, stamp_day = row_stamp(row)
    report_day = date.fromisoformat(day)
    if stamp_day is None:
        # An undated row belongs to the moment it was fetched. It is usable for
        # a live report (with a flag), never as evidence about a past day.
        return (strategy == "current", "no_row_date")
    if stamp_day > report_day:
        return False, "after_report_day"
    if precision == "date":
        return True, "date_precision"
    if instant is not None and instant > cutoff:
        return False, "after_cutoff"
    return True, "timestamp"


def snapshot_only(envelope: dict, strategy: str) -> bool:
    """Refuse an envelope that only promises a live/current snapshot.

    The adapter exposes ``temporal`` both at the top level and inside its
    ``provenance`` object. Values naming the *current* moment are unusable as
    historical evidence; a dated series is usable only with row-level checks.
    The adapter also states ``historical_first_seen_verified=False`` by default,
    which we record rather than silently upgrade to verified.
    """
    if strategy != "historical":
        return False
    if envelope.get("historical_as_of_supported") is False:
        return True
    nested = envelope.get("provenance") or {}
    words = []
    for source in (envelope, nested if isinstance(nested, dict) else {}):
        words.extend(_text(source.get(key)).lower() for key in TEMPORAL_KEYS
                     + ("availability", "kind"))
    return any(any(token in word for token in
                   ("current", "snapshot", "now", "live", "realtime", "today"))
               for word in words)


def _provenance(name: str, envelope: dict, row: dict,
                cutoff: datetime, strategy: str) -> dict:
    nested = envelope.get("provenance") or {}
    if not isinstance(nested, dict):
        nested = {}
    provenance = {
        "dataset": name,
        "capability": _text(envelope.get("capability")) or name,
        "source": (_text(envelope.get("source")) or _text(nested.get("source"))
                   or _text(row.get("source")) or _text(row.get("provider"))
                   or _text(envelope.get("provider")) or name),
        "source_id": _first(row, ID_KEYS) or None,
        "source_url": (_first(row, URL_KEYS) or _first(envelope, ("source_url",))
                       or _first(nested, ("source_url",)) or None),
        "fetched_at": (_text(row.get("fetched_at")) or _text(envelope.get("fetched_at"))
                       or _text(nested.get("fetched_at")) or None),
        "observed_at": (_text(row.get("observed_at"))
                        or _text(envelope.get("observed_at")) or None),
        "known_at": (_text(row.get("known_at")) or _text(row.get("first_seen_at"))
                     or _text(nested.get("known_at"))
                     or _text(nested.get("first_seen_at")) or None),
        "availability": _text(envelope.get("availability")) or None,
        "temporal": (_text(envelope.get("temporal")) or _text(nested.get("temporal"))
                     or None),
        "historical_first_seen_verified": envelope.get(
            "historical_first_seen_verified", nested.get("historical_first_seen_verified")),
        "publication_precision": (_text(row.get("publication_precision"))
                                  or row_stamp(row)[1]),
        "cutoff": cutoff.isoformat(),
        "strategy": strategy,
    }
    urls = (row.get("source_urls") or envelope.get("source_urls")
            or nested.get("source_urls"))
    if urls:
        provenance["source_urls"] = urls
    if envelope.get("snapshot_sha256"):
        provenance["snapshot_sha256"] = envelope["snapshot_sha256"]
    if strategy == "historical" and provenance["fetched_at"]:
        provenance["fetch_note"] = (
            "this series may have been fetched after the report day; only rows "
            "whose own stamp is on/before the report day are used. A date-level "
            "stamp does not prove intraday availability.")
    return provenance


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    text = _text(value)
    if text in ("", "-", "null", "None"):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _cents(row: dict, aliases, envelope: dict) -> tuple[int | None, dict]:
    """Return integer cents plus the untouched raw field/value/unit."""
    unit = _text(row.get("unit") or envelope.get("unit")).lower()
    already_cents = unit in ("cent", "cents", "分")
    for key in aliases:
        if key not in row:
            continue
        number = _number(row.get(key))
        if number is None:
            return None, {"key": key, "value": row.get(key), "unit": unit or "yuan"}
        is_cents = already_cents or key.endswith("_cents")
        raw = {"key": key, "value": row.get(key),
               "unit": "cent" if is_cents else "yuan"}
        return int(round(number if is_cents else number * 100)), raw
    return None, {}


def _skip(code: str, detail: str, **extra) -> dict:
    return {"skip": dict(code=code, detail=detail, **extra)}


def build_flow_payload(symbol: str, name: str, result, *, day: str,
                       strategy: str, cutoff: datetime) -> dict:
    """Order-flow payload, or a skip. Missing buckets are never fabricated."""
    try:
        rows, envelope = rows_and_provenance(result)
    except SourceResultError as error:
        return _skip(error.code, error.detail)
    if snapshot_only(envelope, strategy):
        return _skip("snapshot_not_historical",
                     "a current snapshot cannot be used as historical flow data")
    kept, excluded = [], {"after_report_day": 0, "after_cutoff": 0,
                          "no_row_date": 0, "missing_buckets": 0}
    for row in rows:
        allowed, why = eligible(row, day=day, strategy=strategy, cutoff=cutoff)
        if not allowed:
            excluded[why] = excluded.get(why, 0) + 1
            continue
        buckets, raws = {}, {}
        for field, aliases in FLOW_BUCKETS:
            value, raw = _cents(row, aliases, envelope)
            buckets[field] = value
            raws[field] = raw
        if any(value is None for value in buckets.values()):
            excluded["missing_buckets"] += 1
            continue
        instant, precision, stamp_day = row_stamp(row)
        kept.append({
            "symbol": _text(row.get("symbol")) or symbol,
            "date": stamp_day.isoformat() if stamp_day else None,
            "stamp": instant.isoformat() if instant else None,
            "publication_precision": precision,
            "cutoff_unverified": precision == "unknown",
            "unit": "cent",
            **buckets,
            "main_net_inflow_cents": _cents(
                row, ("main_net_inflow_cents", "main_net"), envelope)[0],
            "raw": raws,
            "semantics": {
                "kind": "derived",
                "raw_kind": "vendor_size_bucket_net_inflow",
                "is_raw_level2_tape": False,
                "derivation": ("vendor bucketed net inflow from a trade tape; "
                               "yuan x 100 -> integer cents, no other derivation"),
                "substituted_from_volume_or_price": False,
            },
            "provenance": _provenance(name, envelope, row, cutoff, strategy),
        })
    if not kept:
        code = "snapshot_not_historical" if (
            strategy == "historical" and excluded.get("no_row_date")) else "no_rows"
        return _skip(code, "no eligible dated flow rows with all four buckets",
                     excluded=excluded)
    kept.sort(key=lambda item: item["stamp"] or item["date"] or "", reverse=True)
    kept = kept[:PUBLIC_ROW_LIMIT]
    latest = kept[0]
    source = _provenance(name, envelope, {}, cutoff, strategy)
    flows = {
        "symbol": symbol,
        "day": day,
        "as_of": cutoff.isoformat(),
        "cutoff": cutoff.isoformat(),
        "unit": "cent",
        "symbols": sorted({item["symbol"] for item in kept}),
        "rows": kept,
        "row_count": len(kept),
        "excluded": excluded,
        "source": source,
        "semantics": {
            "kind": "derived",
            "raw_kind": "vendor_size_bucket_net_inflow",
            "is_raw_level2_tape": False,
            "derivation": ("upstream reports yuan; the consumer converts to "
                           "integer cents. Buckets are vendor-derived estimates, "
                           "not raw Level-2 prints and not volume."),
            "cutoff": cutoff.isoformat(),
            "strategy": strategy,
        },
        **{field: latest[field] for field, _ in FLOW_BUCKETS},
    }
    return {
        "payload": {"symbol": symbol, "flows": flows},
        "provenance": source,
        "meta": {"rows": len(kept), "excluded": excluded, "dataset": name},
    }


def build_news_payload(symbol: str, name: str, result, *, day: str,
                       strategy: str, cutoff: datetime,
                       display_name: str | None = None) -> dict:
    """News/announcement materials with source provenance, or a skip."""
    try:
        rows, envelope = rows_and_provenance(result)
    except SourceResultError as error:
        return _skip(error.code, error.detail)
    if snapshot_only(envelope, strategy):
        return _skip("snapshot_not_historical",
                     "a current news snapshot cannot be used as historical news")
    materials, excluded = [], {"after_report_day": 0, "after_cutoff": 0,
                               "no_row_date": 0, "no_text": 0}
    for row in rows:
        allowed, why = eligible(row, day=day, strategy=strategy, cutoff=cutoff)
        if not allowed:
            excluded[why] = excluded.get(why, 0) + 1
            continue
        title = _first(row, TITLE_KEYS)
        body = _first(row, BODY_KEYS)
        if not title and not body:
            excluded["no_text"] += 1
            continue
        text = body or title
        truncated = len(text) > TEXT_LIMIT
        if truncated:
            text = text[:TEXT_LIMIT]
        # An upstream summary/content is a derived excerpt (often truncated to
        # 200 chars by the vendor). A bare official title is raw source text.
        derived = bool(body) and body != title
        instant, precision, stamp_day = row_stamp(row)
        materials.append({
            "ref": f"m{len(materials) + 1}",
            "symbol": _text(row.get("symbol")) or symbol,
            "id": _first(row, ID_KEYS) or None,
            "title": title[:TEXT_LIMIT] or None,
            "text": text,
            "date": stamp_day.isoformat() if stamp_day else None,
            "published_at": (None if precision == "date" else
                             _text(row.get("published_at")) or None),
            "publication_precision": (_text(row.get("publication_precision"))
                                      or precision),
            "category": _first(row, ("type", "category_raw", "category",
                                     "announcementType")) or None,
            "source": (_text(row.get("source")) or _text(row.get("mediaName"))
                       or _text(envelope.get("source")) or name),
            "source_url": _first(row, URL_KEYS) or None,
            "cutoff_unverified": precision == "unknown",
            "semantics": {
                "kind": "derived_excerpt" if derived else "raw_source_text",
                "derivation": ("vendor-supplied excerpt/summary; upstream may "
                               "truncate to 200 chars; full article not fetched"
                               if derived else
                               "official title as published; PDF/body not fetched"),
                "raw_fields": sorted(key for key in row if key in TITLE_KEYS
                                     or key in BODY_KEYS),
                "truncated_by_consumer": truncated,
                "text_limit": TEXT_LIMIT,
            },
            "provenance": _provenance(name, envelope, row, cutoff, strategy),
        })
        if len(materials) >= PUBLIC_ROW_LIMIT:
            break
    if not materials:
        code = "snapshot_not_historical" if (
            strategy == "historical" and excluded.get("no_row_date")) else "no_rows"
        return _skip(code, "no eligible dated news/announcement materials",
                     excluded=excluded)
    source = _provenance(name, envelope, {}, cutoff, strategy)
    payload = {
        "symbol": symbol,
        "name": display_name or symbol,
        "materials": materials,
    }
    return {
        "payload": payload,
        "provenance": source,
        "meta": {"rows": len(materials), "excluded": excluded, "dataset": name},
    }


def get(api: str, path: str, timeout: float = 30.0):
    with urllib.request.urlopen(api.rstrip("/") + path, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def signal_payloads(report: dict, cap: int) -> list[dict]:
    """At most `cap` decision payloads, fills first then refusals."""
    return [payload for _, payload in zip(range(cap), _signal_payloads(report))]


def _signal_payloads(report: dict):
    """One payload per fill, plus one per refusal — both are decisions."""
    for trade in report.get("trades") or []:
        yield {
            "symbol": trade.get("symbol"),
            "signal": {
                "side": trade.get("side"),
                "quantity": trade.get("quantity"),
                "price_cents": trade.get("price_cents"),
                "reason": trade.get("reason"),
            },
            "basis": trade.get("basis") or {},
            "context": {"day": report.get("day")},
        }
    for blocked in report.get("blocked") or []:
        yield {
            "symbol": blocked.get("symbol"),
            "signal": {
                "side": blocked.get("side"),
                "quantity": blocked.get("quantity"),
                "reason": blocked.get("reason"),
            },
            "basis": {"blocked": True, "blocked_day": blocked.get("blocked_day")},
            "context": {"day": report.get("day")},
        }


def pnl_payload(report: dict) -> dict | None:
    activity = report.get("activity") or {}
    if not activity:
        return None
    return {
        "period": report.get("day"),
        "total_bps": activity.get("total_bps") or activity.get("return_bps") or 0,
        "activity": activity,
    }


def evolution_payload(report: dict, params: dict) -> dict | None:
    activity = report.get("activity") or {}
    if not params:
        return None
    return {
        "parameters": params,
        "performance": {
            "day": report.get("day"),
            "activity": activity,
            "fills": len(report.get("trades") or []),
            "blocked": len(report.get("blocked") or []),
        },
    }


class _NoPublicData:
    """Inert client for `--public-data off`: no dataset method is ever called."""

    def datasets(self):
        return []


def default_client():
    """Build the shared client only when a strategy will actually query it.

    Main thread owns the adapter query behind ``PublicDataClient.dataset``; the
    consumer only depends on that name and the injected ``client`` argument of
    ``main``. Construction is lazy so a missing/optional adapter dependency is
    an isolated skip of the affected task, not a failure of the whole run.

    A cache directory is opt-in (``ASTOCK_PUBLIC_DATA_CACHE_DIR`` or
    ``--public-data-cache-dir``). It is what makes the strict historical
    ``as_of`` read possible: the adapter only answers an ``as_of`` query from a
    snapshot frozen on or before that instant, and never fetches today's data
    to fill a past hole. No cache means historical queries skip honestly.
    """
    try:
        from public_data import PublicDataClient
    except ImportError as error:                 # optional in offline installs
        raise SourceResultError("public_data_client_unavailable", str(error)) from error
    cache_dir = os.environ.get("ASTOCK_PUBLIC_DATA_CACHE_DIR")
    if cache_dir:
        try:
            from source_http import SourceHttp
        except ImportError as error:
            raise SourceResultError("public_data_transport_unavailable",
                                    str(error)) from error
        return PublicDataClient(SourceHttp(Path(cache_dir)))
    return PublicDataClient()


def catalog_of(client) -> dict[str, dict]:
    """Introspect dataset names once; no network by contract.

    A catalog that cannot be read is an empty catalog, not a crashed run: the
    result is that no capability is resolvable, so both public-data tasks skip
    with a reason instead of the whole daily review failing.
    """
    getter = getattr(client, "datasets", None)
    if getter is None:
        return {}
    try:
        return catalog_index(getter())
    except Exception as error:                 # optional adapter introspection
        print(f"warning: public data catalog unavailable: "
              f"{type(error).__name__}: {error}", file=sys.stderr)
        return {}


def display_names(report: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    for key in ("trades", "blocked", "blocked_detail", "positions", "holdings"):
        for item in report.get(key) or []:
            if not isinstance(item, dict):
                continue
            symbol = _text(item.get("symbol")).upper()
            label = _text(item.get("name")) or _text(item.get("company_name"))
            if symbol and label:
                names.setdefault(symbol, label)
    return names


def unavailable_record(task: str, label: str, reason: str, detail: str,
                       *, public: dict | None = None) -> dict:
    """Preserve the original skip fields; public evidence is additive."""
    task_spec = llm_tasks.TASKS[task]
    record = {
        "task": task,
        "label": label,
        "status": "skipped",
        "reason": reason,
        "detail": detail,
    }
    if task == "flow_reading":
        record["missing"] = list(task_spec.required_fields)
    if public is not None:
        record["public_data"] = public
    return record


def public_data_tasks(report: dict, *, now: datetime, client,
                      requested: str = "auto", as_of: str | None = None,
                      max_symbols: int = DEFAULT_MAX_SYMBOLS,
                      max_public_calls: int = DEFAULT_PUBLIC_CALLS) -> tuple[list[dict], dict]:
    """Query only the selected symbols; return (records, audit).

    A network/transport failure skips only the affected task and warns. The
    audit carries the plan, cutoff, exact calls and per-call outcomes so the
    report can prove what was and was not used.
    """
    labels = {"flow_reading": "资金流向", "news_impact": "新闻影响"}
    outcomes: dict[str, list[dict]] = {role: [] for role in ROLES}
    errors: dict[str, list[dict]] = {role: [] for role in ROLES}
    audit: dict = {"requested": requested, "strategy": "off", "cutoff": None,
                   "client_as_of": None, "symbols": [], "calls": [],
                   "note": "", "records": []}
    try:
        plan_data = plan_public_data(
            report, requested=requested, now=now, as_of=as_of,
            max_symbols=max_symbols, max_calls=max_public_calls,
            catalog=catalog_of(client) if requested != "off" else {},
        )
    except (SourceResultError, ValueError, OSError) as error:
        audit["note"] = f"public data planning failed: {error}"
        print(f"warning: {audit['note']}", file=sys.stderr)
        return [], audit

    strategy = plan_data["strategy"]
    audit.update(strategy=strategy, cutoff=plan_data["cutoff"],
                 client_as_of=plan_data.get("client_as_of"),
                 symbols=plan_data["symbols"], note=plan_data["note"])
    cutoff = parse_cutoff(plan_data["cutoff"])
    day = plan_data["day"]
    names = display_names(report)

    if strategy not in ("current", "historical") or not plan_data["queries"]:
        flow_task = llm_tasks.TASKS["flow_reading"]
        records = [
            unavailable_record(
                "flow_reading", labels["flow_reading"], "data_unavailable",
                (plan_data["note"] or "public data not enabled; "
                 "no size-bucketed flow rows were supplied"),
                public={"strategy": strategy, "cutoff": plan_data["cutoff"]}),
            unavailable_record(
                "news_impact", labels["news_impact"], "materials_not_supplied",
                (plan_data["note"] or "没有取得公告/新闻材料；"
                 "空 materials 会让模型凭空推断。"),
                public={"strategy": strategy, "cutoff": plan_data["cutoff"]}),
        ]
        # The original "no data" detail remains available for audit trails.
        records[0]["unavailable_reason"] = flow_task.unavailable_reason
        audit["records"] = records
        return records, audit

    for query in plan_data["queries"]:
        role, symbol, dataset = query["task"], query["symbol"], query["dataset"]
        call_audit = {"dataset": dataset, "symbol": symbol, "task": role,
                      "params": query["params"],
                      "temporal": query.get("temporal") or ""}
        audit["calls"].append(call_audit)
        try:
            if plan_data.get("client_as_of"):
                result = client.dataset(dataset, as_of=plan_data["client_as_of"],
                                        **query["params"])
            else:
                result = client.dataset(dataset, **query["params"])
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            call_audit["outcome"] = "network_error"
            call_audit["detail"] = f"{type(error).__name__}: {error}"[:300]
            print(f"warning: public data {dataset} for {symbol} failed: "
                  f"{call_audit['detail']}; skipping {role}", file=sys.stderr)
            errors[role].append(unavailable_record(
                role, f"{labels[role]} {symbol}",
                "data_unavailable" if role == "flow_reading" else "materials_not_supplied",
                f"public data source unreachable: {call_audit['detail']}",
                public={"strategy": strategy, "dataset": dataset,
                        "cutoff": plan_data["cutoff"], "error": "network_error"},
            ))
            continue
        except Exception as error:                 # adapter/query error is local
            code = str(getattr(error, "code", "") or "")
            call_audit["outcome"] = ("historical_snapshot_unavailable" if code ==
                                     "historical_snapshot_unavailable" else
                                     "network_error" if code in
                                     ("offline_cache_miss", "cache_expired",
                                      "upstream_empty") else "dataset_error")
            call_audit["detail"] = f"{type(error).__name__}: {error}"[:300]
            print(f"warning: public data {dataset} for {symbol} failed: "
                  f"{call_audit['detail']}; skipping {role}", file=sys.stderr)
            errors[role].append(unavailable_record(
                role, f"{labels[role]} {symbol}",
                "data_unavailable" if role == "flow_reading" else "materials_not_supplied",
                (f"no frozen public snapshot exists for {day}; strict historical "
                 f"read did not fetch present-day data ({code})"
                 if code == "historical_snapshot_unavailable" else
                 f"public data query failed ({code or 'unknown'}): {call_audit['detail']}"),
                public={"strategy": strategy, "dataset": dataset,
                        "cutoff": plan_data["cutoff"], "code": code or None,
                        "error": call_audit["outcome"]},
            ))
            continue
        if role == "flow_reading":
            built = build_flow_payload(symbol, dataset, result, day=day,
                                       strategy=strategy, cutoff=cutoff)
        else:
            built = build_news_payload(
                symbol, dataset, result, day=day, strategy=strategy,
                cutoff=cutoff, display_name=names.get(symbol))
        if "skip" in built:
            reason = built["skip"]["code"]
            call_audit["outcome"] = "no_eligible_rows"
        else:
            reason = "ok"
            call_audit["outcome"] = "ok"
            call_audit["rows"] = built["meta"]["rows"]
        call_audit["detail"] = built.get("skip", {}).get("detail", "")
        outcomes[role].append(built)
        outcomes[role][-1]["query"] = {"dataset": dataset, "symbol": symbol,
                                       "params": query["params"]}
        outcomes[role][-1]["outcome"] = reason

    records: list[dict] = []
    for role in ROLES:
        accepted = [item for item in outcomes[role] if "payload" in item]
        skipped = [item for item in outcomes[role] if "skip" in item]
        if errors[role]:
            # A failed endpoint skips only its own task; a healthy endpoint for
            # the same task still runs, and the failure is recorded alongside.
            skipped.extend(errors[role])
        if accepted:
            # One model call per task: the selected symbols' materials are
            # merged into one bounded payload, with per-source provenance kept.
            merged = merge_task_payloads(role, accepted, day=day,
                                         cutoff=plan_data["cutoff"],
                                         strategy=strategy)
            call = {"task": role, "label": labels[role], "payload": merged}
            records.append(call)
        elif skipped:
            first = skipped[0]
            skip_info = first.get("skip") or {
                "detail": first.get("detail", "public data unavailable"),
                "code": (first.get("public_data") or {}).get("error", first.get("reason", "dataset_error")),
                "excluded": {},
            }
            records.append(unavailable_record(
                role, labels[role],
                "data_unavailable" if role == "flow_reading" else "materials_not_supplied",
                skip_info["detail"],
                public={"strategy": strategy, "cutoff": plan_data["cutoff"],
                        "code": skip_info["code"],
                        "excluded": skip_info.get("excluded", {}),
                        "queries": [item.get("query") for item in skipped
                                    if "query" in item],
                        "skipped_symbols": [item.get("label") for item in skipped
                                            if item.get("label")]},
            ))
        else:
            records.append(unavailable_record(
                role, labels[role],
                "data_unavailable" if role == "flow_reading" else "materials_not_supplied",
                "no public data query produced eligible rows",
                public={"strategy": strategy, "cutoff": plan_data["cutoff"]},
            ))
    audit["records"] = records
    return records, audit


def merge_task_payloads(role: str, accepted: list[dict], *, day: str,
                        cutoff: str, strategy: str) -> dict:
    """Merge per-symbol builds into one bounded payload for one model call."""
    if role == "flow_reading":
        rows, sources, meta = [], [], []
        for item in accepted:
            flows = item["payload"]["flows"]
            rows.extend(flows["rows"])
            sources.append(item["provenance"])
            meta.append(item["meta"])
        rows.sort(key=lambda row: row["stamp"] or row["date"] or "", reverse=True)
        rows = rows[:PUBLIC_ROW_LIMIT]
        latest = rows[0]
        flows = {
            "day": day,
            "as_of": cutoff,
            "cutoff": cutoff,
            "unit": "cent",
            "symbols": sorted({row["symbol"] for row in rows}),
            "rows": rows,
            "row_count": len(rows),
            "excluded": dict((key, sum(item["meta"].get("excluded", {}).get(key, 0)
                                       for item in accepted))
                             for key in ("after_report_day", "after_cutoff",
                                         "no_row_date", "missing_buckets")),
            "sources": sources,
            "query_meta": meta,
            "semantics": {
                "kind": "derived",
                "raw_kind": "vendor_size_bucket_net_inflow",
                "is_raw_level2_tape": False,
                "derivation": ("vendor bucketed net inflow from a trade tape; "
                               "yuan x 100 -> integer cents; never volume/price"),
                "cutoff": cutoff,
                "strategy": strategy,
            },
            **{field: latest[field] for field, _ in FLOW_BUCKETS},
        }
        return {"symbol": ", ".join(flows["symbols"]), "flows": flows}
    materials, sources, meta = [], [], []
    names: dict[str, str] = {}
    for item in accepted:
        payload = item["payload"]
        for material in payload["materials"]:
            material["ref"] = f"m{len(materials) + 1}"
            materials.append(material)
        names[payload["symbol"]] = payload["name"]
        sources.append(item["provenance"])
        meta.append(item["meta"])
    materials = materials[:PUBLIC_ROW_LIMIT]
    return {
        "symbol": ", ".join(sorted(names)),
        "name": " / ".join(f"{symbol} {name}" for symbol, name in sorted(names.items())),
        "materials": materials,
        "sources": sources,
        "query_meta": meta,
        "cutoff": cutoff,
        "strategy": strategy,
    }


def main(argv=None, *, client=None, now: datetime | None = None) -> int:
    """`client` and `now` are injectable; both default to the real ones."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=os.environ.get("ASTOCK_API", "http://127.0.0.1:18765"))
    parser.add_argument("--day", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--base-url", default=os.environ.get("ASTOCK_LLM_BASE", ""))
    parser.add_argument("--model", default=os.environ.get("ASTOCK_LLM_MODEL", "deepseek-v4.1-flash"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-calls", type=int, default=10, help="total model calls this run")
    parser.add_argument("--signals-per-run", type=int, default=3)
    parser.add_argument("--public-data", choices=PUBLIC_DATA_STRATEGIES,
                        default=os.environ.get("ASTOCK_PUBLIC_DATA", "auto"),
                        help="auto (default): current data for today, none for past reports; "
                             "current: live report only; historical: dated rows by trading day; "
                             "off: no public queries")
    parser.add_argument("--public-data-as-of", default=os.environ.get("ASTOCK_PUBLIC_DATA_AS_OF"),
                        help="explicit ISO cutoff; never later than now or the report day")
    parser.add_argument("--public-data-symbols", type=int, default=DEFAULT_MAX_SYMBOLS,
                        help="maximum report symbols queried")
    parser.add_argument("--public-data-calls", type=int, default=DEFAULT_PUBLIC_CALLS,
                        help="maximum dataset queries this run")
    parser.add_argument("--public-data-cache-dir", type=Path,
                        default=(Path(os.environ["ASTOCK_PUBLIC_DATA_CACHE_DIR"])
                                 if os.environ.get("ASTOCK_PUBLIC_DATA_CACHE_DIR") else None),
                        help="opt-in shared snapshot cache; required for strict historical as_of")
    args = parser.parse_args(argv)

    api_key = os.environ.get("ASTOCK_LLM_KEY")
    if not args.base_url or not api_key:
        print(json.dumps({"status": "skipped", "reason": "no_llm_configuration"}))
        return 0

    try:
        report = get(args.api, f"/v2/account/daily-report?day={args.day}")
    except (urllib.error.URLError, OSError) as error:
        print(json.dumps({"status": "failed", "code": "report_unavailable", "detail": str(error)}))
        return 0

    results: list[dict] = []
    spent = 0

    def call(task: str, payload: dict, label: str) -> None:
        nonlocal spent
        if spent >= args.max_calls:
            results.append({"task": task, "label": label, "status": "skipped_budget"})
            return
        spent += 1
        record = dict(llm_runner.run(
            task, payload, base_url=args.base_url, api_key=api_key,
            model=args.model, timeout=args.timeout,
        ))
        # Copied, not mutated in place: the runner may hand back a shared dict,
        # and stamping the task onto it would relabel every earlier record.
        record["task"] = task
        record["label"] = label
        results.append(record)

    # 1. Signals, including refusals — a blocked order is a decision too.
    for index, payload in enumerate(signal_payloads(report, args.signals_per_run)):
        call("signal_review", payload,
             f"{payload['symbol']} {payload['signal'].get('side')} #{index}")

    # 2. P&L attribution.
    pnl = pnl_payload(report)
    if pnl is None:
        results.append({"task": "pnl_review", "label": "当日归因",
                        "status": "skipped", "reason": "no_activity"})
    else:
        call("pnl_review", pnl, "当日归因")

    # 3. Evolution proposal, from real parameters.
    params = report.get("parameters") or (report.get("policy") or {})
    evolution = evolution_payload(report, params)
    if evolution is None:
        results.append({"task": "evolution_proposal", "label": "参数进化",
                        "status": "skipped", "reason": "no_parameters"})
    else:
        call("evolution_proposal", evolution, "参数进化")

    # 4 & 5. Order flow and news/announcement impact. By default today's report
    # actually queries the shared public client (only for its own symbols); a
    # past report uses no live snapshots unless `historical` is requested.
    public_audit: dict = {"strategy": "off", "calls": []}
    try:
        shared = client
        if shared is None and args.public_data != "off":
            if args.public_data_cache_dir is not None:
                try:
                    from public_data import PublicDataClient
                    from source_http import SourceHttp
                except ImportError as error:
                    raise SourceResultError("public_data_client_unavailable",
                                            str(error)) from error
                shared = PublicDataClient(SourceHttp(args.public_data_cache_dir))
            else:
                shared = default_client()
        if shared is not None:
            public_records, public_audit = public_data_tasks(
                report, now=now or datetime.now(SHANGHAI), client=shared,
                requested=args.public_data, as_of=args.public_data_as_of,
                max_symbols=args.public_data_symbols,
                max_public_calls=args.public_data_calls,
            )
        else:
            public_records, public_audit = public_data_tasks(
                report, now=now or datetime.now(SHANGHAI),
                client=_NoPublicData(), requested="off",
            )
    except (SourceResultError, OSError, ValueError) as error:
        # A missing/incomplete shared client is an isolated skip, never a
        # failure of the deterministic report or the other LLM tasks.
        print(f"warning: public data client unavailable: {error}; "
              "skipping flow/news tasks", file=sys.stderr)
        public_audit = {"strategy": "unavailable", "error": str(error), "calls": []}
        public_records = [
            unavailable_record("flow_reading", "资金流向", "data_unavailable",
                               f"public data client unavailable: {error}",
                               public={"strategy": "unavailable"}),
            unavailable_record("news_impact", "新闻影响", "materials_not_supplied",
                               f"public data client unavailable: {error}",
                               public={"strategy": "unavailable"}),
        ]
    for item in public_records:
        if "payload" in item:
            call(item["task"], item["payload"], item["label"])
        else:
            results.append(item)
    public_audit["records"] = [item for item in results
                               if item.get("task") in ROLES]

    # 6. Factor hypothesis — only a theme is required, so this one can run.
    call("factor_hypothesis", {"theme": os.environ.get("ASTOCK_LLM_THEME", "低波动与动量的组合")},
         "因子假设")

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{args.day}.llm.json"
    summary = {
        "day": args.day,
        "model": args.model,
        "calls": spent,
        "budget": args.max_calls,
        "ran": sum(1 for item in results if item.get("status") == "ok"),
        "skipped": [item for item in results if str(item.get("status", "")).startswith("skipped")],
        "records": results,
        "public_data": public_audit,
        "note": ("A skipped task is reported with the field that was missing, "
                 "never with a placeholder input. Public flow/news rows carry "
                 "source, cutoff, raw-vs-derived semantics and are bounded to "
                 "the report's own symbols."),
    }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": "ok", "day": args.day, "calls": spent,
        "ran": summary["ran"], "skipped": len(summary["skipped"]),
        "public_data": public_audit.get("strategy"),
        "public_calls": len(public_audit.get("calls") or []),
        "report": str(path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
