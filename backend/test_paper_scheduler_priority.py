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
                self.assertFalse(calls.exists())
                full = subprocess.Popen(["bash", "-c", command], env=env)
                time.sleep(0.1)
                self.assertIsNone(full.poll())
                holder.communicate("release\n", timeout=2)
                self.assertEqual(full.wait(timeout=3), 0)
                invocation = calls.read_text().strip()
                self.assertRegex(invocation,
                                 r"^exec --env ASTOCK_PAPER_HOST_LOCK_WAIT_SECONDS=\d+ "
                                 r"astock-paper-worker python backend/paper_runner.py --slot intraday$")
            finally:
                for process in (holder, full):
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait()

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
