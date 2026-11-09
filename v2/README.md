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
- Immutable offline quote evidence, explicitly referenced by contract v4;
  decisions pin both evidence availability and missing-evidence rejections.
  See [the evidence contract](docs/QUOTE_EVIDENCE.md).
- Versioned, content-addressed research: a reviewed strategy artifact pins the
  algorithm source digest, canonical parameters, the frozen research input digest
  and the runtime contract. Two original offline strategies regenerate their
  research signals from one frozen input; the kernel verifies the stored decisions
  and the reviewed research output independently of deterministic replay.
  See [the reproducibility ADR](docs/ADR-003-REPRODUCIBLE-RESEARCH.md).
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
v2/research/worker.py — offline kernel fixture proposal (Python stdlib)
v2/research/run_research.py — reviewed strategy + frozen input -> research output
v2/research/research_contract.py — canonical JSON, SHA-256 and content identity
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
export ASTOCK_V2_DB=./v2-market-packs.sqlite3
cargo run --locked
```

In another terminal, from repository root:

```bash
# Creates experiments A/B, emits Python signals, runs Rust decisions, reads and replays each ledger.
python3 v2/scripts/market_pack_demo.py
curl -s 'http://127.0.0.1:8765/v2/portfolio?cycle_id=pack-experiment-A'
curl -s 'http://127.0.0.1:8765/v2/replay?cycle_id=pack-experiment-B'
# After restarting the Rust service against the same file:
python3 v2/scripts/market_pack_demo.py --verify-only
python3 -m unittest discover -s v2/research -p 'test_*.py' -v
```

For the web UI:

```bash
cd v2/web
npm ci
npm run dev                 # http://127.0.0.1:5174
npm run build               # type-check + production build
npx playwright install chromium
npx playwright test         # real browser: selection, race, failures, XSS, axe
```

Open http://127.0.0.1:5174/?cycle_id=pack-experiment-A (or pack-experiment-B). The dev server proxies `/v2` to the Rust API.
The read-only V2 UI never touches a V1 endpoint.

## State and data boundaries

- Default data file is `v2/v2-market-packs.sqlite3` if run from `v2/`.
  Set `ASTOCK_V2_DB` to select an isolated path, **never** a V1 path.
- Experiments must be explicitly created with positive initial cash through
  POST /v2/experiments. There is no global cash setting or automatic experiment.
  The old ASTOCK_V2_INITIAL_CASH_CENTS environment variable explicitly fails startup.
- SQLite schema 8 adds the immutable frozen-research-input table on top of the
  schema 7 per-pack context and evidence identity. Schema 7 and earlier files are
  refused before writes; keep those files with the original binary. There is no
  automatic migration. Subsequent reviewed pack or research registrations add
  rows without changing trade tables.
  See [ADR 002](docs/ADR-002-MARKET-DATA-PACKS.md) and
  [ADR 003](docs/ADR-003-REPRODUCIBLE-RESEARCH.md).
- Idempotency identity is
  `(cycle_id, strategy_version_id, signal_id)`; same payload returns the
  original result, changed payload fails 409, without a second fill.
- Money and quantities are integers. Store and replay make decisions against
  recorded inputs, not current time or later quotes.
- cycle_id is the unique independently funded experiment identity. Cash, positions,
  T+1, monotonic decision time, decision counts, historical views and replay are
  scoped to it. Identical evidence can be used by independent counterfactual experiments.
- Creation pins a reviewed StrategyArtifact (strategy version, algorithm source
  digest, canonical parameters, frozen research input identity and digest),
  either cn-offline-fixture-pack-1 or cn-offline-fixture-pack-2, plus
  v2-paper-policy-6. Unknown rules/packs fail explicitly. The two pack manifests,
  sources, coverage and snapshot SHA256 summaries live in data/market-pack-*.json.
  GET /v2/market-data-packs/{pack_id} reads the installed fixed inputs; there is
  no HTTP pack registration or upload route.
  Same experiment ID with different parameters or strategy ID with different research
  input returns 409. GET /v2/experiments/{cycle_id} reads these references.
- GET /v2/experiments is a read-only catalogue for the dashboard picker: stored
  cycle id, status, strategy and data-pack identity, initial cash and decision
  count. It clamps the limit, skips unreadable rows instead of failing the whole
  list, and never writes.
- POST /v2/experiments/{cycle_id}/complete verifies the ledger then freezes new
  submissions. Exact retries remain idempotent; it cannot be reactivated.
- GET /v2/experiments/{cycle_id}/research-reproduction proves the stored decisions
  are exactly the reviewed research signals for the pinned artifact and frozen
  input. It describes research identity only; fills, prices, fees and cash remain
  replay's subject. Replay success does not imply research reproduction.
- Portfolio, simulations GET and replay require ?cycle_id=...; no whole-store fallback.
- Transactional writes are owned by Rust. The Python worker emits a proposal,
  not an order quantity, not a fill, not an activation decision.
- Contract v4 rejects inline `quote` and requires explicit quote_ref (pack ID + source + quote ID).
  QuoteImport schema 2 and QuoteEvidence schema 5 carry the pack ID; nested
  execution evidence schema 2 must match it. Cross-pack references are rejected.
  Evidence is ingested separately using `POST /v2/quotes`. An identical retry
  succeeds; a changed payload under the same evidence identity returns 409.
  Missing evidence or availability after the decision produces a saved rejection.
- Policy `v2-paper-policy-6` / SQLite schema version 7 requires a **fresh V2 database**. Existing older
  databases are refused before schema additions; no automatic migration or
  reinterpretation of earlier fills is provided. Keep old files separately.
- Once recorded, decisions are replayed in ledger insertion order. Contract v4
  does not yet support historical out-of-order correction or cancellation:
  consumers must submit decision times monotonically within each experiment.

## Current extension boundary

Experiments independently select one of two explicitly reviewed offline_fixture
contexts. Pack 1 retains the original 2026-10-27 through 2026-11-05 identity
coverage. Pack 2 narrows the test identity coverage to 2026-10-30 through 2026-11-05
and uses a distinct snapshot version. This restriction is synthetic, not a newly
certified security listing/tradability history.

The immutable manifest and stored snapshots are compared with the exact reviewed
definition for their own pack ID. Old definitions must remain supported to open
and replay old experiments; unsupported/missing/altered definitions fail explicitly.
No current/latest pack substitution is used. SHA256 manifest/file consistency is
verified by the Python contract CI; Rust additionally compares the entire persisted
typed manifest and snapshots to the selected reviewed definition, avoiding a new
crypto dependency.

The summary hashes cover fixed snapshot bytes. Offline quote evidence can be
appended to a pack namespace, but an existing identity cannot change; each
experiment pins the exact evidence used by every decision, including missing
evidence. Completing A before registering B or importing B evidence cannot change
A's replay. This is not arbitrary data-package upload or provider authentication.

Verifiable StrategyArtifact content and experiment comparison are separate future
work. Paper-account capital allocation and governed evolution exist as explicit,
audited mechanisms (ADR 005-017); there is no dynamic plugin, hidden scheduler,
live trading or news learning.

## Capability map (2026-11)

| Area | What exists | Entry point |
| --- | --- | --- |
| Paper account | one user-funded account, two strategy slots, reserve cash, risk limits | `POST /v2/paper-accounts` |
| Deterministic clock | created/running/paused/stopped/completed sessions, batch run, per-day and account-level advance | `/v2/paper-sessions/...` |
| Trust and audit | 8 invariant checks incl. fill/order equality, frozen signals, rebalance chain | `GET /v2/paper-accounts/{id}/audit` |
| Valuation | reviewed price-set registry, single-point valuation, equity series, drawdown | `GET /v2/paper-accounts/{id}/valuation` |
| Performance | profit, return bps, drawdown, fees, trades for accounts and experiments | `/performance` |
| Dynamic capital | proposal with bounds, audited rebalance, cost-basis exposure | `/allocation-proposal`, `/rebalances`, `/exposure` |
| Evolution | candidate generation, parameterized artifacts, shadow runs, signal diff, rolling windows, governed decisions, event log (filters, export, offline verify), strategy-version switching | ADR-012-018 |
| Operations | health with schema/price-set counts, single-writer concurrency proof, runbook, cron helpers, backup/restore, archive verification (single pair or directory, manifest sidecar), one-command verification with JSON report | `docs/OPERATIONS.md` |

## ADR index

| ADR | Subject |
| --- | --- |
| 001 | experiments and market-pack identity |
| 002 | reviewed market data packs |
| 003 | reproducible research |
| 004 | A/B comparison boundaries |
| 005 | paper account, slots, orders, sessions, risk limits, audit, rebalances |
| 006 | valuation contract and reviewed price sets |
| 007 | equity series and drawdown |
| 008 | allocation proposal |
| 009 | rebalance ledger event |
| 010 | cost-basis exposure |
| 011 | account-level day advance |
| 012 | evolution governance, events, candidate generation |
| 013 | parameterized strategies and candidate artifacts |
| 014 | shadow artifact experiment boundary |
| 015 | signal-level diff |
| 016 | performance metrics |
| 017 | rolling-window validation |
| 018 | explicit strategy-version switching (implemented) |

## Command cheat sheet

```bash
# from v2/
bash scripts/verify_all.sh            # everything, one summary
cargo +1.99.1-x86_64-pc-windows-gnu test --locked
bash scripts/smoke.sh
python -m unittest discover -s research -p 'test_*.py'
cd web && npx playwright test

# scheduler helpers (explicit days, never machine time)
bash scripts/daily_advance.sh --account paper-001 --day 2026-11-09 --dry-run
python scripts/weekly_review.py --base-cycle base --candidate-cycle cand   --windows "2026-10-30;2026-11-02"

# reviewed price sets
python research/make_price_set.py --input prices.csv --out data/price-sets/x.json ...
```

## Verification entry points

| Command | Covers |
| --- | --- |
| `bash scripts/verify_all.sh` | rust, research, smoke, browser, leak scan |
| `bash scripts/verify_all.sh --archive DIR --report out.json` | adds archive verification, writes a JSON summary |
| `bash scripts/smoke.sh` | HTTP, ledger, replays, scheduler scripts, publishing, evolution, archive |
| `python scripts/verify_archive.py --dir DIR` | ledger/export/sidecar consistency for a backup directory |

## Known limits

- **No reviewed price data is bundled.** Every valuation/performance/proposal path
  fails closed with `no_reviewed_price_set` until audited data is added.
- No annualized/Sharpe metrics, no automatic promotion execution, no daemon:
  scheduling is the caller's job (cron/task system).
- Schema upgrades require a fresh database (current schema v15); old local files are rejected by design.
- All trading is paper-only; there is no broker integration, and the 8600
  deployment claim remains unverified.

`V1` continues to run independently and must not be reconfigured, migrated,
or connected to the new V2 database by this PR.
