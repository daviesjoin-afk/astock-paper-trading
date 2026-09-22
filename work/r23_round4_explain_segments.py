# -*- coding: utf-8 -*-
"""定位 /api/paper/allocation-explain 的 4.6s 花在哪一步（e2e 离线环境）。

只做只读测量，不改生产代码。用与 e2e server.py 相同的环境（ASTOCK_DEMO=1 +
独立临时数据目录）复现真实请求路径，逐段计时。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")

data_dir = tempfile.mkdtemp(prefix="r23-seg-")
os.environ["ASTOCK_DATA_DIR"] = data_dir
os.environ["ASTOCK_DEMO"] = "1"
os.environ["ASTOCK_DEMO_FORCE"] = "1"
os.environ["ASTOCK_ENABLE_FALLBACK_THREADS"] = "0"
os.environ["LLM_ADVISOR_ENABLED"] = "0"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import data_fetcher as dfc
import paper_trading as PT


def timed(label, fn):
    t0 = time.time()
    try:
        value = fn()
        note = ""
    except Exception as exc:
        value, note = None, f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - t0
    print(f"  {elapsed:7.3f}s  {label} {note}")
    return value, elapsed


print("=== 分段计时（与 e2e server.py 同环境）===")
print("\n[1] init_db()")
timed("init_db", lambda: PT.init_db())

print("\n[2] 页面读路径里的 provider / snapshot 调用")
timed("dfc.fetch_market_snapshot_full(max_age=240)",
      lambda: dfc.fetch_market_snapshot_full(max_age=240))
timed("PT._market_state(today, allow_network=False)",
      lambda: PT._market_state(PT._date(), allow_network=False))

print("\n[3] 整个 explain 入口（页面实际调用）")
timed("PT.strategy_allocation_explain()", lambda: PT.strategy_allocation_explain())
print("\n[4] 再调一次（同进程第二次）")
timed("PT.strategy_allocation_explain() #2", lambda: PT.strategy_allocation_explain())

print("\n[5] 独立子进程再调一次（每次请求都是新进程时更贴近 API 进程以外的情况）")
