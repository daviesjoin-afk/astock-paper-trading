"""PR-3: bounded acquisition without reusing decisions or weakening evidence."""
import datetime as dt
import unittest
from unittest import mock

import paper_replacement_decision as Decision
import paper_replacement_evidence as Evidence
import paper_risk_evidence as Risk
import paper_risk_market_inputs as Inputs
import paper_trading as PT
from test_replacement_asof_provenance import _EvidenceCase, ACCOUNT, DAY, DAY_NEXT, DAY_PREV, OTHER
from test_paper_risk_exit_production_path import PaperRiskExitProductionPathTestCase


class PublicInterfaceTests(unittest.TestCase):
    def test_replacement_eligibility_is_declared_public(self):
        self.assertIn(
            "eligible_replacement_candidates",
            Risk.__all__,
        )


class ReplacementScopeTests(_EvidenceCase):
    def test_eligibility_and_winner_match_original_query_and_filters(self):
        deps = PT._risk_service_ports().evidence
        statuses = ("pending", "deferred_capacity", deps.entry_frozen_waitlist_status)
        cases = [
            ("600001", DAY, DAY, "pending", ACCOUNT),
            ("600002", DAY, DAY_PREV, "deferred_capacity", ACCOUNT),
            ("600003", DAY, DAY, deps.entry_frozen_waitlist_status, ACCOUNT),
            ("600004", DAY_NEXT, DAY, "pending", ACCOUNT),
            ("600005", DAY, DAY_NEXT, "pending", ACCOUNT),
            ("600006", DAY_PREV, DAY_PREV, "pending", ACCOUNT),
            ("600007", DAY, DAY, "filled", ACCOUNT),
            ("600008", DAY, DAY, "pending", OTHER),
            ("600009", DAY, DAY, "pending", ACCOUNT),
            ("600010", DAY, DAY, "pending", ACCOUNT),
        ]
        for index, (code, intended, signal, status, account) in enumerate(cases, 1):
            self.add_signal(index, code=code, intended_date=str(intended), signal_date=str(signal),
                            status=status, account_id=account, entry_score=70 + index)
        # Exercise security filtering independently of changing exchange fixture data.
        def scope(code, *args):
            return {"allowed": code != "600010"}
        held = {"600009"}
        rows = Evidence.load_replacement_candidates(
            self.conn, account_id=ACCOUNT, asof_day=str(DAY), statuses=statuses,
        )
        with mock.patch.object(Risk, "_security_scope", side_effect=scope):
            original_allowed = [row for row in rows if scope(row["code"])["allowed"]]
            expected = Decision.choose_best_candidate(original_allowed, held_codes=held)
            actual_rows = Risk.eligible_replacement_candidates(self.conn, ACCOUNT, DAY, held, deps=deps)
            actual = Risk.best_replacement_candidate(self.conn, ACCOUNT, DAY, held, deps=deps)
        self.assertEqual(actual_rows, [r for r in original_allowed if r["code"] not in held])
        self.assertEqual({r["code"] for r in actual_rows}, {"600001", "600002", "600003"})
        self.assertEqual(actual, expected)  # Includes score, name, status, signal ID, date.

    def test_same_score_order_and_writer_recheck_are_preserved(self):
        deps = PT._risk_service_ports().evidence
        for ident, code in enumerate(("600001", "600002"), 1):
            self.add_signal(ident, code=code, intended_date=str(DAY))
        with mock.patch.object(Risk, "_security_scope", return_value={"allowed": True}):
            before = Risk.best_replacement_candidate(self.conn, ACCOUNT, DAY, set(), deps=deps)
            self.assertEqual(before["signal_id"], 2)
            self.conn.execute("UPDATE paper_signals SET status='expired' WHERE id=2")
            after = Risk.best_replacement_candidate(self.conn, ACCOUNT, DAY, set(), deps=deps)
            self.assertEqual(after["signal_id"], 1)


class AcquisitionTests(unittest.TestCase):
    def ports(self):
        return Inputs.RiskMarketInputs(
            market_context=mock.Mock(return_value={"source_at": "original"}),
            quotes=mock.Mock(return_value={"600001": {"quote_at": "original"}}),
            news=mock.Mock(return_value=[{"code": "600001", "tone": -1, "source": "fixture"}]),
            news_meta=mock.Mock(return_value={"stale": True, "error": "source failed"}),
            trajectories=mock.Mock(return_value={"600001": {"status": "ok", "source_at": "original"}}),
        )

    def test_union_quotes_holding_only_flow_and_unchanged_news(self):
        ports = self.ports()
        day = dt.date(2026, 10, 30)
        positions = [{"code": "600001", "name": "held"}, {"code": "600001", "name": "held"}]
        result = Inputs.load(day=day, positions=positions,
                             candidate_codes={"600001", "600002", "600003"}, ports=ports)
        ports.quotes.assert_called_once_with(["600001", "600002", "600003"], asof_date=day)
        ports.trajectories.assert_called_once_with(["600001"], asof_day=day)
        ports.news.assert_called_once_with({"600001": "held"})
        self.assertEqual(result["news_meta"], {"stale": True, "error": "source failed"})
        self.assertEqual(result["news"], ports.news.return_value)
        self.assertEqual(result["flow_trajectory_map"], ports.trajectories.return_value)
        self.assertEqual(result["quote_map"], ports.quotes.return_value)

    def test_flow_failure_remains_explicit_and_next_load_retries(self):
        ports = self.ports()
        ports.trajectories.side_effect = [TimeoutError("fixture"), {"600001": {"status": "ok"}}]
        args = dict(day="2026-10-30", positions=[{"code": "600001"}],
                    candidate_codes={"600002"}, ports=ports)
        first = Inputs.load(**args)
        self.assertEqual(first["flow_trajectory_map"]["600001"]["status"], "source_unavailable")
        self.assertEqual(first["flow_trajectory_map"]["600001"]["error"], "TimeoutError")
        self.assertFalse(first["flow_trajectory_map"]["600001"]["score_applied"])
        self.assertEqual(Inputs.load(**args)["flow_trajectory_map"], {"600001": {"status": "ok"}})
        self.assertEqual(ports.trajectories.call_count, 2)

    def test_no_holdings_skips_candidate_sources(self):
        ports = self.ports()
        result = Inputs.load(day="2026-10-30", positions=[], candidate_codes={"600002"}, ports=ports)
        self.assertEqual(result["quote_map"], {})
        for source in (ports.news, ports.quotes, ports.trajectories):
            source.assert_not_called()


class ProductionAcquisitionTests(PaperRiskExitProductionPathTestCase):
    def test_real_service_requests_only_eligible_quote_union_and_holding_flow(self):
        self._insert_lot(ACCOUNT, self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code, price=10.0, pct=0.0)
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_accounts SET cycle_id=1 WHERE id=?", (OTHER,))
            for code, intended, signal, account in (
                (self.code, DAY, DAY, ACCOUNT),  # already held
                ("600002", DAY, DAY_PREV, ACCOUNT),  # valid overnight
                ("600003", DAY_NEXT, DAY, ACCOUNT),
                ("600004", DAY, DAY_NEXT, ACCOUNT),
                ("600005", DAY_PREV, DAY_PREV, ACCOUNT),
                ("600006", DAY, DAY, OTHER),  # no position consumer in this account
            ):
                conn.execute(
                    "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,"
                    "rank_score,t_score,payload,status,created_at,"
                    "strategy_id,strategy_version,strategy_checksum,cycle_id) "
                    "VALUES(?,?,?,?,?,60,60,'{}','pending',?,?,?,?,?)",
                    (account, str(signal), str(intended), code, "测试股", str(signal) + " 09:00:00",
                     *PT._strategy_stamp(conn, account),
                     conn.execute("SELECT cycle_id FROM paper_accounts WHERE id=?", (account,)).fetchone()[0]),
                )
            legacy_codes = {r[0] for r in conn.execute(
                "SELECT DISTINCT code FROM paper_signals WHERE status='pending'",
            )}
        quote_source = mock.Mock(wraps=PT._quotes)
        flow_source = mock.Mock(return_value={})
        with mock.patch.object(PT, "_quotes", quote_source), \
             mock.patch.object(PT, "AD") as alt, \
             mock.patch.object(Risk, "_security_scope", return_value={"allowed": True}):
            alt.fund_flow_trajectories = flow_source
            result = PT.monitor_risk(self.day)
        self.assertEqual(result["orders"], [])
        quote_source.assert_called_once_with(["600001", "600002"], asof_date=self.day)
        flow_source.assert_called_once_with(["600001"], asof_day=self.day)
        self.assertEqual(len(legacy_codes), 6)  # Before: quote + trajectory for every pending code.
        self.assertEqual(len(quote_source.call_args.args[0]), 2)
        self.assertEqual(len(flow_source.call_args.args[0]), 1)
