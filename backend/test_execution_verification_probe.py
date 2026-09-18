# -*- coding: utf-8 -*-
"""生产探针：验证闸门在**真实读路径**上的计数口径。

不测闸门函数本身（那在 ``test_execution_verification.py``），而是把三种账本
状态放进真实 schema，然后调用真实的生产读函数，断言"算没算进去"：

* 状态 A：``status='filled'`` + 有 ``paper_fills`` 流水 + ``execution_verified`` 为
  ``NULL`` → **不得计入**任何执行绩效；
* 状态 B：``status='filled'`` + 有流水 + ``execution_verified=1`` → **必须计入**；
* 状态 C：NAV 与 dashboard 的已实现盈亏必须来自同一批已验证成交。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import paper_trading as PT  # noqa: E402

ACCOUNT_ID = "trend_pullback"
CODE = "600001"
BUY_QTY = 1000
BUY_PRICE = 10.0
SELL_QTY = 1000
SELL_PRICE = 12.0
SELL_REALIZED = 1970.0  # 12000 - 6.0 - 10000 - 24.0，仅作"账面值"，不重算


class ExecutionGateProbeTestCase(unittest.TestCase):
    """真实 schema + 真实读函数的最小账本。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="astock-ev-probe-")
        self.old_db_path = PT.DB_PATH
        PT.DB_PATH = os.path.join(self.tmp_dir, "paper.sqlite3")
        # 探针只测账本读路径：把会碰行情缓存/数据源的影子全部挡住。
        # 否则 init_db → _ensure_accounts → _benchmark_close 会去读（并可能拉取）
        # 基准 K 线，既违反离线测试约定，也会污染进程内的 feed 健康注册表。
        self._benchmark_patch = mock.patch.object(PT, "_benchmark_close", return_value=None)
        self._benchmark_patch.start()
        self._session_patch = mock.patch.object(PT, "_market_session", return_value={
            "today_pnl_available": False, "label": "盘前", "code": "preopen",
        })
        self._session_patch.start()
        PT.init_db()
        self.today = dt.date.today()
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_cycles SET status='running' WHERE id=1")
            self.cycle = dict(conn.execute(
                "SELECT * FROM paper_cycles WHERE status IN ('draft','running','paused')"
                " ORDER BY id DESC LIMIT 1").fetchone())
            self.account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT_ID,)).fetchone())

    def tearDown(self):
        PT.DB_PATH = self.old_db_path
        self._session_patch.stop()
        self._benchmark_patch.stop()

    # ---------- 账本构造 ----------

    def _order(self, conn, *, side, qty, price, realized=None, verified=None):
        """写一行委托。``verified`` 为 ``None`` 表示验证列为 NULL（旧行形态）。"""
        status = "filled"
        execution_status = None if verified is None else ("verified" if verified else "unknown")
        executed_at = f"{self.today.isoformat()} 10:00:01"
        stamp = PT._strategy_stamp(conn, ACCOUNT_ID)
        cursor = conn.execute(
            """INSERT INTO paper_orders(
                   account_id,side,code,name,qty,planned_price,filled_price,amount,fees,
                   status,reason,risk_payload,realized_pnl,created_at,executed_at,
                   execution_status,execution_verified,
                   strategy_id,strategy_version,strategy_checksum,cycle_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ACCOUNT_ID, side, CODE, "探针标的", qty, price, price, qty * price,
             round(qty * price * 0.0006, 2), status, "probe", "{}", realized,
             f"{self.today.isoformat()} 10:00:00", executed_at,
             execution_status, None if verified is None else int(bool(verified)),
             *stamp, self.cycle["id"]),
        )
        return cursor.lastrowid

    def _fill(self, conn, order_id, *, side, qty, price):
        conn.execute(
            """INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,
                                       amount,fees,fill_date,quote_at,assumption)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, ACCOUNT_ID, side, CODE, qty, price, qty * price,
             round(qty * price * 0.0006, 2), self.today.isoformat(),
             f"{self.today.isoformat()} 10:00:01", "probe"),
        )

    def _lot(self, conn, qty=SELL_QTY, cost=BUY_PRICE):
        conn.execute(
            """INSERT INTO paper_position_lots(
                   cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,
                   acquired_at,available_date,asset_type,is_t_base)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
            (self.cycle["id"], ACCOUNT_ID, CODE, "探针标的", "测试", qty, qty, cost,
             (self.today - dt.timedelta(days=3)).isoformat(),
             (self.today - dt.timedelta(days=1)).isoformat(), "stock_t1"),
        )

    def _build_ledger(self, *, sell_verified):
        """买入腿 + 卖出腿；返回值决定卖出腿的验证列形态。"""
        with PT._db(immediate=True) as conn:
            buy = self._order(conn, side="buy", qty=BUY_QTY, price=BUY_PRICE,
                              verified=sell_verified)
            self._fill(conn, buy, side="buy", qty=BUY_QTY, price=BUY_PRICE)
            sell = self._order(conn, side="sell", qty=SELL_QTY, price=SELL_PRICE,
                               realized=SELL_REALIZED, verified=sell_verified)
            self._fill(conn, sell, side="sell", qty=SELL_QTY, price=SELL_PRICE)
            self._lot(conn)
        return sell

    # ---------- 读路径 ----------

    def _account_metrics(self, conn):
        """读 dashboard 口径的账户指标。

        ``allow_network=False`` 是必需的：当日成交标的没有行情时，
        ``_account_metrics`` 会去拉实时行情给"昨日收盘基准"用。探针测的是账本
        读路径，不该产生任何外部调用（离线测试约定），也避免把进程内的 feed
        健康状态弄脏影响同进程的其他用例。
        """
        return PT._account_metrics(
            conn, self.account,
            quotes={CODE: {"price": SELL_PRICE, "quote_at": f"{self.today.isoformat()} 10:00:01"}},
            positions=[], metric_cache=None, allow_network=False,
        )

    def _nav_realized(self, conn):
        """NAV 里的已实现分量：nav - 参考资金（探针账本无持仓市值）。"""
        baseline = PT._account_reference_capital(self.account)
        row = conn.execute(
            "SELECT MAX(nav) AS nav FROM paper_nav WHERE account_id=?",
            (ACCOUNT_ID,),
        ).fetchone()
        self.assertIsNotNone(row["nav"], "NAV 未被记录，探针无法比较口径")
        return round(float(row["nav"]) - baseline, 2)

    def _strategy_series_total(self, conn):
        series = PT._strategy_return_series(conn, [ACCOUNT_ID], days=30)
        return round(sum(series.get(ACCOUNT_ID) or []), 2)

    def _claims(self, conn):
        return [
            dict(row) for row in conn.execute(
                "SELECT id,side,status,execution_status,execution_verified,realized_pnl"
                " FROM paper_orders ORDER BY id")
        ]

    # ---------- 用例 ----------

    def test_case_a_claimed_fill_without_verification_is_not_counted(self):
        self._build_ledger(sell_verified=None)
        with PT._db(immediate=True) as conn:
            claims = self._claims(conn)
            self.assertEqual(len(claims), 2)
            self.assertTrue(all(row["execution_verified"] is None for row in claims),
                            claims)
            metrics = self._account_metrics(conn)
            series_total = self._strategy_series_total(conn)
            PT._record_nav(conn, self.today, quotes={})
            nav_realized = self._nav_realized(conn)
            positions = PT._position_rows(conn, ACCOUNT_ID)

        self.assertEqual(metrics["realized_pnl"], 0, metrics)
        self.assertEqual(series_total, 0, series_total)
        self.assertEqual(nav_realized, 0, nav_realized)
        # 持仓摊薄成本必须回落 lot 结算成本，而不是被"缺失的现金流行"变成 0。
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["display_cost_source"], "lot_settlement_cost")
        self.assertEqual(round(float(positions[0]["display_cost"]), 6), BUY_PRICE)

    def test_case_b_evidenced_fill_with_verification_is_counted(self):
        self._build_ledger(sell_verified=True)
        with PT._db(immediate=True) as conn:
            claims = self._claims(conn)
            self.assertTrue(all(row["execution_verified"] == 1 for row in claims), claims)
            metrics = self._account_metrics(conn)
            series_total = self._strategy_series_total(conn)
            PT._record_nav(conn, self.today, quotes={})
            nav_realized = self._nav_realized(conn)

        self.assertEqual(metrics["realized_pnl"], SELL_REALIZED, metrics)
        self.assertEqual(series_total, SELL_REALIZED, series_total)
        self.assertEqual(nav_realized, SELL_REALIZED, nav_realized)

    def test_case_c_nav_and_dashboard_use_identical_realized_pnl(self):
        """混合账本：一条已验证卖出 + 一条自称成交。NAV 与 dashboard 必须同口径。"""
        with PT._db(immediate=True) as conn:
            buy = self._order(conn, side="buy", qty=BUY_QTY, price=BUY_PRICE,
                              verified=True)
            self._fill(conn, buy, side="buy", qty=BUY_QTY, price=BUY_PRICE)
            good = self._order(conn, side="sell", qty=SELL_QTY, price=SELL_PRICE,
                               realized=SELL_REALIZED, verified=True)
            self._fill(conn, good, side="sell", qty=SELL_QTY, price=SELL_PRICE)
            bad = self._order(conn, side="sell", qty=SELL_QTY, price=SELL_PRICE,
                              realized=888888.0, verified=None)
            self._fill(conn, bad, side="sell", qty=SELL_QTY, price=SELL_PRICE)
            self._lot(conn)
        with PT._db(immediate=True) as conn:
            metrics = self._account_metrics(conn)
            series_total = self._strategy_series_total(conn)
            PT._record_nav(conn, self.today, quotes={})
            nav_realized = self._nav_realized(conn)
            # 历史口径（未闸门）会把 888888 也算进去 —— 断言闸门确实生效。
            ungated = float(conn.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) FROM paper_orders"
                " WHERE side='sell' AND status='filled'").fetchone()[0])

        self.assertEqual(metrics["realized_pnl"], SELL_REALIZED, metrics)
        self.assertEqual(nav_realized, metrics["realized_pnl"],
                         "NAV 与 dashboard 的已实现盈亏必须同口径")
        self.assertEqual(series_total, metrics["realized_pnl"],
                         "策略绩效序列与 dashboard 必须同口径")
        self.assertGreater(ungated, metrics["realized_pnl"],
                           "未闸门口径应显著更大，否则用例没有区分度")


if __name__ == "__main__":
    unittest.main()
