from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import backfill_halted
from ingest_market import Row


DAY = "2026-11-03"


def halted_row(symbol):
    return Row(symbol=symbol, day=DAY, name="fixture", open_cents=0,
               high_cents=0, low_cents=0, close_cents=990,
               prev_close_cents=990, volume_shares=0, amount_cents=0,
               tradable=False)


class BackfillHaltedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "research-copy.sqlite3"
        self.manifest = self.root / "manifest.json"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "CREATE TABLE bar(symbol TEXT,day TEXT,name TEXT,open_cents INTEGER,"
                "high_cents INTEGER,low_cents INTEGER,close_cents INTEGER,prev_close_cents INTEGER,"
                "volume_shares INTEGER,amount_cents INTEGER,adjust TEXT,source TEXT,"
                "tradable INTEGER,PRIMARY KEY(symbol,day))")
            connection.execute(
                "INSERT INTO bar VALUES('600000.SH',?,'fixture',0,0,0,990,990,0,0,'none','tdx',1)",
                (DAY,))

    def run_backfill(self, rows):
        with patch.object(backfill_halted, "fetch", return_value=(b"package", "hash")), \
             patch.object(backfill_halted, "parse_package", return_value=rows):
            return backfill_halted.backfill(self.database, self.manifest, self.root / "packages")

    def test_existing_carried_quote_is_reclassified_and_missing_halt_inserted(self):
        result = self.run_backfill([halted_row("600000.SH"), halted_row("000001.SZ")])
        self.assertEqual(result["days"][DAY]["inserted"], 1)
        self.assertEqual(result["days"][DAY]["updated"], 1)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT symbol,close_cents,prev_close_cents,tradable FROM bar ORDER BY symbol"
            ).fetchall(), [("000001.SZ", 990, 990, 0), ("600000.SH", 990, 990, 0)])
        resumed = self.run_backfill([halted_row("600000.SH"), halted_row("000001.SZ")])
        self.assertEqual(resumed, result)

    def test_official_halt_conflicting_with_a_traded_row_fails_without_daily_writes(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE bar SET open_cents=990,high_cents=990,low_cents=990,volume_shares=100")
        with self.assertRaisesRegex(ValueError, "official halt conflicts with traded source row"):
            self.run_backfill([halted_row("000001.SZ"), halted_row("600000.SH")])
        with closing(sqlite3.connect(self.database)) as connection, connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM bar").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT tradable,volume_shares FROM bar").fetchone(), (1, 100))
        self.assertFalse(self.manifest.exists())


if __name__ == "__main__":
    unittest.main()
