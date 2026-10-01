# -*- coding: utf-8 -*-
"""R32 Final: Lifecycle Promotion consumes one exact ShadowComparisonReport.

The report is built and appended by the R32-D/E1 owner machinery, and the policy
reads exactly the record the owner persists. Nothing here searches for a latest
comparison, a current challenger or a most recent shadow run, and nothing here
compares one strategy's performance against another.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import execution_planner as EP  # noqa: E402
import paper_schema_migrations as PSM  # noqa: E402
import paper_trading as PT  # noqa: E402
import shadow_comparison as SC  # noqa: E402
import shadow_comparison_repository as SCR  # noqa: E402
import shadow_comparison_service as SCS  # noqa: E402
import shadow_runtime as SH  # noqa: E402
import strategy_lifecycle as SL  # noqa: E402
import strategy_promotion as SP  # noqa: E402
import strategy_registry as SR  # noqa: E402
import strategy_runtime as SRT  # noqa: E402
import strategy_service as SVC  # noqa: E402
import test_shadow_comparison as _CT  # noqa: E402

DSL = {"op": "gt", "left": {"op": "field", "name": "close"},
       "right": {"op": "const", "value": 0}}
DEFINITION = {"dsl_ast": DSL}
STRATEGY_ID = "r32_final_strategy"


class _PromotionFixture:
    """Shared fixture: a real paper database plus one real comparison report.

    Not a TestCase: the promotion and workspace regressions both inherit it, and
    its helper methods are the only thing either one reuses.
    """

    # The comparison fixture owns the exact environment, ShadowRun and report
    # builder; only its methods are reused, never its test cases.
    _shadow_run = _CT.ShadowComparisonTests._shadow_run
    _ledger = _CT.ShadowComparisonTests._ledger
    _comparison_spec = _CT.ShadowComparisonTests._comparison_spec
    _link_risk_decision = _CT.ShadowComparisonTests._link_risk_decision
    _execution_evidence = _CT.ShadowComparisonTests._execution_evidence
    _risk_payload = _CT.ShadowComparisonTests._risk_payload
    _signal_row = _CT.ShadowComparisonTests._signal_row
    _active_runtime_context = _CT.ShadowComparisonTests._active_runtime_context

    def setUp(self):
        _CT.ShadowComparisonTests.setUp(self)
        self.tmp = tempfile.TemporaryDirectory(prefix="r32-final-", ignore_cleanup_errors=True)
        self.paper_path = os.path.join(self.tmp.name, "paper.sqlite3")
        self.paper = sqlite3.connect(self.paper_path, timeout=10)
        self.paper.row_factory = sqlite3.Row
        SR.ensure_schema(self.paper)
        self.spec = SR.create_user_definition(self.paper, STRATEGY_ID, "R32 Final", dsl_ast=DSL)
        self.version = SR.get_version(self.spec.id, conn=self.paper)
        self.paper.commit()
        PSM.ensure_shadow_comparison_reports(self.paper)
        # Re-pin the Challenger leg onto the registry's exact immutable version, so
        # the report carries a real stamp the policy can verify.
        self.strategy_version = types.SimpleNamespace(
            strategy_id=self.spec.id, version=int(self.version.version),
            checksum=self.version.checksum, definition=DEFINITION)
        self.risk_policy = SRT.risk_policy_projection_for_definition(DEFINITION)
        self._pin_challenger(self._exact_challenger_stamp())
        self._seed_state("shadow")

    def tearDown(self):
        self.paper.close()
        self.tmp.cleanup()

    # ── fixture helpers ──────────────────────────────────────────────────────

    def _exact_challenger_stamp(self):
        return SH.StrategyStamp(self.spec.id, int(self.version.version),
                                self.version.checksum)

    def _pin_challenger(self, stamp):
        """Rebuild the Challenger leg and its ShadowRun on one exact stamp."""
        self.challenger = stamp
        self.execution_policy = EP.execution_policy_snapshot(stamp.strategy_id)
        self.shadow_spec = SH.ShadowRunSpec(
            challenger=stamp, active_comparator=self.active,
            environment_fingerprint=self.identity.environment_fingerprint,
            session_date=self.day, decision_at=self.decision_at, reference_capital=100_000.0)
        self.shadow_run = self._shadow_run((self.candidate,))

    def _pin_foreign_challenger(self, strategy_id, version, checksum):
        """Pin the Challenger leg onto another strategy's exact stamp."""
        self.strategy_version = types.SimpleNamespace(
            strategy_id=strategy_id, version=int(version), checksum=checksum,
            definition=DEFINITION)
        self._pin_challenger(SH.StrategyStamp(strategy_id, int(version), checksum))

    def _restore_challenger(self):
        """Pin the Challenger leg back onto the registry's exact version."""
        self.strategy_version = types.SimpleNamespace(
            strategy_id=self.spec.id, version=int(self.version.version),
            checksum=self.version.checksum, definition=DEFINITION)
        self._pin_challenger(self._exact_challenger_stamp())

    def _seed_state(self, state):
        """Fixture seed of the lifecycle state.

        The lifecycle walk itself is covered by the R31 regressions; these tests
        are about which *evidence* the policy accepts, so only the starting state
        is seeded. Every write under test still goes through ``strategy_lifecycle``.
        """
        self.paper.execute(
            "UPDATE strategy_lifecycle_state SET state=? WHERE strategy_id=? AND strategy_version=?",
            (state, self.spec.id, int(self.version.version)))
        self.paper.commit()

    def _build_report(self, kind="available"):
        """Build one real report for this Challenger and append it as its owner."""
        if kind == "available":
            conn, order_ids = self._ledger()
            self._link_risk_decision(conn, order_ids[0], decision="downside_warning",
                                     authority="RISK")
        elif kind == "partial":
            conn, order_ids = self._ledger()
            self._link_risk_decision(conn, order_ids[0], decision="downside_warning",
                                     authority="EXECUTION")
        elif kind == "unavailable":
            # A named order whose own stamp disagrees with the declared Active
            # comparator: the Active leg is blocked before alignment, while the
            # shared environment itself stays EQUAL.
            conn, order_ids = self._ledger(orders=({"strategy_id": "other_active"},))
        else:
            raise AssertionError(f"unknown report kind: {kind}")
        evidence = SCS.capture_active_comparison_evidence(
            conn, active_order_ids=tuple(order_ids))
        spec = self._comparison_spec(
            order_ids=tuple(order_ids),
            expected=(SC.ObservationKey(self.code, "buy"),),
            active_evidence_id=evidence.source_fingerprint)
        report = SC.build_shadow_comparison(
            spec=spec, active_evidence=evidence, shadow_run=self.shadow_run)
        conn.close()
        appended = SCR.append_report(self.paper, report)
        self.paper.commit()
        return appended

    def _evaluate(self, report_id, **overrides):
        values = {"strategy_id": self.spec.id,
                  "strategy_version": int(self.version.version),
                  "strategy_checksum": self.version.checksum,
                  "from_state": "shadow", "target_state": "paper"}
        values.update(overrides)
        return SP.evaluate(self.paper, evidence_bundle={"shadow_comparison_report_id": report_id},
                           **values)

    def _blocking(self, decision):
        return list(decision.blocking_reasons)

    def _create_proposal(self, report, *, proposer_type="human", proposer_id="operator"):
        return SP.create_proposal(
            self.paper, strategy_id=self.spec.id, strategy_version=int(self.version.version),
            strategy_checksum=self.version.checksum, from_state="shadow", target_state="paper",
            evidence_bundle={"shadow_comparison_report_id": report.report_id},
            proposer_type=proposer_type, proposer_id=proposer_id, rationale="exact comparison")

class PromotionComparisonEvidenceTests(_PromotionFixture, unittest.TestCase):
    """R-F1 … R-F6: the shadow -> paper evidence contract."""

    # ── R-F1: exact AVAILABLE comparison satisfies shadow -> paper ───────────

    def test_r_f1_exact_available_report_is_eligible(self):
        report = self._build_report()
        self.assertEqual("AVAILABLE", report.availability)
        self.assertEqual(1, report.coverage["available_observations"])
        decision = self._evaluate(report.report_id)
        self.assertTrue(decision.eligible)
        self.assertEqual([], self._blocking(decision))
        self.assertEqual(["shadow_comparison_report_id"], list(decision.required_evidence))
        self.assertIn("shadow_comparison_report_id", decision.satisfied_evidence)
        # The exact owner identity is what the decision pinned, not a search result.
        self.assertEqual(report.report_fingerprint,
                         decision.evidence_fingerprints["shadow_comparison_report_fingerprint"])
        self.assertEqual(report.comparison_spec["comparison_scope_identity"],
                         decision.evidence_fingerprints["shadow_comparison_scope_identity"])
        self.assertEqual(report.shadow_run_fingerprint,
                         decision.evidence_fingerprints["shadow_run_fingerprint"])

    def test_r_f1b_report_is_required_and_only_the_declared_id_is_read(self):
        report = self._build_report()
        self.assertEqual(["shadow_comparison_report_required"],
                         self._blocking(self._evaluate(None)))
        with mock.patch.object(SCR, "get_report", return_value=None) as reader:
            decision = self._evaluate(report.report_id)
        self.assertEqual(["shadow_comparison_report_not_found"], self._blocking(decision))
        reader.assert_called_once_with(self.paper, report.report_id)

    # ── R-F2 / R-F3: PARTIAL and UNAVAILABLE always block ────────────────────

    def test_r_f2_partial_report_blocks(self):
        report = self._build_report(kind="partial")
        self.assertEqual("PARTIAL", report.availability)
        self.assertEqual(0.0, report.coverage["coverage_ratio"])
        decision = self._evaluate(report.report_id)
        self.assertFalse(decision.eligible)
        self.assertEqual(["shadow_comparison_partial"], self._blocking(decision))
        self.assertNotIn("shadow_comparison_report_id", decision.satisfied_evidence)

    def test_r_f3_unavailable_report_blocks(self):
        report = self._build_report(kind="unavailable")
        self.assertEqual("UNAVAILABLE", report.availability)
        decision = self._evaluate(report.report_id)
        self.assertFalse(decision.eligible)
        self.assertEqual(["shadow_comparison_unavailable"], self._blocking(decision))

    def test_r_f3b_blocking_reasons_never_read_as_success(self):
        report = self._build_report()
        tainted = dataclasses.replace(report, blocking_reasons=("environment_mismatch",))
        with mock.patch.object(SCR, "get_report", return_value=tainted):
            decision = self._evaluate(report.report_id)
        self.assertFalse(decision.eligible)
        self.assertEqual(["shadow_comparison_blocking_reasons"], self._blocking(decision))

    def test_r_f3c_coverage_and_provenance_must_be_complete(self):
        report = self._build_report()
        short = dataclasses.replace(
            report, coverage={**dict(report.coverage), "available_observations": 0})
        with mock.patch.object(SCR, "get_report", return_value=short):
            self.assertEqual(["shadow_comparison_coverage_incomplete"],
                             self._blocking(self._evaluate(report.report_id)))
        degraded_paths = dict(report.provenance)
        degraded_paths[SP.REQUIRED_COMPARISON_PROVENANCE[0]] = "UNAVAILABLE"
        with mock.patch.object(SCR, "get_report",
                               return_value=dataclasses.replace(report, provenance=degraded_paths)):
            self.assertEqual(["shadow_comparison_provenance_incomplete"],
                             self._blocking(self._evaluate(report.report_id)))
        # A declared (never verified) value cannot be spent as an owner fact.
        declared_paths = dict(report.provenance)
        declared_paths["challenger.entry"] = "DECLARED"
        with mock.patch.object(SCR, "get_report",
                               return_value=dataclasses.replace(report, provenance=declared_paths)):
            self.assertEqual(["shadow_comparison_provenance_incomplete"],
                             self._blocking(self._evaluate(report.report_id)))

    # ── R-F4: identity / environment / corruption ────────────────────────────

    def test_r_f4_report_stamp_must_equal_the_requested_exact_version(self):
        report = self._build_report()
        self.assertEqual(self.spec.id, report.challenger_strategy_stamp["strategy_id"])
        # The Shadow runtime refuses to build a run whose stamp and exact version
        # disagree, so an inconsistent report cannot even be produced. The policy's
        # own identity comparison is therefore asserted at its boundary, against
        # the real persisted report row.
        for identity in ({"strategy_id": "another_strategy",
                          "strategy_version": int(self.version.version),
                          "strategy_checksum": self.version.checksum},
                         {"strategy_id": self.spec.id,
                          "strategy_version": int(self.version.version) + 1,
                          "strategy_checksum": self.version.checksum},
                         {"strategy_id": self.spec.id,
                          "strategy_version": int(self.version.version),
                          "strategy_checksum": "e" * 64}):
            with self.subTest(identity=identity["strategy_id"],
                              version=identity["strategy_version"]):
                _, reason = SP._verify_shadow_comparison(self.paper, report.report_id, identity)
                self.assertEqual("shadow_comparison_identity_mismatch", reason)
        _, reason = SP._verify_shadow_comparison(
            self.paper, report.report_id,
            {"strategy_id": self.spec.id, "strategy_version": int(self.version.version),
             "strategy_checksum": self.version.checksum})
        self.assertIsNone(reason)

    def test_r_f4b_requested_identity_must_match_the_registry_version(self):
        report = self._build_report()
        decision = self._evaluate(report.report_id, strategy_checksum="f" * 64)
        self.assertFalse(decision.eligible)
        self.assertIn("strategy_checksum_mismatch", self._blocking(decision))
        decision = self._evaluate(report.report_id, strategy_version=99)
        self.assertFalse(decision.eligible)
        self.assertIn("strategy_version_not_found", self._blocking(decision))

    def test_r_f4c_environment_mismatch_blocks(self):
        report = self._build_report()
        environment = dict(report.environment_identity)
        environment["shared_environment_equality"] = "MISMATCH"
        with mock.patch.object(SCR, "get_report",
                               return_value=dataclasses.replace(report,
                                                                environment_identity=environment)):
            self.assertEqual(["shadow_comparison_environment_mismatch"],
                             self._blocking(self._evaluate(report.report_id)))

    def test_r_f4d_tampered_stored_row_fails_closed(self):
        report = self._build_report()
        # The owner table is append-only, so an edited row can only be *inserted*
        # inconsistently. The owner's reader re-verifies the fingerprint of the
        # stored evidence, so such a row is corruption, never a silently different
        # comparison. (columns mirror shadow_comparison_reports)
        torn_id = "d" * 64
        with self.paper:
            self.paper.execute(
                "INSERT INTO shadow_comparison_reports(report_id,report_fingerprint,shadow_run_id,"
                "shadow_run_fingerprint,active_strategy_id,active_strategy_version,"
                "active_strategy_checksum,challenger_strategy_id,challenger_strategy_version,"
                "challenger_strategy_checksum,environment_fingerprint,session_date,decision_at,"
                "availability,coverage_ratio,evidence_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (torn_id, torn_id, self.shadow_run.run_id, self.shadow_run.run_fingerprint,
                 self.active.strategy_id, self.active.version, self.active.checksum,
                 self.challenger.strategy_id, self.challenger.version, self.challenger.checksum,
                 self.identity.environment_fingerprint, self.day, self.decision_at,
                 "AVAILABLE", 1.0, '{"schema_version":"shadow-comparison-v1"}'))
        decision = self._evaluate(torn_id)
        self.assertFalse(decision.eligible)
        self.assertEqual(["shadow_comparison_corrupt"], self._blocking(decision))
        # The real report still resolves exactly.
        self.assertTrue(self._evaluate(report.report_id).eligible)

    # ── R-F5: an exact report is used; nothing falls back to "the newest" ────

    def test_r_f5_no_latest_lookup_exists_on_the_promotion_path(self):
        self.assertEqual([], [name for name in dir(SCR)
                              if "latest" in name.lower() or "recent" in name.lower()])
        source = pathlib.Path(SP.__file__).read_text(encoding="utf-8")
        for token in ("get_latest", "find_latest", "latest_report", "latest_comparison",
                      "most_recent", "current_challenger", "current_report", "ORDER BY id DESC LIMIT 1"):
            self.assertNotIn(token, source)

    def test_r_f5b_a_blocked_report_never_falls_back_to_an_available_one(self):
        blocked = self._build_report(kind="partial")
        usable = self._build_report()
        self.assertEqual("PARTIAL", blocked.availability)
        self.assertEqual("AVAILABLE", usable.availability)
        self.assertNotEqual(blocked.report_id, usable.report_id)
        # The exact requested record decides, even though a better one exists.
        self.assertEqual(["shadow_comparison_partial"],
                         self._blocking(self._evaluate(blocked.report_id)))
        self.assertTrue(self._evaluate(usable.report_id).eligible)
        # And an unknown id stays unknown instead of resolving to the newest row.
        self.assertEqual(["shadow_comparison_report_not_found"],
                         self._blocking(self._evaluate("a" * 64)))

    # ── R-F6: apply drives exactly one lifecycle write ───────────────────────

    def test_r_f6_apply_promotes_the_lifecycle_and_nothing_else(self):
        report = self._build_report()
        proposal = self._create_proposal(report)
        self.assertTrue(proposal["decision"]["eligible"])
        applied = SP.apply_proposal(self.paper, None, proposal,
                                    actor_type="human", actor_id="operator")
        self.assertEqual("paper", applied["lifecycle"]["state"])
        self.assertEqual("paper", SL.get_state(
            self.paper, self.spec.id, int(self.version.version),
            checksum=self.version.checksum)["state"])
        events = SL.history(self.paper, self.spec.id)
        promotions = [row for row in events if row["transition_kind"] == "promotion"]
        # The version-created event plus exactly one promotion event: the report is
        # evidence, so it never writes a lifecycle row of its own.
        self.assertEqual(1, len(promotions))
        self.assertEqual(["version_created", "promotion"], [row["transition_kind"]
                                                            for row in events])
        self.assertEqual("paper", promotions[0]["to_state"])
        self.assertEqual(applied["decision"]["decision_fingerprint"],
                         promotions[0]["promotion_decision_fingerprint"])
        self.assertEqual(SP.POLICY_VERSION, promotions[0]["promotion_policy_version"])
        self.assertEqual(report.report_fingerprint,
                         applied["decision"]["evidence_fingerprints"][
                             "shadow_comparison_report_fingerprint"])

    def test_r_f6b_ai_and_stale_proposals_fail_closed(self):
        report = self._build_report()
        proposal = self._create_proposal(report, proposer_type="ai", proposer_id="assistant")
        with self.assertRaises(SP.PromotionError) as raised:
            SP.apply_proposal(self.paper, None, proposal, actor_type="ai", actor_id="assistant")
        self.assertEqual("ai_cannot_apply_transition", str(raised.exception))
        # The world moves on: the recorded decision is no longer the current one.
        self._seed_state("validated")
        with self.assertRaises(SP.PromotionError) as raised:
            SP.apply_proposal(self.paper, None, proposal,
                              actor_type="human", actor_id="operator")
        self.assertEqual("promotion_proposal_stale_or_blocked", str(raised.exception))
        self.assertEqual("validated", SL.get_state(
            self.paper, self.spec.id, int(self.version.version),
            checksum=self.version.checksum)["state"])

    def test_r_f6c_paper_to_production_sim_stays_blocked(self):
        self._seed_state("paper")
        decision = SP.evaluate(
            self.paper, strategy_id=self.spec.id, strategy_version=int(self.version.version),
            strategy_checksum=self.version.checksum, from_state="paper",
            target_state="production_sim", evidence_bundle=None)
        self.assertFalse(decision.eligible)
        self.assertEqual(["paper_runtime_evidence_owner_unavailable"],
                         self._blocking(decision))




class ChallengerWorkspaceReadModelTests(_PromotionFixture, unittest.TestCase):
    """W1 … W7: the workspace is assembled from owners, and never guesses."""

    def setUp(self):
        super().setUp()
        # The service opens its own connection to the same file; close ours while
        # the migrations run (they set the journal mode) and reopen afterwards.
        self.paper.close()
        self._patches = [mock.patch.object(PT, "DB_PATH", self.paper_path),
                         mock.patch.object(SR, "DEFAULT_DB_PATH", self.paper_path)]
        for item in self._patches:
            item.start()
        PT.init_db()
        self.paper = sqlite3.connect(self.paper_path, timeout=10)
        self.paper.row_factory = sqlite3.Row

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        super().tearDown()

    def _read(self, **kwargs):
        return SVC.challenger_read_model(self.spec.id, **kwargs)

    def test_w1a_both_legs_come_from_the_report_stamps(self):
        report = self._build_report()
        view = self._read(comparison_report_id=report.report_id)
        active_stamp = dict(report.active_strategy_stamp)
        challenger_stamp = dict(report.challenger_strategy_stamp)
        for leg, stamp, source in (("active", active_stamp,
                                    "shadow_comparison.active_strategy_stamp"),
                                   ("challenger", challenger_stamp,
                                    "shadow_comparison.challenger_strategy_stamp")):
            with self.subTest(leg=leg):
                self.assertEqual(stamp["strategy_id"], view[leg]["strategy_id"])
                self.assertEqual(int(stamp["version"]), view[leg]["version"])
                self.assertEqual(stamp["checksum"], view[leg]["checksum"])
                self.assertEqual(source, view[leg]["identity_source"])
                self.assertTrue(view[leg]["comparison_bound"])
                self.assertTrue(view[leg]["available"])
        # The two legs are genuinely different strategies in this fixture: neither
        # may be collapsed onto the endpoint's own registry identity.
        self.assertNotEqual(view["active"]["strategy_id"], view["challenger"]["strategy_id"])
        self.assertEqual(self.spec.id, view["challenger"]["strategy_id"])
        self.assertEqual(active_stamp["strategy_id"], view["active"]["strategy_id"])
        # Exactly one lifecycle state per leg, and an unknown Active comparator is
        # reported as unknown rather than filled from the Challenger.
        self.assertIsNone(view["active"]["lifecycle_state"])
        self.assertEqual("shadow", view["challenger"]["lifecycle_state"])
        self.assertEqual("shadow", view["lifecycle"]["state"])
        self.assertIn("paper", view["lifecycle"]["allowed_next_transitions"])
        self.assertEqual(["version_created"], [row["transition_kind"]
                                               for row in view["lifecycle"]["history"]])
        self.assertEqual(int(self.version.version), view["lifecycle"]["version"])
        self.assertEqual({"strategy_id": self.spec.id,
                          "strategy_version": int(self.version.version),
                          "strategy_checksum": self.version.checksum},
                         view["lifecycle_promotion"]["candidate"])

    def test_w1b_a_historical_report_keeps_its_exact_identity(self):
        report = self._build_report()
        stale_version = int(self.version.version)
        stale_checksum = self.version.checksum
        # The registry moves on: a new immutable version becomes the head.
        SR.save_definition(self.paper, self.spec.id, {"dsl_ast": {"op": "lt",
                          "left": {"op": "field", "name": "close"},
                          "right": {"op": "const", "value": 0}}},
                          expected_version=stale_version, actor="r32-final-test")
        self.paper.commit()
        head = SR.get_version(self.spec.id, conn=self.paper)
        self.assertEqual(stale_version + 1, int(head.version))
        self.assertNotEqual(stale_checksum, head.checksum)

        view = self._read(comparison_report_id=report.report_id)
        # The displayed Challenger is still the report's exact version, not the head.
        self.assertEqual(stale_version, view["challenger"]["version"])
        self.assertEqual(stale_checksum, view["challenger"]["checksum"])
        self.assertEqual(stale_version, view["lifecycle"]["version"])
        self.assertEqual(stale_checksum, view["lifecycle"]["checksum"])
        self.assertEqual({"strategy_id": self.spec.id, "strategy_version": stale_version,
                          "strategy_checksum": stale_checksum},
                         view["lifecycle_promotion"]["candidate"])
        # The head is reported separately, so the drift is visible rather than hidden.
        self.assertEqual(int(head.version), view["registry_head"]["version"])
        self.assertEqual(head.checksum, view["registry_head"]["checksum"])
        # And the policy blocks the stale candidate on its own terms.
        self.assertFalse(view["lifecycle_promotion"]["eligible"])
        self.assertIn("strategy_version_changed",
                      view["lifecycle_promotion"]["blocking_reasons"])

    def test_w1c_a_report_of_another_strategy_fails_closed(self):
        self._pin_foreign_challenger("another_strategy", 1, "f" * 64)
        foreign = self._build_report()
        self.assertEqual("another_strategy", foreign.challenger_strategy_stamp["strategy_id"])
        with self.assertRaises(SVC.InvalidStrategyDefinition) as raised:
            self._read(comparison_report_id=foreign.report_id)
        self.assertEqual("shadow_comparison_identity_mismatch", str(raised.exception))
        # An explicit version that contradicts the report is the same conflict.
        self._restore_challenger()
        report = self._build_report()
        with self.assertRaises(SVC.InvalidStrategyDefinition) as raised:
            self._read(comparison_report_id=report.report_id,
                       version=int(self.version.version) + 1)
        self.assertEqual("shadow_comparison_identity_mismatch", str(raised.exception))
        # The matching request stays usable.
        self.assertTrue(self._read(comparison_report_id=report.report_id,
                                   version=int(self.version.version))["comparison"]["available"])

    def test_w2_the_named_report_is_the_only_comparison_evidence(self):
        report = self._build_report()
        section = self._read(comparison_report_id=report.report_id)["comparison"]
        self.assertTrue(section["available"])
        self.assertEqual(report.report_id, section["report_id"])
        self.assertEqual(report.report_fingerprint, section["report_fingerprint"])
        self.assertEqual(report.comparison_spec["comparison_scope_identity"],
                         section["comparison_scope_identity"])
        self.assertEqual(report.shadow_run_id, section["shadow_run_id"])
        self.assertEqual(dict(report.challenger_strategy_stamp),
                         section["challenger_strategy_stamp"])
        self.assertEqual(report.report_id,
                         self._read(comparison_report_id=report.report_id)[
                             "exact_evidence"]["comparison_report_id"])

    def test_w3_to_w5_availability_coverage_and_blocking_come_from_the_owner(self):
        partial = self._build_report(kind="partial")
        view = self._read(comparison_report_id=partial.report_id)
        section = view["comparison"]
        self.assertEqual("PARTIAL", section["availability"])
        self.assertEqual(dict(partial.coverage), section["coverage"])
        self.assertEqual(list(partial.blocking_reasons), section["blocking_reasons"])
        self.assertEqual(0.0, section["coverage"]["coverage_ratio"])
        # A partial comparison is never rendered as ready.
        self.assertFalse(view["lifecycle_promotion"]["eligible"])
        self.assertIn("shadow_comparison_partial",
                      view["lifecycle_promotion"]["blocking_reasons"])

    def test_w6_readiness_comes_from_the_policy_not_the_read_model(self):
        report = self._build_report()
        section = self._read(comparison_report_id=report.report_id)["lifecycle_promotion"]
        self.assertEqual("strategy_promotion", section["authority"])
        expected = SP.evaluate(self.paper, strategy_id=self.spec.id,
                               strategy_version=int(self.version.version),
                               strategy_checksum=self.version.checksum,
                               from_state="shadow", target_state="paper",
                               evidence_bundle={"shadow_comparison_report_id": report.report_id})
        self.assertEqual(expected.eligible, section["eligible"])
        self.assertEqual(expected.decision_fingerprint, section["decision_fingerprint"])
        self.assertEqual(list(expected.blocking_reasons), section["blocking_reasons"])
        self.assertEqual(dict(expected.evidence_fingerprints), section["evidence_fingerprints"])

    def test_w6b_the_two_authorities_are_never_merged(self):
        self._build_report()
        view = self._read()
        self.assertEqual("strategy_promotion", view["lifecycle_promotion"]["authority"])
        self.assertEqual("strategy lifecycle state", view["lifecycle_promotion"]["target_fact"])
        activation = view["parameter_head_activation"]
        self.assertEqual("strategy_champion", activation["authority"])
        self.assertEqual("formal parameter/version head", activation["target_fact"])
        self.assertNotIn("eligible", activation)
        for combined in ("ready", "promotable", "winner", "score", "ranking"):
            self.assertNotIn(combined, view)

    def test_w7_no_latest_fallback_and_absent_evidence_stays_absent(self):
        report = self._build_report()
        unnamed = self._read()
        self.assertFalse(unnamed["comparison"]["available"])
        self.assertEqual("shadow_comparison_report_required",
                         unnamed["comparison"]["unavailable_reason"])
        self.assertEqual("UNAVAILABLE", unnamed["comparison"]["availability"])
        # Absent is not zero: the missing coverage keeps its unknown shape.
        self.assertIsNone(unnamed["comparison"]["coverage"])
        self.assertIsNone(unnamed["comparison"]["performance"])
        self.assertIn("shadow_comparison_report_required",
                      unnamed["lifecycle_promotion"]["blocking_reasons"])
        # No report ⇒ no Active comparator fact at all: never the registry head.
        self.assertFalse(unnamed["active"]["available"])
        self.assertEqual("shadow_comparison_report_required",
                         unnamed["active"]["unavailable_reason"])
        self.assertIsNone(unnamed["active"]["strategy_id"])
        self.assertIsNone(unnamed["active"]["version"])
        self.assertIsNone(unnamed["active"]["checksum"])
        self.assertIsNone(unnamed["active"]["lifecycle_state"])
        self.assertFalse(unnamed["active"]["comparison_bound"])
        # The Challenger is a registry candidate here, explicitly not bound to a
        # comparison, so nothing is fabricated into an Active fact.
        self.assertFalse(unnamed["challenger"]["comparison_bound"])
        self.assertEqual("registry_candidate", unnamed["challenger"]["identity_source"])
        self.assertEqual(self.spec.id, unnamed["challenger"]["strategy_id"])
        self.assertEqual(int(self.version.version), unnamed["challenger"]["version"])
        # A perfectly good report exists and the unnamed request still reports
        # absence: the workspace never resolves "the newest one".
        unknown = self._read(comparison_report_id="e" * 64)
        self.assertEqual("shadow_comparison_report_not_found",
                         unknown["comparison"]["unavailable_reason"])
        self.assertEqual("shadow_comparison_report_not_found",
                         unknown["active"]["unavailable_reason"])
        self.assertIsNone(unknown["active"]["strategy_id"])
        self.assertEqual(report.report_id,
                         self._read(comparison_report_id=report.report_id)["comparison"]["report_id"])
        self.assertTrue(self._read(comparison_report_id=report.report_id)[
            "lifecycle_promotion"]["eligible"])


class PromotionAuthoritySeparationTests(unittest.TestCase):
    """J / R-F7 guards: the two promotion authorities never cross."""

    def setUp(self):
        self.promotion_source = pathlib.Path(SP.__file__).read_text(encoding="utf-8")
        self.champion_source = pathlib.Path(
            os.path.join(BACKEND_DIR, "strategy_champion.py")).read_text(encoding="utf-8")
        self.lifecycle_source = pathlib.Path(SL.__file__).read_text(encoding="utf-8")

    def test_lifecycle_promotion_never_touches_the_parameter_head(self):
        for token in ("strategy_champion", "activate_params_candidate", "self_evolution",
                      "adjust_strategy_params", "PROMOTION_TOLERANCE"):
            self.assertNotIn(token, self.promotion_source)

    def test_lifecycle_promotion_mutates_only_through_the_lifecycle_authority(self):
        self.assertIn("SL.transition(", self.promotion_source)
        for token in ("UPDATE strategy_lifecycle_state", "INSERT INTO strategy_lifecycle",
                      "DELETE FROM strategy_lifecycle"):
            self.assertNotIn(token, self.promotion_source)

    def test_parameter_head_activation_never_writes_the_lifecycle(self):
        for token in ("strategy_lifecycle", "shadow_comparison", "shadow_run",
                      "promotion_proposal", "policy_version"):
            self.assertNotIn(token, self.champion_source)

    def test_the_two_authorities_keep_distinct_target_facts(self):
        import strategy_champion as SCM
        # Parameter-head activation: metric tolerances over its own shadow ledgers.
        self.assertIsInstance(SCM.PROMOTION_TOLERANCE, dict)
        self.assertTrue(callable(SCM.compare_for_promotion))
        self.assertTrue(callable(SCM.promote_challenger))
        # Lifecycle promotion: evidence completeness over one exact comparison
        # report, with no metric vocabulary at all.
        for token in ("return_pct", "fill_rate", "turnover_ratio", "concentration",
                      "sharpe", "winner", "ranking"):
            self.assertNotIn(token, self.promotion_source)
        self.assertEqual("strategy-promotion-policy-v1", SP.POLICY_VERSION)
        self.assertEqual([], [target for target, required in SP.PROMOTION_RULES.items()
                              if any(name in ("return_pct", "score", "rank")
                                     for name in required)])

    def test_lifecycle_state_machine_is_the_only_descendant_of_the_policy(self):
        self.assertIn("import strategy_lifecycle as SL", self.promotion_source)
        self.assertNotIn("import strategy_champion", self.promotion_source)
        self.assertIn("def transition(", self.lifecycle_source)


if __name__ == "__main__":
    unittest.main()
