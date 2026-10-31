#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR-6 固定输入账本对照：master 与 PR-6 在同一份输入上的逐字段结果。

做法（固定输入，不依赖网络与行情）：

1. 用 **master** 后端的 ``init_db`` 建一个干净账本；
2. 直接写一份确定性样本：两笔委托（买/卖）、两条成交、一条 lot、一条 signal、
   一条风险决策、一条资金预留、一条租约行、一条 running job；
3. 复制两份，分别用 master 与 PR-6 的 ``init_db`` 初始化；
4. 逐表逐字段比对（按全部列排序），时间戳列单独列出、不计入严格差异。

输出 JSON：``{"tables": {name: {"rows": n, "diff": [...]}}, "strict_equal": bool}``。

用法::

    python scripts/pr6_ledger_compare.py \
        --master-backend <MASTER_CHECKOUT>/backend \
        --pr6-backend <PR6_CHECKOUT>/backend \
        --out .codex-test-tmp/pr6/ledger-diff.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

# 由"当前时刻"写入的列：不算业务事实差异，单独报告。
VOLATILE = {
    "created_at", "updated_at", "verified_at", "applied_at", "acquired_at",
    "heartbeat_at", "expires_at", "started_at", "finished_at", "released_at",
}
SKIP_TABLES = {"schema_version"}


RUNNER = r"""
import os, sys, time
sys.path.insert(0, {backend!r})
os.environ["ASTOCK_DATA_DIR"] = {data_dir!r}
import paper_trading as PT
PT.DB_PATH = {db_path!r}
PT._benchmark_close = lambda: None
PT.init_db()
print("OK")
"""


def run_init(backend, data_dir, db_path):
    script = RUNNER.format(backend=backend, data_dir=data_dir, db_path=db_path)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, encoding="utf-8", env=env)
    if out.returncode != 0 or "OK" not in out.stdout:
        raise RuntimeError(f"init 失败 ({backend}):\n{out.stdout}\n{out.stderr}")


def seed_fixed_rows(db_path):
    """确定性样本：所有主键、时间、金额都写死，两个分支看到完全相同的输入。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        picked = conn.execute(
            """SELECT d.id,d.current_version,d.current_checksum,a.id,a.cycle_id
                 FROM strategy_definitions d
                 JOIN paper_accounts a ON a.source_strategy=d.id
                WHERE d.current_version IS NOT NULL AND a.cycle_id IS NOT NULL
                ORDER BY a.id LIMIT 1""").fetchone()
        stamp, account, cycle = (picked[0], picked[1], picked[2]), picked[3], picked[4]
        conn.execute(
            """INSERT INTO paper_signals(id,account_id,signal_date,intended_date,code,
                   payload,status,created_at,strategy_id,strategy_version,
                   strategy_checksum,cycle_id)
               VALUES(9001,?,?,?,?,?,?,?,?,?,?,?)""",
            (account, "2026-10-30", "2026-10-30", "600001", '{"fixed":true}',
             "filled", "2026-10-30 09:30:00", *stamp, cycle),
        )
        buy_id = conn.execute(
            """INSERT INTO paper_orders(
                   account_id,side,code,name,qty,planned_price,filled_price,amount,fees,
                   status,reason,risk_payload,realized_pnl,created_at,executed_at,
                   execution_status,execution_verified,strategy_id,strategy_version,
                   strategy_checksum,cycle_id,signal_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account, "buy", "600001", "固定标的", 1000, 10.0, 10.0, 10000.0, 6.0,
             "filled", "fixed", "{}", 0.0, "2026-10-30 09:31:00", "2026-10-30 09:31:02",
             "verified", 1, *stamp, cycle, 9001),
        ).lastrowid
        sell_id = conn.execute(
            """INSERT INTO paper_orders(
                   account_id,side,code,name,qty,planned_price,filled_price,amount,fees,
                   status,reason,risk_payload,realized_pnl,created_at,executed_at,
                   execution_status,execution_verified,strategy_id,strategy_version,
                   strategy_checksum,cycle_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account, "sell", "600001", "固定标的", 500, 11.0, 11.0, 5500.0, 3.3,
             "filled", "fixed", "{}", 497.0, "2026-10-30 14:00:00", "2026-10-30 14:00:02",
             "verified", 1, *stamp, cycle),
        ).lastrowid
        for order_id, side, qty, price, fees in (
            (buy_id, "buy", 1000, 10.0, 6.0),
            (sell_id, "sell", 500, 11.0, 3.3),
        ):
            conn.execute(
                """INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,
                       fees,fill_date,quote_at,assumption)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (order_id, account, side, "600001", qty, price, qty * price, fees,
                 "2026-10-30", "2026-10-30 10:00:00", "fixed"),
            )
        conn.execute(
            """INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,
                   remaining_qty,cost,acquired_at,available_date,asset_type,is_t_base,
                   source_order_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cycle, account, "600001", "固定标的", "固定行业", 500, 500, 10.006,
             "2026-10-30", "2026-10-31", "stock_t1", 1, buy_id),
        )
        conn.execute(
            """INSERT INTO paper_risk_decisions(account_id,code,side,decision,reason,
                   payload,created_at,strategy_id,strategy_version,strategy_checksum)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (account, "600001", "buy", "allow", "fixed", '{"fixed":true}',
             "2026-10-30 14:00:00", *stamp),
        )
        conn.execute(
            """INSERT INTO paper_capital_reservations(order_key,account_id,cycle_id,code,
                   side,amount,fees,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (str(buy_id), account, cycle, "600001", "buy", 10000.0, 6.0, "consumed",
             "2026-10-30 09:31:00"),
        )
        conn.execute(
            """INSERT INTO paper_jobs(slot,market_date,status,detail,started_at,owner_key,
                   heartbeat_at,expires_at,fencing_token)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            ("risk", "2026-10-30", "running", '{"fixed":true}', "2026-10-30 13:55:00",
             "fixed-worker", "2099-01-01 00:00:00", "2099-01-01 00:30:00", 7),
        )
        conn.execute(
            """INSERT INTO paper_runtime_locks(lock_key,owner_key,slot,acquired_at,
                   heartbeat_at,expires_at,fencing_token)
               VALUES(?,?,?,?,?,?,?)""",
            ("risk:2026-10-30", "fixed-worker", "risk", "2026-10-30 13:55:00",
             "2099-01-01 00:00:00", "2099-01-01 00:30:00", 7),
        )
        conn.commit()
    finally:
        conn.close()


def dump(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name").fetchall()]
        data, structure = {}, {}
        for table in tables:
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            structure[table] = columns
            stable = sorted(
                (tuple(v for c, v in row.items() if c not in VOLATILE) for row in rows),
                key=repr,
            )
            data[table] = {
                "rows": len(rows),
                "stable": [list(row) for row in stable],
                "volatile": {
                    column: sorted(str(row[column]) for row in rows)
                    for column in columns if column in VOLATILE
                },
            }
        return {"structure": structure, "data": data}
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-backend", required=True)
    parser.add_argument("--pr6-backend", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="pr6-ledger-")
    try:
        base_dir = os.path.join(workdir, "base")
        os.makedirs(base_dir)
        base_db = os.path.join(base_dir, "paper_trading.sqlite3")
        run_init(args.master_backend, base_dir, base_db)
        seed_fixed_rows(base_db)

        results = {}
        for label, backend in (("master", args.master_backend), ("pr6", args.pr6_backend)):
            data_dir = os.path.join(workdir, label)
            os.makedirs(data_dir)
            db_path = os.path.join(data_dir, "paper_trading.sqlite3")
            shutil.copyfile(base_db, db_path)
            run_init(backend, data_dir, db_path)
            results[label] = dump(db_path)

        report = {"tables": {}, "strict_equal": True, "structure_equal": True}
        master_tables = set(results["master"]["data"])
        pr6_tables = set(results["pr6"]["data"])
        extra = sorted(pr6_tables - master_tables)
        missing = sorted(master_tables - pr6_tables)
        report["pr6_only_tables"] = extra
        report["missing_in_pr6"] = missing

        for table in sorted(master_tables & pr6_tables):
            if table in SKIP_TABLES:
                continue
            left, right = results["master"]["data"][table], results["pr6"]["data"][table]
            entry = {"rows_master": left["rows"], "rows_pr6": right["rows"]}
            if results["master"]["structure"][table] != results["pr6"]["structure"][table]:
                entry["columns_master"] = results["master"]["structure"][table]
                entry["columns_pr6"] = results["pr6"]["structure"][table]
                report["structure_equal"] = False
            if left["stable"] != right["stable"]:
                entry["diff"] = "strict"
                report["strict_equal"] = False
            if left["volatile"] != right["volatile"]:
                entry["volatile_diff"] = True
            if entry.get("diff") or entry.get("volatile_diff") or entry.get("columns_pr6"):
                report["tables"][table] = entry

        report["compared_tables"] = len(master_tables & pr6_tables)
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(payload)
        print(payload)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
