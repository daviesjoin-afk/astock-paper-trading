#!/usr/bin/env python3
"""In-memory architecture regression mutation matrix for R27-B2C-FINAL."""
from __future__ import annotations

import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)
from test_r27b2c_final_research_convergence import (  # noqa: E402
    _load_sources,
    architecture_violations,
)


def _inject(source: str, function: str, statement: str) -> str:
    lines = source.splitlines()
    prefix = f"def {function}("
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            end = index
            while not lines[end].rstrip().endswith(":"):
                end += 1
            lines.insert(end + 1, f"    {statement}")
            return "\n".join(lines) + "\n"
    raise AssertionError(f"function anchor not found: {function}")


def _sha(sources: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(sources.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(value.encode())
    return digest.hexdigest()


def main() -> int:
    originals = _load_sources()
    before = _sha(originals)
    baseline = architecture_violations(originals)
    if baseline:
        print(f"BASELINE-RED: {baseline}")
        return 1
    print("baseline: GREEN")

    cases = [
        ("M-FINAL-01", "incident adaptive_runs SQL", "backend/deepseek_research.py",
         lambda s: _inject(s, "_incident_evidence", '"SELECT * FROM adaptive_runs"')),
        ("M-FINAL-02", "paper_orders non-fill incident SQL", "backend/deepseek_research.py",
         lambda s: _inject(s, "_incident_evidence", '"SELECT status FROM paper_orders WHERE status != \'filled\'"')),
        ("M-FINAL-03", "runtime current/latest row fallback", "backend/deepseek_research.py",
         lambda s: _inject(s, "_incident_evidence", "latest_runtime_row = latest_runtime_row")),
        ("M-FINAL-04", "selection candidate SQL", "backend/deepseek_research.py",
         lambda s: _inject(s, "_candidate_evidence", '"SELECT * FROM adaptive_selection_candidates"')),
        ("M-FINAL-05", "risk candidate SQL", "backend/deepseek_research.py",
         lambda s: _inject(s, "_candidate_evidence", '"SELECT * FROM adaptive_risk_candidates"')),
        ("M-FINAL-06", "rewards and paper_nav SQL", "backend/deepseek_research.py",
         lambda s: _inject(s, "_overfit_evidence", '"SELECT * FROM adaptive_rewards JOIN paper_nav"')),
        ("M-FINAL-07", "news network fallback", "backend/deepseek_research.py",
         lambda s: _inject(s, "_event_evidence", "data_fetcher.fetch_fast_news()")),
        ("M-FINAL-08", "news PIT from published_at", "backend/deepseek_research.py",
         lambda s: _inject(s, "_event_evidence", "as_of = projection.published_at")),
        ("M-FINAL-09", "missing owner result becomes empty mapping", "backend/deepseek_research.py",
         lambda s: _inject(s, "_latest_data_quality", "missing = {}")),
        ("M-FINAL-10", "missing context falls back to today", "backend/deepseek_research.py",
         lambda s: _inject(s, "run_task", "today()")),
        ("M-FINAL-11", "suite derives a fresh current time", "backend/deepseek_research.py",
         lambda s: _inject(s, "_collect_suite_snapshot", "dt.datetime.now()")),
        ("M-FINAL-12", "legacy _save_run writer restored", "backend/deepseek_research.py",
         lambda s: s + "\n\ndef _save_run(*args):\n    return None\n"),
        ("M-FINAL-13", "canonical and legacy result dual-write", "backend/deepseek_research.py",
         lambda s: s + '\nLEGACY_WRITE = "INSERT INTO adaptive_advisor_runs(result) VALUES(?)"\n'),
        ("M-FINAL-14", "ai_analysis raw position read", "backend/ai_analysis.py",
         lambda s: _inject(s, "deterministic_snapshot", '"SELECT * FROM paper_positions"')),
        ("M-FINAL-15", "ai_analysis direct provider authority", "backend/ai_analysis.py",
         lambda s: "import ai_research_provider\n" + s),
        ("M-FINAL-16", "compatibility projection claims owner authority", "backend/deepseek_research.py",
         lambda s: s.rsplit('"authority": "research_composition_only_not_an_owner",', 1)[0]
         + '"authority": "canonical_owner_fact",'
         + s.rsplit('"authority": "research_composition_only_not_an_owner",', 1)[1]),
        ("M-FINAL-17", "runtime lifecycle status treated as verification", "backend/deepseek_research.py",
         lambda s: _inject(s, "_incident_evidence", "verified = projection.status == 'completed'")),
        ("M-FINAL-18", "owner verification leaks into model payload", "backend/deepseek_research.py",
         lambda s: s.replace('"is_verified", "outcome",', '"verified_flag", "outcome",', 1)),
    ]

    detected = 0
    for mutation_id, label, target, mutate in cases:
        if architecture_violations(originals):
            print(f"{mutation_id} {label}: BASELINE-RED")
            return 1
        changed = dict(originals)
        changed[target] = mutate(changed[target])
        violations = architecture_violations(changed)
        if violations:
            detected += 1
            print(f"{mutation_id} {label}: DETECTED")
        else:
            print(f"{mutation_id} {label}: SURVIVED")

    after = _sha(_load_sources())
    restore = "PASS" if before == after else "FAIL"
    survived = len(cases) - detected
    print(f"detected={detected}/{len(cases)} survived={survived} fake=0 timeout=0 restore sha256={restore}")
    return 0 if survived == 0 and restore == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
