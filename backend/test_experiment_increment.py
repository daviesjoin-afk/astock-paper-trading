# -*- coding: utf-8 -*-
"""F3 持续增量的验证：原子回执、幂等重放、顺序约束、重启续跑、B 侧失败安全。"""
import tempfile
import unittest

import pandas as pd

import agent_task_queue as AQ
import agent_task_runner as AR
import experiment_increment as EI
import native_paired_replay as NPR
import replay_receipt_ledger as RL

TRADING_DAYS = ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09")
CAPITAL = 100_000.0
HANDLERS = {EI.TASK_TYPE_SESSION: AR.HandlerSpec(handler=EI._handle_session_task)}


def factor_table(bump=0.0):
    rows = [
        {"name": "甲", "industry": "行业A", "price": 10.0, "pct": 3.0 + bump,
         "amount": 5e7, "turnover": 3.0, "main_pct": 1.0, "super_net_raw": 1e6,
         "mom5_raw": 0.02, "mom20_raw": 0.03, "mom60_raw": 0.05, "mom_short": 0.02,
         "volsurge": 1.5, "vol_surge_raw": 2.0, "rsi14_raw": 55.0, "flow": 1.0,
         "sentiment": 0.0, "above_boll_mid": True, "boll_mid_breakout": True,
         "ma20": 9.5, "ma60": 9.0},
        {"name": "乙", "industry": "行业A", "price": 12.0, "pct": 2.0, "amount": 4e7,
         "turnover": 2.0, "main_pct": 0.8, "super_net_raw": 8e5, "mom5_raw": 0.01,
         "mom20_raw": 0.02, "mom60_raw": 0.04, "mom_short": 0.01, "volsurge": 1.2,
         "vol_surge_raw": 1.5, "rsi14_raw": 52.0, "flow": 0.9, "sentiment": 0.0,
         "above_boll_mid": True, "boll_mid_breakout": False, "ma20": 11.5, "ma60": 11.0},
    ]
    return pd.DataFrame(rows, index=["600001", "600002"])


def environment(days=TRADING_DAYS):
    sessions = []
    for index, day in enumerate(days):
        scale = 1.0 + 0.01 * index
        sessions.append(NPR.ReplaySession(
            session_date=day, decision_at=f"{day}T14:30:00+08:00",
            quotes=(NPR.ReplayQuote("600001", round(10.0 * scale, 2)),
                    NPR.ReplayQuote("600002", round(12.0 * scale, 2), prev_close=12.0)),
            factor_table=factor_table(), first_board_codes=("600001",)))
    return NPR.ReplayEnvironment(sessions=tuple(sessions))


def sides():
    return NPR.sides_from_manifest(
        "tq_breakout", target_kind="entry_policy", model_family="first_board_relay",
        variant_fields={"first_board_bonus": 0.0})


def rules():
    return NPR.ReplayRules(reference_capital=CAPITAL, max_positions=1,
                           hold_sessions=1, topn=1)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/research.sqlite3"

    def tearDown(self):
        self._folder.cleanup()

    def _pair(self, conn):
        champion, challenger = sides()
        paired = NPR.run_paired_session(
            session=environment().sessions[0], index=0,
            champion_state=NPR.SideState.initial(CAPITAL),
            challenger_state=NPR.SideState.initial(CAPITAL),
            champion=champion, challenger=challenger, rules=rules(),
            select=NPR.strategies_selector(), experiment_id="exp-1",
            target_identity="t", environment_hash=environment().environment_hash)
        return paired, champion, challenger

    def _publish(self, conn, paired):
        return RL.publish_pair(
            conn, champion=paired.receipts[0], challenger=paired.receipts[1],
            champion_step=paired.champion_step.projection(),
            challenger_step=paired.challenger_step.projection(),
            champion_state={"cash": 1.0}, challenger_state={"cash": 2.0})

    def test_the_pair_is_published_under_the_five_part_key(self):
        with AQ.connect(self.path) as conn:
            paired, _c, _x = self._pair(conn)
            stored, created = self._publish(conn, paired)
            self.assertTrue(created)
            self.assertEqual(len(stored), 2)
            keys = {(row["experiment_id"], row["target_identity"],
                     row["environment_hash"], row["transition_sequence"],
                     row["evaluator_version"]) for row in stored}
            self.assertEqual(len(keys), 2)
            self.assertEqual({row["side"] for row in stored}, {"champion", "challenger"})

    def test_republishing_the_same_pair_is_idempotent(self):
        with AQ.connect(self.path) as conn:
            paired, _c, _x = self._pair(conn)
            first, created = self._publish(conn, paired)
            second, created_again = self._publish(conn, paired)
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual([row["receipt_fingerprint"] for row in first],
                             [row["receipt_fingerprint"] for row in second])
            self.assertEqual(len(RL.receipts(conn, "exp-1")), 2)

    def test_content_drift_on_the_same_key_is_refused(self):
        with AQ.connect(self.path) as conn:
            paired, _c, _x = self._pair(conn)
            self._publish(conn, paired)
            drifted = NPR.Receipt(
                experiment_id=paired.receipts[0].experiment_id,
                target_identity=paired.receipts[0].target_identity,
                environment_hash=paired.receipts[0].environment_hash,
                transition_sequence=paired.receipts[0].transition_sequence,
                evaluator_version=paired.receipts[0].evaluator_version,
                side="champion", session_date=paired.receipts[0].session_date,
                receipt_fingerprint="0" * 64)
            with self.assertRaises(RL.ReceiptConflict):
                RL.publish_pair(conn, champion=drifted, challenger=paired.receipts[1],
                                champion_step={}, challenger_step={},
                                champion_state={}, challenger_state={})

    def test_a_failing_side_never_half_publishes(self):
        with AQ.connect(self.path) as conn:
            paired, _c, _x = self._pair(conn)
            RL.ensure_schema(conn)
            drifted = NPR.Receipt(
                experiment_id=paired.receipts[1].experiment_id,
                target_identity=paired.receipts[1].target_identity,
                environment_hash=paired.receipts[1].environment_hash,
                transition_sequence=paired.receipts[1].transition_sequence,
                evaluator_version=paired.receipts[1].evaluator_version,
                side="challenger", session_date=paired.receipts[1].session_date,
                receipt_fingerprint="1" * 64)
            # 先塞一条"冠军已存在"的漂移行，让 B 侧（挑战者）注定冲突。
            conn.execute(
                "INSERT INTO paired_replay_receipts (experiment_id, target_identity,"
                " environment_hash, transition_sequence, evaluator_version, side,"
                " session_date, receipt_fingerprint, step, next_state, published_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (paired.receipts[1].experiment_id, paired.receipts[1].target_identity,
                 paired.receipts[1].environment_hash,
                 int(paired.receipts[1].transition_sequence),
                 paired.receipts[1].evaluator_version, "challenger",
                 paired.receipts[1].session_date, "2" * 64, "{}", "{}", "2026-09-28T00:00:00Z"))
            with self.assertRaises(RL.ReceiptConflict):
                RL.publish_pair(conn, champion=paired.receipts[0],
                                challenger=drifted,
                                champion_step={}, challenger_step={},
                                champion_state={}, challenger_state={})
            sides_published = {row["side"] for row in RL.receipts(conn, "exp-1")}
            self.assertNotIn("champion", sides_published, "B 侧失败时 A 侧不能单独完成")

    def test_latest_state_returns_the_last_published_side_state(self):
        with AQ.connect(self.path) as conn:
            self.assertIsNone(RL.latest_state(conn, "exp-1", NPR.SIDE_CHAMPION))
            paired, _c, _x = self._pair(conn)
            self._publish(conn, paired)
            latest = RL.latest_state(conn, "exp-1", NPR.SIDE_CHAMPION)
            self.assertEqual(latest["state"], {"cash": 1.0})
            self.assertEqual(latest["session_date"], TRADING_DAYS[0])

    def test_a_run_cannot_be_re_registered_with_different_parameters(self):
        with AQ.connect(self.path) as conn:
            champion, challenger = sides()
            enviro = environment()
            self.assertTrue(RL.record_run(
                conn, experiment_id="exp-1", target_identity="t",
                environment_hash=enviro.environment_hash,
                champion=champion.identity(), challenger=challenger.identity(),
                rules=rules().identity(), sessions=[]))
            with self.assertRaises(RL.ExperimentRunMismatch):
                RL.record_run(
                    conn, experiment_id="exp-1", target_identity="t2",
                    environment_hash=enviro.environment_hash,
                    champion=champion.identity(), challenger=challenger.identity(),
                    rules=rules().identity(), sessions=[])


class IncrementTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/research.sqlite3"
        self.clock = lambda: None

    def tearDown(self):
        self._folder.cleanup()

    def _enqueue(self, conn):
        champion, challenger = sides()
        return EI.enqueue_experiment(
            conn, experiment_id="exp-1", target_identity="t",
            environment=environment(), champion=champion, challenger=challenger,
            rules=rules())

    def _drain(self, conn):
        return AR.run_batch(conn, max_tasks=50, handlers=HANDLERS, worker_id="w1",
                            clock=self.clock)

    def test_enqueue_records_the_run_and_schedules_only_the_first_session(self):
        with AQ.connect(self.path) as conn:
            report = self._enqueue(conn)
            self.assertTrue(report["run_recorded"])
            self.assertIsNotNone(report["next_task"])
            active = AQ.list_tasks(conn, status=list(AQ.ACTIVE_STATUSES))
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["payload"]["session_index"], 0)
            progress = EI.experiment_progress(conn, "exp-1")
            self.assertEqual(progress["sessions_total"], len(TRADING_DAYS))
            self.assertEqual(progress["sessions_published"], 0)

    def test_draining_the_queue_publishes_every_session(self):
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            results = self._drain(conn)
            self.assertEqual(len(results), len(TRADING_DAYS))
            progress = EI.experiment_progress(conn, "exp-1")
            self.assertTrue(progress["complete"])
            self.assertEqual(progress["receipt_count"], 2 * len(TRADING_DAYS))
            self.assertEqual(len(AQ.list_tasks(conn, status=list(AQ.ACTIVE_STATUSES))), 0)

    def test_incremental_and_full_replay_publish_identical_receipts(self):
        champion, challenger = sides()
        full = NPR.run_paired_replay(
            environment=environment(), champion=champion, challenger=challenger,
            rules=rules(), select=NPR.strategies_selector(),
            experiment_id="exp-1", target_identity="t")
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            self._drain(conn)
            incremental = [row["receipt_fingerprint"] for row in RL.receipts(conn, "exp-1")]
        self.assertEqual(incremental,
                         [receipt.receipt_fingerprint for receipt in full.receipts])

    def test_a_restart_continues_from_the_ledger(self):
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            first = AR.run_batch(conn, max_tasks=2, handlers=HANDLERS, worker_id="w1")
            self.assertEqual(len(first), 2)
            published_before = len(RL.receipts(conn, "exp-1"))
            self.assertEqual(published_before, 4)
        # 新进程：同一研究库、全新连接。
        with AQ.connect(self.path) as conn:
            AR.run_batch(conn, max_tasks=50, handlers=HANDLERS, worker_id="w2")
            receipts = RL.receipts(conn, "exp-1")
            self.assertEqual(len(receipts), 2 * len(TRADING_DAYS))
            sequences = [row["transition_sequence"] for row in receipts]
            self.assertEqual(sequences, sorted(sequences))
            self.assertEqual(len(set(sequences)), len(sequences))

    def test_a_duplicate_event_does_not_schedule_a_second_task(self):
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            self._enqueue(conn)
            active = AQ.list_tasks(conn, status=list(AQ.ACTIVE_STATUSES))
            self.assertEqual(len(active), 1)

    def test_reprocessing_a_published_session_does_not_publish_again(self):
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            self._drain(conn)
            before = len(RL.receipts(conn, "exp-1"))
            task, _created = AQ.enqueue(
                conn, task_type=EI.TASK_TYPE_SESSION, goal="replay session",
                input_fingerprint="replay-session-0", event_version="manual",
                payload={"experiment_id": "exp-1", "session_index": 0})
            self_handled = AR.run_batch(conn, max_tasks=1, handlers=HANDLERS,
                                        worker_id="w1")
            self.assertEqual(len(self_handled), 1)
            self.assertEqual(len(RL.receipts(conn, "exp-1")), before)
            stored = AQ.get(conn, task["task_id"])
            self.assertEqual(stored["status"], AQ.STATUS_SUCCEEDED)
            self.assertEqual(stored["result"]["already_published"], True)

    def test_a_later_session_cannot_jump_ahead_of_its_predecessor(self):
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            # 用更高优先级抢在会话 0 前面被领取，验证顺序是硬约束而不是靠巧合。
            AQ.enqueue(conn, task_type=EI.TASK_TYPE_SESSION,
                       goal="jump ahead", input_fingerprint="skip-ahead",
                       event_version="manual", priority=AQ.PRIORITY_CRITICAL,
                       payload={"experiment_id": "exp-1", "session_index": 2})
            result = AR.run_batch(conn, max_tasks=1, handlers=HANDLERS, worker_id="w1")
            self.assertEqual(result[0]["status"], AQ.STATUS_WAITING_DATA)
            self.assertEqual(result[0]["blocking_reason"], EI.R_PREVIOUS_NOT_PUBLISHED)
            self.assertEqual(RL.receipts(conn, "exp-1"), [])

    def test_a_waiting_session_is_woken_when_its_predecessor_publishes(self):
        """恢复路径：某会话曾因前序未发布而进入等待态，前序发布后必须被唤醒。"""
        with AQ.connect(self.path) as conn:
            enviro = environment()
            second = enviro.sessions[1]
            # 先造出"会话 1 的等待态任务"，再让编排投递会话 0，这样 claim 一定先领到它。
            stale, _created = AQ.enqueue(
                conn, task_type=EI.TASK_TYPE_SESSION, goal="stale session 1",
                input_fingerprint=EI._fingerprint({
                    "experiment_id": "exp-1",
                    "environment_hash": enviro.environment_hash,
                    "session_date": second.session_date}),
                event_version=second.session_date,
                payload={"experiment_id": "exp-1", "session_index": 1})
            claimed = AQ.claim(conn, worker_id="ghost")
            self.assertEqual(claimed["task_id"], stale["task_id"])
            AQ.start(conn, claimed["task_id"], claimed["lease_token"])
            AQ.awaiting(conn, claimed["task_id"], claimed["lease_token"],
                        status=AQ.STATUS_WAITING_DATA, reason=EI.R_PREVIOUS_NOT_PUBLISHED)

            champion, challenger = sides()
            EI.enqueue_experiment(
                conn, experiment_id="exp-1", target_identity="t", environment=enviro,
                champion=champion, challenger=challenger, rules=rules())
            AR.run_batch(conn, max_tasks=1, handlers=HANDLERS, worker_id="w1")
            # 会话 0 发布后，编排放出的会话 1 任务命中了那条等待态任务并唤醒它。
            self.assertEqual(AQ.get(conn, stale["task_id"])["status"], AQ.STATUS_QUEUED)
            AR.run_batch(conn, max_tasks=10, handlers=HANDLERS, worker_id="w1")
            self.assertEqual(AQ.list_tasks(conn, status=[AQ.STATUS_WAITING_DATA]), [])
            self.assertTrue(EI.experiment_progress(conn, "exp-1")["complete"])

    def test_progress_reports_both_nav_series(self):
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            self._drain(conn)
            progress = EI.experiment_progress(conn, "exp-1")
            for side in (NPR.SIDE_CHAMPION, NPR.SIDE_CHALLENGER):
                series = progress["nav_series"][side]
                self.assertEqual(len(series), len(TRADING_DAYS))
                self.assertEqual([row[0] for row in series], list(TRADING_DAYS))
                self.assertTrue(all(row[1] > 0 for row in series))

    def test_an_unknown_experiment_is_refused(self):
        with AQ.connect(self.path) as conn:
            with self.assertRaises(EI.IncrementError):
                EI.experiment_progress(conn, "nope")
            with self.assertRaises(EI.IncrementError):
                EI.ensure_next_session_enqueued(conn, "nope")

    def test_the_progress_cli_reports_state_and_exit_codes(self):
        import io
        from unittest import mock

        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            self._drain(conn)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(EI.main(["progress", "--experiment", "exp-1",
                                      "--path", self.path]), 0)
            self.assertIn("\"complete\": true", out.getvalue())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(EI.main(["progress", "--experiment", "missing",
                                      "--path", self.path]), 2)
            self.assertIn("error", out.getvalue())

    def test_the_progress_cli_works_as_a_real_subprocess(self):
        """R7：`__main__` 早于函数定义会让真实命令行直接 NameError。"""
        import json
        import os
        import subprocess
        import sys

        backend = os.path.dirname(os.path.abspath(EI.__file__))
        env = dict(os.environ, PYTHONIOENCODING="utf-8")

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, "-m", "experiment_increment", *args],
                cwd=backend, capture_output=True, text=True, encoding="utf-8", env=env)

        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            self._drain(conn)

        done = run_cli("progress", "--experiment", "exp-1", "--path", self.path)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["sessions_published"], len(TRADING_DAYS))

        missing = run_cli("progress", "--experiment", "nope", "--path", self.path)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("error", json.loads(missing.stdout))

        fresh = f"{self._folder.name}/fresh.sqlite3"
        uninitialized = run_cli("progress", "--experiment", "exp-1", "--path", fresh)
        self.assertEqual(uninitialized.returncode, 0, uninitialized.stderr)
        self.assertFalse(json.loads(uninitialized.stdout)["initialized"])

    def test_a_twenty_session_experiment_needs_no_budget_change(self):
        """R5：工具预算按任务计步，不能把实验会话序号当步数。"""
        import universe as U
        import datetime as dt

        days = []
        day = dt.date(2026, 9, 28)
        while len(days) < 20:
            if U.is_trade_day(day):
                days.append(day.isoformat())
            day += dt.timedelta(days=1)
        champion, challenger = sides()
        with AQ.connect(self.path) as conn:
            EI.enqueue_experiment(
                conn, experiment_id="exp-long", target_identity="t",
                environment=environment(days=tuple(days)), champion=champion,
                challenger=challenger, rules=rules())
            results = self._drain(conn)
            self.assertEqual(len(results), 20)
            self.assertFalse([row for row in results
                              if row.get("status") != AQ.STATUS_SUCCEEDED])
            progress = EI.experiment_progress(conn, "exp-long")
            self.assertTrue(progress["complete"])
            self.assertEqual(progress["receipt_count"], 40)
            self.assertEqual(progress["failed_tasks"], [])
            stored = AQ.list_tasks(conn, task_type=EI.TASK_TYPE_SESSION, limit=200)
            self.assertEqual(len(stored), 20)
            # 每个任务只记自己的工具步数（=1），与实验第几天无关。
            self.assertEqual({task["step"] for task in stored}, {1})

    def test_a_half_published_session_is_reported_and_blocks_progress(self):
        """R3：单侧残留必须显式报损坏，不能算作已发布后继续往前跑。"""
        champion, challenger = sides()
        enviro = environment()
        with AQ.connect(self.path) as conn:
            EI.enqueue_experiment(
                conn, experiment_id="exp-1", target_identity="t", environment=enviro,
                champion=champion, challenger=challenger, rules=rules())
            paired = NPR.run_paired_session(
                session=enviro.sessions[0], index=0,
                champion_state=NPR.SideState.initial(CAPITAL),
                challenger_state=NPR.SideState.initial(CAPITAL),
                champion=champion, challenger=challenger, rules=rules(),
                select=NPR.strategies_selector(), experiment_id="exp-1",
                target_identity="t", environment_hash=enviro.environment_hash)
            # 只写冠军一侧，模拟历史上没跑完原子的发布。
            RL.ensure_schema(conn)
            conn.execute(
                "INSERT INTO paired_replay_receipts (experiment_id, target_identity,"
                " environment_hash, transition_sequence, evaluator_version, side,"
                " session_date, receipt_fingerprint, step, next_state, published_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("exp-1", "t", enviro.environment_hash,
                 int(paired.receipts[0].transition_sequence), NPR.EVALUATOR_VERSION,
                 "champion", paired.receipts[0].session_date,
                 paired.receipts[0].receipt_fingerprint, "{}", "{}",
                 "2026-09-28T00:00:00Z"))
            progress = EI.experiment_progress(conn, "exp-1")
            self.assertEqual(progress["sessions_published"], 0)
            self.assertIn(TRADING_DAYS[0], progress["incomplete_receipt_sessions"])
            self.assertFalse(progress["complete"])
            results = self._drain(conn)
            self.assertTrue(results)
            stored = AQ.list_tasks(conn, task_type=EI.TASK_TYPE_SESSION)
            reasons = {task.get("blocking_reason") for task in stored if task.get("blocking_reason")}
            self.assertTrue(any(EI.R_INCOMPLETE_RECEIPT_PAIR in str(reason)
                                for reason in reasons), reasons)

    def test_a_missing_position_quote_waits_instead_of_faking_a_valuation(self):
        """R2 的运行侧：缺持仓报价 → 等待数据，而不是把市值当 0 发布。"""
        day_one = TRADING_DAYS[0]
        sessions = (
            NPR.ReplaySession(session_date=day_one,
                              decision_at=f"{day_one}T14:30:00+08:00",
                              quotes=(NPR.ReplayQuote("600001", 10.0, prev_close=10.0),),
                              factor_table=factor_table()),
            # 第二天该股没有报价：会话必须转等待，而不是发布一个缺项净值。
            NPR.ReplaySession(session_date=TRADING_DAYS[1],
                              decision_at=f"{TRADING_DAYS[1]}T14:30:00+08:00",
                              quotes=(NPR.ReplayQuote("600002", 12.0, prev_close=12.0),),
                              factor_table=factor_table()),
        )
        champion, challenger = sides()
        with AQ.connect(self.path) as conn:
            EI.enqueue_experiment(
                conn, experiment_id="exp-1", target_identity="t",
                environment=NPR.ReplayEnvironment(sessions=sessions),
                champion=champion, challenger=challenger,
                rules=rules())
            first = AR.run_batch(conn, max_tasks=1, handlers=HANDLERS, worker_id="w1")
            self.assertEqual(first[0]["status"], AQ.STATUS_SUCCEEDED)
            second = AR.run_batch(conn, max_tasks=1, handlers=HANDLERS, worker_id="w1")
            self.assertEqual(second[0]["status"], AQ.STATUS_WAITING_DATA)
            self.assertIn(NPR.R_MISSING_POSITION_QUOTE,
                          second[0].get("blocking_reason") or "")
            progress = EI.experiment_progress(conn, "exp-1")
            self.assertFalse(progress["complete"])
            self.assertEqual(progress["sessions_published"], 1)


if __name__ == "__main__":
    unittest.main()
