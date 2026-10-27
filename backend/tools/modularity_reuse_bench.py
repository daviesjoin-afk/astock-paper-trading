#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""同轮输入复用的资源测量（模块化审计 §13.4 的可重跑证据）。

这个脚本回答一个问题：`paper_candidate_inputs.CandidateScanInputs` 让三个消费者
共用一轮输入之后，**净**资源到底变了多少 —— 不是"少调了函数"，而是调用次数、
CPU、墙钟和峰值分配一起看，并且把哈希/深拷贝这些复用自身的开销算进去。

只合成数据、只读代码：不联网、不打开生产库、不写任何业务表。

用法（在仓库根）：

    PYTHONDONTWRITEBYTECODE=1 python -B backend/tools/modularity_reuse_bench.py --securities 5000

输出一份 JSON：两种场景的调用次数、墙钟、进程 CPU、tracemalloc 峰值，以及
结果等价/失效反例的布尔结论。任何一项不成立都会以非零退出码结束。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
import tracemalloc
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)


def _universe(count: int):
    codes = [f"{600000 + index:06d}" for index in range(count)]
    return [
        {
            "code": code,
            "name": f"合成{index:04d}",
            "industry": f"行业{index % 20}",
            "board": "主板",
            "price": 10.0 + (index % 50) * 0.1,
            "mktcap": 1e9 + index,
            "float_cap": 8e8 + index,
            "pe": 12.0 + (index % 30),
            "pb": 1.2 + (index % 5) * 0.1,
        }
        for index, code in enumerate(codes)
    ]


def _finance(rows):
    return {
        "data": {
            str(row["code"]): {
                "report_date": "2026-06-30",
                "annual_net_profit": 5e8,
                "net_profit": 2.5e8,
                "profit_yoy": 12.5,
                "revenue_yoy": 8.0,
                "roe": 11.0,
            }
            for row in rows
        },
        "report_date": "2026-06-30",
    }


class _Counters:
    def __init__(self):
        self.calls = {"universe": 0, "finance": 0, "disclosure": 0, "fundamentals": 0}

    def loader(self, name, value):
        def load(*args, **kwargs):
            self.calls[name] += 1
            return copy.deepcopy(value)
        return load


def _run_round(count, *, share, rows, finance, asof, counters):
    """One scan round; ``share`` decides whether the three consumers share inputs."""
    import paper_candidate_inputs as PCI

    shared = PCI.CandidateScanInputs(asof) if share else None
    frames = []
    for _consumer in range(3):
        inputs = shared or PCI.CandidateScanInputs(asof)
        universe = inputs.universe(counters.loader("universe", rows))
        ledger = inputs.finance(counters.loader("finance", finance))
        frames.append(inputs.fundamentals(universe, ledger, asof=asof))
    return frames


def _measure(*, share, rows, finance, asof, counters):
    tracemalloc.start()
    wall = time.perf_counter()
    cpu = time.process_time()
    frames = _run_round(len(rows), share=share, rows=rows, finance=finance,
                        asof=asof, counters=counters)
    elapsed = time.perf_counter() - wall
    used = time.process_time() - cpu
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"frames": frames, "wall_s": round(elapsed, 4),
            "cpu_s": round(used, 4), "peak_mib": round(peak / 1024 / 1024, 3)}


def _fingerprint(frame):
    payload = frame.reset_index().to_json(orient="split", double_precision=10)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--securities", type=int, default=2000)
    args = parser.parse_args(argv)

    import datetime as dt

    import factors as F

    rows = _universe(args.securities)
    finance = _finance(rows)
    asof = dt.date(2026, 10, 22)

    real_compute = F.compute_fundamental_factors

    def counting_compute(*call_args, **call_kwargs):
        counters.calls["fundamentals"] += 1
        return real_compute(*call_args, **call_kwargs)

    results = {}
    for name, share in (("separate_consumers", False), ("shared_round", True)):
        counters = _Counters()
        with mock.patch.object(F, "compute_fundamental_factors", counting_compute):
            results[name] = _measure(share=share, rows=rows, finance=finance,
                                     asof=asof, counters=counters)
            results[name]["calls"] = dict(counters.calls)

    separate, shared = results["separate_consumers"], results["shared_round"]
    equivalence = {
        "same_frame_count": len(separate["frames"]) == len(shared["frames"]) == 3,
        "same_fingerprint": len({_fingerprint(frame) for frame in
                                 (*separate["frames"], *shared["frames"])}) == 1,
        "source_loads_once": shared["calls"]["universe"] == 1 and shared["calls"]["finance"] == 1,
        "expensive_compute_once": shared["calls"]["fundamentals"] == 1,
        "separate_path_repeated": separate["calls"]["universe"] == 3
        and separate["calls"]["fundamentals"] == 3,
    }

    # 失效反例：不同 as-of / 空读不得命中复用键。
    import paper_candidate_inputs as PCI
    counters = _Counters()
    inputs = PCI.CandidateScanInputs(asof)
    inputs.universe(counters.loader("universe", rows))
    inputs.universe(counters.loader("universe", rows))
    empty_first = counters.calls["universe"]
    PCI.CandidateScanInputs(asof).universe(lambda: [])
    recoverable = PCI.CandidateScanInputs(asof)
    recoverable.universe(lambda: [])
    recoverable.universe(counters.loader("universe", rows))
    invalidations = {
        "repeat_read_reuses_snapshot": empty_first == 1,
        "empty_read_is_not_cached": counters.calls["universe"] == 2,
    }

    # 消费者改写自己的工作副本不得污染下一个消费者。
    isolate = PCI.CandidateScanInputs(asof)
    first = isolate.universe(counters.loader("universe", rows))
    first[0]["price"] = -1.0
    second = isolate.universe(counters.loader("universe", rows))
    invalidations["consumer_mutation_isolated"] = second[0]["price"] != -1.0

    ok = all(equivalence.values()) and all(invalidations.values())
    payload = {
        "securities": args.securities,
        "consumers": 3,
        "separate_consumers": {k: separate[k] for k in ("calls", "wall_s", "cpu_s", "peak_mib")},
        "shared_round": {k: shared[k] for k in ("calls", "wall_s", "cpu_s", "peak_mib")},
        "delta": {
            "wall_saved_s": round(separate["wall_s"] - shared["wall_s"], 4),
            "cpu_saved_s": round(separate["cpu_s"] - shared["cpu_s"], 4),
            "peak_saved_mib": round(separate["peak_mib"] - shared["peak_mib"], 3),
        },
        "equivalence": equivalence,
        "invalidations": invalidations,
        "ok": ok,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
