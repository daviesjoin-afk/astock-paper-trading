# -*- coding: utf-8 -*-
"""AI 授权凭据签发接口的验证（S4 的"凭据从哪来"这一段）。

路由鉴权（token / Origin / CSRF）由 ``test_operator_boundary`` 统一覆盖——它从
``main.app.openapi()`` 枚举写路由，所以本文件只需要验证业务校验与状态机。
"""
import asyncio
import contextlib
import json
import sqlite3
import tempfile
import unittest

from fastapi import HTTPException

import ai_capability as CAP
import ai_capability_store as STORE
import api_settings as API

NOW = "2026-10-26T02:00:00+00:00"
FUTURE = "2026-11-26T02:00:00+00:00"


def credential_body(**overrides):
    body = {
        "capability_id": "cap-1", "mode": CAP.MODE_BOUNDED_AUTO,
        "strategy_ids": ["tq_breakout"], "allowed_target_kinds": ["entry_policy"],
        "allowed_fields": ["first_board_bonus"], "bounds_version": "bounds-v1",
        "evidence_policy_id": "evidence-v1", "max_change_per_apply": 0.05,
        "max_change_per_day": 0.10, "loss_stop_policy_id": "loss-v1",
        "max_trials_per_day": 3, "daily_cost_cap": 5.0,
        "max_tool_steps_per_task": 12, "expires_at": FUTURE,
        "revocation_epoch": 1, "issued_by": "operator:alice",
    }
    body.update(overrides)
    return body


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def call(coro):
    return asyncio.run(coro)


class ValidationTests(unittest.TestCase):
    def test_issuing_requires_explicit_confirmation(self):
        with self.assertRaises(HTTPException) as caught:
            call(API.issue_ai_capability(_FakeRequest(credential_body()), confirmed=False))
        self.assertEqual(caught.exception.status_code, 409)

    def test_unknown_fields_are_refused(self):
        body = credential_body()
        body["max_leverage"] = 3
        with self.assertRaises(HTTPException) as caught:
            call(API.issue_ai_capability(_FakeRequest(body), confirmed=True))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("max_leverage", caught.exception.detail)

    def test_missing_required_fields_are_refused(self):
        body = credential_body()
        del body["daily_cost_cap"]
        with self.assertRaises(HTTPException) as caught:
            call(API.issue_ai_capability(_FakeRequest(body), confirmed=True))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("daily_cost_cap", caught.exception.detail)

    def test_a_bogus_mode_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            call(API.issue_ai_capability(
                _FakeRequest(credential_body(mode="FULL_AUTO")), confirmed=True))
        self.assertEqual(caught.exception.status_code, 422)

    def test_revoking_requires_confirmation_and_an_operator(self):
        with self.assertRaises(HTTPException) as caught:
            call(API.revoke_ai_capability("cap-1", _FakeRequest({}), confirmed=False))
        self.assertEqual(caught.exception.status_code, 409)
        with self.assertRaises(HTTPException) as caught:
            call(API.revoke_ai_capability("cap-1", _FakeRequest({}), confirmed=True))
        self.assertEqual(caught.exception.status_code, 422)


class LifecycleTests(unittest.TestCase):
    """用一张临时库跑完整旅程：签发 → 列表 → 撤销。"""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/paper.sqlite3"
        self._original = API.PST.paper_db

        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE placeholder(id INTEGER)")
        conn.commit()
        conn.close()

        @contextlib.contextmanager
        def _temp_db(immediate=False):
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

        API.PST.paper_db = _temp_db
        self._cleanup = lambda: setattr(API.PST, "paper_db", self._original)

    def tearDown(self):
        self._cleanup()
        self._folder.cleanup()

    def test_issue_then_list_then_revoke(self):
        issued = call(API.issue_ai_capability(_FakeRequest(credential_body()),
                                              confirmed=True))
        self.assertEqual(issued["capability_id"], "cap-1")
        self.assertTrue(issued["auto_applies"])

        listing = API.ai_capabilities_snapshot()
        self.assertEqual(len(listing["capabilities"]), 1)
        self.assertEqual(listing["capabilities"][0]["revocation_epoch"], 1)
        # 当前生效的授权仍是"没有凭据"：签发不等于启用。
        self.assertEqual(listing["current"]["mode"], CAP.MODE_ADVISORY)
        self.assertFalse(listing["current"]["auto_applies"])

        revoked = call(API.revoke_ai_capability(
            "cap-1", _FakeRequest({"operator": "operator:bob", "reason": "drift"}),
            confirmed=True))
        self.assertEqual(revoked["revocation_epoch"], 2)

        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            events = STORE.events(conn, "cap-1")
        finally:
            conn.close()
        self.assertEqual([row["event"] for row in events],
                         [STORE.EVENT_ISSUED, STORE.EVENT_REVOKED])

    def test_reissuing_the_same_capability_with_new_numbers_is_a_conflict(self):
        call(API.issue_ai_capability(_FakeRequest(credential_body()), confirmed=True))
        with self.assertRaises(HTTPException) as caught:
            call(API.issue_ai_capability(
                _FakeRequest(credential_body(max_change_per_day=99.0)), confirmed=True))
        self.assertEqual(caught.exception.status_code, 409)

    def test_revoking_an_unknown_capability_is_404(self):
        with self.assertRaises(HTTPException) as caught:
            call(API.revoke_ai_capability(
                "ghost", _FakeRequest({"operator": "operator:alice"}), confirmed=True))
        self.assertEqual(caught.exception.status_code, 404)

    def test_the_listing_never_contains_secret_material(self):
        call(API.issue_ai_capability(_FakeRequest(credential_body()), confirmed=True))
        payload = json.dumps(API.ai_capabilities_snapshot(), ensure_ascii=False)
        self.assertNotIn("api_key", payload)
        self.assertNotIn("secret", payload)


if __name__ == "__main__":
    unittest.main()
