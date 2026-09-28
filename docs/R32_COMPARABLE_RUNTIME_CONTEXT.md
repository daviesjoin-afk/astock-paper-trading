# R32-A Comparable Runtime Context

Status: **R32-A IN REVIEW**. R31 is **COMPLETE**; R32-B and R33 are **NOT STARTED**.

R32-A gives a simulation run a deterministic identity over owner-issued facts and explicit mutable-state inputs. It does not create market, tradability, risk, or execution authority. It adds one contract that records which exact inputs a run consumed.

## Market fact identity

`market_data_contract.snapshot_fingerprint()` produces a SHA-256 identity from snapshot kind, source, as-of and observation times, completeness, expected row count, verification semantics, and canonical rows. Rows are treated as a bag: input order does not matter, while duplicate rows remain significant. Mapping key order is canonicalized. `saved_at` and policy-derived reading fields such as freshness ratios do not relabel the underlying fact. Symbol quote snapshots use the same function.

Market fact identity and reading classification are separate. The runtime context also records the named market policy, so equal facts classified under different policies do not compare as the same run.

## Tradability and risk identity

Tradability identity reuses `tradability_archive.evidence_fingerprint()` and the fingerprint exposed by `tradability_at()`. R32-A does not hash a second tradability representation. Risk identity references the cycle-pinned `StrategyRiskFingerprint` and compiled risk profile returned by `strategy_runtime`; it does not introduce another risk hash algorithm.

## Runtime context

`simulation_runtime_context.ComparableRuntimeContext` is an immutable, I/O-free contract. Its semantic identity includes exact strategy id, version, checksum, session and decision instant, market policy and fact identities, tradability fingerprints, execution ruleset version, and risk identity. The context fingerprint is canonical JSON plus SHA-256. It contains no database row id, request id, creation time, or wall clock.

## State injection and shared decisions

`execution_context_from_state()` converts facts plus an `ExecutionStateSnapshot` into the existing `ExecutionContext`. The formal paper adapter reads its ledger facts and then calls this builder. An isolated caller can supply the same values without mutating the formal ledger. Both paths feed the unchanged `evaluate_simulated_execution()` authority, which still owns T+1, price limits, suspension, liquidity, slippage, fees, partial fills, and session rules.

Entry checks follow the same pattern: the formal `plan_entry()` adapter captures cash, reservations, capacity, account risk, and market state into `EntryGateState`, then delegates to `evaluate_entry_state()`. Risk decision rules remain in their existing pure domain modules.

## Active evidence and legacy records

New decision audit envelopes can persist a supplied runtime context and its fingerprint. New execution evidence persists the exact context when all cycle-pinned strategy, market, and tradability identities are available. If an input cannot be proven, evidence records `active_runtime_context_unavailable` and a null fingerprint. Historical Active rows are not backfilled or reconstructed from current data; they remain unavailable for comparison.

R32-A does not add a Shadow scheduler, account, portfolio, order ledger, comparison UI, lifecycle transition, or promotion path. R32-B remains **NOT STARTED** pending review of this contract and its readiness gates.
