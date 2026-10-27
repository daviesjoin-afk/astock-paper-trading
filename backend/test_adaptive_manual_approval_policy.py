"""Legacy auto-apply settings must not bypass explicit human approval."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import adaptive_configuration as configuration
import adaptive_engine as engine
import api_adaptive


class ManualApprovalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "adaptive.sqlite3"
        with self.connect() as conn:
            conn.execute("CREATE TABLE adaptive_config(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def test_fresh_defaults_are_off_and_stay_off_after_reopen(self):
        with self.connect() as conn:
            engine._ensure_config_defaults(conn)
            stored = dict(conn.execute("SELECT key, value FROM adaptive_config"))
            for key in configuration._AUTO_APPLY_KEYS:
                self.assertIs(json.loads(stored[key]), False)
                self.assertIs(configuration.read_config(conn)[key], False)
        with self.connect() as conn:
            for key in configuration._AUTO_APPLY_KEYS:
                self.assertIs(configuration.read_config(conn)[key], False)

    def test_legacy_true_values_remain_ineffective_after_reopen_and_defaults(self):
        with self.connect() as conn:
            for key in configuration._AUTO_APPLY_KEYS:
                conn.execute("INSERT INTO adaptive_config(key,value) VALUES(?,?)", (key, "true"))
            engine._ensure_config_defaults(conn)
            self.assertTrue(all(not configuration.read_config(conn)[key]
                                for key in configuration._AUTO_APPLY_KEYS))
        with self.connect() as conn:
            self.assertTrue(all(not configuration.read_config(conn)[key]
                                for key in configuration._AUTO_APPLY_KEYS))
            self.assertTrue(all(row[0] == "true" for row in conn.execute(
                "SELECT value FROM adaptive_config WHERE key IN (?,?,?)", configuration._AUTO_APPLY_KEYS)))

    def test_settings_reject_enabling_and_accept_explicit_off(self):
        with self.connect() as conn:
            engine._ensure_config_defaults(conn)
        with mock.patch.object(engine, "_connect", self.connect):
            with self.assertRaisesRegex(ValueError, "不允许自动应用"):
                engine.update_ai_settings({"llm_realtime_auto_apply": True})
            self.assertIs(engine.ai_settings()["llm_realtime_auto_apply"], False)
            self.assertIs(engine.update_ai_settings({"llm_realtime_auto_apply": False})
                          ["llm_realtime_auto_apply"], False)

    def test_http_settings_cannot_enable_legacy_auto_apply(self):
        with self.connect() as conn:
            engine._ensure_config_defaults(conn)
        app = FastAPI()
        app.include_router(api_adaptive.router)
        with mock.patch.object(engine, "_connect", self.connect), TestClient(app) as client:
            response = client.post("/api/adaptive/ai/settings?confirmed=true&llm_realtime_auto_apply=true")
            self.assertEqual(response.status_code, 422)
            response = client.post("/api/adaptive/ai/settings?confirmed=true&llm_realtime_auto_apply=false")
            self.assertEqual(response.status_code, 200)
            self.assertIs(response.json()["settings"]["llm_realtime_auto_apply"], False)


if __name__ == "__main__":
    unittest.main()
