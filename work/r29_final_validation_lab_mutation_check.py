#!/usr/bin/env python3
"""Behavioral mutation checks for R29-FINAL. All source mutations stay in memory."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
import r29a_pit_validation_mutation_check as R29A  # noqa: E402
import test_experiment_pit_validation as PIT_TESTS  # noqa: E402
import test_r29_final_archives as ARCHIVE_TESTS  # noqa: E402
import test_r29_final_financial as FIN_TESTS  # noqa: E402
import test_r29_final_runner as RUNNER_TESTS  # noqa: E402
import test_r29_final_api as API_TESTS  # noqa: E402

BASE_PIT = "backend/experiment_pit_validation.py"
EXTRA = [
    ("M-R29F-31", "qfq stays outside the historical archive", "backend/historical_market_archive.py",
     'if adjustment not in {"raw", "none", "unadjusted"}:', "if False:",
     ARCHIVE_TESTS, "HistoricalMarketArchiveTests", "HMA", "test_qfq_is_rejected"),
    ("M-R29F-32", "conflicting duplicate bars fail closed", "backend/historical_market_archive.py",
     "if prior is not None and prior != item:", "if False:",
     ARCHIVE_TESTS, "HistoricalMarketArchiveTests", "HMA", "test_conflicting_duplicate_bar_fails_closed"),
    ("M-R29F-33", "historical archive rows cannot be edited", "backend/historical_market_archive.py",
     "BEFORE UPDATE ON historical_market_bars BEGIN SELECT RAISE(ABORT, 'immutable archive'); END;",
     "BEFORE UPDATE ON historical_market_bars BEGIN SELECT 1; END;",
     ARCHIVE_TESTS, "HistoricalMarketArchiveTests", "HMA", "test_manifest_and_rows_are_immutable"),
    ("M-R29F-34", "calendar must cover the exact requested range", "backend/historical_session_calendar.py",
     "if manifest.coverage_start > start or manifest.coverage_end < end:", "if False:",
     ARCHIVE_TESTS, "HistoricalMarketArchiveTests", "HSC", "test_calendar_issues_only_from_raw_archive_and_bounded_range"),
    ("M-R29F-35", "same-day later filing remains future", "backend/financial_feature_evidence.py",
     "financial_visibility(\n        row, decision_at,", "financial_visibility(\n        row, decision_at[:10],",
     FIN_TESTS, "FinancialOwnerTests", "FFE", "test_FIN_07_same_day_later_instant_is_future"),
    ("M-R29F-36", "date-only filing cannot prove intraday visibility", "backend/financial_feature_evidence.py",
     'return False, "financial_publication_unproven"', 'return True, "visible"',
     FIN_TESTS, "FinancialOwnerTests", "FFE", "test_FIN_08_date_only_same_day_is_unproven_at_intraday_decision"),
    ("M-R29F-37", "financial field cannot be relabeled", "backend/financial_feature_evidence.py",
     "_decimal_value(stored_number) != computed", "False",
     FIN_TESTS, "FinancialOwnerTests", "FFE", "test_FIN_05_dataset_value_mismatch_is_blocked"),
    ("M-R29F-38", "decision instants normalize to UTC", BASE_PIT,
     "parsed.astimezone(dt.timezone.utc)", "parsed",
     PIT_TESTS, "PITValidationTests", "PV", "test_R29_35_decision_instant_is_normalized_to_utc"),
    ("M-R29F-39", "exact strategy version is required", "backend/experiment_validation_runner.py",
     "strategy_version.version == spec.strategy.version", "True",
     RUNNER_TESTS, "R29RunnerTests", "RUNNER", "test_strategy_version_mismatch_is_unavailable"),
    ("M-R29F-40", "code revision must match the pinned spec", "backend/experiment_validation_runner.py",
     "runner_code_revision != spec.code_revision", "False",
     RUNNER_TESTS, "R29RunnerTests", "RUNNER", "test_code_revision_mismatch_is_unavailable_without_metrics"),
    ("M-R29F-41", "ledger key retains archive identities", "backend/experiment_validation_repository.py",
     '"owner_identities": dict(owner_identities)', '"owner_identities": {}',
     ARCHIVE_TESTS, "ValidationLedgerTests", "EVR", "test_run_key_is_deterministic_and_corrupt_json_fails_closed"),
    ("M-R29F-42", "corrupt ledger JSON fails closed", "backend/experiment_validation_repository.py",
     'except (TypeError, ValueError, json.JSONDecodeError) as exc:\n        raise ExperimentValidationPersistenceError("corrupt_validation_run") from exc',
     'except (TypeError, ValueError, json.JSONDecodeError):\n        return {}',
     ARCHIVE_TESTS, "ValidationLedgerTests", "EVR", "test_run_key_is_deterministic_and_corrupt_json_fails_closed"),
    ("M-R29F-43", "report period cannot replace publication time", "backend/historical_financial_archive.py",
     '_publication(raw.get("published_at"), raw.get("publication_precision"))',
     '_publication(raw.get("published_at") or raw.get("report_period"), raw.get("publication_precision") if raw.get("published_at") else "date")',
     FIN_TESTS, "FinancialOwnerTests", "HFA", "test_FIN_09_missing_publication_cannot_enter_archive"),
    ("M-R29F-44", "financial feature identity changes the dataset fingerprint", "backend/learning_dataset.py",
     'if refs:', 'if False:',
     FIN_TESTS, "FinancialOwnerTests", "LD", "test_FIN_15_derivation_version_changes_dataset_and_evidence_identity"),
    ("M-R29F-45", "feature availability is latest required disclosure", "backend/financial_feature_evidence.py",
     "maximum = max(values)", "maximum = min(values)",
     FIN_TESTS, "FinancialOwnerTests", "FFE", "test_FIN_14_feature_availability_is_latest_required_input"),
    ("M-R29F-46", "caller universe rows cannot issue archive proof", BASE_PIT,
     "passed = owner_issued and memberships_complete and session_calendar_complete",
     "passed = memberships_complete and session_calendar_complete",
     PIT_TESTS, "PITValidationTests", "PV", "test_R29_04b_caller_universe_claim_is_not_an_archive_owner"),
]


def _load(path: Path, source: str, name: str):
    module_name = f"_r29_final_mutant_{name.lower().replace('-', '_')}"
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def _probe(test_module, class_name: str, attribute: str, mutant, test_name: str) -> bool:
    original = getattr(test_module, attribute)
    setattr(test_module, attribute, mutant)
    try:
        case = getattr(test_module, class_name)(test_name)
        result = unittest.TestResult()
        case.run(result)
        return not result.errors and not result.failures
    finally:
        setattr(test_module, attribute, original)


def main() -> int:
    hashes = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
              for path in [BASE_PIT] + [item[2] for item in EXTRA]}

    # Retain the R29-A mutations whose behavioral probes remain exact after the
    # final owner split. These are not counted when their probes are stale.
    retained_ids = {"M-R29-05", "M-R29-06", "M-R29-09", "M-R29-10",
                    "M-R29-11", "M-R29-12", "M-R29-13", "M-R29-15", "M-R29-16",
                    "M-R29-17", "M-R29-18", "M-R29-19", "M-R29-20", "M-R29-21",
                    "M-R29-22", "M-R29-24", "M-R29-25", "M-R29-26", "M-R29-27"}
    old_source = (ROOT / BASE_PIT).read_text(encoding="utf-8")
    old_module = R29A._load(old_source, "final_baseline")
    old_mutations = [item for item in R29A._mutations() if item[0] in retained_ids]
    failed = [key for key, _label, _mutate, probe in old_mutations
              if not probe(old_module, old_source)]
    extra_sources = {item[2]: (ROOT / item[2]).read_text(encoding="utf-8") for item in EXTRA}
    failed.extend(item[0] for item in EXTRA if item[3] not in extra_sources[item[2]])
    if failed:
        print("baseline: RED (" + ", ".join(failed) + ")")
        return 1
    print("baseline: GREEN")

    detected = fake = survived = 0
    for key, label, mutate, probe in old_mutations:
        changed = mutate(old_source)
        if changed == old_source:
            fake += 1
            print(f"{key} {label}: FAKE")
            continue
        try:
            mutant = R29A._load(changed, key)
            caught = not probe(mutant, changed)
        except Exception as exc:
            caught = False
            print(f"{key} {label}: INVALID MUTANT ({type(exc).__name__})")
        if caught:
            detected += 1
            print(f"{key} {label}: DETECTED")
        else:
            survived += 1
            print(f"{key} {label}: SURVIVED")

    for key, label, relative, good, bad, test_module, class_name, attr, test_name in EXTRA:
        original = extra_sources[relative]
        changed = original.replace(good, bad, 1)
        if changed == original:
            fake += 1
            print(f"{key} {label}: FAKE")
            continue
        try:
            mutant = _load(ROOT / relative, changed, key)
            caught = not _probe(test_module, class_name, attr, mutant, test_name)
        except Exception as exc:
            caught = False
            print(f"{key} {label}: INVALID MUTANT ({type(exc).__name__})")
        if caught:
            detected += 1
            print(f"{key} {label}: DETECTED")
        else:
            survived += 1
            print(f"{key} {label}: SURVIVED")

    restored = all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == value
                   for path, value in hashes.items())
    total = len(old_mutations) + len(EXTRA)
    print(f"detected={detected}/{total} survived={survived} fake={fake} timeout=0 restore SHA256={'PASS' if restored else 'FAIL'}")
    return 0 if detected == total and survived == 0 and fake == 0 and restored else 1


if __name__ == "__main__":
    raise SystemExit(main())
