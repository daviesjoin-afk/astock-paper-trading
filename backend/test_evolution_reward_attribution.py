# -*- coding: utf-8 -*-
"""调参 → 奖励 归因契约的聚焦测试（真实因果顺序，fail-closed）。

契约要求的生产顺序：

    effective tuning → 明确 account/strategy → 明确 effective time
      → 其后成熟的 reward → 归因落库 → evaluate_tuning_from_reward()
      → evolution_tracking.evaluated=1

本文件覆盖 §11 要求的 A–H 八组验证：

  A 生效之前的 reward                        → 拒绝
  B 跨越新旧参数的 reward 窗口               → 拒绝 / waiting_evidence
  C 账户不匹配                               → 拒绝（禁止跨账户评估）
  D 未真正生效（影子 / 仅 consensus）        → 拒绝
  E 缺少归因记录                             → 拒绝
  F 正向路径                                 → evaluated=1 且 eval_score == tanh(raw_reward)
  G 幂等                                     → 一条归因、一次有效评估；冲突 fail closed
  H 无证据                                   → waiting_evidence，且不算失败

两条硬约束：

* 生效证据只能来自项目**真实**的显式 apply 生命周期
  （``evolution_apply.apply_tuner_proposals`` → ``applied_ids`` + 账户运行参数
  覆盖），测试不手工写 "effective" 标记位；
* 评分由生产服务计算。测试端只用 ``math.tanh`` 做**期望值断言**，
  绝不把自算分数注入生产接口。
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
import adaptive_selection as ASEL
import evolution_apply
import self_evolution as SE

TZ = ZoneInfo("Asia/Shanghai")

#: 真实内建策略账户（strategy_registry 的内建定义），模型族 one_to_two。
ACCOUNT_ID = "tq_breakout"
SOURCE_STRATEGY = "one_to_two"


class _FrozenDateTime(dt.datetime):
    """把 evolution_apply 观测到的"现在"固定在某个时刻。"""

    frozen: dt.datetime = None  # type: ignore[assignment]

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003 - 固定时刻，忽略 tz
        return cls.frozen


@contextmanager
def _frozen_apply_clock(moment: dt.datetime):
    """把 apply **事件发生的时刻**固定到 ``moment``。

    只冻结时间，不伪造任何生效证据：``applied_ids`` 与账户运行参数覆盖仍然由
    ``evolution_apply.apply_tuner_proposals`` 真实写入。
    """

    class _Shim:
        datetime = _FrozenDateTime
        date = dt.date
        timedelta = dt.timedelta

    _FrozenDateTime.frozen = moment
    stamp = moment.isoformat(timespec="seconds")
    with patch.object(evolution_apply, "dt", _Shim), \
            patch.object(evolution_apply, "_now", lambda: stamp):
        yield stamp


def _base_weights():
    import strategies as S
    return dict(S.PAPER_WEIGHTS[SOURCE_STRATEGY])


class RewardAttributionTestBase(unittest.TestCase):
    """离线夹具：真实 adaptive schema + 真实 paper 账户 + 真实 apply 通道。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adaptive_path = os.path.join(self.tmp.name, "adaptive.sqlite3")
        self.paper_path = os.path.join(self.tmp.name, "paper.sqlite3")

        self._old_paper_db = AE.PAPER_DB_PATH
        self._old_adaptive_db = AE.DB_PATH
        AE.PAPER_DB_PATH = self.paper_path
        AE.DB_PATH = self.adaptive_path
        self.addCleanup(self._restore_paths)

        self._init_adaptive()
        self._init_paper()

        # apply 事件固定发生在 10 天前，让"生效之后"的 reward 窗口可以是
        # 真实的多日窗口，而不是退化成同一天。
        self.applied_day = dt.date.today() - dt.timedelta(days=10)
        self.applied_moment = dt.datetime(
            self.applied_day.year, self.applied_day.month, self.applied_day.day,
            15, 30, tzinfo=TZ)

    def _restore_paths(self):
        AE.PAPER_DB_PATH = self._old_paper_db
        AE.DB_PATH = self._old_adaptive_db

    # -- schema ------------------------------------------------------------
    def _adaptive(self):
        conn = sqlite3.connect(self.adaptive_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _paper(self):
        conn = sqlite3.connect(self.paper_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _adaptive_ctx(self):
        conn = self._adaptive()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _paper_ctx(self):
        conn = self._paper()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_adaptive(self):
        with self._adaptive_ctx() as conn:
            AE._init_schema(conn)
            SE.ensure_schema(conn)
            # 最小可用的调参运行表（真实表由 dual_ai_tuner 维护；这里只需要
            # 归因链路真正消费的四列）。
            conn.execute(
                """CREATE TABLE IF NOT EXISTS dual_ai_tuning_runs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT,
                    merged_proposals TEXT, applied_ids TEXT,
                    created_at TEXT NOT NULL)"""
            )

    def _init_paper(self):
        with self._paper_ctx() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_accounts(
                    id TEXT PRIMARY KEY, name TEXT, source_strategy TEXT,
                    status TEXT, initial_cash REAL, cash REAL, cycle_days INTEGER,
                    max_positions INTEGER, max_weight REAL, max_exposure REAL,
                    version TEXT, params TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS paper_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT,
                    event TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_parameter_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER,
                    account_id TEXT, version TEXT, style TEXT, params TEXT,
                    reason TEXT, effective_date TEXT, created_at TEXT);
                """
            )
            conn.execute(
                "INSERT INTO paper_accounts(id,name,source_strategy,status,initial_cash,"
                "cash,cycle_days,max_positions,max_weight,max_exposure,version,params,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ACCOUNT_ID, ACCOUNT_ID, SOURCE_STRATEGY, "active", 100000.0, 100000.0,
                 60, 15, 0.10, 0.80, "v1", "{}", "2026-01-01T00:00:00+08:00"),
            )

    # -- seeders -----------------------------------------------------------
    def _seed_tuner_run(self, status="consensus", applied_ids=None,
                        proposals=None, created_at=None):
        """写入一条调参运行（真实 dual_ai_tuner 也写这张表）。"""
        proposals = proposals if proposals is not None else [self._proposal()]
        created_at = created_at or (
            self.applied_moment - dt.timedelta(minutes=2)).isoformat(timespec="seconds")
        with self._adaptive_ctx() as conn:
            cursor = conn.execute(
                "INSERT INTO dual_ai_tuning_runs(status,merged_proposals,applied_ids,created_at) "
                "VALUES(?,?,?,?)",
                (status, json.dumps(proposals),
                 json.dumps(applied_ids) if applied_ids else None, created_at),
            )
            return cursor.lastrowid

    def _proposal(self, delta=0.02):
        return {
            "account_id": ACCOUNT_ID,
            "weights": {k: v + delta for k, v in _base_weights().items()},
            "entry_score_delta": 0.0,
            "conditions": {},
        }

    def _track_run(self, run_id, status="consensus", applied=False, applied_count=0):
        with self._adaptive_ctx() as conn:
            return SE.track_run(
                conn, run_id, trigger="scheduled-close", mode="normal",
                status=status, market_regime="trend",
                applied=applied, applied_count=applied_count,
            )

    def _apply_run(self, run_id):
        """走**真实**的显式 apply 生命周期，产生真正的生效证据。"""
        with _frozen_apply_clock(self.applied_moment):
            return evolution_apply.apply_tuner_proposals(
                self._adaptive_ctx, self.paper_path, run_id, confirmed=True)

    def _account_meta(self):
        with self._paper_ctx() as conn:
            row = conn.execute(
                "SELECT params FROM paper_accounts WHERE id=?", (ACCOUNT_ID,)
            ).fetchone()
        return (json.loads(row["params"] or "{}") or {}).get("adaptive_selection_meta") or {}

    def _effective_from(self):
        """从真实 apply 写下的覆盖里读出生效日期。"""
        meta = self._account_meta()
        return dt.date.fromisoformat(str(meta["effective_date"])[:10])

    def _seed_reward(self, start_date, end_date, raw_reward=0.8,
                     account_id=ACCOUNT_ID, horizon=1):
        with self._adaptive_ctx() as conn:
            cursor = conn.execute(
                """INSERT INTO adaptive_rewards(
                       account_id,horizon,start_date,end_date,regime,strategy_return_pct,
                       benchmark_return_pct,excess_return_pct,drawdown_pct,turnover_pct,
                       raw_reward,weighted_reward,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, horizon, start_date, end_date, "trend",
                 1.0, 0.5, 0.5, -1.0, 10.0, raw_reward, raw_reward, "2026-01-01T00:00:00+08:00"),
            )
            return cursor.lastrowid

    def _reconcile(self):
        with self._adaptive_ctx() as conn:
            return AE.reconcile_tuning_reward_attribution(conn)

    def _tracking(self, tracking_id):
        with self._adaptive_ctx() as conn:
            return SE.get_tracking(conn, tracking_id)

    def _attributions(self, tracking_id=None):
        with self._adaptive_ctx() as conn:
            return SE.list_reward_attributions(conn, tracking_id)


class EffectiveTuningPositivePathTests(RewardAttributionTestBase):
    """F：真实因果顺序的正向路径。"""

    def test_F_valid_chain_marks_evaluated_and_scores_with_production_mapping(self):
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)

        effective_from = self._effective_from()
        self.assertEqual(effective_from, self.applied_day)
        # apply 之后账户覆盖必须真的存在（证据来自真实生命周期）
        meta = self._account_meta()
        self.assertEqual(meta["status"], "active")
        self.assertEqual(meta["tier"], "llm_consensus")
        self.assertEqual(meta["run_id"], run_id)

        # 生效之前的 reward 先落库：归因匹配器必须无视它
        self._seed_reward(
            (effective_from - dt.timedelta(days=4)).isoformat(),
            (effective_from - dt.timedelta(days=2)).isoformat(),
            raw_reward=0.30)
        # 生效之后成熟的 reward
        target_id = self._seed_reward(
            (effective_from + dt.timedelta(days=1)).isoformat(),
            (effective_from + dt.timedelta(days=5)).isoformat(),
            raw_reward=0.8, horizon=3)

        report = self._reconcile()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(len(report["evaluated"]), 1, report)
        outcome = report["evaluated"][0]
        self.assertEqual(outcome["tracking_id"], tracking_id)
        self.assertEqual(outcome["reward_id"], target_id)
        self.assertEqual(outcome["account_id"], ACCOUNT_ID)
        self.assertEqual(outcome["effective_from"], effective_from.isoformat())
        self.assertTrue(outcome["attribution_created"])

        tracking = self._tracking(tracking_id)
        self.assertEqual(tracking["evaluated"], 1)
        # 评分必须由生产服务给出，并且等于 raw_reward 的 tanh 映射
        self.assertAlmostEqual(tracking["eval_score"], math.tanh(0.8), places=9)
        detail = json.loads(tracking["eval_detail"])
        self.assertEqual(detail["source"], "adaptive_rewards")
        self.assertEqual(detail["score_mapping_version"], AE.EVOLUTION_REWARD_SCORE_VERSION)
        self.assertEqual(detail["raw_reward"], 0.8)
        self.assertEqual(detail["attribution"]["account_id"], ACCOUNT_ID)
        self.assertEqual(detail["attribution"]["effective_from"], effective_from.isoformat())

        links = self._attributions(tracking_id)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["reward_id"], target_id)
        self.assertEqual(links[0]["account_id"], ACCOUNT_ID)
        self.assertEqual(links[0]["linkage_source"], SE.ATTRIBUTION_LINKAGE_SOURCE)

    def test_F_picks_earliest_post_effective_reward_deterministically(self):
        """同一 tracking 的 reward 选择必须确定：取生效之后最早的合规窗口。"""
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()

        earliest = self._seed_reward(
            (effective_from + dt.timedelta(days=1)).isoformat(),
            (effective_from + dt.timedelta(days=2)).isoformat(),
            raw_reward=0.4, horizon=1)
        later = self._seed_reward(
            (effective_from + dt.timedelta(days=3)).isoformat(),
            (effective_from + dt.timedelta(days=8)).isoformat(),
            raw_reward=0.95, horizon=5)
        self.assertNotEqual(earliest, later)

        report = self._reconcile()
        self.assertEqual(len(report["evaluated"]), 1, report)
        self.assertEqual(report["evaluated"][0]["reward_id"], earliest)
        self.assertAlmostEqual(
            self._tracking(tracking_id)["eval_score"], math.tanh(0.4), places=9)


class PreEffectiveRewardTests(RewardAttributionTestBase):
    """A：生效之前的 reward 必须被拒绝。"""

    def test_A_reward_ending_before_effective_is_rejected(self):
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()

        stale_id = self._seed_reward(
            (effective_from - dt.timedelta(days=4)).isoformat(),
            (effective_from - dt.timedelta(days=2)).isoformat())

        # 即使调用方手工建立了归因（模拟错误的配对），门禁也必须拒绝：
        # 归因不是"免检通行证"，时间窗口要独立复核。
        with self._adaptive_ctx() as conn:
            SE.record_reward_attribution(
                conn, tracking_id, stale_id, ACCOUNT_ID, effective_from.isoformat())
            with self.assertRaises(ValueError) as ctx:
                AE.evaluate_tuning_from_reward(
                    tracking_id=tracking_id, reward_id=stale_id, conn=conn)
        self.assertIn("生效", str(ctx.exception))
        self.assertEqual(self._tracking(tracking_id)["evaluated"], 0)

    def test_A_matcher_ignores_pre_effective_reward(self):
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()
        self._seed_reward(
            (effective_from - dt.timedelta(days=4)).isoformat(),
            (effective_from - dt.timedelta(days=2)).isoformat())

        report = self._reconcile()
        self.assertEqual(report["evaluated"], [])
        self.assertEqual([w["tracking_id"] for w in report["waiting_evidence"]], [tracking_id])
        self.assertEqual(self._attributions(tracking_id), [])
        self.assertEqual(self._tracking(tracking_id)["evaluated"], 0)


class OverlappingWindowTests(RewardAttributionTestBase):
    """B：跨越新旧参数的 reward 窗口不能作为纯证据。"""

    def test_B_window_straddling_effective_is_rejected(self):
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()

        straddling = self._seed_reward(
            (effective_from - dt.timedelta(days=1)).isoformat(),
            effective_from.isoformat())

        with self._adaptive_ctx() as conn:
            SE.record_reward_attribution(
                conn, tracking_id, straddling, ACCOUNT_ID, effective_from.isoformat())
            with self.assertRaises(ValueError):
                AE.evaluate_tuning_from_reward(
                    tracking_id=tracking_id, reward_id=straddling, conn=conn)
        self.assertEqual(self._tracking(tracking_id)["evaluated"], 0)

    def test_B_matcher_reports_waiting_evidence_for_straddling_window(self):
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()
        self._seed_reward(
            (effective_from - dt.timedelta(days=1)).isoformat(),
            effective_from.isoformat())

        report = self._reconcile()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["evaluated"], [])
        self.assertEqual([w["tracking_id"] for w in report["waiting_evidence"]], [tracking_id])
        self.assertEqual(self._attributions(tracking_id), [])

    def test_B_window_starting_exactly_on_effective_is_eligible(self):
        """start_date == effective_from 属于生效之后（契约的首选口径是 >=）。"""
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()
        self._seed_reward(effective_from.isoformat(), effective_from.isoformat())

        report = self._reconcile()
        self.assertEqual(len(report["evaluated"]), 1, report)
        self.assertEqual(self._tracking(tracking_id)["evaluated"], 1)


class CrossAccountTests(RewardAttributionTestBase):
    """C：reward 只能评估同一账户的生效调参，禁止跨账户。"""

    def test_C_reward_from_other_account_is_rejected(self):
        """归因指向生效账户，但拿另一个账户的 reward 来评估 → 拒绝。"""
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()

        other_id = self._seed_reward(
            (effective_from + dt.timedelta(days=1)).isoformat(),
            (effective_from + dt.timedelta(days=3)).isoformat(),
            account_id="sector_rotation")

        with self._adaptive_ctx() as conn:
            SE.record_reward_attribution(
                conn, tracking_id, other_id, ACCOUNT_ID, effective_from.isoformat())
            with self.assertRaises(ValueError) as ctx:
                AE.evaluate_tuning_from_reward(
                    tracking_id=tracking_id, reward_id=other_id, conn=conn)
        self.assertIn("跨账户", str(ctx.exception))
        self.assertEqual(self._tracking(tracking_id)["evaluated"], 0)

    def test_C_attribution_for_non_effective_account_is_rejected(self):
        """归因声称的账户本身没有生效证据 → 同样拒绝。"""
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()

        other_id = self._seed_reward(
            (effective_from + dt.timedelta(days=1)).isoformat(),
            (effective_from + dt.timedelta(days=3)).isoformat(),
            account_id="sector_rotation")

        with self._adaptive_ctx() as conn:
            SE.record_reward_attribution(
                conn, tracking_id, other_id, "sector_rotation", effective_from.isoformat())
            with self.assertRaises(ValueError) as ctx:
                AE.evaluate_tuning_from_reward(
                    tracking_id=tracking_id, reward_id=other_id, conn=conn)
        self.assertIn("未真正进入生效状态", str(ctx.exception))
        self.assertEqual(self._tracking(tracking_id)["evaluated"], 0)

    def test_C_matcher_never_uses_other_account_reward(self):
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()
        self._seed_reward(
            (effective_from + dt.timedelta(days=1)).isoformat(),
            (effective_from + dt.timedelta(days=3)).isoformat(),
            account_id="sector_rotation")

        report = self._reconcile()
        self.assertEqual(report["evaluated"], [])
        self.assertEqual([w["tracking_id"] for w in report["waiting_evidence"]], [tracking_id])
        self.assertEqual(self._attributions(tracking_id), [])


class NotEffectiveTrackingTests(RewardAttributionTestBase):
    """D：未真正进入生效状态的 tracking 不能被评估。"""

    def test_D_shadow_consensus_tracking_is_not_effective(self):
        """有共识、有提案、但从未 apply → 不是生效证据。"""
        run_id = self._seed_tuner_run(status="consensus")
        tracking_id = self._track_run(run_id, status="consensus")
        effective_from = self.applied_day
        reward_id = self._seed_reward(
            (effective_from + dt.timedelta(days=1)).isoformat(),
            (effective_from + dt.timedelta(days=3)).isoformat())

        report = self._reconcile()
        self.assertEqual(report["evaluated"], [])
        self.assertEqual([w["tracking_id"] for w in report["waiting_evidence"]], [tracking_id])

        with self._adaptive_ctx() as conn:
            SE.record_reward_attribution(
                conn, tracking_id, reward_id, ACCOUNT_ID, effective_from.isoformat())
            with self.assertRaises(ValueError) as ctx:
                AE.evaluate_tuning_from_reward(
                    tracking_id=tracking_id, reward_id=reward_id, conn=conn)
        self.assertIn("未真正进入生效状态", str(ctx.exception))

    def test_D_consensus_with_merged_proposals_but_no_applied_ids_is_not_effective(self):
        """merged_proposals != [] 不能自动等价 applied。"""
        run_id = self._seed_tuner_run(status="consensus", proposals=[self._proposal()])
        tracking_id = self._track_run(run_id, status="consensus")
        with self._adaptive_ctx() as conn:
            row = conn.execute(
                "SELECT applied_ids FROM dual_ai_tuning_runs WHERE id=?", (run_id,)
            ).fetchone()
        self.assertIsNone(row["applied_ids"])
        report = self._reconcile()
        self.assertEqual(report["evaluated"], [])
        self.assertEqual([w["tracking_id"] for w in report["waiting_evidence"]], [tracking_id])

    def test_D_rolled_back_overlay_is_no_longer_effective(self):
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()
        reward_id = self._seed_reward(
            (effective_from + dt.timedelta(days=1)).isoformat(),
            (effective_from + dt.timedelta(days=3)).isoformat())

        evolution_apply.rollback_tuner_overlay(
            self._adaptive_ctx, self.paper_path, ACCOUNT_ID, confirmed=True)

        report = self._reconcile()
        self.assertEqual(report["evaluated"], [])
        self.assertEqual([w["tracking_id"] for w in report["waiting_evidence"]], [tracking_id])

        with self._adaptive_ctx() as conn:
            SE.record_reward_attribution(
                conn, tracking_id, reward_id, ACCOUNT_ID, effective_from.isoformat())
            with self.assertRaises(ValueError):
                AE.evaluate_tuning_from_reward(
                    tracking_id=tracking_id, reward_id=reward_id, conn=conn)

    def test_D_manual_evaluation_entry_refuses_non_effective_tracking(self):
        """人工评估入口也不得给未生效的 tracking 写 evaluated=1。"""
        run_id = self._seed_tuner_run(status="consensus")
        tracking_id = self._track_run(run_id, status="consensus")
        with self.assertRaises(ValueError) as ctx:
            AE.evaluate_tuning_fn(tracking_id, 0.9)
        self.assertIn("未真正进入生效状态", str(ctx.exception))
        self.assertEqual(self._tracking(tracking_id)["evaluated"], 0)

    def test_D_manual_evaluation_entry_allows_effective_tracking(self):
        """真实生效的调参仍然允许运维手工打分。"""
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        result = AE.evaluate_tuning_fn(tracking_id, 0.25)
        self.assertTrue(result["success"])
        tracking = self._tracking(tracking_id)
        self.assertEqual(tracking["evaluated"], 1)
        self.assertAlmostEqual(tracking["eval_score"], 0.25, places=9)

    def test_D_future_effective_date_is_not_effective(self):
        """尚未真正开始影响 runtime 的调参不能消费历史收益。"""
        future_day = dt.date.today() + dt.timedelta(days=3)
        moment = dt.datetime(future_day.year, future_day.month, future_day.day,
                             15, 30, tzinfo=TZ)
        run_id = self._seed_tuner_run(
            created_at=(moment - dt.timedelta(minutes=2)).isoformat(timespec="seconds"))
        tracking_id = self._track_run(run_id)
        with _frozen_apply_clock(moment):
            evolution_apply.apply_tuner_proposals(
                self._adaptive_ctx, self.paper_path, run_id, confirmed=True)

        report = self._reconcile()
        self.assertEqual(report["evaluated"], [])
        self.assertEqual([w["tracking_id"] for w in report["waiting_evidence"]], [tracking_id])


class OverlaySupersedeTests(RewardAttributionTestBase):
    """生效是**活**属性：overlay 被别的通道顶掉后，这次调参不再生效。

    这是"双条件"里第二条件真正吃劲的场景。``applied_ids`` 单独并不足够：
    ``rollback_tuner_overlay`` 会顺手把账户从 ``applied_ids`` 里摘掉，所以回滚
    由 ``applied_ids`` 就能挡住；但**选股进化通道**
    （``adaptive_selection.apply_candidate``）写入 ``adaptive_selection`` /
    ``adaptive_selection_meta`` 时**不检查**账户上是否已有生效的 tuner 覆盖，
    于是 ``applied_ids`` 仍列着该账户、而 overlay 已经指向别人的版本。
    此时只有"当前 meta 是否仍指向这次 run"能判定它已经不再生效。
    """

    def _supersede_via_selection_channel(self, effective_day):
        """走真实选股进化 apply 通道，用另一条版本覆盖账户 overlay。"""
        with self._adaptive_ctx() as conn:
            ASEL.ensure_schema(conn)
            cursor = conn.execute(
                """INSERT INTO adaptive_selection_candidates(
                       run_date,account_id,regime,model_id,baseline_params,candidate_params,
                       evidence,status,tier,reason,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (effective_day.isoformat(), ACCOUNT_ID, "trend", SOURCE_STRATEGY,
                 json.dumps({"weights": _base_weights()}),
                 json.dumps({"weights": _base_weights()}),
                 "{}", "eligible_manual_review", "manual_review",
                 "supersede an active tuner overlay",
                 "2026-01-01T00:00:00+08:00", "2026-01-01T00:00:00+08:00"),
            )
            conn.commit()
            candidate_id = cursor.lastrowid
        conn = self._adaptive()
        try:
            ASEL.apply_candidate(
                conn, self.paper_path, candidate_id,
                lambda: "2026-01-01T00:00:00+08:00", approved_by="acceptance_operator")
        finally:
            conn.close()
        return candidate_id

    def test_D_superseded_overlay_is_no_longer_effective(self):
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()
        reward_id = self._seed_reward(
            (effective_from + dt.timedelta(days=1)).isoformat(),
            (effective_from + dt.timedelta(days=3)).isoformat())

        self._supersede_via_selection_channel(effective_from)

        # 前置事实：applied_ids 仍列着该账户（所以光看它挡不住）
        with self._adaptive_ctx() as conn:
            applied_ids = json.loads(conn.execute(
                "SELECT applied_ids FROM dual_ai_tuning_runs WHERE id=?", (run_id,)
            ).fetchone()[0])
        with self._paper_ctx() as conn:
            meta = json.loads(conn.execute(
                "SELECT params FROM paper_accounts WHERE id=?", (ACCOUNT_ID,)
            ).fetchone()[0])["adaptive_selection_meta"]
        self.assertEqual(applied_ids, [ACCOUNT_ID])
        self.assertNotEqual(meta.get("run_id"), run_id)

        report = self._reconcile()
        self.assertEqual(report["evaluated"], [])
        self.assertEqual([w["tracking_id"] for w in report["waiting_evidence"]], [tracking_id])

        with self._adaptive_ctx() as conn:
            SE.record_reward_attribution(
                conn, tracking_id, reward_id, ACCOUNT_ID, effective_from.isoformat())
            with self.assertRaises(ValueError) as ctx:
                AE.evaluate_tuning_from_reward(
                    tracking_id=tracking_id, reward_id=reward_id, conn=conn)
        self.assertIn("未真正进入生效状态", str(ctx.exception))
        self.assertEqual(self._tracking(tracking_id)["evaluated"], 0)


class MissingAttributionTests(RewardAttributionTestBase):
    """E：缺少归因记录必须拒绝。"""

    def test_E_missing_attribution_is_rejected(self):
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()
        reward_id = self._seed_reward(
            (effective_from + dt.timedelta(days=1)).isoformat(),
            (effective_from + dt.timedelta(days=3)).isoformat())

        # 账户生效、reward 窗口合规，但归因契约不存在 → 仍然拒绝
        with self._adaptive_ctx() as conn:
            with self.assertRaises(KeyError) as ctx:
                AE.evaluate_tuning_from_reward(
                    tracking_id=tracking_id, reward_id=reward_id, conn=conn)
        self.assertIn("归因记录不存在", str(ctx.exception))
        self.assertEqual(self._tracking(tracking_id)["evaluated"], 0)


class IdempotencyTests(RewardAttributionTestBase):
    """G：同一 (tracking_id, reward_id) 必须幂等；冲突必须 fail closed。"""

    def _positive_setup(self):
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()
        reward_id = self._seed_reward(
            (effective_from + dt.timedelta(days=1)).isoformat(),
            (effective_from + dt.timedelta(days=5)).isoformat(),
            raw_reward=0.6, horizon=3)
        return tracking_id, reward_id, effective_from

    def test_G_repeated_cycles_are_idempotent(self):
        tracking_id, reward_id, _ = self._positive_setup()

        first = self._reconcile()
        self.assertEqual(len(first["evaluated"]), 1, first)
        score_after_first = self._tracking(tracking_id)["eval_score"]

        for _ in range(3):
            again = self._reconcile()
            self.assertEqual(again["status"], "ok")
            self.assertEqual(again["evaluated"], [])
            self.assertEqual(again["failed"], [])
            # 已经 evaluated 的 tracking 不再进入匹配范围
            self.assertEqual(again["waiting_evidence"], [])

        links = self._attributions(tracking_id)
        self.assertEqual(len(links), 1, "重复周期绝不能新增归因")
        self.assertEqual(links[0]["reward_id"], reward_id)
        tracking = self._tracking(tracking_id)
        self.assertEqual(tracking["evaluated"], 1)
        self.assertEqual(tracking["eval_score"], score_after_first)

    def test_G_direct_replay_returns_already_evaluated(self):
        tracking_id, reward_id, effective_from = self._positive_setup()
        self._reconcile()

        with self._adaptive_ctx() as conn:
            replay = AE.evaluate_tuning_from_reward(
                tracking_id=tracking_id, reward_id=reward_id, conn=conn)
        self.assertTrue(replay["success"])
        self.assertTrue(replay["already_evaluated"])
        self.assertAlmostEqual(replay["eval_score"], math.tanh(0.6), places=9)
        self.assertEqual(len(self._attributions(tracking_id)), 1)

    def test_G_interrupted_attribution_is_resumed_with_same_reward(self):
        """落库归因与写入评估之间被中断 → 续做同一条归因，绝不另挑 reward。"""
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)
        effective_from = self._effective_from()
        first = self._seed_reward(
            (effective_from + dt.timedelta(days=1)).isoformat(),
            (effective_from + dt.timedelta(days=3)).isoformat(),
            raw_reward=0.5, horizon=1)
        # 另一条同样合规、分数更"好看"的 reward 不得被误选
        self._seed_reward(
            (effective_from + dt.timedelta(days=4)).isoformat(),
            (effective_from + dt.timedelta(days=6)).isoformat(),
            raw_reward=0.95, horizon=3)
        with self._adaptive_ctx() as conn:
            SE.record_reward_attribution(
                conn, tracking_id, first, ACCOUNT_ID, effective_from.isoformat())

        report = self._reconcile()
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["failed"], [], report)
        self.assertEqual(len(report["evaluated"]), 1, report)
        outcome = report["evaluated"][0]
        self.assertEqual(outcome["reward_id"], first)
        self.assertFalse(outcome["attribution_created"])
        self.assertEqual(len(self._attributions(tracking_id)), 1)
        self.assertAlmostEqual(
            self._tracking(tracking_id)["eval_score"], math.tanh(0.5), places=9)

    def test_G_conflicting_attribution_fails_closed(self):
        tracking_id, reward_id, effective_from = self._positive_setup()
        self._reconcile()

        with self._adaptive_ctx() as conn:
            with self.assertRaises(ValueError) as ctx:
                SE.record_reward_attribution(
                    conn, tracking_id, reward_id, "sector_rotation",
                    effective_from.isoformat())
            self.assertIn("冲突", str(ctx.exception))
            with self.assertRaises(ValueError):
                SE.record_reward_attribution(
                    conn, tracking_id, reward_id, ACCOUNT_ID,
                    (effective_from - dt.timedelta(days=1)).isoformat())

        links = self._attributions(tracking_id)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["account_id"], ACCOUNT_ID)
        self.assertEqual(links[0]["effective_from"], effective_from.isoformat())

    def test_G_second_reward_cannot_rewrite_first_decision(self):
        """tracking 一旦由某条 reward 评估，第二条 reward 不得改写首次决定。"""
        tracking_id, first_reward, effective_from = self._positive_setup()
        self._reconcile()
        first_score = self._tracking(tracking_id)["eval_score"]

        second_reward = self._seed_reward(
            (effective_from + dt.timedelta(days=6)).isoformat(),
            (effective_from + dt.timedelta(days=8)).isoformat(),
            raw_reward=-0.9, horizon=5)

        with self._adaptive_ctx() as conn:
            SE.record_reward_attribution(
                conn, tracking_id, second_reward, ACCOUNT_ID, effective_from.isoformat())
            with self.assertRaises(ValueError):
                AE.evaluate_tuning_from_reward(
                    tracking_id=tracking_id, reward_id=second_reward, conn=conn)

        tracking = self._tracking(tracking_id)
        self.assertEqual(tracking["eval_score"], first_score)
        self.assertNotEqual(first_reward, second_reward)


class NoEvidenceTests(RewardAttributionTestBase):
    """H：没有证据是正常状态（waiting_evidence），绝不能变成失败。"""

    def test_H_no_rewards_at_all_is_waiting_evidence(self):
        run_id = self._seed_tuner_run()
        tracking_id = self._track_run(run_id)
        self._apply_run(run_id)

        report = self._reconcile()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["evaluated"], [])
        self.assertEqual(report["already_evaluated"], [])
        self.assertEqual(report["failed"], [])
        self.assertEqual([w["tracking_id"] for w in report["waiting_evidence"]], [tracking_id])
        self.assertEqual(self._tracking(tracking_id)["evaluated"], 0)

    def test_H_no_tracking_at_all_is_a_clean_noop(self):
        report = self._reconcile()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["evaluated"], [])
        self.assertEqual(report["waiting_evidence"], [])
        self.assertEqual(report["failed"], [])

    def test_H_repeated_no_evidence_never_raises(self):
        run_id = self._seed_tuner_run()
        self._track_run(run_id)
        for _ in range(3):
            report = self._reconcile()
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["failed"], [])


if __name__ == "__main__":
    unittest.main()
