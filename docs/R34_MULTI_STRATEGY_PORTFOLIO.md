# R34 Multi-Strategy Portfolio

## Scope and baseline

R34 is delivered in three reviewed stages: R34-A captures exact portfolio runtime facts; R34-B consumes those facts for allocation/conflict/capacity policy; R34-C wires approved plans into production and converges callers. This document is the R34-A owner inventory and scope boundary.

R34-A base (`R34_BASE`): `a1ee679b4609b1f7d44f88151614b379f7cc6ffa` (R33-C merge commit; PR #224). Branch: `codex/r34a-portfolio-runtime-facts`.

This stage records what an explicitly selected cycle owned or exposed at a stated time. It does not rank strategies, choose weights, grant or block risk permission, change capital, or place orders. No frontend is planned for R34-A.

## Frozen authority boundaries

```text
Strategy = wants / intent
Allocator = resource allocation
Risk = allow / block

cycle economic ownership != execution eligibility
                       != risk-exit eligibility
                       != portfolio allocation

Cycle owns capital.
Lifecycle controls execution permission.
Existing exposure still owns risk-exit rights.
```

Pausing, retiring, or archiving a strategy must not detach its cycle account. An account with remaining durable lots must remain eligible for risk-exit evaluation. R33 health evidence is not a score, rank, tier, or allocation weight.

## Existing authority inventory

| Candidate fact / owner | Identity and mutability | Historical/as-of support and failure semantics | Canonical R34-A use | Duplication / implicit lookup / convergence |
| --- | --- | --- | --- | --- |
| Cycle identity and declared owners — `paper_cycles`, `paper_cycle_ownership.py` | Explicit `cycle_id`; `cycle_key` identifies the cycle. `enabled_strategies`, status, and timestamps are operationally mutable. | `exact_cycle_owner_snapshot()` requires an explicit cycle, validates the owner list, and returns `cycle_ledger_ids`; it has no latest or legacy fallback. General helpers retain their compatibility fallback behavior. | Capture exact declared owner set and cycle identity. Missing/corrupt owner evidence fails closed; no fallback cycle. | Keep this as ownership owner. No second ownership resolver. Historical cycle status is captured as observed metadata, not an event replay. |
| Exact cycle strategy pins — `paper_cycle_strategy_versions`, `strategy_registry.py` | `(cycle_id, account_id)` maps to `strategy_id`, `strategy_version`, `strategy_checksum`, `bound_at`; version rows and cycle pin rows are intended immutable. | `cycle_stamp_for_account(..., cycle_id=...)` is exact and has no current-head fallback; `cycle_version_for_account` verifies the immutable version. `stamp_for_account` can fall back to legacy bindings/current user head and is not suitable for replay. | Capture exact account-to-strategy pins and checksum; fail closed for missing, partial, or checksum-mismatched pins. | Reuse Registry exact getters. Do not duplicate strategy version storage or use current head. |
| Economic ownership / cycle capital — `paper_cycle_ownership.py`, `paper_cycle_capital.py` | Cycle id plus account ids. `paper_accounts.cycle_id`, `initial_cash`, and `cash` can change; `paper_cycles.capital` is cycle-level configuration. | Ownership is cycle-bounded. `paper_cycle_capital` computes unallocated and late-join reference capital but includes a fallback division when no funded accounts exist. It is not an as-of account-balance owner. Historical cash is reconstructed by the portfolio read model from cycle capital and verified fills. | Reuse exact owner set and bounded accounting facts. Distinguish fixed cycle capital, initial sleeve capital, reconstructed cash, committed and pending reservation facts; report any unprovable historical value unavailable. | No second capital algorithm. Avoid treating current mutable `paper_accounts.cash` as historical. Record fallback semantics for later convergence. |
| Shared-pool allocator — `paper_allocation.py` | Runtime inputs include strategy id, lifecycle stage, factor fields, positions, capital and optional `StrategyRuntime.health`; output is a computed plan. | Deterministic for supplied inputs, but it is policy/decision logic rather than historical fact evidence. No fact-level as-of history is implied by its output. | Do not invoke to produce runtime facts. Never map health availability to a numeric health/weight. | Reuse in R34-B; no allocator copy in R34-A. Audit/retire generic health factor during later convergence only. |
| Cross-strategy coordinator — `portfolio_coordinator.py` | Inputs are positions, quotes, pending orders and optional theme map; output aggregates symbol/industry/theme and headroom. | `pending_symbol_amounts` and `pending_risk_exit_codes` turn SQLite errors into empty collections. `_positions_and_quotes` substitutes position cost when quote is absent. Queries are not explicitly cycle/as-of bounded. These are operational compatibility fallbacks, not canonical evidence. | Do not call as the canonical capture path. Pending read failure must be PARTIAL/UNAVAILABLE; missing quote must not produce market value from cost. | Keep old caller semantics stable in this stage; add a strict canonical read path through existing owners. Record both error-as-empty and cost-as-market fallback for later convergence. |
| Portfolio projection — `paper_portfolio.py`, `paper_portfolio_read_model.py` | Read model context is exact `(cycle_id, asof_day[, account_id])`; durable lot identity is cycle/account/code/lot id. `paper_positions` is a mutable compatibility projection and not authoritative. | Bounded lots, cash, realized PnL, and `accounting_fact_projections` are cycle/as-of aware and fail closed to unknown when reconstruction is not provable. `portfolio_for_context` accepts explicit valuations and returns unknown market value if a position has no valid price. Legacy pure `exposure()` defaults missing quote to cost and is not canonical. | Reuse bounded lot/accounting readers. Keep `cost_basis` separate from market value; use only explicit market evidence. Snapshot must freeze the resulting facts and coverage. | No second lot, cash, FIFO, or exposure algorithm. R34-A may add a snapshot contract around existing owner projections. Do not read `paper_positions` for canonical facts. |
| Risk-exit scope — `paper_risk_exit_eligibility.py`, facade in `paper_trading.py` | Owner unions execution participants with accounts that own remaining lots. | Current helper retains its historical global query by default; explicit `cycle_id` scopes the operational query. Historical R34-A capture supplies exact as-of lot owners from the bounded portfolio owner to the same union function. | Keep economic owners and execution participants independent; add exact-as-of remaining-lot owners to risk-exit scope. Unknown bounded lots fail closed. | No second remaining-lot or union implementation. Legacy calls without new arguments preserve behavior. |
| Risk facts and decisions — `paper_risk_service.py`, `risk_center.py` | Risk service is given cycle/run context, exact strategy stamps and market/position ports; risk state tables are cycle/account/code bounded where available. `risk_center` is a dashboard projection over refreshed snapshots. | Risk service applies live allow/block/exit semantics; dashboard snapshots are refreshed/current and do not establish arbitrary historical facts. Existing risk state has as-of timestamps but incomplete risk-consumption history is possible. | Only project owner-issued risk usage/limit evidence with exact source identity and time. Otherwise mark unavailable. Never recompute ALLOW/BLOCK or alter risk results. | Reuse risk owners; no portfolio risk engine. Keep current/dashboard reads out of historical capture. |
| Strategy runtime — `strategy_runtime.py` | Exact strategy definition/version context and settings revision; compiled risk/execution policy projections. | Some context depends on caller-supplied DB/current settings; no portfolio historical snapshot is provided by the module itself. | Use exact pinned version only where a needed fact is explicitly provided and fingerprinted; do not synthesize portfolio style or allocation health. | Keep runtime compilation owner; no new parallel strategy runtime. |
| Lifecycle permission — `strategy_lifecycle.py` | Exact `(strategy_id, version, checksum)` state and append-only lifecycle events. Current state is mutable through events. | Exact lifecycle history exists; transition owner decides legality. Helpers that use current heads are not suitable for arbitrary cycle replay. | Capture the exact pinned version's observed lifecycle state as permission context; preserve economic and risk-exit sets independently. | Do not import lifecycle into portfolio pure domain or modify transition semantics. |
| Signal intent/provenance — `signal_service.py`, `strategy_selection_resolver.py`, `paper_signals` | Signal rows carry signal id, account, signal/intended dates, code, status/payload, strategy id/version/checksum and cycle id; canonical writer freezes provenance supplied by `SignalWriteContext`. | Rows may be refreshed/upserted in business columns, so they are not immutable historical signal snapshots. Exact stamps and explicit time bounds are required; orders cannot stand in for historical signal intent. | A conflict is AVAILABLE only when both exact-cycle, exact-version intents and explicit as-of are provable. Otherwise PARTIAL/UNAVAILABLE; no inference from orders. | Reuse signal writer/provenance. Later consider append-only intent evidence if existing refreshed rows cannot replay exact signals. |
| Execution / turnover evidence — `execution_evidence.py`, `paper_orders`, `paper_fills` | Fill id references order id/account/code/side/qty/price/fees/fill date/quote time; order has cycle and strategy stamp plus mutable execution status/verification fields. | Fills are execution records; orders evolve. Verified execution contract distinguishes verified from unverified. Turnover needs exact cycle, stamp, verified fills and explicit window. | Use verified exact fills only. Missing proof makes turnover PARTIAL/UNAVAILABLE, not zero. A known empty verified window may be a proven zero. | Reuse execution evidence owner; do not create a second execution verifier. |
| Market data / valuation — `market_data_contract.py`, `market_data_service.py`, quote policies | Market evidence has explicit kind/source/observed-at/as-of/verification/fingerprint dimensions. A quote is not identified by code alone. | Contract distinguishes missing, stale, disagreement, single-source and not-attempted. Current quote cache is not arbitrary historical evidence. | Require explicit market evidence identity and verify each quote's as-of/source/fingerprint before valuation. Missing exact quote => market value PARTIAL/UNAVAILABLE; never substitute cost. | Reuse Market Data authority and policy. Do not fetch network or use current/latest cache in builder. |
| Sector / industry / theme / style classification | Lots/signals may persist an `industry` string. Coordinator has a static industry-to-theme map. No exact strategy-version style authority or classification provenance is established by these fields alone. | Lot industry may be a capture-time string without source fingerprint; theme is derived from mapping; a missing classification is not evidence of a category. | Sector/theme concentration only when classification source and market-valued exposure are both exact. Style concentration unavailable until an exact style owner exists. | Do not infer from strategy/stock names. Treat coordinator theme map as legacy compatibility, not canonical classification authority. |
| Return correlation | No exact strategy-version historical return-series owner was found in the candidate portfolio owners. | No series identity, window, adjustment policy, or missingness contract to replay. | Always `UNAVAILABLE` in this stage unless inventory is revised with a concrete exact owner. Never substitute overlap, sector, risk fingerprint, or name similarity. | Do not add a correlation engine or score. Position overlap remains a distinct future fact. |

## Canonical path constraints

- Facts are captured for explicit `cycle_id`, `asof_day`, and `decision_at`; the pure builder receives all inputs and has no database, network, clock, provider, or current/latest lookup.
- Strategy pins are exact `(strategy_id, strategy_version, strategy_checksum)` identities and are sorted canonically for fingerprints.
- Each dimension records `AVAILABLE`, `PARTIAL`, `UNAVAILABLE`, or `NOT_APPLICABLE`, plus facts, provenance, source identity/fingerprint, and blocking reasons.
- Snapshot fingerprint excludes `created_at`; persistence metadata may record it.
- Market value never aliases cost basis. Correlation, overlap, concentration, risk consumption, capacity, cash, and allocation remain separate facts.
- Capture appends a snapshot only. It does not write accounts, cycles, orders, fills, lots/positions, or lifecycle state.
- Exact snapshot persistence, if retained after inventory, is append-only with fingerprint verification and only `append_snapshot()` / `get_snapshot(snapshot_id)` repository methods.
- Implemented authority: `portfolio_runtime.py` (pure builder), `portfolio_runtime_repository.py` (append + exact get), `portfolio_runtime_service.py` (explicit owner reads), and `POST/GET /api/portfolio/runtime/snapshots`.
- Dimensions currently provide bounded capital/accounting and per-account/per-symbol cost basis. Market value, concentration, turnover, risk consumption, signal conflict, pending capacity, and correlation are honestly PARTIAL/UNAVAILABLE because exact owner inputs are absent; no zero, cost-as-market, style guess, or allocation score is emitted.
- Capture persists only its append-only snapshot row. It reads exact-cycle ownership/pins/lifecycle and bounded as-of lots/accounting; it does not write the formal ledger or lifecycle.

## R34 stage boundary

```text
Existing Owners → Portfolio Runtime Facts → PortfolioRuntimeSnapshot → STOP (R34-A)
PortfolioRuntimeSnapshot → R34-B Allocator Policy → AllocationPlan (reserved)
```

R34-B is responsible for resource allocation, budgets, conflict arbitration, and capacity policy while reusing `paper_allocation` and coordinator ownership. R34-C is responsible for production wiring, old caller migration/removal, and the Portfolio Workspace. Neither is implemented here.

## R34-A verification and status

Focused owner suites and `test_portfolio_runtime` pass. Mutation matrix `work/r34a_portfolio_runtime_mutation_check.py`: M-P1…M-P10 10/10 detected; survived/fake/timeout = 0; restore SHA256 PASS.

R31, R32, and R33 are COMPLETE. R34-A is IN REVIEW after this PR; R34-B/C and R34 are NOT STARTED/NOT COMPLETE; R35–R37 are NOT STARTED. This PR remains unmerged and undeployed until human review.
