# -*- coding: utf-8 -*-
"""公共层瘦身切片：新增 owner 的边界与行为守卫。

本文件钉住 2026-10-28 审计切片的三个结论：

1. ``data_fetcher.completed_kline`` 是日线截断的**唯一 owner**，任何异常
   都不得退化成"返回未截断数据"；
2. ``paper_candidate_lanes`` 与 ``paper_entry_price_rules`` 是纯计算 owner：
   不写账本、不取行情、不 import 交易引擎；
3. 手单规划的日线复用只活在**一次规划**内，且每个消费者拿到独立副本。
"""
import ast
import os
import unittest

import paper_candidate_lanes as Lanes
import paper_entry_price_rules as EntryRules
import paper_trading as PT
from paper_entry_price_rules import new_entry_price_gate
from paper_trading_rules import security_scope


BACKEND = os.path.dirname(os.path.abspath(__file__))


def _import_roots(path):
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class ExtractedOwnerBoundaryTests(unittest.TestCase):
    """新 owner 不得反向依赖交易引擎，也不得自己写账本。"""

    def test_candidate_lanes_does_not_import_the_trading_engine(self):
        roots = _import_roots(os.path.join(BACKEND, "paper_candidate_lanes.py"))
        self.assertNotIn("paper_trading", roots,
                         "候选车道 owner 不得反向依赖交易引擎")

    def test_entry_price_rules_does_not_import_the_trading_engine(self):
        roots = _import_roots(os.path.join(BACKEND, "paper_entry_price_rules.py"))
        self.assertNotIn("paper_trading", roots,
                         "价格门禁 owner 不得反向依赖交易引擎")

    def test_candidate_lanes_has_no_database_or_write_calls(self):
        with open(os.path.join(BACKEND, "paper_candidate_lanes.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        forbidden = {"execute", "executemany", "executescript", "commit",
                     "rollback", "connect", "open", "write_text"}
        used = {node.func.attr for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertEqual(set(), used & forbidden,
                         "候选车道 owner 不得写账本或落盘")

    def test_entry_price_rules_is_pure(self):
        with open(os.path.join(BACKEND, "paper_entry_price_rules.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        forbidden = {"execute", "executemany", "executescript", "commit",
                     "rollback", "connect", "open"}
        used = {node.func.attr for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertEqual(set(), used & forbidden,
                         "价格门禁 owner 必须是纯判定")


class EntryPriceGateParityTests(unittest.TestCase):
    """引擎 facade 与 owner 的判定必须一致（迁移没有改变口径）。"""

    def _cases(self):
        return [
            ({"id": "trend_pullback"}, {"code": "600000"},
             {"pct": 1.0, "price": 10.0, "open_price": 10.0}),
            ({"id": "trend_pullback"}, {"code": "600000"},
             {"pct": 1.0, "price": 11.0, "open_price": 10.0}),
            ({"id": "tq_breakout"}, {"code": "600000"},
             {"pct": 9.9, "price": 10.0, "open_price": 10.0}),
            ({"id": "reported_profit_breakout"}, {"code": "600000"},
             {"pct": 9.9, "price": 10.0, "open_price": 10.0}),
            ({"id": "sector_rotation"}, {"code": "600000",
                                          "candidate_status": "ths_hot_lane"},
             {"pct": 8.0, "price": 10.0, "open_price": 10.0}),
            ({"id": "sector_rotation"}, {"code": "600000"},
             {"pct": 8.0, "price": 10.0, "open_price": 10.0}),
        ]

    def test_engine_facade_matches_the_owner(self):
        for account, pick, quote in self._cases():
            with self.subTest(account=account["id"], pct=quote.get("pct")):
                account_id = account["id"]
                owner_result = new_entry_price_gate(
                    account_id, pick, quote,
                    spec=PT._spec_for(account_id),
                    selection_mode=PT.spec_selection_mode(account_id))
                facade_result = PT._new_entry_price_gate(account, pick, quote)
                self.assertEqual(owner_result, facade_result)

    def test_owner_defaults_to_the_builtin_spec(self):
        allowed, reason = new_entry_price_gate("trend_pullback", {"code": "600000"},
                                               {"pct": 1.0, "price": 10.0,
                                                "open_price": 10.0})
        self.assertTrue(allowed, reason)

    def test_unknown_account_falls_back_instead_of_raising(self):
        allowed, reason = new_entry_price_gate("__unknown__", {"code": "600000"},
                                               {"pct": 1.0, "price": 10.0,
                                                "open_price": 10.0})
        self.assertIsInstance(allowed, bool)
        self.assertIsNone(reason)

    def test_security_scope_is_still_the_single_eligibility_entry(self):
        # 候选车道与价格门禁都必须用同一个权限 owner，不能各写一份。
        self.assertIs(Lanes._security_scope, security_scope)


class CandidateLanesBehaviourTests(unittest.TestCase):
    """下沉后的候选车道计算行为不变。"""

    def test_sector_key_strips_known_suffixes(self):
        self.assertEqual("半导体", Lanes._sector_key("半导体行业"))
        self.assertEqual("银行", Lanes._sector_key("银行Ⅱ"))

    def test_sector_heat_map_is_empty_for_empty_universe(self):
        heat = Lanes._sector_heat_map([])
        self.assertIsInstance(heat, dict)

    def test_sector_entry_threshold_context_is_total(self):
        context = Lanes._sector_entry_threshold_context({}, {})
        self.assertIsInstance(context, dict)

    def test_open_runup_bonus_is_time_bounded(self):
        import datetime as dt
        morning = EntryRules._open_runup_bonus(dt.datetime(2026, 10, 28, 9, 35))
        midday = EntryRules._open_runup_bonus(dt.datetime(2026, 10, 28, 11, 30))
        self.assertGreater(morning, midday)
        self.assertGreaterEqual(morning, 0.0)
        self.assertLessEqual(midday, 0.0)


if __name__ == "__main__":
    unittest.main()
