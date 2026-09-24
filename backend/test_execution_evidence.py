# -*- coding: utf-8 -*-
"""执行证据三态契约测试。

每条测试对应一个**可证伪**陈述。变异脚本
``work/execution_reality_mutation_check.py`` 的 M51（missing fill => assume filled）
由本文件抓住；M52（reject => filled）与 M53（partial fill => full fill）由本文件与
``test_execution_lifecycle.py`` 共同抓住。

核心不变式：**``None`` 不等于零，不等于"没成交"，也不等于"被拒绝"**。
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
import sqlite3
import unittest

import execution_evidence as EE
import execution_lifecycle as EL
import paper_trading_rules as PTR

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION = "2026-09-16"
CREATED_AT = "2026-09-16 09:31:00"


def order(**overrides):
    """一行 ``paper_orders`` 的最小真实形状。"""
    row = {
        "id": 1,
        "account_id": "main_force_top10",
        "side": "buy",
        "code": "600001",
        "qty": 100,
        "planned_price": 10.0,
        "status": "filled",
        "reason": "",
        "created_at": CREATED_AT,
        "order_type": "market",
    }
    row.update(overrides)
    return row


def fill(qty=100, price=10.01, fees=None, session=SESSION):
    amount = qty * price
    if fees is None:
        fees = PTR.commission(amount)
    return {
        "qty": qty,
        "price": price,
        "amount": amount,
        "fees": fees,
        "fill_date": session,
    }


class EvidenceFieldStateTests(unittest.TestCase):
    """三态本身：构造期就禁止 None 语义混用。"""

    def test_evidence_states_are_exactly_the_three_contract_states(self):
        self.assertEqual(("known", "unknown", "not_applicable"), EE.EVIDENCE_STATES)

    def test_known_field_rejects_a_none_value(self):
        with self.assertRaises(EE.ExecutionEvidenceError):
            EE.EvidenceField.known("filled_qty", None)

    def test_non_known_field_rejects_carrying_a_value(self):
        for state in (EE.EVIDENCE_UNKNOWN, EE.EVIDENCE_NOT_APPLICABLE):
            with self.assertRaises(EE.ExecutionEvidenceError):
                EE.EvidenceField(name="filled_qty", state=state, value=0)

    def test_unknown_and_not_applicable_carry_no_value_at_all(self):
        unknown = EE.EvidenceField.unknown("filled_qty")
        not_applicable = EE.EvidenceField.not_applicable("filled_qty")
        self.assertIsNone(unknown.value)
        self.assertIsNone(not_applicable.value)
        self.assertIsNone(unknown.maybe())
        self.assertIsNone(not_applicable.maybe())

    def test_require_raises_for_every_non_known_state(self):
        for holder in (
            EE.EvidenceField.unknown("filled_qty"),
            EE.EvidenceField.not_applicable("filled_qty"),
        ):
            with self.assertRaises(EE.UnknownEvidenceAccess):
                holder.require()

    def test_known_zero_is_readable_and_distinguishable_from_unknown(self):
        zero = EE.EvidenceField.known("filled_qty", 0)
        self.assertEqual(0, zero.require())
        self.assertNotEqual(
            zero.fingerprint(), EE.EvidenceField.unknown("filled_qty").fingerprint()
        )
        self.assertNotEqual(
            zero.fingerprint(),
            EE.EvidenceField.not_applicable("filled_qty").fingerprint(),
        )

    def test_unknown_and_not_applicable_are_distinguishable(self):
        self.assertNotEqual(
            EE.EvidenceField.unknown("filled_qty").fingerprint(),
            EE.EvidenceField.not_applicable("filled_qty").fingerprint(),
        )

    def test_unknown_field_name_is_rejected(self):
        with self.assertRaises(EE.ExecutionEvidenceError):
            EE.EvidenceField.known("not_a_field", 1)

    def test_required_contract_fields_match_the_declared_boundary(self):
        expected = {
            "code", "order_time", "action", "requested_qty", "filled_qty", "fill_price",
            "fill_session", "reject_reason", "cancel_reason", "available_qty",
            "commission", "slippage",
        }
        self.assertTrue(expected.issubset(set(EE.EXECUTION_EVIDENCE_FIELDS)))
        names = {item.name for item in dataclasses.fields(EE.ExecutionEvidence)}
        self.assertTrue(expected.issubset(names), sorted(expected - names))

    def test_field_name_mismatch_is_rejected(self):
        evidence = EE.evidence_from_order(order(), [fill()])
        with self.assertRaises(EE.ExecutionEvidenceError):
            dataclasses.replace(evidence, filled_qty=evidence.requested_qty)

    def test_lifecycle_state_must_be_a_known_state(self):
        evidence = EE.evidence_from_order(order(), [fill()])
        with self.assertRaises(EE.ExecutionEvidenceError):
            dataclasses.replace(evidence, lifecycle_state="BOGUS")


class FillVerdictTests(unittest.TestCase):
    """六分类成交判定：三态不得塌缩成一团。"""

    def test_full_fill_is_verified(self):
        evidence = EE.evidence_from_order(order(), [fill()])
        self.assertTrue(evidence.proves_fill())
        self.assertEqual(EE.FILL_VERDICT_VERIFIED, evidence.fill_verdict_value())
        self.assertTrue(evidence.has_positive_fill())
        self.assertEqual((), evidence.inconsistencies())

    def test_partial_fill_is_partial_and_never_verified(self):
        evidence = EE.evidence_from_order(order(qty=1000), [fill(qty=300, price=10.0)])
        self.assertEqual(EE.FILL_VERDICT_PARTIAL, evidence.fill_verdict_value())
        self.assertFalse(evidence.proves_fill())
        self.assertTrue(evidence.has_positive_fill())

    def test_stored_filled_without_any_fill_row_is_unknown_not_filled(self):
        """M51：缺失成交证据绝不能被当成成交。"""
        evidence = EE.evidence_from_order(order(status="filled"), [])
        self.assertTrue(evidence.filled_qty.is_unknown, evidence.filled_qty)
        self.assertFalse(evidence.filled_qty.is_known)
        self.assertIsNone(evidence.filled_qty.maybe())
        self.assertFalse(evidence.has_positive_fill())
        self.assertFalse(evidence.proves_fill())
        self.assertEqual(EE.FILL_VERDICT_UNKNOWN, evidence.fill_verdict_value())
        self.assertIn(
            EE.INCONSISTENCY_STORED_FILLED_WITHOUT_FILL_ROW, evidence.inconsistencies()
        )

    def test_rejected_order_is_an_affirmative_zero_not_unknown(self):
        """M52：``reject`` 不能被当成 ``filled``。"""
        evidence = EE.evidence_from_order(
            order(status="risk_rejected", reason="策略席位已满"), []
        )
        self.assertEqual(EL.STATE_REJECTED, evidence.lifecycle_state)
        self.assertTrue(evidence.filled_qty.is_known)
        self.assertEqual(0, evidence.filled_qty.require())
        self.assertEqual(EE.FILL_VERDICT_NONE_CONFIRMED, evidence.fill_verdict_value())
        self.assertFalse(evidence.proves_fill())
        self.assertTrue(evidence.reject_reason.is_known)
        self.assertEqual("策略席位已满", evidence.reject_reason.require())
        self.assertTrue(evidence.fill_price.is_not_applicable)
        self.assertTrue(evidence.fill_session.is_not_applicable)

    def test_never_submitted_order_is_not_attempted_not_none_confirmed(self):
        evidence = EE.evidence_from_order(order(status="shadow_q3"), [])
        self.assertEqual(EL.STATE_CREATED, evidence.lifecycle_state)
        self.assertTrue(evidence.filled_qty.is_not_applicable)
        self.assertEqual(EE.FILL_VERDICT_NOT_ATTEMPTED, evidence.fill_verdict_value())
        self.assertNotEqual(
            evidence.fill_verdict_value(), EE.FILL_VERDICT_NONE_CONFIRMED
        )

    def test_in_flight_order_is_pending_not_none_confirmed(self):
        evidence = EE.evidence_from_order(order(status="pending_limit"), [])
        self.assertEqual(EL.STATE_SUBMITTED, evidence.lifecycle_state)
        self.assertEqual(EE.FILL_VERDICT_PENDING, evidence.fill_verdict_value())
        self.assertNotEqual(
            evidence.fill_verdict_value(), EE.FILL_VERDICT_NONE_CONFIRMED
        )

    def test_the_three_no_fill_situations_are_mutually_distinct(self):
        """``None`` / zero / rejected / never-attempted 四种写法必须互不相同。"""
        rejected = EE.evidence_from_order(order(status="risk_rejected", reason="x"), [])
        missing = EE.evidence_from_order(order(status="filled"), [])
        shadow = EE.evidence_from_order(order(status="shadow_q3"), [])
        fingerprints = {
            rejected.filled_qty.fingerprint(),
            missing.filled_qty.fingerprint(),
            shadow.filled_qty.fingerprint(),
        }
        self.assertEqual(3, len(fingerprints), fingerprints)
        verdicts = {
            rejected.fill_verdict_value(),
            missing.fill_verdict_value(),
            shadow.fill_verdict_value(),
        }
        self.assertEqual(3, len(verdicts), verdicts)

    def test_fill_verdict_requires_a_trustworthy_price(self):
        evidence = EE.evidence_from_order(order(), [dict(fill(), price=0.0)])
        self.assertTrue(evidence.fill_price.is_unknown)
        self.assertEqual(EE.FILL_VERDICT_UNKNOWN, evidence.fill_verdict_value())
        self.assertFalse(evidence.proves_fill())

    def test_fill_verdict_requires_a_fill_session(self):
        evidence = EE.evidence_from_order(order(), [dict(fill(), fill_date="")])
        self.assertTrue(evidence.fill_session.is_unknown)
        self.assertEqual(EE.FILL_VERDICT_UNKNOWN, evidence.fill_verdict_value())

    def test_fill_rows_without_a_positive_quantity_are_not_fills(self):
        evidence = EE.evidence_from_order(order(), [fill(qty=0, price=10.0)])
        self.assertTrue(evidence.filled_qty.is_unknown)
        self.assertEqual(1, evidence.provenance["ignored_fill_rows"])
        self.assertEqual(0, evidence.provenance["fill_rows"])

    def test_over_fill_is_reported_as_an_inconsistency(self):
        evidence = EE.evidence_from_order(order(qty=100), [fill(qty=200, price=10.0)])
        self.assertIn(
            EE.INCONSISTENCY_FILL_EXCEEDS_REQUESTED, evidence.inconsistencies()
        )
        # 超过目标数量的证据自相矛盾：不认一次干净的成交。
        self.assertEqual(EE.FILL_VERDICT_UNKNOWN, evidence.fill_verdict_value())
        self.assertFalse(evidence.proves_fill())

    def test_multi_row_fill_uses_a_quantity_weighted_price(self):
        rows = [fill(qty=60, price=10.0), fill(qty=40, price=11.0)]
        evidence = EE.evidence_from_order(order(qty=100), rows)
        self.assertEqual(100, evidence.filled_qty.require())
        self.assertAlmostEqual(10.4, evidence.fill_price.require(), places=6)
        self.assertEqual(EE.FILL_VERDICT_VERIFIED, evidence.fill_verdict_value())


class SideSpecificEvidenceTests(unittest.TestCase):
    def test_sell_available_qty_is_unknown_without_explicit_evidence(self):
        evidence = EE.evidence_from_order(
            order(side="sell", status="pending_limit"), []
        )
        self.assertTrue(evidence.available_qty.is_unknown)
        self.assertIsNone(evidence.available_qty.maybe())
        self.assertIn(
            EE.INCONSISTENCY_SELL_WITHOUT_AVAILABLE_QTY, evidence.inconsistencies()
        )

    def test_sell_available_qty_is_known_only_with_explicit_provenance(self):
        evidence = EE.evidence_from_order(
            order(side="sell", status="pending_limit"), [],
            available_qty=500, available_source="pit_lots_replay",
        )
        self.assertEqual(500, evidence.available_qty.require())
        self.assertEqual("pit_lots_replay", evidence.available_qty.source)

    def test_buy_available_qty_does_not_apply(self):
        evidence = EE.evidence_from_order(order(), [fill()])
        self.assertTrue(evidence.available_qty.is_not_applicable)
        self.assertNotIn(
            EE.INCONSISTENCY_SELL_WITHOUT_AVAILABLE_QTY, evidence.inconsistencies()
        )

    def test_unknown_side_leaves_available_qty_unknown(self):
        evidence = EE.evidence_from_order(order(side=""), [])
        self.assertTrue(evidence.action.is_unknown)
        self.assertTrue(evidence.available_qty.is_unknown)


class FeeAndSlippageTests(unittest.TestCase):
    def test_commission_is_known_when_fees_match_the_authoritative_model(self):
        amount = 100 * 10.01
        evidence = EE.evidence_from_order(order(), [fill(price=10.01)])
        self.assertTrue(evidence.commission.is_known)
        self.assertAlmostEqual(PTR.commission(amount), evidence.commission.require())
        self.assertTrue(evidence.fees.is_known)
        # 买入侧仓库只收佣金，因此 commission 与 fees 恰好相等；它们仍然是
        # 两个独立字段（卖出侧就必须分开，见下一个用例）。
        self.assertAlmostEqual(evidence.fees.require(), evidence.commission.require())

    def test_sell_commission_excludes_the_stamp_tax(self):
        amount = 100 * 10.01
        fees = PTR.commission(amount) + amount * PTR.STAMP_SELL
        evidence = EE.evidence_from_order(
            order(side="sell", status="filled"), [fill(price=10.01, fees=fees)]
        )
        self.assertTrue(evidence.commission.is_known)
        self.assertAlmostEqual(PTR.commission(amount), evidence.commission.require())
        self.assertGreater(evidence.fees.require(), evidence.commission.require())

    def test_commission_is_unknown_when_fees_do_not_reconcile(self):
        evidence = EE.evidence_from_order(
            order(), [fill(price=10.01, fees=99.99)]
        )
        self.assertTrue(evidence.commission.is_unknown)
        self.assertIsNone(evidence.commission.maybe())
        self.assertIn(
            EE.INCONSISTENCY_FEES_NOT_RECONCILED, evidence.inconsistencies()
        )

    def test_commission_is_not_applicable_without_a_fill(self):
        evidence = EE.evidence_from_order(order(status="risk_rejected", reason="x"), [])
        self.assertTrue(evidence.commission.is_not_applicable)
        self.assertTrue(evidence.fees.is_not_applicable)

    def test_slippage_is_adverse_positive_for_both_sides(self):
        bought = EE.evidence_from_order(order(), [fill(price=10.10)])
        sold = EE.evidence_from_order(
            order(side="sell"), [fill(price=9.90)]
        )
        self.assertGreater(bought.slippage.require(), 0)
        self.assertGreater(sold.slippage.require(), 0)
        self.assertAlmostEqual(100.0, bought.slippage.require(), places=6)

    def test_slippage_is_unknown_without_a_planned_price(self):
        evidence = EE.evidence_from_order(order(planned_price=None), [fill()])
        self.assertTrue(evidence.slippage.is_unknown)
        self.assertIn(
            EE.INCONSISTENCY_PLANNED_PRICE_MISSING, evidence.inconsistencies()
        )

    def test_slippage_does_not_apply_without_a_fill(self):
        evidence = EE.evidence_from_order(order(status="expired", reason="超时"), [])
        self.assertTrue(evidence.slippage.is_not_applicable)

    def test_reconcile_fees_rejects_an_unknown_side(self):
        result = EE.reconcile_fees("", 1000.0, 0.1)
        self.assertFalse(result["reconciled"])
        self.assertIsNone(result["commission"])


class SerializationTests(unittest.TestCase):
    def test_as_dict_is_json_serializable_and_carries_every_field(self):
        evidence = EE.evidence_from_order(order(), [fill()])
        payload = evidence.as_dict()
        json.dumps(payload)
        for name in EE.ALL_EVIDENCE_FIELDS:
            self.assertIn(name, payload)
            self.assertIn("state", payload[name])
        self.assertEqual(EE.EXECUTION_EVIDENCE_VERSION, payload["version"])
        self.assertEqual(EE.FILL_VERDICT_VERIFIED, payload["fill_verdict"])

    def test_fingerprint_covers_every_field(self):
        evidence = EE.evidence_from_order(order(), [fill()])
        self.assertEqual(
            set(EE.ALL_EVIDENCE_FIELDS), set(evidence.fingerprint())
        )


class LoadExecutionEvidenceTests(unittest.TestCase):
    """真实 schema 集成：只读读取 paper_orders + paper_fills。"""

    SIGMA_ORDERS = (
        "id", "account_id", "side", "code", "qty", "planned_price", "filled_price",
        "amount", "fees", "status", "reason", "created_at", "executed_at",
        "cancelled_at", "order_type",
    )
    #: 身份列必须在读取列集里：只按 order_id 关联会把错行当成权威成交证据。
    #: ``event_key`` 是 owner 的**逐次执行事实身份**（``sha256(order_id|quote_at|ruleset_version)``），
    #: R27-B2C-1 起随流水一起读出，供 owner fact 投影使用。
    SIGMA_FILLS = ("order_id", "account_id", "side", "code", "qty", "price", "amount",
                   "fees", "fill_date", "quote_at", "event_key")

    @classmethod
    def _migrated_columns(cls, table):
        """``paper_schema_migrations.ensure_columns(conn, "<table>", {...})`` 声明的列名。

        生产 schema **不只是**基础 ``CREATE TABLE``：增量列由 migration 补齐
        （``paper_fills.event_key`` 就在这里，基础 DDL 里没有）。少了这一步，
        "被选中的列真的存在于生产 schema"会退化成"存在于某一份不完整的表结构快照"。
        """
        with open(os.path.join(BACKEND_DIR, "paper_schema_migrations.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 3:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "ensure_columns":
                continue
            table_arg, definitions = node.args[1], node.args[2]
            if not (isinstance(table_arg, ast.Constant) and table_arg.value == table):
                continue
            if isinstance(definitions, ast.Dict):
                found.update(
                    key.value for key in definitions.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
        return found

    @classmethod
    def _real_ddl(cls, table):
        with open(os.path.join(BACKEND_DIR, "paper_trading.py"), encoding="utf-8") as handle:
            source = handle.read()
        match = re.search(
            r"CREATE TABLE IF NOT EXISTS %s\s*\((.*?)\n\s*\);" % table, source, re.S
        )
        if match is None:
            raise AssertionError("paper_trading.py no longer declares %s" % table)
        body = re.sub(r"(PRIMARY|UNIQUE)\s*\([^)]*\)", "", match.group(1), flags=re.I)
        columns = set()
        for part in body.split(","):
            token = part.strip()
            if not token or token.startswith("--"):
                continue
            name = token.split()[0].strip().strip('"').strip("`")
            if name and name.upper() not in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN"):
                columns.add(name)
        return columns | cls._migrated_columns(table)

    def test_selected_columns_exist_in_the_production_schema(self):
        orders = self._real_ddl("paper_orders")
        fills = self._real_ddl("paper_fills")
        self.assertTrue(set(self.SIGMA_ORDERS).issubset(orders), sorted(
            set(self.SIGMA_ORDERS) - orders))
        self.assertTrue(set(self.SIGMA_FILLS).issubset(fills), sorted(
            set(self.SIGMA_FILLS) - fills))

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE paper_orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                signal_id INTEGER, side TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
                qty INTEGER NOT NULL, planned_price REAL, filled_price REAL,
                amount REAL, fees REAL, status TEXT NOT NULL, reason TEXT,
                risk_payload TEXT NOT NULL DEFAULT '', realized_pnl REAL,
                created_at TEXT NOT NULL, executed_at TEXT,
                order_type TEXT NOT NULL DEFAULT 'market', origin TEXT NOT NULL DEFAULT 'strategy',
                expires_at TEXT, cancelled_at TEXT
            );
            CREATE TABLE paper_fills(
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, side TEXT NOT NULL, code TEXT NOT NULL,
                qty INTEGER NOT NULL, price REAL NOT NULL, amount REAL NOT NULL, fees REAL NOT NULL,
                fill_date TEXT NOT NULL, quote_at TEXT, event_key TEXT,
                assumption TEXT NOT NULL DEFAULT ''
            );
            """
        )
        return conn

    def test_load_reads_and_groups_fills_per_order(self):
        conn = self._conn()
        amount = 100 * 10.01
        conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,planned_price,status,reason,"
            "created_at,order_type) VALUES('a','buy','600001',100,10.0,'filled','',?,'market')",
            (CREATED_AT,),
        )
        filled_id = conn.execute("SELECT id FROM paper_orders").fetchone()[0]
        conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,'')",
            (filled_id, "a", "buy", "600001", 100, 10.01, amount, PTR.commission(amount),
             SESSION, CREATED_AT),
        )
        conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,planned_price,status,reason,"
            "created_at,order_type) VALUES('a','buy','600002',100,10.0,'risk_rejected','x',"
            "?,'market')",
            (CREATED_AT,),
        )
        evidence = EE.load_execution_evidence(conn, limit=50)
        self.assertEqual(2, len(evidence))
        verdicts = {item.code.require(): item.fill_verdict_value() for item in evidence}
        self.assertEqual(EE.FILL_VERDICT_VERIFIED, verdicts["600001"])
        self.assertEqual(EE.FILL_VERDICT_NONE_CONFIRMED, verdicts["600002"])

    def test_load_filters_by_account_and_returns_empty_for_unmatched(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,status,reason,created_at) "
            "VALUES('a','buy','600001',100,'filled','',?)",
            (CREATED_AT,),
        )
        self.assertEqual(1, len(EE.load_execution_evidence(conn, account_id="a")))
        self.assertEqual([], EE.load_execution_evidence(conn, account_id="zzz"))

    def test_load_is_read_only(self):
        conn = self._conn()
        conn.execute("INSERT INTO paper_orders(account_id,side,code,qty,status,reason,"
                     "created_at) VALUES('a','buy','600001',100,'filled','',?)", (CREATED_AT,))
        before = conn.total_changes
        EE.load_execution_evidence(conn)
        self.assertEqual(before, conn.total_changes)

    def _order_with_mismatched_fill(self):
        """一笔买 ``600001`` 的委托，配一条身份完全对不上的流水。"""
        conn = self._conn()
        conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,planned_price,status,reason,"
            "created_at,order_type) VALUES('acc-a','buy','600001',100,10.0,'filled','',"
            "?,'market')",
            (CREATED_AT,),
        )
        order_id = conn.execute("SELECT id FROM paper_orders").fetchone()[0]
        amount = 100 * 99.0
        conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,'imported')",
            (order_id, "acc-other", "sell", "600002", 100, 99.0, amount,
             PTR.commission(amount), SESSION, CREATED_AT),
        )
        return conn, order_id

    def test_a_fill_row_that_does_not_match_the_order_is_not_evidence(self):
        """身份对不上的流水不得被当成权威成交证据。"""
        conn, order_id = self._order_with_mismatched_fill()
        evidence = EE.load_execution_evidence(conn, order_ids=[order_id])[0]
        self.assertFalse(evidence.proves_fill())
        self.assertEqual(EE.FILL_VERDICT_UNKNOWN, evidence.fill_verdict_value())
        self.assertFalse(evidence.has_positive_fill())
        self.assertTrue(evidence.filled_qty.is_unknown)
        self.assertNotEqual(99.0, evidence.fill_price.maybe())
        self.assertIn(
            EE.INCONSISTENCY_FILL_IDENTITY_MISMATCH, evidence.inconsistencies()
        )
        self.assertEqual(0, evidence.provenance["fill_rows"])
        self.assertEqual(1, evidence.provenance["excluded_fill_rows"])
        self.assertTrue(evidence.provenance["fill_identity_checked"])
        fields = {item["field"] for item in evidence.provenance["fill_identity_mismatches"]}
        self.assertEqual({"account_id", "side", "code"}, fields)

    def test_a_matching_fill_row_still_verifies_the_order(self):
        """身份一致的流水照常验证通过：修复不能把正常路径一起否掉。"""
        conn = self._conn()
        conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,qty,planned_price,status,reason,"
            "created_at,order_type) VALUES('acc-a','buy','600001',100,10.0,'filled','',"
            "?,'market')",
            (CREATED_AT,),
        )
        order_id = conn.execute("SELECT id FROM paper_orders").fetchone()[0]
        amount = 100 * 10.01
        conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,'')",
            (order_id, "acc-a", "buy", "600001", 100, 10.01, amount,
             PTR.commission(amount), SESSION, CREATED_AT),
        )
        evidence = EE.load_execution_evidence(conn, order_ids=[order_id])[0]
        self.assertTrue(evidence.proves_fill())
        self.assertEqual(EE.FILL_VERDICT_VERIFIED, evidence.fill_verdict_value())
        self.assertNotIn(
            EE.INCONSISTENCY_FILL_IDENTITY_MISMATCH, evidence.inconsistencies()
        )
        self.assertEqual(1, evidence.provenance["fill_rows"])
        self.assertEqual(0, evidence.provenance["excluded_fill_rows"])
        self.assertEqual([], evidence.provenance["fill_identity_mismatches"])

    def test_identity_is_not_compared_when_the_caller_did_not_check_it(self):
        """未核对 ≠ 发现不一致：显式声明未核对时不得凭空报违规。"""
        fill_row = dict(fill(), account_id="acc-other", side="sell", code="600002")
        evidence = EE.evidence_from_order(
            order(), [fill_row], fill_identity_known=False
        )
        self.assertNotIn(
            EE.INCONSISTENCY_FILL_IDENTITY_MISMATCH, evidence.inconsistencies()
        )
        self.assertEqual([], evidence.provenance["fill_identity_mismatches"])
        self.assertFalse(evidence.provenance["fill_identity_checked"])
        # 流水本身仍然是有效成交证据（未核对只影响"归属"，不影响"这笔流水是什么"）。
        self.assertTrue(evidence.proves_fill())

    def test_omitting_the_identity_rows_does_not_claim_they_were_checked(self):
        """没有身份证据就**不能**自称核对过。

        默认 ``fill_identity_known`` 若为 ``True``，调用方只要省略
        ``fill_identity_rows`` 就会留下 ``fill_identity_checked=True`` 而一项都没
        比对：身份完全不符的流水照样把委托验证成成交。
        """
        mismatched = dict(fill(), account_id="acc-other", side="sell", code="600002")
        evidence = EE.evidence_from_order(order(), [mismatched])
        self.assertFalse(evidence.provenance["fill_identity_checked"])
        self.assertEqual([], evidence.provenance["fill_identity_mismatches"])
        self.assertNotIn(
            EE.INCONSISTENCY_FILL_IDENTITY_MISMATCH, evidence.inconsistencies()
        )

    def test_passing_identity_rows_claims_the_check_was_made(self):
        """传了身份行才算核对过；一致时不得报违规，不一致时必须报。"""
        matching = dict(fill(), account_id="main_force_top10", side="buy", code="600001")
        checked = EE.evidence_from_order(
            order(), [matching], fill_identity_rows=[matching]
        )
        self.assertTrue(checked.provenance["fill_identity_checked"])
        self.assertEqual([], checked.provenance["fill_identity_mismatches"])
        self.assertTrue(checked.proves_fill())

        mismatched = dict(fill(), account_id="acc-other", side="sell", code="600002")
        caught = EE.evidence_from_order(
            order(), [mismatched], fill_identity_rows=[mismatched]
        )
        self.assertTrue(caught.provenance["fill_identity_checked"])
        self.assertFalse(caught.proves_fill())
        self.assertIn(
            EE.INCONSISTENCY_FILL_IDENTITY_MISMATCH, caught.inconsistencies()
        )

    def test_a_fill_row_without_identity_columns_is_not_a_mismatch(self):
        """没有可比对的证据就不判违规。"""
        evidence = EE.evidence_from_order(order(), [fill()], fill_identity_known=True)
        self.assertNotIn(
            EE.INCONSISTENCY_FILL_IDENTITY_MISMATCH, evidence.inconsistencies()
        )
        self.assertTrue(evidence.provenance["fill_identity_checked"])
        self.assertTrue(evidence.proves_fill())


class ArchitectureGuardTests(unittest.TestCase):
    """新模块必须纯 stdlib、只读、不反向依赖 paper_trading。"""

    MODULES = ("execution_lifecycle.py", "execution_evidence.py")

    def _source(self, name):
        with open(os.path.join(BACKEND_DIR, name), encoding="utf-8") as handle:
            return handle.read()

    def test_modules_do_not_import_paper_trading(self):
        for name in self.MODULES:
            source = self._source(name)
            self.assertNotIn("import paper_trading\n", source, name)
            self.assertNotIn("import paper_trading as", source, name)

    def test_modules_contain_no_write_sql(self):
        for name in self.MODULES:
            upper = self._source(name).upper()
            for keyword in ("INSERT INTO", "UPDATE PAPER_", "DELETE FROM", "DROP TABLE"):
                self.assertNotIn(keyword, upper, "%s must stay read-only" % name)

    def test_evidence_loader_only_touches_orders_and_fills(self):
        source = self._source("execution_evidence.py").upper()
        self.assertIn("FROM PAPER_ORDERS", source)
        self.assertIn("FROM PAPER_FILLS", source)
        for table in ("PAPER_POSITIONS", "PAPER_ACCOUNTS", "PAPER_CAPITAL_RESERVATIONS"):
            self.assertNotIn(table, source)


class SelfCheckTests(unittest.TestCase):
    def test_module_self_check_helper_states_the_baseline(self):
        """一条黄金基线：10.00 计划价、0.10% 滑点、万分之 0.1 佣金。"""
        evidence = EE.evidence_from_order(order(), [fill(qty=100, price=10.01)])
        self.assertEqual(EE.FILL_VERDICT_VERIFIED, evidence.fill_verdict_value())
        self.assertAlmostEqual(10.0, evidence.slippage.require(), places=6)
        self.assertAlmostEqual(
            PTR.commission(1001.0), evidence.commission.require(), places=9
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
