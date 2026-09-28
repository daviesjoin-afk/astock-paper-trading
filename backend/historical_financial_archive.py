"""Append-only owner for explicitly imported raw historical financial records.

This module never contacts a provider or treats current endpoint output as
historical fact. Importers must supply publication time and its precision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
import re
import sqlite3
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "historical-financial-archive-v1"
_CODE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")


class HistoricalFinancialArchiveError(ValueError):
    """Raw financial evidence is malformed, conflicting, or unavailable."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _period(value: Any) -> str:
    if not isinstance(value, str):
        raise HistoricalFinancialArchiveError("financial_report_period_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HistoricalFinancialArchiveError("financial_report_period_invalid") from exc
    if parsed.isoformat() != value:
        raise HistoricalFinancialArchiveError("financial_report_period_invalid")
    return value


def _publication(value: Any, precision: Any) -> tuple[str, str]:
    if precision == "date":
        return _period(value), "date"
    if precision != "instant" or not isinstance(value, str) or "T" not in value:
        raise HistoricalFinancialArchiveError("financial_publication_precision_invalid")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HistoricalFinancialArchiveError("financial_publication_instant_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalFinancialArchiveError("financial_publication_instant_invalid")
    return parsed.astimezone(timezone.utc).isoformat(), "instant"


def _record(raw: Mapping[str, Any], source: str, revision: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise HistoricalFinancialArchiveError("financial_record_invalid")
    code = raw.get("code")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise HistoricalFinancialArchiveError("financial_security_code_invalid")
    period = _period(raw.get("report_period"))
    published_at, precision = _publication(raw.get("published_at"), raw.get("publication_precision"))
    record_type = raw.get("record_type")
    if not isinstance(record_type, str) or not record_type.strip():
        raise HistoricalFinancialArchiveError("financial_record_type_invalid")
    values = raw.get("financial_fields")
    if not isinstance(values, Mapping) or not values:
        raise HistoricalFinancialArchiveError("financial_fields_missing")
    fields: dict[str, float | int] = {}
    for name, value in values.items():
        if (not isinstance(name, str) or not name.strip() or isinstance(value, bool)
                or not isinstance(value, (int, float)) or not math.isfinite(float(value))):
            raise HistoricalFinancialArchiveError("financial_field_value_invalid")
        fields[name] = int(value) if isinstance(value, float) and value.is_integer() else value
    record = {"code": code, "report_period": period, "published_at": published_at,
              "publication_precision": precision, "source": source,
              "source_revision": revision, "record_type": record_type.strip(),
              "financial_fields": dict(sorted(fields.items())),
              "schema_version": SCHEMA_VERSION}
    record["record_fingerprint"] = _sha(record)
    return record


@dataclass(frozen=True, slots=True)
class FinancialRecordRef:
    record_fingerprint: str
    code: str
    report_period: str
    published_at: str
    publication_precision: str
    source: str
    source_revision: str
    record_type: str
    financial_fields: Mapping[str, Any]
    schema_version: str = SCHEMA_VERSION

    def projection(self) -> dict[str, Any]:
        return {"record_fingerprint": self.record_fingerprint, "code": self.code,
                "report_period": self.report_period, "published_at": self.published_at,
                "publication_precision": self.publication_precision, "source": self.source,
                "source_revision": self.source_revision, "record_type": self.record_type,
                "financial_fields": dict(self.financial_fields),
                "schema_version": self.schema_version}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS historical_financial_archives (
      archive_fingerprint TEXT PRIMARY KEY, source TEXT NOT NULL,
      source_revision TEXT NOT NULL, record_count INTEGER NOT NULL,
      content_hash TEXT NOT NULL, schema_version TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS historical_financial_records (
      archive_fingerprint TEXT NOT NULL REFERENCES historical_financial_archives(archive_fingerprint),
      record_fingerprint TEXT NOT NULL, code TEXT NOT NULL, report_period TEXT NOT NULL,
      published_at TEXT NOT NULL, publication_precision TEXT NOT NULL CHECK(publication_precision IN ('date','instant')),
      source TEXT NOT NULL, source_revision TEXT NOT NULL, record_type TEXT NOT NULL,
      financial_fields_json TEXT NOT NULL, schema_version TEXT NOT NULL,
      PRIMARY KEY(archive_fingerprint, record_fingerprint)
    );
    CREATE INDEX IF NOT EXISTS idx_historical_financial_code_period
      ON historical_financial_records(archive_fingerprint, code, report_period, published_at);
    CREATE TRIGGER IF NOT EXISTS historical_financial_archives_no_update
      BEFORE UPDATE ON historical_financial_archives BEGIN SELECT RAISE(ABORT, 'append-only financial archive'); END;
    CREATE TRIGGER IF NOT EXISTS historical_financial_archives_no_delete
      BEFORE DELETE ON historical_financial_archives BEGIN SELECT RAISE(ABORT, 'append-only financial archive'); END;
    CREATE TRIGGER IF NOT EXISTS historical_financial_records_no_update
      BEFORE UPDATE ON historical_financial_records BEGIN SELECT RAISE(ABORT, 'append-only financial records'); END;
    CREATE TRIGGER IF NOT EXISTS historical_financial_records_no_delete
      BEFORE DELETE ON historical_financial_records BEGIN SELECT RAISE(ABORT, 'append-only financial records'); END;
    """)


class HistoricalFinancialArchiveRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        ensure_schema(conn)

    def import_records(self, *, source: str, source_revision: str,
                       records: Iterable[Mapping[str, Any]]) -> str:
        """Import an explicitly trusted offline export and return its identity."""
        if not isinstance(source, str) or not source.strip() or source.strip().lower() in {"current", "latest", "unknown"}:
            raise HistoricalFinancialArchiveError("financial_source_invalid")
        if not isinstance(source_revision, str) or not source_revision.strip():
            raise HistoricalFinancialArchiveError("financial_source_revision_invalid")
        normalized = [_record(row, source.strip(), source_revision.strip()) for row in records]
        if not normalized:
            raise HistoricalFinancialArchiveError("financial_archive_empty")
        normalized.sort(key=lambda row: (row["code"], row["report_period"],
                                         row["published_at"], row["record_fingerprint"]))
        if len({row["record_fingerprint"] for row in normalized}) != len(normalized):
            raise HistoricalFinancialArchiveError("financial_duplicate_record")
        content_hash = _sha(normalized)
        archive_fingerprint = _sha({"source": source.strip(),
                                    "source_revision": source_revision.strip(),
                                    "schema_version": SCHEMA_VERSION,
                                    "content_hash": content_hash,
                                    "record_count": len(normalized)})
        existing = self.get_archive(archive_fingerprint)
        if existing is not None:
            if existing["content_hash"] != content_hash or existing["record_count"] != len(normalized):
                raise HistoricalFinancialArchiveError("financial_archive_identity_conflict")
            return archive_fingerprint
        try:
            with self.conn:
                self.conn.execute("INSERT INTO historical_financial_archives VALUES(?,?,?,?,?,?)",
                                  (archive_fingerprint, source.strip(), source_revision.strip(),
                                   len(normalized), content_hash, SCHEMA_VERSION))
                self.conn.executemany("""INSERT INTO historical_financial_records
                    (archive_fingerprint,record_fingerprint,code,report_period,published_at,
                     publication_precision,source,source_revision,record_type,financial_fields_json,schema_version)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    [(archive_fingerprint, row["record_fingerprint"], row["code"], row["report_period"],
                      row["published_at"], row["publication_precision"], row["source"],
                      row["source_revision"], row["record_type"], _canonical(row["financial_fields"]),
                      row["schema_version"]) for row in normalized])
        except sqlite3.IntegrityError as exc:
            raise HistoricalFinancialArchiveError("financial_archive_conflict") from exc
        return archive_fingerprint

    def get_archive(self, fingerprint: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM historical_financial_archives WHERE archive_fingerprint=?",
                                (fingerprint,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def records(self, fingerprint: str, *, code: str | None = None) -> list[FinancialRecordRef]:
        archive = self.get_archive(fingerprint)
        if archive is None:
            return []
        if code is None:
            rows = self.conn.execute("SELECT * FROM historical_financial_records WHERE archive_fingerprint=? ORDER BY code,report_period,published_at,record_fingerprint",
                                     (fingerprint,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM historical_financial_records WHERE archive_fingerprint=? AND code=? ORDER BY report_period,published_at,record_fingerprint",
                                     (fingerprint, code)).fetchall()
        result = []
        for row in rows:
            fields = json.loads(row["financial_fields_json"], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            material = {"code": row["code"], "report_period": row["report_period"],
                        "published_at": row["published_at"], "publication_precision": row["publication_precision"],
                        "source": row["source"], "source_revision": row["source_revision"],
                        "record_type": row["record_type"], "financial_fields": fields,
                        "schema_version": row["schema_version"]}
            if _sha(material) != row["record_fingerprint"]:
                raise HistoricalFinancialArchiveError("corrupt_financial_record")
            result.append(FinancialRecordRef(row["record_fingerprint"], row["code"], row["report_period"],
                                             row["published_at"], row["publication_precision"], row["source"],
                                             row["source_revision"], row["record_type"], fields,
                                             row["schema_version"]))
        if code is None and len(result) != int(archive["record_count"]):
            raise HistoricalFinancialArchiveError("corrupt_financial_archive")
        return result
