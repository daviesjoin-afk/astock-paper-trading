# -*- coding: utf-8 -*-
"""``paper_risk_scan_state`` 的契约矩阵（R16）。

R16 defect
----------
风险扫描的幂等性此前由 ``paper_audit`` 里一条 ``event='risk_scan_state'`` 的
JSON 标记决定，而那条标记的 key **只有机器分钟**：

* 同分钟翻周期 ⇒ 新周期读到旧周期的 completed 标记，直接 ``already_scanned``
  —— 新周期持仓一次风控都没跑；
* 同分钟不同 asof 的 replay 互相抑制；
* wrapper 与 impl 各自取一次时钟，跨分钟边界留下 orphan running。

本文件把修复后的契约钉死：

    RS-01  fresh claim → running / attempt=1
    RS-02  同身份 running 不重复认领
    RS-03  同身份 completed 不重复认领
    RS-04  failed 身份可重试
    RS-05  重试 attempt 递增，且清空 finished_at / error
    RS-06  同分钟不同 cycle 相互独立（本 PR 最核心 contract）
    RS-07  同分钟不同 asof 相互独立
    RS-08  completion 只认 exact identity（CAS），否则 raise
    RS-09  failure 只认 exact identity（CAS），否则 raise
    RS-10  身份（cycle_id / asof_date / scan_minute）不可更改
    RS-11  非法 / 不存在的 cycle 被拒绝
    RS-12  绝不从旧 paper_audit 标记回填
    RS-13  模块不拥有事务（无 commit / rollback / BEGIN / SAVEPOINT）
    RS-14  模块不读 wall clock（无 date.today / datetime.now / time.time）
    RS-15  adaptive DB 绝不出现本表

全部纯 fixture：临时 SQLite，不访问网络、不 sleep、不读系统时钟（时间戳一律
由测试显式传入）。
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import paper_risk_scan_state as PRSS  # noqa: E402
import paper_schema_migrations as PSM  # noqa: E402

MODULE_PATH = os.path.join(BACKEND_DIR, "paper_risk_scan_state.py")

CYCLE_A = 1
CYCLE_B = 2
ASOF = "2026-09-10"
ASOF_OTHER = "2026-09-11"
MINUTE = "2026-09-10 14:50"
MINUTE_OTHER = "2026-09-10 14:51"
STARTED = "2026-09-10 14:50:05"
FINISHED = "2026-09-10 14:50:40"


def _module_source():
    with open(MODULE_PATH, encoding="utf-8") as fh:
        return fh.read()


def _call_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


class RiskScanStateTestCase(unittest.TestCase):
    """临时 SQLite + 两个真实周期。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "paper.sqlite3")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, cycle_key TEXT, status TEXT)"
        )
        for cid in (CYCLE_A, CYCLE_B):
            self.conn.execute(
                "INSERT INTO paper_cycles(id,cycle_key,status) VALUES(?,?,'running')",
                (cid, f"c{cid}"),
            )
        PSM.ensure_risk_scan_run_state(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _claim(self, *, cycle_id=CYCLE_A, asof_date=ASOF, scan_minute=MINUTE,
               started_at=STARTED):
        return PRSS.claim_scan(
            self.conn, cycle_id=cycle_id, asof_date=asof_date,
            scan_minute=scan_minute, started_at=started_at,
        )

    def _complete(self, *, cycle_id=CYCLE_A, asof_date=ASOF, scan_minute=MINUTE):
        return PRSS.complete_scan(
            self.conn, cycle_id=cycle_id, asof_date=asof_date,
            scan_minute=scan_minute, finished_at=FINISHED,
        )

    def _fail(self, *, cycle_id=CYCLE_A, asof_date=ASOF, scan_minute=MINUTE,
              error="boom"):
        return PRSS.fail_scan(
            self.conn, cycle_id=cycle_id, asof_date=asof_date,
            scan_minute=scan_minute, finished_at=FINISHED, error=error,
        )

    def _rows(self):
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM {PRSS.SCAN_RUN_TABLE} ORDER BY id").fetchall()]


class ClaimLifecycleTests(RiskScanStateTestCase):
    """RS-01 … RS-05 —— claim / suppress / retry 的完整状态机。"""

    def test_rs01_fresh_claim_starts_running(self):
        result = self._claim()
        self.assertTrue(result["claimed"])
        self.assertEqual(result["state"], "claimed")
        self.assertEqual(result["attempt"], 1)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "running")
        self.assertEqual(int(rows[0]["attempt"]), 1)
        self.assertIsNone(rows[0]["finished_at"])
        self.assertIsNone(rows[0]["error"])

    def test_rs02_running_identity_is_not_reclaimed(self):
        self._claim()
        second = self._claim(started_at="2026-09-10 14:50:20")
        self.assertFalse(second["claimed"])
        self.assertEqual(second["state"], "running")
        self.assertEqual(len(self._rows()), 1, "running 身份被重复插入")
        self.assertEqual(int(self._rows()[0]["attempt"]), 1)

    def test_rs03_completed_identity_is_not_reclaimed(self):
        self._claim()
        self._complete()
        second = self._claim(started_at="2026-09-10 14:50:50")
        self.assertFalse(second["claimed"])
        self.assertEqual(second["state"], "completed")
        self.assertEqual(len(self._rows()), 1)

    def test_rs04_failed_identity_is_retryable(self):
        self._claim()
        self._fail(error="provider down")
        retry = self._claim(started_at="2026-09-10 14:50:55")
        self.assertTrue(retry["claimed"], "failed 身份必须可重试")
        self.assertEqual(retry["state"], "retry")

    def test_rs05_retry_increments_attempt_and_clears_failure(self):
        self._claim()
        self._fail(error="provider down")
        self._claim(started_at="2026-09-10 14:50:55")
        row = self._rows()[0]
        self.assertEqual(int(row["attempt"]), 2, "重试未递增 attempt")
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["finished_at"], "重试未清空 finished_at")
        self.assertIsNone(row["error"], "重试未清空 error")
        self.assertEqual(row["started_at"], "2026-09-10 14:50:55")
        # 第二次失败 → 第三次重试 attempt=3
        self._fail(error="down again")
        third = self._claim(started_at="2026-09-10 14:51:30")
        self.assertEqual(third["attempt"], 3)


class IdentityScopeTests(RiskScanStateTestCase):
    """RS-06 / RS-07 —— 身份的判别力（本 PR 最核心 contract）。"""

    def test_rs06_different_cycle_same_minute_is_independent(self):
        first = self._claim(cycle_id=CYCLE_A)
        self._complete(cycle_id=CYCLE_A)
        # 同一 runtime 分钟，但周期不同 ⇒ 必须是**另一个**业务扫描
        second = self._claim(cycle_id=CYCLE_B)
        self.assertTrue(second["claimed"], "同分钟不同 cycle 被错误抑制")
        self._complete(cycle_id=CYCLE_B)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(int(r["cycle_id"]) for r in rows), [CYCLE_A, CYCLE_B])
        self.assertTrue(all(r["status"] == "completed" for r in rows))
        self.assertTrue(first["claimed"])

    def test_rs07_different_asof_same_minute_is_independent(self):
        self._claim(asof_date=ASOF)
        self._complete(asof_date=ASOF)
        second = self._claim(asof_date=ASOF_OTHER)
        self.assertTrue(second["claimed"], "同分钟不同 asof 被错误抑制")
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r["asof_date"] for r in rows), sorted([ASOF, ASOF_OTHER]))

    def test_rs07b_different_minute_same_cycle_is_independent(self):
        self._claim(scan_minute=MINUTE)
        self._complete(scan_minute=MINUTE)
        second = self._claim(scan_minute=MINUTE_OTHER)
        self.assertTrue(second["claimed"])
        self.assertEqual(len(self._rows()), 2)


class ExactIdentityCastests(RiskScanStateTestCase):
    """RS-08 / RS-09 —— 状态转换必须精确命中唯一一行。"""

    def test_rs08_completion_requires_exact_identity(self):
        self._claim(cycle_id=CYCLE_A, asof_date=ASOF, scan_minute=MINUTE)
        # 错 cycle
        with self.assertRaises(PRSS.RiskScanStateConflict):
            self._complete(cycle_id=CYCLE_B)
        # 错 asof
        with self.assertRaises(PRSS.RiskScanStateConflict):
            self._complete(asof_date=ASOF_OTHER)
        # 错 minute
        with self.assertRaises(PRSS.RiskScanStateConflict):
            self._complete(scan_minute=MINUTE_OTHER)
        # 原身份仍然 running（错误的 CAS 没有副作用）
        self.assertEqual(self._rows()[0]["status"], "running")
        self._complete()
        self.assertEqual(self._rows()[0]["status"], "completed")

    def test_rs08b_completion_twice_is_a_conflict(self):
        self._claim()
        self._complete()
        with self.assertRaises(PRSS.RiskScanStateConflict):
            self._complete()

    def test_rs09_failure_requires_exact_identity(self):
        self._claim(cycle_id=CYCLE_A)
        with self.assertRaises(PRSS.RiskScanStateConflict):
            self._fail(cycle_id=CYCLE_B)
        self.assertEqual(len(self._rows()), 1, "失败的 CAS 伪造了新行")
        self.assertEqual(self._rows()[0]["status"], "running")
        self._fail(error="boom")
        self.assertEqual(self._rows()[0]["status"], "failed")

    def test_rs09b_failure_never_inserts_a_new_row(self):
        """失败绝不能 INSERT 一条新行来"假装"状态已经转换。"""
        self._claim()
        self._complete()
        with self.assertRaises(PRSS.RiskScanStateConflict):
            self._fail()
        self.assertEqual(len(self._rows()), 1)


class IdentityImmutabilityTests(RiskScanStateTestCase):
    """RS-10 —— 身份一经写入不可更改。"""

    def test_rs10_identity_columns_are_immutable(self):
        self._claim(cycle_id=CYCLE_A, asof_date=ASOF, scan_minute=MINUTE)
        for column, value in (("cycle_id", CYCLE_B), ("asof_date", ASOF_OTHER),
                              ("scan_minute", MINUTE_OTHER)):
            with self.subTest(column=column):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(
                        f"UPDATE {PRSS.SCAN_RUN_TABLE} SET {column}=? WHERE cycle_id=?",
                        (value, CYCLE_A),
                    )
        row = self._rows()[0]
        self.assertEqual(int(row["cycle_id"]), CYCLE_A)
        self.assertEqual(row["asof_date"], ASOF)
        self.assertEqual(row["scan_minute"], MINUTE)

    def test_rs10b_mutable_columns_still_update(self):
        """status / attempt / finished_at / error / detail 必须仍可推进。"""
        self._claim()
        self._complete()
        row = self._rows()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["finished_at"], FINISHED)


class InvalidCycleTests(RiskScanStateTestCase):
    """RS-11 —— 不存在的周期不得成为扫描归属。"""

    def test_rs11_nonexistent_cycle_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._claim(cycle_id=999)
        self.assertEqual(self._rows(), [], "非法周期仍然写入了 scan run")

    def test_rs11b_missing_identity_parts_fail_fast(self):
        with self.assertRaises(ValueError):
            PRSS.claim_scan(self.conn, cycle_id=None, asof_date=ASOF,
                            scan_minute=MINUTE, started_at=STARTED)
        with self.assertRaises(ValueError):
            PRSS.claim_scan(self.conn, cycle_id=CYCLE_A, asof_date="",
                            scan_minute=MINUTE, started_at=STARTED)
        with self.assertRaises(ValueError):
            PRSS.claim_scan(self.conn, cycle_id=CYCLE_A, asof_date=ASOF,
                            scan_minute="  ", started_at=STARTED)
        with self.assertRaises(ValueError):
            PRSS.claim_scan(self.conn, cycle_id=CYCLE_A, asof_date=ASOF,
                            scan_minute=MINUTE, started_at="")
        self.assertEqual(self._rows(), [])

    def test_rs11c_asof_and_minute_are_required_keyword_only(self):
        with self.assertRaises(TypeError):
            PRSS.claim_scan(self.conn, CYCLE_A, ASOF, MINUTE, STARTED)


class NoAuditBackfillTests(RiskScanStateTestCase):
    """RS-12 —— 绝不从旧 paper_audit 标记回填。"""

    def test_rs12_migration_leaves_new_table_empty(self):
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS paper_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " account_id TEXT, event TEXT, detail TEXT, created_at TEXT)"
        )
        self.conn.execute(
            "INSERT INTO paper_audit(event,detail,created_at) VALUES"
            "('risk_scan_state','{\"scan_minute\": \"2026-09-10 14:50\","
            " \"status\": \"completed\"}','2026-09-10 14:50:10')"
        )
        self.conn.commit()
        PSM.ensure_risk_scan_run_state(self.conn)
        self.conn.commit()
        self.assertEqual(
            self._rows(), [],
            "migration 从旧 audit 标记回填了 scan run —— 历史 cycle 归属是未知的，"
            "任何推算都是把'不知道'洗白成'知道'",
        )
        legacy = self.conn.execute(
            "SELECT COUNT(*) FROM paper_audit WHERE event='risk_scan_state'").fetchone()[0]
        self.assertEqual(legacy, 1, "旧 audit 标记被改写")

    def test_rs12b_idempotent_second_migration(self):
        PSM.ensure_risk_scan_run_state(self.conn)
        self._claim()
        PSM.ensure_risk_scan_run_state(self.conn)
        self.conn.commit()
        self.assertEqual(len(self._rows()), 1, "重复 migration 破坏了已有行")


class ModuleBoundaryTests(unittest.TestCase):
    """RS-13 / RS-14 —— 模块自身的静态硬边界。"""

    def setUp(self):
        self.source = _module_source()
        self.tree = ast.parse(self.source)
        # 去掉模块 docstring：事务/时钟 token 的**唯一**合法出现处是说明文字，
        # 全部集中在模块头 docstring 里。检查只针对实际代码，否则说明文字会误报。
        first = self.tree.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            self.source = "\n".join(self.source.splitlines()[first.end_lineno:])

    def test_rs13_module_owns_no_transaction(self):
        leaked = sorted(_call_names(self.tree) & {"commit", "rollback"})
        self.assertEqual(
            leaked, [],
            f"paper_risk_scan_state 出现了事务调用 {leaked}：事务必须由调用方"
            "（paper_trading.monitor_risk）拥有",
        )
        for token in ("BEGIN", "SAVEPOINT", "RELEASE SAVEPOINT", "COMMIT", "ROLLBACK"):
            with self.subTest(token=token):
                self.assertNotIn(
                    token, self.source,
                    f"paper_risk_scan_state 出现了事务语句 {token}",
                )

    def test_rs14_module_reads_no_wall_clock(self):
        leaked = sorted(_call_names(self.tree) & {"today", "now", "time", "utcnow"})
        self.assertEqual(
            leaked, [],
            f"paper_risk_scan_state 读取了系统时钟 {leaked}：身份与时间戳只能由"
            "调用方显式传入，否则身份会自己漂移",
        )

    def test_rs14b_module_does_not_import_paper_trading(self):
        roots = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertNotIn(
            "paper_trading", roots,
            "依赖方向必须单向：paper_trading → paper_risk_scan_state，反向禁止",
        )


class DualDatabaseTests(unittest.TestCase):
    """RS-15 —— 本表只属于 paper trading DB。"""

    def test_rs15_adaptive_db_never_receives_the_table(self):
        tmp = tempfile.mkdtemp()
        try:
            import db_migrate
            import sqlite3 as _sq

            adaptive_path = os.path.join(tmp, "adaptive_learning.sqlite3")
            _sq.connect(adaptive_path).close()  # migrate 对不存在的文件会跳过
            db_migrate.migrate("adaptive_learning", apply=True, path=adaptive_path,
                               backup=False)
            conn = _sq.connect(adaptive_path)
            try:
                present = bool(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (PRSS.SCAN_RUN_TABLE,),
                ).fetchone())
            finally:
                conn.close()
            self.assertFalse(
                present,
                f"{PRSS.SCAN_RUN_TABLE} 出现在 adaptive_learning.sqlite3 —— "
                "风险扫描运行状态是 paper ledger 事实，不能跨库污染",
            )

            paper_path = os.path.join(tmp, "paper_trading.sqlite3")
            _sq.connect(paper_path).close()
            db_migrate.migrate("paper_trading", apply=True, path=paper_path, backup=False)
            conn = _sq.connect(paper_path)
            try:
                present = bool(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (PRSS.SCAN_RUN_TABLE,),
                ).fetchone())
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_version WHERE db_name='paper_trading'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertTrue(present, "paper trading DB 缺少 paper_risk_scan_runs")
            self.assertGreaterEqual(int(version), 21)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class CycleFenceTests(RiskScanStateTestCase):
    """``assert_cycle_active`` —— 外部 I/O 之后的周期 fence。"""

    def test_fence_passes_for_current_cycle(self):
        self.assertEqual(PRSS.assert_cycle_active(self.conn, cycle_id=CYCLE_B), CYCLE_B)

    def test_fence_raises_when_cycle_moved_on(self):
        self.conn.execute("UPDATE paper_cycles SET status='archived' WHERE id=?", (CYCLE_B,))
        self.conn.commit()
        with self.assertRaises(PRSS.RiskScanCycleChanged):
            PRSS.assert_cycle_active(self.conn, cycle_id=CYCLE_B)

    def test_fence_raises_when_no_active_cycle(self):
        self.conn.execute("UPDATE paper_cycles SET status='archived'")
        self.conn.commit()
        with self.assertRaises(PRSS.RiskScanCycleChanged):
            PRSS.assert_cycle_active(self.conn, cycle_id=CYCLE_A)

    def test_fence_never_falls_back(self):
        """绝不返回 None 让调用方自行解释，也绝不改用新周期。"""
        self.conn.execute("UPDATE paper_cycles SET status='archived' WHERE id=?", (CYCLE_A,))
        self.conn.commit()
        with self.assertRaises(PRSS.RiskScanCycleChanged):
            PRSS.assert_cycle_active(self.conn, cycle_id=CYCLE_A)
        with self.assertRaises(PRSS.RiskScanCycleChanged):
            PRSS.assert_cycle_active(self.conn, cycle_id=None)

    def test_fence_matches_read_model_semantics(self):
        """判据必须与 PPRM.active_cycle_id 完全一致（同一条 SQL）。"""
        import paper_position_read_model as PPRM
        self.assertEqual(
            PRSS.assert_cycle_active(self.conn, cycle_id=CYCLE_B),
            PPRM.active_cycle_id(self.conn),
        )

    def test_fence_missing_table_is_not_provable(self):
        conn = sqlite3.connect(":memory:")
        with self.assertRaises(PRSS.RiskScanCycleChanged):
            PRSS.assert_cycle_active(conn, cycle_id=CYCLE_A)
        conn.close()


class ScanRunReadTests(RiskScanStateTestCase):
    """``scan_run`` 只读辅助。"""

    def test_scan_run_returns_none_when_absent(self):
        self.assertIsNone(PRSS.scan_run(self.conn, cycle_id=CYCLE_A, asof_date=ASOF,
                                        scan_minute=MINUTE))

    def test_scan_run_returns_row(self):
        self._claim()
        row = PRSS.scan_run(self.conn, cycle_id=CYCLE_A, asof_date=ASOF, scan_minute=MINUTE)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["detail"], "{}")


class SchemaContractTests(RiskScanStateTestCase):
    """DDL 契约：唯一身份 + 状态 CHECK + 列序。"""

    def test_unique_identity_includes_cycle_and_asof(self):
        columns = PSM._unique_index_columns(self.conn, PRSS.SCAN_RUN_TABLE)
        self.assertEqual(sorted(columns), sorted(["cycle_id", "asof_date", "scan_minute"]))

    def test_columns_match_declared_order(self):
        actual = [r["name"] for r in self.conn.execute(
            f"PRAGMA table_info({PRSS.SCAN_RUN_TABLE})").fetchall()]
        self.assertEqual(tuple(actual), PSM.RISK_SCAN_RUN_COLUMNS)

    def test_status_check_rejects_unknown_value(self):
        self._claim()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                f"UPDATE {PRSS.SCAN_RUN_TABLE} SET status='paused' WHERE cycle_id=?",
                (CYCLE_A,),
            )

    def test_statuses_are_shared_with_migration(self):
        self.assertEqual(tuple(PRSS.SCAN_STATUSES), PSM.RISK_SCAN_RUN_STATUSES)


if __name__ == "__main__":
    unittest.main()
