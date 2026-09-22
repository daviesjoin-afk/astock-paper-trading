# -*- coding: utf-8 -*-
"""R19：普通策略 BUY 的成交提交收敛 + 预占归属生产回归。

直接驱动**真实** ``PT._buy_order``（不 mock 闸门），证明：

    SB-1  普通策略买入经过 execution_planner.commit_fill
    SB-2  正常成交的 order / reservation / lot 同周期
    SB-3  错周期预占 fail closed
    SB-4  错周期预占保持原样（不被改写、不被释放）
    SB-5  冲突的当前订单终态化，候选保留在复试管道
    SB-6  正常预占被消费（consumed）
    SB-7  只有一条成交流水
    SB-8  只有一个 risk 事件
    SB-9  只有一个 audit 事件
    SB-10 提交失败回滚现金
    SB-11 提交失败不留 lot
    SB-12 提交失败不留 fill
    SB-13 提交失败回滚 slot borrow（源码级契约，见说明）
    SB-14 中间片保持 deferred
    SB-15 最后一片标记 filled
    SB-16 执行验证仍然盖章
    EC-21 final BUY sizing 必须消费 cycle-pinned profile / effective spec
"""
from __future__ import annotations

import ast
import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import execution_planner as EP  # noqa: E402
import paper_trading as PT  # noqa: E402
import universe as U  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600901"
DAY = dt.date(2026, 9, 10)
DAY_PREV = dt.date(2026, 9, 9)
CAPITAL = 1_000_000.0
MARKET = {
    "light": "green",
    "overseas": {"light": "green", "advice": "fixture"},
    "breadth": 0.5,
    "sentiment": "neutral",
}


def _quote(code, price=10.0, pct=1.5):
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


class _BuyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "r19_buy.sqlite3")
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
            mock.patch.object(PT, "ET", None),
            mock.patch.dict(os.environ, {"PAPER_ENTRY_FREEZE": "0"}),
        ]
        for patch in self._patches:
            patch.start()
        PT._ENTRY_FREEZE_CACHE.update({"at": 0.0, "status": None})
        PT.init_db()
        PT.start_new_cycle(capital=CAPITAL, include_dashboard=False)

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _quotes(self, codes, asof_date=None):
        return {code: self.quotes[code] for code in codes if code in self.quotes}

    def cycle_id(self):
        with PT._db() as conn:
            return int(conn.execute(
                "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
                " ORDER BY id DESC LIMIT 1").fetchone()[0])

    def new_cycle(self, key):
        with PT._db(immediate=True) as conn:
            return int(conn.execute(
                "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,started_at,"
                "created_at,updated_at) VALUES(?,'running',?,'balanced',?,?,?)",
                (key, CAPITAL, f"{DAY.isoformat()} 00:00:00",
                 f"{DAY.isoformat()} 00:00:00", f"{DAY.isoformat()} 00:00:00")).lastrowid)

    def add_signal(self, *, code=CODE, account_id=ACCOUNT, intended_date=None,
                   signal_date=None, payload=None):
        intended_date = DAY.isoformat() if intended_date is None else intended_date
        signal_date = signal_date or intended_date
        body = payload if payload is not None else {
            "pick": {"code": code, "score": 0.8, "price": 10.0},
            "decision": {"entry_model": {"score": 90.0}},
        }
        with PT._db(immediate=True) as conn:
            cursor = conn.execute(
                "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
                "close_price,rank_score,t_tier,t_score,payload,status,created_at,"
                "strategy_id,strategy_version,strategy_checksum,cycle_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, signal_date, intended_date, code, f"测试股_{code}", 10.0,
                 0.9, "A", 0.9, PT._json(body), "pending", f"{signal_date} 15:00:00",
                 *PT._strategy_stamp(conn, account_id),
                 int(conn.execute(
                     "SELECT cycle_id FROM paper_accounts WHERE id=?", (account_id,)
                 ).fetchone()[0])),
            )
            return int(cursor.lastrowid)

    def run_buy(self, *, signal_id, code=CODE, asof_day=DAY):
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

    def counters(self):
        with PT._db() as conn:
            return {
                "fills": conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
                "lots": conn.execute(
                    "SELECT COUNT(*) FROM paper_position_lots").fetchone()[0],
                "risk": conn.execute(
                    "SELECT COUNT(*) FROM paper_risk_decisions").fetchone()[0],
                "audit_buy_filled": conn.execute(
                    "SELECT COUNT(*) FROM paper_audit WHERE event='buy_filled'"
                ).fetchone()[0],
                "cash": PT._shared_cash(conn),
            }


class FinalSizingUsesCyclePinnedVersion(_BuyCase):
    """EC-21 —— final BUY sizing 不得被后来的 risk/profile version 改写。"""

    def test_ec21_final_buy_sizing_uses_cycle_pinned_profile_and_spec(self):
        import strategy_registry as SR
        cycle = self.cycle_id()
        self.quotes[CODE] = _quote(CODE)
        signal_id = self.add_signal(signal_date=DAY_PREV.isoformat())
        with PT._db() as conn:
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT,)).fetchone())
            pinned_profile = PT._risk_profile(
                account, asof_day=DAY, conn=conn, cycle_id=cycle)
            pinned_spec = PT.SRE.effective_spec_for_cycle(
                conn, ACCOUNT, PT.ACCOUNT_SPECS.get(ACCOUNT) or {}, cycle_id=cycle)
            current = SR.get_version(ACCOUNT, conn=conn)
        with PT._db(immediate=True) as conn:
            SR.save_definition(
                conn, ACCOUNT,
                {"metadata": {"style": "momentum", "daily": True, "positions": 3}},
                expected_version=current.version, actor="r19-test",
                change_note="ec21 advance current head",
            )
        with PT._db() as conn:
            head_profile = PT.SRE.compiled_profile_for(conn, ACCOUNT)
            head_spec = PT.SRE.effective_spec(
                conn, ACCOUNT, PT.ACCOUNT_SPECS.get(ACCOUNT) or {})
        self.assertNotEqual(
            pinned_profile.get("max_exposure"), head_profile.get("max_exposure"),
            "fixture 的 v1/v2 max_exposure 相同，无法区分 provenance")
        self.assertNotEqual(
            pinned_spec.get("hard_stop"), head_spec.get("hard_stop"),
            "fixture 的 v1/v2 hard_stop 相同，无法区分 provenance")
        captured = {}

        def spy(*args, **kwargs):
            captured["hard_stop"] = args[6]
            captured["profile"] = args[7]
            return 0, {"qty": 0, "reason": "ec21_capture"}

        with mock.patch.object(PT, "_price_aware_qty", side_effect=spy):
            result, _order = self.run_buy(signal_id=signal_id)
        self.assertIn(
            "profile", captured,
            f"_buy_order 没有到达 final sizing（fixture 未覆盖目标路径）：{result}")
        self.assertEqual(
            pinned_profile.get("max_exposure"),
            captured["profile"].get("max_exposure"),
            "final sizing profile 吃到了后来 current head 的风险画像")
        self.assertEqual(
            pinned_spec.get("hard_stop"), captured["hard_stop"],
            "final sizing hard_stop 吃到了后来 current head 的 effective spec")


class NormalBuyConvergence(_BuyCase):
    """SB-1 / SB-2 / SB-6 / SB-7 / SB-8 / SB-9 / SB-16。"""

    def test_normal_buy_goes_through_the_planner_and_keeps_parity(self):
        self.quotes[CODE] = _quote(CODE)
        signal_id = self.add_signal(signal_date=DAY_PREV.isoformat())
        calls = []
        original = EP.commit_fill

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        with mock.patch.object(EP, "commit_fill", side_effect=spy):
            result, order = self.run_buy(signal_id=signal_id)

        # SB-1：普通策略 BUY 必须经过唯一 commit primitive。
        self.assertTrue(calls, "普通策略买入没有调用 execution_planner.commit_fill")
        self.assertTrue(result.get("filled"), f"正常买入未成交：{result}")
        with PT._db() as conn:
            order_row = dict(conn.execute(
                "SELECT * FROM paper_orders WHERE id=?", (order["id"],)).fetchone())
            reservation = dict(conn.execute(
                "SELECT * FROM paper_capital_reservations WHERE order_key=?",
                (str(order["id"]),)).fetchone())
            lots = [dict(row) for row in conn.execute(
                "SELECT * FROM paper_position_lots")]
        # SB-2：order / reservation / lot 必须是同一个周期。
        self.assertEqual(order_row["cycle_id"], reservation["cycle_id"],
                         "订单周期与预占周期不一致")
        self.assertTrue(lots, "成交后必须写入 lot")
        for lot in lots:
            self.assertEqual(order_row["cycle_id"], lot["cycle_id"],
                             "订单周期与 lot 周期不一致")
        # SB-6：预占被消费。
        self.assertEqual("consumed", reservation["status"],
                         "正常成交的预占没有被消费")
        # SB-16：执行验证盖章在 fill 之后。
        self.assertEqual(1, int(order_row["execution_verified"] or 0),
                         "策略成交没有被执行验证盖章")
        self.assertIsNotNone(order_row["execution_status"])
        # SB-7/8/9：事件恰好一次。
        counts = self.counters()
        self.assertEqual(1, counts["fills"], "成交流水不是恰好一条")
        self.assertEqual(1, counts["lots"], "lot 不是恰好一条")
        self.assertEqual(1, counts["risk"], "risk 事件不是恰好一条")
        self.assertEqual(1, counts["audit_buy_filled"], "buy_filled audit 不是恰好一条")

    def test_second_buy_is_capped_by_the_shared_pool_not_broken(self):
        """第二笔受**共享池上限**约束（超限进等待池），不是被"修死"。

        非空门禁：第一笔必须成交，且第二笔的等待原因必须来自部署额度不足
        （``capital_deployment.allowed=False``），而不是任何硬拒绝。
        """
        self.quotes[CODE] = _quote(CODE)
        first = self.add_signal(signal_date=DAY_PREV.isoformat())
        result_a, _order_a = self.run_buy(signal_id=first)
        self.assertTrue(result_a.get("filled"), f"首笔未成交：{result_a}")
        second = self.add_signal(code="600911", signal_date=DAY_PREV.isoformat())
        self.quotes["600911"] = _quote("600911")
        result_b, order_b = self.run_buy(signal_id=second, code="600911")
        self.assertFalse(result_b.get("filled"), f"超出共享池上限仍成交：{result_b}")
        self.assertTrue(result_b.get("deferred"),
                        f"应为软性等待而非终态拒绝：{result_b}")
        payload = PT._loads(order_b["risk_payload"], {})
        deployment = (payload.get("sizing") or {}).get("capital_deployment") or {}
        self.assertFalse(deployment.get("allowed"),
                         "第二笔不是因部署额度不足而等待（等待原因不属于资金约束）")
        # §56：成功路径的 risk 事件由 commit_fill 写一次；被闸门挡下的决策
        # 各写一次。两笔合计恰好 2 条，既不多也不少。
        self.assertEqual(2, self.counters()["risk"],
                         "risk 事件数量不符：成功成交重复记录或等待决策漏记")


class CommitFailureRollsBack(_BuyCase):
    """SB-10 / SB-11 / SB-12 —— 提交失败必须整体回滚。"""

    def test_planner_failure_leaves_no_side_effects(self):
        self.quotes[CODE] = _quote(CODE)
        signal_id = self.add_signal(signal_date=DAY_PREV.isoformat())
        # 非空门禁：同一 fixture 无 sentinel 时必须能成交。
        with mock.patch.object(EP, "commit_fill", side_effect=RuntimeError("R19_SENTINEL")):
            result, order = self.run_buy(signal_id=signal_id)
        counts = self.counters()
        self.assertFalse(result.get("filled"), "commit_fill 失败却报告成交")
        # SB-12 / SB-11：不留 fill / lot。
        self.assertEqual(0, counts["fills"], "提交失败留下了成交流水")
        self.assertEqual(0, counts["lots"], "提交失败留下了 lot")
        # SB-10：现金没有被扣（共享现金仍等于初始资金）。
        self.assertAlmostEqual(CAPITAL, counts["cash"], places=2,
                               msg="提交失败却扣除了现金")
        with PT._db() as conn:
            reservation = conn.execute(
                "SELECT status FROM paper_capital_reservations WHERE order_key=?",
                (str(order["id"]),)).fetchone()
        if reservation is not None:
            self.assertNotEqual("consumed", reservation["status"],
                                "提交失败却消费了预占")
        self.assertIn(result.get("status"), {PT.STRATEGY_EXECUTION_RETRY_STATUS},
                      f"失败未降级为可重试态：{result}")


class WrongCycleReservationFailsClosed(_BuyCase):
    """SB-3 / SB-4 / SB-5 —— 错周期预占必须 fail closed 且保持原样。"""

    def _prepare(self):
        older = self.cycle_id()
        newer = self.new_cycle(f"r19-sb3-{older}")
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (older,))
            conn.execute("UPDATE paper_accounts SET cycle_id=?, status='running'",
                         (newer,))
        with PT._db() as conn:
            seq = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='paper_orders'").fetchone()
            next_order_id = int(seq["seq"] if seq is not None else 0) + 1
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_capital_reservations(cycle_id,order_key,account_id,code,"
                "side,amount,fees,status,created_at) VALUES(?,?,?,?,'buy',4321.0,7.0,"
                "'reserved',?)",
                (older, str(next_order_id), ACCOUNT, CODE, f"{DAY.isoformat()} 09:00:00"))
        return older, newer, next_order_id

    def test_wrong_cycle_reservation_is_rejected_and_untouched(self):
        older, newer, next_order_id = self._prepare()
        self.quotes[CODE] = _quote(CODE)
        # 非空门禁：预占存在、reserved、周期不同于将建的订单周期。
        with PT._db() as conn:
            before = dict(conn.execute(
                "SELECT * FROM paper_capital_reservations WHERE order_key=?",
                (str(next_order_id),)).fetchone())
        self.assertEqual("reserved", before["status"])
        self.assertEqual(older, int(before["cycle_id"]))
        self.assertNotEqual(older, newer)

        signal_id = self.add_signal(signal_date=DAY_PREV.isoformat())
        result, order = self.run_buy(signal_id=signal_id)

        # SB-3：必须 fail closed（不成交）。
        self.assertFalse(result.get("filled"), f"错周期预占被消费：{result}")
        self.assertTrue(result.get("reservation_cycle_mismatch"),
                        f"未标记为预占归属冲突：{result}")
        with PT._db() as conn:
            after = dict(conn.execute(
                "SELECT * FROM paper_capital_reservations WHERE order_key=?",
                (str(next_order_id),)).fetchone())
            counts = self.counters()
            order_row = dict(conn.execute(
                "SELECT * FROM paper_orders WHERE id=?", (order["id"],)).fetchone())
            signal_row = dict(conn.execute(
                "SELECT * FROM paper_signals WHERE id=?", (signal_id,)).fetchone())
        # SB-4：冲突预占保持原样 —— 周期、金额、状态都不许动。
        self.assertEqual(before, after,
                         "冲突的预占被改写或释放（那不属于本订单）")
        # 反证：没有 cash / lot / fill。
        self.assertEqual(0, counts["fills"], "错周期冲突却写入了成交流水")
        self.assertEqual(0, counts["lots"], "错周期冲突却写入了 lot")
        self.assertAlmostEqual(CAPITAL, counts["cash"], places=2,
                               msg="错周期冲突却扣除了现金")
        # SB-5：当前订单终态化，信号留在复试管道等待**新的** order identity。
        self.assertEqual("risk_rejected", order_row["status"],
                         "冲突的当前订单没有终态化")
        self.assertEqual("deferred_capacity", signal_row["status"],
                         "冲突后候选没有留在复试管道等待新委托")
        self.assertEqual(newer, int(order_row["cycle_id"]),
                         "订单周期被改写（周期归属不可变）")


class SliceSemanticsPreserved(_BuyCase):
    """SB-14 / SB-15 —— 分批建仓的切片账面语义。"""

    def _sliced_signal(self, plan, filled):
        payload = {
            "pick": {"code": CODE, "score": 0.8, "price": 10.0},
            "decision": {"entry_model": {"score": 90.0}},
            "entry_slices": {"plan": list(plan), "filled": filled,
                             "day": DAY.isoformat(), "slices": len(plan)},
        }
        return self.add_signal(signal_date=DAY_PREV.isoformat(), payload=payload)

    @staticmethod
    def _plan():
        # 10000 股总量切成 3 片（首片 = 计划首段，便于断言"中间片仍 deferred"）。
        return [3300, 3300, 3400]

    def test_sb14_intermediate_slice_stays_deferred(self):
        self.quotes[CODE] = _quote(CODE)
        signal_id = self._sliced_signal(self._plan(), 0)
        result, _order = self.run_buy(signal_id=signal_id)
        self.assertTrue(result.get("filled"), f"首片未成交：{result}")
        with PT._db() as conn:
            signal = dict(conn.execute(
                "SELECT * FROM paper_signals WHERE id=?", (signal_id,)).fetchone())
        self.assertEqual(
            "deferred_capacity", signal["status"],
            "中间片成交后信号被标成终态：剩余片会被永久丢失")
        slices = PT._loads(signal["payload"], {}).get("entry_slices") or {}
        self.assertEqual(1, int(slices.get("filled") or 0), "切片计数没有推进")

    def test_sb15_final_slice_marks_signal_filled(self):
        self.quotes[CODE] = _quote(CODE)
        signal_id = self._sliced_signal(self._plan(), len(self._plan()) - 1)
        result, _order = self.run_buy(signal_id=signal_id)
        self.assertTrue(result.get("filled"), f"最后一片未成交：{result}")
        with PT._db() as conn:
            signal = dict(conn.execute(
                "SELECT * FROM paper_signals WHERE id=?", (signal_id,)).fetchone())
        self.assertEqual("filled", signal["status"],
                         "最后一片成交后信号没有标记 filled")


class SlotBorrowRollsBackOnFailure(unittest.TestCase):
    """SB-13 —— slot borrow 的失败回滚由统一失败入口负责（源码级契约）。

    为什么是源码级：借位只会在"策略席位已满且存在 donor 余量"的 crowding
    fixture 下发生，而提交失败的注入点在其之后；这条契约的价值是防止
    「失败路径整段被删/被绕过」。实际行为由 Guard 10k 与既有 RPL-P5 族覆盖。
    """

    def test_sb13_failure_path_rolls_back_the_borrow(self):
        path = os.path.join(BACKEND, "manual_orders.py")
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        tree = ast.parse(raw)
        node = next(
            item for item in ast.walk(tree)
            if isinstance(item, ast.FunctionDef) and item.name == "strategy_fill_failure"
        )
        body = "".join(
            "\n".join(raw.splitlines()[node.lineno - 1:node.end_lineno]).split())
        self.assertIn("_rollback_slot_borrow(", body,
                      "成交失败不再回滚本次 order 借出的席位")
        self.assertIn("ifborrow.get(\"allowed\"):", body,
                      "slot borrow 回滚不再受 allowed 守卫（会对未借位的单误回滚）")


if __name__ == "__main__":
    unittest.main()
