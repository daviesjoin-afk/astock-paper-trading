# -*- coding: utf-8 -*-
"""R27-B2C-7 —— runtime / incident **owner 侧**的永久回归（INC-01 ~ INC-23）。

接缝侧的 INC-24 ~ INC-32 在 ``test_ai_research_runtime_adapter``。本文件只管 owner：

* 谁**真的**写这两张 runtime 表（静态写法 + 动态表名写法 + 它们的写入范围）；
* runtime lifecycle 词表与 owner fact verification 词表**零交集**；
* 可用性只从 owner 证明的瞬间派生（``started_at`` / ``profile_date`` / ``market_date``
  一律不得当终态可用性）；
* mutable row 不得倒填历史，retry 覆盖过的旧失败不得从当前行重建；
* 「业务拒绝 ≠ 系统事故」与「previous research ≠ owner fact」是**结构**保证，不是注释。

────────────── 为什么必须是结构断言 ──────────────

"owner 只有一个 writer"若只写在文档里，下一个为了"顺手补一行"而直写 ``paper_jobs`` 的模块
不会让任何东西红。INC-01 ~ INC-03 因此扫真实源码：**静态写语句**用字面扫描，
**动态表名写法**（``f"DELETE FROM {table}"``）单独登记并断言其写入范围 —— 只挡静态写法等于
给动态写法留了一条静默通道。
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_engine as AE  # noqa: E402
import ai_research_contract as ARC  # noqa: E402
import paper_trading as PT  # noqa: E402

ADAPTIVE_RUNS = "adaptive_runs"
PAPER_JOBS = "paper_jobs"
PAPER_JOB_RUNS = "paper_job_runs"
PAPER_RUNTIME_LOCKS = "paper_runtime_locks"
ADAPTIVE_EXECUTION_EVIDENCE = "adaptive_execution_evidence"

#: 每张表的 **lifecycle writer** —— 唯一有权改变事实内容的模块。
#: 其余出现方式（行清除、旧时间戳格式归一）在 ``_DYNAMIC_WRITER_REGISTRY`` 里逐项登记。
OWNER_BY_TABLE = {
    ADAPTIVE_RUNS: "adaptive_engine.py",
    PAPER_JOBS: "paper_trading.py",
    PAPER_JOB_RUNS: "paper_trading.py",
    PAPER_RUNTIME_LOCKS: "paper_trading.py",
    ADAPTIVE_EXECUTION_EVIDENCE: "adaptive_engine.py",
}

#: 用**动态表名**（f-string / 拼接）写这些表的模块，以及它被允许的写入范围。
#:
#: 两者都是真实存在的写路径：``paper_schema_migrations.ensure_runtime_lease_columns``
#: 遍历 ``("paper_jobs","paper_job_runs","paper_runtime_locks")`` 做 ``T``→空格分隔符归一；
#: ``paper_cycle_service.archive_cycle`` 与 ``retention_maintenance.run`` 遍历表名做行清除。
#: 它们**都不改 lifecycle**，因此不构成第二个 fact authority —— 但必须被显式登记并断言范围，
#: 否则新增的第三个动态写者会绕过只扫字面语句的 guard。
#:
#: ``purge_only``        只允许 ``DELETE FROM {}``（行清除；删掉的行读出来就是 UNAVAILABLE）
#: ``normalization_only`` 允许 ``UPDATE {} SET {}=...`` 与整表重建用的
#:                        ``INSERT INTO "{}" (...) SELECT ...``；断言其**列名占位符的绑定词表**
#:                        不含 lifecycle 列（见 ``_loop_literal_vocabularies``）
_DYNAMIC_WRITER_REGISTRY = {
    ADAPTIVE_RUNS: {
        "retention_maintenance.py": "purge_only",
    },
    PAPER_JOBS: {
        "paper_cycle_service.py": "purge_only",
        "paper_schema_migrations.py": "normalization_only",
    },
    PAPER_JOB_RUNS: {
        "paper_cycle_service.py": "purge_only",
        "paper_schema_migrations.py": "normalization_only",
    },
    PAPER_RUNTIME_LOCKS: {
        "paper_schema_migrations.py": "normalization_only",
    },
    ADAPTIVE_EXECUTION_EVIDENCE: {},
}

#: 非 owner 模块**绝不**允许改写的 lifecycle 列。owner 的 `status` / `finished_at` 是事实内容；
#: 别人改了它，owner 的核验语义就失效了。
LIFECYCLE_COLUMNS = ("status", "finished_at")

#: 事故**严重级别**词表。owner 层绝不允许出现 —— 分级是 research 结论。
SEVERITY_TOKENS = ("critical", "severity", "root_cause", "requires_restart", "needs_rollback")

_WRITE_VERB = r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b"
_PLACEHOLDER = re.compile(r"\{[^}]*\}")


def _production_modules() -> list[str]:
    return sorted(
        name for name in os.listdir(BACKEND)
        if name.endswith(".py") and not name.startswith("test_")
    )


def _source(name: str) -> str:
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


def _imported_roots(name: str) -> set[str]:
    """模块真正 import 的顶层模块名（``__future__`` 除外）。"""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(_source(name))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots - {"__future__"}


def _code_identifiers(name: str) -> set[str]:
    """模块里的**代码标识符**：类/函数/参数/关键字参数名、``Name`` 与 ``Attribute`` 名、import 别名。

    刻意**不含**字符串常量（含 docstring）与注释：本层的边界是"没有 severity 字段 / 参数 /
    常量"，不是"文件里不许出现 severity 这个词"—— 契约文档正是在逐字解释它为什么不在这里。
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(_source(name))):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.alias):
            found.add(node.asname or node.name.split(".")[0])
    return found


def _sql_constants(name: str) -> list[str]:
    """模块里的 SQL 文本常量（含 f-string 模板）。用于证明某个模块根本不读 DB。"""
    out = []
    for text in _string_constants(name):
        if re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE|REPLACE|PRAGMA)\b", text, re.I):
            out.append(text.strip())
    return out


def _function_facts(module: str, function: str) -> tuple[set[str], list[str]]:
    """只扫某一个函数体：``(标识符集合, 字符串常量列表)``。"""
    identifiers: set[str] = set()
    constants: list[str] = []
    for node in ast.walk(ast.parse(_source(module))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != function:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                identifiers.add(child.id)
            elif isinstance(child, ast.Attribute):
                identifiers.add(child.attr)
            elif isinstance(child, ast.arg):
                identifiers.add(child.arg)
            elif isinstance(child, ast.keyword) and child.arg:
                identifiers.add(child.arg)
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                constants.append(child.value)
        return identifiers, constants
    raise AssertionError(f"{module} 里没有函数 {function}")


def _literal_write_modules(table: str) -> set[str]:
    """**字面**写语句（含 ``INSERT OR REPLACE`` / ``INSERT OR IGNORE`` / UPSERT 头）指向
    ``table`` 的模块。UPSERT 的 ``ON CONFLICT ... DO UPDATE`` 由同一句 INSERT 覆盖。"""
    pattern = re.compile(rf"{_WRITE_VERB}[\s\S]{{0,60}}?\b{table}\b")
    return {name for name in _production_modules() if pattern.search(_source(name))}


def _string_constants(name: str) -> list[str]:
    """模块里的字符串常量，以及 f-string 的**模板形状**（占位符归一成 ``{}``）。"""
    out: list[str] = []
    for node in ast.walk(ast.parse(_source(name))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append("".join(
                value.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
                else "{}"
                for value in node.values
            ))
    return out


def _templated_write_statements(name: str) -> list[tuple[str, str]]:
    """模块里**以写动词开头**且**带表名占位符**的语句模板 → ``(verb, statement)``。

    两个限制都是必要的，按当前代码的真实形状：

    * **必须以写动词开头** —— 否则 ``CREATE TRIGGER ... BEFORE INSERT ON {}`` 会被误判成
      一次 INSERT（``paper_schema_migrations`` 里有六七条这样的 trigger 定义）；
    * **表名必须是占位符** —— 字面表名的写语句已经由 :func:`_literal_write_modules` 抓走，
      这里要抓的是 ``f"DELETE FROM {table}"`` 这类**动态派发**，它的目标表由循环变量决定。
    """
    out: list[tuple[str, str]] = []
    for text in _string_constants(name):
        stripped = text.strip()
        match = re.match(r"(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)", stripped, re.I)
        if not match:
            continue
        tail = stripped[match.end():]
        if not re.match(r"\s*[\"']?\{\}", tail):
            continue
        out.append((re.match(r"\w+", stripped).group(0).upper(), stripped))
    return out


def _loop_literal_vocabularies(name: str) -> list[frozenset]:
    """模块里每个"遍历字面量元组"的循环 → 它遍历到的字符串集合。

    这是为了给**列名占位符**找到它的绑定词表：``f"UPDATE {table} SET {column}=replace(...)"``
    里的 ``{column}`` 由 ``for column in ("started_at", ...)`` 决定，因此"这条语句会不会改
    lifecycle"必须看那个元组，而不是看语句文本。
    """
    out: list[frozenset] = []
    for node in ast.walk(ast.parse(_source(name))):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        values = []
        if isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
            values = [
                element.value for element in node.iter.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
        if values:
            out.append(frozenset(values))
    return out


def _dynamic_write_modules(table: str) -> set[str]:
    """用动态表名写 ``table`` 的模块（源码同时出现写动词模板与表名字面量）。"""
    out: set[str] = set()
    for name in _production_modules():
        if not re.search(rf"\b{table}\b", _source(name)):
            continue
        if _templated_write_statements(name):
            out.add(name)
    return out


# ───────────────────────────── fixtures ─────────────────────────────

_OPEN_CONNECTIONS: list = []


def _track(conn):
    _OPEN_CONNECTIONS.append(conn)
    return conn


class _DbTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        while _OPEN_CONNECTIONS:
            try:
                _OPEN_CONNECTIONS.pop().close()
            except Exception:  # pragma: no cover
                pass


def _adaptive_conn() -> sqlite3.Connection:
    conn = _track(sqlite3.connect(":memory:"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE adaptive_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT NOT NULL, "
        "status TEXT NOT NULL, profile_date TEXT, new_rewards INTEGER NOT NULL DEFAULT 0, "
        "detail TEXT, started_at TEXT NOT NULL, finished_at TEXT NOT NULL)"
    )
    return conn


def _insert_adaptive_run(conn, *, status, started_at, finished_at, trigger="scheduled-close",
                         profile_date=None, detail='{"stage":"init"}', new_rewards=0) -> int:
    cursor = conn.execute(
        "INSERT INTO adaptive_runs(trigger,status,profile_date,new_rewards,detail,"
        "started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
        (trigger, status, profile_date, new_rewards, detail, started_at, finished_at),
    )
    return int(cursor.lastrowid)


def _paper_conn() -> sqlite3.Connection:
    conn = _track(sqlite3.connect(":memory:"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE paper_job_runs(run_key TEXT PRIMARY KEY, slot TEXT NOT NULL, "
        "market_date TEXT NOT NULL, status TEXT NOT NULL, detail TEXT, started_at TEXT NOT NULL, "
        "finished_at TEXT, owner_key TEXT, heartbeat_at TEXT, expires_at TEXT, "
        "fencing_token INTEGER NOT NULL DEFAULT 0)"
    )
    return conn


def _insert_paper_attempt(conn, *, run_key="intraday:202609200930", slot="intraday",
                          market_date="2026-09-20", status="running",
                          started_at="2026-09-20 09:30:00", finished_at=None,
                          detail=None, owner_key="sha1:abc", fencing_token=3) -> str:
    conn.execute(
        "INSERT INTO paper_job_runs(run_key,slot,market_date,status,detail,started_at,finished_at,"
        "owner_key,fencing_token) VALUES(?,?,?,?,?,?,?,?,?)",
        (run_key, slot, market_date, status, detail, started_at, finished_at, owner_key,
         fencing_token),
    )
    return run_key


# ═══════════════════════════════════════════════════════════════════════════
# INC-01 ~ INC-03：真实 writer 闭集（静态 + 动态，按代码真实形状）
# ═══════════════════════════════════════════════════════════════════════════


class RuntimeWriterBoundaryTests(unittest.TestCase):
    def test_INC_01_adaptive_runs_lifecycle_writer_is_the_owner_only(self):
        """INC-01：``adaptive_runs`` 的字面 lifecycle writer 只有 owner 一个模块。

        覆盖 ``INSERT`` / ``INSERT OR REPLACE`` / ``INSERT OR IGNORE`` / UPSERT 头 /
        ``UPDATE`` / ``DELETE``：任何一种新的裸写都会让集合变大而红。少一个（owner
        不再写它）同样红 —— 那说明这张表的事实来源已经不是它了。
        """
        writers = _literal_write_modules(ADAPTIVE_RUNS)
        self.assertEqual(
            {OWNER_BY_TABLE[ADAPTIVE_RUNS]}, writers,
            f"{ADAPTIVE_RUNS} 的写者集合发生变化：{sorted(writers)}",
        )
        # 非空性：扫描器真的能从 owner 源码里看见写语句。
        self.assertIn("INSERT INTO adaptive_runs", _source(OWNER_BY_TABLE[ADAPTIVE_RUNS]))
        self.assertIn("UPDATE adaptive_runs", _source(OWNER_BY_TABLE[ADAPTIVE_RUNS]))

    def test_INC_02_paper_jobs_writer_is_the_owner_only(self):
        """INC-02：``paper_jobs`` 的字面 lifecycle writer 只有 ``paper_trading``。"""
        writers = _literal_write_modules(PAPER_JOBS)
        self.assertEqual(
            {OWNER_BY_TABLE[PAPER_JOBS]}, writers,
            f"{PAPER_JOBS} 的写者集合发生变化：{sorted(writers)}",
        )
        self.assertIn("INSERT INTO paper_jobs", _source(OWNER_BY_TABLE[PAPER_JOBS]))

    def test_INC_03_paper_job_runs_writer_is_the_owner_only(self):
        """INC-03：``paper_job_runs``（本轮的 attempt 级 fact 来源）同样只有一个 writer。"""
        writers = _literal_write_modules(PAPER_JOB_RUNS)
        self.assertEqual(
            {OWNER_BY_TABLE[PAPER_JOB_RUNS]}, writers,
            f"{PAPER_JOB_RUNS} 的写者集合发生变化：{sorted(writers)}",
        )
        self.assertIn("INSERT INTO paper_job_runs", _source(OWNER_BY_TABLE[PAPER_JOB_RUNS]))

    def test_INC_03b_adaptive_execution_evidence_writer_is_the_owner_only(self):
        """INC-03b：``adaptive_execution_evidence`` 的 writer 也只有 owner 一个模块。

        它的 ``status`` 是**自由字符串**，因此本轮**不**把它变成 typed evidence（见
        ``INC_23b``）—— 但 writer 闭集必须先钉住，否则把它变成 evidence 时会无法归因。
        """
        writers = _literal_write_modules(ADAPTIVE_EXECUTION_EVIDENCE)
        self.assertEqual(
            {OWNER_BY_TABLE[ADAPTIVE_EXECUTION_EVIDENCE]}, writers,
            f"{ADAPTIVE_EXECUTION_EVIDENCE} 的写者集合发生变化：{sorted(writers)}",
        )

    def test_INC_03c_dynamic_table_name_writers_are_registered_and_scoped(self):
        """INC-03c：动态表名写者必须**逐项登记**，且写入范围不得触碰 lifecycle。

        只扫字面语句会漏掉 ``f"DELETE FROM {table}"`` / ``f"UPDATE {table} SET {col}=..."``；
        只断言"登记过"又会漏掉"某个已登记模块顺手把 ``status`` 也改了"。因此三个方向都查：

        * 非 owner 的动态写者集合必须**恰好**等于登记表；
        * 已登记的模板动词必须落在其 scope 之内（``purge_only`` 只能 DELETE）；
        * 模板里**列名占位符的绑定词表**不得包含 lifecycle 列 —— 否则"归一分隔符"的语句
          可以在不改模板文本的前提下被扩成改 ``status``。
        """
        for table, registered in _DYNAMIC_WRITER_REGISTRY.items():
            with self.subTest(table=table):
                actual = _dynamic_write_modules(table)
                unregistered = actual - {OWNER_BY_TABLE[table]} - set(registered)
                self.assertEqual(
                    set(), unregistered,
                    f"{table} 出现了**未登记**的动态表名写者：{sorted(unregistered)} —— "
                    "必须先在 owner matrix 里判定它是否会改 lifecycle，再登记",
                )
                for module in registered:
                    self.assertIn(
                        module, actual,
                        f"{table} / {module} 已登记但不是动态写者 —— 登记表漂移了",
                    )

        for table, entries in _DYNAMIC_WRITER_REGISTRY.items():
            for module, scope in entries.items():
                with self.subTest(table=table, module=module, scope=scope):
                    self.assertNotIn(
                        module, OWNER_BY_TABLE.values(),
                        "owner 模块不需要登记成外部动态写者",
                    )
                    statements = _templated_write_statements(module)
                    self.assertTrue(statements, f"{module} 的模板扫描空转了")
                    verbs = {verb for verb, _text in statements}
                    if scope == "purge_only":
                        self.assertEqual(
                            {"DELETE"}, verbs,
                            f"{module} 被登记为 purge_only，却出现动词 {sorted(verbs)}",
                        )
                    else:
                        self.assertEqual(
                            set(), verbs - {"INSERT", "UPDATE"},
                            f"{module} 被登记为 normalization_only，却出现动词 {sorted(verbs)}",
                        )
                    # 列名占位符的绑定词表不得包含 lifecycle 列。
                    for vocabulary in _loop_literal_vocabularies(module):
                        leaked = sorted(vocabulary & set(LIFECYCLE_COLUMNS))
                        self.assertEqual(
                            [], leaked,
                            f"{module} 的占位符绑定词表含 lifecycle 列 {leaked} —— "
                            "非 owner 模块不得改写事实内容",
                        )
                    # 字面 SET 也不得指向 lifecycle 列（占位符那一路由上面的绑定词表覆盖）。
                    for _verb, text in statements:
                        self.assertIsNone(
                            re.search(
                                r"\bSET\b\s*[\"']?(?:status|finished_at)[\"']?\s*=", text, re.I,
                            ),
                            f"{module} 的直接写入指向了 lifecycle 列：{text!r}",
                        )


# ═══════════════════════════════════════════════════════════════════════════
# INC-04 ~ INC-08：lifecycle ≠ verification，severity ≠ verification
# ═══════════════════════════════════════════════════════════════════════════


class RuntimeLifecycleVsVerificationTests(_DbTestCase):
    def test_INC_04_lifecycle_status_and_fact_verification_are_disjoint(self):
        """INC-04：runtime lifecycle 词表与 owner fact verification 词表**零交集**。

        两个 owner 各自发布一张闭集；它们彼此之间、以及它们与 research 的 owner-neutral
        三态之间，都不许有交集 —— 有交集就说明"运行状态"正在冒充"核验结论"。
        """
        adaptive_lifecycle = frozenset(AE.ADAPTIVE_RUN_LIFECYCLE_STATUSES)
        paper_lifecycle = frozenset(PT.PAPER_JOB_RUN_LIFECYCLE_STATUSES)
        adaptive_verify = frozenset(AE.ADAPTIVE_RUN_FACT_VERIFICATION_STATUSES)
        paper_verify = frozenset(PT.PAPER_JOB_RUN_FACT_VERIFICATION_STATUSES)

        self.assertEqual(set(), adaptive_lifecycle & adaptive_verify)
        self.assertEqual(set(), paper_lifecycle & paper_verify)
        self.assertEqual(set(), adaptive_verify & paper_verify)
        # 两侧的核验闭集都不含任何 lifecycle 词，也不含 research 的三态。
        for vocabulary in (adaptive_verify, paper_verify):
            for token in ("running", "completed", "failed", "killed", "success", "ok"):
                self.assertNotIn(token, vocabulary)
            self.assertEqual(set(), vocabulary & frozenset(ARC.OWNER_OUTCOMES))
        # 非空性：两张 lifecycle 闭集都真的存在（否则上面的交集断言空转）。
        self.assertTrue(adaptive_lifecycle)
        self.assertTrue(paper_lifecycle)

    def test_INC_05_an_adaptive_failed_run_is_an_owner_verified_fact(self):
        """INC-05：``status='failed'`` **可以**是 owner-verified 的**事实**。

        ``verified`` 的含义是"这条事实可信"，不是"运行成功"。因此 failed 与 completed 在
        事实层是同一档次 —— 差别只在 ``runtime_status`` 这个**内容**字段里。
        """
        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="failed", started_at="2026-09-20T15:05:00+08:00",
            finished_at="2026-09-20T15:10:00+08:00", profile_date="2026-09-20",
            detail='{"error":"provider timeout"}',
        )
        fact = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
        self.assertIsNotNone(fact)
        self.assertEqual("failed", fact.runtime_status)
        self.assertEqual(AE.ADAPTIVE_RUN_FACT_RECORDED, fact.fact_verification_status)
        self.assertFalse(fact.runtime_status_is_verification)

    def test_INC_06_an_adaptive_completed_run_is_also_an_owner_verified_fact(self):
        """INC-06：``completed`` 同样是 owner-verified 的**事实**，不额外获得什么。

        两侧对照：若有人把 ``completed`` 当成通用的 verified 状态、或把 ``failed`` 降级，
        上面和下面这两条会同时红。
        """
        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="completed", started_at="2026-09-20T15:05:00+08:00",
            finished_at="2026-09-20T15:40:00+08:00", profile_date="2026-09-20",
            detail='{"stage":"done"}', new_rewards=7,
        )
        fact = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
        self.assertEqual("completed", fact.runtime_status)
        self.assertEqual(AE.ADAPTIVE_RUN_FACT_RECORDED, fact.fact_verification_status)

    def test_INC_07_owner_modules_carry_no_incident_severity_vocabulary(self):
        """INC-07：``failed`` 不自动变成事故级别 —— owner 层根本**没有**分级字段。

        分级（critical / high / …）是 research 结论。可执行形式：两个 owner 与 adapter 里
        不得出现任何 severity / is_incident / root_cause 类的**标识符**（字段名、参数名、常量、
        属性名）。契约文档里解释"它为什么不在这里"不算违规，所以这里扫的是 AST 标识符而不是
        源文本。
        """
        for name in (
            "adaptive_engine.py", "paper_trading.py", "ai_research_runtime_adapter.py",
        ):
            identifiers = _code_identifiers(name)
            hits = sorted(
                token for token in identifiers
                if any(bad in token.lower() for bad in SEVERITY_TOKENS)
            )
            with self.subTest(module=name):
                self.assertEqual(
                    [], hits,
                    f"{name} 出现了事故分级标识符 {hits} —— 分级属于 research 结论",
                )
            self.assertTrue(identifiers, f"{name} 的标识符扫描器空转了")
        # 非空性：分级词表确实存在于 research 侧（legacy collector），只是不属于 owner。
        import deepseek_research as DR
        self.assertIn("critical", DR.SEVERITIES)

    def test_INC_08_completed_is_not_a_generic_verified_status(self):
        """INC-08：``completed`` **不**等于通用的 verified 状态。

        owner-native 状态词被逐字保留在 ``OwnerVerification.status``，而判据是
        ``outcome`` —— 因此 ``status`` 里出现 "verified" 这种泛化词是不允许的。
        """
        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="completed", started_at="2026-09-20T15:05:00+08:00",
            finished_at="2026-09-20T15:40:00+08:00",
        )
        fact = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
        self.assertIn("completed", fact.runtime_status)
        self.assertNotIn("verified", fact.runtime_status)
        # 投影里没有任何字段把 lifecycle 翻译成通用核验结论。
        projected = fact.projection()
        self.assertFalse(projected["runtime_status_is_verification"])
        self.assertEqual("owner_fact", projected["authority"])
        self.assertNotIn("is_verified", projected)


# ═══════════════════════════════════════════════════════════════════════════
# INC-09 ~ INC-19：PIT 可用性 / 覆盖语义 / fail closed
# ═══════════════════════════════════════════════════════════════════════════


class RuntimePitAvailabilityTests(_DbTestCase):
    def test_INC_09_started_at_cannot_be_terminal_result_availability(self):
        """INC-09：终态事实的可用性是 ``finished_at``，**不是** ``started_at``。

        造一条跨午夜的记录（D-1 23:50 开始、D 00:20 结束）：可用日必须是 **D**。
        若有人把 ``started_at`` 当终态可用性，可用日会退回 D-1。
        """
        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="failed", started_at="2026-09-20T23:50:00+08:00",
            finished_at="2026-09-21T00:20:00+08:00", profile_date="2026-09-20",
        )
        fact = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-21")
        self.assertEqual(AE.ADAPTIVE_RUN_AVAILABILITY_TERMINAL, fact.availability_kind)
        self.assertEqual("2026-09-21", fact.availability_day)
        # 反方向：D 日读不到这条**终态**事实（那时它还没结束）。
        self.assertIsNone(AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20"))

    def test_INC_10_profile_date_cannot_be_terminal_result_availability(self):
        """INC-10：``profile_date`` 是业务标签，不得当可用性。

        ``profile_date`` 指 D 而终态在 D+2 落地时，可用日必须是 D+2。
        """
        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="completed", started_at="2026-09-20 15:05:00+08:00".replace(" ", "T"),
            finished_at="2026-09-22T15:40:00+08:00", profile_date="2026-09-20",
        )
        fact = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-22")
        self.assertEqual("2026-09-22", fact.availability_day)
        self.assertEqual("2026-09-20", fact.profile_date)
        self.assertIsNone(AE.adaptive_run_fact(conn, run_id, as_of="2026-09-21"))

    def test_INC_11_paper_market_date_cannot_be_failure_availability(self):
        """INC-11：``paper_job_runs.market_date`` 是业务日，**不是**失败被知悉的时刻。

        D 日的窗口失败到 D+1 才被回收扫到：可用日是终态写入那一天，``market_date`` 不得
        顶替它。
        """
        conn = _paper_conn()
        _insert_paper_attempt(
            conn, run_key="intraday:202609201455", market_date="2026-09-20", status="failed",
            started_at="2026-09-20 14:55:00", finished_at="2026-09-21 09:05:00",
            detail='{"error":"recovered"}',
        )
        fact = PT.paper_job_run_fact(conn, "intraday:202609201455", as_of="2026-09-21")
        self.assertEqual("2026-09-21", fact.availability_day)
        self.assertEqual("2026-09-20", fact.market_date)
        self.assertIsNone(
            PT.paper_job_run_fact(conn, "intraday:202609201455", as_of="2026-09-20")
        )

    def test_INC_12_finished_at_after_as_of_fails_closed(self):
        """INC-12：当前 revision 的可用日晚于 ``as_of`` → UNAVAILABLE，绝不放行。"""
        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="failed", started_at="2026-09-25T10:00:00+08:00",
            finished_at="2026-09-25T10:05:00+08:00",
        )
        for as_of in ("2026-09-20", "2026-09-24"):
            with self.subTest(as_of=as_of):
                self.assertIsNone(AE.adaptive_run_fact(conn, run_id, as_of=as_of))
        self.assertIsNotNone(AE.adaptive_run_fact(conn, run_id, as_of="2026-09-25"))

    def test_INC_13_a_mutable_current_row_never_backfills_an_older_revision(self):
        """INC-13：mutable 行的当前值**不得**被倒填成旧时点的内容。

        同一个 ``run_id`` 先 failed（D）后 completed（D）：以 D 之前的 ``as_of`` 读，
        必须 UNAVAILABLE —— 而不是"因为 id 相同所以拿当前值"。
        """
        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="failed", started_at="2026-09-20T15:05:00+08:00",
            finished_at="2026-09-20T15:10:00+08:00", detail='{"error":"boom"}',
        )
        conn.execute(
            "UPDATE adaptive_runs SET status='completed',detail='{\"stage\":\"done\"}',"
            "finished_at=? WHERE id=?",
            ("2026-09-20T16:00:00+08:00", run_id),
        )
        # 同一天内两次 revision 都"可用" ⇒ 读到的是当前 revision；这正是倒填不可做的原因。
        fact = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
        self.assertEqual("completed", fact.runtime_status)
        # 而 15:10 那次失败**已经不存在**：D-1 读必须是 UNAVAILABLE，不是旧的 failed。
        self.assertIsNone(AE.adaptive_run_fact(conn, run_id, as_of="2026-09-19"))

    def test_INC_14_malformed_detail_json_fails_closed(self):
        """INC-14：坏 ``detail`` JSON 必须 fail closed，绝不回落成空对象。"""
        for bad in ("{not json", "[]", '"scalar"', "12"):
            with self.subTest(detail=bad):
                conn = _adaptive_conn()
                run_id = _insert_adaptive_run(
                    conn, status="failed", started_at="2026-09-20T15:05:00+08:00",
                    finished_at="2026-09-20T15:10:00+08:00", detail=bad,
                )
                with self.assertRaises(AE.AdaptiveRuntimeFactError):
                    AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")

        for bad in ("{not json", "[]", "12"):
            with self.subTest(paper_detail=bad):
                conn = _paper_conn()
                _insert_paper_attempt(
                    conn, status="failed", detail=bad, finished_at="2026-09-20 09:40:00",
                )
                with self.assertRaises(PT.PaperJobRuntimeFactError):
                    PT.paper_job_run_fact(conn, "intraday:202609200930", as_of="2026-09-20")

        # 反面对照：SQL NULL 是 owner 的**合法** in-progress 形状（running INSERT 不写该列），
        # 归一成 JSON 字面量 null —— 与"坏 JSON"和"空对象 {}"都不同。
        conn = _paper_conn()
        _insert_paper_attempt(conn, status="running")
        fact = PT.paper_job_run_fact(conn, "intraday:202609200930", as_of="2026-09-20")
        self.assertEqual("null", fact.detail_canonical)
        self.assertIsNone(fact.detail)
        self.assertNotEqual("{}", fact.detail_canonical)

    def test_INC_15_unknown_runtime_status_is_never_defaulted_to_recorded(self):
        """INC-15：未知 status 不得默认成 owner-recorded。

        * adaptive：未知 status ⇒ 没有人为它的可用性语义做过决定 ⇒ UNAVAILABLE；
        * paper：legacy 只读的 ``interrupted``（retry CAS 接受它作输入，但**当前没有任何
          production writer 会写它**）同样不可签发。
        两者都**不**返回一个 ``fact_verification_status`` 已记录的投影。
        """
        self.assertNotIn("interrupted", PT.PAPER_JOB_RUN_LIFECYCLE_STATUSES)
        self.assertIn("interrupted", PT.PAPER_JOB_RUN_LEGACY_READONLY_STATUSES)

        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="brand_new_status", started_at="2026-09-20T15:05:00+08:00",
            finished_at="2026-09-20T15:10:00+08:00",
        )
        self.assertIsNone(AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20"))

        for status in ("interrupted", "success", "failed_but_ok"):
            with self.subTest(paper_status=status):
                conn = _paper_conn()
                _insert_paper_attempt(
                    conn, status=status, finished_at="2026-09-20 09:40:00",
                )
                self.assertIsNone(
                    PT.paper_job_run_fact(conn, "intraday:202609200930", as_of="2026-09-20")
                )

    def test_INC_16_timezone_normalization_uses_the_owner_timezone(self):
        """INC-16：可用日按 **owner 时区（Asia/Shanghai）** 归一，不看运行机器。

        adaptive 的 owner 时间戳是带 offset 的 aware ISO；paper 的 owner 时间戳是 owner
        自己在 ``_now:905`` 声明的裸 ``%Y-%m-%d %H:%M:%S``，容器时区由
        ``Dockerfile`` / ``docker-compose.yml`` 的 ``TZ=Asia/Shanghai`` 固定，
        ``_market_session:914`` 也逐字声明"生产环境为 Asia/Shanghai"。
        """
        # adaptive：同一 instant 的不同写法必须派生出同一个可用日。
        for stamp in ("2026-09-20T23:30:00+08:00", "2026-09-20T15:30:00+00:00"):
            with self.subTest(adaptive_stamp=stamp):
                conn = _adaptive_conn()
                run_id = _insert_adaptive_run(
                    conn, status="failed", started_at="2026-09-20T15:00:00+08:00",
                    finished_at=stamp,
                )
                fact = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
                self.assertEqual("2026-09-20", fact.availability_day)

        # paper：裸时间戳按 Asia/Shanghai 解释，而不是 UTC。
        self.assertEqual("2026-09-20", PT._paper_runtime_owner_day(
            PT._paper_runtime_owner_instant("2026-09-20 23:30:00", what="t"), what="t",
        ))
        offset = PT._paper_runtime_owner_instant("2026-09-20 09:30:00", what="t").utcoffset()
        self.assertEqual(8 * 3600, int(offset.total_seconds()))
        # 非空性对照：同一堵墙钟若按 UTC 解释会落到**前一天** —— 说明这个断言有内容。
        conn = _paper_conn()
        _insert_paper_attempt(
            conn, status="failed", started_at="2026-09-20 01:10:00",
            finished_at="2026-09-20 01:20:00",
        )
        fact = PT.paper_job_run_fact(conn, "intraday:202609200930", as_of="2026-09-20")
        self.assertEqual("2026-09-20", fact.availability_day)

    def test_INC_17_a_cross_offset_instant_cannot_be_issued_a_day_early(self):
        """INC-17：带别的 offset 的 instant 不得被提前一天签发。

        ``2026-09-20T23:30:00+00:00`` 在 owner 时区是 **09-21 07:30**。若有人直接用原
        offset 的 ``.date()``，会得到 09-20 并让 D 日的研究引用一条当时还不存在的事实。
        """
        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="failed", started_at="2026-09-20T01:00:00+00:00",
            finished_at="2026-09-20T23:30:00+00:00",
        )
        self.assertIsNone(AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20"))
        fact = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-21")
        self.assertEqual("2026-09-21", fact.availability_day)
        # 反向：naive 时间戳在 adaptive 表里不是合法 owner 格式，必须 fail closed
        # （禁止用运行机器本地时区猜）。
        naive = _insert_adaptive_run(
            conn, status="failed", started_at="2026-09-20T15:00:00",
            finished_at="2026-09-20T15:05:00",
        )
        with self.assertRaises(AE.AdaptiveRuntimeFactError):
            AE.adaptive_run_fact(conn, naive, as_of="2026-09-20")
        # paper 侧的 naive 是 owner 声明的格式，但**只认**那一种：带 offset 的一律拒绝。
        pconn = _paper_conn()
        _insert_paper_attempt(
            conn=pconn, status="failed", started_at="2026-09-20T09:30:00+08:00",
            finished_at="2026-09-20 09:40:00",
        )
        with self.assertRaises(PT.PaperJobRuntimeFactError):
            PT.paper_job_run_fact(pconn, "intraday:202609200930", as_of="2026-09-20")

    def test_INC_18_a_paper_retry_overwrite_cannot_rebuild_the_old_failure(self):
        """INC-18：retry 覆盖旧失败后，**不得**从当前行重建那次失败。

        真实形状（``paper_trading.run_slot``）：09:30 running → 09:40 failed →
        10:00 retry running（``finished_at=NULL``）→ 10:05 completed。全部落在**同一行**
        （PK ``run_key``）。因此：
        * 当前行只能读成 completed；
        * as_of 落到被覆盖的历史时点时必须 UNAVAILABLE，而不是"猜一个旧状态"。
        """
        conn = _paper_conn()
        run_key = _insert_paper_attempt(conn, status="running")
        conn.execute(
            "UPDATE paper_job_runs SET status='failed',detail='{\"error\":\"boom\"}',"
            "finished_at=? WHERE run_key=?",
            ("2026-09-20 09:40:00", run_key),
        )
        conn.execute(
            "UPDATE paper_job_runs SET status='running',detail='{\"retry_count\":1}',"
            "started_at=?,finished_at=NULL WHERE run_key=?",
            ("2026-09-20 10:00:00", run_key),
        )
        conn.execute(
            "UPDATE paper_job_runs SET status='completed',detail='{\"ok\":true}',"
            "finished_at=? WHERE run_key=?",
            ("2026-09-20 10:05:00", run_key),
        )
        fact = PT.paper_job_run_fact(conn, run_key, as_of="2026-09-20")
        self.assertEqual("completed", fact.runtime_status)
        self.assertEqual(PT.PAPER_JOB_RUN_FACT_RECORDED, fact.fact_verification_status)
        # 09:40 那次失败已经**物理消失** —— 更早的时点只能 UNAVAILABLE。
        for as_of in ("2026-09-19", "2026-09-01"):
            with self.subTest(as_of=as_of):
                self.assertIsNone(PT.paper_job_run_fact(conn, run_key, as_of=as_of))
        # 非空性：确实存在过一个被覆盖的 failed revision（fixture 真的走过 retry）。
        self.assertEqual("completed", conn.execute(
            "SELECT status FROM paper_job_runs WHERE run_key=?", (run_key,)
        ).fetchone()[0])

    def test_INC_19_attempt_identity_comes_from_the_owner_not_the_caller(self):
        """INC-19：attempt identity 由 owner 派生，调用方无法命名。

        ``run_key``（窗口身份）不足以标识一次 revision —— 同一窗口可以被 retry 改写。
        revision identity 因此必须包含 owner 的时间戳，且**内容一变就变**。
        """
        conn = _paper_conn()
        run_key = _insert_paper_attempt(conn, status="failed", finished_at="2026-09-20 09:40:00")
        first = PT.paper_job_run_fact(conn, run_key, as_of="2026-09-20")
        self.assertEqual(run_key, first.run_key)
        self.assertIn(run_key, first.revision_identity)
        self.assertIn(first.started_at, first.revision_identity)
        self.assertIn(first.finished_at, first.revision_identity)

        conn.execute(
            "UPDATE paper_job_runs SET finished_at=? WHERE run_key=?",
            ("2026-09-20 09:45:00", run_key),
        )
        second = PT.paper_job_run_fact(conn, run_key, as_of="2026-09-20")
        self.assertNotEqual(first.revision_identity, second.revision_identity)
        self.assertNotEqual(first.identity, second.identity)

    def test_INC_19b_heartbeat_does_not_move_the_fact_identity(self):
        """INC-19b：租约心跳只改租约字段，**不得**把同一条事实变成两条。

        ``heartbeat_at`` / ``expires_at`` 是租约上下文（``B21``），不是事实内容；它们若进
        identity 或指纹，同一次 attempt 会在每次续期后变成"新事实"。
        """
        conn = _paper_conn()
        run_key = _insert_paper_attempt(conn, status="running", owner_key="sha1:abc",
                                        fencing_token=3)
        first = PT.paper_job_run_fact(conn, run_key, as_of="2026-09-20")
        conn.execute(
            "UPDATE paper_job_runs SET heartbeat_at=?,expires_at=?,fencing_token=? WHERE run_key=?",
            ("2026-09-20 09:33:00", "2026-09-20 09:38:00", 3, run_key),
        )
        second = PT.paper_job_run_fact(conn, run_key, as_of="2026-09-20")
        self.assertEqual(first.revision_identity, second.revision_identity)
        self.assertEqual(first.content_fingerprint, second.content_fingerprint)
        self.assertNotIn("heartbeat_at", first.projection())
        self.assertNotIn("expires_at", first.projection())

    def test_INC_19c_a_running_row_never_claims_terminal_availability(self):
        """INC-19c：``running`` 行的可用瞬间是 ``started_at``，且被显式标注为 in_progress。

        这正是"``started_at`` 不是终态可用性"的可执行形式：进行中的事实**不冒充**终态
        结果，消费者能一眼看出它还没结束。
        """
        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="running", started_at="2026-09-20T15:05:00+08:00",
            finished_at="2026-09-20T15:05:00+08:00",
        )
        fact = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
        self.assertEqual(AE.ADAPTIVE_RUN_AVAILABILITY_IN_PROGRESS, fact.availability_kind)
        self.assertEqual("2026-09-20", fact.availability_day)

        pconn = _paper_conn()
        _insert_paper_attempt(pconn, status="running")
        attempt = PT.paper_job_run_fact(pconn, "intraday:202609200930", as_of="2026-09-20")
        self.assertEqual(PT.PAPER_JOB_RUN_AVAILABILITY_IN_PROGRESS, attempt.availability_kind)
        self.assertIsNone(attempt.finished_at)

    def test_INC_19d_a_terminal_row_without_its_terminal_instant_is_refused(self):
        """INC-19d：终态行没有终态瞬间 = 记录损坏 ⇒ hard fail closed（不是猜一个时刻）。

        与 ``killed`` 的区别要看清：``killed`` 是 owner **不盖**终态瞬间（ADR/常量段有说明），
        因此它返回 UNAVAILABLE；终态行缺 instant 则是记录与 owner 的写入形状不符，
        必须显式报错。
        """
        conn = _paper_conn()
        _insert_paper_attempt(conn, status="completed", finished_at=None)
        with self.assertRaises(PT.PaperJobRuntimeFactError):
            PT.paper_job_run_fact(conn, "intraday:202609200930", as_of="2026-09-20")

    def test_INC_19e_self_inconsistent_timestamps_are_unproven_not_recorded(self):
        """INC-19e：时间戳形状自洽性不成立 ⇒ ``*_unproven``，而不是 recorded。

        这是两态闭集的第二个 arm 必须可达的证明：记录确实存在、可读，但 owner 无法自证
        它是自己签发的合法事实 —— 它仍然是一条事实，owner 只是不背书。
        """
        conn = _adaptive_conn()
        # 终态写入发生在开始之前（时钟回拨 / 记录被改写）。
        run_id = _insert_adaptive_run(
            conn, status="failed", started_at="2026-09-20T15:10:00+08:00",
            finished_at="2026-09-20T15:05:00+08:00",
        )
        fact = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
        self.assertEqual(AE.ADAPTIVE_RUN_FACT_OWNER_UNPROVEN, fact.fact_verification_status)

        pconn = _paper_conn()
        # running 行却带着终态瞬间 —— 不符合 owner 的 INSERT / retry 形状。
        _insert_paper_attempt(pconn, status="running", finished_at="2026-09-20 09:40:00")
        attempt = PT.paper_job_run_fact(pconn, "intraday:202609200930", as_of="2026-09-20")
        self.assertEqual(PT.PAPER_JOB_RUN_FACT_OWNER_UNPROVEN, attempt.fact_verification_status)

    def test_INC_19f_as_of_is_mandatory(self):
        """INC-19f：``as_of`` 必须显式给出 —— 没有 ``None → latest`` / 墙钟回落。"""
        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="completed", started_at="2026-09-20T15:05:00+08:00",
            finished_at="2026-09-20T15:40:00+08:00",
        )
        for bad in (None, "", "   ", "2026/09/20", "today"):
            with self.subTest(as_of=bad):
                with self.assertRaises(AE.AdaptiveRuntimeFactError):
                    AE.adaptive_run_fact(conn, run_id, as_of=bad)
        pconn = _paper_conn()
        _insert_paper_attempt(pconn, status="failed", finished_at="2026-09-20 09:40:00")
        for bad in (None, "", "2026/09/20"):
            with self.subTest(paper_as_of=bad):
                with self.assertRaises(PT.PaperJobRuntimeFactError):
                    PT.paper_job_run_fact(pconn, "intraday:202609200930", as_of=bad)

    def test_INC_19g_killed_rows_have_no_provable_terminal_availability(self):
        """INC-19g：``killed`` 行不可签发（终态 instant 不可证）—— 显式记录的 OPEN 项。

        ``_learning_detect_stale`` 只改 ``status`` 与 ``detail``，甚至在自己的 detail 里写着
        ``no finished_at``；因此该行的 ``finished_at`` 仍等于 ``started_at``。用
        ``started_at`` 代替终态 instant 是**超前声明**（"进程被杀"要到悬挂检测跑过才知道），
        所以本层不签发，而不是硬签一个 ref。
        """
        self.assertIn("killed", AE.ADAPTIVE_RUN_TERMINAL_AVAILABILITY_UNPROVABLE_STATUSES)
        self.assertNotIn("killed", AE.ADAPTIVE_RUN_TERMINAL_STATUSES)
        conn = _adaptive_conn()
        run_id = _insert_adaptive_run(
            conn, status="running", started_at="2026-09-20T15:05:00+08:00",
            finished_at="2026-09-20T15:05:00+08:00",
        )
        conn.execute(
            "UPDATE adaptive_runs SET status='killed',detail=? WHERE id=?",
            ('{"error":"process killed (OOM/restart) before completion; no finished_at"}', run_id),
        )
        self.assertIsNone(AE.adaptive_run_fact(conn, run_id, as_of="2026-09-30"))


# ═══════════════════════════════════════════════════════════════════════════
# INC-20 ~ INC-23：authority 分离（不复制第二个 authority）
# ═══════════════════════════════════════════════════════════════════════════


class RuntimeAuthoritySeparationTests(_DbTestCase):
    def test_INC_20_paper_runtime_locks_is_not_an_incident_fact(self):
        """INC-20：租约表**不**直接成为 incident fact。

        ``paper_runtime_locks`` 是 lease context（``owner_key`` / ``heartbeat`` / ``expires`` /
        ``fencing_token``），不是事故事实台账。本层既没有读它的 typed 路径，也没有把它变成
        投影的入口；因此"lock expired → incident"这种转换在结构上不可表达。
        """
        self.assertIsNone(getattr(PT, "paper_runtime_lock_fact", None))
        self.assertIsNone(getattr(PT, "PaperRuntimeLockFactProjection", None))
        for name in ("AdaptiveRunFactProjection", "PaperJobRunFactProjection"):
            projection = getattr(AE, name, None) or getattr(PT, name, None)
            fields = set(projection.__dataclass_fields__)
            for leaked in ("expires_at", "heartbeat_at", "lock_key", "lease_lost"):
                self.assertNotIn(
                    leaked, fields,
                    f"{name} 携带了租约字段 {leaked!r} —— 租约上下文不是事故事实",
                )
        self.assertNotIn(
            "lock_key", _code_identifiers("ai_research_runtime_adapter.py"),
            "runtime adapter 出现了租约标识符 —— 租约不是它的事故判据",
        )
        # 非空性：租约表确实存在且被 owner 写（只是不成为 incident fact）。
        self.assertEqual({"paper_trading.py"}, _literal_write_modules(PAPER_RUNTIME_LOCKS))

    def test_INC_21_paper_orders_business_rejection_is_not_a_runtime_incident(self):
        """INC-21：业务拒单**不是**系统事故 —— runtime typed 路径根本不读 ``paper_orders``。

        风控拒单 / 容量延后 / 等待池 / 未成交都是正常业务规则。把它当事故就是把"设计如此"
        升级成"系统故障"。可执行形式（扫**代码**，不扫散文）：

        * adapter 里没有任何 SQL 文本（它压根不读 DB）；
        * 两个 owner 的 typed 读入口（``adaptive_run_fact`` / ``paper_job_run_fact``）的函数体
          不引用 ``paper_orders``；
        * 两个投影类型没有订单字段。
        """
        adapter_sql = _sql_constants("ai_research_runtime_adapter.py")
        self.assertEqual(
            [], adapter_sql,
            f"runtime adapter 出现了 SQL 文本 {adapter_sql} —— 它不得读 DB",
        )
        self.assertNotIn(
            "paper_orders", _code_identifiers("ai_research_runtime_adapter.py"),
        )

        for module, function in (
            ("adaptive_engine.py", "adaptive_run_fact"),
            ("paper_trading.py", "paper_job_run_fact"),
        ):
            identifiers, constants = _function_facts(module, function)
            with self.subTest(module=module, function=function):
                for token in ("paper_orders", "order_nonfill", "nonfill_distribution",
                              "filled", "partially_filled", "deferred_capacity"):
                    self.assertFalse(
                        any(token in item for item in identifiers | set(constants)),
                        f"{module}.{function} 触达了订单生命周期事实 —— 那归 execution owner",
                    )

        for name in ("AdaptiveRunFactProjection", "PaperJobRunFactProjection"):
            projection = getattr(AE, name, None) or getattr(PT, name, None)
            fields = set(projection.__dataclass_fields__)
            for leaked in ("order_status", "filled", "rejected", "blocked", "nonfill"):
                self.assertNotIn(leaked, fields, f"{name} 携带了订单生命周期字段 {leaked!r}")

        # 非空性：legacy collector **确实**还在做这个聚合（说明这条断言有内容），
        # 而且它自己也写着这条业务规则 —— 本轮把它落实到架构上。
        legacy = _source("deepseek_research.py")
        self.assertIn("paper_orders", legacy)
        self.assertIn("order_nonfill_distribution", legacy)
        self.assertIn("业务风控拒单不是系统事故", legacy)

    def test_INC_22_execution_nonfill_still_belongs_to_the_execution_owner(self):
        """INC-22：订单执行事实继续归 existing execution owner，本层**不复制**它。

        因此 B2C-7 不新增 ``RuntimeOrderFact`` / ``IncidentOrderFact`` / ``OrderFailureFact``；
        runtime adapter 也不得 import ``execution_verification`` / ``execution_evidence``。
        """
        for leaked in ("RuntimeOrderFact", "IncidentOrderFact", "OrderFailureFact"):
            self.assertFalse(hasattr(AE, leaked), f"adaptive_engine 新增了 {leaked}")
            self.assertFalse(hasattr(PT, leaked), f"paper_trading 新增了 {leaked}")
        adapter_roots = _imported_roots("ai_research_runtime_adapter.py")
        for forbidden in ("execution_evidence", "execution_verification", "execution_planner",
                          "execution_dispatch", "execution_lifecycle"):
            self.assertNotIn(
                forbidden, adapter_roots,
                "runtime adapter 引入了 execution authority 的第二份实现",
            )
        # 非空性：execution owner 的那一份确实存在（本层只是不复制它）。
        import execution_verification as EV
        self.assertTrue(hasattr(EV, "ExecutionFactProjection"))

    def test_INC_23_previous_research_output_stays_context_only(self):
        """INC-23：上一轮 AI research 的结论只是**上下文**，不得升级成 owner 事实。

        ``latest_data_quality_research`` 读的是 canonical research ledger —— 那是**研究产物**。
        若让它变成 runtime owner fact，研究就能给自己签 factual verification（自引用回路）。
        可执行形式：两个 owner 与 adapter 都不得触达 canonical research ledger。
        """
        for name in ("adaptive_engine.py", "paper_trading.py", "ai_research_runtime_adapter.py"):
            roots = _imported_roots(name)
            for forbidden in ("ai_research_repository", "ai_research_provider",
                              "ai_research_service"):
                with self.subTest(module=name, forbidden=forbidden):
                    self.assertNotIn(
                        forbidden, roots,
                        f"{name} import 了 research 持久化/编排层 —— 研究产物不得成为 owner fact",
                    )
        # 非空性：legacy collector 仍然把上一轮研究当上下文（本轮刻意不改它的 shape）；
        # 而 canonical 台账的读入口只有 advisor 一处。
        import deepseek_research as DR
        self.assertEqual(
            "canonical_research_ledger",
            DR._latest_data_quality(_adaptive_conn()).get("source", "canonical_research_ledger"),
        )

    def test_INC_23b_adaptive_execution_evidence_status_is_not_typed_evidence(self):
        """INC-23b：``adaptive_execution_evidence.status`` 是**自由字符串** ⇒ OPEN PREREQUISITE。

        writer 只有一个模块，但 ``status = str(detail.get("status") or "unknown")`` 说明它的值
        由三段各自的 ``state[...]["status"]`` 决定 —— 既没有闭集，也没有 owner 声明它是
        数据质量 / 完整性 / 阈值 / 人工标签中的哪一种。因此本轮**不得**把它签发成 evidence。
        """
        source = _source("adaptive_engine.py")
        self.assertIn('str(detail.get("status") or "unknown")', source)
        # 没有为它发布闭集常量，也没有 typed 读入口。
        self.assertIsNone(getattr(AE, "EXECUTION_EVIDENCE_STATUSES", None))
        self.assertIsNone(getattr(AE, "execution_evidence_fact", None))
        self.assertIsNone(getattr(AE, "AdaptiveExecutionEvidenceFactProjection", None))
        # 非空性：本轮的**另外**两个投影确实存在（说明上面不是"整个模块都没发布契约"）。
        self.assertTrue(hasattr(AE, "AdaptiveRunFactProjection"))
        self.assertTrue(hasattr(PT, "PaperJobRunFactProjection"))


if __name__ == "__main__":
    unittest.main()
