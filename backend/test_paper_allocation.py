# -*- coding: utf-8 -*-
import unittest

import paper_allocation as allocation


class PaperAllocationTests(unittest.TestCase):
    def test_position_limits_respect_pool_and_strategy_caps(self):
        result = allocation.position_limits(
            ["tq_breakout", "main_force_top10"],
            {"tq_breakout": 0.8, "main_force_top10": 0.4},
            0.6,
            hard_pool_cap=15,
            strategy_max_positions=6,
            strategy_min_positions=1,
            protected_slot_floor=2,
            account_order={"tq_breakout": 0, "main_force_top10": 1},
            main_force_id="main_force_top10",
        )
        self.assertLessEqual(sum(result["limits"].values()), 12)
        self.assertLessEqual(result["limits"]["main_force_top10"], 3)
        self.assertGreaterEqual(result["limits"]["tq_breakout"], 1)

    def test_strategy_budget_protects_main_force_floor(self):
        result = allocation.strategy_pool_budget(
            account_id="main_force_top10",
            values={"tq_breakout": 1000.0, "main_force_top10": 0.0},
            weights={"tq_breakout": 0.5, "main_force_top10": 0.5},
            pending_by_account={"tq_breakout": 100.0, "main_force_top10": 0.0},
            pending_total=100.0,
            nav=10000.0,
            market_scales={"tq_breakout": 0.65, "main_force_top10": 0.8},
            shared_pool_max_exposure=0.82,
            strategy_pool_floor_ratio=0.55,
            main_force_id="main_force_top10",
            main_force_priority_floor_pct=0.20,
        )
        self.assertEqual(result["priority_floor_pct"], 20.0)
        self.assertTrue(result["market_scale_applied"])
        self.assertGreaterEqual(result["floor_amount"], 2000.0)

    def test_sector_rotation_budget_cannot_exceed_own_nav(self):
        result = allocation.strategy_pool_budget(
            account_id="sector_rotation",
            values={"sector_rotation": 9500.0, "other": 0.0},
            weights={"sector_rotation": 1.0, "other": 1.0},
            pending_by_account={"sector_rotation": 100.0, "other": 0.0},
            pending_total=100.0,
            nav=10000.0,
            market_scales=None,
            shared_pool_max_exposure=0.82,
            strategy_pool_floor_ratio=0.55,
            main_force_id="main_force_top10",
            main_force_priority_floor_pct=0.20,
        )
        self.assertLessEqual(result["current_total_amount"] + result["allowance_amount"], 10000.0)


if __name__ == "__main__":
    unittest.main()
