# -*- coding: utf-8 -*-
"""自定义策略确定性全链路 Golden Replay（PR-20）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import demo_strategy_replay as DSR


class GoldenReplayTests(unittest.TestCase):
    def test_full_chain_runs_offline_and_hits_every_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest = DSR.run_custom_strategy_replay(tmp)
        stages = [item["stage"] for item in digest["stages"]]
        self.assertEqual(
            ["risk_compile", "signal", "allocation", "sizing", "fill", "stop",
             "evolution_proposal"],
            stages,
        )

    def test_golden_values_are_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest = DSR.run_custom_strategy_replay(tmp)
        stages = {item["stage"]: item for item in digest["stages"]}
        # 创建：指纹命中 trend，执行画像为限价。
        self.assertEqual("trend", stages["risk_compile"]["archetype"])
        self.assertEqual("trend", stages["risk_compile"]["risk_profile"])
        self.assertEqual("limit", stages["risk_compile"]["order_type"])
        self.assertIn("max_weight_delta", stages["risk_compile"]["tunable"])
        self.assertIn("hold_bias", stages["risk_compile"]["tunable"])
        # 席位分配在 15 上限内。
        self.assertEqual(3, stages["allocation"]["position_limit"])
        self.assertLessEqual(stages["allocation"]["pool_limit"], 15)
        # 成交：整手、限价、金额一致。
        self.assertEqual(11900, stages["sizing"]["qty"])
        self.assertEqual(11900, stages["fill"]["qty"])
        self.assertEqual(20.10, stages["fill"]["fill_price"])
        self.assertEqual(round(11900 * 20.10, 2), stages["fill"]["amount"])
        # 止损：-5% 触发、亏损出场。
        self.assertEqual(18.95, stages["stop"]["trigger_price"])
        self.assertEqual(18.90, stages["stop"]["fill_price"])
        self.assertEqual(round(11900 * (18.90 - 20.10), 2),
                         stages["stop"]["realized_pnl"])
        self.assertLess(stages["stop"]["realized_pnl"], 0)
        # 进化提案：已调整且带画像。
        self.assertTrue(stages["evolution_proposal"]["adjusted"])
        self.assertEqual(["max_weight_delta"],
                         stages["evolution_proposal"]["changed_keys"])
        # 自定义策略未被画像收录 → 进化提案走保守默认画像（fail-closed）。
        self.assertEqual("保守默认",
                         stages["evolution_proposal"]["profile"])

    def test_two_independent_runs_produce_identical_digests(self):
        """Golden Replay 核心：两套独立临时账本，digest 逐字节一致。"""
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            run_a = DSR.run_custom_strategy_replay(first)
            run_b = DSR.run_custom_strategy_replay(second)
        self.assertEqual(run_a["digest"], run_b["digest"])
        self.assertEqual(
            json.dumps(run_a["stages"], sort_keys=True),
            json.dumps(run_b["stages"], sort_keys=True),
        )

    def test_replay_is_really_offline(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "demo_strategy_replay.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        # 不允许 import 任何网络抓取模块。
        for forbidden in ("data_fetcher", "marketdata_transport", "requests",
                          "urllib", "socket"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from {forbidden}", source)


if __name__ == "__main__":
    unittest.main()
