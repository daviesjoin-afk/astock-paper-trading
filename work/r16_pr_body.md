## ⛔ STATUS: DO NOT MERGE / DO NOT DEPLOY — awaiting human review

`MERGE: NOT MERGED` · `DEPLOY: NOT DEPLOYED`

## Review follow-up (round 2)

An automated review of head `042fe1d` raised one actionable point, now fixed:

> **Keep scan completion inside the failure handler** — if the completion
> transaction raises a non-lease exception (`complete_scan`, the completion
> audit, or the commit), the exception occurred *after* the surrounding `try`, so
> `fail_scan` was never called and the durable row stayed `running` forever.

Correct: the completion block sat **outside** the `try`, so a failure while
finalising left exactly the orphan `running` identity this PR exists to
eliminate. Completion now lives **inside** the guarded region, so any failure
there — including `complete_scan`'s own CAS or the commit — falls through to
`fail_scan` on the **same** identity:

```python
try:
    result = _monitor_risk_impl(asof_date, cycle_id=ident["cycle_id"])
    with _db(immediate=True, hot_path=True) as conn:
        _assert_active_lease(conn, "risk scan completion")
        PRSS.complete_scan(conn, **ident, finished_at=_now())
        _audit(conn, None, "risk_scan_completed", _json(dict(ident)))
    return result
except Exception as exc:
    ...
    PRSS.fail_scan(conn, **ident, finished_at=_now(), error=...)
    raise
```

New regression **RISK-SCAN-P7** pins it: inject a failure into `complete_scan`
and assert the row ends `failed` (not `running`) with exactly one identity. New
mutation **M-RS15** reverts completion to outside the `try` and P7 goes RED.

## Summary

A risk scan's identity was **the machine minute and nothing else**. The dedupe
gate was a `paper_audit` JSON marker:

```text
SELECT event,detail FROM paper_audit
 WHERE event='risk_scan_state' AND detail LIKE '%"scan_minute": "14:50"%'
```

That marker carries **no cycle identity and no asof date**, so:

```text
14:50:05  cycle 8 risk scan completes
14:50:30  cycle 8 -> cycle 9 rollover
14:50:4x  monitor_risk(cycle 9)  ->  reads cycle 8's completed marker
                                  ->  returns "already_scanned"
                                  ->  cycle 9 positions never got a risk pass
```

Two more defects ride on the same root cause: the active cycle was only resolved
**after** the quote/news/flow I/O (so a scan that began under cycle 8 could finish
by writing cycle 9 lots/orders/reviews), and `monitor_risk` / `_monitor_risk_impl`
each computed `scan_minute` independently (a minute boundary produced a `running`
marker with **no matching `failed` transition** — an orphan).

This PR makes the scan identity **durable and cycle-owned**, and extracts the
scan lifecycle out of the 16k-line `paper_trading.py`.

## Two goals

1. **Correctness** — a risk pass belongs to exactly one
   `(cycle_id, asof_date, scan_minute)`. The identity is resolved **once**, the
   position snapshot is pinned to the claimed cycle, and a cycle change during
   external I/O **aborts before any execution-side write**.
2. **Architecture** — the scan lifecycle (claim / running / completed / failed /
   retry / cycle fence) moves to `backend/paper_risk_scan_state.py`, with a
   one-way dependency `paper_trading → paper_risk_scan_state`.

## Before-fix reproduction (unmodified base `c81022e`)

`work/r16_before_fix_repro.py`, run in a clean `git worktree` of the base commit:

| Case | Scenario | Base result |
|---|---|---|
| R16-C1 | cross-cycle same-minute suppression | **REPRODUCED** |
| R16-C2 | different-asof same-minute collision | **REPRODUCED** |
| R16-C3 | minute-boundary orphan running marker | **REPRODUCED** |
| R16-C4 | cycle rollover during quote fetch | **REPRODUCED** |

Raw base output:

```text
R16-C1: first=None second=already_scanned; audit markers=2
R16-C2: second=already_scanned; audit markers=2
R16-C3: audit markers=[('2026-09-10 14:51','running'), ('2026-09-10 14:50','failed')] orphan_running=True
R16-C4: raised=None; c9_orders=1 c9_reviews=1 fills=1 c8_remaining=500 c9_remaining=0
SUMMARY: 4/4 reproduced on this tree
```

**C4 was not merely "the write phase resolved cycle 9"** — it produced a real
cross-cycle sale: an order, a fill and a review on cycle 9, plus cycle 9's lot
consumed to 0, while cycle 8 was untouched. That is the execution-level error in
full.

After the fix, the same script reports:

```text
R16-C1: scan_runs cycles=[1, 2] (c8=1, c9=2)
R16-C2: scan_runs asofs=['2026-09-10', '2026-09-11']
R16-C3: scan_runs=[('2026-09-10 14:51', 'failed')]
R16-C4: fail closed: RiskScanCycleChanged: risk scan 已认领 cycle 1，但当前 active cycle 是 2;
        c9 orders=0 reviews=0 fills=0
SUMMARY: 0/4 reproduced on this tree
```

## New module: `backend/paper_risk_scan_state.py`

Hard boundary (enforced by AST guard, not by convention):

- zero DB/network/filesystem/process — it only uses the **caller's** connection to
  read and write its own run table;
- **zero wall clock** — no `date.today()` / `datetime.now()` / `time.time()`;
  every timestamp (`started_at` / `finished_at`) is passed in explicitly, so the
  identity cannot drift on its own;
- **zero transaction ownership** — no `commit` / `rollback` / `BEGIN` /
  `SAVEPOINT`; the transaction belongs to `paper_trading.monitor_risk`;
- no import of `paper_trading` (one-way dependency).

```python
claim_scan(conn, *, cycle_id, asof_date, scan_minute, started_at)
complete_scan(conn, *, cycle_id, asof_date, scan_minute, finished_at, detail=None)
fail_scan(conn, *, cycle_id, asof_date, scan_minute, finished_at, error)
scan_run(conn, *, cycle_id, asof_date, scan_minute)
assert_cycle_active(conn, *, cycle_id)
```

### Scan identity matrix

| cycle | asof | runtime minute | same scan? |
|---|---|---|---|
| same | same | same | **YES** |
| different | same | same | NO |
| same | different | same | NO |
| same | same | different | NO |

`asof_date` is part of the identity because a manual replay can run
`monitor_risk(2026-09-10)` and `monitor_risk(2026-09-11)` inside the same runtime
minute — those are two different business scans. `cycle_id` is part of the
identity because the same asof at the same minute in two different cycles is two
different risk fact domains.

## Authority matrix

| Concern | Authority |
|---|---|
| Risk decision | `paper_risk_decision` |
| Position runtime risk state | `paper_position_risk_state` |
| **Risk scan execution lifecycle** | **`paper_risk_scan_runs` via `paper_risk_scan_state`** |
| Position quantity | `paper_position_lots` |
| Compatibility position projection | `paper_positions` |
| Generic observability | `paper_audit` |

**`paper_audit` is no longer the authority for risk-scan idempotency or execution
ownership.** It keeps `risk_scan_claimed` / `risk_scan_completed` /
`risk_scan_failed` as human-readable diagnostics only; it may not be read to
decide whether to run.

## Cycle rollover contract

> A risk pass may never switch cycle after it has claimed its identity. If the
> active cycle changes while external evidence is being fetched, the pass fails
> closed before any execution-side write.

Order is now: `resolve cycle → build identity (run_at taken once) → claim` in an
immediate transaction, **then** network/quotes/news/flow, then a new transaction
whose **first** statement is `assert_cycle_active(conn, cycle_id=claimed)`. On
change: `RiskScanCycleChanged`, the claimed identity is advanced to `failed` with
`failure_code=cycle_changed`, and the exception propagates to the scheduler. There
is no auto-retry on the new cycle — the quote/news/position snapshot belongs to
the old cycle, and re-binding it would be provenance fabrication.

## Migration v21

`paper_risk_scan_runs` — new table in the **paper trading DB only**:

```sql
UNIQUE(cycle_id, asof_date, scan_minute)
CHECK(status IN ('running','completed','failed'))
cycle_id INTEGER NOT NULL
```

- **Never backfilled.** Legacy `paper_audit` markers have no durable cycle
  ownership; `paper_accounts.cycle_id` is a mutable rebinding, `MAX(paper_cycles.id)`
  is not "active at the time", and dates are not a function of cycles. Guessing is
  how "unknown" gets laundered into "known". The table starts empty and only
  `claim_scan` creates rows.
- Guards: insert requires a real `paper_cycles.id`
  (`invalid risk scan cycle provenance`); `cycle_id` / `asof_date` / `scan_minute`
  are immutable after insert.
- DDL lives **only** in `paper_schema_migrations` — no runtime
  `CREATE TABLE IF NOT EXISTS` in `paper_trading.py` or the state module.
- Cycle archive/purge (`paper_cycle_service`) now covers the new table, so an
  archived cycle's runs cannot linger as orphans.

## `paper_positions` explicit-cycle read

`paper_position_read_model.positions_for_cycle(conn, cycle_id, ...)` reads an
**exact** cycle and never falls back to "who is active now" — not even when the
requested cycle is paused/archived. `current_positions()` now delegates to it and
keeps its old semantics verbatim (current active cycle only; `[]` when there is
none — **not** "latest cycle").

## `paper_trading.py` changes (net reduce)

- `monitor_risk` owns the whole lifecycle: one `run_at`, one identity, claim
  before any I/O, fence before execution, complete/fail on the same identity.
- `_monitor_risk_impl(asof_date=None, *, cycle_id)` — `cycle_id` is keyword-only
  and required; the scan snapshot uses `PPRM.positions_for_cycle(..., cycle_id)`;
  the write transaction opens with `PRSS.assert_cycle_active(...)`.
- **Removed** from the impl: `scan_minute` computation, the `paper_audit`
  `risk_scan_state` SELECT, and the `running` / `completed` marker writes.
- Deliberately **not** moved in this PR: `_position_quality_score`,
  `_concentration_action`, `_over_capacity_exit_candidates`,
  `_permission_scope_exit_candidates`, `_save_position_review`,
  `_intraday_downside_guard` (still uses R15's explicit `asof_day`).
- Size ratchet: **16181 → 16179 LOC**, **284 → 284 top-level defs**.

## Tests

- **`backend/test_paper_risk_scan_state.py` (NEW, 35 tests)** — RS-01 … RS-15:
  fresh claim, running/completed suppression, `failed` retry, `attempt`
  increment + failure clearing, cross-cycle and cross-asof independence, exact
  identity CAS for complete/fail (including "fail must never insert a new row"),
  identity immutability, invalid cycle rejection, **no audit backfill**,
  module owns no transaction, module reads no wall clock, no reverse import,
  dual-DB isolation (paper has the table, adaptive does not), fence semantics
  matching `PPRM.active_cycle_id`, DDL/status contract shared with the migration.
- **`backend/test_paper_risk_exit_production_path.py`** — new
  `TestRiskScanLifecycleProductionPath`:
  - **RISK-SCAN-P1** same identity dedupes with no duplicate orders/fills
  - **RISK-SCAN-P2** cross-cycle same minute is **not** suppressed (both cycles
    get a `completed` run)
  - **RISK-SCAN-P3** different asof same minute is claimable
  - **RISK-SCAN-P4** cycle rollover during quote fetch → `RiskScanCycleChanged`,
    **0** orders / **0** fills / **0** lot mutations / **0** reviews on the new
    cycle, old identity `failed`
  - **RISK-SCAN-P5** failure → retry: `failed(attempt=1)` → `completed(attempt=2)`
  - **RISK-SCAN-P6** minute boundary leaves exactly one row, `failed`, no orphan
  - **RISK-SCAN-P7** a failure while *finalising* (injected into `complete_scan`)
    still lands on `failed` on the same identity — no orphan `running`
  - The old `_clear_audit_scan_marker` helper became `_clear_scan_run_state`:
    clearing the audit marker alone **no longer** opens the gate, which is the
    observable proof of the authority downgrade.
- **`backend/test_authoritative_position_consumers.py`** — `positions_for_cycle`
  returns only the requested cycle, never falls back to current, returns `[]` for
  `None`/missing, and `current_positions` shares the same aggregation.
- **`backend/test_paper_cycle_service.py`** — archiving purges
  `paper_risk_scan_runs`; snapshot counts include it.
- **`backend/test_paper_trading_architecture_guard.py`** — new **Guard 7**
  (7a zero project imports, 7b no I/O, 7c no wall clock, 7d no transaction,
  7e no scan-table CRUD in `paper_trading`, 7f `paper_audit` is not risk-scan
  control state, 7g `_monitor_risk_impl.cycle_id` keyword-only + required,
  7h snapshot pinned via `positions_for_cycle` + fence ordered before execution
  writes). Baseline ratcheted **down** to 16179 / 284.

## Non-vacuity (mutation check)

`work/r16_mutation_check.py` injects **15 byte-level mutations** into real
production sources and requires the *specific* contract test to fail with
`FAIL:`/`ERROR:` on that exact method (so an import/collection error cannot
masquerade as a catch). Every file is restored byte-identically and verified by
sha256 in a `finally` block.

```text
RESULT: 15/15 mutations RED, all files restored byte-identical
```

| ID | Mutation | Caught by |
|---|---|---|
| M-RS1 | scan identity drops `cycle_id` | RS-06 |
| M-RS2 | scan identity drops `asof_date` | RS-07 |
| M-RS3 | `failed` identity not retryable | RS-04 |
| M-RS4 | retry does not increment `attempt` | RS-05 |
| M-RS5 | complete does not check `status='running'` | RS-08b |
| M-RS6 | identity mutable via UPDATE | RS-10 |
| M-RS7 | invalid cycle insert allowed | RS-11 |
| M-RS8 | snapshot reverted to `current_positions()` | Guard 7h |
| M-RS9 | cycle fence removed after external I/O | RISK-SCAN-P4 |
| M-RS10 | cycle change silently adopted | RISK-SCAN-P4 |
| M-RS11 | catch block recomputes `scan_minute` | RISK-SCAN-P6 |
| M-RS12 | `paper_audit` back as scan control authority | Guard 7f |
| M-RS13 | `paper_risk_scan_state` imports `paper_trading` | Guard 7a |
| M-RS14 | migration backfills from legacy `paper_audit` | RS-12 |
| M-RS15 | completion moved outside the guarded region | RISK-SCAN-P7 |

**M-RS9 / M-RS11 are real business mutations, not signature checks.** M-RS9
deletes the fence and RISK-SCAN-P4 goes RED because the old snapshot reaches the
new cycle. M-RS11 replaces the failure identity with a freshly-read clock, and
RISK-SCAN-P6 goes RED because the claimed `running` row is never transitioned —
a genuine orphan, not a `TypeError`.

### Harness correctness: cold bytecode cache

The runner passes a fresh `PYTHONPYCACHEPREFIX` per invocation. CPython validates
a `.pyc` using the source's mtime (**second** granularity) plus its byte length,
so two same-length mutations within the same second can make the second one reuse
the first's stale bytecode — the injection never executes and the test stays
green.

## Verification (local)

| Check | Result |
|---|---|
| `unittest backend.test_paper_risk_scan_state` | 35 tests OK |
| `unittest backend.test_paper_risk_exit_production_path` | 22 tests OK |
| `unittest backend.test_paper_trading_architecture_guard` | 23 tests OK |
| targeted set (scan state + production + guard + risk state + risk decision) | 136 tests OK |
| cycle/runtime regression set (12 modules) | 188 tests OK |
| `unittest discover -s backend` | **3569 tests OK** (skipped=5) |
| `work/r16_before_fix_repro.py` | base 4/4 REPRODUCED → fixed 0/4 |
| `work/r16_mutation_check.py` | 15/15 RED, restore byte-identical |
| `ruff check backend` | All checks passed |
| `python -m compileall -q backend` | exit 0 |
| `node --check` over `frontend/src/**/*.js` | 17 files, exit 0 |
| `npm run build` + `git diff --exit-code -- frontend/dist` | exit 0 (no drift) |
| `npm run test:unit` | 111 pass / 0 fail |
| `scripts/security/scan-sensitive-data.py --scope worktree` | `kinds: none / values: 0` |

## Documentation

`ARCHITECTURE.md`: new module row (风险扫描运行生命周期), new invariant **#17**
freezing the boundary, and the `paper_audit` authority downgrade.

## Out of scope

- No strategy/threshold changes; no execution-gate changes.
- No migration of position review / the rest of `_monitor_risk_impl`.
- `paper_jobs(slot='risk')` is **not** reused as the internal scan authority —
  `monitor_risk` is also nested inside `execute_open` / `monitor_intraday`, so the
  nested risk pass needs its own cycle-owned identity rather than the outer slot's.
- No merge, no deploy. Awaiting human review.
