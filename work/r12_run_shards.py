# -*- coding: utf-8 -*-
"""Round-12/13 变异矩阵分片启动器。

每个分片在**独立 git worktree** 里运行：``work/tradability_position_mutation_check.py``
从自身位置推导 ``ROOT``，因此每个 worker 有自己的源码树和自己的 lock 文件 ——
"矩阵运行期间不得并行跑测试"这条铁律保护的是"没有两个进程共享同一份源码"，
独立 worktree 满足它。

用法::

    python work/r12_run_shards.py <gen>          # 起 8 个分片（后台）
    python work/r12_run_shards.py <gen> --wait   # 起完后等待并汇总
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARDS = 8

#: Round-12/13 新增/受影响的变异 id（全部要跑）。
IDS = [f"M-RC{i}" for i in range(1, 12)]

#: 同时重跑 Round-11 的 DB 归属变异，确认它们仍然 CAUGHT。
IDS += [f"M-PC{i}" for i in range(8, 14)]


def _env() -> dict:
    return {**os.environ, "PYTHONPATH": "backend", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8"}


def shard_ids(index: int) -> list:
    """第 index 个分片负责的 id（round-robin，负载均衡）。"""
    return [mid for position, mid in enumerate(IDS) if position % SHARDS == index]


def worktree_for(gen: str, index: int) -> Path:
    return ROOT.parent / f"{ROOT.name}-r12-{gen}-s{index}"


def setup_worktrees(gen: str) -> None:
    """为每个分片建一个独立 worktree（detached HEAD 于当前 commit）。"""
    for index in range(SHARDS):
        path = worktree_for(gen, index)
        if path.exists():
            print(f"worktree 已存在，跳过：{path.name}")
            continue
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(path), "HEAD"],
            cwd=str(ROOT), check=True, capture_output=True, text=True,
        )
        print(f"worktree 已创建：{path.name}")


def launch(gen: str) -> list:
    """把每个分片作为**独立受管后台进程**启动（绝不前台跑）。"""
    out_dir = ROOT / "work" / f"r13_matrix_{gen}"
    out_dir.mkdir(parents=True, exist_ok=True)
    procs = []
    for index in range(SHARDS):
        ids = shard_ids(index)
        if not ids:
            continue
        path = worktree_for(gen, index)
        log = out_dir / f"shard{index}.log"
        handle = open(log, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "work/tradability_position_mutation_check.py",
             "--only", ",".join(ids)],
            cwd=str(path), env=_env(), stdout=handle, stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append((index, ids, proc, log, handle))
        print(f"shard{index}: pid={proc.pid} ids={','.join(ids)}")
    return procs


def launch_non_vacuity(gen: str) -> list:
    """非空性同样分片跑（每条要先绿后红，成本与矩阵同量级）。"""
    out_dir = ROOT / "work" / f"r13_nonvac_{gen}"
    out_dir.mkdir(parents=True, exist_ok=True)
    procs = []
    for index in range(SHARDS):
        ids = shard_ids(index)
        if not ids:
            continue
        path = worktree_for(gen, index)
        log = out_dir / f"shard{index}.log"
        handle = open(log, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "work/tradability_position_mutation_check.py",
             "--non-vacuity", "--only", ",".join(ids)],
            cwd=str(path), env=_env(), stdout=handle, stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append((index, ids, proc, log, handle))
        print(f"non-vacuity shard{index}: pid={proc.pid} ids={','.join(ids)}")
    return procs


def wait_all(procs) -> int:
    failures = 0
    for index, ids, proc, log, handle in procs:
        code = proc.wait()
        handle.close()
        print(f"shard{index}: exit={code} ids={','.join(ids)} log={log}")
        if code != 0:
            failures += 1
    return failures


def main() -> int:
    argv = sys.argv[1:]
    gen = argv[0] if argv and not argv[0].startswith("--") else "gen1"
    mode = "nonvac" if "--non-vacuity" in argv else "matrix"

    setup_worktrees(gen)
    procs = launch_non_vacuity(gen) if mode == "nonvac" else launch(gen)
    if "--wait" not in argv:
        print("\n已后台启动；用 --wait 等待并汇总，或读 work/r12_*/ 下的 log。")
        return 0
    return 1 if wait_all(procs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
