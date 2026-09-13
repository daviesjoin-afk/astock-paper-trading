# -*- coding: utf-8 -*-
"""周期所有权 / 执行参与者解析边界契约测试（Extract Cycle Ownership Resolver）。

本文件锁住六件事：

1. **经济所有权口径**：``enabled_strategies`` ∩ ``paper_accounts.cycle_id ==
   目标周期``；lifecycle pause **不**改变这个集合，注册表 active **不得**替代它。
2. **执行资格口径**：经济所有权 − lifecycle pause；pause 立即失效、resume 恢复
   且**不**凭空放大资本。
3. **idle 与未配置不合并**：``enabled_strategies == []`` 是合法零策略周期
   （零参与者，绝不回落内置五套）；``NULL`` / 不可解析才是「未配置」，保留
   legacy 回退。
4. **只读**：解析器不写库、不联网、不下单、不建周期、不做风控决策。
5. **兼容 facade 只允许别名 / 委托**：``paper_trading`` 内同名符号不得出现
   第二套实现；注入的注册表作用域必须**调用时**读取。
6. **单一实现**：全仓生产模块中，周期所有权谓词的 SQL 只存在于
   ``paper_cycle_ownership``。
"""
from __future__ import annotations

import ast
import copy
import json
import os
import pathlib
import sqlite3
import unittest
from unittest import mock

import paper_cycle_ownership as PCY
import paper_trading as PT
import user_strategy_participation as USP

BACKEND = os.path.dirname(os.path.abspath(__file__))
OWNERSHIP_PATH = pathlib.Path(BACKEND, "paper_cycle_ownership.py")
PAPER_PATH = pathlib.Path(BACKEND, "paper_trading.py")

# 合成作用域（测试用字面量，故意不取 ``PT.ACTIVE_ACCOUNT_IDS``：模块的
# ``builtin_scope`` 是**注入参数**，契约必须由字面量独立锁定）。
SCOPE = ("tq_breakout", "trend_pullback")
USER_A = "own_user_a"
USER_B = "own_user_b"

# 冻结 golden：参与者解析版本标签（抽取前后必须一致）。
GOLDEN_PARTICIPANT_VERSION = "cycle-participant-v1"
GOLDEN_PAUSED_STATUSES = ("paused",)

# ---------------------------------------------------------------------------
# 架构守卫常量
# ---------------------------------------------------------------------------
ALLOWED_IMPORT_ROOTS = {
    "__future__", "json", "sqlite3", "paper_account_specs", "user_strategy_participation",
}
FORBIDDEN_IMPORT_ROOTS = {
    "paper_trading", "paper_storage", "paper_repository", "paper_schema_migrations",
    "strategy_registry", "strategy_runtime", "strategy_service", "dashboard_queries",
    "order_intent", "execution_planner", "execution_dispatch", "entry_lifecycle",
    "manual_orders", "paper_slot_service", "paper_runner", "paper_cycle_service",
    "main", "api_paper", "requests", "urllib", "http", "socket", "threading",
    "subprocess", "asyncio", "fastapi", "starlette",
}

FORBIDDEN_CALL_ATTRS = {
    "connect", "executemany", "executescript", "commit", "rollback",
    "urlopen", "request", "place_order", "submit_order", "start", "run_slot",
    "transition", "create_user_definition", "save_definition", "bind_cycle_versions",
    "start_new_cycle", "configure_capital", "set_accounts_status", "generate_signals",
    "_ensure_cycle", "archive_cycle", "insert", "update", "delete",
}

# 写操作 SQL 前缀（只认「整条语句以写动词开头」的字符串常量，避免散文误报）。
WRITE_SQL_PREFIXES = ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
                      "REPLACE", "TRUNCATE", "PRAGMA")

# 所有权模块不得携带的执行 / 风控 / 开户 / 归档权威名字。
FORBIDDEN_AUTHORITY_NAMES = {
    "remaining_qty", "paper_position_lots", "paper_positions", "_risk_exit_account_ids",
    "risk_exit", "_debit_shared_cash", "_credit_shared_cash", "paper_capital_reservations",
    "_ensure_user_strategy_accounts", "_ensure_cycle", "_create_cycle",
    "_archive_current_cycle", "start_new_cycle", "configure_capital",
    "set_accounts_status", "generate_signals", "run_slot", "ACTIVE_ACCOUNT_IDS",
    "ACTIVE_ACCOUNT_SPECS", "_active_account_clause", "_active_cycle_filter",
    "_shared_account_rows", "_shared_cash", "_shared_initial_cash",
}

# 兼容 facade 必须委托到的目标（`paper_trading` 内同名符号 → 模块函数）。
FACADE_DELEGATIONS = {
    "_lifecycle_paused_ids": "PCY.lifecycle_paused_ids",
    "_active_cycle_filter": "PCY.cycle_ledger_filter",
    "_shared_account_rows": "PCY.cycle_ledger_rows",
    "cycle_ledger_ids": "PCY.cycle_ledger_ids",
    "execution_participant_ids": "PCY.execution_participant_ids",
    "current_cycle_participant_ids": "PCY.current_cycle_participant_ids",
    "_cycle_participant_resolution": "PCY.cycle_participant_resolution",
}

# facade 体内**不得**出现的第二套实现标记（模块里必须有，见非空性测试）。
OWNERSHIP_MARKERS = (
    "SELECT enabled_strategies FROM paper_cycles",
    "SELECT id,enabled_strategies FROM paper_cycles",
    "cycle_enabled_unbound_fallback",
    "cycle_not_configured",
    "cycle_idle",
    "no_cycle",
    "no_conn",
)

SQLITE_WRITE_ACTIONS = {
    getattr(sqlite3, name)
    for name in ("SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE",
                 "SQLITE_CREATE_TABLE", "SQLITE_DROP_TABLE", "SQLITE_ALTER_TABLE",
                 "SQLITE_CREATE_INDEX", "SQLITE_DROP_INDEX")
    if hasattr(sqlite3, name)
}

# 解析面（模块内私有名）：只允许在所有权模块中定义。
MODULE_ONLY_FUNCTION_NAMES = (
    "cycle_ledger_filter", "cycle_ledger_rows", "cycle_participant_resolution",
    "lifecycle_paused_ids",
)

# 公开解析名：所有权模块定义 + `paper_trading` 兼容 facade，其它模块禁止。
PUBLIC_RESOLVER_NAMES = (
    "cycle_ledger_ids", "current_cycle_participant_ids", "execution_participant_ids",
)


# ---------------------------------------------------------------------------
# AST 辅助
# ---------------------------------------------------------------------------
def _parse(path):
    return ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))


def _import_roots(tree):
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _module_level_statements(tree):
    return list(tree.body)


def _find_assign(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node
    return None


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _executable_source(func):
    """函数体源码（**去掉 docstring**），避免文档措辞触发代码守卫。"""
    node = copy.deepcopy(func)
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        node.body = body[1:]
    return ast.unparse(node)


def _string_constants(tree):
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _iter_backend_sources():
    for name in sorted(os.listdir(BACKEND)):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        yield name, os.path.join(BACKEND, name)


# ---------------------------------------------------------------------------
# 合成账本 fixture（只建解析器真正读的三张表）
# ---------------------------------------------------------------------------
def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE paper_cycles(
            id INTEGER PRIMARY KEY, status TEXT, capital REAL, enabled_strategies TEXT
        );
        CREATE TABLE paper_accounts(
            id TEXT PRIMARY KEY, cycle_id INTEGER, status TEXT,
            cash REAL, initial_cash REAL
        );
        CREATE TABLE strategy_definitions(
            id TEXT PRIMARY KEY, origin TEXT, lifecycle_status TEXT,
            supports_new_cycle INTEGER
        );
        CREATE TABLE paper_position_lots(
            account_id TEXT, remaining_qty REAL
        );
        """
    )
    return conn


def _cycle(conn, cycle_id, enabled, status="running"):
    """``enabled``: list → JSON；``None`` → SQL NULL；str → 原样写入（损坏数据）。"""
    raw = json.dumps(enabled) if isinstance(enabled, list) else enabled
    conn.execute(
        "INSERT INTO paper_cycles(id,status,capital,enabled_strategies) VALUES(?,?,?,?)",
        (cycle_id, status, 300000.0, raw),
    )


def _account(conn, account_id, cycle_id, *, status="running", cash=0.0, initial_cash=0.0):
    """幂等写入 / 换绑（周期切换会把同一账户行改挂到新周期）。"""
    conn.execute(
        "INSERT INTO paper_accounts(id,cycle_id,status,cash,initial_cash) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET cycle_id=excluded.cycle_id, status=excluded.status, "
        "cash=excluded.cash, initial_cash=excluded.initial_cash",
        (account_id, cycle_id, status, cash, initial_cash),
    )


def _registry(conn, strategy_id, *, origin="user", lifecycle_status="active",
              supports_new_cycle=1):
    conn.execute(
        "INSERT INTO strategy_definitions(id,origin,lifecycle_status,supports_new_cycle) "
        "VALUES(?,?,?,?)",
        (strategy_id, origin, lifecycle_status, supports_new_cycle),
    )


def _ledger(conn, cycle_id):
    return PCY.cycle_ledger_ids(conn, cycle_id, builtin_scope=SCOPE)


def _resolve(conn, cycle_id=None):
    return PCY.cycle_participant_resolution(conn, cycle_id, builtin_scope=SCOPE)


def _participants(conn, cycle_id=None):
    return PCY.current_cycle_participant_ids(conn, cycle_id, builtin_scope=SCOPE)


# ---------------------------------------------------------------------------
# 1) 经济所有权（Cycle owns capital）
# ---------------------------------------------------------------------------
class EconomicOwnershipContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_ownership_is_enabled_intersected_with_cycle_binding(self):
        """启用集合 ∩ 周期挂接：只启用未挂接、只挂接未启用都不算所有权。"""
        _registry(self.conn, USER_A)
        _registry(self.conn, USER_B)
        _cycle(self.conn, 1, [USER_A, USER_B, "never_bound"])
        _account(self.conn, USER_A, 1)
        _account(self.conn, USER_B, 1)
        _account(self.conn, "bound_but_not_enabled", 1)
        self.assertEqual((USER_A, USER_B), _ledger(self.conn, 1))

    def test_account_bound_to_another_cycle_cannot_enter_ownership(self):
        """``paper_accounts.cycle_id == 目标周期`` 是硬约束。"""
        _registry(self.conn, USER_A)
        _registry(self.conn, USER_B)
        _cycle(self.conn, 1, [USER_A, USER_B])
        _account(self.conn, USER_A, 1)
        _account(self.conn, USER_B, 1)
        _cycle(self.conn, 2, [USER_A, USER_B], status="draft")
        _account(self.conn, USER_B, 2)  # 同一 id 不可能同时挂两周期 → 模拟换绑
        self.assertEqual((USER_A,), _ledger(self.conn, 1))
        self.assertEqual((USER_B,), _ledger(self.conn, 2))

    def test_ownership_never_substitutes_registry_active_scope(self):
        """注册表 active 只回答"下一周期能否启用"，不得当成本期所有权。"""
        _registry(self.conn, USER_A)
        _registry(self.conn, "registry_active_but_not_enabled")
        _cycle(self.conn, 1, [USER_A])
        _account(self.conn, USER_A, 1)
        _account(self.conn, "registry_active_but_not_enabled", 1)
        self.assertEqual((USER_A,), _ledger(self.conn, 1))

    def test_lifecycle_pause_does_not_remove_economic_ownership(self):
        _registry(self.conn, USER_A)
        _registry(self.conn, USER_B)
        _cycle(self.conn, 1, [USER_A, USER_B])
        _account(self.conn, USER_A, 1, cash=150000.0, initial_cash=150000.0)
        _account(self.conn, USER_B, 1, cash=150000.0, initial_cash=150000.0)
        self.assertIn(USER_A, _ledger(self.conn, 1))
        self.conn.execute(
            "UPDATE strategy_definitions SET lifecycle_status='paused', "
            "supports_new_cycle=0 WHERE id=?", (USER_A,),
        )
        # 经济账本不变，且账户行未被改写。
        self.assertIn(USER_A, _ledger(self.conn, 1))
        row = self.conn.execute(
            "SELECT * FROM paper_accounts WHERE id=?", (USER_A,)
        ).fetchone()
        self.assertEqual(1, row["cycle_id"])
        self.assertAlmostEqual(150000.0, float(row["cash"]), places=2)
        self.assertAlmostEqual(150000.0, float(row["initial_cash"]), places=2)

    def test_explicit_empty_enabled_set_is_an_idle_ledger(self):
        _cycle(self.conn, 1, [])
        _account(self.conn, USER_A, 1)
        self.assertEqual((), _ledger(self.conn, 1))
        clause, params = PCY.cycle_ledger_filter(self.conn, 1, "id", builtin_scope=SCOPE)
        self.assertEqual("1=0", clause)
        self.assertEqual((), params)

    def test_unconfigured_enabled_set_keeps_migration_fallback(self):
        """未配置周期：账本未齐 → 1=1 保留迁移期现金对账；账本已齐 → 严格过滤。"""
        _cycle(self.conn, 1, None)
        _account(self.conn, "only_one_sleeve", 1)
        clause, params = PCY.cycle_ledger_filter(self.conn, 1, "id", builtin_scope=SCOPE)
        self.assertEqual("1=1", clause)
        self.assertEqual((), params)
        self.assertEqual(("only_one_sleeve",), _ledger(self.conn, 1))

        _cycle(self.conn, 2, None)
        for account_id in SCOPE:
            _account(self.conn, account_id, 2)
        clause, params = PCY.cycle_ledger_filter(self.conn, 2, "id", builtin_scope=SCOPE)
        self.assertEqual(SCOPE, params)
        self.assertIn("IN (", clause)
        self.assertEqual(SCOPE, _ledger(self.conn, 2))

    def test_corrupt_enabled_set_is_treated_as_unconfigured(self):
        """损坏 JSON ≠ 显式空集：走 legacy 迁移回退，不得当成 idle。"""
        _cycle(self.conn, 1, "{not-json")
        _account(self.conn, "only_one_sleeve", 1)
        clause, _params = PCY.cycle_ledger_filter(self.conn, 1, "id", builtin_scope=SCOPE)
        self.assertEqual("1=1", clause)

    def test_ledger_rows_are_ordered_by_id(self):
        _registry(self.conn, USER_A)
        _registry(self.conn, USER_B)
        _cycle(self.conn, 1, [USER_B, USER_A])
        _account(self.conn, USER_B, 1)
        _account(self.conn, USER_A, 1)
        rows = PCY.cycle_ledger_rows(self.conn, 1, builtin_scope=SCOPE)
        self.assertEqual([USER_A, USER_B], [row["id"] for row in rows])
        self.assertEqual((USER_A, USER_B), _ledger(self.conn, 1))

    def test_row_reader_does_not_assume_the_connection_row_factory(self):
        """公共模块不得假设 ``row_factory``（裸连接也必须可用）。"""
        bare = sqlite3.connect(":memory:")
        self.addCleanup(bare.close)
        bare.executescript(
            "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, status TEXT, capital REAL, "
            "enabled_strategies TEXT);"
            "CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, cycle_id INTEGER, status TEXT, "
            "cash REAL, initial_cash REAL);"
            "CREATE TABLE strategy_definitions(id TEXT PRIMARY KEY, origin TEXT, "
            "lifecycle_status TEXT, supports_new_cycle INTEGER);"
        )
        _registry(bare, USER_A)
        _cycle(bare, 1, [USER_A])
        _account(bare, USER_A, 1, cash=10.0)
        _account(bare, "bound_but_not_enabled", 1, cash=20.0)
        self.assertEqual((USER_A,), PCY.cycle_ledger_ids(bare, 1, builtin_scope=SCOPE))
        rows = PCY.cycle_ledger_rows(bare, 1, builtin_scope=SCOPE)
        self.assertEqual([{"id": USER_A, "cycle_id": 1, "status": "running",
                           "cash": 10.0, "initial_cash": 0.0}], rows)
        self.assertIsNone(bare.row_factory)

    def test_bare_connection_execution_resolution_uses_cycle_snapshot(self):
        """执行解析的周期快照读取也必须兼容裸连接。"""
        bare = sqlite3.connect(":memory:")
        self.addCleanup(bare.close)
        bare.executescript(
            "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, status TEXT, capital REAL, "
            "enabled_strategies TEXT);"
            "CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, cycle_id INTEGER, status TEXT, "
            "cash REAL, initial_cash REAL);"
            "CREATE TABLE strategy_definitions(id TEXT PRIMARY KEY, origin TEXT, "
            "lifecycle_status TEXT, supports_new_cycle INTEGER);"
        )
        _registry(bare, USER_A)
        _cycle(bare, 7, [USER_A])
        _account(bare, USER_A, 7)
        resolution = PCY.cycle_participant_resolution(bare, 7, builtin_scope=SCOPE)
        self.assertEqual("cycle_snapshot", resolution["source"])
        self.assertEqual((USER_A,), resolution["ids"])
        self.assertEqual(7, resolution["cycle_id"])
        self.assertEqual((USER_A,), resolution["enabled"])
        self.assertEqual(frozenset({USER_A}), resolution["bound"])
        self.assertIsNone(bare.row_factory)


# ---------------------------------------------------------------------------
# 2) 执行资格（Lifecycle controls execution permission）
# ---------------------------------------------------------------------------
class ExecutionParticipationContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)
        _registry(self.conn, USER_A)
        _registry(self.conn, USER_B)
        _cycle(self.conn, 1, [USER_A, USER_B])
        _account(self.conn, USER_A, 1, cash=150000.0, initial_cash=150000.0)
        _account(self.conn, USER_B, 1, cash=150000.0, initial_cash=150000.0)

    def _pause(self, strategy_id):
        self.conn.execute(
            "UPDATE strategy_definitions SET lifecycle_status='paused' WHERE id=?",
            (strategy_id,),
        )

    def _resume(self, strategy_id):
        self.conn.execute(
            "UPDATE strategy_definitions SET lifecycle_status='active' WHERE id=?",
            (strategy_id,),
        )

    def test_pause_removes_execution_permission_but_not_ownership(self):
        self.assertEqual((USER_A, USER_B), _participants(self.conn, 1))
        self._pause(USER_A)
        self.assertEqual((USER_B,), _participants(self.conn, 1))
        self.assertEqual((USER_A, USER_B), _ledger(self.conn, 1))
        resolution = _resolve(self.conn, 1)
        self.assertEqual(frozenset({USER_A}), resolution["paused"])
        self.assertEqual("cycle_snapshot", resolution["source"])

    def test_pause_does_not_unbind_or_clear_the_ledger_row(self):
        self._pause(USER_A)
        row = self.conn.execute(
            "SELECT * FROM paper_accounts WHERE id=?", (USER_A,)
        ).fetchone()
        self.assertEqual(1, row["cycle_id"])
        self.assertEqual("running", row["status"])
        self.assertAlmostEqual(150000.0, float(row["cash"]), places=2)
        self.assertAlmostEqual(150000.0, float(row["initial_cash"]), places=2)
        cycle = self.conn.execute(
            "SELECT enabled_strategies FROM paper_cycles WHERE id=1"
        ).fetchone()
        self.assertEqual([USER_A, USER_B], json.loads(cycle["enabled_strategies"]))

    def test_resume_restores_execution_without_minting_capital(self):
        before = self.conn.execute(
            "SELECT SUM(initial_cash) AS total FROM paper_accounts WHERE cycle_id=1"
        ).fetchone()["total"]
        self._pause(USER_A)
        self.assertEqual((USER_B,), _participants(self.conn, 1))
        self._resume(USER_A)
        self.assertEqual((USER_A, USER_B), _participants(self.conn, 1))
        after = self.conn.execute(
            "SELECT SUM(initial_cash) AS total FROM paper_accounts WHERE cycle_id=1"
        ).fetchone()["total"]
        self.assertAlmostEqual(float(before), float(after), places=2)

    def test_builtin_and_user_strategies_share_the_same_contract(self):
        _registry(self.conn, "tq_breakout", origin="builtin")
        _cycle(self.conn, 2, ["tq_breakout", USER_A])
        _account(self.conn, "tq_breakout", 2)
        _account(self.conn, USER_A, 2)
        self.assertEqual(("tq_breakout", USER_A), _participants(self.conn, 2))
        self._pause(USER_A)
        self.assertEqual(("tq_breakout",), _participants(self.conn, 2))
        self.assertEqual({"tq_breakout", USER_A}, set(_ledger(self.conn, 2)))

    def test_participant_order_is_deterministic_and_follows_enabled_order(self):
        _cycle(self.conn, 2, [USER_B, USER_A])
        _account(self.conn, USER_B, 2)
        _account(self.conn, USER_A, 2)
        for _ in range(3):
            self.assertEqual((USER_B, USER_A), _participants(self.conn, 2))
            self.assertEqual((USER_A, USER_B), _ledger(self.conn, 2))

    def test_resolution_metadata_is_preserved(self):
        self._pause(USER_B)
        resolution = _resolve(self.conn, 1)
        self.assertEqual((USER_A,), resolution["ids"])
        self.assertEqual("cycle_snapshot", resolution["source"])
        self.assertEqual((USER_A, USER_B), resolution["enabled"])
        self.assertEqual(frozenset({USER_A, USER_B}), resolution["bound"])
        self.assertEqual(frozenset({USER_B}), resolution["paused"])
        self.assertEqual(1, resolution["cycle_id"])
        self.assertEqual(GOLDEN_PARTICIPANT_VERSION, resolution["version"])

    def test_resolution_prefers_the_latest_active_cycle_when_cycle_id_is_omitted(self):
        _cycle(self.conn, 2, [USER_A], status="paused")
        _account(self.conn, USER_A, 2)
        self.assertEqual((USER_A,), _participants(self.conn))
        self.assertEqual(2, _resolve(self.conn)["cycle_id"])

    def test_registry_table_missing_is_tolerated(self):
        """注册表尚未建表：内置能力位仍可解析，pause 集合为空且不抛异常。"""
        conn = _make_conn()
        self.addCleanup(conn.close)
        conn.execute("DROP TABLE strategy_definitions")
        _cycle(conn, 1, [SCOPE[0]])
        _account(conn, SCOPE[0], 1)
        self.assertEqual((SCOPE[0],), _participants(conn, 1))
        self.assertEqual(frozenset(), _resolve(conn, 1)["paused"])
        self.assertEqual(frozenset(), PCY.lifecycle_paused_ids(conn))
        self.assertEqual((SCOPE[0],), _ledger(conn, 1))


# ---------------------------------------------------------------------------
# 3) idle 与 legacy 回退（两种语义不得合并）
# ---------------------------------------------------------------------------
class IdleAndLegacyFallbackContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_explicit_empty_enabled_set_yields_zero_participants(self):
        _cycle(self.conn, 1, [])
        resolution = _resolve(self.conn, 1)
        self.assertEqual("cycle_idle", resolution["source"])
        self.assertEqual((), resolution["ids"])
        self.assertEqual((), _participants(self.conn, 1))
        self.assertEqual((), _ledger(self.conn, 1))

    def test_idle_cycle_never_falls_back_to_builtin_scope(self):
        _registry(self.conn, USER_A)
        _cycle(self.conn, 1, [])
        _account(self.conn, SCOPE[0], 1)
        _account(self.conn, USER_A, 1)
        self.assertEqual((), _participants(self.conn, 1))
        for account_id in (*SCOPE, USER_A):
            self.assertNotIn(account_id, _participants(self.conn, 1))

    def test_unconfigured_cycle_preserves_legacy_fallback(self):
        _registry(self.conn, USER_A)
        _cycle(self.conn, 1, None)
        resolution = _resolve(self.conn, 1)
        self.assertEqual("cycle_not_configured", resolution["source"])
        self.assertEqual(tuple([*SCOPE, USER_A]), resolution["ids"])
        self.assertEqual(tuple([*SCOPE, USER_A]), _participants(self.conn, 1))

    def test_unconfigured_cycle_does_not_query_user_participants_without_connection(self):
        resolution = PCY.cycle_participant_resolution(None, 1, builtin_scope=SCOPE)
        self.assertEqual("no_conn", resolution["source"])
        self.assertEqual(SCOPE, resolution["ids"])
        self.assertIsNone(resolution["cycle_id"])
        self.assertEqual(frozenset(), resolution["bound"])
        self.assertEqual(GOLDEN_PARTICIPANT_VERSION, resolution["version"])

    def test_missing_cycle_preserves_legacy_fallback(self):
        _registry(self.conn, USER_A)
        resolution = _resolve(self.conn, 999)
        self.assertEqual("no_cycle", resolution["source"])
        self.assertEqual(tuple([*SCOPE, USER_A]), resolution["ids"])
        self.assertIsNone(resolution["cycle_id"])

    def test_unbound_cycle_falls_back_to_enabled_set_without_paused(self):
        """周期已声明启用集合但账本尚未挂接（首轮 / 迁移窗口）。"""
        _registry(self.conn, USER_A)
        _registry(self.conn, USER_B)
        _cycle(self.conn, 1, [USER_A, USER_B])
        self.conn.execute(
            "UPDATE strategy_definitions SET lifecycle_status='paused' WHERE id=?", (USER_B,)
        )
        resolution = _resolve(self.conn, 1)
        self.assertEqual("cycle_enabled_unbound_fallback", resolution["source"])
        self.assertEqual((USER_A,), resolution["ids"])
        self.assertEqual(frozenset(), resolution["bound"])
        self.assertEqual((USER_A, USER_B), resolution["enabled"])

    def test_explicit_idle_and_unconfigured_are_not_merged(self):
        _cycle(self.conn, 1, [])
        _cycle(self.conn, 2, None)
        self.assertEqual("cycle_idle", _resolve(self.conn, 1)["source"])
        self.assertEqual("cycle_not_configured", _resolve(self.conn, 2)["source"])
        self.assertNotEqual(_resolve(self.conn, 1)["source"], _resolve(self.conn, 2)["source"])

    def test_capability_bits_use_known_user_ids_not_lifecycle(self):
        """能力位判定用 ``user_known_ids``：paused / 非 active 用户策略仍在账本。"""
        _registry(self.conn, USER_A, lifecycle_status="paused", supports_new_cycle=0)
        _cycle(self.conn, 1, [USER_A])
        _account(self.conn, USER_A, 1)
        self.assertEqual((USER_A,), _ledger(self.conn, 1))
        self.assertEqual((), _participants(self.conn, 1))
        self.assertIn(USER_A, USP.user_known_ids(self.conn))
        self.assertNotIn(USER_A, USP.user_participant_ids(self.conn))


# ---------------------------------------------------------------------------
# 4) 只读（不写库）
# ---------------------------------------------------------------------------
class ReadOnlyResolverTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)
        _registry(self.conn, USER_A)
        _registry(self.conn, USER_B)
        _cycle(self.conn, 1, [USER_A, USER_B])
        _account(self.conn, USER_A, 1, cash=1.0, initial_cash=1.0)
        _account(self.conn, USER_B, 1, cash=2.0, initial_cash=2.0)

    def _snapshot(self):
        return {
            table: [tuple(row) for row in self.conn.execute(f"SELECT * FROM {table}")]
            for table in ("paper_cycles", "paper_accounts", "strategy_definitions")
        }

    def test_resolution_does_not_mutate_the_database(self):
        seen = []

        def _authorizer(action, arg1, arg2, dbname, source):
            if action in SQLITE_WRITE_ACTIONS:
                seen.append(action)
            return sqlite3.SQLITE_OK

        self.conn.set_authorizer(_authorizer)
        try:
            before = self._snapshot()
            _ledger(self.conn, 1)
            _participants(self.conn, 1)
            _resolve(self.conn, 1)
            PCY.cycle_ledger_filter(self.conn, 1, "id", builtin_scope=SCOPE)
            PCY.cycle_ledger_rows(self.conn, 1, builtin_scope=SCOPE)
            PCY.lifecycle_paused_ids(self.conn)
            PCY.cycle_participant_resolution(None, 1, builtin_scope=SCOPE)
        finally:
            self.conn.set_authorizer(None)
        self.assertEqual([], seen, "只读解析器不得发出任何写操作")
        self.assertEqual(before, self._snapshot())


# ---------------------------------------------------------------------------
# 5) 兼容 facade
# ---------------------------------------------------------------------------
class CompatibilityFacadeTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_version_constants_are_aliases(self):
        self.assertEqual(GOLDEN_PARTICIPANT_VERSION, PCY.CYCLE_PARTICIPANT_VERSION)
        self.assertEqual(GOLDEN_PARTICIPANT_VERSION, PT._CYCLE_PARTICIPANT_VERSION)
        self.assertEqual(GOLDEN_PAUSED_STATUSES, PCY.LIFECYCLE_PAUSED_STATUSES)
        self.assertEqual(GOLDEN_PAUSED_STATUSES, PT._LIFECYCLE_PAUSED_STATUSES)

    def test_paper_trading_exposes_every_compatibility_name(self):
        for name in FACADE_DELEGATIONS:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(PT, name, None)), f"{name} 兼容入口缺失")

    def test_facade_functions_delegate_to_the_ownership_module(self):
        tree = _parse(PAPER_PATH)
        for name, target in FACADE_DELEGATIONS.items():
            with self.subTest(name=name):
                func = _find_function(tree, name)
                self.assertIsNotNone(func, f"{name} 兼容 facade 缺失")
                self.assertIn(target, ast.unparse(func), f"{name} 必须委托给 {target}")

    def test_facade_bodies_hold_no_second_ownership_implementation(self):
        tree = _parse(PAPER_PATH)
        for name in FACADE_DELEGATIONS:
            with self.subTest(name=name):
                body = _executable_source(_find_function(tree, name))
                for marker in OWNERSHIP_MARKERS:
                    self.assertNotIn(marker, body, f"{name} 内联了第二套所有权实现")
                for marker in ("1=0", "1=1", "IN (", "strategy_definitions",
                               "paper_accounts", "paper_cycles"):
                    self.assertNotIn(marker, body, f"{name} 内联了第二套所有权实现：{marker}")

    def test_facade_reads_the_registry_scope_at_call_time(self):
        """``ACTIVE_ACCOUNT_IDS`` 必须在函数体内读取，patch 后立即生效。"""
        _cycle(self.conn, 1, None)
        with mock.patch.object(PT, "ACTIVE_ACCOUNT_IDS", ("patched_sleeve",)):
            resolution = PT._cycle_participant_resolution(self.conn, 1)
            self.assertEqual("cycle_not_configured", resolution["source"])
            self.assertEqual(("patched_sleeve",), resolution["ids"])
            self.assertEqual(("patched_sleeve",), PT.current_cycle_participant_ids(self.conn, 1))
        self.assertNotEqual(("patched_sleeve",), PT.current_cycle_participant_ids(self.conn, 1))

    def test_facade_delegation_is_behaviourally_identical_to_the_module(self):
        _registry(self.conn, USER_A)
        _cycle(self.conn, 1, [USER_A, SCOPE[0]])
        _account(self.conn, USER_A, 1, cash=1.0)
        _account(self.conn, SCOPE[0], 1, cash=2.0)
        self.assertEqual(
            PCY.cycle_ledger_ids(self.conn, 1, builtin_scope=PT.ACTIVE_ACCOUNT_IDS),
            PT.cycle_ledger_ids(self.conn, 1),
        )
        self.assertEqual(
            PCY.execution_participant_ids(self.conn, 1, builtin_scope=PT.ACTIVE_ACCOUNT_IDS),
            PT.execution_participant_ids(self.conn, 1),
        )
        self.assertEqual(
            PCY.current_cycle_participant_ids(self.conn, 1, builtin_scope=PT.ACTIVE_ACCOUNT_IDS),
            PT.current_cycle_participant_ids(self.conn, 1),
        )
        self.assertEqual(
            PCY.cycle_participant_resolution(self.conn, 1, builtin_scope=PT.ACTIVE_ACCOUNT_IDS),
            PT._cycle_participant_resolution(self.conn, 1),
        )

    def test_facade_cycle_selection_still_owns_the_write_side_helper(self):
        """``_active_cycle``（可能触发旧库补周期）必须留在 ``paper_trading``。"""
        tree = _parse(PAPER_PATH)
        self.assertIsNotNone(_find_function(tree, "_active_cycle"))
        self.assertIsNotNone(_find_function(tree, "_ensure_cycle"))
        self.assertIsNone(_find_function(_parse(OWNERSHIP_PATH), "_active_cycle"))
        self.assertIsNone(_find_function(_parse(OWNERSHIP_PATH), "_ensure_cycle"))


# ---------------------------------------------------------------------------
# 6) 架构守卫
# ---------------------------------------------------------------------------
class OwnershipArchitectureGuardTests(unittest.TestCase):
    def test_ownership_module_never_imports_paper_trading(self):
        roots = _import_roots(_parse(OWNERSHIP_PATH))
        self.assertNotIn("paper_trading", roots)

    def test_ownership_module_imports_only_allowlisted_roots(self):
        roots = _import_roots(_parse(OWNERSHIP_PATH))
        self.assertTrue(roots, "守卫非空性：至少应有 import")
        self.assertEqual(set(), roots - ALLOWED_IMPORT_ROOTS)

    def test_ownership_module_declares_no_forbidden_dependency(self):
        roots = _import_roots(_parse(OWNERSHIP_PATH))
        self.assertEqual(set(), roots & FORBIDDEN_IMPORT_ROOTS)

    def test_ownership_module_emits_no_write_sql(self):
        values = _string_constants(_parse(OWNERSHIP_PATH))
        offenders = [
            value for value in values
            if value.strip().upper().startswith(WRITE_SQL_PREFIXES)
        ]
        self.assertEqual([], offenders, "只读解析器不得携带任何写 SQL / PRAGMA")
        # 非空性：模块确实带 SELECT（否则上面的扫描无意义）。
        selects = [value for value in values if value.strip().upper().startswith("SELECT")]
        self.assertTrue(selects, "守卫非空性：模块应带只读 SELECT")

    def test_ownership_module_performs_no_forbidden_calls(self):
        offenders = []
        for node in ast.walk(_parse(OWNERSHIP_PATH)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_CALL_ATTRS:
                    offenders.append(f"{node.lineno}: .{node.func.attr}(")
        self.assertEqual([], offenders, "所有权模块不得下单 / 建周期 / 开户 / 归档 / 改状态")

    def test_ownership_module_carries_no_execution_or_risk_authority(self):
        offenders = []
        for node in ast.walk(_parse(OWNERSHIP_PATH)):
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_AUTHORITY_NAMES:
                offenders.append(f"{node.lineno}: {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_AUTHORITY_NAMES:
                offenders.append(f"{node.lineno}: .{node.attr}")
        self.assertEqual([], offenders, "所有权模块不得携带执行 / 风控退出 / 开户 / 归档权威")

    def test_ownership_module_has_no_import_time_side_effects(self):
        offenders = []
        for node in _module_level_statements(_parse(OWNERSHIP_PATH)):
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign,
                                 ast.FunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # docstring
            offenders.append(f"{node.lineno}: {type(node).__name__}")
        self.assertEqual([], offenders)

    def test_module_level_assignments_contain_no_calls(self):
        offenders = []
        for node in _module_level_statements(_parse(OWNERSHIP_PATH)):
            if not isinstance(node, ast.Assign):
                continue
            for child in ast.walk(node.value):
                if isinstance(child, ast.Call):
                    offenders.append(f"{node.lineno}: {ast.unparse(child)}")
        self.assertEqual([], offenders, "import 本模块不得执行任何调用（不改变运行时状态）")

    def test_risk_exit_eligibility_stays_in_the_authoritative_layer(self):
        """风控退出 = 执行参与者 ∪ 仍有剩余 lots 的账户；不得并入所有权模块。"""
        tree = _parse(PAPER_PATH)
        func = _find_function(tree, "_risk_exit_account_ids")
        self.assertIsNotNone(func, "_risk_exit_account_ids facade 必须留在 paper_trading")
        body = ast.unparse(func)
        self.assertIn("risk_exit_account_ids", body)
        self.assertIsNone(_find_function(_parse(OWNERSHIP_PATH), "_risk_exit_account_ids"))
        ownership_src = OWNERSHIP_PATH.read_text(encoding="utf-8")
        self.assertNotIn("paper_position_lots", ownership_src)
        self.assertNotIn("remaining_qty", ownership_src)

    def test_registry_active_scope_stays_in_the_authoritative_layer(self):
        tree = _parse(PAPER_PATH)
        assign = _find_assign(tree, "ACTIVE_ACCOUNT_IDS")
        self.assertIsNotNone(assign, "ACTIVE_ACCOUNT_IDS 必须由 paper_trading 定义")
        source = ast.unparse(assign)
        self.assertIn("SR.active_ids", source)
        ownership = _parse(OWNERSHIP_PATH)
        assigned = {
            target.id
            for node in ast.walk(ownership) if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        }
        self.assertNotIn("ACTIVE_ACCOUNT_IDS", assigned)
        self.assertNotIn("ACTIVE_ACCOUNT_SPECS", assigned)

    # ---- 单一实现守卫（非空性先证存在，再证别处没有） ---------------------
    def test_detector_finds_the_ownership_sql_in_the_ownership_module(self):
        values = _string_constants(_parse(OWNERSHIP_PATH))
        hits = [value for value in values if "paper_cycles" in value and "enabled_strategies" in value]
        self.assertTrue(hits, "守卫非空性：所有权模块必须带周期快照 SQL")
        self.assertTrue(
            any("SELECT id,enabled_strategies FROM paper_cycles" in value for value in hits)
        )
        self.assertTrue(
            any("SELECT enabled_strategies FROM paper_cycles" in value for value in hits)
        )

    def test_cycle_ownership_sql_lives_only_in_the_ownership_module(self):
        """全仓唯一：读取周期快照启用集合的 SELECT 只允许出现在所有权模块。"""
        offenders = []
        for name, path in _iter_backend_sources():
            if name == OWNERSHIP_PATH.name:
                continue
            for value in _string_constants(_parse(path)):
                upper = value.upper()
                if "SELECT" in upper and "PAPER_CYCLES" in upper and "ENABLED_STRATEGIES" in upper:
                    offenders.append(f"{name}: {value!r}")
        self.assertEqual([], offenders, "周期所有权谓词只能在 paper_cycle_ownership 中实现")

    def test_no_other_module_redefines_the_resolution_surface(self):
        offenders = []
        for name, path in _iter_backend_sources():
            if name == OWNERSHIP_PATH.name:
                continue
            tree = _parse(path)
            for candidate in MODULE_ONLY_FUNCTION_NAMES:
                if _find_function(tree, candidate) is not None:
                    offenders.append(f"{name}.{candidate}")
            if name == PAPER_PATH.name:
                continue  # 兼容 facade 允许同名（上面已断言其必须委托）
            for candidate in PUBLIC_RESOLVER_NAMES:
                if _find_function(tree, candidate) is not None:
                    offenders.append(f"{name}.{candidate}")
        self.assertEqual([], offenders, "解析面只允许在 paper_cycle_ownership 中定义")

    def test_every_facade_target_exists_in_the_ownership_module(self):
        for name, target in FACADE_DELEGATIONS.items():
            with self.subTest(name=name):
                attr = target.split(".", 1)[1]
                self.assertTrue(callable(getattr(PCY, attr, None)),
                                f"{name} 的委托目标 {target} 不存在")


# ---------------------------------------------------------------------------
# 7) 风控退出资格 ≠ 执行资格（本模块**不**拥有这条口径）
# ---------------------------------------------------------------------------
class RiskExitEligibilityTests(unittest.TestCase):
    """风控退出 = 执行参与者 ∪ 仍有剩余 lots 的账户。

    ``_risk_exit_account_ids`` 留在 ``paper_trading``，消费迁出后的解析器；
    绝不能被"统一"成 ``execution_participant_ids``——否则 paused / 已退出
    当前周期的存量持仓会变成无人风控的孤儿敞口。
    """

    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def _hold(self, account_id, qty):
        self.conn.execute(
            "INSERT INTO paper_position_lots(account_id,remaining_qty) VALUES(?,?)",
            (account_id, qty),
        )

    def test_paused_account_with_remaining_lots_keeps_risk_exit_eligibility(self):
        _registry(self.conn, USER_A)
        _registry(self.conn, USER_B)
        _cycle(self.conn, 1, [USER_A, USER_B])
        _account(self.conn, USER_A, 1)
        _account(self.conn, USER_B, 1)
        self.conn.execute(
            "UPDATE strategy_definitions SET lifecycle_status='paused' WHERE id=?", (USER_A,)
        )
        self._hold(USER_A, 100.0)
        self.assertNotIn(USER_A, _participants(self.conn, 1))
        risk_ids = PT._risk_exit_account_ids(self.conn)
        self.assertIn(USER_A, risk_ids, "paused 但有存量 lots 的账户必须继续被风控扫描")
        self.assertIn(USER_B, risk_ids)

    def test_account_retired_from_the_cycle_still_gets_risk_exit(self):
        _registry(self.conn, USER_A, lifecycle_status="archived", supports_new_cycle=0)
        _registry(self.conn, USER_B)
        _cycle(self.conn, 1, [USER_B])  # A 已退出当前周期
        _account(self.conn, USER_A, 1, status="running")
        _account(self.conn, USER_B, 1)
        self._hold(USER_A, 50.0)
        self.assertNotIn(USER_A, _participants(self.conn, 1))
        self.assertIn(USER_A, PT._risk_exit_account_ids(self.conn))

    def test_flat_account_outside_the_cycle_is_not_risk_exit_eligible(self):
        """既非执行参与者、也无剩余 lots → 不在风控退出名单里。"""
        _registry(self.conn, USER_A, lifecycle_status="archived", supports_new_cycle=0)
        _registry(self.conn, USER_B)
        _cycle(self.conn, 1, [USER_B])
        _account(self.conn, USER_A, 1, status="running")
        _account(self.conn, USER_B, 1)
        self._hold(USER_A, 0.0)
        self.assertNotIn(USER_A, PT._risk_exit_account_ids(self.conn))
        self.assertIn(USER_B, PT._risk_exit_account_ids(self.conn))


if __name__ == "__main__":
    unittest.main()
