"""Exercise the real backup shell and embedded SQLite code against an active WAL.

Docker is replaced only at the process boundary; actual SQL, gzip and checksums run.
The mount assertion covers the permission contract; real container mounts still need
Linux deployment verification.
"""
import gzip
import os
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = ((str(Path(os.environ["ProgramFiles"]) / "Git/bin/bash.exe")
         if os.environ.get("ProgramFiles") else None)
        if os.name == "nt" else shutil.which("bash"))


@unittest.skipUnless(BASH and Path(BASH).is_file(), "bash unavailable")
class BackupScriptTests(unittest.TestCase):
    def test_live_wal_backup_can_be_decompressed_and_restored(self):
        with tempfile.TemporaryDirectory(prefix="astock-backup-") as temporary:
            root = Path(temporary)
            source = root / "app/data_cache"
            source.mkdir(parents=True)
            db = source / "paper_trading.sqlite3"
            live = sqlite3.connect(db)
            self.addCleanup(live.close)
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("PRAGMA wal_autocheckpoint=0")
            live.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT)")
            live.executemany("INSERT INTO evidence(value) VALUES (?)", [(f"row-{n}",) for n in range(50)])
            live.commit()
            self.assertTrue(Path(str(db) + "-wal").is_file())
            expected = live.execute("SELECT * FROM evidence ORDER BY id").fetchall()
            shim = root / "shim"
            shim.mkdir()
            driver = shim / "docker.py"
            driver.write_text('''import json, pathlib, subprocess, sys
args = sys.argv[1:]
if args[:2] == ["image", "inspect"]:
    sys.exit(0)
assert args[0] == "run", args
mounts = [args[n+1] for n, arg in enumerate(args) if arg == "-v"]
source = next(value for value in mounts if ":/backup-src" in value)
assert not source.endswith(":ro"), "WAL source directory must support -shm"
target = next(value for value in mounts if ":/backup-dst" in value)
code = args[args.index("-c")+1]
assert "?mode=ro" in code, "source SQL connection must remain read-only"
code = code.replace("/backup-src/", source.split(":/backup-src")[0] + "/")
code = code.replace("/backup-dst/", target.split(":/backup-dst")[0] + "/")
sys.exit(subprocess.run([sys.executable, "-c", code, *args[-2:]]).returncode)
''', encoding="utf-8")
            docker = shim / "docker"
            docker.write_text(f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "{driver.as_posix()}" "$@"\n',
                              encoding="utf-8", newline="\n")
            docker.chmod(0o755)
            env = {**os.environ, "APP_DIR": source.parent.as_posix(),
                   "BACKUP_ROOT": (root / "backups").as_posix(),
                   "PATH": str(shim) + os.pathsep + os.environ["PATH"],
                   "MSYS_NO_PATHCONV": "1"}
            result = subprocess.run([BASH, str(ROOT / "deploy/backup.sh")],
                                    env=env, text=True, capture_output=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("engine=container sqlite=", result.stdout)
            self.assertNotIn("falling back", result.stderr)
            backups = list((root / "backups/daily").glob("*/paper_trading.sqlite3.gz"))
            self.assertEqual(len(backups), 1)
            restored = root / "restored.sqlite3"
            restored.write_bytes(gzip.decompress(backups[0].read_bytes()))
            with closing(sqlite3.connect(restored)) as copy:
                self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(copy.execute("SELECT * FROM evidence ORDER BY id").fetchall(), expected)
            self.assertEqual(live.execute("SELECT * FROM evidence ORDER BY id").fetchall(), expected)
            live.close()
