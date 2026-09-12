# -*- coding: utf-8 -*-
"""Pluggable realtime market-data feed contracts and reliability state.

Provider adapters own transport orchestration only. Trading freshness, cross-source
spread tolerances, and execution policy remain in the paper-trading layer.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

QuoteRow = dict[str, Any]


@runtime_checkable
class DataFeed(Protocol):
    """Minimal realtime quote source contract used by scan/execution facades."""

    name: str

    def fetch_realtime(self, codes: Sequence[str]) -> list[QuoteRow]:
        """Return zero or one normalized quote row per requested code."""


@dataclass(frozen=True)
class FeedReliabilityPolicy:
    """Shared bounded transport and circuit-breaker defaults for realtime feeds."""

    timeout_seconds: float = 8.0
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0


class FeedHealthRegistry:
    """Thread-safe per-provider runtime health used by API/audit diagnostics.

    Circuit failures are reserved for provider/transport exceptions. A valid
    request that returns no quote may simply name a nonexistent or delisted
    symbol, so symbol-level misses are degraded evidence and never poison the
    provider-wide circuit.
    """

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time):
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        with self._lock:
            self._state.clear()

    def _entry(self, name: str) -> dict[str, Any]:
        return self._state.setdefault(str(name), {
            "status": "unknown",
            "consecutive_failures": 0,
            "total_successes": 0,
            "total_failures": 0,
            "last_success_at": None,
            "last_failure_at": None,
            "last_error": None,
            "open_until_monotonic": 0.0,
            "probe_in_flight": False,
            "last_requested": 0,
            "last_returned": 0,
        })

    def allow(self, name: str) -> bool:
        """Admit normal calls, or exactly one half-open probe after cooldown."""
        now = self._monotonic()
        with self._lock:
            entry = self._entry(name)
            open_until = float(entry.get("open_until_monotonic") or 0.0)
            if open_until > now:
                entry["status"] = "circuit_open"
                return False
            if entry.get("status") == "circuit_open":
                if entry.get("probe_in_flight"):
                    return False
                entry["status"] = "half_open"
                entry["probe_in_flight"] = True
                return True
            if entry.get("status") == "half_open":
                # One probe already owns the recovery attempt. Other workers
                # keep failing fast until that probe records success/failure.
                return not bool(entry.get("probe_in_flight"))
            return True

    def record_result(self, name: str, *, requested: int, returned: int,
                      policy: FeedReliabilityPolicy, reason: str | None = None) -> None:
        """Record a completed transport result without treating misses as outages."""
        del policy  # Kept in the public signature for one uniform registry API.
        requested = max(0, int(requested))
        returned = max(0, int(returned))
        now = self._wall_clock()
        with self._lock:
            entry = self._entry(name)
            entry["last_requested"] = requested
            entry["last_returned"] = returned
            entry["probe_in_flight"] = False
            # A completed provider response proves transport availability even
            # if this particular symbol is absent. Clear outage streaks and
            # leave missing/partial coverage as degraded, fail-closed evidence.
            entry["consecutive_failures"] = 0
            entry["open_until_monotonic"] = 0.0
            entry["total_successes"] += 1
            entry["last_success_at"] = now
            entry["last_error"] = reason if returned < requested else None
            entry["status"] = "healthy" if returned >= requested else "degraded"

    def record_exception(self, name: str, exc: BaseException,
                         policy: FeedReliabilityPolicy, requested: int) -> None:
        """Record provider-wide/transport failure and advance the circuit."""
        requested = max(0, int(requested))
        now = self._wall_clock()
        with self._lock:
            entry = self._entry(name)
            entry["last_requested"] = requested
            entry["last_returned"] = 0
            entry["probe_in_flight"] = False
            failures = int(entry.get("consecutive_failures") or 0) + 1
            entry["consecutive_failures"] = failures
            entry["total_failures"] += 1
            entry["last_failure_at"] = now
            entry["last_error"] = f"{type(exc).__name__}: {exc}"
            threshold = max(1, int(policy.failure_threshold))
            if failures >= threshold:
                entry["open_until_monotonic"] = (
                    self._monotonic() + max(0.0, float(policy.cooldown_seconds))
                )
                entry["status"] = "circuit_open"
            else:
                entry["open_until_monotonic"] = 0.0
                entry["status"] = "degraded"

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = self._monotonic()
        with self._lock:
            output: dict[str, dict[str, Any]] = {}
            for name, raw in self._state.items():
                item = dict(raw)
                open_until = float(item.pop("open_until_monotonic", 0.0) or 0.0)
                item["circuit_open"] = open_until > now
                item["retry_after_seconds"] = round(max(0.0, open_until - now), 3)
                if item["circuit_open"]:
                    item["status"] = "circuit_open"
                output[name] = item
            return output


DEFAULT_RELIABILITY_POLICY = FeedReliabilityPolicy()
FEED_HEALTH = FeedHealthRegistry()


def feed_health_snapshot() -> dict[str, dict[str, Any]]:
    """Return a JSON-safe copy of per-feed runtime health without network I/O."""
    return FEED_HEALTH.snapshot()


def normalize_codes(codes: Sequence[str] | None) -> list[str]:
    """Keep valid six-digit A-share codes in caller order, without duplicates."""
    return list(dict.fromkeys(
        str(code) for code in (codes or [])
        if str(code).isdigit() and len(str(code)) == 6
    ))


def usable_quote(row: Mapping[str, Any] | None) -> bool:
    """A fallback quote is usable only with a positive finite price and source time."""
    if not isinstance(row, Mapping):
        return False
    try:
        price = float(row.get("price") or 0)
    except (TypeError, ValueError):
        return False
    return math.isfinite(price) and price > 0 and bool(str(row.get("quote_at") or "").strip())


def market_symbol(code: str) -> str:
    code = str(code)
    if code.startswith(("920", "8", "4")):
        return "bj" + code
    if code.startswith(("6", "9")):
        return "sh" + code
    return "sz" + code


@dataclass(frozen=True)
class EastmoneyRealtimeFeed:
    """Eastmoney ulist adapter preserving the legacy completeness metadata contract."""

    get_json: Callable[..., Any]
    secid: Callable[[str], str]
    row_parser: Callable[[Mapping[str, Any]], QuoteRow | None]
    reset_data_source: Callable[..., Any]
    ut: str
    fields: str
    hosts: Sequence[str] = (
        "push2delay.eastmoney.com", "push2.eastmoney.com", "82.push2.eastmoney.com",
    )
    sleep: Callable[[float], Any] = time.sleep
    batch_size: int = 200
    attempts: int = 3
    small_batch_size: int = 50
    name: str = "eastmoney_ulist"
    reliability: FeedReliabilityPolicy = DEFAULT_RELIABILITY_POLICY
    health: FeedHealthRegistry = field(default_factory=lambda: FEED_HEALTH, repr=False, compare=False)

    @staticmethod
    def _empty_meta(normalized: Sequence[str]) -> dict[str, Any]:
        return {
            "rows": [], "expected": len(normalized), "returned": 0,
            "coverage_pct": 0.0, "complete": False, "batches": [],
            "missing_codes": list(normalized)[:200],
        }

    def _fetch(self, codes: Sequence[str]) -> dict[str, Any]:
        normalized = normalize_codes(codes)
        if not normalized:
            return self._empty_meta(normalized)
        if not self.health.allow(self.name):
            return self._empty_meta(normalized)
        out: list[QuoteRow] = []
        batch_meta: list[dict[str, Any]] = []
        batch_size = max(1, int(self.batch_size))
        attempts = max(1, int(self.attempts))
        host_list = list(self.hosts)
        if not host_list:
            self.health.record_exception(
                self.name, RuntimeError("no provider hosts configured"),
                self.reliability, len(normalized),
            )
            return self._empty_meta(normalized)
        transport_succeeded = False
        last_transport_error: BaseException | None = None
        try:
            for offset in range(0, len(normalized), batch_size):
                batch = normalized[offset:offset + batch_size]
                params = {
                    "pn": 1, "pz": len(batch), "np": 1, "fltt": 2, "invt": 2,
                    "ut": self.ut, "fields": self.fields,
                    "secids": ",".join(self.secid(code) for code in batch),
                }
                batch_by_code: dict[str, QuoteRow] = {}
                attempts_used = 0
                for attempt in range(attempts):
                    attempts_used = attempt + 1
                    for host_index in range(len(host_list)):
                        host = host_list[(offset // batch_size + host_index + attempt) % len(host_list)]
                        try:
                            payload = self.get_json(
                                f"https://{host}/api/qt/ulist.np/get", params,
                                timeout=self.reliability.timeout_seconds, retries=1,
                            )
                            transport_succeeded = True
                            diff = (payload or {}).get("data", {}).get("diff") or []
                        except Exception as exc:
                            last_transport_error = exc
                            diff = []
                        if diff:
                            for raw in diff:
                                row = self.row_parser(raw)
                                code = str(row.get("code") or "") if row else ""
                                if row and code in batch:
                                    batch_by_code[code] = row
                            if len(batch_by_code) >= len(batch):
                                break
                        self.reset_data_source("实时行情源空响应")
                        self.sleep(0.25 * (attempt + 1))
                    if len(batch_by_code) >= len(batch):
                        break
                missing = [code for code in batch if code not in batch_by_code]
                if missing and len(missing) > 1:
                    small_size = max(1, int(self.small_batch_size))
                    for start in range(0, len(missing), small_size):
                        small = missing[start:start + small_size]
                        small_params = dict(
                            params, secids=",".join(self.secid(code) for code in small), pz=len(small),
                        )
                        for host in host_list:
                            try:
                                payload = self.get_json(
                                    f"https://{host}/api/qt/ulist.np/get", small_params,
                                    timeout=self.reliability.timeout_seconds, retries=1,
                                )
                                transport_succeeded = True
                                diff = (payload or {}).get("data", {}).get("diff") or []
                            except Exception as exc:
                                last_transport_error = exc
                                diff = []
                            for raw in diff:
                                row = self.row_parser(raw)
                                code = str(row.get("code") or "") if row else ""
                                if row and code in small:
                                    batch_by_code[code] = row
                            if all(code in batch_by_code for code in small):
                                break
                batch_rows = [batch_by_code[code] for code in batch if code in batch_by_code]
                out.extend(batch_rows)
                batch_meta.append({
                    "offset": offset, "requested": len(batch), "returned": len(batch_rows),
                    "coverage_pct": round(len(batch_rows) / max(len(batch), 1) * 100, 2),
                    "complete": len(batch_rows) == len(batch), "attempts": attempts_used,
                    "missing_codes": [code for code in batch if code not in batch_by_code][:100],
                })
        except Exception as exc:
            self.health.record_exception(self.name, exc, self.reliability, len(normalized))
            return self._empty_meta(normalized)
        returned_codes = {str(row.get("code")) for row in out if row.get("code")}
        result = {
            "rows": out, "expected": len(normalized), "returned": len(returned_codes),
            "coverage_pct": round(len(returned_codes) / max(len(normalized), 1) * 100, 2),
            "complete": len(returned_codes) == len(normalized), "batches": batch_meta,
            "missing_codes": [code for code in normalized if code not in returned_codes][:200],
        }
        if not transport_succeeded and last_transport_error is not None:
            self.health.record_exception(
                self.name, last_transport_error, self.reliability, len(normalized),
            )
        else:
            self.health.record_result(
                self.name, requested=len(normalized), returned=len(returned_codes),
                policy=self.reliability,
                reason=None if result["complete"] else "partial realtime coverage",
            )
        return result

    def fetch_realtime(self, codes: Sequence[str]) -> list[QuoteRow]:
        return self._fetch(codes)["rows"]

    def fetch_realtime_with_meta(self, codes: Sequence[str]) -> dict[str, Any]:
        return self._fetch(codes)


@dataclass(frozen=True)
class TencentRealtimeFeed:
    http_get: Callable[..., str]
    parser: Callable[..., list[QuoteRow]]
    reset_data_source: Callable[..., Any]
    sleep: Callable[[float], Any] = time.sleep
    batch_size: int = 30
    attempts: int = 3
    name: str = "tencent_public_quote"
    reliability: FeedReliabilityPolicy = DEFAULT_RELIABILITY_POLICY
    health: FeedHealthRegistry = field(default_factory=lambda: FEED_HEALTH, repr=False, compare=False)

    def fetch_realtime(self, codes: Sequence[str]) -> list[QuoteRow]:
        normalized = normalize_codes(codes)
        if not normalized or not self.health.allow(self.name):
            return []
        rows_by_code: dict[str, QuoteRow] = {}
        transport_succeeded = False
        last_transport_error: BaseException | None = None
        try:
            for start in range(0, len(normalized), max(1, int(self.batch_size))):
                batch = normalized[start:start + max(1, int(self.batch_size))]
                pending = list(batch)
                for attempt in range(max(1, int(self.attempts))):
                    if not pending:
                        break
                    try:
                        text = self.http_get(
                            "https://qt.gtimg.cn/q=" + ",".join(market_symbol(code) for code in pending),
                            timeout=self.reliability.timeout_seconds, encoding="gbk", retries=1,
                        )
                        transport_succeeded = True
                    except Exception as exc:
                        last_transport_error = exc
                        text = ""
                    for row in self.parser(text, attempt=attempt + 1, allowed_codes=pending):
                        code = str(row.get("code") or "")
                        if code in pending:
                            rows_by_code[code] = row
                    pending = [code for code in pending if code not in rows_by_code]
                    if pending and attempt < max(1, int(self.attempts)) - 1:
                        if not text:
                            self.reset_data_source("腾讯个股独立行情源空响应")
                        self.sleep(0.25 * (attempt + 1))
        except Exception as exc:
            self.health.record_exception(self.name, exc, self.reliability, len(normalized))
            return []
        rows = [rows_by_code[code] for code in normalized if code in rows_by_code]
        if not transport_succeeded and last_transport_error is not None:
            self.health.record_exception(
                self.name, last_transport_error, self.reliability, len(normalized),
            )
        else:
            self.health.record_result(
                self.name, requested=len(normalized), returned=len(rows), policy=self.reliability,
                reason=None if len(rows) == len(normalized) else "partial or empty provider response",
            )
        return rows


@dataclass(frozen=True)
class SinaRealtimeFeed:
    session_factory: Callable[[], Any]
    headers: Mapping[str, str]
    parser: Callable[..., list[QuoteRow]]
    reset_data_source: Callable[..., Any]
    sleep: Callable[[float], Any] = time.sleep
    batch_size: int = 80
    attempts: int = 2
    name: str = "sina_public_quote"
    reliability: FeedReliabilityPolicy = DEFAULT_RELIABILITY_POLICY
    health: FeedHealthRegistry = field(default_factory=lambda: FEED_HEALTH, repr=False, compare=False)

    def fetch_realtime(self, codes: Sequence[str]) -> list[QuoteRow]:
        normalized = normalize_codes(codes)
        if not normalized or not self.health.allow(self.name):
            return []
        rows_by_code: dict[str, QuoteRow] = {}
        transport_succeeded = False
        last_transport_error: BaseException | None = None
        try:
            for start in range(0, len(normalized), max(1, int(self.batch_size))):
                batch = normalized[start:start + max(1, int(self.batch_size))]
                text = ""
                for attempt in range(max(1, int(self.attempts))):
                    try:
                        response = self.session_factory().get(
                            "https://hq.sinajs.cn/list=" + ",".join(market_symbol(code) for code in batch),
                            headers={"Referer": "https://finance.sina.com.cn/", **dict(self.headers)},
                            timeout=self.reliability.timeout_seconds,
                        )
                        response.raise_for_status()
                        response.encoding = "gbk"
                        text = response.text or ""
                        transport_succeeded = True
                    except Exception as exc:
                        last_transport_error = exc
                        text = ""
                    if text.strip():
                        break
                    if attempt < max(1, int(self.attempts)) - 1:
                        self.reset_data_source("新浪独立行情源空响应，自动重试")
                        self.sleep(0.25 * (attempt + 1))
                for row in self.parser(text, allowed_codes=batch):
                    code = str(row.get("code") or "")
                    if code in batch:
                        rows_by_code[code] = row
        except Exception as exc:
            self.health.record_exception(self.name, exc, self.reliability, len(normalized))
            return []
        rows = [rows_by_code[code] for code in normalized if code in rows_by_code]
        if not transport_succeeded and last_transport_error is not None:
            self.health.record_exception(
                self.name, last_transport_error, self.reliability, len(normalized),
            )
        else:
            self.health.record_result(
                self.name, requested=len(normalized), returned=len(rows), policy=self.reliability,
                reason=None if len(rows) == len(normalized) else "partial or empty provider response",
            )
        return rows


@dataclass(frozen=True)
class DataFeedChain:
    """Resolve missing codes through ordered feeds; one failed source never aborts the chain."""

    feeds: Sequence[DataFeed]
    reset_data_source: Callable[..., Any] | None = None
    sleep: Callable[[float], Any] = time.sleep
    retry_last_feed: bool = True
    name: str = "independent_public_quote_chain"

    def _merge(self, by_code: dict[str, QuoteRow], rows: Sequence[QuoteRow], allowed: set[str]) -> None:
        for row in rows or []:
            code = str(row.get("code") or "") if isinstance(row, Mapping) else ""
            if code in allowed and usable_quote(row):
                by_code[code] = dict(row)

    @staticmethod
    def _safe_fetch(feed: DataFeed, missing: Sequence[str]) -> list[QuoteRow]:
        try:
            return feed.fetch_realtime(missing)
        except Exception as exc:
            policy = getattr(feed, "reliability", DEFAULT_RELIABILITY_POLICY)
            health = getattr(feed, "health", FEED_HEALTH)
            name = str(getattr(feed, "name", type(feed).__name__))
            if hasattr(health, "record_exception"):
                health.record_exception(name, exc, policy, len(missing))
            return []

    def fetch_realtime(self, codes: Sequence[str]) -> list[QuoteRow]:
        normalized = normalize_codes(codes)
        if not normalized:
            return []
        by_code: dict[str, QuoteRow] = {}
        for feed in self.feeds:
            missing = [code for code in normalized if code not in by_code]
            if not missing:
                break
            self._merge(by_code, self._safe_fetch(feed, missing), set(missing))
        missing = [code for code in normalized if code not in by_code]
        if missing and self.retry_last_feed and self.feeds:
            if self.reset_data_source is not None:
                self.reset_data_source("腾讯/新浪独立行情均有缺口，自动切换后重试")
            self.sleep(0.25)
            self._merge(by_code, self._safe_fetch(self.feeds[-1], missing), set(missing))
        return [by_code[code] for code in normalized if code in by_code]
