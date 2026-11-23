#!/usr/bin/env python3
"""Offline tests for the three-tier resident memory.

The rules under test: a thin sample never becomes a confident sentence, a write
against a stale snapshot is refused rather than applied, capacity is enforced
instead of silently overrunning the prompt, and a behaviour change is adopted
only when its interval excludes zero and no case regressed.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_resident as mem  # noqa: E402


def card(**regimes) -> dict:
    return {"by_regime": regimes}


class FactTests(unittest.TestCase):
    def test_a_thin_sample_produces_no_fact(self):
        thin = card(risk_off={"blocked_symbols": {"cases": 3, "excess_bps": 400}})
        self.assertEqual(mem.facts_from_scorecard(thin), [])

    def test_a_block_that_beat_the_market_is_called_a_mistake(self):
        good = card(mixed={"blocked_symbols": {"cases": 214, "excess_bps": 145,
                                              "beat_market": 154, "lagged_market": 43}})
        facts = mem.facts_from_scorecard(good)
        self.assertEqual(len(facts), 1)
        self.assertIn("拦错了", facts[0].text)
        self.assertIn("硬证据", facts[0].text)
        self.assertEqual(facts[0].cases, 214)

    def test_a_block_that_lagged_the_market_is_called_correct(self):
        # Raw return can be positive on a strong day while still losing ground;
        # the verdict must follow the excess, not the raw move.
        good = card(risk_on={"blocked_symbols": {"cases": 777, "excess_bps": -381,
                                                "mean_bps": 1, "beat_market": 200,
                                                "lagged_market": 577}})
        facts = mem.facts_from_scorecard(good)
        self.assertIn("拦对了", facts[0].text)
        self.assertNotIn("拦错了", facts[0].text)

    def test_a_costly_named_buy_tells_the_model_to_stop_naming(self):
        good = card(mixed={"named_buys": {"cases": 129, "excess_bps": -122}})
        facts = mem.facts_from_scorecard(good)
        self.assertIn("不要点名", facts[0].text)


class ResidentBudgetTests(unittest.TestCase):
    def test_capacity_is_enforced_rather_than_overrun(self):
        entries = [mem._entry("fact", "x" * 400 + str(i), "test") for i in range(20)]
        kept = mem._bounded(entries)
        total = sum(len(e.text) + 1 for e in kept)
        self.assertLessEqual(total, mem.MAX_RESIDENT_CHARS)
        self.assertLess(len(kept), len(entries))

    def test_facts_survive_the_cut_before_rules(self):
        facts = [mem._entry("fact", "f" * 300 + str(i), "t") for i in range(4)]
        rules = [mem._entry("rule", "r" * 300 + str(i), "t") for i in range(10)]
        kept = mem._bounded(rules + facts)
        self.assertTrue(any(e.kind == "fact" for e in kept))
        self.assertGreaterEqual(sum(1 for e in kept if e.kind == "fact"), 4)

    def test_round_trip_is_stable_and_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [mem._entry("fact", "risk_off 状态平均 +26 bps", "t", cases=432)]
            mem.write_resident(root, entries)
            again = mem.read_resident(root)
            self.assertEqual([e.text for e in again], [e.text for e in entries])
            block = mem.render_resident(again)
            self.assertIn("【你自己的记忆】", block)
            self.assertIn("实测", block)


class WriteProtocolTests(unittest.TestCase):
    def setUp(self):
        self.base = [mem._entry("rule", "弱市不要追涨停", "observer")]
        self.rev = mem.revision_of(self.base)

    def test_a_stale_snapshot_is_refused_not_applied(self):
        out, rejected = mem.apply_mutations(
            self.base, [mem.Mutation("add", text="新规则", expected_revision="deadbeef")],
            revision=self.rev)
        self.assertEqual(len(out), 1)
        self.assertTrue(any(r.startswith("stale_revision") for r in rejected))

    def test_a_duplicate_is_refused(self):
        out, rejected = mem.apply_mutations(
            self.base, [mem.Mutation("add", text="弱市不要追涨停")], revision=self.rev)
        self.assertIn("duplicate", rejected)
        self.assertEqual(len(out), 1)

    def test_replace_needs_exactly_one_match(self):
        base = self.base + [mem._entry("rule", "弱市不要追涨停 也不要追高", "observer")]
        rev = mem.revision_of(base)
        out, rejected = mem.apply_mutations(
            base, [mem.Mutation("replace", text="改成别的", old_text="弱市")], revision=rev)
        self.assertTrue(any(r.startswith("replace_needs_one_match") for r in rejected))
        self.assertEqual(out, mem._bounded(base))

    def test_remove_only_on_a_unique_match(self):
        out, rejected = mem.apply_mutations(
            self.base, [mem.Mutation("remove", old_text="追涨停")], revision=self.rev)
        self.assertEqual(out, [])
        self.assertEqual(rejected, [])


class TrialTests(unittest.TestCase):
    def test_a_short_sample_stays_collecting(self):
        self.assertEqual(mem.paired_summary([5.0] * 10)["status"], "collecting")

    def test_an_interval_that_includes_zero_is_not_adopted(self):
        noisy = [50, -50] * 15
        summary = mem.paired_summary(noisy)
        self.assertEqual(summary["status"], "not_adopted")
        ok, reasons = mem.adopt(mem.Trial("c", {}, "2026-11-20"), forwarded=noisy)
        self.assertFalse(ok)
        self.assertIn("no_positive_95_percent_lower_bound", reasons)

    def test_a_single_regressed_case_blocks_adoption(self):
        cases = [40.0] * 30
        cases[7] = -1.0
        ok, reasons = mem.adopt(mem.Trial("c", {}, "2026-11-20"), forwarded=cases)
        self.assertFalse(ok)
        self.assertIn("a_forward_case_regressed", reasons)

    def test_a_clean_positive_sample_requires_review(self):
        cases = [40.0 + (i % 3) for i in range(30)]
        ok, reasons = mem.adopt(mem.Trial("c", {}, "2026-11-20"), forwarded=cases)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])


class RefreshTests(unittest.TestCase):
    def test_refresh_replaces_facts_but_keeps_learned_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mem.write_resident(root, [mem._entry("rule", "这条规则要保留", "observer")])
            first = card(risk_off={"blocked_symbols": {"cases": 100, "excess_bps": 26}})
            mem.refresh(root, first)
            second = card(risk_off={"blocked_symbols": {"cases": 120, "excess_bps": 40}})
            entries = mem.refresh(root, second)
            texts = [e.text for e in entries]
            self.assertTrue(any("这条规则要保留" in t for t in texts))
            self.assertFalse(any("+26 bps" in t for t in texts), "old fact replaced")
            self.assertTrue(any("+40 bps" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
