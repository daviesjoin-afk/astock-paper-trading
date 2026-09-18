# -*- coding: utf-8 -*-
"""§8 实证：real-time risk 的权威 paper-ledger 证据落在哪张表/哪个列。

只读探测，不写任何库。
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import paper_trading as PT  # noqa: E402
from unittest import mock  # noqa: E402


def schema_probe(label, path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    names = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"))
    print(f"── {label} ──")
    print(f"  risk_log present      : {'risk_log' in names}")
    print(f"  paper_risk_decisions  : {'paper_risk_decisions' in names}")
    print(f"  paper_orders          : {'paper_orders' in names}")
    if "paper_risk_decisions" in names:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(paper_risk_decisions)")]
        print(f"  prd columns           : {cols}")
        n = conn.execute("SELECT COUNT(*) FROM paper_risk_decisions").fetchone()[0]
        print(f"  prd rows              : {n}")
        for row in conn.execute(
            "SELECT side,decision,substr(payload,1,160) AS p FROM paper_risk_decisions"
            " ORDER BY id DESC LIMIT 8"
        ):
            print(f"    side={row['side']!r} decision={row['decision']!r}")
            print(f"      payload={row['p']}")
    if "paper_orders" in names:
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE side='sell'"
                " AND json_extract(risk_payload,'$.exit_class') IS NOT NULL"
            ).fetchone()[0]
            print(f"  sell orders w/ risk_payload.exit_class: {rows}")
        except sqlite3.Error as exc:
            print(f"  exit_class probe failed: {exc}")
    conn.close()
    print()


# 1) 本地真实 data_cache
for f in ("paper_trading.sqlite3", "adaptive_learning.sqlite3"):
    p = os.path.join(ROOT, "data_cache", f)
    if os.path.exists(p):
        schema_probe(f"data_cache/{f}", p)

# 2) 由真实 init_db 生成的干净生产 schema
tmp = tempfile.mkdtemp(prefix="r11_risk_")
p = os.path.join(tmp, "paper.sqlite3")
with mock.patch.object(PT, "DB_PATH", p), \
        mock.patch.object(PT, "_benchmark_close", return_value=None), \
        mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True):
    PT.init_db()
schema_probe("fresh init_db() paper.sqlite3", p)

# 3) 真实驱动一次风控退出，看 exit_class 落在哪
print("── 驱动真实 _sell_plan 看 exit_class 落点 ──")
import paper_trading as PT2  # noqa: E402

position = {"account_id": "tq_breakout", "code": "600519", "name": "X",
            "cost": 10.0, "qty": 100, "peak_price": 10.0, "take_stage": 0,
            "entry_date": "2026-08-31", "available_qty": 100}
quote = {"code": "600519", "price": 9.4, "pct": -6.0, "high": 10.0, "low": 9.3}
ratio, reason, next_stage, detail = PT2._sell_plan(
    position, quote, __import__("datetime").date(2026, 9, 18), [])
print(f"  ratio={ratio}")
print(f"  reason={reason}")
print(f"  detail.exit_class       = {detail.get('exit_class')!r}")
print(f"  detail.exit_reason_code = {detail.get('exit_reason_code')!r}")
print(f"  detail.protective_exit  = {detail.get('protective_exit')!r}")
