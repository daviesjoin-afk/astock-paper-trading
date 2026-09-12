# -*- coding: utf-8 -*-
"""PR-9 contract tests: model-agnostic reproducible shadow evaluation gate.

Every test here is hermetic: an in-memory SQLite database, no network, and no
execution path.  The suite deliberately does not just assert "the happy path is
green" -- it asserts that the *refusals* happen and, more importantly, that the
statistics mean what they claim.  An evaluation gate that hands out out-of-sample
credibility for a row count, for a positive point estimate without a bound, for a
cherry-picked sample, or for a model whose training boundary nobody proved, is
worse than no gate at all.
"""

import ast
import datetime as dt
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, os.path.dirname(__file__))
import learning_dataset as LD  # noqa: E402
import learning_evaluation as LE  # noqa: E402
import neural_shadow as NS  # noqa: E402


FEATURES = LD.DEFAULT_ALPHA_FEATURES
CODES = ("000001", "000002", "000003", "000004")
DAYS = [(dt.date(2026, 1, 1) + dt.timedelta(days=offset)).isoformat() for offset in range(40)]
CUTOFF = DAYS[-1]

MODEL_ID = "shadow-a"
MODEL_VERSION = "shadow-2026.01"
MODEL_ARTIFACT = "a" * 64
OTHER_ARTIFACT = "d" * 64
TRAINING_DATASET = "b" * 64
HYPERPARAMETERS = "c" * 64
RANDOM_SEED = 7


def code_value(code):
    """Deterministic, code-width agnostic forward return: numeric suffix / 100.

    Forward returns depend only on the code, so a score derived from the same
    code ranks perfectly against them on *every* date.
    """
    return round(int(code) * 0.01, 4)

# The production (pre-PR-8) schema: no provenance columns at all.
LEGACY_SCHEMA = """
CREATE TABLE adaptive_alpha_samples(
    profile_date TEXT NOT NULL, code TEXT NOT NULL, industry TEXT,
    close_price REAL NOT NULL, regime TEXT NOT NULL, price_momentum REAL NOT NULL,
    main_flow REAL NOT NULL, turnover REAL NOT NULL, volume_ratio REAL NOT NULL,
    small_size REAL NOT NULL, value REAL NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(profile_date,code)
);
CREATE TABLE adaptive_alpha_returns(
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, horizon INTEGER NOT NULL,
    code TEXT NOT NULL, forward_return_pct REAL NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(start_date,end_date,horizon,code)
);
"""

SAMPLE_COLUMNS = (
    "profile_date,code,industry,close_price,regime,price_momentum,main_flow,"
    "turnover,volume_ratio,small_size,value,created_at,"
    "feature_asof,feature_available_at,pit_status,source,source_version,contract_version"
)


def open_db(*, prediction_table=True, config=True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    LD.ensure_schema(conn)
    if prediction_table:
        LE.ensure_schema(conn)
    if config:
        conn.execute("CREATE TABLE IF NOT EXISTS adaptive_config(key TEXT PRIMARY KEY, value TEXT)")
    return conn


class DbTestCase(unittest.TestCase):
    """Base class that guarantees every in-memory database is closed."""

    def new_db(self, **kwargs):
        conn = open_db(**kwargs)
        self.addCleanup(conn.close)
        return conn

    def proven(self, *, day_count=40, codes=CODES, target_fn=None, **kwargs):
        """Return a database whose dataset contract passes."""
        conn = self.new_db(**kwargs)
        seed_panel(conn, day_count=day_count, codes=codes, target_fn=target_fn)
        return conn


# ─────────────────────────────── seeding helpers ───────────────────────────────


def seed_sample(conn, day, code, *, asof=None, available="auto"):
    asof_value = day if asof is None else asof
    availability = f"{day}T15:15:00" if available == "auto" else available
    pit = LD.PIT_VERIFIED if availability else LD.PIT_LEGACY_UNPROVEN
    conn.execute(
        f"INSERT OR REPLACE INTO adaptive_alpha_samples({SAMPLE_COLUMNS}) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (day, code, "测试行业", 10.0, "neutral",
         *[0.5 for _ in FEATURES], f"{day}T16:00:00",
         asof_value, availability, pit, "market_snapshot_full.json", "test-v1",
         LD.CONTRACT_VERSION),
    )


def seed_label(conn, start, end, code, *, horizon=1, ret=1.0, available="auto", pit="auto"):
    availability = f"{end}T15:15:00" if available == "auto" else available
    status = LD.PIT_VERIFIED if pit == "auto" else pit
    conn.execute(
        """INSERT OR IGNORE INTO adaptive_alpha_returns(
               start_date,end_date,horizon,code,forward_return_pct,created_at,
               label_available_at,horizon_semantics,pit_status,source,source_version,
               contract_version)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (start, end, horizon, code, ret, f"{end}T16:00:00", availability,
         LD.HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS, status,
         LD.DATASET_KIND_ADAPTIVE_ALPHA, "test-v1", LD.CONTRACT_VERSION),
    )


def seed_panel(conn, *, day_count=40, codes=CODES, target_fn=None, horizons=(1,)):
    """Seed features plus mature forward labels over a consecutive date series."""
    days = DAYS[:day_count]
    target_fn = target_fn or (lambda index, code_index, code: code_value(code))
    for index, day in enumerate(days):
        for code in codes:
            seed_sample(conn, day, code)
    for horizon in horizons:
        for index in range(len(days) - horizon):
            for code in codes:
                seed_label(
                    conn, days[index], days[index + horizon], code,
                    horizon=horizon, ret=target_fn(index, codes.index(code), code),
                )
    return days


def alternating_target(index, code_index, code):
    """Per-date IC alternates +1 / -1 so the date-level dispersion is non-zero."""
    sign = 1.0 if index % 2 == 0 else -1.0
    return round(sign * code_value(code), 4)


def rank_permuted_target(permutation):
    """Return a target_fn producing a chosen per-date rank IC magnitude.

    ``permutation`` maps the score rank (by code) to the target rank; ranking
    four names as ``(0, 3, 2, 1)`` yields a per-date rank IC of exactly ``0.2``
    while ``(0, 1, 2, 3)`` yields exactly ``1.0``.
    """

    def target_fn(index, code_index, code):
        return code_value(CODES[permutation[code_index]])

    return target_fn


# ─────────────────────────────── build helpers ───────────────────────────────


def build_dataset(conn, cutoff=CUTOFF, **kwargs):
    return LD.build_dataset(conn, cutoff=cutoff, persist=False, **kwargs)


def score_by_code(sample):
    return code_value(sample.code)


def score_negative(sample):
    return -code_value(sample.code)


def score_constant(sample):
    return 0.0


def predictions_for(
    build,
    score_of=score_by_code,
    *,
    model_id=MODEL_ID,
    partition="test",
    dataset_fingerprint=None,
    prediction_asof="auto",
    **row_overrides,
):
    """One prediction per held-out sample, fully attributable and timed.

    Every row carries the artifact that produced it and a generation instant
    that precedes the label it predicts -- the two facts the v2 contract
    refuses to take on faith.
    """
    fingerprint = dataset_fingerprint if dataset_fingerprint is not None else build.fingerprint
    rows = []
    for sample in build.partitions.get(partition) or []:
        asof = f"{sample.label_start_date}T15:20:00" if prediction_asof == "auto" else prediction_asof
        row = {
            "dataset_fingerprint": fingerprint,
            "model_id": model_id,
            "model_version": MODEL_VERSION,
            "model_artifact_fingerprint": MODEL_ARTIFACT,
            "sample_key": sample.sample_key,
            "code": sample.code,
            "partition": partition,
            "label_start_date": sample.label_start_date,
            "score": score_of(sample),
            "prediction_asof": asof,
            "source": "shadow_model",
        }
        row.update(row_overrides)
        rows.append(row)
    return rows


def provenance_for(
    build,
    *,
    model_id=MODEL_ID,
    trained_through="auto",
    selection_partition="validation",
    **overrides,
):
    """Declared training provenance for one model.

    ``trained_through`` is taken from the last label the training and validation
    partitions actually consumed -- which the dataset contract guarantees stops
    before the test partition begins -- so the default is a *provable* boundary
    rather than a convenient one.
    """
    if trained_through == "auto":
        dates = [
            sample.label_end_date
            for name in ("train", "validation")
            for sample in (build.partitions.get(name) or [])
        ]
        trained_through = max(dates) if dates else None
    provenance = {
        "model_id": model_id,
        "model_version": MODEL_VERSION,
        "model_artifact_fingerprint": MODEL_ARTIFACT,
        "training_dataset_fingerprint": TRAINING_DATASET,
        "trained_through": trained_through,
        "selection_partition": selection_partition,
        "hyperparameters_fingerprint": HYPERPARAMETERS,
        "random_seed": RANDOM_SEED,
        "source": "shadow_model",
    }
    provenance.update(overrides)
    return provenance


def evaluate_build(build, predictions, *, model_id=None, provenance="auto", **kwargs):
    """``build_evaluation`` with a declared provenance unless a test opts out.

    ``provenance="none"`` means "this test is about the missing-provenance
    refusal"; ``provenance=<dict>`` injects a specific (possibly dishonest)
    claim.
    """
    if provenance == "auto":
        provenance = provenance_for(build, model_id=model_id or MODEL_ID)
    elif provenance == "none":
        provenance = None
    return LE.build_evaluation(
        build, predictions, model_id=model_id, model_provenance=provenance, **kwargs
    )


def evaluate(conn, score_of=score_by_code, *, model_id=None, **kwargs):
    """Build the dataset, evaluate the predictions in memory, return both."""
    build = build_dataset(conn)
    chosen = model_id or MODEL_ID
    build_eval = evaluate_build(
        build,
        predictions_for(build, score_of, model_id=chosen),
        model_id=chosen,
        **kwargs,
    )
    return build, build_eval


def record(conn, build, score_of=score_by_code, *, provenance="auto", **kwargs):
    """Persist prediction evidence (and, by default, its model provenance)."""
    predictions = predictions_for(build, score_of, **kwargs)
    LE.record_predictions(conn, predictions)
    if provenance != "skip":
        overrides = provenance if isinstance(provenance, dict) else {}
        LE.record_model_provenance(conn, provenance_for(build, **overrides))
    return predictions


def gate(conn, **kwargs):
    kwargs.setdefault("cutoff", CUTOFF)
    return LE.contract_status(conn, **kwargs)


def active_exclusions(build_eval):
    return {key: count for key, count in build_eval.exclusions.items() if count}


def test_dates(build):
    return sorted({sample.label_start_date for sample in build.partitions["test"]})


def test_start(build):
    dates = test_dates(build)
    return dates[0] if dates else None


# ─────────────────────── prediction identity / immutability ───────────────────────


class PredictionIdentityTests(DbTestCase):
    """Prediction evidence is content-addressed, append-only and unambiguous."""

    def test_prediction_identity_is_content_addressed_and_stable(self):
        first = LE.prediction_identity("f" * 64, "m1", "s1", 0.5)
        self.assertEqual(first, LE.prediction_identity("f" * 64, "m1", "s1", 0.5))
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, LE.prediction_identity("f" * 64, "m1", "s1", 0.6))
        self.assertNotEqual(first, LE.prediction_identity("f" * 64, "m2", "s1", 0.5))
        self.assertNotEqual(first, LE.prediction_identity("0" * 64, "m1", "s1", 0.5))
        self.assertNotEqual(first, LE.prediction_identity("f" * 64, "m1", "s2", 0.5))

    def test_identity_does_not_depend_on_row_position(self):
        rows = [LE.normalize_prediction({"model_id": "m", "sample_key": f"s{index}", "score": index})
                for index in range(5)]
        shuffled = list(reversed(rows))
        self.assertEqual(
            {row["prediction_id"] for row in rows},
            {row["prediction_id"] for row in shuffled},
        )

    def test_identical_prediction_is_never_duplicated(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        first = LE.record_predictions(conn, predictions)
        second = LE.record_predictions(conn, predictions)
        self.assertEqual(first["inserted"], len(predictions))
        self.assertEqual(second["inserted"], 0)
        count = conn.execute("SELECT COUNT(*) FROM learning_prediction_evidence").fetchone()[0]
        self.assertEqual(count, len(predictions))

    def test_conflicting_scores_for_one_identity_fail_closed(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        target = predictions[0]
        predictions.append(dict(target, score=target["score"] + 10.0))
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertIn("evaluation_prediction_conflict", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)
        # Both contradictory rows are refused; neither is picked by row order.
        self.assertEqual(active_exclusions(build_eval)["prediction_conflict"], 2)

    def test_conflicting_evidence_is_refused_regardless_of_insertion_order(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        target = predictions[0]
        conflicting = [target, dict(target, score=target["score"] + 1.0)]
        forward = evaluate_build(build, predictions + conflicting, model_id=MODEL_ID)
        backward = evaluate_build(build, conflicting + predictions, model_id=MODEL_ID)
        self.assertEqual(forward.blockers, backward.blockers)
        self.assertEqual(forward.fingerprint, backward.fingerprint)
        self.assertIn("evaluation_prediction_conflict", forward.blockers)

    def test_persisted_conflict_is_detected_by_the_gate(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = record(conn, build)
        LE.record_predictions(conn, [dict(predictions[0], score=predictions[0]["score"] - 3.0)])
        status = gate(conn)
        self.assertIn("evaluation_prediction_conflict", status["evaluation_blockers"])
        self.assertFalse(status["evaluation_contract_ok"])

    def test_non_finite_scores_are_audited_not_imputed(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        predictions[0] = dict(predictions[0], score=float("nan"))
        predictions[1] = dict(predictions[1], score=None)
        predictions[2] = dict(predictions[2], score="not-a-number")
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["invalid_prediction_score"], 3)
        for row in predictions[:3]:
            self.assertNotEqual(LE.normalize_prediction(row)["score"], "0")

    def test_missing_model_id_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        predictions[0] = dict(predictions[0], model_id="")
        predictions[1] = dict(predictions[1], model_id="")
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["missing_model_id"], 2)
        # An unattributed row is not silently ignored: the model's prediction
        # set is now incomplete, which is itself a refusal.
        self.assertIn("evaluation_missing_predictions", build_eval.blockers)
        self.assertEqual(build_eval.coverage["missing_prediction_rows"], 2)
        self.assertFalse(build_eval.contract_ok)

    def test_predictions_for_another_model_are_audited_not_scored(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, model_id=MODEL_ID)
        predictions += predictions_for(build, model_id="shadow-b")
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["other_model"], len(build.partitions["test"]))
        self.assertEqual(build_eval.evaluated_rows, len(build.partitions["test"]))


# ─────────────────────── E: material evidence in the identity ───────────────────────


class PredictionIdentityV2Tests(DbTestCase):
    """``prediction_id`` must bind the *whole* material claim, not just a score."""

    MATERIAL = {
        "dataset_fingerprint": "f" * 64,
        "model_id": "m1",
        "sample_key": "s1",
        "score": 0.5,
        "model_version": "v1",
        "model_artifact_fingerprint": MODEL_ARTIFACT,
        "code": "000001",
        "partition": "test",
        "label_start_date": "2026-01-05",
        "prediction_asof": "2026-01-05T15:20:00",
        "source": "shadow_model",
    }

    ALTERNATES = (
        ("dataset_fingerprint", "0" * 64),
        ("model_id", "m2"),
        ("sample_key", "s2"),
        ("score", 0.6),
        ("model_version", "v2"),
        ("model_artifact_fingerprint", OTHER_ARTIFACT),
        ("code", "000002"),
        ("partition", "validation"),
        ("label_start_date", "2026-01-06"),
        ("prediction_asof", "2026-01-05T16:20:00"),
        ("source", "another_source"),
    )

    def test_identity_binds_every_material_field(self):
        base = LE.prediction_identity(**self.MATERIAL)
        for field, alternate in self.ALTERNATES:
            changed = LE.prediction_identity(**dict(self.MATERIAL, **{field: alternate}))
            self.assertNotEqual(base, changed, f"{field} did not move the identity")

    def test_availability_instant_participates_in_the_identity(self):
        """An unproven-asof record must never silently merge with a timed one."""
        timed = LE.normalize_prediction(dict(self.MATERIAL))
        untimed = LE.normalize_prediction(dict(self.MATERIAL, prediction_asof=None))
        self.assertNotEqual(timed["prediction_id"], untimed["prediction_id"])

    def test_identity_is_order_and_duplication_insensitive(self):
        rows = [LE.normalize_prediction(dict(self.MATERIAL, sample_key=f"s{index}"))
                for index in range(6)]
        forward = {row["prediction_id"] for row in rows}
        backward = {row["prediction_id"] for row in reversed(rows)}
        self.assertEqual(forward, backward)
        self.assertEqual(len(forward), 6)

    def test_same_artifact_conflict_is_refused_in_any_insertion_order(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        first = dict(predictions[0])
        divergent = dict(first, score=first["score"] + 2.0)
        forward = evaluate_build(build, predictions + [divergent], model_id=MODEL_ID)
        backward = evaluate_build(build, [divergent] + predictions, model_id=MODEL_ID)
        for build_eval in (forward, backward):
            self.assertIn("evaluation_prediction_conflict", build_eval.blockers)
            self.assertEqual(active_exclusions(build_eval)["prediction_conflict"], 2)
        self.assertEqual(forward.fingerprint, backward.fingerprint)

    def test_conflict_detection_is_scoped_to_one_artifact(self):
        """Two *artifacts* disagreeing is not a contradiction inside one claim."""
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        other = dict(predictions[0], model_artifact_fingerprint=OTHER_ARTIFACT, score=99.0)
        build_eval = evaluate_build(build, predictions + [other], model_id=MODEL_ID)
        self.assertNotIn("evaluation_prediction_conflict", build_eval.blockers)
        # ... but it is still not evidence about the declared artifact.
        self.assertEqual(
            active_exclusions(build_eval)["unattributed_model_artifact"], 1
        )

    def test_scores_from_an_undeclared_artifact_are_never_scored(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = [
            dict(row, model_artifact_fingerprint=OTHER_ARTIFACT)
            for row in predictions_for(build)
        ]
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(
            active_exclusions(build_eval)["unattributed_model_artifact"], len(predictions)
        )
        self.assertEqual(build_eval.evaluated_dates, 0)
        self.assertIn("evaluation_missing_predictions", build_eval.blockers)


# ───────────────────────── dataset fingerprint binding ─────────────────────────


class FingerprintBindingTests(DbTestCase):
    """An evaluation is bound to exactly one dataset fingerprint."""

    def test_prediction_from_another_dataset_is_never_accepted(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, dataset_fingerprint="0" * 64)
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["unbound_prediction"], len(predictions))
        self.assertIn("evaluation_no_held_out_predictions", build_eval.blockers)
        self.assertEqual(build_eval.evaluated_dates, 0)

    def test_missing_dataset_fingerprint_fails_closed(self):
        conn = self.proven()
        build = build_dataset(conn)
        view = LE._DatasetView(fingerprint="", cutoff=CUTOFF, partitions=build.partitions)
        build_eval = evaluate_build(view, predictions_for(build), model_id=MODEL_ID)
        self.assertIn("evaluation_dataset_fingerprint_unavailable", build_eval.blockers)
        self.assertIn("evaluation_no_held_out_predictions", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)

    def test_no_predictions_at_all_cannot_be_ready(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = LE.build_evaluation(build, [])
        self.assertIn("evaluation_model_unresolved", build_eval.blockers)
        self.assertIn("evaluation_insufficient_dates", build_eval.blockers)
        self.assertIn("evaluation_confidence_bound_unavailable", build_eval.blockers)
        self.assertIn("evaluation_missing_predictions", build_eval.blockers)

    def test_dataset_fingerprint_participates_in_the_evaluation_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        baseline = evaluate_build(build, predictions, model_id=MODEL_ID)
        other = LE._DatasetView(fingerprint="a" * 64, cutoff=CUTOFF, partitions=build.partitions)
        rebound = predictions_for(other, model_id=MODEL_ID)
        changed = evaluate_build(other, rebound, model_id=MODEL_ID)
        self.assertNotEqual(baseline.fingerprint, changed.fingerprint)

    def test_ambiguous_model_evidence_fails_closed(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, model_id=MODEL_ID)
        predictions += predictions_for(build, model_id="shadow-b")
        build_eval = evaluate_build(build, predictions)
        self.assertIn("evaluation_model_ambiguous", build_eval.blockers)
        self.assertEqual(build_eval.evaluated_dates, 0)

    def test_explicit_model_selection_resolves_ambiguity(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, model_id=MODEL_ID)
        predictions += predictions_for(build, model_id="shadow-b", score_of=score_negative)
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertNotIn("evaluation_model_ambiguous", build_eval.blockers)
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)


# ─────────────────────────── held-out / leakage ───────────────────────────


class HoldoutLeakageTests(DbTestCase):
    """Only the chronological held-out partition may be scored."""

    def test_train_partition_prediction_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, partition="train")
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertGreater(active_exclusions(build_eval).get("not_held_out", 0), 0)
        self.assertEqual(build_eval.evaluated_dates, 0)
        self.assertIn("evaluation_no_held_out_predictions", build_eval.blockers)

    def test_validation_partition_prediction_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, partition="validation")
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["not_held_out"], len(predictions))

    def test_partition_mismatch_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, partition="test")
        for row in predictions[:2]:
            row["partition"] = "train"
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["partition_mismatch"], 2)

    def test_prediction_created_after_the_cutoff_is_a_lookahead_leak(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        predictions[0] = dict(predictions[0], prediction_asof="2027-01-01T10:00:00")
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["future_prediction"], 1)

    def test_prediction_created_before_the_cutoff_is_accepted(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, prediction_asof="2026-01-02T10:00:00")
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertNotIn("future_prediction", active_exclusions(build_eval))
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)

    def test_unknown_sample_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        predictions[0] = dict(predictions[0], sample_key="deadbeef" * 4)
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["unknown_sample"], 1)

    def test_label_identity_mismatch_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        predictions[0] = dict(predictions[0], label_start_date="2025-01-01")
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["invalid_prediction_identity"], 1)

    def test_code_identity_mismatch_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        wrong = "999999"
        predictions[0] = dict(predictions[0], code=wrong)
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["invalid_prediction_identity"], 1)

    def test_train_rows_are_never_counted_as_evaluated(self):
        conn = self.proven()
        build = build_dataset(conn)
        heldout = len(build.partitions["test"])
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertEqual(build_eval.evaluated_rows, heldout)
        self.assertLess(build_eval.evaluated_rows, build.eligible_rows)


# ──────────────── A: only ``test`` may ever be the evaluation partition ────────────────


class HoldoutPartitionTests(DbTestCase):
    """A closed partition list: train / validation are refused outright."""

    def test_only_test_is_a_supported_holdout_partition(self):
        self.assertEqual(LE.SUPPORTED_HOLDOUT_PARTITIONS, ("test",))
        self.assertTrue(set(LE.SUPPORTED_HOLDOUT_PARTITIONS) < set(LD.PARTITIONS))

    def _evaluate_partition(self, partition):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, partition="test")
        # Same evidence, same perfect IC -- only the partition under test moves.
        build_eval = evaluate_build(
            build, predictions, model_id=MODEL_ID, holdout_partition=partition
        )
        return build_eval

    def test_train_partition_is_refused_even_with_a_flawless_ic(self):
        build_eval = self._evaluate_partition("train")
        self.assertIn("evaluation_holdout_partition_unsupported", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)
        self.assertEqual(build_eval.evaluated_dates, 0)

    def test_validation_partition_is_refused_even_with_a_flawless_ic(self):
        build_eval = self._evaluate_partition("validation")
        self.assertIn("evaluation_holdout_partition_unsupported", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)
        self.assertEqual(build_eval.evaluated_dates, 0)

    def test_unknown_partition_is_refused(self):
        build_eval = self._evaluate_partition("holdout")
        self.assertIn("evaluation_holdout_partition_unsupported", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)

    def test_empty_partition_name_falls_back_to_the_default(self):
        """An omitted/blank name means ``test`` -- it is never read as a refusal."""
        build_eval = self._evaluate_partition("")
        self.assertEqual(build_eval.holdout_partition, "test")
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)

    def test_the_partition_choice_participates_in_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        good = evaluate_build(build, predictions, model_id=MODEL_ID)
        bad = evaluate_build(
            build, predictions, model_id=MODEL_ID, holdout_partition="validation"
        )
        self.assertNotEqual(good.fingerprint, bad.fingerprint)


# ──────────────── B: 100% coverage -- no cherry-picking ────────────────


class CoverageTests(DbTestCase):
    """Every canonical held-out sample must carry exactly one prediction."""

    def test_complete_coverage_is_reported_as_one(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        coverage = build_eval.coverage
        self.assertEqual(coverage["expected_prediction_rows"], len(build.partitions["test"]))
        self.assertEqual(coverage["observed_prediction_rows"], len(build.partitions["test"]))
        self.assertEqual(coverage["missing_prediction_rows"], 0)
        self.assertEqual(coverage["coverage_ratio"], 1.0)
        self.assertTrue(coverage["coverage_complete"])
        self.assertNotIn("evaluation_missing_predictions", build_eval.blockers)

    def test_one_missing_prediction_is_a_refusal(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build)[:-1], model_id=MODEL_ID)
        self.assertIn("evaluation_missing_predictions", build_eval.blockers)
        self.assertEqual(build_eval.coverage["missing_prediction_rows"], 1)
        self.assertLess(build_eval.coverage["coverage_ratio"], 1.0)
        self.assertFalse(build_eval.contract_ok)

    def test_cherry_picking_a_whole_date_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        skipped = test_dates(build)[-1]
        predictions = [
            row for row in predictions_for(build) if row["label_start_date"] != skipped
        ]
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertIn("evaluation_missing_predictions", build_eval.blockers)
        self.assertEqual(build_eval.coverage["missing_prediction_rows"], len(CODES))
        self.assertFalse(build_eval.contract_ok)
        # The IC on the surviving dates is still flawless: only coverage refuses.
        self.assertAlmostEqual(build_eval.mean_rank_ic, 1.0, places=10)

    def test_dropping_the_worst_scoring_name_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        # Remove exactly the prediction with the lowest score, which a
        # performance-hunting provider would love to omit.
        worst = min(predictions, key=lambda row: row["score"])
        build_eval = evaluate_build(
            build, [row for row in predictions if row is not worst], model_id=MODEL_ID
        )
        self.assertIn("evaluation_missing_predictions", build_eval.blockers)
        self.assertEqual(build_eval.coverage["missing_prediction_rows"], 1)
        self.assertLess(build_eval.coverage["coverage_ratio"], 1.0)

    def test_evidence_outside_the_canonical_set_is_refused(self):
        """Defensive invariant: an admitted row must belong to the holdout set."""
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, partition="train") + predictions_for(build)
        with mock.patch.object(LE, "_is_held_out", return_value=True):
            build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertIn("evaluation_unexpected_predictions", build_eval.blockers)
        self.assertNotIn("evaluation_missing_predictions", build_eval.blockers)
        self.assertGreater(build_eval.coverage["unexpected_prediction_rows"], 0)

    def test_coverage_counts_samples_not_rows(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertEqual(
            build_eval.coverage["expected_prediction_rows"], len(build.partitions["test"])
        )

    def test_no_held_out_samples_means_no_coverage_claim(self):
        conn = self.proven()
        build = build_dataset(conn)
        empty = LE._DatasetView(fingerprint=build.fingerprint, cutoff=CUTOFF)
        build_eval = evaluate_build(empty, predictions_for(build), model_id=MODEL_ID)
        self.assertEqual(build_eval.coverage["coverage_ratio"], 0.0)
        self.assertFalse(build_eval.coverage["coverage_complete"])
        self.assertIn("evaluation_missing_predictions", build_eval.blockers)


# ──────────────── C/F: provable train/test separation ────────────────


class ModelProvenanceTests(DbTestCase):
    """A verdict must be able to name its artifact and prove its boundary."""

    def test_missing_provenance_cannot_open_the_gate(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(
            build, predictions_for(build), model_id=MODEL_ID, provenance="none"
        )
        self.assertIn("evaluation_training_boundary_unproven", build_eval.blockers)
        self.assertIn("evaluation_selection_partition_unproven", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)
        # The statistics themselves are still flawless: only the contract refuses.
        self.assertAlmostEqual(build_eval.mean_rank_ic, 1.0, places=10)

    def test_declared_boundary_is_consistent_with_the_dataset(self):
        conn = self.proven()
        build = build_dataset(conn)
        provenance = provenance_for(build)
        self.assertIsNotNone(provenance["trained_through"])
        self.assertLess(provenance["trained_through"], test_start(build))
        self.assertTrue(
            evaluate_build(build, predictions_for(build), model_id=MODEL_ID).contract_ok
        )

    def test_training_through_inside_the_test_partition_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        for overlap in (test_start(build), test_dates(build)[-1], "2027-01-01"):
            build_eval = evaluate_build(
                build,
                predictions_for(build),
                model_id=MODEL_ID,
                provenance=provenance_for(build, trained_through=overlap),
            )
            self.assertIn("evaluation_training_overlaps_test", build_eval.blockers, overlap)
            self.assertFalse(build_eval.contract_ok, overlap)

    def test_unparseable_training_boundary_is_unproven(self):
        conn = self.proven()
        build = build_dataset(conn)
        for boundary in (None, "", "not-a-date", "2026-13-45"):
            build_eval = evaluate_build(
                build,
                predictions_for(build),
                model_id=MODEL_ID,
                provenance=provenance_for(build, trained_through=boundary),
            )
            self.assertIn("evaluation_training_boundary_unproven", build_eval.blockers, boundary)

    def test_unnamed_artifact_is_a_refusal(self):
        conn = self.proven()
        build = build_dataset(conn)
        for field in ("model_version", "model_artifact_fingerprint", "training_dataset_fingerprint"):
            build_eval = evaluate_build(
                build,
                predictions_for(build),
                model_id=MODEL_ID,
                provenance=provenance_for(build, **{field: ""}),
            )
            self.assertIn("evaluation_training_boundary_unproven", build_eval.blockers, field)

    def test_selection_on_the_held_out_partition_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(
            build,
            predictions_for(build),
            model_id=MODEL_ID,
            provenance=provenance_for(build, selection_partition="test"),
        )
        self.assertIn("evaluation_test_used_for_selection", build_eval.blockers)
        self.assertNotIn("evaluation_selection_partition_unproven", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)

    def test_selection_partition_must_be_validation(self):
        conn = self.proven()
        build = build_dataset(conn)
        for selection in ("", "train", "holdout", "none"):
            build_eval = evaluate_build(
                build,
                predictions_for(build),
                model_id=MODEL_ID,
                provenance=provenance_for(build, selection_partition=selection),
            )
            self.assertIn("evaluation_selection_partition_unproven", build_eval.blockers, selection)

    def test_validation_selection_is_the_only_accepted_choice(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(
            build,
            predictions_for(build),
            model_id=MODEL_ID,
            provenance=provenance_for(build, selection_partition="validation"),
        )
        self.assertNotIn("evaluation_selection_partition_unproven", build_eval.blockers)
        self.assertNotIn("evaluation_test_used_for_selection", build_eval.blockers)
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)

    def test_provenance_for_another_model_cannot_vouch(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(
            build,
            predictions_for(build, model_id=MODEL_ID),
            model_id=None,
            provenance=provenance_for(build, model_id="shadow-b"),
        )
        self.assertIn("evaluation_model_ambiguous", build_eval.blockers)

    def test_provenance_evidence_is_append_only_and_idempotent(self):
        conn = self.proven()
        build = build_dataset(conn)
        first = LE.record_model_provenance(conn, provenance_for(build))
        second = LE.record_model_provenance(conn, provenance_for(build))
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)
        rows, truncated = LE.read_model_provenance(conn)
        self.assertFalse(truncated)
        self.assertEqual(len(rows), 1)

    def test_ambiguous_provenance_fails_closed_at_the_gate(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        LE.record_model_provenance(
            conn, provenance_for(build, model_version="shadow-2026.02")
        )
        status = gate(conn)
        self.assertIn("evaluation_model_ambiguous", status["evaluation_blockers"])
        self.assertFalse(status["evaluation_contract_ok"])

    def test_truncated_provenance_read_fails_closed(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        LE.record_model_provenance(conn, provenance_for(build, model_version="shadow-2026.02"))
        status = gate(conn, max_provenance_rows=1)
        self.assertIn("evaluation_model_provenance_truncated", status["evaluation_blockers"])
        self.assertFalse(status["evaluation_contract_ok"])

    def test_gate_exposes_the_resolved_provenance(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        status = gate(conn)
        self.assertTrue(status["evaluation_contract_ok"], status["evaluation_blockers"])
        self.assertEqual(status["model_artifact_fingerprint"], MODEL_ARTIFACT)
        self.assertEqual(status["model_version"], MODEL_VERSION)
        self.assertEqual(status["selection_partition"], "validation")
        self.assertEqual(status["trained_through"], provenance_for(build)["trained_through"])
        self.assertTrue(status["provenance_fingerprint"])

    def test_gate_resolves_the_requested_model(self):
        """The gate reports the model whose evidence it actually judged."""
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        status = gate(conn, model_id=MODEL_ID)
        self.assertEqual(status["model_id"], MODEL_ID)


# ──────────────── F: the fingerprint binds the real artifact ────────────────


class ModelArtifactFingerprintTests(DbTestCase):
    """Every provenance field must move the verdict, not just ``model_id``."""

    def _fingerprint(self, build, **overrides):
        predictions = predictions_for(build)
        if overrides.get("model_artifact_fingerprint") and overrides[
            "model_artifact_fingerprint"
        ] != MODEL_ARTIFACT:
            predictions = [
                dict(row, model_artifact_fingerprint=overrides["model_artifact_fingerprint"])
                for row in predictions
            ]
        return evaluate_build(
            build,
            predictions,
            model_id=MODEL_ID,
            provenance=provenance_for(build, **overrides),
        )

    def test_model_version_moves_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        base = evaluate_build(build, predictions_for(build), model_id=MODEL_ID).fingerprint
        self.assertNotEqual(base, self._fingerprint(build, model_version="shadow-2026.99").fingerprint)

    def test_model_artifact_fingerprint_moves_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        base = evaluate_build(build, predictions_for(build), model_id=MODEL_ID).fingerprint
        changed = self._fingerprint(build, model_artifact_fingerprint=OTHER_ARTIFACT)
        self.assertNotEqual(base, changed.fingerprint)

    def test_training_dataset_fingerprint_moves_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        base = evaluate_build(build, predictions_for(build), model_id=MODEL_ID).fingerprint
        changed = self._fingerprint(build, training_dataset_fingerprint="e" * 64)
        self.assertNotEqual(base, changed.fingerprint)

    def test_trained_through_moves_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        base = evaluate_build(build, predictions_for(build), model_id=MODEL_ID).fingerprint
        earlier = test_dates(build)[0]
        changed = self._fingerprint(build, trained_through=earlier)
        self.assertNotEqual(base, changed.fingerprint)

    def test_selection_partition_moves_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        base = evaluate_build(build, predictions_for(build), model_id=MODEL_ID).fingerprint
        self.assertNotEqual(base, self._fingerprint(build, selection_partition="train").fingerprint)

    def test_hyperparameters_and_seed_move_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        base = evaluate_build(build, predictions_for(build), model_id=MODEL_ID).fingerprint
        self.assertNotEqual(
            base, self._fingerprint(build, hyperparameters_fingerprint="f" * 64).fingerprint
        )
        self.assertNotEqual(base, self._fingerprint(build, random_seed=99).fingerprint)

    def test_manifest_records_the_artifact_provenance(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        manifest = build_eval.manifest
        self.assertEqual(manifest["model_version"], MODEL_VERSION)
        self.assertEqual(manifest["model_artifact_fingerprint"], MODEL_ARTIFACT)
        self.assertEqual(manifest["training_dataset_fingerprint"], TRAINING_DATASET)
        self.assertEqual(manifest["selection_partition"], "validation")
        self.assertEqual(manifest["hyperparameters_fingerprint"], HYPERPARAMETERS)
        self.assertTrue(manifest["provenance_fingerprint"])
        self.assertEqual(build_eval.model_id, MODEL_ID)


# ──────────────── D: prediction availability is fail-closed ────────────────


class PredictionAvailabilityTests(DbTestCase):
    """A score that cannot prove *when* it was made is not evidence."""

    def test_missing_availability_instant_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, prediction_asof=None)
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(
            active_exclusions(build_eval)["unproven_prediction_availability"], len(predictions)
        )
        self.assertEqual(build_eval.evaluated_dates, 0)
        self.assertIn("evaluation_missing_predictions", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)

    def test_unparseable_availability_instant_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        for stamp in ("", "not-a-timestamp", "2026-13-45T99:99:99", "nan"):
            predictions = predictions_for(build, prediction_asof=stamp)
            build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
            self.assertEqual(
                active_exclusions(build_eval)["unproven_prediction_availability"],
                len(predictions),
                stamp,
            )

    def test_availability_at_the_label_instant_is_a_leak(self):
        conn = self.proven()
        build = build_dataset(conn)
        sample = build.partitions["test"][0]
        predictions = predictions_for(build)
        target = next(
            row for row in predictions if row["sample_key"] == sample.sample_key
        )
        target["prediction_asof"] = sample.label_available_at
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["future_prediction"], 1)
        self.assertNotIn("unproven_prediction_availability", active_exclusions(build_eval))

    def test_availability_after_the_label_instant_is_a_leak(self):
        conn = self.proven()
        build = build_dataset(conn)
        sample = build.partitions["test"][0]
        predictions = predictions_for(build)
        target = next(row for row in predictions if row["sample_key"] == sample.sample_key)
        target["prediction_asof"] = "2030-01-01T00:00:00"
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(active_exclusions(build_eval)["future_prediction"], 1)

    def test_availability_is_read_on_the_exchange_clock(self):
        """A naive stamp is exchange-local, never the host's timezone."""
        naive = LE._availability_instant("2026-01-05T15:20:00")
        self.assertEqual(naive.isoformat(), "2026-01-05T07:20:00+00:00")
        explicit = LE._availability_instant("2026-01-05T15:20:00+08:00")
        self.assertEqual(naive, explicit)
        day_only = LE._availability_instant("2026-01-05")
        self.assertEqual(day_only.isoformat(), "2026-01-04T16:00:00+00:00")

    def test_declared_availability_matches_the_dataset_layer(self):
        for value in ("2026-01-05T15:20:00", "2026-01-05T15:20:00+08:00",
                      "2026-01-05T07:20:00Z", "2026-01-05", "nonsense", None, ""):
            mine = LE._availability_instant(value)
            theirs = LD._instant(value)
            self.assertEqual(
                None if mine is None else mine.isoformat(),
                theirs,
                repr(value),
            )

    def test_a_timely_prediction_is_accepted(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertNotIn("unproven_prediction_availability", active_exclusions(build_eval))
        self.assertNotIn("future_prediction", active_exclusions(build_eval))
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)


# ─────────────────────────────── rank IC math ───────────────────────────────


class RankIcTests(DbTestCase):
    """IC is a cross-sectional rank statistic, computed per date."""

    def test_perfect_alignment_is_exactly_one(self):
        self.assertAlmostEqual(LE.spearman_rank_ic([1, 2, 3, 4], [1, 2, 3, 4]), 1.0, places=12)

    def test_inverted_alignment_is_exactly_minus_one(self):
        self.assertAlmostEqual(LE.spearman_rank_ic([1, 2, 3, 4], [4, 3, 2, 1]), -1.0, places=12)

    def test_monotone_non_linear_alignment_is_exactly_one(self):
        # Rank IC must not reward linearity: a monotone transform is a perfect rank bet.
        self.assertAlmostEqual(LE.spearman_rank_ic([1, 2, 3, 4], [1, 4, 9, 100]), 1.0, places=12)

    def test_average_ranks_for_ties(self):
        self.assertEqual(LE._average_ranks([10, 20, 20, 30]), [1.0, 2.5, 2.5, 4.0])
        self.assertEqual(LE._average_ranks([5, 5, 5, 5]), [2.5, 2.5, 2.5, 2.5])

    def test_tied_ranks_do_not_depend_on_input_order(self):
        forward = LE.spearman_rank_ic([1, 2, 2, 3], [1, 2, 3, 4])
        backward = LE.spearman_rank_ic([3, 2, 2, 1], [4, 3, 2, 1])
        self.assertAlmostEqual(forward, backward, places=12)

    def test_degenerate_cross_section_is_undefined_not_zero(self):
        self.assertIsNone(LE.spearman_rank_ic([0, 0, 0], [1, 2, 3]))
        self.assertIsNone(LE.spearman_rank_ic([1, 2, 3], [7, 7, 7]))
        self.assertIsNone(LE.spearman_rank_ic([1], [1]))
        self.assertIsNone(LE.spearman_rank_ic([], []))

    def test_ic_is_measured_per_date(self):
        conn = self.proven()
        build, build_eval = evaluate(conn)
        self.assertEqual(build_eval.evaluated_dates, len(test_dates(build)))
        self.assertEqual(set(build_eval.per_date_ic), set(test_dates(build)))
        for value in build_eval.per_date_ic.values():
            self.assertAlmostEqual(value, 1.0, places=10)

    def test_inverted_scores_produce_negative_ic(self):
        conn = self.proven()
        _build, build_eval = evaluate(conn, score_negative)
        self.assertLess(build_eval.mean_rank_ic, 0.0)
        self.assertLess(build_eval.ic_lower_bound, 0.0)
        self.assertIn("evaluation_mean_rank_ic_below_floor", build_eval.blockers)
        self.assertIn("evaluation_confidence_bound_not_positive", build_eval.blockers)

    def test_constant_scores_drop_every_date_as_undefined(self):
        conn = self.proven()
        _build, build_eval = evaluate(conn, score_constant)
        self.assertEqual(build_eval.evaluated_dates, 0)
        self.assertTrue(build_eval.dropped_dates > 0)
        self.assertIn("evaluation_insufficient_dates", build_eval.blockers)
        self.assertIsNone(build_eval.mean_rank_ic)

    def test_cross_section_below_the_floor_is_dropped(self):
        conn = self.new_db()
        seed_panel(conn, day_count=40, codes=("000001", "000002"))
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertEqual(build_eval.evaluated_dates, 0)
        self.assertEqual(build_eval.dropped_dates, len(test_dates(build)))


# ──────────────────────── same-date dependence (the point) ────────────────────────


class SameDateDependenceTests(DbTestCase):
    """Rows on one date are one observation, not many."""

    def test_many_rows_on_few_dates_do_not_create_evidence(self):
        conn = self.new_db()
        wide_codes = tuple(f"{index:06d}" for index in range(40))
        seed_panel(conn, day_count=8, codes=wide_codes)
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertGreater(build_eval.evaluated_rows, 0)
        self.assertLess(build_eval.evaluated_dates, LE.MIN_EVAL_DATES)
        self.assertEqual(build_eval.evaluated_dates, len(test_dates(build)))
        self.assertIn("evaluation_insufficient_dates", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)

    def test_confidence_unit_is_the_date_not_the_row(self):
        conn = self.proven()
        _build, build_eval = evaluate(conn)
        self.assertEqual(build_eval.evaluated_dates, len(build_eval.per_date_ic))
        self.assertEqual(build_eval.ic_std, 0.0)
        self.assertEqual(build_eval.ic_std_error, 0.0)
        self.assertAlmostEqual(build_eval.ic_lower_bound, build_eval.mean_rank_ic, places=12)

    def test_adding_codes_to_a_date_does_not_shrink_the_standard_error(self):
        conn = self.proven()
        narrow = build_dataset(conn)
        narrow_eval = evaluate_build(narrow, predictions_for(narrow), model_id=MODEL_ID)
        wide_codes = tuple(f"{index:06d}" for index in range(20))
        wide_conn = self.proven(codes=wide_codes)
        wide = build_dataset(wide_conn)
        wide_eval = evaluate_build(wide, predictions_for(wide), model_id=MODEL_ID)
        # Same per-date IC sequence, many more rows: the interval is unchanged.
        self.assertGreater(wide_eval.evaluated_rows, narrow_eval.evaluated_rows)
        self.assertAlmostEqual(wide_eval.ic_std_error, narrow_eval.ic_std_error, places=12)

    def test_dispersion_comes_from_dates_not_from_rows(self):
        conn = self.new_db()
        seed_panel(conn, day_count=40, target_fn=alternating_target)
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        signs = {round(value, 12) for value in build_eval.per_date_ic.values()}
        self.assertEqual(signs, {1.0, -1.0})
        self.assertGreater(build_eval.ic_std, 0.0)
        self.assertLess(build_eval.ic_lower_bound, 0.0)
        self.assertIn("evaluation_confidence_bound_not_positive", build_eval.blockers)

    def test_positive_point_estimate_without_a_positive_bound_is_not_evidence(self):
        """A positive mean with real date dispersion must still not pass."""
        conn = self.new_db()
        seed_panel(conn, day_count=40, target_fn=alternating_target)
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertGreater(build_eval.mean_rank_ic, 0.0)
        self.assertGreater(build_eval.ic_std, 0.0)
        self.assertLess(build_eval.ic_lower_bound, 0.0)
        self.assertFalse(build_eval.contract_ok)
        self.assertIn("evaluation_confidence_bound_not_positive", build_eval.blockers)


# ──────────────── G: chronological tail robustness ────────────────


class TailRobustnessTests(DbTestCase):
    """The most recent slice of the held-out window must still carry the edge."""

    def _invert_recent_dates(self, conn, build, count, permutation=(3, 2, 1, 0)):
        """Re-label the most recent ``count`` test dates to a chosen IC.

        The default permutation inverts the cross-section (rank IC ``-1``); pass
        ``(0, 3, 2, 1)`` for a positive-but-degraded rank IC of exactly ``0.2``.
        """
        recent = test_dates(build)[-count:]
        for day in recent:
            for index, code in enumerate(CODES):
                conn.execute(
                    "UPDATE adaptive_alpha_returns SET forward_return_pct=? "
                    "WHERE start_date=? AND code=? AND horizon=1",
                    (code_value(CODES[permutation[index]]), day, code),
                )
        return recent

    def test_tail_metrics_are_reported(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        holdout = build_eval.holdout
        self.assertEqual(holdout["holdout_date_count"], LE.MIN_HOLDOUT_DATES)
        self.assertAlmostEqual(holdout["holdout_mean_rank_ic"], 1.0, places=12)
        self.assertAlmostEqual(holdout["holdout_positive_ratio"], 1.0, places=12)
        self.assertIn("holdout_date_count", build_eval.metrics)
        self.assertIn("holdout_mean_rank_ic", build_eval.manifest["holdout"])

    def test_tail_uses_the_most_recent_dates(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        expected_start = test_dates(build)[-LE.MIN_HOLDOUT_DATES]
        self.assertEqual(build_eval.holdout["holdout_start_date"], expected_start)

    def test_a_shrunk_tail_is_refused(self):
        """Early strength must not launder a decayed recent window."""
        conn = self.proven()
        build = build_dataset(conn)
        recent = self._invert_recent_dates(conn, build, LE.MIN_HOLDOUT_DATES)
        rebuild = build_dataset(conn)
        build_eval = evaluate_build(rebuild, predictions_for(rebuild), model_id=MODEL_ID)
        self.assertEqual(recent, test_dates(rebuild)[-LE.MIN_HOLDOUT_DATES:])
        self.assertGreater(build_eval.mean_rank_ic, 0.0)
        self.assertLess(build_eval.holdout["holdout_mean_rank_ic"], 0.0)
        self.assertIn("evaluation_holdout_mean_not_positive", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)

    def test_deterioration_is_refused_even_when_the_tail_is_positive(self):
        """A tail that kept only a fraction of the edge is still a refusal."""
        conn = self.proven()
        build = build_dataset(conn)
        # Rank IC of exactly 0.2 on the recent dates: positive, but far below the
        # 1.0 the rest of the window delivers.
        self._invert_recent_dates(
            conn, build, LE.MIN_HOLDOUT_DATES, permutation=(0, 3, 2, 1)
        )
        rebuild = build_dataset(conn)
        build_eval = evaluate_build(rebuild, predictions_for(rebuild), model_id=MODEL_ID)
        self.assertAlmostEqual(build_eval.holdout["holdout_mean_rank_ic"], 0.2, places=10)
        self.assertGreater(build_eval.holdout["holdout_mean_rank_ic"], 0.0)
        self.assertLess(
            build_eval.holdout["holdout_retention"], LE.HOLDOUT_RETENTION_FLOOR
        )
        self.assertIn("evaluation_holdout_deterioration", build_eval.blockers)
        self.assertNotIn("evaluation_holdout_mean_not_positive", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)

    def test_a_strong_tail_passes(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertNotIn("evaluation_holdout_deterioration", build_eval.blockers)
        self.assertNotIn("evaluation_holdout_mean_not_positive", build_eval.blockers)
        self.assertNotIn("evaluation_holdout_insufficient", build_eval.blockers)
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)

    def test_too_few_tail_dates_is_a_refusal(self):
        conn = self.new_db()
        seed_panel(conn, day_count=8)
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertLess(build_eval.holdout["holdout_date_count"], LE.MIN_HOLDOUT_DATES)
        self.assertIn("evaluation_holdout_insufficient", build_eval.blockers)

    def test_tail_is_not_a_row_count_gate(self):
        """Many names per date must not manufacture tail evidence."""
        conn = self.new_db()
        wide = tuple(f"{index:06d}" for index in range(30))
        seed_panel(conn, day_count=8, codes=wide)
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertGreater(build_eval.evaluated_rows, 50)
        self.assertEqual(build_eval.holdout["holdout_date_count"], len(test_dates(build)))
        self.assertLess(build_eval.holdout["holdout_date_count"], LE.MIN_HOLDOUT_DATES)
        self.assertIn("evaluation_holdout_insufficient", build_eval.blockers)


# ──────────────────────────── confidence / holdout gate ────────────────────────────


class ConfidenceGateTests(DbTestCase):
    """The gate is a bound, not a point estimate."""

    def test_positive_evidence_passes_the_gate(self):
        conn = self.proven()
        _build, build_eval = evaluate(conn)
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)
        self.assertAlmostEqual(build_eval.mean_rank_ic, 1.0, places=12)
        self.assertAlmostEqual(build_eval.ic_lower_bound, 1.0, places=12)
        self.assertGreaterEqual(build_eval.evaluated_dates, LE.MIN_EVAL_DATES)
        self.assertEqual(build_eval.manifest["evaluation_contract_ok"], True)

    def test_min_dates_floor_is_enforced(self):
        conn = self.proven()
        _build, build_eval = evaluate(conn, min_dates=1000)
        self.assertIn("evaluation_insufficient_dates", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)

    def test_mean_floor_is_enforced(self):
        conn = self.proven()
        _build, build_eval = evaluate(conn, min_mean_rank_ic=1.5)
        self.assertIn("evaluation_mean_rank_ic_below_floor", build_eval.blockers)

    def test_lower_bound_floor_is_enforced(self):
        conn = self.proven()
        _build, build_eval = evaluate(conn, min_ic_lower_bound=1.5)
        self.assertIn("evaluation_confidence_bound_not_positive", build_eval.blockers)
        self.assertNotIn("evaluation_mean_rank_ic_below_floor", build_eval.blockers)

    def test_single_date_has_no_interval_at_all(self):
        conn = self.proven()
        build = build_dataset(conn)
        single = [s for s in build.partitions["test"][: len(CODES)]]
        single_date = single[0].label_start_date
        predictions = [
            {
                "dataset_fingerprint": build.fingerprint,
                "model_id": MODEL_ID,
                "model_version": MODEL_VERSION,
                "model_artifact_fingerprint": MODEL_ARTIFACT,
                "sample_key": sample.sample_key,
                "code": sample.code,
                "partition": "test",
                "label_start_date": single_date,
                "score": score_by_code(sample),
                "prediction_asof": f"{single_date}T15:20:00",
            }
            for sample in single
        ]
        build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertEqual(build_eval.evaluated_dates, 1)
        self.assertIsNone(build_eval.ic_std)
        self.assertIsNone(build_eval.ic_lower_bound)
        self.assertIn("evaluation_confidence_bound_unavailable", build_eval.blockers)

    def test_lower_bound_is_strictly_below_the_mean_when_dates_disperse(self):
        conn = self.new_db()
        seed_panel(conn, day_count=40, target_fn=alternating_target)
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertLess(build_eval.ic_lower_bound, build_eval.mean_rank_ic)
        self.assertEqual(build_eval.scoring["unit_of_observation"], "date")
        self.assertTrue(build_eval.scoring["same_date_rows_are_not_independent"])

    def test_truncated_prediction_read_is_never_ready(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        total = conn.execute("SELECT COUNT(*) FROM learning_prediction_evidence").fetchone()[0]
        status = gate(conn, max_prediction_rows=total - 1)
        self.assertTrue(status["truncated"])
        self.assertIn("evaluation_predictions_read_truncated", status["evaluation_blockers"])

    def test_exact_prediction_count_is_not_truncated(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        total = conn.execute("SELECT COUNT(*) FROM learning_prediction_evidence").fetchone()[0]
        status = gate(conn, max_prediction_rows=total)
        self.assertFalse(status["truncated"])
        self.assertNotIn("evaluation_predictions_read_truncated", status["evaluation_blockers"])
        self.assertTrue(status["evaluation_contract_ok"], status["evaluation_blockers"])

    def test_dataset_contract_failure_blocks_the_evaluation(self):
        conn = self.new_db()
        seed_panel(conn, day_count=4)
        status = gate(conn)
        self.assertIn("evaluation_dataset_contract_failed", status["evaluation_blockers"])
        self.assertFalse(status["evaluation_contract_ok"])

    def test_missing_prediction_table_fails_closed(self):
        conn = self.proven(prediction_table=False, config=False)
        status = gate(conn)
        self.assertEqual(status["evaluation_blockers"], ["evaluation_prediction_table_missing"])
        self.assertIsNone(status["evaluation_fingerprint"])
        self.assertEqual(status["coverage"]["coverage_ratio"], 0.0)

    def test_contract_layer_error_fails_closed(self):
        conn = self.proven()
        with mock.patch.object(LE, "_evaluate", side_effect=RuntimeError("boom")):
            status = gate(conn)
        self.assertEqual(status["evaluation_blockers"], ["evaluation_contract_error"])
        self.assertFalse(status["evaluation_contract_ok"])

    def test_read_only_gate_reports_a_bound_and_stays_shadow_only(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        status = gate(conn)
        self.assertTrue(status["evaluation_contract_ok"], status["evaluation_blockers"])
        self.assertTrue(status["evaluation_fingerprint"])
        metrics = status["evaluation_metrics"]
        self.assertEqual(metrics["unit_of_observation"], "date")
        self.assertGreater(metrics["ic_lower_bound"], 0.0)
        self.assertEqual(status["execution_authority"], "none")
        self.assertEqual(status["evaluation_scope"], "research_read_only")


# ──────────────────────────────── manifests ────────────────────────────────


class ManifestTests(DbTestCase):
    """Evaluation manifests are content-addressed and append-only."""

    def _build(self, conn):
        build = build_dataset(conn)
        return evaluate_build(build, predictions_for(build), model_id=MODEL_ID)

    def test_manifest_is_append_only_and_idempotent(self):
        conn = self.proven()
        build_eval = self._build(conn)
        self.assertTrue(LE.persist_evaluation_manifest(conn, build_eval.manifest))
        self.assertFalse(LE.persist_evaluation_manifest(conn, build_eval.manifest))
        count = conn.execute("SELECT COUNT(*) FROM learning_evaluation_manifests").fetchone()[0]
        self.assertEqual(count, 1)

    def test_rerun_cannot_rewrite_the_first_record(self):
        conn = self.proven()
        build_eval = self._build(conn)
        LE.persist_evaluation_manifest(conn, build_eval.manifest)
        stored = LE.read_evaluation_manifest(conn, build_eval.fingerprint)
        replay = dict(build_eval.manifest, created_at="2099-01-01T00:00:00+00:00")
        LE.persist_evaluation_manifest(conn, replay)
        again = LE.read_evaluation_manifest(conn, build_eval.fingerprint)
        self.assertEqual(stored["created_at"], again["created_at"])
        self.assertNotEqual(again["created_at"], "2099-01-01T00:00:00+00:00")

    def test_manifest_round_trips_structured_columns(self):
        conn = self.proven()
        build_eval = self._build(conn)
        LE.persist_evaluation_manifest(conn, build_eval.manifest)
        stored = LE.read_evaluation_manifest(conn, build_eval.fingerprint)
        self.assertIsInstance(stored["per_date_ic"], dict)
        self.assertIsInstance(stored["scoring"], dict)
        self.assertIsInstance(stored["evaluation_blockers"], list)
        self.assertEqual(stored["execution_authority"], "none")
        self.assertEqual(stored["evaluation_scope"], "research_read_only")
        self.assertEqual(stored["evaluation_contract_ok"], 1)
        self.assertEqual(stored["prediction_digest"], build_eval.prediction_digest)

    def test_manifest_records_the_provenance_and_robustness_columns(self):
        conn = self.proven()
        build_eval = self._build(conn)
        LE.persist_evaluation_manifest(conn, build_eval.manifest)
        stored = LE.read_evaluation_manifest(conn, build_eval.fingerprint)
        self.assertEqual(stored["model_version"], MODEL_VERSION)
        self.assertEqual(stored["model_artifact_fingerprint"], MODEL_ARTIFACT)
        self.assertEqual(stored["training_dataset_fingerprint"], TRAINING_DATASET)
        self.assertEqual(stored["selection_partition"], "validation")
        self.assertTrue(stored["provenance_fingerprint"])
        self.assertEqual(
            stored["expected_prediction_rows"],
            build_eval.coverage["expected_prediction_rows"],
        )
        self.assertEqual(stored["missing_prediction_rows"], 0)
        self.assertEqual(stored["holdout_date_count"], build_eval.holdout["holdout_date_count"])

    def test_read_only_gate_never_writes_a_manifest(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        gate(conn)
        count = conn.execute("SELECT COUNT(*) FROM learning_evaluation_manifests").fetchone()[0]
        self.assertEqual(count, 0)

    def test_failed_evaluation_is_recorded_with_its_blockers(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(
            build, predictions_for(build, score_negative), model_id=MODEL_ID
        )
        LE.persist_evaluation_manifest(conn, build_eval.manifest)
        stored = LE.read_evaluation_manifest(conn, build_eval.fingerprint)
        self.assertEqual(stored["evaluation_contract_ok"], 0)
        self.assertIn("evaluation_confidence_bound_not_positive", stored["evaluation_blockers"])

    def test_created_at_is_not_part_of_the_fingerprint(self):
        conn = self.proven()
        build_eval = self._build(conn)
        recomputed = LE.evaluation_fingerprint(
            dataset_fingerprint=build_eval.dataset_fingerprint,
            model_id=build_eval.model_id,
            holdout_partition=build_eval.holdout_partition,
            scoring=build_eval.scoring,
            per_date_ic=build_eval.per_date_ic,
            evaluated_dates=build_eval.evaluated_dates,
            evaluated_rows=build_eval.evaluated_rows,
            mean_rank_ic=build_eval.mean_rank_ic,
            ic_std=build_eval.ic_std,
            ic_std_error=build_eval.ic_std_error,
            ic_lower_bound=build_eval.ic_lower_bound,
            evaluation_blockers=build_eval.blockers,
            prediction_digest=build_eval.prediction_digest,
            model_provenance=build_eval.model_provenance,
            coverage=build_eval.coverage,
            holdout=build_eval.holdout,
        )
        self.assertEqual(recomputed, build_eval.fingerprint)
        self.assertIn("created_at", build_eval.manifest)

    def test_ensure_schema_is_idempotent_and_additive(self):
        conn = self.new_db()
        report = LE.ensure_schema(conn)
        self.assertTrue(report["prediction_table"])
        self.assertTrue(report["model_provenance_table"])
        self.assertTrue(report["evaluation_manifest_table"])
        LE.ensure_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(learning_evaluation_manifests)")}
        for name in ("evaluation_fingerprint", "ic_lower_bound", "provenance_fingerprint",
                     "coverage_ratio", "holdout_date_count"):
            self.assertIn(name, columns)
        prediction_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(learning_prediction_evidence)")
        }
        self.assertIn("model_artifact_fingerprint", prediction_columns)

    def test_ensure_schema_widens_an_older_table(self):
        """A table written by the v1 contract must be widened, not rejected."""
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE learning_prediction_evidence("
            "prediction_id TEXT PRIMARY KEY, evaluation_schema_version TEXT,"
            "contract_version TEXT, source TEXT, dataset_fingerprint TEXT, model_id TEXT,"
            "sample_key TEXT, code TEXT, partition TEXT, label_start_date TEXT, score TEXT,"
            "prediction_asof TEXT, created_at TEXT)"
        )
        LE.ensure_schema(conn)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(learning_prediction_evidence)")
        }
        self.assertIn("model_version", columns)
        self.assertIn("model_artifact_fingerprint", columns)
        self.assertIn("prediction_id", columns)


# ──────────────────────────────── determinism ────────────────────────────────


class DeterminismTests(DbTestCase):
    """The same evidence must always produce the same verdict."""

    def test_same_inputs_reproduce_the_same_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        first = evaluate_build(build, predictions, model_id=MODEL_ID)
        second = evaluate_build(build, list(reversed(predictions)), model_id=MODEL_ID)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 64)

    def test_rebuilding_the_dataset_reproduces_the_same_verdict(self):
        conn = self.proven()
        first_build = build_dataset(conn)
        first = evaluate_build(first_build, predictions_for(first_build), model_id=MODEL_ID)
        second_build = build_dataset(conn)
        second = evaluate_build(second_build, predictions_for(second_build), model_id=MODEL_ID)
        self.assertEqual(first_build.fingerprint, second_build.fingerprint)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_prediction_insertion_order_does_not_fork_the_verdict(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        LE.record_predictions(conn, list(reversed(predictions)))
        LE.record_model_provenance(conn, provenance_for(build))
        first = gate(conn)
        conn2 = self.proven()
        build2 = build_dataset(conn2)
        LE.record_predictions(conn2, predictions_for(build2))
        LE.record_model_provenance(conn2, provenance_for(build2))
        second = gate(conn2)
        self.assertEqual(first["evaluation_fingerprint"], second["evaluation_fingerprint"])
        self.assertEqual(first["evaluation_blockers"], second["evaluation_blockers"])

    def test_fingerprint_binds_the_judged_prediction_evidence(self):
        conn = self.proven()
        build = build_dataset(conn)
        baseline = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        # A monotone rescaling leaves every rank IC identical, so only the
        # evidence digest can -- and must -- tell the two verdicts apart.
        noisy = predictions_for(
            build, score_of=lambda sample: code_value(sample.code) * 2.0
        )
        doubled = evaluate_build(build, noisy, model_id=MODEL_ID)
        self.assertEqual(baseline.evaluated_rows, doubled.evaluated_rows)
        self.assertEqual(baseline.per_date_ic, doubled.per_date_ic)
        self.assertNotEqual(baseline.prediction_digest, doubled.prediction_digest)
        self.assertNotEqual(baseline.fingerprint, doubled.fingerprint)


class SensitivityTests(DbTestCase):
    """Every material input must move the evaluation fingerprint."""

    def _fingerprint(self, build, predictions, **kwargs):
        return evaluate_build(build, predictions, model_id=MODEL_ID, **kwargs).fingerprint

    def test_score_change_moves_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        baseline = self._fingerprint(build, predictions_for(build))
        changed = self._fingerprint(
            build, predictions_for(build, score_of=lambda sample: code_value(sample.code) + 0.5)
        )
        self.assertNotEqual(baseline, changed)

    def test_model_identity_moves_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        baseline = evaluate_build(
            build, predictions_for(build, model_id="shadow-a"), model_id="shadow-a"
        ).fingerprint
        changed = evaluate_build(
            build, predictions_for(build, model_id="shadow-b"), model_id="shadow-b"
        ).fingerprint
        self.assertNotEqual(baseline, changed)

    def test_holdout_partition_moves_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        baseline = self._fingerprint(build, predictions_for(build))
        changed = self._fingerprint(
            build, predictions_for(build), holdout_partition="validation"
        )
        self.assertNotEqual(baseline, changed)

    def test_scoring_floors_move_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        baseline = self._fingerprint(build, predictions)
        changed = self._fingerprint(build, predictions, min_dates=99)
        self.assertNotEqual(baseline, changed)
        other = self._fingerprint(build, predictions, confidence_z=2.576)
        self.assertNotEqual(baseline, other)

    def test_tail_parameters_move_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        baseline = self._fingerprint(build, predictions)
        changed = self._fingerprint(build, predictions, holdout_fraction=0.5)
        self.assertNotEqual(baseline, changed)
        other = self._fingerprint(build, predictions, min_holdout_dates=5)
        self.assertNotEqual(baseline, other)

    def test_blockers_and_truncation_move_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        baseline = self._fingerprint(build, predictions)
        truncated = self._fingerprint(build, predictions, truncated=True)
        self.assertNotEqual(baseline, truncated)

    def test_dataset_binding_moves_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        baseline = self._fingerprint(build, predictions_for(build))
        rebound = LE._DatasetView(fingerprint="b" * 64, cutoff=CUTOFF, partitions=build.partitions)
        changed = self._fingerprint(rebound, predictions_for(rebound))
        self.assertNotEqual(baseline, changed)

    def test_coverage_moves_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        baseline = self._fingerprint(build, predictions)
        partial = self._fingerprint(build, predictions[:-1])
        self.assertNotEqual(baseline, partial)


# ─────────────────────────── neural shadow integration ───────────────────────────


class NeuralShadowGateTests(DbTestCase):
    """The bounded shadow needs dataset AND evaluation AND human approval."""

    def _open_numeric_gates(self):
        return mock.patch.multiple(
            NS, MIN_PROFILE_DAYS=1, MIN_LABEL_DATES=1, MIN_LABEL_ROWS=1, REQUIRED_HORIZONS=(1,)
        )

    def _approve(self, conn):
        conn.execute(
            "INSERT OR REPLACE INTO adaptive_config(key,value) VALUES('neural_network_approved','1')"
        )

    def test_dataset_ready_but_no_verdict_still_blocks(self):
        conn = self.proven()
        with self._open_numeric_gates():
            ready = NS.readiness(conn)
        self.assertTrue(ready["dataset_blockers"] == [], ready["dataset_blockers"])
        self.assertFalse(ready["evaluation_contract_ok"])
        self.assertTrue(ready["evaluation_blockers"])
        self.assertFalse(ready["admitted"])
        self.assertTrue(any("评估契约未通过" in item for item in ready["blockers"]))

    def test_approval_without_a_verdict_waits_for_evaluation(self):
        conn = self.proven()
        self._approve(conn)
        with self._open_numeric_gates():
            status = NS.control_status(conn)
        self.assertEqual(status["status"], "approval_waiting_evaluation")
        self.assertTrue(status["dataset_contract_ok"])
        self.assertFalse(status["evaluation_contract_ok"])
        self.assertFalse(status["combined_gate_ok"])
        self.assertEqual(status["max_rank_adjustment"], 0.0)
        self.assertEqual(status["mode"], "shadow_only")
        self.assertEqual(status["trading_impact"], "none")

    def test_approval_waits_for_data_when_the_dataset_is_not_ready(self):
        conn = self.new_db()
        seed_panel(conn, day_count=4)
        self._approve(conn)
        with self._open_numeric_gates():
            status = NS.control_status(conn)
        self.assertEqual(status["status"], "approval_waiting_data")
        self.assertFalse(status["dataset_contract_ok"])

    def test_all_three_gates_open_only_with_evidence(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        self._approve(conn)
        with self._open_numeric_gates():
            status = NS.control_status(conn)
        self.assertEqual(status["status"], "approved_bounded_shadow")
        self.assertTrue(status["dataset_contract_ok"])
        self.assertTrue(status["evaluation_contract_ok"])
        self.assertTrue(status["human_approved"])
        self.assertTrue(status["combined_gate_ok"])
        self.assertEqual(status["max_rank_adjustment"], 0.05)
        # Even a fully admitted shadow keeps zero execution authority.
        self.assertEqual(status["mode"], "shadow_only")
        self.assertEqual(status["trading_impact"], "none")
        self.assertTrue(status["hard_gates_unchanged"])
        self.assertEqual(status["readiness"]["execution_authority"], "none")

    def test_unproven_training_boundary_waits_for_evaluation(self):
        conn = self.proven()
        build = build_dataset(conn)
        # Evidence exists and the IC is flawless -- but nothing names the
        # artifact or proves when training stopped.
        record(conn, build, provenance="skip")
        self._approve(conn)
        with self._open_numeric_gates():
            status = NS.control_status(conn)
        self.assertEqual(status["status"], "approval_waiting_evaluation")
        self.assertIn(
            "evaluation_training_boundary_unproven",
            status["readiness"]["evaluation_blockers"],
        )
        self.assertEqual(status["max_rank_adjustment"], 0.0)
        self.assertEqual(status["mode"], "shadow_only")

    def test_cherry_picked_predictions_wait_for_evaluation(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = record(conn, build)
        conn.execute(
            "DELETE FROM learning_prediction_evidence WHERE prediction_id=?",
            (LE.normalize_prediction(predictions[-1])["prediction_id"],),
        )
        self._approve(conn)
        with self._open_numeric_gates():
            status = NS.control_status(conn)
        self.assertEqual(status["status"], "approval_waiting_evaluation")
        self.assertIn(
            "evaluation_missing_predictions", status["readiness"]["evaluation_blockers"]
        )
        self.assertLess(status["readiness"]["evaluation_coverage"]["coverage_ratio"], 1.0)
        self.assertEqual(status["max_rank_adjustment"], 0.0)

    def test_train_partition_only_evidence_waits_for_evaluation(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build, partition="train")
        self._approve(conn)
        with self._open_numeric_gates():
            status = NS.control_status(conn)
        self.assertEqual(status["status"], "approval_waiting_evaluation")
        blockers = status["readiness"]["evaluation_blockers"]
        self.assertIn("evaluation_no_held_out_predictions", blockers)
        self.assertIn("evaluation_missing_predictions", blockers)
        self.assertEqual(status["mode"], "shadow_only")
        self.assertEqual(status["trading_impact"], "none")

    def test_ambiguous_provenance_waits_for_evaluation(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        LE.record_model_provenance(conn, provenance_for(build, model_version="shadow-2026.02"))
        self._approve(conn)
        with self._open_numeric_gates():
            status = NS.control_status(conn)
        self.assertEqual(status["status"], "approval_waiting_evaluation")
        self.assertIn(
            "evaluation_model_ambiguous", status["readiness"]["evaluation_blockers"]
        )

    def test_disabled_beats_every_other_state(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        self._approve(conn)
        conn.execute(
            "INSERT OR REPLACE INTO adaptive_config(key,value) VALUES('neural_shadow_enabled','0')"
        )
        with self._open_numeric_gates():
            status = NS.control_status(conn)
        self.assertEqual(status["status"], "disabled")
        self.assertEqual(status["max_rank_adjustment"], 0.0)

    def test_evaluation_blockers_are_surfaced_to_the_caller(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build, score_of=score_negative)
        with self._open_numeric_gates():
            ready = NS.readiness(conn)
        self.assertIn("evaluation_mean_rank_ic_below_floor", ready["evaluation_blockers"])
        metrics = ready["evaluation_metrics"]
        self.assertLess(metrics["mean_rank_ic"], 0.0)
        self.assertEqual(ready["evaluation_holdout_partition"], "test")
        self.assertEqual(ready["evaluation_metric"], LE.METRIC_SPEARMAN_RANK_IC)

    def test_readiness_exposes_the_provenance_and_robustness_fields(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        with self._open_numeric_gates():
            ready = NS.readiness(conn)
        self.assertEqual(ready["evaluation_model_version"], MODEL_VERSION)
        self.assertEqual(
            ready["evaluation_model_artifact_fingerprint"], MODEL_ARTIFACT
        )
        self.assertEqual(ready["evaluation_selection_partition"], "validation")
        self.assertEqual(
            ready["evaluation_trained_through"], provenance_for(build)["trained_through"]
        )
        self.assertEqual(ready["evaluation_coverage"]["coverage_ratio"], 1.0)
        self.assertIn("holdout_date_count", ready["evaluation_holdout"])

    def test_gate_error_is_fail_closed(self):
        conn = self.proven()
        self._approve(conn)
        with mock.patch.object(LE, "contract_status", side_effect=RuntimeError("boom")):
            with self._open_numeric_gates():
                status = NS.control_status(conn)
        self.assertEqual(status["status"], "approval_waiting_evaluation")
        self.assertIn("evaluation_contract_error", status["readiness"]["evaluation_blockers"])

    def test_neural_gate_never_takes_execution_authority(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        self._approve(conn)
        with self._open_numeric_gates():
            status = NS.control_status(conn)
        self.assertEqual(status["mode"], "shadow_only")
        self.assertEqual(status["trading_impact"], "none")
        self.assertEqual(status["readiness"]["execution_authority"], "none")
        self.assertTrue(status["hard_gates_unchanged"])


# ───────────────────────── cross-module normalization ─────────────────────────


class NormalizationAgreementTests(DbTestCase):
    """Local normalization must not drift from the dataset layer's rules."""

    VALUES = (0, 0.0, -0.0, 1, 1.5, "2.25", -3, 1e-13, None, "nan", "", "abc", True, float("inf"))

    def test_numeric_normalization_matches_the_dataset_layer(self):
        for value in self.VALUES:
            self.assertEqual(LE._canon_number(value), LD._canon_number(value), repr(value))

    def test_finite_parsing_matches_the_dataset_layer(self):
        for value in self.VALUES:
            self.assertEqual(LE._finite(value), LD._finite(value), repr(value))

    def test_date_normalization_matches_the_dataset_layer(self):
        for value in ("2026-01-01", "2026-01-01T10:00:00", "2026/01/01", "not-a-date", None, ""):
            self.assertEqual(LE._iso_date(value), LD._date_text(value), repr(value))

    def test_availability_normalization_is_delegated(self):
        for value in ("2026-01-05T10:00:00", "2026-01-05T10:00:00+08:00", "2026-01-05", "nonsense"):
            self.assertEqual(LE.normalize_prediction({"prediction_asof": value})["prediction_asof"],
                             LD.normalize_availability(value), repr(value))


# ──────────────────────────────── safety ────────────────────────────────


class SafetyTests(DbTestCase):
    """The evaluator is a read-only measurement device."""

    def test_evaluation_never_touches_the_network(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        with mock.patch("socket.socket", side_effect=AssertionError("network during evaluation")):
            with mock.patch("socket.create_connection", side_effect=AssertionError("network")):
                with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                    build_eval = evaluate_build(build, predictions, model_id=MODEL_ID)
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)

    def test_evaluator_has_no_forbidden_dependency(self):
        self.assertEqual(LE.forbidden_dependencies(), [])

    def test_source_imports_no_ml_or_network_module(self):
        tree = ast.parse(Path(LE.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        forbidden = (
            "numpy", "pandas", "scipy", "sklearn", "torch", "tensorflow", "statsmodels",
            "xgboost", "lightgbm", "catboost",
            "requests", "urllib", "socket", "httpx", "aiohttp", "http",
            "adaptive_engine", "paper_trading", "order_intent", "strategy_registry",
        )
        for name in forbidden:
            self.assertNotIn(name, imported)

    def test_source_performs_no_training_or_order_submission(self):
        tree = ast.parse(Path(LE.__file__).read_text(encoding="utf-8"))
        forbidden_attributes = {"fit", "fit_predict", "train", "partial_fit", "backward", "step"}
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(forbidden_attributes.isdisjoint(called), sorted(forbidden_attributes & called))
        forbidden_names = {
            "submit_order", "submit_manual_order", "cancel_manual_order",
            "execute_open", "_execute_manual_plan", "_commit_strategy_buy",
        }
        named = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertTrue(forbidden_names.isdisjoint(named), sorted(forbidden_names & named))

    def test_read_only_gate_does_not_change_the_database(self):
        conn = self.proven()
        build = build_dataset(conn)
        record(conn, build)
        before = conn.total_changes
        gate(conn)
        self.assertEqual(conn.total_changes, before)

    def test_evaluation_does_not_mutate_the_dataset_snapshot(self):
        conn = self.proven()
        build = build_dataset(conn)
        before = {name: list(build.partitions[name]) for name in LD.PARTITIONS}
        evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        after = {name: list(build.partitions[name]) for name in LD.PARTITIONS}
        self.assertEqual(before, after)
        for name in LD.PARTITIONS:
            self.assertTrue(all(sample.partition == name for sample in after[name]))

    def test_module_keeps_research_only_identity(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertEqual(build_eval.manifest["execution_authority"], "none")
        self.assertEqual(build_eval.manifest["evaluation_scope"], "research_read_only")
        self.assertFalse(build_eval.scoring["training_performed"])
        self.assertTrue(build_eval.scoring["model_agnostic"])

    def test_provenance_never_grants_execution_authority(self):
        """Even a fully proven artifact keeps zero execution authority."""
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = evaluate_build(build, predictions_for(build), model_id=MODEL_ID)
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)
        self.assertEqual(build_eval.model_provenance["selection_partition"], "validation")
        self.assertEqual(build_eval.manifest["execution_authority"], "none")
        self.assertEqual(build_eval.manifest["evaluation_scope"], "research_read_only")


if __name__ == "__main__":
    unittest.main()
