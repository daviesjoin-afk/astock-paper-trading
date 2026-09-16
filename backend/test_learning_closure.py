# -*- coding: utf-8 -*-
"""Learning-closure contract tests.

These tests exist to prove one thing: **no formal learning candidate can bypass
the canonical PIT dataset / split contract**.  They are hermetic (in-memory
SQLite, no network, no execution), and they deliberately assert the *refusals*
as much as the happy path -- a learning loop that quietly repairs a broken
partition is worse than one that stops.

The suite is written so the old 70/30 implementation cannot pass it: the
fixture below has a labelled sample whose future label reaches into the
validation window, which the raw-date split happily trains on and the canonical
purge removes.
"""

import dataclasses
import json
import random
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import adaptive_engine as AE  # noqa: E402
import learning_dataset as LD  # noqa: E402

FEATURES = LD.DEFAULT_ALPHA_FEATURES
CODES = ("000001", "000002", "000003")
# 26 consecutive sessions.  The last one carries no matured label, so the
# eligible date axis is 25 sessions long -> 15 / 5 / 5 before purging.  Purging
# then removes the last train and last validation session (their labels reach
# into the next window), leaving 14 / 4 / 5 -- comfortably non-empty, so a test
# failure here means a real contract break rather than a starved fixture.
SESSIONS = [f"2026-01-{day:02d}" for day in range(5, 31)]
CUTOFF = SESSIONS[-1]


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    AE._init_schema(conn)
    # The canonical layer adds the provenance columns the strict dataset needs.
    LD.ensure_schema(conn)
    return conn


def seed(conn, profile_date, code, *, end_date, target, available=None):
    """Insert one fully-proven point-in-time sample and its matured label."""
    features = {
        name: round(0.1 * (FEATURES.index(name) + 1) + int(code[-1]) * 0.01, 6)
        for name in FEATURES
    }
    conn.execute(
        """INSERT OR REPLACE INTO adaptive_alpha_samples(
               profile_date,code,industry,close_price,regime,price_momentum,main_flow,
               turnover,volume_ratio,small_size,value,created_at,
               feature_asof,feature_available_at,pit_status,source,source_version,
               contract_version,provenance_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (profile_date, code, "测试行业", 10.0, "neutral",
         *[features[name] for name in FEATURES], f"{profile_date}T16:00:00",
         profile_date, f"{profile_date}T15:15:00", LD.PIT_VERIFIED,
         "market_snapshot_full.json", "test-v1", LD.CONTRACT_VERSION, None),
    )
    conn.execute(
        """INSERT OR REPLACE INTO adaptive_alpha_returns(
               start_date,end_date,horizon,code,forward_return_pct,created_at,
               label_available_at,horizon_semantics,pit_status,source,source_version,
               contract_version,provenance_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (profile_date, end_date, 1, code, target, f"{end_date}T16:00:00",
         available or f"{end_date}T15:15:00",
         LD.HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS, LD.PIT_VERIFIED,
         LD.DATASET_KIND_ADAPTIVE_ALPHA, "test-v1", LD.CONTRACT_VERSION, None),
    )


def seed_series(conn, *, sessions=None, codes=CODES, long_label_sessions=()):
    """Seed one cross-section per session, each label maturing one session later.

    ``long_label_sessions`` gives the named session a label that reaches *past*
    the validation boundary, which the canonical purge must remove from train
    and the old raw-date split would have trained on.
    """
    sessions = list(sessions or SESSIONS)
    for index, session in enumerate(sessions[:-1]):
        span = 3 if session in long_label_sessions else 1
        end = sessions[min(index + span, len(sessions) - 1)]
        for position, code in enumerate(codes):
            seed(conn, session, code, end_date=end, target=round(0.5 * (position + 1), 6))


def run_lab(conn, min_days=5, min_rows=10, **kwargs):
    with mock.patch.object(AE, "ALPHA_MIN_PROFILE_DAYS", min_days), \
         mock.patch.object(AE, "ALPHA_MIN_MATURE_ROWS", min_rows):
        return AE._run_alpha_lab(conn, CUTOFF, **kwargs)


def detail_of(result):
    return result.get("detail") or {}


def stored_run(conn):
    row = conn.execute(
        "SELECT status,detail FROM adaptive_alpha_runs WHERE run_date=?", (CUTOFF,)
    ).fetchone()
    return (row["status"], json.loads(row["detail"])) if row else (None, {})


def candidates(conn):
    return conn.execute(
        "SELECT status,genome FROM adaptive_alpha_candidates WHERE run_date=?", (CUTOFF,)
    ).fetchall()


class LearningClosureFixtures(unittest.TestCase):
    def new_db(self):
        conn = make_db()
        self.addCleanup(conn.close)
        return conn


class CanonicalSplitReplacesRawSplit(LearningClosureFixtures):
    """The 70/30 bypass must be gone, not merely re-tuned."""

    def test_alpha_lab_no_longer_slices_dates_itself(self):
        source = Path(AE.__file__).read_text(encoding="utf-8")
        self.assertNotIn("len(dates) * 0.70", source)
        self.assertNotIn("len(dates) * 0.7", source)
        self.assertNotIn("dates[:split]", source)
        self.assertNotIn("dates[split:]", source)

    def test_alpha_lab_consumes_the_canonical_dataset(self):
        conn = self.new_db()
        seed_series(conn)
        result = run_lab(conn)
        self.assertEqual("completed", result["status"])
        detail = detail_of(result)
        self.assertEqual("learning_dataset", detail["dataset"]["split_source"])
        self.assertEqual(LD.CONTRACT_VERSION, detail["dataset"]["dataset_contract_version"])
        self.assertTrue(detail["dataset"]["dataset_fingerprint"])

    def test_recorded_partition_comes_from_the_canonical_build(self):
        conn = self.new_db()
        seed_series(conn)
        result = run_lab(conn)
        build = LD.build_dataset(conn, cutoff=CUTOFF, code_build_identity=AE.ENGINE_VERSION)
        detail = detail_of(result)
        self.assertEqual(build.fingerprint, detail["dataset"]["dataset_fingerprint"])
        self.assertEqual(
            {name: len(build.partitions[name]) for name in LD.PARTITIONS},
            detail["dataset"]["partition_rows"],
        )
        # The audit trail must not merely re-label the old hand-made split: the
        # recorded train window has to equal the canonical one.
        self.assertEqual(
            sorted({sample.feature_asof for sample in build.partitions["train"]}),
            detail["dataset"]["train_dates"],
        )
        self.assertEqual(
            sorted({sample.feature_asof for sample in build.partitions["test"]}),
            detail["dataset"]["test_dates"],
        )

    def test_raw_alpha_dataset_is_no_longer_a_partition_authority(self):
        conn = self.new_db()
        seed_series(conn)
        result = run_lab(conn)
        detail = detail_of(result)
        raw_rows = AE._alpha_dataset(conn)
        raw_dates = {row["profile_date"] for row in raw_rows}
        # The raw set still contains the session that sits on the train
        # boundary, so a split derived from raw dates would keep it in train.
        # The canonical purge removes it: a boundary session's label reaches
        # into the evaluation window, so it is not train evidence.
        boundary = SESSIONS[14]
        self.assertIn(boundary, raw_dates)
        self.assertNotIn(boundary, set(detail["dataset"]["train_dates"]))
        self.assertNotIn(boundary, set(detail["dataset"]["validation_dates"]))


class PitEligibilityIsEnforced(LearningClosureFixtures):
    def test_future_feature_is_excluded(self):
        conn = self.new_db()
        seed_series(conn)
        # A feature that only becomes visible after the cutoff.
        conn.execute(
            """INSERT OR REPLACE INTO adaptive_alpha_samples(
                   profile_date,code,industry,close_price,regime,price_momentum,main_flow,
                   turnover,volume_ratio,small_size,value,created_at,
                   feature_asof,feature_available_at,pit_status,source,source_version,
                   contract_version,provenance_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (SESSIONS[0], "000009", "测试行业", 10.0, "neutral",
             *[0.5 for _ in FEATURES], f"{SESSIONS[0]}T16:00:00",
             SESSIONS[0], "2026-12-31T15:15:00", LD.PIT_VERIFIED,
             "market_snapshot_full.json", "test-v1", LD.CONTRACT_VERSION, None),
        )
        result = run_lab(conn)
        build = LD.build_dataset(conn, cutoff=CUTOFF)
        codes = {
            sample.code for name in LD.PARTITIONS for sample in build.partitions[name]
        }
        self.assertNotIn("000009", codes)
        self.assertGreater(
            detail_of(result)["dataset"]["purge_counts"]["train"], 0
        )

    def test_immature_future_label_is_excluded(self):
        conn = self.new_db()
        seed_series(conn)
        # Same sample, but its label only matures after the cutoff.
        conn.execute(
            """INSERT OR REPLACE INTO adaptive_alpha_returns(
                   start_date,end_date,horizon,code,forward_return_pct,created_at,
                   label_available_at,horizon_semantics,pit_status,source,source_version,
                   contract_version,provenance_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (SESSIONS[0], "2026-02-02", 1, "000002", 9.0, "2026-02-02T16:00:00",
             "2026-02-02T15:15:00", LD.HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
             LD.PIT_VERIFIED, LD.DATASET_KIND_ADAPTIVE_ALPHA, "test-v1",
             LD.CONTRACT_VERSION, None),
        )
        build = LD.build_dataset(conn, cutoff=CUTOFF)
        ends = {sample.label_end_date for name in LD.PARTITIONS
                for sample in build.partitions[name]}
        self.assertTrue(all(end <= CUTOFF for end in ends), sorted(ends))
        self.assertNotIn("2026-02-02", ends)

    def test_unknown_availability_is_not_treated_as_available(self):
        conn = self.new_db()
        seed_series(conn)
        # Same fully-formed sample (feature + matured label), but with *no
        # proof* of when the feature became visible.  Unproven availability is
        # not availability.
        conn.execute(
            """INSERT OR REPLACE INTO adaptive_alpha_samples(
                   profile_date,code,industry,close_price,regime,price_momentum,main_flow,
                   turnover,volume_ratio,small_size,value,created_at,
                   feature_asof,feature_available_at,pit_status,source,source_version,
                   contract_version,provenance_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (SESSIONS[0], "000008", "测试行业", 10.0, "neutral",
             *[0.5 for _ in FEATURES], f"{SESSIONS[0]}T16:00:00",
             SESSIONS[0], None, LD.PIT_LEGACY_UNPROVEN,
             "legacy_import.json", "test-v1", LD.CONTRACT_VERSION, None),
        )
        seed(conn, SESSIONS[0], "000008", end_date=SESSIONS[1], target=1.0)
        conn.execute(
            """UPDATE adaptive_alpha_samples
                  SET feature_available_at=NULL, pit_status=?
                WHERE profile_date=? AND code=?""",
            (LD.PIT_LEGACY_UNPROVEN, SESSIONS[0], "000008"),
        )
        result = run_lab(conn)
        build = LD.build_dataset(conn, cutoff=CUTOFF)
        codes = {sample.code for name in LD.PARTITIONS for sample in build.partitions[name]}
        self.assertNotIn("000008", codes)
        self.assertGreater(build.exclusions["legacy_unproven_pit"], 0)
        self.assertEqual("completed", result["status"])


class PurgeAndPartitionContract(LearningClosureFixtures):
    def test_overlapping_label_is_purged_and_the_fixture_is_not_vacuous(self):
        conn = self.new_db()
        # ``SESSIONS[14]`` is the last train session when the axis is 25 long.
        boundary = SESSIONS[14]
        seed_series(conn, long_label_sessions=(boundary,))
        build = LD.build_dataset(conn, cutoff=CUTOFF)
        self.assertGreater(
            build.purge_counts["train"], 0,
            "the fixture must actually purge, otherwise this test proves nothing",
        )
        train_dates = {sample.feature_asof for sample in build.partitions["train"]}
        validation_dates = {
            sample.feature_asof for sample in build.partitions["validation"]
        }
        # Non-vacuity: without the purge this session would have been train (its
        # label *starts* inside the train range); it is removed only because its
        # label reaches into the validation window.
        self.assertNotIn(boundary, train_dates)
        self.assertLess(boundary, min(validation_dates))
        self.assertGreater(
            build.exclusions["overlapping_label_purged"], 0,
            "the purge must be audited as an exclusion, not dropped silently",
        )

    def test_purge_is_not_merely_a_date_comparison(self):
        """The canonical rule is label_available_at < evaluation_start_at."""
        conn = self.new_db()
        seed_series(conn)
        build = LD.build_dataset(conn, cutoff=CUTOFF)
        validation_start = min(
            sample.label_start_date for sample in build.partitions["validation"]
        )
        for sample in build.partitions["train"]:
            self.assertLess(sample.label_available_at, f"{validation_start}T00:00:00")

    def test_same_session_is_never_split_across_partitions(self):
        conn = self.new_db()
        seed_series(conn)
        build = LD.build_dataset(conn, cutoff=CUTOFF)
        seen = {}
        for name in LD.PARTITIONS:
            sessions = {sample.feature_asof for sample in build.partitions[name]}
            for session in sessions:
                self.assertNotIn(
                    session, seen,
                    f"session {session} appears in both {seen.get(session)} and {name}",
                )
                seen[session] = name
        # The fixture really does carry several codes per session, so the
        # row-level split this guards against was actually possible.
        per_session = {}
        for name in LD.PARTITIONS:
            for sample in build.partitions[name]:
                per_session.setdefault(sample.feature_asof, set()).add(sample.code)
        self.assertTrue(any(len(codes) > 1 for codes in per_session.values()))

    def test_cutoff_keeps_later_evidence_out(self):
        conn = self.new_db()
        seed_series(conn)
        build = LD.build_dataset(conn, cutoff=CUTOFF)
        for name in LD.PARTITIONS:
            for sample in build.partitions[name]:
                self.assertLessEqual(sample.feature_asof, CUTOFF)
                self.assertLessEqual(sample.label_end_date, CUTOFF)


class PartitionAuthorityIsHard(LearningClosureFixtures):
    """train fits, validation selects, test stays held out -- verified by observing the calls.

    The spies record the row objects the lab actually used, not a frame rebuilt
    alongside it: identity comparison against a separately-built frame would
    compare different objects and pass vacuously.
    """

    def _capture(self, conn):
        """Run the lab, recording its real frame and every row-set handed to fitness."""
        frames = []
        calls = []
        real_frame = AE._canonical_alpha_frame
        real_fitness = AE.AG.alpha_fitness

        def frame_spy(build, **kwargs):
            frame = real_frame(build, **kwargs)
            frames.append(frame)
            return frame

        def fitness_spy(genome, rows, features, horizon_weights):
            calls.append(frozenset(id(row) for row in rows))
            return real_fitness(genome, rows, features, horizon_weights)

        with mock.patch.object(AE, "_canonical_alpha_frame", side_effect=frame_spy), \
             mock.patch.object(AE.AG, "alpha_fitness", side_effect=fitness_spy), \
             mock.patch.object(AE, "ALPHA_MIN_PROFILE_DAYS", 5), \
             mock.patch.object(AE, "ALPHA_MIN_MATURE_ROWS", 10):
            result = AE._run_alpha_lab(conn, CUTOFF)
        self.assertEqual(1, len(frames), "the lab must build exactly one frame")
        return result, frames[0], calls

    def test_test_rows_never_reach_fitness_or_selection(self):
        conn = self.new_db()
        seed_series(conn)
        result, frame, calls = self._capture(conn)
        self.assertEqual("completed", result["status"])
        self.assertTrue(calls, "no fitness call was observed; the spy is not working")
        # Non-vacuity: all three partitions must be non-empty, and the held-out
        # set must be big enough that leaking it would be detectable.
        for name in LD.PARTITIONS:
            self.assertTrue(frame[name], f"{name} partition is empty")
        test_ids = {id(row) for row in frame["test"]}
        for row_ids in calls:
            self.assertFalse(
                row_ids & test_ids,
                "held-out test rows were handed to the fitness/selection path",
            )
        # And the frame really is the one the run used: the train rows were scored.
        train_ids = {id(row) for row in frame["train"]}
        self.assertTrue(any(row_ids & train_ids for row_ids in calls))

    def test_validation_selects_and_never_fits(self):
        conn = self.new_db()
        seed_series(conn)
        result, frame, calls = self._capture(conn)
        self.assertEqual("completed", result["status"])
        train_ids = {id(row) for row in frame["train"]}
        validation_ids = {id(row) for row in frame["validation"]}
        self.assertTrue(train_ids and validation_ids)
        train_calls = sum(1 for ids in calls if ids == train_ids)
        validation_calls = sum(1 for ids in calls if ids == validation_ids)
        # Train drives the evolution loop; validation is touched only to score
        # the handful of leaders.  If validation were folded into fitting these
        # counts would be indistinguishable.
        self.assertGreater(train_calls, 0)
        self.assertGreater(validation_calls, 0)
        self.assertGreater(train_calls, validation_calls)
        self.assertLessEqual(validation_calls, 5)
        self.assertEqual(len(calls), train_calls + validation_calls)


class FailClosedReadiness(LearningClosureFixtures):
    def _blocked(self, conn, expected_status="waiting_dataset", **kwargs):
        result = run_lab(conn, **kwargs)
        self.assertEqual(expected_status, result["status"])
        # Nothing may have been promoted for a blocked run.
        self.assertEqual([], candidates(conn))
        return detail_of(result)

    def test_fingerprint_mismatch_blocks_selection(self):
        conn = self.new_db()
        seed_series(conn)
        detail = self._blocked(conn, expected_fingerprint="deadbeef" * 8)
        self.assertEqual("dataset_fingerprint_mismatch", detail["blocker"])
        self.assertEqual("deadbeef" * 8, detail["persisted_fingerprint"])
        self.assertTrue(detail["rebuilt_fingerprint"])
        self.assertNotEqual(detail["persisted_fingerprint"], detail["rebuilt_fingerprint"])

    def test_matching_fingerprint_does_not_block(self):
        conn = self.new_db()
        seed_series(conn)
        build = LD.build_dataset(conn, cutoff=CUTOFF, code_build_identity=AE.ENGINE_VERSION)
        result = run_lab(conn, expected_fingerprint=build.fingerprint)
        self.assertEqual("completed", result["status"])

    def test_empty_partition_fails_closed(self):
        conn = self.new_db()
        seed_series(conn)
        for name in LD.PARTITIONS:
            build = LD.build_dataset(conn, cutoff=CUTOFF, code_build_identity=AE.ENGINE_VERSION)
            crippled = dataclasses.replace(build, partitions={**build.partitions, name: []})
            detail = self._blocked(
                conn, expected_status="waiting_validation_window", dataset_build=crippled
            )
            self.assertEqual(f"empty_{name}_partition", detail["blocker"])
            self.assertIn("blocked", detail["reason"])

    def test_truncated_evidence_fails_closed(self):
        conn = self.new_db()
        seed_series(conn)
        build = LD.build_dataset(conn, cutoff=CUTOFF, code_build_identity=AE.ENGINE_VERSION)
        crippled = dataclasses.replace(build, truncated=True)
        detail = self._blocked(conn, dataset_build=crippled)
        self.assertEqual("truncated_evidence", detail["blocker"])

    def test_no_eligible_rows_fails_closed(self):
        conn = self.new_db()
        seed_series(conn)
        build = LD.build_dataset(conn, cutoff=CUTOFF, code_build_identity=AE.ENGINE_VERSION)
        crippled = dataclasses.replace(
            build, partitions={name: [] for name in LD.PARTITIONS}
        )
        detail = self._blocked(conn, dataset_build=crippled)
        self.assertEqual("pit_eligible_rows_empty", detail["blocker"])

    def test_unprovable_cutoff_fails_closed(self):

        conn = self.new_db()
        seed_series(conn)
        with mock.patch.object(AE, "ALPHA_MIN_PROFILE_DAYS", 5), \
             mock.patch.object(AE, "ALPHA_MIN_MATURE_ROWS", 10):
            result = AE._run_alpha_lab(conn, "not-a-date")
        self.assertEqual("waiting_dataset", result["status"])
        self.assertEqual("unprovable_cutoff", detail_of(result)["blocker"])
        self.assertEqual([], candidates(conn))

    def test_blocked_run_is_persisted_with_its_blocker(self):
        conn = self.new_db()
        seed_series(conn)
        run_lab(conn, expected_fingerprint="a" * 64)
        status, detail = stored_run(conn)
        self.assertEqual("waiting_dataset", status)
        self.assertEqual("dataset_fingerprint_mismatch", detail["blocker"])

    def test_insufficient_samples_still_report_waiting_data(self):
        conn = self.new_db()
        seed(conn, SESSIONS[0], "000001", end_date=SESSIONS[1], target=1.0)
        result = run_lab(conn, min_days=99, min_rows=99999)
        self.assertEqual("waiting_data", result["status"])
        self.assertEqual(0, len(candidates(conn)))


class DatasetIdentityAndDeterminism(LearningClosureFixtures):
    def test_reorder_does_not_change_the_fingerprint(self):
        """Same logical evidence inserted in a different order -> same identity."""
        forward = self.new_db()
        reverse = self.new_db()
        seed_series(forward)
        for session in reversed(SESSIONS[:-1]):
            for code in reversed(CODES):
                seed(reverse, session, code,
                     end_date=sessions_after(session),
                     target=round(0.5 * (CODES.index(code) + 1), 6))
        forward_build = LD.build_dataset(forward, cutoff=CUTOFF)
        reverse_build = LD.build_dataset(reverse, cutoff=CUTOFF)
        self.assertGreater(forward_build.eligible_rows, 0)
        self.assertEqual(forward_build.fingerprint, reverse_build.fingerprint)
        self.assertEqual(
            {name: len(forward_build.partitions[name]) for name in LD.PARTITIONS},
            {name: len(reverse_build.partitions[name]) for name in LD.PARTITIONS},
        )
        self.assertEqual(forward_build.manifest["source_digest"],
                         reverse_build.manifest["source_digest"])

    def test_generation_is_bound_to_the_dataset_fingerprint(self):
        conn = self.new_db()
        seed_series(conn)
        build = LD.build_dataset(conn, cutoff=CUTOFF, code_build_identity=AE.ENGINE_VERSION)
        seeds = []
        real = random.Random

        def spy(seed=None):
            seeds.append(seed)
            return real(seed)

        with mock.patch("random.Random", spy):
            run_lab(conn)
        self.assertTrue(seeds)
        for seed in seeds:
            self.assertIsInstance(seed, str)
            self.assertIn(AE.ENGINE_VERSION, seed)
            self.assertIn(CUTOFF, seed)
            self.assertIn(build.fingerprint, seed)

    def test_same_dataset_and_engine_reproduce_the_same_candidates(self):
        first = self.new_db()
        seed_series(first)
        run_lab(first)
        second = self.new_db()
        seed_series(second)
        run_lab(second)
        self.assertEqual(
            [row["genome"] for row in candidates(first)],
            [row["genome"] for row in candidates(second)],
        )
        self.assertTrue(candidates(first))

    def test_different_evidence_changes_the_seed_identity(self):
        conn = self.new_db()
        seed_series(conn)
        first = LD.build_dataset(conn, cutoff=CUTOFF).fingerprint
        conn.execute(
            "UPDATE adaptive_alpha_returns SET forward_return_pct=forward_return_pct+0.25"
        )
        second = LD.build_dataset(conn, cutoff=CUTOFF).fingerprint
        self.assertNotEqual(first, second)


class AuditTrailRecordsDatasetIdentity(LearningClosureFixtures):
    def test_detail_carries_the_full_canonical_identity(self):
        conn = self.new_db()
        seed_series(conn)
        result = run_lab(conn)
        dataset = detail_of(result)["dataset"]
        for key in (
            "dataset_contract_version", "dataset_fingerprint", "cutoff", "split_spec",
            "partition_rows", "purge_counts", "train_dates", "validation_dates",
            "test_dates", "split_source",
        ):
            self.assertIn(key, dataset)
            self.assertIsNotNone(dataset[key], key)
        self.assertEqual(CUTOFF, dataset["cutoff"])
        self.assertEqual(LD.DEFAULT_SPLIT_SPEC, {
            name: dataset["split_spec"][name] for name in LD.PARTITIONS
        })
        self.assertEqual("none", dataset["execution_authority"])
        self.assertEqual(0, dataset["embargo_sessions"])
        self.assertIn("no established business convention", dataset["embargo_reason"])
        self.assertEqual(len(LD.PARTITIONS), len(dataset["partition_rows"]))
        self.assertEqual(LD.PARTITIONS, tuple(dataset["purge_counts"].keys()))

    def test_heldout_test_window_is_reported_separately(self):
        conn = self.new_db()
        seed_series(conn)
        result = run_lab(conn)
        detail = detail_of(result)
        self.assertEqual(detail["dataset"]["partition_rows"]["test"], detail["heldout_test_rows"])
        self.assertEqual(detail["dataset"]["test_dates"], detail["heldout_test_dates"])
        self.assertTrue(detail["heldout_test_dates"])

    def test_runs_never_claim_a_raw_split(self):
        conn = self.new_db()
        seed_series(conn)
        run_lab(conn)
        _, detail = stored_run(conn)
        self.assertEqual("learning_dataset", detail["dataset"]["split_source"])
        self.assertEqual(
            detail["dataset"]["train_dates"], detail["dataset"]["train_dates"]
        )
        self.assertNotIn(
            sorted(detail["dataset"]["train_dates"] + detail["dataset"]["validation_dates"]),
            sorted(detail["dataset"]["train_dates"]),
        )


class SingleBuildPerCycle(LearningClosureFixtures):
    def test_cycle_hands_the_lab_the_build_it_already_fingerprinted(self):
        conn = self.new_db()
        seed_series(conn)
        manifest = AE._persist_research_dataset(conn, CUTOFF)
        build = manifest["build"]
        self.assertEqual(manifest["dataset_fingerprint"], build.fingerprint)
        # Option A: the lab must accept and reuse the handed-over build rather
        # than rebuilding a second, possibly different, dataset.
        with mock.patch.object(
            LD, "build_dataset", side_effect=AssertionError("lab rebuilt the dataset")
        ):
            result = run_lab(conn, dataset_build=build,
                             expected_fingerprint=manifest["dataset_fingerprint"])
        self.assertEqual("completed", result["status"])
        self.assertEqual(
            build.fingerprint, detail_of(result)["dataset"]["dataset_fingerprint"]
        )

    def test_persisted_dataset_manifest_matches_the_lab_run(self):
        conn = self.new_db()
        seed_series(conn)
        manifest = AE._persist_research_dataset(conn, CUTOFF)
        run_lab(conn, expected_fingerprint=manifest["dataset_fingerprint"])
        _, detail = stored_run(conn)
        self.assertEqual(
            manifest["dataset_fingerprint"], detail["dataset"]["dataset_fingerprint"]
        )


def sessions_after(session):
    index = SESSIONS.index(session)
    return SESSIONS[min(index + 1, len(SESSIONS) - 1)]


if __name__ == "__main__":
    unittest.main()
