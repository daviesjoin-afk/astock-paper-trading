# -*- coding: utf-8 -*-
"""研究循环生产入口的验证（方案 S1 第一条：明确的注册/启动入口）。"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import adaptive_engine as AE
import agent_task_queue as AQ
import agent_task_runner as AR
import experiment_increment as EI
import native_paired_replay as NPR
import replay_receipt_ledger as RL
import research_worker as RW

TRADING_DAYS = ("2026-09-28", "2026-09-29", "2026-09-30")
CAPITAL = 100_000.0


def factor_table():
    import pandas as pd

    return pd.DataFrame([{
        "name": "甲", "industry": "A", "price": 10.0, "pct": 3.0, "amount": 5e7,
        "turnover": 3.0, "main_pct": 1.0, "super_net_raw": 1e6, "mom5_raw": 0.02,
        "mom20_raw": 0.03, "mom60_raw": 0.05, "mom_short": 0.02, "volsurge": 1.5,
        "vol_surge_raw": 2.0, "rsi14_raw": 55.0, "flow": 1.0, "sentiment": 0.0,
        "above_boll_mid": True, "boll_mid_breakout": True, "ma20": 9.5, "ma60": 9.0,
    }], index=["600001"])


def environment():
    sessions = tuple(
        NPR.ReplaySession(
            session_date=day, decision_at=f"{day}T14:30:00+08:00",
            quotes=(NPR.ReplayQuote("600001", round(10.0 * (1 + 0.01 * index), 2),
                                    prev_close=10.0),),
            factor_table=factor_table(), first_board_codes=("600001",))
        for index, day in enumerate(TRADING_DAYS))
    return NPR.ReplayEnvironment(sessions=sessions)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/research.sqlite3"
        self.config_path = f"{self._folder.name}/adaptive_learning.sqlite3"
        # handler 注册表是执行器的进程级状态：入口会往里写，跑完必须还原，
        # 否则同进程里的 `agent_task_runner` 命令行用例会看到"已经有人注册"。
        self._handlers_before = dict(AR._HANDLERS)
        self._config_owner = mock.patch.multiple(
            AE, DB_PATH=self.config_path, CACHE_DIR=self._folder.name)
        self._config_owner.start()

    def tearDown(self):
        AR._HANDLERS.clear()
        AR._HANDLERS.update(self._handlers_before)
        self._config_owner.stop()
        self._folder.cleanup()

    def _enqueue(self, conn, experiment_id="exp-1"):
        champion, challenger = NPR.sides_from_manifest(
            "tq_breakout", target_kind="entry_policy", model_family="first_board_relay",
            variant_fields={"first_board_bonus": 0.0})
        return EI.enqueue_experiment(
            conn, experiment_id=experiment_id, target_identity="t",
            environment=environment(), champion=champion, challenger=challenger,
            rules=NPR.ReplayRules(reference_capital=CAPITAL, max_positions=1,
                                  hold_sessions=1, topn=1))

    def test_the_worker_registers_the_research_handlers(self):
        """注册是全局表上的动作：跑完必须还原，否则会污染同进程里的其他命令行用例。"""
        before = dict(AR._HANDLERS)
        try:
            report = RW.register()
            self.assertEqual(report["registered"], sorted(RW.HANDLERS))
            self.assertEqual(AR.registered_types(), sorted(set(before) | set(RW.HANDLERS)))
        finally:
            AR._HANDLERS.clear()
            AR._HANDLERS.update(before)

    def test_a_registered_worker_drains_an_experiment_to_completion(self):
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            report = RW.run_once_pass(conn, max_tasks=10, worker_id="w1")
            self.assertEqual(len(report["results"]), len(TRADING_DAYS))
            snapshot = RW.snapshot(conn)
            experiment = snapshot["experiments"][0]
            self.assertTrue(experiment["complete"])
            self.assertEqual(experiment["sessions_published"], len(TRADING_DAYS))
            self.assertEqual(snapshot["queue"]["active_total"], 0)

    def test_resource_denial_defers_replay_without_publishing_receipts(self):
        """重型租约被拒：任务回到可重试态，回放一行都没跑、回执一条都没发。"""
        import contextlib

        ran = []

        def spy(task, ctx):
            ran.append(task["task_id"])
            raise AssertionError("resource_denied 之后不该执行回放")

        @contextlib.contextmanager
        def denied(job_name):
            yield {"allowed": False, "reason": "heavy_job_busy"}

        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            AQ.commit(conn)
            queued = AQ.list_tasks(conn, status=list(AQ.ACTIVE_STATUSES))
            self.assertEqual(len(queued), 1)
            with mock.patch.object(
                    AR.resource_guard, "heavy_job_lease",
                    mock.Mock(wraps=denied)) as lease, \
                    mock.patch.dict(RW.HANDLERS, {EI.TASK_TYPE_SESSION:
                                                  AR.HandlerSpec(handler=spy, heavy=True)}):
                report = RW.run_once_pass(conn, max_tasks=1, worker_id="w1")
            self.assertEqual(lease.call_count, 1)
            self.assertEqual(ran, [])
            self.assertEqual(report["results"][0]["status"], AQ.STATUS_DEFERRED)
            self.assertEqual(report["results"][0]["blocking_reason"],
                             "resource_heavy_job_busy")
            stored = AQ.get(conn, queued[0]["task_id"])
            self.assertEqual(stored["status"], AQ.STATUS_DEFERRED)
            self.assertGreaterEqual(stored["attempts"], 1)
            self.assertIsNone(stored["lease_token"])
            self.assertEqual(RL.receipts(conn, "exp-1"), [])
            progress = EI.experiment_progress(conn, "exp-1")
            self.assertEqual(progress["sessions_published"], 0)
            self.assertFalse(progress["complete"])

    def test_a_lost_session_task_is_re_enqueued_before_the_pass(self):
        """启动入口要能自愈：队列里丢了下一会话的任务时，这一轮会先补上。"""
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            AQ.trim_queue(conn, capacity=0)
            AQ.commit(conn)
            self.assertEqual(
                AQ.list_tasks(conn, status=list(AQ.ACTIVE_STATUSES)), [])
            report = RW.run_once_pass(conn, max_tasks=10, worker_id="w1")
            self.assertIn("exp-1", report["healed_experiments"])
            self.assertTrue(RW.snapshot(conn)["experiments"][0]["complete"])

    def test_the_snapshot_reports_residue_and_failures(self):
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            RL.ensure_schema(conn)
            conn.execute(
                "INSERT INTO paired_replay_receipts (experiment_id, target_identity,"
                " environment_hash, transition_sequence, evaluator_version, side,"
                " session_date, receipt_fingerprint, step, next_state, published_at)"
                " VALUES ('exp-1','t','h',1,'v','champion',?,?, '{}','{}','now')",
                (TRADING_DAYS[0], "a" * 64))
            snapshot = RW.snapshot(conn)
            experiment = snapshot["experiments"][0]
            self.assertEqual(experiment["sessions_published"], 0)
            self.assertIn(TRADING_DAYS[0], experiment["incomplete_receipt_sessions"])
            self.assertFalse(experiment["complete"])

    def test_the_cli_status_and_run_paths(self):
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(RW.main(["run", "--max-tasks", "10", "--path", self.path]), 0)
            self.assertIn("experiments", out.getvalue())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(RW.main(["status", "--path", self.path]), 0)
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["experiments"][0]["complete"])

    def test_the_snapshot_reports_latency_split_and_adoption_mode(self):
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
            RW.run_once_pass(conn, max_tasks=10, worker_id="w1")
            snapshot = RW.snapshot(conn)
            self.assertIn("stages", snapshot["latency"])
            self.assertIsNotNone(snapshot["latency"]["bottleneck"])
            self.assertTrue(snapshot["latency"]["hint"])
            # 没有凭据 → ADVISORY：只出建议，不自动采用。
            self.assertEqual(snapshot["capability"]["mode"], "ADVISORY")
            self.assertFalse(snapshot["capability"]["auto_applies"])

    def test_the_snapshot_reports_provider_readiness_without_leaking_keys(self):
        with AQ.connect(self.path) as conn:
            snapshot = RW.snapshot(conn)
            provider = snapshot["provider"]
            self.assertEqual(sorted(provider), ["ai1", "ai2"])
            for slot in provider.values():
                self.assertIn("ready", slot)
                # 只报 SET/MISSING 与模型名，绝不出现密钥本身。
                self.assertNotIn("api_key", slot)
                self.assertNotIn("sk-", json.dumps(slot, ensure_ascii=False))

    def test_the_cli_runs_as_a_real_subprocess(self):
        backend = os.path.dirname(os.path.abspath(RW.__file__))
        with AQ.connect(self.path) as conn:
            self._enqueue(conn)
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   ASTOCK_DATA_DIR=self._folder.name)
        done = subprocess.run(
            [sys.executable, "-m", "research_worker", "run", "--max-tasks", "10",
             "--path", self.path],
            cwd=backend, capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("experiments", done.stdout)
        status = subprocess.run(
            [sys.executable, "-m", "research_worker", "status", "--path", self.path],
            cwd=backend, capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertTrue(json.loads(status.stdout)["experiments"][0]["complete"])

    def test_the_host_cron_advances_the_persistent_research_queue(self):
        from pathlib import Path

        cron = (Path(RW.__file__).resolve().parents[1]
                / "deploy" / "astock-codex.cron").read_text(encoding="utf-8")
        self.assertIn("research_worker.py run --max-tasks 8", cron)
        self.assertIn("research-worker.lock", cron)

    def test_an_empty_workspace_is_a_controlled_no_op(self):
        fresh = f"{self._folder.name}/empty.sqlite3"
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(RW.main(["status", "--path", fresh]), 0)
            self.assertFalse(json.loads(out.getvalue())["initialized"])


class ProviderCheckTests(unittest.TestCase):
    """`provider-check` 是"配置是否真的生效"的唯一可信判据。"""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/research.sqlite3"
        self.config_path = f"{self._folder.name}/adaptive_learning.sqlite3"
        self._config_owner = mock.patch.multiple(
            AE, DB_PATH=self.config_path, CACHE_DIR=self._folder.name)
        self._config_owner.start()

    def tearDown(self):
        self._config_owner.stop()
        self._folder.cleanup()

    def _save_config(self, *, api_key="", base_url="", model="", enabled=True):
        """把槽位真的写进配置库；空 Key 走显式清空，避免环境变量悄悄兜底。"""
        import ai_review_service as AIR

        with AE._connect() as conn:
            AIR.update_slot(conn, "ai1", api_key=api_key or None,
                            base_url=base_url, model=model, enabled=enabled,
                            clear_api_key=not api_key)

    def _check(self, call, **config):
        """只替换 transport（那一次真实网络调用）；配置走真实配置库。"""
        import ai_provider_transport as transport

        self._save_config(**config)
        with mock.patch.object(transport, "call_json", side_effect=call):
            return RW.provider_check(slot="ai1")

    def test_an_unconfigured_slot_is_reported_without_calling_out(self):
        def must_not_call(*args, **kwargs):
            raise AssertionError("未配置的槽位不该发起调用")

        result = self._check(must_not_call)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "config")
        self.assertEqual(result["error"], "provider_not_configured")
        self.assertFalse(result["key_set"])

    def test_disabled_slot_does_not_call_out(self):
        result = self._check(
            lambda *args, **kwargs: self.fail("disabled slot called"),
            api_key="fixture-key", model="fixture-model",
            base_url="https://provider.example.invalid/v1", enabled=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "provider_disabled")
        self.assertFalse(result["enabled"])

    def test_a_working_slot_reports_latency_and_no_secret(self):
        result = self._check(
            lambda *args, **kwargs: ({"ok": True}, 9, 3, 812.5),
            api_key="sk-secret", model="deepseek-v4.1-flash",
            base_url="https://gw.example.com/v1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "called")
        self.assertEqual(result["model"], "deepseek-v4.1-flash")
        self.assertEqual(result["base_host"], "gw.example.com")
        self.assertEqual(result["input_tokens"], 9)
        self.assertEqual(result["output_tokens"], 3)
        self.assertAlmostEqual(result["latency_ms"], 812.5)
        self.assertNotIn("sk-secret", json.dumps(result, ensure_ascii=False))

    def test_a_failing_call_is_reported_as_a_call_failure(self):
        def boom(*args, **kwargs):
            raise TimeoutError("provider timed out")

        result = self._check(boom, api_key="fixture-key", model="fixture-model",
                             base_url="https://provider.example.invalid/v1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "call")
        self.assertIn("TimeoutError", result["error"])
        self.assertNotIn("fixture-key", json.dumps(result, ensure_ascii=False))

    def test_the_check_reads_the_configuration_owner_not_the_research_queue(self):
        """研究队列库不是配置来源：既不建配置表，也不遮蔽权威配置。"""
        import sqlite3

        with AQ.connect(self.path) as conn:
            AQ.commit(conn)
        result = self._check(lambda *args, **kwargs: ({"ok": True}, 1, 1, 5.0),
                             api_key="fixture-key", model="fixture-model",
                             base_url="https://provider.example.invalid/v1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "fixture-model")
        queue = sqlite3.connect(self.path)
        try:
            tables = {row[0] for row in queue.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            queue.close()
        self.assertNotIn("ai_provider_slots", tables)
        self.assertNotIn("ai_slot_provider_bindings", tables)

    def test_an_unbound_config_owner_reports_the_entry_hint_and_calls_nothing(self):
        """绕开入口直接调用时没有配置 owner：报未绑定 + 入口提示，不发请求。"""
        import ai_provider_transport as transport
        import research_runtime_config as RRC

        previous = RRC.config_connection_factory()
        try:
            RRC.bind_config_connection(None)
            with mock.patch.object(
                    transport, "call_json",
                    side_effect=AssertionError("未绑定时不该发起调用")):
                result = RW.provider_check(slot="ai1")
        finally:
            RRC.bind_config_connection(previous)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "config")
        self.assertIn(RRC.ConfigurationConnectionUnbound.__name__, result["error"])
        self.assertIn("bind_config_owner", result["hint"])

    def test_the_entry_points_bind_the_configuration_owner(self):
        """CLI 与宿主兜底线程都是组合根：两者都必须能把 owner 绑上。"""
        import research_runtime_config as RRC

        previous = RRC.config_connection_factory()
        try:
            RRC.bind_config_connection(None)
            self.assertFalse(RRC.config_connection_bound())
            self.assertIs(RW.bind_config_owner(), RW.bind_config_owner())
        finally:
            RRC.bind_config_connection(previous)
        self.assertTrue(RRC.config_connection_bound())

    def test_the_cli_reads_the_real_configuration_library(self):
        """`python -m research_worker provider-check` 走真实配置库；禁用槽位不发请求。"""
        import ai_review_service as AIR

        with AE._connect() as config:
            AIR.update_slot(config, "ai1", api_key="fixture-key",
                            base_url="https://provider.example.invalid/v1",
                            model="fixture-model", enabled=False)
        backend = os.path.dirname(os.path.abspath(RW.__file__))
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   ASTOCK_DATA_DIR=self._folder.name)
        done = subprocess.run(
            [sys.executable, "-m", "research_worker", "provider-check", "--slot", "ai1"],
            cwd=backend, capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(done.returncode, 2, done.stderr)
        payload = json.loads(done.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "provider_disabled")
        self.assertFalse(payload["enabled"])
        self.assertNotIn("fixture-key", done.stdout)


if __name__ == "__main__":
    unittest.main()
