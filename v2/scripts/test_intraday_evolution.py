from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import intraday_evolution as evolution


class CloseReviewTests(unittest.TestCase):
    def test_t_plus_one_attribution_separates_same_symbol_reentry(self):
        previous = {'equity_cents': 20000, 'positions': [
            {'slot': 'steady', 'symbol': 'A', 'quantity': 100, 'market_value_cents': 10000}]}
        closing = {'equity_cents': 19100, 'positions': [
            {'slot': 'steady', 'symbol': 'A', 'quantity': 100, 'market_value_cents': 9400}]}
        fills = [{'slot': 'steady', 'symbol': 'A', 'side': 'sell', 'quantity': 100,
                  'price_cents': 95, 'fee_cents': 100},
                 {'slot': 'steady', 'symbol': 'A', 'side': 'buy', 'quantity': 100,
                  'price_cents': 93, 'fee_cents': 400}]
        result = evolution.attribution(previous, closing, fills)
        self.assertEqual(result['daily_change_cents'], -900)
        self.assertEqual(result['carry_contribution_cents'], -600)
        self.assertEqual(result['opened_contribution_cents'], -300)
        with self.assertRaisesRegex(ValueError, 'does not reconcile'):
            evolution.attribution(previous, {**closing, 'equity_cents': 19000}, fills)

    def test_model_cannot_invent_evidence_or_set_live_strategy(self):
        item = {'mechanism': '比较弱市同输入的两本账', 'evidence': ['pnl'],
                'policy': {'kind': 'weak_entry_buffer', 'points': 5}}
        reply = {'summary': '新仓亏损', 'counterevidence': '部分错失机会后来上涨', 'candidates': [item]}
        first = evolution.validated_review(reply, {'pnl'}, '2026-11-20')
        self.assertEqual(first, evolution.validated_review(reply, {'pnl'}, '2026-11-20'))
        self.assertEqual(first['candidates'][0]['formed_on'], '2026-11-20')
        bad = [{'evidence': ['previous']}, {'policy': {'kind': 'strong_entry_relief', 'points': True}},
               {'policy': {'kind': 'weak_entry_buffer', 'points': 16}}, {'activate': True}]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                evolution.validated_review({**reply, 'candidates': [{**item, **change}]}, {'pnl'}, '2026-11-20')

    def test_same_day_gain_is_excluded_and_small_positive_sample_is_blocked(self):
        replay = {'formed_on': '2026-11-20', 'policy': {'kind': 'weak_entry_pause'},
                  'fill_timing': 'next_executable_snapshot', 'days': [
                      {'day': '2026-11-20', 'return_bps': [-500, 0]},
                      {'day': '2026-11-23', 'return_bps': [-30, 4]}]}
        x = evolution.evaluate(replay, expected_bps=25, pilot_stdev_bps=100)
        self.assertEqual(x['forward_days'], 1)
        self.assertEqual(x['mean_paired_bps'], 34)
        self.assertEqual(x['status'], 'blocked')
        self.assertIsNone(x['ci95'])

    def test_positive_pairings_only_qualify_for_independent_audit(self):
        start = date(2026, 1, 1)
        days = [{'day': (start + timedelta(days=i)).isoformat(), 'return_bps': [1, 48+i%3], 'covered_buckets': 80}
                for i in range(1, 31)]
        replay = {'formed_on': start.isoformat(), 'policy': {'kind': 'weak_entry_pause'},
                  'fill_timing': 'next_executable_snapshot', 'days': days}
        result = evolution.evaluate(replay, expected_bps=50, pilot_stdev_bps=60)
        self.assertGreater(result['ci95'][0], 0)
        self.assertEqual(result['status'], 'requires_independent_audit')
        reason = evolution.evaluate({**replay, 'diagnostic': True}, expected_bps=50, pilot_stdev_bps=60)
        self.assertIn('diagnostic_replay', reason['reasons'])

    def test_daily_freezes_successful_review_and_appends_trial_feedback(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            evolution.write_json(root/'trials'/'older.json', {'formed_on': '2026-11-19',
                                 'evaluated_through': '2026-11-20', 'status': 'blocked', 'days': [1]})
            db = Mock()
            db.execute.return_value.fetchall.return_value = [(0,)]
            with patch.object(evolution, 'collect', return_value={'day': '2026-11-20'}), \
                 patch.object(evolution, 'export') as export, \
                 patch.object(evolution, 'review', return_value={'model_review_status': 'complete'}) as review:
                for _ in range(2):
                    evolution.daily(db, root, '2026-11-20', root/'unused.json', model_on=True)
                export.assert_called_once()
                review.assert_called_once_with({'day': '2026-11-20'},
                    [{'formed_on': '2026-11-19', 'evaluated_through': '2026-11-20', 'status': 'blocked'}],
                    model_on=True)
                self.assertTrue((root/'rounds'/'2026-11-20.jsonl.gz').is_file())


class AdvanceTrialsTests(unittest.TestCase):
    """``advance_trials`` used to reference an undefined ``executable``, so the
    NameError aborted the close job even when there was nothing to advance — the
    review never ran and no trial was ever recorded."""

    def setUp(self):
        self.directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'rounds').mkdir()
        (self.root / 'reviews').mkdir()
        (self.root / 'trials').mkdir()
        self.binary = self.root / 'replay.bin'
        self.binary.write_bytes(b'replay-binary-v1')

    def review(self, formed_on, candidate_id='c1', policy=None):
        policy = policy or {'kind': 'weak_entry_buffer', 'points': 5}
        evolution.write_json(self.root / 'reviews' / f'{formed_on}.json', {
            'model_review_status': 'complete',
            'model_review': {'candidates': [
                {'mechanism': 'm', 'evidence': ['pnl'], 'policy': policy,
                 'candidate_id': candidate_id, 'formed_on': formed_on}]},
        })

    def archive(self, day):
        (self.root / 'rounds' / f'{day}.jsonl.gz').write_bytes(b'')

    def test_an_empty_candidate_set_returns_without_touching_anything(self):
        self.archive('2026-11-19')
        # No review at all: nothing to advance. This used to raise NameError.
        evolution.advance_trials(self.root, '2026-11-20', [str(self.binary), 'replay-intraday'],
                                 25, 100, replay_executable=self.binary)
        self.assertEqual(list((self.root / 'trials').glob('*.json')), [])

    def test_an_empty_candidate_set_returns_even_without_a_binary(self):
        evolution.advance_trials(self.root, '2026-11-20',
                                 [str(self.root / 'missing'), 'replay-intraday'],
                                 25, 100, replay_executable=self.root / 'missing')
        self.assertEqual(list((self.root / 'trials').glob('*.json')), [])

    def test_a_missing_replay_binary_is_a_clear_error_not_a_hash_of_nothing(self):
        self.review('2026-11-19')
        self.archive('2026-11-19')
        self.archive('2026-11-20')
        missing = self.root / 'absent-binary'
        with self.assertRaisesRegex(ValueError, 'replay executable does not exist'):
            evolution.advance_trials(self.root, '2026-11-20',
                                     [str(missing), 'replay-intraday'], 25, 100,
                                     replay_executable=missing)

    def test_a_trial_records_the_hash_of_the_passed_binary(self):
        import hashlib
        self.review('2026-11-19')
        self.archive('2026-11-19')
        self.archive('2026-11-20')
        recorded = {'status': 'collecting', 'reasons': ['insufficient_forward_days'],
                    'evaluated_through': '2026-11-20'}
        with patch.object(evolution, 'run_trial', return_value=dict(recorded)) as run:
            evolution.advance_trials(self.root, '2026-11-20',
                                     [str(self.binary), 'replay-intraday'], 25, 100,
                                     replay_executable=self.binary)
        self.assertEqual(run.call_count, 1)
        trial = evolution.json.loads(
            (self.root / 'trials' / 'c1.json').read_text(encoding='utf-8'))
        self.assertEqual(trial['binary_sha256'],
                         hashlib.sha256(b'replay-binary-v1').hexdigest())
        self.assertEqual(trial['status'], 'collecting')

    def test_the_binary_defaults_to_the_command_when_not_given(self):
        import hashlib
        self.review('2026-11-19')
        self.archive('2026-11-19')
        self.archive('2026-11-20')
        with patch.object(evolution, 'run_trial',
                          return_value={'status': 'collecting',
                                        'reasons': ['insufficient_forward_days']}):
            evolution.advance_trials(self.root, '2026-11-20', [str(self.binary)], 25, 100)
        trial = evolution.json.loads(
            (self.root / 'trials' / 'c1.json').read_text(encoding='utf-8'))
        self.assertEqual(trial['binary_sha256'],
                         hashlib.sha256(b'replay-binary-v1').hexdigest())

    def test_a_changed_binary_blocks_an_existing_collecting_trial(self):
        self.review('2026-11-19')
        self.archive('2026-11-19')
        self.archive('2026-11-20')
        evolution.write_json(self.root / 'trials' / 'c1.json', {
            'candidate_id': 'c1', 'status': 'collecting', 'evaluated_through': '2026-11-20',
            'binary_sha256': 'not-the-current-build', 'formed_on': '2026-11-19',
            'expected_bps': 25, 'pilot_stdev_bps': 100})
        with patch.object(evolution, 'run_trial') as run:
            evolution.advance_trials(self.root, '2026-11-21', [str(self.binary)], 25, 100,
                                     replay_executable=self.binary)
        run.assert_not_called()
        trial = evolution.json.loads(
            (self.root / 'trials' / 'c1.json').read_text(encoding='utf-8'))
        self.assertEqual(trial['status'], 'blocked')
        self.assertEqual(trial['reasons'], ['replay_binary_changed_requires_review'])

    def test_daily_passes_the_binary_through_to_the_trial_step(self):
        db = Mock()
        db.execute.return_value.fetchall.return_value = [(0,)]
        self.binary.write_bytes(b'replay-binary-v1')
        with patch.object(evolution, 'collect', return_value={'day': '2026-11-20'}), \
             patch.object(evolution, 'export'), \
             patch.object(evolution, 'review',
                          return_value={'model_review_status': 'complete'}), \
             patch.object(evolution, 'advance_trials') as advance:
            evolution.daily(db, self.root, '2026-11-20', self.root / 'unused.json',
                            model_on=False, replay_command=[str(self.binary), 'replay-intraday'],
                            replay_executable=self.binary)
        self.assertEqual(advance.call_args.kwargs['replay_executable'], self.binary)


if __name__ == '__main__':
    unittest.main()
