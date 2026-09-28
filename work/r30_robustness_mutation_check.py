#!/usr/bin/env python3
"""Small, reversible semantic mutation check for the R30 exit matrix."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def py(test: str) -> tuple[str, ...]:
    return (sys.executable, "-m", "unittest", test, "-q")


def replace(path: str, old: str, new: str, test: str, *, frontend=False):
    return (ROOT / path, old, new, ("node", "--test", "tests/adaptive-robustness.test.mjs", "--test-name-pattern", test)
            if frontend else py(test), FRONTEND if frontend else BACKEND)


mutations = [
    replace("backend/robustness_runner.py", 'if run.get("validation_status") != "ready":', 'if False:', "test_robustness_runner.RobustnessRunnerTests.test_R30_12_blocked_run_is_rejected"),
    replace("backend/robustness_runner.py", 'if not isinstance(result, Mapping) or result.get("status") != "completed":', 'if not isinstance(result, Mapping):', "test_robustness_runner.RobustnessRunnerTests.test_R30_13_failed_result_is_rejected"),
    replace("backend/robustness_runner.py", 'if expected_run_key != run.get("run_key"):', 'if False:', "test_robustness_runner.RobustnessRunnerTests.test_baseline_run_key_must_match_the_R29_owner_projection"),
    replace("backend/robustness_contract.py", '"random_seed": self.random_seed,', '"random_seed": 0,', "test_robustness_runner.RobustnessContractTests.test_R30_03_seed_changes_plan_fingerprint"),
    replace("backend/robustness_contract.py", 'value.update({name: _thaw(getattr(self, name)) for name in _PLAN_LISTS})', 'value.update({name: [] for name in _PLAN_LISTS})', "test_robustness_runner.RobustnessContractTests.test_R30_04_stress_value_changes_plan_fingerprint"),
    replace("backend/robustness_contract.py", '"baseline_experiment_fingerprint": self.baseline_experiment_fingerprint}', '"baseline_experiment_fingerprint": "0" * 64}', "test_robustness_runner.RobustnessContractTests.test_R30_09_scenario_fingerprint_binds_baseline_experiment"),
    replace("backend/robustness_contract.py", 'return _sha({"report_version": report_version, "baseline_identity": dict(baseline_identity),', 'return _sha({"report_version": report_version, "baseline_identity": {},', "test_robustness_runner.RobustnessContractTests.test_R30_10_report_fingerprint_is_deterministic"),
    replace("backend/robustness_regimes.py", 'sessions[max(0, index - trend_window + 1):index + 1]', 'sessions[max(0, index - trend_window + 1):]', "test_robustness_runner.RobustnessRunnerTests.test_R30_19_regime_classifier_is_trailing_only"),
    replace("backend/robustness_regimes.py", 'trend = volatility = "unknown"', 'trend = volatility = "sideways"', "test_robustness_runner.RobustnessRunnerTests.test_R30_21_insufficient_history_is_unknown"),
    replace("backend/robustness_runner.py", 'costs = dict(spec.cost_model)', 'costs = {}', "test_robustness_runner.RobustnessRunnerTests.test_R30_28_cost_stress_comes_from_spec_cost_model"),
    replace("backend/robustness_runner.py", 'stress["slippage_multiplier"] = multiplier', 'stress["slippage_multiplier"] = 1', "test_robustness_runner.RobustnessRunnerTests.test_R30_29_slippage_stress_comes_from_spec"),
    replace("backend/experiment_execution_model.py", 'due_index = index + 1 + signal_delay + execution_delay', 'due_index = index + 1 + execution_delay', "test_robustness_runner.RobustnessRunnerTests.test_R30_31_signal_delay_uses_owner_session_indices"),
    replace("backend/experiment_execution_model.py", 'due_index = index + 1 + signal_delay + execution_delay', 'due_index = index + 1 + signal_delay', "test_robustness_runner.RobustnessRunnerTests.test_R30_33_execution_model_requeries_delayed_tradability"),
    replace("backend/experiment_execution_model.py", 'if evidence is None:', 'if False and evidence is None:', "test_robustness_runner.RobustnessRunnerTests.test_R30_33_execution_model_requeries_delayed_tradability"),
    replace("backend/experiment_execution_model.py", 'if bar is None:\n                raise ExperimentExecutionUnavailable("execution_market_bar_unavailable")', 'if False:\n                raise ExperimentExecutionUnavailable("execution_market_bar_unavailable")', "test_robustness_runner.RobustnessRunnerTests.test_R30_34_missing_delayed_market_bar_is_unavailable"),
    replace("backend/experiment_execution_model.py", 'float(volume) * float(stress.get("liquidity_multiplier", 1.0))', 'float(volume)', "test_robustness_runner.RobustnessRunnerTests.test_R30_36_liquidity_stress_changes_capacity_not_price_truth"),
    replace("backend/robustness_runner.py", 'if case_evidence["masked_observations"]:', 'if False:', "test_robustness_runner.RobustnessRunnerTests.test_masked_required_strategy_bar_is_unavailable_not_zero_performance"),
    replace("backend/robustness_runner.py", 'return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") / 2**64', 'return __import__("random").random()', "test_robustness_runner.RobustnessRunnerTests.test_R30_38_missingness_is_deterministic"),
    replace("backend/robustness_runner.py", 'def _bar_is_masked(bar: Mapping[str, Any], *, seed: int, fraction: float,\n                   field_scope: str) -> bool:', 'def _bar_is_masked(bar: Mapping[str, Any], *, seed: int, fraction: float,\n                   field_scope: str, pnl: float = 0) -> bool:', "test_robustness_runner.RobustnessRunnerTests.test_R30_40_mask_selector_has_no_performance_input"),
    replace("backend/robustness_contract.py", 'if path not in allowlist:', 'if False:', "test_robustness_runner.RobustnessRunnerTests.test_R30_44_only_allowlisted_parameter_path_changes"),
    replace("backend/robustness_runner.py", 'if start < 0 or end >= len(sessions) or start > end:', 'if start > end:', "test_robustness_runner.RobustnessRunnerTests.test_R30_49_out_of_coverage_date_shift_fails_closed"),
    replace("backend/robustness_runner.py", 'if _deterministic_fraction(seed, code) < fraction}', 'if False}', "test_robustness_runner.RobustnessRunnerTests.test_R30_50_universe_drop_is_deterministic"),
    replace("backend/robustness_contract.py", 'elif metrics is not None or baseline_delta is not None or not reason_code:', 'elif not reason_code:', "test_robustness_runner.RobustnessRunnerTests.test_R30_55_unavailable_scenario_cannot_claim_metrics"),
    replace("backend/robustness_runner.py", 'return {"report_fingerprint": fingerprint,', 'return {"score": 1, "report_fingerprint": fingerprint,', "test_robustness_runner.RobustnessRunnerTests.test_R30_58_report_has_no_single_score"),
    replace("backend/robustness_runner.py", 'return {"report_fingerprint": fingerprint,', 'return {"promotion_status": "ready", "report_fingerprint": fingerprint,', "test_robustness_runner.RobustnessRunnerTests.test_R30_60_report_has_no_promotion_status"),
    replace("backend/robustness_contract.py", 'if include_created_at:', 'if True:', "test_robustness_runner.RobustnessContractTests.test_R30_05_created_at_is_outside_plan_identity"),
    replace("backend/robustness_runner.py", 'for scenario in plan.scenarios():', 'for scenario in reversed(plan.scenarios()):', "test_robustness_runner.RobustnessRunnerTests.test_R30_62_case_order_is_deterministic"),
    replace("backend/experiment_execution_model.py", 'if include_trace:\n        result["trace"] = trace', 'if True:\n        result["trace"] = trace', "test_robustness_runner.RobustnessRunnerTests.test_R30_35_trace_keeps_R29_aggregate_metrics_bit_identical"),
    replace("backend/robustness_repository.py", "BEFORE UPDATE ON robustness_reports BEGIN SELECT RAISE(ABORT, 'append-only robustness reports'); END;", "BEFORE UPDATE ON robustness_reports BEGIN SELECT 1; END;", "test_robustness_repository.RobustnessRepositoryTests.test_R30_64_report_ledger_is_append_only"),
    replace("backend/robustness_repository.py", '        if existing is not None:\n            decoded = _decode(existing)\n            if decoded["payload_fingerprint"] != payload_fingerprint:\n                raise RobustnessPersistenceError("report_key_payload_conflict")\n            return decoded', '        if False:', "test_robustness_repository.RobustnessRepositoryTests.test_R30_65_same_report_is_idempotent"),
    replace("backend/robustness_repository.py", 'if decoded["payload_fingerprint"] != payload_fingerprint:', 'if False:', "test_robustness_repository.RobustnessRepositoryTests.test_R30_66_same_key_with_different_content_is_rejected"),
    replace("backend/robustness_repository.py", '            existing = self.conn.execute("SELECT * FROM robustness_reports WHERE report_key=?",\n                                         (payload["report_key"],)).fetchone()\n            if existing is not None:\n                decoded = _decode(existing)\n                if decoded["payload_fingerprint"] == payload_fingerprint:\n                    return decoded\n                raise RobustnessPersistenceError("report_key_payload_conflict") from exc', '            raise RobustnessPersistenceError("robustness_report_conflict") from exc', "test_robustness_repository.RobustnessRepositoryTests.test_concurrent_identical_posts_are_idempotent"),
    replace("backend/api_adaptive.py", '    except RREP.RobustnessPersistenceError as exc:\n        raise HTTPException(status_code=409, detail=str(exc)) from exc\n    except (TypeError, ValueError, KeyError) as exc:\n        reason = str(exc) if str(exc).isidentifier() else "robustness_request_invalid"', '    except (TypeError, ValueError, KeyError) as exc:\n        reason = str(exc) if str(exc).isidentifier() else "robustness_request_invalid"\n    except RREP.RobustnessPersistenceError as exc:\n        raise HTTPException(status_code=409, detail=str(exc)) from exc', "test_robustness_api.RobustnessApiTests.test_persistence_errors_are_mapped_before_value_error"),
    replace("backend/robustness_runner.py", 'if (archive_tradability_fingerprint != baseline_identity["tradability_evidence_fingerprint"]\n            or archive_tradability_fingerprint != spec.tradability_fingerprint):', 'if False:', "test_robustness_runner.RobustnessRunnerTests.test_baseline_rejects_later_tradability_archive_revision"),
    replace("backend/robustness_runner.py", 'start = bisect_left(sessions, spec.start_date)', 'start = sessions.index(spec.start_date)', "test_robustness_runner.RobustnessRunnerTests.test_weekend_spec_boundaries_anchor_date_stresses_to_owner_sessions"),
    replace("backend/robustness_runner.py", 'tradability_fingerprint=ranged_tradability_fingerprint,', 'tradability_fingerprint=spec.tradability_fingerprint,', "test_robustness_runner.RobustnessRunnerTests.test_weekend_spec_boundaries_anchor_date_stresses_to_owner_sessions"),
    replace("frontend/src/features/adaptive.js", "return value===null||value===undefined?'不可用':adaptiveValue(value,'',4);", "return adaptiveValue(value||0,'',4);", "R30-82", frontend=True),
    replace("frontend/src/features/adaptive.js", "+'<h5>Baseline identity</h5><pre>'+adaptiveEsc(JSON.stringify(report.baseline_identity||{},null,2))+'</pre>'", "+'<p>score: 0</p><h5>Baseline identity</h5><pre>'+adaptiveEsc(JSON.stringify(report.baseline_identity||{},null,2))+'</pre>'", "R30-83", frontend=True),
    replace("frontend/src/features/adaptive.js", "+'<h5>Sensitivity</h5>'+sensitivityHtml", "+'<button>promote</button><h5>Sensitivity</h5>'+sensitivityHtml", "R30-84", frontend=True),
    replace("frontend/src/features/adaptive.js", "'<dl>'+r30MetricRows(metrics)", "'<dl>'+r30MetricRows(Number(metrics))", "R30-85", frontend=True),
    replace("backend/experiment_pit_validation.py", '"execution_evidence": (TA.evidence_fingerprint(execution_facts[(code, session)])\n                               if execution_facts.get((code, session)) is not None else None),', '"execution_evidence": None,', "test_robustness_runner.RobustnessRunnerTests.test_R30_64_open_time_tradability_revision_rejects_baseline_when_close_fact_is_unchanged"),
]


def run(command: tuple[str, ...], cwd: Path, *, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main() -> int:
    files = sorted({item[0] for item in mutations})
    original = {path: path.read_bytes() for path in files}
    hashes = {path: sha(data) for path, data in original.items()}
    baseline_command = (sys.executable, "-m", "unittest", "test_robustness_runner",
                        "test_robustness_repository", "test_robustness_api", "-q")
    baseline = run(baseline_command, BACKEND)
    if baseline.returncode:
        print("baseline = RED")
        print(baseline.stdout[-3000:] + baseline.stderr[-3000:])
        return 1
    print("baseline = GREEN")
    detected = fake = timeout = survived = 0
    try:
        for index, (path, old, new, command, cwd) in enumerate(mutations, 1):
            source = original[path].decode("utf-8")
            if source.count(old) != 1:
                fake += 1
                print(f"M-R30-{index:02d} FAKE (anchor count={source.count(old)})")
                continue
            path.write_text(source.replace(old, new, 1), encoding="utf-8", newline="")
            try:
                result = run(command, cwd)
            except subprocess.TimeoutExpired:
                timeout += 1
                print(f"M-R30-{index:02d} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"M-R30-{index:02d} DETECTED")
                else:
                    survived += 1
                    print(f"M-R30-{index:02d} SURVIVED")
            finally:
                path.write_bytes(original[path])
    finally:
        for path, data in original.items():
            path.write_bytes(data)
    restored = all(sha(path.read_bytes()) == hashes[path] for path in files)
    final_baseline = run(baseline_command, BACKEND)
    print(f"mutation = {detected}/{len(mutations)} DETECTED")
    print(f"survived = {survived}")
    print(f"fake = {fake}")
    print(f"timeout = {timeout}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final_baseline.returncode == 0 else 'RED'}")
    return 0 if detected == len(mutations) and not survived and not fake and not timeout and restored and final_baseline.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
