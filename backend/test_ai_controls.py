# -*- coding: utf-8 -*-
"""Offline regression tests for AI permissions, PIT attribution and ModLens input."""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
# The runtime image intentionally has no requests dependency; these tests do
# not make network calls and only need import-time compatibility.
sys.modules.setdefault("requests", mock.MagicMock())
import adaptive_selection as AS  # noqa: E402
import deepseek_advisor as DA  # noqa: E402
import dual_ai_tuner as DT  # noqa: E402
import modlens_bridge as MB  # noqa: E402
import trade_attribution as TA  # noqa: E402


class AIControlTests(unittest.TestCase):
    def test_realtime_patch_is_factor_only_and_three_percent(self):
        base = dict(AS.BASE_WEIGHTS["one_to_two"])
        patch = DA._bounded_tuning_patch(
            {"entry_score_delta": 0.0},
            {"weights": {key: 100 for key in base},
             "entry_score_delta": 0.9,
             "conditions": {"pct_high": 0.01}},
            "one_to_two", base, AS._conditions("one_to_two"),
        )
        self.assertEqual(set(patch), {"weights"})
        self.assertTrue(all(abs(patch["weights"][key] - base[key]) <= 0.030001 for key in base))

    def test_auto_validator_rejects_conditions_and_thresholds(self):
        base = {"weights": dict(AS.BASE_WEIGHTS["one_to_two"]), "entry_score_delta": 0.0}
        self.assertFalse(AS._factor_only_patch(
            {"weights": dict(base["weights"]), "conditions": {}}, base, "one_to_two"
        ))
        self.assertFalse(AS._factor_only_patch(
            {"weights": dict(base["weights"]), "entry_score_delta": 0.01}, base, "one_to_two"
        ))
        changed = dict(base["weights"])
        changed["mom_short"] += 0.02
        self.assertTrue(AS._factor_only_patch({"weights": changed}, base, "one_to_two"))

    def test_weight_patch_merges_current_overlay(self):
        params = {"adaptive_selection": {
            "model_family": "one_to_two",
            "conditions": {"pct_high": 5.0},
            "entry_paths": {"normal": True},
            "weights": dict(AS.BASE_WEIGHTS["one_to_two"]),
        }}
        changed = dict(params["adaptive_selection"]["weights"])
        changed["mom_short"] += 0.01
        merged = AS._merge_selection_overlay(
            params, {"weights": changed}, params["adaptive_selection"]["weights"], "one_to_two"
        )
        self.assertEqual(merged["conditions"], {"pct_high": 5.0})
        self.assertEqual(merged["entry_paths"], {"normal": True})
        self.assertEqual(merged["weights"], changed)

    def test_dual_ai_unknown_factor_is_rejected(self):
        base = {"weights": dict(AS.BASE_WEIGHTS["one_to_two"])}
        unknown = dict(base["weights"])
        unknown["invented_factor"] = 0.05
        proposal = {"account_id": "tq_breakout", "confidence": 90,
                    "weights": unknown, "entry_score_delta": 0.0, "conditions": {}}
        ok, reason, merged = DT._check_consensus([proposal], [proposal], {"tq_breakout": base})
        self.assertFalse(ok)
        self.assertFalse(merged)
        self.assertIn("未知因子", reason)


class PointInTimeTests(unittest.TestCase):
    def test_historical_quote_does_not_use_current_snapshot(self):
        target = dt.date(2026, 8, 24)
        class FakeFrame:
            empty = False
            def __init__(self):
                self.close = {dt.datetime(2026, 8, 24): 100.0}
            def __getitem__(self, key):
                return self.close
            def __contains__(self, key):
                return key == "close"

        fake_fetcher = types.SimpleNamespace(load_cached_kline=lambda code: FakeFrame())
        with mock.patch.dict(sys.modules, {"data_fetcher": fake_fetcher}):
            price, quality = TA._point_in_time_quote(
                "000001", target,
                {"000001": {"price": 200.0, "quote_at": "2026-08-25T10:00:00"}},
                {"saved_at": "2026-08-25T10:00:00"},
            )
        self.assertEqual(price, 100.0)
        self.assertEqual(quality, "historical_kline")

    def test_horizon_does_not_use_future_bars(self):
        class FakeFrame:
            empty = False
            def __init__(self):
                self.close = {
                    dt.datetime(2026, 8, 24): 101.0,
                    dt.datetime(2026, 8, 25): 102.0,
                    dt.datetime(2026, 8, 26): 103.0,
                }
            def __getitem__(self, key):
                return self.close
            def __contains__(self, key):
                return key == "close"

        fake_fetcher = types.SimpleNamespace(load_cached_kline=lambda code: FakeFrame())
        with mock.patch.dict(sys.modules, {"data_fetcher": fake_fetcher}):
            result = TA._horizon_results(
                "000001", dt.date(2026, 8, 23), 100.0,
                dt.date(2026, 8, 25), 102.0, None,
            )
        self.assertEqual(result["1d"]["target_date"], "2026-08-24")
        self.assertNotIn("3d", result)

    def test_horizon_uses_cumulative_benchmark_return(self):
        class FakeFrame:
            empty = False
            def __init__(self, closes):
                self.close = closes
            def __getitem__(self, key):
                return self.close
            def __contains__(self, key):
                return key == "close"

        stock = {dt.datetime(2026, 8, day): 100.0 + day for day in (24, 25, 26, 27, 28)}
        benchmark = {dt.datetime(2026, 8, day): 100.0 + (day - 23) * 2 for day in (23, 24, 25, 26, 27, 28)}
        fake_fetcher = types.SimpleNamespace(load_cached_kline=lambda code: FakeFrame(benchmark if code == TA.BENCHMARK_CACHE_KEY else stock))
        with mock.patch.dict(sys.modules, {"data_fetcher": fake_fetcher}):
            result = TA._horizon_results(
                "000001", dt.date(2026, 8, 23), 100.0,
                dt.date(2026, 8, 28), 105.0,
                {"price": 110.0, "quote_at": "2026-08-28T15:00:00+08:00"},
            )
        self.assertAlmostEqual(result["3d"]["benchmark_return_pct"], 6.0, places=3)

    def test_same_day_news_after_fill_is_excluded(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE news_events(
            code TEXT,name TEXT,title TEXT,source_name TEXT,source_type TEXT,
            evidence_grade TEXT,published_at TEXT,first_seen_at TEXT,event_type TEXT,
            expected_direction REAL,severity REAL,source_url TEXT)""")
        conn.execute("INSERT INTO news_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            "000001", "x", "late", "source", "news", "A",
            "2026-08-25T10:00:00+08:00", "2026-08-25T10:00:00+08:00",
            "news", 1, 1, "",))
        rows = TA._event_rows(conn, "000001", dt.date(2026, 8, 25), fill_at="2026-08-25T09:30:00+08:00")
        self.assertEqual(rows, [])


class ModLensInputTests(unittest.TestCase):
    def test_modlens_rejects_outside_path_and_remote_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "uploads"
            allowed.mkdir()
            image = allowed / "ok.png"
            image.write_bytes(b"png")
            with mock.patch.object(MB, "_allowed_dirs", return_value=[allowed]):
                resolved, error = MB._validate_image_reference(str(Path(tmp) / "secret.png"))
                self.assertIsNone(resolved)
                self.assertIn("允许", error)
                resolved, error = MB._validate_image_reference("https://example.com/a.png")
                self.assertIsNone(resolved)
                self.assertIn("禁用", error)
                resolved, error = MB._validate_image_reference("ok.png")
                self.assertEqual(Path(resolved), image.resolve())
                self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
