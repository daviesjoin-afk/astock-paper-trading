# -*- coding: utf-8 -*-
"""研究链路耗时拆分的验证（方案 S2 的可测部分）。"""
import datetime as dt
import tempfile
import unittest

import agent_task_queue as AQ
import research_metrics as RM

BASE = dt.datetime(2026, 10, 26, 1, 0, 0, tzinfo=dt.timezone.utc)


def at(seconds):
    return BASE + dt.timedelta(seconds=seconds)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/research.sqlite3"

    def tearDown(self):
        self._folder.cleanup()

    def _task(self, conn, **overrides):
        kwargs = dict(task_type="research_question", goal="g", input_fingerprint="fp-1",
                      now=at(0))
        kwargs.update(overrides)
        task, _ = AQ.enqueue(conn, **kwargs)
        return task

    def test_the_four_windows_are_split_without_overlap(self):
        with AQ.connect(self.path) as conn:
            task = self._task(conn)
            claimed = AQ.claim(conn, worker_id="w1", lease_seconds=900, now=at(5))
            AQ.start(conn, claimed["task_id"], claimed["lease_token"], now=at(5))
            AQ.record_stages(conn, claimed["task_id"], 1,
                             {AQ.STAGE_PROVIDER_WAIT: 12_000.0,
                              AQ.STAGE_SNAPSHOT_READ: 2_000.0,
                              AQ.STAGE_EVALUATOR_CPU: 1_000.0}, now=at(20))
            AQ.finish(conn, claimed["task_id"], claimed["lease_token"],
                      status=AQ.STATUS_SUCCEEDED, now=at(30))
            breakdown = RM.task_breakdown(conn, task["task_id"])
            self.assertAlmostEqual(breakdown["queue_wait_s"], 5.0, places=3)
            self.assertAlmostEqual(breakdown["provider_wait_s"], 12.0, places=3)
            self.assertAlmostEqual(breakdown["compute_s"], 3.0, places=3)
            self.assertAlmostEqual(breakdown["evidence_wait_s"], 0.0, places=3)
            self.assertAlmostEqual(breakdown["wall_s"], 30.0, places=3)
            self.assertGreater(breakdown["unaccounted_s"], 0.0)

    def test_evidence_wait_is_measured_from_the_transition_ledger(self):
        with AQ.connect(self.path) as conn:
            task = self._task(conn)
            claimed = AQ.claim(conn, worker_id="w1", lease_seconds=900, now=at(1))
            AQ.start(conn, claimed["task_id"], claimed["lease_token"], now=at(1))
            AQ.awaiting(conn, claimed["task_id"], claimed["lease_token"],
                        status=AQ.STATUS_WAITING_DATA, reason="label_not_ready",
                        now=at(2))
            # 等待期间数据到达，任务被唤醒并完成。
            AQ.wake(conn, claimed["task_id"], reason="label_arrived", now=at(3602))
            claimed = AQ.claim(conn, worker_id="w2", lease_seconds=900, now=at(3602))
            AQ.start(conn, claimed["task_id"], claimed["lease_token"], now=at(3602))
            AQ.finish(conn, claimed["task_id"], claimed["lease_token"],
                      status=AQ.STATUS_SUCCEEDED, now=at(3604))
            breakdown = RM.task_breakdown(conn, task["task_id"])
            self.assertAlmostEqual(breakdown["evidence_wait_s"], 3600.0, places=3)

    def test_the_report_names_the_bottleneck_and_gives_an_actionable_hint(self):
        with AQ.connect(self.path) as conn:
            for index in range(3):
                task = self._task(conn, input_fingerprint=f"fp-{index}")
                claimed = AQ.claim(conn, worker_id="w1", lease_seconds=900,
                                   now=at(index))
                AQ.start(conn, claimed["task_id"], claimed["lease_token"], now=at(index))
                AQ.record_stages(conn, claimed["task_id"], 1,
                                 {AQ.STAGE_PROVIDER_WAIT: 20_000.0,
                                  AQ.STAGE_SNAPSHOT_READ: 500.0}, now=at(index))
                AQ.finish(conn, claimed["task_id"], claimed["lease_token"],
                          status=AQ.STATUS_SUCCEEDED, now=at(index + 30))
                self.assertIsNotNone(task)
            report = RM.latency_report(conn, task_type="research_question")
            self.assertEqual(report["samples"], 3)
            self.assertEqual(report["bottleneck"]["stage"], "provider_wait_s")
            self.assertIn("等模型", RM.speed_hint(report))
            self.assertGreater(report["stages"]["wall_s"]["p95_s"], 0)

    def test_waiting_on_the_market_is_not_reported_as_a_speed_problem(self):
        with AQ.connect(self.path) as conn:
            task = self._task(conn)
            claimed = AQ.claim(conn, worker_id="w1", lease_seconds=900, now=at(1))
            AQ.start(conn, claimed["task_id"], claimed["lease_token"], now=at(1))
            AQ.awaiting(conn, claimed["task_id"], claimed["lease_token"],
                        status=AQ.STATUS_WAITING_LABEL, reason="next_close", now=at(2))
            AQ.wake(conn, claimed["task_id"], reason="arrived", now=at(7202))
            claimed = AQ.claim(conn, worker_id="w2", lease_seconds=900, now=at(7202))
            AQ.start(conn, claimed["task_id"], claimed["lease_token"], now=at(7202))
            AQ.finish(conn, claimed["task_id"], claimed["lease_token"],
                      status=AQ.STATUS_SUCCEEDED, now=at(7203))
            report = RM.latency_report(conn)
            self.assertEqual(report["bottleneck"]["stage"], "evidence_wait_s")
            self.assertIn("工程加速无效", RM.speed_hint(report))
            self.assertIsNotNone(task)

    def test_an_empty_report_does_not_pretend_to_have_numbers(self):
        with AQ.connect(self.path) as conn:
            report = RM.latency_report(conn)
            self.assertEqual(report["samples"], 0)
            self.assertIsNone(report["bottleneck"])
            self.assertIn("没有已完成的样本", RM.speed_hint(report))

    def test_an_unknown_task_is_refused(self):
        with AQ.connect(self.path) as conn:
            with self.assertRaises(RM.MetricsError):
                RM.task_breakdown(conn, "nope")


if __name__ == "__main__":
    unittest.main()
