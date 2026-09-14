# -*- coding: utf-8 -*-
"""Dual closed-loop production path acceptance test.

Verifies end-to-end integration of both closed loops without fake order inserts:
Loop 1: Trading Closed Loop (signal -> OrderIntent -> allocation -> planner -> fill -> position -> T+1 exit -> NAV -> reward)
Loop 2: Evolution Closed Loop (observe -> evaluate -> mutate -> validate -> apply/activation)

Strict invariants enforced:
- Separate databases: paper_db != adaptive_db
- candidate != validated != active != latest
- No direct INSERT INTO paper_orders / paper_fills
- Real reward evaluation pipeline (AE._evaluate_rewards) deriving rewards from trade NAV
- **真实因果顺序**的调参评估：只有经由显式 apply 生命周期真正生效的调参，才能消费
  其生效之后成熟的 reward：
      effective tuning -> 明确 account/strategy -> 明确 effective time
        -> 其后成熟的 reward -> 归因落库 -> evaluate_tuning_from_reward()
        -> evolution_tracking.evaluated=1
- 测试端**绝不**自算 ``math.tanh(raw_reward)`` 并注入生产接口；评分一律由生产服务给出，
  测试只做等值断言。
- Runtime is strictly isolated from unactivated candidate mutations
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from zoneinfo import ZoneInfo

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_engine as AE
import adaptive_selection as AS
import deepseek_advisor
import dual_ai_tuner
import evolution_activation as EA
import evolution_apply
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

TZ = ZoneInfo("Asia/Shanghai")

#: 保留原始 registry 读口：生产 apply 通道会在内部调用 ``strategy_registry.labels()``，
#: 而它默认解析"仓库默认库"。测试需要让它读**同一个**临时 paper 库（账户与注册表
#: 必须来自同一个库，否则账户会被判成未知）。
_ORIGINAL_LABELS = SR.labels

# 合成净值窗口：结束于 T+1 开仓日之前，与真实成交链路的净值行互不覆盖。
NAV_WINDOW_DAYS = 14
NAV_DAILY_RETURN = 0.009
BENCH_DAILY_RETURN = 0.001
# 五组"真实生效"的调参时刻，全部落在合成净值窗口内（真实证据自然 >= MIN_SAMPLES_FOR_EVOLUTION）。
EFFECTIVE_DAY_OFFSETS = (12, 10, 8, 6, 4)


class _FrozenDateTime(dt.datetime):
    """把 apply / 调参观测到的"现在"固定在某个时刻。"""

    frozen: dt.datetime = None  # type: ignore[assignment]

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003 - 固定时刻，忽略 tz
        return cls.frozen


@contextmanager
def _frozen_clock(moment: dt.datetime):
    """把 apply 与调参**事件发生的时刻**固定到 ``moment``。

    只冻结时间，不伪造任何生效证据：``dual_ai_tuning_runs.applied_ids`` 与账户运行
    参数覆盖仍然由真实生产代码写入。
    """

    class _Shim:
        datetime = _FrozenDateTime
        date = dt.date
        timedelta = dt.timedelta

    _FrozenDateTime.frozen = moment
    stamp = moment.isoformat(timespec="seconds")
    with patch.object(evolution_apply, "dt", _Shim), \
            patch.object(evolution_apply, "_now", lambda: stamp), \
            patch.object(dual_ai_tuner, "_now", lambda: stamp):
        yield stamp


class DualClosedLoopProductionPathTests(OfflinePaperEnv, unittest.TestCase):

    def setUp(self):
        super().setUp()
        QUOTE_PRICES.clear()
        QUOTE_SCENARIOS.clear()
        self.paper_db_path = PT.DB_PATH
        self.adaptive_db_path = os.path.join(self._tmp, "adaptive_learning.sqlite3")
        self.old_ae_db = AE.DB_PATH
        self.old_ae_paper_db = AE.PAPER_DB_PATH
        AE.DB_PATH = self.adaptive_db_path
        AE.PAPER_DB_PATH = self.paper_db_path

    def tearDown(self):
        AE.DB_PATH = self.old_ae_db
        AE.PAPER_DB_PATH = self.old_ae_paper_db
        super().tearDown()

    def _paper_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.paper_db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _adaptive_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.adaptive_db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _account_params(self):
        with self._paper_conn() as p_conn:
            row = self._one(
                p_conn, "SELECT params FROM paper_accounts WHERE id=?", (STRATEGY_ID,))
        return json.loads(row["params"] or "{}") or {}

    def test_dual_closed_loop_production_path(self):
        """End-to-end dual closed loop execution with real services, separate DBs, and zero fake fills."""
        self.assertNotEqual(os.path.realpath(self.paper_db_path), os.path.realpath(self.adaptive_db_path))

        # ─────────────────────────────────────────────────────────────────
        # 1. 初始化自进化库（Adaptive DB）：建立 schema 并注册初始候选版本 A + 显式激活
        # ─────────────────────────────────────────────────────────────────
        with self._adaptive_conn() as a_conn:
            AE._init_schema(a_conn)
            SE.ensure_schema(a_conn)
            EA.ensure_schema(a_conn)
            EL.ensure_loop_schema(a_conn)
            dual_ai_tuner.ensure_schema(a_conn)

            initial_params = {
                "max_weight_delta": 0.030,
                "max_delta_threshold": 0.005,
                "confidence_threshold": 60,
                "consensus_weight_ratio": 0.60,
                "consensus_direction_threshold": 0.005,
                "hold_bias": 0.10,
            }
            res_a = EA.create_candidate(
                a_conn,
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
                a_conn, cand_a_id, actor="test_operator", reason="initial activation of version A"
            )
            self.assertTrue(act_a["activated"])
            active_a = EA.resolve_effective(a_conn, strategy_id=STRATEGY_ID)["active"]
            self.assertIsNotNone(active_a)
            self.assertEqual(active_a["id"], cand_a_id)
            self.assertEqual(active_a["params"]["max_weight_delta"], 0.030)

        # ─────────────────────────────────────────────────────────────────
        # 2. 初始化交易库（Paper DB）：注册并激活策略定义，配置周期与资金
        # ─────────────────────────────────────────────────────────────────
        with self._paper_conn() as p_conn:
            SR.ensure_schema(p_conn)
            strategy = SR.create_user_definition(
                p_conn,
                STRATEGY_ID,
                "双闭环验收策略",
                dsl_ast=RULE,
                metadata={"candidate_topn": 10, "style": "trend", "hold": 5},
                actor="dual_loop_test",
            )
            self.assertEqual(strategy.origin, "user")
            SR.transition(p_conn, STRATEGY_ID, "validated", expected_status="draft",
                          reason="dual loop validate", actor="dual_loop_test")
            SR.transition(p_conn, STRATEGY_ID, "active", expected_status="validated",
                          reason="dual loop activate", actor="dual_loop_test")

        PT.init_db()
        with self._paper_conn() as p_conn:
            RSET.update(p_conn, {"enabled_strategies": [STRATEGY_ID]}, actor="dual_loop_test")

        summary, cycle = PT.start_new_cycle(capital=CAPITAL, include_dashboard=False)
        self.assertEqual(tuple(cycle["enabled_strategies"]), (STRATEGY_ID,))

        with self._paper_conn() as p_conn:
            context = SRT.get_context(p_conn, STRATEGY_ID)
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

        with self._paper_conn() as p_conn:
            fills = p_conn.execute(
                "SELECT * FROM paper_fills WHERE account_id=? AND side='buy'",
                (STRATEGY_ID,),
            ).fetchall()
            self.assertTrue(fills, "必须通过真实执行链产生买入成交，禁止 fake insert")
            buy_fill = fills[0]
            self.assertGreater(buy_fill["qty"], 0)

            order = self._one(p_conn, "SELECT * FROM paper_orders WHERE id=?", (buy_fill["order_id"],))
            self.assertEqual(order["status"], "filled")
            self.assertEqual(order["strategy_id"], STRATEGY_ID)

            position = self._one(
                p_conn, "SELECT * FROM paper_positions WHERE account_id=? AND qty>0", (STRATEGY_ID,)
            )
            self.assertIsNotNone(position, "开仓成交后必须在持仓表中真实存在")
            cost = float(position["cost"])

        # ─────────────────────────────────────────────────────────────────
        # 4. T+1 风控退出与平仓成交
        # ─────────────────────────────────────────────────────────────────
        with self._paper_conn() as p_conn:
            p_conn.execute("DELETE FROM paper_jobs WHERE slot='risk'")
            p_conn.execute("DELETE FROM paper_audit WHERE event='risk_scan_state'")

        QUOTE_SCENARIOS[D2.isoformat()] = {
            "pct": -8.5, "main_pct": -5.0, "main_net": -5_000_000.0,
            "super_net": -4_000_000.0, "vol_ratio": 1.8, "open_above": True,
        }
        for code in PASS_CODES:
            QUOTE_PRICES[(code, D2.isoformat())] = round(cost * 0.83, 2)

        PT.run_slot("risk", D2, force=True)

        with self._paper_conn() as p_conn:
            sells = p_conn.execute(
                "SELECT * FROM paper_fills WHERE account_id=? AND side='sell'",
                (STRATEGY_ID,),
            ).fetchall()
            self.assertTrue(sells, "风控触发后必须产生真实卖出成交")
            remaining_pos = self._one(
                p_conn, "SELECT COALESCE(SUM(qty), 0) AS n FROM paper_positions WHERE account_id=?",
                (STRATEGY_ID,),
            )
            self.assertEqual(int(remaining_pos["n"]), 0, "持仓全清")

        # ─────────────────────────────────────────────────────────────────
        # 5. 生产收益评估管线：写入真实交易生命周期净值序列，驱动 AE._evaluate_rewards
        #    —— 此时产生的全部 reward 都早于后面的调参生效时刻，是"旧 reward"。
        # ─────────────────────────────────────────────────────────────────
        nav_end = D1 - dt.timedelta(days=1)
        nav_dates = [nav_end - dt.timedelta(days=NAV_WINDOW_DAYS - 1 - i)
                     for i in range(NAV_WINDOW_DAYS)]
        # 账户声明的因子基线：deepseek_advisor._tuning_accounts 只把这个 overlay
        # 当作"当前已有因子"暴露给模型。注意这里**没有** adaptive_selection_meta，
        # 所以它不是生效覆盖 —— 调参前 runtime 仍按策略基准运行。
        base_weights = dict(AS.BASE_WEIGHTS["one_to_two"])
        with self._paper_conn() as p_conn:
            p_conn.execute(
                "UPDATE paper_accounts SET params=? WHERE id=?",
                (json.dumps({"adaptive_selection": {
                    "weights": base_weights, "entry_score_delta": 0.0, "conditions": {}}}),
                 STRATEGY_ID),
            )
            for index, day in enumerate(nav_dates):
                nav = round((1.0 + NAV_DAILY_RETURN) ** index, 4)
                bench = round((1.0 + BENCH_DAILY_RETURN) ** index, 4)
                p_conn.execute(
                    """INSERT OR REPLACE INTO paper_nav(account_id, nav_date, cash, market_value, nav, benchmark, created_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?)""",
                    (STRATEGY_ID, day.isoformat(), round(nav * 200_000, 2),
                     round(nav * 800_000, 2), nav, bench, f"{day.isoformat()} 15:05:00"),
                )
            p_conn.commit()

        # 在 Adaptive DB 注册市场状态，使奖励归因带上确定性 regime
        with self._adaptive_conn() as a_conn:
            a_conn.execute(
                """INSERT OR REPLACE INTO adaptive_market_profiles(
                       profile_date, observed_at, source_at, regime, quality, valid_rows, features, drivers, created_at, updated_at
                   ) VALUES (
                       ?, '2026-09-01 15:00:00', '2026-09-01 15:00:00', 'trend', 'high', 5000, '{}', '{}', '2026-09-01 15:00:00', '2026-09-01 15:00:00'
                   )""",
                (nav_dates[0].isoformat(),),
            )
            a_conn.commit()

            # 调用生产收益评估管线：跨多周期窗口计算真实超额、回撤与周转率
            new_rewards = AE._evaluate_rewards(a_conn)
            self.assertGreaterEqual(new_rewards, 5, "AE._evaluate_rewards 必须跨多个周期产生至少 5 条奖励样本")
            reward_rows = a_conn.execute(
                "SELECT * FROM adaptive_rewards WHERE account_id=? ORDER BY id ASC",
                (STRATEGY_ID,),
            ).fetchall()
            self.assertGreaterEqual(len(reward_rows), 5)

            # 配置 AI Provider Key（双AI均配置且启用 —— 缺一不可，见 fail-closed 约束）
            dual_ai_tuner.update_api_key(a_conn, "mimo", api_key="test-mimo-key", enabled=True)
            dual_ai_tuner.update_api_key(a_conn, "deepseek", api_key="test-ds-key", enabled=True)

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

        def _run_one_tuning(effective_day):
            """真实时间顺序：创建调参 -> 显式 apply -> 生效证据 -> 归因 -> 评估。"""
            moment = dt.datetime(effective_day.year, effective_day.month,
                                 effective_day.day, 15, 30, tzinfo=TZ)
            with _frozen_clock(moment):
                tuning = dual_ai_tuner.run_dual_ai_tuning(
                    connect_factory=self._adaptive_conn,
                    paper_db_path=self.paper_db_path,
                    snapshot_paths=[],
                    evidence_collector=deepseek_advisor.collect_evidence,
                    tuning_accounts_fn=deepseek_advisor._tuning_accounts,
                    profile={"profile_date": effective_day.isoformat(),
                             "regime": "trend", "quality": "high"},
                    trigger="trade_cycle_attribution",
                    mode="production_acceptance",
                )
                self.assertEqual(tuning.get("status"), "consensus")
                run_id = tuning["id"]

                with self._adaptive_conn() as a_conn:
                    tracking_id = a_conn.execute(
                        "SELECT id FROM evolution_tracking WHERE run_id=? ORDER BY id DESC LIMIT 1",
                        (run_id,),
                    ).fetchone()[0]
                    self.assertIsNotNone(tracking_id)

                # 真实生效证据：显式 apply 生命周期（不是测试手写的标记位）
                apply_res = evolution_apply.apply_tuner_proposals(
                    self._adaptive_conn, self.paper_db_path, run_id,
                    approved_by="acceptance_operator", confirmed=True,
                    base_weights_fn=lambda account_id: dict(base_weights),
                )
                self.assertTrue(apply_res["applied"])
                self.assertEqual(apply_res["accounts"], [STRATEGY_ID])
                self.assertEqual(apply_res["effective_date"], effective_day.isoformat())

                meta = self._account_params()["adaptive_selection_meta"]
                self.assertEqual(meta["tier"], "llm_consensus")
                self.assertEqual(meta["status"], "active")
                self.assertEqual(meta["run_id"], run_id)
                effective_from = dt.date.fromisoformat(str(meta["effective_date"])[:10])
                self.assertEqual(effective_from, effective_day)

            return run_id, tracking_id, effective_from

        # ─────────────────────────────────────────────────────────────────
        # 6. 自进化闭环：真实因果顺序
        #    effective tuning -> 明确 account -> 明确 effective time
        #      -> 其后成熟的 reward -> 归因落库 -> evaluate_tuning_from_reward()
        #      -> evolution_tracking.evaluated=1
        #    评分全部由生产服务从 adaptive_rewards.raw_reward 计算，测试端零注入。
        # ─────────────────────────────────────────────────────────────────
        with patch.object(dual_ai_tuner, "_call_single_ai", side_effect=_stub_call_single_ai), \
                patch.object(SR, "labels",
                             lambda **kwargs: _ORIGINAL_LABELS(db_path=self.paper_db_path)):
            stale_reward = min(reward_rows, key=lambda r: (r["start_date"], r["id"]))
            attribution_evidence = []

            for index, offset in enumerate(EFFECTIVE_DAY_OFFSETS):
                effective_day = nav_end - dt.timedelta(days=offset)
                self.assertGreaterEqual(effective_day, nav_dates[0])

                run_id, tracking_id, effective_from = _run_one_tuning(effective_day)

                if index == 0:
                    # ── 负向验证（§1）：旧 reward + 新 tracking => reject ──
                    # 生效之前的 reward 不得评估新调参；即使强行建立归因，时间窗口
                    # 也要被独立复核（归因不是免检通行证）。
                    self.assertLess(stale_reward["start_date"], effective_from.isoformat())
                    with self._adaptive_conn() as a_conn:
                        with self.assertRaises(KeyError):
                            AE.evaluate_tuning_from_reward(
                                tracking_id=tracking_id,
                                reward_id=stale_reward["id"],
                                conn=a_conn,
                            )
                        SE.record_reward_attribution(
                            a_conn, tracking_id, stale_reward["id"], STRATEGY_ID,
                            effective_from.isoformat())
                        with self.assertRaises(ValueError) as ctx:
                            AE.evaluate_tuning_from_reward(
                                tracking_id=tracking_id,
                                reward_id=stale_reward["id"],
                                conn=a_conn,
                            )
                        self.assertIn("生效", str(ctx.exception))
                        # 清掉这次刻意的无效探针，避免污染"首次决定"的归因
                        a_conn.execute(
                            "DELETE FROM evolution_reward_attribution WHERE tracking_id=? AND reward_id=?",
                            (tracking_id, stale_reward["id"]))
                        a_conn.commit()
                    with self._adaptive_conn() as a_conn:
                        self.assertEqual(
                            a_conn.execute(
                                "SELECT evaluated FROM evolution_tracking WHERE id=?",
                                (tracking_id,)).fetchone()[0], 0)

                # 生产归因匹配器：证明匹配并驱动评估
                with self._adaptive_conn() as a_conn:
                    report = AE.reconcile_tuning_reward_attribution(a_conn)
                self.assertEqual(report["status"], "ok", report)
                self.assertEqual(report["failed"], [], report)
                self.assertEqual(len(report["evaluated"]), 1, report)
                outcome = report["evaluated"][0]
                self.assertEqual(outcome["tracking_id"], tracking_id)
                self.assertEqual(outcome["account_id"], STRATEGY_ID)
                self.assertEqual(outcome["effective_from"], effective_from.isoformat())
                self.assertTrue(outcome["attribution_created"])

                with self._adaptive_conn() as a_conn:
                    reward = a_conn.execute(
                        "SELECT * FROM adaptive_rewards WHERE id=?",
                        (outcome["reward_id"],)).fetchone()
                    tracking = a_conn.execute(
                        "SELECT evaluated, eval_score, eval_detail FROM evolution_tracking WHERE id=?",
                        (tracking_id,)).fetchone()
                    links = SE.list_reward_attributions(a_conn, tracking_id)

                # reward 必须整体落在生效之后（旧 reward 被排除）
                self.assertGreaterEqual(reward["start_date"], effective_from.isoformat())
                self.assertGreater(reward["end_date"], effective_from.isoformat())
                self.assertNotEqual(reward["id"], stale_reward["id"])

                # 评分必须由生产服务给出，并严格等于 raw_reward 的 tanh 映射
                self.assertEqual(tracking["evaluated"], 1)
                self.assertAlmostEqual(
                    tracking["eval_score"], math.tanh(reward["raw_reward"]), places=9)
                detail = json.loads(tracking["eval_detail"])
                self.assertEqual(detail["source"], "adaptive_rewards")
                self.assertEqual(detail["score_mapping_version"], AE.EVOLUTION_REWARD_SCORE_VERSION)
                self.assertEqual(detail["raw_reward"], reward["raw_reward"])
                self.assertEqual(detail["attribution"]["account_id"], STRATEGY_ID)
                self.assertEqual(detail["attribution"]["effective_from"], effective_from.isoformat())
                self.assertEqual(detail["attribution"]["linkage_source"], SE.ATTRIBUTION_LINKAGE_SOURCE)

                # 归因契约落库：一条 tracking 恰好一条归因
                self.assertEqual(len(links), 1)
                self.assertEqual(links[0]["reward_id"], reward["id"])
                self.assertEqual(links[0]["account_id"], STRATEGY_ID)
                attribution_evidence.append((tracking_id, reward["id"]))

                # 幂等：重复周期不得新增归因、不得改写首次决定
                with self._adaptive_conn() as a_conn:
                    repeat = AE.reconcile_tuning_reward_attribution(a_conn)
                    self.assertEqual(repeat["evaluated"], [], repeat)
                    self.assertEqual(repeat["failed"], [], repeat)
                    self.assertEqual(len(SE.list_reward_attributions(a_conn, tracking_id)), 1)
                    self.assertEqual(
                        a_conn.execute(
                            "SELECT eval_score FROM evolution_tracking WHERE id=?",
                            (tracking_id,)).fetchone()[0], tracking["eval_score"])

                # 释放覆盖，为下一次调参让出生效通道（rollback 后该 tracking 不再生效，
                # 但它已经完成的评估保持不变 —— 首次决定不可改写）。
                evolution_apply.rollback_tuner_overlay(
                    self._adaptive_conn, self.paper_db_path, STRATEGY_ID,
                    reason="dual loop next tuning", confirmed=True)

            self.assertEqual(len(attribution_evidence), len(EFFECTIVE_DAY_OFFSETS))
            self.assertEqual(
                len({tracking_id for tracking_id, _ in attribution_evidence}),
                len(EFFECTIVE_DAY_OFFSETS), "每次调参必须各自独立评估")

        # ─────────────────────────────────────────────────────────────────
        # 7. 演化引擎闭环（Generation 1）：observe -> evaluate -> mutate -> validate -> apply
        # ─────────────────────────────────────────────────────────────────
        with self._adaptive_conn() as a_conn:
            obs = EL.ProductionBackend().observe(a_conn, generation=1)
            self.assertTrue(obs["has_data"], "必须有真实学习数据")
            self.assertGreaterEqual(obs["sample_count"], 5, "样本数必须 >= 5")
            self.assertGreaterEqual(obs.get("evidence_count", 0), 5, "证据数必须 >= 5")
            self.assertIsNotNone(obs["samples"])
            self.assertGreaterEqual(obs["samples"].get("sample_count", 0), 5)
            self.assertGreaterEqual(obs["samples"].get("evaluated_count", 0), 5,
                                    "评估样本必须来自真实生效调参，不得人工凑数")

            eval_res = EL.ProductionBackend().evaluate(a_conn, generation=1)
            self.assertIn("intelligence_score", eval_res)
            self.assertGreater(eval_res["intelligence_score"], 0)

            loop_rep = EL.run_loop(a_conn, EL.ProductionBackend(), generations=1)
            self.assertEqual(loop_rep["generations_run"], 1)
            self.assertEqual(loop_rep["completed"], 1)
            self.assertEqual(loop_rep["total_stage_errors"], 0)

            # 不变量断言：未显式激活前，Runtime 必须继续读取并使用 Active Version A
            current_active = EA.resolve_effective(a_conn, strategy_id=STRATEGY_ID)["active"]
            self.assertEqual(current_active["id"], cand_a_id, "未显式激活时 active 指针绝对不能变动")
            self.assertEqual(current_active["params"]["max_weight_delta"], 0.030)
            self.assertEqual(current_active["params"]["hold_bias"], 0.10)

            # 候选 B 处于 validated 状态，且基于 A 派生
            cand_b_id = a_conn.execute(
                "SELECT candidate_params_id FROM evolution_loop_state WHERE generation=1"
            ).fetchone()[0]
            self.assertIsNotNone(cand_b_id)
            self.assertNotEqual(cand_b_id, cand_a_id)
            cand_b_row = a_conn.execute(
                "SELECT validation_state, base_params_id FROM evolution_params WHERE id=?",
                (cand_b_id,),
            ).fetchone()
            self.assertEqual(cand_b_row["validation_state"], "validated")
            self.assertEqual(cand_b_row["base_params_id"], cand_a_id)

            # 显式 Promotion / Activation 切换生效指针至 Version B
            act_b = EA.activate_params_candidate(
                a_conn,
                cand_b_id,
                actor="acceptance_operator",
                reason="manual approval after validation",
            )
            self.assertTrue(act_b["activated"])

            new_active = EA.resolve_effective(a_conn, strategy_id=STRATEGY_ID)["active"]
            self.assertEqual(new_active["id"], cand_b_id)
            self.assertNotEqual(new_active["id"], cand_a_id)

            # 下一代/运行时读取方观测：Generation 2 必须读取已激活的候选 B
            obs_gen2 = EL.ProductionBackend().observe(a_conn, generation=2)
            self.assertEqual(obs_gen2["params_id"], cand_b_id, "下一代 observe 必须读取已显式激活的候选 B")
            self.assertEqual(obs_gen2["params"]["max_weight_delta"], new_active["params"]["max_weight_delta"])

            # 激活历史审计留痕
            history = a_conn.execute(
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

        # ─────────────────────────────────────────────────────────────────
        # 8. 跨库物理隔离与账本完整性校验
        # ─────────────────────────────────────────────────────────────────
        with self._paper_conn() as p_conn:
            p_orders = p_conn.execute("SELECT count(*) FROM paper_orders WHERE account_id=?", (STRATEGY_ID,)).fetchone()[0]
            p_fills = p_conn.execute("SELECT count(*) FROM paper_fills WHERE account_id=?", (STRATEGY_ID,)).fetchone()[0]
            p_nav = p_conn.execute("SELECT count(*) FROM paper_nav WHERE account_id=?", (STRATEGY_ID,)).fetchone()[0]
            self.assertGreater(p_orders, 0, "Paper DB 必须包含真实订单")
            self.assertGreater(p_fills, 0, "Paper DB 必须包含真实成交")
            self.assertGreater(p_nav, 0, "Paper DB 必须包含净值历史")

            orders = p_conn.execute(
                "SELECT strategy_id, strategy_version, strategy_checksum FROM paper_orders WHERE account_id=?",
                (STRATEGY_ID,),
            ).fetchall()
            self.assertTrue(orders)
            for order in orders:
                self.assertEqual(order["strategy_id"], STRATEGY_ID)
                self.assertIsNotNone(order["strategy_version"])
                self.assertIsNotNone(order["strategy_checksum"])

        with self._adaptive_conn() as a_conn:
            a_rewards = a_conn.execute("SELECT count(*) FROM adaptive_rewards WHERE account_id=?", (STRATEGY_ID,)).fetchone()[0]
            a_runs = a_conn.execute("SELECT count(*) FROM dual_ai_tuning_runs").fetchone()[0]
            a_tracking = a_conn.execute("SELECT count(*) FROM evolution_tracking").fetchone()[0]
            a_evaluated = a_conn.execute("SELECT count(*) FROM evolution_tracking WHERE evaluated=1").fetchone()[0]
            a_params = a_conn.execute("SELECT count(*) FROM evolution_params").fetchone()[0]
            a_loop = a_conn.execute("SELECT count(*) FROM evolution_loop_state").fetchone()[0]
            a_attributions = a_conn.execute("SELECT count(*) FROM evolution_reward_attribution").fetchone()[0]
            self.assertGreaterEqual(a_rewards, 5, "Adaptive DB 必须包含 >= 5 条奖励样本")
            self.assertGreaterEqual(a_runs, len(EFFECTIVE_DAY_OFFSETS), "Adaptive DB 必须包含调参运行记录")
            self.assertGreaterEqual(a_tracking, len(EFFECTIVE_DAY_OFFSETS), "Adaptive DB 必须包含自进化追踪记录")
            self.assertEqual(a_evaluated, len(EFFECTIVE_DAY_OFFSETS),
                             "只有真实生效的调参才能被评估")
            self.assertEqual(a_attributions, len(EFFECTIVE_DAY_OFFSETS),
                             "每个真实生效的调参恰好落一条 reward 归因契约")
            self.assertGreaterEqual(a_params, 2, "Adaptive DB 必须包含候选 A 与候选 B 参数版本")
            self.assertEqual(a_loop, 1, "Adaptive DB 必须包含 1 代循环执行记录")


class EvolutionRewardScoreServiceTests(unittest.TestCase):
    """Unit tests for production helper _reward_to_evolution_score and evaluate_tuning_from_reward."""

    def test_reward_to_evolution_score_positive(self):
        score = AE._reward_to_evolution_score(0.75)
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)
        self.assertAlmostEqual(score, math.tanh(0.75), places=7)

    def test_reward_to_evolution_score_negative(self):
        score = AE._reward_to_evolution_score(-0.45)
        self.assertLess(score, 0.0)
        self.assertGreater(score, -1.0)
        self.assertAlmostEqual(score, math.tanh(-0.45), places=7)

    def test_reward_to_evolution_score_large_magnitude_clamps(self):
        score_pos = AE._reward_to_evolution_score(15.0)
        self.assertGreater(score_pos, 0.9999)
        self.assertLessEqual(score_pos, 1.0)

        score_neg = AE._reward_to_evolution_score(-25.0)
        self.assertLess(score_neg, -0.9999)
        self.assertGreaterEqual(score_neg, -1.0)

    def test_reward_score_is_monotonic_across_old_boundary(self):
        values = [0.0, 0.5, 0.99, 1.0, 1.01, 2.0, 10.0]
        scores = [AE._reward_to_evolution_score(x) for x in values]
        for left, right in zip(scores, scores[1:], strict=False):
            self.assertLess(left, right)

        neg_values = [-10.0, -2.0, -1.01, -1.0, -0.99, -0.5, 0.0]
        neg_scores = [AE._reward_to_evolution_score(x) for x in neg_values]
        for left, right in zip(neg_scores, neg_scores[1:], strict=False):
            self.assertLess(left, right)

        self.assertEqual(AE._reward_to_evolution_score(0.0), 0.0)
        self.assertGreater(AE._reward_to_evolution_score(0.5), 0.0)
        self.assertLess(AE._reward_to_evolution_score(-0.5), 0.0)
        self.assertAlmostEqual(
            AE._reward_to_evolution_score(-0.75),
            -AE._reward_to_evolution_score(0.75),
            places=9,
        )
        for v in values + neg_values:
            self.assertLess(abs(AE._reward_to_evolution_score(v)), 1.0)

        # Critical regression: no inversion or drop at +-1.0 boundary
        s_099 = AE._reward_to_evolution_score(0.99)
        s_100 = AE._reward_to_evolution_score(1.00)
        s_101 = AE._reward_to_evolution_score(1.01)
        self.assertGreater(s_101, s_100)
        self.assertGreater(s_100, s_099)

        s_neg_099 = AE._reward_to_evolution_score(-0.99)
        s_neg_100 = AE._reward_to_evolution_score(-1.00)
        s_neg_101 = AE._reward_to_evolution_score(-1.01)
        self.assertGreater(s_neg_099, s_neg_100)
        self.assertGreater(s_neg_100, s_neg_101)

    def test_reward_to_evolution_score_non_finite_fails(self):
        for invalid in (float("nan"), float("inf"), float("-inf"), None, "not-a-number"):
            with self.assertRaises(ValueError):
                AE._reward_to_evolution_score(invalid)

    def test_evaluate_tuning_from_reward_missing_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "adaptive_test.sqlite3")
            conn = sqlite3.connect(test_db)
            conn.row_factory = sqlite3.Row
            try:
                AE._init_schema(conn)
                SE.ensure_schema(conn)
                # 1. Missing tracking_id
                with self.assertRaises(KeyError):
                    AE.evaluate_tuning_from_reward(tracking_id=99999, reward_id=1, conn=conn)

                # Insert dummy tracking run
                conn.execute(
                    """INSERT INTO evolution_tracking(run_id, trigger, mode, status, created_at)
                       VALUES(1, 'test', 'test', 'consensus', '2026-09-14')"""
                )
                conn.commit()
                tracking_id = conn.execute("SELECT id FROM evolution_tracking WHERE run_id=1").fetchone()[0]

                # 2. Missing reward_id
                with self.assertRaises(KeyError):
                    AE.evaluate_tuning_from_reward(tracking_id=tracking_id, reward_id=88888, conn=conn)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
