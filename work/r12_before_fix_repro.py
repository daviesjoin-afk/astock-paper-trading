# -*- coding: utf-8 -*-
"""Round-12 §2 —— before-fix reproduction：rebalance state 不是 cycle-owned。

用法::

    python work/r12_before_fix_repro.py

本脚本回答一个问题：**cycle 8 的 rebalance 状态会不会影响 cycle 9 的调仓判断？**
与 ``work/r11_before_fix_repro.py`` 同样是**差分探针** —— 修复前后各跑一次，
两次判定含义相反：

* 在 **未修复** 源码（exact head ``5b04c9b``）上跑 ⇒ 期望 ``PASS``
  （观察到跨周期串线）；
* 在 **已修复** 源码上跑 ⇒ 期望 ``FAIL (defect not reproduced)``。

**修复后报 FAIL 是正确结果，不是回归** —— 它正是"缺陷已消失"的读数。
修复后的正向验证见 ``backend/test_rebalance_cycle_scope.py``。

夹具（deterministic，不依赖真实行情/网络）：

    cycle 8:  account tq_breakout / code 600519
              rebalance_scans: quality_score=90, fund_flow_trend=outflow
                               （今日 1 行 + 更早 4 行，共 5 行）
              rebalance_plans: status=planned, sell_qty=100
    cycle 9:  同 account / 同 code，activate 后做一次新 scan

探针逐条断言以下七个事实是否成立（未修复时应**全部成立**）：

  C1  cycle 9 的新 scan 行 ``prev_quality_score`` 读到了 cycle 8 的 90
  C2  cycle 9 的 ``consecutive_outflow_days`` 含 cycle 8 的 outflow 行
  C3  ``get_pending_plans(conn)`` 返回 cycle 8 的 plan
  C4  ``verify_all_plans(...)`` 改写了 cycle 8 的 plan 状态
  C5  ``UNIQUE(scan_date, account_id, code)`` 使同日两周期无法各留一行
      （cycle 9 的 scan **replace 掉** cycle 8 的当日行）
  C6  新写入的 scan / plan 行**没有** cycle 归属列（schema 里根本没有该列）
  C7  ``rebalance_cooldown`` 主键是 ``(code, account_id)`` —— 旧周期冷却
      天然压住新周期，且无 cycle 列可分区

**夹具隔离是承重的（第一版探针就栽在这里）**：``api_adaptive._paper_rebalance_db``
用的是 ``adaptive.PAPER_DB_PATH``，**不是** ``paper_trading.DB_PATH``。只 patch
后者会让真实 scan 打到 ``data_cache/paper_trading.sqlite3`` —— 探针既污染真实
本地账本，又让所有读数取自未被扫描的夹具库而**看似通过**。因此本脚本：

1. 同时 patch ``AE.DB_PATH`` / ``AE.PAPER_DB_PATH`` / ``AE.CACHE_DIR`` /
   ``PT.DB_PATH``；
2. 断言夹具路径与真实路径 ``realpath`` 不同；
3. 断言 scan **确实**在夹具库里产生了新行；
4. 对真实两个库做 before/after 快照，断言**零变化**。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)

import adaptive_engine as AE  # noqa: E402
import api_adaptive as API  # noqa: E402
import paper_trading as PT  # noqa: E402
import rebalance_scanner as RS  # noqa: E402

ACCOUNT = "tq_breakout"   # max_hold = 4 天（REBALANCE_THRESHOLDS）
CODE = "600519"
NAME = "测试股"
CYCLE8_QUALITY = 90.0
CYCLE9_QUALITY = 50.0     # lot 派生持仓没有 quality review ⇒ 默认 50.0
CYCLE8_OUTFLOW_DAYS = 5

REBALANCE_TABLES = ("rebalance_scans", "rebalance_plans", "rebalance_cooldown")

#: super_net < 0 ⇒ fund_flow_trend = "outflow"；price==cost ⇒ 收益 0% < 2%
#: 且持仓远超 max_hold ⇒ 确定性触发 sell 计划。
QUOTE = {"code": CODE, "price": 10.0, "pct": 0.0, "super_net": -1.0e7}


def _today():
    return RS._date()


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _pk_columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            if row[5]]


def _live_snapshot(path):
    """真实库的只读快照：rebalance 表是否存在 + 行数。"""
    if not os.path.exists(path):
        return {"exists": False}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            "exists": True,
            "rebalance": {
                table: (conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        if table in tables else None)
                for table in REBALANCE_TABLES
            },
        }
    finally:
        conn.close()


def _add_cycle(conn, status, stamp):
    cur = conn.execute(
        "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
        "updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
        (f"c-{stamp}", status, 100000.0, "shared_pool", stamp, stamp,
         stamp if status == "running" else None),
    )
    conn.commit()
    return int(cur.lastrowid)


def _activate(conn, cycle_id):
    conn.execute("UPDATE paper_accounts SET cycle_id=?, status='running' WHERE id=?",
                 (cycle_id, ACCOUNT))
    conn.commit()


def _add_lot(conn, cycle_id, qty):
    conn.execute(
        "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
        "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
        "is_t_base,source_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cycle_id, ACCOUNT, CODE, NAME, "测试", qty, qty, 10.0,
         "2026-08-31 10:00:00", "2026-09-01", "stock_t1", 1, 1, 4242),
    )
    conn.commit()


def _seed_cycle8_scans(conn, days):
    """写 cycle 8 的扫描历史：``days`` 个不同 scan_date，全部 outflow。"""
    today = _today()
    dates = [(today - dt.timedelta(days=offset)).isoformat()
             for offset in range(days - 1, -1, -1)]
    for scan_date in dates:
        conn.execute(
            "INSERT INTO rebalance_scans(scan_date,account_id,code,name,current_qty,cost,"
            "current_price,unrealized_pnl_pct,hold_days,quality_score,prev_quality_score,"
            "quality_change,fund_flow_trend,consecutive_outflow_days,action,action_reason,"
            "planned_sell_ratio,scan_version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scan_date, ACCOUNT, CODE, NAME, 100, 10.0, 10.0, 0.0, 19,
             CYCLE8_QUALITY, CYCLE8_QUALITY, 0.0, "outflow", 1, "hold", "cycle8 fixture",
             0.0, RS.REBALANCE_VERSION, f"{scan_date} 22:00:00"),
        )
    conn.commit()
    return dates


def _seed_cycle8_plan(conn, sell_qty=100):
    stamp = f"{_today().isoformat()} 22:00:00"
    cur = conn.execute(
        "INSERT INTO rebalance_plans(plan_date,account_id,code,name,action,sell_qty,"
        "sell_ratio,sell_reason,status,plan_version,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (_today().isoformat(), ACCOUNT, CODE, NAME, "sell", sell_qty, 1.0,
         "cycle8 fixture plan", "planned", RS.REBALANCE_VERSION, stamp, stamp),
    )
    conn.commit()
    return int(cur.lastrowid)


def _probe(paper_path):
    """在夹具库上跑完整场景，返回逐条判定结果。"""
    conn = _connect(paper_path)
    RS.ensure_schema(conn)

    # ── 1. cycle 8 夹具 ─────────────────────────────────────────────────────
    cycle8 = _add_cycle(conn, "running", "2026-09-19 10:00:00")
    _activate(conn, cycle8)
    _add_lot(conn, cycle8, 100)
    scan_dates = _seed_cycle8_scans(conn, CYCLE8_OUTFLOW_DAYS)
    plan8_id = _seed_cycle8_plan(conn, sell_qty=100)

    print("── cycle 8 夹具 ──")
    print(f"  cycle_id                          = {cycle8}")
    print(f"  rebalance_scans scan_dates        = {scan_dates}")
    print(f"  rebalance_scans quality_score     = {CYCLE8_QUALITY} (all rows)")
    print(f"  rebalance_scans fund_flow_trend   = outflow (all rows)")
    print(f"  rebalance_plans id={plan8_id} status=planned sell_qty=100")
    print()

    # ── 2. cycle 9：创建 + 激活（同一天）─────────────────────────────────────
    cycle9 = _add_cycle(conn, "running", "2026-09-19 12:00:00")
    _activate(conn, cycle9)
    _add_lot(conn, cycle9, 100)
    print("── cycle 9 ──")
    print(f"  cycle_id                          = {cycle9}")
    print(f"  active cycle (read-only resolver) = {RS.PPRM.active_cycle_id(conn)}")
    print()

    scans_before = conn.execute("SELECT COUNT(*) FROM rebalance_scans").fetchone()[0]
    max_scan_id_before = conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM rebalance_scans").fetchone()[0]
    plan8_before = dict(conn.execute(
        "SELECT * FROM rebalance_plans WHERE id=?", (plan8_id,)).fetchone())
    conn.close()

    # ── 3. 在 cycle 9 做一次真实 scan（经 HTTP handler，行情已 patch）────────
    with mock.patch.object(API, "_fetch_rebalance_quotes",
                           return_value=({CODE: QUOTE}, {"source": "fixture"})):
        result = API.run_rebalance_scan(confirmed=True)

    conn = _connect(paper_path)
    scans_after = conn.execute("SELECT COUNT(*) FROM rebalance_scans").fetchone()[0]
    new_rows = conn.execute(
        "SELECT * FROM rebalance_scans WHERE id > ? ORDER BY id",
        (max_scan_id_before,)).fetchall()

    # 承重前提：scan 必须**真的**写进了夹具库。少了这一条，夹具隔离一旦失效，
    # 探针会读到未更新的旧行并把结论判成"已修复"（第一版探针正是如此）。
    if not new_rows:
        conn.close()
        raise AssertionError(
            "夹具隔离失效：scan 未在夹具库中产生新行（检查 AE.PAPER_DB_PATH 是否已 patch）"
        )
    new_scan = dict(new_rows[-1])

    print("── cycle 9 scan 写下的行 ──")
    print(f"  scan_date                 = {new_scan['scan_date']}")
    print(f"  quality_score             = {new_scan['quality_score']}")
    print(f"  prev_quality_score        = {new_scan['prev_quality_score']}")
    print(f"  fund_flow_trend           = {new_scan['fund_flow_trend']}")
    print(f"  consecutive_outflow_days  = {new_scan['consecutive_outflow_days']}")
    print(f"  action                    = {new_scan['action']}")
    print(f"  plans_created (summary)   = {result.get('plans_created')}")
    print()

    # ── C1/C2：跨周期读取 ───────────────────────────────────────────────────
    c1 = float(new_scan["prev_quality_score"]) == CYCLE8_QUALITY
    c2 = int(new_scan["consecutive_outflow_days"]) >= CYCLE8_OUTFLOW_DAYS

    # ── C3/C4：端点同款流程 —— get_pending_plans → verify_all_plans ─────────
    pending = RS.get_pending_plans(conn)
    pending_ids = [int(p["id"]) for p in pending]
    c3 = plan8_id in pending_ids

    verify_quote = {"code": CODE, "price": 9.0, "pct": -5.0, "vol_ratio": 1.0,
                    "super_net": 0.0}
    RS.verify_all_plans(conn, pending, {CODE: verify_quote})
    conn.commit()
    plan8_after = dict(conn.execute(
        "SELECT * FROM rebalance_plans WHERE id=?", (plan8_id,)).fetchone())
    c4 = plan8_after["status"] != plan8_before["status"]

    # ── C5：同日两周期无法各留一行（UNIQUE 跨周期）──────────────────────────
    today = _today().isoformat()
    today_rows = conn.execute(
        "SELECT id, quality_score FROM rebalance_scans WHERE scan_date=? AND account_id=?"
        " AND code=?", (today, ACCOUNT, CODE)).fetchall()
    c5 = (scans_after == scans_before) and len(today_rows) == 1

    # ── C6：schema 里根本没有 cycle 归属列 ──────────────────────────────────
    cols = _columns(conn, "rebalance_scans")
    plan_cols = _columns(conn, "rebalance_plans")
    c6 = ("cycle_id" not in cols) and ("cycle_id" not in plan_cols)

    # ── C7：cooldown 主键不含 cycle ─────────────────────────────────────────
    cooldown_pk = _pk_columns(conn, "rebalance_cooldown")
    cooldown_cols = _columns(conn, "rebalance_cooldown")
    c7 = (cooldown_pk == ["code", "account_id"]) and ("cycle_id" not in cooldown_cols)

    print("── 跨周期证据 ──")
    print(f"  rebalance_scans 列                    = {cols}")
    print(f"  rebalance_plans 列                    = {plan_cols}")
    print(f"  rebalance_cooldown 列 / PK            = {cooldown_cols} / {cooldown_pk}")
    print(f"  rebalance_scans 行数 before/after     = {scans_before}/{scans_after}")
    print(f"  今日 ({today}) A/X 的 scan 行数        = {len(today_rows)}"
          f" (quality={[float(r['quality_score']) for r in today_rows]})")
    print(f"  cycle 8 plan #{plan8_id} status       = "
          f"{plan8_before['status']} -> {plan8_after['status']}")
    print(f"  get_pending_plans() 返回的 plan id    = {pending_ids}")
    print()

    conn.close()
    return [
        ("C1", "cycle 9 scan 的 prev_quality_score 读到 cycle 8 的 90", c1),
        ("C2", "cycle 9 的 consecutive_outflow_days 含 cycle 8 的 outflow 行", c2),
        ("C3", "get_pending_plans(conn) 返回 cycle 8 的 plan", c3),
        ("C4", "verify_all_plans(...) 改写了 cycle 8 的 plan 状态", c4),
        ("C5", "同日 UNIQUE 让 cycle 9 scan replace 掉 cycle 8 当日行", c5),
        ("C6", "新写入的 scan/plan 行没有 cycle 归属（schema 无该列）", c6),
        ("C7", "cooldown 主键是 (code, account_id)，无 cycle 分区", c7),
    ]


def main():
    tmp = tempfile.mkdtemp(prefix="r12_repro_")
    paper_path = os.path.join(tmp, "paper.sqlite3")
    adaptive_path = os.path.join(tmp, "adaptive.sqlite3")

    live_paper = AE.PAPER_DB_PATH
    live_adaptive = AE.DB_PATH

    print("=" * 78)
    print("Round-12 §2  before-fix reproduction：rebalance state cycle ownership")
    print("=" * 78)
    print(f"fixture paper.sqlite3    = {paper_path}")
    print(f"fixture adaptive.sqlite3 = {adaptive_path}")
    print(f"live    paper            = {live_paper}")
    print(f"live    adaptive         = {live_adaptive}")
    print(f"today                    = {_today().isoformat()}")
    print()

    # 承重前提：夹具库必须与真实 data_cache 库物理分离。少了这一层，scan 会打到
    # 真实本地账本（第一版探针就这样污染了 data_cache 并留下 4 行扫描记录）。
    assert os.path.realpath(paper_path) != os.path.realpath(live_paper), \
        "夹具 paper 库与真实 paper 库指向同一文件 ⇒ 探针会污染真实账本"
    assert os.path.realpath(paper_path) != os.path.realpath(live_adaptive), \
        "夹具 paper 库与真实 adaptive 库指向同一文件"
    assert os.path.realpath(adaptive_path) != os.path.realpath(live_adaptive), \
        "夹具 adaptive 库与真实 adaptive 库指向同一文件"

    live_before = {"paper": _live_snapshot(live_paper),
                   "adaptive": _live_snapshot(live_adaptive)}

    with mock.patch.object(AE, "DB_PATH", adaptive_path), \
            mock.patch.object(AE, "PAPER_DB_PATH", paper_path), \
            mock.patch.object(AE, "CACHE_DIR", tmp), \
            mock.patch.object(PT, "DB_PATH", paper_path), \
            mock.patch.object(PT, "_benchmark_close", return_value=None), \
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True):
        PT.init_db()
        with AE._connect():
            pass
        claims = _probe(paper_path)
        live_after = {"paper": _live_snapshot(live_paper),
                      "adaptive": _live_snapshot(live_adaptive)}

    print("── 真实库零变化校验（承重）──")
    for label in ("paper", "adaptive"):
        same = live_before[label] == live_after[label]
        print(f"  live {label:9s} before = {live_before[label]}")
        print(f"  live {label:9s} after  = {live_after[label]}")
        print(f"  live {label:9s} unchanged = {same}")
        if not same:
            raise AssertionError(
                f"探针污染了真实 {label} 库：{live_before[label]} -> {live_after[label]}"
            )
    print()

    print("── VERDICT ──")
    for cid, text, hit in claims:
        print(f"  {cid} {'REPRODUCED' if hit else 'not reproduced':<14} {text}")
    reproduced = sum(1 for _cid, _text, hit in claims if hit)
    print()

    if reproduced:
        print(f"  BEFORE-FIX REPRODUCTION: PASS ({reproduced}/{len(claims)} claims)")
        print("  → rebalance state 确实不是 cycle-owned：cycle 8 的 scan / plan /")
        print("    资金流历史会直接进入 cycle 9 的调仓判断与计划验证。")
        print("  （这是在**未修复**源码上期望的读数。）")
        return 0

    print("  BEFORE-FIX REPRODUCTION: FAIL (defect not reproduced)")
    print("  → rebalance state 已按 cycle 隔离。")
    print("  （若当前源码**已含修复**，这正是期望读数：缺陷已消除，不是回归。")
    print("    修复后的正向验证见 backend/test_rebalance_cycle_scope.py。）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
