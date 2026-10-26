# -*- coding: utf-8 -*-
"""有界自动采用的验证（方案 §5）：机制在，授权不在，默认什么都不自动做。"""
import unittest

import ai_capability as CAP

NOW = "2026-10-26T02:00:00+00:00"
FUTURE = "2026-11-26T02:00:00+00:00"


def credential(**overrides):
    kwargs = dict(
        capability_id="cap-1", mode=CAP.MODE_BOUNDED_AUTO,
        strategy_ids=("tq_breakout",), allowed_target_kinds=("entry_policy",),
        allowed_fields=("first_board_bonus",), bounds_version="bounds-v1",
        evidence_policy_id="evidence-v1", max_change_per_apply=0.05,
        max_change_per_day=0.10, loss_stop_policy_id="loss-v1",
        max_trials_per_day=3, daily_cost_cap=5.0, max_tool_steps_per_task=12,
        expires_at=FUTURE, revocation_epoch=1, issued_by="operator:alice",
    )
    kwargs.update(overrides)
    return CAP.CapabilityCredential(**kwargs)


def request(**overrides):
    kwargs = dict(
        credential=credential(), strategy_id="tq_breakout", target_kind="entry_policy",
        field_changes=(("first_board_bonus", 0.20),),
        current_baseline=(("first_board_bonus", 0.22),),
        requested_baseline=(("first_board_bonus", 0.22),),
        evidence_policy_id="evidence-v1", revocation_epoch=1,
        producer="research-agent", approver="operator:alice",
        cost=1.0, now=NOW,
    )
    kwargs.update(overrides)
    return CAP.AdoptionRequest(**kwargs)


class CredentialTests(unittest.TestCase):
    def test_a_credential_missing_any_budget_field_is_refused(self):
        for field in CAP.REQUIRED_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(CAP.AdoptionRefused) as caught:
                    credential(**{field: None})
                self.assertEqual(caught.exception.reason, CAP.R_INCOMPLETE_CREDENTIAL)

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(CAP.AdoptionRefused):
            credential(mode="FULL_AUTO")

    def test_only_bounded_auto_claims_to_auto_apply(self):
        self.assertTrue(credential().auto_applies)
        for mode in (CAP.MODE_ADVISORY, CAP.MODE_REQUIRE_AI, CAP.MODE_OFF):
            self.assertFalse(credential(mode=mode).auto_applies)

    def test_without_a_credential_the_system_stays_advisory(self):
        described = CAP.describe_capability(None)
        self.assertEqual(described["mode"], CAP.MODE_ADVISORY)
        self.assertFalse(described["auto_applies"])

    def test_the_fingerprint_changes_with_the_scope(self):
        base = CAP.credential_fingerprint(credential())
        self.assertEqual(base, CAP.credential_fingerprint(credential()))
        self.assertNotEqual(base, CAP.credential_fingerprint(
            credential(allowed_fields=("first_board_bonus", "chase_penalty"))))


class AdopterTests(unittest.TestCase):
    def test_no_credential_means_no_auto_adoption(self):
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request(credential=None))
        self.assertEqual(caught.exception.reason, CAP.R_NO_CREDENTIAL)

    def test_advisory_mode_never_auto_applies(self):
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request(credential=credential(mode=CAP.MODE_ADVISORY)))
        self.assertEqual(caught.exception.reason, CAP.R_NO_CREDENTIAL)

    def test_the_producer_cannot_approve_its_own_change(self):
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request(approver="research-agent"))
        self.assertEqual(caught.exception.reason, CAP.R_APPROVER_IS_PRODUCER)

    def test_expiry_and_revocation_are_enforced(self):
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request(credential=credential(
                expires_at="2026-10-01T00:00:00+00:00")))
        self.assertEqual(caught.exception.reason, CAP.R_EXPIRED)
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request(revocation_epoch=2))
        self.assertEqual(caught.exception.reason, CAP.R_REVOKED)

    def test_scope_is_enforced_on_strategy_target_and_field(self):
        cases = (
            (dict(strategy_id="trend_pullback"), CAP.R_STRATEGY_OUT_OF_SCOPE),
            (dict(target_kind="selection_weights"), CAP.R_TARGET_KIND_OUT_OF_SCOPE),
            (dict(field_changes=(("chase_penalty", 0.5),)), CAP.R_FIELD_OUT_OF_SCOPE),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(CAP.AdoptionRefused) as caught:
                    CAP.evaluate(request(**overrides))
                self.assertEqual(caught.exception.reason, reason)

    def test_a_change_beyond_the_per_apply_limit_is_refused(self):
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request(field_changes=(("first_board_bonus", 0.10),)))
        self.assertEqual(caught.exception.reason, CAP.R_CHANGE_TOO_LARGE)

    def test_a_changed_baseline_is_stale_base(self):
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request(requested_baseline=(("first_board_bonus", 0.30),)))
        self.assertEqual(caught.exception.reason, CAP.R_STALE_BASE)

    def test_an_evidence_policy_mismatch_is_refused(self):
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request(evidence_policy_id="evidence-v2"))
        self.assertEqual(caught.exception.reason, CAP.R_EVIDENCE_POLICY_MISMATCH)

    def test_the_daily_budget_is_cumulative_across_candidates(self):
        # 每次改 0.05，日上限 0.10：跨候选累计，第三次就耗尽，换候选不清零。
        change = request(field_changes=(("first_board_bonus", 0.17),))
        writer = lambda _request: None  # noqa: E731
        first = CAP.evaluate(change, budget=CAP.BudgetState(window_key="2026-10-26"),
                             apply_writer=writer)
        second = CAP.evaluate(change, budget=first.budget_after, apply_writer=writer)
        self.assertAlmostEqual(second.budget_after.changes_used, 0.10, places=8)
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(change, budget=second.budget_after, apply_writer=writer)
        self.assertEqual(caught.exception.reason, CAP.R_BUDGET_EXHAUSTED)

    def test_a_new_day_starts_a_new_window(self):
        spent = CAP.BudgetState(window_key="2026-10-25", changes_used=0.10,
                                cost_used=5.0, trials_used=3)
        decision = CAP.evaluate(request(now="2026-10-26T02:00:00+00:00"), budget=spent,
                                apply_writer=lambda _request: None)
        self.assertTrue(decision.applied)
        self.assertEqual(decision.budget_after.window_key, "2026-10-26")
        self.assertAlmostEqual(decision.budget_after.changes_used, 0.02, places=8)

    def test_the_trial_and_cost_caps_are_enforced(self):
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request(cost=10.0))
        self.assertEqual(caught.exception.reason, CAP.R_BUDGET_EXHAUSTED)
        spent = CAP.BudgetState(window_key="2026-10-26", trials_used=3)
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request(), budget=spent)
        self.assertEqual(caught.exception.reason, CAP.R_BUDGET_EXHAUSTED)

    def test_without_a_writer_nothing_is_applied(self):
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request())
        self.assertEqual(caught.exception.reason, CAP.R_APPLY_NOT_WIRED)

    def test_a_fully_valid_request_calls_the_single_writer_once(self):
        calls = []

        def writer(req):
            calls.append(req)

        decision = CAP.evaluate(request(), apply_writer=writer)
        self.assertTrue(decision.applied)
        self.assertEqual(len(calls), 1)
        self.assertEqual(decision.credential_id, "cap-1")
        self.assertEqual(dict(decision.changes), {"first_board_bonus": 0.20})

    def test_refusal_happens_before_any_write(self):
        writes = []

        def writer(req):
            writes.append(req)

        with self.assertRaises(CAP.AdoptionRefused):
            CAP.evaluate(request(strategy_id="trend_pullback"), apply_writer=writer)
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
