#!/usr/bin/env python3
"""Fetch and cross-check A-share daily closes from a-stock-data sources.

This module is deliberately outside the Rust domain and store. It never opens
the ledger, never decides order quantity and never writes a price set. Its only
output is a canonical CSV plus an audit report for the existing
`research/make_price_set.py` pipeline.

The Tencent K-line and Tongdaxin after-hours package logic is adapted from
simonlin1212/a-stock-data v3.10.4 (Apache-2.0), commit
f21534946c42be3ad048ac938eecb1bad84cb8ab. See THIRD_PARTY_NOTICES.md.

Safety rules:
- Both sources must return the same trading day and the same close in cents.
- Both sources must report non-zero volume.
- Volume is compared after normalizing Tencent's lot unit; the known daily
  rounding difference is capped at one lot (100 shares).
- Any missing symbol/day, duplicate row, adjusted-series mismatch, suspicious
  zero volume or source disagreement fails the whole requested batch.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

UPSTREAM_REPO = "https://github.com/simonlin1212/a-stock-data"
UPSTREAM_VERSION = "3.10.4"
UPSTREAM_COMMIT = "f21534946c42be3ad048ac938eecb1bad84cb8ab"
UPSTREAM_LICENSE = "Apache-2.0"

TENCENT_HOSTS = (
    "https://web.ifzq.gtimg.cn",
    "https://proxy.finance.qq.com/ifzqgtimg",
    "https://ifzq.gtimg.cn",
)
TENCENT_KLINE_PATH = "/appstock/app/fqkline/get"
TDX_PACKAGE_URL = "https://www.tdx.com.cn/products/data/data/g4day/{ymd}.zip"
SINA_ADJUST_URL = "https://finance.sina.com.cn/realstock/company/{symbol}/{kind}.js"

# The upstream parser's floors count every priced row (including funds and
# indices). This adapter keeps ordinary stocks only, so those floors would
# reject a valid package. Apply a lower stock-only sanity floor instead.
TDX_MIN_PRICED = {"sh": 1_000, "sz": 500}

TENCENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

SOURCE_LABEL = "tencent-tdx-crosscheck"
MAX_VOLUME_DIFFERENCE_SHARES = 100

_SYMBOL_RE = re.compile(r"^(\d{6})\.(SH|SZ)$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SH_PREFIXES = ("600", "601", "603", "605", "688", "689")
_SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")


def is_supported_stock_code(market: str, code: str) -> bool:
    """Keep ordinary stocks only, never indices, funds or bonds.

    The TDX package also contains indices such as 999999 with four-decimal
    values. V2 price sets are for explicitly classified ordinary shares and
    must not silently absorb those instruments.
    """

    if market == "sh":
        return code.startswith(_SH_PREFIXES)
    if market == "sz":
        return code.startswith(_SZ_PREFIXES)
    return False


class DataSourceError(Exception):
    """A source or contract failure with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class ReviewFinding:
    """One item a human must check before a batch may become `reviewed`."""

    code: str
    symbol: str
    days: tuple[str, ...]
    detail: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "symbol": self.symbol,
            "days": list(self.days),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TencentBar:
    day: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    volume_shares: int
    source_url: str


@dataclass(frozen=True)
class TencentBatch:
    symbol: str
    bars: dict[str, TencentBar]
    raw_sha256: str
    source_url: str


@dataclass(frozen=True)
class TdxBar:
    day: str
    market: str
    code: str
    name: str
    close: Decimal
    volume: int
    amount: Decimal
    source_url: str


@dataclass(frozen=True)
class TdxBatch:
    day: str
    bars: dict[tuple[str, str], TdxBar]
    raw_sha256: str
    source_url: str
    counts: dict[str, int]

    def bar(self, market: str, code: str) -> TdxBar | None:
        return self.bars.get((market, code))


@dataclass(frozen=True)
class AdjustFactor:
    day: str
    factor: Decimal


@dataclass(frozen=True)
class AdjustSeries:
    symbol: str
    kind: str
    factors: tuple[AdjustFactor, ...]
    raw_sha256: str
    source_url: str

    def factor_on(self, day: str) -> Decimal | None:
        """The factor in force on `day`: the latest one published on/before it."""

        current = None
        for item in self.factors:
            if item.day > day:
                break
            current = item.factor
        return current

    def factor_before(self, day: str) -> Decimal | None:
        """The factor in force immediately before `day`, if one was published."""

        current = None
        for item in self.factors:
            if item.day >= day:
                break
            current = item.factor
        return current


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _decimal(value: object, what: str) -> Decimal:
    if isinstance(value, bool):
        raise DataSourceError("invalid_numeric_value", f"{what}: boolean")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise DataSourceError("invalid_numeric_value", f"{what}: {value!r}") from error
    if not result.is_finite():
        raise DataSourceError("invalid_numeric_value", f"{what}: {value!r}")
    return result


def price_to_cents(value: object, what: str = "price") -> int:
    amount = _decimal(value, what)
    if amount <= 0:
        raise DataSourceError("price_must_be_positive", f"{what}: {value!r}")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise DataSourceError(
            "price_precision_not_supported",
            f"{what}: {value!r} has more than two decimals",
        )
    return int(cents)


def format_price(cents: int) -> str:
    if cents <= 0:
        raise DataSourceError("price_must_be_positive", str(cents))
    return f"{cents // 100}.{cents % 100:02d}"


def normalize_symbol(raw: str) -> tuple[str, str, str]:
    """Return `(symbol, market, code)` or refuse an ambiguous identity.

    V2 stores symbols as six digits plus `.SH`/`.SZ`. A bare code is rejected
    because `000001` is ambiguous between a Shenzhen stock and a Shanghai
    index, and guessing would silently value the wrong security.
    """

    text = str(raw).strip().upper()
    match = _SYMBOL_RE.fullmatch(text)
    if not match:
        raise DataSourceError(
            "symbol_requires_exchange_suffix",
            f"{raw!r}; use 600000.SH or 000001.SZ",
        )
    code, market = match.groups()
    if market == "SH" and not code.startswith(_SH_PREFIXES):
        raise DataSourceError("unsupported_symbol", f"{text} is not a supported SH stock")
    if market == "SZ" and not code.startswith(_SZ_PREFIXES):
        raise DataSourceError("unsupported_symbol", f"{text} is not a supported SZ stock")
    return f"{code}.{market}", market.lower(), code


def _parse_day(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise DataSourceError("invalid_day", value) from error
    if parsed.isoformat() != value:
        raise DataSourceError("invalid_day", value)
    return value


def validate_days(days: list[str]) -> list[str]:
    if not days:
        raise DataSourceError("empty_days", "at least one trading day is required")
    parsed = [_parse_day(value) for value in days]
    if len(set(parsed)) != len(parsed):
        raise DataSourceError("duplicate_day", ",".join(parsed))
    if parsed != sorted(parsed):
        raise DataSourceError("days_not_sorted", ",".join(parsed))
    return parsed


def source_label_valid(value: str) -> bool:
    """Match the V2 key rule used by quote identity, not just any text."""

    return bool(_KEY_RE.fullmatch(value))


def _tencent_volume_shares(code: str, volume: object) -> int:
    lots = _decimal(volume, "tencent volume")
    if lots < 0 or lots != lots.to_integral_value():
        raise DataSourceError("invalid_tencent_volume", f"{code}: {volume!r}")
    whole_lots = int(lots)
    # Tencent reports STAR shares directly; all other supported securities
    # report lots. The upstream project documents this as a known 100x trap.
    return whole_lots if code.startswith(("688", "689")) else whole_lots * 100


def parse_tencent_payload(
    payload: object,
    symbol: str,
    start: str,
    end: str,
    source_url: str,
    raw_sha256: str,
) -> TencentBatch:
    normalized, market, code = normalize_symbol(symbol)
    _parse_day(start)
    _parse_day(end)
    if start > end:
        raise DataSourceError("invalid_range", f"{start} > {end}")
    if not isinstance(payload, dict):
        raise DataSourceError("tencent_payload_not_object")
    if payload.get("msg") == "param error":
        raise DataSourceError("tencent_param_error", f"{normalized} {start}~{end}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DataSourceError("tencent_payload_missing_data")
    key = f"{market}{code}"
    node = data.get(key)
    if not isinstance(node, dict):
        raise DataSourceError("tencent_symbol_missing", key)
    if "qfqday" in node and "day" not in node:
        raise DataSourceError(
            "unexpected_adjusted_series",
            "requested unadjusted prices but received only qfqday",
        )
    items = node.get("day")
    if not isinstance(items, list) or not items:
        raise DataSourceError("tencent_empty_series", key)

    bars: dict[str, TencentBar] = {}
    for item in items:
        if not isinstance(item, list) or len(item) < 6:
            raise DataSourceError("tencent_row_format_changed", repr(item))
        day = _parse_day(str(item[0]))
        if day < start or day > end:
            raise DataSourceError("tencent_day_outside_range", day)
        if day in bars:
            raise DataSourceError("tencent_duplicate_day", day)
        open_ = _decimal(item[1], "tencent open")
        close = _decimal(item[2], "tencent close")
        high = _decimal(item[3], "tencent high")
        low = _decimal(item[4], "tencent low")
        if min(open_, close) < low or max(open_, close) > high:
            raise DataSourceError("tencent_ohlc_inconsistent", day)
        price_to_cents(close, f"tencent close {day}")
        bars[day] = TencentBar(
            day=day,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=_decimal(item[5], "tencent volume"),
            volume_shares=_tencent_volume_shares(code, item[5]),
            source_url=source_url,
        )
    return TencentBatch(
        symbol=normalized,
        bars=bars,
        raw_sha256=raw_sha256,
        source_url=source_url,
    )


def fetch_tencent_daily(
    symbol: str,
    start: str,
    end: str,
    *,
    timeout: float = 30.0,
    opener=urllib.request.urlopen,
) -> TencentBatch:
    normalized, market, code = normalize_symbol(symbol)
    _parse_day(start)
    _parse_day(end)
    if start > end:
        raise DataSourceError("invalid_range", f"{start} > {end}")
    param = f"{market}{code},day,{start},{end},640,"
    errors: list[str] = []
    for host in TENCENT_HOSTS:
        url = f"{host}{TENCENT_KLINE_PATH}?{urllib.parse.urlencode({'param': param})}"
        request = urllib.request.Request(url, headers=TENCENT_HEADERS)
        try:
            with opener(request, timeout=timeout) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            errors.append(f"{host}: {type(error).__name__}")
            continue
        if status >= 400:
            errors.append(f"{host}: HTTP {status}")
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"{host}: invalid JSON")
            continue
        try:
            return parse_tencent_payload(
                payload,
                normalized,
                start,
                end,
                url,
                _sha256(raw),
            )
        except DataSourceError as error:
            if error.code in {"tencent_param_error", "unexpected_adjusted_series"}:
                raise
            errors.append(f"{host}: {error.code}")
    raise DataSourceError("tencent_unavailable", "; ".join(errors))


def _decode_tdx_name(raw: bytes) -> str:
    try:
        name = raw.split(b"\x00")[0].decode("gbk").strip()
    except UnicodeDecodeError as error:
        raise DataSourceError("tdx_name_not_gbk") from error
    if not name:
        raise DataSourceError("tdx_name_empty")
    return name


def parse_tdx_package(
    content: bytes,
    day: str,
    *,
    source_url: str,
    raw_sha256: str | None = None,
    min_priced: dict[str, int] | None = None,
) -> TdxBatch:
    _parse_day(day)
    if not content.startswith(b"PK"):
        raise DataSourceError("tdx_package_not_zip")
    limits = dict(TDX_MIN_PRICED if min_priced is None else min_priced)
    ymd = day.replace("-", "")
    bars: dict[tuple[str, str], TdxBar] = {}
    counts: dict[str, int] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            for market in ("sh", "sz"):
                cod_name = f"{market}{ymd[2:]}.cod"
                md1_name = f"{market}{ymd[2:]}.md1"
                if cod_name not in names or md1_name not in names:
                    raise DataSourceError(
                        "tdx_package_missing_market",
                        f"{day} {market}: {cod_name}/{md1_name}",
                    )
                cod = archive.read(cod_name)
                md1 = archive.read(md1_name)
                if len(cod) % 150 or len(md1) % 512:
                    raise DataSourceError("tdx_package_truncated", f"{day} {market}")
                if len(cod) // 150 != len(md1) // 512:
                    raise DataSourceError("tdx_package_count_mismatch", f"{day} {market}")
                seen_codes: set[str] = set()
                seen_sequences: set[int] = set()
                before = len(bars)
                for offset in range(0, len(cod), 150):
                    record = cod[offset : offset + 150]
                    try:
                        code = record[0:6].rstrip(b"\x00 ").decode("ascii")
                    except UnicodeDecodeError as error:
                        raise DataSourceError("tdx_code_not_ascii") from error
                    if not re.fullmatch(r"[0-9]{6}", code):
                        raise DataSourceError("tdx_code_format_changed", repr(code))
                    sequence = struct.unpack("<H", record[32:34])[0]
                    if code in seen_codes or sequence in seen_sequences:
                        raise DataSourceError(
                            "tdx_duplicate_record",
                            f"{day} {market} {code} seq={sequence}",
                        )
                    seen_codes.add(code)
                    seen_sequences.add(sequence)
                    block = md1[sequence * 512 : (sequence + 1) * 512]
                    if len(block) != 512:
                        raise DataSourceError(
                            "tdx_block_out_of_range",
                            f"{day} {market} {code} seq={sequence}",
                        )
                    prev_close, open_, high, low, close = struct.unpack(
                        "<5d", block[4:44]
                    )
                    amount = struct.unpack("<d", block[72:80])[0]
                    values = (prev_close, open_, high, low, close, amount)
                    if not all(math.isfinite(value) for value in values):
                        raise DataSourceError(
                            "tdx_non_finite_value",
                            f"{day} {market} {code}",
                        )
                    if close <= 0:
                        # Indices and custom board codes can have an all-zero
                        # block. They are not securities in this adapter.
                        continue
                    if not is_supported_stock_code(market, code):
                        # Do not treat an index close (often four decimals) as
                        # an ordinary-share price. It would either fail the
                        # precision check or, worse, enter a stock price set.
                        continue
                    price_to_cents(close, f"tdx close {day} {market}{code}")
                    volume = struct.unpack("<Q", block[56:64])[0]
                    name = _decode_tdx_name(record[40:72])
                    bars[(market, code)] = TdxBar(
                        day=day,
                        market=market,
                        code=code,
                        name=name,
                        close=Decimal(str(close)).quantize(Decimal("0.01")),
                        volume=volume,
                        amount=Decimal(str(amount)),
                        source_url=source_url,
                    )
                count = len(bars) - before
                counts[market] = count
                if count < limits[market]:
                    raise DataSourceError(
                        "tdx_market_too_small",
                        f"{day} {market}: {count} < {limits[market]}",
                    )
    except zipfile.BadZipFile as error:
        raise DataSourceError("tdx_package_corrupt") from error
    return TdxBatch(
        day=day,
        bars=bars,
        raw_sha256=raw_sha256 or _sha256(content),
        source_url=source_url,
        counts=counts,
    )


def fetch_tdx_daily(
    day: str,
    *,
    timeout: float = 90.0,
    opener=urllib.request.urlopen,
) -> TdxBatch:
    _parse_day(day)
    url = TDX_PACKAGE_URL.format(ymd=day.replace("-", ""))
    request = urllib.request.Request(url, headers=TENCENT_HEADERS)
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise DataSourceError(
                "tdx_package_unavailable",
                f"{day}: non-trading day, not published yet, or outside retention",
            ) from error
        raise DataSourceError("tdx_http_error", f"{day}: HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DataSourceError(
            "tdx_unavailable",
            f"{day}: {type(error).__name__}",
        ) from error
    if status >= 400:
        raise DataSourceError("tdx_http_error", f"{day}: HTTP {status}")
    return parse_tdx_package(
        raw,
        day,
        source_url=url,
        raw_sha256=_sha256(raw),
    )


def parse_sina_adjust_payload(
    text: str,
    symbol: str,
    kind: str,
    *,
    source_url: str,
    raw_sha256: str,
) -> AdjustSeries:
    """Parse Sina's qfq/hfq factor series without assuming a clean JSON doc.

    The response is wrapped in `var sh600000qfq=...` and may end with a
    `/* ... */` block, so the decoder must stop at the first complete object.
    Only ordinary-share `f` factors are accepted; funds use a different
    split/dividend口径 that V2 does not model.
    """

    normalized, market, code = normalize_symbol(symbol)
    if kind not in {"qfq", "hfq"}:
        raise DataSourceError("invalid_adjust_kind", kind)
    brace = text.find("{")
    if brace < 0:
        raise DataSourceError("sina_adjust_not_json", normalized)
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[brace:])
    except json.JSONDecodeError as error:
        raise DataSourceError("sina_adjust_not_json", normalized) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise DataSourceError("sina_adjust_payload_changed", normalized)
    factors: dict[str, AdjustFactor] = {}
    for item in payload["data"]:
        if not isinstance(item, dict) or "d" not in item or "f" not in item:
            raise DataSourceError("sina_adjust_row_changed", normalized)
        if "s" in item or "u" in item:
            raise DataSourceError("sina_adjust_fund_not_supported", normalized)
        day = _parse_day(str(item["d"]))
        factor = _decimal(item["f"], f"sina {kind} factor {normalized}")
        if factor <= 0:
            raise DataSourceError("sina_adjust_factor_not_positive", normalized)
        if day in factors:
            raise DataSourceError("sina_adjust_duplicate_day", f"{normalized} {day}")
        factors[day] = AdjustFactor(day=day, factor=factor)
    if not factors:
        # Sina returns an empty data array for unsupported symbols. Treating
        # that as "no corporate action" would silently approve a stale series.
        raise DataSourceError("sina_adjust_empty", normalized)
    ordered = tuple(factors[day] for day in sorted(factors))
    return AdjustSeries(
        symbol=normalized,
        kind=kind,
        factors=ordered,
        raw_sha256=raw_sha256,
        source_url=source_url,
    )


def fetch_sina_adjust(
    symbol: str,
    kind: str = "qfq",
    *,
    timeout: float = 20.0,
    opener=urllib.request.urlopen,
) -> AdjustSeries:
    normalized, market, code = normalize_symbol(symbol)
    if kind not in {"qfq", "hfq"}:
        raise DataSourceError("invalid_adjust_kind", kind)
    url = SINA_ADJUST_URL.format(symbol=f"{market}{code}", kind=kind)
    request = urllib.request.Request(url, headers=TENCENT_HEADERS)
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DataSourceError(
            "sina_adjust_unavailable", f"{normalized}: {type(error).__name__}"
        ) from error
    if status >= 400:
        raise DataSourceError("sina_adjust_http_error", f"{normalized}: HTTP {status}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DataSourceError("sina_adjust_not_utf8", normalized) from error
    return parse_sina_adjust_payload(
        text,
        normalized,
        kind,
        source_url=url,
        raw_sha256=_sha256(raw),
    )


def review_findings(
    symbols: list[str],
    days: list[str],
    tencent_batches: dict[str, TencentBatch],
    tdx_batches: dict[str, TdxBatch],
    adjust_series: dict[str, "AdjustSeries | DataSourceError"],
    *,
    max_volume_difference_shares: int = MAX_VOLUME_DIFFERENCE_SHARES,
) -> list[ReviewFinding]:
    """Flag facts that block automatic promotion to `reviewed`.

    Cross-checking two sources proves that both agree *today*. It cannot prove
    that a close is comparable across an ex-dividend/split boundary or that a
    suspended stock's stale close is a real trade. Those checks are stated as
    findings for a human; this function never silently "fixes" a price.
    """

    findings: list[ReviewFinding] = []
    for symbol in symbols:
        normalized, market, code = normalize_symbol(symbol)
        batch = tencent_batches.get(normalized)
        if batch is None:
            findings.append(
                ReviewFinding(
                    "tencent_series_missing",
                    normalized,
                    tuple(days),
                    "no Tencent series was available for this symbol",
                )
            )
            continue
        adjust = adjust_series.get(normalized)
        if isinstance(adjust, DataSourceError):
            findings.append(
                ReviewFinding(
                    "adjust_factor_unavailable",
                    normalized,
                    tuple(days),
                    f"{adjust.code}: {adjust.detail}",
                )
            )
            adjust = None
        elif adjust is None:
            findings.append(
                ReviewFinding(
                    "adjust_factor_missing",
                    normalized,
                    tuple(days),
                    "no adjust-factor series; corporate actions cannot be ruled out",
                )
            )
        if adjust is not None:
            changes: list[str] = []
            previous = None
            first = days[0] if days else None
            if first is not None:
                before = adjust.factor_before(first)
                on_first = adjust.factor_on(first)
                if before is not None and on_first is not None and before != on_first:
                    changes.append(f"before {first} {before}→{first} {on_first}")
            for day in days:
                factor = adjust.factor_on(day)
                if factor is None:
                    findings.append(
                        ReviewFinding(
                            "adjust_factor_not_covered",
                            normalized,
                            (day,),
                            "adjust-factor series does not cover this day",
                        )
                    )
                    previous = None
                    continue
                if previous is not None and factor != previous[1]:
                    changes.append(f"{previous[0]}→{day} {previous[1]}→{factor}")
                previous = (day, factor)
            if changes:
                findings.append(
                    ReviewFinding(
                        "corporate_action_in_window",
                        normalized,
                        tuple(days),
                        "adjust factor changed: " + "; ".join(changes),
                    )
                )
        for day in days:
            tdx = tdx_batches.get(day)
            if tdx is None:
                continue
            tdx_bar = tdx.bar(market, code)
            tencent_bar = batch.bars.get(day)
            if tdx_bar is None or tencent_bar is None:
                continue
            difference = abs(tencent_bar.volume_shares - tdx_bar.volume)
            if difference > max_volume_difference_shares:
                findings.append(
                    ReviewFinding(
                        "volume_gap_needs_review",
                        normalized,
                        (day,),
                        f"volume differs by {difference} shares beyond one lot",
                    )
                )
            if tdx_bar.volume == 0 or tencent_bar.volume_shares == 0:
                findings.append(
                    ReviewFinding(
                        "possible_suspension",
                        normalized,
                        (day,),
                        "zero volume; verify suspension/migration before publishing",
                    )
                )
    return findings


def build_csv(rows: list[dict]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=("symbol", "day", "price", "source", "observed_at"),
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def cross_check(
    symbols: list[str],
    days: list[str],
    tencent_fetcher,
    tdx_fetcher,
    adjust_fetcher,
    *,
    source_label: str = SOURCE_LABEL,
) -> tuple[list[dict], dict, list[dict]]:
    """Cross-check every requested symbol/day and return CSV rows plus evidence.

    The function continues after individual failures so the report can show all
    disagreements. The caller must write the CSV only when `errors` is empty.
    """

    normalized_symbols = [normalize_symbol(symbol)[0] for symbol in symbols]
    if len(set(normalized_symbols)) != len(normalized_symbols):
        raise DataSourceError("duplicate_symbol", ",".join(normalized_symbols))
    checked_days = validate_days(days)
    if not normalized_symbols:
        raise DataSourceError("empty_symbols")
    if not source_label_valid(source_label):
        raise DataSourceError(
            "invalid_source_label",
            f"{source_label!r}; use V2 key characters A-Z, a-z, 0-9, -_.:",
        )

    errors: list[dict] = []
    checks: list[dict] = []
    rows: list[dict] = []
    tencent_batches: dict[str, TencentBatch] = {}
    tdx_batches: dict[str, TdxBatch] = {}
    adjust_series: dict[str, AdjustSeries] = {}

    for symbol in normalized_symbols:
        try:
            batch = tencent_fetcher(symbol, checked_days[0], checked_days[-1])
        except DataSourceError as error:
            errors.append(
                {"scope": "tencent", "symbol": symbol, "code": error.code, "detail": error.detail}
            )
            continue
        tencent_batches[symbol] = batch

    for symbol in normalized_symbols:
        try:
            adjust_series[symbol] = adjust_fetcher(symbol)
        except DataSourceError as error:
            adjust_series[symbol] = error
            errors.append(
                {
                    "scope": "adjust",
                    "symbol": symbol,
                    "code": error.code,
                    "detail": error.detail,
                }
            )

    for day in checked_days:
        try:
            tdx_batches[day] = tdx_fetcher(day)
        except DataSourceError as error:
            errors.append(
                {"scope": "tdx", "day": day, "code": error.code, "detail": error.detail}
            )

    for symbol in normalized_symbols:
        normalized, market, code = normalize_symbol(symbol)
        batch = tencent_batches.get(normalized)
        if batch is None:
            continue
        for day in checked_days:
            tdx = tdx_batches.get(day)
            if tdx is None:
                continue
            tencent_bar = batch.bars.get(day)
            tdx_bar = tdx.bar(market, code)
            if tencent_bar is None:
                errors.append(
                    {
                        "scope": "crosscheck",
                        "symbol": normalized,
                        "day": day,
                        "code": "tencent_day_missing",
                        "detail": "source did not return the requested trading day",
                    }
                )
                continue
            if tdx_bar is None:
                errors.append(
                    {
                        "scope": "crosscheck",
                        "symbol": normalized,
                        "day": day,
                        "code": "tdx_symbol_missing",
                        "detail": f"{market}{code} absent from package",
                    }
                )
                continue
            tencent_close = price_to_cents(tencent_bar.close, f"tencent {normalized} {day}")
            tdx_close = price_to_cents(tdx_bar.close, f"tdx {normalized} {day}")
            if tencent_close != tdx_close:
                errors.append(
                    {
                        "scope": "crosscheck",
                        "symbol": normalized,
                        "day": day,
                        "code": "close_mismatch",
                        "detail": f"tencent={tencent_close} tdx={tdx_close} cents",
                    }
                )
                continue
            if tencent_bar.volume_shares == 0 or tdx_bar.volume == 0:
                errors.append(
                    {
                        "scope": "crosscheck",
                        "symbol": normalized,
                        "day": day,
                        "code": "zero_volume",
                        "detail": (
                            f"tencent={tencent_bar.volume_shares} "
                            f"tdx={tdx_bar.volume} shares; suspended, migrated or stale"
                        ),
                    }
                )
                continue
            difference = abs(tencent_bar.volume_shares - tdx_bar.volume)
            if difference > MAX_VOLUME_DIFFERENCE_SHARES:
                errors.append(
                    {
                        "scope": "crosscheck",
                        "symbol": normalized,
                        "day": day,
                        "code": "volume_mismatch",
                        "detail": (
                            f"tencent={tencent_bar.volume_shares} "
                            f"tdx={tdx_bar.volume} diff={difference} shares"
                        ),
                    }
                )
                continue
            rows.append(
                {
                    "symbol": normalized,
                    "day": day,
                    "price": format_price(tencent_close),
                    "source": source_label,
                    "observed_at": f"{day}T15:00:00+08:00",
                }
            )
            checks.append(
                {
                    "symbol": normalized,
                    "day": day,
                    "tencent_close_cents": tencent_close,
                    "tdx_close_cents": tdx_close,
                    "tencent_volume_shares": tencent_bar.volume_shares,
                    "tdx_volume_shares": tdx_bar.volume,
                    "volume_difference_shares": difference,
                    "close_ok": True,
                    "volume_ok": True,
                    "tencent_source_url": tencent_bar.source_url,
                    "tdx_source_url": tdx_bar.source_url,
                }
            )

    csv_text = build_csv(rows) if rows else ""
    expected_rows = len(normalized_symbols) * len(checked_days)
    findings = review_findings(
        normalized_symbols,
        checked_days,
        tencent_batches,
        tdx_batches,
        adjust_series,
    )
    report = {
        "schema_version": 1,
        "adapter": "a-stock-data-crosscheck",
        "status": "failed" if errors else "needs_review" if findings else "ok",
        "upstream": {
            "repo": UPSTREAM_REPO,
            "version": UPSTREAM_VERSION,
            "commit": UPSTREAM_COMMIT,
            "license": UPSTREAM_LICENSE,
        },
        "symbols": normalized_symbols,
        "days": checked_days,
        "expected_rows": expected_rows,
        "rows": len(rows),
        "partial_rows": len(rows) if errors else 0,
        # Do not publish a digest for a failed batch: it could be mistaken for
        # a complete CSV even though the CLI intentionally did not write one.
        "csv_sha256": _sha256(csv_text.encode("utf-8")) if not errors and csv_text else None,
        "checks": checks,
        "errors": errors,
        "review_findings": [finding.as_dict() for finding in findings],
        "review_complete": not findings,
        "tencent_series": [
            {
                "symbol": symbol,
                "raw_sha256": batch.raw_sha256,
                "source_url": batch.source_url,
                "bar_count": len(batch.bars),
            }
            for symbol, batch in sorted(tencent_batches.items())
        ],
        "tdx_packages": [
            {
                "day": day,
                "raw_sha256": batch.raw_sha256,
                "source_url": batch.source_url,
                "counts": batch.counts,
            }
            for day, batch in sorted(tdx_batches.items())
        ],
        "adjust_series": [
            {
                "symbol": symbol,
                "kind": series.kind,
                "raw_sha256": series.raw_sha256,
                "source_url": series.source_url,
                "factor_count": len(series.factors),
            }
            for symbol, series in sorted(adjust_series.items())
            if isinstance(series, AdjustSeries)
        ],
    }
    return rows, report, errors


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(build_csv(rows))


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _split_csv(value: str, what: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise DataSourceError(f"empty_{what}", value)
    return items


def _default_report_path(out: Path) -> Path:
    return out.with_name(out.name + ".report.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser(
        "fetch",
        help="fetch Tencent daily bars and cross-check them with TDX packages",
    )
    fetch.add_argument("--symbols", required=True, help="comma-separated, e.g. 600000.SH")
    fetch.add_argument("--days", required=True, help="explicit sorted trading days")
    fetch.add_argument("--out", required=True, type=Path, help="canonical CSV path")
    fetch.add_argument("--report", type=Path, help="audit JSON path")
    fetch.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    try:
        symbols = _split_csv(args.symbols, "symbols")
        days = _split_csv(args.days, "days")
        normalized_symbols = [normalize_symbol(symbol)[0] for symbol in symbols]
        checked_days = validate_days(days)
        rows, report, errors = cross_check(
            normalized_symbols,
            checked_days,
            lambda symbol, start, end: fetch_tencent_daily(
                symbol, start, end, timeout=args.timeout
            ),
            lambda day: fetch_tdx_daily(day, timeout=args.timeout),
            lambda symbol: fetch_sina_adjust(symbol, timeout=args.timeout),
        )
    except DataSourceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    report_path = args.report or _default_report_path(args.out)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    write_report(report_path, report)
    if errors:
        for error in errors:
            print(
                "error: {scope} {symbol} {day} {code}: {detail}".format(
                    scope=error.get("scope", ""),
                    symbol=error.get("symbol", ""),
                    day=error.get("day", ""),
                    code=error.get("code", ""),
                    detail=error.get("detail", ""),
                ).strip(),
                file=sys.stderr,
            )
        print(f"audit report: {report_path}", file=sys.stderr)
        return 1

    write_csv(args.out, rows)
    print(
        f"wrote {args.out} rows={len(rows)} symbols={len(normalized_symbols)} "
        f"days={len(checked_days)} source={SOURCE_LABEL}"
    )
    print(f"audit report: {report_path}")
    if report["review_findings"]:
        print(
            f"review required: {len(report['review_findings'])} finding(s); "
            "see review_findings in the audit report",
            file=sys.stderr,
        )
        return 4
    print("review checklist: no corporate-action or volume anomaly flagged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
