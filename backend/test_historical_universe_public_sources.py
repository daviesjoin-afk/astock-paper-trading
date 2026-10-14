from __future__ import annotations

import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import historical_universe_public_sources as HUPS


class PublicHistoricalUniverseTests(unittest.TestCase):
    def test_xlsx_preserves_empty_columns_shared_strings_and_chinese_names(self):
        from io import BytesIO
        import zipfile
        book = BytesIO()
        ns = HUPS._XLSX_NS["m"]
        with zipfile.ZipFile(book, "w") as archive:
            archive.writestr("xl/workbook.xml", '<workbook xmlns="%s" xmlns:r="%s"><sheets><sheet r:id="r1"/></sheets></workbook>' % (ns, HUPS._XLSX_NS["r"]))
            archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
            archive.writestr("xl/sharedStrings.xml", '<sst xmlns="%s"><si><t>中文证券</t></si></sst>' % ns)
            archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="%s"><sheetData><row r="1"><c r="A1"><v>000001</v></c><c r="C1" t="s"><v>0</v></c><c r="D1" t="inlineStr"><is><t>2024-01-02</t></is></c></row></sheetData></worksheet>' % ns)
        self.assertEqual([["000001", "", "中文证券", "2024-01-02"]],
                         HUPS._xlsx_first_sheet_rows(book.getvalue()))

    def test_free_http_retry_uses_existing_adapter_for_query_pages(self):
        from unittest.mock import Mock
        session = Mock()
        with patch.object(HUPS.requests, "Session", return_value=session), \
                patch.object(HUPS, "_sse_rows", side_effect=HUPS.PublicUniverseSourceError("offline")):
            with self.assertRaises(HUPS.PublicUniverseSourceError):
                HUPS.fetch_public_security_master()
        adapter = session.mount.call_args.args[1]
        self.assertEqual(2, adapter.max_retries.total)
        self.assertIn("POST", adapter.max_retries.allowed_methods)
        session.close.assert_called_once()

    def test_normalizes_exchange_date_formats_and_rejects_invalid_dates(self):
        self.assertEqual(HUPS._as_day("20240102"), "2024-01-02")
        self.assertEqual(HUPS._as_day("2024/4/8"), "2024-04-08")
        with self.assertRaises(HUPS.PublicUniverseSourceError):
            HUPS._as_day("not-a-date")

    def test_archive_rows_create_a_delisting_revision(self):
        rows = HUPS._archive_rows([{
            "code": "600000.SH", "listed_from": "1999-11-10",
            "delisted_at": "2024-06-01", "security_type": "ordinary_share",
            "exchange": "SSE", "name": "sample",
        }], "revision")
        self.assertEqual([row["observed_at"] for row in rows],
                         ["1999-11-10", "2024-06-01"])
        self.assertIsNone(rows[0]["delisted_at"])
        self.assertEqual(rows[1]["delisted_at"], "2024-06-01")

    def test_refresh_imports_complete_archive_and_returns_asof_members(self):
        fixture = {
            "rows": [
                {"code": "600000.SH", "listed_from": "1999-11-10", "delisted_at": None,
                 "security_type": "ordinary_share", "exchange": "SSE", "name": "active"},
                {"code": "000001.SZ", "listed_from": "1991-04-03", "delisted_at": "2023-01-01",
                 "security_type": "ordinary_share", "exchange": "SZSE", "name": "delisted"},
            ],
            "coverage_start": "1991-04-03", "source_revision": "test-revision",
        }
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "adaptive.sqlite3")
            with patch.object(HUPS, "fetch_public_security_master", return_value=fixture):
                result = HUPS.refresh_archive(db_path, asof="2022-12-31")
            self.assertTrue(result["source"]["historical_membership_complete"])
            # Members are projected onto the bare 6-digit market code that the
            # kline cache, market snapshot and factor file all key on.
            self.assertEqual({row["code"] for row in result["members"]},
                             {"600000", "000001"})
            with patch.object(HUPS, "fetch_public_security_master",
                              side_effect=AssertionError("same-day archive should be reused")):
                reused = HUPS.refresh_archive(db_path, asof="2022-12-31")
            self.assertEqual(result["manifest"]["universe_archive_fingerprint"],
                             reused["manifest"]["universe_archive_fingerprint"])
            with patch.object(HUPS, "fetch_public_security_master", return_value=fixture):
                future = HUPS.refresh_archive(db_path, asof="2024-01-01")
            self.assertEqual({row["code"] for row in future["members"]}, {"600000"})

    def test_market_code_projection_rejects_inconsistent_identity(self):
        self.assertEqual(HUPS._market_code("000001.SZ", "SZSE"), "000001")
        self.assertEqual(HUPS._market_code("600000.SH", "SSE"), "600000")
        self.assertEqual(HUPS._market_code("920000.BJ", "BSE"), "920000")
        # The suffix must agree with the archived exchange: the old BSE code
        # ranges overlap SSE main-board codes, so a mismatch is not cosmetic.
        for code, exchange in (("000001.SZ", "SSE"), ("000001", "SZSE"),
                               ("00001.SZ", "SZSE"), ("", "BSE")):
            with self.assertRaises(HUPS.PublicUniverseSourceError):
                HUPS._market_code(code, exchange)


if __name__ == "__main__":
    unittest.main()
