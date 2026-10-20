"""Shared candidate work must retain cutoff, missingness and account isolation."""
import copy
import datetime as dt
import os
import sys
import unittest
from contextlib import ExitStack, contextmanager
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factors as F  # noqa: E402
import paper_candidate_inputs as PCI  # noqa: E402
import paper_trading as PT  # noqa: E402


class SharedFundamentalTests(unittest.TestCase):
    def setUp(self):
        self.now = "2026-10-16T13:40:00+08:00"
        self.day = dt.date(2026, 10, 15)
        self.rows = [{"code": "600001", "name": "样本", "industry": "银行",
                      "price": 12., "pe": 8., "quote_at": self.now}]
        self.finance = {"data": {"600001": {"net_profit": 3., "profit_yoy": 20.,
                        "report_date": "2026-06-30", "report_published_at": "2026-10-16"}}}

    def test_exact_facts_attrs_and_distinct_financial_cutoffs(self):
        shared = PCI.CandidateScanInputs(self.now)
        expected = {cutoff: F.compute_fundamental_factors(
            self.rows, self.finance, asof=cutoff, snapshot_asof=self.now)
            for cutoff in (self.day, None)}
        with mock.patch.object(F, "compute_fundamental_factors",
                               wraps=F.compute_fundamental_factors) as compute:
            for cutoff in (self.day, None):
                actual = shared.fundamentals(self.rows, self.finance, asof=cutoff)
                pd.testing.assert_frame_equal(actual, expected[cutoff])
                self.assertEqual(actual.attrs, expected[cutoff].attrs)
                again = shared.fundamentals(copy.deepcopy(self.rows), copy.deepcopy(self.finance), asof=cutoff)
                pd.testing.assert_frame_equal(actual, again)
                self.assertEqual(actual.attrs, again.attrs)
                actual.iloc[0, actual.columns.get_loc("name")] = "不能污染缓存"
                self.assertEqual(shared.fundamentals(self.rows, self.finance, asof=cutoff).iloc[0]["name"], "样本")
            self.assertEqual(compute.call_count, 2)
        strict = shared.fundamentals(self.rows, self.finance, asof=self.day)
        live = shared.fundamentals(self.rows, self.finance, asof=None)
        self.assertTrue(pd.isna(strict.iloc[0]["net_profit"]))
        self.assertEqual(live.iloc[0]["net_profit"], 3.)

    def test_changed_source_members_and_finance_invalidate_and_next_scan_is_new(self):
        shared = PCI.CandidateScanInputs(self.now)
        with mock.patch.object(F, "compute_fundamental_factors",
                               wraps=F.compute_fundamental_factors) as compute:
            shared.fundamentals(self.rows, self.finance, asof=self.day)
            self.rows[0]["pe"] = 9.
            shared.fundamentals(self.rows, self.finance, asof=self.day)
            self.finance["data"]["600001"]["profit_yoy"] = 30.
            shared.fundamentals(self.rows, self.finance, asof=self.day)
            shared.fundamentals(self.rows + [{**self.rows[0], "code": "600002"}], self.finance, asof=self.day)
            PCI.CandidateScanInputs(self.now).fundamentals(self.rows, self.finance, asof=self.day)
            self.assertEqual(compute.call_count, 5)

    def test_future_snapshot_stays_unavailable_and_unsupported_metadata_bypasses_reuse(self):
        shared = PCI.CandidateScanInputs(self.now)
        self.rows[0]["quote_at"] = "2026-10-16T13:41:00+08:00"
        result = shared.fundamentals(self.rows, self.finance, asof=self.day)
        self.assertTrue(pd.isna(result.iloc[0]["pe"]))
        self.rows[0]["extra"] = object()
        with mock.patch.object(F, "compute_fundamental_factors",
                               wraps=F.compute_fundamental_factors) as compute:
            for _ in range(2):
                shared.fundamentals(self.rows, self.finance, asof=self.day)
            self.assertEqual(compute.call_count, 2)


class SourceSnapshotTests(unittest.TestCase):
    def test_usable_sources_read_once_and_copies_do_not_leak_between_strategies(self):
        universe = [{"code": "600001", "facts": {"risk": 0}}]
        finance = {"data": {"600001": {"profit_yoy": 20.}}}
        load_universe, load_finance = mock.Mock(return_value=universe), mock.Mock(return_value=finance)
        shared = PCI.CandidateScanInputs("2026-10-20T10:00:00+08:00")
        first_u, first_f = shared.universe(load_universe), shared.finance(load_finance)
        first_u[0]["facts"]["risk"] = 1
        first_f["data"]["600001"]["profit_yoy"] = -100
        self.assertEqual(shared.universe(load_universe), universe)
        self.assertEqual(shared.finance(load_finance), finance)
        self.assertEqual(load_universe.call_count, 1)
        self.assertEqual(load_finance.call_count, 1)
        next_round = PCI.CandidateScanInputs("2026-10-20T10:03:00+08:00")
        next_round.universe(load_universe)
        next_round.finance(load_finance)
        self.assertEqual(load_universe.call_count, 2)
        self.assertEqual(load_finance.call_count, 2)

    def test_failures_and_empty_sources_can_recover_within_round(self):
        shared = PCI.CandidateScanInputs("now")
        for method, empty, usable in ((shared.universe, [], [{"code": "600001"}]),
                                      (shared.finance, {"data": {}}, {"data": {"600001": {"pe": 8}}})):
            loader = mock.Mock(side_effect=[TimeoutError, empty, usable])
            with self.assertRaises(TimeoutError):
                method(loader)
            self.assertEqual(method(loader), empty)
            self.assertEqual(method(loader), usable)
            self.assertEqual(method(loader), usable)
            self.assertEqual(loader.call_count, 3)


class BatchedCandidateEvidenceTests(unittest.TestCase):
    def collect(self, *, fail=False):
        accounts = [{"id": key} for key in ("a", "b", "blocked")]
        candidates = {"a": ([{"code": "600001"}, {"code": "600002"}], {}),
                      "b": ([{"code": "600001"}, {"code": "600003"}], {}),
                      "blocked": ([], {"blocked": True})}
        events = []

        @contextmanager
        def readonly():
            yield None

        quotes = {"600001": {"price": 12., "cross": {"verified": False}},
                  "600003": {"price": 9.}}
        def query(codes):
            events.append("quotes")
            if fail:
                raise TimeoutError
            return quotes

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(PT, "_db_readonly", readonly))
            stack.enter_context(mock.patch.object(PT, "_db", side_effect=AssertionError("writer used")))
            stack.enter_context(mock.patch.object(PT, "_bootstrap_cooled_candidates",
                side_effect=lambda conn, account, rows, day: {"candidates": rows}))
            stack.enter_context(mock.patch.object(PT, "_news_for", side_effect=lambda names: events.append("news") or []))
            sector = stack.enter_context(mock.patch.object(PT.dfc, "fetch_sector_flow", return_value=[{"score": 1}]))
            provider = stack.enter_context(mock.patch.object(PT, "_quotes", side_effect=query))
            result = PT._bootstrap_candidate_evidence(accounts, candidates, dt.date(2026, 10, 16))
            provider.assert_called_once_with(["600001", "600002", "600003"])
            sector.assert_called_once_with("industry")
        self.assertEqual(events, ["news", "news", "quotes"])
        self.assertNotIn("blocked", result)
        return result, quotes

    def test_union_preserves_partial_missing_quotes_and_account_isolation(self):
        result, source = self.collect()
        self.assertEqual(set(result["a"]["quotes"]), {"600001"})
        self.assertEqual(set(result["b"]["quotes"]), {"600001", "600003"})
        result["a"]["quotes"]["600001"]["cross"]["verified"] = True
        self.assertFalse(result["b"]["quotes"]["600001"]["cross"]["verified"])
        self.assertFalse(source["600001"]["cross"]["verified"])
        result["a"]["sector_flow"][0]["score"] = 99
        self.assertEqual(result["b"]["sector_flow"][0]["score"], 1)

    def test_provider_failure_does_not_create_quote_evidence(self):
        result, _ = self.collect(fail=True)
        self.assertEqual(result["a"]["quotes"], {})
        self.assertEqual(result["b"]["quotes"], {})


if __name__ == "__main__":
    unittest.main()
