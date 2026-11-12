# AStock V2: instructions for coding agents

This directory is a **new, independent, paper-only system**. Follow these rules
for all changes under `v2/`. Read `docs/REWRITE-SPEC.md` and
`README.md` first.

## Default design

- Keep **one Cargo package and one Rust service**. New modules are allowed only
  for distinct business responsibilities; do not create another crate,
  microservice, message bus, ORM, generic DI framework, or compatibility
  facade merely to reduce file size.
- Code dependency direction: HTTP `main.rs` -> `store/` -> `domain/`.
  The domain must not use networking, storage, filesystem, runtime clock,
  SQLite, axum, tokio, or any V1 modules.
- `domain/account.rs`: one account, two slot books, cash, lots, T+1 and fees.
  `strategy.rs` and `allocation.rs`: deterministic signals and bounded
  cash-budget adjustment. `market.rs`: reviewed close-price set validation.
- `store/mod.rs` exclusively owns durable writes, schema, transactions,
  idempotency and storage policy. A trading day, including any cash transfer,
  is one transaction.
- `data_sources/` fetches and normalizes public bars, quote snapshots, company
  profiles and disclosures. `public_data.py` owns query semantics;
  `source_http.py` owns opt-in raw/query snapshot cache artifacts. It never
  writes the ledger or publishes a reviewed set by itself. Reference data must
  preserve source, fetch time and publication precision; current snapshots
  cannot be silently used as historically known facts.
- `research/make_price_set.py` only builds an artifact from explicit CSV rows.
  TypeScript only presents V2 API read models; it must not replicate paper
  execution rules.
- Do not import, reuse, mutate, auto-migrate or query legacy V1 runtime code,
  databases, Cron jobs or broker credentials.

## Business invariants

- Explicit day, price-set identity, source and as-of timestamp; never silently
  use the wall clock in the core.
- Integer cents and whole-lot quantities; quote freshness, T+1,
  cash budget and market eligibility are fail-closed.
- A reviewed price set is a complete rectangle over its declared symbols and
  dates; sparse input rejects instead of becoming a silent hold.
- Fill, cash transfer, weights, daily equity and the day marker must commit
  atomically. Re-running a settled day returns its stored result.
- Trading remains simulated. No broker endpoints or live-order APIs.
- Old V2 databases are never auto-migrated. Keep the file and start a new
  database path.

## Small-change protocol

1. State the **single business owner**, consumer(s), dependency direction,
   and invariant before introducing a new module.
2. Prefer editing the existing owner. Only split when business ownership,
   change cadence or tested isolation requires it. Never set LOC limits.
3. New dependency? Document its concrete benefit and reject it if stdlib
   or an existing dependency solves the same need.
4. Add at least one **behavioral** test per changed rule and a negative
   control when removing a possible bypass. Avoid tests that only assert
   private function layout or source lines.
5. Run Rust tests/release build, Python research tests, TypeScript build
   and HTTP integration smoke. Preserve or document any failures.
6. Keep PRs reviewable and single-purpose. Never auto-merge, deploy,
   alter active parameters, or edit V1 for a V2 refactor.
