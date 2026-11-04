# -*- coding: utf-8 -*-
"""风险语义守卫：一次扫描一次处理，且只有一套风险实现。

背景：`test_guard12b_paper_trading_facade_is_thin` 在 #341 被删除。它原本除了
限制 40 行，还盯住「`_monitor_risk_impl` 必须委托正式风险服务、不得自己长出
风控循环」。40 行上限是结构性门槛，已按治理规则移除；**业务约束必须保留**。

本文件不恢复行数门槛，改用行为与结构两类断言：

1. 同一个风险扫描身份（cycle_id + asof_day + 扫描分钟）只产生一次有效处理；
2. 重复调度不会重复提交卖出成交、不会重复扣减持仓；
3. 风险运行必须携带显式 cycle/as-of 身份，缺失即拒绝；
4. 风险卖出只能经过唯一成交权威写入，不能自行 INSERT paper_fills；
5. 不允许出现第二套风险执行实现（另一处 position 循环 + 卖出决策）。

负向控制放在 `RiskSemanticNegativeControls`：故意制造重复执行、缺失周期身份
与绕过权威入口，证明守卫**确实会失败**，而不是恒真。
"""
from __future__ import annotations

import ast
import datetime as dt
import os
import re
import unittest
from unittest import mock

import paper_trading as PT
import paper_risk_service as PRSVC
import paper_risk_scan_state as PRSS
from test_paper_risk_exit_production_path import PaperRiskExitProductionPathTestCase

BACKEND = os.path.dirname(os.path.abspath(__file__))


def _source(module: str) -> str:
    with open(os.path.join(BACKEND, f"{module}.py"), encoding="utf-8") as handle:
        return handle.read()


def _function_source(module: str, name: str) -> str:
    """Return the source of one top-level function, or '' when absent."""
    source = _source(module)
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    return ""


class RiskRunIdentityTests(PaperRiskExitProductionPathTestCase):
    """1 & 3：同一扫描身份只处理一次；身份必须显式。"""

    def _sell_rows(self, account_id):
        with PT._db() as conn:
            return conn.execute(
                "SELECT id, side, code, qty, status FROM paper_orders"
                " WHERE account_id=? AND side='sell'", (account_id,)).fetchall()

    def test_one_scan_identity_yields_exactly_one_effective_risk_pass(self):
        account_id = "tq_breakout"
        self._insert_lot(account_id, self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        with mock.patch.object(PT, "request_risk_snapshot_refresh",
                               return_value={"status": "skipped"}), \
                mock.patch.object(PRSVC, "run", wraps=PRSVC.run) as risk_run:
            first = PT.run_slot("risk", self.day, force=True)
            self.assertEqual(first.get("status"), "completed")
            self.assertEqual(risk_run.call_count, 1, "一次调度应只调用一次风险服务")

            # 同一分钟再跑一次：作业层幂等会直接挡住（already_done），
            # 不得再次进入风险服务。
            again = PT.run_slot("risk", self.day, force=True)
            self.assertIn(again.get("status"), {"already_scanned", "already_done"}, again)
            self.assertEqual(risk_run.call_count, 1, "重复调度不得再次进入风险服务")

        rows = self._sell_rows(account_id)
        self.assertEqual(len(rows), 1, "同一扫描身份产生了重复卖出委托")

    def test_repeated_dispatch_does_not_double_consume_lots(self):
        account_id = "tq_breakout"
        self._insert_lot(account_id, self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        with mock.patch.object(PT, "request_risk_snapshot_refresh",
                               return_value={"status": "skipped"}):
            PT.run_slot("risk", self.day, force=True)
            for _ in range(3):
                PT.run_slot("risk", self.day, force=True)

        with PT._db() as conn:
            filled = conn.execute(
                "SELECT COALESCE(SUM(qty),0) FROM paper_orders"
                " WHERE account_id=? AND side='sell' AND status='filled'",
                (account_id,)).fetchone()[0]
            remaining = conn.execute(
                "SELECT COALESCE(SUM(remaining_qty),0) FROM paper_position_lots"
                " WHERE account_id=? AND code=?", (account_id, self.code)).fetchone()[0]
        self.assertEqual(filled, 1000, "卖出成交数量被重复提交")
        self.assertEqual(remaining, 0, "持仓被重复扣减")

    def test_risk_context_rejects_missing_cycle_or_asof(self):
        # 显式身份是决策证据的一部分：缺失必须拒绝，不能回落到 active cycle。
        with self.assertRaises(ValueError):
            PRSVC.RiskRunContext(cycle_id=None, asof_day=self.day)
        with self.assertRaises(ValueError):
            PRSVC.RiskRunContext(cycle_id=1, asof_day=None)

    def test_risk_service_run_requires_context_and_ports(self):
        run_src = _function_source("paper_risk_service", "run")
        self.assertIn("context: RiskRunContext", run_src)
        self.assertIn("ports: RiskServicePorts", run_src)
        # 运行期不得重新解析"此刻的 active cycle"，也不得读机器日期当身份。
        for forbidden in ("_active_cycle(", "date.today(", "datetime.now().date("):
            self.assertNotIn(forbidden, run_src,
                             f"paper_risk_service.run 重新推导了运行身份: {forbidden}")


class RiskFillAuthorityTests(PaperRiskExitProductionPathTestCase):
    """4：风险卖出只能经唯一成交权威写入。"""

    def test_risk_service_never_inserts_fills_directly(self):
        source = _source("paper_risk_service")
        writes = re.findall(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+paper_fills", source, re.I)
        self.assertEqual([], writes, "paper_risk_service 不应自行写入 paper_fills")

    def test_risk_sells_land_through_the_authority_path(self):
        """真实跑一次：成交行由权威路径产生，且带完整订单/持仓血缘。"""
        account_id = "tq_breakout"
        self._insert_lot(account_id, self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

        with mock.patch.object(PT, "request_risk_snapshot_refresh",
                               return_value={"status": "skipped"}):
            self.assertEqual(PT.run_slot("risk", self.day, force=True).get("status"),
                             "completed")

        with PT._db() as conn:
            fills = conn.execute(
                "SELECT f.order_id, f.code, f.qty FROM paper_fills f"
                " JOIN paper_orders o ON o.id=f.order_id"
                " WHERE o.account_id=? AND o.side='sell'", (account_id,)).fetchall()
            orders = conn.execute(
                "SELECT id, status FROM paper_orders WHERE account_id=? AND side='sell'",
                (account_id,)).fetchall()
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(fills), 1, "卖出成交未经过权威路径落库")
        self.assertEqual(fills[0]["order_id"], orders[0]["id"], "成交缺少订单血缘")
        self.assertEqual(orders[0]["status"], "filled")


class NoSecondRiskImplementationTests(unittest.TestCase):
    """5：不允许出现第二套风险执行逻辑。

    判定口径刻意保守：在**非风控 owner** 的生产模块里，同时出现"遍历持仓"与
    "卖出决策/提交"才算可疑。风控 owner 自己当然允许同时拥有两者。
    """

    #: 允许拥有完整风险循环的 owner。
    RISK_OWNERS = frozenset({
        "paper_risk_service",      # 风险业务编排
        "paper_risk_evidence",     # 卖出计划与证据
        "paper_risk_exit_eligibility",
        "paper_risk_scan_state",
        "paper_risk_writes",
        "paper_risk_decision",
        "paper_risk_market_inputs",
        "paper_position_risk_state",
        "adaptive_risk",
        "asymmetric_risk",
    })
    #: 非生产模块（测试/工具/生成物）。
    NON_PRODUCTION_EXCLUDE = (
        "test_", "tools", "demo_", "seed", "fixture", "migrate", "bench",
    )

    def _production_modules(self):
        for name in sorted(os.listdir(BACKEND)):
            if not name.endswith(".py"):
                continue
            stem = name[:-3]
            if stem in self.RISK_OWNERS:
                continue
            if any(token in stem for token in self.NON_PRODUCTION_EXCLUDE):
                continue
            if stem in {"__init__"}:
                continue
            yield stem, _source(stem)

    def test_no_non_owner_module_grows_a_risk_loop(self):
        """只把「遍历持仓 + 真正写入成交」判为第二套实现。

        口径刻意收紧：风控 owner 之外的模块遍历持仓是正常的（只读投影、组合
        读模型、再平衡扫描都这么做）。真正危险的是在循环里**自己提交卖出
        成交**——那才是绕过唯一成交权威的第二套实现。
        """
        offenders = []
        for stem, source in self._production_modules():
            loops_positions = re.search(r"for\s+\w+\s+in\s+positions\b", source)
            if not loops_positions:
                continue
            writes_fill = re.search(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+paper_fills", source, re.I)
            commits_fill = re.search(r"\b(record_fill|submit_fill)\s*\(", source)
            if writes_fill or commits_fill:
                offenders.append(stem)
        self.assertEqual(
            [], offenders,
            "非风控 owner 模块里出现了持仓循环 + 成交写入，疑似第二套风险实现",
        )


class RiskSemanticNegativeControls(unittest.TestCase):
    """负向控制：证明上面的守卫真的会发现违规。

    这些用例构造**应当被判定为违规**的最小样本，断言检测器返回命中。
    如果检测器恒假，这里会失败。
    """

    def _detect_second_implementation(self, source: str) -> bool:
        loops_positions = re.search(r"for\s+\w+\s+in\s+positions\b", source)
        if not loops_positions:
            return False
        writes_fill = re.search(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+paper_fills", source, re.I)
        commits_fill = re.search(r"\b(record_fill|submit_fill)\s*\(", source)
        return bool(writes_fill or commits_fill)

    def test_detector_flags_a_hidden_risk_loop(self):
        hidden = """
def quietly_exit_everything(conn, positions):
    for position in positions:
        if position["pct"] < -8:
            record_fill(conn, position, side="sell")
"""
        self.assertTrue(self._detect_second_implementation(hidden),
                        "检测器没有发现隐藏的风险循环")

    def test_detector_flags_a_hidden_direct_fill_write(self):
        """绕过权威入口的另一种形态：循环里自己写 paper_fills。"""
        hidden = """
def quietly_exit(conn, positions):
    for position in positions:
        conn.execute("INSERT INTO paper_fills(order_id, code, qty) VALUES(?,?,?)",
                     (position["order_id"], position["code"], position["qty"]))
"""
        self.assertTrue(self._detect_second_implementation(hidden))

    def test_detector_ignores_plain_iteration_without_selling(self):
        harmless = """
def summarize(positions):
    total = 0
    for position in positions:
        total += position["value"]
    return total
"""
        self.assertFalse(self._detect_second_implementation(harmless))

    def test_duplicate_scan_is_detectable(self):
        """重复身份必须能被 claim 判定拒绝——负向样本取自真实 claim 接口。"""
        import sqlite3
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        conn = sqlite3.connect(os.path.join(tmp, "scan_state.sqlite3"))
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        # 扫描身份表由主 schema 建立，用真实建表路径而不是手工 DDL。
        with mock.patch.object(PT, "DB_PATH", os.path.join(tmp, "scan_state.sqlite3")):
            PT.init_db()
        day = dt.date(2026, 9, 10)
        identity = dict(cycle_id=1, asof_date=day, scan_minute="2026-09-10 10:00",
                        started_at="2026-09-10 10:00:01")
        first = PRSS.claim_scan(conn, **identity)
        self.assertTrue(first["claimed"], first)
        # 同一身份在 running/completed 时第二次认领必须失败：这就是
        # "重复处理"被挡住的机制。
        second = PRSS.claim_scan(conn, **identity)
        self.assertFalse(second["claimed"], second)
        self.assertIn(second["state"], {"running", "completed"})

    def test_missing_identity_raises_in_negative_sample(self):
        with self.assertRaises(ValueError):
            PRSVC.RiskRunContext(cycle_id=None, asof_day=None)


if __name__ == "__main__":
    unittest.main()
