# -*- coding: utf-8 -*-
"""W0 执行器合同：续跑、失败不装成功、预算耗尽、租约丢失、资源准入。"""
import datetime as dt
import io
import os
import tempfile
import unittest
from unittest import mock

import agent_task_queue as AQ
import agent_task_runner as AR

BASE = dt.datetime(2026, 10, 26, 1, 0, 0, tzinfo=dt.timezone.utc)


def at(minutes=0.0):
    return BASE + dt.timedelta(minutes=minutes)


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/agent_tasks.sqlite3"
        self.clock = lambda: at(0)
        self.handlers = {}

    def tearDown(self):
        self._folder.cleanup()

    def register(self, task_type, handler, *, heavy=False):
        self.handlers[task_type] = AR.HandlerSpec(handler=handler, heavy=heavy)

    def enqueue(self, conn, **overrides):
        kwargs = dict(task_type="audit_selection_rejections", goal="复核被拒候选",
                      input_fingerprint="fp-1", strategy_id="trend_pullback",
                      now=self.clock())
        kwargs.update(overrides)
        return AQ.enqueue(conn, **kwargs)

    def run_once(self, conn):
        return AR.run_once(conn, worker_id="w1", handlers=self.handlers, clock=self.clock)


class HappyPathTests(RunnerTestCase):
    def test_success_persists_plan_checkpoint_and_stage_timings(self):
        seen = {}

        def handler(task, ctx):
            seen["resume_step"] = ctx.resume_step
            seen["resume_checkpoint"] = ctx.resume_checkpoint
            with ctx.stage(AQ.STAGE_PROVIDER_WAIT):
                pass
            return AR.TaskOutcome(
                status=AQ.STATUS_SUCCEEDED, result={"findings": 3},
                plan={"hypothesis": "软评分门槛过紧"}, step=1,
                checkpoint={"cursor": "row-50"}, next_action="evaluate",
                budget_used={"tool_steps": 1},
            )

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn)
            outcome = self.run_once(conn)
            self.assertEqual(outcome["status"], AQ.STATUS_SUCCEEDED)
            stored = AQ.get(conn, task["task_id"])
            self.assertEqual(stored["result"], {"findings": 3})
            self.assertEqual(stored["plan"], {"hypothesis": "软评分门槛过紧"})
            self.assertEqual(stored["checkpoint"], {"cursor": "row-50"})
            self.assertEqual(stored["step"], 1)
            self.assertEqual(stored["budget_used"], {"tool_steps": 1})
            self.assertEqual(seen["resume_step"], 0)
            self.assertEqual(seen["resume_checkpoint"], {})
            stages = AQ.stage_report(conn)["stages"]
            self.assertIn(AQ.STAGE_PROVIDER_WAIT, stages)
            self.assertIn(AQ.STAGE_QUEUE_WAIT, stages)
            self.assertAlmostEqual(stored["timing"][AQ.STAGE_PROVIDER_WAIT],
                                   stages[AQ.STAGE_PROVIDER_WAIT]["max_ms"], delta=0.5)

    def test_empty_queue_returns_none(self):
        with AQ.connect(self.path) as conn:
            self.assertIsNone(self.run_once(conn))


class FailureTests(RunnerTestCase):
    def test_exception_becomes_a_failed_task_with_a_reason(self):
        def handler(task, ctx):
            raise RuntimeError("provider exploded")

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn)
            outcome = self.run_once(conn)
            self.assertEqual(outcome["status"], AQ.STATUS_FAILED)
            stored = AQ.get(conn, task["task_id"])
            self.assertEqual(stored["status"], AQ.STATUS_FAILED)
            self.assertIn("provider exploded", stored["blocking_reason"])
            self.assertIsNotNone(stored["finished_at"])

    def test_partial_work_is_never_recorded_as_success(self):
        def handler(task, ctx):
            ctx.timer.add(AQ.STAGE_SNAPSHOT_READ, 1200.0)
            raise RuntimeError("evaluator crashed after reading the snapshot")

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn)
            self.run_once(conn)
            stored = AQ.get(conn, task["task_id"])
            self.assertEqual(stored["status"], AQ.STATUS_FAILED)
            self.assertNotIn("succeeded", AQ.queue_report(conn)["terminal_by_status"])
            self.assertEqual(stored["timing"][AQ.STAGE_SNAPSHOT_READ], 1200.0)

    def test_unregistered_task_type_fails_closed(self):
        with AQ.connect(self.path) as conn:
            task, _ = self.enqueue(conn, task_type="nobody_handles_this")
            outcome = self.run_once(conn)
            self.assertEqual(outcome["status"], AQ.STATUS_FAILED)
            self.assertIn("nobody_handles_this", AQ.get(conn, task["task_id"])["blocking_reason"])

    def test_handler_returning_the_wrong_type_fails_closed(self):
        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", lambda task, ctx: {"status": "ok"})
            task, _ = self.enqueue(conn)
            self.run_once(conn)
            self.assertEqual(AQ.get(conn, task["task_id"])["status"], AQ.STATUS_FAILED)


class RetryAndWaitTests(RunnerTestCase):
    def test_retryable_error_defers_then_retries_after_the_backoff(self):
        calls = []

        def handler(task, ctx):
            calls.append(task["attempts"])
            if len(calls) == 1:
                raise AR.RetryableTaskError("provider_timeout", retry_in_seconds=60)
            return AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED)

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn)
            self.assertEqual(self.run_once(conn)["status"], AQ.STATUS_DEFERRED)
            self.assertIsNone(self.run_once(conn))
            self.clock = lambda: at(2)
            self.assertEqual(self.run_once(conn)["status"], AQ.STATUS_SUCCEEDED)
            self.assertEqual(calls, [1, 2])

    def test_waiting_is_not_counted_as_a_retry_and_needs_the_event(self):
        def handler(task, ctx):
            raise AR.WaitingTaskError(AQ.STATUS_WAITING_DATA, "label_not_matured")

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn)
            self.assertEqual(self.run_once(conn)["status"], AQ.STATUS_WAITING_DATA)
            stored = AQ.get(conn, task["task_id"])
            self.assertEqual(stored["attempts"], 1)
            self.assertEqual(stored["blocking_reason"], "label_not_matured")
            self.clock = lambda: at(30)
            self.assertIsNone(self.run_once(conn), "等待态不能被隐式重领")
            AQ.wake(conn, task["task_id"], reason="label_arrived", now=self.clock())
            self.assertEqual(self.run_once(conn)["status"], AQ.STATUS_WAITING_DATA)

    def test_waiting_with_a_wake_time_is_rechecked_automatically(self):
        def handler(task, ctx):
            raise AR.WaitingTaskError(AQ.STATUS_WAITING_LABEL, "next_close", wake_at=at(5))

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            self.enqueue(conn)
            self.run_once(conn)
            self.clock = lambda: at(4)
            self.assertIsNone(self.run_once(conn))
            self.clock = lambda: at(6)
            self.assertEqual(self.run_once(conn)["status"], AQ.STATUS_WAITING_LABEL)


class ResumeTests(RunnerTestCase):
    def test_restart_resumes_from_the_committed_step(self):
        observed = []

        def handler(task, ctx):
            observed.append((ctx.resume_step, dict(ctx.resume_checkpoint)))
            return AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED, step=3,
                                  checkpoint={"cursor": "row-400"})

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn)
            # 上一个 worker 跑到第 2 步就没了：任务停在 running，租约随后过期
            claimed = AQ.claim(conn, worker_id="ghost", lease_seconds=60, now=at(0))
            AQ.start(conn, claimed["task_id"], claimed["lease_token"], now=at(0))
            AQ.checkpoint(conn, claimed["task_id"], claimed["lease_token"], step=2,
                          checkpoint={"cursor": "row-200"}, now=at(0))
            self.clock = lambda: at(30)
            AR.maintenance(conn, clock=self.clock)
            self.assertEqual(self.run_once(conn)["status"], AQ.STATUS_SUCCEEDED)
            self.assertEqual(observed, [(2, {"cursor": "row-200"})])
            stored = AQ.get(conn, task["task_id"])
            self.assertEqual(stored["step"], 3)
            self.assertEqual(stored["status"], AQ.STATUS_SUCCEEDED)

    def test_lease_taken_away_mid_run_cannot_be_overwritten(self):
        def handler(task, ctx):
            ctx.heartbeat()
            # 另一个进程在**独立连接**上回收了这条任务的租约（真实接管场景）。
            with AQ.connect(self.path) as other:
                AQ.reclaim_expired(other, now=at(120))
            return AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED, result={"should": "not land"})

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn)
            outcome = self.run_once(conn)
            self.assertEqual(outcome["status"], "lease_lost")
            stored = AQ.get(conn, task["task_id"])
            self.assertIsNone(stored["result"])
            self.assertEqual(stored["status"], AQ.STATUS_DEFERRED)
            self.assertEqual(stored["blocking_reason"], "lease_expired")


class BudgetTests(RunnerTestCase):
    def test_step_beyond_the_tool_budget_fails_instead_of_running_forever(self):
        def handler(task, ctx):
            return AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED, step=99)

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn, max_steps=3)
            self.run_once(conn)
            stored = AQ.get(conn, task["task_id"])
            self.assertEqual(stored["status"], AQ.STATUS_FAILED)
            self.assertIn("max_steps", stored["blocking_reason"])


class AdmissionTests(RunnerTestCase):
    def test_heavy_task_defers_when_the_resource_lease_is_busy(self):
        self.register("heavy_replay",
                      lambda task, ctx: AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED),
                      heavy=True)
        blockers = mock.MagicMock()
        blockers.return_value.__enter__.return_value = {"allowed": False,
                                                        "reason": "heavy_job_busy"}
        blockers.return_value.__exit__.return_value = False
        with AQ.connect(self.path) as conn, \
                mock.patch.object(AR.resource_guard, "heavy_job_lease", blockers):
            task, _ = self.enqueue(conn, task_type="heavy_replay")
            outcome = self.run_once(conn)
            self.assertEqual(outcome["status"], AQ.STATUS_DEFERRED)
            stored = AQ.get(conn, task["task_id"])
            self.assertEqual(stored["blocking_reason"], "resource_heavy_job_busy")
            self.assertEqual(stored["step"], 0)

    def test_heavy_task_runs_under_an_allowed_lease(self):
        self.register("heavy_replay",
                      lambda task, ctx: AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED),
                      heavy=True)
        allowed = mock.MagicMock()
        allowed.return_value.__enter__.return_value = {"allowed": True,
                                                      "fencing_token": "fence-1"}
        allowed.return_value.__exit__.return_value = False
        with AQ.connect(self.path) as conn, \
                mock.patch.object(AR.resource_guard, "heavy_job_lease", allowed):
            self.enqueue(conn, task_type="heavy_replay")
            self.assertEqual(self.run_once(conn)["status"], AQ.STATUS_SUCCEEDED)
            self.assertIn("agent-task:heavy_replay", allowed.call_args.args[0])


class ConcurrencyTests(RunnerTestCase):
    """R4：处理器执行期间不得占住研究库写锁。"""

    def test_a_second_connection_can_queue_work_while_a_handler_runs(self):
        seen = {}

        def handler(task, ctx):
            with AQ.connect(self.path) as other:
                # 别的进程要能看到真实状态（running），而不是 queued。
                seen["status"] = AQ.get(other, task["task_id"])["status"]
                # 而且要能入队：长模型调用期间队列不能被锁死。
                enqueued, created = AQ.enqueue(
                    other, task_type="other_work", goal="并发入队",
                    input_fingerprint="fp-concurrent", now=at(0))
                seen["enqueued"] = created
                seen["queued_status"] = enqueued["status"]
            return AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED)

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            self.enqueue(conn)
            outcome = self.run_once(conn)
            self.assertEqual(outcome["status"], AQ.STATUS_SUCCEEDED)
        self.assertEqual(seen["status"], AQ.STATUS_RUNNING)
        self.assertTrue(seen["enqueued"])
        self.assertEqual(seen["queued_status"], AQ.STATUS_QUEUED)

    def test_a_second_connection_can_read_progress_while_a_handler_runs(self):
        def handler(task, ctx):
            with AQ.connect(self.path):
                snapshot = AR.status_snapshot(self.path)
                self.assertTrue(snapshot["initialized"])
                self.assertIn(AQ.STATUS_RUNNING,
                              snapshot["queue"]["active_by_status"])
            return AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED)

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            self.enqueue(conn)
            self.assertEqual(self.run_once(conn)["status"], AQ.STATUS_SUCCEEDED)


    def test_a_task_cancelled_during_the_handler_cannot_publish(self):
        """发布前要复核：任务在计算期间被取消，结果就不许落库。"""
        def handler(task, ctx):
            with AQ.connect(self.path) as other:
                AQ.cancel(other, task["task_id"], reason="authorization_revoked")
            return AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED,
                                  result={"should": "not land"},
                                  checkpoint={"cursor": "row-1"})

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn)
            outcome = self.run_once(conn)
            self.assertEqual(outcome["status"], "lease_lost")
            stored = AQ.get(conn, task["task_id"])
            self.assertEqual(stored["status"], AQ.STATUS_CANCELLED)
            self.assertIsNone(stored["result"])
            self.assertIsNone(stored["checkpoint"])


class WorkUnitAtomicityTests(RunnerTestCase):
    """R3：工作单元要么整块落地，要么整块消失。"""

    def test_a_failing_publish_rolls_back_the_whole_work_unit(self):
        """R3：publish 里的写入属于工作单元；它失败，写入必须整块消失。"""
        import replay_receipt_ledger as RL

        def publish(conn):
            RL.ensure_schema(conn)
            conn.execute(
                "INSERT INTO experiment_runs (experiment_id, target_identity,"
                " environment_hash, champion, challenger, rules, sessions, created_at)"
                " VALUES ('exp-partial','t','h','{}','{}','{}','[]','2026-10-26T00:00:00Z')")
            raise RuntimeError("publish died halfway")

        def handler(task, ctx):
            return AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED, publish=publish)

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn)
            outcome = self.run_once(conn)
            self.assertEqual(outcome["status"], AQ.STATUS_FAILED)
            stored = AQ.get(conn, task["task_id"])
            self.assertEqual(stored["status"], AQ.STATUS_FAILED)
            # 失败前的 publish 写入必须被回滚：库里不能留半条实验登记。
            self.assertIsNone(RL.get_run(conn, "exp-partial"))

    def test_publish_runs_inside_the_short_transaction_not_during_compute(self):
        """计算阶段不持写锁：publish 尚未执行的时刻，别的连接能正常写入。"""
        observed = {}

        def publish(conn):
            observed["published"] = True
            return {"marker": "published"}

        def handler(task, ctx):
            with AQ.connect(self.path) as other:
                AQ.enqueue(other, task_type="other_work", goal="并发入队",
                           input_fingerprint="fp-during-compute")
                observed["concurrent_write"] = True
            return AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED, publish=publish)

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn)
            outcome = self.run_once(conn)
            self.assertEqual(outcome["status"], AQ.STATUS_SUCCEEDED)
            self.assertTrue(observed["concurrent_write"])
            self.assertTrue(observed["published"])
            stored = AQ.get(conn, task["task_id"])
            self.assertEqual(stored["result"]["marker"], "published")

    def test_a_stale_lease_after_computation_rolls_the_work_unit_back(self):
        def handler(task, ctx):
            return AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED,
                                  checkpoint={"cursor": "row-9"},
                                  plan={"hypothesis": "never lands"})

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            task, _ = self.enqueue(conn)
            claimed = AQ.claim(conn, worker_id="thief", lease_seconds=1, now=at(0))
            AQ.commit(conn)
            # 让处理器拿到的任务在"计算完成"时已经过期：写入必须被拒绝且不落库。
            outcome = AR._execute(conn, claimed, AR.HandlerSpec(handler=handler),
                                  "w1", 1.0, lambda: at(5))
            self.assertEqual(outcome["status"], "lease_lost")
            stored = AQ.get(conn, task["task_id"])
            self.assertIsNone(stored["plan"])
            self.assertIsNone(stored["checkpoint"])


class BatchTests(RunnerTestCase):
    def test_batch_respects_the_task_limit_and_reclaims_stuck_leases(self):
        self.register("audit_selection_rejections",
                      lambda task, ctx: AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED))
        with AQ.connect(self.path) as conn:
            for version in ("v1", "v2", "v3"):
                self.enqueue(conn, event_version=version)
            stuck, _ = self.enqueue(conn, task_type="stuck_one", event_version="s1")
            AQ.claim(conn, worker_id="ghost", lease_seconds=60, now=at(0))
            self.clock = lambda: at(5)
            results = AR.run_batch(conn, max_tasks=2, handlers=self.handlers,
                                   worker_id="w1", clock=self.clock)
            self.assertEqual(len(results), 2)
            self.assertEqual(AQ.get(conn, stuck["task_id"])["status"], AQ.STATUS_QUEUED)

    def test_batch_capacity_trim_keeps_critical_work(self):
        self.register("revoke", lambda task, ctx: AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED))
        with AQ.connect(self.path) as conn:
            critical, _ = self.enqueue(conn, task_type="revoke", priority=0,
                                       event_version="r1")
            AR.maintenance(conn, capacity=0, clock=self.clock)
            self.assertEqual(AQ.get(conn, critical["task_id"])["status"], AQ.STATUS_QUEUED)


class CliTests(RunnerTestCase):
    def test_status_on_a_fresh_install_reads_without_creating_the_queue(self):
        snapshot = AR.status_snapshot(self.path)
        self.assertFalse(snapshot["initialized"])
        self.assertEqual(snapshot["queue"]["active_total"], 0)
        self.assertFalse(os.path.exists(self.path), "读路径不能顺手建库")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(AR.main(["status", "--path", self.path]), 0)
            self.assertIn("initialized", out.getvalue())

    def test_status_exposes_blocking_reasons_and_the_longest_wait(self):
        def handler(task, ctx):
            ctx.timer.add(AQ.STAGE_ARCHIVE_DECODE, 820_000.0)
            raise AR.RetryableTaskError("provider_timeout", retry_in_seconds=60)

        with AQ.connect(self.path) as conn:
            self.register("audit_selection_rejections", handler)
            self.enqueue(conn)
            self.run_once(conn)
        snapshot = AR.status_snapshot(self.path)
        self.assertTrue(snapshot["initialized"])
        self.assertEqual(snapshot["queue"]["blocking_reasons"], {"provider_timeout": 1})
        self.assertEqual(snapshot["stages"]["longest_waiting_stage"]["stage"],
                         AQ.STAGE_ARCHIVE_DECODE)
        self.assertEqual(snapshot["active_tasks"][0]["status"], AQ.STATUS_DEFERRED)

    def test_run_refuses_to_pretend_when_nothing_is_registered(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(AR.main(["run", "--path", self.path]), 2)
            self.assertIn("no_handlers", out.getvalue())
        self.assertFalse(os.path.exists(self.path))

    def test_run_exit_code_is_zero_only_when_every_task_succeeded(self):
        self.assertEqual(AR._exit_code([]), 0)
        self.assertEqual(AR._exit_code([{"status": AQ.STATUS_SUCCEEDED}]), 0)
        self.assertEqual(AR._exit_code([{"status": AQ.STATUS_SUCCEEDED},
                                        {"status": AQ.STATUS_DEFERRED}]), 1)


if __name__ == "__main__":
    unittest.main()
