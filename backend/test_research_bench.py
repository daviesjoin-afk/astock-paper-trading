# -*- coding: utf-8 -*-
"""耗时基准的验证（方案 S2 的验收工具本身要被验证）。

关键点不是"数字好看"，而是：没测到的段必须写成"未判定"，不能因为没报错就算达标。
"""
import json
import sqlite3
import tempfile
import unittest

import agent_task_queue as AQ
import research_bench as RB


def decision(**overrides):
    payload = {"action": "eliminate", "reason": "基准：无边际", "evidence_refs": []}
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class BenchTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/research.sqlite3"
        self.config_path = f"{self._folder.name}/adaptive_learning.sqlite3"
        self.config = sqlite3.connect(self.config_path)

    def tearDown(self):
        self.config.close()
        self._folder.cleanup()

    def test_a_synthetic_environment_uses_real_trading_days(self):
        environment = RB.build_environment(3)
        self.assertEqual(len(environment.sessions), 3)
        import universe as U

        for session in environment.sessions:
            self.assertTrue(U.is_trade_day(session.session_date))

    def test_the_experiment_leg_produces_real_numbers_without_a_model(self):
        with AQ.connect(self.path) as conn:
            result = RB.bench_experiment(conn, sessions=3)
            self.assertEqual(result["sessions"], 3)
            self.assertEqual(result["published"], 3)
            self.assertTrue(result["complete"])
            self.assertGreater(result["total_s"], 0.0)
            self.assertGreaterEqual(result["total_s"], result["enqueue_s"])

    def test_without_a_configured_provider_the_two_agent_targets_are_not_judged(self):
        with AQ.connect(self.path) as conn:
            report = RB.run_bench(conn, runs=1, sessions=2, config_conn=self.config)
        self.assertFalse(report["agent"]["configured"])
        self.assertEqual(report["agent"]["reason"], RB.R_PROVIDER_NOT_CONFIGURED)
        for name in (RB.TARGET_EVENT_TO_TASK, RB.TARGET_SNAPSHOT_TO_OPINION):
            self.assertIsNone(report["targets"][name]["met"], name)
            self.assertEqual(report["targets"][name]["samples"], 0)
        # 实验段不需要模型，所以它仍然是实测值。
        self.assertTrue(report["targets"][RB.TARGET_SMALL_EXPERIMENT]["samples"] >= 1)
        self.assertIsNotNone(
            report["targets"][RB.TARGET_SMALL_EXPERIMENT]["p95_s"])

    def test_a_fake_provider_closes_the_two_agent_targets(self):
        calls = {"n": 0}

        def provider(system_prompt, user_prompt):
            calls["n"] += 1
            return decision()

        with AQ.connect(self.path) as conn:
            report = RB.run_bench(conn, runs=2, sessions=2, provider=provider,
                             config_conn=self.config)
        self.assertTrue(report["agent"]["configured"])
        self.assertEqual(report["agent"]["opinions"], 2)
        event = report["targets"][RB.TARGET_EVENT_TO_TASK]
        opinion = report["targets"][RB.TARGET_SNAPSHOT_TO_OPINION]
        self.assertEqual(event["samples"], 2)
        self.assertEqual(opinion["samples"], 2)
        self.assertTrue(event["met"])
        self.assertTrue(opinion["met"])
        self.assertGreaterEqual(calls["n"], 2)

    def test_a_run_without_an_opinion_is_reported_as_unmeasured_not_fast(self):
        def provider(system_prompt, user_prompt):
            return json.dumps({"action": "wait_for_data", "reason": "等标签",
                               "wait_for": "next_label_event"}, ensure_ascii=False)

        with AQ.connect(self.path) as conn:
            report = RB.run_bench(conn, runs=2, sessions=0, provider=provider,
                             config_conn=self.config)
        self.assertEqual(report["agent"]["opinions"], 0)
        self.assertEqual(report["targets"][RB.TARGET_SNAPSHOT_TO_OPINION]["samples"], 0)
        self.assertIsNone(report["targets"][RB.TARGET_SNAPSHOT_TO_OPINION]["met"])
        self.assertEqual(len(report["agent"]["runs_without_opinion_s"]), 2)

    def test_the_bench_never_claims_alpha(self):
        with AQ.connect(self.path) as conn:
            report = RB.run_bench(conn, runs=1, sessions=0, config_conn=self.config)
        self.assertIn("收益结论不能由耗时推出", report["note"])


if __name__ == "__main__":
    unittest.main()
