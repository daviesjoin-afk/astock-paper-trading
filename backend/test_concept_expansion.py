import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import data_fetcher as DF  # noqa: E402
import paper_trading as PT  # noqa: E402


class ConceptExpansionTests(unittest.TestCase):
    def setUp(self):
        with DF._cache_lock:
            DF._mem_cache.clear()

    def test_malformed_board_does_not_abort_snapshot(self):
        boards = [
            {"code": "BK0001", "name": "坏数据", "pct": "-", "main_net": "-"},
            {"code": "BK1138", "name": "液冷服务器", "pct": 2.0, "main_net": 1000},
        ]
        complete = {"members": [{"code": "000001", "pct": 2.0}], "complete": True,
                    "expected_count": 1, "fetched_count": 1, "pages_ok": 1,
                    "pages_expected": 1, "malformed_rows": 0}
        with mock.patch.object(DF, "fetch_sector_flow", return_value=boards), \
             mock.patch.object(DF, "_fetch_concept_members", return_value=complete):
            result = DF.fetch_hot_concept_snapshot(6)
        self.assertEqual([row["name"] for row in result], ["液冷服务器"])

    def test_incomplete_constituents_are_not_formal(self):
        boards = [{"code": "BK1138", "name": "液冷服务器", "pct": 2.0, "main_net": 1000}]
        incomplete = {"members": [{"code": "000001", "pct": 2.0}], "complete": False}
        with mock.patch.object(DF, "fetch_sector_flow", return_value=boards), \
             mock.patch.object(DF, "_fetch_concept_members", return_value=incomplete):
            self.assertEqual(DF.fetch_hot_concept_snapshot(6), [])

    def test_lane_requires_current_full_market_quote(self):
        concepts = [{"code": "BK1138", "name": "液冷服务器", "rank": 1,
                     "pct": 2.0, "main_net": 1000, "positive_ratio": 0.7,
                     "complete": True, "members": [{"code": "000001", "name": "甲"}]}]
        universe = [{"code": "000001", "name": "甲", "industry": "设备"}]
        stale = {"000001": {"code": "000001", "name": "甲", "price": 10, "pct": 2,
                            "main_net": 100, "main_pct": 2, "super_net": 50,
                            "vol_ratio": 1.2, "quote_at": "2026-08-24T10:00:00+08:00"}}
        lane, meta = PT._concept_expansion_lane_candidates(
            concepts, universe, stale, asof_date="2026-08-25")
        self.assertEqual(lane, [])
        self.assertEqual(meta["skipped_stale_live"], 1)

    def test_leader_reverse_map_expands_only_eligible_peers(self):
        leaders = [{"code": "600869", "name": "领涨股", "pct": 9.9,
                    "main_net": 1200, "vol_ratio": 2.0}]
        members = {
            "members": [
                {"code": "600869", "name": "领涨股", "pct": 9.9},
                {"code": "002536", "name": "同概念甲", "pct": 3.2},
                {"code": "000001", "name": "同概念乙", "pct": 2.1},
                {"code": "000002", "name": "同概念丙", "pct": 1.0},
                {"code": "000003", "name": "同概念丁", "pct": -0.2},
            ],
            "complete": True, "expected_count": 5, "fetched_count": 5,
            "pages_ok": 1, "pages_expected": 1, "malformed_rows": 0,
        }
        flow = [{"code": "BK1138", "name": "液冷", "pct": 0.8, "main_net": 80}]
        with mock.patch.object(DF, "_fetch_stock_concept_refs", return_value=[{"code": "BK1138", "name": "液冷"}]), \
             mock.patch.object(DF, "_fetch_concept_members", return_value=members), \
             mock.patch.object(DF, "fetch_sector_flow", return_value=flow):
            concepts = DF.fetch_leader_concept_snapshot(leaders)
        self.assertEqual(len(concepts), 1)
        self.assertEqual(concepts[0]["leader_context"][0]["code"], "600869")
        live = {
            "600869": {"code": "600869", "name": "领涨股", "price": 10, "pct": 9.9,
                       "main_net": 1200, "main_pct": 8, "super_net": 400, "vol_ratio": 2,
                       "quote_at": "2026-09-02T10:00:00+08:00"},
            "002536": {"code": "002536", "name": "同概念甲", "price": 10, "pct": 3.2,
                       "main_net": 100, "main_pct": 2, "super_net": 20, "vol_ratio": 1.3,
                       "quote_at": "2026-09-02T10:00:00+08:00"},
        }
        universe = [{"code": code, "name": row["name"], "industry": "设备"} for code, row in live.items()]
        lane, _ = PT._concept_expansion_lane_candidates(concepts, universe, live, asof_date="2026-09-02")
        self.assertEqual([row["code"] for row in lane], ["002536"])
        self.assertEqual(lane[0]["concept_context"]["discovery_path"], "leader_reverse_map")


if __name__ == "__main__":
    unittest.main()
