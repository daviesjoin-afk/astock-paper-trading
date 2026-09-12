# -*- coding: utf-8 -*-
"""Model-agnostic reproducible shadow evaluation gate for research evidence.

This module turns an already-built point-in-time dataset (see
:mod:`learning_dataset`) plus a set of *already generated* out-of-sample
predictions into one scientific, reproducible verdict:

    immutable, availability-proven prediction evidence
        -> dataset-fingerprint + model-artifact binding
        -> provable train / test separation
        -> 100% held-out coverage (no cherry-picking)
        -> per-date cross-sectional Spearman rank IC
        -> confidence lower bound over *date* units
        -> chronological tail robustness check
        -> append-only evaluation manifest + SHA-256 fingerprint
        -> shadow admission gate (never execution authority)

It performs **no model training** and depends on **no ML library** (no numpy,
no pandas, no scikit-learn, no torch, no tensorflow).  It is a pure,
deterministic measurement device: give it the same dataset and the same
predictions and it must return the same verdict byte-for-byte, on any machine,
on any day.  Which model produced a score is deliberately opaque -- the gate
measures *evidence quality*, not any particular architecture.

Contracts enforced here (v2 -- every one of them is a refusal, not a warning):

    an evaluation is bound to exactly one dataset fingerprint
    a prediction from another dataset is never silently accepted
    prediction evidence is content-addressed and never overwritten
    two conflicting predictions for one logical identity fail closed
    only ``test`` may be scored; train/validation are refused outright
    a prediction whose availability instant was never proven cannot count
    a prediction must predate the label it claims to have predicted
    every canonical held-out sample must carry exactly one prediction
    the model's training boundary must be proven to stop before the test set
    the model must not have been selected on the held-out partition
    the recent chronological tail must not have lost the edge
    IC is cross-sectional per date, and tied ranks use average ranks
    rows on the same date are NOT independent: the date is the unit
    a row count is not scientific evidence; the date count is
    a positive IC point estimate is not evidence; the confidence bound is
    missing / unknown / non-finite scores are never repaired into 0
    scientific readiness != execution authority
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

import learning_dataset as LD


EVALUATION_SCHEMA_VERSION = "learning-evaluation-v2"
EVALUATION_CONTRACT_VERSION = "learning-evaluation-v2"
FINGERPRINT_VERSION = "sha256-canonical-v1"
MANIFEST_SCHEMA_VERSION = "learning-evaluation-manifest-v2"

METRIC_SPEARMAN_RANK_IC = "spearman_rank_ic"

# The only partition that may be scored.  Evaluating on ``train`` (or
# ``validation``) would measure in-sample fit, which is not out-of-sample
# evidence.  This is a *closed* list on purpose: a partition name that is not
# ``test`` -- including a plausible-looking typo -- is refused outright rather
# than silently treated as "no evidence yet".
DEFAULT_HOLDOUT_PARTITION = "test"
SUPPORTED_HOLDOUT_PARTITIONS = (DEFAULT_HOLDOUT_PARTITION,)

# The only partition a model may be *selected* on.  Selecting on the held-out
# partition would turn the out-of-sample claim into an in-sample one.
SUPPORTED_SELECTION_PARTITIONS = ("validation",)

# Scientific floors.  Every one of these is a *date* count or a bound, never a
# row count: a million rows observed on two dates are two observations.
MIN_EVAL_DATES = 5
MIN_CODES_PER_DATE = 3
MIN_MEAN_RANK_IC = 0.02
MIN_IC_LOWER_BOUND = 0.0
CONFIDENCE_Z = 1.645
CONFIDENCE_LABEL = "one_sided_95pct"

# Chronological *tail* robustness.  A verdict built only from the full-sample
# mean hides a model that worked early and decayed: the last slice of the
# held-out window is scored separately and must still look like the sample.
HOLDOUT_FRACTION = 0.3
MIN_HOLDOUT_DATES = 3
MIN_HOLDOUT_POSITIVE_RATIO = 0.5
# The tail must retain at least this fraction of the full-sample mean IC.
HOLDOUT_RETENTION_FLOOR = 0.5

PREDICTION_EVIDENCE_TABLE = "learning_prediction_evidence"
MODEL_PROVENANCE_TABLE = "learning_model_provenance"
EVALUATION_MANIFEST_TABLE = "learning_evaluation_manifests"

PREDICTION_EXCLUSION_REASONS = (
    "unbound_prediction",
    "missing_model_id",
    "other_model",
    "unattributed_model",
    "unattributed_model_artifact",
    "invalid_prediction_score",
    "unknown_sample",
    "partition_mismatch",
    "not_held_out",
    "invalid_prediction_identity",
    "unproven_prediction_availability",
    "future_prediction",
    "prediction_conflict",
    "duplicate_prediction",
)

# Every blocker this module can emit, documented in one place.  A blocker is a
# *refusal*, never a warning: any single one of them keeps ``contract_ok``
# false and therefore keeps the neural shadow at ``shadow_only``.
EVALUATION_BLOCKERS = (
    "evaluation_prediction_table_missing",
    "evaluation_contract_error",
    "evaluation_dataset_contract_failed",
    "evaluation_dataset_fingerprint_unavailable",
    "evaluation_holdout_partition_unsupported",
    "evaluation_model_ambiguous",
    "evaluation_model_unresolved",
    "evaluation_model_provenance_truncated",
    "evaluation_training_boundary_unproven",
    "evaluation_training_overlaps_test",
    "evaluation_selection_partition_unproven",
    "evaluation_test_used_for_selection",
    "evaluation_prediction_conflict",
    "evaluation_no_held_out_predictions",
    "evaluation_missing_predictions",
    "evaluation_unexpected_predictions",
    "evaluation_insufficient_dates",
    "evaluation_mean_rank_ic_below_floor",
    "evaluation_confidence_bound_unavailable",
    "evaluation_confidence_bound_not_positive",
    "evaluation_holdout_insufficient",
    "evaluation_holdout_mean_not_positive",
    "evaluation_holdout_deterioration",
    "evaluation_predictions_read_truncated",
)

# Stable, fully deterministic prediction read order: two databases holding the
# same rows in a different insertion order must read them identically.
STABLE_PREDICTION_ORDER = (
    "dataset_fingerprint",
    "model_id",
    "label_start_date",
    "sample_key",
    "prediction_id",
)

# Stable, fully deterministic model-provenance read order.
STABLE_PROVENANCE_ORDER = ("model_id", "provenance_fingerprint")

# Material prediction evidence.  Every field here participates in the content
# address, so two rows that disagree about any one of them can never collapse
# into a single ``prediction_id`` and hide the disagreement.
_PREDICTION_MATERIAL_FIELDS = (
    "evaluation_schema_version",
    "contract_version",
    "dataset_fingerprint",
    "model_id",
    "model_version",
    "model_artifact_fingerprint",
    "sample_key",
    "code",
    "partition",
    "label_start_date",
    "score",
    "prediction_asof",
    "source",
)

_PREDICTION_FIELDS = (
    "dataset_fingerprint",
    "model_id",
    "model_version",
    "model_artifact_fingerprint",
    "sample_key",
    "code",
    "partition",
    "label_start_date",
    "score",
    "prediction_asof",
    "source",
    "contract_version",
)

# Material model provenance.  The evaluation fingerprint binds all of it, so a
# re-trained artifact can never inherit an older verdict's credibility.
_PROVENANCE_MATERIAL_FIELDS = (
    "evaluation_schema_version",
    "contract_version",
    "model_id",
    "model_version",
    "model_artifact_fingerprint",
    "training_dataset_fingerprint",
    "trained_through",
    "selection_partition",
    "hyperparameters_fingerprint",
    "random_seed",
    "source",
)

_PROVENANCE_FIELDS = (
    "model_id",
    "model_version",
    "model_artifact_fingerprint",
    "training_dataset_fingerprint",
    "trained_through",
    "selection_partition",
    "hyperparameters_fingerprint",
    "random_seed",
    "source",
)

# Modules that must never appear in this module's namespace.  Enforced by
# :func:`forbidden_dependencies`.  The ML entries matter as much as the network
# ones: an evaluation gate that quietly pulls in a training library is no longer
# a reproducible measurement device.
_FORBIDDEN_DEPENDENCY_PREFIXES = (
    "adaptive_engine",
    "adaptive_learning_dispatch",
    "adaptive_learning_worker",
    "adaptive_risk",
    "adaptive_selection",
    "adaptive_shadow_risk",
    "asymmetric_risk",
    "paper_trading",
    "paper_cycle_service",
    "paper_repository",
    "execution",
    "order_intent",
    "strategy_registry",
    "risk_center",
    "deepseek",
    "ai_analysis",
    "news_learning",
    "requests",
    "urllib",
    "http",
    "socket",
    "httpx",
    "aiohttp",
    "market_data",
    "alt_data",
    "numpy",
    "pandas",
    "scipy",
    "sklearn",
    "torch",
    "tensorflow",
    "statsmodels",
    "xgboost",
    "lightgbm",
    "catboost",
)

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The dataset layer reads a naive availability timestamp as *exchange-local*
# (Asia/Shanghai) rather than the host's timezone.  The evaluation layer must
# use exactly the same rule, otherwise "prediction predates label" would be a
# host-dependent claim.
_EXCHANGE_TZ = _dt.timezone(_dt.timedelta(hours=8), "UTC+08:00")
_UTC = _dt.timezone.utc


# ────────────────────────────── small helpers ──────────────────────────────
# These intentionally re-state the dataset layer's normalization *rules* rather
# than importing its private helpers, so this module depends only on
# ``learning_dataset``'s public API and stays independently auditable.
# ``NormalizationAgreementTests`` pins the two implementations together: if they
# ever drift, the suite fails instead of the fingerprints silently diverging.


def _text(value: Any) -> Optional[str]:
    """Return a trimmed string, or ``None`` for any missing/blank value."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null", "-"}:
        return None
    return text


def _iso_date(value: Any) -> Optional[str]:
    """Extract a ``YYYY-MM-DD`` date without ever inventing one."""
    text = _text(value)
    if text is None:
        return None
    candidate = text[:10].replace("/", "-")
    if not _DATE_ONLY.match(candidate):
        return None
    try:
        _dt.date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def _finite(value: Any) -> Optional[float]:
    """Return a finite float, or ``None``.  Never coerces missing to ``0``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"nan", "none", "null", "-", "inf", "-inf", "+inf"}:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    return number if math.isfinite(number) else None


def _canon_number(value: Any) -> Optional[str]:
    """Versioned numeric normalization for canonical serialization.

    Numbers become a fixed-precision decimal string so ``repr`` of a Python
    object can never leak into a fingerprint.  ``-0.0`` collapses to ``0.0``.
    """
    number = _finite(value)
    if number is None:
        return None
    if number == 0.0:
        number = 0.0
    return format(number, ".12g")


def _canon_json(value: Any) -> Any:
    """Canonical, deterministic JSON shape: sorted keys, explicit nulls."""
    if isinstance(value, Mapping):
        return {str(key): _canon_json(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canon_json(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return _canon_number(value)
    return str(value)


def _digest(payload: Mapping[str, Any], *, domain: str) -> str:
    """SHA-256 over a canonical serialization, namespaced by purpose."""
    serialized = json.dumps(
        {"domain": str(domain), "payload": _canon_json(payload)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _availability_instant(value: Any) -> Optional[_dt.datetime]:
    """Canonical UTC instant for a *proven* availability timestamp.

    Mirrors the dataset layer's rule exactly: a full timestamp is converted to
    the canonical UTC clock (naive input is read as exchange-local), and a
    date-only value encodes day granularity, i.e. the start of the
    exchange-local day.  Anything unparseable returns ``None`` -- which callers
    must treat as "unproven", never as "fine".
    """
    text = _text(value)
    if text is None:
        return None
    normalized = text if _DATE_ONLY.match(text) else LD.normalize_availability(text)
    if normalized is None:
        return None
    if _DATE_ONLY.match(normalized):
        try:
            naive = _dt.datetime.fromisoformat(normalized + "T00:00:00")
        except ValueError:
            return None
        return naive.replace(tzinfo=_EXCHANGE_TZ).astimezone(_UTC)
    try:
        parsed = _dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_EXCHANGE_TZ)
    return parsed.astimezone(_UTC)


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
    """Additive, idempotent column migration for the research tables.

    ``CREATE TABLE IF NOT EXISTS`` cannot widen a table that already exists, so
    a database written by an earlier contract version would otherwise reject the
    richer rows.  Only ``ADD COLUMN`` is ever issued: no column is dropped,
    renamed, retyped or rewritten.
    """
    existing = _table_columns(conn, table)
    if not existing:
        return
    for name, declaration in columns.items():
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _rows_as_dicts(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list:
    """Row-factory independent read, so a bare connection works too."""
    try:
        cursor = conn.execute(sql, args)
    except sqlite3.Error:
        return []
    names = [str(description[0]) for description in cursor.description or ()]
    return [dict(zip(names, row, strict=False)) for row in cursor.fetchall()]


def _row_as_dict(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> Optional[dict]:
    rows = _rows_as_dicts(conn, sql, args)
    return rows[0] if rows else None


def forbidden_dependencies() -> list:
    """Return forbidden modules present in this module's namespace.

    Backs the source-dependency guard: the evaluation gate must not be able to
    reach an execution path, a network path or a training library.
    """
    present = []
    for name, value in globals().items():
        if name.startswith("_"):
            continue
        module_name = getattr(value, "__name__", None)
        if not isinstance(module_name, str):
            continue
        root = module_name.split(".")[0]
        if any(root.startswith(prefix) for prefix in _FORBIDDEN_DEPENDENCY_PREFIXES):
            present.append(module_name)
    return sorted(set(present))


# ─────────────────────────────── ranking math ───────────────────────────────


def _average_ranks(values: Sequence[float]) -> list:
    """Ranks ``1..n`` with tied values sharing their average rank.

    Deterministic by construction: the sort is keyed on ``(value, index)`` so
    equal values can never be ordered by chance, and every tie block receives
    the same rank regardless of input order.
    """
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for offset in range(position, end + 1):
            ranks[order[offset]] = average
        position = end + 1
    return ranks


def spearman_rank_ic(scores: Sequence[float], targets: Sequence[float]) -> Optional[float]:
    """Cross-sectional Spearman rank IC between scores and forward returns.

    Returns ``None`` when the correlation is undefined -- fewer than two pairs,
    or a degenerate cross-section with no rank variance (e.g. every candidate
    scored identically).  ``None`` is *not* repaired into ``0``: an undefined
    statistic is a missing observation, never a neutral one.
    """
    if len(scores) != len(targets) or len(scores) < 2:
        return None
    rank_scores = _average_ranks([float(value) for value in scores])
    rank_targets = _average_ranks([float(value) for value in targets])
    count = len(rank_scores)
    mean_scores = sum(rank_scores) / count
    mean_targets = sum(rank_targets) / count
    covariance = sum(
        (rank_scores[index] - mean_scores) * (rank_targets[index] - mean_targets)
        for index in range(count)
    )
    variance_scores = sum((value - mean_scores) ** 2 for value in rank_scores)
    variance_targets = sum((value - mean_targets) ** 2 for value in rank_targets)
    if variance_scores <= 0 or variance_targets <= 0:
        return None
    return covariance / math.sqrt(variance_scores * variance_targets)


def _mean_and_bound(
    values: Sequence[float], confidence_z: float
) -> tuple:
    """Return ``(mean, sample_std, standard_error, lower_bound)``.

    The unit of observation is one date, which is exactly why same-date rows
    must never be treated as independent samples: a cross-section of 500 names
    is a single observation of the strategy's cross-sectional skill, not 500.
    With fewer than two dates no interval exists at all -- and ``None`` here
    keeps the gate closed instead of inventing a bound from one number.
    """
    if not values:
        return None, None, None, None
    count = len(values)
    mean = sum(values) / count
    if count < 2:
        return mean, None, None, None
    variance = sum((value - mean) ** 2 for value in values) / (count - 1)
    std = math.sqrt(variance)
    standard_error = std / math.sqrt(count)
    return mean, std, standard_error, mean - float(confidence_z) * standard_error


# ──────────────────────────────── schema ────────────────────────────────


def ensure_schema(conn: sqlite3.Connection) -> dict:
    """Idempotently create / widen the research evidence tables.

    Purely additive: nothing is dropped, renamed or rewritten, and no historical
    value is fabricated.  Safe on an empty database, on a database written by the
    first PR-9 contract version, and on a database that already holds earlier
    evaluation runs.
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PREDICTION_EVIDENCE_TABLE}(
            prediction_id TEXT PRIMARY KEY,
            evaluation_schema_version TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            source TEXT NOT NULL,
            dataset_fingerprint TEXT NOT NULL,
            model_id TEXT NOT NULL,
            sample_key TEXT NOT NULL,
            code TEXT NOT NULL,
            partition TEXT NOT NULL,
            label_start_date TEXT,
            score TEXT,
            prediction_asof TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        PREDICTION_EVIDENCE_TABLE,
        {
            "model_version": "TEXT",
            "model_artifact_fingerprint": "TEXT",
        },
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MODEL_PROVENANCE_TABLE}(
            provenance_fingerprint TEXT PRIMARY KEY,
            evaluation_schema_version TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            source TEXT NOT NULL,
            model_id TEXT NOT NULL,
            model_version TEXT,
            model_artifact_fingerprint TEXT,
            training_dataset_fingerprint TEXT,
            trained_through TEXT,
            selection_partition TEXT,
            hyperparameters_fingerprint TEXT,
            random_seed TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {EVALUATION_MANIFEST_TABLE}(
            evaluation_fingerprint TEXT PRIMARY KEY,
            evaluation_schema_version TEXT NOT NULL,
            evaluation_contract_version TEXT NOT NULL,
            metric TEXT NOT NULL,
            dataset_fingerprint TEXT NOT NULL,
            model_id TEXT NOT NULL,
            holdout_partition TEXT NOT NULL,
            scoring TEXT NOT NULL,
            prediction_digest TEXT NOT NULL,
            evaluated_dates INTEGER NOT NULL,
            evaluated_rows INTEGER NOT NULL,
            dropped_dates INTEGER NOT NULL,
            mean_rank_ic TEXT,
            ic_std TEXT,
            ic_std_error TEXT,
            ic_lower_bound TEXT,
            confidence_label TEXT NOT NULL,
            confidence_z TEXT,
            per_date_ic TEXT NOT NULL,
            exclusion_reasons TEXT NOT NULL,
            evaluation_blockers TEXT NOT NULL,
            evaluation_contract_ok INTEGER NOT NULL,
            execution_authority TEXT NOT NULL,
            evaluation_scope TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        EVALUATION_MANIFEST_TABLE,
        {
            "model_version": "TEXT",
            "model_artifact_fingerprint": "TEXT",
            "training_dataset_fingerprint": "TEXT",
            "trained_through": "TEXT",
            "selection_partition": "TEXT",
            "hyperparameters_fingerprint": "TEXT",
            "random_seed": "TEXT",
            "provenance_fingerprint": "TEXT",
            "coverage_ratio": "TEXT",
            "expected_prediction_rows": "INTEGER",
            "observed_prediction_rows": "INTEGER",
            "missing_prediction_rows": "INTEGER",
            "holdout_date_count": "INTEGER",
            "holdout_mean_rank_ic": "TEXT",
            "holdout_positive_ratio": "TEXT",
        },
    )
    return {
        "prediction_table": True,
        "model_provenance_table": True,
        "evaluation_manifest_table": True,
    }


# ─────────────────────── immutable prediction evidence ───────────────────────


def prediction_identity(
    dataset_fingerprint: Any,
    model_id: Any,
    sample_key: Any,
    score: Any,
    *,
    model_version: Any = None,
    model_artifact_fingerprint: Any = None,
    code: Any = None,
    partition: Any = None,
    label_start_date: Any = None,
    prediction_asof: Any = None,
    source: Any = None,
    contract_version: str = EVALUATION_CONTRACT_VERSION,
) -> str:
    """Content address of one prediction over its *complete* material evidence.

    The identity is derived from the content -- which contract version, which
    dataset, which model *artifact*, which logical sample, which score, and
    which availability instant -- and never from row position.  Replaying the
    same prediction on a later day therefore maps to the same id and ``INSERT OR
    IGNORE`` makes the append a no-op.

    Two contradictory records for the same logical identity produce two
    different ids, which is precisely how the gate detects the conflict instead
    of silently keeping whichever row arrived last.  Note that a prediction
    recorded without an availability instant therefore can *never* collide with
    the same prediction recorded with one.
    """
    payload = {
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "contract_version": str(_text(contract_version) or EVALUATION_CONTRACT_VERSION),
        "dataset_fingerprint": str(_text(dataset_fingerprint) or ""),
        "model_id": str(_text(model_id) or ""),
        "model_version": str(_text(model_version) or ""),
        "model_artifact_fingerprint": str(_text(model_artifact_fingerprint) or ""),
        "sample_key": str(_text(sample_key) or ""),
        "code": str(_text(code) or ""),
        "partition": str(_text(partition) or ""),
        "label_start_date": _iso_date(label_start_date),
        "score": _canon_number(score),
        "prediction_asof": LD.normalize_availability(prediction_asof),
        "source": str(_text(source) or ""),
    }
    return _digest(payload, domain="learning-prediction-evidence")


def normalize_prediction(
    raw: Mapping[str, Any], *, contract_version: str = EVALUATION_CONTRACT_VERSION
) -> dict:
    """Normalize one prediction into an immutable, content-addressed row.

    Nothing is dropped here -- an unparseable score, or a missing availability
    instant, still produces a row, so the gate can *audit and refuse* it.
    Silently discarding malformed evidence is exactly the failure mode this
    layer exists to prevent.
    """
    record = {name: raw.get(name) for name in _PREDICTION_FIELDS}
    normalized = {
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "contract_version": _text(record.get("contract_version")) or contract_version,
        "source": _text(record.get("source")) or "shadow_model",
        "dataset_fingerprint": _text(record.get("dataset_fingerprint")) or "",
        "model_id": _text(record.get("model_id")) or "",
        "model_version": _text(record.get("model_version")) or "",
        "model_artifact_fingerprint": _text(record.get("model_artifact_fingerprint")) or "",
        "sample_key": _text(record.get("sample_key")) or "",
        "code": _text(record.get("code")) or "",
        "partition": _text(record.get("partition")) or "",
        "label_start_date": _iso_date(record.get("label_start_date")),
        "score": _canon_number(record.get("score")),
        "prediction_asof": LD.normalize_availability(record.get("prediction_asof")),
    }
    normalized["prediction_id"] = prediction_identity(
        normalized["dataset_fingerprint"],
        normalized["model_id"],
        normalized["sample_key"],
        normalized["score"],
        model_version=normalized["model_version"],
        model_artifact_fingerprint=normalized["model_artifact_fingerprint"],
        code=normalized["code"],
        partition=normalized["partition"],
        label_start_date=normalized["label_start_date"],
        prediction_asof=normalized["prediction_asof"],
        source=normalized["source"],
        contract_version=normalized["contract_version"],
    )
    return normalized


def record_predictions(
    conn: sqlite3.Connection, predictions: Sequence[Mapping[str, Any]], *, persist: bool = True
) -> dict:
    """Append prediction evidence.  Re-recording the same prediction is a no-op.

    Append-only by construction: ``prediction_id`` is the primary key and the
    insert is ``INSERT OR IGNORE``, so a replay can never rewrite the facts a
    first run recorded.
    """
    rows = [normalize_prediction(item) for item in predictions]
    report = {
        "received": len(rows),
        "distinct": len({row["prediction_id"] for row in rows}),
        "inserted": 0,
        "persisted": bool(persist),
    }
    if not persist:
        return report
    ensure_schema(conn)
    created_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    inserted = 0
    for row in rows:
        cursor = conn.execute(
            f"""
            INSERT OR IGNORE INTO {PREDICTION_EVIDENCE_TABLE}(
                prediction_id, evaluation_schema_version, contract_version, source,
                dataset_fingerprint, model_id, model_version, model_artifact_fingerprint,
                sample_key, code, partition, label_start_date, score, prediction_asof,
                created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["prediction_id"],
                row["evaluation_schema_version"],
                row["contract_version"],
                row["source"],
                row["dataset_fingerprint"],
                row["model_id"],
                row["model_version"],
                row["model_artifact_fingerprint"],
                row["sample_key"],
                row["code"],
                row["partition"],
                row["label_start_date"],
                row["score"],
                row["prediction_asof"],
                created_at,
            ),
        )
        inserted += int(cursor.rowcount or 0)
    report["inserted"] = inserted
    return report


def read_prediction_evidence(
    conn: sqlite3.Connection, *, max_rows: Optional[int] = None
) -> tuple:
    """Read persisted prediction evidence with an explicit truncation verdict.

    Reads ``max_rows + 1`` rows so an evidence set that is *exactly* ``max_rows``
    long is not mistaken for a truncated one; the returned list still never
    exceeds ``max_rows``.  Returns ``(rows, truncated)``.
    """
    if not _table_columns(conn, PREDICTION_EVIDENCE_TABLE):
        return [], False
    order = ", ".join(STABLE_PREDICTION_ORDER)
    limit_clause = ""
    args: tuple = ()
    fetch_limit = None
    if max_rows is not None and int(max_rows) > 0:
        fetch_limit = int(max_rows) + 1
        limit_clause = " LIMIT ?"
        args = (fetch_limit,)
    rows = _rows_as_dicts(
        conn,
        f"SELECT * FROM {PREDICTION_EVIDENCE_TABLE} ORDER BY {order}{limit_clause}",
        args,
    )
    truncated = fetch_limit is not None and len(rows) > int(max_rows)
    if truncated:
        rows = rows[: int(max_rows)]
    return rows, truncated


# ─────────────────────────── model provenance ───────────────────────────


def normalize_model_provenance(
    raw: Mapping[str, Any], *, contract_version: str = EVALUATION_CONTRACT_VERSION
) -> dict:
    """Normalize one model's training provenance into a content-addressed row.

    Provenance is *declared* by the producer and never inferred by the
    evaluator: an evaluator that guessed ``trained_through`` from the dataset it
    is judging would be marking its own homework.  Anything unparseable is kept
    as ``None`` so the boundary checks below fail closed.
    """
    record = {name: (raw or {}).get(name) for name in _PROVENANCE_FIELDS}
    normalized = {
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "contract_version": _text(record.get("contract_version")) or contract_version,
        "source": _text(record.get("source")) or "shadow_model",
        "model_id": _text(record.get("model_id")) or "",
        "model_version": _text(record.get("model_version")) or "",
        "model_artifact_fingerprint": _text(record.get("model_artifact_fingerprint")) or "",
        "training_dataset_fingerprint": _text(record.get("training_dataset_fingerprint")) or "",
        "trained_through": _iso_date(record.get("trained_through")),
        "selection_partition": _text(record.get("selection_partition")) or "",
        "hyperparameters_fingerprint": _text(record.get("hyperparameters_fingerprint")) or "",
        "random_seed": _canon_number(record.get("random_seed")),
    }
    normalized["provenance_fingerprint"] = _digest(
        {name: normalized[name] for name in _PROVENANCE_MATERIAL_FIELDS},
        domain="learning-model-provenance",
    )
    return normalized


def record_model_provenance(conn: sqlite3.Connection, provenance: Any, *, persist: bool = True) -> dict:
    """Append model provenance.  Re-recording identical provenance is a no-op."""
    if isinstance(provenance, Mapping):
        items = [provenance]
    else:
        items = list(provenance or [])
    rows = [normalize_model_provenance(item) for item in items]
    report = {
        "received": len(rows),
        "distinct": len({row["provenance_fingerprint"] for row in rows}),
        "inserted": 0,
        "persisted": bool(persist),
    }
    if not persist:
        return report
    ensure_schema(conn)
    created_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    inserted = 0
    for row in rows:
        cursor = conn.execute(
            f"""
            INSERT OR IGNORE INTO {MODEL_PROVENANCE_TABLE}(
                provenance_fingerprint, evaluation_schema_version, contract_version, source,
                model_id, model_version, model_artifact_fingerprint,
                training_dataset_fingerprint, trained_through, selection_partition,
                hyperparameters_fingerprint, random_seed, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["provenance_fingerprint"],
                row["evaluation_schema_version"],
                row["contract_version"],
                row["source"],
                row["model_id"],
                row["model_version"],
                row["model_artifact_fingerprint"],
                row["training_dataset_fingerprint"],
                row["trained_through"],
                row["selection_partition"],
                row["hyperparameters_fingerprint"],
                row["random_seed"],
                created_at,
            ),
        )
        inserted += int(cursor.rowcount or 0)
    report["inserted"] = inserted
    return report


def read_model_provenance(conn: sqlite3.Connection, *, max_rows: Optional[int] = None) -> tuple:
    """Read persisted model provenance with an explicit truncation verdict."""
    if not _table_columns(conn, MODEL_PROVENANCE_TABLE):
        return [], False
    order = ", ".join(STABLE_PROVENANCE_ORDER)
    limit_clause = ""
    args: tuple = ()
    fetch_limit = None
    if max_rows is not None and int(max_rows) > 0:
        fetch_limit = int(max_rows) + 1
        limit_clause = " LIMIT ?"
        args = (fetch_limit,)
    rows = _rows_as_dicts(
        conn,
        f"SELECT * FROM {MODEL_PROVENANCE_TABLE} ORDER BY {order}{limit_clause}",
        args,
    )
    truncated = fetch_limit is not None and len(rows) > int(max_rows)
    if truncated:
        rows = rows[: int(max_rows)]
    return rows, truncated


# ───────────────────────────── scoring contract ─────────────────────────────


def scoring_spec(
    *,
    holdout_partition: str = DEFAULT_HOLDOUT_PARTITION,
    min_dates: int = MIN_EVAL_DATES,
    min_codes_per_date: int = MIN_CODES_PER_DATE,
    min_mean_rank_ic: float = MIN_MEAN_RANK_IC,
    min_ic_lower_bound: float = MIN_IC_LOWER_BOUND,
    confidence_z: float = CONFIDENCE_Z,
    holdout_fraction: float = HOLDOUT_FRACTION,
    min_holdout_dates: int = MIN_HOLDOUT_DATES,
    min_holdout_positive_ratio: float = MIN_HOLDOUT_POSITIVE_RATIO,
) -> dict:
    """The frozen scoring parameters that participate in the fingerprint."""
    return {
        "metric": METRIC_SPEARMAN_RANK_IC,
        "holdout_partition": str(holdout_partition),
        "supported_holdout_partitions": list(SUPPORTED_HOLDOUT_PARTITIONS),
        "supported_selection_partitions": list(SUPPORTED_SELECTION_PARTITIONS),
        "cross_section_field": "label_start_date",
        "score_field": "score",
        "target_field": "target",
        "unit_of_observation": "date",
        "same_date_rows_are_not_independent": True,
        "tie_handling": "average_rank",
        "tails": "one_sided",
        "min_dates": int(min_dates),
        "min_codes_per_date": int(min_codes_per_date),
        "min_mean_rank_ic": float(min_mean_rank_ic),
        "min_ic_lower_bound": float(min_ic_lower_bound),
        "confidence_z": float(confidence_z),
        "confidence_label": CONFIDENCE_LABEL,
        "coverage_required": 1.0,
        "holdout_fraction": float(holdout_fraction),
        "min_holdout_dates": int(min_holdout_dates),
        "min_holdout_positive_ratio": float(min_holdout_positive_ratio),
        "holdout_retention_floor": float(HOLDOUT_RETENTION_FLOOR),
        "model_agnostic": True,
        "training_performed": False,
    }


def _evidence_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    """Content digest of the judged prediction evidence, order-independent.

    Rank IC is scale-invariant, so two different prediction sets can produce the
    same IC sequence.  The fingerprint still has to identify *which* evidence was
    judged, otherwise two different claims would share an audit record.
    """
    keys = sorted(str(row.get("prediction_id") or "") for row in rows)
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def evaluation_fingerprint(
    *,
    dataset_fingerprint: str,
    model_id: str,
    holdout_partition: str,
    scoring: Mapping[str, Any],
    per_date_ic: Mapping[str, float],
    evaluated_dates: int,
    evaluated_rows: int,
    mean_rank_ic: Optional[float],
    ic_std: Optional[float],
    ic_std_error: Optional[float],
    ic_lower_bound: Optional[float],
    evaluation_blockers: Sequence[str],
    prediction_digest: str = "",
    model_provenance: Optional[Mapping[str, Any]] = None,
    coverage: Optional[Mapping[str, Any]] = None,
    holdout: Optional[Mapping[str, Any]] = None,
    contract_version: str = EVALUATION_CONTRACT_VERSION,
) -> str:
    """SHA-256 over a canonical serialization of one evaluation verdict.

    ``created_at`` is deliberately absent: replaying the same predictions against
    the same dataset must reproduce the same fingerprint.  Everything material
    changes it -- including the *model artifact* provenance and the coverage /
    tail-robustness measurements, so a verdict can never be re-attributed to a
    different artifact.
    """
    payload = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "contract_version": contract_version,
        "dataset_fingerprint": str(dataset_fingerprint),
        "model_id": str(model_id),
        "model_provenance": _canon_json(dict(model_provenance or {})),
        "holdout_partition": str(holdout_partition),
        "scoring": _canon_json(dict(scoring)),
        "prediction_digest": str(prediction_digest),
        "coverage": _canon_json(dict(coverage or {})),
        "holdout": _canon_json(dict(holdout or {})),
        "per_date_ic": {str(key): _canon_number(value) for key, value in sorted(per_date_ic.items())},
        "evaluated_dates": int(evaluated_dates),
        "evaluated_rows": int(evaluated_rows),
        "mean_rank_ic": _canon_number(mean_rank_ic),
        "ic_std": _canon_number(ic_std),
        "ic_std_error": _canon_number(ic_std_error),
        "ic_lower_bound": _canon_number(ic_lower_bound),
        "evaluation_blockers": sorted(str(item) for item in evaluation_blockers),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ─────────────────── prediction admissibility predicates ───────────────────
# Each admissibility rule below is one small, named predicate so that it can be
# read (and negatively mutated) in isolation, rather than buried inside a loop
# where a silent loosening would be invisible.


def _binds_dataset(row: Mapping[str, Any], dataset_fingerprint: str) -> bool:
    """True only when the prediction was produced against this exact dataset.

    A missing dataset fingerprint makes the claim unprovable, not acceptable.
    """
    return bool(dataset_fingerprint) and row.get("dataset_fingerprint") == dataset_fingerprint


def _is_held_out(sample_partition: Optional[str], holdout_partition: str) -> bool:
    """True only for the chronological held-out partition.

    Scoring in-sample rows would measure fit, not out-of-sample skill.
    """
    return str(sample_partition) == str(holdout_partition)


def _matches_sample_identity(row: Mapping[str, Any], sample: Any) -> bool:
    """True only when the prediction's self-declared identity is the sample's.

    Both the exchange code and the label start date must match: a prediction
    that points at the right *row slot* but describes a different instrument or
    a different date is a wiring bug, not evidence.
    """
    return (
        row.get("label_start_date") == getattr(sample, "label_start_date", None)
        and row.get("code") == getattr(sample, "code", None)
    )


def _unproven_availability(row: Mapping[str, Any], sample: Any) -> bool:
    """True when the prediction's availability instant was never proven.

    An unpredateable claim cannot be checked against the label it claims to
    forecast, so it is refused rather than assumed timely.
    """
    return _availability_instant(row.get("prediction_asof")) is None or _availability_instant(
        getattr(sample, "label_available_at", None)
    ) is None


def _is_future_prediction(row: Mapping[str, Any], sample: Any, cutoff: Optional[str]) -> bool:
    """True when the score was not knowable before the label it predicts.

    The strong rule is the label-availability rule: a prediction stamped at or
    after the moment the label became available has already seen the answer.
    The cutoff rule is the dataset-freeze companion -- anything stamped after
    the frozen cutoff cannot be part of a reproducible verdict either.
    """
    predicted = _availability_instant(row.get("prediction_asof"))
    available = _availability_instant(getattr(sample, "label_available_at", None))
    if predicted is None or available is None:
        # Unprovable ordering is a leak, never a pass.
        return True
    if predicted >= available:
        return True
    cutoff_instant = _availability_instant(cutoff)
    if cutoff_instant is not None and predicted > cutoff_instant:
        return True
    return False


def _holdout_key(sample_key: Any, code: Any, label_start_date: Any) -> str:
    """Canonical identity of one held-out sample: key *and* its declared facts."""
    return "|".join(
        [
            str(_text(sample_key) or ""),
            str(_text(code) or ""),
            str(_iso_date(label_start_date) or ""),
        ]
    )


@dataclass(slots=True)
class EvaluationBuild:
    """Result of one evaluation run; carries the manifest and its fingerprint."""

    dataset_fingerprint: str
    model_id: str
    holdout_partition: str
    scoring: dict
    per_date_ic: dict
    evaluated_dates: int
    evaluated_rows: int
    dropped_dates: int
    mean_rank_ic: Optional[float]
    ic_std: Optional[float]
    ic_std_error: Optional[float]
    ic_lower_bound: Optional[float]
    exclusions: dict
    manifest: dict
    fingerprint: str
    prediction_digest: str = ""
    truncated: bool = False
    metrics: dict = field(default_factory=dict)
    model_provenance: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    holdout: dict = field(default_factory=dict)

    @property
    def contract_ok(self) -> bool:
        return not self.manifest["evaluation_blockers"]

    @property
    def blockers(self) -> list:
        return list(self.manifest["evaluation_blockers"])


def build_evaluation(
    dataset: Any,
    predictions: Sequence[Mapping[str, Any]],
    *,
    model_id: Optional[str] = None,
    holdout_partition: str = DEFAULT_HOLDOUT_PARTITION,
    model_provenance: Optional[Mapping[str, Any]] = None,
    provenance_pool: Sequence[Mapping[str, Any]] = (),
    provenance_truncated: bool = False,
    dataset_blockers: Sequence[str] = (),
    min_dates: int = MIN_EVAL_DATES,
    min_codes_per_date: int = MIN_CODES_PER_DATE,
    min_mean_rank_ic: float = MIN_MEAN_RANK_IC,
    min_ic_lower_bound: float = MIN_IC_LOWER_BOUND,
    confidence_z: float = CONFIDENCE_Z,
    holdout_fraction: float = HOLDOUT_FRACTION,
    min_holdout_dates: int = MIN_HOLDOUT_DATES,
    min_holdout_positive_ratio: float = MIN_HOLDOUT_POSITIVE_RATIO,
    contract_version: str = EVALUATION_CONTRACT_VERSION,
    truncated: bool = False,
) -> EvaluationBuild:
    """Score one dataset against one model's predictions, fail-closed.

    A strictly read-only, side-effect-free function: it never trains, never
    persists and never mutates its inputs.  Every prediction it refuses is
    counted under a machine-readable reason, so nothing disappears silently.
    """
    dataset_fingerprint = _text(getattr(dataset, "fingerprint", None)) or ""
    cutoff = _iso_date(getattr(dataset, "cutoff", None))
    holdout = _text(holdout_partition) or DEFAULT_HOLDOUT_PARTITION
    scoring = scoring_spec(
        holdout_partition=holdout,
        min_dates=min_dates,
        min_codes_per_date=min_codes_per_date,
        min_mean_rank_ic=min_mean_rank_ic,
        min_ic_lower_bound=min_ic_lower_bound,
        confidence_z=confidence_z,
        holdout_fraction=holdout_fraction,
        min_holdout_dates=min_holdout_dates,
        min_holdout_positive_ratio=min_holdout_positive_ratio,
    )

    sample_index = {}
    sample_partitions = {}
    for name in LD.PARTITIONS:
        for sample in (getattr(dataset, "partitions", {}) or {}).get(name) or []:
            sample_index[sample.sample_key] = sample
            sample_partitions[sample.sample_key] = name

    # ── the canonical held-out set: what *must* carry a prediction.  Coverage
    #    is measured against the dataset's own partition, not against whatever
    #    the predictor chose to emit, which is what makes cherry-picking
    #    impossible. ──
    holdout_samples = [
        sample
        for sample in ((getattr(dataset, "partitions", {}) or {}).get(holdout) or [])
    ]
    expected_keys = frozenset(
        _holdout_key(sample.sample_key, sample.code, sample.label_start_date)
        for sample in holdout_samples
    )
    test_start = min(
        (_iso_date(sample.label_start_date) for sample in holdout_samples), default=None
    )

    exclusions = {reason: 0 for reason in PREDICTION_EXCLUSION_REASONS}

    ordered = sorted(
        (normalize_prediction(item, contract_version=contract_version) for item in predictions),
        key=lambda row: (
            str(row["model_id"]),
            str(row["label_start_date"]),
            str(row["sample_key"]),
            str(row["prediction_id"]),
        ),
    )

    # ── dataset-fingerprint binding: a prediction from another dataset is not
    #    evidence about this one, whatever its score. ──
    bound = []
    for row in ordered:
        if not _binds_dataset(row, dataset_fingerprint):
            exclusions["unbound_prediction"] += 1
            continue
        if not row["model_id"]:
            # No model attribution means the row cannot be compared against any
            # model's out-of-sample claim.
            exclusions["missing_model_id"] += 1
            continue
        bound.append(row)

    requested_model = _text(model_id)
    model_ids = sorted({row["model_id"] for row in bound})
    ambiguous = bool(model_ids) and len(model_ids) > 1 and requested_model is None
    resolved_model = requested_model or (model_ids[0] if len(model_ids) == 1 else None)
    if resolved_model is not None:
        kept = []
        for row in bound:
            if row["model_id"] == resolved_model:
                kept.append(row)
            else:
                exclusions["other_model"] += 1
        bound = kept
    elif bound:
        # More than one model and no explicit choice: attributing these rows to
        # any single model would be a coin flip, so none of them are scored.
        exclusions["unattributed_model"] += len(bound)
        bound = []

    # ── model provenance: which artifact produced these scores, trained on
    #    what, through when, and selected on which partition.  Unknown is a
    #    refusal -- a verdict that cannot name its artifact is not auditable. ──
    if model_provenance is not None:
        candidates = [normalize_model_provenance(model_provenance)]
    else:
        candidates = [
            normalize_model_provenance(row)
            for row in (provenance_pool or ())
            if normalize_model_provenance(row)["model_id"] == (resolved_model or "")
        ]
    distinct_provenance = {row["provenance_fingerprint"] for row in candidates}
    provenance_ambiguous = len(distinct_provenance) > 1
    provenance = candidates[0] if len(distinct_provenance) == 1 else None
    if provenance is not None and resolved_model and provenance["model_id"] != resolved_model:
        # Provenance for a different model cannot be used to vouch for these.
        provenance = None
        provenance_ambiguous = True
        distinct_provenance = set()
    declared_artifact = str((provenance or {}).get("model_artifact_fingerprint") or "")

    # ── artifact attribution: when the model's provenance names the artifact
    #    that produced it, a score from a *different* artifact is not evidence
    #    about that model -- whatever its model_id claims.  Without this the
    #    verdict could be stamped with artifact A while judging artifact B,
    #    which is exactly the mis-attribution section C exists to prevent. ──
    if declared_artifact:
        kept = []
        for row in bound:
            if row["model_artifact_fingerprint"] == declared_artifact:
                kept.append(row)
            else:
                exclusions["unattributed_model_artifact"] += 1
        bound = kept

    # ── conflicting evidence: for one logical identity -- dataset, *model
    #    artifact*, sample -- two materially different records cannot both be
    #    true, and picking one would make the verdict order-dependent.  Because
    #    prediction_id covers the whole material payload, "more than one id for
    #    one identity" *is* the conflict. ──
    ids_by_identity: dict = {}
    for row in bound:
        identity = (
            row["dataset_fingerprint"],
            row["model_artifact_fingerprint"],
            row["sample_key"],
        )
        ids_by_identity.setdefault(identity, set()).add(row["prediction_id"])
    conflicting = frozenset(
        identity for identity, seen in ids_by_identity.items() if len(seen) > 1
    )

    accepted = []
    seen_ids = set()
    for row in bound:
        identity = (
            row["dataset_fingerprint"],
            row["model_artifact_fingerprint"],
            row["sample_key"],
        )
        if identity in conflicting:
            exclusions["prediction_conflict"] += 1
            continue
        if row["prediction_id"] in seen_ids:
            exclusions["duplicate_prediction"] += 1
            continue
        if row["score"] is None:
            exclusions["invalid_prediction_score"] += 1
            continue
        sample = sample_index.get(row["sample_key"])
        if sample is None:
            exclusions["unknown_sample"] += 1
            continue
        if row["partition"] != sample_partitions.get(row["sample_key"]):
            # A prediction that mislabels its own partition is evidence of a
            # wiring bug, not of skill.
            exclusions["partition_mismatch"] += 1
            continue
        if not _is_held_out(sample_partitions.get(row["sample_key"]), holdout):
            exclusions["not_held_out"] += 1
            continue
        if not _matches_sample_identity(row, sample):
            exclusions["invalid_prediction_identity"] += 1
            continue
        if _unproven_availability(row, sample):
            # The prediction never proved *when* it was made, so it cannot be
            # shown to predate the label.  Silence is refused, not assumed.
            exclusions["unproven_prediction_availability"] += 1
            continue
        if _is_future_prediction(row, sample, cutoff):
            # The score was not knowable before the answer: an out-of-sample
            # claim built on it is a lookahead leak.
            exclusions["future_prediction"] += 1
            continue
        seen_ids.add(row["prediction_id"])
        accepted.append((row, sample))

    # ── coverage: every canonical held-out sample must be covered exactly
    #    once.  A model that scored only the winners it liked is not being
    #    measured, it is being advertised. ──
    observed_keys = frozenset(
        _holdout_key(row["sample_key"], row["code"], row["label_start_date"])
        for row, _sample in accepted
    )
    missing_keys = expected_keys - observed_keys
    unexpected_keys = observed_keys - expected_keys
    coverage_ratio = (
        len(observed_keys & expected_keys) / len(expected_keys) if expected_keys else 0.0
    )
    coverage = {
        "expected_prediction_rows": len(expected_keys),
        "observed_prediction_rows": len(observed_keys),
        "missing_prediction_rows": len(missing_keys),
        "unexpected_prediction_rows": len(unexpected_keys),
        "coverage_ratio": coverage_ratio,
        "coverage_complete": bool(expected_keys) and coverage_ratio == 1.0,
    }

    grouped: dict = {}
    for row, sample in accepted:
        grouped.setdefault(sample.label_start_date, []).append(
            (row["sample_key"], _finite(row["score"]), _finite(sample.target))
        )

    per_date_ic: dict = {}
    dropped_dates = 0
    for date in sorted(grouped):
        pairs = sorted(grouped[date], key=lambda item: item[0])
        if len(pairs) < int(min_codes_per_date):
            dropped_dates += 1
            continue
        ic = spearman_rank_ic(
            [pair[1] for pair in pairs], [pair[2] for pair in pairs]
        )
        if ic is None:
            # Degenerate cross-section: undefined, never a zero.
            dropped_dates += 1
            continue
        per_date_ic[date] = ic

    evaluated_dates = len(per_date_ic)
    evaluated_rows = sum(len(grouped[date]) for date in per_date_ic)
    mean_ic, ic_std, ic_std_error, ic_lower_bound = _mean_and_bound(
        [per_date_ic[date] for date in sorted(per_date_ic)], confidence_z
    )

    # ── chronological tail robustness: the most recent slice of the held-out
    #    window is scored separately, so a model that decayed still refuses. ──
    ic_dates = sorted(per_date_ic)
    tail_count = 0
    if ic_dates:
        tail_count = max(
            int(min_holdout_dates), int(math.ceil(float(holdout_fraction) * len(ic_dates)))
        )
        tail_count = min(tail_count, len(ic_dates))
    tail_dates = ic_dates[len(ic_dates) - tail_count:] if tail_count else []
    tail_values = [per_date_ic[date] for date in tail_dates]
    tail_mean = (sum(tail_values) / len(tail_values)) if tail_values else None
    positive_ratio = (
        sum(1 for value in tail_values if value > 0) / len(tail_values)
        if tail_values
        else None
    )
    retention = (
        tail_mean / mean_ic
        if (tail_mean is not None and mean_ic is not None and mean_ic > 0)
        else None
    )
    holdout_stats = {
        "holdout_start_date": tail_dates[0] if tail_dates else None,
        "holdout_date_count": len(tail_dates),
        "holdout_mean_rank_ic": tail_mean,
        "holdout_positive_ratio": positive_ratio,
        "holdout_retention": retention,
        "holdout_fraction": float(holdout_fraction),
    }

    # ── model provenance: which artifact produced these scores, trained on
    #    what, through when, and selected on which partition.  Resolved above,
    #    before the evidence was admitted, so the artifact filter could run. ──
    blockers = []
    if dataset_blockers:
        blockers.append("evaluation_dataset_contract_failed")
    if not dataset_fingerprint:
        blockers.append("evaluation_dataset_fingerprint_unavailable")
    if holdout not in SUPPORTED_HOLDOUT_PARTITIONS:
        # A typo'd or in-sample partition would otherwise look exactly like
        # "no evidence yet".  train/validation are refused outright.
        blockers.append("evaluation_holdout_partition_unsupported")
    if ambiguous or provenance_ambiguous:
        blockers.append("evaluation_model_ambiguous")
    elif resolved_model is None:
        blockers.append("evaluation_model_unresolved")
    if provenance_truncated:
        blockers.append("evaluation_model_provenance_truncated")

    # ── provable train / test separation ──
    if provenance is None:
        blockers.append("evaluation_training_boundary_unproven")
        blockers.append("evaluation_selection_partition_unproven")
    else:
        trained_through = provenance["trained_through"]
        boundary_declared = bool(
            provenance["model_version"]
            and provenance["model_artifact_fingerprint"]
            and provenance["training_dataset_fingerprint"]
        )
        if trained_through is None or test_start is None or not boundary_declared:
            # Without a declared training endpoint and a named artifact, the
            # separation cannot be proven -- and unproven separation is
            # indistinguishable from an in-sample claim.
            blockers.append("evaluation_training_boundary_unproven")
        if trained_through is not None and test_start is not None and trained_through >= test_start:
            blockers.append("evaluation_training_overlaps_test")
        selection = provenance["selection_partition"]
        if selection == holdout:
            blockers.append("evaluation_test_used_for_selection")
        elif selection not in SUPPORTED_SELECTION_PARTITIONS:
            blockers.append("evaluation_selection_partition_unproven")

    if conflicting:
        blockers.append("evaluation_prediction_conflict")
    if not accepted and ordered:
        # Predictions exist, but not one of them is admissible evidence for this
        # dataset's held-out partition.
        blockers.append("evaluation_no_held_out_predictions")
    if not expected_keys or missing_keys:
        blockers.append("evaluation_missing_predictions")
    if unexpected_keys:
        # Defensive invariant: an admitted prediction must belong to the
        # canonical held-out set.  Firing here means an identity filter was
        # bypassed, which is itself disqualifying.
        blockers.append("evaluation_unexpected_predictions")
    if evaluated_dates < int(min_dates):
        blockers.append("evaluation_insufficient_dates")
    if mean_ic is not None and mean_ic < float(min_mean_rank_ic):
        blockers.append("evaluation_mean_rank_ic_below_floor")
    if ic_lower_bound is None:
        blockers.append("evaluation_confidence_bound_unavailable")
    elif ic_lower_bound <= float(min_ic_lower_bound):
        blockers.append("evaluation_confidence_bound_not_positive")
    if len(tail_dates) < int(min_holdout_dates):
        blockers.append("evaluation_holdout_insufficient")
    if tail_mean is not None and tail_mean <= 0:
        blockers.append("evaluation_holdout_mean_not_positive")
    deteriorated = False
    if retention is not None and retention < float(HOLDOUT_RETENTION_FLOOR):
        deteriorated = True
    if positive_ratio is not None and positive_ratio < float(min_holdout_positive_ratio):
        deteriorated = True
    if deteriorated:
        blockers.append("evaluation_holdout_deterioration")
    if truncated:
        blockers.append("evaluation_predictions_read_truncated")
    blockers = sorted(set(blockers))

    prediction_digest = _evidence_digest(bound)
    provenance_payload = dict(provenance or {})

    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "evaluation_contract_version": contract_version,
        "metric": METRIC_SPEARMAN_RANK_IC,
        "dataset_fingerprint": dataset_fingerprint,
        "model_id": resolved_model or "",
        "holdout_partition": holdout,
        "scoring": scoring,
        "prediction_digest": prediction_digest,
        "evaluated_dates": evaluated_dates,
        "evaluated_rows": evaluated_rows,
        "dropped_dates": dropped_dates,
        "mean_rank_ic": _canon_number(mean_ic),
        "ic_std": _canon_number(ic_std),
        "ic_std_error": _canon_number(ic_std_error),
        "ic_lower_bound": _canon_number(ic_lower_bound),
        "confidence_label": CONFIDENCE_LABEL,
        "confidence_z": _canon_number(confidence_z),
        "per_date_ic": {date: _canon_number(per_date_ic[date]) for date in sorted(per_date_ic)},
        "exclusion_reasons": {key: int(value) for key, value in sorted(exclusions.items())},
        "evaluation_blockers": blockers,
        "evaluation_contract_ok": not blockers,
        "execution_authority": "none",
        "evaluation_scope": "research_read_only",
        # ── model / training provenance (section C & F) ──
        "model_version": provenance_payload.get("model_version", ""),
        "model_artifact_fingerprint": provenance_payload.get("model_artifact_fingerprint", ""),
        "training_dataset_fingerprint": provenance_payload.get("training_dataset_fingerprint", ""),
        "trained_through": provenance_payload.get("trained_through"),
        "selection_partition": provenance_payload.get("selection_partition", ""),
        "hyperparameters_fingerprint": provenance_payload.get("hyperparameters_fingerprint", ""),
        "random_seed": provenance_payload.get("random_seed"),
        "provenance_fingerprint": provenance_payload.get("provenance_fingerprint", ""),
        # ── coverage & tail robustness (section B & G) ──
        "coverage": dict(coverage),
        "holdout": dict(holdout_stats),
        # ``created_at`` describes the run, never the content, and is absent
        # from the fingerprint.
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    }
    fingerprint = evaluation_fingerprint(
        dataset_fingerprint=dataset_fingerprint,
        model_id=resolved_model or "",
        holdout_partition=holdout,
        scoring=scoring,
        per_date_ic=per_date_ic,
        evaluated_dates=evaluated_dates,
        evaluated_rows=evaluated_rows,
        mean_rank_ic=mean_ic,
        ic_std=ic_std,
        ic_std_error=ic_std_error,
        ic_lower_bound=ic_lower_bound,
        evaluation_blockers=blockers,
        prediction_digest=prediction_digest,
        model_provenance=provenance_payload,
        coverage=coverage,
        holdout=manifest["holdout"],
        contract_version=contract_version,
    )
    manifest["evaluation_fingerprint"] = fingerprint

    metrics = {
        "metric": METRIC_SPEARMAN_RANK_IC,
        "per_date_ic": {date: per_date_ic[date] for date in sorted(per_date_ic)},
        "evaluated_dates": evaluated_dates,
        "evaluated_rows": evaluated_rows,
        "dropped_dates": dropped_dates,
        "mean_rank_ic": mean_ic,
        "ic_std": ic_std,
        "ic_std_error": ic_std_error,
        "ic_lower_bound": ic_lower_bound,
        "confidence_z": float(confidence_z),
        "confidence_label": CONFIDENCE_LABEL,
        "unit_of_observation": "date",
        "holdout_partition": holdout,
        "prediction_digest": prediction_digest,
        "expected_prediction_rows": coverage["expected_prediction_rows"],
        "observed_prediction_rows": coverage["observed_prediction_rows"],
        "missing_prediction_rows": coverage["missing_prediction_rows"],
        "coverage_ratio": coverage_ratio,
        "holdout_date_count": holdout_stats["holdout_date_count"],
        "holdout_mean_rank_ic": holdout_stats["holdout_mean_rank_ic"],
        "holdout_positive_ratio": holdout_stats["holdout_positive_ratio"],
        "model_version": provenance_payload.get("model_version", ""),
        "model_artifact_fingerprint": provenance_payload.get("model_artifact_fingerprint", ""),
        "trained_through": provenance_payload.get("trained_through"),
        "selection_partition": provenance_payload.get("selection_partition", ""),
    }
    return EvaluationBuild(
        dataset_fingerprint=dataset_fingerprint,
        model_id=resolved_model or "",
        holdout_partition=holdout,
        scoring=scoring,
        per_date_ic=per_date_ic,
        evaluated_dates=evaluated_dates,
        evaluated_rows=evaluated_rows,
        dropped_dates=dropped_dates,
        mean_rank_ic=mean_ic,
        ic_std=ic_std,
        ic_std_error=ic_std_error,
        ic_lower_bound=ic_lower_bound,
        exclusions=exclusions,
        manifest=manifest,
        fingerprint=fingerprint,
        prediction_digest=prediction_digest,
        truncated=bool(truncated),
        metrics=metrics,
        model_provenance=provenance_payload,
        coverage=coverage,
        holdout=manifest["holdout"],
    )


def persist_evaluation_manifest(conn: sqlite3.Connection, manifest: Mapping[str, Any]) -> bool:
    """Append an evaluation manifest.  Re-running the same evaluation is a no-op."""
    ensure_schema(conn)
    coverage = dict(manifest.get("coverage") or {})
    holdout = dict(manifest.get("holdout") or {})
    cursor = conn.execute(
        f"""
        INSERT OR IGNORE INTO {EVALUATION_MANIFEST_TABLE}(
            evaluation_fingerprint, evaluation_schema_version, evaluation_contract_version,
            metric, dataset_fingerprint, model_id, holdout_partition, scoring,
            prediction_digest, evaluated_dates, evaluated_rows, dropped_dates, mean_rank_ic,
            ic_std, ic_std_error, ic_lower_bound, confidence_label, confidence_z, per_date_ic,
            exclusion_reasons, evaluation_blockers, evaluation_contract_ok,
            execution_authority, evaluation_scope, created_at,
            model_version, model_artifact_fingerprint, training_dataset_fingerprint,
            trained_through, selection_partition, hyperparameters_fingerprint, random_seed,
            provenance_fingerprint, coverage_ratio, expected_prediction_rows,
            observed_prediction_rows, missing_prediction_rows, holdout_date_count,
            holdout_mean_rank_ic, holdout_positive_ratio
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            manifest["evaluation_fingerprint"],
            manifest["evaluation_schema_version"],
            manifest["evaluation_contract_version"],
            manifest["metric"],
            manifest["dataset_fingerprint"],
            manifest["model_id"],
            manifest["holdout_partition"],
            json.dumps(manifest["scoring"], ensure_ascii=False, sort_keys=True),
            manifest["prediction_digest"],
            manifest["evaluated_dates"],
            manifest["evaluated_rows"],
            manifest["dropped_dates"],
            manifest.get("mean_rank_ic"),
            manifest.get("ic_std"),
            manifest.get("ic_std_error"),
            manifest.get("ic_lower_bound"),
            manifest["confidence_label"],
            manifest.get("confidence_z"),
            json.dumps(manifest["per_date_ic"], ensure_ascii=False, sort_keys=True),
            json.dumps(manifest["exclusion_reasons"], ensure_ascii=False, sort_keys=True),
            json.dumps(manifest["evaluation_blockers"], ensure_ascii=False),
            int(bool(manifest["evaluation_contract_ok"])),
            manifest["execution_authority"],
            manifest["evaluation_scope"],
            manifest["created_at"],
            manifest.get("model_version"),
            manifest.get("model_artifact_fingerprint"),
            manifest.get("training_dataset_fingerprint"),
            manifest.get("trained_through"),
            manifest.get("selection_partition"),
            manifest.get("hyperparameters_fingerprint"),
            manifest.get("random_seed"),
            manifest.get("provenance_fingerprint"),
            _canon_number(coverage.get("coverage_ratio")),
            coverage.get("expected_prediction_rows"),
            coverage.get("observed_prediction_rows"),
            coverage.get("missing_prediction_rows"),
            holdout.get("holdout_date_count"),
            _canon_number(holdout.get("holdout_mean_rank_ic")),
            _canon_number(holdout.get("holdout_positive_ratio")),
        ),
    )
    return bool(cursor.rowcount)


def read_evaluation_manifest(
    conn: sqlite3.Connection, fingerprint: str
) -> Optional[dict]:
    manifest = _row_as_dict(
        conn,
        f"SELECT * FROM {EVALUATION_MANIFEST_TABLE} WHERE evaluation_fingerprint=?",
        (fingerprint,),
    )
    if manifest is None:
        return None
    for key in ("scoring", "per_date_ic", "exclusion_reasons", "evaluation_blockers"):
        if isinstance(manifest.get(key), str):
            try:
                manifest[key] = json.loads(manifest[key])
            except ValueError:
                pass
    return manifest


# ─────────────────────────────── build entry ───────────────────────────────


@dataclass(slots=True)
class _DatasetView:
    """Minimal read-only stand-in used when the dataset contract already failed."""

    fingerprint: str = ""
    cutoff: Optional[str] = None
    partitions: dict = field(default_factory=lambda: {name: [] for name in LD.PARTITIONS})


def _evaluate(
    conn: sqlite3.Connection,
    *,
    cutoff: Any = None,
    model_id: Optional[str] = None,
    holdout_partition: str = DEFAULT_HOLDOUT_PARTITION,
    max_evidence_rows: Optional[int] = None,
    max_prediction_rows: Optional[int] = None,
    max_provenance_rows: Optional[int] = None,
    persist: bool = False,
) -> EvaluationBuild:
    """Shared evaluation path used by both the explicit runner and the gate."""
    dataset_status = LD.contract_status(conn, cutoff=cutoff, max_evidence_rows=max_evidence_rows)
    dataset_blockers = list(dataset_status.get("dataset_blockers") or [])
    if dataset_blockers:
        # No research-grade dataset means no scientific evaluation, so the
        # prediction read is not even attempted.
        return build_evaluation(
            _DatasetView(
                fingerprint=str(dataset_status.get("dataset_fingerprint") or ""),
                cutoff=_iso_date(dataset_status.get("cutoff")),
            ),
            [],
            model_id=model_id,
            holdout_partition=holdout_partition,
            dataset_blockers=dataset_blockers,
        )
    resolved_cutoff = _iso_date(dataset_status.get("cutoff"))
    dataset = LD.build_dataset(
        conn, cutoff=resolved_cutoff, max_evidence_rows=max_evidence_rows, persist=False
    )
    predictions, truncated = read_prediction_evidence(conn, max_rows=max_prediction_rows)
    provenance_rows, provenance_truncated = read_model_provenance(
        conn, max_rows=max_provenance_rows
    )
    build = build_evaluation(
        dataset,
        predictions,
        model_id=model_id,
        holdout_partition=holdout_partition,
        provenance_pool=provenance_rows,
        provenance_truncated=provenance_truncated,
        truncated=truncated,
    )
    if persist:
        persist_evaluation_manifest(conn, build.manifest)
    return build


def evaluate_dataset(
    conn: sqlite3.Connection,
    *,
    cutoff: Any = None,
    model_id: Optional[str] = None,
    holdout_partition: str = DEFAULT_HOLDOUT_PARTITION,
    max_evidence_rows: Optional[int] = None,
    max_prediction_rows: Optional[int] = None,
    max_provenance_rows: Optional[int] = None,
    persist: bool = False,
) -> EvaluationBuild:
    """Evaluate persisted predictions against the strict PIT dataset.

    Pure measurement: no training, no provider call, no execution call, and no
    mutation of the dataset itself.  With ``persist=True`` the resulting
    evaluation manifest is appended (never rewritten).
    """
    cutoff_date = _iso_date(cutoff)
    if cutoff is not None and cutoff_date is None:
        raise ValueError("a parseable cutoff date is required to evaluate a dataset")
    return _evaluate(
        conn,
        cutoff=cutoff_date,
        model_id=model_id,
        holdout_partition=holdout_partition,
        max_evidence_rows=max_evidence_rows,
        max_prediction_rows=max_prediction_rows,
        max_provenance_rows=max_provenance_rows,
        persist=persist,
    )


# ─────────────────── read-only readiness gate for consumers ───────────────────


def _empty_metrics(holdout_partition: str) -> dict:
    return {
        "metric": METRIC_SPEARMAN_RANK_IC,
        "per_date_ic": {},
        "evaluated_dates": 0,
        "evaluated_rows": 0,
        "dropped_dates": 0,
        "mean_rank_ic": None,
        "ic_std": None,
        "ic_std_error": None,
        "ic_lower_bound": None,
        "confidence_z": float(CONFIDENCE_Z),
        "confidence_label": CONFIDENCE_LABEL,
        "unit_of_observation": "date",
        "holdout_partition": holdout_partition,
        "prediction_digest": None,
        "expected_prediction_rows": 0,
        "observed_prediction_rows": 0,
        "missing_prediction_rows": 0,
        "coverage_ratio": 0.0,
        "holdout_date_count": 0,
        "holdout_mean_rank_ic": None,
        "holdout_positive_ratio": None,
        "model_version": "",
        "model_artifact_fingerprint": "",
        "trained_through": None,
        "selection_partition": "",
    }


def _empty_coverage() -> dict:
    return {
        "expected_prediction_rows": 0,
        "observed_prediction_rows": 0,
        "missing_prediction_rows": 0,
        "unexpected_prediction_rows": 0,
        "coverage_ratio": 0.0,
        "coverage_complete": False,
    }


def contract_status(
    conn: sqlite3.Connection,
    *,
    cutoff: Any = None,
    model_id: Optional[str] = None,
    holdout_partition: str = DEFAULT_HOLDOUT_PARTITION,
    max_evidence_rows: Optional[int] = None,
    max_prediction_rows: Optional[int] = None,
    max_provenance_rows: Optional[int] = None,
) -> dict:
    """Lightweight, side-effect-free evaluation gate for readiness consumers.

    Returns ``evaluation_blockers``: the reasons the current evidence is not yet
    a scientific out-of-sample verdict.  A row count alone can never clear them.
    This function reads persistence only and never writes a manifest.
    """
    status = {
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
        "fingerprint_version": FINGERPRINT_VERSION,
        "metric": METRIC_SPEARMAN_RANK_IC,
        "holdout_partition": holdout_partition,
        "model_id": None,
        "dataset_fingerprint": None,
        "evaluation_fingerprint": None,
        "evaluation_contract_ok": False,
        "evaluation_admitted": False,
        "evaluation_status": "not_ready",
        "evaluation_metrics": _empty_metrics(holdout_partition),
        "evaluation_blockers": [],
        "exclusion_reasons": {},
        "coverage": _empty_coverage(),
        "holdout": {},
        "model_version": None,
        "model_artifact_fingerprint": None,
        "training_dataset_fingerprint": None,
        "trained_through": None,
        "selection_partition": None,
        "hyperparameters_fingerprint": None,
        "random_seed": None,
        "provenance_fingerprint": None,
        "truncated": False,
        "execution_authority": "none",
        "evaluation_scope": "research_read_only",
    }
    try:
        if not _table_columns(conn, PREDICTION_EVIDENCE_TABLE):
            status["evaluation_blockers"] = ["evaluation_prediction_table_missing"]
            return status
        build = _evaluate(
            conn,
            cutoff=cutoff,
            model_id=model_id,
            holdout_partition=holdout_partition,
            max_evidence_rows=max_evidence_rows,
            max_prediction_rows=max_prediction_rows,
            max_provenance_rows=max_provenance_rows,
            persist=False,
        )
    except Exception:
        # Fail closed: any error in the contract layer means "no scientific
        # evidence", never "assume admitted".
        status["evaluation_blockers"] = ["evaluation_contract_error"]
        return status

    provenance = dict(build.model_provenance or {})
    status["model_id"] = build.model_id or None
    status["dataset_fingerprint"] = build.dataset_fingerprint or None
    status["evaluation_fingerprint"] = build.fingerprint or None
    status["evaluation_metrics"] = dict(build.metrics)
    status["evaluation_blockers"] = list(build.blockers)
    status["exclusion_reasons"] = {
        key: int(value) for key, value in sorted(build.exclusions.items()) if value
    }
    status["coverage"] = dict(build.coverage)
    status["holdout"] = dict(build.holdout)
    status["model_version"] = provenance.get("model_version") or None
    status["model_artifact_fingerprint"] = provenance.get("model_artifact_fingerprint") or None
    status["training_dataset_fingerprint"] = provenance.get("training_dataset_fingerprint") or None
    status["trained_through"] = provenance.get("trained_through")
    status["selection_partition"] = provenance.get("selection_partition") or None
    status["hyperparameters_fingerprint"] = provenance.get("hyperparameters_fingerprint") or None
    status["random_seed"] = provenance.get("random_seed")
    status["provenance_fingerprint"] = provenance.get("provenance_fingerprint") or None
    status["truncated"] = bool(build.truncated)
    ok = not status["evaluation_blockers"]
    status["evaluation_contract_ok"] = ok
    status["evaluation_admitted"] = ok
    status["evaluation_status"] = "ready" if ok else "not_ready"
    return status


# ─────────────────────────────── self-check ───────────────────────────────


def _check_sample(code: str, asof: str, label_end: str, partition: str, target: float) -> Any:
    return LD.CanonicalSample(
        sample_key=LD.sample_key(LD.DATASET_KIND_ADAPTIVE_ALPHA, asof, code, 1),
        source=LD.DATASET_KIND_ADAPTIVE_ALPHA,
        source_version="self-check",
        code=code,
        strategy_id="",
        model_family="",
        feature_asof=asof,
        feature_available_at=asof + "T15:00:00",
        label_start_date=asof,
        label_end_date=label_end,
        label_available_at=label_end + "T15:00:00",
        horizon=1,
        horizon_semantics=LD.HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
        features={name: 0.5 for name in LD.DEFAULT_ALPHA_FEATURES},
        target=target,
        pit_status=LD.PIT_VERIFIED,
        quality_flags=(),
        provenance={},
        partition=partition,
    )


def _self_check() -> None:
    assert forbidden_dependencies() == [], forbidden_dependencies()
    assert spearman_rank_ic([1, 2, 3], [1, 2, 3]) == 1.0
    assert spearman_rank_ic([1, 2, 3], [3, 2, 1]) == -1.0
    assert spearman_rank_ic([1, 1, 1], [1, 2, 3]) is None
    assert _average_ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]
    # Only ``test`` may be scored; train/validation are refused outright.
    assert SUPPORTED_HOLDOUT_PARTITIONS == ("test",)
    assert set(SUPPORTED_HOLDOUT_PARTITIONS) < set(LD.PARTITIONS)

    fingerprint = "f" * 64
    dataset = _DatasetView(fingerprint=fingerprint, cutoff="2026-03-01")
    samples = []
    predictions = []
    days = ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09")
    for day in days:
        for offset in range(4):
            code = f"{offset:06d}"
            sample = _check_sample(code, day, "2026-01-20", "test", float(offset))
            samples.append(sample)
            predictions.append(
                {
                    "dataset_fingerprint": fingerprint,
                    "model_id": "shadow-a",
                    "model_version": "2026.01",
                    "model_artifact_fingerprint": "a" * 64,
                    "sample_key": sample.sample_key,
                    "code": code,
                    "partition": "test",
                    "label_start_date": day,
                    "score": float(offset),
                    "prediction_asof": day + "T15:20:00",
                    "source": "shadow_model",
                }
            )
    dataset.partitions = {"train": [], "validation": [], "test": samples}
    provenance = {
        "model_id": "shadow-a",
        "model_version": "2026.01",
        "model_artifact_fingerprint": "a" * 64,
        "training_dataset_fingerprint": "b" * 64,
        "trained_through": "2025-12-31",
        "selection_partition": "validation",
        "hyperparameters_fingerprint": "c" * 64,
        "random_seed": 7,
    }

    first = build_evaluation(dataset, predictions, model_provenance=provenance)
    reordered = build_evaluation(
        dataset, list(reversed(predictions)), model_provenance=provenance
    )
    assert first.fingerprint == reordered.fingerprint, "prediction order must not fork the fingerprint"
    assert first.contract_ok, first.blockers
    assert first.evaluated_dates == 5, first.per_date_ic
    assert abs(first.mean_rank_ic - 1.0) < 1e-12, first.mean_rank_ic
    assert first.coverage["coverage_ratio"] == 1.0, first.coverage
    assert first.manifest["execution_authority"] == "none"

    # train / validation are refused outright, even with a perfect IC.
    for partition in ("train", "validation"):
        refused = build_evaluation(
            dataset, predictions, holdout_partition=partition, model_provenance=provenance
        )
        assert "evaluation_holdout_partition_unsupported" in refused.blockers, refused.blockers
        assert not refused.contract_ok

    # No declared training boundary means no provable out-of-sample claim.
    boundary = build_evaluation(dataset, predictions)
    assert "evaluation_training_boundary_unproven" in boundary.blockers, boundary.blockers
    assert "evaluation_selection_partition_unproven" in boundary.blockers, boundary.blockers
    assert not boundary.contract_ok

    # Selection on the held-out partition is a refusal, not a footnote.
    leaked = build_evaluation(
        dataset, predictions, model_provenance=dict(provenance, selection_partition="test")
    )
    assert "evaluation_test_used_for_selection" in leaked.blockers, leaked.blockers

    # A training window that reaches the test partition is a refusal.
    overlap = build_evaluation(
        dataset, predictions, model_provenance=dict(provenance, trained_through="2026-01-07")
    )
    assert "evaluation_training_overlaps_test" in overlap.blockers, overlap.blockers
    assert "evaluation_training_boundary_unproven" not in overlap.blockers

    # Dropping one held-out prediction is cherry-picking, not a smaller sample.
    cherry_picked = build_evaluation(
        dataset, predictions[:-1], model_provenance=provenance
    )
    assert "evaluation_missing_predictions" in cherry_picked.blockers, cherry_picked.blockers
    assert cherry_picked.coverage["coverage_ratio"] < 1.0

    # An availability instant that was never proven cannot be scored.
    unproven = build_evaluation(
        dataset,
        [dict(row, prediction_asof=None) for row in predictions],
        model_provenance=provenance,
    )
    assert unproven.exclusions["unproven_prediction_availability"] == len(predictions)

    # A score from an artifact other than the one the provenance names is not
    # evidence about that model, however confidently it is labelled.
    foreign_artifact = build_evaluation(
        dataset,
        [dict(row, model_artifact_fingerprint="d" * 64) for row in predictions],
        model_provenance=provenance,
    )
    assert foreign_artifact.exclusions["unattributed_model_artifact"] == len(predictions)
    assert "evaluation_missing_predictions" in foreign_artifact.blockers

    # A prediction that belongs to another dataset is never counted.
    foreign = build_evaluation(dataset, predictions, model_provenance=provenance)
    assert foreign.contract_ok
    mismatched = build_evaluation(
        dataset,
        [dict(predictions[0], dataset_fingerprint="0" * 64)],
        model_provenance=provenance,
    )
    assert mismatched.exclusions["unbound_prediction"] == 1
    assert "evaluation_no_held_out_predictions" in mismatched.blockers
    print("learning_evaluation self-check: ok")


if __name__ == "__main__":
    _self_check()
