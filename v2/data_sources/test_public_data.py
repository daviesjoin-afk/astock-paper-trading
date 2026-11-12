"""Public contracts against synthetic data, never the research/real ledger."""
from contextlib import closing
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import a_stock_data
import ingest_market
import realtime_quotes
from public_data import PublicDataClient, PublicDataError, main
from source_http import SourceHttp, SourceHttpError
from test_source_http import Response, NOW
from test_ingest_market import build_package
from test_realtime_quotes import payload


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = []
        self.now = NOW
        self.body = ('v_sh600000="' + payload() + '";').encode('gbk')
        def opener(request, timeout):
            self.calls.append(request.full_url)
            return Response(self.body)
        self.http = SourceHttp(self.root/'cache', clock=lambda: self.now, opener=opener)
        self.client = PublicDataClient(self.http)

    def error(self, code, call):
        with self.assertRaises(SourceHttpError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_quote_units_source_and_observation(self):
        row = self.client.quotes(['600000.SH'])['rows'][0]
        self.assertEqual(row['price_cents'], 1017)
        self.assertEqual(row['volume_shares'], 48_504_600)
        self.assertEqual(row['amount_cents'], 4_963_754_000_000)
        self.assertEqual(row['observed_at'], '2026-11-11T15:06:19+08:00')
        self.assertEqual(row['fetched_at'], NOW.isoformat())
        self.assertEqual(row['source'], 'tencent-quotes')
        self.assertEqual(row['adjust'], 'none')

    def test_quote_consumer_and_client_share_snapshot(self):
        quotes = realtime_quotes.fetch_quotes(['600000.SH'], http=self.http)
        self.assertEqual(len(quotes), 1)
        result = self.client.quotes(['600000.SH'])
        self.assertTrue(result['snapshots'][0]['cache_hit'])
        self.assertEqual(quotes[0].price_cents, result['rows'][0]['price_cents'])
        self.assertEqual(len(self.calls), 1)

    def test_quote_reordered_records_match_identity(self):
        self.body = ('v_sz000001="' + payload(f2='000001', f3='12.34') + '";v_sh600000="' + payload() + '";').encode('gbk')
        rows = self.client.quotes(['600000.SH', '000001.SZ'])['rows']
        self.assertEqual([(r['symbol'], r['price_cents']) for r in rows], [('000001.SZ', 1234), ('600000.SH', 1017)])

    def test_quote_missing_malformed_or_misidentified_never_cached(self):
        cases = [('quote_missing', b''), ('quote_identity_mismatch', ('v_sh600000="'+payload(f2='000001')+'";').encode('gbk')),
                 ('quote_missing', b'v_sh600000="";'), ('quote_invalid_record', b'<html>blocked</html>')]
        for code, body in cases:
            self.body = body
            with self.subTest(code=code, body=body[:20]):
                # Empty HTTP bytes have a transport error, distinct from missing records.
                expected = 'upstream_empty' if not body else code
                self.error(expected, lambda: self.client.quotes(['600000.SH']))
                self.assertEqual(list((self.root/'cache').glob('*.json')), [])

    def test_quote_partial_response_is_explicit_failure(self):
        self.error('quote_missing', lambda: self.client.quotes(['600000.SH', '000001.SZ']))
        self.assertEqual(list((self.root/'cache').glob('*.json')), [])

    def test_future_quote_observation_rejected_without_cache(self):
        self.body = ('v_sh600000="'+payload(f30='20261113150619')+'";').encode('gbk')
        self.error('observation_after_as_of', lambda: self.client.quotes(['600000.SH']))
        self.assertEqual(list((self.root/'cache').glob('*.json')), [])

    def test_quotes_historical_requires_frozen_fetch(self):
        self.client.quotes(['600000.SH'])
        self.error('snapshot_after_as_of', lambda: self.client.quotes(['600000.SH'], as_of='2026-11-11T20:00:00+08:00'))
        self.assertEqual(len(self.calls), 1)

    def test_symbol_and_adjust_rejection_precedes_network(self):
        for symbol in ('600000', '000001.SH', '600000.SZ', '600000.sh'):
            with self.assertRaises(PublicDataError):
                self.client.quotes([symbol])
        self.error('unsupported_adjust', lambda: self.client.bars('600000.SH', '2026-11-11', '2026-11-11', adjust='qfq'))
        self.assertEqual(self.calls, [])

    def package(self):
        return build_package('20261111', {'sh': [('600000', '浦发银行', 10.17, 10.20)],
                                          'sz': [('000001', '平安银行', 12.34, 12.30)],
                                          'bj': [('430047', '测试公司', 8.5, 8.4)]})

    def test_actual_tdx_consumers_and_symbols_share_full_day_cache(self):
        self.body = self.package()
        with patch.dict(ingest_market.MIN_PRICED, {'sh': 1, 'sz': 1, 'bj': 1}), patch.dict(a_stock_data.TDX_MIN_PRICED, {'sh': 1, 'sz': 1}):
            raw = ingest_market.fetch_package('2026-11-11', http=self.http)
            tdx = a_stock_data.fetch_tdx_daily('2026-11-11', http=self.http)
            first = self.client.bars('600000.SH', '2026-11-11', '2026-11-11')['rows'][0]
            second = self.client.bars('000001.SZ', '2026-11-11', '2026-11-11')['rows'][0]
            bj = self.client.bars('430047.BJ', '2026-11-11', '2026-11-11')['rows'][0]
        self.assertEqual(raw, self.body)
        self.assertEqual(tdx.bar('sh', '600000').volume, first['volume_shares'])
        self.assertEqual((first['close_cents'], second['close_cents'], bj['close_cents']), (1017, 1234, 850))
        self.assertEqual(first['sha256'], second['sha256'])
        self.assertTrue(first['cache_hit'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(list((self.root/'cache').glob('*.json'))), 1)

    def test_tdx_invalid_package_not_cached_by_real_consumers(self):
        self.body = b'PK not a real zip'
        for fetch in (ingest_market.fetch_package, a_stock_data.fetch_tdx_daily):
            with self.assertRaises(Exception):
                fetch('2026-11-11', http=self.http)
            self.assertEqual(list((self.root/'cache').glob('*.json')), [])
        self.assertEqual(len(self.calls), 2)

    def test_tdx_missing_symbol_explicit_without_fill(self):
        self.body = self.package()
        with patch.dict(ingest_market.MIN_PRICED, {'sh': 1, 'sz': 1, 'bj': 1}):
            self.error('bars_missing', lambda: self.client.bars('600001.SH', '2026-11-11', '2026-11-11'))
        self.assertEqual(len(self.calls), 1)

    def make_database(self, adjust='none'):
        db = self.root/'bars.sqlite'
        with closing(sqlite3.connect(db)) as connection:
            connection.execute('CREATE TABLE bar(symbol TEXT, day TEXT, close_cents INTEGER, volume_shares INTEGER, adjust TEXT, source TEXT)')
            connection.execute('INSERT INTO bar VALUES(?,?,?,?,?,?)', ('600000.SH', '2026-11-11', 1017, 12300, adjust, 'tdx'))
            connection.commit()
        self.client.bar_db = db
        return db

    def test_local_database_is_read_only_closed_and_no_implicit_fill(self):
        db = self.make_database()
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        actual_connect = sqlite3.connect
        connections = []
        def connect(*args, **kwargs):
            self.assertTrue(args[0].endswith('?mode=ro'))
            self.assertTrue(kwargs['uri'])
            connection = actual_connect(*args, **kwargs)
            connections.append(connection)
            return connection
        with patch('public_data.sqlite3.connect', side_effect=connect):
            result = self.client.bars('600000.SH', '2026-11-10', '2026-11-12', source='local')
        self.assertEqual([r['day'] for r in result['rows']], ['2026-11-11'])
        self.assertEqual(result['coverage'], 'stored_rows_only')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['missing_calendar_days'], ['2026-11-10', '2026-11-12'])
        self.assertIsNone(result['rows'][0]['fetched_at'])
        self.assertEqual(before, hashlib.sha256(db.read_bytes()).hexdigest())
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute('SELECT 1')
        self.assertEqual(self.calls, [])

    def test_local_missing_db_not_created(self):
        self.client.bar_db = self.root/'missing.sqlite'
        self.error('local_database_missing', lambda: self.client.bars('600000.SH', '2026-11-11', '2026-11-11', source='local'))
        self.assertFalse(self.client.bar_db.exists())

    def test_local_adjusted_data_refused_and_connection_closed(self):
        self.make_database(adjust='qfq')
        self.error('bars_missing', lambda: self.client.bars('600000.SH', '2026-11-11', '2026-11-11', source='local'))

    def test_local_history_without_fetch_provenance_is_unsupported(self):
        self.make_database()
        self.error('historical_local_snapshot_unsupported', lambda: self.client.bars('600000.SH', '2026-11-11', '2026-11-11', source='local', as_of=NOW.isoformat()))

    def test_unknown_provider_explicitly_unsupported(self):
        self.error('unsupported_source', lambda: self.client.company('600000.SH', source='tdx'))
        self.error('unsupported', lambda: self.client.company('600000.SH', source='szse'))
        self.error('unsupported_source', lambda: self.client.announcements('600000.SH', '2026-11-01', '2026-11-11', source='szse'))
        self.assertEqual(self.calls, [])

    def test_company_hook_snapshot_freezes_actual_availability(self):
        calls = []
        def provider(symbol, *, fetched_at, fetch):
            calls.append((symbol, fetched_at))
            return {'symbol': symbol, 'name': 'current company', 'published_day': '2020-01-01'}
        self.client.company_provider = provider
        self.client.provider_source = 'fixture-v1'
        self.error('historical_snapshot_unavailable', lambda: self.client.company('600000.SH', as_of='2020-01-01T20:00:00+08:00'))
        self.assertEqual(calls, [])
        first = self.client.company('600000.SH')
        self.error('snapshot_after_as_of', lambda: self.client.company('600000.SH', as_of='2020-01-01T20:00:00+08:00'))
        frozen = self.client.company('600000.SH', as_of=NOW.isoformat())
        self.assertEqual(len(calls), 1)
        self.assertTrue(frozen['cache_hit'])
        self.assertEqual(first['observed_at'], NOW.isoformat())
        self.assertEqual(frozen['availability'], 'frozen_fetch_time')

    def test_date_only_announcements_cannot_be_backdated(self):
        calls = []
        def provider(symbol, start, end, *, fetched_at, fetch, page_size, max_pages):
            calls.append((symbol, start, end, page_size, max_pages))
            return {'items': [{'published_at': '2026-11-11', 'precision': 'day'}]}
        self.client.announcements_provider = provider
        self.client.provider_source = 'fixture-v1'
        result = self.client.announcements('600000.SH', '2026-11-01', '2026-11-11', page_size=2, max_pages=3)
        self.assertEqual(calls, [('600000.SH', '2026-11-01', '2026-11-11', 2, 3)])
        self.assertEqual(result['observed_at'], NOW.isoformat())
        self.error('snapshot_after_as_of', lambda: self.client.announcements('600000.SH', '2026-11-01', '2026-11-11', page_size=2, max_pages=3, as_of='2026-11-11T23:59:59+08:00'))

    def test_provider_failure_not_cached_and_fetch_signature_supported(self):
        def provider(symbol, *, fetched_at, fetch):
            self.assertEqual(fetch('https://fixture.test/', method='POST', data=b'a', headers={'X-Test': 'yes'}), self.body)
            raise PublicDataError('provider_failed')
        self.client.company_provider = provider
        self.client.provider_source = 'fixture-v1'
        self.error('provider_failed', lambda: self.client.company('600000.SH'))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list((self.root/'cache').glob('*.json')), [])

    def test_provider_empty_distinct_from_confirmed_no_announcements(self):
        self.client.company_provider = lambda *a, **kw: {}
        self.client.provider_source = 'fixture-v1'
        self.error('provider_empty_result', lambda: self.client.company('600000.SH'))
        self.client.announcements_provider = lambda *a, **kw: {'status': 'empty', 'items': []}
        result = self.client.announcements('600000.SH', '2026-11-01', '2026-11-11')
        self.assertEqual(result['data']['status'], 'empty')

    def test_quote_decimal_amount_normalizes_without_float_drift(self):
        self.body = ('v_sh600000="'+payload(f37='123.456789')+'";').encode('gbk')
        row = self.client.quotes(['600000.SH'])['rows'][0]
        self.assertEqual(row['amount_cents'], 123456789)

    def test_quote_fractional_cent_and_nan_prices_rejected_without_cache(self):
        for price in ('10.171', 'NaN', 'garbage'):
            self.body = ('v_sh600000="'+payload(f3=price)+'";').encode('gbk')
            self.error('quote_invalid_price', lambda: self.client.quotes(['600000.SH']))
        self.assertEqual(list((self.root/'cache').glob('*.json')), [])

    def test_all_crosscheck_sources_honor_offline_without_network(self):
        self.http.offline = True
        for call in (lambda: a_stock_data.fetch_tencent_daily('600000.SH', '2026-11-11', '2026-11-11', http=self.http),
                     lambda: a_stock_data.fetch_tdx_daily('2026-11-11', http=self.http),
                     lambda: a_stock_data.fetch_sina_adjust('600000.SH', http=self.http)):
            with self.assertRaises(a_stock_data.DataSourceError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'offline_cache_miss')
        self.assertEqual(self.calls, [])

    def test_provider_source_and_symbol_caches_cannot_alias(self):
        calls = []
        def provider(symbol, **kw):
            calls.append(symbol)
            return {'symbol': symbol, 'name': 'company'}
        self.client.company_provider = provider
        self.client.provider_source = 'first-v1'
        self.client.company('600000.SH')
        self.client.company('000001.SZ')
        self.client.provider_source = 'second-v1'
        self.client.company('600000.SH')
        self.assertEqual(calls, ['600000.SH', '000001.SZ', '600000.SH'])
        self.assertEqual(len(list((self.root/'cache').glob('*.json'))), 3)

    def test_provider_incomplete_or_wrong_symbol_not_cached(self):
        self.client.provider_source = 'fixture-v1'
        for result, code in (({'complete': False}, 'provider_incomplete_result'),
                             ({'symbol': '000001.SZ'}, 'provider_symbol_mismatch')):
            self.client.company_provider = lambda *a, **kw: result
            self.error(code, lambda: self.client.company('600000.SH'))
        self.assertEqual(list((self.root/'cache').glob('*.json')), [])

    def test_cli_unsupported_and_offline_miss_are_json_errors(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            code = main(['company', '--symbol', '600000.SH', '--source', 'szse'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())['code'], 'unsupported')
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            code = main(['quotes', '--symbol', '600000.SH', '--offline'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())['code'], 'offline_cache_miss')


    def fixture_transport(self, *responses):
        pending = list(responses)
        def opener(request, timeout):
            self.calls.append(request.full_url)
            return Response(json.dumps(pending.pop(0)).encode('utf-8'))
        self.http.opener = opener

    def test_default_real_modules_cache_separately(self):
        from test_company_disclosures import PROFILE, IDENTITY, page
        self.fixture_transport(PROFILE, [IDENTITY], page())
        company = self.client.company('000001.SZ')
        news = self.client.announcements('000001.SZ', '2026-10-20', '2026-10-27')
        self.assertEqual((company['source'], news['source']), ('szse', 'cninfo'))
        self.assertEqual(company['data']['rows'][0]['source'], 'szse')
        self.assertEqual(news['data']['rows'][0]['source'], 'cninfo')
        self.assertIsNone(news['data']['rows'][0]['published_at'])
        self.http.offline = True
        self.assertTrue(self.client.company('000001.SZ')['cache_hit'])
        self.assertTrue(self.client.announcements('000001.SZ', '2026-10-20', '2026-10-27')['cache_hit'])
        docs = [json.loads(p.read_text()) for p in (self.root/'cache').glob('*.json')]
        self.assertEqual({d['identity']['source'] for d in docs}, {'szse:company:v1', 'cninfo:announcements:v1'})
        self.assertEqual(len(self.calls), 3)

    def test_real_default_cli_without_hooks(self):
        from test_company_disclosures import PROFILE, IDENTITY, page
        self.fixture_transport(PROFILE, [IDENTITY], page())
        def factory(*a, **kw):
            return SourceHttp(*a, **kw) if 'opener' in kw else self.http
        for args, source in ((['company', '--symbol', '000001.SZ'], 'szse'),
                             (['announcements', '--symbol', '000001.SZ', '--start-day', '2026-10-20', '--end-day', '2026-10-27'], 'cninfo')):
            with patch('public_data.SourceHttp', side_effect=factory), patch('sys.stdout', new_callable=io.StringIO) as output:
                code = main(args)
            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())['source'], source)

    def test_real_incomplete_provider_leaves_no_intermediate_cache(self):
        from test_company_disclosures import IDENTITY, page
        self.fixture_transport([IDENTITY], page(total=2, more=True), dict(code=99))
        self.error('Incomplete', lambda: self.client.announcements('000001.SZ', '2026-10-20', '2026-10-27', page_size=1))
        self.assertEqual(list((self.root/'cache').glob('*.json')), [])
        self.assertEqual(len(self.calls), 3)

    def test_default_szse_failure_never_switches_provider(self):
        self.fixture_transport(dict(code=99))
        self.error('UpstreamBusinessError', lambda: self.client.company('000001.SZ'))
        self.assertEqual(len(self.calls), 1)
        self.assertIn('szse.cn', self.calls[0])
        self.assertEqual(list((self.root/'cache').glob('*.json')), [])

    def test_provider_versions_invalidate_independently(self):
        from test_company_disclosures import PROFILE, IDENTITY, page
        self.fixture_transport(PROFILE, [IDENTITY], page(), PROFILE, [IDENTITY], page())
        self.client.company('000001.SZ')
        self.client.announcements('000001.SZ', '2026-10-20', '2026-10-27')
        self.client.company_provider_version = '2'
        self.assertFalse(self.client.company('000001.SZ')['cache_hit'])
        self.assertTrue(self.client.announcements('000001.SZ', '2026-10-20', '2026-10-27')['cache_hit'])
        self.client.announcements_provider_version = '3'
        self.assertFalse(self.client.announcements('000001.SZ', '2026-10-20', '2026-10-27')['cache_hit'])
        self.assertEqual(len(self.calls), 6)

    def test_cli_invalid_cache_and_historical_intent_are_json_errors(self):
        cases = [(['--cache-ttl', value], 'invalid_transport_config')
                 for value in ('-1', 'nan', 'inf')]
        cases += [(['--adjust', 'qfq'], 'unsupported_adjust'),
                  (['--start-day', '2020-01-01'], 'unsupported_range')]
        for args, expected in cases:
            with self.subTest(args=args), patch('sys.stdout', new_callable=io.StringIO) as output:
                code = main(['company', '--symbol', '000001.SZ', *args])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output.getvalue())['code'], expected)
        self.assertEqual(self.calls, [])

    def test_cli_source_rejected_per_dataset(self):
        for kind, source in [('company', 'tdx'), ('announcements', 'szse'), ('quotes', 'cninfo'), ('bars', 'eastmoney')]:
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                code = main([kind, '--symbol', '000001.SZ', '--source', source])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())['code'], 'unsupported_source')


    def test_remote_range_requires_days_before_network(self):
        self.error('trading_days_required', lambda: self.client.bars('600000.SH', '2026-11-06', '2026-11-09'))
        self.assertEqual(self.calls, [])

    def test_explicit_days_skip_weekend(self):
        days = ['2026-11-06', '2026-11-09']
        def opener(request, timeout):
            self.calls.append(request.full_url)
            day = request.full_url.rsplit('/', 1)[1][:8]
            return Response(build_package(day, {'sh': [('600000', 'fixture', 10.17, 10.20)], 'sz': [('000001', 'fixture', 12.34, 12.30)], 'bj': [('430047', 'fixture', 8.5, 8.4)]}))
        self.http.opener = opener
        with patch.dict(ingest_market.MIN_PRICED, {'sh': 1, 'sz': 0, 'bj': 0}):
            result = self.client.bars('600000.SH', days[0], days[1], days=days)
        self.assertEqual([r['day'] for r in result['rows']], days)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[0].endswith('20261106.zip'))
        self.assertTrue(self.calls[1].endswith('20261109.zip'))

    def test_invalid_days_rejected_without_network(self):
        for days in ([], ['2026-11-09', '2026-11-06'], ['2026-11-06']*2, ['2026-11-05'], ['2026-11-10'], '2026-11-06'):
            self.error('invalid_trading_days', lambda: self.client.bars('600000.SH', '2026-11-06', '2026-11-09', days=days))
        self.assertEqual(self.calls, [])

    def test_quotes_expire_in_30_seconds_preserving_daily_ttl(self):
        first = self.client.quotes(['600000.SH'])
        self.now += timedelta(seconds=30)
        self.assertTrue(self.client.quotes(['600000.SH'])['snapshots'][0]['cache_hit'])
        self.now += timedelta(seconds=1)
        result = self.client.quotes(['600000.SH'])
        self.assertFalse(result['snapshots'][0]['cache_hit'])
        self.assertEqual(self.http.ttl, 86400)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result['rows'][0]['quote_time'], first['rows'][0]['observed_at'])
        self.assertEqual(result['rows'][0]['age_seconds'] - first['rows'][0]['age_seconds'], 31)

    def test_quotes_shorter_ttl_and_offline_expiry(self):
        self.http.ttl = 5
        self.client.quotes(['600000.SH'])
        self.http.offline = True
        self.now += timedelta(seconds=6)
        self.error('cache_expired', lambda: self.client.quotes(['600000.SH']))
        self.assertEqual(len(self.calls), 1)

    def test_local_mixed_adjustments_do_not_reject_or_mix(self):
        db = self.make_database()
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("INSERT INTO bar VALUES('600000.SH','2026-11-11',999,1,'qfq','other')")
            connection.commit()
        rows = self.client.bars('600000.SH', '2026-11-11', '2026-11-11', source='local')['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['close_cents'], rows[0]['adjust']), (1017, 'none'))

    def test_local_conflicting_sources_still_rejected(self):
        db = self.make_database()
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("INSERT INTO bar VALUES('600000.SH','2026-11-11',999,1,'none','other')")
            connection.commit()
        self.error('duplicate_bar', lambda: self.client.bars('600000.SH', '2026-11-11', '2026-11-11', source='local'))

    def test_local_tradable_bool_and_invalid_values(self):
        db = self.make_database()
        with closing(sqlite3.connect(db)) as connection:
            connection.execute('ALTER TABLE bar ADD COLUMN tradable INTEGER')
            for value in (0, 1, 2, -1, None, 'bad'):
                connection.execute('UPDATE bar SET tradable=?', (value,))
                connection.commit()
                query = lambda: self.client.bars('600000.SH', '2026-11-11', '2026-11-11', source='local')
                if value in (0, 1):
                    row = query()['rows'][0]
                    self.assertIs(row['tradable'], bool(value))
                    self.assertEqual(row['close_cents'], 1017)
                    self.assertIsNone(row['fetched_at'])
                else:
                    self.error('local_bar_invalid', query)


    def test_eastmoney_default_sh_bj_and_explicit_sz(self):
        for symbol in ('600000.SH', '920001.BJ', '000001.SZ'):
            body = dict(success=True, code=0, result=dict(count=1, pages=1, data=[
                dict(SECUCODE=symbol, SECURITY_CODE=symbol[:6], ORG_NAME='fixture')]))
            self.fixture_transport(body)
            result = self.client.company(symbol, source='eastmoney' if symbol.endswith('.SZ') else None)
            self.assertEqual(result['source'], 'eastmoney')
            self.assertEqual(result['data']['rows'][0]['source'], 'eastmoney')
            self.assertIn('eastmoney.com', self.calls[-1])
        self.assertEqual(len(self.calls), 3)

    def test_eastmoney_failure_never_switches_to_szse(self):
        self.fixture_transport(dict(success=False, code=500))
        self.error('UpstreamBusinessError', lambda: self.client.company('000001.SZ', source='eastmoney'))
        self.assertEqual(len(self.calls), 1)
        self.assertIn('eastmoney.com', self.calls[0])
        self.assertEqual(list((self.root/'cache').glob('*.json')), [])

    def test_unknown_market_explicitly_unsupported(self):
        self.error('unsupported_market', lambda: self.client.company('000001.HK'))
        self.assertEqual(self.calls, [])

    def test_quote_expiry_does_not_expire_daily_package(self):
        self.body = self.package()
        with patch.dict(ingest_market.MIN_PRICED, {'sh': 1, 'sz': 1, 'bj': 1}):
            self.client.bars('600000.SH', '2026-11-11', '2026-11-11')
            self.body = ('v_sh600000="' + payload() + '";').encode('gbk')
            self.client.quotes(['600000.SH'])
            self.now += timedelta(seconds=31)
            self.http.offline = True
            self.error('cache_expired', lambda: self.client.quotes(['600000.SH']))
            result = self.client.bars('600000.SH', '2026-11-11', '2026-11-11')
        self.assertTrue(result['rows'][0]['cache_hit'])
        self.assertEqual(len(self.calls), 2)

    def test_cli_days_reach_bars_without_calendar_guess(self):
        with patch.object(PublicDataClient, 'bars', return_value={'status': 'ok'}) as bars, patch('sys.stdout', new_callable=io.StringIO):
            code = main(['bars', '--symbol', '600000.SH', '--start-day', '2026-11-06', '--end-day', '2026-11-09', '--days', '2026-11-06,2026-11-09'])
        self.assertEqual(code, 0)
        self.assertEqual(bars.call_args.kwargs['days'], ['2026-11-06', '2026-11-09'])
        self.assertEqual(bars.call_args.kwargs['source'], 'tdx')

    def test_latest_snapshot_cannot_reconstruct_overwritten_history(self):
        self.client.company_provider = lambda symbol, **kw: {'symbol': symbol}
        self.client.provider_source = 'fixture'
        self.client.company('600000.SH')
        old = self.now.isoformat()
        self.now += timedelta(days=2)
        self.client.company('600000.SH')
        self.error('snapshot_after_as_of', lambda: self.client.company('600000.SH', as_of=old))
        self.assertEqual(len(list((self.root/'cache').glob('*.json'))), 1)


if __name__ == '__main__':
    unittest.main()
