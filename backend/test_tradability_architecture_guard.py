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

# ST 名称推断的**已知例外**：这些函数确实按子串扫名称，但同属"当日实时"链路。
#
# 记录它们是为了让护栏保持**精确**：一个会连既有实时链路一起报红的护栏不会被人
# 认真对待，最后会被整条关掉。例外必须逐条登记并写明理由，新增例外只能随实时链路
# 的独立改造一起做（届时这处 `.str.contains("ST")` 应改为读取事实层结论）。
#
# 本 PR 的范围禁止改动交易/选股逻辑，因此这里只登记、不修（见 PR body 的 Scope）。
KNOWN_ST_NAME_INFERENCE_EXCEPTIONS = {
    # 当日选股池的身份筛：用当前名称排除 ST / 退市股。
    ("strategies.py", "_permitted_a_share_mask"),
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
    """True when ``node`` compares or scans a string for ``keys``.

    要抓两类等价写法，缺一不可：

    * 比较式：``"ST" in str(row.get("name"))`` / ``name in ("ST", ...)``；
    * 调用式：``name.startswith("ST")`` / ``name.upper().str.contains("ST")`` /
      ``"ST" in name`` 之外的 pandas 风格 ``.str.contains`` / ``.str.match``。

    只查 ``ast.Compare`` 会漏掉调用式——而 pandas 的 ``.str.contains("ST")``
    在正式判断模块里真实存在，属于本护栏要禁止的"用当前名称推断 ST"。
    只看字符串常量，不匹配注释与文档字符串。
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Compare):
            if not any(isinstance(op, (ast.In, ast.NotIn)) for op in child.ops):
                continue
            operands = [child.left, *child.comparators]
            if any(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in operands
            ):
                return True
            continue
        if isinstance(child, ast.Call):
            if _string_scan_call_on(child, keys):
                return True
    return False


# 会按子串扫描字符串的方法名（str 内建 + pandas Series.str 命名空间）。
_STRING_SCAN_CALLS = {
    "startswith", "endswith", "find", "rfind", "index", "rindex",
    "contains", "match", "fullmatch", "extract", "count", "replace",
}


def _string_scan_call_on(call, keys):
    """True when ``call`` scans a string literal for one of ``keys``.

    命中条件：被调用名是字符串扫描方法，且实参里出现命中的字符串常量。
    这样 ``name.startswith("ST")``、``names.str.contains("ST")``、
    ``names.str.upper().str.contains("ST")`` 都会被抓到。
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in _STRING_SCAN_CALLS:
        return False
    for arg in [*call.args, *[kw.value for kw in call.keywords]]:
        for part in ast.walk(arg):
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                if any(key in part.value for key in keys) or "ST" in part.value:
                    return True
    return False


def _st_name_inference_in(node):
    """返回该函数里**第一处**"用名称推断 ST"的表达式；没有则 ``None``。

    判据：表达式里同时出现 ST 字面量与名称/标记来源。覆盖两类写法：

    * 比较式 —— ``"ST" in str(row.get("name"))``；
    * 调用式 —— ``names.str.upper().str.contains("ST", regex=False)``、
      ``name.startswith("ST")``。

    只查 ``ast.Compare`` 会漏掉调用式，而 pandas 的 ``.str.contains("ST")``
    在正式判断模块里真实存在。
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Compare):
            if not any(isinstance(op, (ast.In, ast.NotIn)) for op in child.ops):
                continue
        elif isinstance(child, ast.Call):
            if not _string_scan_call_on(child, ST_KEYS):
                continue
        else:
            continue
        rendered = ast.unparse(child)
        if "ST" not in rendered.upper():
            continue
        if any(token in rendered for token in ("name", "risk_flag", "risk_warning")):
            return rendered
    return None


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
        """禁止 ``name.startswith("ST")`` / ``.str.contains("ST")`` / ``"ST" in name``。

        只查 ``ast.Compare`` 会漏掉调用式，而 pandas 的 ``.str.contains("ST")``
        正是要禁止的"用当前名称推断 ST"。
        """
        offenders = []
        for name in FORMAL_JUDGEMENT_MODULES:
            tree = _load_tree(name)
            allowed = ALLOWED_FUNCTIONS.get(name, set())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name in allowed:
                    continue
                if (name, node.name) in KNOWN_ST_NAME_INFERENCE_EXCEPTIONS:
                    continue
                expression = _st_name_inference_in(node)
                if expression is not None:
                    offenders.append(f"{name}:{node.lineno} {node.name}: {expression}")
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

    def test_st_detector_fires_on_call_based_checks(self):
        """调用式与比较式必须同样被抓到 —— 这是 review 指出的漏检。"""
        for source in (
            "def f(name):\n"
            "    return name.startswith('ST')\n",
            "def f(names):\n"
            "    return names.str.contains('ST', regex=False)\n",
            "def f(names):\n"
            "    return names.str.upper().str.contains('ST', regex=False)\n",
            "def f(name):\n"
            "    return name.endswith('ST')\n",
        ):
            tree = ast.parse(source)
            function = next(
                node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
            )
            self.assertIsNotNone(
                _st_name_inference_in(function), source
            )

    def test_st_detector_still_ignores_unrelated_calls(self):
        """不得把无关的字符串调用误报成 ST 推断。"""
        tree = ast.parse(
            "def f(names):\n"
            "    return names.str.startswith('600')\n"
        )
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        self.assertIsNone(_st_name_inference_in(function))

    def test_registered_exceptions_are_real(self):
        """登记的例外必须指向真实存在的函数，否则例外会变成永久豁免。"""
        for module_name, function_name in KNOWN_ST_NAME_INFERENCE_EXCEPTIONS:
            tree = _load_tree(module_name)
            names = {
                node.name for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            self.assertIn(function_name, names, f"{module_name}:{function_name}")

    def test_registered_exception_is_the_only_remaining_inference(self):
        """例外清单必须与真实命中集合相等：既不多登记，也不遗漏。

        统计口径与主护栏一致：已被 ``ALLOWED_FUNCTIONS`` 按"当日实时链路"整体
        豁免的函数不计入，它们另有理由。这条是例外机制的自我约束——如果哪天有人
        在别处新增了名称推断 ST，本断言会失败，而不是被例外清单悄悄吸收。
        """
        found = set()
        for module_name in FORMAL_JUDGEMENT_MODULES:
            tree = _load_tree(module_name)
            allowed = ALLOWED_FUNCTIONS.get(module_name, set())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name in allowed:
                    continue
                if _st_name_inference_in(node) is not None:
                    found.add((module_name, node.name))
        self.assertEqual(KNOWN_ST_NAME_INFERENCE_EXCEPTIONS, found)

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


# ───────────────────────── ingestion 边界护栏 ─────────────────────────

#: 摄取层的事实来源 Provider 类必须**永不**写 archive。检查其方法体里不得出现
#: archive 表名字面量，也不得调用任何 ``.save(...)``（写 authority 只在
#: IngestionService 编排层）。
INGESTION_MODULE = "tradability_ingestion.py"


def _provider_classes(tree):
    """摄取模块里，基类名含 ``Provider`` 或以 ``Provider`` 结尾的类（事实源 adapter）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [ast.unparse(base) for base in node.bases]
        if any("Provider" in base for base in bases) or node.name.endswith("Provider"):
            yield node


def _writes_archive(node) -> list:
    """返回 Provider 类方法体里"写 archive"的违规表达式列表。

    判据两条：出现 ``historical_tradability_archive`` 表名字面量，或调用 ``.save(...)``。
    """
    offenders = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute) and func.attr == "save":
                offenders.append(ast.unparse(child))
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if "historical_tradability_archive" in child.value:
                offenders.append(repr(child.value))
    return offenders


class ProvidersNeverWriteTheArchive(unittest.TestCase):
    def test_provider_classes_do_not_write_archive(self):
        tree = _load_tree(INGESTION_MODULE)
        offenders = []
        for cls in _provider_classes(tree):
            hits = _writes_archive(cls)
            if hits:
                offenders.append(f"{cls.name}: {hits}")
        self.assertEqual([], offenders, "Provider 不得写 archive，写 authority 只在 ingestion 编排层")

    def test_guard_detects_a_provider_writing_archive(self):
        """护栏必须真的能失败：合成一个写 archive 的 Provider，应被抓到。"""
        source = (
            "class FooProvider:\n"
            "    def fetch(self):\n"
            "        x = 'historical_tradability_archive'\n"
            "        return x\n"
        )
        tree = ast.parse(source)
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        self.assertTrue(_writes_archive(cls))

    def test_guard_ignores_ingestion_service_write(self):
        """编排层 IngestionService 的 save 不在 Provider 边界内，不应误报。"""
        tree = _load_tree(INGESTION_MODULE)
        provider_hits = {
            cls.name: _writes_archive(cls) for cls in _provider_classes(tree)
        }
        self.assertTrue(all(not hits for hits in provider_hits.values()), provider_hits)


#: 执行/学习链路不得 import/consume ingestion 结果作为正式交易 gate。
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


class ExecutionAndLearningDoNotConsumeIngestion(unittest.TestCase):
    def test_no_execution_or_learning_module_imports_ingestion(self):
        offenders = []
        for name in EXECUTION_LEARNING_MODULES:
            tree = _load_tree(name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "tradability_ingestion" in alias.name:
                            offenders.append(f"{name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if "tradability_ingestion" in node.module:
                        offenders.append(f"{name}: from {node.module}")
        self.assertEqual(
            [], offenders,
            "执行/学习链路不得 import tradability_ingestion 作为正式交易 gate",
        )


if __name__ == "__main__":
    unittest.main()
