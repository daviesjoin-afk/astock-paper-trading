## Base

- R21 #178 merged
- exact base SHA: `a628b194cdf984b11a8cbd3eaba0266f28342abc`
- branch: `codex/portfolio-read-model-closure`

## Before-fix

- C1 later-cycle position contamination: `REPRODUCED`
- C2 display-cost cross-cycle contamination: `REPRODUCED`
- C3 future fill contamination: `REPRODUCED`
- C4 pending/unverified order contamination: `NOT REPRODUCED` (current lot path already ignored pending orders)
- C5 projection corruption: `NOT REPRODUCED` (lot reader already ignored `paper_positions`)
- C6 current-cycle fallback: `REPRODUCED` through `_shared_account_exposure`
- C7 wall-clock leakage: `NOT REPRODUCED` for the exact-cycle facade
- C8 unknown valuation preservation: `NOT REPRODUCED` because no bounded historical valuation contract existed at the base

Base probe summary: `4/8 reproduced`.

After-fix probe summary: `0/8 reproduced`; C1/C2/C3/C6 now use the explicit bounded read model or explicit-cycle exposure path, and C8 verifies `market_value=None`, `nav=None`, `status=unknown` without a current-quote fallback.

## Authority model

- quantity authority: `paper_position_lots`
- cost authority: durable lot `cost`; verified cash-flow display cost only when the complete cycle/as-of cash flow is proven
- realized PnL authority: verified committed SELL execution facts written by `execution_planner.commit_fill`
- cash authority: `paper_cycles.capital` + verified fill cashflows up to `asof_day`
- projection status: `paper_positions` remains compatibility-only, zero execution authority
- valuation behavior: explicit bounded valuation evidence only; missing historical valuation remains `unknown`
- read boundary: immutable `PortfolioReadContext(cycle_id, asof_day)` in `paper_portfolio_read_model.py`
- read model is read-only and never resolves `active_cycle`, `today`, or a current quote

## Correctness

- cycle ownership: lots and verified fills are filtered by exact `cycle_id`
- as-of ownership: lot acquisition and fill facts use `<= asof_day`; future rows are excluded
- future-fill exclusion: later BUY/SELL cannot change an earlier as-of portfolio view
- projection isolation: `paper_positions` is never consulted as quantity/cost authority
- unknown preservation: missing price keeps market value, unrealized PnL and NAV as `None` / `unknown`
- cash/NAV: bounded by the requested cycle and as-of day; no current account cash fallback in the strict read model
- R20 `execution_planner.commit_fill` remains the only fill/PnL commit path
- R21 `paper_risk_service` remains the risk application authority; only its shared-exposure port now carries the explicit cycle
- legacy/unverified cash fallback (risk compatibility only) uses durable cycle initial capital, not current account cash

## Architecture

- new module: `backend/paper_portfolio_read_model.py` (639 LOC, 25 top-level defs)
- `paper_trading.py` LOC: `14847 -> 14847`
- `paper_trading.py` top-level defs: `280 -> 280`
- reverse imports: `0` (new read model does not import `paper_trading`)
- facade status: `paper_trading` remains compatibility wiring; `_shared_account_exposure` gained an explicit `cycle_id` keyword
- `paper_position_read_model.py` live/risk semantics remain unchanged for R19/R20/R21 compatibility
- no schema changes or migrations
- architecture guard: new `paper_portfolio_read_model.py` is registered in the domain boundary and Guard 13 scans no reverse import, no execution mutation, no active-cycle resolution, no wall clock, explicit context, and no schema change

## R19/R20/R21 invariants

- BUY authority unchanged: `execution_planner` remains the execution planner; no new BUY path
- fill authority unchanged: `execution_planner.commit_fill` remains the single fill commit primitive
- risk application authority unchanged: `paper_risk_service` remains the risk application orchestration owner
- no new table/DDL, no direct lot/cash mutation, no copied commit path

## Tests

- targeted portfolio read model: `16/16 PASS`
- architecture guard: `94/94 PASS`
- full backend: `3862 tests OK (skipped=5)`
- frontend build: `PASS` (2 pre-existing duplicate-key warnings in `frontend/src/core/format.js`, not introduced by R22)
- frontend unit: `111/111 PASS`
- dist drift: `PASS`
- compileall: `PASS`
- ruff: `PASS`

## Mutation

- `M-PORT1`..`M-PORT12`: `12/12 RED`
- survived: `0`
- restore sha256: `PASS`

## Security

- local sensitive-data scan: `kinds: none`, `values: 0`
- manual review: `2` existing screenshot/binary items (`docs/assets/dashboard.png`), no new sensitive findings
- GitHub Security Leak Scan: pending exact-head verification

## Merge status

MERGE: NOT MERGED
DEPLOY: NOT DEPLOYED