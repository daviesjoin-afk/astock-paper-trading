# -*- coding: utf-8 -*-
"""Round-11 §2 —— before-fix reproduction：rebalance endpoint 使用错误数据库。

用法::

    python work/r11_before_fix_repro.py

本脚本**只读地**回答一个问题：在 exact head ``c48a80ca`` 上，
``api_adaptive.run_rebalance_scan`` 到底在哪个 SQLite 文件上跑？

做法（刻意不使用任何 mock 掉数据库的东西）：

1. 建两个**物理分离**的临时 SQLite：
   - ``paper.sqlite3``    —— 用真实 ``paper_trading.init_db()`` 生成（有 paper_accounts /
     paper_cycles / paper_position_lots）；
   - ``adaptive.sqlite3`` —— 用真实 ``adaptive._connect()`` 生成（只有 adaptive 表）。
2. 把 ``adaptive.DB_PATH`` / ``adaptive.PAPER_DB_PATH`` 分别指向上面两个文件。
3. patch 行情来源，禁止真实网络。
4. 直接调用 ``api_adaptive.run_rebalance_scan(confirmed=True)``。
5. 打印实际观察到的异常 / 成功，并断言它**确实**是「在 adaptive 库上找 paper 表」。

判定：若出现 ``no such table: paper_accounts``（或等价证据），
则 before-fix reproduction = PASS（缺陷已复现）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import traceback
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)

import adaptive_engine as AE  # noqa: E402
import api_adaptive as API  # noqa: E402
import paper_trading as PT  # noqa: E402


def _table_names(path):
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def main():
    tmp = tempfile.mkdtemp(prefix="r11_repro_")
    paper_path = os.path.join(tmp, "paper.sqlite3")
    adaptive_path = os.path.join(tmp, "adaptive.sqlite3")

    print("=" * 78)
    print("Round-11 §2  before-fix reproduction")
    print("=" * 78)
    print(f"paper.sqlite3    = {paper_path}")
    print(f"adaptive.sqlite3 = {adaptive_path}")
    print(f"same file? {os.path.realpath(paper_path) == os.path.realpath(adaptive_path)}")
    print()

    # ── 1. paper DB：真实生产 schema ────────────────────────────────────────
    with mock.patch.object(PT, "DB_PATH", paper_path), \
            mock.patch.object(PT, "_benchmark_close", return_value=None), \
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True):
        PT.init_db()
    paper_tables = _table_names(paper_path)

    # ── 2. adaptive DB：真实 adaptive schema ────────────────────────────────
    with mock.patch.object(AE, "DB_PATH", adaptive_path), \
            mock.patch.object(AE, "CACHE_DIR", tmp):
        with AE._connect():
            pass
    adaptive_tables = _table_names(adaptive_path)

    print("── paper.sqlite3 关键表 ──")
    for t in ("paper_accounts", "paper_cycles", "paper_position_lots", "paper_positions",
              "paper_orders", "paper_signals", "risk_log",
              "rebalance_scans", "rebalance_plans", "rebalance_cooldown"):
        print(f"  {t:24s} {'YES' if t in paper_tables else 'no'}")
    print()
    print("── adaptive.sqlite3 关键表 ──")
    for t in ("paper_accounts", "paper_cycles", "paper_position_lots", "paper_positions",
              "paper_orders", "paper_signals", "risk_log",
              "rebalance_scans", "rebalance_plans", "rebalance_cooldown"):
        print(f"  {t:24s} {'YES' if t in adaptive_tables else 'no'}")
    print()

    # ── 3. 接线：两个库物理分离，并注入一个 running account + active cycle ──
    #     paper DB 里放一个真实 running 账户，这样只要 endpoint 读对了库，
    #     它就能看到账户并继续往下走（而不是因为「库是空的」而误判成功）。
    conn = sqlite3.connect(paper_path)
    conn.row_factory = sqlite3.Row
    conn.execute("UPDATE paper_accounts SET status='running' WHERE id='tq_breakout'")
    conn.commit()
    running = conn.execute(
        "SELECT id, cycle_id FROM paper_accounts WHERE status='running'"
    ).fetchall()
    print(f"paper DB running accounts = {[(r['id'], r['cycle_id']) for r in running]}")
    active_cycle = conn.execute(
        "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    print(f"paper DB active cycle     = {active_cycle[0] if active_cycle else None}")
    conn.close()
    print()

    quotes = {"600519": {"code": "600519", "price": 10.0, "pct": 0.0, "super_net": 0.0}}

    observed = None
    with mock.patch.object(AE, "DB_PATH", adaptive_path), \
            mock.patch.object(AE, "PAPER_DB_PATH", paper_path), \
            mock.patch.object(AE, "CACHE_DIR", tmp), \
            mock.patch.object(API, "_fetch_rebalance_quotes",
                              return_value=(quotes, {"source": "fixture"})):
        try:
            result = API.run_rebalance_scan(confirmed=True)
            observed = ("OK", result)
        except Exception as exc:  # noqa: BLE001 - 复现脚本要抓到原始异常
            observed = ("RAISED", exc)

    print("── 观察到的 endpoint 行为 ──")
    kind, payload = observed
    if kind == "OK":
        print(f"  returned OK: {str(payload)[:300]}")
    else:
        print(f"  RAISED {type(payload).__name__}: {payload}")
        cause = payload.__cause__ or payload.__context__
        if cause is not None:
            print(f"  cause: {type(cause).__name__}: {cause}")
        print()
        print("  traceback:")
        traceback.print_exception(type(payload), payload, payload.__traceback__)
    print()

    # ── 4. 判定 ────────────────────────────────────────────────────────────
    #    读的是哪个库，用「谁被创建了 rebalance 表」来独立佐证。
    paper_after = _table_names(paper_path)
    adaptive_after = _table_names(adaptive_path)
    print("── 调用后 rebalance_* 表出现在哪个库 ──")
    for label, tables in (("paper.sqlite3", paper_after), ("adaptive.sqlite3", adaptive_after)):
        present = [t for t in ("rebalance_scans", "rebalance_plans", "rebalance_cooldown")
                   if t in tables]
        print(f"  {label:20s} {present}")
    print()

    text = f"{payload}" if kind == "RAISED" else f"{payload}"
    cause = payload.__cause__ if kind == "RAISED" else None
    cause_text = f"{cause}" if cause is not None else ""
    has_missing_paper_table = (
        "no such table: paper_accounts" in text
        or "no such table: paper_accounts" in cause_text
        or "no such table: paper_cycles" in text
        or "no such table: paper_cycles" in cause_text
    )
    # adaptive 库被写入了 rebalance schema ⇒ 证明 endpoint 在错误的库上工作
    adaptive_got_rebalance = any(
        t in adaptive_after for t in ("rebalance_scans", "rebalance_plans", "rebalance_cooldown")
    )
    paper_got_rebalance = any(
        t in paper_after for t in ("rebalance_scans", "rebalance_plans", "rebalance_cooldown")
    )

    print("── VERDICT ──")
    print(f"  no such table: paper_* in error        : {has_missing_paper_table}")
    print(f"  rebalance schema created in adaptive DB: {adaptive_got_rebalance}")
    print(f"  rebalance schema created in paper DB   : {paper_got_rebalance}")

    if has_missing_paper_table or adaptive_got_rebalance:
        print()
        print("  BEFORE-FIX REPRODUCTION: PASS")
        print("  → rebalance endpoint 确实在 adaptive_learning.sqlite3 上执行，")
        print("    而它读取的是 paper ledger 事实。")
        return 0
    if kind == "OK" and paper_got_rebalance and not adaptive_got_rebalance:
        print()
        print("  BEFORE-FIX REPRODUCTION: FAIL (defect not reproduced)")
        print("  → endpoint 已经在 paper DB 上运行。")
        return 1
    print()
    print("  BEFORE-FIX REPRODUCTION: INCONCLUSIVE")
    print(f"  → 未观察到预期失败形态；原始结果：{kind}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
