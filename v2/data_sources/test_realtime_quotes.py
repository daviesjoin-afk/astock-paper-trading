#!/usr/bin/env python3
"""Offline tests for the realtime quote parser.

No network: `_parse_record` is exercised directly against captured payloads.
"""

from __future__ import annotations

import unittest

from realtime_quotes import _to_cents, _to_tencent, _records, _parse_record


def payload(**overrides: str) -> str:
    """Build a Tencent `~`-delimited record with sane defaults.

    Only the fields the parser reads are populated; the rest stay empty, which
    is what the real endpoint returns for unused positions.
    """
    fields = [""] * 45
    fields[1] = "浦发银行"
    fields[2] = "600000"
    fields[3] = "10.17"
    fields[4] = "10.20"
    fields[5] = "10.22"
    fields[6] = "485046"
    fields[30] = "20261111150619"
    fields[33] = "10.31"
    fields[34] = "10.15"
    fields[37] = "4963754"
    for key, value in overrides.items():
        fields[int(key.lstrip("f"))] = value
    return "~".join(fields)


class CentParsingTests(unittest.TestCase):
    def test_whole_number(self):
        self.assertEqual(_to_cents("10"), 1000)

    def test_two_decimals(self):
        self.assertEqual(_to_cents("10.17"), 1017)

    def test_one_decimal(self):
        self.assertEqual(_to_cents("42.5"), 4250)

    def test_no_float_drift(self):
        # 0.29 * 100 is 28.999999999999996 in binary floating point.
        self.assertEqual(_to_cents("0.29"), 29)
        self.assertEqual(_to_cents("10.23"), 1023)

    def test_large_price(self):
        self.assertEqual(_to_cents("1234.56"), 123456)

    def test_empty_rejected(self):
        with self.assertRaises(Exception):
            _to_cents("")

    def test_garbage_rejected(self):
        with self.assertRaises(Exception):
            _to_cents("abc")


class SymbolMappingTests(unittest.TestCase):
    def test_shanghai(self):
        self.assertEqual(_to_tencent("600000.SH"), "sh600000")

    def test_shenzhen(self):
        self.assertEqual(_to_tencent("000001.SZ"), "sz000001")

    def test_beijing(self):
        self.assertEqual(_to_tencent("430047.BJ"), "bj430047")


class RecordParsingTests(unittest.TestCase):
    def test_parses_a_normal_quote(self):
        quote = _parse_record("600000.SH", payload(), "2026-11-11T15:06:19")
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.symbol, "600000.SH")
        self.assertEqual(quote.name, "浦发银行")
        self.assertEqual(quote.price_cents, 1017)
        self.assertEqual(quote.prev_close_cents, 1020)
        self.assertEqual(quote.open_cents, 1022)
        self.assertEqual(quote.high_cents, 1031)
        self.assertEqual(quote.low_cents, 1015)
        # Volume arrives in hands (lots); store shares.
        self.assertEqual(quote.volume_shares, 48_504_600)
        # Amount arrives in 万元; store cents.
        self.assertEqual(quote.amount_cents, 4_963_754 * 10_000 * 100)

    def test_change_bps(self):
        quote = _parse_record("600000.SH", payload(), "t")
        assert quote is not None
        self.assertEqual(quote.change_bps, (1017 - 1020) * 10_000 // 1020)

    def test_zero_price_skipped(self):
        # A halted stock before the open reports 0; that is not a -100% move.
        quote = _parse_record("600000.SH", payload(f3="0.00"), "t")
        self.assertIsNone(quote)

    def test_empty_price_skipped(self):
        quote = _parse_record("600000.SH", payload(f3=""), "t")
        self.assertIsNone(quote)

    def test_short_payload_skipped(self):
        self.assertIsNone(_parse_record("600000.SH", "1~name~600000", "t"))

    def test_zero_prev_close_gives_zero_bps(self):
        quote = _parse_record("600000.SH", payload(f4="0.00"), "t")
        assert quote is not None
        self.assertEqual(quote.change_bps, 0)


class RecordSplitTests(unittest.TestCase):
    def test_splits_multiple_records(self):
        text = (
            'v_sh600000="a~b";v_sz000001="c~d";\n'
            'v_sh600036="e~f";'
        )
        self.assertEqual(list(_records(text)), ["a~b", "c~d", "e~f"])

    def test_ignores_lines_without_quotes(self):
        self.assertEqual(list(_records("v_foo=;bar")), [])

    def test_empty_input(self):
        self.assertEqual(list(_records("")), [])


if __name__ == "__main__":
    unittest.main()
