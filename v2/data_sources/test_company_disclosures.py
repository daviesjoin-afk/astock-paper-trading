"""Offline behavioral fixtures; no external network or filesystem writes."""
import copy
import json
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from company_disclosures import (
    CNINFO_QUERY, CNINFO_SEARCH, ReferenceDataError, announcements, company_profile,
    company_profile_eastmoney,
)

OBSERVED = '2026-11-12T12:00:00+00:00'
MILLIS = 1792771200000  # Shanghai 2026-10-24 midnight, UTC previous day
IDENTITY = dict(code='000001', category='A股', type='shj', orgId='fixture-org', delisted='false')
PROFILE = dict(code='0', data=dict(agdm='000001', gsqc='平安银行股份有限公司',
    agjc='平安银行', agssrq='1991-04-03', sshymc='金融业', zcdz='深圳',
    http='bank.pingan.com', agzgb='1,940,592', agltgb='1,940,568'))


def item(key='101', **updates):
    result = dict(announcementId=key, secCode='000001', orgId='fixture-org',
                  announcementTitle='公告 ' + key, announcementTime=MILLIS,
                  adjunctUrl=f'finalpage/2026-10-24/{key}.PDF', announcementType=None)
    result.update(updates)
    return result


def page(items=None, total=1, more=False, **updates):
    result = dict(announcements=[item()] if items is None else items,
                  totalAnnouncement=total, hasMore=more, totalpages=1)
    result.update(updates)
    return result


class FixtureFetch:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, method='GET', data=None, headers=None):
        self.calls.append(dict(url=url, method=method, data=data, headers=headers))
        if not self.responses:
            raise AssertionError('unexpected extra fetch')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, bytes) else json.dumps(response).encode('utf-8')


def query(fetch, **kwargs):
    args = dict(fetched_at=OBSERVED, fetch=fetch)
    args.update(kwargs)
    return announcements('000001.SZ', '2026-10-20', '2026-10-27', **args)


class CompanyDisclosuresTests(unittest.TestCase):
    def assert_error(self, code, callback):
        with self.assertRaises(ReferenceDataError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code, str(raised.exception))
        self.assertIsInstance(raised.exception.detail, str)

    def eastmoney_fixture(self):
        return dict(success=True, code=0, result=dict(count=1, pages=1, data=[dict(
            SECUCODE='600000.SH', SECURITY_CODE='600000', ORG_NAME='测试上市公司',
            SECURITY_NAME_ABBR='测试公司', LISTING_DATE='1999-11-10 00:00:00',
            FOUND_DATE='1992-10-19', MAX_DATE='2026-09-30 00:00:00')]))

    def test_eastmoney_explicit_source_and_query_identity(self):
        fetch = FixtureFetch(self.eastmoney_fixture())
        result = company_profile_eastmoney('600000.SH', fetched_at=OBSERVED, fetch=fetch)
        row = result['rows'][0]
        self.assertEqual(result['provider'], 'eastmoney')
        self.assertTrue(result['complete'])
        self.assertEqual((row['symbol'], row['source']), ('600000.SH', 'eastmoney'))
        self.assertEqual(row['company_name'], '测试上市公司')
        self.assertEqual(row['listed_on'], '1999-11-10')
        self.assertEqual(row['founded_on'], '1992-10-19')
        params = parse_qs(urlsplit(fetch.calls[0]['url']).query)
        self.assertEqual(params['filter'], ['(SECUCODE="600000.SH")'])
        self.assertEqual(params['pageSize'], ['1'])
        self.assertNotIn('ALL', params['columns'][0])
        self.assertEqual(fetch.calls[0]['headers']['Referer'], 'https://emweb.securities.eastmoney.com/')
        self.assertIsNone(row['website'])

    def test_eastmoney_business_dates_are_not_snapshot_availability(self):
        result = company_profile_eastmoney('600000.SH', fetched_at=OBSERVED,
                                          fetch=FixtureFetch(self.eastmoney_fixture()))
        row = result['rows'][0]
        self.assertFalse(result['historical_as_of_supported'])
        self.assertEqual(row['known_at'], OBSERVED)
        self.assertEqual(row['first_seen_at'], OBSERVED)
        for field in ('valid_from', 'published_at', 'available_at', 'industry_standard'):
            self.assertIsNone(row[field])
        self.assertNotIn('MAX_DATE', row)

    def test_eastmoney_identity_name_and_dates_cannot_be_guessed(self):
        for updates, code in ((dict(SECUCODE='000001.SZ'), 'SymbolMismatch'),
                              (dict(SECURITY_CODE='000001'), 'SymbolMismatch'),
                              (dict(ORG_NAME=None), 'SchemaChanged'),
                              (dict(LISTING_DATE='yesterday'), 'InvalidDate')):
            payload = self.eastmoney_fixture()
            payload['result']['data'][0].update(updates)
            self.assert_error(code, lambda: company_profile_eastmoney('600000.SH',
                              fetched_at=OBSERVED, fetch=FixtureFetch(payload)))

    def test_eastmoney_error_empty_and_partial_are_distinct(self):
        payloads = ((dict(success=False, code=451), 'UpstreamBusinessError'),
                    (dict(success=True, code=0, result=None), 'SchemaChanged'),
                    (dict(success=True, code=0, result=dict(count=0, pages=0, data=[])), 'NoData'),
                    (dict(success=True, code=0, result=dict(count=2, pages=2, data=[])), 'Incomplete'))
        for payload, code in payloads:
            self.assert_error(code, lambda: company_profile_eastmoney('600000.SH',
                              fetched_at=OBSERVED, fetch=FixtureFetch(payload)))

    def test_company_profile_snapshot_and_raw_units(self):
        fetch = FixtureFetch(PROFILE)
        result = company_profile('000001.SZ', fetched_at=OBSERVED, fetch=fetch)
        self.assertTrue(result['complete'])
        self.assertEqual((result['dataset'], result['provider']), ('company_profile', 'szse'))
        row = result['rows'][0]
        self.assertEqual(row['company_name'], '平安银行股份有限公司')
        self.assertEqual(row['total_share_capital_raw'], '1,940,592')
        self.assertIsNone(row['share_capital_unit'])
        self.assertIsNone(row['english_name'])
        self.assertEqual(row['website'], 'bank.pingan.com')
        self.assertEqual(parse_qs(urlsplit(fetch.calls[0]['url']).query)['secCode'], ['000001'])
        self.assertEqual(fetch.calls[0]['method'], 'GET')

    def test_company_cannot_claim_past_availability(self):
        row = company_profile('000001.SZ', fetched_at=OBSERVED, fetch=FixtureFetch(PROFILE))['rows'][0]
        self.assertEqual(row['known_at'], OBSERVED)
        self.assertEqual(row['first_seen_at'], OBSERVED)
        for field in ('valid_from', 'available_at', 'published_at'):
            self.assertIsNone(row[field])
        self.assertNotEqual(row['known_at'][:10], row['listed_on'])
        result = company_profile('000001.SZ', fetched_at=OBSERVED, fetch=FixtureFetch(PROFILE))
        self.assertFalse(result['historical_as_of_supported'])
        with self.assertRaises(TypeError):
            company_profile('000001.SZ', fetched_at=OBSERVED, fetch=FixtureFetch(), as_of='2020-01-01')

    def test_company_only_sz_supported(self):
        for symbol in ('600000.SH', '920001.BJ'):
            with self.subTest(symbol=symbol):
                fetch = FixtureFetch()
                self.assert_error('Unsupported', lambda: company_profile(symbol, fetched_at=OBSERVED, fetch=fetch))
                self.assertFalse(fetch.calls)

    def test_invalid_symbols_rejected_before_fetch(self):
        for symbol in ('000001', 'sz000001', '000001.SH', '600000.SZ', '399001.SZ',
                       '000001.sz', '000001.SZ ', '../000001.SZ', None):
            with self.subTest(symbol=symbol):
                fetch = FixtureFetch()
                self.assert_error('InvalidSymbol', lambda: company_profile(symbol, fetched_at=OBSERVED, fetch=fetch))
                self.assert_error('InvalidSymbol', lambda: announcements(symbol, '2026-10-20', '2026-10-27', fetched_at=OBSERVED, fetch=fetch))
                self.assertFalse(fetch.calls)

    def test_company_mismatch_or_business_failure_is_not_empty(self):
        for payload, code in ((dict(code='1', data={}), 'UpstreamBusinessError'),
                              (dict(code='0', data={}), 'SymbolMismatch'),
                              (dict(code='0', data=None), 'SchemaChanged'),
                              (dict(data=PROFILE['data']), 'SchemaChanged')):
            with self.subTest(payload=payload):
                self.assert_error(code, lambda: company_profile('000001.SZ', fetched_at=OBSERVED, fetch=FixtureFetch(payload)))

    def test_company_requires_name_and_valid_listing_date(self):
        for updates, error in (({'gsqc': None}, 'SchemaChanged'), ({'agssrq': 'yesterday'}, 'InvalidDate')):
            payload = copy.deepcopy(PROFILE)
            payload['data'].update(updates)
            self.assert_error(error, lambda: company_profile('000001.SZ', fetched_at=OBSERVED, fetch=FixtureFetch(payload)))

    def test_timezone_required_and_datetime_input_supported(self):
        for value in ('2026-11-12', '2026-11-12T12:00:00', None):
            self.assert_error('InvalidFetchedAt', lambda: query(FixtureFetch(), fetched_at=value))
        fetch = FixtureFetch(PROFILE)
        result = company_profile('000001.SZ', fetched_at=datetime(2026, 11, 12, 12, tzinfo=timezone.utc), fetch=fetch)
        self.assertEqual(result['fetched_at'], OBSERVED)

    def test_precise_mapping_and_exact_query_contract(self):
        candidates = [dict(IDENTITY, code='000002'), dict(IDENTITY, category='债券'),
                      dict(IDENTITY, type='sse'), IDENTITY]
        fetch = FixtureFetch(candidates, page())
        result = query(fetch)
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual([c['url'] for c in fetch.calls], [CNINFO_SEARCH, CNINFO_QUERY])
        self.assertTrue(all(c['method'] == 'POST' for c in fetch.calls))
        identity_params = parse_qs(fetch.calls[0]['data'].decode())
        self.assertEqual(identity_params['keyWord'], ['000001'])
        params = parse_qs(fetch.calls[1]['data'].decode(), keep_blank_values=True)
        self.assertEqual(params['stock'], ['000001,fixture-org'])
        self.assertEqual(params['column'], ['szse'])
        self.assertEqual(params['seDate'], ['2026-10-20~2026-10-27'])
        self.assertEqual(params['plate'], [''])
        self.assertEqual(result['source_urls'], [CNINFO_SEARCH, CNINFO_QUERY])

    def test_mapping_not_found_never_queries_announcements(self):
        for candidates in ([], [dict(IDENTITY, code='000002')], [dict(IDENTITY, type='sse')],
                           [dict(IDENTITY, category='债券')]):
            fetch = FixtureFetch(candidates)
            self.assert_error('SymbolMappingNotFound', lambda: query(fetch))
            self.assertEqual(len(fetch.calls), 1)

    def test_ambiguous_or_unsafe_mapping(self):
        self.assert_error('AmbiguousSymbolMapping', lambda: query(FixtureFetch([IDENTITY, dict(IDENTITY, orgId='other')])))
        for org in ('', None, 'org&stock=other'):
            self.assert_error('SchemaChanged', lambda: query(FixtureFetch([dict(IDENTITY, orgId=org)])))

    def test_other_markets_require_matching_identity(self):
        for symbol, market_type, column in (('600000.SH', 'sse', 'sse'), ('920001.BJ', 'bse', 'bse'),
                                             ('600000.SH', 'shj', 'sse'), ('920001.BJ', 'shj', 'bse')):
            code = symbol.split('.')[0]
            fetch = FixtureFetch([dict(IDENTITY, code=code, type=market_type)],
                                 page([item(secCode=code)]))
            result = announcements(symbol, '2026-10-20', '2026-10-27', fetched_at=OBSERVED, fetch=fetch)
            self.assertEqual(result['rows'][0]['symbol'], symbol)
            self.assertEqual(parse_qs(fetch.calls[1]['data'].decode())['column'], [column])

    def test_midnight_is_date_only_and_uses_shanghai_day(self):
        row = query(FixtureFetch([IDENTITY], page()))['rows'][0]
        self.assertEqual(row['source_date'], '2026-10-24')
        self.assertEqual(row['source_timestamp_raw'], MILLIS)
        self.assertEqual(row['source_datetime'], '2026-10-24T00:00:00+08:00')
        self.assertEqual(row['timezone'], 'Asia/Shanghai')
        self.assertEqual(row['publication_precision'], 'date')
        self.assertIsNone(row['published_at'])
        self.assertIsNone(row['available_at'])
        self.assertEqual(row['first_seen_at'], OBSERVED)
        self.assertEqual(row['source_id'], '101')
        self.assertIn('announcementId=101', row['source_url'])
        self.assertEqual(row['attachment_url'], 'https://static.cninfo.com.cn/finalpage/2026-10-24/101.PDF')

    def test_nonmidnight_does_not_invent_verified_publication_time(self):
        row = query(FixtureFetch([IDENTITY], page([item(announcementTime=MILLIS + 3600000)])))['rows'][0]
        self.assertEqual(row['publication_precision'], 'date')
        self.assertIsNone(row['published_at'])
        self.assertIn('01:00:00', row['source_datetime'])

    def test_legal_zero_only(self):
        for items in (None, []):
            fetch = FixtureFetch([IDENTITY], dict(totalAnnouncement=0, announcements=items, hasMore=False))
            result = query(fetch)
            self.assertEqual(result['rows'], [])
            self.assertEqual(result['total_reported'], 0)
            self.assertTrue(result['complete'])
        for payload, error in ((dict(success=False), 'UpstreamBusinessError'),
                               (dict(resultcode=451), 'UpstreamBusinessError'),
                               (dict(code=500), 'UpstreamBusinessError'),
                               (dict(totalAnnouncement=0, hasMore=False), 'Incomplete'),
                               (dict(totalAnnouncement=0, hasMore=True, announcements=[]), 'Incomplete'),
                               (page(total=0), 'Incomplete'),
                               (dict(announcements=[]), 'SchemaChanged')):
            # Missing announcements is not a legitimate null field.
            self.assert_error(error, lambda: query(FixtureFetch([IDENTITY], payload)))

    def test_totalpages_is_not_used_for_termination(self):
        fetch = FixtureFetch([IDENTITY], page([item('101'), item('102')], total=3, more=True),
                             page([item('103')], total=3))
        result = query(fetch, page_size=2)
        self.assertEqual(len(result['rows']), 3)
        self.assertEqual(result['pages_fetched'], 2)
        self.assertEqual(parse_qs(fetch.calls[-1]['data'].decode())['pageNum'], ['2'])

    def test_overlap_deduplicates_by_id(self):
        fetch = FixtureFetch([IDENTITY], page([item('101'), item('102')], total=3, more=True),
                             page([item('102'), item('103')], total=3))
        result = query(fetch, page_size=2)
        self.assertEqual({r['source_id'] for r in result['rows']}, {'101', '102', '103'})
        self.assertEqual(len(result['rows']), result['total_reported'])

    def test_repeated_and_no_progress_pages_fail(self):
        for second in ([item('101'), item('102')], [item('101')], []):
            fetch = FixtureFetch([IDENTITY], page([item('101'), item('102')], total=4, more=True),
                                 page(second, total=4, more=True))
            self.assert_error('Incomplete', lambda: query(fetch, page_size=2))

    def test_conflicting_duplicate_is_not_silently_overwritten(self):
        fetch = FixtureFetch([IDENTITY], page([item('101')], total=3, more=True),
                             page([item('101', announcementTitle='modified'), item('102')], total=3, more=True))
        self.assert_error('Incomplete', lambda: query(fetch))

    def test_total_or_has_more_contradictions_fail(self):
        cases = [(page(total=2),), (page(total=1, more=True),),
                 (page([item('101'), item('102')], total=1),),
                 (page(total=3, more=True), page([item('102')], total=2))]
        for pages in cases:
            with self.subTest(pages=pages):
                self.assert_error('Incomplete', lambda: query(FixtureFetch([IDENTITY], *pages)))

    def test_interruption_and_page_limit_never_return_partial(self):
        fetch = FixtureFetch([IDENTITY], page(total=2, more=True), OSError('connection interrupted'))
        self.assert_error('Incomplete', lambda: query(fetch))
        fetch = FixtureFetch([IDENTITY], page(total=2, more=True))
        self.assert_error('Incomplete', lambda: query(fetch, max_pages=1))
        self.assertEqual(len(fetch.calls), 2)

    def test_transport_and_decode_failures_not_empty(self):
        for response, error in ((OSError('EOF'), 'TransportError'), (b'<html>captcha</html>', 'DecodeError'),
                                (b'\xff', 'DecodeError'), (b'null', 'SchemaChanged')):
            self.assert_error(error, lambda: query(FixtureFetch(response)))

    def test_date_range_and_pagination_arguments_validate_before_fetch(self):
        for kwargs in ({'page_size': 0}, {'page_size': True}, {'page_size': 101},
                       {'max_pages': 0}, {'max_pages': 1.5}):
            self.assert_error('InvalidPagination', lambda: query(FixtureFetch(), **kwargs))
        for start, end, error in (('2026-10-28', '2026-10-27', 'InvalidDateRange'),
                                  ('2026-02-30', '2026-10-27', 'InvalidDate'),
                                  ('2026-1-01', '2026-10-27', 'InvalidDate')):
            fetch = FixtureFetch()
            self.assert_error(error, lambda: announcements('000001.SZ', start, end, fetched_at=OBSERVED, fetch=fetch))
            self.assertFalse(fetch.calls)

    def test_rows_must_match_symbol_org_and_date_range(self):
        for updates, error in (({'secCode': '000002'}, 'SymbolMismatch'),
                               ({'orgId': 'other'}, 'SymbolMismatch'),
                               ({'announcementTime': MILLIS - 10 * 86400000}, 'DateRangeMismatch'),
                               ({'announcementTime': MILLIS + 10 * 86400000}, 'DateRangeMismatch')):
            self.assert_error(error, lambda: query(FixtureFetch([IDENTITY], page([item(**updates)]))))

    def test_schema_anomalies_fail(self):
        for payload in (page(total='1'), page(total=True), page(total=-1), page(more='false'),
                        page(items='not-list'), page(items=[None]),
                        page([item(announcementId=None)]), page([item(announcementTime='1792771200000')]),
                        page([item(announcementTime=True)]), page([item(announcementTime=10**30)]),
                        page([item(announcementTitle=None)])):
            self.assert_error('SchemaChanged', lambda: query(FixtureFetch([IDENTITY], payload)))
        self.assert_error('SchemaChanged', lambda: query(FixtureFetch([IDENTITY], page([item('1'), item('2')], total=2)), page_size=1))

    def test_attachment_safe_official_urls_and_missing_value(self):
        for path, expected in ((None, None), ('', None),
                               ('/finalpage/2026/101.PDF', 'https://static.cninfo.com.cn/finalpage/2026/101.PDF'),
                               ('https://static.cninfo.com.cn/finalpage/101.PDF', 'https://static.cninfo.com.cn/finalpage/101.PDF'),
                               ('https://www.cninfo.com.cn/101.PDF', 'https://www.cninfo.com.cn/101.PDF')):
            row = query(FixtureFetch([IDENTITY], page([item(adjunctUrl=path)])))['rows'][0]
            self.assertEqual(row['attachment_url'], expected)

    def test_dangerous_or_unofficial_attachment_urls_reject(self):
        # Hostile hosts are assembled from reserved example labels: the case
        # needs a foreign host, not a real-looking address in the source tree.
        foreign = 'evil.' + 'example'
        official = 'static.cninfo.com.cn'
        for path in ('javascript:alert(1)', 'file:///tmp/a.pdf', 'data:application/pdf,abc',
                     'http://' + official + '/a.pdf', 'https://' + foreign + '/a.pdf',
                     'https://' + official + '.' + foreign + '/a.pdf', '//' + foreign + '/a.pdf',
                     'https://' + 'user' + '@' + official + '/a.pdf',
                     'https://' + official + ':444/a.pdf',
                     '../a.pdf', 'finalpage/%2e%2e/a.pdf', 'finalpage/%252e%252e/a.pdf',
                     'finalpage\\a.pdf', 'finalpage/a\n.pdf', 'finalpage/%0a.pdf',
                     'https://static.cninfo.com.cn/a.pdf#script', 'https://[broken/a.pdf', 123):
            with self.subTest(path=path):
                self.assert_error('UnsafeSourceUrl', lambda: query(FixtureFetch([IDENTITY], page([item(adjunctUrl=path)]))))


if __name__ == '__main__':
    unittest.main()
