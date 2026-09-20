## ⛔ STATUS: DO NOT MERGE / DO NOT DEPLOY — awaiting human review

`MERGE: NOT MERGED` · `DEPLOY: NOT DEPLOYED`

## Review follow-up (round 2)

An automated review of head `8071253b` raised one actionable point, now fixed:

> **Resolve archived entry signals by their exact ID** — when a sliced entry
> fills its first slice but leaves later slices pending, `_buy_order` changes the
> originating signal to `deferred_capacity`; after 24 hours `_cleanup_stale_data`
> moves every such signal to `paper_signals_archive` and deletes it from
> `paper_signals`. This lookup then reports `signal_not_found` for an otherwise
> intact, verified opening-order chain, replacing the episode's real model score
> with 50 and potentially changing automatic rotation decisions.

Correct, and verified in the source before fixing: `_buy_order` leaves the signal
at `deferred_capacity` after the first slice fills (`paper_trading.py`, the
`if not slice_done:` branch) while the order is already `filled` and
execution-verified, and `_cleanup_stale_data` archives `deferred_capacity` rows
older than a day into `paper_signals_archive` (same columns, **same `id`**) and
deletes them from `paper_signals`. The chain is intact, so the episode's score is
still provable — reporting `unknown` there loses real provenance.

The exact-ID lookup now covers both tables, with **identical** identity and
as-of checks, and reports which row source answered:

```python
def _load_signal(conn, signal_id):
    for table in SIGNAL_SOURCES:              # paper_signals → paper_signals_archive
        row = conn.execute(f"SELECT {_SIGNAL_COLUMNS} FROM {table} WHERE id=?", ...)
```

This is still exact provenance, not a fallback: the archive row *is* the same
row (`id` preserved by `INSERT ... SELECT *`), and the shape is pinned statically
— the guard now rejects any use of `paper_signals_archive` outside `WHERE id=?`.
New regression **RP-15** (archived entry signal still resolves), **RP-16** (the
archive keeps every identity/as-of check), **RP-16b** (the archive is never a
latest fallback) and production regression **RISK-REVIEW-P10**, which drives the
**real** `_cleanup_stale_data` and asserts the real model score survives
(`90.0` / `episode_provenance`, not `50.0`). New mutations **M-PR14** (archive
lookup removed → RP-15 RED) and **M-PR15** (signal identity check removed →
RP-16 RED).

## Before / After

**Before** — Position quality resolved its original model score by querying the
latest `paper_signals` row for `(account_id, code)`:

```sql
SELECT rank_score,t_score,payload FROM paper_signals
 WHERE account_id=? AND code=?
 ORDER BY signal_date DESC,id DESC LIMIT 1
```

That row was not tied to the current position episode and was not bounded by
`asof_day`, so a later unrelated signal — including a future signal during
historical replay — could change an automatic concentration/rotation decision.

**After** — Position quality resolves entry-model evidence only through:

```text
paper_position_risk_state.opened_order_id
  -> verified cycle-owned BUY order   (paper_orders)
  -> exact paper_orders.signal_id
  -> exact paper_signals row          (WHERE id=?)
```

Missing provenance remains **unknown** and uses the existing neutral model-score
fallback (`model_score = 50.0`, `model_score_source = "unknown"`). No
latest-signal guess is allowed.

## Summary

`_position_quality_score` did not ask *"which signal opened this position
episode?"* — it asked *"what is the newest signal for this account+code?"*:

```text
cycle 8, tq_breakout, 600000

  signal A score=90 -> verified BUY order 101 (cycle_id=8, signal_id=A)
                    -> paper_position_risk_state.opened_order_id = 101

  later: signal B score=10 (same account+code, does NOT open anything)

  _position_quality_score()  ORDER BY signal_date DESC
      -> picks B
      -> this episode's model_score silently 90 -> 10
```

`model_score` is **not** a display field. It feeds

```text
score = model*0.35 + trend*0.05 + flow*0.25 + momentum*0.25 + return*0.10 - news_penalty
      -> _concentration_action
      -> watch / hold / consolidation_exit
      -> real automatic sells / rotation
```

so a wrong signal changes real automatic trading. The second defect rides on the
same query: there is no `signal_date <= asof_day` bound, so an historical
`asof_day` can read a **future** signal (future leakage).

This PR fixes the provenance **and** extracts the pure position-review domain out
of the 16k-line `paper_trading.py`.

## Two goals

1. **Correctness** — the original model score of the current position episode may
   only come from its **real opening order's signal provenance**; a missing chain
   is `unknown`, never a guess.
2. **Architecture** — the pure scoring arithmetic and the concentration/rotation
   decision move to `backend/paper_position_review.py` (zero I/O, zero wall
   clock, zero project imports), with the read-only provenance resolver in
   `backend/paper_position_review_evidence.py`.

## Why not just add `cycle_id` to `paper_signals`

`paper_signals` has no `cycle_id` column, and which cycle a historical signal
belonged to **cannot** be inferred from its date, from the current
`paper_accounts.cycle_id`, or from `MAX(cycle_id)`. Backfilling it would be
provenance fabrication. The authoritative episode fact already exists —
`paper_position_risk_state.opened_order_id` (established by #170) — and this PR
uses it instead. **No migration in this PR.**

## Before-fix reproduction (unmodified base `2ab817b`)

`work/r17_before_fix_repro.py`, run in a clean `git worktree` of the base commit:

```text
R17-C1 unrelated later signal hijacks current episode: REPRODUCED
    actual : model_score=10.0 (episode signal A=1 order=1); source=None

R17-C2 future signal leaks into historical asof: REPRODUCED
    actual : asof=2026-09-10 model_score=10.0 (future signal B dated 2026-09-11 must not leak)

R17-C3 wrong provenance changes automatic action: REPRODUCED
    actual : score 100.0->0.0; action hold->consolidation_exit (持仓质量评分 32.5 低于淘汰线 38)

R17-C4 missing episode provenance guesses latest signal: REPRODUCED
    actual : model_score=0.0 source=None (no risk_state → provenance unknown → must be 50.0)

R17-C5 add-on does not change episode origin: REPRODUCED
    actual : opened_order_id=1 (A=1, B=2); model_score=10.0

R17-C6 full exit + re-entry uses new provenance: NOT REPRODUCED
    actual : opened_order_id=2 (old A=1, new C=2); model_score=20.0

SUMMARY: 5/6 reproduced on this tree
```

**C3 is the execution-level error in full**: the wrong provenance moved the real
score from `100.0` to `0.0` and flipped the real action from `hold` to
`consolidation_exit` — an automatic sell that would not otherwise happen.

Before-fix evidence:

```text
R17-C1 unrelated later signal hijacks current episode: REPRODUCED
R17-C2 future signal leaks into historical asof: REPRODUCED
R17-C3 wrong signal changes concentration action: REPRODUCED
R17-C4 missing episode provenance guesses latest signal: REPRODUCED
```

`C5` / `C6` are positive lifecycle contracts (add-on keeps the episode origin;
full exit + re-entry takes a new origin); they are not required to fail on the
base, and `C6` already holds.

After the fix, the same script on this head reports:

```text
R17-C1 ...: NOT REPRODUCED   model_score=90.0 ...; source=episode_provenance
R17-C2 ...: NOT REPRODUCED   asof=2026-09-10 model_score=90.0
R17-C3 ...: NOT REPRODUCED   score 100.0->100.0; action hold->hold (评分 67.5)
R17-C4 ...: NOT REPRODUCED   model_score=50.0 source=unknown
R17-C5 ...: NOT REPRODUCED   opened_order_id=1 ...; model_score=90.0
R17-C6 ...: NOT REPRODUCED   opened_order_id=2 ...; model_score=20.0
SUMMARY: 0/6 reproduced on this tree
```

## Episode provenance chain

```text
current authoritative lot position            (paper_position_lots)
  -> same-cycle paper_position_risk_state    (episode origin authority)
  -> opened_order_id
  -> paper_orders.id
       verify: order.cycle_id == claimed cycle
               order.account_id == position.account_id
               order.code == position.code
               order.side == 'buy'
               order.status == 'filled'
               execution_verified == 1 / execution_status == 'verified'
  -> paper_orders.signal_id
  -> paper_signals.id                        (exact row, WHERE id=?)
       verify: signal.account_id == account_id
               signal.code == code
               signal_date <= asof_day
  -> original episode model score
```

Not `account + code + latest date`.

## New module: `backend/paper_position_review_evidence.py`

Read-only, single responsibility: resolve the entry signal of the **current
position episode**.

```python
resolve_entry_signal(conn, *, cycle_id, account_id, code, opened_order_id, asof_day)
```

`cycle_id` and `asof_day` are explicit keyword-only inputs — the module never
resolves the active cycle and never reads a clock. `verified` is decided by the
existing `execution_verification.VERIFIED_PREDICATE` (`EV.is_verified_row`), not
by a second, reinvented definition.

Result on success:

```python
{"status": "verified", "opened_order_id": 101, "signal_id": 55,
 "signal_date": "2026-09-10", "signal_source": "paper_signals",
 "signal": {...}, "provenance_version": ...}
```

The exact-ID lookup covers `paper_signals` **and** `paper_signals_archive`: a
sliced entry leaves its (already filled) signal at `deferred_capacity`, and
`_cleanup_stale_data` later moves that same row — same `id` — into the archive.
Reading the archive is still exact provenance, so the episode keeps its real
model score instead of degrading to the neutral `50`. Identity and as-of checks
are applied identically to archival rows, and the archive is never searched by
account/code.

Result on any break in the chain (`status="unknown"` + explicit reason):

| reason | meaning |
|---|---|
| `missing_opened_order_id` | no episode origin can be proven |
| `order_not_found` | the referenced order does not exist |
| `order_cycle_mismatch` | order belongs to another cycle |
| `order_identity_mismatch` | order account/code ≠ position account/code |
| `order_not_buy` | not a BUY |
| `order_not_filled` | not filled |
| `order_unverified` | execution not verified |
| `missing_signal_id` | order carries no signal |
| `signal_not_found` | signal row absent |
| `signal_identity_mismatch` | signal account/code ≠ position |
| `signal_after_asof` | signal is dated after the requested as-of day |

**The resolver can never fall back.** There is no
`WHERE account_id=? AND code=? ORDER BY signal_date DESC LIMIT 1` in it, no
"missing `opened_order_id` → search latest", and no "missing `signal_id` →
search latest". Unknown stays unknown, and the caller applies the pre-existing
neutral score.

## New module: `backend/paper_position_review.py`

The deterministic review domain. Hard boundary, enforced by AST guard rather
than convention:

- zero DB / network / filesystem / subprocess;
- zero wall clock (`date.today` / `datetime.now` / `time.time` all absent) —
  `hold_days` is passed in;
- imports only `dataclasses` / `typing` (stdlib) — **no** `paper_trading`,
  `strategy_policies`, `paper_position_read_model`, `data_fetcher`;
- identical input ⇒ identical output.

```python
score_quality(*, model_score, trend_score, flow_score, momentum_score,
              return_score, news_penalty, weights, hold_days)
    -> {"score", "grade", "trend_for_score", "review_phase"}

decide_action(review, position, quote_status, sells_used, *, policy)
    -> (action, reason)
```

`score_quality` is semantically line-for-line the old arithmetic:

```text
score = model_score*weights["model"] + trend_for_score*weights["trend"]
      + flow_score*weights["flow"] + momentum_score*weights["momentum"]
      + return_score*weights["return"] - news_penalty        (clamped to 0..100)
```

with `hold_days < 1 → trend_for_score = 50.0` preserved (a fresh position must
not be scored against its raw trend), and the grade ladder unchanged
(`建仓复核` / `核心 ≥65` / `观察 ≥50` / `减仓 ≥40` / `淘汰`).

`ReviewPolicy` (frozen dataclass) carries the thresholds, so the pure module
never parses account configuration. The production adapter builds it once as a
module constant `REVIEW_POLICY` from the existing `LOT_SIZE` / `POSITION_*` /
`SLOT_UPGRADE_*` constants — **no threshold value changed**.

Decision precedence is preserved item for item:

```text
stale quote -> quote_pending
missing score -> review_pending
available_qty < LOT_SIZE -> t1_locked
max sells per run reached -> queued
urgent slot upgrade -> consolidation_exit
minimum observation period -> new_position
trend_pullback without confirmation -> watch
score <= exit threshold -> consolidation_exit
daily rotation quota reached -> queued
valid replacement / full-slot upgrade -> consolidation_exit
weak candidate edge -> watch
otherwise -> hold
```

## Authority matrix

```text
Position quantity: paper_position_lots
Position runtime / episode origin: paper_position_risk_state
Entry-model provenance for current episode: opened_order_id → verified paper_orders → exact paper_signals.id
Protective sell decision: paper_risk_decision
Position quality score/action: paper_position_review
Risk-scan lifecycle: paper_risk_scan_runs
Compatibility projection: paper_positions (zero execution authority)
```

Not changed by this PR: `paper_position_lots` remains the quantity authority,
`paper_position_risk_state` remains peak / take-stage / episode-origin authority,
`paper_risk_decision` remains the protective-sell authority, `paper_risk_scan_runs`
remains the risk-scan lifecycle authority, and `paper_positions` keeps **zero**
execution authority. Provenance only touches position quality / concentration
rotation — hard stop, trailing stop, take profit, max hold, intraday downside
guard, permission exit and capacity exit behave exactly as before.

## `paper_portfolio` / read model

`aggregate_positions` already merged `peak_price` / `take_stage` from the
same-cycle `paper_position_risk_state`; it now also exposes
`episode_opened_order_id` (`None` when the state row is missing). The name is
deliberately not `order_id`, to avoid confusion with sell / current orders.
`peak_price` / `take_stage` / `risk_state_source` keep their existing semantics.

The origin is **never** re-derived from `paper_position_lots.source_order_id` —
every add-on lot has a different one, and MIN / MAX / latest are all wrong.

## `paper_trading.py` changes (net reduce)

- `_position_quality_score(conn, position, quote, asof_day, *, cycle_id, ...)` —
  `cycle_id` is keyword-only and **required**; there is no `cycle_id=None`, no
  `_active_cycle()`, no `MAX(cycle_id)`, no `paper_accounts.cycle_id` fallback.
  `monitor_risk` passes the explicit claimed cycle it already owns (R16).
- The old latest-signal SELECT is **gone**; entry evidence now comes from
  `PREV.resolve_entry_signal(...)`, and the verified case keeps the existing
  `max(_score100(t_score), _score100(entry_model.score), _score100(rank_score))`
  semantics (no weighted composite was introduced).
- The review dict now carries `model_score_source`, `episode_opened_order_id`,
  `entry_signal_id`, `entry_signal_date`, `entry_signal_provenance_status`,
  `entry_signal_provenance_reason`, so the dashboard/audit can explain *why* a
  model score is `50` instead of it being a black box.
- **Removed** `_concentration_action` (-94 lines) with **no** compatibility
  wrapper; the two call sites call `PReview.decide_action(...)` directly.
- `_save_position_review` no longer has a wall-clock fallback: `review_date` must
  be explicit, otherwise `ValueError`. `monitor_risk` already sets it, so a
  historical as-of review can never be silently re-dated to "today".
- Deliberately **not** moved: `_save_position_review` persistence itself, the
  rest of `_monitor_risk_impl`, `_best_replacement_candidate` provenance (a
  further convergence step, not this PR).

Size ratchet per §94:

```text
Before: 16179 / 284
After:  16134 / 283
Delta:  -45 / -1
```

## Tests

- **`backend/test_paper_position_review.py` (NEW, 27 tests)** — PR-01 … PR-15:
  score formula equivalence, new-position trend neutralisation, grade
  boundaries, quote pending, T+1 lock, per-run sell cap, urgent slot upgrade,
  minimum observation window, `trend_pullback` confirmation gate, absolute exit
  score, daily rotation quota, replacement edge, weak-candidate watch, healthy
  hold, same-input-same-output determinism, explicit threshold boundaries
  (`38 / 38.01 / 40 / 50 / 65`, so a `<=` → `<` drift cannot slip through), plus
  module-boundary self-checks.
- **`backend/test_position_review_provenance.py` (NEW, 30 tests)** —
  resolver contract **RP-01 … RP-16** (verified opening order resolves the exact
  signal; later same-account/code signal ignored; future signal ignored;
  wrong-cycle order rejected; wrong-account order rejected; wrong-code order
  rejected; unverified order rejected; non-filled order rejected; missing
  `signal_id`; missing signal row; signal identity mismatch; missing
  `opened_order_id`; add-on does not replace origin; full-exit + re-entry uses
  the new origin; archived entry signal still resolves by exact id; archived rows
  keep identity/as-of checks and are never a latest fallback) and a static
  "never latest-search" assertion.
  Production regressions **RISK-REVIEW-P1 … P10** drive the real
  `PT._position_quality_score` / real action decision: a later unrelated signal
  must not change the model score (**P1**), a future episode signal must stay
  unknown under an historical as-of (**P2b**), missing provenance must not guess
  (**P3/P3b**), the wrong provenance must not manufacture a false
  `consolidation_exit` (**P4**), add-on keeps the origin (**P5**), re-entry takes
  a new origin (**P6**), `episode_opened_order_id` is exposed (**P7**),
  `review_date` must be explicit (**P8**), protective exits are unaffected
  (**P9**), and an episode signal archived by the **real** `_cleanup_stale_data`
  keeps its real model score (**P10**).
- **`backend/test_paper_trading_architecture_guard.py`** — new **Guard 8**
  (`PositionReviewIsProvenanceBound`): 8a pure review module has zero project
  imports, 8b zero I/O and zero wall clock, 8c evidence module has no reverse
  dependency on `paper_trading`, 8d the resolver contains no latest-search
  fallback (and touches `paper_signals_archive` only as `WHERE id=?`), 8e
  `_position_quality_score.cycle_id` is keyword-only + required,
  8f the adapter must use `PREV.resolve_entry_signal` and the old SQL must not
  reappear, 8g no `ORDER BY signal_date DESC` in `paper_trading`, 8h
  `_save_position_review` has no `date.today()` fallback. Baseline ratcheted
  **down** to 16134 / 283.

## Non-vacuity (mutation check)

`work/r17_mutation_check.py` injects **15 byte-level mutations** into real
production sources and requires the *specific* contract test to fail with
`FAIL:`/`ERROR:` on that exact method, so an import/collection error cannot
masquerade as a catch. Every file is restored byte-identically and verified by
sha256 in a `finally` block, with a fresh `PYTHONPYCACHEPREFIX` per run so a
same-length mutation in the same second cannot reuse stale bytecode.

```text
RESULT: 15/15 mutations RED, all files restored byte-identical
```

| ID | Mutation | Caught by |
|---|---|---|
| M-PR1 | resolver reverted to latest account+code signal | RISK-REVIEW-P1 |
| M-PR2 | resolver stops validating `order.cycle_id` | RP-04 |
| M-PR3 | resolver stops validating `execution_verified` | RP-07 |
| M-PR4 | resolver stops validating order account/code | RP-05 |
| M-PR5 | resolver stops checking `signal_date <= asof` | RISK-REVIEW-P2b |
| M-PR6 | missing `opened_order_id` falls back to latest signal | RISK-REVIEW-P3b |
| M-PR7 | signal identity mismatch still accepted | RP-11 |
| M-PR8 | new position scored on raw trend instead of neutral 50 | PR-02 |
| M-PR9 | T+1 lock still allows concentration exit | PR-05 |
| M-PR10 | `trend_pullback` confirmation gate removed | PR-09 |
| M-PR11 | `score <= exit_threshold` changed to `<` | threshold boundary |
| M-PR12 | `paper_position_review` reverse-imports `paper_trading` | Guard 8a |
| M-PR13 | `_save_position_review` falls back to wall clock | Guard 8h |
| M-PR14 | archived entry signal no longer resolved by exact id | RP-15 |
| M-PR15 | signal identity check removed (archive rows included) | RP-16 |

**M-PR1 is the core business mutation**, not a signature check: reverting the
chain to "latest account+code signal" makes the R17-C1 production regression go
RED. **M-PR5** proves the future-leakage bound: removing `signal_date <= asof`
makes the historical-as-of regression go RED on a wrong `model_score`.

## Verification (local)

| Check | Result |
|---|---|
| `unittest backend.test_paper_position_review` + provenance + guard | 88 tests OK |
| targeted set (8 modules) | 239 tests OK |
| strategy + golden replay set (9 modules) | 116 tests OK |
| `unittest discover -s backend` | **3634 tests OK** (skipped=5) |
| `work/r17_before_fix_repro.py` | base 5/6 REPRODUCED → fixed 0/6 |
| `work/r17_mutation_check.py` | 15/15 RED, restore byte-identical |
| `ruff check backend` | All checks passed |
| `python -m compileall -q backend` | exit 0 |
| `npm run build` + `git diff --exit-code -- frontend/dist` | exit 0 (no drift) |
| `npm run test:unit` | 111 pass / 0 fail |
| `node --check` over `frontend/src/**/*.js` | 17 files, exit 0 |
| `scripts/security/scan-sensitive-data.py --scope worktree` | `kinds: none / values: 0` |
| `scripts/security/scan-sensitive-data.py --scope all` | `kinds: none / values: 0` |

Golden replay (`test_production_path_golden_replay`, `test_demo_replay_golden`)
shows **no snapshot drift**: live fixtures carry complete provenance, so the
correct semantics are unchanged where the chain is intact.

## Documentation

`ARCHITECTURE.md`: new module rows (持仓复核决策 / 入场模型 provenance), new
invariant **#18** freezing the single-direction dependency
(`paper_trading → evidence → orders/signals`, `paper_trading → pure review`) and
stating the rule explicitly:

> Position review may never resolve an entry model by "latest signal for
> account+code". Current episode provenance is:
> `risk_state.opened_order_id → verified buy order → exact signal_id`.

## Out of scope

- No strategy changes, no threshold changes, no sell/rotation threshold changes,
  no dynamic-slot, T+1, tradability or execution-verification changes.
- No selection rework and no whole-`_monitor_risk_impl` move.
- No migration — `opened_order_id`, `order.cycle_id` and `order.signal_id`
  already exist; nothing was added to the schema.
- `_best_replacement_candidate` provenance (which also reads `paper_signals`
  without cycle ownership) is intentionally **not** touched here — it is a
  separate convergence step.
- No merge, no deploy. Awaiting human review.