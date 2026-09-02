# -*- coding: utf-8 -*-
"""Point-in-time disclosure timestamps for financial factor evidence.

The existing financial endpoint exposes a report period, but not the time at
which the report became visible.  This module fills that missing metadata from
the public Eastmoney notice feed.  It deliberately does *not* provide profit
values or change any strategy gate: callers can use the returned timestamp to
decide whether an already loaded financial row was visible at ``asof``.

The feed is an aggregation endpoint rather than a signed exchange API.  The
source is therefore labelled explicitly, cached atomically, rate limited and
treated as unknown when a timestamp cannot be parsed.  A failed refresh may
use a previously fetched row, but that row is marked stale and never gets a
new publication time.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


EASTMONEY_NOTICE_URL = (
    "https://np-anotice-stock.eastmoney.com/api/security/ann"
)
SOURCE_NAME = "eastmoney-public-notice"
SOURCE_KIND = "public_aggregator"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_CACHE_TTL_SECONDS = 6 * 60 * 60
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 3
MIN_REQUEST_INTERVAL_SECONDS = 0.35

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(_BASE_DIR, "data_cache", "disclosure_timeline.json")
_CACHE_LOCK = threading.RLock()
_RATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0

# Keep the matching contract ASCII-only in source so deployments with a
# non-UTF-8 shell cannot corrupt Chinese title literals.  These escaped values
# are equivalent to the Chinese labels used by the public notice feed.
_REPORT_TITLE_RE = re.compile(
    r"(?P<year>20\d{2})\s*\u5e74\s*"
    r"(?:(?:\u7b2c)?(?P<quarter>[\u4e00\u4e8c\u4e09\u56db1234])\s*\u5b63\u5ea6|"
    r"(?P<half>\u534a\u5e74\u5ea6|\u534a\u5e74\u5ea6\u62a5\u544a|\u4e2d\u671f)|"
    r"(?P<annual>\u5e74\u5ea6|\u5e74\u62a5|\u5e74\u5ea6\u62a5\u544a))"
)
_EXCLUDED_TITLE_MARKERS = tuple("\u4e1a\u7ee9\u9884\u544a \u4e1a\u7ee9\u5feb\u62a5 \u4e1a\u7ee9\u8bf4\u660e\u4f1a \u8bf4\u660e\u4f1a \u6743\u76ca\u5206\u6d3e \u5229\u6da6\u5206\u914d \u5206\u7ea2".split())
_QUARTER_MAP = {
    "\u4e00": "03-31", "1": "03-31", "\u4e8c": "06-30", "2": "06-30",
    "\u4e09": "09-30", "3": "09-30", "\u56db": "12-31", "4": "12-31",
}

# latter is retained for provenance, but a shell with a non-UTF-8 code page
# may have rewritten its Chinese literals when this file was transported.
_REPORT_TITLE_RE = re.compile(
    r"(?P<year>20\d{2})\s*\u5e74\s*"
    r"(?:(?:\u7b2c)?(?P<quarter>[\u4e00\u4e8c\u4e09\u56db1234])\s*\u5b63\u5ea6|"
    r"(?P<half>\u534a\u5e74\u5ea6|\u534a\u5e74\u5ea6\u62a5\u544a|\u4e2d\u671f)|"
    r"(?P<annual>\u5e74\u5ea6|\u5e74\u62a5|\u5e74\u5ea6\u62a5\u544a))"
)
_EXCLUDED_TITLE_MARKERS = tuple("\u4e1a\u7ee9\u9884\u544a \u4e1a\u7ee9\u5feb\u62a5 \u4e1a\u7ee9\u8bf4\u660e\u4f1a \u8bf4\u660e\u4f1a \u6743\u76ca\u5206\u6d3e \u5229\u6da6\u5206\u914d \u5206\u7ea2".split())
_QUARTER_MAP = {"\u4e00": "03-31", "1": "03-31", "\u4e8c": "06-30", "2": "06-30", "\u4e09": "09-30", "3": "09-30", "\u56db": "12-31", "4": "12-31"}


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _format_dt(value: _dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    # Keep the source offset visible (Eastmoney timestamps are China time),
    # instead of silently turning an audit field into a UTC wall-clock time.
    return value.isoformat(timespec="seconds")


def _parse_dt(value: Any) -> Optional[_dt.datetime]:
    """Parse source timestamps without treating a report period as publish time."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        parsed = value
    elif isinstance(value, _dt.date):
        parsed = _dt.datetime.combine(value, _dt.time())
    else:
        text = str(value).strip()
        if not text:
            return None
        # Eastmoney uses ``YYYY-mm-dd HH:MM:SS:fff`` in display_time.
        text = re.sub(r"(\d{2}:\d{2}:\d{2}):(\d{1,6})$", r"\1.\2", text)
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = _dt.datetime.fromisoformat(text.replace("/", "-"))
        except ValueError:
            parsed = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    parsed = _dt.datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    # The source timestamps have no offset but are China-market times.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    return parsed


def _parse_asof(value: Any) -> Optional[_dt.datetime]:
    """Parse a cutoff; a date-only cutoff means the end of that China day."""
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    text = str(value).strip() if not isinstance(value, (_dt.date, _dt.datetime)) else ""
    if text and re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text):
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def normalize_code(code: Any) -> str:
    """Return a six digit A-share code, preserving 920xxx board codes."""
    text = str(code or "").strip().upper()
    if text.startswith(("SH", "SZ", "BJ")):
        text = text[2:]
    digits = re.sub(r"\D", "", text)
    return digits.zfill(6) if digits else ""


def parse_report_period(title: Any) -> Optional[str]:
    """Extract a financial report period from a Chinese announcement title."""
    text = str(title or "").strip()
    if not text or any(marker in text for marker in _EXCLUDED_TITLE_MARKERS):
        return None
    match = _REPORT_TITLE_RE.search(text)
    if not match:
        return None
    year = match.group("year")
    if match.group("annual"):
        suffix = "12-31"
    elif match.group("half"):
        suffix = "06-30"
    else:
        suffix = _QUARTER_MAP.get(match.group("quarter"))
    return f"{year}-{suffix}" if suffix else None


def _row_codes(row: Mapping[str, Any]) -> set[str]:
    values = row.get("codes") or row.get("stock_codes") or []
    if isinstance(values, Mapping):
        values = [values]
    result = set()
    for item in values if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) else []:
        if isinstance(item, Mapping):
            value = item.get("stock_code") or item.get("code")
        else:
            value = item
        code = normalize_code(value)
        if code:
            result.add(code)
    return result


def _published_at(row: Mapping[str, Any]) -> Optional[_dt.datetime]:
    # display_time is the time shown to users.  eiTime is a source ingestion
    # time and is only a fallback when display_time is absent.
    return _parse_dt(row.get("display_time") or row.get("published_at") or row.get("eiTime"))


def _request_json(params: Mapping[str, Any], timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Mapping[str, Any]:
    """Fetch one public page with a bounded timeout and no hidden retries."""
    global _LAST_REQUEST_AT
    with _RATE_LOCK:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_AT = time.monotonic()
    query = urlencode({key: value for key, value in params.items() if value is not None})
    request = Request(
        f"{EASTMONEY_NOTICE_URL}?{query}",
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; astock-quant/1.0)",
            "Referer": "https://data.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = response.read()
    decoded = json.loads(payload.decode("utf-8", errors="replace"))
    return decoded if isinstance(decoded, Mapping) else {}


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_cache(payload: Mapping[str, Any]) -> None:
    directory = os.path.dirname(CACHE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{CACHE_PATH}.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, CACHE_PATH)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _unknown(code: str, fetched_at: str, reason: str, *, status: str = "unknown") -> dict:
    return {
        "code": code,
        "report_period": None,
        "published_at": None,
        "source": "unknown",
        "source_kind": SOURCE_KIND,
        "fetched_at": fetched_at,
        "status": status,
        "reason": reason,
    }


def _extract_rows(payload: Mapping[str, Any]) -> list[dict]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    rows = data.get("list") if isinstance(data, Mapping) else []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _candidate_records(rows: Iterable[Mapping[str, Any]], requested: set[str], asof: Optional[_dt.datetime]) -> list[dict]:
    result = []
    for row in rows:
        report_period = parse_report_period(row.get("title") or row.get("title_ch"))
        if not report_period:
            continue
        published = _published_at(row)
        if published is None:
            continue
        row_codes = _row_codes(row) & requested
        if not row_codes:
            # A single-code query can omit codes in a few endpoint responses.
            row_codes = requested if len(requested) == 1 else set()
        if asof is not None:
            if published > asof or report_period > asof.date().isoformat():
                continue
        for code in row_codes:
            result.append({
                "code": code,
                "report_period": report_period,
                "published_at": _format_dt(published),
                "source": SOURCE_NAME,
                "source_kind": SOURCE_KIND,
                "title": row.get("title") or row.get("title_ch"),
                "announcement_id": row.get("art_code"),
            })
    return result


def _latest(records: Iterable[Mapping[str, Any]]) -> Optional[dict]:
    candidates = list(records)
    if not candidates:
        return None
    # Prefer the latest report period, then the latest revision visible at the
    # cutoff.  Do not let a newer publication of an older period hide a newer
    # period that was already available.
    candidates.sort(key=lambda row: (row.get("report_period") or "", row.get("published_at") or ""), reverse=True)
    return dict(candidates[0])


def fetch_disclosure_timeline(
    codes: Iterable[Any] | Any,
    asof: Any = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    request_json: Optional[Callable[..., Mapping[str, Any]]] = None,
) -> list[dict]:
    """Return the latest visible financial-report disclosure per requested code.

    ``asof`` is strict: a source row published after it, or whose report period
    is later than it, is omitted.  ``None`` is live mode.  Every requested code
    gets one result, with ``source=unknown`` when no trustworthy timestamp is
    available.  ``request_json`` is injectable for deterministic tests.
    """
    if isinstance(codes, (str, bytes)):
        requested = [normalize_code(codes)]
    else:
        requested = [normalize_code(code) for code in (codes or [])]
    requested = list(dict.fromkeys(code for code in requested if code))
    if not requested:
        return []
    cutoff = _parse_asof(asof) if asof is not None else None
    fetched_at = _format_dt(_now())
    transport = request_json or _request_json
    with _CACHE_LOCK:
        cache = _load_cache()
    cached_rows = cache.get("rows") if isinstance(cache.get("rows"), Mapping) else {}
    output: dict[str, dict] = {}
    fresh_codes: list[str] = []
    now_epoch = time.time()
    for code in requested:
        cached = cached_rows.get(code) if isinstance(cached_rows, Mapping) else None
        cached_at = cached.get("fetched_epoch", 0) if isinstance(cached, Mapping) else 0
        if cached and cached.get("ok", True) and now_epoch - float(cached_at or 0) < cache_ttl:
            record = _latest(_candidate_records(cached.get("items", []), {code}, cutoff))
            if record:
                record.update({"fetched_at": cached.get("fetched_at") or fetched_at, "status": "cached"})
                output[code] = record
                continue
        fresh_codes.append(code)

    # Query in batches; the endpoint preserves codes in each row, while the
    # single-code fallback handles responses that omit that nested field.
    rows_by_code: dict[str, list[dict]] = {code: [] for code in fresh_codes}
    batch_ok_by_code: dict[str, bool] = {code: True for code in fresh_codes}
    errors: list[str] = []
    batch_size = 30
    for start in range(0, len(fresh_codes), batch_size):
        batch = fresh_codes[start:start + batch_size]
        raw_rows: list[dict] = []
        batch_ok = True
        for page in range(1, max(1, int(max_pages)) + 1):
            try:
                payload = transport({
                    "sr": -1,
                    "st": "ann_date",
                    "ann_type": "SHA,SZA,BJA",
                    "art_code": "",
                    "page_size": max(1, min(int(page_size), 100)),
                    "page_index": page,
                    "stock_list": ",".join(batch),
                }, timeout=timeout)
                page_rows = _extract_rows(payload or {})
                raw_rows.extend(page_rows)
                if len(page_rows) < page_size:
                    break
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
                batch_ok = False
                errors.append(f"{type(exc).__name__}: {exc}")
                break
        for code in batch:
            batch_ok_by_code[code] = batch_ok
            rows_by_code[code] = [
                row for row in raw_rows
                if code in _row_codes(row) or (len(batch) == 1 and not _row_codes(row))
            ]
            if not batch_ok:
                cached = cached_rows.get(code) if isinstance(cached_rows, Mapping) else None
                if isinstance(cached, Mapping):
                    rows_by_code[code] = list(cached.get("items") or [])
            candidate = _latest(_candidate_records(rows_by_code[code], {code}, cutoff))
            if candidate:
                cached_item = cached_rows.get(code) if isinstance(cached_rows, Mapping) else None
                cached_fetched_at = cached_item.get("fetched_at") if isinstance(cached_item, Mapping) else None
                candidate.update({
                    "fetched_at": fetched_at if batch_ok else (cached_fetched_at or fetched_at),
                    "status": "live" if batch_ok else "stale",
                })
                if not batch_ok:
                    candidate["source"] = f"{SOURCE_NAME}-cache-stale"
                    candidate["reason"] = "; ".join(errors) or "实时源未返回"
                output[code] = candidate
            else:
                output[code] = _unknown(
                    code,
                    fetched_at,
                    "未找到可在截止时间前确认的定期报告披露时间",
                )

    # Persist raw parsed-source rows, not the asof-filtered result, so a later
    # historical replay can reuse the same evidence without a network call.
    for code in fresh_codes:
        rows = rows_by_code.get(code, [])
        if batch_ok_by_code.get(code, True):
            cached_rows[code] = {
                "fetched_epoch": now_epoch,
                "fetched_at": fetched_at,
                "items": rows,
                "ok": True,
            }
    cache_payload = {"version": 1, "updated_at": fetched_at, "rows": cached_rows}
    with _CACHE_LOCK:
        _save_cache(cache_payload)

    for code in requested:
        if code not in output:
            # A source failure must never look like a current negative result.
            cached = cached_rows.get(code) if isinstance(cached_rows, Mapping) else None
            if cached:
                stale = _latest(_candidate_records(cached.get("items", []), {code}, cutoff))
                if stale:
                    stale.update({
                        "fetched_at": cached.get("fetched_at") or fetched_at,
                        "status": "stale",
                        "source": f"{SOURCE_NAME}-cache-stale",
                        "reason": "; ".join(errors) or "实时源未返回",
                    })
                    output[code] = stale
            output.setdefault(code, _unknown(code, fetched_at, "; ".join(errors) or "实时源未返回"))
    return [output[code] for code in requested]


__all__ = [
    "EASTMONEY_NOTICE_URL",
    "SOURCE_NAME",
    "fetch_disclosure_timeline",
    "normalize_code",
    "parse_report_period",
]
