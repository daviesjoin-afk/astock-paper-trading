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
    EC-20 dynamic position limits 的 (cycle, as-of, pinned version)

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


class CompiledProfileIsAsOfBound(_CapitalCase):
    """EC-12 —— 编译风险画像不得由回放日之后创建的策略版本改写。"""

    def test_ec12_future_strategy_version_does_not_change_history(self):
        import strategy_registry as SR
        self.quotes[CODE] = _quote(CODE)
        # 非空门禁：当前（live）口径下编译画像**确实**被融合（否则断言无意义）。
        with PT._db() as conn:
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT,)).fetchone())
            live = PT._risk_profile(account, conn=conn)
        self.assertIn("compiled_risk_profile", live,
                      "live 口径下没有融合编译画像（门禁会是空转）")
        # 历史 as-of 早于策略版本创建日 ⇒ 该版本的帽不可证明当时已生效。
        with PT._db() as conn:
            version = SR.get_version(ACCOUNT, conn=conn)
        if version is None:
            self.skipTest("该账户没有注册表版本行")
        created = str(version.created_at)[:10]
        earlier = (dt.date.fromisoformat(created) - dt.timedelta(days=30)).isoformat()
        with PT._db() as conn:
            historical = PT._risk_profile(account, asof_day=earlier, conn=conn)
        self.assertNotIn(
            "compiled_risk_profile", historical,
            f"回放日 {earlier} 早于策略版本创建日 {created}，未来版本的编译帽被融进历史")


class ExplicitEmptyCycleHasNoCapital(_CapitalCase):
    """EC-13 —— 显式 idle 周期（enabled_strategies == []）不得凭空产生预算。"""

    def test_ec13_idle_cycle_does_not_fall_back_to_the_caller_account(self):
        idle = self.new_cycle(f"r19-ec13-{self.cycle_id()}")
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET enabled_strategies=? WHERE id=?",
                         ("[]", idle))
        with PT._db() as conn:
            rows = PT._shared_account_rows(conn, idle)
        self.assertEqual([], list(rows), "fixture 的 idle 周期本应没有参与者（空门禁）")
        self.quotes[CODE] = _quote(CODE)
        with PT._db() as conn:
            inputs = PT._pool_allocation_inputs(
                conn, {"id": ACCOUNT}, 100_000.0, [], dict(self.quotes),
                dict(MARKET), cycle_id=idle, asof_day=DAY,
            )
        self.assertEqual(
            [], list(inputs["rows"]),
            "显式 idle 周期把调用方账户注入成参与者：零策略周期凭空有了资金表达")
        self.assertEqual({}, inputs["weights"], "idle 周期产生了策略权重")


class DynamicPositionLimitsAreCycleAsOfBound(_CapitalCase):
    """EC-20 —— seat budget 也必须消费同一个 (cycle, as-of, pinned version)。"""

    def test_ec20_dynamic_position_limits_are_cycle_asof_deterministic(self):
        import strategy_registry as SR
        cycle = self.cycle_id()
        with PT._db() as conn:
            before = PT._dynamic_position_limits(conn, cycle_id=cycle, asof_day=DAY)
            current = SR.get_version(ACCOUNT, conn=conn)
        self.assertIn(ACCOUNT, before["weights"], "fixture 没有生成 seat-budget 权重")
        # 清掉 baseline 缓存行，确保 after 调用真正走 runtime 组装路径。
        with PT._db(immediate=True) as conn:
            conn.execute(
                "DELETE FROM paper_position_limit_versions WHERE cycle_id=?",
                (cycle,))
        # 未来 adaptive risk：历史 as-of 回放不得看见它。
        self.set_account_params(
            ACCOUNT, adaptive_risk={"max_exposure": 0.30},
            adaptive_risk_meta={
                "status": "active", "effective_date": DAY_NEXT.isoformat(),
            })
        # current head 前进到不同风险模板；cycle pin 仍应停在 v1。
        with PT._db(immediate=True) as conn:
            SR.save_definition(
                conn, ACCOUNT,
                {"metadata": {"style": "trend", "hold": 8, "daily": True, "positions": 3}},
                expected_version=current.version, actor="r19-test",
                change_note="ec20 advance current head",
            )
        captured = {}
        original_runtimes = PT._strategy_runtimes

        def spy(*args, **kwargs):
            captured["profiles"] = kwargs.get("profiles")
            captured["cycle_id"] = kwargs.get("cycle_id")
            return original_runtimes(*args, **kwargs)

        with PT._db() as conn:
            with mock.patch.object(PT, "_strategy_runtimes", side_effect=spy):
                after = PT._dynamic_position_limits(conn, cycle_id=cycle, asof_day=DAY)
            head_runtime = PT.SRT.get_context(conn, ACCOUNT).allocation_runtime
        self.assertNotEqual(
            before["weights"].get(ACCOUNT), 0.30,
            "fixture 的未来 adaptive risk 没有形成可区分状态")
        self.assertNotEqual(
            before["weights"].get(ACCOUNT), head_runtime.own_exposure_cap_pct,
            "fixture 的 current head 与 pinned runtime 帽相同，无法区分 provenance")
        self.assertEqual(
            cycle, captured.get("cycle_id"),
            "seat-budget runtime 没有收到 explicit cycle")
        self.assertIn(
            ACCOUNT, captured.get("profiles") or {},
            "seat-budget runtime 没有收到 pinned profiles")
        self.assertEqual(
            before["pool_limit"], after["pool_limit"],
            "seat-budget pool_limit 被未来 adaptive risk / current head 改写")
        self.assertEqual(
            before["limits"], after["limits"],
            "seat-budget limits 被未来 adaptive risk / current head 改写")
        self.assertEqual(
            before["weights"], after["weights"],
            "seat-budget weights 被未来 adaptive risk / current head 改写")


    def test_ec20b_explicit_idle_cycle_does_not_reinject_builtins(self):
        """EC-20b —— explicit idle cycle 的 seat budget 也必须保持空。"""
        idle = self.new_cycle(f"r19-ec20b-{self.cycle_id()}")
        with PT._db(immediate=True) as conn:
            conn.execute(
                "UPDATE paper_cycles SET enabled_strategies=? WHERE id=?",
                ("[]", idle))
        with PT._db() as conn:
            result = PT._dynamic_position_limits(
                conn, cycle_id=idle, asof_day=DAY)
        self.assertEqual({}, result["limits"], "idle cycle 注入了 builtin 席位")
        self.assertEqual({}, result["weights"], "idle cycle 注入了 builtin 权重")
        self.assertEqual(0, result["pool_limit"], "idle cycle 产生了非零 pool_limit")


class CyclePinnedStrategyVersionIsUsed(_CapitalCase):
    """EC-15 / EC-16 —— capital budget 必须消费 cycle **冻结**的不可变版本。

    这是 R19 §25 的真正 contract：``paper_cycle_strategy_versions`` 在周期启动时
    就 pin 住了 ``strategy_id / strategy_version / strategy_checksum``，后来的编辑
    无法再给该周期的证据换标签。仅凭 ``current head.created_at <= asof`` 判断是
    **另一个** contract —— 它既可能让历史 cycle 吃到后来版本，也可能在 head 晚于
    asof 时整体丢掉收紧（那会让历史风险限制反而比真正 pinned 版本更宽松）。
    """

    USER = "r19_alpha"
    #: 两个**合法 DSL** 且产出不同 archetype / max_exposure 的版本定义。
    #: v1 → Trend / 0.85；v2 → Composite / 0.65。
    PINNED_RULE = {"op": "gt", "left": {"op": "field", "name": "close"},
                   "right": {"op": "indicator", "name": "ma", "window": 20}}
    PINNED_CONFIG = {"style": "trend", "hold": 8, "positions": 3,
                     "daily": True, "close": True}
    HEAD_RULE = {"op": "gt", "left": {"op": "field", "name": "close"},
                 "right": {"op": "const", "value": 1}}
    HEAD_CONFIG = {"style": "quality", "daily": True, "close": True, "hold": 20,
                   "positions": 8, "stop": True, "atr": True}

    def _seed_pinned_cycle(self, *, cycle_id):
        """建 v1（Trend, max_exposure=0.85）并把该账户 pin 到 cycle_id。"""
        import strategy_registry as SR
        with PT._db(immediate=True) as conn:
            SR.ensure_schema(conn)
            SR.create_user_definition(
                conn, self.USER, "R19 trend", dsl_ast=dict(self.PINNED_RULE),
                metadata=dict(self.PINNED_CONFIG), actor="r19-test")
        with PT._db(immediate=True) as conn:
            # 账户需要存在才能被 pin；创建用户名下的账户行。
            conn.execute(
                "INSERT OR IGNORE INTO paper_accounts(id,name,source_strategy,status,"
                "initial_cash,cash,cycle_days,max_positions,max_weight,max_exposure,"
                "version,created_at,updated_at,cycle_id,risk_profile) "
                "VALUES(?,?,'strategy_dsl','running',0,0,8,3,0.32,0.9,'v0',?,?,?, 'trend')",
                (self.USER, self.USER, f"{DAY.isoformat()} 00:00:00",
                 f"{DAY.isoformat()} 00:00:00", int(cycle_id)),
            )
            SR.bind_cycle_versions(conn, int(cycle_id), [self.USER])
        with PT._db() as conn:
            stamp = SR.stamp_for_account(conn, self.USER, cycle_id=int(cycle_id))
        self.assertIsNotNone(stamp[1], "fixture 的 pin 没有建立")
        return int(stamp[1])

    def _advance_head(self, *, expected_version):
        """把 current head 推到 v2（Composite, max_exposure=0.65）。"""
        import strategy_registry as SR
        with PT._db(immediate=True) as conn:
            SR.save_definition(
                conn, self.USER,
                {"dsl_ast": dict(self.HEAD_RULE), "metadata": dict(self.HEAD_CONFIG)},
                expected_version=expected_version, actor="r19-test",
                change_note="r19 advance head",
            )

    def test_ec15_cycle_pinned_version_beats_a_later_current_head(self):
        """cycle pin v1；之后 head 前进到 v2（created_at <= asof）仍不得改写该 cycle。"""
        import strategy_registry as SR
        cycle = self.cycle_id()
        pinned_version = self._seed_pinned_cycle(cycle_id=cycle)
        with PT._db() as conn:
            pinned_profile = PT.SRE.compiled_profile_for_cycle(
                conn, self.USER, cycle_id=cycle)
        self._advance_head(expected_version=pinned_version)
        with PT._db() as conn:
            head = SR.get_version(self.USER, conn=conn)
            head_profile = PT.SRE.compiled_profile_for(conn, self.USER)
            resolved = PT.SRE.compiled_profile_for_cycle(conn, self.USER, cycle_id=cycle)
            pinned_after = int(SR.stamp_for_account(conn, self.USER, cycle_id=cycle)[1])
            head_created = str(head.created_at)[:10]
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (self.USER,)).fetchone())
            # asof **不早于** head 创建日 —— 旧逻辑（current head + created_at <= asof）
            # 会认为 v2 可证明，从而让 cycle 的资本预算吃到 v2 的帽。
            production = PT._risk_profile(
                account, asof_day=head_created, conn=conn, cycle_id=cycle)
        self.assertNotEqual(
            int(head.version), pinned_version,
            "fixture 的 current head 没有前进（空门禁）")
        # 非空门禁：两版本画像**确实**不同，否则本测试无法区分。
        self.assertNotEqual(
            pinned_profile.get("max_exposure"), head_profile.get("max_exposure"),
            f"两版本 max_exposure 相同，无法区分：{pinned_profile} vs {head_profile}")
        # 核心断言：cycle 口径必须停在 pinned v1，而不是 current head v2。
        self.assertEqual(
            pinned_version, pinned_after, "cycle pin 被 head 前进改写了")
        self.assertEqual(
            pinned_profile.get("max_exposure"), resolved.get("max_exposure"),
            "cycle capital planning 使用了 current head 而不是 cycle-pinned 版本")
        self.assertNotEqual(
            head_profile.get("max_exposure"), resolved.get("max_exposure"),
            "cycle 口径吃到了 current head 的帽")
        # 生产路径（``_risk_profile`` 的 explicit cycle 分支）同样必须消费 pinned 版本。
        audit = production.get("compiled_risk_profile") or {}
        self.assertEqual(
            pinned_profile.get("template"), audit.get("template"),
            f"生产资金路径用了 current head 的编译画像：{audit}")
        self.assertNotEqual(
            head_profile.get("template"), audit.get("template"),
            "生产资金路径吃到了 current head 的模板")
        self.assertEqual(
            round(float(pinned_profile.get("max_exposure") or 0), 4),
            round(float(production.get("max_exposure") or 0), 4),
            "生产资金路径的 max_exposure 不是 cycle-pinned 版本的帽")

    def test_ec16_late_head_does_not_drop_the_pinned_profile(self):
        """head 晚于 asof 也必须继续用 pinned 版本，不得整体跳过收紧。"""
        cycle = self.cycle_id()
        self._seed_pinned_cycle(cycle_id=cycle)
        with PT._db() as conn:
            pinned = PT.SRE.compiled_profile_for_cycle(conn, self.USER, cycle_id=cycle)
        # 非空门禁：pin 住的版本不是 Composite 兜底（否则无法区分"用了 pinned"
        # 与"fail closed 到 Composite"）。
        self.assertNotEqual(
            pinned.get("template"), PT.SRE.composite_compiled_profile().get("template"),
            "pin 住的版本解析成了 Composite：fixture 无法区分 pinned 与兜底")
        # asof 远早于版本创建日 —— 旧逻辑（created_at <= asof）会整体跳过收紧。
        with PT._db() as conn:
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (self.USER,)).fetchone())
            profile = PT._risk_profile(
                account, asof_day="2020-01-01", conn=conn, cycle_id=cycle)
        self.assertIn(
            "compiled_risk_profile", profile,
            "asof 早于版本创建时整体跳过了 compiled profile：历史风险限制被放宽")
        self.assertEqual(
            profile["compiled_risk_profile"].get("template"), pinned.get("template"),
            "asof 早于版本创建时没有继续使用 cycle-pinned 版本")
        self.assertEqual(
            round(float(profile.get("max_exposure") or 0), 4),
            round(float(pinned.get("max_exposure") or 0), 4),
            "cycle-pinned 的 max_exposure 没有生效")

    def test_ec17_missing_cycle_pin_never_falls_back_to_current_head(self):
        """EC-17 —— explicit cycle 没有 pin 时必须 Composite，不得回退 current head。"""
        import strategy_registry as SR
        cycle = self.cycle_id()
        with PT._db(immediate=True) as conn:
            SR.ensure_schema(conn)
            SR.create_user_definition(
                conn, self.USER, "R19 unpinned", dsl_ast=dict(self.PINNED_RULE),
                metadata=dict(self.PINNED_CONFIG), actor="r19-test")
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO paper_accounts(id,name,source_strategy,status,"
                "initial_cash,cash,cycle_days,max_positions,max_weight,max_exposure,"
                "version,created_at,updated_at,cycle_id,risk_profile) "
                "VALUES(?,?,'strategy_dsl','running',0,0,8,3,0.32,0.9,'v0',?,?,?, 'trend')",
                (self.USER, self.USER, f"{DAY.isoformat()} 00:00:00",
                 f"{DAY.isoformat()} 00:00:00", int(cycle)),
            )
        with PT._db() as conn:
            pin = conn.execute(
                "SELECT 1 FROM paper_cycle_strategy_versions WHERE cycle_id=? AND account_id=?",
                (int(cycle), self.USER)).fetchone()
            head = PT.SRE.compiled_profile_for(conn, self.USER)
            resolved = PT.SRE.compiled_profile_for_cycle(conn, self.USER, cycle_id=cycle)
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (self.USER,)).fetchone())
            production = PT._risk_profile(
                account, asof_day=DAY.isoformat(), conn=conn, cycle_id=cycle)
        composite = PT.SRE.composite_compiled_profile()
        self.assertIsNone(pin, "fixture 不应存在 cycle pin")
        self.assertNotEqual(
            head.get("template"), composite.get("template"),
            "fixture 的 current head 与 Composite 无法区分")
        self.assertEqual(
            composite.get("template"), resolved.get("template"),
            "缺 cycle pin 时没有 fail closed 到 Composite，而是回退到了 current head")
        self.assertEqual(
            composite.get("max_exposure"), resolved.get("max_exposure"),
            "缺 cycle pin 时读到了 current head 的帽")
        audit = production.get("compiled_risk_profile") or {}
        self.assertEqual(
            composite.get("template"), audit.get("template"),
            "生产资金路径缺 pin 时没有 fail closed 到 Composite")

    def test_ec18_cluster_dsl_uses_cycle_pinned_version(self):
        """EC-18 —— cluster 的结构证据必须取 cycle pin 的 DSL，而不是 current head。"""
        import strategy_registry as SR
        cycle = self.cycle_id()
        pinned_version = self._seed_pinned_cycle(cycle_id=cycle)
        other = "r19_clone"
        with PT._db(immediate=True) as conn:
            SR.ensure_schema(conn)
            SR.create_user_definition(
                conn, other, "R19 clone", dsl_ast=dict(self.HEAD_RULE),
                metadata=dict(self.HEAD_CONFIG), actor="r19-test")
            SR.bind_cycle_versions(conn, int(cycle), [other])
        with PT._db() as conn:
            before = PT._strategy_cluster_profiles(
                conn, DAY, [self.USER, other], cycle_id=cycle)
        before_similarity = PT.SC.dsl_ast_similarity(
            before[self.USER].get("dsl_ast"), before[other].get("dsl_ast"))
        self.assertLess(
            before_similarity, PT.SC.DSL_CLONE_THRESHOLD,
            "fixture 的 v1 与对照策略不应是结构克隆")
        self._advance_head(expected_version=pinned_version)
        with PT._db() as conn:
            after = PT._strategy_cluster_profiles(
                conn, DAY, [self.USER, other], cycle_id=cycle)
            clusters, _factors = PT._strategy_cluster_factors(
                conn, DAY, [self.USER, other], cycle_id=cycle)
        after_similarity = PT.SC.dsl_ast_similarity(
            after[self.USER].get("dsl_ast"), after[other].get("dsl_ast"))
        self.assertLess(
            after_similarity, PT.SC.DSL_CLONE_THRESHOLD,
            "cycle 回放的 cluster DSL 吃到了后来 current head 的结构")
        self.assertNotEqual(
            PT.SC.cluster_of(self.USER, clusters), PT.SC.cluster_of(other, clusters),
            "cycle 回放因后来 current head 的 DSL 改变了簇预算")

    def test_ec19_runtime_cap_uses_cycle_pinned_version(self):
        """EC-19 —— allocation runtime 的版本派生字段必须取 cycle pin。"""
        cycle = self.cycle_id()
        pinned_version = self._seed_pinned_cycle(cycle_id=cycle)
        with PT._db() as conn:
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (self.USER,)).fetchone())
            before = PT._pool_allocation_inputs(
                conn, account, CAPITAL, [], {}, dict(MARKET), rows=[account],
                cycle_id=cycle, asof_day=DAY)
            pinned_runtime = next(
                item for item in before["runtimes"] if item.strategy_id == self.USER)
        self._advance_head(expected_version=pinned_version)
        with PT._db() as conn:
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (self.USER,)).fetchone())
            after = PT._pool_allocation_inputs(
                conn, account, CAPITAL, [], {}, dict(MARKET), rows=[account],
                cycle_id=cycle, asof_day=DAY)
            after_runtime = next(
                item for item in after["runtimes"] if item.strategy_id == self.USER)
            head_runtime = PT.SRT.get_context(conn, self.USER).allocation_runtime
        self.assertNotEqual(
            pinned_runtime.own_exposure_cap_pct, head_runtime.own_exposure_cap_pct,
            "fixture 的两个版本敞口帽相同，无法区分 provenance")
        self.assertEqual(
            pinned_runtime.own_exposure_cap_pct, after_runtime.own_exposure_cap_pct,
            "cycle 回放的 allocation runtime 敞口帽被后来 current head 改写")


class ForeignReservationIsNeverReleased(unittest.TestCase):
    """EC-14 —— 周期冲突的预占绝不被 release（含手动终态化路径）。"""

    def test_ec14_terminalizer_skips_release_for_a_foreign_reservation(self):
        path = os.path.join(BACKEND, "manual_orders.py")
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        tree = ast.parse(raw)
        node = next(
            item for item in ast.walk(tree)
            if isinstance(item, ast.FunctionDef)
            and item.name == "_terminalize_cycle_stale_order"
        )
        body = "".join(
            "\n".join(raw.splitlines()[node.lineno - 1:node.end_lineno]).split())
        self.assertIn(
            "_is_reservation_cycle_mismatch(exc,ReservationCycleMismatch)", body,
            "终态化路径没有识别预占周期冲突")
        self.assertIn(
            "ifnotforeign_reservation:", body,
            "终态化路径无条件释放预占：冲突的预占属于别的订单（§50）")
        guard_at = body.index("ifnotforeign_reservation:")
        release_at = body.index("_finish_capital_reservation(conn,order_id,")
        self.assertLess(guard_at, release_at,
                        "释放预占出现在冲突守卫之外（会释放别人的资产）")


if __name__ == "__main__":
    unittest.main()
