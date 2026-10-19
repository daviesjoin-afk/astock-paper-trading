"""Static architecture inventory; never imports application modules."""

import ast
import json
import re
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    paths = sorted(p for p in (root / "backend").glob("*.py") if not p.name.startswith("test_"))
    names = {p.stem for p in paths}
    modules = []
    for path in paths:
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        imports = set()
        functions = []
        writers = []
        dependencies = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names if a.name.split(".")[0] in names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                name = node.module.split(".")[0]
                if name in names:
                    imports.add(name)
            elif isinstance(node, ast.ImportFrom) and node.level:
                imports.update(a.name for a in node.names if a.name in names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(
                    {"name": node.name, "line": node.lineno, "lines": node.end_lineno - node.lineno + 1}
                )
            elif isinstance(node, ast.ClassDef) and node.name.endswith(("Dependencies", "Ports")):
                dependencies[node.name] = [
                    n.target.id
                    for n in node.body
                    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
                ]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"execute", "executemany", "executescript"}
                and node.args
            ):
                arg = node.args[0]
                sql = (
                    arg.value
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    else "".join(
                        n.value
                        for n in arg.values
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    )
                    if isinstance(arg, ast.JoinedStr)
                    else ""
                )
                for operation, table in re.findall(
                    r"\b(INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(paper_\w+)", sql, re.I
                ):
                    writers.append(
                        {"table": table.lower(), "operation": operation.upper(), "line": node.lineno}
                    )
        modules.append(
            {
                "name": path.stem,
                "lines": len(source.splitlines()),
                "imports": sorted(imports),
                "functions": sorted(functions, key=lambda f: -f["lines"]),
                "dependencies": dependencies,
                "literal_sql_writes": writers,
            }
        )
    graph = {m["name"]: m["imports"] for m in modules}
    index = {}
    low = {}
    stack = []
    active = set()
    components = []

    def visit(name):
        index[name] = low[name] = len(index)
        stack.append(name)
        active.add(name)
        for child in graph[name]:
            if child not in index:
                visit(child)
                low[name] = min(low[name], low[child])
            elif child in active:
                low[name] = min(low[name], index[child])
        if low[name] == index[name]:
            component = []
            while True:
                child = stack.pop()
                active.remove(child)
                component.append(child)
                if child == name:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for name in graph:
        if name not in index:
            visit(name)
    result = {
        "scope": "backend top-level non-test .py; literal imports including local imports; SQL constants and f-string literal parts only; excludes dynamic imports/callback edges/generated SQL",
        "module_count": len(modules),
        "cycles": components,
        "modules": modules,
    }
    (root / "work" / "current_architecture_inventory.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "module_count": len(modules),
                "cycles": components,
                "largest": [
                    {
                        "name": m["name"],
                        "lines": m["lines"],
                        "imports": len(m["imports"]),
                        "importers": sum(m["name"] in other["imports"] for other in modules),
                        "functions": m["functions"][:5],
                        "dependencies": {k: len(v) for k, v in m["dependencies"].items()},
                    }
                    for m in sorted(modules, key=lambda m: -m["lines"])[:12]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
