"""PR-10 写入清单扫描与收尾计划渲染。

两件事：
1. 把 `backend/*.py` 里对十张核心表的**静态 SQL 写入**与**动态拼接写入**逐条抽出来，
   并区分「真实写目标」与「语句里被引用的读表」—— 既有
   `test_ledger_ownership.write_sites()` 会把 `UPDATE paper_signals ... WHERE id IN
   (SELECT ... FROM paper_orders ...)` 里的订单表也记成订单写入，本扫描器修掉这个口径。
2. 把人工判定表（`backend/ledger_owner_plan.json`）渲染成 markdown，供
   `docs/LEDGER_OWNERSHIP.md` 使用；守卫测试比对生成结果，避免计划与源码漂移。

用法：
    python backend/tools/ledger_write_inventory.py                # 写 inventory JSON
    python backend/tools/ledger_write_inventory.py --markdown     # 打印计划 markdown
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

#: PR-10 收尾清单一：十张核心表。
CORE_TABLES = (
    "paper_orders",
    "paper_signals",
    "paper_accounts",
    "paper_cycles",
    "paper_fills",
    "paper_positions",
    "paper_position_lots",
    "paper_capital_reservations",
    "paper_risk_decisions",
    "paper_audit",
)

#: 仍为 pending、需要在本阶段收口的四张表。
PENDING_TABLES = ("paper_orders", "paper_signals", "paper_accounts", "paper_cycles")

WRITE_PATTERNS = (
    ("UPDATE", re.compile(r"\bUPDATE\s+(?:OR\s+\w+\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.I)),
    ("INSERT", re.compile(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)),
    ("DELETE", re.compile(r"\bDELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)),
    ("REPLACE", re.compile(r"\bREPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)),
)
READ_PATTERN = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
SQL_START = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|WITH|CREATE|DROP|ALTER|PRAGMA|BEGIN|SAVEPOINT|RELEASE)\b",
    re.I,
)

PLAN_PATH = BACKEND / "ledger_owner_plan.json"
PLAN_PACKAGES = ("A", "B", "C", "D")
PLAN_START = "<!-- ledger-owner-plan:start -->"
PLAN_END = "<!-- ledger-owner-plan:end -->"


def module_role(path: Path) -> str:
    """模块角色：运行时写方 / 测试 / 种子 / 迁移 / 基准夹具。"""
    name = path.stem
    if name.startswith("test_"):
        return "test"
    if name in {"demo_seed", "demo_reset"} or name.endswith("_seed"):
        return "seed"
    if name in {"paper_schema_migrations", "recover_legacy_order_cycle_provenance",
                "db_migrate"}:
        return "migration"
    if name == "execution_quality_shadow" or name.endswith("_fixture"):
        return "fixture"
    if name == "tools" or name.startswith("tools."):
        return "tooling"
    return "runtime"


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _write_targets(sql: str) -> list[str]:
    targets: list[str] = []
    for _, pattern in WRITE_PATTERNS:
        for match in pattern.finditer(sql):
            table = match.group(1).lower()
            if table not in targets:
                targets.append(table)
    return targets


def _read_tables(sql: str) -> list[str]:
    # DELETE FROM 的首表是写目标；其余 FROM/JOIN（包括子查询）才是读引用。
    sql = re.sub(r"^\s*DELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)",
                 r"DELETE \1", sql, flags=re.I)
    reads: list[str] = []
    for match in READ_PATTERN.finditer(sql):
        table = match.group(1).lower()
        if table not in reads:
            reads.append(table)
    return reads


def _iter_string_nodes(tree: ast.AST):
    """产出 (节点, 是否动态拼接, 组合后的文本)。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node, False, node.value
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(str(value.value))
                else:
                    parts.append("{}")
            yield node, True, "".join(parts)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            parts = []
            ok = False
            for child in (node.left, node.right):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    parts.append(child.value)
                    ok = True
                else:
                    parts.append("{}")
            if ok:
                yield node, True, "".join(parts)


def _function_spans(tree: ast.AST) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            spans.append((node.lineno, end, node.name))
    return spans


def _owner_function(spans, line: int) -> str:
    best = None
    for start, end, name in spans:
        if start <= line <= end and (best is None or start >= best[0]):
            best = (start, name)
    return best[1] if best else "<module>"


def scan(root: Path = BACKEND) -> dict:
    entries: list[dict] = []
    for path in sorted(root.glob("*.py")):
        role = module_role(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法错误由 compileall 负责
            continue
        spans = _function_spans(tree)
        for node, dynamic, text in _iter_string_nodes(tree):
            if not SQL_START.match(text):
                continue
            targets = [t for t in _write_targets(text) if t in CORE_TABLES]
            if not targets:
                continue
            line = getattr(node, "lineno", 0)
            reads = [t for t in _read_tables(text) if t in CORE_TABLES and t not in targets]
            entries.append({
                "module": path.stem,
                "function": _owner_function(spans, line),
                "line": line,
                "role": role,
                "kind": "dynamic" if dynamic else "static",
                "write_targets": targets,
                "read_refs": reads,
                "sql": _normalize(text)[:160],
            })
    entries.sort(key=lambda item: (item["module"], item["line"], item["sql"]))
    return {"core_tables": list(CORE_TABLES), "entries": entries}


def group_targets(inventory: dict) -> dict[str, list[str]]:
    """(module::function) → 运行时写入的 pending 表集合。"""
    groups: dict[str, set[str]] = {}
    for entry in inventory["entries"]:
        if entry["role"] != "runtime":
            continue
        targets = set(entry["write_targets"]) & set(PENDING_TABLES)
        if targets:
            groups.setdefault(f'{entry["module"]}::{entry["function"]}', set()).update(targets)
    return {key: sorted(value) for key, value in sorted(groups.items())}


def related_tests(module: str, function: str, limit: int = 3) -> list[str]:
    """在 backend/test_*.py 里找提到该函数的文件，作为该操作的现有测试证据。"""
    needle = function.strip("<>")
    hits = []
    for path in sorted(BACKEND.glob("test_*.py")):
        if not needle:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover
            continue
        if needle in text:
            hits.append(path.stem)
    return hits[:limit]


def render_plan_markdown(inventory: dict | None = None, plan: dict | None = None) -> str:
    inventory = inventory or scan()
    plan = plan or json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    operations = plan["operations"]
    grouped = group_targets(inventory)

    rows_by_package: dict[str, list[str]] = {name: [] for name in PLAN_PACKAGES}
    for key in sorted(operations):
        spec = operations[key]
        tables = grouped.get(key, [])
        if not tables:
            continue
        module, function = key.split("::", 1)
        tests = related_tests(module, function)
        tests_text = "、".join(f"`{name}`" for name in tests) if tests else "待补"
        for table in tables:
            target = (spec.get("targets") or {}).get(table)
            if target is None:
                owner, package = spec["single_owner"], spec["single_package"]
                operation = spec["operation"]
            else:
                owner, package = target["owner"], target["package"]
                operation = target.get("operation") or spec["operation"]
            rows_by_package.setdefault(package, []).append(
                f"| `{table}` | {operation} | `{module}::{function}` "
                f"| {spec['judge']} | `{owner}` | {spec['transaction']} | {tests_text} |")

    lines: list[str] = []
    for package in PLAN_PACKAGES:
        rows = rows_by_package.get(package) or []
        if not rows:
            continue
        lines.append(f"### 工作包 {package}")
        lines.append("")
        lines.append("| 表 | 业务操作 | 现有写入点 | 业务判定者 | 最终写入 owner | 调用方事务 | 相关测试 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        lines.extend(rows)
        lines.append("")
    return chr(10).join(lines).rstrip() + chr(10)


def scan_readers(root: Path = BACKEND) -> dict[str, list[str]]:
    """每张核心表被哪些运行时模块**读取**（SELECT/JOIN 引用），供读取契约守卫使用。"""
    readers: dict[str, set[str]] = {table: set() for table in CORE_TABLES}
    for path in sorted(root.glob("*.py")):
        if module_role(path) != "runtime":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node, _dynamic, text in _iter_string_nodes(tree):
            if not SQL_START.match(text):
                continue
            for table in _read_tables(text):
                if table in readers:
                    readers[table].add(path.stem)
    return {table: sorted(modules) for table, modules in readers.items()}


def main(argv: list[str]) -> int:
    if "--markdown" in argv:
        print(render_plan_markdown(), end="")
        return 0
    out = Path(argv[1]) if len(argv) > 1 else BACKEND / "ledger_write_inventory.json"
    payload = scan()
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + chr(10),
                   encoding="utf-8")
    runtime = [e for e in payload["entries"] if e["role"] == "runtime"]
    print(f"entries={len(payload['entries'])} runtime={len(runtime)} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
