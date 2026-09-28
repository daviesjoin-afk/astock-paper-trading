"""Append-only persistence for canonical R29 validation runs."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

SCHEMA_VERSION = "experiment-validation-ledger-v1"


class ExperimentValidationPersistenceError(ValueError):
    """A canonical validation run could not be safely persisted or read."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ExperimentValidationPersistenceError("validation_payload_invalid") from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS experiment_validation_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_key TEXT NOT NULL UNIQUE,
      experiment_fingerprint TEXT NOT NULL,
      strategy_id TEXT NOT NULL,
      strategy_version INTEGER NOT NULL,
      strategy_checksum TEXT NOT NULL,
      calendar_fingerprint TEXT,
      universe_archive_fingerprint TEXT,
      financial_archive_fingerprint TEXT,
      tradability_evidence_fingerprint TEXT,
      market_archive_fingerprint TEXT,
      dataset_fingerprint TEXT NOT NULL,
      validation_status TEXT NOT NULL CHECK(validation_status IN ('ready','blocked')),
      validation_evidence_json TEXT NOT NULL,
      result_json TEXT NOT NULL,
      folds_json TEXT NOT NULL,
      runner_version TEXT NOT NULL,
      runner_code_revision TEXT NOT NULL,
      created_at TEXT NOT NULL,
      payload_fingerprint TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_experiment_validation_recent
      ON experiment_validation_runs(id DESC);
    CREATE INDEX IF NOT EXISTS idx_experiment_validation_identity
      ON experiment_validation_runs(experiment_fingerprint, strategy_id, id DESC);
    CREATE TRIGGER IF NOT EXISTS experiment_validation_runs_no_update
      BEFORE UPDATE ON experiment_validation_runs BEGIN SELECT RAISE(ABORT, 'append-only validation ledger'); END;
    CREATE TRIGGER IF NOT EXISTS experiment_validation_runs_no_delete
      BEFORE DELETE ON experiment_validation_runs BEGIN SELECT RAISE(ABORT, 'append-only validation ledger'); END;
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(experiment_validation_runs)")}
    if "financial_archive_fingerprint" not in columns:
        conn.execute("ALTER TABLE experiment_validation_runs ADD COLUMN financial_archive_fingerprint TEXT")


def _decode(row: Any) -> dict[str, Any]:
    keys = ("id", "run_key", "experiment_fingerprint", "strategy_id", "strategy_version",
            "strategy_checksum", "calendar_fingerprint", "universe_archive_fingerprint",
            "financial_archive_fingerprint",
            "tradability_evidence_fingerprint", "market_archive_fingerprint", "dataset_fingerprint",
            "validation_status", "validation_evidence_json", "result_json", "folds_json",
            "runner_version", "runner_code_revision", "created_at", "payload_fingerprint")
    record = dict(row) if isinstance(row, sqlite3.Row) else dict(zip(keys, row, strict=True))
    try:
        for field in ("validation_evidence_json", "result_json", "folds_json"):
            record[field.removesuffix("_json")] = json.loads(record.pop(field), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExperimentValidationPersistenceError("corrupt_validation_run") from exc
    payload_keys = (
        "run_key", "experiment_fingerprint", "strategy_id", "strategy_version", "strategy_checksum",
        "calendar_fingerprint", "universe_archive_fingerprint", "tradability_evidence_fingerprint",
        "market_archive_fingerprint", "dataset_fingerprint", "validation_status",
        "validation_evidence", "result", "folds", "runner_version", "runner_code_revision")
    payload = {key: record[key] for key in payload_keys}
    if record.get("financial_archive_fingerprint") is not None:
        payload["financial_archive_fingerprint"] = record["financial_archive_fingerprint"]
    if _sha(payload) != record["payload_fingerprint"]:
        raise ExperimentValidationPersistenceError("corrupt_validation_run")
    return record


class ExperimentValidationRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        ensure_schema(conn)

    @staticmethod
    def build_run_key(experiment_fingerprint: str, owner_identities: Mapping[str, Any],
                      runner_version: str) -> str:
        return _sha({"experiment_fingerprint": experiment_fingerprint,
                     "owner_identities": dict(owner_identities),
                     "runner_version": runner_version, "schema_version": SCHEMA_VERSION})

    def append_run(self, *, run_key: str, experiment_fingerprint: str,
                   strategy_id: str, strategy_version: int, strategy_checksum: str,
                   calendar_fingerprint: str | None, universe_archive_fingerprint: str | None,
                   financial_archive_fingerprint: str | None = None,
                   tradability_evidence_fingerprint: str | None, market_archive_fingerprint: str | None,
                   dataset_fingerprint: str, validation_evidence: Mapping[str, Any],
                   result: Mapping[str, Any], folds: list[Mapping[str, Any]],
                   runner_version: str, runner_code_revision: str, created_at: str) -> dict[str, Any]:
        payload = {"run_key": run_key, "experiment_fingerprint": experiment_fingerprint,
                   "strategy_id": strategy_id, "strategy_version": strategy_version,
                   "strategy_checksum": strategy_checksum, "calendar_fingerprint": calendar_fingerprint,
                   "universe_archive_fingerprint": universe_archive_fingerprint,
                   "tradability_evidence_fingerprint": tradability_evidence_fingerprint,
                   "market_archive_fingerprint": market_archive_fingerprint,
                   "dataset_fingerprint": dataset_fingerprint, "validation_status": validation_evidence.get("status"),
                   "validation_evidence": dict(validation_evidence), "result": dict(result),
                   "folds": list(folds), "runner_version": runner_version,
                   "runner_code_revision": runner_code_revision}
        if financial_archive_fingerprint is not None:
            payload["financial_archive_fingerprint"] = financial_archive_fingerprint
        encoded = {"validation_evidence_json": _canonical(payload["validation_evidence"]),
                   "result_json": _canonical(payload["result"]), "folds_json": _canonical(payload["folds"])}
        payload_hash = _sha(payload)
        existing = self.conn.execute("SELECT * FROM experiment_validation_runs WHERE run_key=?", (run_key,)).fetchone()
        if existing is not None:
            item = _decode(existing)
            if item["payload_fingerprint"] != payload_hash:
                raise ExperimentValidationPersistenceError("run_key_payload_conflict")
            return item
        try:
            with self.conn:
                cursor = self.conn.execute("""INSERT INTO experiment_validation_runs
                  (run_key,experiment_fingerprint,strategy_id,strategy_version,strategy_checksum,
                   calendar_fingerprint,universe_archive_fingerprint,financial_archive_fingerprint,tradability_evidence_fingerprint,
                   market_archive_fingerprint,dataset_fingerprint,validation_status,
                   validation_evidence_json,result_json,folds_json,runner_version,runner_code_revision,
                   created_at,payload_fingerprint)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (run_key, experiment_fingerprint, strategy_id, strategy_version, strategy_checksum,
                   calendar_fingerprint, universe_archive_fingerprint, financial_archive_fingerprint,
                   tradability_evidence_fingerprint, market_archive_fingerprint, dataset_fingerprint, payload["validation_status"],
                   encoded["validation_evidence_json"], encoded["result_json"], encoded["folds_json"],
                   runner_version, runner_code_revision, created_at, payload_hash))
        except sqlite3.IntegrityError as exc:
            raise ExperimentValidationPersistenceError("validation_run_conflict") from exc
        row = self.conn.execute("SELECT * FROM experiment_validation_runs WHERE id=?", (cursor.lastrowid,)).fetchone()
        return _decode(row)

    def recent_runs(self, *, limit: int = 50, experiment_fingerprint: str | None = None,
                    strategy_id: str | None = None) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ExperimentValidationPersistenceError("invalid_validation_run_limit")
        clauses, values = [], []
        if experiment_fingerprint is not None:
            clauses.append("experiment_fingerprint=?"); values.append(experiment_fingerprint)
        if strategy_id is not None:
            clauses.append("strategy_id=?"); values.append(strategy_id)
        sql = "SELECT * FROM experiment_validation_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"; values.append(limit)
        return [_decode(row) for row in self.conn.execute(sql, values).fetchall()]

    def get_run(self, run_id: int | None = None, *, run_key: str | None = None) -> dict[str, Any] | None:
        if (run_id is None) == (run_key is None):
            raise ExperimentValidationPersistenceError("exact_validation_run_identity_required")
        if run_id is not None:
            row = self.conn.execute("SELECT * FROM experiment_validation_runs WHERE id=?", (run_id,)).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM experiment_validation_runs WHERE run_key=?", (run_key,)).fetchone()
        return _decode(row) if row is not None else None
