# -*- coding: utf-8 -*-
"""R17 before-fix 差分复现（在**未修改**的 R17 base SHA 上运行）。

覆盖规格 §12-§18：

    R17-C1  后来一条无关 signal 顶掉当前 episode 的 model score
    R17-C2  历史 as-of 能看到未来 signal（future leakage）
    R17-C3  错误 provenance 足以翻转真实 concentration action
    R17-C4  missing opened_order_id 时"能搜到就用"最新 signal

正向 lifecycle 契约（base 上不要求 fail，修复后必须成立）：

    R17-C5  add-on 不得改变 episode origin
    R17-C6  full exit + re-entry 必须换 provenance

用法（仓库根目录）::

    python work/r17_before_fix_repro.py
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
CODE = "600000"
DAY = dt.date(2026, 9, 10)
DAY_NEXT = dt.date(2026, 9, 11)


class Repro:
    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "r17_repro.sqlite3")
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
            mock.patch.object(PT, "_completed_kline", return_value=None),
        ]
        for p in self._patches:
            p.start()
        PT.init_db()
        self.quotes = {}
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='running' WHERE id=1")

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

    # ── 夹具：真实 signal → verified BUY order → lot → risk_state ───────────
    def add_signal(self, *, score, signal_date, account_id=ACCOUNT, code=CODE,
                   t_score=None, rank_score=None):
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, account_id)
            cur = conn.execute(
                "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
                "close_price,rank_score,t_tier,t_score,payload,status,created_at,"
                "strategy_id,strategy_version,strategy_checksum) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)",
                (account_id, signal_date, signal_date, code, f"测试股_{code}", 10.0,
                 rank_score, "A", t_score,
                 PT._json({"decision": {"entry_model": {"score": score}}}),
                 f"{signal_date} 15:00:00", *stamp),
            )
            return int(cur.lastrowid)

    def add_buy_order(self, *, signal_id, qty=100, price=10.0, cycle_id=None,
                      account_id=ACCOUNT, code=CODE, side="buy", status="filled",
                      verified=True, order_id=None):
        cycle_id = self.cycle_id() if cycle_id is None else cycle_id
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, account_id)
            cur = conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
                "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
                "signal_id,execution_verified,execution_status) "
                "VALUES(?,?,?,?,?,?,?,?,5.0,?,'seed_buy','{}',?,?,'market','seed',?,?,?,?,?,?,?)",
                (account_id, side, code, f"测试股_{code}", qty, price, price, qty * price,
                 status, f"2026-09-09 09:30:00", f"2026-09-09 09:30:00", *stamp,
                 cycle_id, signal_id, 1 if verified else 0,
                 "verified" if verified else "unknown"),
            )
            return int(cur.lastrowid)

    def add_lot(self, *, order_id, qty=100, cost=10.0, cycle_id=None,
                account_id=ACCOUNT, code=CODE):
        cycle_id = self.cycle_id() if cycle_id is None else cycle_id
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
                "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
                "is_t_base,source_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,'stock_t1',1,1,?)",
                (cycle_id, account_id, code, f"测试股_{code}", "Tech", qty, qty, cost,
                 "2026-09-08 10:00:00", "2026-09-09", order_id),
            )

    def add_risk_state(self, *, opened_order_id, cycle_id=None, account_id=ACCOUNT,
                       code=CODE, peak_price=10.0, take_stage=0):
        cycle_id = self.cycle_id() if cycle_id is None else cycle_id
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_position_risk_state(cycle_id,account_id,code,peak_price,"
                "take_stage,opened_order_id,initialized_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (cycle_id, account_id, code, peak_price, take_stage, opened_order_id,
                 "2026-09-08 10:00:00", "2026-09-08 10:00:00"),
            )

    # ── 驱动真实生产函数 ────────────────────────────────────────────────────
    def position(self):
        with PT._db() as conn:
            rows = PT._position_rows(conn, asof_day=DAY)
        return [p for p in rows if p["code"] == CODE][0]

    def review(self, *, asof_day=DAY):
        """驱动真实 _position_quality_score。"""
        position = self.position()
        quote = {"code": CODE, "price": 10.0, "pct": 0.0, "high": 10.0, "low": 10.0}
        kwargs = {}
        try:
            import inspect
            if "cycle_id" in inspect.signature(PT._position_quality_score).parameters:
                kwargs["cycle_id"] = self.cycle_id()
        except (TypeError, ValueError):
            pass
        with PT._db() as conn:
            return PT._position_quality_score(conn, position, quote, asof_day, **kwargs)

    def action(self, review, *, available_qty=100, sells_used=0, fresh=True):
        position = dict(self.position())
        position["available_qty"] = available_qty
        quote_status = {"fresh": fresh, "reason": "fixture"}
        import inspect
        params = inspect.signature(PT._concentration_action).parameters if hasattr(
            PT, "_concentration_action") else None
        if hasattr(PT, "_concentration_action"):
            return PT._concentration_action(review, position, quote_status, sells_used)
        import paper_position_review as PReview
        return PReview.decide_action(review, position, quote_status, sells_used,
                                     policy=PT.REVIEW_POLICY)

    # ── 通用夹具：episode A（高分） + 可选后来 signal B ─────────────────────
    def build_episode(self, *, episode_score=90.0, episode_date="2026-09-10"):
        signal_a = self.add_signal(score=episode_score, signal_date=episode_date,
                                   t_score=episode_score, rank_score=episode_score)
        order_a = self.add_buy_order(signal_id=signal_a)
        self.add_lot(order_id=order_a)
        self.add_risk_state(opened_order_id=order_a)
        return signal_a, order_a


# ─── R17-C1：后来一条无关 signal 顶掉当前 episode ──────────────────────────
def case_c1():
    r = Repro()
    r.setup()
    try:
        signal_a, order_a = r.build_episode(episode_score=90.0)
        # 后来一条同账户同代码的 signal（次日），不创建任何订单、不属于当前 episode
        r.add_signal(score=10.0, signal_date="2026-09-11", t_score=10.0, rank_score=10.0)
        review = r.review(asof_day=DAY_NEXT)  # asof 也在次日 ⇒ 不是 future leakage
        ok = float(review["model_score"]) == 90.0
        return ok, (f"model_score={review['model_score']} "
                    f"(episode signal A={signal_a} order={order_a}); "
                    f"source={review.get('model_score_source')}")
    finally:
        r.teardown()


# ─── R17-C2：未来 signal 泄漏进历史 as-of ──────────────────────────────────
def case_c2():
    r = Repro()
    r.setup()
    try:
        r.build_episode(episode_score=90.0, episode_date="2026-09-10")
        r.add_signal(score=10.0, signal_date="2026-09-11", t_score=10.0, rank_score=10.0)
        review = r.review(asof_day=DAY)  # asof = 2026-09-10，B 在 09-11
        ok = float(review["model_score"]) == 90.0
        return ok, (f"asof=2026-09-10 model_score={review['model_score']} "
                    f"(future signal B dated 2026-09-11 must not leak)")
    finally:
        r.teardown()


# ─── R17-C3：错误 provenance 足以翻转真实 action ──────────────────────────
def case_c3():
    r = Repro()
    r.setup()
    try:
        r.build_episode(episode_score=100.0)
        review_good = r.review(asof_day=DAY_NEXT)
        action_good, _ = r.action(review_good)
        r.add_signal(score=0.0, signal_date="2026-09-11", t_score=0.0, rank_score=0.0)
        review_bad = r.review(asof_day=DAY_NEXT)
        action_bad, reason_bad = r.action(review_bad)
        ok = action_bad == action_good
        return ok, (f"score {review_good['model_score']}->{review_bad['model_score']}; "
                    f"action {action_good}->{action_bad} ({reason_bad[:40]})")
    finally:
        r.teardown()


# ─── R17-C4：missing provenance 不得猜 latest signal ──────────────────────
def case_c4():
    r = Repro()
    r.setup()
    try:
        # 有 lot，但 risk_state 缺失（无法证明 episode origin）
        order_a = r.add_buy_order(signal_id=r.add_signal(score=0.0, signal_date="2026-09-10",
                                                         t_score=0.0, rank_score=0.0))
        r.add_lot(order_id=order_a)
        review = r.review()
        ok = float(review["model_score"]) == 50.0
        return ok, (f"model_score={review['model_score']} "
                    f"source={review.get('model_score_source')} "
                    "(no risk_state → provenance unknown → must be 50.0)")
    finally:
        r.teardown()


# ─── R17-C5：add-on 不得改变 episode origin ───────────────────────────────
def case_c5():
    r = Repro()
    r.setup()
    try:
        signal_a, order_a = r.build_episode(episode_score=90.0)
        signal_b = r.add_signal(score=10.0, signal_date="2026-09-11",
                                t_score=10.0, rank_score=10.0)
        order_b = r.add_buy_order(signal_id=signal_b, qty=100)
        r.add_lot(order_id=order_b, qty=100)
        # 加仓：risk_state.opened_order_id 必须仍是 order A
        with PT._db() as conn:
            state = conn.execute(
                "SELECT opened_order_id FROM paper_position_risk_state WHERE account_id=? AND code=?",
                (ACCOUNT, CODE)).fetchone()
        review = r.review()
        ok = (int(state["opened_order_id"]) == order_a
              and float(review["model_score"]) == 90.0)
        return ok, (f"opened_order_id={state['opened_order_id']} (A={order_a}, B={order_b}); "
                    f"model_score={review['model_score']}")
    finally:
        r.teardown()


# ─── R17-C6：full exit + re-entry 必须换 provenance ───────────────────────
def case_c6():
    r = Repro()
    r.setup()
    try:
        signal_a, order_a = r.build_episode(episode_score=90.0)
        # full exit：risk state 删除（模拟 finalize_sell）
        with PT._db(immediate=True) as conn:
            conn.execute("DELETE FROM paper_position_risk_state WHERE account_id=? AND code=?",
                         (ACCOUNT, CODE))
            conn.execute("UPDATE paper_position_lots SET remaining_qty=0 WHERE account_id=? AND code=?",
                         (ACCOUNT, CODE))
        # 同周期重入：新 signal C → 新 order C
        signal_c = r.add_signal(score=20.0, signal_date="2026-09-11",
                                t_score=20.0, rank_score=20.0)
        order_c = r.add_buy_order(signal_id=signal_c)
        r.add_lot(order_id=order_c)
        r.add_risk_state(opened_order_id=order_c)
        with PT._db() as conn:
            state = conn.execute(
                "SELECT opened_order_id FROM paper_position_risk_state WHERE account_id=? AND code=?",
                (ACCOUNT, CODE)).fetchone()
        review = r.review(asof_day=DAY_NEXT)
        ok = (int(state["opened_order_id"]) == order_c
              and float(review["model_score"]) == 20.0)
        return ok, (f"opened_order_id={state['opened_order_id']} "
                    f"(old A={order_a}, new C={order_c}); model_score={review['model_score']}")
    finally:
        r.teardown()


def main() -> int:
    print(f"repo root: {ROOT}")
    print(f"HEAD      : {os.popen('git rev-parse --short=12 HEAD').read().strip()}")
    print()
    cases = [
        ("R17-C1 unrelated later signal hijacks current episode", case_c1),
        ("R17-C2 future signal leaks into historical asof", case_c2),
        ("R17-C3 wrong provenance changes automatic action", case_c3),
        ("R17-C4 missing episode provenance guesses latest signal", case_c4),
        ("R17-C5 add-on does not change episode origin", case_c5),
        ("R17-C6 full exit + re-entry uses new provenance", case_c6),
    ]
    results = []
    for name, fn in cases:
        ok, detail = fn()
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
