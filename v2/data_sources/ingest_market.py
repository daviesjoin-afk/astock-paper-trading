"""TDX day-package parser and fetch adapter for the Python public-data SDK.

Use ``astock-v2 ingest`` for the operational CLI and SQLite writes.
"""

from __future__ import annotations

import io
import math
import re
import struct
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass

try:
    from .source_http import SourceHttp, SourceHttpError, tdx_identity
except ImportError:
    from source_http import SourceHttp, SourceHttpError, tdx_identity

PACKAGE_URL = "https://www.tdx.com.cn/products/data/data/g4day/{ymd}.zip"
USER_AGENT = "Mozilla/5.0 (compatible; astock-v2)"

# Per-market floor on rows carrying a price. Measured 2026-09-20 across 11
# real packages; the minimum observed was SH 16202 / SZ 3641 / BJ 87, so these
# thresholds sit clearly below that. A package under the floor is truncated.
MIN_PRICED = {"sh": 10_000, "sz": 3_000, "bj": 50}

# Packages before this date carry no Beijing exchange files at all.
BJ_FIRST_DAY = "20220506"

CODE_RE = re.compile(r"[0-9]{6}")

# The package carries every security TDX knows about: 22k bonds, 1.7k funds,
# 1.1k TDX-internal board indices (880*/881*), exchange indices. Only ordinary
# A-shares belong in the trading universe. A full day is ~5.6k stocks versus
# 52k total rows, so filtering here removes 89% of the noise before it ever
# reaches the database.
STOCK_PREFIXES: dict[str, tuple[str, ...]] = {
    "sh": ("600", "601", "603", "605", "688", "689"),
    "sz": ("000", "001", "002", "003", "300", "301"),
    "bj": ("43", "83", "87", "88", "92"),
}


def is_ordinary_a_share(market: str, code: str) -> bool:
    prefixes = STOCK_PREFIXES.get(market)
    if prefixes is None:
        return False
    return code.startswith(prefixes)


class IngestError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Row:
    symbol: str
    day: str
    name: str
    open_cents: int
    high_cents: int
    low_cents: int
    close_cents: int
    prev_close_cents: int
    volume_shares: int
    amount_cents: int
    tradable: bool

    def change_bps(self) -> int:
        """Change from previous close in basis points; 0 when prev close absent."""
        if self.prev_close_cents <= 0:
            return 0
        return (self.close_cents - self.prev_close_cents) * 10_000 // self.prev_close_cents


def _cents(value: float) -> int:
    """Round a decimal price to integer cents.

    The package stores prices as little-endian f64. Values are exact to 2 dp in
    practice, but round() keeps a 0.005 representation error from shaving a cent.
    """
    return int(round(value * 100))


def parse_package(content: bytes, ymd: str) -> list[Row]:
    """Parse a daytime package into rows.

    Layout: per market, a `.cod` code table (150 bytes/record) and an `.md1`
    quote block (512 bytes/block); the code record carries the block index.
    Reference: jing2uo/tdx2db (MIT), cross-checked against Tencent closes.
    """
    day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise IngestError("package_not_zip", str(exc)) from exc

    names = set(archive.namelist())
    rows: list[Row] = []
    for market in ("sh", "sz", "bj"):
        cod_name = f"{market}{ymd[2:]}.cod"
        md1_name = f"{market}{ymd[2:]}.md1"
        if market == "bj" and ymd < BJ_FIRST_DAY:
            if cod_name not in names and md1_name not in names:
                continue
        if cod_name not in names or md1_name not in names:
            raise IngestError(
                "package_missing_file", f"{cod_name}/{md1_name} not in archive"
            )
        cod = archive.read(cod_name)
        md1 = archive.read(md1_name)
        if len(cod) % 150 or len(md1) % 512:
            raise IngestError("package_bad_block_size", market)
        if len(cod) // 150 != len(md1) // 512:
            raise IngestError(
                "package_length_mismatch",
                f"{market}: {len(cod) // 150} codes vs {len(md1) // 512} blocks",
            )

        before = len(rows)
        priced = 0
        seen_codes: set[str] = set()
        seen_seqs: set[int] = set()
        for offset in range(0, len(cod), 150):
            record = cod[offset:offset + 150]
            code = record[0:6].rstrip(b"\x00 ").decode("ascii", "replace")
            if not CODE_RE.fullmatch(code):
                raise IngestError("package_bad_code", f"{market} {code!r}")
            seq = struct.unpack("<H", record[32:34])[0]
            if code in seen_codes or seq in seen_seqs:
                raise IngestError("package_duplicate", f"{market} {code} seq={seq}")
            seen_codes.add(code)
            seen_seqs.add(seq)

            block = md1[seq * 512:(seq + 1) * 512]
            if len(block) != 512:
                raise IngestError("package_block_out_of_range", f"{market}{code}")
            prev_close = struct.unpack("<d", block[4:12])[0]
            open_, high, low, close = struct.unpack("<4d", block[12:44])
            amount = struct.unpack("<d", block[72:80])[0]
            if not all(
                math.isfinite(v) for v in (prev_close, open_, high, low, close, amount)
            ):
                raise IngestError("package_non_finite", f"{market}{code}")
            if close <= 0:
                # Indices and TDX-internal board codes carry an all-zero block.
                # A halted ordinary A-share is different: the quote block is
                # zero, but the package still carries the official previous
                # close. Keep that row as valuation-only instead of dropping it.
                if not is_ordinary_a_share(market, code) or prev_close <= 0:
                    continue
                close = prev_close
                open_ = high = low = 0.0
                amount = 0.0
                volume = 0
                tradable = False
            else:
                priced += 1
                if not is_ordinary_a_share(market, code):
                    continue
                volume = struct.unpack("<Q", block[56:64])[0]
                # A positive close with no volume and all-zero open/high/low is
                # a carried quote, not a fill. The official previous close is
                # the only price that may value an existing position.
                if volume == 0 and open_ == 0 and high == 0 and low == 0:
                    if prev_close <= 0:
                        continue
                    close = prev_close
                    open_ = high = low = 0.0
                    amount = 0.0
                    tradable = False
                else:
                    tradable = True
            raw_name = record[40:72].split(b"\x00")[0]
            try:
                name = raw_name.decode("gbk").strip()
            except UnicodeDecodeError as exc:
                raise IngestError("package_bad_name_encoding", f"{market}{code}") from exc
            if not name:
                raise IngestError("package_missing_name", f"{market}{code}")

            rows.append(
                Row(
                    symbol=f"{code}.{market.upper()}",
                    day=day,
                    name=name,
                    open_cents=_cents(open_),
                    high_cents=_cents(high),
                    low_cents=_cents(low),
                    close_cents=_cents(close),
                    prev_close_cents=_cents(prev_close),
                    volume_shares=volume,
                    amount_cents=int(round(amount * 100)),
                    tradable=tradable,
                )
            )
        # The floor is checked on *priced* rows (bonds, funds, indices included)
        # because that is what the measured thresholds describe. Filtering to
        # stocks happens after, so a truncated package still trips the floor.
        if priced < MIN_PRICED[market]:
            raise IngestError(
                "package_below_floor",
                f"{market}: {priced} priced rows < {MIN_PRICED[market]}",
            )
        if len(rows) - before == 0:
            raise IngestError("package_no_stocks", market)
    return rows


def fetch_package(day: str, *, timeout: float = 90.0,
                  http: SourceHttp | None = None, as_of: str | None = None) -> bytes:
    transport = http or SourceHttp(timeout=timeout)
    try:
        return transport.request(
            tdx_identity(day), headers={"User-Agent": USER_AGENT}, as_of=as_of,
            validate=lambda raw: parse_package(raw, day.replace("-", "")),
        ).raw
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise IngestError("package_not_published", day) from exc
        raise IngestError("package_http_error", str(exc.code)) from exc
    except SourceHttpError as exc:
        raise IngestError(exc.code, exc.detail) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IngestError("package_network_error", str(exc)) from exc
