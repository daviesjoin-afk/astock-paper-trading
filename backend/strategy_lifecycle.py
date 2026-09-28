# -*- coding: utf-8 -*-
"""Canonical, exact-version strategy lifecycle state and history owner.

This module owns lifecycle vocabulary, legal transitions, formal-cycle eligibility,
and the append-only state ledger. Promotion evidence is deliberately evaluated by
``strategy_promotion`` and passed here as a fingerprinted decision.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections.abc import Mapping

STATES = (
    "draft", "candidate", "research", "validated", "shadow", "paper",
    "production_sim", "degraded", "paused", "retiring", "archived",
    "rejected", "validation_failed", "quarantined",
)

TRANSITION_TABLE = {
    "draft": frozenset({"candidate", "archived", "quarantined"}),
    "candidate": frozenset({"research", "rejected", "retiring", "quarantined"}),
    "research": frozenset({"validated", "validation_failed", "rejected", "retiring", "quarantined"}),
    "validated": frozenset({"shadow", "paused", "retiring", "quarantined"}),
    "shadow": frozenset({"paper", "paused", "retiring", "quarantined"}),
    "paper": frozenset({"production_sim", "degraded", "paused", "retiring", "quarantined"}),
    "production_sim": frozenset({"degraded", "paused", "retiring", "quarantined"}),
    "degraded": frozenset({"paused", "retiring", "quarantined"}),
    "paused": frozenset({"paper", "production_sim", "retiring", "quarantined"}),
    "retiring": frozenset({"archived", "quarantined"}),
    "archived": frozenset(),
    "rejected": frozenset(),
    "validation_failed": frozenset(),
    "quarantined": frozenset({"retiring"}),
}

FORMAL_CYCLE_STATES = frozenset({"paper", "production_sim"})
SAFETY_TRANSITION_TARGETS = frozenset({
    "paused", "quarantined", "retiring", "archived", "rejected",
})
RESUME_TRANSITION_TARGETS = frozenset({"paper", "production_sim"})
LEGACY_STATE_MAP = {
    "draft": "draft", "validated": "validated", "active": "paper",
    "paused": "paused", "retiring": "retiring", "archived": "archived",
}
_EVENT_SCHEMA = "strategy-lifecycle-event-v1"


class LifecycleError(ValueError):
    """Stable lifecycle rejection with a machine-readable reason."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def allows_formal_cycle(state: str) -> bool:
    return str(state or "") in FORMAL_CYCLE_STATES


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(
        f"PRAGMA table_info({table_name})").fetchall()}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Install lifecycle owners and idempotently migrate exact live-cycle pins."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS strategy_lifecycle_state (
      strategy_id TEXT NOT NULL,
      strategy_version INTEGER NOT NULL,
      strategy_checksum TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN
        ('draft','candidate','research','validated','shadow','paper','production_sim',
         'degraded','paused','retiring','archived','rejected','validation_failed','quarantined')),
      last_event_id INTEGER,
      updated_at TEXT NOT NULL,
      PRIMARY KEY(strategy_id,strategy_version)
    );
    CREATE TABLE IF NOT EXISTS strategy_lifecycle_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_fingerprint TEXT NOT NULL UNIQUE,
      strategy_id TEXT NOT NULL,
      strategy_version INTEGER NOT NULL,
      strategy_checksum TEXT NOT NULL,
      from_state TEXT,
      to_state TEXT NOT NULL CHECK(to_state IN
        ('draft','candidate','research','validated','shadow','paper','production_sim',
         'degraded','paused','retiring','archived','rejected','validation_failed','quarantined')),
      transition_kind TEXT NOT NULL,
      promotion_policy_version TEXT,
      promotion_decision_fingerprint TEXT,
      evidence_json TEXT NOT NULL,
      reason_code TEXT NOT NULL,
      reason_text TEXT NOT NULL,
      actor_type TEXT NOT NULL,
      actor_id TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_strategy_lifecycle_history
      ON strategy_lifecycle_events(strategy_id,strategy_version,id DESC);
    CREATE TRIGGER IF NOT EXISTS strategy_lifecycle_events_no_update
      BEFORE UPDATE ON strategy_lifecycle_events
      BEGIN SELECT RAISE(ABORT,'append-only strategy lifecycle events'); END;
    CREATE TRIGGER IF NOT EXISTS strategy_lifecycle_events_no_delete
      BEFORE DELETE ON strategy_lifecycle_events
      BEGIN SELECT RAISE(ABORT,'append-only strategy lifecycle events'); END;
    """)
    rows = conn.execute("""SELECT d.id,d.lifecycle_status,d.current_version,d.current_checksum
        FROM strategy_definitions d WHERE d.current_version IS NOT NULL
        ORDER BY d.id""").fetchall()
    for row in rows:
        current = get_state(conn, row[0], int(row[2]), checksum=row[3])
        if current is None:
            legacy = LEGACY_STATE_MAP.get(str(row[1]))
            if legacy is None:
                raise LifecycleError("legacy_lifecycle_state_unknown")
            _insert_initial(conn, row[0], int(row[2]), row[3], legacy,
                            transition_kind="legacy_import",
                            evidence={"migration_source": "legacy_strategy_registry",
                                      "legacy_imported": True,
                                      "legacy_status": str(row[1])},
                            actor_type="system", actor_id="r31_migration",
                            reason_code="legacy_state_imported",
                            reason_text="Imported existing lifecycle state without inventing intermediate states.")

    # A running cycle owns its exact immutable version. On an upgrade, that
    # version may no longer be the definition head, so importing only the head
    # leaves the live cycle without lifecycle authority. Use the legacy
    # strategy-level status as the migration source for these missing exact
    # pins; never copy the lifecycle state of the newer head.
    required_columns = {
        "paper_cycles": {"id", "status"},
        "paper_cycle_strategy_versions": {
            "cycle_id", "strategy_id", "strategy_version", "strategy_checksum"},
        "paper_strategy_versions": {"strategy_id", "version", "checksum"},
    }
    existing_tables = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if all(table in existing_tables and columns <= _table_columns(conn, table)
           for table, columns in required_columns.items()):
        pinned = conn.execute("""SELECT p.strategy_id,p.strategy_version,p.strategy_checksum,
                d.lifecycle_status,GROUP_CONCAT(DISTINCT c.id)
            FROM paper_cycle_strategy_versions p
            JOIN paper_cycles c ON c.id=p.cycle_id
            JOIN strategy_definitions d ON d.id=p.strategy_id
            WHERE c.status IN ('draft','running','paused')
            GROUP BY p.strategy_id,p.strategy_version,p.strategy_checksum,d.lifecycle_status
            ORDER BY p.strategy_id,p.strategy_version,p.strategy_checksum""").fetchall()
        for row in pinned:
            strategy_id, version, checksum, legacy_status, cycle_ids = row
            exact = conn.execute("""SELECT checksum FROM paper_strategy_versions
                WHERE strategy_id=? AND version=?""", (strategy_id, int(version))).fetchone()
            if exact is None or exact[0] != checksum:
                raise LifecycleError("live_cycle_strategy_pin_checksum_mismatch")
            if get_state(conn, strategy_id, int(version), checksum=checksum) is not None:
                continue
            legacy = LEGACY_STATE_MAP.get(str(legacy_status))
            if legacy is None:
                raise LifecycleError("legacy_lifecycle_state_unknown")
            _insert_initial(conn, strategy_id, int(version), checksum, legacy,
                transition_kind="live_cycle_pin_import",
                evidence={"migration_source": "live_cycle_exact_version_pin",
                          "legacy_status": str(legacy_status),
                          "live_cycle_ids": sorted(str(value) for value in str(cycle_ids or "").split(",") if value)},
                actor_type="system", actor_id="r31_migration",
                reason_code="live_cycle_version_state_imported",
                reason_text="Imported legacy lifecycle authority for the exact version still pinned by a live cycle.")


def _exact_version(conn, strategy_id: str, version: int, checksum: str, *, require_head=True):
    row = conn.execute("""SELECT v.checksum FROM paper_strategy_versions v
        WHERE v.strategy_id=? AND v.version=?""", (str(strategy_id), int(version))).fetchone()
    if row is None:
        raise LifecycleError("strategy_version_not_found")
    if row[0] != checksum:
        raise LifecycleError("strategy_checksum_mismatch")
    if not require_head:
        return
    head = conn.execute("""SELECT current_version,current_checksum
        FROM paper_strategy_version_heads WHERE strategy_id=?""", (str(strategy_id),)).fetchone()
    if head is None:
        raise LifecycleError("strategy_version_head_missing")
    if int(head[0]) != int(version) or head[1] != checksum:
        raise LifecycleError("strategy_version_changed")


def _is_current_head(conn, strategy_id: str, version: int, checksum: str) -> bool:
    head = conn.execute("""SELECT current_version,current_checksum
        FROM paper_strategy_version_heads WHERE strategy_id=?""", (str(strategy_id),)).fetchone()
    return bool(head and int(head[0]) == int(version) and head[1] == checksum)


def is_live_cycle_pinned(conn: sqlite3.Connection, strategy_id: str, version: int,
                         checksum: str) -> bool:
    required_columns = {
        "paper_cycles": {"id", "status"},
        "paper_cycle_strategy_versions": {
            "cycle_id", "strategy_id", "strategy_version", "strategy_checksum"},
        "paper_strategy_versions": {"strategy_id", "version", "checksum"},
    }
    tables = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if not all(table in tables and columns <= _table_columns(conn, table)
               for table, columns in required_columns.items()):
        return False
    return conn.execute("""SELECT 1 FROM paper_cycle_strategy_versions p
        JOIN paper_cycles c ON c.id=p.cycle_id
        JOIN paper_strategy_versions v ON v.strategy_id=p.strategy_id AND v.version=p.strategy_version
        WHERE p.strategy_id=? AND p.strategy_version=? AND p.strategy_checksum=?
          AND v.checksum=p.strategy_checksum AND c.status IN ('draft','running','paused')
        LIMIT 1""", (str(strategy_id), int(version), str(checksum))).fetchone() is not None


def _resume_policy(conn: sqlite3.Connection, strategy_id: str, version: int,
                   checksum: str) -> tuple[str, str | None]:
    row = conn.execute("""SELECT event_fingerprint,transition_kind,evidence_json
        FROM strategy_lifecycle_events WHERE strategy_id=? AND strategy_version=?
          AND strategy_checksum=? AND to_state='paused'
        ORDER BY id DESC LIMIT 1""", (str(strategy_id), int(version), str(checksum))).fetchone()
    if row is None:
        raise LifecycleError("paused_resume_source_unavailable")
    try:
        evidence = json.loads(row[2] or "{}")
    except (TypeError, ValueError):
        evidence = {}
    target = evidence.get("resume_state")
    if target in RESUME_TRANSITION_TARGETS:
        return str(target), str(row[0])
    # Legacy lifecycle stored only `paused`, with no prior execution mode.
    # Its explicit, conservative recovery policy is PAPER and is recorded in
    # the resume event so the choice remains auditable.
    if row[1] == "legacy_import":
        return "paper", str(row[0])
    raise LifecycleError("paused_resume_source_unavailable")


def _insert_initial(conn, strategy_id, version, checksum, state, *, transition_kind,
                    evidence, actor_type, actor_id, reason_code, reason_text):
    if state not in STATES:
        raise LifecycleError("invalid_lifecycle_state")
    material = {"schema": _EVENT_SCHEMA, "strategy_id": str(strategy_id),
                "strategy_version": int(version), "strategy_checksum": str(checksum),
                "from_state": None, "to_state": state,
                "transition_kind": transition_kind, "evidence": dict(evidence),
                "reason_code": reason_code, "reason_text": reason_text,
                "actor_type": actor_type, "actor_id": actor_id}
    fingerprint = _sha(material)
    now = _now()
    conn.execute("""INSERT OR IGNORE INTO strategy_lifecycle_events
        (event_fingerprint,strategy_id,strategy_version,strategy_checksum,from_state,to_state,
         transition_kind,promotion_policy_version,promotion_decision_fingerprint,evidence_json,
         reason_code,reason_text,actor_type,actor_id,created_at)
        VALUES(?,?,?,?,NULL,?,?,NULL,NULL,?,?,?,?,?,?)""",
        (fingerprint, str(strategy_id), int(version), str(checksum), state, transition_kind,
         _canonical(dict(evidence)), reason_code, reason_text, actor_type, actor_id, now))
    event_id = conn.execute("SELECT id FROM strategy_lifecycle_events WHERE event_fingerprint=?",
                            (fingerprint,)).fetchone()[0]
    conn.execute("""INSERT OR IGNORE INTO strategy_lifecycle_state
        (strategy_id,strategy_version,strategy_checksum,state,last_event_id,updated_at)
        VALUES(?,?,?,?,?,?)""", (str(strategy_id), int(version), str(checksum), state, event_id, now))


def initialize_version(conn: sqlite3.Connection, strategy_id: str, version: int,
                       checksum: str, *, actor_type="system", actor_id="strategy_registry") -> dict:
    """Initialize a newly created immutable version as DRAFT in its save transaction."""
    _exact_version(conn, strategy_id, version, checksum)
    _insert_initial(conn, strategy_id, version, checksum, "draft",
                    transition_kind="version_created", evidence={"initial_state": "draft"},
                    actor_type=actor_type, actor_id=actor_id,
                    reason_code="new_strategy_version", reason_text="New immutable version starts in draft.")
    result = get_state(conn, strategy_id, version, checksum=checksum)
    if result is None:
        raise LifecycleError("lifecycle_state_initialization_failed")
    return result


def get_state(conn: sqlite3.Connection, strategy_id: str, version: int,
              *, checksum: str | None = None) -> dict | None:
    sql = "SELECT * FROM strategy_lifecycle_state WHERE strategy_id=? AND strategy_version=?"
    params = [str(strategy_id), int(version)]
    if checksum is not None:
        sql += " AND strategy_checksum=?"; params.append(str(checksum))
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return dict(row) if isinstance(row, sqlite3.Row) else dict(zip(
        ("strategy_id", "strategy_version", "strategy_checksum", "state", "last_event_id", "updated_at"),
        row, strict=True))


def history(conn: sqlite3.Connection, strategy_id: str, version: int | None = None) -> list[dict]:
    sql = "SELECT * FROM strategy_lifecycle_events WHERE strategy_id=?"
    params: list[object] = [str(strategy_id)]
    if version is not None:
        sql += " AND strategy_version=?"; params.append(int(version))
    sql += " ORDER BY id"
    cursor = conn.execute(sql, params)
    names = tuple(item[0] for item in cursor.description or ())
    return [dict(row) if isinstance(row, sqlite3.Row) else dict(zip(names, row, strict=True))
            for row in cursor.fetchall()]


def has_left_initial_draft(conn: sqlite3.Connection, strategy_id: str, version: int) -> bool:
    rows = conn.execute("""SELECT from_state,to_state,transition_kind FROM strategy_lifecycle_events
        WHERE strategy_id=? AND strategy_version=? ORDER BY id""",
        (str(strategy_id), int(version))).fetchall()
    return any(str(row[1]) != "draft" or row[0] not in (None, "draft")
               or str(row[2]) not in {"version_created", "legacy_import"} for row in rows)


def transition(conn: sqlite3.Connection, *, strategy_id: str, strategy_version: int,
               strategy_checksum: str, expected_state: str, target_state: str,
               actor_type: str, actor_id: str, reason_code: str = "", reason_text: str = "",
               transition_kind: str = "promotion", promotion_decision: Mapping | None = None,
               evidence: Mapping | None = None) -> dict:
    """Append event and compare-and-swap current state atomically."""
    if actor_type == "ai":
        raise LifecycleError("ai_cannot_apply_transition")
    if actor_type not in {"human", "system"}:
        raise LifecycleError("lifecycle_actor_invalid")
    if expected_state not in STATES or target_state not in STATES:
        raise LifecycleError("invalid_lifecycle_state")
    if expected_state == "quarantined" and target_state != "retiring":
        raise LifecycleError("quarantine_release_evidence_missing")
    if target_state not in TRANSITION_TABLE[expected_state]:
        raise LifecycleError("invalid_lifecycle_transition")
    if (expected_state == "paused" and target_state in RESUME_TRANSITION_TARGETS
            and transition_kind != "resume"):
        raise LifecycleError("explicit_resume_required")
    if transition_kind == "promotion":
        decision = dict(promotion_decision or {})
        if (not decision.get("eligible")
                or decision.get("strategy_id") != str(strategy_id)
                or decision.get("strategy_version") != int(strategy_version)
                or decision.get("strategy_checksum") != str(strategy_checksum)
                or decision.get("from_state") != expected_state
                or decision.get("target_state") != target_state
                or not decision.get("decision_fingerprint")):
            raise LifecycleError("promotion_decision_required")
        decision_fp = str(decision["decision_fingerprint"])
        policy_version = str(decision.get("policy_version") or "")
    elif transition_kind == "safety":
        decision_fp, policy_version = None, None
        if target_state not in SAFETY_TRANSITION_TARGETS:
            raise LifecycleError("safety_transition_target_invalid")
        if not str(reason_code or "").strip() or not str(reason_text or "").strip():
            raise LifecycleError("safety_transition_reason_required")
    elif transition_kind == "resume":
        decision_fp, policy_version = None, "r31-explicit-resume-v1"
        if actor_type != "human":
            raise LifecycleError("resume_requires_human_actor")
        if expected_state != "paused" or target_state not in RESUME_TRANSITION_TARGETS:
            raise LifecycleError("resume_transition_invalid")
        if not str(reason_code or "").strip() or not str(reason_text or "").strip():
            raise LifecycleError("safety_transition_reason_required")
    else:
        raise LifecycleError("transition_kind_invalid")

    evidence_value = dict(evidence or {})
    if transition_kind == "safety" and target_state == "paused":
        evidence_value["resume_state"] = (expected_state
            if expected_state in RESUME_TRANSITION_TARGETS else None)
    resume_event_fingerprint = None
    if transition_kind == "resume":
        resume_target, resume_event_fingerprint = _resume_policy(
            conn, strategy_id, strategy_version, strategy_checksum)
        if target_state != resume_target:
            raise LifecycleError("paused_resume_target_mismatch")
        evidence_value.update({"resume_policy_version": policy_version,
                               "resumed_from_event": resume_event_fingerprint})
    material = {"schema": _EVENT_SCHEMA, "strategy_id": str(strategy_id),
                "strategy_version": int(strategy_version), "strategy_checksum": str(strategy_checksum),
                "from_state": expected_state, "to_state": target_state,
                "transition_kind": transition_kind, "promotion_policy_version": policy_version,
                "promotion_decision_fingerprint": decision_fp, "evidence": evidence_value,
                "reason_code": str(reason_code or ""), "reason_text": str(reason_text or ""),
                "actor_type": actor_type, "actor_id": str(actor_id or "")}
    fingerprint = _sha(material)
    now = _now()
    savepoint = "strategy_lifecycle_transition"
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        # Acquire the write reservation before reading current state so two
        # independent callers serialize and the loser observes a CAS conflict.
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"SAVEPOINT {savepoint}")
        allow_pinned_version = transition_kind in {"safety", "resume"}
        _exact_version(conn, strategy_id, strategy_version, strategy_checksum,
                       require_head=not allow_pinned_version)
        if allow_pinned_version and not _is_current_head(
                conn, strategy_id, strategy_version, strategy_checksum):
            if not is_live_cycle_pinned(conn, strategy_id, strategy_version, strategy_checksum):
                raise LifecycleError("strategy_version_changed")
        current = get_state(conn, strategy_id, strategy_version, checksum=strategy_checksum)
        if current is None:
            raise LifecycleError("lifecycle_state_not_found")
        if current["state"] != expected_state:
            raise LifecycleError("strategy_lifecycle_conflict")
        cursor = conn.execute("""INSERT INTO strategy_lifecycle_events
            (event_fingerprint,strategy_id,strategy_version,strategy_checksum,from_state,to_state,
             transition_kind,promotion_policy_version,promotion_decision_fingerprint,evidence_json,
             reason_code,reason_text,actor_type,actor_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fingerprint, str(strategy_id), int(strategy_version), str(strategy_checksum),
             expected_state, target_state, transition_kind, policy_version, decision_fp,
             _canonical(evidence_value), str(reason_code or ""), str(reason_text or ""),
             actor_type, str(actor_id or ""), now))
        event_id = int(cursor.lastrowid)
        updated = conn.execute("""UPDATE strategy_lifecycle_state
            SET state=?,last_event_id=?,updated_at=?
            WHERE strategy_id=? AND strategy_version=? AND strategy_checksum=? AND state=?""",
            (target_state, event_id, now, str(strategy_id), int(strategy_version),
             str(strategy_checksum), expected_state))
        if updated.rowcount != 1:
            raise LifecycleError("strategy_lifecycle_conflict")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if owns_transaction:
            conn.commit()
    except Exception:
        if conn.in_transaction:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except sqlite3.OperationalError:
                pass
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise
    return get_state(conn, strategy_id, strategy_version, checksum=strategy_checksum)
