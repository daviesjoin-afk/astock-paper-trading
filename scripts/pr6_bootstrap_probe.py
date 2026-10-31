#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR-6 初始化生命周期测量探针（同一台机器、同一份固定输入）。

对三种库各跑同一批 ``paper_trading.init_db()``，输出 JSON：

* ``empty``   —— 全新空目录的第一次初始化（冷路径）；
* ``legacy``  —— 由 **master** 代码生成的账本（升级前形态）上的初始化；
* ``current`` —— 同一个库在初始化完成之后的重复初始化（日常启动）。

测量项（全部来自进程内观测，不靠外部 profiling）：

* ``duration_ms``     各次 ``init_db`` 墙钟时间（取 P50 / P95）；
* ``acquire_ms``      开连接 + ``BEGIN IMMEDIATE`` 的耗时（写锁等待在这段里）；
* ``statements``      一次初始化实际执行的 SQL 条数（trace callback 统计）；
* ``ddl``             其中 CREATE / ALTER / DROP 的条数；
* ``schema_checks``   ``PRAGMA table_info`` / ``sqlite_schema`` 读结构次数；
* ``csv_reads``       ``_benchmark_close`` 被调用次数（基准 K 线读取点）。

用法::

    python scripts/pr6_bootstrap_probe.py --label master --out /tmp/master.json
    python scripts/pr6_bootstrap_probe.py --label pr6 --legacy-db /tmp/legacy.sqlite3 \
        --out /tmp/pr6.json

``--legacy-db`` 指向 master 生成的库；脚本总是先复制一份再操作，不改原件。
测量限制写在输出里（本机、单进程、SQLite 单写者、测试用的空行情缓存）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(REPO_ROOT, "backend")


def _p95(values):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def _stats(values):
    return {
        "n": len(values),
        "p50": round(statistics.median(values), 2) if values else None,
        "p95": round(_p95(values), 2) if values else None,
        "min": round(min(values), 2) if values else None,
        "max": round(max(values), 2) if values else None,
    }


class Recorder:
    """统计一条连接上真正执行的 SQL，并给它们分类。"""

    DEF_CALLBACK = None

    def __init__(self):
        self.statements = 0
        self.ddl = 0
        self.schema_checks = 0

    def __call__(self, sql):
        text = str(sql or "").strip()
        if not text:
            return
        self.statements += 1
        head = text.split(None, 1)[0].upper()
        if head in {"CREATE", "ALTER", "DROP"}:
            self.ddl += 1
        elif head == "PRAGMA" and "table_info" in text.lower():
            self.schema_checks += 1
        elif "sqlite_schema" in text.lower() or "sqlite_master" in text.lower():
            self.schema_checks += 1


class CallCounter:
    """可调用计数器（trace 之外再数 checkpoint 次数，避免闭包捕获循环变量）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(1)


def _traced_db_factory(original, recorder, acquire_times):
    @contextmanager
    def traced(*args, **kwargs):
        started = time.perf_counter()
        with original(*args, **kwargs) as conn:
            acquire_times.append((time.perf_counter() - started) * 1000.0)
            conn.set_trace_callback(recorder)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    traced.__wrapped__ = original
    return traced


def _measure(paper_trading, paper_storage, db_path, runs, *, reader=None):
    """在同一个库上跑 ``runs`` 次初始化，返回耗时、SQL 与 checkpoint 统计。"""
    paper_trading.DB_PATH = db_path
    durations = []
    acquires = []
    checkpoints = []
    last = {}
    for _ in range(runs):
        recorder = Recorder()
        acquired = []
        counter = CallCounter()
        original = paper_storage.db
        with mock.patch.object(
            paper_storage, "db", _traced_db_factory(original, recorder, acquired)
        ), mock.patch.object(
            paper_storage, "wal_checkpoint", side_effect=counter
        ):
            started = time.perf_counter()
            paper_trading.init_db()
            durations.append((time.perf_counter() - started) * 1000.0)
        acquires.extend(acquired)
        checkpoints.append(len(counter.calls))
        last = {
            "statements": recorder.statements,
            "ddl": recorder.ddl,
            "schema_checks": recorder.schema_checks,
        }
    return {
        "duration_ms": _stats(durations),
        "acquire_ms": _stats(acquires),
        "wal_checkpoint_calls_per_run": checkpoints,
        "last_run": last,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--runs", type=int, default=15)
    parser.add_argument("--legacy-db", default=None)
    parser.add_argument("--keep-legacy-db", default=None,
                        help="空库场景结束后把生成的库复制到这里（供另一个分支当旧库用）")
    parser.add_argument("--backend", default=None, help="被测分支的 backend 目录（默认本仓库）")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix=f"pr6-probe-{args.label}-")
    os.environ["ASTOCK_DATA_DIR"] = workdir
    backend_dir = os.path.abspath(args.backend) if args.backend else BACKEND_DIR
    sys.path.insert(0, backend_dir)

    import paper_storage as PST  # noqa: E402
    import paper_schema_migrations as PSM  # noqa: E402
    import paper_trading as PT  # noqa: E402

    csv_reads = {"count": 0}
    real_benchmark = PT._benchmark_close

    def counting_benchmark():
        csv_reads["count"] += 1
        return real_benchmark()

    PT._benchmark_close = counting_benchmark

    result = {
        "label": args.label,
        "repo": os.path.dirname(backend_dir),
        "python": sys.version.split()[0],
        "runs": args.runs,
        "environment": {
            "note": "本机单进程、SQLite 单写者、空行情缓存，仅作同机对照；不代表生产耗时",
            "data_dir": "临时目录（每场景独立）",
            "db_path": "临时目录/paper_trading.sqlite3",
        },
        "scenarios": {},
    }

    # ---- empty：全新空库的第一次初始化 ----
    empty_dir = os.path.join(workdir, "empty")
    os.makedirs(empty_dir, exist_ok=True)
    empty_db = os.path.join(empty_dir, "paper_trading.sqlite3")
    csv_reads["count"] = 0
    result["scenarios"]["empty"] = _measure(PT, PST, empty_db, 1)
    result["scenarios"]["empty"]["csv_reads"] = csv_reads["count"]

    # ---- current：同一个库上的重复初始化（日常启动）----
    csv_reads["count"] = 0
    current = _measure(PT, PST, empty_db, args.runs)
    current["csv_reads"] = csv_reads["count"]
    with PST.paper_db(empty_db) as conn:
        current["schema_objects"] = conn.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        reader = getattr(PSM, "read_bootstrap_receipt", None)
        receipt = reader(conn) if reader else None
        current["receipt"] = None if receipt is None else receipt["contract_version"]
    result["scenarios"]["current"] = current

    # ---- contention：verified 初始化期间一直有一个只读事务开着 ----
    reader_conn = sqlite3.connect(empty_db, timeout=30)
    try:
        reader_conn.execute("BEGIN")
        reader_conn.execute("SELECT count(*) FROM paper_orders").fetchone()
        result["scenarios"]["reader_contention"] = _measure(PT, PST, empty_db, args.runs)
    finally:
        reader_conn.rollback()
        reader_conn.close()

    # ---- legacy：master 生成的旧库 ----
    if args.legacy_db:
        legacy_dir = os.path.join(workdir, "legacy")
        os.makedirs(legacy_dir, exist_ok=True)
        legacy_db = os.path.join(legacy_dir, "paper_trading.sqlite3")
        shutil.copyfile(args.legacy_db, legacy_db)
        csv_reads["count"] = 0
        result["scenarios"]["legacy"] = _measure(PT, PST, legacy_db, args.runs)
        result["scenarios"]["legacy"]["csv_reads"] = csv_reads["count"]

    if args.keep_legacy_db:
        shutil.copyfile(empty_db, args.keep_legacy_db)

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload)
    print(payload)
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
