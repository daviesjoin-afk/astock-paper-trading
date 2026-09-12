# -*- coding: utf-8 -*-
"""Pluggable realtime market-data feed contracts.

This module owns provider orchestration, not trading policy. Concrete feeds
normalize the same small realtime quote contract while callers decide whether a
quote is fresh/tradable and whether cross-source differences are acceptable.
Transport and parsers are injected so every adapter is testable offline.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

QuoteRow = dict[str, Any]


@runtime_checkable
class DataFeed(Protocol):
    """Minimal realtime quote source contract used by scan/execution facades."""

    name: str

    def fetch_realtime(self, codes: Sequence[str]) -> list[QuoteRow]:
        """Return zero or one normalized quote row per requested code."""


def normalize_codes(codes: Sequence[str] | None) -> list[str]:
    """Keep valid six-digit A-share codes in caller order, without duplicates."""
    return list(
        dict.fromkeys(
            str(code)
            for code in (codes or [])
            if str(code).isdigit() and len(str(code)) == 6
        )
    )


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
    """Translate a six-digit A-share code to the public-feed market symbol."""
    code = str(code)
    if code.startswith(("920", "8", "4")):
        return "bj" + code
    if code.startswith(("6", "9")):
        return "sh" + code
    return "sz" + code


@dataclass(frozen=True)
class EastmoneyRealtimeFeed:
    """Eastmoney ulist adapter with the existing completeness metadata contract."""

    get_json: Callable[..., Any]
    secid: Callable[[str], str]
    row_parser: Callable[[Mapping[str, Any]], QuoteRow | None]
    reset_data_source: Callable[..., Any]
    ut: str
    fields: str
    hosts: Sequence[str] = (
        "push2delay.eastmoney.com",
        "push2.eastmoney.com",
        "82.push2.eastmoney.com",
    )
    sleep: Callable[[float], Any] = time.sleep
    batch_size: int = 200
    attempts: int = 3
    small_batch_size: int = 50
    name: str = "eastmoney_ulist"

    def _fetch(self, codes: Sequence[str]) -> dict[str, Any]:
        normalized = normalize_codes(codes)
        if not normalized:
            return {
                "rows": [], "expected": 0, "returned": 0, "coverage_pct": 0.0,
                "complete": False, "batches": [], "missing_codes": [],
            }
        out: list[QuoteRow] = []
        batch_meta: list[dict[str, Any]] = []
        batch_size = max(1, int(self.batch_size))
        attempts = max(1, int(self.attempts))
        host_list = list(self.hosts)
        for offset in range(0, len(normalized), batch_size):
            batch = normalized[offset:offset + batch_size]
            secids = ",".join(self.secid(code) for code in batch)
            params = {
                "pn": 1, "pz": len(batch), "np": 1, "fltt": 2, "invt": 2,
                "ut": self.ut, "fields": self.fields, "secids": secids,
            }
            batch_by_code: dict[str, QuoteRow] = {}
            attempts_used = 0
            for attempt in range(attempts):
                attempts_used = attempt + 1
                for host_index in range(len(host_list)):
                    host = host_list[(offset // batch_size + host_index + attempt) % len(host_list)]
                    try:
                        payload = self.get_json(
                            f"https://{host}/api/qt/ulist.np/get", params, retries=1,
                        )
                        diff = (payload or {}).get("data", {}).get("diff") or []
                    except Exception:
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
                small_batch_size = max(1, int(self.small_batch_size))
                for start in range(0, len(missing), small_batch_size):
                    small = missing[start:start + small_batch_size]
                    small_params = dict(
                        params,
                        secids=",".join(self.secid(code) for code in small),
                        pz=len(small),
                    )
                    for host in host_list:
                        try:
                            payload = self.get_json(
                                f"https://{host}/api/qt/ulist.np/get", small_params, retries=1,
                            )
                            diff = (payload or {}).get("data", {}).get("diff") or []
                        except Exception:
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
                "offset": offset,
                "requested": len(batch),
                "returned": len(batch_rows),
                "coverage_pct": round(len(batch_rows) / max(len(batch), 1) * 100, 2),
                "complete": len(batch_rows) == len(batch),
                "attempts": attempts_used,
                "missing_codes": [code for code in batch if code not in batch_by_code][:100],
            })

        returned_codes = {str(row.get("code")) for row in out if row.get("code")}
        return {
            "rows": out,
            "expected": len(normalized),
            "returned": len(returned_codes),
            "coverage_pct": round(len(returned_codes) / max(len(normalized), 1) * 100, 2),
            "complete": len(returned_codes) == len(normalized),
            "batches": batch_meta,
            "missing_codes": [code for code in normalized if code not in returned_codes][:200],
        }

    def fetch_realtime(self, codes: Sequence[str]) -> list[QuoteRow]:
        return self._fetch(codes)["rows"]

    def fetch_realtime_with_meta(self, codes: Sequence[str]) -> dict[str, Any]:
        return self._fetch(codes)


@dataclass(frozen=True)
class TencentRealtimeFeed:
    """Tencent public realtime adapter with bounded per-code retries."""

    http_get: Callable[..., str]
    parser: Callable[..., list[QuoteRow]]
    reset_data_source: Callable[..., Any]
    sleep: Callable[[float], Any] = time.sleep
    batch_size: int = 30
    attempts: int = 3
    name: str = "tencent_public_quote"

    def fetch_realtime(self, codes: Sequence[str]) -> list[QuoteRow]:
        normalized = normalize_codes(codes)
        if not normalized:
            return []
        rows_by_code: dict[str, QuoteRow] = {}
        batch_size = max(1, int(self.batch_size))
        attempts = max(1, int(self.attempts))
        for start in range(0, len(normalized), batch_size):
            batch = normalized[start:start + batch_size]
            pending = list(batch)
            for attempt in range(attempts):
                if not pending:
                    break
                try:
                    text = self.http_get(
                        "https://qt.gtimg.cn/q=" + ",".join(market_symbol(code) for code in pending),
                        timeout=8,
                        encoding="gbk",
                        retries=1,
                    )
                except Exception:
                    text = ""
                for row in self.parser(text, attempt=attempt + 1, allowed_codes=pending):
                    code = str(row.get("code") or "")
                    if code in pending:
                        rows_by_code[code] = row
                pending = [code for code in pending if code not in rows_by_code]
                if pending and attempt < attempts - 1:
                    if not text:
                        self.reset_data_source("腾讯个股独立行情源空响应")
                    self.sleep(0.25 * (attempt + 1))
        return [rows_by_code[code] for code in normalized if code in rows_by_code]


@dataclass(frozen=True)
class SinaRealtimeFeed:
    """Sina public realtime adapter used as an independent fallback source."""

    session_factory: Callable[[], Any]
    headers: Mapping[str, str]
    parser: Callable[..., list[QuoteRow]]
    reset_data_source: Callable[..., Any]
    sleep: Callable[[float], Any] = time.sleep
    batch_size: int = 80
    attempts: int = 2
    name: str = "sina_public_quote"

    def fetch_realtime(self, codes: Sequence[str]) -> list[QuoteRow]:
        normalized = normalize_codes(codes)
        if not normalized:
            return []
        rows_by_code: dict[str, QuoteRow] = {}
        batch_size = max(1, int(self.batch_size))
        attempts = max(1, int(self.attempts))
        for start in range(0, len(normalized), batch_size):
            batch = normalized[start:start + batch_size]
            text = ""
            for attempt in range(attempts):
                try:
                    response = self.session_factory().get(
                        "https://hq.sinajs.cn/list=" + ",".join(market_symbol(code) for code in batch),
                        headers={"Referer": "https://finance.sina.com.cn/", **dict(self.headers)},
                        timeout=8,
                    )
                    response.raise_for_status()
                    response.encoding = "gbk"
                    text = response.text or ""
                except Exception:
                    text = ""
                if text.strip():
                    break
                if attempt < attempts - 1:
                    self.reset_data_source("新浪独立行情源空响应，自动重试")
                    self.sleep(0.25 * (attempt + 1))
            for row in self.parser(text, allowed_codes=batch):
                code = str(row.get("code") or "")
                if code in batch:
                    rows_by_code[code] = row
        return [rows_by_code[code] for code in normalized if code in rows_by_code]


@dataclass(frozen=True)
class DataFeedChain:
    """Resolve requested codes through ordered feeds without core-chain branching.

    Each feed receives only codes still missing a usable quote. Adding another
    provider is therefore a composition change (append/register a DataFeed), not
    a change to scan, matching, or execution policy. The optional final retry
    preserves the historical Tencent -> Sina -> Sina-retry behavior.
    """

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

    def fetch_realtime(self, codes: Sequence[str]) -> list[QuoteRow]:
        normalized = normalize_codes(codes)
        if not normalized:
            return []
        by_code: dict[str, QuoteRow] = {}
        for feed in self.feeds:
            missing = [code for code in normalized if code not in by_code]
            if not missing:
                break
            self._merge(by_code, feed.fetch_realtime(missing), set(missing))
        missing = [code for code in normalized if code not in by_code]
        if missing and self.retry_last_feed and self.feeds:
            if self.reset_data_source is not None:
                self.reset_data_source("腾讯/新浪独立行情均有缺口，自动切换后重试")
            self.sleep(0.25)
            self._merge(by_code, self.feeds[-1].fetch_realtime(missing), set(missing))
        return [by_code[code] for code in normalized if code in by_code]
