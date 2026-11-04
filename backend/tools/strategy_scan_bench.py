# -*- coding: utf-8 -*-
"""Measure the strategy scan path on synthetic factors (A/B across revisions).

The production `close` slot spends almost all of its time in `signal_generation`,
i.e. `S.run_strategy` over the full candidate table for each paper strategy. This
script times exactly that call for the three formal strategies on a synthetic
table, so the same code path can be compared before/after a refactor without
touching a live ledger, a provider or a database.

It deliberately does **not** claim to reproduce end-to-end slot latency: no
network, no SQLite, no per-strategy filtering. It answers one question — did the
refactor make strategy scoring itself slower?

Usage (from the repository root of whichever revision):

    python -B backend/tools/strategy_scan_bench.py --codes 3000 --repeats 3
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import tracemalloc

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

SELECTORS = ("first_board_relay", "pullback_reversal", "reported_profit_breakout")


def synthetic_table(codes: int):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(20261028)
    index = [f"{600000 + i:06d}" for i in range(codes)]
    n = len(index)
    close = 10 + rng.random(n) * 40
    frame = pd.DataFrame({
        "code": index,
        "name": [f"合成{i:04d}" for i in range(n)],
        "industry": [f"行业{i % 20}" for i in range(n)],
        "close": close,
        "open": close * (1 + rng.normal(0, 0.01, n)),
        "high": close * (1 + abs(rng.normal(0, 0.015, n))),
        "low": close * (1 - abs(rng.normal(0, 0.015, n))),
        "pct": rng.normal(0.4, 2.5, n),
        "amount": rng.random(n) * 8e8,
        "turnover": rng.random(n) * 8,
        "volume": rng.random(n) * 4e7,
        "pe": 10 + rng.random(n) * 40,
        "pb": 1 + rng.random(n) * 6,
        "roe": 4 + rng.random(n) * 20,
        "rev_yoy": rng.normal(8, 20, n),
        "profit_yoy": rng.normal(10, 30, n),
        "net_profit": rng.random(n) * 4e8,
        "annual_net_profit": rng.random(n) * 1.5e9,
        "mktcap": 1e9 + rng.random(n) * 9e10,
        "float_cap": 8e8 + rng.random(n) * 7e10,
        "main_pct": rng.normal(0, 1.2, n),
        "main_net": rng.normal(0, 4e7, n),
        "super_net_raw": rng.normal(0, 2e7, n),
        "mom_short": rng.normal(0, 3, n),
        "mom": rng.normal(0, 6, n),
        "vol_ratio": 0.5 + rng.random(n) * 2,
        "sector_heat_score": rng.random(n) * 100,
        "rsi": rng.random(n) * 100,
        "ma5": close * (1 + rng.normal(0, 0.01, n)),
        "ma10": close * (1 + rng.normal(0, 0.015, n)),
        "ma20": close * (1 + rng.normal(0, 0.02, n)),
        "ma60": close * (1 + rng.normal(0, 0.03, n)),
        "boll_mid": close * (1 + rng.normal(0, 0.01, n)),
        "report_date": "2026-06-30",
        "annual_report_date": "2025-12-31",
    })
    frame.index = index
    return frame


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", type=int, default=3000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)

    import strategies as S

    table = synthetic_table(args.codes)
    revision = os.environ.get("ASTOCK_REVISION") or "unknown"
    results = {}
    tracemalloc.start()
    for name in SELECTORS:
        timings = []
        picks = None
        for _ in range(args.repeats):
            start = time.perf_counter()
            result = S.run_strategy(name, table, topn=10, auto_news=False)
            timings.append(time.perf_counter() - start)
            picks = len((result or {}).get("picks") or [])
        results[name] = {
            "median_seconds": round(statistics.median(timings), 4),
            "min_seconds": round(min(timings), 4),
            "picks": picks,
        }
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    total = round(sum(item["median_seconds"] for item in results.values()), 4)
    print(json.dumps({
        "revision": revision,
        "codes": args.codes,
        "repeats": args.repeats,
        "strategies": results,
        "total_median_seconds": total,
        "peak_mib": round(peak / 1024 / 1024, 2),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
