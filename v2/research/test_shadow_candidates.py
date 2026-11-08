"""Tests for the shadow orchestration helpers (no network: call is stubbed)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import shadow_candidates as shadow  # noqa: E402


BASE = {
    "cycle_id": "shadow-base-1", "days": 2, "trades": 2,
    "return_bps": 120, "drawdown_bps": 30,
}
CANDIDATE = {
    "cycle_id": "shadow-candidate-1", "days": 2, "trades": 1,
    "return_bps": 400, "drawdown_bps": 90,
}


class ShadowDecideTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls = []
        self.original = shadow.call

        def fake(api, path, payload=None, expected=200):
            self.calls.append((path, payload))
            if "/performance" in path:
                if "candidate" in path:
                    return dict(CANDIDATE)
                return dict(BASE)
            if path == "evolution/decisions":
                return {"status": "promoted", "reasons": ["通过"]}
            if path == "evolution/events":
                return {"event_id": payload["event_id"], "status": "promoted"}
            return {}

        shadow.call = fake

    def tearDown(self) -> None:
        shadow.call = self.original

    def test_rolling_consistency_requires_every_window(self) -> None:
        base = {"segments": [
            {"days": ["2026-10-30"], "status": "valued",
             "performance": {"return_bps": 100, "drawdown_bps": 50}},
            {"days": ["2026-11-02"], "status": "valued",
             "performance": {"return_bps": 80, "drawdown_bps": 40}},
        ]}
        good = {"segments": [
            {"days": ["2026-10-30"], "status": "valued",
             "performance": {"return_bps": 200, "drawdown_bps": 60}},
            {"days": ["2026-11-02"], "status": "valued",
             "performance": {"return_bps": 180, "drawdown_bps": 70}},
        ]}
        stable = shadow.rolling_consistency(base, good)
        self.assertEqual(stable["status"], "consistent")
        self.assertEqual(stable["windows"], 2)

        weak = {"segments": [
            {"days": ["2026-10-30"], "status": "valued",
             "performance": {"return_bps": 200, "drawdown_bps": 60}},
            {"days": ["2026-11-02"], "status": "valued",
             "performance": {"return_bps": 90, "drawdown_bps": 70}},
        ]}
        unstable = shadow.rolling_consistency(base, weak)
        self.assertEqual(unstable["status"], "unstable")
        self.assertTrue(any("优势不足" in reason for reason in unstable["reasons"]))

        risky = {"segments": [
            {"days": ["2026-10-30"], "status": "valued",
             "performance": {"return_bps": 900, "drawdown_bps": 2_000}},
            {"days": ["2026-11-02"], "status": "valued",
             "performance": {"return_bps": 900, "drawdown_bps": 50}},
        ]}
        self.assertTrue(any(
            "回撤超限" in reason
            for reason in shadow.rolling_consistency(base, risky)["reasons"]
        ))

        uncovered = {"segments": [
            {"days": ["2026-10-30"], "status": "unavailable", "performance": None},
            {"days": ["2026-11-02"], "status": "valued",
             "performance": {"return_bps": 900, "drawdown_bps": 50}},
        ]}
        self.assertTrue(any(
            "缺少完整覆盖" in reason
            for reason in shadow.rolling_consistency(base, uncovered)["reasons"]
        ))

    def test_performance_missing_returns_none(self) -> None:
        shadow.call = lambda api, path, payload=None, expected=200: {
            "status": "unavailable", "reason": "no_reviewed_price_set"
        }
        self.assertIsNone(shadow.performance("test-api", "c", ["2026-10-30"]))

    def test_decide_requires_coverage_on_both_sides(self) -> None:
        def fake(api, path, payload=None, expected=200):
            if "candidate" in path:
                return {"status": "unavailable", "reason": "no_reviewed_price_set"}
            return dict(BASE)

        shadow.call = fake
        result = shadow.decide("test-api", "shadow-base-1", "shadow-candidate-1", ["2026-10-30"])
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["candidate_available"])

    def test_decide_maps_metrics_and_records_event(self) -> None:
        result = shadow.decide(
            "test-api",
            "shadow-base-1",
            "shadow-candidate-1",
            ["2026-10-30", "2026-11-02"],
            record_event=True,
            event_id="evo-shadow-1",
            decided_at="2026-11-06T15:00:00+08:00",
        )
        self.assertEqual(result["decision"]["status"], "promoted")
        decision_call = next(call for call in self.calls if call[0] == "evolution/decisions")
        payload = decision_call[1]
        self.assertEqual(payload["incumbent"]["candidate_id"], "shadow-base-1")
        self.assertEqual(payload["candidate"]["return_bps"], 400)
        self.assertEqual(payload["candidate"]["shadow_days"], 2)
        event_call = next(call for call in self.calls if call[0] == "evolution/events")
        self.assertEqual(event_call[1]["event_id"], "evo-shadow-1")
        self.assertEqual(event_call[1]["decided_at"], "2026-11-06T15:00:00+08:00")

    def test_recording_requires_an_explicit_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "decided_at"):
            shadow.decide(
                "test-api", "shadow-base-1", "shadow-candidate-1",
                ["2026-10-30"], record_event=True,
            )


if __name__ == "__main__":
    unittest.main()
