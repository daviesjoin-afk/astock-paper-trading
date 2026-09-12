# -*- coding: utf-8 -*-
import gzip
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import strategy_plugins as plugins
import strategy_trace as trace


class StrategyReplayTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {
                "ASTOCK_CACHE_DIR": self.temp.name,
                "ASTOCK_GIT_COMMIT": "A1B2C3D4E5F67890",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def _table():
        table = pd.DataFrame(
            {
                "factor_a": [0.9, 0.4, 0.7],
                "factor_b": [3, 2, 1],
                "name": ["A", "B", "C"],
            },
            index=["000001", "000002", "000003"],
        )
        table.attrs["data_date"] = "2026-09-11"
        return table

    @staticmethod
    def _plugin():
        def runner(table, *, topn=2, **_kwargs):
            ranked = table.sort_values("factor_a", ascending=False).head(topn)
            return {
                "strategy": "trace_test",
                "count": len(ranked),
                "picks": [
                    {"code": str(code), "score": float(row["factor_a"])}
                    for code, row in ranked.iterrows()
                ],
            }

        return plugins.StrategyPlugin(
            "trace_test",
            "trace_test",
            ("factor_a", "factor_b"),
            candidate_runner=runner,
        )

    def _traced(self, *, plugin=None, table=None, **kwargs):
        return (plugin or self._plugin()).select_candidates(
            table if table is not None else self._table(),
            replay_required=True,
            **kwargs,
        )

    def _root(self):
        return Path(self.temp.name) / "strategy_replay"

    def _run_path(self, snapshot_id):
        return self._root() / "runs" / f"{snapshot_id}.json.gz"

    def test_low_level_candidate_call_has_no_replay_side_effect(self):
        result = self._plugin().select_candidates(self._table(), topn=1)
        self.assertEqual(result["picks"][0]["code"], "000001")
        self.assertNotIn("replay_trace", result)
        self.assertNotIn("candidate_trace", result["picks"][0])
        self.assertFalse(self._root().exists())

    def test_candidate_trace_persists_factor_date_code_and_replay_artifact(self):
        result = self._traced(topn=2)
        replay = result["replay_trace"]
        self.assertEqual(replay["data_date"], "2026-09-11")
        self.assertEqual(replay["code_version"], "a1b2c3d4e5f6")
        self.assertEqual(replay["row_count"], 3)
        self.assertEqual(replay["retention_days"], trace.REPLAY_RETENTION_DAYS)
        self.assertRegex(replay["snapshot_id"], r"^[0-9a-f]{64}$")
        self.assertTrue(self._run_path(replay["snapshot_id"]).is_file())

        pick_trace = result["picks"][0]["candidate_trace"]
        self.assertEqual(pick_trace["snapshot_id"], replay["snapshot_id"])
        self.assertEqual(pick_trace["data_date"], "2026-09-11")
        self.assertEqual(pick_trace["code_version"], "a1b2c3d4e5f6")
        self.assertEqual(pick_trace["factor_snapshot"], {"factor_a": 0.9, "factor_b": 3})
        self.assertNotIn("name", pick_trace["factor_snapshot"])

    def test_explicit_data_date_is_authoritative_over_table_metadata(self):
        result = self._plugin().select_candidates(
            self._table(),
            topn=1,
            replay_required=True,
            replay_data_date="2026-09-10",
        )
        self.assertEqual(result["replay_trace"]["data_date"], "2026-09-10")
        self.assertEqual(result["picks"][0]["candidate_trace"]["data_date"], "2026-09-10")

    def test_replay_reproduces_candidate_generation(self):
        plugin = self._plugin()
        original = self._traced(plugin=plugin, topn=2)
        replayed = plugin.replay_candidates(original["replay_trace"]["snapshot_id"])
        self.assertEqual(replayed["strategy"], original["strategy"])
        self.assertEqual(replayed["count"], original["count"])
        self.assertEqual(replayed["picks"], [
            {key: value for key, value in pick.items() if key != "candidate_trace"}
            for pick in original["picks"]
        ])

    def test_replay_rejects_different_code_revision(self):
        plugin = self._plugin()
        original = self._traced(plugin=plugin, topn=1)
        snapshot_id = original["replay_trace"]["snapshot_id"]
        with mock.patch.dict(os.environ, {"ASTOCK_GIT_COMMIT": "bbbbbbb12345"}, clear=False):
            with self.assertRaisesRegex(ValueError, "code version mismatch"):
                plugin.replay_candidates(snapshot_id)

    def test_table_columns_are_content_addressed_and_reused(self):
        plugin = self._plugin()
        first = self._traced(plugin=plugin, topn=1)
        first_snapshot = trace.load_snapshot(first["replay_trace"]["snapshot_id"])
        second = self._traced(plugin=plugin, topn=2)
        second_snapshot = trace.load_snapshot(second["replay_trace"]["snapshot_id"])
        self.assertNotEqual(first["replay_trace"]["snapshot_id"], second["replay_trace"]["snapshot_id"])
        self.assertEqual(first_snapshot["table"], second_snapshot["table"])
        column_dir = self._root() / "columns"
        self.assertEqual(len(list(column_dir.glob("*.json.gz"))), 3)

    def test_retention_prunes_expired_runs_without_deleting_live_shared_blobs(self):
        plugin = self._plugin()
        first = self._traced(plugin=plugin, topn=1)
        second = self._traced(plugin=plugin, topn=2)
        first_id = first["replay_trace"]["snapshot_id"]
        second_id = second["replay_trace"]["snapshot_id"]
        now_value = time.time()
        old = now_value - (trace.REPLAY_RETENTION_DAYS + 5) * 86400

        os.utime(self._run_path(first_id), (old, old))
        report = trace.prune_snapshots(trace_dir=self._root(), now=now_value)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["deleted_runs"], 1)
        self.assertFalse(self._run_path(first_id).exists())
        self.assertTrue(self._run_path(second_id).exists())
        replayed = plugin.replay_candidates(second_id)
        self.assertEqual(replayed["count"], 2)

        os.utime(self._run_path(second_id), (old, old))
        for kind in ("indexes", "columns"):
            for path in (self._root() / kind).glob("*.json.gz"):
                os.utime(path, (old, old))
        report = trace.prune_snapshots(trace_dir=self._root(), now=now_value)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["deleted_runs"], 1)
        self.assertFalse(self._run_path(second_id).exists())
        self.assertEqual(list((self._root() / "indexes").glob("*.json.gz")), [])
        self.assertEqual(list((self._root() / "columns").glob("*.json.gz")), [])

    def test_undeclared_selection_input_fails_closed_instead_of_being_dropped(self):
        with self.assertRaisesRegex(ValueError, "undeclared selection input: account_state"):
            self._traced(topn=1, account_state={"cash": 123456})
        self.assertFalse(self._root().exists())

    def test_prohibited_market_columns_fail_closed(self):
        table = self._table()
        table["account_id"] = "real-account"
        with self.assertRaisesRegex(ValueError, "prohibited table columns"):
            self._traced(table=table, topn=1)
        self.assertFalse(self._root().exists())

    def test_prohibited_nested_whitelisted_input_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "prohibited field"):
            self._traced(topn=1, gate={"light": "green", "api_token": "secret"})

    def test_mixed_factor_dates_fail_closed(self):
        table = self._table()
        table.attrs.clear()
        table["last_date"] = ["2026-09-11", "2026-09-10", "2026-09-11"]
        with self.assertRaisesRegex(ValueError, "mixed data dates"):
            self._traced(table=table, topn=1)

    def test_latest_selection_cache_is_never_used_as_historical_date_fallback(self):
        table = self._table()
        table.attrs.clear()
        with open(Path(self.temp.name) / "selection_cache.json", "w", encoding="utf-8") as handle:
            json.dump({"factor_date": "2026-09-11"}, handle)
        with self.assertRaisesRegex(ValueError, "cannot prove factor data date"):
            self._traced(table=table, topn=1)

    def test_replay_required_without_any_date_fails_closed(self):
        table = self._table()
        table.attrs.clear()
        with self.assertRaisesRegex(ValueError, "cannot prove factor data date"):
            self._traced(table=table, topn=1)

    def test_missing_declared_optional_factor_is_recorded_as_null(self):
        plugin = plugins.StrategyPlugin(
            "optional_factor",
            "optional_factor",
            ("factor_a", "does_not_exist"),
            candidate_runner=lambda table, **_kwargs: {
                "strategy": "optional_factor",
                "count": 1,
                "picks": [{"code": str(table.index[0])}],
            },
        )
        result = self._traced(plugin=plugin, topn=1)
        self.assertEqual(
            result["picks"][0]["candidate_trace"]["factor_snapshot"],
            {"factor_a": 0.9, "does_not_exist": None},
        )
        replayed = plugin.replay_candidates(result["replay_trace"]["snapshot_id"])
        self.assertEqual(replayed["picks"], [{"code": "000001"}])

    def test_snapshot_id_is_stable_for_set_selection_inputs(self):
        plugin = self._plugin()
        one = self._traced(
            plugin=plugin, topn=1, first_board_codes={"000003", "000001"}
        )
        two = self._traced(
            plugin=plugin, topn=1, first_board_codes={"000001", "000003"}
        )
        self.assertEqual(one["replay_trace"]["snapshot_id"], two["replay_trace"]["snapshot_id"])

    def test_integrity_check_rejects_modified_snapshot(self):
        result = self._traced(topn=1)
        snapshot_id = result["replay_trace"]["snapshot_id"]
        path = self._run_path(snapshot_id)
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["data_date"] = "2026-09-10"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.assertRaisesRegex(ValueError, "integrity mismatch"):
            trace.load_snapshot(snapshot_id)

    def test_snapshot_from_another_strategy_cannot_be_replayed(self):
        original = self._traced(topn=1)
        other = plugins.StrategyPlugin(
            "other", "other", ("factor_a",),
            candidate_runner=lambda table, **_kwargs: {
                "strategy": "other", "count": 0, "picks": []
            },
        )
        with self.assertRaisesRegex(ValueError, "another strategy"):
            other.replay_candidates(original["replay_trace"]["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
