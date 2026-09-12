# -*- coding: utf-8 -*-
"""Deterministic, privacy-bounded strategy candidate replay traces.

Replay data is content-addressed and column-sharded. Static factor columns are
stored once and reused by every strategy/run; only changed live columns create
new blobs. Arbitrary kwargs, account state, positions, credentials and process
environment data are never serialized.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

TRACE_SCHEMA = "strategy-replay-v1"
_TABLE_SCHEMA = "strategy-replay-table-v1"
_INDEX_SCHEMA = "strategy-replay-index-v1"
_COLUMN_SCHEMA = "strategy-replay-column-v1"
_GIT_HASH = re.compile(r"^[0-9a-fA-F]{7,40}$")
_BLOB_ID = re.compile(r"^[0-9a-f]{64}$")
_DATE_KEYS = ("last_date", "factor_date", "data_date", "historical_factor_date")
_SAFE_SELECTION_INPUTS = {
    "topn",
    "news_hits",
    "gate",
    "auto_news",
    "first_board_codes",
    "weight_overrides",
    "condition_overrides",
}
_PROHIBITED_FIELD_NAMES = {
    "account", "account_id", "accounts",
    "position", "positions", "holding", "holdings",
    "cash", "cash_balance", "balance", "balances",
    "password", "passwd", "secret", "secrets", "token", "api_key",
    "authorization", "cookie", "cookies", "environment", "env",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _cache_root() -> Path:
    cache = os.environ.get("ASTOCK_CACHE_DIR")
    return Path(cache).expanduser() if cache else _repo_root() / "data_cache"


def _default_trace_dir() -> Path:
    return _cache_root() / "strategy_replay"


def _canonical_git_hash(value: Any) -> str | None:
    text = str(value or "").strip()
    if not _GIT_HASH.fullmatch(text):
        return None
    return text.lower()[:12]


def code_version(explicit: Any = None) -> str:
    """Return only the audited short git hash, never a dump of runtime env."""
    candidate = _canonical_git_hash(explicit)
    if candidate:
        return candidate
    candidate = _canonical_git_hash(os.environ.get("ASTOCK_GIT_COMMIT"))
    if candidate:
        return candidate
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=_repo_root(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return _canonical_git_hash(proc.stdout) or "unavailable"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
            return None
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_json_safe(item) for item in sorted(value, key=lambda item: str(item))]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (dt.date, dt.datetime, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _field_is_prohibited(name: Any) -> bool:
    text = str(name or "").strip().lower()
    if text in _PROHIBITED_FIELD_NAMES:
        return True
    parts = {part for part in re.split(r"[^a-z0-9]+", text) if part}
    return bool(parts & {"password", "passwd", "secret", "token", "cookie"})


def _assert_no_prohibited_keys(value: Any, *, path: str = "input") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _field_is_prohibited(key):
                raise ValueError(f"strategy replay refuses prohibited field: {path}.{key}")
            _assert_no_prohibited_keys(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            _assert_no_prohibited_keys(item, path=f"{path}[{index}]")


def _canonical_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def data_date(table: pd.DataFrame, explicit: Any = None) -> str:
    """Resolve one exact factor as-of date; mixed dates are never collapsed."""
    value = _canonical_date(explicit)
    if explicit is not None:
        if value is None:
            raise ValueError("strategy replay data date is invalid")
        return value
    attrs = getattr(table, "attrs", {}) or {}
    for key in ("data_date", "factor_date", "as_of_date"):
        value = _canonical_date(attrs.get(key))
        if value:
            return value
    for key in _DATE_KEYS:
        if key not in table.columns:
            continue
        dates = {
            item
            for item in (_canonical_date(raw) for raw in table[key].tolist())
            if item is not None
        }
        if len(dates) == 1:
            return next(iter(dates))
        if len(dates) > 1:
            raise ValueError(f"strategy replay refuses mixed data dates in {key}")
    raise ValueError("strategy replay cannot prove factor data date")


def _safe_selection_inputs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    extras = sorted(
        key for key, value in kwargs.items()
        if key not in _SAFE_SELECTION_INPUTS and value is not None
    )
    if extras:
        raise ValueError(f"strategy replay refuses undeclared selection input: {extras[0]}")
    selected = {
        key: kwargs[key]
        for key in _SAFE_SELECTION_INPUTS
        if key in kwargs and kwargs[key] is not None
    }
    _assert_no_prohibited_keys(selected, path="selection_inputs")
    return _json_safe(selected)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _blob_path(root: Path, kind: str, blob_id: str) -> Path:
    if kind not in {"runs", "indexes", "columns"}:
        raise ValueError("invalid strategy replay blob kind")
    if not _BLOB_ID.fullmatch(str(blob_id or "")):
        raise ValueError("invalid strategy replay snapshot id")
    return root / kind / f"{blob_id}.json.gz"


def _write_blob(root: Path, kind: str, payload: Mapping[str, Any]) -> str:
    raw = _canonical_json(payload)
    blob_id = hashlib.sha256(raw).hexdigest()
    path = _blob_path(root, kind, blob_id)
    if path.exists():
        return blob_id
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{blob_id}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as raw_handle:
            with gzip.GzipFile(filename="", fileobj=raw_handle, mode="wb", mtime=0) as handle:
                handle.write(raw)
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return blob_id


def _read_blob(root: Path, kind: str, blob_id: str) -> dict[str, Any]:
    path = _blob_path(root, kind, blob_id)
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if hashlib.sha256(_canonical_json(payload)).hexdigest() != blob_id:
        raise ValueError("strategy replay snapshot integrity mismatch")
    return payload


def _validate_table(table: pd.DataFrame) -> None:
    if not isinstance(table, pd.DataFrame):
        raise TypeError("strategy replay requires a pandas DataFrame")
    prohibited = [str(column) for column in table.columns if _field_is_prohibited(column)]
    if prohibited:
        raise ValueError(
            "strategy replay refuses prohibited table columns: " + ", ".join(sorted(prohibited))
        )


def _persist_table(root: Path, table: pd.DataFrame) -> dict[str, Any]:
    _validate_table(table)
    index_payload = {
        "schema": _INDEX_SCHEMA,
        "values": [str(value) for value in table.index.tolist()],
    }
    index_id = _write_blob(root, "indexes", index_payload)
    columns = []
    for column in table.columns:
        name = str(column)
        values = [_json_safe(value) for value in table[column].tolist()]
        for row_no, value in enumerate(values):
            _assert_no_prohibited_keys(value, path=f"table.{name}[{row_no}]")
        column_payload = {
            "schema": _COLUMN_SCHEMA,
            "name": name,
            "values": values,
        }
        columns.append({"name": name, "blob_id": _write_blob(root, "columns", column_payload)})
    return {
        "schema": _TABLE_SCHEMA,
        "row_count": len(table),
        "index_id": index_id,
        "columns": columns,
    }


def _load_table(root: Path, manifest: Mapping[str, Any]) -> pd.DataFrame:
    if manifest.get("schema") != _TABLE_SCHEMA:
        raise ValueError("unsupported strategy replay table schema")
    index_payload = _read_blob(root, "indexes", str(manifest.get("index_id") or ""))
    if index_payload.get("schema") != _INDEX_SCHEMA:
        raise ValueError("unsupported strategy replay index schema")
    index = [str(value) for value in index_payload.get("values") or []]
    data: dict[str, list[Any]] = {}
    ordered_columns = []
    for item in manifest.get("columns") or []:
        name = str(item.get("name") or "")
        payload = _read_blob(root, "columns", str(item.get("blob_id") or ""))
        if payload.get("schema") != _COLUMN_SCHEMA or payload.get("name") != name:
            raise ValueError("strategy replay column manifest mismatch")
        values = list(payload.get("values") or [])
        if len(values) != len(index):
            raise ValueError("strategy replay column length mismatch")
        ordered_columns.append(name)
        data[name] = values
    table = pd.DataFrame(data, index=index)
    return table.loc[:, ordered_columns]


def persist_snapshot(
    *,
    strategy_id: str,
    selector_id: str,
    table: pd.DataFrame,
    factor_inputs: Iterable[str],
    selection_kwargs: Mapping[str, Any],
    explicit_data_date: Any = None,
    explicit_code_version: Any = None,
    trace_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Persist one immutable replay run manifest over deduplicated table blobs."""
    factors = tuple(str(item).strip() for item in factor_inputs if str(item).strip())
    if not factors:
        raise ValueError("strategy replay requires declared factor inputs")
    _validate_table(table)
    resolved_date = data_date(table, explicit=explicit_data_date)
    resolved_version = code_version(explicit_code_version)
    safe_inputs = _safe_selection_inputs(selection_kwargs)
    for column in table.columns:
        for row_no, value in enumerate(table[column].tolist()):
            safe = _json_safe(value)
            _assert_no_prohibited_keys(safe, path=f"table.{column}[{row_no}]")
    root = Path(trace_dir) if trace_dir is not None else _default_trace_dir()
    table_manifest = _persist_table(root, table)
    snapshot = {
        "schema": TRACE_SCHEMA,
        "strategy_id": str(strategy_id),
        "selector_id": str(selector_id),
        "data_date": resolved_date,
        "code_version": resolved_version,
        "factor_inputs": list(factors),
        "selection_inputs": safe_inputs,
        "table": table_manifest,
    }
    snapshot_id = _write_blob(root, "runs", snapshot)
    return {
        "schema": TRACE_SCHEMA,
        "snapshot_id": snapshot_id,
        "data_date": resolved_date,
        "code_version": resolved_version,
        "factor_inputs": list(factors),
        "row_count": len(table),
    }


def load_snapshot(snapshot_id: str, *, trace_dir: Path | str | None = None) -> dict[str, Any]:
    root = Path(trace_dir) if trace_dir is not None else _default_trace_dir()
    payload = _read_blob(root, "runs", snapshot_id)
    if payload.get("schema") != TRACE_SCHEMA:
        raise ValueError("unsupported strategy replay schema")
    _assert_no_prohibited_keys(payload, path="snapshot")
    return payload


def replay_inputs(
    snapshot: Mapping[str, Any],
    *,
    trace_dir: Path | str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    root = Path(trace_dir) if trace_dir is not None else _default_trace_dir()
    table = _load_table(root, snapshot.get("table") or {})
    table.attrs["data_date"] = snapshot.get("data_date")
    return table, dict(snapshot.get("selection_inputs") or {})


def _row_for_code(table: pd.DataFrame, code: Any) -> Mapping[str, Any] | None:
    target = str(code or "")
    if target in table.index:
        row = table.loc[target]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return row
    for index in table.index:
        if str(index) == target:
            row = table.loc[index]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            return row
    return None


def attach_candidate_traces(
    result: Mapping[str, Any],
    *,
    table: pd.DataFrame,
    factor_inputs: Iterable[str],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach compact provenance to every emitted candidate-like row."""
    factors = tuple(str(item) for item in factor_inputs)
    output = dict(result)
    for key in ("picks", "shadow_picks", "hot_leader_watch"):
        if key not in result:
            continue
        traced = []
        for raw_pick in result.get(key) or []:
            pick = dict(raw_pick)
            row = _row_for_code(table, pick.get("code"))
            if row is None:
                raise ValueError(f"strategy replay candidate missing from input table: {pick.get('code')}")
            pick["candidate_trace"] = {
                "snapshot_id": manifest["snapshot_id"],
                "data_date": manifest["data_date"],
                "code_version": manifest["code_version"],
                "factor_snapshot": {factor: _json_safe(row.get(factor)) for factor in factors},
            }
            traced.append(pick)
        output[key] = traced
    output["replay_trace"] = dict(manifest)
    return output