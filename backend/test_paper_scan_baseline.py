"""Natural scan summaries must not mix degraded, failed or unidentified runs."""
import json
import unittest

import paper_scan_baseline as B


def line(attempt, elapsed=90, **identity):
    return json.dumps({"event": "paper_slot_timing", "status": identity.pop("status", "completed"),
                       "timing": {"slot": "intraday", "scope": "finished", "attempt_id": attempt,
                                  "runtime_commit": "abc123", "started_at": "2026-10-20T09:33:00+08:00",
                                  "elapsed_seconds": elapsed,
                                  "identity": {"market_date": "2026-10-20", "snapshot_source": "eastmoney",
                                               "snapshot_price_only": False, "scan_ready": True, **identity},
                                  "stages": {"outer": {"seconds": elapsed}, "inner": {"seconds": elapsed / 2}}}})


class BaselineTests(unittest.TestCase):
    def test_groups_do_not_hide_failed_degraded_or_unknown_runs(self):
        report = B.summarize([line("a"), line("b", status="failed"),
                              line("c", snapshot_source="tencent", snapshot_price_only=True),
                              line("d", scan_ready=False), line("e", snapshot_source=None)])
        self.assertEqual(len(report["groups"]), 5)
        self.assertEqual(sum(g["total"]["samples"] for g in report["groups"]), 5)
        self.assertTrue(all(g["p95_is_provisional"] for g in report["groups"]))

    def test_percentile_dedup_exclusion_and_date_filter(self):
        report = B.summarize([line("a", 10), line("b", 20), line("b", 20), line("cold", 500),
                              line("other-day", market_date="2026-10-19"), "{paper_slot_timing bad}"],
                             market_date="2026-10-20", excluded_attempts={"cold"})
        group = report["groups"][0]
        self.assertEqual(group["total"], {"samples": 2, "median_seconds": 15, "p95_seconds": 20, "max_seconds": 20})
        self.assertEqual(group["stages_inclusive"]["outer"]["median_seconds"], 15)
        self.assertEqual(group["stages_inclusive"]["inner"]["median_seconds"], 7.5)
        self.assertEqual(report["counts"]["duplicates"], 1)
        self.assertEqual(report["counts"]["excluded"], 1)
        self.assertEqual(report["counts"]["malformed"], 1)
        self.assertFalse(group["covers_scheduled_minutes"])

    def test_old_log_is_unknown_and_nonfinite_duration_is_rejected(self):
        old = json.loads(line("old"))
        for key in ("runtime_commit", "started_at"):
            del old["timing"][key]
        old["timing"]["identity"] = {}
        group = B.summarize([json.dumps(old)])["groups"][0]
        self.assertEqual(group["runtime_commit"], "unknown")
        self.assertEqual(group["observed_start_minutes"], [])
        self.assertEqual(len(B.summarize([line("bad", float("nan"))])["groups"]), 0)

    def test_early_return_does_not_count_as_a_fast_full_scan(self):
        full = json.loads(line("full", 90))
        full["timing"]["stages"] = {"risk_pass": {"seconds": 20}, "candidate_bootstrap": {"seconds": 40}}
        short = json.loads(line("short", 2))
        short["timing"]["stages"] = {}
        groups = B.summarize([json.dumps(full), json.dumps(short)])["groups"]
        self.assertEqual({g["scan_path"] for g in groups}, {"risk_and_candidate", "no_scan_stages"})

    def test_coverage_uses_declared_cron_and_utc_is_converted(self):
        self.assertIn("09:30", B.expected_minutes())
        self.assertIn("13:54", B.expected_minutes())
        self.assertNotIn("13:57", B.expected_minutes())
        rows = []
        for minute in B.expected_minutes():
            value = json.loads(line(minute))
            value["timing"]["started_at"] = "2026-10-20T" + minute + ":05+08:00"
            rows.append(json.dumps(value))
        self.assertTrue(B.summarize(rows)["groups"][0]["covers_scheduled_minutes"])
        utc = json.loads(line("utc"))
        utc["timing"]["started_at"] = "2026-10-20T01:33:00+00:00"
        self.assertEqual(B.summarize([json.dumps(utc)])["groups"][0]["observed_start_minutes"], ["09:33"])
