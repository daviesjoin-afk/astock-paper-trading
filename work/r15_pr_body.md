## ⛔ STATUS: DO NOT MERGE / DO NOT DEPLOY — awaiting human review

`MERGE: NOT MERGED` · `DEPLOY: NOT DEPLOYED`

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

- **`backend/test_paper_risk_decision.py` (NEW)** — RD-01 … RD-15 golden
  matrix: `bought_today` semantics, explicit-asof fail-fast, same-day vs
  overnight peak basis, as-of normalisation (date/datetime/ISO/blank), caller
  must supply `limit_pct`, missing quote is a no-op, hard-stop first-trim vs
  full clear, trailing stop on peak drawdown, severity arbitration
  (hard_stop outranks max_hold), unknown stage skips, and
  **RD-15: `_sell_plan` is machine-date independent** (two `dt` stubs, same
  result).
- **`backend/test_paper_trading_architecture_guard.py`** — new
  `RiskDecisionModuleIsDeterministic` class: 6a zero project imports, 6b no I/O
  calls, 6c no wall-clock attribute reads, 6d `asof_day` keyword-only and
  required on every public entry, 6e `_sell_plan` must contain both
  `PRD.evaluate_sell(` and `asof_day=asof_day`. Guard 2b now also bans the
  three re-inlined helper names. Baselines ratcheted down, never up.
- **`backend/test_ignition_entry.py`** — `SameDayPeakTests` re-pointed at
  `PRD.bought_today` / `PRD.position_peak`.

## Non-vacuity (mutation check)

`work/r15_mutation_check.py` injects **12 byte-level mutations** into the real
production sources and runs the *specific* contract test for each, requiring
exit code ≠ 0 **and** `FAIL:`/`ERROR:` on that exact test method (so an
import/collection error cannot masquerade as a catch). Every file is restored
byte-identically and verified by sha256 in a `finally` block.

```text
RESULT: 12/12 mutations RED, all files restored byte-identical
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

## Verification (local)

| Check | Result |
|---|---|
| `unittest backend.test_paper_risk_decision` | 15 tests OK |
| `unittest backend.test_paper_trading_architecture_guard` | 15 tests OK |
| `unittest backend.test_paper_risk_decision backend.test_paper_trading_architecture_guard` | 30 tests OK |
| `work/r15_before_fix_repro.py` | C1/C2 NOT REPRODUCED, C3/C4 PASS |
| `work/r15_mutation_check.py` | 12/12 RED, restore byte-identical |
| `ruff check backend` | All checks passed |
| `python -m compileall -q backend` | exit 0 |
| `node --check` over `frontend/src/**/*.js` | exit 0 |
| `scripts/security/scan-sensitive-data.py --scope all` | `kinds: none / values: 0` |
| `unittest discover -s backend` | 3509 tests, **1 pre-existing failure** (see below) |

### Pre-existing failure, not caused by this PR

`test_rebalance_cycle_scope.SameDayRolloverKeepsBothRows.test_two_cycles_same_day_coexist`
fails on the **unmodified base SHA `06197d76`** as well (reproduced in a clean
`git worktree` of the base commit). It is a date-brittle fixture in the rebalance
scanner suite — unrelated to sell-risk decisions — and no CI run on master has
observed it yet because it depends on the local machine date. Reported honestly
rather than papered over; it is **out of scope** for this PR.

## Documentation

`ARCHITECTURE.md`: new module row in 模块职责速查, new entry in 当前边界, and
new invariant **#16** freezing the boundary (zero I/O / zero wall-clock /
explicit `asof_day` / frozen semantics / one-way dependency / guard locations).

## Out of scope

- No strategy semantics change. `main_force_intent` / shadow news / volatility
  shadow remain explainability-only.
- No merge, no deploy. Awaiting human review.
