# -*- coding: utf-8 -*-
"""Database-backed strategy definitions with a legacy-compatible facade.

The five built-in strategies remain executable exactly as before. This module
adds a durable catalogue and lifecycle for future user strategies without
renaming account IDs or rewriting historical strategy references.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass


ORIGINS = ("builtin", "user")
LIFECYCLE_STATUSES = (
    "draft", "validated", "active", "paused", "retiring", "archived",
)
_TRANSITIONS = {
    "draft": frozenset({"validated", "archived"}),
    "validated": frozenset({"draft", "active", "archived"}),
    "active": frozenset({"paused", "retiring"}),
    "paused": frozenset({"active", "retiring", "archived"}),
    "retiring": frozenset({"archived"}),
    "archived": frozenset(),
}
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(_BASE, "data_cache", "paper_trading.sqlite3")


@dataclass(frozen=True)
class StrategySpec:
    id: str
    name: str
    status: str
    supports_new_cycle: bool
    origin: str = "builtin"
    implementation_key: str = ""
    description: str = ""
    metadata: dict | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self):
        payload = asdict(self)
        payload["metadata"] = dict(self.metadata or {})
        return payload


BUILTIN_STRATEGIES = (
    StrategySpec("tq_breakout", "短线日内做T", "active", True,
                 implementation_key="tq_breakout"),
    StrategySpec("trend_pullback", "趋势波段优选", "active", True,
                 implementation_key="trend_pullback"),
    StrategySpec("sector_rotation", "板块轮动先锋", "active", True,
                 implementation_key="sector_rotation"),
    StrategySpec("reported_profit_breakout", "三日策略", "active", True,
                 implementation_key="reported_profit_breakout"),
    StrategySpec("main_force_top10", "超强主力股", "active", True,
                 implementation_key="main_force_top10"),
)

# Compatibility export for modules and integrations that import the old tuple.
# New catalogue reads use list_definitions()/get().
STRATEGY_REGISTRY = BUILTIN_STRATEGIES
_BUILTIN_BY_ID = {spec.id: spec for spec in BUILTIN_STRATEGIES}


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def ensure_schema(conn):
    """Create the catalogue and seed built-ins without touching ledger rows.

    ``INSERT OR IGNORE`` is intentional: once seeded, lifecycle state is owned
    by the database and is not reset to ``active`` on every application boot.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS strategy_definitions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            origin TEXT NOT NULL CHECK(origin IN ('builtin','user')),
            lifecycle_status TEXT NOT NULL CHECK(
                lifecycle_status IN ('draft','validated','active','paused','retiring','archived')
            ),
            implementation_key TEXT NOT NULL,
            supports_new_cycle INTEGER NOT NULL DEFAULT 0 CHECK(supports_new_cycle IN (0,1)),
            description TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            sort_order INTEGER NOT NULL DEFAULT 1000,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_strategy_definitions_lifecycle
            ON strategy_definitions(lifecycle_status, origin, sort_order, id)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS strategy_definition_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT 'system',
            created_at TEXT NOT NULL,
            FOREIGN KEY(strategy_id) REFERENCES strategy_definitions(id)
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_strategy_definition_events_strategy
            ON strategy_definition_events(strategy_id, id DESC)"""
    )
    now = _now()
    for sort_order, spec in enumerate(BUILTIN_STRATEGIES):
        conn.execute(
            """INSERT OR IGNORE INTO strategy_definitions
               (id,name,origin,lifecycle_status,implementation_key,
                supports_new_cycle,description,metadata,sort_order,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (spec.id, spec.name, "builtin", spec.status,
             spec.implementation_key or spec.id, int(spec.supports_new_cycle),
             spec.description, json.dumps(spec.metadata or {}, ensure_ascii=False),
             sort_order, now, now),
        )
    return True


def _table_exists(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_definitions'"
    ).fetchone() is not None


def _row_to_spec(row):
    values = dict(row) if isinstance(row, sqlite3.Row) else {
        "id": row[0], "name": row[1], "origin": row[2],
        "lifecycle_status": row[3], "implementation_key": row[4],
        "supports_new_cycle": row[5], "description": row[6],
        "metadata": row[7], "created_at": row[8], "updated_at": row[9],
    }
    try:
        metadata = json.loads(values.get("metadata") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    return StrategySpec(
        id=values["id"], name=values["name"],
        status=values["lifecycle_status"],
        supports_new_cycle=bool(values["supports_new_cycle"]),
        origin=values["origin"], implementation_key=values["implementation_key"],
        description=values.get("description") or "", metadata=metadata,
        created_at=values.get("created_at"), updated_at=values.get("updated_at"),
    )


def _open_readonly(path):
    uri = f"file:{os.path.abspath(path).replace(os.sep, '/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _validate_filter_values(values, allowed, label):
    requested = tuple(str(value) for value in (values or ()))
    invalid = set(requested) - set(allowed)
    if invalid:
        raise ValueError(f"invalid strategy {label}: {sorted(invalid)[0]}")
    return requested


def _filter_builtins(origins, statuses, include_archived):
    return tuple(
        spec for spec in BUILTIN_STRATEGIES
        if (not origins or spec.origin in origins)
        and (not statuses or spec.status in statuses)
        and (statuses or include_archived or spec.status != "archived")
    )


def list_definitions(*, conn=None, db_path=None, origins=None, statuses=None,
                     include_archived=True):
    """Return durable definitions; fall back to built-ins before DB bootstrap."""
    origins = _validate_filter_values(origins, ORIGINS, "origin")
    statuses = _validate_filter_values(statuses, LIFECYCLE_STATUSES, "status")
    owned = False
    try:
        if conn is None:
            path = db_path or DEFAULT_DB_PATH
            if not os.path.exists(path):
                return _filter_builtins(origins, statuses, include_archived)
            conn = _open_readonly(path)
            owned = True
        if not _table_exists(conn):
            return _filter_builtins(origins, statuses, include_archived)
        clauses = []
        params = []
        if origins:
            clauses.append(f"origin IN ({','.join('?' for _ in origins)})")
            params.extend(origins)
        if statuses:
            clauses.append(f"lifecycle_status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        elif not include_archived:
            clauses.append("lifecycle_status<>'archived'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            "SELECT id,name,origin,lifecycle_status,implementation_key,"
            "supports_new_cycle,description,metadata,created_at,updated_at "
            f"FROM strategy_definitions{where} ORDER BY origin,sort_order,id", params,
        ).fetchall()
        return tuple(_row_to_spec(row) for row in rows)
    finally:
        if owned:
            conn.close()


def get(strategy_id, *, conn=None, db_path=None):
    """Return one immutable definition, or ``None`` for an unknown ID."""
    strategy_id = str(strategy_id or "").strip()
    return next((spec for spec in list_definitions(conn=conn, db_path=db_path)
                 if spec.id == strategy_id), None)


def labels(*, conn=None, db_path=None):
    """Return an ID-to-label copy for legacy consumers."""
    return {spec.id: spec.name for spec in list_definitions(conn=conn, db_path=db_path)}


def active_ids(*, conn=None, db_path=None):
    """Return definitions eligible for a new cycle in deterministic order."""
    return tuple(
        spec.id for spec in list_definitions(conn=conn, db_path=db_path, statuses=("active",))
        if spec.supports_new_cycle
    )


def statuses(*, conn=None, db_path=None):
    """Return an ID-to-lifecycle-state copy for legacy consumers."""
    return {spec.id: spec.status for spec in list_definitions(conn=conn, db_path=db_path)}


def create_user_definition(conn, strategy_id, name, *, implementation_key="",
                           description="", metadata=None, actor="system"):
    """Create a user strategy in ``draft`` state."""
    strategy_id = str(strategy_id or "").strip()
    name = str(name or "").strip()
    if not _ID_PATTERN.fullmatch(strategy_id):
        raise ValueError("strategy id must match ^[a-z][a-z0-9_]{2,63}$")
    if not name:
        raise ValueError("strategy name is required")
    if strategy_id in _BUILTIN_BY_ID:
        raise ValueError("built-in strategy id is reserved")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError("strategy metadata must be an object")
    ensure_schema(conn)
    now = _now()
    try:
        conn.execute(
            """INSERT INTO strategy_definitions
               (id,name,origin,lifecycle_status,implementation_key,
                supports_new_cycle,description,metadata,sort_order,created_at,updated_at)
               VALUES(?,?,'user','draft',?,0,?,?,1000,?,?)""",
            (strategy_id, name, str(implementation_key or strategy_id).strip(),
             str(description or "").strip(),
             json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True), now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("strategy id already exists") from exc
    conn.execute(
        """INSERT INTO strategy_definition_events
           (strategy_id,from_status,to_status,reason,actor,created_at)
           VALUES(?,NULL,'draft','definition_created',?,?)""",
        (strategy_id, str(actor or "system"), now),
    )
    return get(strategy_id, conn=conn)


def transition(conn, strategy_id, to_status, *, reason="", actor="system",
               expected_status=None):
    """Apply one validated lifecycle transition and append an audit event."""
    ensure_schema(conn)
    current = get(strategy_id, conn=conn)
    if current is None:
        raise ValueError("unknown strategy id")
    to_status = str(to_status or "").strip()
    if to_status not in LIFECYCLE_STATUSES:
        raise ValueError("invalid strategy status")
    if expected_status is not None and current.status != expected_status:
        raise ValueError(f"strategy status changed: expected {expected_status}, found {current.status}")
    if to_status == current.status:
        return current
    if to_status not in _TRANSITIONS[current.status]:
        raise ValueError(f"invalid lifecycle transition: {current.status} -> {to_status}")
    now = _now()
    # A user definition may complete its catalogue lifecycle before a later
    # adapter PR supplies an executable account profile. Only built-ins are
    # cycle-capable in PR-01.
    supports_new_cycle = int(to_status == "active" and current.origin == "builtin")
    cursor = conn.execute(
        """UPDATE strategy_definitions
           SET lifecycle_status=?,supports_new_cycle=?,updated_at=?
           WHERE id=? AND lifecycle_status=?""",
        (to_status, supports_new_cycle, now, current.id, current.status),
    )
    if cursor.rowcount != 1:
        raise ValueError("strategy status changed concurrently")
    conn.execute(
        """INSERT INTO strategy_definition_events
           (strategy_id,from_status,to_status,reason,actor,created_at)
           VALUES(?,?,?,?,?,?)""",
        (current.id, current.status, to_status, str(reason or "").strip(),
         str(actor or "system"), now),
    )
    return get(current.id, conn=conn)


def lifecycle_events(conn, strategy_id):
    """Return append-only lifecycle history for one definition."""
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT id,strategy_id,from_status,to_status,reason,actor,created_at
           FROM strategy_definition_events WHERE strategy_id=? ORDER BY id""",
        (str(strategy_id or ""),),
    ).fetchall()
    columns = ("id", "strategy_id", "from_status", "to_status", "reason", "actor", "created_at")
    return tuple(dict(zip(columns, row, strict=True)) for row in rows)
