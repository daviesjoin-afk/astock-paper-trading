## ⛔ STATUS: DO NOT MERGE / DO NOT DEPLOY — awaiting human review

`MERGE: NOT MERGED` · `DEPLOY: NOT DEPLOYED`

## Review follow-up (round 3)

A second human review of head `f7fb858` found **two remaining cycle-fencing
blockers**: R18 claimed "the whole slot lifecycle belongs to one explicit cycle",
but two evidence channels still fell back to the current active cycle.

### Blocker 1 — pending BUY slot occupancy crossed the cycle

`_slot_upgrade_context(..., cycle_id=8)` fixed reviews, budget and positions to
cycle 8, but then called:

```python
pending_slots = _pending_position_slots(conn, positions)
```

which reached `paper_slot_occupancy.pending_position_slots()` and ran:

```sql
SELECT account_id,code FROM paper_orders
 WHERE origin IN ('manual','strategy') AND side='buy' AND status IN (...)
```

with **no `cycle_id=?`**. So with cycle 8 requested and cycle 9 active:

```text
positions          → cycle 8
reviews            → cycle 8
allocation version → cycle 8
pending BUY seats  → ALL cycles        ← split-brain
```

Three in-flight BUYs belonging to cycle 9 changed cycle 8's `occupied_pool`, and
therefore the existence of a `shared_pool` donor, `borrow_ready` and the whole
slot-upgrade state. No P5/P5c/P5d test covered pending-order occupancy, which is
why CI was green.

Fixed: `pending_position_slots(conn, positions, exclude_order_key=None, *,
cycle_id=None, ...)` appends `AND cycle_id=?` when a cycle is supplied (`None`
keeps the pre-existing unfiltered semantics for dashboard / manual-order
readers, where legacy `cycle_id IS NULL` rows must stay visible).
`_pending_position_slots(conn, positions=None, exclude_order_key=None, *,
cycle_id=None)` forwards it, and the in-flight chain passes its claimed cycle:

```python
pending_slots = _pending_position_slots(conn, positions, cycle_id=resolved_cycle_id)
```

New production regression **RPL-P5e** (cycle 8 requested / cycle 9 active, only
cycle 9 has executable pending BUYs ⇒ cycle 8's `occupied_pool` must ignore them)
plus mutation **M-RPL16** (drop the cycle filter ⇒ P5e RED) and Guard 9m now also
pins the pending-slot call shape.

### Blocker 2 — explicit-cycle allocation budget still read active-cycle positions

`_dynamic_position_limits(conn, cycle_id=8)` fixed the account rows and the
version row, but internally called `_strategy_cluster_factors(conn,
account_ids=...)` without the cycle, which reached
`_strategy_cluster_profiles(...)` and read:

```python
positions = _position_rows(conn, asof_day=day)   # current-position facade
```

That facade re-resolves the **active** cycle, so:

```text
_dynamic_position_limits(cycle_id=8)
  account rows            → cycle 8
  allocation version      → cycle 8
  cluster position evidence → active cycle 9   ← split-brain
```

`cluster_diversification` then feeds `runtime_inputs` → `fingerprint` →
`allocation_key` → `PA.position_limits`, so an allocation version was created
under cycle 8 whose fingerprint / cluster evidence came from cycle 9 — and in
some combinations the limits themselves changed.

Fixed:

```python
_dynamic_position_limits(conn, *, cycle_id=None, asof_day=None)
  → _strategy_cluster_factors(conn, asof_day, account_ids=..., cycle_id=cycle_id)
  → _strategy_cluster_profiles(conn, asof_day, account_ids, *, cycle_id=None)
  → PPRM.positions_for_cycle(conn, int(cycle_id), asof_day=day)
```

`_slot_upgrade_context` / `_apply_slot_borrow` pass both `cycle_id` and `asof_day`.

Two more future-leakage channels in the same helper were closed while in there:
the cluster signal query had only `intended_date >= day-14` and now also carries
`intended_date <= day`; and `_strategy_return_series` used the wall clock
(`_date()`) with no cycle filter, and now takes `cycle_id` / `asof_day` and bounds
the fill window with `executed_at < asof+1d`.

New production regression **RPL-P5f** (cycle 9 positions and an as-of-after
signal must not appear in cycle 8's cluster profile, while `cycle_id=None` keeps
reading the active cycle) plus mutation **M-RPL17** (cluster positions back to
`_position_rows()` ⇒ P5f RED).

Both blockers share one principle, now written into invariant 19 and Guard 9n:

> Once a helper accepts an explicit `cycle_id`, every position / order / budget
> evidence read inside it that affects the decision must no longer fall back to
> the current active cycle.

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

Slot-upgrade/borrow/rollback use one explicit `cycle_id`, and **every** evidence
read behind that decision — holding reviews, position counts, pending BUY seat
occupancy, and the allocation budget's cluster profiles / return series — is
fenced to the same explicit cycle and as-of. Holding review evidence is read only
from that cycle at `review_date <= asof_day`.

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

Once a helper accepts an explicit cycle_id, every position / order / budget
evidence read inside it that affects the decision must be bounded by that
same cycle (and as-of) — it must not fall back to the current active cycle.
```

The first two are **equality / upper bound**, not a range and not "take the
newest row". A legal overnight plan (`signal_date = D-1`, `intended_date = D`)
therefore stays **usable** — the fix does not ban overnight planning.

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
    actual : requested cycle=1 active_cycle=2 weakest_score=20.0 (cycle8=70, cycle9=20; expected 70) has_cycle_kwarg=False

R18-C5 pending BUY slots cross the cycle boundary: REPRODUCED
    actual : requested cycle=1 active_cycle=2 cycle9_pending_buys=3 cycle8 occupied_pool donor=['sector_rotation'] has_cycle_kwarg=False (cycle 8 无在途买单 ⇒ shared_pool donor 必须存在)

R18-C6 cluster signal evidence has no as-of bound: REPRODUCED
    actual : asof=2026-09-10 signals=['600301', '600302'] leaked_future=['600302'] (600302 属于 2026-09-11，必须排除)

SUMMARY: 6/6 reproduced on this tree
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
R18-C5 ...: NOT REPRODUCED   requested cycle=1 active_cycle=2 ... donor=['sector_rotation', 'shared_pool']
R18-C6 ...: NOT REPRODUCED   asof=2026-09-10 signals=['600301'] leaked_future=[]
SUMMARY: 0/6 reproduced on this tree
```

C5/C6 (added in round 3) reproduce on the unmodified base `f979a166` (6/6 there)
and were also confirmed on the previously reviewed head `f7fb858`; both are
**NOT REPRODUCED** after this round's fix. C1–C4 remain 0/4 as before.

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
allocation_version_id(text, default=0)                      # `slots-vN` token
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
  locate the seat-version row by the **explicit** cycle (via
  `PRep.allocation_version_id`), and the donor position count is read via
  `PPRM.positions_for_cycle(conn, resolved_cycle_id)`.
- **Blocker 1 (round 3)** — `_pending_position_slots(..., *, cycle_id=None)`
  forwards `cycle_id=?` into `paper_slot_occupancy.pending_position_slots()`, and
  the in-flight chain passes its claimed cycle, so another cycle's pending BUYs
  cannot occupy the requested cycle's seats.
- **Blocker 2 (round 3)** — `_dynamic_position_limits(conn, *, cycle_id=None,
  asof_day=None)` forwards both into `_strategy_cluster_factors` /
  `_strategy_cluster_profiles`, which read `PPRM.positions_for_cycle(...)`
  instead of the current-position facade, bound the signal query with
  `intended_date <= day`, and pass the same cycle / as-of to
  `_strategy_return_series` (which itself now bounds `executed_at` and filters
  `cycle_id`).
- **Removed** `_replacement_score_from_signal` with **no** compatibility wrapper;
  production calls `PRep.score_candidate` directly.
- Deliberately **not** changed: `_rotation_buy_candidate` still delegates to the
  single `_buy_order` gate; `_save_position_review`, the rest of
  `_monitor_risk_impl` and the R17 episode provenance are untouched.

Size ratchet per §104:

```text
Before: 16134 / 283
After:  16046 / 282
Delta:  -88 / -1
```

## Authority matrix

```text
Position quantity: paper_position_lots
Episode origin: paper_position_risk_state
Position review: paper_position_review
Replacement candidate evidence: paper_signals active rows, same intended trading day only
Replacement/slot decision: paper_replacement_decision
Slot review evidence: paper_position_reviews, explicit cycle + asof bound
Slot seat occupancy: paper_orders, explicit cycle
Allocation budget evidence: positions + signals + fills of the explicit cycle
Risk scan lifecycle: paper_risk_scan_runs
Compatibility position projection: paper_positions
```

## Tests

- **`backend/test_paper_replacement_decision.py` (NEW, 43 tests)** — RD-1 … RD-16,
  plus explicit threshold boundaries (`candidate == borrow_min_candidate`,
  `edge == borrow_min_edge`, `candidate == upgrade_min_candidate`,
  `edge == upgrade_min_edge`, `net_edge == full_cap_edge`,
  `weakest == full_cap_max_score`, `weakest == any_replace_score`,
  `weakest == replace_score`, `weakest == exit_score`) so a `>=`/`>` or `<`/`<=`
  drift cannot slip through. Also asserts the policy defaults equal the
  production constants, that the adapter and the pure module agree on the score,
  and the module's zero-project-import / zero-I/O / zero-clock boundary.
- **`backend/test_replacement_asof_provenance.py` (NEW, 30 tests)** — evidence
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
  (plus **P5b** positive control that the same-cycle borrow does write,
  **P5c** that the donor position count is read from the explicit cycle, and
  **P5d** that the seat budget (`allocation_version` / `target_limit` / donors)
  is derived from the explicit cycle so a legitimate same-cycle borrow is not
  rejected as "version missing"),
  **P7** the review's `replacement_score` only ever comes from a same-day
  candidate,
  **P5e** cycle 8's `occupied_pool` ignores cycle 9's executable pending BUYs
  (with a non-vacuity sub-assertion that cycle 9 *does* see them), and
  **P5f** cycle 8's cluster profile contains neither cycle 9's positions nor an
  as-of-after signal, while `cycle_id=None` still reads the active cycle.
- **`backend/test_paper_slot_occupancy.py`** — facade signature now pins the
  keyword-only `cycle_id`, plus a test that an explicit cycle is forwarded into
  `paper_slot_occupancy.pending_position_slots` while `None` stays `None`.
- **`backend/test_paper_trading_architecture_guard.py`** — new **Guard 9**
  (`ReplacementIsAsOfAndCycleBound`): 9a pure module zero project imports, 9b
  zero I/O and zero clock, 9c evidence module no reverse dependency and no clock,
  9d the candidate query is not a next-day range, 9e it has the `signal_date <=
  asof` bound, 9f `_slot_upgrade_context.cycle_id` keyword-only + required, 9g it
  never resolves the active cycle, 9h borrow and rollback likewise, 9i the
  historical review read is bounded by `(cycle_id, review_date <= asof)`, 9j the
  legacy `_replacement_score_from_signal` is gone, 9k the candidate path never
  regains the next-day range, 9l the adapter uses the evidence layer and the pure
  module, 9m the slot chain passes the explicit cycle into the budget lookup and
  into the pending-slot read, 9n the cluster evidence follows the claimed cycle /
  as-of. Baseline ratcheted **down** to 16049 / 282 and the file now sits at
  16046.

## Non-vacuity (mutation check)

`work/r18_mutation_check.py` injects **17 byte-level mutations** into real
production sources and requires the *specific* contract test to fail with
`FAIL:`/`ERROR:` on that exact method, so an import/collection error cannot
masquerade as a catch. Every file is restored byte-identically and verified by
sha256 in a `finally` block, with a fresh `PYTHONPYCACHEPREFIX` per run so a
same-length mutation in the same second cannot reuse stale bytecode.

```text
RESULT: 17/17 mutations RED, all files restored byte-identical
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
| M-RPL16 | pending-slot read loses the `cycle_id` filter | RPL-P5e |
| M-RPL17 | cluster positions back to `_position_rows()` | RPL-P5f |

**M-RPL1 and M-RPL14 are the core business mutations**, not signature checks:
restoring the next-day window — either inside the evidence layer or by shifting
the adapter's `asof` — makes the tomorrow-candidate production regression go RED.
**M-RPL3** proves the historical leakage bound: removing `review_date <= asof`
makes the future-review regression go RED. **M-RPL16 / M-RPL17** prove the two
round-3 blockers are actually fenced rather than merely untested.

## Verification (local)

| Check | Result |
|---|---|
| `test_paper_replacement_decision` + `test_replacement_asof_provenance` (new modules) | 73 tests OK |
| `unittest backend.test_paper_trading_architecture_guard` | 46 tests OK |
| targeted set (7 modules incl. slot occupancy / position review) | 240 tests OK |
| entry / slot / cycle regression set (11 modules) | 244 tests OK |
| strategy + golden replay set (6 modules) | 99 tests OK |
| `unittest discover -s backend` | **3723 tests OK** (skipped=5) |
| `work/r18_before_fix_repro.py` | base 6/6 REPRODUCED → fixed 0/6 |
| `work/r18_mutation_check.py` | 17/17 RED, restore byte-identical |
| `ruff check backend` | All checks passed |
| `python -m compileall -q backend` | exit 0 |
| `scripts/security/scan-sensitive-data.py --scope worktree` | kinds: none / values: 0 |

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
