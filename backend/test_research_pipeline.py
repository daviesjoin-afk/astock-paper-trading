# -*- coding: utf-8 -*-
"""生产业务事件 → 持久研究任务的窄入口验证（F5）。

这里的重点不是"模型能不能跑"，而是接线契约：

* 行情/成交/实验事件确实落库，并唤醒正在等它的持久任务；
* 重复事件幂等，不制造额外模型工作；
* 研究侧任何失败都是 fail-open：返回可观测结果，不抛给交易 owner；
* 没有受支持目标清单的策略只记事件，不凭空造研究任务。
"""
import sqlite3
import tempfile
import unittest
from unittest import mock

import agent_task_queue as AQ
import ai_research_agent as AGENT
import experiment_increment as EI
import research_candidate as RC
import research_orchestration as RO
import research_pipeline as RP
import research_worker as RW

DAYS = ("2026-10-19", "2026-10-20", "2026-10-21")
IDENTITY = {"strategy_id": "tq_breakout", "strategy_version": 3,
            "strategy_checksum": "c" * 64}

AGENT_TOOL_CREATE = AGENT.TOOL_CREATE_FLOW_CANDIDATE
AGENT_TOOL_RESULT = AGENT.TOOL_READ_CANDIDATE_RESULT
AGENT_TASK_TYPE = AGENT.TASK_TYPE_RESEARCH
AGENT_RUN = AGENT.run_research_task


def factor_table():
    import pandas as pd

    return pd.DataFrame([{
        "name": "甲", "industry": "A", "price": 10.0, "pct": 3.0, "amount": 5e7,
        "turnover": 3.0, "main_pct": 1.0, "super_net_raw": 1e6, "mom5_raw": 0.02,
        "mom20_raw": 0.03, "mom60_raw": 0.05, "mom_short": 0.02, "volsurge": 1.5,
        "vol_surge_raw": 2.0, "rsi14_raw": 55.0, "flow": 1.0, "sentiment": 0.0,
        "above_boll_mid": True, "boll_mid_breakout": True, "ma20": 9.5, "ma60": 9.0,
    }], index=["600001"])


def frozen_sessions(days=DAYS, *, price=10.0):
    rows = []
    for index, day in enumerate(days):
        rows.append({
            "session_date": day,
            "decision_at": f"{day}T15:00:00+08:00",
            "quotes": [{"code": "600001", "price": round(price * (1 + 0.01 * index), 2),
                        "prev_close": price}],
            "factor_table": factor_table(),
            "first_board_codes": ["600001"],
        })
    return RO.sessions_from_rows(rows)


def market_event(**overrides):
    body = {"kind": "market", "strategy_id": "tq_breakout",
            "observed_at": "2026-10-21T15:00:00+08:00",
            "session_date": "2026-10-21", "environment_ref": {"factor_refresh": "f-1"},
            "exact_identity": dict(IDENTITY),
            "frozen_sessions": frozen_sessions()}
    body.update(overrides)
    return body


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/agent_tasks.sqlite3"

    def tearDown(self):
        self._folder.cleanup()

    def submit(self, event, **kwargs):
        with AQ.connect(self.path) as conn:
            result = RP.submit(conn, event)
            AQ.commit(conn)
        return result

    def tasks(self):
        with AQ.connect(self.path) as conn:
            return AQ.list_tasks(conn, limit=50)


class MarketEventTests(PipelineTestCase):
    def test_missing_factor_table_records_event_without_creating_broken_task(self):
        sessions = frozen_sessions()
        sessions[0]["factor_table"] = None
        result = self.submit(market_event(frozen_sessions=sessions))
        self.assertTrue(result["created"])
        self.assertFalse(result["task_created"])
        self.assertEqual(result["task_blocked_reason"], "frozen_factor_table_required")
        self.assertEqual(self.tasks(), [])

    def test_a_market_event_becomes_one_durable_research_task(self):
        result = self.submit(market_event())
        self.assertEqual(result["status"], "recorded")
        self.assertTrue(result["created"])
        self.assertTrue(result["task_created"])
        tasks = self.tasks()
        self.assertEqual(len(tasks), 1)
        payload = tasks[0]["payload"]
        # 冻结会话随事件进入任务：模型工具只能消费这一份行情。
        self.assertEqual(len(payload["frozen_sessions"]), len(DAYS))
        self.assertEqual(payload["exact_identity"], IDENTITY)
        self.assertEqual(payload["research_target"]["strategy_id"], "tq_breakout")
        self.assertEqual(payload["research_target"]["field"], RC.SUPPORTED_FIELD)

    def test_resubmitting_the_same_event_is_idempotent(self):
        first = self.submit(market_event())
        second = self.submit(market_event())
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertFalse(second["created"])
        self.assertFalse(second["task_created"])
        self.assertEqual(len(self.tasks()), 1)

    def test_a_different_session_is_a_different_event(self):
        first = self.submit(market_event())
        second = self.submit(market_event(session_date="2026-10-20",
                                          observed_at="2026-10-20T15:00:00+08:00"))
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertEqual(len(self.tasks()), 2)

    def test_an_unsupported_strategy_is_recorded_without_a_task(self):
        """trend_pullback 没有 first_board_relay 的 selection_weights 清单。"""
        result = self.submit(market_event(strategy_id="trend_pullback"))
        self.assertEqual(result["status"], "recorded")
        self.assertTrue(result["created"])
        self.assertFalse(result["task_created"])
        self.assertIsNone(result["task_id"])
        self.assertEqual(self.tasks(), [])

    def test_the_event_row_keeps_the_payload_for_replay(self):
        self.submit(market_event())
        with AQ.connect(self.path) as conn:
            row = conn.execute("SELECT kind,strategy_id,payload FROM research_business_events"
                               ).fetchone()
        self.assertEqual(row["kind"], "market")
        self.assertEqual(row["strategy_id"], "tq_breakout")
        self.assertIn("frozen_sessions", row["payload"])

    def test_a_label_event_without_frozen_evidence_only_wakes_waiters(self):
        """标签成熟没有行情可回放：记事件、唤醒等待者，但不给模型空任务。"""
        result = self.submit({"kind": "label", "strategy_id": "tq_breakout",
                              "observed_at": "2026-10-21T16:00:00+08:00",
                              "new_rewards": 12})
        self.assertTrue(result["created"])
        self.assertFalse(result["task_created"])
        self.assertEqual(self.tasks(), [])


class EventValidationTests(PipelineTestCase):
    def test_an_unknown_event_kind_is_refused(self):
        with self.assertRaises(ValueError):
            self.submit({"kind": "rumour", "observed_at": "2026-10-21T15:00:00+08:00"})

    def test_an_event_without_an_observation_time_is_refused(self):
        with self.assertRaises(ValueError):
            self.submit({"kind": "fill", "strategy_id": "tq_breakout"})


class WakeTests(PipelineTestCase):
    def _waiting_task(self, subscription, *, kind=RP.EVENT_LABEL):
        with AQ.connect(self.path) as conn:
            task, _ = AGENT.enqueue_research_question(
                conn, goal="g", hypothesis="h", exact_identity=dict(IDENTITY),
                input_fingerprint=f"fp-{subscription.get('kind')}")
            claimed = AQ.claim(conn, worker_id="w1", lease_seconds=60)
            AQ.start(conn, claimed["task_id"], claimed["lease_token"])
            payload = dict(task["payload"])
            payload["wait_subscription"] = dict(subscription)
            conn.execute("UPDATE agent_tasks SET payload=? WHERE task_id=?",
                         (AQ._dump(payload), task["task_id"]))
            AQ.awaiting(conn, task["task_id"], claimed["lease_token"],
                        status=AQ.STATUS_WAITING_DATA, reason="waiting")
            AQ.commit(conn)
        return task["task_id"]

    def _status(self, task_id):
        with AQ.connect(self.path) as conn:
            return AQ.get(conn, task_id)["status"]

    def test_a_fill_event_wakes_a_task_waiting_on_fills(self):
        task_id = self._waiting_task({"kind": "fill", "strategy_id": "tq_breakout"})
        result = self.submit({"kind": "fill", "strategy_id": "tq_breakout",
                              "code": "600001", "observed_at": "2026-10-21T10:00:00+08:00",
                              "order_id": 7, "status": "filled"})
        self.assertIn(task_id, result["woken_task_ids"])
        self.assertEqual(self._status(task_id), AQ.STATUS_QUEUED)

    def test_a_market_event_does_not_wake_a_fill_waiter(self):
        task_id = self._waiting_task({"kind": "fill", "strategy_id": "tq_breakout"})
        result = self.submit(market_event())
        self.assertNotIn(task_id, result["woken_task_ids"])
        self.assertEqual(self._status(task_id), AQ.STATUS_WAITING_DATA)

    def test_a_strategy_filtered_waiter_is_not_woken_by_another_strategy(self):
        task_id = self._waiting_task({"kind": "label", "strategy_id": "trend_pullback"})
        result = self.submit({"kind": "label", "strategy_id": "tq_breakout",
                              "observed_at": "2026-10-21T16:00:00+08:00"})
        self.assertNotIn(task_id, result["woken_task_ids"])

    def test_an_experiment_waiter_is_woken_by_the_completion_event(self):
        task_id = self._waiting_task({"kind": "experiment", "experiment_id": "exp-1"})
        with AQ.connect(self.path) as conn:
            conn.execute("CREATE TABLE research_business_events ("
                         " event_id TEXT PRIMARY KEY, kind TEXT NOT NULL,"
                         " strategy_id TEXT NOT NULL, code TEXT, observed_at TEXT NOT NULL,"
                         " payload TEXT NOT NULL, created_at TEXT NOT NULL)")
            result = RP.submit(conn, {"kind": "experiment", "experiment_id": "exp-1",
                                      "strategy_id": "tq_breakout",
                                      "observed_at": "2026-10-21"})
            AQ.commit(conn)
        self.assertIn(task_id, result["woken_task_ids"])
        self.assertEqual(self._status(task_id), AQ.STATUS_QUEUED)


class FailureOpenTests(PipelineTestCase):
    def test_a_broken_research_store_returns_a_failed_result_instead_of_raising(self):
        with mock.patch.object(RP.AQ, "connect", side_effect=RuntimeError("disk gone")):
            result = RP.submit_safe(market_event())
        self.assertEqual(result["status"], "failed")
        self.assertIn("disk gone", result["error"])

    def test_the_ledger_audit_records_both_success_and_failure(self):
        ledger = sqlite3.connect(f"{self._folder.name}/paper.sqlite3")
        ledger.row_factory = sqlite3.Row
        ledger.execute("CREATE TABLE paper_audit (account_id TEXT, event TEXT,"
                       " detail TEXT, created_at TEXT)")
        try:
            ok = RP.submit_safe(market_event(), queue_path=self.path, ledger_conn=ledger)
            self.assertEqual(ok["status"], "recorded")
            with mock.patch.object(RP.AQ, "connect", side_effect=RuntimeError("boom")):
                bad = RP.submit_safe(market_event(), queue_path=self.path,
                                     ledger_conn=ledger)
            self.assertEqual(bad["status"], "failed")
            events = [row["event"] for row in ledger.execute(
                "SELECT event FROM paper_audit ORDER BY rowid").fetchall()]
            self.assertEqual(events, ["research_event_recorded", "research_event_failed"])
        finally:
            ledger.close()


class ExperimentCompletionTests(PipelineTestCase):
    def _completed_candidate(self):
        with AQ.connect(self.path) as conn:
            created = RC.create_and_enqueue(
                conn, {"variant": RC.baseline() + 0.03},
                frozen_sessions=frozen_sessions())
            AQ.commit(conn)
            RW.run_once_pass(conn, max_tasks=10, worker_id="w1")
            AQ.commit(conn)
        return created["experiment_id"]

    def test_the_completion_event_uses_a_scalar_timestamp(self):
        experiment_id = self._completed_candidate()
        with AQ.connect(self.path) as conn:
            event = RP.completed_experiment_event(conn, experiment_id)
            AQ.commit(conn)
            row = conn.execute("SELECT observed_at,kind FROM research_business_events"
                               " WHERE kind='experiment'").fetchone()
        self.assertEqual(event["status"], "recorded")
        self.assertEqual(row["kind"], "experiment")
        # observed_at 必须是能比较的标量时间，不是净值序列字典。
        self.assertIsInstance(row["observed_at"], str)
        self.assertEqual(row["observed_at"], DAYS[-1])

    def test_an_incomplete_experiment_has_no_completion_event(self):
        with AQ.connect(self.path) as conn:
            created = RC.create_and_enqueue(
                conn, {"variant": RC.baseline() + 0.03},
                frozen_sessions=frozen_sessions())
            AQ.commit(conn)
            result = RP.completed_experiment_event(conn, created["experiment_id"])
        self.assertEqual(result["status"], "waiting_experiment")

    def test_an_unknown_experiment_is_never_reported_as_a_candidate(self):
        with AQ.connect(self.path) as conn:
            with self.assertRaises(EI.IncrementError):
                RP.completed_experiment_event(conn, "missing")


class EndToEndLoopTests(PipelineTestCase):
    """从行情事件到 ADVISORY 意见，再到交易账本归因的完整闭环。"""

    def _provider(self):
        import json as _json

        state = {"waited": False}

        def provider(system_prompt, user_prompt):
            history = _json.loads(user_prompt)["tool_history"]
            tools = [row for row in history if row.get("role") == "tool"]
            names = [row.get("name") for row in tools]
            if AGENT_TOOL_CREATE not in names:
                return _json.dumps({
                    "action": "continue", "reason": "先造受限 flow 候选",
                    "next_tool": {"name": AGENT_TOOL_CREATE,
                                  "arguments": {"variant": RC.baseline() + 0.03}}})
            if AGENT_TOOL_RESULT not in names:
                experiment_id = tools[-1]["result"]["experiment_id"]
                return _json.dumps({
                    "action": "continue", "reason": "读实验结果",
                    "next_tool": {"name": AGENT_TOOL_RESULT,
                                  "arguments": {"experiment_id": experiment_id}}})
            if not state["waited"]:
                state["waited"] = True
                return _json.dumps({"action": "wait_for_data",
                                    "reason": "等成对回执跑完",
                                    "wait_for": "experiment_complete"})
            experiment_id = tools[-1]["result"]["experiment_id"]
            return _json.dumps({
                "action": "publish_advisory", "reason": "差异可观察",
                "evidence_refs": [experiment_id],
                "valid_until": "2026-10-30T00:00:00+00:00",
                "advisory": {"action": "prefer_entry", "confidence": 0.6}})

        return provider

    def _run_research(self, provider):
        import functools

        import agent_task_runner as AR

        with AQ.connect(self.path) as conn:
            results = AR.run_batch(
                conn, max_tasks=1, worker_id="model",
                handlers={AGENT_TASK_TYPE: AR.HandlerSpec(
                    handler=functools.partial(AGENT_RUN, provider=provider))})
            AQ.commit(conn)
        return results

    def _complete_waiting_question(self):
        self.submit(market_event())
        first = self._run_research(self._provider())
        self.assertEqual(first[0]["status"], AQ.STATUS_WAITING_DATA)
        with AQ.connect(self.path) as conn:
            RW.run_once_pass(conn, max_tasks=20, worker_id="experiment")
            AQ.commit(conn)

    def test_woken_model_sees_committed_results_without_requesting_a_second_read(self):
        import json

        self._complete_waiting_question()
        seen = []

        def resumed(system_prompt, user_prompt):
            observations = [row["result"] for row in json.loads(user_prompt)["tool_history"]
                            if row.get("name") == AGENT_TOOL_RESULT]
            self.assertTrue(observations[-1]["complete"])
            self.assertEqual(observations[-1]["sessions_published"], len(DAYS))
            seen.append(observations[-1]["receipt_count"])
            return json.dumps({"action": "eliminate", "reason": "完整回执没有证明优势"})

        result = self._run_research(resumed)
        self.assertEqual(result[0]["status"], AQ.STATUS_REJECTED)
        self.assertIn("recommendation_id", result[0]["result"])
        self.assertEqual(seen, [2 * len(DAYS)])

    def test_waiting_again_for_a_completed_experiment_is_terminal_not_permanently_waiting(self):
        import json

        self._complete_waiting_question()
        result = self._run_research(lambda *_: json.dumps({
            "action": "wait_for_data", "reason": "仍然等待", "wait_for": "experiment_complete"}))
        self.assertEqual(result[0]["status"], AQ.STATUS_REJECTED)
        self.assertEqual(result[0]["blocking_reason"], "cannot_wait_for_completed_experiment")

    def test_completed_experiment_can_still_wait_for_new_market_labels(self):
        import json

        self._complete_waiting_question()
        result = self._run_research(lambda *_: json.dumps({
            "action": "wait_for_data", "reason": "需要新的收益标签", "wait_for": "new_label"}))
        self.assertEqual(result[0]["status"], AQ.STATUS_WAITING_DATA)
        self.assertEqual(result[0]["payload"]["wait_subscription"]["kind"], "label")

    def test_the_loop_publishes_advisory_without_adopting_anything(self):
        self.submit(market_event())
        provider = self._provider()
        first = self._run_research(provider)
        self.assertEqual(first[0]["status"], AQ.STATUS_WAITING_DATA)

        # 实验由 worker 跑完，再显式唤醒等待中的模型任务。
        with AQ.connect(self.path) as conn:
            RW.run_once_pass(conn, max_tasks=20, worker_id="w1")
            AQ.commit(conn)
        second = self._run_research(provider)
        self.assertEqual(second[0]["status"], AQ.STATUS_SUCCEEDED)
        self.assertEqual(second[0]["result"]["mode"], "ADVISORY")
        self.assertFalse(second[0]["result"]["auto_adopted"])

        import ai_recommendation_store as STORE

        with AQ.connect(self.path) as conn:
            stored = [item for item in STORE.list_for_task(conn, second[0]["task_id"])]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["action"], "prefer_entry")
        self.assertEqual(stored[0]["payload"]["mode"], "ADVISORY")
        self.assertTrue(stored[0]["payload"]["experiment_ids"])

    def test_all_cash_replay_cannot_be_used_as_trade_advice_evidence(self):
        sessions = frozen_sessions()
        for session in sessions:
            session["quotes"][0]["prev_close"] = None
        self.submit(market_event(frozen_sessions=sessions))
        provider = self._provider()
        self.assertEqual(self._run_research(provider)[0]["status"], AQ.STATUS_WAITING_DATA)
        with AQ.connect(self.path) as conn:
            RW.run_once_pass(conn, max_tasks=20, worker_id="experiment")
            AQ.commit(conn)
        result = self._run_research(provider)[0]
        self.assertEqual(result["status"], AQ.STATUS_REJECTED)
        self.assertEqual(result["blocking_reason"], "advisory_requires_observable_fills")

    def test_consuming_the_published_opinion_never_moves_the_rule_decision(self):
        import sqlite3

        import ai_advice_service as ADV
        import paper_schema_migrations as PSM

        self.submit(market_event())
        provider = self._provider()
        self._run_research(provider)
        with AQ.connect(self.path) as conn:
            RW.run_once_pass(conn, max_tasks=20, worker_id="w1")
            AQ.commit(conn)
        self._run_research(provider)

        ledger = sqlite3.connect(f"{self._folder.name}/paper.sqlite3")
        ledger.row_factory = sqlite3.Row
        try:
            ledger.execute("CREATE TABLE paper_risk_decisions (id INTEGER PRIMARY KEY"
                           " AUTOINCREMENT, account_id TEXT, code TEXT, side TEXT,"
                           " decision TEXT)")
            ledger.execute("INSERT INTO paper_risk_decisions(account_id,code,side,decision)"
                           " VALUES('tq_breakout','600001','buy','approved_signal')")
            ledger.execute("CREATE TABLE paper_audit (account_id TEXT, event TEXT,"
                           " detail TEXT, created_at TEXT)")
            PSM.ensure_ai_decision_audits(ledger)
            result = ADV.consume_risk_decision_safe(
                ledger, account_id="tq_breakout", code="600001", side="buy",
                decision="approved_signal",
                strategy_stamp=(IDENTITY["strategy_id"], IDENTITY["strategy_version"],
                                IDENTITY["strategy_checksum"]),
                queue_path=self.path)
            decision = ledger.execute("SELECT decision FROM paper_risk_decisions"
                                      ).fetchone()["decision"]
        finally:
            ledger.close()
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["audit"]["mode"], ADV.MODE_ADVISORY)
        self.assertEqual(result["audit"]["effect_reason"], ADV.R_MODE_IS_ADVISORY)
        self.assertIsNone(result["audit"]["consumed_recommendation_id"])
        self.assertEqual(result["final_decision"], result["audit"]["rules_only_decision"])
        self.assertEqual(decision, "approved_signal")


if __name__ == "__main__":
    unittest.main()
