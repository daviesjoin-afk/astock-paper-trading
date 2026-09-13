# -*- coding: utf-8 -*-
"""Contract tests for :mod:`paper_decision_audit`.

These tests pin the *behaviour* of the extracted decision-audit serializer with
explicit expectations and one frozen golden envelope.  They deliberately never
derive an expected value from ``paper_trading``: after the issue #124 facade
cutover such a comparison would be ``new implementation == new implementation``
(tautological) and could no longer detect a regression.  The facade itself is
covered by ``test_paper_decision_audit_facade.py``.
"""
import copy
import datetime as dt
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_decision_audit as audit


GOLDEN_SCAN_META = {
    "observed_at": "2026-09-11T10:00:00",
    "stale": False,
    "error": None,
}

# Representative envelope frozen from the parity-verified implementation of
# PR #125.  It is the contract every audit consumer (and the paper facade)
# must keep producing byte-for-byte.
GOLDEN_ENVELOPE = {
    "account_id": "tq_breakout",
    "asof": "2026-09-11",
    "code": "000001",
    "data_quality": {
        "financial": "ok",
        "history_last_date": "2026-09-11",
        "kline": "ok",
        "news": "ok",
        "news_scan": {
            "error": None,
            "observed_at": "2026-09-11T10:00:00",
            "stale": False,
        },
        "overall": "ok",
        "quote": "ok",
    },
    "decision_at": "2026-09-11 10:01:03",
    "factors": {
        "contributions": {"mom5": 1.68, "roe": 0.096},
        "entry_checks": [
            {
                "contribution": 0.54,
                "detail": "fixture",
                "name": "flow",
                "raw_score": 0.9,
                "weight": 0.6,
            }
        ],
        "raw": {
            "annual_report_date": "2025-12-31",
            "disclosure_at": "2026-08-20T18:00:00",
            "financial_source": "reported",
            "mom20": 8.4,
            "mom5": 4.2,
            "report_date": "2026-06-30",
            "roe": 0.16,
        },
        "score_components": {
            "version": "score-components-v1",
            "weights": {"mom5": 0.4, "roe": 0.6},
        },
    },
    "final": {
        "decision": "approved_signal",
        "reason": "fixture passed",
        "score": 0.83,
    },
    "financial": {
        "annual_report_date": "2025-12-31",
        "disclosure_at": "2026-08-20T18:00:00",
        "report_date": "2026-06-30",
        "report_period": "2026-06-30",
        "source": "reported",
    },
    "kline": {
        "count": 2,
        "first_date": "2026-09-10",
        "future_rows": 0,
        "last_date": "2026-09-11",
        "omitted_rows": 0,
        "rows": [
            {
                "amount": 10800.0,
                "close": 10.8,
                "custom_factor": 0.25,
                "date": "2026-09-10",
                "high": 11.0,
                "low": 9.8,
                "open": 10.0,
                "volume": 1000.0,
            },
            {
                "amount": 13440.0,
                "close": 11.2,
                "custom_factor": 0.5,
                "date": "2026-09-11",
                "high": 11.5,
                "low": 10.2,
                "open": 10.5,
                "volume": 1200.0,
            },
        ],
        "rows_stored": 2,
        "source": "fixture-kline",
        "status": "ok",
    },
    "news": {
        "announcement_times": ["2026-09-11T09:00:00"],
        "events": [
            {
                "event_at": "2026-09-11T09:00:00",
                "extra": {"keep": True},
                "negative": False,
                "published_at": "2026-09-11T09:00:00",
                "source": "exchange",
                "title": "fixture announcement",
                "verified": True,
            }
        ],
    },
    "quote": {
        "cross_check": {"passed": True, "provider": "tencent"},
        "pct": 2.1,
        "price": 12.5,
        "quote_at": "2026-09-11T10:01:02",
        "source": "eastmoney",
        "validation": "cross_source_checked",
    },
    "side": "buy",
    "strategy_id": "tq_breakout",
    "threshold": {
        "delta": -0.02,
        "dynamic": True,
        "value": 0.72,
        "version": "threshold-fixture-v1",
    },
    "version": "decision-snapshot-v1",
}


def _kline(source="fixture-kline"):
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
    frame.attrs["source"] = source
    return frame


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


def _quote():
    return {
        "code": "000001",
        "price": np.float64(12.5),
        "pct": 2.1,
        "quote_at": "2026-09-11T10:01:02",
        "source": "eastmoney",
        "quote_validation": "cross_source_checked",
        "quote_cross_check": {"provider": "tencent", "passed": True},
    }


class SnapshotSafeContractTests(unittest.TestCase):
    def test_normalises_temporal_numpy_and_nested_values(self):
        value = {
            "datetime": dt.datetime(2026, 9, 11, 10, 30, 5),
            "date": dt.date(2026, 9, 11),
            "time": dt.time(10, 30, 5),
            "timestamp": pd.Timestamp("2026-09-11T10:30:05"),
            "numpy_int": np.int64(7),
            "numpy_float": np.float64(1.25),
            "numpy_bool": np.bool_(True),
            "set": {"only"},
            "tuple": (1, 2),
        }
        self.assertEqual(
            audit.snapshot_safe(value),
            {
                "datetime": "2026-09-11T10:30:05",
                "date": "2026-09-11",
                "time": "10:30:05",
                "timestamp": "2026-09-11T10:30:05",
                "numpy_int": 7,
                "numpy_float": 1.25,
                "numpy_bool": True,
                "set": ["only"],
                "tuple": [1, 2],
            },
        )

    def test_converts_nan_to_null_but_keeps_ordinary_floats(self):
        self.assertIsNone(audit.snapshot_safe(float("nan")))
        self.assertIsNone(audit.snapshot_safe(np.float64("nan")))
        self.assertEqual(
            audit.snapshot_safe({"nested": [float("nan"), 1.0, {"deep": np.float64("nan")}]}),
            {"nested": [None, 1.0, {"deep": None}]},
        )
        self.assertEqual(audit.snapshot_safe(0.0), 0.0)

    def test_missing_evidence_stays_null_and_is_never_stringified(self):
        self.assertIsNone(audit.snapshot_safe(None))
        serialised = audit.snapshot_safe({"missing": None})
        self.assertEqual(serialised, {"missing": None})
        self.assertIsNone(serialised["missing"])
        self.assertNotEqual(serialised, {"missing": "None"})
        self.assertNotEqual(serialised, {"missing": ""})

    def test_dict_keys_are_stringified(self):
        self.assertEqual(audit.snapshot_safe({1: "a"}), {"1": "a"})


class SnapshotHelperContractTests(unittest.TestCase):
    def test_snapshot_date_normalises_and_rejects_invalid_input(self):
        cases = {
            None: None,
            "": None,
            "2026-09-11": "2026-09-11",
            dt.date(2026, 9, 11): "2026-09-11",
            dt.datetime(2026, 9, 11, 10, 5): "2026-09-11",
            pd.Timestamp("2026-09-11T10:05:00"): "2026-09-11",
            "2026-09-11T10:05:00": "2026-09-11",
            "not-a-date": None,
            12345: None,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(audit._snapshot_date(value), expected)

    def test_snapshot_first_skips_blank_values_and_non_mappings(self):
        mapping = {"a": "", "b": None, "c": 0, "d": "value"}
        self.assertEqual(audit._snapshot_first(mapping, "a", "b", "c", "d"), 0)
        self.assertEqual(audit._snapshot_first(mapping, "a", "b", "d"), "value")
        self.assertIsNone(audit._snapshot_first(mapping, "a", "b", "missing"))
        self.assertIsNone(audit._snapshot_first(None, "a"))
        self.assertIsNone(audit._snapshot_first(["a"], "a"))


class SnapshotKlineContractTests(unittest.TestCase):
    def test_serialises_completed_bars_with_custom_columns(self):
        evidence = audit._snapshot_kline(_kline(), "2026-09-11")
        self.assertEqual(evidence["source"], "fixture-kline")
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["count"], 2)
        self.assertEqual(evidence["rows_stored"], 2)
        self.assertEqual(evidence["omitted_rows"], 0)
        self.assertEqual(evidence["future_rows"], 0)
        self.assertEqual(evidence["first_date"], "2026-09-10")
        self.assertEqual(evidence["last_date"], "2026-09-11")
        self.assertEqual(
            [row["date"] for row in evidence["rows"]],
            ["2026-09-10", "2026-09-11"],
        )
        self.assertEqual(evidence["rows"][1]["close"], 11.2)
        self.assertEqual(evidence["rows"][1]["custom_factor"], 0.5)

    def test_excludes_future_rows_and_marks_status(self):
        frame = _kline()
        evidence = audit._snapshot_kline(frame, "2026-09-10")
        self.assertEqual(evidence["future_rows"], 1)
        self.assertEqual(evidence["count"], 1)
        self.assertEqual(evidence["rows_stored"], 1)
        self.assertEqual(evidence["status"], "future_excluded")
        self.assertEqual([row["date"] for row in evidence["rows"]], ["2026-09-10"])
        self.assertEqual(evidence["last_date"], "2026-09-10")

    def test_truncates_to_120_bars_but_reports_the_full_series(self):
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
        evidence = audit._snapshot_kline(frame, cutoff)
        self.assertEqual(evidence["future_rows"], 1)
        self.assertEqual(evidence["rows_stored"], 120)
        self.assertEqual(evidence["omitted_rows"], 4)
        self.assertEqual(evidence["count"], 124)
        self.assertEqual(evidence["status"], "future_excluded_truncated")
        self.assertEqual(evidence["first_date"], index[0].date().isoformat())
        self.assertEqual(evidence["last_date"], cutoff)
        self.assertEqual(evidence["rows"][-1]["date"], cutoff)
        self.assertEqual(evidence["rows"][0]["date"], index[4].date().isoformat())
        self.assertIn("custom_factor", evidence["rows"][-1])

    def test_missing_frame_is_reported_as_unknown_evidence(self):
        self.assertEqual(
            audit._snapshot_kline(None, "2026-09-11"),
            {
                "source": "unknown",
                "rows": [],
                "count": 0,
                "first_date": None,
                "last_date": None,
                "future_rows": 0,
                "status": "unknown",
            },
        )
        self.assertEqual(audit._snapshot_kline([], "2026-09-11")["status"], "unknown")


class SnapshotFactorEvidenceContractTests(unittest.TestCase):
    def test_computes_weighted_contributions_and_entry_checks(self):
        evidence = audit._snapshot_factor_evidence(_payload())
        self.assertEqual(evidence["contributions"], {"mom5": 1.68, "roe": 0.096})
        self.assertEqual(
            evidence["entry_checks"],
            [
                {
                    "name": "flow",
                    "raw_score": 0.9,
                    "weight": 0.6,
                    "contribution": 0.54,
                    "detail": "fixture",
                }
            ],
        )
        self.assertEqual(evidence["raw"]["mom5"], 4.2)
        self.assertEqual(evidence["raw"]["report_date"], "2026-06-30")
        self.assertEqual(
            evidence["score_components"]["weights"], {"mom5": 0.4, "roe": 0.6}
        )

    def test_missing_evidence_yields_empty_evidence_containers(self):
        self.assertEqual(
            audit._snapshot_factor_evidence({}),
            {
                "raw": {},
                "contributions": {},
                "score_components": {},
                "entry_checks": [],
            },
        )
        self.assertEqual(
            audit._snapshot_factor_evidence(None),
            {
                "raw": {},
                "contributions": {},
                "score_components": {},
                "entry_checks": [],
            },
        )

    def test_unparsable_weight_contribution_stays_null(self):
        payload = {
            "pick": {
                "factor_snapshot": {"mom5": "not-a-number"},
                "score_components": {"weights": {"mom5": 0.4}},
            }
        }
        self.assertEqual(
            audit._snapshot_factor_evidence(payload)["contributions"], {"mom5": None}
        )


class BuildDecisionSnapshotContractTests(unittest.TestCase):
    def test_full_envelope_matches_frozen_golden_contract(self):
        snapshot = audit.build_decision_snapshot(
            _payload(),
            account_id="tq_breakout",
            code="000001",
            side="buy",
            decision="approved_signal",
            reason="fixture passed",
            asof_date="2026-09-11",
            quote=_quote(),
            kline=_kline(),
            news=_payload()["news"],
            final_score=0.83,
            decision_at="2026-09-11 10:01:03",
            news_scan_meta=GOLDEN_SCAN_META,
            risk_version="paper-risk-v4",
        )
        self.assertEqual(snapshot, GOLDEN_ENVELOPE)

    def test_missing_evidence_envelope_is_degraded_not_invented(self):
        snapshot = audit.build_decision_snapshot(
            {},
            asof_date="2026-09-11",
            decision_at="2026-09-11 10:01:03",
            news_scan_meta={**GOLDEN_SCAN_META, "stale": True},
            risk_version="paper-risk-v4",
        )
        self.assertEqual(snapshot["version"], "decision-snapshot-v1")
        self.assertIsNone(snapshot["account_id"])
        self.assertIsNone(snapshot["code"])
        self.assertIsNone(snapshot["side"])
        self.assertEqual(snapshot["asof"], "2026-09-11")
        self.assertIsNone(snapshot["final"]["score"])
        self.assertIsNone(snapshot["final"]["reason"])
        self.assertEqual(snapshot["final"]["decision"], "unknown")
        self.assertEqual(snapshot["threshold"]["version"], "paper-risk-v4")
        self.assertFalse(snapshot["threshold"]["dynamic"])
        self.assertEqual(snapshot["data_quality"]["quote"], "unknown")
        self.assertEqual(snapshot["data_quality"]["kline"], "unknown")
        self.assertEqual(snapshot["data_quality"]["financial"], "unknown")
        self.assertEqual(snapshot["data_quality"]["news"], "stale")
        self.assertEqual(snapshot["data_quality"]["overall"], "degraded")
        self.assertEqual(snapshot["kline"]["rows"], [])

    def test_model_signal_and_entry_model_fallbacks(self):
        variants = [
            ({"model": {"final_score": 0.73, "entry_model": {"threshold": 0.7}}}, 0.73, 0.7),
            ({"signal": {"avg_score": 0.74, "entry_model": {"threshold": 0.71}}}, 0.74, 0.71),
            ({"entry_model": {"score": 0.75, "threshold": 0.72}}, 0.75, 0.72),
        ]
        for payload, expected_score, expected_threshold in variants:
            with self.subTest(payload=payload):
                snapshot = audit.build_decision_snapshot(
                    payload,
                    decision_at="2026-09-11 10:01:03",
                    news_scan_meta=GOLDEN_SCAN_META,
                    risk_version="paper-risk-v4",
                )
                self.assertEqual(snapshot["final"]["score"], expected_score)
                self.assertEqual(snapshot["threshold"]["value"], expected_threshold)

    def test_reason_falls_back_to_entry_reasons_then_blockers(self):
        payload = {
            "decision": {
                "entry_model": {"reasons": ["momentum ok", "volume ok"]}
            }
        }
        snapshot = audit.build_decision_snapshot(
            payload, decision_at="2026-09-11 10:01:03", news_scan_meta=GOLDEN_SCAN_META
        )
        self.assertEqual(snapshot["final"]["reason"], "momentum ok；volume ok")

        blocked = {"decision": {"entry_model": {"blockers": ["limit up"]}}}
        snapshot = audit.build_decision_snapshot(
            blocked, decision_at="2026-09-11 10:01:03", news_scan_meta=GOLDEN_SCAN_META
        )
        self.assertEqual(snapshot["final"]["reason"], "limit up")

    def test_decision_name_is_used_when_no_explicit_decision(self):
        snapshot = audit.build_decision_snapshot(
            {"decision_name": "candidate"}, decision_at="2026-09-11 10:01:03"
        )
        self.assertEqual(snapshot["final"]["decision"], "candidate")

    def test_injected_kline_loader_is_used_with_inclusive_flag(self):
        frame = _kline(source="injected-loader")
        calls = []

        def loader(code, asof_date, inclusive=True):
            calls.append((code, asof_date, inclusive))
            return frame

        snapshot = audit.build_decision_snapshot(
            {"pick": {"code": "000001"}, "signal_date": "2026-09-11"},
            decision_at="2026-09-11 10:01:03",
            kline_loader=loader,
            news_scan_meta=GOLDEN_SCAN_META,
            risk_version="paper-risk-v4",
        )
        self.assertEqual(calls, [("000001", "2026-09-11", True)])
        self.assertEqual(snapshot["kline"]["source"], "injected-loader")
        self.assertEqual(snapshot["kline"]["count"], 2)

    def test_explicit_kline_skips_the_loader(self):
        calls = []

        def loader(*args, **kwargs):
            calls.append((args, kwargs))
            return _kline()

        snapshot = audit.build_decision_snapshot(
            {"pick": {"code": "000001"}, "signal_date": "2026-09-11"},
            kline=_kline(source="explicit"),
            decision_at="2026-09-11 10:01:03",
            kline_loader=loader,
            news_scan_meta=GOLDEN_SCAN_META,
        )
        self.assertEqual(calls, [])
        self.assertEqual(snapshot["kline"]["source"], "explicit")

    def test_loader_failure_degrades_to_unknown_kline(self):
        def broken_loader(*_args, **_kwargs):
            raise RuntimeError("fixture loader failure")

        snapshot = audit.build_decision_snapshot(
            {"pick": {"code": "000001"}, "signal_date": "2026-09-11"},
            decision_at="2026-09-11 10:01:03",
            kline_loader=broken_loader,
            news_scan_meta=GOLDEN_SCAN_META,
            risk_version="paper-risk-v4",
        )
        self.assertEqual(snapshot["kline"]["status"], "unknown")
        self.assertEqual(snapshot["kline"]["rows"], [])
        self.assertEqual(snapshot["data_quality"]["kline"], "unknown")

    def test_injected_clock_supplies_decision_at(self):
        snapshot = audit.build_decision_snapshot(
            {},
            now_fn=lambda: "2026-09-11 10:01:03",
            news_scan_meta=GOLDEN_SCAN_META,
        )
        self.assertEqual(snapshot["decision_at"], "2026-09-11 10:01:03")

    def test_explicit_decision_at_wins_over_the_injected_clock(self):
        snapshot = audit.build_decision_snapshot(
            {},
            decision_at="2026-09-11 10:01:03",
            now_fn=lambda: "2099-01-01 00:00:00",
            news_scan_meta=GOLDEN_SCAN_META,
        )
        self.assertEqual(snapshot["decision_at"], "2026-09-11 10:01:03")

    def test_quote_validation_gate_controls_quote_quality(self):
        degraded = audit.build_decision_snapshot(
            {},
            quote={
                "quote_at": "2026-09-11T10:01:02",
                "source": "eastmoney",
                "quote_validation": "single_source",
            },
            decision_at="2026-09-11 10:01:03",
            news_scan_meta=GOLDEN_SCAN_META,
        )
        self.assertEqual(degraded["data_quality"]["quote"], "degraded")
        self.assertEqual(degraded["data_quality"]["overall"], "degraded")

    def test_news_events_keep_announcement_times_for_verified_rows(self):
        snapshot = audit.build_decision_snapshot(
            {
                "news": [
                    {
                        "title": "verified",
                        "time": "2026-09-11T08:00:00",
                        "verified": True,
                    },
                    {
                        "title": "aggregator",
                        "time": "2026-09-11T08:30:00",
                        "source_type": "announcement_aggregator",
                    },
                    {"title": "plain", "time": "2026-09-11T09:00:00"},
                    "not-a-mapping",
                ]
            },
            decision_at="2026-09-11 10:01:03",
            news_scan_meta=GOLDEN_SCAN_META,
        )
        self.assertEqual(
            snapshot["news"]["announcement_times"],
            ["2026-09-11T08:00:00", "2026-09-11T08:30:00"],
        )
        self.assertEqual(len(snapshot["news"]["events"]), 3)
        self.assertEqual(snapshot["news"]["events"][0]["event_at"], "2026-09-11T08:00:00")


class WithDecisionSnapshotContractTests(unittest.TestCase):
    def test_preserves_caller_payload_and_enriches_strategy_id(self):
        payload = {
            "side": "buy",
            "decision_name": "fixture",
            "nested": {"keep": True},
        }
        original = copy.deepcopy(payload)
        enriched = audit.with_decision_snapshot(
            payload,
            account_id="tq_breakout",
            side="buy",
            asof_date="2026-09-11",
            kline=_kline(),
            decision_at="2026-09-11 10:01:03",
            news_scan_meta=GOLDEN_SCAN_META,
            risk_version="paper-risk-v4",
        )
        self.assertEqual(payload, original)
        self.assertIsNot(enriched, payload)
        self.assertEqual(enriched["strategy_id"], "tq_breakout")
        self.assertEqual(enriched["side"], "buy")
        self.assertEqual(enriched["nested"], {"keep": True})
        self.assertEqual(
            enriched["decision_snapshot"]["strategy_id"], "tq_breakout"
        )
        self.assertEqual(enriched["decision_snapshot"]["side"], "buy")

    def test_existing_strategy_id_is_not_overwritten(self):
        enriched = audit.with_decision_snapshot(
            {"strategy_id": "explicit-strategy"},
            account_id="tq_breakout",
            decision_at="2026-09-11 10:01:03",
            news_scan_meta=GOLDEN_SCAN_META,
        )
        self.assertEqual(enriched["strategy_id"], "explicit-strategy")

    def test_non_mapping_payload_is_normalised_to_empty_mapping(self):
        enriched = audit.with_decision_snapshot(
            None,
            account_id="tq_breakout",
            decision_at="2026-09-11 10:01:03",
            news_scan_meta=GOLDEN_SCAN_META,
        )
        self.assertEqual(enriched["strategy_id"], "tq_breakout")
        self.assertIn("decision_snapshot", enriched)

    def test_attached_snapshot_is_the_built_envelope(self):
        kwargs = {
            "account_id": "tq_breakout",
            "asof_date": "2026-09-11",
            "kline": _kline(),
            "decision_at": "2026-09-11 10:01:03",
            "news_scan_meta": GOLDEN_SCAN_META,
            "risk_version": "paper-risk-v4",
        }
        enriched = audit.with_decision_snapshot({"pick": {"code": "000001"}}, **kwargs)
        self.assertEqual(
            enriched["decision_snapshot"],
            audit.build_decision_snapshot(
                {"pick": {"code": "000001"}, "strategy_id": "tq_breakout"}, **kwargs
            ),
        )


if __name__ == "__main__":
    unittest.main()
