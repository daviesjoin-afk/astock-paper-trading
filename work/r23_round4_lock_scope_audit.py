# -*- coding: utf-8 -*-
"""BEGIN IMMEDIATE 锁范围复核（维护性检查，不改生产代码）。

`generate_signals` 的最终 commit phase 用 `_db(immediate=True)`。本脚本用 AST
把它锁内**实际调用的函数**全部列出来，再按「允许（本地/DB）」与「禁止（网络/
provider/大规模扫描）」分类，确认没有 provider 调用被搬进写锁。
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")

FORBIDDEN = (
    "fetch_market_snapshot_full", "fetch_sector_flow", "fetch_hot_sector_snapshot",
    "fetch_indices", "fetch_finance_latest", "check_data_source_health",
    "refresh_history", "market_microstructures", "capture_candidate_snapshot",
    "record_shadow_run", "http_get", "requests.",
)
#: 这些是 provider 入口名，但会作为局部字典名（如 ``evidence_quotes``）合法出现；
#: 只有作为**调用**出现才算违规。用 AST 调用名精确判定，不用子串。
FORBIDDEN_CALLS = ("_news_for", "_quotes", "_validated_live_universe",
                   "_rebuild_selection_factor_cache", "fetch_market_snapshot_full",
                   "fetch_sector_flow", "fetch_hot_sector_snapshot")


def main() -> int:
    src = open(os.path.join(BACKEND, "paper_trading.py"), encoding="utf-8").read()
    lines = src.splitlines()
    tree = ast.parse(src)
    target = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "generate_signals")

    block = None
    for with_node in ast.walk(target):
        if not isinstance(with_node, ast.With):
            continue
        call = with_node.items[0].context_expr
        if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_db"):
            continue
        body = "\n".join(lines[with_node.lineno - 1:with_node.end_lineno])
        if "INSERT OR IGNORE INTO paper_signals" in body:
            block = (call, with_node, body)
            break
    if block is None:
        print("找不到 signal commit phase")
        return 1

    call, node, body = block
    kw = {k.arg: k.value for k in call.keywords}
    immediate = kw.get("immediate")
    print(f"commit phase @ paper_trading.py:{node.lineno}-{node.end_lineno}")
    print(f"  immediate 字面量 = {getattr(immediate, 'value', None)}")
    print(f"  块内行数 = {node.end_lineno - node.lineno + 1}")

    called = sorted({c.func.id for c in ast.walk(node)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)})
    attr_called = sorted({c.func.attr for c in ast.walk(node)
                          if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)})
    print(f"\n锁内被调用的函数（{len(called)} 个）：")
    offenders = []
    for name in called:
        bad = name in FORBIDDEN or name in FORBIDDEN_CALLS
        if bad:
            offenders.append(name)
        print(f'  {"FORBIDDEN" if bad else "ok       "}  {name}')
    print(f"\n锁内被调用的方法（{len(attr_called)} 个）：{attr_called}")

    # 文本层：只检查明确是远程/provider 的入口名（局部字典名不算）。
    text_forbidden = [token for token in FORBIDDEN if token in body]
    print(f"\n块内出现的禁止 token：{text_forbidden or '（无）'}")

    print("\n=== 结论 ===")
    if offenders or text_forbidden:
        print(f"FAIL: 写锁内存在 provider/网络调用：{sorted(set(offenders + text_forbidden))}")
        return 1
    print("PASS: 锁内只有本地决策 + DB 写入；provider 调用全部在拿锁之前完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
