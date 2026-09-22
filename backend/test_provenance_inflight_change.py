# -*- coding: utf-8 -*-
"""R23 review round 2 —— 两个 provenance correctness blocker 的永久回归。

规格 §A1 / §B1。两条都必须在**同一次 production run 内部**制造变化：跑完一次
再改版本（SP-01）或跑完一次再换周期（SP-11）都测不出 TOCTOU —— 那种写法里，
读取发生的时刻本来就在变化之后，任何实现都会「看起来正确」。

* :class:`SignalCycleRolloverTests`（Blocker A）—— 候选构建与 signal commit
  之间存在 provider I/O。期间创建 cycle B、给 B pin 另一个 immutable version、
  把账户重绑到 B。旧实现会在写 signal 时重新解析 ``paper_accounts.cycle_id``，
  于是把属于 cycle A 的候选盖成 B 的策略版本。
* :class:`ResearchVersionInflightTests`（Blocker B）—— ``run_daily`` 在
  ``_run_one`` 完成后才解析 provenance。若 ``_run_one`` 执行期间发布新版本，
  旧实现会把 v1 时代的计算记成 v2。

两条都驱动**真实**生产入口（``PT.generate_signals`` /
``PT._bootstrap_signals_for_today`` / ``PS.run_daily``），不手写 SQL 代替写入器。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_selection as PS
import paper_trading as PT
import runtime_settings as RSET
import strategy_registry as SR
import strategy_selection_resolver as SRES
from test_production_path_golden_replay import (
    CAPITAL,
    D0,
    OfflinePaperEnv,
    RULE,
    STRATEGY_ID,
)

D_DAY = D0


class _RolloverFixture(OfflinePaperEnv, unittest.TestCase):
    """真实生产链路装配 + 一个能在 I/O 窗口内 rollover 的钩子。"""

    def setUp(self):
        self._db_index = getattr(_RolloverFixture, "_db_seq", 0)
        _RolloverFixture._db_seq = self._db_index + 1
        PT.DB_PATH = os.path.join(self._tmp, f"paper_rollover_{self._db_index}.sqlite3")
        self._boot_strategy()

    def _conn(self):
        conn = sqlite3.connect(PT.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _boot_strategy(self):
        with self._conn() as conn:
            SR.ensure_schema(conn)
            SR.create_user_definition(
                conn, STRATEGY_ID, "rollover 回放策略", dsl_ast=RULE,
                metadata={"candidate_topn": 10, "style": "trend", "hold": 8},
                actor="rollover-test",
            )
            SR.transition(conn, STRATEGY_ID, "validated", expected_status="draft",
                          reason="validate", actor="rollover-test")
            SR.transition(conn, STRATEGY_ID, "active", expected_status="validated",
                          reason="activate", actor="rollover-test")
        # 账本 schema 必须先于任何 settings/cycle 写入建立。
        PT.init_db()
        with self._conn() as conn:
            RSET.update(conn, {"enabled_strategies": [STRATEGY_ID]}, actor="rollover-test")
        _, cycle = PT.start_new_cycle(capital=CAPITAL, include_dashboard=False)
        self.cycle_a = int(cycle["id"])
        with self._conn() as conn:
            account = conn.execute(
                "SELECT cycle_id FROM paper_accounts WHERE id=?", (STRATEGY_ID,)
            ).fetchone()
        self.assertEqual(int(account["cycle_id"]), self.cycle_a,
                         "前提：账户属于 cycle A")

    def _pinned(self, cycle_id):
        with self._conn() as conn:
            return SR.cycle_stamp_for_account(conn, STRATEGY_ID, cycle_id=cycle_id)

    def rollover_to_new_cycle(self):
        """创建 cycle B、给 B pin 一个**不同**的 immutable version、把账户搬到 B。

        全部走生产服务（``SR.save_definition`` / ``SR.bind_cycle_versions``），
        只有「把账户行指到 B」这一步是数据搬运 —— 生产里由 ``_create_cycle`` 完成，
        但那条路径要求当前周期先 pause，会同时把账户改成 paused 从而让扫描跳过，
        反而测不到 rollover。这里只搬运归属，其余保持真实。
        """
        with self._conn() as conn:
            version_b = SR.save_definition(
                conn, STRATEGY_ID, {"name": "rollover 之后的 v2"},
                actor="rollover-test", change_note="published during I/O",
            )
            conn.execute(
                "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,"
                "created_at,updated_at) VALUES(?,'running',?,?,datetime('now'),"
                "datetime('now'))",
                (f"rollover-b-{self._db_index}", CAPITAL, "shared_pool"),
            )
            cycle_b = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            SR.bind_cycle_versions(conn, cycle_b, [STRATEGY_ID])
            conn.execute("UPDATE paper_accounts SET cycle_id=?,status='running' WHERE id=?",
                         (cycle_b, STRATEGY_ID))
            conn.commit()
        with self._conn() as conn:
            pinned_b = SR.cycle_stamp_for_account(conn, STRATEGY_ID, cycle_id=cycle_b)
        return cycle_b, version_b.version, pinned_b

    def signal_rows(self):
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id, code, strategy_id, strategy_version, strategy_checksum,"
                " cycle_id FROM paper_signals ORDER BY id")]

    def audit_events(self):
        with self._conn() as conn:
            return [row["event"] for row in conn.execute(
                "SELECT event FROM paper_audit ORDER BY id")]


class SignalCycleRolloverTests(_RolloverFixture):
    """Blocker A —— close signal generation 期间的 rollover。"""

    def test_RV01_close_signal_rollover_does_not_restamp_old_candidates(self):
        """A1: ``generate_signals`` 的候选在 commit 前遭遇 rollover。

        复现（修复前）：候选由 cycle A 的账户构建，provider I/O 期间账户被搬到
        cycle B（B 上 pin 的是 v2）。旧实现直接 ``SELECT cycle_id FROM
        paper_accounts`` 取到 B，于是 A 的候选被写成 ``cycle_id=B,
        strategy_version=v2`` —— 计算时用的明明是 A 的 v1。

        修复后：整批 stale candidate 不写 signal，并留下明确的 stale audit。
        """
        cycle_a = self.cycle_a
        pinned_a = self._pinned(cycle_a)
        self.assertIsNotNone(pinned_a, "前提：cycle A 上有 pin")

        original_market = PT._market_state

        def _market_then_rollover(day, live_universe=None):
            # 候选构建之前：账户仍属于 A。
            state = original_market(day, live_universe=live_universe)
            return state

        # 真正的 I/O 窗口在候选构建之后、写事务之前。用 ``_quotes``（provider 调用）
        # 作为钩子：它发生在 evidence 收集阶段，即 candidate build 与 commit 之间。
        original_quotes = PT._quotes
        rolled = {}

        def _quotes_then_rollover(codes, asof_date=None):
            result = original_quotes(codes, asof_date=asof_date)
            if not rolled:
                rolled["cycle"], rolled["version"], rolled["pin"] = (
                    self.rollover_to_new_cycle())
            return result

        PT._quotes = _quotes_then_rollover
        self.addCleanup(setattr, PT, "_quotes", original_quotes)

        with self._conn() as conn:
            head_before = SR.get_version(STRATEGY_ID, conn=conn).version

        summary = PT.generate_signals(D_DAY)

        self.assertTrue(rolled, "前提：I/O 窗口内确实发生了 rollover")
        cycle_b, version_b, _pin_b = rolled["cycle"], rolled["version"], rolled["pin"]
        self.assertNotEqual(cycle_b, cycle_a)
        self.assertEqual(head_before, pinned_a[1],
                         "前提：cycle A 上 pin 的是当时的 head（v1）")
        self.assertGreater(version_b, pinned_a[1], "前提：B 上 pin 的是另一个版本")

        rows = self.signal_rows()
        stamped_to_b = [row for row in rows if int(row["cycle_id"] or 0) == cycle_b]
        self.assertEqual(
            stamped_to_b, [],
            f"cycle A 的候选被写成了 cycle B（cycle_id={cycle_b}）：{stamped_to_b[:2]}")
        stamped_to_v2 = [row for row in rows if int(row["strategy_version"] or 0) == version_b]
        self.assertEqual(
            stamped_to_v2, [],
            f"cycle A 的候选被写成了 v{version_b}：{stamped_to_v2[:2]}")
        # 正对照：stale 必须有明确审计，而不是静默消失。
        self.assertIn("signal_stale_cycle_context", self.audit_events(),
                      f"stale rollover 没有留下审计：{self.audit_events()[-5:]}")
        account_rows = [row for row in summary.get("accounts", [])]
        self.assertTrue(account_rows and any(row.get("created") == 0 for row in account_rows),
                        f"stale 批次必须报告 0 created：{account_rows}")

    def test_RV02_close_signal_without_rollover_still_writes_the_captured_cycle(self):
        """正对照：没有 rollover 时必须照常写入，且戳是 A 的 v1。

        否则 RV01 可能是「反正什么都不写」造成的假绿。
        """
        cycle_a = self.cycle_a
        pinned_a = self._pinned(cycle_a)
        summary = PT.generate_signals(D_DAY)
        rows = self.signal_rows()
        self.assertTrue(rows, "正对照：没有 rollover 时必须产生 signal")
        for row in rows:
            self.assertEqual(int(row["cycle_id"]), cycle_a,
                             "signal 的 cycle 不是候选构建时的 cycle")
            self.assertEqual(int(row["strategy_version"]), pinned_a[1])
        self.assertNotIn("signal_stale_cycle_context", self.audit_events())
        self.assertTrue(any(row.get("created") for row in summary.get("accounts", [])))


class BootstrapCycleRolloverTests(_RolloverFixture):
    """Blocker A —— intraday / bootstrap 路径的 rollover。"""

    def _bootstrap(self):
        """用生产因子重建装配盘中输入，再驱动真实 bootstrap。

        不手写因子表：``_rebuild_selection_factor_cache`` 是生产里的唯一因子
        落盘口，用它才能保证被测的正是生产闸门（freshness / PIT / 覆盖）。
        """
        built = PT._rebuild_selection_factor_cache(D_DAY)
        self.assertEqual(built.get("status"), "ok",
                         f"前提：因子缓存必须可用：{built}")
        return PT._bootstrap_signals_for_today(D_DAY, source_slot="intraday")

    def test_RV03_bootstrap_signal_rollover_aborts_the_stale_account_batch(self):
        """A2: ``_bootstrap_signals_for_today`` 在候选构建后遭遇 rollover。

        该函数已经持有显式 cycle 上下文（``_active_cycle``），所以修复后必须用
        候选构建前捕获的账户归属来校验，而不是写 signal 时再推导一次当前周期。
        """
        cycle_a = self.cycle_a
        pinned_a = self._pinned(cycle_a)
        self.assertIsNotNone(pinned_a, "前提：cycle A 上有 pin")

        original_candidate_rows = PT._candidate_rows
        rolled = {}

        def _candidate_rows_then_rollover(*args, **kwargs):
            rows, meta = original_candidate_rows(*args, **kwargs)
            # 候选已经构建完成（它绑定在账户当时的 cycle A 上），此刻 rollover。
            if not rolled:
                rolled["cycle"], rolled["version"], rolled["pin"] = (
                    self.rollover_to_new_cycle())
            return rows, meta

        PT._candidate_rows = _candidate_rows_then_rollover
        self.addCleanup(setattr, PT, "_candidate_rows", original_candidate_rows)

        self._bootstrap()

        self.assertTrue(rolled, "前提：候选构建后确实发生了 rollover")
        cycle_b, version_b = rolled["cycle"], rolled["version"]
        self.assertNotEqual(cycle_b, cycle_a)
        self.assertGreater(version_b, pinned_a[1])

        rows = self.signal_rows()
        self.assertEqual(
            [row for row in rows if int(row["cycle_id"] or 0) == cycle_b], [],
            "bootstrap 把 cycle A 的候选写成了 cycle B")
        self.assertEqual(
            [row for row in rows if int(row["strategy_version"] or 0) == version_b], [],
            f"bootstrap 把 cycle A 的候选写成了 v{version_b}")
        self.assertIn("signal_stale_cycle_context", self.audit_events(),
                      "bootstrap 的 stale rollover 没有留下审计")

    def test_RV04_bootstrap_without_rollover_writes_the_declared_cycle(self):
        """正对照：无 rollover 时 bootstrap 正常写入 A/v1。"""
        cycle_a = self.cycle_a
        pinned_a = self._pinned(cycle_a)
        self._bootstrap()
        rows = self.signal_rows()
        self.assertTrue(rows, "正对照：bootstrap 必须产生 signal")
        for row in rows:
            self.assertEqual(int(row["cycle_id"]), cycle_a)
            self.assertEqual(int(row["strategy_version"]), pinned_a[1])


class SignalWriteContextUnitTests(unittest.TestCase):
    """resolver 的冻结上下文：authority 只能来自显式 cycle。"""

    def test_RV05_stale_context_when_observed_cycle_differs_from_declared(self):
        class _Conn:
            def execute(self, *_args, **_kwargs):  # pragma: no cover - 不应被调用
                raise AssertionError("signal_write_context 不得自行查询 paper_accounts")

        with self.assertRaises(SRES.SignalStaleContext) as caught:
            SRES.signal_write_context(
                _Conn(), "acc", cycle_id=1, account_cycle_id=2)
        self.assertEqual(caught.exception.captured_cycle_id, 1)
        self.assertEqual(caught.exception.declared_cycle_id, 2)
        self.assertIn("stale_context", caught.exception.detail)
        self.assertEqual(
            type(caught.exception).event, "signal_stale_cycle_context")

    def test_RV06_non_canonical_cycle_is_unprovable_not_stale(self):
        class _Conn:
            def execute(self, *_args, **_kwargs):  # pragma: no cover
                raise AssertionError("不得查询")

        for bogus in (None, 0, -1, "abc"):
            with self.assertRaises(SRES.SignalCycleUnprovable):
                SRES.signal_write_context(
                    _Conn(), "acc", cycle_id=bogus, account_cycle_id=1)


class ResearchVersionInflightTests(unittest.TestCase):
    """Blocker B —— ``run_daily`` 计算**期间**的策略升级。"""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.registry_db = os.path.join(self.tmp.name, "paper_trading.sqlite3")
        self.research_db = os.path.join(self.tmp.name, "selection_tracking.db")
        import selection_tracking as ST
        self.ST = ST
        for module, attr, value in (
            (PS, "DB_PATH", self.research_db),
            (ST, "DB_PATH", self.research_db),
            (PS, "REGISTRY_DB_PATH", self.registry_db),
            (ST, "REGISTRY_DB_PATH", self.registry_db),
        ):
            old = getattr(module, attr)
            setattr(module, attr, value)
            self.addCleanup(setattr, module, attr, old)
        self.old_run_one = PS._run_one
        self.addCleanup(setattr, PS, "_run_one", self.old_run_one)
        with sqlite3.connect(self.registry_db, timeout=20) as conn:
            SR.ensure_schema(conn)

    def _registry(self):
        conn = sqlite3.connect(self.registry_db, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def test_RV07_inflight_version_publication_does_not_change_the_run_stamp(self):
        """B1: version change 发生在**同一次 run 内部**。

        复现（修复前）：``_run_one`` 里读到 v1 → 计算中途发布 v2 → 返回结果 →
        ``_run_provenance`` 读 current head 得到 v2 → 把 v1 时代的计算记成 v2。

        修复后：pin 在 ``_run_one`` 之前取，落库的必须是 v1。
        """
        from test_strategy_selection_provenance import _payload

        with self._registry() as conn:
            v1 = SR.get_version("tq_breakout", conn=conn)
        self.assertEqual(v1.version, 1, "前提：初始 head 是 v1")

        observed = []

        def _run_one_then_upgrade(model_id, topn):
            with self._registry() as conn:
                observed.append(SR.get_version("tq_breakout", conn=conn).version)
            result = _payload(count=3)
            with self._registry() as conn:
                upgraded = SR.save_definition(
                    conn, "tq_breakout", {"name": "SP-B1 mid-run upgrade"},
                    actor="sp-matrix", change_note="v2 during selection")
                conn.commit()
                observed.append(upgraded.version)
            return result

        PS._run_one = _run_one_then_upgrade
        PS.run_daily(topn=5, run_date=D_DAY.isoformat(), strategies=["tq_breakout"])

        self.assertEqual(observed[:2], [v1.version, 2],
                         "前提：计算开始时是 v1，计算期间升级到 v2")
        with self._registry() as conn:
            head = SR.get_version("tq_breakout", conn=conn)
        self.assertEqual(head.version, 2, "前提：最终 head 是 v2")

        with sqlite3.connect(self.research_db, timeout=20) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT strategy_version, strategy_checksum FROM paper_selection_runs"
                " WHERE strategy_id=? ORDER BY id DESC LIMIT 1", ("tq_breakout",)
            ).fetchone()
        self.assertIsNotNone(row, "必须写出一行 run")
        self.assertEqual(row["strategy_version"], v1.version,
                         "计算期间才发布的 v2 被记成了产出该结果的那一版")
        self.assertEqual(row["strategy_checksum"], v1.checksum)
        self.assertNotEqual(row["strategy_checksum"], head.checksum)


if __name__ == "__main__":
    unittest.main()
