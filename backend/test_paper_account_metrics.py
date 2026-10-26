import ast
import sqlite3
from pathlib import Path
import unittest

import paper_account_metrics as metrics
import paper_trading as trading


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

    def test_extracted_projection_matches_legacy_with_identical_inputs(self):
        account = {
            "id": "orphan", "name": "fixture", "status": "running", "cycle_days": 1,
            "mode": "test", "style": "strong", "risk_profile": "breakout",
            "version": 1, "params": "{}", "initial_cash": 10000.0, "cash": 10000.0,
            "benchmark_start": None,
        }
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE paper_nav(account_id TEXT, nav REAL, nav_date TEXT,
                benchmark REAL, quote_status TEXT);
            CREATE TABLE paper_fills(account_id TEXT, side TEXT);
            CREATE TABLE paper_orders(account_id TEXT, status TEXT);
        """)
        original = trading._legacy_account_metrics
        cache = {
            "latest_nav": {}, "sells": {}, "buy_count": {}, "rejected": {},
            "navs": {}, "previous_nav": {},
        }
        try:
            trading._account_metrics = original
            expected = original(conn, account, quotes={}, positions=[], metric_cache=cache, allow_network=False)
            actual = metrics.account_metrics(conn, account, quotes={}, positions=[], metric_cache=cache, allow_network=False)
        finally:
            trading._account_metrics = metrics.account_metrics
            conn.close()
        self.assertEqual(expected, actual)

    def test_unbound_module_fails_explicitly(self):
        factory = metrics._dependencies
        metrics._dependencies = None
        try:
            with self.assertRaisesRegex(RuntimeError, "dependencies are not bound"):
                metrics._ports()
        finally:
            metrics._dependencies = factory


if __name__ == "__main__":
    unittest.main()
