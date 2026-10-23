"""Isolated audit probes: temporary data directory, no real providers or ledger.

修复后重跑（B1/B2/B3/B5）：``ASTOCK_DATA_DIR`` 现在真的移动全部运行态，
因此本脚本**不再**手工重绑定 ``dfc.CACHE_DIR`` / ``KLINE_DIR`` / ``MANIFEST``
—— 那些重绑定本身就是当初 B1 的绕过手段。
"""
import asyncio
from contextlib import closing
import datetime as dt
import gc
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
temp = tempfile.TemporaryDirectory(prefix='source-audit-')
os.environ['ASTOCK_DATA_DIR'] = temp.name
import data_fetcher as dfc
path_probe = {'configured_directory': temp.name, 'effective_market_cache': dfc.CACHE_DIR,
              'effective_kline_dir': dfc.KLINE_DIR,
              'effective_manifest': dfc.KLINE_MANIFEST_PATH,
              'kline_dir_created_by_import': Path(dfc.KLINE_DIR).is_dir(),
              'isolated': Path(dfc.CACHE_DIR).resolve() == Path(temp.name).resolve()}
import market_data_contract as MDC
import market_data_service as MD
import selection_tracking as ST
assert Path(ST.DB_PATH).parent == Path(temp.name)
assert Path(dfc.KLINE_DIR).resolve().parent == Path(temp.name).resolve()


class _FrozenDateTime(dt.datetime):
    """把 ``ST.dt.datetime.now()`` 冻在探针时点。"""

    @classmethod
    def now(cls, tz=None):
        instant = _FROZEN_NOW
        return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)


def _freeze(instant):
    global _FROZEN_NOW
    _FROZEN_NOW = instant
    return SimpleNamespace(datetime=_FrozenDateTime, date=dt.date, time=dt.time,
                           timedelta=dt.timedelta, timezone=dt.timezone)

results = {'data_directory': path_probe}
TZ = dt.timezone(dt.timedelta(hours=8))
now = dt.datetime(2026, 10, 23, 15, 45, tzinfo=TZ)
rows = [{'code': str(600000+i), 'price': 10., 'quote_at': '2026-10-23T15:00:00+08:00'}
        for i in range(dfc.FULL_MARKET_MIN_ROWS)]
payload = {'complete': True, 'rows': rows, 'saved_at': now.isoformat(), 'expected_rows': len(rows)}
with patch.object(dfc, 'load_market_snapshot_full_payload', return_value=payload), \
     patch.object(dfc, 'fetch_market_snapshot_full', return_value=rows), \
     patch.object(MD, 'now_utc', return_value=now), \
     patch.object(ST, 'dt', _freeze(now)):
    tracking_rows = ST._close_snapshot_rows(now, now.date().isoformat())
    closing_rows = MD.refresh_rows(policy=MDC.CLOSE_SNAPSHOT_POLICY, asof_day=now.date())
    results['close_policy'] = {'provider_rows': len(rows), 'tracking_rows': len(tracking_rows),
                              'close_policy_rows': len(closing_rows), 'update': ST.update_observations()}

# Seed yesterday's run via the real persistence boundary.
yesterday = now.date() - dt.timedelta(days=1)
with patch.object(ST, '_today', return_value=yesterday), \
     patch.object(ST, '_latest_signal_date', return_value=yesterday.isoformat()), \
     patch.object(ST, '_benchmark_price_on_or_before', return_value=None), \
     patch.object(ST, '_registered_strategy_ids', return_value=frozenset()):
    ST.record_run({'strategy': 'three_day', 'picks': [{'code': '600000', 'price': 10.}]},
                  run_date=yesterday.isoformat(), asof_day=yesterday.isoformat())

# Intraday quotes must NOT be recorded as a closing observation.
midday = now.replace(hour=10, minute=30)
intraday_rows = [dict(row, quote_at=midday.isoformat(), price=11.) for row in rows]
intraday_payload = dict(payload, rows=intraday_rows, saved_at=midday.isoformat())
with patch.object(dfc, 'load_market_snapshot_full_payload', return_value=intraday_payload), \
     patch.object(dfc, 'fetch_market_snapshot_full', return_value=intraday_rows), \
     patch.object(MD, 'now_utc', return_value=midday), \
     patch.object(ST, 'dt', _freeze(midday)), \
     patch.object(ST, 'refresh_benchmark'), \
     patch.object(ST, '_benchmark_price_on_or_before', return_value=None):
    midday_result = ST.update_observations()
with patch.object(ST, '_close_snapshot_rows', return_value=rows), \
     patch.object(ST, 'dt', _freeze(now)), \
     patch.object(ST, 'refresh_benchmark'), \
     patch.object(ST, '_benchmark_price_on_or_before', return_value=None):
    close_result = ST.update_observations()
with closing(ST._connect()) as conn:
    stored = dict(conn.execute('SELECT price,return_pct FROM selection_observations').fetchone())
results['intraday_as_close'] = {'midday_result': midday_result, 'close_result': close_result, 'stored': stored}

# A read endpoint must not change the schema nor wait for a write lock.
with patch.object(dfc, 'get_kline_manifest', return_value={}):
    with closing(ST._connect()) as conn:
        before = conn.execute('PRAGMA schema_version').fetchone()[0]
    ST.dashboard()
    with closing(ST._connect()) as conn:
        after = conn.execute('PRAGMA schema_version').fetchone()[0]
    with closing(ST._connect()) as writer:
        writer.execute('BEGIN IMMEDIATE')
        with closing(ST._connect()) as reader:
            readable = reader.execute('SELECT COUNT(*) FROM selection_runs').fetchone()[0]
        original_connect = ST._connect_read_only
        def short_connect():
            connection = original_connect()
            connection.execute('PRAGMA busy_timeout=50')
            return connection
        start = time.perf_counter()
        try:
            with patch.object(ST, '_connect_read_only', side_effect=short_connect):
                ST.dashboard()
            error = None
        except sqlite3.OperationalError as exc:
            error = str(exc)
        writer.rollback()
    results['dashboard_schema'] = {'version_before': before, 'version_after': after,
        'read_during_writer_count': readable, 'dashboard_during_writer_error': error,
        'probe_busy_timeout_ms': 50, 'elapsed_ms': round((time.perf_counter()-start)*1000)}

# 旧预览路由：修复后是同步路由，由 FastAPI 的线程池执行，事件循环不再等待数据库。
import inspect
import api_paper as API
import paper_trading as P
lock_db = str(Path(temp.name) / 'loop-lock.sqlite3')
with closing(sqlite3.connect(lock_db)) as writer:
    writer.execute('CREATE TABLE t(x)')
    writer.commit()
    writer.execute('BEGIN EXCLUSIVE')
    def locked_preview(draft):
        with closing(sqlite3.connect(lock_db, timeout=.25)) as reader:
            reader.execute('SELECT * FROM t').fetchall()
        return {}
    async def observe_loop():
        start = time.perf_counter()
        async def heartbeat():
            await asyncio.sleep(.02)
            return round((time.perf_counter()-start)*1000)
        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        try:
            # 与 FastAPI 对同步路由的调度一致：放进线程池，而不是在循环里直接调用。
            await asyncio.to_thread(API.strategy_preview, {})
        except Exception:
            pass
        return await beat
    # Limit retry count only to keep this isolated reproducer short.
    retry = API._call_with_retry
    with patch.object(P, 'strategy_creation_preview', side_effect=locked_preview), \
         patch.object(API, '_call_with_retry', side_effect=lambda fn,*a: retry(fn,*a,retries=1)):
        results['async_blocking'] = {
            'heartbeat_expected_ms': 20, 'heartbeat_actual_ms': asyncio.run(observe_loop()),
            'sqlite_wait_seconds': .25,
            'route_is_coroutine_function': inspect.iscoroutinefunction(API.strategy_preview)}
    writer.rollback()

# Active settings route: use the real paper-storage transaction acquisition.
import api_settings as SETTINGS
import paper_storage as STORAGE
class Request:
    async def json(self):
        return {'risk': {'shared_pool_exposure_cap': .8}}
native_connect = sqlite3.connect
class ShortWaitConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if sql.startswith('PRAGMA busy_timeout='):
            sql = 'PRAGMA busy_timeout=250'
        return super().execute(sql, *args, **kwargs)
def short_sqlite_connect(*args, **kwargs):
    kwargs.update(timeout=.25, factory=ShortWaitConnection)
    return native_connect(*args, **kwargs)
async def settings_heartbeat():
    start = time.perf_counter()
    async def heartbeat():
        await asyncio.sleep(.02)
        return round((time.perf_counter()-start)*1000)
    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    try:
        await SETTINGS.update_settings(Request(), confirmed=True)
    except Exception:
        pass
    return await beat
with closing(native_connect(lock_db)) as writer:
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('BEGIN IMMEDIATE')
    with patch.object(P, 'DB_PATH', lock_db), patch.object(P, 'init_db'), \
         patch.object(SETTINGS, '_get_adaptive', return_value=object()), \
         patch.object(STORAGE.sqlite3, 'connect', side_effect=short_sqlite_connect):
        delay = asyncio.run(settings_heartbeat())
    writer.rollback()
results['settings_async_blocking'] = {'heartbeat_expected_ms': 20,
    'heartbeat_actual_ms': delay, 'probe_busy_timeout_ms': 250,
    'note': 'Real settings handler and storage BEGIN IMMEDIATE; init_db bypassed; shortened SQLite timeout.'}

print(json.dumps(results, ensure_ascii=False, indent=2))
gc.collect()
try:
    temp.cleanup()
except PermissionError:
    # Windows 下探针自己的 SQLite 句柄可能还没释放；结果已打印，不影响结论。
    pass
