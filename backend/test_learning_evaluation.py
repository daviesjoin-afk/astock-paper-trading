# -*- coding: utf-8 -*-
"""PR-9 contract tests: model-agnostic reproducible shadow evaluation gate.

Every test here is hermetic: an in-memory SQLite database, no network, and no
execution path.  The suite deliberately does not just assert "the happy path is
green" -- it asserts that the *refusals* happen and, more importantly, that the
statistics mean what they claim.  An evaluation gate that hands out out-of-sample
credibility for a row count, or for a positive point estimate without a bound, is
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
    build, score_of=score_by_code, *, model_id="shadow-a", partition="test", dataset_fingerprint=None
):
    """One prediction per held-out sample, bound to the dataset fingerprint."""
    fingerprint = dataset_fingerprint if dataset_fingerprint is not None else build.fingerprint
    rows = []
    for sample in build.partitions.get(partition) or []:
        rows.append(
            {
                "dataset_fingerprint": fingerprint,
                "model_id": model_id,
                "sample_key": sample.sample_key,
                "code": sample.code,
                "partition": partition,
                "label_start_date": sample.label_start_date,
                "score": score_of(sample),
            }
        )
    return rows


def evaluate(conn, score_of=score_by_code, *, model_id=None, **kwargs):
    """Build the dataset, evaluate the predictions in memory, return both."""
    build = build_dataset(conn)
    build_eval = LE.build_evaluation(
        build, predictions_for(build, score_of, model_id=model_id or "shadow-a"), model_id=model_id, **kwargs
    )
    return build, build_eval


def record(conn, build, score_of=score_by_code, **kwargs):
    predictions = predictions_for(build, score_of, **kwargs)
    LE.record_predictions(conn, predictions)
    return predictions


def gate(conn, **kwargs):
    kwargs.setdefault("cutoff", CUTOFF)
    return LE.contract_status(conn, **kwargs)


def active_exclusions(build):
    return {key: count for key, count in build.exclusions.items() if count}


def test_dates(build):
    return sorted({sample.label_start_date for sample in build.partitions["test"]})


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
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
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
        forward = LE.build_evaluation(build, predictions + conflicting, model_id="shadow-a")
        backward = LE.build_evaluation(build, conflicting + predictions, model_id="shadow-a")
        self.assertEqual(forward.blockers, backward.blockers)
        self.assertEqual(forward.fingerprint, backward.fingerprint)
        self.assertIn("evaluation_prediction_conflict", forward.blockers)

    def test_persisted_conflict_is_detected_by_the_gate(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        LE.record_predictions(conn, predictions)
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
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertEqual(active_exclusions(build_eval)["invalid_prediction_score"], 3)
        for row in predictions[:3]:
            self.assertNotEqual(LE.normalize_prediction(row)["score"], "0")

    def test_missing_model_id_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        predictions[0] = dict(predictions[0], model_id="")
        predictions[1] = dict(predictions[1], model_id="")
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertEqual(active_exclusions(build_eval)["missing_model_id"], 2)
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)

    def test_predictions_for_another_model_are_audited_not_scored(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, model_id="shadow-a")
        predictions += predictions_for(build, model_id="shadow-b")
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertEqual(active_exclusions(build_eval)["other_model"], len(build.partitions["test"]))
        self.assertEqual(build_eval.evaluated_rows, len(build.partitions["test"]))


# ───────────────────────── dataset fingerprint binding ─────────────────────────


class FingerprintBindingTests(DbTestCase):
    """An evaluation is bound to exactly one dataset fingerprint."""

    def test_prediction_from_another_dataset_is_never_accepted(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, dataset_fingerprint="0" * 64)
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertEqual(active_exclusions(build_eval)["unbound_prediction"], len(predictions))
        self.assertIn("evaluation_no_held_out_predictions", build_eval.blockers)
        self.assertEqual(build_eval.evaluated_dates, 0)

    def test_missing_dataset_fingerprint_fails_closed(self):
        conn = self.proven()
        build = build_dataset(conn)
        view = LE._DatasetView(fingerprint="", cutoff=CUTOFF, partitions=build.partitions)
        build_eval = LE.build_evaluation(view, predictions_for(build), model_id="shadow-a")
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

    def test_dataset_fingerprint_participates_in_the_evaluation_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        baseline = LE.build_evaluation(build, predictions, model_id="shadow-a")
        other = LE._DatasetView(fingerprint="a" * 64, cutoff=CUTOFF, partitions=build.partitions)
        rebound = predictions_for(other, model_id="shadow-a")
        changed = LE.build_evaluation(other, rebound, model_id="shadow-a")
        self.assertNotEqual(baseline.fingerprint, changed.fingerprint)

    def test_ambiguous_model_evidence_fails_closed(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, model_id="shadow-a")
        predictions += predictions_for(build, model_id="shadow-b")
        build_eval = LE.build_evaluation(build, predictions)
        self.assertIn("evaluation_model_ambiguous", build_eval.blockers)
        self.assertEqual(build_eval.evaluated_dates, 0)

    def test_explicit_model_selection_resolves_ambiguity(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, model_id="shadow-a")
        predictions += predictions_for(build, model_id="shadow-b", score_of=score_negative)
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertNotIn("evaluation_model_ambiguous", build_eval.blockers)
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)


# ─────────────────────────── held-out / leakage ───────────────────────────


class HoldoutLeakageTests(DbTestCase):
    """Only the chronological held-out partition may be scored."""

    def test_train_partition_prediction_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, partition="train")
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertGreater(active_exclusions(build_eval).get("not_held_out", 0), 0)
        self.assertEqual(build_eval.evaluated_dates, 0)
        self.assertIn("evaluation_no_held_out_predictions", build_eval.blockers)

    def test_validation_partition_prediction_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, partition="validation")
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertEqual(active_exclusions(build_eval)["not_held_out"], len(predictions))

    def test_partition_mismatch_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build, partition="test")
        for row in predictions[:2]:
            row["partition"] = "train"
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertEqual(active_exclusions(build_eval)["partition_mismatch"], 2)

    def test_prediction_created_after_the_cutoff_is_a_lookahead_leak(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        predictions[0] = dict(predictions[0], prediction_asof="2027-01-01T10:00:00")
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertEqual(active_exclusions(build_eval)["future_prediction"], 1)

    def test_prediction_created_before_the_cutoff_is_accepted(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        for row in predictions:
            row["prediction_asof"] = "2026-01-02T10:00:00"
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertNotIn("future_prediction", active_exclusions(build_eval))
        self.assertTrue(build_eval.contract_ok, build_eval.blockers)

    def test_unknown_sample_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        predictions[0] = dict(predictions[0], sample_key="deadbeef" * 4)
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertEqual(active_exclusions(build_eval)["unknown_sample"], 1)

    def test_label_identity_mismatch_is_refused(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        predictions[0] = dict(predictions[0], label_start_date="2025-01-01")
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertEqual(active_exclusions(build_eval)["invalid_prediction_identity"], 1)

    def test_train_rows_are_never_counted_as_evaluated(self):
        conn = self.proven()
        build = build_dataset(conn)
        heldout = len(build.partitions["test"])
        build_eval = LE.build_evaluation(
            build, predictions_for(build), model_id="shadow-a"
        )
        self.assertEqual(build_eval.evaluated_rows, heldout)
        self.assertLess(build_eval.evaluated_rows, build.eligible_rows)


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
        build_eval = LE.build_evaluation(build, predictions_for(build), model_id="shadow-a")
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
        build_eval = LE.build_evaluation(build, predictions_for(build), model_id="shadow-a")
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
        narrow_eval = LE.build_evaluation(
            narrow, predictions_for(narrow), model_id="shadow-a"
        )
        wide_codes = tuple(f"{index:06d}" for index in range(20))
        wide_conn = self.proven(codes=wide_codes)
        wide = build_dataset(wide_conn)
        wide_eval = LE.build_evaluation(wide, predictions_for(wide), model_id="shadow-a")
        # Same per-date IC sequence, many more rows: the interval is unchanged.
        self.assertGreater(wide_eval.evaluated_rows, narrow_eval.evaluated_rows)
        self.assertAlmostEqual(wide_eval.ic_std_error, narrow_eval.ic_std_error, places=12)

    def test_dispersion_comes_from_dates_not_from_rows(self):
        conn = self.new_db()
        seed_panel(conn, day_count=40, target_fn=alternating_target)
        build = build_dataset(conn)
        build_eval = LE.build_evaluation(build, predictions_for(build), model_id="shadow-a")
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
        build_eval = LE.build_evaluation(build, predictions_for(build), model_id="shadow-a")
        self.assertGreater(build_eval.mean_rank_ic, 0.0)
        self.assertGreater(build_eval.ic_std, 0.0)
        self.assertLess(build_eval.ic_lower_bound, 0.0)
        self.assertFalse(build_eval.contract_ok)
        self.assertIn("evaluation_confidence_bound_not_positive", build_eval.blockers)


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
                "model_id": "shadow-a",
                "sample_key": sample.sample_key,
                "code": sample.code,
                "partition": "test",
                "label_start_date": single_date,
                "score": score_by_code(sample),
            }
            for sample in single
        ]
        build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
        self.assertEqual(build_eval.evaluated_dates, 1)
        self.assertIsNone(build_eval.ic_std)
        self.assertIsNone(build_eval.ic_lower_bound)
        self.assertIn("evaluation_confidence_bound_unavailable", build_eval.blockers)

    def test_lower_bound_is_strictly_below_the_mean_when_dates_disperse(self):
        conn = self.new_db()
        seed_panel(conn, day_count=40, target_fn=alternating_target)
        build = build_dataset(conn)
        build_eval = LE.build_evaluation(build, predictions_for(build), model_id="shadow-a")
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

    def test_unsupported_holdout_partition_is_an_explicit_refusal(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = LE.build_evaluation(
            build, predictions_for(build), model_id="shadow-a", holdout_partition="holdout"
        )
        self.assertIn("evaluation_holdout_partition_unsupported", build_eval.blockers)
        self.assertFalse(build_eval.contract_ok)

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
        return LE.build_evaluation(build, predictions_for(build), model_id="shadow-a")

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
        build_eval = LE.build_evaluation(build, predictions_for(build, score_negative), model_id="shadow-a")
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
        )
        self.assertEqual(recomputed, build_eval.fingerprint)
        self.assertIn("created_at", build_eval.manifest)

    def test_ensure_schema_is_idempotent(self):
        conn = self.new_db()
        report = LE.ensure_schema(conn)
        self.assertTrue(report["prediction_table"])
        self.assertTrue(report["evaluation_manifest_table"])
        LE.ensure_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(learning_evaluation_manifests)")}
        self.assertIn("evaluation_fingerprint", columns)
        self.assertIn("ic_lower_bound", columns)


# ──────────────────────────────── determinism ────────────────────────────────


class DeterminismTests(DbTestCase):
    """The same evidence must always produce the same verdict."""

    def test_same_inputs_reproduce_the_same_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        first = LE.build_evaluation(build, predictions, model_id="shadow-a")
        second = LE.build_evaluation(build, list(reversed(predictions)), model_id="shadow-a")
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 64)

    def test_rebuilding_the_dataset_reproduces_the_same_verdict(self):
        conn = self.proven()
        first_build = build_dataset(conn)
        first = LE.build_evaluation(first_build, predictions_for(first_build), model_id="shadow-a")
        second_build = build_dataset(conn)
        second = LE.build_evaluation(second_build, predictions_for(second_build), model_id="shadow-a")
        self.assertEqual(first_build.fingerprint, second_build.fingerprint)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_prediction_insertion_order_does_not_fork_the_verdict(self):
        conn = self.proven()
        build = build_dataset(conn)
        predictions = predictions_for(build)
        LE.record_predictions(conn, list(reversed(predictions)))
        first = gate(conn)
        conn2 = self.proven()
        build2 = build_dataset(conn2)
        LE.record_predictions(conn2, predictions_for(build2))
        second = gate(conn2)
        self.assertEqual(first["evaluation_fingerprint"], second["evaluation_fingerprint"])
        self.assertEqual(first["evaluation_blockers"], second["evaluation_blockers"])

    def test_fingerprint_binds_the_judged_prediction_evidence(self):
        conn = self.proven()
        build = build_dataset(conn)
        baseline = LE.build_evaluation(build, predictions_for(build), model_id="shadow-a")
        # A monotone rescaling leaves every rank IC identical, so only the
        # evidence digest can -- and must -- tell the two verdicts apart.
        noisy = predictions_for(build, score_of=lambda sample: code_value(sample.code) * 2.0)
        doubled = LE.build_evaluation(build, noisy, model_id="shadow-a")
        self.assertEqual(baseline.evaluated_rows, doubled.evaluated_rows)
        self.assertEqual(baseline.per_date_ic, doubled.per_date_ic)
        self.assertNotEqual(baseline.prediction_digest, doubled.prediction_digest)
        self.assertNotEqual(baseline.fingerprint, doubled.fingerprint)


class SensitivityTests(DbTestCase):
    """Every material input must move the evaluation fingerprint."""

    def _fingerprint(self, build, predictions, **kwargs):
        return LE.build_evaluation(build, predictions, model_id="shadow-a", **kwargs).fingerprint

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
        baseline = self._fingerprint(build, predictions_for(build, model_id="shadow-a"))
        changed = self._fingerprint(build, predictions_for(build, model_id="shadow-b"))
        self.assertNotEqual(baseline, changed)

    def test_holdout_partition_moves_the_fingerprint(self):
        conn = self.proven()
        build = build_dataset(conn)
        baseline = self._fingerprint(build, predictions_for(build))
        changed = self._fingerprint(build, predictions_for(build), holdout_partition="validation")
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
                    build_eval = LE.build_evaluation(build, predictions, model_id="shadow-a")
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
        LE.build_evaluation(build, predictions_for(build), model_id="shadow-a")
        after = {name: list(build.partitions[name]) for name in LD.PARTITIONS}
        self.assertEqual(before, after)
        for name in LD.PARTITIONS:
            self.assertTrue(all(sample.partition == name for sample in after[name]))

    def test_module_keeps_research_only_identity(self):
        conn = self.proven()
        build = build_dataset(conn)
        build_eval = LE.build_evaluation(build, predictions_for(build), model_id="shadow-a")
        self.assertEqual(build_eval.manifest["execution_authority"], "none")
        self.assertEqual(build_eval.manifest["evaluation_scope"], "research_read_only")
        self.assertFalse(build_eval.scoring["training_performed"])
        self.assertTrue(build_eval.scoring["model_agnostic"])


if __name__ == "__main__":
    unittest.main()
