"""Risk optional evidence must not fetch while holding the ledger writer."""
import datetime as dt
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock

import paper_risk_evidence as E
import paper_trading as PT
import test_paper_risk_exit_production_path as fixtures


SIGNALS = ("lockup_penalty", "lhb_position_signal", "margin_signal",
           "block_trade_signal", "holder_signal")


def provider(callback):
    return SimpleNamespace(
        **{name: (lambda code, *, asof_day, name=name: callback(name, code, asof_day))
           for name in SIGNALS},
        fund_flow_trajectories=lambda *args, **kwargs: {},
    )


class PrefetchContractTests(unittest.TestCase):
    def test_unique_codes_and_exact_date_with_independent_source_failures(self):
        calls = []

        def fetch(name, code, day):
            calls.append((name, code, day))
            if name == "margin_signal":
                raise RuntimeError("optional fixture unavailable")
            if name == "holder_signal":
                return None  # Existing tuple-unpack failure fallback.
            return 2.0, name

        day = dt.date(2026, 9, 10)
        result = E.prefetch_position_alt_data(
            [{"code": "600001"}, {"code": "600001"}, {"code": "000001"}],
            day, alt_data=provider(fetch),
        )
        self.assertEqual(len(calls), 10)
        self.assertEqual(set(result), {(day, "600001"), (day, "000001")})
        self.assertTrue(all(call[2] == day for call in calls))
        for signals in result.values():
            self.assertEqual(signals["margin_signal"], (0.0, None))
            self.assertEqual(signals["holder_signal"], (0.0, None))
            self.assertEqual(signals["lhb_position_signal"], (2.0, "lhb_position_signal"))

    def test_no_provider_or_empty_positions_does_not_fetch(self):
        fail = mock.Mock(side_effect=AssertionError("no provider call"))
        self.assertEqual(E.prefetch_position_alt_data([], "2026-09-10", alt_data=provider(fail)), {})
        self.assertEqual(E.prefetch_position_alt_data([{"code": "600001"}], "2026-09-10", alt_data=None), {})
        fail.assert_not_called()


class RiskPrefetchProductionTests(fixtures.PaperRiskExitProductionPathTestCase):
    def seed(self):
        self._insert_lot("tq_breakout", self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code, price=9.0, pct=-8.0)

    def test_real_risk_path_allows_another_writer_during_each_source_call(self):
        self.seed()
        attempts = []
        with PT._db(immediate=True) as conn:
            conn.execute("CREATE TABLE fixture_heartbeat(beat INTEGER)")
            conn.execute("INSERT INTO fixture_heartbeat VALUES(0)")

        def fetch(name, code, day):
            probe = sqlite3.connect(self.db_path, timeout=0.03)
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.execute("UPDATE fixture_heartbeat SET beat=beat+1")
                probe.commit()
                attempts.append((name, True))
            except sqlite3.OperationalError:
                attempts.append((name, False))
            finally:
                probe.close()
            return 0.0, None

        PT.AD = provider(fetch)
        result = PT.monitor_risk(self.day)
        self.assertEqual(attempts, [(name, True) for name in SIGNALS])
        self.assertEqual(result["orders"][0]["status"], "filled")
        self.assertEqual(result["orders"][0]["qty"], 1000)
        with PT._db() as conn:
            self.assertEqual(conn.execute("SELECT beat FROM fixture_heartbeat").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_fills WHERE side='sell'").fetchone()[0], 1)

    def test_prefetched_review_matches_compatibility_and_never_refetches(self):
        self.seed()
        calls = []
        PT.AD = provider(lambda name, code, day: calls.append(name) or (2.0, name))
        with mock.patch.object(PT, "_completed_kline", return_value=None):
            deps = PT._risk_service_ports().evidence
            with PT._db() as conn:
                position = PT._position_rows(conn, asof_day=self.day)[0]
                kwargs = {"cycle_id": int(PT._active_cycle(conn)["id"]), "deps": deps}
                expected = E.position_quality_score(conn, position, self.quotes_map[self.code], self.day, **kwargs)
                evidence = E.prefetch_position_alt_data([position, position], self.day, alt_data=PT.AD)
                calls.clear()
                actual = E.position_quality_score(
                    conn, position, self.quotes_map[self.code], self.day,
                    alt_evidence=evidence, **kwargs,
                )
                missing = E.position_quality_score(
                    conn, position, self.quotes_map[self.code], self.day,
                    alt_evidence={}, **kwargs,
                )
                wrong_day = E.position_quality_score(
                    conn, position, self.quotes_map[self.code], self.day,
                    alt_evidence={(self.day + dt.timedelta(days=1), self.code): evidence[(self.day, self.code)]},
                    **kwargs,
                )
        self.assertEqual(calls, [])
        self.assertEqual(actual, expected)
        self.assertFalse(actual["alt_score_applied"])
        for result in (missing, wrong_day):
            for field in ("lockup_penalty", "lhb_bonus", "margin_bonus", "block_bonus", "holder_bonus"):
                self.assertEqual(result[field], 0.0)

    def test_cycle_change_during_prefetch_still_rejects_order_writes(self):
        self.seed()
        calls = []

        def fetch(name, code, day):
            if not calls:
                with PT._db(immediate=True) as conn:
                    conn.execute("UPDATE paper_cycles SET status='archived' WHERE id=1")
            calls.append(name)
            return 0.0, None

        PT.AD = provider(fetch)
        with self.assertRaises(PT.PRSS.RiskScanCycleChanged):
            PT.monitor_risk(self.day)
        self.assertEqual(calls, list(SIGNALS))
        with PT._db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders WHERE side='sell'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_fills WHERE side='sell'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM paper_risk_scan_runs").fetchone()[0], "failed")

    def test_empty_risk_scope_keeps_optional_sources_idle(self):
        fail = mock.Mock(side_effect=AssertionError("empty scope fetched"))
        PT.AD = provider(fail)
        self.assertEqual(PT.monitor_risk(self.day)["orders"], [])
        fail.assert_not_called()

    def reject_changed_lease(self, change_sql):
        self.seed()
        with PT._db(immediate=True) as conn:
            claimed, owner, _expiry = PT._claim_runtime_lease(conn, "fixture", "owner", "risk")
            self.assertTrue(claimed)
            token = conn.execute("SELECT fencing_token FROM paper_runtime_locks").fetchone()[0]
        PT._set_lease_context("fixture", owner, token)
        self.addCleanup(PT._clear_lease_context)
        calls = []

        def fetch(name, code, day):
            if not calls:
                with PT._db(immediate=True) as conn:
                    conn.execute(change_sql)
            calls.append(name)
            return 0.0, None

        PT.AD = provider(fetch)
        with self.assertRaisesRegex(RuntimeError, "^paper lease lost:"):
            PT.monitor_risk(self.day)
        self.assertEqual(calls, list(SIGNALS))
        with PT._db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_fills WHERE side='sell'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders WHERE side='sell'").fetchone()[0], 0)

    def test_handoff_during_prefetch_still_fences_old_owner(self):
        self.reject_changed_lease(
            "UPDATE paper_runtime_locks SET owner_key='replacement',fencing_token=fencing_token+1"
        )

    def test_expiry_during_prefetch_still_fences_old_owner(self):
        self.reject_changed_lease("UPDATE paper_runtime_locks SET expires_at='2000-01-01 00:00:00'")
