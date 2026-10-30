"""Durable display jobs: real SQLite, process-loss simulation and fenced file writes."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import paper_job_lifecycle as PJL
import paper_schema_migrations as PSM
import paper_snapshot_jobs as Jobs
import paper_storage as Storage
import risk_center as RC


class SnapshotJobsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "paper.sqlite3"
        with self.db(immediate=True) as conn:
            PSM.ensure_dashboard_refresh_jobs(conn)
        self.now = dt.datetime(2026, 10, 30, 10)
        patch = mock.patch.object(Jobs, "_stamp", side_effect=self.stamp)
        patch.start()
        self.addCleanup(patch.stop)
        self.snapshot = self.root / "snapshot.json"
        self.history = self.root / "history.json"

    def stamp(self, offset=0):
        return (self.now + dt.timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")

    def db(self, **kwargs):
        return Storage.db(str(self.path), **kwargs)

    def row(self):
        with self.db() as conn:
            return PJL.read_snapshot_refresh(conn)

    def request(self):
        return Jobs.request_refresh(self.db, trigger="test", request_kind="explicit")

    def publish_value(self, publish, value="new"):
        publish(lambda: self.snapshot.write_text(value, encoding="utf-8"))

    def test_schema_is_idempotent_and_queue_is_independent_of_trading_jobs(self):
        import db_migrate

        migrations = db_migrate.MIGRATIONS["paper_trading"]
        self.assertEqual([row[0] for row in migrations].count(41), 1)
        self.assertIs(next(row[2] for row in migrations if row[0] == 41), PSM.ensure_dashboard_refresh_jobs)
        with self.db(immediate=True) as conn:
            PSM.ensure_dashboard_refresh_jobs(conn)
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {"paper_dashboard_refresh_jobs"})

    def test_request_survives_connection_and_process_context_loss(self):
        self.assertEqual(self.request()["status"], "scheduled")
        # A fresh service invocation uses only persisted state, not module memory.
        result = Jobs.run_refresh_job(self.db, self.publish_value)
        self.assertEqual(result["status"], "completed")
        row = self.row()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["last_completed_at"], self.stamp())
        self.assertEqual(self.snapshot.read_text(encoding="utf-8"), "new")
        self.assertEqual(Jobs.run_refresh_job(self.db, mock.Mock())["status"], "idle")

    def test_queued_duplicates_coalesce_and_requests_during_work_get_one_followup(self):
        self.request()
        self.request()

        def refresh(publish):
            self.assertEqual(self.request()["status"], "already_running")
            self.request()
            self.publish_value(publish)

        self.assertEqual(Jobs.run_refresh_job(self.db, refresh)["status"], "completed")
        self.assertEqual(self.row()["status"], "queued")
        self.assertEqual(Jobs.run_refresh_job(self.db, self.publish_value)["status"], "completed")
        self.assertEqual(self.row()["attempt"], 2)
        self.assertEqual(Jobs.run_refresh_job(self.db, mock.Mock())["status"], "idle")

    def test_failure_keeps_old_file_and_retries_only_when_due(self):
        self.snapshot.write_text("last trusted", encoding="utf-8")
        self.request()
        result = Jobs.run_refresh_job(self.db, mock.Mock(side_effect=RuntimeError("provider unavailable")))
        self.assertEqual(result["status"], "failed")
        self.assertIn("provider unavailable", self.row()["last_error"])
        self.assertEqual(self.snapshot.read_text(encoding="utf-8"), "last trusted")
        self.request()  # New callers cannot bypass the retry backoff.
        refresh = mock.Mock()
        self.assertEqual(Jobs.run_refresh_job(self.db, refresh)["status"], "idle")
        refresh.assert_not_called()
        self.now += dt.timedelta(seconds=Jobs.RETRY_SECONDS)
        self.assertEqual(Jobs.run_refresh_job(self.db, self.publish_value)["status"], "completed")
        self.assertIsNone(self.row()["last_error"])

    def test_crashed_worker_is_reclaimed_after_lease_expiry(self):
        self.request()
        with self.db(immediate=True) as conn:
            old = PJL.claim_snapshot_refresh(conn, owner="dead", now=self.stamp(), expires_at=self.stamp(120))
        self.assertEqual(Jobs.run_refresh_job(self.db, mock.Mock())["status"], "idle")
        self.now += dt.timedelta(seconds=120)
        self.assertEqual(Jobs.run_refresh_job(self.db, self.publish_value)["status"], "completed")
        self.assertGreater(self.row()["fencing_token"], old["fencing_token"])
        self.assertEqual(self.row()["attempt"], 2)

    def test_expired_worker_cannot_renew_or_publish(self):
        self.request()
        with self.db(immediate=True) as conn:
            old = PJL.claim_snapshot_refresh(conn, owner="old", now=self.stamp(), expires_at=self.stamp(120))
        self.now += dt.timedelta(seconds=120)
        with self.db(immediate=True) as conn:
            self.assertFalse(PJL.renew_snapshot_refresh(
                conn, owner="old", fencing_token=old["fencing_token"], now=self.stamp(), expires_at=self.stamp(120),
            ))
            with self.assertRaises(PJL.SnapshotLeaseLost):
                PJL.assert_snapshot_refresh_lease(conn, owner="old", fencing_token=old["fencing_token"], now=self.stamp())

    def test_competing_workers_do_not_run_same_request(self):
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        self.request()

        def refresh(publish):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test worker timed out")
            self.publish_value(publish)

        worker = threading.Thread(target=lambda: outcomes.append(Jobs.run_refresh_job(self.db, refresh)))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(Jobs.run_refresh_job(self.db, mock.Mock())["status"], "idle")
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes, [{"status": "completed"}])

    def test_late_old_process_cannot_overwrite_new_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        self.request()

        def old_refresh(publish):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test worker timed out")
            self.publish_value(publish, "obsolete")

        worker = threading.Thread(target=lambda: outcomes.append(Jobs.run_refresh_job(self.db, old_refresh)))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.now += dt.timedelta(seconds=120)
            self.assertEqual(Jobs.run_refresh_job(self.db, self.publish_value)["status"], "completed")
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(outcomes[0]["status"], "lease_lost")
        self.assertEqual(self.snapshot.read_text(encoding="utf-8"), "new")
        self.assertEqual(self.row()["status"], "completed")

    def test_heartbeat_keeps_live_worker_owned(self):
        self.request()
        renewed = threading.Event()
        original = PJL.renew_snapshot_refresh

        def renew(*args, **kwargs):
            value = original(*args, **kwargs)
            renewed.set()
            return value

        def refresh(publish):
            self.assertTrue(renewed.wait(3))
            self.publish_value(publish)

        with mock.patch.object(Jobs, "HEARTBEAT_SECONDS", .01), \
                mock.patch.object(PJL, "renew_snapshot_refresh", side_effect=renew):
            self.assertEqual(Jobs.run_refresh_job(self.db, refresh)["status"], "completed")

    def test_no_network_or_build_runs_inside_writer_transaction(self):
        self.request()

        def refresh(publish):
            # A second writer must be able to acquire while collection/build runs.
            with self.db(immediate=True, hot_path=True) as conn:
                self.assertTrue(conn.in_transaction)
            self.publish_value(publish)

        self.assertEqual(Jobs.run_refresh_job(self.db, refresh)["status"], "completed")

    def test_snapshot_and_history_are_both_gated_by_publication(self):
        with mock.patch.object(RC, "SNAPSHOT_PATH", str(self.snapshot)), \
                mock.patch.object(RC, "HISTORY_PATH", str(self.history)):
            args = dict(market={}, positions=[], universe=[], snapshot_at=None,
                        news_events=[], news_error=None, sector_rows=[])
            with self.assertRaises(PJL.SnapshotLeaseLost):
                RC.refresh_snapshot(**args, publish=mock.Mock(side_effect=PJL.SnapshotLeaseLost("lost")))
            self.assertFalse(self.snapshot.exists())
            self.assertFalse(self.history.exists())
            self.request()
            self.assertEqual(Jobs.run_refresh_job(self.db, lambda publish: RC.refresh_snapshot(**args, publish=publish))["status"], "completed")
            self.assertIsInstance(json.loads(self.snapshot.read_text(encoding="utf-8")), dict)
            self.assertIsInstance(json.loads(self.history.read_text(encoding="utf-8")), list)


if __name__ == "__main__":
    unittest.main()
