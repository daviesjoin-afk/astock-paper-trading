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
        report = TESTS._evaluate(samples=samples,
                                 authoritative_sessions=TESTS._sessions()[:4])
        return report.dimensions["walk_forward"]["status"] == "blocked"
    finally:
        TESTS.PV = original


def _mutations():
    return [
        ("M-R29-01", "current universe source rejected",
         lambda s: s.replace('passed = bool(result.get("passed")) and bool(report.get("historical_membership_complete"))', 'passed = True', 1),
         lambda m, s: _unit_probe(m, "test_R29_04_current_universe_source_cannot_pass_historical_completeness")),
        ("M-R29-02", "universe fingerprint is not provenance",
         lambda s: s.replace('passed = bool(result.get("passed")) and bool(report.get("historical_membership_complete"))', 'passed = bool(spec.universe_fingerprint)', 1),
         lambda m, s: _unit_probe(m, "test_R29_03_universe_sha_alone_does_not_prove_history")),
        ("M-R29-03", "missing tradability facts block",
         lambda s: s.replace('complete = requested > 0 and available + blocked == requested and unknown == 0', 'complete = requested > 0', 1),
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
         lambda s: s.replace('view = FPIT.financial_visibility(record, cutoff)', 'view = FPIT.financial_visibility({**record, "published_at": record.get("report_period")}, cutoff)', 1),
         lambda m, s: _unit_probe(m, "test_R29_10_report_period_cannot_substitute_publication_time")),
        ("M-R29-08", "future publication is invisible",
         lambda s: s.replace('if source == "future":\n            counts["future"] += 1', 'if source == "future":\n            counts["visible"] += 1', 1),
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
         lambda s: s.replace('sessions=authoritative_sessions, asof=cutoff,', 'asof=cutoff,', 1),
         lambda m, s: _unit_probe(m, "test_R29_17_walk_forward_requires_explicit_sessions")),
        ("M-R29-15", "not-matured OOS cannot be ready",
         lambda s: s.replace('ready = walk["timeline_source"] == "explicit_sessions" and walk["ready_folds"] > 0', 'ready = True', 1),
         lambda m, s: _all_future_probe(m)),
        ("M-R29-16", "purge stays in session/label owner",
         lambda s: s.replace('def _normalize_samples(samples: Sequence[Any]) -> list[WFV.ValidationSample]:', 'def _calendar_day_purge():\n    return dt.timedelta(days=5)\n\ndef _normalize_samples(samples: Sequence[Any]) -> list[WFV.ValidationSample]:', 1),
         lambda m, s: _static_probe(s, "no-calendar-purge")),
        ("M-R29-17", "any blocked required dimension blocks the report",
         lambda s: s.replace('ready = all_proven and walk.get("ready_folds", 0) >= 1 and not reasons', 'ready = walk.get("ready_folds", 0) >= 1', 1),
         lambda m, s: _unit_probe(m, "test_R29_20_blocked_required_dimension_blocks_whole_validation")),
        ("M-R29-18", "unknown denominator remains null",
         lambda s: s.replace('if denominator <= 0:\n        return None', 'if denominator <= 0:\n        return 0.0', 1),
         lambda m, s: _unit_probe(m, "test_R29_23_unknown_coverage_remains_none")),
        ("M-R29-19", "blocked input has no zero performance result",
         lambda s: s.replace('"status": self.status,', '"metrics": {"return": 0},\n            "status": self.status,', 1),
         lambda m, s: _unit_probe(m, "test_R29_21_blocked_evidence_has_no_zero_performance_metrics")),
        ("M-R29-20", "evaluation uses explicit as-of only",
         lambda s: s.replace('cutoff = spec.asof_policy.get("cutoff")', 'cutoff = spec.asof_policy.get("cutoff") or dt.datetime.now().isoformat()', 1),
         lambda m, s: _static_probe(s, "no-clock")),
        ("M-R29-21", "report has no approval or promotion status",
         lambda s: s.replace('if self.status not in {"ready", "blocked"}:', 'if self.status not in {"ready", "blocked", "approved"}:', 1),
         lambda m, s: _static_probe(s, "no-promotion")),
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
