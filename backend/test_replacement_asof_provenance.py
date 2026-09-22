# -*- coding: utf-8 -*-
"""R18：replacement / slot-upgrade 的 as-of 与 cycle 归属契约 + 生产回归。

evidence 层契约（``paper_replacement_evidence``）::

    RP2-01  same-day candidate 被选中
    RP2-02  tomorrow candidate 被排除（intended_date 必须等于 asof）
    RP2-03  合法 overnight plan（signal_date=D-1, intended_date=D）仍可用
    RP2-04  future signal_date（D+1）即使 intended_date=D 也被排除
    RP2-05  非 asof 的过去 intended_date 同样被排除
    RP2-06  status 过滤生效
    RP2-07  multi-account 隔离
    RP2-08  latest_position_review 受 review_date <= asof 约束
    RP2-09  latest_position_review 受显式 cycle_id 约束
    RP2-10  review 不存在返回 None（不 fallback 别的周期）
    RP2-11  static：candidate SQL 无 next-day range / 无归档表

生产回归（直接驱动 ``PT`` 真实函数）::

    RPL-P1  tomorrow-only 强候选不能让今天持仓被卖
    RPL-P2  same-day 强候选仍能触发换仓（rotation 未被"修死"）
    RPL-P3  overnight 候选仍可影响今天的 replacement 决策
    RPL-P4  future review 不能造出 slot_upgrade_ready
    RPL-P5  explicit cycle borrow 只写该周期的席位版本
    RPL-P6  explicit cycle rollback 只还原该周期
"""
from __future__ import annotations

import ast
import datetime as dt
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import paper_replacement_evidence as PREPL  # noqa: E402
import paper_replacement_decision as PRep  # noqa: E402
import paper_trading as PT  # noqa: E402
import universe as U  # noqa: E402

ACCOUNT = "tq_breakout"
OTHER = "sector_rotation"
CODE = "600001"
DAY = dt.date(2026, 9, 10)
DAY_NEXT = dt.date(2026, 9, 11)
DAY_PREV = dt.date(2026, 9, 9)
STATUSES = ("pending", "deferred_capacity")
#: 真实 ``_buy_order`` 需要 ``light``（市场灯）与 ``overseas``（跳空/竞价闸门）。
MARKET = {
    "light": "green",
    "overseas": {"light": "green", "advice": "fixture"},
    "breadth": 0.5,
    "sentiment": "neutral",
}


class _EvidenceCase(unittest.TestCase):
    """只含 candidate / review 读取所需表的极简 SQLite。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE paper_signals(
                id INTEGER PRIMARY KEY, account_id TEXT, signal_date TEXT,
                intended_date TEXT, code TEXT, name TEXT, rank_score REAL,
                t_score REAL, payload TEXT, status TEXT, created_at TEXT);
            CREATE TABLE paper_position_reviews(
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER,
                account_id TEXT, code TEXT, review_date TEXT, score REAL,
                action TEXT);
            """
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def add_signal(self, signal_id, *, intended_date, signal_date=None, code=CODE,
                   account_id=ACCOUNT, t_score=60.0, rank_score=60.0,
                   entry_score=60.0, status="pending"):
        self.conn.execute(
            "INSERT INTO paper_signals(id,account_id,signal_date,intended_date,code,name,"
            "rank_score,t_score,payload,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (signal_id, account_id, signal_date or intended_date, intended_date, code,
             f"测试股_{code}", rank_score, t_score,
             '{"decision":{"entry_model":{"score":%s}}}' % entry_score, status,
             f"{signal_date or intended_date} 15:00:00"),
        )
        self.conn.commit()

    def add_review(self, *, cycle_id, review_date, score, action="hold", code=CODE,
                   account_id=ACCOUNT):
        self.conn.execute(
            "INSERT INTO paper_position_reviews(cycle_id,account_id,code,review_date,score,"
            "action) VALUES(?,?,?,?,?,?)",
            (cycle_id, account_id, code, review_date, score, action),
        )
        self.conn.commit()

    def load(self, *, asof_day=None, account_id=ACCOUNT, statuses=STATUSES):
        asof_day = DAY.isoformat() if asof_day is None else asof_day
        return PREPL.load_replacement_candidates(
            self.conn, account_id=account_id, asof_day=asof_day, statuses=statuses)


class CandidateEvidenceTests(_EvidenceCase):
    """RP2-01 … RP2-07 —— 候选读取的 as-of / status / account 边界。"""

    def test_rp2_01_same_day_candidate_is_loaded(self):
        self.add_signal(1, intended_date=DAY.isoformat())
        rows = self.load()
        self.assertEqual([row["id"] for row in rows], [1])

    def test_rp2_02_tomorrow_candidate_is_excluded(self):
        self.add_signal(1, intended_date=DAY_NEXT.isoformat())
        self.assertEqual(self.load(), [], "明天的候选出现在今天的 replacement 集合里")

    def test_rp2_03_overnight_plan_is_allowed(self):
        """signal_date=D-1 + intended_date=D 是合法隔夜计划，必须保留。"""
        self.add_signal(1, signal_date=DAY_PREV.isoformat(),
                        intended_date=DAY.isoformat())
        rows = self.load()
        self.assertEqual([row["id"] for row in rows], [1],
                         "合法隔夜计划被误杀")

    def test_rp2_04_future_signal_date_is_excluded(self):
        """intended_date=D 但 signal_date=D+1 ⇒ 证据来自未来 ⇒ fail closed。"""
        self.add_signal(1, signal_date=DAY_NEXT.isoformat(),
                        intended_date=DAY.isoformat())
        self.assertEqual(self.load(), [], "signal_date 晚于 asof 仍被采用")

    def test_rp2_05_past_intended_date_is_excluded(self):
        self.add_signal(1, intended_date=DAY_PREV.isoformat(),
                        signal_date=DAY_PREV.isoformat())
        self.assertEqual(self.load(), [], "非 asof 的旧 intended_date 仍被采用")

    def test_rp2_06_status_filter_is_applied(self):
        self.add_signal(1, intended_date=DAY.isoformat(), status="expired")
        self.add_signal(2, intended_date=DAY.isoformat(), status="filled")
        self.assertEqual(self.load(), [])

    def test_rp2_07_account_isolation(self):
        self.add_signal(1, intended_date=DAY.isoformat(), account_id=OTHER)
        self.assertEqual(self.load(), [])
        rows = self.load(account_id=OTHER)
        self.assertEqual([row["id"] for row in rows], [1])

    def test_rp2_11_candidate_sql_has_no_next_day_range_or_archive(self):
        """静态：candidate SQL 不得出现 next-day range，也不得读归档表。"""
        with open(os.path.join(BACKEND_DIR, "paper_replacement_evidence.py"),
                  encoding="utf-8") as fh:
            raw = fh.read()
        tree = ast.parse(raw)
        first = tree.body[0]
        body = "\n".join(raw.splitlines()[first.end_lineno:]) if isinstance(
            first, ast.Expr) else raw
        self.assertNotIn("intended_date>=", body)
        self.assertNotIn("intended_date<=", body)
        self.assertNotIn("_next_weekday", body)
        self.assertNotIn("paper_signals_archive", body)
        self.assertIn("intended_date=?", body)
        self.assertIn("signal_date<=?", body)


class ReviewEvidenceTests(_EvidenceCase):
    """RP2-08 … RP2-10 —— 历史 review 的 cycle + as-of 双重上界。"""

    def latest(self, *, cycle_id=8, asof_day=None):
        asof_day = DAY.isoformat() if asof_day is None else asof_day
        return PREPL.latest_position_review(
            self.conn, cycle_id=cycle_id, account_id=ACCOUNT, code=CODE,
            asof_day=asof_day)

    def test_rp2_08_review_date_upper_bound(self):
        self.add_review(cycle_id=8, review_date=DAY.isoformat(), score=70.0)
        self.add_review(cycle_id=8, review_date=DAY_NEXT.isoformat(), score=20.0)
        row = self.latest()
        self.assertIsNotNone(row)
        self.assertEqual(row["score"], 70.0, "asof 之前的最新 review 未被选中")
        self.assertEqual(row["review_date"], DAY.isoformat())

    def test_rp2_09_explicit_cycle_bound(self):
        self.add_review(cycle_id=8, review_date=DAY.isoformat(), score=70.0)
        self.add_review(cycle_id=9, review_date=DAY.isoformat(), score=20.0)
        self.assertEqual(self.latest(cycle_id=8)["score"], 70.0)
        self.assertEqual(self.latest(cycle_id=9)["score"], 20.0)

    def test_rp2_10_missing_review_returns_none(self):
        self.assertEqual(self.latest(), None)
        self.add_review(cycle_id=8, review_date=DAY_NEXT.isoformat(), score=20.0)
        self.assertIsNone(self.latest(), "未来 review 被当成可用的历史 review")

    def test_rp2_10b_invalid_inputs_fail_closed(self):
        self.add_review(cycle_id=8, review_date=DAY.isoformat(), score=70.0)
        self.assertIsNone(self.latest(cycle_id=None))
        # 空的 asof_day 不可比较 ⇒ 不能给出任何 review（"不知道"不是"是"）。
        self.assertIsNone(self.latest(asof_day=""))
        self.assertIsNone(PREPL.latest_position_review(
            self.conn, cycle_id=8, account_id=ACCOUNT, code=CODE, asof_day=None))


class ProductionCandidateCase(unittest.TestCase):
    """真实生产 schema 的临时账本。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "paper_r18.sqlite3")
        self.quotes = {}
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
            mock.patch.dict(os.environ, {"PAPER_ENTRY_FREEZE": "0"}),
        ]
        for p in self._patches:
            p.start()
        PT._ENTRY_FREEZE_CACHE.update({"at": 0.0, "status": None})
        PT.init_db()
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='running' WHERE id=1")
            conn.execute("UPDATE paper_accounts SET status='running'")

    def tearDown(self):
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

    def account_cycle_id(self, account_id=ACCOUNT):
        """账户当前所属周期 —— signal 的 cycle 归属就是这个 write-time fact。

        不用 ``self.cycle_id()``（= active cycle）：两者在「在途借位时 active 已
        翻到下个周期、账户仍留在原周期」的 fixture 里**故意不同**，而生产
        ``generate_signals`` 盖的是账户自己的周期（DB guard 也强制这一点）。
        """
        with PT._db() as conn:
            return int(conn.execute(
                "SELECT cycle_id FROM paper_accounts WHERE id=?", (account_id,)
            ).fetchone()[0])

    def add_signal(self, *, code, intended_date, signal_date=None, entry_score=90.0,
                   t_score=90.0, rank_score=90.0, status="pending", account_id=ACCOUNT):
        signal_date = signal_date or intended_date
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, account_id)
            cur = conn.execute(
                "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
                "close_price,rank_score,t_tier,t_score,payload,status,created_at,"
                "strategy_id,strategy_version,strategy_checksum,cycle_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, signal_date, intended_date, code, f"测试股_{code}", 10.0,
                 rank_score, "A", t_score,
                 PT._json({"decision": {"entry_model": {"score": entry_score}}}),
                 status, f"{signal_date} 15:00:00", *stamp,
                 self.account_cycle_id(account_id)),
            )
            return int(cur.lastrowid)

    def add_lot(self, *, code=CODE, qty=100, cost=10.0, account_id=ACCOUNT, cycle_id=None):
        cycle_id = self.cycle_id() if cycle_id is None else cycle_id
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, account_id)
            order = conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
                "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
                "execution_verified,execution_status) VALUES(?,'buy',?,?,?,?,?,?,5.0,'filled',"
                "'seed_buy','{}',?,?,'market','seed',?,?,?,?,1,'verified')",
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

    def add_review(self, *, code=CODE, review_date, score, action="hold",
                   account_id=ACCOUNT, cycle_id=None):
        cycle_id = self.cycle_id() if cycle_id is None else cycle_id
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_position_reviews(cycle_id,account_id,code,review_date,"
                "score,grade,action,market_value,position_pct,reasons,detail,created_at) "
                "VALUES(?,?,?,?,?,'观察',?,1000.0,1.0,'fixture','{}',?)",
                (cycle_id, account_id, code, review_date, score, action,
                 f"{review_date} 14:50:00"),
            )

    def set_quote(self, code, price=10.0):
        self.quotes[code] = {
            "code": code, "name": f"测试股_{code}", "price": price,
            "high": price * 1.01, "low": price * 0.99, "pct": 0.0,
            "amount": 100000.0, "volume": 10000.0, "turnover": 1.0,
            "quote_source": "live", "quote_at": f"{DAY.isoformat()} 14:50:00",
            "quote_validation": "cross_source_checked",
        }

    def position(self, code=CODE):
        with PT._db() as conn:
            rows = PT._position_rows(conn, asof_day=DAY)
        return dict([p for p in rows if p["code"] == code and p["account_id"] == ACCOUNT][0])

    def review_for(self, *, code=CODE, replacement, asof_day=DAY):
        position = self.position(code)
        quote = self.quotes.get(code) or {}
        with PT._db() as conn:
            return PT._position_quality_score(
                conn, position, quote, asof_day, cycle_id=self.cycle_id(),
                replacement=replacement, nav=100000.0, market={},
            )

    def decide(self, review, code=CODE):
        position = dict(self.position(code), available_qty=100)
        return PT.PReview.decide_action(
            review, position, {"fresh": True, "reason": "fixture"}, 0,
            policy=PT.REVIEW_POLICY)


class ProductionAsOfRegression(ProductionCandidateCase):
    """RPL-P1 … RPL-P4 —— 直接驱动生产函数。"""

    def test_rpl_p1_tomorrow_candidate_cannot_sell_today_holding(self):
        self.add_lot()
        self.add_review(review_date=DAY.isoformat(), score=40.0)
        self.set_quote(CODE)
        # 只有明天的强候选
        fut = self.add_signal(code="600002", intended_date=DAY_NEXT.isoformat(),
                              entry_score=100.0, t_score=100.0, rank_score=100.0)
        self.set_quote("600002")
        with PT._db() as conn:
            best = PT._best_replacement_candidate(conn, ACCOUNT, DAY, {CODE})
        self.assertIsNone(best, f"明天的候选 {fut} 被当成今天可用的替补")
        review = self.review_for(replacement=best)
        action, reason = self.decide(review)
        self.assertNotEqual(action, "consolidation_exit",
                            f"明天候选让今天持仓被卖：{reason}")

    def test_rpl_p2_same_day_candidate_still_rotates(self):
        """不能修成"所有 replacement 永远 None"：当天强候选必须照常换仓。"""
        self.add_lot()
        self.add_review(review_date=DAY.isoformat(), score=30.0)
        self.set_quote(CODE)
        same = self.add_signal(code="600003", intended_date=DAY.isoformat(),
                               entry_score=100.0, t_score=100.0, rank_score=100.0)
        self.set_quote("600003")
        with PT._db() as conn:
            best = PT._best_replacement_candidate(conn, ACCOUNT, DAY, {CODE})
        self.assertIsNotNone(best)
        self.assertEqual(best["signal_id"], same)
        review = self.review_for(replacement=best)
        action, reason = self.decide(review)
        self.assertEqual(action, "consolidation_exit", f"当天强候选不再换仓：{reason}")

    def test_rpl_p3_overnight_candidate_still_usable(self):
        """D-1 生成、intended D 的合法隔夜计划仍必须能影响今天的 replacement。"""
        self.add_lot()
        self.set_quote(CODE)
        night = self.add_signal(code="600004", signal_date=DAY_PREV.isoformat(),
                                intended_date=DAY.isoformat(), entry_score=100.0,
                                t_score=100.0, rank_score=100.0)
        self.set_quote("600004")
        with PT._db() as conn:
            best = PT._best_replacement_candidate(conn, ACCOUNT, DAY, {CODE})
        self.assertIsNotNone(best, "合法隔夜计划被误杀")
        self.assertEqual(best["signal_id"], night)

    def test_rpl_p4_future_review_cannot_create_slot_upgrade_ready(self):
        account_id = ACCOUNT
        code = CODE
        self.add_lot(code=code)
        self.add_review(code=code, review_date=DAY.isoformat(), score=70.0)
        self.add_review(code=code, review_date=DAY_NEXT.isoformat(), score=10.0)
        cand = self.add_signal(code="600005", intended_date=DAY.isoformat(),
                               entry_score=60.0, t_score=60.0, rank_score=60.0)
        with PT._db() as conn:
            signal = dict(conn.execute("SELECT * FROM paper_signals WHERE id=?",
                                       (cand,)).fetchone())
            positions = [dict(row) for row in PT._position_rows(conn, asof_day=DAY)
                         if row["account_id"] == account_id]
            ctx = PT._slot_upgrade_context(
                conn, account_id, signal, positions, DAY, cycle_id=self.cycle_id())
        weakest = ctx.get("weakest")
        self.assertIsNotNone(weakest)
        self.assertEqual(float(weakest["score"]), 70.0,
                         "asof=D 的最弱分来自 D+1 的 review（future leakage）")
        self.assertEqual(ctx.get("state"), "edge_insufficient",
                         "未来 review 造出了 slot upgrade 状态")
        self.assertFalse(ctx.get("eligible"))

    def test_rpl_p7_position_quality_uses_same_day_replacement_only(self):
        """端到端：review 的 replacement_score 必须来自当天候选。"""
        self.add_lot()
        self.set_quote(CODE)
        self.add_signal(code="600006", intended_date=DAY_NEXT.isoformat(),
                        entry_score=100.0, t_score=100.0, rank_score=100.0)
        with PT._db() as conn:
            best = PT._best_replacement_candidate(conn, ACCOUNT, DAY, {CODE})
        review = self.review_for(replacement=best)
        self.assertIsNone(review.get("replacement_score"))


class ProductionSlotLifecycleProvenance(ProductionCandidateCase):
    """RPL-P5 / RPL-P6 —— 借位与回滚都固定显式周期。

    这两个 case 钉的是**越界写**：请求的是 cycle 8 的在途借位，而 active cycle
    已经翻到 9。修复前 helper 自己 ``_active_cycle()``，于是会去读写 **cycle 9**
    的席位版本行 —— 一次 cycle 8 的下单把席位记到了 cycle 9 头上。
    """

    def _seed_cycle(self, key):
        with PT._db(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,started_at,"
                "created_at,updated_at) VALUES(?,'running',1000000.0,'balanced',?,?,?)",
                (key, f"{DAY_NEXT.isoformat()} 00:00:00",
                 f"{DAY_NEXT.isoformat()} 00:00:00", f"{DAY_NEXT.isoformat()} 00:00:00"))
            return int(cur.lastrowid)

    def _seed_allocation_version(self, cycle_id, *, limits, events=None):
        """插一条该周期的席位版本行，返回 version_id。"""
        with PT._db(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO paper_position_limit_versions(cycle_id,allocation_key,pool_limit,"
                "limits,weights,inputs,source,effective_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (cycle_id, f"r18-fixture-{cycle_id}", 15, PT._json(limits),
                 PT._json({"tq_breakout": 0.2, "sector_rotation": 0.8}),
                 PT._json({"slot_borrow_events": list(events or [])}),
                 "versioned_runtime_active_risk_budget",
                 "2026-09-10 09:30:00", "2026-09-10 09:30:00"),
            )
            return int(cur.lastrowid)

    def _version_row(self, version_id):
        with PT._db() as conn:
            row = conn.execute(
                "SELECT cycle_id,limits,inputs FROM paper_position_limit_versions WHERE id=?",
                (version_id,)).fetchone()
        return dict(row) if row is not None else None

    def _requested_cycle_borrow_fixture(self):
        """建 cycle8（请求周期）+ cycle9（active），两者的席位版本行都在。"""
        cycle8 = self.cycle_id()
        v8 = self._seed_allocation_version(
            cycle8, limits={"tq_breakout": 2, "sector_rotation": 6})
        cycle9 = self._seed_cycle(f"r18-slot-race-{cycle8}")
        v9 = self._seed_allocation_version(
            cycle9, limits={"tq_breakout": 2, "sector_rotation": 6})
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (cycle8,))
        self.assertEqual(self.cycle_id(), cycle9, "fixture 没把 active cycle 翻到 9")
        return cycle8, v8, cycle9, v9

    def test_rpl_p5_borrow_never_touches_another_cycles_allocation(self):
        cycle8, v8, cycle9, v9 = self._requested_cycle_borrow_fixture()
        before8 = self._version_row(v8)
        before9 = self._version_row(v9)
        budget = {
            "pool_limit": 15, "limits": {"tq_breakout": 2, "sector_rotation": 6},
            # budget 来自 active cycle（9）—— 正是"在途下单遇到周期翻转"的形状。
            "allocation_version": f"slots-v{v9}",
        }
        upgrade = {
            "borrow_ready": True, "borrow_candidate_score": 80.0,
            "donors": [{"account_id": "shared_pool", "limit": 15, "count": 3,
                        "remaining_after": 3, "unused_pool_slots": 12}],
        }
        with mock.patch.object(PT, "_dynamic_position_limits",
                                   lambda conn, *, cycle_id=None, asof_day=None: dict(budget)):
            with PT._db(immediate=True) as conn:
                result = PT._apply_slot_borrow(
                    conn, ACCOUNT, upgrade, DAY, cycle_id=cycle8)
        self.assertEqual(self._version_row(v9), before9,
                         "cycle 8 的在途借位写进了 active cycle 9 的席位版本")
        self.assertEqual(self._version_row(v8), before8,
                         "借位越界改写了别的周期的版本行")
        self.assertFalse(result.get("allowed"),
                         "显式周期不匹配时借位必须 fail closed")

    def test_rpl_p6_rollback_never_touches_another_cycles_allocation(self):
        cycle8, v8, cycle9, v9 = self._requested_cycle_borrow_fixture()
        # cycle9 的版本行已经处在"借位后"状态；一次 cycle8 的回滚绝不能回退它。
        with PT._db(immediate=True) as conn:
            conn.execute(
                "UPDATE paper_position_limit_versions SET limits=? WHERE id=?",
                (PT._json({"tq_breakout": 3, "sector_rotation": 6}), v9))
        before9 = self._version_row(v9)
        before8 = self._version_row(v8)
        borrow = {
            "allowed": True, "allocation_version": f"slots-v{v9}",
            "limits_before": {"tq_breakout": 2, "sector_rotation": 6},
            "limits_after": {"tq_breakout": 3, "sector_rotation": 6},
        }
        with PT._db(immediate=True) as conn:
            result = PT._rollback_slot_borrow(conn, borrow, cycle_id=cycle8)
        self.assertEqual(self._version_row(v9), before9,
                         "cycle 8 的回滚撤销了 cycle 9 的借位")
        self.assertEqual(self._version_row(v8), before8,
                         "回滚越界改写了别的周期的版本行")
        self.assertFalse(result.get("allowed"),
                         "显式周期不匹配时回滚必须 fail closed")
        self.assertNotIn("rolled_back", result)

    def test_rpl_p5c_donor_count_is_read_from_the_explicit_cycle(self):
        """donor 的持仓数必须来自**显式周期**，不能来自 active cycle。

        这里让 cycle 9（active）持有足量 donor 仓位、cycle 8 几乎为空：
        显式 cycle 8 的借位应当被允许；若 helper 去读 active cycle 9，donor 就
        会被误判为"已达最小保留席位"而拒绝。
        """
        donor = OTHER
        cycle8 = self.cycle_id()
        v8 = self._seed_allocation_version(cycle8, limits={"tq_breakout": 2, donor: 6})
        cycle9 = self._seed_cycle(f"r18-p5c-{cycle8}")
        self._seed_allocation_version(cycle9, limits={"tq_breakout": 2, donor: 6})
        # cycle 9（active）里塞满 donor 的持仓
        for index in range(6):
            self.add_lot(code=f"6010{index:02d}", account_id=donor, cycle_id=cycle9)
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (cycle8,))
        self.assertEqual(self.cycle_id(), cycle9)
        self.assertEqual(len(self._cycle_positions(cycle8, donor)), 0)
        self.assertEqual(len(self._cycle_positions(cycle9, donor)), 6)

        budget = {"pool_limit": 15, "limits": {"tq_breakout": 2, donor: 6},
                  "allocation_version": f"slots-v{v8}"}
        upgrade = {
            "borrow_ready": True, "borrow_candidate_score": 80.0,
            "donors": [{"account_id": donor, "limit": 6, "count": 0,
                        "remaining_after": 5}],
        }
        with mock.patch.object(PT, "_dynamic_position_limits",
                                   lambda conn, *, cycle_id=None, asof_day=None: dict(budget)):
            with PT._db(immediate=True) as conn:
                result = PT._apply_slot_borrow(
                    conn, ACCOUNT, upgrade, DAY, cycle_id=cycle8)
        self.assertTrue(result.get("allowed"),
                        f"donor 持仓数取错了周期（应当只读显式 cycle 8）：{result.get('reason')}")
        after = self._version_row(v8)
        self.assertEqual(PT._loads(after["limits"], {})[donor], 5)
        self.assertEqual(PT._loads(after["limits"], {})[ACCOUNT], 3)

    def test_rpl_p5d_borrow_budget_is_derived_from_the_explicit_cycle(self):
        """席位预算（allocation_version / target_limit / donors）也必须来自显式周期。

        审查发现的真实缺陷：``resolved_cycle_id`` 曾只约束 review 查询，而
        ``_dynamic_position_limits()`` 仍自行 ``_active_cycle()``，于是预算取自更新的
        active cycle，``_apply_slot_borrow`` 又拿 active cycle 的
        ``allocation_version`` 去查显式周期的版本行 —— 一次**合法的同周期借位**会被
        误判成"未找到当前席位版本"而拒绝。

        这里不 mock 预算，走真实调用链，只断言"显式周期请求必须能借到席位"。
        """
        cycle8 = self.cycle_id()
        cycle9 = self._seed_cycle(f"r18-p5d-{cycle8}")
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (cycle8,))
        self.assertEqual(self.cycle_id(), cycle9, "fixture 没把 active cycle 翻到 9")

        upgrade = {
            "borrow_ready": True, "borrow_candidate_score": 80.0,
            "donors": [{"account_id": "shared_pool", "limit": 15, "count": 3,
                        "remaining_after": 3, "unused_pool_slots": 12}],
        }
        with PT._db(immediate=True) as conn:
            result = PT._apply_slot_borrow(
                conn, ACCOUNT, upgrade, DAY, cycle_id=cycle8)
        self.assertTrue(
            result.get("allowed"),
            "显式 cycle 8 的同周期借位被拒绝（预算/版本行取自 active cycle 9）："
            f"{result.get('reason')}")
        version_id = int(str(result["allocation_version"]).rsplit("v", 1)[-1])
        with PT._db() as probe:
            row = probe.execute(
                "SELECT cycle_id FROM paper_position_limit_versions WHERE id=?",
                (version_id,)).fetchone()
        self.assertEqual(int(row["cycle_id"]), cycle8,
                         "借位写进了别的周期的席位版本行")

    def _cycle_positions(self, cycle_id, account_id):
        with PT._db() as conn:
            return [dict(row) for row in PT.PPRM.positions_for_cycle(
                conn, cycle_id, account_id=account_id, asof_day=DAY)]

    def _add_pending_buy(self, *, code, cycle_id, account_id=ACCOUNT,
                         status="pending_limit"):
        """一条**可执行**的在途 BUY 委托（占席位），归属显式周期。"""
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

    def test_rpl_p5e_pending_slots_are_read_from_the_explicit_cycle(self):
        """cycle8 请求 / cycle9 active：cycle8 的 occupied_pool 必须忽略 cycle9 的
        pending BUY。

        修复前 ``_pending_position_slots()`` 完全没有 ``cycle_id``：请求 cycle 8 的
        席位比较会把 cycle 9 那 3 个在途买单算进 occupied_pool，于是 shared_pool
        donor 消失、borrow/upgrade 状态被**另一个周期**的在途委托改写。
        """
        cycle8 = self.cycle_id()
        cycle9 = self._seed_cycle(f"r18-p5e-{cycle8}")
        for index in range(3):
            self._add_pending_buy(code=f"6001{index:02d}", cycle_id=cycle9)
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (cycle8,))
        self.assertEqual(self.cycle_id(), cycle9, "fixture 没把 active cycle 翻到 9")

        budget = {"pool_limit": 3, "limits": {ACCOUNT: 2, OTHER: 3},
                  "allocation_version": "slots-v1"}
        signal = {"code": "600009", "payload": "{}"}
        with mock.patch.object(
                PT, "_dynamic_position_limits",
                lambda conn, *, cycle_id=None, asof_day=None: dict(budget)):
            with PT._db() as conn:
                ctx8 = PT._slot_upgrade_context(
                    conn, ACCOUNT, signal, [], DAY, cycle_id=cycle8)
                ctx9 = PT._slot_upgrade_context(
                    conn, ACCOUNT, signal, [], DAY, cycle_id=cycle9)

        donor8 = [item["account_id"] for item in ctx8["donors"]]
        donor9 = [item["account_id"] for item in ctx9["donors"]]
        # 非空门禁：这些在途委托确实占席位 —— 请求 cycle 9 时共享池席位被占满。
        self.assertNotIn("shared_pool", donor9,
                         f"fixture 的 cycle 9 在途买单没有占席位：{donor9}")
        self.assertIn("shared_pool", donor8,
                      f"cycle 8 的 occupied_pool 混进了 cycle 9 的 pending BUY：{donor8}")

    def test_rpl_p5f_cluster_evidence_is_cycle_and_asof_bound(self):
        """cycle8 请求 / cycle9 active：簇画像证据必须只来自 cycle 8 且截至 asof。

        这是 Blocker 2 的根因链：``_dynamic_position_limits(cycle_id=8)`` 内部经
        ``_strategy_cluster_factors`` → ``_strategy_cluster_profiles`` 读
        ``_position_rows()``（重新解析 active cycle 9）并且 signal 查询没有
        ``intended_date <= day`` 上界 —— 于是 cycle 8 的
        ``cluster_diversification`` / fingerprint / allocation version 由 cycle 9 的
        持仓（甚至 asof 之后的 signal）决定。
        """
        cycle8 = self.cycle_id()
        cycle9 = self._seed_cycle(f"r18-p5f-{cycle8}")
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (cycle8,))
        self.assertEqual(self.cycle_id(), cycle9, "fixture 没把 active cycle 翻到 9")

        # cycle 9（active）持有 3 只仓位；cycle 8 为空。
        for index in range(3):
            self.add_lot(code=f"6002{index:02d}", cycle_id=cycle9)
        # signal 证据：asof 当天一条（合法），asof 之后一条（未来，必须排除）。
        self.add_signal(code="600301", intended_date=DAY.isoformat())
        self.add_signal(code="600302", intended_date=DAY_NEXT.isoformat())

        def profile(cycle_id):
            with PT._db() as conn:
                return PT._strategy_cluster_profiles(
                    conn, DAY, [ACCOUNT], cycle_id=cycle_id)[ACCOUNT]

        live = profile(None)
        self.assertEqual(
            {f"6002{index:02d}" for index in range(3)},
            set(live["positions"]),
            "current/live 语义仍必须读 active cycle 的持仓（否则 fixture 是空的）")

        requested = profile(cycle8)
        self.assertEqual(
            set(), set(requested["positions"]),
            f"cycle 8 的簇画像混进了 cycle 9 的持仓：{sorted(requested['positions'])}")
        self.assertIn("600301", requested["signals"],
                      "asof 当天的 signal 证据被误杀")
        self.assertNotIn("600302", requested["signals"],
                         "asof 之后的 signal 被当成簇画像证据（future leakage）")

    def test_rpl_p5b_borrow_does_write_its_own_cycle(self):
        """正向对照：显式周期**匹配**时借位必须真的写入（证明上两条不是空门禁）。"""
        cycle8 = self.cycle_id()
        v8 = self._seed_allocation_version(
            cycle8, limits={"tq_breakout": 2, "sector_rotation": 6})
        budget = {
            "pool_limit": 15, "limits": {"tq_breakout": 2, "sector_rotation": 6},
            "allocation_version": f"slots-v{v8}",
        }
        upgrade = {
            "borrow_ready": True, "borrow_candidate_score": 80.0,
            "donors": [{"account_id": "shared_pool", "limit": 15, "count": 3,
                        "remaining_after": 3, "unused_pool_slots": 12}],
        }
        with mock.patch.object(PT, "_dynamic_position_limits",
                                   lambda conn, *, cycle_id=None, asof_day=None: dict(budget)):
            with PT._db(immediate=True) as conn:
                result = PT._apply_slot_borrow(
                    conn, ACCOUNT, upgrade, DAY, cycle_id=cycle8)
        self.assertTrue(result.get("allowed"), f"同周期借位被拒绝：{result.get('reason')}")
        after = self._version_row(v8)
        self.assertEqual(PT._loads(after["limits"], {})["tq_breakout"], 3)
        inputs = PT._loads(after["inputs"], {})
        self.assertTrue(inputs.get("slot_borrow_events"))


class ProductionBuyOrderCycleFence(ProductionCandidateCase):
    """RPL-P5g / RPL-P5h —— 真实 ``_buy_order`` 主路径的周期与 as-of 连续性。

    前面几条 P5* 都只驱动 ``_slot_upgrade_context`` / ``_apply_slot_borrow``
    这些 helper。开仓主路径 ``_buy_order`` 自己也有两处容量读取：
    pending 席位与 allocation 预算。它们同样必须钉在
    ``current_cycle["id"]`` + 本次 ``asof_day`` 上。
    """

    ALL_ACCOUNTS = (
        ACCOUNT, "trend_pullback", OTHER, "reported_profit_breakout",
        "main_force_top10",
    )

    def _seed_cycle(self, key, enabled_strategies):
        with PT._db(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,started_at,"
                "created_at,updated_at,enabled_strategies) VALUES(?,'running',1000000.0,"
                "'balanced',?,?,?,?)",
                (key, f"{DAY.isoformat()} 00:00:00", f"{DAY.isoformat()} 00:00:00",
                 f"{DAY.isoformat()} 00:00:00", enabled_strategies))
            return int(cur.lastrowid)

    def _activate(self, key):
        """把 active cycle 翻到新建的周期，并把五个账户全部挂上去。"""
        requested = self.cycle_id()
        with PT._db() as conn:
            enabled = conn.execute(
                "SELECT enabled_strategies FROM paper_cycles WHERE id=?",
                (requested,)).fetchone()["enabled_strategies"]
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (requested,))
        active = self._seed_cycle(key, enabled)
        with PT._db(immediate=True) as conn:
            conn.executemany(
                "UPDATE paper_accounts SET cycle_id=?, status='running' WHERE id=?",
                [(active, account_id) for account_id in self.ALL_ACCOUNTS])
        self.assertEqual(self.cycle_id(), active, "fixture 没把 active cycle 翻过去")
        return requested, active

    def _add_pending_buy(self, *, code, cycle_id, account_id=ACCOUNT,
                         status="pending_limit"):
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

    def _add_lot(self, *, code, cycle_id, account_id=ACCOUNT):
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, account_id)
            order_id = conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
                "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
                "execution_verified,execution_status) VALUES(?,'buy',?,?,100,10.0,10.0,1000.0,"
                "5.0,'filled','seed_buy','{}',?,?,'market','seed',?,?,?,?,1,'verified')",
                (account_id, code, f"测试股_{code}", "2026-08-20 09:30:00",
                 "2026-08-20 09:30:00", *stamp, cycle_id)).lastrowid
            conn.execute(
                "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
                "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
                "is_t_base,source_order_id) VALUES(?,?,?,?,?,100,100,10.0,?,?,'stock_t1',1,1,?)",
                (cycle_id, account_id, code, f"测试股_{code}", "Tech",
                 "2026-08-20 10:00:00", "2026-08-21", order_id),
            )

    def _add_review(self, *, code, cycle_id, score=20.0, account_id=ACCOUNT):
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_position_reviews(cycle_id,account_id,code,review_date,"
                "score,grade,action,market_value,position_pct,reasons,detail,created_at) "
                "VALUES(?,?,?,?,?,'观察','hold',1000.0,1.0,'fixture','{}',?)",
                (cycle_id, account_id, code, DAY.isoformat(), score,
                 f"{DAY.isoformat()} 14:50:00"),
            )

    def _run_buy_order(self, *, signal_id, code):
        """驱动**真实** ``_buy_order``，返回 (结果, 落库的 risk_payload)。"""
        self.set_quote(code)
        with PT._db() as conn:
            signal = dict(conn.execute(
                "SELECT * FROM paper_signals WHERE id=?", (signal_id,)).fetchone())
        with PT._db(immediate=True) as conn:
            result = PT._buy_order(
                conn, {"id": ACCOUNT}, signal, self.quotes[code], dict(MARKET), [], DAY,
                all_quotes=dict(self.quotes),
            )
        with PT._db() as conn:
            order = conn.execute(
                "SELECT risk_payload FROM paper_orders WHERE signal_id=? ORDER BY id DESC LIMIT 1",
                (signal_id,)).fetchone()
        payload = PT._loads(order["risk_payload"], {}) if order is not None else {}
        return result, payload

    def test_rpl_p5g_buy_order_pending_slots_are_cycle_bound(self):
        """cycle8 的 3 个在途 BUY 不得占掉 cycle9 的席位（真实 ``_buy_order``）。

        修复前 ``_buy_order`` 虽然已经拿到 ``current_cycle``，却仍调用
        ``_pending_position_slots(conn, positions)``：更新的 active cycle 在正常开仓
        时会把**上一个周期**的在途买单算进 ``committed_open_codes`` /
        ``pool_open_positions``，把一次合法开仓错误 defer/reject。
        """
        requested, active = self._activate("r18-p5g-requested")
        for index in range(3):
            self._add_pending_buy(code=f"6001{index:02d}", cycle_id=requested)
        signal_id = self.add_signal(code="600900", intended_date=DAY.isoformat(),
                                    signal_date=DAY_PREV.isoformat())
        _result, payload = self._run_buy_order(signal_id=signal_id, code="600900")
        gate = payload["position_count_gate"]
        self.assertEqual(active, self.cycle_id())
        # 非空门禁：这 3 张单**确实**占席位 —— 请求 cycle8 时必须看得到。
        with PT._db() as conn:
            occupied = PT._pending_position_slots(conn, [], cycle_id=requested)
        self.assertEqual(3, len(occupied), "fixture 的 cycle 8 在途买单没有占席位")
        self.assertEqual(
            0, gate["committed"],
            f"cycle 9 的开仓把 cycle 8 的在途买单算进了承诺席位：{gate}")
        self.assertEqual(
            0, gate["pool_current"],
            f"cycle 9 的开仓把 cycle 8 的在途买单算进了共享池席位：{gate}")

    def _seed_asof_split_fixture(self, *, lots):
        """建一个让 (cycle, as-of) 解析出**不同**席位版本的 fixture。

        A/B 两策略持有完全相同的 ``lots`` 支股票（position/industry jaccard = 1.0），
        并共享一条 ``intended_date`` 只落在"机器今天"窗口内的 signal ⇒ 有界与无界
        as-of 的簇证据不同，fingerprint / allocation version 必然分叉。返回
        ``(active, bounded, unbounded)``。
        """
        _requested, active = self._activate(f"r18-asof-split-{len(lots)}")
        for code in lots:
            self._add_lot(code=code, cycle_id=active)
            self._add_lot(code=code, cycle_id=active, account_id=OTHER)
            self._add_review(code=code, cycle_id=active)
        with PT._db(immediate=True) as conn:
            PT._sync_positions(conn, asof_day=DAY)
        only_today = (dt.date.today() - dt.timedelta(days=5)).isoformat()
        self.add_signal(code="600888", intended_date=only_today)
        self.add_signal(code="600888", intended_date=only_today, account_id=OTHER)
        with PT._db() as conn:
            bounded = PT._dynamic_position_limits(conn, cycle_id=active, asof_day=DAY)
        with PT._db() as conn:
            unbounded = PT._dynamic_position_limits(conn, cycle_id=active)
        self.assertNotEqual(
            bounded["allocation_version"], unbounded["allocation_version"],
            "fixture 没能让 as-of 有界/无界解析出不同的席位版本（断言会是空门禁）")
        self.assertNotEqual(
            bounded["allocation_key"], unbounded["allocation_key"],
            "fixture 的两个 as-of 解析出了同一个 allocation key")
        return active, bounded, unbounded

    def test_rpl_p5h_buy_order_post_borrow_reread_keeps_the_same_asof(self):
        """借位后的 re-read 必须回到**同一个** as-of 有界的席位版本。

        修复前 re-read 只带 cycle、漏传 ``asof_day`` ⇒ 解析到"机器今天"的版本，
        刚刚借到的席位在 ``position_count_gate`` 里消失，``strategy_count_blocked``
        又变回 true。
        """
        active, bounded, unbounded = self._seed_asof_split_fixture(
            lots=["600001", "600002", "600003"])
        # 非空门禁：无界 as-of 的上限确实低于持仓数（否则不会触发借位），
        # 且有界 as-of 的上限更高 —— 两个 as-of 的判定口径真的不同。
        crowded = 3
        self.assertLess(
            unbounded["limits"][ACCOUNT], crowded,
            "fixture 在无界 as-of 下没有达到席位上限（不会触发借位）")
        self.assertGreater(
            bounded["limits"][ACCOUNT], unbounded["limits"][ACCOUNT],
            "fixture 的 as-of 有界上限没有高于无界上限")

        signal_id = self.add_signal(code="600901", intended_date=DAY.isoformat(),
                                    signal_date=DAY_PREV.isoformat())
        _result, payload = self._run_buy_order(signal_id=signal_id, code="600901")
        borrow = payload.get("slot_borrow") or {}
        self.assertTrue(borrow.get("allowed"), f"fixture 没能真的借到席位：{borrow}")
        self.assertEqual(
            bounded["allocation_version"], borrow["allocation_version"],
            "借位写进了另一个 as-of 的席位版本")
        gate = payload["position_count_gate"]
        self.assertEqual(
            borrow["allocation_version"], gate["allocation_version"],
            "借位后的 re-read 解析到了另一个 as-of 的席位版本（借到的席位消失了）")
        self.assertEqual(
            borrow["limits_after"][ACCOUNT], gate["limit"],
            f"借位后的 limit 没有反映刚借到的席位：{gate} vs {borrow}")
        self.assertEqual(active, self.cycle_id())

    def test_rpl_p5i_buy_order_initial_budget_uses_the_asof(self):
        """初次预算也必须用本次 as-of：本周期还有余量时不得无谓借位。

        修复前初次预算只带 cycle ⇒ 用"机器今天"的收紧上限判定席位已满，于是触发
        ``_slot_upgrade_context`` / ``_apply_slot_borrow``，把 donor 的席位白削一刀。
        """
        _active, bounded, unbounded = self._seed_asof_split_fixture(
            lots=["600001", "600002"])
        crowded = 2
        self.assertLessEqual(
            unbounded["limits"][ACCOUNT], crowded,
            "fixture 在无界 as-of 下没有达到席位上限")
        self.assertGreater(
            bounded["limits"][ACCOUNT], crowded,
            "fixture 的 as-of 有界上限没有留出余量（无法证明'不该借位'）")

        signal_id = self.add_signal(code="600902", intended_date=DAY.isoformat(),
                                    signal_date=DAY_PREV.isoformat())
        _result, payload = self._run_buy_order(signal_id=signal_id, code="600902")
        gate = payload["position_count_gate"]
        self.assertEqual(
            bounded["limits"][ACCOUNT], gate["limit"],
            "初次预算没有使用本次 as-of 的席位上限")
        self.assertNotIn(
            "slot_upgrade", payload,
            "本周期还有余量，却进入了席位比较（初次预算漏传 as-of）")
        self.assertNotIn(
            "slot_borrow", payload,
            "本周期还有余量，却无谓触发了借位（初次预算漏传 as-of）")


class SlotUpgradeContractTests(unittest.TestCase):
    """纯模块层面钉住 T+1 / min-hold / 借位优先级（不依赖 DB）。"""

    policy = PRep.ReplacementPolicy()

    def weakest(self, **over):
        base = {"code": "600001", "name": "测试股", "score": 40.0, "hold_days": 10,
                "available_qty": 100, "review_action": "hold"}
        base.update(over)
        return base

    def test_t1_locked_is_not_bypassed_by_a_strong_candidate(self):
        ctx = PRep.decide_slot_upgrade(
            candidate_score=100.0, weakest=self.weakest(score=10.0, available_qty=0),
            target_limit=3, donors=[], at_dynamic_limit=True, min_hold_days=2,
            policy=self.policy)
        self.assertEqual(ctx["state"], "t1_locked")
        self.assertFalse(ctx["eligible"])

    def test_min_hold_blocks_non_urgent_upgrade(self):
        ctx = PRep.decide_slot_upgrade(
            candidate_score=70.0, weakest=self.weakest(score=30.0, hold_days=0),
            target_limit=3, donors=[], at_dynamic_limit=True, min_hold_days=2,
            policy=self.policy)
        self.assertEqual(ctx["state"], "observe")
        self.assertFalse(ctx["eligible"])

    def test_urgent_upgrade_ignores_min_hold(self):
        ctx = PRep.decide_slot_upgrade(
            candidate_score=90.0, weakest=self.weakest(score=30.0, hold_days=0),
            target_limit=3, donors=[], at_dynamic_limit=True, min_hold_days=5,
            policy=self.policy)
        self.assertEqual(ctx["state"], "urgent_upgrade")
        self.assertTrue(ctx["eligible"])

    def test_donor_requires_unused_slot_above_the_floor(self):
        donors = PRep.derive_donors(
            limits={"tq_breakout": 3, "sector_rotation": 5, "other": 2},
            counts={"tq_breakout": 3, "sector_rotation": 5, "other": 2},
            account_id="tq_breakout", pool_limit=15, occupied_pool_count=15,
            policy=self.policy)
        self.assertEqual([d["account_id"] for d in donors], [],
                         "已满 / 已达底座的策略被当成 donor")

    def test_shared_pool_donor_is_lent_when_pool_has_free_seats(self):
        donors = PRep.derive_donors(
            limits={"tq_breakout": 3}, counts={"tq_breakout": 3},
            account_id="tq_breakout", pool_limit=15, occupied_pool_count=8,
            policy=self.policy)
        self.assertEqual([d["account_id"] for d in donors], ["shared_pool"])

    def test_same_input_yields_identical_output(self):
        kwargs = dict(candidate_score=80.0, weakest=self.weakest(score=30.0),
                      target_limit=3, donors=[], at_dynamic_limit=True,
                      min_hold_days=2, policy=self.policy)
        self.assertEqual(PRep.decide_slot_upgrade(**kwargs),
                         PRep.decide_slot_upgrade(**kwargs))


if __name__ == "__main__":
    unittest.main()
