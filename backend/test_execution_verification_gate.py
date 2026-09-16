# -*- coding: utf-8 -*-
"""执行验证闸门（PR150 消费层）的架构守卫。

本文件守护的是**接入完整性**，不是成交判定：每一条把流水写进 ``paper_fills`` 的
生产路径都必须在同一次事务里盖章，否则该笔委托的验证列永远为 NULL，被闸门当成
"没有证据"而从已实现盈亏、持仓现金流与执行绩效里剔除 —— 一笔真实成交被记成没发生。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import execution_verification as EV  # noqa: E402

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


def _paper_trading_source() -> str:
    with open(os.path.join(BACKEND_DIR, "paper_trading.py"), encoding="utf-8") as handle:
        return handle.read()


#: 绕过唯一谓词的手写判断：在 SQL 字符串常量里直接写死 ``execution_verified = 1``。
_BARE_VERIFIED = re.compile(r"execution_verified\s*=\s*1")


def _bare_verified_sql(path: str, module: str) -> list:
    """找出模块里**真实字符串常量**中绕过唯一谓词的写法。

    只解析 AST 的非文档字符串常量：注释与 docstring 不算 —— 文档为了说明口径而
    引用 ``execution_verified=1`` 是必要的，把它当成违规会让守卫变成噪声。
    唯一谓词的权威定义模块（``execution_verification``）与测试文件本身也不检查。
    """
    import ast

    if module.startswith("test_") or module == "execution_verification.py":
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except SyntaxError:  # pragma: no cover - 语法错误由其它门禁负责
        return []
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if _BARE_VERIFIED.search(node.value):
                hits.append(f"{module}:{node.lineno}")
    return hits


class EveryFillPathStampsVerificationTests(unittest.TestCase):
    """``INSERT INTO paper_fills`` 的每一个位置后面都必须跟着一次盖章。"""

    #: 一条流水写入到它那次盖章之间允许跨越的行数。同一次事务里的写入与盖章是
    #: 紧邻的几行（最多跨越一次 ``UPDATE paper_orders`` 与若干审计调用），
    #: 用上限而不是精确值，避免无关重构把守卫变成噪声。
    MAX_GAP_LINES = 40

    def test_every_fill_insert_is_followed_by_a_stamp(self):
        lines = _paper_trading_source().split("\n")
        inserts = [i for i, line in enumerate(lines) if "INSERT INTO paper_fills" in line]
        stamps = [i for i, line in enumerate(lines) if "EV.stamp_order(" in line]
        self.assertTrue(inserts, "paper_trading.py 不再直接写 paper_fills？守卫需重新定位")
        self.assertTrue(stamps, "没有任何盖章点：闸门等于没接进写路径")
        for index in inserts:
            later = [s for s in stamps if s > index]
            self.assertTrue(
                later,
                f"paper_fills 写入（第 {index + 1} 行）之后没有盖章："
                "这笔真实成交会被闸门当成没有证据",
            )
            gap = later[0] - index
            self.assertLessEqual(
                gap,
                self.MAX_GAP_LINES,
                f"paper_fills 写入（第 {index + 1} 行）与盖章（第 {later[0] + 1} 行）"
                f"相隔 {gap} 行，可能已不在同一次事务里",
            )

    def test_the_predicate_requires_both_columns_to_agree(self):
        """谓词必须同时要求两列，两列不一致的行 fail closed。"""
        self.assertIn("execution_verified", EV.VERIFIED_PREDICATE)
        self.assertIn("execution_status = 'verified'", EV.VERIFIED_PREDICATE)
        self.assertIn("COALESCE", EV.VERIFIED_PREDICATE.upper())

    def test_legacy_null_rows_are_excluded_by_the_predicate(self):
        """历史 NULL 行不得被当成成交：COALESCE 取 0，谓词为假。"""
        self.assertFalse(EV.is_verified_status(None))
        self.assertFalse(EV.is_verified_status(""))
        self.assertFalse(EV.is_verified_status("unknown"))
        self.assertTrue(EV.is_verified_status(EV.EXECUTION_STATUS_VERIFIED))

    def test_only_the_verified_verdict_maps_to_verified(self):
        """六分类到四态的映射必须穷尽，且只有 fill_verified 判成交。"""
        verdicts = (
            "fill_verified", "fill_partial", "fill_pending",
            "fill_none_confirmed", "fill_not_attempted", "fill_unknown",
        )
        for verdict in verdicts:
            self.assertIn(verdict, EV.VERDICT_TO_STATUS, verdict)
        mapping = EV.VERDICT_TO_STATUS
        self.assertEqual(
            [v for v, s in mapping.items() if s == EV.EXECUTION_STATUS_VERIFIED],
            ["fill_verified"],
        )
        # 在途不是"成交了一部分"，也不是"确认没成交"。
        self.assertEqual(EV.EXECUTION_STATUS_UNKNOWN, EV.status_from_verdict("fill_pending"))
        # 契约之外的 verdict 不能当成成交。
        self.assertEqual(EV.EXECUTION_STATUS_UNKNOWN, EV.status_from_verdict("bogus"))

    def test_unverified_predicate_is_the_exact_negation(self):
        self.assertEqual("NOT " + EV.VERIFIED_PREDICATE, EV.UNVERIFIED_PREDICATE)

    def test_the_gate_is_referenced_by_the_read_paths_it_claims_to_gate(self):
        """读路径必须引用同一个谓词，不得各写一份 ``execution_verified=1``。

        只检查**真实字符串常量**（SQL 字面量），跳过注释与文档字符串：文档里为了
        说明口径而引用该写法不算绕过。
        """
        source = _paper_trading_source()
        self.assertIn("_execution_verified_predicate()", source)
        offenders = []
        for module in sorted(os.listdir(BACKEND_DIR)):
            if not module.endswith(".py"):
                continue
            offenders.extend(_bare_verified_sql(os.path.join(BACKEND_DIR, module), module))
        self.assertEqual(
            [], offenders,
            "读路径出现了绕过唯一谓词的手写判断：" + repr(offenders),
        )


class DisplayCostUnknownTests(unittest.TestCase):
    """现金流投影缺失必须是**未知**，不能塌成"有定义的 0"。"""

    def test_missing_flow_falls_back_to_the_lot_cost(self):
        import paper_portfolio as portfolio

        lots = [{
            "account_id": "acct", "code": "000001", "name": "测试股", "industry": "银行",
            "remaining_qty": 100, "cost": 10.0, "acquired_at": "2026-09-02 10:00:00",
            "available_date": "2026-09-03", "asset_type": "stock_t1",
        }]
        num = lambda value, default=0.0: float(value) if value is not None else default  # noqa: E731
        absent = portfolio.aggregate_positions(lots, [], {}, "2026-09-03", num=num)[0]
        present = portfolio.aggregate_positions(
            lots, [], {("acct", "000001"): {"buy_cash": 1000.0, "sell_cash": 0.0}},
            "2026-09-03", num=num,
        )[0]
        self.assertEqual("lot_settlement_cost", absent["display_cost_source"])
        self.assertEqual("verified_cash_flow", present["display_cost_source"])
        self.assertNotEqual(0.0, absent["display_cost"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
