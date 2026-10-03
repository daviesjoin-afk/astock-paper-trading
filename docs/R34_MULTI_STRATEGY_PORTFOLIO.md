# R34 Multi-Strategy Portfolio

## Scope and baseline

R34 is delivered in three reviewed stages: R34-A captures exact portfolio runtime facts; R34-B consumes those facts for allocation/conflict/capacity policy; R34-C wires approved plans into production and converges callers. This document is the R34 owner inventory and scope boundary for R34-A and R34-B.

R34-A base (`R34_BASE`): `a1ee679b4609b1f7d44f88151614b379f7cc6ffa` (R33-C merge commit; PR #224). Branch: `codex/r34a-portfolio-runtime-facts`.

R34-B base: `186e0bfb4e2c4c6da67ecb2c68086ffed1b19f04` (R34-A merge commit; PR #225). Branch: `codex/r34b-portfolio-allocation-policy`. The tree at this merge commit is identical to the approved R34-A head `771921d64d87bf09981e2c58cec309c0a767f4c0`.

R34-A records what an explicitly selected cycle owned or exposed at a stated time. R34-B turns those facts, plus explicit strategy declarations and explicit resource intents, into one deterministic allocation plan. Neither stage ranks strategies, grants or blocks risk permission, changes capital, places orders, or exposes a frontend.

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
| Cycle identity and declared owners — `paper_cycles`, `paper_cycle_ownership.py` | Explicit `cycle_id`; `cycle_key` identifies the cycle. `enabled_strategies`, status, and timestamps are operationally mutable. | `exact_cycle_owner_snapshot()` requires explicit `cycle_id` and `asof_day`, validates an exact configured/bound owner set, and reuses `paper_portfolio_read_model.account_attached_by_asof`; it has no latest or legacy fallback. General helpers retain their compatibility fallback behavior. | Capture exact declared owner set and cycle identity. Missing/corrupt or not-yet-attached owner evidence fails closed; no current-binding fallback. | Keep this as ownership owner. No second ownership resolver. Historical cycle status is captured as observed metadata, not an event replay. |
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

## R34-B owner arbitration

R34-B does **not** create a third allocation implementation. `paper_allocation`
stays the only owner of allocation arithmetic; the new policy layer only owns a
contract, strict input validation, the policy version, the evidence gates,
orchestration of the existing pure arithmetic, and the plan builder.

### `paper_allocation.py` — retained as the sole allocation arithmetic owner

| Symbol | Nature | Production callers | R34-B disposition |
| --- | --- | --- | --- |
| `position_limits_from_weights` | pure arithmetic core over **given** weights and declared caps/floors (new) | `portfolio_allocation_policy` (canonical), `position_limits` (legacy) | the single arithmetic site |
| `position_limits` | legacy adapter: `StrategyRuntime` → `effective_weight()` → core | `paper_trading.py` (`_dynamic_position_limits`) | public signature, output shape and behavior unchanged |
| `strategy_pool_budget` | pure, `weights=` override only overwrites known keys | `paper_trading.py` | legacy contract unchanged; canonical coverage is validated in the policy before any arithmetic call |
| `allocation_plan`, `pool_headroom`, `deployable_budget`, `stage_capital_scale`, `minimum_deployable_budget`, `diversification_factor` | pure arithmetic | `paper_trading.py`, `strategy_runtime.py` | reused as-is; never copied |
| `StrategyRuntime.effective_weight()` (six dynamic factors, default `1.0`) | pure, but the defaults are **not** owner evidence | no external caller | kept for the legacy path; the canonical path never reaches it |

### `portfolio_coordinator.py` — no longer a canonical authority

| Symbol | Nature | Production callers before | R34-B disposition |
| --- | --- | --- | --- |
| `classify_intent` | free-text keyword guess of business intent | **0** | **deleted** |
| `sort_intents_by_priority` | pure sorter over the deleted classification | **0** | **deleted** |
| `_EXIT_PURPOSE_KEYWORDS` | keyword table for the above | **0** | **deleted** |
| `INTENT_PRIORITY`, `INTENT_PRIORITY_INDEX`, `INTENT_LABELS` | dead constants once the two functions above were removed | **0** | **deleted**; the canonical vocabulary lives in `portfolio_allocation_policy` |
| `PORTFOLIO_COORDINATOR_VERSION` | version string | `aggregate_exposure`, `symbol_headroom` | **kept** |
| `DEFAULT_THEME_MAP`, `theme_for` | legacy static industry→theme map | `paper_trading.py` | **kept**; canonical concentration stays `UNAVAILABLE` |
| `pending_symbol_amounts`, `pending_risk_exit_codes` | DB-bound; `sqlite3.Error` → `{}` / `set()` | `paper_trading.py` | **kept**; **forbidden** on the canonical path |
| `_positions_and_quotes` | substitutes position cost when a quote is absent | internal | **forbidden** on the canonical path |
| `aggregate_exposure`, `symbol_headroom` | pure, but cost-as-market inputs | `paper_trading.py` | **kept**, R34-C migration target |

### Convergence table

| Business rule | Current owner | Canonical future owner | Callers before | R34-B callers after | R34-C migration target | Deletion candidate |
| --- | --- | --- | --- | --- | --- | --- |
| Slot allocation arithmetic | `paper_allocation.position_limits` | same, via `position_limits_from_weights` | 1 (`paper_trading`) | 1 legacy + 1 canonical | converge on the core | no |
| Legacy declaration → weight derivation | `StrategyRuntime.effective_weight` | explicit canonical weights | 1 (`paper_trading`) | unchanged | migrate callers off dynamic factors | no |
| Resource execution priority | `portfolio_coordinator.classify_intent` / `sort_intents_by_priority` | `portfolio_allocation_policy.INTENT_KINDS` | 0 | **0** | — | **deleted in R34-B** |
| Pending buy capacity | `portfolio_coordinator.pending_symbol_amounts` | exact as-of capacity dimension (absent) | 3 (`paper_trading`) | 3 legacy | exact capacity owner | no |
| Risk-exit in-flight codes | `portfolio_coordinator.pending_risk_exit_codes` | exact as-of intents (absent) | 1 (`paper_trading`) | 1 legacy | exact intent evidence | no |
| Symbol/industry/theme exposure | `portfolio_coordinator.aggregate_exposure` / `symbol_headroom` | market-valued exposure dimension (absent) | 5 (`paper_trading`) | 5 legacy | market valuation owner | no |

## Canonical weight semantics

The canonical weight of a strategy is an **explicit declaration**, supplied by
the caller and required to cover exactly the snapshot's eligible resource
strategy ids. The canonical path never reads `StrategyRuntime.health`,
`regime_fit`, `confidence`, `data_quality`, or `diversification`; those fields
default to `1.0` in the library and a default is not owner-issued evidence.
R33 health evidence is not mapped to a numeric factor. A weight set that misses
a strategy fails closed as `canonical_allocation_weight_set_incomplete`; a weight
set with a stray strategy fails closed as
`canonical_allocation_weight_set_exceeds_eligible`. There is no fallback to a
dynamic factor, no fallback to `1.0`, and no silent ignore.

## R34-B v1 capability boundary

What the current R34-A evidence actually supports, and the resulting honest plan
status:

| R34-A fact | Status | R34-B component | v1 result |
| --- | --- | --- | --- |
| `execution_participant_ids` | AVAILABLE | `slot_plan` | **PLANNED** |
| explicit `intent_kind` + canonical priority | supplied by the caller | `conflict_plan` | **PLANNED** |
| `strategy_exposure` | PARTIAL — cost basis only, `market_value_by_account` is `None` | `capital_plan` | **INSUFFICIENT_EVIDENCE** |
| `capacity` | UNAVAILABLE | `capacity_plan` | **INSUFFICIENT_EVIDENCE** |
| `concentration` | UNAVAILABLE | `concentration_adjustment` | **UNAVAILABLE** |
| `correlation` | UNAVAILABLE | `correlation_term` | **UNAVAILABLE** |

Overall `plan_status` for a non-empty eligible set is therefore **PARTIAL**: some
resource facts are computable, others are not. Forbidden substitutions are
enforced and asserted: cost basis never becomes market value, missing pending is
never `0`, missing capacity is never unlimited, missing correlation is never `0`,
missing classification never creates a concentration fact, and a missing dynamic
factor is never claimed as `1.0`. Capacity is only reported as `PLANNED` when
the owner-issued `used`/`pending`/`headroom` facts are actually present, in which
case they are echoed verbatim rather than derived.

New-resource eligibility reads **only**
`PortfolioRuntimeSnapshot.execution_participant_ids`. `economic_owner_ids` keeps
economic ownership and `risk_exit_participant_ids` keeps exit rights; neither
grants new-entry resources. A paused economic owner therefore retains ownership
and receives no new-resource allocation, and a risk-exit-only account can exit
but receives no entry allocation. No second eligibility resolver exists.

## PortfolioAllocationPlan contract

Immutable, fingerprinted, and input-order independent. `plan_id` equals
`plan_fingerprint`; `created_at` is persistence metadata and never fingerprint
material; `portfolio-allocation-policy-v1` is bound into the fingerprint, so
changing allocation arithmetic, priority rules, conflict rules, or required
evidence requires a policy version bump and old plans do not drift.

Carried fields: `plan_id`, `plan_fingerprint`, `portfolio_snapshot_id`,
`portfolio_snapshot_fingerprint`, `allocation_policy_version`, `cycle_id`,
`asof_day`, `decision_at`, `strategy_pins`,
`eligible_resource_strategy_ids`, `strategy_resource_declarations`,
`allocation_weights`, `slot_plan`, `capital_plan`, `conflict_plan`,
`capacity_plan`, `concentration_adjustment`, `correlation_term`,
`blocking_reasons`, `source_identities`, `source_fingerprints`, `plan_status`.

Conflict policy consumes only explicit intents with an explicit `intent_kind`
from the canonical vocabulary `RISK_EXIT > TAKE_PROFIT_EXIT > MANUAL_EXIT >
RISK_REDUCE > NEW_ENTRY > ADD_POSITION`. That ordering is resource *execution*
priority, never a Risk approval. Opposite intents on one symbol are never netted
away: both keep their own provenance and their own arbitration result, and a
RISK_EXIT is never deferred behind an entry. The allocator emits no ALLOW/BLOCK
and produces no Risk decision; the future production chain remains Strategy
intent → allocation allowance → Risk authority → execution, with Risk always
able to block.

`plan_status` uses only `PLANNED`, `PARTIAL`, `INSUFFICIENT_EVIDENCE`, and
`NO_ELIGIBLE_STRATEGIES`. There is no `GOOD_PORTFOLIO`/`BAD_PORTFOLIO`/`OPTIMAL`,
no score, no rank, and no winner.

## Persistence, service and API

`portfolio_allocation_plans` is created by migration v31
(`paper_schema_migrations.ensure_portfolio_allocation_plans`) with
`CHECK(plan_id = plan_fingerprint)` and append-only `no_update` / `no_delete`
triggers. The repository exposes `append_plan()` and an exact `get_plan(plan_id)`
only — no `get_latest_plan`, no `get_current_plan`, no overwrite, no delete. A
repeated append of the same `plan_id` is idempotent; the same `plan_id` with
different content is a conflict. No runtime DDL anywhere.

`portfolio_allocation_service` loads the exact named snapshot, verifies its
fingerprint, loads the exact cycle-pinned declarations (identity from the
snapshot pins, caps from the exact pinned version's compiled risk profile),
validates the explicit intents, evaluates the policy, and appends the plan. It
never writes `paper_accounts`, `paper_cycles`, orders, fills, position lots, risk
decisions or lifecycle, and never creates schema.

`POST /api/portfolio/allocation/plans` and
`GET /api/portfolio/allocation/plans/{plan_id}` are the only endpoints. There is
no `current`, no `latest`, no `rebalance-now`, and no apply/execute path.

## R34 stage boundary

```text
Exact Portfolio Snapshot
        + Exact Strategy Allocation Declarations
        + Explicit Resource Intents
        ↓
Multi-Strategy Allocation Policy (portfolio-allocation-policy-v1)
        ↓
PortfolioAllocationPlan
        ↓
STOP (R34-B)
```

R34-C is responsible for controlled production wiring, resource reservation and
sizing, old caller migration/removal, and the Portfolio Workspace:

```text
PortfolioAllocationPlan
        ↓
R34-C Controlled Production Wiring
        ↓
Sizing / Resource Reservation
        ↓
Risk
        ↓
Execution
```

## Verification and status

R34-A: focused owner suites and `test_portfolio_runtime` pass; mutation matrix
`work/r34a_portfolio_runtime_mutation_check.py` reports M-P1…M-P12 12/12
detected, survived/fake/timeout = 0, restore SHA256 PASS. PA21/PA22 guard
historical exact owner resolution; PA23 guards the service/schema ownership
boundary. Snapshot DDL remains owned by migration v30.

R34-B: `test_portfolio_allocation_policy` covers B1–B36 plus the canonical
business invariants migrated off the deleted text-derived path;
`test_paper_allocation_limits_equivalence` proves the `position_limits` refactor
is behavior-equivalent to the pre-refactor arithmetic over a 300+ case sweep
(0/1/N strategies, varied weights, caps, mins, `account_order`,
`protected_slot_floor`, `baseline_exposure`, comparing `total_cap`,
`risk_scale`, `protected_slot_floor`, `limits`, `effective_weights`); mutation
matrix `work/r34b_allocation_mutation_check.py` reports M-B1…M-B20 20/20
detected, survived/fake/timeout = 0, restore SHA256 PASS. Plan DDL is owned by
migration v31.

Review follow-ups closed at this head:

- B31 / B32: a negative pool or slot bound and a negative canonical weight fail
  closed instead of being fed to the arithmetic (a negative bound previously
  produced a `PLANNED` plan with negative `total_cap` and negative per-strategy
  limits).
- B33: an intent the policy has already denied no longer arbitrates, so a denied
  `RISK_EXIT` cannot defer a valid entry for the same symbol.
- B34: the strategy declaration carries an allocation **stage**, not the raw
  lifecycle state. `paper` is a lifecycle state, not a stage, and passing it
  straight through fell into the unknown-stage branch and silently declared the
  quarantined capital scale of `0.0` for every real strategy. The stage now comes
  from the existing owner mapping `strategy_runtime.lifecycle_stage_for` instead
  of a second, parallel rule.
- B35: allocation stage uses exact pinned-version metadata together with the
  snapshot lifecycle state. The service regression pins v1/pilot, advances the
  registry head to v2/mature, and verifies the plan retains v1 identity and pilot.
- B36: only `risk_exit_participant_ids` grants exit eligibility. An economic-only
  paused owner without an as-of open lot is denied `RISK_EXIT` scope and cannot
  defer an eligible same-symbol `NEW_ENTRY`.

R31, R32, and R33 are COMPLETE. R34-A is COMPLETE. R34-B is IN REVIEW after this
PR; R34-C is NOT STARTED; R34 is NOT COMPLETE; R35–R37 are NOT STARTED. This PR
remains unmerged and undeployed until human review.
