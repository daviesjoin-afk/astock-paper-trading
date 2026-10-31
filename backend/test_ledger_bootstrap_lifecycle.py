# -*- coding: utf-8 -*-
"""PR-6 专项：账本结构 bootstrap 与运行期恢复的职责边界。

覆盖三件事，全部驱动**真实** ``paper_trading.init_db``：

1. 结构 bootstrap：新库建表、旧库升级、缺失表/列修复、失败回滚、中断可恢复；
2. 运行期恢复：结构已 ready 也照跑（有效租约不动、过期租约回收、signal 与订单
   对账），不会因为"结构完成了"而跳过业务恢复；
3. 并发与隔离：两个进程同时初始化、API 只读与 worker 初始化并发、切换
   ``DB_PATH`` 不继承上一个库的初始化结论、失败时 fail closed。

所有断言都落在**行为**上（表/列是否存在、业务行是否被改、收据是否写入），
不依赖私有实现细节。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import paper_ledger_bootstrap as PLB  # noqa: E402
import paper_schema_migrations as PSM  # noqa: E402
import paper_trading as PT  # noqa: E402

ACCOUNT_ID = "trend_pullback"
CODE = "600001"


def _stamp(now):
    return now.strftime("%Y-%m-%d %H:%M:%S")


class LedgerBootstrapFixture(unittest.TestCase):
    """公共夹具：每个用例一块独立 SQLite 文件；不碰 checkout 里的真实账本。

    只放 setUp 与辅助方法 —— 专项场景由下面的子类各自声明，避免同一批用例被
    重复执行。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astock-pr6-")
        self.old_db_path = PT.DB_PATH
        PT.DB_PATH = os.path.join(self.tmp, "paper.sqlite3")
        self.addCleanup(setattr, PT, "DB_PATH", self.old_db_path)
        # 初始化不该为了基准价去读（或拉取）CSV/行情：用例只测结构与恢复边界。
        benchmark = mock.patch.object(PT, "_benchmark_close", return_value=None)
        benchmark.start()
        self.addCleanup(benchmark.stop)

    # ---------- 基础工具 ----------

    def _init(self):
        """跑一次 init_db，并单独拿它的公开返回值做契约断言。"""
        result = PT.init_db()
        self.assertIsNone(result, "init_db() 的公开契约是返回 None")
        return result

    def _init_path(self):
        """跑一次 init_db，并从 schema bootstrap owner 的编排结果里读路径。

        ``init_db()`` 本身只返回 ``None``（拆分前的公开契约），所以 cold/verified
        只能从 owner 的返回值或真实结构状态观察，不能依赖公开返回值。
        """
        captured = {}
        real = PLB.bootstrap_schema

        def spy(conn, *, fresh):
            outcome = real(conn, fresh=fresh)
            captured["path"] = outcome["path"]
            return outcome

        with mock.patch.object(PLB, "bootstrap_schema", side_effect=spy):
            self._init()
        return captured["path"]

    def _db(self):
        return PT._db(immediate=True)

    def _table_names(self):
        with PT._db() as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                ).fetchall()
            }

    def _columns(self, table):
        with PT._db() as conn:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    def _receipt(self):
        with PT._db() as conn:
            return PSM.read_bootstrap_receipt(conn)

    def _account_cycle(self):
        with PT._db() as conn:
            row = conn.execute(
                "SELECT id,cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT_ID,)
            ).fetchone()
            return dict(row)

    def _insert_verified_fill(self):
        """一笔"有流水证据"的已验证成交；作为"不得被初始化改写"的事实。"""
        with self._db() as conn:
            cycle_id = conn.execute(
                "SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT_ID,)
            ).fetchone()[0]
            stamp = PT._strategy_stamp(conn, ACCOUNT_ID)
            now = _stamp(datetime.now())
            order_id = conn.execute(
                """INSERT INTO paper_orders(
                       account_id,side,code,name,qty,planned_price,filled_price,amount,fees,
                       status,reason,risk_payload,realized_pnl,created_at,executed_at,
                       execution_status,execution_verified,
                       strategy_id,strategy_version,strategy_checksum,cycle_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ACCOUNT_ID, "buy", CODE, "专项标的", 1000, 10.0, 10.0, 10000.0, 6.0,
                 "filled", "pr6-fixture", "{}", 0.0, now, now,
                 "verified", 1, *stamp, cycle_id),
            ).lastrowid
            conn.execute(
                """INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,
                                           amount,fees,fill_date,quote_at,assumption)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (order_id, ACCOUNT_ID, "buy", CODE, 1000, 10.0, 10000.0, 6.0,
                 now[:10], now, "pr6"),
            )
        return order_id

class LedgerBootstrapLifecycleTestCase(LedgerBootstrapFixture):
    """PR-6 主场景：结构 bootstrap 与运行期恢复的边界。"""

    # ---------- 1. 全新空库 ----------

    def test_fresh_database_bootstraps_full_structure(self):
        self.assertEqual(self._init_path(), "cold")
        tables = self._table_names()
        for required in PSM.REQUIRED_TABLES:
            self.assertIn(required, tables, f"新库缺少必需表 {required}")
        # 业务引导仍然发生：账户 + 周期都有行，且账户绑定到当前周期。
        account = self._account_cycle()
        self.assertIsNotNone(account["cycle_id"])
        # 升级成功的收据必须存在，且记录的就是当前结构。
        receipt = self._receipt()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["contract_version"], PSM.BOOTSTRAP_CONTRACT_VERSION)
        with PT._db() as conn:
            self.assertEqual(receipt["structure_fingerprint"], PSM.structure_fingerprint(conn))

    # ---------- 2. 已升级库重复初始化 ----------

    def test_repeat_init_takes_verified_path_without_ddl_but_keeps_recovery(self):
        self._init()
        recovery = mock.patch.object(PT, "_recover_ledger_runtime", wraps=PT._recover_ledger_runtime)
        structure = mock.patch.object(PLB, "ensure_ledger_structure", wraps=PLB.ensure_ledger_structure)
        with recovery as recovery_spy, structure as structure_spy:
            path = self._init_path()
        self.assertEqual(path, "verified")
        structure_spy.assert_not_called()   # 不重复 DDL
        recovery_spy.assert_called_once()   # 但业务恢复照跑

    # ---------- 3. 缺表/缺列的旧库 ----------

    def test_legacy_database_missing_feature_table_and_column_is_repaired(self):
        self._init()
        with self._db() as conn:
            conn.execute("DROP TABLE paper_position_risk_state")
            conn.execute("ALTER TABLE paper_nav DROP COLUMN quote_status")
        self.assertNotIn("paper_position_risk_state", self._table_names())
        self.assertNotIn("quote_status", self._columns("paper_nav"))

        self.assertEqual(self._init_path(), "cold")
        self.assertIn("paper_position_risk_state", self._table_names())
        self.assertIn("quote_status", self._columns("paper_nav"))
        # 修完之后收据要跟着新结构更新，否则下一次又会全量重建。
        self.assertEqual(self._init_path(), "verified")

    # ---------- 4. 中途失败 + 5/11. 失败不落收据、fail closed ----------

    def test_interrupted_bootstrap_leaves_no_receipt_and_recovers_next_run(self):
        with mock.patch.object(
            PSM, "ensure_feature_tables", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self._init()
        # 事务整体回滚：没有半成品结构，也没有"已完成"的收据。
        self.assertNotIn("paper_accounts", self._table_names())
        self.assertNotIn(PSM.BOOTSTRAP_RECEIPT_TABLE, self._table_names())
        self.assertIsNone(self._receipt())

        self.assertEqual(self._init_path(), "cold")
        self.assertIn("paper_accounts", self._table_names())
        self.assertIsNotNone(self._receipt())

    def test_verification_failure_aborts_initialization(self):
        error = PSM.LedgerSchemaBootstrapError("结构缺失")
        with mock.patch.object(PSM, "verify_required_structure", side_effect=error):
            with self.assertRaises(PSM.LedgerSchemaBootstrapError):
                self._init()
        self.assertIsNone(self._receipt())

    # ---------- 5/6. 并发 ----------

    def _spawn_initializers(self, count, db_dir):
        script = os.path.join(self.tmp, "init_worker.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(
                f"""
                import os, sys, time
                sys.path.insert(0, {BACKEND_DIR!r})
                import paper_trading as PT
                PT._benchmark_close = lambda: None
                go = os.environ["PR6_GO"]
                while not os.path.exists(go):
                    time.sleep(0.01)
                PT.init_db()
                print("ok")
                """
            ))
        env = dict(os.environ)
        env["ASTOCK_DATA_DIR"] = db_dir
        env["PR6_GO"] = os.path.join(self.tmp, "go")
        env["PYTHONIOENCODING"] = "utf-8"
        procs = [
            subprocess.Popen([sys.executable, script], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(count)
        ]
        time.sleep(0.2)
        open(env["PR6_GO"], "w").close()
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            results.append((proc.returncode, out.decode("utf-8", "replace"),
                            err.decode("utf-8", "replace")))
        return results

    def test_two_processes_initializing_the_same_ledger(self):
        db_dir = os.path.join(self.tmp, "shared")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "paper_trading.sqlite3")
        PT.DB_PATH = db_path

        results = self._spawn_initializers(2, db_dir)

        for code, out, err in results:
            self.assertEqual(code, 0, f"初始化进程失败: {out} {err}")
        tables = self._table_names()
        for required in PSM.REQUIRED_TABLES:
            self.assertIn(required, tables)
        with PT._db() as conn:
            receipt = PSM.read_bootstrap_receipt(conn)
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt["structure_fingerprint"], PSM.structure_fingerprint(conn))

    def test_readonly_api_access_survives_concurrent_initialization(self):
        self._init()
        failures = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    with PT._db_readonly() as conn:
                        conn.execute("SELECT count(*) FROM paper_orders").fetchone()
            except Exception as exc:  # pragma: no cover - 只在失败时触发
                failures.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for _ in range(5):
                self._init()
        finally:
            stop.set()
            thread.join(timeout=30)
        self.assertEqual(failures, [])

    # ---------- 7/8. 租约 ----------

    def _insert_lease(self, *, expired):
        now = datetime.now()
        expires = now - timedelta(minutes=5) if expired else now + timedelta(minutes=30)
        with self._db() as conn:
            conn.execute(
                """INSERT INTO paper_jobs(
                       slot,market_date,status,detail,started_at,owner_key,
                       heartbeat_at,expires_at,fencing_token)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("risk", "2026-10-31", "running", "{}", _stamp(now - timedelta(minutes=5)),
                 "worker-1", _stamp(now - timedelta(minutes=4)), _stamp(expires), 3),
            )
            conn.execute(
                """INSERT INTO paper_runtime_locks(
                       lock_key,owner_key,slot,acquired_at,heartbeat_at,expires_at,fencing_token)
                   VALUES(?,?,?,?,?,?,?)""",
                ("risk:2026-10-31", "worker-1", "risk", _stamp(now - timedelta(minutes=5)),
                 _stamp(now - timedelta(minutes=4)), _stamp(expires), 3),
            )

    def test_valid_lease_is_not_reset_by_a_new_process(self):
        self._init()
        self._insert_lease(expired=False)

        self._init()

        with PT._db() as conn:
            job = conn.execute(
                "SELECT status,owner_key,fencing_token FROM paper_jobs WHERE slot='risk'"
            ).fetchone()
            lock = conn.execute(
                "SELECT count(*) FROM paper_runtime_locks WHERE lock_key='risk:2026-10-31'"
            ).fetchone()[0]
        self.assertEqual(dict(job), {"status": "running", "owner_key": "worker-1",
                                     "fencing_token": 3})
        self.assertEqual(lock, 1)

    def test_expired_lease_is_recovered_by_the_existing_rule(self):
        self._init()
        self._insert_lease(expired=True)

        self._init()

        with PT._db() as conn:
            job = conn.execute(
                "SELECT status,detail FROM paper_jobs WHERE slot='risk'"
            ).fetchone()
            lock = conn.execute(
                "SELECT count(*) FROM paper_runtime_locks WHERE lock_key='risk:2026-10-31'"
            ).fetchone()[0]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(json.loads(job["detail"])["recovered_from_running"], 1)
        self.assertEqual(lock, 0)

    # ---------- 9. 订单 / signal 对账 ----------

    def test_signal_order_drift_is_still_reconciled_when_schema_is_ready(self):
        self._init()
        self.assertEqual(self._init_path(), "verified")  # 结构已 ready
        with self._db() as conn:
            cycle_id = conn.execute(
                "SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT_ID,)
            ).fetchone()[0]
            now = _stamp(datetime.now())
            stamp = PT._strategy_stamp(conn, ACCOUNT_ID)
            signal_id = conn.execute(
                """INSERT INTO paper_signals(
                       account_id,signal_date,intended_date,code,payload,status,created_at,
                       strategy_id,strategy_version,strategy_checksum,cycle_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (ACCOUNT_ID, now[:10], now[:10], CODE, "{}", "superseded", now,
                 *stamp, cycle_id),
            ).lastrowid
            order_id = conn.execute(
                """INSERT INTO paper_orders(
                       account_id,side,code,name,qty,planned_price,amount,fees,status,
                       reason,risk_payload,created_at,signal_id,
                       strategy_id,strategy_version,strategy_checksum,cycle_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ACCOUNT_ID, "buy", CODE, "专项标的", 100, 10.0, 1000.0, 0.6,
                 "pending_limit", "pr6-drift", "{}", now, signal_id, *stamp, cycle_id),
            ).lastrowid
            conn.execute(
                """INSERT INTO paper_capital_reservations(
                       order_key,account_id,cycle_id,code,side,amount,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (str(order_id), ACCOUNT_ID, cycle_id, CODE, "buy", 1000.0, "reserved", now),
            )

        self._init()

        with PT._db() as conn:
            order = conn.execute(
                "SELECT status,reason FROM paper_orders WHERE id=?", (order_id,)
            ).fetchone()
            reservation = conn.execute(
                "SELECT status FROM paper_capital_reservations WHERE order_key=?",
                (str(order_id),),
            ).fetchone()
        self.assertEqual(order["status"], "superseded")
        self.assertIn("信号已替代", order["reason"])
        self.assertEqual(reservation["status"], "released")

    # ---------- 10. 换 DB_PATH ----------

    def test_switching_db_path_does_not_inherit_readiness(self):
        self._init()
        other = os.path.join(self.tmp, "other.sqlite3")
        PT.DB_PATH = other

        self.assertEqual(self._init_path(), "cold")
        self.assertIn("paper_accounts", self._table_names())
        self.assertIsNotNone(self._receipt())

    # ---------- 12. 已验证成交不被改写 ----------

    def test_repeated_initialization_preserves_verified_fills(self):
        self._init()
        order_id = self._insert_verified_fill()

        def snapshot():
            with PT._db() as conn:
                order = dict(conn.execute(
                    "SELECT * FROM paper_orders WHERE id=?", (order_id,)).fetchone())
                fill = dict(conn.execute(
                    "SELECT * FROM paper_fills WHERE order_id=?", (order_id,)).fetchone())
                return order, fill

        before = snapshot()
        self._init()
        self._init()
        after = snapshot()
        self.assertEqual(before, after)


# ── 修复项一：WAL checkpoint 只跟随真正的结构初始化/升级 ──────────────────────


class WalCheckpointScopeTests(LedgerBootstrapFixture):
    """verified 路径不得主动发起 ``PRAGMA wal_checkpoint(TRUNCATE)``。

    master 的健康账本快路径没有这个行为；拆分后必须保持。checkpoint 还必须留在
    事务之外（``paper_db`` 已提交并关闭连接），不能进 schema/恢复事务内部。
    """

    def _spy_checkpoint(self):
        calls = []
        patcher = mock.patch.object(
            PT, "_wal_checkpoint", side_effect=lambda: calls.append(1)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_fresh_initialization_triggers_checkpoint_once(self):
        calls = self._spy_checkpoint()
        self.assertEqual(self._init_path(), "cold")
        self.assertEqual(len(calls), 1, "新库初始化应在提交后收缩一次 WAL")

    def test_cold_upgrade_triggers_checkpoint_once(self):
        self._init_path()                       # 先把健康账本建好
        calls = self._spy_checkpoint()
        with self._db() as conn:
            conn.execute("DROP TABLE paper_position_risk_state")
        self.assertEqual(self._init_path(), "cold")
        self.assertEqual(len(calls), 1, "结构修复路径应保留原有 checkpoint")

    def test_verified_repeat_does_not_trigger_checkpoint(self):
        self._init_path()
        calls = self._spy_checkpoint()
        self.assertEqual(self._init_path(), "verified")
        self.assertEqual(calls, [], "verified 路径不得主动 checkpoint")

    def test_verified_init_does_not_wait_on_an_open_read_transaction(self):
        """有持续只读事务时，verified 初始化不该因为 TRUNCATE checkpoint 额外等待。"""
        self._init_path()
        calls = self._spy_checkpoint()
        reader = sqlite3.connect(PT.DB_PATH, timeout=10)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM paper_orders").fetchone()
            started = time.perf_counter()
            self.assertEqual(self._init_path(), "verified")
            elapsed = time.perf_counter() - started
        finally:
            reader.rollback()
            reader.close()
        self.assertEqual(calls, [])
        self.assertLess(elapsed, 10.0, "verified 初始化不应被 checkpoint 拖住")


# ── 修复项二：源码指纹不可用时 fail closed ──────────────────────────────────


class OwnerFingerprintFailClosedTests(LedgerBootstrapFixture):
    """取不到任一 owner 源码时：不写收据、不认旧收据、不进入 verified。"""

    def _no_spec(self):
        return mock.patch.object(importlib.util, "find_spec", return_value=None)

    def test_missing_owner_source_raises_with_reason(self):
        with self._no_spec():
            with self.assertRaises(PSM.LedgerSchemaBootstrapError) as ctx:
                PSM.schema_owner_fingerprint()
        message = str(ctx.exception)
        self.assertIn("paper_schema_migrations", message)
        self.assertIn("源码", message)

    def test_invalid_owner_source_path_raises(self):
        missing = os.path.join(self.tmp, "not-there.py")
        spec = types.SimpleNamespace(origin=missing)
        with mock.patch.object(importlib.util, "find_spec", return_value=spec):
            with self.assertRaises(PSM.LedgerSchemaBootstrapError) as ctx:
                PSM.schema_owner_fingerprint()
        self.assertIn("not-there.py", str(ctx.exception))

    def test_unavailable_fingerprint_fails_closed_without_receipt(self):
        with self._no_spec():
            with self.assertRaises(PSM.LedgerSchemaBootstrapError):
                self._init()
        # 失败不能产生新的有效收据
        self.assertNotIn(PSM.BOOTSTRAP_RECEIPT_TABLE, self._table_names())

    def test_existing_receipt_is_not_confirmed_when_fingerprint_unavailable(self):
        self._init_path()
        before = self._receipt()
        with self._no_spec():
            with self.assertRaises(PSM.LedgerSchemaBootstrapError):
                self._init()
        # 既有收据原样保留，既没有被"确认"，也没有被伪造成新值
        self.assertEqual(self._receipt(), before)

    def test_legacy_unavailable_sentinel_receipt_is_rejected(self):
        self._init_path()
        with self._db() as conn:
            conn.execute(
                "UPDATE ledger_schema_receipt SET owner_fingerprint=?"
                " WHERE id=1",
                ("unavailable:strategy_champion",),
            )
        self.assertEqual(self._init_path(), "cold", "旧式哨兵收据不得被接受")
        receipt = self._receipt()
        self.assertTrue(PSM.is_valid_owner_fingerprint(receipt["owner_fingerprint"]))
        self.assertEqual(self._init_path(), "verified")

    def test_owner_fingerprint_change_forces_cold(self):
        self._init_path()
        with mock.patch.object(PSM, "schema_owner_fingerprint", return_value="a" * 64):
            self.assertEqual(self._init_path(), "cold")
        # 收据里现在是那份假指纹，真实指纹仍不匹配 -> 再冷一次后才收敛
        self.assertEqual(self._init_path(), "cold")
        self.assertEqual(self._init_path(), "verified")

    def test_fingerprint_guard_rejects_non_sha256_values(self):
        for value in ("", "unavailable:paper_storage", "a" * 63, "A" * 64, None):
            with self.subTest(value=value):
                self.assertFalse(PSM.is_valid_owner_fingerprint(value))
        self.assertTrue(PSM.is_valid_owner_fingerprint("a" * 64))


# ── 修复项三：init_db 公开返回值 ────────────────────────────────────────────


class PublicReturnContractTests(LedgerBootstrapFixture):
    """``init_db()`` 与拆分前一致返回 ``None``；cold/verified 只是 owner 内部结果。"""

    def test_init_db_returns_none_on_both_paths(self):
        self.assertIsNone(PT.init_db())   # cold
        self.assertIsNone(PT.init_db())   # verified

    def test_no_process_wide_schema_ready_flag(self):
        self.assertFalse(
            any("SCHEMA" in name and "READY" in name for name in vars(PT)),
            "不得新增进程级 schema ready 布尔变量",
        )


if __name__ == "__main__":
    unittest.main()
