"""Audit retention must limit conversion work, not the evidence metadata."""
import copy
import datetime as dt
import os
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_decision_audit as audit


class AuditWindowTests(unittest.TestCase):
    def frame(self, count=915):
        frame = pd.DataFrame({
            "close": np.arange(count, dtype=float) + 0.5,
            "volume": np.arange(count, dtype=np.int64),
        }, index=pd.date_range("2023-01-01", periods=count))
        frame.attrs["source"] = "fixture"
        return frame

    def test_only_retained_cells_are_converted_with_full_history_metadata(self):
        frame = self.frame()
        before = frame.copy(deep=True)
        with mock.patch.object(audit, "snapshot_safe", wraps=audit.snapshot_safe) as convert:
            result = audit._snapshot_kline(frame, "2026-10-19")
        # NumPy scalars recurse once through item(); even including that work,
        # conversion stays bounded by the retained window, not all 915 bars.
        self.assertLess(convert.call_count, 600)
        self.assertEqual(result["count"], 915)
        self.assertEqual(result["omitted_rows"], 795)
        self.assertEqual(result["rows_stored"], 120)
        self.assertEqual(result["source"], "fixture")
        self.assertEqual(result["first_date"], "2023-01-01")
        self.assertEqual(result["last_date"], frame.index[-1].date().isoformat())
        self.assertEqual(result["rows"][0]["close"], 795.5)
        # iterrows' existing mixed numeric coercion remains part of the contract.
        self.assertIs(type(result["rows"][0]["volume"]), float)
        pd.testing.assert_frame_equal(frame, before)
        self.assertEqual(frame.attrs, before.attrs)

    def test_cutoff_precedes_retention_and_unsorted_duplicate_dates_keep_order(self):
        frame = self.frame(180)
        frame = frame.iloc[list(range(179, -1, -1)) + [5]]
        result = audit._snapshot_kline(frame, "2023-05-30")
        self.assertEqual(result["future_rows"], 30)
        self.assertEqual(result["count"], 151)
        self.assertEqual(result["omitted_rows"], 31)
        self.assertEqual(result["first_date"], "2023-01-01")
        self.assertEqual(result["last_date"], "2023-05-30")
        self.assertEqual(result["status"], "future_excluded_truncated")
        self.assertEqual([r["close"] for r in result["rows"]],
                         [float(i) + 0.5 for i in range(118, -1, -1)] + [5.5])

    def test_all_future_and_empty_frames_keep_unknown_counts(self):
        for frame in (self.frame(), self.frame(0)):
            with self.subTest(size=len(frame)):
                result = audit._snapshot_kline(frame, "2022-12-31")
                self.assertEqual(result["rows"], [])
                self.assertEqual(result["count"], 0)
                self.assertEqual(result["future_rows"], len(frame))
                self.assertIsNone(result["first_date"])
                self.assertIsNone(result["last_date"])
                self.assertEqual(result["status"], "future_excluded" if len(frame) else "unknown")

    def test_missing_cutoff_and_non_date_index_keep_original_fallback_dates(self):
        frame = self.frame(125)
        frame.index = ["bad-index"] + [dt.date(2023, 1, 1)] * 124
        result = audit._snapshot_kline(frame, "invalid")
        self.assertEqual(result["count"], 125)
        self.assertEqual(result["first_date"], "2023-01-01")
        self.assertEqual(result["last_date"], "bad-index")
        self.assertEqual(result["future_rows"], 0)

    def test_date_column_keeps_legacy_overwrite_and_full_metadata(self):
        frame = self.frame(125)
        frame["date"] = ["1900-01-01"] + ["2023-01-01"] * 124
        result = audit._snapshot_kline(frame, "2026-10-19")
        self.assertEqual(result["first_date"], "1900-01-01")
        self.assertEqual(result["last_date"], "2023-01-01")
        self.assertEqual(result["omitted_rows"], 5)
        self.assertEqual(result["rows"][0]["date"], "2023-01-01")

    def test_nested_values_nullable_columns_and_outputs_are_independent(self):
        frame = self.frame(125)
        frame["custom"] = [{"x": [float("nan"), np.int64(3)]} for _ in range(125)]
        frame["nullable"] = pd.array([None] * 124 + [7], dtype="Int64")
        before = copy.deepcopy(frame.iloc[-1]["custom"])
        result = audit._snapshot_kline(frame)
        self.assertEqual(result["rows"][-1]["custom"], {"x": [None, 3]})
        self.assertEqual(result["rows"][-1]["nullable"], 7)
        result["rows"][-1]["custom"]["x"][1] = 99
        self.assertEqual(frame.iloc[-1]["custom"]["x"][1], before["x"][1])

    def test_iterrows_adapter_without_iloc_still_works(self):
        frame = self.frame(125)

        class Adapter:
            columns = frame.columns
            attrs = frame.attrs
            iterrows = frame.iterrows

        self.assertEqual(audit._snapshot_kline(Adapter()), audit._snapshot_kline(frame))


if __name__ == "__main__":
    unittest.main()
