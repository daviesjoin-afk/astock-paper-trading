# -*- coding: utf-8 -*-
"""受控 A/B：master vs R23 head 的 explain 读路径耗时（同机、同条件、**真正交错**）。

frontend 在两版本间字节相同，所以差异只可能来自 backend。本脚本对**同一个**
无网络条件（不可达代理，≈ CI 的离线环境）测量两个版本的：
  * fetch_market_snapshot_full(max_age=240)
  * strategy_allocation_explain()

交错方式：每一轮**连续**跑 master 与 R23 各一次，并在轮间轮换先后顺序
（AB, BA, AB, BA…）。这样任何随时间漂移的机器负载（后台任务、散热、其他进程）
都会均摊到两个版本上，而块状测量（先跑完 A 再跑完 B）做不到这一点。

每次测量都是独立子进程 + 独立临时数据目录，环境完全一致；脚本会**断言**实际
执行顺序确实是交错的（相邻两次必属不同版本），并把顺序打印出来供审核。

用法：
    python work/r23_round4_ab_timing.py [--rounds N]   # 默认 6 轮（AB/BA 各 3 次）
"""
from __future__ import annotations

import os
import statistics
import subprocess
import sys
import tempfile

MASTER = os.path.join(tempfile.gettempdir(), "r23-baseline-master")
CURRENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(CURRENT, ".venv", "Scripts", "python.exe")

#: 子进程：注入近似 CI 的无网络条件，量两个只读入口的耗时。
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


def measure_once(root):
    """跑一次独立进程，返回 ``（snapshot 秒, explain 秒）``。"""
    out = subprocess.run([PYTHON, "-c", CHILD, root], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=600)
    if out.returncode != 0:
        tail = (out.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"]
        raise RuntimeError(f"child failed for {root}: {tail[0]}")
    lines = [l for l in (out.stdout or "").splitlines() if l.strip()]
    if not lines:
        raise RuntimeError(f"child produced no output for {root}")
    snap, expl = (float(v) for v in lines[-1].split())
    return snap, expl


def build_schedule(rounds):
    """AB/BA 轮换：偶数轮 master 先，奇数轮 R23 先。"""
    schedule = []
    for index in range(rounds):
        pair = ("master", "R23") if index % 2 == 0 else ("R23", "master")
        schedule.append(pair)
    return schedule


def verify_interleaved(schedule):
    """断言调度真的是交错，而不是块状（先 A×N 再 B×N）。

    判定依据是三条性质，缺一不可：

    1. **每一轮都同时包含两个版本** —— 这是交错的核心：同一轮里的两次测量共享
       几乎相同的机器负载，漂移被均摊到两边；
    2. **每轮的首个版本在轮间交替**（AB, BA, AB…）—— 让「谁先跑」这件事也不偏袒
       任何一方；
    3. **同版本连续出现的最大长度 ≤ 2** —— 块状测量会出现长度为 N 的连续段，
       这里必须把它挡住。

    注意：满足 (2) 的 AB/BA 轮换，在**翻转处**必然出现一次相邻的同版本
    （``master,R23 | R23,master``）。那是交错调度的正常形状，不是块状测量，所以
    第 3 条用的是「最大连续长度」而不是「相邻必不同」。
    """
    if len(schedule) < 2:
        return False, "轮数不足（至少 2 轮）"
    for index, pair in enumerate(schedule, start=1):
        if set(pair) != {"master", "R23"}:
            return False, f"第 {index} 轮没有同时包含两个版本：{pair}"
    firsts = [pair[0] for pair in schedule]
    if any(firsts[i] == firsts[i - 1] for i in range(1, len(firsts))):
        return False, f"每轮首个版本没有交替：{firsts}"

    flat = [label for pair in schedule for label in pair]
    longest, run = 1, 1
    for index in range(1, len(flat)):
        run = run + 1 if flat[index] == flat[index - 1] else 1
        longest = max(longest, run)
    if longest > 2:
        return False, f"同版本连续出现 {longest} 次（块状测量，非交错）"
    return True, (f"ok（{len(schedule)} 轮 AB/BA 轮换，每轮两版本各一次，"
                  f"最长连续段 {longest}）")


def main() -> int:
    argv = sys.argv[1:]
    rounds = 6
    if "--rounds" in argv:
        rounds = max(2, int(argv[argv.index("--rounds") + 1]))

    for label, root in (("master", MASTER), ("R23", CURRENT)):
        if not os.path.isdir(os.path.join(root, "backend")):
            print(f"缺少 {label} 工作树：{root}")
            return 2

    schedule = build_schedule(rounds)
    interleaved, why = verify_interleaved(schedule)
    print("=== 受控交错 A/B（不可达代理 ≈ CI 无网络）===")
    print(f"master = {MASTER}")
    print(f"R23    = {CURRENT}")
    print(f"轮数 = {rounds}（AB/BA 轮换）")
    print(f"交错自检：{why}")
    if not interleaved:
        print("VERDICT: 调度不是交错的 —— 证据不成立，拒绝输出结论")
        return 1

    samples = {"master": {"snap": [], "expl": []},
               "R23": {"snap": [], "expl": []}}
    order = []
    print("\n=== 逐次执行（真实顺序）===")
    for index, pair in enumerate(schedule, start=1):
        for label in pair:
            snap, expl = measure_once(MASTER if label == "master" else CURRENT)
            samples[label]["snap"].append(snap)
            samples[label]["expl"].append(expl)
            order.append(label)
            print(f"  round {index}  {label:6}  snapshot={snap:7.3f}s  explain={expl:7.3f}s")

    print(f"\n实际执行序列：{' → '.join(order)}")

    print("\n=== 原始样本 ===")
    for label in ("master", "R23"):
        print(f"  {label:6} snapshot = {[round(v, 3) for v in samples[label]['snap']]}")
        print(f"  {label:6} explain  = {[round(v, 3) for v in samples[label]['expl']]}")

    print("\n=== 中位数 ===")
    med = {}
    for label in ("master", "R23"):
        med[label] = {
            "snap": statistics.median(samples[label]["snap"]),
            "expl": statistics.median(samples[label]["expl"]),
        }
        print(f"  {label:6} snapshot = {med[label]['snap']:8.3f}s   "
              f"explain = {med[label]['expl']:8.3f}s")

    d_snap = med["R23"]["snap"] - med["master"]["snap"]
    d_expl = med["R23"]["expl"] - med["master"]["expl"]
    print(f"\nΔ snapshot (R23 - master) = {d_snap:+.3f}s")
    print(f"Δ explain  (R23 - master) = {d_expl:+.3f}s")

    threshold = 1.5
    verdict = "NO REGRESSION" if abs(d_expl) < threshold else "SLOWER — investigate"
    print(f"\n（判定阈值 ±{threshold}s，为无网络条件下单次代价 ~13.8s 的 ~10%）")
    print(f"VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
