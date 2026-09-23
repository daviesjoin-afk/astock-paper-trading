# -*- coding: utf-8 -*-
"""``paper_trading`` 架构边界护栏（AST / 源码静态扫描）—— Round-2 第一版。

存在理由只有一个：**position runtime risk state 的 authority 不能再搬回 god
module**，domain implementation 也不能再长回 ``paper_trading.py``。Round-1
把 ``paper_position_risk_state`` 的四个 runtime CRUD helper 直接加进了 1.6 万行
的大文件，于是"唯一 runtime 状态所有者"在源码层并不存在，任何一次顺手修改都能
把 risk-state 读写散回去。护栏把边界钉成可执行断言：

    Guard 1  新 domain 模块禁止反向 import ``paper_trading``；
    Guard 2  ``paper_trading.py`` 不得再出现 risk-state CRUD SQL / 转发 wrapper；
    Guard 4  新 domain 模块不得成为 service locator（零项目级 import）；
    Guard 5  三条生产 SELL 路径必须都经过同一个 episode finalizer；
    Guard 6  ``paper_risk_decision`` 必须是零 I/O / 零 wall-clock 的纯决策边界；
    Guard 7  ``paper_risk_scan_state`` 必须是零 I/O / 零 wall-clock / 零事务的
             扫描生命周期边界，且 ``paper_audit`` 不得再充当执行权威；
    Guard 8  position review 的评分/动作决策必须是零 I/O 纯域模块，入场模型分
             只能来自 episode provenance（``opened_order_id`` → verified BUY
             order → 精确 ``signal_id``），且禁止 latest-signal 搜索回流；
    Guard 9  replacement candidate 与 slot-upgrade 必须是 as-of / cycle 有界的：
             候选 ``intended_date == asof_day`` 且 ``signal_date <= asof_day``，
             历史 review 受 ``(cycle_id, review_date <= asof_day)`` 约束，
             slot context / borrow / rollback 一律使用显式 ``cycle_id``；
    Guard 14 strategy / selection provenance 必须 **version-pinned**：纯契约模块
             零项目依赖、零时钟；版本解析只在 (a) cycle pin、(b) run 创建那一刻的
             current head 两处发生；历史一律读 persisted stamp，永不 ``MAX(version)``
             / ``ORDER BY version DESC``；signal / order 写入器不得重新 stamp
             current version；immutable provenance 列不得被 UPDATE 修正。
"""
import ast
import re
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
PAPER_TRADING = BACKEND / "paper_trading.py"
EXECUTION_PLANNER = BACKEND / "execution_planner.py"
RISK_STATE_MODULE = "paper_position_risk_state.py"
RISK_DECISION_MODULE = "paper_risk_decision.py"
RISK_SCAN_STATE_MODULE = "paper_risk_scan_state.py"
POSITION_REVIEW_MODULE = "paper_position_review.py"
REVIEW_EVIDENCE_MODULE = "paper_position_review_evidence.py"
REPLACEMENT_MODULE = "paper_replacement_decision.py"
REPLACEMENT_EVIDENCE_MODULE = "paper_replacement_evidence.py"

RISK_STATE_TABLE = "paper_position_risk_state"
RISK_SCAN_RUN_TABLE = "paper_risk_scan_runs"

#: Guard 1 —— 这些 domain 模块只允许依赖 stdlib / 各自的低层契约。反向 import
#: ``paper_trading`` 会把它们变成 god module 的延伸，authority 随之泄漏。
DOMAIN_MODULES = (
    "paper_position_risk_state.py",
    "paper_position_read_model.py",
    "paper_portfolio.py",
    "paper_portfolio_read_model.py",
    "paper_cycle_service.py",
    "paper_capital_reservations.py",
    "paper_cycle_capital.py",
    "paper_slot_occupancy.py",
    "paper_risk_exit_eligibility.py",
    "paper_risk_decision.py",
    "paper_risk_scan_state.py",
    "paper_position_review.py",
    "paper_position_review_evidence.py",
    "paper_replacement_decision.py",
    "paper_replacement_evidence.py",
)

#: 历史上已有的例外。当前为空。新模块 ``paper_position_risk_state.py`` 永远不得
#: 进入本表 —— 它的价值恰恰在于"不依赖 paper_trading"。
REVERSE_IMPORT_ALLOWLIST: dict = {}

#: Guard 2 —— risk-state runtime CRUD 只能存在于 authority 模块里。
_RISK_STATE_WRITE = re.compile(
    r"(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)"
    r"\s+" + RISK_STATE_TABLE + r"\b",
    re.IGNORECASE,
)

#: Guard 7 —— risk scan run 的 runtime CRUD 只能存在于状态模块里。
_RISK_SCAN_WRITE = re.compile(
    r"(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)"
    r"\s+" + RISK_SCAN_RUN_TABLE + r"\b",
    re.IGNORECASE,
)

#: Round-1 加进 ``paper_trading.py`` 的四个转发 wrapper；迁出后不得回流。
FORBIDDEN_PAPER_TRADING_DEFS = frozenset({
    "init_position_risk_state",
    "update_position_peak",
    "update_position_take_stage",
    "delete_position_risk_state",
    # R15：纯风险卖出状态机搬进 paper_risk_decision 后不得再长回来。
    "_bought_today",
    "_position_peak",
    "_main_force_intent",
})

#: Guard 3（已移除）—— ``paper_trading.py`` 的 LOC / 模块级函数数曾经是 CI hard gate
#: （R15 → R24 一路向下 ratchet：16365 行 / 287 函数 → 14895 行 / 280 函数）。
#:
#: 它被**删除**而不是继续调阈值，因为 size 不是架构性质：一个"确实需要新增一个有
#: 业务意义的 orchestration wiring"的改动会先撞上 defs 上限，然后被迫把另一个无关
#: 函数机械搬到新文件 —— 结果多出一个 wrapper / helper / import，调用链更长，
#: 代码反而更难维护。这正是本文件要防的方向的反面。
#:
#: 现在由**语义 guard** hard fail（authority 不得回流、provider 不得被绕开、
#: dependency direction 不得违反、historical 不得 current-fill、io/clock/事务边界
#: 不得破坏），``paper_trading.py`` 的 LOC / defs 只作为 review signal 人工观察。
#: 具体规则见 ``ARCHITECTURE.md`` 的 "验证与架构护栏" 一节。

#: Guard 4 —— 新模块允许出现的 import 根（stdlib）。
ALLOWED_STDLIB_IMPORTS = frozenset({"__future__", "datetime", "typing", "sqlite3"})

#: Guard 7 —— 扫描生命周期模块允许的 import 根（纯 stdlib，含 json 做 detail 编码）。
RISK_SCAN_ALLOWED_IMPORTS = frozenset({"__future__", "json", "sqlite3"})

#: Guard 8 —— 纯 position-review 域模块允许的 import 根（仅 stdlib）。
POSITION_REVIEW_ALLOWED_IMPORTS = frozenset({"__future__", "dataclasses", "typing"})

#: Guard 8 —— evidence resolver 允许的 import 根（含仓库唯一的 verified 判据）。
REVIEW_EVIDENCE_ALLOWED_IMPORTS = frozenset({"__future__", "sqlite3", "execution_verification"})

#: Guard 9 —— 纯 replacement 决策域允许的 import 根（仅 stdlib）。
REPLACEMENT_ALLOWED_IMPORTS = frozenset({"__future__", "dataclasses", "json", "typing"})

#: Guard 9 —— replacement evidence 只允许 sqlite conn + stdlib（零反向依赖、零时钟）。
REPLACEMENT_EVIDENCE_ALLOWED_IMPORTS = frozenset({"__future__", "sqlite3"})

#: Guard 9 —— ``paper_trading`` 里不得再出现的 latest-signal / next-day-range 形状。
FORBIDDEN_REPLACEMENT_SHAPES = (
    "ORDER BY signal_date DESC",
    "intended_date>=",
    "intended_date<=",
)

#: Guard 9 —— 旧候选评分 helper 必须迁出后不得回流。
FORBIDDEN_REPLACEMENT_DEFS = frozenset({"_replacement_score_from_signal"})

#: Guard 9 —— 这些函数必须显式接收 cycle_id，函数体内不得再解析 active cycle。
EXPLICIT_CYCLE_FUNCTIONS = (
    "_slot_upgrade_context",
    "_apply_slot_borrow",
    "_rollback_slot_borrow",
)

#: Guard 14 —— provenance 纯契约 + 薄 adapter（R23）。
SELECTION_PROVENANCE_MODULE = "strategy_selection_provenance.py"
SELECTION_RESOLVER_MODULE = "strategy_selection_resolver.py"

#: Guard 14 —— resolver 允许的 import 根：只允许**向下**依赖（契约 + Registry）。
#: 出现 ``paper_trading`` / ``paper_selection`` / ``selection_tracking`` 意味着
#: adapter 反过来依赖调用方，版本权威就重新变成循环的。
SELECTION_RESOLVER_ALLOWED_IMPORTS = frozenset({
    "__future__", "dataclasses", "typing", "strategy_registry",
    "strategy_selection_provenance",
})

#: Guard 14 —— 纯契约模块允许的 import 根（仅 stdlib，且不含 os/sqlite3）。
SELECTION_PROVENANCE_ALLOWED_IMPORTS = frozenset({
    "__future__", "dataclasses", "datetime", "re", "typing",
})

#: Guard 14 —— 版本解析的反模式：只按 version 拿「最新那一版」。
#: ``MAX(version)`` 与 ``ORDER BY version DESC LIMIT 1`` 都会把历史 run 重新解释成
#: current head —— 这正是 R23 要消灭的行为。
FORBIDDEN_VERSION_SEARCH_PATTERNS = (
    r"MAX\s*\(\s*version\s*\)",
    r"ORDER\s+BY\s+version\s+DESC",
    r"ORDER\s+BY\s+current_version\s+DESC",
)

#: Guard 14 —— 历史 provenance 只能**读**：这两张表的 immutable 列不得 UPDATE。
IMMUTABLE_PROVENANCE_TABLES = ("paper_selection_runs", "selection_runs", "paper_signals")

#: Guard 14 —— 「现在能跑什么」的 current resolver 名；cycle 路径不得调用它们。
#: Guard 14 —— 「现在是哪一版」的解析函数名。**只读 current head** 的意思是不带显式
#: version/checksum 去问 Registry；带 version+checksum 的读法是**校验**一个已经
#: 持久化的事实，不构成回退，所以两条路径必须分开断言。
CURRENT_HEAD_RESOLVERS = frozenset({"get_version", "current_version_for_account"})

#: Guard 14 —— signal / order 写入器必须沿用 cycle pin，不得重新取 current head。
STRICT_STAMP_FUNCTIONS = ("_strategy_stamp",)

#: Guard 6 —— 纯决策边界不得触碰的任何 I/O / 时钟 API 名（属性名或调用名）。
FORBIDDEN_IO_CALLS = frozenset({
    "open", "exec", "eval", "compile", "__import__",
    "connect", "cursor", "execute", "executemany", "executescript", "commit",
    "urlopen", "urlretrieve", "socket", "request", "getenv", "environ",
})
FORBIDDEN_CLOCK_ATTRS = frozenset({"today", "now", "utcnow", "time", "time_ns", "monotonic"})

#: Guard 5 —— 生产 SELL 路径 → (文件, 函数名)。
PRODUCTION_SELL_PATHS = (
    ("paper_risk_service.py", "run"),
    ("paper_trading.py", "_intraday_sell"),
    ("execution_planner.py", "execute_order"),
)

FINALIZER_CALL = "finalize_sell("
FINALIZER_OWNER = "PPRS"


def _source(name):
    return (BACKEND / name).read_text(encoding="utf-8")


def _tree(name):
    return ast.parse(_source(name))


def _imported_roots(tree):
    """所有 import 的**根模块名**（``a.b`` → ``a``；``from a import b`` → ``a``）。"""
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _call_names(tree):
    """树里出现的所有被调用名（``f(...)`` 的 ``f`` / ``obj.m(...)`` 的 ``m``）。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _top_level_defs(tree):
    return {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _code_string_constants(tree):
    """模块 / 类 / 函数的 docstring 之外的字符串常量（即真正的 SQL 文本）。"""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _function_source(tree, name, raw):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(raw.splitlines()[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"未找到函数 {name}")


class DomainModulesNeverDependOnPaperTrading(unittest.TestCase):
    """Guard 1 —— authority 边界必须是单向的。"""

    def test_guard1_domain_modules_do_not_import_paper_trading(self):
        for name in DOMAIN_MODULES:
            with self.subTest(module=name):
                self.assertTrue((BACKEND / name).exists(), f"{name} 不存在")
                if name in REVERSE_IMPORT_ALLOWLIST:
                    self.skipTest(f"{name} 是已登记的例外")
                self.assertNotIn(
                    "paper_trading", _imported_roots(_tree(name)),
                    f"{name} 反向 import 了 paper_trading：依赖方向被反转，"
                    "risk-state / 持仓读模型会重新变成大文件的延伸",
                )

    def test_guard1b_risk_state_module_is_never_allowlisted(self):
        self.assertNotIn(
            RISK_STATE_MODULE, REVERSE_IMPORT_ALLOWLIST,
            "新模块的价值就在「不依赖 paper_trading」；不允许把它登记成例外",
        )


class RiskStateCrudStaysInItsOwner(unittest.TestCase):
    """Guard 2 —— runtime CRUD 不得重回 ``paper_trading.py``。"""

    def test_guard2_no_direct_risk_state_crud_in_paper_trading(self):
        hits = [
            (number, line.strip())
            for number, line in enumerate(_source("paper_trading.py").splitlines(), 1)
            if _RISK_STATE_WRITE.search(line)
        ]
        self.assertEqual(
            hits, [],
            "paper_trading.py 又出现了 paper_position_risk_state 的 CRUD SQL；"
            "全部读写必须经 paper_position_risk_state 模块（DDL 仍归 "
            "paper_schema_migrations）",
        )

    def test_guard2b_forbidden_forwarding_wrappers_do_not_come_back(self):
        defined = _top_level_defs(_tree("paper_trading.py"))
        self.assertEqual(
            sorted(defined & FORBIDDEN_PAPER_TRADING_DEFS), [],
            "四个 risk-state CRUD wrapper 被搬回了 paper_trading.py",
        )


# Guard 3（``PaperTradingDoesNotRegrow`` / ``test_guard3_line_count_*`` /
# ``test_guard3b_top_level_function_count_*``）已**整体删除**。
#
# 它曾经断言 ``paper_trading.py`` 的 LOC 与模块级 def 数不得超过固定基线。
# 删除理由：那是 mechanical metric，不是架构不变量。它会把"新增一个有业务意义的
# orchestration 函数"变成 CI 失败，从而逼出"为凑数字而机械搬函数"这种
# 降低可维护性的改动 —— 与护栏存在的目的相反。
#
# 明确不做的事：不换成新的更大阈值、不做 soft/warning 阈值、不做 growth budget。
# size-based gate 整体取消，只保留人工趋势观察（``wc -l`` / 一次性 AST 统计）。
#
# 仍然 hard fail 的是语义 guard（本文件其余全部理由，以及
# ``test_market_data_boundary.py`` 的 MDG-* 环境）：
#   * authority 不得回流 / 不得出现第二份实现
#   * provider 不得被绕过、full-market cache 不得裸读
#   * dependency direction 不得反转、pure domain 不得引入项目级 import
#   * historical 不得 current-fill、implicit current-state 不得重新解析
#   * 零 I/O / 零 wall-clock / 零事务所有权边界不得破坏
#   * 前端不得重复后端业务规则


class RiskStateModuleIsAPureDomainBoundary(unittest.TestCase):
    """Guard 4 —— 新模块必须是纯 domain/infrastructure 边界。"""

    def test_guard4_risk_state_module_has_zero_project_imports(self):
        project_modules = {path.stem for path in BACKEND.glob("*.py")}
        roots = _imported_roots(_tree(RISK_STATE_MODULE))
        leaked = sorted(roots & (project_modules - {Path(RISK_STATE_MODULE).stem}))
        self.assertEqual(
            leaked, [],
            f"{RISK_STATE_MODULE} import 了项目模块 {leaked}；"
            "它只应依赖 stdlib 与调用方交进来的 sqlite connection",
        )
        self.assertEqual(sorted(roots - ALLOWED_STDLIB_IMPORTS), [],
                         "出现了未登记的 import，请审慎评估后再放行")

    def test_guard4b_risk_state_module_does_not_own_transactions_or_resolve_cycles(self):
        """只扫**代码与 SQL 常量**，不扫文档字符串（那里的 ``BEGIN`` 是说明文字）。"""
        tree = _tree(RISK_STATE_MODULE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(
                    node.func.attr, {"commit", "rollback", "executescript", "executemany"},
                    f"状态模块调用了 .{node.func.attr}()：transaction 归调用方所有",
                )
        sql = "\n".join(_code_string_constants(tree)).upper()
        for forbidden in ("BEGIN", "SAVEPOINT", "ROLLBACK", "COMMIT"):
            self.assertNotIn(
                forbidden, sql,
                "状态模块的 SQL 里出现了事务语句：状态收尾必须与成交同处调用方事务",
            )
        for forbidden in ("PAPER_ACCOUNTS", "MAX(CYCLE_ID)"):
            self.assertNotIn(
                forbidden, sql,
                "状态模块不得解析 active cycle：cycle_id 必须由调用方显式传入",
            )


class RiskDecisionModuleIsDeterministic(unittest.TestCase):
    """Guard 6 —— 退出决策只能由显式输入决定，不得自带 I/O 或 wall-clock。"""

    def _call_names(self, tree):
        """所有被调用的名字：``f()`` 取 ``f``，``a.b()`` 取 ``b``。"""
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        return names

    def test_guard6a_risk_decision_module_has_zero_project_imports(self):
        roots = _imported_roots(_tree(RISK_DECISION_MODULE))
        self.assertEqual(sorted(roots - ALLOWED_STDLIB_IMPORTS), [],
                         "出现了未登记的 import：决策引擎不得依赖项目模块或第三方栈")

    def test_guard6b_risk_decision_module_performs_no_io(self):
        tree = _tree(RISK_DECISION_MODULE)
        leaked = sorted(self._call_names(tree) & FORBIDDEN_IO_CALLS)
        self.assertEqual(
            leaked, [],
            f"{RISK_DECISION_MODULE} 出现了 I/O 调用 {leaked}：决策必须是纯函数，"
            "DB / 网络 / 文件 / 进程一律由调用方在边界之外完成",
        )

    def test_guard6c_risk_decision_module_reads_no_wall_clock(self):
        tree = _tree(RISK_DECISION_MODULE)
        leaked = sorted(self._call_names(tree) & FORBIDDEN_CLOCK_ATTRS)
        self.assertEqual(
            leaked, [],
            f"{RISK_DECISION_MODULE} 读取了系统时钟 {leaked}：决策日期只能由 "
            "asof_day 显式传入，禁止任何形式的 wall-clock 回退",
        )

    def test_guard6d_asof_day_is_keyword_only_and_required(self):
        """``asof_day`` 一旦可省，就一定会有人省掉——于是又回到机器当前日期。

        只查公开 API（``__all__`` 里的入口）；模块内私有规整函数由它们间接覆盖。
        """
        tree = _tree(RISK_DECISION_MODULE)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            if "asof_day" not in {arg.arg for arg in node.args.args + node.args.kwonlyargs}:
                continue
            with self.subTest(function=node.name):
                kwonly = {arg.arg for arg in node.args.kwonlyargs}
                self.assertIn(
                    "asof_day", kwonly,
                    f"{node.name} 的 asof_day 不是 keyword-only：位置参数可以漏传，"
                    "等于把 wall-clock 泄漏重新引入",
                )
                defaults = node.args.kw_defaults
                index = [arg.arg for arg in node.args.kwonlyargs].index("asof_day")
                self.assertTrue(
                    defaults[index] is None,
                    f"{node.name} 的 asof_day 带默认值：缺失时必须 fail fast，"
                    "不得静默回退到机器当前日期",
                )

    def test_guard6e_sell_plan_delegates_to_the_pure_engine(self):
        raw = _source("paper_risk_evidence.py")
        body = _function_source(ast.parse(raw), "sell_plan", raw)
        self.assertIn(
            "PRD.evaluate_sell(", body,
            "_sell_plan 不再委托 paper_risk_decision.evaluate_sell —— "
            "退出状态机正在回流到 god module",
        )
        self.assertIn(
            "asof_day=asof_day", body,
            "_sell_plan 没有把显式 as-of 传给决策引擎：R15 的 wall-clock 泄漏会复发",
        )


class EveryProductionSellPathFinalizesTheEpisode(unittest.TestCase):
    """Guard 5 —— episode 收尾只有一条判据、一个 finalizer。"""

    def test_guard5_sell_paths_call_the_shared_finalizer(self):
        """R20：应用层只能调 commit primitive，finalizer 只归 execution_planner。"""
        for filename, function in PRODUCTION_SELL_PATHS:
            with self.subTest(path=f"{filename}:{function}"):
                raw = _source(filename)
                body = _function_source(ast.parse(raw), function, raw)
                if filename == "execution_planner.py":
                    self.assertIn(
                        f"{FINALIZER_OWNER}.{FINALIZER_CALL}", body,
                        "execution_planner.execute_order 不再调用共享的 episode finalizer",
                    )
                else:
                    self.assertIn(
                        "EP.execute_order(", body,
                        f"{filename}:{function} 不再经过唯一成交提交原语",
                    )
                    self.assertNotIn(
                        f"{FINALIZER_OWNER}.{FINALIZER_CALL}", body,
                        f"{filename}:{function} 重新直接调用 episode finalizer；"
                        "成交提交 authority 又出现第二套",
                    )

    def test_guard5b_finalizer_is_not_reached_through_paper_trading(self):
        """execution_planner 必须直接依赖 authority 模块，而不是让 PT 转发。"""
        source = _source("execution_planner.py")
        self.assertIn(f"import {RISK_STATE_MODULE[:-3]} as {FINALIZER_OWNER}", source,
                      "execution_planner 未直接 import paper_position_risk_state")
        self.assertNotIn("PT.finalize_sell(", source,
                         "execution_planner 经 paper_trading 转发调用 finalizer："
                         "依赖方向又被打回 god module")


class RiskScanStateIsACycleOwnedBoundary(unittest.TestCase):
    """Guard 7 —— 风险扫描生命周期必须是零 I/O / 零时钟 / 零事务的独立边界。

    为什么需要它：R16 之前"这次分钟级扫描是否已经跑过"由 ``paper_audit`` 里一条
    JSON 标记决定，标记的 key 只有机器分钟、没有 cycle 身份，而且 wrapper 与 impl
    各自取一次时钟。把这些重新写回 ``paper_trading.py``（或让本模块变成 god
    module 的延伸）就会让同一类缺陷立刻复发，所以边界必须是可执行断言。
    """

    def test_guard7a_scan_state_module_has_zero_project_imports(self):
        project_modules = {path.stem for path in BACKEND.glob("*.py")}
        roots = _imported_roots(_tree(RISK_SCAN_STATE_MODULE))
        leaked = sorted(roots & (project_modules - {Path(RISK_SCAN_STATE_MODULE).stem}))
        self.assertEqual(
            leaked, [],
            f"{RISK_SCAN_STATE_MODULE} 依赖了项目模块 {leaked}：扫描生命周期状态"
            "必须只依赖 stdlib，绝不 import paper_trading（依赖方向单向）",
        )
        self.assertEqual(sorted(roots - RISK_SCAN_ALLOWED_IMPORTS), [],
                         "出现了未登记的 import 根")

    def test_guard7b_scan_state_module_performs_no_io(self):
        tree = _tree(RISK_SCAN_STATE_MODULE)
        # ``execute`` 是合法的：本模块唯一职责就是对调用方传入的连接做
        # 自己的运行表读写。真正要禁的是网络 / 文件 / 进程 / 自建连接。
        forbidden = FORBIDDEN_IO_CALLS - {"execute", "executemany", "cursor",
                                          "connect", "commit"}
        leaked = sorted(_call_names(tree) & forbidden)
        self.assertEqual(
            leaked, [],
            f"{RISK_SCAN_STATE_MODULE} 出现了 I/O 调用 {leaked}：它只能通过调用方"
            "传入的连接读写自己的运行表，网络 / 文件 / 进程 / 自建连接一律禁用",
        )

    def test_guard7c_scan_state_module_reads_no_wall_clock(self):
        tree = _tree(RISK_SCAN_STATE_MODULE)
        leaked = sorted(_call_names(tree) & FORBIDDEN_CLOCK_ATTRS)
        self.assertEqual(
            leaked, [],
            f"{RISK_SCAN_STATE_MODULE} 读取了系统时钟 {leaked}：身份与时间戳只能由"
            "调用方显式传入，否则 scan identity 会自己漂移",
        )

    def test_guard7d_scan_state_module_owns_no_transaction(self):
        raw = _source(RISK_SCAN_STATE_MODULE)
        body = raw[raw.find('"""', raw.find('"""') + 3) + 3:]
        leaked = sorted(_call_names(ast.parse(raw)) & {"commit", "rollback"})
        self.assertEqual(
            leaked, [],
            f"{RISK_SCAN_STATE_MODULE} 出现了事务调用 {leaked}：事务由调用方"
            "（paper_trading.monitor_risk）拥有",
        )
        for token in ("BEGIN", "SAVEPOINT", "COMMIT", "ROLLBACK"):
            with self.subTest(token=token):
                self.assertNotIn(token, body,
                                 f"{RISK_SCAN_STATE_MODULE} 出现了事务语句 {token}")

    def test_guard7e_paper_trading_does_not_crud_the_scan_table(self):
        hits = [
            (number, line.strip())
            for number, line in enumerate(_source("paper_trading.py").splitlines(), 1)
            if _RISK_SCAN_WRITE.search(line)
        ]
        self.assertEqual(
            hits, [],
            "paper_trading.py 直接 CRUD paper_risk_scan_runs；运行时读写必须全部经"
            " paper_risk_scan_state（DDL 仍归 paper_schema_migrations）",
        )

    def test_guard7f_paper_audit_is_not_risk_scan_control_state(self):
        """``paper_audit`` 只允许做 observability，不得再决定"要不要执行"。"""
        raw = _source("paper_trading.py")
        hits = [
            (number, line.strip())
            for number, line in enumerate(raw.splitlines(), 1)
            if "paper_audit" in line and "risk_scan_state" in line
        ]
        self.assertEqual(
            hits, [],
            "paper_trading.py 又用 paper_audit 的 risk_scan_state 标记做控制判断；"
            "执行权威只能是 paper_risk_scan_runs",
        )
        body = _function_source(ast.parse(raw), "monitor_risk", raw)
        self.assertIn(
            "PRSS.claim_scan(", body,
            "monitor_risk 不再通过 paper_risk_scan_state 认领扫描身份",
        )
        impl = _function_source(ast.parse(raw), "_monitor_risk_impl", raw)
        self.assertNotIn(
            "risk_scan_state", impl,
            "_monitor_risk_impl 又碰了 paper_audit 的 risk_scan_state 标记：scan "
            "生命周期必须整体由 monitor_risk + paper_risk_scan_state 负责",
        )
        self.assertNotIn(
            "scan_minute", impl,
            "_monitor_risk_impl 又自己算 scan_minute：身份必须只由 monitor_risk "
            "解析一次，否则跨分钟边界会留下 orphan running",
        )

    def test_guard7g_impl_cycle_id_is_keyword_only_and_required(self):
        tree = ast.parse(_source("paper_trading.py"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "_monitor_risk_impl":
                continue
            kwonly = {arg.arg for arg in node.args.kwonlyargs}
            self.assertIn("cycle_id", kwonly,
                          "_monitor_risk_impl 的 cycle_id 不是 keyword-only："
                          "位置参数可以漏传，等于把跨周期执行重新引入")
            index = [arg.arg for arg in node.args.kwonlyargs].index("cycle_id")
            self.assertIsNone(
                node.args.kw_defaults[index],
                "_monitor_risk_impl 的 cycle_id 带默认值：缺失时必须 fail fast，"
                "不得静默回退到'现在 active 的周期'",
            )
            return
        self.fail("paper_trading.py 里找不到 _monitor_risk_impl")

    def test_guard7h_scan_snapshot_is_pinned_to_the_claimed_cycle(self):
        """风险扫描的持仓读取必须走显式周期，不能重新猜 active cycle。"""
        raw = _source("paper_risk_service.py")
        tree = ast.parse(raw)
        impl = _function_source(tree, "run", raw)
        scope = _function_source(tree, "_bounded_risk_scope", raw)
        self.assertIn(
            "_bounded_risk_scope(", impl,
            "risk service 不再通过显式 cycle/as-of bounded scope 读取持仓",
        )
        self.assertIn(
            "PPort.risk_positions_for_context(", scope,
            "risk service 不再用显式 cycle/as-of bounded reader：一次从旧周期开始的"
            "扫描会重新问'现在 active 的是谁'，从而操作新周期",
        )
        self.assertNotIn(
            "PPRM.positions_for_cycle(", scope,
            "risk service 又回落到 current remaining_qty 读取",
        )
        self.assertIn(
            "PRSS.assert_cycle_active(", impl,
            "_monitor_risk_impl 在外部 I/O 之后不再做 cycle fence："
            "周期 rollover 会带着旧快照继续执行",
        )
        # fence 必须出现在执行侧写入之前（源码顺序只是弱证据，权威是生产回归
        # RISK-SCAN-P4；这里挡住"把 fence 挪到写完订单之后"这种明显回归）。
        fence_at = impl.index("PRSS.assert_cycle_active(")
        for execution_write in ("_consume_available_lots(", "finalize_sell("):
            with self.subTest(write=execution_write):
                if execution_write in impl:
                    self.assertLess(
                        fence_at, impl.index(execution_write),
                        f"cycle fence 出现在 {execution_write} 之后",
                    )


class PositionReviewIsProvenanceBound(unittest.TestCase):
    """Guard 8 —— position review 必须是纯域模块 + episode provenance 绑定。

    为什么需要它：R17 之前持仓的"原始模型分"来自
    ``SELECT ... FROM paper_signals WHERE account_id=? AND code=?
    ORDER BY signal_date DESC,id DESC LIMIT 1`` —— 一个既没有 episode 归属、
    也没有 asof 上界的 latest 搜索。它会被后来无关的 signal 甚至未来 signal
    顶掉，并真实改变自动集中换仓判定。把这套 SQL 或"最近一条 signal"的习惯
    写回去，缺陷立刻复发。
    """

    def test_guard8a_pure_review_module_has_zero_project_imports(self):
        project = {path.stem for path in BACKEND.glob("*.py")}
        roots = _imported_roots(_tree(POSITION_REVIEW_MODULE))
        leaked = sorted(roots & (project - {Path(POSITION_REVIEW_MODULE).stem}))
        self.assertEqual(leaked, [], f"{POSITION_REVIEW_MODULE} 依赖了项目模块 {leaked}")
        self.assertEqual(sorted(roots - POSITION_REVIEW_ALLOWED_IMPORTS), [],
                         "出现了未登记的 import 根")

    def test_guard8b_pure_review_module_performs_no_io_or_clock(self):
        tree = _tree(POSITION_REVIEW_MODULE)
        leaked_io = sorted(_call_names(tree) & FORBIDDEN_IO_CALLS)
        leaked_clock = sorted(_call_names(tree) & FORBIDDEN_CLOCK_ATTRS)
        self.assertEqual(leaked_io, [], f"{POSITION_REVIEW_MODULE} 出现 I/O {leaked_io}")
        self.assertEqual(leaked_clock, [],
                         f"{POSITION_REVIEW_MODULE} 读取系统时钟 {leaked_clock}")

    def test_guard8c_evidence_module_has_no_reverse_dependency(self):
        roots = _imported_roots(_tree(REVIEW_EVIDENCE_MODULE))
        self.assertNotIn("paper_trading", roots,
                         "证据解析器不得反向 import paper_trading")
        self.assertEqual(sorted(roots - REVIEW_EVIDENCE_ALLOWED_IMPORTS), [],
                         "出现了未登记的 import 根")

    def test_guard8d_evidence_module_never_latest_searches(self):
        raw = _source(REVIEW_EVIDENCE_MODULE)
        tree = ast.parse(raw)
        first = tree.body[0]
        body = "\n".join(raw.splitlines()[first.end_lineno:]) if isinstance(
            first, ast.Expr) else raw
        self.assertNotIn("ORDER BY signal_date", body,
                         "证据解析器出现了 latest-signal 排序：episode provenance 只能"
                         "经 opened_order_id → 精确 signal_id 解析")
        self.assertNotIn("LIMIT 1", body,
                         "证据解析器出现了 LIMIT 1：这是 latest 搜索的形状")
        # 归档表是 episode signal 被 _cleanup_stale_data 搬走后的同一行（id 不变），
        # 允许读取，但只允许**精确 id** —— 不得变成"活跃表没有就去归档表搜一条最新的"。
        for number, line in enumerate(body.splitlines(), 1):
            text = line.strip()
            if "paper_signals_archive" not in text or text.startswith("SIGNAL_SOURCES"):
                continue
            self.assertIn("WHERE id=?", text,
                          f"{REVIEW_EVIDENCE_MODULE} 第 {number} 行把归档表用在了"
                          f"精确 id 之外的形状：{text}")

    def test_guard8e_position_quality_requires_explicit_cycle(self):
        tree = ast.parse(_source("paper_risk_evidence.py"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "position_quality_score":
                continue
            kwonly = [arg.arg for arg in node.args.kwonlyargs]
            self.assertIn("cycle_id", kwonly,
                          "_position_quality_score 的 cycle_id 不是 keyword-only："
                          "episode provenance 必须钉在已认领的周期上")
            self.assertIsNone(node.args.kw_defaults[kwonly.index("cycle_id")],
                              "_position_quality_score 的 cycle_id 带默认值：缺失必须 fail fast")
            return
        self.fail("paper_trading.py 里找不到 _position_quality_score")

    def test_guard8f_position_quality_uses_the_resolver(self):
        raw = _source("paper_risk_evidence.py")
        body = _function_source(ast.parse(raw), "position_quality_score", raw)
        self.assertIn("PREV.resolve_entry_signal(", body,
                      "_position_quality_score 不再经 episode provenance 解析入场模型分")
        self.assertNotIn("ORDER BY signal_date DESC", body,
                         "_position_quality_score 又出现了 latest-signal 搜索")

    def test_guard8g_legacy_latest_signal_sql_is_gone(self):
        """整份 paper_trading.py 不得再把 latest signal 当 position quality 证据。"""
        raw = _source("paper_trading.py")
        hits = [
            (number, line.strip())
            for number, line in enumerate(raw.splitlines(), 1)
            if "ORDER BY signal_date DESC" in line
        ]
        self.assertEqual(hits, [], f"latest-signal 排序回流到 paper_trading：{hits}")

    def test_guard8h_save_position_review_has_no_wall_clock_fallback(self):
        raw = _source("paper_risk_evidence.py")
        body = _function_source(ast.parse(raw), "save_position_review", raw)
        # 只查**代码**：注释里会引用旧实现作为历史说明，不应误报。
        code = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn("dt.date.today()", code,
                         "_save_position_review 又用机器今天兜底 review_date")
        self.assertNotIn("date.today()", code,
                         "_save_position_review 又用机器今天兜底 review_date")
        self.assertIn("review_date", code)
        self.assertIn("ValueError", code,
                      "review_date 缺失必须 fail fast，不得静默猜日期")


class ReplacementIsAsOfAndCycleBound(unittest.TestCase):
    """Guard 9 —— replacement / slot-upgrade 必须是 as-of 与 cycle 有界的。

    为什么需要它：R18 之前，一个 risk pass 可以选中 **intended_date = 明天** 的
    signal 作为今天的替补，用它的分数优势触发**今天的** consolidation_exit；随后
    真实 ``_buy_order`` 又因 ``signal_freshness`` 要求 ``intended_date == asof_day``
    把同一个 signal 打成 expired —— 今天纯粹为明天的候选卖了仓。同一批 helper 还
    会自己 ``_active_cycle()`` 并读**没有 as-of 上界**的最新 review。把这三种形状
    写回去，缺陷立刻复发。
    """

    def test_guard9a_pure_replacement_module_has_zero_project_imports(self):
        project = {path.stem for path in BACKEND.glob("*.py")}
        roots = _imported_roots(_tree(REPLACEMENT_MODULE))
        leaked = sorted(roots & (project - {Path(REPLACEMENT_MODULE).stem}))
        self.assertEqual(leaked, [], f"{REPLACEMENT_MODULE} 依赖了项目模块 {leaked}")
        self.assertEqual(sorted(roots - REPLACEMENT_ALLOWED_IMPORTS), [],
                         "出现了未登记的 import 根")

    def test_guard9b_pure_replacement_module_performs_no_io_or_clock(self):
        tree = _tree(REPLACEMENT_MODULE)
        leaked_io = sorted(_call_names(tree) & FORBIDDEN_IO_CALLS)
        leaked_clock = sorted(_call_names(tree) & FORBIDDEN_CLOCK_ATTRS)
        self.assertEqual(leaked_io, [], f"{REPLACEMENT_MODULE} 出现 I/O {leaked_io}")
        self.assertEqual(leaked_clock, [],
                         f"{REPLACEMENT_MODULE} 读取系统时钟 {leaked_clock}")

    def test_guard9c_replacement_evidence_has_no_reverse_dependency(self):
        roots = _imported_roots(_tree(REPLACEMENT_EVIDENCE_MODULE))
        self.assertNotIn("paper_trading", roots,
                         "replacement evidence 不得反向 import paper_trading")
        self.assertEqual(sorted(roots - REPLACEMENT_EVIDENCE_ALLOWED_IMPORTS), [],
                         "出现了未登记的 import 根")
        leaked_clock = sorted(_call_names(_tree(REPLACEMENT_EVIDENCE_MODULE))
                              & FORBIDDEN_CLOCK_ATTRS)
        self.assertEqual(leaked_clock, [],
                         f"{REPLACEMENT_EVIDENCE_MODULE} 读取系统时钟 {leaked_clock}")

    def test_guard9d_candidate_query_is_not_a_next_day_range(self):
        body = _module_body(REPLACEMENT_EVIDENCE_MODULE)
        self.assertNotIn("_next_weekday", body,
                         "candidate 读取又出现了 next-day 推算")
        self.assertNotIn("intended_date>=", body)
        self.assertNotIn("intended_date<=", body)
        self.assertIn("intended_date=?", body,
                      "candidate 的 intended_date 必须是等式，不是 range")

    def test_guard9e_candidate_has_an_asof_upper_bound(self):
        body = _module_body(REPLACEMENT_EVIDENCE_MODULE)
        self.assertIn("signal_date<=?", body,
                      "candidate 读取缺少 signal_date <= asof 上界（future leakage）")

    def test_guard9f_slot_context_cycle_id_is_required(self):
        self._assert_kwonly_required("_slot_upgrade_context", "cycle_id")

    def test_guard9g_slot_context_never_resolves_the_active_cycle(self):
        self._assert_no_active_cycle("_slot_upgrade_context")

    def test_guard9h_borrow_and_rollback_require_explicit_cycle(self):
        for name in ("_apply_slot_borrow", "_rollback_slot_borrow"):
            with self.subTest(function=name):
                self._assert_kwonly_required(name, "cycle_id")
                self._assert_no_active_cycle(name)

    def test_guard9i_historical_review_read_is_bounded(self):
        """evidence 的历史 review 读取必须同时有 cycle_id 与 review_date 上界。"""
        body = _module_body(REPLACEMENT_EVIDENCE_MODULE)
        self.assertIn("cycle_id=?", body)
        self.assertIn("review_date<=?", body,
                      "历史 review 缺少 review_date <= asof 上界（future leakage）")

    def test_guard9j_legacy_replacement_score_helper_is_gone(self):
        defined = _top_level_defs(_tree("paper_trading.py"))
        self.assertEqual(sorted(defined & FORBIDDEN_REPLACEMENT_DEFS), [],
                         "旧候选评分 helper 被搬回了 paper_trading.py；"
                         "生产调用必须直接用 PRep.score_candidate")
        raw = _source("paper_trading.py")
        self.assertNotIn("_replacement_score_from_signal(", raw,
                         "paper_trading.py 仍在调用已迁出的候选评分 helper")

    def test_guard9k_replacement_candidate_never_uses_a_next_day_range(self):
        """候选读取只允许 ``intended_date=?``；不得回到 today..next_day 的 range。

        注意：``intended_date>=`` / ``intended_date<=`` 在仓库别处有**无关**的正当
        用途（例如结算/归档窗口），因此这里只钉 replacement 候选这条读取路径。
        """
        raw = _source("paper_risk_evidence.py")
        body = _function_source(ast.parse(raw), "best_replacement_candidate", raw)
        for shape in FORBIDDEN_REPLACEMENT_SHAPES:
            self.assertNotIn(shape, body,
                             f"_best_replacement_candidate 又出现 {shape!r}："
                             "今天的替补必须属于今天，不能是 next-day range")
        self.assertNotIn("_next_weekday", body,
                         "_best_replacement_candidate 又在推算 next weekday")

    def test_guard9k2_paper_trading_never_latest_signal_searches(self):
        raw = _source("paper_trading.py")
        hits = [
            (number, line.strip())
            for number, line in enumerate(raw.splitlines(), 1)
            if "ORDER BY signal_date DESC" in line
        ]
        self.assertEqual(hits, [], f"latest-signal 排序回流到 paper_trading：{hits}")

    def test_guard9l_candidate_selection_uses_the_evidence_layer_and_pure_module(self):
        raw = _source("paper_risk_evidence.py")
        body = _function_source(ast.parse(raw), "best_replacement_candidate", raw)
        self.assertIn("PREPL.load_replacement_candidates(", body,
                      "候选读取没有经过 as-of 有界的 evidence 层")
        self.assertIn("PRep.choose_best_candidate(", body,
                      "候选排序没有交给纯域模块")

    def test_guard9m_slot_chain_budget_uses_the_explicit_cycle(self):
        """席位预算查询必须显式传 cycle_id（否则一次借位跨越两个周期）。

        `_dynamic_position_limits()` 自身允许 `cycle_id=None`（= active cycle，冷启动
        / 容量退出 / 面板读取的既有语义），但 ``_slot_upgrade_context`` /
        ``_apply_slot_borrow`` 这条在途下单链**必须**把已认领周期传进去，否则
        reviews / 持仓数看显式周期，而 target_limit / donors / allocation_version
        看 active cycle —— 同周期借位会被错误拒绝。
        """
        raw = _source("paper_trading.py")
        for name in ("_slot_upgrade_context", "_apply_slot_borrow"):
            with self.subTest(function=name):
                # 调用可能跨行，所以按空白归一化后再匹配调用形状。
                flat = " ".join(_function_source(ast.parse(raw), name, raw).split())
                self.assertIn(
                    "_dynamic_position_limits( conn, cycle_id=resolved_cycle_id",
                    flat,
                    f"{name} 没有把显式 cycle 交给席位预算查询")
        # pending 席位读取只发生在 _slot_upgrade_context：它一旦跨周期，active
        # cycle 的在途买单就会占掉被请求周期的席位。
        flat = " ".join(
            _function_source(ast.parse(raw), "_slot_upgrade_context", raw).split())
        self.assertIn(
            "_pending_position_slots(conn, positions, cycle_id=resolved_cycle_id",
            flat,
            "_slot_upgrade_context 的 pending 席位仍然跨周期（active cycle 的在途"
            "买单会占掉被请求周期的席位）")

    def test_guard9n_cluster_evidence_follows_the_claimed_cycle(self):
        """进入 fingerprint / allocation version 的簇证据也必须周期与 as-of 有界。

        ``_dynamic_position_limits`` 允许 ``cycle_id=None``（= active cycle），但
        ``_slot_upgrade_context`` / ``_apply_slot_borrow`` 传来的显式周期必须一路走到
        **簇画像**：持仓走 ``positions_for_cycle``、signal 查询有 as-of 上界、成交序列
        固定同一周期。否则 cycle 8 的预算行由 cycle 9 的持仓（甚至未来 signal）决定。
        """
        raw = _source("paper_trading.py")
        profiles = _function_source(ast.parse(raw), "_strategy_cluster_profiles", raw)
        self.assertIn("cycle_id=None", profiles,
                      "_strategy_cluster_profiles 不再接受显式周期")
        self.assertIn("PPRM.positions_for_cycle(", profiles,
                      "簇画像的持仓读取没有固定到显式周期（会重新解析 active cycle）")
        self.assertIn("intended_date<=?", profiles,
                      "簇画像的 signal 查询缺少 as-of 上界（future leakage）")
        self.assertIn("cycle_id=cycle_id", profiles,
                      "簇画像的成交序列没有继承显式周期")
        # 预算侧必须继续把已认领周期 / as-of 交给簇画像。
        limits = " ".join(
            _function_source(ast.parse(raw), "_dynamic_position_limits", raw).split())
        self.assertIn("_strategy_cluster_factors( conn, asof_day, account_ids=account_ids,"
                      " cycle_id=cycle_id,",
                      limits,
                      "_dynamic_position_limits 没有把显式周期/as-of 交给簇画像")

    def test_guard9o_buy_order_capacity_reads_use_the_proven_cycle(self):
        """开仓主路径 ``_buy_order`` 的容量读取也必须钉在已认领周期与 as-of 上。

        这条路径与 ``_slot_upgrade_context`` 是两处独立的 wiring：``_buy_order``
        已经解析过 ``current_cycle``（并据此拒绝了非本周期账户），因此它的
        pending 席位读取与 allocation 预算都不能再回到"现在 active 的是谁"或
        "机器今天"。漏了 pending 的 cycle ⇒ 上一个周期的在途单占掉本周期席位；
        漏了预算的 as-of ⇒ 历史 as-of 下借位前后的版本号不一致。

        R19 之后同一条链上又多出两处资金预算读取（strategy budget 与
        allocation plan），所以这里只钉**下界**：席位预算的两次 (cycle, as-of)
        必须仍在，资金口径的完整性由 Guard 10 单独负责。
        """
        raw = _source("paper_trading.py")
        body = " ".join(
            _function_source(ast.parse(raw), "_buy_order", raw).split())
        self.assertIn(
            "_pending_position_slots(conn, positions, cycle_id=current_cycle[\"id\"])",
            body,
            "_buy_order 的 pending 席位读取没有固定到已认领周期")
        # 初次预算与借位后的 re-read 都必须是同一组 facts（cycle + as-of）。
        # 归一化空白后按出现次数断言，避免依赖换行位置。
        self.assertGreaterEqual(
            body.count("cycle_id=current_cycle[\"id\"], asof_day=asof_day"), 2,
            "_buy_order 里带 (cycle, as-of) 的预算读取少于两次"
            "（初次预算或借位后 re-read 漏传了 provenance）")

    # ── 共用断言 ──────────────────────────────────────────────────────────
    def _assert_kwonly_required(self, name, param):
        tree = ast.parse(_source("paper_trading.py"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != name:
                continue
            kwonly = [arg.arg for arg in node.args.kwonlyargs]
            self.assertIn(param, kwonly,
                          f"{name} 的 {param} 不是 keyword-only：归属必须显式传入")
            self.assertIsNone(
                node.args.kw_defaults[kwonly.index(param)],
                f"{name} 的 {param} 带默认值：缺失必须 fail fast")
            return
        self.fail(f"paper_trading.py 里找不到 {name}")

    def _assert_no_active_cycle(self, name):
        raw = _source("paper_trading.py")
        tree = ast.parse(raw)
        node = _function_node(tree, name)
        body = "\n".join(raw.splitlines()[node.lineno - 1:node.end_lineno])
        # 只查**代码**：docstring 会引用旧实现（``_active_cycle()``）作为历史说明。
        body = _strip_function_docstring(node, body)
        code = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn("_active_cycle(", code,
                         f"{name} 又自己解析 active cycle：一次在途下单的归属"
                         "会被周期翻转掉包")


class EntryCapitalPlanningIsBounded(unittest.TestCase):
    """Guard 10 —— 资金预算/部署证据必须 (cycle, as-of) 有界，成交只能有一个 commit。

    为什么需要它：R19 之前 strategy BUY 的**席位**预算已被 R18 钉在显式
    ``cycle_id`` + ``asof_day`` 上，但同一决策点下游的**资金**预算与部署计划
    仍会重新解析 active cycle、读取"机器今天"的簇证据，并让生效日更晚的
    adaptive risk / adaptive allocation overlay 改写历史 as-of 的预算与数量。
    同时普通 ``_buy_order`` 还维护着第二套 reserve/debit/lot/fill/verification
    写路径。把这四种形状写回去，缺陷都会立刻复发。

    注意两组概念**不可混淆**（§29、§101）：
    - 有界证据：participants / adaptive risk / adaptive allocation / cluster；
    - 全局经济义务：仍然 ``reserved`` 的共享现金（**故意**不做 cycle 过滤，
      否则会造成真实 double-spend）。
    """

    def _flat(self, name, filename="paper_trading.py"):
        """函数源码的**空白无关**形式（换行/缩进不影响断言）。"""
        raw = _source(filename)
        body = _function_source(ast.parse(raw), name, raw)
        return "".join(body.split())

    # ── capital planning provenance ───────────────────────────────────────
    def test_guard10a_pool_inputs_accept_explicit_cycle_and_asof(self):
        tree = ast.parse(_source("paper_trading.py"))
        node = _function_node(tree, "_pool_allocation_inputs")
        kwonly = {arg.arg for arg in node.args.kwonlyargs}
        for param in ("cycle_id", "asof_day"):
            with self.subTest(param=param):
                self.assertIn(
                    param, kwonly,
                    f"_pool_allocation_inputs 的 {param} 不是 keyword-only："
                    "证据边界必须显式传入，位置参数可以漏传")

    def test_guard10b_participant_rows_follow_the_explicit_cycle(self):
        body = self._flat("_pool_allocation_inputs")
        self.assertIn(
            "_shared_account_rows(conn,cycle_id)", body,
            "_pool_allocation_inputs 的参与者账户没有固定到显式周期："
            "历史 as-of 回放会读到 active cycle 的账本")

    def test_guard10c_risk_profile_is_asof_bound_on_both_paths(self):
        """rows path 与 account fallback **都**必须带 as-of（§25/§26）。"""
        body = self._flat("_pool_allocation_inputs")
        self.assertIn(
            "_risk_profile(row,asof_day=asof_day,conn=conn,cycle_id=cycle_id)", body,
            "rows path 的 risk profile 漏传 as-of / cycle")
        self.assertIn(
            "_risk_profile(account,asof_day=asof_day,conn=conn,cycle_id=cycle_id)", body,
            "account fallback 漏传 as-of / cycle：未来 adaptive risk 会污染历史")

    def test_guard10d_adaptive_allocation_weight_is_asof_bound(self):
        tree = ast.parse(_source("paper_trading.py"))
        node = _function_node(tree, "_strategy_pool_weights")
        kwonly = {arg.arg for arg in node.args.kwonlyargs}
        self.assertIn("asof_day", kwonly,
                      "_strategy_pool_weights 的 asof_day 不是 keyword-only")
        body = self._flat("_strategy_pool_weights")
        self.assertIn(
            "_runtime_parameter_active(alloc.get(\"effective_date\"),"
            "asof_day=asof_day,status=alloc.get(\"status\"))",
            body,
            "_strategy_pool_weights 没有把 as-of 交给 overlay 激活判定："
            "生效日更晚的 adaptive allocation 会改写历史 strategy weight")

    def test_guard10e_cluster_evidence_follows_cycle_and_asof(self):
        body = self._flat("_pool_allocation_inputs")
        self.assertIn(
            "_strategy_cluster_factors(conn,asof_day,"
            "account_ids=list(weights),cycle_id=cycle_id,",
            body,
            "簇证据没有继承显式 (cycle, as-of)：机器今天的持仓/未来 signal 会"
            "改变历史 as-of 的簇结构与簇预算")

    def test_guard10f_strategy_budget_and_allocation_plan_carry_provenance(self):
        for name in ("_strategy_pool_budget", "_allocation_plan"):
            with self.subTest(function=name):
                tree = ast.parse(_source("paper_trading.py"))
                node = _function_node(tree, name)
                kwonly = {arg.arg for arg in node.args.kwonlyargs}
                for param in ("cycle_id", "asof_day"):
                    self.assertIn(param, kwonly,
                                  f"{name} 的 {param} 不是 keyword-only")
                body = self._flat(name)
                self.assertIn(
                    "cycle_id=cycle_id,asof_day=asof_day,", body,
                    f"{name} 没有把 (cycle, as-of) 原样传给 _pool_allocation_inputs")

    def test_guard10g_production_buy_callers_pass_cycle_and_asof(self):
        """三条生产买入路径必须显式传 (cycle, as-of)（§33-§36、§75）。"""
        expectations = {
            "_buy_order": ("_strategy_pool_budget(", "_allocation_plan("),
            "_intraday_buyback": ("_strategy_pool_budget(",),
            "_swing_scale_in": ("_strategy_pool_budget(",),
        }
        for name, calls in expectations.items():
            body = self._flat(name)
            for call in calls:
                with self.subTest(function=name, call=call):
                    index = body.index(call)
                    open_at = body.index("(", index)
                    depth = 0
                    end = None
                    for offset in range(open_at, len(body)):
                        if body[offset] == "(":
                            depth += 1
                        elif body[offset] == ")":
                            depth -= 1
                            if depth == 0:
                                end = offset + 1
                                break
                    self.assertIsNotNone(
                        end, f"{name} 的 {call} 调用括号不完整，门禁无法验证")
                    window = body[index:end]
                    self.assertIn("cycle_id=", window,
                                  f"{name} 的 {call} 调用没有显式传 cycle_id")
                    self.assertIn("asof_day=asof_day", window,
                                  f"{name} 的 {call} 调用没有显式传 asof_day")

    def test_guard10h_reserved_cash_stays_a_global_obligation(self):
        """在途预占是全局经济义务：参与者有界，但**资金占用**不得按周期过滤。"""
        body = self._flat("_pool_allocation_inputs")
        index = body.index("_pending_buy_reservations(")
        window = body[index:index + 200]
        self.assertNotIn("cycle_id", window,
                         "在途预占被按周期过滤：旧周期尚未释放的真实 reserved "
                         "cash 仍占用同一个资金池，过滤会造成 double-spend")
        raw = _source("paper_capital_reservations.py")
        self.assertIn("compatibility parameter intentionally ignored", raw,
                      "pending_buy_reservations 的 cycle_id 不再是刻意忽略的兼容参数")

    def test_guard10n_explicit_cycle_has_no_single_account_fallback(self):
        """显式周期的参与者集合就是周期账本；不得再注入调用方账户（§24）。

        否则 `enabled_strategies == []` 的 idle 周期会凭空拿到一份非零预算或
        部署计划 —— 零策略周期本不该有资金表达。
        """
        body = self._flat("_pool_allocation_inputs")
        self.assertIn(
            "ifnotrowsandcycle_idisNone:", body,
            "参与者空列表兜底没有按显式周期设限")
        self.assertIn(
            "ifaccountisnotNoneandaccount.get(\"id\")notinvalues"
            "andcycle_idisNone:", body,
            "账户补入分支没有按显式周期设限：idle 周期会凭空产生参与者")

    def test_guard10o_compiled_profile_is_asof_provable(self):
        """历史 as-of 只融合**可证明当时已生效**的编译风险画像（§25）。"""
        body = self._flat("_risk_profile")
        self.assertIn(
            "andSRE.compiled_profile_is_asof_provable(conn,account_id,asof_day):", body,
            "_risk_profile 无条件融合当前版本编译画像：回放日之后创建的策略版本"
            "会改写历史 weights / allocation")
        helper = self._flat("compiled_profile_is_asof_provable",
                            "strategy_risk_enforcement.py")
        self.assertIn("SR.get_version(", helper,
                      "as-of 可证明性没有查策略版本行")
        self.assertIn("returncreated<=target", helper,
                      "as-of 可证明性没有比较版本创建日与 asof")

    def test_guard10p_terminalizer_never_releases_a_foreign_reservation(self):
        """手动终态化路径同样不得 release 周期冲突的预占（§50）。"""
        body = self._flat("_terminalize_cycle_stale_order", "manual_orders.py")
        self.assertIn(
            "_is_reservation_cycle_mismatch(exc,ReservationCycleMismatch)", body,
            "终态化路径没有识别预占周期冲突")
        guard_at = body.index("ifnotforeign_reservation:")
        release_at = body.index("_finish_capital_reservation(conn,order_id,")
        self.assertLess(
            guard_at, release_at,
            "终态化路径无条件释放预占：冲突的预占属于别的订单")

    def test_guard10q_cycle_capital_resolves_the_pinned_strategy_version(self):
        """explicit cycle 的资金路径必须用 ``paper_cycle_strategy_versions`` 的 pin（§25）。

        ``_risk_profile`` 的 cycle 分支**只能**走 cycle-pinned 版本解析：
        current/latest head（``paper_strategy_version_heads`` / ``get_context``）不是
        exact-cycle 口径的 authority —— 它既可能让历史 cycle 吃到后来版本，也可能在
        head 晚于 asof 时整体丢掉收紧。
        """
        body = self._flat("_risk_profile")
        self.assertIn(
            "ifconnisnotNoneandcycle_idisnotNone:", body,
            "_risk_profile 没有按 explicit cycle 分流策略版本 provenance")
        cycle_branch = body.split("elif", 1)[0]
        self.assertIn(
            "SRE.compiled_profile_for_cycle(conn,account_id,cycle_id=cycle_id)",
            cycle_branch,
            "explicit cycle 分支没有走 cycle-pinned 版本解析")
        self.assertNotIn(
            "SRE.compiled_profile_for(conn,account_id)", cycle_branch,
            "explicit cycle 分支回落到 current head 编译画像：历史 cycle 的资本预算"
            "会被后来的版本改写")
        helper = self._flat("compiled_profile_for_cycle",
                            "strategy_risk_enforcement.py")
        self.assertIn(
            "SR.cycle_version_for_account(conn,str(account_id),cycle_id=int(cycle_id))",
            helper,
            "cycle 口径的画像解析没有走 strict cycle version resolver")
        self.assertNotIn(
            "SR.stamp_for_account(", helper,
            "cycle 口径的画像解析又复用了带 legacy/current-head fallback 的 stamp")
        for shape, label in (
            ("paper_strategy_version_heads", "strategy version heads（current head）"),
            ("paper_strategy_legacy_bindings", "legacy binding"),
            ("get_context(", "strategy_runtime 当前上下文（current head）"),
        ):
            with self.subTest(shape=label):
                self.assertNotIn(
                    shape, helper,
                    f"cycle 口径的画像解析把 {label} 当成了 authority")
        self.assertIn(
            "composite_compiled_profile()", helper,
            "binding 缺失时没有 fail closed 到 Composite（会因 provenance 不可证明而放宽风险）")
        strict = self._flat("cycle_stamp_for_account", "strategy_registry.py")
        self.assertIn(
            "FROMpaper_cycle_strategy_versionsWHEREcycle_id=?ANDaccount_id=?",
            strict,
            "strict cycle resolver 没有只读周期 pin 表")
        for shape in (
            "paper_strategy_legacy_bindings",
            "paper_strategy_version_heads",
            "strategy_definitions",
        ):
            with self.subTest(strict_fallback=shape):
                self.assertNotIn(
                    shape, strict,
                    "strict cycle resolver 混入了非周期 pin 的 fallback")

    def test_guard10r_cluster_dsl_uses_cycle_pinned_version(self):
        """cluster 的结构证据必须与 explicit cycle 的版本 pin 同源（§25）。"""
        body = self._flat("_strategy_cluster_profiles")
        self.assertIn(
            "SRE.compiled_dsl_for_cycle(conn,account_id,cycle_id=cycle_id)", body,
            "explicit cycle 的 cluster DSL 没有走 cycle-pinned resolver")
        cycle_branch = body.split(
            "ifconnisnotNone:try:ifcycle_idisNone:", 1)[1].split("else:", 1)[1].split("except", 1)[0]
        self.assertNotIn(
            "SRT.get_context(", cycle_branch,
            "explicit cycle 的 cluster DSL 又读了 current head")
        helper = self._flat("compiled_dsl_for_cycle", "strategy_risk_enforcement.py")
        self.assertIn(
            "SR.cycle_version_for_account(conn,str(account_id),cycle_id=int(cycle_id))",
            helper,
            "cluster DSL 没有复用 strict cycle version resolver")
        self.assertNotIn("get_context(", helper,
                         "cluster DSL helper 回退到了 current runtime context")

    def test_guard10s_allocation_runtime_uses_cycle_pinned_fields(self):
        """allocation runtime 的版本派生字段必须与 explicit cycle 同源（§25）。"""
        wrapper = self._flat("_strategy_runtimes")
        self.assertIn("SRT.allocation_runtimes(", wrapper,
                      "runtime 组装没有下沉到 strategy_runtime")
        self.assertIn("profiles=profiles", wrapper,
                      "runtime facade 没有传递 cycle-pinned profiles")
        self.assertIn("cycle_id=cycle_id", wrapper,
                      "runtime facade 没有传递 explicit cycle")
        builder = self._flat("allocation_runtimes", "strategy_runtime.py")
        self.assertIn("ifpinned:", builder,
                      "runtime builder 没有区分 explicit cycle provenance")
        self.assertIn(
            'compiled_audit.get("max_positions")', builder,
            "runtime max_positions 没有取 cycle-pinned compiled profile")
        self.assertIn(
            '_number(profile.get("max_exposure"))', builder,
            "runtime own_exposure_cap_pct 没有取 cycle-pinned risk profile")
        cycle_branch = builder.split("ifpinned:", 1)[1].split("else:", 1)[0]
        self.assertNotIn(
            "get_context(", cycle_branch,
            "explicit cycle runtime 又读了 current strategy context")

    def test_guard10t_dynamic_position_limits_are_cycle_asof_bound(self):
        """seat budget 也必须消费 explicit cycle + as-of + pinned runtime。"""
        body = self._flat("_dynamic_position_limits")
        self.assertIn("explicit_cycle=cycle_idisnotNone", body,
                      "seat budget 没有保存 explicit cycle 语义")
        self.assertIn(
            '_risk_profile(row_map.get(account_id)or{"id":account_id}', body,
            "seat-budget risk profile 没有按账户行解析")
        self.assertIn(
            "asof_day=asof_day,conn=conn,cycle_id=cycle_id", body,
            "seat-budget risk profile 漏传 as-of / cycle / conn")
        self.assertIn(
            "_strategy_runtimes(account_ids,weights,diversification=diversification", body,
            "seat-budget runtime 没有走统一组装")
        self.assertIn(
            "conn=conn,profiles=profiles,cycle_id=cycle_id", body,
            "seat-budget runtime 没有启用 pinned profiles/cycle")
        self.assertIn(
            "explicit_empty_cycle=(explicit_cycleand"
            "PCY.explicit_empty_cycle(conn,cycle_id))", body,
            "seat budget 没有区分 explicit empty cycle 与 legacy 缺字段")
        self.assertIn("ifnotaccount_idsandnotexplicit_empty_cycle:", body,
                      "explicit idle cycle 会重新注入全部 builtin")
        clusters = self._flat("_strategy_cluster_profiles")
        self.assertIn(
            "wanted=[str(item)foritemin(list(ACCOUNT_SPECS)ifaccount_idsisNoneelseaccount_ids)]",
            clusters,
            "显式空 account_ids 被 cluster helper 当成默认全量策略")

    def test_guard10u_final_buy_sizing_uses_cycle_pinned_version(self):
        """final sizing profile / effective spec 必须与 cycle pin 同源。"""
        body = self._flat("_buy_order")
        self.assertIn(
            '_risk_profile(account,asof_day=asof_day,conn=conn', body,
            "final sizing profile 漏传 as-of / cycle")
        self.assertIn(
            'cycle_id=current_cycle["id"]', body,
            "final sizing 没有绑定 current cycle")
        self.assertIn(
            'SRE.effective_spec_for_cycle(conn,account["id"]', body,
            "final effective spec 仍走 current/latest 编译画像")
        self.assertIn(
            'ACCOUNT_SPECS.get(account["id"])or{}', body,
            "final effective spec 没有传 base spec")
        helper = self._flat("effective_spec_for_cycle", "strategy_risk_enforcement.py")
        self.assertIn("compiled_profile_for_cycle(conn,account_id,cycle_id=cycle_id)", helper,
                      "effective_spec_for_cycle 没有消费 strict cycle profile")
        self.assertNotIn("compiled_profile_for(conn,account_id)", helper,
                         "effective_spec_for_cycle 回退 current/latest profile")

    def test_guard10v_seat_participants_come_from_cycle_ledger_rows(self):
        """seat budget 的参与者必须是 cycle ledger rows，不能反向筛 ACCOUNT_SPECS。"""
        body = self._flat("_dynamic_position_limits")
        self.assertIn(
            'account_ids=[str(row.get("id"))forrowinrowsifrow.get("id")]', body,
            "seat budget 没有直接消费 cycle ledger rows")
        self.assertNotIn(
            "account_ids=[keyforkeyinACCOUNT_SPECS", body,
            "seat budget 仍用 ACCOUNT_SPECS 过滤参与者：用户策略会被丢掉")

    # ── 普通 BUY 的 commit 收敛 ────────────────────────────────────────────
    def test_guard10i_normal_buy_order_has_no_direct_ledger_writes(self):
        """``_buy_order`` 不再直接写成交账本（§45、§76、§80）。

        注意断言用的是**空白移除后**的形状：``_flat`` 会把函数源码里所有空白
        （包括 SQL 字符串内部的空格）去掉，所以多词 SQL 必须写成
        ``INSERTINTOpaper_fills`` —— 否则断言在扁平化文本上永远匹配不到，
        门禁会静默变成空转（变异矩阵 M-ENT14 专门钉这一点）。
        """
        body = self._flat("_buy_order")
        for shape, label in (
            ("_debit_shared_cash(", "现金扣款"),
            ("_record_lot(", "lot 写入"),
            ("INSERTINTOpaper_fills", "成交流水写入"),
            ("EV.stamp_order(", "执行验证盖章"),
            ("_reserve_shared_capital(", "资金预占"),
        ):
            with self.subTest(shape=label):
                self.assertNotIn(
                    shape, body,
                    f"_buy_order 又直接写成交账本（{label}）：reserve/cash/lot/fill/"
                    "verification 必须整体由 execution_planner.execute_order 负责")
        self.assertIn("commit_strategy_entry_fill(", body,
                      "_buy_order 不再委托统一的成交提交编排")
        self.assertNotIn(
            "UPDATEpaper_ordersSETstatus='filled'", body,
            "_buy_order 又自己把订单改成 filled —— 那是 execute_order 的职责")

    def test_guard10j_commit_orchestration_owns_the_planner_primitive(self):
        body = self._flat("commit_strategy_entry_fill", "manual_orders.py")
        self.assertIn("EP.execute_order(", body,
                      "普通策略 BUY 的提交编排不再调用 execution_planner.execute_order")
        for shape, label in (
            ("_debit_shared_cash(", "现金扣款"),
            ("_record_lot(", "lot 写入"),
            ("INSERTINTOpaper_fills", "成交流水写入"),
            ("EV.stamp_order(", "执行验证盖章"),
        ):
            with self.subTest(shape=label):
                self.assertNotIn(
                    shape, body,
                    f"提交编排自己重写了{label}（{shape}）：唯一 commit primitive 是 "
                    "execution_planner.execute_order（§65），不得复制第二套账本")

    def test_guard10k_mismatch_never_releases_a_foreign_reservation(self):
        """周期归属冲突绝不 release 不属于本订单的预占（§50、§51）。"""
        body = self._flat("strategy_fill_failure", "manual_orders.py")
        mismatch_at = body.index("isinstance(exc,ReservationCycleMismatch)")
        release_at = body.index("_finish_capital_reservation(conn,order_id,")
        self.assertLess(
            mismatch_at, release_at,
            "释放预占出现在周期冲突判定之前：冲突的 reservation 会被误释放")
        branch = body[mismatch_at:release_at]
        self.assertNotIn(
            "_finish_capital_reservation(", branch,
            "周期冲突分支释放了冲突的 reservation —— 那不属于本订单（§50）")
        self.assertIn("risk_rejected", branch,
                      "周期冲突没有把当前订单终态化（§51）")
        self.assertIn("deferred_capacity", branch,
                      "周期冲突后候选意图没有留在复试管道等待新 order identity")

    def test_guard10l_new_reservation_uses_the_expected_order_cycle(self):
        """新预占行的 cycle 必须来自 expected_cycle_id（§19、§78）。"""
        body = self._flat("reserve_shared_capital", "paper_capital_reservations.py")
        self.assertIn(
            "ifexpected_cycle_idisnotNone:reservation_cycle_id="
            "int(expected_cycle_id)", body,
            "新建预占行重新解析 active cycle：订单周期与预占周期会被拆成两个事实")
        self.assertNotIn(
            "cycle=active_cycle_fn(conn)", body,
            "新建预占行回落到 active cycle 作为唯一来源")

    def test_guard10m_production_reservations_carry_the_order_cycle(self):
        """所有 order-backed 生产预占都必须传 expected_cycle_id（§40、§67、§79）。"""
        order_backed = (
            ("manual_orders.py", "submit_manual_order"),
            ("manual_orders.py", "process_pending_manual_orders"),
        )
        for filename, function in order_backed:
            with self.subTest(path=f"{filename}:{function}"):
                body = self._flat(function, filename)
                index = -1
                found = 0
                while True:
                    index = body.find("_reserve_shared_capital(", index + 1)
                    if index < 0:
                        break
                    found += 1
                    window = body[index:index + 320]
                    self.assertIn(
                        "expected_cycle_id=", window,
                        f"{filename}:{function} 的预占调用没有带订单周期")
                self.assertGreater(found, 0,
                                   f"{filename}:{function} 里找不到预占调用（空门禁）")
        # planner 是唯一 commit primitive，它必须自己传订单周期。
        planner = " ".join(_source("execution_planner.py").split()).replace(" ", "")
        index = planner.index("PT._reserve_shared_capital(")
        self.assertIn("expected_cycle_id=order_cycle_id",
                      planner[index:index + 300],
                      "execution_planner.execute_order 没有把订单周期交给预占层")



class SellFillCommitConvergenceIsBounded(unittest.TestCase):
    """Guard 11 —— SELL 决策可以有多条路径，成交提交只能有一个 owner。"""

    def _flat(self, name, filename="paper_trading.py"):
        raw = _source(filename)
        body = _function_source(ast.parse(raw), name, raw)
        return "".join(body.split())

    def _assert_app_sell_delegates(self, function, filename="paper_trading.py"):
        body = self._flat(function, filename)
        for shape, label in (
            ("INSERTINTOpaper_fills", "成交流水写入"),
            ("EV.stamp_order(", "执行验证盖章"),
            ("_credit_shared_cash(", "现金入账"),
            ("_consume_available_lots(", "lot 消耗"),
            ("PPRS.finalize_sell(", "episode 收尾"),
        ):
            with self.subTest(function=function, shape=label):
                self.assertNotIn(
                    shape, body,
                    f"{function} 又直接执行{label}（{shape}）：应用层只能创建订单并调用 "
                    "execution_planner.execute_order",
                )
        self.assertIn(
            "EP.execute_order(", body,
            f"{function} 不再经过唯一成交提交原语 execution_planner.execute_order",
        )

    def test_guard11a_risk_sell_delegates_to_execute_order(self):
        self._assert_app_sell_delegates("run", "paper_risk_service.py")

    def test_guard11b_intraday_sell_delegates_to_execute_order(self):
        self._assert_app_sell_delegates("_intraday_sell")

    def test_guard11c_paper_trading_has_no_runtime_fill_insert(self):
        tree = ast.parse(_source("paper_trading.py"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in {"execute", "executemany"}:
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            sql = str(node.args[0].value or "").upper()
            if "INSERT INTO PAPER_FILLS" in sql:
                offenders.append(node.lineno)
        self.assertEqual(
            offenders, [],
            f"paper_trading.py 第 {offenders} 行又出现 runtime paper_fills INSERT："
            "成交流水必须只由 execution_planner.execute_order 写入",
        )

    def test_guard11d_stamp_order_is_owned_by_execute_order(self):
        source = _source("paper_trading.py")
        self.assertNotIn(
            "EV.stamp_order(", source,
            "paper_trading.py 又直接给成交盖章：execution verification 必须归 execute_order",
        )
        self.assertIn(
            "EV.stamp_order(", _source("execution_planner.py"),
            "execution_planner.execute_order 不再盖执行验证章",
        )

    def test_guard11e_finalize_sell_is_owned_by_execute_order(self):
        self.assertNotIn(
            "PPRS.finalize_sell(", _source("paper_trading.py"),
            "paper_trading.py 又直接收尾 position episode：SELL episode authority 必须归 execute_order",
        )
        self.assertIn(
            "PPRS.finalize_sell(", _source("execution_planner.py"),
            "execution_planner.execute_order 不再收尾 position episode",
        )

    def test_guard11f_sell_commit_keeps_defense_in_depth_order(self):
        body = self._flat("execute_order", "execution_planner.py")
        ordered = (
            "_order_cycle_provenance_for_order(",
            "_assert_order_identity(",
            "_assert_order_execution_cycle(",
            "_consume_available_lots(",
        )
        positions = []
        for shape in ordered:
            self.assertIn(shape, body, f"execute_order 缺少 SELL 防守链：{shape}")
            positions.append(body.index(shape))
        self.assertEqual(
            positions, sorted(positions),
            "execute_order 的 SELL 防守链顺序被打乱：provenance → identity → execution-cycle → lot mutation",
        )
        self.assertNotIn(
            "_active_cycle(", body,
            "execute_order 重新解析 active cycle 决定成交归属",
        )



class RiskApplicationServiceBoundary(unittest.TestCase):
    """Guard 12 —— one claimed risk run is owned by the application service."""

    SERVICE = "paper_risk_service.py"
    EVIDENCE = "paper_risk_evidence.py"

    def test_guard12a_no_reverse_dependency(self):
        for filename in (self.SERVICE, self.EVIDENCE):
            roots = _imported_roots(_tree(filename))
            self.assertNotIn("paper_trading", roots,
                             f"{filename} reverse-imports paper_trading")

    def test_guard12b_paper_trading_facade_is_thin(self):
        raw = _source("paper_trading.py")
        body = _function_source(ast.parse(raw), "_monitor_risk_impl", raw)
        self.assertLessEqual(
            len(body.splitlines()), 40,
            "_monitor_risk_impl 不再是 thin compatibility adapter",
        )
        self.assertIn("PRSVC.run(", body,
                      "_monitor_risk_impl 不再委托 paper_risk_service.run")
        self.assertNotIn("for position in positions", body,
                         "_monitor_risk_impl 又长出 risk loop")

    def test_guard12c_explicit_context_and_no_active_cycle_resolution(self):
        raw = _source(self.SERVICE)
        run_body = _function_source(ast.parse(raw), "run", raw)
        flat = "".join(run_body.split())
        self.assertIn("run(context:RiskRunContext,*,ports:RiskServicePorts)", flat,
                      "paper_risk_service.run 没有显式接收 RiskRunContext")
        self.assertNotIn("_active_cycle(", flat,
                         "risk run 重新解析 active cycle")
        self.assertNotIn("date.today", flat,
                         "risk run 重新读取机器日期作为决策身份")
        self.assertNotIn("datetime.now().date", flat,
                         "risk run 从机器时钟推导决策日期")
        self.assertIn("class RiskRunContext", raw)
        self.assertIn("cycle_id: int", raw)
        self.assertIn("asof_day: dt.date", raw)
        self.assertIn("requires explicit cycle_id", raw)
        self.assertIn("requires explicit asof_day", raw)

    def test_guard12d_bounded_capital_context(self):
        service = "".join(_source(self.SERVICE).split())
        self.assertIn(
            "ports.dynamic_position_limits(conn,cycle_id=cycle_id,asof_day=day,)",
            service,
            "risk service dynamic limits 没有绑定 cycle/as-of",
        )
        self.assertIn("policy_override=ports.risk_profile(", service)
        self.assertIn("asof_day=day,conn=conn,cycle_id=cycle_id,", service,
                      "risk service downside profile 没有绑定 cycle/as-of/conn")
        self.assertIn(
            "SRE.effective_spec_for_cycle(conn,position[\"account_id\"],base_spec,cycle_id=cycle_id,)",
            service,
            "risk service SELL spec 没有使用 cycle-pinned effective spec",
        )
        self.assertNotIn("SRE.effective_spec(conn", service,
                         "risk service 仍直接使用 current-head effective spec")
        self.assertNotIn("ports.dynamic_position_limits(conn)", service,
                         "risk service dynamic limits 又省略 cycle/as-of")
        self.assertNotIn(
            "ports.risk_profile(account_map.get(position[\"account_id\"])or{\"id\":position[\"account_id\"]},)",
            service,
        )

    def test_guard12e_sell_authority_stays_in_execution_planner(self):
        service = "".join(_source(self.SERVICE).split())
        for shape, label in (
            ("INSERTINTOpaper_fills", "成交流水写入"),
            ("EV.stamp_order(", "执行验证盖章"),
            ("_credit_shared_cash(", "现金入账"),
            ("_consume_available_lots(", "lot 消耗"),
            ("PPRS.finalize_sell(", "episode 收尾"),
        ):
            with self.subTest(shape=label):
                self.assertNotIn(shape, service,
                                 f"risk service 直接执行{label}")
        self.assertIn("EP.execute_order(", service,
                      "risk service 不再经过 execution_planner.execute_order")

    def test_guard12f_pure_domain_direction(self):
        forbidden = {
            "paper_trading", "paper_risk_service", "sqlite3",
            "requests", "urllib", "httpx", "aiohttp", "socket",
            "data_fetcher", "alt_data",
        }
        for module in (
            "paper_risk_decision.py",
            "paper_position_review.py",
            "paper_replacement_decision.py",
        ):
            roots = _imported_roots(_tree(module))
            leaked = sorted(roots & forbidden)
            self.assertEqual(
                leaked, [],
                f"{module} reverse-imports {leaked}; pure decision modules stay pure",
            )

    def test_guard12h_user_spec_is_cycle_pinned(self):
        raw = _source(self.SERVICE)
        body = _function_source(ast.parse(raw), "_spec_for", raw)
        self.assertIn(
            "SRT.get_context_for_cycle(", body,
            "paper_risk_service._spec_for 仍读取 current strategy head",
        )
        self.assertNotIn(
            "SRT.get_context(", body,
            "paper_risk_service._spec_for 回退到 current head context",
        )
        service = "".join(raw.split())
        self.assertEqual(
            service.count("_spec_for(position[\"account_id\"],conn,cycle_id=cycle_id)"),
            2,
            "risk service 的 stale/fresh SELL 路径没有全部把 explicit cycle 传给 _spec_for",
        )
        context = _function_source(ast.parse(_source("strategy_runtime.py")), "get_context_for_cycle", _source("strategy_runtime.py"))
        self.assertIn("SR.cycle_version_for_account(", context)
        self.assertNotIn("SR.get_version(", context)
        self.assertNotIn("stamp_for_account(", context)

    def test_guard12i_risk_stamps_use_strict_cycle_resolver(self):
        raw = _source(self.SERVICE)
        body = _function_source(ast.parse(raw), "_strategy_stamp", raw)
        self.assertIn("SR.cycle_stamp_for_account(", body)
        self.assertNotIn("SR.stamp_for_account(", body)
        service = "".join(raw.split())
        self.assertIn("_strategy_stamp(conn,account_id,cycle_id=cycle_id)", service)
        self.assertEqual(
            service.count("_strategy_stamp(conn,position[\"account_id\"],cycle_id=cycle_id)"),
            2,
            "unfilled / pending_execution SELL 没有全部把 explicit cycle 传给 stamp resolver",
        )

    def test_guard12j_execute_order_inherits_durable_order_stamp(self):
        raw = _source("execution_planner.py")
        commit = _function_source(ast.parse(raw), "execute_order", raw)
        self.assertIn("order_strategy_stamp = _assert_order_identity(", commit)
        self.assertIn("PT._risk_log(", commit)
        self.assertIn("PT._audit(", commit)
        self.assertGreaterEqual(commit.count("strategy_stamp=order_strategy_stamp"), 2)
        identity = _function_source(ast.parse(raw), "_assert_order_identity", raw)
        self.assertIn("strategy_id, strategy_version, strategy_checksum", identity)
        self.assertIn("partial strategy stamp", identity)
        for forbidden in ("SR.stamp_for_account(", "SR.get_version(", "SRT.get_context("):
            self.assertNotIn(
                forbidden, commit,
                f"execute_order 重新解析 strategy provenance：{forbidden}",
            )

    def test_guard12k_unknown_stamp_exception_is_table_aware(self):
        raw = _source("paper_schema_migrations.py")
        start = raw.index("STRATEGY_STAMP_UNKNOWN_ALLOWANCE = {")
        end = raw.index("\n}\n", start)
        allowance = raw[start:end]
        self.assertIn('"paper_orders"', allowance)
        self.assertIn('"paper_risk_decisions"', allowance)
        self.assertIn('"paper_audit"', allowance)
        self.assertNotIn('"paper_signals"', allowance)
        self.assertIn("NEW.side='sell'", allowance)
        self.assertIn("NEW.cycle_id IS NOT NULL", allowance)
        self.assertIn("pending_execution", allowance)
        self.assertIn("sell_filled", allowance)
        self.assertIn("protective_exit_recovery_watch", allowance)
        self.assertIn("quality_rotation", allowance)
        self.assertIn("concentration_rotation", allowance)
        self.assertIn("permission_scope_exit", allowance)
        # Behavioral allow/reject coverage lives in
        # test_strategy_versioning.DbStrat* / test_db_strat_*.

    def test_guard12l_post_fill_sell_audits_inherit_existing_stamp(self):
        raw = _source(self.SERVICE)
        start = raw.rindex("            if concentration_triggered:")
        end = raw.index("            orders.append({", start)
        post_fill = raw[start:end]
        self.assertEqual(
            post_fill.count("_audit("), 3,
            "post-fill rotation/capacity audit calls changed unexpectedly",
        )
        for event in (
            "quality_rotation",
            "concentration_rotation",
            "permission_scope_exit",
        ):
            self.assertIn(f'"{event}"', post_fill)
        self.assertEqual(
            post_fill.count("strategy_stamp=strategy_stamp"), 3,
            "post-fill audit does not inherit the durable SELL strategy stamp",
        )
        for forbidden in (
            "SR.stamp_for_account(", "SR.cycle_stamp_for_account(",
            "SRT.get_context(", "SRT.get_context_for_cycle(",
        ):
            self.assertNotIn(
                forbidden, post_fill,
                f"post-fill SELL audit re-resolves strategy provenance: {forbidden}",
            )

    def test_guard12g_service_is_not_a_monolith(self):
        loc = len(_source(self.SERVICE).splitlines())
        self.assertLess(loc, 950, f"paper_risk_service.py grew to {loc} LOC")


class PortfolioReadModelIsCycleAsOfBounded(unittest.TestCase):
    """Guard 13 —— portfolio read model 必须只读、显式上下文、无 current 回退。"""

    MODULE = "paper_portfolio_read_model.py"

    def test_guard13a_no_reverse_dependency(self):
        self.assertNotIn("paper_trading", _imported_roots(_tree(self.MODULE)))

    def test_guard13b_read_model_has_no_execution_mutation(self):
        tree = _tree(self.MODULE)
        sql = "\n".join(_code_string_constants(tree)).upper()
        for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE", "ALTER TABLE", "DROP TABLE"):
            self.assertNotIn(forbidden, sql, f"read model 出现写语句：{forbidden}")
        calls = _call_names(tree)
        self.assertFalse(calls & {"commit", "rollback", "executescript", "executemany"})

    def test_guard13c_no_active_cycle_or_wall_clock(self):
        body = _module_body(self.MODULE)
        for forbidden in ("active_cycle", "current_cycle", "latest_cycle",
                          "date.today", "datetime.now", "datetime.utcnow"):
            self.assertNotIn(forbidden, body, f"read model 回退 current/wall-clock：{forbidden}")

    def test_guard13d_historical_entrypoint_requires_explicit_context(self):
        raw = _source(self.MODULE)
        body = _function_source(_tree(self.MODULE), "portfolio_for_cycle", raw)
        self.assertIn("PortfolioReadContext(cycle_id=cycle_id, asof_day=asof_day)", body)

    def test_guard13e_shared_exposure_accepts_explicit_cycle(self):
        body = _function_source(_tree("paper_trading.py"),
                                "_shared_account_exposure", _source("paper_trading.py"))
        self.assertIn("cycle_id=None", body)
        self.assertIn("PPRM.positions_for_cycle", body)

    def test_guard13f_risk_service_passes_cycle_to_shared_exposure(self):
        service = _source("paper_risk_service.py")
        self.assertIn("ports.shared_exposure(", service)
        self.assertIn("cycle_id=cycle_id", service)

    def test_guard13g_no_schema_change(self):
        body = _module_body(self.MODULE).upper()
        self.assertNotIn("CREATE TABLE", body)
        self.assertNotIn("ALTER TABLE", body)
        self.assertNotIn("DROP TABLE", body)


class SelectionProvenanceIsVersionPinned(unittest.TestCase):
    """Guard 14 —— strategy / selection provenance 必须 version-pinned（R23）。

    这一组护栏存在的理由：R22 之前「这个 run 是哪个策略版本跑的」没有持久事实，
    历史 run 会被**今天的** current head 重新解释（改个名字、升一版，昨天的选股就
    换了个解释），而 checksum / as-of / cycle 全都可以在没有门禁的情况下被顺手
    「修好」。所以这里把边界钉成静态断言，能 AST 的不用 grep，能用 AST 位置信息
    的不用子串。
    """

    # ---------- pure contract ----------

    def test_guard14a_contract_module_has_no_project_imports(self):
        roots = _imported_roots(_tree(SELECTION_PROVENANCE_MODULE))
        self.assertEqual(sorted(roots - SELECTION_PROVENANCE_ALLOWED_IMPORTS), [],
                         "纯契约模块 import 了项目模块 / 非白名单 stdlib")

    def test_guard14b_contract_module_never_reads_clock_or_db(self):
        tree = _tree(SELECTION_PROVENANCE_MODULE)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {
                    "today", "now", "utcnow", "time", "time_ns", "monotonic"}:
                offenders.append(node.attr)
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name in {"connect", "cursor", "execute", "executescript", "commit",
                            "getenv"}:
                    offenders.append(name)
        self.assertEqual(offenders, [],
                         f"纯契约模块触碰了时钟 / DB / 环境：{offenders}")

    def test_guard14c_contract_module_declares_the_scope_invariants(self):
        """scope 语义必须是契约的一部分，而不是调用方各自记得的约定。"""
        raw = _source(SELECTION_PROVENANCE_MODULE)
        self.assertIn("SCOPE_CYCLE", raw)
        self.assertIn("SCOPE_RESEARCH", raw)
        body = _function_source(_tree(SELECTION_PROVENANCE_MODULE),
                                "__post_init__", raw)
        self.assertIn("cycle scope requires an explicit cycle id", body)
        self.assertIn("research scope must not carry a cycle id", body)

    # ---------- thin resolver ----------

    def test_guard14d_resolver_only_depends_downwards(self):
        roots = _imported_roots(_tree(SELECTION_RESOLVER_MODULE))
        self.assertEqual(sorted(roots - SELECTION_RESOLVER_ALLOWED_IMPORTS), [],
                         "resolver 依赖了调用方（版本权威会变回循环）")

    def test_guard14e_resolver_has_no_version_search_or_clock(self):
        body = _module_body(SELECTION_RESOLVER_MODULE)
        for pattern in FORBIDDEN_VERSION_SEARCH_PATTERNS:
            self.assertIsNone(re.search(pattern, body, re.IGNORECASE),
                              f"resolver 用「最新那一版」解释历史：{pattern}")
        for forbidden in ("date.today", "datetime.now", "utcnow"):
            self.assertNotIn(forbidden, body, f"resolver 读了 wall-clock：{forbidden}")

    def test_guard14f_cycle_resolution_goes_through_the_pin(self):
        body = _function_source(_tree(SELECTION_RESOLVER_MODULE),
                                "cycle_provenance", _source(SELECTION_RESOLVER_MODULE))
        self.assertIn("cycle_version_for_account", body,
                      "cycle scope 必须走 cycle pin，不能自己找版本")
        self.assertNotIn("get_version(", body,
                         "cycle scope 不得读 current head")
        self.assertNotIn("paper_accounts", body,
                         "requested cycle 不得从 paper_accounts.cycle_id 推导")

    def test_guard14g_current_head_is_read_only_at_run_creation(self):
        """current head（**不带** version 的读取）只能出现在 run 创建入口。

        ``get_version(id, version, checksum=…)`` 校验的是一个**已经持久化**的事实，
        不是「现在是哪一版」，因此只把没有显式 version 的调用判为回退。
        """
        tree = _tree(SELECTION_RESOLVER_MODULE)
        head_readers = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr in CURRENT_HEAD_RESOLVERS):
                    continue
                if len(call.args) >= 2:
                    continue  # 带显式 version → 校验，不是回退
                head_readers.append(node.name)
        self.assertEqual(sorted(set(head_readers)), ["research_version_pin"],
                         f"current head 被这些入口读取了：{sorted(set(head_readers))}")

    def test_guard14h_paper_trading_never_version_searches_provenance(self):
        """god module 里不得出现「找最新那一版」，也不得自己推导周期归属。

        ``current_cycle`` 是本文件既有的**局部变量名**（执行路径解析出的周期），
        不是回退；这里禁的是把「当前周期」当解析入口用的写法。
        """
        raw = _source("paper_trading.py")
        tree = _tree("paper_trading.py")
        body = _module_body("paper_trading.py")
        for pattern in FORBIDDEN_VERSION_SEARCH_PATTERNS:
            self.assertIsNone(re.search(pattern, body, re.IGNORECASE),
                              f"paper_trading 出现「最新版本」搜索：{pattern}")
        for forbidden in ("_current_cycle()", "latest_cycle(", "active_cycle_id("):
            self.assertNotIn(forbidden, body,
                             f"paper_trading 回退 current/latest：{forbidden}")
        self.assertNotIn(
            "FROM paper_accounts WHERE id=?",
            _function_source(tree, "_strategy_stamp", raw),
            "_strategy_stamp 自己推导周期归属（应经 SRES.signal_cycle_provenance）")

    def test_guard14i_signal_writer_uses_the_strict_pin_resolver(self):
        """signal / order 写入戳的权威来源必须唯一，且不得读 current head。

        血缘查询本身已归 resolver（``signal_order_provenance``），所以这里断言
        god module 是**委托**而不是自带一份 SELECT；它同时不得自己推导周期归属。
        """
        raw = _source("paper_trading.py")
        tree = _tree("paper_trading.py")
        for name in STRICT_STAMP_FUNCTIONS:
            body = _function_source(tree, name, raw)
            self.assertIn("signal_order_provenance", body,
                          f"{name} 不再经 resolver 继承 signal 的因果戳")
            self.assertIn("paper_signals", _source(SELECTION_RESOLVER_MODULE),
                          "signal 血缘的唯一查询点消失了")
            self.assertNotIn("get_version(", body,
                             f"{name} 重新取了 current head")
            self.assertNotIn("FROM paper_signals", body,
                             f"{name} 自己复制了一套 signal 查询（双重 authority）")
        # 严格解析器只存在于 resolver，``paper_trading`` 只做别名转发。
        self.assertIn("SRES.signal_cycle_provenance", raw)
        self.assertNotIn("class SignalCycleUnprovable", raw,
                         "SignalCycleUnprovable 被复制回 god module（双重 authority）")
        # 两个 signal 写入点都必须冻结写入上下文，且把**候选构建前捕获的**周期与
        # 提交时观测到的周期都交给 resolver（详见 guard 14n / 14o）。
        for function in ("generate_signals", "_bootstrap_signals_for_today"):
            body = _function_source(tree, function, raw)
            self.assertIn("signal_write_context_or_error(", body,
                          f"{function} 没有冻结 signal 的写入上下文")
            self.assertIn("cycle_id=", body,
                          f"{function} 没有把已解析的周期显式传给 resolver")
            self.assertNotIn("get_version(", body,
                             f"{function} 在写入路径里读了 current head")

    def test_guard14j_immutable_provenance_columns_are_never_updated(self):
        """immutable stamp 只能被 INSERT：任何 ``UPDATE … SET strategy_version`` 都是回填。"""
        for module in ("paper_trading.py", "paper_selection.py",
                       "selection_tracking.py"):
            tree = _tree(module)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "execute"):
                    continue
                sql = node.args[0] if node.args else None
                if not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)):
                    continue
                upper = sql.value.upper()
                if "UPDATE " not in upper:
                    continue
                target = upper.split("UPDATE ", 1)[1].split()[0].strip("\"'")
                if target not in IMMUTABLE_PROVENANCE_TABLES:
                    continue
                # provenance 列出现 SET 里（或出现在 BEFORE UPDATE 触发器名里）即违规
                bad = [column for column in
                       ("STRATEGY_VERSION", "STRATEGY_CHECKSUM", "ASOF_DAY",
                        "PROVENANCE_KEY")
                       if "SET " in upper and column in upper]
                self.assertEqual(bad, [],
                                 f"{module} 用 UPDATE 改写 {target} 的 provenance：{bad}")

    def test_guard14k_provenance_readers_exist_in_one_place(self):
        """历史读取只有一个实现：``reading_from_row``（+ resolver 的薄转发）。"""
        raw = _source(SELECTION_PROVENANCE_MODULE)
        tree = _tree(SELECTION_PROVENANCE_MODULE)
        body = _function_source(tree, "reading_from_row", raw)
        self.assertIn("provenance_status", body)
        # 反向：任何其他模块都不得自己实现一套 status 推断
        for module in ("paper_selection.py", "selection_tracking.py", "paper_trading.py"):
            module_body = _module_body(module)
            for invented in ("STATUS_LEGACY_UNPROVEN", "STATUS_NOT_APPLICABLE"):
                if invented in module_body:
                    self.assertIn(
                        "SP.", module_body,
                        f"{module} 自己发明了 provenance 状态 {invented}")
            # 只禁**字面量**：``f"...provenance_status = '{SP.STATUS_VERIFIED}'"``
            # 是引用契约常量生成 SQL，恰恰是「不手写状态」的做法。
            tree = _tree(module)
            templated = {
                id(part) for node in ast.walk(tree)
                if isinstance(node, ast.JoinedStr) for part in node.values
            }
            docstrings = {
                id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            }
            literal = [
                node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in templated and id(node) not in docstrings
                and re.search(r"provenance_status\s*=\s*['\"]", node.value)
            ]
            self.assertEqual(literal, [],
                             f"{module} 手写 provenance_status 字面量：{literal[:1]}")

    def test_guard14l_signal_cycle_requires_a_keyword_only_explicit_cycle(self):
        """Guard 14l：``signal_cycle_provenance`` 的 ``cycle_id`` 必须是 keyword-only 必填。

        签名本身就是 authority 边界：只要 ``cycle_id`` 有默认值（或能按位置省略），
        调用方就能不带周期调用它，解析器于是又得自己去猜一个 —— 那正是
        「重新解析 mutable ``paper_accounts.cycle_id``」的入口。
        """
        tree = _tree(SELECTION_RESOLVER_MODULE)
        node = _function_node(tree, "signal_cycle_provenance")
        args = node.args
        required = [a.arg for a in args.args] + [a.arg for a in args.kwonlyargs]
        self.assertIn("cycle_id", required,
                      "signal_cycle_provenance 不再接受显式 cycle_id")
        self.assertIn("cycle_id", [a.arg for a in args.kwonlyargs],
                      "cycle_id 必须是 keyword-only（否则调用方可以按位置/省略传入）")
        defaults = list(args.defaults) + [d for d in args.kw_defaults if d is not None]
        self.assertEqual(len(defaults), 0,
                         f"signal_cycle_provenance 的参数 {sorted(required)} 里出现了默认值："
                         "cycle_id 一旦有默认值，调用方就能省略它")
        # 位置参数也不得能承载周期：cycle_id 必须**只**能在 kwonly 段出现。
        self.assertNotIn("cycle_id", [a.arg for a in args.args],
                         "cycle_id 仍可作为位置参数传入（keyword-only 契约被绕过）")

    def test_guard14m_signal_cycle_body_never_reads_paper_accounts(self):
        """Guard 14m：``signal_cycle_provenance`` 函数体不得读 ``paper_accounts``。

        周期归属是**调用方**在写事务里已经确定的事实。函数体自己去查账户当前
        周期，就是把一个可变的 current-state 当成 write-time authority ——
        I/O 期间发生的 rollover 会被静默采纳，旧候选被盖成新周期的策略版本。
        """
        raw = _source(SELECTION_RESOLVER_MODULE)
        tree = _tree(SELECTION_RESOLVER_MODULE)
        node = _function_node(tree, "signal_cycle_provenance")
        body = _strip_function_docstring(
            node, _function_source(tree, "signal_cycle_provenance", raw))
        self.assertNotIn("paper_accounts", body,
                         "signal_cycle_provenance 又去 paper_accounts 取周期了")
        for forbidden in ("active_cycle", "latest_cycle", "current_cycle",
                          "ORDER BY id DESC", "MAX(id)"):
            self.assertNotIn(forbidden, body,
                             f"signal_cycle_provenance 出现周期搜索：{forbidden}")
        # ``SR.stamp_for_account`` 有 legacy/current-head 回退，必须禁；但
        # ``cycle_stamp_for_account`` 是唯一合法权威，所以按调用属性名精确判，
        # 不能用子串（前者是后者的子串，子串判断会把正确实现判成违规）。
        for node_call in ast.walk(node):
            if isinstance(node_call, ast.Call) and isinstance(node_call.func, ast.Attribute):
                self.assertNotEqual(
                    node_call.func.attr, "stamp_for_account",
                    "signal_cycle_provenance 回退到了 SR.stamp_for_account"
                    "（legacy binding / current-head）")
        self.assertIn("cycle_stamp_for_account", body,
                      "signal 的版本权威必须仍是 cycle pin")
        self.assertIn("cycle_id=requested", body,
                      "cycle pin 查询没有把显式 cycle 传下去")

    def test_guard14n_generate_signals_passes_a_frozen_explicit_cycle(self):
        """Guard 14n：``generate_signals`` 必须把**候选构建前捕获的**周期传下去。

        候选/provenance/写入三者的周期必须是同一个：捕获归属来自 provider I/O
        之前的账户行，提交时再观测一次，两者不一致即整批 stale —— 绝不重新取
        新周期，然后把旧候选 stamp 到新周期。
        """
        raw = _source("paper_trading.py")
        tree = _tree("paper_trading.py")
        body = _function_source(tree, "generate_signals", raw)
        self.assertIn("signal_write_context_or_error", body,
                      "generate_signals 不再经 resolver 冻结写入上下文")
        self.assertIn("cycle_id=account[\"cycle_id\"]", body,
                      "generate_signals 没有把候选构建前捕获的周期显式传下去")
        self.assertIn("account_cycle_id=current[\"cycle_id\"]", body,
                      "generate_signals 没有把提交时观测到的账户周期交给校验")
        self.assertNotIn("_cycle_signal_provenance(", body,
                         "generate_signals 仍直接调用旧的 cycle 解析入口")
        self.assertNotIn("get_version(", body,
                         "generate_signals 在写入路径里读了 current head")

    def test_guard14o_bootstrap_signals_passes_an_explicit_cycle(self):
        """Guard 14o：``_bootstrap_signals_for_today`` 必须传显式 cycle + 捕获归属。

        该函数已经持有解析好的 cycle 上下文；它不得在写入前**再次**推导账户当前
        周期，否则 rollover 会被当成「账户换了周期」而静默迁移 provenance。
        """
        raw = _source("paper_trading.py")
        tree = _tree("paper_trading.py")
        body = _function_source(tree, "_bootstrap_signals_for_today", raw)
        self.assertIn("signal_write_context_or_error", body,
                      "_bootstrap_signals_for_today 不再经 resolver 冻结写入上下文")
        self.assertIn("cycle_id=cycle[\"id\"]", body,
                      "_bootstrap_signals_for_today 没有传它已解析的显式周期")
        self.assertIn("account_cycle_id=account[\"cycle_id\"]", body,
                      "_bootstrap_signals_for_today 没有用候选构建前的捕获归属校验")
        self.assertNotIn("_cycle_signal_provenance(", body,
                         "_bootstrap_signals_for_today 仍直接调用旧的 cycle 解析入口")
        self.assertNotIn("get_version(", body,
                         "_bootstrap_signals_for_today 在写入路径里读了 current head")

    def test_guard14p_research_head_is_pinned_before_the_run_computes(self):
        """Guard 14p：research selection 的 current head pin 必须发生在 ``_run_one`` **之前**。

        动态时序不能只靠字符串顺序，所以这里同时做两件事：

        * 静态：``run_daily`` 里 ``_pin_research_version`` 的调用位置出现在
          ``_run_one`` 之前，且 ``_run_provenance`` **不再**自己读 head；
        * 动态：``test_provenance_inflight_change`` 的 in-flight regression
          在 ``_run_one`` 内部发布新版本 —— 落库的戳必须仍是计算开始时的版本。
        """
        raw = _source("paper_selection.py")
        tree = _tree("paper_selection.py")
        body = _function_source(tree, "run_daily", raw)
        pin_at = body.find("_pin_research_version(")
        run_at = body.find("_run_one(")
        self.assertNotEqual(pin_at, -1, "run_daily 不再 pin immutable version")
        self.assertNotEqual(run_at, -1, "run_daily 不再调用 _run_one")
        self.assertLess(pin_at, run_at,
                        "immutable version 的 pin 发生在 _run_one **之后**"
                        "（计算期间发布的版本会被错误归因给本次结果）")
        # pin 恰好一次：重复 pin 会重新引入「算完再问一次 head」的窗口。
        self.assertEqual(body.count("_pin_research_version("), 1,
                         "run_daily 里 pin 了多次（TOCTOU 窗口重新出现）")
        # provenance 组装侧不得再读 current head。
        assembly = _strip_function_docstring(
            _function_node(tree, "_run_provenance"),
            _function_source(tree, "_run_provenance", raw))
        self.assertNotIn("get_version(", assembly,
                         "_run_provenance 重新读了 current head")
        self.assertIn("pin", assembly,
                      "_run_provenance 没有消费计算前冻结的 pin")
        # 唯一允许读 current head 的入口仍是 resolver 里的 research_version_pin。
        resolver_raw = _source(SELECTION_RESOLVER_MODULE)
        self.assertIn("research_version_pin", resolver_raw)

    def test_guard14q_signal_order_lineage_has_one_owner(self):
        """signal → order 的血缘解析只有一套实现，且不得 current-fill。

        多个 order writer 各自复制「查 signal / 校验账号 / 校验周期 / 校验戳」是
        双重 authority 的温床：只要有一个漏了某步校验，那条路径就能把 legacy
        signal 补成今天的版本。因此要求：

        * 唯一实现是 resolver 的 ``signal_order_provenance``；
        * 它自己不做 current-head / current-cycle / ``stamp_for_account`` 回退；
        * god module 的两处 ``_strategy_stamp`` 都**只**经它继承 signal 血缘，
          不再自己写 SELECT ``paper_signals``。
        """
        raw = _source(SELECTION_RESOLVER_MODULE)
        tree = _tree(SELECTION_RESOLVER_MODULE)
        node = _function_node(tree, "signal_order_provenance")
        body = _strip_function_docstring(
            node, _function_source(tree, "signal_order_provenance", raw))
        self.assertIn("paper_signals", body,
                      "signal_order_provenance 不再按 exact signal 查血缘")
        for forbidden in ("get_version", "stamp_for_account", "active_cycle",
                          "latest_cycle", "current_cycle", "paper_accounts"):
            self.assertNotIn(forbidden, body,
                             f"signal_order_provenance 回退到了 {forbidden}")
        self.assertIn("SignalOrderUnprovable", body,
                      "血缘不完整时必须 fail closed")

    def test_guard14r_signal_risk_log_consumes_the_frozen_stamp(self):
        """Guard 14r：signal 的 risk decision 必须复用**同一个 frozen stamp**。

        ``_risk_log`` 的 ``strategy_stamp=None`` 默认会把 ``_strategy_stamp`` 拉进来，
        于是每个 candidate 各解析一次 Registry。更重要的是 provenance 会分叉：
        ``paper_signals`` 盖的是批次级 frozen 戳，而 ``paper_risk_decisions`` 拿到的是
        逐 candidate 重解析的结果 —— rollover 窗口里两者可能不是同一版策略。

        因此两条 signal 写入路径都必须显式传批次级 frozen stamp。
        """
        raw = _source("paper_trading.py")
        tree = _tree("paper_trading.py")
        expectations = {
            "generate_signals": "strategy_stamp=account_stamp",
            "_bootstrap_signals_for_today": "strategy_stamp=bootstrap_stamp",
        }
        for function, expected in expectations.items():
            body = _function_source(tree, function, raw)
            self.assertIn(expected, body,
                          f"{function} 的 _risk_log 没有复用冻结戳（会逐 candidate 重解析）")
            # R25：signal 行的 provenance 不再解包成局部变量再手抄进 INSERT，而是
            # 整体交给唯一 writer（SIG.commit_signal 只从 frozen context 取四列）。
            # 所以这里断言「该路径确实把 frozen context 交给了 writer」，而不是断言
            # 某个局部变量名出现在 SQL 里 —— 后者会奖励"再抄一份 provenance"的写法。
            self.assertIn("SIG.commit_signal(", body,
                          f"{function} 的 signal 落库没有经唯一 writer")
            self.assertIn("context=account_context" if function == "generate_signals"
                          else "context=bootstrap_context", body,
                          f"{function} 的 signal 落库没有传批次级 frozen context")
            # 禁止把 provenance 四列写进 row：writer 的契约是"provenance 只来自
            # context"，一旦有人把 strategy_version/cycle_id 塞回 row，就等于重新
            # 引入"候选批一套戳、落库另一套戳"的可表达性。只看 commit 的 row 字典，
            # 不看函数里其它合法的 cycle_id 读取（例如重读 paper_accounts）。
            for keys in _commit_signal_row_keys(tree, function):
                for leaked in ("strategy_id", "strategy_version",
                               "strategy_checksum", "cycle_id"):
                    self.assertNotIn(
                        leaked, keys,
                        f"{function} 把 provenance 列 {leaked!r} 塞进了 commit row")
        # 冻结戳必须在 **candidate** 循环之外解析一次（N candidates ≠ N Registry
        # queries）。账户级循环允许每次都解析（每个账户各有自己的周期与 pin），
        # 所以这里只禁「候选内层循环里的解析」。
        for function, frozen in (("generate_signals", "account_context"),
                                 ("_bootstrap_signals_for_today", "bootstrap_context")):
            node = _function_node(tree, function)
            for loop in ast.walk(node):
                if not isinstance(loop, ast.For):
                    continue
                if "candidates" not in ast.dump(loop.iter):
                    continue  # 只看 candidate 维度的那一层
                for call in ast.walk(loop):
                    if (isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Attribute)
                            and call.func.attr == "signal_write_context_or_error"):
                        self.fail(f"{function} 在 candidate 循环内解析写入上下文"
                                  "（应为每账户一次，而不是每个候选一次）")
            self.assertIn(frozen, _function_source(tree, function, raw),
                          f"{function} 不再持有批次级冻结上下文 {frozen}")
        # 解析器本身仍然只存在于 resolver。
        self.assertIn("def signal_write_context", _source(SELECTION_RESOLVER_MODULE))

    def test_guard14s_signal_commit_phase_is_fenced(self):
        """Guard 14s：signal 的最终 commit phase 必须在 ``BEGIN IMMEDIATE`` 内。

        校验（账户仍属 captured cycle）与随后的 signal INSERT 必须处于同一个
        write boundary。deferred 事务下两者之间会被一个 rollover 事务穿透 ——
        ``RV08`` 用真实竞争者证明了这一点（deferred → 竞争者提交成功 → RED）。

        R25 把 INSERT 搬进 ``signal_service.commit_signal`` 之后，"写锁内"不再
        能靠 grep 一段 SQL 字面量来判定。改为断言两件事：

        * 该批次确实经唯一 writer 落库（``SIG.commit_signal(``），且
        * 这一调用位于 ``generate_signals`` 的 ``_db(immediate=True)`` 块内，
          并且该块里没有 provider/network I/O。
        """
        raw = _source("paper_trading.py")
        tree = _tree("paper_trading.py")
        node = _function_node(tree, "generate_signals")
        commit_block = None
        for with_node in ast.walk(node):
            if not isinstance(with_node, ast.With):
                continue
            call = with_node.items[0].context_expr
            if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_db"):
                continue
            block = "\n".join(raw.splitlines()[with_node.lineno - 1:with_node.end_lineno])
            if "SIG.commit_signal(" in block:
                commit_block = (call, block, with_node)
                break
        self.assertIsNotNone(commit_block, "generate_signals 里找不到 signal commit phase")
        call, block, with_node = commit_block
        keywords = {kw.arg: kw.value for kw in call.keywords}
        self.assertIn("immediate", keywords,
                      "signal commit phase 用的是 deferred 事务（rollover 可穿透）")
        self.assertIs(keywords["immediate"].value, True,
                      "signal commit phase 的 immediate 不是字面量 True")
        # provider/network I/O 不得被搬进写锁。
        for io in ("fetch_market_snapshot_full", "fetch_sector_flow",
                   "fetch_hot_sector_snapshot", "_news_for(", "_quotes("):
            self.assertNotIn(io, block, f"provider 调用 {io} 被搬进了 signal 写锁")
        # commit 必须是这个 immediate 块内的**直接**调用：若把 commit 挪出块外
        # （先收集再提交），fencing 就重新变成 deferred 语义。
        self.assertTrue(
            any(isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "commit_signal"
                for child in ast.walk(with_node)),
            "signal commit 不在 BEGIN IMMEDIATE 块内",
        )

    def test_guard14t_signal_cycle_must_match_the_account_binding(self):
        """Guard 14t：新 signal 的 cycle 必须与 account 当前绑定一致（DB 层）。

        这是防 rollover 穿透的**第二层**防线：即使某个调用方漏了应用层校验，
        DB 也必须拒绝一条 cycle 归属已经过期的行。同时必须保持既有语义 ——
        只约束新行、不回填 legacy、archive 继续允许 legacy NULL。
        """
        raw = _source("paper_schema_migrations.py")
        node = _function_node(_tree("paper_schema_migrations.py"),
                              "_ensure_signal_cycle_provenance_guards")
        body = _strip_function_docstring(
            node, _function_source(_tree("paper_schema_migrations.py"),
                                   "_ensure_signal_cycle_provenance_guards", raw))
        self.assertIn("paper_accounts", body,
                      "signal INSERT guard 不再校验 account/cycle 一致性")
        self.assertIn("a.cycle_id = NEW.cycle_id", body,
                      "account/cycle 一致性谓词消失了")
        self.assertIn("paper_cycles", body, "cycle 存在性检查消失了")
        self.assertIn("BEFORE INSERT ON paper_signals", body,
                      "INSERT guard 不再装在 paper_signals 上")
        # 只约束**新写**：不得出现任何回填/UPDATE 历史行的语句。
        for forbidden in ("UPDATE paper_signals", "SET cycle_id"):
            self.assertNotIn(forbidden, body,
                             f"guard 试图改写历史行 provenance：{forbidden}")
        # archive 不装 INSERT guard（legacy NULL 行必须能归档）。
        self.assertNotIn("BEFORE INSERT ON paper_signals_archive", body,
                         "archive 装了 INSERT guard（legacy NULL 行会无法归档）")
        # 两张表的 cycle 不可变 guard 都要在（archive 侧的表名由 f-string 生成）。
        self.assertIn("paper_signals\"", body, "paper_signals 的不可变 guard 消失了")
        self.assertIn("paper_signals_archive\"", body,
                      "paper_signals_archive 的不可变 guard 消失了")
        self.assertIn("BEFORE UPDATE OF cycle_id", body,
                      "cycle 归属的不可变 guard 消失了")
        # 唯一 owner：调用方只调用它，不复制一套。
        for module in ("paper_trading.py", "paper_risk_service.py"):
            module_body = _module_body(module)
            self.assertIn("signal_order_provenance", module_body,
                          f"{module} 没有经 resolver 解析 signal 血缘")
            tree_m = _tree(module)
            for name in ("_strategy_stamp",):
                fn = _function_source(tree_m, name, _source(module))
                self.assertIn("signal_order_provenance", fn,
                              f"{module}.{name} 的 signal 分支不再走唯一 owner")
                self.assertNotIn("paper_signals", fn,
                                 f"{module}.{name} 又自己写了一套 signal 查询")


def _function_node(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"未找到函数 {name}")


def _commit_signal_row_keys(tree, function):
    """Yield the string keys of every ``SIG.commit_signal(... row={...})`` call.

    Yields one ``set`` of keys per call site, so a guard can assert that the
    ``row`` mapping never carries provenance columns — provenance must come from
    the frozen context, never from hand-written row data.
    """
    for node in ast.walk(_function_node(tree, function)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "commit_signal"):
            continue
        for keyword in node.keywords:
            if keyword.arg != "row" or not isinstance(keyword.value, ast.Dict):
                continue
            yield {
                key.value
                for key in keyword.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }


def _strip_function_docstring(node, body):
    """从函数源码里去掉它自己的 docstring（按 AST 行号切）。"""
    statements = getattr(node, "body", None) or []
    if not statements:
        return body
    first = statements[0]
    if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        return body
    lines = body.splitlines()
    offset = first.end_lineno - node.lineno
    return "\n".join(lines[:1] + lines[offset + 1:])


def _module_body(name):
    """模块 docstring 之外的**代码**（docstring 会引用旧 SQL 作为历史说明）。"""
    raw = _source(name)
    tree = ast.parse(raw)
    first = tree.body[0]
    if isinstance(first, ast.Expr):
        return "\n".join(raw.splitlines()[first.end_lineno:])
    return raw


if __name__ == "__main__":
    unittest.main()
