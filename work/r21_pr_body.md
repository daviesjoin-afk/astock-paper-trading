# refactor(risk): extract cycle-owned risk application service

## Base

- Repository: `daviesjoin-afk/astock-paper-trading`
- PR #177: `MERGED`
- Base commit: `aba3ec5a366a44aa61c1df0f204ae228b0beb324`
- Branch: `codex/risk-application-service`

## Before-fix

The R21 probe was run on the R21 base before any production-code edit:

```text
R21-C1 risk scan capacity budget leaks current facts: REPRODUCED
    scan_call_without_context=True bounded_weights={'tq_breakout': 0.65} current_weights={'tq_breakout': 0.35}
R21-C2 downside policy leaks future/current risk profile: REPRODUCED
    scan_override_warning=-4.0 bounded_warning=-2.0
R21-C3 sell policy leaks current strategy head: REPRODUCED
    structural_current_head_resolver=True head_vs_pin_object_distinct=True head_hard_stop=-0.04 pin_hard_stop=-0.04
    C3 proves the structural current-head resolver path; the behavioral user-policy difference is covered by C5 / RSVC-15.
R21-C4 risk application orchestration remains in paper_trading: REPRODUCED
    _monitor_risk_impl LOC=651 responsibilities={snapshot, external evidence, quality review, capacity review, sell decision, sell execution orchestration, rotation, projection/nav, pending manual retry}
R21 before-fix reproduced: 4/4

R21-C5 user SELL base policy leaks current strategy head: REPRODUCED
    pinned=v1/hard_stop=-0.04/hold_max=10 current=v2/hard_stop=-0.04/hold_max=5
    v1 does not hit max_hold; v2 does.
R21-C6 risk facts stamp current strategy version instead of cycle pin: REPRODUCED
    pinned_stamp=('r19_alpha', 1, ...) current_head=v2; missing cycle/legacy binding adopts v2/current
R21 follow-up before-fix reproduced: 2/2

R21-C7 filled SELL downstream provenance re-resolves current head: REPRODUCED
    order=(None, None, None)
    filled decision=('r21_alpha', 2, ...)
    sell_filled audit=('r21_alpha', 2, ...)
R21-C8 all-NULL strategy provenance is accepted outside protective SELL: REPRODUCED
    paper_signals_insert_ok=True paper_orders_buy_insert_ok=True
R21 final before-fix reproduced: 2/2
```

After the fix, the original probe reports `0/4`, the follow-up probe reports `0/2`, and the final probe reports `0/2`; the real production contracts are covered by `backend/test_risk_application_service.py` (RSVC-1..17).

## Correctness

- dynamic limit: the risk run passes the explicit claimed `cycle_id` and `asof_day` to `dynamic_position_limits`.
- downside profile: the risk run passes `asof_day`, `conn`, and `cycle_id` to `risk_profile` before an intraday guard can see it.
- SELL spec: the risk run uses `strategy_risk_enforcement.effective_spec_for_cycle` and never falls back to the current strategy head.
- cycle fence: external quote/news/fund-flow I/O ends before the write transaction; the first write-phase action is `paper_risk_scan_state.assert_cycle_active(cycle_id)`; rollover aborts without orders/reviews/risk writes.
- explicit context: `RiskRunContext(cycle_id, asof_day)` is immutable and rejects `None`; `run()` requires that context and does not resolve `active_cycle` or wall-clock dates for decision identity.
- USER base spec: `strategy_runtime.get_context_for_cycle(...)` resolves only `paper_cycle_strategy_versions`; missing pin raises instead of falling back to current head. `paper_risk_service._spec_for(...)` requires `cycle_id` and is used on both stale and fresh SELL paths.
- risk provenance: `paper_risk_service._strategy_stamp(...)` requires `cycle_id`, uses `SR.cycle_stamp_for_account(...)` only, and returns `(None, None, None)` when the pin is absent. Unfilled/pending SELL orders and risk decision logs carry the cycle-pinned v1 stamp; missing provenance stays NULL.
- audit guard: the strategy-stamp insert guard is table-aware. Signals, BUY orders, BUY decisions, unrelated audits, partial stamps, and forged stamps remain rejected; only SELL pending orders with an explicit cycle, SELL risk decisions, and causal SELL audit events may carry an all-NULL explicit unknown state.
- fill provenance: `execution_planner.commit_fill` reads the durable order row’s `(strategy_id, strategy_version, strategy_checksum)` before mutation and passes it unchanged to both `PT._risk_log` and `PT._audit`. Missing pin therefore stays NULL/NULL/NULL through fill; it is never replaced by current head.
- recovery watch: `protective_exit_recovery_watch` inherits the same causal SELL stamp held by the risk service.
- migration: v22 refreshes the narrow guard contract without backfilling any historical evidence; a v21 DB upgrade test proves the existing ledger receives the new semantics.

## Architecture

- `paper_trading.py`: before `16045 LOC / 282 top-level defs`; after `14847 LOC / 280 top-level defs` (provenance plumbing only).
- `paper_risk_service.py`: `883 LOC`, one risk-run application workflow.
- `paper_risk_evidence.py`: risk-only evidence/read-model adapters moved out of `paper_trading.py`.
- `paper_trading._monitor_risk_impl`: thin compatibility adapter, 4 LOC; calls `paper_risk_service.run(...)`.
- reverse imports: `paper_risk_service.py = 0`, `paper_risk_evidence.py = 0`.
- ports: 10 callable capabilities (including the injected cached-kline loader); projection is a two-method adapter object.
- `ARCHITECTURE.md` records the target dependency chain and the invariant `A claimed risk cycle never re-resolves "current cycle".`

## R20 invariants

- SELL fill commit owner remains `execution_planner.commit_fill`.
- `paper_risk_service.py` does not INSERT into `paper_fills`, stamp execution verification, credit cash, consume lots, or call `PPRS.finalize_sell`.
- Risk SELL still creates a pending order and delegates the commit exactly once.
- Rotation BUY delegates through the existing BUY adapter; the service does not create its own BUY commit path.

## Tests

```text
Targeted suite:
119 tests OK (risk application + strategy runtime + Guard 12 + schema/migration)
201 tests OK (sell / decision / asof / provenance regression)

Full backend:
3837 tests OK (skipped=5)

Frontend:
npm --prefix frontend run build: PASS (2 existing duplicate-key warnings)
npm --prefix frontend test --if-present: PASS
dist drift: 0

Syntax / quality:
python -m compileall -q backend: PASS
ruff check backend: All checks passed
```

## Mutation

`work/r21_mutation_check.py`:

```text
M-RSK1..M-RSK20
20/20 RED
survived=0
restore bytes byte-identical
restore sha256 PASS
```

This is a local mutation artifact, not a GitHub CI job claim.

## Security

```text
scope  : all
kinds  : none
values : 0
manual review: 2 existing image/binary assets
```

`security-leak-scan` is the CI hard gate; this local result is reported separately.

## Merge status

```text
Merge: NOT MERGED
Deploy: NOT DEPLOYED
```

No real broker trading is enabled; the workflow remains paper-only.
