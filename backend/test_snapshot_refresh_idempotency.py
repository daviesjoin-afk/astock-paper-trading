"""Automatic reads do not turn a stale display into an unbounded refresh queue."""
import contextlib
import datetime as dt
import io
import itertools
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import api_paper as API
import paper_job_lifecycle as PJL
import paper_schema_migrations as Schema
import paper_snapshot_jobs as Jobs
import paper_snapshot_worker as Worker
import paper_storage as Storage
import paper_trading as Paper


class SnapshotRequestIntentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(Paper, "DB_PATH", str(Path(tmp.name) / "ledger.db")))
        Paper.init_db()
        self.now = dt.datetime(2026, 10, 30, 10)
        self.stack.enter_context(mock.patch.object(Jobs, "_stamp", side_effect=self.stamp))
        self.stack.enter_context(mock.patch.object(Paper, "_risk_base_dashboard", return_value={"positions": []}))
        self.stack.enter_context(mock.patch.object(Paper.RC, "load_snapshot", return_value={"asof": "old source"}))
        self.stack.enter_context(mock.patch.object(Paper.RC, "snapshot_age_seconds", return_value=9999))
        self.stack.enter_context(mock.patch.object(Paper.RC, "build_dashboard", side_effect=lambda *_: {}))
        self.stack.enter_context(mock.patch.object(Paper.dfc, "load_source_health", return_value={}))
        # Exercise every GET as a real cache miss; idempotency must not depend on TTL/cache hits.
        generations = itertools.count()
        self.stack.enter_context(mock.patch.object(API, "_current_cache_generation", side_effect=lambda: str(next(generations))))
        API._cclear()
        self.addCleanup(API._cclear)
        self.provider = mock.Mock()

    def stamp(self, offset=0):
        return (self.now + dt.timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")

    def row(self):
        with Paper._db_readonly() as conn:
            return PJL.read_snapshot_refresh(conn)

    def refresh(self, publish):
        self.provider()
        publish(lambda: None)

    def run_worker(self, refresh=None):
        return Jobs.run_refresh_job(Paper._db, refresh or self.refresh)

    def get_many(self, count=50):
        for _ in range(count):
            API.risk_overview()

    def test_missing_snapshot_first_get_creates_one_persistent_request(self):
        with mock.patch.object(Paper.RC, "load_snapshot", return_value=None):
            self.get_many()
        self.assertEqual(self.row()["request_version"], 1)
        self.assertEqual(self.row()["status"], "queued")
        self.assertEqual(self.row()["attempt"], 0)

    def test_stale_snapshot_running_fifty_gets_do_not_create_followup(self):
        API.risk_overview()

        def collect(publish):
            self.get_many()
            self.assertEqual(self.row()["request_version"], 1)
            self.refresh(publish)

        self.assertEqual(self.run_worker(collect)["status"], "completed")
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.run_worker()["status"], "idle")
        self.assertEqual(self.row()["attempt"], 1)
        self.assertEqual(self.provider.call_count, 1)

    def test_manual_post_while_running_retains_exactly_one_followup(self):
        API.risk_overview()

        def collect(publish):
            for _ in range(5):
                API.risk_refresh()
            self.get_many()
            self.assertEqual(self.row()["request_version"], 2)
            self.assertEqual(self.row()["claimed_version"], 1)
            self.refresh(publish)

        self.assertEqual(self.run_worker(collect)["status"], "completed")
        self.assertEqual(self.row()["status"], "queued")
        self.assertEqual(self.run_worker()["status"], "completed")
        self.assertEqual(self.run_worker()["status"], "idle")
        self.assertEqual(self.row()["attempt"], 2)
        self.assertEqual(self.provider.call_count, 2)

    def test_manual_post_then_immediate_get_does_not_add_a_second_version(self):
        API.risk_refresh()
        self.assertEqual(self.row()["request_version"], 1)
        API.risk_overview()
        self.assertEqual(self.row()["request_version"], 1)
        self.run_worker()
        self.get_many()
        self.assertEqual(self.row()["request_version"], 1)
        self.assertEqual(self.provider.call_count, 1)

    def test_failed_backoff_is_unchanged_by_fifty_auto_gets(self):
        API.risk_overview()
        self.run_worker(mock.Mock(side_effect=RuntimeError("provider unavailable")))
        failed = self.row()
        self.get_many()
        self.assertEqual(self.row(), failed)
        self.assertEqual(self.run_worker()["status"], "idle")
        self.now += dt.timedelta(seconds=Jobs.RETRY_SECONDS)
        self.assertEqual(self.run_worker()["status"], "completed")

    def test_old_source_after_success_is_throttled_but_not_permanently_suppressed(self):
        API.risk_overview()
        self.run_worker()
        completed = self.row()
        self.get_many()
        self.assertEqual(self.row(), completed)
        self.now += dt.timedelta(seconds=PJL.SNAPSHOT_AUTO_REFRESH_SECONDS)
        self.get_many()
        self.assertEqual(self.row()["status"], "queued")
        self.assertEqual(self.row()["request_version"], 2)
        self.run_worker()
        self.get_many()
        self.assertEqual(self.row()["attempt"], 2)
        self.assertEqual(self.provider.call_count, 2)

    def test_missing_snapshot_can_be_reestablished_even_after_recent_completion(self):
        API.risk_overview()
        self.run_worker()
        with mock.patch.object(Paper.RC, "load_snapshot", return_value=None):
            self.get_many()
        self.assertEqual(self.row()["status"], "queued")
        self.assertEqual(self.row()["request_version"], 2)

    def slot(self, status, count=1):
        with mock.patch.object(Paper, "init_db"), mock.patch.object(Paper.PSS, "run_preflight"), \
                mock.patch.object(Paper, "_run_slot_impl", return_value={"status": status}):
            with contextlib.redirect_stderr(io.StringIO()):
                for _ in range(count):
                    Paper.run_slot("risk", asof_date="2026-10-30")

    def test_repeated_already_done_does_not_refresh_even_after_auto_cooldown(self):
        self.slot("completed")
        self.run_worker()
        completed = self.row()
        self.now += dt.timedelta(seconds=3600)
        self.slot("already_done", 50)
        self.assertEqual(self.row(), completed)
        self.assertEqual(self.run_worker()["status"], "idle")
        self.assertEqual(self.provider.call_count, 1)
        self.slot("completed")  # New completed trading work still requests a new snapshot.
        self.assertEqual(self.row()["request_version"], 2)

    def test_already_done_can_recover_missing_request_without_losing_manual_followup(self):
        self.slot("already_done", 50)
        self.assertEqual(self.row()["request_version"], 1)

        def collect(publish):
            API.risk_refresh()
            self.slot("already_done", 50)
            self.assertEqual(self.row()["request_version"], 2)
            self.refresh(publish)

        self.run_worker(collect)
        self.assertEqual(self.row()["status"], "queued")
        self.run_worker()
        self.assertEqual(self.provider.call_count, 2)


class SnapshotWorkerPrecheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ledger.db"
        patch = mock.patch.object(Storage, "paper_db_path", return_value=str(self.path))
        patch.start()
        self.addCleanup(patch.stop)
        with Storage.db(str(self.path)) as conn:
            Schema.ensure_dashboard_refresh_jobs(conn)
        self.now = dt.datetime.now()

    def stamp(self, offset=0):
        return (self.now + dt.timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")

    def enqueue(self):
        with Storage.db(str(self.path), immediate=True) as conn:
            PJL.enqueue_snapshot_refresh(conn, trigger="test", now=self.stamp(), completed_before=self.stamp(-300))

    def test_empty_and_backoff_and_live_lease_do_not_import_the_heavy_engine(self):
        with mock.patch.dict(sys.modules, {"paper_trading": None}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(Worker.main(), 0)
            self.enqueue()
            with Storage.db(str(self.path), immediate=True) as conn:
                job = PJL.claim_snapshot_refresh(conn, owner="live", now=self.stamp(), expires_at=self.stamp(3600))
            self.assertEqual(Worker.main(), 0)
            with Storage.db(str(self.path), immediate=True) as conn:
                PJL.fail_snapshot_refresh(conn, owner="live", fencing_token=job["fencing_token"], now=self.stamp(),
                                          retry_at=self.stamp(3600), error="retry later")
            self.assertEqual(Worker.main(), 0)

    def test_precheck_is_readonly_and_does_not_grant_a_claim(self):
        self.enqueue()
        original = Storage.paper_db_readonly
        observed = []

        @contextlib.contextmanager
        def readonly(**kwargs):
            with original(**kwargs) as conn:
                yield conn
                observed.append((conn.execute("PRAGMA query_only").fetchone()[0], conn.total_changes))

        with mock.patch.object(Storage, "paper_db_readonly", side_effect=readonly):
            self.assertTrue(Worker.refresh_work_due())
        self.assertEqual(observed, [(1, 0)])
        with original() as conn:
            self.assertEqual(PJL.read_snapshot_refresh(conn)["status"], "queued")

    def test_missing_schema_and_missing_database_take_the_original_initialization_path(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            with conn:
                conn.execute("DROP TABLE paper_dashboard_refresh_jobs")
        fake = types.SimpleNamespace(init_db=mock.Mock(), run_risk_snapshot_refresh_job=mock.Mock(return_value={"status": "idle"}))
        with mock.patch.dict(sys.modules, {"paper_trading": fake}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(Worker.main(), 0)
            fake.init_db.assert_called_once()
            with mock.patch.object(Storage, "paper_db_path", return_value=str(self.path.with_name("missing.db"))):
                self.assertEqual(Worker.main(), 0)
                self.assertFalse(self.path.with_name("missing.db").exists())
        self.assertEqual(fake.init_db.call_count, 2)

    def test_due_and_expired_lease_enter_original_cas_processing(self):
        self.enqueue()
        fake = types.SimpleNamespace(init_db=mock.Mock(), run_risk_snapshot_refresh_job=mock.Mock(return_value={"status": "completed"}))
        with mock.patch.dict(sys.modules, {"paper_trading": fake}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(Worker.main(), 0)
            with Storage.db(str(self.path), immediate=True) as conn:
                PJL.claim_snapshot_refresh(conn, owner="dead", now=self.stamp(-3600), expires_at=self.stamp(-120))
            self.assertTrue(Worker.refresh_work_due())
            self.assertEqual(Worker.main(), 0)
        self.assertEqual(fake.run_risk_snapshot_refresh_job.call_count, 2)

    def test_cold_empty_worker_does_not_import_pandas_or_paper_trading(self):
        backend = Path(__file__).resolve().parent
        code = """import builtins, sys
sys.path.insert(0, sys.argv[1])
import paper_storage as storage
storage.bind_ledger_path(lambda: sys.argv[2])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {'paper_trading', 'pandas', 'numpy'}:
        raise AssertionError('unnecessary heavy import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import paper_snapshot_worker
raise SystemExit(paper_snapshot_worker.main())
"""
        result = subprocess.run([sys.executable, "-c", code, str(backend), str(self.path)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"idle"', result.stdout)


if __name__ == "__main__":
    unittest.main()
