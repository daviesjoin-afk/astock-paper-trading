# -*- coding: utf-8 -*-
"""Regression coverage for risk-audit filtering and cursor pagination.

The browser asked for ``page=1/2`` with ``page_size=10`` and received the same
160-row payload twice, because the projection returns a dict (``accounts`` /
``alerts`` / ``scan_status`` / ``asof`` / ``lightweight``) while the API only
sliced lists.  These tests pin the contract that filtering, paging and totals
now happen in the ledger query itself.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

sys.modules.setdefault("requests", mock.MagicMock())

import paper_trading as PT  # noqa: E402


class RiskAuditPaginationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-risk-audit-test-")
        cls._old_db = PT.DB_PATH
        PT.DB_PATH = os.path.join(cls._tmp, "paper_trading.sqlite3")
        PT.init_db()

    @classmethod
    def tearDownClass(cls):
        PT.DB_PATH = cls._old_db
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        # Each case counts rows and asserts totals, so it must start empty.
        self._clear()

    def _seed(self, rows):
        """Insert risk decisions directly; (account, code, decision, day).

        Accounts must exist: ``_strategy_stamp`` resolves the immutable version
        binding and refuses to fabricate one for an unknown account.
        """
        with PT._db() as conn:
            for index, (account_id, code, decision, day) in enumerate(rows):
                stamp = PT._strategy_stamp(conn, account_id)
                conn.execute(
                    """INSERT INTO paper_risk_decisions(
                           account_id,code,side,decision,reason,payload,created_at,
                           strategy_id,strategy_version,strategy_checksum)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (account_id, code, "buy", decision, f"reason-{index}",
                     json.dumps({"name": f"标的{index}"}, ensure_ascii=False),
                     f"{day} 10:00:00", *stamp),
                )

    def _clear(self):
        with PT._db() as conn:
            conn.execute("DELETE FROM paper_risk_decisions")

    def test_page_two_returns_a_different_slice_than_page_one(self):
        rows = [("sector_rotation", f"60010{i}", "entry_approved", "2026-10-15")
                for i in range(25)]
        self._seed(rows)

        first = PT.risk_audit(limit=300, page_size=10)
        second = PT.risk_audit(limit=300, page_size=10,
                               cursor=first["pagination"]["next_cursor"])

        first_ids = [row["id"] for row in first["alerts"]]
        second_ids = [row["id"] for row in second["alerts"]]
        self.assertEqual(len(first_ids), 10)
        self.assertEqual(len(second_ids), 10)
        self.assertFalse(set(first_ids) & set(second_ids),
                         "page 1 and page 2 must not overlap")
        self.assertGreater(min(first_ids), max(second_ids),
                           "pages must advance strictly backwards by id")
        self.assertEqual(first["pagination"]["total"], 25)
        self.assertTrue(first["pagination"]["has_next"])
        self.assertTrue(second["pagination"]["has_prev"])

    def test_cursor_walk_covers_every_row_exactly_once(self):
        rows = [("sector_rotation", f"60011{i}", "entry_approved", "2026-10-15")
                for i in range(23)]
        self._seed(rows)

        seen = []
        cursor = None
        for _ in range(10):
            page = PT.risk_audit(limit=300, page_size=7, cursor=cursor)
            seen.extend(row["id"] for row in page["alerts"])
            cursor = page["pagination"].get("next_cursor")
            if not cursor:
                break

        self.assertEqual(len(seen), 23, "cursor walk must return every row")
        self.assertEqual(len(set(seen)), 23, "cursor walk must not repeat a row")
        self.assertEqual(seen, sorted(seen, reverse=True), "walk must stay ordered")

    def test_date_filter_reaches_the_query(self):
        self._seed([
            ("sector_rotation", "600001", "entry_approved", "2026-10-14"),
            ("sector_rotation", "600002", "entry_approved", "2026-10-15"),
            ("trend_pullback", "600003", "entry_approved", "2026-10-15"),
        ])

        result = PT.risk_audit(limit=300, date="2026-10-15")
        self.assertEqual({row["code"] for row in result["alerts"]},
                         {"600002", "600003"})
        self.assertEqual(result["pagination"]["total"], 2)

        outside = PT.risk_audit(limit=300, date="2026-10-01")
        self.assertEqual(outside["alerts"], [])
        self.assertEqual(outside["pagination"]["total"], 0)

    def test_backdated_insert_does_not_break_id_ordered_cursor(self):
        self._seed([
            ("sector_rotation", "600001", "entry_approved", "2026-10-15"),
            ("sector_rotation", "600002", "entry_approved", "2026-10-13"),
            ("sector_rotation", "600003", "entry_approved", "2026-10-14"),
        ])
        seen, cursor = [], None
        for _ in range(4):
            result = PT.risk_audit(page_size=1, cursor=cursor)
            seen.extend(row["code"] for row in result["alerts"])
            cursor = result["pagination"].get("next_cursor")
            if not cursor:
                break
        self.assertEqual(["600003", "600002", "600001"], seen)

    def test_filtered_total_is_stable_across_cursor_pages(self):
        self._seed([("sector_rotation", f"6001{i:02d}", "filled", "2026-10-15")
                    for i in range(5)])
        first = PT.risk_audit(page_size=2, level="tightened")
        second = PT.risk_audit(page_size=2, level="tightened",
                               cursor=first["pagination"]["next_cursor"])
        self.assertEqual(5, second["pagination"]["total"])
        self.assertEqual(3, second["pagination"]["total_pages"])

    def test_exactly_full_last_page_has_no_next_cursor(self):
        self._seed([("sector_rotation", f"6001{i:02d}", "filled", "2026-10-15")
                    for i in range(4)])
        first = PT.risk_audit(page_size=2)
        second = PT.risk_audit(page_size=2, cursor=first["pagination"]["next_cursor"])
        self.assertEqual(2, len(second["alerts"]))
        self.assertFalse(second["pagination"]["has_next"])
        self.assertIsNone(second["pagination"].get("next_cursor"))

    def test_concurrent_new_row_does_not_repeat_on_next_page(self):
        self._seed([("sector_rotation", f"6001{i:02d}", "filled", "2026-10-15")
                    for i in range(4)])
        first = PT.risk_audit(page_size=2)
        self._seed([("sector_rotation", "600999", "filled", "2026-10-12")])
        second = PT.risk_audit(page_size=2, cursor=first["pagination"]["next_cursor"])
        ids = [a["id"] for page in (first, second) for a in page["alerts"]]
        self.assertEqual(4, len(set(ids)))
        self.assertNotIn("600999", [a["code"] for a in second["alerts"]])

    def test_invalid_date_and_out_of_range_cursor_are_rejected(self):
        for day in ("2026-02-30", "2026-13-01", "not-a-date"):
            with self.subTest(date=day), self.assertRaises(ValueError):
                PT.risk_audit(date=day)
        for cursor in ("0", str(2**63), "9" * 60):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                PT.risk_audit(cursor=cursor)

    def test_filtered_query_uses_covering_index_before_reading_payloads(self):
        self._seed([("sector_rotation", f"6001{i:02d}", "filled", "2026-10-15")
                    for i in range(20)])
        statements = []
        reader = PT._db_readonly
        @contextmanager
        def traced_reader():
            with reader() as conn:
                conn.set_trace_callback(statements.append)
                yield conn
        with mock.patch.object(PT, "_db_readonly", traced_reader):
            result = PT.risk_audit(date="2026-09-01", page_size=10)
            first = PT.risk_audit(date="2026-10-15", page_size=3)
            second = PT.risk_audit(date="2026-10-15", page_size=3,
                                   cursor=first["pagination"]["next_cursor"])
        self.assertEqual([], result["alerts"])
        self.assertEqual(3, len(second["alerts"]))
        queries = [sql for sql in statements
                   if "payload" in sql and "FROM paper_risk_decisions" in sql]
        self.assertEqual(3, len(queries))
        with reader() as conn:
            for query in queries:
                plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query)]
                self.assertTrue(any("USING COVERING INDEX" in step for step in plan), plan)
                self.assertTrue(any("USING INTEGER PRIMARY KEY" in step for step in plan), plan)
                # Only the outer page fetch may touch the rowid b-tree.
                materialized = plan.index("MATERIALIZE selected")
                self.assertIn("USING COVERING INDEX", plan[materialized + 1], plan)

    def test_filtered_query_remains_correct_without_optional_indexes(self):
        self._seed([("sector_rotation", "600001", "filled", "2026-10-15")])
        with PT._db() as conn:
            definitions = [row[0] for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_paper_risk_decisions%' AND sql IS NOT NULL")]
            for row in conn.execute("PRAGMA index_list(paper_risk_decisions)").fetchall():
                if row[1].startswith("idx_paper_risk_decisions"):
                    conn.execute('DROP INDEX "' + row[1] + '"')
        try:
            self.assertEqual(["600001"], [row["code"] for row in
                             PT.risk_audit(date="2026-10-15")["alerts"]])
        finally:
            with PT._db() as conn:
                for definition in definitions:
                    conn.execute(definition)

    def test_level_filter_matches_the_projected_value(self):
        self._seed([
            ("sector_rotation", "600001", "filled", "2026-10-15"),
            ("sector_rotation", "600002", "t1_rejected", "2026-10-15"),
            ("sector_rotation", "600003", "exit_pending_data", "2026-10-15"),
        ])

        tightened = PT.risk_audit(limit=300, level="tightened")
        watch = PT.risk_audit(limit=300, level="watch")

        self.assertEqual({row["code"] for row in tightened["alerts"]},
                         {"600001", "600003"})
        self.assertEqual({row["code"] for row in watch["alerts"]}, {"600002"})
        self.assertEqual(tightened["pagination"]["total"], 2)
        self.assertEqual(watch["pagination"]["total"], 1)

    def test_unknown_level_is_rejected_not_silently_empty(self):
        self._seed([("sector_rotation", "600001", "filled", "2026-10-15")])
        # ``blocked`` is offered by the UI but the projection never emits it.
        # Silently returning [] would hide that mismatch from the operator.
        with self.assertRaises(ValueError):
            PT.risk_audit(limit=300, level="blocked")

    def test_account_filter_reaches_the_query(self):
        self._seed([
            ("sector_rotation", "600001", "entry_approved", "2026-10-15"),
            ("trend_pullback", "600002", "entry_approved", "2026-10-15"),
            ("sector_rotation", "600003", "entry_approved", "2026-10-15"),
        ])
        result = PT.risk_audit(limit=300, account_id="sector_rotation")
        self.assertEqual({row["code"] for row in result["alerts"]}, {"600001", "600003"})
        self.assertEqual(result["pagination"]["total"], 2,
                         "the count must describe the filtered set, not the table")

        # Paging within a filtered set must stay inside that filter.
        page_one = PT.risk_audit(limit=300, account_id="sector_rotation", page_size=1)
        page_two = PT.risk_audit(limit=300, account_id="sector_rotation", page_size=1,
                                 cursor=page_one["pagination"]["next_cursor"])
        self.assertEqual([row["code"] for row in page_one["alerts"]], ["600003"])
        self.assertEqual([row["code"] for row in page_two["alerts"]], ["600001"])

    def test_invalid_cursor_is_rejected(self):
        self._seed([("sector_rotation", "600001", "entry_approved", "2026-10-15")])
        for bad in ("not-a-number", "a|b", "1|2|3", "2026-10-15|abc"):
            with self.subTest(cursor=bad), self.assertRaises(ValueError):
                PT.risk_audit(limit=300, cursor=bad)

    def test_page_size_is_honoured_exactly(self):
        # A clamped page_size silently returns more rows than asked for, which
        # makes the client's "next page" cursor skip the rows it never saw.
        self._seed([
            ("sector_rotation", f"60013{i:02d}", "entry_approved", "2026-10-15")
            for i in range(5)
        ])
        for asked in (1, 2, 3):
            with self.subTest(page_size=asked):
                result = PT.risk_audit(limit=300, page_size=asked)
                self.assertEqual(len(result["alerts"]), asked)
                self.assertEqual(result["pagination"]["page_size"], asked)

    def test_default_call_keeps_the_legacy_row_cap(self):
        self._seed([
            ("sector_rotation", f"60012{i:02d}", "entry_approved", "2026-10-15")
            for i in range(30)
        ])
        # ``risk_audit(limit=N)`` without page args must stay a plain row cap.
        legacy = PT.risk_audit(limit=20)
        self.assertEqual(len(legacy["alerts"]), 20)
        self.assertIn("pagination", legacy)
        self.assertEqual(legacy["pagination"]["page_size"], 20)


class RiskAuditRouteTests(unittest.TestCase):
    """The reported failure was at the HTTP boundary, so pin it there too.

    ``starlette.testclient`` needs ``httpx``, which is not a runtime dependency
    here, so the route is driven through a minimal ASGI call built on the real
    router — the same approach ``test_operator_boundary`` uses.  Query strings
    therefore go through FastAPI's real parameter binding instead of being
    passed straight into the handler.
    """

    @classmethod
    def setUpClass(cls):
        import asyncio

        from fastapi import FastAPI

        import api_paper

        cls._tmp = tempfile.mkdtemp(prefix="astock-risk-audit-api-")
        cls._old_db = PT.DB_PATH
        PT.DB_PATH = os.path.join(cls._tmp, "paper_trading.sqlite3")
        PT.init_db()

        app = FastAPI()
        app.include_router(api_paper.router)
        cls._operation = app.openapi()["paths"]["/api/paper/risk-audit"]["get"]
        cls._api_paper = api_paper
        cls._asyncio = asyncio
        cls._app = app
        api_paper._cclear("risk_audit")

    @classmethod
    def tearDownClass(cls):
        PT.DB_PATH = cls._old_db
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        with PT._db() as conn:
            conn.execute("DELETE FROM paper_risk_decisions")
        self._api_paper._cclear("risk_audit")

    def _get(self, **params):
        """GET /api/paper/risk-audit through the real ASGI stack."""
        from urllib.parse import urlencode

        query = urlencode({k: v for k, v in params.items() if v is not None})
        path = "/api/paper/risk-audit" + (f"?{query}" if query else "")
        scope = {
            "type": "http", "method": "GET", "path": "/api/paper/risk-audit",
            "raw_path": path.encode("utf-8"), "query_string": query.encode("utf-8"),
            "headers": [], "scheme": "http", "server": ("localhost", 80),
            "client": ("127.0.0.1", 45678), "root_path": "", "http_version": "1.1",
        }
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        self._asyncio.run(self._app(scope, receive, send))
        start = next((m for m in messages if m["type"] == "http.response.start"), None)
        body = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        status = start["status"] if start else None
        return status, (json.loads(body) if body else None)

    def _seed(self, count):
        with PT._db() as conn:
            for index in range(count):
                stamp = PT._strategy_stamp(conn, "sector_rotation")
                conn.execute(
                    """INSERT INTO paper_risk_decisions(
                           account_id,code,side,decision,reason,payload,created_at,
                           strategy_id,strategy_version,strategy_checksum)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    ("sector_rotation", f"6002{index:03d}", "buy", "entry_approved",
                     f"reason-{index}", "{}", "2026-10-15 10:00:00", *stamp),
                )

    def test_second_page_is_not_a_byte_identical_repeat_of_the_first(self):
        # The original report: two requests with the same filters returned the
        # same SHA-256 for ``alerts``.  Reproduce that shape here.
        import hashlib

        self._seed(60)
        status, payload = self._get(limit=160, page_size=10)
        self.assertEqual(200, status)
        self.assertIn("pagination", payload)
        self.assertNotIn("items", payload, "the projection is not a bare list")

        cursor = payload["pagination"]["next_cursor"]
        second_status, second = self._get(limit=160, page_size=10, cursor=cursor)
        self.assertEqual(200, second_status)

        digest_first = hashlib.sha256(
            json.dumps(payload["alerts"], sort_keys=True, ensure_ascii=False)
            .encode("utf-8")).hexdigest()
        digest_second = hashlib.sha256(
            json.dumps(second["alerts"], sort_keys=True, ensure_ascii=False)
            .encode("utf-8")).hexdigest()
        self.assertNotEqual(digest_first, digest_second,
                            "page 2 must differ from page 1")

        # The OpenAPI contract must not advertise a ``page`` offset parameter:
        # accepting it while ignoring it is the same bug in a new costume.
        names = {p["name"] for p in self._operation["parameters"]}
        self.assertNotIn("page", names)
        for expected in ("limit", "page_size", "account_id", "level", "date", "cursor"):
            self.assertIn(expected, names)

    def test_scan_status_survives_filtering(self):
        # The response must keep reporting the live scan state; filtering the
        # history must not silently drop that field.
        self._seed(5)
        status, body = self._get(account_id="sector_rotation", date="2026-10-15")
        self.assertEqual(200, status)
        self.assertIn("scan_status", body)
        self.assertIn("asof", body)
        self.assertEqual(5, body["pagination"]["total"])

    def test_unknown_level_returns_422_not_an_empty_list(self):
        self._seed(3)
        status, _ = self._get(level="blocked")
        self.assertEqual(422, status)

    def test_malformed_cursor_returns_422(self):
        self._seed(3)
        status, _ = self._get(cursor="garbage")
        self.assertEqual(422, status)

    def test_legacy_unparameterised_call_still_works(self):
        # The frontend still issues ``?limit=160`` with no paging arguments.
        self._seed(20)
        status, body = self._get(limit=160)
        self.assertEqual(200, status)
        self.assertEqual(20, len(body["alerts"]))


if __name__ == "__main__":
    unittest.main()
