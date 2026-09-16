# -*- coding: utf-8 -*-
"""架构护栏：正式可交易性判断必须经过 TradabilityArchive，不得各自读原始字段。

这条护栏的理由是一个真实缺陷类型：系统"拥有历史行情"，于是各处顺手写

    if row.get("risk_flag") or "ST" in str(row.get("name")): continue
    if not volume: continue

每一次这样的就地判断都绕开了历史事实层——它不知道**当时**是否上市、是否停牌、
观测时点是什么，也拿不出证据来源。结果就是"股票在数据库里存在"被当成"当时能交易"，
历史回测里的样本外结果因此不可复现，而且没人能回答"当时凭什么这么认为"。

护栏刻意写得很窄，避免误伤：

* 只检查**正式判断模块**（执行/学习/策略），不检查数据抓取、展示、回测等消费者；
* 判定基于 AST 结构（字符串子串判断 ST、变量名/字段名线索），不是宽松 regex，
  所以注释、文档字符串、``name`` 变量本身不会被误报；
* 例外只按**函数**放行，并各自写明理由，避免整文件豁免把真问题一起放过。
"""

import ast
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent

# 会产生正式交易/学习判断的模块：它们不得自行读取原始可交易性字段。
FORMAL_JUDGEMENT_MODULES = (
    "paper_trading.py",
    "adaptive_engine.py",
    "strategies.py",
)

# 原始可交易性字段名：直接读它们等于绕过事实层。
RAW_TRADABILITY_KEYS = (
    "listing_date",
    "delisting_date",
    "suspension_reason",
    "is_suspended",
    "is_price_limit_locked",
    "has_market_quote",
    "has_trade_volume",
)

# ST 判定的原始线索字段：读取后做字符串/子串判断即视为绕过。
ST_KEYS = ("risk_flag", "st_flag", "risk_warning")

# 按模块 -> 函数名 放行，每个都必须有理由。
#
# 放行的判据是**语义**：这些函数只消费**当日实时快照**（参数名即证据：
# ``live_map`` / ``live_universe`` / ``quote`` / ``market``），属于"现在这一秒能不能
# 下单"的实时风控口径，不是"历史日期 D 当时是否可交易"的 PIT 判断。它们不声称
# 提供历史证据链，也没有可复现性职责，因此不在本护栏管辖范围内。
#
# 把它们改造成走事实层查询是一次独立的实时链路改造，会改变当日选股/审批行为，
# 而本 PR 的范围明确禁止改动交易逻辑（见 PR body 的 Scope）。
ALLOWED_FUNCTIONS = {
    "paper_trading.py": {
        "_sector_rows",               # 板块聚合展示
        "_live_universe_rows",        # 当日实时全市场过滤
        "_build_selection_universe",  # 当日选股池构建
        "_live_quote_is_st",          # 实时行情 ST 互证
        "_is_st_or_delisting",        # 兼容导出别名（指向既有权威实现）
        "_sector_surge_lane_candidates",  # 当日实时板块车道
        "_ths_hot_lane_candidates",       # 当日实时热点车道
        "_candidate_rows",                # 当日实时快照覆盖候选
        "_signal_approval",               # 当日实时信号审批（quote 参数即证据）
    },
    # adaptive_engine 里的是对**当日**盘面画像的描述性统计，不产出历史可交易结论。
    "adaptive_engine.py": {
        "_market_profile",
        "_portfolio_shadow_arbitration",  # 只读影子裁决，不产出可交易结论
    },
    "strategies.py": set(),
}


def _load_tree(name):
    return ast.parse((BACKEND / name).read_text(encoding="utf-8"))


def _function_spans(tree):
    """Yield ``(name, start, end)`` for every top-level/nested function."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = max(
                (child.lineno for child in ast.walk(node) if hasattr(child, "lineno")),
                default=start,
            )
            yield node.name, start, end


def _string_substring_test_on(node, keys):
    """True when ``node`` compares a string containing ``keys`` against something.

    针对 ``"ST" in str(row.get("name"))`` 这类写法：只看比较表达式里是否出现
    字符串常量 + ``in``/``not in`` 运算，不匹配注释与文档字符串。
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Compare):
            continue
        operators = child.ops
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in operators):
            continue
        left = child.left
        if isinstance(left, ast.Constant) and isinstance(left.value, str):
            return True
        for comparator in child.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                return True
    return False


def _reads_key(node, keys):
    """True when ``node`` reads any of ``keys`` as a subscript or attribute."""
    for child in ast.walk(node):
        if isinstance(child, ast.Subscript):
            for part in ast.walk(child.slice):
                if isinstance(part, ast.Constant) and part.value in keys:
                    return True
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr in ("get", "pop", "setdefault"):
                for arg in child.args[:1]:
                    if isinstance(arg, ast.Constant) and arg.value in keys:
                        return True
    return False


class FormalJudgementMustUseTheArchive(unittest.TestCase):
    def test_no_module_reads_raw_tradability_fields_outside_the_archive(self):
        offenders = []
        for name in FORMAL_JUDGEMENT_MODULES:
            tree = _load_tree(name)
            allowed = ALLOWED_FUNCTIONS.get(name, set())
            for function_name, start, end in _function_spans(tree):
                if function_name in allowed:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if node.name != function_name or node.lineno != start:
                        continue
                    if _reads_key(node, RAW_TRADABILITY_KEYS):
                        offenders.append(f"{name}:{start} {function_name}")
        self.assertEqual(
            [], offenders,
            "正式判断模块直接读取了原始可交易性字段；必须经过 TradabilityArchive API",
        )

    def test_no_module_infers_st_from_a_name_substring(self):
        """禁止 ``name.startswith("ST")`` / ``"ST" in name`` 这类推断。"""
        offenders = []
        for name in FORMAL_JUDGEMENT_MODULES:
            tree = _load_tree(name)
            allowed = ALLOWED_FUNCTIONS.get(name, set())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name in allowed:
                    continue
                if not _string_substring_test_on(node, ST_KEYS):
                    continue
                for child in ast.walk(node):
                    if not isinstance(child, ast.Compare):
                        continue
                    rendered = ast.unparse(child)
                    if "ST" in rendered and (
                        "name" in rendered or "risk_flag" in rendered
                    ):
                        offenders.append(f"{name}:{node.lineno} {node.name}: {rendered}")
                        break
        self.assertEqual(
            [], offenders,
            "ST 必须来自带 effective_at 的历史证据，不得由名称/标记字符串推断",
        )


class GuardIsNotVacuouslyPassing(unittest.TestCase):
    """护栏必须真的能失败；否则它只是装饰。"""

    def test_key_reader_detects_a_raw_read(self):
        tree = ast.parse(
            "def f(row):\n"
            "    return row.get('is_suspended')\n"
        )
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        self.assertTrue(_reads_key(function, RAW_TRADABILITY_KEYS))

    def test_key_reader_ignores_unrelated_reads(self):
        tree = ast.parse(
            "def f(row):\n"
            "    return row.get('close_price') + row['volume']\n"
        )
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        self.assertFalse(_reads_key(function, RAW_TRADABILITY_KEYS))

    def test_st_substring_detector_fires_on_the_antipattern(self):
        tree = ast.parse(
            "def f(name):\n"
            "    return 'ST' in str(name).upper()\n"
        )
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        self.assertTrue(_string_substring_test_on(function, ST_KEYS))

    def test_st_substring_detector_ignores_plain_membership(self):
        tree = ast.parse(
            "def f(allowed, code):\n"
            "    return code in allowed\n"
        )
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        self.assertFalse(_string_substring_test_on(function, ST_KEYS))


class ArchiveIsTheSingleEntryPoint(unittest.TestCase):
    def test_archive_module_exposes_the_documented_api(self):
        import tradability_archive as TA

        for name in ("TradabilityEvidence", "TradabilityDecision",
                     "TradabilityEvaluator", "TradabilityArchiveRepository",
                     "TradabilityProvider", "TradabilityReason", "tradability_at"):
            self.assertTrue(hasattr(TA, name), name)

    def test_archive_does_not_import_strategy_or_execution_modules(self):
        """事实层必须自足：不得反向依赖交易/策略/学习链路。"""
        tree = ast.parse(Path(BACKEND / "tradability_archive.py").read_text(encoding="utf-8"))
        forbidden = {"paper_trading", "strategies", "adaptive_engine",
                     "execution_verification", "learning_dataset", "main"}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        self.assertTrue(forbidden.isdisjoint(imported), sorted(forbidden & imported))

    def test_archive_is_not_embedded_in_existing_hot_modules(self):
        """事实层必须是独立模块，而不是塞进既有大文件。"""
        backend = Path(BACKEND)
        self.assertTrue((backend / "tradability_archive.py").is_file())
        for name in ("paper_trading.py", "data_fetcher.py", "learning_dataset.py"):
            source = (backend / name).read_text(encoding="utf-8")
            self.assertNotIn("class TradabilityEvidence", source, name)
            self.assertNotIn("class TradabilityArchiveRepository", source, name)


if __name__ == "__main__":
    unittest.main()
