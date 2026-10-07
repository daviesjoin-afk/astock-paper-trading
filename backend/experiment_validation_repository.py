"""Append-only persistence for canonical R29 validation runs."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

import experiment_contract as EC

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


RECORD_V1 = "experiment-validation-record-v1"
RECORD_V2 = "experiment-validation-record-v2"
LEDGER_VERSION = "experiment-validation-ledger-v2"


def _ledger_ddl(table):
    return f"""CREATE TABLE IF NOT EXISTS {table} (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_key TEXT NOT NULL UNIQUE,
      experiment_fingerprint TEXT NOT NULL,
      strategy_id TEXT,
      strategy_version INTEGER,
      strategy_checksum TEXT,
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
      payload_fingerprint TEXT NOT NULL,
      record_contract_version TEXT NOT NULL DEFAULT 'experiment-validation-record-v1',
      subject_kind TEXT NOT NULL,
      subject_json TEXT NOT NULL,
      candidate_id TEXT,
      candidate_fingerprint TEXT,
      parent_strategy_id TEXT,
      parent_strategy_version INTEGER,
      parent_strategy_checksum TEXT,
      experiment_plan_fingerprint TEXT,
      CHECK((subject_kind='strategy_version' AND strategy_id IS NOT NULL
        AND strategy_version IS NOT NULL AND strategy_checksum IS NOT NULL
        AND candidate_id IS NULL AND candidate_fingerprint IS NULL
        AND parent_strategy_id IS NULL AND parent_strategy_version IS NULL AND parent_strategy_checksum IS NULL
        AND experiment_plan_fingerprint IS NULL)
        OR (subject_kind='strategy_candidate' AND strategy_id IS NULL
        AND strategy_version IS NULL AND strategy_checksum IS NULL
        AND candidate_id IS NOT NULL AND candidate_fingerprint IS NOT NULL AND candidate_id=candidate_fingerprint
        AND parent_strategy_id IS NOT NULL AND parent_strategy_version IS NOT NULL
        AND parent_strategy_checksum IS NOT NULL AND experiment_plan_fingerprint IS NOT NULL))
    );"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Forward-only normalization; old run keys, JSON and payload hashes are copied verbatim."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(experiment_validation_runs)")]
    conn.execute("SAVEPOINT validation_subject_schema")
    try:
        if columns and "subject_kind" not in columns:
            conn.execute(_ledger_ddl("experiment_validation_runs_subject_upgrade"))
            copy_columns = list(columns)
            selected = list(columns)
            if "financial_archive_fingerprint" not in columns:
                copy_columns.append("financial_archive_fingerprint")
                selected.append("NULL")
            conn.execute(
                f"INSERT INTO experiment_validation_runs_subject_upgrade({','.join(copy_columns)},subject_kind,subject_json)"
                f" SELECT {','.join(selected)},'strategy_version',"
                "json_object('kind','strategy_version','strategy_id',strategy_id,'version',strategy_version,'checksum',strategy_checksum)"
                " FROM experiment_validation_runs")
            conn.execute("DROP TABLE experiment_validation_runs")
            conn.execute("ALTER TABLE experiment_validation_runs_subject_upgrade RENAME TO experiment_validation_runs")
        else:
            conn.execute(_ledger_ddl("experiment_validation_runs"))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_experiment_validation_recent ON experiment_validation_runs(id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_experiment_validation_identity ON experiment_validation_runs(experiment_fingerprint,strategy_id,id DESC)")
        for action in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS experiment_validation_runs_no_{action.lower()} "
                         f"BEFORE {action} ON experiment_validation_runs BEGIN SELECT RAISE(ABORT,'append-only validation ledger'); END")
        conn.execute("RELEASE validation_subject_schema")
    except BaseException:
        conn.execute("ROLLBACK TO validation_subject_schema")
        conn.execute("RELEASE validation_subject_schema")
        raise


def _decode(row: Any) -> dict[str, Any]:
    keys = ("id", "run_key", "experiment_fingerprint", "strategy_id", "strategy_version",
            "strategy_checksum", "calendar_fingerprint", "universe_archive_fingerprint",
            "financial_archive_fingerprint",
            "tradability_evidence_fingerprint", "market_archive_fingerprint", "dataset_fingerprint",
            "validation_status", "validation_evidence_json", "result_json", "folds_json",
            "runner_version", "runner_code_revision", "created_at", "payload_fingerprint",
            "record_contract_version", "subject_kind", "subject_json", "candidate_id", "candidate_fingerprint",
            "parent_strategy_id", "parent_strategy_version", "parent_strategy_checksum", "experiment_plan_fingerprint")
    record = dict(row) if isinstance(row, sqlite3.Row) else dict(zip(keys, row, strict=True))
    try:
        for field in ("validation_evidence_json", "result_json", "folds_json", "subject_json"):
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
    if record["record_contract_version"] == RECORD_V2:
        payload.update(record_contract_version=RECORD_V2, subject=record["subject"],
                       experiment_plan_fingerprint=record["experiment_plan_fingerprint"])
    elif record["record_contract_version"] != RECORD_V1:
        raise ExperimentValidationPersistenceError("corrupt_validation_run")
    try:
        if record["subject_kind"] == "strategy_version":
            expected_subject = {"kind": "strategy_version", **EC.StrategyIdentity(
                record["strategy_id"], record["strategy_version"], record["strategy_checksum"]).projection()}
            if (record["record_contract_version"] != RECORD_V1
                    or any(record[name] is not None for name in (
                        "candidate_id", "candidate_fingerprint", "parent_strategy_id",
                        "parent_strategy_version", "parent_strategy_checksum", "experiment_plan_fingerprint"))):
                raise ValueError("formal record contract mismatch")
        elif record["subject_kind"] == "strategy_candidate":
            expected_subject = _candidate_subject(record["subject"]).projection()
            parent = expected_subject["parent_strategy"]
            if (record["record_contract_version"] != RECORD_V2
                    or record["candidate_id"] != expected_subject["candidate_id"]
                    or record["candidate_fingerprint"] != expected_subject["candidate_fingerprint"]
                    or any(record[name] is not None for name in ("strategy_id", "strategy_version", "strategy_checksum"))
                    or record["parent_strategy_id"] != parent["strategy_id"]
                    or record["parent_strategy_version"] != parent["version"]
                    or record["parent_strategy_checksum"] != parent["checksum"]):
                raise ValueError("subject column mismatch")
        else:
            raise ValueError("unsupported subject")
        if record["subject"] != expected_subject:
            raise ValueError("subject mismatch")
    except (TypeError, ValueError, KeyError) as exc:
        raise ExperimentValidationPersistenceError("corrupt_validation_run") from exc
    if _sha(payload) != record["payload_fingerprint"]:
        raise ExperimentValidationPersistenceError("corrupt_validation_run")
    return record


def _candidate_subject(value):
    try:
        args = dict(value)
        if args.pop("kind") != "strategy_candidate":
            raise ValueError("wrong subject kind")
        args["parent_strategy"] = EC.StrategyIdentity(**args["parent_strategy"])
        return EC.CandidateExperimentSubject(**args)
    except (TypeError, ValueError, KeyError) as exc:
        raise ExperimentValidationPersistenceError("corrupt_validation_run") from exc


class ExperimentValidationRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        ensure_schema(conn)

    @staticmethod
    def build_run_key(experiment_fingerprint: str, owner_identities: Mapping[str, Any],
                      runner_version: str) -> str:
        return _sha({"experiment_fingerprint": experiment_fingerprint,
                     "owner_identities": dict(owner_identities),
                     "runner_version": runner_version, "schema_version": (LEDGER_VERSION
                         if "experiment_subject" in owner_identities else SCHEMA_VERSION)})

    def append_run(self, *, run_key: str, experiment_fingerprint: str,
                   strategy_id: str | None = None, strategy_version: int | None = None,
                   strategy_checksum: str | None = None, subject: Mapping | None = None,
                   experiment_plan_fingerprint: str | None = None,
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
        if subject is None:
            if experiment_plan_fingerprint is not None:
                raise ExperimentValidationPersistenceError("formal_run_must_not_have_candidate_plan")
            normalized_subject = {"kind": "strategy_version", **EC.StrategyIdentity(
                strategy_id, strategy_version, strategy_checksum).projection()}
            record_version = RECORD_V1
            extra = {"subject_kind": "strategy_version", "subject_json": _canonical(normalized_subject),
                     "record_contract_version": record_version}
        else:
            normalized_subject = _candidate_subject(subject).projection()
            if any(value is not None for value in (strategy_id, strategy_version, strategy_checksum)):
                raise ExperimentValidationPersistenceError("candidate_must_not_masquerade_as_strategy_version")
            experiment_plan_fingerprint = EC._stable_fingerprint(experiment_plan_fingerprint, name="experiment_plan_fingerprint")
            record_version = RECORD_V2
            parent = normalized_subject["parent_strategy"]
            payload.update(record_contract_version=record_version, subject=normalized_subject,
                           experiment_plan_fingerprint=experiment_plan_fingerprint)
            extra = {"subject_kind": "strategy_candidate", "subject_json": _canonical(normalized_subject),
                     "record_contract_version": record_version,
                     "candidate_id": normalized_subject["candidate_id"],
                     "candidate_fingerprint": normalized_subject["candidate_fingerprint"],
                     "parent_strategy_id": parent["strategy_id"], "parent_strategy_version": parent["version"],
                     "parent_strategy_checksum": parent["checksum"],
                     "experiment_plan_fingerprint": experiment_plan_fingerprint}
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
                values = {key: value for key, value in payload.items()
                          if key not in ("validation_evidence", "result", "folds", "subject")}
                values.update(encoded, **extra, created_at=created_at, payload_fingerprint=payload_hash)
                columns = tuple(values)
                cursor = self.conn.execute(
                    f"INSERT INTO experiment_validation_runs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns))
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
            clauses.append("subject_kind='strategy_version' AND strategy_id=?"); values.append(strategy_id)
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
