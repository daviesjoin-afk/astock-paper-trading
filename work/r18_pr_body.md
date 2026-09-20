## ⛔ STATUS: DO NOT MERGE / DO NOT DEPLOY — awaiting human review

`MERGE: NOT MERGED` · `DEPLOY: NOT DEPLOYED`

## Review follow-up (round 2)

An automated review of head `5f03a185` raised one actionable point, now fixed:

> **Bind the allocation budget to the supplied cycle** — when the caller supplies
> a non-active cycle (the rollover case this change is intended to handle),
> `resolved_cycle_id` only bounded the review lookup: `_dynamic_position_limits()`
> still calls `_active_cycle()` and therefore derives `target_limit` and donors
> from the newer active cycle. `_apply_slot_borrow` repeated this split, pairing
> the active cycle's `allocation_version` with the explicit cycle in its row
> query, so a valid same-cycle borrow is incorrectly rejected as missing.

Correct, and verified in the source before fixing: `_dynamic_position_limits`
opened with `cycle = _active_cycle(conn)` and used it for both the version-row
lookup and the insert, so the budget was always the **active** cycle's while the
reviews and position counts had already moved to the explicit one. In the
rollover case this made a legitimate same-cycle borrow fail closed with
"未找到当前席位版本，暂不借位" — a silent loss of the borrow, not a safety win.

`_dynamic_position_limits(conn, *, cycle_id=None)` now takes an optional explicit
cycle (`None` keeps the pre-existing "whatever is active now" semantics for the
cold-start allocation, capacity exit and dashboard readers), and the whole
in-flight order chain passes its claimed cycle through:

```text
_dynamic_position_limits(conn, cycle_id=current_cycle.id)   # in _buy_order
  → _slot_upgrade_context(..., cycle_id=current_cycle.id)
  → _apply_slot_borrow(..., cycle_id=current_cycle.id)
  → position_limit_version read/write on that same cycle
  → _rollback_slot_borrow(..., cycle_id=current_cycle.id)
```

New production regression **RPL-P5d** pins it without mocking the budget: with
cycle 9 active and the allocation only existing for cycle 8, a cycle-8 borrow
must succeed and must write cycle 8's version row. New mutation **M-RPL15**
reverts the budget call to the active cycle and P5d goes RED. New **Guard 9m**
statically requires `_slot_upgrade_context` / `_apply_slot_borrow` to pass
`cycle_id` into the budget lookup.

## Before / After

**Before** — A risk pass could select a replacement signal intended for the next
trading day. That future candidate could provide enough score edge to trigger a
same-day `consolidation_exit`, while the subsequent real BUY path rejected the
same signal because `signal_freshness` requires its `intended_date` to equal the
current as-of day.

Slot-upgrade context also re-resolved the active cycle and read the latest
position review without an as-of upper bound.

**After** — Replacement evidence used for a same-day sell is restricted to
active signals whose `intended_date` equals the explicit `asof_day` and whose
`signal_date` is not in the future.

Slot-upgrade/borrow/rollback use one explicit `cycle_id`, and holding review
evidence is read only from that cycle at `review_date <= asof_day`.

Final replacement BUY execution still goes through the existing `_buy_order`
gate; this PR does not duplicate or weaken execution checks.

## Summary

`_best_replacement_candidate` picked *today's* replacement with a **next-day
range**:

```python
next_day = _next_weekday(day).isoformat()
SELECT ... FROM paper_signals
 WHERE account_id=? AND status IN (...)
   AND intended_date>=? AND intended_date<=?      -- today .. tomorrow
```

So a signal that is only *intended for tomorrow* could be today's replacement,
and the chain is deterministic, not "the buy occasionally fails":

```text
monitor_risk(D)
  → selects signal with intended_date = D+1
  → replacement_score high → consolidation_exit
  → TODAY'S REAL HOLDING IS SOLD
  → _rotation_buy_candidate → _buy_order(asof_day=D)
  → ELC.signal_freshness: intended_date != D ⇒ usable=False
      "信号属于 D+1，禁止使用旧信号开新仓"
  → the D+1 candidate is additionally marked expired today
```

Today's position is sold purely because of tomorrow's candidate — and that
candidate is then pre-emptively voided. Two further defects live in the same
helpers: `_slot_upgrade_context` re-resolved the active cycle itself, and read
the newest review with **no as-of upper bound**, so `asof=D` could read a `D+1`
review (real future leakage).

This PR makes replacement evidence **as-of and cycle bounded**, and extracts the
pure candidate scoring / slot comparison out of the 16k-line `paper_trading.py`.

## Two goals

1. **Correctness** — a candidate may influence a same-day sell only if it
   belongs to that same as-of day; and slot-upgrade evidence is bounded by
   `(cycle_id, review_date <= asof_day)`.
2. **Architecture** — candidate scoring and slot-upgrade comparison move to
   `backend/paper_replacement_decision.py` (zero I/O, zero wall clock, zero
   project imports), with the bounded reads in
   `backend/paper_replacement_evidence.py`.

## Core invariants

```text
A candidate may influence a same-day sell only if
candidate.intended_date == asof_day
and candidate.signal_date <= asof_day.

Slot-upgrade review evidence must be bounded by
(cycle_id, review_date <= asof_day).
```

Both are **equality / upper bound**, not a range and not "take the newest row".
A legal overnight plan (`signal_date = D-1`, `intended_date = D`) therefore stays
**usable** — the fix does not ban overnight planning.

## Before-fix reproduction (unmodified base `f979a16`)

`work/r18_before_fix_repro.py`, run in a clean `git worktree` of the base commit:

```text
R18-C1 tomorrow candidate selected for today: REPRODUCED
    actual : selector returned signal_id=1 intended_date=2026-09-11 (future signal=1 intended 2026-09-11); asof=2026-09-10

R18-C2 tomorrow candidate triggers today rotation/sell: REPRODUCED
    actual : selected_future=True action=consolidation_exit review_score=50.0 replacement_score=100.0
             rotation_status=signal_expired
             rotation_reason='信号属于 2026-09-11，禁止使用旧信号开新仓'
             reason='低质量小仓换仓：评分 50.0，后备候选原始高 50.0 分，扣执行缓冲后高 47.0 分'

R18-C3 historical slot context reads future review: REPRODUCED
    actual : asof=2026-09-10 weakest_score=20.0 (D review=70, D+1 review=20; expected 70) state=upgrade_ready

R18-C4 slot context re-resolves active cycle: REPRODUCED
    actual : requested cycle=1 active_cycle=2 weakest_score=20.0 (cycle8=70, cycle9=20; expected 70)

SUMMARY: 4/4 reproduced on this tree
```

**C2 is the full production chain, not a partial one**: the future candidate
flipped the real decision to `consolidation_exit`, and the real replacement BUY
then rejected that same signal with exactly the date-mismatch reason
(`signal_expired` / "信号属于 2026-09-11，禁止使用旧信号开新仓"). Today sells for a
candidate that today cannot buy.

After the fix, the same script on this head reports:

```text
R18-C1 ...: NOT REPRODUCED   selector returned signal_id=None (future signal=1 ...); asof=2026-09-10
R18-C2 ...: NOT REPRODUCED   selected_future=False action=watch replacement_score=None
R18-C3 ...: NOT REPRODUCED   asof=2026-09-10 weakest_score=70.0 ... state=edge_insufficient
R18-C4 ...: NOT REPRODUCED   requested cycle=1 active_cycle=2 weakest_score=70.0 ...
SUMMARY: 0/4 reproduced on this tree
```

## New module: `backend/paper_replacement_evidence.py`

The single **bounded**, read-only source of replacement facts.

```python
load_replacement_candidates(conn, *, account_id, asof_day, statuses)
latest_position_review(conn, *, cycle_id, account_id, code, asof_day)
```

```sql
-- candidates: equality + upper bound (NOT a range)
SELECT ... FROM paper_signals
 WHERE account_id=? AND status IN (...)
   AND intended_date=?        -- == asof_day
   AND signal_date<=?         -- evidence may not come from the future

-- historical review: explicit cycle + as-of upper bound
SELECT score,action,review_date FROM paper_position_reviews
 WHERE cycle_id=? AND account_id=? AND code=?
   AND review_date<=?
 ORDER BY review_date DESC,id DESC LIMIT 1
```

`cycle_id` / `asof_day` are always explicit. Not found ⇒ `None` / empty — **no
fallback** to another cycle, another date, or the global newest row.

**The archive does not participate.** `paper_signals_archive` is historical
opening evidence (R17's episode provenance legitimately reads it), but it is not
an executable candidate: an archived signal must never be revived as a
replacement.

## New module: `backend/paper_replacement_decision.py`

```python
score_candidate(signal)                                     # 0..100 composite
choose_best_candidate(candidates, *, held_codes)            # strongest, held excluded
derive_donors(*, limits, counts, account_id, pool_limit,
              occupied_pool_count, policy)                  # donor arithmetic
decide_slot_upgrade(*, candidate_score, weakest, target_limit, donors,
                    at_dynamic_limit, min_hold_days, policy) # comparison/state
```

Hard boundary, enforced by AST guard: zero DB / network / filesystem / wall
clock; imports only `dataclasses` / `json` / `typing` — **no** `paper_trading`,
`paper_position_review`, `strategy_policies`, `sqlite3`; identical input ⇒
identical output.

The candidate score formula is preserved **verbatim** — including the 0..1 /
0..100 normalization, the clamp and `round(..., 2)`:

```text
entry_model.score * 0.45 + t_score * 0.35 + rank_score * 0.20
```

Decision precedence is preserved item for item:

```text
borrow eligibility → weakest position → T+1 lock → minimum hold
  → urgent upgrade → normal / full-slot upgrade → edge insufficient
```

In particular **T+1 lock is not bypassed by a strong candidate**.

`ReplacementPolicy` (frozen dataclass) carries the thresholds, so the pure module
never parses account configuration. The adapter builds it once as a module
constant `REPLACEMENT_POLICY` from the existing constants — **no threshold value
changed**.

### Replacement decision is deliberately NOT merged with position review

`paper_position_review` owns **holding** review (watch / hold /
consolidation_exit). This module owns **candidate** scoring and seat comparison.
The two score scales differ on purpose:

```text
candidate: entry 45% / t_score 35% / rank 20%
holding:   model / trend / flow / momentum / return / news
```

Merging them into one "unified score" would silently change both semantics.

## `paper_trading.py` changes (net reduce)

- `_best_replacement_candidate` = `PREPL.load_replacement_candidates` (bounded)
  → `_security_scope` filter (still the adapter's job) →
  `PRep.choose_best_candidate`. The `today → next_weekday` range SQL is **gone**.
- `_slot_upgrade_context(..., *, cycle_id)` / `_apply_slot_borrow(..., *,
  cycle_id)` / `_rollback_slot_borrow(conn, borrow, *, cycle_id)` — `cycle_id`
  is **keyword-only and required**, and the function bodies no longer call
  `_active_cycle()`. `_buy_order` passes the `current_cycle` it already proved.
- A borrow lifecycle is now `same cycle in → same cycle out`: borrow and rollback
  locate the seat-version row by the **explicit** cycle, and the donor position
  count is read via `PPRM.positions_for_cycle(conn, resolved_cycle_id)`.
- **Removed** `_replacement_score_from_signal` with **no** compatibility wrapper;
  production calls `PRep.score_candidate` directly.
- Deliberately **not** changed: `_rotation_buy_candidate` still delegates to the
  single `_buy_order` gate; `_save_position_review`, the rest of
  `_monitor_risk_impl` and the R17 episode provenance are untouched.

Size ratchet per §104:

```text
Before: 16134 / 283
After:  16049 / 282
Delta:  -85 / -1
```

## Authority matrix

```text
Position quantity: paper_position_lots
Episode origin: paper_position_risk_state
Position review: paper_position_review
Replacement candidate evidence: paper_signals active rows, same intended trading day only
Replacement/slot decision: paper_replacement_decision
Slot review evidence: paper_position_reviews, explicit cycle + asof bound
Risk scan lifecycle: paper_risk_scan_runs
Compatibility position projection: paper_positions
```

## Tests

- **`backend/test_paper_replacement_decision.py` (NEW, 42 tests)** — RD-1 … RD-15,
  plus explicit threshold boundaries (`candidate == borrow_min_candidate`,
  `edge == borrow_min_edge`, `candidate == upgrade_min_candidate`,
  `edge == upgrade_min_edge`, `net_edge == full_cap_edge`,
  `weakest == full_cap_max_score`, `weakest == any_replace_score`,
  `weakest == replace_score`, `weakest == exit_score`) so a `>=`/`>` or `<`/`<=`
  drift cannot slip through. Also asserts the policy defaults equal the
  production constants, that the adapter and the pure module agree on the score,
  and the module's zero-project-import / zero-I/O / zero-clock boundary.
- **`backend/test_replacement_asof_provenance.py` (NEW, 28 tests)** — evidence
  contract **RP2-01 … RP2-11** (same-day candidate loaded; tomorrow candidate
  excluded; **legal overnight plan still allowed**; future `signal_date` excluded
  even when `intended_date == D`; past intended_date excluded; status filter;
  account isolation; review as-of bound; review explicit-cycle bound; missing
  review ⇒ `None`; static "no next-day range / no archive").
  Production regressions **RPL-P1 … RPL-P7** drive the real functions:
  **P1** a tomorrow-only strong candidate cannot sell today's holding,
  **P2** a same-day strong candidate still rotates (the fix is not "everything
  is always None"), **P3** a legal overnight candidate still influences today's
  decision, **P4** a future review cannot create `slot_upgrade_ready`,
  **P5/P6** borrow and rollback never touch another cycle's seat-version row
  (plus **P5b** positive control that the same-cycle borrow does write, and
  **P5c** that the donor position count is read from the explicit cycle),
  **P7** the review's `replacement_score` only ever comes from a same-day
  candidate, **P5d** the seat budget (`allocation_version` / `target_limit` /
  donors) is derived from the explicit cycle so a legitimate same-cycle borrow
  is not rejected as "version missing".
- **`backend/test_paper_trading_architecture_guard.py`** — new **Guard 9**
  (`ReplacementIsAsOfAndCycleBound`): 9a pure module zero project imports, 9b
  zero I/O and zero clock, 9c evidence module no reverse dependency and no clock,
  9d the candidate query is not a next-day range, 9e it has the `signal_date <=
  asof` bound, 9f `_slot_upgrade_context.cycle_id` keyword-only + required, 9g it
  never resolves the active cycle, 9h borrow and rollback likewise, 9i the
  historical review read is bounded by `(cycle_id, review_date <= asof)`, 9j the
  legacy `_replacement_score_from_signal` is gone, 9k the candidate path never
  regains the next-day range, 9l the adapter uses the evidence layer and the pure
  module. Baseline ratcheted **down** to 16049 / 282.

## Non-vacuity (mutation check)

`work/r18_mutation_check.py` injects **15 byte-level mutations** into real
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
| M-RPL1 | candidate query back to `today..next_day` range | RPL-P1 |
| M-RPL2 | `signal_date` bound reversed | RP2-04 |
| M-RPL3 | historical review loses `review_date <= asof` | RPL-P4 |
| M-RPL4 | slot context back to `_active_cycle()` | Guard 9g |
| M-RPL5 | apply borrow back to `_active_cycle()` | RPL-P5 |
| M-RPL6 | rollback borrow back to `_active_cycle()` | RPL-P6 |
| M-RPL7 | donor position read back to `current_positions` | RPL-P5c |
| M-RPL8 | candidate score weights changed | RD-1 |
| M-RPL9 | T+1 lock removed | RD-6 |
| M-RPL10 | min-hold gate removed | RD-7 |
| M-RPL11 | urgent threshold `>=` → `>` | threshold boundary |
| M-RPL12 | net edge ignores the execution buffer | threshold boundary |
| M-RPL13 | pure module reverse-imports `paper_trading` | Guard 9a |
| M-RPL14 | adapter swaps `asof` for the next weekday | RPL-P1 |
| M-RPL15 | seat budget reverted to the active cycle | RPL-P5d |

**M-RPL1 and M-RPL14 are the core business mutations**, not signature checks:
restoring the next-day window — either inside the evidence layer or by shifting
the adapter's `asof` — makes the tomorrow-candidate production regression go RED.
**M-RPL3** proves the historical leakage bound: removing `review_date <= asof`
makes the future-review regression go RED.

## Verification (local)

| Check | Result |
|---|---|
| new modules (replacement decision + provenance) | 69 tests OK |
| `unittest backend.test_paper_trading_architecture_guard` | 45 tests OK |
| targeted set (7 modules) | 227 tests OK |
| entry / slot / cycle regression set (11 modules) | 151 tests OK |
| strategy + golden replay set (9 modules) | 116 tests OK |
| `unittest discover -s backend` | **3717 tests OK** (skipped=5) |
| `work/r18_before_fix_repro.py` | base 4/4 REPRODUCED → fixed 0/4 |
| `work/r18_mutation_check.py` | 15/15 RED, restore byte-identical |
| `ruff check backend` | All checks passed |
| `python -m compileall -q backend` | exit 0 |

Golden replay (`test_production_path_golden_replay`, `test_demo_replay_golden`)
shows **no snapshot drift**: normal fixtures do not rely on a future replacement
candidate or a future review.

## Out of scope

- No strategy changes: `SLOT_BORROW_MIN_CANDIDATE_SCORE`,
  `SLOT_BORROW_MIN_EDGE`, `SLOT_UPGRADE_MIN_CANDIDATE_SCORE`,
  `SLOT_UPGRADE_MIN_EDGE`, `POSITION_REVIEW_REPLACEMENT_EDGE`,
  `POSITION_REPLACEMENT_EXECUTION_BUFFER`,
  `POSITION_FULL_CAP_REPLACEMENT_EDGE`, `POSITION_FULL_CAP_MAX_SCORE`,
  `POSITION_REVIEW_ANY_REPLACE_SCORE`, `POSITION_REVIEW_REPLACE_SCORE`,
  `STRATEGY_MIN_POSITIONS`, `STRATEGY_MAX_POSITIONS` — all unchanged.
- No protective risk exits changed (hard stop / trailing / take profit / max hold
  / downside guard / capacity exit / permission exit).
- No migration. `paper_position_limit_versions.cycle_id` already exists;
  `paper_signals` gains no `cycle_id` (today's eligibility is decided by
  `intended_date` + `signal_date` + `status`).
- No `_buy_order` split, no entry-planner rewrite, no selection rework, no
  atomic sell+buy transaction. **This PR does not promise the replacement will
  fill** — only that a candidate which can trigger a same-day sell belongs to
  that same day. Final execution still goes through the single `_buy_order` gate.
- No merge, no deploy. Awaiting human review.