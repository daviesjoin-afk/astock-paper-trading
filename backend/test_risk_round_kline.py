"""Round-local risk facts, PIT boundaries and prefetch failure handoff."""
import datetime as dt
import sqlite3
import unittest
from unittest import mock

import pandas as pd

import data_fetcher as Data
from paper_kline_snapshot import RoundKlineCache
import paper_risk_service as Service
import paper_trading as PT
from test_risk_application_service import _RiskServiceCase


def bars(day):
    index = pd.date_range(day - dt.timedelta(days=100), day + dt.timedelta(days=1))
    return pd.DataFrame({"open": 10.0, "high": 10.5, "low": 9.5,
                         "close": 10.0, "volume": 1000.0}, index=index)


class PrefetchContractTests(unittest.TestCase):
    def test_owner_preserves_cutoff_and_inclusive_and_independent_copies(self):
        day = dt.date(2026, 9, 10)
        frame = bars(day)
        with mock.patch.object(Data, "load_cached_kline", return_value=frame) as load:
            cache = RoundKlineCache(Data.completed_kline)
            for inclusive in (False, True):
                cache.prefetch("600001", day, inclusive=inclusive)
                actual = cache.get("600001", day, inclusive=inclusive)
                expected = frame.loc[frame.index.date <= day] if inclusive else frame.loc[frame.index.date < day]
                pd.testing.assert_frame_equal(actual, expected)
                actual.iloc[0, 0] = 999
                actual.attrs["source"] = "consumer mutation"
                again = cache.get("600001", day, inclusive=inclusive)
                pd.testing.assert_frame_equal(again, expected)
                self.assertNotIn("source", again.attrs)
            self.assertEqual(load.call_count, 2)
            cache.get("600002", day, inclusive=False)
            cache.get("600001", day - dt.timedelta(days=1), inclusive=False)
            self.assertEqual(load.call_count, 4)
            RoundKlineCache(Data.completed_kline).get("600001", day, inclusive=False)
            self.assertEqual(load.call_count, 5)

    def test_missing_empty_and_failure_are_delivered_once_then_retried(self):
        day = dt.date(2026, 9, 10)
        for first in (None, pd.DataFrame(), OSError("corrupt fixture")):
            with self.subTest(first=type(first).__name__):
                loader = mock.Mock(side_effect=[first, bars(day)])
                cache = RoundKlineCache(loader)
                cache.prefetch("600001", day, inclusive=False)
                cache.prefetch("600001", day, inclusive=False)
                self.assertEqual(loader.call_count, 1)
                if isinstance(first, Exception):
                    with self.assertRaisesRegex(OSError, "corrupt fixture"):
                        cache.get("600001", day, inclusive=False)
                elif first is None:
                    self.assertIsNone(cache.get("600001", day, inclusive=False))
                else:
                    self.assertTrue(cache.get("600001", day, inclusive=False).empty)
                pd.testing.assert_frame_equal(cache.get("600001", day, inclusive=False), bars(day))
                cache.get("600001", day, inclusive=False)
                self.assertEqual(loader.call_count, 2)


class RiskRoundKlineTests(_RiskServiceCase):
    def seed(self):
        self.add_lot(1000, 10.0)
        self.set_sell_quote()

    def loader(self, records, callback=None):
        def load(code, cutoff, *, inclusive=True):
            probe = sqlite3.connect(PT.DB_PATH, timeout=0.02)
            try:
                probe.execute("BEGIN IMMEDIATE")
                unlocked = True
                probe.rollback()
            except sqlite3.OperationalError:
                unlocked = False
            finally:
                probe.close()
            records.append((code, cutoff, inclusive, unlocked))
            if callback:
                callback(code, inclusive, len(records))
            frame = bars(self.day)
            mask = frame.index.date <= cutoff if inclusive else frame.index.date < cutoff
            return frame.loc[mask].copy()
        return load

    def test_successful_reads_once_per_boundary_outside_writer_across_accounts_and_rounds(self):
        self.seed()
        self.add_lot_for_account("trend_pullback", self.code, 100, 10.0)
        records = []
        load = self.loader(records)
        original_ports = PT._risk_service_ports()
        with mock.patch.object(PT, "_completed_kline", side_effect=load):
            result = self.run_risk()
            self.assertTrue(result["orders"])
            self.assertEqual(records, [(self.code, self.day, False, True), (self.code, self.day, True, True)])
            self.add_lot(100, 10.0)
            self.clear_scan_state()
            self.run_risk()
        # Both rounds are independent; a remaining position gets its own reads.
        self.assertTrue(all(item[3] for item in records))
        self.assertEqual(len(records), 4)
        self.assertIs(original_ports.evidence.load_kline, PT._completed_kline)

    def test_same_run_audit_date_text_uses_the_date_key(self):
        self.seed()
        records = []
        original = Service.PDA.with_decision_snapshot
        def snapshot(payload=None, **kwargs):
            # Audit fallback uses an ISO string while models use date objects.
            if getattr(kwargs["kline_loader"], "__name__", None) == "round_kline":
                frame = kwargs["kline_loader"](self.code, self.day.isoformat(), inclusive=True)
                self.assertEqual(frame.index.max().date(), self.day)
            return original(payload, **kwargs)
        with mock.patch.object(PT, "_completed_kline", side_effect=self.loader(records)), \
             mock.patch.object(Service.PDA, "with_decision_snapshot", side_effect=snapshot):
            self.run_risk()
        self.assertEqual(len(records), 2)

    def test_prefetch_error_reaches_original_consumer_without_extra_attempt(self):
        self.seed()
        calls = []
        def load(code, cutoff, *, inclusive=True):
            calls.append(inclusive)
            if not inclusive:
                raise OSError("risk CSV corrupt")
            return bars(self.day)
        with mock.patch.object(PT, "_completed_kline", side_effect=load):
            with self.assertRaisesRegex(OSError, "risk CSV corrupt"):
                self.run_risk()
        self.assertEqual(calls, [False, True])
        self.assertEqual(self.sell_fills(), [])
        self.assertEqual(self.conn.execute("SELECT status FROM paper_risk_scan_runs").fetchone()[0], "failed")

    def test_unused_audit_error_does_not_fail_a_valid_sell(self):
        self.seed()
        def load(code, cutoff, *, inclusive=True):
            if inclusive:
                raise OSError("unused audit boundary")
            return bars(self.day).loc[lambda frame: frame.index.date < cutoff]
        with mock.patch.object(PT, "_completed_kline", side_effect=load):
            self.assertEqual(self.run_risk()["orders"][0]["status"], "filled")

    def test_cycle_change_during_prefetch_fences_writes(self):
        self.seed()
        def change(code, inclusive, count):
            if count == 1:
                with PT._db(immediate=True) as conn:
                    conn.execute("UPDATE paper_cycles SET status='archived' WHERE id=?", (self.cycle,))
        with mock.patch.object(PT, "_completed_kline", side_effect=self.loader([], change)):
            with self.assertRaises(PT.PRSS.RiskScanCycleChanged):
                self.run_risk()
        self.assertEqual(self.sell_fills(), [])

    def reject_changed_lease(self, sql):
        self.seed()
        with PT._db(immediate=True) as conn:
            claimed, owner, _ = PT._claim_runtime_lease(conn, "fixture", "owner", "risk")
            self.assertTrue(claimed)
            token = conn.execute("SELECT fencing_token FROM paper_runtime_locks").fetchone()[0]
        PT._set_lease_context("fixture", owner, token)
        self.addCleanup(PT._clear_lease_context)
        def change(code, inclusive, count):
            if count == 1:
                with PT._db(immediate=True) as conn:
                    conn.execute(sql)
        with mock.patch.object(PT, "_completed_kline", side_effect=self.loader([], change)):
            with self.assertRaisesRegex(RuntimeError, "^paper lease lost:"):
                self.run_risk()
        self.assertEqual(self.sell_fills(), [])

    def test_prefetch_owner_handoff_still_fences_writes(self):
        self.reject_changed_lease(
            "UPDATE paper_runtime_locks SET owner_key='new-owner',fencing_token=fencing_token+1"
        )

    def test_prefetch_lease_expiry_still_fences_writes(self):
        self.reject_changed_lease("UPDATE paper_runtime_locks SET expires_at='2000-01-01 00:00:00'")

    def test_new_holding_is_rechecked_and_uses_original_lazy_read(self):
        self.seed()
        records = []
        def change(code, inclusive, count):
            if count == 1:
                self.add_lot_for(self.code_b, 100, 10.0)
        with mock.patch.object(PT, "_completed_kline", side_effect=self.loader(records, change)):
            self.run_risk()
        self.assertIn((self.code_b, self.day, False, False), records)
        self.assertIsNotNone(self.review_detail(self.code_b).get("score"))
