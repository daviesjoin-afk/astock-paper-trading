import ast
import dataclasses
import datetime as dt
import functools
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch

import paper_account_metrics as metrics
import paper_performance as performance
import paper_repository as repository
import paper_trading as trading
from paper_numeric import num


class AccountMetricModuleTests(unittest.TestCase):
    def test_facade_is_bound_to_extracted_read_model(self):
        self.assertIs(metrics.account_metrics, trading._account_metrics)

    def test_module_uses_ports_not_the_trading_facade(self):
        tree = ast.parse(Path(metrics.__file__).read_text(encoding="utf-8"))
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                   for alias in node.names}
        self.assertNotIn("paper_trading", imports)
        self.assertIn("AccountMetricDependencies", {
            node.name for node in tree.body if isinstance(node, ast.ClassDef)})

    def test_unbound_module_fails_explicitly(self):
        factory = metrics._dependencies
        metrics._dependencies = None
        try:
            with self.assertRaisesRegex(RuntimeError, "dependencies are not bound"):
                metrics._ports()
        finally:
            metrics._dependencies = factory


class AccountMetricLedgerTests(unittest.TestCase):
    """固定账本检验口径，预期值不依赖另一份生产实现。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE paper_orders(
                id INTEGER PRIMARY KEY, account_id TEXT, code TEXT, qty INTEGER,
                filled_price REAL, amount REAL, fees REAL, status TEXT, side TEXT,
                created_at TEXT, executed_at TEXT, realized_pnl REAL,
                execution_status TEXT, execution_verified INTEGER, cycle_id TEXT
            );
            CREATE TABLE paper_fills(account_id TEXT, side TEXT);
            CREATE TABLE paper_nav(account_id TEXT, nav_date TEXT, nav REAL,
                benchmark REAL, created_at TEXT, quote_status TEXT);
            INSERT INTO paper_orders VALUES
                (1,'acct','000002',100,11,1100,5,'filled','sell',
                 '2026-01-02 10:00','2026-01-02 10:00',95,'verified',1,'current'),
                (2,'acct','000003',100,10,1000,5,'filled','sell',
                 '2026-01-02 10:00','2026-01-02 10:00',-900,'unknown',0,'current'),
                (3,'acct','000004',100,10,1000,5,'rejected','buy',
                 '2026-01-02 10:00',NULL,NULL,NULL,NULL,'current');
            INSERT INTO paper_fills VALUES('acct','buy'),('acct','buy');
            INSERT INTO paper_nav VALUES
                ('acct','2025-12-31',10000,1000,'2025-12-31','verified'),
                ('acct','2026-01-01',11000,1050,'2026-01-01','verified'),
                ('acct','2026-01-02',10500,1100,'2026-01-02','verified');
        """)
        self.account = {
            "id": "acct", "name": "fixture", "status": "running", "cycle_days": 1,
            "mode": "test", "style": "strong", "risk_profile": "breakout",
            "version": 1, "params": "{}", "initial_cash": 10000.0,
            "cash": 8000.0, "benchmark_start": 1000.0,
        }
        self.positions = [{"code": "000001", "qty": 100, "cost": 10,
                           "today_acquired_qty": 0, "today_acquired_cost": 0}]
        self.quotes = {
            "000001": {"price": 12, "pct": 20, "previous_close": 10,
                       "quote_source": "live", "quote_at": "2026-01-02T10:00:00+08:00"},
            "000002": {"price": 11, "pct": 10, "previous_close": 10,
                       "quote_source": "live", "quote_at": "2026-01-02T10:00:00+08:00"},
        }
        session = lambda: {"today_pnl_available": True, "code": "trading", "label": "trading"}
        date_fn = lambda value: dt.date.fromisoformat(str(value)[:10])
        profile = {"max_weight": .3, "max_exposure": .8, "max_industry": .4,
                   "single_risk": .01, "daily_loss": .05, "drawdown": .1}

        def unexpected_network(_codes):
            self.fail("account projection unexpectedly requested quotes")

        ports = metrics.AccountMetricDependencies(
            account_specs={}, today=lambda: "2026-01-02",
            account_metric_inputs=repository.account_metric_inputs,
            account_reference_capital=lambda account: account["initial_cash"],
            market_session=session, position_rows=lambda *_: self.positions,
            quotes=unexpected_network, risk_profile=lambda *_args, **_kwargs: profile,
            today_position_performance=functools.partial(
                performance.position_performance, date_fn=date_fn, market_session=session, num=num),
            today_sell_performance=functools.partial(
                performance.sell_performance, date_fn=date_fn, num=num),
        )
        dependency_patch = patch.object(metrics, "_dependencies", lambda: ports)
        dependency_patch.start()
        self.addCleanup(dependency_patch.stop)

    def project(self, *, quotes=None, cached=False):
        cache = repository.account_metric_inputs(self.conn, ["acct"], "2026-01-02") if cached else None
        return metrics.account_metrics(
            self.conn, self.account, quotes=self.quotes if quotes is None else quotes,
            positions=self.positions, metric_cache=cache, allow_network=False,
        )

    def test_nonempty_ledger_has_fixed_cash_pnl_and_daily_values(self):
        # 已实现 95 = 1100 卖出所得 - 5 费用 - 1000 成本。
        # 持仓浮盈 200 = (12 - 10) * 100；共享现金桶不计作策略收益。
        expected = {
            "cash": 8000.0, "initial_cash": 10000.0, "nav": 10295.0,
            "realized_pnl": 95.0, "unrealized_pnl": 200.0, "total_pnl": 295.0,
            "position_value": 1200.0, "position_cost_value": 1000.0,
            "holding_return_pct": 20.0, "return_pct": 2.95,
            "max_drawdown_pct": -6.41, "benchmark_return_pct": 10.0,
            "excess_return_pct": -7.05, "trade_count": 3, "risk_blocks": 1,
            "win_rate_pct": 100.0, "today_open_position_pnl": 200.0,
            "today_sell_pnl": 95.0, "today_pnl": 295.0, "today_baseline_nav": 2000.0,
            "today_return_pct": 14.75, "today_sell_quote_coverage": "1/1",
            "today_pnl_available": True, "valuation_status": "verified",
        }
        for cached in (False, True):
            with self.subTest(cached=cached):
                actual = self.project(cached=cached)
                self.assertEqual({key: actual[key] for key in expected}, expected)

    def test_missing_position_quote_uses_cost_and_reports_fallback(self):
        actual = self.project(quotes={"000002": self.quotes["000002"]})
        self.assertEqual(actual["valuation_status"], "cost_fallback")
        self.assertEqual(actual["valuation_missing_codes"], ["000001"])
        self.assertEqual(actual["position_value"], 1000.0)
        self.assertEqual(actual["unrealized_pnl"], 0.0)
        self.assertEqual(actual["nav"], 10095.0)
        # The verified sell is known, but the missing open holding makes the
        # account's *complete* daily P&L unknown.
        self.assertEqual(actual["today_sell_pnl"], 95.0)
        self.assertIsNone(actual["today_pnl"])
        self.assertIsNone(actual["today_return_pct"])
        self.assertFalse(actual["today_pnl_available"])
        self.assertEqual(actual["today_position_missing_codes"], ["000001"])

    def test_one_missing_holding_does_not_publish_partial_daily_profit(self):
        self.positions.append({"code": "000004", "qty": 50, "cost": 10,
                               "today_acquired_qty": 0, "today_acquired_cost": 0})
        actual = self.project()
        self.assertIsNone(actual["today_pnl"])
        self.assertFalse(actual["today_pnl_available"])
        self.assertEqual(actual["today_position_missing_codes"], ["000004"])

    def test_quote_without_price_is_not_replaced_by_cost_for_daily_profit(self):
        quotes = {code: dict(quote) for code, quote in self.quotes.items()}
        del quotes["000001"]["price"]
        actual = self.project(quotes=quotes)
        self.assertIsNone(actual["today_pnl"])
        self.assertEqual(actual["today_position_missing_codes"], ["000001"])

    def test_unusable_or_incomplete_holding_quote_makes_daily_profit_unknown(self):
        for changes in ({"pct": None}, {"quote_at": "2026-01-01T10:00:00+08:00"}):
            with self.subTest(changes=changes):
                quotes = {code: dict(quote) for code, quote in self.quotes.items()}
                quotes["000001"].update(changes)
                actual = self.project(quotes=quotes)
                self.assertIsNone(actual["today_pnl"])
                self.assertEqual(actual["today_position_missing_codes"], ["000001"])

    def test_fully_sold_position_with_covered_sell_remains_available(self):
        self.positions.clear()
        actual = self.project()
        self.assertEqual(actual["today_pnl"], 95.0)
        self.assertTrue(actual["today_pnl_available"])
        self.assertEqual(actual["today_position_missing_codes"], [])

    def test_empty_account_has_fixed_values(self):
        self.conn.executescript("DELETE FROM paper_orders; DELETE FROM paper_fills; DELETE FROM paper_nav;")
        self.positions.clear()
        for cached in (False, True):
            with self.subTest(cached=cached):
                actual = self.project(quotes={}, cached=cached)
                self.assertEqual(actual["nav"], 10000.0)
                self.assertEqual(actual["total_pnl"], 0.0)
                self.assertEqual(actual["trade_count"], 0)
                self.assertIsNone(actual["today_pnl"])
                self.assertIsNone(actual["win_rate_pct"])
                self.assertEqual(actual["valuation_status"], "verified")

    def test_late_joining_account_uses_reference_without_minting_cash(self):
        self.account.update({
            "id": trading.MAIN_FORCE_STRATEGY_ID, "initial_cash": 0.0, "cash": 0.0,
            "params": '{"shared_pool_reference_capital": 10000}',
        })
        self.positions.clear()
        self.conn.execute(
            "INSERT INTO paper_nav VALUES(?,?,?,?,?,?)",
            (self.account["id"], "2026-01-01", 0, None, "2026-01-01", "verified"),
        )
        real_reference = dataclasses.replace(
            metrics._ports(), account_reference_capital=trading._account_reference_capital,
        )
        with patch.object(metrics, "_dependencies", lambda: real_reference):
            actual = metrics.account_metrics(
                self.conn, self.account, quotes={}, positions=[], allow_network=False,
            )
        self.assertEqual(actual["ledger_initial_cash"], 0.0)
        self.assertEqual(actual["cash"], 0.0)
        self.assertEqual(actual["capital_allocation_mode"], "shared_pool_reference")
        self.assertEqual(actual["shared_pool_reference_capital"], 10000.0)
        self.assertEqual(actual["nav"], 10000.0)
        self.assertEqual(actual["max_drawdown_pct"], 0.0)

    def test_missing_sell_quote_keeps_daily_result_unavailable(self):
        actual = self.project(quotes={"000001": self.quotes["000001"]})
        self.assertFalse(actual["today_pnl_available"])
        self.assertIsNone(actual["today_pnl"])
        self.assertIsNone(actual["today_return_pct"])
        self.assertEqual(actual["today_sell_quote_coverage"], "0/1")
        self.assertEqual(actual["today_sell_missing_codes"], ["000002"])

    def test_past_cycle_and_other_account_do_not_change_account_boundary(self):
        # 当前投影按账户归集历史，旧周期该账户的已核验成交仍在口径内；
        # 其他账户及未核验成交不得混入。这里只保护既有行为，不改变周期规则。
        self.conn.executescript("""
            INSERT INTO paper_orders VALUES
                (4,'acct','000005',100,9,900,5,'filled','sell',
                 '2025-12-31 10:00','2025-12-31 10:00',-20,'verified',1,'past'),
                (5,'other','000006',100,9,900,5,'filled','sell',
                 '2025-12-31 10:00','2025-12-31 10:00',-999,'verified',1,'past');
        """)
        for cached in (False, True):
            with self.subTest(cached=cached):
                actual = self.project(cached=cached)
                self.assertEqual(actual["realized_pnl"], 75.0)
                self.assertEqual(actual["nav"], 10275.0)
                self.assertEqual(actual["trade_count"], 4)
                self.assertEqual(actual["win_rate_pct"], 50.0)
                self.assertEqual(actual["profit_loss_ratio"], 4.75)
                self.assertEqual(actual["today_pnl"], 295.0)


if __name__ == "__main__":
    unittest.main()
