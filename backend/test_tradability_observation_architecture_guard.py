# -*- coding: utf-8 -*-
"""Observation Ledger 的**架构护栏**（AST 静态扫描）。

台账存在的唯一理由是回答"我们什么时候看到 / 尝试看到这些事实"。护栏锁死四件事：

1. 台账**不是**第二套判定层——不得出现 ``can_buy`` / ``can_sell`` / ``allow_order``
   这类授权词汇，也不得 import 任何执行 / 学习 / 策略模块；
2. 台账表**只有一个写入口**——写 ``tradability_observation_ledger`` 的 SQL 只允许
   出现在台账模块自己的 repository 里，散落到别处就意味着有一条绕过审计的写路径；
3. 执行 / 学习 / 选股链路不得 import 台账并把观察结果当 gate；
4. 台账公开 API 面必须**恰好**是登记过的那一组（等值断言，黑名单抓不住
   ``permit_trade`` 这种没被想到的名字）。

**扫描只看代码，不看文档字符串。** 台账模块的文档里刻意写着"本模块**不**含
``can_buy`` / ``can_sell``"以及"禁止 ``recorded_at = session_date``"——把 docstring
算进扫描面会让护栏因为一句*说明*而误报，而误报的护栏会被整条关掉，于是它什么都保护
不了。因此所有检查都在剥离 docstring 之后的字符串常量与标识符集合上做。
"""

import ast
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent

LEDGER_MODULE = "tradability_observation_ledger.py"
LEDGER_TABLE = "tradability_observation_ledger"

#: 台账不得 import 的模块前缀（执行 / 持仓 / 成交 / 学习 / 策略 / 生产热路径）。
FORBIDDEN_IMPORTS = (
    "paper_trading",
    "execution_dispatch",
    "execution_evidence",
    "execution_lifecycle",
    "execution_outcome",
    "execution_planner",
    "execution_profiles",
    "execution_verification",
    "manual_orders",
    "entry_lifecycle",
    "learning_dataset",
    "learning_evaluation",
    "adaptive_engine",
    "adaptive_risk",
    "strategies",
    "evolution_loop",
    "evolution_apply",
    "ai_analysis",
    "tradability_shadow",
)

#: 台账不得出现的授权词汇。台账记的是"看到什么"，不是"能不能交易"。
FORBIDDEN_AUTHORITY_TOKENS = (
    "can_buy",
    "can_sell",
    "allow_order",
    "block_order",
    "override",
    "effective_can_buy",
    "effective_can_sell",
    "execution_authority",
)

#: 台账不得调用的写状态入口。
FORBIDDEN_CALLS = (
    "submit_order", "cancel_order", "modify_order", "place_order",
    "save_position", "save_fill", "record_fill", "apply_evolution",
    "set_risk_budget", "write_learning_row",
)

#: 执行 / 学习 / 选股链路不得 import 台账。
EXECUTION_LEARNING_MODULES = (
    "paper_trading.py",
    "adaptive_engine.py",
    "strategies.py",
    "execution_dispatch.py",
    "execution_evidence.py",
    "execution_lifecycle.py",
    "execution_outcome.py",
    "execution_planner.py",
    "execution_profiles.py",
    "execution_verification.py",
    "learning_dataset.py",
    "learning_evaluation.py",
)

#: 允许写 ``tradability_observation_ledger`` 的模块（唯一写入口）。
#: ``db_migrate.py`` 只在迁移里 ``CREATE TABLE``（不 INSERT），登记它是为了说明
#: "建表由迁移负责"这个分工是有意的。
LEDGER_WRITER_ALLOWLIST = (
    LEDGER_MODULE,
    "db_migrate.py",
)

#: 台账模块**允许**暴露的公开可调用面（等值断言）。
LEDGER_PUBLIC_API = frozenset({
    "ObservationError",
    "ObservationEvent", "ObservationKnowledge", "ObservationCoverage",
    "ArchiveObservationCoverage", "reconcile_archive_rows",
    "normalize_error_identity", "observation_fingerprint", "now_utc",
    "ensure_ledger_schema", "ensure_archive_link_schema",
    "ObservationLedgerRepository", "coverage",
    "event_from_provider_result",
})

#: ``ObservationLedgerRepository`` **允许**暴露的公开方法（等值断言）。
#: ``link_archive_row`` / ``append_links`` / ``archive_links`` /
#: ``reconcile_archive_coverage`` 是 issue #161 的行级 provenance 面：前两者是
#: append-only 写入口，后两者是行级对账读入口。它们不含任何 ``can_*``，也不参与
#: verdict——只回答"这条 archive 行当年是不是和某次观察一起落库"。
REPOSITORY_PUBLIC_API = frozenset({
    "append", "append_many", "connection", "count", "ensure_schema",
    "events", "first_observation", "knowledge_at",
    "ensure_archive_link_schema", "link_archive_row", "append_links",
    "archive_links", "reconcile_archive_coverage",
})

#: ``ObservationKnowledge`` **允许**暴露的公开方法——**不含**任何 ``can_*``。
KNOWLEDGE_PUBLIC_API = frozenset({"has_any_observation", "to_dict"})

#: 行级 provenance 链接表名：只有台账模块与迁移可以写它（唯一写入口）。
ARCHIVE_LINK_TABLE = "tradability_archive_observation_links"

#: 会改数据的 SQL 关键字（append-only 台账不得出现）。
MUTATING_SQL_KEYWORDS = ("UPDATE ", "DELETE FROM", "DROP TABLE", "REPLACE INTO")

#: 写 SQL 关键字（用于"唯一写入口"判定）。
WRITE_SQL_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def _public_names(tree, *, methods_of=None):
    if methods_of is None:
        return {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        }
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == methods_of:
            return {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not child.name.startswith("_")
            }
    raise AssertionError(f"未找到类 {methods_of}")


def _docstring_constant_ids(tree) -> set:
    """所有 docstring 常量节点的 ``id()``（模块 / 类 / 函数的第一条字符串表达式）。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _code_strings(tree) -> set:
    """**非 docstring** 的字符串常量。护栏只看代码，不看说明文字。"""
    docstrings = _docstring_constant_ids(tree)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def _sql_strings(tree) -> set:
    """源码里所有**可拼出 SQL 文本**的字符串，含 f-string。

    f-string 在 AST 里是 ``JoinedStr`` 而不是 ``Constant``，所以只收集常量的实现会
    对 ``f"INSERT INTO {LEDGER_TABLE} ..."`` 视而不见——而台账真实的写入口正是这种
    形式。把 ``JoinedStr`` 的格式化表达式按源码文本（如 ``LEDGER_TABLE``）拼回去，
    检测器才看得见它要保护的东西。docstring 同样排除。
    """
    docstrings = _docstring_constant_ids(tree)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                out.add(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    parts.append(ast.unparse(value.value))
            out.add("".join(parts))
    return out


def _code_identifiers(tree) -> set:
    """代码里出现的标识符（名字 / 属性 / 参数 / 关键字参数名）。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
    return out


def _authority_hits(source: str) -> list:
    """源码里出现的授权词汇（只看代码面）。"""
    tree = ast.parse(source)
    surface = _sql_strings(tree) | _code_identifiers(tree)
    return sorted(
        token
        for token in FORBIDDEN_AUTHORITY_TOKENS
        if any(token in value for value in surface)
    )


def _mutating_sql(source: str) -> list:
    """源码里出现的改数据 SQL（append-only 台账不得有）。"""
    tree = ast.parse(source)
    return sorted(
        {
            value
            for value in _sql_strings(tree)
            if any(keyword in value.upper() for keyword in MUTATING_SQL_KEYWORDS)
        }
    )


def _writes_ledger_table(source: str) -> bool:
    """源码里是否有**写** ``tradability_observation_ledger`` 的 SQL。

    同时认字面表名与 ``{LEDGER_TABLE}`` 形式（台账模块用 f-string 引用常量，
    只匹配字面量会让唯一写入口检测变成永远为假——那正是"装饰性护栏"）。
    """
    tree = ast.parse(source)
    for value in _sql_strings(tree):
        touches = LEDGER_TABLE in value or "LEDGER_TABLE" in value
        writes = any(keyword in value.upper() for keyword in WRITE_SQL_KEYWORDS)
        if touches and writes:
            return True
    return False


def _ledger_tree():
    return ast.parse((BACKEND / LEDGER_MODULE).read_text(encoding="utf-8"))


def _ledger_source():
    return (BACKEND / LEDGER_MODULE).read_text(encoding="utf-8")


def _imported_modules(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append(node.module.split(".")[-1])
            for alias in node.names:
                out.append(alias.name.split(".")[-1])
    return out


def _called_names(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                out.append(func.id)
            elif isinstance(func, ast.Attribute):
                out.append(func.attr)
    return out


def _recorded_at_sources(source: str) -> list:
    """所有 ``recorded_at=...`` 传入值的源码文本（检查是否被伪装成历史日期）。"""
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "recorded_at":
                    out.append(ast.unparse(keyword.value))
    return out


class LedgerExposesNoAuthorityApi(unittest.TestCase):
    """等值断言：台账的公开 API 面必须**恰好**是登记过的那一组。"""

    def test_module_public_api_is_exactly_the_registered_set(self):
        self.assertEqual(LEDGER_PUBLIC_API, _public_names(_ledger_tree()))

    def test_repository_public_api_is_exactly_the_registered_set(self):
        self.assertEqual(
            REPOSITORY_PUBLIC_API,
            _public_names(_ledger_tree(), methods_of="ObservationLedgerRepository"),
        )

    def test_knowledge_public_api_has_no_verdict_methods(self):
        self.assertEqual(
            KNOWLEDGE_PUBLIC_API,
            _public_names(_ledger_tree(), methods_of="ObservationKnowledge"),
        )

    def test_detector_fires_on_a_new_public_entry_point(self):
        tree = ast.parse("def effective_can_buy():\n    pass\n")
        found = _public_names(tree)
        self.assertIn("effective_can_buy", found)
        self.assertNotEqual(LEDGER_PUBLIC_API, found)


class LedgerContainsNoVerdictVocabulary(unittest.TestCase):
    """台账记"看到什么"，不记"能不能交易"。"""

    def test_no_authority_token_in_code(self):
        found = _authority_hits(_ledger_source())
        self.assertEqual([], found, f"台账代码不得出现授权词汇: {found}")

    def test_docstrings_may_explain_the_boundary(self):
        """反向断言：文档里**必须**写明边界，但扫描面刻意不含它。

        这条同时防止未来有人"为了消掉 guard 报错"而删掉边界说明，或反过来把
        docstring 纳入扫描面（那会让护栏因为一句说明而误报）。
        """
        source = _ledger_source()
        self.assertIn("can_buy", source)  # 文档里说明了"不含 can_buy"
        self.assertEqual([], _authority_hits(source))  # 但代码里没有

    def test_detector_fires_on_an_authority_token(self):
        source = "def knowledge_at():\n    return {'can_buy': True}\n"
        self.assertEqual(["can_buy"], _authority_hits(source))

    def test_detector_ignores_observation_vocabulary(self):
        source = "OBSERVED_EVIDENCE = 'evidence'\nLEGACY_OBSERVATION_UNKNOWN = 'legacy'\n"
        self.assertEqual([], _authority_hits(source))

    def test_detector_ignores_a_docstring_mention(self):
        """非空洞性：docstring 里的 ``can_buy`` 不得被判为违规。"""
        source = 'def f():\n    """本模块不含 can_buy / can_sell。"""\n    return 1\n'
        self.assertEqual([], _authority_hits(source))


class LedgerDoesNotImportExecutionModules(unittest.TestCase):
    def test_no_forbidden_import(self):
        found = [
            name for name in _imported_modules(_ledger_tree())
            if name in FORBIDDEN_IMPORTS
        ]
        self.assertEqual([], found, f"台账不得 import {found}")

    def test_no_forbidden_call(self):
        found = [name for name in _called_names(_ledger_tree()) if name in FORBIDDEN_CALLS]
        self.assertEqual([], found, f"台账不得调用 {found}")

    def test_detector_fires_on_a_forbidden_import(self):
        tree = ast.parse("import paper_trading\n")
        found = [n for n in _imported_modules(tree) if n in FORBIDDEN_IMPORTS]
        self.assertEqual(["paper_trading"], found)

    def test_detector_fires_on_a_forbidden_call(self):
        tree = ast.parse("def f(conn):\n    submit_order(conn)\n")
        found = [n for n in _called_names(tree) if n in FORBIDDEN_CALLS]
        self.assertEqual(["submit_order"], found)

    def test_detector_ignores_benign_imports(self):
        tree = ast.parse("import sqlite3\nimport point_in_time as PIT\n")
        found = [n for n in _imported_modules(tree) if n in FORBIDDEN_IMPORTS]
        self.assertEqual([], found)


class LedgerTableHasASingleWriter(unittest.TestCase):
    """写台账表的 SQL 只能出现在唯一写入口。"""

    def _foreign_writers(self) -> list:
        offenders = []
        for path in sorted(BACKEND.glob("*.py")):
            if path.name.startswith("test_"):
                continue  # 测试可以断言表内容，但生产模块不行
            if path.name in LEDGER_WRITER_ALLOWLIST:
                continue
            if _writes_ledger_table(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        return offenders

    def test_only_the_ledger_module_writes_the_table(self):
        self.assertEqual(
            [], self._foreign_writers(),
            "观察台账只能由 tradability_observation_ledger 写入（唯一写入口）",
        )

    def test_the_writer_is_actually_the_ledger_module(self):
        # 非空洞性：写入口必须真的存在于台账模块里，而不是"谁都没写"。
        self.assertTrue(_writes_ledger_table(_ledger_source()))

    def test_detector_fires_on_a_literal_table_write(self):
        source = f"conn.execute('INSERT INTO {LEDGER_TABLE} VALUES (1)')\n"
        self.assertTrue(_writes_ledger_table(source))

    def test_detector_fires_on_an_fstring_table_write(self):
        source = 'conn.execute(f"DELETE FROM {LEDGER_TABLE} WHERE code=?")\n'
        self.assertTrue(_writes_ledger_table(source))

    def test_detector_ignores_a_read(self):
        source = f"conn.execute('SELECT COUNT(*) FROM {LEDGER_TABLE}')\n"
        self.assertFalse(_writes_ledger_table(source))

    def test_detector_ignores_a_foreign_table_write(self):
        source = "conn.execute('INSERT INTO paper_orders VALUES (1)')\n"
        self.assertFalse(_writes_ledger_table(source))


class ExecutionAndLearningDoNotConsumeLedger(unittest.TestCase):
    def test_no_execution_or_learning_module_imports_ledger(self):
        offenders = []
        for name in EXECUTION_LEARNING_MODULES:
            path = BACKEND / name
            if not path.exists():  # pragma: no cover - 模块清单防御
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for imported in _imported_modules(tree):
                if "tradability_observation_ledger" in imported:
                    offenders.append(f"{name}: {imported}")
        self.assertEqual(
            [], offenders,
            "执行/学习链路不得 import 观察台账（台账零 authority）",
        )

    def test_detector_fires_on_a_ledger_import(self):
        tree = ast.parse("import tradability_observation_ledger\n")
        found = [
            n for n in _imported_modules(tree) if "tradability_observation_ledger" in n
        ]
        self.assertEqual(["tradability_observation_ledger"], found)

    def test_no_execution_module_reads_the_ledger_table(self):
        offenders = []
        for name in EXECUTION_LEARNING_MODULES:
            path = BACKEND / name
            if not path.exists():  # pragma: no cover
                continue
            if LEDGER_TABLE in path.read_text(encoding="utf-8"):
                offenders.append(name)
        self.assertEqual(
            [], offenders,
            f"执行/学习链路不得直接读 {LEDGER_TABLE} 决定交易",
        )

    def test_ledger_logic_lives_in_its_own_module(self):
        self.assertTrue((BACKEND / LEDGER_MODULE).exists())
        for name in EXECUTION_LEARNING_MODULES:
            path = BACKEND / name
            if not path.exists():  # pragma: no cover
                continue
            self.assertNotIn(
                "ObservationLedgerRepository", path.read_text(encoding="utf-8"),
                f"{name} 不得内嵌台账",
            )


class LedgerIsAppendOnly(unittest.TestCase):
    """台账不得有 UPDATE / DELETE 路径：历史观察是已经发生过的事实。"""

    def test_no_mutating_sql_in_code(self):
        found = _mutating_sql(_ledger_source())
        self.assertEqual([], found, f"台账必须 append-only，不得包含 {found}")

    def test_no_update_or_delete_method_on_repository(self):
        methods = _public_names(_ledger_tree(), methods_of="ObservationLedgerRepository")
        for forbidden in ("update", "delete", "remove", "replace"):
            self.assertNotIn(forbidden, methods)

    def test_detector_fires_on_an_update_statement(self):
        source = f"conn.execute('UPDATE {LEDGER_TABLE} SET recorded_at=?')\n"
        self.assertTrue(_mutating_sql(source))

    def test_detector_fires_on_a_delete_statement(self):
        source = f"conn.execute('DELETE FROM {LEDGER_TABLE}')\n"
        self.assertTrue(_mutating_sql(source))

    def test_detector_ignores_select_and_insert(self):
        source = (
            f"conn.execute('SELECT * FROM {LEDGER_TABLE}')\n"
            f'conn.execute(f"INSERT OR IGNORE INTO {{LEDGER_TABLE}} VALUES(?)")\n'
        )
        self.assertEqual([], _mutating_sql(source))


class LedgerDoesNotFabricateFirstSeen(unittest.TestCase):
    """不得把 ``session_date`` / ``created_at`` 伪装成 ``recorded_at``。"""

    def test_recorded_at_is_never_sourced_from_a_historical_date(self):
        sources = _recorded_at_sources(_ledger_source())
        self.assertTrue(sources, "非空洞性：必须真的存在 recorded_at= 的传入点")
        for text in sources:
            for forbidden in ("session", "created_at", "archive"):
                self.assertNotIn(
                    forbidden, text,
                    f"recorded_at 不得取自 {forbidden}（那是时间旅行）: {text}",
                )

    def test_missing_recorded_at_is_an_error_not_a_default(self):
        source = _ledger_source()
        self.assertIn("观察事件缺少可解析的 recorded_at", source)

    def test_detector_fires_on_a_fabricated_recorded_at(self):
        fake = "event_from_provider_result(r, code=c, session=s, recorded_at=session_date)\n"
        sources = _recorded_at_sources(fake)
        self.assertEqual(["session_date"], sources)
        self.assertIn("session", sources[0])

    def test_legacy_rows_are_not_backdated(self):
        source = _ledger_source()
        # 升级前数据只能标 unknown，不得从 archive 反推 first_seen。
        self.assertIn("legacy_observation_unknown", source)
        self.assertNotIn("archive_created_at", source)
        self.assertNotIn("first_seen_at=session", source)


if __name__ == "__main__":
    unittest.main()
