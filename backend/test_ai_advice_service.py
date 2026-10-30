# -*- coding: utf-8 -*-
"""入场建议归因的验证（方案 §6.1/§6.3）：ADVISORY 下行为不变，归因可追溯。"""
import unittest

import ai_advice_service as ADV

NOW = "2026-10-26T02:00:00+00:00"
IDENTITY = {"strategy_id": "tq_breakout", "strategy_version": 1,
            "strategy_checksum": "a" * 64}


def rule(approved=True, reasons=()):
    return ADV.RuleDecision(code="600001", approved=approved, reason_codes=tuple(reasons))


def opinion(**overrides):
    kwargs = dict(recommendation_id="rec-1", action="prefer_entry",
                  valid_until="2026-10-26T03:00:00+00:00", exact_identity=IDENTITY,
                  reason="结构良好")
    kwargs.update(overrides)
    return ADV.Opinion(**kwargs)


def audit(**overrides):
    kwargs = dict(code="600001", mode=ADV.MODE_ADVISORY, rule=rule(),
                  opinion=opinion(), expected_identity=IDENTITY, now=NOW)
    kwargs.update(overrides)
    return ADV.compose_entry_audit(**kwargs)


class PublishableActionVocabularyTests(unittest.TestCase):
    def test_every_position_action_can_be_published(self):
        """意见服务支持的退出动作必须都能通过 store 的 action 白名单。"""
        import ai_recommendation_store as STORE

        self.assertLessEqual(set(ADV.POSITION_ACTIONS), set(STORE.ALL_ACTIONS))
        self.assertIn("wait_soft_exit", STORE.TRADE_ACTIONS)


class ReasonClassificationTests(unittest.TestCase):
    def test_hard_soft_and_unknown_codes_are_separated(self):
        self.assertEqual(ADV.classify_reason("suspended"), ADV.REASON_CLASS_HARD)
        self.assertEqual(ADV.classify_reason("score_below_threshold"),
                         ADV.REASON_CLASS_SOFT)

    def test_an_unrecognised_code_is_treated_as_hard(self):
        """认不出来的原因按硬约束处理：不解析 AI 文案去猜它是不是软门槛。"""
        self.assertEqual(ADV.classify_reason("some_new_gate_v9"), ADV.REASON_CLASS_UNKNOWN)
        self.assertTrue(rule(approved=False, reasons=("some_new_gate_v9",)).has_hard_block())

    def test_a_soft_only_block_is_not_treated_as_hard(self):
        self.assertFalse(rule(approved=False,
                              reasons=("score_below_threshold",)).has_hard_block())


class AdvisoryModeTests(unittest.TestCase):
    def test_the_final_decision_always_equals_the_rule_decision(self):
        for mode in (ADV.MODE_OFF, ADV.MODE_ADVISORY):
            with self.subTest(mode=mode):
                result = audit(mode=mode)
                self.assertEqual(result.final_decision, result.rules_only_decision)
                self.assertIsNone(result.consumed_recommendation_id)

    def test_the_reason_says_why_the_opinion_had_no_effect(self):
        self.assertEqual(audit(mode=ADV.MODE_ADVISORY).effect_reason,
                         ADV.R_MODE_IS_ADVISORY)
        self.assertEqual(audit(mode=ADV.MODE_OFF).effect_reason, ADV.R_MODE_OFF)

    def test_a_rejected_rule_stays_rejected_in_advisory(self):
        result = audit(rule=rule(approved=False, reasons=("suspended",)))
        self.assertFalse(result.final_decision["approved"])
        self.assertEqual(result.final_decision["reasons"], ["suspended"])


class OpinionValidityTests(unittest.TestCase):
    def test_no_opinion_leaves_the_rule_decision_untouched(self):
        result = audit(opinion=None)
        self.assertEqual(result.effect_reason, ADV.R_NO_OPINION)
        self.assertIsNone(result.ai_opinion)

    def test_an_expired_opinion_has_no_effect(self):
        result = audit(opinion=opinion(valid_until="2026-10-26T01:00:00+00:00"))
        self.assertEqual(result.effect_reason, ADV.R_OPINION_EXPIRED)

    def test_an_identity_mismatch_has_no_effect(self):
        result = audit(opinion=opinion(exact_identity={**IDENTITY, "strategy_version": 2}))
        self.assertEqual(result.effect_reason, ADV.R_IDENTITY_MISMATCH)

    def test_a_non_entry_action_is_out_of_scope(self):
        result = audit(opinion=opinion(action="propose_allocation"))
        self.assertEqual(result.effect_reason, ADV.R_OPINION_ACTION_OUT_OF_SCOPE)

    def test_the_same_opinion_cannot_be_consumed_twice(self):
        first = audit(mode=ADV.MODE_BOUNDED_AUTO)
        self.assertEqual(first.consumed_recommendation_id, "rec-1")
        again = audit(mode=ADV.MODE_BOUNDED_AUTO,
                      consumed=frozenset({"rec-1"}))
        self.assertEqual(again.effect_reason, ADV.R_ALREADY_CONSUMED)
        self.assertIsNone(again.consumed_recommendation_id)


class HardConstraintTests(unittest.TestCase):
    def test_a_hard_block_suppresses_any_entry_preference(self):
        result = audit(mode=ADV.MODE_BOUNDED_AUTO,
                       rule=rule(approved=False, reasons=("suspended",)))
        self.assertEqual(result.effect_reason, ADV.R_HARD_CONSTRAINT)
        self.assertFalse(result.final_decision["approved"])

    def test_an_unknown_reason_code_also_suppresses_it(self):
        result = audit(mode=ADV.MODE_BOUNDED_AUTO,
                       rule=rule(approved=False, reasons=("mystery_gate",)))
        self.assertEqual(result.effect_reason, ADV.R_HARD_CONSTRAINT)

    def test_prefer_entry_cannot_flip_a_rejected_rule(self):
        """最容易被误实现的一条：AI 不能把 rejected 改成 approved。"""
        result = audit(mode=ADV.MODE_BOUNDED_AUTO,
                       rule=rule(approved=False, reasons=()),
                       opinion=opinion(action="prefer_entry"))
        self.assertEqual(result.effect_reason, ADV.R_HARD_CONSTRAINT)
        self.assertFalse(result.final_decision["approved"])


class AuditTests(unittest.TestCase):
    def test_the_audit_is_deterministic_for_the_same_inputs(self):
        first = audit(mode=ADV.MODE_BOUNDED_AUTO)
        second = audit(mode=ADV.MODE_BOUNDED_AUTO)
        self.assertEqual(first.audit_fingerprint, second.audit_fingerprint)

    def test_the_audit_fingerprint_tracks_the_effect_reason(self):
        self.assertNotEqual(audit(mode=ADV.MODE_ADVISORY).audit_fingerprint,
                            audit(mode=ADV.MODE_BOUNDED_AUTO).audit_fingerprint)

    def test_the_consumption_key_pairs_opinion_with_decision(self):
        result = audit(mode=ADV.MODE_BOUNDED_AUTO)
        self.assertEqual(ADV.consumption_key(result, "decision-7"), "rec-1@decision-7")
        self.assertIsNone(ADV.consumption_key(audit(mode=ADV.MODE_ADVISORY), "decision-7"))

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ADV.AdviceRejected):
            audit(mode="FULL_AUTO")


class ExitAdviceTests(unittest.TestCase):
    def _exit(self, **overrides):
        kwargs = dict(code="600001", mode=ADV.MODE_BOUNDED_AUTO,
                      rule=ADV.ExitDecision(code="600001", should_exit=True,
                                           reason_codes=("take_profit_target",)),
                      opinion=opinion(action="wait_soft_exit"), expected_identity=IDENTITY,
                      now=NOW)
        kwargs.update(overrides)
        return ADV.compose_exit_audit(**kwargs)

    def test_a_hard_exit_cannot_be_deferred_by_the_ai(self):
        result = self._exit(rule=ADV.ExitDecision(code="600001", should_exit=True,
                                                  reason_codes=("hard_stop",)))
        self.assertEqual(result.effect_reason, ADV.R_HARD_EXIT_WINS)
        self.assertTrue(result.final_decision["should_exit"])
        self.assertIsNone(result.consumed_recommendation_id)

    def test_an_unrecognised_exit_reason_is_treated_as_a_hard_exit(self):
        result = self._exit(rule=ADV.ExitDecision(code="600001", should_exit=True,
                                                  reason_codes=("mystery_exit",)))
        self.assertEqual(result.effect_reason, ADV.R_HARD_EXIT_WINS)

    def test_waiting_for_a_soft_exit_needs_an_actual_soft_exit(self):
        result = self._exit(rule=ADV.ExitDecision(code="600001", should_exit=False,
                                                  reason_codes=()))
        self.assertEqual(result.effect_reason, ADV.R_SOFT_EXIT_ONLY)

    def test_a_soft_exit_can_be_deferred_under_bounded_auto(self):
        result = self._exit()
        self.assertEqual(result.effect_reason, "ai_opinion_considered")
        self.assertEqual(result.consumed_recommendation_id, "rec-1")
        # 但规则侧"应当退出"的结论本身不被改写。
        self.assertTrue(result.final_decision["should_exit"])

    def test_advisory_never_changes_the_exit_decision(self):
        result = self._exit(mode=ADV.MODE_ADVISORY)
        self.assertEqual(result.effect_reason, ADV.R_MODE_IS_ADVISORY)
        self.assertEqual(result.final_decision, result.rules_only_decision)

    def test_an_exit_action_must_be_in_scope(self):
        result = self._exit(opinion=opinion(action="prefer_entry"))
        self.assertEqual(result.effect_reason, ADV.R_OPINION_ACTION_OUT_OF_SCOPE)


class AllocationTests(unittest.TestCase):
    PARTICIPANTS = ("tq_breakout", "trend_pullback", "reported_profit_breakout")

    def test_normalization_fills_the_gap_and_keeps_the_bounds(self):
        projected = ADV.normalize_allocation(
            ADV.AllocationProposal(ratios=(("tq_breakout", 0.6), ("trend_pullback", 0.4))),
            participants=self.PARTICIPANTS, min_pct=10, max_pct=50)
        self.assertEqual(sorted(projected), sorted(self.PARTICIPANTS))
        self.assertAlmostEqual(sum(projected.values()), 100.0, places=6)
        for value in projected.values():
            self.assertGreaterEqual(value, 10.0)
            self.assertLessEqual(value, 50.0)

    def test_an_unknown_strategy_is_refused(self):
        with self.assertRaises(ADV.AdviceRejected) as caught:
            ADV.normalize_allocation(
                ADV.AllocationProposal(ratios=(("mystery", 1.0),)),
                participants=self.PARTICIPANTS, min_pct=10, max_pct=50)
        self.assertIn(ADV.R_ALLOCATION_UNKNOWN_STRATEGY, str(caught.exception))

    def test_an_infeasible_bound_is_refused_not_silently_clamped(self):
        with self.assertRaises(ADV.AdviceRejected) as caught:
            ADV.normalize_allocation(
                ADV.AllocationProposal(ratios=(("a", 0.5), ("b", 0.5))),
                participants=("a", "b"), min_pct=60, max_pct=100)
        self.assertIn(ADV.R_ALLOCATION_INFEASIBLE, str(caught.exception))

    def test_negative_or_all_zero_targets_are_refused(self):
        for ratios in ((("tq_breakout", -1.0),), (("tq_breakout", 0.0),)):
            with self.subTest(ratios=ratios):
                with self.assertRaises(ADV.AdviceRejected) as caught:
                    ADV.normalize_allocation(
                        ADV.AllocationProposal(ratios=ratios),
                        participants=self.PARTICIPANTS, min_pct=10, max_pct=50)
                self.assertIn(ADV.R_ALLOCATION_NOT_ALL_POSITIVE, str(caught.exception))


class AuditPersistenceTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        import tempfile

        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/paper.sqlite3"
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        import paper_schema_migrations as PSM

        PSM.ensure_ai_decision_audits(self.conn)

    def tearDown(self):
        self.conn.close()
        self._folder.cleanup()

    def _record(self, **overrides):
        kwargs = dict(decision_id="decision-1", strategy_id="tq_breakout", now=NOW)
        kwargs.update(overrides)
        return ADV.record_audit(self.conn, audit(mode=ADV.MODE_ADVISORY), **kwargs)

    def test_a_row_records_rule_opinion_final_and_reason(self):
        row = self._record()
        self.assertEqual(row["decision_id"], "decision-1")
        self.assertEqual(row["effect_reason"], ADV.R_MODE_IS_ADVISORY)
        self.assertIn("approved", row["rules_only_decision"])
        self.assertEqual(row["final_decision"], row["rules_only_decision"])
        self.assertEqual(row["strategy_id"], "tq_breakout")

    def test_recording_the_same_audit_twice_is_idempotent(self):
        first = self._record()
        again = self._record()
        self.assertEqual(first["audit_id"], again["audit_id"])
        self.assertEqual(len(ADV.audits_for_decision(self.conn, "decision-1")), 1)

    def test_a_decision_id_is_required(self):
        with self.assertRaises(ADV.AuditStoreError):
            self._record(decision_id="")

    def test_the_table_is_append_only(self):
        import sqlite3

        row = self._record()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE ai_decision_audits SET effect_reason='x'"
                              " WHERE audit_id=?", (row["audit_id"],))
        self.conn.rollback()

    def test_the_migration_registry_registers_v40_exactly_once(self):
        import db_migrate

        versions = [row[0] for row in db_migrate.MIGRATIONS["paper_trading"]]
        self.assertEqual(len(versions), len(set(versions)), "迁移版本号不能重复")
        self.assertGreaterEqual(max(versions), 40, "不得遗漏 v40 或阻止后续独立迁移")
        self.assertIn(40, versions)
        self.assertIs(next(row[2] for row in db_migrate.MIGRATIONS["paper_trading"] if row[0] == 40),
                      db_migrate.paper_schema.ensure_ai_decision_audits)

    def test_attribution_summary_counts_reasons_without_claiming_alpha(self):
        self._record()
        self._record(decision_id="decision-2", strategy_id="trend_pullback")
        summary = ADV.attribution_summary(self.conn)
        self.assertEqual(summary["audits"], 2)
        self.assertEqual(summary["by_effect_reason"], {ADV.R_MODE_IS_ADVISORY: 2})
        self.assertEqual(summary["by_mode"], {ADV.MODE_ADVISORY: 2})
        self.assertEqual(summary["opinions_consumed"], 0)
        self.assertEqual(summary["decisions_changed_by_ai"], 0)
        self.assertIn("成对实验", summary["note"])
        scoped = ADV.attribution_summary(self.conn, strategy_id="tq_breakout")
        self.assertEqual(scoped["audits"], 1)


class ProductionConsumptionTests(unittest.TestCase):
    """生产消费点：账本里只追加解释，规则结论一行都不改。"""

    def setUp(self):
        import sqlite3
        import tempfile

        import paper_schema_migrations as PSM
        import ai_recommendation_store as STORE

        self._folder = tempfile.TemporaryDirectory()
        self.ledger = sqlite3.connect(f"{self._folder.name}/paper.sqlite3")
        self.ledger.row_factory = sqlite3.Row
        self.ledger.execute(
            "CREATE TABLE paper_audit (account_id TEXT, event TEXT, detail TEXT,"
            " created_at TEXT)")
        PSM.ensure_ai_decision_audits(self.ledger)
        self.research_path = f"{self._folder.name}/research.sqlite3"
        self.research = sqlite3.connect(self.research_path)
        self.research.row_factory = sqlite3.Row
        STORE.ensure_schema(self.research)

    def tearDown(self):
        self.research.close()
        self.ledger.close()
        self._folder.cleanup()

    def _opinion(self, **overrides):
        import ai_recommendation_store as STORE

        kwargs = dict(task_id="task-1", action="prefer_entry", reason="结构良好",
                      exact_identity=dict(IDENTITY), input_fingerprint="fp-1",
                      valid_until="2026-10-26T03:00:00+00:00", now=NOW)
        kwargs.update(overrides)
        recorded = STORE.record(self.research, **kwargs)
        self.research.commit()
        return recorded

    def _consume(self, **overrides):
        kwargs = dict(account_id="tq_breakout", code="600001", side="buy",
                      decision="approved_signal",
                      strategy_stamp=(IDENTITY["strategy_id"],
                                      IDENTITY["strategy_version"],
                                      IDENTITY["strategy_checksum"]),
                      queue_path=self.research_path, now=NOW)
        kwargs.update(overrides)
        return ADV.consume_risk_decision_safe(self.ledger, **kwargs)

    def _audits(self):
        return [dict(row) for row in self.ledger.execute(
            "SELECT * FROM ai_decision_audits ORDER BY rowid").fetchall()]

    def test_another_stock_does_not_hide_a_matching_opinion(self):
        first = self._opinion(payload={"code": "600001"})
        self._opinion(payload={"code": "600002"})
        result = self._consume()
        self.assertEqual(result["audit"]["ai_opinion"]["recommendation_id"],
                         first["recommendation_id"])
        self.assertEqual(result["final_decision"], result["audit"]["rules_only_decision"])

    def test_global_and_stock_opinions_use_latest_in_scope_without_stale_fallback(self):
        self._opinion(payload={"code": "600001"})
        latest = self._opinion(payload={}, valid_until="2026-10-26T01:00:00+00:00")
        result = self._consume()
        self.assertEqual(result["audit"]["effect_reason"], ADV.R_OPINION_EXPIRED)
        self.assertEqual(result["audit"]["ai_opinion"]["recommendation_id"],
                         latest["recommendation_id"])


    def test_without_any_opinion_the_rule_decision_is_audited_unchanged(self):
        result = self._consume()
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["audit"]["effect_reason"], ADV.R_NO_OPINION)
        self.assertEqual(result["audit"]["final_decision"],
                         result["audit"]["rules_only_decision"])
        self.assertTrue(result["audit"]["final_decision"]["approved"])
        self.assertEqual(result["audit"]["mode"], ADV.MODE_ADVISORY)

    def test_an_advisory_opinion_never_changes_the_final_decision(self):
        self._opinion()
        result = self._consume()
        self.assertEqual(result["audit"]["effect_reason"], ADV.R_MODE_IS_ADVISORY)
        self.assertEqual(result["audit"]["final_decision"],
                         result["audit"]["rules_only_decision"])
        self.assertIsNone(result["audit"]["consumed_recommendation_id"])
        self.assertEqual(result["audit"]["ai_opinion"]["action"], "prefer_entry")

    def test_a_rejected_rule_stays_rejected_even_with_a_prefer_entry_opinion(self):
        self._opinion()
        result = self._consume(decision="rejected_signal")
        self.assertFalse(result["audit"]["final_decision"]["approved"])
        self.assertEqual(result["audit"]["final_decision"]["reasons"],
                         ["rejected_signal"])

    def test_an_identity_mismatch_is_reported_as_such(self):
        self._opinion(exact_identity={**IDENTITY, "strategy_checksum": "b" * 64})
        result = self._consume()
        self.assertEqual(result["audit"]["effect_reason"], ADV.R_IDENTITY_MISMATCH)

    def test_an_exit_uses_the_real_rule_code_not_a_guessed_one(self):
        import ai_recommendation_store as STORE

        STORE.record(self.research, task_id="task-2", action="hold_position",
                     reason="趋势还在", exact_identity=dict(IDENTITY),
                     input_fingerprint="fp-2", valid_until="2026-10-26T03:00:00+00:00",
                     now=NOW)
        self.research.commit()
        hard = self._consume(side="sell", decision="risk_exit")
        self.assertEqual(hard["audit"]["effect_reason"], ADV.R_HARD_EXIT_WINS)
        self.assertTrue(hard["audit"]["final_decision"]["should_exit"])

    def test_the_ledger_points_at_the_risk_decision_row_it_consumed(self):
        self._consume()
        self.ledger.execute(
            "CREATE TABLE paper_risk_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        self.ledger.execute("INSERT INTO paper_risk_decisions DEFAULT VALUES")
        result = self._consume()
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(self._audits()[-1]["decision_id"], "risk:1")

    def test_a_missing_research_store_fails_open(self):
        result = ADV.consume_risk_decision_safe(
            self.ledger, account_id="tq_breakout", code="600001", side="buy",
            decision="approved_signal",
            strategy_stamp=(IDENTITY["strategy_id"], IDENTITY["strategy_version"],
                            IDENTITY["strategy_checksum"]),
            queue_path=f"{self._folder.name}/no-such-dir/research.sqlite3", now=NOW)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], ADV.R_PRODUCTION_UNAVAILABLE)
        self.assertEqual(self._audits(), [])

    def test_allocation_advice_keeps_the_rules_weights(self):
        import ai_recommendation_store as STORE

        STORE.record(self.research, task_id="task-3", action="propose_allocation",
                     reason="提高强策略权重",
                     exact_identity={"cycle_id": 1, "policy_id": "p"}, input_fingerprint="fp-3",
                     payload={"ratios": {"tq_breakout": 0.9, "trend_pullback": 0.1}}, now=NOW)
        self.research.commit()
        result = ADV.consume_safe(
            self.ledger, kind="allocation", decision_id="allocation:1:2026-10-26:abc",
            strategy_id="portfolio", code="cycle:1", rule=None,
            exact_identity={"cycle_id": 1, "policy_id": "p"},
            current={"tq_breakout": 50.0, "trend_pullback": 50.0},
            proposed={"tq_breakout": 90.0, "trend_pullback": 10.0},
            participants=("tq_breakout", "trend_pullback"), min_pct=5.0, max_pct=60.0,
            cycle_id=1, now=NOW, queue_path=self.research_path)
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["final_decision"],
                         {"tq_breakout": 50.0, "trend_pullback": 50.0})
        self.assertEqual(result["audit"]["effect_reason"], ADV.R_MODE_IS_ADVISORY)

    def test_the_attribution_summary_counts_what_actually_happened(self):
        self._opinion()
        self._consume()
        self._consume(decision="rejected_signal")
        summary = ADV.attribution_summary(self.ledger)
        self.assertEqual(summary["audits"], 2)
        self.assertEqual(summary["by_mode"], {ADV.MODE_ADVISORY: 2})
        self.assertEqual(summary["decisions_changed_by_ai"], 0)
        self.assertEqual(summary["opinions_consumed"], 0)


class ProductionWiringTests(unittest.TestCase):
    """接线证据：每个生产 owner 里确实存在调用点（不是只写了模块）。"""

    @staticmethod
    def _source(module, name):
        import inspect

        return inspect.getsource(getattr(module, name))

    def test_the_risk_decision_owner_consumes_advice(self):
        import paper_trading as PT

        source = self._source(PT, "_risk_log")
        self.assertIn("consume_risk_decision_safe", source)

    def test_the_risk_service_consumes_advice_for_real_exits(self):
        import paper_risk_service as PRSVC

        source = self._source(PRSVC, "run")
        self.assertIn("consume_risk_decision_safe", source)

    def test_the_allocation_owner_consumes_advice(self):
        import paper_dynamic_allocation as PDA

        source = self._source(PDA, "rebalance")
        self.assertIn("ADV.consume_safe", source)

    def test_the_trading_owner_emits_market_and_fill_events(self):
        import paper_trading as PT

        for name in ("generate_signals", "_buy_order", "_intraday_sell"):
            with self.subTest(owner=name):
                self.assertIn("notify_research", self._source(PT, name))

    def test_the_learning_owner_emits_the_label_event(self):
        import adaptive_engine as AE

        self.assertIn("notify_research", self._source(AE, "run_learning_cycle"))

    def test_the_worker_wakes_waiters_after_committing_receipts(self):
        import research_worker as RW

        source = self._source(RW, "run_once_pass")
        self.assertIn("wake_completed_experiments", source)
        self.assertLess(source.index("run_batch"), source.index("wake_completed_experiments"))


if __name__ == "__main__":
    unittest.main()
