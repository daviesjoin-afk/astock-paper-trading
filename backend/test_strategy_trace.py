# -*- coding: utf-8 -*-
import gzip
import json
import os
import tempfile
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

    def _run_path(self, snapshot_id):
        return Path(self.temp.name) / "strategy_replay" / "runs" / f"{snapshot_id}.json.gz"

    def test_candidate_trace_persists_factor_date_code_and_replay_artifact(self):
        result = self._plugin().select_candidates(self._table(), topn=2)
        replay = result["replay_trace"]
        self.assertEqual(replay["data_date"], "2026-09-11")
        self.assertEqual(replay["code_version"], "a1b2c3d4e5f6")
        self.assertEqual(replay["row_count"], 3)
        self.assertRegex(replay["snapshot_id"], r"^[0-9a-f]{64}$")
        self.assertTrue(self._run_path(replay["snapshot_id"]).is_file())

        pick_trace = result["picks"][0]["candidate_trace"]
        self.assertEqual(pick_trace["snapshot_id"], replay["snapshot_id"])
        self.assertEqual(pick_trace["data_date"], "2026-09-11")
        self.assertEqual(pick_trace["code_version"], "a1b2c3d4e5f6")
        self.assertEqual(pick_trace["factor_snapshot"], {"factor_a": 0.9, "factor_b": 3})
        self.assertNotIn("name", pick_trace["factor_snapshot"])

    def test_replay_reproduces_candidate_generation(self):
        plugin = self._plugin()
        original = plugin.select_candidates(self._table(), topn=2)
        replayed = plugin.replay_candidates(original["replay_trace"]["snapshot_id"])
        self.assertEqual(replayed["strategy"], original["strategy"])
        self.assertEqual(replayed["count"], original["count"])
        self.assertEqual(replayed["picks"], [
            {key: value for key, value in pick.items() if key != "candidate_trace"}
            for pick in original["picks"]
        ])

    def test_table_columns_are_content_addressed_and_reused(self):
        plugin = self._plugin()
        first = plugin.select_candidates(self._table(), topn=1)
        first_snapshot = trace.load_snapshot(first["replay_trace"]["snapshot_id"])
        second = plugin.select_candidates(self._table(), topn=2)
        second_snapshot = trace.load_snapshot(second["replay_trace"]["snapshot_id"])
        self.assertNotEqual(first["replay_trace"]["snapshot_id"], second["replay_trace"]["snapshot_id"])
        self.assertEqual(first_snapshot["table"], second_snapshot["table"])
        column_dir = Path(self.temp.name) / "strategy_replay" / "columns"
        self.assertEqual(len(list(column_dir.glob("*.json.gz"))), 3)

    def test_snapshot_contains_only_whitelisted_selection_inputs(self):
        seen = {}

        def runner(table, **kwargs):
            seen.update(kwargs)
            return {"strategy": "trace_private", "count": 1,
                    "picks": [{"code": str(table.index[0])}]}

        plugin = plugins.StrategyPlugin(
            "trace_private", "trace_private", ("factor_a",), candidate_runner=runner
        )
        result = plugin.select_candidates(
            self._table(), topn=1, account_state={"cash": 123456}, harmless_extra="not persisted"
        )
        self.assertIn("account_state", seen)
        snapshot = trace.load_snapshot(result["replay_trace"]["snapshot_id"])
        self.assertEqual(snapshot["selection_inputs"], {"topn": 1})
        raw = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("123456", raw)
        self.assertNotIn("account_state", raw)
        self.assertNotIn("harmless_extra", raw)

    def test_prohibited_market_columns_fail_closed(self):
        table = self._table()
        table["account_id"] = "real-account"
        with self.assertRaisesRegex(ValueError, "prohibited table columns"):
            self._plugin().select_candidates(table, topn=1)
        self.assertFalse((Path(self.temp.name) / "strategy_replay").exists())

    def test_prohibited_nested_whitelisted_input_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "prohibited field"):
            self._plugin().select_candidates(
                self._table(), topn=1, gate={"light": "green", "api_token": "secret"}
            )

    def test_mixed_factor_dates_fail_closed(self):
        table = self._table()
        table.attrs.clear()
        table["last_date"] = ["2026-09-11", "2026-09-10", "2026-09-11"]
        with self.assertRaisesRegex(ValueError, "mixed data dates"):
            self._plugin().select_candidates(table, topn=1)

    def test_production_factor_cache_date_is_explicit_last_fallback(self):
        table = self._table()
        table.attrs.clear()
        with open(Path(self.temp.name) / "selection_cache.json", "w", encoding="utf-8") as handle:
            json.dump({"factor_date": "2026-09-11"}, handle)
        result = self._plugin().select_candidates(table, topn=1)
        self.assertEqual(result["replay_trace"]["data_date"], "2026-09-11")

    def test_missing_factor_date_fails_instead_of_using_today(self):
        table = self._table()
        table.attrs.clear()
        with self.assertRaisesRegex(ValueError, "cannot prove factor data date"):
            self._plugin().select_candidates(table, topn=1)

    def test_declared_factor_must_exist(self):
        plugin = plugins.StrategyPlugin(
            "missing_factor",
            "missing_factor",
            ("does_not_exist",),
            candidate_runner=lambda table, **kwargs: {
                "strategy": "missing_factor", "count": 0, "picks": []
            },
        )
        with self.assertRaisesRegex(ValueError, "factor missing"):
            plugin.select_candidates(self._table())

    def test_snapshot_id_is_stable_for_set_selection_inputs(self):
        plugin = self._plugin()
        one = plugin.select_candidates(self._table(), topn=1, first_board_codes={"000003", "000001"})
        two = plugin.select_candidates(self._table(), topn=1, first_board_codes={"000001", "000003"})
        self.assertEqual(one["replay_trace"]["snapshot_id"], two["replay_trace"]["snapshot_id"])

    def test_integrity_check_rejects_modified_snapshot(self):
        result = self._plugin().select_candidates(self._table(), topn=1)
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
        original = self._plugin().select_candidates(self._table(), topn=1)
        other = plugins.StrategyPlugin(
            "other", "other", ("factor_a",),
            candidate_runner=lambda table, **kwargs: {"strategy": "other", "count": 0, "picks": []},
        )
        with self.assertRaisesRegex(ValueError, "another strategy"):
            other.replay_candidates(original["replay_trace"]["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
