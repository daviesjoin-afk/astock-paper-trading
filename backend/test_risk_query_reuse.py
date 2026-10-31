"""PR5: query reuse remains scoped to factual owner and write-time identity."""
from contextlib import contextmanager
import datetime as dt
import json
import sqlite3
import unittest
from unittest import mock

import execution_verification as EV
import paper_replacement_evidence as Replacement
import paper_risk_evidence as Risk
import paper_trading as PT
from test_replacement_asof_provenance import _EvidenceCase, ACCOUNT, DAY
from test_risk_application_service import _RiskServiceCase


class MarkerBatchTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE paper_orders(
            id INTEGER PRIMARY KEY, account_id, code, side, execution_verified,
            execution_status, created_at, executed_at, filled_price, risk_payload, reason)""")
        self.markers = {"hard_stop_first_trim": "%旧首段%", "downside_partial": "%旧partial%",
                        "downside_full": None, "downside_warning_trim": None}
        self.kwargs = dict(account_id="a", code="600001", asof_day=DAY,
                           now=dt.datetime(2026, 9, 10, 10, 30))

    def add(self, marker, *, status="verified", flag=1, account="a", code="600001",
            day=DAY, side="sell", payload=None, reason="", executed=None):
        return self.conn.execute(
            "INSERT INTO paper_orders(account_id,code,side,execution_verified,execution_status,"
            "created_at,executed_at,filled_price,risk_payload,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (account, code, side, flag, status, str(day) + " 10:00:00", executed,
             9.0, json.dumps({"exit_marker": marker}) if payload is None else payload, reason),
        ).lastrowid

    def expected(self):
        return {marker: EV.first_verified_positive_execution(
            self.conn, marker=marker, legacy_reason_like=legacy, **self.kwargs,
        ) for marker, legacy in self.markers.items()}

    def assert_parity(self):
        expected = self.expected()
        exists = {marker: EV.has_verified_positive_execution(
            self.conn, marker=marker, legacy_reason_like=legacy,
            **{k: v for k, v in self.kwargs.items() if k != "now"},
        ) for marker, legacy in self.markers.items()}
        actual = EV.verified_execution_markers(self.conn, markers=self.markers, **self.kwargs)
        self.assertEqual(actual, expected)
        self.assertEqual({k: v is not None for k, v in actual.items()}, exists)
        return actual

    def test_healthy_batch_is_one_select_and_preserves_first_evidence(self):
        self.add("hard_stop_first_trim", executed="2026-09-10 10:05:00")
        self.add("hard_stop_first_trim", executed="2026-09-10 10:10:00")
        self.add("downside_partial", status="partial", flag=0)
        self.add("downside_full", status="unknown", flag=0)
        self.add("downside_full", status="verified", flag=0)
        self.add("downside_full", account="other")
        self.add("downside_full", code="600002")
        self.add("downside_full", side="buy")
        self.add("downside_full", day=DAY + dt.timedelta(days=1))
        expected = self.assert_parity()
        queries = []
        self.conn.set_trace_callback(queries.append)
        try:
            result = EV.verified_execution_markers(self.conn, markers=self.markers, **self.kwargs)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(queries), 1)
        self.assertEqual(result, expected)
        self.assertEqual(result["hard_stop_first_trim"]["age_minutes"], 25.0)
        self.assertIsNotNone(result["downside_partial"])
        self.assertIsNone(result["downside_full"])

    def test_legacy_like_and_unparseable_execution_time_match_owner(self):
        self.add(None, payload="{}", reason="旧首段已完成", executed="not-a-time")
        result = self.assert_parity()
        self.assertIsNotNone(result["hard_stop_first_trim"])
        self.assertIsNone(result["hard_stop_first_trim"]["age_minutes"])

    def test_malformed_json_falls_back_without_poisoning_earlier_match(self):
        self.add("hard_stop_first_trim")
        self.add(None, payload="malformed JSON")
        result = self.assert_parity()
        self.assertEqual(result["hard_stop_first_trim"]["filled_price"], 9.0)
        self.assertEqual(result["downside_full"], {
            "created_at": None, "executed_at": None, "filled_price": None, "age_minutes": None,
        })

    def test_absent_table_is_unknown_and_never_confirmed_zero(self):
        self.conn.execute("DROP TABLE paper_orders")
        result = self.assert_parity()
        self.assertTrue(all(value is not None for value in result.values()))

    def test_reads_after_new_execution_do_not_reuse_previous_snapshot(self):
        self.assertIsNone(self.assert_parity()["downside_full"])
        self.add("downside_full", status="partial", flag=0)
        self.assertIsNotNone(self.assert_parity()["downside_full"])
        self.conn.commit()
        self.conn.execute("DELETE FROM paper_orders")
        self.assertIsNone(self.assert_parity()["downside_full"])

    def test_empty_requests_and_marker_sql_identifiers_remain_bounded(self):
        self.assertEqual(EV.verified_execution_markers(self.conn, markers={}, **self.kwargs), {})
        for argument in ("column", "marker_key"):
            with self.assertRaises(ValueError):
                EV.verified_execution_markers(self.conn, markers=self.markers,
                                              **self.kwargs, **{argument: "unapproved"})

    def test_bare_sqlite_rows_are_supported(self):
        self.add("downside_full")
        self.conn.row_factory = None
        result = EV.verified_execution_markers(self.conn, markers=self.markers, **self.kwargs)
        self.assertEqual(result["downside_full"]["filled_price"], 9.0)


class ReplacementRowsTests(_EvidenceCase):
    def test_raw_rows_are_reused_but_each_selection_has_its_own_held_scope(self):
        self.assertIn("replacement_candidate_inputs", Risk.__all__)
        self.add_signal(1, intended_date=str(DAY), code="600001", entry_score=80)
        self.add_signal(2, intended_date=str(DAY), code="600002", entry_score=90)
        deps = PT._risk_service_ports().evidence
        rows = Replacement.load_replacement_candidates(
            self.conn, account_id=ACCOUNT, asof_day=str(DAY),
            statuses=("pending", "deferred_capacity", deps.entry_frozen_waitlist_status),
        )
        before = json.dumps(rows, sort_keys=True)
        with mock.patch.object(Replacement, "load_replacement_candidates", side_effect=AssertionError("re-read")):
            a = Risk.best_replacement_candidate(self.conn, ACCOUNT, DAY, {"600001"}, deps=deps, candidate_rows=rows)
            b = Risk.best_replacement_candidate(self.conn, ACCOUNT, DAY, {"600002"}, deps=deps, candidate_rows=rows)
            empty = Risk.best_replacement_candidate(self.conn, ACCOUNT, DAY, set(), deps=deps, candidate_rows=[])
        self.assertEqual(a["code"], "600002")
        self.assertEqual(b["code"], "600001")
        self.assertIsNone(empty)
        self.assertEqual(json.dumps(rows, sort_keys=True), before)


class ProductionQueryReuseTests(_RiskServiceCase):
    def test_three_positions_share_one_account_query_but_markers_are_position_local(self):
        self.add_lot(100, 10.0)
        self.add_lot_for(self.code_b, 100, 10.0)
        self.add_lot_for("600003", 100, 10.0)
        for code in (self.code, self.code_b, "600003"):
            self.set_quote(code)
        queries = []
        original = PT._db
        @contextmanager
        def tracked(*args, **kwargs):
            with original(*args, **kwargs) as conn:
                conn.set_trace_callback(lambda sql: queries.append((kwargs.get("immediate", False), sql)))
                yield conn
        with mock.patch.object(PT, "_db", tracked):
            self.run_risk()
        replacements = [(writer, sql) for writer, sql in queries
                        if "FROM paper_signals WHERE account_id=" in sql and "signal_date<=" in sql]
        self.assertEqual([writer for writer, _ in replacements], [False, True])
        markers = [(writer, sql) for writer, sql in queries if "AS marker_0" in sql]
        self.assertEqual(len(markers), 3)
        self.assertTrue(all(writer for writer, _ in markers))

    def test_candidate_expired_after_prefetch_is_rejected_by_writer_snapshot(self):
        self.add_lot(100, 10.0)
        self.set_non_sell_quote()
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,payload,status,"
                "created_at,strategy_id,strategy_version,strategy_checksum,cycle_id) "
                "VALUES(?,?,?,?,?,'{}','pending',?,?,?,?,?)",
                (self.ACCOUNT, str(self.day), str(self.day), "600999", "测试股", str(self.day),
                 *PT._strategy_stamp(conn, self.ACCOUNT), self.cycle),
            )
        original = PT._risk_load_market_inputs
        def change(**kwargs):
            self.assertIn("600999", kwargs["candidate_codes"])
            result = original(**kwargs)
            with PT._db(immediate=True) as conn:
                conn.execute("UPDATE paper_signals SET status='expired' WHERE code='600999'")
            return result
        with mock.patch.object(PT, "_risk_load_market_inputs", side_effect=change):
            self.run_risk()
        self.assertIsNone(self.review_detail().get("replacement_score"))
