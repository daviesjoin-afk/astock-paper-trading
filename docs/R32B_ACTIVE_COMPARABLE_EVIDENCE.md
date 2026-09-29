# R32-B Active Comparable Evidence

Status: **IN PROGRESS**. R32-A is complete. R32-C remains **NOT STARTED** until this stage is reviewed and merged.

R32-B closes the Active evidence path needed before a Shadow comparison can begin. It records whether a full comparable context was captured and gives an explicit reason when it was not. It does not change signal, entry, risk, or execution decisions.

## Production path inventory

| Path | Evidence capture and owner | Context use and persistence | Unavailable behavior |
|---|---|---|---|
| Signal generation (`paper_trading.generate_signals`, `_bootstrap_signals_for_today`) | Signal evidence is captured by `signal_evidence_for`; the signal pipeline does not consume the Tradability Archive. The signal is stamped with the cycle-pinned strategy version by `strategy_selection_resolver`. | `paper_decision_audit.build_decision_snapshot` persists the envelope into the signal payload. A full comparable context is unavailable because canonical `EntryGateState` and its policy identity do not exist at signal approval time. | `UNAVAILABLE / missing_entry_state`; no later owner lookup upgrades this historical decision. |
| Entry evaluation (`execution_planner.plan_entry` → `evaluate_entry_state`) | The module exposes an explicit state/evaluator contract, but repository search found no production caller of `plan_entry`. Formal signal gates remain in `paper_trading._signal_approval`; they do not produce the canonical `EntryGateState` plus policy identity required by R32-A. | Standalone evaluator validates a supplied state and policy fingerprint. Signal audit snapshots currently persist no runtime context. | Production signal decisions record `UNAVAILABLE / missing_entry_state`; no context is synthesized from the decision payload. |
| Order planning | Strategy intent is persisted with its exact cycle strategy stamp. Planning does not claim a comparable execution context. | The persisted request is later combined with execution evidence; no latest strategy fallback is used for cycle-bound orders. | Missing or partial exact strategy stamp remains unavailable. |
| Execution (`execution_context_from_facts`) | The adapter calls `tradability_archive.tradability_at` once at the quote's explicit `execution_asof`, using the current SQLite connection. The returned fact and fingerprint are passed to both context construction and `evaluate_simulated_execution`. | `ComparableRuntimeContext` binds the exact cycle/version/checksum, quote, market snapshot, Tradability Archive fingerprint, ruleset, risk identity and frozen execution state. `_decision_evidence` persists that projection and fingerprint. | Missing owner evidence remains `UNAVAILABLE / missing_tradability_evidence`. The execution result still follows the existing fail-closed tradability decision. No retry with latest/current facts occurs. |
| Risk application | Existing risk authority evaluates the frozen state and writes through the existing risk/audit path. It does not create a separate tradability authority. | Risk output remains part of the decision audit; a comparable context is included only when the exact context was supplied. | Missing context remains explicitly unavailable. |
| Decision audit | `paper_decision_audit` is the sole snapshot serializer; paper-trading wrappers only inject dependencies. | Supplied context projection, fingerprint, availability and stable unavailability reason are stored together. | Historical snapshots are never reconstructed from a later database row. |

## Availability contract

`simulation_runtime_context.ActiveRuntimeContextResult` has two states:

- `AVAILABLE` carries one immutable `ComparableRuntimeContext` and no reason code.
- `UNAVAILABLE` carries no context and one stable reason code.

Current reason codes are `missing_strategy_identity`, `missing_market_snapshot`, `missing_quote_identity`, `missing_tradability_evidence`, `missing_execution_state`, `missing_entry_state`, `missing_policy_identity`, `strategy_cycle_identity_mismatch`, and `invalid_runtime_context_inputs`.

The execution adapter captures Tradability Archive evidence once. It does not copy the archive's listing, ST, suspension, quote, price-limit, age, or tradability rules. The existing `tradability_archive.tradability_at` remains the sole owner for that decision.

## Authority and side-effect inventory

- Tradability authorities added: **0**; the existing archive authority remains **1**.
- Business authorities added or removed: **0**.
- Duplicate implementations removed: **0**.
- New facade/wrapper: **0**; existing compatibility facades still delegate to their owners.
- Implicit current/latest lookup added: **0**.
- Direct database write sites added: **0**.
- Trade, risk, entry, execution, fee, lot, T+1, price-limit, and allocation rules changed: **NO**.
- R32-C readiness: **NO** until this PR's exact-head verification and human review are complete.
