# R31 · Strategy Lifecycle & Promotion

## Authorities

| Owner | Owns | Does not own |
| --- | --- | --- |
| `strategy_registry` | Stable strategy identity, immutable `StrategyVersion`, current version head, clone provenance | Lifecycle state or promotion decisions |
| `strategy_lifecycle` | Exact-version state, legal transition table, compare-and-swap (CAS) writer, append-only state history, formal-cycle predicate | R29/R30 evidence or performance interpretation |
| `strategy_promotion` | Immutable proposal ledger, exact evidence reads, versioned evidence-sufficiency rules, decision fingerprint | Lifecycle state writes, latest-run selection, AI/provider calls |

Lifecycle identity is `strategy_id + strategy_version + strategy_checksum`. A new immutable version always starts in `draft`; it never inherits a prior version's validation or runtime state. Existing cycles keep the exact version they pinned.

## Canonical state vocabulary

Persisted and API states use lowercase snake_case:

`draft`, `candidate`, `research`, `validated`, `shadow`, `paper`, `production_sim`, `degraded`, `paused`, `retiring`, `archived`, `rejected`, `validation_failed`, `quarantined`.

`strategy_lifecycle.TRANSITION_TABLE` is the sole legal-edge table. `archived`, `rejected`, and `validation_failed` are terminal. A quarantined strategy has no release path in this version; an attempted return fails with `quarantine_release_evidence_missing`.

Only `paper` and `production_sim` satisfy `strategy_lifecycle.allows_formal_cycle()`. `shadow`, `degraded`, `paused`, and all other states are excluded from new formal cycles. The eligibility value exposed to UI is derived from this owner; the legacy `supports_new_cycle` database column is not consulted.

## State changes and proposals

Lifecycle writes bind an exact immutable version and checksum, expected state, actor, and target. Promotion remains current-head-only. Safety transitions can also target an older exact version while a live cycle still pins that version; the owner verifies its checksum and live cycle binding inside the write transaction. Arbitrary unpinned historical versions cannot be changed. State and event writes use one immediate SQLite transaction plus compare-and-swap. A conflict writes no event. Lifecycle events and promotion proposals have database-level append-only guards.

Promotion rules are evidence checks, not return or win-rate thresholds:

| Transition | Evidence policy |
| --- | --- |
| `draft → candidate` | Exact current version plus compiled DSL/runtime readiness |
| `candidate → research` | Exact immutable current version; no performance result required |
| `research → validated` | Explicit exact R29 run; `ready`, completed result, runner and owner identity verified |
| `validated → shadow` | Explicit exact R29 run plus exact R30 report bound to that R29 baseline and strategy version; failed/unavailable cases block |
| `shadow → paper` | Blocked until a canonical R32 shadow evidence owner exists |
| `paper → production_sim` | Blocked until canonical downstream paper/runtime evidence exists |

No latest-run/report fallback exists. Apply re-reads the selected immutable R29/R30 rows and recomputes the decision. R29/R30 live in the separate adaptive evidence database; R31 reads their immutable exact rows and writes only to the paper database, without attaching databases or attempting a cross-database transaction.

AI may create a proposal with exact references and rationale. AI cannot apply any lifecycle transition. Human/system safety intent can pause, quarantine, retire, archive, or reject only along legal edges and with a reason.

`paused → paper` and `paused → production_sim` are explicit human-only resume operations, not promotions. A pause from either formal state records that exact resume target in its immutable event; resume must match it, name a reason, and audit the pause event it resumes. A pause from a non-formal state does not grant a route to `paper` or `production_sim`, so resume cannot bypass promotion evidence. Legacy rows that only say `paused` have no source mode; the fixed recovery policy maps them to `paper` and records that decision in the resume event. Cycle ownership, capital, lots, and exact version pins are independent of pause/resume; lifecycle only controls execution permission.

## Legacy migration

The first lifecycle-owner schema initialization imports the existing current state as one `legacy_import` event with `migration_source=legacy_strategy_registry`:

| Legacy value | Canonical value |
| --- | --- |
| `draft` | `draft` |
| `validated` | `validated` |
| `active` | `paper` |
| `paused` | `paused` |
| `retiring` | `retiring` |
| `archived` | `archived` |

Migration does not invent `candidate`, `research`, or `shadow` history. Existing `strategy_definition_events` for retained definitions remain available for audit but have no new writers. Deleting an unused scratch definition removes its legacy event rows to satisfy the restrictive foreign key; its canonical lifecycle state/events remain as a permanent identity tombstone. Legacy lifecycle columns remain because the current SQLite table is retained without a risky rebuild. They are non-authoritative and never read by production decisions. New identity-row creation writes only the required `draft` / `0` placeholders; the migration imports old values once, while lifecycle changes never write those columns. All state changes and reads use `strategy_lifecycle`.

Upgrade also checks exact versions pinned by cycles in `draft`, `running`, or `paused` status. For a missing pinned-version row, it imports the legacy strategy status as a one-time migration event for that exact immutable version; it never copies the newer head's lifecycle state. The migration runs only when the cycle, pin, and version tables expose all identity and status columns needed to prove the binding. A compatibility schema without those columns cannot claim a live pin, and safety operations on a non-head version fail closed. Repeated initialization is idempotent.

## API contract

| Route | Contract |
| --- | --- |
| `GET /api/strategies/{id}` | Strategy identity/current exact version and derived state |
| `GET /api/strategies/{id}/lifecycle` | Current state, version/checksum, history, legal/eligible/blocked edges, reasons, evidence requirements, proposals, `formal_cycle_allowed` |
| `GET /api/strategies/{id}/events` | Canonical lifecycle events across exact versions |
| `POST /api/strategies/{id}/promotion/proposals` | Append proposal with exact version/checksum/state/target, explicit evidence references, proposer and rationale |
| `GET /api/strategies/{id}/promotion/proposals` | Bounded proposal list, optionally scoped to one version |
| `POST /api/strategies/{id}/transition` | Requires version, checksum, expected state, target and actor. Promotion requires `proposal_fingerprint`; safety intent and explicit resume require `reason_code` and `reason`. Resume is human-only and must match the recorded pause origin. `actor_type=ai` is rejected. |

The API delegates all transition and eligibility decisions to the two owners. The UI renders backend-provided legal/eligible states and evidence decisions; it does not calculate promotion rules.

## Legacy caller audit

| Caller | Reads legacy state? | Reads canonical state? | Writes state? | Reads compatibility flag? | Used `SR.transition`? | R31 action |
| --- | --- | --- | --- | --- | --- | --- |
| `strategy_registry` | Migration/bootstrap only | Yes | No state changes; new identities get required legacy placeholders | No physical read; compatibility value derived | No | Owns identity/version only; legacy transition/archive writers removed |
| `strategy_service` | No | Through registry/lifecycle | Orchestrates owner call | Returns derived response alias only | Before: yes | Revalidates proposal then delegates to both owners |
| `api_strategies` / models | No | Through service | No direct writes | Response compatibility alias only | Before: indirectly | Exact version and proposal request contract |
| Paper runtime / cycle selection | No | Current or pinned exact lifecycle owner state | No lifecycle writes | No physical read; derived compatibility projection only | Before: no | Formal-cycle eligibility comes from lifecycle owner |
| Strategy list/detail | No | Yes | No | Derived API field only | Before: no | Canonical lifecycle read model and event history |
| Hard-delete guard | No | Yes, including exact-version history | No lifecycle writes | No | Before: no | `never_left_draft` uses canonical event history; scratch deletion removes legacy FK events but retains canonical state/events as an identity tombstone, so the id cannot be reused |
| Frontend strategy admin | No | Backend read model only | Sends proposal/transition requests | Renders derived field only | Before: transition endpoint | Renders backend state, history, evidence, and decisions; no business rules |
| `promotion_science.py` | No | No R31 lifecycle read | No R31 state writes | No | No | Retained for existing challenger science; not the generic R31 policy owner |

Legacy writer caller audit against PR #214 base (`24002778bfeb707ab2807f7ae08b7c29756f459f`): `SR.transition` production callers 2 → 0; registry `_TRANSITIONS` authority 1 → 0; `strategy_definition_events` writers 2 → 0; lifecycle status updates 1 → 0; supports-new-cycle physical decision readers 1 → 0. Two required initial-row inserts still set legacy placeholders (`draft` and `0`) because those columns remain `NOT NULL`; they do not change or determine canonical state. `strategy_lifecycle` is the only state writer, and promotion proposals are written only by `strategy_promotion`.

`strategy_registry.transition`, `_TRANSITIONS`, `archive_definition`, and the old `wbValidateAndMark` writer are removed. `strategy_definition_events` has no production insert/update/delete caller after bootstrap. `promotion_science.py` remains challenger-specific; it is not the R31 policy owner.

## Scope boundary

R28, R29, R30 and R31 are complete after merge/review. R32 shadow/challenger runtime and R33 health monitoring/automatic retirement remain **NOT STARTED**. This change adds no broker or real-trading path and does not deploy.
