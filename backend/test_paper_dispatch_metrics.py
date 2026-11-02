# -*- coding: utf-8 -*-
"""PR-11 调度观测的回归：只验证聚合口径，不触碰调度行为。"""
import json
import tempfile
import unittest
from pathlib import Path

import paper_dispatch_metrics as PDM


def _timing(slot, started_at, *, status="ok", wait=None, stages=None):
    timing = {"started_at": started_at, "stages": stages or {}}
    if wait is not None:
        timing["host_paper_lock_wait_seconds"] = wait
    return json.dumps({"event": "paper_slot_timing", "status": status, "timing": timing,
                       "slot": slot}, ensure_ascii=False)


def _lock(status, wait=None):
    payload = {"event": "paper_host_lock", "status": status}
    if wait is not None:
        payload["wait_seconds"] = wait
    return json.dumps(payload)


class EventParsingTests(unittest.TestCase):
    def test_bad_lines_are_skipped_without_raising(self):
        lines = [
            "",
            "   ",
            "cron started /opt/astock-codex/deploy/run_paper_slot.sh",
            "{not json}",
            "[1,2,3]",
            '{"event": "paper_host_lock", "status": "not_acquired", "wait_seconds": 60}',
        ]
        events = list(PDM.iter_events(lines))
        self.assertEqual(1, len(events))
        self.assertEqual("paper_host_lock", events[0]["event"])

    def test_events_without_event_key_are_ignored(self):
        events = list(PDM.iter_events(['{"status": "ok"}', '{"event": 42}']))
        self.assertEqual([], events)


class SkipAndQueueTests(unittest.TestCase):
    def test_skips_count_both_reasons_separately(self):
        events = list(PDM.iter_events([
            _lock("not_acquired", 60),
            _lock("not_acquired", 60),
            _lock(PDM.YIELDED),
            _timing("intraday", "2026-11-02T09:33:00+08:00"),
        ]))
        summary = PDM.summarize(events)
        self.assertEqual(2, summary["skips"][PDM.NOT_ACQUIRED])
        self.assertEqual(1, summary["skips"][PDM.YIELDED])
        self.assertEqual(3, summary["ticks"]["skipped"])
        self.assertEqual(1, summary["ticks"]["started"])

    def test_queue_age_combines_lock_events_and_timing(self):
        events = list(PDM.iter_events([
            _lock("not_acquired", 60),
            _timing("intraday", "2026-11-02T09:33:00+08:00", wait=0),
            _timing("intraday", "2026-11-02T09:36:00+08:00", wait=12),
        ]))
        queue = PDM.summarize(events)["queue_age_seconds"]
        self.assertEqual(3, queue["samples"])
        self.assertEqual(60.0, queue["max"])
        self.assertIsNotNone(queue["p50"])

    def test_negative_wait_is_not_counted(self):
        events = list(PDM.iter_events([_lock("not_acquired", -5)]))
        self.assertEqual(0, PDM.summarize(events)["queue_age_seconds"]["samples"])


class IntervalTests(unittest.TestCase):
    def test_start_to_start_uses_started_at_and_counts_breaches(self):
        events = list(PDM.iter_events([
            _timing("risk", "2026-11-02T09:30:00+08:00"),
            _timing("risk", "2026-11-02T09:33:00+08:00"),
            _timing("risk", "2026-11-02T09:40:00+08:00"),
        ]))
        risk = PDM.summarize(events)["start_to_start_seconds"]["risk"]
        self.assertEqual(2, risk["samples"])
        self.assertEqual(180.0, risk["p50_seconds"])
        self.assertEqual(420.0, risk["max_seconds"])
        self.assertEqual(1, risk["breaches"])
        self.assertEqual(180.0, risk["target_seconds"])

    def test_intervals_are_tracked_per_slot(self):
        events = list(PDM.iter_events([
            _timing("risk", "2026-11-02T09:30:00+08:00"),
            _timing("intraday", "2026-11-02T09:31:00+08:00"),
            _timing("risk", "2026-11-02T09:33:00+08:00"),
        ]))
        summary = PDM.summarize(events)["start_to_start_seconds"]
        self.assertEqual(1, summary["risk"]["samples"])
        self.assertNotIn("intraday", summary)

    def test_custom_target_changes_breach_count(self):
        events = list(PDM.iter_events([
            _timing("risk", "2026-11-02T09:30:00+08:00"),
            _timing("risk", "2026-11-02T09:34:00+08:00"),
        ]))
        summary = PDM.summarize(events, target_interval_seconds=300)
        self.assertEqual(0, summary["start_to_start_seconds"]["risk"]["breaches"])


class RiskScanTests(unittest.TestCase):
    def test_risk_slot_event_wins(self):
        events = list(PDM.iter_events([
            _timing("risk", "2026-11-02T09:33:00+08:00"),
        ]))
        last = PDM.summarize(events)["last_successful_risk_scan"]
        self.assertEqual("risk_slot", last["source"])
        self.assertTrue(last["at"].startswith("2026-11-02T09:33"))

    def test_intraday_risk_phase_is_the_documented_fallback(self):
        stages = {"risk.alt_evidence": {"seconds": 1.0, "errors": 0}}
        events = list(PDM.iter_events([
            _timing("intraday", "2026-11-02T09:36:00+08:00", stages=stages),
        ]))
        last = PDM.summarize(events)["last_successful_risk_scan"]
        self.assertEqual("intraday_risk_phase", last["source"])

    def test_failed_intraday_is_not_a_successful_risk_scan(self):
        stages = {"risk.alt_evidence": {"seconds": 1.0, "errors": 1}}
        events = list(PDM.iter_events([
            _timing("intraday", "2026-11-02T09:36:00+08:00", status="failed", stages=stages),
        ]))
        self.assertIsNone(PDM.summarize(events)["last_successful_risk_scan"]["at"])

    def test_intraday_without_risk_stage_is_not_a_risk_scan(self):
        stages = {"candidate.strategy": {"seconds": 1.0, "errors": 0}}
        events = list(PDM.iter_events([
            _timing("intraday", "2026-11-02T09:36:00+08:00", stages=stages),
        ]))
        self.assertIsNone(PDM.summarize(events)["last_successful_risk_scan"]["at"])


class FileTests(unittest.TestCase):
    def test_missing_file_reports_unavailable_instead_of_raising(self):
        result = PDM.summarize_file(Path(tempfile.gettempdir()) / "no-such-scheduler.log")
        self.assertFalse(result["available"])

    def test_file_summary_keeps_the_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scheduler.log"
            path.write_text("\n".join([
                _timing("risk", "2026-11-02T09:30:00+08:00"),
                _timing("risk", "2026-11-02T09:33:00+08:00"),
            ]) + "\n", encoding="utf-8")
            result = PDM.summarize_file(path)
        self.assertTrue(result["available"])
        self.assertEqual(2, result["lines_scanned"])
        self.assertEqual(1, result["start_to_start_seconds"]["risk"]["samples"])


if __name__ == "__main__":
    unittest.main()
