# -*- coding: utf-8 -*-
"""R33-B regressions: the retirement policy recommends, it never mutates.

P1–P15 覆盖：同一快照同一决策、policy version 进指纹、证据不完整一律
INSUFFICIENT_EVIDENCE、UNKNOWN/PARTIAL 永不触发退休、policy 不读原始表、不写 lifecycle、
append-only 幂等与冲突、exact get、以及纯函数在 provider/时钟 poison 下不变。
"""
from __future__ import annotations

import dataclasses
import ast
import os
import pathlib
import sys
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import strategy_health as SH  # noqa: E402
import strategy_retirement_policy as RP  # noqa: E402
import strategy_retirement_repository as RR  # noqa: E402
import strategy_retirement_service as RTV  # noqa: E402
import test_r33a_strategy_health as _A  # noqa: E402

WINDOW = {"observation_start": "2026-09-01", "observation_end": "2026-10-01"}


def _tree(module):
    return ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))


def _imports(module) -> set:
    """Module-level imports via AST.

    Substring scanning is not usable here: this repo's prose deliberately spells
    out the symbols it forbids, so a text match would flag the comments that
    explain the rule instead of a real dependency.
    """
    names = set()
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    return names


def _string_literals(module) -> list:
    return [node.value for node in ast.walk(_tree(module))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def _called_attributes(module) -> set:
    return {node.func.attr for node in ast.walk(_tree(module))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


class _RetirementCase(_A._HealthCase):
    """复用 R33-A 的真实 fixture（临时库 + 真实 exact version + 健康采集）。"""

    def _snapshot(self, **overrides):
        return self._capture(**overrides)

    def _evaluate(self, snapshot_id=None, **overrides):
        values = {"strategy_version": int(self.version.version),
                  "strategy_checksum": self.version.checksum, **WINDOW}
        values.update(overrides)
        snapshot = self._snapshot(**values)
        snapshot_id = snapshot_id or snapshot["snapshot_id"]
        return snapshot, RTV.evaluate_retirement(self.spec.id, snapshot_id=snapshot_id)

    def _synthetic_snapshot(self, *, window=None, **status_overrides):
        """A complete snapshot with all dimensions AVAILABLE (for the NO_ACTION path)."""
        window = SH.HealthObservationWindow(**(window or WINDOW))
        dimensions = []
        for name in SH.DIMENSIONS:
            status = status_overrides.get(name, SH.STATUS_AVAILABLE)
            if status == SH.STATUS_NOT_APPLICABLE:
                dimensions.append(SH.not_applicable_dimension(name, "fixture_not_applicable"))
                continue
            if status == SH.STATUS_UNAVAILABLE:
                dimensions.append(SH.unavailable_dimension(name, f"fixture_{name}_unavailable"))
                continue
            dimensions.append(SH.HealthDimension(
                name=name, status=status, facts={"n": 1},
                provenance=SH.PROVENANCE_OWNER_ISSUED, source_identity=name,
                source_fingerprint="a" * 64))
        return SH.build_strategy_health(
            strategy_id="synthetic", strategy_version=1, strategy_checksum="a" * 64,
            observation_window=window, lifecycle_state="paper", dimensions=dimensions)

    def _lifecycle_projection(self):
        state = self.conn.execute(
            "SELECT strategy_id,strategy_version,state,last_event_id,updated_at"
            " FROM strategy_lifecycle_state ORDER BY strategy_id").fetchall()
        events = self.conn.execute(
            "SELECT event_fingerprint FROM strategy_lifecycle_events ORDER BY id").fetchall()
        return [tuple(row) for row in state], [row[0] for row in events]


class RetirementDecisionContractTests(_RetirementCase):
    """P1–P3、P13：决策身份与指纹。"""

    def test_p1_same_snapshot_input_yields_the_same_decision_fingerprint(self):
        snapshot, first = self._evaluate()
        _, second = self._evaluate(snapshot_id=snapshot["snapshot_id"])
        self.assertEqual(first["decision"]["decision_id"],
                         second["decision"]["decision_id"])
        self.assertEqual(first, second)
        decision = first["decision"]
        self.assertEqual(snapshot["snapshot_id"], decision["snapshot_id"])
        self.assertEqual(snapshot["snapshot_id"], decision["snapshot_fingerprint"])
        self.assertEqual(64, len(decision["decision_id"]))
        self.assertEqual(64, len(decision["decision_fingerprint"]))
        self.assertNotIn("created_at", decision)

    def test_p2_a_different_snapshot_yields_a_different_decision(self):
        _, first = self._evaluate()
        _, other = self._evaluate(observation_start="2026-09-15")
        self.assertNotEqual(first["decision"]["snapshot_id"], other["decision"]["snapshot_id"])
        self.assertNotEqual(first["decision"]["decision_id"], other["decision"]["decision_id"])

    def test_p3_policy_version_is_part_of_the_decision_identity(self):
        snapshot = self._synthetic_snapshot()
        base = RP.evaluate_retirement_policy(snapshot)
        self.assertEqual(RP.RETIREMENT_POLICY_VERSION, base.policy_version)
        next_version = RP.evaluate_retirement_policy(snapshot, policy_version="r33b.v2")
        self.assertNotEqual(base.decision_id, next_version.decision_id)
        # 旧 policy version 的决策不会被新版本改写身份。
        self.assertEqual(base.decision_id, RP.evaluate_retirement_policy(snapshot).decision_id)

    def test_the_snapshot_identity_is_part_of_the_decision_identity(self):
        # 内容相同、身份不同的两份快照必须得到不同决策：snapshot id/fingerprint
        # 必须真的在决策指纹里，而不是只作为一个字段被复制过去。
        base = self._synthetic_snapshot()
        twin = dataclasses.replace(base, snapshot_id="b" * 64, snapshot_fingerprint="b" * 64)
        with mock.patch.object(SH, "verify_snapshot_fingerprint", return_value=True):
            first = RP.evaluate_retirement_policy(base)
            second = RP.evaluate_retirement_policy(twin)
        self.assertEqual(first.decision, second.decision)
        self.assertNotEqual(first.decision_id, second.decision_id)

    def test_the_decision_table_rejects_update_and_delete(self):
        import sqlite3
        self._evaluate()
        for statement in ("UPDATE strategy_retirement_decisions SET decision_type='NO_ACTION'",
                          "DELETE FROM strategy_retirement_decisions"):
            with self.subTest(statement=statement.split()[0]):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(statement)
        self.conn.rollback()

    def test_p13_a_tampered_snapshot_fails_closed(self):
        snapshot = self._synthetic_snapshot()
        torn = dataclasses.replace(snapshot, lifecycle_state="retiring")
        with self.assertRaises(RP.RetirementPolicyError) as raised:
            RP.evaluate_retirement_policy(torn)
        self.assertEqual("health_snapshot_identity_mismatch", str(raised.exception))
        with self.assertRaises(RP.RetirementPolicyError) as raised:
            RP.evaluate_retirement_policy("not-a-snapshot")
        self.assertEqual("canonical_health_snapshot_required", str(raised.exception))

    def test_the_decision_vocabulary_is_action_candidates_only(self):
        self.assertEqual(("NO_ACTION", "DEGRADE_CANDIDATE", "RETIRE_CANDIDATE",
                          "ARCHIVE_READY", "INSUFFICIENT_EVIDENCE"), RP.DECISIONS)
        forbidden = ("BAD_STRATEGY", "FAILED_STRATEGY", "UNHEALTHY", "health_score",
                     "ranking", "tier")
        # 只看真正的字符串字面量（AST），不看解释规则的注释。
        for literal in _string_literals(RP):
            for word in forbidden:
                self.assertNotIn(word, literal)


class RetirementEvidenceGateTests(_RetirementCase):
    """P4–P8：证据闸门与安全规则。"""

    def test_p4_incomplete_evidence_is_insufficient_evidence(self):
        snapshot, result = self._evaluate()
        decision = result["decision"]
        self.assertEqual("INSUFFICIENT_EVIDENCE", decision["decision"])
        self.assertIn("health_dimension_partial:activity_coverage",
                      decision["blocking_reasons"])
        self.assertIsNone(result["transition_proposal"])
        self.assertLess(snapshot["coverage"]["coverage_ratio"], 1.0)

    def test_p5_unavailable_performance_blocks_any_retirement(self):
        _, result = self._evaluate()
        decision = result["decision"]
        self.assertIn("health_dimension_unavailable:performance", decision["blocking_reasons"])
        self.assertNotIn("RETIRE_CANDIDATE", decision["decision"])
        # 换句话说明：performance 缺失不是「收益 0」，而是「不知道」。
        summary = decision["evidence_summary"]["dimensions"]["performance"]
        self.assertEqual("UNAVAILABLE", summary["status"])
        self.assertEqual("UNAVAILABLE", summary["provenance"])

    def test_p5b_an_unavailable_dimension_on_a_complete_snapshot_still_blocks(self):
        snapshot = self._synthetic_snapshot(**{SH.DIMENSION_PERFORMANCE: SH.STATUS_UNAVAILABLE})
        decision = RP.evaluate_retirement_policy(snapshot)
        self.assertEqual("INSUFFICIENT_EVIDENCE", decision.decision)
        self.assertEqual(("health_dimension_unavailable:performance",),
                         decision.blocking_reasons)

    def test_p6_partial_execution_evidence_can_never_retire(self):
        snapshot = self._synthetic_snapshot(
            **{SH.DIMENSION_EXECUTION_EVIDENCE: SH.STATUS_PARTIAL})
        decision = RP.evaluate_retirement_policy(snapshot)
        self.assertEqual("INSUFFICIENT_EVIDENCE", decision.decision)
        self.assertEqual(("health_dimension_partial:execution_evidence",),
                         decision.blocking_reasons)
        self.assertNotEqual("RETIRE_CANDIDATE", decision.decision)

    def test_p7_unknown_risk_evidence_can_never_retire(self):
        snapshot = self._synthetic_snapshot(**{SH.DIMENSION_RISK_EVIDENCE: SH.STATUS_UNAVAILABLE})
        decision = RP.evaluate_retirement_policy(snapshot)
        self.assertEqual("INSUFFICIENT_EVIDENCE", decision.decision)
        self.assertEqual(("health_dimension_unavailable:risk_evidence",),
                         decision.blocking_reasons)
        # 既不能说「风险很好」，也不能说「风险很差」：只有「不知道」。
        self.assertEqual("UNAVAILABLE",
                         decision.evidence_summary["dimensions"]["risk_evidence"]["status"])

    def test_p8_zero_activity_is_a_fact_not_a_retirement_signal(self):
        # 真实快照：窗口内 0 笔订单 —— 仍然不得产出退休候选。
        snapshot, result = self._evaluate()
        activity = next(item for item in snapshot["dimensions"]
                        if item["name"] == SH.DIMENSION_ACTIVITY_COVERAGE)
        self.assertEqual(0, activity["facts"]["orders_in_window"])
        self.assertNotEqual("RETIRE_CANDIDATE", result["decision"]["decision"])
        # 证据完整 + 0 活动 ⇒ NO_ACTION（合法无信号是合法事实）。
        complete = self._synthetic_snapshot()
        self.assertEqual("NO_ACTION", RP.evaluate_retirement_policy(complete).decision)

    def test_a_complete_snapshot_without_signals_is_no_action(self):
        snapshot = self._synthetic_snapshot()
        decision = RP.evaluate_retirement_policy(snapshot)
        self.assertEqual("NO_ACTION", decision.decision)
        self.assertEqual((), decision.blocking_reasons)
        self.assertEqual(tuple(sorted(SH.DIMENSIONS)), decision.required_evidence)
        self.assertEqual(tuple(sorted(SH.DIMENSIONS)), decision.satisfied_evidence)
        self.assertIsNone(RP.build_transition_proposal(
            decision, lifecycle_state="paper", legal_targets=("degraded", "retiring")))

    def test_v1_never_emits_an_action_candidate_for_any_real_snapshot(self):
        # v1 的规则只到 Rule1/Rule2：没有任何 owner 化阈值，因此对真实快照只能
        # 是 INSUFFICIENT_EVIDENCE 或 NO_ACTION，绝不产出候选动作。
        for overrides in ({}, {"observation_start": "2026-09-15"}):
            _, result = self._evaluate(**overrides)
            self.assertIn(result["decision"]["decision"], ("INSUFFICIENT_EVIDENCE", "NO_ACTION"))
            self.assertNotIn(result["decision"]["decision"],
                             tuple(RP.CANDIDATE_TARGET_STATES))
        self.assertEqual((), RP.evaluate_retirement_conditions(self._synthetic_snapshot()))

    def test_conditions_interface_fails_closed_when_a_future_version_emits_them(self):
        # 预留接口：v1 不认识任何条件词表，因此非空条件必须 fail closed，
        # 绝不把未知条件映射成动作。
        with mock.patch.object(RP, "evaluate_retirement_conditions",
                               return_value=("unknown_condition",)):
            with self.assertRaises(RP.RetirementPolicyError) as raised:
                RP.evaluate_retirement_policy(self._synthetic_snapshot())
        self.assertEqual("retirement_conditions_unsupported_in_policy_version",
                         str(raised.exception))


class RetirementTransitionProposalTests(_RetirementCase):
    """§15：proposal 只是结构，且合法性由 lifecycle owner 判定。"""

    def _decision(self, decision_type):
        snapshot = self._synthetic_snapshot()
        base = RP.evaluate_retirement_policy(snapshot)
        return dataclasses.replace(base, decision=decision_type,
                                   decision_id=base.decision_fingerprint)

    def test_proposals_are_described_but_never_executed(self):
        for decision_type, target in (("DEGRADE_CANDIDATE", "degraded"),
                                      ("RETIRE_CANDIDATE", "retiring"),
                                      ("ARCHIVE_READY", "archived")):
            with self.subTest(decision=decision_type):
                proposal = RP.build_transition_proposal(
                    self._decision(decision_type), lifecycle_state="paper",
                    legal_targets=("degraded", "retiring"))
                if target == "archived":
                    # paper → archived 不是合法边：不产出提案（fail closed）。
                    self.assertIsNone(proposal)
                    continue
                self.assertEqual(target, proposal["target_state"])
                self.assertFalse(proposal["executed"])
                self.assertEqual(64, len(proposal["proposal_id"]))
                self.assertIn("decision_id", proposal)

    def test_no_proposal_for_non_candidate_decisions(self):
        for decision_type in ("NO_ACTION", "INSUFFICIENT_EVIDENCE"):
            with self.subTest(decision=decision_type):
                self.assertIsNone(RP.build_transition_proposal(
                    self._decision(decision_type), lifecycle_state="paper",
                    legal_targets=("degraded", "retiring", "archived")))

    def test_the_service_evaluates_the_real_repo_and_proposes_nothing_today(self):
        _, result = self._evaluate()
        self.assertEqual("INSUFFICIENT_EVIDENCE", result["decision"]["decision"])
        self.assertIsNone(result["transition_proposal"])
        self.assertEqual("draft", self.conn.execute(
            "SELECT state FROM strategy_lifecycle_state WHERE strategy_id=?",
            (self.spec.id,)).fetchone()[0])


class RetirementPurityAndSafetyTests(_RetirementCase):
    """P9–P12、P14、P15：纯函数、不写 lifecycle、append-only、无 latest。"""

    def test_p9_the_policy_never_reads_raw_tables(self):
        # 纯函数不可能读表：它连 DB 模块都没 import（AST 判定，不看注释）。
        self.assertEqual({"__future__", "hashlib", "json", "collections", "dataclasses",
                          "types", "strategy_health"}, _imports(RP))
        # 行为验证：评估不依赖任何库文件存在 —— 挪走 paper 库路径后结论不变。
        snapshot = self._synthetic_snapshot()
        baseline = RP.evaluate_retirement_policy(snapshot)
        missing = os.path.join(self.tmp.name, "definitely-missing.sqlite3")
        self.assertFalse(os.path.exists(missing))
        with mock.patch("paper_trading.DB_PATH", missing):
            decision = RP.evaluate_retirement_policy(snapshot)
        self.assertEqual(baseline.decision_id, decision.decision_id)
        self.assertEqual("NO_ACTION", decision.decision)

    def test_p10_the_policy_never_mutates_the_lifecycle(self):
        self.assertNotIn("strategy_lifecycle", _imports(RP))
        # AST：policy 里不存在任何名为 transition 的调用。
        self.assertNotIn("transition", _called_attributes(RP))
        for literal in _string_literals(RP):
            self.assertNotIn("UPDATE strategy_lifecycle", literal)
            self.assertNotIn("INSERT INTO strategy_lifecycle", literal)
        before = self._lifecycle_projection()
        RP.evaluate_retirement_policy(self._synthetic_snapshot())
        self._evaluate()
        self.assertEqual(before, self._lifecycle_projection())

    def test_p11_decision_append_is_idempotent(self):
        _, result = self._evaluate()
        decision = RR.get_decision(self.conn, result["decision"]["decision_id"])
        RR.append_decision(self.conn, decision)
        RR.append_decision(self.conn, decision)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM strategy_retirement_decisions").fetchone()[0]
        self.assertEqual(1, rows)

    def test_p12_same_identity_different_content_conflicts(self):
        _, result = self._evaluate()
        decision = RR.get_decision(self.conn, result["decision"]["decision_id"])
        other = RP.evaluate_retirement_policy(self._synthetic_snapshot(
            window={"observation_start": "2026-08-01", "observation_end": "2026-09-01"}))
        self.assertNotEqual(decision.decision_id, other.decision_id)
        self.conn.execute(
            "INSERT INTO strategy_retirement_decisions(decision_id,decision_fingerprint,"
            "snapshot_id,snapshot_fingerprint,strategy_id,strategy_version,decision_type,"
            "policy_version,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (other.decision_id, other.decision_fingerprint, other.snapshot_id,
             other.snapshot_fingerprint, other.strategy_id, other.strategy_version,
             other.decision, other.policy_version, '{"tampered":true}',
             "2026-10-01T00:00:00+00:00"))
        self.conn.commit()
        with self.assertRaises(RR.StrategyRetirementRepositoryError) as raised:
            RR.append_decision(self.conn, other)
        self.assertEqual("retirement_decision_idempotency_conflict", str(raised.exception))
        self.assertTrue(RP.verify_decision_fingerprint(decision))

    def test_p14_no_latest_fallback_and_exact_get_only(self):
        self.assertEqual({"StrategyRetirementRepositoryError", "append_decision",
                          "get_decision"}, set(RR.__all__))
        _, result = self._evaluate()
        self.assertIsNone(RR.get_decision(self.conn, "f" * 64))
        with self.assertRaises(RR.StrategyRetirementRepositoryError) as raised:
            RR.get_decision(self.conn, "short")
        self.assertEqual("explicit_retirement_decision_id_required", str(raised.exception))
        with self.assertRaises(RP.RetirementPolicyError) as raised:
            RTV.get_retirement_decision(self.spec.id, "f" * 64)
        self.assertEqual("retirement_decision_not_found", str(raised.exception))
        self.assertEqual(result["decision"]["decision_id"],
                         RTV.get_retirement_decision(self.spec.id,
                                                     result["decision"]["decision_id"]
                                                     )["decision_id"])
        for module in (RP, RR, RTV):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            for token in ("get_latest", "latest_decision", "current_retirement_state",
                          "most_recent", "ORDER BY id DESC", "get_current"):
                self.assertFalse(token in source, f"{module.__name__}: {token}")

    def test_p15_provider_ai_and_clock_poison_leave_evaluation_unchanged(self):
        snapshot = self._synthetic_snapshot()
        baseline = RP.evaluate_retirement_policy(snapshot)
        with mock.patch("socket.socket", side_effect=AssertionError("no network")), \
                mock.patch("time.time", side_effect=AssertionError("no clock")), \
                mock.patch("datetime.datetime") as frozen:
            frozen.now.side_effect = AssertionError("no clock")
            poisoned = RP.evaluate_retirement_policy(snapshot)
        self.assertEqual(baseline.decision_id, poisoned.decision_id)
        self.assertEqual(baseline.projection(), poisoned.projection())


class RetirementApiSurfaceTests(_RetirementCase):
    """§16：只接受显式 snapshot id，且没有 status 端点。"""

    def setUp(self):
        super().setUp()
        import api_strategies as API
        import strategy_api_models as Models
        self.API = API
        self.Models = Models

    def test_evaluate_and_exact_get_round_trip(self):
        snapshot = self._snapshot()
        created = self.API.evaluate_strategy_retirement(
            self.spec.id, self.Models.StrategyRetirementEvaluateRequest(
                snapshot_id=snapshot["snapshot_id"]))
        self.assertEqual("INSUFFICIENT_EVIDENCE", created["decision"]["decision"])
        self.assertIsNone(created["transition_proposal"])
        fetched = self.API.get_strategy_retirement_decision(
            self.spec.id, created["decision"]["decision_id"])
        self.assertEqual(created["decision"], fetched)

    def test_unknown_evidence_is_404_and_identity_conflicts_are_400(self):
        from fastapi import HTTPException
        snapshot = self._snapshot()          # 先让表非空：404 不能兜底到「最新一条」
        with self.assertRaises(HTTPException) as raised:
            self.API.evaluate_strategy_retirement(
                self.spec.id, self.Models.StrategyRetirementEvaluateRequest(
                    snapshot_id="f" * 64))
        self.assertEqual(404, raised.exception.status_code)
        with self.assertRaises(HTTPException) as raised:
            self.API.evaluate_strategy_retirement(
                "another_strategy", self.Models.StrategyRetirementEvaluateRequest(
                    snapshot_id=snapshot["snapshot_id"]))
        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("health_snapshot_strategy_mismatch", str(raised.exception.detail))

    def test_the_route_surface_has_no_status_or_latest_endpoint(self):
        paths = {getattr(route, "path", "") for route in self.API.router.routes}
        self.assertIn("/api/strategies/{strategy_id}/retirement/evaluate", paths)
        self.assertIn("/api/strategies/{strategy_id}/retirement/decisions/{decision_id}", paths)
        banned = [path for path in paths
                  if "retirement" in path and ("status" in path or "latest" in path)]
        self.assertEqual([], banned)


class RetirementArchitectureGuardTests(unittest.TestCase):
    """§19：依赖方向与「policy 不是 mutation authority」。"""

    def _source(self, module):
        return pathlib.Path(module.__file__).read_text(encoding="utf-8")

    def test_policy_has_no_io_or_lifecycle_dependency(self):
        self.assertEqual({"__future__", "hashlib", "json", "collections", "dataclasses",
                          "types", "strategy_health"}, _imports(RP))
        self.assertIn("import strategy_health as SH",
                      pathlib.Path(RP.__file__).read_text(encoding="utf-8"))

    def test_repository_does_no_business_interpretation(self):
        self.assertEqual({"__future__", "datetime", "json", "sqlite3",
                          "strategy_retirement_policy"}, _imports(RR))
        for literal in _string_literals(RR):
            for token in ("INSUFFICIENT_EVIDENCE", "NO_ACTION", "ORDER BY", "SELECT *"):
                self.assertNotIn(token, literal)

    def test_service_never_calls_the_lifecycle_transition(self):
        source = pathlib.Path(RTV.__file__).read_text(encoding="utf-8")
        self.assertIn("SL.TRANSITION_TABLE.get(", source)
        self.assertNotIn("transition", _called_attributes(RTV) - {"get"})
        for literal in _string_literals(RTV):
            for token in ("UPDATE strategy_lifecycle", "INSERT INTO strategy_lifecycle"):
                self.assertNotIn(token, literal)

    def test_service_never_reads_raw_ledgers(self):
        self.assertEqual({"__future__", "paper_trading", "strategy_health_repository",
                          "strategy_lifecycle", "strategy_retirement_policy",
                          "strategy_retirement_repository"}, _imports(RTV))
        for literal in _string_literals(RTV):
            for token in ("INSERT INTO paper_", "UPDATE paper_", "DELETE FROM paper_",
                          "paper_orders", "paper_fills", "paper_risk_decisions",
                          "paper_positions", "paper_performance"):
                self.assertNotIn(token, literal)

    def test_the_lifecycle_owner_does_not_depend_on_the_policy(self):
        import strategy_lifecycle
        self.assertNotIn("strategy_retirement", _imports(strategy_lifecycle))

    def test_decision_table_has_no_current_state_column(self):
        import paper_schema_migrations as PSM
        columns = set(PSM.STRATEGY_RETIREMENT_DECISION_COLUMNS)
        self.assertIn("decision_id", columns)
        self.assertEqual(set(), {column for column in columns
                                 if column.startswith(("current", "latest"))})


if __name__ == "__main__":
    unittest.main()
