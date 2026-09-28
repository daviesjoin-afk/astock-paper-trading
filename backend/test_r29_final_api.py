"""R29 canonical API read/write boundaries."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_engine as adaptive
import api_adaptive


class R29ValidationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "adaptive.sqlite3")
        self.patch = mock.patch.object(adaptive, "DB_PATH", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_GET_missing_history_is_empty_and_does_not_create_database(self):
        result = api_adaptive.canonical_experiment_runs()
        self.assertEqual({"status": "ok", "runs": []}, result)
        self.assertFalse(os.path.exists(self.path))

    def test_GET_detail_missing_identity_is_404_without_creating_database(self):
        with self.assertRaises(HTTPException) as raised:
            api_adaptive.canonical_experiment_run("a" * 64)
        self.assertEqual(404, raised.exception.status_code)
        self.assertFalse(os.path.exists(self.path))

    def test_three_canonical_routes_are_registered(self):
        app = FastAPI()
        app.include_router(api_adaptive.router)
        paths = app.openapi()["paths"]
        self.assertIn("post", paths["/api/adaptive/experiments/validate"])
        self.assertIn("get", paths["/api/adaptive/experiments/runs"])
        self.assertIn("get", paths["/api/adaptive/experiments/runs/{run_id}"])

    def test_api_uses_the_canonical_docker_build_identity(self):
        with mock.patch.dict(os.environ, {
            "ASTOCK_GIT_COMMIT": "a" * 40,
            "ASTOCK_BUILD_REVISION": "b" * 40,
        }, clear=True):
            self.assertEqual("a" * 40, api_adaptive._canonical_build_revision())
