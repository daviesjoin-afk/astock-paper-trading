"""Canonical historical session calendar derived from a pinned raw benchmark archive."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

try:
    import historical_market_archive as HMA
except ImportError:  # pragma: no cover
    from . import historical_market_archive as HMA

CALENDAR_VERSION = "historical-session-calendar-v1"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class HistoricalSessionCalendarError(ValueError):
    """A calendar cannot be issued from the requested immutable archive."""


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoricalSessionCalendar:
    calendar_fingerprint: str
    source_archive_fingerprint: str
    benchmark_symbol: str
    coverage_start: str
    coverage_end: str
    sessions: tuple[str, ...]
    session_count: int
    content_hash: str
    verification_status: str
    schema_version: str = CALENDAR_VERSION

    def projection(self) -> dict[str, Any]:
        return {"calendar_fingerprint": self.calendar_fingerprint,
                "source_archive_fingerprint": self.source_archive_fingerprint,
                "benchmark_symbol": self.benchmark_symbol,
                "coverage_start": self.coverage_start, "coverage_end": self.coverage_end,
                "sessions": list(self.sessions), "session_count": self.session_count,
                "content_hash": self.content_hash,
                "verification_status": self.verification_status,
                "schema_version": self.schema_version}


def issue_from_market_archive(repository: HMA.HistoricalMarketArchiveRepository, *,
                              archive_fingerprint: str, benchmark_symbol: str,
                              start: str, end: str) -> HistoricalSessionCalendar:
    """Issue a typed projection only from an exact raw benchmark archive identity.

    Caller mappings, weekday rules, samples, and the live calendar are not accepted.
    The archive must span the requested range and contain benchmark data throughout
    that range. Its sessions are derived only from the benchmark's raw bars.
    """
    if not _DATE.fullmatch(start or "") or not _DATE.fullmatch(end or "") or start > end:
        raise HistoricalSessionCalendarError("invalid_calendar_range")
    manifest = repository.get_manifest(archive_fingerprint)
    if manifest is None or manifest.adjustment != "raw":
        raise HistoricalSessionCalendarError("historical_market_archive_unavailable")
    if manifest.coverage_start > start or manifest.coverage_end < end:
        raise HistoricalSessionCalendarError("historical_session_calendar_incomplete_range")
    bars = repository.read_bars(archive_fingerprint, start=start, end=end,
                                symbols=(benchmark_symbol,))
    sessions = tuple(sorted({row["session"] for row in bars}))
    if not sessions:
        raise HistoricalSessionCalendarError("historical_session_calendar_incomplete_range")
    content_hash = _digest({"benchmark_symbol": benchmark_symbol, "sessions": list(sessions)})
    projection = {"schema_version": CALENDAR_VERSION,
                  "source_archive_fingerprint": archive_fingerprint,
                  "benchmark_symbol": benchmark_symbol, "coverage_start": start,
                  "coverage_end": end, "sessions": list(sessions),
                  "session_count": len(sessions), "content_hash": content_hash}
    fingerprint = _digest(projection)
    return HistoricalSessionCalendar(
        calendar_fingerprint=fingerprint, source_archive_fingerprint=archive_fingerprint,
        benchmark_symbol=benchmark_symbol, coverage_start=start, coverage_end=end,
        sessions=sessions, session_count=len(sessions), content_hash=content_hash,
        verification_status="owner_issued_from_raw_benchmark_archive",
    )
