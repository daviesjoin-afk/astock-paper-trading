"""Run the real cycle-resume shell with Docker replaced only at its boundary."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name == "posix" else None


@unittest.skipUnless(BASH, "requires POSIX bash")
class ResumeThreeStrategyCycleTests(unittest.TestCase):
    def run_script(self, strategies, status="paused"):
        temporary = tempfile.TemporaryDirectory(prefix="astock-resume-cycle-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        deploy = root / "deploy"
        deploy.mkdir()
        (root / ".start_three_strategy_cycle").write_text("pending\n", encoding="utf-8")
        trace = root / "trace.jsonl"
        shim = root / "shim"
        shim.mkdir()
        driver = shim / "docker.py"
        driver.write_text('''import json
import os
import pathlib
import sys
import types

args = sys.argv[1:]
assert args[:3] == ["exec", "astock-codex", "python"], args
assert args[3] == "-c", args
code = args[4]
class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _active_cycle(self):
        return {
            "id": 15,
            "status": os.environ["CYCLE_STATUS"],
            "enabled_strategies": os.environ["CYCLE_STRATEGIES"],
        }

conn = FakeConnection()
PT = types.ModuleType("paper_trading")
PT.init_db = lambda: None
PT._db = lambda: conn
PT._active_cycle = lambda current: current._active_cycle()
PT._loads = lambda value, default=None: json.loads(value) if value else default
PT.set_accounts_status = lambda status: pathlib.Path(os.environ["TRACE"]).write_text(status)
sys.modules["paper_trading"] = PT
exec(code, {"__name__": "__main__"})
''', encoding="utf-8")
        docker = shim / "docker"
        docker.write_text(
            f'#!/usr/bin/env bash\nexec "{Path(sys.executable)}" "{driver}" "$@"\n',
            encoding="utf-8", newline="\n")
        docker.chmod(0o755)
        env = {
            **os.environ,
            "ASTOCK_DEPLOY_ROOT": str(root),
            "PATH": str(shim) + os.pathsep + os.environ["PATH"],
            "CYCLE_STATUS": status,
            "CYCLE_STRATEGIES": __import__("json").dumps(strategies),
            "TRACE": str(trace),
            "PYTHONPATH": str(ROOT / "backend"),
        }
        result = subprocess.run(
            [BASH, str(ROOT / "deploy/resume_three_strategy_cycle.sh")],
            env=env, text=True, capture_output=True, timeout=30)
        return result, root, trace

    def test_only_the_exact_formal_roster_is_resumed(self):
        result, root, trace = self.run_script([
            "reported_profit_breakout", "tq_breakout", "trend_pullback"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("started=15", result.stdout)
        self.assertEqual(trace.read_text(encoding="utf-8"), "running")
        self.assertFalse((root / ".start_three_strategy_cycle").exists())

    def test_wrong_scope_never_resumes_and_keeps_the_marker(self):
        result, root, trace = self.run_script(["tq_breakout", "user_strategy"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("scope mismatch", result.stdout + result.stderr)
        self.assertFalse(trace.exists())
        self.assertTrue((root / ".start_three_strategy_cycle").exists())

    def test_matching_non_paused_cycle_only_clears_the_stale_marker(self):
        result, root, trace = self.run_script([
            "tq_breakout", "trend_pullback", "reported_profit_breakout"], status="running")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already=running", result.stdout)
        self.assertFalse(trace.exists())
        self.assertFalse((root / ".start_three_strategy_cycle").exists())


if __name__ == "__main__":
    unittest.main()
