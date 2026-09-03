# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_ledger_reader as reader


class PaperLedgerReaderTests(unittest.TestCase):
    def test_connection_is_read_only_and_returns_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper.sqlite3")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
            conn.execute("INSERT INTO sample(value) VALUES ('ok')")
            conn.commit()
            conn.close()

            paper = reader.connect(path, timeout=2)
            try:
                row = paper.execute("SELECT value FROM sample").fetchone()
                self.assertEqual(row["value"], "ok")
                with self.assertRaises(sqlite3.OperationalError):
                    paper.execute("INSERT INTO sample(value) VALUES ('blocked')")
            finally:
                paper.close()

    def test_missing_database_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(sqlite3.OperationalError):
                reader.connect(os.path.join(directory, "missing.sqlite3"))


if __name__ == "__main__":
    unittest.main()
