"""选股模块独立性、HTTP 衔接与跨刷新代次的缓存回归。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

import main as M
import selection_service as SEL
from fastapi.testclient import TestClient


class SelectionServiceTests(unittest.TestCase):
    def test_jobs_execute_without_web_app_or_import_time_network_and_threads(self):
        script = textwrap.dedent('''
            import datetime as dt
            import importlib.abc
            import socket
            import sys
            import threading
            from unittest import mock

            class NoWeb(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.')[0] in {'main', 'fastapi'}:
                        raise AssertionError('job imported web module: ' + fullname)
            sys.meta_path.insert(0, NoWeb())
            def forbidden(*args, **kwargs):
                raise AssertionError('unexpected import-time work')
            with mock.patch.object(socket.socket, 'connect', forbidden), \
                    mock.patch.object(socket, 'create_connection', forbidden), \
                    mock.patch.object(threading.Thread, 'start', forbidden):
                import selection_service as SEL
                import paper_selection as PS
                import selection_runner as R
                import paper_selection_runner as PR
            with mock.patch.object(SEL.U, 'coverage_report', return_value={
                    'fresh_selection': 0, 'fresh_selection_pct': 0.0}):
                for model_id in PS.STRATEGY_MODEL.values():
                    assert PS._run_one(model_id, 5)['need_init']
            payload = {'picks': [{'code': '600000', 'score': 1.0}]}
            with mock.patch.object(SEL, '_select_uncached', return_value=payload) as run:
                assert PS._run_one('one_to_two', 3) is payload
                run.assert_called_once_with(strategy='one_to_two', topn=3)
                with mock.patch.object(R.ST, 'ensure_schema'), \
                        mock.patch.object(R.ST, 'update_observations', return_value={}), \
                        mock.patch.object(SEL, '_complete_daily_cutoff', return_value=dt.date(2026, 10, 13)), \
                        mock.patch.object(R.U, 'refresh_history', return_value={'status': 'up_to_date'}), \
                        mock.patch.object(R.P, '_rebuild_selection_factor_cache', return_value={}), \
                        mock.patch.object(R.ST, 'record_run', side_effect=lambda data, **kw: data):
                    result = R._run_daily(now=dt.datetime(2026, 10, 14, 18))
                    assert result['status'] == 'ok', result
                    assert len(result['saved']) == len(R.S.STRATEGIES)
                    assert all(row is payload for row in result['saved'])
            with mock.patch.object(SEL.U, 'is_trade_day', return_value=False) as calendar:
                assert PR._is_trade_day(dt.date(2026, 10, 14)) is False
                calendar.assert_called_once_with(dt.date(2026, 10, 14))
            assert not {'main', 'fastapi'} & sys.modules.keys()
        ''')
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent))
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script],
                                env=env, capture_output=True, text=True,
                                encoding='utf-8', timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_http_selection_and_latest_share_the_service_cache_and_saved_result(self):
        payload = {'picks': [{'code': '600000', 'score': 0.5}], 'context_pending': []}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(SEL, '_SELECTION_RESULT_DIR', directory), \
                mock.patch.dict(SEL._SELECT_RESULT_CACHE, {}, clear=True), \
                mock.patch.object(SEL, '_select_uncached', return_value=payload) as run, \
                mock.patch.object(SEL, '_selection_result_staleness', return_value=(False, None)):
            self.assertIs(payload, M.select('three_day', 10))
            self.assertIs(payload, M.select('three_day', 10))
            run.assert_called_once_with(strategy='three_day', topn=10, background_context=True)
            latest = M.select_latest('three_day', 10)
            self.assertEqual(payload['picks'], latest['picks'])
            self.assertTrue(latest['found'])
            self.assertFalse(latest['stale'])
            run.assert_called_once()
        self.assertIs(M._LIVE_CACHE, SEL._LIVE_CACHE)

    def test_http_rejects_unknown_strategy_and_keeps_topn_validation(self):
        client = TestClient(M.app)
        self.addCleanup(client.close)
        with mock.patch.dict(SEL._SELECT_RESULT_CACHE, {}, clear=True), \
                mock.patch.object(SEL.U, 'coverage_report', side_effect=AssertionError('invalid strategy fetched data')):
            response = client.get('/api/select', params={'strategy': 'missing'})
            self.assertEqual(400, response.status_code)
            self.assertEqual({'error': '未知策略'}, response.json())
        for topn in (0, 101):
            with self.subTest(topn=topn), mock.patch.object(SEL, 'select') as run:
                self.assertEqual(422, client.get('/api/select', params={'topn': topn}).status_code)
                run.assert_not_called()

    def test_rejected_factor_freshness_keeps_rows_out_of_the_shared_cache(self):
        frame = SEL.pd.DataFrame({column: [True] for column in SEL._SELECTION_REQUIRED_COLUMNS},
                                 index=['600000'])
        freshness = {'passed': False, 'target_date': '2026-10-14',
                     'factor_date': '2026-10-13', 'reason': 'waiting factors'}
        with mock.patch.dict(SEL._SELECTION_CACHE, {
                    'signature': None, 'price_factors': None, 'first_board_codes': None, 'status': None}, clear=True), \
                mock.patch.object(SEL, '_selection_signature', return_value=('test',)), \
                mock.patch.object(SEL.SFS, 'read', return_value=(frame, {'first_board_codes': ['600000']})), \
                mock.patch.object(SEL.U, 'load_universe', return_value=[]), \
                mock.patch.object(SEL.P, '_selection_factor_freshness', return_value=freshness):
            for _ in range(2):
                actual, first_boards, status = SEL._load_selection_base(with_status=True)
                self.assertTrue(actual.empty)
                self.assertEqual(set(), first_boards)
                self.assertEqual('blocked', status['state'])
                self.assertEqual('factor_snapshot_not_ready', status['reason_code'])
                self.assertEqual('2026-10-13', status['factor_date'])

    def test_refresh_during_selection_invalidates_even_when_freshness_stays_true(self):
        old = {'data': {'600000': {'net_profit': 1}}, 'report_dates': []}
        new = {'data': {'600000': {'net_profit': 2}}, 'report_dates': []}
        published, allow_clear = threading.Event(), threading.Event()
        lock = threading.RLock()
        workers = []
        calls = []

        class PausedResultLock:
            def __enter__(self):
                if threading.current_thread().name == 'selection-test-refresh':
                    published.set()
                    allow_clear.wait(3)
                lock.acquire()

            def __exit__(self, *args):
                lock.release()

        def select(**kwargs):
            value = SEL._LIVE_CACHE['finance']
            calls.append(value)
            if len(calls) == 1:
                worker = threading.Thread(target=SEL._publish_selection_context,
                                          args=('finance', new), name='selection-test-refresh')
                workers.append(worker)
                worker.start()
                self.assertTrue(published.wait(2), 'refresh did not publish')
            return {'finance': value}

        with mock.patch.dict(SEL._LIVE_CACHE, finance=old), \
                mock.patch.dict(SEL._SELECTION_CONTEXT_STATE, {
                    key: {'refreshing': False, 'retry_at': 0.} for key in ('finance', 'sentiment')}, clear=True), \
                mock.patch.object(SEL, '_SELECTION_CONTEXT_VERSION', 0), \
                mock.patch.object(SEL.dfc, 'cached_age_seconds', return_value=0.), \
                mock.patch.dict(SEL._SELECT_RESULT_CACHE, {}, clear=True), \
                mock.patch.object(SEL, '_SELECT_RESULT_LOCK', PausedResultLock()), \
                mock.patch.object(SEL, '_select_uncached', side_effect=select), \
                mock.patch.object(SEL, '_save_selection_result'):
            SEL._publish_selection_context('finance', old)
            before = SEL._selection_context_signature()
            try:
                self.assertIs(old, SEL.select('three_day', 10)['finance'])
                self.assertEqual(before, SEL._selection_context_signature())
                self.assertIs(new, SEL.select('three_day', 10)['finance'])
                self.assertIs(new, SEL.select('three_day', 10)['finance'])
                self.assertEqual(2, len(calls))
            finally:
                allow_clear.set()
                for worker in workers:
                    worker.join(3)
                    self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
