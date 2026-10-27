# -*- coding: utf-8 -*-
"""受限研究候选的验证（F5）：模型只能在一个很窄的窗口里提参数。

边界清单：

* 只有 ``tq_breakout`` 的 selection weight ``flow`` 可以被研究；
* 变体必须在基线附近的有界区间内（幅度 ≤ 5 个百分点）；
* 行情只能来自**事件冻结**的会话 —— 模型自己给 ``sessions`` 一律拒绝；
* 结果永远只是 ADVISORY 证据，不带任何晋升/采用权限。
"""
import tempfile
import unittest

import agent_task_queue as AQ
import experiment_increment as EI
import research_candidate as RC
import research_orchestration as RO
import research_worker as RW

DAYS = ("2026-10-19", "2026-10-20")
IDENTITY = {"strategy_id": "tq_breakout", "strategy_version": 3,
            "strategy_checksum": "c" * 64}


def factor_table():
    import pandas as pd

    return pd.DataFrame([{
        "name": "甲", "industry": "A", "price": 10.0, "pct": 3.0, "amount": 5e7,
        "turnover": 3.0, "main_pct": 1.0, "super_net_raw": 1e6, "mom5_raw": 0.02,
        "mom20_raw": 0.03, "mom60_raw": 0.05, "mom_short": 0.02, "volsurge": 1.5,
        "vol_surge_raw": 2.0, "rsi14_raw": 55.0, "flow": 1.0, "sentiment": 0.0,
        "above_boll_mid": True, "boll_mid_breakout": True, "ma20": 9.5, "ma60": 9.0,
    }], index=["600001"])


def frozen_sessions():
    rows = [{
        "session_date": day, "decision_at": f"{day}T15:00:00+08:00",
        "quotes": [{"code": "600001", "price": 10.0 + index, "prev_close": 10.0}],
        "factor_table": factor_table(), "first_board_codes": ["600001"],
    } for index, day in enumerate(DAYS)]
    return RO.sessions_from_rows(rows)


class CandidateTestCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/research.sqlite3"
        self.baseline = RC.baseline()

    def tearDown(self):
        self._folder.cleanup()

    def create(self, **arguments):
        args = {"variant": self.baseline + 0.03}
        args.update(arguments)
        with AQ.connect(self.path) as conn:
            created = RC.create_and_enqueue(conn, args, frozen_sessions=frozen_sessions())
            AQ.commit(conn)
        return created


class BoundsTests(CandidateTestCase):
    def test_only_the_declared_field_can_be_studied(self):
        for overrides in ({"strategy_id": "trend_pullback"}, {"field": "sentiment"},
                          {"field": "entry_policy"}):
            with self.subTest(**overrides):
                with self.assertRaises(ValueError) as caught:
                    self.create(**overrides)
                self.assertIn("only_tq_breakout_selection_flow_is_supported",
                              str(caught.exception))

    def test_the_variant_must_stay_near_the_baseline(self):
        for variant in (self.baseline, self.baseline + RC.MAX_DELTA + 0.02,
                        RC.MIN_FLOW - 0.01, RC.MAX_FLOW + 0.01):
            with self.subTest(variant=variant):
                with self.assertRaises(ValueError) as caught:
                    self.create(variant=variant)
                self.assertIn("flow_variant_out_of_bounds", str(caught.exception))

    def test_a_missing_or_non_numeric_variant_is_refused(self):
        with self.assertRaises(ValueError):
            with AQ.connect(self.path) as conn:
                RC.create_and_enqueue(conn, {}, frozen_sessions=frozen_sessions())
        with self.assertRaises(ValueError):
            self.create(variant="much-better")

    def test_the_model_cannot_supply_its_own_market_data(self):
        forged = [{"session_date": DAYS[0], "decision_at": f"{DAYS[0]}T14:30:00+08:00",
                   "quotes": [{"code": "600001", "price": 99.0}], "factor_table": None,
                   "first_board_codes": ["600001"]}]
        with self.assertRaises(ValueError) as caught:
            self.create(sessions=forged)
        self.assertIn(RC.R_MODEL_SUPPLIED_SESSIONS, str(caught.exception))

    def test_an_event_without_frozen_sessions_cannot_create_a_candidate(self):
        with AQ.connect(self.path) as conn:
            with self.assertRaises(ValueError) as caught:
                RC.create_and_enqueue(conn, {"variant": self.baseline + 0.03})
        self.assertIn(RC.R_FROZEN_SESSIONS_REQUIRED, str(caught.exception))


class CreationTests(CandidateTestCase):
    def test_a_candidate_is_persisted_with_the_frozen_environment(self):
        created = self.create()
        self.assertEqual(created["field"], RC.SUPPORTED_FIELD)
        self.assertEqual(created["sessions"], len(DAYS))
        self.assertEqual(created["sessions_source"], "research_event")
        with AQ.connect(self.path) as conn:
            row = RC.get(conn, created["experiment_id"])
            progress = EI.experiment_progress(conn, created["experiment_id"])
        self.assertEqual(row["status"], RC.STATUS_RUNNING)
        self.assertEqual(row["environment_hash"], created["environment_hash"])
        self.assertEqual(progress["sessions_total"], len(DAYS))
        self.assertFalse(progress["complete"])

    def test_the_same_frozen_environment_produces_the_same_hash(self):
        first = self.create()
        second = self.create()
        self.assertNotEqual(first["experiment_id"], second["experiment_id"])
        self.assertEqual(first["environment_hash"], second["environment_hash"])

    def test_the_result_never_claims_adoption_authority(self):
        created = self.create()
        with AQ.connect(self.path) as conn:
            result = RC.result(conn, created["experiment_id"])
        self.assertTrue(result["historical_replay_only"])
        self.assertFalse(result["promotion_eligible"])
        self.assertIn("ADVISORY", result["interpretation"])
        self.assertNotIn("authorized", result)
        self.assertNotIn("promoted", result)


class CompletionTests(CandidateTestCase):
    def _completed(self):
        created = self.create()
        with AQ.connect(self.path) as conn:
            report = RW.run_once_pass(conn, max_tasks=10, worker_id="w1")
            AQ.commit(conn)
        return created, report

    def test_the_worker_finishes_the_experiment_and_marks_the_candidate(self):
        created, report = self._completed()
        statuses = [row["status"] for row in report["completed_experiments"]]
        self.assertIn("complete", statuses)
        self.assertEqual(
            [row["experiment_id"] for row in report["completed_experiments"]],
            [created["experiment_id"]])
        with AQ.connect(self.path) as conn:
            self.assertEqual(RC.get(conn, created["experiment_id"])["status"],
                             RC.STATUS_COMPLETE)
            self.assertTrue(RC.result(conn, created["experiment_id"])["complete"])

    def test_waking_the_same_experiment_twice_is_idempotent(self):
        created, _ = self._completed()
        with AQ.connect(self.path) as conn:
            again = RO.wake_experiment(conn, created["experiment_id"])
        self.assertEqual(again["status"], "already_complete")
        self.assertEqual(again["woken_task_ids"], [])

    def test_an_experiment_that_is_not_a_candidate_is_ignored(self):
        with AQ.connect(self.path) as conn:
            report = RO.wake_experiment(conn, "not-a-candidate")
        self.assertEqual(report["status"], "not_a_research_candidate")

    def test_an_unfinished_candidate_is_reported_as_waiting(self):
        created = self.create()
        with AQ.connect(self.path) as conn:
            report = RO.wake_experiment(conn, created["experiment_id"])
            self.assertEqual(report["status"], "waiting_experiment")
            self.assertEqual(RC.get(conn, created["experiment_id"])["status"],
                             RC.STATUS_RUNNING)

    def test_a_waiting_model_task_is_woken_when_the_experiment_completes(self):
        # 先让模型任务进入等待态，再登记并跑完实验：唤醒的是"已经在等"的那个任务。
        with AQ.connect(self.path) as conn:
            task, _ = AQ.enqueue(
                conn, task_type="research_question", goal="g", input_fingerprint="fp-x",
                event_version="ai1", payload={"wait_subscription": {"kind": "experiment"}})
            claimed = AQ.claim(conn, worker_id="w1", lease_seconds=60)
            AQ.start(conn, claimed["task_id"], claimed["lease_token"])
            AQ.awaiting(conn, task["task_id"], claimed["lease_token"],
                        status=AQ.STATUS_WAITING_DATA, reason="waiting experiment")
            AQ.commit(conn)
        created = self.create()
        with AQ.connect(self.path) as conn:
            conn.execute("UPDATE agent_tasks SET payload=? WHERE task_id=?",
                         ('{"wait_subscription":{"kind":"experiment","experiment_id":"'
                          + created["experiment_id"] + '"}}', task["task_id"]))
            AQ.commit(conn)
            report = RW.run_once_pass(conn, max_tasks=10, worker_id="w1")
            AQ.commit(conn)
            self.assertEqual(AQ.get(conn, task["task_id"])["status"], AQ.STATUS_QUEUED)
        woken = [row for row in report["completed_experiments"]
                 if row["status"] == "complete"]
        self.assertEqual(len(woken), 1)
        self.assertIn(task["task_id"], woken[0]["woken_task_ids"])


class EnvironmentFixtureTests(unittest.TestCase):
    def test_historical_selection_cannot_be_mislabeled_as_a_contemporaneous_snapshot(self):
        from unittest import mock
        import datetime as dt

        snapshot = {}
        with mock.patch.object(RO, "_capture_time", return_value="2026-10-21T15:05:00+08:00"):
            RO.capture_selection_snapshot(snapshot, table=factor_table(),
                live_map={"600001": {"price": 10.0}}, first_board_codes=["600001"],
                asof_date=dt.date.fromisoformat(DAYS[0]), selection_model="first_board_relay",
                selection_overlay={})
        self.assertIn("not_contemporaneous_selection_snapshot", snapshot["reason"])
        self.assertNotIn("frozen_sessions", snapshot)

    def test_market_event_preserves_actual_snapshot_time(self):
        event = RO.market_event("tq_breakout", session_date=DAYS[0], environment_ref={},
                                observed_at=f"{DAYS[0]}T15:05:00+08:00")
        self.assertEqual(event["observed_at"], f"{DAYS[0]}T15:05:00+08:00")

    def test_selection_snapshot_freezes_factors_and_first_board_membership(self):
        import native_paired_replay as NPR

        table = factor_table()
        sessions = RO.session_from_selection(
            table, quotes=[{"code": "600001", "price": 10.0, "prev_close": 9.8}],
            first_board_codes=["600001"], session_date=DAYS[0])
        table.loc["600001", "flow"] = -99.0
        restored = NPR.session_from_payload(sessions[0])
        self.assertEqual(restored.factor_table.loc["600001", "flow"], 1.0)
        self.assertEqual(restored.first_board_codes, ("600001",))
        with AQ.connect(":memory:") as conn:
            created = RC.create_and_enqueue(conn, {"variant": RC.baseline() + 0.03},
                                            frozen_sessions=sessions)
            AQ.commit(conn)
            RW.run_once_pass(conn, max_tasks=5)
            self.assertTrue(RC.result(conn, created["experiment_id"])["complete"])

    def test_missing_factors_are_refused_before_an_experiment_is_created(self):
        sessions = RO.session_from_quotes([{"code": "600001", "price": 10.0}],
                                           session_date=DAYS[0])
        with AQ.connect(":memory:") as conn:
            with self.assertRaisesRegex(ValueError, "frozen_factor_table_required"):
                RC.create_and_enqueue(conn, {"variant": RC.baseline() + 0.03},
                                      frozen_sessions=sessions)
            self.assertEqual(RW.known_experiments(conn), [])

    def test_quotes_without_a_price_are_skipped_instead_of_fabricated(self):
        sessions = RO.session_from_quotes(
            [{"code": "600001", "price": None}, {"code": "600002", "price": 5.0}],
            session_date="2026-10-20")
        self.assertEqual([quote["code"] for quote in sessions[0]["quotes"]], ["600002"])

    def test_no_usable_quote_means_no_session(self):
        self.assertEqual(RO.session_from_quotes([], session_date="2026-10-20"), [])
        self.assertEqual(RO.session_from_quotes([{"code": "", "price": 1.0}],
                                                session_date="2026-10-20"), [])


if __name__ == "__main__":
    unittest.main()
