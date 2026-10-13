import os
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
                fast = subprocess.run(["bash", str(runner), "fast-entry"], env=env, timeout=2)
                self.assertEqual(fast.returncode, 1)
                self.assertFalse(calls.exists())
                full = subprocess.Popen(["bash", "-c", command], env=env)
                time.sleep(0.1)
                self.assertIsNone(full.poll())
                holder.communicate("release\n", timeout=2)
                self.assertEqual(full.wait(timeout=3), 0)
                self.assertEqual(calls.read_text().strip(),
                                 "exec astock-paper-worker python backend/paper_runner.py --slot intraday")
            finally:
                for process in (holder, full):
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait()
