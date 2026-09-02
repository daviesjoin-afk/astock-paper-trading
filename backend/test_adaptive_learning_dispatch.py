# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import adaptive_learning_dispatch as D


class AdaptiveLearningDispatchTests(unittest.TestCase):
    def paths(self, folder):
        root = Path(folder)
        jobs = root / "jobs"
        return mock.patch.multiple(
            D, CACHE_DIR=root, JOB_DIR=jobs, PENDING_DIR=jobs / "pending",
            RUNNING_DIR=jobs / "running", TERMINAL_DIR=jobs / "terminal",
            STATUS_PATH=jobs / "status.json", LOCK_PATH=jobs / ".lock",
        )

    def test_single_claim_and_finish(self):
        with tempfile.TemporaryDirectory() as folder, self.paths(folder):
            accepted, state = D.enqueue("manual-ui")
            self.assertTrue(accepted); self.assertEqual(state["status"], "queued")
            duplicate, _ = D.enqueue("manual-ui")
            self.assertFalse(duplicate)
            request = D.claim()
            self.assertEqual(request["trigger"], "manual-ui")
            self.assertTrue(D.read_status()["running"])
            D.finish(request, "completed")
            self.assertEqual(D.read_status()["status"], "completed")

    def test_running_request_is_interrupted_after_worker_restart(self):
        with tempfile.TemporaryDirectory() as folder, self.paths(folder):
            D.enqueue("manual-ui"); request = D.claim()
            self.assertIsNotNone(request)
            D.recover_orphaned()
            self.assertEqual(D.read_status()["status"], "interrupted")
            self.assertIsNone(D.claim())


if __name__ == "__main__":
    unittest.main()
