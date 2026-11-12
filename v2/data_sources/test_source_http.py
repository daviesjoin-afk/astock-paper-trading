"""Behavioral HTTP/cache tests; all network and time are injected."""
import io
import json
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from source_http import SourceHttp, SourceHttpError, RequestIdentity, tdx_identity

NOW = datetime(2026, 11, 12, 10, tzinfo=timezone.utc)


class Response(io.BytesIO):
    status = 200


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.calls = []
        self.now = NOW
        self.body = b'valid'
        def opener(request, timeout):
            self.calls.append(request.full_url)
            return Response(self.body)
        self.http = SourceHttp(self.cache, ttl=60, opener=opener, clock=lambda: self.now)
        self.identity = RequestIdentity('test', 'https://example.test/one')

    def request(self, **kwargs):
        return self.http.request(self.identity, validate=lambda raw: self.assertEqual(raw, b'valid'), **kwargs)

    def error(self, code, call):
        with self.assertRaises(SourceHttpError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_cache_shared_between_transport_instances(self):
        first = self.request()
        other = SourceHttp(self.cache, offline=True, ttl=60, clock=lambda: NOW)
        second = other.request(self.identity, validate=lambda raw: self.assertEqual(raw, b'valid'))
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.fetched_at, second.fetched_at)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list(self.cache.glob('*.tmp')), [])

    def test_concurrent_consumers_download_once(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            snapshots = list(pool.map(lambda _: self.request(), range(12)))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(not s.cache_hit for s in snapshots), 1)

    def test_request_identity_does_not_alias(self):
        identities = [self.identity, replace(self.identity, source='other'),
                      replace(self.identity, symbol='000001.SZ'), replace(self.identity, adjust='qfq'),
                      replace(self.identity, date='2026-11-11'), replace(self.identity, method='POST'),
                      replace(self.identity, payload=b'a'), replace(self.identity, url='https://example.test/two')]
        for identity in identities:
            self.http.request(identity, validate=lambda _: True)
        self.assertEqual(len(self.calls), len(identities))
        self.assertEqual(len({i.key() for i in identities}), len(identities))

    def test_tdx_identity_is_entire_day(self):
        self.assertEqual(tdx_identity('2026-11-11').symbol, '*')
        self.assertNotEqual(tdx_identity('2026-11-11').key(), tdx_identity('2026-11-12').key())

    def test_ttl_online_refresh_offline_refuses(self):
        self.request()
        self.now += timedelta(seconds=61)
        self.http.offline = True
        self.error('cache_expired', self.request)
        self.assertEqual(len(self.calls), 1)
        self.http.offline = False
        self.assertFalse(self.request().cache_hit)
        self.assertEqual(len(self.calls), 2)

    def test_offline_miss_never_fetches(self):
        self.http.offline = True
        self.error('offline_cache_miss', self.request)
        self.assertEqual(self.calls, [])

    def test_corrupt_source_digest_time_and_identity_refuse(self):
        self.request()
        path = next(self.cache.glob('*.json'))
        original = json.loads(path.read_text())
        for field, value in [('source', 'other'), ('sha256', '0'*64), ('fetched_at', 'yesterday'),
                             ('identity', {}), ('response', 'invalid!')]:
            with self.subTest(field=field):
                doc = dict(original, **{field: value})
                path.write_text(json.dumps(doc))
                self.error('cache_corrupt', self.request)
        self.assertEqual(len(self.calls), 1)

    def test_future_cache_time_refuses(self):
        self.request()
        self.now -= timedelta(seconds=1)
        self.error('cache_future_timestamp', self.request)

    def test_failed_validation_never_cached(self):
        def reject(raw):
            raise ValueError('bad payload')
        with self.assertRaises(ValueError):
            self.http.request(self.identity, validate=reject)
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertFalse(self.request().cache_hit)

    def test_empty_and_unvalidated_responses_never_cached(self):
        self.http.request(self.identity)
        self.assertEqual(list(self.cache.iterdir()), [])
        self.body = b''
        self.error('upstream_empty', self.request)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_historical_requires_frozen_snapshot(self):
        self.error('historical_snapshot_unavailable', lambda: self.request(as_of=NOW.isoformat()))
        self.assertEqual(self.calls, [])
        self.request()
        self.error('snapshot_after_as_of', lambda: self.request(as_of=(NOW-timedelta(seconds=1)).isoformat()))
        self.now += timedelta(days=5)
        self.assertTrue(self.request(as_of=(NOW+timedelta(seconds=30)).isoformat()).cache_hit)
        self.error('cache_expired', lambda: self.request(as_of=(NOW+timedelta(seconds=61)).isoformat()))
        self.assertEqual(len(self.calls), 1)

    def test_cache_hit_revalidates_for_consumer(self):
        self.request()
        def reject(raw):
            raise ValueError('not accepted by second consumer')
        with self.assertRaises(ValueError):
            self.http.request(self.identity, validate=reject)
        self.assertEqual(len(self.calls), 1)

    def test_no_cache_by_default(self):
        self.http.cache_dir = None
        self.request()
        self.request()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_auth_and_permanent_http_errors_stop(self):
        for status in (400, 401, 403, 404):
            calls = []
            def opener(request, timeout):
                calls.append(status)
                raise urllib.error.HTTPError(request.full_url, status, '', {}, None)
            http = SourceHttp(self.cache, retries=3, opener=opener, sleep=lambda _: self.fail('must not sleep'))
            with self.assertRaises(urllib.error.HTTPError):
                http.request(self.identity, validate=lambda _: True)
            self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_retry_after_seconds_and_http_date(self):
        for header, expected in [('7', 7), ('Thu, 12 Nov 2026 10:00:09 GMT', 9)]:
            calls, waits = [], []
            def opener(request, timeout):
                calls.append(1)
                if len(calls) == 1:
                    raise urllib.error.HTTPError(request.full_url, 429, '', {'Retry-After': header}, None)
                return Response(b'valid')
            http = SourceHttp(retries=2, opener=opener, clock=lambda: NOW, sleep=waits.append)
            self.assertEqual(http.request(self.identity, validate=lambda _: True).raw, b'valid')
            self.assertEqual(waits, [expected])
            self.assertEqual(len(calls), 2)

    def test_long_retry_after_stops_without_early_retry(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, '', {'Retry-After': '120'}, None)
        http = SourceHttp(retries=2, opener=opener, sleep=lambda _: self.fail('must not sleep'))
        self.error('retry_after_exceeds_bound', lambda: http.request(self.identity))

    def test_network_retry_is_bounded_and_failure_not_cached(self):
        calls, waits = [], []
        def opener(request, timeout):
            calls.append(1)
            raise urllib.error.URLError('down')
        http = SourceHttp(self.cache, retries=2, opener=opener, sleep=waits.append)
        with self.assertRaises(urllib.error.URLError):
            http.request(self.identity, validate=lambda _: True)
        self.assertEqual(len(calls), 3)
        self.assertEqual(waits, [1, 2])
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_invalid_config_rejected(self):
        for opts in ({'retries': 4}, {'retries': -1}, {'timeout': 0}, {'ttl': float('nan')}, {'ttl': -1}):
            self.error('invalid_transport_config', lambda: SourceHttp(**opts))


if __name__ == '__main__':
    unittest.main()
