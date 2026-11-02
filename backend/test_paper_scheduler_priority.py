import os
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("flock"),
                     "requires native POSIX flock")
class SchedulerPriorityTests(unittest.TestCase):
    def test_busy_fast_pass_yields_while_full_scan_waits_and_then_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locks = root / "locks"
            locks.mkdir()
            source = Path(__file__).resolve().parents[1] / "deploy" / "run_paper_slot.sh"
            runner = root / "runner.sh"
            runner.write_text(source.read_text().replace("/opt/astock-codex/.locks", str(locks)))
            calls = root / "docker-call"
            docker = root / "docker"
            docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > " + shlex.quote(str(calls)) + "\n")
            docker.chmod(0o755)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            cron = source.with_name("astock-codex.cron").read_text()
            command = next(line for line in cron.splitlines() if ".locks/intraday.lock" in line)
            command = command.split(" root ", 1)[1].split(" >>", 1)[0]
            command = command.replace("/opt/astock-codex/.locks", str(locks))
            command = command.replace("/opt/astock-codex/deploy/run_paper_slot.sh",
                                      "bash " + shlex.quote(str(runner)))
            holder = subprocess.Popen(
                ["flock", str(locks / "intraday.lock"), "flock", str(locks / "paper-scan.lock"),
                 "sh", "-c", "echo ready; read release"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            full = None
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                fast = subprocess.run(["bash", str(runner), "fast-entry"], env=env, timeout=2,
                                      capture_output=True, text=True)
                self.assertEqual(fast.returncode, 1)
                denied = json.loads(fast.stderr)
                self.assertEqual(denied["event"], "paper_host_lock")
                self.assertEqual(denied["status"], "not_acquired")
                # PR-11 观测：被锁挡住的那一轮必须能归因到具体 lane。
                self.assertEqual(denied["slot"], "fast-entry")
                self.assertFalse(calls.exists())
                full = subprocess.Popen(["bash", "-c", command], env=env)
                time.sleep(0.1)
                self.assertIsNone(full.poll())
                holder.communicate("release\n", timeout=2)
                self.assertEqual(full.wait(timeout=3), 0)
                invocation = calls.read_text().strip()
                self.assertRegex(invocation,
                                 r"^exec --env ASTOCK_PAPER_HOST_LOCK_WAIT_SECONDS=\d+ "
                                 r"--env ASTOCK_SCHEDULER_TICK_ID=intraday-\d+-\d+ "
                                 r"--env ASTOCK_SCAN_BUDGET_SECONDS=\d+ "
                                 r"--env ASTOCK_SCAN_TAIL_AFTER_SECONDS=\d+ "
                                 r"--env ASTOCK_TRANSPORT_BREAKER_FILE=\S+ "
                                 r"--env ASTOCK_MICRO_BATCH_SIZE=\d+ "
                                 r"astock-candidate-worker python backend/paper_runner.py --slot intraday$")
            finally:
                for process in (holder, full):
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait()

    def test_fast_pass_yields_when_full_scan_is_waiting(self):
        """全扫描已经留下意图标记时，fast-entry 直接跳过而不是抢锁。

        生产上 15 次 ``not_acquired`` 全部发生在这种竞争下：fast-entry 先拿到
        ``paper-scan.lock``，整点/半点的全扫描等 20 秒后整轮放弃。快通道下一
        分钟还会再跑，全扫描丢了就是丢了，因此优先级必须反过来。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locks = root / "locks"
            locks.mkdir()
            source = Path(__file__).resolve().parents[1] / "deploy" / "run_paper_slot.sh"
            runner = root / "runner.sh"
            runner.write_text(source.read_text().replace("/opt/astock-codex/.locks", str(locks)))
            (locks / "full-scan-pending.4321").write_text("")
            calls = root / "docker-call"
            docker = root / "docker"
            docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > " + shlex.quote(str(calls)) + "\n")
            docker.chmod(0o755)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            fast = subprocess.run(["bash", str(runner), "fast-entry"], env=env,
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(fast.returncode, 0)
            self.assertEqual(json.loads(fast.stderr)["status"], "yielded_to_full_scan")
            self.assertFalse(calls.exists(), "yielded fast pass must not invoke docker")

    def test_fast_pass_ignores_stale_full_scan_intent(self):
        """残留的旧标记（全扫描被 SIGKILL）不能永久饿死快通道。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locks = root / "locks"
            locks.mkdir()
            source = Path(__file__).resolve().parents[1] / "deploy" / "run_paper_slot.sh"
            runner = root / "runner.sh"
            runner.write_text(source.read_text().replace("/opt/astock-codex/.locks", str(locks)))
            stale = locks / "full-scan-pending.9999"
            stale.write_text("")
            old = time.time() - 3600
            os.utime(stale, (old, old))
            calls = root / "docker-call"
            docker = root / "docker"
            docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > " + shlex.quote(str(calls)) + "\n")
            docker.chmod(0o755)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            fast = subprocess.run(["bash", str(runner), "fast-entry"], env=env,
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(fast.returncode, 0)
            self.assertTrue(calls.exists())
            self.assertIn("--slot fast-entry", calls.read_text())
            self.assertFalse(stale.exists(), "stale intent must be cleaned up")

    def test_full_scan_clears_its_pending_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locks = root / "locks"
            locks.mkdir()
            source = Path(__file__).resolve().parents[1] / "deploy" / "run_paper_slot.sh"
            runner = root / "runner.sh"
            runner.write_text(source.read_text().replace("/opt/astock-codex/.locks", str(locks)))
            docker = root / "docker"
            docker.write_text("#!/bin/sh\nexit 0\n")
            docker.chmod(0o755)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            run = subprocess.run(["bash", str(runner), "intraday"], env=env,
                                 capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 0)
            self.assertEqual(list(locks.glob("full-scan-pending.*")), [])

    def test_host_lock_stays_held_until_docker_command_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locks = root / "locks"
            locks.mkdir()
            source = Path(__file__).resolve().parents[1] / "deploy" / "run_paper_slot.sh"
            runner = root / "runner.sh"
            runner.write_text(source.read_text().replace("/opt/astock-codex/.locks", str(locks)))
            docker = root / "docker"
            docker.write_text("#!/bin/sh\necho ready\nread release\nexit 7\n")
            docker.chmod(0o755)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            first = subprocess.Popen(["bash", str(runner), "intraday"], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(first.stdout.readline().strip(), "ready")
                fast = subprocess.run(["bash", str(runner), "fast-entry"], env=env,
                                      capture_output=True, text=True, timeout=2)
                self.assertEqual(fast.returncode, 1)
                self.assertEqual(json.loads(fast.stderr)["status"], "not_acquired")
                first.communicate("release\n", timeout=2)
                self.assertEqual(first.returncode, 7)
                check = subprocess.run(["flock", "-n", str(locks / "paper-scan.lock"), "true"],
                                       capture_output=True, timeout=2)
                self.assertEqual(check.returncode, 0)
            finally:
                if first.poll() is None:
                    first.kill()
                    first.wait()

    def test_runner_entry_and_exit_events_carry_slot_and_tick_id(self):
        """进入 runner 前发 start、返回后发 exit：这是"是否真的跑起来"的唯一证据。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locks = root / "locks"
            locks.mkdir()
            source = Path(__file__).resolve().parents[1] / "deploy" / "run_paper_slot.sh"
            runner = root / "runner.sh"
            runner.write_text(source.read_text().replace("/opt/astock-codex/.locks", str(locks)))
            docker = root / "docker"
            docker.write_text("#!/bin/sh\nexit 0\n")
            docker.chmod(0o755)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            run = subprocess.run(["bash", str(runner), "intraday"], env=env,
                                 capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 0)
            events = [json.loads(line) for line in run.stderr.splitlines() if line.strip()]
        self.assertEqual(["paper_slot_start", "paper_slot_exit"],
                         [event["event"] for event in events])
        start, exited = events
        self.assertEqual("intraday", start["slot"])
        self.assertEqual(0, start["wait_seconds"])
        self.assertEqual(start["tick_id"], exited["tick_id"])
        self.assertRegex(start["tick_id"], r"^intraday-\d+-\d+$")
        self.assertEqual(0, exited["exit_code"])

    def test_runner_entry_event_is_absent_when_the_lock_is_refused(self):
        """没抢到锁就没有 start 事件：否则"启动次数"会把被挡住的轮次算进去。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locks = root / "locks"
            locks.mkdir()
            source = Path(__file__).resolve().parents[1] / "deploy" / "run_paper_slot.sh"
            runner = root / "runner.sh"
            runner.write_text(source.read_text().replace("/opt/astock-codex/.locks", str(locks)))
            holder = subprocess.Popen(
                ["flock", str(locks / "paper-scan.lock"), "sh", "-c", "echo ready; read release"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                denied = subprocess.run(["bash", str(runner), "fast-entry"], timeout=2,
                                        capture_output=True, text=True)
            finally:
                holder.communicate("release\n", timeout=2)
                if holder.poll() is None:
                    holder.kill()
                    holder.wait()
        self.assertEqual(1, denied.returncode)
        events = [json.loads(line) for line in denied.stderr.splitlines() if line.strip()]
        self.assertEqual(["paper_host_lock"], [event["event"] for event in events])

    # ── PR-11 B：风险快通道的独立主机锁 ────────────────────────────────────
    def test_each_slot_uses_the_contract_container_route(self):
        import paper_schedule_contract as schedule
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "locks").mkdir()
            runner = self._risk_runner(root)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            for slot in schedule.RUNNER_SLOTS:
                with self.subTest(slot=slot):
                    result = subprocess.run(["bash", str(runner), slot], env=env,
                                            capture_output=True, text=True, timeout=3)
                    self.assertEqual(0, result.returncode, result.stderr)
                    call = (root / "docker-call").read_text()
                    self.assertIn(f"{schedule.worker_container_name(slot)} python backend/paper_runner.py --slot {slot}", call)

    def test_execution_lane_runs_while_candidate_and_risk_locks_are_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "locks").mkdir()
            runner = self._risk_runner(root)
            (root / "locks" / "full-scan-pending.1234").touch()
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            holder = subprocess.Popen(
                ["flock", str(root / "locks" / "paper-scan.lock"), "flock",
                 str(root / "locks" / "risk-scan.lock"), "sh", "-c", "echo ready; read release"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual("ready", holder.stdout.readline().strip())
                result = subprocess.run(["bash", str(runner), "execution-resume"],
                                        env=env, text=True, capture_output=True, timeout=3)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("--slot execution-resume", (root / "docker-call").read_text())
                self.assertEqual(1, len(list((root / "locks").glob("full-scan-pending.*"))))
            finally:
                holder.communicate("release\n", timeout=3)

    def test_execution_lane_refuses_its_own_busy_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "locks").mkdir()
            runner = self._risk_runner(root)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            holder = subprocess.Popen(
                ["flock", str(root / "locks" / "execution-scan.lock"), "sh", "-c", "echo ready; read release"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual("ready", holder.stdout.readline().strip())
                result = subprocess.run(["bash", str(runner), "execution-resume"],
                                        env=env, text=True, capture_output=True, timeout=3)
                self.assertEqual(1, result.returncode)
                event = json.loads(result.stderr)
                self.assertEqual("execution-resume", event["slot"])
                self.assertEqual("not_acquired", event["status"])
                self.assertFalse((root / "docker-call").exists())
            finally:
                holder.communicate("release\n", timeout=3)

    def _risk_runner(self, root):
        source = Path(__file__).resolve().parents[1] / "deploy" / "run_paper_slot.sh"
        runner = root / "runner.sh"
        runner.write_text(source.read_text().replace("/opt/astock-codex/.locks", str(root / "locks")))
        docker = root / "docker"
        docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > " + shlex.quote(str(root / "docker-call")) + "\n")
        docker.chmod(0o755)
        return runner

    def test_risk_lane_runs_while_the_shared_scan_lock_is_held(self):
        """全扫描占着 paper-scan.lock 时，风险 lane 仍然必须能跑起来。

        这正是 PR-11 B 的独立边界：如果风险任务还在等那把共享锁，它只是一条
        多出来的 cron，节拍依旧被候选扫描拖住。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "locks").mkdir()
            runner = self._risk_runner(root)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            holder = subprocess.Popen(
                ["flock", str(root / "locks" / "paper-scan.lock"),
                 "sh", "-c", "echo ready; read release"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                run = subprocess.run(["bash", str(runner), "risk"], env=env, timeout=5,
                                     capture_output=True, text=True)
            finally:
                holder.communicate("release\n", timeout=2)
                if holder.poll() is None:
                    holder.kill()
                    holder.wait()
            # 断言必须在临时目录还存在时取证据：目录在 with 结束时已被删除。
            docker_called = (root / "docker-call").exists()
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertTrue(docker_called, "risk 被共享锁挡住了")
        events = [json.loads(line) for line in run.stderr.splitlines() if line.strip()]
        self.assertEqual(["paper_slot_start", "paper_slot_exit"],
                         [event["event"] for event in events])
        self.assertEqual("risk", events[0]["slot"])

    def test_risk_lane_is_serialized_by_its_own_lock(self):
        """两轮风险重叠时跳过这一跳（flock -n），而不是排队等自己。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "locks").mkdir()
            runner = self._risk_runner(root)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            holder = subprocess.Popen(
                ["flock", str(root / "locks" / "risk-scan.lock"),
                 "sh", "-c", "echo ready; read release"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                denied = subprocess.run(["bash", str(runner), "risk"], env=env, timeout=5,
                                        capture_output=True, text=True)
            finally:
                holder.communicate("release\n", timeout=2)
                if holder.poll() is None:
                    holder.kill()
                    holder.wait()
            docker_called = (root / "docker-call").exists()
        self.assertEqual(1, denied.returncode)
        event = json.loads(denied.stderr)
        self.assertEqual("paper_host_lock", event["event"])
        self.assertEqual("not_acquired", event["status"])
        self.assertEqual("risk", event["slot"])
        self.assertFalse(docker_called)

    def test_risk_lane_never_leaves_a_full_scan_intent_marker(self):
        """意图标记是共用 paper-scan.lock 的 lane 的协议；风险 lane 不参与。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "locks").mkdir()
            runner = self._risk_runner(root)
            docker = root / "docker"
            docker.write_text("#!/bin/sh\nexit 0\n")
            docker.chmod(0o755)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            run = subprocess.run(["bash", str(runner), "risk"], env=env, timeout=5,
                                 capture_output=True, text=True)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertEqual(list((root / "locks").glob("full-scan-pending.*")), [])
