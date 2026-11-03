"""P11-E: real queue age boundaries, safe migration, and observable admission."""
import contextlib
import datetime as dt
import io
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import paper_dispatch_metrics as Metrics
import paper_job_lifecycle as Owner
import paper_schema_migrations as Schema
import paper_snapshot_jobs as Jobs
import paper_snapshot_worker as Worker
import paper_storage as Storage

V41 = """CREATE TABLE paper_dashboard_refresh_jobs (
 id INTEGER PRIMARY KEY CHECK(id=1),
 status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
 trigger TEXT NOT NULL, requested_at TEXT NOT NULL,
 request_version INTEGER NOT NULL DEFAULT 1, claimed_version INTEGER NOT NULL DEFAULT 0,
 attempt INTEGER NOT NULL DEFAULT 0, owner_key TEXT, fencing_token INTEGER NOT NULL DEFAULT 0,
 started_at TEXT, heartbeat_at TEXT, expires_at TEXT, finished_at TEXT,
 next_retry_at TEXT, last_error TEXT, last_completed_at TEXT)"""


class DashboardLaneTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "ledger.db"
        self.now = dt.datetime(2026, 11, 2, 10)
        patch = mock.patch.object(Jobs, "_stamp", side_effect=self.stamp)
        patch.start()
        self.addCleanup(patch.stop)
        with self.db() as conn:
            Schema.ensure_dashboard_refresh_jobs(conn)
        self.snapshot = self.root / "snapshot.json"
        self.snapshot.write_text("previous", encoding="utf-8")

    def stamp(self, offset=0):
        return (self.now + dt.timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")

    def db(self, **kwargs):
        return Storage.db(str(self.path), **kwargs)

    def row(self):
        with self.db() as conn:
            return Owner.read_snapshot_refresh(conn)

    def request(self, kind="explicit"):
        return Jobs.request_refresh(self.db, trigger="test", request_kind=kind)

    def publish(self, publish):
        publish(lambda: self.snapshot.write_text("current", encoding="utf-8"))

    def test_exact_age_limit_runs_but_one_second_older_does_not_collect(self):
        self.request()
        self.now += dt.timedelta(seconds=300)
        self.assertEqual(Jobs.run_refresh_job(self.db, self.publish)["status"], "completed")
        completed_at = self.row()["last_completed_at"]
        self.request()
        self.now += dt.timedelta(seconds=301)
        provider = mock.Mock()
        result = Jobs.run_refresh_job(self.db, provider)
        provider.assert_not_called()
        self.assertEqual((result["status"], result["reason"], result["queue_age_seconds"]),
                         ("skipped", "queue_age_exceeded", 301))
        row = self.row()
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["last_completed_at"], completed_at)
        self.assertEqual(row["attempt"], 1)
        self.assertEqual(self.snapshot.read_text(encoding="utf-8"), "current")
        self.assertEqual(row["skip_count"], 1)
        self.assertEqual(Jobs.run_refresh_job(self.db, provider)["reason"], "request_expired")
        self.assertEqual(self.row(), row)  # repeated sweeps do not inflate durable skip count

    def test_every_normal_request_kind_can_recover_expired_work_once(self):
        for kind in ("automatic", "explicit", "ensure"):
            with self.subTest(kind=kind):
                self.request()
                self.now += dt.timedelta(seconds=301)
                Jobs.run_refresh_job(self.db, mock.Mock())
                count = self.row()["skip_count"]
                self.assertEqual(self.request(kind)["status"], "scheduled")
                self.assertEqual(Jobs.run_refresh_job(self.db, self.publish)["status"], "completed")
                self.assertEqual(self.row()["skip_count"], count)

    def test_old_request_cannot_revoke_a_live_lease_or_bypass_backoff(self):
        self.request()
        with self.db(immediate=True) as conn:
            job = Owner.claim_snapshot_refresh(conn, owner="live", now=self.stamp(), expires_at=self.stamp(600))
        self.now += dt.timedelta(seconds=301)
        provider = mock.Mock()
        self.assertEqual(Jobs.run_refresh_job(self.db, provider)["reason"], "already_running")
        self.assertEqual(self.row()["owner_key"], "live")
        with self.db(immediate=True) as conn:
            Owner.fail_snapshot_refresh(conn, owner="live", fencing_token=job["fencing_token"],
                                        now=self.stamp(), retry_at=self.stamp(60), error="upstream")
        self.assertEqual(Jobs.run_refresh_job(self.db, provider)["reason"], "retry_backoff")
        provider.assert_not_called()
        self.now += dt.timedelta(seconds=60)
        # The retry is due again. Its wait is the 60s backoff that just elapsed,
        # not the 361s since the original request, so it must run rather than be
        # expired; the provider mock publishes nothing, so the attempt fails.
        self.assertEqual(Jobs.run_refresh_job(self.db, provider)["status"], "failed")
        provider.assert_called_once()

    def slow_failed_attempt(self, seconds, owner="slow"):
        """Claim, renew the lease like the heartbeat thread, then fail.

        Simulating the real worker matters: a 250s provider run only fails
        cleanly because heartbeats keep the 120s lease alive. Expiring the lease
        instead would test ``lease_lost``, not the queue-age rule.
        """
        with self.db(immediate=True) as conn:
            job = Owner.claim_snapshot_refresh(conn, owner=owner, now=self.stamp(), expires_at=self.stamp(120))
        remaining = seconds
        while remaining:
            step = min(60, remaining)
            self.now += dt.timedelta(seconds=step)
            remaining -= step
            if remaining:  # still working: renew before the lease can lapse
                with self.db(immediate=True) as conn:
                    self.assertTrue(Owner.renew_snapshot_refresh(
                        conn, owner=owner, fencing_token=job["fencing_token"],
                        now=self.stamp(), expires_at=self.stamp(120)))
        with self.db(immediate=True) as conn:
            self.assertTrue(Owner.fail_snapshot_refresh(
                conn, owner=owner, fencing_token=job["fencing_token"],
                now=self.stamp(), retry_at=self.stamp(60), error="provider down"))

    def failed_with_follow_up(self, run_seconds, follow_up_age):
        """A failed run carrying an unclaimed follow-up of a given age.

        The follow-up's own request time is what matters, so it is set
        explicitly rather than raced against the 60s renewal granularity of
        ``slow_failed_attempt``. ``fail_snapshot_refresh`` leaves
        ``request_version > claimed_version``, which is exactly the state the
        row is in between a failed original and its retry.
        """
        self.request()
        self.slow_failed_attempt(run_seconds)
        with self.db(immediate=True) as conn:
            conn.execute("UPDATE paper_dashboard_refresh_jobs SET"
                         " request_version=claimed_version+1, requested_at=?", (self.stamp(-follow_up_age),))
        row = self.row()
        self.assertEqual(row["status"], "failed")
        self.assertGreater(row["request_version"], row["claimed_version"])

    def test_provider_run_time_is_not_charged_to_the_retry_wait(self):
        # 10:00 request -> 250s provider run -> 60s backoff -> retry at 10:05:10.
        # Only the backoff was queue waiting; the 250s of work was not.
        self.request()
        self.slow_failed_attempt(250)
        self.assertEqual(self.row()["next_retry_at"], self.stamp(60))
        self.now += dt.timedelta(seconds=60)
        state = Jobs.refresh_status(self.db)
        self.assertEqual((state["status"], state["queue_age_seconds"]), ("failed", 60))
        self.assertEqual(Jobs.run_refresh_job(self.db, self.publish)["status"], "completed")

    def test_repeated_slow_failures_keep_retrying_instead_of_expiring(self):
        # Each wait is capped at 300s; a long run is never mistaken for one.
        self.request()
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                self.slow_failed_attempt(250)
                self.now += dt.timedelta(seconds=60)
                self.assertLess(Jobs.refresh_status(self.db)["queue_age_seconds"], 300)
        self.assertEqual(Jobs.run_refresh_job(self.db, self.publish)["status"], "completed")
        self.assertEqual(self.row()["skip_count"], 0)

    def test_a_follow_up_still_ages_from_its_own_request_after_the_original_fails(self):
        # 10:00 original claim -> follow-up queued 1s in -> 10:04:10 original
        # fails after 250s -> 10:05:10 retry. The follow-up itself has waited
        # 309s, so it must expire; a failure must not restart its clock.
        self.failed_with_follow_up(250, follow_up_age=309)
        self.now += dt.timedelta(seconds=60)  # retry tick: 60s backoff elapsed
        provider = mock.Mock()
        result = Jobs.run_refresh_job(self.db, provider)
        provider.assert_not_called()
        # 369s of follow-up waiting, not the 60s since the failure.
        self.assertEqual((result["status"], result["reason"], result["queue_age_seconds"]),
                         ("skipped", "queue_age_exceeded", 369.0))
        expired = self.row()
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(expired["skip_count"], 1)

    def test_a_follow_up_under_the_cap_still_runs_after_the_original_fails(self):
        # Same shape, but the follow-up is young enough to stay inside 300s.
        self.failed_with_follow_up(250, follow_up_age=60)
        self.now += dt.timedelta(seconds=60)
        self.assertEqual(Jobs.refresh_status(self.db)["queue_age_seconds"], 120)
        self.assertEqual(Jobs.run_refresh_job(self.db, self.publish)["status"], "completed")
        self.assertEqual(self.row()["skip_count"], 0)

    def test_a_retry_that_never_gets_admitted_still_expires(self):
        # Fail closed the other way: once the provider has failed, the retry
        # wait is capped too. A tick that can never claim the resource lock must
        # not leave a stale request reviving days later.
        self.request()
        self.slow_failed_attempt(250)
        self.now += dt.timedelta(seconds=301)  # 301s past the failure, retry never ran
        provider = mock.Mock()
        result = Jobs.run_refresh_job(self.db, provider)
        provider.assert_not_called()
        self.assertEqual((result["status"], result["reason"], result["queue_age_seconds"]),
                         ("skipped", "queue_age_exceeded", 301))
        self.assertEqual(self.row()["status"], "expired")

    def test_expired_dead_worker_cannot_publish_after_a_fresh_request(self):
        self.request()
        with self.db(immediate=True) as conn:
            old = Owner.claim_snapshot_refresh(conn, owner="dead", now=self.stamp(), expires_at=self.stamp(120))
        self.now += dt.timedelta(seconds=301)
        self.assertEqual(Jobs.run_refresh_job(self.db, mock.Mock())["status"], "skipped")
        self.assertIsNone(self.row()["owner_key"])
        self.request("automatic")
        Jobs.run_refresh_job(self.db, self.publish)
        with self.db(immediate=True) as conn:
            with self.assertRaises(Owner.SnapshotLeaseLost):
                Owner.assert_snapshot_refresh_lease(conn, owner="dead", fencing_token=old["fencing_token"], now=self.stamp())
        self.assertEqual(self.snapshot.read_text(encoding="utf-8"), "current")

    def test_followup_age_uses_its_request_time_and_does_not_rejuvenate_on_duplicates(self):
        self.request()
        def first(publish):
            self.now += dt.timedelta(seconds=60)
            self.request()
            self.now += dt.timedelta(seconds=20)
            self.request()
            self.publish(publish)
        Jobs.run_refresh_job(self.db, first)
        self.assertEqual(self.row()["requested_at"], self.stamp(-20))
        self.now += dt.timedelta(seconds=280)  # original request is 360s old; followup exactly 300
        self.assertEqual(Jobs.run_refresh_job(self.db, self.publish)["status"], "completed")

    def test_bad_or_future_requested_time_never_reaches_provider(self):
        for value in ("bad timestamp", self.stamp(10)):
            with self.subTest(value=value):
                self.request()
                with self.db(immediate=True) as conn:
                    conn.execute("UPDATE paper_dashboard_refresh_jobs SET requested_at=?", (value,))
                provider = mock.Mock()
                self.assertEqual(Jobs.run_refresh_job(self.db, provider)["reason"], "invalid_requested_at")
                provider.assert_not_called()

    def test_running_collection_time_is_not_reported_as_waiting_age(self):
        self.request()
        self.now += dt.timedelta(seconds=20)
        with self.db(immediate=True) as conn:
            Owner.claim_snapshot_refresh(conn, owner="live", now=self.stamp(), expires_at=self.stamp(120))
        self.now += dt.timedelta(seconds=30)
        state = Jobs.refresh_status(self.db)
        self.assertEqual((state["reason"], state["queue_age_seconds"]), ("already_running", 20))
        self.request()
        self.now += dt.timedelta(seconds=10)
        self.assertEqual(Jobs.refresh_status(self.db)["queue_age_seconds"], 10)

    def test_readonly_status_exposes_age_and_policy_and_durable_expiration(self):
        self.request()
        self.now += dt.timedelta(seconds=301)
        Jobs.run_refresh_job(self.db, mock.Mock())
        @contextlib.contextmanager
        def readonly():
            with Storage.paper_db_readonly(db_path=str(self.path)) as conn:
                yield conn
                self.assertEqual(conn.total_changes, 0)
        state = Jobs.refresh_status(readonly)
        self.assertEqual((state["status"], state["running"], state["max_concurrency"]), ("expired", False, 1))
        self.assertEqual(state["max_queue_age_seconds"], 300)
        self.assertEqual((state["skip_count"], state["last_skip_reason"], state["last_queue_age_seconds"]),
                         (1, "queue_age_exceeded", 301))

    def test_owner_expiration_does_not_commit_the_callers_transaction(self):
        self.request()
        self.now += dt.timedelta(seconds=301)
        with self.db(immediate=True) as conn:
            self.assertTrue(Owner.expire_snapshot_refresh(conn, now=self.stamp(), reason="queue_age_exceeded", queue_age_seconds=301))
            self.assertTrue(conn.in_transaction)
            conn.rollback()
        self.assertEqual((self.row()["status"], self.row()["skip_count"]), ("queued", 0))

    def test_cli_expiration_emits_reason_and_metrics_does_not_import_engine(self):
        self.request()
        self.now += dt.timedelta(seconds=301)
        fake = type("Paper", (), {"init_db": mock.Mock(), "run_risk_snapshot_refresh_job":
                                  staticmethod(lambda: Jobs.run_refresh_job(self.db, mock.Mock()))})
        with mock.patch.object(Storage, "paper_db_path", return_value=str(self.path)), \
                mock.patch.dict(sys.modules, {"paper_trading": fake}), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(Worker.main(), 0)
        event = json.loads(out.getvalue())
        self.assertEqual((event["event"], event["status"], event["reason"]),
                         ("dashboard_refresh", "skipped", "queue_age_exceeded"))
        log = self.root / "snapshot-refresh.log"
        log.write_text(out.getvalue(), encoding="utf-8")
        with mock.patch.object(Storage, "paper_db_readonly", side_effect=lambda: self.db()), \
                mock.patch.object(Worker.data_paths, "report_dir", return_value=str(self.root)), \
                mock.patch.dict(sys.modules, {"paper_trading": None}), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(Worker.main(metrics=True), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["events"]["dashboard_refresh"]["skips_by_reason"], {"queue_age_exceeded": 1})
        self.assertEqual(result["queue"]["status"], "expired")

    def test_display_events_are_separate_from_trading_counts_and_invalid_ages(self):
        reasons = ["resource_busy", "already_running", "retry_backoff", "queue_age_exceeded"]
        events = [{"event": "dashboard_refresh", "status": "skipped", "reason": reason,
                   "queue_age_seconds": age} for reason, age in zip(reasons, [None, -1, float("nan"), 301], strict=True)]
        summary = Metrics.summarize(events)
        self.assertEqual(summary["dashboard_refresh"]["skips_by_reason"], dict.fromkeys(reasons, 1))
        self.assertEqual(summary["dashboard_refresh"]["queue_age_seconds"], {"samples": 1, "max": 301})
        self.assertEqual(summary["ticks"]["skipped"], 0)
        self.assertEqual(summary["runs"], {})
        self.assertIsNone(summary["last_successful_risk_scan"]["at"])

    def test_resource_failure_keeps_raw_evidence_for_the_ci_artifact(self):
        source = Path(__file__).resolve().parents[1] / "scripts/pr11_lane_acceptance.py"
        spec = importlib.util.spec_from_file_location("lane_probe", source)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        output = self.root / "failed-probe.json"
        def reject(image, evidence):
            evidence.update(phase="assertions", baseline={"raw": [6.1]}, contended={"raw": [8.2]})
            raise AssertionError("relative interval gate")
        with mock.patch.object(probe, "host_probe", side_effect=reject), \
                mock.patch.object(sys, "argv", ["probe", "--output", str(output)]), \
                self.assertRaisesRegex(AssertionError, "relative interval gate"):
            probe.main()
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {
            "ok": False, "phase": "assertions", "baseline": {"raw": [6.1]}, "contended": {"raw": [8.2]},
        })


class DashboardMigrationTests(unittest.TestCase):
    def test_v41_upgrade_preserves_every_active_field_and_caller_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v41.db")
            with Storage.db(path) as conn:
                conn.execute(V41)
                conn.execute("""INSERT INTO paper_dashboard_refresh_jobs VALUES
                 (1,'running','manual','2026-11-02 10:01:00',3,2,7,'live',9,
                  '2026-11-02 10:00:00','2026-11-02 10:01:00','2026-11-02 10:03:00',
                  NULL,NULL,'prior error','2026-11-02 09:00:00')""")
            with Storage.db(path, immediate=True) as conn:
                before = Owner.read_snapshot_refresh(conn)
                Schema.ensure_dashboard_refresh_jobs(conn)
                after = Owner.read_snapshot_refresh(conn)
                self.assertEqual({key: after[key] for key in before}, before)
                self.assertEqual(after["skip_count"], 0)
                Schema.ensure_dashboard_refresh_jobs(conn)
                self.assertEqual(Owner.read_snapshot_refresh(conn), after)
                self.assertTrue(conn.in_transaction)
                conn.rollback()
            with Storage.db(path, immediate=True) as conn:
                self.assertEqual(Owner.read_snapshot_refresh(conn), before)
                Schema.ensure_dashboard_refresh_jobs(conn)
            with Storage.db(path) as conn:
                after = Owner.read_snapshot_refresh(conn)
                self.assertEqual({key: after[key] for key in before}, before)
                conn.execute("UPDATE paper_dashboard_refresh_jobs SET status='expired'")


@unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("flock"), "requires POSIX flock")
class DashboardHostAdmissionTests(unittest.TestCase):
    def test_busy_resource_lock_logs_skip_and_trading_locks_do_not_block_display(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            marker = temp / "invocation"
            docker = temp / "docker"
            docker.write_text("#!/bin/sh\nprintf '%s' \"$*\" > " + shlex.quote(str(marker)) + "\n")
            docker.chmod(0o755)
            env = {**os.environ, "PATH": str(temp) + os.pathsep + os.environ["PATH"], "ASTOCK_LOCK_DIR": str(temp)}
            script = root / "deploy/run_snapshot_refresh.sh"
            for lock, allowed in (("resource-heavy.lock", False), ("paper-scan.lock", True),
                                  ("risk-scan.lock", True), ("execution-scan.lock", True)):
                with self.subTest(lock=lock):
                    holder = subprocess.Popen(["flock", str(temp / lock), "sh", "-c", "echo ready; read release"],
                                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                    try:
                        self.assertEqual(holder.stdout.readline().strip(), "ready")
                        result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=3)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        if allowed:
                            self.assertEqual(marker.read_text(), "exec astock-data-worker python backend/paper_snapshot_worker.py")
                        else:
                            self.assertEqual(json.loads(result.stdout)["reason"], "resource_busy")
                            self.assertFalse(marker.exists())
                    finally:
                        holder.communicate("release\n", timeout=3)
                    marker.unlink(missing_ok=True)
        cron = (root / "deploy/astock-codex.cron").read_text(encoding="utf-8")
        self.assertIn("* * * * * root bash /opt/astock-codex/deploy/run_snapshot_refresh.sh", cron)

    def test_non_default_deploy_root_shares_the_rendered_heavy_lock(self):
        """A deployment outside /opt must contend for the same lock as cron.

        sync-cron.sh rewrites /opt/astock-codex to the real root, so the script
        has to derive that root the same way instead of defaulting to /opt: two
        different lock files would let a dashboard refresh and a selection or
        history run enter the same data-worker cgroup together.
        """
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            root = temp / "srv" / "codex"  # deliberately not /opt/astock-codex
            (root / "deploy").mkdir(parents=True)
            for name in ("run_snapshot_refresh.sh", "sync-cron.sh", "astock-codex.cron"):
                shutil.copy2(repo / "deploy" / name, root / "deploy" / name)
            marker = temp / "invocation"
            docker = temp / "docker"
            docker.write_text("#!/bin/sh\nprintf '%s' \"$*\" > " + shlex.quote(str(marker)) + "\n")
            docker.chmod(0o755)
            env = {**os.environ, "PATH": str(temp) + os.pathsep + os.environ["PATH"],
                   "ASTOCK_DEPLOY_ROOT": str(root), "ASTOCK_CRON_DIR": str(temp / "cron.d"),
                   "ASTOCK_CRON_BACKUP_DIR": str(temp / "cron-backup")}
            env.pop("ASTOCK_LOCK_DIR", None)  # must not rely on the operator setting it
            (temp / "cron.d").mkdir()
            rendered = subprocess.run(["bash", str(root / "deploy" / "sync-cron.sh")],
                                      env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            cron = (temp / "cron.d" / "astock-codex").read_text(encoding="utf-8")
            self.assertIn(f"bash {root}/deploy/run_snapshot_refresh.sh", cron)
            self.assertIn(f"flock -n {root}/.locks/resource-heavy.lock", cron)
            self.assertNotIn("/opt/astock-codex/.locks", cron)

            # The rendered heavy tasks and this script now resolve one lock file.
            holder = subprocess.Popen(["flock", str(root / ".locks" / "resource-heavy.lock"),
                                       "sh", "-c", "echo ready; read release"],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                result = subprocess.run(["bash", str(root / "deploy" / "run_snapshot_refresh.sh")],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["reason"], "resource_busy")
                self.assertFalse(marker.exists())
            finally:
                holder.communicate("release\n", timeout=3)
            # And the same script does run once that shared lock is free.
            allowed = subprocess.run(["bash", str(root / "deploy" / "run_snapshot_refresh.sh")],
                                     env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual(marker.read_text(), "exec astock-data-worker python backend/paper_snapshot_worker.py")


if __name__ == "__main__":
    unittest.main()
