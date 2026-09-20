# -*- coding: utf-8 -*-
"""R19 before-fix 差分复现（必须在**未修改**的 R19 base 上先跑）。

覆盖规格 §16-§20：

    R19-C1  future adaptive allocation 改变 historical capital budget
    R19-C2  future cluster evidence 改变 historical BUY sizing
    R19-C3  wrong-cycle reservation 被普通 strategy BUY 消费
    R19-C4  普通 strategy BUY 绕过 execution_planner.commit_fill

用法（仓库根目录）::

    python work/r19_before_fix_repro.py

判定约定（与 R18 一致）：脚本报告 ``REPRODUCED`` = 缺陷在当前树上成立。
每条 case 都带**非空门禁**：先证明"未来证据本身是有效的/路径本身是可达的"，
再断言有界路径没有把未来事实带进历史回放 —— 否则测试是空门禁。
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

ACCOUNT = "tq_breakout"
OTHER = "sector_rotation"
CODE = "600901"
DAY = dt.date(2026, 9, 10)        # D：历史 as-of
DAY_NEXT = dt.date(2026, 9, 11)   # D+1
DAY_PREV = dt.date(2026, 9, 9)    # D-1
CAPITAL = 1_000_000.0
MARKET = {
    "light": "green",
    "overseas": {"light": "green", "advice": "fixture"},
    "breadth": 0.5,
    "sentiment": "neutral",
}


def quote(code, price=10.0, pct=1.5):
    return {
        "code": code, "name": f"测试股_{code}", "price": price, "pct": pct,
        "open_price": round(price * 0.998, 2),
        "high": round(price * 1.01, 2), "low": round(price * 0.99, 2),
        "vol_ratio": 2.0, "main_pct": 2.0,
        "main_net": 3_000_000.0, "super_net": 2_000_000.0,
        "amount": 50_000_000.0, "volume": 10_000.0, "turnover": 1.0,
        "quote_at": f"{DAY.isoformat()} 10:00:00",
        "quote_source": "live", "source": "unit_test_injection",
        "quote_validation": "cross_source_checked", "risk_flag": 0,
    }


class Repro:
    """真实 ``init_db()`` 账本 + 依赖注入；不手写 orders/fills 结构。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "r19_repro.sqlite3")
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
            # 入场时机状态机：内置策略首发必返回 triggered，会让 BUY 停在
            # deferred_capacity，走不到本 case 要证明的资金/成交事实。
            mock.patch.object(PT, "ET", None),
            mock.patch.dict(os.environ, {"PAPER_ENTRY_FREEZE": "0"}),
        ]
        for p in self._patches:
            p.start()
        PT._ENTRY_FREEZE_CACHE.update({"at": 0.0, "status": None})
        PT.init_db()
        PT.start_new_cycle(capital=CAPITAL, include_dashboard=False)

    def teardown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _quotes(self, codes, asof_date=None):
        return {c: self.quotes[c] for c in codes if c in self.quotes}

    # ── 账本 fixtures ────────────────────────────────────────────────────
    def cycle_id(self):
        with PT._db() as conn:
            return int(conn.execute(
                "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
                " ORDER BY id DESC LIMIT 1").fetchone()[0])

    def new_cycle(self, key):
        with PT._db() as conn:
            return int(conn.execute(
                "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,started_at,"
                "created_at,updated_at) VALUES(?,'running',?,'balanced',?,?,?)",
                (key, CAPITAL, f"{DAY.isoformat()} 00:00:00",
                 f"{DAY.isoformat()} 00:00:00", f"{DAY.isoformat()} 00:00:00")).lastrowid)

    def activate(self, key):
        requested = self.cycle_id()
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (requested,))
        active = self.new_cycle(key)
        assert self.cycle_id() == active, "fixture 没把 active cycle 翻过去"
        return requested, active

    def set_account_params(self, account_id, **params):
        with PT._db(immediate=True) as conn:
            row = conn.execute("SELECT params FROM paper_accounts WHERE id=?",
                               (account_id,)).fetchone()
            current = PT._loads(row["params"] if row is not None else None, {}) or {}
            current.update(params)
            conn.execute("UPDATE paper_accounts SET params=? WHERE id=?",
                         (PT._json(current), account_id))

    def add_signal(self, *, account_id=ACCOUNT, code=CODE, intended_date=DAY.isoformat(),
                   signal_date=None, entry_score=90.0, t_score=90.0, rank_score=90.0,
                   status="pending", pick=None):
        signal_date = signal_date or intended_date
        payload = {"pick": pick if pick is not None else
                   {"code": code, "score": 0.8, "price": 10.0},
                   "decision": {"entry_model": {"score": entry_score}}}
        with PT._db(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
                "close_price,rank_score,t_tier,t_score,payload,status,created_at,"
                "strategy_id,strategy_version,strategy_checksum) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, signal_date, intended_date, code, f"测试股_{code}", 10.0,
                 rank_score, "A", t_score, PT._json(payload), status,
                 f"{signal_date} 15:00:00", *PT._strategy_stamp(conn, account_id)),
            )
            return int(cur.lastrowid)

    def add_lot(self, *, account_id, code, qty=100, cost=10.0, cycle_id):
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, account_id)
            order_id = conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
                "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
                "execution_verified,execution_status) VALUES(?,'buy',?,?,?,?,?,?,5.0,'filled',"
                "'seed_buy','{}',?,?,'market','seed',?,?,?,?,1,'verified')",
                (account_id, code, f"测试股_{code}", qty, cost, cost, qty * cost,
                 "2026-08-20 09:30:00", "2026-08-20 09:30:00", *stamp, cycle_id)).lastrowid
            conn.execute(
                "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
                "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
                "is_t_base,source_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,'stock_t1',1,1,?)",
                (cycle_id, account_id, code, f"测试股_{code}", "Tech", qty, qty, cost,
                 "2026-08-20 10:00:00", "2026-08-21", order_id))
            return order_id

    def sync(self):
        with PT._db(immediate=True) as conn:
            PT._sync_positions(conn, asof_day=DAY)

    def _budget_args(self, account_id):
        quotes = dict(self.quotes)
        with PT._db() as conn:
            positions, _value, nav, _ind, _codes = PT._shared_account_exposure(
                conn, quotes, DAY)
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone())
        return dict(account=account, nav=nav, positions=positions, quotes=quotes,
                    market=dict(MARKET))

    def budget(self, *, cycle_id=None, asof_day=None, account_id=ACCOUNT):
        """调真实 ``_strategy_pool_budget``；base 树会忽略两个新 kwarg。"""
        kwargs = self._budget_args(account_id)
        with PT._db() as conn:
            try:
                return PT._strategy_pool_budget(
                    conn, cycle_id=cycle_id, asof_day=asof_day, **kwargs)
            except TypeError:
                return PT._strategy_pool_budget(conn, **kwargs)

    def run_buy_order(self, *, signal_id, code, asof_day=DAY):
        with PT._db() as conn:
            signal = dict(conn.execute(
                "SELECT * FROM paper_signals WHERE id=?", (signal_id,)).fetchone())
        with PT._db(immediate=True) as conn:
            result = PT._buy_order(
                conn, {"id": ACCOUNT}, signal, dict(self.quotes[code]), dict(MARKET),
                [], asof_day, all_quotes=dict(self.quotes),
            )
        with PT._db() as conn:
            order = conn.execute(
                "SELECT * FROM paper_orders WHERE signal_id=? ORDER BY id DESC LIMIT 1",
                (signal_id,)).fetchone()
        return result, (dict(order) if order is not None else None)


# ─── R19-C1：future adaptive allocation 改变 historical budget ────────────
def case_c1():
    r = Repro()
    r.setup()
    try:
        r.quotes[CODE] = quote(CODE)
        cycle = r.cycle_id()
        # 1) 正确的历史 D 预算：没有 overlay。
        clean_d = r.budget(cycle_id=cycle, asof_day=DAY)
        # 2) 加入一条 D+1 才生效的 adaptive_allocation，再按 **as-of=D** 取预算。
        r.set_account_params(ACCOUNT, adaptive_allocation={
            "status": "active", "effective_date": DAY_NEXT.isoformat(), "weight_pct": 90.0,
        })
        leaked_d = r.budget(cycle_id=cycle, asof_day=DAY)
        # 非空门禁：这确实是一条"未来生效"的 overlay（D 不可见 / D+1 可见）。
        at_d = PT._runtime_parameter_active(
            DAY_NEXT.isoformat(), asof_day=DAY, status="active")
        at_next = PT._runtime_parameter_active(
            DAY_NEXT.isoformat(), asof_day=DAY_NEXT, status="active")
        assert at_d is False and at_next is True, (at_d, at_next)
        # 非空门禁：这条 overlay 若生效，确实会改变权重（不是空写入）。
        effective_weights = _strategy_weights(r)
        assert effective_weights[ACCOUNT] == 0.9, effective_weights
        # 缺陷成立：as-of=D 的预算被 D+1 才生效的 overlay 改写了。
        leaked = (
            clean_d.get("target_pct") != leaked_d.get("target_pct")
            or clean_d.get("absolute_cap_amount") != leaked_d.get("absolute_cap_amount")
        )
        return (not leaked), (
            f"overlay effective={DAY_NEXT.isoformat()} "
            f"(visible at D={at_d}, at D+1={at_next}) weight={effective_weights[ACCOUNT]} | "
            f"budget(asof=D, no overlay) target_pct={clean_d.get('target_pct')} "
            f"cap={clean_d.get('absolute_cap_amount')} | "
            f"budget(asof=D, overlay effective D+1) target_pct={leaked_d.get('target_pct')} "
            f"cap={leaked_d.get('absolute_cap_amount')} | future_overlay_leaked_into_D={leaked}"
        )
    finally:
        r.teardown()


def _strategy_weights(r):
    with PT._db() as conn:
        rows = PT._shared_account_rows(conn, r.cycle_id())
        profiles = {row["id"]: PT._risk_profile(row) for row in rows if row.get("id")}
        return PT._strategy_pool_weights(conn, rows, profiles)


# ─── R19-C2：future cluster evidence 改变 historical BUY sizing ───────────
def case_c2():
    r = Repro()
    r.setup()
    try:
        active = r.cycle_id()
        # A 持 {600001,600002,600003}，B 持 {600001,600002,600004}：
        # 持仓 jaccard=0.5、行业 jaccard=1.0、signal 交集为空。
        # D 口径下加权相似度 ≈ 0.29（成不了簇）；只把"机器今天"的 signal
        # 重合证据加进来后 ≈ 0.65（越过 0.6 阈值成簇）。
        for code, account_id in (("600001", ACCOUNT), ("600002", ACCOUNT),
                                 ("600003", ACCOUNT), ("600001", OTHER),
                                 ("600002", OTHER), ("600004", OTHER)):
            r.add_lot(account_id=account_id, code=code, cycle_id=active)
            r.quotes[code] = quote(code)
        r.quotes[CODE] = quote(CODE)
        r.sync()
        # 1) 正确的历史 D 预算（只有 D 可见的证据）。
        clean_d = r.budget(cycle_id=active, asof_day=DAY)
        with PT._db() as conn:
            clusters_clean, factors_clean = PT._strategy_cluster_factors(
                conn, DAY, account_ids=[ACCOUNT, OTHER], cycle_id=active)
        # 2) 只属于"机器今天"窗口的 signal 重合证据（D 看不到）。
        only_today = (dt.date.today() - dt.timedelta(days=5)).isoformat()
        for index in range(4):
            for account_id in (ACCOUNT, OTHER):
                r.add_signal(account_id=account_id, code=f"6010{index:02d}",
                             intended_date=only_today)
                r.quotes[f"6010{index:02d}"] = quote(f"6010{index:02d}")
        # 非空门禁：这批证据在**有界 as-of=D** 下不可见，在无界口径下可见。
        bounded_keys = _cluster_signal_codes(r, DAY)
        unbounded_keys = _cluster_signal_codes(r, None)
        assert unbounded_keys and not bounded_keys, (bounded_keys, unbounded_keys)
        # 非空门禁：加入这批证据后，无界口径**确实**把两策略并成了一簇。
        with PT._db() as conn:
            clusters_polluted, _f = PT._strategy_cluster_factors(
                conn, None, account_ids=[ACCOUNT, OTHER], cycle_id=active)
        assert len(clusters_polluted) == 1, clusters_polluted
        # 3) 仍按 as-of=D 取预算 —— 修复前它会随时间推移而改变。
        leaked_d = r.budget(cycle_id=active, asof_day=DAY)
        changed = (
            clean_d.get("target_pct") != leaked_d.get("target_pct")
            or clean_d.get("absolute_cap_amount") != leaked_d.get("absolute_cap_amount")
            or clean_d.get("allowance_amount") != leaked_d.get("allowance_amount")
        )
        return (not changed), (
            f"future signals intended={only_today} "
            f"(D-visible keys={sorted(bounded_keys)}, unbounded keys={sorted(unbounded_keys)}) | "
            f"D clusters={clusters_clean}/{factors_clean} "
            f"unbounded clusters={clusters_polluted} | "
            f"budget(asof=D) before target_pct={clean_d.get('target_pct')} "
            f"cap={clean_d.get('absolute_cap_amount')} "
            f"allowance={clean_d.get('allowance_amount')} | "
            f"budget(asof=D) after target_pct={leaked_d.get('target_pct')} "
            f"cap={leaked_d.get('absolute_cap_amount')} "
            f"allowance={leaked_d.get('allowance_amount')}"
        )
    finally:
        r.teardown()


def _cluster_signal_codes(r, asof_day):
    with PT._db() as conn:
        profiles = PT._strategy_cluster_profiles(
            conn, asof_day, [ACCOUNT, OTHER], cycle_id=r.cycle_id())
    return set(profiles[ACCOUNT]["signals"]) | set(profiles[OTHER]["signals"])


# ─── R19-C3：wrong-cycle reservation 被普通 BUY 消费 ─────────────────────
def case_c3():
    r = Repro()
    r.setup()
    try:
        cycle8 = r.cycle_id()
        cycle9 = r.new_cycle(f"r19-c3-{cycle8}")
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (cycle8,))
            conn.execute("UPDATE paper_accounts SET cycle_id=?, status='running'",
                         (cycle9,))
        assert r.cycle_id() == cycle9, "fixture 没把 active cycle 翻到新周期"
        r.quotes[CODE] = quote(CODE)
        # 下一条 order 会复用的 id：预置同 key 的 cycle8 预占。
        with PT._db() as conn:
            seq = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='paper_orders'").fetchone()
            next_order_id = int(seq["seq"] if seq is not None else 0) + 1
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_capital_reservations(cycle_id,order_key,account_id,code,"
                "side,amount,fees,status,created_at) VALUES(?,?,?,?,'buy',1000.0,1.0,"
                "'reserved',?)",
                (cycle8, str(next_order_id), ACCOUNT, CODE, f"{DAY.isoformat()} 09:00:00"))
        # 非空门禁：预占确实存在、status=reserved、周期与将建的订单周期不同。
        with PT._db() as conn:
            pre = dict(conn.execute(
                "SELECT * FROM paper_capital_reservations WHERE order_key=?",
                (str(next_order_id),)).fetchone())
        assert pre["status"] == "reserved", pre
        assert int(pre["cycle_id"]) == cycle8, pre
        signal_id = r.add_signal(intended_date=DAY.isoformat(),
                                 signal_date=DAY_PREV.isoformat())
        result, order = r.run_buy_order(signal_id=signal_id, code=CODE)
        with PT._db() as conn:
            row = dict(conn.execute(
                "SELECT * FROM paper_capital_reservations WHERE order_key=?",
                (str(next_order_id),)).fetchone())
            fills = conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
            lot_cycles = [int(item["cycle_id"]) for item in
                          conn.execute("SELECT cycle_id FROM paper_position_lots")]
        consumed = row["status"] == "consumed"
        order_cycle = int(order["cycle_id"]) if order and order["cycle_id"] is not None else None
        same_key = bool(order) and int(order["id"]) == next_order_id
        ok = (
            same_key and bool(result.get("filled")) and consumed
            and int(row["cycle_id"]) == cycle8 and order_cycle == cycle9
            and lot_cycles == [cycle9] and fills == 1
        )
        return (not ok), (
            f"prepared reservation order_key={next_order_id} cycle={cycle8} status=reserved | "
            f"order id={order and order['id']} (== prep key: {same_key}) "
            f"cycle={order_cycle} status={order and order['status']} | "
            f"reservation after: cycle={row['cycle_id']} status={row['status']} | "
            f"lot cycles={lot_cycles} fills={fills} | result={result}"
        )
    finally:
        r.teardown()


# ─── R19-C4：普通 BUY 绕过 execution_planner.commit_fill ─────────────────
def _buy_with_commit_patch(patch_commit):
    """在全新 fixture 上跑一次真实 BUY；返回 (sentinel_calls, result, counters)。"""
    r = Repro()
    r.setup()
    try:
        r.quotes[CODE] = quote(CODE)
        signal_id = r.add_signal(intended_date=DAY.isoformat(),
                                 signal_date=DAY_PREV.isoformat())
        import execution_planner as EP
        hits = []
        if patch_commit:
            def sentinel(*_args, **_kwargs):
                hits.append(1)
                raise RuntimeError("R19_SENTINEL")
            with mock.patch.object(EP, "commit_fill", side_effect=sentinel):
                result, _order = r.run_buy_order(signal_id=signal_id, code=CODE)
        else:
            result, _order = r.run_buy_order(signal_id=signal_id, code=CODE)
        with PT._db() as conn:
            fills = conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
            lots = conn.execute("SELECT COUNT(*) FROM paper_position_lots").fetchone()[0]
            cash = PT._shared_cash(conn)
            reservation = conn.execute(
                "SELECT status FROM paper_capital_reservations LIMIT 1").fetchone()
        return {
            "hits": len(hits), "result": result, "fills": fills, "lots": lots,
            "cash": cash,
            "reservation": reservation["status"] if reservation is not None else None,
        }
    finally:
        r.teardown()


def case_c4():
    baseline = _buy_with_commit_patch(patch_commit=False)
    with_sentinel = _buy_with_commit_patch(patch_commit=True)
    # 非空门禁：同一 fixture 在无 sentinel 时必须真的能成交（否则"没成交"无意义）。
    reachable = bool(baseline["result"].get("filled"))
    called = with_sentinel["hits"] > 0
    # 缺陷成立：EP.commit_fill 从未被调用，且 sentinel 无法阻止成交落库。
    bypassed = reachable and not called and bool(with_sentinel["result"].get("filled"))
    return (not bypassed), (
        f"baseline(no sentinel): filled={baseline['result'].get('filled')} "
        f"fills={baseline['fills']} lots={baseline['lots']} | "
        f"with sentinel: EP.commit_fill calls={with_sentinel['hits']} "
        f"filled={with_sentinel['result'].get('filled')} fills={with_sentinel['fills']} "
        f"lots={with_sentinel['lots']} reservation={with_sentinel['reservation']} | "
        f"reachable={reachable} ep_called={called}"
    )


def main() -> int:
    print(f"repo root: {ROOT}")
    print(f"HEAD      : {os.popen('git rev-parse --short=12 HEAD').read().strip()}")
    print()
    cases = [
        ("R19-C1 future adaptive allocation changes historical budget", case_c1),
        ("R19-C2 future cluster evidence changes historical BUY sizing", case_c2),
        ("R19-C3 wrong-cycle reservation consumed by normal strategy buy", case_c3),
        ("R19-C4 normal strategy buy bypasses execution_planner.commit_fill", case_c4),
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
