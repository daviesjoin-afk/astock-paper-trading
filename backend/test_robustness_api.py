"""R30 API offline and read-only boundaries (R30-68 through R30-74)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_engine as adaptive
import api_adaptive as API
import robustness_repository as RREP


class RobustnessApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "adaptive.sqlite3")
        patcher = mock.patch.object(adaptive, "DB_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_R30_68_missing_GETs_do_not_create_database(self):
        self.assertEqual({"status": "ok", "reports": []}, API.canonical_robustness_reports(1))
        with self.assertRaises(HTTPException) as raised:
            API.canonical_robustness_report(1)
        self.assertEqual(404, raised.exception.status_code)
        self.assertFalse(os.path.exists(self.path))

    def test_R30_69_GET_does_not_invoke_robustness_runner(self):
        with mock.patch("robustness_runner.run_robustness", side_effect=AssertionError("executed")) as run:
            self.assertEqual({"status": "ok", "reports": []},
                             API.canonical_robustness_reports(1))
        run.assert_not_called()

    def test_R30_70_POST_route_has_no_provider_or_network_dependency(self):
        source = Path(os.path.join(BACKEND, "robustness_runner.py")).read_text(encoding="utf-8")
        for forbidden in ("import requests", "import httpx", "data_fetcher", "marketdata_transport"):
            self.assertNotIn(forbidden, source)
        app = FastAPI()
        app.include_router(API.router)
        self.assertIn("post", app.openapi()["paths"]["/api/adaptive/experiments/runs/{run_id}/robustness"])

    def test_R30_71_POST_requires_explicit_complete_request_body(self):
        app = FastAPI()
        app.include_router(API.router)
        operation = app.openapi()["paths"]["/api/adaptive/experiments/runs/{run_id}/robustness"]["post"]
        self.assertTrue(operation["requestBody"]["required"])
        schema_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        schema_name = schema_ref.rsplit("/", 1)[-1]
        properties = app.openapi()["components"]["schemas"][schema_name]["properties"]
        self.assertTrue({"spec", "plan", "owner_identities"}.issubset(properties))

    def test_R30_72_owner_mismatch_is_rejected_by_canonical_runner(self):
        with self.assertRaisesRegex(ValueError, "owner_identity_mismatch"):
            API._robustness_owner_identities(
                API.RobustnessRequest(spec={}, plan={},
                    owner_identities={"market_archive_fingerprint": "x"},
                    benchmark_symbol="000001.SH"),
                {"market_archive_fingerprint": "y"})
        self.assertTrue(issubclass(RREP.RobustnessPersistenceError, ValueError))

    def test_R30_73_list_limit_contract_is_capped(self):
        app = FastAPI()
        app.include_router(API.router)
        parameter = next(p for p in app.openapi()["paths"][
            "/api/adaptive/experiments/runs/{run_id}/robustness"]["get"]["parameters"]
            if p["name"] == "limit")
        self.assertEqual(200, parameter["schema"]["maximum"])

    def test_R30_74_errors_do_not_include_database_details(self):
        self.assertIn("robustness_history_unavailable", Path(
            os.path.join(BACKEND, "api_adaptive.py")).read_text(encoding="utf-8"))
        self.assertNotIn(os.path.abspath(self.path), API.canonical_robustness_reports(1).__repr__())


if __name__ == "__main__":
    unittest.main()
