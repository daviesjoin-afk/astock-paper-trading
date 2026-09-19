## ⛔ STATUS: DO NOT MERGE / DO NOT DEPLOY — awaiting human review

`MERGE: NOT MERGED` · `DEPLOY: NOT DEPLOYED`

## Review follow-up (round 2)

An independent exact-head review of `0cd7d4b` confirmed the production
correctness fix and the architecture extraction, but flagged two closing items.
Both are addressed in the current head.

1. **Exact-head CI was red.** Three jobs (`tests (3.11)`, `tests (3.12)`,
   `docker-smoke`) failed on the *same single* date-brittle fixture,
   `test_rebalance_cycle_scope.SameDayRolloverKeepsBothRows.test_two_cycles_same_day_coexist`.
   It was pre-existing (reproduced on the unmodified base SHA), but this project's
   merge rule is **exact-head CI all green**, so "pre-existing" is not a basis for
   merging over red. Fixed **test-only**, in this PR rather than a separate one,
   because it was blocking the whole repository's CI.
   `rebalance_scanner.py` / production code was **not** touched. See
   "Fixture fix" below.
2. **The second production path had no regression coverage.** The original defect
   lived on **two** entries (`_sell_plan` *and* `_intraday_downside_guard`). Both
   were fixed, but only the first was pinned by a test. Added **RD-16** (real
   `PT._intraday_downside_guard`) plus the matching non-vacuous mutation
   **M-RD13**. See "Tests" and "Non-vacuity" below.

## Fixture fix (`backend/test_rebalance_cycle_scope.py`, test-only)

The fixture asserted a **trading day** (`2026-09-19`) while the production scanner
stamps new rows with `_date()` → `_now()` → the machine's Asia/Shanghai date. So
the case only passed while the machine happened to still be on 09-19. The fix
pins the *scanner clock* inside the case, leaving the fixture's dates alone:

```python
frozen = dt.datetime(2026, 9, 19, 22, 0, 0,
                     tzinfo=dt.timezone(dt.timedelta(hours=8)))  # Asia/Shanghai
with mock.patch.object(RS, "_now", return_value=frozen):
    self.scan({CODE: QUOTE_FLAT})
```

The case now asserts what it was always meant to assert — *a cycle rollover
within one trading day keeps one scan row per cycle* — instead of *the test
machine's today happens to be 2026-09-19*. No production code changed.

## Summary

Sell-risk decisions silently depended on **the machine's current date**, not on
the `asof_day` the caller passed in. `_sell_plan(asof_day=D)` computed the
trailing-stop peak through a helper that had **dropped `asof_day` on the floor**:

```text
_sell_plan(asof_day=D)
  └─ _position_peak(position, quote, price)          # asof_day never forwarded
       └─ _bought_today(position, asof_day=None)
            └─ _date(asof_day or dt.date.today())    # wall-clock fallback
```

Whenever `asof_day != machine today` (backfill, replay, historical audit), a
**same-day new position was no longer recognised as same-day**, so its peak
absorbed the pre-entry intraday `quote.high`. A position bought at 10.00 after a
12.00 morning spike instantly showed a ≥4% drawdown and fired a fabricated
`trailing_stop` (`sell_ratio` 0.0 → 1.0, exit `none` → `trailing_stop`). The
identical bug existed on the second path: `_intraday_downside_guard` called
`_position_peak` with no `asof_day` either.

This PR removes the wall-clock leak at its root **and** extracts the pure sell
risk state machine out of the 16k-line `paper_trading.py` god module into a
deterministic domain module.

## Two goals

1. **Correctness** — sell decisions become a pure function of
   `(position, quote, explicit asof_day, resolved policy)`. No machine date, no
   hidden fallback, no ambient state.
2. **Architecture** — the sell risk state machine (hard stop / trailing stop /
   max hold / staged take-profit / severity arbitration / peak-basis selection)
   moves to `backend/paper_risk_decision.py`, with a one-way dependency
   `paper_trading → paper_risk_decision`.

## Before-fix reproduction (unmodified base)

`work/r15_before_fix_repro.py` — pure fixture differential, no DB, no network,
no sleep, no clock patching beyond `mock.patch.object(PT, "dt", fake)`:

| Case | Scenario | Base result |
|---|---|---|
| R15-C1 | historical same-day new position absorbs pre-entry high | **REPRODUCED** |
| R15-C2 | same ledger + same `asof_day`, only machine today differs → different exit class | **REPRODUCED** |
| R15-C3 | overnight position must still absorb the intraday high | PASS (semantics must survive) |
| R15-C4 | partial same-day add-on keeps old-position peak semantics | PASS |

After the fix, C1/C2 report **NOT REPRODUCED** and C3/C4 still PASS — that is the
expected after-fix differential outcome.

## New module: `backend/paper_risk_decision.py`

Hard boundary (enforced by AST guard, not by convention):

- zero DB / network / filesystem / global cache / module-level mutable state;
- **zero wall-clock** — no `date.today()` / `datetime.now()` anywhere;
- no import of `paper_trading`, `strategy_policies`, `paper_account_specs`,
  `strategy_registry` — policy is **resolved by the caller and injected**;
- public API takes `asof_day` as **keyword-only and required**; omitting it
  raises `ValueError` instead of silently falling back to today;
- byte-for-byte identical output for identical input.

```python
bought_today(position, *, asof_day)
position_peak(position, quote, price, *, asof_day)
main_force_intent(position, quote, market=None, news=None)
evaluate_sell(position, quote, *, asof_day, spec, hold_days, news=(),
              hard_stop_touched_today=False, limit_pct=None,
              hard_stop_first_trim_ratio=0.35, risk_version="")
```

**Semantics preserved exactly** (these were the load-bearing rules, and they are
deliberately not "simplified"):

| Rule | Frozen meaning |
|---|---|
| same-day new position | `qty > 0 ∧ today_acquired_qty >= qty ∧ entry_date == asof_day` |
| peak basis, same-day | `max(authoritative peak, current price)` — pre-entry high ignored |
| peak basis, overnight / add-on | `max(authoritative peak, quote.high, current price)` |
| unknown take stage | `take_stage=None` → staged take-profit **skipped entirely**; never re-read as stage 0 via `int(x or 0)` |
| exit severity order | `none(0) < tactical_take_profit(1) < max_hold(2) < trailing_stop(3) < hard_stop(4)` — more severe wins |
| `limit_pct` | resolved by caller; the engine must not guess price limits |
| shadow evidence | `main_force_intent` / `shadow_news_warning_count` / `volatility_shadow` stay explainability-only, **never triggers** |

## `paper_trading.py` changes (net reduce)

- `_sell_plan` becomes a thin **orchestration adapter**: resolve `spec`,
  `hold_days`, `limit_pct`, `hard_stop_first_trim_ratio`, `risk_version` →
  call `PRD.evaluate_sell` → reassemble the old 4-tuple + `exit_profile` /
  `volatility_shadow` diagnostics. Return contract unchanged.
- `_intraday_downside_guard` now takes explicit `asof_day` and forwards it to
  `PRD.position_peak`.
- **Deleted** `_bought_today`, `_position_peak`, `_main_force_intent` — no
  forwarding wrappers remain (`FORBIDDEN_PAPER_TRADING_DEFS` blocks them from
  growing back).
- Deliberately **not** moved: `_intraday_downside_guard` (orchestration),
  `_position_quality_score`, `_concentration_action`, `_volatility_shadow`
  (reads K-lines), `_hold_days` (reads K-lines; computed by the wrapper).
- Size ratchet: **16365 → 16181 LOC**, **287 → 284 top-level defs**.

## Tests

- **`backend/test_paper_risk_decision.py` (NEW)** — RD-01 … RD-16 golden
  matrix: `bought_today` semantics, explicit-asof fail-fast, same-day vs
  overnight peak basis, as-of normalisation (date/datetime/ISO/blank), caller
  must supply `limit_pct`, missing quote is a no-op, hard-stop first-trim vs
  full clear, trailing stop on peak drawdown, severity arbitration
  (hard_stop outranks max_hold), unknown stage skips, and
  **RD-15: `_sell_plan` is machine-date independent** (two `dt` stubs, same
  result).
  **RD-16** covers the **second** production path — the real
  `PT._intraday_downside_guard`:
  - *RD-16*: historical same-day new position does **not** re-absorb the
    pre-entry `quote.high` (`peak_retrace_pct == 0.0`, `level == "none"`);
  - *RD-16b*: overnight position still absorbs the intraday high
    (`peak_retrace_pct == 12.5`, `level == "warning"`, `sell_ratio == 0.25`) —
    positive control, semantics not weakened;
  - *RD-16c*: guard output is identical under two different machine dates given
    the same explicit `asof_day` — so a future re-introduction of
    `date.today()` inside the guard diverges and fails.
- **`backend/test_paper_trading_architecture_guard.py`** — new
  `RiskDecisionModuleIsDeterministic` class: 6a zero project imports, 6b no I/O
  calls, 6c no wall-clock attribute reads, 6d `asof_day` keyword-only and
  required on every public entry, 6e `_sell_plan` must contain both
  `PRD.evaluate_sell(` and `asof_day=asof_day`. Guard 2b now also bans the
  three re-inlined helper names. Baselines ratcheted down, never up.
- **`backend/test_ignition_entry.py`** — `SameDayPeakTests` re-pointed at
  `PRD.bought_today` / `PRD.position_peak`.

## Non-vacuity (mutation check)

`work/r15_mutation_check.py` injects **13 byte-level mutations** into the real
production sources and runs the *specific* contract test for each, requiring
exit code ≠ 0 **and** `FAIL:`/`ERROR:` on that exact test method (so an
import/collection error cannot masquerade as a catch). Every file is restored
byte-identically and verified by sha256 in a `finally` block.

```text
RESULT: 13/13 mutations RED, all files restored byte-identical
```

| ID | Mutation | Caught by |
|---|---|---|
| M-RD1 | `bought_today` ignores `entry_date` | RD-01 |
| M-RD2 | `asof_day=None` falls back to machine today | RD-02 |
| M-RD3 | same-day peak absorbs pre-entry high | RD-03 |
| M-RD4 | overnight peak drops intraday high | RD-04 |
| M-RD5 | severity arbitration disabled | RD-13 |
| M-RD6 | hard-stop first touch clears everything | RD-10 |
| M-RD7 | unknown `take_stage` treated as stage 0 | RD-14 |
| M-RD8 | `limit_pct` silently defaulted | RD-07 |
| M-RD9 | trailing stop ignores drawdown | RD-12 |
| M-RD10 | no-quote treated as decidable | RD-08 |
| M-RD11 | `_position_peak` re-inlined into `paper_trading` | Guard 2b |
| M-RD12 | `_sell_plan` stops forwarding `asof_day` | Guard 6e |
| M-RD13 | **`_intraday_downside_guard` forwards the wrong `asof_day`** | **RD-16** |

M-RD13 deliberately mutates the *value* rather than deleting the argument:
dropping `asof_day=asof_day` would raise `TypeError` on the keyword-only
required parameter, which would only prove the signature still exists. Passing a
wrong-but-valid date makes `bought_today` return `False`, so the same-day
position re-absorbs the pre-entry high — the real R15 business defect — and fails
on the assertion, not on an exception.

### Harness correctness: cold bytecode cache

The runner passes a fresh `PYTHONPYCACHEPREFIX` per invocation. CPython validates
a `.pyc` using the source's mtime (**second** granularity) plus its byte length,
so two same-length mutations within the same second can make the second one reuse
the first's stale bytecode — the injection never executes and the test stays
green. This was not hypothetical: M-RD8 and M-RD9 both shift the file by −34
bytes and did collide, turning a genuinely-caught mutation into a false
`SUSPECT`. With the isolated cache the matrix is 13/13 RED.

## Verification (local)

| Check | Result |
|---|---|
| `unittest backend.test_paper_risk_decision` | 18 tests OK |
| `unittest backend.test_paper_trading_architecture_guard` | 15 tests OK |
| `unittest backend.test_rebalance_cycle_scope` | 29 tests OK |
| `unittest` (three modules above, together) | 62 tests OK |
| `work/r15_before_fix_repro.py` | C1/C2 NOT REPRODUCED, C3/C4 PASS |
| `work/r15_mutation_check.py` | 13/13 RED, restore byte-identical |
| `ruff check backend` | All checks passed |
| `python -m compileall -q backend` | exit 0 |
| `node --check` over `frontend/src/**/*.js` | exit 0 |
| `scripts/security/scan-sensitive-data.py --scope all` | `kinds: none / values: 0` |
| `unittest discover -s backend` | **3512 tests OK** (skipped=5) — no failures |

### The previously-red fixture now passes

```
$ python -m unittest backend.test_rebalance_cycle_scope -v
Ran 29 tests in 6.937s
OK
```

Root cause, retained here for the record (it was a wall-clock leak in a *test
fixture*, never in the code under this PR):

- the fixture seeded `scan_date="2026-09-19"` while `rebalance_scanner._date()`
  (→ `_now().date()`) returns the **machine's local date**, and the scanner writes
  rows with `today.isoformat()`;
- once the machine date rolled past `2026-09-19`, the seeded row and today's row
  landed on different `scan_date` values, so the query returned only one row;
- why no earlier CI run caught it: master's last green run was at
  `2026-09-19T15:55Z` (Shanghai 23:55, 09-19) — still inside the fixture's day.
  The `190da07` run at `2026-09-19T16:52Z` (Shanghai 00:52, **09-20**) crossed the
  boundary, so the same suite turned red.

Fixed test-only by freezing the scanner clock inside the case; production
`rebalance_scanner.py` is untouched.

## Documentation

`ARCHITECTURE.md`: new module row in 模块职责速查, new entry in 当前边界, and
new invariant **#16** freezing the boundary (zero I/O / zero wall-clock /
explicit `asof_day` / frozen semantics / one-way dependency / guard locations).

## Out of scope

- No strategy semantics change. `main_force_intent` / shadow news / volatility
  shadow remain explainability-only.
- No merge, no deploy. Awaiting human review.
