# -*- coding: utf-8 -*-
"""R34-C exact canonical allocation-weight owner regressions."""
from __future__ import annotations

import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import portfolio_allocation_weights as PAW


class CanonicalAllocationWeightTests(unittest.TestCase):
    def pins(self, *accounts):
        return [{"account_id": account, "strategy_id": account,
                 "strategy_version": 1, "strategy_checksum": "a" * 64}
                for account in accounts]

    def row(self, account, weight=30, *, cycle=7, effective="2026-10-01"):
        return {
            "id": 1 if account == "s1" else 2,
            "account_id": account, "cycle_id": cycle,
            "version": "v1", "style": "momentum",
            "effective_date": effective,
            "created_at": "2026-10-01T09:00:00+08:00",
            "reason": "test owner fact",
            "params": {"adaptive_allocation": {
                "status": "active", "effective_date": effective,
                "weight_pct": weight,
            }},
        }

    def test_rc10_active_exact_declaration_returns_source_fingerprint(self):
        resolved = PAW.resolve_canonical_allocation_weights(
            [self.row("s1", 30), self.row("s2", 70)],
            eligible_account_ids=("s1", "s2"), cycle_id=7,
            asof_day="2026-10-03", decision_at="2026-10-03T10:00:00+08:00",
            strategy_pins=self.pins("s1", "s2"))
        self.assertEqual({"s1": 0.3, "s2": 0.7}, resolved["weights"])
        self.assertIn("paper_parameter_versions:cycle:7",
                      resolved["source_identity"])
        self.assertEqual(64, len(resolved["source_fingerprint"]))

    def test_rc11_risk_cap_or_missing_weight_never_becomes_canonical_weight(self):
        row = {**self.row("s1"),
               "params": {"max_exposure": 0.85}}
        with self.assertRaisesRegex(
                PAW.AllocationWeightEvidenceUnavailable,
                "canonical_allocation_weight_declaration_unavailable"):
            PAW.resolve_canonical_allocation_weights(
                [row], eligible_account_ids=("s1",), cycle_id=7,
                asof_day="2026-10-03", decision_at="2026-10-03T10:00:00+08:00",
                strategy_pins=self.pins("s1"))

    def test_rc12_future_or_incomplete_declaration_fails_closed(self):
        with self.assertRaisesRegex(
                PAW.AllocationWeightEvidenceUnavailable,
                "canonical_allocation_weight_declaration_unavailable"):
            PAW.resolve_canonical_allocation_weights(
                [self.row("s1", effective="2026-10-04")],
                eligible_account_ids=("s1",), cycle_id=7,
                asof_day="2026-10-03", decision_at="2026-10-03T10:00:00+08:00",
                strategy_pins=self.pins("s1"))
        with self.assertRaisesRegex(
                PAW.AllocationWeightEvidenceUnavailable,
                "canonical_allocation_weight_declaration_unavailable"):
            PAW.resolve_canonical_allocation_weights(
                [self.row("s1")], eligible_account_ids=("s1", "s2"),
                cycle_id=7, asof_day="2026-10-03",
                decision_at="2026-10-03T10:00:00+08:00",
                strategy_pins=self.pins("s1", "s2"))

    def test_rc13_later_owner_rows_are_excluded_by_decision_at(self):
        old = self.row("s1", 30)
        old.update(id=11, created_at="2026-10-03T09:00:00+08:00")
        new = self.row("s1", 40)
        new.update(id=12, created_at="2026-10-03T14:00:00+08:00")
        at_ten = PAW.resolve_canonical_allocation_weights(
            [old, new], eligible_account_ids=("s1",), cycle_id=7,
            asof_day="2026-10-03", decision_at="2026-10-03T10:00:00+08:00",
            strategy_pins=self.pins("s1"))
        at_fifteen = PAW.resolve_canonical_allocation_weights(
            [old, new], eligible_account_ids=("s1",), cycle_id=7,
            asof_day="2026-10-03", decision_at="2026-10-03T15:00:00+08:00",
            strategy_pins=self.pins("s1"))
        self.assertEqual({"s1": 0.3}, at_ten["weights"])
        self.assertEqual({"s1": 0.4}, at_fifteen["weights"])
        self.assertNotEqual(at_ten["source_fingerprint"],
                            at_fifteen["source_fingerprint"])


if __name__ == "__main__":
    unittest.main()
