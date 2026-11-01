# -*- coding: utf-8 -*-
"""风险决策收口：冻结两条旧路径的行内容、权限校验和事务边界。"""
from __future__ import annotations

import copy
import datetime as dt
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paper_risk_writes as PRW  # noqa: E402
import paper_trading as PT  # noqa: E402


STAMP = ("fixture", 7, "frozen-checksum")
NOW = "2026-10-30 10:01:02"
DDL = """CREATE TABLE paper_risk_decisions(
    id INTEGER PRIMARY KEY, account_id TEXT, code TEXT, side TEXT,
    decision TEXT, reason TEXT, payload TEXT, created_at TEXT,
    strategy_id TEXT, strategy_version INTEGER, strategy_checksum TEXT,
    order_id INTEGER)"""
LEGACY_INSERT = """INSERT INTO paper_risk_decisions(
    account_id,code,side,decision,reason,payload,created_at,
    strategy_id,strategy_version,strategy_checksum,order_id)
    VALUES(?,?,?,?,?,?,?,?,?,?,?)"""


class RiskDecisionWriteTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(DDL)
        self.addCleanup(self.conn.close)

    def write(self, payload=None, **kwargs):
        PRW.insert_risk_decision(
            self.conn, "fixture", "600001", "sell", "unfilled", "冻结原因",
            {} if payload is None else payload,
            strategy_stamp=STAMP, cycle_id=9, now=NOW, **kwargs)

    def test_both_legacy_paths_match_every_column_including_raw_json(self):
        for sort_keys in (False, True):
            for authority, order_id, kind in (
                (None, None, None), ("RISK", "17", None),
                ("ENTRY", 18, "entry_gate"), ("EXECUTION", 19, ""),
                ("ALLOCATION", None, "capacity"), ("TIMING", None, "wait"),
                ("INTRADAY", None, "exit"), ("AUDIT", None, "audit"),
            ):
                with self.subTest(sort_keys=sort_keys, authority=authority, kind=kind):
                    payload = {
                        "z": "中文", "a": [None, 0, False],
                        "observed_at": dt.date(2026, 10, 30),
                        "decision_snapshot": {"status": "unknown", "source": None},
                        "decision_provenance": {"authority": "payload-claim"},
                    }
                    original = copy.deepcopy(payload)
                    legacy = copy.deepcopy(payload)
                    if authority is not None:
                        legacy["decision_provenance"] = {
                            "schema_version": "risk-decision-provenance-v1",
                            "authority": authority, "decision_kind": kind or "unfilled",
                        }
                    expected = (
                        "fixture", "600001", "sell", "unfilled", "冻结原因",
                        json.dumps(legacy, ensure_ascii=False, default=str, sort_keys=sort_keys),
                        NOW, *STAMP, int(order_id) if order_id is not None else None,
                    )
                    self.conn.execute("DELETE FROM paper_risk_decisions")
                    self.conn.execute(LEGACY_INSERT, expected)
                    before = self.conn.execute("SELECT * FROM paper_risk_decisions").fetchone()
                    self.conn.execute("DELETE FROM paper_risk_decisions")
                    self.write(payload, order_id=order_id, authority=authority,
                               decision_kind=kind, sort_payload_keys=sort_keys)
                    self.assertEqual(before, self.conn.execute(
                        "SELECT * FROM paper_risk_decisions").fetchone())
                    self.assertEqual(payload, original, "写入不得污染调用方 payload")

    def test_order_link_and_payload_claim_do_not_imply_authority(self):
        with self.assertRaisesRegex(ValueError, "requires an explicit authority"):
            self.write({"decision_provenance": {"authority": "RISK"}}, order_id=1)
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM paper_risk_decisions").fetchone()[0])
        self.assertFalse(self.conn.in_transaction)

    def test_invalid_authority_writes_nothing(self):
        with self.assertRaisesRegex(ValueError, "unknown risk decision authority"):
            self.write(authority="GUESSED")
        self.assertFalse(self.conn.in_transaction)

    def test_write_stays_in_callers_transaction_and_rolls_back(self):
        self.conn.execute("CREATE TABLE sibling(value INTEGER)")
        self.conn.execute("INSERT INTO sibling VALUES(1)")
        statements = []
        self.conn.set_trace_callback(statements.append)
        self.write(authority="RISK", order_id=17)
        self.assertEqual(1, len(statements), "仍应只执行一条 INSERT")
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual([], self.conn.execute("SELECT * FROM sibling").fetchall())
        self.assertEqual([], self.conn.execute("SELECT * FROM paper_risk_decisions").fetchall())

    def test_facade_keeps_snapshot_stamp_time_and_advice_once(self):
        snapshot = {"z": "中文", "decision_snapshot": {"status": "unknown"}, "a": None}
        with mock.patch.object(PT, "_with_decision_snapshot", return_value=snapshot) as build, \
                mock.patch.object(PT, "resolve_strategy_stamp", return_value=STAMP) as stamp, \
                mock.patch.object(PT, "_now", return_value=NOW), \
                mock.patch("ai_advice_service.consume_risk_decision_safe") as advice:
            PT._risk_log(self.conn, "fixture", "600001", "sell", "unfilled", "冻结原因", {},
                         order_id="17", authority="RISK")
        build.assert_called_once_with({}, account_id="fixture", code="600001", side="sell",
                                      decision="unfilled", reason="冻结原因")
        stamp.assert_called_once_with(self.conn, "fixture")
        advice.assert_called_once_with(self.conn, account_id="fixture", code="600001",
                                       side="sell", decision="unfilled", strategy_stamp=STAMP)
        payload = dict(snapshot, decision_provenance={
            "schema_version": "risk-decision-provenance-v1",
            "authority": "RISK", "decision_kind": "unfilled"})
        self.assertEqual(("fixture", "600001", "sell", "unfilled", "冻结原因",
                          json.dumps(payload, ensure_ascii=False, default=str), NOW, *STAMP, 17),
                         self.conn.execute("SELECT * FROM paper_risk_decisions").fetchone()[1:])

    def test_missing_authority_fails_before_snapshot_or_stamp_resolution(self):
        with mock.patch.object(PT, "_with_decision_snapshot") as build, \
                mock.patch.object(PT, "resolve_strategy_stamp") as stamp, \
                self.assertRaisesRegex(ValueError, "requires an explicit authority"):
            PT._risk_log(self.conn, "fixture", "600001", "sell", "unfilled", "r", {}, order_id=1)
        build.assert_not_called()
        stamp.assert_not_called()

    def test_invalid_authority_fails_before_strategy_queries(self):
        with mock.patch.object(PT, "resolve_strategy_stamp") as stamp, \
                self.assertRaisesRegex(ValueError, "unknown risk decision authority"):
            PT._risk_log(self.conn, "fixture", "600001", "sell", "unfilled", "r", {},
                         authority="GUESSED")
        stamp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
