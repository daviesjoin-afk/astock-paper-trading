"""The observer builds a prompt, reads an answer, and submits it. That is all.

These tests deliberately do **not** check any bound, floor or whitelist. Those
live in the Rust `domain::ai_policy` module and are tested there. Re-asserting
them here would recreate the duplicate-rules problem this module was rewritten
to remove: if Python keeps its own copy of a limit, someone will eventually
"fix" only one of them.

So what is tested here is the shape work: does the prompt carry the numbers the
model is asked about, does a fenced or malformed answer get read or refused
correctly, and does a missing configuration stay silent instead of raising.
"""
import json
import unittest

import market_observer as observer
from market_observer import MarketObservation, build_user_prompt, parse_fields


def answer(**overrides) -> str:
    payload = {
        "bias": "bearish",
        "entry_delta": 10,
        "weight_delta_bps": 0,
        "position_delta": 0,
        "blocked_symbols": [],
        "confidence": 80,
        "reason": "regime declining, candidates show long lower wicks",
    }
    payload.update(overrides)
    return json.dumps(payload)


def observation() -> MarketObservation:
    return MarketObservation(
        day="2026-11-16",
        regime="declining",
        regime_separation_bps=-230,
        regime_breadth_bps=3100,
        slot="aggressive",
        slot_return_bps=-512,
        slot_drawdown_bps=1100,
        slot_positions=2,
        candidates=[
            {"symbol": "600000.SH", "score": 62, "reason": "fresh_breakout",
             "klow_bps": 40, "klen_bps": 290, "kmid_bps": 120},
            {"symbol": "000001.SZ", "score": 55, "reason": "fresh_breakout",
             "klow_bps": 210, "klen_bps": 410, "kmid_bps": -30},
        ],
    )


class ParseTests(unittest.TestCase):
    def test_fields_are_read_without_judgement(self):
        # The values are absurd on purpose. Parsing must not fix them: the
        # kernel is the authority on what is acceptable, and silently clamping
        # here would hide from the kernel that the model asked for 999.
        fields = parse_fields(answer(entry_delta=999, position_delta=9,
                                     blocked_symbols=["ANYTHING.SH"]))
        self.assertEqual(fields["entry_delta"], 999)
        self.assertEqual(fields["position_delta"], 9)
        self.assertEqual(fields["blocked_symbols"], ["ANYTHING.SH"])

    def test_a_fenced_answer_is_read(self):
        fields = parse_fields("```json\n" + answer() + "\n```")
        self.assertIsNotNone(fields)
        self.assertEqual(fields["entry_delta"], 10)

    def test_non_json_is_refused(self):
        for bad in ("not json", "", "[1,2,3]", "{}extra"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_fields(bad))

    def test_a_missing_number_is_absent_not_zero(self):
        # "the model did not say" and "the model said no change" are different
        # claims; collapsing them to 0 loses the distinction the kernel records.
        fields = parse_fields('{"bias": "neutral", "confidence": 70, "reason": "x"}')
        self.assertIsNone(fields["entry_delta"])
        self.assertIsNone(fields["position_delta"])

    def test_a_boolean_is_not_read_as_a_number(self):
        fields = parse_fields(answer(position_delta=True))
        self.assertIsNone(fields["position_delta"])

    def test_non_numeric_text_in_a_number_field_is_absent(self):
        fields = parse_fields(answer(entry_delta="lots"))
        self.assertIsNone(fields["entry_delta"])

    def test_blocked_symbols_that_is_not_a_list_becomes_empty(self):
        fields = parse_fields(answer(blocked_symbols="600000.SH"))
        self.assertEqual(fields["blocked_symbols"], [])


class PromptTests(unittest.TestCase):
    def test_the_prompt_carries_the_numbers_the_model_is_asked_about(self):
        text = build_user_prompt(observation())
        self.assertIn("declining", text)
        self.assertIn("-230", text)
        # The code must keep its case: the model echoes it back, and a
        # lowercased symbol would not match a candidate in the kernel.
        self.assertIn("600000.SH", text)
        self.assertNotIn("600000.sh", text)
        self.assertIn("klow", text)

    def test_an_empty_news_list_says_so_instead_of_omitting_the_section(self):
        # Otherwise the model cannot tell "no news" from "news not shown".
        self.assertIn("没有取到", build_user_prompt(observation()))

    def test_news_and_trades_appear_when_present(self):
        obs = observation()
        obs.news = [{"symbol": "600000.SH", "title": "中标公告",
                     "published_at": "2026-11-16T08:00:00+08:00"}]
        obs.recent_trades = [{"trade_day": "2026-11-13", "side": "buy",
                              "symbol": "600000.SH", "quantity": 100,
                              "price_cents": 1200, "reason": "fresh_breakout"}]
        text = build_user_prompt(obs)
        self.assertIn("中标公告", text)
        self.assertIn("fresh_breakout", text)


class SilenceTests(unittest.TestCase):
    def test_no_configuration_produces_no_advice_rather_than_an_exception(self):
        self.assertIsNone(observer.ask_model(observation(), base_url="",
                                             api_key="", model="m"))

    def test_an_unreachable_model_produces_no_advice(self):
        self.assertIsNone(observer.ask_model(observation(),
                                             base_url="http://127.0.0.1:9",
                                             api_key="k", model="m", timeout=1.0))

    def test_submitting_to_an_unreachable_kernel_reports_false(self):
        self.assertFalse(observer.submit({"bias": "neutral"},
                                         api="http://127.0.0.1:9", timeout=1.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
