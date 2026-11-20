#!/usr/bin/env python3
"""Offline tests for the fold diagnosis.

The behaviour under test is the one that took two wrong guesses to find: a book
that buys breakouts and exits on the first pullback churns, and churn is
visible in the holding period before it is visible in the return.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fold_diagnose import analyse, load_from_replay  # noqa: E402


def trade(day: str, side: str, symbol: str = "600000.SH", reason: str = "x",
          price: int = 1_000, qty: int = 1_000, basis: dict | None = None) -> dict:
    return {
        "slot": "aggressive", "symbol": symbol, "side": side,
        "quantity": qty, "price_cents": price, "fees_cents": 500,
        "day": day, "reason": reason, "basis": basis or {},
        "notional_cents": qty * price,
    }


class TestHoldingPeriod(unittest.TestCase):
    def test_a_round_trip_of_three_days_is_measured(self):
        trades = [trade("d0", "buy"), trade("d3", "sell")]
        result = analyse(trades)
        self.assertEqual(result["holding_period"]["median_days"], 1)
        # day_order is over the days that traded, so d0 -> d3 is one step.
        self.assertEqual(result["holding_period"]["closed"], 1)

    def test_fast_churn_is_flagged(self):
        # Five round trips, each held one session: well past the 60% threshold.
        trades = []
        for index in range(5):
            trades.append(trade(f"d{index * 2}", "buy", symbol=f"S{index}"))
            trades.append(trade(f"d{index * 2 + 1}", "sell", symbol=f"S{index}"))
        result = analyse(trades)
        findings = [item["finding"] for item in result["findings"]]
        self.assertIn("churns_faster_than_the_signal_can_work", findings)
        self.assertGreaterEqual(result["holding_period"]["share_within_5_days"], 0.6)

    def test_slow_turnover_is_not_flagged(self):
        # A real calendar of 120 sessions; each position is held 25 of them.
        calendar = [f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(120)]
        trades = [
            trade(calendar[0], "buy", symbol="A"),
            trade(calendar[25], "sell", symbol="A"),
            trade(calendar[30], "buy", symbol="B"),
            trade(calendar[55], "sell", symbol="B"),
            trade(calendar[60], "buy", symbol="C"),
            trade(calendar[85], "sell", symbol="C"),
        ]
        result = analyse(trades, calendar)
        findings = [item["finding"] for item in result["findings"]]
        self.assertNotIn("churns_faster_than_the_signal_can_work", findings)
        self.assertEqual(result["holding_period"]["median_days"], 25)

    def test_holding_is_measured_on_the_calendar_not_on_traded_days(self):
        # Two trades three calendar days apart, but only those two days traded.
        # Counting traded days would call this one session; the calendar says 3.
        calendar = ["d0", "d1", "d2", "d3"]
        trades = [trade("d0", "buy"), trade("d3", "sell")]
        result = analyse(trades, calendar)
        self.assertEqual(result["holding_period"]["median_days"], 3)

    def test_an_open_position_has_no_holding_period(self):
        result = analyse([trade("d0", "buy")])
        # None, not zero: nothing closed, so there is no period to average.
        self.assertIsNone(result["holding_period"])

    def test_exit_reasons_are_counted(self):
        trades = [
            trade("d0", "buy"),
            trade("d1", "sell", reason="momentum_lost"),
            trade("d2", "buy", symbol="S2"),
            trade("d3", "sell", symbol="S2", reason="momentum_lost"),
            trade("d4", "buy", symbol="S3"),
            trade("d5", "sell", symbol="S3", reason="stop_loss"),
        ]
        result = analyse(trades)
        self.assertEqual(result["exit_reasons"]["momentum_lost"], 2)
        self.assertEqual(result["exit_reasons"]["stop_loss"], 1)


class TestEntryAndSizing(unittest.TestCase):
    def test_blocked_entries_are_counted_from_the_basis(self):
        trades = [
            trade("d0", "buy", basis={"allow_entries": True}),
            trade("d1", "sell", basis={"allow_entries": False}),
            trade("d2", "sell", basis={"allow_entries": False}),
        ]
        result = analyse(trades)
        self.assertEqual(result["allow_entries_true"], 1)
        self.assertEqual(result["allow_entries_false"], 2)

    def test_a_majority_of_blocked_decisions_is_flagged(self):
        trades = [
            trade("d0", "buy", basis={"allow_entries": False}),
            trade("d1", "sell", basis={"allow_entries": False}),
            trade("d2", "buy", basis={"allow_entries": True}),
        ]
        findings = [item["finding"] for item in analyse(trades)["findings"]]
        self.assertIn("entries_blocked_majority", findings)

    def test_small_targets_are_flagged(self):
        trades = [
            trade("d0", "buy", basis={"target_weight_bps": 1_500}),
            trade("d1", "buy", symbol="S2", basis={"target_weight_bps": 1_500}),
        ]
        findings = [item["finding"] for item in analyse(trades)["findings"]]
        self.assertIn("target_weights_below_full", findings)

    def test_costly_turnover_is_flagged(self):
        # Fees are 5_000 on 10_000 of notional: 5_000 bps.
        heavy = [trade("d0", "buy", price=10, qty=1_000)]
        heavy[0]["fees_cents"] = 5_000
        findings = [item["finding"] for item in analyse(heavy)["findings"]]
        self.assertIn("turnover_costly", findings)

    def test_no_trades_says_nothing_rather_than_zero(self):
        result = analyse([])
        self.assertIn("note", result)
        self.assertEqual(result["trades"], 0)


class TestReplayReader(unittest.TestCase):
    def test_basis_travels_from_the_report(self):
        payload = {"filtered": {"trades": [{
            "slot": "steady", "symbol": "600000.SH", "side": "buy",
            "quantity": 100, "price_cents": 500, "fees_cents": 5,
            "trade_day": "2025-01-10", "reason": "trend",
            "basis": {"regime": "choppy", "allow_entries": True},
        }]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            trades = load_from_replay(path, "filtered")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["basis"]["regime"], "choppy")
        self.assertEqual(trades[0]["notional_cents"], 50_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
