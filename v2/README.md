# AStock V2 — Independent paper-only kernel (MVP)

V2 is **a new system**, not an extraction or translation of V1. No V1 Python
module, schema, task, broker or runtime database is imported or reused.

## What exists today

- Rust 2024 pure decision engine: explicitly pinned cycle, strategy version,
  signal identity, timestamp and immutable quote input.
- A-share *MVP* paper rules: 100-share board lots, no same-day sales (T+1),
  simple freshness and session checks, conservative limit-up buy and limit-down
  sell rejection, fixed illustrative commission and stamp duty.
- A single-writer SQLite V2 event store: idempotent signal identity, immediate
  transactions, saved rejected decisions, stored fills, state projection,
  exact deterministic replay against the pinned recorded inputs.
- Local-only Rust HTTP API, Python standard-library offline research signal
  producer, TypeScript/Vite read-only dashboard.
- Separate V2 CI pipeline; no V1 test suite dependency.

**This is a deterministic demonstration kernel, not market-ready exchange
simulation.** It does not verify whether a quote truly originates from a market
provider, exchange holiday calendars, ST/STAR/ChiNext/Beijing price-limit tiers,
intraday liquidity, call auctions, partial fills, slippage or broker custody.
It treats a supplied quote as a fixture and assumes complete fills at that
price once basic conditions hold. The fixed fee model is deliberately
illustrative rather than a regulatory claim. It must not be used to estimate
investment returns or to trade real money.

## Architecture

\`\`\`text
v2/web/  TypeScript + Vite — localhost read-only UI
      |
      v
v2/src/main.rs — Rust localhost-only API (127.0.0.1:8765)
      |
      v
v2/src/domain.rs — pure decision function, explicit quote + time + state
v2/src/store.rs  — only writer, SQLite v2_* ledger, replay verifier
      ^
      | explicit versioned HTTP POST only; never access the ledger directly
      |
v2/research/worker.py — offline research proposal (Python stdlib)
\`\`\`

There are no live-market, broker, autonomous activation, scheduler, or live
portfolio endpoints. The trading path is isolated from V1 and from the
research worker.

## Quick start

Prerequisites: Rust stable >= 1.85, Python >= 3.11, Node.js >= 20.

From repository root:

\`\`\`bash
cd v2
cargo test
cargo run
\`\`\`

In another terminal, from repository root:

\`\`\`bash
python3 v2/research/worker.py --output /tmp/v2-signal.json
python3 v2/research/worker.py --output /tmp/v2-signal.json --send
curl -s http://127.0.0.1:8765/v2/portfolio
curl -s http://127.0.0.1:8765/v2/replay
python3 -m unittest discover -s v2/research -p 'test_*.py' -v
\`\`\`

For the web UI:

\`\`\`bash
cd v2/web
npm install
npm run dev
\`\`\`

Open http://127.0.0.1:5174. The dev server proxies \`/v2\` to the Rust API.
The read-only V2 UI never touches a V1 endpoint.

## State and data boundaries

- Default data file is \`v2/v2-paper.sqlite3\` if run from \`v2/\`.
  Set \`ASTOCK_V2_DB\` to select an isolated path, **never** a V1 path.
- Default seed cash is CNY 100,000 (10,000,000 cents). Override only for a
  *new database* with \`ASTOCK_V2_INITIAL_CASH_CENTS\`.
- Database startup refuses known V1 schema names and existing seed-cash
  / policy mismatches. Back up before migrating schema versions.
- Idempotency identity is
  \`(cycle_id, strategy_version_id, signal_id)\`; same payload returns the
  original result, changed payload fails 409, without a second fill.
- Money and quantities are integers. Store and replay make decisions against
  recorded inputs, not current time or later quotes.
- A cycle in this first MVP is an explicit identity namespace rather than an
  independently funded portfolio. All accepted events share **one**
  demonstration portfolio; multi-account capital and strategy risk allocation
  are not implemented yet.
- Transactional writes are owned by Rust. The Python worker emits a proposal,
  not an order quantity, not a fill, not an activation decision.
- Once recorded, decisions are replayed in ledger insertion order. Contract v1
  does not yet support historical out-of-order correction or cancellation:
  consumers must submit decision times monotonically.

## V2 roadmap (not claimed implemented)

1. **V2-1:** finished minimal kernel; validate CI, real HTTP E2E and coverage.
2. **V2-2:** trusted point-in-time data ingress; exchange calendars, board-
   specific limits, fees, fills, liquidity and price-source attestations.
3. **V2-3:** separate account/cycle ledger, order lifecycle and rigorous
   versioned strategy model plus command authorization.
4. **V2-4:** immutable research dataset + experiment IDs + walk-forward /
   leakage checks; deterministic promotion and explicit strategy approvals.
5. **V2-5:** scenario replay/shadow comparison against V1 snapshots (read-only);
   signed V2 import and standalone repo extraction only after verification.

\`V1\` continues to run independently and must not be reconfigured, migrated,
or connected to the new V2 database by this PR.
