"""调参历史 API 的空落地记录必须保持数组形状。"""
import sqlite3
import unittest

import ai_review_service as review


class TuningHistoryTests(unittest.TestCase):
    def test_null_and_applied_account_ids_have_a_stable_list_shape(self):
        with sqlite3.connect(':memory:') as conn:
            review.ensure_schema(conn)
            for applied in (None, '["tq_breakout"]'):
                conn.execute(
                    'INSERT INTO dual_ai_tuning_runs(trigger,mode,status,evidence_hash,evidence,created_at,finished_at,applied_ids) VALUES(?,?,?,?,?,?,?,?)',
                    ('manual', 'intraday', 'both_hold', 'test', '{}', '2026-10-22', '2026-10-22', applied),
                )
            rows = review.recent_runs(conn, 10)
            self.assertEqual(['tq_breakout'], rows[0]['applied_ids'])
            self.assertEqual([], rows[1]['applied_ids'])
