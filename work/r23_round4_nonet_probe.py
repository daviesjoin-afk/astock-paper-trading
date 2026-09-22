# -*- coding: utf-8 -*-
"""模拟 CI 的**无网络**条件，测量只读 explain 路径的耗时。

CI 跑在 ``--network none`` 容器里：provider 调用不会快速失败，而是走
连接超时/重试。本地有网（走 Clash 代理）所以 _fetch_clist 0.5s 就返回。
把代理指向一个不可达端口可以近似 CI 的"连接失败/超时"行为。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
data_dir = tempfile.mkdtemp(prefix="r23-nonet-")
# 指向一个不可达代理 → 近似 CI 无网络时的连接失败行为
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
os.environ["http_proxy"] = "http://127.0.0.1:9"
os.environ["https_proxy"] = "http://127.0.0.1:9"
os.environ["NO_PROXY"] = ""
os.environ["ASTOCK_DATA_DIR"] = data_dir
os.environ["ASTOCK_DEMO"] = "1"
os.environ["ASTOCK_DEMO_FORCE"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import data_fetcher as dfc
import paper_trading as PT


def timed(label, fn, n=2):
    for i in range(n):
        t0 = time.time()
        try:
            fn()
            note = ""
        except Exception as exc:
            note = f"{type(exc).__name__}"
        print(f"  run{i+1} {time.time()-t0:8.3f}s  {label} {note}")


print("=== 代理不可达（近似 CI 无网络）===")
PT.init_db()
timed("fetch_market_snapshot_full(max_age=240)",
      lambda: dfc.fetch_market_snapshot_full(max_age=240))
print()
timed("strategy_allocation_explain()", lambda: PT.strategy_allocation_explain())
