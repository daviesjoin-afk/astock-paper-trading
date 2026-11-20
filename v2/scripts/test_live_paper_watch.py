"""Behavior checks for the read-only intraday watchdog."""
from datetime import datetime
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch

import live_paper_watch as watch

CALENDAR = {"coverage_start": "2026-01-01", "coverage_end": "2026-12-31",
            "holidays": [{"start": "2026-10-01", "end": "2026-10-07"}]}


def at(clock, day="2026-11-20"):
    return datetime.fromisoformat(f"{day}T{clock}:00+08:00")


def round_at(clock, *, quotes=5219, ai=True):
    return {"started_at": at(clock).isoformat(), "round_id": clock,
            "quote_count": quotes, "trade_count": 1, "ai_ok": ai}


class WatchTests(unittest.TestCase):
    def observe(self, now, rounds, proposals=()):
        payloads = {"/v2/health": {"status": "ok"}, "/v2/live/rounds": rounds,
                    "/v2/live/evolution": proposals}
        opener = Mock()
        opener.open.side_effect = lambda url, timeout: BytesIO(
            json.dumps(payloads[url.removeprefix("http://kernel.example.com")]).encode())
        with patch.object(watch, "build_opener", return_value=opener):
            result = watch.observe("http://kernel.example.com", CALENDAR, now)
        return result, opener

    def test_fresh_round_passes_with_only_read_requests(self):
        result, opener = self.observe(at("14:10"), [round_at("14:09")])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["simulated_fills_today"], 1)
        self.assertEqual(result["latest_age_seconds"], 60)
        self.assertEqual([call.args[0] for call in opener.open.call_args_list],
                         ["http://kernel.example.com/v2/health", "http://kernel.example.com/v2/live/rounds"])

    def test_stale_missing_coverage_and_ai_failures_are_visible(self):
        rounds = [round_at("13:48"), round_at("13:51", ai=False),
                  round_at("13:54", ai=False), round_at("13:57", quotes=3000, ai=False)]
        result, _ = self.observe(at("14:10"), rounds)
        self.assertEqual(result["status"], "alert")
        self.assertEqual(result["issues"], ["latest_round_stale", "quote_coverage_drop",
                                          "ai_unavailable_three_rounds"])

    def test_holiday_skips_and_uncovered_year_reports_alert(self):
        for day, status in (("2026-10-05", "skipped"), ("2027-01-04", "alert")):
            with self.subTest(day=day):
                result, opener = self.observe(at("14:10", day), [])
                self.assertEqual(result["status"], status)
                opener.open.assert_not_called()

    def test_close_requires_one_inert_proposal_covering_the_day(self):
        rounds = [round_at("14:57")]
        row = {"day": "2026-11-20", "accepted": False, "before_metrics": {"round_count": 1}}
        self.assertEqual(self.observe(at("15:10"), rounds, [row])[0]["status"], "ok")
        self.assertIn("close_proposal_count", self.observe(at("15:10"), rounds)[0]["issues"])
        duplicate = self.observe(at("15:10"), rounds, [row, row])[0]
        self.assertIn("close_proposal_count", duplicate["issues"])
        active = self.observe(at("15:10"), rounds, [{**row, "accepted": True}])[0]
        self.assertIn("close_proposal_active", active["issues"])
        partial = self.observe(at("15:10"), rounds, [{**row, "before_metrics": {"round_count": 0}}])[0]
        self.assertIn("close_proposal_round_count", partial["issues"])


if __name__ == "__main__":
    unittest.main()
