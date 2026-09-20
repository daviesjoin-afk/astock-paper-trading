# R19 — fix(entry): bind buy planning and commit to order provenance

- Branch: `codex/entry-capital-commit-convergence`
- Base SHA: `d754fd4e8ac966a8f0e50f079906aa029c1cb4c0` (master containing #174), rebased onto `cbb1863`
- Review-fix commit: `275ee60`（精确 head 与 CI 结论以 PR 的 exact-head 检查为准）
- Final review-fix commit: cycle-pinned strategy version provenance（精确 head 见 PR）

## Before

The strategy BUY path used an as-of-bound seat budget but rebuilt its
capital budget from current-cycle/current-date adaptive and cluster
evidence. A historical entry could therefore receive a different
allocation, quantity, or deployment result because evidence that did
not yet exist at the requested as-of date was visible.

The same BUY path also maintained a second fill-commit implementation
instead of using execution_planner.commit_fill. Its reservation call
did not provide the order's cycle provenance, allowing an existing
wrong-cycle reservation with the same order key to be resized and
consumed.

## After

Strategy capital planning carries one explicit `cycle_id` + `asof_day`
through participant rows, adaptive risk/allocation activation, cluster
evidence, strategy budget, and deployment planning.

Outstanding reserved cash remains a global economic obligation by
design; this PR does not fabricate historical reservation state.

Normal strategy BUYs create their order in `paper_trading` but commit the
fill through `execution_planner.commit_fill`, sharing the same order
provenance, reservation, cash, lot, fill, verification, risk-log, and
audit primitive as the other execution paths.

## Before-fix evidence (`work/r19_before_fix_repro.py` on the clean R19 base)

```text
R19-C1 future adaptive allocation changes historical budget:
REPRODUCED
    overlay effective=2026-09-11 (visible at D=False, at D+1=True) weight=0.9
    budget(asof=D, no overlay) target_pct=16.68 cap=166809.42
    budget(asof=D, overlay effective D+1) target_pct=15.97 cap=159740.26

R19-C2 future cluster evidence changes historical BUY sizing:
REPRODUCED
    future signals intended=2026-09-15 (D-visible keys=[], unbounded keys=4)
    D clusters=[{'sector_rotation'}, {'tq_breakout'}]
    unbounded clusters=[{'sector_rotation', 'tq_breakout'}]
    budget(asof=D) before target_pct=16.68 cap=167810.28 allowance=164810.28
    budget(asof=D) after  target_pct=13.36 cap=134424.71 allowance=131424.71

R19-C3 wrong-cycle reservation is consumed by normal strategy buy:
REPRODUCED
    prepared reservation order_key=1 cycle=2 status=reserved
    order id=1 (== prep key: True) cycle=3 status=filled
    reservation after: cycle=2 status=consumed | lot cycles=[3] fills=1

R19-C4 normal strategy buy bypasses execution_planner.commit_fill:
REPRODUCED
    baseline(no sentinel): filled=True fills=1 lots=1
    with sentinel: EP.commit_fill calls=0 filled=True fills=1 lots=1
    reservation=consumed | reachable=True ep_called=False
```

After the fix the same script reports `0/4 reproduced`.

## Capital planning provenance

```text
participant cycle:                          PASS
risk profile as-of (rows + fallback):       PASS
adaptive risk as-of:                        PASS
adaptive allocation as-of:                  PASS
cluster cycle:                              PASS
cluster as-of:                              PASS
strategy_pool_budget explicit cycle/asof:   PASS
allocation_plan explicit cycle/asof:        PASS
intraday buyback explicit cycle/asof:       PASS
swing scale-in explicit cycle/asof:         PASS
```

## Strategy version provenance (explicit cycle)

The capital budget already carries an explicit `cycle_id`. The compiled
strategy risk profile must consume **the same immutable version that cycle
pinned at start-up**, not the current/latest head.

```text
authority:                    paper_cycle_strategy_versions
resolver:                     SRE.compiled_profile_for_cycle(conn, account_id, cycle_id=)
  → SR.cycle_version_for_account(conn, account_id, cycle_id=)
  → SR.get_version(strategy_id, version, checksum=checksum)
strict pin lookup:            only paper_cycle_strategy_versions
legacy / current-head fallback: forbidden on the exact-cycle branch
cluster DSL:                  exact pinned version → normalized AST; missing pin = unknown
allocation runtime fields:    pinned max_positions / own_exposure_cap_pct;
                              current lifecycle permission may stay current
missing / invalid binding:    risk/runtime fail closed; cluster DSL = None
as-of earlier than head:      pinned version still applied (no silent un-tightening)
```

"current head + `created_at <= asof`" is a *different* contract and does not
replace this one: cycle 8 may pin v1 and only later create v2, so a
timestamp-only check lets cycle 8's historical budget adopt v2's caps. The
reverse direction must hold too — when the head is newer than the as-of date
the pinned profile must **not** be dropped entirely, because the compiled
profile only ever tightens; dropping it silently loosens historical risk
limits beyond the genuinely pinned version.

## Global reservation semantics

```text
reserved cash remains global economic obligation:  PASS
pending_buy_reservations cycle_id still ignored:   PASS
fake historical reservation reconstruction:        NO
```

`_pool_allocation_inputs` bounds participants / adaptive overlays / cluster
evidence, but deliberately does **not** cycle-filter `pending_buy_reservations`
— an older cycle's still-reserved cash keeps consuming the same economic pool.
Filtering it would create a real double-spend.

## Strategy BUY commit

```text
normal _buy_order uses EP.commit_fill:      PASS
direct cash debit in _buy_order:            NO
direct lot write in _buy_order:             NO
direct fill insert in _buy_order:           NO
direct EV.stamp_order in _buy_order:        NO
direct _reserve_shared_capital in _buy_order: NO
```

Order creation stays in `paper_trading._buy_order` (§42); the commit
orchestration (SAVEPOINT → `commit_fill` → slice bookkeeping → failure
handling) lives in `manual_orders.commit_strategy_entry_fill`. No second
reserve/cash/lot/fill ledger is created.

## Reservation provenance

```text
new reservation uses expected order cycle:  PASS
existing wrong-cycle reservation:           REJECTED
wrong reservation rewritten:                NO
wrong reservation released:                 NO
cash debit on mismatch:                     NO
lot on mismatch:                            NO
fill on mismatch:                           NO
current order on mismatch:                  terminal risk_rejected
candidate intent on mismatch:               deferred_capacity (new order identity only)
```

`ReservationCycleMismatch` is a durable provenance conflict, not a temporary
funding shortage. The conflicting reservation belongs to another order, so it
is never released; the *current* order is terminalized because the same
`order.id` will always conflict with that reservation's cycle.

## Normal BUY parity

```text
order cycle == reservation cycle:  PASS
order cycle == lot cycle:          PASS
reservation consumed:              PASS
fill:                              PASS
execution_verified:                PASS
risk event count:                  1
audit event count:                 1
```

## Slice entry

```text
intermediate slice remains deferred:  PASS
final slice marks filled:             PASS
ET.mark_entered only after commit:    PASS
```

## Rollback

```text
cash rollback:          PASS
lot rollback:           PASS
fill rollback:          PASS
slot borrow rollback:   PASS
```

## #170–#174 invariants

```text
PASS (test_replacement_asof_provenance, test_paper_replacement_decision,
      test_position_review_provenance, test_paper_risk_scan_state)
```

## Mutation

```text
M-ENT1..M-ENT23:      23/23 CAUGHT
survived:             0
non-vacuity:          PASS
restore bytes:        PASS
restore sha256:       PASS
```

```text
M-ENT1   RED  _strategy_pool_weights drops asof_day
M-ENT2   RED  _risk_profile rows path drops asof_day
M-ENT3   RED  participants fall back to active cycle
M-ENT4   RED  cluster factors drop as-of
M-ENT5   RED  cluster factors drop cycle_id
M-ENT6   RED  _buy_order strategy_budget drops cycle/as-of
M-ENT7   RED  _buy_order allocation_plan drops cycle/as-of
M-ENT8   RED  _intraday_buyback budget drops cycle/as-of
M-ENT9   RED  _swing_scale_in budget drops cycle/as-of
M-ENT10  RED  normal _buy_order bypasses EP.commit_fill
M-ENT11  RED  execution_planner reservation drops expected_cycle_id
M-ENT12  RED  new reservation INSERT falls back to active_cycle_fn
M-ENT13  RED  mismatch branch releases the conflicting reservation
M-ENT14  RED  normal _buy_order re-adds direct INSERT INTO paper_fills
M-ENT15  RED  normal _buy_order re-adds direct _record_lot
M-ENT16  RED  first slice marks the signal filled
M-ENT17  RED  explicit idle cycle re-injects the caller account
M-ENT18  RED  compiled profile merged unconditionally (future version rewrites history)
M-ENT19  RED  terminalizer unconditionally releases a foreign reservation
M-ENT20  RED  cycle-pinned version lookup falls back to current/latest head
M-ENT21  RED  strict cycle resolver falls back to stamp_for_account
M-ENT22  RED  cluster DSL falls back to current runtime context
M-ENT23  RED  runtime version fields fall back to current strategy context
```

## paper_trading.py

```text
before:  16049 / 282
after:   16032 / 282
delta:   -17 LOC / 0 defs
```

The decrease comes from deleting the duplicate reserve/cash/lot/fill/verification
wiring; the LOC ratchet (Guard 3) passes.

## Verification

```text
architecture baseline:  PASS (Guard 1..Guard 10s)
Targeted:               PASS
  test_entry_capital_asof (EC-1..EC-19)
  test_strategy_buy_commit_convergence (SB-1..SB-16)
  test_paper_capital_reservations
  test_paper_trading_architecture_guard
  test_deferred_fill_cycle_binding
  test_replacement_asof_provenance
  test_paper_replacement_decision
  test_position_review_provenance
  test_paper_risk_scan_state
Backend full:           exact-head CI
ruff:                   PASS
compileall:             PASS
Frontend:               PASS (build; no dist drift — this PR does not touch frontend)
Chromium:               exact-head CI
Docker:                 exact-head CI
Security:               kinds: none / values: 0
Review threads:         unresolved=0
Mergeability:           MERGEABLE / CLEAN
Exact-head CI:          ALL PASS
Merge:                  NOT MERGED
Deploy:                 NOT DEPLOYED
```

## Review findings addressed

| # | Finding | Fix | Regression |
|---|---------|-----|------------|
| 1 | Foreign (wrong-cycle) reservation was released by the terminalizer | `_terminalize_cycle_stale_order` skips release when the mismatch is a foreign reservation | EC-14, Guard 10p, M-ENT19 |
| 2 | Explicit idle cycle silently injected the caller account | both fallback branches are gated on `cycle_id is None` | EC-13, Guard 10n, M-ENT17 |
| 3 | Compiled risk profile was merged for as-of dates older than the strategy version | `compiled_profile_is_asof_provable` gates the merge | EC-12, Guard 10o, M-ENT18 |
| 4 | Historical compiled risk profile was resolved from the current head instead of the version the cycle pinned | `SRE.compiled_profile_for_cycle` reads `paper_cycle_strategy_versions` via strict `SR.cycle_version_for_account` → `SR.get_version(checksum=…)`; exact-cycle branch never consults current/latest head; missing binding fails closed to Composite; the pinned profile still applies when the head is newer than the as-of date | EC-15, EC-16, Guard 10q, M-ENT20 |
| 5 | Strict cycle authority still reused a resolver with legacy/current-head fallback | `SR.cycle_stamp_for_account` / `cycle_version_for_account` only read `paper_cycle_strategy_versions`; no legacy binding or current head fallback | EC-17, Guard 10q, M-ENT21 |
| 6 | Cluster DSL structural evidence still read the current strategy head | cluster DSL is compiled from the exact cycle-pinned version; missing pin means unknown (`None`), never current DSL | EC-18, Guard 10r, M-ENT22 |
| 7 | Allocation runtime cap could still read the current strategy head | runtime `max_positions` / `own_exposure_cap_pct` come from cycle-pinned profile/version fields; only current lifecycle permission stays current | EC-19, Guard 10s, M-ENT23 |

## Authority matrix

```text
Entry intent / gates:        paper_trading orchestration
Entry allocation math:       paper_allocation
Cycle/as-of capital evidence: _pool_allocation_inputs bounded inputs
Order provenance:            paper_orders.cycle_id
Capital reservation:         paper_capital_reservations
Fill commit:                 execution_planner.commit_fill
Position quantity:           paper_position_lots
Position episode:            paper_position_risk_state
Execution verification:      execution_verification
```

## Deliberately out of scope

- `paper_capital_reservations` is not event-sourced (only the final status is
  kept), so this PR does not claim the ability to reconstruct every historical
  reservation state. It only closes the future-evidence leakage that the
  existing cycle/as-of fields can bound.
- No migration: the existing schema already carries `reservation.cycle_id`,
  `order.cycle_id`, `created_at` and `status`.
- No strategy threshold changes; no new allocation formula; the existing
  `paper_allocation` engine and `execution_planner.commit_fill` primitive are
  reused rather than copied.
