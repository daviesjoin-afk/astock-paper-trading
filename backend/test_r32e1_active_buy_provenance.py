# -*- coding: utf-8 -*-
"""R32-E1：真实 Active BUY 路径的 exact order-linked decision provenance。

这些用例**不手工 INSERT** ``paper_risk_decisions``：它们驱动真实的
``PT._buy_order``（真实门禁、真实 owner 输出、真实订单行），然后检查
write-time provenance 与 exact order linkage，并确认 comparison 只消费
真正属于 Risk Authority 的那一行。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_trading as PT  # noqa: E402
import shadow_comparison as SC  # noqa: E402
import shadow_comparison_service as SCS  # noqa: E402
from test_strategy_buy_commit_convergence import (  # noqa: E402
    ACCOUNT, CODE, DAY_PREV, _BuyCase, _quote,
)


def _challenger_leg(*, allowed: bool, reasons=()):
    """A minimal Challenger observation for the pure risk dimension."""
    return {
        "symbol": CODE, "side": "buy",
        "candidate": {"desired_quantity": 100, "order_type": "market",
                      "reference_price": 10.0},
        "signal": {"outcome": "approved", "status": "pending", "reason": "fixture",
                   "evidence": None},
        "entry": {"allowed": allowed, "reasons": list(reasons), "gates": {},
                  "policy": None, "requires_manual_entry_review": False},
        "execution": None,
    }


class ActiveBuyDecisionProvenanceTests(unittest.TestCase):
    """R32-E1 —— 真实买入路径的 owner provenance 与 exact linkage。"""

    # Reuse the real production buy harness (its fixture and helpers) without
    # inheriting its test methods.
    _quotes = _BuyCase._quotes
    _seed_tradability_archive = _BuyCase._seed_tradability_archive
    add_signal = _BuyCase.add_signal
    run_buy = _BuyCase.run_buy

    def setUp(self):
        _BuyCase.setUp(self)
        self.quotes[CODE] = _quote(CODE)

    def tearDown(self):
        _BuyCase.tearDown(self)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _run_buy(self, *, veto=None, entry_block=None):
        """Drive the real buy path, injecting one owner's own output.

        The owner under test is replaced at its boundary (the Risk Authority's
        ``_shared_risk_state``, or an entry-owner gate): the gate composition,
        order creation, write-time provenance and linkage stay the production
        code under test. Nothing here writes a risk-decision row by hand.
        """
        patches = []
        if veto is not None:
            patches.append(mock.patch.object(PT, "_shared_risk_state",
                                             return_value=veto))
        if entry_block is not None:
            patches.append(mock.patch.object(PT, "_new_entry_price_gate",
                                             return_value=entry_block))
        for patch in patches:
            patch.start()
        try:
            signal_id = self.add_signal(signal_date=DAY_PREV.isoformat())
            result, order = self.run_buy(signal_id=signal_id)
        finally:
            for patch in reversed(patches):
                patch.stop()
        self.assertIsNotNone(order, "真实买入路径必须留下订单行")
        with PT._db() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT id,decision,reason,payload,order_id FROM paper_risk_decisions"
                " WHERE order_id=? ORDER BY id", (int(order["id"]),))]
        return result, order, rows

    @staticmethod
    def _authority(row):
        payload = json.loads(row["payload"] or "{}")
        return (payload.get("decision_provenance") or {}).get("authority")

    def _capture(self, order):
        with PT._db() as conn:
            return SCS.capture_active_comparison_evidence(
                conn, active_order_ids=(int(order["id"]),))

    def _risk_dimension(self, evidence, *, allowed=False, reasons=()):
        return SC._risk_dimension(
            evidence.orders[0], _challenger_leg(allowed=allowed, reasons=reasons), None)

    # ── 5.A 真实 Risk Authority veto → exact RISK evidence ───────────────────

    def test_r1_real_risk_authority_veto_is_exactly_linked(self):
        result, order, rows = self._run_buy(veto={
            "blocked": True,
            "reasons": ["共享资金池单日亏损 3.20% 触发熔断"],
            "daily_loss_pct": 3.2, "drawdown_pct": 1.0,
            "cooldown_until": None, "cooldown_active": False,
        })
        self.assertFalse(result.get("filled"))
        self.assertEqual("risk_rejected", order["status"])
        authorities = [self._authority(row) for row in rows]
        # The Risk Authority's own veto and the admission owner's composite
        # conclusion are two distinct, exactly-linked rows.
        self.assertIn("RISK", authorities)
        self.assertIn("ENTRY", authorities)
        self.assertEqual({int(order["id"])}, {int(row["order_id"]) for row in rows})
        risk_row = next(row for row in rows if self._authority(row) == "RISK")
        self.assertEqual("shared_risk_state_blocked", risk_row["decision"])
        self.assertIn("熔断", risk_row["reason"])
        # The captured comparison evidence exposes exactly that row as risk
        # evidence, and only that row.
        evidence = self._capture(order)
        linked = evidence.orders[0].risk_decision_evidence
        self.assertEqual({int(order["id"])},
                         {int(item["order_id"]) for item in linked})
        risk_authorities = [item["decision_provenance"]["authority"] for item in linked]
        self.assertEqual(1, risk_authorities.count("RISK"))
        self.assertEqual(1, risk_authorities.count("ENTRY"))
        dimension = self._risk_dimension(evidence)
        self.assertEqual("PRESENT", dimension["legs"]["active"])
        self.assertEqual("AVAILABLE", dimension["availability"])
        self.assertEqual([row["decision"] for row in rows if self._authority(row) == "RISK"],
                         [item["decision"] for item in
                          dimension["active"]["risk_decision_evidence"]])
        # The composite ENTRY row is never presented as risk evidence.
        self.assertEqual(1, len(dimension["active"]["risk_decision_evidence"]))
        self.assertEqual("RISK", dimension["active"]["risk_decision_evidence"][0][
            "decision_provenance"]["authority"])

    # ── 5.B/5.C/5.D 非 Risk Authority 拒绝 → 绝不冒充 RISK，也不降级 ──────────

    def test_r2_non_risk_rejection_is_never_labelled_risk(self):
        # The entry owner's own gate rejects: the admission owner composes it,
        # so the row's authority is the admission owner — never the Risk
        # Authority.
        result, order, rows = self._run_buy(
            entry_block=(False, "入场价格闸门：当前价高于该策略允许的入场区间"))
        self.assertFalse(result.get("filled"))
        self.assertNotEqual("pending_execution", order["status"])
        authorities = [self._authority(row) for row in rows]
        self.assertTrue(authorities)
        # No Risk Authority veto happened, so no row may claim RISK.
        self.assertNotIn("RISK", authorities)
        self.assertNotIn("shared_risk_state_blocked",
                         [row["decision"] for row in rows])
        # The composite admission conclusion is the admission owner's, exactly
        # linked to this order.
        self.assertIn("ENTRY", authorities)
        self.assertEqual({int(order["id"])}, {int(row["order_id"]) for row in rows})
        evidence = self._capture(order)
        linked = evidence.orders[0].risk_decision_evidence
        self.assertEqual([], [item for item in linked
                              if item["decision_provenance"]["authority"] == "RISK"])
        # No downgrade to make the dimension look complete: it stays UNAVAILABLE.
        dimension = self._risk_dimension(evidence)
        self.assertEqual("UNAVAILABLE", dimension["legs"]["active"])
        # One leg has evidence, the other none: PARTIAL, never AVAILABLE.
        self.assertEqual("PARTIAL", dimension["availability"])
        self.assertEqual("order_linked_rows_are_not_risk_authority_decisions",
                         dimension["active"]["reason"])
        self.assertIsNone(dimension["active"]["risk_decision_evidence"])

    def test_r3_execution_authority_rows_are_not_risk_evidence(self):
        """把 execution authority 的 order-linked row 放进同一订单也不进 risk。"""
        _result, order, rows = self._run_buy(veto={
            "blocked": True, "reasons": ["共享资金池滚动回撤 12.00% 触发风控"],
            "daily_loss_pct": 0.4, "drawdown_pct": 12.0,
            "cooldown_until": None, "cooldown_active": False})
        # The Execution Authority's own order-linked decision on the same order.
        with PT._db(immediate=True) as conn:
            PT._risk_log(conn, ACCOUNT, CODE, "buy", "execution_blocked",
                         "执行受阻", {"execution": "fixture"},
                         order_id=int(order["id"]), authority="EXECUTION",
                         decision_kind="execution_blocked")
        evidence = self._capture(order)
        linked = evidence.orders[0].risk_decision_evidence
        authorities = [item["decision_provenance"]["authority"] for item in linked]
        self.assertEqual(1, authorities.count("RISK"))
        self.assertEqual(1, authorities.count("ENTRY"))
        self.assertEqual(1, authorities.count("EXECUTION"))
        execution_row = next(item for item in linked
                             if item["decision_provenance"]["authority"] == "EXECUTION")
        self.assertEqual("execution_blocked", execution_row["decision"])
        dimension = self._risk_dimension(evidence)
        decisions = [item["decision"] for item in
                     dimension["active"]["risk_decision_evidence"]]
        self.assertNotIn("execution_blocked", decisions)
        self.assertEqual(3, dimension["active"]["detail"]["order_linked_rows_examined"])
        self.assertEqual(2, dimension["active"]["detail"][
            "order_linked_non_risk_authority_rows"])

    # ── 架构约束：owner 词表与 write-time 强制 ───────────────────────────────

    def test_r4_write_time_authority_is_required_and_owned(self):
        self.assertIn("ALLOCATION", PT.PSM.RISK_DECISION_AUTHORITIES)
        self.assertIn("TIMING", PT.PSM.RISK_DECISION_AUTHORITIES)
        with PT._db() as conn:
            with self.assertRaisesRegex(
                    ValueError, "order-linked risk decision requires an explicit authority"):
                PT._risk_log(conn, ACCOUNT, CODE, "buy", "x", "r", {}, order_id=1)


if __name__ == "__main__":
    unittest.main()
