"""Immutable, offline historical raw OHLCV archive owner for canonical PIT replay.

Import is an explicit boundary. This module never contacts providers and refuses
adjusted series because a retrospectively adjusted price is not point-in-time raw truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
import re
import sqlite3
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "historical-market-archive-v1"
_CODE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
_TABLES = ("historical_market_manifests", "historical_market_bars")


class HistoricalMarketArchiveError(ValueError):
    """Input or persisted historical market evidence is invalid."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _date(value: Any) -> str:
    if not isinstance(value, str):
        raise HistoricalMarketArchiveError("session_must_be_iso_date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HistoricalMarketArchiveError("session_must_be_iso_date") from exc
    if parsed.isoformat() != value:
        raise HistoricalMarketArchiveError("session_must_be_iso_date")
    return value


def _bar(row: Mapping[str, Any], source: str, revision: str) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise HistoricalMarketArchiveError("bar_must_be_object")
    code = row.get("code")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise HistoricalMarketArchiveError("invalid_security_code")
    session = _date(row.get("session"))
    values = {}
    for key in ("open", "high", "low", "close", "volume", "amount"):
        raw = row.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            raise HistoricalMarketArchiveError("bar_numeric_value_invalid")
        values[key] = float(raw)
    if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
        raise HistoricalMarketArchiveError("bar_price_must_be_positive")
    if values["high"] < max(values["open"], values["low"], values["close"]):
        raise HistoricalMarketArchiveError("bar_ohlc_invalid")
    if values["low"] > min(values["open"], values["high"], values["close"]):
        raise HistoricalMarketArchiveError("bar_ohlc_invalid")
    if values["volume"] < 0 or values["amount"] < 0:
        raise HistoricalMarketArchiveError("bar_volume_amount_negative")
    return {"code": code, "session": session, **values, "source": source,
            "source_revision": revision}


@dataclass(frozen=True, slots=True)
class HistoricalMarketArchiveManifest:
    archive_fingerprint: str
    source: str
    source_revision: str
    adjustment: str
    coverage_start: str
    coverage_end: str
    symbols: tuple[str, ...]
    row_count: int
    content_hash: str
    imported_at: str | None
    schema_version: str = SCHEMA_VERSION

    def projection(self) -> dict[str, Any]:
        return {"archive_fingerprint": self.archive_fingerprint, "source": self.source,
                "source_revision": self.source_revision, "adjustment": self.adjustment,
                "coverage_start": self.coverage_start, "coverage_end": self.coverage_end,
                "symbols": list(self.symbols), "row_count": self.row_count,
                "content_hash": self.content_hash, "imported_at": self.imported_at,
                "schema_version": self.schema_version}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS historical_market_manifests (
      archive_fingerprint TEXT PRIMARY KEY, source TEXT NOT NULL, source_revision TEXT NOT NULL,
      adjustment TEXT NOT NULL CHECK(adjustment='raw'), coverage_start TEXT NOT NULL,
      coverage_end TEXT NOT NULL, symbols_json TEXT NOT NULL, row_count INTEGER NOT NULL,
      content_hash TEXT NOT NULL, imported_at TEXT, schema_version TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS historical_market_bars (
      archive_fingerprint TEXT NOT NULL REFERENCES historical_market_manifests(archive_fingerprint),
      code TEXT NOT NULL, session TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
      low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, amount REAL NOT NULL,
      source TEXT NOT NULL, source_revision TEXT NOT NULL,
      PRIMARY KEY(archive_fingerprint, code, session)
    );
    CREATE INDEX IF NOT EXISTS idx_historical_market_range
      ON historical_market_bars(archive_fingerprint, session, code);
    CREATE TRIGGER IF NOT EXISTS historical_market_manifests_no_update
      BEFORE UPDATE ON historical_market_manifests BEGIN SELECT RAISE(ABORT, 'immutable archive'); END;
    CREATE TRIGGER IF NOT EXISTS historical_market_manifests_no_delete
      BEFORE DELETE ON historical_market_manifests BEGIN SELECT RAISE(ABORT, 'immutable archive'); END;
    CREATE TRIGGER IF NOT EXISTS historical_market_bars_no_update
      BEFORE UPDATE ON historical_market_bars BEGIN SELECT RAISE(ABORT, 'immutable archive'); END;
    CREATE TRIGGER IF NOT EXISTS historical_market_bars_no_delete
      BEFORE DELETE ON historical_market_bars BEGIN SELECT RAISE(ABORT, 'immutable archive'); END;
    """)


class HistoricalMarketArchiveRepository:
    """Explicit append-only import and bounded offline reads."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        ensure_schema(conn)

    def import_raw_market_archive(
        self, rows: Iterable[Mapping[str, Any]], *, source: str, source_revision: str,
        adjustment: str, imported_at: str | None = None,
    ) -> HistoricalMarketArchiveManifest:
        if adjustment not in {"raw", "none", "unadjusted"}:
            raise HistoricalMarketArchiveError("adjusted_market_data_rejected")
        if not isinstance(source, str) or not source.strip() or not isinstance(source_revision, str) or not source_revision.strip():
            raise HistoricalMarketArchiveError("source_provenance_required")
        normalized = [_bar(item, source.strip(), source_revision.strip()) for item in rows]
        if not normalized:
            raise HistoricalMarketArchiveError("empty_market_archive")
        normalized.sort(key=lambda item: (item["session"], item["code"]))
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for item in normalized:
            key = (item["code"], item["session"])
            prior = by_key.get(key)
            if prior is not None and prior != item:
                raise HistoricalMarketArchiveError("conflicting_duplicate_bar")
            by_key[key] = item
        normalized = list(by_key.values())
        content_hash = _sha(normalized)
        material = {"schema_version": SCHEMA_VERSION, "source": source.strip(),
                    "source_revision": source_revision.strip(), "adjustment": "raw",
                    "coverage_start": min(row["session"] for row in normalized),
                    "coverage_end": max(row["session"] for row in normalized),
                    "symbols": sorted({row["code"] for row in normalized}),
                    "row_count": len(normalized), "content_hash": content_hash}
        fingerprint = _sha(material)
        manifest = HistoricalMarketArchiveManifest(
            archive_fingerprint=fingerprint, source=material["source"],
            source_revision=material["source_revision"], adjustment="raw",
            coverage_start=material["coverage_start"], coverage_end=material["coverage_end"],
            symbols=tuple(material["symbols"]), row_count=len(normalized),
            content_hash=content_hash, imported_at=imported_at,
        )
        existing = self.conn.execute(
            "SELECT content_hash FROM historical_market_manifests WHERE archive_fingerprint=?",
            (fingerprint,),
        ).fetchone()
        if existing:
            if existing[0] != content_hash:
                raise HistoricalMarketArchiveError("archive_identity_conflict")
            return self.get_manifest(fingerprint)
        try:
            with self.conn:
                self.conn.execute("""INSERT INTO historical_market_manifests VALUES
                  (?, ?, ?, 'raw', ?, ?, ?, ?, ?, ?, ?)""",
                    (fingerprint, material["source"], material["source_revision"],
                     material["coverage_start"], material["coverage_end"],
                     _canonical(material["symbols"]), len(normalized), content_hash,
                     imported_at, SCHEMA_VERSION))
                self.conn.executemany("""INSERT INTO historical_market_bars VALUES
                  (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(fingerprint, row["code"], row["session"], row["open"], row["high"],
                      row["low"], row["close"], row["volume"], row["amount"], row["source"],
                      row["source_revision"]) for row in normalized])
        except sqlite3.IntegrityError as exc:
            raise HistoricalMarketArchiveError("immutable_archive_conflict") from exc
        return manifest

    def get_manifest(self, archive_fingerprint: str) -> HistoricalMarketArchiveManifest | None:
        row = self.conn.execute("SELECT * FROM historical_market_manifests WHERE archive_fingerprint=?",
                                (archive_fingerprint,)).fetchone()
        if row is None:
            return None
        value = dict(row) if isinstance(row, sqlite3.Row) else dict(zip(
            ("archive_fingerprint", "source", "source_revision", "adjustment", "coverage_start",
             "coverage_end", "symbols_json", "row_count", "content_hash", "imported_at", "schema_version"), row, strict=True))
        manifest = HistoricalMarketArchiveManifest(
            value["archive_fingerprint"], value["source"], value["source_revision"],
            value["adjustment"], value["coverage_start"], value["coverage_end"],
            tuple(json.loads(value["symbols_json"])), int(value["row_count"]),
            value["content_hash"], value["imported_at"], value["schema_version"])
        if _sha({"schema_version": manifest.schema_version, "source": manifest.source,
                 "source_revision": manifest.source_revision, "adjustment": manifest.adjustment,
                 "coverage_start": manifest.coverage_start, "coverage_end": manifest.coverage_end,
                 "symbols": list(manifest.symbols), "row_count": manifest.row_count,
                 "content_hash": manifest.content_hash}) != manifest.archive_fingerprint:
            raise HistoricalMarketArchiveError("corrupt_market_manifest")
        bars = self.conn.execute("""SELECT code,session,open,high,low,close,volume,amount,source,source_revision
          FROM historical_market_bars WHERE archive_fingerprint=? ORDER BY session,code""",
                                 (archive_fingerprint,)).fetchall()
        keys = ("code", "session", "open", "high", "low", "close", "volume", "amount", "source", "source_revision")
        content = [dict(row) if isinstance(row, sqlite3.Row) else dict(zip(keys, row, strict=True)) for row in bars]
        if len(content) != manifest.row_count or _sha(content) != manifest.content_hash:
            raise HistoricalMarketArchiveError("corrupt_market_archive_content")
        return manifest

    def read_bars(self, archive_fingerprint: str, *, start: str, end: str,
                  symbols: Iterable[str] | None = None) -> list[dict[str, Any]]:
        _date(start); _date(end)
        if start > end:
            raise HistoricalMarketArchiveError("invalid_market_range")
        params: list[Any] = [archive_fingerprint, start, end]
        sql = "SELECT code,session,open,high,low,close,volume,amount,source,source_revision FROM historical_market_bars WHERE archive_fingerprint=? AND session>=? AND session<=?"
        codes = sorted(set(symbols or ()))
        if codes:
            sql += " AND code IN (" + ",".join("?" for _ in codes) + ")"
            params.extend(codes)
        sql += " ORDER BY session,code"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) if isinstance(row, sqlite3.Row) else dict(zip(
            ("code", "session", "open", "high", "low", "close", "volume", "amount", "source", "source_revision"), row, strict=True))
            for row in rows]
