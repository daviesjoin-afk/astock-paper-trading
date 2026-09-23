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
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent

EXECUTION_PLANNER = BACKEND / "execution_planner.py"
PAPER_TRADING = BACKEND / "paper_trading.py"
PAPER_RISK_SERVICE = BACKEND / "paper_risk_service.py"
EXECUTION_VERIFICATION = BACKEND / "execution_verification.py"
PAPER_SCHEMA_MIGRATIONS = BACKEND / "paper_schema_migrations.py"

#: 允许写入 ``paper_fills`` 的生产模块（``demo_seed`` 是非生产夹具）。
#: 单一 writer 是本轮的核心不变式之一，因此这里刻意用白名单而不是"扫到不报"。
FILL_WRITER_ALLOWLIST = frozenset({"execution_planner.py"})

#: 禁止 ``execution_planner`` 依赖的模块。执行只消费行情事实，不消费信号结论。
EXECUTION_FORBIDDEN_DEPENDENCIES = frozenset({"signal_service"})


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


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


def _execute_calls(path: Path):
    """产出 (sql 字符串, 所在函数节点源码) 供白名单判定。"""
    source = _source(path)
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name not in {"execute", "executescript", "executemany"}:
            continue
        if not node.args:
            continue
        literal = node.args[0]
        if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
            yield literal.value, node, source


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


class PaperFillsHasASingleProductionWriter(unittest.TestCase):
    """Guard 16：成交流水的生产写入者只有一个。"""

    def test_guard16a_only_the_execution_authority_writes_paper_fills(self):
        offenders = []
        for path in sorted(BACKEND.glob("*.py")):
            name = path.name
            if name.startswith("test_") or name in FILL_WRITER_ALLOWLIST:
                continue
            for sql, node, _source_text in _execute_calls(path):
                if "INSERT INTO paper_fills" in sql or "INSERT OR REPLACE INTO paper_fills" in sql:
                    offenders.append(f"{name}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            "存在绕过执行权威的成交流水写入路径：\n" + "\n".join(offenders),
        )


class RiskActionDedupHasOneImplementation(unittest.TestCase):
    """Guard 17：一次性风险动作的"是否已执行"只有一个判据。"""

    def test_guard17a_risk_service_delegates_to_the_verification_authority(self):
        source = _source(PAPER_RISK_SERVICE)
        self.assertIn(
            "has_verified_positive_execution", source,
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
