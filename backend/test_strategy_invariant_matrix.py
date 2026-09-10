# -*- coding: utf-8 -*-
"""PR-54（二）：DSL 边界、生命周期矩阵、版本时间线、系统风控不可覆盖、退出优先。

与既有套件的分工（刻意不重复，只补矩阵化的那部分）：

- ``test_strategy_dsl.py`` 已覆盖"代码型节点/未知字段/资源上限/类型错误/顺序无关"；
  本文件把它做成**边界矩阵**：限内合法 vs 越界一格非法；
- ``test_strategy_registry_lifecycle.py`` 覆盖若干具体边；本文件用
  **Registry 自己的迁移图**跑满 6×6 全矩阵（合法必成功、非法必拒绝）；
- ``test_strategy_version_immutability.py`` 覆盖触发器与删除保护；本文件覆盖
  **版本时间线**：v1→v2→v3 下旧 checksum/definition 不漂移、已打戳记录仍解析回原版本；
- 风控门禁一律调用生产入口（``execution_planner`` / ``order_intent`` /
  ``runtime_settings`` / ``paper_trading_rules`` / ``paper_sizing``），并用
  **入参结构**证明用户 metadata/DSL 根本没有进入这些闸门的通道。
"""
from __future__ import annotations

import datetime as dt
import inspect
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import execution_planner as EPL
import order_intent as OI
import paper_allocation as PA
import paper_sizing as PSZ
import paper_trading as PT
import paper_trading_rules as PTR
import portfolio_coordinator as PCO
import runtime_settings as RSET
import strategy_dsl_schema as DSL
import strategy_registry as SR

RULE = {
    "op": "and",
    "args": [
        {"op": "gt", "left": {"op": "field", "name": "close"},
         "right": {"op": "indicator", "name": "ma", "window": 20}},
        {"op": "gt", "left": {"op": "field", "name": "volume"},
         "right": {"op": "mul", "left": {"op": "indicator", "name": "volume_mean", "window": 5},
                   "right": {"op": "const", "value": 1.2}}},
    ],
}
COMPARISON = RULE["args"][0]

# 生产 sizing 的标准调用形态（与既有测试同形，避免自造参数）。
SIZING_PROFILE = {"max_weight": 0.32, "max_exposure": 0.92, "single_risk": 0.012,
                  "max_industry": 0.42, "cooldown_days": 2, "min_cost_edge": 0.006}


def _size(cash, *, price=10.0):
    qty, sizing = PSZ.price_aware_qty(
        nav=100_000.0, cash=cash, position_value=0.0,
        industry_value=0.0, code_value=0.0,
        fill_price=price, hard_stop=0.05, profile=SIZING_PROFILE,
        exposure_cap=0.82, max_exposure_cap=0.82, exposure_scale=1.0,
        strategy_position_value=0.0, strategy_cap_amount=32_000.0,
        pool_cap_amount=82_000.0,
        pending_strategy_amount=0.0, pending_pool_amount=0.0,
        num=lambda value, default=None: value,
        single_position_max_amount=0.0,
    )
    return qty, sizing


class _RegistryFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-pr54-")
        cls.conn = sqlite3.connect(os.path.join(cls._tmp, "paper.sqlite3"), timeout=30)
        cls.conn.row_factory = sqlite3.Row
        SR.ensure_schema(cls.conn)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _create(self, strategy_id):
        SR.create_user_definition(self.conn, strategy_id, strategy_id.upper(),
                                  dsl_ast=RULE, actor="pr54")
        self.conn.commit()
        return strategy_id


class DslBoundaryMatrixTests(unittest.TestCase):
    """测试组 E：限内必须确定，越界/未知一律 fail closed，且绝不执行代码。"""

    @staticmethod
    def _nested_not(depth):
        node = COMPARISON
        for _ in range(depth):
            node = {"op": "not", "arg": node}
        return node

    @staticmethod
    def _wide_and(count):
        return {"op": "and", "args": [COMPARISON for _ in range(count)]}

    def test_depth_limit_boundary(self):
        self.assertIsInstance(DSL.normalize({"op": "not", "arg": COMPARISON}), dict)
        with self.assertRaises(DSL.StrategyDslValidationError):
            DSL.normalize(self._nested_not(DSL.MAX_AST_DEPTH + 1))

    def test_node_count_limit_boundary(self):
        self.assertIsInstance(DSL.normalize(self._wide_and(3)), dict)
        with self.assertRaises(DSL.StrategyDslValidationError):
            DSL.normalize(self._wide_and(DSL.MAX_AST_NODES + 5))

    def test_rolling_window_boundary(self):
        at_limit = {"op": "gt", "left": {"op": "indicator", "name": "ma",
                                         "window": DSL.MAX_ROLLING_WINDOW},
                    "right": {"op": "const", "value": 1}}
        self.assertIsInstance(DSL.normalize(at_limit), dict)
        over = {"op": "gt", "left": {"op": "indicator", "name": "ma",
                                     "window": DSL.MAX_ROLLING_WINDOW + 1},
                "right": {"op": "const", "value": 1}}
        with self.assertRaises(DSL.StrategyDslValidationError):
            DSL.normalize(over)

    def test_unknown_operator_function_field_indicator_are_rejected(self):
        cases = {
            "operator": {"op": "sql", "args": []},
            "function": {"op": "call", "name": "os.system", "args": []},
            "field": {"op": "gt", "left": {"op": "field", "name": "__class__"},
                      "right": {"op": "const", "value": 1}},
            "indicator": {"op": "gt", "left": {"op": "indicator", "name": "boll",
                                               "window": 20},
                          "right": {"op": "const", "value": 1}},
        }
        for label, ast in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(DSL.StrategyDslValidationError):
                    DSL.normalize(ast)

    def test_empty_expression_fails_closed(self):
        for empty in ({}, {"op": ""}, {"op": "and", "args": []}, None, []):
            with self.subTest(value=repr(empty)):
                # 归一化对空/畸形输入必须报错（不接受"空策略"这种隐式放行）。
                with self.assertRaises((DSL.StrategyDslValidationError, TypeError, ValueError)):
                    DSL.normalize(empty)

    def test_extreme_numbers_stay_json_safe(self):
        for value in (1e308, -1e308, 0.0, -0.0, 1e-9, 10 ** 30):
            with self.subTest(value=value):
                ast = {"op": "gt", "left": {"op": "field", "name": "close"},
                       "right": {"op": "const", "value": value}}
                try:
                    normalized = DSL.normalize(ast)
                except DSL.StrategyDslValidationError:
                    continue  # 拒绝也是 fail closed
                text = json.dumps(normalized, allow_nan=False)
                self.assertNotIn("NaN", text)
                self.assertNotIn("Infinity", text)
                self.assertTrue(math.isfinite(float(json.loads(text)["right"]["value"])))

    def test_canonical_serialization_is_deterministic(self):
        _first, _canonical_a, checksum_a = DSL.canonicalize(RULE)
        _second, _canonical_b, checksum_b = DSL.canonicalize(RULE)
        self.assertEqual(checksum_a, checksum_b)
        _third, _canonical_c, checksum_c = DSL.canonicalize({"args": RULE["args"], "op": "and"})
        self.assertEqual(checksum_a, checksum_c)

    def test_dsl_module_never_evaluates_code(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "strategy_dsl_schema.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("eval(", "exec(", "compile(", "__import__", "os.system",
                          "subprocess", "pickle"):
            self.assertNotIn(forbidden, source, forbidden)


class LifecycleMatrixTests(_RegistryFixture):
    """测试组 F：以 Registry 的迁移图为唯一来源，跑满 6×6 矩阵。"""

    @staticmethod
    def _graph():
        # 生产图是唯一权威：测试只读它，从不自己写一份生命周期表。
        return SR._TRANSITIONS

    @classmethod
    def _statuses(cls):
        return tuple(SR.LIFECYCLE_STATUSES)

    def _walk_to(self, strategy_id, target):
        """BFS 找路径，并**通过生产 transition** 走到目标状态。"""
        if target == "draft":
            return  # 新建即 draft
        graph = self._graph()
        queue = [("draft", [])]
        seen = {"draft"}
        while queue:
            node, path = queue.pop(0)
            for nxt in sorted(graph.get(node, ())):
                if nxt in seen:
                    continue
                trail = path + [nxt]
                if nxt == target:
                    for step in trail:
                        SR.transition(self.conn, strategy_id, step, actor="pr54")
                        self.conn.commit()
                    return
                seen.add(nxt)
                queue.append((nxt, trail))
        raise AssertionError(f"{target} 在 Registry 图里从 draft 不可达")

    def test_graph_is_well_formed(self):
        graph = self._graph()
        statuses = set(self._statuses())
        self.assertEqual(statuses, set(graph), "图与 LIFECYCLE_STATUSES 不一致")
        for source, targets in graph.items():
            with self.subTest(source=source):
                self.assertTrue(targets <= statuses, f"{source} 指向未知状态")
                self.assertNotIn(source, targets, "出现自环")
        self.assertEqual(set(), set(graph["archived"]), "archived 必须是终态")

    def test_every_legal_edge_is_accepted(self):
        for source, targets in sorted(self._graph().items()):
            for target in sorted(targets):
                with self.subTest(source=source, target=target):
                    strategy_id = self._create(f"m_{source}_{target}")
                    self._walk_to(strategy_id, source)
                    spec = SR.transition(self.conn, strategy_id, target,
                                         expected_status=source, actor="pr54")
                    self.conn.commit()
                    self.assertEqual(target, spec.status)
                    events = SR.lifecycle_events(self.conn, strategy_id)
                    self.assertEqual((source, target),
                                     (events[-1]["from_status"], events[-1]["to_status"]))

    def test_every_illegal_edge_is_rejected(self):
        graph = self._graph()
        for source in self._statuses():
            for target in self._statuses():
                if target == source or target in graph[source]:
                    continue
                with self.subTest(source=source, target=target):
                    strategy_id = self._create(f"x_{source}_{target}")
                    self._walk_to(strategy_id, source)
                    before = SR.get(strategy_id, conn=self.conn).status
                    with self.assertRaises(ValueError):
                        SR.transition(self.conn, strategy_id, target, actor="pr54")
                    self.conn.rollback()
                    self.assertEqual(before, SR.get(strategy_id, conn=self.conn).status)

    def test_supports_new_cycle_is_true_only_for_active(self):
        strategy_id = self._create("cycle_flag")
        self._walk_to(strategy_id, "active")
        self.assertTrue(SR.get(strategy_id, conn=self.conn).supports_new_cycle)
        SR.transition(self.conn, strategy_id, "paused", actor="pr54")
        self.conn.commit()
        self.assertFalse(SR.get(strategy_id, conn=self.conn).supports_new_cycle)


class ImmutableVersionTimelineTests(_RegistryFixture):
    """测试组 G：v1→v2→v3 时间线里，历史版本与已打戳记录都不漂移。"""

    def test_old_checksums_and_definitions_never_change(self):
        strategy_id = self._create("timeline")
        v1 = SR.get_version(strategy_id, conn=self.conn)
        v2 = SR.save_definition(self.conn, strategy_id, {"description": "v2"},
                                expected_version=1, actor="pr54", change_note="v2")
        self.conn.commit()
        v3 = SR.save_definition(self.conn, strategy_id, {"description": "v3"},
                                expected_version=v2.version, actor="pr54", change_note="v3")
        self.conn.commit()
        self.assertEqual([1, 2, 3], [v1.version, v2.version, v3.version])
        self.assertEqual(3, len({v1.checksum, v2.checksum, v3.checksum}))

        again_v1 = SR.get_version(strategy_id, v1.version, conn=self.conn)
        self.assertEqual(v1.checksum, again_v1.checksum)
        self.assertEqual(v1.definition, again_v1.definition)
        self.assertNotEqual(again_v1.definition, v3.definition)
        again_v2 = SR.get_version(strategy_id, v2.version, checksum=v2.checksum, conn=self.conn)
        self.assertIsNotNone(again_v2)
        self.assertEqual(v2.checksum, again_v2.checksum)

    def test_stamped_records_still_resolve_to_their_original_version(self):
        strategy_id = self._create("timeline_resolve")
        v1 = SR.get_version(strategy_id, conn=self.conn)
        stamp = {"strategy_id": strategy_id, "strategy_version": v1.version,
                 "strategy_checksum": v1.checksum}
        SR.save_definition(self.conn, strategy_id, {"description": "second"},
                           expected_version=v1.version, actor="pr54")
        self.conn.commit()
        # v2 出现后，v1 的记录仍解析回 v1（不漂移到"当前版本"）。
        resolved = SR.resolve_record_version(self.conn, stamp)
        self.assertEqual(v1.version, resolved.version)
        self.assertEqual(v1.checksum, resolved.checksum)

    def test_partial_or_unknown_stamp_fails_closed(self):
        strategy_id = self._create("timeline_partial")
        v1 = SR.get_version(strategy_id, conn=self.conn)
        with self.assertRaises(ValueError):
            SR.resolve_record_version(self.conn, {"strategy_id": strategy_id,
                                                  "strategy_version": v1.version})
        with self.assertRaises(ValueError):
            SR.resolve_record_version(self.conn, {"strategy_id": strategy_id,
                                                  "strategy_version": v1.version,
                                                  "strategy_checksum": "0" * 64})

    def test_archive_keeps_history_readable(self):
        strategy_id = self._create("timeline_archive")
        v1 = SR.get_version(strategy_id, conn=self.conn)
        SR.save_definition(self.conn, strategy_id, {"description": "second"},
                           expected_version=v1.version, actor="pr54")
        self.conn.commit()
        for status in ("validated", "archived"):
            SR.transition(self.conn, strategy_id, status, actor="pr54")
            self.conn.commit()
        versions = SR.list_versions(strategy_id, conn=self.conn)
        self.assertEqual([1, 2], [version.version for version in versions])
        self.assertEqual(v1.checksum, versions[0].checksum)
        self.assertEqual(v1.definition, versions[0].definition)
        events = SR.lifecycle_events(self.conn, strategy_id)
        self.assertEqual("archived", events[-1]["to_status"])


class SystemRiskNonOverrideTests(unittest.TestCase):
    """测试组 H：用户 metadata / DSL 没有通道覆盖系统硬边界。"""

    HOSTILE = {
        "t_plus_1": 0, "settlement": "t0", "lot_size": 1, "min_lot": 1,
        "allow_short": True, "short": True, "leverage": 10, "max_leverage": 10,
        "security_scope": "all", "allow_star": True, "allow_st": True,
        "quote_freshness_minutes": 10 ** 6, "stale_quote_ok": True,
        "shared_pool_max_exposure": 5.0, "pool_cap": 10 ** 9,
    }

    def test_gates_take_no_user_writable_parameter(self):
        # 结构证明：这些闸门的入参里没有 metadata/overrides/settings，
        # 用户写什么键都进不来。
        forbidden = {"metadata", "overrides", "strategy_metadata", "params", "settings"}
        for name in ("security_gate", "quote_gate", "cash_gate", "capacity_gate",
                     "seat_reserve_gate", "market_gate", "account_risk_gate"):
            with self.subTest(gate=name):
                params = set(inspect.signature(getattr(EPL, name)).parameters)
                self.assertFalse(params & forbidden, f"{name} 接受了用户可写参数")

    def test_t_plus_1_and_settlement_are_not_tunable(self):
        with self.assertRaises(ValueError):
            RSET.validate({"t_plus_1": 0})
        with self.assertRaises(ValueError):
            RSET.validate({"settlement": "t0"})

    def test_strategy_defaults_contain_no_system_risk_keys(self):
        for strategy_id, override in RSET.STRATEGY_DEFAULTS.items():
            with self.subTest(strategy=strategy_id):
                for hostile in ("lot_size", "leverage", "allow_short", "security_scope",
                                "quote_freshness_minutes", "shared_pool_max_exposure"):
                    self.assertNotIn(hostile, override)

    def test_security_scope_ignores_any_declared_scope(self):
        self.assertFalse(PTR.security_scope("688001", "科创板样例")["allowed"])
        self.assertFalse(PTR.security_scope("430001", "北交所样例")["allowed"])
        self.assertFalse(PTR.security_scope("600901", "ST样例", risk_flag=True)["allowed"])
        self.assertTrue(PTR.security_scope("600901", "普通主板")["allowed"])
        self.assertTrue(PTR.security_scope("300750", "创业板样例")["allowed"])

    def test_lot_size_is_never_fractional_whatever_the_input(self):
        for cash in (0.0, 1.0, 999.99, 1e9):
            with self.subTest(cash=cash):
                qty, _sizing = _size(cash)
                self.assertEqual(0, int(qty) % PT.LOT_SIZE)

    def test_no_leverage_cash_constraint_is_absolute(self):
        qty, _sizing = _size(0.0)
        self.assertEqual(0, int(qty))

    def test_no_short_selling_is_enforced_by_the_intent_contract(self):
        signal = {"code": "600901", "name": "样例", "price": 10.0, "score": 0.8,
                  "industry": "工程机械", "entry_model": "强势日内候选实时确认",
                  "stop_loss": 9.5, "candidate_status": "normal", "side": "short"}
        with self.assertRaises(OI.OrderIntentContractError):
            OI.order_intent_from_signal("tq_breakout", signal,
                                        now=dt.datetime(2026, 9, 8, 10, 30))

    def test_quote_verification_matrix_blocks_entry_but_allows_risk_exit(self):
        # 同一份"新鲜"行情，按核验等级 × 用途形成矩阵：
        # 买入必须双源核验通过，风控退出允许降级（否则池子一坏就卖不掉）。
        today = dt.date.today().isoformat()
        base = {"price": 10.0, "quote_source": "live",
                "quote_at": dt.datetime.now().isoformat(timespec="seconds")}
        matrix = {
            "cross_source_checked": {"entry": True, "exit": True},
            "cross_source_unavailable": {"entry": False, "exit": True},
            "range_timestamp_checked": {"entry": False, "exit": True},
            "incomplete": {"entry": False, "exit": False},
            None: {"entry": False, "exit": False},
        }
        for validation, expected in matrix.items():
            quote = dict(base)
            if validation is not None:
                quote["quote_validation"] = validation
            for purpose in ("entry", "exit"):
                with self.subTest(validation=validation, purpose=purpose):
                    self.assertEqual(
                        expected[purpose],
                        EPL.quote_gate(quote, today, purpose=purpose)["fresh"],
                    )

    def test_stale_and_cached_quotes_are_never_tradeable(self):
        today = dt.date.today().isoformat()
        stale = {"price": 10.0, "quote_at": "2026-01-05T09:30:00",
                 "quote_source": "live", "quote_validation": "cross_source_checked"}
        cached = {"price": 10.0, "quote_source": "local_cache",
                  "quote_at": dt.datetime.now().isoformat(timespec="seconds"),
                  "quote_validation": "cross_source_checked"}
        for name, quote in (("stale", stale), ("cached", cached)):
            for purpose in ("entry", "exit"):
                with self.subTest(quote=name, purpose=purpose):
                    self.assertFalse(EPL.quote_gate(quote, today, purpose=purpose)["fresh"])

    def test_shared_pool_cap_is_a_system_constant(self):
        self.assertGreater(PT.SHARED_POOL_MAX_EXPOSURE, 0.0)
        self.assertLess(PT.SHARED_POOL_MAX_EXPOSURE, 1.0)
        self.assertGreaterEqual(PT.SHARED_POOL_MAX_POSITIONS, 1)
        # 池余量只由"系统上限 × 净值"决定，函数没有接受用户上限的通道。
        headroom = PA.pool_headroom(
            pool_cap_amount=1_000_000.0 * PT.SHARED_POOL_MAX_EXPOSURE,
            pool_value=0.0, pending_total=0.0,
        )
        self.assertAlmostEqual(1_000_000.0 * PT.SHARED_POOL_MAX_EXPOSURE, headroom, places=6)
        self.assertFalse(
            set(inspect.signature(PA.pool_headroom).parameters) & {"metadata", "overrides"})


class _PaperDbFixture(unittest.TestCase):
    """带完整模拟盘 schema 的临时库（现金/在途门禁需要真实表）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-pr54-paper-")
        cls._old_path = PT.DB_PATH
        PT.DB_PATH = os.path.join(cls._tmp, "paper.sqlite3")
        PT.init_db()
        cls.conn = sqlite3.connect(PT.DB_PATH, timeout=30)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        PT.DB_PATH = cls._old_path
        shutil.rmtree(cls._tmp, ignore_errors=True)


class ForcedExitPriorityTests(_PaperDbFixture):
    """测试组 J：容量压力下，风控退出不被新开仓挤掉。"""

    def test_risk_exit_is_always_p0(self):
        for purpose in ("hard_stop", "崩盘清仓", "强平", "risk_exit", "drawdown"):
            with self.subTest(purpose=purpose):
                self.assertEqual("P0", PCO.classify_intent("sell", purpose)["priority"])

    def test_exits_sort_before_entries_regardless_of_input_order(self):
        def classified(side, purpose, index):
            item = {"side": side, "purpose": purpose, "order": index}
            item["priority"] = PCO.classify_intent(side, purpose)["priority"]
            return item

        entries = [classified("buy", "new_entry", index) for index in range(20)]
        exits = [classified("sell", "hard_stop", 999)]
        for order in (entries + exits, exits + entries, entries[:10] + exits + entries[10:]):
            with self.subTest(first=order[0]["side"] + order[0]["purpose"]):
                ordered = PCO.sort_intents_by_priority(order)
                self.assertEqual("P0", ordered[0]["priority"])
                self.assertEqual("sell", ordered[0]["side"])

    def test_full_pool_blocks_new_entries_but_never_blocks_exits(self):
        nav = 1_000_000.0
        headroom = PA.pool_headroom(
            pool_cap_amount=nav * PT.SHARED_POOL_MAX_EXPOSURE,
            pool_value=nav * PT.SHARED_POOL_MAX_EXPOSURE,
            pending_total=0.0,
        )
        self.assertAlmostEqual(0.0, headroom, places=6)
        # 退出只减仓，不消耗容量：资金门禁对卖出永不设限（对照：买入被拒）。
        self.assertTrue(EPL.cash_gate(self.conn, "sell", 1e9, 0.0, shared_cash=0.0)["allowed"])
        self.assertFalse(EPL.cash_gate(self.conn, "buy", 1e6, 0.0, shared_cash=0.0)["allowed"])
        self.assertLessEqual(headroom, 1e-6)

    def test_entry_intent_classification_is_never_above_exit(self):
        mapping = {
            "new_entry": PCO.classify_intent("buy", "new_entry")["priority"],
            "add_position": PCO.classify_intent("buy", "scale_in 加仓")["priority"],
            "risk_exit": PCO.classify_intent("sell", "hard_stop")["priority"],
            "take_profit": PCO.classify_intent("sell", "take_profit")["priority"],
        }
        for name in ("new_entry", "add_position"):
            with self.subTest(intent=name):
                self.assertLess(
                    PCO.INTENT_PRIORITY_INDEX[mapping["risk_exit"]],
                    PCO.INTENT_PRIORITY_INDEX[mapping[name]],
                )
        self.assertLess(
            PCO.INTENT_PRIORITY_INDEX[mapping["risk_exit"]],
            PCO.INTENT_PRIORITY_INDEX[mapping["take_profit"]],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
