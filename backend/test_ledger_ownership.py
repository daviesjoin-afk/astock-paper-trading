# -*- coding: utf-8 -*-
"""PR-10：核心账本表的写入 owner 矩阵（机器可验证的边界）。

这张矩阵不是文档，而是**守卫**：它扫描所有生产模块里的 SQL 字面量，按表统计
``INSERT/UPDATE/DELETE/REPLACE`` 的写入方，并与声明的 owner 集合比对。
新增一个非 owner 写入会让本文件立刻变红。

口径（避免把非运行时写入混进来）：

* 只扫生产模块（``test_*`` 与 ``__init__`` 除外）；
* 只看以 SQL 关键字开头的字符串字面量，docstring / 注释里的表名不计；
* schema 迁移（``paper_schema_migrations`` / ``db_migrate``）、demo 种子
  （``demo_seed``）、一次性历史修复工具（``recover_legacy_order_cycle_provenance``）
  记为 ``maintenance``，不进 owner 比较。

``status="pending"`` 的表表示"多写入方是现状、本 PR 未收敛"：守卫锁定当前集合，
只允许**减少**，不允许新增。
"""
from __future__ import annotations

import ast
import re
import sys
import textwrap
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

SQL_HEAD = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|WITH)\b", re.IGNORECASE)
WRITE_HEAD = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)

#: 非运行时写入方：schema 迁移、demo 种子、一次性修复工具。它们不进 owner 比较。
MAINTENANCE_WRITERS = frozenset({
    "paper_schema_migrations",
    "db_migrate",
    "demo_seed",
    "recover_legacy_order_cycle_provenance",
})

#: 表 -> 允许的写入方（owner 在前）。``pending`` 表示多写方是现状，本 PR 不收敛。
OWNERSHIP = {
    "paper_cycles": {
        "status": "pending",
        "writers": {"paper_trading", "paper_cycle_service"},
        "note": "覆盖补核查：主入口创建/配置/状态及兼容补齐，周期服务归档；分阶段职责不认定为多写缺陷",
    },
    "paper_fills": {
        "status": "owned",
        "writers": {"execution_planner"},
        "note": "成交流水只有 commit_fill 一处权威写入",
    },
    "paper_positions": {
        "status": "owned",
        "writers": {"paper_lot_ledger"},
        "note": "paper_positions 是投影，只能由 lot owner 同步",
    },
    "paper_position_lots": {
        "status": "owned",
        "writers": {"paper_lot_ledger"},
        "note": (
            "lot 的创建/消耗/成交血缘都归 paper_lot_ledger：commit_fill 通过注入的 "
            "link_source_fill 回调回填 source_fill_id，本模块不再直写 lot 表"
        ),
    },
    "paper_capital_reservations": {
        "status": "owned",
        "writers": {"paper_capital_reservations"},
        "note": (
            "PR-10 三批收敛完成：paper_trading / entry_lifecycle / execution_dispatch 都改为"
            "调用装配层注入的 release 回调，预占表只剩 owner 一个运行时写入方"
        ),
    },
    "paper_risk_decisions": {
        "status": "owned",
        "writers": {"paper_risk_writes"},
        "note": "paper_risk_writes 统一校验来源信息并写入；两个 risk_log 只准备快照和策略戳",
    },
    "paper_audit": {
        "status": "owned",
        "writers": {"paper_repository"},
        "note": "paper_repository 统一审计追加及失败进化操作的清理；补偿方保留事务与异常处理",
    },
    "paper_orders": {
        "status": "pending",
        "writers": {
            "paper_trading", "manual_orders", "execution_planner",
            "paper_order_ledger", "execution_verification",
            "paper_risk_writes",
        },
        "note": "订单状态机由多条权威路径按生命周期阶段推进；本 PR 只锁定集合，不合并状态机",
    },
    "paper_signals": {
        "status": "pending",
        "writers": {
            "paper_trading", "manual_orders", "signal_service",
        },
        "note": "信号生命周期同样按阶段分写；锁定集合，下一步按阶段收口",
    },
    "paper_accounts": {
        "status": "pending",
        "writers": {
            "paper_trading", "paper_shared_cash", "paper_user_cycle_attachment",
            "paper_user_account_provisioning", "strategy_registry",
            "paper_order_execution_authority", "paper_cycle_ownership",
            "adaptive_engine", "adaptive_risk", "adaptive_selection",
            "adaptive_selection_compat", "evolution_apply", "ai_adoption_writer",
        },
        "note": "账户行被多个 owner 按各自字段更新（现金、周期挂接、策略参数）；集合只减不增",
    },
}


def _sql_literals(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = textwrap.dedent(node.value).strip()
            if SQL_HEAD.match(text):
                yield text


def write_sites(table: str):
    """返回 ``{模块: 写入语句条数}``（不含 non-runtime 写入方）。"""
    pattern = re.compile(rf"\b{re.escape(table)}\b", re.IGNORECASE)
    sites: dict[str, int] = {}
    for path in sorted(BACKEND_DIR.glob("*.py")):
        name = path.stem
        if name.startswith("test_") or name == "__init__" or name in MAINTENANCE_WRITERS:
            continue
        for text in _sql_literals(path):
            if not WRITE_HEAD.match(text) or not pattern.search(text):
                continue
            sites[name] = sites.get(name, 0) + 1
    return sites


def real_write_modules(table: str) -> set[str]:
    """该表的**真实写目标**模块集合（按 SQL 首表解析，不把子查询读表算成写入）。

    与 :func:`write_sites` 的区别：``UPDATE paper_signals ... WHERE id IN
    (SELECT ... FROM paper_orders ...)`` 在 ``write_sites`` 里会同时命中订单表，
    本函数只认 ``UPDATE / INSERT INTO / DELETE FROM`` 后的第一张表。
    """
    import tools.ledger_write_inventory as INV
    return {
        entry["module"] for entry in INV.scan()["entries"]
        if entry["role"] == "runtime" and table in entry["write_targets"]
    }


class OrderExpiryOperationOwnershipTests(unittest.TestCase):
    """订单/信号过期的操作级边界；不宣称订单/信号整表已单写。"""

    @staticmethod
    def inline_expiry_writes(source):
        tables = set()
        for node in ast.walk(ast.parse(textwrap.dedent(source))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = textwrap.dedent(node.value).strip()
                if WRITE_HEAD.match(sql):
                    tables.update(re.findall(r"\bpaper_(?:orders|signals)\b", sql, re.IGNORECASE))
        return tables

    def test_lifecycle_expiry_delegates_both_writes_without_inline_sql(self):
        import inspect
        import entry_lifecycle as ELC
        source = inspect.getsource(ELC.expire_stale_orders)
        self.assertEqual(set(), self.inline_expiry_writes(source))
        # 正控证明守卫会发现回流，而不是空扫即通过。
        self.assertEqual({"paper_orders", "paper_signals"}, self.inline_expiry_writes(
            source + '\n    conn.execute("UPDATE paper_orders SET status=?")\n'
            '    conn.execute("UPDATE paper_signals SET status=?")\n'))
        signature = inspect.signature(ELC.expire_stale_orders)
        for name in ("release_reservation", "expire_order", "expire_signal"):
            self.assertEqual(inspect.Parameter.empty, signature.parameters[name].default)
        self.assertEqual("pending", OWNERSHIP["paper_orders"]["status"])
        self.assertEqual("pending", OWNERSHIP["paper_signals"]["status"])

    def test_expiry_owners_expose_narrow_business_operations(self):
        import inspect
        import paper_order_ledger as POL
        import signal_service as SIG
        self.assertEqual(["conn", "order_id", "reason", "cancelled_at"],
                         list(inspect.signature(POL.expire_order).parameters))
        self.assertEqual(["conn", "signal_id", "reason"],
                         list(inspect.signature(SIG.expire_for_expired_order).parameters))
        self.assertEqual(["conn", "signal_id", "reason"],
                         list(inspect.signature(SIG.expire_stale_signal).parameters))
        self.assertEqual(["conn", "order_id", "cancelled_at"],
                         list(inspect.signature(POL.supersede_for_expired_signal).parameters))

    def test_signal_expiry_has_required_owner_ports_and_no_inline_writes(self):
        import inspect
        import entry_lifecycle as ELC
        source = inspect.getsource(ELC.expire_stale_signals)
        self.assertEqual(set(), self.inline_expiry_writes(source))
        self.assertEqual({"paper_orders"}, self.inline_expiry_writes(
            source + '\n    conn.execute("UPDATE paper_orders SET status=?")\n'))
        signature = inspect.signature(ELC.expire_stale_signals)
        for name in ("release_reservation", "expire_signal", "supersede_order"):
            self.assertEqual(inspect.Parameter.empty, signature.parameters[name].default)
        for table in ("paper_orders", "paper_signals"):
            self.assertEqual("pending", OWNERSHIP[table]["status"])
            self.assertNotIn("entry_lifecycle", OWNERSHIP[table]["writers"])

    def test_cycles_are_registered_by_stage_without_claiming_single_writer(self):
        self.assertEqual("pending", OWNERSHIP["paper_cycles"]["status"])
        self.assertEqual({"paper_trading", "paper_cycle_service"},
                         OWNERSHIP["paper_cycles"]["writers"])

    def test_cycle_authority_reads_cannot_write_or_bootstrap(self):
        import sqlite3
        import paper_order_execution_authority as POEA
        import paper_cycle_ownership as PCY
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE paper_cycles(id INTEGER,status TEXT,enabled_strategies TEXT)")
        conn.execute("INSERT INTO paper_cycles VALUES(8,'paused','[]')")
        conn.commit()
        conn.execute("PRAGMA query_only=ON")
        self.assertEqual(8, POEA.active_cycle_id_readonly(conn))
        self.assertTrue(PCY.explicit_empty_cycle(conn, 8))
        self.assertFalse(PCY.explicit_empty_cycle(conn, 9))
        missing = sqlite3.connect(":memory:")
        self.addCleanup(missing.close)
        missing.execute("PRAGMA query_only=ON")
        self.assertIsNone(POEA.active_cycle_id_readonly(missing))


class WaitlistRetirementOperationTests(unittest.TestCase):
    """第九批：等待池重复记录回收的操作级边界；整表仍为 pending。"""

    def test_recover_path_delegates_both_writes_to_the_dispatch_owner(self):
        import inspect
        import paper_trading as PT
        source = inspect.getsource(PT._recover_stale_runtime_state)
        inline = OrderExpiryOperationOwnershipTests.inline_expiry_writes
        self.assertEqual(set(), inline(source))
        # 正控：两条语句回流时守卫必须变红。
        reflow = "\n" + '    conn.execute("UPDATE paper_orders SET status=?")\n' \
                      '    conn.execute("UPDATE paper_signals SET status=?")\n'
        self.assertEqual({"paper_orders", "paper_signals"}, inline(source + reflow))

    def test_waitlist_owner_is_a_business_operation_not_a_sql_sink(self):
        import inspect
        import execution_dispatch as EPD
        signature = inspect.signature(EPD.retire_duplicate_waitlist)
        self.assertEqual(["conn", "waitlist_status", "signal_statuses", "signals"],
                         list(signature.parameters))
        for name in ("waitlist_status", "signal_statuses", "signals"):
            self.assertEqual(inspect.Parameter.KEYWORD_ONLY,
                             signature.parameters[name].kind)
            self.assertEqual(inspect.Parameter.empty,
                             signature.parameters[name].default)

    def test_no_module_joins_or_leaves_the_writer_sets(self):
        self.assertEqual("pending", OWNERSHIP["paper_orders"]["status"])
        self.assertEqual("pending", OWNERSHIP["paper_signals"]["status"])
        self.assertIn("paper_order_ledger", OWNERSHIP["paper_orders"]["writers"])
        self.assertIn("signal_service", OWNERSHIP["paper_signals"]["writers"])
        self.assertNotIn("execution_dispatch", OWNERSHIP["paper_signals"]["writers"])
        self.assertIn("paper_trading", OWNERSHIP["paper_orders"]["writers"])
        self.assertIn("paper_trading", OWNERSHIP["paper_signals"]["writers"])


class ReconcileWriteOperationTests(unittest.TestCase):
    """第十批：活动委托/信号对账的操作级边界；整表仍为 pending。"""

    def test_reconcile_path_has_no_inline_order_or_signal_writes(self):
        import inspect
        import paper_trading as PT
        source = inspect.getsource(PT._reconcile_signal_order_states)
        inline = OrderExpiryOperationOwnershipTests.inline_expiry_writes
        self.assertEqual(set(), inline(source))
        reflow = (chr(10) + '    conn.execute("UPDATE paper_orders SET status=?")' + chr(10)
                  + '    conn.execute("UPDATE paper_signals SET status=?")' + chr(10))
        self.assertEqual({"paper_orders", "paper_signals"}, inline(source + reflow))

    def test_reconcile_owners_are_narrow_business_operations(self):
        import inspect
        import paper_order_ledger as POL
        import signal_service as SIG
        self.assertEqual(["conn", "order_id"],
                         list(inspect.signature(POL.supersede_duplicate_execution_retry).parameters))
        self.assertEqual(["conn", "order_id", "reason", "statuses"],
                         list(inspect.signature(POL.supersede_for_terminal_signal).parameters))
        self.assertEqual(["conn", "signal_id", "status"],
                         list(inspect.signature(SIG.reconcile_signal_status).parameters))

    def test_terminal_owner_takes_the_status_vocabulary_from_the_caller(self):
        import sqlite3
        import entry_lifecycle as ELC
        import paper_order_ledger as POL
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, status TEXT, reason TEXT)")
        conn.executemany("INSERT INTO paper_orders VALUES(?,?,?)",
                         [(1, "pending_limit", None), (2, "awaiting_batch", None),
                          (3, "deferred_capacity", None)])
        POL.supersede_for_terminal_signal(conn, 1, reason="回收",
                                          statuses=ELC.ENTRY_RETRY_ORDER_STATUSES)
        self.assertEqual("superseded", conn.execute(
            "SELECT status FROM paper_orders WHERE id=1").fetchone()[0])
        # 不在传入词表里的状态不被动：owner 不自己拼状态集合。
        self.assertEqual("awaiting_batch", conn.execute(
            "SELECT status FROM paper_orders WHERE id=2").fetchone()[0])

    def test_write_sets_keep_pending_and_unchanged_membership(self):
        self.assertEqual(6, len(write_sites("paper_orders")))
        self.assertEqual(3, len(write_sites("paper_signals")))
        for table in ("paper_orders", "paper_signals"):
            self.assertEqual("pending", OWNERSHIP[table]["status"])
        self.assertIn("paper_order_ledger", OWNERSHIP["paper_orders"]["writers"])
        self.assertIn("signal_service", OWNERSHIP["paper_signals"]["writers"])


class LedgerWriteOwnershipTests(unittest.TestCase):
    def test_every_declared_table_exists_in_the_schema(self):
        import paper_schema_migrations as PSM
        for table in OWNERSHIP:
            self.assertIn(table, PSM.REQUIRED_TABLES,
                          f"owner 矩阵里的 {table} 不在必需表清单里（表名可能写错）")

    def test_non_owner_writes_do_not_regress(self):
        problems = []
        for table, spec in OWNERSHIP.items():
            actual = real_write_modules(table)
            extra = actual - spec["writers"]
            if extra:
                problems.append(f"{table}: 非 owner 写入 {sorted(extra)}（允许 {sorted(spec['writers'])}）")
        self.assertEqual(problems, [], "新增了非 owner 的账本写入：\n" + "\n".join(problems))

    def test_owned_tables_have_exactly_one_runtime_writer(self):
        for table, spec in OWNERSHIP.items():
            if spec["status"] != "owned":
                continue
            with self.subTest(table=table):
                actual = real_write_modules(table)
                self.assertEqual(actual, spec["writers"],
                                 f"{table} 声明为单写方但实际写入方是 {sorted(actual)}")

    #: 例外集合必须为空：三批迁移后，预占释放的 UPDATE 只允许出现在 owner 内。
    RESERVATION_RELEASE_EXCEPTIONS: set[str] = set()

    def test_reservation_release_has_a_single_implementation(self):
        """同一句释放 UPDATE 只允许出现在 owner 内（例外集合必须为空）。"""
        offenders = set()
        for path in sorted(BACKEND_DIR.glob("*.py")):
            name = path.stem
            if name.startswith("test_") or name in MAINTENANCE_WRITERS:
                continue
            if name == "paper_capital_reservations":
                continue
            for text in _sql_literals(path):
                if re.search(r"UPDATE\s+paper_capital_reservations", text, re.IGNORECASE):
                    offenders.add(name)
                    break
        self.assertEqual(offenders, self.RESERVATION_RELEASE_EXCEPTIONS,
                         f"预占写入权归 paper_capital_reservations；实际写入方 {sorted(offenders)}")

    def test_owner_api_is_a_business_operation_not_a_raw_sql_sink(self):
        import paper_capital_reservations as PCR
        self.assertTrue(callable(PCR.release_order_reservation))
        signature = __import__("inspect").signature(PCR.release_order_reservation)
        self.assertEqual(list(signature.parameters), ["conn", "order_id", "now"],
                         "owner 接口必须是业务操作（conn/order_id/now），不能退化成任意 SQL")


if __name__ == "__main__":
    unittest.main()

class ReservationReleaseEquivalenceTests(unittest.TestCase):
    """owner 的释放入口与旧的三份内联 SQL 逐字段等价（冻结输入）。"""

    def _conn(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE paper_capital_reservations(
                   id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                   order_key TEXT NOT NULL UNIQUE, account_id TEXT NOT NULL,
                   code TEXT NOT NULL, side TEXT NOT NULL, amount REAL NOT NULL,
                   fees REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'reserved',
                   created_at TEXT NOT NULL, released_at TEXT)"""
        )
        return conn

    def _row(self, conn, key, status="reserved", released_at=None):
        conn.execute(
            "INSERT INTO paper_capital_reservations(cycle_id,order_key,account_id,code,"
            "side,amount,status,created_at,released_at) VALUES(1,?,?,?,?,?,?,?,?)",
            (key, "acct", "600001", "buy", 1000.0, status, "2026-10-30 09:30:00", released_at),
        )
        conn.commit()

    def _state(self, conn, key):
        return conn.execute(
            "SELECT status,released_at FROM paper_capital_reservations WHERE order_key=?",
            (key,),
        ).fetchone()

    def test_release_matches_legacy_statement(self):
        import paper_capital_reservations as PCR
        conn = self._conn()
        self._row(conn, "9")
        affected = PCR.release_order_reservation(conn, 9, now="2026-10-30 10:00:00")
        self.assertEqual(affected, 1)
        self.assertEqual(self._state(conn, "9"), ("released", "2026-10-30 10:00:00"))

    def test_release_is_idempotent_and_keeps_the_first_timestamp(self):
        import paper_capital_reservations as PCR
        conn = self._conn()
        self._row(conn, "9")
        PCR.release_order_reservation(conn, 9, now="2026-10-30 10:00:00")
        second = PCR.release_order_reservation(conn, 9, now="2026-10-30 11:00:00")
        self.assertEqual(second, 0)
        self.assertEqual(self._state(conn, "9"), ("released", "2026-10-30 10:00:00"),
                         "重复释放不得改写首次 released_at")

    def test_release_only_touches_reserved_rows(self):
        import paper_capital_reservations as PCR
        conn = self._conn()
        self._row(conn, "10", status="consumed", released_at="2026-10-30 09:59:00")
        self.assertEqual(PCR.release_order_reservation(conn, 10, now="2026-10-30 10:00:00"), 0)
        self.assertEqual(self._state(conn, "10"), ("consumed", "2026-10-30 09:59:00"))

    def test_integer_stored_keys_are_still_matched(self):
        """旧库里 order_key 可能以整数存储：CAST(... AS TEXT) 必须仍然命中。"""
        import paper_capital_reservations as PCR
        conn = self._conn()
        self._row(conn, 11)
        conn.execute("UPDATE paper_capital_reservations SET order_key=11 WHERE order_key='11'")
        conn.commit()
        self.assertEqual(PCR.release_order_reservation(conn, 11, now="2026-10-30 10:00:00"), 1)

