"""Current evolution scope stays within the three-strategy roster; history is preserved."""
import json
import os
import sqlite3
import unittest
from unittest import mock

import adaptive_engine as AE
import deepseek_advisor as DA
import evolution_apply as APPLY
import evolution_loop as EL
import evolution_profiles as EP
import paper_trading as PT
import runtime_settings as RSET
import self_evolution as SE
import strategy_registry as SR
import strategy_roster as ROSTER
import strategy_weight_scope as SCOPE
from test_evolution_apply import EvolutionApplyTestBase
import test_evolution_loop as loop_tests
import test_strategy_roster as roster_tests

THREE = ROSTER.formal_strategy_ids()


class CurrentEvolutionScopeTests(EvolutionApplyTestBase):
    def setUp(self):
        super().setUp()
        self._bind_participants(THREE)

    def test_old_accounts_are_not_sent_to_ai_even_with_active_overlays(self):
        with self._paper_ctx() as paper:
            paper.execute("UPDATE paper_accounts SET params=? WHERE id='sector_rotation'",
                          (json.dumps({'adaptive_selection': {'weights': {'flow': 1.0}}}),))
        accounts = DA._tuning_accounts(self.paper_path)
        self.assertEqual({row['account_id'] for row in accounts}, {'tq_breakout', 'trend_pullback'})
        self.assertNotIn('sector_rotation', DA._tuning_prompt({}, accounts, 'intraday')[1])

    def test_missing_ledger_does_not_create_file_or_fallback_to_labels(self):
        missing = os.path.join(self.tmp.name, 'missing.sqlite3')
        with mock.patch.object(AE, 'PAPER_DB_PATH', missing):
            self.assertEqual(AE._weight_participants(), [])
        self.assertEqual(DA._tuning_accounts(missing), [])
        self.assertFalse(os.path.exists(missing))

    def test_empty_cycle_and_unreadable_ledger_have_no_weight_participants(self):
        self._bind_participants(())
        with mock.patch.object(AE, 'PAPER_DB_PATH', self.paper_path):
            self.assertEqual(AE._weight_participants(), [])
            with mock.patch.object(AE.paper_reader, 'connect', side_effect=sqlite3.OperationalError):
                self.assertEqual(AE._weight_participants(), [])

    def test_unconfigured_or_unbound_cycle_cannot_create_weight_proposals(self):
        with self._paper_ctx() as paper:
            paper.execute('UPDATE paper_cycles SET enabled_strategies=NULL')
            self.assertEqual(SCOPE.participant_ids(paper), ())
            paper.execute('UPDATE paper_cycles SET enabled_strategies=?', (json.dumps(THREE),))
            paper.execute('UPDATE paper_accounts SET cycle_id=NULL')
            self.assertEqual(SCOPE.participant_ids(paper), ())

    def test_saved_legacy_lifecycle_cannot_restore_old_weight_participants(self):
        self._bind_participants((*THREE, *ROSTER.exit_only_ids()))
        with self._paper_ctx() as paper:
            self.assertEqual(set(SCOPE.participant_ids(paper)), set(THREE))

    def test_no_participant_generates_no_persisted_bandit_decision(self):
        self._bind_participants(())
        with self._adaptive_ctx() as adaptive, \
                mock.patch.object(AE, 'PAPER_DB_PATH', self.paper_path), \
                mock.patch.object(AE, '_reward_stats', return_value=([], {}, {}, {}, set())), \
                mock.patch.object(AE.trade_attribution, 'summary_from_conn', return_value={}):
            before = adaptive.total_changes
            decision = AE._bandit_decision(adaptive, {'regime': 'balanced'}, 1)
            self.assertEqual(decision['status'], 'no_participant')
            self.assertEqual(decision['weights'], {})
            self.assertEqual(adaptive.total_changes, before)

    def test_retired_ai_proposal_is_rejected_without_account_writes(self):
        run = self._seed_tuner_run([{'account_id': 'sector_rotation', 'weights': {'flow': 1.0}}])
        before = self._paper_params('sector_rotation')
        with mock.patch('evolution_validation.pre_apply_gate'), \
                self.assertRaisesRegex(ValueError, '执行参与者'):
            APPLY.apply_tuner_proposals(self._adaptive_ctx, self.paper_path, run, confirmed=True)
        self.assertEqual(self._paper_params('sector_rotation'), before)

    def test_old_strategies_have_no_dedicated_evolution_profile(self):
        self.assertEqual(set(EP.STRATEGY_EVOLUTION_PROFILES), set(THREE))
        for strategy in ROSTER.exit_only_ids():
            self.assertTrue(EP.evolution_profile_for(strategy)['fallback'])


class CurrentCycleIntegrationTests(unittest.TestCase):
    def test_evolution_reinitialization_and_next_cycle_keep_three_and_policy(self):
        fixture = roster_tests.CycleBudgetSeedingTests('test_three_strategy_cycle_splits_capital_by_roster_ratio')
        learning = sqlite3.connect(':memory:')
        learning.row_factory = sqlite3.Row
        try:
            fixture.setUp()
            roster_tests._load_script().authorize_daily_policy(PT.DB_PATH, effective_date='2026-10-26')
            with PT._db() as paper:
                template = dict(paper.execute('SELECT * FROM paper_accounts LIMIT 1').fetchone())
                for strategy in ROSTER.exit_only_ids():
                    values = {**template, 'id': strategy, 'status': 'paused', 'cycle_id': None,
                              'initial_cash': 0, 'cash': 0}
                    paper.execute('INSERT INTO paper_accounts('+','.join(values)+') VALUES('
                                  + ','.join('?' for _ in values)+')', tuple(values.values()))
                SR.ensure_schema(paper)

            def snapshot():
                with PT._db() as paper:
                    return {table: [tuple(row) for row in paper.execute('SELECT * FROM '+table)]
                            for table in ('paper_cycles', 'paper_accounts', 'paper_runtime_settings',
                                          'paper_orders', 'paper_fills', 'strategy_lifecycle_state')}

            before = snapshot()
            loop_tests.ProductionIntegrationTests()._seed(learning)
            active = SE.get_current_params(learning)['id']
            report = EL.run_loop(learning, EL.ProductionBackend(), generations=1)
            self.assertEqual(report['completed'], 1)
            self.assertTrue(SE.validated_pending(learning))
            self.assertEqual(SE.get_current_params(learning)['id'], active)
            self.assertEqual(snapshot(), before)
            PT.init_db()
            self.assertEqual(snapshot(), before)
            PT.set_accounts_status('paused')
            _, cycle = PT.start_new_cycle(capital=300000, include_dashboard=False)
            self.assertEqual(tuple(cycle['enabled_strategies']), THREE)
            with PT._db() as paper:
                self.assertEqual(RSET.enabled_strategies(paper), list(THREE))
                for row in paper.execute('SELECT id,params FROM paper_accounts WHERE cycle_id=?', (cycle['id'],)):
                    params = json.loads(row['params'])
                    self.assertEqual(params['adaptive_allocation_policy']['id'], 'authorized-daily-performance-v2')
                    self.assertEqual(params['adaptive_allocation']['weight_pct'], ROSTER.role_for(row['id']).budget_share * 100)
        finally:
            learning.close()
            fixture.doCleanups()
