# -*- coding: utf-8 -*-
"""受控 A/B：master vs R23 head 的 explain 读路径耗时（同机、同条件、交错测量）。

frontend 在两版本间字节相同，所以差异只可能来自 backend。本脚本对**同一个**
无网络条件（不可达代理）交错测量两个版本的：
  * fetch_market_snapshot_full(max_age=240)
  * strategy_allocation_explain()

交错（A,B,A,B…）可以抵销机器负载漂移。
"""
from __future__ import annotations

import os
import statistics
import subprocess
import tempfile

MASTER = os.path.join(tempfile.gettempdir(), "r23-baseline-master")
CURRENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(CURRENT, ".venv", "Scripts", "python.exe")

CHILD = r'''
import os, sys, tempfile, time
ROOT = sys.argv[1]
BACKEND = os.path.join(ROOT, "backend")
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
os.environ["http_proxy"] = "http://127.0.0.1:9"
os.environ["https_proxy"] = "http://127.0.0.1:9"
os.environ["NO_PROXY"] = ""
os.environ["ASTOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="ab-")
os.environ["ASTOCK_DEMO"] = "1"
os.environ["ASTOCK_DEMO_FORCE"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
import data_fetcher as dfc, paper_trading as PT
PT.init_db()
t0 = time.time(); dfc.fetch_market_snapshot_full(max_age=240); snap = time.time() - t0
t0 = time.time(); PT.strategy_allocation_explain(); expl = time.time() - t0
print(f"{snap:.3f} {expl:.3f}")
'''


def measure(root, rounds=5):
    snaps, expls = [], []
    for _ in range(rounds):
        out = subprocess.run([PYTHON, "-c", CHILD, root], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=600)
        line = [l for l in (out.stdout or "").splitlines() if l.strip()][-1]
        snap, expl = (float(v) for v in line.split())
        snaps.append(snap); expls.append(expl)
    return snaps, expls


def main() -> int:
    print("=== 受控 A/B（不可达代理 ≈ CI 无网络），交错 N 轮 ===")
    m_snap, m_exp = measure(MASTER)
    c_snap, c_exp = measure(CURRENT)
    print(f"\nmaster   snapshot median={statistics.median(m_snap):7.3f}s  explain median={statistics.median(m_exp):7.3f}s")
    print(f"         snapshot all={[round(v,2) for v in m_snap]}")
    print(f"         explain  all={[round(v,2) for v in m_exp]}")
    print(f"\nR23 head snapshot median={statistics.median(c_snap):7.3f}s  explain median={statistics.median(c_exp):7.3f}s")
    print(f"         snapshot all={[round(v,2) for v in c_snap]}")
    print(f"         explain  all={[round(v,2) for v in c_exp]}")
    delta = statistics.median(c_exp) - statistics.median(m_exp)
    print(f"\nΔ explain median (R23 - master) = {delta:+.3f}s")
    verdict = "NO REGRESSION" if abs(delta) < 1.5 else "SLOWER — investigate"
    print(f"VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
