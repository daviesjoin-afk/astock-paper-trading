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

- new module: `backend/paper_portfolio_read_model.py` (1227 LOC, 42 top-level defs)
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

- targeted portfolio read model: `66/66 PASS`
- architecture guard: `94/94 PASS`
- risk / authoritative-position / portfolio read-model suites: `122/122 PASS`
- full backend: `3913 tests OK (skipped=5)`
- frontend build: `PASS` (2 pre-existing duplicate-key warnings in `frontend/src/core/format.js`, not introduced by R22)
- frontend unit: `111/111 PASS`
- frontend e2e (chromium, `--workers=1`): `32 passed`
- dist drift: `PASS`
- compileall: `PASS`
- ruff: `PASS`

## Mutation

- `M-PORT1`..`M-PORT59`: `59/59 CAUGHT`
- survived: `0`; fake kills (Syntax/Import won): `0`
- non-vacuity (`--non-vacuity`, baseline GREEN then mutated RED on the designated test): `59/59`
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
- P1 closed legacy lots: source-less uncertainty is tracked per lot and only unresolved open quantity makes quantity proof unknown; fully consumed legacy lots no longer abort unrelated risk scans.
- P2 consumed legacy cash: strict cash and compatibility reconciliation inspect the full bounded lot set, including lots fully consumed by verified SELLs, so missing acquisition cash cannot be published as verified.
- P2 nonexistent cycles: initial capital requires a declared cycle row or at least one matching account ledger row; a missing cycle stays unknown instead of becoming a verified zero-capital portfolio.
- P2 pre-cycle reads: initial capital also requires a declared cycle row whose `created_at` date is not later than `asof_day`; a pre-cycle date stays unknown unless bounded lot/fill/order evidence already exists before that date.
- P1 source-order identity: a durable lot is trusted only when its source order and fill match the lot cycle/account/code and BUY side and the order is execution-verified; otherwise quantity and cash stay unknown.
- P2 incomplete lot schema: when `paper_position_lots` is absent or lacks required columns, quantity stays unknown instead of publishing a verified empty portfolio.
- P2 incomplete execution schema: when SELL/order evidence cannot be read, realized PnL stays unknown instead of publishing a verified zero.
- P2 mismatched fill identity: a BUY/SELL fill that disagrees with its order cannot leave a partial cash-flow projection for the order's real account/code; the affected key falls back to settlement cost.
- P2 account-scoped pre-cycle proof: bounded activity from another account can no longer authorize an account-specific initial capital before the cycle's `created_at`.
- P1 historical risk eligibility: bounded positions are always included in the risk scan scope for the requested as-of; current eligibility may add accounts but must not filter out reconstructed historical holdings.
- P2 partial order schema: an account-specific read on a database without `paper_orders.account_id` fails closed as unknown instead of raising `OperationalError`.
- P2 future source-less lot: a source-less lot whose untrusted `acquired_at` is later than the requested as-of still keeps quantity/risk proof unknown; it is not silently treated as absent.
- P2 partially bounded SELL order: order-level `realized_pnl` is published only after every fill for that order is bounded by the requested as-of; a first fill cannot leak later fill PnL.
- P1 multi-fill source order: a durable lot without fill-level allocation accepts a source order only when it has exactly one quantity-matching BUY fill; multi-fill and oversized/duplicate evidence remains unknown.
- P2 partial lot schema: account-specific pre-cycle activity checks require `paper_position_lots.account_id` before adding its predicate, so incomplete migrations remain unknown rather than raising SQL errors.
- P1 reused source fill: a `source_order_id` claimed by more than one durable lot no longer provides acquisition evidence for any of them (the schema has no uniqueness constraint on it), so a duplicated lot cannot inflate verified quantity, halve display cost, or double risk exposure.
- P2 partial fill schema: the fill readers' capability check now requires every column they actually `SELECT` (`price` / `amount` / `fees`), so a partially migrated `paper_fills` table falls back to unknown instead of raising `sqlite3.OperationalError` through realized-PnL, portfolio, and risk reads.
- P2 missing cycle creation evidence: a `paper_cycles` row without `created_at` is no longer treated as proof that the cycle already existed; pre-cycle initial capital still requires bounded activity evidence and otherwise stays unknown.
- P2 non-finite ledger values: numeric ledger evidence (fill `amount`/`fees`, order `realized_pnl`, cycle/account capital, lot `qty`/`cost`) must be finite — SQLite REAL columns can hold `inf` / `-inf` / `nan`, and publishing one made cash / realized PnL / NAV / exposure "verified infinite". `_ledger_num` now treats non-finite values as unknown, matching the policy `_valuation_price` already applies to prices.
- P2 risk-state identity columns: the `paper_position_risk_state` prerequisite check now also requires `account_id` / `code`, so a partially migrated table is treated as unavailable instead of raising `KeyError` out of the aggregator.
- P2 mismatched fill dating: only an identity-consistent fill may supply an order's economic date. An unrelated (another account / code / side) fill can no longer push a filled order past the as-of and hide both the fill and the order from the bounded sell checks, which would have published a pre-sale portfolio as verified.
- P2 missing lot `id` column: the bounded lot read is `ORDER BY ... id`, so `id` is now part of the `_POSITION_LOT_COLUMNS` prerequisite. A partially migrated table without it stays quantity unknown instead of raising `sqlite3.OperationalError`.
- P2 non-finite fees coerced to zero: `_ledger_num` now distinguishes "absent" (may take the default — an absent fee really is zero) from "present but non-finite" (stays unknown). Previously `fees=inf` was silently converted to `0.0` and published as verified cash.
- P2 non-finite sell quantity: a non-finite SELL fill quantity is rejected before FIFO conversion, so it can no longer raise `OverflowError: cannot convert float infinity to integer` out of portfolio reads and the risk scan.
- P2 non-finite source-backed lot cost: a lot whose cost is non-finite is rejected alongside its quantity, so a verified source order can no longer let `cost=inf` reach aggregation and publish infinite unrealized PnL / exposure.
- P1 mixed uncertain lots after a partial sell: uncertainty is only resolved when the whole account/code position is closed. A source-less lot and a verified lot can share the key, and a partial SELL may consume the source-less row first purely because its untrusted `acquired_at` sorts earlier — FIFO cannot prove which lot was actually sold, so the remainder's quantity, cost and entry date stay unknown.
- P2 partial order coverage: an order now counts as covered only when **every** selected fill is identity-consistent and verified. Previously one valid fill alongside a mismatched one let `verified_cash_flows()` publish a partial projection (e.g. half the cost) for the order's real account/code.
- P2 side-contradicting fills: the bounded fill readers no longer filter by the fill's declared side. A verified BUY order carrying an additional same-day SELL fill is contradictory execution evidence and now fails closed, instead of publishing only the BUY amount as verified cash/NAV when no lot exists.
- P2 account attachment date: a cycle's `created_at` says nothing about when a given account joined it. Account-level initial capital now also requires bounded attachment evidence (`paper_parameter_versions.effective_date <= asof_day`), so an account attached mid-cycle cannot lend its current `initial_cash` to a snapshot that predates its participation.
- P2 missing order identity in scoped fill checks: `_has_any_fill_rows` now returns `None` (unreadable) when an account-scoped read is requested on a `paper_orders` table lacking `account_id`, instead of omitting the account predicate and letting `cash()` publish the account's initial balance as verified zero net flow. Added PORT-5h and M-PORT59.
- each review fix has a permanent regression and a dedicated mutation (`M-PORT13`..`M-PORT59`).

## Security

- local sensitive-data scan (`--scope worktree`): `kinds: none`, `values: 0`
- manual review: `1` existing screenshot/binary entry (`docs/assets/dashboard.png`, pre-existing in master), no new sensitive findings
- GitHub Security Leak Scan: to be re-verified on the new head after push
- exact-head CI: to be re-verified on the new head after push (previously `9/9 PASS` on `5bebd30`)
- unresolved actionable review threads: `0` after this fix is pushed and the thread is resolved

## Merge status

MERGE: NOT MERGED
DEPLOY: NOT DEPLOYED
