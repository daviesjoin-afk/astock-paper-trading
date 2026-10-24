# -*- coding: utf-8 -*-
"""AI 调参落地链路的三个可单测修复。

- 单账户分歧不得丢弃其它账户已达成的共识
- confidence_threshold 是登记过的可进化参数，必须真的生效
（entry_score_delta 的增量比较在 test_evolution_apply 覆盖）
"""
import unittest

import ai_review_service as S

BASE_WEIGHTS = {"flow": 0.5, "mom_short": 0.5}


def _proposal(account_id, weights, entry=0.0, confidence=80):
    return {
        "account_id": account_id,
        "decision": "propose",
        "confidence": confidence,
        "weights": dict(weights),
        "entry_score_delta": entry,
        "conditions": {},
    }


def _accounts(*account_ids):
    return {
        account_id: {"weights": dict(BASE_WEIGHTS), "entry_score_delta": 0.0}
        for account_id in account_ids
    }


AGREE = {"flow": 0.52, "mom_short": 0.48}
DISAGREE = {"flow": 0.40, "mom_short": 0.60}


class ConsensusPartialTests(unittest.TestCase):
    """一个账户分歧不能把其它账户谈成的补丁一起丢掉。"""

    def test_agreeing_account_survives_a_disagreeing_sibling(self):
        accounts = _accounts("tq_breakout", "sector_rotation")
        proposals = {
            "ai1": [_proposal("tq_breakout", AGREE), _proposal("sector_rotation", AGREE)],
            "ai2": [_proposal("tq_breakout", AGREE), _proposal("sector_rotation", DISAGREE)],
        }
        ok, reason, merged, issues = S._check_consensus(proposals, accounts)
        self.assertTrue(ok, reason)
        self.assertEqual(["tq_breakout"], [m["account_id"] for m in merged])
        self.assertTrue(issues)

    def test_no_agreement_at_all_still_fails(self):
        accounts = _accounts("tq_breakout")
        proposals = {
            "ai1": [_proposal("tq_breakout", AGREE)],
            "ai2": [_proposal("tq_breakout", DISAGREE)],
        }
        ok, _reason, merged, _issues = S._check_consensus(proposals, accounts)
        self.assertFalse(ok)
        self.assertEqual([], merged)


class ConfidenceThresholdTests(unittest.TestCase):
    """confidence_threshold 可进化，必须影响共识判定。"""

    def _run(self, confidence, evolution=None):
        accounts = _accounts("tq_breakout")
        proposals = {
            "ai1": [_proposal("tq_breakout", AGREE, confidence=confidence)],
            "ai2": [_proposal("tq_breakout", AGREE, confidence=confidence)],
        }
        return S._check_consensus(proposals, accounts, evolution=evolution)

    def test_default_threshold_rejects_low_confidence(self):
        ok, _reason, merged, _issues = self._run(60)
        self.assertFalse(ok)
        self.assertEqual([], merged)

    def test_raised_threshold_rejects_what_default_would_accept(self):
        ok, _reason, merged, _issues = self._run(75, evolution={"confidence_threshold": 85})
        self.assertFalse(ok)
        self.assertEqual([], merged)

    def test_lowered_threshold_accepts_what_default_would_reject(self):
        ok, _reason, merged, _issues = self._run(60, evolution={"confidence_threshold": 55})
        self.assertTrue(ok)
        self.assertEqual(["tq_breakout"], [m["account_id"] for m in merged])


if __name__ == "__main__":
    unittest.main()
