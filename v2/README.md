# AStock V2 — Independent paper-only kernel (MVP)

V2 is **a new system**, not an extraction or translation of V1. No V1 Python
module, schema, task, broker or runtime database is imported or reused.

## What exists today

- Rust 2024 pure decision engine: explicitly pinned cycle, strategy version,
  signal identity, timestamp and immutable quote input.
- Fixed offline main-board A-share subset: explicit identity and dated quantity
  rules (100-share minimum/step, 1,000,000 maximum), no same-day sales (T+1),
  simple freshness and session checks, conservative limit-up buy and limit-down
  sell rejection, fixed illustrative commission and stamp duty.
- A single-writer SQLite V2 event store: idempotent signal identity, immediate
  transactions, saved rejected decisions, stored fills, state projection,
  exact deterministic replay against the pinned recorded inputs.
- Local-only Rust HTTP API, Python standard-library offline research signal
  producer, TypeScript/Vite read-only dashboard.
- Separate V2 CI pipeline; no V1 test suite dependency.
- Immutable offline quote evidence, explicitly referenced by contract v3;
  decisions pin both evidence availability and missing-evidence rejections.
  See [the evidence contract](docs/QUOTE_EVIDENCE.md).
- Store records actual receipt separately from the fixture's declared times,
  fixes trust to `offline_fixture`, and leaves provider publication unknown.
  See [source trust and time facts](docs/SOURCE_TRUST.md).
- Pinned 2026 SSE/SZSE scheduled calendar and full-precision continuous-session
  policy; weekends, published holidays, uncovered dates and unsupported auctions
  reject. Store pins complete market snapshots and each run's version references.
  See [calendar and session scope](docs/CALENDAR_SESSIONS.md).
- Explicit offline directional execution price and capacity; missing, stale,
  wrong-direction or insufficient evidence rejects the whole candidate.
  See [execution evidence and limits](docs/EXECUTION_EVIDENCE.md).
- Store-resolved offline instrument identities; missing, conflicting, unsupported
  or uncovered identities/rules reject. Initially only the explicit 600000.SH
  fixture supports execution, for 2026-10-27 through 2026-11-05. STAR/ChiNext,
  ETFs and other unimplemented types cannot inherit main-board parameters.
  See [instrument eligibility and sources](docs/INSTRUMENT_ELIGIBILITY.md).

**This is a deterministic demonstration kernel, not market-ready exchange
simulation.** It does not verify whether a quote truly originates from a market
provider, live emergency market closures, ST/STAR/ChiNext/Beijing price-limit tiers,
intraday liquidity, call auctions, partial fills, slippage or broker custody.
It accepts separately ingested `offline-fixture` evidence. A reference price alone
cannot fill; a complete candidate must fit an explicit directional price level.
That offline level is an assumption, not a verified order book or fill guarantee.
The fixed fee model is deliberately
illustrative rather than a regulatory claim. It must not be used to estimate
investment returns or to trade real money.

## Architecture

V2 module boundaries: [docs/MODULE_BOUNDARIES.md](docs/MODULE_BOUNDARIES.md).
Coding-agent conventions: [AGENTS.md](AGENTS.md).

```text
v2/web/  TypeScript + Vite — localhost read-only UI
      |
      v
v2/src/main.rs — Rust localhost-only API (127.0.0.1:8765)
      |
      v
v2/src/domain/ — typed contract, pure execution policy, T+1 portfolio rules
v2/src/store/  — sole SQLite writer, read models and deterministic replay
      ^
      | explicit versioned HTTP POST only; never access the ledger directly
      |
v2/research/worker.py — offline research proposal (Python stdlib)
```

There are no live-market, broker, autonomous activation, scheduler, or live
portfolio endpoints. The trading path is isolated from V1 and from the
research worker.

## Quick start

Prerequisites: Rust **1.99.1** (pinned by `rust-toolchain.toml`), Python >= 3.11,
Node.js >= 20. CI uses Node 22 and Python 3.13. See [build reproducibility](docs/BUILD_REPRODUCIBILITY.md).

From repository root:

```bash
cd v2
cargo test --locked
export ASTOCK_V2_DB=./v2-experiments.sqlite3
cargo run --locked
```

In another terminal, from repository root:

```bash
# Creates experiments A/B, emits Python signals, runs Rust decisions, reads and replays each ledger.
python3 v2/scripts/experiment_demo.py
curl -s 'http://127.0.0.1:8765/v2/portfolio?cycle_id=experiment-A'
curl -s 'http://127.0.0.1:8765/v2/replay?cycle_id=experiment-B'
# After restarting the Rust service against the same file:
python3 v2/scripts/experiment_demo.py --verify-only
python3 -m unittest discover -s v2/research -p 'test_*.py' -v
```

For the web UI:

```bash
cd v2/web
npm ci
npm run dev
```

Open http://127.0.0.1:5174/?cycle_id=experiment-A (or experiment-B). The dev server proxies `/v2` to the Rust API.
The read-only V2 UI never touches a V1 endpoint.

## State and data boundaries

- Default data file is `v2/v2-experiments.sqlite3` if run from `v2/`.
  Set `ASTOCK_V2_DB` to select an isolated path, **never** a V1 path.
- Experiments must be explicitly created with positive initial cash through
  POST /v2/experiments. There is no global cash setting or automatic experiment.
  The old ASTOCK_V2_INITIAL_CASH_CENTS environment variable explicitly fails startup.
- SQLite schema 6 adds experiment ownership. Earlier database files are refused
  before writes; this one-time ownership change does not imply future data updates
  require new schemas. See [ADR 001](docs/ADR-001-EXPERIMENT-OWNERSHIP.md).
- Idempotency identity is
  `(cycle_id, strategy_version_id, signal_id)`; same payload returns the
  original result, changed payload fails 409, without a second fill.
- Money and quantities are integers. Store and replay make decisions against
  recorded inputs, not current time or later quotes.
- cycle_id is the unique independently funded experiment identity. Cash, positions,
  T+1, monotonic decision time, decision counts, historical views and replay are
  scoped to it. Identical evidence can be used by independent counterfactual experiments.
- Creation pins a StrategyArtifact (strategy version + immutable research input ID),
  cn-offline-fixture-pack-1 and v2-paper-policy-6. Unknown rules/packs fail explicitly.
  Same experiment ID with different parameters or strategy ID with different research
  input returns 409. GET /v2/experiments/{cycle_id} reads these references.
- POST /v2/experiments/{cycle_id}/complete verifies the ledger then freezes new
  submissions. Exact retries remain idempotent; it cannot be reactivated.
- Portfolio, simulations GET and replay require ?cycle_id=...; no whole-store fallback.
- Transactional writes are owned by Rust. The Python worker emits a proposal,
  not an order quantity, not a fill, not an activation decision.
- Contract v3 retains inline `quote` with `quote_ref` (source + quote ID).
  Evidence is ingested separately using `POST /v2/quotes`. An identical retry
  succeeds; a changed payload under the same evidence identity returns 409.
  Missing evidence or availability after the decision produces a saved rejection.
- Policy `v2-paper-policy-6` / SQLite schema version 6 requires a **fresh V2 database**. Existing older
  databases are refused before schema additions; no automatic migration or
  reinterpretation of earlier fills is provided. Keep old files separately.
- Once recorded, decisions are replayed in ledger insertion order. Contract v3
  does not yet support historical out-of-order correction or cancellation:
  consumers must submit decision times monotonically within each experiment.

## Current extension boundary

The implemented chain is independent offline experiments. The single reviewed fixture
pack keeps the prior exact-content checks for its snapshots; arbitrary pack ingestion
and multiple simultaneously installed packs are not implemented. ADR 001 separates
the future pack registry, execution policy dispatch and SQLite schema ownership.
New securities/data should add verified versions, not force structural migrations.

The next step is explicit verified pack registration and immutable research inputs,
not V1 scheduling, strategy evolution, news learning or compatibility interfaces.

`V1` continues to run independently and must not be reconfigured, migrated,
or connected to the new V2 database by this PR.
