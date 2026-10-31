"""Deterministic, read-only coupling metrics for the backend module graph.

The analyzer owns source discovery and a single syntax-tree pass per module.  It
never imports production modules, so measuring architecture cannot initialize
runtime state.  Budgets belong in ``test_architecture_boundary_audit``.
"""
from __future__ import annotations

import ast
from collections import Counter
from functools import lru_cache
from pathlib import Path
import re
from typing import Iterable

BACKEND = Path(__file__).resolve().parents[1]
PAPER_TABLE = re.compile(r"\bpaper_(?:orders|accounts|signals|cycles|fills|nav|positions|position_lots|risk_decisions|audit)\b")
DOMAIN_GROUPS = {
    "paper", "adaptive", "ai", "strategy", "experiment", "agent", "research",
    "execution", "learning", "evolution", "replay", "selection", "tradability",
    "market", "data", "news", "risk", "portfolio", "trade", "user", "api",
    "dashboard", "signal", "factor", "report", "backtest", "intraday", "main",
}
SQL_WRITE = re.compile(r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
NUMERIC_NAMES = {"_num", "_number", "_finite", "_finite_number"}
DOMAIN_PREFIXES = (
    "paper", "adaptive", "ai", "strategy", "experiment", "agent", "research",
    "execution", "learning", "evolution", "replay", "selection", "tradability",
)


def production_paths(root: Path = BACKEND) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.glob("*.py")
                        if not path.name.startswith("test_") and path.name != "__init__.py"))


def _trees(paths: Iterable[Path]):
    """逐个产出 (路径, 源码, 语法树)。

    优先走测试侧的共享 AST 缓存：架构守卫与耦合度量会把同一批生产文件解析
    很多遍（``paper_trading.py`` 单次约 0.5 秒），缓存按 (路径, mtime, 大小)
    失效所以不会读到过期语法树。``tools/`` 需要能脱离测试环境单独运行，
    因此这里是"可用则用、不可用则退回直接解析"，不把工具硬绑到测试包上。

    2026-10-29：同一批路径的解析结果在本进程内只做一次。守卫里
    ``test_foundation_allowlist_has_no_unused_entries`` 自己也要全仓 316 个
    文件的语法树（实测 33.5 秒），与 ``measure_all`` 完全重叠，因此共享同一份
    ``(路径, 源码, 树)`` 元组。路径集合是缓存键，临时目录夹具天然互不干扰。
    """
    return iter(_records(tuple(paths)))


@lru_cache(maxsize=8)
def _records(paths: tuple[Path, ...]):
    try:
        from test_support import ast_cache
    except ImportError:
        ast_cache = None
    records = []
    for path in paths:
        if ast_cache is not None:
            records.append((path, ast_cache.source(path), ast_cache.tree(path)))
            continue
        source = path.read_text(encoding="utf-8")
        records.append((path, source, ast.parse(source, filename=str(path))))
    return tuple(records)


def _local_imports(tree, modules):
    edges = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            edges.update(alias.name.split(".")[0] for alias in node.names
                         if alias.name.split(".")[0] in modules)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            if root in modules:
                edges.add(root)
    return edges


def strongly_connected(edges):
    """Tarjan SCC; every component larger than one is exactly one import cycle."""
    index = 0
    indices, lowlinks, stack, on_stack, result = {}, {}, [], set(), []
    def visit(node):
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in edges.get(node, ()):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] == indices[node]:
            component = []
            while True:
                item = stack.pop()
                on_stack.remove(item)
                component.append(item)
                if item == node:
                    break
            if len(component) > 1:
                result.append(tuple(sorted(component)))
    for node in sorted(edges):
        if node not in indices:
            visit(node)
    return sorted(result)


#: 事实/基础 owner：只提供账本读法、规则与纯工具，不含任何业务域决策。
#: 业务域模块直接 import 它们属于"显式依赖 owner"，不是跨业务域耦合；
#: 旧口径按前缀把这些模块算进 "paper" 域，导致 A1 迁移反而显示边数上涨。
FOUNDATION_MODULES = frozenset({
    "data_paths", "env_utils", "entry_lifecycle", "paper_common",
    "paper_decision_audit", "paper_numeric", "paper_position_read_model",
    "paper_repository", "paper_storage", "paper_trading_rules",
})


#: 基础层允许依赖的**业务域**模块白名单。
#:
#: 基础层的定义是"只提供账本读法、规则与纯工具"，所以它**不能**反过来
#: 依赖任何业务域模块。单向的 foundation → 业务域 公开导入不会产生导入环，
#: 也不会产生私有跨模块访问，因此 :func:`measure_all` 的两个指标都发现不了
#: 它 —— 这正是审计第 4 项指出的口径缺口，必须由独立守卫覆盖。
#:
#: 基础层**之间**的互相依赖不算违规（它们同属这一层）；这里只登记需要
#: 越过层边界的例外，每一条都是一个**已确认的装配边界**：
#: - ``execution_verification``：唯一一份"成交已验证"谓词。基础层用它来
#:   fail-closed 排除未验证行；它本身是账本事实的读取方，不含业务决策。
#: - ``paper_portfolio``：持仓聚合的读模型 owner；基础层只调用它聚合行。
#: - ``strategy_registry``：审计戳的唯一 owner（策略版本 pin）。
#: - ``paper_timing``：低开销诊断计时器，无业务语义。
#: - ``universe`` / ``factors``：交易日历与涨跌停阈值的事实来源。
FOUNDATION_ALLOWED_DEPENDENCIES = {
    "paper_position_read_model": frozenset({"execution_verification", "paper_portfolio"}),
    "paper_repository": frozenset({"execution_verification", "strategy_registry"}),
    "paper_storage": frozenset({"paper_timing"}),
    "paper_trading_rules": frozenset({"factors", "universe"}),
}


@lru_cache(maxsize=4)
def _foundation_violations_cached(root: Path = BACKEND):
    """基础层 → 业务域的反向依赖（缓存版，返回不可变元组）。

    返回 ``((foundation_module, offending_target), ...)``，按模块与目标排序。

    基础层之间的互相依赖是允许的（它们同属"事实/规则/纯工具"这一层）；
    基础层依赖**业务域**模块才是方向错误。白名单之外的依赖一律视为违规。

    只读 AST，所以缓存是安全的：``measure_all`` 与 ``foundation_violations``
    原本会各自把全仓再走一遍（同一进程里 3~4 次），2026-10-29 起共用一份。
    """
    records = list(_trees(production_paths(root)))
    modules = {path.stem for path, _, _ in records}
    violations = []
    for path, _, tree in records:
        module = path.stem
        if module not in FOUNDATION_MODULES:
            continue
        allowed = (FOUNDATION_ALLOWED_DEPENDENCIES.get(module, frozenset())
                   | (FOUNDATION_MODULES - {module}))
        for target in sorted(_local_imports(tree, modules) - {module}):
            if target not in allowed:
                violations.append((module, target))
    return tuple(sorted(violations))


def foundation_violations(root: Path = BACKEND):
    """``_foundation_violations_cached`` 的列表视图（调用方可安全持有）。"""
    return list(_foundation_violations_cached(root))


def domain_of(module: str) -> str:
    if module in FOUNDATION_MODULES:
        return "foundation"
    head = module.split("_", 1)[0]
    return head if head in DOMAIN_GROUPS else "other"


def _private_cross_access(tree, module, modules):
    imported_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_aliases.update(alias.asname or alias.name.split(".")[0]
                                    for alias in node.names if alias.name.split(".")[0] in modules)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in modules:
            imported_aliases.update(alias.asname or node.module.split(".")[0] for alias in node.names)
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
            continue
        if isinstance(node.value, ast.Name) and node.value.id in imported_aliases and node.value.id != module:
            count += 1
    return count


def dependency_private_fields(tree) -> int:
    """统计 ``*Dependencies`` 容器上的私有字段名（跨模块私有依赖的真实载体）。

    ``private_cross_access`` 只认 ``import 别名.私有名`` 这一种写法，因此
    "把 66 个私有函数注入 dataclass、消费者再用 ``deps._x`` 调用"这种形态
    完全落在它的视野外（见 2026-10-29 对抗性审查 F-01）。这里度量的是**契约
    本身**：容器上有多少个私有字段名，就有多少个跨模块私有依赖需要治理。
    """
    total = 0
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("Dependencies"):
            continue
        for item in node.body:
            targets = []
            if isinstance(item, ast.AnnAssign):
                targets = [item.target]
            elif isinstance(item, ast.Assign):
                targets = item.targets
            for target in targets:
                if (isinstance(target, ast.Name) and target.id.startswith("_")
                        and not target.id.startswith("__")):
                    total += 1
    return total


def private_channel_counts(tree, modules) -> tuple[int, int]:
    """一趟 AST 走查同时统计两条"口径外私有通道"。

    返回 ``(from_import_private, dynamic_name_bridges)``：

    * ``from <项目模块> import _私有名`` —— 别名形态之外的第二条私有通道；
      **绝对导入与同包相对导入（``from .module import _x``）都算**：本仓库的生产
      模块都在同一层目录里，相对导入只是写法不同，不能因为写法绕开统计；
    * ``globals()[...]`` / ``getattr(x, computed)`` / ``sys.modules[...]``
      —— 任何基于静态 Name/Attribute 的守卫都看不见的"按名字取成员"。

    合并成一次 ``ast.walk``：``measure_all`` 对 316 个模块逐个调用，分成两趟
    会让守卫多付一次全仓走查（实测各约 0.7 秒）。
    """
    from_import = 0
    bridges = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # level==0 是绝对导入，level>=1 是同包相对导入（``from .x import y``，
            # 此时 node.module 只含模块名）。两种写法指向同一批生产模块，必须同口径
            # 统计；原先只认 level==0，`from .market_data_contract import _parse_instant`
            # 这类回退分支就成了漏检。
            if node.module and node.module.split(".")[0] in modules:
                from_import += sum(
                    1 for alias in node.names
                    if alias.name.startswith("_") and not alias.name.startswith("__"))
        elif isinstance(node, ast.Subscript):
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                    and value.func.id == "globals":
                bridges += 1
            elif isinstance(value, ast.Attribute) and value.attr == "modules" \
                    and isinstance(value.value, ast.Name) and value.value.id == "sys":
                bridges += 1
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "getattr" and len(node.args) >= 2:
            name_arg = node.args[1]
            if not (isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str)
                    and not name_arg.value.startswith("_")):
                bridges += 1
    return from_import, bridges


def from_import_private_names(tree, modules) -> int:
    """``from <项目模块> import _私有名`` 的数量（独立入口，便于定点核查）。"""
    return private_channel_counts(tree, modules)[0]


def dynamic_name_bridges(tree) -> int:
    """按名字字符串动态取模块成员的站点数（独立入口，便于定点核查）。"""
    return private_channel_counts(tree, ())[1]


def function_names(node):
    """Names loaded/stored in one function, excluding nested scopes."""
    referenced, locals_used = set(), set()

    def collect(item, *, root=True):
        if not root and isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(item, ast.Name):
            (referenced if isinstance(item.ctx, ast.Load) else locals_used).add(item.id)
        for child in ast.iter_child_nodes(item):
            collect(child, root=False)

    collect(node)
    return referenced, locals_used


def measure_all(root: Path = BACKEND):
    """全部耦合事实（同一进程内按 ``root`` 复用一次全仓 AST 走查）。

    2026-10-29：架构守卫里 ``test_no_metric_regresses`` 与
    ``test_no_backend_import_cycles`` 各调一次本函数，实测各约 10 秒。
    度量是纯只读 AST，因此按 ``root`` 缓存；返回浅拷贝，调用方改键不会污染
    缓存。需要强制重算时用 ``measure_all.cache_clear()``。
    """
    return dict(_measure_all_cached(root))


measure_all.cache_clear = lambda: _measure_all_cached.cache_clear()  # type: ignore[attr-defined]


@lru_cache(maxsize=4)
def _measure_all_cached(root: Path = BACKEND):
    records = list(_trees(production_paths(root)))
    modules = {path.stem for path, _, _ in records}
    edges = {path.stem: _local_imports(tree, modules) - {path.stem}
             for path, _, tree in records}
    cycles = strongly_connected(edges)
    deferred = raw_sql = private_access = max_globals = 0
    injected_fields = from_import_private = name_bridges = 0
    max_file_lines = max_function_lines = 0
    helpers = Counter()
    numeric_helpers = Counter()
    fanout = Counter()
    unsuffixed = 0
    side_effects = 0
    largest_file = largest_function = None
    for path, source, tree in records:
        max_file_lines = max(max_file_lines, len(source.splitlines()))
        if len(source.splitlines()) == max_file_lines:
            largest_file = path.name
        if path.stem.split("_")[0] not in DOMAIN_PREFIXES:
            unsuffixed += 1
        for table in {match.group(0) for match in PAPER_TABLE.finditer(source)}:
            fanout[table] += 1
        private_access += _private_cross_access(tree, path.stem, modules)
        injected_fields += dependency_private_fields(tree)
        from_import_tree, bridges_tree = private_channel_counts(tree, modules)
        from_import_private += from_import_tree
        name_bridges += bridges_tree
        defined = {node.name for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        helpers.update(defined)
        numeric_helpers.update(defined & NUMERIC_NAMES)
        raw_sql += any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr in {"execute", "executemany", "executescript"}
                       for node in ast.walk(tree))
        side_effects += sum(isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                            and not (isinstance(node.value.func, ast.Name) and node.value.func.id == "print")
                            for node in tree.body)
        for parent in ast.walk(tree):
            children = getattr(parent, "body", None)
            if not isinstance(children, list):
                continue
            for node in children:
                if isinstance(node, (ast.Import, ast.ImportFrom)) and not isinstance(parent, ast.Module):
                    deferred += 1
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                size = (parent.end_lineno or parent.lineno) - parent.lineno + 1
                if size > max_function_lines:
                    max_function_lines, largest_function = size, f"{path.stem}.{parent.name}"
                referenced, locals_used = function_names(parent)
                globals_used = referenced - {arg.arg for arg in parent.args.args + parent.args.kwonlyargs}
                if parent.args.vararg:
                    globals_used.discard(parent.args.vararg.arg)
                if parent.args.kwarg:
                    globals_used.discard(parent.args.kwarg.arg)
                max_globals = max(max_globals, len(globals_used - locals_used - set(defined)))
    return {
        "import_cycles": len(cycles),
        "import_cycle_members": cycles,
        # 模块 → 项目内依赖（排序后的列表）。守卫要用同一份边集合做白名单
        # 核查，不必再自己走一遍全仓（原先那份重复走查实测 33 秒）。
        "import_edges": {source: sorted(targets) for source, targets in sorted(edges.items())},
        "deferred_imports": deferred,
        "raw_sql_modules": raw_sql,
        "paper_table_fanout": dict(sorted(fanout.items())),
        "paper_table_fanout_max": max(fanout.values(), default=0),
        "private_cross_access": private_access,
        # 事实/基础 owner 两侧的边不算业务域耦合：业务域直接 import owner
        # 是 A1 期望的"显式依赖"。foundation 反向依赖业务域属于另一种问题，
        # 由 import_cycles / private_cross_access 以及独立的
        # ``foundation_violations`` 守卫覆盖（单向导入不会产生前两者）。
        "cross_domain_edges": sum(
            1 for source, targets in edges.items()
            for target in targets
            if domain_of(source) != domain_of(target)
            and "foundation" not in (domain_of(source), domain_of(target))),
        "max_file_lines": max_file_lines,
        "largest_file": largest_file,
        "max_function_lines": max_function_lines,
        "largest_function": largest_function,
        "max_function_globals": max_globals,
        "duplicate_helpers": sum(1 for count in helpers.values() if count > 1),
        "helper_definitions": sum(helpers.values()),
        "duplicate_helper_names": dict(sorted(
            (name, count) for name, count in helpers.items() if count > 1)),
        "duplicate_numeric_helpers": sum(numeric_helpers.values()),
        "duplicate_numeric_helper_names": dict(sorted(numeric_helpers.items())),
        # 私有跨模块依赖的三条"口径外"通道（2026-10-29 对抗性审查 F-01/F-02）：
        # ``private_cross_access`` 只认 ``import 别名.私有名``，上面这三种形态
        # 能拿到私有成员却完全不计数，因此必须各自成为独立事实指标。
        "injected_private_fields": injected_fields,
        "from_import_private_names": from_import_private,
        "dynamic_name_bridges": name_bridges,
        "unsuffixed_module_ratio": round(unsuffixed / len(records), 4) if records else 0,
        "import_side_effects": side_effects,
        "production_modules": len(records),
        # 基础层 → 业务域的反向依赖：单向公开导入既不产生环也不产生私有
        # 访问，上面的指标发现不了，必须单列（审计第 4 项）。
        "foundation_violations": len(foundation_violations(root)),
        "foundation_violation_members": foundation_violations(root),
    }
