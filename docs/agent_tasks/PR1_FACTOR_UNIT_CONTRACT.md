# PR-1 Agent Task — P0 Factor Unit Contract

Repository: `daviesjoin-afk/astock-paper-trading`

Working branch: `codex/p0-factor-unit-contract`

Base: latest `master` at the time this branch was created.

## Objective

Fix the confirmed percentage-unit mismatch in factor and strategy logic without changing strategy intent.

Canonical rule for historical return / momentum values:

- `0.02 == +2%`
- `0.18 == +18%`
- `-0.03 == -3%`

`mom5`, `mom20`, `mom60`, `rev5` and corresponding internal `*_raw` momentum values are **fractions**, not percentage points.

This PR is a correctness fix only. It is **not** permission to retune strategy thresholds, redesign scoring, change risk rules, alter execution behavior, or refactor unrelated modules.

---

## Hard scope

Primary files to inspect:

- `backend/factors.py`
- `backend/strategies.py`
- existing tests that exercise paper strategy scoring / candidate ranking / momentum / hot leader / bottom reversal / sentiment pioneer

You may add:

- `backend/factor_units.py` if useful for explicit conversion helpers
- `backend/test_factor_unit_contract.py`
- narrowly targeted regression tests in existing strategy test files when that is a better fit

Do not modify frontend files in this PR.

Do not modify self-evolution, security/auth, database schema, Docker, deployment, execution, T+1, quote freshness, risk gates, capital allocation, or Champion/Challenger logic.

---

## Step 1 — Prove the current unit semantics before editing

Read the implementation of `compute_price_factors()` in `backend/factors.py` and document in the PR description exactly how each momentum field is computed.

Confirm that expressions are of the form:

```python
close_now / close_past - 1
```

and therefore produce fractions.

Before changing any comparison, inspect all callers/usages of:

- `mom5`
- `mom20`
- `mom60`
- `mom5_raw`
- `mom20_raw`
- `mom60_raw`
- `rev5`

Do not rely on names or comments alone. Search definitions and every strategy comparison.

Create a short audit table in the PR description with columns:

- location
- field
- current threshold
- intended human percentage
- canonical fraction
- action taken

Do not silently correct any threshold whose intent cannot be established from code/tests/comments. If intent is genuinely ambiguous, leave it unchanged and call it out in the PR report.

---

## Step 2 — Preserve already-correct fraction thresholds

Known examples that are already fraction-based and must not be converted again include values such as:

```python
MOM5_OVERHEAT_PCT = 0.05
MOM20_OVERHEAT_PCT = 0.15
```

when they mean 5% and 15%.

The Agent must explicitly verify these remain semantically unchanged.

Never perform a global numeric search/replace.

---

## Step 3 — Fix confirmed mismatches in strategy scoring

Inspect at minimum:

- `_hot_leader_profile()`
- `_bottom_reversal_profile()`
- `PAPER_CONDITION_DEFAULTS`
- the `sentiment_pioneer` individual-strength path
- `_run_paper_strategy()`
- every direct comparison involving `mom5_raw` / `mom20_raw` / `mom60_raw`

Previously observed suspicious examples include comparisons equivalent to:

```python
mom5.ge(18)
mom20.ge(35)
mom5.between(1.0, 10.0)
mom5.ge(15)
individual_mom5_min = 2.0
```

Because the underlying momentum values are fractions, if the documented intent is respectively 18%, 35%, 1%–10%, 15%, and 2%, the correct comparisons are respectively:

```python
0.18
0.35
0.01 .. 0.10
0.15
0.02
```

Do not assume every large-looking threshold is wrong. Confirm each one from surrounding semantics.

The goal is to restore the originally intended human percentages, not optimize them.

---

## Step 4 — Introduce an explicit unit contract

Prefer a small, dependency-free helper module such as `backend/factor_units.py` if it improves clarity.

Acceptable helpers include:

```python
def fraction_to_pct_points(value): ...
def pct_points_to_fraction(value): ...
```

or equivalent names.

Requirements:

- pure functions
- no pandas dependency unless truly necessary
- no network / DB / strategy imports
- finite-number behavior must be explicit
- tests for positive, negative and zero values

Do not force conversions everywhere merely to use the helper. The important rule is that strategy comparisons operate in the same unit as the field.

For newly touched local variables, prefer names/comments that make the unit obvious, e.g.:

```python
# fraction: 0.05 == +5%
```

Do not rename public JSON/API fields in this PR.

---

## Step 5 — Add regression tests that prove the bug is actually fixed

Add `backend/test_factor_unit_contract.py` or equivalent tests.

Minimum required cases:

### A. Price-factor generation

Construct deterministic close data and verify:

- approximately +2% five-day return is represented as approximately `0.02`, not `2.0`
- negative returns remain negative fractions

Do not make the test dependent on live market data.

### B. Hot leader overheat

Build a minimal factor table that reaches `_hot_leader_profile()` and prove:

- +18% `mom5` triggers the intended 18% overheat component
- +8% `mom5` does not trigger the 18% condition

When multiple overheat components exist, assert the specific difference attributable to momentum instead of using a vague final score assertion.

### C. Bottom reversal momentum semantics

Prove that the intended 1%–10% short-turn band can be reached using fraction values such as `0.03`.

Prove that a realistic +15% threshold behaves as 0.15 rather than 15.0 where that condition exists.

### D. Sentiment-pioneer individual momentum path

If the configured intent is 2%, construct otherwise-passing data and prove:

- `mom5_raw = 0.02` satisfies the momentum component
- `mom5_raw = 0.01` does not

The test should isolate the relevant gate as much as possible.

### E. Preserve existing correct thresholds

Add at least one test showing the existing 5% / 15% overheat guard still behaves as 5% / 15%, not 0.05% / 0.15% and not 500% / 1500%.

---

## Step 6 — Add a narrow regression guard against future 100x mistakes

Add a test/static check that protects the known class of failure without creating a noisy generic linter.

Good approach:

- inspect the specific strategy functions/known configurable momentum fields
- assert percentage-style defaults are stored as fractions when they are compared directly to `mom*_raw`

Bad approach:

- reject every number greater than 1 anywhere near the word `mom`
- regex the whole repository with many false positives
- block display/serialization code that legitimately converts a fraction into percent points

The regression guard must be narrow and explainable.

---

## Step 7 — Verify no unrelated strategy retuning occurred

Before finalizing, inspect the diff and explicitly confirm:

- no factor weights changed unless needed purely to fix unit representation
- no T+1 rule changed
- no quote freshness rule changed
- no security scope changed
- no order sizing changed
- no execution threshold changed
- no market-risk light rule changed
- no strategy family changed
- no frontend behavior changed

If any unrelated numeric threshold appears in the diff, justify it or revert it.

---

## Required validation commands

Run all commands that are supported by the environment. Do not claim commands were run if they were not.

Backend full regression:

```bash
python -m unittest discover -s backend -p "test_*.py" -v
python -m compileall -q backend
ruff check backend
```

Security regression:

```bash
python scripts/security/scan-sensitive-data.py --repo . --scope all
```

If the repository's standard CI/test command differs, run that too.

No frontend change is expected; do not regenerate frontend artifacts merely to create noise.

---

## Required PR title

```text
fix(factors): enforce momentum fraction units across strategy scoring
```

## Required PR body structure

Use these sections:

### Root cause

Explain that price momentum is produced as a fraction (`0.18 == 18%`) while several strategy thresholds were interpreted as percentage points (`18 == 18%`), causing 100x threshold mismatches and effectively dead scoring/gating branches.

### Runtime path

Show the real path from:

`compute_price_factors` → factor table `mom*_raw` → paper strategy profile/gates → ranked candidate score.

### Threshold audit

Include the audit table requested above.

### Changes

Explain exactly which comparisons/defaults were corrected and whether a unit helper was added.

### Invariants preserved

Explicitly state that the PR does not change T+1, quote freshness, risk gates, security scope, capital allocation, strategy families, frontend contracts, or self-evolution activation.

### Tests

List every new regression test and the exact bug it prevents.

### Validation

List commands actually executed and their actual results.

### Compatibility

State whether public API field names and persisted data formats changed. They should not change in this PR.

### Residual risk

List any momentum threshold whose intended unit could not be established and was intentionally left unchanged.

---

## Definition of Done

This PR is complete only when all are true:

1. `mom5/mom20/mom60/rev5` historical-return semantics are explicitly documented as fractions.
2. Confirmed percentage-point-vs-fraction mismatches in production strategy logic are fixed.
3. Existing correct fraction thresholds remain correct.
4. Tests prove realistic 2%, 8%, 15%, 18%, 35% cases with the correct internal representation.
5. The known hot-leader / bottom-reversal / sentiment-pioneer dead-unit branches are reachable under realistic data where intended.
6. Public API field names are unchanged.
7. No unrelated strategy retuning is present in the diff.
8. Full backend regression passes, or any failure is reported verbatim with root-cause analysis.
9. Full-history security scan passes.
10. A PR is opened from `codex/p0-factor-unit-contract` to `master`; do not merge it automatically.
