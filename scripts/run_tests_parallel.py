#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分片并行跑后端回归：成本均衡分片 + 每分片独立数据目录。

为什么要它
----------
``python -m unittest discover -s backend`` 单进程要跑十几分钟，瓶颈不在 import
（实测全部 350 个测试模块 import 只要 2.4 秒），而在测试体本身：最贵的 12 个模块
占了总耗时的 37%。把这几块打散到不同进程，墙钟时间就由"最慢的分片"决定。

实测（本机 8 核，约 6900 用例）：

============================  ==========
单进程                        882 s
8 路轮转分片（按模块数均分）   254 s
8 路成本均衡分片（LPT 装箱）   **193 s**
============================  ==========

为什么必须"成本均衡"
--------------------
单个测试模块的成本从 0.06 秒到 56.7 秒，差 900 倍；文件大小只能解释 14% 的方差
（Pearson r=0.373），用例数同样不足以预测（昂贵的是 fixture 重建，不是断言）。
按模块数均分会让最慢分片比最快分片慢 1.7 倍，而**墙钟等于最慢分片**。所以这里用
实测成本做 LPT（Longest Processing Time）装箱，实测能把负载差压到 1.02 倍。

成本档案来自 ``--calibrate``，存在 ``scripts/test_costs.json``。它是**建议值**不是
真理：机器、Python 版本、缓存状态都会让绝对值变化，但相对权重通常稳定。档案缺失
或模块新增时自动退化成轮转分片，不会失败。

为什么每分片要独立数据目录
--------------------------
``data_paths.data_dir()`` 每次调用都读 ``ASTOCK_DATA_DIR``，但 ``paper_trading``
的 ``DB_PATH`` 是 **import 时捕获的模块级常量**，所以环境变量必须在任何项目 import
**之前**设置。本脚本在每个 worker 子进程的最前面就把它指到独立临时目录，因此
350 个测试模块全部被覆盖，不需要逐个改测试文件。

为什么子进程不用管道
--------------------
测试会往 stdout/stderr 打大量日志（一次全量可达数十 MB）。若用 ``subprocess.PIPE``
再逐个 ``communicate()``，先完成的分片会因管道缓冲写满而阻塞在 write 上，整个跑批
退化成串行——实测会从 193 秒劣化到 365 秒。所以这里把子进程输出重定向到**每个分片
自己的日志文件**，结果单独写一份 JSON 文件，父进程只读文件。

用法
----
::

    # 默认：按 CPU 核数分片，成本均衡
    python scripts/run_tests_parallel.py

    # 指定分片数与超时
    python scripts/run_tests_parallel.py --shards 8 --timeout 900

    # 只打印分片计划，不执行（用于检查均衡度）
    python scripts/run_tests_parallel.py --list

    # 重新采集成本档案（串行跑一遍，本机约 15 分钟）
    python scripts/run_tests_parallel.py --calibrate

    # 退回轮转分片（对照用）
    python scripts/run_tests_parallel.py --strategy rotate

退出码：全部通过为 0；任一 error/failure/加载失败/超时为 1；用法或环境错误为 2。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

#: 成本档案文件名（相对 scripts/）。
COST_FILE = "test_costs.json"
#: 成本档案格式版本；不匹配直接忽略，不猜测兼容。
COST_VERSION = 1
#: 单个测试模块的成本下限，避免除零与"零成本"模块被无限堆叠。
MIN_COST = 0.05
#: worker 结果文件的固定名（在分片数据目录的父目录里）。
RESULT_NAME = "shard_result.json"
LOG_NAME = "shard_run.log"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def backend_dir() -> Path:
    return repo_root() / "backend"


def cost_path() -> Path:
    return Path(__file__).resolve().parent / COST_FILE


def discover_modules(pattern: str) -> list[str]:
    """按 unittest discover 的规则列出测试模块名（不带 .py）。"""
    root = backend_dir()
    if not root.is_dir():
        raise SystemExit(f"backend 目录不存在: {root}")
    names = []
    for entry in sorted(root.iterdir()):
        if not entry.is_file() or entry.suffix != ".py":
            continue
        if not entry.name.startswith("test_"):
            continue
        if not entry.match(pattern):
            continue
        names.append(entry.stem)
    if not names:
        raise SystemExit(f"{root} 下没有匹配 {pattern} 的测试模块")
    return names


def load_costs() -> dict[str, float]:
    """读成本档案；缺失、损坏或版本不符时返回空字典（调用方退化成轮转）。"""
    path = cost_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != COST_VERSION:
        return {}
    costs = payload.get("costs")
    if not isinstance(costs, dict):
        return {}
    out: dict[str, float] = {}
    for name, value in costs.items():
        try:
            out[str(name)] = max(MIN_COST, float(value))
        except (TypeError, ValueError):
            continue
    return out


def save_costs(costs: dict[str, float]) -> Path:
    path = cost_path()
    payload = {
        "version": COST_VERSION,
        "kind": "measured-seconds",
        "note": (
            "由 scripts/run_tests_parallel.py --calibrate 生成的分片权重。"
            "绝对值随机器变化，仅用于 LPT 装箱的相对比较。"
        ),
        "costs": {name: round(value, 3) for name, value in sorted(costs.items())},
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def plan_rotate(modules: list[str], shards: int) -> list[list[str]]:
    """轮转分片：按模块数均分。已知不均衡，保留作为对照。"""
    groups: list[list[str]] = [[] for _ in range(shards)]
    for index, name in enumerate(modules):
        groups[index % shards].append(name)
    return groups


def plan_cost(modules: list[str], shards: int, costs: dict[str, float]) -> list[list[str]]:
    """LPT 装箱：成本从大到小，每次放进当前累计成本最小的分片。"""
    groups: list[list[str]] = [[] for _ in range(shards)]
    loads = [0.0] * shards
    # 未登记的模块给一个中性成本，既不抢跑也不沉底。
    default = (sum(costs.values()) / len(costs)) if costs else 1.0
    weighted = sorted(
        ((max(MIN_COST, costs.get(name, default)), name) for name in modules),
        reverse=True,
    )
    for cost, name in weighted:
        target = loads.index(min(loads))
        groups[target].append(name)
        loads[target] += cost
    return groups


def shard_loads(groups: list[list[str]], costs: dict[str, float]) -> list[float]:
    default = (sum(costs.values()) / len(costs)) if costs else 0.0
    return [sum(costs.get(name, default) for name in group) for group in groups]


WORKER_ENV_KEYS = ("ASTOCK_DATA_DIR", "ASTOCK_REPORT_DIR")


def _worker(argv: list[str]) -> int:
    """子进程入口：先隔离数据目录，再 import 项目代码。"""
    parser = argparse.ArgumentParser(prog="run_tests_parallel --worker")
    parser.add_argument("--worker", type=int, required=True)
    parser.add_argument("--modules", required=True, help="逗号分隔的模块名")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--result-file", required=True)
    args = parser.parse_args(argv)

    # 必须在任何项目 import 之前：paper_trading.DB_PATH 是 import 期常量。
    os.environ["ASTOCK_DATA_DIR"] = args.data_dir
    os.environ.setdefault("ASTOCK_REPORT_DIR", os.path.join(args.data_dir, "reports"))

    backend = backend_dir()
    sys.path.insert(0, str(backend))
    import unittest  # noqa: E402  - 延迟到路径就绪后再导入

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    module_names = [n for n in args.modules.split(",") if n]
    load_errors: list[str] = []
    for name in module_names:
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except Exception as exc:  # noqa: BLE001 - 单个模块加载失败不能吞掉整个分片
            load_errors.append(f"{name}: {type(exc).__name__}: {exc}")

    started = time.perf_counter()
    # 测试主动打印的噪声与 runner 的进度都留在本进程的 stdout/stderr，
    # 由父进程重定向到分片日志；结果单独写文件，不经过管道。
    runner = unittest.TextTestRunner(verbosity=0, stream=sys.stderr)
    result = runner.run(suite)
    elapsed = time.perf_counter() - started

    payload = {
        "worker": args.worker,
        "modules": module_names,
        "seconds": round(elapsed, 2),
        "tests": result.testsRun,
        "errors": [f"{case}: {text.splitlines()[-1] if text else ''}"
                   for case, text in result.errors],
        "failures": [f"{case}: {text.splitlines()[-1] if text else ''}"
                     for case, text in result.failures],
        "skipped": len(result.skipped),
        "load_errors": load_errors,
    }
    tmp = args.result_file + ".tmp"
    Path(tmp).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, args.result_file)
    ok = not (result.errors or result.failures or load_errors)
    return 0 if ok else 1


_SINGLE_MODULE_SNIPPET = (
    "import os, sys, unittest\n"
    "sys.path.insert(0, '.')\n"
    "loader = unittest.TestLoader()\n"
    "suite = loader.loadTestsFromName(sys.argv[1])\n"
    "unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, 'w')).run(suite)\n"
)


def _calibrate(modules: list[str]) -> int:
    """串行跑每个模块，采集成本档案（每模块独立进程，贴近并行时的真实成本）。"""
    backend = backend_dir()
    print(f"校准 {len(modules)} 个模块（串行，可能需要十几分钟）...", flush=True)
    costs: dict[str, float] = {}
    workdir = tempfile.mkdtemp(prefix="astock-calibrate-")
    env_base = {k: v for k, v in os.environ.items() if k not in WORKER_ENV_KEYS}
    env_base["PYTHONIOENCODING"] = "utf-8"
    try:
        for index, name in enumerate(modules, 1):
            data_dir = os.path.join(workdir, name)
            os.makedirs(data_dir, exist_ok=True)
            env = dict(env_base)
            env["ASTOCK_DATA_DIR"] = data_dir
            env["ASTOCK_REPORT_DIR"] = os.path.join(data_dir, "reports")
            started = time.perf_counter()
            subprocess.run(
                [sys.executable, "-c", _SINGLE_MODULE_SNIPPET, name],
                cwd=str(backend), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False,
            )
            costs[name] = max(MIN_COST, time.perf_counter() - started)
            if index % 25 == 0 or index == len(modules):
                print(f"  {index}/{len(modules)}  已采集", flush=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    path = save_costs(costs)
    total = sum(costs.values())
    heaviest = sorted(costs.items(), key=lambda kv: kv[1], reverse=True)[:10]
    print(f"\n已写入 {path}")
    print(f"串行总成本 {total:.1f}s，最重的 10 个模块：")
    for name, value in heaviest:
        print(f"  {value:7.2f}s  {name}")
    return 0


def _tail(path: Path, limit: int = 25) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-limit:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_tests_parallel",
        description="成本均衡 + 数据目录隔离的并行后端回归",
    )
    parser.add_argument("--shards", type=int, default=0,
                        help="分片数；0 表示按 CPU 核数（默认）")
    parser.add_argument("--strategy", choices=("cost", "rotate"), default="cost",
                        help="分片策略；cost 需要成本档案，缺失时自动退化为 rotate")
    parser.add_argument("--pattern", default="test_*.py", help="模块名 glob")
    parser.add_argument("--timeout", type=float, default=1800.0,
                        help="单个分片超时秒数（默认 1800）")
    parser.add_argument("--list", action="store_true", help="只打印分片计划")
    parser.add_argument("--calibrate", action="store_true", help="重新采集成本档案")
    parser.add_argument("--keep-data-dirs", action="store_true",
                        help="保留各分片的临时数据目录与日志（排查用）")
    parser.add_argument("--verbose", action="store_true",
                        help="把各分片的测试输出实时透传到终端")
    # 内部 worker 参数（供本脚本自身以子进程方式调用）
    parser.add_argument("--worker", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--modules", default="", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", default="", help=argparse.SUPPRESS)
    parser.add_argument("--result-file", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker is not None:
        return _worker(argv if argv is not None else sys.argv[1:])

    modules = discover_modules(args.pattern)
    if args.calibrate:
        return _calibrate(modules)

    shards = args.shards or (os.cpu_count() or 1)
    shards = max(1, min(shards, len(modules)))

    costs = load_costs()
    strategy = args.strategy
    if strategy == "cost" and not costs:
        print("提示：未找到成本档案，退化为轮转分片。"
              "运行 --calibrate 可生成 scripts/test_costs.json 以启用成本均衡。",
              file=sys.stderr)
        strategy = "rotate"

    groups = (plan_cost(modules, shards, costs) if strategy == "cost"
              else plan_rotate(modules, shards))

    if args.list:
        loads = shard_loads(groups, costs)
        print(f"策略={strategy}  分片={shards}  模块={len(modules)}")
        for index, group in enumerate(groups):
            print(f"  分片 {index}: {len(group):3d} 模块  预估 {loads[index]:7.1f}s")
        if strategy == "cost" and loads:
            print(f"  最大/最小 = {max(loads) / max(min(loads), 0.01):.2f}x")
        return 0

    print(f"分片={shards}  策略={strategy}  模块={len(modules)}  "
          f"Python={sys.version.split()[0]}", flush=True)
    started = time.perf_counter()
    workdir = Path(tempfile.mkdtemp(prefix="astock-parallel-"))
    env_base = {k: v for k, v in os.environ.items() if k not in WORKER_ENV_KEYS}
    env_base["PYTHONDONTWRITEBYTECODE"] = "1"
    env_base["PYTHONIOENCODING"] = "utf-8"

    launched: dict[int, tuple[subprocess.Popen, float, Path, Path]] = {}
    handles = []
    try:
        for index, group in enumerate(groups):
            if not group:
                continue
            data_dir = workdir / f"shard{index}"
            data_dir.mkdir(parents=True, exist_ok=True)
            result_file = workdir / f"shard{index}.result.json"
            log_file = workdir / f"shard{index}.log"
            cmd = [
                sys.executable, str(Path(__file__).resolve()),
                "--worker", str(index),
                "--modules", ",".join(group),
                "--data-dir", str(data_dir),
                "--result-file", str(result_file),
            ]
            if args.verbose:
                # 实时透传：不重定向，用户直接看到测试输出。
                proc = subprocess.Popen(cmd, cwd=str(repo_root()), env=env_base)
            else:
                # 重定向到文件：绝不用管道，避免子进程因缓冲写满而阻塞。
                handle = open(log_file, "w", encoding="utf-8", errors="replace")
                handles.append(handle)
                proc = subprocess.Popen(
                    cmd, cwd=str(repo_root()), env=env_base,
                    stdout=handle, stderr=subprocess.STDOUT,
                )
            launched[index] = (proc, time.perf_counter(), result_file, log_file)

        results: list[dict] = []
        problems: list[str] = []
        for index, (proc, at, result_file, log_file) in sorted(launched.items()):
            remaining = max(1.0, args.timeout - (time.perf_counter() - at))
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                problems.append(f"分片 {index} 超时（>{args.timeout:.0f}s）")
                continue
            if not result_file.is_file():
                tail = _tail(log_file)
                problems.append(
                    f"分片 {index} 没有产出结果文件（退出码 {proc.returncode}）"
                    + ("\n  " + "\n  ".join(tail) if tail else "")
                )
                continue
            try:
                payload = json.loads(result_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                problems.append(f"分片 {index} 结果文件无法解析: {exc}")
                continue
            payload["log_file"] = str(log_file)
            results.append(payload)

        elapsed = time.perf_counter() - started
        total_tests = sum(item["tests"] for item in results)
        slowest = max((item["seconds"] for item in results), default=0.0)

        print(f"\n{'分片':>4}  {'模块':>4}  {'用例':>5}  {'耗时':>9}  状态")
        for item in sorted(results, key=lambda x: x["worker"]):
            bad = item["errors"] or item["failures"] or item["load_errors"]
            print(f"{item['worker']:>4}  {len(item['modules']):>4}  "
                  f"{item['tests']:>5}  {item['seconds']:>8.1f}s  "
                  f"{'FAIL' if bad else 'ok'}")
        if results:
            spread = slowest / max(min(i["seconds"] for i in results), 0.01)
            print(f"\n合计 {total_tests} 个用例，墙钟 {elapsed:.1f}s，"
                  f"最慢分片 {slowest:.1f}s，并行度 {len(results)}，"
                  f"快慢比 {spread:.2f}x")

        for item in sorted(results, key=lambda x: x["worker"]):
            for kind in ("errors", "failures"):
                for case in item[kind]:
                    problems.append(f"[分片 {item['worker']}] {kind[:-1]}: {case}")
            for message in item["load_errors"]:
                problems.append(f"[分片 {item['worker']}] 加载失败: {message}")

        if problems:
            print(f"\n失败 {len(problems)} 项：", file=sys.stderr)
            for line in problems:
                print(f"  {line}", file=sys.stderr)
            if not args.verbose:
                print(f"\n日志目录：{workdir}", file=sys.stderr)
                for index, item in sorted(((i["worker"], i) for i in results)):
                    if item["errors"] or item["failures"] or item["load_errors"]:
                        print(f"  分片 {index}: {item['log_file']}", file=sys.stderr)
            return 1
        print("\n全部通过。")
        return 0
    finally:
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass
        if args.keep_data_dirs:
            print(f"保留数据目录与日志：{workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
