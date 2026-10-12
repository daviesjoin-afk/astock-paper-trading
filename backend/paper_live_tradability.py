"""Prefetch current public status facts; ingest only at an execution write boundary.

No historical backfill and no provider I/O in ``persist``. Source observations
retain their retrieval times and cannot authorize an earlier execution instant.
"""
import datetime as dt
import hashlib
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
import market_data_contract as MDC
import market_data_service as MDSvc
from marketdata_transport import http_get
import tradability_archive as TA
import tradability_ingestion as TI

TZ = dt.timezone(dt.timedelta(hours=8))
STATUS_MAX_AGE_SECONDS = 120
_CACHE = {}
_LOCK = threading.Lock()
PROFILE_SOURCE = "eastmoney_stock_profile+complete_suspension_report+verified_quote"
MARKET_SOURCE = "current_full_market_membership+complete_suspension_report+verified_quote_range"


def _get(key, url, params):
    with _LOCK:
        cached = _CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1], cached[2]
    data = json.loads(http_get(url, params=params, timeout=3, retries=1))
    observed = dt.datetime.now(TZ).isoformat(timespec="microseconds")
    with _LOCK:
        _CACHE[key] = (time.monotonic(), data, observed)
    return data, observed


def _suspensions(day):
    params = {"sortColumns": "SUSPEND_START_DATE", "sortTypes": -1,
              "pageSize": 500, "pageNumber": 1,
              "reportName": "RPT_CUSTOM_SUSPEND_DATA_INTERFACE", "columns": "ALL",
              "source": "WEB", "client": "WEB",
              "filter": f'(MARKET="全部")(DATETIME=\'{day}\')'}
    rows, observed, expected = [], None, None
    pages = 1
    for page in range(1, 5):
        if page > pages:
            break
        params["pageNumber"] = page
        payload, stamp = _get(("suspend", day, page),
                              "https://datacenter-web.eastmoney.com/api/data/v1/get", params)
        result = payload.get("result")
        if payload.get("success") is not True or not isinstance(result, dict):
            raise ValueError("suspension source unavailable")
        if page == 1:
            pages, expected = int(result["pages"]), int(result["count"])
        if (int(result["pages"]) != pages or int(result["count"]) != expected
                or pages < 0 or pages > 4 or not isinstance(result.get("data"), list)):
            raise ValueError("suspension page coverage inconsistent")
        rows.extend(result["data"])
        observed = max(observed or stamp, stamp)
    if (len(rows) != expected or len({json.dumps(r, sort_keys=True) for r in rows}) != len(rows)
            or any(len(str(r.get("SECURITY_CODE") or "")) != 6
                   or not str(r.get("SECURITY_CODE") or "").isdigit() for r in rows)):
        raise ValueError("suspension coverage incomplete")
    return rows, observed


def _time(value):
    if not value:
        return None
    text = str(value)
    result = (dt.datetime.strptime(text, "%Y%m%d%H%M%S") if text.isdigit() and len(text) == 14
              else dt.datetime.fromisoformat(text))
    return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)


def _suspended(rows, code, observed):
    relevant = [r for r in rows if str(r["SECURITY_CODE"]) == code]
    for row in relevant:
        start, end = _time(row.get("SUSPEND_START_TIME")), _time(row.get("SUSPEND_END_TIME"))
        if start is None or (end is not None and end < start):
            return None
        if start <= observed and (end is None or observed <= end):
            return True
    return False


def prefetch(quotes, *, asof_day):
    """Current-day only, bounded to 200 passing independent-checked quotes."""
    day = str(asof_day)[:10]
    if day != dt.datetime.now(TZ).date().isoformat():
        return {}
    selected = {str(k): v for k, v in quotes.items()
                if v.get("quote_source") == "live" and v.get("quote_validation") == "cross_source_checked"
                and MDC.classify(MDC.symbol_quote_snapshot(v, asof_day=day),
                                 MDC.EXECUTION_QUOTE_POLICY, now=dt.datetime.now(TZ),
                                 asof_day=day).status == MDC.STATUS_FRESH}
    if not selected or len(selected) > 200:
        return {}
    try:
        suspensions, suspension_at = _suspensions(day)
    except Exception:
        return {}
    try:
        reading = MDSvc.read_snapshot(MDC.LIVE_MARKET_POLICY, now=dt.datetime.now(TZ), asof_day=day)
        market = ({str(r.get("code")): dict(r) for r in reading.rows()}
                  if reading.status == MDC.STATUS_FRESH and reading.snapshot is not None
                  and reading.snapshot.kind == MDSvc.KIND_FULL_MARKET_SNAPSHOT
                  and reading.snapshot.complete and reading.snapshot.as_of == day
                  and reading.snapshot.verification == MDC.VERIFICATION_VERIFIED else {})
        market_fingerprint = MDC.snapshot_fingerprint(reading.snapshot) if market else None
    except Exception:
        market, market_fingerprint = {}, None

    def one(item):
        code, quote = item
        try:
            if len(code) != 6 or not code.isdigit() or not code.startswith(("0", "3", "6")):
                return code, None
            primary_quote = quote
            cross = quote.get("quote_cross_check") or {}
            if (cross.get("passed") is True and cross.get("code") == code
                    and cross.get("source") in ("tencent_public_quote", "sina_public_quote")
                    and float(cross.get("price") or 0) > 0):
                current_at = _time(cross.get("quote_at"))
                if current_at is not None and current_at > _time(quote["quote_at"]):
                    # This is a status reference only. Never retimestamp the
                    # older primary price or its fund-flow fields.
                    quote = {**quote, "price": float(cross["price"]),
                             "quote_at": current_at.isoformat(),
                             "name": cross.get("name") or quote.get("name")}
            row = market.get(code) or {}
            price = float(quote["price"])
            low, high = row.get("low"), row.get("high")
            row_at = _time(row.get("quote_at"))
            interior = (isinstance(low, (int, float)) and isinstance(high, (int, float))
                        and math.isfinite(low) and math.isfinite(high) and 0 < low < price < high
                        and row_at is not None and row_at.date().isoformat() == day
                        and row_at <= dt.datetime.now(TZ))
            if interior and row.get("name"):
                # A complete current-market observation proves current membership,
                # not an IPO date or historical membership. An interior price is
                # strictly inside any fixed daily limit: observed low/high already
                # lie inside that limit. No order-book lock is inferred at extrema.
                listing, lower, upper = None, float(low), float(high)
                profile = {"f57": code, "f58": quote.get("name") or row["name"]}
                profile_at = reading.snapshot.saved_at or reading.snapshot.observed_at
                price_at = _time(quote["quote_at"])
                source = MARKET_SOURCE
                market_identity = market_fingerprint
            else:
                payload, profile_at = _get(("profile", day, code),
                    "https://push2.eastmoney.com/api/qt/stock/get",
                    {"fltt": 2, "invt": 2, "secid": f"{1 if code.startswith('6') else 0}.{code}",
                     "fields": "f57,f58,f189,f51,f52,f43,f86"})
                profile = payload.get("data") or {}
                if payload.get("rc") != 0 or profile.get("f57") != code or not profile.get("f58"):
                    return code, None
                listing = dt.datetime.strptime(str(profile.get("f189")), "%Y%m%d").date()
                lower, upper, price = float(profile["f52"]), float(profile["f51"]), float(profile["f43"])
                price_at = dt.datetime.fromtimestamp(float(profile["f86"]), TZ)
                source, market_identity = PROFILE_SOURCE, None
            price_age = (dt.datetime.now(TZ) - price_at).total_seconds()
            if (not all(math.isfinite(v) for v in (lower, upper, price)) or not 0 < lower < price < upper
                    or price_at.date().isoformat() != day or not -120 <= price_age <= STATUS_MAX_AGE_SECONDS
                    or abs(price - float(quote["price"])) / price > 0.005):
                # At a price limit, a last price cannot prove the book is unlocked.
                return code, None
            observed = max(_time(profile_at), _time(suspension_at), _time(quote.get("quote_at")))
            suspended = _suspended(suspensions, code, observed)
            if (listing is not None and listing > observed.date()) or suspended is None:
                return code, None
            volume = quote.get("volume")
            fields = {"is_listed": True, "listing_date": listing.isoformat() if listing is not None else None,
                      "is_st": "ST" in profile["f58"].upper() or "退" in profile["f58"],
                      "is_suspended": suspended, "has_market_quote": True,
                      "has_trade_volume": (volume > 0 if isinstance(volume, (int, float))
                                           and math.isfinite(volume) else None),
                      "is_price_limit_locked": False}
            return code, {"code": code, "session_date": day, "fields": fields,
                          "observed_at": observed.isoformat(), "effective_at": observed.isoformat(),
                          "source_observations": {"profile": profile_at, "suspension": suspension_at,
                                                  "quote": quote.get("quote_at"), "latest": observed.isoformat()},
                          "source_evidence": {"profile": profile, "suspension_count": len(suspensions),
                                              "status_price_reference": {"price": quote["price"], "quote_at": quote["quote_at"]},
                                              "primary_price_reference": {"price": primary_quote["price"], "quote_at": primary_quote["quote_at"]},
                                              "market_snapshot_fingerprint": market_identity,
                                              "observed_range": {"low": lower, "high": upper},
                                              "suspension_rows": [r for r in suspensions if r["SECURITY_CODE"] == code],
                                              "suspension_digest": hashlib.sha256(json.dumps(
                                                  suspensions, sort_keys=True).encode()).hexdigest()},
                          "source": source}
        except (ValueError, TypeError, KeyError, OverflowError, requests.RequestException):
            return code, None
    with ThreadPoolExecutor(max_workers=min(8, len(selected))) as executor:
        return {code: fact for code, fact in executor.map(one, selected.items()) if fact is not None}


class _PrefetchedProvider(TI.TradabilityFactProvider):
    provider_id = "current_public_status"
    provider_version = "1"

    def __init__(self, observation):
        self.observation = observation

    def fetch(self, code, session):
        item = self.observation
        if code != item["code"] or session != item["session_date"]:
            raise ValueError("observation identity mismatch")
        return TI.ProviderResult(self.provider_id, self.provider_version, TI.OUTCOME_EVIDENCE,
                                 evidence=item["fields"], observed_kind=TI.OBSERVED_RETRIEVED_AT,
                                 observed_at=item["observed_at"], effective_at=item["effective_at"])


def persist(conn, quote, *, code, asof_day):
    """Ingest prefetched facts through the existing owner, without committing."""
    item = quote.get("tradability_observation")
    if not isinstance(item, dict):
        return
    instant = _time(quote.get("execution_asof"))
    observed, effective = _time(item.get("observed_at")), _time(item.get("effective_at"))
    if (item.get("code") != str(code) or item.get("session_date") != str(asof_day)[:10]
            or not instant or not observed or not effective or max(observed, effective) > instant
            or (instant - observed).total_seconds() > STATUS_MAX_AGE_SECONDS
            or observed.date().isoformat() != str(asof_day)[:10]
            or item.get("source") not in (PROFILE_SOURCE, MARKET_SOURCE)
            or quote.get("quote_validation") != "cross_source_checked"):
        raise ValueError("current tradability identity/time evidence invalid")
    fingerprint = hashlib.sha256(json.dumps(item, sort_keys=True, allow_nan=False).encode()).hexdigest()
    TI.IngestionService([_PrefetchedProvider(item)], TA.TradabilityArchiveRepository(conn),
                        cutoff=instant.isoformat(), audit_conn=conn).ingest(
        [str(code)], [str(asof_day)[:10]], write=True, run_id=f"live:{fingerprint}")
