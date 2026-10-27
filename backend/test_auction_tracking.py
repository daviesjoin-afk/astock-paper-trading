"""竞价观测的时间、真实性、留存和无交易写入边界。"""
import datetime as dt
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import auction_tracking as A
import market_data_contract as MDC
import paper_schedule_contract as schedule


class AuctionTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict('os.environ', {'ASTOCK_DATA_DIR': str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.health = mock.patch.object(A.dfc, 'check_data_source_health', return_value={'healthy': True})
        self.health_probe = self.health.start()
        self.addCleanup(self.health.stop)
        self.calendar = mock.patch.object(A.universe, 'is_trade_day', return_value=True)
        self.calendar.start()
        self.addCleanup(self.calendar.stop)
        self.refresh = mock.patch.object(A.market, 'refresh_snapshot')
        self.provider = self.refresh.start()
        self.addCleanup(self.refresh.stop)

    def now(self, minute, *, day=22):
        return dt.datetime(2026, 10, day, 9, minute, tzinfo=A.CN)

    def quote(self, now, **updates):
        return {'code': '000001', 'price': 10, 'quote_at': now.isoformat(), **updates}

    def reading(self, rows, *, freshness=MDC.FRESHNESS_FRESH, reason=None):
        self.provider.return_value = SimpleNamespace(available=True, freshness=freshness, reason=reason, rows=lambda: rows)

    def test_first_tick_tracks_real_quotes_without_touching_ledger(self):
        now = self.now(15)
        ledger = self.root / 'paper_trading.sqlite3'
        ledger.write_bytes(b'ledger-unchanged')
        self.reading([self.quote(now, source='tencent_public_quote', main_net=None)])
        result = A.run(now)
        self.assertEqual('completed', result['status'])
        self.assertTrue(result['observation_only'])
        self.assertEqual(1, result['tracked_stocks'])
        self.assertEqual(b'ledger-unchanged', ledger.read_bytes())
        stored = json.loads((self.root / 'auction_tracking/current.json').read_text())
        self.assertEqual('cancellable', stored['observations'][0]['phase'])
        self.assertEqual(now.isoformat(), stored['observations'][0]['rows'][0]['quote_at'])
        self.provider.assert_called_once_with(MDC.AUCTION_TRACKING_POLICY, now=now, force=True)

    def test_outside_window_and_holidays_do_not_fetch_or_publish(self):
        for minute in (14, 25, 30):
            self.assertEqual('skipped', A.run(self.now(minute))['status'])
        self.calendar.stop()
        with mock.patch.object(A.universe, 'is_trade_day', return_value=False):
            self.assertEqual('skipped', A.run(self.now(15))['status'])
        self.provider.assert_not_called()
        self.health_probe.assert_not_called()
        self.assertFalse((self.root / 'auction_tracking').exists())

    def test_old_future_and_invalid_quotes_are_never_retimed(self):
        now = self.now(15)
        self.reading([self.quote(now - dt.timedelta(days=1)), self.quote(now + dt.timedelta(minutes=4)),
                      self.quote(now, price=None), self.quote(now, price=0)])
        result = A.run(now)
        self.assertEqual('blocked', result['status'])
        self.assertEqual(0, result['tracked_stocks'])
        self.assertEqual(0, A.status()['tracked_stocks'])

    def test_failed_refresh_records_blocked_observation_without_reusing_old_rows(self):
        now = self.now(18)
        self.reading([self.quote(now)], freshness=MDC.FRESHNESS_STALE, reason='refresh_failed')
        self.assertEqual('blocked', A.run(now)['status'])
        self.assertEqual('refresh_failed', A.status()['reason'])

    def test_price_only_quotes_remain_observation_and_do_not_claim_source_health(self):
        now = self.now(21)
        self.health_probe.return_value = {'healthy': False}
        self.reading([self.quote(now, main_net=None)], reason='price_only_coverage')
        result = A.run(now)
        self.assertEqual('completed', result['status'])
        self.assertFalse(result['source_healthy'])
        self.assertEqual('price_only_coverage', result['reason'])
        stored = json.loads((self.root / 'auction_tracking/current.json').read_text())
        self.assertEqual('non_cancellable', stored['observations'][0]['phase'])

    def test_retention_is_four_ticks_and_resets_next_day(self):
        for minute in (15, 18, 21, 24, 24):
            self.reading([self.quote(self.now(minute))])
            A.run(self.now(minute))
        self.assertEqual(4, A.status()['observations'])
        self.reading([self.quote(self.now(15, day=23))])
        A.run(self.now(15, day=23))
        self.assertEqual(1, A.status()['observations'])

    def test_read_status_never_fetches_and_schedule_starts_0915(self):
        self.assertEqual({'status': 'not_run'}, A.status())
        self.provider.assert_not_called()
        tracking = next(x for x in schedule.SCHEDULE_TASKS if x.slot == 'auction-track')
        self.assertEqual('09:15', tracking.clock)
        self.assertIn('15-24/3 9 * * 1-5 run_paper_slot.sh auction-track', schedule.render_cron_lines())

    def test_final_auction_uses_existing_rows_without_network_or_late_reconstruction(self):
        now = self.now(25)
        self.assertTrue(A.record_final([self.quote(now)],now,{'healthy':True}))
        self.provider.assert_not_called()
        self.assertEqual(1,A.status()['observations'])
        saved=json.loads((self.root/'auction_tracking/current.json').read_text())
        self.assertEqual('final_snapshot',saved['observations'][0]['phase'])
        self.assertFalse(A.record_final([self.quote(now)],self.now(40),{'healthy':True}))


class AuctionAmountSummaryTests(unittest.TestCase):
    """竞价金额：只汇总真实可用的金额，缺失不填零，并逐日落档。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = mock.patch.dict('os.environ', {'ASTOCK_DATA_DIR': str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def quote(self, now, **updates):
        row = {'code': '000001', 'price': 10.0, 'quote_at': now.isoformat()}
        row.update(updates)
        return row

    def test_summary_counts_only_rows_that_really_carry_amount(self):
        summary = A.summarize([
            {'code': '000001', 'name': '甲', 'pct': 1.5, 'amount': 1000.0, 'volume': 100},
            {'code': '000002', 'name': '乙', 'pct': -2.5, 'amount': 2500.0, 'volume': 200},
            {'code': '000003', 'name': '丙', 'pct': 0.0, 'amount': None, 'volume': 0},
        ])
        self.assertEqual(summary['tracked'], 3)
        self.assertEqual(summary['with_amount'], 2)
        self.assertEqual(summary['amount_total'], 3500.0)
        self.assertEqual(summary['volume_total'], 300.0)
        self.assertAlmostEqual(summary['amount_coverage_pct'], 66.67, places=2)
        self.assertEqual((summary['up'], summary['down'], summary['flat']), (1, 1, 1))
        self.assertEqual([row['code'] for row in summary['largest']], ['000002', '000001'])
        self.assertEqual(summary['unit'], {'amount': '元', 'volume': '手'})
        self.assertIn('未覆盖部分不计入', summary['boundary'])

    def test_missing_amounts_are_never_filled_with_zero_rows(self):
        summary = A.summarize([{'code': '000001'}, {'code': '000002', 'amount': 0}])
        self.assertEqual(summary['with_amount'], 0)
        self.assertEqual(summary['amount_total'], 0.0)
        self.assertEqual(summary['largest'], [])
        self.assertEqual(summary['amount_coverage_pct'], 0.0)
        self.assertIsNone(summary['mean_pct'])
        self.assertEqual(A.summarize([])['tracked'], 0)

    def test_final_snapshot_is_archived_once_per_day(self):
        now = dt.datetime(2026, 10, 26, 9, 25, tzinfo=A.CN)
        rows = [self.quote(now, code='600001', code_name='甲', amount=1234.5, volume=321.0),
                self.quote(now, code='600002', code_name='乙', amount=None)]
        self.assertTrue(A.record_final(rows, now, {'healthy': True}))
        archived = A.load_archived_day('2026-10-26')
        self.assertEqual(archived['trade_date'], '2026-10-26')
        self.assertEqual(len(archived['rows']), 2)
        self.assertEqual(archived['summary']['amount_total'], 1234.5)
        self.assertEqual([path.name for path in A.archived_days()], ['2026-10-26.json'])
        self.assertEqual(A.load_archived_day('2026-10-27'), {})

    def test_archive_prunes_to_the_retention_window(self):
        folder = self.root / 'auction_tracking'
        folder.mkdir(parents=True, exist_ok=True)
        start = dt.date(2026, 6, 1)
        names = [(start + dt.timedelta(days=offset)).isoformat() for offset in range(65)]
        for name in names:
            (folder / f'{name}.json').write_text('{}', encoding='utf-8')
        A._prune_archive(folder)
        kept = sorted(path.stem for path in folder.glob('*.json'))
        self.assertEqual(len(kept), A.ARCHIVE_KEEP_DAYS)
        self.assertEqual(kept, names[-A.ARCHIVE_KEEP_DAYS:])
        self.assertNotIn(names[0], kept)
