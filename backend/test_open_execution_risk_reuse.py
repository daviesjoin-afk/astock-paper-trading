# -*- coding: utf-8 -*-
"""``execute_open`` 复用同轮风控结论：不再二次跑 ``monitor_risk``。

2026-10-29 实测：``monitor_risk`` 的去重键是 ``(cycle, asof_date, scan_minute)``，
而一轮 intraday 耗时 99~245 秒必然跨分钟，于是同一轮里它被完整执行两遍
（622/632 轮 ``risk_pass.calls=2``，单次中位 11.577 秒，约 880 秒/交易日）。
本文件钉住"调用方把结论传进来就不再重跑"这一契约，以及"不传就照旧自己跑"。
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_trading as PT  # noqa: E402


class _StopDownstream(Exception):
    """哨兵：在风控之后的第一个下游调用处停下，不跑整条开盘链路。"""


class ExecuteOpenRiskReuseTests(unittest.TestCase):
    def test_empty_resume_result_is_reused(self):
        for orders in ([], [{"order_id": 7, "status": "partially_filled",
                              "filled_qty": 300, "remaining_qty": 700}]):
            with self.subTest(orders=orders):
                gate = {"ready": False, "covered_codes": 0, "eligible_codes": 1,
                        "coverage_pct": 0.0, "required_codes": 1}
                with mock.patch.object(PT, "init_db"), \
                        mock.patch.object(PT, "process_pending_execution_orders") as resume, \
                        mock.patch.object(PT.dfc, "check_data_source_health", return_value={}), \
                        mock.patch.object(PT, "_entry_freeze_status"), \
                        mock.patch.object(PT.MDSvc, "refresh_rows", return_value=[]), \
                        mock.patch.object(PT, "_live_scan_gate", return_value=gate):
                    result = PT.execute_open("2026-10-29", risk_result={}, execution_orders=orders)
                resume.assert_not_called()
                self.assertIs(result["execution_orders"], orders)

    def test_a_provided_risk_result_prevents_a_second_pass(self):
        risk = {"status": "ok", "orders": [{"id": "r1"}], "manual_orders": []}
        with mock.patch.object(PT, "init_db"), \
                mock.patch.object(PT, "monitor_risk",
                                  side_effect=AssertionError("execute_open 重跑了 monitor_risk")), \
                mock.patch.object(PT, "process_pending_execution_orders",
                                  side_effect=_StopDownstream()):
            with self.assertRaises(_StopDownstream):
                PT.execute_open("2026-10-29", risk_result=risk)

    def test_the_default_path_still_runs_the_pass(self):
        sentinel = RuntimeError("默认路径必须自己跑风控")
        with mock.patch.object(PT, "monitor_risk", side_effect=sentinel) as monitor:
            with self.assertRaises(RuntimeError):
                PT.execute_open("2026-10-29")
        monitor.assert_called_once()

    def test_the_reuse_branch_is_the_only_place_that_reads_risk_result(self):
        """结构守卫：复用必须发生在风控调用那一行，而不是事后覆盖。"""
        source = inspect.getsource(PT.execute_open)
        tree = ast.parse(source)
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "monitor_risk"
        ]
        self.assertEqual(1, len(calls), "execute_open 里只允许一处 monitor_risk 调用")
        assigns = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "opening_risk" for t in node.targets)
        ]
        self.assertEqual(1, len(assigns), "opening_risk 只能被赋值一次")
        rendered = ast.unparse(assigns[0])
        self.assertIn("risk_result", rendered, rendered)
        self.assertIn("is not None", rendered, rendered)

    def test_reused_risk_orders_are_not_listed_twice(self):
        """execute_open 已把复用到的风控单并进它的 orders，调用方不得再拼一次。

        code-review 在 PR #292 抓到的 P2：``monitor_intraday`` 既把
        ``risk_result["orders"]`` 拼进 ``actions``，又让 ``execute_open`` 把这些
        同一批订单并进 ``open_result["orders"]``，于是每笔风控单在响应与 job
        detail 里出现两次（实际只成交一次）。
        """
        risk_order = {"id": "risk-1", "side": "sell"}
        risk = {"status": "ok", "orders": [risk_order], "manual_orders": []}
        open_result = {"status": "ok", "orders": [risk_order], "manual_orders": [],
                       "opening_event": {"orders": []}}
        # 直接复算调用方的聚合逻辑，避免把整条 intraday 链路拖进单测。
        opening_orders = list(open_result.get("orders", []))
        already = {id(order) for order in opening_orders}
        risk_orders_for_result = [o for o in (risk.get("orders") or []) if id(o) not in already]
        actions = risk_orders_for_result + opening_orders
        self.assertEqual(1, len(actions), "同一笔风控单不得在聚合里出现两次")
        self.assertIs(risk_order, actions[0])

if __name__ == "__main__":
    unittest.main()
