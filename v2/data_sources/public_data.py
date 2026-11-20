"""Common public data SDK/CLI with an explicit capability catalog.

Legacy bars/quotes/company/announcements retain their original contracts.
Additional upstream datasets preserve their declared units, adjustment and
history/snapshot/derived semantics; discovery never performs network I/O.
No query writes the account ledger or implicitly fills missing market data.

The public client owns query semantics; SourceHttp owns raw snapshot persistence.
Defaults: company SZ=SZSE, SH/BJ=Eastmoney; announcements=CNINFO.
Only canonical ordinary A shares (.SZ/.SH/.BJ); no automatic source fallback.
Quotes are Tencent snapshots, not a trading-grade real-time feed.
Cache retains only the latest snapshot per request, not a versioned archive.
Run: python v2/data_sources/public_data.py --help
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import math
import re
import sqlite3
import urllib.error
from contextlib import closing, redirect_stdout
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


def _dataset_value(value):
    """Project native dataset values to finite JSON without losing frame attrs."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if type(value).__module__.startswith("pandas") and type(value).__name__ in ("NAType", "NaTType"):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _dataset_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dataset_value(v) for v in value]
    if hasattr(value, "columns") and callable(getattr(value, "to_dict", None)):
        return dict(frame=True, columns=[str(c) for c in value.columns],
                    rows=_dataset_value(value.to_dict(orient="records")),
                    attrs=_dataset_value(dict(value.attrs)))
    if callable(getattr(value, "item", None)):
        return _dataset_value(value.item())
    if callable(getattr(value, "tolist", None)):
        return _dataset_value(value.tolist())
    raise PublicDataError("unsupported_dataset_result", type(value).__name__)


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

    @staticmethod
    def datasets(category: str | None = None) -> list[dict]:
        """Discover upstream capabilities and the project's existing sources."""
        try:
            from .astock_public import catalog
            from .astock_public.source_snapshot import python_blocks
        except ImportError:
            from astock_public import catalog
            from astock_public.source_snapshot import python_blocks
        signatures = {}
        for block in python_blocks():
            for node in ast.parse(block).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    signatures[node.name] = [arg.arg for arg in
                                             node.args.posonlyargs + node.args.args + node.args.kwonlyargs]
        legacy = [
            dict(name="bars", category="market", source="tdx/local", origin="project"),
            dict(name="quotes", category="market", source="tencent-quotes", origin="project"),
            dict(name="company", category="fundamentals", source="szse/eastmoney", origin="project"),
            dict(name="announcements", category="news", source="cninfo", origin="project"),
        ]
        upstream = catalog()
        items = legacy + [dict(item, label=item["name"], name=item["id"], category=item["layer"],
                               parameters=item.get("parameters", signatures.get(item["functions"][0], [])))
                          for item in upstream["capabilities"]]
        seen = {item["name"] for item in items}
        for item in upstream["capabilities"]:
            for function in item["functions"]:
                if function not in seen:
                    items.append(dict(item, name=function, alias_of=item["id"], category=item["layer"],
                                      parameters=item.get("parameters", signatures.get(function, []))))
                    seen.add(function)
        helpers = list(upstream["local_helpers"]) + [
            dict(id="helper." + function, function=function, description="Upstream utility")
            for function in ("cn_today", "dedup_articles", "limit_up_sentiment")]
        for item in helpers:
            for alias in (item["id"], item["function"]):
                if alias not in seen:
                    items.append(dict(item, name=alias, category="helpers", source="upstream",
                                      parameters=signatures.get(item["function"], [])))
                    seen.add(alias)
        return [item for item in items if category is None or item.get("category") == category]

    def dataset(self, name: str, /, *, as_of: str | None = None, **params) -> dict:
        """Query one registered capability; reuse existing routes and cache.

        Strict historical reads require a previously frozen semantic result.
        Effective dates returned by an upstream endpoint never waive that rule.
        """
        if name in ("bars", "quotes", "company", "announcements"):
            return getattr(self, name)(as_of=as_of, **params)
        try:
            from .astock_public import PublicClient, UPSTREAM_COMMIT, catalog
        except ImportError:
            from astock_public import PublicClient, UPSTREAM_COMMIT, catalog
        if not isinstance(name, str) or not name:
            raise PublicDataError("dataset_required")
        if any(key.lower() in ("api_key", "token", "password", "authorization", "secret")
               for key in params):
            raise PublicDataError("credentials_require_environment")
        try:
            request_params = json.dumps(
                {"params": params, "types": {k: type(v).__module__ + "." + type(v).__name__
                                               for k, v in params.items()}},
                default=_dataset_value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise PublicDataError("invalid_dataset_params", "finite serializable parameters required") from exc
        identity = RequestIdentity("astock-public:v2:" + UPSTREAM_COMMIT,
                                   "https://public-data.invalid/dataset/" + name,
                                   date=request_params)

        inventory = catalog()
        capability = next((item for item in inventory["capabilities"]
                           if name in (item["id"], item["name"]) or name in item["functions"]), None)
        live_snapshot = capability is not None and capability["temporal"] in (
            "same_trading_day", "current_snapshot")
        ttl = min(self.http.ttl, 30) if live_snapshot else self.http.ttl

        class DatasetFailure(Exception):
            def __init__(self, result):
                self.result = result

        def validate(raw):
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("status") not in ("ok", "empty"):
                raise PublicDataError("invalid_dataset_result", name)
            return result

        def opener(request, timeout):
            # Raw endpoint bytes are not persisted before the dataset parser has
            # accepted them. The outer SourceHttp freezes only a successful result.
            transport = SourceHttp(timeout=self.http.timeout, retries=self.http.retries,
                                   opener=self.http.opener, clock=self.http.clock, sleep=self.http.sleep)
            upstream = PublicClient(http=transport)
            output = io.StringIO()
            try:
                with redirect_stdout(output):
                    if capability is not None:
                        options = dict(params)
                        if name in capability["functions"]:
                            options.setdefault("function", name)
                        answer = upstream.query(capability["id"], **options)
                        data = _dataset_value(answer.result)
                        provenance = answer.provenance
                    else:
                        helpers = list(inventory["local_helpers"]) + [
                            dict(id="helper." + function, function=function)
                            for function in ("cn_today", "dedup_articles", "limit_up_sentiment")]
                        helper = next((item for item in helpers
                                       if name in (item["id"], item["function"])), None)
                        if helper is None:
                            raise PublicDataError("dataset_not_found", name)
                        data = _dataset_value(upstream.call(helper["function"], **params))
                        provenance = dict(source="upstream helper", temporal="caller_inputs",
                                          historical_first_seen_verified=False)
            except Exception as exc:
                code = str(getattr(exc, "code", "source_error"))
                result = dict(status="blocked" if code in ("credential_required", "optional_dependency_missing")
                              else "error", capability=name, code=code, detail=str(exc)[:300])
                raise DatasetFailure(result) from exc
            messages = output.getvalue().splitlines()[:10]
            warnings = [line for line in messages if any(token in line.lower()
                        for token in ("warn", "error", "失败"))]
            rows = data.get("rows", []) if isinstance(data, dict) else data if isinstance(data, list) else []
            result = dict(status="ok", capability=name, rows=rows, data=data, warnings=warnings,
                          source=provenance.get("source"), temporal=provenance.get("temporal"),
                          provenance=provenance, historical_first_seen_verified=False)
            network_empty = (capability is not None
                             and capability["temporal"] != "local_computation"
                             and (data == [] or (isinstance(data, dict) and data.get("frame") and not rows)))
            incomplete = isinstance(data, dict) and (data.get("attrs") or {}).get("complete") is False
            if warnings or network_empty or incomplete:
                result.update(status="partial" if rows else "error",
                              code="source_warning" if warnings else "source_empty_unverified" if network_empty
                                   else "source_incomplete",
                              fetched_at=timestamp(self.http.clock().isoformat()).isoformat(),
                              cache_hit=False)
                raise DatasetFailure(result)
            return io.BytesIO(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))

        frozen = SourceHttp(self.http.cache_dir, ttl=ttl, offline=self.http.offline,
                            timeout=self.http.timeout, opener=opener, clock=self.http.clock)
        try:
            snap = frozen.request(identity, validate=validate, as_of=as_of)
        except DatasetFailure as exc:
            return exc.result
        result = validate(snap.raw)
        result.update(cache_hit=snap.cache_hit, snapshot_sha256=snap.sha256,
                      fetched_at=snap.fetched_at, snapshot_fetched_at=snap.fetched_at,
                      availability="frozen_fetch_time" if as_of else "retrieved_version")
        return result

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
    parser.add_argument("kind", choices=("bars", "quotes", "company", "announcements", "datasets", "dataset"))
    parser.add_argument("--symbol", help="canonical symbol; comma-separated for quotes")
    parser.add_argument("--name", help="capability name from datasets, for dataset queries")
    parser.add_argument("--params", default="{}", help="JSON object of capability-specific arguments")
    parser.add_argument("--category", help="filter the capability catalog")
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
        if args.kind in ("datasets", "dataset"):
            if (args.symbol is not None or args.start_day is not None or args.end_day is not None
                    or args.source is not None or args.days is not None or args.adjust != "none"
                    or args.bar_db is not None):
                raise PublicDataError("capability_params_required", "put dataset arguments in --params JSON")
            if args.kind == "datasets":
                result = {"status": "ok", "kind": "datasets", "datasets": client.datasets(args.category)}
            else:
                if not args.name:
                    raise PublicDataError("dataset_required", "use --name from datasets")
                params = json.loads(args.params)
                if not isinstance(params, dict):
                    raise PublicDataError("invalid_dataset_params", "--params requires a JSON object")
                result = client.dataset(args.name, as_of=args.as_of, **params)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
            return 0 if result.get("status") in ("ok", "empty") else 3 if result.get("status") == "partial" else 2
        if args.symbol is None:
            raise PublicDataError("symbols_required")
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
