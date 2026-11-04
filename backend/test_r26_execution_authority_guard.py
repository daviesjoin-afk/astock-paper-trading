# -*- coding: utf-8 -*-
"""R26 架构护栏：模拟执行权威的边界必须是**可执行的**断言，而不是文档承诺。

存在理由与前面几轮护栏一致：这些边界一旦被绕过，缺陷不会当场报错，而是变成
"同一张委托在不同路径得到不同结论"——最难发现、也最难回溯的一类问题。

    Guard 15  ``execution_planner`` 只能依赖 **R24 Market Data 契约** 获取行情事实，
              不得依赖 signal 层（``signal_service``）。依赖方向必须是
              ``Market Data → Execution``，而不是 ``Signal → Execution``：
              执行需要的是"这份行情是否可信"，那是行情边界的事实，不是信号层的结论。

    Guard 16  ``paper_fills`` 的生产写入者只有 ``execution_planner.commit_fill``。
              其它模块（含 ``paper_trading``）不得自行 INSERT 成交流水 —— 否则
              "什么算成交"就不再只有一处实现。

    Guard 17  "一次性风险动作是否已执行"的判断只有
              ``execution_verification.has_verified_positive_execution`` 一处实现。
              风险模块不得再手写 ``status='filled'`` + 验证谓词的 SQL：正是那份手写
              判据把**部分成交**读成了"动作没发生过"，导致重复减仓。

    Guard 18  "存量订单是否可能携带成交流水"的选取条件只有一个常量
              （``FILL_CARRYING_PREDICATE``）。读路径不得各自硬编码 ``status='filled'``，
              否则部分成交会在某些读路径上凭空消失。

    Guard 19  R26 新增字段（``paper_position_lots.source_fill_id``）不得被回填猜测：
              迁移只允许 ``ADD COLUMN``，不得给历史行写入某个 fill id。

    Guard 20  逐笔成交血缘的**回填**只发生在 ``commit_fill`` 同一事务内，并且直接取
              刚写入那条 fill 的 ``lastrowid`` —— 不得按时间/价格"找一笔像的"。
"""
from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from test_support import ast_cache

BACKEND = Path(__file__).resolve().parent

EXECUTION_PLANNER = BACKEND / "execution_planner.py"
PAPER_TRADING = BACKEND / "paper_trading.py"
PAPER_RISK_SERVICE = BACKEND / "paper_risk_service.py"
EXECUTION_VERIFICATION = BACKEND / "execution_verification.py"
PAPER_SCHEMA_MIGRATIONS = BACKEND / "paper_schema_migrations.py"

#: 允许写入 ``paper_fills`` 的生产模块，用**库内相对路径**而不是文件名。
#: 文件名白名单在 package 移动或出现同名文件时会静默失效（放行或误报），
#: 因此这里固定 ``backend/`` 下的相对 POSIX 路径。
#: 单一 writer 是本轮的核心不变式之一，因此刻意用白名单而不是"扫到不报"。
#:
#: ``demo_seed`` 是唯一允许的第二处写入，且**只在 ``ASTOCK_DEMO=1``** 时由
#: ``main.py`` 调用：它构造合成账本（signal→risk→order→fill→position→NAV），
#: 不参与任何正式交易路径。把它显式列出比"扫描不到"更安全——扫描盲区才是
#: 真正的风险，白名单至少让这条豁免可审计。
FILL_WRITER_ALLOWLIST = frozenset({
    "execution_planner.py",   # 唯一正式成交提交 authority
    "demo_seed.py",           # 仅 ASTOCK_DEMO=1 的合成账本夹具
})

#: 通过包装函数执行 SQL 的入口名。``demo_seed._fill`` 就是用
#: ``PT.query_rows(conn, "INSERT INTO paper_fills ...")`` 写的：只认
#: ``.execute/.executescript/.executemany`` 会让这类第二 writer 完全逃过扫描。
SQL_EXECUTION_WRAPPERS = frozenset(
    {"execute", "executescript", "executemany", "query_rows"}
)

#: 禁止 ``execution_planner`` 依赖的模块。执行只消费行情事实，不消费信号结论。
EXECUTION_FORBIDDEN_DEPENDENCIES = frozenset({"signal_service"})


def _source(path: Path) -> str:
    return ast_cache.source(path)


def _tree(path: Path) -> ast.Module:
    return ast_cache.tree(path)


def _imported_modules(path: Path) -> set[str]:
    """该文件 import 的所有模块名（含函数内惰性 import）。"""
    names: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module.split(".")[0])
    return names


def _sql_literal(node: ast.Call) -> str | None:
    """取调用里第一个字符串字面量参数（execute(conn 已是绑定方法时是 argv[0]）。

    ``query_rows(conn, sql, params)`` 的 SQL 在第二个位置，因此逐个位置找
    第一个字符串常量，而不是写死 ``args[0]``。
    """
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _execute_calls(path: Path):
    """产出 (sql 字符串, 调用节点, 源码) 供 writer 判定。

    覆盖 ``execute`` / ``executescript`` / ``executemany`` 以及 ``query_rows``
    这类执行包装；后者曾让 ``demo_seed._fill`` 的 ``paper_fills`` 写入完全
    逃过扫描，而它按自己的注释就是一条生产写入路径。
    """
    source = _source(path)
    tree = _tree(path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name not in SQL_EXECUTION_WRAPPERS:
            continue
        sql = _sql_literal(node)
        if sql is not None:
            yield sql, node, source


class ExecutionConsumesOnlyTheMarketDataBoundary(unittest.TestCase):
    """Guard 15：``execution_planner`` 不得依赖 signal 层。"""

    def test_guard15a_execution_planner_does_not_import_signal_service(self):
        forbidden = _imported_modules(EXECUTION_PLANNER) & EXECUTION_FORBIDDEN_DEPENDENCIES
        self.assertEqual(
            set(), forbidden,
            "执行权威依赖了 signal 层：依赖方向必须是 Market Data → Execution，"
            f"而不是 Signal → Execution（发现：{sorted(forbidden)}）",
        )

    def test_guard15a2_no_signal_service_attribute_access_in_execution_planner(self):
        """惰性 import 之外，也不允许用 ``SIG.`` 这类别名访问信号层。"""
        source = _source(EXECUTION_PLANNER)
        self.assertNotIn(
            "signal_evidence", source,
            "执行权威不得调用 signal_service.signal_evidence 获取行情事实",
        )

    def test_guard15b_execution_planner_depends_on_the_market_data_contract(self):
        """正向要求：行情事实必须来自 R24 契约，否则它就没有可用的可信度判据。"""
        self.assertIn("market_data_contract", _imported_modules(EXECUTION_PLANNER))

    def test_guard15c_the_canonical_quote_mapping_is_declared(self):
        """逐票报价 → snapshot 的映射只有一处实现（契约模块）。"""
        contract = (BACKEND / "market_data_contract.py").read_text(encoding="utf-8")
        self.assertIn("def symbol_quote_snapshot(", contract)
        planner = _source(EXECUTION_PLANNER)
        self.assertIn(
            "symbol_quote_snapshot", planner,
            "execution_planner 必须消费契约里的唯一映射，而不是自己拼 snapshot",
        )
        self.assertNotIn(
            "MarketDataSnapshot(", planner,
            "execution_planner 不得自行构造 MarketDataSnapshot（可信度判定会因此分叉）",
        )


def _paper_fill_writer_offenders(root: Path) -> list[str]:
    """递归扫描生产 Python 源码，避免 package 移动后漏掉第二个 writer。"""
    offenders = []
    insert_into = "".join(chr(code) for code in (73, 78, 83, 69, 82, 84, 32, 73, 78, 84, 79, 32))
    insert_or_replace_into = "".join(
        chr(code) for code in (
            73, 78, 83, 69, 82, 84, 32, 79, 82, 32, 82, 69, 80, 76, 65, 67, 69, 32,
            73, 78, 84, 79, 32,
        )
    )
    table = "PAPER_" + "FILLS"
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if path.name.startswith("test_") or relative in FILL_WRITER_ALLOWLIST:
            continue
        for sql, node, _source_text in _execute_calls(path):
            normalized = " ".join(sql.upper().split())
            if (insert_into + table in normalized
                    or insert_or_replace_into + table in normalized):
                offenders.append(f"{relative}:{node.lineno}")
    return offenders

class PaperFillsHasASingleProductionWriter(unittest.TestCase):
    """Guard 16：paper_fills 的生产写入 authority 唯一，且递归覆盖 package。"""

    def test_guard16_allowlist_stays_minimal_and_named(self):
        """白名单只能有两条，且必须是明确的文件名。

        这条锁住的是"为了通过测试而扩大白名单"这条路：新增豁免必须同时
        改这个断言，从而在 review 中可见。
        """
        self.assertEqual(
            {"execution_planner.py", "demo_seed.py"},
            set(FILL_WRITER_ALLOWLIST),
            "paper_fills 的写入豁免被扩大；新增豁免需要显式理由与 review",
        )

    def test_guard16_wrapped_sql_execution_is_scanned(self):
        """负向控制：经 ``query_rows`` 包装的写入同样必须被发现。

        ``demo_seed._fill`` 曾用 ``PT.query_rows(conn, "INSERT INTO paper_fills")``
        写入而完全逃过扫描（守卫当时只认 execute/executescript/executemany）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer = root / "paper" / "infrastructure" / "wrapped_writer.py"
            writer.parent.mkdir(parents=True)
            sql = "".join(
                chr(code) for code in (
                    73, 78, 83, 69, 82, 84, 32, 73, 78, 84, 79, 32,
                    80, 65, 80, 69, 82, 95, 70, 73, 76, 76, 83,
                    40, 97, 99, 99, 111, 117, 110, 116, 95, 105, 100, 41,
                    32, 86, 65, 76, 85, 69, 83, 32, 40, 34, 121, 34, 41,
                )
            )
            writer.write_text(
                "def write(conn):\n"
                "    return query_rows(conn, " + repr(sql) + ", ())\n",
                encoding="utf-8",
            )
            offenders = _paper_fill_writer_offenders(root)
            self.assertEqual(1, len(offenders), offenders)
            self.assertIn("wrapped_writer.py", offenders[0])

    def test_guard16a_only_the_execution_authority_writes_paper_fills(self):
        offenders = _paper_fill_writer_offenders(BACKEND)
        self.assertEqual(
            offenders, [],
            "存在绕过执行权威的成交流水写入路径：\n" + "\n".join(offenders),
        )

    def test_guard16b_nested_package_writer_is_detected(self):
        """负向控制：新 package 中出现第二 writer 时，守卫必须失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "paper" / "infrastructure" / "unexpected_writer.py"
            nested.parent.mkdir(parents=True)
            sql = "".join(
                chr(code) for code in (
                    73, 78, 83, 69, 82, 84, 32, 73, 78, 84, 79, 32,
                    80, 65, 80, 69, 82, 95, 70, 73, 76, 76, 83,
                    40, 97, 99, 99, 111, 117, 110, 116, 95, 105, 100, 41,
                    32, 86, 65, 76, 85, 69, 83, 32, 40, 34, 120, 34, 41,
                )
            )
            source = "def write_fill(conn):\n    conn.execute(" + repr(sql) + ")\n"
            nested.write_text(source, encoding="utf-8")
            offenders = _paper_fill_writer_offenders(root)
            self.assertEqual(1, len(offenders), offenders)
            self.assertTrue(offenders[0].startswith("paper/infrastructure/"))


class RiskActionDedupHasOneImplementation(unittest.TestCase):
    """Guard 17：一次性风险动作的"是否已执行"只有一个判据。"""

    def test_guard17a_risk_service_delegates_to_the_verification_authority(self):
        source = _source(PAPER_RISK_SERVICE)
        self.assertIn(
            "EV.verified_execution_markers(", source,
            "风险扫描必须走 execution_verification 的唯一去重判据",
        )

    def test_guard17b_risk_service_does_not_hand_roll_a_dedup_query(self):
        """风险模块不得再自己写 ``status='filled'`` 的去重 SQL。"""
        offenders = []
        for sql, node, _text in _execute_calls(PAPER_RISK_SERVICE):
            if "exit_marker" in sql:
                offenders.append(f"paper_risk_service.py:{node.lineno}")
        self.assertEqual(
            offenders, [],
            "风险模块手写了 exit_marker 去重 SQL（应由 execution_verification 拥有）：\n"
            + "\n".join(offenders),
        )

    def test_guard17c_the_dedup_predicate_treats_partial_as_executed(self):
        """判据本身必须承认部分成交：这是本轮修复的语义核心。"""
        source = _source(EXECUTION_VERIFICATION)
        self.assertIn("POSITIVE_EXECUTION_PREDICATE", source)
        self.assertIn("EXECUTION_STATUS_PARTIAL", source)
        self.assertIn(
            "def has_verified_positive_execution(", source,
            "去重判据必须作为命名契约存在，而不是内联在调用方",
        )


class FillCarryingSelectionHasOnePredicate(unittest.TestCase):
    """Guard 18：读路径不得硬编码 ``status='filled'`` 来选取流水。"""

    #: 允许保留 ``status='filled'`` 的读路径：它们问的是"这张订单是否**完整**成交"
    #: （例如已实现盈亏、胜率统计），而不是"它是否有成交"。两者必须区分。
    COMPLETENESS_SEMANTICS_MARKERS = (
        "realized_pnl", "realized", "win_rate", "complete",
    )

    def test_guard18a_read_model_selects_fill_carrying_orders_via_the_predicate(self):
        source = _source(BACKEND / "paper_portfolio_read_model.py")
        self.assertIn(
            "EV.FILL_CARRYING_PREDICATE", source,
            "读模型必须用唯一选取谓词，否则部分成交会在读路径上消失",
        )

    def test_guard18b_error_test_plan_does_not_use_partial_as_full(self):
        """反向不变式：部分成交**不得**被当成完整成交。"""
        source = _source(EXECUTION_VERIFICATION)
        # ``is_verified_row`` 必须仍然只认 'verified'：把 partial 也算作 verified
        # 会让"部分成交"冒充"完整成交"，那是相反方向的错误。
        self.assertIn(
            "def is_verified_row(", source,
        )
        tree = _tree(EXECUTION_VERIFICATION)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "is_verified_row":
                continue
            body = ast.get_source_segment(source, node) or ""
            self.assertNotIn(
                "EXECUTION_STATUS_PARTIAL", body,
                "is_verified_row 不得把 partial 当成 verified（部分成交不是完整成交）",
            )


class SourceFillLineageIsNeverFabricated(unittest.TestCase):
    """Guard 19/20：逐笔血缘只允许真实写入，绝不为历史数据猜造。"""

    def test_guard19a_migration_only_adds_the_column(self):
        source = _source(PAPER_SCHEMA_MIGRATIONS)
        self.assertIn("source_fill_id", source)
        # 迁移里不得出现为历史 lot 写 fill id 的 UPDATE。
        for sql, node, _text in _execute_calls(PAPER_SCHEMA_MIGRATIONS):
            if "source_fill_id" in sql and sql.strip().upper().startswith("UPDATE"):
                self.fail(
                    f"迁移在给历史 lot 回填 source_fill_id（paper_schema_migrations.py:{node.lineno}）："
                    "无法证明的血缘必须保持 NULL，绝不猜造"
                )

    def test_guard20a_lineage_backlink_uses_the_just_written_fill_id(self):
        source = _source(EXECUTION_PLANNER)
        tree = _tree(EXECUTION_PLANNER)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "commit_fill":
                continue
            body = ast.get_source_segment(source, node) or ""
            found = True
            self.assertIn(
                "source_fill_id", body,
                "commit_fill 必须回填逐笔成交血缘",
            )
            self.assertIn(
                "lastrowid", body,
                "血缘必须取刚写入那条 fill 的 lastrowid，不得按时间/价格找一笔像的",
            )
            self.assertIn(
                "INSERT INTO paper_fills", body,
                "血缘回填必须与流水写入同处一个事务",
            )
        self.assertTrue(found, "commit_fill 未找到（护栏失效？）")


if __name__ == "__main__":
    unittest.main()
