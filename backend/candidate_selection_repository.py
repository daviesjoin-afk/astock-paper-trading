"""R36-C append-only canonical selection-report ledger.

One real authority: persistence of the canonical search-level selection report. It is
deliberately **not** part of :mod:`experiment_search_repository`, which owns only the
search control plane; selection is a business evaluation output.

There is exactly one canonical selection report per search run, because the selection
policy is pinned into the search identity and the exact evidence set is immutable once
complete. Identical retries are idempotent; a different report for the same search is a
hard conflict. No scalar score / winner / promotion columns exist.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections.abc import Mapping

import candidate_selection as CS
import paper_schema_migrations as PSM

SCHEMA_VERSION = "candidate-selection-report-ledger-v1"

#: Columns that must never appear on the selection ledger: those belong to R31 promotion
#: or would become a second metrics authority.
FORBIDDEN_SELECTION_FIELDS = frozenset({
    "score", "weighted_score", "utility", "winner", "best_candidate", "champion",
    "recommended_candidate", "promotion", "promotable", "promotion_candidate",
    "top_candidate", "rank", "ranking",
})

_REPORT_KEYS = (
    "report_version", "search_run_id", "search_input_fingerprint",
    "selection_policy", "selection_policy_fingerprint", "evidence_set_fingerprint",
    "candidate_count", "eligible_count", "advance_count", "retain_count",
    "eliminate_count", "candidates", "selection_report_key", "report_fingerprint",
)


class SelectionPersistenceError(ValueError):
    """A selection report cannot be safely persisted or read."""


def _canonical(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SelectionPersistenceError("selection_report_payload_invalid") from exc


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _reject_forbidden_fields(value, *, where: str) -> None:
    if isinstance(value, Mapping):
        present = sorted(set(value) & FORBIDDEN_SELECTION_FIELDS)
        if present:
            raise SelectionPersistenceError(
                f"selection_ledger_must_not_store:{where}:{present[0]}")
        for item in value.values():
            _reject_forbidden_fields(item, where=where)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_fields(item, where=where)


def _validate_canonical_identity(record: Mapping) -> None:
    try:
        report = record["report"]
        policy = CS.CandidateSelectionPolicy.from_projection(report["selection_policy"])
        expected_key = CS.selection_report_key(
            report_version=report["report_version"], search_run_id=report["search_run_id"],
            search_input_fingerprint=report["search_input_fingerprint"],
            selection_policy_fingerprint=report["selection_policy_fingerprint"],
            evidence_set_fingerprint=report["evidence_set_fingerprint"])
        candidates = report["candidates"]
        counts = {
            "candidate_count": len(candidates),
            "eligible_count": sum(1 for item in candidates if item["eligibility"]),
            "advance_count": sum(1 for item in candidates
                                 if item["disposition"] == "advance"),
            "retain_count": sum(1 for item in candidates
                                if item["disposition"] == "retain"),
            "eliminate_count": sum(1 for item in candidates
                                   if item["disposition"] == "eliminate"),
        }
        evidence_set = CS._sha([{
            "candidate_id": item["candidate_id"], "pit_run_key": item["pit_run_key"],
            "pit_result_fingerprint": item["pit_result_fingerprint"],
            "pit_validation_status": item["pit_validation_status"],
            "pit_result_status": item["pit_result_status"],
            "robustness_report_key": item["robustness_report_key"],
            "robustness_report_fingerprint": item["robustness_report_fingerprint"],
        } for item in record["evidence_binding"]])
        expected_fingerprint = _sha({key: value for key, value in report.items()
                                     if key != "report_fingerprint"})
    except (KeyError, TypeError, ValueError) as exc:
        raise SelectionPersistenceError("corrupt_selection_report") from exc
    if (report.get("selection_report_key") != record["selection_report_key"]
            or expected_key != record["selection_report_key"]
            or policy.fingerprint != report["selection_policy_fingerprint"]
            or policy.fingerprint != record["selection_policy_fingerprint"]
            or evidence_set != report["evidence_set_fingerprint"]
            or evidence_set != record["evidence_set_fingerprint"]
            or expected_fingerprint != report["report_fingerprint"]
            or report["report_version"] != record["report_version"]
            or report["search_run_id"] != record["search_run_id"]
            or report["search_input_fingerprint"] != record["search_input_fingerprint"]
            or any(report[name] != record[name] for name in counts)):
        raise SelectionPersistenceError("corrupt_selection_report")
    # The report's candidate set must correspond exactly to the bound evidence set:
    # a canonical-looking report whose candidates are not the bound candidates is corrupt.
    report_ids = sorted(item["candidate_id"] for item in candidates)
    binding_ids = sorted(item["candidate_id"] for item in record["evidence_binding"])
    if report_ids != binding_ids or len(set(report_ids)) != len(report_ids):
        raise SelectionPersistenceError("corrupt_selection_report")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the canonical selection ledger via the single DDL owner.

    The DDL (columns, CHECK constraints, append-only triggers) lives only in
    :func:`paper_schema_migrations.ensure_candidate_selection`; this repository never
    defines a second, divergent schema for the same table.
    """
    PSM.ensure_candidate_selection(conn)


def _identity_payload(value: Mapping) -> dict:
    payload = dict(value)
    payload.pop("created_at", None)
    payload.pop("payload_fingerprint", None)
    return payload


def _decode(row) -> dict:
    keys = ("selection_report_key", "search_run_id", "search_input_fingerprint",
            "selection_policy_fingerprint", "evidence_set_fingerprint", "report_version",
            "candidate_count", "eligible_count", "advance_count", "retain_count",
            "eliminate_count", "report_json", "evidence_binding_json", "created_at",
            "payload_fingerprint")
    record = dict(row) if isinstance(row, sqlite3.Row) else dict(zip(keys, row, strict=True))
    try:
        record["report"] = json.loads(record.pop("report_json"),
                                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        record["evidence_binding"] = json.loads(
            record.pop("evidence_binding_json"),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SelectionPersistenceError("corrupt_selection_report") from exc
    if _sha(_identity_payload(record)) != record["payload_fingerprint"]:
        raise SelectionPersistenceError("corrupt_selection_report")
    _validate_canonical_identity(record)
    return record


class CandidateSelectionRepository:
    """Append-only owner of canonical selection reports."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        ensure_schema(conn)

    def append_report(self, report: Mapping, *, evidence_binding, created_at: str | None = None,
                      conn: sqlite3.Connection | None = None) -> dict:
        if not isinstance(report, Mapping) or any(key not in report for key in _REPORT_KEYS):
            raise SelectionPersistenceError("selection_report_incomplete")
        report = dict(report)
        _reject_forbidden_fields(report, where="selection_report")
        evidence_binding = [dict(item) for item in evidence_binding]
        _reject_forbidden_fields(evidence_binding, where="evidence_binding")
        stamp = created_at or _now()
        if not isinstance(stamp, str) or not stamp:
            raise SelectionPersistenceError("selection_report_created_at_required")
        payload = {
            "selection_report_key": report["selection_report_key"],
            "search_run_id": report["search_run_id"],
            "search_input_fingerprint": report["search_input_fingerprint"],
            "selection_policy_fingerprint": report["selection_policy_fingerprint"],
            "evidence_set_fingerprint": report["evidence_set_fingerprint"],
            "report_version": report["report_version"],
            "candidate_count": report["candidate_count"],
            "eligible_count": report["eligible_count"],
            "advance_count": report["advance_count"],
            "retain_count": report["retain_count"],
            "eliminate_count": report["eliminate_count"],
            "report": report,
            "evidence_binding": evidence_binding,
            "created_at": stamp,
        }
        _validate_canonical_identity(payload)
        payload["payload_fingerprint"] = _sha(_identity_payload(payload))
        target = conn or self.conn
        existing = target.execute(
            "SELECT * FROM experiment_search_selection_reports WHERE selection_report_key=?",
            (payload["selection_report_key"],)).fetchone()
        if existing is not None:
            return self._resolve_existing(existing, payload)
        existing = target.execute(
            "SELECT * FROM experiment_search_selection_reports WHERE search_run_id=?",
            (payload["search_run_id"],)).fetchone()
        if existing is not None:
            return self._resolve_existing(existing, payload)
        columns = ("selection_report_key", "search_run_id", "search_input_fingerprint",
                   "selection_policy_fingerprint", "evidence_set_fingerprint",
                   "report_version", "candidate_count", "eligible_count", "advance_count",
                   "retain_count", "eliminate_count", "report_json", "evidence_binding_json",
                   "created_at", "payload_fingerprint")
        values = (payload["selection_report_key"], payload["search_run_id"],
                  payload["search_input_fingerprint"], payload["selection_policy_fingerprint"],
                  payload["evidence_set_fingerprint"], payload["report_version"],
                  payload["candidate_count"], payload["eligible_count"],
                  payload["advance_count"], payload["retain_count"],
                  payload["eliminate_count"], _canonical(report),
                  _canonical(evidence_binding), stamp, payload["payload_fingerprint"])
        try:
            with target:
                target.execute(
                    f"INSERT INTO experiment_search_selection_reports({','.join(columns)})"
                    f" VALUES({','.join('?' for _ in columns)})", values)
        except sqlite3.IntegrityError as exc:
            # A concurrent identical writer may have won. Re-read the winner: identical
            # payload stays idempotent, a different payload is a hard conflict.
            winner = target.execute(
                "SELECT * FROM experiment_search_selection_reports WHERE selection_report_key=?",
                (payload["selection_report_key"],)).fetchone()
            if winner is None:
                winner = target.execute(
                    "SELECT * FROM experiment_search_selection_reports WHERE search_run_id=?",
                    (payload["search_run_id"],)).fetchone()
            if winner is not None:
                return self._resolve_existing(winner, payload)
            raise SelectionPersistenceError("selection_report_conflict") from exc
        row = target.execute(
            "SELECT * FROM experiment_search_selection_reports WHERE selection_report_key=?",
            (payload["selection_report_key"],)).fetchone()
        return _decode(row)

    @staticmethod
    def _resolve_existing(existing, payload: Mapping) -> dict:
        decoded = _decode(existing)
        if (decoded["payload_fingerprint"] != payload["payload_fingerprint"]
                or decoded["selection_report_key"] != payload["selection_report_key"]):
            raise SelectionPersistenceError("selection_report_conflict")
        return decoded

    def get_selection_report(self, report_key: str) -> dict | None:
        """Exact report identity read; never "the latest report"."""
        if not isinstance(report_key, str) or len(report_key) != 64:
            raise SelectionPersistenceError("exact_selection_report_identity_required")
        row = self.conn.execute(
            "SELECT * FROM experiment_search_selection_reports WHERE selection_report_key=?",
            (report_key,)).fetchone()
        return _decode(row) if row is not None else None

    def get_selection_report_for_search(self, search_run_id: str) -> dict | None:
        """Exact one-to-one read by search run (``search_run_id`` is UNIQUE)."""
        if not isinstance(search_run_id, str) or len(search_run_id) != 64:
            raise SelectionPersistenceError("exact_search_run_identity_required")
        row = self.conn.execute(
            "SELECT * FROM experiment_search_selection_reports WHERE search_run_id=?",
            (search_run_id,)).fetchone()
        return _decode(row) if row is not None else None


__all__ = [
    "CandidateSelectionRepository",
    "FORBIDDEN_SELECTION_FIELDS",
    "SCHEMA_VERSION",
    "SelectionPersistenceError",
    "ensure_schema",
]
