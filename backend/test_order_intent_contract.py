# -*- coding: utf-8 -*-
"""PR-28 回归：OrderIntent 从审计附件升级为强制契约。

覆盖四件事：
1. ``_strategy_contract_mode``：origin=user 或 metadata.contract_version>=1
   进入强制契约模式；builtin（contract_version<1）仍允许 legacy adapter；
2. user 策略信号携带任何 qty/shares/amount/sizing 声明（含嵌套）→ 信号
   终态拒绝（paper_signals.status='rejected'），绝不进入执行链路；
3. 正常 user 信号（意图式负载）→ OrderIntent 构造成功，qty 不在契约中；
4. Guard：paper_trading 里所有直接决定数量（``_price_aware_qty``）的执行
   函数都必须先过 ``_enforce_order_intent``——禁止新增绕过 planner 的
   数量路径。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_trading as PT
import strategy_runtime as SRT


def _breakout_rule():
    return {"op": "gt", "left": {"op": "field", "name": "close"},
            "right": {"op": "indicator", "name": "ma", "window": 20}}


class OrderIntentContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = PT.DB_PATH
        PT.DB_PATH = os.path.join(self.tmp.name, "paper_trading.sqlite3")
        PT.init_db()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, PT, "DB_PATH", self.old_db)
        self.ctx = PT._db()
        self.conn = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.account = {"id": "tq_breakout"}
        self.addCleanup(SRT.clear_cache)
        SRT.clear_cache()

    def _make_user_strategy(self, strategy_id="user_alpha"):
        import strategy_registry as SR
        strategy = SR.create_user_definition(
            self.conn, strategy_id, "User alpha", dsl_ast=_breakout_rule(), actor="test",
        )
        SR.transition(self.conn, strategy.id, "validated", actor="test")
        SR.transition(self.conn, strategy.id, "active", actor="test")
        # 把策略版本绑定到同名账户（信号表触发器要求有效版本戳）。
        head = self.conn.execute(
            "SELECT current_version,current_checksum FROM paper_strategy_version_heads"
            " WHERE strategy_id=?", (strategy.id,),
        ).fetchone()
        self.conn.execute(
            "INSERT OR IGNORE INTO paper_strategy_legacy_bindings"
            " (account_id,strategy_id,strategy_version,strategy_checksum,created_at)"
            " VALUES(?,?,?,?,datetime('now'))",
            (strategy.id, strategy.id, head["current_version"], head["current_checksum"]),
        )
        SRT.clear_cache()
        return strategy.id

    def _set_contract_version(self, strategy_id, version):
        import strategy_registry as SR
        SR.save_definition(
            self.conn, strategy_id, {"metadata": {"contract_version": version}},
        )
        SRT.clear_cache()

    def _signal(self, code="600000", **extra):
        payload = {"decision": {"passed": True}, "pick": {"code": code, "score": 0.8}}
        payload.update(extra)
        return {"id": None, "code": code, "payload": PT._json(payload)}

    def test_builtin_strategy_stays_on_legacy_adapter(self):
        enforced, origin, version = PT._strategy_contract_mode(self.conn, "tq_breakout")
        self.assertFalse(enforced)
        self.assertEqual("builtin", origin)
        self.assertEqual(0, version)

    def test_user_strategy_is_enforced(self):
        strategy_id = self._make_user_strategy()
        enforced, origin, version = PT._strategy_contract_mode(self.conn, strategy_id)
        self.assertTrue(enforced)
        self.assertEqual("user", origin)
        self.assertEqual(0, version)

    def test_contract_version_one_enforces_builtin_too(self):
        self._set_contract_version("tq_breakout", 1)
        enforced, origin, version = PT._strategy_contract_mode(self.conn, "tq_breakout")
        self.assertTrue(enforced)
        self.assertEqual("builtin", origin)
        self.assertEqual(1, version)

    def test_user_signal_with_qty_claim_is_rejected(self):
        strategy_id = self._make_user_strategy()
        stamp = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_strategy_legacy_bindings WHERE account_id=?", (strategy_id,),
        ).fetchone()
        signal = self._signal(qty=500)
        signal["id"] = self.conn.execute(
            "INSERT INTO paper_signals(account_id,code,signal_date,intended_date,status,payload,reason,"
            "strategy_id,strategy_version,strategy_checksum,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (strategy_id, "600000", PT._date().isoformat(), PT._date().isoformat(),
             "pending", signal["payload"], "", stamp["strategy_id"],
             stamp["strategy_version"], stamp["strategy_checksum"]),
        ).lastrowid
        intent, reject = PT._enforce_order_intent(
            self.conn, {"id": strategy_id}, "600000",
            PT._loads(signal["payload"]), signal_id=signal["id"],
        )
        self.assertIsNone(intent)
        self.assertIsNotNone(reject)
        self.assertEqual("risk_rejected", reject["status"])
        status = self.conn.execute(
            "SELECT status FROM paper_signals WHERE id=?", (signal["id"],),
        ).fetchone()
        self.assertEqual("rejected", status["status"])

    def test_nested_sizing_claim_is_rejected(self):
        strategy_id = self._make_user_strategy()
        payload = {
            "decision": {"passed": True},
            "pick": {"code": "600000", "score": 0.8, "sizing": {"ratio": 0.5}},
        }
        intent, reject = PT._enforce_order_intent(
            self.conn, {"id": strategy_id}, "600000", payload,
        )
        self.assertIsNone(intent)
        self.assertIsNotNone(reject)

    def test_intent_style_user_signal_passes_without_qty(self):
        strategy_id = self._make_user_strategy()
        payload = {"decision": {"passed": True},
                   "pick": {"code": "600000", "score": 0.8, "reason": "user alpha"}}
        intent, reject = PT._enforce_order_intent(
            self.conn, {"id": strategy_id}, "600000", payload,
        )
        self.assertIsNone(reject)
        self.assertIsNotNone(intent)
        self.assertEqual("600000", intent.symbol)
        # 契约结构中不允许出现数量字段。
        self.assertNotIn("qty", intent.to_payload())

    def test_builtin_payload_with_market_amount_is_not_rejected(self):
        # legacy adapter：main_force_top10 的 amount 是市场成交额，不拒绝。
        payload = {"decision": {"passed": True},
                   "pick": {"code": "600000", "amount": 12345678.0}}
        intent, reject = PT._enforce_order_intent(
            self.conn, {"id": "main_force_top10"}, "600000", payload,
        )
        self.assertIsNone(reject)
        self.assertIsNone(intent)  # legacy 模式不返回 intent


class ExecutionPathGuardTests(unittest.TestCase):
    """Guard：禁止新增绕过 OrderIntent 闸门的直接数量路径。"""

    @staticmethod
    def _functions_with_direct_sizing():
        import re

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        # 找到所有 def 块及其中 _price_aware_qty 的调用位置。
        defs = [(match.start(), match.group(1))
                for match in re.finditer(r"^def ([a-z_]+)\(", source, re.MULTILINE)]
        call_sites = [match.start() for match in
                      re.finditer(r"qty, sizing = _price_aware_qty\(", source)]
        owners = []
        for position in call_sites:
            owner = next((name for start, name in reversed(defs) if start < position), None)
            owners.append(owner)
        return source, owners

    def test_every_direct_qty_path_is_gated_by_order_intent(self):
        source, owners = self._functions_with_direct_sizing()
        self.assertEqual(
            {"_buy_order", "_intraday_buyback", "_swing_scale_in"}, set(owners),
            f"发现了新的直接数量决策路径: {owners}",
        )
        for owner in sorted(set(owners)):
            match = __import__("re").search(
                rf"^def {owner}\(.*?(?=^def )", source, __import__("re").MULTILINE
                | __import__("re").DOTALL,
            )
            body = match.group(0)
            self.assertIn(
                "_enforce_order_intent", body,
                f"{owner} 直接决定数量但没有 OrderIntent 契约闸门",
            )


if __name__ == "__main__":
    unittest.main()
