# -*- coding: utf-8 -*-
"""R23 before-fix 复现：account attachment provenance 必须 fail closed。

规格（PR #180 最后一轮人工审核 P1）：

    cycle existed by D  !=  account belonged to cycle by D

场景 A：cycle 在 asof 之前创建，`paper_accounts` 当前指向该 cycle，
`paper_parameter_versions` 表存在但没有匹配的 attachment/version 行，
且没有 account-specific bounded activity
=> account initial capital 与 cash 都必须是 UNKNOWN。

用法::

    python work/r23_before_fix_repro.py [--source <module path>]

不传 ``--source`` 时用仓库中的当前版本；传 ``HEAD:...`` 形式不可用时
用 ``--head`` 从 git HEAD 取未修复版本。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

ACCOUNT = "r23-acct"          # 独立于既有夹具，避免依赖生产常量
DAY = dt.date(2026, 9, 20)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def build_fixture(conn: sqlite3.Connection) -> tuple[int, str]:
    """场景 A 的最小 schema：attachment 证据缺失，cycle 本身早于 asof。"""
    conn.executescript(
        """
        CREATE TABLE paper_cycles(
            id INTEGER PRIMARY KEY, cycle_key TEXT, status TEXT, capital REAL,
            created_at TEXT, updated_at TEXT, started_at TEXT, ended_at TEXT);
        CREATE TABLE paper_accounts(
            id TEXT PRIMARY KEY, initial_cash REAL, cycle_id INTEGER);
        CREATE TABLE paper_parameter_versions(
            cycle_id INTEGER, account_id TEXT, version TEXT, style TEXT,
            params TEXT, reason TEXT, effective_date TEXT, created_at TEXT);
        CREATE TABLE paper_position_lots(
            id INTEGER PRIMARY KEY, cycle_id INTEGER, account_id TEXT, code TEXT,
            qty INTEGER, remaining_qty INTEGER, acquired_at TEXT, available_date TEXT,
            asset_type TEXT, cost REAL, source_order_id INTEGER);
        CREATE TABLE paper_fills(
            id INTEGER PRIMARY KEY, order_id INTEGER, account_id TEXT, side TEXT,
            code TEXT, qty REAL, price REAL, amount REAL, fees REAL, fill_date TEXT);
        CREATE TABLE paper_orders(
            id INTEGER PRIMARY KEY, cycle_id INTEGER, account_id TEXT, side TEXT,
            status TEXT, code TEXT, qty REAL, executed_at TEXT, realized_pnl REAL,
            execution_status TEXT, execution_verified INTEGER, amount REAL, fees REAL);
        """
    )
    cycle_id = int(conn.execute(
        "INSERT INTO paper_cycles(id,cycle_key,status,capital,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?)",
        (7, "r23-cycle", "running", 100000.0,
         f"{DAY.isoformat()} 09:00:00", f"{DAY.isoformat()} 09:00:00"),
    ).lastrowid)
    conn.execute("INSERT INTO paper_accounts VALUES(?,?,?)",
                 (ACCOUNT, 100000.0, cycle_id))
    conn.commit()
    return cycle_id, ACCOUNT


def probe(module, conn: sqlite3.Connection) -> dict:
    context = module.PortfolioReadContext(7, DAY)
    initial = module._cycle_initial(conn, context, account_id=ACCOUNT)
    cash = module.cash(conn, context, account_id=ACCOUNT)
    attached = module._account_attached_by(conn, context, ACCOUNT)
    return {"attached": attached, "initial": initial, "cash": cash}


def main() -> int:
    argv = sys.argv[1:]
    use_head = "--head" in argv
    if use_head:
        raw = subprocess.run(
            ["git", "show", "HEAD:backend/paper_portfolio_read_model.py"],
            cwd=str(ROOT), capture_output=True, check=True,
        ).stdout
        tmp = Path(tempfile.mkdtemp(prefix="r23_head_")) / "paper_portfolio_read_model.py"
        tmp.write_bytes(raw.replace(b"\r\n", b"\n"))
        module = _load_module(tmp, "r23_head_module")
        label = "HEAD (unfixed)"
    else:
        module = _load_module(
            BACKEND / "paper_portfolio_read_model.py", "r23_current_module")
        label = "worktree (fixed)"

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    build_fixture(conn)
    result = probe(module, conn)
    conn.close()

    print(f"source: {label}")
    print(f"  _account_attached_by      = {result['attached']}")
    print(f"  _cycle_initial(account)   = {result['initial']}")
    print(f"  cash(account)             = {result['cash']}")

    verified = result["cash"] != (None, "unknown") or result["initial"] is not None
    if verified:
        print("  verdict: REPRODUCED (account capital published without attachment proof)")
        return 0
    print("  verdict: NOT REPRODUCED (account capital stays unknown)")
    return 1 if not use_head else 0


if __name__ == "__main__":
    raise SystemExit(main())
