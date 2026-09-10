# -*- coding: utf-8 -*-
"""Database-backed, immutable and versioned strategy definitions.

``strategy_definitions`` owns stable identity and lifecycle state. Immutable
definition snapshots live in ``paper_strategy_versions`` and a small head table
selects the version used by the next cycle. Running cycles pin their own version
so later edits cannot relabel signals, orders or audit evidence.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass

import strategy_dsl_schema as DSL


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
_VERSIONED_FIELDS = ("name", "implementation_key", "description", "metadata", "dsl_ast")


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
    current_version: int | None = None
    current_checksum: str | None = None
    dsl_ast: dict | None = None

    def to_dict(self):
        payload = asdict(self)
        payload["metadata"] = dict(self.metadata or {})
        payload["version_label"] = (
            f"v{self.current_version}" if self.current_version is not None else None
        )
        return payload


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    version: int
    checksum: str
    definition: dict
    created_at: str
    created_by: str
    change_note: str = ""
    cloned_from_strategy_id: str | None = None
    cloned_from_version: int | None = None
    cloned_from_checksum: str | None = None

    def to_dict(self):
        payload = asdict(self)
        payload["version_label"] = f"v{self.version}"
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

# Compatibility export for integrations that import the original tuple.
STRATEGY_REGISTRY = BUILTIN_STRATEGIES
_BUILTIN_BY_ID = {spec.id: spec for spec in BUILTIN_STRATEGIES}


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _canonical_definition(*, name, implementation_key, description="", metadata=None, dsl_ast=None):
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError("strategy metadata must be an object")
    payload = {
        "dsl_ast": None if dsl_ast is None else DSL.normalize(dsl_ast),
        "description": str(description or "").strip(),
        "implementation_key": str(implementation_key or "").strip(),
        "metadata": dict(metadata or {}),
        "name": str(name or "").strip(),
    }
    if not payload["name"]:
        raise ValueError("strategy name is required")
    if not payload["implementation_key"]:
        raise ValueError("implementation key is required")
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload, canonical, checksum


def _definitions_equal(current_definition, candidate):
    """Compare definition content while accepting the v6 missing-DSL shape.

    Migration v7 introduced ``dsl_ast``.  A v6 immutable snapshot legitimately
    lacks that key, and it means the same thing as a null DSL for native
    built-ins.  Do not create a synthetic version merely to add that spelling.
    """
    current = dict(current_definition or {})
    proposed = dict(candidate or {})
    if current.get("dsl_ast") is None:
        current.pop("dsl_ast", None)
    if proposed.get("dsl_ast") is None:
        proposed.pop("dsl_ast", None)
    return json.dumps(current, sort_keys=True, separators=(",", ":"), ensure_ascii=False) == json.dumps(
        proposed, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def _add_column(conn, table, name, definition):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if columns and name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


# PR-50：version immutability 的数据库闸门。
#
# 闸门是**默认拒绝**的：没有同一事务内、针对该 strategy_id 的一次性清除授权
# （``VERSION_PURGE_TOKEN_TABLE``）时，任何 DELETE 都被 ABORT。这样
#
#   - 裸 ``DELETE FROM paper_strategy_versions`` 一律失败——包括"lifecycle 从
#     validated 回退到 draft，但 ledger/audit 仍引用某个 version/checksum"的
#     情况（paper_signals 等表对 version 没有 FK，只靠 lifecycle 判断会漏）；
#   - 唯一合法例外是 **从未离开 draft 生命周期** 的 unused user draft 私有快照：
#     domain 层用 canonical helper ``_ever_left_draft``（生命周期历史：PR-56，
#     draft→validated→draft 的回退不算"没用过"）与 ``_historical_reference_exists``
#     （ledger/audit/执行引用）双重判定后，插入一次性授权、删除、再撤销授权，
#     全程在调用方事务内。
#   - 即便有人伪造授权，触发器仍要求 definition 是 ``origin='user' AND
#     lifecycle_status='draft'``，因此正式版本永远删不掉。
#
# 名称固定：``_install_immutable_version_trigger`` 按 SQL 内容比对，把老库上
# 残留的"无条件拒绝"旧定义**在一个显式事务内原子替换**（见该函数文档）。
IMMUTABLE_VERSION_TRIGGER = "trg_strategy_versions_no_delete"
VERSION_PURGE_TOKEN_TABLE = "paper_strategy_version_purge_tokens"
IMMUTABLE_VERSION_TRIGGER_SQL = (
    """CREATE TRIGGER IF NOT EXISTS trg_strategy_versions_no_delete
       BEFORE DELETE ON paper_strategy_versions
       WHEN NOT EXISTS (
           SELECT 1 FROM paper_strategy_version_purge_tokens t
           JOIN strategy_definitions d ON d.id=t.strategy_id
           WHERE t.strategy_id=OLD.strategy_id
             AND d.origin='user'
             AND d.lifecycle_status='draft'
       )
       BEGIN SELECT RAISE(ABORT, 'strategy versions are immutable'); END"""
)


def _normalize_trigger_sql(sql):
    text = " ".join(str(sql or "").split()).lower()
    return text.replace("if not exists ", "")


def _install_immutable_version_trigger(conn):
    """Create the immutability trigger, upgrading a legacy definition atomically.

    Older databases carry an unconditional BEFORE DELETE trigger that made the
    unused-draft purge impossible without a temporary DROP.  The replacement
    must not reopen that hole: a bare DROP + CREATE on an autocommit connection
    are two independently committed DDL statements, so an interruption between
    them would leave the database with no protection at all.

    Therefore the swap runs in one explicit ``BEGIN IMMEDIATE`` transaction when
    the caller has not already opened one (when it has, the swap inherits the
    caller's transaction and is atomic with it).  SQLite DDL is transactional,
    so no connection can ever observe a state without the trigger.
    """
    stored = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (IMMUTABLE_VERSION_TRIGGER,),
    ).fetchone()
    if stored is not None and _normalize_trigger_sql(stored[0]) == _normalize_trigger_sql(
        IMMUTABLE_VERSION_TRIGGER_SQL
    ):
        return
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"DROP TRIGGER IF EXISTS {IMMUTABLE_VERSION_TRIGGER}")
        conn.execute(IMMUTABLE_VERSION_TRIGGER_SQL)
    except Exception:
        if owned:
            conn.rollback()
        raise
    if owned:
        conn.commit()


def _create_version_schema(conn):
    _add_column(conn, "strategy_definitions", "current_version", "INTEGER")
    _add_column(conn, "strategy_definitions", "current_checksum", "TEXT")
    _add_column(conn, "strategy_definitions", "dsl_ast", "TEXT NOT NULL DEFAULT 'null'")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_strategy_versions (
            strategy_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version >= 1),
            checksum TEXT NOT NULL CHECK(
                length(checksum)=64 AND checksum NOT GLOB '*[^0-9a-f]*'
            ),
            definition_json TEXT NOT NULL,
            name TEXT NOT NULL,
            implementation_key TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            dsl_ast TEXT NOT NULL DEFAULT 'null',
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'system',
            change_note TEXT NOT NULL DEFAULT '',
            cloned_from_strategy_id TEXT,
            cloned_from_version INTEGER,
            cloned_from_checksum TEXT,
            PRIMARY KEY(strategy_id, version),
            FOREIGN KEY(strategy_id) REFERENCES strategy_definitions(id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_strategy_version_heads (
            strategy_id TEXT PRIMARY KEY,
            current_version INTEGER NOT NULL,
            current_checksum TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(strategy_id, current_version)
                REFERENCES paper_strategy_versions(strategy_id, version)
        )"""
    )
    _add_column(conn, "paper_strategy_versions", "dsl_ast", "TEXT NOT NULL DEFAULT 'null'")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_cycle_strategy_versions (
            cycle_id INTEGER NOT NULL,
            account_id TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version INTEGER NOT NULL,
            strategy_checksum TEXT NOT NULL,
            bound_at TEXT NOT NULL,
            PRIMARY KEY(cycle_id, account_id),
            FOREIGN KEY(strategy_id, strategy_version)
                REFERENCES paper_strategy_versions(strategy_id, version)
        )"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_strategy_definition_dsl_version_guard
           BEFORE UPDATE OF dsl_ast ON strategy_definitions
           WHEN NOT EXISTS (
              SELECT 1 FROM paper_strategy_versions v
              WHERE v.strategy_id=NEW.id AND v.version=NEW.current_version
                AND v.checksum=NEW.current_checksum AND v.dsl_ast=NEW.dsl_ast
           )
           BEGIN SELECT RAISE(ABORT, 'DSL changes require a new strategy version'); END"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_strategy_legacy_bindings (
            account_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            strategy_version INTEGER NOT NULL,
            strategy_checksum TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(strategy_id, strategy_version)
                REFERENCES paper_strategy_versions(strategy_id, version)
        )"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_strategy_versions_no_update
           BEFORE UPDATE ON paper_strategy_versions
           BEGIN SELECT RAISE(ABORT, 'strategy versions are immutable'); END"""
    )
    # PR-50：一次性清除授权表。触发器只认"该 strategy_id 在**同一事务内**留下一行
    # 授权、且其 definition 仍是 user+draft"的删除，这是 unused draft 私有快照的
    # 唯一合法出口。授权行不落盘（调用方事务结束即消失），也不影响其他 strategy。
    # 必须早于触发器创建：SQLite 在 CREATE TRIGGER 时就会解析触发器体里引用的表。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_strategy_version_purge_tokens (
            strategy_id TEXT PRIMARY KEY,
            FOREIGN KEY(strategy_id) REFERENCES strategy_definitions(id)
        )"""
    )
    _install_immutable_version_trigger(conn)
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_strategy_definition_version_guard
           BEFORE UPDATE OF name,implementation_key,description,metadata,
                            current_version,current_checksum
           ON strategy_definitions
           WHEN NEW.current_version IS NULL
             OR NEW.current_version <= COALESCE(OLD.current_version,0)
             OR NOT EXISTS (
                SELECT 1 FROM paper_strategy_versions v
                WHERE v.strategy_id=NEW.id AND v.version=NEW.current_version
                  AND v.checksum=NEW.current_checksum AND v.name=NEW.name
                  AND v.implementation_key=NEW.implementation_key
                  AND v.description=NEW.description AND v.metadata=NEW.metadata
             )
           BEGIN SELECT RAISE(ABORT, 'definition changes require a new strategy version'); END"""
    )


def ensure_schema(conn):
    """Create lifecycle/version schema and seed the five built-ins as v1."""
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
            dsl_ast TEXT NOT NULL DEFAULT 'null',
            sort_order INTEGER NOT NULL DEFAULT 1000,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            current_version INTEGER,
            current_checksum TEXT
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
    _create_version_schema(conn)
    now = _now()
    for sort_order, spec in enumerate(BUILTIN_STRATEGIES):
        payload, canonical, checksum = _canonical_definition(
            name=spec.name, implementation_key=spec.implementation_key or spec.id,
            description=spec.description, metadata=spec.metadata, dsl_ast=None,
        )
        conn.execute(
            """INSERT OR IGNORE INTO strategy_definitions
               (id,name,origin,lifecycle_status,implementation_key,
                supports_new_cycle,description,metadata,sort_order,created_at,updated_at,
                current_version,current_checksum)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (spec.id, payload["name"], "builtin", spec.status,
             payload["implementation_key"], int(spec.supports_new_cycle),
             payload["description"], json.dumps(payload["metadata"], ensure_ascii=False,
                                                sort_keys=True, separators=(",", ":")),
             sort_order, now, now, 1, checksum),
        )
    # PR-01 databases already contain definitions. Preserve those bytes when
    # establishing v1 instead of replacing them with process defaults.
    rows = conn.execute(
        """SELECT id,name,implementation_key,description,metadata,dsl_ast,current_version
           FROM strategy_definitions ORDER BY origin,sort_order,id"""
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row[4] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        payload, canonical, checksum = _canonical_definition(
            name=row[1], implementation_key=row[2], description=row[3], metadata=metadata,
            dsl_ast=json.loads(row[5]) if row[5] else None,
        )
        conn.execute(
            """INSERT OR IGNORE INTO paper_strategy_versions
               (strategy_id,version,checksum,definition_json,name,implementation_key,
                description,metadata,dsl_ast,created_at,created_by,change_note)
               VALUES(?,?,?,?,?,?,?,?,?,?,'system','initial immutable definition')""",
            (row[0], 1, checksum, canonical, payload["name"], payload["implementation_key"],
             payload["description"], json.dumps(payload["metadata"], ensure_ascii=False,
                                                sort_keys=True, separators=(",", ":")),
             json.dumps(payload["dsl_ast"], ensure_ascii=False, sort_keys=True, separators=(",", ":")), now),
        )
        conn.execute(
            """INSERT OR IGNORE INTO paper_strategy_version_heads
               (strategy_id,current_version,current_checksum,updated_at) VALUES(?,1,?,?)""",
            (row[0], checksum, now),
        )
        if row[6] is None:
            metadata_json = json.dumps(
                payload["metadata"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            conn.execute(
                """UPDATE strategy_definitions SET metadata=?,dsl_ast=?,current_version=1,current_checksum=?
                   WHERE id=? AND current_version IS NULL""",
                (metadata_json, json.dumps(payload["dsl_ast"], ensure_ascii=False,
                                            sort_keys=True, separators=(",", ":")), checksum, row[0]),
            )
    _seed_legacy_bindings(conn)
    return True


def _seed_legacy_bindings(conn):
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "paper_accounts" not in tables:
        return
    now = _now()
    conn.execute(
        """INSERT OR IGNORE INTO paper_strategy_legacy_bindings
           (account_id,strategy_id,strategy_version,strategy_checksum,created_at)
           SELECT a.id,a.id,h.current_version,h.current_checksum,?
           FROM paper_accounts a
           JOIN paper_strategy_version_heads h ON h.strategy_id=a.id""",
        (now,),
    )


def _table_exists(conn, table="strategy_definitions"):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone() is not None


def _row_to_spec(row):
    values = dict(row) if isinstance(row, sqlite3.Row) else {
        "id": row[0], "name": row[1], "origin": row[2],
        "lifecycle_status": row[3], "implementation_key": row[4],
        "supports_new_cycle": row[5], "description": row[6],
        "metadata": row[7], "created_at": row[8], "updated_at": row[9],
        "current_version": row[10], "current_checksum": row[11], "dsl_ast": row[12],
    }
    try:
        metadata = json.loads(values.get("metadata") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    return StrategySpec(
        id=values["id"], name=values["name"], status=values["lifecycle_status"],
        supports_new_cycle=bool(values["supports_new_cycle"]), origin=values["origin"],
        implementation_key=values["implementation_key"],
        description=values.get("description") or "", metadata=metadata,
        created_at=values.get("created_at"), updated_at=values.get("updated_at"),
        current_version=values.get("current_version"),
        current_checksum=values.get("current_checksum"),
        dsl_ast=json.loads(values.get("dsl_ast") or "null"),
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
    """Return durable definitions; fall back to built-ins before bootstrap."""
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
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(strategy_definitions)").fetchall()
        }
        version_projection = (
            "current_version,current_checksum"
            if {"current_version", "current_checksum"}.issubset(columns)
            else "NULL AS current_version,NULL AS current_checksum"
        )
        dsl_projection = "dsl_ast" if "dsl_ast" in columns else "NULL AS dsl_ast"
        rows = conn.execute(
            "SELECT id,name,origin,lifecycle_status,implementation_key,"
            "supports_new_cycle,description,metadata,created_at,updated_at,"
            f"{version_projection} "
            f",{dsl_projection} "
            f"FROM strategy_definitions{where} ORDER BY origin,sort_order,id", params,
        ).fetchall()
        return tuple(_row_to_spec(row) for row in rows)
    finally:
        if owned:
            conn.close()


def get(strategy_id, *, conn=None, db_path=None):
    strategy_id = str(strategy_id or "").strip()
    return next((spec for spec in list_definitions(conn=conn, db_path=db_path)
                 if spec.id == strategy_id), None)


def labels(*, conn=None, db_path=None):
    return {spec.id: spec.name for spec in list_definitions(conn=conn, db_path=db_path)}


def active_ids(*, conn=None, db_path=None):
    return tuple(
        spec.id for spec in list_definitions(conn=conn, db_path=db_path, statuses=("active",))
        if spec.supports_new_cycle
    )


def runtime_readiness(conn, strategy_id):
    """Compile the four runtime contracts needed before a new cycle may use a strategy.

    Built-ins retain their audited native implementation path while user
    strategies must carry a valid executable DSL AST.  This is intentionally a
    pure compilation gate: it does not place orders or mutate definitions.
    """
    spec = get(strategy_id, conn=conn)
    if spec is None:
        raise ValueError("unknown strategy id")
    version = get_version(spec.id, conn=conn)
    definition = version.definition if version else {}
    dsl_ast = definition.get("dsl_ast")
    checks = {"dsl_compiled": False, "fingerprint_valid": False,
              "risk_profile_compiled": False, "execution_profile_compiled": False}
    errors = []
    try:
        if dsl_ast is None:
            if spec.origin != "builtin":
                raise ValueError("user strategy requires a DSL AST")
        else:
            DSL.normalize(dsl_ast)
        checks["dsl_compiled"] = True
        from strategy_risk_fingerprint import compile_strategy_risk_fingerprint
        fingerprint = compile_strategy_risk_fingerprint(dsl_ast, definition.get("metadata"))
        checks["fingerprint_valid"] = True
        from strategy_risk_profiles import compile_strategy_risk_profile
        risk_profile = compile_strategy_risk_profile(fingerprint)
        checks["risk_profile_compiled"] = True
        from execution_profiles import execution_profile_for
        execution_profile = execution_profile_for(fingerprint)
        checks["execution_profile_compiled"] = True
    except Exception as exc:
        errors.append(str(exc))
        fingerprint = risk_profile = execution_profile = None
    return {
        "strategy_id": spec.id,
        "version": version.version if version else None,
        "checksum": version.checksum if version else None,
        "runtime_ready": not errors and all(checks.values()),
        "checks": checks,
        "errors": errors,
        "fingerprint": fingerprint.to_dict() if fingerprint else None,
        "risk_profile": risk_profile.to_dict() if risk_profile else None,
        "execution_profile": execution_profile,
    }


def statuses(*, conn=None, db_path=None):
    return {spec.id: spec.status for spec in list_definitions(conn=conn, db_path=db_path)}


def _row_to_version(row):
    values = dict(row) if isinstance(row, sqlite3.Row) else {
        "strategy_id": row[0], "version": row[1], "checksum": row[2],
        "definition_json": row[3], "created_at": row[4], "created_by": row[5],
        "change_note": row[6], "cloned_from_strategy_id": row[7],
        "cloned_from_version": row[8], "cloned_from_checksum": row[9],
    }
    return StrategyVersion(
        strategy_id=values["strategy_id"], version=int(values["version"]),
        checksum=values["checksum"], definition=json.loads(values["definition_json"]),
        created_at=values["created_at"], created_by=values["created_by"],
        change_note=values.get("change_note") or "",
        cloned_from_strategy_id=values.get("cloned_from_strategy_id"),
        cloned_from_version=values.get("cloned_from_version"),
        cloned_from_checksum=values.get("cloned_from_checksum"),
    )


_VERSION_SELECT = (
    "SELECT strategy_id,version,checksum,definition_json,created_at,created_by,"
    "change_note,cloned_from_strategy_id,cloned_from_version,cloned_from_checksum "
    "FROM paper_strategy_versions"
)


def get_version(strategy_id, version=None, *, checksum=None, conn=None, db_path=None):
    owned = False
    try:
        if conn is None:
            conn = _open_readonly(db_path or DEFAULT_DB_PATH)
            owned = True
        if version is None:
            head = conn.execute(
                "SELECT current_version FROM paper_strategy_version_heads WHERE strategy_id=?",
                (str(strategy_id),),
            ).fetchone()
            if head is None:
                return None
            version = int(head[0])
        row = conn.execute(
            _VERSION_SELECT + " WHERE strategy_id=? AND version=?",
            (str(strategy_id), int(version)),
        ).fetchone()
        result = _row_to_version(row) if row else None
        if result is not None and checksum is not None and result.checksum != checksum:
            raise ValueError("strategy version checksum mismatch")
        return result
    finally:
        if owned:
            conn.close()


def list_versions(strategy_id, *, conn=None, db_path=None):
    owned = False
    try:
        if conn is None:
            conn = _open_readonly(db_path or DEFAULT_DB_PATH)
            owned = True
        rows = conn.execute(
            _VERSION_SELECT + " WHERE strategy_id=? ORDER BY version",
            (str(strategy_id),),
        ).fetchall()
        return tuple(_row_to_version(row) for row in rows)
    finally:
        if owned:
            conn.close()


def create_user_definition(conn, strategy_id, name, *, implementation_key="",
                           description="", metadata=None, dsl_ast=None, actor="system",
                           _clone_source=None):
    strategy_id = str(strategy_id or "").strip()
    if not _ID_PATTERN.fullmatch(strategy_id):
        raise ValueError("strategy id must match ^[a-z][a-z0-9_]{2,63}$")
    if strategy_id in _BUILTIN_BY_ID:
        raise ValueError("built-in strategy id is reserved")
    ensure_schema(conn)
    if get(strategy_id, conn=conn) is not None:
        raise ValueError("strategy id already exists")
    payload, canonical, checksum = _canonical_definition(
        name=name, implementation_key=implementation_key or strategy_id,
        description=description, metadata=metadata, dsl_ast=dsl_ast,
    )
    now = _now()
    conn.execute(
        """INSERT INTO strategy_definitions
            (id,name,origin,lifecycle_status,implementation_key,supports_new_cycle,
            description,metadata,dsl_ast,sort_order,created_at,updated_at,current_version,current_checksum)
           VALUES(?,?,'user','draft',?,0,?,?,?,1000,?,?,1,?)""",
        (strategy_id, payload["name"], payload["implementation_key"],
         payload["description"], json.dumps(payload["metadata"], ensure_ascii=False,
                                            sort_keys=True, separators=(",", ":")),
         json.dumps(payload["dsl_ast"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
         now, now, checksum),
    )
    conn.execute(
        """INSERT INTO paper_strategy_versions
           (strategy_id,version,checksum,definition_json,name,implementation_key,
            description,metadata,dsl_ast,created_at,created_by,change_note,
            cloned_from_strategy_id,cloned_from_version,cloned_from_checksum)
           VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (strategy_id, checksum, canonical, payload["name"], payload["implementation_key"],
         payload["description"], json.dumps(payload["metadata"], ensure_ascii=False,
                                            sort_keys=True, separators=(",", ":")),
         json.dumps(payload["dsl_ast"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
         now, str(actor or "system"),
         "cloned strategy definition" if _clone_source else "initial user definition",
         _clone_source.strategy_id if _clone_source else None,
         _clone_source.version if _clone_source else None,
         _clone_source.checksum if _clone_source else None),
    )
    conn.execute(
        """INSERT INTO paper_strategy_version_heads
           (strategy_id,current_version,current_checksum,updated_at) VALUES(?,1,?,?)""",
        (strategy_id, checksum, now),
    )
    conn.execute(
        """INSERT INTO strategy_definition_events
           (strategy_id,from_status,to_status,reason,actor,created_at)
           VALUES(?,NULL,'draft','definition_created',?,?)""",
        (strategy_id, str(actor or "system"), now),
    )
    return get(strategy_id, conn=conn)


def save_definition(conn, strategy_id, changes, *, expected_version=None,
                    actor="system", change_note="",
                    risk_evidence=None, challenger_win=False):
    """Append a new immutable version and atomically advance the head.

    PR-33：``dsl_ast`` 里声明式参数的**风险方向变化**必须经过非对称风险门
    （收紧放行 / 放大需 evidence + 观察期 + 单轮上限 + Challenger 胜出）。
    这是所有策略定义写入（人工 API / 自进化 / 晋升）的唯一落库口，
    因此不提供任何绕过开关；``risk_evidence`` 与 ``challenger_win`` 由
    调用方如实申报，缺失即按 fail-closed 处理（只允许收紧）。
    """
    ensure_schema(conn)
    current = get_version(strategy_id, conn=conn)
    if current is None:
        raise ValueError("unknown strategy id")
    if expected_version is not None and current.version != int(expected_version):
        raise ValueError(
            f"strategy version changed: expected v{expected_version}, found v{current.version}"
        )
    if "dsl_ast" in (changes or {}):
        import asymmetric_risk as AR

        AR.guard_definition_change(
            conn, str(strategy_id),
            (current.definition or {}).get("dsl_ast"), changes["dsl_ast"],
            evidence_count=risk_evidence, challenger_win=challenger_win, actor=actor,
        )
    invalid = set(changes or {}) - set(_VERSIONED_FIELDS)
    if invalid:
        raise ValueError(f"non-versioned strategy field: {sorted(invalid)[0]}")
    merged = dict(current.definition)
    merged.update(dict(changes or {}))
    payload, canonical, checksum = _canonical_definition(**merged)
    if checksum == current.checksum or _definitions_equal(current.definition, payload):
        return current
    next_version = current.version + 1
    now = _now()
    metadata_json = json.dumps(
        payload["metadata"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    savepoint = "strategy_definition_save"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        conn.execute(
            """INSERT INTO paper_strategy_versions
               (strategy_id,version,checksum,definition_json,name,implementation_key,
                description,metadata,dsl_ast,created_at,created_by,change_note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(strategy_id), next_version, checksum, canonical, payload["name"],
             payload["implementation_key"], payload["description"], metadata_json,
             json.dumps(payload["dsl_ast"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             now, str(actor or "system"), str(change_note or "")),
        )
        cursor = conn.execute(
            """UPDATE paper_strategy_version_heads
               SET current_version=?,current_checksum=?,updated_at=?
               WHERE strategy_id=? AND current_version=? AND current_checksum=?""",
            (next_version, checksum, now, str(strategy_id), current.version, current.checksum),
        )
        if cursor.rowcount != 1:
            raise ValueError("strategy version changed concurrently")
        conn.execute(
            """UPDATE strategy_definitions
               SET name=?,implementation_key=?,description=?,metadata=?,dsl_ast=?,
                   current_version=?,current_checksum=?,updated_at=? WHERE id=?""",
            (payload["name"], payload["implementation_key"], payload["description"],
             metadata_json, json.dumps(payload["dsl_ast"], ensure_ascii=False,
                                       sort_keys=True, separators=(",", ":")),
             next_version, checksum, now, str(strategy_id)),
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    return get_version(strategy_id, next_version, checksum=checksum, conn=conn)


def clone_definition(conn, source_strategy_id, source_version, new_strategy_id,
                     *, name=None, actor="system"):
    """Clone one explicit immutable source version into a new user/draft v1."""
    source = get_version(source_strategy_id, source_version, conn=conn)
    if source is None:
        raise ValueError("unknown source strategy version")
    payload = dict(source.definition)
    if name is not None:
        payload["name"] = name
    clone = create_user_definition(
        conn, new_strategy_id, payload["name"],
        implementation_key=payload["implementation_key"],
        description=payload["description"], metadata=payload["metadata"],
        dsl_ast=payload.get("dsl_ast"), actor=actor,
        _clone_source=source,
    )
    return clone


def transition(conn, strategy_id, to_status, *, reason="", actor="system",
               expected_status=None):
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
    readiness = runtime_readiness(conn, current.id) if to_status == "active" else None
    if to_status == "active" and not readiness["runtime_ready"]:
        raise ValueError("strategy runtime is not ready: " + "; ".join(readiness["errors"]))
    supports_new_cycle = int(to_status == "active" and readiness["runtime_ready"])
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


def archive_definition(conn, strategy_id, *, reason="", actor="system"):
    """Move a definition through legal lifecycle edges until archived."""
    current = get(strategy_id, conn=conn)
    if current is None:
        raise ValueError("unknown strategy id")
    while current.status != "archived":
        target = {"active": "retiring", "paused": "retiring", "retiring": "archived",
                  "draft": "archived", "validated": "archived"}.get(current.status)
        if target is None:
            raise ValueError("strategy cannot be archived")
        current = transition(conn, current.id, target, reason=reason, actor=actor,
                             expected_status=current.status)
    return current


def _table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _historical_reference_exists(conn, strategy_id):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("paper_accounts", "paper_signals", "paper_signals_archive", "paper_orders",
                  "paper_orders_archive", "paper_risk_decisions", "paper_audit",
                  "paper_cycle_strategy_versions", "paper_strategy_legacy_bindings"):
        if table not in tables:
            continue
        columns = _table_columns(conn, table)
        column = "strategy_id" if "strategy_id" in columns else "account_id" if "account_id" in columns else None
        if column and conn.execute(f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (strategy_id,)).fetchone():
            return True
    return False


def _left_draft_evidence(conn, strategy_id):
    """Return the first lifecycle event proving the strategy left scratch draft.

    PR-56：``draft -> validated -> draft`` 的回退不能把策略变回"从未使用过的
    草稿"——生命周期历史本身就是"已正式使用"的证据。判定只看事件表：

    - 创建事件 ``NULL -> draft``（reason=definition_created）每个 draft 都有，
      **不算**离开 draft；
    - 任何 ``to_status`` 非 draft 的事件（draft -> validated / draft -> archived）
      算；``from_status`` 是真实非 draft 状态的事件（validated -> draft 的回退）
      也算。

    不能只看 ``current lifecycle_status``：状态可以退回 draft；也不能只数事件
    条数：定义事件对任何草稿都存在。
    """
    row = conn.execute(
        """SELECT from_status,to_status FROM strategy_definition_events
           WHERE strategy_id=?
             AND ( IFNULL(to_status,'draft') <> 'draft'
                   OR (from_status IS NOT NULL AND from_status <> ''
                       AND from_status <> 'draft') )
           ORDER BY id LIMIT 1""",
        (str(strategy_id),),
    ).fetchone()
    return None if row is None else (row[0], row[1])


def _ever_left_draft(conn, strategy_id):
    """Canonical predicate：生命周期历史是否证明该策略离开过初始 draft。

    hard delete 的形式化规则之一（``never_left_draft``）。与
    ``_historical_reference_exists`` 正交：后者看 ledger/audit 的经济与执行
    引用，本谓词只看生命周期形态。
    """
    return _left_draft_evidence(conn, strategy_id) is not None


def hard_delete_unused_draft(conn, strategy_id):
    """Physically delete only an unused user draft and its private version rows.

    Why an unused draft may be deleted while a formal version may not:

    - A **formal** version carries history — it was validated/activated, bound
      to a cycle, or referenced by signals/orders/fills/positions/audit and by
      allocation/runtime/evolution/champion/challenger consumers.  Those
      records must stay resolvable to their exact checksum forever, so the
      database refuses every delete.
    - A **user draft** that **never left the draft lifecycle** and has no
      historical reference is the single deliberate exception: it was never
      validated/activated, never entered a cycle and has no ledger/audit row
      pointing at it, so its private snapshot is pure scratch state.  Deleting
      it removes nothing anyone can still resolve.

      "Never left the draft lifecycle" is stricter than "is currently draft":
      PR-56 起用 canonical 谓词 ``_ever_left_draft``（见 ``_left_draft_evidence``）
      判定，``draft -> validated -> draft`` 的回退**不能**重新获得删除资格——
      生命周期历史本身就是正式使用的证据。多版本（v2/v3）草稿只要没离开过
      draft，仍是可删除的 scratch。

    The database trigger is **default-deny**: it aborts every delete on
    ``paper_strategy_versions`` unless the same transaction holds a one-shot
    authorization row for a definition that is still ``origin='user' AND
    lifecycle_status='draft'``.  So neither this function nor anyone else can
    delete a version by just calling ``DELETE`` — a formal version is refused by
    the database even after a lifecycle rollback, because the trigger no longer
    trusts lifecycle alone.  This function is the only place that mints the
    authorization, and it mints it only after both gates prove the draft never
    left scratch state: ``_ever_left_draft`` (lifecycle history — PR-56) and
    ``_historical_reference_exists`` (ledger/audit/execution references).
    The authorization row is
    inserted and dropped inside the caller's transaction, so it is never
    observable by another connection and disappears if anything fails.
    """
    ensure_schema(conn)
    spec = get(strategy_id, conn=conn)
    if spec is None:
        raise ValueError("unknown strategy id")
    if spec.origin != "user" or spec.status != "draft":
        raise ValueError("only unused user drafts can be hard-deleted")
    # PR-56：先证明"从未离开 draft 生命周期"，再看经济/执行引用。顺序即
    # 形式化规则：origin ∧ status ∧ never_left_draft ∧ no historical reference。
    # 回退到 draft 的策略在这里就被挡住——早于任何 purge token 铸造。
    evidence = _left_draft_evidence(conn, spec.id)
    if evidence is not None:
        raise ValueError(
            "strategy has historical references and must be archived: "
            "lifecycle history shows it already left the draft state "
            f"({evidence[0] or 'NULL'} -> {evidence[1]})"
        )
    if _historical_reference_exists(conn, spec.id):
        raise ValueError("strategy has historical references and must be archived")
    conn.execute("DELETE FROM strategy_definition_events WHERE strategy_id=?", (spec.id,))
    conn.execute("DELETE FROM paper_strategy_version_heads WHERE strategy_id=?", (spec.id,))
    conn.execute(
        f"INSERT OR REPLACE INTO {VERSION_PURGE_TOKEN_TABLE} (strategy_id) VALUES(?)",
        (spec.id,),
    )
    try:
        conn.execute("DELETE FROM paper_strategy_versions WHERE strategy_id=?", (spec.id,))
    finally:
        conn.execute(f"DELETE FROM {VERSION_PURGE_TOKEN_TABLE} WHERE strategy_id=?", (spec.id,))
    conn.execute("DELETE FROM strategy_definitions WHERE id=?", (spec.id,))
    return {"strategy_id": spec.id, "deleted": True}


def lifecycle_events(conn, strategy_id):
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT id,strategy_id,from_status,to_status,reason,actor,created_at
           FROM strategy_definition_events WHERE strategy_id=? ORDER BY id""",
        (str(strategy_id or ""),),
    ).fetchall()
    columns = ("id", "strategy_id", "from_status", "to_status", "reason", "actor", "created_at")
    return tuple(dict(zip(columns, row, strict=True)) for row in rows)


def bind_cycle_versions(conn, cycle_id, account_ids):
    """Pin current heads for a cycle; repeated calls never change a binding."""
    ensure_schema(conn)
    now = _now()
    for account_id in tuple(account_ids or ()):
        conn.execute(
            """INSERT OR IGNORE INTO paper_cycle_strategy_versions
               (cycle_id,account_id,strategy_id,strategy_version,strategy_checksum,bound_at)
               SELECT ?,?,h.strategy_id,h.current_version,h.current_checksum,?
               FROM paper_strategy_version_heads h WHERE h.strategy_id=?""",
            (int(cycle_id), str(account_id), now, str(account_id)),
        )


def stamp_for_account(conn, account_id, *, cycle_id=None):
    """Resolve the immutable version pinned to an account's current cycle."""
    account_id = str(account_id or "").strip()
    if not account_id:
        return (None, None, None)
    if cycle_id is None and _table_exists(conn, "paper_accounts"):
        row = conn.execute("SELECT cycle_id FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        cycle_id = row[0] if row else None
    row = None
    if cycle_id is not None:
        row = conn.execute(
            """SELECT strategy_id,strategy_version,strategy_checksum
               FROM paper_cycle_strategy_versions WHERE cycle_id=? AND account_id=?""",
            (int(cycle_id), account_id),
        ).fetchone()
    if row is None:
        row = conn.execute(
            """SELECT strategy_id,strategy_version,strategy_checksum
               FROM paper_strategy_legacy_bindings WHERE account_id=?""",
            (account_id,),
        ).fetchone()
    if row is None and _table_exists(conn, "strategy_definitions"):
        # PR-35：用户策略账户在未绑定周期（开户后等待分配资金/暂停）时，
        # 以其定义头作为审计戳。内置五套仍保持严格的 legacy 绑定语义。
        head = conn.execute(
            """SELECT h.strategy_id,h.current_version,h.current_checksum
               FROM paper_strategy_version_heads h
               JOIN strategy_definitions d ON d.id=h.strategy_id
               WHERE h.strategy_id=? AND d.origin='user'""",
            (account_id,),
        ).fetchone()
        if head is not None and head[1] is not None:
            return tuple(head)
    if row is None:
        raise ValueError(f"strategy version binding not found for account: {account_id}")
    return tuple(row)


def resolve_record_version(conn, record):
    """Resolve an exact stamp or the immutable v1 legacy binding; fail partial."""
    values = (
        record.get("strategy_id"), record.get("strategy_version"),
        record.get("strategy_checksum"),
    )
    present = tuple(value is not None and value != "" for value in values)
    if any(present) and not all(present):
        raise ValueError("partial strategy version stamp")
    if all(present):
        result = get_version(values[0], int(values[1]), checksum=values[2], conn=conn)
        if result is None:
            raise ValueError("unknown strategy version stamp")
        return result
    account_id = str(record.get("account_id") or "")
    binding = conn.execute(
        """SELECT strategy_id,strategy_version,strategy_checksum
           FROM paper_strategy_legacy_bindings WHERE account_id=?""",
        (account_id,),
    ).fetchone()
    if binding is None:
        raise ValueError("legacy strategy binding not found")
    result = get_version(binding[0], binding[1], checksum=binding[2], conn=conn)
    if result is None:
        raise ValueError("legacy strategy version not found")
    return result
