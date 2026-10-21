"""Real slot resolution, transport and attribution persistence without network."""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock
import urllib.error

import ai_provider_transport as transport
import ai_review_service as review
import deepseek_advisor as legacy
import trade_attribution as attribution


class AttributionSlotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "adaptive.sqlite3"
        self.active_connections = []
        self.calls = []
        for patcher in (
            mock.patch.dict(os.environ, {"LLM_PROVIDER": "deepseek"}, clear=True),
            mock.patch.object(legacy, "configured", side_effect=AssertionError("legacy key gate used")),
            mock.patch.object(legacy, "call_json", side_effect=AssertionError("legacy transport used")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.connect() as c:
            attribution.ensure_schema(c)
            review.update_slot(c, "ai2", api_key="canonical-test-key", base_url="https://slot.example/v1",
                               model="slot-model", enabled=True, timeout_seconds=17)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        self.active_connections.append(c)
        try:
            yield c
            c.commit()
        finally:
            self.active_connections.remove(c)
            c.close()

    def pending(self, count=1):
        with self.connect() as c:
            for order_id in range(1, count + 1):
                c.execute("""INSERT INTO adaptive_trade_attributions
                    (order_id,fill_id,account_id,code,side,qty,fill_date,fill_price,order_status,
                     asof_date,close_price,stock_move_pct,reason_codes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (order_id, order_id, "tq_breakout", "000001", "buy", 100, "2026-10-21", 10., "filled",
                     "2026-10-21", 10.2, 2., '["市场共振"]', "now", "now"))
            return attribution._pending_items(c, dt.date(2026, 10, 21))

    def rows(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM adaptive_trade_attributions ORDER BY order_id')]

    def http_reply(self, request, timeout):
        self.assertEqual(self.active_connections, [], "network call inside a database transaction")
        body = json.loads(request.data)
        evidence = json.loads(body['messages'][-1]['content'].split('\n', 1)[1])
        self.calls.append((request, timeout, body, evidence))
        content = {'analyses': [{'order_id': item['order_id'], 'summary': '市场共振，因果证据仍不足',
                                'primary_causes': ['市场共振'], 'confidence': 35} for item in evidence]}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': json.dumps(content)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 80}}).encode()
        return response

    def run_ai(self, items, config=None):
        with mock.patch.object(transport.urllib.request, 'urlopen', side_effect=self.http_reply):
            return attribution._ai_batches(items, config or {}, 'test-attribution', self.connect)

    def test_database_slot_works_without_legacy_env_and_persists_real_source(self):
        result = self.run_ai(self.pending())
        self.assertEqual(result['analyzed'], 1)
        self.assertEqual((result['provider'], result['model']), ('ai2', 'slot-model'))
        request, timeout, body, _ = self.calls[0]
        self.assertEqual(request.full_url, 'https://slot.example/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer canonical-test-key')
        self.assertEqual(timeout, 17)
        self.assertEqual(body['model'], 'slot-model')
        row = self.rows()[0]
        self.assertEqual((row['ai_status'], row['ai_provider']), ('completed', 'ai2'))
        self.assertEqual(row['ai_summary'], '市场共振，因果证据仍不足')
        self.assertEqual(row['stock_move_pct'], 2.)
        self.assertEqual(json.loads(row['reason_codes']), ['市场共振'])

    def test_saved_provider_binding_uses_current_credentials_and_model(self):
        with self.connect() as c:
            saved = review.save_provider(c, display_name='服务商', api_key='before-rotation',
                base_url='https://library.example/v1', models=['library-model'], timeout_seconds=23)
            review.update_slot(c, 'ai2', provider_id=saved['id'], model='library-model')
            review.save_provider(c, saved['id'], api_key='rotated-canonical-key')
        result = self.run_ai(self.pending())
        self.assertEqual(result['model'], 'library-model')
        request, timeout, _, _ = self.calls[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer rotated-canonical-key')
        self.assertEqual(request.full_url, 'https://library.example/v1/chat/completions')
        self.assertEqual(timeout, 23)

    def test_existing_provider_selection_can_choose_ai1(self):
        with self.connect() as c:
            review.update_slot(c, 'ai1', api_key='ai1-key', base_url='https://ai1.example/v1',
                               model='ai1-model', enabled=True)
        with mock.patch.dict(os.environ, {'LLM_PROVIDER': 'mimo'}):
            result = self.run_ai(self.pending())
        self.assertEqual((result['provider'], result['model']), ('ai1', 'ai1-model'))
        self.assertEqual(self.calls[0][0].full_url, 'https://ai1.example/v1/chat/completions')

    def test_disabled_cleared_invalid_slots_and_ai_switch_never_make_paid_calls(self):
        cases = (({'enabled': False}, {}, 'disabled'),
                 ({'clear_api_key': True}, {}, 'not_configured'),
                 ({'base_url': ''}, {}, 'unusable_base_url'),
                 ({'model': ''}, {}, 'model_missing'),
                 ({}, {'trade_attribution_ai_enabled': False}, 'ai_disabled'))
        for changes, config, reason in cases:
            with self.subTest(reason=reason):
                with self.connect() as c:
                    review.update_slot(c, 'ai2', api_key='canonical-test-key', base_url='https://slot.example/v1',
                                       model='slot-model', enabled=True)
                    review.update_slot(c, 'ai2', **changes)
                    review.update_slot(c, 'ai1', api_key='other-slot-key', base_url='https://other.example',
                                       model='other-model', enabled=True)
                    c.execute('DELETE FROM adaptive_trade_attributions')
                items = self.pending()
                with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'stale-legacy-key'}), \
                        mock.patch.object(transport.urllib.request, 'urlopen') as http:
                    result = attribution._ai_batches(items, config, 'test', self.connect)
                http.assert_not_called()
                self.assertEqual(result['error'], reason)
                row = self.rows()[0]
                self.assertEqual(row['ai_status'], 'deterministic_fallback')
                self.assertIn('规则归因', row['ai_summary'])
                self.assertEqual(row['stock_move_pct'], 2.)

    def test_transport_failure_keeps_deterministic_evidence_and_hides_raw_error(self):
        items = self.pending()
        with mock.patch.object(transport.urllib.request, 'urlopen',
                               side_effect=urllib.error.URLError('canonical-test-key')):
            result = attribution._ai_batches(items, {}, 'test', self.connect)
        self.assertEqual(result['analyzed'], 0)
        self.assertEqual(result['error'], 'network_error')
        row = self.rows()[0]
        self.assertEqual(row['ai_status'], 'failed_fallback')
        self.assertEqual(row['stock_move_pct'], 2.)
        self.assertNotIn('canonical-test-key', json.dumps(result) + json.dumps(row))

    def test_batches_and_missing_response_rows_are_counted_correctly(self):
        items = self.pending(21)
        result = self.run_ai(items)
        self.assertEqual(result['analyzed'], 21)
        self.assertEqual([len(call[3]) for call in self.calls], [20, 1])
        with mock.patch.object(transport, 'call_json', return_value=(
                {'analyses': [{'order_id': items[0]['order_id'], 'summary': '部分返回', 'confidence': 20}]}, 1, 1, 1)):
            result = attribution._ai_batches(items[:2], {}, 'partial', self.connect)
        self.assertEqual((result['analyzed'], result['deterministic']), (1, 1))
        with self.connect() as c:
            missing = c.execute('SELECT * FROM adaptive_trade_attributions WHERE order_id=?',
                                (items[1]['order_id'],)).fetchone()
            self.assertEqual(missing['ai_status'], 'deterministic_fallback')

    def test_no_pending_does_not_resolve_or_call_provider(self):
        with mock.patch.object(legacy, '_research_provider_config') as resolve, \
                mock.patch.object(transport, 'call_json') as call:
            result = attribution._ai_batches([], {}, 'test', self.connect)
        resolve.assert_not_called()
        call.assert_not_called()
        self.assertEqual(result['status'], 'no_pending')

    def test_run_audit_records_actual_slot_and_model(self):
        self.pending()
        with mock.patch.object(attribution, '_paper_orders', return_value=[]), \
                mock.patch.object(attribution, '_quote_maps', return_value=({}, {}, {}, {})), \
                mock.patch.object(transport.urllib.request, 'urlopen', side_effect=self.http_reply):
            result = attribution.run_close_attribution(self.connect, 'not-used.sqlite3',
                trade_date='2026-10-21', trigger='test-audit')
        self.assertEqual(result['detail']['ai']['analyzed'], 1)
        with self.connect() as c:
            row = c.execute('SELECT * FROM adaptive_trade_attribution_runs').fetchone()
        self.assertEqual((row['provider'], row['model'], row['analyzed_count']), ('ai2', 'slot-model', 1))


if __name__ == '__main__':
    unittest.main()
