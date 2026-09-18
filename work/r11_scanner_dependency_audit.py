# -*- coding: utf-8 -*-
"""§8 枚举：rebalance_scanner 引用的全部表 + 每个表在真实生产 schema 中是否存在。

分类口径：
* Paper ledger facts —— 描述「账户/持仓/委托/信号/风控」的账本事实；
* Rebalance state   —— 调仓引擎自己的持久状态。

只读；不写任何库。
"""
import ast
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

SCANNER = os.path.join(ROOT, "backend", "rebalance_scanner.py")

# 从 SQL 文本里抽表名（FROM / JOIN / INSERT INTO / UPDATE / DELETE FROM）
PATTERNS = (
    re.compile(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.I),
    re.compile(r"\bJOIN\s+([A-Za-z_][A-Za-z0-9_]*)", re.I),
    re.compile(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.I),
    re.compile(r"\bUPDATE\s+([A-Za-z_][A-Za-z0-9_]*)", re.I),
    re.compile(r"\bDELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.I),
    re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.I),
)
# SQL 关键字/函数噪声，避免误报成表名
NOISE = {
    "select", "where", "and", "or", "set", "values", "limit", "order", "by",
    "group", "having", "as", "on", "using", "json_extract", "coalesce",
    "substr", "max", "min", "count", "sum", "case", "when", "then", "else",
    "end", "not", "null", "in", "exists", "union", "all", "distinct",
}

with open(SCANNER, encoding="utf-8") as fh:
    src = fh.read()
tree = ast.parse(src)

hits = defaultdict(set)          # table -> {"sql", "ddl"}
for node in ast.walk(tree):
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        continue
    text = node.value
    if "SELECT" not in text.upper() and "INSERT" not in text.upper() \
            and "UPDATE" not in text.upper() and "CREATE" not in text.upper() \
            and "DELETE" not in text.upper():
        continue
    kind = "ddl" if "CREATE TABLE" in text.upper() else "sql"
    for pat in PATTERNS:
        for m in pat.finditer(text):
            t = m.group(1)
            if t.lower() in NOISE:
                continue
            hits[t].add(kind)

# ── 真实生产 schema ────────────────────────────────────────────────────────
import paper_trading as PT  # noqa: E402
from unittest import mock  # noqa: E402

tmp = tempfile.mkdtemp(prefix="r11_enum_")
p = os.path.join(tmp, "paper.sqlite3")
with mock.patch.object(PT, "DB_PATH", p), \
        mock.patch.object(PT, "_benchmark_close", return_value=None), \
        mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True):
    PT.init_db()
conn = sqlite3.connect(p)
live_tables = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
conn.close()

REBALANCE_STATE = {"rebalance_scans", "rebalance_plans", "rebalance_cooldown"}

print("=" * 84)
print("rebalance_scanner.py 引用的全部表  vs  真实生产 paper schema")
print("=" * 84)
print(f"{'table':26s} {'kind':16s} {'in prod schema':15s} ref")
print("-" * 84)
missing = []
for t in sorted(hits):
    kind = "Rebalance state" if t in REBALANCE_STATE else "Paper ledger fact"
    present = t in live_tables
    if not present and t not in REBALANCE_STATE:
        missing.append(t)
    ref = ",".join(sorted(hits[t]))
    print(f"{t:26s} {kind:16s} {'YES' if present else 'NO':15s} {ref}")

print()
print("── 缺失的 Paper ledger fact（引用但生产 schema 中不存在）──")
print(f"  {missing if missing else 'none'}")
print()
print(f"总表数: {len(hits)}  |  rebalance state: {sorted(REBALANCE_STATE & set(hits))}")
print(f"paper ledger facts: {sorted(set(hits) - REBALANCE_STATE)}")
