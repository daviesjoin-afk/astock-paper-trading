# -*- coding: utf-8 -*-
"""Shadow Tradability Validation 的**架构护栏**（AST 静态扫描）。

护栏存在只为一件事：**Shadow 永远不能获得 authority**。

它在源码层禁止四类退化，每一类都是真实可能发生的"顺手一改"：

1. Shadow 模块 import 任何下单 / 持仓 / 成交 / 学习模块，或调用它们的入口；
2. Shadow 模块自己读原始可交易性字段、自己判 ST / 停牌 / 涨跌停方向——那些规则
   已经有 authority，复制一份就意味着比的是"复制版 A vs 归档"；
3. 执行 / 学习 / 选股链路 import Shadow 并把它的结论当 gate；
4. 操作员 CLI 长出 ``--apply`` / ``--enforce`` / ``--switch-authority`` 这类开关。

护栏刻意保持**精确**：只扫本 PR 新增的两个文件 + 既有的执行/学习模块清单，不做
宽泛正则，避免误伤既有代码后被整条关掉。
"""

import ast
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent

SHADOW_MODULE = "tradability_shadow.py"
SHADOW_CLI = ROOT / "work" / "tradability_shadow_validation.py"

#: Shadow 不得 import 的模块前缀（下单 / 持仓 / 成交 / 学习 / 执行编排 / 策略）。
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
)

#: Shadow 不得调用的名字（写状态 / 授权 / 下单）。
FORBIDDEN_CALLS = (
    "submit_order", "cancel_order", "modify_order", "place_order",
    "save_position", "save_fill", "record_fill", "apply_evolution",
    "set_risk_budget", "write_learning_row",
)

#: Shadow 不得直接读取的原始可交易性字段（必须经 archive 权威判定）。
RAW_TRADABILITY_KEYS = (
    "is_suspended",
    "is_price_limit_locked",
    "has_market_quote",
    "has_trade_volume",
    "price_limit_direction",
)

#: 执行 / 学习 / 选股链路不得把 Shadow 的结论当 gate。
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

#: 操作员 CLI 不得出现的开关（会把它从观察变成 authority）。
FORBIDDEN_CLI_FLAGS = ("--apply", "--enforce", "--switch-authority", "--take-over")

#: Shadow 模块**允许**暴露的公开可调用面（等值断言，不是黑名单）。
#:
#: 黑名单只能抓住"我想到的名字"；一个叫 ``permit_trade`` 的新入口会顺利通过。
#: 因此这里钉住整个公开 API 面：任何新增的公开函数 / 类都必须显式登记到这里，
#: 而登记这个动作本身就是一次需要理由的架构决定。
#:
#: 只登记**可调用对象**（函数 / 类）——常量本身不能执行任何动作，它作为"策略开关"
#: 的风险由下面的 forbidden-call / forbidden-flag 断言覆盖。
SHADOW_PUBLIC_API = frozenset({
    # 错误
    "ShadowError", "ShadowConflictError",
    # 词表
    "ShadowStatus",
    # 只读观察对象
    "ShadowComparison", "ShadowComparisonSummary", "ShadowComparator",
    # 可选持久化（独立表，与生产表隔离）
    "ensure_shadow_schema", "save_comparison", "save_comparisons", "load_comparisons",
})

#: ``ShadowComparator`` **允许**暴露的公开方法（等值断言）。
COMPARATOR_PUBLIC_API = frozenset({"compare", "compare_many", "summarize"})


def _public_names(tree, *, methods_of=None):
    """模块级**可调用**公开名（函数 / 类）；给了 ``methods_of`` 则取该类的方法名。"""
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


def _shadow_tree():
    return ast.parse((BACKEND / SHADOW_MODULE).read_text(encoding="utf-8"))


def _imported_modules(tree):
    """源码里出现的所有被 import 的模块名（绝对与相对都归一成末段）。"""
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


def _string_constants(tree):
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


class ShadowExposesNoAuthorityApi(unittest.TestCase):
    """等值断言：Shadow 的公开 API 面必须**恰好**是登记过的那一组。

    黑名单只能抓住"我想到的名字"；任何新增的公开入口（哪怕叫 ``permit_trade``）
    都必须显式登记，而登记本身就是一次需要理由的架构决定。
    """

    def test_module_public_api_is_exactly_the_registered_set(self):
        self.assertEqual(SHADOW_PUBLIC_API, _public_names(_shadow_tree()))

    def test_comparator_public_api_is_exactly_the_registered_set(self):
        self.assertEqual(
            COMPARATOR_PUBLIC_API,
            _public_names(_shadow_tree(), methods_of="ShadowComparator"),
        )

    def test_comparison_object_public_api_is_registered(self):
        self.assertEqual(
            frozenset({"identity", "content", "fingerprint", "to_dict"}),
            _public_names(_shadow_tree(), methods_of="ShadowComparison"),
        )

    def test_detector_fires_on_a_new_public_entry_point(self):
        tree = ast.parse(
            "def allow_order():\n    pass\n\n\ndef load_comparisons():\n    pass\n"
        )
        found = _public_names(tree)
        self.assertIn("allow_order", found)
        self.assertNotEqual(SHADOW_PUBLIC_API, found)


class ShadowDoesNotImportExecutionModules(unittest.TestCase):
    def test_no_forbidden_import(self):
        found = [
            name for name in _imported_modules(_shadow_tree())
            if name in FORBIDDEN_IMPORTS
        ]
        self.assertEqual([], found, f"tradability_shadow 不得 import {found}")

    def test_no_forbidden_call(self):
        found = [name for name in _called_names(_shadow_tree()) if name in FORBIDDEN_CALLS]
        self.assertEqual([], found, f"tradability_shadow 不得调用 {found}")

    def test_detector_fires_on_a_forbidden_import(self):
        tree = ast.parse("import paper_trading\n")
        found = [n for n in _imported_modules(tree) if n in FORBIDDEN_IMPORTS]
        self.assertEqual(["paper_trading"], found)

    def test_detector_fires_on_a_forbidden_call(self):
        tree = ast.parse("def f(conn):\n    submit_order(conn)\n")
        found = [n for n in _called_names(tree) if n in FORBIDDEN_CALLS]
        self.assertEqual(["submit_order"], found)

    def test_detector_ignores_benign_imports(self):
        tree = ast.parse("import tradability_archive as TA\nimport selection_tradability as ST\n")
        found = [n for n in _imported_modules(tree) if n in FORBIDDEN_IMPORTS]
        self.assertEqual([], found)


class ShadowDoesNotReimplementMarketRules(unittest.TestCase):
    """Shadow 不得自己读原始事实字段——判定必须来自 archive 的公开 API。"""

    def test_no_raw_tradability_key_access(self):
        offenders = []
        for node in ast.walk(_shadow_tree()):
            # ``x.is_suspended`` / ``x["is_suspended"]`` 都算直接读原始字段。
            if isinstance(node, ast.Attribute) and node.attr in RAW_TRADABILITY_KEYS:
                offenders.append(ast.unparse(node))
            if isinstance(node, ast.Subscript):
                slice_node = node.slice
                if isinstance(slice_node, ast.Constant) and slice_node.value in RAW_TRADABILITY_KEYS:
                    offenders.append(ast.unparse(node))
        self.assertEqual([], offenders, f"Shadow 不得直接读原始字段: {offenders}")

    def test_archive_reason_vocabulary_is_imported_not_duplicated(self):
        # 未知原因必须来自 archive 的权威词表，不能在 Shadow 里再写一个字符串常量。
        source = (BACKEND / SHADOW_MODULE).read_text(encoding="utf-8")
        self.assertIn("TA.TradabilityReason", source)
        self.assertIn("tradability_at", source)
        self.assertNotIn('"unknown_state"', source)

    def test_detector_fires_on_a_raw_field_read(self):
        tree = ast.parse("def f(row):\n    return row.is_suspended\n")
        offenders = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr in RAW_TRADABILITY_KEYS
        ]
        self.assertEqual(1, len(offenders))


class ExecutionAndLearningDoNotConsumeShadow(unittest.TestCase):
    def test_no_execution_or_learning_module_imports_shadow(self):
        offenders = []
        for name in EXECUTION_LEARNING_MODULES:
            path = BACKEND / name
            if not path.exists():  # pragma: no cover - 模块清单防御
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for imported in _imported_modules(tree):
                if "tradability_shadow" in imported:
                    offenders.append(f"{name}: {imported}")
        self.assertEqual(
            [], offenders,
            "执行/学习链路不得 import tradability_shadow（Shadow 零 authority）",
        )

    def test_detector_fires_on_a_shadow_import(self):
        tree = ast.parse("import tradability_shadow\n")
        found = [n for n in _imported_modules(tree) if "tradability_shadow" in n]
        self.assertEqual(["tradability_shadow"], found)


class OperatorCliExposesNoAuthority(unittest.TestCase):
    """操作员 CLI 的 flag / 只读契约。

    ``work/`` **不在运行时镜像里**（Dockerfile 只 ``COPY backend/frontend/deploy``），
    而 ``docker-smoke`` 会在镜像内跑整套 backend suite。读 ``work/`` 文件的那几条
    断言因此必须**显式跳过并说明原因**，而不是让整个测试文件 FileNotFoundError——
    那会把同文件里不依赖 ``work/`` 的护栏（公开 API 面等值断言、import 边界）一起带走。

    注意跳过的**范围**：只有真正读 ``SHADOW_CLI`` 的用例跳过；检测器自身的非空洞性
    用例（构造一个假 AST 喂给检测器）必须在**任何**环境里都跑，否则"护栏的护栏"就
    在运行时镜像里消失了。

    跳过的代价是可控的：CI 的 ``tests`` job 用的是**完整
    仓库 checkout**，读文件的用例在那里一定会真的执行。
    """

    SKIP_WITHOUT_WORK = "work/ 不在运行时镜像内（docker-smoke 只挂载 backend/frontend/deploy）"

    def _cli_source(self) -> str:
        if not SHADOW_CLI.exists():
            self.skipTest(f"{self.SKIP_WITHOUT_WORK}；CLI 契约由完整 checkout 的 tests job 覆盖")
        return SHADOW_CLI.read_text(encoding="utf-8")

    def test_no_forbidden_flag_is_accepted(self):
        source = self._cli_source()
        accepted = {value for value in _string_constants(ast.parse(source))}
        found = sorted(flag for flag in FORBIDDEN_CLI_FLAGS if flag in accepted)
        self.assertEqual([], found, f"shadow CLI 不得接受 {found}")

    def test_cli_defaults_to_read_only(self):
        source = self._cli_source()
        # 只读工具：不得出现任何写库语句。
        for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE"):
            self.assertNotIn(statement, source, f"shadow CLI 不得包含 {statement}")

    def test_cli_reuses_the_shared_operator_scope_contract(self):
        """CLI 必须复用 backend 的 scope 解析，不得自带第二套。"""
        source = self._cli_source()
        self.assertIn("TB.resolve_codes", source)
        self.assertIn("TB.resolve_sessions", source)
        self.assertIn("TB.ScopeError", source)

    def test_cli_does_not_fabricate_an_entry_session_for_sell(self):
        """卖出方向不得伪造 ``entry_session``（会造出假的 T+1 分歧）。"""
        source = self._cli_source()
        self.assertIn(
            "ST.exit_tradability(evidence, code=code, exit_session=session)", source
        )
        self.assertNotIn("ST.exit_tradability(evidence, code=code, exit_session=session,", source)

    # ── 以下检测器用例**不读文件**，因此在运行时镜像内也必须真的跑 ──

    def test_detector_fires_on_a_forbidden_flag(self):
        """非空洞性：检测器本身必须能抓到禁用的 flag。"""
        tree = ast.parse("parser.add_argument('--apply', action='store_true')\n")
        accepted = _string_constants(tree)
        self.assertIn("--apply", accepted)
        self.assertIn("--apply", FORBIDDEN_CLI_FLAGS)

    def test_detector_fires_on_a_write_statement(self):
        source = "conn.execute('INSERT INTO paper_orders VALUES (1)')\n"
        self.assertIn("INSERT INTO", source)

    def test_detector_ignores_a_read_only_statement(self):
        source = "conn.execute('SELECT COUNT(*) FROM historical_tradability_archive')\n"
        for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE"):
            self.assertNotIn(statement, source)

    def test_detector_fires_on_a_fabricated_entry_session(self):
        """非空洞性：卖出分支里出现 ``entry_session`` 必须能被这段检查抓住。"""
        fabricated = (
            "return ST.exit_tradability(evidence, code=code, exit_session=session, "
            "entry_session=session)\n"
        )
        self.assertIn("entry_session=", fabricated)
        honest = "return ST.exit_tradability(evidence, code=code, exit_session=session)\n"
        self.assertNotIn("entry_session=", honest)


class ShadowModuleIsNotEmbeddedInHotModules(unittest.TestCase):
    """Shadow 必须是独立模块，不得被塞进生产热路径文件里。"""

    def test_shadow_logic_lives_in_its_own_module(self):
        self.assertTrue((BACKEND / SHADOW_MODULE).exists())
        for name in EXECUTION_LEARNING_MODULES:
            path = BACKEND / name
            if not path.exists():  # pragma: no cover
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("ShadowComparator", source, f"{name} 不得内嵌 Shadow")


if __name__ == "__main__":
    unittest.main()
