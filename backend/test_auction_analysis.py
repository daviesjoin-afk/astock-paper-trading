"""AI 竞价证据、推断留档和复核不得使用未来或不存在的事实。"""
from contextlib import contextmanager, closing
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import ai_review_service as review
import auction_analysis as A
import auction_tracking as T


class AuctionAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patch = mock.patch.dict('os.environ', {'ASTOCK_DATA_DIR':str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        review.ensure_schema(self.conn)

    def at(self, minute):
        return dt.datetime(2026,10,22,9,minute,tzinfo=T.CN)

    def save_observations(self, count=1):
        observations=[]
        for minute in (18,24):
            rows=[{'code':f'{n:06d}','price':10+(minute-18)/100,'quote_at':self.at(minute).isoformat(),'volume':None,'source':'test'} for n in range(1,count+1)]
            observations.append({'captured_at':self.at(minute).isoformat(),'phase':'cancellable' if minute<20 else 'non_cancellable','rows':rows})
        path=self.root/'auction_tracking/current.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'trade_date':'2026-10-22','observations':observations}))

    def context(self):
        self.save_observations()
        return A.evidence(self.at(25),[{'code':'000001','price':10.1,'quote_at':self.at(25).isoformat()}],self.conn)

    def test_missing_auction_is_not_reconstructed_from_later_quotes(self):
        result=A.evidence(self.at(30),[{'code':'000001','price':12,'quote_at':self.at(30).isoformat()}],self.conn)
        self.assertEqual('not_collected',result['status'])
        self.assertEqual([],result['stocks'])

    def test_future_observations_and_future_current_quotes_are_excluded(self):
        self.save_observations()
        result=A.evidence(self.at(20),[{'code':'000001','price':99,'quote_at':self.at(25).isoformat()}],self.conn)
        self.assertEqual(1,len(result['stocks'][0]['points']))
        self.assertEqual(10,result['stocks'][0]['reference_quote']['price'])
        self.assertFalse(result['stocks'][0]['activity_available'])

    def test_focus_is_prioritized_and_input_is_bounded(self):
        self.save_observations(40)
        result=A.evidence(self.at(25),[],self.conn,{'000040'})
        self.assertEqual(40,result['observed_stocks'])
        self.assertEqual(20,result['included_stocks'])
        self.assertEqual('000040',result['stocks'][0]['code'])

    def test_judgment_cannot_invent_code_or_reference_price(self):
        context=self.context()
        raw=[{'code':'000001','intent':'support','confidence':120,'expected_direction':'up','reference_quote':{'price':999}}, {'code':'999999','intent':'accumulation'}]
        normalized=A.normalize_intents(raw,context)
        self.assertEqual(1,len(normalized))
        self.assertEqual(100,normalized[0]['confidence'])
        self.assertEqual(10.1,normalized[0]['reference_quote']['price'])
        self.assertEqual('price_only',normalized[0]['evidence_grade'])
        self.assertTrue(normalized[0]['intent_is_hypothesis'])

    def test_one_point_or_malformed_model_enums_cannot_claim_clear_intent(self):
        self.save_observations()
        context=A.evidence(self.at(20),[],self.conn)
        result=A.normalize_intents([{'code':'000001','intent':'accumulation','expected_direction':{},'confidence':'nan'}],context)
        self.assertEqual('unclear',result[0]['intent'])
        self.assertEqual('unclear',result[0]['expected_direction'])
        self.assertEqual(0,result[0]['confidence'])

    def write_judgment(self, *, issued=26, reference=25, direction='up'):
        judgment={'code':'000001','intent':'support','expected_direction':direction,'reference_quote':{'price':10,'quote_at':self.at(reference).isoformat()}}
        self.conn.execute('INSERT INTO dual_ai_tuning_runs(trigger,mode,status,evidence_hash,evidence,created_at,finished_at,reviewers) VALUES(?,?,?,?,?,?,?,?)',('test','intraday','both_hold','test','{}',self.at(issued).isoformat(),self.at(issued).isoformat(),json.dumps({'ai1':{'auction_intents':[judgment]}})))

    def test_later_price_can_support_or_contradict_direction_but_never_proves_intent(self):
        self.write_judgment()
        for price,verdict in ((11,'supported'),(9,'contradicted')):
            quotes=A._current_quotes([{'code':'000001','price':price,'quote_at':self.at(30).isoformat()}],self.at(30))
            result=A.feedback(self.conn,quotes,self.at(30))[0]
            self.assertEqual(verdict,result['direction_verdict'])
            self.assertEqual('not_directly_observable',result['intent_truth'])

    def test_before_response_old_reference_and_future_runs_are_not_scored(self):
        self.write_judgment()
        quotes=A._current_quotes([{'code':'000001','price':11,'quote_at':self.at(26).isoformat()}],self.at(30))
        self.assertEqual('pending',A.feedback(self.conn,quotes,self.at(30))[0]['direction_verdict'])
        self.write_judgment(issued=30,reference=18)
        quotes=A._current_quotes([{'code':'000001','price':11,'quote_at':self.at(35).isoformat()}],self.at(35))
        self.assertEqual('pending',A.feedback(self.conn,quotes,self.at(35))[0]['direction_verdict'])
        self.assertEqual([],A.feedback(self.conn,quotes,self.at(25)))

    def test_audit_persists_normalized_model_judgments(self):
        context=self.context()
        @contextmanager
        def connect():
            with self.conn:
                yield self.conn
        reviewers={'ai1':{'status':'completed','decision':'hold','proposals':[],
                         'auction_intents':[{'code':'000001','intent':'support','expected_direction':'up'}]}}
        with mock.patch.object(review,'_now',return_value=self.at(26).isoformat()):
            run_id,_=review._write_audit(connect,'test','intraday','both_hold','dual','dual_review',{},
                                         {'auction_tracking':context},'test',{},reviewers,False,'hold',[],
                                         self.at(25).isoformat(),time.monotonic())
        saved=json.loads(self.conn.execute('SELECT reviewers FROM dual_ai_tuning_runs WHERE id=?',(run_id,)).fetchone()[0])
        self.assertEqual(10.1,saved['ai1']['auction_intents'][0]['reference_quote']['price'])
        self.assertIn('auction_intents',review._reviewer_response(saved['ai1']))
        prompt=review._build_tuning_user_prompt({'auction_tracking':context},[], 'intraday')
        self.assertIn('review_feedback',prompt)
        self.assertIn('auction_intents',prompt)

    def test_followup_is_retained_across_days_without_writing_the_audit_database(self):
        self.write_judgment()
        path=self.root/'adaptive_learning.sqlite3'
        # Backup requires the in-memory source transaction to be committed.
        self.conn.commit()
        with closing(sqlite3.connect(path)) as destination:
            self.conn.backup(destination)
        before=path.read_bytes()
        result=A.record_outcomes([{'code':'000001','price':11,'quote_at':self.at(30).isoformat()}],self.at(30))
        self.assertEqual(1,result)
        self.assertEqual(before,path.read_bytes())
        tomorrow=self.at(30)+dt.timedelta(days=1)
        evidence=A.evidence(tomorrow,[],self.conn)
        self.assertEqual('supported',evidence['review_feedback'][0]['direction_verdict'])
        self.assertEqual([],A.evidence(self.at(25),[],self.conn)['review_feedback'])

    def test_pending_current_quote_does_not_erase_an_observed_historical_verdict(self):
        old={'run_id':1,'slot':'ai1','code':'000001','observed_at':self.at(30).isoformat(),'direction_verdict':'supported'}
        pending={**old,'observed_at':None,'direction_verdict':'pending'}
        self.assertEqual('supported',A._merge_feedback([old],[pending])[0]['direction_verdict'])
