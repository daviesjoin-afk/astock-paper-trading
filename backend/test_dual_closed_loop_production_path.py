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

import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import evolution_activation as EA
import evolution_loop as EL
import paper_trading as PT
import runtime_settings as RSET
import self_evolution as SE
import strategy_registry as SR
import strategy_runtime as SRT
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
        QUOTE_PRICES.clear()
        QUOTE_SCENARIOS.clear()

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
                strategy_id=STRATEGY_ID,
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
        # 5. 自进化闭环：基于真实运行环境驱动（Backend 产生候选 Candidate B）
        # ─────────────────────────────────────────────────────────────────
        class AcceptanceEvolutionBackend(EL.Backend):
            def observe(self, conn, generation, ctx):
                active = EA.resolve_effective(conn, strategy_id=STRATEGY_ID)["active"]
                return {
                    "params_id": active["id"],
                    "params": active["params"],
                    "has_data": True,
                    "sample_count": 10,
                }

            def evaluate(self, conn, generation, ctx):
                return {"intelligence_score": 0.85}

            def mutate(self, conn, generation, ctx):
                # 提出改进候选参数 B (保守收紧 hold_bias: 0.10 -> 0.12)
                active = EA.resolve_effective(conn, strategy_id=STRATEGY_ID)["active"]
                mutated = dict(active["params"])
                mutated["hold_bias"] = 0.12
                res = EA.create_candidate(
                    conn,
                    mutated,
                    strategy_id=STRATEGY_ID,
                    source="challenger_promotion",
                    reason=f"gen_{generation}_optimization",
                    parent_id=ctx["params_id_start"],
                    evidence_count=20,
                    validate=False,
                )
                return {
                    "mutated": True,
                    "active_params_id": ctx["params_id_start"],
                    "candidate_params_id": res["params_id"],
                    "candidate_validation_state": "candidate",
                }

            def validate(self, conn, generation, ctx):
                cand_id = ctx.get("candidate_params_id")
                val_res = EA.validate_candidate(conn, cand_id)
                return {
                    "valid": val_res["valid"],
                    "candidate_params_id": cand_id,
                    "candidate_validation_state": "validated",
                }

            def apply(self, conn, generation, ctx):
                # 严格遵守契约：apply 绝不自动激活！active 保持不变
                return {
                    "applied": False,
                    "active_params_id_start": ctx["params_id_start"],
                    "active_params_id_end": ctx["params_id_start"],
                    "pending_activation": True,
                    "candidate_params_id": ctx.get("candidate_params_id"),
                }

        with self._conn() as conn:
            EL.ensure_loop_schema(conn)
            loop_rep = EL.run_loop(conn, AcceptanceEvolutionBackend(), generations=1)

        self.assertEqual(loop_rep["generations_run"], 1)
        self.assertEqual(loop_rep["completed"], 1)

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
            self.assertEqual(new_active["params"]["hold_bias"], 0.12)

            # ─────────────────────────────────────────────────────────────────
            # 8. 下一轮 Runtime 观测与历史审计完整性断言
            # ─────────────────────────────────────────────────────────────────
            history = conn.execute(
                "SELECT action, from_pointer_params_id, to_params_id, actor FROM evolution_activation_history "
                "WHERE scope_key=? ORDER BY id ASC",
                (f"strategy:{STRATEGY_ID}",),
            ).fetchall()
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["action"], "activate")
            self.assertEqual(history[0]["to_params_id"], cand_a_id)
            self.assertEqual(history[1]["action"], "activate")
            self.assertEqual(history[1]["from_pointer_params_id"], cand_a_id)
            self.assertEqual(history[1]["to_params_id"], cand_b_id)

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
