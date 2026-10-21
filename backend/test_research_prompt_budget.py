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


class DisplayIdTests(unittest.TestCase):
    """全市场横截面的 evidence_id 有 39KB，模型复述它会耗尽输出预算。"""

    def test_long_id_is_shortened_below_the_display_limit(self):
        long_id = "live_market|full_market_snapshot|" + "|".join(
            "%06d" % i for i in range(2000))
        short = P._short_evidence_id(long_id)
        self.assertLess(len(short), P._MAX_DISPLAY_ID_CHARS)
        self.assertLess(len(short), 64)

    def test_short_id_passes_through_unchanged(self):
        self.assertEqual("abc123", P._short_evidence_id("abc123"))

    def test_shortened_id_restores_to_the_full_one(self):
        long_id = "live_market|snapshot|" + "|".join("%06d" % i for i in range(1500))
        short = P._short_evidence_id(long_id)
        payload = {"evidence_relations": [{"evidence_id": short, "relation": "supports"}]}
        restored = P._restore_display_ids(payload, {long_id: short})
        self.assertEqual(long_id, restored["evidence_relations"][0]["evidence_id"])

    def test_unknown_ids_are_left_alone_so_validation_still_rejects_them(self):
        payload = {"evidence_relations": [{"evidence_id": "made-up", "relation": "supports"}]}
        out = P._restore_display_ids(payload, {"full": "short"})
        self.assertEqual("made-up", out["evidence_relations"][0]["evidence_id"])

    def test_output_budget_leaves_room_for_reasoning(self):
        # 实测 1800 里 1071 被 reasoning 吃掉，正文被截断。
        self.assertGreaterEqual(P.RESEARCH_MAX_OUTPUT_TOKENS, 3000)


if __name__ == "__main__":
    unittest.main()
