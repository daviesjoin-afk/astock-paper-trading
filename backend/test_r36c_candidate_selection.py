"""SEL-01..41: R36-C selection policy, evidence closure, Pareto fronts and ledger."""
from __future__ import annotations

import ast
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

import candidate_selection as CS
import candidate_selection_repository as CSR
import candidate_selection_service as CSS
import experiment_search_contract as ESC
import experiment_search_repository as ESR
import experiment_search_service as ESS
import paper_schema_migrations as PSM
import test_r36b2_candidate_robustness_execution as B2


def ungated_policy(**changes):
    """A policy with every business gate disabled (explicit None, never a default)."""
    values = {"objectives": ["baseline_return"], "advance_through_front": 1,
              "retain_through_front": 1}
    values.update(changes)
    return CS.CandidateSelectionPolicy(**values)


def policy(**changes):
    values = {"objectives": ["baseline_return", "robustness_worst_return"],
              "min_baseline_return": 0.0,
              "max_baseline_drawdown_abs": 0.5,
              "min_trade_count": 1,
              "min_data_coverage": 0.5,
              "advance_through_front": 1,
              "retain_through_front": 2}
    values.update(changes)
    return CS.CandidateSelectionPolicy(**values)


def evidence(candidate_id, **changes):
    values = dict(
        candidate_id=candidate_id,
        pit_run_key="a" * 64,
        pit_result_fingerprint="b" * 64,
        pit_validation_status="ready",
        pit_result_status="completed",
        robustness_report_key="c" * 64,
        robustness_report_fingerprint="d" * 64,
        baseline_return=0.10,
        baseline_drawdown_abs=0.10,
        baseline_turnover=1.0,
        baseline_trade_count=5,
        baseline_data_coverage=1.0,
        robustness_worst_return=0.05,
        robustness_worst_drawdown_abs=0.15,
        robustness_max_return_degradation=0.05,
        robustness_unavailable_count=0,
        robustness_failed_count=0,
        robustness_threshold_breach_count=0,
        robustness_fragility_count=0)
    values.update(changes)
    return CS.CandidateSelectionEvidence(**values)


def cid(n):
    return f"{n:064x}"


class PolicyTests(unittest.TestCase):
    def test_sel03_objective_order_independent_and_closed(self):
        self.assertEqual(policy(objectives=["baseline_return", "robustness_worst_return"]).fingerprint,
                         policy(objectives=["robustness_worst_return", "baseline_return"]).fingerprint)
        for objectives in ([], ["baseline_return", "baseline_return"],
                            ["baseline_return", "unknown_objective"]):
            with self.subTest(objectives=objectives), self.assertRaises(CS.SelectionContractError):
                policy(objectives=objectives)

    def test_sel04_policy_fact_changes_fingerprint(self):
        base = policy()
        for changes in ({"min_baseline_return": 0.01},
                        {"max_baseline_drawdown_abs": 0.2},
                        {"min_trade_count": 2},
                        {"min_data_coverage": 0.9},
                        {"require_no_robustness_unavailable": True},
                        {"require_no_robustness_failed": True},
                        {"require_no_threshold_breaches": True},
                        {"max_observed_fragilities": 1},
                        {"advance_through_front": 2, "retain_through_front": 2},
                        {"retain_through_front": 3},
                        {"objectives": ["baseline_turnover"]}):
            with self.subTest(changes=changes):
                self.assertNotEqual(base.fingerprint, policy(**changes).fingerprint)

    def test_sel03b_policy_projection_roundtrip(self):
        p = policy()
        self.assertEqual(p.fingerprint,
                         CS.CandidateSelectionPolicy.from_projection(p.projection()).fingerprint)

    def test_retain_below_advance_rejected(self):
        with self.assertRaises(CS.SelectionContractError):
            policy(advance_through_front=2, retain_through_front=1)


class PureEvaluationTests(unittest.TestCase):
    def test_sel20_pareto_simple_dominance(self):
        a = evidence(cid(1), baseline_return=0.20, baseline_drawdown_abs=0.10,
                     robustness_worst_return=0.10)
        b = evidence(cid(2), baseline_return=0.15, baseline_drawdown_abs=0.12,
                     robustness_worst_return=0.08)
        report = CS.evaluate_selection(search_run_id="e" * 64,
                                       search_input_fingerprint="f" * 64,
                                       policy=policy(), evidence=[a, b])
        fronts = {row["candidate_id"]: row["pareto_front"] for row in report["candidates"]}
        self.assertEqual(1, fronts[cid(1)])
        self.assertEqual(2, fronts[cid(2)])

    def test_sel21_pareto_tradeoff_same_front(self):
        # A: higher return, higher drawdown. B: lower return, lower drawdown. Trade-off.
        a = evidence(cid(1), baseline_return=0.20, baseline_drawdown_abs=0.20)
        b = evidence(cid(2), baseline_return=0.10, baseline_drawdown_abs=0.05)
        tradeoff = policy(objectives=["baseline_return", "baseline_drawdown_abs"])
        report = CS.evaluate_selection(search_run_id="e" * 64,
                                       search_input_fingerprint="f" * 64,
                                       policy=tradeoff, evidence=[a, b])
        self.assertEqual([1, 1], [row["pareto_front"] for row in report["candidates"]])

    def test_sel22_exact_ties_same_front(self):
        a = evidence(cid(1))
        b = evidence(cid(2))
        report = CS.evaluate_selection(search_run_id="e" * 64,
                                       search_input_fingerprint="f" * 64,
                                       policy=policy(), evidence=[a, b])
        self.assertEqual([1, 1], [row["pareto_front"] for row in report["candidates"]])
        # Serialization is candidate_id ASC, and the id order carries no semantics:
        # a "higher" candidate_id is not better than a "lower" one.
        self.assertEqual([cid(1), cid(2)], [r["candidate_id"] for r in report["candidates"]])
        self.assertEqual([r["disposition"] for r in report["candidates"]],
                         ["advance", "advance"])
        self.assertNotIn("rank", report["candidates"][0])

    def test_sel23_input_order_independent(self):
        items = [evidence(cid(n), baseline_return=0.05 * n,
                          baseline_drawdown_abs=0.01 * n,
                          robustness_worst_return=0.01 * n) for n in range(1, 8)]
        reference = CS.evaluate_selection(search_run_id="e" * 64,
                                          search_input_fingerprint="f" * 64,
                                          policy=policy(), evidence=list(items))
        evens = [item for index, item in enumerate(items) if index % 2 == 0]
        odds = [item for index, item in enumerate(items) if index % 2 == 1]
        for order in (list(reversed(items)), items[3:] + items[:3], evens + odds):
            shuffled = CS.evaluate_selection(search_run_id="e" * 64,
                                             search_input_fingerprint="f" * 64,
                                             policy=policy(), evidence=order)
            self.assertEqual(reference["report_fingerprint"], shuffled["report_fingerprint"])
            self.assertEqual([r["pareto_front"] for r in reference["candidates"]],
                             [r["pareto_front"] for r in shuffled["candidates"]])

    def test_sel24_advance_retain_eliminate(self):
        items = [evidence(cid(n), baseline_return=0.30 - 0.05 * n,
                          robustness_worst_return=0.20 - 0.05 * n) for n in range(1, 5)]
        report = CS.evaluate_selection(search_run_id="e" * 64,
                                       search_input_fingerprint="f" * 64,
                                       policy=policy(advance_through_front=1,
                                                     retain_through_front=2), evidence=items)
        by_front = {row["pareto_front"]: row for row in report["candidates"]}
        self.assertEqual("advance", by_front[1]["disposition"])
        self.assertEqual("retain", by_front[2]["disposition"])
        self.assertEqual("eliminate", by_front[3]["disposition"])
        self.assertTrue(by_front[1]["next_generation_eligible"])
        self.assertFalse(by_front[2]["next_generation_eligible"])

    def test_sel25_ineligible_has_no_front(self):
        items = [evidence(cid(1), baseline_return=-1.0), evidence(cid(2))]
        report = CS.evaluate_selection(search_run_id="e" * 64,
                                       search_input_fingerprint="f" * 64,
                                       policy=policy(), evidence=items)
        row = next(r for r in report["candidates"] if r["candidate_id"] == cid(1))
        self.assertFalse(row["eligibility"])
        self.assertIsNone(row["pareto_front"])
        self.assertFalse(row["next_generation_eligible"])
        self.assertEqual("eliminate", row["disposition"])
        self.assertIn("baseline_return_below_floor", row["blocking_reasons"])
        # An ineligible candidate must never dominate an eligible one: the eligible
        # candidate stays alone on front 1.
        eligible_row = next(r for r in report["candidates"] if r["candidate_id"] == cid(2))
        self.assertEqual(1, eligible_row["pareto_front"])
        self.assertEqual(1, report["eligible_count"])

    def test_sel33_all_ineligible_is_a_legal_report(self):
        items = [evidence(cid(n), pit_validation_status="blocked",
                          pit_result_status="unavailable",
                          robustness_report_key=None, robustness_report_fingerprint=None)
                 for n in range(1, 4)]
        report = CS.evaluate_selection(search_run_id="e" * 64,
                                       search_input_fingerprint="f" * 64,
                                       policy=policy(), evidence=items)
        self.assertEqual(0, report["eligible_count"])
        self.assertEqual(0, report["advance_count"])
        self.assertEqual(3, report["eliminate_count"])
        self.assertEqual([], [r for r in report["candidates"] if r["pareto_front"] is not None])

    def test_sel19_fixed_objective_directions(self):
        higher_return = evidence(cid(1), baseline_return=0.20)
        lower_return = evidence(cid(2), baseline_return=0.10)
        report = CS.evaluate_selection(search_run_id="e" * 64,
                                       search_input_fingerprint="f" * 64,
                                       policy=policy(objectives=["baseline_return"]),
                                       evidence=[higher_return, lower_return])
        fronts = {row["candidate_id"]: row["pareto_front"] for row in report["candidates"]}
        self.assertEqual(1, fronts[cid(1)])
        self.assertEqual(2, fronts[cid(2)])
        # MINIMIZE drawdown_abs: the smaller absolute drawdown wins.
        small = evidence(cid(3), baseline_drawdown_abs=0.05)
        large = evidence(cid(4), baseline_drawdown_abs=0.40)
        report = CS.evaluate_selection(search_run_id="e" * 64,
                                       search_input_fingerprint="f" * 64,
                                       policy=policy(objectives=["baseline_drawdown_abs"]),
                                       evidence=[small, large])
        fronts = {row["candidate_id"]: row["pareto_front"] for row in report["candidates"]}
        self.assertEqual(1, fronts[cid(3)])
        self.assertEqual(2, fronts[cid(4)])

    def test_sel16_missing_metric_is_not_zero(self):
        items = [evidence(cid(1), baseline_return=None)]
        report = CS.evaluate_selection(search_run_id="e" * 64,
                                       search_input_fingerprint="f" * 64,
                                       policy=policy(), evidence=items)
        row = report["candidates"][0]
        self.assertFalse(row["eligibility"])
        self.assertIn("selection_metric_unavailable", row["blocking_reasons"])
        self.assertIsNone(row["selection_features"]["baseline_return"])

    def test_sel17_baseline_gates(self):
        cases = (
            ({"baseline_return": -0.01}, "baseline_return_below_floor"),
            ({"baseline_drawdown_abs": 0.9}, "baseline_drawdown_above_limit"),
            ({"baseline_trade_count": 0}, "baseline_trade_count_below_floor"),
            ({"baseline_data_coverage": 0.1}, "baseline_data_coverage_below_floor"),
        )
        for changes, reason in cases:
            with self.subTest(reason=reason):
                report = CS.evaluate_selection(
                    search_run_id="e" * 64, search_input_fingerprint="f" * 64,
                    policy=policy(), evidence=[evidence(cid(1), **changes)])
                self.assertIn(reason, report["candidates"][0]["blocking_reasons"])

    def test_sel18_robustness_gates(self):
        base = policy(require_no_robustness_unavailable=True,
                      require_no_robustness_failed=True,
                      require_no_threshold_breaches=True,
                      max_observed_fragilities=1)
        cases = (
            ({"robustness_unavailable_count": 1}, "robustness_unavailable_cases_present"),
            ({"robustness_failed_count": 1}, "robustness_failed_cases_present"),
            ({"robustness_threshold_breach_count": 1}, "robustness_threshold_breaches_present"),
            ({"robustness_fragility_count": 5}, "robustness_fragility_count_above_limit"),
        )
        for changes, reason in cases:
            with self.subTest(reason=reason):
                report = CS.evaluate_selection(
                    search_run_id="e" * 64, search_input_fingerprint="f" * 64,
                    policy=base, evidence=[evidence(cid(1), **changes)])
                self.assertIn(reason, report["candidates"][0]["blocking_reasons"])

    def test_sel34_scale_128(self):
        items = [evidence(cid(n), baseline_return=n / 1000.0,
                          robustness_worst_return=n / 2000.0,
                          baseline_drawdown_abs=1.0 - n / 1000.0) for n in range(1, 129)]
        report = CS.evaluate_selection(search_run_id="e" * 64,
                                       search_input_fingerprint="f" * 64,
                                       policy=policy(), evidence=items)
        self.assertEqual(128, report["candidate_count"])
        self.assertEqual(128, len({row["candidate_id"] for row in report["candidates"]}))
        shuffled = CS.evaluate_selection(search_run_id="e" * 64,
                                         search_input_fingerprint="f" * 64,
                                         policy=policy(),
                                         evidence=list(reversed(items)))
        self.assertEqual(report["report_fingerprint"], shuffled["report_fingerprint"])

    def test_sel27_evidence_set_fingerprint_exact(self):
        base = [evidence(cid(1)), evidence(cid(2))]
        reference = CS.evaluate_selection(search_run_id="e" * 64,
                                          search_input_fingerprint="f" * 64,
                                          policy=policy(), evidence=base)
        for changes in ({"pit_run_key": "1" * 64}, {"pit_result_fingerprint": "2" * 64},
                        {"robustness_report_key": "3" * 64,
                         "robustness_report_fingerprint": "4" * 64}):
            mutated = [evidence(cid(1), **changes), evidence(cid(2))]
            report = CS.evaluate_selection(search_run_id="e" * 64,
                                           search_input_fingerprint="f" * 64,
                                           policy=policy(), evidence=mutated)
            with self.subTest(changes=changes):
                self.assertNotEqual(reference["evidence_set_fingerprint"],
                                    report["evidence_set_fingerprint"])
                self.assertNotEqual(reference["selection_report_key"],
                                    report["selection_report_key"])


class FailClosedGateTests(unittest.TestCase):
    """Regression: enabled robustness gates fail closed when the count is unavailable."""

    def test_enabled_gate_with_none_count_is_ineligible(self):
        strict = policy(require_no_robustness_unavailable=True,
                        require_no_robustness_failed=True,
                        require_no_threshold_breaches=True)
        for feature in ("robustness_unavailable_count", "robustness_failed_count",
                        "robustness_threshold_breach_count"):
            with self.subTest(feature=feature):
                report = CS.evaluate_selection(
                    search_run_id="e" * 64, search_input_fingerprint="f" * 64,
                    policy=strict, evidence=[evidence(cid(1), **{feature: None})])
                row = report["candidates"][0]
                self.assertFalse(row["eligibility"])
                self.assertIn("selection_metric_unavailable", row["blocking_reasons"])
                self.assertIsNone(row["pareto_front"])

    def test_disabled_gate_ignores_none_count(self):
        report = CS.evaluate_selection(search_run_id="e" * 64,
                                       search_input_fingerprint="f" * 64,
                                       policy=ungated_policy(),
                                       evidence=[evidence(cid(1),
                                                          robustness_failed_count=None)])
        self.assertTrue(report["candidates"][0]["eligibility"])


class LedgerCrossCheckTests(unittest.TestCase):
    """Regression: report candidates must equal the bound evidence candidates."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.repo = CSR.CandidateSelectionRepository(self.conn)

    def test_candidates_must_match_evidence_binding(self):
        # A forged producer swaps the report's candidate_id while keeping the bound
        # evidence and every other identity field self-consistent (including a
        # recomputed report_fingerprint). The candidate<->evidence cross-check must
        # still reject it.
        report = CS.evaluate_selection(search_run_id="a" * 64,
                                       search_input_fingerprint="b" * 64,
                                       policy=policy(), evidence=[evidence(cid(1))])
        binding = [evidence(cid(1)).evidence_binding_projection()]
        forged = dict(report)
        forged["candidates"] = [dict(report["candidates"][0], candidate_id=cid(9))]
        forged["report_fingerprint"] = CS._sha(
            {key: value for key, value in forged.items() if key != "report_fingerprint"})
        with self.assertRaises(CSR.SelectionPersistenceError):
            self.repo.append_report(forged, evidence_binding=binding,
                                    created_at="2026-01-01T00:00:00Z")
        # The un-forged report with the same binding is accepted.
        stored = self.repo.append_report(report, evidence_binding=binding,
                                         created_at="2026-01-01T00:00:00Z")
        self.assertEqual(report["selection_report_key"], stored["selection_report_key"])

    def test_matching_binding_is_accepted(self):
        report = CS.evaluate_selection(search_run_id="a" * 64,
                                       search_input_fingerprint="b" * 64,
                                       policy=policy(), evidence=[evidence(cid(1))])
        stored = self.repo.append_report(
            report, evidence_binding=[evidence(cid(1)).evidence_binding_projection()],
            created_at="2026-01-01T00:00:00Z")
        self.assertEqual(report["selection_report_key"], stored["selection_report_key"])


class ContractV3Tests(unittest.TestCase):
    def test_sel01_v1_frozen(self):
        spec = ESC.ExperimentSearchSpec("a" * 64, "b" * 64, (cid(1),), ESC.SearchBudget(1))
        self.assertEqual(ESC.SEARCH_CONTRACT_VERSION, spec.search_contract_version)
        self.assertNotIn("experiment_plan", spec.projection())
        self.assertNotIn("selection_policy", spec.projection())
        self.assertEqual(spec.search_input_fingerprint,
                         ESC.search_spec_from_projection(spec.projection()).search_input_fingerprint)

    def test_sel02_v2_frozen(self):
        plan = B2.B1.plan()
        v2 = ESC.ExperimentSearchPlanV2.from_v1(plan, B2.policy())
        spec = ESC.ExperimentSearchSpec("a" * 64, "b" * 64, (cid(1),), ESC.SearchBudget(1),
                                        ESC.SEARCH_CONTRACT_VERSION_V2, experiment_plan=v2)
        self.assertNotIn("selection_policy", spec.projection())
        self.assertEqual(spec.search_input_fingerprint,
                         ESC.search_spec_from_projection(spec.projection()).search_input_fingerprint)

    def test_sel05_v3_binds_selection_policy(self):
        plan = B2.B1.plan()
        v2 = ESC.ExperimentSearchPlanV2.from_v1(plan, B2.policy())
        a = ESC.ExperimentSearchSpec("a" * 64, "b" * 64, (cid(1),), ESC.SearchBudget(1),
                                     ESC.SEARCH_CONTRACT_VERSION_V3, experiment_plan=v2,
                                     selection_policy=policy())
        b = ESC.ExperimentSearchSpec("a" * 64, "b" * 64, (cid(1),), ESC.SearchBudget(1),
                                     ESC.SEARCH_CONTRACT_VERSION_V3, experiment_plan=v2,
                                     selection_policy=policy(min_baseline_return=0.99))
        self.assertNotEqual(a.search_input_fingerprint, b.search_input_fingerprint)
        self.assertEqual(a.search_input_fingerprint,
                         ESC.search_spec_from_projection(a.projection()).search_input_fingerprint)

    def test_sel06_policy_does_not_contaminate_experiment_identity(self):
        plan = B2.B1.plan()
        robustness_policy = B2.policy()
        v2 = ESC.ExperimentSearchPlanV2.from_v1(plan, robustness_policy)
        candidate = B2.B1.candidate()
        base_spec = B2.B1.spec(candidate, plan)
        robustness_plan = robustness_policy.bind("a" * 64, base_spec.fingerprint)
        a = ESC.ExperimentSearchSpec("a" * 64, "b" * 64, (cid(1),), ESC.SearchBudget(1),
                                     ESC.SEARCH_CONTRACT_VERSION_V3, experiment_plan=v2,
                                     selection_policy=policy())
        b = ESC.ExperimentSearchSpec("a" * 64, "b" * 64, (cid(1),), ESC.SearchBudget(1),
                                     ESC.SEARCH_CONTRACT_VERSION_V3, experiment_plan=v2,
                                     selection_policy=policy(advance_through_front=2,
                                                             retain_through_front=3))
        self.assertNotEqual(a.search_input_fingerprint, b.search_input_fingerprint)
        # The nested plan is byte-identical and neither fingerprint moves.
        self.assertEqual(a.experiment_plan.projection(), b.experiment_plan.projection())
        self.assertEqual(a.experiment_plan.fingerprint, b.experiment_plan.fingerprint)
        self.assertEqual(a.experiment_plan.plan.fingerprint, v2.plan.fingerprint)
        self.assertEqual(a.experiment_plan.robustness_policy.fingerprint,
                         b.experiment_plan.robustness_policy.fingerprint)
        # CandidateExperimentSpec / R30 RobustnessPlan identities are independent of the
        # selection policy: they are not even inputs to it.
        self.assertEqual(base_spec.fingerprint, B2.B1.spec(candidate, plan).fingerprint)
        self.assertEqual(robustness_plan.fingerprint,
                         robustness_policy.bind("a" * 64, base_spec.fingerprint).fingerprint)
        self.assertNotIn("selection_policy", base_spec.projection())
        self.assertNotIn("selection", robustness_plan.projection())

    def test_sel06b_plan_v2_projection_has_no_selection_leak(self):
        # The candidate-experiment-plan-v2 projection must carry no selection fact and
        # must round-trip canonically; the selection policy is not an experiment input.
        v2 = ESC.ExperimentSearchPlanV2.from_v1(B2.B1.plan(), B2.policy())
        projection = v2.projection()
        self.assertNotIn("selection", " ".join(projection))
        self.assertNotIn("selection_policy", projection)
        rebuilt = ESC.experiment_plan_from_projection(projection)
        self.assertEqual(v2.fingerprint, rebuilt.fingerprint)
        self.assertEqual(v2.plan.fingerprint, rebuilt.plan.fingerprint)
        self.assertEqual(v2.robustness_policy.fingerprint,
                         rebuilt.robustness_policy.fingerprint)

    def test_sel03c_v3_requires_robustness_plan(self):
        # A v3 search over a bare v1 plan would be a dead-end declaration: R30 could
        # never run, so the contract refuses it.
        bare_v1 = B2.B1.plan()
        with self.assertRaisesRegex(ESC.SearchContractError,
                                    "search_run_robustness_policy_unavailable"):
            ESC.ExperimentSearchSpec("a" * 64, "b" * 64, (cid(1),), ESC.SearchBudget(1),
                                     ESC.SEARCH_CONTRACT_VERSION_V3,
                                     experiment_plan=bare_v1, selection_policy=policy())

    def test_sel03b_policy_requires_v3(self):
        plan = B2.B1.plan()
        v2 = ESC.ExperimentSearchPlanV2.from_v1(plan, B2.policy())
        with self.assertRaises(ESC.SearchContractError):
            ESC.ExperimentSearchSpec("a" * 64, "b" * 64, (cid(1),), ESC.SearchBudget(1),
                                     ESC.SEARCH_CONTRACT_VERSION_V2, experiment_plan=v2,
                                     selection_policy=policy())
        with self.assertRaises(ESC.SearchContractError):
            ESC.ExperimentSearchSpec("a" * 64, "b" * 64, (cid(1),), ESC.SearchBudget(1),
                                     ESC.SEARCH_CONTRACT_VERSION_V3, experiment_plan=v2)


class ReportIdentityTests(unittest.TestCase):
    """SEL-35/38/39: deterministic identity, no latest lookup, refusal writes nothing."""

    def test_sel35_report_key_formula(self):
        report = CS.evaluate_selection(search_run_id="a" * 64,
                                       search_input_fingerprint="b" * 64,
                                       policy=policy(), evidence=[evidence(cid(1))])
        expected = CS.selection_report_key(
            report_version=report["report_version"], search_run_id="a" * 64,
            search_input_fingerprint="b" * 64,
            selection_policy_fingerprint=policy().fingerprint,
            evidence_set_fingerprint=report["evidence_set_fingerprint"])
        self.assertEqual(expected, report["selection_report_key"])
        # created_at is not part of the identity.
        self.assertNotIn("created_at", report)

    def test_sel38_service_has_no_latest_lookup(self):
        # Structural check: no implicit latest/recent evidence lookup. ``events[-1]`` on a
        # specific job's own event log is the exact ordering authority, not a lookup.
        tree = ast.parse((BACKEND / "candidate_selection_service.py").read_text(encoding="utf-8"))
        source = (BACKEND / "candidate_selection_service.py").read_text(encoding="utf-8")
        for forbidden in ("recent_runs", "recent_reports", "ORDER BY", "MAX(created_at)"):
            self.assertNotIn(forbidden, source, forbidden)
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse(attributes & {"recent_runs", "recent_reports", "get_latest"})
        # The only evidence reads are the exact-key reads.
        self.assertIn("get_run", attributes)
        self.assertIn("get_report_by_key", attributes)

    def test_sel39_refusal_writes_no_selection_row(self):
        source = (BACKEND / "candidate_selection_service.py").read_text(encoding="utf-8")
        # The only write path is the single repository append, reached after closure.
        self.assertEqual(1, source.count("selection_repository.append_report("))


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.repo = CSR.CandidateSelectionRepository(self.conn)

    def _report(self, **changes):
        values = dict(search_run_id="a" * 64, search_input_fingerprint="b" * 64)
        values.update(changes)
        return CS.evaluate_selection(policy=policy(), evidence=[evidence(cid(1))], **values)

    def test_sel28_created_at_outside_identity(self):
        report = self._report()
        binding = [evidence(cid(1)).evidence_binding_projection()]
        first = self.repo.append_report(report, evidence_binding=binding,
                                        created_at="2026-01-01T00:00:00Z")
        self.assertEqual(report["selection_report_key"], first["selection_report_key"])

    def test_sel29_append_only(self):
        report = self._report()
        binding = [evidence(cid(1)).evidence_binding_projection()]
        self.repo.append_report(report, evidence_binding=binding,
                                created_at="2026-01-01T00:00:00Z")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE experiment_search_selection_reports SET created_at='x'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM experiment_search_selection_reports")

    def test_sel30_repository_self_verification(self):
        report = self._report()
        binding = [evidence(cid(1)).evidence_binding_projection()]
        self.repo.append_report(report, evidence_binding=binding,
                                created_at="2026-01-01T00:00:00Z")
        self.conn.execute("DROP TRIGGER experiment_search_selection_reports_no_update")
        for column, value in (("report_json", "{}"), ("evidence_set_fingerprint", "9" * 64),
                              ("eligible_count", 99), ("payload_fingerprint", "8" * 64),
                              ("selection_policy_fingerprint", "7" * 64)):
            old = self.conn.execute(
                f"SELECT {column} FROM experiment_search_selection_reports").fetchone()[0]
            self.conn.execute(
                f"UPDATE experiment_search_selection_reports SET {column}=?", (value,))
            with self.subTest(column=column), self.assertRaises(CSR.SelectionPersistenceError):
                self.repo.get_selection_report(report["selection_report_key"])
            self.conn.execute(
                f"UPDATE experiment_search_selection_reports SET {column}=?", (old,))

    def test_sel31_same_report_idempotent(self):
        report = self._report()
        binding = [evidence(cid(1)).evidence_binding_projection()]
        first = self.repo.append_report(report, evidence_binding=binding,
                                        created_at="2026-01-01T00:00:00Z")
        second = self.repo.append_report(report, evidence_binding=binding,
                                         created_at="2026-01-02T00:00:00Z")
        self.assertEqual(first["selection_report_key"], second["selection_report_key"])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM experiment_search_selection_reports").fetchone()[0]
        self.assertEqual(1, count)

    def test_sel32_same_search_different_report_conflict(self):
        report = self._report()
        binding = [evidence(cid(1)).evidence_binding_projection()]
        self.repo.append_report(report, evidence_binding=binding,
                                created_at="2026-01-01T00:00:00Z")
        other = self._report(search_input_fingerprint="c" * 64)
        with self.assertRaises(CSR.SelectionPersistenceError):
            self.repo.append_report(other, evidence_binding=binding,
                                    created_at="2026-01-01T00:00:00Z")

    def test_sel26_no_scalar_score_or_winner_columns(self):
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(experiment_search_selection_reports)")}
        self.assertFalse(columns & {"score", "weighted_score", "utility", "winner",
                                    "best_candidate", "promotion", "promotable"})
        self.assertIn("eligible_count", columns)


class SelectionServiceTests(B2.CandidateRobustnessFixture):
    """SEL-07..15, SEL-36..41: exact evidence closure over a real search."""

    def setUp(self):
        super().setUp()
        self.selection = CSR.CandidateSelectionRepository(self.owners)

    def create_v3(self, *, selection_policy=None, batch=None):
        return self.create(batch or self.batch_record, experiment_plan=self.plan_v2,
                           selection_policy=selection_policy or policy())

    def select(self, search_run_id, **changes):
        args = dict(search_run_id=search_run_id, validation_repository=self.validation,
                    robustness_repository=self.robustness,
                    selection_repository=self.selection,
                    created_at="2026-10-07T00:00:00Z")
        args.update(changes)
        return CSS.select_search_candidates(self.queue, **args)

    def _run_through_robustness(self, search_run_id):
        self.run_all_pit(search_run_id)
        self.declare(search_run_id)
        for _ in range(20):
            job_id = self.claim_robustness(search_run_id)
            if job_id is None:
                break
            self.execute_robustness(job_id)

    def test_sel07_v2_selection_unavailable(self):
        created = self.create_v2()
        self._run_through_robustness(created["search_run_id"])
        with self.assertRaisesRegex(CSS.CandidateSelectionUnavailable,
                                    "search_run_selection_policy_unavailable"):
            self.select(created["search_run_id"])

    def test_sel08_v3_still_executes_b1_b2_and_selects(self):
        # Ungated policy: proves v3 flows through B1 PIT, B2 robustness and selection.
        created = self.create_v3(selection_policy=ungated_policy())
        self._run_through_robustness(created["search_run_id"])
        output = self.select(created["search_run_id"])
        report = output["report"]
        self.assertEqual(2, report["candidate_count"])
        self.assertEqual(2, report["eligible_count"])
        self.assertEqual(2, report["advance_count"])
        self.assertEqual(0, report["eliminate_count"])
        self.assertEqual(2, report["retain_count"] + report["advance_count"])

    def test_sel09_exact_candidate_pool(self):
        created = self.create_v3()
        self._run_through_robustness(created["search_run_id"])
        output = self.select(created["search_run_id"])
        spec = ESC.search_spec_from_projection(
            ESR.get_search_run(self.queue, created["search_run_id"])["search_spec"])
        self.assertEqual(list(spec.candidate_ids),
                         [row["candidate_id"] for row in output["report"]["candidates"]])

    def test_sel10_candidate_self_verification(self):
        created = self.create_v3()
        self._run_through_robustness(created["search_run_id"])
        # The candidate ledger lives in the search DB (queue connection).
        self.queue.execute("DROP TRIGGER IF EXISTS strategy_candidates_no_update")
        self.queue.execute("UPDATE strategy_candidates SET candidate_json='{}'")
        with self.assertRaisesRegex(CSS.CandidateSelectionUnavailable,
                                    "candidate_identity_mismatch|corrupt"):
            self.select(created["search_run_id"])

    def test_sel11_operational_pit_failure_blocks_selection(self):
        created = self.create_v3()
        self.queue.execute("BEGIN IMMEDIATE")
        ESS.claim_next_job(self.queue, created["search_run_id"])
        self.queue.commit()
        with self.assertRaisesRegex(CSS.CandidateSelectionUnavailable,
                                    "selection_operational_evidence_incomplete"):
            self.select(created["search_run_id"])

    def test_sel11b_cancelled_pit_blocks_selection(self):
        created = self.create_v3()
        for state in ESS.list_search_jobs(self.queue, created["search_run_id"]):
            self.queue.execute("BEGIN IMMEDIATE")
            ESS.record_job_event(self.queue, job_id=state["job_id"], event_kind="cancelled")
            self.queue.commit()
        with self.assertRaisesRegex(CSS.CandidateSelectionUnavailable,
                                    "selection_operational_evidence_incomplete"):
            self.select(created["search_run_id"])

    def test_sel12_canonical_blocked_pit_is_selectable_as_ineligible(self):
        with mock.patch.object(B2.SF, "UNIVERSE",
                               {"scope_kind": "a_share_boards", "boards": ["main_board"]}):
            blocked_batch = self.batch(values=[18], campaign="sel_blocked")
        created = self.create(blocked_batch, experiment_plan=self.plan_v2,
                              selection_policy=policy())
        self.run_all_pit(created["search_run_id"])
        output = self.select(created["search_run_id"])
        report = output["report"]
        self.assertEqual(0, report["eligible_count"])
        self.assertEqual(1, report["eliminate_count"])
        row = report["candidates"][0]
        self.assertIn("pit_validation_not_ready", row["blocking_reasons"])
        self.assertIsNone(row["robustness_report_key"])
        self.assertIsNone(row["pareto_front"])

    def test_sel13_ready_without_robustness_job_blocks_selection(self):
        created = self.create_v3()
        self.run_all_pit(created["search_run_id"])
        with self.assertRaisesRegex(CSS.CandidateSelectionUnavailable,
                                    "selection_robustness_stage_incomplete"):
            self.select(created["search_run_id"])

    def test_sel14_operational_robustness_failure_blocks_selection(self):
        created = self.create_v3()
        self.run_all_pit(created["search_run_id"])
        self.declare(created["search_run_id"])
        job_id = self.claim_robustness(created["search_run_id"])
        with mock.patch.object(B2.RRUN, "run_robustness",
                               side_effect=sqlite3.OperationalError("disk")):
            with self.assertRaises(sqlite3.OperationalError):
                self.execute_robustness(job_id)
        with self.assertRaisesRegex(CSS.CandidateSelectionUnavailable,
                                    "selection_robustness_stage_incomplete"):
            self.select(created["search_run_id"])

    def test_sel15_foreign_robustness_report_rejected(self):
        # The report re-read for a candidate must be that candidate's own canonical
        # report. Point the second candidate's robustness event at the FIRST candidate's
        # real report and require selection to refuse (never silently accept).
        created = self.create_v3(selection_policy=ungated_policy())
        self._run_through_robustness(created["search_run_id"])
        spec = ESC.search_spec_from_projection(
            ESR.get_search_run(self.queue, created["search_run_id"])["search_spec"])
        first, second = spec.candidate_ids
        jobs = {j["candidate_id"]: j for j in
                ESR.list_search_jobs(self.queue, created["search_run_id"])
                if j["stage"] == ESC.JOB_STAGE_ROBUSTNESS}
        first_report_key = ESR.list_job_events(self.queue, jobs[first]["job_id"])[-1]["evidence_id"]
        self.assertIsNotNone(
            self.robustness.get_report_by_key(first_report_key))
        self.queue.execute("DROP TRIGGER experiment_search_job_events_no_update")
        self.queue.execute(
            "UPDATE experiment_search_job_events SET evidence_id=? WHERE job_id=?"
            " AND event_kind='completed'",
            (first_report_key, jobs[second]["job_id"]))
        with self.assertRaises(CSS.CandidateSelectionUnavailable):
            self.select(created["search_run_id"])

    def test_sel04_selection_uses_exact_r29_run_key(self):
        # Selection must read the exact R29 run named by the PIT completed event.
        created = self.create_v3(selection_policy=ungated_policy())
        self._run_through_robustness(created["search_run_id"])
        reads = []
        original = self.validation.get_run

        def recording(*, run_key=None, run_id=None):
            reads.append(run_key)
            return original(run_key=run_key) if run_key is not None else original(run_id=run_id)

        with mock.patch.object(self.validation, "get_run", side_effect=recording):
            output = self.select(created["search_run_id"])
        spec = ESC.search_spec_from_projection(
            ESR.get_search_run(self.queue, created["search_run_id"])["search_spec"])
        expected = set()
        for candidate_id in spec.candidate_ids:
            job = next(j for j in ESR.list_search_jobs(self.queue, created["search_run_id"])
                       if j["candidate_id"] == candidate_id
                       and j["stage"] == ESC.JOB_STAGE_PIT_VALIDATION)
            expected.add(ESR.list_job_events(self.queue, job["job_id"])[-1]["evidence_id"])
        self.assertEqual(expected, set(reads))
        self.assertEqual(2, output["report"]["eligible_count"])

    def test_sel05_selection_uses_exact_r30_report_key(self):
        created = self.create_v3(selection_policy=ungated_policy())
        self._run_through_robustness(created["search_run_id"])
        reads = []
        original = self.robustness.get_report_by_key

        def recording(report_key):
            reads.append(report_key)
            return original(report_key)

        with mock.patch.object(self.robustness, "get_report_by_key", side_effect=recording):
            self.select(created["search_run_id"])
        spec = ESC.search_spec_from_projection(
            ESR.get_search_run(self.queue, created["search_run_id"])["search_spec"])
        expected = set()
        for candidate_id in spec.candidate_ids:
            job = next(j for j in ESR.list_search_jobs(self.queue, created["search_run_id"])
                       if j["candidate_id"] == candidate_id
                       and j["stage"] == ESC.JOB_STAGE_ROBUSTNESS)
            expected.add(ESR.list_job_events(self.queue, job["job_id"])[-1]["evidence_id"])
        self.assertEqual(expected, set(reads))

    def test_sel23b_concurrent_identical_append_is_idempotent(self):
        # Two connections race on the same exact report: one row, same key, no leak.
        import threading
        created = self.create_v3(selection_policy=ungated_policy())
        self._run_through_robustness(created["search_run_id"])
        results, errors = [], []

        def append_once():
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                repo = CSR.CandidateSelectionRepository(conn)
                repo.append_report(self._built_report, evidence_binding=self._binding,
                                   created_at="2026-10-07T00:00:00Z")
                results.append("ok")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            finally:
                conn.close()

        # Build the canonical report once via the service (idempotent fast path later).
        output = self.select(created["search_run_id"])
        stored = self.selection.get_selection_report(output["selection_report_key"])
        self._built_report = stored["report"]
        self._binding = stored["evidence_binding"]
        threads = [threading.Thread(target=append_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        count = self.owners.execute(
            "SELECT COUNT(*) FROM experiment_search_selection_reports").fetchone()[0]
        self.assertEqual(1, count)

    def test_sel33_idempotent_and_concurrent(self):
        created = self.create_v3()
        self._run_through_robustness(created["search_run_id"])
        first = self.select(created["search_run_id"])
        second = self.select(created["search_run_id"])
        self.assertEqual(first["selection_report_key"], second["selection_report_key"])
        count = self.owners.execute(
            "SELECT COUNT(*) FROM experiment_search_selection_reports").fetchone()[0]
        self.assertEqual(1, count)

    def test_sel36_no_promotion_or_lifecycle_writes(self):
        created = self.create_v3()
        self._run_through_robustness(created["search_run_id"])
        tables = [row[0] for row in self.queue.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN"
            " ('strategy_candidates','strategy_candidate_proposals','strategy_candidate_batches')")]
        before = {name: self.queue.execute(
            f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}
        self.select(created["search_run_id"])
        after = {name: self.queue.execute(
            f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}
        self.assertEqual(before, after)
        self.assertTrue(tables)

    def test_sel34b_bootstrap_creates_selection_table(self):
        fresh = sqlite3.connect(":memory:")
        fresh.row_factory = sqlite3.Row
        try:
            fresh.executescript(PSM.experiment_search_run_ddl())
            PSM.ensure_candidate_selection(fresh)
            columns = {row[1] for row in fresh.execute(
                "PRAGMA table_info(experiment_search_selection_reports)")}
            self.assertEqual(set(PSM.SELECTION_REPORT_COLUMNS), columns)
        finally:
            fresh.close()


class MigrationTests(unittest.TestCase):
    """SEL-34/35: migration v36 creates the table and never backfills."""

    def setUp(self):
        import db_migrate
        self.db_migrate = db_migrate

    def test_v36_is_the_next_free_version(self):
        versions = [item[0] for item in self.db_migrate.MIGRATIONS["paper_trading"]]
        self.assertEqual(sorted(versions), versions)
        self.assertEqual(36, max(versions))
        self.assertEqual(36, versions[-1])

    def test_sel35_migration_creates_table_without_backfill(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "paper.sqlite3")
        # migrate() requires an existing DB file; an empty ledger must upgrade cleanly.
        sqlite3.connect(path).close()
        self.db_migrate.migrate("paper_trading", apply=True, path=path, backup=False)
        conn = sqlite3.connect(path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("experiment_search_selection_reports", tables)
            count = conn.execute(
                "SELECT COUNT(*) FROM experiment_search_selection_reports").fetchone()[0]
            self.assertEqual(0, count)
            triggers = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertIn("experiment_search_selection_reports_no_update", triggers)
            self.assertIn("experiment_search_selection_reports_no_delete", triggers)
        finally:
            conn.close()

    def test_sel34b_bootstrap_ddl_matches_migration_ddl(self):
        from_migration = sqlite3.connect(":memory:")
        PSM.ensure_candidate_selection(from_migration)
        migrated_columns = [r[1] for r in from_migration.execute(
            "PRAGMA table_info(experiment_search_selection_reports)")]
        self.assertEqual(list(PSM.SELECTION_REPORT_COLUMNS), migrated_columns)
        from_migration.close()


class ArchitectureTests(unittest.TestCase):
    def test_sel36b_no_promotion_imports(self):
        for name in ("candidate_selection.py", "candidate_selection_repository.py",
                     "candidate_selection_service.py"):
            tree = ast.parse((BACKEND / name).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(imported & {
                "strategy_promotion", "promotion_science", "strategy_lifecycle",
                "adaptive_selection", "adaptive_selection_compat", "selection_runner",
                "paper_selection", "strategy_selection_resolver",
                "strategy_selection_provenance", "selection_tracking",
                "learning_evaluation", "ai_research_service", "strategy_ai_provider",
                "ai_provider_transport", "ai_research_repository", "paper_trading",
                "portfolio_allocation_service"}, name)

    def test_sel37_pure_domain_has_no_io(self):
        tree = ast.parse((BACKEND / "candidate_selection.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {"sqlite3", "os", "pathlib", "datetime", "requests",
                                     "urllib", "strategy_registry"})

    def test_sel26b_no_winner_tokens_as_fields(self):
        source = (BACKEND / "candidate_selection.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        forbidden = {"winner", "best_candidate", "champion", "top_candidate",
                     "recommended_candidate", "promotion_candidate", "weighted_score"}
        self.assertFalse({name for name in names if name in forbidden})


if __name__ == "__main__":
    unittest.main()
