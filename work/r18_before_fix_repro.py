# -*- coding: utf-8 -*-
"""R18 before-fix 差分复现（在**未修改**的 R18 base 上运行）。

覆盖规格 §15-§19：

    R18-C1  tomorrow candidate 被 today replacement selector 选中
    R18-C2  tomorrow candidate 真实制造 today SELL（production path）
    R18-C3  historical slot upgrade 读到未来 review
    R18-C4  slot context 重新解析 active cycle（读到别的周期）
    R18-C5  在途 pending BUY 席位跨周期（cycle 9 的买单占掉 cycle 8 的席位）
    R18-C6  簇画像 signal 证据没有 as-of 上界（future leakage）
    R18-C7  _buy_order 的 pending 席位跨周期（真实开仓主路径）
    R18-C8  _buy_order 的预算漏传 as-of（借位前后版本不一致）

用法（仓库根目录）::

    python work/r18_before_fix_repro.py
"""
from __future__ import annotations

import datetime as dt
import inspect
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

DAY = dt.date(2026, 9, 10)        # D
DAY_NEXT = dt.date(2026, 9, 11)   # D+1
DAY_PREV = dt.date(2026, 9, 9)    # D-1


class Repro:
    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "r18_repro.sqlite3")
        self._patches = []
        self.quotes = {}

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
            mock.patch.object(PT, "_completed_kline", return_value=None),
            # 解除入场冻结：否则 replacement BUY 会先在 freeze 闸门被挡，
            # 走不到本 case 要证明的 signal freshness 日期不匹配。
            mock.patch.dict(os.environ, {"PAPER_ENTRY_FREEZE": "0"}),
        ]
        for p in self._patches:
            p.start()
        PT._ENTRY_FREEZE_CACHE.update({"at": 0.0, "status": None})
        PT.init_db()
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='running' WHERE id=1")
            # 让策略账户处于 running：否则 replacement BUY 会先被周期/账户闸门拒绝，
            # 走不到 signal freshness（那才是本 case 要证明的日期不匹配）。
            conn.execute("UPDATE paper_accounts SET status='running'")

    def teardown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _quotes(self, codes, asof_date=None):
        return {c: self.quotes[c] for c in codes if c in self.quotes}

    def cycle_id(self):
        with PT._db() as conn:
            return int(conn.execute(
                "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
                " ORDER BY id DESC LIMIT 1").fetchone()[0])

    def add_signal(self, *, account_id, code, intended_date, signal_date=None,
                   entry_score=0.0, t_score=0.0, rank_score=0.0, status="pending"):
        signal_date = signal_date or intended_date
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, account_id)
            cur = conn.execute(
                "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
                "close_price,rank_score,t_tier,t_score,payload,status,created_at,"
                "strategy_id,strategy_version,strategy_checksum) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, signal_date, intended_date, code, f"测试股_{code}", 10.0,
                 rank_score, "A", t_score,
                 PT._json({"decision": {"entry_model": {"score": entry_score}}}),
                 status, f"{signal_date} 15:00:00", *stamp),
            )
            return int(cur.lastrowid)

    def add_lot(self, *, account_id, code, qty=100, cost=10.0, cycle_id=None):
        cycle_id = self.cycle_id() if cycle_id is None else cycle_id
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, account_id)
            order = conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
                "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
                "execution_verified,execution_status) "
                "VALUES(?,'buy',?,?,?,?,?,?,5.0,'filled','seed_buy','{}',?,?,'market','seed',"
                "?,?,?,?,1,'verified')",
                (account_id, code, f"测试股_{code}", qty, cost, cost, qty * cost,
                 "2026-09-08 09:30:00", "2026-09-08 09:30:00", *stamp, cycle_id),
            )
            order_id = int(order.lastrowid)
            conn.execute(
                "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
                "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
                "is_t_base,source_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,'stock_t1',1,1,?)",
                (cycle_id, account_id, code, f"测试股_{code}", "Tech", qty, qty, cost,
                 "2026-09-08 10:00:00", "2026-09-09", order_id),
            )
            PT._sync_positions(conn, asof_day=DAY)
            return order_id

    def add_review(self, *, account_id, code, review_date, score, action="hold",
                   cycle_id=None):
        cycle_id = self.cycle_id() if cycle_id is None else cycle_id
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_position_reviews(cycle_id,account_id,code,review_date,"
                "score,grade,action,market_value,position_pct,reasons,detail,created_at) "
                "VALUES(?,?,?,?,?,'观察',?,1000.0,1.0,'fixture','{}',?)",
                (cycle_id, account_id, code, review_date, score, action,
                 f"{review_date} 14:50:00"),
            )

    def positions(self, account_id):
        with PT._db() as conn:
            return [p for p in PT._position_rows(conn, asof_day=DAY)
                    if p["account_id"] == account_id]

    def add_pending_buy(self, *, account_id, code, cycle_id,
                        status="pending_limit"):
        """一条可执行的在途 BUY 委托（占席位）。"""
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, account_id)
            conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                "status,reason,risk_payload,origin,created_at,strategy_id,"
                "strategy_version,strategy_checksum,cycle_id) VALUES(?,'buy',?,?,100,"
                "10.0,?,'fixture','{}','strategy',?,?,?,?,?)",
                (account_id, code, f"测试股_{code}", status,
                 f"{DAY.isoformat()} 10:00:00", *stamp, cycle_id),
            )

    def new_cycle(self, key):
        with PT._db(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,started_at,"
                "created_at,updated_at) VALUES(?,'running',1000000.0,'balanced',?,?,?)",
                (key, f"{DAY_NEXT.isoformat()} 00:00:00",
                 f"{DAY_NEXT.isoformat()} 00:00:00", f"{DAY_NEXT.isoformat()} 00:00:00"))
            return int(cur.lastrowid)

    def positions_for_cycle(self, cycle_id, account_id):
        """显式周期的持仓（C4 需要与 active cycle 不同的周期）。"""
        import paper_position_read_model as PPRM
        with PT._db() as conn:
            rows = PPRM.positions_for_cycle(
                conn, cycle_id, account_id=account_id, asof_day=DAY)
        return [dict(row) for row in rows]


# ─── R18-C1：tomorrow candidate 被 today selector 选中 ────────────────────
def case_c1():
    r = Repro()
    r.setup()
    try:
        account = "tq_breakout"
        sig_next = r.add_signal(account_id=account, code="600002",
                                intended_date=DAY_NEXT.isoformat(),
                                entry_score=100.0, t_score=100.0, rank_score=100.0)
        with PT._db() as conn:
            best = PT._best_replacement_candidate(conn, account, DAY, set())
        got_future = bool(best and int(best["signal_id"]) == sig_next)
        return (not got_future), (
            f"selector returned signal_id={best and best['signal_id']} "
            f"intended_date={best and best['intended_date']} (future signal={sig_next} "
            f"intended {DAY_NEXT.isoformat()}); asof={DAY.isoformat()}"
        )
    finally:
        r.teardown()


# ─── R18-C2：tomorrow candidate 真实制造 today SELL ──────────────────────
def case_c2():
    r = Repro()
    r.setup()
    try:
        account = "tq_breakout"
        held = "600001"
        r.add_lot(account_id=account, code=held, qty=100, cost=10.0)
        r.add_review(account_id=account, code=held, review_date=DAY.isoformat(), score=40.0)
        # 明天的高分候选（同账户，不同代码）
        fut = r.add_signal(account_id=account, code="600002",
                           intended_date=DAY_NEXT.isoformat(),
                           entry_score=100.0, t_score=100.0, rank_score=100.0)
        r.quotes[held] = {
            "code": held, "name": f"测试股_{held}", "price": 10.0, "high": 10.1,
            "low": 9.9, "pct": 0.0, "amount": 100000.0, "volume": 10000.0,
            "turnover": 1.0, "quote_source": "live",
            "quote_at": f"{DAY.isoformat()} 14:50:00",
            "quote_validation": "cross_source_checked",
        }
        r.quotes["600002"] = dict(r.quotes[held], code="600002", name="测试股_600002")

        with PT._db() as conn:
            best = PT._best_replacement_candidate(conn, account, DAY, {held})
        position = r.positions(account)[0]
        with PT._db() as conn:
            review = PT._position_quality_score(
                conn, position, r.quotes[held], DAY, cycle_id=r.cycle_id(),
                replacement=best, nav=100000.0, market={},
            )
        action, reason = PT.PReview.decide_action(
            review, dict(position, available_qty=100), {"fresh": True, "reason": "fixture"},
            0, policy=PT.REVIEW_POLICY,
        )
        rotation = None
        if best:
            with PT._db() as conn:
                rotation = PT._rotation_buy_candidate(
                    conn, {"id": account}, best, r.quotes.get("600002") or {},
                    {}, [], DAY,
                )
        future_selected = bool(best and int(best["signal_id"]) == fut)
        caused_by_future = future_selected and action == "consolidation_exit"
        rotation_status = (rotation or {}).get("status")
        buy_rejected = bool(rotation and not rotation.get("filled"))
        # 旧行为：未来候选 -> today 卖出决策 + replacement BUY 被日期不匹配拒绝
        date_mismatch = rotation_status == "signal_expired" and \
            DAY_NEXT.isoformat() in str((rotation or {}).get("reason") or "")
        ok = caused_by_future and buy_rejected and date_mismatch
        return (not ok), (
            f"selected_future={future_selected} action={action} "
            f"review_score={review['score']} replacement_score={review.get('replacement_score')} "
            f"rotation_status={rotation_status} "
            f"rotation_reason={str((rotation or {}).get('reason'))[:60]!r} "
            f"reason={reason[:50]!r}"
        )
    finally:
        r.teardown()


# ─── R18-C3：historical slot upgrade 读到未来 review ─────────────────────
def case_c3():
    r = Repro()
    r.setup()
    try:
        account = "tq_breakout"
        held = "600001"
        r.add_lot(account_id=account, code=held, qty=100, cost=10.0)
        # D 的 review = 70（强），D+1 的 review = 20（弱）
        r.add_review(account_id=account, code=held, review_date=DAY.isoformat(), score=70.0)
        r.add_review(account_id=account, code=held, review_date=DAY_NEXT.isoformat(), score=20.0)
        cand = r.add_signal(account_id=account, code="600003",
                            intended_date=DAY.isoformat(), entry_score=60.0,
                            t_score=60.0, rank_score=60.0)
        with PT._db() as conn:
            signal = dict(conn.execute("SELECT * FROM paper_signals WHERE id=?", (cand,)).fetchone())
        positions = r.positions(account)
        kwargs = {}
        if "cycle_id" in inspect.signature(PT._slot_upgrade_context).parameters:
            kwargs["cycle_id"] = r.cycle_id()
        with PT._db() as conn:
            ctx = PT._slot_upgrade_context(conn, account, signal, positions, DAY, **kwargs)
        weakest = ctx.get("weakest")
        got = weakest and float(weakest.get("score"))
        # 正确行为：asof=D 必须看到 70，绝不能看到 D+1 的 20
        ok = got == 70.0
        return ok, (
            f"asof={DAY.isoformat()} weakest_score={got} "
            f"(D review=70, D+1 review=20; expected 70) state={ctx.get('state')}"
        )
    finally:
        r.teardown()


# ─── R18-C4：slot context 重新解析 active cycle ──────────────────────────
def case_c4():
    r = Repro()
    r.setup()
    try:
        account = "tq_breakout"
        held = "600001"
        cycle8 = r.cycle_id()
        r.add_lot(account_id=account, code=held, qty=100, cost=10.0, cycle_id=cycle8)
        r.add_review(account_id=account, code=held, review_date=DAY.isoformat(),
                     score=70.0, cycle_id=cycle8)
        # 新建 cycle 9 并置为 active，其中该持仓 review = 20
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (cycle8,))
            cur = conn.execute(
                "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,started_at,"
                "created_at,updated_at) VALUES(?,'running',1000000.0,'balanced',?,?,?)",
                (f"r18-c4-{cycle8}", f"{DAY_NEXT.isoformat()} 00:00:00",
                 f"{DAY_NEXT.isoformat()} 00:00:00", f"{DAY_NEXT.isoformat()} 00:00:00"))
            cycle9 = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO paper_position_reviews(cycle_id,account_id,code,review_date,"
                "score,grade,action,market_value,position_pct,reasons,detail,created_at) "
                "VALUES(?,?,?,?,?,'观察','hold',1000.0,1.0,'fixture','{}',?)",
                (cycle9, account, held, DAY.isoformat(), 20.0, f"{DAY.isoformat()} 14:50:00"),
            )
        cand = r.add_signal(account_id=account, code="600004",
                            intended_date=DAY.isoformat(), entry_score=60.0,
                            t_score=60.0, rank_score=60.0)
        with PT._db() as conn:
            signal = dict(conn.execute("SELECT * FROM paper_signals WHERE id=?", (cand,)).fetchone())
        positions = r.positions_for_cycle(cycle8, account)
        sig = inspect.signature(PT._slot_upgrade_context)
        if "cycle_id" in sig.parameters:
            with PT._db() as conn:
                ctx = PT._slot_upgrade_context(
                    conn, account, signal, positions, DAY, cycle_id=cycle8)
        else:
            with PT._db() as conn:
                ctx = PT._slot_upgrade_context(conn, account, signal, positions, DAY)
        weakest = ctx.get("weakest")
        got = weakest and float(weakest.get("score"))
        # 正确行为：显式 cycle8 ⇒ 只能读到 70；旧行为会读 active cycle9 的 20
        ok = got == 70.0
        return ok, (
            f"requested cycle={cycle8} active_cycle={cycle9} weakest_score={got} "
            f"(cycle8=70, cycle9=20; expected 70) has_cycle_kwarg={'cycle_id' in sig.parameters}"
        )
    finally:
        r.teardown()


# ─── R18-C5：在途 pending BUY 席位跨周期 ─────────────────────────────────
def case_c5():
    r = Repro()
    r.setup()
    try:
        account = "tq_breakout"
        cycle8 = r.cycle_id()
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (cycle8,))
        cycle9 = r.new_cycle(f"r18-c5-{cycle8}")
        for index in range(3):
            r.add_pending_buy(account_id=account, code=f"6001{index:02d}",
                              cycle_id=cycle9)
        budget = {"pool_limit": 3, "limits": {account: 2, "sector_rotation": 3},
                  "allocation_version": "slots-v1"}
        signal = {"code": "600009", "payload": "{}"}
        # base 的 _slot_upgrade_context 没有 cycle_id 参数（缺陷本身的一部分）；
        # 有则显式传，没有就按旧签名调用。
        has_kwarg = "cycle_id" in inspect.signature(
            PT._slot_upgrade_context).parameters
        kwargs = {"cycle_id": cycle8} if has_kwarg else {}
        with mock.patch.object(
                PT, "_dynamic_position_limits",
                lambda conn, *, cycle_id=None, asof_day=None: dict(budget)):
            with PT._db() as conn:
                ctx8 = PT._slot_upgrade_context(
                    conn, account, signal, [], DAY, **kwargs)
        donors = [item["account_id"] for item in ctx8["donors"]]
        leaked = "shared_pool" not in donors
        return (not leaked), (
            f"requested cycle={cycle8} active_cycle={cycle9} "
            f"cycle9_pending_buys=3 cycle8 occupied_pool donor={donors} "
            f"has_cycle_kwarg={has_kwarg} "
            f"(cycle 8 无在途买单 ⇒ shared_pool donor 必须存在)"
        )
    finally:
        r.teardown()


# ─── R18-C6：簇画像 signal 证据没有 as-of 上界 ────────────────────────────
def case_c6():
    r = Repro()
    r.setup()
    try:
        account = "tq_breakout"
        r.add_signal(account_id=account, code="600301", intended_date=DAY.isoformat())
        r.add_signal(account_id=account, code="600302", intended_date=DAY_NEXT.isoformat())
        with PT._db() as conn:
            profile = PT._strategy_cluster_profiles(conn, DAY, [account])[account]
        leaked = sorted(
            code for code in profile["signals"] if code == "600302")
        return (not leaked), (
            f"asof={DAY.isoformat()} signals={sorted(profile['signals'])} "
            f"leaked_future={leaked} (600302 属于 {DAY_NEXT.isoformat()}，必须排除)"
        )
    finally:
        r.teardown()


# ─── R18-C7 / C8：真实 _buy_order 主路径的周期与 as-of 连续性 ───────────────
_ALL_ACCOUNTS = ("tq_breakout", "trend_pullback", "sector_rotation",
                 "reported_profit_breakout", "main_force_top10")
_MARKET = {"light": "green", "overseas": {"light": "green", "advice": "x"},
           "breadth": 0.5, "sentiment": "neutral"}


class BuyOrderRepro(Repro):
    """在 Repro 之上加：真实 _buy_order 驱动 + 周期翻转。

    ``add_signal`` / ``add_lot`` / ``add_review`` / ``add_pending_buy`` 全部复用
    基类；这里只补"翻转 active cycle"和"驱动真实 _buy_order"。
    """

    def activate(self, key):
        requested = self.cycle_id()
        with PT._db() as conn:
            enabled = conn.execute(
                "SELECT enabled_strategies FROM paper_cycles WHERE id=?",
                (requested,)).fetchone()["enabled_strategies"]
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (requested,))
        with PT._db(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,started_at,"
                "created_at,updated_at,enabled_strategies) VALUES(?,'running',1000000.0,"
                "'balanced',?,?,?,?)",
                (key, f"{DAY.isoformat()} 00:00:00", f"{DAY.isoformat()} 00:00:00",
                 f"{DAY.isoformat()} 00:00:00", enabled))
            active = int(cur.lastrowid)
        with PT._db(immediate=True) as conn:
            conn.executemany(
                "UPDATE paper_accounts SET cycle_id=?, status='running' WHERE id=?",
                [(active, account_id) for account_id in _ALL_ACCOUNTS])
        return requested, active

    def run_buy_order(self, signal_id, code, account_id="tq_breakout"):
        with PT._db() as conn:
            signal = dict(conn.execute("SELECT * FROM paper_signals WHERE id=?",
                                       (signal_id,)).fetchone())
        self.quotes[code] = {
            "code": code, "name": f"测试股_{code}", "price": 10.0, "high": 10.1,
            "low": 9.9, "pct": 0.0, "amount": 100000.0, "volume": 10000.0,
            "turnover": 1.0, "quote_source": "live",
            "quote_at": f"{DAY.isoformat()} 14:50:00",
            "quote_validation": "cross_source_checked",
        }
        with PT._db(immediate=True) as conn:
            result = PT._buy_order(
                conn, {"id": account_id}, signal, self.quotes[code],
                dict(_MARKET), [], DAY, all_quotes=dict(self.quotes))
        with PT._db() as conn:
            order = conn.execute(
                "SELECT risk_payload FROM paper_orders WHERE signal_id=? ORDER BY id DESC LIMIT 1",
                (signal_id,)).fetchone()
        payload = PT._loads(order["risk_payload"], {}) if order is not None else {}
        return result, payload

    def sync(self):
        with PT._db(immediate=True) as conn:
            PT._sync_positions(conn, asof_day=DAY)


def case_c7():
    """真实 _buy_order 把上一个周期的在途买单算进本周期承诺席位。"""
    r = BuyOrderRepro()
    r.setup()
    try:
        requested, active = r.activate("r18-c7-requested")
        for index in range(3):
            r.add_pending_buy(account_id="tq_breakout", code=f"6001{index:02d}",
                              cycle_id=requested)
        sig = r.add_signal(account_id="tq_breakout", code="600900",
                           intended_date=DAY.isoformat(), signal_date=DAY_PREV.isoformat())
        _result, payload = r.run_buy_order(sig, "600900")
        gate = payload.get("position_count_gate") or {}
        leaked = bool(gate.get("committed")) or bool(gate.get("pool_current"))
        return (not leaked), (
            f"requested cycle={requested} active={active} cycle{requested}_pending_buys=3 "
            f"gate.committed={gate.get('committed')} gate.pool_current={gate.get('pool_current')} "
            f"(expected 0/0: 上一个周期的在途单不属于本周期)"
        )
    finally:
        r.teardown()


def case_c8():
    """真实 _buy_order 的预算漏传 as-of：借位写进另一个版本行。"""
    r = BuyOrderRepro()
    r.setup()
    try:
        _requested, active = r.activate("r18-c8-requested")
        codes = ["600001", "600002", "600003"]
        for code in codes:
            r.add_lot(account_id="tq_breakout", code=code, cycle_id=active)
            r.add_lot(account_id="sector_rotation", code=code, cycle_id=active)
            r.add_review(account_id="tq_breakout", code=code, review_date=DAY.isoformat(),
                         score=20.0, cycle_id=active)
        r.sync()
        only_today = (dt.date.today() - dt.timedelta(days=5)).isoformat()
        r.add_signal(account_id="tq_breakout", code="600888", intended_date=only_today)
        r.add_signal(account_id="sector_rotation", code="600888", intended_date=only_today)

        with PT._db() as conn:
            bounded = PT._dynamic_position_limits(conn, cycle_id=active, asof_day=DAY)
        with PT._db() as conn:
            unbounded = PT._dynamic_position_limits(conn, cycle_id=active)
        sig = r.add_signal(account_id="tq_breakout", code="600901",
                           intended_date=DAY.isoformat(), signal_date=DAY_PREV.isoformat(),
                           entry_score=90.0, t_score=90.0, rank_score=90.0)
        _result, payload = r.run_buy_order(sig, "600901")
        gate = payload.get("position_count_gate") or {}
        borrow = payload.get("slot_borrow") or {}
        mismatched = bool(borrow.get("allowed")) and (
            borrow.get("allocation_version") != gate.get("allocation_version")
            or borrow.get("limits_after", {}).get("tq_breakout") != gate.get("limit")
        )
        return (not mismatched), (
            f"active={active} bounded(asof=D)={bounded['allocation_version']} "
            f"unbounded={unbounded['allocation_version']} "
            f"borrow.version={borrow.get('allocation_version')} "
            f"gate.version={gate.get('allocation_version')} gate.limit={gate.get('limit')} "
            f"borrow.limits_after.tq={borrow.get('limits_after', {}).get('tq_breakout')} "
            f"(expected 借位前后同一版本行)"
        )
    finally:
        r.teardown()


def main() -> int:
    print(f"repo root: {ROOT}")
    print(f"HEAD      : {os.popen('git rev-parse --short=12 HEAD').read().strip()}")
    print()
    cases = [
        ("R18-C1 tomorrow candidate selected for today", case_c1),
        ("R18-C2 tomorrow candidate triggers today rotation/sell", case_c2),
        ("R18-C3 historical slot context reads future review", case_c3),
        ("R18-C4 slot context re-resolves active cycle", case_c4),
        ("R18-C5 pending BUY slots cross the cycle boundary", case_c5),
        ("R18-C6 cluster signal evidence has no as-of bound", case_c6),
        ("R18-C7 _buy_order pending slots cross the cycle boundary", case_c7),
        ("R18-C8 _buy_order budget loses the as-of (borrow version drift)", case_c8),
    ]
    results = []
    for name, fn in cases:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"EXCEPTION {type(exc).__name__}: {exc}"
        verdict = "NOT REPRODUCED" if ok else "REPRODUCED"
        results.append((name, verdict))
        print(f"{name}: {verdict}")
        print(f"    actual : {detail}")
        print()
    reproduced = [n for n, v in results if v == "REPRODUCED"]
    print(f"SUMMARY: {len(reproduced)}/{len(results)} reproduced on this tree")
    for name, verdict in results:
        print(f"  {verdict:<15} {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
