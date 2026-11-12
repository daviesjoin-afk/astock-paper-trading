"""Official reference-data adapters; stdlib only, no storage or network ownership.

Consumers inject fetch(url, method='GET', data=None, headers=None) -> bytes.
Errors never return partial rows. Source timestamps do not establish historical
availability. first_seen_at is this call's observation; a durable consumer must
preserve its earliest observation on subsequent upserts.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin, urlsplit, unquote

SZSE_PROFILE = 'https://www.szse.cn/api/report/index/companyGeneralization'
CNINFO_SEARCH = 'https://www.cninfo.com.cn/new/information/topSearch/query'
CNINFO_QUERY = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
EASTMONEY_PROFILE = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')


class ReferenceDataError(Exception):
    def __init__(self, code: str, detail: str = '') -> None:
        self.code, self.detail = code, detail
        super().__init__(f'{code}: {detail}' if detail else code)


def _fail(code, detail):
    raise ReferenceDataError(code, detail)


def _symbol(symbol):
    if not isinstance(symbol, str) or not re.fullmatch(r'[0-9]{6}\.(SZ|SH|BJ)', symbol):
        _fail('InvalidSymbol', 'expected six-digit A-share code with .SZ/.SH/.BJ')
    code, market = symbol.split('.')
    prefixes = {'SZ': ('000', '001', '002', '003', '300', '301'),
                'SH': ('600', '601', '603', '605', '688', '689'),
                'BJ': ('43', '83', '87', '88', '92')}
    if not code.startswith(prefixes[market]):
        _fail('InvalidSymbol', f'not an A-share code for {market}: {code}')
    return code, market


def _observation(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('timezone required')
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReferenceDataError('InvalidFetchedAt', 'timezone-aware ISO datetime required') from exc


def _day(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('format')
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ReferenceDataError('InvalidDate', str(value)) from exc


def _request(fetch, url, *, data=None):
    referer = ('https://www.szse.cn/' if url.startswith(SZSE_PROFILE) else
               'https://emweb.securities.eastmoney.com/' if url.startswith(EASTMONEY_PROFILE) else
               'https://www.cninfo.com.cn/')
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': referer}
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    try:
        raw = fetch(url, method='POST' if data is not None else 'GET',
                    data=urlencode(data).encode('utf-8') if data is not None else None,
                    headers=headers)
    except Exception as exc:
        raise ReferenceDataError('TransportError', f'{url}: {type(exc).__name__}: {exc}') from exc
    try:
        if not isinstance(raw, bytes):
            raise ValueError('fetch must return bytes')
        payload = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise ReferenceDataError('DecodeError', url) from exc
    if isinstance(payload, dict):
        for field in ('code', 'resultcode'):
            if field in payload and str(payload[field]) != '0':
                _fail('UpstreamBusinessError', f'{url}: {field}={payload[field]}')
        if 'success' in payload and payload['success'] not in (True, 'true'):
            _fail('UpstreamBusinessError', f'{url}: success={payload["success"]}')
    return payload


def _text(value, field, *, required=False):
    if value is None or value == '':
        if required:
            _fail('SchemaChanged', f'missing {field}')
        return None
    if not isinstance(value, str):
        _fail('SchemaChanged', f'{field} must be text')
    return value


def _result(dataset, provider, rows, urls, observed, **metadata):
    return dict(dataset=dataset, provider=provider, rows=rows,
                source_urls=list(dict.fromkeys(urls)), complete=True,
                fetched_at=observed, **metadata)


def company_profile(symbol, *, fetched_at, fetch) -> dict:
    """Latest SZSE snapshot only (SZ); SH/BJ raise Unsupported.

    No as-of parameter or historical lookup is supported. known_at is fetched_at;
    listed_on is a business date, never the snapshot's historical effective date.
    """
    code, market = _symbol(symbol)
    observed = _observation(fetched_at)
    if market != 'SZ':
        _fail('Unsupported', f'SZSE company profile does not support {market}')
    url = SZSE_PROFILE + '?' + urlencode({'random': '0.123', 'secCode': code})
    payload = _request(fetch, url)
    if not isinstance(payload, dict) or str(payload.get('code')) != '0' or not isinstance(payload.get('data'), dict):
        _fail('SchemaChanged', 'SZSE success code and company object required')
    data = payload['data']
    if data.get('agdm') != code:
        _fail('SymbolMismatch', 'SZSE agdm differs from requested code')
    listed_on = _text(data.get('agssrq'), 'agssrq')
    if listed_on is not None:
        _day(listed_on)
    row = dict(symbol=symbol, company_name=_text(data.get('gsqc'), 'gsqc', required=True),
               short_name=_text(data.get('agjc') or data.get('agdjc'), 'agjc'),
               english_name=_text(data.get('ywqc'), 'ywqc'), listed_on=listed_on,
               industry_name=_text(data.get('sshymc'), 'sshymc'),
               industry_standard=None, industry_source='szse',
               registered_address=_text(data.get('zcdz'), 'zcdz'),
               website=_text(data.get('http'), 'http'),
               total_share_capital_raw=data.get('agzgb'),
               circulating_share_capital_raw=data.get('agltgb'), share_capital_unit=None,
               source='szse', source_url=url, fetched_at=observed,
               first_seen_at=observed, known_at=observed, valid_from=None,
               available_at=None, published_at=None, publication_precision='unknown')
    return _result('company_profile', 'szse', [row], [url], observed,
                   historical_as_of_supported=False, snapshot=True)


def company_profile_eastmoney(symbol, *, fetched_at, fetch) -> dict:
    """Explicit non-official latest snapshot; never a fallback inside SZSE."""
    code, _market = _symbol(symbol)
    observed = _observation(fetched_at)
    columns = ('SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,ORG_NAME,ORG_NAME_EN,'
               'ORG_CODE,FOUND_DATE,LISTING_DATE,ORG_WEB,REG_ADDRESS,MAIN_BUSINESS,'
               'BUSINESS_SCOPE,CSRC_INDUSTRY_NAME,TRADE_MARKET')
    url = EASTMONEY_PROFILE + '?' + urlencode(dict(
        reportName='RPT_F10_ORG_BASICINFO', columns=columns,
        filter=f'(SECUCODE="{symbol}")', pageNumber=1, pageSize=1,
        source='HSF10', client='PC'))
    payload = _request(fetch, url)
    if (not isinstance(payload, dict) or payload.get('success') is not True
            or str(payload.get('code')) != '0'):
        _fail('SchemaChanged', 'Eastmoney success and code=0 required')
    result = payload.get('result')
    if not isinstance(result, dict):
        _fail('SchemaChanged', 'Eastmoney result object required')
    count, pages, rows = result.get('count'), result.get('pages'), result.get('data')
    if (type(count) is not int or count < 0 or type(pages) is not int or pages < 0
            or not isinstance(rows, list)):
        _fail('SchemaChanged', 'Eastmoney count/pages/data required')
    if count == 0 and pages == 0 and not rows:
        _fail('NoData', symbol)
    if count != 1 or pages != 1 or len(rows) != 1 or not isinstance(rows[0], dict):
        _fail('Incomplete', 'company query must contain exactly one complete record')
    data = rows[0]
    if data.get('SECUCODE') != symbol or data.get('SECURITY_CODE') != code:
        _fail('SymbolMismatch', 'Eastmoney identity differs from requested symbol')

    def business_day(field):
        value = _text(data.get(field), field)
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value).date().isoformat()
        except ValueError as exc:
            raise ReferenceDataError('InvalidDate', field) from exc

    row = dict(symbol=symbol, company_name=_text(data.get('ORG_NAME'), 'ORG_NAME', required=True),
               short_name=_text(data.get('SECURITY_NAME_ABBR'), 'SECURITY_NAME_ABBR'),
               english_name=_text(data.get('ORG_NAME_EN'), 'ORG_NAME_EN'),
               company_source_id=_text(data.get('ORG_CODE'), 'ORG_CODE'),
               listed_on=business_day('LISTING_DATE'), founded_on=business_day('FOUND_DATE'),
               industry_name=_text(data.get('CSRC_INDUSTRY_NAME'), 'CSRC_INDUSTRY_NAME'),
               industry_standard=None, industry_source='eastmoney',
               registered_address=_text(data.get('REG_ADDRESS'), 'REG_ADDRESS'),
               website=_text(data.get('ORG_WEB'), 'ORG_WEB'),
               main_business=_text(data.get('MAIN_BUSINESS'), 'MAIN_BUSINESS'),
               business_scope=_text(data.get('BUSINESS_SCOPE'), 'BUSINESS_SCOPE'),
               source='eastmoney', source_url=url, fetched_at=observed,
               first_seen_at=observed, known_at=observed, valid_from=None,
               available_at=None, published_at=None, publication_precision='unknown')
    return _result('company_profile', 'eastmoney', [row], [url], observed,
                   historical_as_of_supported=False, snapshot=True)


def _org_id(code, market, fetch):
    payload = _request(fetch, CNINFO_SEARCH, data={'keyWord': code, 'maxNum': 10})
    if not isinstance(payload, list):
        _fail('SchemaChanged', 'CNINFO topSearch must return a list')
    # shj is CNINFO's shared stock-search group, not an exchange suffix.
    # Exchange identity also requires the exact validated A-share code prefix.
    types = {'SZ': {'shj'}, 'SH': {'shj', 'sse'}, 'BJ': {'shj', 'bj', 'bse'}}
    matches = []
    for candidate in payload:
        if not isinstance(candidate, dict):
            _fail('SchemaChanged', 'invalid identity candidate')
        if (candidate.get('code') == code and candidate.get('category') == 'A股'
                and isinstance(candidate.get('type'), str)
                and candidate['type'] in types[market]):
            org = candidate.get('orgId')
            if not isinstance(org, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', org):
                _fail('SchemaChanged', 'invalid orgId')
            matches.append(org)
    if not matches:
        _fail('SymbolMappingNotFound', f'{code}.{market}: no exact code/market/A-share match')
    if len(set(matches)) != 1:
        _fail('AmbiguousSymbolMapping', f'{code}.{market}: multiple orgIds')
    return matches[0]


def _attachment(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        _fail('UnsafeSourceUrl', 'attachment must be text')
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
    if (any(ord(c) < 33 or ord(c) == 127 for c in decoded)
            or '\\' in decoded or decoded.startswith('//')):
        _fail('UnsafeSourceUrl', 'unsafe attachment characters')
    try:
        parts = urlsplit(decoded)
        if '..' in parts.path.split('/'):
            _fail('UnsafeSourceUrl', 'attachment path traversal')
        url = urljoin('https://static.cninfo.com.cn/', value)
        parts = urlsplit(url)
    except ValueError as exc:
        raise ReferenceDataError('UnsafeSourceUrl', 'malformed attachment URL') from exc
    if (parts.scheme != 'https' or parts.netloc not in {'static.cninfo.com.cn', 'www.cninfo.com.cn'}
            or parts.username or parts.password or parts.fragment):
        _fail('UnsafeSourceUrl', 'only official HTTPS CNINFO attachment URLs permitted')
    return url


def _count(value, field):
    if type(value) is not int or value < 0:
        _fail('SchemaChanged', f'{field} must be a nonnegative integer')
    return value


def _announcement(item, symbol, org, start, end, observed):
    code = symbol.split('.')[0]
    if not isinstance(item, dict):
        _fail('SchemaChanged', 'announcement must be an object')
    if item.get('secCode') != code or item.get('orgId') != org:
        _fail('SymbolMismatch', 'announcement code/orgId differs from requested identity')
    source_id = item.get('announcementId')
    if not isinstance(source_id, str) or not re.fullmatch(r'[0-9]+', source_id):
        _fail('SchemaChanged', 'invalid announcementId')
    millis = item.get('announcementTime')
    if type(millis) is not int:
        _fail('SchemaChanged', 'announcementTime must be integer milliseconds')
    try:
        local = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=millis)).astimezone(SHANGHAI)
    except (OverflowError, ValueError) as exc:
        raise ReferenceDataError('SchemaChanged', 'invalid announcementTime') from exc
    if not start <= local.date() <= end:
        _fail('DateRangeMismatch', f'{source_id}: {local.date()} outside requested range')
    detail = 'https://www.cninfo.com.cn/new/disclosure/detail?' + urlencode(
        {'stockCode': code, 'announcementId': source_id, 'orgId': org})
    return dict(symbol=symbol, source_id=source_id, announcement_id=source_id,
                org_id=org, title=_text(item.get('announcementTitle'), 'announcementTitle', required=True),
                category_raw=item.get('announcementType'), source_date=local.date().isoformat(),
                announcement_date=local.date().isoformat(), source_timestamp_raw=millis,
                source_timestamp_unit='milliseconds', source_datetime=local.isoformat(),
                timezone='Asia/Shanghai', published_at=None, publication_precision='date',
                source='cninfo', source_url=detail, attachment_url=_attachment(item.get('adjunctUrl')),
                fetched_at=observed, first_seen_at=observed, known_at=observed,
                available_at=None)


def announcements(symbol, start_day, end_day, *, fetched_at, fetch,
                  page_size=30, max_pages=20) -> dict:
    """Fetch a complete single-stock date window or raise ReferenceDataError.

    totalpages is intentionally ignored. hasMore and the unique ID count must
    agree with totalAnnouncement; no partial success or implicit provider change.
    CNINFO time semantics are unverified: even non-midnight values are not
    promoted to exact publication times. No PDFs are fetched.
    """
    code, market = _symbol(symbol)
    observed = _observation(fetched_at)
    start, end = _day(start_day), _day(end_day)
    if start > end:
        _fail('InvalidDateRange', 'start_day exceeds end_day')
    if type(page_size) is not int or not 1 <= page_size <= 100:
        _fail('InvalidPagination', 'page_size must be 1..100')
    if type(max_pages) is not int or max_pages < 1:
        _fail('InvalidPagination', 'max_pages must be positive')
    org = _org_id(code, market, fetch)
    rows, total = {}, None
    page_fingerprints = set()
    for page in range(1, max_pages + 1):
        params = dict(pageNum=page, pageSize=page_size,
                      column={'SZ': 'szse', 'SH': 'sse', 'BJ': 'bse'}[market],
                      tabName='fulltext', plate='', stock=f'{code},{org}', searchkey='',
                      secid='', category='', trade='', seDate=f'{start_day}~{end_day}',
                      sortName='time', sortType='desc', isHLtitle='false')
        try:
            payload = _request(fetch, CNINFO_QUERY, data=params)
        except ReferenceDataError as exc:
            if page > 1:
                raise ReferenceDataError('Incomplete', f'page {page}: {exc.code}: {exc.detail}') from exc
            raise
        if not isinstance(payload, dict):
            _fail('SchemaChanged', f'page {page}: expected object')
        current_total = _count(payload.get('totalAnnouncement'), 'totalAnnouncement')
        if total is None:
            total = current_total
        elif current_total != total:
            _fail('Incomplete', 'totalAnnouncement changed during pagination')
        more = payload.get('hasMore')
        if type(more) is not bool:
            _fail('SchemaChanged', 'hasMore must be boolean')
        if 'announcements' not in payload:
            _fail('Incomplete', 'missing announcements field')
        items = payload['announcements']
        if items is None and total == 0 and not more:
            items = []
        if not isinstance(items, list) or len(items) > page_size:
            _fail('SchemaChanged', 'invalid announcements page')
        if total == 0:
            if items or more or page != 1:
                _fail('Incomplete', 'contradictory zero total')
            return _result('announcements', 'cninfo', [], [CNINFO_SEARCH, CNINFO_QUERY],
                           observed, total_reported=0, pages_fetched=page,
                           symbol=symbol, start_day=start_day, end_day=end_day)
        normalized = [_announcement(i, symbol, org, start, end, observed) for i in items]
        fingerprint = frozenset(r['source_id'] for r in normalized)
        if not fingerprint or fingerprint in page_fingerprints:
            _fail('Incomplete', f'empty or repeated page {page}')
        page_fingerprints.add(fingerprint)
        previous_size = len(rows)
        for row in normalized:
            key = row['source_id']
            if key in rows and rows[key] != row:
                _fail('Incomplete', f'conflicting duplicate ID {key}')
            rows[key] = row
        if len(rows) == previous_size or len(rows) > total:
            _fail('Incomplete', 'unique count inconsistent with totalAnnouncement')
        if not more:
            if len(rows) != total:
                _fail('Incomplete', f'expected {total} unique IDs, received {len(rows)}')
            ordered = sorted(rows.values(), key=lambda r: (r['source_timestamp_raw'], r['source_id']), reverse=True)
            return _result('announcements', 'cninfo', ordered, [CNINFO_SEARCH, CNINFO_QUERY],
                           observed, total_reported=total, pages_fetched=page,
                           symbol=symbol, start_day=start_day, end_day=end_day)
        if len(rows) >= total:
            _fail('Incomplete', 'hasMore contradicts accumulated unique count')
    _fail('Incomplete', f'max_pages={max_pages} exhausted before completion')
