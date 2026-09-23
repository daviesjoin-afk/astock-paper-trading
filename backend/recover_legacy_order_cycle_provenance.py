#!/usr/bin/env python3
"""Plan and apply a reviewed recovery of provable legacy order cycle IDs.

Dry-run is the default. This tool never infers provenance from the current
account binding or from timestamps alone. Apply requires a saved, unchanged
plan and keeps the canonical immutability trigger in the same transaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import execution_verification as EV  # noqa: E402
import paper_schema_migrations as PSM  # noqa: E402

PLAN_VERSION = "legacy-order-cycle-plan-v1"
ORDER_IMMUTABLE_TRIGGER = "trg_paper_orders_cycle_provenance_immutable"
PROTECTED_TABLES = (
    "paper_orders",
    "paper_fills",
    "paper_position_lots",
    "paper_signals",
    "paper_risk_decisions",
    "paper_capital_reservations",
    "paper_accounts",
    "paper_nav",
    "paper_cycles",
    "paper_archives",
    "paper_orders_archive",
)
ARCHIVE_IDENTITY_FIELDS = (
    "id", "account_id", "side", "code", "qty", "created_at",
    "executed_at", "status", "signal_id", "order_type", "origin",
)


class RecoveryError(RuntimeError):
    """Raised when a plan or the ledger no longer satisfies its contract."""


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.Error:
        return []


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    }


def _as_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(row)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str, allow_nan=False,
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _table_fingerprint(conn: sqlite3.Connection, table: str) -> dict[str, Any] | None:
    columns = _columns(conn, table)
    if not columns:
        return None
    if table == "paper_orders":
        # Cycle ID is the only allowed data difference.
        columns = [column for column in columns if column != "cycle_id"]
    pk = [
        (int(row[5]), str(row[1]))
        for row in conn.execute(f'PRAGMA table_info("{table}")') if int(row[5] or 0)
    ]
    order_by = ",".join(f'"{name}"' for _, name in sorted(pk))
    if not order_by:
        order_by = ",".join(f'"{name}"' for name in columns)
    select = ",".join(f'"{name}"' for name in columns)
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(
        f'SELECT {select} FROM "{table}" ORDER BY {order_by}'
    ):
        digest.update(_canonical_json(tuple(row)).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return {"rows": count, "sha256": digest.hexdigest(), "columns": columns}


def _protected_fingerprints(conn: sqlite3.Connection) -> dict[str, Any]:
    existing = _tables(conn)
    return {
        table: fingerprint
        for table in PROTECTED_TABLES
        if table in existing
        for fingerprint in [_table_fingerprint(conn, table)]
    }


def _schema_identity(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    schema_rows = [
        tuple(row) for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger','view') ORDER BY type,name"
        )
    ]
    versions = []
    if "schema_version" in tables:
        cols = set(_columns(conn, "schema_version"))
        if {"db_name", "version"}.issubset(cols):
            versions = [tuple(row) for row in conn.execute(
                "SELECT db_name,version FROM schema_version "
                "WHERE db_name='paper_trading' ORDER BY db_name"
            )]
    return {
        "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "schema_version": versions,
        "schema_sha256": _sha256(_canonical_json(schema_rows).encode("utf-8")),
    }


def _cycle_rows(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    if not {"id", "cycle_key"}.issubset(_columns(conn, "paper_cycles")):
        raise RecoveryError("paper_cycles is missing id/cycle_key")
    return {
        int(row["id"]): _as_dict(row)
        for row in conn.execute("SELECT * FROM paper_cycles ORDER BY id")
    }


def _matches_archive_identity(order: dict[str, Any], archived: dict[str, Any]) -> bool:
    return all(
        field not in order or field not in archived or order.get(field) == archived.get(field)
        for field in ARCHIVE_IDENTITY_FIELDS
    )


def _build_archive_index(conn: sqlite3.Connection,
                         cycles: dict[int, dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    index: dict[int, list[dict[str, Any]]] = {}
    if "paper_archives" not in _tables(conn) or not {
        "cycle_id", "cycle_key", "snapshot", "created_at"
    }.issubset(_columns(conn, "paper_archives")):
        return index
    for row in conn.execute(
        "SELECT id,cycle_id,cycle_key,snapshot,created_at FROM paper_archives "
        "WHERE cycle_id IS NOT NULL ORDER BY id"
    ):
        archive = _as_dict(row)
        cycle_id = int(archive["cycle_id"])
        cycle = cycles.get(cycle_id)
        if cycle is None or str(cycle.get("cycle_key") or "") != str(archive["cycle_key"] or ""):
            continue
        try:
            snapshot = json.loads(archive["snapshot"] or "{}")
        except (TypeError, ValueError):
            continue
        if snapshot.get("_archive_format") != "compact-ledger-v2":
            continue
        for archived_order in snapshot.get("paper_orders", ()):
            if isinstance(archived_order, dict) and archived_order.get("id") is not None:
                index.setdefault(int(archived_order["id"]), []).append({
                    "archive_id": int(archive["id"]),
                    "cycle_id": cycle_id,
                    "created_at": archive.get("created_at"),
                    "order": archived_order,
                })
    return index


def _archive_evidence(conn: sqlite3.Connection, order: dict[str, Any],
                      cycles: dict[int, dict[str, Any]],
                      archive_index: dict[int, list[dict[str, Any]]],
                      has_order_archive: bool
                      ) -> tuple[list[dict[str, Any]], list[str]]:
    evidence: list[dict[str, Any]] = []
    invalid: list[str] = []
    for archived in archive_index.get(int(order["id"]), ()):
        cycle_id = int(archived["cycle_id"])
        if not _matches_archive_identity(order, archived["order"]):
            invalid.append(f"archive:{archived['archive_id']}:order_identity_mismatch")
            continue
        evidence.append({
            "type": "cycle_owned_archive_snapshot",
            "source_id": int(archived["archive_id"]),
            "cycle_id": cycle_id,
        })
    if has_order_archive:
        row = conn.execute(
            "SELECT * FROM paper_orders_archive WHERE id=? AND cycle_id IS NOT NULL",
            (order["id"],),
        ).fetchone()
        if row is not None:
            archived_order = _as_dict(row)
            cycle_id = int(archived_order["cycle_id"])
            if cycle_id in cycles:
                if _matches_archive_identity(order, archived_order):
                    evidence.append({
                        "type": "cycle_owned_order_archive_row",
                        "source_id": int(order["id"]),
                        "cycle_id": cycle_id,
                    })
                else:
                    invalid.append("paper_orders_archive:order_identity_mismatch")
    return evidence, invalid


def _build_lot_index(conn: sqlite3.Connection,
                     orders: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    target_ids = {int(order["id"]) for order in orders}
    index: dict[int, list[dict[str, Any]]] = {}
    if not target_ids or "paper_position_lots" not in _tables(conn):
        return index
    for row in conn.execute(
        "SELECT * FROM paper_position_lots WHERE source_order_id IS NOT NULL ORDER BY id"
    ):
        item = _as_dict(row)
        source_id = int(item["source_order_id"])
        if source_id in target_ids:
            index.setdefault(source_id, []).append(item)
    return index


def _lot_evidence(conn: sqlite3.Connection, order: dict[str, Any],
                  cycles: dict[int, dict[str, Any]],
                  lot_index: dict[int, list[dict[str, Any]]],
                  fills_by_order: dict[int, list[dict[str, Any]]]
                  ) -> tuple[list[dict[str, Any]], list[str]]:
    lots = lot_index.get(int(order["id"]), [])
    if not lots:
        return [], []
    invalid: list[str] = []
    if len(lots) != 1:
        return [], ["lot_source_order_not_unique"]
    lot = lots[0]
    try:
        cycle_id = int(lot.get("cycle_id"))
    except (TypeError, ValueError):
        return [], ["lot_cycle_id_missing"]
    if cycle_id not in cycles:
        return [], ["lot_cycle_id_unknown"]
    if str(lot.get("account_id") or "") != str(order.get("account_id") or ""):
        invalid.append("lot_order_account_mismatch")
    if str(lot.get("code") or "") != str(order.get("code") or ""):
        invalid.append("lot_order_code_mismatch")
    if str(order.get("side") or "").lower() != "buy":
        invalid.append("lot_source_is_not_buy")
    fills = fills_by_order.get(int(order["id"]), [])
    if len(fills) != 1:
        invalid.append("lot_source_fill_not_unique")
    else:
        fill = fills[0]
        if not EV.is_verified_row(fill):
            invalid.append("lot_source_fill_unverified")
        if str(fill.get("account_id") or "") != str(order.get("account_id") or ""):
            invalid.append("fill_order_account_mismatch")
        if str(fill.get("code") or "") != str(order.get("code") or ""):
            invalid.append("fill_order_code_mismatch")
        if str(fill.get("side") or "").lower() != str(order.get("side") or "").lower():
            invalid.append("fill_order_side_mismatch")
        try:
            if float(fill.get("qty")) != float(order.get("qty")):
                invalid.append("fill_order_qty_mismatch")
            if float(fill.get("qty")) != float(lot.get("qty")):
                invalid.append("fill_lot_qty_mismatch")
        except (TypeError, ValueError):
            invalid.append("fill_qty_missing")
    if invalid:
        return [], invalid
    return [{"type": "durable_lot_source_order", "source_id": int(lot["id"]),
             "cycle_id": cycle_id}], []


def _signal_evidence(conn: sqlite3.Connection, order: dict[str, Any],
                     cycles: dict[int, dict[str, Any]],
                     signals_by_id: dict[int, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    signal_id = order.get("signal_id")
    if signal_id is None:
        return [], []
    signal = signals_by_id.get(int(signal_id))
    if signal is None:
        # A pruned optional parent is not evidence for any cycle; it does not
        # contradict a separate durable lot/archive/reservation relation.
        return [], []
    try:
        cycle_id = int(signal.get("cycle_id"))
    except (TypeError, ValueError):
        return [], []
    if cycle_id not in cycles:
        return [], ["signal_cycle_id_unknown"]
    if str(signal.get("account_id") or "") != str(order.get("account_id") or ""):
        return [], ["signal_order_account_mismatch"]
    if str(signal.get("code") or "") != str(order.get("code") or ""):
        return [], ["signal_order_code_mismatch"]
    return [{"type": "cycle_owned_signal", "source_id": int(signal_id),
             "cycle_id": cycle_id}], []


def _reservation_evidence(conn: sqlite3.Connection, order: dict[str, Any],
                          cycles: dict[int, dict[str, Any]],
                          reservations_by_key: dict[str, list[dict[str, Any]]]
                          ) -> tuple[list[dict[str, Any]], list[str]]:
    rows = reservations_by_key.get(str(order["id"]), [])
    if not rows:
        return [], []
    if len(rows) != 1:
        return [], ["reservation_order_link_not_unique"]
    row = rows[0]
    try:
        cycle_id = int(row.get("cycle_id"))
    except (TypeError, ValueError):
        return [], ["reservation_cycle_id_missing"]
    if cycle_id not in cycles:
        return [], ["reservation_cycle_id_unknown"]
    identity = (
        str(row.get("account_id") or "") == str(order.get("account_id") or "")
        and str(row.get("code") or "") == str(order.get("code") or "")
        and str(row.get("side") or "").lower() == str(order.get("side") or "").lower()
    )
    if not identity:
        return [], ["reservation_order_identity_mismatch"]
    return [{"type": "one_to_one_cycle_reservation", "source_id": int(row["id"]),
             "cycle_id": cycle_id}], []


def _timestamp_conflicts(order: dict[str, Any], cycle: dict[str, Any]) -> list[str]:
    conflicts: list[str] = []
    started = str(cycle.get("started_at") or "")[:19]
    ended = str(cycle.get("ended_at") or "")[:19]
    for field in ("created_at", "executed_at"):
        value = str(order.get(field) or "")[:19]
        if value and started and value < started:
            conflicts.append(f"{field}_before_cycle_start")
        if value and ended and value > ended:
            conflicts.append(f"{field}_after_cycle_end")
    return conflicts


def _runtime_order_scope(conn: sqlite3.Connection, cycle_id: int,
                         null_orders: list[dict[str, Any]]) -> tuple[set[int], list[int]]:
    """Scope candidates through cycle-owned durable lots, never account state."""
    if "paper_position_lots" not in _tables(conn):
        return set(), []
    lot_cols = set(_columns(conn, "paper_position_lots"))
    if not {"cycle_id", "account_id", "code", "source_order_id"}.issubset(lot_cols):
        return set(), []
    rows = [
        _as_dict(row) for row in conn.execute(
            "SELECT account_id,code,source_order_id FROM paper_position_lots "
            "WHERE cycle_id=? ORDER BY id", (cycle_id,),
        )
    ]
    keys = {(str(row.get("account_id") or ""), str(row.get("code") or ""))
            for row in rows}
    source_ids = {int(row["source_order_id"]) for row in rows
                  if row.get("source_order_id") is not None}
    relevant = set(source_ids)
    for order in null_orders:
        key = (str(order.get("account_id") or ""), str(order.get("code") or ""))
        status = str(order.get("status") or "").lower()
        if key in keys and (
            status == "filled" or status in {
                "pending_execution", "unfilled_limit_down", "partially_filled",
                "pending", "open",
            }
        ):
            relevant.add(int(order["id"]))
    wrong_existing = []
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        for row in conn.execute(
            f"SELECT id,cycle_id FROM paper_orders WHERE id IN ({placeholders})",
            tuple(sorted(source_ids)),
        ):
            if row["cycle_id"] is not None and int(row["cycle_id"]) != cycle_id:
                wrong_existing.append(int(row["id"]))
    return relevant, sorted(wrong_existing)


def _classify_order(conn: sqlite3.Connection, order: dict[str, Any],
                    cycles: dict[int, dict[str, Any]],
                    archive_index: dict[int, list[dict[str, Any]]],
                    has_order_archive: bool,
                    lot_index: dict[int, list[dict[str, Any]]],
                    fills_by_order: dict[int, list[dict[str, Any]]],
                    signals_by_id: dict[int, dict[str, Any]],
                    reservations_by_key: dict[str, list[dict[str, Any]]]
                    ) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    invalid: list[str] = []
    for found, rejected in (
        _lot_evidence(conn, order, cycles, lot_index, fills_by_order),
        _signal_evidence(conn, order, cycles, signals_by_id),
        _reservation_evidence(conn, order, cycles, reservations_by_key),
    ):
        evidence.extend(found)
        invalid.extend(rejected)
    found, rejected = _archive_evidence(
        conn, order, cycles, archive_index, has_order_archive
    )
    evidence.extend(found)
    invalid.extend(rejected)
    cycle_ids = sorted({int(item["cycle_id"]) for item in evidence})
    conflicts = list(invalid)
    for cycle_id in cycle_ids:
        conflicts.extend(_timestamp_conflicts(order, cycles[cycle_id]))
    if len(cycle_ids) > 1:
        classification = "AMBIGUOUS"
        reason = "direct evidence names multiple cycles"
    elif conflicts:
        classification = "AMBIGUOUS"
        reason = "direct evidence or sanity checks conflict"
    elif len(cycle_ids) == 1:
        classification = "PROVEN"
        reason = "unique direct cycle-owned evidence"
    elif str(order.get("status") or "").lower() != "filled" and not order.get("executed_at"):
        classification = "IRRELEVANT"
        reason = "not an executed order and no direct lineage evidence"
    else:
        classification = "UNPROVABLE"
        reason = "no direct cycle-owned evidence"
    return {
        "order_id": int(order["id"]),
        "account_id": order.get("account_id"),
        "side": order.get("side"),
        "code": order.get("code"),
        "old_cycle_id": None,
        "new_cycle_id": cycle_ids[0] if classification == "PROVEN" else None,
        "classification": classification,
        "evidence": sorted(evidence, key=lambda item: (
            item["type"], int(item["source_id"]), int(item["cycle_id"])
        )),
        "conflict_count": len(conflicts),
        "conflicts": sorted(set(conflicts)),
        "reason": reason,
    }


def _base_plan(conn: sqlite3.Connection, cycle_id: int,
               normalized_target_ids: set[int] | None = None) -> dict[str, Any]:
    cycles = _cycle_rows(conn)
    if cycle_id not in cycles:
        raise RecoveryError(f"requested cycle does not exist: {cycle_id}")
    normalized_target_ids = set(normalized_target_ids or ())
    if "paper_orders" not in _tables(conn):
        raise RecoveryError("paper_orders does not exist")
    order_cols = set(_columns(conn, "paper_orders"))
    required = {"id", "account_id", "side", "code", "qty", "created_at",
                "executed_at", "status", "signal_id", "cycle_id"}
    if not required.issubset(order_cols):
        raise RecoveryError("paper_orders schema is missing required provenance columns")
    orders = []
    for row in conn.execute(
        "SELECT * FROM paper_orders WHERE cycle_id IS NULL OR id IN ("
        + (",".join("?" for _ in normalized_target_ids) if normalized_target_ids else "NULL")
        + ") ORDER BY id",
        tuple(sorted(normalized_target_ids)),
    ):
        order = _as_dict(row)
        current_cycle_id = order.get("cycle_id")
        if current_cycle_id is not None:
            if int(order["id"]) not in normalized_target_ids or int(current_cycle_id) != cycle_id:
                raise RecoveryError(
                    f"normalized target has unexpected cycle assignment: order={order['id']}"
                )
            order["cycle_id"] = None
        orders.append(order)
    runtime_relevant_ids, wrong_existing_sources = _runtime_order_scope(
        conn, cycle_id, orders
    )
    null_order_ids = {int(order["id"]) for order in orders}
    runtime_relevant_null_ids = runtime_relevant_ids & null_order_ids
    archive_index = _build_archive_index(conn, cycles)
    archive_columns = set(_columns(conn, "paper_orders_archive"))
    has_order_archive = {"id", "cycle_id"}.issubset(archive_columns)
    order_ids = {int(order["id"]) for order in orders}
    lot_index = _build_lot_index(conn, orders)
    fills_by_order: dict[int, list[dict[str, Any]]] = {}
    if order_ids and "paper_fills" in _tables(conn):
        for row in conn.execute(
            "SELECT f.*,o.execution_status,o.execution_verified "
            "FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id "
            "WHERE f.order_id IS NOT NULL ORDER BY f.id"
        ):
            item = _as_dict(row)
            if int(item["order_id"]) in order_ids:
                fills_by_order.setdefault(int(item["order_id"]), []).append(item)
    signal_ids = {int(order["signal_id"]) for order in orders
                  if order.get("signal_id") is not None}
    signals_by_id: dict[int, dict[str, Any]] = {}
    if signal_ids and "paper_signals" in _tables(conn):
        for signal_id in sorted(signal_ids):
            row = conn.execute("SELECT * FROM paper_signals WHERE id=?", (signal_id,)).fetchone()
            if row is not None:
                signals_by_id[signal_id] = _as_dict(row)
    reservations_by_key: dict[str, list[dict[str, Any]]] = {}
    if order_ids and "paper_capital_reservations" in _tables(conn):
        columns = set(_columns(conn, "paper_capital_reservations"))
        if {"id", "cycle_id", "order_key", "account_id", "code", "side"}.issubset(columns):
            for row in conn.execute(
                "SELECT * FROM paper_capital_reservations ORDER BY id"
            ):
                item = _as_dict(row)
                key = str(item.get("order_key") or "")
                if key in {str(value) for value in order_ids}:
                    reservations_by_key.setdefault(key, []).append(item)
    classifications = []
    for order in orders:
        if int(order["id"]) in runtime_relevant_null_ids:
            item = _classify_order(
                conn, order, cycles, archive_index, has_order_archive, lot_index,
                fills_by_order, signals_by_id, reservations_by_key,
            )
        else:
            item = {
                "order_id": int(order["id"]),
                "account_id": order.get("account_id"),
                "side": order.get("side"),
                "code": order.get("code"),
                "old_cycle_id": None,
                "new_cycle_id": None,
                "classification": "IRRELEVANT",
                "evidence": [],
                "conflict_count": 0,
                "conflicts": [],
                "reason": "outside the requested cycle durable-lot dependency scope",
            }
        classifications.append(item)
    counts = {name: 0 for name in ("PROVEN", "AMBIGUOUS", "UNPROVABLE", "IRRELEVANT")}
    for item in classifications:
        counts[item["classification"]] += 1
    proven = [item for item in classifications
              if item["classification"] == "PROVEN" and item["new_cycle_id"] == cycle_id]
    target_source_lots = []
    if "paper_position_lots" in _tables(conn):
        target_source_lots = [
            int(row[0]) for row in conn.execute(
                "SELECT DISTINCT source_order_id FROM paper_position_lots "
                "WHERE cycle_id=? AND source_order_id IS NOT NULL ORDER BY source_order_id",
                (cycle_id,),
            )
        ]
    source_missing_proof = sorted(
        set(target_source_lots)
        - {int(item["order_id"]) for item in proven}
        - {
            int(row[0]) for row in conn.execute(
                "SELECT id FROM paper_orders WHERE cycle_id=?", (cycle_id,)
            )
        }
    )
    fingerprints = _protected_fingerprints(conn)
    plan = {
        "plan_version": PLAN_VERSION,
        "cycle_id": cycle_id,
        "cycle_key": str(cycles[cycle_id].get("cycle_key") or ""),
        "schema_identity": _schema_identity(conn),
        "fingerprints_before": fingerprints,
        "orders_cycle_null_total_normalized": len(orders),
        "classification_counts": counts,
        "proven_count_for_requested_cycle": len(proven),
        "runtime_scoped_order_count": len(runtime_relevant_ids),
        "runtime_relevant_count": len(runtime_relevant_null_ids),
        "runtime_relevant_order_ids": sorted(runtime_relevant_null_ids),
        "source_lot_orders_missing_proof": source_missing_proof,
        "source_orders_with_conflicting_existing_cycle": wrong_existing_sources,
        "proven": sorted(proven, key=lambda item: int(item["order_id"])),
        "ambiguous": sorted(
            (item for item in classifications if item["classification"] == "AMBIGUOUS"),
            key=lambda item: int(item["order_id"]),
        ),
        "unprovable": sorted(
            (item for item in classifications if item["classification"] == "UNPROVABLE"),
            key=lambda item: int(item["order_id"]),
        ),
        "irrelevant": sorted(
            (item for item in classifications if item["classification"] == "IRRELEVANT"),
            key=lambda item: int(item["order_id"]),
        ),
        "normalized_target_ids": sorted(normalized_target_ids),
    }
    return plan


def build_plan(conn: sqlite3.Connection, cycle_id: int,
               normalized_target_ids: set[int] | None = None) -> dict[str, Any]:
    """Build a deterministic plan; account binding and wall clock are not inputs."""
    plan = _base_plan(conn, int(cycle_id), normalized_target_ids)
    # Normalize the target set through one second pass so a saved plan stays
    # byte-stable after its own cycle_id updates have already been applied.
    if normalized_target_ids is None:
        target_ids = {int(row["order_id"]) for row in plan["proven"]}
        if target_ids:
            plan["normalized_target_ids"] = sorted(target_ids)
    payload = _canonical_json(plan).encode("utf-8")
    plan["plan_sha256"] = _sha256(payload)
    return plan


def _canonical_trigger_sql() -> str:
    memory = sqlite3.connect(":memory:")
    try:
        memory.executescript(
            "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY);"
            "CREATE TABLE paper_orders(id INTEGER PRIMARY KEY,account_id TEXT,cycle_id INTEGER);"
            "CREATE TABLE paper_orders_archive(id INTEGER PRIMARY KEY,cycle_id INTEGER);"
        )
        PSM._ensure_order_cycle_provenance_guards(memory)
        row = memory.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (ORDER_IMMUTABLE_TRIGGER,),
        ).fetchone()
        if row is None:
            raise RecoveryError("canonical provenance trigger cannot be built")
        return str(row[0])
    finally:
        memory.close()


def _trigger_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (ORDER_IMMUTABLE_TRIGGER,),
    ).fetchone()
    return None if row is None else str(row[0])


def apply_plan(conn: sqlite3.Connection, saved_plan: dict[str, Any]) -> int:
    """Apply only reviewed NULL->cycle assignments in one rollback-safe transaction."""
    target_ids = {int(item["order_id"]) for item in saved_plan.get("proven", ())}
    recomputed = build_plan(conn, int(saved_plan["cycle_id"]), target_ids)
    if recomputed.get("plan_sha256") != saved_plan.get("plan_sha256"):
        raise RecoveryError("database/evidence changed since the saved plan")
    if _canonical_json(recomputed) != _canonical_json(saved_plan):
        raise RecoveryError("saved plan does not match deterministic re-plan")
    target_cycle = int(saved_plan["cycle_id"])
    trigger_before = _trigger_sql(conn)
    canonical = _canonical_trigger_sql()
    if trigger_before != canonical:
        raise RecoveryError("cycle immutability trigger is absent or differs from schema owner")
    schema_before = _schema_identity(conn)
    changed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Recheck after obtaining the write lock, before temporarily removing
        # exactly the cycle_id immutability guard.
        locked_plan = build_plan(conn, target_cycle, target_ids)
        if locked_plan.get("plan_sha256") != saved_plan.get("plan_sha256"):
            raise RecoveryError("database changed while acquiring maintenance lock")
        if _trigger_sql(conn) != canonical:
            raise RecoveryError("cycle immutability trigger changed before maintenance lock")
        before = _protected_fingerprints(conn)
        conn.execute(f'DROP TRIGGER "{ORDER_IMMUTABLE_TRIGGER}"')
        for item in saved_plan.get("proven", ()):
            order_id = int(item["order_id"])
            cycle_id = int(item["new_cycle_id"])
            if cycle_id != target_cycle:
                raise RecoveryError("plan contains an assignment outside the requested cycle")
            cur = conn.execute(
                "UPDATE paper_orders SET cycle_id=? WHERE id=? AND cycle_id IS NULL",
                (cycle_id, order_id),
            )
            if cur.rowcount not in (0, 1):
                raise RecoveryError(f"unexpected update count for order {order_id}")
            if cur.rowcount == 0:
                row = conn.execute("SELECT cycle_id FROM paper_orders WHERE id=?", (order_id,)).fetchone()
                if row is None or int(row[0]) != target_cycle:
                    raise RecoveryError(f"target order changed before apply: {order_id}")
            changed += cur.rowcount
        # Reuse the schema owner; the same canonical immutable rule is restored
        # before any invariant is checked or the transaction can commit.
        PSM._ensure_order_cycle_provenance_guards(conn)
        if _trigger_sql(conn) != canonical:
            raise RecoveryError("cycle immutability trigger was not restored exactly")
        after = _protected_fingerprints(conn)
        if before != after:
            raise RecoveryError("non-cycle ledger facts changed during provenance repair")
        if _schema_identity(conn) != schema_before:
            raise RecoveryError("schema changed during provenance repair")
        for item in saved_plan.get("proven", ()):
            row = conn.execute(
                "SELECT cycle_id FROM paper_orders WHERE id=?", (int(item["order_id"]),)
            ).fetchone()
            if row is None or int(row[0]) != target_cycle:
                raise RecoveryError("post-apply order cycle verification failed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return changed


def connect_readonly(path: str) -> sqlite3.Connection:
    resolved = Path(path).resolve(strict=True)
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def connect_writable(path: str) -> sqlite3.Connection:
    resolved = Path(path).resolve(strict=True)
    conn = sqlite3.connect(str(resolved), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _read_plan(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as stream:
        plan = json.load(stream)
    if not isinstance(plan, dict) or plan.get("plan_version") != PLAN_VERSION:
        raise RecoveryError("unsupported or malformed plan file")
    hash_value = plan.pop("plan_sha256", None)
    expected = _sha256(_canonical_json(plan).encode("utf-8"))
    plan["plan_sha256"] = hash_value
    if hash_value != expected:
        raise RecoveryError("plan file SHA-256 does not match its contents")
    return plan


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except RecoveryError as exc:
        # A contract violation is an expected operator-facing failure, not a
        # crash: report it plainly and fail closed with a non-zero status.
        print(f"recovery refused: {exc}", file=sys.stderr)
        return 2


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite ledger path")
    parser.add_argument("--cycle-id", type=int, required=True,
                        help="explicit cycle whose direct historical evidence is being reviewed")
    parser.add_argument("--plan-out", help="write deterministic dry-run plan JSON")
    parser.add_argument("--apply", action="store_true", help="apply a previously reviewed plan")
    parser.add_argument("--plan", help="saved plan required with --apply")
    args = parser.parse_args(argv)
    if args.apply and not args.plan:
        parser.error("--apply requires --plan")
    if not args.apply and args.plan:
        parser.error("--plan is only used with --apply")
    if args.apply:
        # Validate the reviewed plan *before* opening anything writable. An
        # operator who selects cycle N but passes a reviewed cycle-M plan must
        # never mutate cycle M, so the requested cycle is checked against the
        # plan here — no connection, no transaction, no trigger change yet.
        plan = _read_plan(args.plan)
        requested_cycle = int(args.cycle_id)
        try:
            plan_cycle = int(plan.get("cycle_id"))
        except (TypeError, ValueError):
            raise RecoveryError(
                "saved plan does not declare a usable cycle: "
                f"requested={requested_cycle} plan={plan.get('cycle_id')!r}"
            ) from None
        if requested_cycle != plan_cycle:
            raise RecoveryError(
                "requested cycle does not match reviewed plan: "
                f"requested={requested_cycle} plan={plan_cycle}"
            )
        conn = connect_writable(args.db)
        try:
            changed = apply_plan(conn, plan)
            print(_canonical_json({
                "status": "applied", "changed_rows": changed,
                "plan_sha256": plan["plan_sha256"],
                "cycle_id": plan["cycle_id"],
            }))
        finally:
            conn.close()
        return 0
    conn = connect_readonly(args.db)
    try:
        plan = build_plan(conn, args.cycle_id)
        if args.plan_out:
            with open(args.plan_out, "x", encoding="utf-8", newline="\n") as stream:
                stream.write(_canonical_json(plan) + "\n")
        counts = plan["classification_counts"]
        print(_canonical_json({
            "status": "dry_run", "cycle_id": plan["cycle_id"],
            "cycle_key": plan["cycle_key"],
            "orders_cycle_null_total_normalized": plan["orders_cycle_null_total_normalized"],
            "proven_for_cycle": plan["proven_count_for_requested_cycle"],
            "runtime_relevant": plan["runtime_relevant_count"],
            "runtime_scoped_total": plan["runtime_scoped_order_count"],
            "ambiguous": counts["AMBIGUOUS"],
            "unprovable": counts["UNPROVABLE"],
            "irrelevant": counts["IRRELEVANT"],
            "source_lot_orders_missing_proof": len(plan["source_lot_orders_missing_proof"]),
            "source_orders_with_conflicting_existing_cycle": len(
                plan["source_orders_with_conflicting_existing_cycle"]
            ),
            "plan_sha256": plan["plan_sha256"],
        }))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
