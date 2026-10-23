"""Candidate preprocessing keeps security gates and missing sector evidence."""
import datetime as dt
import os
import sys
import unittest
from contextlib import ExitStack
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_candidate_assembly as PCA  # noqa: E402
import paper_trading as PT  # noqa: E402


class TableReady(Exception):
    pass


class CandidateColumnReadTests(unittest.TestCase):
    def capture(self, *, names=True, reject_all=False):
        codes = ["600001", "600002", "600003", "600004"]
        day = dt.date(2026, 10, 15)
        table = pd.DataFrame({
            "industry": ["银行行业", None, float("nan"), "半导体Ⅱ"],
            "name": ["普通甲", "普通乙", "普通丙", "ST测试"],
        }, index=codes)
        if reject_all:
            table["name"] = "ST测试"
        if not names:
            table = table.drop(columns="name")
        original = table.copy(deep=True)
        price = pd.DataFrame({"last_date": [day.isoformat()] * 4}, index=codes)
        universe = [{"code": code, "name": "普通样本", "risk_flag": 0} for code in codes]
        captured = []

        def selected(model, frame, **kwargs):
            captured.append(frame.copy(deep=True))
            raise TableReady

        with ExitStack() as stack:
            patches = [
                (PT, "_selection_inputs", (price, set())),
                (PT, "_selection_factor_freshness", {"passed": True}),
                (PT, "CANDIDATE_FACTOR_MIN_ROWS", 1),
                (PT.U, "load_universe", universe),
                (PT.U, "latest_complete_trade_date", day),
                (PT.dfc, "fetch_finance_latest", {"data": {}}),
                (PT.F, "compute_fundamental_factors", pd.DataFrame()),
                (PT.S, "build_factor_table", table),
                # 板块证据已迁入装配模块，补丁跟随真正持有实现的模块。
                (PCA, "_star_sector_impulse", {"银行": {"bonus": 2.5}}),
                (PCA, "_sector_heat_map", {"银行": {
                    "score": 3.0, "early_rotation": True, "early_rotation_score": 1.5,
                }}),
            ]
            for module, name, result in patches:
                if name == "CANDIDATE_FACTOR_MIN_ROWS":
                    stack.enter_context(mock.patch.object(module, name, result))
                else:
                    stack.enter_context(mock.patch.object(module, name, return_value=result))
            stack.enter_context(mock.patch.object(PT.S, "run_strategy", side_effect=selected))
            with self.assertRaises(TableReady):
                PT._candidate_rows({"id": "sector_rotation", "style": "sector", "params": "{}"},
                                   day, {"light": "green"})
        pd.testing.assert_frame_equal(table, original)
        return captured[0]

    def test_security_filter_and_sector_evidence_are_preserved(self):
        frame = self.capture()
        self.assertEqual(list(frame.index), ["600001", "600002", "600003"])
        self.assertEqual(frame["star_sector_bonus"].tolist(), [2.5, 0.0, 0.0])
        self.assertEqual(frame["sector_heat_score"].tolist(), [3.0, 0.0, 0.0])
        self.assertEqual(frame["sector_early_rotation"].tolist(), [True, False, False])
        self.assertEqual(frame["sector_early_rotation_score"].tolist(), [1.5, 0.0, 0.0])
        # Existing zscore keeps a cross-section this small neutral.
        self.assertEqual(frame["sentiment"].tolist(), [0.0, 0.0, 0.0])

    def test_missing_name_uses_universe_name_and_keeps_index_order(self):
        self.assertEqual(list(self.capture(names=False).index),
                         ["600001", "600002", "600003", "600004"])

    def test_empty_security_pool_remains_empty(self):
        self.assertTrue(self.capture(reject_all=True).empty)


if __name__ == "__main__":
    unittest.main()
