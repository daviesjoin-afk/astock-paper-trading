# -*- coding: utf-8 -*-
"""R22 attachment-provenance before-fix 复现：account attachment 必须 fail closed。

规格（PR #180 最后一轮人工审核 P1）：

    cycle existed by D  !=  account belonged to cycle by D

场景 A：cycle 在 asof 之前创建，`paper_accounts` 当前指向该 cycle，
`paper_parameter_versions` 表存在但没有匹配的 attachment/version 行，
且没有 account-specific bounded activity
=> account initial capital 与 cash 都必须是 UNKNOWN。

用法::

    python work/r22_attachment_provenance_repro.py [--rev <rev>]

- 不带 ``--rev``：检查**工作区**当前版本（应当已修复）。
  预期 NOT REPRODUCED；若变成 REPRODUCED 说明修复被回退，退出码 1。
- 带 ``--rev``：从该 revision 取出 ``backend/paper_portfolio_read_model.py``
  作为 **before-fix 基线**（应指向修复提交的父提交，默认 ``HEAD^``）。
  预期 REPRODUCED；若该 rev 其实已含修复，退出码 1 并给出明确提示。

本脚本是自校验的：它不假设你给的 revision 一定未修复，而是直接检查源码里
是否还存在那条 fail-open 的 fallback，指错版本会明确报错，而不是静默给出
一个「两边一致」的无意义结论。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

ACCOUNT = "r22-acct"          # 独立于既有夹具，避免依赖生产常量
DAY = dt.date(2026, 9, 20)
MODULE_REL = "backend/paper_portfolio_read_model.py"

#: 修复前的 fail-open fallback：把 cycle creation 当作 account attachment 证据。
PREFIX_MARKER = "return _cycle_created_by(conn, context, account_id=account_id)"


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
        (7, "r22-cycle", "running", 100000.0,
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


def _resolve_rev(rev: str) -> Path:
    raw = subprocess.run(
        ["git", "show", f"{rev}:{MODULE_REL}"],
        cwd=str(ROOT), capture_output=True, check=True,
    ).stdout
    tmp = Path(tempfile.mkdtemp(prefix="r22_rev_")) / "paper_portfolio_read_model.py"
    tmp.write_bytes(raw.replace(b"\r\n", b"\n"))
    return tmp


def main() -> int:
    argv = sys.argv[1:]
    rev = argv[argv.index("--rev") + 1] if "--rev" in argv else None

    if rev is None:
        source = BACKEND / "paper_portfolio_read_model.py"
        module = _load_module(source, "r22_current_module")
        label = "worktree (expected: fixed)"
        expect_reproduced = False
    else:
        source = _resolve_rev(rev)
        module = _load_module(source, "r22_rev_module")
        label = f"{rev} (expected: pre-fix)"
        expect_reproduced = True

    # 自校验：先确认这个来源确实是/不是 before-fix 代码，再下结论。
    text = source.read_text(encoding="utf-8")
    if PREFIX_MARKER in text and not expect_reproduced:
        print(f"source: {label}")
        print("  !! 工作区仍存在 fail-open fallback，修复被回退", file=sys.stderr)
        return 1
    if PREFIX_MARKER not in text and expect_reproduced:
        print(f"source: {label}")
        print(f"  !! {rev} 已包含修复，不能作为 before-fix 基线；"
              "请指向修复提交的父提交（如 HEAD^）", file=sys.stderr)
        return 1

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    build_fixture(conn)
    result = probe(module, conn)
    conn.close()

    print(f"source: {label}")
    print(f"  _account_attached_by      = {result['attached']}")
    print(f"  _cycle_initial(account)   = {result['initial']}")
    print(f"  cash(account)             = {result['cash']}")

    published = result["cash"] != (None, "unknown") or result["initial"] is not None
    if published:
        print("  verdict: REPRODUCED (account capital published without attachment proof)")
    else:
        print("  verdict: NOT REPRODUCED (account capital stays unknown)")

    if published != expect_reproduced:
        want = "REPRODUCED" if expect_reproduced else "NOT REPRODUCED"
        print(f"  !! 预期 {want}，实得相反 → 退出码 1", file=sys.stderr)
        return 1
    print("  assertion: as expected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
