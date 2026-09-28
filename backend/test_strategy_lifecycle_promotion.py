# -*- coding: utf-8 -*-
"""R31 lifecycle/promotion contract regressions (R31-01 through R31-93)."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_lifecycle as SL
import strategy_promotion as SP
import strategy_registry as SR


DSL = {"op": "gt", "left": {"op": "field", "name": "close"},
       "right": {"op": "indicator", "name": "ma", "window": 20}}
RUN_KEY = "a" * 64
REPORT_KEY = "b" * 64
RESULT_FP = "c" * 64


class StrategyLifecyclePromotionRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="r31-lifecycle-")
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        SR.ensure_schema(self.conn)
        self.spec = SR.create_user_definition(self.conn, "r31_strategy", "R31", dsl_ast=DSL)
        self.version = SR.get_version(self.spec.id, conn=self.conn)
        self.evidence_conn = sqlite3.connect(":memory:")
        self.evidence_conn.row_factory = sqlite3.Row
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.evidence_conn.close()
        self.tmp.cleanup()

    def seed_r29(self, *, validation_status="ready", result_status="completed",
                 stored_strategy_id=None, stored_version=1, stored_checksum=None,
                 runner_version=None):
        import experiment_contract as EC
        import experiment_validation_repository as EVR
        import experiment_validation_runner as R29

        experiment_fp = "f" * 64
        result_fields = {"experiment_fingerprint": experiment_fp, "status": result_status}
        if result_status == "completed":
            result_fields.update(total_return=0.1, max_drawdown=0.02, volatility=0.03,
                turnover=1.0, trade_count=2, total_cost=0.01, exposure=0.5,
                capacity_proxy=1.0, data_coverage=1.0, regime_breakdown={})
        else:
            result_fields["failure_reason"] = "r29_validation_failed"
        result = EC.ExperimentResult(**result_fields)
        validation = {"experiment_fingerprint": experiment_fp, "status": validation_status,
                      "reason_codes": []}
        stored_strategy_id = stored_strategy_id or self.spec.id
        stored_checksum = stored_checksum or self.version.checksum
        identities = {"calendar_fingerprint": "1" * 64,
            "universe_archive_fingerprint": "2" * 64,
            "tradability_evidence_fingerprint": "3" * 64,
            "market_archive_fingerprint": "4" * 64,
            "financial_archive_fingerprint": None,
            "dataset_fingerprint": "5" * 64,
            "strategy_version": {"strategy_id": stored_strategy_id,
                                 "version": stored_version, "checksum": stored_checksum},
            "validation_evidence_fingerprint": EVR._sha(validation)}
        run_key = EVR.ExperimentValidationRepository.build_run_key(
            experiment_fp, identities, runner_version or R29.RUNNER_VERSION)
        EVR.ExperimentValidationRepository(self.evidence_conn).append_run(
            run_key=run_key, experiment_fingerprint=experiment_fp,
            strategy_id=stored_strategy_id, strategy_version=stored_version,
            strategy_checksum=stored_checksum,
            calendar_fingerprint=identities["calendar_fingerprint"],
            universe_archive_fingerprint=identities["universe_archive_fingerprint"],
            financial_archive_fingerprint=None,
            tradability_evidence_fingerprint=identities["tradability_evidence_fingerprint"],
            market_archive_fingerprint=identities["market_archive_fingerprint"],
            dataset_fingerprint=identities["dataset_fingerprint"],
            validation_evidence=validation, result=result.projection(), folds=[],
            runner_version=runner_version or R29.RUNNER_VERSION, runner_code_revision="6" * 40,
            created_at="2026-09-28T00:00:00Z")
        identity = {"strategy_id": self.spec.id, "strategy_version": 1,
                    "strategy_checksum": self.version.checksum}
        return run_key, result.result_fingerprint, identity, experiment_fp

    def seed_r30(self, run_key, result_fingerprint, identity, experiment_fp, *,
                 baseline_identity_run_key=None, report_strategy_version=None,
                 case_status="completed"):
        import robustness_contract as RC
        import robustness_repository as RREP
        import robustness_runner as R30

        plan = RC.RobustnessPlan(baseline_run_key=run_key,
            baseline_experiment_fingerprint=experiment_fp, random_seed=7,
            regime_policy={"policy_version": "r30-test-v1", "benchmark_symbol": "SPY",
                "trend_window_sessions": 2, "bull_threshold": 0.01,
                "bear_threshold": 0.01, "volatility_window_sessions": 2,
                "high_vol_threshold": 0.03, "low_vol_threshold": 0.01},
            cost_stresses=({"commission_multiplier": 2.0},))
        scenario = plan.scenarios()[0]
        metrics = None if case_status != "completed" else {
            "return": 0.1, "drawdown": 0.02, "volatility": 0.03,
            "turnover": 1.0, "trade_count": 2, "cost": 0.01,
            "exposure": 0.5, "capacity_proxy": 1.0, "data_coverage": 1.0}
        case = {"scenario": scenario,
            "evidence": {"masked_observations": 0},
            "result": RC.case_result(scenario_fingerprint=scenario["scenario_fingerprint"],
                status=case_status, metrics=metrics,
                baseline_delta={} if case_status == "completed" else None,
                reason_code="r30_test_case_failed" if case_status != "completed" else None)}
        baseline = {"run_key": baseline_identity_run_key or run_key,
            "result_fingerprint": result_fingerprint,
            "strategy_id": identity["strategy_id"],
            "strategy_version": report_strategy_version or identity["strategy_version"],
            "strategy_checksum": identity["strategy_checksum"]}
        baseline_spec = {"experiment_fingerprint": experiment_fp}
        report_fp = RC.report_fingerprint(baseline_identity={**baseline, "spec": baseline_spec},
            plan_fingerprint=plan.fingerprint, cases=[case])
        report_key = RREP._sha({"baseline_run_key": run_key,
            "baseline_result_fingerprint": result_fingerprint,
            "plan_fingerprint": plan.fingerprint, "runner_version": R30.RUNNER_VERSION})
        report = {"report_key": report_key, "baseline_run_key": run_key,
            "baseline_experiment_fingerprint": experiment_fp,
            "baseline_result_fingerprint": result_fingerprint,
            "plan_fingerprint": plan.fingerprint, "report_fingerprint": report_fp,
            "plan": plan.projection(), "runner_version": R30.RUNNER_VERSION,
            "baseline_identity": baseline, "baseline_spec": baseline_spec,
            "cases": [case], "report_version": RC.REPORT_VERSION,
            "created_at": "2026-09-28T00:00:00Z"}
        RREP.RobustnessRepository(self.evidence_conn).append_report(report)
        return report_key

    def state(self, strategy_id=None, version=None):
        strategy_id = strategy_id or self.spec.id
        version = version or SR.get_version(strategy_id, conn=self.conn)
        return SL.get_state(self.conn, strategy_id, version.version,
                            checksum=version.checksum)["state"]

    def move(self, target, *, strategy_id=None, version=None, expected=None):
        strategy_id = strategy_id or self.spec.id
        version = version or SR.get_version(strategy_id, conn=self.conn)
        current = expected or self.state(strategy_id, version)
        if target in SL.SAFETY_TRANSITION_TARGETS:
            return SL.transition(self.conn, strategy_id=strategy_id,
                strategy_version=version.version, strategy_checksum=version.checksum,
                expected_state=current, target_state=target, actor_type="human",
                actor_id="r31-test", transition_kind="safety",
                reason_code="test_safety", reason_text="R31 safety regression")
        decision = SP.evaluate(self.conn, strategy_id=strategy_id,
            strategy_version=version.version, strategy_checksum=version.checksum,
            from_state=current, target_state=target)
        if not decision.eligible:
            raise AssertionError(f"unexpected blocked decision: {decision.blocking_reasons}")
        return SL.transition(self.conn, strategy_id=strategy_id,
            strategy_version=version.version, strategy_checksum=version.checksum,
            expected_state=current, target_state=target, actor_type="human",
            actor_id="r31-test", promotion_decision=decision.projection())

    def stage(self, target_state):
        path = {"candidate": ("candidate",), "research": ("candidate", "research"),
                "validated": ("candidate", "research", "validated"),
                "shadow": ("candidate", "research", "validated", "shadow")}[target_state]
        for target in path:
            if target == "validated":
                version = self.version
                run = {"result": {"result_fingerprint": RESULT_FP},
                       "payload_fingerprint": "d" * 64}
                with mock.patch.object(SP, "_verify_r29", return_value=(run, None)):
                    decision = SP.evaluate(self.conn, strategy_id=self.spec.id,
                        strategy_version=version.version, strategy_checksum=version.checksum,
                        from_state="research", target_state="validated",
                        evidence_bundle={"r29_run_key": RUN_KEY}, evidence_conn=self.conn)
                self.assertTrue(decision.eligible, decision.blocking_reasons)
                SL.transition(self.conn, strategy_id=self.spec.id,
                    strategy_version=version.version, strategy_checksum=version.checksum,
                    expected_state="research", target_state="validated", actor_type="human",
                    actor_id="r31-test", promotion_decision=decision.projection())
            elif target == "shadow":
                version = self.version
                run = {"result": {"result_fingerprint": RESULT_FP},
                       "payload_fingerprint": "d" * 64}
                report = {"report_fingerprint": "e" * 64}
                with mock.patch.object(SP, "_verify_r29", return_value=(run, None)), \
                     mock.patch.object(SP, "_verify_r30", return_value=(report, None)):
                    decision = SP.evaluate(self.conn, strategy_id=self.spec.id,
                        strategy_version=version.version, strategy_checksum=version.checksum,
                        from_state="validated", target_state="shadow",
                        evidence_bundle={"r29_run_key": RUN_KEY, "r30_report_key": REPORT_KEY},
                        evidence_conn=self.conn)
                self.assertTrue(decision.eligible, decision.blocking_reasons)
                SL.transition(self.conn, strategy_id=self.spec.id,
                    strategy_version=version.version, strategy_checksum=version.checksum,
                    expected_state="validated", target_state="shadow", actor_type="human",
                    actor_id="r31-test", promotion_decision=decision.projection())
            else:
                self.move(target)

    def _case(self, number):
        if number == 1:
            self.assertEqual(set(SL.STATES), {"draft", "candidate", "research", "validated", "shadow",
                "paper", "production_sim", "degraded", "paused", "retiring", "archived", "rejected",
                "validation_failed", "quarantined"})
        elif number == 2:
            self.assertIn("candidate", SL.TRANSITION_TABLE["draft"])
            self.assertIn("research", SL.TRANSITION_TABLE["candidate"])
            self.assertIn("validated", SL.TRANSITION_TABLE["research"])
            self.assertIn("shadow", SL.TRANSITION_TABLE["validated"])
            self.assertIn("paper", SL.TRANSITION_TABLE["shadow"])
            self.assertIn("production_sim", SL.TRANSITION_TABLE["paper"])
            self.assertEqual(SL.TRANSITION_TABLE["draft"], frozenset({"candidate", "archived", "quarantined"}))
        elif number == 3:
            with self.assertRaisesRegex(SL.LifecycleError, "invalid_lifecycle_transition"):
                SL.transition(self.conn, strategy_id=self.spec.id, strategy_version=1,
                    strategy_checksum=self.version.checksum, expected_state="draft", target_state="paper",
                    actor_type="human", actor_id="test", transition_kind="safety",
                    reason_code="x", reason_text="x")
        elif number == 4:
            self.assertEqual(SL.TRANSITION_TABLE["archived"], frozenset())
        elif number == 5:
            outcomes = []
            barrier = threading.Barrier(2)
            def writer(target):
                conn = sqlite3.connect(self.path, timeout=10)
                conn.row_factory = sqlite3.Row
                try:
                    barrier.wait(timeout=5)
                    version = SR.get_version("tq_breakout", conn=conn)
                    SL.transition(conn, strategy_id="tq_breakout", strategy_version=version.version,
                        strategy_checksum=version.checksum, expected_state="paper", target_state=target,
                        actor_type="human", actor_id="race", transition_kind="safety",
                        reason_code="race", reason_text="concurrent transition")
                    outcomes.append("won")
                except SL.LifecycleError as exc:
                    outcomes.append(str(exc))
                finally:
                    conn.close()
            threads = [threading.Thread(target=writer, args=(target,))
                       for target in ("paused", "quarantined")]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=15)
            self.assertEqual(1, outcomes.count("won"), outcomes)
            self.assertEqual(1, outcomes.count("strategy_lifecycle_conflict"), outcomes)
            self.assertEqual(2, len(SL.history(self.conn, "tq_breakout")))
        elif number in (6, 7, 8):
            kwargs = {"strategy_id": self.spec.id, "strategy_version": 1,
                "strategy_checksum": self.version.checksum, "expected_state": "candidate",
                "target_state": "quarantined", "actor_type": "human", "actor_id": "test",
                "transition_kind": "safety", "reason_code": "test", "reason_text": "test"}
            if number == 6:
                SR.save_definition(self.conn, self.spec.id, {"description": "advance current head"})
                kwargs["expected_state"] = "draft"
            elif number == 7: kwargs["strategy_checksum"] = "0" * 64
            elif number == 8: kwargs["actor_type"] = "ai"
            expected_reason = {6: "strategy_version_changed", 7: "strategy_checksum_mismatch",
                               8: "ai_cannot_apply_transition"}[number]
            with self.assertRaisesRegex(SL.LifecycleError, expected_reason):
                SL.transition(self.conn, **kwargs)
        elif number == 9:
            before = len(SL.history(self.conn, self.spec.id))
            self.conn.execute("CREATE TRIGGER r31_fail_state BEFORE UPDATE ON strategy_lifecycle_state BEGIN SELECT RAISE(ABORT,'forced'); END")
            self.conn.execute("BEGIN")
            with self.assertRaises(sqlite3.IntegrityError): self.move("quarantined")
            self.assertEqual(before, len(SL.history(self.conn, self.spec.id)))
            self.assertEqual("draft", self.state())
        elif number == 10:
            self.move("quarantined")
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute("UPDATE strategy_lifecycle_events SET reason_text='tamper'")
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute("DELETE FROM strategy_lifecycle_events")
        elif number == 11:
            self.assertEqual("draft", self.state())
            self.assertEqual(self.version.checksum,
                SL.get_state(self.conn, self.spec.id, 1, checksum=self.version.checksum)["strategy_checksum"])
        elif number in (12, 13, 68, 69):
            self.move("quarantined")
            old = self.version
            new = SR.save_definition(self.conn, self.spec.id, {"description": "new immutable version"})
            self.assertEqual(2, new.version)
            self.assertEqual("draft", self.state(version=new))
            self.assertEqual("quarantined", self.state(version=old))
        elif number in (14, 17, 63, 93):
            self.assertFalse(SL.allows_formal_cycle("shadow" if number in (14, 93) else "quarantined"))
        elif number in (15, 16):
            self.assertTrue(SL.allows_formal_cycle("paper" if number == 15 else "production_sim"))
        elif number == 18:
            self.assertEqual("draft", SL.LEGACY_STATE_MAP["draft"])
        elif number == 19:
            self.assertEqual("validated", SL.LEGACY_STATE_MAP["validated"])
        elif number == 20:
            self.assertEqual("paper", SL.LEGACY_STATE_MAP["active"])
        elif number == 21:
            self.assertEqual("paused", SL.LEGACY_STATE_MAP["paused"])
        elif number == 22:
            self.assertEqual("retiring", SL.LEGACY_STATE_MAP["retiring"])
        elif number == 23:
            self.assertEqual("archived", SL.LEGACY_STATE_MAP["archived"])
        elif number == 24:
            self.assertNotIn("candidate", SL.LEGACY_STATE_MAP.values())
            self.assertNotIn("research", SL.LEGACY_STATE_MAP.values())
            self.assertNotIn("shadow", SL.LEGACY_STATE_MAP.values())
        elif number == 25:
            self.assertTrue(all(SL.allows_formal_cycle(row.status)
                for row in SR.list_definitions(conn=self.conn) if row.origin == "builtin"))
        elif number == 26:
            self.conn.execute("UPDATE strategy_definitions SET lifecycle_status='active',supports_new_cycle=1 WHERE id=?",
                              (self.spec.id,))
            self.assertEqual("draft", SR.get(self.spec.id, conn=self.conn).status)
            self.assertNotIn(self.spec.id, SR.active_ids(conn=self.conn))
        elif number == 27:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(root, "backend", "strategy_registry.py"), encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("def transition(", source)
            self.assertNotIn("_TRANSITIONS", source)
            self.assertNotIn("INSERT INTO strategy_definition_events", source)
            physical = self.conn.execute("SELECT lifecycle_status,supports_new_cycle FROM strategy_definitions WHERE id='r31_strategy'").fetchone()
            self.assertEqual("draft", physical[0])
            self.assertFalse(bool(physical[1]))
        elif number == 28:
            result = SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                strategy_checksum=self.version.checksum, from_state="draft", target_state="candidate")
            self.assertTrue(result.eligible, result.blocking_reasons)
        elif number == 29:
            with self.assertRaises(ValueError): SR.create_user_definition(self.conn, "r31_bad", "bad", dsl_ast={"op": "not-real"})
            with mock.patch.object(SR, "runtime_readiness", return_value={"runtime_ready": False}):
                blocked = SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                    strategy_checksum=self.version.checksum, from_state="draft", target_state="candidate")
            self.assertFalse(blocked.eligible)
            self.assertIn("strategy_runtime_not_ready", blocked.blocking_reasons)
        elif number == 30:
            self.move("candidate")
            self.assertTrue(SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                strategy_checksum=self.version.checksum, from_state="candidate", target_state="research").eligible)
        elif number in range(31, 46):
            if number <= 36:
                self.stage("research")
                run = {"result": {"result_fingerprint": RESULT_FP}, "payload_fingerprint": "d" * 64}
                reason = {32: "r29_evidence_unavailable", 33: "r29_result_not_completed",
                          34: "r29_identity_or_status_mismatch", 35: "r29_identity_or_status_mismatch",
                          36: "r29_run_not_found"}.get(number)
                if number == 31:
                    run_key, _, _, _ = self.seed_r29()
                    decision = SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                        strategy_checksum=self.version.checksum, from_state="research", target_state="validated",
                        evidence_bundle={"r29_run_key": run_key}, evidence_conn=self.evidence_conn)
                    self.assertTrue(decision.eligible, decision.blocking_reasons)
                elif number in (32, 33, 34, 35):
                    run_key, _, _, _ = self.seed_r29(
                        validation_status="blocked" if number == 32 else "ready",
                        result_status="failed" if number == 33 else "completed",
                        stored_version=2 if number == 34 else 1,
                        stored_checksum="0" * 64 if number == 35 else None)
                    decision = SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                        strategy_checksum=self.version.checksum, from_state="research", target_state="validated",
                        evidence_bundle={"r29_run_key": run_key}, evidence_conn=self.evidence_conn)
                    self.assertFalse(decision.eligible, decision.blocking_reasons)
                    expected = {32: "r29_identity_or_status_mismatch",
                                33: "r29_result_not_completed",
                                34: "r29_identity_or_status_mismatch",
                                35: "r29_identity_or_status_mismatch"}[number]
                    self.assertIn(expected, decision.blocking_reasons)
                elif number == 36:
                    run_key, _, _, _ = self.seed_r29(runner_version="legacy-backtest-v0")
                    decision = SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                        strategy_checksum=self.version.checksum, from_state="research", target_state="validated",
                        evidence_bundle={"r29_run_key": run_key}, evidence_conn=self.evidence_conn)
                    self.assertFalse(decision.eligible)
                    self.assertIn("r29_run_identity_invalid", decision.blocking_reasons)
                else:
                    verified = (None, "r29_evidence_unavailable") if number == 32 else (None, reason)
                    with mock.patch.object(SP, "_verify_r29", return_value=verified):
                        decision = SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                            strategy_checksum=self.version.checksum, from_state="research", target_state="validated",
                            evidence_bundle={"r29_run_key": RUN_KEY}, evidence_conn=self.conn)
                    self.assertFalse(decision.eligible)
            elif number <= 41:
                self.stage("validated")
                run_key, result_fp, identity, experiment_fp = self.seed_r29()
                report_kwargs = {}
                if number == 38: report_kwargs["baseline_identity_run_key"] = "9" * 64
                if number == 39: report_kwargs["report_strategy_version"] = 2
                if number == 40: report_kwargs["case_status"] = "failed"
                if number == 41: report_kwargs["case_status"] = "unavailable"
                report_key = self.seed_r30(run_key, result_fp, identity, experiment_fp,
                                           **report_kwargs)
                decision = SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                    strategy_checksum=self.version.checksum, from_state="validated", target_state="shadow",
                    evidence_bundle={"r29_run_key": run_key, "r30_report_key": report_key},
                    evidence_conn=self.evidence_conn)
                if number == 37:
                    self.assertTrue(decision.eligible, decision.blocking_reasons)
                else:
                    self.assertFalse(decision.eligible)
            elif number == 42:
                self.move("candidate")
                decision = SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                    strategy_checksum=self.version.checksum, from_state="candidate", target_state="research")
                self.assertTrue(decision.eligible, decision.blocking_reasons)
            elif number == 43:
                with self.assertRaises(SP.PromotionError):
                    SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                        strategy_checksum=self.version.checksum, from_state="draft", target_state="candidate",
                        evidence_bundle={"validated": True, "return": 100, "win_rate": 1.0})
            elif number == 44:
                self.stage("research")
                decision = SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                    strategy_checksum=self.version.checksum, from_state="research", target_state="validated")
                self.assertIn("exact_r29_run_key_required", decision.blocking_reasons)
            else:
                self.stage("validated")
                run = {"result": {"result_fingerprint": RESULT_FP}, "payload_fingerprint": "d" * 64}
                with mock.patch.object(SP, "_verify_r29", return_value=(run, None)):
                    decision = SP.evaluate(self.conn, strategy_id=self.spec.id, strategy_version=1,
                        strategy_checksum=self.version.checksum, from_state="validated", target_state="shadow",
                        evidence_bundle={"r29_run_key": RUN_KEY}, evidence_conn=self.conn)
                self.assertIn("exact_r30_report_key_required", decision.blocking_reasons)
        elif number in range(46, 53):
            self.move("candidate")
            kwargs = dict(strategy_id=self.spec.id, strategy_version=1,
                strategy_checksum=self.version.checksum, from_state="candidate", target_state="research",
                evidence_bundle={}, proposer_type="ai" if number in (51, 52) else "human",
                proposer_id="r31", rationale="proposal", created_at="2026-01-01T00:00:00Z")
            proposal = SP.create_proposal(self.conn, **kwargs)
            self.assertEqual("candidate", proposal["from_state"])
            if number == 46:
                same_checksum_other_version = SP.create_proposal(self.conn,
                    strategy_id=self.spec.id, strategy_version=2,
                    strategy_checksum=self.version.checksum, from_state="candidate",
                    target_state="research", evidence_bundle={}, proposer_type="human",
                    proposer_id="r31", rationale="proposal")
                self.assertNotEqual(proposal["proposal_fingerprint"],
                                    same_checksum_other_version["proposal_fingerprint"])
                self.assertEqual(proposal["proposal_fingerprint"],
                    SP.create_proposal(self.conn, **kwargs)["proposal_fingerprint"])
                SR.save_definition(self.conn, self.spec.id, {"description": "version 2"})
                newer = SR.get_version(self.spec.id, conn=self.conn)
                other = SP.create_proposal(self.conn, strategy_id=self.spec.id,
                    strategy_version=newer.version, strategy_checksum=newer.checksum,
                    from_state="draft", target_state="candidate", evidence_bundle={},
                    proposer_type="human", proposer_id="r31", rationale="proposal")
                self.assertNotEqual(proposal["proposal_fingerprint"], other["proposal_fingerprint"])
            if number == 47:
                self.move("research")
                first = SP.create_proposal(self.conn, strategy_id=self.spec.id,
                    strategy_version=1, strategy_checksum=self.version.checksum,
                    from_state="research", target_state="validated",
                    evidence_bundle={"r29_run_key": "a" * 64}, proposer_type="human", proposer_id="r31")
                second = SP.create_proposal(self.conn, strategy_id=self.spec.id,
                    strategy_version=1, strategy_checksum=self.version.checksum,
                    from_state="research", target_state="validated",
                    evidence_bundle={"r29_run_key": "f" * 64}, proposer_type="human", proposer_id="r31")
                self.assertNotEqual(first["proposal_fingerprint"], second["proposal_fingerprint"])
                repeated = SP.create_proposal(self.conn, strategy_id=self.spec.id,
                    strategy_version=1, strategy_checksum=self.version.checksum,
                    from_state="research", target_state="validated",
                    evidence_bundle={"r29_run_key": "f" * 64}, proposer_type="human", proposer_id="r31",
                    created_at="2027-01-01T00:00:00Z")
                self.assertEqual(second["proposal_fingerprint"], repeated["proposal_fingerprint"])
            if number == 48:
                repeated = SP.create_proposal(self.conn, **{**kwargs, "created_at": "2027-01-01T00:00:00Z"})
                self.assertEqual(proposal["proposal_fingerprint"], repeated["proposal_fingerprint"])
            if number == 49:
                with self.assertRaises(SP.PromotionError):
                    SP.create_proposal(self.conn, **{**kwargs, "rationale": "changed rationale"})
            if number == 50:
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute("UPDATE strategy_promotion_proposals SET rationale='tamper'")
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute("DELETE FROM strategy_promotion_proposals")
            if number == 52:
                with self.assertRaisesRegex(SP.PromotionError, "ai_cannot_apply_transition"):
                    SP.apply_proposal(self.conn, None, proposal, actor_type="ai", actor_id="ai")
        elif number in (53, 54, 55, 56, 57, 58):
            self.move("candidate")
            proposal = SP.create_proposal(self.conn, strategy_id=self.spec.id,
                strategy_version=1, strategy_checksum=self.version.checksum,
                from_state="candidate", target_state="research", evidence_bundle={},
                proposer_type="human", proposer_id="test")
            if number == 54: self.move("quarantined")
            if number == 55:
                SR.save_definition(self.conn, self.spec.id, {"description": "new head"})
            if number == 56:
                proposal["evidence_bundle"] = {"r29_run_key": "invalid"}
            if number == 53:
                proposal["decision"]["decision_fingerprint"] = "0" * 64
                with self.assertRaisesRegex(SP.PromotionError, "promotion_proposal_stale_or_blocked"):
                    SP.apply_proposal(self.conn, self.conn, proposal,
                                      actor_type="human", actor_id="test")
            elif number == 57:
                with mock.patch.object(SP, "_verify_r29", return_value=(None, "r29_evidence_corrupt")):
                    run, reason = SP._verify_r29(self.conn, RUN_KEY, {"strategy_id": self.spec.id,
                        "strategy_version": 1, "strategy_checksum": self.version.checksum})
                self.assertIsNone(run)
                self.assertEqual("r29_evidence_corrupt", reason)
            elif number == 58:
                with mock.patch.object(SP, "_verify_r30", return_value=(None, "r30_evidence_corrupt")):
                    report, reason = SP._verify_r30(self.conn, REPORT_KEY, RUN_KEY,
                        {"strategy_id": self.spec.id, "strategy_version": 1,
                         "strategy_checksum": self.version.checksum}, RESULT_FP)
                self.assertIsNone(report)
                self.assertEqual("r30_evidence_corrupt", reason)
            else:
                with self.assertRaises((SP.PromotionError, SL.LifecycleError)):
                    SP.apply_proposal(self.conn, self.conn, proposal, actor_type="human", actor_id="test")
        elif number in (59, 60, 61, 62, 64, 65, 66, 67):
            version = SR.get_version("tq_breakout", conn=self.conn)
            def safety(target, actor="human", reason=True, expected="paper"):
                return SL.transition(self.conn, strategy_id="tq_breakout", strategy_version=version.version,
                    strategy_checksum=version.checksum, expected_state=expected, target_state=target,
                    actor_type=actor, actor_id="r31", transition_kind="safety",
                    reason_code="test" if reason else "", reason_text="test" if reason else "")
            if number in (59, 60, 62):
                target = "quarantined" if number == 60 else "paused"
                safety(target, actor="system" if number == 60 else "human")
                self.assertEqual(target, SL.get_state(self.conn, "tq_breakout", 1)["state"])
            elif number == 61:
                with self.assertRaisesRegex(SL.LifecycleError, "ai_cannot_apply_transition"):
                    safety("paused", actor="ai")
            elif number == 64:
                with self.assertRaisesRegex(SL.LifecycleError, "quarantine_release_evidence_missing"):
                    SL.transition(self.conn, strategy_id=self.spec.id, strategy_version=1,
                        strategy_checksum=self.version.checksum, expected_state="quarantined", target_state="paper",
                        actor_type="human", actor_id="r31", transition_kind="safety",
                        reason_code="test", reason_text="test")
            elif number == 65:
                safety("retiring")
                with self.assertRaises(SL.LifecycleError):
                    safety("paper", expected="retiring")
            elif number == 66:
                safety("retiring")
                safety("archived", expected="retiring")
                self.assertEqual("archived", SL.get_state(self.conn, "tq_breakout", 1)["state"])
            else:
                self.move("candidate")
                self.move("rejected")
                with self.assertRaises(SL.LifecycleError):
                    SL.transition(self.conn, strategy_id=self.spec.id, strategy_version=1,
                        strategy_checksum=self.version.checksum, expected_state="rejected", target_state="paper",
                        actor_type="human", actor_id="r31", transition_kind="safety",
                        reason_code="test", reason_text="test")
        elif number == 68:
            builtin = SR.get_version("tq_breakout", conn=self.conn)
            SR.bind_cycle_versions(self.conn, 99, ["tq_breakout"])
            old_stamp = SR.cycle_stamp_for_account(self.conn, "tq_breakout", cycle_id=99)
            new = SR.save_definition(self.conn, "tq_breakout", {"description": "edited after paper"})
            self.assertEqual("paper", SL.get_state(self.conn, "tq_breakout", builtin.version)["state"])
            self.assertEqual("draft", SL.get_state(self.conn, "tq_breakout", new.version)["state"])
            self.assertEqual(old_stamp, SR.cycle_stamp_for_account(self.conn, "tq_breakout", cycle_id=99))
        elif number == 69:
            builtin = SR.get_version("tq_breakout", conn=self.conn)
            SR.bind_cycle_versions(self.conn, 100, ["tq_breakout"])
            stamp = SR.cycle_stamp_for_account(self.conn, "tq_breakout", cycle_id=100)
            SR.save_definition(self.conn, "tq_breakout", {"description": "v2"})
            self.assertEqual(("tq_breakout", builtin.version, builtin.checksum), stamp)
        elif number == 70:
            self.move("candidate")
            proposal = SP.create_proposal(self.conn, strategy_id=self.spec.id,
                strategy_version=1, strategy_checksum=self.version.checksum,
                from_state="candidate", target_state="research", evidence_bundle={},
                proposer_type="human", proposer_id="test")
            SR.save_definition(self.conn, self.spec.id, {"description": "new exact version"})
            with self.assertRaises((SP.PromotionError, SL.LifecycleError)):
                SP.apply_proposal(self.conn, self.conn, proposal, actor_type="human", actor_id="test")
        elif number == 71:
            self.move("candidate")
            self.assertTrue(SL.has_left_initial_draft(self.conn, self.spec.id, 1))
            with self.assertRaises(ValueError): SR.hard_delete_unused_draft(self.conn, self.spec.id)
        elif number in range(72, 84):
            import paper_trading as P
            from strategy_service import lifecycle_read_model
            with mock.patch.object(P, "DB_PATH", self.path):
                model = lifecycle_read_model("tq_breakout")
            for key in ("state", "version", "checksum", "state_history", "legal_transitions",
                        "eligible_transitions", "blocked_transitions", "blocking_reasons",
                        "required_evidence", "evidence", "proposals", "formal_cycle_allowed"):
                self.assertIn(key, model)
            self.assertIsInstance(model["state_history"], list)
            if number == 81:
                before = len(SL.history(self.conn, "tq_breakout"))
                lifecycle_read_model("tq_breakout")
                self.assertEqual(before, len(SL.history(self.conn, "tq_breakout")))
            elif number == 82:
                self.assertEqual([], model["proposals"])
            elif number == 83:
                root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                with open(os.path.join(root, "backend", "api_strategies.py"), encoding="utf-8") as handle:
                    api_source = handle.read()
                self.assertNotIn("TRANSITION_TABLE", api_source)
                self.assertNotIn("PROMOTION_RULES", api_source)
        elif number in range(84, 94):
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(root, "frontend", "src", "features", "strategies.js"), encoding="utf-8") as handle:
                source = handle.read()
            checks = {
                84: ("lifecycle.state",),
                85: ('data-testid="strategy-lifecycle-history"', "strategy-lifecycle-event"),
                86: ("lifecycle.eligible_transitions",),
                87: ("lifecycle.blocking_reasons",),
                88: ("decision.blocking_reasons",),
                89: ("var evidence='<small>R29 '+adaptiveEsc(bundle.r29_run_key||'未指定')+' · R30 '+adaptiveEsc(bundle.r30_report_key||'未指定')+'</small>';",),
                90: ("PROPOSAL", "proposal.proposer_type"),
                91: ("lifecycle.eligible_transitions", "function wbValidateAndMark"),
                92: ("proposal.proposer_type!=='ai'", "function wbValidateAndMark"),
                93: ("lifecycle.formal_cycle_allowed",),
            }
            for token in checks[number]:
                if token.startswith("function "):
                    self.assertNotIn(token, source)
                else:
                    self.assertIn(token, source)
            if number == 93:
                self.assertFalse(SL.allows_formal_cycle("shadow"))


_LABELS = [
    "full_state_vocabulary", "explicit_main_edges", "invalid_draft_to_paper", "archived_terminal",
    "expected_state_cas", "exact_version", "exact_checksum", "ai_cannot_apply", "event_state_atomic",
    "event_ledger_append_only", "state_exact_version_bound", "new_version_starts_draft", "old_history_preserved",
    "shadow_not_formal", "paper_formal", "production_sim_formal", "unsafe_states_not_formal",
    "legacy_draft_import", "legacy_validated_import", "legacy_active_to_paper", "legacy_paused_import",
    "legacy_retiring_import", "legacy_archived_import", "migration_no_fake_forward_history", "builtin_paper_compatible",
    "legacy_columns_non_authority", "support_flag_non_authority", "candidate_requires_exact_version", "invalid_dsl_blocks_candidate",
    "candidate_research_no_performance", "research_validated_exact_r29", "r29_blocked_not_validated", "r29_failed_not_validated",
    "r29_version_mismatch_blocked", "r29_checksum_mismatch_blocked", "legacy_backtest_no_authority", "validated_shadow_exact_r30",
    "r30_baseline_run_exact", "r30_version_identity_exact", "r30_failed_case_blocks", "r30_unavailable_case_blocks",
    "performance_score_not_authority", "caller_boolean_not_authority", "no_latest_r29_fallback", "no_latest_r30_fallback",
    "proposal_fingerprint_deterministic", "created_at_excluded", "same_proposal_idempotent", "same_key_payload_conflict",
    "proposal_append_only", "ai_can_propose", "ai_cannot_apply", "apply_recomputes_policy", "state_change_stales_proposal",
    "new_version_stales_proposal", "wrong_evidence_ref_blocked", "corrupt_r29_blocked", "corrupt_r30_blocked",
    "human_pause_reasoned", "system_quarantine_reasoned", "ai_cannot_pause", "pause_no_positive_evidence",
    "quarantine_no_formal_cycle", "quarantine_release_blocked", "retiring_no_paper_return", "retiring_archived",
    "rejected_no_silent_paper", "v3_paper_v4_draft", "pinned_v3_unchanged", "old_evidence_cannot_promote_v4",
    "hard_delete_uses_lifecycle_history", "api_current_state", "api_legal_transitions", "api_blocking_reasons",
    "api_evidence_refs", "transition_requires_version_checksum", "transition_requires_expected_state",
    "promotion_requires_proposal_fingerprint", "safety_requires_reason", "stale_proposal_stable_error",
    "read_does_not_mutate_state", "read_does_not_create_proposal", "api_no_policy_rules", "frontend_current_state",
    "frontend_state_history", "frontend_eligible_transitions", "frontend_blocking_reasons", "frontend_missing_evidence",
    "frontend_exact_evidence_identity", "proposal_rendered_as_proposal", "frontend_no_eligibility_calculation",
    "ai_proposal_not_applied", "shadow_not_formal_capital",
]


def _make_test(number):
    def test(self):
        self._case(number)
    test.__name__ = f"test_R31_{number:02d}_{_LABELS[number - 1]}"
    test.__doc__ = f"Permanent R31 regression {number:02d}: {_LABELS[number - 1]}"
    return test


for _number in range(1, 94):
    setattr(StrategyLifecyclePromotionRegressions, f"test_R31_{_number:02d}_{_LABELS[_number - 1]}",
            _make_test(_number))


if __name__ == "__main__":
    unittest.main()
