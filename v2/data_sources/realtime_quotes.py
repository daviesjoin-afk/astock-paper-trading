#!/usr/bin/env python3
"""Realtime quotes from Tencent's batch snapshot endpoint.

Used by the 3-minute intraday scan. Deliberately dumb: it fetches prices and
returns them. It never decides anything and never touches the ledger.

Request cap measured 2026-11-11: 800 codes per request is safe, 1000 returns
HTTP 414 (URI too long). 7 batches cover the whole market in ~1.2s.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Sequence

BATCH_SIZE = 800
ENDPOINT = "https://qt.gtimg.cn/q="
USER_AGENT = "Mozilla/5.0 (compatible; astock-v2)"

# Field layout of one `v_sh600000="..."` record, zero-indexed after splitting
# on '~'. Only the fields we actually use are named here; Tencent's layout has
# many more.
_F_NAME = 1
_F_CODE = 2
_F_PRICE = 3
_F_PREV_CLOSE = 4
_F_OPEN = 5
_F_VOLUME_HAND = 6
_F_HIGH = 33
_F_LOW = 34
_F_AMOUNT_WAN = 37
_F_TURNOVER = 38
_F_PE = 39
_F_TIME = 30


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price_cents: int
    prev_close_cents: int
    open_cents: int
    high_cents: int
    low_cents: int
    volume_shares: int
    amount_cents: int
    observed_at: str

    @property
    def change_bps(self) -> int:
        """Change from previous close in basis points; 0 when prev close is absent."""
        if self.prev_close_cents <= 0:
            return 0
        return (self.price_cents - self.prev_close_cents) * 10_000 // self.prev_close_cents


class QuoteError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def to_symbol(market: str, code: str) -> str:
    """`sh`/`600000` -> `600000.SH` to match the bar table's symbol format."""
    return f"{code}.{market.upper()}"


def _to_cents(text: str) -> int:
    """Parse a decimal price string into integer cents without float drift."""
    if not text:
        raise QuoteError("empty_price_field")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if "." in text:
        whole, frac = text.split(".", 1)
    else:
        whole, frac = text, ""
    frac = (frac + "00")[:2]
    try:
        value = int(whole or "0") * 100 + int(frac)
    except ValueError as exc:
        raise QuoteError("unparsable_price", text) from exc
    return -value if negative else value


def _parse_record(symbol: str, payload: str, observed_at: str) -> Quote | None:
    fields = payload.split("~")
    if len(fields) < 40:
        return None
    # A halted or not-yet-traded stock returns zeros. Skip rather than report a
    # zero price, which downstream would look like a -100% move.
    if not fields[_F_PRICE] or _to_cents(fields[_F_PRICE]) <= 0:
        return None
    try:
        return Quote(
            symbol=symbol,
            name=fields[_F_NAME],
            price_cents=_to_cents(fields[_F_PRICE]),
            prev_close_cents=_to_cents(fields[_F_PREV_CLOSE]),
            open_cents=_to_cents(fields[_F_OPEN]),
            high_cents=_to_cents(fields[_F_HIGH]),
            low_cents=_to_cents(fields[_F_LOW]),
            volume_shares=int(fields[_F_VOLUME_HAND] or 0) * 100,
            amount_cents=int(fields[_F_AMOUNT_WAN] or 0) * 10_000 * 100,
            observed_at=observed_at,
        )
    except (QuoteError, ValueError):
        return None


def fetch_quotes(symbols: Sequence[str], *, timeout: float = 10.0) -> list[Quote]:
    """Fetch quotes for `symbols` (format `600000.SH`) in batches.

    Batches that fail are skipped rather than retried: the next scan is 3
    minutes away, and a tight retry loop is exactly what gets an IP throttled.
    """
    observed_at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    out: list[Quote] = []
    for start in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[start:start + BATCH_SIZE]
        query = ",".join(_to_tencent(s) for s in batch)
        request = urllib.request.Request(
            ENDPOINT + query, headers={"User-Agent": USER_AGENT}
        )
        try:
            raw = urllib.request.urlopen(request, timeout=timeout).read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            continue
        text = raw.decode("gbk", "replace")
        by_symbol = dict(zip(batch, _records(text)))
        for symbol in batch:
            record = by_symbol.get(symbol)
            if record is None:
                continue
            quote = _parse_record(symbol, record, observed_at)
            if quote is not None:
                out.append(quote)
    return out


def _to_tencent(symbol: str) -> str:
    """`600000.SH` -> `sh600000`."""
    code, _, market = symbol.partition(".")
    return f"{market.lower()}{code}"


def _records(text: str) -> Iterable[str]:
    """Yield the quoted payload of each `v_xxx="..."` line, in order."""
    for line in text.split(";"):
        line = line.strip()
        if not line or '"' not in line:
            continue
        _, _, rest = line.partition('="')
        payload, _, _ = rest.rpartition('"')
        if payload:
            yield payload
