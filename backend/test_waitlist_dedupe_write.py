"""第九批：等待池重复记录回收的持久化切片（对照 master 48efdef）。

冻结基线见 ``fixtures/waitlist_dedupe_48efdef.txt`` —— 逐字复制自
``paper_trading._recover_stale_runtime_state`` 的两条 superseded 写入。
本批只把这两条写入移交 ``execution_dispatch.retire_duplicate_waitlist``，
判定（哪条算"较新"）、调用时机、顺序和事务归属保持在原处。
"""
from __future__ import annotations

import ast
import inspect
import sqlite3
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import entry_lifecycle as ELC
import execution_dispatch as EPD
import paper_trading as PT
import signal_service as SIG
import test_ledger_ownership as TLO

BASELINE = Path(__file__).with_name("fixtures") / "waitlist_dedupe_48efdef.txt"
WAITLIST = ELC.ENTRY_FROZEN_WAITLIST_STATUS
SIGNAL_STATES = ELC.ENTRY_RETRY_SIGNAL_STATUSES
ORDER_MARK = "同标的较新等待池记录已保留"
SIGNAL_MARK = "等待池旧委托已由后续记录替代"

SCHEMA = """
CREATE TABLE paper_orders(
    id INTEGER PRIMARY KEY,
    account_id INTEGER,
    code TEXT,
    side TEXT,
    status TEXT,
    reason TEXT,
    signal_id INTEGER
);
CREATE TABLE paper_signals(
    id INTEGER PRIMARY KEY,
    status TEXT,
    reason TEXT
);
"""

# (id, account_id, code, side, status, reason, signal_id)
ORDERS = [
    (1, 7, "AAA", "buy", "deferred_capacity", None, 11),            # 旧等待池，被 2/3 替代
    (2, 7, "AAA", "buy", "deferred_capacity", "原始原因", 12),      # 较新，保留
    (3, 7, "AAA", "buy", WAITLIST, None, 13),                       # 较新的冻结标记，也算替代者
    (4, 9, "AAA", "buy", "deferred_capacity", None, 14),            # 其他账户，无替代者
    (5, 7, "BBB", "sell", "deferred_capacity", None, 15),           # 卖单不参与
    (6, 7, "CCC", "buy", "deferred_capacity", "", None),            # 旧行且无信号
    (7, 7, "CCC", "buy", "deferred_capacity", None, 16),            # 较新，保留
    (8, 7, "DDD", "buy", "deferred_capacity", "已有原因", 17),      # 旧行，信号已终态
    (9, 7, "DDD", "buy", "deferred_capacity", None, 18),            # 较新，保留
    (10, 7, "EEE", "buy", "pending_limit", None, 19),               # 非等待池状态，不参与
]

# (id, status, reason)
SIGNALS = [
    (11, "pending", None),          # 旧等待池订单的信号 → superseded
    (12, "pending", "原始原因"),     # 保留订单的信号 → 不动
    (13, "pending", None),          # 保留订单的信号 → 不动
    (14, "pending", None),          # 其他账户 → 不动
    (15, "pending", None),          # 卖单信号 → 不动
    (16, "filled", None),           # 终态，即使订单被替代也不改写
    (17, "blocked", None),          # 终态，同上
    (18, "pending", None),          # 保留订单的信号 → 不动
    (19, "pending", None),          # 与等待池无关 → 不动
]


def _db(rows_orders=None, rows_signals=None):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?)",
                     ORDERS if rows_orders is None else rows_orders)
    conn.executemany("INSERT INTO paper_signals VALUES(?,?,?)",
                     SIGNALS if rows_signals is None else rows_signals)
    conn.commit()
    return conn


def _state(conn):
    return {
        "paper_orders": [tuple(row) for row in
                         conn.execute("SELECT * FROM paper_orders ORDER BY id")],
        "paper_signals": [tuple(row) for row in
                          conn.execute("SELECT * FROM paper_signals ORDER BY id")],
    }


def _row(conn, table, row_id):
    return conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()


def _baseline_namespace():
    namespace = {
        "ENTRY_FROZEN_WAITLIST_STATUS": WAITLIST,
        "ENTRY_RETRY_SIGNAL_STATUSES": SIGNAL_STATES,
    }
    exec(compile(BASELINE.read_text(encoding="utf-8"), str(BASELINE), "exec"), namespace)
    return namespace


class WaitlistDedupeParityTests(unittest.TestCase):
    """与新 owner 逐行对照：状态、原因、未涉及行、SQL 顺序、事务归属。"""

    def run_legacy(self, conn):
        namespace = _baseline_namespace()
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            namespace["_retire_duplicate_waitlist"](
                conn, waitlist_status=WAITLIST, signal_statuses=SIGNAL_STATES)
        finally:
            conn.set_trace_callback(None)
        return [" ".join(sql.split()) for sql in statements]

    def run_owner(self, conn):
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            with mock.patch.object(EPD, "_now", side_effect=AssertionError("owner read clock")):
                result = EPD.retire_duplicate_waitlist(
                    conn, waitlist_status=WAITLIST, signal_statuses=SIGNAL_STATES,
                    signals=SIG)
        finally:
            conn.set_trace_callback(None)
        return result, [" ".join(sql.split()) for sql in statements]

    def test_full_state_matches_frozen_baseline(self):
        legacy, owner = _db(), _db()
        self.run_legacy(legacy)
        result, _ = self.run_owner(owner)
        self.assertIsNone(result)
        self.assertEqual(_state(legacy), _state(owner))

    def test_history_exceeds_bind_limit_with_duplicates_and_terminal_signals(self):
        for status in ("filled", "pending"):
            with self.subTest(status=status):
                orders = [(i, 7, "AAA", "buy", "superseded", ORDER_MARK, (i + 1) // 2)
                          for i in range(1, 81)]
                signals = [(i, status, None) for i in range(1, 41)]
                with _db(orders, signals) as legacy, _db(orders, signals) as owner:
                    for conn in (legacy, owner):
                        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32)
                    self.run_legacy(legacy)
                    self.run_owner(owner)
                    self.assertEqual(_state(legacy), _state(owner))

    def test_signal_update_failure_is_statement_atomic_and_caller_can_rollback(self):
        with _db() as conn:
            before = _state(conn)
            conn.execute("""CREATE TRIGGER reject_signal BEFORE UPDATE ON paper_signals
                            WHEN NEW.id=12 BEGIN SELECT RAISE(ABORT,'reject'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                self.run_owner(conn)
            self.assertEqual(before["paper_signals"], _state(conn)["paper_signals"])
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            self.assertEqual(before, _state(conn))

    def test_superseded_rows_reasons_and_untouched_rows(self):
        conn = _db()
        self.run_owner(conn)
        expected_orders = {
            1: "；" + ORDER_MARK,                 # reason 为 NULL → COALESCE 保留空串
            2: "原始原因；" + ORDER_MARK,          # 已有原因保留并追加
            6: "；" + ORDER_MARK,                 # reason 为空串
            8: "已有原因；" + ORDER_MARK,          # 信号已终态，订单照样回收
        }
        for order_id, reason in expected_orders.items():
            row = _row(conn, "paper_orders", order_id)
            self.assertEqual("superseded", row["status"], f"order {order_id} 未回收")
            self.assertEqual(reason, row["reason"])
        original_status = {row[0]: row[4] for row in ORDERS}
        for order_id in (3, 4, 5, 7, 9, 10):
            self.assertEqual(original_status[order_id],
                             _row(conn, "paper_orders", order_id)["status"],
                             f"order {order_id} 被误改")
        expected_signals = {
            11: "；" + SIGNAL_MARK,
            12: "原始原因；" + SIGNAL_MARK,
        }
        for signal_id, reason in expected_signals.items():
            row = _row(conn, "paper_signals", signal_id)
            self.assertEqual("superseded", row["status"], f"signal {signal_id} 未回收")
            self.assertEqual(reason, row["reason"])
        original_signal_status = {row[0]: row[1] for row in SIGNALS}
        for signal_id in (13, 14, 15, 16, 17, 18, 19):
            self.assertEqual(original_signal_status[signal_id],
                             _row(conn, "paper_signals", signal_id)["status"],
                             f"signal {signal_id} 被误改")

    def test_order_write_precedes_signal_write(self):
        conn = _db()
        _, statements = self.run_owner(conn)
        writes = [sql for sql in statements if sql.upper().startswith("UPDATE")]
        self.assertEqual(2, len(writes))
        self.assertTrue(writes[0].startswith("UPDATE paper_orders"))
        self.assertTrue(writes[1].startswith("UPDATE paper_signals"))
        self.assertIn(ORDER_MARK, writes[0])
        self.assertIn(SIGNAL_MARK, writes[1])
        # 信号 owner 只写信号表；订单表作为集合子查询读取，保留固定参数规模。
        self.assertIn("SELECT signal_id FROM paper_orders", writes[1])
        self.assertIn("WHERE id IN (", writes[1])

    def test_repeat_run_is_idempotent(self):
        conn = _db()
        self.run_owner(conn)
        once = _state(conn)
        self.run_owner(conn)
        self.assertEqual(once, _state(conn))

    def test_late_terminal_signal_is_reclaimed_only_while_retryable(self):
        # 已终态信号的遗留订单：订单照样回收，信号保持原终态（与基线一致）。
        conn = _db()
        self.run_owner(conn)
        order = _row(conn, "paper_orders", 8)
        self.assertEqual("superseded", order["status"])
        self.assertEqual("blocked", _row(conn, "paper_signals", 17)["status"])
        self.assertEqual("filled", _row(conn, "paper_signals", 16)["status"])

    def test_owner_leaves_transaction_open(self):
        conn = _db()
        self.run_owner(conn)
        self.assertTrue(conn.in_transaction)

    def test_truly_empty_waitlist_is_a_noop(self):
        conn = _db(rows_orders=[(1, 7, "AAA", "buy", "deferred_capacity", None, 11)],
                   rows_signals=[(11, "pending", None)])
        before = _state(conn)
        self.run_owner(conn)
        self.assertEqual(before, _state(conn))


class WaitlistDedupeOwnershipTests(unittest.TestCase):
    """操作级边界：写入在 owner，判定与顺序在编排层。"""

    @staticmethod
    def inline_writes(source):
        tables = set()
        for node in ast.walk(ast.parse(textwrap.dedent(source))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = textwrap.dedent(node.value).strip()
                if sql.upper().startswith(("UPDATE", "INSERT", "DELETE", "REPLACE")):
                    for table in ("paper_orders", "paper_signals"):
                        if table in sql.lower():
                            tables.add(table)
        return tables

    def test_recover_path_has_no_inline_order_or_signal_writes(self):
        source = inspect.getsource(PT._recover_stale_runtime_state)
        self.assertEqual(set(), self.inline_writes(source))
        # 正控：守卫确实能发现回流，而不是空扫即通过。
        self.assertEqual({"paper_orders"}, self.inline_writes(
            source + '\n    conn.execute("UPDATE paper_orders SET status=?")\n'))
        self.assertEqual({"paper_signals"}, self.inline_writes(
            source + '\n    conn.execute("UPDATE paper_signals SET status=?")\n'))

    def test_recover_path_calls_the_owner_with_keywords(self):
        source = inspect.getsource(PT._recover_stale_runtime_state)
        calls = [node for node in ast.walk(ast.parse(textwrap.dedent(source)))
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "retire_duplicate_waitlist"]
        self.assertEqual(1, len(calls))
        self.assertEqual(["waitlist_status", "signal_statuses", "signals"],
                         [kw.arg for kw in calls[0].keywords])

    def test_owner_exposes_narrow_business_operation(self):
        signature = inspect.signature(EPD.retire_duplicate_waitlist)
        self.assertEqual(["conn", "waitlist_status", "signal_statuses", "signals"],
                         list(signature.parameters))
        self.assertEqual(inspect.Parameter.KEYWORD_ONLY,
                         signature.parameters["waitlist_status"].kind)
        self.assertEqual(inspect.Parameter.KEYWORD_ONLY,
                         signature.parameters["signal_statuses"].kind)

    def test_owner_delegates_both_writes_and_keeps_no_sql(self):
        source = inspect.getsource(EPD.retire_duplicate_waitlist)
        self.assertIn("POL.retire_duplicate_waitlist_orders", source)
        self.assertIn("signals.retire_waitlist_signals", source)
        self.assertNotIn("signal_ids", source)
        self.assertNotIn("UPDATE ", source.upper())

    def test_owner_is_exported_and_no_new_module_joins_the_writer_sets(self):
        self.assertIn("retire_duplicate_waitlist", EPD.__all__)
        matrix = TLO.OWNERSHIP
        # 订单行的持久化已在 PR-10 工作包 A 移交 paper_order_ledger；
        # 信号写入仍由 execution_dispatch 在对账/等待池阶段承担。
        self.assertIn("paper_order_ledger", matrix["paper_orders"]["writers"])
        self.assertIn("signal_service", matrix["paper_signals"]["writers"])
        for table in ("paper_orders", "paper_signals"):
            self.assertEqual("pending", matrix[table]["status"])
        # 信号侧没有新增写入口：本批不把同一业务操作拆给 signal_service。
        self.assertNotIn("retire_duplicate_waitlist", SIG.__all__)


if __name__ == "__main__":
    unittest.main()
