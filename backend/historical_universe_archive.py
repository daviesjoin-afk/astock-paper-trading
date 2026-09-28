"""Explicit historical security-master archive; current universe snapshots are not inputs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
import sqlite3
from typing import Any, Iterable, Mapping

try:
    import point_in_time as PIT
except ImportError:  # pragma: no cover
    from . import point_in_time as PIT

SCHEMA_VERSION = "historical-universe-archive-v1"
_CODE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")


class HistoricalUniverseArchiveError(ValueError):
    """Historical security-master evidence is invalid or conflicting."""


def _sha(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _day(value: Any, *, optional: bool = False) -> str | None:
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str):
        raise HistoricalUniverseArchiveError("invalid_security_master_date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HistoricalUniverseArchiveError("invalid_security_master_date") from exc
    if parsed.isoformat() != value:
        raise HistoricalUniverseArchiveError("invalid_security_master_date")
    return value


@dataclass(frozen=True, slots=True)
class HistoricalUniverseManifest:
    universe_archive_fingerprint: str
    coverage_start: str
    coverage_end: str
    record_count: int
    source: str
    source_revision: str
    content_hash: str
    schema_version: str = SCHEMA_VERSION

    def projection(self) -> dict[str, Any]:
        return {"universe_archive_fingerprint": self.universe_archive_fingerprint,
                "coverage_start": self.coverage_start, "coverage_end": self.coverage_end,
                "record_count": self.record_count, "source": self.source,
                "source_revision": self.source_revision, "content_hash": self.content_hash,
                "schema_version": self.schema_version}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS historical_universe_manifests (
      universe_archive_fingerprint TEXT PRIMARY KEY, coverage_start TEXT NOT NULL,
      coverage_end TEXT NOT NULL, record_count INTEGER NOT NULL, source TEXT NOT NULL,
      source_revision TEXT NOT NULL, content_hash TEXT NOT NULL, schema_version TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS historical_universe_members (
      universe_archive_fingerprint TEXT NOT NULL REFERENCES historical_universe_manifests(universe_archive_fingerprint),
      code TEXT NOT NULL, listed_from TEXT NOT NULL, delisted_at TEXT,
      security_type TEXT NOT NULL, exchange TEXT NOT NULL, observed_at TEXT NOT NULL,
      source TEXT NOT NULL, source_revision TEXT NOT NULL,
      PRIMARY KEY(universe_archive_fingerprint, code, observed_at)
    );
    CREATE INDEX IF NOT EXISTS idx_historical_universe_codes
      ON historical_universe_members(universe_archive_fingerprint, code);
    CREATE TRIGGER IF NOT EXISTS historical_universe_manifests_no_update
      BEFORE UPDATE ON historical_universe_manifests BEGIN SELECT RAISE(ABORT, 'immutable archive'); END;
    CREATE TRIGGER IF NOT EXISTS historical_universe_manifests_no_delete
      BEFORE DELETE ON historical_universe_manifests BEGIN SELECT RAISE(ABORT, 'immutable archive'); END;
    CREATE TRIGGER IF NOT EXISTS historical_universe_members_no_update
      BEFORE UPDATE ON historical_universe_members BEGIN SELECT RAISE(ABORT, 'immutable archive'); END;
    CREATE TRIGGER IF NOT EXISTS historical_universe_members_no_delete
      BEFORE DELETE ON historical_universe_members BEGIN SELECT RAISE(ABORT, 'immutable archive'); END;
    """)


class HistoricalUniverseArchiveRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        ensure_schema(conn)

    def import_historical_security_master(self, records: Iterable[Mapping[str, Any]], *,
                                          coverage_start: str, coverage_end: str,
                                          source: str, source_revision: str) -> HistoricalUniverseManifest:
        start, end = _day(coverage_start), _day(coverage_end)
        if start > end:
            raise HistoricalUniverseArchiveError("invalid_universe_archive_range")
        if not isinstance(source, str) or not source.strip() or not isinstance(source_revision, str) or not source_revision.strip():
            raise HistoricalUniverseArchiveError("historical_source_provenance_required")
        rows = []
        for record in records:
            if not isinstance(record, Mapping):
                raise HistoricalUniverseArchiveError("security_master_record_invalid")
            code = record.get("code")
            listed = _day(record.get("listed_from"))
            delisted = _day(record.get("delisted_at"), optional=True)
            observed_at = record.get("observed_at")
            parsed_observed = PIT.parse_asof(observed_at)
            security_type, exchange = record.get("security_type"), record.get("exchange")
            if (not isinstance(code, str) or not _CODE.fullmatch(code) or
                    (delisted and delisted <= listed) or parsed_observed is None or
                    not isinstance(security_type, str) or not security_type.strip() or
                    not isinstance(exchange, str) or not exchange.strip()):
                raise HistoricalUniverseArchiveError("security_master_record_invalid")
            rows.append({"code": code, "listed_from": listed, "delisted_at": delisted,
                         "security_type": security_type.strip(), "exchange": exchange.strip(),
                         "observed_at": parsed_observed.isoformat(), "source": source.strip(),
                         "source_revision": source_revision.strip()})
        if not rows:
            raise HistoricalUniverseArchiveError("empty_historical_universe_archive")
        rows.sort(key=lambda row: (row["code"], row["observed_at"]))
        if len({(row["code"], row["observed_at"]) for row in rows}) != len(rows):
            raise HistoricalUniverseArchiveError("duplicate_security_master_revision")
        content_hash = _sha(rows)
        material = {"schema_version": SCHEMA_VERSION, "coverage_start": start,
                    "coverage_end": end, "record_count": len(rows), "source": source.strip(),
                    "source_revision": source_revision.strip(), "content_hash": content_hash}
        fingerprint = _sha(material)
        existing = self.conn.execute("SELECT content_hash FROM historical_universe_manifests WHERE universe_archive_fingerprint=?", (fingerprint,)).fetchone()
        if existing:
            if existing[0] != content_hash:
                raise HistoricalUniverseArchiveError("universe_archive_identity_conflict")
            return HistoricalUniverseManifest(fingerprint, start, end, len(rows), material["source"], material["source_revision"], content_hash)
        try:
            with self.conn:
                self.conn.execute("INSERT INTO historical_universe_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                  (fingerprint, start, end, len(rows), material["source"], material["source_revision"], content_hash, SCHEMA_VERSION))
                self.conn.executemany("INSERT INTO historical_universe_members VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(fingerprint, row["code"], row["listed_from"], row["delisted_at"], row["security_type"], row["exchange"], row["observed_at"], row["source"], row["source_revision"]) for row in rows])
        except sqlite3.IntegrityError as exc:
            raise HistoricalUniverseArchiveError("immutable_universe_archive_conflict") from exc
        return HistoricalUniverseManifest(fingerprint, start, end, len(rows), material["source"], material["source_revision"], content_hash)

    def manifest(self, universe_archive_fingerprint: str) -> HistoricalUniverseManifest | None:
        row = self.conn.execute("SELECT * FROM historical_universe_manifests WHERE universe_archive_fingerprint=?", (universe_archive_fingerprint,)).fetchone()
        if row is None:
            return None
        values = dict(row) if isinstance(row, sqlite3.Row) else dict(zip(("universe_archive_fingerprint", "coverage_start", "coverage_end", "record_count", "source", "source_revision", "content_hash", "schema_version"), row, strict=True))
        material = {"schema_version": values["schema_version"], "coverage_start": values["coverage_start"], "coverage_end": values["coverage_end"], "record_count": values["record_count"], "source": values["source"], "source_revision": values["source_revision"], "content_hash": values["content_hash"]}
        if _sha(material) != universe_archive_fingerprint:
            raise HistoricalUniverseArchiveError("corrupt_universe_manifest")
        member_rows = self.conn.execute("SELECT code,listed_from,delisted_at,security_type,exchange,observed_at,source,source_revision FROM historical_universe_members WHERE universe_archive_fingerprint=? ORDER BY code,observed_at", (universe_archive_fingerprint,)).fetchall()
        keys = ("code", "listed_from", "delisted_at", "security_type", "exchange", "observed_at", "source", "source_revision")
        members = [dict(item) if isinstance(item, sqlite3.Row) else dict(zip(keys, item, strict=True)) for item in member_rows]
        if len(members) != int(values["record_count"]) or _sha(members) != values["content_hash"]:
            raise HistoricalUniverseArchiveError("corrupt_universe_archive_content")
        return HistoricalUniverseManifest(values["universe_archive_fingerprint"], values["coverage_start"], values["coverage_end"], int(values["record_count"]), values["source"], values["source_revision"], values["content_hash"], values["schema_version"])

    def membership_rows(self, universe_archive_fingerprint: str, *, asof: str) -> list[dict[str, Any]]:
        if self.manifest(universe_archive_fingerprint) is None:
            raise HistoricalUniverseArchiveError("historical_universe_archive_unavailable")
        decision = PIT.parse_asof(asof)
        if decision is None:
            raise HistoricalUniverseArchiveError("invalid_universe_asof")
        rows = self.conn.execute("SELECT code,listed_from,delisted_at,security_type,exchange,observed_at,source,source_revision FROM historical_universe_members WHERE universe_archive_fingerprint=? ORDER BY code,observed_at", (universe_archive_fingerprint,)).fetchall()
        latest_by_code = {}
        for raw in rows:
            row = dict(raw) if isinstance(raw, sqlite3.Row) else dict(zip(("code", "listed_from", "delisted_at", "security_type", "exchange", "observed_at", "source", "source_revision"), raw, strict=True))
            if PIT.is_visible_at(row["observed_at"], decision)["visible"]:
                prior = latest_by_code.get(row["code"])
                observed = PIT.parse_asof(row["observed_at"])
                prior_observed = PIT.parse_asof(prior["observed_at"]) if prior else None
                if prior is None or (observed and prior_observed and observed > prior_observed):
                    latest_by_code[row["code"]] = {"code": row["code"], "list_date": row["listed_from"],
                        "delist_date": row["delisted_at"], "security_type": row["security_type"],
                        "exchange": row["exchange"], "observed_at": row["observed_at"]}
        return [latest_by_code[code] for code in sorted(latest_by_code)]

    def membership_records(self, universe_archive_fingerprint: str) -> list[dict[str, Any]]:
        """Return archived records without PIT filtering for the canonical consumer."""
        if self.manifest(universe_archive_fingerprint) is None:
            raise HistoricalUniverseArchiveError("historical_universe_archive_unavailable")
        rows = self.conn.execute("SELECT code,listed_from,delisted_at,security_type,exchange,observed_at FROM historical_universe_members WHERE universe_archive_fingerprint=? ORDER BY code,observed_at", (universe_archive_fingerprint,)).fetchall()
        keys = ("code", "listed_from", "delisted_at", "security_type", "exchange", "observed_at")
        return [dict(row) if isinstance(row, sqlite3.Row) else dict(zip(keys, row, strict=True)) for row in rows]

    def source_projection(self, universe_archive_fingerprint: str) -> dict[str, Any] | None:
        manifest = self.manifest(universe_archive_fingerprint)
        if manifest is None:
            return None
        return {"kind": "historical_archive", "historical_membership_complete": True,
                "historical_membership_asof": manifest.coverage_end,
                "source": manifest.source, "source_revision": manifest.source_revision,
                "archive_fingerprint": manifest.universe_archive_fingerprint,
                "content_hash": manifest.content_hash}
