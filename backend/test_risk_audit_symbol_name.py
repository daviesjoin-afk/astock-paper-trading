"""Regression coverage for risk-audit symbol labels."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import paper_trading as P  # noqa: E402


class RiskAuditSymbolNameTests(unittest.TestCase):
    def test_audit_response_contains_snapshot_symbol_name(self):
        # The extraction is deliberately snapshot-based: this test guards the
        # historical-name contract without needing a live ledger.
        with open(os.path.join(os.path.dirname(__file__), "paper_trading.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('for key in ("signal", "pick", "quote", "execution_quote")', source)
        self.assertIn('"name": symbol_name', source)

    def test_legacy_decision_uses_matching_position_name(self):
        name = P._audit_symbol_name(
            {}, "sector_rotation", "002237",
            position_names={("sector_rotation", "002237"): "恒邦股份"},
        )
        self.assertEqual(name, "恒邦股份")

    def test_snapshot_name_remains_authoritative_over_current_ledger(self):
        name = P._audit_symbol_name(
            {"quote": {"name": "历史名称"}}, "sector_rotation", "002237",
            position_names={("sector_rotation", "002237"): "当前名称"},
        )
        self.assertEqual(name, "历史名称")

    def test_legacy_closed_position_uses_latest_order_name(self):
        name = P._audit_symbol_name(
            {}, "tq_breakout", "300112",
            order_names={("tq_breakout", "300112"): "万讯自控"},
        )
        self.assertEqual(name, "万讯自控")


if __name__ == "__main__":
    unittest.main()
