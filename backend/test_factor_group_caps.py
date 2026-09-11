# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

try:
    import factor_calibration as FC
    import strategies as S
except ImportError:
    from . import factor_calibration as FC
    from . import strategies as S


class FactorGroupCapTests(unittest.TestCase):
    def test_all_default_paper_weights_respect_group_cap(self):
        for strategy_id, weights in S.PAPER_WEIGHTS.items():
            with self.subTest(strategy=strategy_id):
                self.assertTrue(FC.group_caps_ok(weights))
                self.assertTrue(all(
                    total <= FC.MAX_FACTOR_GROUP_WEIGHT + 1e-12
                    for total in FC.weight_group_totals(weights).values()
                ))

    def test_correlated_momentum_override_is_rejected(self):
        weights = {
            "mom_short": 0.35,
            "mom": 0.30,
            "flow": 0.15,
            "volsurge": 0.10,
            "quality": 0.10,
        }
        self.assertFalse(FC.group_caps_ok(weights))
        self.assertAlmostEqual(FC.weight_group_totals(weights)["momentum"], 0.65)

    def test_runtime_falls_back_to_defaults_on_group_cap_violation(self):
        table = S.pd.DataFrame({
            "mom_short": [1.0],
            "mom": [1.0],
            "flow": [1.0],
            "volsurge": [1.0],
            "quality": [1.0],
            "mom_short_evidence_quality": [1.0],
            "mom_evidence_quality": [1.0],
            "flow_evidence_quality": [1.0],
            "volsurge_evidence_quality": [1.0],
            "quality_evidence_quality": [1.0],
            "name": ["样本"], "industry": ["A"], "price": [10.0], "pct": [2.0],
            "amount": [1e8], "turnover": [3.0], "mom5_raw": [0.03],
            "mom20_raw": [0.08], "mom60_raw": [0.12], "vol_surge_raw": [1.3],
            "rsi14_raw": [55.0], "super_net_raw": [1e6], "main_pct": [3.0],
            "star_sector_bonus": [0.0], "sector_heat_score": [0.0],
            "sector_early_rotation_score": [0.0], "sector_early_rotation": [False],
            "ma20": [9.5], "ma60": [9.0], "above_boll_mid": [True],
            "boll_mid_breakout": [False],
        }, index=["000001"])
        override = {
            "mom_short": 0.35,
            "mom": 0.30,
            "flow": 0.15,
            "volsurge": 0.10,
            "quality": 0.10,
        }
        result = S.run_strategy(
            "trend_continuation",
            table,
            topn=1,
            gate={"light": "green"},
            weight_overrides=override,
        )
        self.assertTrue(result["factor_calibration"]["weight_override_rejected"])
        self.assertEqual(result["weights_used"], S.PAPER_WEIGHTS["trend_continuation"])
        self.assertTrue(FC.group_caps_ok(result["weights_used"]))


if __name__ == "__main__":
    unittest.main()
