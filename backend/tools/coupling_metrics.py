"""Deterministic, read-only coupling metrics for the backend module graph.

The analyzer owns source discovery and a single syntax-tree pass per module.  It
never imports production modules, so measuring architecture cannot initialize
runtime state.  Budgets belong in ``test_architecture_boundary_audit``.
"""
from __future__ import annotations

import ast
from collections import Counter
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
    """
    try:
        from test_support import ast_cache
    except ImportError:
        ast_cache = None
    for path in paths:
        if ast_cache is not None:
            yield path, ast_cache.source(path), ast_cache.tree(path)
            continue
        source = path.read_text(encoding="utf-8")
        yield path, source, ast.parse(source, filename=str(path))


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


def foundation_violations(root: Path = BACKEND):
    """基础层 → 业务域的反向依赖。

    返回 ``[(foundation_module, offending_target), ...]``，按模块与目标排序。

    基础层之间的互相依赖是允许的（它们同属"事实/规则/纯工具"这一层）；
    基础层依赖**业务域**模块才是方向错误。白名单之外的依赖一律视为违规。
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
    return sorted(violations)


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
    records = list(_trees(production_paths(root)))
    modules = {path.stem for path, _, _ in records}
    edges = {path.stem: _local_imports(tree, modules) - {path.stem}
             for path, _, tree in records}
    cycles = strongly_connected(edges)
    deferred = raw_sql = private_access = max_globals = 0
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
        "duplicate_helper_names": dict(sorted(
            (name, count) for name, count in helpers.items() if count > 1)),
        "duplicate_numeric_helpers": sum(numeric_helpers.values()),
        "duplicate_numeric_helper_names": dict(sorted(numeric_helpers.items())),
        "unsuffixed_module_ratio": round(unsuffixed / len(records), 4) if records else 0,
        "import_side_effects": side_effects,
        "production_modules": len(records),
        # 基础层 → 业务域的反向依赖：单向公开导入既不产生环也不产生私有
        # 访问，上面的指标发现不了，必须单列（审计第 4 项）。
        "foundation_violations": len(foundation_violations(root)),
        "foundation_violation_members": foundation_violations(root),
    }
