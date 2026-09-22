# -*- coding: utf-8 -*-
"""R23 SP-01 … SP-18 —— Strategy / Selection Provenance 永久回归矩阵。

规格 §18。本文件只驱动**真实**生产入口，不手写最小 DDL：

* family A（``paper_selection_runs`` / ``paper_selection_picks``）用隔离的
  research DB + 隔离的账本 registry DB；
* family B（``selection_runs`` / ``selection_picks``）用同上；
* ledger 侧（``paper_signals`` / ``paper_signals_archive``）用 ``PT.init_db()``
  建出的真实 schema。

每一条都用「正对照 + 反例」成对断言：只断言 final state 是不够的，因为同一
final state 可能来自完全不同的分支。
"""
from __future__ import annotations

import ast
import contextlib
import datetime as dt
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock as mock  # noqa: F401  # noqa: F401 - 供子类 mock.patch 使用

from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_selection as PS
import selection_tracking as ST
import strategy_registry as SR
import strategy_selection_provenance as SP
import strategy_selection_resolver as SRES

DAY = "2026-09-07"
OLD_DAY = "2026-08-03"


def _columns(conn, table):
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


@contextlib.contextmanager
def _closing(conn, factory):
    """A context manager that both commits on success and always closes.

    ``with sqlite3.connect(...)`` commits but never closes, which leaves the
    handle alive on Windows and makes the temp dir un-cleanable.
    """
    conn.row_factory = factory
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _picks(count=3, day=DAY, prefix="6001"):
    return [
        {
            "code": f"{prefix}{i:04d}", "name": f"溯源{i}", "industry": "电子",
            "price": 10.0 + i, "pct": 1.0, "score": 0.9 - i * 0.01,
            "super_net": 1_000_000.0 - i, "reasons": [f"理由{i}"],
            "news_check": {"status": "clean", "hits": 0},
            "historical_factor_date": day,
        }
        for i in range(1, count + 1)
    ]


def _payload(count=3, day=DAY):
    return {
        "strategy": "three_day",
        "strategy_name": "三日策略",
        "picks": _picks(count=count, day=day),
        "data_quality": {"reference_date": day, "complete_cutoff": day},
    }


class _IsolatedStudy(unittest.TestCase):
    """family A / B：隔离 research DB + 隔离 registry DB（显式配置，绝不猜路径）。"""

    def setUp(self):
        # 生产模块用 ``with sqlite3.connect(...)``（提交但不 close），Windows 上
        # 句柄会活到进程结束 —— 隔离目录的清理必须容忍这一点，否则测试会因为
        # 「临时目录删不掉」而不是断言失败而报错。
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.registry_db = os.path.join(self.tmp.name, "paper_trading.sqlite3")
        self.research_db = os.path.join(self.tmp.name, "selection_tracking.db")
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
        PS._run_one = lambda model_id, topn: _payload(count=3)
        self.addCleanup(setattr, PS, "_run_one", self.old_run_one)
        for module, attr, value in (
            (ST, "_latest_signal_date", lambda picks: DAY),
            (ST, "_benchmark_price_on_or_before", lambda day: None),
        ):
            old = getattr(module, attr)
            setattr(module, attr, value)
            self.addCleanup(setattr, module, attr, old)
        with self._registry() as conn:
            SR.ensure_schema(conn)
            conn.commit()

    def _registry(self):
        return _closing(sqlite3.connect(self.registry_db, timeout=20),
                        sqlite3.Row)

    def _research(self):
        return _closing(sqlite3.connect(self.research_db, timeout=20),
                        sqlite3.Row)

    def upgrade(self, strategy_id="tq_breakout", name="改名后的策略"):
        with self._registry() as conn:
            version = SR.save_definition(conn, strategy_id, {"name": name},
                                         actor="sp-matrix", change_note="v2")
            conn.commit()
            return version.version, version.checksum

    def runs(self, strategy_id="tq_breakout", day=DAY):
        with self._research() as conn:
            PS.ensure_schema(conn)
            if strategy_id is None:
                rows = conn.execute(
                    "SELECT * FROM paper_selection_runs WHERE trade_date=? ORDER BY id",
                    (day,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM paper_selection_runs WHERE trade_date=? AND "
                    "strategy_id=? ORDER BY id", (day, strategy_id)).fetchall()
            return [dict(row) for row in rows]

    def run_row(self, strategy_id="tq_breakout", day=DAY):
        rows = self.runs(strategy_id, day)
        return rows[-1] if rows else {}


# ---------------------------------------------------------------------------
# SP-01 … SP-08 —— family A（paper_selection）
# ---------------------------------------------------------------------------


class SelectionProvenanceTests(_IsolatedStudy):
    def test_SP01_v1_selection_still_resolves_v1_after_v2(self):
        """SP-01：v1 run 在 v2 发布后仍解析为 v1（不是 current head）。"""
        with self._registry() as conn:
            v1 = SR.get_version("tq_breakout", conn=conn)
        PS.run_daily(topn=5, run_date=DAY)
        v2, checksum2 = self.upgrade()
        row = self.run_row()
        self.assertEqual(row["strategy_version"], v1.version,
                         "历史 run 被 current head 重新解释了")
        self.assertEqual(row["strategy_checksum"], v1.checksum)
        self.assertNotEqual(row["strategy_checksum"], checksum2)
        reading = SRES.reading_from_run(row, default_scope=SP.SCOPE_RESEARCH)
        self.assertTrue(reading.is_authoritative)
        self.assertEqual(reading.require().strategy_version, v1.version)

    def test_SP02_same_day_versions_do_not_overwrite_each_other(self):
        """SP-02：同日 v1/v2 是两份证据，互相不覆盖。"""
        PS.run_daily(topn=5, run_date=DAY)
        self.upgrade()
        PS.run_daily(topn=5, run_date=DAY)
        rows = self.runs()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["strategy_version"] for row in rows), [1, 2])
        keys = {row["provenance_key"] for row in rows}
        self.assertEqual(len(keys), 2, "不同 immutable version 必须有不同的 run identity")
        # 幂等 retry：同一份证据重跑仍是 2 行，不是 3 行（行 id 会变，内容不变）。
        PS.run_daily(topn=5, run_date=DAY)
        rows = self.runs()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["strategy_version"] for row in rows), [1, 2])
        # picks 跟着各自的 run，不串味；每个 run 都必须有 picks
        with self._research() as conn:
            by_run = {row["run_id"]: row["strategy_id"] for row in conn.execute(
                "SELECT run_id, strategy_id FROM paper_selection_picks "
                "WHERE trade_date=? AND strategy_id=?", (DAY, "tq_breakout")).fetchall()}
        self.assertEqual(sorted(by_run), sorted(row["id"] for row in rows),
                         "同一天两份证据必须各自持有 picks，且不串到别的 run 上")
        self.assertEqual(set(by_run.values()), {"tq_breakout"})

    def test_SP03_cycle_pinned_version_beats_current_head(self):
        """SP-03：cycle pin = v1、current head = v2 → 解析必须是 v1。"""
        with self._registry() as conn:
            SR.bind_cycle_versions(conn, 7, ("tq_breakout",))
            conn.commit()
            self.upgrade()
            reading = SRES.cycle_provenance(conn, "tq_breakout", cycle_id=7, asof_day=DAY)
        self.assertTrue(reading.is_authoritative)
        self.assertEqual(reading.require().strategy_version, 1)
        self.assertEqual(reading.provenance.cycle_id, 7)

    def test_SP04_explicit_cycle_without_pin_fails_closed(self):
        """SP-04：cycle 上没有 pin → fail closed，不回退 current head / legacy。"""
        with self._registry() as conn:
            reading = SRES.cycle_provenance(conn, "tq_breakout", cycle_id=99, asof_day=DAY)
            self.assertFalse(reading.is_authoritative)
            self.assertEqual(reading.status, SP.STATUS_UNKNOWN)
            self.assertIn("no pinned immutable version", reading.detail)
            with self.assertRaises(SP.AsOfUnprovable):
                reading.require()
            # 正对照：同一个 cycle 一旦有 pin，同一调用必须成功。
            SR.bind_cycle_versions(conn, 99, ("tq_breakout",))
            conn.commit()
            ok = SRES.cycle_provenance(conn, "tq_breakout", cycle_id=99, asof_day=DAY)
        self.assertTrue(ok.is_authoritative)

    def test_SP05_wrong_checksum_is_rejected_not_corrected(self):
        """SP-05：checksum 不匹配 → 拒绝，绝不静默纠正成 Registry 里的值。"""
        with self._registry() as conn:
            head = SR.get_version("tq_breakout", conn=conn)
            wrong = "0" * 64
            self.assertNotEqual(wrong, head.checksum)
            conn.execute(
                "INSERT OR REPLACE INTO paper_cycle_strategy_versions(cycle_id,account_id,"
                "strategy_id,strategy_version,strategy_checksum,bound_at) "
                "VALUES(?,?,?,?,?,datetime('now'))",
                (5, "tq_breakout", "tq_breakout", head.version, wrong))
            conn.commit()
            reading = SRES.cycle_provenance(conn, "tq_breakout", cycle_id=5, asof_day=DAY)
            self.assertFalse(reading.is_authoritative, "错 checksum 被接受或纠正了")
            # verify_immutable 也必须拒绝，且不得返回 head.checksum
            self.assertFalse(SRES.verify_immutable(
                conn, SP.StrategySelectionProvenance(
                    strategy_id="tq_breakout", strategy_version=head.version,
                    strategy_checksum=wrong, asof_day=DAY, scope=SP.SCOPE_CYCLE,
                    cycle_id=5)))
            ok = SRES.verify_immutable(
                conn, SP.StrategySelectionProvenance(
                    strategy_id="tq_breakout", strategy_version=head.version,
                    strategy_checksum=head.checksum, asof_day=DAY,
                    scope=SP.SCOPE_CYCLE, cycle_id=5))
        self.assertTrue(ok)

    def test_SP06_archived_strategy_history_still_resolves(self):
        """SP-06：归档后历史 run 仍可解析（presentation ≠ historical authority）。"""
        PS.run_daily(topn=5, run_date=DAY)
        before = self.run_row()
        with self._registry() as conn:
            # 归档前把 v1 pin 到一个周期上：归档之后这条 pin 仍必须是历史权威。
            SR.bind_cycle_versions(conn, 7, ("tq_breakout",))
            conn.commit()
            pinned = SR.cycle_stamp_for_account(conn, "tq_breakout", cycle_id=7)
            SR.archive_definition(conn, "tq_breakout", reason="sp-06", actor="sp-matrix")
            conn.commit()
            status = SR.get("tq_breakout", conn=conn).status
        self.assertEqual(status, "archived")
        self.assertIsNotNone(pinned)
        with self._registry() as conn:
            reading = SRES.cycle_provenance(conn, "tq_breakout", cycle_id=7, asof_day=DAY)
        self.assertTrue(reading.is_authoritative,
                        "归档后历史 version 无法 replay（current lifecycle 成了历史权威）")
        self.assertEqual(reading.require().strategy_version, pinned[1])
        self.assertEqual(reading.require().strategy_checksum, pinned[2])
        after = self.run_row()
        self.assertEqual(after["strategy_version"], before["strategy_version"])
        self.assertEqual(after["strategy_checksum"], before["strategy_checksum"])
        group = next(g for g in PS.latest(trade_date=DAY)["strategies"]
                     if g["strategy_id"] == "tq_breakout")
        self.assertEqual(group["strategy_name"], before["strategy_name"])
        self.assertEqual(group["provenance_status"], SP.STATUS_VERIFIED)
        # 正对照：归档策略不再出现在「现在能跑哪些策略」里
        self.assertNotIn("tq_breakout", [item["strategy_id"] for item in PS.catalog()])

    def test_SP07_legacy_strategy_id_only_row_is_unproven(self):
        """SP-07：只有 strategy_id 的 legacy run → legacy_unproven，不 current-fill。"""
        legacy_db = os.path.join(self.tmp.name, "legacy.db")
        with sqlite3.connect(legacy_db) as conn:
            conn.executescript(
                """
                CREATE TABLE paper_selection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT NOT NULL,
                    strategy_id TEXT NOT NULL, strategy_no INTEGER NOT NULL,
                    strategy_name TEXT NOT NULL, model_id TEXT NOT NULL,
                    status TEXT NOT NULL, message TEXT, factor_date TEXT,
                    topn INTEGER NOT NULL, source TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(trade_date, strategy_id));
                CREATE TABLE paper_selection_picks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT NOT NULL,
                    strategy_id TEXT NOT NULL, rank_no INTEGER NOT NULL,
                    code TEXT NOT NULL, name TEXT, industry TEXT, price REAL, pct REAL,
                    score REAL, super_net REAL, reasons TEXT, news_status TEXT,
                    UNIQUE(trade_date, strategy_id, rank_no));
                """
            )
            conn.execute(
                "INSERT INTO paper_selection_runs(trade_date,strategy_id,strategy_no,"
                "strategy_name,model_id,status,message,factor_date,topn,source,created_at)"
                " VALUES('2026-08-04','trend_pullback',2,'趋势波段优选','bottom_reversal',"
                "'ok','','2026-08-04',5,'scheduled','2026-08-04T17:25:00')")
            conn.execute(
                "INSERT INTO paper_selection_picks(trade_date,strategy_id,rank_no,code,name)"
                " VALUES('2026-08-04','trend_pullback',1,'600001','历史票')")
            conn.commit()
        with sqlite3.connect(legacy_db) as conn:
            conn.row_factory = sqlite3.Row
            PS.ensure_schema(conn)
            row = dict(conn.execute(
                "SELECT * FROM paper_selection_runs WHERE trade_date='2026-08-04'"
            ).fetchone())
            pick = dict(conn.execute(
                "SELECT * FROM paper_selection_picks WHERE trade_date='2026-08-04'"
            ).fetchone())
        with self._registry() as registry:
            head = SR.get("trend_pullback", conn=registry)
        self.assertIsNone(row["strategy_version"], "legacy 行被 current head 回填了")
        self.assertIsNone(row["strategy_checksum"])
        self.assertEqual(row["provenance_status"], SP.STATUS_LEGACY_UNPROVEN)
        reading = SRES.reading_from_run(row, default_scope=SP.SCOPE_RESEARCH)
        self.assertEqual(reading.status, SP.STATUS_LEGACY_UNPROVEN)
        self.assertFalse(reading.is_authoritative)
        # 结构性归属（不是 provenance 回填）：picks 只能属于那一条 legacy run
        self.assertEqual(pick["run_id"], row["id"])
        self.assertIsNotNone(head)

    def test_SP08_future_version_does_not_pollute_earlier_asof(self):
        """SP-08：D+1 的新版本/as-of 不污染 D 日的 run。"""
        PS.run_daily(topn=5, run_date=OLD_DAY)
        before = self.run_row(day=OLD_DAY)
        self.upgrade()
        PS.run_daily(topn=5, run_date=DAY)
        after = self.run_row(day=OLD_DAY)
        self.assertEqual(before["strategy_version"], after["strategy_version"])
        self.assertEqual(before["asof_day"], after["asof_day"])
        self.assertEqual(before["provenance_key"], after["provenance_key"])
        self.assertEqual(len(self.runs(day=OLD_DAY)), 1,
                         "D 日的证据数量被 D+1 的运行改变了")

    def test_SP17_rename_does_not_change_historical_identity(self):
        """SP-17：改名不改历史身份（display ≠ authority）。"""
        PS.run_daily(topn=5, run_date=DAY)
        before = self.run_row()
        self.upgrade(name="完全不同的名字")
        after = self.run_row()
        self.assertEqual(after["strategy_name"], before["strategy_name"])
        self.assertEqual(after["strategy_version"], before["strategy_version"])
        group = next(g for g in PS.latest(trade_date=DAY)["strategies"]
                     if g["strategy_id"] == "tq_breakout")
        self.assertEqual(group["strategy_name"], "短线日内做T")
        self.assertEqual(group["current_strategy_name"], "完全不同的名字")

    def test_SP18_pause_archive_does_not_mutate_historical_stamp(self):
        """SP-18：pause / archive 不改历史 stamp。"""
        PS.run_daily(topn=5, run_date=DAY)
        before = self.run_row()
        with self._registry() as conn:
            latest = SR.get("tq_breakout", conn=conn)
            SR.transition(conn, "tq_breakout", "paused", expected_status=latest.status,
                          reason="sp-18", actor="sp-matrix")
            conn.commit()
            paused = self.run_row()
            self.assertEqual(paused["strategy_version"], before["strategy_version"])
            self.assertEqual(paused["strategy_checksum"], before["strategy_checksum"])
            SR.archive_definition(conn, "tq_breakout", reason="sp-18", actor="sp-matrix")
            conn.commit()
        archived = self.run_row()
        self.assertEqual(archived["strategy_version"], before["strategy_version"])
        self.assertEqual(archived["strategy_checksum"], before["strategy_checksum"])
        self.assertEqual(archived["provenance_status"], SP.STATUS_VERIFIED)


# ---------------------------------------------------------------------------
# SP-09 / SP-10 —— scope 语义
# ---------------------------------------------------------------------------


class ScopeSemanticsTests(_IsolatedStudy):
    def test_SP09_research_scope_never_resolves_the_active_cycle(self):
        """SP-09：research run 不解析 active cycle（NULL 是「不属于」，不是「猜」）。"""
        # 造一个真实的「当前活跃 cycle」：research run 必须完全无视它。
        with _closing(sqlite3.connect(self.research_db, timeout=20), sqlite3.Row) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_cycles("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_key TEXT, status TEXT,"
                "capital REAL, risk_profile TEXT, created_at TEXT, updated_at TEXT)")
            conn.execute(
                "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
                "updated_at) VALUES('sp-09','running',1000000,'paper-risk-v4',"
                "datetime('now'),datetime('now'))")
            active = int(conn.execute(
                "SELECT MAX(id) FROM paper_cycles WHERE status='running'").fetchone()[0])
        self.assertGreaterEqual(active, 1)
        PS.run_daily(topn=5, run_date=DAY)
        row = self.run_row()
        self.assertEqual(row["scope"], SP.SCOPE_RESEARCH)
        self.assertIsNone(row["cycle_id"], "research run 偷偷绑定了 active cycle")
        self.assertIsNone(row["provenance_status"] and None)
        self.assertEqual(row["provenance_status"], SP.STATUS_VERIFIED)
        # 契约在库层也是硬约束：research + cycle_id 必须被拒绝
        with self._research() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO paper_selection_runs(trade_date,strategy_id,strategy_no,"
                    "strategy_name,model_id,status,message,factor_date,topn,source,created_at,"
                    "strategy_version,strategy_checksum,asof_day,scope,cycle_id,"
                    "provenance_status,provenance_key) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (DAY, "injected", 9, "注入", "one_to_two", "ok", "", DAY, 5,
                     "manual", "2026-09-07T17:00:00", 1, "b" * 64, DAY,
                     SP.SCOPE_RESEARCH, active, SP.STATUS_VERIFIED, "injected|key"))

    def test_SP10_cycle_scope_requires_an_explicit_cycle_id(self):
        """SP-10：cycle scope 必须显式 cycle_id（构造不合法直接 ValueError）。"""
        with self.assertRaises(ValueError):
            SP.StrategySelectionProvenance(
                strategy_id="tq_breakout", strategy_version=1,
                strategy_checksum="a" * 64, asof_day=DAY, scope=SP.SCOPE_CYCLE)
        with self.assertRaises(ValueError):
            SP.StrategySelectionProvenance(
                strategy_id="tq_breakout", strategy_version=1,
                strategy_checksum="a" * 64, asof_day=DAY, scope=SP.SCOPE_RESEARCH,
                cycle_id=3)
        with self._registry() as registry:
            refused = SRES.cycle_provenance(registry, "tq_breakout", cycle_id=None,
                                            asof_day=DAY)
        self.assertFalse(refused.is_authoritative)
        self.assertIn("explicit cycle id", refused.detail)
        with self.assertRaises(ValueError):
            SP.run_provenance_key(scope=SP.SCOPE_CYCLE, subject="x", asof_day=DAY)
        # 正对照
        ok = SP.StrategySelectionProvenance(
            strategy_id="tq_breakout", strategy_version=1, strategy_checksum="a" * 64,
            asof_day=DAY, scope=SP.SCOPE_CYCLE, cycle_id=3)
        self.assertEqual(ok.cycle_id, 3)


# ---------------------------------------------------------------------------
# SP-11 / SP-12 / SP-13 / SP-14 / SP-16 —— ledger 侧
# ---------------------------------------------------------------------------


class LedgerProvenanceTests(unittest.TestCase):
    """真实 ``PT.init_db()`` schema：signal 的 cycle 归属与不可变性。"""

    ACCOUNT = "tq_breakout"

    def setUp(self):
        # 生产模块用 ``with sqlite3.connect(...)``（提交但不 close），Windows 上
        # 句柄会活到进程结束 —— 隔离目录的清理必须容忍这一点，否则测试会因为
        # 「临时目录删不掉」而不是断言失败而报错。
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self._patches = []
        import paper_trading as PT
        self.PT = PT
        for patcher in (mock.patch.object(PT, "DB_PATH", self.path),
                        mock.patch.object(PT, "_benchmark_close", return_value=None),
                        mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True)):
            patcher.start()
            self._patches.append(patcher)
        self.addCleanup(self._stop)
        PT.init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.cycle_id = int(self.conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id=?", (self.ACCOUNT,)
        ).fetchone()[0])

    def _stop(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def stamp(self):
        return self.PT._strategy_stamp(self.conn, self.ACCOUNT)

    def test_SP11_signal_carries_the_pinned_immutable_stamp(self):
        """SP-11：signal 的 stamp 与 cycle pin 一致，且**不**重新查 current head。

        含 DB 层约束（原 SP-12 的语义）：signal 的 cycle 归属是 write-time fact，
        NULL / 幽灵 cycle 被拒绝，写入后不可更改。
        """
        pinned = SR.cycle_stamp_for_account(self.conn, self.ACCOUNT,
                                            cycle_id=self.cycle_id)
        self.assertIsNotNone(pinned, "cycle 上必须有 pin")
        cycle_id, stamp = self.PT._cycle_signal_provenance(self.conn, self.ACCOUNT)
        self.assertEqual(cycle_id, self.cycle_id)
        self.assertEqual(tuple(stamp), tuple(pinned))
        # 把 current head 升级：signal 的写入器**不得**跟随
        import strategy_registry as _SR
        version = _SR.save_definition(self.conn, self.ACCOUNT, {"name": "SP-11 改名"},
                                     actor="sp-matrix", change_note="v2")
        self.conn.commit()
        self.assertGreater(version.version, pinned[1])
        cycle_id2, stamp2 = self.PT._cycle_signal_provenance(self.conn, self.ACCOUNT)
        self.assertEqual(stamp2, tuple(pinned), "signal 写入器跟随了 current head")
        self.assertEqual(cycle_id2, self.cycle_id)

        # DB 层：NULL / 幽灵 cycle 被拒绝，写入后不可更改。
        stamp = self.stamp()
        base = dict(
            account_id=self.ACCOUNT, signal_date=DAY, intended_date=DAY, code="600901",
            name="测试", payload="{}", status="pending", reason="", created_at="2026-09-07T15:00:00",
            strategy_id=stamp[0], strategy_version=stamp[1], strategy_checksum=stamp[2])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
                "payload,status,reason,created_at,strategy_id,strategy_version,"
                "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                tuple(base.values()))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
                "payload,status,reason,created_at,strategy_id,strategy_version,"
                "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(base.values()) + (99999,))
        self.conn.execute(
            "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
            "payload,status,reason,created_at,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(base.values()) + (self.cycle_id,))
        self.conn.commit()
        row = self.conn.execute("SELECT cycle_id FROM paper_signals").fetchone()
        self.assertEqual(row["cycle_id"], self.cycle_id)
        # 一经写入不得更改
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE paper_signals SET cycle_id=? WHERE code='600901'",
                              (self.cycle_id + 1,))

    def test_SP12_order_stamp_is_inherited_from_its_signal(self):
        """SP-12：order 的 provenance 必须来自 signal 的因果戳，不得重查 current head。

        关键是**跨周期**才看得出差别：signal 属于 cycle A，账户已经搬到 cycle B。
        从 signal 继承得到 A 的不可变版本；若写入器改问「账户现在属于谁」就会拿到
        B 的版本 —— 于是「哪一版策略做的这个决定」被后一次换周期静默改写。
        """
        import strategy_registry as _SR
        cycle_a = self.cycle_id
        # cycle A 上的 signal
        self.conn.execute("BEGIN")
        sig_stamp = self.PT._strategy_stamp(self.conn, self.ACCOUNT)
        cur = self.conn.execute(
            "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
            "payload,status,reason,created_at,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) "
            "VALUES(?,?,?,?,?,'{}','pending','',"
            "'2026-09-07T15:00:00',?,?,?,?)",
            (self.ACCOUNT, DAY, DAY, "600901", "测试") + tuple(sig_stamp) + (cycle_a,))
        signal_id = int(cur.lastrowid)
        self.conn.commit()

        # 账户搬到 cycle B，且 B 上 pin 的是**另一个**不可变版本。
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
            "updated_at) VALUES('sp-12-b','running',1000000,'shared-risk',"
            "datetime('now'),datetime('now'))")
        cycle_b = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        self.assertNotEqual(cycle_b, cycle_a)
        v2 = _SR.save_definition(self.conn, self.ACCOUNT, {"name": "SP-12 v2"},
                                 actor="sp-matrix", change_note="v2")
        self.conn.execute(
            "INSERT INTO paper_cycle_strategy_versions(cycle_id,account_id,strategy_id,"
            "strategy_version,strategy_checksum,bound_at) VALUES(?,?,?,?,?,datetime('now'))",
            (cycle_b, self.ACCOUNT, self.ACCOUNT, v2.version, v2.checksum))
        self.conn.execute("UPDATE paper_accounts SET cycle_id=? WHERE id=?",
                          (cycle_b, self.ACCOUNT))
        self.conn.commit()

        account_stamp = self.PT._strategy_stamp(self.conn, self.ACCOUNT)
        inherited = self.PT._strategy_stamp(self.conn, self.ACCOUNT, signal_id)
        self.assertEqual(tuple(inherited), tuple(sig_stamp),
                         "order 写入戳没有继承 signal 的因果戳")
        self.assertEqual(inherited[1], sig_stamp[1])
        # 正对照：不带 signal 时确实会拿到 cycle B 的另一个版本（说明差异真实存在）
        self.assertNotEqual(account_stamp[1], sig_stamp[1])

    def test_SP11b_signal_writer_fails_closed_without_a_cycle_pin(self):
        """SP-11b：cycle 上没有 pin → 拒绝写入，**不**回退 legacy / current head。

        没有这一条，「只认 cycle pin」与「愿意接受任何可解析的戳」这两种实现无法
        区分 —— 夹具里 cycle 恰好有 pin 时它们的输出一模一样。
        """
        pinned = SR.cycle_stamp_for_account(self.conn, self.ACCOUNT,
                                            cycle_id=self.cycle_id)
        self.assertIsNotNone(pinned)
        # 卸下 pin：现在唯一可用的退路是 legacy binding / current head。
        self.conn.execute(
            "DELETE FROM paper_cycle_strategy_versions WHERE cycle_id=? AND account_id=?",
            (self.cycle_id, self.ACCOUNT))
        self.conn.commit()
        legacy = self.conn.execute(
            "SELECT strategy_version FROM paper_strategy_legacy_bindings WHERE account_id=?",
            (self.ACCOUNT,)).fetchone()
        self.assertIsNotNone(legacy, "前提：legacy binding 存在，才谈得上「回退」")
        with self.assertRaises(self.PT.SignalCycleUnprovable) as caught:
            self.PT._cycle_signal_provenance(self.conn, self.ACCOUNT)
        self.assertEqual(caught.exception.cycle_id, self.cycle_id)
        self.assertIn("no pinned immutable strategy version", caught.exception.detail)

        # 账户自己**没有** durable cycle 时同样必须拒绝：不得顺手采纳「当前
        # active cycle」（规格 C4 明确禁止 paper_accounts.cycle_id / active cycle
        # 作为 fallback）。这里保证账本里**确实**存在一个 running cycle，否则
        # 「拒绝」可以是因为无处可退，而不是因为拒绝采纳。
        self.conn.execute("UPDATE paper_cycles SET status='running' WHERE id=?",
                          (self.cycle_id,))
        self.conn.commit()
        running = self.conn.execute(
            "SELECT id FROM paper_cycles WHERE status='running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(running, "前提：账本里有一个 active cycle 可供误采纳")
        self.conn.execute("UPDATE paper_accounts SET cycle_id=NULL WHERE id=?",
                          (self.ACCOUNT,))
        self.conn.commit()
        with self.assertRaises(self.PT.SignalCycleUnprovable) as orphan:
            self.PT._cycle_signal_provenance(self.conn, self.ACCOUNT)
        self.assertIsNone(orphan.exception.cycle_id,
                          "signal 的周期归属从 active cycle 推导出来了")
        self.assertIn("no durable cycle", orphan.exception.detail)
        self.conn.execute("UPDATE paper_accounts SET cycle_id=? WHERE id=?",
                          (self.cycle_id, self.ACCOUNT))
        self.conn.commit()

        # 正对照：pin 放回去后同一个调用必须成功
        self.conn.execute(
            "INSERT INTO paper_cycle_strategy_versions(cycle_id,account_id,strategy_id,"
            "strategy_version,strategy_checksum,bound_at) VALUES(?,?,?,?,?,datetime('now'))",
            (self.cycle_id, self.ACCOUNT) + tuple(pinned))
        self.conn.commit()
        cycle_id, stamp = self.PT._cycle_signal_provenance(self.conn, self.ACCOUNT)
        self.assertEqual(cycle_id, self.cycle_id)
        self.assertEqual(tuple(stamp), tuple(pinned))

    def test_SP13_archive_copy_preserves_full_provenance(self):
        """SP-13：归档整行拷贝保留完整 provenance（列数与顺序严格一致）。"""
        stamp = self.stamp()
        self.conn.execute(
            "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
            "payload,status,reason,created_at,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,'{}','pending','','2026-09-07T15:00:00',"
            "?,?,?,?)", (self.ACCOUNT, DAY, DAY, "600901", "测试") + tuple(stamp)
            + (self.cycle_id,))
        self.conn.commit()
        live_cols = _columns(self.conn, "paper_signals")
        arch_cols = _columns(self.conn, "paper_signals_archive")
        self.assertEqual(live_cols, arch_cols, "整行拷贝要求列与顺序严格一致")
        self.conn.execute("INSERT OR IGNORE INTO paper_signals_archive SELECT * FROM paper_signals")
        self.conn.commit()
        moved = dict(self.conn.execute(
            "SELECT * FROM paper_signals_archive WHERE code='600901'").fetchone())
        self.assertEqual(
            (moved["strategy_id"], moved["strategy_version"], moved["strategy_checksum"],
             moved["cycle_id"]), tuple(stamp) + (self.cycle_id,))

    def test_SP14_provenance_survives_db_reopen(self):
        """SP-14：重新打开数据库后 provenance 仍可解析（不依赖进程内缓存）。"""
        stamp = self.stamp()
        self.conn.execute(
            "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
            "payload,status,reason,created_at,strategy_id,strategy_version,"
            "strategy_checksum,cycle_id) VALUES(?,?,?,?,?,'{}','pending','','2026-09-07T15:00:00',"
            "?,?,?,?)", (self.ACCOUNT, DAY, DAY, "600901", "测试") + tuple(stamp)
            + (self.cycle_id,))
        self.conn.commit()
        self.conn.close()
        reopened = sqlite3.connect(self.path)
        reopened.row_factory = sqlite3.Row
        self.addCleanup(reopened.close)
        row = dict(reopened.execute(
            "SELECT * FROM paper_signals WHERE code='600901'").fetchone())
        with _closing(sqlite3.connect(self.path, timeout=20), sqlite3.Row) as registry:
            reading = SRES.cycle_provenance(
                registry, row["strategy_id"], cycle_id=row["cycle_id"], asof_day=DAY)
        self.assertTrue(reading.is_authoritative)
        self.assertEqual(reading.require().strategy_version, stamp[1])
        self.assertEqual(reading.require().strategy_checksum, stamp[2])

    def test_SP16_legacy_null_provenance_reads_without_raising(self):
        """SP-16：legacy NULL provenance 不抛异常（四种 row 形状都要读得出来）。"""
        rows = [
            {"strategy_id": "x", "strategy_version": None, "strategy_checksum": None,
             "asof_day": None, "scope": None, "cycle_id": None},
            None,
            ("x", None, None, None, None, None),
        ]
        for row in rows:
            reading = SP.reading_from_row(row)
            self.assertFalse(reading.is_authoritative)
            self.assertIn(reading.status,
                          (SP.STATUS_LEGACY_UNPROVEN, SP.STATUS_NOT_APPLICABLE,
                           SP.STATUS_UNKNOWN))
        # sqlite3.Row 形状（既不是 Mapping 也不是属性对象）
        row = self.conn.execute(
            "SELECT NULL AS strategy_id, NULL AS strategy_version, NULL AS strategy_checksum,"
            " NULL AS asof_day, NULL AS scope, NULL AS cycle_id").fetchone()
        reading = SP.reading_from_row(row)
        self.assertEqual(reading.status, SP.STATUS_NOT_APPLICABLE)

    def test_SP15_same_version_number_does_not_cross_resolve(self):
        """SP-15：A v1 / B v1 不能只按 version number 解析（必须带 strategy_id+checksum）。"""
        with _closing(sqlite3.connect(self.path, timeout=20), sqlite3.Row) as registry:
            a = SR.get_version("tq_breakout", conn=registry)
            b = SR.get_version("trend_pullback", conn=registry)
            self.assertEqual(a.version, b.version, "前提：两条策略的 v1 同号")
            self.assertNotEqual(a.checksum, b.checksum)
            # 交叉校验：A 的 (id, version) 配 B 的 checksum 必须被拒绝
            self.assertFalse(SRES.verify_immutable(
                registry, SP.StrategySelectionProvenance(
                    strategy_id="tq_breakout", strategy_version=a.version,
                    strategy_checksum=b.checksum, asof_day=DAY,
                    scope=SP.SCOPE_RESEARCH)))
            self.assertTrue(SRES.verify_immutable(
                registry, SP.StrategySelectionProvenance(
                    strategy_id="tq_breakout", strategy_version=a.version,
                    strategy_checksum=a.checksum, asof_day=DAY,
                    scope=SP.SCOPE_RESEARCH)))


# ---------------------------------------------------------------------------
# 契约单元（canonical / as-of）
# ---------------------------------------------------------------------------


class ContractUnitTests(unittest.TestCase):
    def test_canonical_rejects_non_canonical_inputs(self):
        for bad in (None, 0, -1, "0", "latest", "current", "abc"):
            self.assertIsNone(SP.canonical_version(bad), f"version {bad!r} 被接受")
        for bad in (None, "", "ZZZ", "a" * 63, "a" * 65, "g" * 64):
            self.assertIsNone(SP.canonical_checksum(bad), f"checksum {bad!r} 被接受")
        # 大小写不是「不同的 checksum」：canonical 形式是 64 位小写 hex
        self.assertEqual(SP.canonical_checksum("A" * 64), "a" * 64)
        self.assertEqual(SP.canonical_version("3"), 3)
        self.assertIsNone(SP.canonical_day(""))
        self.assertIsNone(SP.canonical_day("2026-13-40"))
        self.assertEqual(SP.canonical_day(dt.date(2026, 9, 7)), DAY)

    def test_asof_resolution_refuses_to_guess(self):
        self.assertEqual(SP.resolve_asof_day(DAY), DAY)
        self.assertEqual(SP.resolve_asof_day(None, [("p", DAY), ("q", DAY)]), DAY)
        with self.assertRaises(SP.AsOfUnprovable):
            SP.resolve_asof_day(None, [])
        with self.assertRaises(SP.AsOfUnprovable):
            SP.resolve_asof_day(None, [("p", DAY), ("q", OLD_DAY)])
        # 显式给出但不可解析：错误，不是「当作没给」
        with self.assertRaises(SP.AsOfUnprovable):
            SP.resolve_asof_day("not-a-date")
        # reference_date（目标交易日）不是 factor as-of 的候选
        self.assertEqual(
            SP.declared_asof_candidates(
                {"data_quality": {"reference_date": DAY, "complete_cutoff": OLD_DAY},
                 "picks": []}),
            [("data_quality.complete_cutoff", OLD_DAY)])

    def test_reading_from_row_refuses_silent_upgrades(self):
        complete = {"strategy_id": "tq_breakout", "strategy_version": 1,
                    "strategy_checksum": "a" * 64, "asof_day": DAY,
                    "scope": SP.SCOPE_RESEARCH, "cycle_id": None}
        self.assertTrue(SP.reading_from_row(complete).is_authoritative)
        # 声称 verified 但字段不全 → unknown，而不是 verified
        claimed = dict(complete, strategy_checksum=None,
                       provenance_status=SP.STATUS_VERIFIED)
        reading = SP.reading_from_row(claimed)
        self.assertEqual(reading.status, SP.STATUS_UNKNOWN)
        self.assertFalse(reading.is_authoritative)
        # 声称 legacy 但字段完整 → unknown，而不是 legacy
        lied = dict(complete, provenance_status=SP.STATUS_LEGACY_UNPROVEN)
        self.assertEqual(SP.reading_from_row(lied).status, SP.STATUS_UNKNOWN)

    def test_contract_is_frozen_and_validates_scope(self):
        with self.assertRaises(ValueError):
            SP.StrategySelectionProvenance(
                strategy_id="", strategy_version=1, strategy_checksum="a" * 64,
                asof_day=DAY, scope=SP.SCOPE_RESEARCH)
        with self.assertRaises(ValueError):
            SP.StrategySelectionProvenance(
                strategy_id="x", strategy_version=1, strategy_checksum="a" * 64,
                asof_day=DAY, scope="nonsense")
        contract = SP.StrategySelectionProvenance(
            strategy_id=" tq_breakout ", strategy_version="2",
            strategy_checksum=("A" * 64), asof_day="2026-09-07T15:00:00",
            scope=SP.SCOPE_RESEARCH)
        self.assertEqual(contract.strategy_id, "tq_breakout")
        self.assertEqual(contract.strategy_version, 2)
        self.assertEqual(contract.strategy_checksum, "a" * 64)
        self.assertEqual(contract.asof_day, DAY)
        with self.assertRaises(AttributeError):
            contract.strategy_version = 3  # frozen dataclass

    def test_non_verified_reading_must_not_carry_a_contract(self):
        contract = SP.StrategySelectionProvenance(
            strategy_id="x", strategy_version=1, strategy_checksum="a" * 64,
            asof_day=DAY, scope=SP.SCOPE_RESEARCH)
        with self.assertRaises(ValueError):
            SP.ProvenanceReading(contract, SP.STATUS_UNKNOWN)
        with self.assertRaises(ValueError):
            SP.ProvenanceReading(None, SP.STATUS_VERIFIED)


class FamilyBProvenanceTests(_IsolatedStudy):
    """family B：模型族永远 not_applicable，as-of 必须一次性固定。"""

    def test_model_family_is_not_applicable_never_fabricated(self):
        ST.record_run(_payload(day=DAY), run_date=DAY, source="scheduled")
        with self._research() as conn:
            row = dict(conn.execute(
                "SELECT * FROM selection_runs WHERE run_date=? AND strategy='three_day'",
                (DAY,)).fetchone())
        self.assertEqual(row["provenance_status"], SP.STATUS_NOT_APPLICABLE)
        self.assertIsNone(row["strategy_id"])
        self.assertIsNone(row["strategy_version"])
        self.assertIsNone(row["strategy_checksum"])
        self.assertEqual(row["asof_day"], DAY)
        self.assertEqual(row["scope"], SP.SCOPE_RESEARCH)
        self.assertIsNone(row["cycle_id"])
        reading = SRES.reading_from_run(row, default_scope=SP.SCOPE_RESEARCH)
        self.assertEqual(reading.status, SP.STATUS_NOT_APPLICABLE)
        self.assertFalse(reading.is_authoritative)

    def test_historical_run_date_is_immutable_before_any_write(self):
        ST.record_run(_payload(day=OLD_DAY), run_date=OLD_DAY, source="scheduled")
        with self._research() as conn:
            before = dict(conn.execute(
                "SELECT * FROM selection_runs WHERE run_date=? AND strategy='three_day'",
                (OLD_DAY,)).fetchone())
        changed = dict(_payload(day=OLD_DAY))
        changed["universe_size"] = 4242
        result = ST.record_run(changed, run_date=OLD_DAY, source="manual")
        self.assertEqual(result["status"], "skipped")
        with self._research() as conn:
            after = dict(conn.execute(
                "SELECT * FROM selection_runs WHERE run_date=? AND strategy='three_day'",
                (OLD_DAY,)).fetchone())
        for key in ("result_json", "universe_size", "source", "generated_at"):
            self.assertEqual(before[key], after[key], f"历史 {key} 被改写了")
        # 正对照：**不传** run_date 的当前 run 不是历史，必须正常写入
        live = ST.record_run(_payload(day=dt.date.today().isoformat()),
                             source="manual")
        self.assertNotEqual(live.get("status"), "skipped")

    def test_missing_asof_stays_unknown_instead_of_today(self):
        payload = {"strategy": "three_day", "picks": [], "data_quality": {}}
        ST.record_run(payload, run_date=DAY, source="manual", asof_day=None)
        with self._research() as conn:
            row = dict(conn.execute(
                "SELECT * FROM selection_runs WHERE run_date=? AND strategy='three_day'",
                (DAY,)).fetchone())
        self.assertEqual(row["provenance_status"], SP.STATUS_UNKNOWN)
        self.assertIsNone(row["asof_day"], "as-of 被 today 兜底了")
        self.assertNotEqual(row["asof_day"], DAY)


class ContractModulePurityTests(unittest.TestCase):
    """契约模块的纯度：不 import 生产模块、不读时钟/环境。"""

    def test_contract_module_has_no_project_imports(self):
        backend = os.path.dirname(os.path.abspath(__file__))
        raw = open(os.path.join(backend, "strategy_selection_provenance.py"),
                   encoding="utf-8").read()
        tree = ast.parse(raw)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        import sys as _sys
        stdlib = set(_sys.stdlib_module_names)
        self.assertEqual(sorted(roots - stdlib), [],
                         "纯契约模块 import 了非 stdlib 模块")

    def test_contract_module_never_touches_clock_or_env(self):
        backend = os.path.dirname(os.path.abspath(__file__))
        raw = open(os.path.join(backend, "strategy_selection_provenance.py"),
                   encoding="utf-8").read()
        tree = ast.parse(raw)
        # 逐个属性/名字比对，而不是扫子串：``canonical_day`` 里出现 "date."
        # 这类匹配会让子串断言变成「永远为真」的假门禁。
        forbidden_attrs = {"today", "now", "utcnow", "getenv", "environ", "connect"}
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                offenders.append(node.attr)
            elif isinstance(node, ast.Name) and node.id in {"environ", "getenv"}:
                offenders.append(node.id)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"sqlite3", "os"}:
                        offenders.append(alias.name)
            elif (isinstance(node, ast.ImportFrom) and node.module
                    and node.module.split(".")[0] in {"sqlite3", "os"}):
                offenders.append(node.module)
        self.assertEqual(offenders, [], f"纯契约模块触碰了时钟/环境/DB：{offenders}")

    def test_module_import_is_side_effect_free(self):
        module = importlib.reload(SP)
        self.assertEqual(module.SCOPES, (module.SCOPE_CYCLE, module.SCOPE_RESEARCH))
        self.assertEqual(module.AUTHORITATIVE_STATUSES, frozenset({module.STATUS_VERIFIED}))


class AsOfIsolationTests(_IsolatedStudy):
    """C7 的永久化：D+1 的证据不得进入 D 的读取。"""

    def test_reading_a_day_never_sees_the_next_day(self):
        PS.run_daily(topn=5, run_date=OLD_DAY)
        old_ids = {row["id"] for row in self.runs(strategy_id=None, day=OLD_DAY)}
        before = [dict(r) for r in self.runs(strategy_id=None, day=OLD_DAY)]
        self.assertTrue(old_ids)
        PS.run_daily(topn=5, run_date=DAY)
        # D 日的 run 集合与内容不因 D+1 的运行而改变
        self.assertEqual({row["id"] for row in self.runs(strategy_id=None, day=OLD_DAY)},
                         old_ids)
        self.assertEqual([dict(r) for r in self.runs(strategy_id=None, day=OLD_DAY)], before)
        view = PS.latest(trade_date=OLD_DAY)
        self.assertEqual(view["trade_date"], OLD_DAY)
        seen = {run["run_id"] for group in view["strategies"] for run in group["runs"]}
        self.assertTrue(seen)
        self.assertEqual(seen, old_ids, "D 日的读取看到了 D+1 的 run（或漏掉自己的）")
        day_ids = {row["id"] for row in self.runs(strategy_id=None, day=DAY)}
        self.assertFalse(seen.intersection(day_ids))

    def test_latest_defaults_to_the_most_recent_run_day(self):
        """正对照：``latest()`` 的默认日必须是最近一次运行日，不是「今天」。"""
        PS.run_daily(topn=5, run_date=OLD_DAY)
        self.assertEqual(PS.latest()["trade_date"], OLD_DAY)
        PS.run_daily(topn=5, run_date=DAY)
        self.assertEqual(PS.latest()["trade_date"], DAY)
        self.assertEqual(
            {row["id"] for row in self.runs(strategy_id=None, day=DAY)},
            {run["run_id"] for group in PS.latest()["strategies"] for run in group["runs"]})
        self.assertEqual(PS.latest(trade_date=DAY)["trade_date"], DAY)


# ---------------------------------------------------------------------------
# R23 review round —— 迁移重建顺序 / 中断恢复 / 冲突刷新（3 条 review findings）
# ---------------------------------------------------------------------------

#: 升级前的 family B 结构。子表**必须**带 ``REFERENCES``：finding 1 的全部内容
#: 就是 SQLite 会跟着 ``RENAME`` 改写子表的 FK 目标。用一份不带 FK 的 DDL 来测，
#: 会把这条 finding 测成永远绿色的假测试。
LEGACY_TRACKING_RUNS = """
CREATE TABLE selection_runs (
    id INTEGER PRIMARY KEY,
    run_date TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    data_asof_date TEXT,
    benchmark_entry_price REAL,
    universe_size INTEGER,
    candidate_count INTEGER,
    selected_count INTEGER,
    executable_count INTEGER,
    source TEXT NOT NULL,
    result_json TEXT NOT NULL,
    UNIQUE(run_date, strategy)
)
"""

LEGACY_TRACKING_PICKS = """
CREATE TABLE selection_picks (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES selection_runs(id) ON DELETE CASCADE,
    rank_no INTEGER NOT NULL,
    code TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
)
"""


class _LegacyTracking(unittest.TestCase):
    """把 family B 库造成升级前的形状（含真实 FK 与一行历史 run）。"""

    RUN_ID = 7
    PICK_ID = 1

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "selection_tracking.db")
        self.conn = sqlite3.connect(self.path, timeout=20)
        self.addCleanup(self.conn.close)
        self.conn.executescript(LEGACY_TRACKING_RUNS + ";" + LEGACY_TRACKING_PICKS + ";")
        self.conn.execute(
            "INSERT INTO selection_runs(id, run_date, generated_at, strategy,"
            " strategy_name, source, result_json)"
            " VALUES(?,?,?,?,?,?,?)",
            (self.RUN_ID, "2026-09-01", "2026-09-01T15:05:00", "trend_pullback",
             "趋势波段", "scheduled", "{}"),
        )
        self.conn.execute(
            "INSERT INTO selection_picks(id, run_id, rank_no, code, snapshot_json)"
            " VALUES(?,?,?,?,?)",
            (self.PICK_ID, self.RUN_ID, 1, "600000", "{}"),
        )
        self.conn.commit()

    def fk_target(self, table="selection_picks"):
        sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        return sql.split("REFERENCES")[1].split("(")[0].strip() if "REFERENCES" in sql else None

    def migrate(self):
        """调用真实 ``ensure_schema``，而不是单独调 ``_migrate_runs``。

        迁移顺序（外键开关、guard 安装）本身就是 finding 1 的一部分，绕过
        ``ensure_schema`` 就等于把被测对象换成了另一个较短的路径。
        """
        old = ST.DB_PATH
        ST.DB_PATH = self.path
        try:
            ST.ensure_schema()
        finally:
            ST.DB_PATH = old


class RunTableRebuildTests(_LegacyTracking):
    def test_RF01_rebuild_keeps_child_fk_pointing_at_selection_runs(self):
        """finding 1：重建后子表 FK 必须仍指向 ``selection_runs``。

        回归的是真实故障：``RENAME selection_runs TO selection_runs_legacy`` 会被
        SQLite 传播到子表，``DROP TABLE selection_runs_legacy`` 之后子表 schema
        指向不存在的表 —— 插入 pick 报
        ``no such table: main.selection_runs_legacy``。
        """
        self.migrate()
        self.conn.close()
        self.conn = sqlite3.connect(self.path, timeout=20)
        self.addCleanup(self.conn.close)
        # 正对照：DDL 里确实有 REFERENCES，否则本测试什么也没测。
        self.assertEqual(self.fk_target(), "selection_runs",
                         "重建后子表 FK 指向了别的表（finding 1 复发）")
        self.assertTrue(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='selection_runs'"
            ).fetchone(), "重建后 selection_runs 不存在")
        # 历史数据未被 CASCADE 带走。
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM selection_picks").fetchone()[0],
            1, "重建把 picks 丢了（ON DELETE CASCADE 被触发）")
        # 真正的验收：picks 仍可写入，且必须通过 FK 校验。
        self.conn.execute("PRAGMA foreign_keys = ON")
        run_id = self.conn.execute("SELECT id FROM selection_runs").fetchone()[0]
        self.conn.execute(
            "INSERT INTO selection_picks(run_id, rank_no, code, snapshot_json)"
            " VALUES(?,?,?,?)", (run_id, 2, "600001", "{}"))
        self.conn.commit()

    def test_RF02_interrupted_rebuild_does_not_discard_the_only_copy(self):
        """finding 2：中断在「改名之后、拷贝之前」时，legacy 表是唯一副本。

        旧实现下次启动无条件 ``DROP TABLE selection_runs_legacy``，历史直接消失。
        现在必须先回收再删。
        """
        # 模拟中断：只执行旧实现的第一条语句。
        self.conn.execute("ALTER TABLE selection_runs RENAME TO selection_runs_legacy")
        self.conn.commit()
        rows_before = self.conn.execute(
            "SELECT COUNT(*) FROM selection_runs_legacy").fetchone()[0]
        self.assertEqual(rows_before, 1)
        self.conn.close()

        self.migrate()

        self.conn.close()
        self.conn = sqlite3.connect(self.path, timeout=20)
        self.addCleanup(self.conn.close)
        rows_after = self.conn.execute(
            "SELECT COUNT(*) FROM selection_runs").fetchone()[0]
        self.assertEqual(rows_after, rows_before,
                         "中断恢复把历史 runs 丢了（finding 2 复发）")
        self.assertFalse(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='selection_runs_legacy'").fetchone(),
            "legacy 副本应已回收并删除")
        # 历史行必须保留可读身份，而不是被 NULL 掉。
        row = self.conn.execute(
            "SELECT run_date, strategy, provenance_status FROM selection_runs").fetchone()
        self.assertEqual(row[0], "2026-09-01")
        self.assertEqual(row[1], "trend_pullback")
        self.assertEqual(row[2], SP.STATUS_LEGACY_UNPROVEN,
                         "回收来的历史行必须显式声明为 legacy_unproven")

    def test_RF03_rebuild_is_idempotent_and_leaves_no_legacy_tables(self):
        """正对照：正常路径跑两次不产生 legacy 残留，也不重复插入。"""
        self.migrate()
        self.migrate()
        self.conn.close()
        self.conn = sqlite3.connect(self.path, timeout=20)
        self.addCleanup(self.conn.close)
        leftovers = [
            row[0] for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name LIKE 'selection_runs%'")
        ]
        self.assertEqual(sorted(leftovers), ["selection_runs"],
                         f"重建留下了临时表：{leftovers}")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM selection_runs").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM selection_picks").fetchone()[0], 1)


class SignalRefreshTests(LedgerProvenanceTests):
    """finding 3：升级后的首次盘中刷新必须能刷新**升级前就存在**的 signal。

    这类 signal 的 provenance 列诚实地是 NULL，而 v23 与既有的 stamp trigger 都
    禁止把 NULL 改成值 —— 所以刷新语句不能把不可变列放进 ``DO UPDATE SET``。
    """

    def _legacy_signal(self, code="600901"):
        """写入一行「升级前」形状的 signal：**stamp 真实、cycle_id 未知**。

        finding 3 描述的正是这个形状 —— 升级前写入的 signal 早于 v23 加列，所以
        ``cycle_id`` 诚实地是 NULL；而 stamp 列当时就已存在并有值。行是在 guard
        安装**之前**写进去的，所以按仓库既有写法临时卸下 guard 再装回
        （见 ``test_deferred_fill_cycle_binding``），而不是放宽 guard。
        """
        import paper_schema_migrations as PSM
        stamp = self.PT._strategy_stamp(self.conn, self.ACCOUNT)
        self.conn.execute(
            "DROP TRIGGER IF EXISTS trg_paper_signals_cycle_provenance_insert")
        try:
            self.conn.execute(
                "INSERT INTO paper_signals(account_id, signal_date, intended_date, code,"
                " name, close_price, rank_score, payload, status, reason, created_at,"
                " strategy_id, strategy_version, strategy_checksum, cycle_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (self.ACCOUNT, DAY, DAY, code, "旧信号", 10.0, 1.0, "{}", "pending",
                 "升级前写入", "2026-09-07T09:35:00", *stamp),
            )
        finally:
            PSM._ensure_signal_cycle_provenance_guards(self.conn)
        self.conn.commit()

    def _upsert(self):
        """抽取**生产**的 ``INSERT ... ON CONFLICT`` 语句。

        手抄一份 SQL 会在生产语句改动后继续通过 —— 那正是探针失明的成因。
        """
        src = (Path(__file__).resolve().parent / "paper_trading.py").read_text(
            encoding="utf-8")
        start = src.index('"""INSERT INTO paper_signals(\n')
        end = src.index('"""', start + 3)
        stmt = src[start + 3:end]
        self.assertIn("ON CONFLICT(account_id,signal_date,code)", stmt)
        head = stmt.split(")", 1)[0].split("(", 1)[1]
        return stmt, [part.strip() for part in head.split(",")]

    def test_RF04_bootstrap_refresh_updates_a_pre_upgrade_signal(self):
        """finding 3：升级后的首次刷新**不得**抛异常，且必须保留原 provenance。"""
        self._legacy_signal()
        before = self.conn.execute(
            "SELECT strategy_id, strategy_version, strategy_checksum, cycle_id"
            " FROM paper_signals WHERE account_id=? AND code=?",
            (self.ACCOUNT, "600901")).fetchone()
        self.assertTrue(before["strategy_id"], "夹具的 stamp 必须真实")
        self.assertIsNone(before["cycle_id"], "夹具的 cycle_id 必须未知（finding 3 的形状）")
        stmt, order = self._upsert()
        # 生产环境的账号 stamp 来自 cycle pin，所以「升级后的刷新」带来的 stamp 与
        # 既有行**相同** —— 这条 finding 只可能是 cycle_id 一列被改写（review 描述
        # 的形状）。stamp 列本身也在 SET 里，但值相同，所以只有 cycle 那条 trigger
        # 会 abort。
        stamp = self.stamp()
        values = {
            "account_id": self.ACCOUNT, "signal_date": DAY, "intended_date": DAY,
            "code": "600901", "name": "新信号", "industry": "银行",
            "close_price": 11.0, "rank_score": 2.0, "t_tier": "A", "t_score": 1.5,
            "payload": "{}", "status": "ready", "reason": "刷新", "created_at": "t2",
            "strategy_id": stamp[0], "strategy_version": stamp[1],
            "strategy_checksum": stamp[2], "cycle_id": self.cycle_id,
        }
        self.assertEqual(set(order), set(values),
                         f"探针列集合与生产语句不一致：{set(order) ^ set(values)}")
        # 不能抛 IntegrityError —— 那正是 finding 3 的真实故障。
        self.conn.execute(stmt, tuple(values[column] for column in order))
        self.conn.commit()
        row = self.conn.execute(
            "SELECT status, name, strategy_id, cycle_id FROM paper_signals"
            " WHERE account_id=? AND code=?", (self.ACCOUNT, "600901"),
        ).fetchone()
        # 决策字段被刷新（证明确实走了 UPDATE 分支，不是插了一行新的）
        self.assertEqual(row["name"], "新信号")
        self.assertEqual(row["status"], "ready")
        # 不可变 provenance 保持原样：NULL 就是 NULL，绝不回填。
        self.assertEqual(row["strategy_id"], before["strategy_id"],
                         "刷新语句回填/改写了不可变的 strategy stamp")
        self.assertIsNone(row["cycle_id"],
                          "刷新语句回填了不可变的 cycle 归属（finding 3 复发）")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM paper_signals WHERE account_id=? AND code=?",
                (self.ACCOUNT, "600901")).fetchone()[0],
            1, "刷新兴建了第二行，而不是更新既有行")

    def test_RF05_provenance_columns_are_absent_from_the_conflict_update(self):
        """静态：``DO UPDATE SET`` 不得包含任何不可变 provenance 列。

        快照断言在「第一次刷新恰好成功」时会漏掉这条，静态断言则无论 fixtures
        如何都能挡住 —— 两条互补，缺一不可。
        """
        stmt, _order = self._upsert()
        set_clause = stmt.split("DO UPDATE SET", 1)[1]
        for column in ("cycle_id", "strategy_id", "strategy_version",
                       "strategy_checksum"):
            self.assertNotIn(f"{column}=excluded.{column}", set_clause,
                             f"刷新语句仍会改写不可变列 {column}")


if __name__ == "__main__":
    unittest.main()
