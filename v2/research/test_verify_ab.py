import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest

from verify_ab import VerificationError, bar_projection, verify


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
        # Project-local isolation: Windows /tmp and native paths can diverge.
        self.root = Path(__file__).resolve().parents[1] / "work" / "p0-research-tests" / self._testMethodName
        if self.root.exists():
            for child in self.root.iterdir():
                if child.is_file():
                    child.unlink()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "quotes.sqlite3"
        self.report = self.root / "report.json"
        self.report.write_text(json.dumps(report_fixture()), encoding="utf-8")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "CREATE TABLE bar(symbol TEXT,day TEXT,close_cents INTEGER,"
                "open_cents INTEGER,high_cents INTEGER NOT NULL DEFAULT 0,"
                "low_cents INTEGER NOT NULL DEFAULT 0,volume_shares INTEGER,"
                "adjust TEXT,source TEXT,prev_close_cents INTEGER,tradable INTEGER,"
                "PRIMARY KEY(symbol,day))")
            connection.executemany(
                "INSERT INTO bar(symbol,day,close_cents,open_cents,volume_shares,"
                "adjust,source,prev_close_cents,tradable) "
                "VALUES(?,?,?,?,?,'none','fixture',?,?)",
                [("600000.SH", DAYS[0], 1_000, 1_000, 10_000, 1_000, 1),
                 ("600000.SH", DAYS[1], 990, 0, 0, 990, 0),
                 ("600000.SH", DAYS[2], 1_100, 1_000, 10_000, 990, 1),
                 *[("000002.SZ", day, 2_000, 2_000, 10_000, 2_000, 1)
                   for day in DAYS]])

    def test_legacy_column_set_projects_halt_without_modifying_the_database(self):
        legacy = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as connection, connection:
            connection.execute(
                "CREATE TABLE bar(symbol TEXT,day TEXT,close_cents INTEGER,"
                "open_cents INTEGER,high_cents INTEGER NOT NULL DEFAULT 0,"
                "low_cents INTEGER NOT NULL DEFAULT 0,volume_shares INTEGER,"
                "adjust TEXT,source TEXT,prev_close_cents INTEGER,"
                "PRIMARY KEY(symbol,day))")
            connection.executemany(
                "INSERT INTO bar(symbol,day,close_cents,open_cents,volume_shares,"
                "adjust,source,prev_close_cents) VALUES(?,?,?,?,?,'none','tdx',?)",
                [("600000.SH", DAYS[0], 1_000, 1_000, 10_000, 1_000),
                 ("600000.SH", DAYS[1], 480, 0, 0, 990),
                 ("600000.SH", DAYS[2], 1_100, 1_000, 10_000, 990),
                 ("000002.SZ", DAYS[0], 2_000, 2_000, 10_000, 2_000),
                 ("000002.SZ", DAYS[1], 2_000, 2_000, 10_000, 2_000),
                 ("000002.SZ", DAYS[2], 2_000, 2_000, 10_000, 2_000)])
        before = hashlib.sha256(legacy.read_bytes()).hexdigest()
        with closing(sqlite3.connect(f"file:{legacy.resolve().as_posix()}?mode=ro", uri=True)) as connection:
            projection = bar_projection(connection)
            self.assertIn("CASE", projection[0])
            projected = connection.execute(
                f"SELECT {projection[0]},{projection[1]} FROM bar "
                "WHERE symbol='600000.SH' AND day=?", (DAYS[1],)).fetchone()
            self.assertEqual(projected, (990, 0))
        self.assertEqual(before, hashlib.sha256(legacy.read_bytes()).hexdigest())

    def test_no_reliable_reference_price_fails_closed_in_legacy_projection(self):
        legacy = self.root / "legacy-no-reference.sqlite3"
        with closing(sqlite3.connect(legacy)) as connection, connection:
            connection.execute(
                "CREATE TABLE bar(symbol TEXT,day TEXT,close_cents INTEGER,"
                "open_cents INTEGER,high_cents INTEGER NOT NULL DEFAULT 0,"
                "low_cents INTEGER NOT NULL DEFAULT 0,volume_shares INTEGER,"
                "adjust TEXT,source TEXT,prev_close_cents INTEGER,"
                "PRIMARY KEY(symbol,day))")
            connection.execute(
                "INSERT INTO bar(symbol,day,close_cents,open_cents,volume_shares,"
                "adjust,source,prev_close_cents) VALUES('600000.SH',?,0,0,0,'none','tdx',0)",
                (DAYS[0],))
        before = hashlib.sha256(legacy.read_bytes()).hexdigest()
        with closing(sqlite3.connect(f"file:{legacy.resolve().as_posix()}?mode=ro", uri=True)) as connection:
            projection = bar_projection(connection)
            projected = connection.execute(
                f"SELECT {projection[0]},{projection[1]} FROM bar").fetchone()
            # No official previous close means zero projected mark and no fill.
            # The >0 research projection filter excludes this row entirely.
            self.assertEqual(projected, (0, 0))
            visible = connection.execute(
                f"SELECT count(*) FROM bar WHERE {projection[2]}>0").fetchone()[0]
            self.assertEqual(visible, 0)
        self.assertEqual(before, hashlib.sha256(legacy.read_bytes()).hexdigest())

    def test_unsupported_column_set_is_diagnosed_early(self):
        unsupported = self.root / "unsupported.sqlite3"
        with closing(sqlite3.connect(unsupported)) as connection:
            connection.execute("CREATE TABLE bar(symbol TEXT, day TEXT, close_cents INTEGER)")
        with closing(sqlite3.connect(f"file:{unsupported.resolve().as_posix()}?mode=ro", uri=True)) as connection:
            with self.assertRaisesRegex(VerificationError, "unsupported_bar_column_set"):
                bar_projection(connection)

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


class StopFillAuditTests(unittest.TestCase):
    """A resting stop may fill away from the close, but only inside the range.

    Before stops could fill at their line the audit demanded price == close for
    every fill. That is still the rule for a normal order; it had to widen by
    exactly one case, and the widened rule has to stay strict enough to catch a
    genuinely wrong fill price.
    """

    def setUp(self):
        self.root = (Path(__file__).resolve().parents[1] / "work"
                     / "p0-research-tests" / self._testMethodName)
        if self.root.exists():
            for child in self.root.iterdir():
                if child.is_file():
                    child.unlink()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "quotes.sqlite3"
        self.report = self.root / "report.json"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "CREATE TABLE bar(symbol TEXT,day TEXT,close_cents INTEGER,"
                "open_cents INTEGER,high_cents INTEGER NOT NULL DEFAULT 0,"
                "low_cents INTEGER NOT NULL DEFAULT 0,volume_shares INTEGER,"
                "adjust TEXT,source TEXT,prev_close_cents INTEGER,tradable INTEGER,"
                "PRIMARY KEY(symbol,day))")

    def write_quote(self, close, low):
        """Write all three fixture days so the period stays complete."""
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executemany(
                "INSERT INTO bar(symbol,day,close_cents,open_cents,high_cents,"
                "low_cents,volume_shares,adjust,source,prev_close_cents,tradable) "
                "VALUES(?,?,?,?,?,?,?,'none','fixture',?,1)",
                [("600000.SH", DAYS[0], close, close, close, low, 10_000, close),
                 ("600000.SH", DAYS[1], close, close, close, low, 10_000, close),
                 ("600000.SH", DAYS[2], close, close, close, low, 10_000, close)])

    def run_audit(self, fill_price):
        report = report_fixture()
        for variant in ("unfiltered", "filtered"):
            report[variant]["trades"][0]["price_cents"] = fill_price
        self.report.write_text(json.dumps(report), encoding="utf-8")
        return verify(self.report, self.database)

    def fill_price_passed(self, price):
        """Whether the *price rule* accepted this fill.

        The fixture's cash was built for a 1000 fill, so a different price also
        breaks the ledger arithmetic. That is a fixture artefact, not the rule
        under test, so assert on the price check alone.
        """
        result = self.run_audit(price)
        mismatches = result["variants"]["filtered"].get("mismatches") or []
        return not any(e["category"] == "fill_price" for e in mismatches)

    def test_a_stop_filling_below_the_close_inside_the_range_passes(self):
        # Sold at 990 while the close was 1000: a stop that filled at its line.
        self.write_quote(1_000, 985)
        self.assertTrue(self.fill_price_passed(990),
                        "a fill inside the session range must be accepted")

    def test_a_fill_below_the_session_low_still_fails(self):
        # A price the session never traded is an accounting break, not a stop.
        self.write_quote(1_000, 985)
        self.assertFalse(self.fill_price_passed(975),
                         "a fill below the low cannot be a legitimate stop")

    def test_a_fill_without_a_range_still_must_equal_the_close(self):
        # No range carried (low=0) means the old close-only rule applies.
        self.write_quote(1_000, 0)
        self.assertFalse(self.fill_price_passed(1_050),
                         "without a range a fill must still equal the close")


if __name__ == "__main__":
    unittest.main()
