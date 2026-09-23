# -*- coding: utf-8 -*-
"""R17：episode 入场 signal provenance 契约 + 生产回归。

    RP-01  verified opening order 精确解析到原 signal
    RP-02  后来的同账户/代码 signal 被忽略
    RP-03  未来 signal 被忽略（asof 上界）
    RP-04  错周期的 order 被拒绝
    RP-05  错账户的 order 被拒绝
    RP-06  错代码的 order 被拒绝
    RP-07  未验证 order 被拒绝
    RP-08  非 filled order 被拒绝
    RP-09  signal_id 缺失 → unknown
    RP-10  signal 行不存在 → unknown
    RP-11  signal 身份不匹配 → unknown
    RP-12  opened_order_id 缺失 → unknown
    RP-13  add-on 不替换 episode origin
    RP-14  full exit + re-entry 使用新 origin
    RP-15  归档（paper_signals_archive）后的 episode signal 仍按精确 id 解析
    RP-16  归档行同样受身份与 asof 校验约束，且绝不当作 latest 回退

生产回归（直接驱动 ``PT._position_quality_score`` / ``PT.monitor_risk``）：

    RISK-REVIEW-P1   后来无关 signal 不改变 production model score
    RISK-REVIEW-P2   未来 signal 不进入 historical as-of
    RISK-REVIEW-P3   缺失 provenance 保持中性 50 且不猜 latest
    RISK-REVIEW-P4   错误 provenance 不再制造 false consolidation_exit
    RISK-REVIEW-P10  分批建仓 signal 归档后真实模型分不退化
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

import paper_position_review_evidence as PREV  # noqa: E402
import paper_trading as PT  # noqa: E402
import tradability_archive as TA  # noqa: E402
import universe as U  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600000"
DAY = dt.date(2026, 9, 10)
DAY_NEXT = dt.date(2026, 9, 11)


class _ResolverCase(unittest.TestCase):
    """只含 provenance 所需三张表的极简 SQLite。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, status TEXT);
            INSERT INTO paper_cycles(id,status) VALUES(1,'running'),(2,'paused');
            CREATE TABLE paper_orders(
                id INTEGER PRIMARY KEY, cycle_id INTEGER, account_id TEXT, code TEXT,
                side TEXT, status TEXT, signal_id INTEGER,
                execution_verified INTEGER, execution_status TEXT);
            CREATE TABLE paper_signals(
                id INTEGER PRIMARY KEY, account_id TEXT, code TEXT, signal_date TEXT,
                rank_score REAL, t_score REAL, payload TEXT);
            """
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def add_order(self, order_id, *, cycle_id=1, account_id=ACCOUNT, code=CODE,
                  side="buy", status="filled", signal_id=55, verified=1,
                  exec_status="verified"):
        self.conn.execute(
            "INSERT INTO paper_orders(id,cycle_id,account_id,code,side,status,signal_id,"
            "execution_verified,execution_status) VALUES(?,?,?,?,?,?,?,?,?)",
            (order_id, cycle_id, account_id, code, side, status, signal_id,
             verified, exec_status),
        )
        self.conn.commit()

    def add_signal(self, signal_id, *, account_id=ACCOUNT, code=CODE,
                   signal_date="2026-09-10", rank_score=90.0, t_score=90.0):
        self.conn.execute(
            "INSERT INTO paper_signals(id,account_id,code,signal_date,rank_score,t_score,"
            "payload) VALUES(?,?,?,?,?,?,'{}')",
            (signal_id, account_id, code, signal_date, rank_score, t_score),
        )
        self.conn.commit()

    def archive_signal(self, signal_id):
        """把 signal 搬进归档表（与 ``_cleanup_stale_data`` 同一形状：id 不变）。"""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS paper_signals_archive("
            "id INTEGER, account_id TEXT, code TEXT, signal_date TEXT,"
            "rank_score REAL, t_score REAL, payload TEXT)")
        self.conn.execute(
            "INSERT INTO paper_signals_archive"
            " SELECT * FROM paper_signals WHERE id=?", (signal_id,))
        self.conn.execute("DELETE FROM paper_signals WHERE id=?", (signal_id,))
        self.conn.commit()

    def resolve(self, *, cycle_id=1, opened_order_id=101, asof_day="2026-09-10",
                account_id=ACCOUNT, code=CODE):
        return PREV.resolve_entry_signal(
            self.conn, cycle_id=cycle_id, account_id=account_id, code=code,
            opened_order_id=opened_order_id, asof_day=asof_day)


class ResolverContractTests(_ResolverCase):
    """RP-01 … RP-12 —— resolver 的精确性与 fail-closed。"""

    def test_rp01_verified_order_resolves_exact_signal(self):
        self.add_signal(55)
        self.add_order(101, signal_id=55)
        result = self.resolve()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["opened_order_id"], 101)
        self.assertEqual(result["signal_id"], 55)
        self.assertEqual(result["signal_date"], "2026-09-10")
        self.assertEqual(result["signal"]["rank_score"], 90.0)

    def test_rp02_later_signal_is_ignored(self):
        """resolver 只按 signal_id 精确 lookup，库里另有一条更新的 signal 也不看。"""
        self.add_signal(55, signal_date="2026-09-10", rank_score=90.0)
        self.add_signal(99, signal_date="2026-09-11", rank_score=10.0)
        self.add_order(101, signal_id=55)
        result = self.resolve(asof_day="2026-09-11")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["signal_id"], 55, "解析到了更新的 signal（latest 搜索）")

    def test_rp03_future_signal_is_unknown(self):
        self.add_signal(55, signal_date="2026-09-11")
        self.add_order(101, signal_id=55)
        result = self.resolve(asof_day="2026-09-10")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "signal_after_asof")

    def test_rp04_wrong_cycle_order_rejected(self):
        self.add_signal(55)
        self.add_order(101, cycle_id=2, signal_id=55)
        result = self.resolve(cycle_id=1)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "order_cycle_mismatch")

    def test_rp05_wrong_account_order_rejected(self):
        self.add_signal(55)
        self.add_order(101, account_id="sector_rotation", signal_id=55)
        result = self.resolve()
        self.assertEqual(result["reason"], "order_identity_mismatch")

    def test_rp06_wrong_code_order_rejected(self):
        self.add_signal(55)
        self.add_order(101, code="000001", signal_id=55)
        result = self.resolve()
        self.assertEqual(result["reason"], "order_identity_mismatch")

    def test_rp07_unverified_order_rejected(self):
        self.add_signal(55)
        self.add_order(101, signal_id=55, verified=0, exec_status="unknown")
        result = self.resolve()
        self.assertEqual(result["reason"], "order_unverified")

    def test_rp08_non_filled_order_rejected(self):
        self.add_signal(55)
        self.add_order(101, signal_id=55, status="unfilled_limit_down")
        result = self.resolve()
        self.assertEqual(result["reason"], "order_not_filled")

    def test_rp08b_non_buy_order_rejected(self):
        self.add_signal(55)
        self.add_order(101, signal_id=55, side="sell")
        result = self.resolve()
        self.assertEqual(result["reason"], "order_not_buy")

    def test_rp09_missing_signal_id_is_unknown(self):
        self.add_order(101, signal_id=None)
        result = self.resolve()
        self.assertEqual(result["reason"], "missing_signal_id")

    def test_rp10_missing_signal_row_is_unknown(self):
        self.add_order(101, signal_id=777)
        result = self.resolve()
        self.assertEqual(result["reason"], "signal_not_found")

    def test_rp11_signal_identity_mismatch_is_unknown(self):
        self.add_signal(55, account_id="sector_rotation")
        self.add_order(101, signal_id=55)
        result = self.resolve()
        self.assertEqual(result["reason"], "signal_identity_mismatch")

    def test_rp12_missing_opened_order_id_is_unknown(self):
        self.assertEqual(self.resolve(opened_order_id=None)["reason"],
                         "missing_opened_order_id")

    def test_rp12b_missing_order_row_is_unknown(self):
        self.assertEqual(self.resolve(opened_order_id=999)["reason"], "order_not_found")

    def test_never_latest_search_static(self):
        """静态：resolver **代码**里不得出现 latest-signal 搜索（docstring 除外）。"""
        with open(os.path.join(BACKEND_DIR, "paper_position_review_evidence.py"),
                  encoding="utf-8") as fh:
            raw = fh.read()
        tree = ast.parse(raw)
        first = tree.body[0]
        self.assertIsInstance(first, ast.Expr)
        body = "\n".join(raw.splitlines()[first.end_lineno:])
        self.assertNotIn("ORDER BY signal_date", body)
        self.assertNotIn("ORDER BY signal_date DESC", body)
        self.assertNotIn("LIMIT 1", body)

    def test_rp15_archived_entry_signal_still_resolves_by_exact_id(self):
        """分批建仓首片成交后 signal 被 _cleanup_stale_data 归档，链仍然可证。"""
        self.add_signal(55, rank_score=90.0, t_score=90.0)
        self.add_order(101, signal_id=55)
        self.archive_signal(55)
        result = self.resolve()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["reason"], None)
        self.assertEqual(result["signal_id"], 55)
        self.assertEqual(result["signal"]["rank_score"], 90.0)
        self.assertEqual(result["signal_source"], "paper_signals_archive")

    def test_rp16_archived_signal_keeps_identity_and_asof_checks(self):
        """归档不是"放行"：身份与 asof 校验在归档行上一样成立。"""
        self.add_signal(55, account_id="sector_rotation")
        self.add_order(101, signal_id=55)
        self.archive_signal(55)
        self.assertEqual(self.resolve()["reason"], "signal_identity_mismatch")

        self.add_signal(56, signal_date="2026-09-11")
        self.add_order(102, signal_id=56)
        self.archive_signal(56)
        result = self.resolve(opened_order_id=102, asof_day="2026-09-10")
        self.assertEqual(result["reason"], "signal_after_asof")

    def test_rp16b_archive_never_used_as_latest_fallback(self):
        """归档表里另有一行同账户同代码的 signal：仍只按 id 精确命中。"""
        self.add_signal(55, signal_date="2026-09-10", rank_score=90.0)
        self.add_order(101, signal_id=55)
        self.archive_signal(55)
        self.add_signal(77, signal_date="2026-09-11", rank_score=10.0)
        self.archive_signal(77)
        result = self.resolve(asof_day="2026-09-11")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["signal_id"], 55, "归档表被当成 latest 搜索用了")


class ProductionEpisodeCase(unittest.TestCase):
    """真实生产 schema 的临时账本（用于 production regression）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "paper_r17.sqlite3")
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

    def add_signal(self, *, score, signal_date, t_score=None, rank_score=None):
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, ACCOUNT)
            cur = conn.execute(
                "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
                "close_price,rank_score,t_tier,t_score,payload,status,created_at,"
                "strategy_id,strategy_version,strategy_checksum,cycle_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)",
                (ACCOUNT, signal_date, signal_date, CODE, "测试股", 10.0, rank_score, "A",
                 t_score, PT._json({"decision": {"entry_model": {"score": score}}}),
                 f"{signal_date} 15:00:00", *stamp, self.cycle_id()),
            )
            return int(cur.lastrowid)

    def add_buy_order(self, *, signal_id, qty=100, price=10.0):
        cycle_id = self.cycle_id()
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, ACCOUNT)
            cur = conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
                "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
                "signal_id,execution_verified,execution_status) "
                "VALUES(?,?,?,?,?,?,?,?,5.0,'filled','seed_buy','{}',?,?,'market','seed',?,?,?,?,?,1,'verified')",
                (ACCOUNT, "buy", CODE, "测试股", qty, price, price, qty * price,
                 "2026-09-09 09:30:00", "2026-09-09 09:30:00", *stamp, cycle_id, signal_id),
            )
            order_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,"
                "fees,fill_date,quote_at,assumption) "
                "VALUES(?,?,'buy',?,?,?,?,5.0,?,?,'seed')",
                (order_id, ACCOUNT, CODE, qty, price, qty * price,
                 "2026-09-09", "2026-09-09 09:30:00"),
            )
            return order_id

    def add_lot(self, *, order_id, qty=100, cost=10.0):
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
                "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
                "is_t_base,source_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,'stock_t1',1,1,?)",
                (self.cycle_id(), ACCOUNT, CODE, "测试股", "Tech", qty, qty, cost,
                 "2026-09-08 10:00:00", "2026-09-09", order_id),
            )

    def add_risk_state(self, *, opened_order_id):
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_position_risk_state(cycle_id,account_id,code,peak_price,"
                "take_stage,opened_order_id,initialized_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (self.cycle_id(), ACCOUNT, CODE, 10.0, 0, opened_order_id,
                 "2026-09-08 10:00:00", "2026-09-08 10:00:00"),
            )

    def build_episode(self, *, episode_score=90.0, with_risk_state=True,
                      episode_date="2026-09-10"):
        signal_a = self.add_signal(score=episode_score, signal_date=episode_date,
                                   t_score=episode_score, rank_score=episode_score)
        order_a = self.add_buy_order(signal_id=signal_a)
        self.add_lot(order_id=order_a)
        if with_risk_state:
            self.add_risk_state(opened_order_id=order_a)
        return signal_a, order_a

    def position(self):
        with PT._db() as conn:
            rows = PT._position_rows(conn, asof_day=DAY)
        return [p for p in rows if p["code"] == CODE][0]

    def review(self, *, asof_day=DAY):
        position = self.position()
        quote = {"code": CODE, "price": 10.0, "pct": 0.0, "high": 10.0, "low": 10.0}
        with PT._db() as conn:
            return PT._position_quality_score(conn, position, quote, asof_day,
                                              cycle_id=self.cycle_id())

    def action(self, review):
        position = dict(self.position())
        position["available_qty"] = 100
        return PT.PReview.decide_action(review, position, {"fresh": True, "reason": "ok"},
                                        0, policy=PT.REVIEW_POLICY)


class ProductionProvenanceRegression(ProductionEpisodeCase):
    """RISK-REVIEW-P1 … P4 —— 直接驱动生产函数。"""

    def test_risk_review_p1_later_signal_does_not_change_model_score(self):
        self.build_episode(episode_score=90.0)
        before = self.review(asof_day=DAY_NEXT)
        self.add_signal(score=10.0, signal_date="2026-09-11",
                        t_score=10.0, rank_score=10.0)
        after = self.review(asof_day=DAY_NEXT)
        self.assertEqual(before["model_score"], 90.0)
        self.assertEqual(after["model_score"], 90.0,
                         "后来的无关 signal 改变了 production model score")
        self.assertEqual(after["model_score_source"], "episode_provenance")
        self.assertEqual(after["entry_signal_provenance_status"], "verified")

    def test_risk_review_p2_future_signal_does_not_leak(self):
        self.build_episode(episode_score=90.0)
        self.add_signal(score=10.0, signal_date="2026-09-11",
                        t_score=10.0, rank_score=10.0)
        review = self.review(asof_day=DAY)  # asof = 09-10
        self.assertEqual(review["model_score"], 90.0, "未来 signal 泄漏进历史 as-of")

    def test_risk_review_p2b_episode_signal_after_asof_stays_unknown(self):
        """episode 自己的 signal 晚于 asof ⇒ 不可证明 ⇒ 中性 50（asof 上界）。

        这条才是 ``signal_date <= asof_day`` 的直接判据：删掉该上界，回放
        ``asof=09-10`` 就会读到 09-11 的 episode signal 并给出它的分数。
        """
        self.build_episode(episode_score=90.0, episode_date="2026-09-11")
        review = self.review(asof_day=DAY)  # asof = 09-10，episode signal 在 09-11
        self.assertEqual(
            review["model_score"], 50.0,
            "回放 asof 早于 episode signal 日期时读到了未来 signal",
        )
        self.assertEqual(review["entry_signal_provenance_status"], "unknown")
        self.assertEqual(review["entry_signal_provenance_reason"], "signal_after_asof")

    def test_risk_review_p3_missing_provenance_stays_neutral(self):
        self.build_episode(with_risk_state=False)
        review = self.review()
        self.assertEqual(review["model_score"], 50.0)
        self.assertEqual(review["model_score_source"], "unknown")
        self.assertEqual(review["entry_signal_provenance_status"], "unknown")
        self.assertEqual(review["entry_signal_provenance_reason"], "missing_opened_order_id")

    def test_risk_review_p3b_unknown_is_not_latest_guess(self):
        """库里有 signal，但没有 episode provenance ⇒ 仍必须是 50，不能"搜到就用"。"""
        self.add_signal(score=0.0, signal_date="2026-09-10", t_score=0.0, rank_score=0.0)
        order = self.add_buy_order(signal_id=1)
        self.add_lot(order_id=order)
        review = self.review()
        self.assertEqual(review["model_score"], 50.0, "unknown 被洗成了 latest guess")
        self.assertEqual(review["model_score_source"], "unknown")

    def test_risk_review_p4_wrong_provenance_cannot_flip_action(self):
        self.build_episode(episode_score=100.0)
        good = self.review(asof_day=DAY_NEXT)
        action_good, _ = self.action(good)
        self.add_signal(score=0.0, signal_date="2026-09-11", t_score=0.0, rank_score=0.0)
        bad = self.review(asof_day=DAY_NEXT)
        action_bad, reason = self.action(bad)
        self.assertEqual(action_good, "hold")
        self.assertEqual(action_bad, "hold",
                         f"错误 provenance 制造了 false consolidation_exit：{reason}")
        self.assertEqual(good["model_score"], bad["model_score"])

    def test_risk_review_p5_add_on_keeps_episode_origin(self):
        signal_a, order_a = self.build_episode(episode_score=90.0)
        signal_b = self.add_signal(score=10.0, signal_date="2026-09-11",
                                   t_score=10.0, rank_score=10.0)
        order_b = self.add_buy_order(signal_id=signal_b)
        self.add_lot(order_id=order_b)
        with PT._db() as conn:
            state = conn.execute(
                "SELECT opened_order_id FROM paper_position_risk_state"
                " WHERE account_id=? AND code=?", (ACCOUNT, CODE)).fetchone()
        self.assertEqual(int(state["opened_order_id"]), order_a,
                         "add-on 改变了 episode origin")
        review = self.review(asof_day=DAY_NEXT)
        self.assertEqual(review["model_score"], 90.0,
                         "add-on 的 signal 顶掉了 episode origin 的 signal")

    def test_risk_review_p6_full_exit_reentry_uses_new_origin(self):
        _signal_a, order_a = self.build_episode(episode_score=90.0)
        with PT._db(immediate=True) as conn:
            conn.execute("DELETE FROM paper_position_risk_state WHERE account_id=? AND code=?",
                         (ACCOUNT, CODE))
            conn.execute(
                "UPDATE paper_position_lots SET remaining_qty=0 WHERE account_id=? AND code=?",
                (ACCOUNT, CODE))
        signal_c = self.add_signal(score=20.0, signal_date="2026-09-11",
                                   t_score=20.0, rank_score=20.0)
        order_c = self.add_buy_order(signal_id=signal_c)
        self.add_lot(order_id=order_c)
        self.add_risk_state(opened_order_id=order_c)
        with PT._db() as conn:
            state = conn.execute(
                "SELECT opened_order_id FROM paper_position_risk_state"
                " WHERE account_id=? AND code=?", (ACCOUNT, CODE)).fetchone()
        self.assertEqual(int(state["opened_order_id"]), order_c)
        self.assertNotEqual(int(state["opened_order_id"]), order_a)
        review = self.review(asof_day=DAY_NEXT)
        self.assertEqual(review["model_score"], 20.0, "重入后仍沿用旧 provenance")

    def test_risk_review_p7_episode_metadata_exposed(self):
        _signal_a, order_a = self.build_episode(episode_score=90.0)
        position = self.position()
        self.assertEqual(int(position["episode_opened_order_id"]), order_a)

    def test_risk_review_p8_review_date_must_be_explicit(self):
        """``_save_position_review`` 不再有 wall-clock 回退。"""
        with PT._db(immediate=True) as conn:
            with self.assertRaises(ValueError):
                PT._save_position_review(
                    conn, self.cycle_id(),
                    {"account_id": ACCOUNT, "code": CODE, "score": 50.0, "grade": "观察",
                     "market_value": 1000.0, "position_pct": 1.0},
                    "hold", "fixture")

    def test_risk_review_p9_protective_exits_unaffected(self):
        """provenance 只影响 quality/rotation，不影响 hard stop 等保护性退出。"""
        self.build_episode(episode_score=90.0)
        # 现价 9.0（-10%）→ 硬止损应照常触发，与 model provenance 无关
        self.quotes[CODE] = {
            "code": CODE, "name": "测试股", "price": 9.0, "high": 9.2, "low": 8.9,
            "pct": -8.0, "amount": 10000000.0, "volume": 1000000.0, "turnover": 1.0,
            "quote_source": "live", "quote_at": "2026-09-10 14:50:00",
            "execution_asof": "2026-09-10 14:50:00",
            "quote_validation": "cross_source_checked",
        }
        with PT._db(immediate=True) as conn:
            TA.ensure_schema(conn)
            TA.TradabilityArchiveRepository(conn).save(TA.TradabilityEvidence(
                code=CODE, session_date=DAY.isoformat(), is_listed=True,
                listing_date="2000-01-01", delisting_date=None, is_st=False,
                is_suspended=False, suspension_reason=None, has_market_quote=True,
                has_trade_volume=True, is_price_limit_locked=False,
                price_limit_direction=None, source="unit_test_injection",
                observed_at=f"{DAY.isoformat()}T08:50:00+08:00",
                effective_at=f"{DAY.isoformat()}T09:00:00+08:00",
            ))
        result = PT.monitor_risk(DAY)
        sells = [o for o in result.get("orders", []) if o.get("status") == "filled"]
        self.assertTrue(sells, "保护性硬止损被 provenance 改动影响")

    def test_risk_review_p10_archived_entry_signal_keeps_real_model_score(self):
        """分批建仓：首片成交后 signal 被真实 cleanup 归档，分数不得退化成 50。

        ``_buy_order`` 在 sliced entry 首片成交后把 signal 留在
        ``deferred_capacity``，而 ``_cleanup_stale_data`` 会把它搬进
        ``paper_signals_archive`` 并从 ``paper_signals`` 删除。订单链完好，
        所以 episode 的真实模型分必须仍然可证 —— 否则一条**可证明**的持仓会
        被误判成 unknown 并拿到中性 50。
        """
        signal_a, order_a = self.build_episode(episode_score=90.0)
        with PT._db(immediate=True) as conn:
            conn.execute(
                "UPDATE paper_signals SET status='deferred_capacity',"
                " created_at='2020-01-01 09:00:00' WHERE id=?", (signal_a,))
        cleaned = PT._cleanup_stale_data()
        self.assertGreaterEqual(int(cleaned.get("deferred_capacity_signals") or 0), 1)
        with PT._db() as conn:
            self.assertIsNone(conn.execute(
                "SELECT id FROM paper_signals WHERE id=?", (signal_a,)).fetchone())
            self.assertIsNotNone(conn.execute(
                "SELECT id FROM paper_signals_archive WHERE id=?", (signal_a,)).fetchone())
        review = self.review(asof_day=DAY_NEXT)
        self.assertEqual(review["model_score"], 90.0,
                         "归档后的 episode signal 失去了 provenance，退化成中性 50")
        self.assertEqual(review["model_score_source"], "episode_provenance")
        self.assertEqual(review["entry_signal_provenance_status"], "verified")
        self.assertEqual(int(review["entry_signal_id"]), signal_a)
        self.assertEqual(int(review.get("episode_opened_order_id") or 0), order_a)


if __name__ == "__main__":
    unittest.main()
