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


if __name__ == "__main__":
    unittest.main()
