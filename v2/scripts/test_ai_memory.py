#!/usr/bin/env python3
"""Offline tests for the observer's self-measured track record.

The rules under test: a wrong block must be counted as a cost, a name sealed at
its ceiling must not be counted at all, a thin sample must not be reported as if
it were evidence, only a closed session may be scored, and a stock's move must
be compared against the market over the same window.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_memory  # noqa: E402

SCHEMA = """
CREATE TABLE round(round_id TEXT PRIMARY KEY, regime TEXT, finished_at TEXT);
CREATE TABLE quote_snapshot(round_id TEXT, symbol TEXT, price_cents INTEGER,
                            limit_up_cents INTEGER);
CREATE TABLE ai_observation(round_id TEXT, application_json TEXT);
"""

# A round that ran to its close. The scheduler's last in-session round is 14:57
# for a 15:00 close, and the guard is 14:50.
CLOSE_STAMP = "2026-11-20T14:57:00+08:00"
INTRADAY_STAMP = "2026-11-20T10:30:00+08:00"


@contextlib.contextmanager
def ledger(root: Path):
    """A temporary ledger that is closed before the directory is removed.

    On Windows an open SQLite handle keeps the file locked and the temp-dir
    cleanup fails with WinError 32.
    """
    db = make_db(root)
    try:
        yield db
    finally:
        db.close()


def make_db(root: Path) -> sqlite3.Connection:
    db = sqlite3.connect(root / "ledger.sqlite3")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def observe(db, round_id, regime, blocked, buys=(), *, stamp=INTRADAY_STAMP):
    db.execute("INSERT INTO round VALUES(?,?,?)", (round_id, regime, stamp))
    db.execute("INSERT INTO ai_observation VALUES(?,?)", (round_id, json.dumps({
        "accepted": True, "entry_delta": 6, "bias": -6,
        "blocked_symbols": list(blocked),
        "active_suggestions": [{"symbol": s, "side": "buy"} for s in buys],
    })))


def quote(db, round_id, symbol, price, limit_up=None):
    db.execute("INSERT INTO quote_snapshot VALUES(?,?,?,?)",
               (round_id, symbol, price, limit_up))


def close_round(db, day, regime="risk_off"):
    """The day's closing round: its stamp is what marks the session as closed."""
    close = f"{day}-145700"
    db.execute("INSERT INTO round VALUES(?,?,?)", (close, regime, CLOSE_STAMP))
    return close


class TrackRecordTests(unittest.TestCase):
    def test_a_block_that_later_rose_is_recorded_as_a_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with ledger(root) as db:
                day = "2026-11-20"
                for i in range(25):
                    rid = f"{day}-{i:04d}"
                    observe(db, rid, "risk_off", ["600000.SH"])
                    quote(db, rid, "600000.SH", 1000)
                close = close_round(db, day)
                quote(db, close, "600000.SH", 1010)
                db.commit()
                card = ai_memory.scorecard(ai_memory.collect(db))
                blocked = card["by_regime"]["risk_off"]["blocked_symbols"]
                self.assertEqual(blocked["cases"], 25)
                self.assertEqual(blocked["mean_bps"], 100)
                self.assertGreater(blocked["mean_bps"], 0, "blocking a riser costs money")

    def test_a_name_sealed_at_its_ceiling_is_not_a_missed_gain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with ledger(root) as db:
                day = "2026-11-20"
                for i in range(25):
                    rid = f"{day}-{i:04d}"
                    observe(db, rid, "mixed", ["600001.SH"])
                    quote(db, rid, "600001.SH", 1100, limit_up=1100)  # sealed
                close = close_round(db, day, "mixed")
                quote(db, close, "600001.SH", 1100)
                db.commit()
                card = ai_memory.scorecard(ai_memory.collect(db))
                self.assertNotIn("mixed", card["by_regime"],
                                 "nothing buyable was given up, so nothing is reported")

    def test_a_thin_sample_is_omitted_rather_than_averaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with ledger(root) as db:
                day = "2026-11-20"
                for i in range(3):
                    rid = f"{day}-{i:04d}"
                    observe(db, rid, "risk_on", ["600002.SH"], buys=["600002.SH"])
                    quote(db, rid, "600002.SH", 1000)
                close = close_round(db, day, "risk_on")
                quote(db, close, "600002.SH", 900)
                db.commit()
                card = ai_memory.scorecard(ai_memory.collect(db))
                self.assertNotIn("risk_on", card["by_regime"])

    def test_attach_is_a_no_op_without_a_card(self):
        import live_ai
        digest = {"sample_count": 1}
        self.assertEqual(live_ai.attach_track_record(digest, None), {"sample_count": 1})
        self.assertNotIn("own_track_record", digest)
        card = {"by_regime": {"risk_off": {"blocked_symbols": {"mean_bps": 26}}}}
        out = live_ai.attach_track_record({}, card)
        self.assertIs(out["own_track_record"], card)


class ClosedSessionTests(unittest.TestCase):
    """A session that stopped at 10:30 leaves a midday price, not a close."""

    def build(self, db, *, close_stamp):
        day = "2026-11-20"
        for i in range(25):
            rid = f"{day}-{i:04d}"
            observe(db, rid, "mixed", ["600000.SH"])
            quote(db, rid, "600000.SH", 1000)
        close = f"{day}-145700"
        db.execute("INSERT INTO round VALUES(?,?,?)", (close, "mixed", close_stamp))
        quote(db, close, "600000.SH", 1010)
        db.commit()
        return day

    def test_a_session_that_never_reached_its_close_is_not_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ledger(Path(tmp)) as db:
                self.build(db, close_stamp="2026-11-20T10:30:00+08:00")
                memory = ai_memory.collect(db)
                self.assertEqual(memory["blocked"], [],
                                 "an unfinished session yields no scored call")
                self.assertEqual(memory["skipped_sessions"][0]["reason"],
                                 "session_not_closed")
                card = ai_memory.scorecard(memory)
                self.assertEqual(card["by_regime"], {})
                self.assertEqual(card["sessions"], 0)
                self.assertEqual(card["skipped_sessions"], 1)

    def test_a_session_whose_close_time_cannot_be_read_is_not_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ledger(Path(tmp)) as db:
                self.build(db, close_stamp="")
                memory = ai_memory.collect(db)
                self.assertEqual(memory["blocked"], [])
                self.assertEqual(memory["skipped_sessions"][0]["reason"],
                                 "close_time_unparseable")

    def test_a_closed_session_is_scored_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ledger(Path(tmp)) as db:
                self.build(db, close_stamp=CLOSE_STAMP)
                card = ai_memory.scorecard(ai_memory.collect(db))
                self.assertEqual(card["sessions"], 1)
                self.assertEqual(card["skipped_sessions"], 0)
                self.assertTrue(card["closed_sessions_only"])
                self.assertEqual(
                    card["by_regime"]["mixed"]["blocked_symbols"]["cases"], 25)


class SameWindowTests(unittest.TestCase):
    """The stock's move and its benchmark must cover the same stretch of time."""

    def test_the_benchmark_is_measured_from_the_same_round_as_the_call(self):
        # The market doubles between the open and the close, so an
        # opening-to-close median would report a huge negative excess for a name
        # that simply rose with its benchmark from the entry round.
        with tempfile.TemporaryDirectory() as tmp:
            with ledger(Path(tmp)) as db:
                day = "2026-11-20"
                for i in range(25):
                    rid = f"{day}-{i:04d}"
                    observe(db, rid, "risk_on", ["600000.SH"])
                    # the blocked name sits flat at 1000 from 10:30 on
                    quote(db, rid, "600000.SH", 1000)
                    # the rest of the market is also flat from 10:30 on
                    quote(db, rid, "000001.SZ", 2000)
                close = close_round(db, day, "risk_on")
                quote(db, close, "600000.SH", 1010)   # +100 bps over the window
                quote(db, close, "000001.SZ", 2020)   # +100 bps over the window
                db.commit()
                case = ai_memory.collect(db)["blocked"][0]
                self.assertEqual(case["move_bps"], 100)
                self.assertEqual(case["baseline_bps"], 100,
                                 "the median must be the same-window move")
                self.assertEqual(case["excess_bps"], 0)
                card = ai_memory.scorecard(ai_memory.collect(db))
                blocked = card["by_regime"]["risk_on"]["blocked_symbols"]
                self.assertEqual(blocked["excess_bps"], 0,
                                 "matching the market is not alpha")

    def test_excess_is_positive_only_when_the_name_beats_the_same_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ledger(Path(tmp)) as db:
                day = "2026-11-20"
                for i in range(25):
                    rid = f"{day}-{i:04d}"
                    observe(db, rid, "risk_on", ["600000.SH"])
                    quote(db, rid, "600000.SH", 1000)
                    # A market of five names, four of which rise 100 bps: the
                    # median is the market, not the blocked name's own move.
                    for j, symbol in enumerate(("000001.SZ", "000002.SZ",
                                                "000004.SZ", "000005.SZ")):
                        quote(db, rid, symbol, 1000)
                close = close_round(db, day, "risk_on")
                quote(db, close, "600000.SH", 1030)   # +300 bps
                for symbol in ("000001.SZ", "000002.SZ", "000004.SZ", "000005.SZ"):
                    quote(db, close, symbol, 1010)    # +100 bps median
                db.commit()
                case = ai_memory.collect(db)["blocked"][0]
                self.assertEqual(case["window"],
                                 {"from_round": f"{day}-0000", "to_round": close})
                self.assertEqual(case["baseline_bps"], 100)
                self.assertEqual(case["excess_bps"], 200)
                card = ai_memory.scorecard(ai_memory.collect(db))
                self.assertEqual(
                    card["by_regime"]["risk_on"]["blocked_symbols"]["excess_bps"], 200)

    def test_a_mid_session_call_is_not_scored_against_an_opening_baseline(self):
        # Same numbers as the previous test, but the call is made at 13:00 when
        # both names already doubled. An open-to-close baseline would report a
        # ~-9000 bps excess for a name that simply tracked its market.
        with tempfile.TemporaryDirectory() as tmp:
            with ledger(Path(tmp)) as db:
                day = "2026-11-20"
                open_rid = f"{day}-093000"
                db.execute("INSERT INTO round VALUES(?,?,?)",
                           (open_rid, "risk_on", "2026-11-20T09:30:00+08:00"))
                quote(db, open_rid, "600000.SH", 1000)
                quote(db, open_rid, "000001.SZ", 1000)
                for i in range(25):
                    rid = f"{day}-13{i:04d}"
                    observe(db, rid, "risk_on", ["600000.SH"],
                            stamp="2026-11-20T13:05:00+08:00")
                    quote(db, rid, "600000.SH", 2000)
                    quote(db, rid, "000001.SZ", 2000)
                close = close_round(db, day, "risk_on")
                quote(db, close, "600000.SH", 2020)
                quote(db, close, "000001.SZ", 2020)
                db.commit()
                case = ai_memory.collect(db)["blocked"][0]
                self.assertEqual(case["move_bps"], 100)
                self.assertEqual(case["excess_bps"], 0,
                                 "no phantom alpha from a mismatched window")


class NonExecutableTests(unittest.TestCase):
    """A counterfactual that could not have been traded must be labelled."""

    def test_a_sealed_name_is_carried_but_labelled_non_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ledger(Path(tmp)) as db:
                day = "2026-11-20"
                for i in range(25):
                    rid = f"{day}-{i:04d}"
                    observe(db, rid, "mixed", ["600001.SH"])
                    quote(db, rid, "600001.SH", 1100, limit_up=1100)
                close = close_round(db, day, "mixed")
                quote(db, close, "600001.SH", 1210)
                db.commit()
                memory = ai_memory.collect(db)
                case = memory["blocked"][0]
                self.assertFalse(case["executable"])
                self.assertEqual(case["non_executable_reason"], "sealed_at_limit_up")
                self.assertIsNone(case["excess_bps"],
                                  "a sealed name has no tradeable counterfactual")

    def test_a_named_buy_without_a_tradable_quote_is_labelled_and_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ledger(Path(tmp)) as db:
                day = "2026-11-20"
                for i in range(25):
                    rid = f"{day}-{i:04d}"
                    observe(db, rid, "mixed", [], buys=["600003.SH"])
                    quote(db, rid, "000001.SZ", 1000)     # benchmark only
                close = close_round(db, day, "mixed")
                quote(db, close, "000001.SZ", 1010)
                db.commit()
                memory = ai_memory.collect(db)
                case = memory["buys"][0]
                self.assertFalse(case["executable"])
                self.assertEqual(case["non_executable_reason"], "no_tradable_quote")
                self.assertIsNone(case["move_bps"])
                card = ai_memory.scorecard(memory)
                self.assertIsNone(card["by_regime"]["mixed"]["named_buys"],
                                  "an unexecutable buy cannot become an average")

    def test_non_executable_cases_are_reported_but_do_not_shift_the_average(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ledger(Path(tmp)) as db:
                day = "2026-11-20"
                for i in range(25):
                    rid = f"{day}-{i:04d}"
                    observe(db, rid, "mixed", ["600000.SH", "600001.SH"])
                    quote(db, rid, "600000.SH", 1000)
                    quote(db, rid, "600001.SH", 1100, limit_up=1100)
                close = close_round(db, day, "mixed")
                quote(db, close, "600000.SH", 1010)
                quote(db, close, "600001.SH", 1210)   # would be +1000 if counted
                db.commit()
                card = ai_memory.scorecard(ai_memory.collect(db))
                blocked = card["by_regime"]["mixed"]["blocked_symbols"]
                self.assertEqual(blocked["cases"], 25, "only the tradeable name counts")
                self.assertEqual(blocked["non_executable"], 25)
                self.assertEqual(blocked["mean_bps"], 100,
                                 "the sealed name would have doubled this average")
                self.assertEqual(blocked["excess_cases"], 25)


if __name__ == "__main__":
    unittest.main()
