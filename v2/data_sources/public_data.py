"""On-demand public data. No ledger writes, implicit fill or adjusted prices.

The public client owns query semantics; SourceHttp owns raw snapshot persistence.
Defaults: company SZ=SZSE, SH/BJ=Eastmoney; announcements=CNINFO.
Only canonical ordinary A shares (.SZ/.SH/.BJ); no automatic source fallback.
Quotes are Tencent snapshots, not a trading-grade real-time feed.
Cache retains only the latest snapshot per request, not a versioned archive.
Run: python v2/data_sources/public_data.py --help
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sqlite3
import urllib.error
from contextlib import closing
from decimal import Decimal, InvalidOperation
from dataclasses import asdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Sequence

try:
    from .source_http import SourceHttp, SourceHttpError, RequestIdentity, tdx_identity, timestamp, add_cache_arguments
except ImportError:
    from source_http import SourceHttp, SourceHttpError, RequestIdentity, tdx_identity, timestamp, add_cache_arguments


# Bump the affected semantic adapter version when its output contract changes.
REFERENCE_PROVIDER_VERSIONS = {("company", "szse"): "1",
                               ("company", "eastmoney"): "1",
                               ("announcements", "cninfo"): "1"}


class PublicDataError(SourceHttpError):
    pass


def symbol_id(value: str) -> str:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{6}\.[A-Z]+", value) and value.split(".")[1] not in ("SH", "SZ", "BJ"):
        raise PublicDataError("unsupported_market", value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", value):
        raise PublicDataError("invalid_symbol", str(value))
    code, market = value.split(".")
    prefixes = {"SH": ("600", "601", "603", "605", "688", "689"),
                "SZ": ("000", "001", "002", "003", "300", "301"),
                "BJ": ("43", "83", "87", "88", "92")}
    if not code.startswith(prefixes[market]):
        raise PublicDataError("unsupported_symbol", value)
    return value


def day_id(value: str) -> str:
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError(value)
    except (ValueError, TypeError):
        raise PublicDataError("invalid_day", str(value)) from None
    return value


def parse_quote_bytes(raw: bytes, symbols: Sequence[str]) -> list[dict]:
    # Match by the response's identity, never by response order or zip().
    try:
        from .realtime_quotes import _parse_record
    except ImportError:
        from realtime_quotes import _parse_record
    try:
        text = raw.decode("gbk")
    except UnicodeDecodeError as exc:
        raise PublicDataError("quote_bad_encoding") from exc
    found = {}
    for part in text.split(";"):
        if not part.strip():
            continue
        match = re.fullmatch(r'\s*v_(sh|sz|bj)([0-9]{6})="([^"\r\n]*)"\s*', part)
        if not match:
            raise PublicDataError("quote_invalid_record")
        market, code, payload = match.groups()
        symbol = code + "." + market.upper()
        if symbol not in symbols or symbol in found:
            raise PublicDataError("quote_identity_mismatch", symbol)
        if not payload:
            raise PublicDataError("quote_missing", symbol)
        fields = payload.split("~")
        if len(fields) <= 30 or fields[2] != code:
            raise PublicDataError("quote_identity_mismatch", symbol)
        try:
            observed = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(
                tzinfo=timezone(timedelta(hours=8))).isoformat()
        except ValueError as exc:
            raise PublicDataError("quote_invalid_observation", symbol) from exc
        for index in (3, 4, 5, 33, 34):
            try:
                value = Decimal(fields[index])
                if not value.is_finite() or value < 0 or value * 100 != (value * 100).to_integral_value():
                    raise ValueError("not nonnegative integer cents")
            except (InvalidOperation, ValueError, IndexError) as exc:
                raise PublicDataError("quote_invalid_price", symbol) from exc
        try:
            quote = _parse_record(symbol, payload, observed)
        except Exception as exc:
            raise PublicDataError("quote_invalid_record", symbol) from exc
        if quote is None:
            raise PublicDataError("quote_missing", symbol)
        row = asdict(quote)
        if any(row[key] < 0 for key in ("volume_shares", "amount_cents", "open_cents", "high_cents", "low_cents", "prev_close_cents")):
            raise PublicDataError("quote_invalid_value", symbol)
        found[symbol] = row
    missing = set(symbols) - found.keys()
    if missing or not found:
        raise PublicDataError("quote_missing", ",".join(sorted(missing)))
    return [found[s] for s in symbols]


class PublicDataClient:
    def __init__(self, http: SourceHttp | None = None, *, bar_db: str | Path | None = None,
                 company_provider: Callable | None = None,
                 announcements_provider: Callable | None = None,
                 provider_source: str | None = None,
                 company_provider_version: str | None = None,
                 announcements_provider_version: str | None = None):
        self.http = http or SourceHttp()
        self.bar_db = Path(bar_db) if bar_db is not None else None
        self.company_provider = company_provider
        self.announcements_provider = announcements_provider
        self.provider_source = provider_source
        self.company_provider_version = company_provider_version
        self.announcements_provider_version = announcements_provider_version

    def quotes(self, symbols: Sequence[str], *, as_of: str | None = None) -> dict:
        symbols = sorted(set(symbol_id(s) for s in symbols))
        if not symbols:
            raise PublicDataError("symbols_required")
        rows, snapshots = [], []
        for offset in range(0, len(symbols), 800):
            batch = symbols[offset:offset + 800]
            query = ",".join(s.split(".")[1].lower() + s.split(".")[0] for s in batch)
            identity = RequestIdentity("tencent-quotes", "https://qt.gtimg.cn/q=" + query,
                                       symbol=",".join(batch))
            def validate(raw):
                for row in parse_quote_bytes(raw, batch):
                    cutoff = timestamp(as_of) if as_of else timestamp(self.http.clock().isoformat())
                    if timestamp(row["observed_at"]) > cutoff:
                        raise PublicDataError("observation_after_as_of", row["symbol"])
            snap = self.http.request(identity, headers={"User-Agent": "Mozilla/5.0 (compatible; astock-v2)"},
                                     validate=validate, as_of=as_of, max_age=30)
            parsed = parse_quote_bytes(snap.raw, batch)
            for row in parsed:
                cutoff = timestamp(as_of) if as_of else timestamp(self.http.clock().isoformat())
                row.update(source=snap.source, fetched_at=snap.fetched_at, adjust="none",
                           quote_time=row["observed_at"],
                           age_seconds=(cutoff - timestamp(row["observed_at"])).total_seconds())
            rows.extend(parsed)
            snapshots.append(dict(sha256=snap.sha256, fetched_at=snap.fetched_at, cache_hit=snap.cache_hit))
        return dict(status="ok", kind="quotes", adjust="none", price_unit="cent",
                    volume_unit="share", rows=rows, snapshots=snapshots,
                    freshness="source_snapshot_not_trading_grade", cache_max_age_seconds=min(self.http.ttl, 30))

    def bars(self, symbol: str, start_day: str, end_day: str, *, source: str = "tdx",
             adjust: str = "none", as_of: str | None = None,
             days: Sequence[str] | None = None) -> dict:
        symbol = symbol_id(symbol)
        start_day, end_day = day_id(start_day), day_id(end_day)
        if start_day > end_day:
            raise PublicDataError("invalid_range")
        if days is not None:
            if isinstance(days, (str, bytes)):
                raise PublicDataError("invalid_trading_days")
            days = [day_id(d) for d in days]
            if not days or days != sorted(set(days)) or any(d < start_day or d > end_day for d in days):
                raise PublicDataError("invalid_trading_days", "strictly ascending unique dates within range required")
        if adjust != "none":
            raise PublicDataError("unsupported_adjust", adjust)
        if source == "local":
            return self._local_bars(symbol, start_day, end_day, as_of, days)
        if source != "tdx":
            raise PublicDataError("unsupported_source", source)
        try:
            from .ingest_market import parse_package
        except ImportError:
            from ingest_market import parse_package
        if days is None:
            if start_day != end_day:
                raise PublicDataError("trading_days_required", "remote ranges require explicit days")
            days = [start_day]
        rows, missing = [], []
        for day in days:
            def parse(raw):
                try:
                    return parse_package(raw, day.replace("-", ""))
                except Exception as exc:
                    if hasattr(exc, "code"):
                        raise PublicDataError(exc.code, str(exc)) from exc
                    raise
            def validate(raw):
                parsed = parse(raw)
                observed = timestamp(day + "T15:00:00+08:00")
                cutoff = timestamp(as_of) if as_of else timestamp(self.http.clock().isoformat())
                if observed > cutoff:
                    raise PublicDataError("observation_after_as_of", day)
                if not parsed:
                    raise PublicDataError("bars_missing", day)
            snap = self.http.request(tdx_identity(day), validate=validate, as_of=as_of)
            matched = [r for r in parse(snap.raw) if r.symbol == symbol]
            if not matched:
                missing.append(day)
            for row in matched:
                rows.append(dict(asdict(row), adjust="none", source="tdx", observed_at=day + "T15:00:00+08:00",
                                 fetched_at=snap.fetched_at, sha256=snap.sha256, cache_hit=snap.cache_hit))
        if missing or not rows:
            raise PublicDataError("bars_missing", ",".join(missing))
        return self._bar_result(rows)

    @staticmethod
    def _bar_result(rows):
        return dict(status="ok", kind="bars", adjust="none", price_unit="cent", volume_unit="share", rows=rows)

    def _local_bars(self, symbol, start, end, as_of, days=None):
        if self.bar_db is None or not self.bar_db.is_file():
            raise PublicDataError("local_database_missing")
        # Legacy bars do not carry first-seen time. Never invent PIT provenance.
        if as_of is not None:
            raise PublicDataError("historical_local_snapshot_unsupported")
        fetched_at = timestamp(self.http.clock().isoformat()).isoformat()
        uri = self.bar_db.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            try:
                rows = [dict(r) for r in connection.execute(
                    "SELECT * FROM bar WHERE symbol=? AND day>=? AND day<=? AND adjust='none' ORDER BY day", (symbol, start, end))
                        if days is None or r["day"] in days]
            except sqlite3.Error as exc:
                raise PublicDataError("local_schema_unsupported", str(exc)) from exc
        if not rows:
            raise PublicDataError("bars_missing", symbol)
        seen = set()
        for row in rows:
            if row.get("adjust") != "none" or not row.get("source"):
                raise PublicDataError("local_provenance_invalid", row.get("day", ""))
            if row["day"] in seen:
                raise PublicDataError("duplicate_bar", row["day"])
            seen.add(row["day"])
            for field in ("close_cents", "volume_shares"):
                if type(row.get(field)) is not int or row[field] < (1 if field == "close_cents" else 0):
                    raise PublicDataError("local_bar_invalid", field)
            if "tradable" in row:
                if type(row["tradable"]) is not int or row["tradable"] not in (0, 1):
                    raise PublicDataError("local_bar_invalid", "tradable must be 0 or 1")
                row["tradable"] = bool(row["tradable"])
            observed = day_id(row["day"]) + "T15:00:00+08:00"
            if timestamp(observed) > timestamp(fetched_at):
                raise PublicDataError("observation_in_future", row["day"])
            row.update(observed_at=observed, fetched_at=None, read_at=fetched_at,
                       availability="source_fetch_time_unknown")
        # Calendar gaps are explicit: no forward fill and no guessed trading calendar.
        result = self._bar_result(rows)
        result["coverage"] = "stored_rows_only"
        cursor, missing = date.fromisoformat(start), []
        while cursor.isoformat() <= end:
            if cursor.isoformat() not in seen and (days is None or cursor.isoformat() in days):
                missing.append(cursor.isoformat())
            cursor += timedelta(days=1)
        result["missing_calendar_days"] = missing
        result["trading_calendar_verified"] = False
        if missing:
            result["status"] = "partial"
        return result

    def _reference(self, kind, symbol, start=None, end=None, *, as_of=None, source=None, **options):
        symbol = symbol_id(symbol)
        provider = self.company_provider if kind == "company" else self.announcements_provider
        version = self.company_provider_version if kind == "company" else self.announcements_provider_version
        if provider is not None:
            actual_source = source or self.provider_source
            if not actual_source:
                raise PublicDataError("unsupported_source", "injected provider requires an explicit source")
            if source is not None and self.provider_source and source != self.provider_source:
                raise PublicDataError("unsupported_source", source)
        else:
            try:
                from . import company_disclosures as adapters
            except ImportError:
                import company_disclosures as adapters
            actual_source = source or (("szse" if symbol.endswith(".SZ") else "eastmoney")
                                       if kind == "company" else "cninfo")
            if kind == "company":
                if actual_source not in ("szse", "eastmoney"):
                    raise PublicDataError("unsupported_source", actual_source)
                if actual_source == "szse" and not symbol.endswith(".SZ"):
                    raise PublicDataError("unsupported", "SZSE company profiles support SZ only")
                function = "company_profile" if actual_source == "szse" else "company_profile_eastmoney"
                provider = getattr(adapters, function, None)
                if provider is None:
                    raise PublicDataError("unsupported", actual_source + " company adapter unavailable")
            else:
                if actual_source != "cninfo":
                    raise PublicDataError("unsupported_source", actual_source)
                provider = adapters.announcements
        if version is None:
            version = REFERENCE_PROVIDER_VERSIONS.get((kind, actual_source), "1")
        if not isinstance(version, str) or not version:
            raise PublicDataError("invalid_provider_version")
        if kind == "announcements":
            start, end = day_id(start), day_id(end)
            if start > end:
                raise PublicDataError("invalid_range")
        identity = RequestIdentity(actual_source + ":" + kind + ":v" + version,
                                   "https://public-data.invalid/" + kind,
                                   symbol=symbol, date=json.dumps([start, end, options], sort_keys=True))
        def validate(raw):
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise PublicDataError("provider_invalid_result") from exc
            if not isinstance(result, dict) or not result:
                raise PublicDataError("provider_empty_result")
            if result.get("complete") is False:
                raise PublicDataError("provider_incomplete_result")
            if result.get("symbol", symbol) != symbol:
                raise PublicDataError("provider_symbol_mismatch")
            if result.get("status") in ("error", "unsupported"):
                raise PublicDataError(result["status"], kind)
            if result.get("provider", actual_source) != actual_source:
                raise PublicDataError("provider_source_mismatch")
            if any(row.get("source", actual_source) != actual_source for row in result.get("rows", [])):
                raise PublicDataError("provider_source_mismatch")
            return result
        def fetch(url, method="GET", data=None, headers=None):
            # Provider owns semantic parsing. Unvalidated intermediate bytes are not cached.
            request = RequestIdentity(actual_source, url, method=method, payload=data or b"", symbol=symbol)
            return self.http.request(request, headers=headers).raw
        def opener(request, timeout):
            fetched_at = timestamp(self.http.clock().isoformat()).isoformat()
            try:
                if kind == "company":
                    result = provider(symbol, fetched_at=fetched_at, fetch=fetch)
                else:
                    result = provider(symbol, start, end, fetched_at=fetched_at, fetch=fetch, **options)
            except SourceHttpError:
                raise
            except Exception as exc:
                if hasattr(exc, "code"):
                    raise PublicDataError(str(exc.code), str(exc)) from exc
                raise PublicDataError("provider_error", str(exc)) from exc
            return io.BytesIO(json.dumps(result, ensure_ascii=False).encode())
        frozen = SourceHttp(self.http.cache_dir, ttl=self.http.ttl, offline=self.http.offline,
                            timeout=self.http.timeout, opener=opener, clock=self.http.clock)
        snap = frozen.request(identity, validate=validate, as_of=as_of)
        return dict(status="ok", kind=kind, symbol=symbol, source=actual_source, provider_version=version,
                    observed_at=snap.fetched_at, fetched_at=snap.fetched_at,
                    availability="frozen_fetch_time", sha256=snap.sha256, cache_hit=snap.cache_hit,
                    data=validate(snap.raw))

    def company(self, symbol: str, *, as_of=None, source=None):
        return self._reference("company", symbol, as_of=as_of, source=source)

    def announcements(self, symbol: str, start_day: str, end_day: str, *, as_of=None,
                      page_size=30, max_pages=20, source=None):
        if type(page_size) is not int or not 1 <= page_size <= 100 or type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise PublicDataError("invalid_pagination")
        return self._reference("announcements", symbol, start_day, end_day, as_of=as_of, source=source,
                               page_size=page_size, max_pages=max_pages)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("bars", "quotes", "company", "announcements"))
    parser.add_argument("--symbol", required=True, help="canonical symbol; comma-separated for quotes")
    parser.add_argument("--start-day")
    parser.add_argument("--end-day")
    parser.add_argument("--source", help="bars: tdx (default)/local; quotes: tencent-quotes; "
                        "company: szse (SZ default)/eastmoney (SH/BJ default); announcements: cninfo")
    parser.add_argument("--days", help="comma-separated strictly ascending trading days for remote multi-day bars")
    parser.add_argument("--bar-db", type=Path)
    parser.add_argument("--adjust", default="none")
    add_cache_arguments(parser)
    args = parser.parse_args(argv)
    try:
        client = PublicDataClient(SourceHttp(args.cache_dir, ttl=args.cache_ttl, offline=args.offline), bar_db=args.bar_db)
        if args.adjust != "none":
            raise PublicDataError("unsupported_adjust", args.adjust)
        if args.kind in ("company", "quotes") and (args.start_day is not None or args.end_day is not None):
            raise PublicDataError("unsupported_range", args.kind + " does not accept a historical date range; use an existing frozen --as-of snapshot")
        allowed = {"bars": {"tdx", "local"}, "quotes": {"tencent-quotes"},
                   "company": {"szse", "eastmoney"}, "announcements": {"cninfo"}}
        if args.source is not None and args.source not in allowed[args.kind]:
            raise PublicDataError("unsupported_source", args.kind + ": " + args.source)
        if args.days is not None and args.kind != "bars":
            raise PublicDataError("unsupported", "--days applies to bars only")
        if args.kind == "quotes":
            result = client.quotes(args.symbol.split(","), as_of=args.as_of)
        elif args.kind == "bars":
            result = client.bars(args.symbol, args.start_day, args.end_day, source=args.source or "tdx", adjust=args.adjust, as_of=args.as_of,
                                 days=args.days.split(",") if args.days is not None else None)
        elif args.kind == "company":
            result = client.company(args.symbol, as_of=args.as_of, source=args.source)
        else:
            result = client.announcements(args.symbol, args.start_day, args.end_day, as_of=args.as_of, source=args.source)
    except (SourceHttpError, urllib.error.URLError, OSError, ValueError) as exc:
        print(json.dumps(dict(status="error", code=getattr(exc, "code", "source_error"), detail=str(exc))))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
