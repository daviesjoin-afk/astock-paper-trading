# -*- coding: utf-8 -*-
"""Round-13 §3 —— before-fix reproduction：``/rebalance/status`` 的固定 TTL cache。

用法::

    python work/r13_before_fix_repro.py

本脚本回答一个问题：**``GET /api/adaptive/rebalance/status`` 返回的是"请求时刻
当前 active cycle 的运营状态"，还是一个进程内的旧快照？**

与 ``work/r11_before_fix_repro.py`` / ``work/r12_before_fix_repro.py`` 同样是
**差分探针** —— 修复前后各跑一次，两次判定含义相反：

* 在 **未修复** 源码（exact head ``4fa2162``）上跑 ⇒ 期望 ``PASS``
  （观察到 stale cached status）；
* 在 **已修复** 源码上跑 ⇒ 期望 ``FAIL (defect not reproduced)``。

**修复后报 FAIL 是正确结果，不是回归** —— 它正是"缺陷已消失"的读数。
修复后的正向验证见 ``backend/test_rebalance_cycle_scope.py`` 的
``RB_C_StatusFreshness``。

为什么这是承重的：Round-12 把 rebalance state 做成 cycle-owned，但
``rebalance_status()`` 在**解析当前周期之前**就返回了
``_cache_get("rebalance_status", ttl=30)``。于是：

    HTTP operational view  !=  authoritative current cycle

三个确定性 claim（全部走真实 handler，不直接测 ``_cache_get``/``_cache_set``）：

  R13-C1  cycle 8 的 status 被缓存后，同日翻到 cycle 9，**不等 TTL** 再 GET
          ⇒ 仍返回 cycle 8 的 payload（``response["cycle_id"] == 8``），
          而库里 active cycle 已经是 9
  R13-C2  同周期内：GET status 缓存一个旧 view，然后用真实 scan handler 写出
          新 scan/plan，**不等 TTL** 再 GET ⇒ 新 scan/plan 不可见
  R13-C3  cycle 8 的 status 被缓存后，让"没有 active cycle"成立，**不等 TTL**
          再 GET ⇒ 仍返回 200 + cycle_id=8，而不是 409 ``no_active_cycle``

**夹具隔离是承重的**（Round-12 的第一版探针正是栽在这里）：
``api_adaptive._paper_rebalance_db`` 用的是 ``adaptive.PAPER_DB_PATH``，
**不是** ``paper_trading.DB_PATH``。只 patch 后者会让真实 scan 打到
``data_cache/paper_trading.sqlite3``。因此本脚本：

1. 对每个 claim 同时 patch ``AE.PAPER_DB_PATH`` **与** ``PT.DB_PATH`` 到同一个
   夹具文件（另 patch ``AE.DB_PATH`` / ``AE.CACHE_DIR`` 指向 temp）；
2. 断言夹具路径与真实路径 ``realpath`` 不同；
3. 断言 scan **确实**在夹具库里产生了新行（否则夹具隔离失效，读数无意义）；
4. 对真实两个库做 before/after 快照，断言**零变化**。

不依赖 sleep / 真实网络 / 真实行情 / 生产 data_cache。
"""
from __future__ import annotations

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

ACCOUNT = "tq_breakout"
CODE8 = "OLD8"          # cycle 8 的扫描/持仓代码
CODE9 = "NEW9"          # cycle 9 的扫描/持仓代码 —— 与 cycle 8 明显不同
NAME = "测试股"

REBALANCE_TABLES = ("rebalance_scans", "rebalance_plans", "rebalance_cooldown")

QUOTE8 = {"code": CODE8, "price": 10.0, "pct": 0.0, "super_net": 0.0}

_TMP_ROOT = None


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


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


# ── 夹具原语 ────────────────────────────────────────────────────────────────

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


def _add_lot(conn, cycle_id, code, qty=100):
    conn.execute(
        "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
        "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
        "is_t_base,source_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cycle_id, ACCOUNT, code, NAME, "测试", qty, qty, 10.0,
         "2026-08-31 10:00:00", "2026-09-01", "stock_t1", 1, 1, 4242),
    )
    conn.commit()


def _seed_scan(conn, cycle_id, code, quality):
    conn.execute(
        "INSERT INTO rebalance_scans(cycle_id,scan_date,account_id,code,name,current_qty,"
        "cost,current_price,unrealized_pnl_pct,hold_days,quality_score,prev_quality_score,"
        "quality_change,fund_flow_trend,consecutive_outflow_days,action,action_reason,"
        "planned_sell_ratio,scan_version,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cycle_id, "2026-09-19", ACCOUNT, code, NAME, 100, 10.0, 10.0, 0.0, 30,
         quality, quality, 0.0, "outflow", 1, "hold", "fixture",
         0.0, RS.REBALANCE_VERSION, "2026-09-19T22:00:00"),
    )
    conn.commit()


def _seed_plan(conn, cycle_id, code, status="planned"):
    cur = conn.execute(
        "INSERT INTO rebalance_plans(cycle_id,plan_date,account_id,code,name,action,"
        "sell_qty,sell_ratio,sell_reason,status,plan_version,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cycle_id, "2026-09-19", ACCOUNT, code, NAME, "sell", 100, 1.0,
         "fixture", status, RS.REBALANCE_VERSION,
         "2026-09-19T22:00:00", "2026-09-19T22:00:00"),
    )
    conn.commit()
    return int(cur.lastrowid)


def _new_fixture(label):
    """建一个全新的夹具 paper 库（真实 ``PT.init_db()`` schema + rebalance 表）。

    调用方必须已经把 ``PT.DB_PATH`` **与** ``AE.PAPER_DB_PATH`` 都 patch 到同一
    路径 —— 前者决定 ``init_db()`` 建在哪，后者决定
    ``api_adaptive._paper_rebalance_db()`` 读写哪。只 patch 一个是 Round-12
    第一版探针污染真实库的原因。
    """
    paper_path = os.path.join(_TMP_ROOT, label, "paper.sqlite3")
    os.makedirs(os.path.dirname(paper_path), exist_ok=True)
    # 承重自检：夹具路径必须**就是**当前被 patch 的两个入口路径。少了这一条，
    # 路径拼写不一致会让 init_db 去建一个父目录不存在的文件（本轮我正是这么
    # 写错过一次：夹具用 "c1"，patch 用 "R13-C1"）。
    assert os.path.realpath(paper_path) == os.path.realpath(PT.DB_PATH), \
        f"夹具路径 {paper_path} != PT.DB_PATH {PT.DB_PATH}"
    assert os.path.realpath(paper_path) == os.path.realpath(AE.PAPER_DB_PATH), \
        f"夹具路径 {paper_path} != AE.PAPER_DB_PATH {AE.PAPER_DB_PATH}"
    conn = sqlite3.connect(paper_path)
    conn.row_factory = sqlite3.Row
    PT.init_db()
    RS.ensure_schema(conn)
    conn.commit()
    return paper_path, conn


def _call_status():
    """调用**真实** handler；返回 ``(payload, http_status)``。

    ``rebalance_status()`` 是普通同步函数，直接调用即为生产路径（FastAPI 只是
    把它挂到 ``GET /api/adaptive/rebalance/status``）。
    """
    from fastapi import HTTPException
    try:
        return API.rebalance_status(), 200
    except HTTPException as exc:
        return exc.detail, exc.status_code


# ── claims ──────────────────────────────────────────────────────────────────

def claim_c1():
    """R13-C1：同日 cycle 翻转后，立即 GET 仍返回旧周期 cached payload。"""
    paper_path, conn = _new_fixture("c1")
    API._cache_clear()

    c8 = _add_cycle(conn, "running", "2026-09-19 10:00:00")
    _activate(conn, c8)
    _add_lot(conn, c8, CODE8)
    _seed_scan(conn, c8, CODE8, 90.0)
    _seed_plan(conn, c8, CODE8)
    conn.close()

    first, code_first = _call_status()

    # ── 同日翻转到 cycle 9，建立**明显不同**的 cycle 9 状态 ──
    conn = _connect(paper_path)
    c9 = _add_cycle(conn, "running", "2026-09-19 12:00:00")
    _activate(conn, c9)
    _add_lot(conn, c9, CODE9)
    _seed_scan(conn, c9, CODE9, 50.0)
    _seed_plan(conn, c9, CODE9)
    db_active = RS.resolve_cycle_id(conn)
    conn.close()

    # 不 sleep、不 clear cache —— 立刻再问一次。
    second, code_second = _call_status()

    first_codes = [r["code"] for r in first["recent_scans"]]
    second_codes = [r["code"] for r in second["recent_scans"]]

    print("── R13-C1 同日 cycle rollover ──")
    print(f"  cycle 8 = {c8}   cycle 9 = {c9}   db active cycle = {db_active}")
    print(f"  GET #1 (cycle 8 active)  http={code_first}  cycle_id={first.get('cycle_id')}"
          f"  recent_scans={first_codes}")
    print(f"  GET #2 (翻转后立即)       http={code_second} cycle_id={second.get('cycle_id')}"
          f"  recent_scans={second_codes}")
    print()

    # 复现成立：第二次仍报旧周期，且看不到新周期状态。
    stale_cycle = int(second.get("cycle_id") or -1) == c8
    stale_rows = second_codes == first_codes and CODE9 not in second_codes
    return stale_cycle and stale_rows, {
        "second_cycle_id": second.get("cycle_id"),
        "db_active_cycle": db_active,
        "second_codes": second_codes,
    }


def claim_c2():
    """R13-C2：同周期内 scan 之后，立即 GET 仍返回 scan 之前的 view。"""
    paper_path, conn = _new_fixture("c2")
    API._cache_clear()

    c8 = _add_cycle(conn, "running", "2026-09-19 10:00:00")
    _activate(conn, c8)
    _add_lot(conn, c8, CODE8)
    conn.close()

    before, code_before = _call_status()
    before_scans = [r["code"] for r in before["recent_scans"]]
    before_pending = len(before["pending_plans"])

    # 真实 scan handler（行情已 patch，禁止网络）。
    with mock.patch.object(API, "_fetch_rebalance_quotes",
                           return_value=({CODE8: QUOTE8}, {"source": "fixture"})):
        scan_result = API.run_rebalance_scan(confirmed=True)

    # 库里确实写下了新状态 —— 承重前提：少了这一条，夹具隔离一旦失效，
    # 探针会读到未更新的旧库并把结论判成"已修复"。
    conn = _connect(paper_path)
    db_scans = [r["code"] for r in conn.execute(
        "SELECT code FROM rebalance_scans ORDER BY id").fetchall()]
    db_pending = conn.execute(
        "SELECT COUNT(*) FROM rebalance_plans WHERE status IN ('planned','verified')"
    ).fetchone()[0]
    conn.close()
    if not db_scans:
        raise AssertionError(
            "夹具隔离失效：scan 未在夹具库中产生新行（检查 AE.PAPER_DB_PATH 是否已 patch）"
        )

    # 不 sleep、不 clear cache —— 立刻再问一次。
    after, code_after = _call_status()
    after_scans = [r["code"] for r in after["recent_scans"]]
    after_pending = len(after["pending_plans"])

    print("── R13-C2 同周期 scan freshness ──")
    print(f"  cycle 8 = {c8}")
    print(f"  GET #1 (scan 之前)     http={code_before} recent_scans={before_scans}"
          f"  pending={before_pending}")
    print(f"  POST /rebalance/scan   plans_created={scan_result.get('plans_created')}"
          f"  db_scans={db_scans}  db_pending={db_pending}")
    print(f"  GET #2 (scan 之后立即)  http={code_after} recent_scans={after_scans}"
          f"  pending={after_pending}")
    print()

    invisible_scan = after_scans == before_scans and not after_scans
    invisible_plan = after_pending == before_pending and db_pending > before_pending
    return (invisible_scan or invisible_plan), {
        "after_scans": after_scans,
        "before_scans": before_scans,
        "after_pending": after_pending,
        "db_pending": db_pending,
    }


def claim_c3():
    """R13-C3：没有 active cycle 时，立即 GET 仍返回旧周期 cached 200。"""
    paper_path, conn = _new_fixture("c3")
    API._cache_clear()

    c8 = _add_cycle(conn, "running", "2026-09-19 10:00:00")
    _activate(conn, c8)
    _add_lot(conn, c8, CODE8)
    _seed_scan(conn, c8, CODE8, 90.0)
    conn.close()

    first, code_first = _call_status()

    # 让"没有 active cycle"成立：所有周期移出 (draft,running,paused)。
    conn = _connect(paper_path)
    conn.execute("UPDATE paper_cycles SET status='archived'")
    conn.execute("UPDATE paper_accounts SET status='running', cycle_id=NULL")
    conn.commit()
    resolved = RS.resolve_cycle_id(conn)
    conn.close()

    # 不 sleep、不 clear cache —— 立刻再问一次。
    second, code_second = _call_status()

    print("── R13-C3 no active cycle fail closed ──")
    print(f"  GET #1 (cycle 8 active)   http={code_first} cycle_id={first.get('cycle_id')}")
    print(f"  库里 active cycle         = {resolved}")
    print(f"  GET #2 (无 active cycle)   http={code_second}"
          f"  cycle_id={second.get('cycle_id') if isinstance(second, dict) else None}"
          f"  detail={second.get('status') if isinstance(second, dict) else second}")
    print()

    bypassed = code_second == 200 and int(second.get("cycle_id") or -1) == c8
    return bypassed, {"second_http": code_second, "db_active_cycle": resolved}


# ── main ────────────────────────────────────────────────────────────────────

CLAIMS = (
    ("R13-C1", "c1", "同日 cycle 翻转后立即 GET 仍返回旧周期 cached status", claim_c1),
    ("R13-C2", "c2", "同周期 scan 之后立即 GET 仍返回 scan 之前的 view", claim_c2),
    ("R13-C3", "c3", "无 active cycle 时立即 GET 仍返回旧周期 cached 200", claim_c3),
)


def main():
    global _TMP_ROOT

    live_paper = AE.PAPER_DB_PATH
    live_adaptive = AE.DB_PATH

    print("=" * 78)
    print("Round-13 §3  before-fix reproduction：rebalance operational status freshness")
    print("=" * 78)
    print(f"live paper    = {live_paper}")
    print(f"live adaptive = {live_adaptive}")
    print()

    _TMP_ROOT = tempfile.mkdtemp(prefix="r13_repro_")
    print(f"fixture root  = {_TMP_ROOT}")

    # 承重前提：夹具库必须与真实 data_cache 库物理分离。
    probe_fixture = os.path.join(_TMP_ROOT, "probe", "paper.sqlite3")
    for label, real in (("paper", live_paper), ("adaptive", live_adaptive)):
        assert os.path.realpath(probe_fixture) != os.path.realpath(real), \
            f"夹具库与真实 {label} 库指向同一文件 ⇒ 探针会污染真实账本"

    live_before = {"paper": _live_snapshot(live_paper),
                   "adaptive": _live_snapshot(live_adaptive)}

    adaptive_path = os.path.join(_TMP_ROOT, "adaptive.sqlite3")
    results = []
    with mock.patch.object(AE, "DB_PATH", adaptive_path), \
            mock.patch.object(AE, "CACHE_DIR", _TMP_ROOT), \
            mock.patch.object(PT, "_benchmark_close", return_value=None), \
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True):
        with AE._connect():
            pass

        for cid, label, text, fn in CLAIMS:
            fixture_path = os.path.join(_TMP_ROOT, label, "paper.sqlite3")
            # 每个 claim 用自己的夹具文件；两个"当前库"入口必须同时指向它。
            with mock.patch.object(AE, "PAPER_DB_PATH", fixture_path), \
                    mock.patch.object(PT, "DB_PATH", fixture_path):
                hit, evidence = fn()
            results.append((cid, text, hit, evidence))

        live_after = {"paper": _live_snapshot(live_paper),
                      "adaptive": _live_snapshot(live_adaptive)}

    print("── 真实库零变化校验（承重）──")
    for label in ("paper", "adaptive"):
        same = live_before[label] == live_after[label]
        print(f"  live {label:9s} before    = {live_before[label]}")
        print(f"  live {label:9s} after     = {live_after[label]}")
        print(f"  live {label:9s} unchanged = {same}")
        if not same:
            raise AssertionError(
                f"探针污染了真实 {label} 库：{live_before[label]} -> {live_after[label]}"
            )
    print()

    print("── VERDICT ──")
    for cid, text, hit, evidence in results:
        print(f"  {cid} {'REPRODUCED' if hit else 'not reproduced':<14} {text}")
        print(f"          evidence: {evidence}")
    reproduced = sum(1 for _c, _t, hit, _e in results if hit)
    print()

    if reproduced == len(results):
        print(f"  BEFORE-FIX REPRODUCTION: PASS ({reproduced}/{len(results)} claims)")
        print("  → /rebalance/status 返回的是进程内旧快照，不是请求时刻当前周期的")
        print("    运营状态：cycle 翻转、同周期写入、无周期 fail-closed 三者都会被")
        print("    30 秒固定 TTL cache 绕过。")
        print("  （这是在**未修复**源码上期望的读数。）")
        return 0

    if reproduced:
        print(f"  BEFORE-FIX REPRODUCTION: PARTIAL ({reproduced}/{len(results)} claims)")
        return 1

    print("  BEFORE-FIX REPRODUCTION: FAIL (defect not reproduced)")
    print("  → /rebalance/status 每次都重新解析当前周期并读取实时状态。")
    print("  （若当前源码**已含修复**，这正是期望读数：缺陷已消除，不是回归。")
    print("    修复后的正向验证见 backend/test_rebalance_cycle_scope.py。）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
