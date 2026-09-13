# -*- coding: utf-8 -*-
import datetime as dt
import os
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_decision_audit as audit
import paper_trading as legacy


class PaperDecisionAuditParityTests(unittest.TestCase):
    def assert_snapshot_equal(self, expected, actual):
        self.assertEqual(expected, actual)

    def test_snapshot_safe_matches_legacy_for_nested_and_scalar_values(self):
        value = {
            "datetime": dt.datetime(2026, 9, 11, 10, 30, 5),
            "date": dt.date(2026, 9, 11),
            "time": dt.time(10, 30, 5),
            "timestamp": pd.Timestamp("2026-09-11T10:30:05"),
            "nested": [np.int64(7), np.float64(1.25), np.bool_(True), {"only"}],
            "nan": float("nan"),
        }
        expected = legacy._snapshot_safe(value)
        actual = audit.snapshot_safe(value)
        self.assert_snapshot_equal(expected, actual)
        self.assertIsNone(actual["nan"])

    def test_kline_snapshot_matches_legacy_including_cutoff_and_truncation(self):
        index = pd.date_range("2026-01-01", periods=125, freq="D")
        frame = pd.DataFrame(
            {
                "open": np.arange(125, dtype=float),
                "high": np.arange(125, dtype=float) + 2,
                "low": np.arange(125, dtype=float) - 1,
                "close": np.arange(125, dtype=float) + 1,
                "volume": np.arange(125, dtype=np.int64) * 100,
                "amount": np.arange(125, dtype=float) * 1000,
            },
            index=index,
        )
        frame.attrs["source"] = "parity-fixture"
        cutoff = index[-2].date().isoformat()
        expected = legacy._snapshot_kline(frame, cutoff)
        actual = audit._snapshot_kline(frame, cutoff)
        self.assert_snapshot_equal(expected, actual)
        self.assertEqual(actual["future_rows"], 1)
        self.assertEqual(len(actual["rows"]), 120)
        self.assertGreater(actual["omitted_rows"], 0)

    def test_factor_evidence_matches_legacy(self):
        payload = {
            "pick": {
                "code": "000001",
                "score": np.float64(0.82),
                "mom5": np.float64(3.5),
                "main_pct": np.float64(2.0),
                "factor_snapshot": {"mom5": np.float64(3.5)},
                "score_components": {
                    "weights": {"mom5": 0.4, "main_pct": 0.6},
                    "label": "fixture",
                },
            },
            "decision": {
                "entry_model": {
                    "score": 0.79,
                    "checks": [
                        {"name": "momentum", "score": 0.8, "weight": 0.4},
                        {"name": "flow", "score": 0.9, "weight": 0.6},
                    ],
                }
            },
        }
        expected = legacy._snapshot_factor_evidence(payload, final_score=0.81)
        actual = audit._snapshot_factor_evidence(payload, final_score=0.81)
        self.assert_snapshot_equal(expected, actual)

    @staticmethod
    def _payload():
        return {
            "pick": {
                "code": "000001",
                "score": 0.88,
                "price": 12.34,
                "mom5": 4.2,
                "main_pct": 2.3,
                "profit_source": "reported",
                "report_date": "2026-06-30",
                "report_published_at": "2026-08-20T18:00:00",
                "score_components": {"weights": {"mom5": 0.4, "main_pct": 0.6}},
            },
            "decision": {
                "version": "decision-fixture-v1",
                "entry_model": {
                    "score": 0.81,
                    "threshold": 0.72,
                    "threshold_context": {"version": "threshold-fixture-v1", "light": "green"},
                    "checks": [{"name": "flow", "score": 0.9, "weight": 0.6}],
                },
            },
            "news": [
                {
                    "title": "fixture announcement",
                    "source": "exchange",
                    "published_at": "2026-09-11T09:00:00",
                    "verified": True,
                    "negative": False,
                }
            ],
        }

    @staticmethod
    def _kline():
        frame = pd.DataFrame(
            {
                "open": [10.0, 10.5],
                "high": [11.0, 11.5],
                "low": [9.8, 10.2],
                "close": [10.8, 11.2],
                "volume": [1000, 1200],
                "amount": [10800.0, 13440.0],
            },
            index=pd.to_datetime(["2026-09-10", "2026-09-11"]),
        )
        frame.attrs["source"] = "fixture-kline"
        return frame

    def test_full_decision_snapshot_matches_legacy(self):
        payload = self._payload()
        quote = {
            "code": "000001", "price": np.float64(12.5), "pct": 2.1,
            "quote_at": "2026-09-11T10:01:02", "source": "eastmoney",
            "quote_validation": "cross_source_checked",
        }
        kwargs = {
            "account_id": "tq_breakout",
            "code": "000001",
            "side": "buy",
            "decision": "approved_signal",
            "reason": "fixture passed",
            "asof_date": "2026-09-11",
            "quote": quote,
            "kline": self._kline(),
            "news": payload["news"],
            "final_score": 0.83,
            "decision_at": "2026-09-11 10:01:03",
        }
        expected = legacy._decision_snapshot(payload, **kwargs)
        actual = audit.build_decision_snapshot(
            payload,
            **kwargs,
            news_scan_meta=legacy._NEWS_SCAN_META,
            risk_version=legacy.RISK_VERSION,
        )
        self.assert_snapshot_equal(expected, actual)

    def test_missing_evidence_matches_legacy(self):
        kwargs = {
            "payload": {},
            "asof_date": "2026-09-11",
            "decision_at": "2026-09-11 10:01:03",
        }
        expected = legacy._decision_snapshot(**kwargs)
        actual = audit.build_decision_snapshot(
            **kwargs,
            news_scan_meta=legacy._NEWS_SCAN_META,
            risk_version=legacy.RISK_VERSION,
        )
        self.assert_snapshot_equal(expected, actual)
        self.assertEqual(actual["data_quality"]["status"], "unknown")

    def test_injected_kline_loader_matches_legacy_fallback(self):
        frame = self._kline()
        payload = {"pick": {"code": "000001"}, "signal_date": "2026-09-11"}

        def loader(code, asof_date, inclusive=True):
            self.assertEqual(code, "000001")
            self.assertEqual(str(asof_date)[:10], "2026-09-11")
            self.assertTrue(inclusive)
            return frame

        with mock.patch.object(legacy, "_completed_kline", side_effect=loader):
            expected = legacy._decision_snapshot(
                payload, decision_at="2026-09-11 10:01:03"
            )
        actual = audit.build_decision_snapshot(
            payload,
            decision_at="2026-09-11 10:01:03",
            kline_loader=loader,
            news_scan_meta=legacy._NEWS_SCAN_META,
            risk_version=legacy.RISK_VERSION,
        )
        self.assert_snapshot_equal(expected, actual)

    def test_payload_enrichment_matches_legacy_and_does_not_mutate_caller(self):
        payload = {"side": "buy", "decision_name": "fixture", "nested": {"keep": True}}
        original = {"side": "buy", "decision_name": "fixture", "nested": {"keep": True}}
        kwargs = {
            "account_id": "tq_breakout",
            "asof_date": "2026-09-11",
            "kline": self._kline(),
            "decision_at": "2026-09-11 10:01:03",
        }
        expected = legacy._with_decision_snapshot(payload, **kwargs)
        actual = audit.with_decision_snapshot(
            payload,
            **kwargs,
            news_scan_meta=legacy._NEWS_SCAN_META,
            risk_version=legacy.RISK_VERSION,
        )
        self.assert_snapshot_equal(expected, actual)
        self.assertEqual(payload, original)
        self.assertEqual(actual["strategy_id"], "tq_breakout")


if __name__ == "__main__":
    unittest.main()
