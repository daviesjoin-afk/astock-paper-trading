# -*- coding: utf-8 -*-
"""selection executable × execution verified 连接层测试。

每条测试对应一个**可证伪**陈述。变异脚本
``work/execution_reality_mutation_check.py`` 的 M54（execution return fallback
market return）由本文件抓住。

核心不变式：

* ``selection_executable=True`` 且 ``execution_verified=False`` **必须允许存在**；
* 没有成交证据时 ``execution_return`` 不成立，且 ``market_label_value``
  **绝不允许**被拿来顶替它；
* PR149 的 selection outcome 契约字段与口径**不被本层改动**。
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import unittest

import execution_evidence as EE
import execution_lifecycle as EL
import execution_outcome as EO
import selection_tradability as ST

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION = "2026-09-16"
EXIT_SESSION = "2026-09-17"
CREATED_AT = "2026-09-16 09:31:00"
BIG_MARKET_LABEL = 0.5


def selection_outcome(
    *,
    executable=True,
    market_label=BIG_MARKET_LABEL,
    entry_status=ST.STATUS_EXECUTABLE,
    exit_status=ST.STATUS_EXECUTABLE,
    selected=True,
):
    return ST.ExecutableSelectionOutcome(
        sample_key="sample-1",
        security_code="600001",
        selected=selected,
        entry_status=entry_status,
        entry_reason=ST.REASON_OK,
        exit_status=exit_status,
        exit_reason=ST.REASON_OK,
        entry_executable=entry_status == ST.STATUS_EXECUTABLE,
        executable=executable,
        market_label_status="ok",
        market_label_value=market_label,
        executable_return=market_label if executable else None,
    )


def evidence(
    *,
    status="filled",
    side="buy",
    qty=100,
    price=10.0,
    planned=10.0,
    fills=None,
    session=SESSION,
    reason="",
    code="600001",
):
    if fills is None and status == "filled":
        amount = qty * price
        fills = [{"qty": qty, "price": price, "amount": amount, "fees": 0.1,
                  "fill_date": session}]
    return EE.evidence_from_order(
        {
            "id": 1, "side": side, "code": code, "qty": qty,
            "planned_price": planned, "status": status, "reason": reason,
            "created_at": CREATED_AT, "order_type": "market",
        },
        fills if fills is not None else [],
        # 这里按构造期自带身份构造证据，**没有**按 order_id 关联过账本，
        # 因此显式声明"未核对"，不冒充已核对。
        fill_identity_known=False,
    )


class ConceptSeparationTests(unittest.TestCase):
    """两个概念必须分开，且四种组合都合法存在。"""

    def test_selection_executable_with_unverified_execution_is_allowed(self):
        """本题的核心陈述："选出来"不等于"能成交"，也不等于"成交了"。"""
        outcome = EO.link_execution_outcome(
            selection_outcome(executable=True), evidence(status="risk_rejected", reason="席位已满")
        )
        self.assertTrue(outcome.selection_executable)
        self.assertFalse(outcome.execution_verified)
        self.assertEqual(EO.EXECUTION_BUCKET_UNVERIFIED, EO.execution_bucket(outcome))
        self.assertIsNone(outcome.execution_return.maybe())
        self.assertFalse(outcome.execution_return.is_known)

    def test_all_four_combinations_are_legal_and_mutually_exclusive(self):
        cases = (
            (True, True, EO.EXECUTION_BUCKET_VERIFIED),
            (True, False, EO.EXECUTION_BUCKET_UNVERIFIED),
            (False, True, EO.EXECUTION_BUCKET_WITHOUT_SELECTION_PROOF),
            (False, False, EO.EXECUTION_BUCKET_NEITHER),
        )
        for selection_flag, execution_flag, bucket in cases:
            payload = selection_outcome(
                executable=selection_flag,
                entry_status=(ST.STATUS_EXECUTABLE if selection_flag else ST.STATUS_BLOCKED),
            )
            entry = evidence() if execution_flag else evidence(status="risk_rejected", reason="x")
            exit_leg = evidence(side="sell", price=11.0, session=EXIT_SESSION) if execution_flag else None
            outcome = EO.link_execution_outcome(payload, entry, exit_leg)
            self.assertEqual(selection_flag, outcome.selection_executable)
            self.assertEqual(execution_flag, outcome.execution_verified)
            self.assertEqual(bucket, EO.execution_bucket(outcome), bucket)

    def test_verified_execution_requires_both_legs(self):
        entry_only = EO.link_execution_outcome(selection_outcome(), evidence())
        self.assertFalse(entry_only.execution_verified)
        self.assertTrue(entry_only.entry_evidence.proves_fill())
        both = EO.link_execution_outcome(
            selection_outcome(), evidence(),
            evidence(side="sell", price=11.0, session=EXIT_SESSION),
        )
        self.assertTrue(both.execution_verified)

    def test_fill_verdict_describes_the_entry_leg_only(self):
        outcome = EO.link_execution_outcome(selection_outcome(), evidence())
        self.assertEqual(EE.FILL_VERDICT_VERIFIED, outcome.fill_verdict)
        self.assertFalse(outcome.execution_verified)

    def test_no_evidence_at_all_is_not_a_verified_execution(self):
        outcome = EO.link_execution_outcome(selection_outcome(), None, None)
        self.assertFalse(outcome.execution_verified)
        self.assertEqual(EE.FILL_VERDICT_UNKNOWN, outcome.fill_verdict)
        self.assertEqual(EO.EXECUTION_BUCKET_UNVERIFIED, EO.execution_bucket(outcome))


class ReturnLayeringTests(unittest.TestCase):
    """market / selection / execution 三层收益不得互相替代。"""

    def test_market_return_exists_without_any_fill(self):
        outcome = EO.link_execution_outcome(
            selection_outcome(), evidence(status="risk_rejected", reason="x")
        )
        self.assertTrue(outcome.market_return.is_known)
        self.assertEqual(BIG_MARKET_LABEL, outcome.market_return.require())
        self.assertEqual(EO.RETURN_SOURCE_MARKET_LABEL, outcome.market_return.source)

    def test_market_return_is_unknown_when_the_label_is_missing(self):
        outcome = EO.link_execution_outcome(selection_outcome(market_label=None))
        self.assertTrue(outcome.market_return.is_unknown)
        self.assertIsNone(outcome.market_return.maybe())

    def test_no_fill_means_execution_return_is_not_defined(self):
        """M54 的正面击杀点。"""
        outcome = EO.link_execution_outcome(
            selection_outcome(market_label=0.9),
            evidence(status="risk_rejected", reason="席位已满"),
        )
        holder = outcome.execution_return
        self.assertFalse(holder.is_known)
        self.assertTrue(holder.is_not_applicable)
        self.assertIsNone(holder.maybe())
        self.assertNotEqual(0.9, holder.maybe())
        self.assertNotEqual(EO.RETURN_SOURCE_MARKET_LABEL, holder.source)

    def test_the_market_label_is_never_substituted_for_the_execution_return(self):
        """M54：把 ``market_label_value`` 顶替执行收益必须被拒绝。"""
        entry = evidence(status="risk_rejected", reason="席位已满")
        outcome = EO.link_execution_outcome(selection_outcome(market_label=0.9), entry)
        self.assertIsNone(outcome.execution_return.maybe())
        forged = dataclasses.replace(
            outcome,
            execution_return=EE.EvidenceField.known(
                "execution_return", 0.9, source=EO.RETURN_SOURCE_MARKET_LABEL
            ),
        )
        with self.assertRaises(EO.MarketLabelSubstitution):
            EO.assert_no_market_label_substitution(forged)

    def test_a_known_execution_return_requires_a_verified_execution(self):
        outcome = EO.link_execution_outcome(selection_outcome(), evidence())
        forged = dataclasses.replace(
            outcome,
            execution_verified=False,
            execution_return=EE.EvidenceField.known(
                "execution_return", 0.1, source=EO.RETURN_SOURCE_REALIZED_FILLS
            ),
        )
        with self.assertRaises(EO.MarketLabelSubstitution):
            EO.assert_no_market_label_substitution(forged)

    def test_the_legs_must_be_a_complementary_round_trip_on_one_security(self):
        """两笔同向成交不是往返；``buy X`` 配 ``sell Y`` 也不是。"""
        cases = {
            "two_buys": (evidence(side="buy", price=10.0),
                         evidence(side="buy", price=11.0, session=EXIT_SESSION)),
            "two_sells": (evidence(side="sell", price=10.0),
                          evidence(side="sell", price=11.0, session=EXIT_SESSION)),
            "different_securities": (
                evidence(side="buy", code="600001", price=10.0),
                evidence(side="sell", code="600002", price=11.0, session=EXIT_SESSION),
            ),
        }
        for name, (entry_leg, exit_leg) in cases.items():
            with self.subTest(name):
                self.assertTrue(entry_leg.proves_fill())
                self.assertTrue(exit_leg.proves_fill())
                outcome = EO.link_execution_outcome(selection_outcome(), entry_leg, exit_leg)
                self.assertFalse(outcome.execution_verified, name)
                self.assertFalse(outcome.execution_return.is_known, name)
                self.assertIsNone(outcome.execution_return.maybe(), name)
                self.assertNotEqual(
                    EO.RETURN_SOURCE_REALIZED_FILLS, outcome.execution_return.source, name
                )
                # 选股事实与市场反事实不受影响：被否掉的只是"执行已验证"。
                self.assertTrue(outcome.selection_executable, name)
                self.assertTrue(outcome.market_return.is_known, name)

    def test_an_unknown_leg_direction_cannot_be_read_as_a_round_trip(self):
        """方向未知 = 无法证明互补，一律 fail closed。"""
        entry_leg = evidence(side="", status="pending_limit", fills=[])
        exit_leg = evidence(side="sell", price=11.0, session=EXIT_SESSION)
        outcome = EO.link_execution_outcome(selection_outcome(), entry_leg, exit_leg)
        self.assertFalse(outcome.execution_verified)
        self.assertFalse(outcome.execution_return.is_known)

    def test_equal_partial_fills_do_not_yield_a_known_execution_return(self):
        """两腿等量部分成交：数量相等 **不等于** 两腿都完整成交。

        这正是 ``link_execution_outcome`` 曾经自己抛异常的那条路径：收益是
        ``known`` 而 ``execution_verified`` 为假，被
        :func:`assert_no_market_label_substitution` 判成"用标签冒充收益"。
        """
        partial_fill = lambda session, price: [  # noqa: E731
            {"qty": 50, "price": price, "amount": 50 * price, "fees": 0.1,
             "fill_date": session}
        ]
        entry_leg = evidence(qty=100, price=10.0, fills=partial_fill(SESSION, 10.0))
        exit_leg = evidence(
            side="sell", qty=100, price=11.0, session=EXIT_SESSION,
            fills=partial_fill(EXIT_SESSION, 11.0),
        )
        self.assertEqual(EE.FILL_VERDICT_PARTIAL, entry_leg.fill_verdict_value())
        self.assertEqual(EE.FILL_VERDICT_PARTIAL, exit_leg.fill_verdict_value())
        self.assertFalse(entry_leg.proves_fill())
        self.assertFalse(exit_leg.proves_fill())
        self.assertEqual(50, entry_leg.filled_qty.require())
        self.assertEqual(50, exit_leg.filled_qty.require())

        holder = EO.realized_execution_return(entry_leg, exit_leg)
        self.assertFalse(holder.is_known)
        self.assertTrue(holder.is_unknown)
        self.assertIsNone(holder.maybe())

        # 受支持的 ``fill_partial`` 现场必须能被表达与审计，而不是抛异常。
        outcome = EO.link_execution_outcome(selection_outcome(), entry_leg, exit_leg)
        self.assertFalse(outcome.execution_verified)
        self.assertTrue(outcome.execution_return.is_unknown)
        self.assertEqual(EE.FILL_VERDICT_PARTIAL, outcome.fill_verdict)
        self.assertEqual(
            EO.EXECUTION_BUCKET_UNVERIFIED, EO.execution_bucket(outcome)
        )

    def test_entry_only_fill_leaves_the_execution_return_unknown(self):
        outcome = EO.link_execution_outcome(selection_outcome(), evidence())
        self.assertTrue(outcome.execution_return.is_unknown)
        self.assertIsNone(outcome.execution_return.maybe())

    def test_execution_return_is_computed_from_real_fills(self):
        entry = evidence(qty=100, price=10.0)
        exit_leg = evidence(side="sell", qty=100, price=11.0, session=EXIT_SESSION)
        outcome = EO.link_execution_outcome(selection_outcome(), entry, exit_leg)
        cost = 100 * 10.0 + 0.1
        proceeds = 100 * 11.0 - 0.1
        self.assertTrue(outcome.execution_verified)
        self.assertTrue(outcome.execution_return.is_known)
        self.assertAlmostEqual((proceeds - cost) / cost, outcome.execution_return.require())
        self.assertEqual(EO.RETURN_SOURCE_REALIZED_FILLS, outcome.execution_return.source)

    def test_execution_return_refuses_a_non_clean_round_trip(self):
        entry = evidence(qty=100, price=10.0)
        exit_leg = evidence(side="sell", qty=60, price=11.0, session=EXIT_SESSION)
        outcome = EO.link_execution_outcome(selection_outcome(), entry, exit_leg)
        self.assertTrue(outcome.execution_return.is_unknown)
        self.assertIsNone(outcome.execution_return.maybe())

    def test_execution_return_is_unknown_when_fee_evidence_is_missing(self):
        entry = evidence()
        exit_leg = evidence(side="sell", price=11.0, session=EXIT_SESSION)
        stripped = dataclasses.replace(
            exit_leg, fees=EE.EvidenceField.unknown("fees")
        )
        outcome = EO.link_execution_outcome(selection_outcome(), entry, stripped)
        self.assertTrue(outcome.execution_return.is_unknown)

    def test_selection_return_is_a_counterfactual_not_a_fill(self):
        outcome = EO.link_execution_outcome(
            selection_outcome(executable=True, market_label=0.42),
            evidence(status="risk_rejected", reason="席位已满"),
        )
        self.assertTrue(outcome.selection_return.is_known)
        self.assertEqual(EO.RETURN_SOURCE_EXECUTABLE_SELECTION, outcome.selection_return.source)
        self.assertNotEqual(EO.RETURN_SOURCE_REALIZED_FILLS, outcome.selection_return.source)

    def test_selection_return_does_not_apply_to_a_blocked_selection(self):
        outcome = EO.link_execution_outcome(
            selection_outcome(executable=False, entry_status=ST.STATUS_BLOCKED)
        )
        self.assertTrue(outcome.selection_return.is_not_applicable)
        self.assertTrue(outcome.market_return.is_known)

    def test_the_three_return_layers_are_mutually_distinct(self):
        entry = evidence(qty=100, price=10.0)
        exit_leg = evidence(side="sell", qty=100, price=11.0, session=EXIT_SESSION)
        outcome = EO.link_execution_outcome(selection_outcome(), entry, exit_leg)
        self.assertNotEqual(
            outcome.market_return.require(), outcome.execution_return.require()
        )
        self.assertNotEqual(
            outcome.selection_return.require(), outcome.execution_return.require()
        )
        sources = {
            outcome.market_return.source,
            outcome.selection_return.source,
            outcome.execution_return.source,
        }
        self.assertEqual(3, len(sources), sources)
        self.assertTrue(math.isfinite(outcome.execution_return.require()))


class Pr149ContractPreservationTests(unittest.TestCase):
    """PR149 的 PIT tradability contract 不得被本层改动。"""

    PR149_OUTCOME_FIELDS = frozenset({
        "sample_key", "security_code", "selected", "entry_status", "entry_reason",
        "intended_entry_session", "actual_entry_session", "exit_status", "exit_reason",
        "intended_exit_session", "actual_exit_session", "entry_executable", "executable",
        "market_label_status", "market_label_value", "executable_return", "policy_version",
    })

    def test_selection_outcome_contract_is_unchanged(self):
        fields = {item.name for item in dataclasses.fields(ST.ExecutableSelectionOutcome)}
        self.assertEqual(self.PR149_OUTCOME_FIELDS, fields)
        self.assertEqual("selection-tradability-v1", ST.TRADABILITY_CONTRACT_VERSION)
        self.assertEqual(
            ("executable", "blocked", "unproven", "invalid"), ST.TRADABILITY_STATUSES
        )

    def test_selection_bucket_still_comes_from_the_pr149_authority(self):
        blocked = selection_outcome(
            executable=False, entry_status=ST.STATUS_BLOCKED,
            exit_status=ST.STATUS_UNPROVEN,
        )
        outcome = EO.link_execution_outcome(blocked)
        self.assertEqual(ST.outcome_bucket(blocked), outcome.selection_bucket)
        self.assertEqual(ST.OUTCOME_BUCKET_BLOCKED_ENTRY, outcome.selection_bucket)

    def test_linking_does_not_mutate_the_selection_outcome(self):
        payload = selection_outcome(executable=True)
        before = payload.as_dict()
        EO.link_execution_outcome(payload, evidence())
        self.assertEqual(before, payload.as_dict())
        self.assertEqual(BIG_MARKET_LABEL, payload.market_label_value)
        self.assertTrue(payload.executable)

    def test_selection_executable_is_passed_through_verbatim(self):
        for flag in (True, False):
            payload = selection_outcome(
                executable=flag,
                entry_status=(ST.STATUS_EXECUTABLE if flag else ST.STATUS_BLOCKED),
            )
            self.assertEqual(flag, EO.link_execution_outcome(payload).selection_executable)

    def test_module_does_not_reimplement_the_tradability_bucket_priority(self):
        source_path = os.path.join(BACKEND_DIR, "execution_outcome.py")
        with open(source_path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("ST.outcome_bucket", source)
        self.assertNotIn("OUTCOME_BUCKET_BLOCKED_EXIT", source)


class ReportTests(unittest.TestCase):
    def _outcomes(self):
        verified = EO.link_execution_outcome(
            selection_outcome(), evidence(),
            evidence(side="sell", price=11.0, session=EXIT_SESSION),
        )
        unverified = EO.link_execution_outcome(
            selection_outcome(), evidence(status="risk_rejected", reason="席位已满")
        )
        executed_only = EO.link_execution_outcome(
            selection_outcome(executable=False, entry_status=ST.STATUS_BLOCKED),
            evidence(),
            evidence(side="sell", price=11.0, session=EXIT_SESSION),
        )
        neither = EO.link_execution_outcome(
            selection_outcome(executable=False, entry_status=ST.STATUS_BLOCKED),
            evidence(status="expired", reason="超时"),
        )
        return [verified, unverified, executed_only, neither]

    def test_report_accounts_for_every_outcome(self):
        report = EO.build_execution_reality_report(self._outcomes())
        self.assertEqual(4, report["outcomes"])
        self.assertTrue(report["accounted"])
        self.assertEqual(1, report[EO.EXECUTION_BUCKET_VERIFIED])
        self.assertEqual(1, report[EO.EXECUTION_BUCKET_UNVERIFIED])
        self.assertEqual(1, report[EO.EXECUTION_BUCKET_WITHOUT_SELECTION_PROOF])
        self.assertEqual(1, report[EO.EXECUTION_BUCKET_NEITHER])
        self.assertEqual(2, report["selection_executable"])
        self.assertEqual(2, report["execution_verified"])
        self.assertTrue(report["selection_fact_preserved"])
        self.assertTrue(report["execution_return_requires_fill"])

    def test_report_is_json_serializable_and_empty_safe(self):
        json.dumps(EO.build_execution_reality_report(self._outcomes()))
        empty = EO.build_execution_reality_report([])
        self.assertEqual(0, empty["outcomes"])
        self.assertTrue(empty["accounted"])

    def test_report_counts_inconsistencies_from_the_evidence(self):
        report = EO.build_execution_reality_report(
            [EO.link_execution_outcome(selection_outcome(), evidence(status="filled", fills=[]))]
        )
        self.assertEqual(
            1, report["inconsistency_counts"][EE.INCONSISTENCY_STORED_FILLED_WITHOUT_FILL_ROW]
        )

    def test_eligibility_requires_both_layers_by_default(self):
        outcomes = self._outcomes()
        strict = EO.execution_eligibility(outcomes)
        self.assertEqual(1, strict["eligible"])
        self.assertEqual(2, strict["verified_executions"])
        self.assertEqual(1, len(strict["returns"]))
        self.assertTrue(all(value is not None for value in strict["returns"]))
        loose = EO.execution_eligibility(outcomes, require_selection=False)
        self.assertEqual(2, loose["eligible"])

    def test_eligibility_returns_never_carry_a_market_label(self):
        outcomes = self._outcomes()
        payload = EO.execution_eligibility(outcomes)
        for outcome in outcomes:
            if outcome.execution_return.maybe() is not None:
                self.assertEqual(
                    EO.RETURN_SOURCE_REALIZED_FILLS, outcome.execution_return.source
                )
        self.assertEqual(len(payload["returns"]), payload["eligible"])

    def test_as_dict_is_serializable(self):
        payload = EO.link_execution_outcome(
            selection_outcome(), evidence(),
            evidence(side="sell", price=11.0, session=EXIT_SESSION),
        ).as_dict()
        json.dumps(payload)
        self.assertEqual(EO.EXECUTION_OUTCOME_VERSION, payload["version"])
        self.assertEqual(EO.EXECUTION_BUCKET_VERIFIED, payload["execution_bucket"])
        for name in EE.RETURN_FIELDS:
            self.assertIn(name, payload)


class LifecycleBridgeTests(unittest.TestCase):
    """状态机与证据层必须给出同一结论。"""

    def test_a_verified_fill_can_be_walked_to_filled(self):
        entry = evidence()
        lifecycle = EL.OrderLifecycle(1, created_at=CREATED_AT)
        lifecycle.advance("SUBMITTED", source="order_submit")
        lifecycle.advance("ACCEPTED", source="venue_ack")
        lifecycle.advance("FILLED", evidence=entry, source="fill_row")
        self.assertEqual(EL.STATE_FILLED, lifecycle.status)
        self.assertTrue(entry.proves_fill())

    def test_a_stored_filled_row_without_fill_rows_cannot_be_walked_to_filled(self):
        entry = evidence(status="filled", fills=[])
        lifecycle = EL.OrderLifecycle(1, created_at=CREATED_AT)
        lifecycle.advance("SUBMITTED", source="order_submit")
        lifecycle.advance("ACCEPTED", source="venue_ack")
        with self.assertRaises(EL.IllegalLifecycleTransition):
            lifecycle.advance("FILLED", evidence=entry)
        self.assertFalse(entry.proves_fill())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
