"""Append-only report ledger for canonical R30 robustness evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

try:
    import robustness_contract as RC
except ImportError:  # pragma: no cover
    from . import robustness_contract as RC

SCHEMA_VERSION = "robustness-report-ledger-v1"


class RobustnessPersistenceError(ValueError):
    """A robustness report cannot be safely persisted or read."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RobustnessPersistenceError("robustness_payload_invalid") from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _identity_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    report = dict(payload["report"])
    report.pop("created_at", None)
    payload["report"] = report
    return payload


def _validate_canonical_identity(record: Mapping[str, Any]) -> None:
    try:
        plan = RC.RobustnessPlan(**record["plan"])
        report = record["report"]
        expected_report = RC.report_fingerprint(
            baseline_identity={**report["baseline_identity"], "spec": report["baseline_spec"]},
            plan_fingerprint=record["plan_fingerprint"], cases=report["cases"],
            report_version=report.get("report_version", RC.REPORT_VERSION))
        expected_report_key = _sha({"baseline_run_key": record["baseline_run_key"],
            "baseline_result_fingerprint": record["baseline_result_fingerprint"],
            "plan_fingerprint": record["plan_fingerprint"],
            "runner_version": record["runner_version"]})
    except (KeyError, TypeError, ValueError) as exc:
        raise RobustnessPersistenceError("corrupt_robustness_report") from exc
    if (plan.fingerprint != record["plan_fingerprint"]
            or report.get("plan_fingerprint") != record["plan_fingerprint"]
            or report.get("report_fingerprint") != record["report_fingerprint"]
            or expected_report != record["report_fingerprint"]
            or expected_report_key != record["report_key"]
            or report.get("baseline_run_key") != record["baseline_run_key"]
            or report.get("baseline_experiment_fingerprint")
                != record["baseline_experiment_fingerprint"]
            or report.get("baseline_result_fingerprint") != record["baseline_result_fingerprint"]):
        raise RobustnessPersistenceError("corrupt_robustness_report")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS robustness_reports (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      report_key TEXT NOT NULL UNIQUE,
      baseline_run_key TEXT NOT NULL,
      baseline_experiment_fingerprint TEXT NOT NULL,
      baseline_result_fingerprint TEXT NOT NULL,
      plan_fingerprint TEXT NOT NULL,
      report_fingerprint TEXT NOT NULL,
      plan_json TEXT NOT NULL,
      report_json TEXT NOT NULL,
      runner_version TEXT NOT NULL,
      created_at TEXT NOT NULL,
      payload_fingerprint TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_robustness_reports_recent
      ON robustness_reports(id DESC);
    CREATE INDEX IF NOT EXISTS idx_robustness_reports_baseline
      ON robustness_reports(baseline_run_key, id DESC);
    CREATE TRIGGER IF NOT EXISTS robustness_reports_no_update
      BEFORE UPDATE ON robustness_reports BEGIN SELECT RAISE(ABORT, 'append-only robustness reports'); END;
    CREATE TRIGGER IF NOT EXISTS robustness_reports_no_delete
      BEFORE DELETE ON robustness_reports BEGIN SELECT RAISE(ABORT, 'append-only robustness reports'); END;
    """)


def _decode(row: Any) -> dict[str, Any]:
    keys = ("id", "report_key", "baseline_run_key", "baseline_experiment_fingerprint",
            "baseline_result_fingerprint", "plan_fingerprint", "report_fingerprint",
            "plan_json", "report_json", "runner_version", "created_at", "payload_fingerprint")
    record = dict(row) if isinstance(row, sqlite3.Row) else dict(zip(keys, row, strict=True))
    try:
        record["plan"] = json.loads(record.pop("plan_json"),
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        record["report"] = json.loads(record.pop("report_json"),
                                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RobustnessPersistenceError("corrupt_robustness_report") from exc
    payload = {key: record[key] for key in (
        "report_key", "baseline_run_key", "baseline_experiment_fingerprint",
        "baseline_result_fingerprint", "plan_fingerprint", "report_fingerprint",
        "plan", "report", "runner_version")}
    if _sha(_identity_payload(payload)) != record["payload_fingerprint"]:
        raise RobustnessPersistenceError("corrupt_robustness_report")
    _validate_canonical_identity(record)
    return record


class RobustnessRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        ensure_schema(conn)

    def append_report(self, report: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(report, Mapping):
            raise RobustnessPersistenceError("robustness_report_invalid")
        required = ("report_key", "baseline_run_key", "baseline_experiment_fingerprint",
                    "baseline_result_fingerprint", "plan_fingerprint", "report_fingerprint",
                    "plan", "runner_version")
        if any(key not in report for key in required):
            raise RobustnessPersistenceError("robustness_report_incomplete")
        payload = {"report_key": report["report_key"],
                   "baseline_run_key": report["baseline_run_key"],
                   "baseline_experiment_fingerprint": report["baseline_experiment_fingerprint"],
                   "baseline_result_fingerprint": report["baseline_result_fingerprint"],
                   "plan_fingerprint": report["plan_fingerprint"],
                   "report_fingerprint": report["report_fingerprint"],
                   "plan": dict(report["plan"]), "report": dict(report),
                   "runner_version": report["runner_version"]}
        plan_json, report_json = _canonical(payload["plan"]), _canonical(payload["report"])
        payload_fingerprint = _sha(_identity_payload(payload))
        existing = self.conn.execute("SELECT * FROM robustness_reports WHERE report_key=?",
                                     (payload["report_key"],)).fetchone()
        if existing is not None:
            decoded = _decode(existing)
            if decoded["payload_fingerprint"] != payload_fingerprint:
                raise RobustnessPersistenceError("report_key_payload_conflict")
            return decoded
        _validate_canonical_identity(payload)
        created_at = report.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise RobustnessPersistenceError("report_created_at_required")
        try:
            with self.conn:
                cursor = self.conn.execute("""INSERT INTO robustness_reports (
                  report_key,baseline_run_key,baseline_experiment_fingerprint,
                  baseline_result_fingerprint,plan_fingerprint,report_fingerprint,
                  plan_json,report_json,runner_version,created_at,payload_fingerprint)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (payload["report_key"], payload["baseline_run_key"],
                   payload["baseline_experiment_fingerprint"],
                   payload["baseline_result_fingerprint"], payload["plan_fingerprint"],
                   payload["report_fingerprint"], plan_json, report_json,
                   payload["runner_version"], created_at, payload_fingerprint))
        except sqlite3.IntegrityError as exc:
            raise RobustnessPersistenceError("robustness_report_conflict") from exc
        row = self.conn.execute("SELECT * FROM robustness_reports WHERE id=?",
                                (cursor.lastrowid,)).fetchone()
        return _decode(row)

    def recent_reports(self, *, limit: int = 50, baseline_run_key: str | None = None) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise RobustnessPersistenceError("invalid_robustness_report_limit")
        if baseline_run_key is None:
            rows = self.conn.execute("SELECT * FROM robustness_reports ORDER BY id DESC LIMIT ?",
                                     (limit,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM robustness_reports WHERE baseline_run_key=? ORDER BY id DESC LIMIT ?",
                                     (baseline_run_key, limit)).fetchall()
        return [_decode(row) for row in rows]

    def get_report(self, report_id: int) -> dict[str, Any] | None:
        if isinstance(report_id, bool) or not isinstance(report_id, int) or report_id < 1:
            raise RobustnessPersistenceError("invalid_robustness_report_id")
        row = self.conn.execute("SELECT * FROM robustness_reports WHERE id=?", (report_id,)).fetchone()
        return _decode(row) if row is not None else None

\n