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
    scan_uses_unbounded_effective_spec=True head_vs_pin_distinct=True head_hard_stop=-0.04 pin_hard_stop=-0.04
R21-C4 risk application orchestration remains in paper_trading: REPRODUCED
    _monitor_risk_impl LOC=651 responsibilities={snapshot, external evidence, quality review, capacity review, sell decision, sell execution orchestration, rotation, projection/nav, pending manual retry}
R21 before-fix reproduced: 4/4
```

After the fix, the same probe reports `0/4`; the real production contracts are now covered by `backend/test_risk_application_service.py` (RSVC-1..14).

## Correctness

- dynamic limit: the risk run passes the explicit claimed `cycle_id` and `asof_day` to `dynamic_position_limits`.
- downside profile: the risk run passes `asof_day`, `conn`, and `cycle_id` to `risk_profile` before an intraday guard can see it.
- SELL spec: the risk run uses `strategy_risk_enforcement.effective_spec_for_cycle` and never falls back to the current strategy head.
- cycle fence: external quote/news/fund-flow I/O ends before the write transaction; the first write-phase action is `paper_risk_scan_state.assert_cycle_active(cycle_id)`; rollover aborts without orders/reviews/risk writes.
- explicit context: `RiskRunContext(cycle_id, asof_day)` is immutable and rejects `None`; `run()` requires that context and does not resolve `active_cycle` or wall-clock dates for decision identity.

## Architecture

- `paper_trading.py`: before `16045 LOC / 282 top-level defs`; after `14840 LOC / 280 top-level defs`.
- `paper_risk_service.py`: `877 LOC`, one risk-run application workflow.
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
297 tests OK

Full backend:
3826 tests OK (skipped=5)

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
M-RSK1..M-RSK16
16/16 RED
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
