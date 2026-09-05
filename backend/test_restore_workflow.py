"""Exercise restore orchestration with real SQLite files and fake services."""
import gzip
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "requires Linux shell")
class RestoreWorkflowTests(unittest.TestCase):
    def run_case(self, corrupt=False, unhealthy=False):
        script = Path(__file__).resolve().parents[1] / "deploy" / "restore.sh"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, backup, tools = [root / name for name in ("app", "backup", "bin")]
            for path in (app / "data_cache", backup, tools):
                path.mkdir(parents=True)
            db = app / "data_cache" / "paper_trading.sqlite3"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE evidence(value)")
                conn.execute("INSERT INTO evidence VALUES (1)")
            original = db.read_bytes()
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE evidence SET value=2")
            packed = backup / (db.name + ".gz")
            packed.write_bytes(gzip.compress(db.read_bytes()))
            db.write_bytes(original)
            digest = "0" * 64 if corrupt else hashlib.sha256(packed.read_bytes()).hexdigest()
            (backup / "SHA256SUMS").write_text(digest + "  " + packed.name + "\n")
            wrappers = {
                "docker": '#!/bin/bash\necho "$*" >> "$TRACE"\nif [[ "$*" == *"ps --status"* ]]; then printf "astock\\nastock-paper-worker\\n"; fi\n',
                "curl": "#!/bin/bash\nexit " + ("1" if unhealthy else "0") + "\n",
                "sleep": "#!/bin/bash\nexit 0\n",
                "sqlite3": '#!/usr/bin/env python3\nimport sqlite3,sys\nwith sqlite3.connect(sys.argv[1]) as c: print(c.execute(sys.argv[2]).fetchone()[0])\n',
            }
            for name, content in wrappers.items():
                path = tools / name
                path.write_text(content)
                path.chmod(0o755)
            trace = root / "trace"
            env = dict(os.environ, APP_DIR=str(app), TRACE=str(trace),
                       PATH=str(tools) + os.pathsep + os.environ["PATH"])
            result = subprocess.run(["bash", str(script), "--confirm-restore", str(backup)],
                                    env=env, capture_output=True, text=True, timeout=20)
            if corrupt:
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(trace.exists(), "bad checksum must not touch services")
                self.assertEqual(db.read_bytes(), original)
            else:
                calls = trace.read_text()
                self.assertIn("stop astock astock-paper-worker", calls)
                self.assertIn("start astock astock-paper-worker", calls)
                with sqlite3.connect(db) as conn:
                    self.assertEqual(conn.execute("SELECT value FROM evidence").fetchone()[0],
                                     1 if unhealthy else 2)
                self.assertEqual(result.returncode == 0, not unhealthy, result.stderr)
                if unhealthy:
                    self.assertEqual(calls.count("stop astock astock-paper-worker"), 2)

    def test_bad_checksum_leaves_live_data_untouched(self):
        self.run_case(corrupt=True)

    def test_restore_stops_all_workers(self):
        self.run_case()

    def test_health_failure_restores_original_database(self):
        self.run_case(unhealthy=True)
