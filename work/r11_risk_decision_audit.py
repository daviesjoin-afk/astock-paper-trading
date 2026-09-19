# -*- coding: utf-8 -*-
"""§8 枚举：_risk_log 实际写入的 (side, decision) 取值。"""
import ast
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = [
    "backend/paper_trading.py",
    "backend/manual_orders.py",
    "backend/execution_planner.py",
]


def lit(n):
    if isinstance(n, ast.Constant):
        return repr(n.value)
    if isinstance(n, ast.Name):
        return f"<var:{n.id}>"
    if isinstance(n, ast.Attribute):
        return f"<attr:{n.attr}>"
    if isinstance(n, ast.JoinedStr):
        return "<fstring>"
    if isinstance(n, ast.BinOp):
        return "<binop>"
    if isinstance(n, ast.IfExp):
        return "<ifexp>"
    return f"<{type(n).__name__}>"


pairs = defaultdict(list)
for rel in FILES:
    path = os.path.join(ROOT, rel)
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            name = fn.id
        elif isinstance(fn, ast.Attribute):
            name = fn.attr
        else:
            continue
        if name != "_risk_log":
            continue
        side = lit(node.args[3]) if len(node.args) > 3 else "?"
        dec = lit(node.args[4]) if len(node.args) > 4 else "?"
        pairs[(side, dec)].append(f"{rel}:{node.lineno}")

print("=== _risk_log(side, decision) call sites ===")
for (side, dec), locs in sorted(pairs.items()):
    print(f"  side={side:26s} decision={dec:34s} n={len(locs):2d}  e.g. {locs[0]}")

print()
print("=== distinct decision expressions ===")
for d in sorted({d for (_s, d) in pairs}):
    print("  ", d)
