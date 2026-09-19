# -*- coding: utf-8 -*-
"""Round-12：调仓状态的**周期归属**契约（RB-C1 … RB-C8 + 迁移）。

不变量（本文件存在的全部理由）::

    Every rebalance fact belongs to exactly one paper cycle.

    Rebalance state is paper-ledger state,
    but it is also cycle-owned state.

    A scan, plan, cooldown, prior-quality baseline,
    outflow streak and risk-coordination decision
    must never cross a paper-cycle boundary.

    A plan created in cycle N must never be verified
    or acted on in cycle N+1.

    Legacy rebalance rows with unknown cycle ownership
    must remain unknown and operationally invisible.

**为什么 Round-11 的测试不够**：``test_rebalance_db_ownership`` 证明了调仓状态
住在 **paper ledger**（数据库归属）。但数据库归属正确 ≠ 周期归属正确：三张表
当时都没有 ``cycle_id``，所以「同一个库」里 cycle 8 与 cycle 9 的状态仍然混在
一起。本文件补的是另一半：**同一张表里的周期分区**。

**为什么必须真跑两个周期**：只要夹具里只有一个周期，"读到了别的周期的行"与
"读到了本周期的行"就是同一件事 —— 这个缺陷**永远测不出来**。因此每个用例都
先构造 cycle 8 的事实，再切到 cycle 9，然后断言 cycle 9 看不到 cycle 8。

驱动真实入口：``api_adaptive`` 的四个 rebalance 路由函数，以及
``rebalance_scanner`` 的公开 API（``daily_close_scan`` / ``verify_all_plans`` /
``get_pending_plans`` / ``_is_risk_handled``）。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adaptive_engine as AE  # noqa: E402
import api_adaptive as API  # noqa: E402
import paper_schema_migrations as PSM  # noqa: E402
import paper_trading as PT  # noqa: E402
import rebalance_scanner as RS  # noqa: E402

ACCOUNT = "tq_breakout"          # max_hold = 4 天（REBALANCE_THRESHOLDS）
CODE = "600519"
NAME = "测试股"

CYCLE8_QUALITY = 90.0
CYCLE9_QUALITY = 50.0
CYCLE8_OUTFLOW_DAYS = 5

#: 收益 0%（< 2%）且持仓远超上限 ⇒ 确定性触发 sell 计划。
QUOTE_FLAT = {"code": CODE, "price": 10.0, "pct": 0.0, "super_net": 0.0,
              "high": 10.0, "low": 10.0}
#: 开盘跌幅超阈值 ⇒ 确定性 verified（见 verify_opening_data 检查1）。
QUOTE_GAP_DOWN = {"code": CODE, "price": 9.0, "pct": -5.0, "vol_ratio": 1.0,
                  "super_net": 0.0}


class _CycleScopeCase(unittest.TestCase):
    """两个**物理分离**的 SQLite（沿用 Round-11 的隔离口径）+ 两个周期。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paper_path = os.path.join(self.tmp.name, "paper.sqlite3")
        self.adaptive_path = os.path.join(self.tmp.name, "adaptive.sqlite3")
        # 在 patch **之前**记录真实生产库路径 —— patch 之后 ``AE.PAPER_DB_PATH``
        # 就是夹具本身，拿它自比毫无意义（本轮我正是这么写错过一次）。
        self.real_paper_path = AE.PAPER_DB_PATH
        self.real_adaptive_path = AE.DB_PATH

        self._pt_patches = (
            mock.patch.object(PT, "DB_PATH", self.paper_path),
            mock.patch.object(PT, "_benchmark_close", return_value=None),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
        )
        for patcher in self._pt_patches:
            patcher.start()
        PT.init_db()
        # 调仓状态表不在 ``init_db`` 的新建库 DDL 里（历史上由 scanner 的
        # ``ensure_schema`` 建）。夹具要直接 seed 扫描/计划行，因此显式建一次 ——
        # 这也顺带验证了迁移函数在"表不存在"时走的是 create 分支。
        self._schema_conn = sqlite3.connect(self.paper_path)
        RS.ensure_schema(self._schema_conn)
        self._schema_conn.commit()
        self._schema_conn.close()

        self._ae_patches = (
            mock.patch.object(AE, "DB_PATH", self.adaptive_path),
            mock.patch.object(AE, "PAPER_DB_PATH", self.paper_path),
            mock.patch.object(AE, "CACHE_DIR", self.tmp.name),
        )
        for patcher in self._ae_patches:
            patcher.start()
        with AE._connect():
            pass

        API._cache_clear()

        self.assertNotEqual(
            os.path.realpath(self.paper_path), os.path.realpath(self.adaptive_path),
            "paper/adaptive DB 指向同一文件 ⇒ 数据库归属缺陷不可能被测出",
        )
        self.assertNotEqual(
            os.path.realpath(self.paper_path), os.path.realpath(self.real_paper_path),
            "夹具库与真实账本指向同一文件 ⇒ 测试会污染真实账本",
        )
        self.assertNotEqual(
            os.path.realpath(self.adaptive_path), os.path.realpath(self.real_adaptive_path),
            "夹具库与真实 adaptive 库指向同一文件 ⇒ 测试会污染真实账本",
        )
        self.conn = sqlite3.connect(self.paper_path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        API._cache_clear()
        for patcher in reversed(self._ae_patches):
            patcher.stop()
        for patcher in reversed(self._pt_patches):
            patcher.stop()
        self.tmp.cleanup()

    # ── 工具 ────────────────────────────────────────────────────────────────
    def _rows(self, sql, params=()):
        conn = sqlite3.connect(self.paper_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _scalar(self, sql, params=()):
        conn = sqlite3.connect(self.paper_path)
        try:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    # ── 夹具 ────────────────────────────────────────────────────────────────
    def add_cycle(self, status="running", stamp="2026-09-19 12:00:00"):
        cur = self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
            "updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
            (f"c-{stamp}", status, 100000.0, "shared_pool", stamp, stamp,
             stamp if status == "running" else None),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def activate(self, cycle_id, account_id=ACCOUNT):
        """把账户绑到某周期并置为 running（active cycle 由 paper_cycles.status 决定）。"""
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=?, status='running' WHERE id=?",
            (cycle_id, account_id),
        )
        self.conn.commit()

    def pause_all_cycles(self):
        self.conn.execute("UPDATE paper_cycles SET status='paused'")
        self.conn.commit()

    def add_lot(self, cycle_id, qty, *, code=CODE, account_id=ACCOUNT, cost=10.0,
                acquired_at="2026-08-31 10:00:00", available_date="2026-09-01"):
        self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
            "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
            "is_t_base,source_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, account_id, code, NAME, "测试", qty, qty, cost,
             acquired_at, available_date, "stock_t1", 1, 1, 4242),
        )
        self.conn.commit()

    def seed_scan(self, cycle_id, *, scan_date, quality, trend, outflow_days=1,
                  account_id=ACCOUNT, code=CODE, action="hold"):
        """直接写一条 rebalance_scans 行（模拟"上一个周期留下的扫描"）。"""
        self.conn.execute(
            "INSERT INTO rebalance_scans(cycle_id,scan_date,account_id,code,name,"
            "current_qty,cost,current_price,unrealized_pnl_pct,hold_days,quality_score,"
            "prev_quality_score,quality_change,fund_flow_trend,consecutive_outflow_days,"
            "action,action_reason,planned_sell_ratio,scan_version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, scan_date, account_id, code, NAME, 100, 10.0, 10.0, 0.0, 30,
             quality, quality, 0.0, trend, outflow_days, action, "fixture",
             0.0, RS.REBALANCE_VERSION, f"{scan_date}T22:00:00"),
        )
        self.conn.commit()

    def seed_plan(self, cycle_id, *, plan_date, account_id=ACCOUNT, code=CODE,
                  status="planned", sell_qty=100):
        cur = self.conn.execute(
            "INSERT INTO rebalance_plans(cycle_id,plan_date,account_id,code,name,action,"
            "sell_qty,sell_ratio,sell_reason,status,plan_version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, plan_date, account_id, code, NAME, "sell", sell_qty, 1.0,
             "fixture", status, RS.REBALANCE_VERSION,
             f"{plan_date}T22:00:00", f"{plan_date}T22:00:00"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def seed_legacy_plan(self, *, plan_date, account_id=ACCOUNT, code=CODE,
                         status="planned", sell_qty=100):
        self.seed_legacy_rows(
            "INSERT INTO rebalance_plans(cycle_id,plan_date,account_id,code,name,action,"
            "sell_qty,sell_ratio,sell_reason,status,plan_version,created_at,updated_at) "
            "VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_date, account_id, code, NAME, "sell", sell_qty, 1.0, "legacy", status,
             RS.REBALANCE_VERSION, f"{plan_date}T22:00:00", f"{plan_date}T22:00:00"),
        )

    def current_cycle(self):
        return int(self.conn.execute(
            "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
            " ORDER BY id DESC LIMIT 1").fetchone()[0])

    # ── legacy 行：必须以**迁移前**的写法产生 ──────────────────────────────
    def seed_legacy_rows(self, sql, params=()):
        """写一条 ``cycle_id IS NULL`` 的 legacy 行。

        v19 的 guard 会拒绝**新**写入的 NULL 周期行 —— 那正是它存在的理由。
        legacy 行之所以是 NULL，是因为它们在迁移**之前**就被写下了，因此夹具
        必须按迁移前的账本写法产生它们：先摘掉 guard，写完再跑一次迁移把它装
        回来。这样"legacy 行存在"与"新行必须有周期"两条约束同时为真。
        """
        self._drop_cycle_guards()
        self.conn.execute(sql, params)
        self.conn.commit()
        PSM.ensure_rebalance_state_cycle_ownership(self.conn)
        self.conn.commit()

    def _drop_cycle_guards(self):
        for table in PSM.REBALANCE_STATE_TABLES:
            self.conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_cycle_required_insert")
            self.conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_cycle_immutable")
        self.conn.commit()

    # ── 驱动真实 HTTP handler ───────────────────────────────────────────────
    def scan(self, quotes):
        with mock.patch.object(API, "_fetch_rebalance_quotes",
                               return_value=(quotes, {"source": "fixture"})):
            return API.run_rebalance_scan(confirmed=True)

    def verify(self, quotes):
        with mock.patch.object(API, "_fetch_rebalance_quotes",
                               return_value=(quotes, {"source": "fixture"})):
            return API.verify_rebalance_plans(confirmed=True)

    # ── 承重夹具：cycle 8 → cycle 9 的同日翻转 ──────────────────────────────
    def rollover_fixture(self):
        """cycle 8（同日 10:00）留下扫描 + planned 计划，然后 active 切到 cycle 9。

        返回 ``(cycle8, cycle9)``。这是 §18 的 same-day rollover：两个周期在
        **同一天**，因此旧的 ``UNIQUE(scan_date, account_id, code)`` 必然冲突。
        """
        c8 = self.current_cycle()
        self.seed_scan(c8, scan_date="2026-09-19", quality=CYCLE8_QUALITY,
                       trend="outflow", outflow_days=1)
        for day in range(2, CYCLE8_OUTFLOW_DAYS + 1):
            self.seed_scan(c8, scan_date=f"2026-09-{18 + day:02d}",
                           quality=CYCLE8_QUALITY, trend="outflow", outflow_days=day)
        self.seed_plan(c8, plan_date="2026-09-19")
        # active 切到 cycle 9（同一天 12:00）。
        c9 = self.add_cycle(stamp="2026-09-19 12:00:00")
        self.activate(c9)
        self.add_lot(c9, 100)
        return c8, c9


# ─── §22 Plan creation / verification E2E ───────────────────────────────────

class RB_C1_PlanCarriesCreationCycle(_CycleScopeCase):
    """RB-C1：cycle 8 的扫描创建的计划带 ``cycle_id=8``。"""

    def test_RB_C1_plan_is_stamped_with_the_scan_cycle(self):
        self.activate(self.current_cycle())
        self.add_lot(self.current_cycle(), 100)
        c8 = self.current_cycle()

        result = self.scan({CODE: QUOTE_FLAT})

        self.assertGreaterEqual(int(result.get("plans_created") or 0), 1,
                                "夹具未达到计划阈值")
        self.assertEqual(int(result.get("cycle_id")), c8,
                         "scan 结果未带本周期 cycle_id")
        plans = self._rows("SELECT cycle_id,status FROM rebalance_plans")
        self.assertEqual([int(p["cycle_id"]) for p in plans], [c8],
                         "计划未带上创建时的 cycle_id")
        scans = self._rows("SELECT DISTINCT cycle_id FROM rebalance_scans")
        self.assertEqual([int(s["cycle_id"]) for s in scans], [c8],
                         "扫描行未带上本周期 cycle_id")

    def test_RB_C1_scan_and_plan_share_one_cycle(self):
        """scan 与 plan 必须同周期 —— "scan cycle 8 / plan cycle 9" 不可表达。"""
        self.activate(self.current_cycle())
        self.add_lot(self.current_cycle(), 100)
        self.scan({CODE: QUOTE_FLAT})

        scan_cycles = {int(r["cycle_id"]) for r in
                       self._rows("SELECT cycle_id FROM rebalance_scans")}
        plan_cycles = {int(r["cycle_id"]) for r in
                       self._rows("SELECT cycle_id FROM rebalance_plans")}
        self.assertEqual(scan_cycles, plan_cycles,
                         "scan 与 plan 落在了不同周期")


class RB_C2_PendingPlansAreCycleScoped(_CycleScopeCase):
    """RB-C2：active 移到 9 后，``get_pending_plans(cycle=9)`` 不返回 cycle 8 的计划。"""

    def test_RB_C2_cycle8_plan_is_invisible_to_cycle9(self):
        c8, c9 = self.rollover_fixture()
        self.assertEqual(len(self._rows("SELECT 1 FROM rebalance_plans")), 1,
                         "夹具未产出 cycle 8 计划")

        with mock.patch.object(AE, "PAPER_DB_PATH", self.paper_path):
            pending9 = RS.get_pending_plans(self.conn, cycle_id=c9)

        self.assertEqual(pending9, [],
                         f"cycle 9 的 get_pending_plans 返回了 cycle 8 的计划：{pending9}")
        # 正对照：cycle 8 自己仍然能看到自己的计划（不是"什么都看不到"）。
        with mock.patch.object(AE, "PAPER_DB_PATH", self.paper_path):
            pending8 = RS.get_pending_plans(self.conn, cycle_id=c8)
        self.assertEqual([int(p["cycle_id"]) for p in pending8], [c8],
                         "cycle 8 看不到自己的计划 ⇒ 过滤过宽")

    def test_RB_C2_legacy_null_plan_is_never_pending(self):
        """legacy（``cycle_id IS NULL``）计划不可执行：任何周期都取不到它。"""
        self.seed_legacy_plan(plan_date="2026-09-19")
        c9 = self.current_cycle()

        pending = RS.get_pending_plans(self.conn, cycle_id=c9)

        self.assertEqual(pending, [],
                         "legacy NULL 周期计划出现在了待执行列表里")


class RB_C3_VerifyRejectsForeignCyclePlan(_CycleScopeCase):
    """RB-C3：把 cycle 8 的计划直接喂给 ``verify_all_plans(cycle=9)`` ⇒ 拒绝。"""

    def test_RB_C3_stale_plan_is_rejected_and_not_updated(self):
        c8, c9 = self.rollover_fixture()
        stale = self._rows("SELECT * FROM rebalance_plans")[0]

        with self.assertRaises(RS.StalePlanCycle):
            RS.verify_all_plans(self.conn, [stale], {CODE: QUOTE_GAP_DOWN}, cycle_id=c9)

        # fail closed：一行都没改。
        after = self._rows("SELECT status,open_verified FROM rebalance_plans")
        self.assertEqual(after[0]["status"], "planned",
                         "cycle 8 的计划在 cycle 9 被改写了")
        self.assertIn(after[0]["open_verified"], (0, None),
                      "cycle 8 的计划在 cycle 9 被验证了")

    def test_RB_C3_null_cycle_plan_is_rejected(self):
        """legacy NULL 周期计划同样不可验证。"""
        c9 = self.current_cycle()
        self.seed_legacy_plan(plan_date="2026-09-19")
        stale = self._rows("SELECT * FROM rebalance_plans")[0]

        with self.assertRaises(RS.StalePlanCycle):
            RS.verify_all_plans(self.conn, [stale], {CODE: QUOTE_GAP_DOWN}, cycle_id=c9)

    def test_RB_C3_forged_plan_identity_is_rejected_by_update_guard(self):
        """§15 defense in depth：**计划自己声称**的周期不可信。

        调用方可以伪造一个 dict：``cycle_id`` 写成当前周期（骗过上面的身份检查），
        但 ``id`` 指向库里一条**属于旧周期**的真实行。此时唯一还能挡住它的是
        UPDATE 自身的 ``WHERE id=? AND cycle_id=?`` 与 ``rowcount != 1`` 的
        fail closed —— 只按 ``id`` 更新的实现会在这里把 cycle 8 的计划改写成
        verified，而调用方以为自己在验证 cycle 9。
        """
        c8, c9 = self.rollover_fixture()
        real = self._rows("SELECT * FROM rebalance_plans")[0]
        self.assertEqual(int(real["cycle_id"]), c8, "夹具的计划不属于 cycle 8")

        forged = {**real, "cycle_id": c9}          # 谎报归属
        with self.assertRaises(RS.StalePlanCycle):
            RS.verify_all_plans(self.conn, [forged], {CODE: QUOTE_GAP_DOWN}, cycle_id=c9)
        self.conn.commit()

        after = self._rows("SELECT status,open_verified FROM rebalance_plans")[0]
        self.assertEqual(after["status"], "planned",
                         "伪造归属的 stale plan 被 UPDATE 改写了（未按 cycle_id 过滤）")
        self.assertIn(after["open_verified"], (0, None),
                      "伪造归属的 stale plan 被打上了验证标记")


class RB_C4_SameCycleVerifyWorks(_CycleScopeCase):
    """RB-C4：同周期验证正常工作（正对照，防止"什么都拒绝"）。"""

    def test_RB_C4_same_cycle_plan_verifies(self):
        c9 = self.current_cycle()
        self.activate(c9)
        self.add_lot(c9, 100)
        self.seed_plan(c9, plan_date="2026-09-19")
        plan = self._rows("SELECT * FROM rebalance_plans")[0]

        results = RS.verify_all_plans(
            self.conn, [plan], {CODE: QUOTE_GAP_DOWN}, cycle_id=c9)
        # 直接调 scanner 时用的是夹具自己的连接，必须显式提交，否则下面
        # 用**新连接**读回时看不到这次写入（那会把"没提交"误报成"没生效"）。
        self.conn.commit()

        self.assertEqual(len(results), 1, "同周期计划未被验证")
        after = self._rows("SELECT status,open_verified FROM rebalance_plans")[0]
        self.assertEqual(after["status"], "verified",
                         "同周期计划的验证写回没有生效")
        self.assertEqual(int(after["open_verified"]), 1)


# ─── §23 Scan-history E2E ───────────────────────────────────────────────────

class RB_C5_PrevQualityIsSameCycle(_CycleScopeCase):
    """RB-C5：cycle 8 quality=90，cycle 9 首次扫描 quality=50 ⇒ prev=50，不是 90。"""

    def test_RB_C5_prev_quality_does_not_borrow_cycle8_baseline(self):
        self.seed_scan(self.current_cycle(), scan_date="2026-09-18",
                       quality=CYCLE8_QUALITY, trend="neutral")
        c8 = self.current_cycle()
        c9 = self.add_cycle(stamp="2026-09-19 12:00:00")
        self.activate(c9)
        self.add_lot(c9, 100, cost=10.0)

        # 持仓的 quality_score 来自 lot/position 的 latest_quality_review；
        # 夹具用 cost==price 与 hold_days 触发计划，质量分走默认 50.0。
        self.scan({CODE: QUOTE_FLAT})

        row = self._rows("SELECT cycle_id,quality_score,prev_quality_score,quality_change"
                         " FROM rebalance_scans WHERE cycle_id=?", (c9,))
        self.assertEqual(len(row), 1, "cycle 9 的首次扫描未落库")
        self.assertEqual(float(row[0]["quality_score"]), CYCLE9_QUALITY)
        self.assertEqual(float(row[0]["prev_quality_score"]), CYCLE9_QUALITY,
                         "cycle 9 的 prev_quality_score 借用了 cycle 8 的 90.0 基线")
        self.assertEqual(float(row[0]["quality_change"]), 0.0,
                         "cycle 9 的 quality_change 相对旧周期计算")
        # cycle 8 的那一行必须原样保留（不是被 replace 掉）。
        self.assertEqual(
            self._scalar("SELECT quality_score FROM rebalance_scans WHERE cycle_id=?", (c8,)),
            CYCLE8_QUALITY, "cycle 8 的扫描行被覆盖了")


class RB_C6_ConsecutiveOutflowIsSameCycle(_CycleScopeCase):
    """RB-C6：cycle 8 连续 5 天 outflow，cycle 9 第一天 outflow ⇒ 计数 1，不是 6。"""

    def test_RB_C6_outflow_streak_does_not_cross_cycles(self):
        today = RS._date()
        c8 = self.current_cycle()
        # cycle 8：连续 5 天 outflow（都在今天之前）。
        for offset in range(CYCLE8_OUTFLOW_DAYS + 1, 1, -1):
            self.seed_scan(c8, scan_date=(today - dt.timedelta(days=offset)).isoformat(),
                           quality=CYCLE8_QUALITY, trend="outflow", outflow_days=1)
        c9 = self.add_cycle(stamp=f"{today.isoformat()} 12:00:00")
        self.activate(c9)
        self.add_lot(c9, 100)
        # cycle 9 **自己**的昨天也是 outflow。这样连续天数在正确实现下是 1
        # （本日扫描时只数到 cycle 9 自己那 1 行），而在跨周期实现下是 5
        # （cycle 9 的 1 行 + cycle 8 的 4 行填满 LIMIT 5）。两个读数都非零，
        # 判别力因此不依赖"cycle 9 首日无历史"这个更弱的场景。
        self.seed_scan(c9, scan_date=(today - dt.timedelta(days=1)).isoformat(),
                       quality=CYCLE9_QUALITY, trend="outflow", outflow_days=1)

        # 报价 super_net < 0 ⇒ 本日 outflow。
        self.scan({CODE: {**QUOTE_FLAT, "super_net": -1.0e7}})

        row = self._rows("SELECT consecutive_outflow_days,fund_flow_trend"
                         " FROM rebalance_scans WHERE cycle_id=?"
                         " ORDER BY id DESC LIMIT 1", (c9,))[0]
        self.assertEqual(row["fund_flow_trend"], "outflow", "夹具未产生 outflow")
        self.assertEqual(int(row["consecutive_outflow_days"]), 1,
                         f"cycle 9 的连续流出天数含 cycle 8 的行"
                         f"（期望 1 = cycle 9 自己的 1 天，实际"
                         f" {row['consecutive_outflow_days']}）")
        # cycle 8 的 5 行必须原样保留。
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM rebalance_scans WHERE cycle_id=?", (c8,)),
            CYCLE8_OUTFLOW_DAYS, "cycle 8 的扫描行被破坏")


# ─── §24 Risk-handled E2E ───────────────────────────────────────────────────

class RB_C7_RiskHandledIsSameCycle(_CycleScopeCase):
    """RB-C7：cycle 8 今日已验证硬止损卖出 ⇒ cycle 9 的新持仓**不**因此 risk_handled。"""

    def _add_verified_hard_stop(self, cycle_id, *, created_at):
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "filled_price,amount,fees,status,reason,risk_payload,realized_pnl,created_at,"
            "executed_at,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_verified,execution_status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "sell", CODE, NAME, 100, 9.5, 9.5, 950.0, 1.0, "filled",
             "硬止损清仓", '{"exit_class":"hard_stop","exit_reason_code":"hard_stop"}',
             -51.0, created_at, created_at, *stamp, cycle_id, 1, "verified"),
        )
        self.conn.commit()

    def test_RB_C7_cycle8_order_does_not_handle_cycle9_position(self):
        c8, c9 = self.rollover_fixture()
        today = RS._date()
        # cycle 8 **今天**的已验证硬止损卖出（同日翻周期的关键情形）。
        self._add_verified_hard_stop(c8, created_at=f"{today.isoformat()} 09:35:00")

        status = RS._is_risk_handled(self.conn, ACCOUNT, CODE, today, c9)

        self.assertFalse(status["handled"],
                         "cycle 8 的卖出委托把 cycle 9 的新持仓判成了 risk_handled")

    def test_RB_C8_same_cycle_order_does_handle(self):
        """RB-C8：cycle 9 自己的已验证硬止损卖出 ⇒ cycle 9 确实 risk_handled。"""
        c8, c9 = self.rollover_fixture()
        today = RS._date()
        self._add_verified_hard_stop(c8, created_at=f"{today.isoformat()} 09:35:00")
        self._add_verified_hard_stop(c9, created_at=f"{today.isoformat()} 09:36:00")

        status = RS._is_risk_handled(self.conn, ACCOUNT, CODE, today, c9)

        self.assertTrue(status["handled"],
                        "cycle 9 自己的风控退出未被识别（过滤过宽）")
        self.assertIsNotNone(status["order_id"])


# ─── §25 Status E2E ─────────────────────────────────────────────────────────

class RB_C_StatusIsCurrentCycleOnly(_CycleScopeCase):
    """§25：``/rebalance/status`` 只展示当前周期，不混旧周期。"""

    def test_status_shows_only_current_cycle(self):
        c8, c9 = self.rollover_fixture()
        # cycle 9 自己来一次真实扫描。
        self.scan({CODE: QUOTE_FLAT})

        status = API.rebalance_status()

        self.assertEqual(int(status["cycle_id"]), c9, "status 未报告当前周期")
        self.assertEqual([r["code"] for r in status["recent_scans"]], [CODE],
                         "status 的 recent_scans 混入了旧周期")
        self.assertEqual(
            {int(r["cycle_id"]) for r in
             self._rows("SELECT DISTINCT cycle_id FROM rebalance_scans")},
            {c8, c9}, "夹具未留下两个周期的扫描行")
        # pending 必须只有 cycle 9（cycle 8 的计划还在 planned）。
        self.assertEqual(
            sorted(int(p["id"]) for p in status["pending_plans"]),
            sorted(int(p["id"]) for p in
                   self._rows("SELECT id FROM rebalance_plans WHERE cycle_id=?", (c9,))),
            "status 的 pending_plans 混入了 cycle 8 的计划",
        )

    def test_plans_endpoint_is_current_cycle_only(self):
        c8, c9 = self.rollover_fixture()
        self.scan({CODE: QUOTE_FLAT})

        result = API.get_rebalance_plans(status="all")

        self.assertEqual(int(result["cycle_id"]), c9)
        self.assertTrue(all(int(p["cycle_id"]) == c9 for p in result["plans"]),
                        "plans 接口返回了非当前周期的计划")


# ─── §26 Verify race E2E ────────────────────────────────────────────────────

class RB_C_VerifyCycleChangeRace(_CycleScopeCase):
    """§26：取计划（cycle 8）→ 取行情 → 周期切到 9 → verify 必须 fail closed。"""

    def test_cycle_change_during_verify_fails_closed(self):
        from fastapi import HTTPException

        c8 = self.current_cycle()
        self.activate(c8)
        self.add_lot(c8, 100)
        self.seed_plan(c8, plan_date="2026-09-19")
        self.assertEqual(
            [r["status"] for r in self._rows("SELECT status FROM rebalance_plans")],
            ["planned"], "夹具未产出 planned 计划")

        def _fetch_and_rollover():
            """模拟"取行情期间周期从 8 翻到 9"。"""
            c9 = self.add_cycle(stamp="2026-09-19 12:00:00")
            self.pause_all_cycles()
            self.conn.execute("UPDATE paper_cycles SET status='running' WHERE id=?", (c9,))
            self.conn.commit()
            return {CODE: QUOTE_GAP_DOWN}, {"source": "fixture"}

        with mock.patch.object(API, "_fetch_rebalance_quotes",
                               side_effect=_fetch_and_rollover):
            with self.assertRaises(HTTPException) as ctx:
                API.verify_rebalance_plans(confirmed=True)

        self.assertEqual(ctx.exception.status_code, 409,
                         "周期变化时 verify 未 fail closed")
        detail = ctx.exception.detail
        self.assertEqual(detail["status"], "cycle_changed_during_verify")
        self.assertEqual(int(detail["requested_cycle_id"]), c8)
        self.assertNotEqual(int(detail["current_cycle_id"]), c8)

        # 承重断言：cycle 8 的计划**一行未改**，0 条被验证。
        after = self._rows("SELECT status,open_verified FROM rebalance_plans")
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["status"], "planned",
                         "周期变化后 stale plan 仍被验证了")
        self.assertIn(after[0]["open_verified"], (0, None),
                      "周期变化后 stale plan 被打上了验证标记")


# ─── §9 Fail closed：没有 active cycle ──────────────────────────────────────

class NoActiveCycleFailsClosed(_CycleScopeCase):
    """§9：没有 active cycle ⇒ 不扫描、不写任何 rebalance 状态。"""

    def test_scan_without_active_cycle_creates_no_state(self):
        from fastapi import HTTPException

        # ``active_cycle_id`` 的契约是 status IN ('draft','running','paused')；
        # 要让"没有 active cycle"成立，必须把**所有**周期移出这个集合
        # （``archived``），只改账户绑定是不够的。
        self.conn.execute("UPDATE paper_cycles SET status='archived'")
        self.conn.execute("UPDATE paper_accounts SET status='running', cycle_id=NULL")
        self.conn.commit()
        self.assertIsNone(RS.resolve_cycle_id(self.conn),
                          "夹具仍有 active cycle ⇒ 测不到 fail closed")

        with self.assertRaises(HTTPException) as ctx:
            self.scan({CODE: QUOTE_FLAT})

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["status"], "no_active_cycle")
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM rebalance_scans"), 0,
            "无周期扫描仍写下了 rebalance_scans 行")
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM rebalance_plans"), 0,
            "无周期扫描仍写下了 rebalance_plans 行")

    def test_daily_close_scan_requires_cycle_id(self):
        """``cycle_id=None`` **不是** permissive fallback。"""
        with self.assertRaises(RS.NoActiveCycle):
            RS.daily_close_scan(self.conn, [], {})
        with self.assertRaises(RS.NoActiveCycle):
            RS.get_pending_plans(self.conn)
        with self.assertRaises(RS.NoActiveCycle):
            RS.verify_all_plans(self.conn, [], {})


# ─── §18 Same-day rollover ──────────────────────────────────────────────────

class SameDayRolloverKeepsBothRows(_CycleScopeCase):
    """§18：同一天两个周期可以各自保存 scan，不 replace、不冲突。"""

    def test_two_cycles_same_day_coexist(self):
        c8 = self.current_cycle()
        self.seed_scan(c8, scan_date="2026-09-19", quality=CYCLE8_QUALITY,
                       trend="outflow")
        c9 = self.add_cycle(stamp="2026-09-19 12:00:00")
        self.activate(c9)
        self.add_lot(c9, 100)

        self.scan({CODE: QUOTE_FLAT})

        rows = self._rows(
            "SELECT cycle_id,scan_date,quality_score FROM rebalance_scans"
            " WHERE account_id=? AND code=? AND scan_date='2026-09-19'"
            " ORDER BY cycle_id", (ACCOUNT, CODE))
        self.assertEqual([int(r["cycle_id"]) for r in rows], [c8, c9],
                         "同一天两个周期未各自保留一行（发生了 replace/冲突）")
        self.assertEqual(float(rows[0]["quality_score"]), CYCLE8_QUALITY,
                         "cycle 8 的同日行被 cycle 9 覆盖")
        self.assertEqual(float(rows[1]["quality_score"]), CYCLE9_QUALITY)

    def test_unique_contract_includes_cycle_id(self):
        """schema 级：UNIQUE 必须含 cycle_id（否则同日跨周期必然互相 replace）。"""
        columns = PSM._unique_index_columns(self.conn, "rebalance_scans")
        self.assertEqual(columns, PSM.REBALANCE_SCANS_UNIQUE,
                         f"rebalance_scans 的唯一契约不含周期：{columns}")
        self.assertIn("cycle_id", columns)


# ─── §4/§5/§7 Schema ────────────────────────────────────────────────────────

class RebalanceSchemaCarriesCycleId(_CycleScopeCase):
    """§4/§5/§7：三张表都必须有 ``cycle_id``，且唯一契约含周期。"""

    def test_all_three_tables_have_cycle_id(self):
        for table in PSM.REBALANCE_STATE_TABLES:
            self.assertIn("cycle_id", PSM.table_columns(self.conn, table),
                          f"{table} 缺少 cycle_id")

    def test_cooldown_primary_key_includes_cycle_id(self):
        pk = PSM._primary_key_columns(self.conn, "rebalance_cooldown")
        self.assertEqual(pk, PSM.REBALANCE_COOLDOWN_PK,
                         f"rebalance_cooldown 主键不含周期：{pk}")

    def test_new_rows_require_a_cycle(self):
        """新行 ``cycle_id=NULL`` 必须被 guard 拒绝（legacy 行不受影响）。"""
        c9 = self.current_cycle()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO rebalance_scans(cycle_id,scan_date,account_id,code,action,"
                "created_at) VALUES(NULL,'2026-09-19',?,?,'hold','now')", (ACCOUNT, CODE))
        self.conn.rollback()
        # 正对照：带真实周期可以写入。
        self.conn.execute(
            "INSERT INTO rebalance_scans(cycle_id,scan_date,account_id,code,action,"
            "created_at) VALUES(?,'2026-09-19',?,?,'hold','now')", (c9, ACCOUNT, CODE))
        self.conn.commit()
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM rebalance_scans WHERE cycle_id IS NULL"), 0)

    def test_cycle_ownership_is_immutable(self):
        """周期归属一经写入不可更改（否则 repair 脚本能把 8 洗成 9）。"""
        c9 = self.current_cycle()
        self.conn.execute(
            "INSERT INTO rebalance_scans(cycle_id,scan_date,account_id,code,action,"
            "created_at) VALUES(?,'2026-09-19',?,?,'hold','now')", (c9, ACCOUNT, CODE))
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE rebalance_scans SET cycle_id=cycle_id+1")
        self.conn.rollback()


# ─── §20/§21 Migration ──────────────────────────────────────────────────────

class MigrationFromLegacySchema(_CycleScopeCase):
    """§20/§21：旧 schema → 新 schema；旧行保留、``cycle_id`` 保持 NULL、幂等。"""

    def _make_legacy_tables(self):
        """在**独立**库里建 Round-11 的旧 schema（无 cycle_id，旧唯一契约）。"""
        path = os.path.join(self.tmp.name, "legacy.sqlite3")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE rebalance_scans(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_date TEXT NOT NULL, account_id TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT, current_qty INTEGER, cost REAL, current_price REAL,
                unrealized_pnl_pct REAL, hold_days INTEGER, quality_score REAL,
                prev_quality_score REAL, quality_change REAL, fund_flow_trend TEXT,
                consecutive_outflow_days INTEGER, action TEXT NOT NULL,
                action_reason TEXT, planned_sell_ratio REAL DEFAULT 0,
                scan_version TEXT, created_at TEXT NOT NULL,
                UNIQUE(scan_date, account_id, code)
            );
            CREATE TABLE rebalance_plans(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_date TEXT NOT NULL, execute_date TEXT, account_id TEXT NOT NULL,
                code TEXT NOT NULL, name TEXT, action TEXT NOT NULL, sell_qty INTEGER,
                sell_ratio REAL, sell_reason TEXT, replacement_code TEXT,
                replacement_name TEXT, replacement_score REAL,
                status TEXT NOT NULL DEFAULT 'planned', open_price REAL, open_pct REAL,
                open_volume_ratio REAL, open_fund_flow REAL,
                open_verified BOOLEAN DEFAULT 0, open_verify_reason TEXT,
                executed_at TEXT, executed_price REAL, executed_qty INTEGER,
                realized_pnl REAL, plan_version TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE rebalance_cooldown(
                code TEXT NOT NULL, account_id TEXT NOT NULL, sold_date TEXT NOT NULL,
                cooldown_until TEXT NOT NULL, PRIMARY KEY(code, account_id)
            );
            CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT);
            INSERT INTO paper_cycles(id,status) VALUES(9,'running');
        """)
        # 旧行：字段逐字保留，``cycle_id`` 无从得知。
        conn.execute(
            "INSERT INTO rebalance_scans(scan_date,account_id,code,name,current_qty,cost,"
            "current_price,unrealized_pnl_pct,hold_days,quality_score,prev_quality_score,"
            "quality_change,fund_flow_trend,consecutive_outflow_days,action,action_reason,"
            "planned_sell_ratio,scan_version,created_at) "
            "VALUES('2026-09-01','tq_breakout','600519','测试股',100,10.0,11.0,10.0,30,"
            "88.0,80.0,8.0,'inflow',0,'hold','legacy',0.0,'v2','2026-09-01T22:00:00')")
        conn.execute(
            "INSERT INTO rebalance_plans(plan_date,account_id,code,name,action,sell_qty,"
            "sell_ratio,sell_reason,status,plan_version,created_at,updated_at) "
            "VALUES('2026-09-01','tq_breakout','600519','测试股','sell',100,1.0,'legacy',"
            "'planned','v2','2026-09-01T22:00:00','2026-09-01T22:00:00')")
        conn.execute(
            "INSERT INTO rebalance_cooldown(code,account_id,sold_date,cooldown_until) "
            "VALUES('600519','tq_breakout','2026-08-31','2026-09-07')")
        conn.commit()
        return conn

    def test_legacy_rows_are_preserved_with_null_cycle(self):
        conn = self._make_legacy_tables()
        try:
            before_scans = conn.execute("SELECT COUNT(*) FROM rebalance_scans").fetchone()[0]
            before_plans = conn.execute("SELECT COUNT(*) FROM rebalance_plans").fetchone()[0]
            before_cool = conn.execute("SELECT COUNT(*) FROM rebalance_cooldown").fetchone()[0]

            PSM.ensure_rebalance_state_cycle_ownership(conn)

            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rebalance_scans").fetchone()[0],
                             before_scans, "重建后扫描行数变化")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rebalance_plans").fetchone()[0],
                             before_plans, "重建后计划行数变化")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rebalance_cooldown").fetchone()[0],
                             before_cool, "重建后冷却行数变化")

            # 其它字段逐字保留。
            row = conn.execute(
                "SELECT scan_date,account_id,code,current_qty,quality_score,"
                "prev_quality_score,fund_flow_trend,action,scan_version,created_at"
                " FROM rebalance_scans").fetchone()
            self.assertEqual(
                tuple(row),
                ('2026-09-01', 'tq_breakout', '600519', 100, 88.0, 80.0, 'inflow',
                 'hold', 'v2', '2026-09-01T22:00:00'),
                "重建改动了旧行的其它字段")
            # 归属保持 unknown —— 绝不猜。
            self.assertIsNone(conn.execute(
                "SELECT cycle_id FROM rebalance_scans").fetchone()[0],
                "迁移给 legacy 扫描行猜了一个 cycle_id")
            self.assertIsNone(conn.execute(
                "SELECT cycle_id FROM rebalance_plans").fetchone()[0],
                "迁移给 legacy 计划行猜了一个 cycle_id")
            self.assertIsNone(conn.execute(
                "SELECT cycle_id FROM rebalance_cooldown").fetchone()[0],
                "迁移给 legacy 冷却行猜了一个 cycle_id")

            # 新唯一契约生效。
            self.assertEqual(PSM._unique_index_columns(conn, "rebalance_scans"),
                             PSM.REBALANCE_SCANS_UNIQUE)
            self.assertEqual(PSM._primary_key_columns(conn, "rebalance_cooldown"),
                             PSM.REBALANCE_COOLDOWN_PK)
        finally:
            conn.close()

    def test_migration_is_idempotent(self):
        """旧 schema → 新 schema；再跑一次必须是 no-op。"""
        conn = self._make_legacy_tables()
        try:
            PSM.ensure_rebalance_state_cycle_ownership(conn)
            first = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='rebalance_scans'").fetchone()[0]
            rows_after_first = conn.execute("SELECT COUNT(*) FROM rebalance_scans").fetchone()[0]

            second = PSM.ensure_rebalance_state_cycle_ownership(conn)

            self.assertEqual(
                second, {"rebalance_scans": "ok", "rebalance_plans": "ok",
                         "rebalance_cooldown": "ok"},
                f"第二次调用不是 no-op：{second}")
            self.assertEqual(
                conn.execute("SELECT sql FROM sqlite_master WHERE name='rebalance_scans'")
                .fetchone()[0], first, "第二次调用改动了表定义")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM rebalance_scans").fetchone()[0],
                rows_after_first, "第二次调用改变了行数")
        finally:
            conn.close()

    def test_legacy_rows_are_operationally_invisible(self):
        """legacy 行不得参与 current-cycle decision / verification。"""
        conn = self._make_legacy_tables()
        try:
            PSM.ensure_rebalance_state_cycle_ownership(conn)
            self.assertEqual(RS.get_pending_plans(conn, cycle_id=9), [],
                             "legacy NULL 计划进入了 current-cycle pending 列表")
            conn.row_factory = sqlite3.Row
            stale = [dict(r) for r in conn.execute(
                "SELECT * FROM rebalance_plans").fetchall()]
            with self.assertRaises(RS.StalePlanCycle):
                RS.verify_all_plans(conn, stale, {}, cycle_id=9)
            # prev_quality_score 也看不到 legacy 行。
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM rebalance_scans WHERE cycle_id=9")
                .fetchone()[0], 0,
                "legacy 扫描行被算进了 cycle 9",
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
