# -*- coding: utf-8 -*-
"""W0 队列合同：状态闭集、去重、优先级、租约 fencing、续跑位置、阶段计时。"""
import datetime as dt
import tempfile
import unittest

import agent_task_queue as AQ

BASE = dt.datetime(2026, 10, 26, 1, 0, 0, tzinfo=dt.timezone.utc)


def at(minutes=0.0):
    return BASE + dt.timedelta(minutes=minutes)


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/agent_tasks.sqlite3"

    def tearDown(self):
        self._folder.cleanup()

    def enqueue(self, conn, **overrides):
        kwargs = dict(
            task_type="audit_selection_rejections",
            goal="解释为什么稳健波段的候选被软评分拒绝",
            input_fingerprint="fp-1",
            strategy_id="trend_pullback",
            event_version="v1",
            now=at(0),
        )
        kwargs.update(overrides)
        return AQ.enqueue(conn, **kwargs)

    def claimed(self, conn, minutes=0.0, **overrides):
        task, _ = self.enqueue(conn, **overrides)
        # 租约给足 15 分钟：这些用例的时钟只走几分钟，写入必须落在租约内。
        # 到期边界另有 test_a_write_after_expiry_is_refused_even_before_reclaim 守着。
        return AQ.claim(conn, worker_id="w1", lease_seconds=900, now=at(minutes))


class DedupeTests(QueueTestCase):
    def test_same_event_hangs_exactly_one_active_task(self):
        with AQ.connect(self.path) as conn:
            first, created = self.enqueue(conn)
            self.assertTrue(created)
            second, created_again = self.enqueue(conn)
            self.assertFalse(created_again)
            self.assertEqual(first["task_id"], second["task_id"])
            self.assertEqual(AQ.queue_report(conn)["active_total"], 1)

    def test_dedupe_dimensions_are_independent(self):
        with AQ.connect(self.path) as conn:
            self.enqueue(conn)
            for override in ({"event_version": "v2"}, {"input_fingerprint": "fp-2"},
                             {"task_type": "audit_entry_policy"},
                             {"strategy_id": "tq_breakout"}):
                _, created = self.enqueue(conn, **override)
                self.assertTrue(created, override)
            self.assertEqual(AQ.queue_report(conn)["active_total"], 5)

    def test_terminal_task_does_not_block_the_next_event(self):
        with AQ.connect(self.path) as conn:
            task = self.claimed(conn)
            AQ.start(conn, task["task_id"], task["lease_token"], now=at(0))
            AQ.finish(conn, task["task_id"], task["lease_token"],
                      status=AQ.STATUS_SUCCEEDED, now=at(1))
            _, created = self.enqueue(conn, now=at(2))
            self.assertTrue(created)

    def test_waiting_task_still_holds_the_dedupe_key(self):
        with AQ.connect(self.path) as conn:
            task = self.claimed(conn)
            AQ.start(conn, task["task_id"], task["lease_token"], now=at(0))
            AQ.awaiting(conn, task["task_id"], task["lease_token"],
                        status=AQ.STATUS_WAITING_DATA, reason="no_label_yet", now=at(0))
            again, created = self.enqueue(conn, now=at(5))
            self.assertFalse(created)
            self.assertEqual(again["task_id"], task["task_id"])
            self.assertEqual(again["status"], AQ.STATUS_WAITING_DATA)

    def test_missing_input_fingerprint_is_rejected(self):
        with AQ.connect(self.path) as conn:
            with self.assertRaises(AQ.TaskQueueError):
                self.enqueue(conn, input_fingerprint="")


class ClaimTests(QueueTestCase):
    def test_priority_order_and_queue_wait(self):
        with AQ.connect(self.path) as conn:
            self.enqueue(conn, task_type="low", priority=AQ.PRIORITY_LOW,
                         event_version="low", now=at(0))
            self.enqueue(conn, task_type="urgent", priority=AQ.PRIORITY_CRITICAL,
                         event_version="urgent", now=at(0))
            first = AQ.claim(conn, worker_id="w1", now=at(5))
            self.assertEqual(first["task_type"], "urgent")
            self.assertAlmostEqual(first["queue_wait_ms"], 5 * 60 * 1000, delta=1)
            second = AQ.claim(conn, worker_id="w2", now=at(5))
            self.assertEqual(second["task_type"], "low")
            self.assertIsNone(AQ.claim(conn, worker_id="w3", now=at(5)))

    def test_available_at_gates_claim(self):
        with AQ.connect(self.path) as conn:
            self.enqueue(conn, available_at=at(10))
            self.assertIsNone(AQ.claim(conn, worker_id="w1", now=at(9)))
            self.assertIsNotNone(AQ.claim(conn, worker_id="w1", now=at(10)))

    def test_claim_issues_new_fencing_generation_each_time(self):
        with AQ.connect(self.path) as conn:
            task, _ = self.enqueue(conn)
            first = AQ.claim(conn, worker_id="w1", lease_seconds=900, now=at(0))
            AQ.start(conn, first["task_id"], first["lease_token"], now=at(0))
            AQ.defer(conn, first["task_id"], first["lease_token"], reason="provider_timeout",
                     retry_in_seconds=5, now=at(0))
            second = AQ.claim(conn, worker_id="w2", lease_seconds=900, now=at(1))
            self.assertEqual(second["task_id"], task["task_id"])
            self.assertNotEqual(first["lease_token"], second["lease_token"])
            self.assertGreater(second["fencing_counter"], first["fencing_counter"])
            self.assertEqual(second["attempts"], 2)


class LeaseAndRestartTests(QueueTestCase):
    def test_expired_lease_is_reclaimed_and_old_writer_is_fenced(self):
        with AQ.connect(self.path) as conn:
            task, _ = self.enqueue(conn)
            task = AQ.claim(conn, worker_id="w1", lease_seconds=60, now=at(0))
            AQ.start(conn, task["task_id"], task["lease_token"], now=at(0))
            reclaimed = AQ.reclaim_expired(conn, now=at(5))
            self.assertEqual([row["status"] for row in reclaimed], [AQ.STATUS_DEFERRED])
            self.assertEqual(reclaimed[0]["blocking_reason"], "lease_expired")
            with self.assertRaises(AQ.StaleLease):
                AQ.checkpoint(conn, task["task_id"], task["lease_token"],
                              step=1, now=at(5))

    def test_resume_reads_the_committed_step_and_checkpoint(self):
        with AQ.connect(self.path) as conn:
            task, _ = self.enqueue(conn)
            # 这个用例要的是"租约过期后被回收"，所以刻意给短租约。
            first = AQ.claim(conn, worker_id="w1", lease_seconds=60, now=at(0))
            AQ.start(conn, first["task_id"], first["lease_token"], now=at(0))
            AQ.checkpoint(conn, first["task_id"], first["lease_token"], step=2,
                          checkpoint={"cursor": "row-200"}, next_action="evaluate",
                          now=at(0.5))
            AQ.reclaim_expired(conn, now=at(10))
            second = AQ.claim(conn, worker_id="w2", lease_seconds=60, now=at(11))
            self.assertEqual(second["task_id"], task["task_id"])
            self.assertEqual(second["step"], 2)
            self.assertEqual(second["checkpoint"], {"cursor": "row-200"})
            self.assertEqual(second["next_action"], "evaluate")

    def test_reclaim_exhausts_attempts_into_a_definite_terminal_state(self):
        with AQ.connect(self.path) as conn:
            task, _ = self.enqueue(conn, max_attempts=1)
            AQ.claim(conn, worker_id="w1", lease_seconds=60, now=at(0))
            reclaimed = AQ.reclaim_expired(conn, now=at(5))
            self.assertEqual(reclaimed[0]["status"], AQ.STATUS_FAILED)
            self.assertEqual(reclaimed[0]["blocking_reason"], "lease_expired_retry_exhausted")
            self.assertIsNone(AQ.claim(conn, worker_id="w2", now=at(6)))
            again, created = self.enqueue(conn, now=at(7))
            self.assertTrue(created)
            self.assertNotEqual(again["task_id"], task["task_id"])

    def test_checkpoint_cannot_rewind_or_exceed_the_step_budget(self):
        with AQ.connect(self.path) as conn:
            task, _ = self.enqueue(conn, max_steps=3)
            claimed = AQ.claim(conn, worker_id="w1", lease_seconds=900, now=at(0))
            AQ.start(conn, claimed["task_id"], claimed["lease_token"], now=at(0))
            AQ.checkpoint(conn, claimed["task_id"], claimed["lease_token"], step=2, now=at(1))
            with self.assertRaises(AQ.InvalidTransition):
                AQ.checkpoint(conn, claimed["task_id"], claimed["lease_token"],
                              step=1, now=at(2))
            with self.assertRaises(AQ.TaskQueueError):
                AQ.checkpoint(conn, claimed["task_id"], claimed["lease_token"],
                              step=4, now=at(2))
            self.assertEqual(AQ.get(conn, task["task_id"])["step"], 2)


class StateMachineTests(QueueTestCase):
    def test_unknown_status_is_rejected(self):
        with AQ.connect(self.path) as conn:
            task = self.claimed(conn)
            AQ.start(conn, task["task_id"], task["lease_token"], now=at(0))
            with self.assertRaises(AQ.UnknownStatus):
                AQ.finish(conn, task["task_id"], task["lease_token"],
                          status="probably_fine", now=at(1))

    def test_finish_requires_a_terminal_status(self):
        with AQ.connect(self.path) as conn:
            task = self.claimed(conn)
            AQ.start(conn, task["task_id"], task["lease_token"], now=at(0))
            with self.assertRaises(AQ.InvalidTransition):
                AQ.finish(conn, task["task_id"], task["lease_token"],
                          status=AQ.STATUS_RUNNING, now=at(1))

    def test_claimed_task_must_start_before_it_can_succeed(self):
        with AQ.connect(self.path) as conn:
            task = self.claimed(conn)
            with self.assertRaises(AQ.InvalidTransition):
                AQ.finish(conn, task["task_id"], task["lease_token"],
                          status=AQ.STATUS_SUCCEEDED, now=at(1))

    def test_terminal_status_is_final(self):
        with AQ.connect(self.path) as conn:
            task = self.claimed(conn)
            AQ.start(conn, task["task_id"], task["lease_token"], now=at(0))
            AQ.finish(conn, task["task_id"], task["lease_token"],
                      status=AQ.STATUS_REJECTED, reason="no_net_edge", now=at(1))
            for target in (AQ.STATUS_RUNNING, AQ.STATUS_DEFERRED, AQ.STATUS_QUEUED):
                with self.assertRaises((AQ.StaleLease, AQ.InvalidTransition)):
                    AQ.finish(conn, task["task_id"], task["lease_token"],
                              status=target, now=at(2))

    def test_defer_exhausts_attempts_instead_of_leaving_it_pending(self):
        with AQ.connect(self.path) as conn:
            task = self.claimed(conn, max_attempts=1)
            AQ.start(conn, task["task_id"], task["lease_token"], now=at(0))
            updated = AQ.defer(conn, task["task_id"], task["lease_token"],
                               reason="provider_timeout", retry_in_seconds=30, now=at(1))
            self.assertEqual(updated["status"], AQ.STATUS_FAILED)
            self.assertIn("retry_exhausted", updated["blocking_reason"])

    def test_waiting_can_be_woken_by_the_arriving_event(self):
        with AQ.connect(self.path) as conn:
            task = self.claimed(conn)
            AQ.start(conn, task["task_id"], task["lease_token"], now=at(0))
            AQ.awaiting(conn, task["task_id"], task["lease_token"],
                        status=AQ.STATUS_WAITING_AUTHORIZATION, reason="capability_missing",
                        now=at(1))
            self.assertIsNone(AQ.claim(conn, worker_id="w2", now=at(2)))
            woken = AQ.wake(conn, task["task_id"], reason="authorization_granted", now=at(3))
            self.assertEqual(woken["status"], AQ.STATUS_QUEUED)
            self.assertIsNotNone(AQ.claim(conn, worker_id="w2", now=at(3)))

    def test_transition_ledger_is_append_only(self):
        with AQ.connect(self.path) as conn:
            task = self.claimed(conn)
            AQ.start(conn, task["task_id"], task["lease_token"], now=at(0))
            AQ.finish(conn, task["task_id"], task["lease_token"],
                      status=AQ.STATUS_SUCCEEDED, now=at(1))
            trail = [(row["from_status"], row["to_status"])
                     for row in AQ.events(conn, task["task_id"])]
            self.assertEqual(trail, [
                (None, AQ.STATUS_QUEUED),
                (AQ.STATUS_QUEUED, AQ.STATUS_CLAIMED),
                (AQ.STATUS_CLAIMED, AQ.STATUS_RUNNING),
                (AQ.STATUS_RUNNING, AQ.STATUS_SUCCEEDED),
            ])


class CapacityTests(QueueTestCase):
    def test_capacity_trim_drops_low_priority_first_and_keeps_critical(self):
        with AQ.connect(self.path) as conn:
            critical, _ = self.enqueue(conn, task_type="revoke", priority=AQ.PRIORITY_CRITICAL,
                                       event_version="revoke", now=at(0))
            self.enqueue(conn, task_type="low", priority=AQ.PRIORITY_LOW,
                         event_version="low", now=at(1))
            trimmed = AQ.trim_queue(conn, capacity=0, now=at(2))
            self.assertEqual(len(trimmed), 1)
            self.assertEqual(AQ.get(conn, critical["task_id"])["status"], AQ.STATUS_QUEUED)
            self.assertEqual(AQ.queue_report(conn)["active_total"], 1)

    def test_blocking_reasons_are_visible_in_the_queue_report(self):
        with AQ.connect(self.path) as conn:
            task = self.claimed(conn)
            AQ.start(conn, task["task_id"], task["lease_token"], now=at(0))
            gone = AQ.finish(conn, task["task_id"], task["lease_token"],
                             status=AQ.STATUS_FAILED, reason="provider_circuit_open", now=at(1))
            self.assertEqual(gone["status"], AQ.STATUS_FAILED)
            deferred = self.claimed(conn, minutes=2, event_version="v2")
            AQ.start(conn, deferred["task_id"], deferred["lease_token"], now=at(2))
            AQ.defer(conn, deferred["task_id"], deferred["lease_token"],
                     reason="provider_timeout", retry_in_seconds=60, now=at(3))
            report = AQ.queue_report(conn)
            self.assertEqual(report["active_by_status"], {AQ.STATUS_DEFERRED: 1})
            self.assertEqual(report["blocking_reasons"], {"provider_timeout": 1})
            self.assertEqual(report["terminal_by_status"], {AQ.STATUS_FAILED: 1})


class StageTimingTests(QueueTestCase):
    def test_stage_report_points_at_the_real_longest_wait(self):
        with AQ.connect(self.path) as conn:
            first = self.claimed(conn)
            AQ.record_stages(conn, first["task_id"], 1,
                             {AQ.STAGE_SNAPSHOT_READ: 240_000.0,
                              AQ.STAGE_PROVIDER_WAIT: 900.0}, now=at(1))
            second = self.claimed(conn, minutes=3, event_version="v2")
            AQ.record_stages(conn, second["task_id"], 1,
                             {AQ.STAGE_SNAPSHOT_READ: 260_000.0,
                              AQ.STAGE_PROVIDER_WAIT: 1_100.0}, now=at(4))
            report = AQ.stage_report(conn)
            self.assertEqual(report["longest_waiting_stage"]["stage"], AQ.STAGE_SNAPSHOT_READ)
            self.assertEqual(report["stages"][AQ.STAGE_PROVIDER_WAIT]["samples"], 2)
            self.assertGreater(report["stages"][AQ.STAGE_SNAPSHOT_READ]["p95_ms"],
                               report["stages"][AQ.STAGE_SNAPSHOT_READ]["p50_ms"])

    def test_queue_wait_is_recorded_by_the_claim_itself(self):
        with AQ.connect(self.path) as conn:
            self.claimed(conn, minutes=7)
            stages = AQ.stage_report(conn)["stages"]
            self.assertAlmostEqual(stages[AQ.STAGE_QUEUE_WAIT]["max_ms"], 7 * 60 * 1000, delta=1)

    def test_unknown_stage_is_rejected(self):
        with AQ.connect(self.path) as conn:
            task = self.claimed(conn)
            with self.assertRaises(AQ.UnknownStage):
                AQ.record_stages(conn, task["task_id"], 1, {"vibes": 1.0}, now=at(1))

    def test_timer_accumulates_only_the_declared_vocabulary(self):
        timer = AQ.StageTimer()
        with timer.stage(AQ.STAGE_PROVIDER_WAIT):
            pass
        with timer.stage(AQ.STAGE_PROVIDER_WAIT):
            pass
        self.assertEqual(list(timer.totals), [AQ.STAGE_PROVIDER_WAIT])
        self.assertGreaterEqual(timer.totals[AQ.STAGE_PROVIDER_WAIT], 0.0)
        with self.assertRaises(AQ.UnknownStage):
            with timer.stage("thinking"):
                pass


if __name__ == "__main__":
    unittest.main()
