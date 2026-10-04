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
            "id": account, "cycle_id": cycle,
            "params": {"adaptive_allocation": {
                "status": "active", "effective_date": effective,
                "weight_pct": weight,
            }},
        }

    def test_rc10_active_exact_declaration_returns_source_fingerprint(self):
        resolved = PAW.resolve_canonical_allocation_weights(
            [self.row("s1", 30), self.row("s2", 70)],
            eligible_account_ids=("s1", "s2"), cycle_id=7,
            asof_day="2026-10-03", strategy_pins=self.pins("s1", "s2"))
        self.assertEqual({"s1": 0.3, "s2": 0.7}, resolved["weights"])
        self.assertIn("paper_accounts:adaptive_allocation:cycle:7",
                      resolved["source_identity"])
        self.assertEqual(64, len(resolved["source_fingerprint"]))

    def test_rc11_risk_cap_or_missing_weight_never_becomes_canonical_weight(self):
        row = {"id": "s1", "cycle_id": 7,
               "params": {"max_exposure": 0.85}}
        with self.assertRaisesRegex(
                PAW.AllocationWeightEvidenceUnavailable,
                "canonical_allocation_weight_declaration_unavailable"):
            PAW.resolve_canonical_allocation_weights(
                [row], eligible_account_ids=("s1",), cycle_id=7,
                asof_day="2026-10-03", strategy_pins=self.pins("s1"))

    def test_rc12_future_or_incomplete_declaration_fails_closed(self):
        with self.assertRaisesRegex(
                PAW.AllocationWeightEvidenceUnavailable,
                "canonical_allocation_weight_not_effective_asof"):
            PAW.resolve_canonical_allocation_weights(
                [self.row("s1", effective="2026-10-04")],
                eligible_account_ids=("s1",), cycle_id=7,
                asof_day="2026-10-03", strategy_pins=self.pins("s1"))
        with self.assertRaisesRegex(
                PAW.AllocationWeightEvidenceUnavailable,
                "allocation_weight_owner_coverage_mismatch"):
            PAW.resolve_canonical_allocation_weights(
                [self.row("s1")], eligible_account_ids=("s1", "s2"),
                cycle_id=7, asof_day="2026-10-03",
                strategy_pins=self.pins("s1", "s2"))


if __name__ == "__main__":
    unittest.main()
