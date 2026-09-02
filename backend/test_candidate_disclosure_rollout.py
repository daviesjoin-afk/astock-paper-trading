"""Focused regression checks for disclosure ranking rollout.

Run inside the backend container because the trading module imports the live
data/strategy stack.  These cases are network-free: the disclosure provider
is replaced with deterministic evidence.
"""
from __future__ import annotations

import datetime as dt
import unittest

import paper_trading as P


class CandidateDisclosureRolloutTests(unittest.TestCase):
    def setUp(self):
        self.original = P.DT

    def tearDown(self):
        P.DT = self.original

    def test_reported_disclosure_breaks_tie_without_dropping_unknown(self):
        class Timeline:
            @staticmethod
            def fetch_disclosure_timeline(codes, asof=None):
                return [
                    {"code": "000001", "report_period": "2026-03-31", "published_at": "2026-05-01T18:00:00+08:00", "source": "eastmoney_notice", "status": "live"},
                    {"code": "000002", "source": "unknown", "status": "unknown", "reason": "missing"},
                ]
        P.DT = Timeline
        picks, summary = P._attach_candidate_financial_disclosure([
            {"code": "000002", "score": 1.0, "name": "B"},
            {"code": "000001", "score": 1.0, "name": "A"},
        ], dt.date(2026, 8, 13))
        self.assertEqual(len(picks), 2)
        self.assertEqual(picks[0]["code"], "000001")
        self.assertEqual(picks[1]["code"], "000002")
        self.assertEqual(summary["reported"], 1)
        self.assertEqual(summary["unknown"], 1)
        self.assertFalse(summary["hard_gate_readiness"]["ready"])

    def test_sector_hot_lane_is_reserved_in_live_review_budget(self):
        regular = [
            {"code": f"600{i:03d}", "score": 0.95 - i / 1000}
            for i in range(20)
        ]
        hot = {
            "code": "002963", "score": 0.61, "entry_path": "ths_hot",
            "candidate_status": "ths_hot_lane",
        }
        selected = P._prioritize_live_candidate_budget(
            regular + [hot], "sector_rotation", limit=12,
        )
        self.assertEqual(len(selected), 12)
        self.assertIn("002963", {item["code"] for item in selected})
        self.assertEqual(selected[0]["code"], "002963")

    def test_hot_lane_does_not_bypass_other_strategy_rank_budget(self):
        regular = [
            {"code": f"600{i:03d}", "score": 0.95 - i / 1000}
            for i in range(12)
        ]
        hot = {
            "code": "002963", "score": 0.10, "entry_path": "ths_hot",
            "candidate_status": "ths_hot_lane",
        }
        selected = P._prioritize_live_candidate_budget(
            regular + [hot], "tq_breakout", limit=12,
        )
        self.assertNotIn("002963", {item["code"] for item in selected})

    def test_ths_concept_cannot_be_crowded_out_by_hot_leader_lane(self):
        leaders = [
            {"code": f"300{i:03d}", "score": 0.90 - i / 1000,
             "candidate_status": "hot_leader_watch", "entry_path": "sector_heat"}
            for i in range(8)
        ]
        concept = {
            "code": "002963", "score": 0.55, "entry_path": "ths_hot",
            "candidate_status": "ths_hot_lane",
        }
        selected = P._prioritize_live_candidate_budget(
            leaders + [concept], "sector_rotation", limit=4,
        )
        self.assertIn("002963", {item["code"] for item in selected})

    def test_source_failure_keeps_candidates_unmodified(self):
        class Timeline:
            @staticmethod
            def fetch_disclosure_timeline(codes, asof=None):
                raise TimeoutError("offline")
        P.DT = Timeline
        picks, summary = P._attach_candidate_financial_disclosure([
            {"code": "000001", "score": 1.25},
        ], dt.date(2026, 8, 13))
        self.assertEqual(picks[0]["score"], 1.25)
        self.assertEqual(summary["status"], "unavailable")
        self.assertIn("不可用", summary["reason"])


if __name__ == "__main__":
    unittest.main()
