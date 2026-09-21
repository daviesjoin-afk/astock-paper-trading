# -*- coding: utf-8 -*-
"""R22 变异矩阵分片启动器（独立 worktree，互不共享源码）。

用法::

    python work/r22_run_shards.py <gen> [--wait] [--shards N]

每个分片在自己的 git worktree 里跑 ``work/r22_mutation_check.py --only <ids>``。
矩阵会**原地改写生产源码**，所以"矩阵运行期间不得并行跑测试"这条铁律
保护的是"没有两个进程共享同一份源码"——独立 worktree 满足该条件。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "work"))
import r22_mutation_check as M  # noqa: E402

IDS = [m["id"] for m in M.MUTATIONS]


def _env() -> dict:
    return {**os.environ, "PYTHONPATH": "backend", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8"}


def worktree_for(gen: str, index: int) -> Path:
    return ROOT.parent / f"{ROOT.name}-r22-{gen}-s{index}"


def setup_worktrees(gen: str, shards: int) -> None:
    for index in range(shards):
        path = worktree_for(gen, index)
        if path.exists():
            print(f"worktree 已存在，跳过：{path.name}")
            continue
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(path), "HEAD"],
            cwd=str(ROOT), check=True, capture_output=True, text=True,
        )
        print(f"worktree 已创建：{path.name}")


def main() -> int:
    argv = sys.argv[1:]
    gen = argv[0] if argv and not argv[0].startswith("--") else "gen1"
    shards = 8
    if "--shards" in argv:
        shards = int(argv[argv.index("--shards") + 1])
    only_all = None
    if "--only" in argv:
        only_all = [x for x in argv[argv.index("--only") + 1].split(",") if x]
    non_vacuity = "--non-vacuity" in argv

    ids = only_all or IDS
    setup_worktrees(gen, shards)
    out_dir = ROOT / "work" / f"r22_{'nonvac' if non_vacuity else 'matrix'}_{gen}"
    out_dir.mkdir(parents=True, exist_ok=True)
    procs = []
    for index in range(shards):
        shard = [mid for position, mid in enumerate(ids) if position % shards == index]
        if not shard:
            continue
        path = worktree_for(gen, index)
        # 把未提交的工作树改动复制进 worktree（它们是待验证的修复本身）。
        for rel in ("backend/paper_portfolio_read_model.py",
                    "backend/test_portfolio_read_model.py",
                    "work/r22_mutation_check.py"):
            src = ROOT / rel
            dst = path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
        log = out_dir / f"shard{index}.log"
        handle = open(log, "w", encoding="utf-8")
        cmd = [sys.executable, "work/r22_mutation_check.py", "--only", ",".join(shard)]
        if non_vacuity:
            cmd.append("--non-vacuity")
        proc = subprocess.Popen(
            cmd, cwd=str(path), env=_env(), stdout=handle, stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append((index, shard, proc, log, handle))
        print(f"shard{index}: pid={proc.pid} n={len(shard)}")
    if "--wait" not in argv:
        print(f"\n已后台启动；读 {out_dir.name}/shard*.log 汇总。")
        return 0
    failures = 0
    for index, shard, proc, log, handle in procs:
        code = proc.wait()
        handle.close()
        print(f"shard{index}: exit={code} n={len(shard)} log={log.name}")
        if code != 0:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
