# -*- coding: utf-8 -*-
"""研究 prompt 的体积预算。

背景：全市场行情快照的原始 payload 有 3.1MB，加上 39KB 的 evidence_id 会把
请求体推到 3.2MB，上游直接 400 拒绝，分析在任何模型上都不可能成功。这里锁住
「超限时只保留结构与样本」的行为。
"""
import json
import unittest

import ai_research_provider as P


class PayloadCompactionTests(unittest.TestCase):
    def test_small_payload_passes_through_unchanged(self):
        payload = {"a": 1, "b": "x"}
        self.assertEqual(payload, P._compact_payload(payload))

    def test_oversized_list_keeps_total_and_sample(self):
        payload = {"rows": [{"i": i} for i in range(5000)]}
        compacted = P._compact_payload(payload)
        self.assertTrue(compacted["_truncated"])
        self.assertEqual(5000, compacted["rows"]["_total"])
        self.assertEqual(P._MAX_SAMPLE_ITEMS, len(compacted["rows"]["_sample"]))

    def test_compacted_payload_stays_under_the_budget(self):
        payload = {"rows": [{"code": "000001", "blob": "x" * 50} for _ in range(4000)]}
        compacted = P._compact_payload(payload)
        size = len(json.dumps(compacted, ensure_ascii=False))
        self.assertLess(size, P._MAX_PAYLOAD_BYTES)

    def test_budget_is_far_below_the_rejected_request_size(self):
        # 实测被拒绝的请求体约 3.2MB；预算必须留出足够余量。
        self.assertLess(P._MAX_PAYLOAD_BYTES, 100_000)


if __name__ == "__main__":
    unittest.main()
