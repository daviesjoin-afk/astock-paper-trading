#!/usr/bin/env python3
"""Offline tests for the observer's self-measured track record.

The rule under test: a wrong block must be counted as a cost, a name sealed at
its ceiling must not be counted at all, and a thin sample must not be reported
as if it were evidence.
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
CREATE TABLE round(round_id TEXT PRIMARY KEY, regime TEXT);
CREATE TABLE quote_snapshot(round_id TEXT, symbol TEXT, price_cents INTEGER,
                            limit_up_cents INTEGER);
CREATE TABLE ai_observation(round_id TEXT, application_json TEXT);
"""


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


def observe(db, round_id, regime, blocked, buys=()):
    db.execute("INSERT INTO round VALUES(?,?)", (round_id, regime))
    db.execute("INSERT INTO ai_observation VALUES(?,?)", (round_id, json.dumps({
        "accepted": True, "entry_delta": 6, "bias": -6,
        "blocked_symbols": list(blocked),
        "active_suggestions": [{"symbol": s, "side": "buy"} for s in buys],
    })))


def quote(db, round_id, symbol, price, limit_up=None):
    db.execute("INSERT INTO quote_snapshot VALUES(?,?,?,?)",
               (round_id, symbol, price, limit_up))


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
                close = f"{day}-9999"
                db.execute("INSERT INTO round VALUES(?,?)", (close, "risk_off"))
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
                close = f"{day}-9999"
                db.execute("INSERT INTO round VALUES(?,?)", (close, "mixed"))
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
                close = f"{day}-9999"
                db.execute("INSERT INTO round VALUES(?,?)", (close, "risk_on"))
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


if __name__ == "__main__":
    unittest.main()
