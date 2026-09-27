#!/usr/bin/env python3
"""In-memory semantic mutation matrix for the R28-A contract."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
CONTRACT_PATH = os.path.join(BACKEND, "experiment_contract.py")


def _source_sha() -> str:
    return hashlib.sha256(open(CONTRACT_PATH, "rb").read()).hexdigest()


def _spec_kwargs():
    return {
        "strategy": None,
        "code_revision": "b" * 40,
        "dataset_fingerprint": "c" * 64,
        "universe_fingerprint": "d" * 64,
        "tradability_fingerprint": "e" * 64,
        "market_data_fingerprint": "f" * 64,
        "parameter_set": {"nested": {"value": 1}},
        "start_date": "2026-01-01",
        "end_date": "2026-03-31",
        "asof_policy": {"policy_id": "pit-v1", "cutoff": "2026-03-31T15:00:00+08:00"},
        "execution_assumptions": {
            "execution_profile_version": "profile-v1", "fill_assumptions": {"mode": "open"},
            "t_plus_one_semantics": "next-session", "price_limit_semantics": "exchange-v1",
            "partial_fill_semantics": "preserve-remainder", "capacity_assumptions": {"ratio": 0.05},
        },
        "cost_model": {
            "commission_rate": 0.0001, "minimum_commission": 0,
            "stamp_duty_rate": 0.0005, "slippage_model": "fixed-v1", "version": "cost-v1",
        },
        "random_seed": 17,
    }


def _load(source: str, suffix: str):
    name = f"_r28_mutant_{suffix}"
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(source, CONTRACT_PATH, "exec"), module.__dict__)
    return module


def _strategy(ec, checksum="a" * 64):
    return ec.StrategyIdentity("trend_pullback", 2, checksum)


def _spec(ec, **changes):
    values = _spec_kwargs()
    values["strategy"] = _strategy(ec)
    values.update(changes)
    return ec.ExperimentSpec(**values)


def _result(ec, fingerprint, **changes):
    values = {
        "experiment_fingerprint": fingerprint, "status": "completed", "total_return": 0.1,
        "max_drawdown": -0.05, "volatility": 0.2, "turnover": 1.0, "trade_count": 4,
        "total_cost": 10.0, "data_coverage": 0.9,
    }
    values.update(changes)
    return ec.ExperimentResult(**values)


def _probe(ec, mutant_id: str, source: str) -> bool:
    if mutant_id in {f"M-R28-{number:02d}" for number in range(1, 13)}:
        changes = {
            "M-R28-01": {"strategy": _strategy(ec, "1" * 64)},
            "M-R28-02": {"code_revision": "1" * 40},
            "M-R28-03": {"dataset_fingerprint": "1" * 64},
            "M-R28-04": {"universe_fingerprint": "1" * 64},
            "M-R28-05": {"tradability_fingerprint": "1" * 64},
            "M-R28-06": {"market_data_fingerprint": "1" * 64},
            "M-R28-07": {"parameter_set": {"nested": {"value": 2}}},
            "M-R28-08": {"end_date": "2026-04-01"},
            "M-R28-09": {"asof_policy": {"policy_id": "pit-v1", "cutoff": "2026-03-30T15:00:00+08:00"}},
            "M-R28-10": {"execution_assumptions": {
                **_spec_kwargs()["execution_assumptions"], "t_plus_one_semantics": "same-session"}},
            "M-R28-11": {"cost_model": {
                **_spec_kwargs()["cost_model"], "commission_rate": 0.0002}},
            "M-R28-12": {"random_seed": 18},
        }[mutant_id]
        first = _spec(ec)
        return first.fingerprint != _spec(ec, **changes).fingerprint
    if mutant_id == "M-R28-13":
        try:
            _spec(ec, universe_fingerprint="latest")
        except ValueError:
            return True
        return False
    if mutant_id == "M-R28-14":
        result = _result(ec, _spec(ec).fingerprint, status="failed", failure_reason="runner_failed",
                         total_return=None, max_drawdown=None, volatility=None, turnover=None,
                         trade_count=None, total_cost=None, data_coverage=None)
        return result.projection()["metrics"]["data_coverage"] is None
    if mutant_id == "M-R28-15":
        result = _result(ec, _spec(ec).fingerprint, status="failed", failure_reason="runner_failed",
                         total_return=None, max_drawdown=None, volatility=None, turnover=None,
                         trade_count=None, total_cost=None, data_coverage=None)
        return result.projection()["status"] == "failed"
    if mutant_id == "M-R28-16":
        return ec.RESULT_STATUSES == {"completed", "failed", "unavailable"}
    if mutant_id == "M-R28-17":
        first = _spec(ec, parameter_set={"a": 1, "b": 2})
        second = _spec(ec, parameter_set={"b": 2, "a": 1})
        return first.fingerprint == second.fingerprint
    if mutant_id == "M-R28-18":
        tree = ast.parse(source)
        clock_calls = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"now", "today", "utcnow"}
        }
        return not clock_calls
    if mutant_id == "M-R28-19":
        tree = ast.parse(source)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        return roots.isdisjoint({"backtest", "data_fetcher", "strategies", "paper_trading"})
    if mutant_id == "M-R28-20":
        fingerprint = _spec(ec).fingerprint
        result = _result(ec, fingerprint)
        return result.projection().get("experiment_fingerprint") == fingerprint
    raise AssertionError(f"unknown probe {mutant_id}")


def _mutations():
    projection_fields = {
        "M-R28-01": ('"strategy": self.strategy.projection(),', '"strategy": None,'),
        "M-R28-02": ('"code_revision": self.code_revision,', '"code_revision": None,'),
        "M-R28-03": ('"dataset_fingerprint": self.dataset_fingerprint,', '"dataset_fingerprint": None,'),
        "M-R28-04": ('"universe_fingerprint": self.universe_fingerprint,', '"universe_fingerprint": None,'),
        "M-R28-05": ('"tradability_fingerprint": self.tradability_fingerprint,', '"tradability_fingerprint": None,'),
        "M-R28-06": ('"market_data_fingerprint": self.market_data_fingerprint,', '"market_data_fingerprint": None,'),
        "M-R28-07": ('"parameter_set": _thaw_json(self.parameter_set),', '"parameter_set": None,'),
        "M-R28-08": ('"date_range": {"start": self.start_date, "end": self.end_date},', '"date_range": None,'),
        "M-R28-09": ('"asof_policy": _thaw_json(self.asof_policy),', '"asof_policy": None,'),
        "M-R28-10": ('"execution_assumptions": _thaw_json(self.execution_assumptions),', '"execution_assumptions": None,'),
        "M-R28-11": ('"cost_model": _thaw_json(self.cost_model),', '"cost_model": None,'),
        "M-R28-12": ('"random_seed": self.random_seed,', '"random_seed": None,'),
    }
    mutations = []
    for mutation_id, (old, new) in projection_fields.items():
        mutations.append((mutation_id, f"identity includes {mutation_id}", lambda s, a=old, b=new: s.replace(a, b, 1)))
    mutations.extend([
        ("M-R28-13", "no current/latest identity fallback", lambda s: s.replace(
            'raise ValueError(f"{name} must not use a current/latest identity")',
            'return "0" * 64', 1)),
        ("M-R28-14", "unknown result metric stays null", lambda s: s.replace(
            '"data_coverage": self.data_coverage,',
            '"data_coverage": 0.0 if self.data_coverage is None else self.data_coverage,', 1)),
        ("M-R28-15", "failed result status is preserved", lambda s: s.replace(
            '"status": self.status,', '"status": "completed" if self.status == "failed" else self.status,', 1)),
        ("M-R28-16", "result status vocabulary excludes promotion", lambda s: s.replace(
            'frozenset({"completed", "failed", "unavailable"})',
            'frozenset({"completed", "failed", "unavailable", "approved", "promotable"})', 1)),
        ("M-R28-17", "canonical JSON ignores insertion order", lambda s: s.replace(
            'hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()',
            'hashlib.sha256(repr(value).encode("utf-8")).hexdigest()', 1)),
        ("M-R28-18", "contract has no wall clock", lambda s: s.replace(
            'from dataclasses import dataclass', 'from dataclasses import dataclass\nimport datetime as dt', 1
        ).replace('"contract_version": self.contract_version,',
                  '"captured_at": dt.datetime.now().isoformat(),\n            "contract_version": self.contract_version,', 1)),
        ("M-R28-19", "legacy backtest is not canonical source", lambda s: s.replace(
            'from dataclasses import dataclass', 'from dataclasses import dataclass\nfrom backtest import run_backtest', 1)),
        ("M-R28-20", "result binds exact spec fingerprint", lambda s: s.replace(
            '"experiment_fingerprint": self.experiment_fingerprint,', '"experiment_fingerprint": None,', 1)),
    ])
    return mutations


def main() -> int:
    original = open(CONTRACT_PATH, encoding="utf-8").read()
    before = _source_sha()
    baseline_module = _load(original, "baseline")
    baseline_failures = [mutation_id for mutation_id, _label, _mutate in _mutations()
                         if not _probe(baseline_module, mutation_id, original)]
    if baseline_failures:
        print(f"baseline: RED ({', '.join(baseline_failures)})")
        return 1
    print("baseline: GREEN")

    detected = fake = 0
    mutations = _mutations()
    for mutation_id, label, mutate in mutations:
        changed = mutate(original)
        if changed == original:
            fake += 1
            print(f"{mutation_id} {label}: FAKE (mutation anchor did not change source)")
            continue
        try:
            if mutation_id in {"M-R28-18", "M-R28-19"}:
                caught = not _probe(baseline_module, mutation_id, changed)
            else:
                module = _load(changed, mutation_id.lower().replace("-", "_"))
                caught = not _probe(module, mutation_id, changed)
        except Exception as exc:  # malformed mutants are not counted as detection
            caught = False
            print(f"{mutation_id} {label}: INVALID MUTANT ({type(exc).__name__})")
        if caught:
            detected += 1
            print(f"{mutation_id} {label}: DETECTED")
        elif not changed == original:
            print(f"{mutation_id} {label}: SURVIVED")

    restore = "PASS" if before == _source_sha() else "FAIL"
    survived = len(mutations) - detected - fake
    print(f"detected={detected}/{len(mutations)} survived={survived} fake={fake} timeout=0 restore sha256={restore}")
    return 0 if detected == len(mutations) and fake == 0 and restore == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
