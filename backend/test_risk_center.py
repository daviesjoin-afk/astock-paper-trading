# -*- coding: utf-8 -*-
import datetime as dt
import os
import sys
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import risk_center as RC

try:
    import paper_trading as PT
except ImportError:
    PT = None


class RiskCenterTests(unittest.TestCase):
    def test_live_quote_sources_contains_expected_sources(self):
        self.assertIn("live", RC.LIVE_QUOTE_SOURCES)
        self.assertIn("dashboard_cache", RC.LIVE_QUOTE_SOURCES)
        self.assertIn("live_snapshot", RC.LIVE_QUOTE_SOURCES)
        self.assertIn("eastmoney-clist", RC.LIVE_QUOTE_SOURCES)
        self.assertIn("tencent_public_quote", RC.LIVE_QUOTE_SOURCES)

    def test_refresh_snapshot_recognizes_dashboard_cache_quotes(self):
        now_iso = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()
        positions = [
            {
                "account_id": "trend_pullback",
                "code": "600848",
                "name": "上海临港",
                "price": 8.50,
                "ret_pct": 1.2,
                "quote_at": now_iso,
                "quote_source": "dashboard_cache",
                "quote_validation": "dashboard_cached",
                "main_pct": 2.5,
                "small_net": 1000.0,
            }
        ]
        with patch.object(RC, "_save_history", return_value=None):
            snapshot = RC.refresh_snapshot(
                market={"live_index_time": now_iso, "live_index_price": 3800.0, "live_index_pct": 0.5, "light": "yellow"},
                positions=positions,
                universe=[{"code": "600848", "pct": 1.2, "amount": 1e8, "turnover": 2.0}],
                snapshot_at=now_iso,
                news_events=[],
                news_error=None,
                sector_rows=[],
            )
        data_quality = {item["name"]: item for item in snapshot.get("data_quality", [])}
        pos_quote = data_quality.get("持仓实时行情")
        self.assertIsNotNone(pos_quote)
        self.assertEqual(pos_quote["status"], "fresh")
        self.assertEqual(pos_quote["coverage_pct"], 100.0)

    def test_position_queue_does_not_block_dashboard_cache_quote(self):
        now_iso = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()
        positions = [
            {
                "account_id": "trend_pullback",
                "code": "600848",
                "name": "上海临港",
                "price": 8.50,
                "risk_price": 7.50,
                "ret_pct": 0.5,
                "available_qty": 1000,
                "quote_at": now_iso,
                "quote_source": "dashboard_cache",
            }
        ]
        accounts = [{"id": "trend_pullback", "name": "趋势波段优选"}]
        queue = RC._position_queue(accounts, positions, [])
        self.assertEqual(len(queue), 1)
        item = queue[0]
        self.assertNotEqual(item["level"], "blocked")
        self.assertEqual(item["action"], "继续持有")

    def test_build_dashboard_downgrades_unknown_quote_to_tightened_not_blocked(self):
        base_dashboard = {
            "accounts": [
                {
                    "id": "trend_pullback",
                    "name": "趋势波段优选",
                    "status": "running",
                    "nav": 100000.0,
                    "strategy_budget": {"current_pct": 20.0, "target_pct": 50.0, "floor_pct": 10.0, "absolute_cap_amount": 50000.0},
                    "risk_limits": {"drawdown_pct": 15.0},
                }
            ],
            "positions": [
                {
                    "account_id": "trend_pullback",
                    "code": "600848",
                    "price": 8.50,
                    "market_value": 20000.0,
                    "quote_at": "2026-09-15T10:00:00+08:00",
                    "quote_source": "dashboard_cache",
                }
            ],
            "shared": {"nav": 100000.0, "fund_utilization_pct": 20.0},
        }
        # Unknown data source should degrade to tightened, NOT hard-block opening positions
        snapshot = {
            "asof": "2026-09-15T10:00:00+08:00",
            "market": {"light": "green", "live_index_price": 3800.0, "live_index_pct": 0.5},
            "dynamic_risk": {"mode": "normal"},
            "data_quality": [
                {"name": "持仓实时行情", "status": "unknown"},
                {"name": "沪深300实时行情", "status": "fresh"},
            ],
        }
        dashboard = RC.build_dashboard(base_dashboard, snapshot)
        self.assertEqual(dashboard["overall"]["level"], "tightened")
        self.assertNotEqual(dashboard["overall"]["level"], "blocked")

    def test_build_dashboard_blocks_only_on_failed_data_source(self):
        base_dashboard = {
            "accounts": [
                {
                    "id": "trend_pullback",
                    "name": "趋势波段优选",
                    "status": "running",
                    "nav": 100000.0,
                    "strategy_budget": {"current_pct": 20.0, "target_pct": 50.0, "floor_pct": 10.0, "absolute_cap_amount": 50000.0},
                    "risk_limits": {"drawdown_pct": 15.0},
                }
            ],
            "positions": [
                {
                    "account_id": "trend_pullback",
                    "code": "600848",
                    "price": 8.50,
                    "market_value": 20000.0,
                    "quote_at": "2026-09-15T10:00:00+08:00",
                    "quote_source": "dashboard_cache",
                }
            ],
            "shared": {"nav": 100000.0, "fund_utilization_pct": 20.0},
        }
        snapshot = {
            "asof": "2026-09-15T10:00:00+08:00",
            "market": {"light": "green", "live_index_price": 3800.0, "live_index_pct": 0.5},
            "dynamic_risk": {"mode": "normal"},
            "data_quality": [
                {"name": "持仓实时行情", "status": "failed"},
                {"name": "沪深300实时行情", "status": "fresh"},
            ],
        }
        dashboard = RC.build_dashboard(base_dashboard, snapshot)
        self.assertEqual(dashboard["overall"]["level"], "blocked")


class ManifestSemanticMatchTests(unittest.TestCase):
    def test_semantic_match_with_legacy_list_signature(self):
        if PT is None:
            self.skipTest("paper_trading requires pandas")
        current = {
            "content_fingerprint": "abc12345",
            "mtime": 1726000000.0,
            "size": 50000,
            "version": "paper-kline-shared-v1",
        }
        cached_legacy_list = [1725000000.0, 50000, "paper-kline-shared-v1"]
        self.assertTrue(PT._manifest_semantic_match(cached_legacy_list, current))

    def test_semantic_match_with_dict_signature(self):
        if PT is None:
            self.skipTest("paper_trading requires pandas")
        current = {
            "content_fingerprint": "abc12345",
            "mtime": 1726000000.0,
            "size": 50000,
            "version": "paper-kline-shared-v1",
        }
        cached_dict = {
            "content_fingerprint": "abc12345",
            "mtime": 1725000000.0,
            "size": 50000,
            "version": "paper-kline-shared-v1",
        }
        self.assertTrue(PT._manifest_semantic_match(cached_dict, current))

    def test_semantic_match_rejects_mismatched_fingerprint_or_version(self):
        if PT is None:
            self.skipTest("paper_trading requires pandas")
        current = {
            "content_fingerprint": "abc12345",
            "mtime": 1726000000.0,
            "size": 50000,
            "version": "paper-kline-shared-v1",
        }
        mismatched_dict = {
            "content_fingerprint": "different",
            "mtime": 1725000000.0,
            "size": 50000,
            "version": "paper-kline-shared-v1",
        }
        self.assertFalse(PT._manifest_semantic_match(mismatched_dict, current))

        mismatched_list = [1725000000.0, 99999, "paper-kline-shared-v1"]
        self.assertFalse(PT._manifest_semantic_match(mismatched_list, current))


if __name__ == "__main__":
    unittest.main()
