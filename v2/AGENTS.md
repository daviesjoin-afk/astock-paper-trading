# AStock V2: instructions for coding agents

This directory is a **new, independent, paper-only system**. Follow these rules
for all changes under `v2/`. Read `docs/MODULE_BOUNDARIES.md` first.

## Default design

- Keep **one Cargo package and one Rust service**. New modules are allowed only
  for distinct business responsibilities; do not create another crate,
  microservice, message bus, ORM, generic DI framework, or compatibility
  facade merely to reduce file size.
- Code dependency direction: HTTP `main.rs` -> `store/` -> `domain/`.
  The domain must not use networking, storage, filesystem, runtime clock,
  SQLite, axum, tokio, or any V1 modules.
- `domain/contract.rs`: versioned data contracts and identity.
  `portfolio.rs`: cash, lots and T+1 state transitions.
  `calendar.rs`: fixed-version scheduled trading-day facts and source coverage.
  `instrument.rs`: fixed offline security identities and supported quantity-rule applicability.
  `execution.rs`: pure decision policy.
- `src/domain/digest.rs`: SHA-256 and canonical JSON text for content identity.
  `research/research_contract.py`: the producer half of the same identity
  (standard library only). A strategy artifact is reviewed data plus exact
  source bytes; `research/run_research.py` regenerates the reviewed research
  output from a frozen input.
- `store/mod.rs` exclusively owns durable writes, schema, transactions,
  idempotency and storage policy. `store/query.rs` only reads, projects
  and verifies replay; it never commits or writes.
- Python research only emits proposals. It cannot write the ledger, decide
  final order quantity or activate strategies. TypeScript only presents V2 API
  read models; it must not replicate paper execution rules.
- Do not import, reuse, mutate, auto-migrate or query legacy V1 runtime code,
  databases, Cron jobs or broker credentials.

## Business invariants

- Explicit cycle, version, quote identity, source and as-of timestamp;
  never silently use newest/latest or machine time in the core.
- Integer cents and whole-lot quantities; quote freshness, T+1,
  cash budget and market eligibility are fail-closed.
- Strict JSON parsing rejects unexpected nested signal/quote fields.
  Research cannot set `quantity`, `shares`, `amount` or final fills.
- Accepted decision + fill must commit atomically. Same signal identity
  + same payload must be idempotent; conflicting payload must fail.
- Replay must verify the exact persisted request, outcome and fill,
  including rejected decisions. Never silently append earlier
  decision times after a later one.
- Calendar/session snapshots and run version references are immutable. Unknown
  dates and unsupported auctions reject. Replay uses the recorded snapshots;
  unsupported or changed snapshot versions fail rather than reinterpret history.
- Trading remains simulated. No broker endpoints or live-order APIs.
- A reference price cannot fill. Require matching, fresh directional execution
  evidence and capacity for the whole candidate; no reference fallback or partial fill.
- Replay verifies stored decisions; research reproduction verifies that those
  decisions are the ones the fixed strategy definition and frozen input must
  produce. Never present one as evidence for the other.
- A six-digit symbol is syntax only. Store resolves identity from its pinned
  offline snapshot; execution rejects missing/conflicting identities, unsupported
  types/boards/rules and uncovered dates. Research/import cannot declare eligibility.

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
