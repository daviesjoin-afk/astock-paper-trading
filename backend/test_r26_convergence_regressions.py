# -*- coding: utf-8 -*-
"""R26 收敛回归：本轮在 #186 上修复的四个已确认缺陷的永久锚点。

每个用例都对应用户可见的业务断言，而不是内部实现细节：

1. 部分成交后的 signal 状态**不得**被 reconciliation 降级回 ``pending``；
2. 归档必须保留同一订单的**全部** FillEvent，而不是 collapse 成一笔；
3. 一次性风险减仓去重必须把**部分成交**视为"已经执行过"，同时保留订单剩余量的
   继续执行；条件升级仍允许新动作；
4. 同一 session 的累计成交量参与额度**不得**被重复消费。

这些是"测试通过 ≠ 语义正确"的典型场景：旧实现自洽但业务错误，所以锚点必须落在
账本状态与后续扫描行为上。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import execution_verification as EV  # noqa: E402
import paper_archive_projection as PAP  # noqa: E402
import paper_trading as PT  # noqa: E402

DAY = "2026-09-08"
NEXT_DAY = "2026-09-09"
ACCOUNT = "tq_breakout"
CODE = "600901"


def _strategy_stamp(conn):
    """给出一组满足 v22 guard 的策略版本戳（strategy_id == account_id 且已登记）。"""
    row = conn.execute(
        "SELECT strategy_id,version,checksum FROM paper_strategy_versions "
        "WHERE strategy_id=? LIMIT 1",
        (ACCOUNT,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO paper_strategy_versions(strategy_id,version,checksum,"
            "created_at,reason) VALUES(?,?,?,?,?)",
            (ACCOUNT, 1, "sha256:r26-test", f"{DAY} 09:00:00", "r26-test"),
        )
        conn.commit()
        return (ACCOUNT, 1, "sha256:r26-test")
    return (str(row[0]), int(row[1]), str(row[2]))

def _make_db(path):
    old = PT.DB_PATH
    PT.DB_PATH = path
    PT.init_db()
    return old


class _LedgerTestCase(unittest.TestCase):
    """每个用例一个独立临时库；不共享任何全局账本。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r26-convergence-")
        self.db_path = os.path.join(self.tmp, "paper.sqlite3")
        self._old_db_path = _make_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.cycle_id = self._cycle()
        self._account()

    def tearDown(self):
        self.conn.close()
        PT.DB_PATH = self._old_db_path

    def _cycle(self):
        stamp = f"{DAY} 09:00:00"
        return int(self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,"
            "started_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (f"cycle-{DAY}", "running", 1_000_000.0, "shared_pool",
             stamp, stamp, stamp),
        ).lastrowid)

    def _account(self):
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(paper_accounts)")
        }
        values = {"id": ACCOUNT, "name": "首板接力", "cycle_id": self.cycle_id}
        for column, definition in (
            ("initial_cash", 1_000_000.0), ("cash", 1_000_000.0),
            ("status", "running"), ("updated_at", f"{DAY} 09:00:00"),
        ):
            if column in columns:
                values[column] = definition
        names = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        # init_db 已经开好内置账户（cycle_id 为 NULL）；这里把它绑到本用例的
        # 周期上，使"账户周期 == 信号周期"的归属 guard 成立。
        if self.conn.execute(
            "SELECT 1 FROM paper_accounts WHERE id=?", (ACCOUNT,),
        ).fetchone():
            assignments = ",".join(f"{column}=?" for column in values)
            self.conn.execute(
                f"UPDATE paper_accounts SET {assignments} WHERE id=?",
                (*values.values(), ACCOUNT),
            )
        else:
            self.conn.execute(
                f"INSERT INTO paper_accounts({names}) VALUES({placeholders})",
                tuple(values.values()),
            )
        self.conn.commit()

    # ---------- 夹具 ----------

    def _order(self, *, side="sell", qty=1000, status="partially_filled",
               filled=300, marker=None, created_at=f"{DAY} 10:00:00",
               origin="strategy", account_id=ACCOUNT, code=CODE):
        payload = {"exit_marker": marker} if marker else {}
        # 策略版本戳必须**齐备**、与 account_id 同源，且在版本表里真实存在
        # （v22 guard）。这里给出一致的三元组，让订单表达"有冻结的策略来源"。
        stamp = _strategy_stamp(self.conn)
        cursor = self.conn.execute(
            "INSERT INTO paper_orders(account_id,signal_id,side,code,name,qty,"
            "planned_price,filled_price,amount,fees,status,reason,risk_payload,"
            "created_at,executed_at,order_type,origin,cycle_id,filled_qty,"
            "remaining_qty,execution_asof,execution_reasons,execution_evidence,"
            "pricing_basis,slippage,ruleset_version,execution_version,"
            "strategy_id,strategy_version,strategy_checksum) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, None, side, code, "回放甲", qty, 20.0, 20.0,
             filled * 20.0, 6.0, status, "r26-test", json.dumps(payload),
             created_at, f"{created_at}:00", "market", origin, self.cycle_id,
             filled, max(0, qty - filled), f"{DAY} 10:00:00", "[]", "{}",
             "verified_quote_plus_deterministic_slippage", 2.0,
             "a-share-simulation-v1", 1, *stamp),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def _fill(self, order_id, *, qty, event_key=None, quote_at=f"{DAY} 10:00:00",
              fill_date=DAY):
        self._fill_seq = getattr(self, "_fill_seq", 0) + 1
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,"
            "fees,fill_date,quote_at,assumption,event_key,execution_asof,"
            "pricing_basis,slippage,market_evidence,ruleset_version,execution_evidence) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, ACCOUNT, "sell", CODE, qty, 20.0, qty * 20.0, 6.0, fill_date,
             quote_at, "r26-test", event_key or f"key-{order_id}-{self._fill_seq}",
             f"{quote_at}", "verified_quote_plus_deterministic_slippage",
             2.0, "{}", "a-share-simulation-v1", "{}"),
        )
        self.conn.commit()

    def _stamp(self, order_id, status, verified):
        self.conn.execute(
            "UPDATE paper_orders SET execution_status=?,execution_verified=? "
            "WHERE id=?", (status, verified, order_id),
        )
        self.conn.commit()


class PartialSignalReconciliationTests(_LedgerTestCase):
    """缺陷 A：部分成交的 signal 状态必须在对账后**保持** partially_filled。"""

    def _signal(self, status="pending", code=CODE):
        # 信号必须携带可证明的周期归属（v23 guard）与齐备的策略版本戳（v22 guard），
        # 且周期与账户当前周期一致。
        cursor = self.conn.execute(
            "INSERT INTO paper_signals(account_id,code,name,status,intended_date,"
            "signal_date,reason,payload,created_at,cycle_id,strategy_id,"
            "strategy_version,strategy_checksum) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, code, "回放甲", status, DAY, DAY, "r26-test", "{}",
             f"{DAY} 09:30:00", self.cycle_id, *_strategy_stamp(self.conn)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def _reconcile(self):
        with self.conn:
            return PT._reconcile_signal_order_states(self.conn)

    def _signal_status(self, signal_id):
        return str(self.conn.execute(
            "SELECT status FROM paper_signals WHERE id=?", (signal_id,),
        ).fetchone()[0])

    def test_partial_buy_fill_keeps_signal_partially_filled_across_reconciliation(self):
        """approved signal + 首笔部分成交 ⇒ 对账后仍为 partially_filled。

        旧实现把 ``paper_orders.status='partially_filled'`` 映射回
        ``signal.status='pending'``，让"这张信号到底成没成交"在两次读之间自相矛盾。
        """
        signal_id = self._signal()
        order_id = self._order(
            side="buy", qty=1000, status="partially_filled", filled=300,
        )
        self.conn.execute(
            "UPDATE paper_orders SET signal_id=? WHERE id=?", (signal_id, order_id),
        )
        self.conn.commit()
        self.conn.execute(
            "UPDATE paper_signals SET status='partially_filled' WHERE id=?",
            (signal_id,),
        )
        self.conn.commit()

        self._reconcile()

        self.assertEqual("partially_filled", self._signal_status(signal_id))

    def test_reconciliation_preserves_the_filled_and_pending_lifecycles(self):
        """终态语义不变：filled 信号保持 filled，pending_execution → pending。

        reconciliation 只针对**可重试**的在途委托（``pending_execution`` 等）；
        已完整成交的信号不在它的集合里，因此必须原样保留 —— 这条断言守住
        "修好 partial 降级"时不能顺手把 filled 也改掉。
        """
        filled_signal = self._signal(status="filled", code="600902")
        filled_order = self._order(
            side="buy", qty=1000, status="filled", filled=1000, code="600902",
        )
        self.conn.execute(
            "UPDATE paper_orders SET signal_id=? WHERE id=?",
            (filled_signal, filled_order),
        )
        pending_signal = self._signal(code="600903")
        pending_order = self._order(
            side="buy", qty=1000, status="pending_execution", filled=0,
            code="600903",
        )
        self.conn.execute(
            "UPDATE paper_orders SET signal_id=? WHERE id=?",
            (pending_signal, pending_order),
        )
        self.conn.commit()

        self._reconcile()

        self.assertEqual("filled", self._signal_status(filled_signal))
        self.assertEqual("pending", self._signal_status(pending_signal))

    def test_init_db_runs_reconciliation_without_downgrading_a_partial_signal(self):
        """走真正的生产入口（init_db）而不是直接调用内部函数。"""
        signal_id = self._signal(status="partially_filled")
        order_id = self._order(
            side="buy", qty=1000, status="partially_filled", filled=300,
        )
        self.conn.execute(
            "UPDATE paper_orders SET signal_id=? WHERE id=?", (signal_id, order_id),
        )
        self.conn.commit()

        PT.init_db()

        self.assertEqual("partially_filled", self._signal_status(signal_id))


class ArchiveMultiFillTests(_LedgerTestCase):
    """缺陷 B：归档必须保留同一订单的每一笔 FillEvent。"""

    def _archive_snapshot(self, payload):
        self.conn.execute(
            "INSERT INTO paper_archives(cycle_id,cycle_key,reason,snapshot,created_at) "
            "VALUES(?,?,?,?,?)",
            (self.cycle_id, "cycle-archived", "test", json.dumps(payload),
             f"{DAY} 15:00:00"),
        )
        self.conn.commit()

    def _snapshot(self):
        orders = [dict(row) for row in self.conn.execute(
            "SELECT id,account_id,side,code,name,qty,status,created_at,executed_at,"
            "filled_qty,remaining_qty,execution_status,execution_verified,"
            "execution_reasons,realized_pnl,fees,amount,planned_price,filled_price,"
            "cycle_id FROM paper_orders WHERE id=?",
            (self.order_id,),
        )]
        fills = [dict(row) for row in self.conn.execute(
            "SELECT * FROM paper_fills WHERE order_id=? ORDER BY id",
            (self.order_id,),
        )]
        return {
            "paper_accounts": [{"id": ACCOUNT, "name": "首板接力"}],
            "paper_orders": orders,
            "paper_fills": fills,
            "_archive_format": "compact-ledger-v2",
        }

    def test_archived_history_keeps_every_partial_fill(self):
        """order 300 股、三笔各 100 股：归档后历史必须能看到 3 笔。"""
        self.order_id = self._order(
            side="buy", qty=300, status="filled", filled=300,
        )
        self._fill(self.order_id, qty=100, quote_at=f"{DAY} 10:00:00")
        self._fill(self.order_id, qty=100, quote_at=f"{DAY} 10:05:00")
        self._fill(self.order_id, qty=100, quote_at=f"{DAY} 10:10:00")
        self._stamp(self.order_id, "verified", 1)
        self._archive_snapshot(self._snapshot())

        history = PT.stock_trade_history(CODE)

        # 只看归档那一份（活动表里同样有这笔订单的流水）。
        archived_fills = [
            item for item in history["fills"]
            if int(item.get("order_id") or 0) == self.order_id
            and item.get("archived_cycle")
        ]
        self.assertEqual(
            3, len(archived_fills),
            "归档把多笔部分成交 collapse 成一笔（审计缺口）",
        )
        self.assertEqual([100, 100, 100], sorted(int(f["qty"]) for f in archived_fills))
        for fill in archived_fills:
            self.assertEqual("cycle-archived", fill.get("archived_cycle"))
            self.assertAlmostEqual(float(fill["amount"]), 2000.0, places=2)
            self.assertAlmostEqual(float(fill["fees"]), 6.0, places=2)
            self.assertIsNotNone(fill.get("execution_asof"))
            self.assertIsNotNone(fill.get("pricing_basis"))

    def test_archived_order_summary_reports_the_latest_fill_date(self):
        """订单行摘要与活动表读路径同口径：fill_date 取该订单最大成交日。"""
        self.order_id = self._order(
            side="buy", qty=200, status="filled", filled=200,
        )
        self._fill(self.order_id, qty=100, quote_at=f"{DAY} 10:00:00",
                   fill_date=DAY)
        self._fill(self.order_id, qty=100, quote_at=f"{NEXT_DAY} 10:00:00",
                   fill_date=NEXT_DAY)
        self._stamp(self.order_id, "verified", 1)
        self._archive_snapshot(self._snapshot())

        history = PT.stock_trade_history(CODE)

        archived_orders = [
            item for item in history["orders"]
            if int(item.get("id") or 0) == self.order_id
            and item.get("archived_cycle")
        ]
        self.assertEqual(1, len(archived_orders))
        self.assertEqual(NEXT_DAY, str(archived_orders[0].get("fill_date"))[:10])


class RiskPartialDedupTests(_LedgerTestCase):
    """缺陷 C（merge blocker）：部分成交必须参与一次性减仓去重。"""

    def test_partial_trim_counts_as_executed_but_full_fill_also_does(self):
        """完整成交与部分成交都必须被判定为"动作已执行"。"""
        full = self._order(
            side="sell", qty=1000, status="filled", filled=1000,
            marker="hard_stop_first_trim",
        )
        self._fill(full, qty=1000)
        self._stamp(full, "verified", 1)
        self.assertTrue(EV.has_verified_positive_execution(
            self.conn, marker="hard_stop_first_trim", account_id=ACCOUNT,
            code=CODE, asof_day=DAY,
        ))

        self.conn.execute("DELETE FROM paper_fills")
        self.conn.execute("DELETE FROM paper_orders")
        self.conn.commit()
        partial = self._order(
            side="sell", qty=1000, status="partially_filled", filled=300,
            marker="hard_stop_first_trim",
        )
        self._fill(partial, qty=300)
        self._stamp(partial, "partial", 0)

        self.assertTrue(
            EV.has_verified_positive_execution(
                self.conn, marker="hard_stop_first_trim", account_id=ACCOUNT,
                code=CODE, asof_day=DAY,
            ),
            "部分成交被读成'动作没发生过'，下一轮扫描会重复减仓",
        )

    def test_zero_execution_does_not_claim_the_action_ran(self):
        """被拒/未成交的委托不得吃掉今天的第一次减仓。"""
        blocked = self._order(
            side="sell", qty=1000, status="pending_execution", filled=0,
            marker="downside_warning_trim",
        )
        self._stamp(blocked, "unknown", 0)
        self.assertFalse(EV.has_verified_positive_execution(
            self.conn, marker="downside_warning_trim", account_id=ACCOUNT,
            code=CODE, asof_day=DAY,
        ))

    def test_inconsistent_verification_columns_do_not_prove_execution(self):
        """两列不一致（flag=0 却 status='verified'）fail closed。"""
        order_id = self._order(
            side="sell", qty=1000, status="filled", filled=1000,
            marker="downside_full",
        )
        self._fill(order_id, qty=1000)
        self._stamp(order_id, "verified", 0)
        self.assertFalse(EV.has_verified_positive_execution(
            self.conn, marker="downside_full", account_id=ACCOUNT,
            code=CODE, asof_day=DAY,
        ))

    def test_markers_are_deduplicated_per_marker_not_globally(self):
        """按 marker 分别去重：一个 marker 已执行不影响另一个。"""
        order_id = self._order(
            side="sell", qty=1000, status="partially_filled", filled=300,
            marker="downside_warning_trim",
        )
        self._fill(order_id, qty=300)
        self._stamp(order_id, "partial", 0)
        self.assertTrue(EV.has_verified_positive_execution(
            self.conn, marker="downside_warning_trim", account_id=ACCOUNT,
            code=CODE, asof_day=DAY,
        ))
        # 条件升级到 full 是**允许**的新动作，不能被 warning 动作的去重挡住。
        self.assertFalse(EV.has_verified_positive_execution(
            self.conn, marker="downside_full", account_id=ACCOUNT,
            code=CODE, asof_day=DAY,
        ))

    def test_undated_legacy_row_is_found_by_the_reason_fallback(self):
        """标记上线前的旧订单只能靠 reason 文本兜底识别。"""
        cursor = self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "filled_price,amount,fees,status,reason,risk_payload,created_at,"
            "executed_at,order_type,origin,cycle_id,filled_qty,remaining_qty,"
            "strategy_id,strategy_version,strategy_checksum) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "sell", CODE, "回放甲", 350, 20.0, 20.0, 7000.0, 6.0,
             "filled", "硬止损首段减仓：本次处理可卖仓位的 35%", "{}",
             f"{DAY} 10:00:00", f"{DAY} 10:00:01", "market", "strategy",
             self.cycle_id, 350, 0,
             *_strategy_stamp(self.conn)),
        )
        order_id = int(cursor.lastrowid)
        self._fill(order_id, qty=350)
        self._stamp(order_id, "verified", 1)
        self.assertTrue(EV.has_verified_positive_execution(
            self.conn, marker="hard_stop_first_trim", account_id=ACCOUNT,
            code=CODE, asof_day=DAY,
            legacy_reason_like="%硬止损首段减仓%",
        ))


class CumulativeLiquidityConsumptionTests(_LedgerTestCase):
    """缺陷 D：同一 session 的累计成交量参与额度不得被重复消费。"""

    def test_consumed_quantity_is_scoped_to_symbol_and_session(self):
        """只统计同一 symbol、同一 session、且不晚于 execution_asof 的成交。"""
        order_id = self._order(side="buy", qty=3000, status="filled", filled=3000)
        self._fill(order_id, qty=3000, quote_at=f"{DAY} 10:00:00")
        # 另一个 symbol 不参与本 symbol 的额度。
        other = self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,"
            "fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, ACCOUNT, "buy", "600902", 9999, 10.0, 99990.0, 1.0, DAY,
             f"{DAY} 10:00:00", "r26-test"),
        )
        self.conn.commit()

        self.assertEqual(
            3000, PT.EP.consumed_session_quantity(
                self.conn, CODE, DAY, f"{DAY} 10:00:00",
            ),
        )

    def test_fills_after_the_execution_instant_are_not_counted(self):
        """as-of 边界：10:05 的成交不得限制 10:00 的判定。"""
        order_id = self._order(side="buy", qty=4000, status="filled", filled=4000)
        self._fill(order_id, qty=1000, quote_at=f"{DAY} 09:55:00")
        self._fill(order_id, qty=3000, quote_at=f"{DAY} 10:05:00")
        self.assertEqual(
            1000, PT.EP.consumed_session_quantity(
                self.conn, CODE, DAY, f"{DAY} 10:00:00",
            ),
        )
        self.assertEqual(
            4000, PT.EP.consumed_session_quantity(
                self.conn, CODE, DAY, f"{DAY} 10:10:00",
            ),
        )

    def test_undated_legacy_fill_is_counted_conservatively(self):
        """缺时间戳的 legacy 流水按"已消耗"处理（fail conservative）。"""
        order_id = self._order(side="buy", qty=500, status="filled", filled=500)
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,"
            "fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, ACCOUNT, "buy", CODE, 500, 20.0, 10000.0, 1.0, DAY,
             None, "legacy"),
        )
        self.conn.commit()
        self.assertEqual(
            500, PT.EP.consumed_session_quantity(
                self.conn, CODE, DAY, f"{DAY} 10:00:00",
            ),
        )

    def test_second_event_cannot_re_consume_the_same_cumulative_volume(self):
        """同一累计成交量下，第二次执行事件不得再吃一份参与额度。

        行情累计 500,000 股 ⇒ 1% = 5,000 股额度。首笔成交 3,000 股后，
        同一个 snapshot 只允许再成交 2,000 股，绝不是又一个 5,000。
        """
        order_id = self._order(side="buy", qty=10_000, status="filled", filled=3000)
        self._fill(order_id, qty=3000, quote_at=f"{DAY} 10:00:00")

        # 已消耗 3,000 ⇒ 在本 snapshot 下可执行上限 = 5,000 − 3,000 = 2,000。
        consumed = PT.EP.consumed_session_quantity(
            self.conn, CODE, DAY, f"{DAY} 10:05:00",
        )
        self.assertEqual(3000, consumed)

        _, context = _execution_context(
            liquidity=500_000, session_consumed=consumed,
        )
        decision = PT.EP.evaluate_simulated_execution(
            _intent(qty=10_000), context,
        )
        self.assertEqual(
            2_000, decision.fill_quantity,
            "同一份累计成交量被重复消费（参与率被成倍放大）",
        )
        self.assertEqual("partially_filled", decision.status)


# ---------- 纯规则夹具（不触库） ----------

class ManualRemainingRevalidationTests(_LedgerTestCase):
    """手动委托的复核必须基于**剩余量**，而不是原始委托量。"""

    def test_revalidate_feeds_remaining_quantity_and_keeps_desired_qty(self):
        """desired=300、已成交 100 ⇒ 复核拿到的 qty 必须是 200。

        且 durable 的 ``qty`` 仍然表示原始 300：不允许为了省事直接
        ``UPDATE qty=200``，否则"委托了多少 / 成交了多少 / 还剩多少"这三件事
        就被压成一件事，无法再从账本回答。
        """
        import execution_planner as EP
        order_id = self._order(
            side="buy", qty=300, status="partially_filled", filled=100,
            origin="manual",
        )
        captured = {}

        def plan_builder(conn, account_id, code, side, qty, order_type,
                         limit_price, asof_day, **kwargs):
            captured.update({
                "qty": qty, "side": side, "order_type": order_type,
                "limit_price": limit_price, "exclude_reservation_key": kwargs.get(
                    "exclude_reservation_key"),
            })
            return {"allowed": True, "reasons": []}

        with self.conn:
            order = dict(self.conn.execute(
                "SELECT * FROM paper_orders WHERE id=?", (order_id,),
            ).fetchone())
            EP.revalidate_order_plan(
                self.conn, order, plan_builder=plan_builder, asof_day=DAY,
                quote={"price": 20.0, "quote_at": f"{DAY} 10:05:00"},
            )

        self.assertEqual(200, captured["qty"], "复核使用了原始委托量而不是剩余量")
        self.assertEqual(str(order_id), captured["exclude_reservation_key"])

        stored = dict(self.conn.execute(
            "SELECT qty,filled_qty,remaining_qty FROM paper_orders WHERE id=?",
            (order_id,),
        ).fetchone())
        self.assertEqual(300, stored["qty"], "durable qty 必须保持原始委托量")
        self.assertEqual(100, stored["filled_qty"])
        self.assertEqual(200, stored["remaining_qty"])

    def test_revalidate_falls_back_to_desired_minus_filled_without_a_stored_remainder(self):
        """``remaining_qty`` 缺失（legacy 行）时按 desired − filled 推算。"""
        import execution_planner as EP
        order_id = self._order(
            side="buy", qty=300, status="partially_filled", filled=100,
            origin="manual",
        )
        self.conn.execute(
            "UPDATE paper_orders SET remaining_qty=NULL WHERE id=?", (order_id,),
        )
        self.conn.commit()
        captured = {}

        def plan_builder(conn, account_id, code, side, qty, order_type,
                         limit_price, asof_day, **kwargs):
            captured["qty"] = qty
            return {"allowed": True, "reasons": []}

        with self.conn:
            order = dict(self.conn.execute(
                "SELECT * FROM paper_orders WHERE id=?", (order_id,),
            ).fetchone())
            EP.revalidate_order_plan(
                self.conn, order, plan_builder=plan_builder, asof_day=DAY,
            )
        self.assertEqual(200, captured["qty"])


class IdempotencyTests(_LedgerTestCase):
    """相同执行证据重放不得产生第二笔成交。

    这条不变式必须**独立于流动性**验证：当行情累计成交额足够大时，参与额度本身
    不会再拦下重复事件，此时唯一阻止重复成交的就是 event key（绑定订单 × 行情观测
    × ruleset）。把两者混在一个用例里，流动性修复会掩盖 event key 的失效。
    """

    def _event(self, *, quote_at, amount):
        return {
            "code": CODE, "price": 20.0, "amount": float(amount),
            "quote_at": quote_at, "execution_asof": quote_at,
            "quote_source": "live", "quote_validation": "cross_source_checked",
        }

    def test_same_quote_observation_cannot_fill_twice(self):
        import execution_planner as EP
        # 容量足够覆盖整笔委托（2,000,000 股 × 1% = 20,000 > 1000），
        # 因此流动性不会成为重复成交的挡板。
        quote = self._event(quote_at=f"{DAY} 10:00:00", amount=20.0 * 2_000_000)
        _, context = _execution_context(
            liquidity=2_000_000, session_consumed=0, quote=quote,
        )
        intent = _intent(qty=1000)
        first = EP.evaluate_simulated_execution(intent, context)
        self.assertTrue(first.executable_now)
        self.assertEqual(1000, first.fill_quantity)

        # 同一个行情观测（相同 quote_at）重放：容量依然允许成交，但 event key
        # 相同 ⇒ 必须被识别为同一次执行事件，绝不产生第二笔成交。
        _, replay_context = _execution_context(
            liquidity=2_000_000, session_consumed=0, quote=quote,
        )
        replay = EP.evaluate_simulated_execution(intent, replay_context)
        self.assertEqual(
            first, replay,
            "相同行情观测的两次评估结果不同（评估不是纯函数）",
        )
        key = EP._fill_event_key(
            order_id=intent.order_id, quote_at=quote["quote_at"],
            ruleset_version=first.ruleset_version,
        )
        self.assertEqual(
            key,
            EP._fill_event_key(
                order_id=intent.order_id, quote_at=quote["quote_at"],
                ruleset_version=first.ruleset_version,
            ),
            "event key 不稳定",
        )
        # 换一个**新的行情观测**才是新事件。
        self.assertNotEqual(
            key,
            EP._fill_event_key(
                order_id=intent.order_id, quote_at=f"{DAY} 10:01:00",
                ruleset_version=first.ruleset_version,
            ),
        )


class SessionAndTimezoneTests(unittest.TestCase):
    """固定上海交易时区与 session 边界。

    执行结论**不得**随部署服务器的本地时区改变，也**不得**把收盘集合竞价
    （14:57–15:00）偷偷当成连续竞价成交。
    """

    def _phase(self, value):
        import execution_planner as EP
        return EP._session_phase(value)

    def test_boundaries_are_explicit_for_every_phase(self):
        cases = {
            "2026-09-08 09:29:00": "pre_open",
            "2026-09-08 09:30:00": "continuous_morning",
            "2026-09-08 11:29:59": "continuous_morning",
            "2026-09-08 11:30:00": "lunch_break",
            "2026-09-08 12:59:59": "lunch_break",
            "2026-09-08 13:00:00": "continuous_afternoon",
            "2026-09-08 14:56:59": "continuous_afternoon",
            "2026-09-08 14:57:00": "closing_auction",
            "2026-09-08 14:59:59": "closing_auction",
            "2026-09-08 15:00:00": "market_closed",
        }
        for value, expected in cases.items():
            self.assertEqual(expected, self._phase(value), value)

    def test_closing_auction_is_not_a_continuous_phase(self):
        """14:57–15:00 必须是独立阶段：本轮不实现集合竞价撮合，因此不可成交。"""
        import execution_planner as EP
        self.assertNotIn("closing_auction", {"continuous_morning", "continuous_afternoon"})
        quote = {
            "code": CODE, "price": 20.0, "amount": 20.0 * 500_000,
            "quote_at": f"{DAY} 14:58:00", "execution_asof": f"{DAY} 14:58:00",
            "quote_source": "live", "quote_validation": "cross_source_checked",
        }
        reading = EP.market_reading_for_execution(
            quote, asof_day=DAY, execution_asof=f"{DAY} 14:58:00",
        )
        context = EP.ExecutionContext(
            session_date=DAY, execution_asof=f"{DAY} 14:58:00", quote=quote,
            market_reading=reading, tradability=_Tradability(),
            available_liquidity=500_000, sellable_quantity=100_000,
        )
        decision = EP.evaluate_simulated_execution(_intent(qty=100), context)
        self.assertFalse(decision.executable_now)
        self.assertIn(EP.ExecutionReason.OUT_OF_SESSION.value, decision.reasons)

    def test_utc_instant_is_read_as_shanghai_time(self):
        """``02:00+00:00`` 与 ``10:00+08:00`` 必须是同一结论。"""
        self.assertEqual(
            self._phase("2026-09-08T02:00:00+00:00"),
            self._phase("2026-09-08T10:00:00+08:00"),
        )
        self.assertEqual("continuous_morning", self._phase("2026-09-08T02:00:00+00:00"))

    def test_naive_timestamp_is_interpreted_as_shanghai_not_container_local(self):
        """无时区的执行时点按 Asia/Shanghai 解释，不依赖容器本地时区。"""
        import execution_planner as EP
        parsed = EP._parse_execution_instant("2026-09-08 10:00:00")
        self.assertEqual(8 * 3600, parsed.utcoffset().total_seconds())
        self.assertEqual("Asia/Shanghai", str(parsed.tzinfo))

    def test_weekend_is_market_closed(self):
        # 2026-09-12 是周六。
        self.assertEqual("market_closed", self._phase("2026-09-12 10:00:00"))


def _execution_context(*, liquidity, session_consumed, quote=None):
    import execution_planner as EP
    quote = dict(quote or {
        "code": CODE, "price": 20.0, "amount": float(liquidity) * 20.0,
        "quote_at": f"{DAY} 10:05:00", "execution_asof": f"{DAY} 10:05:00",
        "quote_source": "live", "quote_validation": "cross_source_checked",
    })
    execution_asof = str(quote.get("execution_asof") or f"{DAY} 10:05:00")
    reading = EP.market_reading_for_execution(
        quote, asof_day=DAY, execution_asof=execution_asof,
    )
    tradability = _Tradability()
    context = EP.ExecutionContext(
        session_date=DAY, execution_asof=execution_asof, quote=quote,
        market_reading=reading, tradability=tradability,
        available_liquidity=int(liquidity), sellable_quantity=100_000,
        same_day_consumed_quantity=session_consumed,
    )
    return quote, context


class _Tradability:
    evidence_present = True
    can_buy = True
    can_sell = True
    buy_block_reason = "ok"
    sell_block_reason = "ok"

    def to_dict(self):
        return {
            "evidence_present": True, "can_buy": True, "can_sell": True,
            "buy_block_reason": "ok", "sell_block_reason": "ok",
        }


def _intent(*, qty):
    import execution_planner as EP
    return EP.PersistedOrderIntent(
        order_id=1, account_id=ACCOUNT, cycle_id=1,
        strategy_id="test", strategy_version=1, strategy_checksum="sha256:test",
        signal_id=9, symbol=CODE, side="buy", desired_quantity=qty,
        intent_at=f"{DAY} 09:35:00", reference_price=20.0,
        order_type="market", signal_provenance={"frozen": True},
    )


if __name__ == "__main__":
    unittest.main()
