# -*- coding: utf-8 -*-
"""确认 fetch_market_snapshot_full 的 4.4s 是**网络超时**（离线环境），而非 DB 锁或 R23 改动。

同时对比 R23 是否让只读 explain 路径变慢（对比 git stash 不适用，改为直接测量
provider 层与 DB 层各自的耗时）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
data_dir = tempfile.mkdtemp(prefix="r23-net-")
os.environ["ASTOCK_DATA_DIR"] = data_dir
os.environ["ASTOCK_DEMO"] = "1"
os.environ["ASTOCK_DEMO_FORCE"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import data_fetcher as dfc
import paper_trading as PT

print("=== 1) 直接测 clist 网络调用（离线应快速失败或超时）===")
t0 = time.time()
try:
    raw = dfc._fetch_clist({"f12": "code"}, fid="f3", pages=1, return_meta=True)
    print(f"  _fetch_clist ok in {time.time()-t0:.3f}s rows={len((raw or {}).get('rows') or []) if isinstance(raw, dict) else 'n/a'}")
except Exception as exc:
    print(f"  _fetch_clist raised in {time.time()-t0:.3f}s: {type(exc).__name__}: {exc}")

print("\n=== 2) fetch_market_snapshot_full 各阶段 ===")
t0 = time.time()
r = dfc.fetch_market_snapshot_full(max_age=240)
print(f"  total {time.time()-t0:.3f}s rows={len(r or [])}")

print("\n=== 3) DB 层（explain 里的纯 DB 部分是否慢）===")
PT.init_db()
t0 = time.time()
with PT._db() as conn:
    conn.execute("SELECT COUNT(*) FROM paper_signals").fetchone()
print(f"  _db() 打开+查询 {time.time()-t0:.3f}s")

t0 = time.time()
with PT._db(immediate=True) as conn:
    conn.execute("SELECT COUNT(*) FROM paper_signals").fetchone()
print(f"  _db(immediate=True) 打开+查询 {time.time()-t0:.3f}s")
