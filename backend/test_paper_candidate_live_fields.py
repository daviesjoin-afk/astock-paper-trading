import unittest
from unittest.mock import patch

import pandas as pd
from pandas.testing import assert_frame_equal

import paper_candidate_live_fields as L


def scalar_overlay(table, rows, fields):
    for code, row in rows.items():
        if code not in table.index:
            continue
        for target, source in fields.items():
            value = row.get(source)
            if value is not None and str(value).strip() not in {"", "--", "-"}:
                table.loc[code, target] = value


class LiveFieldOverlayTests(unittest.TestCase):
    def test_matches_scalar_prices_missing_markers_nan_and_new_columns(self):
        table = pd.DataFrame({"price": [9.0] * 5, "pct": [2.0] * 5},
                             index=["000005", "000001", "000003", "000002", "000004"])
        rows = {"000001": {"price": 10.0, "pct": 0, "amount": 100},
                "000002": {"price": None, "pct": "--", "amount": None},
                "000003": {"price": float("nan"), "pct": " ", "amount": 200},
                "000004": {"price": "-", "pct": -1.0},
                "orphan": {"price": 999, "amount": 999}}
        fields = {"price": "price", "pct": "pct", "amount": "amount", "absent": "absent"}
        expected = table.copy()
        scalar_overlay(expected, rows, fields)
        L.overlay(table, rows, fields)
        assert_frame_equal(table, expected)
        self.assertEqual(list(table.index), ["000005", "000001", "000003", "000002", "000004"])
        self.assertNotIn("absent", table)

    def test_duplicate_index_rows_keep_original_assignment_semantics(self):
        table = pd.DataFrame({"price": [1.0, 2.0, 3.0]}, index=["a", "a", "b"])
        rows = {"a": {"price": 4.0}, "b": {"price": 5.0}}
        expected = table.copy()
        scalar_overlay(expected, rows, {"price": "price"})
        L.overlay(table, rows, {"price": "price"})
        assert_frame_equal(table, expected)

    def test_assignment_count_depends_on_columns_not_market_size(self):
        codes = [f"{i:06d}" for i in range(5589)]
        table = pd.DataFrame({"price": 1.0, "pct": 0.0}, index=codes)
        rows = {code: {"price": 2.0, "pct": 1.0} for code in codes}
        indexer = type(table.loc)
        original = indexer.__setitem__
        calls = []

        def record(self, key, value):
            calls.append(key)
            return original(self, key, value)

        with patch.object(indexer, "__setitem__", record):
            L.overlay(table, rows, {"price": "price", "pct": "pct"})
        self.assertEqual(len(calls), 2)
        self.assertTrue(table["price"].eq(2.0).all())
        self.assertTrue(table["pct"].eq(1.0).all())
