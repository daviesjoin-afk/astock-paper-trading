# -*- coding: utf-8 -*-
"""Dual closed-loop production path acceptance test.

Verifies end-to-end integration of both closed loops without fake order inserts:
Loop 1: Trading Closed Loop (signal -> OrderIntent -> allocation -> planner -> fill -> position -> T+1 exit -> attribution)
Loop 2: Evolution Closed Loop (observe -> evaluate -> mutate -> validate -> apply/activation)

Strict invariants enforced:
- candidate != validated != active != latest
- No direct INSERT INTO paper_orders / paper_fills
- Runtime is strictly isolated from unactivated candidate mutations
"""
from __future__ import annotations

import json
import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_selection as AS
import evolution_activation as EA
import evolution_loop as EL
import paper_trading as PT
import runtime_settings as RSET
import self_evolution as SE
import strategy_registry as SR
import strategy_runtime as SRT
import adaptive_engine as AE
import deepseek_advisor
import dual_ai_tuner
from unittest.mock import patch
from test_production_path_golden_replay import (
    CAPITAL,
    D0,
    D1,
    D2,
    PASS_CODES,
    RULE,
    STRATEGY_ID,
    OfflinePaperEnv,
    QUOTE_PRICES,
    QUOTE_SCENARIOS,
)


class DualClosedLoopProductionPathTests(OfflinePaperEnv, unittest.TestCase):

    def setUp(self):
        super().setUp()
        QUOTE_PRICES.clear()
        QUOTE_SCENARIOS.clear()
        self.old_ae_db = AE.DB_PATH
        self.old_ae_paper_db = AE.PAPER_DB_PATH
        AE.DB_PATH = PT.DB_PATH
        AE.PAPER_DB_PATH = PT.DB_PATH

    def tearDown(self):
        AE.DB_PATH = self.old_ae_db
        AE.PAPER_DB_PATH = self.old_ae_paper_db
        super().tearDown()

    def test_dual_closed_loop_production_path(self):
        """End-to-end dual closed loop execution with real services and zero fake fills."""
        # ─────────────────────────────────────────────────────────────────
        # 1. 初始化演化参数与指针：创建版本 A 并显式激活
        # ─────────────────────────────────────────────────────────────────
        with self._conn() as conn:
            SR.ensure_schema(conn)
            SE.ensure_schema(conn)
            EA.ensure_schema(conn)

            initial_params = {
                "max_weight_delta": 0.030,
                "max_delta_threshold": 0.005,
                "confidence_threshold": 60,
                "consensus_weight_ratio": 0.60,
                "consensus_direction_threshold": 0.005,
                "hold_bias": 0.10,
            }
            res_a = EA.create_candidate(
                conn,
                initial_params,
                strategy_id=None,
                source="manual_init",
                reason="seed version A",
                evidence_count=20,
                validate=True,
            )
            cand_a_id = res_a["params_id"]
            self.assertTrue(res_a["valid"])

            act_a = EA.activate_params_candidate(
                conn, cand_a_id, actor="test_operator", reason="initial activation of version A"
            )
            self.assertTrue(act_a["activated"])
            active_a = EA.resolve_effective(conn, strategy_id=STRATEGY_ID)["active"]
            self.assertIsNotNone(active_a)
            self.assertEqual(active_a["id"], cand_a_id)
            self.assertEqual(active_a["params"]["max_weight_delta"], 0.030)

            # ─────────────────────────────────────────────────────────────────
            # 2. 注册并激活策略定义，配置周期与资金（第一轮 Runtime 运行）
            # ─────────────────────────────────────────────────────────────────
            strategy = SR.create_user_definition(
                conn,
                STRATEGY_ID,
                "双闭环验收策略",
                dsl_ast=RULE,
                metadata={"candidate_topn": 10, "style": "trend", "hold": 5},
                actor="dual_loop_test",
            )
            self.assertEqual(strategy.origin, "user")
            SR.transition(conn, STRATEGY_ID, "validated", expected_status="draft",
                          reason="dual loop validate", actor="dual_loop_test")
            SR.transition(conn, STRATEGY_ID, "active", expected_status="validated",
                          reason="dual loop activate", actor="dual_loop_test")

        PT.init_db()
        with self._conn() as conn:
            RSET.update(conn, {"enabled_strategies": [STRATEGY_ID]}, actor="dual_loop_test")

        summary, cycle = PT.start_new_cycle(capital=CAPITAL, include_dashboard=False)
        self.assertEqual(tuple(cycle["enabled_strategies"]), (STRATEGY_ID,))

        with self._conn() as conn:
            # 编译时读取上下文，确认其关联并记录版本 A
            context = SRT.get_context(conn, STRATEGY_ID)
            self.assertIsNotNone(context.compiled_dsl)

        # ─────────────────────────────────────────────────────────────────
        # 3. 交易闭环：信号生成 -> OrderIntent -> 资金分配 -> Planner -> 成交
        # ─────────────────────────────────────────────────────────────────
        sig_result = PT.generate_signals(D0)
        self.assertEqual(sig_result.get("slot"), "close")
        user_rows = [row for row in sig_result["accounts"] if row["id"] == STRATEGY_ID]
        self.assertTrue(user_rows and user_rows[0]["created"] > 0)

        # T+1 开仓执行（开盘批次）
        opened = PT.run_slot("open", D1, force=True)
        self.assertNotEqual(opened.get("status"), "failed")

        with self._conn() as conn:
            fills = conn.execute(
                "SELECT * FROM paper_fills WHERE account_id=? AND side='buy'",
                (STRATEGY_ID,),
            ).fetchall()
            self.assertTrue(fills, "必须通过真实执行链产生买入成交，禁止 fake insert")
            buy_fill = fills[0]
            self.assertGreater(buy_fill["qty"], 0)

            order = self._one(conn, "SELECT * FROM paper_orders WHERE id=?", (buy_fill["order_id"],))
            self.assertEqual(order["status"], "filled")
            self.assertEqual(order["strategy_id"], STRATEGY_ID)

            position = self._one(
                conn, "SELECT * FROM paper_positions WHERE account_id=? AND qty>0", (STRATEGY_ID,)
            )
            self.assertIsNotNone(position, "开仓成交后必须在持仓表中真实存在")
            cost = float(position["cost"])

        # ─────────────────────────────────────────────────────────────────
        # 4. T+1 风控退出与结算证据生成
        # ─────────────────────────────────────────────────────────────────
        with self._conn() as conn:
            conn.execute("DELETE FROM paper_jobs WHERE slot='risk'")
            conn.execute("DELETE FROM paper_audit WHERE event='risk_scan_state'")

        QUOTE_SCENARIOS[D2.isoformat()] = {
            "pct": -8.5, "main_pct": -5.0, "main_net": -5_000_000.0,
            "super_net": -4_000_000.0, "vol_ratio": 1.8, "open_above": True,
        }
        for code in PASS_CODES:
            QUOTE_PRICES[(code, D2.isoformat())] = round(cost * 0.83, 2)

        PT.run_slot("risk", D2, force=True)

        with self._conn() as conn:
            sells = conn.execute(
                "SELECT * FROM paper_fills WHERE account_id=? AND side='sell'",
                (STRATEGY_ID,),
            ).fetchall()
            self.assertTrue(sells, "风控触发后必须产生真实卖出成交")
            remaining_pos = self._one(
                conn, "SELECT COALESCE(SUM(qty), 0) AS n FROM paper_positions WHERE account_id=?",
                (STRATEGY_ID,),
            )
            self.assertEqual(int(remaining_pos["n"]), 0, "持仓全清")

        # ─────────────────────────────────────────────────────────────────
        # 5. 自进化闭环：由生产自学习桥接入口驱动真实写入
        #    （dual_ai_tuner.run_dual_ai_tuning + adaptive_engine.evaluate_tuning_fn）
        # ─────────────────────────────────────────────────────────────────
        base_weights = dict(AS.BASE_WEIGHTS["one_to_two"])
        with self._conn() as conn:
            AE._init_schema(conn)
            dual_ai_tuner.ensure_schema(conn)
            dual_ai_tuner.update_api_key(conn, "mimo", api_key="test-mimo-key", enabled=True)
            dual_ai_tuner.update_api_key(conn, "deepseek", api_key="test-ds-key", enabled=True)
            strategy_params = {"adaptive_selection": {"weights": base_weights, "entry_score_delta": 0.0, "conditions": {}}}
            conn.execute(
                "UPDATE paper_accounts SET params=? WHERE id=?",
                (json.dumps(strategy_params), STRATEGY_ID),
            )

        # 针对 AI Provider Transport 进行确定性受控 stub，模拟双方达成共识的 proposal
        def _stub_call_single_ai(provider_config, system_prompt, user_prompt, max_tokens=1800):
            prov = provider_config.get("provider")
            parsed = {
                "decision": "propose",
                "confidence": 85 if prov == "mimo" else 88,
                "market_regime": "trend",
                "summary": "Dual loop trading evidence confirms strategy execution and exit",
                "proposals": [
                    {
                        "account_id": STRATEGY_ID,
                        "confidence": 85 if prov == "mimo" else 88,
                        "rationale": "Slight adjustment based on real fill attribution",
                        "weights": dict(base_weights),
                        "entry_score_delta": 0.0,
                        "conditions": {},
                    }
                ],
            }
            return parsed, 150, 80, 20

        # 基于真实成交与风控退出结果衍生归因评估分数
        realized_attribution_score = 0.42 if len(sells) > 0 else 0.10
        attribution_detail = {
            "buy_fills": len(fills),
            "sell_fills": len(sells),
            "remaining_positions": 0,
            "cost": cost,
            "derived_from": "production_trade_attribution",
        }

        with patch.object(dual_ai_tuner, "_call_single_ai", side_effect=_stub_call_single_ai):
            # 通过生产入口 run_dual_ai_tuning 产生自学习记录，由 evaluate_tuning_fn 进行事后效果评估
            for i in range(6):
                tuning_res = dual_ai_tuner.run_dual_ai_tuning(
                    connect_factory=self._conn,
                    paper_db_path=PT.DB_PATH,
                    snapshot_paths=[],
                    evidence_collector=deepseek_advisor.collect_evidence,
                    tuning_accounts_fn=deepseek_advisor._tuning_accounts,
                    profile={"profile_date": D2.isoformat(), "regime": "trend", "quality": "high"},
                    trigger="trade_cycle_attribution",
                    mode="production_acceptance",
                )
                self.assertEqual(tuning_res.get("status"), "consensus")
                run_id = tuning_res["id"]

                with self._conn() as conn:
                    tracking_row = conn.execute(
                        "SELECT id FROM evolution_tracking WHERE run_id=? ORDER BY id DESC LIMIT 1",
                        (run_id,),
                    ).fetchone()
                    self.assertIsNotNone(tracking_row, "run_dual_ai_tuning 必须在 evolution_tracking 中记录追踪行")
                    tracking_id = tracking_row[0]

                # 驱动生产自学习评估服务，事后评估调参效果
                eval_res = AE.evaluate_tuning_fn(
                    tracking_id=tracking_id,
                    eval_score=realized_attribution_score,
                    eval_detail=attribution_detail,
                )
                self.assertTrue(eval_res.get("success"))

        with self._conn() as conn:
            EL.ensure_loop_schema(conn)

            # 断言 ProductionBackend.observe 读取真实自学习管线写入的数据
            obs = EL.ProductionBackend().observe(conn, generation=1)
            self.assertTrue(obs["has_data"], "必须有真实学习数据")
            self.assertGreaterEqual(obs["sample_count"], 5, "样本数必须 >= 5")
            self.assertGreaterEqual(obs.get("evidence_count", 0), 5, "证据数必须 >= 5")
            self.assertIsNotNone(obs["samples"])
            self.assertEqual(obs["samples"].get("sample_count"), 6)
            self.assertEqual(obs["samples"].get("evaluated_count"), 6)
            self.assertAlmostEqual(obs["samples"].get("avg_eval_score"), realized_attribution_score, places=2)

            loop_rep = EL.run_loop(conn, EL.ProductionBackend(), generations=1)

        self.assertEqual(loop_rep["generations_run"], 1)
        self.assertEqual(loop_rep["completed"], 1)
        self.assertEqual(loop_rep["total_stage_errors"], 0)

        # ─────────────────────────────────────────────────────────────────
        # 6. 不变量断言：未显式激活前，Runtime 必须继续读取并使用 Active Version A
        # ─────────────────────────────────────────────────────────────────
        with self._conn() as conn:
            current_active = EA.resolve_effective(conn, strategy_id=STRATEGY_ID)["active"]
            self.assertEqual(current_active["id"], cand_a_id, "未显式激活时 active 指针绝对不能变动")
            self.assertEqual(current_active["params"]["max_weight_delta"], 0.030)
            self.assertEqual(current_active["params"]["hold_bias"], 0.10)

            # 检查候选 B 的状态
            cand_b_id = conn.execute(
                "SELECT candidate_params_id FROM evolution_loop_state WHERE generation=1"
            ).fetchone()[0]
            self.assertIsNotNone(cand_b_id)
            self.assertNotEqual(cand_b_id, cand_a_id)
            cand_b_row = conn.execute(
                "SELECT validation_state, base_params_id FROM evolution_params WHERE id=?",
                (cand_b_id,),
            ).fetchone()
            self.assertEqual(cand_b_row["validation_state"], "validated")
            self.assertEqual(cand_b_row["base_params_id"], cand_a_id)

            # ─────────────────────────────────────────────────────────────────
            # 7. 显式 Promotion / Activation 切换生效指针至 Version B
            # ─────────────────────────────────────────────────────────────────
            act_b = EA.activate_params_candidate(
                conn,
                cand_b_id,
                actor="acceptance_operator",
                reason="manual approval after validation",
            )
            self.assertTrue(act_b["activated"])

            new_active = EA.resolve_effective(conn, strategy_id=STRATEGY_ID)["active"]
            self.assertEqual(new_active["id"], cand_b_id)
            self.assertNotEqual(new_active["id"], cand_a_id)

            # ─────────────────────────────────────────────────────────────────
            # 8. 下一代/运行时读取方观测与历史审计完整性断言
            # ─────────────────────────────────────────────────────────────────
            obs_gen2 = EL.ProductionBackend().observe(conn, generation=2)
            self.assertEqual(obs_gen2["params_id"], cand_b_id, "下一代 observe 必须读取已显式激活的候选 B")
            self.assertEqual(obs_gen2["params"]["max_weight_delta"], new_active["params"]["max_weight_delta"])

            history = conn.execute(
                "SELECT action, from_pointer_params_id, to_params_id, actor, reason FROM evolution_activation_history "
                "WHERE scope_key=? ORDER BY id ASC",
                (EA.SCOPE_GLOBAL,),
            ).fetchall()
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["action"], "activate")
            self.assertEqual(history[0]["to_params_id"], cand_a_id)
            self.assertEqual(history[1]["action"], "activate")
            self.assertEqual(history[1]["from_pointer_params_id"], cand_a_id)
            self.assertEqual(history[1]["to_params_id"], cand_b_id)
            self.assertEqual(history[1]["actor"], "acceptance_operator")
            self.assertEqual(history[1]["reason"], "manual approval after validation")

            # 订单回放确定性与可追溯性
            orders = conn.execute(
                "SELECT strategy_id, strategy_version, strategy_checksum FROM paper_orders WHERE account_id=?",
                (STRATEGY_ID,),
            ).fetchall()
            self.assertTrue(orders)
            for order in orders:
                self.assertEqual(order["strategy_id"], STRATEGY_ID)
                self.assertIsNotNone(order["strategy_version"])
                self.assertIsNotNone(order["strategy_checksum"])


if __name__ == "__main__":
    unittest.main()
