# -*- coding: utf-8 -*-
"""R34-B contract regressions: deterministic multi-strategy allocation policy.

Covers the B1…B30 matrix, the canonical business invariants migrated off the
deleted text-derived intent path, and the architecture guards of the pure
policy layer.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import api_paper as API
import paper_trading as PT
import portfolio_allocation_policy as PAP
import portfolio_allocation_repository as PAPRepo
import portfolio_allocation_service as PAS
import portfolio_runtime as PR
import portfolio_runtime_service as PRS

CHECKSUM = "a" * 64
OTHER_CHECKSUM = "b" * 64


def _dim(name, status, facts=None, reasons=(), source="owner:test",
         source_fingerprint=None):
    if status == PR.UNAVAILABLE:
        return PR.PortfolioDimension(name, status, {}, "UNAVAILABLE",
                                     source_identity=source,
                                     blocking_reasons=tuple(reasons) or (f"{name}_unavailable",))
    return PR.PortfolioDimension(name, status, facts or {}, "OWNER_ISSUED",
                                 source_identity=source,
                                 source_fingerprint=source_fingerprint or CHECKSUM,
                                 blocking_reasons=tuple(reasons))


def _r34a_dimensions():
    """The dimension shape the current R34-A capture actually produces."""
    return (
        _dim("capital", PR.AVAILABLE, {"cycle_capital": 100000.0}),
        _dim("strategy_exposure", PR.PARTIAL,
             {"position_cost_by_account": [], "market_value_by_account": None},
             reasons=("exact_market_valuations_not_supplied",)),
        _dim("concentration", PR.UNAVAILABLE, reasons=("exact_market_valued_classification_unavailable",)),
        _dim("turnover", PR.UNAVAILABLE, reasons=("explicit_verified_turnover_window_unavailable",)),
        _dim("risk_consumption", PR.UNAVAILABLE, reasons=("exact_asof_risk_consumption_owner_unavailable",)),
        _dim("signal_conflicts", PR.UNAVAILABLE, reasons=("exact_asof_strategy_signal_intents_unavailable",)),
        _dim("capacity", PR.UNAVAILABLE, reasons=("cycle_asof_pending_capacity_owner_unavailable",)),
        _dim("correlation", PR.UNAVAILABLE, reasons=("exact_strategy_version_return_series_unavailable",)),
    )


def _full_evidence_dimensions():
    return (
        _dim("capital", PR.AVAILABLE, {"cycle_capital": 100000.0, "nav": 100000.0}),
        _dim("strategy_exposure", PR.AVAILABLE,
             {"market_value_by_account": {"s1": 10000.0, "s2": 5000.0},
              "market_value_requires_exact_market_evidence": True}),
        _dim("concentration", PR.UNAVAILABLE, reasons=("exact_market_valued_classification_unavailable",)),
        _dim("turnover", PR.UNAVAILABLE, reasons=("explicit_verified_turnover_window_unavailable",)),
        _dim("risk_consumption", PR.UNAVAILABLE, reasons=("exact_asof_risk_consumption_owner_unavailable",)),
        _dim("signal_conflicts", PR.UNAVAILABLE, reasons=("exact_asof_strategy_signal_intents_unavailable",)),
        _dim("capacity", PR.AVAILABLE,
             {"used": 15000.0, "pending": 0.0, "headroom": 67000.0}),
        _dim("correlation", PR.UNAVAILABLE, reasons=("exact_strategy_version_return_series_unavailable",)),
    )


def _snapshot(*, economic=("s1", "s2"), execution=None, risk_exit=None,
              asof_day="2026-09-30", decision_at="2026-10-01T09:30:00+08:00",
              dimensions=None, checksums=None):
    owners = tuple(sorted(economic))
    execution = tuple(sorted(owners if execution is None else execution))
    risk_exit = tuple(sorted(execution if risk_exit is None else risk_exit))
    checksums = dict(checksums or {})
    pins = [{"account_id": account, "strategy_id": account, "strategy_version": 1,
             "strategy_checksum": checksums.get(account, CHECKSUM),
             "lifecycle_state": "active"} for account in owners]
    return PR.build_portfolio_runtime_snapshot(
        cycle_id=7, asof_day=asof_day, decision_at=decision_at,
        cycle_identity={"cycle_id": 7, "cycle_key": "c-7"},
        strategy_pins=pins, economic_owner_ids=owners,
        execution_participant_ids=execution, risk_exit_participant_ids=risk_exit,
        source_identities={"cycle_owner": "paper_cycle_ownership:7"},
        market_evidence_identity=None,
        dimensions=tuple(dimensions if dimensions is not None else _r34a_dimensions()))


def _declaration(account, *, checksum=None, max_positions=4, min_positions=0, **overrides):
    values = {"account_id": account, "strategy_id": account, "strategy_version": 1,
              "strategy_checksum": checksum or CHECKSUM, "max_positions": max_positions,
              "min_positions": min_positions, "lifecycle_stage": "standard"}
    values.update(overrides)
    return PAP.StrategyResourceDeclaration(**values)


def _declarations(accounts, **kwargs):
    return tuple(_declaration(account, **kwargs) for account in accounts)


def _build(snapshot=None, declarations=None, weights=None, intents=(), **overrides):
    snapshot = snapshot if snapshot is not None else _snapshot()
    eligible = tuple(snapshot.execution_participant_ids)
    kwargs = {"hard_pool_cap": 10, "strategy_max_positions": 6,
              "strategy_min_positions": 2, "protected_slot_floor": 2}
    kwargs.update(overrides)
    return PAP.build_portfolio_allocation_plan(
        snapshot=snapshot,
        declarations=declarations if declarations is not None else _declarations(eligible),
        weights=weights if weights is not None else {a: 1.0 for a in eligible},
        intents=intents, **kwargs)


def _module_functions(path):
    """Module-level function names only (nested helpers are not public surface)."""
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    return {node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _imported_modules(path):
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(str(node.module or "").split(".")[0])
    return modules


def _attributed_names(path):
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def _string_literals(path):
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


class PlanIdentityTests(unittest.TestCase):
    def test_b1_same_exact_inputs_same_fingerprint(self):
        self.assertEqual(_build().plan_id, _build().plan_id)
        self.assertTrue(PAP.verify_plan_fingerprint(_build()))

    def test_b2_different_portfolio_snapshot_gives_different_plan(self):
        first = _build(snapshot=_snapshot(asof_day="2026-09-30"))
        second = _build(snapshot=_snapshot(asof_day="2026-10-01"))
        self.assertNotEqual(first.plan_id, second.plan_id)
        self.assertNotEqual(first.portfolio_snapshot_id, second.portfolio_snapshot_id)

    def test_b3_policy_version_enters_the_fingerprint(self):
        baseline = _build()
        with mock.patch.object(PAP, "POLICY_VERSION", "portfolio-allocation-policy-v2"):
            changed = _build()
        self.assertNotEqual(baseline.plan_id, changed.plan_id)
        self.assertIn(PAP.POLICY_VERSION, baseline.allocation_policy_version)

    def test_b4_corrupt_snapshot_fingerprint_fails_closed(self):
        snapshot = _snapshot()
        tampered = replace(snapshot, cycle_id=8)
        self.assertFalse(PR.verify_snapshot_fingerprint(tampered))
        with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                    "snapshot_fingerprint_mismatch"):
            _build(snapshot=tampered)
        with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                    "explicit_portfolio_snapshot_required"):
            PAP.build_portfolio_allocation_plan(
                snapshot="not-a-snapshot", declarations=(), weights={},
                hard_pool_cap=10, strategy_max_positions=6, strategy_min_positions=2,
                protected_slot_floor=2)

    def test_b5_no_latest_or_current_snapshot_fallback(self):
        for name in _attributed_names(PAS.__file__):
            lowered = name.lower()
            for forbidden in ("latest", "current_cycle", "recent", "newest"):
                self.assertNotIn(forbidden, lowered, msg=f"service calls {name}")
        self.assertEqual(_module_functions(PAS.__file__),
                         {"_declarations", "_intents", "_with_connection",
                          "capture_portfolio_allocation_plan",
                          "get_portfolio_allocation_plan"})

    def test_b8_input_order_is_canonical(self):
        snapshot = _snapshot()
        eligible = ("s1", "s2")
        forward = _build(snapshot=snapshot, declarations=_declarations(eligible),
                         weights={"s1": 1.0, "s2": 2.0},
                         intents=(PAP.ResourceIntent("i1", "s1", "RISK_EXIT", "600000"),
                                  PAP.ResourceIntent("i2", "s2", "NEW_ENTRY", "600000")))
        backward = _build(snapshot=snapshot, declarations=_declarations(eligible)[::-1],
                          weights={"s2": 2.0, "s1": 1.0},
                          intents=(PAP.ResourceIntent("i2", "s2", "NEW_ENTRY", "600000"),
                                   PAP.ResourceIntent("i1", "s1", "RISK_EXIT", "600000")))
        self.assertEqual(forward.plan_id, backward.plan_id)

    def test_b29_no_score_rank_or_winner(self):
        dumped = json.dumps(_build().projection()).lower()
        for forbidden in ("portfolio_score", "health_score", "rank", "winner", "optimal"):
            self.assertNotIn(forbidden, dumped)

    def test_b24_allocator_emits_no_risk_allow_or_block(self):
        dumped = json.dumps(_build().projection())
        self.assertNotIn("ALLOW", dumped)
        self.assertNotIn("BLOCK", dumped)
        self.assertNotIn("risk_decision", dumped)
        plan = _build()
        self.assertEqual(plan.conflict_plan["authority"],
                         "resource_execution_priority_only_not_risk_approval")


class DeclarationAndWeightContractTests(unittest.TestCase):
    def test_b6_declarations_must_exactly_cover_eligible_strategies(self):
        snapshot = _snapshot()
        with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                    "declaration_set_incomplete"):
            _build(snapshot=snapshot, declarations=(_declaration("s1"),))
        with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                    "declaration_set_exceeds_eligible"):
            _build(snapshot=snapshot,
                   declarations=(_declaration("s1"), _declaration("s2"), _declaration("s3")))

    def test_b7_current_registry_head_cannot_replace_a_snapshot_pin(self):
        snapshot = _snapshot(checksums={"s2": OTHER_CHECKSUM})
        with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                    "declaration_pin_mismatch"):
            _build(snapshot=snapshot,
                   declarations=(_declaration("s1"), _declaration("s2")))

    def test_b14_missing_factor_never_becomes_a_canonical_one(self):
        snapshot = _snapshot()
        with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                    "weight_set_incomplete"):
            _build(snapshot=snapshot, weights={"s1": 1.0})
        with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                    "weight_set_exceeds_eligible"):
            _build(snapshot=snapshot, weights={"s1": 1.0, "s2": 1.0, "s3": 1.0})
        with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                    "weight_set_incomplete"):
            _build(snapshot=snapshot, weights={})
        parameters = PAP.build_portfolio_allocation_plan.__code__.co_varnames
        self.assertIn("weights", parameters)

    def test_b13_r33_health_evidence_never_becomes_a_numeric_factor(self):
        source = pathlib.Path(PAP.__file__).read_text(encoding="utf-8")
        for forbidden in ("regime_fit", "confidence", "health", "data_quality",
                          "diversification", "health_score"):
            self.assertNotIn(forbidden, source)
        plan = _build(weights={"s1": 0.4, "s2": 0.9})
        self.assertEqual(plan.allocation_weights, {"s1": 0.4, "s2": 0.9})
        self.assertEqual(plan.slot_plan["applied_allocation_weights"],
                         {"s1": 0.4, "s2": 0.9})

    def test_invalid_explicit_weight_fails_closed(self):
        with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                    "canonical_weight_invalid"):
            _build(weights={"s1": float("nan"), "s2": 1.0})


class SlotPlanTests(unittest.TestCase):
    def test_b9_total_slots_never_exceed_the_hard_pool_cap(self):
        for cap in (0, 1, 3, 5, 9, 40):
            plan = _build(hard_pool_cap=cap)
            total = sum(plan.slot_plan["limits"].values())
            self.assertLessEqual(total, cap)
            self.assertEqual(total, plan.slot_plan["total_slots"])

    def test_b10_strategy_slots_never_exceed_the_declared_max(self):
        snapshot = _snapshot()
        declarations = (_declaration("s1", max_positions=2),
                        _declaration("s2", max_positions=9))
        plan = _build(snapshot=snapshot, declarations=declarations, hard_pool_cap=20)
        self.assertLessEqual(plan.slot_plan["limits"]["s1"], 2)
        self.assertLessEqual(plan.slot_plan["limits"]["s2"], 9)

    def test_no_eligible_strategy_yields_the_explicit_empty_status(self):
        snapshot = _snapshot(economic=(), execution=(), risk_exit=())
        plan = _build(snapshot=snapshot, declarations=(), weights={})
        self.assertEqual(plan.slot_plan["status"], PAP.NO_ELIGIBLE_STRATEGIES)
        self.assertEqual(plan.plan_status, PAP.NO_ELIGIBLE_STRATEGIES)
        self.assertEqual(plan.slot_plan["limits"], {})

    def test_slot_arithmetic_is_delegated_to_the_single_owner(self):
        plan = _build()
        self.assertEqual(plan.slot_plan["engine"], "allocation-engine-v2")
        self.assertEqual(plan.slot_plan["eligible_source"],
                         "PortfolioRuntimeSnapshot.execution_participant_ids")


class EvidenceGateTests(unittest.TestCase):
    def test_b15_capacity_unavailable_is_insufficient_evidence(self):
        plan = _build()
        self.assertEqual(plan.capacity_plan["status"], PAP.INSUFFICIENT_EVIDENCE)

    def test_b16_missing_pending_is_not_pending_zero(self):
        plan = _build()
        self.assertIsNone(plan.capacity_plan["pending_amount"])
        self.assertFalse(plan.capacity_plan["missing_pending_treated_as_zero"])
        self.assertIn("cycle_asof_pending_capacity_owner_unavailable",
                      plan.capacity_plan["blocking_reasons"])
        available = _build(snapshot=_snapshot(dimensions=_full_evidence_dimensions()))
        self.assertEqual(available.capacity_plan["status"], PAP.PLANNED)
        self.assertEqual(available.capacity_plan["pending_amount"], 0.0)

    def test_b17_market_value_unavailable_blocks_capital_allocation(self):
        plan = _build()
        self.assertEqual(plan.capital_plan["status"], PAP.INSUFFICIENT_EVIDENCE)
        self.assertIsNone(plan.capital_plan["allowance_by_strategy"])
        self.assertIn("exact_market_value_by_account_unavailable",
                      plan.capital_plan["blocking_reasons"])

    def test_b18_cost_basis_never_substitutes_market_value(self):
        plan = _build()
        self.assertIsNone(plan.capital_plan["market_value_by_account"])
        self.assertFalse(plan.capital_plan["cost_basis_used_as_market_value"])
        self.assertIn("market_valued_strategy_exposure", plan.capital_plan["required_evidence"])

    def test_b19_missing_correlation_does_not_become_zero(self):
        plan = _build()
        self.assertEqual(plan.correlation_term["status"], PAP.UNAVAILABLE)
        self.assertIsNone(plan.correlation_term["correlation"])
        self.assertFalse(plan.correlation_term["overlap_or_sector_substituted"])

    def test_b20_missing_classification_does_not_create_concentration_fact(self):
        plan = _build()
        self.assertEqual(plan.concentration_adjustment["status"], PAP.UNAVAILABLE)
        self.assertIsNone(plan.concentration_adjustment["adjustment"])
        self.assertFalse(plan.concentration_adjustment[
            "classification_guessed_from_name_or_sector"])

    def test_b30_pure_policy_has_no_network_provider_or_ai_dependency(self):
        self.assertEqual(_imported_modules(PAP.__file__),
                         {"__future__", "hashlib", "json", "re", "collections",
                          "dataclasses", "types", "paper_allocation",
                          "portfolio_runtime"})
        for literal in _string_literals(PAP.__file__):
            lowered = literal.lower()
            for forbidden in ("requests", "urllib", "socket", "sqlite3", "fastapi",
                              "paper_trading", "openai", "anthropic", "deepseek",
                              "datetime.now", "utcnow", "http://", "https://"):
                self.assertNotIn(forbidden, lowered, msg=f"policy literal: {literal[:60]}")


class EligibilityScopeTests(unittest.TestCase):
    def test_b11_paused_economic_owner_gets_no_new_resource(self):
        snapshot = _snapshot(economic=("s1", "paused"), execution=("s1",),
                             risk_exit=("s1", "paused"))
        self.assertIn("paused", snapshot.economic_owner_ids)
        self.assertNotIn("paused", snapshot.execution_participant_ids)
        plan = _build(snapshot=snapshot, declarations=_declarations(("s1",)),
                      weights={"s1": 1.0},
                      intents=(PAP.ResourceIntent("i1", "paused", "NEW_ENTRY", "600000"),
                               PAP.ResourceIntent("i2", "paused", "RISK_EXIT", "600000")))
        rows = {row["intent_id"]: row for row in plan.conflict_plan["ordered_intents"]}
        self.assertFalse(rows["i1"]["new_resource_eligible"])
        self.assertEqual(rows["i1"]["denial_reason"],
                         "new_resource_eligibility_requires_execution_participant")
        self.assertFalse(rows["i2"]["new_resource_eligible"])
        self.assertTrue(rows["i2"]["exit_right_eligible"])
        self.assertNotIn("paused", plan.slot_plan["limits"])
        self.assertNotIn("paused", plan.eligible_resource_strategy_ids)

    def test_b12_risk_exit_only_account_gets_no_entry_resource(self):
        snapshot = _snapshot(economic=("s1",), execution=("s1",),
                             risk_exit=("s1", "legacy_only"))
        plan = _build(snapshot=snapshot,
                      declarations=_declarations(("s1",)), weights={"s1": 1.0},
                      intents=(PAP.ResourceIntent("e1", "legacy_only", "NEW_ENTRY", "600000"),
                               PAP.ResourceIntent("e2", "legacy_only", "RISK_EXIT", "600000")))
        rows = {row["intent_id"]: row for row in plan.conflict_plan["ordered_intents"]}
        self.assertFalse(rows["e1"]["new_resource_eligible"])
        self.assertFalse(rows["e1"]["exit_right_eligible"])
        self.assertTrue(rows["e2"]["exit_right_eligible"])
        self.assertNotIn("legacy_only", plan.eligible_resource_strategy_ids)


class ConflictPolicyTests(unittest.TestCase):
    def _intents(self):
        return (
            PAP.ResourceIntent("add", "s2", "ADD_POSITION", "600000"),
            PAP.ResourceIntent("new", "s1", "NEW_ENTRY", "000001"),
            PAP.ResourceIntent("reduce", "s2", "RISK_REDUCE", "600000"),
            PAP.ResourceIntent("manual", "s1", "MANUAL_EXIT", "600000"),
            PAP.ResourceIntent("tp", "s1", "TAKE_PROFIT_EXIT", "600000"),
            PAP.ResourceIntent("risk", "s1", "RISK_EXIT", "600000"),
        )

    def test_b21_risk_exit_sorts_before_every_entry_intent(self):
        plan = _build(intents=self._intents())
        kinds = [row["intent_kind"] for row in plan.conflict_plan["ordered_intents"]]
        self.assertEqual(kinds, list(PAP.INTENT_KINDS))

    def test_b22_opposite_intents_stay_distinct_and_are_not_netted(self):
        plan = _build(intents=(
            PAP.ResourceIntent("buy", "s1", "NEW_ENTRY", "600000"),
            PAP.ResourceIntent("sell", "s2", "RISK_EXIT", "600000")))
        rows = plan.conflict_plan["ordered_intents"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["intent_id"] for row in rows}, {"buy", "sell"})
        self.assertFalse(plan.conflict_plan["opposite_intents_netted"])
        self.assertEqual(list(plan.conflict_plan["deferred_intent_ids"]), ["buy"])
        arbitration = plan.conflict_plan["arbitration"][0]
        self.assertEqual(arbitration["symbol"], "600000")
        self.assertEqual(list(arbitration["blocking_intent_ids"]), ["sell"])
        self.assertEqual(list(arbitration["deferred_intent_ids"]), ["buy"])

    def test_b23_free_text_cannot_manufacture_a_canonical_intent_kind(self):
        for text in ("hard_stop touched", "崩盘清仓", "manual sell", "risk_exit",
                     "P0", "buy"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                            "intent_kind_required"):
                    PAP.ResourceIntent("x", "s1", text, "600000")
        with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                    "intent_kind_required"):
            PAP.ResourceIntent("x", "s1", "", "600000")

    def test_duplicate_intent_id_fails_closed(self):
        with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                    "duplicate_explicit_intent_id"):
            _build(intents=(PAP.ResourceIntent("d", "s1", "RISK_EXIT", "600000"),
                            PAP.ResourceIntent("d", "s1", "NEW_ENTRY", "600000")))

    def test_risk_exit_is_never_deferred_behind_an_entry(self):
        plan = _build(intents=(
            PAP.ResourceIntent("a1", "s1", "ADD_POSITION", "600000"),
            PAP.ResourceIntent("n1", "s2", "NEW_ENTRY", "600000"),
            PAP.ResourceIntent("r1", "s2", "RISK_EXIT", "600000")))
        self.assertNotIn("r1", plan.conflict_plan["deferred_intent_ids"])
        self.assertEqual(sorted(plan.conflict_plan["deferred_intent_ids"]), ["a1", "n1"])
        self.assertEqual(plan.conflict_plan["ordered_intents"][0]["intent_id"], "r1")

    # ── business invariants migrated off the deleted text-derived path ──
    def test_migrated_canonical_priority_vocabulary_order(self):
        self.assertEqual(list(PAP.INTENT_KINDS),
                         ["RISK_EXIT", "TAKE_PROFIT_EXIT", "MANUAL_EXIT",
                          "RISK_REDUCE", "NEW_ENTRY", "ADD_POSITION"])

    def test_migrated_risk_exit_precedes_add_position(self):
        plan = _build(intents=(PAP.ResourceIntent("add", "s1", "ADD_POSITION", "600000"),
                               PAP.ResourceIntent("risk", "s2", "RISK_EXIT", "600000")))
        rows = plan.conflict_plan["ordered_intents"]
        self.assertEqual(rows[0]["intent_kind"], "RISK_EXIT")
        self.assertLess(rows[0]["priority"], rows[-1]["priority"])

    def test_migrated_exits_precede_entries_for_any_input_mix(self):
        entries = [PAP.ResourceIntent(f"e{index}", "s1", "NEW_ENTRY", f"00000{index}")
                   for index in range(12)]
        exits = [PAP.ResourceIntent("x1", "s2", "RISK_EXIT", "600000")]
        for order in (entries + exits, exits + entries,
                      entries[:6] + exits + entries[6:]):
            with self.subTest(first=order[0].intent_id):
                plan = _build(intents=tuple(order))
                head = plan.conflict_plan["ordered_intents"][0]
                self.assertEqual(head["intent_kind"], "RISK_EXIT")
                self.assertEqual(head["intent_id"], "x1")

    def test_migrated_entry_is_never_above_exit(self):
        for entry_kind in PAP.ENTRY_INTENT_KINDS:
            with self.subTest(entry=entry_kind):
                self.assertGreater(PAP.INTENT_PRIORITY[entry_kind],
                                   PAP.INTENT_PRIORITY["RISK_EXIT"])
                self.assertGreater(PAP.INTENT_PRIORITY[entry_kind],
                                   PAP.INTENT_PRIORITY["TAKE_PROFIT_EXIT"])

    def test_migrated_risk_exit_always_ranks_first_over_every_exit_kind(self):
        for kind in PAP.INTENT_KINDS[1:]:
            with self.subTest(kind=kind):
                self.assertLess(PAP.INTENT_PRIORITY["RISK_EXIT"], PAP.INTENT_PRIORITY[kind])


class PersistenceTests(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE portfolio_allocation_plans("
                     "plan_id TEXT PRIMARY KEY,plan_fingerprint TEXT NOT NULL,"
                     "plan_json TEXT NOT NULL,created_at TEXT NOT NULL)")
        self.addCleanup(conn.close)
        return conn

    def test_b25_plan_append_is_idempotent(self):
        conn = self._conn()
        plan = _build()
        PAPRepo.append_plan(conn, plan, created_at="1")
        PAPRepo.append_plan(conn, plan, created_at="2")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM portfolio_allocation_plans").fetchone()[0], 1)

    def test_b26_same_plan_id_with_different_content_conflicts(self):
        conn = self._conn()
        plan = _build()
        PAPRepo.append_plan(conn, plan)
        conn.execute("UPDATE portfolio_allocation_plans SET plan_json='{}' WHERE plan_id=?",
                     (plan.plan_id,))
        with self.assertRaisesRegex(PAPRepo.PortfolioAllocationRepositoryError,
                                    "idempotency_conflict"):
            PAPRepo.append_plan(conn, plan)

    def test_b28_repository_exposes_only_append_and_exact_get(self):
        self.assertEqual(set(PAPRepo.__all__),
                         {"PortfolioAllocationRepositoryError", "append_plan", "get_plan"})
        self.assertEqual(_module_functions(PAPRepo.__file__),
                         {"_payload", "append_plan", "get_plan"})
        for name in _attributed_names(PAPRepo.__file__):
            self.assertNotIn("latest", name.lower())
        for literal in _string_literals(PAPRepo.__file__):
            upper = literal.upper()
            for forbidden in ("UPDATE ", "DELETE ", "REPLACE INTO", "DROP "):
                self.assertNotIn(forbidden, upper, msg=f"repository SQL: {literal[:60]}")

    def test_exact_getter_reads_the_named_plan_only(self):
        conn = self._conn()
        first = _build()
        second = _build(snapshot=_snapshot(asof_day="2026-10-01"))
        PAPRepo.append_plan(conn, first, created_at="1")
        PAPRepo.append_plan(conn, second, created_at="2")
        self.assertEqual(PAPRepo.get_plan(conn, first.plan_id).plan_id, first.plan_id)
        self.assertIsNone(PAPRepo.get_plan(conn, "f" * 64))
        with self.assertRaisesRegex(PAPRepo.PortfolioAllocationRepositoryError,
                                    "explicit_plan_id_required"):
            PAPRepo.get_plan(conn, "short")

    def test_corrupt_stored_plan_is_rejected(self):
        conn = self._conn()
        plan = _build()
        PAPRepo.append_plan(conn, plan)
        conn.execute("UPDATE portfolio_allocation_plans SET plan_json=? WHERE plan_id=?",
                     (json.dumps({**plan.projection(), "plan_status": "PLANNED"}),
                      plan.plan_id))
        with self.assertRaisesRegex(PAPRepo.PortfolioAllocationRepositoryError,
                                    "fingerprint_mismatch"):
            PAPRepo.get_plan(conn, plan.plan_id)

    def test_tampered_component_breaks_the_fingerprint(self):
        plan = _build()
        tampered = replace(plan, slot_plan=PAP._freeze({**PAP._thaw(plan.slot_plan),
                                                        "total_slots": 999}))
        self.assertFalse(PAP.verify_plan_fingerprint(tampered))


class ArchitectureGuardTests(unittest.TestCase):
    def test_api_exposes_only_plan_capture_and_exact_get(self):
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(API.portfolio_allocation_router)
        paths = set(app.openapi()["paths"])
        self.assertEqual(paths, {"/api/portfolio/allocation/plans",
                                 "/api/portfolio/allocation/plans/{plan_id}"})
        for forbidden in ("current", "latest", "rebalance", "apply", "execute"):
            for path in paths:
                self.assertNotIn(forbidden, path)

    def test_service_does_not_own_schema(self):
        tree = ast.parse(pathlib.Path(PAS.__file__).read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        self.assertNotIn("paper_schema_migrations", imported)
        source = pathlib.Path(PAS.__file__).read_text(encoding="utf-8")
        self.assertNotIn("ensure_portfolio_allocation_plans", source)
        self.assertNotIn("CREATE TABLE", source)

    def test_policy_does_not_import_the_legacy_coordinator(self):
        source = pathlib.Path(PAP.__file__).read_text(encoding="utf-8")
        self.assertNotIn("portfolio_coordinator", source)
        self.assertNotIn("classify_intent", source)


class LedgerImmutabilityTests(unittest.TestCase):
    def test_b27_evaluation_leaves_the_formal_ledger_untouched(self):
        temp = tempfile.TemporaryDirectory(prefix="r34b-plan-")
        old_db = PT.DB_PATH
        PT.DB_PATH = os.path.join(temp.name, "paper.sqlite3")
        try:
            PT.init_db()
            PT.start_new_cycle(capital=100_000, include_dashboard=True)
            conn = sqlite3.connect(PT.DB_PATH)
            conn.row_factory = sqlite3.Row
            cycle_id = int(conn.execute("SELECT MAX(id) FROM paper_cycles").fetchone()[0])
            attached = conn.execute(
                "SELECT MAX(effective_date) FROM paper_parameter_versions").fetchone()[0]
            conn.close()
            decision_at = f"{attached}T23:00:00+08:00"
            snapshot = PRS.capture_portfolio_runtime_snapshot(
                cycle_id=cycle_id, asof_day=str(attached), decision_at=decision_at)

            conn = sqlite3.connect(PT.DB_PATH)
            before = self._counts(conn)
            conn.close()

            eligible = list(snapshot["execution_participant_ids"])
            self.assertTrue(eligible)
            plan = PAS.capture_portfolio_allocation_plan(
                portfolio_snapshot_id=snapshot["snapshot_id"],
                allocation_weights={account: 1.0 for account in eligible})

            conn = sqlite3.connect(PT.DB_PATH)
            after = self._counts(conn)
            plan_rows = conn.execute(
                "SELECT COUNT(*) FROM portfolio_allocation_plans").fetchone()[0]
            snapshot_rows = conn.execute(
                "SELECT COUNT(*) FROM portfolio_runtime_snapshots").fetchone()[0]
            conn.close()

            changed = {table for table in before if before[table] != after.get(table)}
            self.assertEqual(changed, {"portfolio_allocation_plans"},
                             msg=f"unexpected ledger writes: {changed}")
            self.assertEqual(plan_rows, 1)
            self.assertEqual(snapshot_rows, 1)

            # The honest v1 boundary: slots and conflicts plan, capital and
            # capacity need facts the current capture does not supply.
            self.assertEqual(plan["plan_status"], PAP.PARTIAL)
            self.assertEqual(plan["slot_plan"]["status"], PAP.PLANNED)
            self.assertEqual(plan["capital_plan"]["status"], PAP.INSUFFICIENT_EVIDENCE)
            self.assertEqual(plan["capacity_plan"]["status"], PAP.INSUFFICIENT_EVIDENCE)
            self.assertEqual(plan["concentration_adjustment"]["status"], PAP.UNAVAILABLE)
            self.assertEqual(plan["correlation_term"]["status"], PAP.UNAVAILABLE)
            self.assertEqual(plan["plan_id"], plan["plan_fingerprint"])
            self.assertTrue(plan["slot_plan"]["limits"])
            self.assertLessEqual(sum(plan["slot_plan"]["limits"].values()),
                                 plan["slot_plan"]["hard_pool_cap"])
            for reason in plan["blocking_reasons"]:
                self.assertTrue(reason)

            again = PAS.capture_portfolio_allocation_plan(
                portfolio_snapshot_id=snapshot["snapshot_id"],
                allocation_weights={account: 1.0 for account in eligible})
            self.assertEqual(again["plan_id"], plan["plan_id"])
            self.assertEqual(PAS.get_portfolio_allocation_plan(plan["plan_id"]), plan)

            # An unknown snapshot id must fail closed: never fall back to the
            # newest stored snapshot.
            with self.assertRaisesRegex(PAP.PortfolioAllocationPolicyError,
                                        "portfolio_snapshot_not_found"):
                PAS.capture_portfolio_allocation_plan(
                    portfolio_snapshot_id="f" * 64, allocation_weights={"x": 1.0})
        finally:
            PT.DB_PATH = old_db
            temp.cleanup()

    @staticmethod
    def _counts(conn):
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables}


if __name__ == "__main__":
    unittest.main()
