"""第十批：活动委托/信号对账写入的持久化切片（对照 master 1d41508）。

冻结基线见 ``fixtures/reconcile_1d41508.txt`` —— 逐字复制 1d41508 的
``paper_trading._reconcile_signal_order_states``（三条内联写入 + 顺序）。
本批只把三条写入移交 owner：订单回收 → ``execution_dispatch``，
信号状态对账 → ``signal_service``；判定、预占释放、统计与顺序留在原处。
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import sqlite3
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import entry_lifecycle as ELC
import execution_dispatch as EPD
import paper_order_ledger as POL
import paper_capital_reservations as PCR
import paper_schema_migrations as PSM
import paper_trading as PT
import signal_service as SIG
import test_ledger_ownership as TLO

BASELINE = Path(__file__).with_name("fixtures") / "reconcile_1d41508.txt"
FIXED_NOW = dt.datetime(2026, 9, 11, 10, 0, 0)

SCHEMA = """
CREATE TABLE paper_orders(
    id INTEGER PRIMARY KEY,
    signal_id INTEGER,
    side TEXT,
    status TEXT,
    reason TEXT
);
CREATE TABLE paper_signals(
    id INTEGER PRIMARY KEY,
    status TEXT,
    reason TEXT
);
CREATE TABLE paper_capital_reservations(
    id INTEGER PRIMARY KEY,
    order_key TEXT,
    status TEXT,
    released_at TEXT
);
"""


def _db(orders=(), signals=(), reservations=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO paper_orders VALUES(?,?,?,?,?)", orders)
    conn.executemany("INSERT INTO paper_signals VALUES(?,?,?)", signals)
    conn.executemany("INSERT INTO paper_capital_reservations VALUES(?,?,?,?)", reservations)
    conn.commit()
    return conn


def _state(conn):
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("paper_orders", "paper_signals", "paper_capital_reservations")}


def _legacy_namespace():
    namespace = {
        "ENTRY_RETRY_ORDER_STATUSES": ELC.ENTRY_RETRY_ORDER_STATUSES,
        "ENTRY_FROZEN_WAITLIST_STATUS": ELC.ENTRY_FROZEN_WAITLIST_STATUS,
        "MANUAL_EXECUTION_RETRY_STATUS": ELC.MANUAL_EXECUTION_RETRY_STATUS,
        "STRATEGY_EXECUTION_RETRY_STATUS": ELC.STRATEGY_EXECUTION_RETRY_STATUS,
        "PSM": PSM,
        "PCR": PCR,
        "_finish_capital_reservation": PT._finish_capital_reservation,
        "_now": lambda: FIXED_NOW,
    }
    exec(compile(BASELINE.read_text(encoding="utf-8"), str(BASELINE), "exec"), namespace)
    return namespace


def run(conn, *, legacy):
    statements = []
    clock = mock.Mock(return_value=FIXED_NOW)
    conn.set_trace_callback(statements.append)
    try:
        with mock.patch.object(PT, "_now", clock):
            if legacy:
                namespace = _legacy_namespace()
                namespace["_now"] = clock
                result = namespace["_reconcile_signal_order_states"](conn)
            else:
                result = PT._reconcile_signal_order_states(conn)
    finally:
        conn.set_trace_callback(None)
    return {"result": result, "state": _state(conn),
            "sql": [" ".join(sql.split()) for sql in statements],
            "clock_calls": clock.call_count, "in_transaction": conn.in_transaction}


# ── 场景 ──────────────────────────────────────────────────────────────
DUP_RETRY = dict(
    orders=[(1, 11, "buy", "execution_retry", None),
            (2, 11, "buy", "execution_retry", "旧原因"),
            (3, 11, "buy", "execution_retry", None)],
    signals=[(11, "pending", None)],
    reservations=[(1, "1", "reserved", None), (2, "2", "reserved", None),
                  (3, "3", "reserved", None)],
)

TERMINAL_SIGNALS = dict(
    orders=[(1, 21, "buy", "pending_limit", None),
            (2, 22, "buy", "deferred_capacity", "旧"),
            (3, 23, "buy", ELC.ENTRY_FROZEN_WAITLIST_STATUS, None),
            (4, 24, "buy", "execution_retry", None),
            (5, 25, "buy", "pending_limit", None)],
    signals=[(21, "superseded", None), (22, "filled", None), (23, "expired", None),
             (24, "blocked", None), (25, "shadow_q3", "原因")],
    reservations=[(1, "1", "reserved", None), (2, "2", "reserved", None),
                  (3, "3", "reserved", None), (4, "4", "reserved", None),
                  (5, "5", "reserved", None)],
)

DRIFT = dict(
    orders=[(1, 31, "buy", "pending_limit", None),
            (2, 32, "buy", "partially_filled", None),
            (3, 33, "buy", "pending_execution", None),
            (4, 34, "buy", ELC.ENTRY_FROZEN_WAITLIST_STATUS, None),
            (5, 35, "buy", "deferred_capacity", None),
            (6, 36, "buy", "manual_execution_retry", None),
            (7, 37, "buy", "execution_retry", None)],
    signals=[(31, "deferred_capacity", None), (32, "pending", "旧"),
             (33, "pending", None), (34, "pending", None),
             (35, "deferred_capacity", None), (36, "blocked", None),
             (37, "pending", None)],
    reservations=[],
)

NOOP = dict(
    orders=[(1, 41, "buy", "pending_limit", None),
            (2, 42, "buy", "deferred_capacity", None)],
    signals=[(41, "pending", None), (42, "deferred_capacity", None)],
    reservations=[],
)

MIXED = dict(
    orders=[(1, 51, "buy", "execution_retry", None),
            (2, 51, "buy", "execution_retry", None),
            (3, 52, "buy", "pending_limit", None),
            (4, 53, "buy", "deferred_capacity", None),
            (5, 54, "buy", "pending_limit", "已有原因"),
            (6, None, "buy", "pending_limit", None),          # 无信号：JOIN 排除
            (7, 55, "sell", "pending_limit", None),           # 卖单：WHERE 排除
            (8, 56, "buy", "expired", None)],                 # 非活动态：WHERE 排除
    signals=[(51, "pending", None), (52, "blocked", None), (53, "pending", None),
             (54, "pending", None), (55, "pending", None), (56, "pending", None)],
    reservations=[(1, "1", "reserved", None), (2, "2", "reserved", None),
                  (3, "3", "reserved", None), (4, "4", "reserved", None)],
)


class ReconcileParityTests(unittest.TestCase):
    """与 1d41508 冻结函数比较返回值、完整行、SQL 顺序与时钟调用。"""

    def assertRoundEqual(self, expected, actual, *, label=""):
        """同一轮内新旧实现必须完全一致（返回值、账本行、SQL 顺序、时钟）。"""
        self.assertEqual(expected["result"], actual["result"], label)
        self.assertEqual(expected["state"], actual["state"], label)
        self.assertEqual(expected["sql"], actual["sql"], label)
        self.assertEqual(expected["clock_calls"], actual["clock_calls"], label)
        self.assertEqual(expected["in_transaction"], actual["in_transaction"], label)

    def compare(self, scenario):
        legacy, current = _db(**scenario), _db(**scenario)
        self.addCleanup(legacy.close)
        self.addCleanup(current.close)
        expected = run(legacy, legacy=True)
        actual = run(current, legacy=False)
        self.assertRoundEqual(expected, actual)
        return actual

    def test_duplicate_retries_keep_newest_and_release_older(self):
        actual = self.compare(DUP_RETRY)
        # 重复 retry 行在同一轮里已被退役，随后按活动态选行只剩最新一条。
        self.assertEqual({"status": "ok", "orders_checked": 1, "orders_reconciled": 0,
                          "retry_index": PSM.SIGNAL_RETRY_INDEX_CREATED},
                         actual["result"])
        statuses = [row[3] for row in actual["state"]["paper_orders"]]
        self.assertEqual(["superseded", "superseded", "execution_retry"], statuses)
        self.assertIn("同一信号仅保留最新执行重试", actual["state"]["paper_orders"][0][4])
        self.assertIn("旧原因；同一信号仅保留最新执行重试",
                      actual["state"]["paper_orders"][1][4])
        released = [row[2] for row in actual["state"]["paper_capital_reservations"]]
        self.assertEqual(["released", "released", "reserved"], released)

    def test_terminal_signal_reclaims_retryable_orders(self):
        actual = self.compare(TERMINAL_SIGNALS)
        reasons = [row[4] for row in actual["state"]["paper_orders"]]
        self.assertIn("信号已替代，回收遗留活动等待委托", reasons[0])
        self.assertIn("已关联终态信号，回收遗留活动委托", reasons[1])
        self.assertIn("旧；", reasons[1])
        self.assertEqual(5, actual["result"]["orders_reconciled"])
        released = [row[2] for row in actual["state"]["paper_capital_reservations"]]
        self.assertEqual(["released"] * 5, released)

    def test_signal_drift_follows_order_state_without_downgrading_partial_fill(self):
        actual = self.compare(DRIFT)
        signals = {row[0]: (row[1], row[2]) for row in actual["state"]["paper_signals"]}
        orders = {row[0]: row for row in actual["state"]["paper_orders"]}
        # pending_limit 的委托把 deferred_capacity 信号对账回 pending，并追加原因。
        self.assertEqual("pending", signals[31][0])
        self.assertIn("按活动委托状态对账", signals[31][1])
        # 部分成交不得降级成 pending。
        self.assertEqual("partially_filled", signals[32][0])
        # 已经是目标状态的行不重写、不刷原因。
        self.assertEqual(("pending", None), signals[33])
        self.assertEqual(ELC.ENTRY_FROZEN_WAITLIST_STATUS, signals[34][0])
        self.assertEqual(("deferred_capacity", None), signals[35])
        # 终态信号（blocked）：订单被回收，信号保持终态。
        self.assertEqual(("blocked", None), signals[36])
        self.assertEqual("superseded", orders[6][3])
        self.assertEqual(("pending", None), signals[37])
        self.assertEqual(4, actual["result"]["orders_reconciled"])

    def test_noop_rows_are_not_rewritten(self):
        actual = self.compare(NOOP)
        self.assertEqual(0, actual["result"]["orders_reconciled"])
        self.assertEqual({"status": "ok", "orders_checked": 2, "orders_reconciled": 0,
                          "retry_index": PSM.SIGNAL_RETRY_INDEX_CREATED}, actual["result"])
        self.assertFalse(any(sql.startswith("UPDATE") for sql in actual["sql"]))

    def test_mixed_scenario_matches_and_repeat_run_is_idempotent(self):
        """两轮必须逐轮对照：第 n 轮旧实现 == 第 n 轮新实现。

        只在两套新建库上各跑一轮、再比较各自首轮快照，无法发现"第二轮悄悄
        追加原因"这类污染 —— 那正是本用例上一版漏掉的东西。
        """
        legacy, current = _db(**MIXED), _db(**MIXED)
        self.addCleanup(legacy.close)
        self.addCleanup(current.close)

        observed = []
        for round_no in range(2):
            expected = run(legacy, legacy=True)
            actual = run(current, legacy=False)
            self.assertRoundEqual(expected, actual, label=f"round {round_no + 1}")
            observed.append(actual)

        first, second = observed
        # 跨轮：账本不再变化，且第二轮没有产生任何 UPDATE。
        self.assertEqual(first["state"], second["state"])
        self.assertFalse([sql for sql in second["sql"] if sql.startswith("UPDATE")])
        # 索引三态：首轮创建，次轮报告已存在；第二轮没有可对账的行。
        self.assertEqual(PSM.SIGNAL_RETRY_INDEX_CREATED, first["result"]["retry_index"])
        self.assertEqual(PSM.SIGNAL_RETRY_INDEX_PRESENT, second["result"]["retry_index"])
        self.assertEqual(0, second["result"]["orders_reconciled"])

    def test_missing_tables_short_circuits_identically(self):
        results = []
        for legacy in (True, False):
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            conn.row_factory = sqlite3.Row
            conn.executescript("CREATE TABLE paper_orders(id INTEGER PRIMARY KEY);")
            if legacy:
                result = _legacy_namespace()["_reconcile_signal_order_states"](conn)
            else:
                result = PT._reconcile_signal_order_states(conn)
            results.append(result)
        self.assertEqual(results[0], results[1])
        self.assertEqual("reconciliation_tables_not_ready", results[1]["reason"])
        self.assertIsNone(results[1]["retry_index"])


class ReconcileOwnershipTests(unittest.TestCase):
    """操作级边界：三条写入在 owner，判定/释放/顺序在编排层。"""

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

    def test_reconcile_has_no_inline_order_or_signal_writes(self):
        source = inspect.getsource(PT._reconcile_signal_order_states)
        self.assertEqual(set(), self.inline_writes(source))
        self.assertEqual({"paper_orders"}, self.inline_writes(
            source + '\n    conn.execute("UPDATE paper_orders SET status=?")\n'))
        self.assertEqual({"paper_signals"}, self.inline_writes(
            source + '\n    conn.execute("UPDATE paper_signals SET status=?")\n'))

    def test_owners_have_narrow_signatures(self):
        self.assertEqual(["conn", "order_id"],
                         list(inspect.signature(POL.supersede_duplicate_execution_retry).parameters))
        sig = inspect.signature(POL.supersede_for_terminal_signal)
        self.assertEqual(["conn", "order_id", "reason", "statuses"], list(sig.parameters))
        for name in ("reason", "statuses"):
            self.assertEqual(inspect.Parameter.KEYWORD_ONLY, sig.parameters[name].kind)
            self.assertEqual(inspect.Parameter.empty, sig.parameters[name].default)
        sig = inspect.signature(SIG.reconcile_signal_status)
        self.assertEqual(["conn", "signal_id", "status"], list(sig.parameters))
        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, sig.parameters["status"].kind)
        self.assertEqual(inspect.Parameter.empty, sig.parameters["status"].default)

    def test_terminal_owner_only_touches_the_statuses_it_is_given(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, status TEXT, reason TEXT)")
        conn.executemany("INSERT INTO paper_orders VALUES(?,?,?)",
                         [(1, "pending_limit", None), (2, "awaiting_batch", None)])
        POL.supersede_for_terminal_signal(conn, 1, reason="回收", statuses=("pending_limit",))
        rows = dict(conn.execute("SELECT id,status FROM paper_orders").fetchall())
        self.assertEqual("superseded", rows[1])
        self.assertEqual("awaiting_batch", rows[2])

    def test_write_sets_and_pending_status_unchanged(self):
        import test_ledger_ownership as TLO2
        for table, expected in (("paper_orders", 3), ("paper_signals", 2)):
            self.assertEqual(expected, len(TLO2.real_write_modules(table)),
                             f"{table} 真实写方模块数变化")
            self.assertEqual("pending", TLO.OWNERSHIP[table]["status"])
        self.assertIn("paper_order_ledger", TLO.OWNERSHIP["paper_orders"]["writers"])
        self.assertIn("signal_service", TLO.OWNERSHIP["paper_signals"]["writers"])

    def test_owners_are_exported(self):
        self.assertIn("supersede_duplicate_execution_retry", POL.__all__)
        self.assertIn("supersede_for_terminal_signal", POL.__all__)
        self.assertIn("reconcile_signal_status", SIG.__all__)

    def test_owners_do_not_read_the_clock(self):
        conn = _db(**DRIFT)
        with mock.patch.object(EPD, "_now", side_effect=AssertionError("owner read clock")), \
                mock.patch.object(SIG, "_now", create=True,
                                  side_effect=AssertionError("owner read clock")):
            POL.supersede_duplicate_execution_retry(conn, 999)
            POL.supersede_for_terminal_signal(conn, 999, reason="x",
                                              statuses=ELC.ENTRY_RETRY_ORDER_STATUSES)
            SIG.reconcile_signal_status(conn, 999, status="pending")


if __name__ == "__main__":
    unittest.main()
