"""Streaming must preserve dataset identity and the caller's SQLite transaction."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest import mock

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import learning_dataset as LD
import test_learning_dataset as FIX


class StreamingDatasetTests(FIX.DbTestCase):
    def test_join_index_preserves_evidence_and_is_installed_idempotently(self):
        conn = self.new_db()
        FIX.seed_series(conn, FIX.dates(14), horizons=(1, 3), codes=("000001", "600002"))
        conn.execute("DROP INDEX idx_alpha_returns_evidence")
        before = LD._read_alpha_evidence(conn)
        LD.ensure_schema(conn)
        LD.ensure_schema(conn)
        self.assertEqual(before, LD._read_alpha_evidence(conn))
        query = ("SELECT s.profile_date,s.code,r.horizon,r.end_date,r.label_available_at,"
                 "r.forward_return_pct FROM adaptive_alpha_samples s "
                 "LEFT JOIN adaptive_alpha_returns r ON r.start_date=s.profile_date AND r.code=s.code "
                 "ORDER BY s.profile_date,s.code,r.horizon,r.end_date,r.label_available_at,r.forward_return_pct")
        plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + query))
        self.assertIn("idx_alpha_returns_evidence", plan)
        self.assertIn("start_date=? AND code=?", plan)

    def test_fingerprint_matches_the_materialized_v1_json_byte_for_byte(self):
        conn = self.new_db()
        FIX.seed_series(conn, FIX.dates(14), horizons=(1, 3), codes=("000001", "600002"))
        built = FIX.build(conn)
        rows = [sample.canonical(built.feature_names)
                for name in LD.PARTITIONS for sample in built.partitions[name]]
        rows.sort(key=LD._canonical_sort_key)
        old_payload = {
            "fingerprint_version": LD.FINGERPRINT_VERSION,
            "contract_version": built.contract_version,
            "cutoff": built.cutoff,
            "feature_names": sorted(built.feature_names),
            "label_spec": LD._canon_json(LD.label_spec(built.horizon_semantics)),
            "horizon_semantics": built.horizon_semantics,
            "split_spec": {name: LD._canon_number(built.split_spec[name]) for name in LD.PARTITIONS},
            "source_digest": built.manifest["source_digest"],
            "rows": rows,
        }
        serialized = json.dumps(old_payload, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False)
        self.assertEqual(hashlib.sha256(serialized.encode("utf-8")).hexdigest(), built.fingerprint)
        keys = sorted(f"{s.source}|{s.source_version}|{s.feature_asof}|{s.code}|{s.horizon}|{s.sample_key}"
                      for name in LD.PARTITIONS for s in built.partitions[name])
        self.assertEqual(hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest(),
                         built.manifest["source_digest"])

    def test_production_builder_does_not_materialize_the_evidence_list(self):
        conn = self.new_db()
        FIX.seed_series(conn, FIX.dates(12), horizons=(1, 3), codes=("000001", "600002"))
        reference_rows = len(LD._read_alpha_evidence(conn))
        with mock.patch.object(LD, "_read_alpha_evidence_page",
                               side_effect=AssertionError("bulk evidence read")), \
                mock.patch.object(LD, "_rows_as_dicts",
                                  side_effect=AssertionError("fetchall evidence")):
            built = FIX.build(conn)
        self.assertEqual(reference_rows, built.manifest["source_row_count"])
        self.assertGreater(built.eligible_rows, 0)

    def test_two_passes_use_one_snapshot_when_a_label_is_added_between_them(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as connections:
            path = str(Path(directory) / "evidence.sqlite3")
            conn = sqlite3.connect(path)
            connections.callback(conn.close)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(FIX.LEGACY_SCHEMA)
            LD.ensure_schema(conn)
            FIX.seed_series(conn, FIX.dates(12), horizons=(1,), codes=("000001",))
            conn.commit()
            reference = FIX.build(conn)
            writer = sqlite3.connect(path)
            connections.callback(writer.close)
            writer.row_factory = sqlite3.Row
            old_detector = LD._ambiguous_identities

            def add_conflict_after_first_pass(evidence):
                verdict = old_detector(evidence)
                FIX.seed_label(writer, FIX.dates(12)[0], FIX.dates(12)[2], horizon=1)
                writer.commit()
                return verdict

            with mock.patch.object(LD, "_ambiguous_identities", side_effect=add_conflict_after_first_pass):
                frozen = FIX.build(conn)
            self.assertEqual(reference.fingerprint, frozen.fingerprint)
            self.assertEqual(reference.exclusions, frozen.exclusions)
            after = FIX.build(conn)
            self.assertEqual(2, after.exclusions["ambiguous_label"])
            self.assertNotEqual(frozen.fingerprint, after.fingerprint)

    def test_read_snapshot_does_not_commit_or_rollback_a_callers_changes(self):
        conn = self.new_db()
        conn.commit()
        FIX.seed_sample(conn, "2026-01-01")
        self.assertTrue(conn.in_transaction)
        FIX.build(conn)
        self.assertTrue(conn.in_transaction)
        conn.rollback()
        self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM adaptive_alpha_samples").fetchone()[0])

    def test_failed_classification_preserves_outer_transaction(self):
        conn = self.new_db()
        conn.commit()
        FIX.seed_sample(conn, "2026-01-01")
        with mock.patch.object(LD, "_classify", side_effect=RuntimeError("classification failed")):
            with self.assertRaises(RuntimeError):
                FIX.build(conn)
        self.assertTrue(conn.in_transaction)
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM adaptive_alpha_samples").fetchone()[0])
        conn.rollback()
        self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM adaptive_alpha_samples").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
