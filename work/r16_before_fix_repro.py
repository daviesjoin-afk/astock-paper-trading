# -*- coding: utf-8 -*-
"""R16 before-fix 差分复现（在**未修改**的 base SHA 上运行）。

覆盖规格 §54-§59 的四条缺陷：

    R16-C1  跨周期同分钟抑制（cycle 8 完成 → 同分钟切 cycle 9 → 被旧标记吞掉）
    R16-C2  同分钟不同 asof 碰撞（同一 cycle，同 runtime 分钟，两个 asof）
    R16-C3  分钟边界 orphan running（wrapper 与 impl 各自取时钟）
    R16-C4  quote I/O 期间 cycle rollover（scan 从 cycle 8 开始，写阶段落到 cycle 9）

脚本对**修复前/后两种代码形态都可用**：

* 修复前：执行权威是 ``paper_audit.event='risk_scan_state'``，没有
  ``paper_risk_scan_runs`` 表；
* 修复后：执行权威是 ``paper_risk_scan_runs``（身份含 cycle_id + asof_date +
  scan_minute）。

因此判据不写成"某张表存在"，而是写成**行为**：同一身份是否被错误抑制、是否
产生 orphan、写阶段是否落到了别的周期。

用法（仓库根目录）::

    python work/r16_before_fix_repro.py
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)

import paper_trading as PT  # noqa: E402
import universe as U  # noqa: E402

DAY1 = dt.date(2026, 9, 10)
DAY2 = dt.date(2026, 9, 11)
CODE = "600001"
ACCOUNT = "tq_breakout"
FIXED_MINUTE = dt.datetime(2026, 9, 10, 14, 50, 5)


class _Clock:
    """可编排的假时钟：按序列返回 ``datetime.now()``，耗尽后重复最后一个。"""

    def __init__(self, values):
        self.values = list(values)
        self.index = 0

    def next_now(self):
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value

    def shim(self):
        clock = self

        class _Date(dt.date):
            @classmethod
            def today(cls):
                return clock.values[0].date()

        class _Datetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                base = clock.next_now()
                return base if tz is None else base.replace(tzinfo=tz)

        class _DT:
            date = _Date
            datetime = _Datetime
            timedelta = dt.timedelta

        return _DT


def _fresh_exit_quote(day, price=9.0, pct=-8.0):
    return {
        "code": CODE, "name": f"测试股_{CODE}", "price": price, "high": price + 0.2,
        "low": price - 0.1, "pct": pct, "amount": 100000.0, "volume": 10000.0,
        "turnover": 1.0, "quote_source": "live",
        "quote_at": f"{day.isoformat()} 14:50:00",
        "quote_validation": "cross_source_checked",
    }


class Repro:
    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "r16_repro.sqlite3")
        self._patches = []

    def setup(self):
        self._patches = [
            mock.patch.object(PT, "DB_PATH", self.db_path),
            mock.patch.object(U, "is_trade_day",
                              lambda value=None: (U._as_date(value) or dt.date.today()).weekday() < 5),
            mock.patch.object(PT, "_quotes", side_effect=self._quotes),
            mock.patch.object(PT, "_news_for", return_value=[]),
            mock.patch.object(PT, "_cached_close_market",
                              return_value={"breadth": 0.5, "sentiment": "neutral"}),
            mock.patch.object(PT, "AD", None),
        ]
        for patcher in self._patches:
            patcher.start()
        PT.init_db()
        self.quotes = {}
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='running' WHERE id=1")

    def teardown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 夹具 ────────────────────────────────────────────────────────────────
    def _quotes(self, codes, asof_date=None):
        return {c: self.quotes[c] for c in codes if c in self.quotes}

    def active_cycle(self):
        with PT._db() as conn:
            return int(conn.execute(
                "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
                " ORDER BY id DESC LIMIT 1").fetchone()[0])

    def add_cycle(self):
        with PT._db(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
                "updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
                (f"c-{dt.datetime.now().timestamp()}", "running", 100000.0, "shared_pool",
                 "2026-09-10 14:50:30", "2026-09-10 14:50:30", "2026-09-10 14:50:30"),
            )
            return int(cur.lastrowid)

    def activate(self, cycle_id):
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='running' WHERE id=?", (cycle_id,))
            conn.execute("UPDATE paper_accounts SET cycle_id=?, status='running' WHERE id=?",
                         (cycle_id, ACCOUNT))

    def insert_lot(self, cycle_id, qty=500, cost=10.0):
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, ACCOUNT)
            cur = conn.execute(
                "INSERT INTO paper_orders(account_id, side, code, name, qty, planned_price,"
                " filled_price, amount, fees, status, reason, risk_payload, created_at,"
                " executed_at, order_type, origin, strategy_id, strategy_version,"
                " strategy_checksum, cycle_id) "
                "VALUES(?, 'buy', ?, ?, ?, ?, ?, ?, 5.0, 'filled', 'seed_buy', '{}', ?, ?,"
                " 'market', 'seed', ?, ?, ?, ?)",
                (ACCOUNT, CODE, f"测试股_{CODE}", qty, cost, cost, qty * cost,
                 "2026-09-09 09:30:00", "2026-09-09 09:30:00", *stamp, cycle_id),
            )
            order_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO paper_fills(order_id, account_id, side, code, qty, price, amount,"
                " fees, fill_date, quote_at, assumption) "
                "VALUES(?, ?, 'buy', ?, ?, ?, ?, 5.0, ?, ?, 'seed')",
                (order_id, ACCOUNT, CODE, qty, cost, qty * cost,
                 "2026-09-09", "2026-09-09 09:30:00"),
            )
            conn.execute(
                "INSERT INTO paper_position_lots(cycle_id, account_id, code, name, industry,"
                " qty, remaining_qty, cost, acquired_at, available_date, asset_type,"
                " cost_fee_included, is_t_base) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stock_t1', 1, 1)",
                (cycle_id, ACCOUNT, CODE, f"测试股_{CODE}", "Tech", qty, qty, cost,
                 "2026-09-08 10:00:00", "2026-09-09"),
            )
            PT._sync_positions(conn, asof_day=DAY1)

    # ── 观察工具 ────────────────────────────────────────────────────────────
    def has_scan_run_table(self):
        with PT._db() as conn:
            return bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_risk_scan_runs'"
            ).fetchone())

    def scan_runs(self):
        if not self.has_scan_run_table():
            return []
        with PT._db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT cycle_id,asof_date,scan_minute,status,attempt"
                " FROM paper_risk_scan_runs ORDER BY id").fetchall()]

    def audit_markers(self):
        with PT._db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT detail FROM paper_audit WHERE event='risk_scan_state' ORDER BY id"
            ).fetchall()]

    def sell_orders(self, cycle_id=None):
        with PT._db() as conn:
            sql = "SELECT id,cycle_id FROM paper_orders WHERE side='sell'"
            params = ()
            if cycle_id is not None:
                sql += " AND cycle_id=?"
                params = (cycle_id,)
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def sell_fills(self):
        with PT._db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM paper_fills WHERE side='sell'").fetchone()[0]

    def remaining(self, cycle_id):
        with PT._db() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(remaining_qty),0) FROM paper_position_lots WHERE cycle_id=?",
                (cycle_id,)).fetchone()
            return int(row[0])

    def reviews(self, cycle_id):
        with PT._db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM paper_position_reviews WHERE cycle_id=?",
                (cycle_id,)).fetchone()[0]

    def run_monitor(self, day, clock_values):
        clock = _Clock(clock_values)
        with mock.patch.object(PT, "dt", clock.shim()):
            return PT.monitor_risk(day)


# ─── R16-C1：跨周期同分钟抑制 ───────────────────────────────────────────────
def case_c1():
    r = Repro()
    r.setup()
    try:
        c8 = r.active_cycle()
        r.insert_lot(c8)
        r.quotes[CODE] = _fresh_exit_quote(DAY1)
        first = r.run_monitor(DAY1, [FIXED_MINUTE] * 6)

        c9 = r.add_cycle()
        r.activate(c9)
        r.insert_lot(c9)
        r.quotes[CODE] = _fresh_exit_quote(DAY1)
        second = r.run_monitor(DAY1, [FIXED_MINUTE] * 6)

        runs = r.scan_runs()
        if runs:
            cycles = sorted({int(x["cycle_id"]) for x in runs})
            ok = c9 in cycles
            detail = f"scan_runs cycles={cycles} (c8={c8}, c9={c9})"
        else:
            # 修复前：唯一权威是 audit 标记，没有 cycle 身份
            ok = second.get("status") != "already_scanned"
            detail = (f"first={first.get('status')} second={second.get('status')}; "
                      f"audit markers={len(r.audit_markers())}")
        return ok, detail
    finally:
        r.teardown()


# ─── R16-C2：同分钟不同 asof 碰撞 ───────────────────────────────────────────
def case_c2():
    r = Repro()
    r.setup()
    try:
        c8 = r.active_cycle()
        r.insert_lot(c8)
        r.quotes[CODE] = _fresh_exit_quote(DAY1)
        r.run_monitor(DAY1, [FIXED_MINUTE] * 6)
        # 同一 runtime 分钟、同一 cycle，但 asof 换成另一天
        r.quotes[CODE] = _fresh_exit_quote(DAY2)
        second = r.run_monitor(DAY2, [FIXED_MINUTE] * 6)

        runs = r.scan_runs()
        if runs:
            asofs = sorted({x["asof_date"] for x in runs})
            ok = len(asofs) >= 2
            detail = f"scan_runs asofs={asofs}"
        else:
            ok = second.get("status") != "already_scanned"
            detail = (f"second={second.get('status')}; "
                      f"audit markers={len(r.audit_markers())}")
        return ok, detail
    finally:
        r.teardown()


# ─── R16-C3：分钟边界 orphan running ────────────────────────────────────────
def case_c3():
    r = Repro()
    r.setup()
    try:
        c8 = r.active_cycle()
        r.insert_lot(c8)
        r.quotes[CODE] = _fresh_exit_quote(DAY1)
        boundary = dt.datetime(2026, 9, 10, 14, 50, 59)
        next_minute = dt.datetime(2026, 9, 10, 14, 51, 0)

        # wrapper 第一次取时钟 = 14:50:59；claim 之后 impl 内部再取一次 = 14:51:00，
        # 随后注入确定性异常。
        boom = RuntimeError("injected provider failure")
        with mock.patch.object(PT, "_quotes", side_effect=boom):
            try:
                r.run_monitor(DAY1, [boundary, next_minute, next_minute, next_minute])
            except Exception:
                pass

        runs = r.scan_runs()
        if runs:
            orphans = [x for x in runs if x["status"] == "running"]
            ok = not orphans and any(x["status"] == "failed" for x in runs)
            detail = f"scan_runs={[(x['scan_minute'], x['status']) for x in runs]}"
        else:
            markers = []
            for row in r.audit_markers():
                import json
                d = json.loads(row["detail"])
                markers.append((d.get("scan_minute"), d.get("status")))
            running = [m for m in markers if m[1] == "running"]
            failed = [m for m in markers if m[1] == "failed"]
            # orphan = 存在 running 却没有**同一身份**的 failed
            orphan = bool(running) and not any(
                f[0] in {x[0] for x in running} for f in failed)
            ok = not orphan
            detail = f"audit markers={markers} orphan_running={orphan}"
        return ok, detail
    finally:
        r.teardown()


# ─── R16-C4：quote I/O 期间 cycle rollover ─────────────────────────────────
def case_c4():
    r = Repro()
    r.setup()
    try:
        c8 = r.active_cycle()
        r.insert_lot(c8)
        r.quotes[CODE] = _fresh_exit_quote(DAY1)

        state = {"c9": None}

        def rollover_quotes(codes, asof_date=None):
            # 行情返回**之前**把 active cycle 切到 cycle 9，且 cycle 9 同样持有 CODE
            if state["c9"] is None:
                c9 = r.add_cycle()
                r.activate(c9)
                r.insert_lot(c9)
                state["c9"] = c9
            return {c: r.quotes[c] for c in codes if c in r.quotes}

        with mock.patch.object(PT, "_quotes", side_effect=rollover_quotes):
            try:
                r.run_monitor(DAY1, [FIXED_MINUTE] * 6)
                raised = None
            except Exception as exc:
                raised = f"{type(exc).__name__}: {exc}"

        c9 = state["c9"]
        c9_orders = r.sell_orders(c9)
        c9_reviews = r.reviews(c9) if c9 else 0
        total_fills = r.sell_fills()
        c8_remaining = r.remaining(c8)
        c9_remaining = r.remaining(c9) if c9 else None

        # 修复后的正确行为：fail closed，且 cycle 9 一行未被触碰。
        clean = (not c9_orders) and c9_reviews == 0 and total_fills == 0
        if raised and "RiskScanCycleChanged" in raised and clean:
            ok = True
            detail = f"fail closed: {raised}; c9 orders=0 reviews=0 fills=0"
        elif clean and raised:
            ok = True
            detail = f"raised {raised}; c9 orders=0 reviews=0 fills=0"
        else:
            ok = False
            detail = (f"raised={raised}; c9_orders={len(c9_orders)} c9_reviews={c9_reviews} "
                      f"fills={total_fills} c8_remaining={c8_remaining} "
                      f"c9_remaining={c9_remaining}")
        return ok, detail
    finally:
        r.teardown()


def main() -> int:
    print(f"repo root: {ROOT}")
    print(f"HEAD      : {os.popen('git rev-parse --short=12 HEAD').read().strip()}")
    print()
    cases = [
        ("R16-C1 cross-cycle same-minute suppression", case_c1,
         "cycle 9 must get its own scan run"),
        ("R16-C2 different-asof same-minute collision", case_c2,
         "both asof values must be claimable"),
        ("R16-C3 minute-boundary orphan marker", case_c3,
         "no running identity without a matching failed"),
        ("R16-C4 cycle rollover during quote fetch", case_c4,
         "fail closed; cycle 9 untouched"),
    ]
    results = []
    for name, fn, expectation in cases:
        ok, detail = fn()
        verdict = "NOT REPRODUCED" if ok else "REPRODUCED"
        results.append((name, verdict))
        print(f"{name}: {verdict}")
        print(f"    expect : {expectation}")
        print(f"    actual : {detail}")
        print()
    reproduced = [n for n, v in results if v == "REPRODUCED"]
    print(f"SUMMARY: {len(reproduced)}/{len(results)} reproduced on this tree")
    for name, verdict in results:
        print(f"  {verdict:<15} {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
