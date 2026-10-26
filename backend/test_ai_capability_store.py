# -*- coding: utf-8 -*-
"""凭据签发/撤销/累计预算的验证（S4 的"凭据从哪来"）。"""
import dataclasses
import sqlite3
import tempfile
import unittest

import ai_capability as CAP
import ai_capability_store as ST

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


def request(credential_value, **overrides):
    kwargs = dict(
        credential=credential_value, strategy_id="tq_breakout",
        target_kind="entry_policy",
        field_changes=(("first_board_bonus", 0.20),),
        current_baseline=(("first_board_bonus", 0.22),),
        requested_baseline=(("first_board_bonus", 0.22),),
        evidence_policy_id="evidence-v1", revocation_epoch=1,
        producer="research-agent", approver="operator:alice", cost=1.0, now=NOW)
    kwargs.update(overrides)
    return CAP.AdoptionRequest(**kwargs)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/research.sqlite3"
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        self._folder.cleanup()


class IssuanceTests(StoreTestCase):
    def test_issuing_requires_an_operator_identity(self):
        with self.assertRaises(ST.CapabilityStoreError) as caught:
            ST.issue(self.conn, credential(), operator="")
        self.assertEqual(str(caught.exception), ST.R_OPERATOR_REQUIRED)

    def test_a_credential_round_trips(self):
        ST.issue(self.conn, credential(), operator="operator:alice", now=NOW)
        loaded = ST.load(self.conn, "cap-1")
        self.assertEqual(loaded.capability_id, "cap-1")
        self.assertEqual(loaded.allowed_fields, ("first_board_bonus",))
        self.assertEqual(loaded.revocation_epoch, 1)

    def test_reissuing_the_same_content_is_idempotent(self):
        ST.issue(self.conn, credential(), operator="operator:alice", now=NOW)
        ST.issue(self.conn, credential(), operator="operator:alice", now=NOW)
        self.assertEqual(len(ST.events(self.conn, "cap-1")), 1)

    def test_reissuing_with_a_changed_scope_is_refused(self):
        ST.issue(self.conn, credential(), operator="operator:alice", now=NOW)
        with self.assertRaises(ST.CapabilityStoreError) as caught:
            ST.issue(self.conn, credential(max_change_per_day=99.0),
                     operator="operator:alice", now=NOW)
        self.assertEqual(str(caught.exception), ST.R_CAPABILITY_CONFLICT)

    def test_an_unknown_capability_is_not_invented(self):
        self.assertIsNone(ST.load(self.conn, "nope"))


class RevocationTests(StoreTestCase):
    def test_revocation_bumps_the_epoch_and_blocks_old_requests(self):
        ST.issue(self.conn, credential(), operator="operator:alice", now=NOW)
        revoked = ST.revoke(self.conn, "cap-1", operator="operator:alice",
                            reason="drift detected", now=NOW)
        self.assertEqual(revoked.revocation_epoch, 2)
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            CAP.evaluate(request(revoked), apply_writer=lambda _r: None)
        self.assertEqual(caught.exception.reason, CAP.R_REVOKED)

    def test_revocation_requires_an_operator_and_an_audit_row(self):
        with self.assertRaises(ST.CapabilityStoreError):
            ST.revoke(self.conn, "cap-1", operator="")
        ST.issue(self.conn, credential(), operator="operator:alice", now=NOW)
        ST.revoke(self.conn, "cap-1", operator="operator:bob", reason="rotated", now=NOW)
        events = ST.events(self.conn, "cap-1")
        self.assertEqual([row["event"] for row in events],
                         [ST.EVENT_ISSUED, ST.EVENT_REVOKED])
        self.assertEqual(events[-1]["operator"], "operator:bob")

    def test_an_unknown_capability_cannot_be_revoked(self):
        with self.assertRaises(ST.CapabilityStoreError) as caught:
            ST.revoke(self.conn, "ghost", operator="operator:alice")
        self.assertEqual(str(caught.exception), ST.R_CAPABILITY_UNKNOWN)


class BudgetPersistenceTests(StoreTestCase):
    def test_usage_survives_a_restart(self):
        ST.save_budget(self.conn, "cap-1", CAP.BudgetState(
            window_key="2026-10-26", changes_used=0.05, cost_used=1.0, trials_used=1),
            now=NOW)
        self.conn.commit()
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        state = ST.budget(self.conn, "cap-1", "2026-10-26")
        self.assertAlmostEqual(state.changes_used, 0.05)
        self.assertEqual(state.trials_used, 1)

    def test_a_new_day_starts_from_zero(self):
        ST.save_budget(self.conn, "cap-1",
                       CAP.BudgetState(window_key="2026-10-25", changes_used=0.10), now=NOW)
        self.assertEqual(ST.budget(self.conn, "cap-1", "2026-10-26").changes_used, 0.0)


class EndToEndAdoptionTests(StoreTestCase):
    def test_store_reloads_issued_authority_instead_of_trusting_request_credential(self):
        """请求里的凭据对象只是"指向哪一条授权"的指针，范围一律以签发库为准。"""
        issued = ST.issue(self.conn, credential(), operator="operator:alice", now=NOW)
        ST.revoke(self.conn, "cap-1", operator="operator:alice", now=NOW)
        # 旧对象还写着 epoch=1：撤销即刻生效，写手一次都不许被调用。
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            ST.adopt(self.conn, request(issued), apply_writer=lambda _r: None, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_REVOKED)

        # 攻击回归（使用一张未撤销的真凭据）：调用方自己拼一份"更宽"的凭据，
        # 库里签发范围仍然说了算 -> 越界拒绝，且没有写出、没有扣预算。
        ST.issue(self.conn, credential(capability_id="scoped",
                                       max_change_per_apply=0.01,
                                       max_change_per_day=0.01),
                 operator="operator:alice", now=NOW)
        forged = dataclasses.replace(credential(capability_id="scoped"),
                                     max_change_per_apply=99.0, max_change_per_day=99.0)
        writes = []
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            ST.adopt(self.conn, request(forged,
                                        field_changes=(("first_board_bonus", 0.05),)),
                     apply_writer=writes.append, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_CHANGE_TOO_LARGE)
        self.assertEqual(writes, [])
        self.assertEqual(ST.budget(self.conn, "cap-1", ST.window_key(NOW)).changes_used, 0.0)
        self.assertEqual(ST.budget(self.conn, "scoped", ST.window_key(NOW)).changes_used, 0.0)

        # 另一张真签发的凭据可以正常采用，但它只消耗自己的预算，不借 cap-1 的账。
        other = ST.issue(self.conn, credential(capability_id="other"),
                         operator="operator:alice", now=NOW)
        decision = ST.adopt(self.conn, request(other), apply_writer=lambda _r: None, now=NOW)
        self.assertTrue(decision.applied)
        self.assertEqual(ST.budget(self.conn, "cap-1", ST.window_key(NOW)).changes_used, 0.0)
        self.assertEqual(ST.budget(self.conn, "other", ST.window_key(NOW)).trials_used, 1)

    def test_a_revoked_authority_cannot_be_revived_by_reloading_its_epoch(self):
        ST.issue(self.conn, credential(), operator="operator:alice", now=NOW)
        revoked = ST.revoke(self.conn, "cap-1", operator="operator:alice", now=NOW)
        writes = []
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            ST.adopt(
                self.conn,
                request(revoked, revocation_epoch=revoked.revocation_epoch),
                apply_writer=writes.append, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_REVOKED)
        self.assertEqual(writes, [])
        self.assertEqual(
            ST.budget(self.conn, "cap-1", ST.window_key(NOW)).trials_used, 0)

    def test_never_issued_credential_cannot_reach_real_writer(self):
        writes = []
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            ST.adopt(self.conn, request(credential()), apply_writer=writes.append, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_NO_CREDENTIAL)
        self.assertEqual(writes, [])

    def test_an_issued_credential_adopts_once_then_hits_the_budget(self):
        issued = ST.issue(self.conn, credential(), operator="operator:alice", now=NOW)
        applied = []

        def writer(req):
            applied.append(dict(req.field_changes))

        change = request(issued, field_changes=(("first_board_bonus", 0.17),))
        first = ST.adopt(self.conn, change, apply_writer=writer, now=NOW)
        self.assertTrue(first.applied)
        self.assertEqual(len(applied), 1)

        second = ST.adopt(self.conn, change, apply_writer=writer, now=NOW)
        self.assertEqual(second.budget_after.changes_used, 0.10)
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            ST.adopt(self.conn, change, apply_writer=writer, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_BUDGET_EXHAUSTED)
        self.assertEqual(len(applied), 2, "第三次被拒时不能写出任何一次采用")

    def test_without_a_writer_nothing_is_adopted_and_budget_is_untouched(self):
        issued = ST.issue(self.conn, credential(), operator="operator:alice", now=NOW)
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            ST.adopt(self.conn, request(issued), apply_writer=None, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_APPLY_NOT_WIRED)
        self.assertEqual(
            ST.budget(self.conn, "cap-1", ST.window_key(NOW)).changes_used, 0.0)

    def test_an_advisory_credential_never_adopts(self):
        issued = ST.issue(self.conn, credential(mode=CAP.MODE_ADVISORY),
                          operator="operator:alice", now=NOW)
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            ST.adopt(self.conn, request(issued, ),
                     apply_writer=lambda _r: None, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_NO_CREDENTIAL)


if __name__ == "__main__":
    unittest.main()
