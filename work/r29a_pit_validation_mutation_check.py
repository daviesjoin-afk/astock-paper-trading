#!/usr/bin/env python3
"""In-memory semantic mutation matrix for the R29-A PIT validation gate."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
MODULE_PATH = os.path.join(BACKEND, "experiment_pit_validation.py")
sys.path.insert(0, BACKEND)
import test_experiment_pit_validation as TESTS  # noqa: E402


def _sha() -> str:
    with open(MODULE_PATH, "rb") as source_file:
        return hashlib.sha256(source_file.read()).hexdigest()


def _load(source: str, name: str):
    module_name = f"_r29_mutant_{name.lower().replace('-', '_')}"
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    exec(compile(source, MODULE_PATH, "exec"), module.__dict__)
    return module


def _unit_probe(module, test_name: str) -> bool:
    original = TESTS.PV
    TESTS.PV = module
    try:
        case = TESTS.PITValidationTests(test_name)
        result = unittest.TestResult()
        case.run(result)
        return not result.errors and not result.failures
    finally:
        TESTS.PV = original


def _static_probe(source: str, requirement: str) -> bool:
    tree = ast.parse(source)
    if requirement == "no-network":
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        return "data_fetcher" not in imported and "refresh_snapshot" not in calls
    if requirement == "no-latest":
        return "read_manifest" not in source and "max(created_at)" not in source.lower()
    if requirement == "no-current-head":
        return "get_version" not in source
    if requirement == "no-runtime-execution":
        return "execution_profiles" not in source and "paper_trading_rules" not in source
    if requirement == "no-backtest":
        return "backtest" not in source
    if requirement == "no-calendar-purge":
        return "timedelta(" not in source and "build_walk_forward_folds" in source
    if requirement == "no-clock":
        return not any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"now", "today", "utcnow"}
            for node in ast.walk(tree)
        )
    if requirement == "no-promotion":
        return not any(word in source.lower() for word in ("approved", "promotable", "champion"))
    raise AssertionError(requirement)


def _all_future_probe(module) -> bool:
    original = TESTS.PV
    TESTS.PV = module
    try:
        samples = TESTS._samples()[:4]
        import walk_forward_validation as WFV
        samples[-1] = WFV.ValidationSample(
            sample_key="future-oos", code="600000", decision_session="2026-01-04",
            label_available_at="2026-04-01T15:00:00+08:00", target=0.1,
            pit_status=WFV.PIT_VERIFIED,
        )
        report = TESTS._evaluate(
            samples=samples, authoritative_sessions=TESTS._sessions()[:4],
            session_calendar_provenance=TESTS._calendar_provenance(TESTS._sessions()[:4]),
        )
        return report.dimensions["walk_forward"]["status"] == "blocked"
    finally:
        TESTS.PV = original


def _mutations():
    return [
        ("M-R29-01", "current universe source rejected",
         lambda s: s.replace('row_list, session, source=source, drop_unproven=True,', 'row_list, session, source={"kind": "historical_archive", "historical_membership_complete": True, "historical_membership_asof": "2026-03-31", "source": "mutant"}, drop_unproven=True,', 1),
         lambda m, s: _unit_probe(m, "test_R29_04_current_universe_source_cannot_pass_historical_completeness")),
        ("M-R29-02", "universe fingerprint is not provenance",
         lambda s: s.replace('memberships_complete = bool(sessions) and len(members_by_session) == len(sessions)', 'memberships_complete = bool(spec.universe_fingerprint)', 1),
         lambda m, s: _unit_probe(m, "test_R29_03_universe_sha_alone_does_not_prove_history")),
        ("M-R29-03", "missing tradability facts block",
         lambda s: s.replace('unknown == 0', 'unknown >= 0', 1),
         lambda m, s: _unit_probe(m, "test_R29_06_missing_tradability_evidence_blocks")),
        ("M-R29-04", "unknown ST is not false",
         lambda s: s.replace('"is_listed", "is_st", "is_suspended", "has_market_quote",\n                "has_trade_volume", "is_price_limit_locked",', '"is_listed", "is_suspended", "has_market_quote",\n                "has_trade_volume", "is_price_limit_locked",', 1).replace('decision.buy_block_reason == TA.TradabilityReason.UNKNOWN_STATE', 'False', 1).replace('decision.sell_block_reason == TA.TradabilityReason.UNKNOWN_STATE', 'False', 1),
         lambda m, s: _unit_probe(m, "test_R29_07_unknown_st_is_not_non_st")),
        ("M-R29-05", "current market snapshot cannot prove history",
         lambda s: s.replace('"blocked", "historical_market_data_unavailable",', '"proven", None,', 1),
         lambda m, s: _unit_probe(m, "test_R29_08_current_market_snapshot_cannot_prove_historical_pit")),
        ("M-R29-06", "market gap cannot trigger network refill",
         lambda s: s.replace('\n\n_DIMENSIONS = (', '\n\nimport data_fetcher\n\n_DIMENSIONS = (', 1),
         lambda m, s: _static_probe(s, "no-network")),
        ("M-R29-07", "report period is not publication time",
         lambda s: s.replace('view = FPIT.financial_visibility(record, decision_asof)', 'view = FPIT.financial_visibility({**record, "published_at": record.get("report_period")}, decision_asof)', 1),
         lambda m, s: _unit_probe(m, "test_R29_10_report_period_cannot_substitute_publication_time")),
        ("M-R29-08", "future publication is invisible",
         lambda s: s.replace('elif source == "future":\n                counts["future"] += 1', 'elif source == "future":\n                counts["visible"] += 1', 1),
         lambda m, s: _unit_probe(m, "test_R29_11_future_publication_is_invisible")),
        ("M-R29-09", "dataset manifest must match exact fingerprint",
         lambda s: s.replace('manifest.get("dataset_fingerprint") == spec.dataset_fingerprint', 'True', 1),
         lambda m, s: _unit_probe(m, "test_R29_13_dataset_fingerprint_mismatch_blocks")),
        ("M-R29-10", "latest dataset fallback forbidden",
         lambda s: s.replace('def _manifest_dimension(spec: EC.ExperimentSpec, manifest: Any) -> dict:', 'def _latest_manifest_fallback(conn):\n    return LD.read_manifest(conn, "latest")\n\ndef _manifest_dimension(spec: EC.ExperimentSpec, manifest: Any) -> dict:', 1),
         lambda m, s: _static_probe(s, "no-latest")),
        ("M-R29-11", "current strategy head forbidden",
         lambda s: s.replace('identity = spec.strategy.projection()', 'identity = SR.get_version(spec.strategy.strategy_id) or spec.strategy.projection()', 1),
         lambda m, s: _static_probe(s, "no-current-head")),
        ("M-R29-12", "execution assumptions are spec-only",
         lambda s: s.replace('\n\n_DIMENSIONS = (', '\n\nimport execution_profiles\n\n_DIMENSIONS = (', 1),
         lambda m, s: _static_probe(s, "no-runtime-execution")),
        ("M-R29-13", "cost constants are not runner inputs",
         lambda s: s.replace('\n\n_DIMENSIONS = (', '\n\nimport backtest\n\n_DIMENSIONS = (', 1),
         lambda m, s: _static_probe(s, "no-backtest")),
        ("M-R29-14", "canonical split requires explicit sessions",
         lambda s: s.replace('_bounded_sessions(\n        authoritative_sessions,', '_bounded_sessions(\n        None,', 1),
         lambda m, s: _unit_probe(m, "test_R29_17_walk_forward_requires_explicit_sessions")),
        ("M-R29-15", "not-matured OOS cannot be ready",
         lambda s: s.replace('and walk["ready_folds"] > 0 and samples_invalid == 0', 'and walk["ready_folds"] >= 0 and samples_invalid == 0', 1),
         lambda m, s: _all_future_probe(m)),
        ("M-R29-16", "purge stays in session/label owner",
         lambda s: s.replace('def _normalize_samples(\n', 'def _calendar_day_purge():\n    return dt.timedelta(days=5)\n\ndef _normalize_samples(\n', 1),
         lambda m, s: _static_probe(s, "no-calendar-purge")),
        ("M-R29-17", "any blocked required dimension blocks the report",
         lambda s: s.replace('ready = all_proven and walk.get("ready_folds", 0) >= 1 and not reasons', 'ready = walk.get("ready_folds", 0) >= 1', 1),
         lambda m, s: _unit_probe(m, "test_R29_20_blocked_required_dimension_blocks_whole_validation")),
        ("M-R29-18", "unknown denominator remains null",
         lambda s: s.replace('if denominator <= 0:\n        return None', 'if denominator <= 0:\n        return 0.0', 1),
         lambda m, s: m._ratio(0, 0) is None),
        ("M-R29-19", "blocked input has no zero performance result",
         lambda s: s.replace('"status": self.status,', '"metrics": {"return": 0},\n            "status": self.status,', 1),
         lambda m, s: _unit_probe(m, "test_R29_21_blocked_evidence_has_no_zero_performance_metrics")),
        ("M-R29-20", "evaluation uses explicit as-of only",
         lambda s: s.replace('cutoff = spec.asof_policy.get("cutoff")', 'cutoff = spec.asof_policy.get("cutoff") or dt.datetime.now().isoformat()', 1),
         lambda m, s: _static_probe(s, "no-clock")),
        ("M-R29-21", "report has no approval or promotion status",
         lambda s: s.replace('if self.status not in {"ready", "blocked"}:', 'if self.status not in {"ready", "blocked", "approved"}:', 1),
         lambda m, s: _static_probe(s, "no-promotion")),
        ("M-R29-22", "universe membership checked per requested session",
         lambda s: s.replace('for session in sessions:', 'for session in sessions[-1:]:', 1),
         lambda m, s: _unit_probe(m, "test_R29_26_universe_and_tradability_follow_each_historical_session")),
        ("M-R29-23", "tradability coverage buckets are mutually exclusive",
         lambda s: s.replace('elif not decision.can_buy and not decision.can_sell:\n                blocked += 1', 'elif not decision.can_buy and not decision.can_sell:\n                available += 1\n                blocked += 1', 1),
         lambda m, s: _unit_probe(m, "test_R29_27_complete_suspension_facts_are_blocked_not_unknown")),
        ("M-R29-24", "walk-forward session calendar is bounded by experiment range",
         lambda s: s.replace('elif day < start or day > end:', 'elif False:', 1),
         lambda m, s: _unit_probe(m, "test_R29_28_walk_forward_sessions_and_samples_stay_inside_spec_range")),
        ("M-R29-25", "walk-forward samples are bounded by experiment range",
         lambda s: s.replace('if day < start or day > end:\n            outside_range += 1', 'if False:\n            outside_range += 1', 1),
         lambda m, s: _unit_probe(m, "test_R29_28_walk_forward_sessions_and_samples_stay_inside_spec_range")),
        ("M-R29-26", "session parser rejects trailing non-date content",
         lambda s: s.replace('text = str(value or "").strip().replace("/", "-")', 'text = str(value or "").strip()[:10].replace("/", "-")', 1),
         lambda m, s: _unit_probe(m, "test_R29_29_invalid_session_or_sample_date_blocks")),
        ("M-R29-27", "calendar requires explicit full-range completeness proof",
         lambda s: s.replace('and source.get("range_complete") is True', 'and True', 1),
         lambda m, s: _unit_probe(m, "test_R29_30_short_calendar_without_full_range_provenance_blocks")),
        ("M-R29-28", "financial visibility uses linked decision session",
         lambda s: s.replace('decision_asof = _session_text(sample.decision_session)', 'decision_asof = spec.asof_policy["cutoff"]', 1),
         lambda m, s: _unit_probe(m, "test_R29_31_financial_observations_use_linked_decision_session")),
    ]


def main() -> int:
    with open(MODULE_PATH, encoding="utf-8") as source_file:
        original = source_file.read()
    before = _sha()
    baseline = _load(original, "baseline")
    failures = [mutation_id for mutation_id, _label, _mutate, probe in _mutations()
                if not probe(baseline, original)]
    if failures:
        print(f"baseline: RED ({', '.join(failures)})")
        return 1
    print("baseline: GREEN")

    detected = fake = 0
    for mutation_id, label, mutate, probe in _mutations():
        changed = mutate(original)
        if changed == original:
            fake += 1
            print(f"{mutation_id} {label}: FAKE (anchor unchanged)")
            continue
        try:
            mutant = _load(changed, mutation_id)
            caught = not probe(mutant, changed)
        except Exception as exc:
            caught = False
            print(f"{mutation_id} {label}: INVALID MUTANT ({type(exc).__name__})")
        if caught:
            detected += 1
            print(f"{mutation_id} {label}: DETECTED")
        else:
            print(f"{mutation_id} {label}: SURVIVED")
    restore = "PASS" if before == _sha() else "FAIL"
    survived = len(_mutations()) - detected - fake
    print(f"detected={detected}/{len(_mutations())} survived={survived} fake={fake} timeout=0 restore sha256={restore}")
    return 0 if detected == len(_mutations()) and fake == 0 and restore == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
