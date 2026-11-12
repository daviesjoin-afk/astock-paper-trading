import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from verify_ab import VerificationError, verify


DAYS = ("2026-11-02", "2026-11-03", "2026-11-04")


def point(day, equity, profit, rate, drawdown, trough=None):
    return dict(day=day, equity_cents=equity, net_flow_cents=0,
                profit_cents=profit, return_bps=rate, max_drawdown_bps=drawdown,
                peak_day=None, trough_day=trough)


def report_fixture():
    curves = {
        "account": [point(DAYS[0], 999_500, -500, -5, 5, DAYS[0]),
                    point(DAYS[1], 998_500, -1_500, -15, 15, DAYS[1]),
                    point(DAYS[2], 1_009_500, 9_500, 95, 15, DAYS[1])],
        "steady": [point(DAYS[0], 499_500, -500, -10, 10, DAYS[0]),
                   point(DAYS[1], 498_500, -1_500, -30, 30, DAYS[1]),
                   point(DAYS[2], 509_500, 9_500, 190, 30, DAYS[1])],
        "aggressive": [point(day, 500_000, 0, 0, 0) for day in DAYS],
    }
    position = dict(symbol="600000.SH", quantity=100)
    variant = dict(
        trades=[dict(id=1, slot="steady", symbol="600000.SH", side="buy",
                     quantity=100, price_cents=1_000, fees_cents=500,
                     trade_day=DAYS[0])],
        rebalances=[], weights_bps=[5_000, 5_000], fills=1, fees_cents=500,
        books=[dict(slot="steady", cash_cents=399_500,
                    positions=[position], sellable=[position]),
               dict(slot="aggressive", cash_cents=500_000,
                    positions=[], sellable=[])],
        slots=[dict(slot="steady", performance=curves["steady"][-1],
                    fills=1, fees_cents=500, invested_days=3, open_positions=1,
                    net_transferred_cents=0),
               dict(slot="aggressive", performance=curves["aggressive"][-1],
                    fills=0, fees_cents=0, invested_days=0, open_positions=0,
                    net_transferred_cents=0)],
        account=curves["account"][-1], curves=curves,
    )
    return dict(
        input=dict(adjust="none", initial_cash_cents=1_000_000,
                   windows=[dict(day=day) for day in DAYS],
                   **{"from": DAYS[0], "through": DAYS[-1], "settled_days": 3}),
        unfiltered=copy.deepcopy(variant), filtered=copy.deepcopy(variant),
    )


class VerifyABTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "quotes.sqlite3"
        self.report = self.root / "report.json"
        self.report.write_text(json.dumps(report_fixture()), encoding="utf-8")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "CREATE TABLE bar(symbol TEXT,day TEXT,close_cents INTEGER,"
                "open_cents INTEGER,volume_shares INTEGER,adjust TEXT,source TEXT,"
                "prev_close_cents INTEGER,tradable INTEGER,PRIMARY KEY(symbol,day))")
            connection.executemany(
                "INSERT INTO bar VALUES(?,?,?,?,?,'none','fixture',?,?)",
                [("600000.SH", DAYS[0], 1_000, 1_000, 10_000, 1_000, 1),
                 ("600000.SH", DAYS[1], 990, 0, 0, 990, 0),
                 ("600000.SH", DAYS[2], 1_100, 1_000, 10_000, 990, 1),
                 *[("000002.SZ", day, 2_000, 2_000, 10_000, 2_000, 1)
                   for day in DAYS]])

    def test_halted_reference_marks_existing_position_and_resumes_without_fills(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        result = verify(self.report, self.database)
        self.assertTrue(result["passed"])
        self.assertEqual(result["checked_days"], 3)
        for variant in result["variants"].values():
            self.assertEqual(variant["halted_fill_count"], 0)
            self.assertEqual(variant["halted_holding_marks"]["slot_symbol_days"], 1)
            self.assertEqual(
                variant["halted_holding_marks"]["first_examples"][0]["market_value_cents"],
                99_000)
            self.assertEqual(variant["halted_resumptions"]["resolved"], 1)
            self.assertEqual(variant["halted_resumptions"]["unresolved_at_end"], 0)
            self.assertEqual(
                variant["halted_resumptions"]["first_examples"][0]["resumed_day"], DAYS[2])
            self.assertEqual(variant["final_performance"]["account"]["equity_cents"], 1_009_500)
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_nontradable_fill_fails_even_with_positive_open_and_volume(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE bar SET tradable=0 WHERE symbol='600000.SH' AND day=?",
                               (DAYS[0],))
        result = verify(self.report, self.database)
        self.assertFalse(result["passed"])
        self.assertFalse(result["quote_tradeability_passed"])
        for variant in result["variants"].values():
            self.assertEqual(variant["halted_fill_count"], 1)
            self.assertEqual(variant["untradeable_fill_count"], 0)
            self.assertIn("fill_tradability", [row["category"] for row in variant["mismatches"]])

    def test_halted_mark_must_equal_official_reference(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE bar SET prev_close_cents=980 WHERE symbol='600000.SH' AND day=?",
                               (DAYS[1],))
        result = verify(self.report, self.database)
        self.assertFalse(result["passed"])
        for variant in result["variants"].values():
            self.assertIn("halted_reference_price", [row["category"] for row in variant["mismatches"]])

    def test_absent_held_quote_is_an_explicit_failure(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM bar WHERE symbol='600000.SH' AND day=?", (DAYS[1],))
        with self.assertRaisesRegex(VerificationError, "required source quote missing: 600000.SH"):
            verify(self.report, self.database)


if __name__ == "__main__":
    unittest.main()
