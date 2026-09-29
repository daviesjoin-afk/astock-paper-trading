# R32-A Comparable Runtime Context

Status: **R32-A COMPLETE**. R31 is **COMPLETE**; R32-B is **IN PROGRESS**; R32-C through R32-E and R33 are **NOT STARTED**.

R32-A gives a simulation run a deterministic identity over owner-issued facts and explicit mutable-state inputs. It does not create market, tradability, risk, or execution authority. It adds one contract that records which exact inputs a run consumed.

## Market fact identity

`market_data_contract.snapshot_fingerprint()` produces a SHA-256 identity from snapshot kind, source, as-of and observation times, completeness, expected row count, verification semantics, `degraded_reason`, and canonical rows. Rows are treated as a bag: input order does not matter, while duplicate rows remain significant. Mapping key order is canonicalized. `saved_at` and policy-derived reading fields such as freshness ratios do not relabel the underlying fact. Symbol quote snapshots use the same function.

Market fact identity and reading classification are separate. The runtime context also records the named market policy, so equal facts classified under different policies do not compare as the same run.

## Tradability and risk identity

Tradability identity reuses `tradability_archive.evidence_fingerprint()` and the fingerprint exposed by `tradability_at()`. R32-A does not hash a second tradability representation. Risk identity references the cycle-pinned `StrategyRiskFingerprint` and compiled risk profile returned by `strategy_runtime`; it does not introduce another risk hash algorithm.

## Runtime context

`simulation_runtime_context.ComparableRuntimeContext` is an immutable, I/O-free contract. Its semantic identity includes exact strategy id, version, checksum, session and decision instant, market policy and fact identities, tradability fingerprints, execution ruleset version, risk identity, and the fingerprint for the explicit state consumed by that decision. It accepts execution-state and entry-gate-state fingerprints issued by `execution_planner`; it only validates and combines these identities, and does not reinterpret their fields. The context fingerprint is canonical JSON plus SHA-256. It contains no database row id, request id, creation time, or wall clock.

## State injection and shared decisions

`execution_state_fingerprint()` canonically covers buying power, sellable quantity, already-filled and same-session consumed quantities, order status, lot size, and participation rate. When a runtime context is supplied, `execution_context_from_state()` verifies that its execution-state fingerprint matches the exact state passed to the builder; it also checks the ruleset, session date, and decision instant against the quote. It requires the canonical execution quote policy, fingerprints the actual quote and market snapshot with `market_data_contract`, and requires the supplied tradability evidence fingerprint to match the context's symbol/session entry. Market readings are checked against the canonical classification for that snapshot, policy, session, and decision instant before use. A complete strategy stamp on the persisted order must match the context's exact strategy id, version, and checksum; a partial stamp fails closed. Available liquidity is derived inside this pure builder from the supplied canonical quote's amount and price; callers cannot pass a second, unbound value. The formal paper adapter captures its ledger state, fingerprints that same state, and then calls this builder. An isolated caller can supply the same values without mutating the formal ledger. Both paths feed the unchanged `evaluate_simulated_execution()` authority, which still owns T+1, price limits, suspension, liquidity, slippage, fees, partial fills, and session rules.

Entry checks follow the same pattern: the formal `plan_entry()` adapter captures cash, reservations, capacity, account risk, market state, and the market-gate option into immutable `EntryGateState`, then delegates to `evaluate_entry_state()`. `entry_gate_state_fingerprint()` canonicalizes its sets and nested mappings so their input order does not change identity, while any decision-relevant cash, risk, capacity, reserve, market, allocation, or gate-option change does. Entry contexts also carry `entry_policy_fingerprint`, derived from the existing `policy_for(account_id)` owner. The evaluator checks that the context strategy id is the formal account/strategy id, that the exact current `ExecutionPolicy` projection matches, and that the quote's symbol matches the requested code before using the quote fingerprint. An execution-only context cannot stand in for entry-state or policy identity. Persisted request fields such as account, symbol, side, amount, and fees remain part of the independent intent/request contract, not the state fingerprint. Risk decision rules remain in their existing pure domain modules.

## Active evidence and legacy records

The exact decision evidence is the persisted intent identity paired with `ComparableRuntimeContext`: the persisted order identifies what was requested, and the context identifies the owner facts and explicit mutable state used to decide it. The context fingerprint alone does not claim to represent the persisted intent. New decision audit envelopes persist an explicitly supplied runtime context or `UNAVAILABLE` with a stable reason code. New execution evidence persists the exact context when all cycle-pinned strategy, market, tradability, and execution-state identities are available. If an input cannot be proven, evidence has a null fingerprint and cannot be upgraded by a later lookup. Historical Active rows are not backfilled or reconstructed from current data; they remain unavailable for comparison.

R32-A does not add a Shadow scheduler, account, portfolio, order ledger, comparison UI, lifecycle transition, or promotion path. R32-B closes Active evidence coverage before any Shadow runner is added.
