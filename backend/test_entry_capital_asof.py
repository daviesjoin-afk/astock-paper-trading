# -*- coding: utf-8 -*-
"""R19：入场资金预算的 (cycle, as-of) 证据边界契约。

本文件只钉**资金**侧的时点边界（席位侧由 R18 的
``test_replacement_asof_provenance`` 覆盖）：

    EC-1  future adaptive risk 不生效（D 看不到 D+1 的 overlay）
    EC-2  same-day adaptive risk 生效
    EC-3  future adaptive allocation 不生效
    EC-4  same-day adaptive allocation 生效
    EC-5  future cluster signal 不参与历史 as-of 的簇证据
    EC-6  as-of 内的 cluster signal 参与
    EC-7  participants 只来自显式周期的账本
    EC-8  allocation_plan 与 strategy_pool_budget 消费同一份有界事实
    EC-9  intraday buyback 的预算带 (cycle, as-of)
    EC-10 swing scale-in 的预算带 (cycle, as-of)

**概念区分**（规格 §29/§101）：participants / adaptive overlays / cluster 是
**有界证据**；仍在 ``reserved`` 的共享现金是**全局经济义务**，故意不受周期
约束 —— 按周期过滤会造成真实 double-spend。EC-11 钉住后者。
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

import paper_trading as PT  # noqa: E402
import universe as U  # noqa: E402

ACCOUNT = "tq_breakout"
OTHER = "sector_rotation"
CODE = "600901"
DAY = dt.date(2026, 9, 10)
DAY_NEXT = dt.date(2026, 9, 11)
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


class _CapitalCase(unittest.TestCase):
    """真实 ``init_db()`` 账本；不手写 orders/fills 结构。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "r19_capital.sqlite3")
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

    def set_account_params(self, account_id, **params):
        with PT._db(immediate=True) as conn:
            row = conn.execute("SELECT params FROM paper_accounts WHERE id=?",
                               (account_id,)).fetchone()
            current = PT._loads(row["params"] if row is not None else None, {}) or {}
            current.update(params)
            conn.execute("UPDATE paper_accounts SET params=? WHERE id=?",
                         (PT._json(current), account_id))

    def add_signal(self, *, account_id=ACCOUNT, code=CODE,
                   intended_date=None, status="pending"):
        intended_date = DAY.isoformat() if intended_date is None else intended_date
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
                "close_price,rank_score,t_tier,t_score,payload,status,created_at,"
                "strategy_id,strategy_version,strategy_checksum) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, intended_date, intended_date, code, f"测试股_{code}", 10.0,
                 0.8, "A", 0.9, "{}", status, f"{intended_date} 15:00:00",
                 *PT._strategy_stamp(conn, account_id)),
            )

    def add_lot(self, *, account_id, code, cycle_id, qty=100, cost=10.0):
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

    def budget(self, *, asof_day, cycle_id=None, account_id=ACCOUNT):
        quotes = dict(self.quotes)
        with PT._db() as conn:
            positions, _value, nav, _industries, _codes = PT._shared_account_exposure(
                conn, quotes, DAY)
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone())
        with PT._db() as conn:
            return PT._strategy_pool_budget(
                conn, account, nav, positions, quotes, market=dict(MARKET),
                cycle_id=self.cycle_id() if cycle_id is None else cycle_id,
                asof_day=asof_day,
            )

    def plan_row(self, *, asof_day, cycle_id=None, account_id=ACCOUNT):
        quotes = dict(self.quotes)
        with PT._db() as conn:
            positions, _value, nav, _industries, _codes = PT._shared_account_exposure(
                conn, quotes, DAY)
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone())
        with PT._db() as conn:
            plan = PT._allocation_plan(
                conn, nav=nav, positions=positions, quotes=quotes, market=dict(MARKET),
                account=account,
                cycle_id=self.cycle_id() if cycle_id is None else cycle_id,
                asof_day=asof_day,
            )
        return (plan.get("rows_by_strategy") or {}).get(account_id) or {}


class AdaptiveRiskIsAsOfBound(_CapitalCase):
    """EC-1 / EC-2 —— adaptive risk overlay 的生效日边界。"""

    def test_ec1_future_adaptive_risk_is_ignored(self):
        self.quotes[CODE] = _quote(CODE)
        baseline = self.budget(asof_day=DAY)
        self.set_account_params(ACCOUNT, adaptive_risk={"max_exposure": 0.30},
                                adaptive_risk_meta={
                                    "status": "active",
                                    "effective_date": DAY_NEXT.isoformat(),
                                })
        after = self.budget(asof_day=DAY)
        self.assertEqual(
            baseline.get("target_pct"), after.get("target_pct"),
            "生效日更晚的 adaptive risk 改写了历史 as-of 的资金预算")
        self.assertEqual(
            baseline.get("absolute_cap_amount"), after.get("absolute_cap_amount"),
            "生效日更晚的 adaptive risk 改写了历史 as-of 的绝对上限")

    def test_ec2_same_day_adaptive_risk_applies(self):
        """不能修成"overlay 永远不生效"：同一天生效的 overlay 必须照常收敛。"""
        self.quotes[CODE] = _quote(CODE)
        baseline = self.budget(asof_day=DAY)
        self.set_account_params(ACCOUNT, adaptive_risk={"max_exposure": 0.30},
                                adaptive_risk_meta={
                                    "status": "active",
                                    "effective_date": DAY.isoformat(),
                                })
        with PT._db() as conn:
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT,)).fetchone())
            profile = PT._risk_profile(account, asof_day=DAY, conn=conn)
        self.assertLess(
            float(profile.get("max_exposure")), 0.95,
            f"同日生效的 adaptive risk 没有收敛风险画像：{profile.get('max_exposure')}")
        after = self.budget(asof_day=DAY)
        self.assertLess(
            after.get("target_pct"), baseline.get("target_pct"),
            "同日生效的 adaptive risk 没有改变资金预算（overlay 被修死）")


class AdaptiveAllocationIsAsOfBound(_CapitalCase):
    """EC-3 / EC-4 —— adaptive allocation 权重的生效日边界。"""

    def test_ec3_future_adaptive_allocation_is_ignored(self):
        self.quotes[CODE] = _quote(CODE)
        baseline = self.budget(asof_day=DAY)
        self.set_account_params(ACCOUNT, adaptive_allocation={
            "status": "active", "effective_date": DAY_NEXT.isoformat(), "weight_pct": 90.0,
        })
        after = self.budget(asof_day=DAY)
        self.assertEqual(
            baseline.get("target_pct"), after.get("target_pct"),
            "生效日更晚的 adaptive allocation 改写了历史 as-of 的 strategy weight")

    def test_ec4_same_day_adaptive_allocation_applies(self):
        self.quotes[CODE] = _quote(CODE)
        baseline = self.budget(asof_day=DAY)
        self.set_account_params(ACCOUNT, adaptive_allocation={
            "status": "active", "effective_date": DAY.isoformat(), "weight_pct": 90.0,
        })
        with PT._db() as conn:
            rows = PT._shared_account_rows(conn, self.cycle_id())
            profiles = {row["id"]: PT._risk_profile(row, asof_day=DAY, conn=conn)
                        for row in rows if row.get("id")}
            weights = PT._strategy_pool_weights(conn, rows, profiles, asof_day=DAY)
        self.assertEqual(weights[ACCOUNT], 0.9, "同日生效的权重没有被采用")
        after = self.budget(asof_day=DAY)
        self.assertNotEqual(
            baseline.get("target_pct"), after.get("target_pct"),
            "同日生效的 adaptive allocation 没有改变资金预算（overlay 被修死）")


class ClusterEvidenceIsAsOfBound(_CapitalCase):
    """EC-5 / EC-6 —— 相关簇证据的 as-of 边界。"""

    def _two_strategy_lots(self):
        cycle = self.cycle_id()
        for code, account_id in (("600001", ACCOUNT), ("600002", ACCOUNT),
                                 ("600003", ACCOUNT), ("600001", OTHER),
                                 ("600002", OTHER), ("600004", OTHER)):
            self.add_lot(account_id=account_id, code=code, cycle_id=cycle)

    def _cluster(self, asof_day):
        with PT._db() as conn:
            return PT._strategy_cluster_factors(
                conn, asof_day,
                account_ids=[ACCOUNT, OTHER], cycle_id=self.cycle_id())

    def test_ec5_future_cluster_signal_is_ignored(self):
        self._two_strategy_lots()
        self.quotes[CODE] = _quote(CODE)
        baseline = self.budget(asof_day=DAY)
        clusters_before, _factors = self._cluster(DAY)
        self.assertEqual(len(clusters_before), 2, "fixture 的两策略本应各自成簇")
        future_day = (dt.date.today() - dt.timedelta(days=5)).isoformat()
        for index in range(4):
            for account_id in (ACCOUNT, OTHER):
                self.add_signal(account_id=account_id, code=f"6010{index:02d}",
                                intended_date=future_day)
        clusters_after, _factors = self._cluster(DAY)
        self.assertEqual(
            clusters_before, clusters_after,
            "只属于机器今天的 signal 重合证据改变了历史 as-of 的簇结构")
        self.assertEqual(
            baseline.get("target_pct"), self.budget(asof_day=DAY).get("target_pct"),
            "未来簇证据改变了历史 as-of 的资金预算")

    def test_ec6_asof_cluster_signal_is_applied(self):
        """不能修成"簇证据永远为空"：as-of 之内的 signal 必须照常参与归簇。"""
        self._two_strategy_lots()
        self.quotes[CODE] = _quote(CODE)
        from paper_trading import _date
        in_window = (_date(DAY) - dt.timedelta(days=3)).isoformat()
        for index in range(4):
            for account_id in (ACCOUNT, OTHER):
                self.add_signal(account_id=account_id, code=f"6011{index:02d}",
                                intended_date=in_window)
        clusters, _factors = self._cluster(DAY)
        self.assertEqual(
            len(clusters), 1,
            f"as-of 之内的 signal 重合证据没有参与归簇：{clusters}")


class ParticipantRowsFollowTheCycle(_CapitalCase):
    """EC-7 —— participants 只来自显式周期的账本。"""

    def test_ec7_participants_come_from_the_explicit_cycle_only(self):
        older = self.cycle_id()
        newer = self.new_cycle(f"r19-ec7-{older}")
        # 只有 older 周期挂接前两个账户；newer 周期挂接另外两个。
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_accounts SET cycle_id=? WHERE id IN (?,?)",
                         (older, ACCOUNT, OTHER))
            conn.execute("UPDATE paper_accounts SET cycle_id=? WHERE id NOT IN (?,?)",
                         (newer, ACCOUNT, OTHER))
        with PT._db() as conn:
            older_ids = {row["id"] for row in PT._shared_account_rows(conn, older)}
            newer_ids = {row["id"] for row in PT._shared_account_rows(conn, newer)}
        self.assertEqual(older_ids, {ACCOUNT, OTHER}, "older 周期的参与者不正确")
        self.assertNotIn(ACCOUNT, newer_ids, "fixture 的周期账本没有分叉")
        self.quotes[CODE] = _quote(CODE)
        # 显式 older 周期的预算必须只看到 older 的参与者。
        with PT._db() as conn:
            inputs = PT._pool_allocation_inputs(
                conn, {"id": ACCOUNT}, 100_000.0, [], dict(self.quotes),
                dict(MARKET), cycle_id=older, asof_day=DAY,
            )
        self.assertEqual(set(inputs["rows"] and [row["id"] for row in inputs["rows"]]),
                         {ACCOUNT, OTHER},
                         "资金预算的参与者不是显式周期的账本")


class PlanAndBudgetShareTheSameFacts(_CapitalCase):
    """EC-8 —— allocation_plan 与 strategy_pool_budget 消费同一份有界事实。"""

    def test_ec8_plan_and_budget_agree_under_the_same_asof(self):
        self.quotes[CODE] = _quote(CODE)
        budget = self.budget(asof_day=DAY)
        row = self.plan_row(asof_day=DAY)
        self.assertEqual(
            budget.get("absolute_cap_amount"), row.get("raw_allowance_amount"),
            "部署计划与策略预算在同一 (cycle, as-of) 下给出了不同的额度："
            "两者必须消费同一份装配输入（PR-26 唯一装配点）")

    def test_ec8b_plan_ignores_future_adaptive_allocation(self):
        self.quotes[CODE] = _quote(CODE)
        baseline = self.plan_row(asof_day=DAY)
        self.set_account_params(ACCOUNT, adaptive_allocation={
            "status": "active", "effective_date": DAY_NEXT.isoformat(), "weight_pct": 90.0,
        })
        after = self.plan_row(asof_day=DAY)
        self.assertEqual(
            baseline.get("deployable_amount"), after.get("deployable_amount"),
            "部署计划被生效日更晚的 adaptive allocation 改写（历史 as-of 不可复现）")


class ProductionBuyCallersCarryProvenance(_CapitalCase):
    """EC-9 / EC-10 —— 回补与加仓的预算同样带 (cycle, as-of)。"""

    @staticmethod
    def _call_window(name, call):
        path = os.path.join(BACKEND, "paper_trading.py")
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        tree = ast.parse(raw)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                body = "\n".join(raw.splitlines()[node.lineno - 1:node.end_lineno])
                flat = "".join(body.split())
                index = flat.index(call)
                return flat[index:index + 400]
        raise AssertionError(f"未找到函数 {name}")

    def test_ec9_intraday_buyback_budget_carries_cycle_and_asof(self):
        window = self._call_window("_intraday_buyback", "_strategy_pool_budget(")
        self.assertIn("cycle_id=", window, "回补预算漏传 cycle")
        self.assertIn("asof_day=asof_day", window, "回补预算漏传 as-of")

    def test_ec10_swing_scale_in_budget_carries_cycle_and_asof(self):
        window = self._call_window("_swing_scale_in", "_strategy_pool_budget(")
        self.assertIn("cycle_id=", window, "加仓预算漏传 cycle")
        self.assertIn("asof_day=asof_day", window, "加仓预算漏传 as-of")


class ReservedCashIsAGlobalObligation(_CapitalCase):
    """EC-11 —— 仍在 reserved 的共享现金是全局经济义务，不受周期约束。"""

    def test_ec11_pending_reservations_are_not_cycle_filtered(self):
        older = self.cycle_id()
        newer = self.new_cycle(f"r19-ec11-{older}")
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='closed' WHERE id=?", (older,))
            conn.execute("UPDATE paper_accounts SET cycle_id=?", (newer,))
            conn.execute(
                "INSERT INTO paper_capital_reservations(cycle_id,order_key,account_id,code,"
                "side,amount,fees,status,created_at) VALUES(?,?,?,?,'buy',5000.0,1.0,"
                "'reserved',?)",
                (older, "r19-ec11-order", ACCOUNT, CODE, f"{DAY.isoformat()} 09:00:00"))
        with PT._db() as conn:
            by_account, total = PT._pending_buy_reservations(conn)
            by_account_new, total_new = PT._pending_buy_reservations(conn, cycle_id=newer)
        self.assertEqual(5001.0, total, "旧周期的在途预占不再占用共享资金池")
        self.assertEqual(total, total_new,
                         "按周期过滤了在途预占：会造成真实 double-spend")
        self.assertEqual(by_account, by_account_new)


if __name__ == "__main__":
    unittest.main()
