# -*- coding: utf-8 -*-
"""Round-11 §9/§10/§27 —— 生产库 rebalance 状态归属审计（**只读**）。

用法::

    python work/r11_prod_rebalance_audit.py <data_dir>

判据：
* 只读打开（``mode=ro&immutable=1``），绝不写、绝不拷贝生产库；
* 分别报告 adaptive / paper 两库里 rebalance_scans / rebalance_plans /
  rebalance_cooldown 是否存在及行数；
* 若 adaptive 库里存在**非空** rebalance 数据 ⇒ 立即停下并报 blocker，
  不自行设计迁移。

输出**不含**路径与业务明细（只有表名与计数），可直接贴进 PR body。
"""
from __future__ import annotations

import os
import sqlite3
import sys

TABLES = ("rebalance_scans", "rebalance_plans", "rebalance_cooldown")


def probe(label, path):
    result = {"label": label, "exists": os.path.exists(path), "tables": {}}
    if not result["exists"]:
        return result
    # 只读 + immutable：生产库处于 WAL 模式，只读挂载下 mode=ro 会因无法建
    # -shm/-wal 而报错；immutable=1 明确声明无并发写入。
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=20)
    try:
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()  # 探活
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        for t in TABLES:
            if t in names:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                result["tables"][t] = n
            else:
                result["tables"][t] = None
        # 额外的归属证据：这张库能不能回答「现在持有什么」
        result["has_paper_accounts"] = "paper_accounts" in names
        result["has_paper_position_lots"] = "paper_position_lots" in names
        result["has_paper_cycles"] = "paper_cycles" in names
        if result["has_paper_cycles"]:
            row = conn.execute(
                "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
                " ORDER BY id DESC LIMIT 1").fetchone()
            result["active_cycle"] = row[0] if row else None
        else:
            result["active_cycle"] = None
        if result["has_paper_position_lots"]:
            row = conn.execute(
                "SELECT COUNT(*) FROM paper_position_lots WHERE remaining_qty>0").fetchone()
            result["open_lots"] = row[0]
        else:
            result["open_lots"] = None
    finally:
        conn.close()
    return result


def fmt(v):
    if v is None:
        return "absent"
    return str(v)


def main(argv):
    data_dir = argv[1] if len(argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_cache")
    paper = os.path.join(data_dir, "paper_trading.sqlite3")
    adaptive = os.path.join(data_dir, "adaptive_learning.sqlite3")

    print("=" * 74)
    print("Round-11 §9/§10/§27  rebalance 状态归属审计（只读）")
    print("=" * 74)
    print()

    adaptive_r = probe("adaptive", adaptive)
    paper_r = probe("paper", paper)

    print("── adaptive DB ──")
    if not adaptive_r["exists"]:
        print("  (file absent)")
    else:
        print(f"  paper_accounts present      : {adaptive_r.get('has_paper_accounts')}")
        print(f"  paper_position_lots present : {adaptive_r.get('has_paper_position_lots')}")
        for t in TABLES:
            print(f"  {t:22s}: {fmt(adaptive_r['tables'][t])}")
    print()
    print("── paper DB ──")
    if not paper_r["exists"]:
        print("  (file absent)")
    else:
        print(f"  paper_accounts present      : {paper_r.get('has_paper_accounts')}")
        print(f"  paper_position_lots present : {paper_r.get('has_paper_position_lots')}")
        print(f"  active cycle                : {fmt(paper_r.get('active_cycle'))}")
        print(f"  open lots (remaining_qty>0) : {fmt(paper_r.get('open_lots'))}")
        for t in TABLES:
            print(f"  {t:22s}: {fmt(paper_r['tables'][t])}")
    print()

    # §10：adaptive 库里若有非空 rebalance 数据 ⇒ blocker，不得静默丢弃。
    nonempty = {t: n for t, n in adaptive_r["tables"].items()
                if isinstance(n, int) and n > 0}
    print("── VERDICT ──")
    if nonempty:
        print(f"  BLOCKER: adaptive DB 存在非空 rebalance 数据 {nonempty}")
        print("  不得 DELETE / copy / overwrite；需人工决定迁移策略。")
        return 3
    print("  adaptive DB rebalance 数据：全部不存在或为空 (row count = 0)")
    print("  ⇒ 本 PR 不需要数据迁移。")
    print()
    print("  归属结论：")
    print(f"    paper facts belong to paper DB      : {paper_r.get('has_paper_accounts')}")
    print(f"    adaptive DB can answer current-pos  : {adaptive_r.get('has_paper_position_lots')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
