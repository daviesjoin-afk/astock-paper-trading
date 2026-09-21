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

- new module: `backend/paper_portfolio_read_model.py` (916 LOC, 34 top-level defs)
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

- targeted portfolio read model: `34/34 PASS`
- architecture guard: `94/94 PASS`
- risk / authoritative-position / sell-convergence suites: `113/113 PASS`
- full backend: `3880 tests OK (skipped=5)`
- frontend build: `PASS` (2 pre-existing duplicate-key warnings in `frontend/src/core/format.js`, not introduced by R22)
- frontend unit: `111/111 PASS`
- dist drift: `PASS`
- compileall: `PASS`
- ruff: `PASS`

## Mutation

- `M-PORT1`..`M-PORT27`: `27/27 RED`
- survived: `0`
- restore sha256: `PASS`

## Review fixes

- P1 unknown cash: strict `PortfolioReadContext.cash()` stays `unknown`; the R21 compatibility port now uses a bounded compatibility estimate that subtracts recorded fills or held lot cost instead of substituting untouched full capital.
- P2 mixed unverified BUY display flow: every relevant BUY/SELL fill must be identity-consistent and verified before a per-symbol `verified_cash_flow` projection is published; otherwise display cost falls back to durable lot settlement cost.
- P2 account cycle scope: account-specific initial capital now requires `paper_accounts.cycle_id == requested cycle_id`; a rebounded account cannot lend its new cycle's capital to an old cycle read.
- P1 filled BUY order without fill evidence: strict cash now inspects all bounded filled BUY orders and returns unknown when any lacks verified fill evidence.
- P1 explicit-cycle exposure: risk exposure now uses `PortfolioReadContext.positions_for_context`, not current `remaining_qty`, so future acquisitions/sales cannot alter the historical pool NAV.
- P2 unknown quantity propagation: `portfolio_for_context` now carries `quantity_status`; unknown holdings force market value, unrealized PnL and NAV to remain unknown.
- P1 lot economic date: lot as-of bounding now prefers `paper_fills.fill_date` (falling back to durable order `executed_at` only when no fill row exists), not wall-clock `acquired_at`.
- P1 risk fail-closed: explicit-cycle exposure now reads `quantity_status` and raises `PortfolioReadUnavailable` instead of valuing unproven holdings.
- P2 missing-order fill evidence: display cash flow now also inspects bounded filled BUY/SELL orders for verified fill evidence, blocking partial per-symbol projections.
- P1 risk snapshot positions: paper_risk_service now reads the bounded status-returning position API for both snapshot and write-time loops, with cycle-owned risk state still injected separately.
- P2 filled-order bounding: BUY/SELL filled-order checks now use the economic fill date when present and fall back to executed_at only for genuinely fill-less legacy orders.
- P2 archived cycles: archived cycles now return unknown portfolio/cash/realized facts instead of a fabricated verified empty portfolio.
- P2 valuation sanity: non-finite and non-positive valuation evidence is treated as unavailable, so NaN/inf cannot produce verified NAV.
- P1 mixed fill-less lots: when recorded fill flows exist, compatibility cash now also subtracts the original cost of durable lots that have no linked BUY fill, preventing fill-less legacy lots from inflating NAV.
- P2 future runtime risk state: risk positions only accept `paper_position_risk_state` rows whose `initialized_at` and `updated_at` dates are not later than `asof_day`; future peak / take-stage / re-entry state is treated as missing rather than injected into historical replay.
- P1 source-less durable lots: strict cash now returns unknown when any open durable lot lacks linked BUY fill evidence; compatibility cash reconciles its original cost instead of treating cash flow as zero.
- P1 source-less historical lots: `bounded_lots_with_status` marks `source_order_id IS NULL` lots as quantity unknown; explicit exposure/risk reads fail closed instead of promoting `acquired_at` to authority.
- each review fix has a permanent regression and a dedicated mutation (`M-PORT13`..`M-PORT27`).

## Security

- local sensitive-data scan: `kinds: none`, `values: 0`
- manual review: `1` existing screenshot/binary item (`docs/assets/dashboard.png`), no new sensitive findings
- GitHub Security Leak Scan: pending exact-head verification

## Merge status

MERGE: NOT MERGED
DEPLOY: NOT DEPLOYED