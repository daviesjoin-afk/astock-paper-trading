# -*- coding: utf-8 -*-
"""R33-A regressions: exact strategy health evidence.

H1–H18 覆盖事实层契约：同一输入同一指纹、身份/窗口 fail closed、owner 词表分档、
缺失保持缺失、历史不愈合、append-only 幂等、以及**零 lifecycle / 零正式账本写入**。
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import api_strategies as API  # noqa: E402
import paper_trading as PT  # noqa: E402
import strategy_api_models as Models  # noqa: E402
import strategy_health as SH  # noqa: E402
import strategy_health_repository as SHR  # noqa: E402
import strategy_health_service as SHV  # noqa: E402
import strategy_lifecycle as SL  # noqa: E402
import strategy_registry as SR  # noqa: E402

DSL = {"op": "gt", "left": {"op": "field", "name": "close"},
       "right": {"op": "const", "value": 0}}
WINDOW = {"observation_start": "2026-09-01", "observation_end": "2026-10-01"}
STRATEGY_ID = "r33a_strategy"


class _HealthCase(unittest.TestCase):
    """真实 paper schema + 一个真实 immutable strategy version。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="r33a-", ignore_cleanup_errors=True)
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self._old_db, self._old_sr_db = PT.DB_PATH, SR.DEFAULT_DB_PATH
        PT.DB_PATH = self.path
        SR.DEFAULT_DB_PATH = self.path
        PT.init_db()
        PT.start_new_cycle(capital=100_000.0, include_dashboard=False)
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.cycle_id = int(self.conn.execute(
            "SELECT MAX(id) FROM paper_cycles").fetchone()[0])
        self.spec = SR.create_user_definition(self.conn, STRATEGY_ID, "R33-A", dsl_ast=DSL)
        self.version = SR.get_version(self.spec.id, conn=self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        PT.DB_PATH, SR.DEFAULT_DB_PATH = self._old_db, self._old_sr_db
        self.tmp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _capture(self, **overrides):
        values = {"strategy_version": int(self.version.version),
                  "strategy_checksum": self.version.checksum, **WINDOW}
        values.update(overrides)
        return SHV.capture_strategy_health(self.spec.id, **values)

    def _seed_order(self, *, created_at="2026-09-10 10:00:00", status="filled",
                    execution_status=None, execution_verified=None,
                    evidence_source=None, cycle_id=None, qty=100, filled_qty=100,
                    strategy_version=None, strategy_checksum=None):
        # 仓库不变式：account_id == strategy_id（策略即账户），且 cycle_id 必须指向
        # 真实存在的周期行（order cycle provenance 触发器）。
        cursor = self.conn.execute(
            "INSERT INTO paper_orders(account_id,cycle_id,strategy_id,strategy_version,"
            "strategy_checksum,code,side,status,reason,order_type,qty,planned_price,"
            "filled_qty,filled_price,created_at,execution_evidence,risk_payload,"
            "execution_status,execution_verified,execution_evidence_source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.spec.id, self.cycle_id if cycle_id is None else cycle_id, self.spec.id,
             int(strategy_version if strategy_version is not None else self.version.version),
             strategy_checksum or self.version.checksum, "600000", "buy", status, None,
             "market", qty, 10.0, filled_qty, 10.0, created_at, "{}", "{}",
             execution_status, execution_verified, evidence_source))
        self.conn.commit()
        return int(cursor.lastrowid)

    def _seed_fill(self, order_id, *, fill_date="2026-09-10"):
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,assumption) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (int(order_id), self.spec.id, "buy", "600000", 100, 10.0, 1000.0, 1.0,
             fill_date, "test"))
        self.conn.commit()

    def _seed_risk(self, *, authority="RISK", decision="downside_warning",
                   created_at="2026-09-10 10:05:00", order_id=None, payload=None):
        if payload is None:
            payload = {}
            if authority is not None:
                payload = {"decision_provenance": {
                    "schema_version": "risk-decision-provenance-v1",
                    "authority": authority, "decision_kind": decision}}
        self.conn.execute(
            "INSERT INTO paper_risk_decisions(account_id,code,side,decision,reason,payload,"
            "created_at,strategy_id,strategy_version,strategy_checksum,order_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (self.spec.id, "600000", "buy", decision, "fixture",
             json.dumps(payload, ensure_ascii=False), created_at, self.spec.id,
             int(self.version.version), self.version.checksum, order_id))
        self.conn.commit()

    def _dimension(self, projection, name):
        return next(item for item in projection["dimensions"] if item["name"] == name)

    def _ledger_counts(self):
        names = ("paper_orders", "paper_fills", "paper_positions", "paper_position_lots",
                 "paper_cash_flows", "paper_nav")
        counts = {}
        for name in names:
            try:
                counts[name] = self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            except sqlite3.Error:
                counts[name] = None
        return counts

    def _lifecycle_fingerprint(self):
        state = self.conn.execute(
            "SELECT strategy_id,strategy_version,state,last_event_id,updated_at"
            " FROM strategy_lifecycle_state ORDER BY strategy_id,strategy_version").fetchall()
        events = self.conn.execute(
            "SELECT event_fingerprint FROM strategy_lifecycle_events ORDER BY id").fetchall()
        return ([tuple(row) for row in state], [row[0] for row in events])


class StrategyHealthContractTests(_HealthCase):
    """H1–H4、H10：身份、窗口与 coverage 契约。"""

    def test_h1_same_exact_inputs_produce_the_same_fingerprint(self):
        first = self._capture()
        second = self._capture()
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first, second)
        self.assertEqual(64, len(first["snapshot_id"]))
        # 指纹不含持久化元数据：同一输入重复采集不会因为时间不同而变。
        self.assertNotIn("created_at", first)

    def test_h1b_builder_is_deterministic_and_self_verifying(self):
        window = SH.HealthObservationWindow(**WINDOW)
        dimensions = [SH.unavailable_dimension(name, "fixture")
                      for name in SH.DIMENSIONS]
        first = SH.build_strategy_health(strategy_id="s", strategy_version=1,
                                         strategy_checksum="a" * 64,
                                         observation_window=window, lifecycle_state="shadow",
                                         dimensions=dimensions)
        second = SH.build_strategy_health(strategy_id="s", strategy_version=1,
                                          strategy_checksum="a" * 64,
                                          observation_window=window, lifecycle_state="shadow",
                                          dimensions=dimensions)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertTrue(SH.verify_snapshot_fingerprint(first))

    def test_h1c_source_fingerprints_participate_in_the_snapshot_fingerprint(self):
        # 快照指纹必须覆盖维度自己的来源指纹，否则「同 id 不同来源」会被静默接受。
        window = SH.HealthObservationWindow(**WINDOW)

        def build(source_fingerprint):
            dimensions = [SH.HealthDimension(
                name=name, status=SH.STATUS_AVAILABLE, facts={"n": 1},
                provenance=SH.PROVENANCE_OWNER_ISSUED, source_identity=name,
                source_fingerprint=source_fingerprint) for name in SH.DIMENSIONS]
            return SH.build_strategy_health(
                strategy_id="s", strategy_version=1, strategy_checksum="a" * 64,
                observation_window=window, lifecycle_state="draft", dimensions=dimensions)

        self.assertNotEqual(build("b" * 64).snapshot_id, build("c" * 64).snapshot_id)

    def test_h2_unknown_exact_version_fails_closed(self):
        with self.assertRaises(SH.HealthEvidenceError) as raised:
            self._capture(strategy_version=99)
        self.assertEqual("exact_strategy_version_not_persisted", str(raised.exception))

    def test_h3_checksum_mismatch_fails_closed(self):
        with self.assertRaises(SH.HealthEvidenceError) as raised:
            self._capture(strategy_checksum="b" * 64)
        self.assertEqual("exact_strategy_version_not_persisted", str(raised.exception))

    def test_h4_window_must_be_explicit_and_forward(self):
        for field in ("observation_start", "observation_end"):
            values = {"strategy_version": int(self.version.version),
                      "strategy_checksum": self.version.checksum, **WINDOW}
            values.pop(field)
            with self.subTest(missing=field):
                with self.assertRaises(TypeError):
                    SHV.capture_strategy_health(self.spec.id, **values)
        with self.assertRaises(SH.HealthEvidenceError) as raised:
            self._capture(observation_start="2026-10-01", observation_end="2026-09-01")
        self.assertEqual("observation_window_must_be_forward", str(raised.exception))
        with self.assertRaises(SH.HealthEvidenceError) as raised:
            self._capture(observation_start="not-a-day", observation_end="2026-10-01")
        self.assertEqual("observation_start_must_be_iso_date", str(raised.exception))

    def test_h4b_window_is_part_of_the_fingerprint(self):
        first = self._capture()
        moved = self._capture(observation_start="2026-08-01")
        self.assertNotEqual(first["snapshot_id"], moved["snapshot_id"])
        self.assertEqual(SH.HealthObservationWindow(**WINDOW).identity,
                         first["window_identity"])

    def test_h10_missing_evidence_lowers_coverage_without_a_synthetic_failure(self):
        projection = self._capture()
        coverage = projection["coverage"]
        self.assertEqual(len(SH.DIMENSIONS), coverage["expected_dimensions"])
        self.assertEqual(
            coverage["expected_dimensions"],
            coverage["available_dimensions"] + coverage["partial_dimensions"]
            + coverage["unavailable_dimensions"] + coverage["not_applicable_dimensions"])
        self.assertEqual(round(coverage["available_dimensions"]
                               / coverage["expected_dimensions"], 6),
                         coverage["coverage_ratio"])
        # PARTIAL 与 NOT_APPLICABLE 都不算「有证据」。
        self.assertLess(coverage["coverage_ratio"], 1.0)
        # 缺失维度必须自己说明原因，而不是被填成 0/false。
        for item in projection["dimensions"]:
            if item["status"] in ("UNAVAILABLE", "NOT_APPLICABLE"):
                self.assertTrue(item["blocking_reasons"], item["name"])
        # 契约里不存在任何健康结论字段：只有证据状态与事实。
        verdict_keys = {"healthy", "unhealthy", "health_score", "verdict", "conclusion"}
        self.assertEqual(set(), set(projection) & verdict_keys)
        for item in projection["dimensions"]:
            self.assertEqual(set(), set(item["facts"]) & verdict_keys, item["name"])

    def test_h10b_a_dimension_cannot_be_omitted(self):
        window = SH.HealthObservationWindow(**WINDOW)
        with self.assertRaises(SH.HealthEvidenceError) as raised:
            SH.build_strategy_health(
                strategy_id="s", strategy_version=1, strategy_checksum="a" * 64,
                observation_window=window, lifecycle_state="shadow",
                dimensions=[SH.unavailable_dimension(n, "fixture")
                            for n in SH.DIMENSIONS[:-1]])
        self.assertEqual("health_dimensions_must_cover_every_dimension_exactly_once",
                         str(raised.exception))


class StrategyHealthDimensionTests(_HealthCase):
    """H5–H9、H5b：每个维度只报告 owner 事实。"""

    def test_h5_runtime_dimension_reports_the_exact_version_checks(self):
        runtime = self._dimension(self._capture(), SH.DIMENSION_RUNTIME_INTEGRITY)
        self.assertEqual("AVAILABLE", runtime["status"])
        self.assertEqual("OWNER_ISSUED", runtime["provenance"])
        self.assertTrue(runtime["facts"]["runtime_ready"])
        self.assertEqual(int(self.version.version),
                         runtime["facts"]["exact_version"]["version"])
        self.assertEqual(self.version.checksum, runtime["facts"]["exact_version"]["checksum"])
        self.assertTrue(all(runtime["facts"]["checks"]))

    def test_h5b_head_readiness_never_stands_in_for_the_exact_version(self):
        # head 前进到 v2 之后，v1 的健康快照仍只报告 v1 的编译事实。
        old = self._capture()
        SR.save_definition(self.conn, self.spec.id,
                           {"dsl_ast": {"op": "lt", "left": {"op": "field", "name": "close"},
                                        "right": {"op": "const", "value": 0}}},
                           expected_version=int(self.version.version), actor="r33a-test")
        self.conn.commit()
        fetched = SHV.get_health_snapshot(self.spec.id, old["snapshot_id"])
        self.assertEqual(old, fetched)
        self.assertEqual(int(self.version.version),
                         self._dimension(fetched, SH.DIMENSION_RUNTIME_INTEGRITY)[
                             "facts"]["exact_version"]["version"])

    def test_h6_only_risk_authority_rows_count_as_risk(self):
        order_id = self._seed_order()
        self._seed_risk(authority="RISK", order_id=order_id)
        self._seed_risk(authority="ENTRY", decision="entry_gate_blocked", order_id=order_id)
        self._seed_risk(authority="EXECUTION", decision="execution_blocked", order_id=order_id)
        risk = self._dimension(self._capture(), SH.DIMENSION_RISK_EVIDENCE)
        facts = risk["facts"]
        self.assertEqual(3, facts["risk_decision_rows"])
        self.assertEqual(1, facts["owner_issued_risk_rows"])
        self.assertEqual(1, facts["rows_by_declared_authority"]["ENTRY"])
        self.assertEqual(1, facts["rows_by_declared_authority"]["EXECUTION"])
        self.assertEqual(3, facts["order_linked_rows"])
        self.assertEqual(0, facts["rows_without_declared_authority"])

    def test_h6b_rows_without_a_declared_authority_stay_unclassified(self):
        self._seed_risk(authority=None, payload={})
        facts = self._dimension(self._capture(), SH.DIMENSION_RISK_EVIDENCE)["facts"]
        self.assertEqual(0, facts["owner_issued_risk_rows"])
        self.assertEqual(1, facts["rows_without_declared_authority"])

    def test_h7_unknown_execution_stays_unknown(self):
        unknown_order = self._seed_order(status="filled", execution_status=None,
                                         execution_verified=None)
        verified_order = self._seed_order(status="filled", execution_status="verified",
                                          execution_verified=1,
                                          evidence_source="paper_orders+paper_fills")
        self._seed_fill(verified_order)
        facts = self._dimension(self._capture(), SH.DIMENSION_EXECUTION_EVIDENCE)["facts"]
        self.assertEqual(2, facts["orders_in_window"])
        self.assertEqual(1, facts["verified_orders"])
        # 未盖章的行记 not_stamped，既不升级成 verified，也不折算成 unknown。
        self.assertEqual(1, facts["owner_execution_status"][SHV.NOT_STAMPED])
        self.assertEqual(1, facts["owner_execution_status"]["verified"])
        self.assertEqual(1, facts["fill_rows"])
        self.assertEqual(1, facts["orders_with_fill_rows"])
        self.assertNotIn("fill_ratio", facts)
        self.assertTrue(unknown_order)

    def test_h8_performance_has_no_owner_and_stays_unavailable(self):
        performance = self._dimension(self._capture(), SH.DIMENSION_PERFORMANCE)
        self.assertEqual("UNAVAILABLE", performance["status"])
        self.assertEqual("UNAVAILABLE", performance["provenance"])
        self.assertEqual([SH.REASON_STRATEGY_PERFORMANCE_OWNER_UNAVAILABLE],
                         performance["blocking_reasons"])
        self.assertIsNone(performance["facts"]["strategy_version_scoped_owner"])
        self.assertEqual({}, {key: value for key, value in performance["facts"].items()
                              if key != "strategy_version_scoped_owner"})

    def test_h9_zero_activity_is_a_fact_not_a_verdict(self):
        activity = self._dimension(self._capture(), SH.DIMENSION_ACTIVITY_COVERAGE)
        self.assertEqual(0, activity["facts"]["orders_in_window"])
        self.assertEqual(0, activity["facts"]["fill_rows"])
        self.assertEqual(0, activity["facts"]["observed_cycles"])
        self.assertIsNone(activity["facts"]["signals"])
        self.assertIn(SH.REASON_SIGNAL_ATTRIBUTION_UNAVAILABLE, activity["blocking_reasons"])
        self.assertTrue(activity["facts"]["zero_activity_is_not_unhealthy"])

    def test_h7b_orders_outside_the_window_are_not_counted(self):
        self._seed_order(created_at="2026-10-05 10:00:00")
        facts = self._dimension(self._capture(), SH.DIMENSION_EXECUTION_EVIDENCE)["facts"]
        self.assertEqual(0, facts["orders_in_window"])

    def test_comparable_evidence_is_not_applicable_unless_named(self):
        comparable = self._dimension(self._capture(), SH.DIMENSION_COMPARABLE_EVIDENCE)
        self.assertEqual("NOT_APPLICABLE", comparable["status"])
        self.assertEqual([SH.REASON_COMPARISON_REPORT_NOT_SPECIFIED],
                         comparable["blocking_reasons"])
        self.assertEqual({}, comparable["facts"])


class StrategyHealthImmutabilityTests(_HealthCase):
    """H11–H17：历史不愈合、append-only、零 lifecycle / 零账本写入。"""

    def test_h11_a_historical_snapshot_does_not_heal(self):
        before = self._capture()
        self.assertEqual("UNAVAILABLE",
                         self._dimension(before, SH.DIMENSION_PERFORMANCE)["status"])
        order_id = self._seed_order()
        self._seed_risk(authority="RISK", order_id=order_id)
        self._seed_fill(order_id)
        fetched = SHV.get_health_snapshot(self.spec.id, before["snapshot_id"])
        self.assertEqual(before, fetched)
        # 新事实需要**新快照**，旧快照的 identity 与内容都不变。
        after = self._capture()
        self.assertNotEqual(before["snapshot_id"], after["snapshot_id"])
        self.assertEqual(1, self._dimension(after, SH.DIMENSION_RISK_EVIDENCE)[
            "facts"]["owner_issued_risk_rows"])

    def test_h12_append_is_idempotent(self):
        projection = self._capture()
        snapshot = SHR.get_snapshot(self.conn, projection["snapshot_id"])
        SHR.append_snapshot(self.conn, snapshot)
        SHR.append_snapshot(self.conn, snapshot)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM strategy_health_snapshots").fetchone()[0]
        self.assertEqual(1, rows)

    def test_h13_same_identity_different_content_conflicts(self):
        window = SH.HealthObservationWindow(**WINDOW)
        dimensions = [SH.unavailable_dimension(name, "fixture") for name in SH.DIMENSIONS]
        snapshot = SH.build_strategy_health(
            strategy_id=self.spec.id, strategy_version=int(self.version.version),
            strategy_checksum=self.version.checksum, observation_window=window,
            lifecycle_state="draft", dimensions=dimensions)
        # 先写一行与仓库无关的垃圾内容（append-only 只挡 UPDATE/DELETE），随后用
        # 同一 id 追加：必须报冲突，绝不静默覆盖。
        self.conn.execute(
            "INSERT INTO strategy_health_snapshots(snapshot_id,snapshot_fingerprint,"
            "health_contract_version,strategy_id,strategy_version,strategy_checksum,"
            "observation_start,observation_end,window_identity,lifecycle_state,"
            "coverage_ratio,evidence_json,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot.snapshot_id, snapshot.snapshot_fingerprint, "x", snapshot.strategy_id,
             1, snapshot.strategy_checksum, "2026-09-01", "2026-10-01", "w", None, 0.0,
             '{"tampered":true}', "2026-10-01T00:00:00+00:00"))
        self.conn.commit()
        with self.assertRaises(SHR.StrategyHealthRepositoryError) as raised:
            SHR.append_snapshot(self.conn, snapshot)
        self.assertEqual("health_snapshot_idempotency_conflict", str(raised.exception))

    def test_h14_exact_get_only(self):
        self.assertEqual({"StrategyHealthRepositoryError", "append_snapshot", "get_snapshot"},
                         set(SHR.__all__))
        with self.assertRaises(SHR.StrategyHealthRepositoryError) as raised:
            SHR.get_snapshot(self.conn, "too-short")
        self.assertEqual("explicit_health_snapshot_id_required", str(raised.exception))
        # 表**非空**时也必须精确：未知 id 不得兜底到「最新的一条」。
        self._capture()
        self.assertIsNotNone(self.conn.execute(
            "SELECT COUNT(*) FROM strategy_health_snapshots").fetchone()[0])
        self.assertIsNone(SHR.get_snapshot(self.conn, "f" * 64))
        with self.assertRaises(SH.HealthEvidenceError) as raised:
            SHV.get_health_snapshot(self.spec.id, "f" * 64)
        self.assertEqual("health_snapshot_not_found", str(raised.exception))

    def test_h15_no_latest_fallback_anywhere_on_the_health_path(self):
        early = self._capture()
        late = self._capture(observation_start="2026-09-15")
        self.assertNotEqual(early["snapshot_id"], late["snapshot_id"])
        first = SHV.get_health_snapshot(self.spec.id, early["snapshot_id"])
        second = SHV.get_health_snapshot(self.spec.id, late["snapshot_id"])
        self.assertEqual(early["observation_start"], first["observation_start"])
        self.assertEqual(late["observation_start"], second["observation_start"])
        for module in (SH, SHR, SHV):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            for token in ("get_latest", "find_latest", "latest_snapshot", "most_recent",
                          "ORDER BY id DESC", "find_best", "get_current_health"):
                self.assertFalse(token in source, f"{module.__name__}: {token}")

    def test_h16_capture_leaves_the_lifecycle_untouched(self):
        before = self._lifecycle_fingerprint()
        self._capture()
        self.assertEqual(before, self._lifecycle_fingerprint())
        states = dict((row[0] + "@" + str(row[1]), row[2]) for row in before[0])
        with PT._db() as conn:
            current = dict((row[0] + "@" + str(row[1]), row[2]) for row in
                           conn.execute("SELECT strategy_id,strategy_version,state"
                                        " FROM strategy_lifecycle_state"))
        self.assertEqual(states, current)

    def test_h17_capture_leaves_the_formal_ledger_untouched(self):
        self._seed_order()
        before = self._ledger_counts()
        for _ in range(2):
            self._capture()
        self.assertEqual(before, self._ledger_counts())

    def test_h18_the_pure_builder_has_no_ambient_dependency(self):
        source = pathlib.Path(SH.__file__).read_text(encoding="utf-8")
        for token in ("sqlite3", "requests", "urllib", "socket", "datetime.now",
                      "utcnow", "paper_trading", "fastapi", "random"):
            self.assertNotIn(token, source, f"strategy_health must not use {token}")
        window = SH.HealthObservationWindow(**WINDOW)
        dimensions = [SH.unavailable_dimension(name, "fixture") for name in SH.DIMENSIONS]
        with mock.patch("socket.socket", side_effect=AssertionError("no network")), \
                mock.patch("time.time", side_effect=AssertionError("no clock")):
            snapshot = SH.build_strategy_health(
                strategy_id="s", strategy_version=1, strategy_checksum="a" * 64,
                observation_window=window, lifecycle_state=None, dimensions=dimensions)
        self.assertEqual(64, len(snapshot.snapshot_id))
        self.assertTrue(SH.verify_snapshot_fingerprint(snapshot))


class StrategyHealthApiSurfaceTests(_HealthCase):
    """§19：最小 surface —— 显式 capture + exact get，且**没有** latest 端点。"""

    def _request(self, **overrides):
        values = {"strategy_version": int(self.version.version),
                  "strategy_checksum": self.version.checksum, **WINDOW}
        values.update(overrides)
        return Models.StrategyHealthCaptureRequest(**values)

    def test_capture_and_exact_get_round_trip(self):
        captured = API.capture_strategy_health_snapshot(self.spec.id, self._request())
        self.assertEqual(64, len(captured["snapshot_id"]))
        fetched = API.get_strategy_health_snapshot(self.spec.id, captured["snapshot_id"])
        self.assertEqual(captured, fetched)

    def test_unknown_snapshot_is_a_404_not_a_fallback(self):
        with self.assertRaises(HTTPException) as raised:
            API.get_strategy_health_snapshot(self.spec.id, "f" * 64)
        self.assertEqual(404, raised.exception.status_code)
        self.assertEqual("health_snapshot_not_found", str(raised.exception.detail))

    def test_input_identity_conflicts_are_400(self):
        with self.assertRaises(HTTPException) as raised:
            API.capture_strategy_health_snapshot(self.spec.id, self._request(strategy_version=99))
        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("exact_strategy_version_not_persisted", str(raised.exception.detail))
        with self.assertRaises(HTTPException) as raised:
            API.capture_strategy_health_snapshot(
                self.spec.id, self._request(observation_start="2026-10-01",
                                            observation_end="2026-09-01"))
        self.assertEqual(400, raised.exception.status_code)

    def test_the_route_surface_has_no_latest_endpoint(self):
        paths = {getattr(route, "path", "") for route in API.router.routes}
        self.assertIn("/api/strategies/{strategy_id}/health/snapshots", paths)
        self.assertIn("/api/strategies/{strategy_id}/health/snapshots/{snapshot_id}", paths)
        self.assertEqual([], [path for path in paths if "health" in path and "latest" in path])


class StrategyHealthArchitectureGuardTests(unittest.TestCase):
    """§15 / §22：健康证据不得变成第二个 lifecycle 或第二个账本 writer。"""

    def _source(self, module):
        return pathlib.Path(module.__file__).read_text(encoding="utf-8")

    def test_capture_never_writes_the_lifecycle(self):
        source = self._source(SHV)
        for token in ("SL.transition(", "UPDATE strategy_lifecycle",
                      "INSERT INTO strategy_lifecycle", "DELETE FROM strategy_lifecycle",
                      "initialize_version"):
            self.assertNotIn(token, source)

    def test_capture_never_writes_the_formal_ledger(self):
        source = self._source(SHV)
        for token in ("INSERT INTO paper_", "UPDATE paper_", "DELETE FROM paper_",
                      "INSERT OR REPLACE INTO paper_"):
            self.assertNotIn(token, source)
        # 它只写自己的表，而且只通过 repository。
        self.assertIn("SHRepo.append_snapshot(", source)

    def test_repository_does_no_business_interpretation(self):
        source = self._source(SHR)
        for token in ("strategy_lifecycle", "paper_trading", "execution_verification",
                      "coverage_ratio >", "healthy", "retire", "ORDER BY"):
            self.assertNotIn(token, source)

    def test_the_lifecycle_owner_does_not_depend_on_health(self):
        source = self._source(SL)
        for token in ("strategy_health", "health_snapshot"):
            self.assertNotIn(token, source)

    def test_no_health_policy_or_score_exists_in_r33a(self):
        for module in (SH, SHR, SHV):
            source = self._source(module).lower()
            for token in ("health_score", "strategy_quality", "ranking", "tier",
                          "retirement_policy", "retire_candidate", "no_action"):
                self.assertNotIn(token, source, f"{module.__name__} must not define {token}")


if __name__ == "__main__":
    unittest.main()
