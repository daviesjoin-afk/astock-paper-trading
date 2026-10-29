"""手动委托规划的日线复用：驱动真实规划和真实账本夹具。"""
import datetime as dt
from dataclasses import replace
from unittest import mock

import pandas as pd

import manual_orders as MO
from test_deferred_fill_cycle_binding import _ProvenRiskHarness


class TestManualOrderRoundKline(_ProvenRiskHarness):
    def test_sell_plan_reuses_successful_kline_and_does_not_share_mutations(self):
        self._insert_lot(self.account_id, self.code, qty=200, cost=10.0)
        frame = pd.DataFrame(
            {'open': [10.0, 11.0], 'high': [11.0, 12.0], 'low': [9.0, 10.0],
             'close': [10.0, 11.0], 'volume': [100, 200], 'amount': [1000, 2200]},
            index=pd.to_datetime([self.day - dt.timedelta(days=3), self.day - dt.timedelta(days=1)]),
        )
        seen = []

        def decision(position, *, kline, **kwargs):
            seen.append(float(kline['close'].iloc[-1]))
            kline.iloc[-1, kline.columns.get_loc('close')] = 999.0
            return {'action': '观察'}

        def snapshot(payload, **kwargs):
            seen.append(float(kwargs['kline']['close'].iloc[-1]))
            return payload

        deps = MO._dependencies()
        quote = {'code': self.code, 'name': '测试股', 'price': 11.0,
                 'quote_at': self.day.isoformat() + ' 10:00:00'}
        with mock.patch.object(MO, '_completed_kline', return_value=frame) as load, \
             mock.patch.object(MO.DE, 'sell_decision', side_effect=decision), \
             mock.patch.object(MO, '_dependencies', return_value=replace(deps, _with_decision_snapshot=snapshot)):
            MO._manual_order_plan(self.conn, self.account_id, self.code, 'sell', qty=100,
                                  asof_day=self.day, quote=quote,
                                  all_quotes={self.code: quote})
            self.assertEqual(load.call_count, 1)
            MO._manual_order_plan(self.conn, self.account_id, self.code, 'sell', qty=100,
                                  asof_day=self.day, quote=quote,
                                  all_quotes={self.code: quote})
        self.assertEqual(load.call_count, 2, '不同规划必须重新取证')
        self.assertEqual(seen, [11.0, 11.0, 11.0, 11.0])
        self.assertEqual(float(frame['close'].iloc[-1]), 11.0)

    def test_missing_kline_is_retried_in_same_plan(self):
        self._insert_lot(self.account_id, self.code, qty=200, cost=10.0)
        frame = pd.DataFrame({'close': [11.0]}, index=pd.to_datetime([self.day - dt.timedelta(days=1)]))
        quote = {'code': self.code, 'name': '测试股', 'price': 11.0,
                 'quote_at': self.day.isoformat() + ' 10:00:00'}
        deps = MO._dependencies()
        with mock.patch.object(MO, '_completed_kline', side_effect=[None, frame]) as load, \
             mock.patch.object(MO.DE, 'sell_decision', return_value={'action': '观察'}) as decision, \
             mock.patch.object(MO, '_dependencies', return_value=replace(
                 deps, _with_decision_snapshot=lambda payload, **kwargs: payload)):
            MO._manual_order_plan(self.conn, self.account_id, self.code, 'sell', qty=100,
                                  asof_day=self.day, quote=quote, all_quotes={self.code: quote})
        self.assertEqual(load.call_count, 2)
        pd.testing.assert_frame_equal(decision.call_args.kwargs['kline'], frame)
