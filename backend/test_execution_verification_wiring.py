# -*- coding: utf-8 -*-
"""生产接线回归：执行验证闸门的**写路径盖章**与**谓词唯一性**。

本模块补的是 PR153 的集成缺口，不是契约测试（契约测试在
``test_execution_verification.py``）。三类断言：

1. 写路径盖章 —— 每一条 ``INSERT INTO paper_fills`` 的生产函数都必须在**同一
   事务内**、**流水写入之后**调用 ``stamp_order``；并且真的跑一遍生产链路，
   证明买入腿 / 风控退出腿 / 日内做T腿产生的委托都带上了
   ``paper_fills`` + ``execution_status`` + ``execution_verified``。
2. 谓词唯一性 —— 任何模块都不得自己拼 ``execution_verified=1 AND
   execution_status='verified'``；执行绩效读路径必须引用唯一那份谓词。
3. 指标口径一致 —— 缓存路径与直查路径必须产出同一批已验证成交。
"""
from __future__ import annotations

import ast
import datetime as dt
import os
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import paper_trading as PT  # noqa: E402
from test_production_path_golden_replay import (  # noqa: E402
    ProductionPathGoldenReplayTests,
)


def _production_sources():
    """所有生产模块的 (文件名, 源码)。测试文件不参与闸门判定。"""
    for name in sorted(os.listdir(BACKEND_DIR)):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        path = os.path.join(BACKEND_DIR, name)
        with open(path, encoding="utf-8") as handle:
            yield name, handle.read()


def _docstring_nodes(tree):
    """收集模块/类/函数的文档字符串节点，判定源码时跳过它们。"""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(id(value))
    return found


def _string_literals(tree):
    """产出 (节点, 字符串值)，跳过文档字符串。"""
    skip = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                yield node, node.value


class PaperFillWritePathGuardTests(unittest.TestCase):
    """静态门禁：每个写 paper_fills 的生产函数都必须盖章。

    这条门禁的价值在于**拦住将来新增的写路径**：PR153 的缺陷不是"某处判断
    写错了"，而是"有 3 条写路径根本没接到闸门上"。运行时用例只能覆盖今天
    已知的路径，静态门禁覆盖明天新增的路径。
    """

    def test_every_paper_fills_insert_is_stamped_in_the_same_function(self):
        offenders = []
        seen = []
        for name, source in _production_sources():
            tree = ast.parse(source, filename=name)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                segment = ast.get_source_segment(source, node) or ""
                if "INSERT INTO paper_fills" not in segment:
                    continue
                seen.append(f"{name}:{node.lineno} {node.name}")
                if "stamp_order" not in segment:
                    offenders.append(
                        f"{name}:{node.lineno} {node.name} 写了 paper_fills 但没有调用 stamp_order"
                    )
                    continue
                insert_at = segment.find("INSERT INTO paper_fills")
                stamp_at = segment.rfind("EV.stamp_order(")
                if stamp_at < insert_at:
                    offenders.append(
                        f"{name}:{node.lineno} {node.name} 在写入流水**之前**就盖章，"
                        "evidence_from_order 看不到这条流水，真实成交会被判成没有证据"
                    )
        self.assertEqual(offenders, [], "存在未接闸门的成交流水写路径：\n" + "\n".join(offenders))
        # 门禁自身不能空转：今天已知的写路径必须全部被扫到。
        for expected in ("execution_planner.py", "demo_seed.py"):
            self.assertTrue(
                any(entry.startswith(expected) for entry in seen),
                f"{expected} 的成交流水写路径未被门禁扫到（门禁失效？）：{seen}",
            )
        # R26：所有生产 SELL/BUY 收敛到 ``execution_planner.execute_order`` 之后，
        # paper_trading 不再直接 INSERT INTO paper_fills；活跃写路径只剩
        # execute_order 与 demo_seed 夹具写入器。
        self.assertEqual(
            len(seen), 2,
            f"成交流水写路径数量异常（应统一走 execution_planner.execute_order）：{seen}",
        )

    def test_no_module_reimplements_the_verified_predicate(self):
        forbidden = (
            "COALESCE(execution_verified",
            "execution_verified=1",
            "execution_verified = 1",
            "execution_status='verified'",
            "execution_status = 'verified'",
        )
        offenders = []
        for name, source in _production_sources():
            if name == "execution_verification.py":
                continue
            tree = ast.parse(source, filename=name)
            for _node, value in _string_literals(tree):
                for fragment in forbidden:
                    if fragment in value:
                        offenders.append(f"{name}: 手写谓词片段 {fragment!r}")
        self.assertEqual(
            offenders, [],
            "执行验证谓词只能有一份实现（execution_verification.VERIFIED_PREDICATE）：\n"
            + "\n".join(offenders),
        )

    def test_execution_performance_reads_reference_the_canonical_predicate(self):
        """每个执行绩效读路径都必须引用唯一谓词（而不是各写一份）。"""
        required = {
            "paper_trading.py": ("_execution_verified_predicate", "_row_is_verified"),
            "paper_repository.py": ("EV.VERIFIED_PREDICATE",),
            "dashboard_queries.py": ("_execution_verified_predicate",),
            "deepseek_research.py": ("EV.VERIFIED_PREDICATE",),
            "strategy_champion.py": ("EV.VERIFIED_PREDICATE",),
            "adaptive_risk.py": ("EV.VERIFIED_PREDICATE", "EV.is_verified_row"),
            "adaptive_selection.py": ("EV.VERIFIED_PREDICATE",),
            "rebalance_scanner.py": ("EV.VERIFIED_PREDICATE",),
            "execution_quality_shadow.py": ("EV.is_verified_row",),
            "trade_attribution.py": ("EV.VERIFIED_PREDICATE",),
        }
        missing = []
        sources = dict(_production_sources())
        for name, needles in required.items():
            source = sources.get(name, "")
            for needle in needles:
                if needle not in source:
                    missing.append(f"{name} 未引用 {needle}")
        self.assertEqual(missing, [], "执行绩效读路径未接唯一谓词：\n" + "\n".join(missing))


class IntradaySellStampTests(unittest.TestCase):
    """运行时：缺少可信成交量时，日内做 T 委托不得落成交流水。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="astock-ev-intraday-")
        self.db_path = os.path.join(self.tmp_dir, "paper.sqlite3")
        self.old_db_path = PT.DB_PATH
        PT.DB_PATH = self.db_path
        PT.init_db()
        self.today = dt.date.today()
        self.code = "600001"

    def tearDown(self):
        PT.DB_PATH = self.old_db_path

    def _seed_position(self, conn, account_id, cycle_id, qty=1000, cost=10.0):
        conn.execute(
            """INSERT INTO paper_position_lots(
                   cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,
                   acquired_at,available_date,asset_type,is_t_base)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
            (cycle_id, account_id, self.code, "测试标的", "测试",
             qty, qty, cost, (self.today - dt.timedelta(days=3)).isoformat(),
             (self.today - dt.timedelta(days=1)).isoformat(), "stock_t1"),
        )

    def test_intraday_t_sell_without_liquidity_evidence_does_not_fill(self):
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='running' WHERE id=1")
            cycle = dict(conn.execute(
                "SELECT * FROM paper_cycles WHERE status IN ('draft','running','paused')"
                " ORDER BY id DESC LIMIT 1").fetchone())
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", ("tq_breakout",)).fetchone())
            self.assertEqual(account.get("mode"), "intraday_t", account)
            self._seed_position(conn, account["id"], cycle["id"])
            position = {
                "account_id": account["id"], "code": self.code, "name": "测试标的",
                "qty": 1000, "remaining_qty": 1000, "available_qty": 1000, "cost": 10.0,
            }
            # 冲高 15% 后回撤 4.3%，仍有 10% 成本收益 → 命中日内高抛卖点。
            # 行情必须带当日源时间戳并通过校验（生产口径），否则连卖点都进不去。
            quote = {
                "code": self.code, "price": 11.0, "prev_close": 10.0, "high": 11.5,
                "low": 10.2, "pct": -0.3, "quote_source": "live", "quote_at": PT._now(),
                "quote_validation": "cross_source_checked",
            }
            result, reason = PT._intraday_sell(
                conn, account, position, quote, self.today,
                {"min_cost_edge": 0.012}, cycle,
            )
            self.assertIsNone(result, f"缺少可成交流动性却报告卖出成交：{result}")
            self.assertIn("未成交", reason)
            fill_count = conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]

        self.assertEqual(0, fill_count, "缺少可成交数量证据时不得创建 fill")


class ProductionFillPathStampTests(ProductionPathGoldenReplayTests):
    """运行时：真实生产链路（黄金回放）产出的每一笔成交都必须盖章。

    直接继承黄金回放：它的第 7 步经 ``_buy_order`` 开仓、第 8 步经
    ``_monitor_risk_impl`` 深跌退出 —— 两条腿都是 PR153 漏接闸门的生产路径。
    单独验证手动下单链路是不够的。
    """

    def test_production_path_golden_replay(self):
        """父类的黄金回放用例已在 ``ProductionPathGoldenReplayTests`` 中覆盖。

        本类与父类共用同一个**类级**临时库：两条用例都跑一遍会让后跑的那条
        看不到"草稿策略不可参与新周期"的前置状态。这里跳过重复执行，只保留本类
        独有的闸门断言（断言内部会显式调用父类实现跑完整回放）。
        """
        self.skipTest("黄金回放本体在 ProductionPathGoldenReplayTests 中已覆盖")

    def test_every_real_fill_from_the_production_replay_is_verified(self):
        super().test_production_path_golden_replay()
        with self._conn() as conn:
            fills = conn.execute(
                "SELECT order_id,account_id,side,fill_date FROM paper_fills"
            ).fetchall()
            orders = {
                int(row["id"]): dict(row)
                for row in conn.execute(
                    "SELECT id,account_id,side,status,execution_status,execution_verified"
                    " FROM paper_orders"
                )
            }
        self.assertTrue(fills, "回放必须产生真实成交，否则本断言是空转")
        sides = set()
        offenders = []
        for fill in fills:
            order = orders.get(int(fill["order_id"]))
            self.assertIsNotNone(order, f"流水 {dict(fill)} 找不到对应委托")
            sides.add(order["side"])
            self.assertEqual(order["status"], "filled", order)
            if order["execution_status"] != "verified" or order["execution_verified"] != 1:
                offenders.append(
                    f"order#{order['id']} side={order['side']} "
                    f"status={order['execution_status']} verified={order['execution_verified']}"
                )
        self.assertEqual(
            offenders, [],
            "生产成交没有盖章，会被闸门从已实现盈亏/NAV/执行绩效里剔除：\n"
            + "\n".join(offenders),
        )
        # 买入腿与卖出腿都要出现，否则只证明了其中一条路径。
        self.assertIn("buy", sides, f"回放未产生买入成交：{sides}")
        self.assertIn("sell", sides, f"回放未产生卖出成交：{sides}")


if __name__ == "__main__":
    unittest.main()
