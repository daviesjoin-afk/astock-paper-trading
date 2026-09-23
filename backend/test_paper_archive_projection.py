# -*- coding: utf-8 -*-
import json
import unittest

import paper_archive_projection as projection


class PaperArchiveProjectionTests(unittest.TestCase):
    def test_projects_orders_as_read_only_and_drops_large_payload(self):
        archives = [{
            "cycle_key": "cycle-1",
            "snapshot": json.dumps({
                "paper_accounts": [{"id": "acct", "name": "策略 A"}],
                "paper_orders": [{
                    "id": 7, "account_id": "acct", "code": "000001",
                    "created_at": "2026-09-02 10:00:00", "risk_payload": {"large": True},
                }],
            }),
        }]
        rows = projection.project_order_rows(archives, lambda value, default=None: json.loads(value) if value else default)
        self.assertEqual(rows[0]["account_name"], "策略 A")
        self.assertTrue(rows[0]["read_only"])
        self.assertNotIn("risk_payload", rows[0])

    def test_deduplicates_and_skips_corrupt_archives(self):
        archives = [
            {"cycle_key": "cycle-1", "snapshot": "not-json"},
            {"cycle_key": "cycle-2", "snapshot": json.dumps({"paper_orders": [{"id": 1, "created_at": "x"}]})},
            {"cycle_key": "cycle-2", "snapshot": json.dumps({"paper_orders": [{"id": 1, "created_at": "x"}]})},
        ]
        rows = projection.project_order_rows(archives, lambda value, default=None: json.loads(value) if value else default)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["archived_cycle"], "cycle-2")

    def test_partial_execution_archive_keeps_fill_evidence_but_drops_full_payload(self):
        evidence = {"market_evidence": {"quote_at": "2026-09-08T10:00:00",
                                         "quote_validation": "cross_source_checked"}}
        archives = [{
            "cycle_key": "cycle-3",
            "snapshot": json.dumps({
                "paper_accounts": [{"id": "acct", "name": "策略 A"}],
                "paper_orders": [{"id": 9, "account_id": "acct", "qty": 300,
                                  "status": "partially_filled", "risk_payload": {
                                      "execution": {"as_of": "2026-09-08T10:00:00",
                                                    "market_state": "cross_source_checked",
                                                    "market_evidence": evidence["market_evidence"]},
                                  }}],
                "paper_fills": [{"id": 10, "order_id": 9, "qty": 100,
                                 "execution_asof": "2026-09-08T10:00:00",
                                 "slippage_amount": 1.0,
                                 "execution_evidence": json.dumps(evidence)}],
            }),
        }]

        row = projection.project_order_rows(
            archives, lambda value, default=None: json.loads(value) if value else default,
        )[0]

        self.assertEqual((300, 100, 200), (
            row["desired_qty"], row["filled_qty"], row["remaining_qty"],
        ))
        self.assertEqual("2026-09-08T10:00:00", row["execution_asof"])
        self.assertEqual("cross_source_checked", row["market_data_validation"])
        self.assertEqual(1.0, row["slippage_amount"])
        self.assertEqual([], row["allowed_actions"])
        self.assertNotIn("risk_payload", row)


if __name__ == "__main__":
    unittest.main()
