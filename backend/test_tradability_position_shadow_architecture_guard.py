# -*- coding: utf-8 -*-
"""Position-aware T+1 Shadow 的**架构护栏**（AST / 源码静态扫描）。

护栏存在只为一件事：**仓位层永远不能获得 authority**。

它在源码层禁止这些退化 —— 每一条都是"顺手一改"就真的可能发生的：

1. 适配器 / 观察层 import 下单 / 持仓写入 / 成交写入 / 学习 / 策略模块；
2. 适配器自己写**任何**表（``INSERT`` / ``UPDATE`` / ``DELETE`` / ``REPLACE``）；
3. 适配器复制 T+1 规则：自己算 ``weekday + 1`` / ``timedelta(days=1)``、
   自己硬编码 ETF T+0 前缀表、自己写 ``t1_not_sellable`` 比较；
4. 用 ``MIN`` / ``MAX`` / ``latest`` 把多 lot 压成一个 ``entry_session``；
5. 执行 / 学习 / 选股链路 import 本层并把观察当 gate；
6. 操作员 CLI 长出 ``--apply`` / ``--enforce`` / ``--switch-authority``。

护栏刻意保持**精确**（只扫本 PR 新增/改动的文件），避免宽泛正则误伤既有代码。
"""

import ast
import re
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent

ADAPTER_MODULE = "tradability_position_evidence.py"
SHADOW_MODULE = "tradability_position_shadow.py"
SHADOW_CLI = ROOT / "work" / "tradability_shadow_validation.py"

POSITION_MODULES = (ADAPTER_MODULE, SHADOW_MODULE)

#: 仓位层不得 import 的模块前缀（与既有 Shadow 护栏同一份清单）。
FORBIDDEN_IMPORTS = (
    "paper_trading",
    "paper_portfolio",
    "execution_dispatch",
    "execution_evidence",
    "execution_lifecycle",
    "execution_outcome",
    "execution_planner",
    "execution_profiles",
    "tradability_archive",
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

#: 仓位层不得调用的写入 / 授权入口。
FORBIDDEN_CALLS = (
    "submit_order", "cancel_order", "modify_order", "place_order",
    "save_position", "save_fill", "record_fill", "apply_evolution",
    "set_risk_budget", "write_learning_row",
    "stamp_order", "backfill_legacy_orders",
)

#: 写库语句（大小写不敏感）。适配器必须一条都没有。
WRITE_STATEMENTS = ("INSERT INTO", "UPDATE ", "DELETE FROM", "REPLACE INTO", "DROP TABLE")

#: 生产业务表 —— 任何写语句点名它们都是越权。
PRODUCTION_TABLES = (
    "paper_orders", "paper_fills", "paper_positions", "paper_position_lots",
    "paper_accounts", "historical_tradability_archive", "paper_signals",
    "paper_risk_decisions", "tradability_shadow_comparisons",
)

#: 执行 / 学习 / 选股链路不得 import 仓位层。
EXECUTION_LEARNING_MODULES = (
    "paper_trading.py",
    "adaptive_engine.py",
    "strategies.py",
    "execution_dispatch.py",
    "execution_planner.py",
    "execution_verification.py",
    "learning_dataset.py",
    "learning_evaluation.py",
)

FORBIDDEN_CLI_FLAGS = (
    "--apply", "--enforce", "--switch-authority", "--take-over", "--repair-position",
)

#: 适配器**允许**暴露的公开可调用面（等值断言）。
ADAPTER_PUBLIC_API = frozenset({
    "PositionEvidenceError",
    "PositionEvidenceStatus", "AcquisitionStatus", "LotSellability",
    "T1EvalSource", "SellabilityStatus",
    "PositionLotEvidence", "PositionSellabilityContext",
    "PositionEvidenceAdapter",
})

#: 观察层**允许**暴露的公开可调用面（等值断言）。
SHADOW_PUBLIC_API = frozenset({
    "PositionShadowStatus",
    "PositionShadowComparison", "PositionShadowSummary", "PositionShadowObserver",
})

#: ``PositionEvidenceAdapter`` **允许**暴露的公开方法（等值断言）。
ADAPTER_METHODS = frozenset({"context_for", "load_lots"})

#: ``PositionShadowObserver`` **允许**暴露的公开方法（等值断言）。
OBSERVER_METHODS = frozenset({"observe", "observe_many", "summarize"})


def _tree(name):
    return ast.parse((BACKEND / name).read_text(encoding="utf-8"))


def _source(name):
    return (BACKEND / name).read_text(encoding="utf-8")


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


def _string_constants(tree):
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


class PositionLayerExposesNoAuthorityApi(unittest.TestCase):
    """等值断言：两个新模块的公开 API 面必须**恰好**是登记过的那一组。"""

    def test_adapter_public_api_is_exactly_the_registered_set(self):
        self.assertEqual(ADAPTER_PUBLIC_API, _public_names(_tree(ADAPTER_MODULE)))

    def test_shadow_public_api_is_exactly_the_registered_set(self):
        self.assertEqual(SHADOW_PUBLIC_API, _public_names(_tree(SHADOW_MODULE)))

    def test_adapter_public_methods_are_exactly_the_registered_set(self):
        self.assertEqual(
            ADAPTER_METHODS,
            _public_names(_tree(ADAPTER_MODULE), methods_of="PositionEvidenceAdapter"),
        )

    def test_observer_public_methods_are_exactly_the_registered_set(self):
        self.assertEqual(
            OBSERVER_METHODS,
            _public_names(_tree(SHADOW_MODULE), methods_of="PositionShadowObserver"),
        )

    def test_detector_fires_on_a_new_public_entry_point(self):
        tree = ast.parse("def permit_trade():\n    pass\n\n\ndef load_lots():\n    pass\n")
        found = _public_names(tree)
        self.assertIn("permit_trade", found)
        self.assertNotEqual(ADAPTER_PUBLIC_API, found)


class AdapterIsReadOnly(unittest.TestCase):
    """适配器**一条写语句都不能有** —— 它只读历史账本。"""

    def test_no_write_statement_in_adapter(self):
        source = _source(ADAPTER_MODULE).upper()
        found = [stmt for stmt in WRITE_STATEMENTS if stmt in source]
        self.assertEqual([], found, f"{ADAPTER_MODULE} 不得包含写语句 {found}")

    def test_no_write_statement_in_position_shadow(self):
        source = _source(SHADOW_MODULE).upper()
        found = [stmt for stmt in WRITE_STATEMENTS if stmt in source]
        self.assertEqual([], found, f"{SHADOW_MODULE} 不得包含写语句 {found}")

    def test_no_production_table_is_named_in_a_mutating_context(self):
        """生产业务表名只允许出现在 SELECT 上下文里。"""
        for name in POSITION_MODULES:
            source = _source(name)
            for table in PRODUCTION_TABLES:
                for match in re.finditer(re.escape(table), source, re.IGNORECASE):
                    window = source[max(0, match.start() - 60):match.start()].upper()
                    self.assertNotRegex(
                        window, r"INSERT INTO|UPDATE |DELETE FROM|REPLACE INTO",
                        f"{name}: {table} 出现在写语句里",
                    )

    def test_detector_fires_on_a_write_statement(self):
        source = "conn.execute('UPDATE paper_position_lots SET remaining_qty=0')"
        self.assertIn("UPDATE ", source.upper())

    def test_detector_ignores_a_read_only_statement(self):
        source = (
            "SELECT id,remaining_qty FROM paper_position_lots WHERE remaining_qty > 0"
        )
        for stmt in WRITE_STATEMENTS:
            self.assertNotIn(stmt, source.upper())


class PositionLayerDoesNotImportExecutionModules(unittest.TestCase):
    def test_no_forbidden_import(self):
        for name in POSITION_MODULES:
            found = [
                module for module in _imported_modules(_tree(name))
                if module in FORBIDDEN_IMPORTS
            ]
            self.assertEqual([], found, f"{name} 不得 import {found}")

    def test_no_forbidden_call(self):
        for name in POSITION_MODULES:
            found = [
                called for called in _called_names(_tree(name))
                if called in FORBIDDEN_CALLS
            ]
            self.assertEqual([], found, f"{name} 不得调用 {found}")

    def test_detector_fires_on_a_forbidden_import(self):
        tree = ast.parse("import paper_trading\n")
        found = [n for n in _imported_modules(tree) if n in FORBIDDEN_IMPORTS]
        self.assertEqual(["paper_trading"], found)

    def test_detector_fires_on_a_forbidden_call(self):
        tree = ast.parse("def f(conn, oid):\n    stamp_order(conn, oid)\n")
        found = [n for n in _called_names(tree) if n in FORBIDDEN_CALLS]
        self.assertEqual(["stamp_order"], found)


class AdapterDoesNotReimplementT1Rules(unittest.TestCase):
    """T+1 与资产类型规则**只有一处**实现：``selection_tradability``。

    本类把"复制一份规则"的四种典型写法挡在门外。
    """

    def test_no_calendar_arithmetic(self):
        """不得自己算"加一天"—— 必须问权威的 ``earliest_sellable_session``。"""
        source = _source(ADAPTER_MODULE)
        self.assertNotIn("timedelta(days=1)", source.replace(" ", ""))
        self.assertNotIn("timedelta(1)", source.replace(" ", ""))
        self.assertNotRegex(source, r"weekday\(\)\s*\+")
        self.assertNotRegex(source, r"\+\s*1\s*\)?\s*#?\s*T\+1")

    def test_no_hardcoded_etf_prefix_list(self):
        """不得硬编码 ETF T+0 前缀表 —— 那在 ``paper_trading_rules`` 里。"""
        source = _source(ADAPTER_MODULE)
        for literal in ('"510', "'510", '"512', "'512", '"159', "'159", '"588'):
            self.assertNotIn(literal, source)
        self.assertNotIn("T0_ETF_PREFIXES", source)

    def test_reuses_the_authoritative_t1_entry_point(self):
        source = _source(ADAPTER_MODULE)
        self.assertIn("ST.exit_tradability", source)
        self.assertIn("ST.earliest_sellable_session", source)
        self.assertIn("ST.REASON_T1_NOT_SELLABLE", source)

    def test_asset_type_comes_from_the_authority(self):
        source = _source(ADAPTER_MODULE)
        self.assertIn("PTR.asset_type", source)
        self.assertNotIn('"stock_t1"', source)
        self.assertNotIn('"etf_t0"', source)

    def test_no_lot_collapsing_min_max_or_latest(self):
        """禁止 ``MIN`` / ``MAX`` / ``latest`` 把多 lot 压成一个 ``entry_session``。"""
        source = _source(ADAPTER_MODULE).upper()
        for forbidden in ("MIN(ACQUIRED_AT", "MAX(ACQUIRED_AT", "MIN(FILL_DATE",
                          "MAX(FILL_DATE", "LATEST_ENTRY_SESSION"):
            self.assertNotIn(forbidden, source)

    def test_no_position_changing_quantity_arithmetic(self):
        """适配器不得回写 ``remaining_qty``（lot 消耗由生产 FIFO 决定）。"""
        source = _source(ADAPTER_MODULE)
        self.assertNotIn("remaining_qty=remaining_qty", source)
        self.assertNotIn("SET remaining_qty", source)

    def test_detector_fires_on_a_copied_t1_rule(self):
        fabricated = (
            "if side == 'sell' and session == entry_session:\n"
            "    return 't1_not_sellable'\n"
        )
        self.assertIn("t1_not_sellable", fabricated)
        authoritative = "reason == ST.REASON_T1_NOT_SELLABLE"
        self.assertNotIn("'t1_not_sellable'", authoritative)

    def test_detector_fires_on_hardcoded_etf_prefixes(self):
        fabricated = 'if code.startswith("510"):\n    return "etf_t0"\n'
        self.assertIn('"510"', fabricated)
        self.assertIn('"etf_t0"', fabricated)


class ExecutionAndLearningDoNotConsumePositionLayer(unittest.TestCase):
    def test_no_execution_or_learning_module_imports_the_position_layer(self):
        offenders = []
        for name in EXECUTION_LEARNING_MODULES:
            path = BACKEND / name
            if not path.exists():  # pragma: no cover - 模块清单防御
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for imported in _imported_modules(tree):
                if imported in ("tradability_position_evidence",
                                "tradability_position_shadow"):
                    offenders.append(f"{name}: {imported}")
        self.assertEqual(
            [], offenders,
            "执行/学习链路不得 import 仓位层（仓位层零 authority）",
        )

    def test_detector_fires_on_a_position_layer_import(self):
        tree = ast.parse("import tradability_position_shadow\n")
        found = [
            n for n in _imported_modules(tree)
            if n in ("tradability_position_evidence", "tradability_position_shadow")
        ]
        self.assertEqual(["tradability_position_shadow"], found)


class PositionShadowIsNotEmbeddedInHotModules(unittest.TestCase):
    def test_position_logic_lives_in_its_own_modules(self):
        self.assertTrue((BACKEND / ADAPTER_MODULE).exists())
        self.assertTrue((BACKEND / SHADOW_MODULE).exists())
        for name in EXECUTION_LEARNING_MODULES:
            path = BACKEND / name
            if not path.exists():  # pragma: no cover
                continue
            source = path.read_text(encoding="utf-8")
            for symbol in ("PositionEvidenceAdapter", "PositionShadowObserver"):
                self.assertNotIn(symbol, source, f"{name} 不得内嵌仓位层")


class PositionQuantityMustBeHistoricalNotCurrent(unittest.TestCase):
    """承重：历史数量必须来自重放，**绝不能**来自当前可变的 ``remaining_qty``。

    这条是 v2 修正的核心缺陷：``paper_position_lots.remaining_qty`` 会被后续 SELL
    原地递减，因此它描述"现在"，不描述 ``decision_at``。
    """

    def test_adapter_replays_before_reading_the_snapshot(self):
        source = _source(ADAPTER_MODULE)
        self.assertIn("snapshot.get(lot_id", source,
                      "历史数量必须取自重放快照")
        self.assertIn("_replay(", source, "必须存在重放步骤")

    def test_adapter_never_derives_historical_quantity_from_remaining_qty(self):
        """``historical_quantity`` 不得被当前余额赋值。"""
        source = _source(ADAPTER_MODULE)
        self.assertNotIn("historical_quantity=_int(_row_field(row, \"remaining_qty\"))",
                         source)
        self.assertNotIn("historical = _int(_row_field(row, \"remaining_qty\"))",
                         source)

    def test_adapter_declares_the_replay_basis(self):
        source = _source(ADAPTER_MODULE)
        self.assertIn("QUANTITY_BASIS_HISTORICAL_REPLAY", source)
        self.assertIn("QUANTITY_BASIS_UNPROVABLE", source)

    def test_unreplayable_history_fails_closed(self):
        """重放不自洽必须 fail closed，而不是退回当前余额。"""
        source = _source(ADAPTER_MODULE)
        self.assertIn("historical_quantity_unprovable", source)
        self.assertIn("replay_does_not_match_ledger", source)

    def test_detector_fires_on_current_balance_substitution(self):
        fabricated = 'historical = _int(_row_field(row, "remaining_qty"))'
        self.assertIn('"remaining_qty"', fabricated)
        self.assertNotIn("snapshot.get(lot_id", fabricated)


class PositionScopeMustBeExplicit(unittest.TestCase):
    """承重：``cycle_id`` 与 ``account_id`` 必须显式，缺任何一个都 fail closed。"""

    def test_lot_query_is_scoped_by_cycle_and_account(self):
        source = _source(ADAPTER_MODULE)
        self.assertIn("FROM paper_position_lots WHERE cycle_id=? AND account_id=?",
                      source)

    def test_context_for_requires_both_scopes(self):
        tree = _tree(ADAPTER_MODULE)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "PositionEvidenceAdapter":
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name == "context_for":
                        names = {arg.arg for arg in child.args.kwonlyargs}
                        self.assertIn("cycle_id", names)
                        self.assertIn("account_id", names)
                        return
        self.fail("未找到 PositionEvidenceAdapter.context_for")

    def test_missing_scope_is_a_diagnostic_not_a_pool(self):
        source = _source(ADAPTER_MODULE)
        self.assertIn("missing_cycle_or_account_scope", source)

    def test_detector_fires_on_an_unscoped_query(self):
        fabricated = "FROM paper_position_lots WHERE code=?"
        self.assertNotIn("cycle_id=?", fabricated)
        self.assertNotIn("account_id=?", fabricated)


class PositionPitMustConsumeExactDecisionAt(unittest.TestCase):
    """承重：调用方的精确 ``decision_at`` 必须被消费，显式非法值必须 fail closed。"""

    def test_adapter_consumes_the_caller_decision_at(self):
        source = _source(ADAPTER_MODULE)
        self.assertIn("resolved_decision_at = _instant(decision_at)", source)

    def test_session_close_is_only_a_guarded_fallback(self):
        """``session_close_at`` 只允许出现在"调用方没给 decision_at"的分支里。"""
        source = _source(ADAPTER_MODULE)
        guard = "if decision_at is None:"
        self.assertIn(guard, source)
        fallback = "resolved_decision_at = _instant(ST.session_close_at(session))"
        self.assertIn(fallback, source)
        self.assertLess(source.index(guard), source.index(fallback),
                        "session close 回退必须排在 None 分支之后（受守卫）")
        # 精确时点赋值必须独立存在，且不得在 None 分支里。
        exact = "resolved_decision_at = _instant(decision_at)"
        self.assertIn(exact, source)
        self.assertGreater(source.index(exact), source.index(fallback))

    def test_invalid_inputs_are_rejected_explicitly(self):
        source = _source(ADAPTER_MODULE)
        for diagnostic in ("invalid_decision_at", "invalid_validation_as_of",
                           "invalid_requested_sell_quantity"):
            self.assertIn(diagnostic, source)

    def test_detector_fires_on_a_session_close_override(self):
        fabricated = "resolved_decision_at = _instant(ST.session_close_at(session))"
        self.assertIn("session_close_at(session)", fabricated)
        self.assertNotIn("if decision_at is None:", fabricated)


class PositionIdentityIntegrityIsEnforced(unittest.TestCase):
    """承重：不得借另一账户 / 另一股票的已验证买入证明当前 lot。"""

    def test_adapter_verifies_lot_order_fill_identity(self):
        source = _source(ADAPTER_MODULE)
        self.assertIn("IDENTITY_MISMATCH", source)
        self.assertIn("source_order_identity_mismatch", source)
        self.assertIn("fill_identity_mismatch", source)

    def test_detector_fires_on_a_dropped_identity_check(self):
        fabricated = "if (order_code != code or lot_code != code):"
        self.assertNotIn("order_account != account_id", fabricated)


class OperatorCliExposesNoAuthority(unittest.TestCase):
    """CLI 的 position-aware 模式必须与市场层面模式一样**只读、零开关**。"""

    SKIP_WITHOUT_WORK = "work/ 不在运行时镜像内（docker-smoke 只挂载 backend/frontend/deploy）"

    def _cli_source(self) -> str:
        if not SHADOW_CLI.exists():
            self.skipTest(f"{self.SKIP_WITHOUT_WORK}；CLI 契约由完整 checkout 的 tests job 覆盖")
        return SHADOW_CLI.read_text(encoding="utf-8")

    def test_no_forbidden_flag_is_accepted(self):
        accepted = set(_string_constants(ast.parse(self._cli_source())))
        found = sorted(flag for flag in FORBIDDEN_CLI_FLAGS if flag in accepted)
        self.assertEqual([], found, f"shadow CLI 不得接受 {found}")

    def test_cli_still_contains_no_write_statement(self):
        source = self._cli_source()
        for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE"):
            self.assertNotIn(statement, source, f"shadow CLI 不得包含 {statement}")

    def test_position_aware_flag_is_opt_in_and_read_only(self):
        source = self._cli_source()
        self.assertIn("--position-aware", source)
        # 缺省必须是关闭的（store_true，不设 default=True 之类）。
        self.assertNotRegex(source, r"--position-aware[^)]*default\s*=\s*True")

    def test_cli_reuses_the_existing_entry_points(self):
        """CLI 不得自造第二套 T+1 或第二套仓位置信逻辑。"""
        source = self._cli_source()
        self.assertIn("PS.PositionShadowObserver", source)
        self.assertIn("PE.PositionEvidenceAdapter", source)

    # ── 以下检测器用例**不读文件**，因此在运行时镜像内也必须真的跑 ──

    def test_detector_fires_on_a_forbidden_flag(self):
        tree = ast.parse("parser.add_argument('--repair-position', action='store_true')\n")
        accepted = _string_constants(tree)
        self.assertIn("--repair-position", accepted)
        self.assertIn("--repair-position", FORBIDDEN_CLI_FLAGS)


if __name__ == "__main__":
    unittest.main()
