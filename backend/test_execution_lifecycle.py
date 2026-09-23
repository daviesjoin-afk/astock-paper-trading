# -*- coding: utf-8 -*-
"""委托成交状态机测试。

每条测试对应一个**可证伪**陈述。变异脚本
``work/execution_reality_mutation_check.py`` 的下列缺陷由本文件抓住：

* M53（partial fill => full fill）—— 数量不变式；
* M55（illegal lifecycle transition accepted）—— 合法边表。

M51/M52 由本文件与 ``test_execution_evidence.py`` 共同覆盖。
"""
from __future__ import annotations

import ast
import os
import unittest

import execution_evidence as EE
import execution_lifecycle as EL

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
CREATED_AT = "2026-09-16 09:31:00"


def full_fill_evidence(qty=100, price=10.01, session="2026-09-16"):
    return EE.evidence_from_order(
        {
            "id": 1, "side": "buy", "code": "600001", "qty": qty,
            "planned_price": 10.0, "status": "filled", "reason": "",
            "created_at": CREATED_AT, "order_type": "market",
        },
        [{"qty": qty, "price": price, "amount": qty * price, "fees": 0.1,
          "fill_date": session}],
    )


class StateSetTests(unittest.TestCase):
    def test_the_nine_contract_states_are_exactly_declared(self):
        self.assertEqual(
            {
                "CREATED", "SUBMITTED", "ACCEPTED", "PARTIAL_FILLED", "FILLED",
                "REJECTED", "CANCELLED", "EXPIRED", "UNKNOWN",
            },
            set(EL.ORDER_STATES),
        )
        self.assertEqual(9, len(EL.ORDER_STATES))

    def test_every_state_has_a_transition_row_and_only_declared_targets(self):
        self.assertEqual(set(EL.ORDER_STATES), set(EL.ALLOWED_TRANSITIONS))
        for state, targets in EL.ALLOWED_TRANSITIONS.items():
            self.assertTrue(set(targets).issubset(set(EL.ORDER_STATES)), state)

    def test_terminal_states_cannot_be_left(self):
        for state in sorted(EL.TERMINAL_STATES):
            self.assertEqual(frozenset(), EL.allowed_targets(state), state)
            for target in EL.ORDER_STATES:
                self.assertFalse(EL.can_transition(state, target), (state, target))

    def test_declared_state_constants_match_the_canonical_strings(self):
        self.assertEqual(
            ("CREATED", "SUBMITTED", "ACCEPTED", "PARTIAL_FILLED", "FILLED",
             "REJECTED", "CANCELLED", "EXPIRED", "UNKNOWN"),
            (EL.STATE_CREATED, EL.STATE_SUBMITTED, EL.STATE_ACCEPTED,
             EL.STATE_PARTIAL_FILLED, EL.STATE_FILLED, EL.STATE_REJECTED,
             EL.STATE_CANCELLED, EL.STATE_EXPIRED, EL.STATE_UNKNOWN),
        )


class IllegalTransitionTests(unittest.TestCase):
    """非法跳转必须失败——这是本模块存在的全部理由。"""

    ILLEGAL_EDGES = (
        ("CREATED", "FILLED"),
        ("CREATED", "PARTIAL_FILLED"),
        ("CREATED", "ACCEPTED"),
        ("SUBMITTED", "FILLED"),
        ("SUBMITTED", "PARTIAL_FILLED"),
        ("REJECTED", "FILLED"),
        ("REJECTED", "SUBMITTED"),
        ("REJECTED", "CANCELLED"),
        ("CANCELLED", "FILLED"),
        ("EXPIRED", "FILLED"),
        ("FILLED", "PARTIAL_FILLED"),
        ("FILLED", "CANCELLED"),
        ("PARTIAL_FILLED", "ACCEPTED"),
        ("PARTIAL_FILLED", "SUBMITTED"),
        ("PARTIAL_FILLED", "REJECTED"),
        ("ACCEPTED", "REJECTED"),
        ("ACCEPTED", "CREATED"),
    )

    def test_every_declared_illegal_edge_is_rejected(self):
        for frm, to in self.ILLEGAL_EDGES:
            self.assertFalse(EL.can_transition(frm, to), (frm, to))

    def test_created_cannot_reach_filled_without_submit_and_accept_evidence(self):
        lifecycle = EL.OrderLifecycle(1, created_at=CREATED_AT)
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance("FILLED", evidence=full_fill_evidence(), requested_qty=100)
        self.assertEqual(EL.STATE_CREATED, lifecycle.status)

    def test_submitted_cannot_reach_filled_without_accept_evidence(self):
        lifecycle = EL.OrderLifecycle(1, created_at=CREATED_AT)
        lifecycle.advance("SUBMITTED", source="order_submit")
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance("FILLED", evidence=full_fill_evidence())
        self.assertEqual(EL.STATE_SUBMITTED, lifecycle.status)

    def test_rejected_cannot_reach_filled_in_the_same_lifecycle(self):
        lifecycle = EL.OrderLifecycle(1, created_at=CREATED_AT)
        lifecycle.advance("SUBMITTED", source="order_submit")
        lifecycle.advance("REJECTED", evidence=_reject_evidence(), source="venue_reject")
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance("FILLED", evidence=full_fill_evidence())
        self.assertEqual(EL.STATE_REJECTED, lifecycle.status)

    def test_unknown_state_can_only_be_used_to_realign(self):
        self.assertIn("FILLED", EL.allowed_targets("UNKNOWN"))
        self.assertIn("REJECTED", EL.allowed_targets("UNKNOWN"))
        self.assertNotIn("UNKNOWN", EL.allowed_targets("UNKNOWN"))

    def test_simulation_owner_accepts_only_executable_fill_edges(self):
        self.assertEqual(
            (EL.STATE_SUBMITTED, EL.STATE_PARTIAL_FILLED),
            EL.assert_simulated_transition("pending_execution", "partially_filled"),
        )
        self.assertEqual(
            (EL.STATE_PARTIAL_FILLED, EL.STATE_FILLED),
            EL.assert_simulated_transition("partially_filled", "filled"),
        )
        with self.assertRaises(EL.IllegalLifecycleTransition):
            EL.assert_simulated_transition("cancelled", "filled")

    def test_unknown_target_state_is_rejected(self):
        lifecycle = EL.OrderLifecycle(1)
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance("BOGUS")
        with self.assertRaises(EL.ExecutionLifecycleError):
            EL.OrderLifecycle(1, state="BOGUS")

    def test_unknown_source_state_is_treated_as_unknown(self):
        self.assertEqual(EL.allowed_targets("UNKNOWN"), EL.allowed_targets("WHATEVER"))


def _reject_evidence():
    return EE.evidence_from_order(
        {
            "id": 2, "side": "buy", "code": "600001", "qty": 100,
            "planned_price": 10.0, "status": "risk_rejected", "reason": "席位已满",
            "created_at": CREATED_AT, "order_type": "market",
        },
        [],
    )


class QuantityInvariantTests(unittest.TestCase):
    """M53：部分成交绝不能被提升为全部成交。"""

    def _accepted(self, order_id=1):
        lifecycle = EL.OrderLifecycle(order_id, created_at=CREATED_AT)
        lifecycle.advance("SUBMITTED", source="order_submit")
        lifecycle.advance("ACCEPTED", source="venue_ack")
        return lifecycle

    def test_partial_filled_requires_a_strictly_partial_quantity(self):
        lifecycle = self._accepted()
        lifecycle.advance(
            "PARTIAL_FILLED", requested_qty=1000, filled_qty=300,
            fill_price=10.01, fill_session="2026-09-16",
        )
        self.assertEqual(EL.STATE_PARTIAL_FILLED, lifecycle.status)

    def test_partial_filled_rejects_a_full_quantity(self):
        lifecycle = self._accepted()
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance(
                "PARTIAL_FILLED", requested_qty=1000, filled_qty=1000,
                fill_price=10.01, fill_session="2026-09-16",
            )

    def test_partial_filled_rejects_a_zero_quantity(self):
        lifecycle = self._accepted()
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance(
                "PARTIAL_FILLED", requested_qty=1000, filled_qty=0,
                fill_price=10.01, fill_session="2026-09-16",
            )

    def test_filled_rejects_a_partial_quantity(self):
        """M53 的正面击杀点。"""
        lifecycle = self._accepted()
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance(
                "FILLED", requested_qty=1000, filled_qty=300,
                fill_price=10.01, fill_session="2026-09-16",
            )
        self.assertEqual(EL.STATE_ACCEPTED, lifecycle.status)

    def test_filled_requires_fill_quantity_evidence(self):
        """M51 的正面击杀点：没有成交数量证据即失败。"""
        lifecycle = self._accepted()
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance("FILLED", requested_qty=100, filled_qty=None)
        self.assertEqual(EL.STATE_ACCEPTED, lifecycle.status)

    def test_filled_requires_a_trustworthy_price_and_session(self):
        for price, session in ((None, "2026-09-16"), (0.0, "2026-09-16"),
                               (10.0, ""), (10.0, None)):
            lifecycle = self._accepted()
            with self.assertRaises(EL.IllegalLifecycleTransition):
                lifecycle.advance(
                    "FILLED", requested_qty=100, filled_qty=100,
                    fill_price=price, fill_session=session,
                )

    def test_filled_requires_a_proven_requested_quantity(self):
        for requested in (None, 0):
            lifecycle = self._accepted()
            with self.assertRaises(EL.IllegalLifecycleTransition):
                lifecycle.advance(
                    "FILLED", requested_qty=requested, filled_qty=100,
                    fill_price=10.0, fill_session="2026-09-16",
                )

    def test_two_step_partial_fill_reaches_a_full_fill(self):
        lifecycle = self._accepted()
        lifecycle.advance(
            "PARTIAL_FILLED", requested_qty=1000, filled_qty=400,
            fill_price=10.0, fill_session="2026-09-16",
        )
        lifecycle.advance(
            "PARTIAL_FILLED", requested_qty=1000, filled_qty=900,
            fill_price=10.0, fill_session="2026-09-16",
        )
        lifecycle.advance(
            "FILLED", requested_qty=1000, filled_qty=1000,
            fill_price=10.0, fill_session="2026-09-16",
        )
        self.assertEqual(EL.STATE_FILLED, lifecycle.status)
        self.assertTrue(lifecycle.is_terminal)

    def test_rejected_cannot_carry_a_positive_fill(self):
        lifecycle = EL.OrderLifecycle(1, created_at=CREATED_AT)
        lifecycle.advance("SUBMITTED", source="order_submit")
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance("REJECTED", requested_qty=100, filled_qty=100,
                              fill_price=10.0, fill_session="2026-09-16")

    def test_cancelled_cannot_carry_a_complete_fill(self):
        lifecycle = self._accepted()
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance("CANCELLED", requested_qty=100, filled_qty=100,
                              fill_price=10.0, fill_session="2026-09-16")

    def test_cancelled_may_carry_a_partial_fill(self):
        lifecycle = self._accepted()
        lifecycle.advance("CANCELLED", requested_qty=1000, filled_qty=300,
                          fill_price=10.0, fill_session="2026-09-16")
        self.assertEqual(EL.STATE_CANCELLED, lifecycle.status)

    def test_submitted_cannot_carry_a_positive_fill(self):
        lifecycle = EL.OrderLifecycle(1, created_at=CREATED_AT)
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance("SUBMITTED", requested_qty=100, filled_qty=1)

    def test_quantities_are_taken_from_three_state_evidence(self):
        lifecycle = self._accepted()
        lifecycle.advance("FILLED", evidence=full_fill_evidence())
        self.assertEqual(EL.STATE_FILLED, lifecycle.status)

    def test_unknown_evidence_is_not_read_as_a_quantity(self):
        lifecycle = self._accepted()
        missing = EE.evidence_from_order(
            {
                "id": 3, "side": "buy", "code": "600001", "qty": 100,
                "planned_price": 10.0, "status": "filled", "reason": "",
                "created_at": CREATED_AT, "order_type": "market",
            },
            [],
        )
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance("FILLED", evidence=missing)


class RetrySemanticsTests(unittest.TestCase):
    """``REJECTED -> FILLED`` 只能通过**新的**委托生命周期表达。"""

    def test_retry_creates_a_fresh_lifecycle_linked_to_the_previous_order(self):
        rejected = EL.OrderLifecycle(1, created_at=CREATED_AT)
        rejected.advance("SUBMITTED", source="order_submit")
        rejected.advance("REJECTED", evidence=_reject_evidence(), source="venue_reject")
        retried = rejected.retry(2, at="2026-09-16 10:00:00")
        self.assertEqual(EL.STATE_CREATED, retried.status)
        self.assertEqual(1, retried.retry_of)
        self.assertEqual(2, retried.order_id)
        self.assertEqual(1, len(retried.history))
        self.assertFalse(retried.has_reached("REJECTED"))

    def test_a_retried_lifecycle_can_fill_because_it_is_a_new_order(self):
        rejected = EL.OrderLifecycle(1, created_at=CREATED_AT)
        rejected.advance("SUBMITTED", source="order_submit")
        rejected.advance("REJECTED", evidence=_reject_evidence(), source="venue_reject")
        retried = rejected.retry(2, at="2026-09-16 10:00:00")
        retried.advance("SUBMITTED", source="order_submit")
        retried.advance("ACCEPTED", source="venue_ack")
        retried.advance("FILLED", evidence=full_fill_evidence())
        self.assertEqual(EL.STATE_FILLED, retried.status)
        self.assertEqual(EL.STATE_REJECTED, rejected.status)

    def test_a_filled_order_cannot_be_retried(self):
        lifecycle = EL.OrderLifecycle(1, created_at=CREATED_AT)
        lifecycle.advance("SUBMITTED", source="order_submit")
        lifecycle.advance("ACCEPTED", source="venue_ack")
        lifecycle.advance("FILLED", evidence=full_fill_evidence())
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.retry(2)


class CanonicalStateTests(unittest.TestCase):
    """仓库 stored status → 权威状态的唯一映射。"""

    def test_documented_mapping(self):
        expected = {
            "filled": EL.STATE_FILLED,
            "risk_rejected": EL.STATE_REJECTED,
            "rejected": EL.STATE_REJECTED,
            "unfilled_limit_down": EL.STATE_REJECTED,
            "cancelled": EL.STATE_CANCELLED,
            "superseded": EL.STATE_CANCELLED,
            "expired": EL.STATE_EXPIRED,
            "pending_limit": EL.STATE_SUBMITTED,
            "deferred_capacity": EL.STATE_SUBMITTED,
            "entry_frozen_waitlist": EL.STATE_SUBMITTED,
            "execution_retry": EL.STATE_SUBMITTED,
            "manual_execution_retry": EL.STATE_SUBMITTED,
            "awaiting_batch": EL.STATE_SUBMITTED,
            "pending_verification": EL.STATE_SUBMITTED,
            "shadow_q3": EL.STATE_CREATED,
            "": EL.STATE_UNKNOWN,
            "something_new": EL.STATE_UNKNOWN,
        }
        for stored, state in expected.items():
            self.assertEqual(state, EL.canonical_state(stored), stored)

    def test_no_stored_status_may_claim_acceptance(self):
        """仓库没有场所受理证据，ACCEPTED 不允许被任何 stored status 冒充。"""
        self.assertEqual(frozenset(), EL.ACCEPTED_STORED_STATUSES)
        for stored in (
            "filled", "risk_rejected", "cancelled", "expired", "pending_limit",
            "execution_retry", "awaiting_batch", "pending_verification", "shadow_q3",
        ):
            self.assertNotEqual(EL.STATE_ACCEPTED, EL.canonical_state(stored), stored)

    def test_has_fill_only_refines_submitted_into_partial(self):
        self.assertEqual(EL.STATE_PARTIAL_FILLED, EL.canonical_state("pending_limit", has_fill=True))
        for stored in ("risk_rejected", "cancelled", "expired", "shadow_q3", "filled"):
            without = EL.canonical_state(stored)
            self.assertEqual(without, EL.canonical_state(stored, has_fill=True), stored)

    def test_shadow_records_are_created_not_submitted(self):
        """影子记录从未提交，因此永远不能合法地成交。"""
        self.assertEqual(EL.STATE_CREATED, EL.canonical_state("shadow_q3"))
        self.assertFalse(EL.can_transition(EL.STATE_CREATED, EL.STATE_FILLED))


class ObservedFillSupportedTests(unittest.TestCase):
    """"订单写着成交了"必须能被证据自证。"""

    def test_non_fill_states_are_trivially_supported(self):
        for state in (EL.STATE_CREATED, EL.STATE_SUBMITTED, EL.STATE_REJECTED):
            verdict = EL.observed_fill_supported(lifecycle_state=state)
            self.assertTrue(verdict["supported"], state)

    def test_full_fill_needs_quantity_price_and_session(self):
        complete = EL.observed_fill_supported(
            lifecycle_state=EL.STATE_FILLED, requested_qty=100, filled_qty=100,
            fill_price=10.0, fill_session="2026-09-16",
        )
        self.assertTrue(complete["supported"])
        for kwargs in (
            {"requested_qty": 100, "filled_qty": None, "fill_price": 10.0,
             "fill_session": "2026-09-16"},
            {"requested_qty": 100, "filled_qty": 100, "fill_price": None,
             "fill_session": "2026-09-16"},
            {"requested_qty": 100, "filled_qty": 100, "fill_price": 0.0,
             "fill_session": "2026-09-16"},
            {"requested_qty": 100, "filled_qty": 100, "fill_price": 10.0, "fill_session": ""},
            {"requested_qty": 100, "filled_qty": 60, "fill_price": 10.0,
             "fill_session": "2026-09-16"},
            {"requested_qty": None, "filled_qty": 100, "fill_price": 10.0,
             "fill_session": "2026-09-16"},
        ):
            verdict = EL.observed_fill_supported(lifecycle_state=EL.STATE_FILLED, **kwargs)
            self.assertFalse(verdict["supported"], kwargs)
            self.assertIsNotNone(verdict["reason"])

    def test_partial_fill_requires_a_strictly_partial_quantity(self):
        self.assertTrue(EL.observed_fill_supported(
            lifecycle_state=EL.STATE_PARTIAL_FILLED, requested_qty=1000, filled_qty=300,
        )["supported"])
        self.assertFalse(EL.observed_fill_supported(
            lifecycle_state=EL.STATE_PARTIAL_FILLED, requested_qty=1000, filled_qty=1000,
        )["supported"])


class AuditStoredRowsTests(unittest.TestCase):
    def test_unsupported_fill_claims_are_listed(self):
        rows = [
            {"id": 1, "status": "filled", "requested_qty": 100, "filled_qty": 100,
             "fill_price": 10.0, "fill_session": "2026-09-16"},
            {"id": 2, "status": "filled", "requested_qty": 100, "filled_qty": None,
             "fill_price": None, "fill_session": None},
            {"id": 3, "status": "risk_rejected"},
        ]
        report = EL.audit_stored_rows(rows)
        self.assertEqual(3, report["rows"])
        self.assertEqual(2, report["state_counts"][EL.STATE_FILLED])
        self.assertEqual(1, report["state_counts"][EL.STATE_REJECTED])
        self.assertEqual([2], [item["order_id"] for item in report["unsupported_fill_claims"]])

    def test_empty_input_is_safe(self):
        report = EL.audit_stored_rows([])
        self.assertEqual(0, report["rows"])
        self.assertEqual([], report["unsupported_fill_claims"])


class LifecycleSerializationTests(unittest.TestCase):
    def test_as_dict_records_the_full_history_with_sources(self):
        lifecycle = EL.OrderLifecycle(7, created_at=CREATED_AT, retry_of=6)
        lifecycle.advance("SUBMITTED", at="2026-09-16 09:31:01", source="order_submit")
        lifecycle.advance("ACCEPTED", at="2026-09-16 09:31:02", source="venue_ack")
        lifecycle.advance("FILLED", at="2026-09-16 09:31:03", source="fill_row",
                          evidence=full_fill_evidence())
        payload = lifecycle.as_dict()
        self.assertEqual(EL.EXECUTION_LIFECYCLE_VERSION, payload["version"])
        self.assertEqual(6, payload["retry_of"])
        self.assertTrue(payload["terminal"])
        self.assertEqual(
            ["CREATED", "SUBMITTED", "ACCEPTED", "FILLED"],
            [event["status"] for event in payload["history"]],
        )
        self.assertEqual("fill_row", payload["history"][-1]["source"])
        self.assertTrue(lifecycle.has_reached("SUBMITTED"))
        self.assertFalse(lifecycle.has_reached("REJECTED"))


class ArchitectureGuardTests(unittest.TestCase):
    def test_module_stays_pure_stdlib_and_read_only(self):
        path = os.path.join(BACKEND_DIR, "execution_lifecycle.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        upper = source.upper()
        for keyword in ("INSERT INTO", "DELETE FROM", "DROP TABLE", "SQLITE3"):
            self.assertNotIn(keyword, upper, keyword)
        self.assertNotIn("import execution_evidence", source)
        self.assertNotIn("import paper_trading\n", source)
        modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                modules.add(node.module.split(".")[0])
        self.assertEqual(
            set(), modules - {"collections", "dataclasses", "typing", "__future__"}, modules
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
