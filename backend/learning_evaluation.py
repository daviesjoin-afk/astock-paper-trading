# -*- coding: utf-8 -*-
"""Model-agnostic reproducible shadow evaluation gate for research evidence.

This module turns an already-built point-in-time dataset (see
:mod:`learning_dataset`) plus a set of *already generated* out-of-sample
predictions into one scientific, reproducible verdict:

    immutable prediction evidence
        -> dataset-fingerprint binding
        -> chronological held-out evaluation set
        -> per-date cross-sectional Spearman rank IC
        -> confidence lower bound over *date* units
        -> append-only evaluation manifest + SHA-256 fingerprint
        -> shadow admission gate (never execution authority)

It performs **no model training** and depends on **no ML library** (no numpy,
no pandas, no scikit-learn, no torch, no tensorflow).  It is a pure,
deterministic measurement device: give it the same dataset and the same
predictions and it must return the same verdict byte-for-byte, on any machine,
on any day.  Which model produced a score is deliberately opaque -- the gate
measures *evidence quality*, not any particular architecture.

Contracts enforced here:

    an evaluation is bound to exactly one dataset fingerprint
    a prediction from another dataset is never silently accepted
    prediction evidence is content-addressed and never overwritten
    two conflicting predictions for one identity fail closed
    only the chronological held-out partition may be scored
    a prediction created after the dataset cutoff is a lookahead leak
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


EVALUATION_SCHEMA_VERSION = "learning-evaluation-v1"
EVALUATION_CONTRACT_VERSION = "learning-evaluation-v1"
FINGERPRINT_VERSION = "sha256-canonical-v1"
MANIFEST_SCHEMA_VERSION = "learning-evaluation-manifest-v1"

METRIC_SPEARMAN_RANK_IC = "spearman_rank_ic"

# The only partition that may be scored.  Evaluating on ``train`` (or
# ``validation``) would measure in-sample fit, which is not out-of-sample
# evidence, so the default is fixed and part of the reproducibility contract.
DEFAULT_HOLDOUT_PARTITION = "test"
SUPPORTED_HOLDOUT_PARTITIONS = tuple(LD.PARTITIONS)

# Scientific floors.  Every one of these is a *date* count or a bound, never a
# row count: a million rows observed on two dates are two observations.
MIN_EVAL_DATES = 5
MIN_CODES_PER_DATE = 3
MIN_MEAN_RANK_IC = 0.02
MIN_IC_LOWER_BOUND = 0.0
CONFIDENCE_Z = 1.645
CONFIDENCE_LABEL = "one_sided_95pct"

PREDICTION_EVIDENCE_TABLE = "learning_prediction_evidence"
EVALUATION_MANIFEST_TABLE = "learning_evaluation_manifests"

PREDICTION_EXCLUSION_REASONS = (
    "unbound_prediction",
    "missing_model_id",
    "other_model",
    "unattributed_model",
    "invalid_prediction_score",
    "unknown_sample",
    "partition_mismatch",
    "not_held_out",
    "invalid_prediction_identity",
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
    "evaluation_prediction_conflict",
    "evaluation_no_held_out_predictions",
    "evaluation_insufficient_dates",
    "evaluation_mean_rank_ic_below_floor",
    "evaluation_confidence_bound_unavailable",
    "evaluation_confidence_bound_not_positive",
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


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


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
    """Idempotently create the prediction-evidence and evaluation-manifest tables.

    Purely additive: nothing is dropped, renamed or rewritten, and no historical
    value is fabricated.  Safe on an empty database and on a database that
    already holds earlier evaluation runs.
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
    return {"prediction_table": True, "evaluation_manifest_table": True}


# ─────────────────────── immutable prediction evidence ───────────────────────


def prediction_identity(
    dataset_fingerprint: Any, model_id: Any, sample_key: Any, score: Any
) -> str:
    """Content address of one prediction.

    The identity is derived from the *content* -- which dataset, which model,
    which logical sample, which score -- and never from row position, so the
    same prediction replayed on a later day maps to the same id and ``INSERT OR
    IGNORE`` makes the append a no-op.  Two contradictory scores for the same
    (dataset, model, sample) therefore produce *two different* ids, which is
    precisely how the gate is able to detect the conflict instead of silently
    keeping whichever row arrived last.
    """
    payload = "|".join(
        [
            str(_text(dataset_fingerprint) or ""),
            str(_text(model_id) or ""),
            str(_text(sample_key) or ""),
            str(_canon_number(score) if _canon_number(score) is not None else "none"),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_PREDICTION_FIELDS = (
    "dataset_fingerprint",
    "model_id",
    "sample_key",
    "code",
    "partition",
    "label_start_date",
    "score",
    "prediction_asof",
    "source",
    "contract_version",
)


def normalize_prediction(
    raw: Mapping[str, Any], *, contract_version: str = EVALUATION_CONTRACT_VERSION
) -> dict:
    """Normalize one prediction into an immutable, content-addressed row.

    Nothing is dropped here -- an unparseable score still produces a row, so the
    gate can *audit and refuse* it.  Silently discarding malformed evidence is
    exactly the failure mode this layer exists to prevent.
    """
    record = {name: raw.get(name) for name in _PREDICTION_FIELDS}
    score = _canon_number(record.get("score"))
    normalized = {
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "contract_version": _text(record.get("contract_version")) or contract_version,
        "source": _text(record.get("source")) or "shadow_model",
        "dataset_fingerprint": _text(record.get("dataset_fingerprint")) or "",
        "model_id": _text(record.get("model_id")) or "",
        "sample_key": _text(record.get("sample_key")) or "",
        "code": _text(record.get("code")) or "",
        "partition": _text(record.get("partition")) or "",
        "label_start_date": _iso_date(record.get("label_start_date")),
        "score": score,
        "prediction_asof": LD.normalize_availability(record.get("prediction_asof")),
    }
    normalized["prediction_id"] = prediction_identity(
        normalized["dataset_fingerprint"],
        normalized["model_id"],
        normalized["sample_key"],
        normalized["score"],
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
                dataset_fingerprint, model_id, sample_key, code, partition,
                label_start_date, score, prediction_asof, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["prediction_id"],
                row["evaluation_schema_version"],
                row["contract_version"],
                row["source"],
                row["dataset_fingerprint"],
                row["model_id"],
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


# ───────────────────────────── scoring contract ─────────────────────────────


def scoring_spec(
    *,
    holdout_partition: str = DEFAULT_HOLDOUT_PARTITION,
    min_dates: int = MIN_EVAL_DATES,
    min_codes_per_date: int = MIN_CODES_PER_DATE,
    min_mean_rank_ic: float = MIN_MEAN_RANK_IC,
    min_ic_lower_bound: float = MIN_IC_LOWER_BOUND,
    confidence_z: float = CONFIDENCE_Z,
) -> dict:
    """The frozen scoring parameters that participate in the fingerprint."""
    return {
        "metric": METRIC_SPEARMAN_RANK_IC,
        "holdout_partition": str(holdout_partition),
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
    contract_version: str = EVALUATION_CONTRACT_VERSION,
) -> str:
    """SHA-256 over a canonical serialization of one evaluation verdict.

    ``created_at`` is deliberately absent: replaying the same predictions against
    the same dataset must reproduce the same fingerprint.  Anything material --
    the dataset fingerprint, the model id, the held-out partition, the scoring
    floors, the judged prediction evidence, every per-date IC, the aggregates or
    the blockers -- changes it.
    """
    payload = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "contract_version": contract_version,
        "dataset_fingerprint": str(dataset_fingerprint),
        "model_id": str(model_id),
        "holdout_partition": str(holdout_partition),
        "scoring": _canon_json(dict(scoring)),
        "prediction_digest": str(prediction_digest),
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


def _is_future_prediction(row: Mapping[str, Any], cutoff: Optional[str]) -> bool:
    """True when the score only became knowable after the dataset was frozen."""
    stamp = row.get("prediction_asof")
    return bool(stamp and cutoff and str(stamp)[:10] > str(cutoff))


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
    dataset_blockers: Sequence[str] = (),
    min_dates: int = MIN_EVAL_DATES,
    min_codes_per_date: int = MIN_CODES_PER_DATE,
    min_mean_rank_ic: float = MIN_MEAN_RANK_IC,
    min_ic_lower_bound: float = MIN_IC_LOWER_BOUND,
    confidence_z: float = CONFIDENCE_Z,
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
    )

    sample_index = {}
    sample_partitions = {}
    for name in LD.PARTITIONS:
        for sample in (getattr(dataset, "partitions", {}) or {}).get(name) or []:
            sample_index[sample.sample_key] = sample
            sample_partitions[sample.sample_key] = name

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

    # ── conflicting evidence: two different scores for one identity cannot both
    #    be true, and picking one would make the verdict order-dependent. ──
    scores_by_identity: dict = {}
    for row in bound:
        scores_by_identity.setdefault(row["sample_key"], set()).add(
            row["score"] if row["score"] is not None else "none"
        )
    conflicting = frozenset(
        identity for identity, seen in scores_by_identity.items() if len(seen) > 1
    )

    accepted = []
    seen_ids = set()
    for row in bound:
        if row["sample_key"] in conflicting:
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
        if row["label_start_date"] != sample.label_start_date:
            exclusions["invalid_prediction_identity"] += 1
            continue
        if _is_future_prediction(row, cutoff):
            # The score only became knowable after the dataset was frozen: an
            # out-of-sample claim built on it is a lookahead leak.
            exclusions["future_prediction"] += 1
            continue
        seen_ids.add(row["prediction_id"])
        accepted.append((row, sample))

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

    blockers = []
    if dataset_blockers:
        blockers.append("evaluation_dataset_contract_failed")
    if not dataset_fingerprint:
        blockers.append("evaluation_dataset_fingerprint_unavailable")
    if holdout not in SUPPORTED_HOLDOUT_PARTITIONS:
        # A typo'd partition would otherwise look exactly like "no evidence".
        blockers.append("evaluation_holdout_partition_unsupported")
    if ambiguous:
        blockers.append("evaluation_model_ambiguous")
    elif resolved_model is None:
        blockers.append("evaluation_model_unresolved")
    if conflicting:
        blockers.append("evaluation_prediction_conflict")
    if not accepted and ordered:
        # Predictions exist, but not one of them is admissible evidence for this
        # dataset's held-out partition.
        blockers.append("evaluation_no_held_out_predictions")
    if evaluated_dates < int(min_dates):
        blockers.append("evaluation_insufficient_dates")
    if mean_ic is not None and mean_ic < float(min_mean_rank_ic):
        blockers.append("evaluation_mean_rank_ic_below_floor")
    if ic_lower_bound is None:
        blockers.append("evaluation_confidence_bound_unavailable")
    elif ic_lower_bound <= float(min_ic_lower_bound):
        blockers.append("evaluation_confidence_bound_not_positive")
    if truncated:
        blockers.append("evaluation_predictions_read_truncated")
    blockers = sorted(set(blockers))

    prediction_digest = _evidence_digest(bound)

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
    )


def persist_evaluation_manifest(conn: sqlite3.Connection, manifest: Mapping[str, Any]) -> bool:
    """Append an evaluation manifest.  Re-running the same evaluation is a no-op."""
    ensure_schema(conn)
    cursor = conn.execute(
        f"""
        INSERT OR IGNORE INTO {EVALUATION_MANIFEST_TABLE}(
            evaluation_fingerprint, evaluation_schema_version, evaluation_contract_version,
            metric, dataset_fingerprint, model_id, holdout_partition, scoring,
            prediction_digest, evaluated_dates, evaluated_rows, dropped_dates, mean_rank_ic,
            ic_std, ic_std_error, ic_lower_bound, confidence_label, confidence_z, per_date_ic,
            exclusion_reasons, evaluation_blockers, evaluation_contract_ok,
            execution_authority, evaluation_scope, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
    build = build_evaluation(
        dataset,
        predictions,
        model_id=model_id,
        holdout_partition=holdout_partition,
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
    }


def contract_status(
    conn: sqlite3.Connection,
    *,
    cutoff: Any = None,
    model_id: Optional[str] = None,
    holdout_partition: str = DEFAULT_HOLDOUT_PARTITION,
    max_evidence_rows: Optional[int] = None,
    max_prediction_rows: Optional[int] = None,
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
            persist=False,
        )
    except Exception:
        # Fail closed: any error in the contract layer means "no scientific
        # evidence", never "assume admitted".
        status["evaluation_blockers"] = ["evaluation_contract_error"]
        return status

    status["model_id"] = build.model_id or None
    status["dataset_fingerprint"] = build.dataset_fingerprint or None
    status["evaluation_fingerprint"] = build.fingerprint or None
    status["evaluation_metrics"] = dict(build.metrics)
    status["evaluation_blockers"] = list(build.blockers)
    status["exclusion_reasons"] = {
        key: int(value) for key, value in sorted(build.exclusions.items()) if value
    }
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

    fingerprint = "f" * 64
    dataset = _DatasetView(fingerprint=fingerprint, cutoff="2026-03-01")
    samples = []
    predictions = []
    for index, day in enumerate(("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09")):
        rows = []
        for offset in range(4):
            code = f"{offset:06d}"
            sample = _check_sample(code, day, "2026-01-20", "test", float(offset))
            rows.append(sample)
            predictions.append(
                {
                    "dataset_fingerprint": fingerprint,
                    "model_id": "shadow-a",
                    "sample_key": sample.sample_key,
                    "code": code,
                    "partition": "test",
                    "label_start_date": day,
                    "score": float(offset),
                }
            )
        samples.extend(rows)
    dataset.partitions = {"train": [], "validation": [], "test": samples}

    first = build_evaluation(dataset, predictions)
    reordered = build_evaluation(dataset, list(reversed(predictions)))
    assert first.fingerprint == reordered.fingerprint, "prediction order must not fork the fingerprint"
    assert first.contract_ok, first.blockers
    assert first.evaluated_dates == 5, first.per_date_ic
    assert abs(first.mean_rank_ic - 1.0) < 1e-12, first.mean_rank_ic
    assert first.manifest["execution_authority"] == "none"

    # A prediction that belongs to another dataset is never counted.
    foreign = build_evaluation(dataset, predictions, model_id=None)
    assert foreign.contract_ok
    mismatched = build_evaluation(
        dataset, [dict(predictions[0], dataset_fingerprint="0" * 64)]
    )
    assert mismatched.exclusions["unbound_prediction"] == 1
    assert "evaluation_no_held_out_predictions" in mismatched.blockers
    print("learning_evaluation self-check: ok")


if __name__ == "__main__":
    _self_check()
