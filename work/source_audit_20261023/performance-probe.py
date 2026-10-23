"""Small synthetic, local-only benchmark; numbers are not production estimates.

修复后重跑（E1）：第二次扫描换了过滤条件（``pe_max``），历史部分必须被复用。
"""
import datetime as dt
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
temp = tempfile.TemporaryDirectory(prefix='source-audit-perf-')
os.environ['ASTOCK_DATA_DIR'] = temp.name
import numpy as np
import pandas as pd
import data_fetcher as dfc
Path(dfc.KLINE_DIR).mkdir(parents=True, exist_ok=True)
import main as M
import selection_service as SEL
import linkage as L
import market_data_contract as MDC
import market_data_service as MD

codes = [str(600000+i) for i in range(250)]
index = pd.bdate_range('2025-01-01', periods=260)
values = 10 + np.arange(len(index)) / 100 + np.sin(np.arange(len(index))/3)/10
frame = pd.DataFrame({'open': values, 'close': values, 'high': values+.1,
    'low': values-.1, 'volume': 100000., 'amount': 1000000.}, index=index)
for code in codes:
    frame.to_csv(Path(dfc.KLINE_DIR) / f'{code}.csv')
results = {}
loaded = []
real_compute = M.F.compute_price_factors
def compute(klines):
    result = real_compute(klines)
    loaded.append(result)
    return result
with patch.object(M.U, 'data_ready', return_value=True), \
     patch.object(M.U, 'load_universe', return_value=[{'code': code} for code in codes]), \
     patch.object(SEL, '_complete_daily_cutoff', return_value=index[-1].date()), \
     patch.object(dfc, 'fetch_realtime_for_codes', return_value={'rows': []}), \
     patch.object(dfc, 'load_shared_kline', wraps=dfc.load_shared_kline) as reads, \
     patch.object(M.F, 'compute_price_factors', side_effect=compute) as factors:
    elapsed = []
    for kwargs in ({}, {'pe_max': 30.0}, {'pb_max': 5.0}):
        start = time.perf_counter()
        result = M.scanner(**kwargs)
        elapsed.append(round((time.perf_counter()-start)*1000))
    results['scanner_repeat'] = {'symbols': len(codes), 'bars_per_symbol': len(index),
        'requests': 3, 'filters': ['none', 'pe_max', 'pb_max'],
        'csv_load_calls': reads.call_count, 'factor_compute_calls': factors.call_count,
        'request_ms': elapsed, 'factor_rows': len(loaded[0]) if loaded else 0,
        'identical_factor_frames': len(loaded) == 1,
        'note': 'Real CSV loading and factor calculation; provider mocked empty; exits at coverage gate.'}

# The cached projection is stale, but linkage labels it as today's resonance.
tz = dt.timezone(dt.timedelta(hours=8))
now = dt.datetime(2026,10,23,10,30,tzinfo=tz)
previous = '2026-10-22T15:00:00+08:00'
uni = [{'code': code, 'industry': 'A' if i<3 else 'B'} for i,code in enumerate(codes[:6])]
rows = [{'code': row['code'], 'industry': row['industry'], 'price': 10., 'pct': 2.,
         'amount': 1000000., 'main_pct': 10., 'quote_at': previous} for row in uni]
snapshot = MD._full_market_snapshot(rows=rows, observed_at=previous, saved_at=previous,
    complete=True, expected_rows=len(rows), kind=MD.KIND_FULL_MARKET_SNAPSHOT)
reading = MDC.classify(snapshot, MDC.LIVE_MARKET_POLICY, now=now)
with patch.object(L.U, 'load_universe', return_value=uni), \
     patch.object(L.dfc, 'load_cached_kline', return_value=frame), \
     patch.object(MD, 'read_snapshot', return_value=reading), \
     patch.dict(L._CACHE, data=None, ts=0., key=None):
    result = L.sector_linkage()
    results['stale_linkage'] = {'reading_freshness': reading.freshness,
        'observed_at': previous, 'today': now.date().isoformat(), 'pairs': result['pairs'],
        'resonating_count': result['resonating_count']}
print(json.dumps(results, ensure_ascii=False, indent=2))
gc.collect()
temp.cleanup()
