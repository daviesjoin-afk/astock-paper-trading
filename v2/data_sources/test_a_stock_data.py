"""Offline tests for the a-stock-data cross-check adapter."""

from __future__ import annotations

import csv
import io
import json
import os
import struct
import subprocess
import tempfile
import shutil
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

import a_stock_data as adapter


def tencent_payload(
    symbol: str = "sh600000",
    rows: list[list[str]] | None = None,
    *,
    key: str = "day",
) -> dict:
    if rows is None:
        rows = [
            ["2026-11-04", "10.05", "10.09", "10.23", "10.05", "824086.000"],
            ["2026-11-05", "10.09", "9.87", "10.10", "9.82", "629078.000"],
        ]
    return {"code": 0, "msg": "", "data": {symbol: {key: rows}}}


def tdx_record(
    code: str,
    sequence: int,
    *,
    name: str = "浦发银行",
    close: float = 9.87,
    volume: int = 62_907_781,
) -> bytes:
    record = bytearray(150)
    record[0:6] = code.encode("ascii")
    record[32:34] = struct.pack("<H", sequence)
    encoded = name.encode("gbk")
    record[40 : 40 + len(encoded)] = encoded
    return bytes(record)


def tdx_block(
    *,
    prev_close: float = 10.09,
    open_: float = 10.09,
    high: float = 10.10,
    low: float = 9.82,
    close: float = 9.87,
    volume: int = 62_907_781,
    amount: float = 623_425_673.0,
) -> bytes:
    block = bytearray(512)
    block[4:12] = struct.pack("<d", prev_close)
    block[12:20] = struct.pack("<d", open_)
    block[20:28] = struct.pack("<d", high)
    block[28:36] = struct.pack("<d", low)
    block[36:44] = struct.pack("<d", close)
    block[56:64] = struct.pack("<Q", volume)
    block[72:80] = struct.pack("<d", amount)
    return bytes(block)


def tdx_zip(
    day: str = "2026-11-05",
    *,
    sh_records: list[tuple[bytes, bytes]] | None = None,
    sz_records: list[tuple[bytes, bytes]] | None = None,
) -> bytes:
    if sh_records is None:
        sh_records = [(tdx_record("600000", 0), tdx_block())]
    if sz_records is None:
        sz_records = [(tdx_record("000001", 0, name="平安银行", close=12.34, volume=1_000_000), tdx_block(close=12.34, volume=1_000_000))]
    buffer = io.BytesIO()
    suffix = day.replace("-", "")[2:]
    with zipfile.ZipFile(buffer, "w") as archive:
        for market, records in (("sh", sh_records), ("sz", sz_records)):
            cod = b"".join(record for record, _ in records)
            md1 = b"".join(block for _, block in records)
            archive.writestr(f"{market}{suffix}.cod", cod)
            archive.writestr(f"{market}{suffix}.md1", md1)
    return buffer.getvalue()


class SymbolTest(unittest.TestCase):
    def test_requires_exchange_suffix(self) -> None:
        with self.assertRaises(adapter.DataSourceError) as caught:
            adapter.normalize_symbol("600000")
        self.assertEqual(caught.exception.code, "symbol_requires_exchange_suffix")

    def test_refuses_cross_market_suffix(self) -> None:
        with self.assertRaises(adapter.DataSourceError) as caught:
            adapter.normalize_symbol("600000.SZ")
        self.assertEqual(caught.exception.code, "unsupported_symbol")

    def test_accepts_supported_main_board_codes(self) -> None:
        self.assertEqual(adapter.normalize_symbol("600000.SH"), ("600000.SH", "sh", "600000"))
        self.assertEqual(adapter.normalize_symbol("000001.SZ"), ("000001.SZ", "sz", "000001"))


class CalendarTest(unittest.TestCase):
    def test_duplicate_or_unsorted_days_are_refused(self) -> None:
        for days, code in (
            (["2026-11-05", "2026-11-05"], "duplicate_day"),
            (["2026-11-05", "2026-11-04"], "days_not_sorted"),
            ([], "empty_days"),
        ):
            with self.assertRaises(adapter.DataSourceError) as caught:
                adapter.validate_days(days)
            self.assertEqual(caught.exception.code, code)


class TencentParserTest(unittest.TestCase):
    def test_parses_unadjusted_series_and_converts_lots(self) -> None:
        batch = adapter.parse_tencent_payload(
            tencent_payload(),
            "600000.SH",
            "2026-11-04",
            "2026-11-05",
            "https://example.test/tencent",
            "a" * 64,
        )
        self.assertEqual(list(batch.bars), ["2026-11-04", "2026-11-05"])
        self.assertEqual(adapter.price_to_cents(batch.bars["2026-11-05"].close), 987)
        self.assertEqual(batch.bars["2026-11-05"].volume_shares, 62_907_800)

    def test_star_market_volume_is_already_shares(self) -> None:
        payload = tencent_payload(
            "sh688001",
            [["2026-11-05", "30.00", "30.20", "30.50", "29.90", "12345"]],
        )
        batch = adapter.parse_tencent_payload(
            payload,
            "688001.SH",
            "2026-11-05",
            "2026-11-05",
            "https://example.test/tencent",
            "b" * 64,
        )
        self.assertEqual(batch.bars["2026-11-05"].volume_shares, 12_345)

    def test_adjusted_only_series_is_rejected(self) -> None:
        with self.assertRaises(adapter.DataSourceError) as caught:
            adapter.parse_tencent_payload(
                tencent_payload(key="qfqday"),
                "600000.SH",
                "2026-11-04",
                "2026-11-05",
                "https://example.test/tencent",
                "c" * 64,
            )
        self.assertEqual(caught.exception.code, "unexpected_adjusted_series")

    def test_day_outside_requested_range_is_rejected(self) -> None:
        with self.assertRaises(adapter.DataSourceError) as caught:
            adapter.parse_tencent_payload(
                tencent_payload(
                    rows=[["2026-11-06", "10.00", "10.00", "10.00", "10.00", "100"]]
                ),
                "600000.SH",
                "2026-11-05",
                "2026-11-05",
                "https://example.test/tencent",
                "d" * 64,
            )
        self.assertEqual(caught.exception.code, "tencent_day_outside_range")


class TdxParserTest(unittest.TestCase):
    def parse(self, content: bytes, day: str = "2026-11-05"):
        return adapter.parse_tdx_package(
            content,
            day,
            source_url="https://example.test/tdx.zip",
            min_priced={"sh": 1, "sz": 1},
        )

    def test_parses_close_volume_and_amount(self) -> None:
        batch = self.parse(tdx_zip())
        bar = batch.bar("sh", "600000")
        self.assertIsNotNone(bar)
        self.assertEqual(adapter.price_to_cents(bar.close), 987)
        self.assertEqual(bar.volume, 62_907_781)
        self.assertEqual(bar.name, "浦发银行")
        self.assertEqual(batch.counts, {"sh": 1, "sz": 1})

    def test_missing_market_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("sh261105.cod", tdx_record("600000", 0))
            archive.writestr("sh261105.md1", tdx_block())
        with self.assertRaises(adapter.DataSourceError) as caught:
            self.parse(buffer.getvalue())
        self.assertEqual(caught.exception.code, "tdx_package_missing_market")

    def test_truncated_package_is_rejected(self) -> None:
        with self.assertRaises(adapter.DataSourceError) as caught:
            adapter.parse_tdx_package(
                b"not a zip",
                "2026-11-05",
                source_url="https://example.test/tdx.zip",
            )
        self.assertEqual(caught.exception.code, "tdx_package_not_zip")

    def test_non_finite_price_is_rejected(self) -> None:
        block = bytearray(tdx_block())
        block[36:44] = struct.pack("<d", float("nan"))
        content = tdx_zip(sh_records=[(tdx_record("600000", 0), bytes(block))])
        with self.assertRaises(adapter.DataSourceError) as caught:
            self.parse(content)
        self.assertEqual(caught.exception.code, "tdx_non_finite_value")

    def test_market_below_observed_floor_is_rejected(self) -> None:
        with self.assertRaises(adapter.DataSourceError) as caught:
            adapter.parse_tdx_package(
                tdx_zip(),
                "2026-11-05",
                source_url="https://example.test/tdx.zip",
                min_priced={"sh": 10_000, "sz": 1},
            )
        self.assertEqual(caught.exception.code, "tdx_market_too_small")

    def test_index_rows_are_not_parsed_as_stock_prices(self) -> None:
        records = [
            (tdx_record("999999", 0, name="上证指数", close=3599.0166), tdx_block(close=3599.0166)),
            (tdx_record("600000", 1), tdx_block()),
        ]
        batch = self.parse(tdx_zip(sh_records=records))
        self.assertIsNone(batch.bar("sh", "999999"))
        self.assertIsNotNone(batch.bar("sh", "600000"))


class SinaAdjustTest(unittest.TestCase):
    def test_parses_factor_series_with_trailing_comment(self) -> None:
        text = (
            'var sh600000qfq={"data":[{"d":"2026-11-05","f":"1.5"},'
            '{"d":"2026-11-04","f":"2.0"}]};/* trailing */'
        )
        series = adapter.parse_sina_adjust_payload(
            text,
            "600000.SH",
            "qfq",
            source_url="https://example.test/sina",
            raw_sha256="f" * 64,
        )
        self.assertEqual([item.day for item in series.factors], ["2026-11-04", "2026-11-05"])
        self.assertEqual(series.factor_on("2026-11-03"), None)
        self.assertEqual(series.factor_on("2026-11-04"), 2)
        self.assertEqual(series.factor_on("2026-11-05"), Decimal("1.5"))

    def test_fund_factor_shape_is_refused(self) -> None:
        text = 'var sh600000qfq={"data":[{"d":"2026-11-05","f":"1","s":"1.2","u":"0.1"}]}'
        with self.assertRaises(adapter.DataSourceError) as caught:
            adapter.parse_sina_adjust_payload(
                text,
                "600000.SH",
                "qfq",
                source_url="https://example.test/sina",
                raw_sha256="f" * 64,
            )
        self.assertEqual(caught.exception.code, "sina_adjust_fund_not_supported")

    def test_empty_factor_series_is_not_treated_as_no_corporate_action(self) -> None:
        with self.assertRaises(adapter.DataSourceError) as caught:
            adapter.parse_sina_adjust_payload(
                'var sh600000qfq={"data":[]}',
                "600000.SH",
                "qfq",
                source_url="https://example.test/sina",
                raw_sha256="f" * 64,
            )
        self.assertEqual(caught.exception.code, "sina_adjust_empty")


class CrossCheckTest(unittest.TestCase):
    def tencent(self, symbol: str, start: str, end: str) -> adapter.TencentBatch:
        return adapter.parse_tencent_payload(
            tencent_payload(),
            symbol,
            start,
            end,
            "https://example.test/tencent",
            "e" * 64,
        )

    def tdx(self, day: str) -> adapter.TdxBatch:
        if day == "2026-11-04":
            record = tdx_record("600000", 0)
            block = tdx_block(
                prev_close=10.05,
                open_=10.05,
                high=10.23,
                low=10.05,
                close=10.09,
                volume=82_408_581,
                amount=830_000_000.0,
            )
        else:
            record = tdx_record("600000", 0)
            block = tdx_block()
        return adapter.parse_tdx_package(
            tdx_zip(day, sh_records=[(record, block)]),
            day,
            source_url="https://example.test/tdx.zip",
            min_priced={"sh": 1, "sz": 1},
        )

    def adjust(self, symbol: str, kind: str = "qfq") -> adapter.AdjustSeries:
        return adapter.AdjustSeries(
            symbol=symbol,
            kind=kind,
            factors=(adapter.AdjustFactor("2026-10-01", Decimal("1")),),
            raw_sha256="1" * 64,
            source_url="https://example.test/sina",
        )

    def test_matching_sources_produce_canonical_rows(self) -> None:
        rows, report, errors = adapter.cross_check(
            ["600000.SH"],
            ["2026-11-04", "2026-11-05"],
            self.tencent,
            self.tdx,
            self.adjust,
        )
        self.assertEqual(errors, [])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["review_findings"], [])
        self.assertTrue(report["review_complete"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["price"], "9.87")
        self.assertEqual(rows[-1]["source"], adapter.SOURCE_LABEL)
        self.assertEqual(report["checks"][-1]["volume_difference_shares"], 19)
        parsed = list(csv.DictReader(io.StringIO(adapter.build_csv(rows))))
        self.assertEqual(tuple(parsed[0]), ("symbol", "day", "price", "source", "observed_at"))

    def test_close_mismatch_fails_the_batch(self) -> None:
        def tdx_wrong_close(day: str) -> adapter.TdxBatch:
            close = 10.10 if day == "2026-11-04" else 9.88
            content = tdx_zip(day, sh_records=[(tdx_record("600000", 0), tdx_block(close=close))])
            return adapter.parse_tdx_package(
                content,
                day,
                source_url="https://example.test/tdx.zip",
                min_priced={"sh": 1, "sz": 1},
            )

        rows, report, errors = adapter.cross_check(
            ["600000.SH"],
            ["2026-11-04", "2026-11-05"],
            self.tencent,
            tdx_wrong_close,
            self.adjust,
        )
        self.assertEqual(rows, [])
        self.assertEqual(report["status"], "failed")
        self.assertEqual({error["code"] for error in errors}, {"close_mismatch"})
        self.assertIsNone(report["csv_sha256"])
        self.assertEqual(report["expected_rows"], 2)
        self.assertEqual(report["partial_rows"], 0)

    def test_partial_success_reports_counts_but_never_a_csv_digest(self) -> None:
        def tdx_one_mismatch(day: str) -> adapter.TdxBatch:
            close = 10.10 if day == "2026-11-04" else 9.87
            content = tdx_zip(
                day,
                sh_records=[(tdx_record("600000", 0), tdx_block(close=close))],
            )
            return adapter.parse_tdx_package(
                content,
                day,
                source_url="https://example.test/tdx.zip",
                min_priced={"sh": 1, "sz": 1},
            )

        rows, report, errors = adapter.cross_check(
            ["600000.SH"],
            ["2026-11-04", "2026-11-05"],
            self.tencent,
            tdx_one_mismatch,
            self.adjust,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["day"], "2026-11-05")
        self.assertEqual([error["code"] for error in errors], ["close_mismatch"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["expected_rows"], 2)
        self.assertEqual(report["rows"], 1)
        self.assertEqual(report["partial_rows"], 1)
        self.assertIsNone(report["csv_sha256"])

    def test_source_label_must_be_a_v2_key(self) -> None:
        with self.assertRaises(adapter.DataSourceError) as caught:
            adapter.cross_check(
                ["600000.SH"],
                ["2026-11-04", "2026-11-05"],
                self.tencent,
                self.tdx,
                self.adjust,
                source_label="tencent+tdx",
            )
        self.assertEqual(caught.exception.code, "invalid_source_label")

    def test_zero_volume_is_refused_even_when_close_matches(self) -> None:
        def tdx_zero_volume(day: str) -> adapter.TdxBatch:
            close = 10.09 if day == "2026-11-04" else 9.87
            content = tdx_zip(
                day,
                sh_records=[(tdx_record("600000", 0), tdx_block(close=close, volume=0))],
            )
            return adapter.parse_tdx_package(
                content,
                day,
                source_url="https://example.test/tdx.zip",
                min_priced={"sh": 1, "sz": 1},
            )

        rows, _, errors = adapter.cross_check(
            ["600000.SH"],
            ["2026-11-04", "2026-11-05"],
            self.tencent,
            tdx_zero_volume,
            self.adjust,
        )
        self.assertEqual(rows, [])
        self.assertEqual({error["code"] for error in errors}, {"zero_volume"})

    def test_volume_disagreement_beyond_one_lot_is_refused(self) -> None:
        def tdx_volume(day: str) -> adapter.TdxBatch:
            close = 10.09 if day == "2026-11-04" else 9.87
            content = tdx_zip(
                day,
                sh_records=[
                    (
                        tdx_record("600000", 0),
                        tdx_block(close=close, volume=62_000_000),
                    )
                ],
            )
            return adapter.parse_tdx_package(
                content,
                day,
                source_url="https://example.test/tdx.zip",
                min_priced={"sh": 1, "sz": 1},
            )

        rows, _, errors = adapter.cross_check(
            ["600000.SH"],
            ["2026-11-04", "2026-11-05"],
            self.tencent,
            tdx_volume,
            self.adjust,
        )
        self.assertEqual(rows, [])
        self.assertEqual({error["code"] for error in errors}, {"volume_mismatch"})

    def test_source_failure_is_reported_without_partial_rows(self) -> None:
        def tdx_missing(day: str) -> adapter.TdxBatch:
            raise adapter.DataSourceError("tdx_package_unavailable", day)

        rows, report, errors = adapter.cross_check(
            ["600000.SH"],
            ["2026-11-04", "2026-11-05"],
            self.tencent,
            tdx_missing,
            self.adjust,
        )
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(error["code"] == "tdx_package_unavailable" for error in errors))
        self.assertEqual(report["status"], "failed")

    def test_corporate_action_in_window_needs_human_review(self) -> None:
        def adjust_changed(symbol: str, kind: str = "qfq") -> adapter.AdjustSeries:
            return adapter.AdjustSeries(
                symbol=symbol,
                kind=kind,
                factors=(
                    adapter.AdjustFactor("2026-10-01", Decimal("1")),
                    adapter.AdjustFactor("2026-11-05", Decimal("2")),
                ),
                raw_sha256="2" * 64,
                source_url="https://example.test/sina",
            )

        _, report, errors = adapter.cross_check(
            ["600000.SH"],
            ["2026-11-04", "2026-11-05"],
            self.tencent,
            self.tdx,
            adjust_changed,
        )
        self.assertEqual(errors, [])
        self.assertEqual(report["status"], "needs_review")
        self.assertFalse(report["review_complete"])
        self.assertIn("corporate_action_in_window", {item["code"] for item in report["review_findings"]})

    def test_corporate_action_on_first_day_needs_human_review(self) -> None:
        def adjust_changed_before(symbol: str, kind: str = "qfq") -> adapter.AdjustSeries:
            return adapter.AdjustSeries(
                symbol=symbol,
                kind=kind,
                factors=(
                    adapter.AdjustFactor("2026-10-01", Decimal("1")),
                    adapter.AdjustFactor("2026-11-04", Decimal("2")),
                ),
                raw_sha256="3" * 64,
                source_url="https://example.test/sina",
            )

        _, report, errors = adapter.cross_check(
            ["600000.SH"],
            ["2026-11-04", "2026-11-05"],
            self.tencent,
            self.tdx,
            adjust_changed_before,
        )
        self.assertEqual(errors, [])
        self.assertEqual(report["status"], "needs_review")
        self.assertIn(
            "corporate_action_in_window",
            {item["code"] for item in report["review_findings"]},
        )

    def test_adjust_factor_failure_blocks_review_eligibility(self) -> None:
        def adjust_missing(symbol: str, kind: str = "qfq") -> adapter.AdjustSeries:
            raise adapter.DataSourceError("sina_adjust_empty", symbol)

        _, report, errors = adapter.cross_check(
            ["600000.SH"],
            ["2026-11-04", "2026-11-05"],
            self.tencent,
            self.tdx,
            adjust_missing,
        )
        self.assertEqual([error["code"] for error in errors], ["sina_adjust_empty"])
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["review_complete"])
        self.assertEqual(
            [item["code"] for item in report["review_findings"]],
            ["adjust_factor_unavailable"],
        )


class CliTest(unittest.TestCase):
    def test_failed_crosscheck_writes_report_but_not_csv(self) -> None:
        result = adapter.main(
            [
                "fetch",
                "--symbols",
                "600000",
                "--days",
                "2026-11-05",
                "--out",
                "unused.csv",
            ]
        )
        self.assertEqual(result, 2)
        self.assertFalse(Path("unused.csv").exists())

    def test_report_is_deterministic_json(self) -> None:
        report = {"status": "failed", "errors": [{"code": "x"}]}
        with self.subTest("write helper exists"):
            self.assertTrue(callable(adapter.write_report))
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            adapter.write_report(target, report)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), report)


class PublishWrapperTest(unittest.TestCase):
    @staticmethod
    def bash() -> str:
        configured = os.environ.get("ASTOCK_TEST_BASH")
        if configured:
            return configured
        if os.name == "nt":
            for parent in (
                Path(os.environ.get("ProgramFiles", "")),
                Path(os.environ.get("ProgramFiles(x86)", "")),
            ):
                candidate = parent / "Git" / "bin" / "bash.exe"
                if candidate.is_file():
                    return str(candidate)
            raise unittest.SkipTest("Git Bash is required to execute the publish wrapper")
        return "bash"

    def setUp(self) -> None:
        self.bash_bin = self.bash()
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.output = self.root / "price-sets"
        v2 = Path(__file__).resolve().parents[1]
        script = (v2 / "scripts" / "fetch_and_publish_reviewed.sh").read_text(
            encoding="utf-8"
        )
        script = script.replace(
            '"$PYTHON" data_sources/a_stock_data.py fetch \\',
            '"$FAKE_FETCH" \\',
        ).replace(
            "bash scripts/publish_price_set.sh \\",
            '"$FAKE_PUBLISH" \\',
        )
        self.script = self.root / "fetch_and_publish_reviewed.sh"
        self.script.write_text(script, encoding="utf-8")
        (self.root / "data_sources").mkdir()
        (self.root / "data_sources" / "a_stock_data.py").write_text("", encoding="utf-8")
        self.fetch = self.root / "fake-fetch.sh"
        self.fetch.write_text(
            "#!/usr/bin/env bash\n"
            'out=""; report=""; previous=""\n'
            'for arg in "$@"; do\n'
            '  [ "$previous" = "--out" ] && out="$arg"\n'
            '  [ "$previous" = "--report" ] && report="$arg"\n'
            '  previous="$arg"\n'
            "done\n"
            "printf 'symbol,day,price,source,observed_at\\n"
            "600000.SH,2026-11-04,10.09,tencent-tdx-crosscheck,"
            "2026-11-04T15:00:00+08:00\\n"
            "600000.SH,2026-11-05,9.87,tencent-tdx-crosscheck,"
            "2026-11-05T15:00:00+08:00\\n' > \"$out\"\n"
            'printf \'{"status":"ok","review_complete":true,'
            '"review_findings":[]}\\n\' > "$report"\n',
            encoding="utf-8",
        )
        self.fetch.chmod(0o755)
        self.publish = self.root / "fake-publish.sh"
        self.publish.write_text(
            "#!/usr/bin/env bash\n"
            'out=""; previous=""\n'
            'for arg in "$@"; do [ "$previous" = "--out" ] && out="$arg"; previous="$arg"; done\n'
            'if [ -e "$out/recorded-set.json" ]; then\n'
            '  echo "refusing to overwrite existing artifact" >&2\n'
            "  exit 3\n"
            "fi\n"
            'mkdir -p "$out"\n'
            'printf "{}\\n" > "$out/recorded-set.json"\n',
            encoding="utf-8",
        )
        self.publish.chmod(0o755)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_wrapper(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                self.bash_bin,
                str(self.script),
                "--symbols",
                "600000.SH",
                "--days",
                "2026-11-04,2026-11-05",
                "--set-id",
                "recorded-set",
                "--pack",
                "cn-offline-fixture-pack-1",
                "--out",
                str(self.output),
                "--review",
            ],
            env={
                **os.environ,
                "FAKE_FETCH": str(self.fetch),
                "FAKE_PUBLISH": str(self.publish),
            },
            cwd=self.root,
            text=True,
            capture_output=True,
        )

    def test_success_keeps_csv_and_report_under_audit(self) -> None:
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.output / "audit" / "recorded-set.csv").is_file())
        self.assertTrue((self.output / "audit" / "recorded-set.report.json").is_file())

    def test_failure_does_not_install_price_set(self) -> None:
        self.fetch.write_text(
            "#!/usr/bin/env bash\nexit 1\n",
            encoding="utf-8",
        )
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.output / "recorded-set.json").exists())
        self.assertFalse((self.output / "audit").exists())

    def test_publish_refusal_keeps_existing_audit_unmodified(self) -> None:
        first = self.run_wrapper()
        self.assertEqual(first.returncode, 0, first.stderr)
        csv = self.output / "audit" / "recorded-set.csv"
        report = self.output / "audit" / "recorded-set.report.json"
        before = (csv.read_bytes(), report.read_bytes())

        second = self.run_wrapper()
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("publication failed", second.stderr)
        self.assertEqual((csv.read_bytes(), report.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
