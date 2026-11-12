"""Public-source HTTP bytes and opt-in, integrity-checked snapshot cache.

Only transport owns cache writes. Consumers own semantic validation; a response
is cached only after their validator accepts it. No ledger access or fallback
between URLs. Shared TDX identity represents the entire day package, not a stock.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class SourceHttpError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise SourceHttpError("invalid_timestamp", str(value)) from exc


@dataclass(frozen=True)
class RequestIdentity:
    source: str
    url: str
    method: str = "GET"
    payload: bytes = b""
    adjust: str = "none"
    symbol: str = "*"
    date: str = ""

    def document(self) -> dict:
        result = asdict(self)
        result["payload"] = base64.b64encode(self.payload).decode("ascii")
        return result

    def key(self) -> str:
        return hashlib.sha256(json.dumps(self.document(), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class HttpSnapshot:
    raw: bytes
    source: str
    fetched_at: str
    sha256: str
    cache_hit: bool


_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


class SourceHttp:
    """No disk cache unless cache_dir is explicit; offline never accesses network.

    TTL applies offline too (relative to as_of for historical reads). as_of additionally requires a pre-existing snapshot
    fetched on/before that instant; a historical miss never triggers a new fetch.
    A cache hit retains its original fetched_at, including after a fresh read.
    """
    def __init__(self, cache_dir: Path | str | None = None, *, ttl: float = 86400,
                 offline: bool = False, timeout: float = 30, retries: int = 0,
                 opener=None, clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if (not math.isfinite(ttl) or ttl < 0 or not math.isfinite(timeout)
                or not 0 < timeout <= 120 or type(retries) is not int
                or not 0 <= retries <= 3):
            raise SourceHttpError("invalid_transport_config")
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.ttl, self.offline, self.timeout, self.retries = ttl, offline, timeout, retries
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def request(self, identity: RequestIdentity, *, headers: dict | None = None,
                validate: Callable[[bytes], object] | None = None,
                as_of: str | None = None, max_age: float | None = None) -> HttpSnapshot:
        if max_age is not None and (not math.isfinite(max_age) or max_age < 0):
            raise SourceHttpError("invalid_transport_config")
        ttl = self.ttl if max_age is None else min(self.ttl, max_age)
        cutoff = timestamp(as_of) if as_of is not None else None
        key = identity.key()
        lock_key = str(self.cache_dir.resolve()) + key if self.cache_dir else key
        with _LOCK_GUARD:
            lock = _LOCKS.setdefault(lock_key, threading.Lock())
        with lock:
            path = self.cache_dir / (key + ".json") if self.cache_dir else None
            if path is not None and path.exists():
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                    raw = base64.b64decode(doc["response"], validate=True)
                    fetched = timestamp(doc["fetched_at"])
                    if (doc["identity"] != identity.document() or doc["source"] != identity.source
                            or hashlib.sha256(raw).hexdigest() != doc["sha256"] or not raw):
                        raise ValueError("identity, source, or response digest mismatch")
                except (OSError, ValueError, KeyError, TypeError, SourceHttpError) as exc:
                    raise SourceHttpError("cache_corrupt", str(path)) from exc
                now = timestamp(self.clock().isoformat())
                age = ((cutoff or now) - fetched).total_seconds()
                if fetched > now:
                    raise SourceHttpError("cache_future_timestamp", str(path))
                if cutoff is not None and fetched > cutoff:
                    raise SourceHttpError("snapshot_after_as_of", doc["fetched_at"])
                if age <= ttl:
                    if validate is not None:
                        validate(raw)
                    return HttpSnapshot(raw, identity.source, doc["fetched_at"], doc["sha256"], True)
                if self.offline or cutoff is not None:
                    raise SourceHttpError("cache_expired", str(path))
            if cutoff is not None:
                raise SourceHttpError("historical_snapshot_unavailable", identity.url)
            if self.offline:
                raise SourceHttpError("offline_cache_miss", identity.url)
            request = urllib.request.Request(identity.url, data=identity.payload or None,
                                             method=identity.method, headers=headers or {})
            for attempt in range(self.retries + 1):
                try:
                    with self.opener(request, timeout=self.timeout) as response:
                        status = getattr(response, "status", 200)
                        if status >= 400:
                            raise urllib.error.HTTPError(identity.url, status, "upstream",
                                                         getattr(response, "headers", {}), None)
                        raw = response.read()
                    break
                except urllib.error.HTTPError as exc:
                    exc.close()
                    # Auth/permission failures and permanent client errors stop.
                    if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == self.retries:
                        raise
                    delay = float(2 ** attempt)
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            try:
                                delay = (parsedate_to_datetime(retry_after) - self.clock()).total_seconds()
                            except (ValueError, TypeError, OverflowError):
                                raise SourceHttpError("invalid_retry_after", retry_after) from exc
                        # Do not retry early when the requested wait exceeds our bound.
                        if not math.isfinite(delay) or delay > 60:
                            raise SourceHttpError("retry_after_exceeds_bound", retry_after) from exc
                    self.sleep(max(0, delay))
                except (urllib.error.URLError, TimeoutError, OSError):
                    if attempt == self.retries:
                        raise
                    self.sleep(float(2 ** attempt))
            if not raw:
                raise SourceHttpError("upstream_empty", identity.url)
            if validate is not None:
                validate(raw)
            fetched_at = timestamp(self.clock().isoformat()).isoformat()
            digest = hashlib.sha256(raw).hexdigest()
            if path is not None and validate is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                doc = dict(identity=identity.document(), source=identity.source,
                           fetched_at=fetched_at, sha256=digest,
                           response=base64.b64encode(raw).decode("ascii"))
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                                     suffix=".tmp", delete=False) as stream:
                        temporary = stream.name
                        json.dump(doc, stream, sort_keys=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if temporary and os.path.exists(temporary):
                        os.unlink(temporary)
            return HttpSnapshot(raw, identity.source, fetched_at, digest, False)


def tdx_identity(day: str) -> RequestIdentity:
    from datetime import date
    if date.fromisoformat(day).isoformat() != day:
        raise SourceHttpError("invalid_day", day)
    return RequestIdentity(source="tdx", url="https://www.tdx.com.cn/products/data/data/g4day/"
                           + day.replace("-", "") + ".zip", date=day)


def add_cache_arguments(parser) -> None:
    parser.add_argument("--cache-dir", type=Path, help="opt-in shared raw snapshot directory (e.g. data_cache/public)")
    parser.add_argument("--cache-ttl", type=float, default=86400)
    parser.add_argument("--as-of", help="require a frozen snapshot available by this timezone-aware instant")
    parser.add_argument("--offline", action="store_true", help="require a fresh verified cache hit")


def transport_from_args(args) -> SourceHttp | None:
    if args.cache_dir is None and not args.offline:
        return None
    return SourceHttp(args.cache_dir, ttl=args.cache_ttl, offline=args.offline)
