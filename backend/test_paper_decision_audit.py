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
    @staticmethod
    def _scan_meta(stale=False):
        return {
            "observed_at": "2026-09-11T10:00:00",
            "stale": stale,
            "error": None,
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
                "custom_factor": [np.float64(0.25), np.float64(0.5)],
            },
            index=pd.to_datetime(["2026-09-10", "2026-09-11"]),
        )
        frame.attrs["source"] = "fixture-kline"
        return frame

    @staticmethod
    def _payload():
        return {
            "pick": {
                "code": "000001",
                "score": 0.88,
                "mom5": 4.2,
                "mom20": 8.4,
                "roe": 0.16,
                "financial_source": "reported",
                "report_date": "2026-06-30",
                "annual_report_date": "2025-12-31",
                "disclosure_at": "2026-08-20T18:00:00",
                "score_components": {
                    "version": "score-components-v1",
                    "weights": {"mom5": 0.4, "roe": 0.6},
                },
            },
            "decision": {
                "entry_model": {
                    "score": 0.81,
                    "threshold": 0.72,
                    "threshold_context": {
                        "version": "threshold-fixture-v1",
                        "light": "green",
                    },
                    "checks": [
                        {
                            "name": "flow",
                            "score": 0.9,
                            "weight": 0.6,
                            "detail": "fixture",
                        }
                    ],
                    "news_learning": {"threshold_delta": -0.02},
                },
            },
            "history": {"last_date": "2026-09-11"},
            "news": [
                {
                    "title": "fixture announcement",
                    "source": "exchange",
                    "published_at": "2026-09-11T09:00:00",
                    "verified": True,
                    "negative": False,
                    "extra": {"keep": True},
                }
            ],
        }

    def test_snapshot_safe_matches_legacy_for_nested_and_scalar_values(self):
        value = {
            "datetime": dt.datetime(2026, 9, 11, 10, 30, 5),
            "date": dt.date(2026, 9, 11),
            "time": dt.time(10, 30, 5),
            "timestamp": pd.Timestamp("2026-09-11T10:30:05"),
            "nested": [np.int64(7), np.float64(1.25), np.bool_(True), {"only"}],
            "nan": float("nan"),
        }
        self.assertEqual(legacy._snapshot_safe(value), audit.snapshot_safe(value))

    def test_snapshot_date_and_first_match_legacy(self):
        for value in (
            None,
            "",
            "2026-09-11",
            dt.date(2026, 9, 11),
            dt.datetime(2026, 9, 11, 10, 5),
            "not-a-date",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    legacy._snapshot_date(value),
                    audit._snapshot_date(value),
                )
        mapping = {"a": "", "b": None, "c": 0, "d": "value"}
        self.assertEqual(
            legacy._snapshot_first(mapping, "a", "b", "c", "d"),
            audit._snapshot_first(mapping, "a", "b", "c", "d"),
        )

    def test_kline_snapshot_matches_legacy_including_custom_columns_and_truncation(self):
        index = pd.date_range("2026-01-01", periods=125, freq="D")
        frame = pd.DataFrame(
            {
                "open": np.arange(125, dtype=float),
                "close": np.arange(125, dtype=float) + 1,
                "volume": np.arange(125, dtype=np.int64) * 100,
                "custom_factor": np.arange(125, dtype=float) / 10,
            },
            index=index,
        )
        frame.attrs["source"] = "parity-fixture"
        cutoff = index[-2].date().isoformat()
        expected = legacy._snapshot_kline(frame, cutoff)
        actual = audit._snapshot_kline(frame, cutoff)
        self.assertEqual(expected, actual)
        self.assertEqual(actual["future_rows"], 1)
        self.assertEqual(actual["rows_stored"], 120)
        self.assertEqual(actual["status"], "future_excluded_truncated")
        self.assertIn("custom_factor", actual["rows"][-1])

    def test_factor_evidence_matches_legacy_exact_signature(self):
        payload = self._payload()
        self.assertEqual(
            legacy._snapshot_factor_evidence(payload),
            audit._snapshot_factor_evidence(payload),
        )

    def test_full_decision_snapshot_matches_legacy(self):
        payload = self._payload()
        quote = {
            "code": "000001",
            "price": np.float64(12.5),
            "pct": 2.1,
            "quote_at": "2026-09-11T10:01:02",
            "source": "eastmoney",
            "quote_validation": "cross_source_checked",
            "quote_cross_check": {"provider": "tencent", "passed": True},
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
        scan_meta = self._scan_meta()
        with mock.patch.object(legacy, "_NEWS_SCAN_META", scan_meta):
            expected = legacy._decision_snapshot(payload, **kwargs)
        actual = audit.build_decision_snapshot(
            payload,
            **kwargs,
            news_scan_meta=scan_meta,
            risk_version=legacy.RISK_VERSION,
        )
        self.assertEqual(expected, actual)

    def test_missing_evidence_matches_legacy(self):
        kwargs = {
            "payload": {},
            "asof_date": "2026-09-11",
            "decision_at": "2026-09-11 10:01:03",
        }
        scan_meta = self._scan_meta(stale=True)
        with mock.patch.object(legacy, "_NEWS_SCAN_META", scan_meta):
            expected = legacy._decision_snapshot(**kwargs)
        actual = audit.build_decision_snapshot(
            **kwargs,
            news_scan_meta=scan_meta,
            risk_version=legacy.RISK_VERSION,
        )
        self.assertEqual(expected, actual)
        self.assertEqual(actual["data_quality"]["overall"], "degraded")
        self.assertEqual(actual["data_quality"]["news"], "stale")

    def test_model_signal_and_entry_model_fallbacks_match_legacy(self):
        variants = [
            {"model": {"final_score": 0.73, "entry_model": {"threshold": 0.7}}},
            {"signal": {"avg_score": 0.74, "entry_model": {"threshold": 0.71}}},
            {"entry_model": {"score": 0.75, "threshold": 0.72}},
        ]
        scan_meta = self._scan_meta()
        for payload in variants:
            with self.subTest(payload=payload):
                with mock.patch.object(legacy, "_NEWS_SCAN_META", scan_meta):
                    expected = legacy._decision_snapshot(
                        payload,
                        decision_at="2026-09-11 10:01:03",
                    )
                actual = audit.build_decision_snapshot(
                    payload,
                    decision_at="2026-09-11 10:01:03",
                    news_scan_meta=scan_meta,
                    risk_version=legacy.RISK_VERSION,
                )
                self.assertEqual(expected, actual)

    def test_injected_kline_loader_matches_legacy_fallback(self):
        frame = self._kline()
        payload = {"pick": {"code": "000001"}, "signal_date": "2026-09-11"}
        calls = []

        def loader(code, asof_date, inclusive=True):
            calls.append((code, asof_date, inclusive))
            return frame

        scan_meta = self._scan_meta()
        with (
            mock.patch.object(legacy, "_completed_kline", side_effect=loader),
            mock.patch.object(legacy, "_NEWS_SCAN_META", scan_meta),
        ):
            expected = legacy._decision_snapshot(
                payload,
                decision_at="2026-09-11 10:01:03",
            )
        actual = audit.build_decision_snapshot(
            payload,
            decision_at="2026-09-11 10:01:03",
            kline_loader=loader,
            news_scan_meta=scan_meta,
            risk_version=legacy.RISK_VERSION,
        )
        self.assertEqual(expected, actual)
        self.assertEqual(
            calls,
            [
                ("000001", "2026-09-11", True),
                ("000001", "2026-09-11", True),
            ],
        )

    def test_injected_loader_failure_matches_legacy_failure_fallback(self):
        payload = {"pick": {"code": "000001"}, "signal_date": "2026-09-11"}

        def broken_loader(*_args, **_kwargs):
            raise RuntimeError("fixture loader failure")

        scan_meta = self._scan_meta()
        with (
            mock.patch.object(legacy, "_completed_kline", side_effect=broken_loader),
            mock.patch.object(legacy, "_NEWS_SCAN_META", scan_meta),
        ):
            expected = legacy._decision_snapshot(
                payload,
                decision_at="2026-09-11 10:01:03",
            )
        actual = audit.build_decision_snapshot(
            payload,
            decision_at="2026-09-11 10:01:03",
            kline_loader=broken_loader,
            news_scan_meta=scan_meta,
            risk_version=legacy.RISK_VERSION,
        )
        self.assertEqual(expected, actual)

    def test_injected_clock_matches_legacy_now(self):
        fixed_now = "2026-09-11 10:01:03"
        scan_meta = self._scan_meta()
        with (
            mock.patch.object(legacy, "_now", return_value=fixed_now),
            mock.patch.object(legacy, "_NEWS_SCAN_META", scan_meta),
        ):
            expected = legacy._decision_snapshot({})
        actual = audit.build_decision_snapshot(
            {},
            now_fn=lambda: fixed_now,
            news_scan_meta=scan_meta,
            risk_version=legacy.RISK_VERSION,
        )
        self.assertEqual(expected, actual)

    def test_payload_enrichment_matches_legacy_and_does_not_mutate_caller(self):
        payload = {
            "side": "buy",
            "decision_name": "fixture",
            "nested": {"keep": True},
        }
        original = {
            "side": "buy",
            "decision_name": "fixture",
            "nested": {"keep": True},
        }
        legacy_kwargs = {
            "account_id": "tq_breakout",
            "asof_date": "2026-09-11",
            "kline": self._kline(),
            "decision_at": "2026-09-11 10:01:03",
        }
        scan_meta = self._scan_meta()
        with mock.patch.object(legacy, "_NEWS_SCAN_META", scan_meta):
            expected = legacy._with_decision_snapshot(payload, **legacy_kwargs)
        actual = audit.with_decision_snapshot(
            payload,
            **legacy_kwargs,
            news_scan_meta=scan_meta,
            risk_version=legacy.RISK_VERSION,
        )
        self.assertEqual(expected, actual)
        self.assertEqual(payload, original)
        self.assertEqual(actual["strategy_id"], "tq_breakout")


if __name__ == "__main__":
    unittest.main()
