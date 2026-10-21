"""Contract tests for saved features, label maturity and conditional daily IC."""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import ai_review_service as AR
import factor_effectiveness_report as FE


class FactorEffectivenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "paper.sqlite3"
        self.klines = self.root / "klines"
        self.klines.mkdir()
        self.sessions = []
        day = dt.date(2026, 8, 24)
        while len(self.sessions) < 42:
            if day.weekday() < 5:
                self.sessions.append(day.isoformat())
            day += dt.timedelta(days=1)
        self.asof = self.sessions[-1] + "T16:00:00+08:00"
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        columns = """id INTEGER, account_id TEXT, signal_date TEXT, code TEXT,
                     created_at TEXT, payload TEXT, strategy_id TEXT,
                     strategy_version INTEGER, strategy_checksum TEXT"""
        for table in ("paper_signals", "paper_signals_archive"):
            self.conn.execute(f"CREATE TABLE {table}({columns})")
        self.write_prices("BENCH_000300", 0)
        for index in range(1, 7):
            self.write_prices(f"{index:06}", index)

    def write_prices(self, code, strength, omit=()):
        with (self.klines / f"{code}.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "close"])
            for index, day in enumerate(self.sessions):
                if day not in omit:
                    writer.writerow([day, 100 * (1 + strength * 0.001) ** index])

    def add(self, day, strength, *, version=1, checksum="checksum-v1", table="paper_signals",
            factors=None, weights=None, created_at=None, row_id=None):
        snapshot = {"mom_short": strength, "flow": 1} if factors is None else factors
        payload = {"pick": {"factor_snapshot": snapshot, "historical_factor_date": day,
                            "score_components": {"weights": weights or {"mom_short": 0.5, "flow": 0.5},
                                                 "factor_calibration_version": "factor-calibration-v2"}}}
        if row_id is None:
            row_id = self.conn.execute("SELECT COUNT(*) FROM paper_signals").fetchone()[0] + 1
        self.conn.execute(f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?,?)", (
            row_id, "tq_breakout", day, f"{strength:06}", created_at or day + "T16:00:00+08:00",
            json.dumps(payload), "tq_breakout", version, checksum))
        self.conn.commit()

    def build(self, **kwargs):
        return FE.build_report(self.db, kline_dir=self.klines, asof=kwargs.pop("asof", self.asof), **kwargs)

    def metric(self, report, factor="mom_short", horizon=1):
        return next(r for r in report["groups"][0]["factors"]
                    if r["factor"] == factor and r["horizon"] == horizon)

    def test_daily_ic_known_order_and_next_session_entry(self):
        for day in self.sessions[:22]:
            for strength in range(1, 7):
                self.add(day, strength)
        report = self.build(include_samples=True)
        metric = self.metric(report)
        self.assertEqual(metric["mean_rank_ic"], 1)
        self.assertEqual(metric["valid_ic_dates"], 22)
        self.assertEqual(metric["paired_rows"], 132)
        self.assertEqual(metric["status"], "descriptive")
        self.assertAlmostEqual(metric["low_return_pct"], 0.15)
        self.assertAlmostEqual(metric["high_return_pct"], 0.55)
        label = report["samples"][0]["labels"]["1"]
        self.assertEqual(label["entry_date"], self.sessions[1])
        self.assertEqual(label["exit_date"], self.sessions[2])

    def test_pending_and_unavailable_returns_never_become_zero(self):
        day = self.sessions[-3]
        for strength in range(1, 7):
            self.add(day, strength)
        self.write_prices("000001", 1, omit=(self.sessions[-1],))
        report = self.build(include_samples=True)
        counts = report["groups"][0]["label_counts"]
        self.assertEqual(counts["1"], {"unavailable": 1, "verified": 5})
        self.assertEqual(counts["5"], {"pending": 6})
        self.assertEqual(self.metric(report, horizon=5)["paired_rows"], 0)
        for sample in report["samples"]:
            self.assertIsNone(sample["labels"]["5"]["raw_forward_return"])

    def test_missing_invalid_and_constant_are_separate(self):
        for strength in range(1, 7):
            factors = {"flow": 1, "mom_short": strength}
            if strength == 1:
                factors.pop("mom_short")
            if strength == 2:
                factors["mom_short"] = "nan"
            self.add(self.sessions[0], strength, factors=factors)
        report = self.build()
        metric = self.metric(report)
        self.assertEqual(metric["missing_rows"], 1)
        self.assertEqual(metric["invalid_rows"], 1)
        self.assertEqual(metric["paired_rows"], 4)
        self.assertIsNone(metric["mean_rank_ic"])
        self.assertEqual(metric["skipped"]["too_few_pairs_dates"], 1)
        constant = self.metric(report, "flow")
        self.assertEqual(constant["skipped"]["constant_factor_dates"], 1)
        self.assertIsNone(constant["high_minus_low_pp"])

    def test_versions_and_weight_configurations_are_not_pooled(self):
        for day, version, weights in ((self.sessions[0], 1, None), (self.sessions[1], 2, None),
                                      (self.sessions[2], 2, {"mom_short": 0.6, "flow": 0.4})):
            for strength in range(1, 7):
                self.add(day, strength, version=version, weights=weights)
        report = self.build()
        self.assertEqual(len(report["groups"]), 3)
        self.assertTrue(all(m["valid_ic_dates"] <= 1 for g in report["groups"] for m in g["factors"]))
        summary = FE.evidence_summary(report)
        self.assertEqual(summary["omitted_historical_groups"], 2)
        self.assertEqual(summary["groups"][0]["last_decision_at"][:10], self.sessions[2])

    def test_unknown_version_withholds_metrics(self):
        for strength in range(1, 7):
            self.add(self.sessions[0], strength, version=None)
        metric = self.metric(self.build())
        self.assertEqual(metric["status"], "incomplete_identity")
        self.assertIsNone(metric["mean_rank_ic"])

    def test_archive_copies_do_not_inflate_and_conflicts_are_excluded(self):
        for strength in range(1, 7):
            self.add(self.sessions[0], strength, row_id=strength)
            self.add(self.sessions[0], strength, row_id=strength, table="paper_signals_archive")
        report = self.build(include_samples=True)
        self.assertEqual(report["coverage"]["candidate_rows"], 6)
        self.assertEqual(report["coverage"]["rejected"]["duplicate_copy"], 6)
        self.assertEqual(len(report["samples"][0]["source_refs"]), 2)
        self.add(self.sessions[0], 1, table="paper_signals_archive", factors={"mom_short": 99, "flow": 1})
        report = self.build()
        self.assertEqual(report["coverage"]["candidate_rows"], 5)
        self.assertEqual(report["coverage"]["rejected"]["conflicting_samples"], 1)

    def test_uses_actual_created_at_and_rejects_future_features(self):
        self.add(self.sessions[0], 1, created_at=self.sessions[1] + "T10:00:00+08:00")
        self.add(self.sessions[2], 2, created_at=self.sessions[1] + "T10:00:00+08:00")
        report = self.build(include_samples=True)
        self.assertEqual(report["coverage"]["rejected"]["future_factor_snapshot"], 1)
        self.assertEqual(report["samples"][0]["labels"]["1"]["entry_date"], self.sessions[2])
        self.assertEqual(self.build(asof=self.sessions[0])["coverage"]["candidate_rows"], 0)

    def test_report_reproducible_readonly_and_missing_database_not_created(self):
        self.add(self.sessions[0], 1)
        before = self.db.read_bytes()
        report = self.build(include_samples=True)
        self.assertEqual(report["report_hash"], self.build()["report_hash"])
        self.assertEqual(before, self.db.read_bytes())
        missing = self.root / "absent.sqlite3"
        result = FE.build_report(missing)
        self.assertEqual(result["issues"], ["signal_ledger_unavailable"])
        self.assertFalse(missing.exists())
        json.dumps(report, allow_nan=False)
        self.assertIn(report["groups"][0]["group_id"], FE.render_markdown(report))

    def test_missing_calendar_does_not_infer_sessions_from_selected_stocks(self):
        self.add(self.sessions[0], 1)
        (self.klines / "BENCH_000300.csv").unlink()
        report = self.build()
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("market_session_calendar_unavailable", report["issues"])
        self.assertEqual(report["groups"][0]["label_counts"]["1"], {"unavailable": 1})

    def test_evidence_collector_passes_report_and_no_anomaly_semantics(self):
        import deepseek_advisor as DA
        adaptive = sqlite3.connect(":memory:")
        self.addCleanup(adaptive.close)
        for table in ("adaptive_market_profiles", "adaptive_rewards", "adaptive_runs"):
            adaptive.execute(f"CREATE TABLE {table}(status TEXT)")
        snapshot = {"saved_at": self.asof, "rows": [
            {"code": f"{i:06}", "price": 10, "pct": 1, "quote_at": self.asof} for i in range(1, 1001)]}
        report = self.build()
        with mock.patch.object(DA, "_read_snapshot", return_value=(snapshot, "snapshot.json")), \
             mock.patch.object(DA, "_secondary_quote_check", return_value={"status": "verified"}), \
             mock.patch.object(FE, "build_report", return_value=report) as build:
            # A missing paper ledger avoids unrelated production accounting fixture setup.
            evidence, digest = DA.collect_evidence(adaptive, str(self.root / "absent.sqlite3"), [])
        self.assertEqual(evidence["factor_effectiveness"]["report_hash"], report["report_hash"])
        self.assertIn("已执行检查", evidence["deterministic_findings_meaning"])
        self.assertEqual(len(digest), 64)
        build.assert_called_once()
        prompt = AR._build_tuning_system_prompt()
        self.assertIn("insufficient_dates", prompt)
        self.assertIn("不能据此估算", prompt)

    def test_daily_ic_uses_equal_date_weights(self):
        for strength in range(1, 7):
            self.add(self.sessions[0], strength)
        for strength in range(1, 6):
            self.add(self.sessions[1], strength, factors={"mom_short": -strength, "flow": 1})
        metric = self.metric(self.build())
        self.assertEqual(metric["mean_rank_ic"], 0)
        self.assertEqual(metric["positive_ic_ratio"], 0.5)
        self.assertEqual(metric["status"], "insufficient_dates")

    def test_tied_factor_tails_are_not_arbitrarily_split(self):
        for strength in range(1, 7):
            self.add(self.sessions[0], strength, factors={"mom_short": int(strength == 6), "flow": 1})
        metric = self.metric(self.build())
        self.assertIsNotNone(metric["mean_rank_ic"])
        self.assertIsNone(metric["high_minus_low_pp"])
        self.assertEqual(metric["group_return_dates"], 0)

    def test_same_stock_on_same_decision_day_is_one_observation(self):
        for day in self.sessions[:2]:
            self.add(day, 1, created_at=self.sessions[2] + "T16:00:00+08:00")
        report = self.build()
        self.assertEqual(report["coverage"]["candidate_rows"], 1)
        self.assertEqual(report["coverage"]["rejected"]["duplicate_daily_observation"], 1)

    def test_exit_day_before_close_remains_pending(self):
        self.add(self.sessions[0], 1)
        report = self.build(asof=self.sessions[2] + "T10:00:00+08:00")
        self.assertEqual(report["groups"][0]["label_counts"]["1"], {"pending": 1})

    def test_frozen_decision_snapshot_takes_precedence_over_mutable_metadata(self):
        self.add(self.sessions[0], 1, created_at=self.sessions[1] + "T16:00:00+08:00")
        payload = json.loads(self.conn.execute("SELECT payload FROM paper_signals").fetchone()[0])
        payload["decision_snapshot"] = {"decision_at": self.sessions[0] + "T16:00:00+08:00",
            "asof": self.sessions[0], "factors": {"raw": {"mom_short": 42, "flow": 1},
                "score_components": payload["pick"]["score_components"]}}
        self.conn.execute("UPDATE paper_signals SET payload=?", (json.dumps(payload),))
        self.conn.commit()
        sample = self.build(include_samples=True)["samples"][0]
        self.assertEqual(sample["factors"]["mom_short"], 42)
        self.assertEqual(sample["labels"]["1"]["entry_date"], self.sessions[1])
        self.assertEqual(sample["factor_source"], "decision_snapshot.factors.raw")

    def test_summary_keeps_simultaneous_configurations_including_unknown_identity(self):
        self.add(self.sessions[0], 1)
        self.add(self.sessions[0], 2, version=None)
        summary = FE.evidence_summary(self.build())
        self.assertEqual(len(summary["groups"]), 2)
        complete = next(g for g in summary["groups"] if g["identity_status"] == "complete")
        unknown = next(g for g in summary["groups"] if g["identity_status"] == "incomplete")
        self.assertTrue(complete["factors"])
        self.assertEqual(unknown["factors"], [])
        self.assertEqual(complete["weights"], {"mom_short": 0.5, "flow": 0.5})


if __name__ == "__main__":
    unittest.main()
